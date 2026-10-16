"""Reusable datapath workloads: L2 ping, TransferBench, RCCL.

Shared by the L1 datapath cases and by every case that re-validates traffic
after a trigger (RAS resets, reboots, link events). Each helper records
failures in ``checks`` and returns the measurements; the caller asserts.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import pytest

from .compute import render
from .parsers import parse_bandwidth, parse_rccl, parse_size
from .rack import Checks, fan_out


def use_sudo(inventory) -> bool:
    """commands.datapath.sudo: run the traffic tools (TransferBench, RCCL) with sudo - for an account that is not in
    the 'render' group and so cannot open /dev/kfd (g02-g17 tenant-hpe: 'no ROCm-capable device is detected')."""
    return bool(inventory.cmd("datapath.sudo", False))


def run_on(node, cmd: str, inventory, timeout: float):
    return node.run(cmd, timeout=timeout, sudo=use_sudo(inventory))


def addr(node) -> str:
    """IP address of a compute for cross-tray rendezvous (master address, NCCL_COMM_ID, host lists): resolved on the
    runner, because the trays themselves may not resolve each other's names (g02-g17 *.enclave-d, 14 Oct).
    host.extra.data_addr overrides."""
    h = str(node.host.extra.get("data_addr") or node.host.host)
    try:
        ipaddress.ip_address(h)
        return h
    except ValueError:
        pass
    try:
        return socket.gethostbyname(h)
    except OSError:
        return h


def template(inventory, key: str) -> str:
    t = inventory.cmd(key)
    if not t:
        pytest.skip(f"commands.{key} not set in {inventory.path.name}")
    return t


def compute_pairs(computes, mode: str = "ring"):
    """Ordered (src, dst) compute pairs; both directions are always covered."""
    if len(computes) < 2:
        pytest.skip("cross-compute test needs >= 2 computes")
    if mode == "all":
        return [(a, b) for a in computes for b in computes if a is not b]
    ring = [(computes[i], computes[(i + 1) % len(computes)]) for i in range(len(computes))]
    if len(computes) == 2:
        return ring  # a ring of two already holds both directions
    return ring + [(b, a) for a, b in ring]


def _workers(inventory, key: str, n: int, default: bool) -> int:
    return n if inventory.th(f"datapath.{key}", default) else 1


# ------------------------------------------------------------------ L2 ping

def _l2_ping_afmctl(computes, inventory, checks: Checks, evidence: list | None = None) -> int:
    """L2 ping driven by afmctl discovery (no per-rack IDs to maintain): accelerator IDs from
    `afmctl show device --json`, ports = LINK_UP on both ends from `afmctl show port -b <bdf> --json`.
    Pairs (``l2ping.pairs``): ring = next GPU on the same tray + same-index GPU on the next tray (both
    directions across trays), as the full-rack suite does; full = every GPU pair. Pings are serial per
    source tray (afmctl ping is not re-entrant per host) and parallel across trays."""
    from .afmctl import Afmctl, ping_evidence

    timeout = float(inventory.cmd("l2ping.timeout_s", 200))
    mode = inventory.cmd("l2ping.pairs", "ring")
    min_ports = int(inventory.th("datapath.l2ping.min_common_ports", 1))

    def discover(c):
        a = Afmctl(c)
        devs = a.devices()
        return a, devs, {d.bdf: {p.id for p in a.ports(d.bdf) if p.up} for d in devs}

    info = {}
    for c, res, exc in fan_out(discover, computes):
        if exc:
            checks.fail(f"{c.name}: afmctl discovery failed: {exc}")
            continue
        a, devs, up = res
        checks.check(len(devs) == c.gpus, f"{c.name}: afmctl shows {len(devs)} devices, expected {c.gpus}")
        not_active = [f"{d.bdf}={d.config_phase}" for d in devs if d.config_phase != "ACTIVE"]
        checks.check(not not_active, f"{c.name}: config_phase not ACTIVE: {not_active}")
        info[c.name] = (a, devs, up)

    names = [c.name for c in computes if c.name in info]
    jobs: dict[str, list[tuple]] = {n: [] for n in names}
    for i, name in enumerate(names):
        _, devs, up = info[name]
        peers = names[i + 1:] + names[:i] if mode == "full" else names[i + 1:i + 2] or names[:1]
        peers = [p for p in peers if p != name]
        for g, d in enumerate(devs):
            targets = [("within", name, h) for h in range(len(devs)) if h != g] if mode == "full" else \
                      ([("within", name, (g + 1) % len(devs))] if len(devs) > 1 else [])
            for pn in peers:
                pdevs = info[pn][1]
                targets += [("cross", pn, h) for h in range(len(pdevs))] if mode == "full" else \
                           ([("cross", pn, g)] if g < len(pdevs) else [])
            for kind, pn, h in targets:
                dd = info[pn][1][h]
                jobs[name].append((kind, d, pn, dd, sorted(up[d.bdf] & info[pn][2][dd.bdf])))

    def run(name):
        a = info[name][0]
        fails = []
        for kind, d, pn, dd, common in jobs[name]:
            tag = f"[{kind}] {name}/{d.bdf} (acc {d.accel_id}) -> {pn} acc {dd.accel_id}"
            if len(common) < min_ports:
                fails.append(f"{tag}: only {len(common)} LINK_UP port(s) in common")
                continue
            res, r = a.ping(d.bdf, dd.accel_id, common, timeout=timeout)
            n = len(fails)
            if r.rc == 124:
                fails.append(f"{tag}: exceeded {timeout:.0f}s")
            elif res.total is None:
                fails.append(f"{tag}: no 'IFoE Request' summary{' (host busy)' if res.busy else ''}: {r.tail(200)!r}")
            elif not res.ok:
                fails.append(f"{tag}: {res.request_fail}/{res.total} port(s) failed {res.failed_ports or ''}".rstrip())
            if evidence is not None and len(fails) > n:
                evidence.append(ping_evidence(tag, r))
        return fails

    for name, fails, exc in fan_out(run, names):
        if exc:
            checks.fail(f"{name}: {exc}")
        for f in fails or []:
            checks.fail(f)
    return sum(len(v) for v in jobs.values())


def l2_ping(computes, inventory, checks: Checks, evidence: list | None = None) -> int:
    """Within each tray (every GPU pair) and across trays (ring/all, both directions). Returns ping count.
    ``evidence`` (afmctl mode): each failed ping's command, exit code and full output is appended to it."""
    if inventory.cmd("l2ping.mode") == "afmctl":
        return _l2_ping_afmctl(computes, inventory, checks, evidence)
    tmpl = template(inventory, "l2ping.cmd")
    ok_re = inventory.cmd("l2ping.success_regex")
    timeout = float(inventory.cmd("l2ping.timeout_s", 30))
    sudo = bool(inventory.cmd("l2ping.sudo", False))
    for c in computes:  # fail fast on missing per-GPU inventory the template needs
        for key, getter in (("{src_bdf}", c.gpu_bdf), ("{dst_accel}", c.accel_id)):
            if key in tmpl and not all(getter(i) for i in range(c.gpus)):
                pytest.skip(f"{c.name}: commands.l2ping.cmd uses {key} but inventory lacks "
                            f"{'gpu_bdfs' if 'bdf' in key else 'accel_ids'} for all {c.gpus} GPUs")

    jobs: dict[str, list[tuple]] = {c.name: [] for c in computes}
    for c in computes:
        jobs[c.name] += [("within", c, s, c, d) for s in range(c.gpus) for d in range(c.gpus) if s != d]
    if len(computes) >= 2:
        for a, b in compute_pairs(computes, inventory.cmd("l2ping.cross_pairs", "ring")):
            jobs[a.name] += [("cross", a, s, b, d) for s in range(a.gpus) for d in range(b.gpus)]

    def run_src(c):
        fails = []
        for kind, src, s, dst, d in jobs[c.name]:
            cmd = render(tmpl, src_name=src.name, src_gpu=s, src_addr=src.gpu_addr(s), src_bdf=src.gpu_bdf(s),
                         src_accel=src.accel_id(s), dst_name=dst.name, dst_host=dst.host.host, dst_gpu=d,
                         dst_addr=dst.gpu_addr(d), dst_bdf=dst.gpu_bdf(d), dst_accel=dst.accel_id(d))
            r = src.run(cmd, timeout=timeout, sudo=sudo)
            if not (r.ok and (not ok_re or re.search(ok_re, r.stdout))):
                fails.append(f"[{kind}] {src.name}/gpu{s} -> {dst.name}/gpu{d}: rc={r.rc} {r.tail(200)!r}")
        return fails

    for c, fails, exc in fan_out(run_src, computes):
        if exc:
            checks.fail(f"{c.name}: {exc}")
        for f in fails or []:
            checks.fail(f)
    return sum(len(v) for v in jobs.values())


