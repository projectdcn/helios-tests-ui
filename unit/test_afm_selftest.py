"""Self-test of tests/afm against selftest/fake_afm.py: all cases pass on a healthy fake controller, and
each injected controller fault fails exactly the test-plan cases that must catch it.

The fault map was established by running the whole AFM suite once per fault (only these IDs flipped);
here each fault run is limited to its IDs with --tc to keep the unit suite fast.
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAULTS = {
    "cookie_flags": {"TC-CTL-022"},
    "cswsh": {"TC-AFM-AGGW-025"},
    "ws_rbac_leak": {"TC-AFM-AGGW-015", "TC-AFM-AGGW-016", "TC-AFM-AGGW-029", "TC-CTL-026"},
    "ws_rbac_stale": {"TC-AFM-AGGW-029", "TC-CTL-026"},
    "no_update_event": {"TC-AFM-AGGW-010", "TC-AFM-AGGW-011", "TC-AFM-AGGW-012", "TC-CTL-011"},
    "rbac_bypass": {"TC-AFM-043", "TC-CTL-045", "TC-CTL-046", "TC-AFM-RC-022", "TC-AFM-AGGW-015",
                    "TC-AFM-AGGW-016", "TC-AFM-AGGW-029", "TC-CTL-026"},
    "unauth_ok": {"TC-AFM-RC-021", "TC-CTL-089"},
    "audit_missing_delete": {"TC-AFM-048A", "TC-CTL-051"},
    "dup_vpod_ok": {"TC-CTL-044", "TC-CTL-096"},
    "system_name_ok": {"TC-E2E-021"},
    "ws_no_close": {"TC-AFM-AGGW-026", "TC-CTL-023"},
    "gpu_not_ready": {"TC-CTL-090"},
    "dup_acc": {"TC-CTL-090"},
    "quorum_down": {"TC-CTL-036", "TC-AFM-AGGW-028"},
    "metrics_missing": {"TC-AFM-053", "TC-CTL-048"},
    "minirack_drain_ok": {"TC-AFM-035A"},
}


def _run(tmp_path: Path, faults: str = "", tcs: set[str] | None = None) -> dict[str, str]:
    results = tmp_path / "results"
    args = [sys.executable, "-m", "pytest", str(ROOT / "tests" / "afm"), "--inventory",
            str(ROOT / "selftest" / "inventory.yaml"), "--results-dir", str(results), "--allow-disruptive",
            "-q", "-p", "no:cacheprovider", "-o", "log_cli=false"]
    if tcs:
        args += ["--tc", ",".join(sorted(tcs))]
    env = {**os.environ, "HELIOS_FAKE_AFM_FAULTS": faults}
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=600, env=env)
    summaries = list(results.glob("*/tc_summary.csv"))
    assert summaries, f"no results written\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
    with open(summaries[0]) as f:
        return {row["tc_id"]: row["verdict"] for row in csv.DictReader(f)}


def test_healthy_fake_all_pass(tmp_path):
    verdicts = _run(tmp_path)
    bad = {k: v for k, v in verdicts.items() if v != "PASS"}
    assert not bad, bad
    assert len(verdicts) >= 60


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_fault_detected(tmp_path, fault):
    verdicts = _run(tmp_path, fault, FAULTS[fault])
    assert {k for k, v in verdicts.items() if v == "FAIL"} == FAULTS[fault], verdicts
