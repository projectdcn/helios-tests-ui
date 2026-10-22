"""Data-path model of the simulated rack: TransferBench jobs become flows over the cabled planes, and the switch
counters (queue packets / drops / watermarks, PG drops / watermarks, buffer-pool watermarks, PFC pause frames)
follow from the load on each egress port and the switch's QoS profile (fake_qos).

A GPU reaches every other GPU over its 36 ports, one per plane; plane p connects GPU port p of every GPU on one
switch ASIC. A flow of R GB/s from GPU a to GPU b puts R/planes on each plane: in at the switch port cabled to a's
port p (ingress PG), out at the port cabled to b's port p (egress queue). A 400G port carries 50 GB/s. When the flows
towards one egress port offer more than that, the port is oversubscribed by r = offered / 50:
- the traffic class lossless (PFC enabled, profile mr3 / lossless): the egress queue fills to the lossless limit, the
  ingress PGs of the contributing ports reach xoff and the switch sends PFC pause frames towards the sources; nothing
  is dropped, the sources slow down to their fair share;
- lossy (profile none): the queue fills its share of the pool and the excess is dropped.
Below line rate the queues stay at a few tens of KB (g02-g17 21 Oct: 23 KB at 0.21 of line rate).

The numbers are modelled (GPU send rate, packet size, pause quanta), chosen to match what g02-g17 showed where we
have a measurement (a2a 2-rank job ~370 GB/s, in-tray a2a ~199.6 GB/s, idle watermark 420 B); they show behaviour,
not performance.
"""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict

PORT_GBPS = 50.0               # 400G port = 50 GB/s
GPU_SEND_GBPS = 400.0          # one GPU's one-way send rate over the fabric (modelled)
A2A_PAIR_GBPS = 372.0          # a 2-rank a2a job (g02-g17 21 Oct: 368-376 GB/s per pair)
IN_TRAY_A2A_GBPS = 199.6       # single-node a2a, XGMI inside the tray (g02-g17 16 Oct: 199.59 GB/s)
PKT = 4096                     # IFoE payload per packet (modelled)
IDLE_WM = 420.0                # idle queue watermark (bytes), as on g02-g17 / MR3
LOSSLESS_WM = 645540.0         # lossless queue / PG at xoff headroom
LOSSY_WM = 1_500_000.0         # lossy queue at its share of the pool
RANK_WAIT_S = 3.0              # how long rank 0 waits for the other ranks of its job


def _pair() -> list:
    return [0.0, 0.0]


