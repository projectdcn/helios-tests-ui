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


def _rename_after_first_read(replay: Path):
    """ip -o link: first read eth0/eth1, second read (after reboot) eth0 renamed to eth2."""
    original = (replay / "ip_link.txt").read_text()
    (replay / "ip_link_after.txt").write_text(original.replace("2: eth0:", "2: eth2:"))
    _edit_rule(replay, "ip\\ \\-o\\ link", files=["ip_link.txt", "ip_link_after.txt"])


def _drops(replay: Path, port: str, live: bool):
    """Give `port` a large RX_DRP in every asic1 counter snapshot; with live=True it also grows per read."""
    for f in sorted(replay.glob("intf_counters_asic1_*.txt")):
        step = int(f.stem.rsplit("_", 1)[1])
        value = 5_000_000 + (step * 1_000_000 if live else 0)
        # RX_DRP is the 8th whitespace token on a data row ("12.30 MB/s" is two tokens). Columns are
        # right-aligned fixed width, so the new value must consume leading spaces, not shift later columns.
        text = f.read_text()
        m = re.search(rf"^(\s*{port}(?:\s+\S+){{6}})(\s+\d+)", text, flags=re.M)
        assert m, f"{port} row not found in {f.name}"
        cell = m.group(2)
        new = f"{value:,}".rjust(len(cell))
        assert len(new) == len(cell), f"value {value:,} too wide for column in {f.name}"
        f.write_text(text[:m.start(2)] + new + text[m.end(2):])


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
    failed = {k: v for k, v in verdicts.items() if v in ("FAIL", "PARTIAL", "BLOCKED")}
    assert not failed, failed
    assert verdicts["TC-SWH-002"] == "PASS"
    assert verdicts["TC-AFM-004"] == "PASS"
    assert verdicts["TC-SWH-009"] == "SKIP"  # manual


