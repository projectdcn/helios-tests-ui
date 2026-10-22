"""Labelled training data from the virtual rack: run TC-SWH-061 / 062 (FDB), 046 (PFC / QoS), 060 (incast congestion)
and 063 (lossless configuration) many times on a private
virtual rack (selftest/vrack.py, g02-g17 layout) with varied injected faults, FDB targets, switches and QoS profiles,
then export SLM examples (tools/dataset_export.py) split into train / eval.

    python tools/vrack_dataset.py --runs 120 --out dataset/vrack-1
    python tools/vrack_dataset.py --runs 30 --qos lossless --out /tmp/ds --seed 7

Writes <out>/runs/<run>/ (results + scenario.json: the injected faults = the labels), <out>/all.jsonl,
<out>/train.jsonl, <out>/eval.jsonl, <out>/summary.json.

Everything is simulated (counters, timings, AFM behaviour are the model's), on 127.0.0.1 only, with its own ports and
state folder - your ~/.helios-vrack and every real rack are untouched. The simulator's AFM login is handed to the
test runs in their environment only (selftest/fake_afm.py ADMIN; not a real credential). Use this data to teach the
model the format and the fault classes; judge it on real-rack runs (tools/dataset_export.py on real run folders).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "selftest"))

import dataset_export as dx  # noqa: E402

TCS = ("TC-SWH-061", "TC-SWH-062", "TC-SWH-046", "TC-SWH-060", "TC-SWH-063")
SWITCHES = [f"st{n}" for n in range(1, 7)]
# (faults, weight): healthy runs and each fault class, alone and combined
SCENARIOS = [
    ([], 4),
    (["no_fdb_reconcile"], 2),                 # AFM never notices an FDB mismatch
    (["fdb_detect_no_repair"], 2),             # notices (event), never repairs
    (["fdb_wrong_port"], 2),                   # repairs a deleted entry on another port
    (["pfc_storm"], 3),                        # watchdog storm, or (params watchdog false) pauses only
    (["qos_odd_port"], 2),                     # one port with another QoS map (QoS profiles mr3 / lossless)
    (["lossless_incast_drops"], 2),            # PFC does not hold the incast
    (["no_congestion_watermark"], 2),          # the switch reports no congestion under incast
    (["tb_slow"], 2),                          # the incast load stays far below line rate (BLOCKED)
    (["rack_vpod_configfailed"], 1),           # AFM pre-flight: system vPOD configurationfailed (g02-g17 18 / 20 Oct)
    (["rack_compute_stale"], 1),               # AFM pre-flight: a compute stopped reporting health (g02-g17 20 Oct)
    (["cni_netns"], 1),
    (["afm_unreachable"], 1),                  # the AFM API resets every connection (MR3 21-22 Oct)
    (["no_fdb_reconcile", "pfc_storm"], 1),
]


def _http(url: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


class Vrack:
    """A private virtual rack (selftest/vrack.py) for one QoS profile."""

    def __init__(self, out: Path, qos: str, ssh_port: int, afm_port: int):
        self.dir = out / f"vrack-{qos}"
        self.inv = self.dir / "vrack.yaml"
        self.ctl = f"http://127.0.0.1:{afm_port + 1}"
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "selftest" / "vrack.py"), "--topology", "g02-g17", "--ssh-port", str(ssh_port),
             "--afm-port", str(afm_port), "--bmc-port", str(ssh_port + 200), "--state-dir", str(self.dir / "state"),
             "--inventory", str(self.inv), "--time-scale", "0.05", "--fresh", "--qos", qos],
            stdout=open(self.dir / "vrack.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
        end = time.monotonic() + 120
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise SystemExit(f"virtual rack exited: see {self.dir / 'vrack.log'}")
            try:
                if self.inv.is_file():
                    _http(self.ctl + "/")
                    return
            except OSError:
                pass
            time.sleep(1)
        self.stop()
        raise SystemExit(f"virtual rack did not come up in 120 s: see {self.dir / 'vrack.log'}")

    def faults(self, want: list[str], params: dict | None = None) -> None:
        now = _http(self.ctl + "/faults").get("faults", [])
        _http(self.ctl + "/faults", {"remove": [f for f in now if f not in want], "add": list(want),
                                     "params": params or {}})

    def stop(self) -> None:
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)


def _sim_env(inv: Path) -> dict:
    """The simulator's AFM login for the test run (environment only, never written)."""
    import fake_afm
    import yaml
    data = yaml.safe_load(inv.read_text()) or {}
    var = (((data.get("afm") or {}).get("auth") or {}).get("password_env")) or "HELIOS_AFM_PASSWORD"
    return {var: fake_afm.ADMIN[1]}


