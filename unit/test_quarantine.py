"""Quarantine: a switch / compute tray marked 'quarantine:' in the inventory is left out of every check, passing
checks are PARTIAL (quarantined), and the health check can be GO with quarantine."""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from helios.inventory import load_inventory

ROOT = Path(__file__).resolve().parent.parent


def _copy_selftest(tmp_path: Path, quarantine: bool, break_c2: bool) -> Path:
    work = tmp_path / "selftest"
    shutil.copytree(ROOT / "selftest", work)
    inv = yaml.safe_load((work / "inventory.yaml").read_text())
    for c in inv["computes"]:
        if c["name"] == "c2":
            c["slot"] = 1
            if quarantine:
                c["quarantine"] = {"reason": "unit test: tray down", "since": "2026-10-13"}
    (work / "inventory.yaml").write_text(yaml.safe_dump(inv, sort_keys=False))
    if break_c2:                      # every command on c2 fails as if the tray were unreachable
        rules = yaml.safe_load((work / "replay" / "responses.yaml").read_text())
        rules.insert(0, {"cmd": ".*", "host": "^c2$", "rc": 255, "stdout": "", "stderr": "ssh: connect to host c2: No route"})
        (work / "replay" / "responses.yaml").write_text(yaml.safe_dump(rules, sort_keys=False, width=200))
    return work


def _run(work: Path, tmp_path: Path) -> dict[str, dict]:
    res = tmp_path / "results"
    subprocess.run([sys.executable, "-m", "pytest", "tests", "-m", "healthcheck", "--ignore", "tests/afm", "--inventory",
                    str(work / "inventory.yaml"), "--results-dir", str(res), "-q", "-p", "no:cacheprovider"],
                   cwd=ROOT, capture_output=True, text=True, timeout=600)
    run = next(res.glob("*/"))
    verdicts = {r["tc_id"]: r["verdict"] for r in csv.DictReader(open(run / "tc_summary.csv"))}
    return {"verdicts": verdicts, "run": run}


def test_inventory_split(tmp_path):
    inv = load_inventory(_copy_selftest(tmp_path, quarantine=True, break_c2=False) / "inventory.yaml")
    assert [h.name for h in inv.computes] == ["c1"] and [h.name for h in inv.quarantined] == ["c2"]
    assert [h.name for h in inv.all_computes] == ["c1", "c2"]
    assert inv.quarantined_slots() == {1}
    assert inv.is_quarantined_afm({"kind": "ComputeNode", "meta": {"name": "pod-1-1-ComputeTray"}, "spec": {"slot-id": 1}})
    assert not inv.is_quarantined_afm({"kind": "ComputeNode", "meta": {"name": "pod-1-0-ComputeTray"}, "spec": {"slot-id": 0}})
    q = inv.quarantine_summary()["c2"]
    assert q["reason"] == "unit test: tray down" and q["slot"] == 1 and q["expired"] is False


def test_broken_tray_fails_without_quarantine(tmp_path):
    v = _run(_copy_selftest(tmp_path, quarantine=False, break_c2=True), tmp_path)["verdicts"]
    assert v["HC-001"] == "FAIL" and v["HC-003"] == "FAIL"


def test_quarantined_tray_is_left_out(tmp_path):
    r = _run(_copy_selftest(tmp_path, quarantine=True, break_c2=True), tmp_path)
    v = r["verdicts"]
    assert not [t for t, x in v.items() if x in ("FAIL", "BLOCKED")], v
    assert v["HC-001"] == "PARTIAL" and v["HC-003"] == "PARTIAL"
    rows = list(csv.DictReader(open(r["run"] / "tc_results.csv")))
    hc1 = next(x for x in rows if x["tc_id"] == "HC-001")
    props = json.loads(hc1["properties"])
    assert props["quarantined"] == {"c2": "unit test: tray down"}
    assert props["snapshot"]["quarantined_reachable"] == {"c2": False}
    assert "NOT run on the quarantined node(s) c2 (unit test: tray down)" in hc1["message"]


def test_health_check_go_with_quarantine(tmp_path):
    work = _copy_selftest(tmp_path, quarantine=True, break_c2=True)
    p = subprocess.run([sys.executable, "tools/rack_health.py", "--inventory", str(work / "inventory.yaml"),
                        "--results-dir", str(tmp_path / "hr"), "--no-html", "--", "--ignore", "tests/afm"],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    # tests/afm left out here, so HC-000 / AFM blockers are 'not run' -> judged on the collected cases via --run
    run = next((tmp_path / "hr").glob("*/"))
    p = subprocess.run([sys.executable, "tools/rack_health.py", "--run", str(run), "--no-html"],
                       cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout[-2000:]
    assert "RACK HEALTH: GO (with quarantine: c2)" in p.stdout and "QUARANTINED c2" in p.stdout


def _afm(work: Path, tmp_path: Path, faults: str) -> dict[str, str]:
    import os
    res = tmp_path / "afmres"
    subprocess.run([sys.executable, "-m", "pytest", "tests/afm/test_afm_health.py", "tests/afm/test_afm_api.py",
                    "--tc", "HC-000,TC-CTL-090", "--inventory", str(work / "inventory.yaml"), "--results-dir", str(res),
                    "-q", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True, timeout=600,
                   env={**os.environ, "HELIOS_FAKE_AFM_FAULTS": faults})
    return {r["tc_id"]: r["verdict"] for r in csv.DictReader(open(next(res.glob("*/tc_summary.csv"))))}


def test_afm_side_unhealthy_tray(tmp_path):
    """AFM reports the last tray (slot 1 = c2) unhealthy: HC-000 / CTL-090 fail - unless c2 is quarantined."""
    v = _afm(_copy_selftest(tmp_path / "a", quarantine=False, break_c2=False), tmp_path / "a", "last_compute_unhealthy")
    assert v["HC-000"] == "FAIL" and v["TC-CTL-090"] == "FAIL", v
    v = _afm(_copy_selftest(tmp_path / "b", quarantine=True, break_c2=False), tmp_path / "b", "last_compute_unhealthy")
    assert v["HC-000"] == "PARTIAL" and v["TC-CTL-090"] == "PARTIAL", v
