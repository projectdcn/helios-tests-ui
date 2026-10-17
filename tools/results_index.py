"""Run index: one row per test item per run, for every results folder under results/runs/<rack>/ - so "every run of
TC-AFM-001, when and with what result" is one lookup instead of reading workbook notes and run folders.

    python tools/results_index.py                      # rebuild results/index.csv from every run folder
    python tools/results_index.py history TC-AFM-001   # that case's runs, newest first
    python tools/results_index.py runs [--rack g02-g17]  # the runs, newest first

The test runner (helios/pytest_plugin.py) adds each new run itself when it finishes. Merged health results
(<run>-merged) and the UI's session folders (<rack>-session-<time>, ui/sessions.py) are derived from other runs and
are not indexed; their source runs are.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
FIELDS = ["rack", "run", "started", "tc_id", "test", "outcome", "duration_s", "message", "log"]


def index_path(results_root: Path = RESULTS) -> Path:
    return results_root / "index.csv"


def tz_offsets(results_root: Path = RESULTS) -> dict[str, float]:
    """Runner clock offsets (hours from UTC) of runs whose 'started' has no ' UTC' suffix (13-14 Oct runs from a
    Mac in PDT): the --tz RUN=HOURS lists in results/notes/*tz-args.txt, as tools/html_report.py uses them."""
    out = {}
    for f in sorted((results_root / "notes").glob("*tz-args.txt")):
        toks = f.read_text().split()
        for i, t in enumerate(toks):
            if t == "--tz" and i + 1 < len(toks) and "=" in toks[i + 1]:
                k, v = toks[i + 1].split("=", 1)
                out[k] = float(v)
    return out


def to_utc(started: str, offset: float | None) -> str:
    from datetime import datetime, timedelta
    s = started.strip()
    if not s or s.endswith("UTC"):
        return s
    try:
        t = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s
    if offset is None:
        return s + " (runner clock)"
    return (t - timedelta(hours=offset)).strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def run_rows(run_dir: Path, rack: str, tz: dict[str, float] | None = None) -> list[dict]:
    """The index rows of one results folder (empty when it has no tc_results.csv or is a merged result / a UI
    session's consolidated folder)."""
    f = run_dir / "tc_results.csv"
    if run_dir.name.endswith("-merged") or "-session-" in run_dir.name or not f.is_file():
        return []
    out = []
    with open(f, newline="") as fh:
        for r in csv.DictReader(fh):
            msg = " ".join((r.get("message") or "").split())
            log = r.get("log") or ""
            out.append({"rack": rack, "run": run_dir.name, "started": to_utc(r.get("started") or "", (tz or {}).get(run_dir.name)),
                        "tc_id": r.get("tc_id") or "", "test": r.get("test") or r.get("nodeid") or "",
                        "outcome": r.get("outcome") or "", "duration_s": r.get("duration_s") or "",
                        "message": msg[:400], "log": f"runs/{rack}/{run_dir.name}/{log}" if log else ""})
    return out


def _all_runs(results_root: Path):
    runs = results_root / "runs"
    for rack_dir in sorted(p for p in runs.iterdir() if p.is_dir()) if runs.is_dir() else []:
        for run_dir in sorted(p for p in rack_dir.iterdir() if p.is_dir()):
            yield rack_dir.name, run_dir


def _write(path: Path, rows: list[dict]) -> None:
    rows.sort(key=lambda r: (r["started"], r["run"], r["test"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".index-", suffix=".csv")
    with os.fdopen(fd, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def read(results_root: Path = RESULTS) -> list[dict]:
    p = index_path(results_root)
    if not p.is_file():
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def rebuild(results_root: Path = RESULTS) -> int:
    tz = tz_offsets(results_root)
    rows = [row for rack, run_dir in _all_runs(results_root) for row in run_rows(run_dir, rack, tz)]
    _write(index_path(results_root), rows)
    return len(rows)


def update_run(run_dir: Path) -> bool:
    """Add (or replace) one run's rows, when the run folder sits in <results>/runs/<rack>/. Returns False when it
    does not (e.g. an explicit --results-dir elsewhere, like the jump host's flat results/)."""
    run_dir = Path(run_dir).resolve()
    if run_dir.parent.parent.name != "runs":
        return False
    results_root, rack = run_dir.parent.parent.parent, run_dir.parent.name
    rows = [r for r in read(results_root) if not (r["rack"] == rack and r["run"] == run_dir.name)]
    _write(index_path(results_root), rows + run_rows(run_dir, rack, tz_offsets(results_root)))
    return True


def history(tc: str, results_root: Path = RESULTS) -> list[dict]:
    return sorted((r for r in read(results_root) if r["tc_id"] == tc), key=lambda r: r["started"], reverse=True)


def runs(results_root: Path = RESULTS, rack: str | None = None) -> list[dict]:
    """One line per run: rack, run, first start, number of items, outcome counts."""
    by: dict[tuple, dict] = {}
    for r in read(results_root):
        if rack and r["rack"] != rack:
            continue
        k = (r["rack"], r["run"])
        d = by.setdefault(k, {"rack": r["rack"], "run": r["run"], "started": r["started"], "items": 0, "outcomes": {}})
        d["started"] = min(d["started"], r["started"]) if d["started"] else r["started"]
        d["items"] += 1
        d["outcomes"][r["outcome"]] = d["outcomes"].get(r["outcome"], 0) + 1
    return sorted(by.values(), key=lambda d: d["started"], reverse=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=RESULTS, help="results root (default results/)")
    sub = ap.add_subparsers(dest="cmd")
    h = sub.add_parser("history", help="every run of one case, newest first")
    h.add_argument("tc")
    r = sub.add_parser("runs", help="every run, newest first")
    r.add_argument("--rack")
    a = ap.parse_args(argv)
    if a.cmd == "history":
        rows = history(a.tc, a.results)
        for x in rows:
            print(f"{x['started']:<24} {x['rack']:<9} {x['run']:<34} {x['outcome']:<8} {x['test']}  {x['message'][:120]}")
        print(f"{len(rows)} execution(s) of {a.tc}", file=sys.stderr)
    elif a.cmd == "runs":
        for x in runs(a.results, a.rack):
            oc = " ".join(f"{k}={v}" for k, v in sorted(x["outcomes"].items()))
            print(f"{x['started']:<24} {x['rack']:<9} {x['run']:<34} {x['items']:>4} items  {oc}")
    else:
        n = rebuild(a.results)
        print(f"{index_path(a.results)}: {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
