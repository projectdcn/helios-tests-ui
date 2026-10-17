"""Results retention: cap what grows without bound, never lose a result.

    python tools/results_retention.py            # dry run: what would be compressed / deleted
    python tools/results_retention.py --apply    # do it

Per rack (results/runs/<rack>/):
  * tc_results.csv, tc_summary.csv, health.json, reports, results/index.csv: kept forever (small; the evidence
    behind the workbook and the reports)
  * per-test command logs (<run>/logs/): kept in full for the newest ``keep_full_runs`` runs OR the last
    ``keep_full_days`` days (whichever keeps more), then compressed to <run>/logs.tar.gz, deleted after
    ``delete_logs_days`` days
  * UI live logs (ui-runs/<id>.log): the newest ``ui_logs_per_rack``; older .log files are deleted (the small .json
    run record stays)
Protected (never touched): the runs listed in results/notes/*runs*.txt (the published reports are built from them),
every <run>-merged folder and the runs it was merged from, every UI session folder (<rack>-session-<time>/, ui/sessions.py)
and the runs its session.json lists. Limits: results/retention.yaml (defaults below).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def default_results_root() -> Path:
    """results/ of this checkout, or $HELIOS_RESULTS_ROOT (an installed UI keeps its results outside the code)."""
    v = os.environ.get("HELIOS_RESULTS_ROOT", "").strip()
    return Path(v).expanduser() if v else ROOT / "results"


RESULTS = default_results_root()
DEFAULTS = {"keep_full_runs": 30, "keep_full_days": 30, "delete_logs_days": 90, "ui_logs_per_rack": 200}


def limits(results_root: Path = RESULTS) -> dict:
    f = results_root / "retention.yaml"
    cfg = dict(DEFAULTS)
    if f.is_file():
        cfg.update({k: int(v) for k, v in (yaml.safe_load(f.read_text()) or {}).items() if k in DEFAULTS})
    return cfg


def protected(results_root: Path = RESULTS) -> set[str]:
    """Run folder names that must keep their logs: report run lists, merged results and their sources, UI session
    folders and their runs."""
    keep: set[str] = set()
    for f in (results_root / "notes").glob("*runs*.txt"):
        keep |= {Path(t).name for t in f.read_text().split() if t.strip()}
    for m in (results_root / "runs").glob("*/*-merged"):
        keep.add(m.name)
        src = m / "merged-from.txt"
        if src.is_file():
            keep |= {n.strip() for n in src.read_text().splitlines() if n.strip()}
    for s in (results_root / "runs").glob("*/*-session-*"):
        if not s.is_dir():
            continue
        keep.add(s.name)
        try:
            runs = json.loads((s / "session.json").read_text()).get("runs") or []
        except (OSError, ValueError, AttributeError):
            continue
        keep |= {Path(str(n)).name for n in runs if str(n).strip()}
    return keep


def run_time(run: Path) -> float:
    """When the run happened: the <rack>-YYYYmmdd-HHMMSS in its name (copies / extraction keep it), else mtime."""
    import re
    from datetime import datetime
    m = re.search(r"(\d{8}-\d{6})", run.name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
        except ValueError:
            pass
    return run.stat().st_mtime


def _size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else (p.stat().st_size if p.exists() else 0)


def plan(results_root: Path = RESULTS, now: float | None = None) -> list[tuple[str, Path, int]]:
    """[(action, path, bytes)] with action in compress-logs / delete-logs / delete-archive / delete-ui-log."""
    cfg, keep, now = limits(results_root), protected(results_root), now or time.time()
    day = 86400
    acts = []
    for rack_dir in sorted(p for p in (results_root / "runs").glob("*") if p.is_dir()):
        runs = sorted((p for p in rack_dir.iterdir() if p.is_dir() and p.name != "ui-runs"),
                      key=run_time, reverse=True)
        newest = {p.name for p in runs[:cfg["keep_full_runs"]]}
        for r in runs:
            if r.name in keep or r.name in newest:
                continue
            age = (now - run_time(r)) / day
            logs, arc = r / "logs", r / "logs.tar.gz"
            if age > cfg["delete_logs_days"]:
                if logs.is_dir():
                    acts.append(("delete-logs", logs, _size(logs)))
                if arc.is_file():
                    acts.append(("delete-archive", arc, _size(arc)))
            elif age > cfg["keep_full_days"] and logs.is_dir():
                acts.append(("compress-logs", logs, _size(logs)))
        ui = rack_dir / "ui-runs"
        if ui.is_dir():
            ui_logs = sorted(ui.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            acts += [("delete-ui-log", f, f.stat().st_size) for f in ui_logs[cfg["ui_logs_per_rack"]:]]
    return acts


def apply(acts) -> None:
    for action, path, _ in acts:
        if action == "compress-logs":
            arc = path.parent / "logs.tar.gz"
            with tarfile.open(arc, "w:gz") as t:
                t.add(path, arcname="logs")
            with tarfile.open(arc) as t:              # verify before deleting the originals
                n = len([m for m in t.getmembers() if m.isfile()])
            if n == len([f for f in path.rglob("*") if f.is_file()]):
                shutil.rmtree(path)
        elif action in ("delete-logs",):
            shutil.rmtree(path)
        elif action in ("delete-archive", "delete-ui-log"):
            path.unlink()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=RESULTS)
    ap.add_argument("--apply", action="store_true", help="do it (default: only print what would be done)")
    a = ap.parse_args(argv)
    acts = plan(a.results)
    cfg = limits(a.results)
    print(f"limits: {cfg} (results/retention.yaml); protected runs: {len(protected(a.results))}")
    for action, path, size in acts:
        print(f"{'' if a.apply else '[dry run] '}{action:15s} {size / 1e6:8.1f} MB  {path.relative_to(a.results)}")
    print(f"{len(acts)} action(s), {sum(s for _, _, s in acts) / 1e6:.1f} MB")
    if a.apply:
        apply(acts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
