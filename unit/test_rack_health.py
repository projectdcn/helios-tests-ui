"""Full-rack health check: verdict rules, snapshot diff, which cases -m healthcheck selects, end-to-end GO on the
self-test rack."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from helios.healthcheck import BLOCKER, CASES, HEALTH_ONLY, WARN, diff_snapshots, judge

ROOT = Path(__file__).resolve().parent.parent
ALL_BLOCKERS = {t for t, (g, _) in {**CASES, **HEALTH_ONLY}.items() if g == BLOCKER}


def _all_pass():
    return {t: "PASS" for t in {**CASES, **HEALTH_ONLY}}


def test_all_pass_is_go():
    assert judge(_all_pass()).verdict == "GO"


def test_blocker_fail_partial_or_blocked_is_no_go():
    for v in ("FAIL", "PARTIAL", "BLOCKED"):
        j = judge({**_all_pass(), "TC-BU-009": v}, {"TC-BU-009": "15 slots for 18 trays"})
        assert j.verdict == "NO-GO" and j.blockers == [("TC-BU-009", v, "15 slots for 18 trays")]


def test_warn_fail_does_not_block():
    j = judge({**_all_pass(), "TC-CTL-035": "FAIL"})
    assert j.verdict == "GO" and [w[0] for w in j.warnings] == ["TC-CTL-035"]


def test_known_failure_is_accepted():
    j = judge({**_all_pass(), "TC-BU-009": "FAIL"}, known={"TC-BU-009": "AMD ticket"})
    assert j.verdict == "GO" and j.known == [("TC-BU-009", "FAIL", "AMD ticket")]


def test_skipped_or_missing_blocker_is_incomplete():
    v = _all_pass()
    v["TC-CTL-036"] = "SKIP"
    del v["HC-000"]
    j = judge(v, {"TC-CTL-036": "AFM password not set"})
    assert j.verdict == "INCOMPLETE"
    assert ("TC-CTL-036", "AFM password not set") in j.not_checked and ("HC-000", "not run") in j.not_checked


def test_no_go_wins_over_incomplete():
    v = {**_all_pass(), "HC-001": "FAIL", "TC-CTL-036": "SKIP"}
    assert judge(v).verdict == "NO-GO"


def test_skipped_warn_is_ignored():
    assert judge({**_all_pass(), "HC-002": "SKIP"}).verdict == "GO"


def test_gates_are_valid():
    assert {g for g, _ in {**CASES, **HEALTH_ONLY}.values()} == {BLOCKER, WARN}


def test_diff_snapshots():
    before = {"computes": {"ct05": {"healthy": True}}, "node_agent_slots": {"st6": [1, 2, 3]}}
    after = {"computes": {"ct05": {"healthy": False}, "ct17": {"healthy": True}}, "node_agent_slots": {"st6": []}}
    assert diff_snapshots(before, after) == ["computes.ct05.healthy: True -> False",
                                             "computes.ct17: (new) -> {'healthy': True}",
                                             "node_agent_slots.st6: [1, 2, 3] -> []"]
    assert diff_snapshots(before, before) == []


def test_marker_selects_every_case_and_none_is_disruptive(tmp_path):
    """-m healthcheck collects exactly the CASES + HEALTH_ONLY ids, each with its gate, none disruptive."""
    (tmp_path / "hcdump.py").write_text(textwrap.dedent("""
        import json, os
        def pytest_collection_finish(session):
            out = {}
            for it in session.items:
                tc, hc = it.get_closest_marker("tc"), it.get_closest_marker("healthcheck")
                out[tc.args[0]] = {"gate": hc.args[0], "disruptive": bool(it.get_closest_marker("disruptive"))}
            open(os.environ["HCDUMP"], "w").write(json.dumps(out))
    """))
    dump = tmp_path / "out.json"
    env = {**__import__("os").environ, "PYTHONPATH": str(tmp_path), "HCDUMP": str(dump)}
    subprocess.run([sys.executable, "-m", "pytest", "tests", "-m", "healthcheck", "--collect-only", "-q",
                    "-p", "hcdump", "-p", "no:cacheprovider", "--inventory", "selftest/inventory.yaml"],
                   cwd=ROOT, env=env, capture_output=True, check=True)
    got = json.loads(dump.read_text())
    want = {t: g for t, (g, _) in {**CASES, **HEALTH_ONLY}.items()}
    assert {t: v["gate"] for t, v in got.items()} == want
    assert not [t for t, v in got.items() if v["disruptive"]]


def test_rack_health_tool_says_go_on_selftest_rack(tmp_path):
    r = subprocess.run([sys.executable, "tools/rack_health.py", "--inventory", "selftest/inventory.yaml",
                        "--results-dir", str(tmp_path), "--no-html"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert "RACK HEALTH: GO" in r.stdout
    run = next(tmp_path.glob("selftest-*"))
    doc = json.loads((run / "health.json").read_text())
    assert doc["verdict"] == "GO" and doc["snapshot"]["computes"]
    # judging the same run again against itself as baseline: nothing changed
    r2 = subprocess.run([sys.executable, "tools/rack_health.py", "--run", str(run), "--baseline", str(run / "health.json"),
                         "--no-html"], cwd=ROOT, capture_output=True, text=True)
    assert r2.returncode == 0 and "changed" not in r2.stdout.split("RACK HEALTH")[1]


def _afm_health(tmp_path, faults: str) -> dict[str, str]:
    import csv
    import os
    res = tmp_path / "results"
    subprocess.run([sys.executable, "-m", "pytest", "tests/afm/test_afm_health.py", "--inventory",
                    "selftest/inventory-health.yaml", "--results-dir", str(res), "-q", "-p", "no:cacheprovider"],
                   cwd=ROOT, capture_output=True, text=True, timeout=300,
                   env={**os.environ, "HELIOS_FAKE_AFM_FAULTS": faults})
    with open(next(res.glob("*/tc_summary.csv"))) as f:
        return {r["tc_id"]: r["verdict"] for r in csv.DictReader(f)}


def test_afm_health_checks_pass_on_healthy_fake(tmp_path):
    v = _afm_health(tmp_path, "")
    assert v == {"HC-000": "PASS", "HC-007": "PASS", "HC-008": "PASS", "HC-009": "PASS"}, v


import pytest  # noqa: E402


@pytest.mark.parametrize("fault,tc", [("leftover_test_objects", "HC-007"), ("critical_event", "HC-008"),
                                      ("controller_restarted", "HC-009")])
def test_afm_health_fault_detected(tmp_path, fault, tc):
    v = _afm_health(tmp_path, fault)
    assert {t for t, x in v.items() if x == "FAIL"} == {tc}, v


def test_rack_health_subset_run_judged_on_its_cases(tmp_path):
    """--tc limits the run; the verdict is judged on those cases only (not INCOMPLETE for the rest)."""
    r = subprocess.run([sys.executable, "tools/rack_health.py", "--inventory", "selftest/inventory.yaml",
                        "--results-dir", str(tmp_path), "--no-html", "--", "--tc", "HC-001,HC-003,TC-BU-009"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and "RACK HEALTH: GO" in r.stdout, r.stdout[-1500:]
    doc = json.loads((next(tmp_path.glob("selftest-*")) / "health.json").read_text())
    assert sorted(doc["passed"]) == ["HC-001", "HC-003", "TC-BU-009"] and not doc["not_checked"]


# ------------------------------------------------------------------ root causes, precise known, age, not configured

def test_root_cause_groups_cases_failing_only_on_an_unreachable_node():
    v = {**_all_pass(), "HC-001": "FAIL", "TC-SWH-037": "FAIL", "TC-BU-009": "FAIL", "TC-001": "FAIL"}
    lines = {"HC-001": ["ct05: not answering SSH"],
             "TC-SWH-037": ["st1/asic0: Ethernet9 (To ComputeTray Slot4/GPU3[19]/Port33) is in no VLAN"],
             "TC-BU-009": ["17 sb-node-agent slots [...] for 18 compute trays - no agent for slot 4"],
             "TC-001": ["pod-1-4-ComputeTray (ct05): tray unreachable", "2 LINK_UP ports without an IFCP session: ct02"]}
    j = judge(v, lines=lines, unreachable=["ct05"], tray_slots={"ct05": {"slot": 4, "afm": "pod-1-4-ComputeTray"}})
    assert j.verdict == "NO-GO"                      # grouping never changes the verdict
    assert sorted(j.root_causes["ct05"]) == ["HC-001", "TC-BU-009", "TC-SWH-037"]
    assert j.caused_by("TC-001") is None            # it also fails on ct02: a real blocker of its own


def test_known_match_only_accepts_matching_lines_and_until_expires():
    v = {**_all_pass(), "TC-SWH-055": "FAIL"}
    k = {"TC-SWH-055": {"reason": "CPU voltage reported", "match": "CPU_VDDCR_CPU"}}
    j = judge(v, known=k, lines={"TC-SWH-055": ["st1: voltage CPU_VDDCR_CPU 680.0 below low threshold 700.0"]})
    assert [x[0] for x in j.known] == ["TC-SWH-055"]
    j = judge(v, known=k, lines={"TC-SWH-055": ["st1: voltage CPU_VDDCR_CPU 680.0 below", "st2: fan FAN3 speed 0"]})
    assert not j.known and j.warnings[0][0] == "TC-SWH-055" and "fan FAN3" in j.warnings[0][2]
    j = judge(v, known={"TC-SWH-055": {"reason": "x", "until": "2000-01-01"}})
    assert not j.known and "expired" in j.warnings[0][2]


def test_not_configured_and_stale_results():
    v = {**_all_pass(), "HC-002": "SKIP"}
    checked = {t: "2026-10-16 03:55:00 UTC" for t in v}
    checked["HC-007"] = "2026-10-15 15:15:00 UTC"
    j = judge(v, {"HC-002": "not configured: commands.health.bmc_power not set"}, checked=checked)
    assert j.verdict == "GO" and j.not_configured[0][0] == "HC-002"
    assert j.stale == ["HC-007"]


def test_rerun_failed_reruns_merges_and_judges(tmp_path):
    """A run where HC-001 / HC-003 failed: --rerun-failed re-runs exactly those on the (healthy) self-test rack and
    judges the merged result."""
    import csv as _csv
    old = tmp_path / "selftest-20261015-000000"
    (old / "logs").mkdir(parents=True)
    with open(old / "tc_summary.csv", "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["tc_id", "verdict", "passed", "failed", "skipped", "error", "blocked"])
        for tc in ("HC-001", "HC-003"):
            w.writerow([tc, "FAIL", 0, 1, 0, 0, 0])
        w.writerow(["HC-004", "PASS", 1, 0, 0, 0, 0])
    with open(old / "tc_results.csv", "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["tc_id", "nodeid", "suite", "test", "description", "outcome", "duration_s", "properties", "message",
                    "started", "log"])
        for tc, out in (("HC-001", "failed"), ("HC-003", "failed"), ("HC-004", "passed")):
            w.writerow([tc, f"x::{tc}", "health/test_rack_health", tc, "", out, 1, "", "- c2: down", "2026-10-15 00:00:00 UTC", ""])
    r = subprocess.run([sys.executable, "tools/rack_health.py", "--inventory", "selftest/inventory.yaml", "--rerun-failed",
                        str(old), "--results-dir", str(tmp_path), "--no-html"], cwd=ROOT, capture_output=True, text=True,
                       timeout=600)
    assert "re-running 2 case(s)" in r.stdout and "HC-001, HC-003" in r.stdout, r.stdout[-1500:]
    merged = next(tmp_path.glob("*-merged"))
    doc = json.loads((merged / "health.json").read_text())
    assert doc["verdict"] == "GO" and sorted(doc["passed"]) == ["HC-001", "HC-003", "HC-004"]


def test_hc000_records_slot_to_tray_map(tmp_path):
    """HC-000 learns which inventory tray is in which AFM slot (afmctl accelerator ids) and records it."""
    import csv as _csv
    subprocess.run([sys.executable, "-m", "pytest", "tests/afm/test_afm_health.py", "--tc", "HC-000", "--inventory",
                    "selftest/inventory.yaml", "--results-dir", str(tmp_path), "-q", "-p", "no:cacheprovider"],
                   cwd=ROOT, capture_output=True, text=True, timeout=300)
    row = next(r for r in _csv.DictReader(open(next(tmp_path.glob("*/tc_results.csv")))) if r["tc_id"] == "HC-000")
    ts = json.loads(row["properties"])["snapshot"]["tray_slots"]
    assert ts == {"c1": {"slot": 0, "afm": "pod-1-0-ComputeTray"}, "c2": {"slot": 1, "afm": "pod-1-1-ComputeTray"}}, ts
