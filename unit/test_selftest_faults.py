"""End-to-end self-test: run the L1 suite against replayed outputs, healthy and with injected faults.

Each fault must flip exactly the test-plan cases that should catch it — this is
what proves the rack tests detect problems rather than passing vacuously.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SELFTEST = ROOT / "selftest"


def _run(tmp_path: Path, mutate=None, extra_args=()) -> dict[str, str]:
    work = tmp_path / "selftest"
    shutil.copytree(SELFTEST, work)
    if mutate:
        mutate(work / "replay")
    results = tmp_path / "results"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests"), "--inventory", str(work / "inventory.yaml"),
         "--results-dir", str(results), "-q", "-p", "no:cacheprovider", *extra_args],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    summaries = list(results.glob("*/tc_summary.csv"))
    assert summaries, f"no results written\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
    with open(summaries[0]) as f:
        return {row["tc_id"]: row["verdict"] for row in csv.DictReader(f)}


def _edit(path: Path, pattern: str, repl: str):
    """Regex-substitute the first match (column spacing in replay files is generated)."""
    text, n = re.subn(pattern, repl, path.read_text(), count=1, flags=re.M)
    assert n == 1, f"{pattern!r} not in {path.name}"
    path.write_text(text)


def _edit_rule(replay: Path, cmd_fragment: str, **changes):
    spec_path = replay / "responses.yaml"
    rules = yaml.safe_load(spec_path.read_text())
    hit = [r for r in rules if cmd_fragment in r["cmd"]]
    assert hit, f"no rule matching {cmd_fragment!r}"
    for r in hit:
        r.update(changes)
    spec_path.write_text(yaml.safe_dump(rules, sort_keys=False, width=200))


def test_healthy_rack_passes(tmp_path):
    verdicts = _run(tmp_path)
    failed = {k: v for k, v in verdicts.items() if v in ("FAIL", "PARTIAL")}
    assert not failed, failed
    assert verdicts["TC-SWH-002"] == "PASS"
    assert verdicts["TC-AFM-004"] == "PASS"
    assert verdicts["TC-SWH-009"] == "SKIP"  # manual


FAULTS = {
    "slow_fan": (lambda d: _edit(d / "platform_fan.txt", r"(FanTray2_Fan1\s+)44%", r"\g<1> 4%"),
                 {"TC-SWH-002", "TC-SWH-003", "TC-SWH-006", "TC-SWH-008"}),
    "fan_missing": (lambda d: _edit(d / "platform_fan.txt", r"(FanTray1_Fan2.*)Present   OK    ", r"\1Absent    N/A   "),
                    {"TC-SWH-002", "TC-SWH-006", "TC-SWH-008", "TC-SWH-017"}),
    "psu_failed": (lambda d: _edit(d / "psustatus.txt", r"1110\.00  OK      green", "   0.00  NOT OK  red  "),
                   {"TC-SWH-010", "TC-SWH-019", "TC-SWH-027"}),
    "psu_led_wrong": (lambda d: _edit(d / "psustatus.txt", r"OK      green", "OK      amber"), {"TC-SWH-028"}),
    "port_down": (lambda d: _edit(d / "intf_status_asic1.txt", r"(Ethernet264.*trunk\s+)up  ", r"\1down"),
                  {"TC-SWH-030", "TC-SWH-031"}),
    "port_slow": (lambda d: _edit(d / "intf_status_asic0.txt", r"(Ethernet8\s+\S+\s+)400G", r"\g<1>200G"),
                  {"TC-SWH-032"}),
    "mtu_wrong": (lambda d: _edit(d / "intf_status_asic0.txt", r"(Ethernet16\s+\S+\s+400G  )9100", r"\g<1>1500"),
                  {"TC-SWH-033"}),
    "eeprom_bad_mac": (lambda d: _edit(d / "syseeprom.txt", "AA:BB:CC:00:11:22", "00:00:00:00:00:00"), {"TC-SWH-014"}),
    "no_fpga": (lambda d: _edit(d / "firmware.txt", r"(FPGA\s+)1\.4\.2", r"\1N/A  "), {"TC-SWH-001"}),
    # losing an ASIC namespace also loses its LLDP neighbours (expected_lldp_neighbors: 8)
    "asic1_missing": (lambda d: _edit_rule(d, "ip\\ netns\\ list", stdout="asic0 (id: 0)\n"),
                      {"TC-SWH-016", "TC-SWH-031"}),
    "vlan_no_members": (lambda d: _edit_rule(d, "-n\\ asic1\\ CONFIG_DB\\ keys\\ 'VLAN_MEMBER", stdout=""),
                        {"TC-SWH-036"}),
    "ping_loss": (lambda d: _edit_rule(d, "^ping-gpu ", stdout="4 packets transmitted, 0 received, 100% packet loss\n", rc=1),
                  {"TC-AFM-001"}),
    "tb_slow": (lambda d: _edit(d / "transferbench.txt", r"612\.48", "212.48"), {"TC-AFM-002", "TC-AFM-003"}),
    "rccl_wrong": (lambda d: _edit(d / "rccl.txt", r"^(\s+2048 .*?)0$", r"\g<1>7"), {"TC-AFM-004", "TC-AFM-005A"}),
    "rccl_truncated": (lambda d: _edit(d / "rccl.txt", r"^\s+1073741824 .*\n", ""),
                       {"TC-AFM-004", "TC-AFM-005A"}),
    "thermalctld_down": (lambda d: _edit_rule(d, "thermalctld", stdout="thermalctld   EXITED\n"), {"TC-SWH-004"}),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_fault_is_detected(tmp_path, fault):
    mutate, must_fail = FAULTS[fault]
    verdicts = _run(tmp_path, mutate)
    failed = {k for k, v in verdicts.items() if v in ("FAIL", "PARTIAL")}
    assert must_fail <= failed, f"{fault}: expected {sorted(must_fail)} to fail, failed={sorted(failed)}"
    unexpected = failed - must_fail
    assert not unexpected, f"{fault}: collateral failures {sorted(unexpected)}"