def run_once(vr: Vrack, runs: Path, faults: list[str], seed: int, switch: str, qos: str,
             params: dict, scope: dict) -> Path | None:
    vr.faults(faults, params)
    before = {p.name for p in runs.iterdir()} if runs.is_dir() else set()
    env = {**os.environ, **_sim_env(vr.inv), "HELIOS_FDB_TARGET_SEED": str(seed), "PYTHONHASHSEED": str(seed)}
    args = [sys.executable, "-m", "pytest", "tests/afm/test_afm_switch_faults.py", "tests/l1/test_switch_health.py",
            "--inventory", str(vr.inv), "--tc", ",".join(TCS), "--switches", switch, "--allow-disruptive",
            "--results-dir", str(runs), "-q", "-p", "no:cacheprovider", "-o", "log_cli=false"]
    p = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    new = sorted({d.name for d in runs.iterdir() if d.is_dir()} - before) if runs.is_dir() else []
    vr.faults([])
    if not new:
        print(f"  no results written (pytest rc {p.returncode}): {p.stdout[-400:]}{p.stderr[-400:]}", file=sys.stderr)
        return None
    run = runs / new[-1]
    (run / "scenario.json").write_text(json.dumps(
        {"rack": "vrack-g02-g17", "faults": faults, "params": params, "scope": scope,
         "seed": seed, "switch_046": switch, "qos": qos, "time_scale": 0.05}, indent=1) + "\n")
    return run


