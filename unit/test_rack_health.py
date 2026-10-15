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