# ------------------------------------------------------------ TransferBench

def _tb_common(inventory, size):
    regexes = inventory.cmd("transferbench.bw_regex")
    regexes = [regexes] if isinstance(regexes, str) else regexes
    kw = dict(ports=inventory.cmd("transferbench.ports", ""), size=size or inventory.cmd("transferbench.size", "1G"))
    return regexes, kw, float(inventory.cmd("transferbench.timeout_s", 600))


def _tb_check(tag, r, exc, regexes, min_bw, checks):
    if exc or not r.ok:
        checks.fail(f"{tag}: TransferBench failed: {exc or f'rc={r.rc} {r.tail()!r}'}")
        return None
    bw = parse_bandwidth(r.stdout, regexes)
    if checks.check(bw is not None, f"{tag}: no bandwidth figure in output (calibrate commands.transferbench.bw_regex)"):
        if min_bw is not None:
            checks.check(bw >= min_bw, f"{tag}: {bw:.1f} GB/s < {min_bw} GB/s")
    return bw


def transferbench_in_domain(computes, inventory, checks: Checks, size: str | None = None) -> dict:
    tmpl = template(inventory, "transferbench.in_domain")
    regexes, kw, timeout = _tb_common(inventory, size)
    min_bw = inventory.th("datapath.transferbench.in_domain_min_gbps")

    def run(c):
        return run_on(c, render(tmpl, name=c.name, gpus=c.gpus, **kw), inventory, timeout)

    return {c.name: _tb_check(c.name, r, exc, regexes, min_bw, checks)
            for c, r, exc in fan_out(run, computes, _workers(inventory, "parallel_computes", len(computes), True))}