def split(recs: list[dict], frac: float, rng: random.Random) -> tuple[list, list]:
    """Train / eval by run (all examples of one run on the same side), every fault class represented in eval."""
    by_run: dict[str, list] = {}
    for r in recs:
        by_run.setdefault(r["meta"]["run"], []).append(r)
    runs = list(by_run)
    rng.shuffle(runs)
    n_eval = max(1, round(len(runs) * frac)) if len(runs) > 1 else 0
    ev = set(runs[:n_eval])
    fault = lambda r: json.loads(r["messages"][-1]["content"])["fault"]  # noqa: E731
    for f in {fault(r) for r in recs}:                 # a class only in train: move one of its runs to eval
        if not any(fault(r) == f for run in ev for r in by_run[run]):
            cand = [run for run in runs if run not in ev and any(fault(r) == f for r in by_run[run])]
            if len(cand) > 1:
                ev.add(cand[0])
    tr = [r for run in runs if run not in ev for r in by_run[run]]
    return tr, [r for run in runs if run in ev for r in by_run[run]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=60, help="test runs in total (each runs the 5 cases; ~40 s each)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--qos", default="none,mr3,lossless", help="QoS profiles of the virtual rack, runs split over them")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--eval-fraction", type=float, default=0.15)
    ap.add_argument("--only", default="", help="run only the scenarios with these faults, e.g. afm_unreachable,cni_netns")
    ap.add_argument("--export-only", action="store_true", help="no new runs: export <out>/runs again (e.g. after "
                                                                  "changing tools/dataset_export.py)")
    ap.add_argument("--legacy-share", type=float, default=0.5,
                    help="share of the faults also given as an older run reported them (raw traceback / check "
                         "message, no timeline) - real runs before 22 Oct look like that")
    ap.add_argument("--ssh-port", type=int, default=23100, help="first SSH port of the private virtual rack")
    ap.add_argument("--afm-port", type=int, default=19443)
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    out = a.out.resolve()
    runs = out / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    only = {f.strip() for f in a.only.split(",") if f.strip()}
    scenarios = [(f, w) for f, w in SCENARIOS if not only or set(f) & only] or SCENARIOS
    profiles = [q.strip() for q in a.qos.split(",") if q.strip()]
    if a.export_only:
        profiles = []
    per = [a.runs // len(profiles) + (1 if k < a.runs % len(profiles) else 0) for k in range(len(profiles))]
    done = 0
    t0 = time.monotonic()
    for qos, n in zip(profiles, per):
        if not n:
            continue
        print(f"== virtual rack, QoS {qos}: {n} run(s)")
        vr = Vrack(out, qos, a.ssh_port, a.afm_port)
        try:
            for _ in range(n):
                faults = rng.choices([s for s, _w in scenarios], weights=[w for _s, w in scenarios])[0]
                if "qos_odd_port" in faults and qos == "none":      # no QoS map to make odd: a storm instead
                    faults = ["pfc_storm"]
                params, scope = {}, {}
                if "pfc_storm" in faults:      # a different stormed switch / port / priority / rate each run
                    k = rng.randrange(len(SWITCHES))
                    params["pfc_storm"] = {"switch": k, "asic": rng.randrange(2), "port": rng.randrange(216),
                                           "prio": rng.choice([0, 1, 3, 4]),
                                           "frames": rng.choice([5000, 40000, 250000, 900000]),
                                           "watchdog": rng.random() < 0.6}
                    scope["pfc_storm"] = [SWITCHES[k]]
                if "rack_compute_stale" in faults:
                    params["rack_compute_stale"] = {"slot": rng.randrange(17)}
                if "qos_odd_port" in faults:
                    k = rng.randrange(len(SWITCHES))
                    params["qos_odd_port"] = {"switch": k, "asic": rng.randrange(2), "port": rng.randrange(216)}
                    scope["qos_odd_port"] = [SWITCHES[k]]
                # TC-SWH-046 / 063 look at one switch: mostly the affected one when there is a switch fault
                hit = [v[0] for v in scope.values()]
                switch = hit[0] if hit and rng.random() < 0.75 else rng.choice(SWITCHES)
                seed = rng.randrange(1, 10**6)
                run = run_once(vr, runs, faults, seed, switch, qos, params, scope)
                done += 1
                print(f"  [{done}/{a.runs}] {run.name if run else '-'}  faults {faults or 'none'}  046 on {switch}  "
                      f"({time.monotonic() - t0:.0f} s)", flush=True)
        finally:
            vr.stop()
    recs, seen = [], set()
    lrng = random.Random(a.seed * 7919)            # the older-style variants: their own stream, stable per seed
    for run in dx.run_dirs([runs]):
        exs = dx.examples(run)
        exs += [v for ex in exs if lrng.random() < a.legacy_share for v in [dx.legacy_variant(ex, lrng)] if v]
        for ex in exs:
            key = dx._strip_ids(ex["input"]) + json.dumps(ex["output"], sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            recs.append(dx.as_record(ex, "chat"))
    train, evl = split(recs, a.eval_fraction, rng)
    for name, rs in (("all", recs), ("train", train), ("eval", evl)):
        with open(out / f"{name}.jsonl", "w") as fh:
            fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rs)
    fault = lambda r: json.loads(r["messages"][-1]["content"])["fault"]  # noqa: E731
    summary = {"runs": len(dx.run_dirs([runs])), "new_runs": done, "examples": len(recs), "train": len(train),
               "eval": len(evl), "legacy_variants": sum(1 for r in recs if r["meta"].get("variant") == "legacy"),
               "needs_review": sum(1 for r in recs if r["meta"].get("needs_review")),
               "by_fault": dict(Counter(fault(r) for r in recs)), "eval_by_fault": dict(Counter(fault(r) for r in evl)),
               "seed": a.seed, "qos": profiles, "source": "virtual rack (simulated)"}
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