def _bytes(size: str) -> float:
    """'1G' / '256M' / '4096' -> bytes (TransferBench sizes)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMG]?)", (size or "1G").strip().upper())
    return float(m.group(1)) * {"": 1, "K": 2**10, "M": 2**20, "G": 2**30}[m.group(2)] if m else float(2**30)


class Fabric:
    def __init__(self, sim):
        self.sim = sim
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.jobs: dict[tuple, dict] = {}              # (master addr, port) -> {"n", "ranks": {rank: slot}, "spec"}
        self.recent: list[dict] = []                   # flows of the last window: concurrency between jobs
        self.q = defaultdict(_pair)                    # (sw, ns, port, "UCn") -> [packets, drops]
        self.qwm = defaultdict(float)                  # (sw, ns, port, "UCn") -> peak bytes since clear
        self.pg_drop = defaultdict(float)              # (sw, ns, port, "PGn") -> drops
        self.pgwm = defaultdict(float)                 # (sw, ns, port, "PGn") -> peak bytes since clear
        self.pool = defaultdict(float)                 # (sw, ns, pool) -> peak bytes since clear
        self.pfc = defaultdict(float)                  # (sw, ns, port, "Rx"|"Tx", prio) -> pause frames
        self.t0 = time.monotonic()

    def __getstate__(self):
        d = dict(self.__dict__)
        for k in ("lock", "cv", "_idx"):
            d.pop(k, None)
        d["jobs"] = {}                           # half-joined jobs do not survive a restart
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)

    # ------------------------------------------------------------ topology helpers
    def _port_of(self, slot: int, gpu: int, pid: int):
        """(sw, ns, EthernetN) cabled to GPU port ``pid`` of (slot, gpu), or None (index built once: cabling is fixed)."""
        idx = self.__dict__.get("_idx")
        if idx is None:
            idx = {}
            for i in range(self.sim.n_sw):
                for ns in ("asic0", "asic1"):
                    for p, (s, g, _acc, q) in self.sim.ports(i, ns).items():
                        idx[(s, g, q)] = (i, ns, p)
            self._idx = idx
        return idx.get((slot, gpu, pid))

    def _classes(self, i, ns):
        """(traffic class queue, PG, lossless?) the IFoE data uses on this ASIC: priority 0 -> TC0 -> PG0 / UC0."""
        qos = self.sim.qos_tables(i, ns)
        pfc = any("0" in (m.get("pfc_enable") or "").split(",") for m in (qos.get("PORT_QOS_MAP") or {}).values())
        return "UC0", "PG0", pfc

    # ------------------------------------------------------------ TransferBench
    def transferbench(self, slot: int, cmd: str) -> tuple[int, str]:
        """(rc, stdout) of a TransferBench command run on compute ``slot``."""
        env = dict(re.findall(r"\b(TB_[A-Z_]+)=(\S+)", cmd))
        args = re.split(r"\s+", cmd.split("TransferBench", 1)[1].strip()) if "TransferBench" in cmd else []
        preset = args[0] if args else ""
        if "TB_NUM_RANKS" not in env:
            return self._single_node(preset, args)
        n = int(env.get("TB_NUM_RANKS", "1"))
        rank = int(env.get("TB_RANK", "0") or 0)
        key = (env.get("TB_MASTER_ADDR", ""), env.get("TB_MASTER_PORT", "29500"))
        expr = None
        if preset == "cmdline":
            m = re.search(r"cmdline\s+(['\"])(.*?)\1\s*(\S*)", cmd) or re.search(r"cmdline\s+(\S+)()", cmd)
            expr = m.group(2) if m and m.lastindex >= 2 and m.group(2) else (m.group(1) if m else "")
            size = m.group(3) if m and m.lastindex == 3 and m.group(3) else "1G"
        else:
            size = args[1] if len(args) > 1 else "1G"
        with self.cv:
            job = self.jobs.get(key)
            if job is None or job.get("done"):
                job = self.jobs[key] = {"n": n, "ranks": {}, "preset": preset, "expr": expr, "t": time.monotonic(),
                                        "bytes": _bytes(size)}
            if rank in job["ranks"]:
                # g02-g17 19 Oct: two jobs on one master port - the second rank cannot join
                return 1, "[ERROR] recv: Invalid argument (rank already connected on this master port)\n"
            job["ranks"][rank] = slot
            self.cv.notify_all()
            if rank != 0:
                return 0, ""                      # TB_SINGLE_LOG: only rank 0 reports
            end = time.monotonic() + RANK_WAIT_S
            while len(job["ranks"]) < job["n"] and time.monotonic() < end:
                self.cv.wait(0.05)
            if len(job["ranks"]) < job["n"]:
                job["done"] = True
                return 1, f"[ERROR] timed out waiting for {job['n'] - len(job['ranks'])} rank(s) to connect\n"
            job["done"] = True
            job["key"] = key
            return 0, self._run_job(job)

    def _single_node(self, preset: str, args: list[str]) -> tuple[int, str]:
        size = args[1] if len(args) > 1 else "268435456"
        if preset in ("", "help", "presets", "envvars"):
            return 0, "TransferBench v1.70.02 (simulated)\nUsage: TransferBench config <N>\n"
        rows = "\n".join(f"│  GPU {a:02d} │ " + "  ".join("   N/A " if a == b else f"{IN_TRAY_A2A_GBPS / 12:7.2f}" for b in range(4))
                         + f" │ {IN_TRAY_A2A_GBPS / 4:10.2f} │" for a in range(4))
        return 0, (f"TransferBench v1.70.02 (simulated) (Single-node mode)\nTest 1: {preset} {size} bytes\n{rows}\n"
                   f"Average   bandwidth (GPU Timed):   {IN_TRAY_A2A_GBPS / 12:.3f} GB/s\n"
                   f"Aggregate bandwidth (GPU Timed):  {IN_TRAY_A2A_GBPS:.3f} GB/s\n")

    def _flows(self, job: dict) -> list[tuple[int, int, int, int, float]]:
        """[(src slot, src gpu, dst slot, dst gpu, wanted GB/s)] of a job (fault tb_slow: a tenth of it - the load is
        then far below the destination's line rate and TC-SWH-060 is BLOCKED, not judged)."""
        slow = 0.1 if "tb_slow" in self.sim.f else 1.0
        return [(s, g, d, h, w * slow) for s, g, d, h, w in self._flows_wanted(job)]

    def _flows_wanted(self, job: dict) -> list[tuple[int, int, int, int, float]]:
        ranks = job["ranks"]
        out = []
        if job["expr"]:
            # cmdline '<n> <se> (RaGx->RaGx->RbGy) ...': one-way transfers between GPUs of two ranks
            for ra, ga, rb, gb in re.findall(r"R(\d+)G(\d+)\s*->\s*R\d+[GD]\d+\s*->\s*R(\d+)G(\d+)", job["expr"]):
                ra, rb = int(ra), int(rb)
                if ra in ranks and rb in ranks and ranks[ra] != ranks[rb]:
                    out.append((ranks[ra], int(ga), ranks[rb], int(gb), GPU_SEND_GBPS))
            return out
        # a2a over the ranks: every GPU to every GPU of the other ranks, both ways; a pair of trays moves
        # A2A_PAIR_GBPS in total (half each way, spread over the 4 x 4 GPU pairs)
        slots = [ranks[r] for r in sorted(ranks)]
        per = A2A_PAIR_GBPS / 2 / 16
        for a in slots:
            for b in slots:
                if a != b:
                    out += [(a, ga, b, gb, per) for ga in range(4) for gb in range(4)]
        return out

    def _run_job(self, job: dict) -> str:
        now = time.monotonic()
        flows = self._flows(job)
        # concurrency: jobs that started within the window (the N jobs of one incast round start together); a new
        # round of the same job (same master address / port) replaces that job's earlier flows - rounds are sequential
        window = 1.0
        self.recent = [f for f in self.recent if now - f["t"] < window and f["job"] != job.get("key")] + [
            {"t": now, "job": job.get("key"), "src": (s, g), "dst": (d, h), "want": w} for s, g, d, h, w in flows]
        load = self._account(self.recent, flows, now, job["bytes"])
        got = sum(load.values())
        lines = ["TransferBench v1.70.02 (simulated) (with NIC+POD support)",
                 f"Test 1: {job['preset']} ({len(flows)} transfers over {len(job['ranks'])} ranks)"]
        lines += [f"  R{r}: slot {s}" for r, s in sorted(job["ranks"].items())]
        lines.append(f"Aggregate bandwidth (GPU Timed):  {got:.3f} GB/s")
        return "\n".join(lines) + "\n"

    def _account(self, active: list[dict], mine, now, nbytes: float) -> dict:
        """Share the egress ports among the active flows (proportional per port, one plane each), then add this
        job's packets / drops / pauses / watermarks: each flow moves ``nbytes`` at its achieved rate, so the counters
        grow by what that transfer would send, drop or pause. Returns {flow index: achieved GB/s}."""
        sim = self.sim
        planes = sorted(p for p in sim.gpu_pids() if sim.pid_link(p) is not None)
        offered = defaultdict(float)                    # egress port -> GB/s offered by all active flows
        for f in active:
            for p in planes:
                e = self._port_of(f["dst"][0], f["dst"][1], p)
                if e:
                    offered[e] += f["want"] / len(planes)
        if "no_congestion_watermark" in sim.f:
            offered = defaultdict(float, {k: min(v, PORT_GBPS * 0.2) for k, v in offered.items()})
        legs = []                                      # (flow n, egress, ingress, wanted, achieved, ratio)
        for n, (s, g, d, h, w) in enumerate(mine):
            for p in planes:
                e, ing = self._port_of(d, h, p), self._port_of(s, g, p)
                if not e or not ing or sim.link_state(*e) != "up" or sim.link_state(*ing) != "up":
                    continue
                want = w / len(planes)
                r = offered[e] / PORT_GBPS
                legs.append((n, e, ing, want, want if r <= 1 else want / r, r))
        achieved = defaultdict(float)
        for n, _e, _i, _w, share, _r in legs:
            achieved[n] += share
        with self.lock:
            for n, e, ing, want, share, r in legs:
                dur = nbytes / (achieved[n] * 1e9) if achieved[n] else 0.0      # seconds this transfer takes
                uc, pg, lossless = self._classes(*e[:2])
                qk = (*e, uc)
                self.q[qk][0] += share * 1e9 * dur / PKT
                if r > 1:
                    if lossless and "lossless_incast_drops" not in sim.f:
                        self.qwm[qk] = max(self.qwm[qk], LOSSLESS_WM)
                        self.pgwm[(*ing, pg)] = max(self.pgwm[(*ing, pg)], LOSSLESS_WM)
                        # pause frames: one per ~2 us the ingress is held (the share of time above line rate)
                        self.pfc[(*ing, "Tx", 0)] += max(1.0, dur * (1 - 1 / r) * 5e5)
                        self.pool[(e[0], e[1], "ingress_lossless_pool")] = max(
                            self.pool[(e[0], e[1], "ingress_lossless_pool")], LOSSLESS_WM * min(r, 64))
                    else:
                        self.qwm[qk] = max(self.qwm[qk], LOSSY_WM)
                        self.q[qk][1] += (want - share) * 1e9 * dur / PKT
                    self.pool[(e[0], e[1], "egress_pool")] = max(self.pool[(e[0], e[1], "egress_pool")],
                                                                 self.qwm[qk] * min(r, 64))
                else:
                    self.qwm[qk] = max(self.qwm[qk], IDLE_WM + 100_000 * r)
        return dict(achieved)

    # ------------------------------------------------------------ counters (switch CLI)
    def clear_queue_wm(self, i, ns):
        with self.lock:
            for k in [k for k in self.qwm if k[0] == i and k[1] == ns]:
                del self.qwm[k]

    def clear_buffer_wm(self, i, ns):
        with self.lock:
            for d in (self.pgwm,):
                for k in [k for k in d if k[0] == i and k[1] == ns]:
                    del d[k]
            for k in [k for k in self.pool if k[0] == i and k[1] == ns]:
                del self.pool[k]

    def idle_pg7_drops(self, i, ns, port) -> float:
        """Lossy PG7 drops that grow without test traffic (g02-g17 138k-158k per port after ~6 days, MR3 ~56 an
        hour): the switch's own control traffic on a PG without a buffer. Fault-free racks: fault pg7_idle_drops."""
        if "pg7_idle_drops" not in self.sim.f:
            return 0.0
        return float(int((time.monotonic() - self.t0) * 0.27))