class RankAborted(RuntimeError):
    """A rank killed because another rank of the same job failed first."""


def run_ranks(jobs: list[tuple], timeout: float, sudo: bool = False, abort_on_fail: bool = True) -> list:
    """Start every (node, command) at once (one SSH channel each) and wait for all of them.
    For socket-bootstrapped multi-rank tools (TransferBench TB_RANK, RCCL NCCL_COMM_ID) - no node-to-node SSH or
    MPI needed. Returns [(node, CmdResult | None, exc | None)] in order.

    abort_on_fail: as soon as one rank fails, the others are killed (they would otherwise wait for their peer
    until the timeout - 300 s per tray pair on g02-g17, 14 Oct); they come back as RankAborted."""
    tag = uuid.uuid4().hex[:12]
    cmds = [f"HELIOS_RUN={tag} {cmd}" for _, cmd in jobs]       # marks this job's processes for pkill
    out: list = [None] * len(jobs)
    aborted_by: str | None = None
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futs = {ex.submit(node.run, cmd, timeout=timeout, sudo=sudo): i for i, ((node, _), cmd) in enumerate(zip(jobs, cmds))}
        pending = set(futs)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                i = futs[f]
                try:
                    r, exc = f.result(), None
                except Exception as e:  # noqa: BLE001 - reported per rank
                    r, exc = None, e
                out[i] = (jobs[i][0], r, exc)
                failed = exc is not None or not r.ok
                if failed and abort_on_fail and aborted_by is None and pending:
                    aborted_by = jobs[i][0].name
                    for j in {futs[p] for p in pending}:
                        try:   # [N] keeps pkill from matching its own command line
                            jobs[j][0].run(f"pkill -TERM -f 'HELIOS_RU[N]={tag}'", timeout=30, sudo=sudo)
                        except Exception:  # noqa: BLE001 - best effort; the remote timeout still applies
                            pass
    if aborted_by:
        for i, (node, r, exc) in enumerate(out):
            if node.name != aborted_by and exc is None and r is not None and not r.ok:
                out[i] = (node, None, RankAborted(f"killed after the rank on {aborted_by} failed"))
    return out


def _tb_multirank(pairs, inventory, checks, regexes, kw, timeout, min_bw) -> dict:
    """Cross-domain TransferBench in socket multi-rank mode: rank 0 on src, rank 1 on dst."""
    tmpl = inventory.cmd("transferbench.rank_cmd")
    out = {}
    for a, b in pairs:
        tag = f"{a.name}->{b.name}"
        jobs = [(node, render(tmpl, rank=r, num_ranks=2, master_addr=addr(a), name=node.name, gpus=node.gpus, **kw))
                for r, node in enumerate((a, b))]
        res = run_ranks(jobs, timeout, sudo=use_sudo(inventory))
        for node, r, exc in res[1:]:
            checks.check(not exc and r.ok, f"{tag}: worker rank on {node.name} failed: {exc or f'rc={r.rc} {r.tail()!r}'}")
        _, r0, exc0 = res[0]
        out[tag] = _tb_check(tag, r0, exc0, regexes, min_bw, checks)
    return out


def require_transferbench_cross(inventory) -> None:
    """Skip unless cross-domain TransferBench is configured (multi-rank rank_cmd or single cross_domain)."""
    if not inventory.cmd("transferbench.rank_cmd"):
        template(inventory, "transferbench.cross_domain")


