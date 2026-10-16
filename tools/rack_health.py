#!/usr/bin/env python3
"""Full-rack health check: run every ``healthcheck`` case, then say GO / NO-GO for disruptive tests.

    python tools/rack_health.py --inventory ~/helios/fullrack-jump-afm.yaml
    python tools/rack_health.py --inventory ... --known testplan/health-known.yaml --baseline results/<run>/health.json
    python tools/rack_health.py --run results/<run>          # judge an existing run again (no rack access)

Read-only (no case in the check is disruptive). Export HELIOS_AFM_PASSWORD first for the AFM part; without it those
blocker cases SKIP and the verdict is INCOMPLETE.

Verdict: NO-GO when a blocker case fails, is blocked or only partially passes (unless accepted in --known);
INCOMPLETE when a blocker case could not run; GO otherwise. Warn-gate failures are listed but never block.
Writes <run>/health.json (verdict, blockers, warnings, HC-000 snapshot) and <run>/health.html (unless --no-html).
Exit code: 0 GO, 1 NO-GO, 2 INCOMPLETE.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from helios.healthcheck import (BAD, diff_snapshots, gate_of, judge, meaning_of, quarantine_only_cases,  # noqa: E402
                               read_details, read_run)


def load_known(path: Path | None) -> dict:
    """YAML of accepted, already-reported failures: 'TC-ID: reason' or 'TC-ID: {reason, match, until}' (match: regex -
    only failure lines matching it are accepted; until: YYYY-MM-DD after which the entry no longer applies)."""
    if not path:
        return {}
    import yaml
    return {str(k): v for k, v in (yaml.safe_load(path.read_text()) or {}).items()}


def run_started(run_dir: Path) -> str | None:
    """Earliest 'started' (UTC) of a results folder: the start of that health run."""
    import csv
    st = [r["started"] for r in csv.DictReader(open(run_dir / "tc_results.csv", newline="")) if r.get("started")]
    return min(st) if st else None


def failed_cases(run_dir: Path) -> list[str]:
    """Health-check cases of a run that did not pass (FAIL / BLOCKED / PARTIAL) - for --rerun-failed."""
    import csv
    return sorted(r["tc_id"] for r in csv.DictReader(open(run_dir / "tc_summary.csv", newline=""))
                  if gate_of(r["tc_id"]) and r["verdict"] in BAD)


def merge_runs(runs: list[Path], results_dir: Path) -> Path:
    """One results folder from several health runs (e.g. a full run + a re-run of its failed checks): every case
    comes from the LATEST run that ran it (its result rows and summary row); logs are kept per source run."""
    import csv
    import shutil
    runs = sorted(runs, key=lambda r: r.name)            # run folders are named <rack>-YYYYmmdd-HHMMSS
    out = results_dir / f"{runs[-1].name}-merged"
    if out.exists():
        shutil.rmtree(out)
    (out / "logs").mkdir(parents=True)
    owner: dict[str, Path] = {}
    for r in runs:
        for row in csv.DictReader(open(r / "tc_summary.csv", newline="")):
            owner[row["tc_id"]] = r
    res_rows, sum_rows, res_fields, sum_fields = [], [], None, None
    for r in runs:
        rows = list(csv.DictReader(open(r / "tc_results.csv", newline="")))
        res_fields = res_fields or (list(rows[0]) if rows else None)
        for row in rows:
            if owner.get(row["tc_id"]) == r:
                if row.get("log"):
                    src = r / row["log"]
                    row["log"] = f"logs/{r.name}/{Path(row['log']).name}"
                    if src.exists():
                        (out / "logs" / r.name).mkdir(exist_ok=True)
                        shutil.copy2(src, out / row["log"])
                res_rows.append(row)
        for row in csv.DictReader(open(r / "tc_summary.csv", newline="")):
            sum_fields = sum_fields or list(row)
            if owner.get(row["tc_id"]) == r:
                sum_rows.append(row)
    for name, fields, rows in (("tc_results.csv", res_fields, res_rows), ("tc_summary.csv", sum_fields, sum_rows)):
        with open(out / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    (out / "merged-from.txt").write_text("\n".join(r.name for r in runs) + "\n")
    return out


def run_pytest(inventory: str, results_dir: Path, extra: list[str], since: str | None = None) -> Path:
    """``since`` (UTC 'YYYY-mm-dd HH:MM:SS'): HC-008 looks for critical AFM events since then (the previous run)."""
    import os
    before = set(results_dir.glob("*-2*")) if results_dir.exists() else set()
    cmd = [sys.executable, "-m", "pytest", "tests", "-m", "healthcheck", "--inventory", inventory,
           "--results-dir", str(results_dir), "-q", "-p", "no:cacheprovider", *extra]
    print("$", " ".join(cmd), flush=True)
    env = {**os.environ, **({"HELIOS_HEALTH_SINCE": since} if since else {})}
    subprocess.run(cmd, cwd=ROOT, env=env)
    new = sorted(set(results_dir.glob("*-2*")) - before, key=lambda p: p.stat().st_mtime)
    if not new:
        sys.exit("no results folder written - see the pytest output above")
    return new[-1]


def _age(j, tc: str) -> str:
    return f" [checked {j.checked.get(tc, '')[:16]} - older result]" if tc in j.stale else ""


def notes(j, diffs: list[str]) -> str:
    lines = [f"{'+' if j.verdict == 'GO' else '!'} Rack health: {j.verdict}"
             f"{' (with quarantine: ' + ', '.join(sorted(j.quarantine)) + ')' if j.quarantine and j.verdict == 'GO' else ''}"
             " for disruptive tests "
             f"({len(j.blockers)} blocker(s), {len(j.warnings)} warning(s), {len(j.known)} known, "
             f"{len(j.not_checked)} blocker case(s) not checked)."]
    lines += [f"Quarantined (left out of every check): {n} - {why}" for n, why in sorted(j.quarantine.items())]
    for node, tcs in j.root_causes.items():
        b = [t for t, _, _ in j.blockers if t in tcs]
        w = [t for t, _, _ in j.warnings if t in tcs]
        lines.append(f"! ROOT CAUSE {node} unreachable: {len(b)} blocker(s) and {len(w)} warning(s) fail only on it - "
                     f"{', '.join(b + w)}")
    lines += [f"! BLOCKER {tc} ({v}) {meaning_of(tc)}: {msg}{_age(j, tc)}" for tc, v, msg in j.blockers
              if not j.caused_by(tc)]
    lines += [f"Not checked: {tc} {meaning_of(tc)} - {why}" for tc, why in j.not_checked]
    lines += [f"Known (accepted): {tc} ({v}) - {why}" for tc, v, why in j.known]
    lines += [f"! warning {tc} ({v}) {meaning_of(tc)}: {msg}{_age(j, tc)}" for tc, v, msg in j.warnings
              if not j.caused_by(tc)]
    lines += [f"Not configured: {tc} {meaning_of(tc)} - {why}" for tc, why in j.not_configured]
    if j.stale:
        lines.append(f"Older results (more than 12 h before the newest): {', '.join(j.stale)}")
    if diffs:
        lines.append(f"Changed since the baseline ({len(diffs)}): " + "; ".join(diffs[:30]))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", help="inventory to run against (or $HELIOS_INVENTORY)")
    ap.add_argument("--run", type=Path, help="judge this existing results folder instead of running")
    ap.add_argument("--rerun-failed", type=Path, metavar="RUN",
                    help="re-run the health checks that did not pass in RUN, merge with RUN and judge the merged result "
                         "(RUN's health.json is the baseline unless --baseline is given)")
    ap.add_argument("--merge", type=Path, nargs="+", metavar="RUN",
                    help="merge these health runs (latest result per case wins) into <latest>-merged and judge that")
    ap.add_argument("--known", type=Path, help="YAML {TC-ID: reason} of accepted failures (do not block)")
    ap.add_argument("--baseline", type=Path, help="health.json of an earlier run: report what changed")
    ap.add_argument("--results-dir", type=Path, default=ROOT / "results")
    ap.add_argument("--no-html", action="store_true")
    ap.add_argument("pytest_args", nargs="*", help="extra pytest arguments after --, e.g. -- --computes ct01,ct03")
    a = ap.parse_args(argv)

    import os
    if a.rerun_failed:
        inv = a.inventory or os.environ.get("HELIOS_INVENTORY")
        if not inv:
            ap.error("--inventory (or HELIOS_INVENTORY) is required with --rerun-failed")
        tcs = failed_cases(a.rerun_failed)
        if not tcs:
            print(f"nothing to re-run: every health check passed in {a.rerun_failed}")
            return 0
        print(f"re-running {len(tcs)} case(s) that did not pass in {a.rerun_failed.name}: {', '.join(tcs)}")
        new = run_pytest(inv, a.results_dir, ["--tc", ",".join(tcs), *a.pytest_args], run_started(a.rerun_failed))
        run_dir = a.run = merge_runs([a.rerun_failed, new], a.results_dir)
        if not a.baseline and (a.rerun_failed / "health.json").exists():
            a.baseline = a.rerun_failed / "health.json"
    elif a.merge:
        run_dir = a.run = merge_runs(a.merge, a.results_dir)
    elif a.run:
        run_dir = a.run
    else:
        inv = a.inventory or os.environ.get("HELIOS_INVENTORY")
        if not inv:
            ap.error("--inventory (or HELIOS_INVENTORY) is required unless --run is given")
        since = None
        if a.baseline:
            try:
                since = run_started(Path(json.loads(a.baseline.read_text())["run"]))
            except (OSError, KeyError, ValueError):
                since = None
        run_dir = run_pytest(inv, a.results_dir, a.pytest_args, since)

    verdicts, messages, snapshot = read_run(run_dir)
    # re-judging an existing run: only the cases that run collected are expected (a case added to the check later
    # is not "not run"); a live run expects every blocker case
    # a run limited with --tc (e.g. re-running the failed cases) is judged on the cases it ran, too
    subset = any(x == "--tc" or x.startswith("--tc=") for x in a.pytest_args)
    expected = {t for t in verdicts if gate_of(t)} if (a.run or subset) else None
    lines, checked = read_details(run_dir)
    qonly, qlist = quarantine_only_cases(run_dir)
    j = judge({t: v for t, v in verdicts.items() if gate_of(t)}, messages, load_known(a.known), expected,
              lines=lines, unreachable=snapshot.get("unreachable") or [], tray_slots=snapshot.get("tray_slots"),
              checked=checked, quarantine_only=qonly, quarantine=qlist)
    diffs: list[str] = []
    if a.baseline:
        base = json.loads(a.baseline.read_text())
        before = base.get("snapshot") or {}
        if subset or a.run:      # a partial run only compares the snapshot sections it collected
            before = {k: v for k, v in before.items() if k in snapshot}
        diffs = diff_snapshots(before, snapshot)
    out = {"run": str(run_dir), "judged": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
           **j.as_dict(), "baseline": str(a.baseline) if a.baseline else None, "changed": diffs, "snapshot": snapshot}
    (run_dir / "health.json").write_text(json.dumps(out, indent=1))
    (run_dir / "health-notes.txt").write_text(notes(j, diffs))

    qtag = f" (with quarantine: {', '.join(sorted(j.quarantine))})" if j.quarantine and j.verdict == "GO" else ""
    print(f"\nRACK HEALTH: {j.verdict}{qtag}   ({run_dir})")
    for n, why in sorted(j.quarantine.items()):
        print(f"  QUARANTINED {n:<8} {why}")
    if j.quarantine_only:
        print(f"  passed without the quarantined node(s): {', '.join(sorted(j.quarantine_only))}")
    for node, tcs in j.root_causes.items():
        print(f"  ROOT CAUSE {node} unreachable -> {len(tcs)} case(s) fail only on it: {', '.join(tcs)}")
    for tc, v, msg in j.blockers:
        if not j.caused_by(tc):
            print(f"  BLOCKER  {tc:<11} {v:<8} {meaning_of(tc)}{_age(j, tc)}\n           {msg[:220]}")
    for tc, why in j.not_checked:
        print(f"  NOT RUN  {tc:<11} {meaning_of(tc)} - {why[:160]}")
    for tc, v, why in j.known:
        print(f"  KNOWN    {tc:<11} {v:<8} {why}")
    for tc, v, msg in j.warnings:
        if not j.caused_by(tc):
            print(f"  warning  {tc:<11} {v:<8} {meaning_of(tc)}: {msg[:160]}{_age(j, tc)}")
    for tc, why in j.not_configured:
        print(f"  NOT CONFIGURED {tc} {meaning_of(tc)} - {why[:140]}")
    if j.stale:
        print(f"  older results (> 12 h before the newest): {', '.join(j.stale)}")
    print(f"  passed: {len(j.passed)} case(s)")
    for d in diffs:
        print(f"  changed  {d}")

    if not a.no_html:
        html = run_dir / "health.html"
        subprocess.run([sys.executable, str(ROOT / "tools/html_report.py"), str(run_dir), "-o", str(html),
                        "--title", f"Rack health check - {j.verdict}",
                        "--subtitle", f"{len(j.blockers)} blocker(s), {len(j.warnings)} warning(s); judged {out['judged']}",
                        "--notes", str(run_dir / "health-notes.txt")], cwd=ROOT, check=False)
        print(f"  report: {html}")
    return {"GO": 0, "NO-GO": 1}.get(j.verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
