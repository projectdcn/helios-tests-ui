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

from helios.healthcheck import diff_snapshots, gate_of, judge, meaning_of, read_run  # noqa: E402


def load_known(path: Path | None) -> dict[str, str]:
    """YAML {TC-ID: reason} of accepted, already-reported failures (e.g. TC-SWH-057: radvd not running - AMD ticket)."""
    if not path:
        return {}
    import yaml
    return {str(k): str(v) for k, v in (yaml.safe_load(path.read_text()) or {}).items()}


def run_pytest(inventory: str, results_dir: Path, extra: list[str]) -> Path:
    before = set(results_dir.glob("*-2*")) if results_dir.exists() else set()
    cmd = [sys.executable, "-m", "pytest", "tests", "-m", "healthcheck", "--inventory", inventory,
           "--results-dir", str(results_dir), "-q", "-p", "no:cacheprovider", *extra]
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT)
    new = sorted(set(results_dir.glob("*-2*")) - before, key=lambda p: p.stat().st_mtime)
    if not new:
        sys.exit("no results folder written - see the pytest output above")
    return new[-1]


def notes(j, diffs: list[str]) -> str:
    lines = [f"{'+' if j.verdict == 'GO' else '!'} Rack health: {j.verdict} for disruptive tests "
             f"({len(j.blockers)} blocker(s), {len(j.warnings)} warning(s), {len(j.known)} known, "
             f"{len(j.not_checked)} blocker case(s) not checked)."]
    lines += [f"! BLOCKER {tc} ({v}) {meaning_of(tc)}: {msg}" for tc, v, msg in j.blockers]
    lines += [f"Not checked: {tc} {meaning_of(tc)} - {why}" for tc, why in j.not_checked]
    lines += [f"Known (accepted): {tc} ({v}) - {why}" for tc, v, why in j.known]
    lines += [f"! warning {tc} ({v}) {meaning_of(tc)}: {msg}" for tc, v, msg in j.warnings]
    if diffs:
        lines.append(f"Changed since the baseline ({len(diffs)}): " + "; ".join(diffs[:30]))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", help="inventory to run against (or $HELIOS_INVENTORY)")
    ap.add_argument("--run", type=Path, help="judge this existing results folder instead of running")
    ap.add_argument("--known", type=Path, help="YAML {TC-ID: reason} of accepted failures (do not block)")
    ap.add_argument("--baseline", type=Path, help="health.json of an earlier run: report what changed")
    ap.add_argument("--results-dir", type=Path, default=ROOT / "results")
    ap.add_argument("--no-html", action="store_true")
    ap.add_argument("pytest_args", nargs="*", help="extra pytest arguments after --, e.g. -- --computes ct01,ct03")
    a = ap.parse_args(argv)

    if a.run:
        run_dir = a.run
    else:
        import os
        inv = a.inventory or os.environ.get("HELIOS_INVENTORY")
        if not inv:
            ap.error("--inventory (or HELIOS_INVENTORY) is required unless --run is given")
        run_dir = run_pytest(inv, a.results_dir, a.pytest_args)

    verdicts, messages, snapshot = read_run(run_dir)
    # re-judging an existing run: only the cases that run collected are expected (a case added to the check later
    # is not "not run"); a live run expects every blocker case
    expected = {t for t in verdicts if gate_of(t)} if a.run else None
    j = judge({t: v for t, v in verdicts.items() if gate_of(t)}, messages, load_known(a.known), expected)
    diffs: list[str] = []
    if a.baseline:
        base = json.loads(a.baseline.read_text())
        diffs = diff_snapshots(base.get("snapshot") or {}, snapshot)
    out = {"run": str(run_dir), "judged": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
           **j.as_dict(), "baseline": str(a.baseline) if a.baseline else None, "changed": diffs, "snapshot": snapshot}
    (run_dir / "health.json").write_text(json.dumps(out, indent=1))
    (run_dir / "health-notes.txt").write_text(notes(j, diffs))

    print(f"\nRACK HEALTH: {j.verdict}   ({run_dir})")
    for tc, v, msg in j.blockers:
        print(f"  BLOCKER  {tc:<11} {v:<8} {meaning_of(tc)}\n           {msg[:220]}")
    for tc, why in j.not_checked:
        print(f"  NOT RUN  {tc:<11} {meaning_of(tc)} - {why[:160]}")
    for tc, v, why in j.known:
        print(f"  KNOWN    {tc:<11} {v:<8} {why}")
    for tc, v, msg in j.warnings:
        print(f"  warning  {tc:<11} {v:<8} {meaning_of(tc)}: {msg[:160]}")
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