def transferbench_cross_domain(computes, inventory, checks: Checks, size: str | None = None) -> dict:
    if inventory.cmd("transferbench.rank_cmd"):
        regexes, kw, timeout = _tb_common(inventory, size)
        pairs = compute_pairs(computes, inventory.cmd("transferbench.cross_pairs", "ring"))
        return _tb_multirank(pairs, inventory, checks, regexes, kw, timeout,
                             inventory.th("datapath.transferbench.cross_domain_min_gbps"))
    tmpl = template(inventory, "transferbench.cross_domain")
    regexes, kw, timeout = _tb_common(inventory, size)
    min_bw = inventory.th("datapath.transferbench.cross_domain_min_gbps")
    pairs = compute_pairs(computes, inventory.cmd("transferbench.cross_pairs", "ring"))

    def run(pair):
        a, b = pair
        return run_on(a, render(tmpl, src_name=a.name, dst_name=b.name, src_host=addr(a), dst_host=addr(b),
                                gpus=a.gpus, **kw), inventory, timeout)

    return {f"{a.name}->{b.name}": _tb_check(f"{a.name}->{b.name}", r, exc, regexes, min_bw, checks)
            for (a, b), r, exc in fan_out(run, pairs, _workers(inventory, "parallel_pairs", len(pairs), False))}


# --------------------------------------------------------------------- RCCL

def _rccl_kw(inventory) -> dict:
    return dict(
        rccl_tests_dir=inventory.cmd("rccl.tests_dir", "/opt/rccl-tests"),
        min_bytes=inventory.cmd("rccl.min_bytes", "1K"),
        max_bytes=inventory.cmd("rccl.max_bytes", "1G"),
        iters=inventory.cmd("rccl.iters", 20),
    )


def check_rccl(tag: str, r, collective: str, inventory, checks: Checks, section: str) -> float | None:
    if not r.ok:
        checks.fail(f"{tag}: {collective} rc={r.rc} {r.tail()!r}")
        return None
    res = parse_rccl(r.stdout)
    max_bytes = parse_size(inventory.cmd("rccl.max_bytes", "1G"))
    if not checks.check(res.rows, f"{tag}: {collective} produced no result rows"):
        return None
    checks.check(res.max_size >= max_bytes, f"{tag}: {collective} stopped at {res.max_size} B (< {max_bytes} B)")
    for row in res.wrong_rows:
        checks.fail(f"{tag}: {collective} data errors at {row.size} B (oop={row.oop_wrong}, ip={row.ip_wrong})")
    checks.check(not res.out_of_bounds, f"{tag}: {collective} {res.out_of_bounds} out-of-bounds values")
    checks.check(not res.errors, f"{tag}: {collective} errors: {res.errors[:3]}")
    floor = inventory.th(f"datapath.{section}.{collective}_min_busbw_gbps")
    if floor is not None and res.avg_busbw is not None:
        checks.check(res.avg_busbw >= floor, f"{tag}: {collective} avg busbw {res.avg_busbw} < {floor} GB/s")
    return res.avg_busbw


def rccl_in_domain(computes, inventory, checks: Checks, collective: str) -> dict:
    tmpl = template(inventory, "rccl.in_domain")
    timeout = float(inventory.cmd("rccl.timeout_s", 900))
    kw = _rccl_kw(inventory)

    def run(c):
        return run_on(c, render(tmpl, collective=collective, gpus=c.gpus, name=c.name, **kw), inventory, timeout)

    busbw = {}
    for c, r, exc in fan_out(run, computes, _workers(inventory, "parallel_computes", len(computes), True)):
        if exc:
            checks.fail(f"{c.name}: {collective}: {exc}")
            continue
        busbw[c.name] = check_rccl(c.name, r, collective, inventory, checks, "rccl")
    return busbw


def rccl_cross_domain(computes, inventory, checks: Checks, collective: str) -> float | None:
    tmpl = template(inventory, "rccl.cross_domain")
    if len(computes) < 2:
        pytest.skip("cross-domain RCCL needs >= 2 computes")
    timeout = float(inventory.cmd("rccl.timeout_s", 900))
    hostlist = ",".join(f"{addr(c)}:{c.gpus}" for c in computes)
    r = run_on(computes[0], render(tmpl, collective=collective, hostlist=hostlist, np=sum(c.gpus for c in computes),
                                   nodes=len(computes), **_rccl_kw(inventory)), inventory, timeout)
    return check_rccl(f"cross[{len(computes)} nodes]", r, collective, inventory, checks, "rccl_cross")