# every test behind the datapath pre-flight gate (helios/preflight.py) - a broken fabric must block all of them
GATED_TRAFFIC = {"SMOKE-RCCL-1N", "SMOKE-RCCL-MN", "TC-AFM-001", "TC-AFM-002", "TC-AFM-003", "TC-AFM-004",
                 "TC-AFM-005A", "TC-HW-008", "TC-HW-017", "TC-AFM-LU-001", "TC-RAS-reset-001", "TC-RAS-reset-002",
                 "TC-RAS-reset-003", "TC-RAS-reset-004", "TC-RAS-reset-007", "TC-RAS-reset-008", "TC-RAS-reset-009"}
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
    # all three FPGAs lose their version -> TC-SWH-001 (FPGA is required there, not in fwutil's list)
    "no_fpga": (lambda d: [_edit(d / "firmware.txt", r"(_FPGA\s+)0x000\d", r"\1N/A   ") for _ in range(3)], {"TC-SWH-001"}),
    "cpld_missing": (lambda d: [_edit(d / "firmware.txt", r"CPLD firmware", "controller fw ") for _ in range(2)],
                     {"TC-SWH-001", "TC-SWH-015"}),
    # losing an ASIC namespace also loses its LLDP neighbours (expected_lldp_neighbors: 8)
    "asic1_missing": (lambda d: _edit_rule(d, "ip\\ netns\\ list", stdout="asic0 (id: 0)\n"),
                      {"TC-SWH-016", "TC-SWH-031", "TC-HW-013", "TC-E2E-062"}),  # asic1 FDB/descriptions unreadable
    "vlan_no_members": (lambda d: _edit_rule(d, "-n\\ asic1\\ CONFIG_DB\\ keys\\ 'VLAN_MEMBER", stdout=""),
                        {"TC-SWH-036", "TC-SWA-079"}),  # also breaks member symmetry
    "ping_loss": (lambda d: _edit(d / "afmctl_ping.txt", r"IFoE Request    : 35/35 PASS, 0/35 fail",
                                  "IFoE Request    : 33/35 PASS, 2/35 fail"),
                  {"TC-AFM-001", "TC-HW-017", *GATED_TRAFFIC}),  # pre-flight ping probe blocks traffic
    # RAS cases and TC-HW-008 re-validate with the same TransferBench / RCCL workloads
    "tb_slow": (lambda d: _edit(d / "transferbench.txt", r"612\.48", "212.48"),
                {"TC-AFM-002", "TC-AFM-003", "TC-HW-008", "TC-HW-017", "TC-AFM-LU-001", *RAS_TRAFFIC}),
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
                           {"TC-BUG-021", *GATED_TRAFFIC}),  # same SSH probe; pre-flight sees nodes not responding
    # ---- metrics exporter
    "exporter_not_listening": (lambda d: _edit_rule(d, "sport\\ =\\ :5000", stdout=""), {"TC-CTL-139"}),
    "metrics_http_500": (lambda d: _edit(d / "metrics_c1.txt", r"^HTTP/1\.1 200 OK", "HTTP/1.1 500 Internal Server Error"),
                         {"TC-CTL-140", "TC-CTL-141", "TC-CTL-142", "TC-CTL-143", "TC-CTL-144", "TC-CTL-145",
                          "TC-CTL-146", "TC-CTL-147", "TC-CTL-148", "TC-CTL-149"}),
    "gpu3_no_power": (lambda d: _edit(d / "metrics_c1.txt", r'^gpu_power_usage\{gpu_id="3".*\n', ""), {"TC-CTL-141"}),
    "gpu_too_hot": (lambda d: _edit(d / "metrics_c2.txt", r'^(gpu_junction_temperature\{gpu_id="1"[^}]*\}) \S+', r"\1 125.0"),
                    {"TC-CTL-141"}),
    "vram_inconsistent": (lambda d: _edit(d / "metrics_c1.txt", r'^(gpu_free_vram\{gpu_id="0"[^}]*\}) \S+', r"\1 1000"),
                          {"TC-CTL-142"}),
    "ecc_uncorrectable": (lambda d: _edit(d / "metrics_c2.txt", r'^(gpu_ecc_uncorrect_total\{gpu_id="2"[^}]*\}) \S+', r"\1 3"),
                          {"TC-CTL-143"}),
    "no_xgmi": (lambda d: [_edit(d / "metrics_c1.txt", r"^xgmi_.*\n", "") for _ in range(8)], {"TC-CTL-145"}),
    "no_rdma_cnp": (lambda d: [_edit(d / "metrics_c2.txt", r"^rdma_cnp_sent.*\n", "") for _ in range(4)], {"TC-CTL-147"}),
    # gpu 3 reported as gpu 4 -> per-GPU coverage gaps + non-sequential ids
    "gpu_id_gap": (lambda d: [_edit(d / "metrics_c2.txt", r'gpu_id="3"', 'gpu_id="4"') for _ in range(17)],
                   {"TC-CTL-141", "TC-CTL-142", "TC-CTL-143", "TC-CTL-148"}),
    "wrong_hostname_label": (lambda d: _edit_rule(d, "^hostname", stdout="some-other-host\n"), {"TC-CTL-148"}),
    "exporter_log_errors": (lambda d: _edit(d / "exporter_logs.txt", r"level=info", "level=error"), {"TC-CTL-149"}),
    # ---- platform
    "ntp_unsynced": (lambda d: _edit_rule(d, "NTPSynchronized", stdout="no\n"), {"TC-CTL-035"}),
    "clock_skew": (lambda d: _edit_rule(d, "date\\ \\+%s\\.%N", stdout="1000000000.0\n"), {"TC-CTL-035"}),
    "dns_fails": (lambda d: _edit_rule(d, "^getent hosts ", stdout="", rc=2), {"TC-CTL-059"}),
    "logrotate_unscheduled": (lambda d: _edit_rule(d, "logrotate\\.timer", stdout="inactive\n"), {"TC-BUG-022"}),
    "var_log_full": (lambda d: _edit_rule(d, "df\\ \\-\\-output", stdout=" 96%\n"), {"TC-BUG-022"}),
    "topo_logs_stale": (lambda d: _edit(d / "topo_find.txt", r"^1760003600$", "1760900000"), {"TC-E2E-118"}),
    "oob_renamed_after_reboot": (lambda d: _rename_after_first_read(d), {"TC-BUG-021"}),
    # ---- IFoE driver
    "ifoe_unload_stuck": (lambda d: _edit_rule(d, "lsmod", rcs=[0]), {"TC-AFM-LU-001", "TC-HW-017"}),
    "gpu_lost_after_reload": (lambda d: _edit(d / "amd_smi_list.txt", r"GPU: 3\n(.*\n){2}", ""),
                              {"TC-AFM-LU-001", "TC-HW-017"}),
    # ---- RCCL smoke / TransferBench multi-rank
    "smoke_collective_fails": (lambda d: _edit(d / "rccl_smoke.txt", r"(AllToAll\s+\|\s+P\s+\|\s+P\s+\|\s+)P", r"\1F"),
                               {"SMOKE-RCCL-1N", "SMOKE-RCCL-MN"}),
    "smoke_oom_skip": (lambda d: _edit(d / "rccl_smoke.txt", r"(Broadcast\s+\|\s+P\s+\|\s+P\s+\|\s+)P", r"\1S"),
                       {"SMOKE-RCCL-1N", "SMOKE-RCCL-MN"}),
    "tb_worker_rank_fails": (lambda d: _edit_rule(d, "TB_RANK=[1-9]", rc=1, stderr="connect to master failed"),
                             {"TC-AFM-003", "TC-RAS-reset-003", "TC-RAS-reset-004", "TC-RAS-reset-007",
                              "TC-RAS-reset-008", "TC-RAS-reset-009"}),
    "gpu_not_active": (lambda d: _edit(d / "afmctl_device_c2.json", r'"config_phase": "ACTIVE"', '"config_phase": "PROVIDER"'),
                       {"TC-BU-011", *GATED_TRAFFIC}),  # pre-flight blocks all traffic tests
    "ping_no_summary": (lambda d: _edit(d / "afmctl_ping.txt", r"Summary:(.|\n)*", "ERR - Failed to start ping test\n"),
                        {"TC-AFM-001", "TC-HW-017", *GATED_TRAFFIC}),
    # MR3 failure mode: one plane silently drops a port's ingress while links/LLDP/VLAN look healthy
    "port_dropping_lifetime": (lambda d: _drops(d, "Ethernet264", live=False), {"TC-SWA-074"}),
    "port_still_dropping": (lambda d: _drops(d, "Ethernet272", live=True), {"TC-SWA-074", *GATED_TRAFFIC}),
    # ---- sideband bring-up / destination MACs
    "sb_slot_missing": (lambda d: _edit(d / "afm_agent_ps.txt", r"^/sb-node-agent -slot-id 0 .*\n", ""), {"TC-BU-009"}),
    "agent_on_compute": (lambda d: _edit(d / "compute_ps.txt", r"\Z", "inb-node-agent  /usr/bin/inb-node-agent\n"),
                         {"TC-BU-006"}),
    "afm_agent_inactive": (lambda d: _edit_rule(d, "systemctl\\ is\\-active\\ afm\\-agent", stdout="inactive\n"),
                           {"TC-BU-002"}),
    "gpu_port_no_link": (lambda d: _edit(d / "afmctl_port_c2_0003.json",
                                         r'("id": "12", "spec": \{"admin_state": "enabled", "station_id": "6"\}, "status": \{"link_status": ")LINK_UP',
                                         r"\1NONE"), {"TC-BU-011"}),
    "dst_accel_missing": (lambda d: _edit(d / "afmctl_accel_addr_c2.json",
                                          r'\{\s*"accelerator_id": "3",\s*"mac_addr": "[^"]+",\s*"ip_addr": "[^"]+"\s*\},?', ""),
                          {"TC-HW-013"}),
    "fdb_mac_on_wrong_port": (lambda d: _edit(d / "fdb_asic0.txt", r"Ethernet1000 ", "Ethernet1001 "), {"TC-HW-013"}),
    "agent_disconnected": (lambda d: _edit(d / "agent_health_13001.json", r'"is-connected": true', '"is-connected": false'),
                           {"TC-SEQ-006"}),
    "agent_config_not_applied": (lambda d: _edit(d / "agent_requests_13000.json", r'"state": 1, "reason": ""',
                                                 '"state": 2, "reason": "apply failed"'), {"TC-SEQ-006"}),
    # ---- IFCP
    "ifcp_session_down": (lambda d: _edit(d / "afmctl_port_c1_0002.json",
                                          r'("id": "5", .*?"ifcp": \{"port_id": "5", "link_up": ")yes', r"\1no"), {"TC-BU-003"}),
    "ifcp_ll_one_way": (lambda d: _edit(d / "afmctl_port_c2_0001.json",
                                        r'("id": "20", .*?"ll_up": ")yes', r"\1no"), {"TC-SBM-062"}),
    "cabling_mismatch": (lambda d: _edit(d / "afmctl_port_c1_0001.json",
                                         r'("id": "3", .*?"peer_port_id": ")1003"', r'\g<1>1004"'), {"TC-E2E-062"}),
    "thermalctld_down": (lambda d: _edit_rule(d, "thermalctld", stdout="thermalctld   EXITED\n"), {"TC-SWH-004"}),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_fault_is_detected(tmp_path, fault):
    mutate, must_fail = FAULTS[fault]
    verdicts = _run(tmp_path, mutate)
    failed = {k for k, v in verdicts.items() if v in ("FAIL", "PARTIAL", "BLOCKED")}
    assert must_fail <= failed, f"{fault}: expected {sorted(must_fail)} to fail, failed={sorted(failed)}"
    unexpected = failed - must_fail
    assert not unexpected, f"{fault}: collateral failures {sorted(unexpected)}"
