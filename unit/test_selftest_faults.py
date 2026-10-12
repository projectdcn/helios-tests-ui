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


def _run(tmp_path: Path, mutate=None, extra_args=("--allow-disruptive",)) -> dict[str, str]:
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


def _edit_all(replay: Path, glob: str, pattern: str, repl: str):
    hits = sorted(replay.glob(glob))
    assert hits, f"no files match {glob}"
    for f in hits:
        _edit(f, pattern, repl)


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


RAS_TRAFFIC = {"TC-RAS-reset-001", "TC-RAS-reset-002", "TC-RAS-reset-003", "TC-RAS-reset-004", "TC-RAS-reset-007", "TC-RAS-reset-008", "TC-RAS-reset-009"}

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
                        {"TC-SWH-036", "TC-SWA-079"}),  # also breaks member symmetry
    "ping_loss": (lambda d: _edit_rule(d, "^ping-gpu ", stdout="4 packets transmitted, 0 received, 100% packet loss\n", rc=1),
                  {"TC-AFM-001"}),
    # RAS cases and TC-HW-008 re-validate with the same TransferBench / RCCL workloads
    "tb_slow": (lambda d: _edit(d / "transferbench.txt", r"612\.48", "212.48"),
                {"TC-AFM-002", "TC-AFM-003", "TC-HW-008", *RAS_TRAFFIC}),
    "rccl_wrong": (lambda d: _edit(d / "rccl.txt", r"^(\s+2048 .*?)0$", r"\g<1>7"), {"TC-AFM-004", "TC-AFM-005A", *RAS_TRAFFIC}),
    "rccl_truncated": (lambda d: _edit(d / "rccl.txt", r"^\s+1073741824 .*\n", ""),
                       {"TC-AFM-004", "TC-AFM-005A", *RAS_TRAFFIC}),
    # ---- switch agent / hw
    "traffic_stalled": (lambda d: _edit_rule(d, "counters\\ \\-n\\ asic0", files=["intf_counters_asic0_0.txt"]),
                        {"TC-SWA-075"}),
    "lifetime_rx_errors": (lambda d: _edit_all(d, "intf_counters_asic1_*.txt", r"(Ethernet264(?:\s+\S+){5}\s+)0(?=\s)", r"\g<1>5"),
                           {"TC-SWA-074"}),
    # asic1 loses Vlan101, but its Vlan101 members remain -> asymmetric + orphan members
    "vlan_asymmetry": (lambda d: _edit_rule(d, "-n\\ asic1\\ CONFIG_DB\\ keys\\ 'VLAN\\|", stdout="VLAN|Vlan100\n"),
                       {"TC-SWA-079", "TC-SWH-036"}),
    "installer_missing_cmd": (lambda d: _edit_rule(d, "sonic\\-installer\\ \\-\\-help",
                                                   stdout="Commands:\n  install\n  list\n  remove\n"), {"TC-SWA-138"}),
    "set_next_boot_fails": (lambda d: _edit_rule(d, "set-next-boot ", rc=1, stderr="Error: image not found"),
                            {"TC-SWA-129"}),
    "lldpd_down": (lambda d: _edit(d / "supervisor_lldp0.txt", r"^lldpd(\s+)RUNNING", r"lldpd\1EXITED "),
                   {"TC-SWA-223", "TC-SWA-140"}),
    "syncd_not_recovering": (lambda d: _edit(d / "supervisor_syncd0.txt", r"^syncd(\s+)RUNNING", r"syncd\1FATAL  "),
                             {"TC-SWA-141"}),
    "port_speed_mismatch": (lambda d: _edit_rule(d, "sonic-db-cli -n asic0 CONFIG_DB keys 'PORT",
                                                 stdout="Ethernet0 400000 400000\nEthernet8 400000 200000\n"),
                            {"TC-HW-002"}),
    # ---- scale-out RDMA
    "rdma_cqe_error": (lambda d: _edit(d / "perftest.txt", r"^ #bytes", " Completion with error at client\n #bytes"),
                       {"TC-IBS-001", "TC-IBS-002", "TC-IBS-003", "TC-IBS-004", "TC-IBS-005", "TC-IBS-006"}),
    "rdma_low_bw": (lambda d: _edit(d / "perftest.txt", r"390\.12", "100.12"),
                    {"TC-IBS-001", "TC-IBS-002", "TC-IBS-003", "TC-IBS-004", "TC-IBS-005", "TC-IBS-006"}),
    # ---- RAS
    "datapath_reset_fails": (lambda d: _edit_rule(d, "afmctl reset", rc=1, stderr="reset failed"),
                             {"TC-RAS-reset-001", "TC-RAS-reset-002", "TC-RAS-reset-003", "TC-RAS-reset-004",
                              "TC-RAS-reset-007", "TC-RAS-reset-008", "TC-RAS-reset-009"}),
    "node_never_returns": (lambda d: _edit_rule(d, "^true$", rc=255),
                           {"TC-RAS-reset-007", "TC-RAS-reset-008", "TC-RAS-reset-009"}),
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
