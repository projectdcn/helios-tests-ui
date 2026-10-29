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
        # tests/afm has its own fake-controller harness (unit/test_afm_selftest.py)
        [sys.executable, "-m", "pytest", str(ROOT / "tests"), "--ignore", str(ROOT / "tests" / "afm"),
         "--inventory", str(work / "inventory.yaml"),
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


def _flap(replay: Path):
    """asic0 Ethernet8 is down on every other read of 'show interfaces status -n asic0'."""
    text = (replay / "intf_status_asic0.txt").read_text()
    down, n = re.subn(r"(Ethernet8\s+.*?trunk\s+)up  ", r"\1down", text, count=1)
    assert n == 1
    (replay / "intf_status_asic0_down.txt").write_text(down)
    _edit_rule(replay, "status\\ \\-n\\ asic0", files=["intf_status_asic0.txt", "intf_status_asic0_down.txt"], file=None)


def _pfc_storm(replay: Path):
    """Ethernet0 receives 5000 more PFC3 pause frames between the two reads."""
    text = (replay / "pfc_asic0.txt").read_text()
    stormy, n = re.subn(r"(Ethernet0(?:\s+0){3}\s+)0", r"\g<1>5000", text, count=1)
    assert n == 1
    (replay / "pfc_asic0_b.txt").write_text(stormy)
    _edit_rule(replay, "pfc\\ counters\\ \\-n\\ asic0", files=["pfc_asic0.txt", "pfc_asic0_b.txt"], file=None)


def _cycle(replay: Path, fname: str, pattern: str, repl: str, rule_fragment: str):
    """Make the second read of a command differ: a copy of ``fname`` with ``pattern`` replaced, served on every other
    call of the rule that matches ``rule_fragment``."""
    text = (replay / fname).read_text()
    changed, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    assert n == 1, f"{pattern!r} not in {fname}"
    alt = fname.replace(".txt", "_b.txt")
    (replay / alt).write_text(changed)
    _edit_rule(replay, rule_fragment, files=[fname, alt], file=None)


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
                 "TC-RAS-reset-003", "TC-RAS-reset-004", "TC-RAS-reset-007", "TC-RAS-reset-008", "TC-RAS-reset-009",
                 "TC-SWH-037T"}
IBS_ALL = {"TC-IBS-001", "TC-IBS-002", "TC-IBS-003", "TC-IBS-004", "TC-IBS-005", "TC-IBS-006"}
RAS_TRAFFIC = {"TC-RAS-reset-001", "TC-RAS-reset-002", "TC-RAS-reset-003", "TC-RAS-reset-004", "TC-RAS-reset-007", "TC-RAS-reset-008", "TC-RAS-reset-009"}

FAULTS = {
    "slow_fan": (lambda d: _edit(d / "platform_fan.txt", r"(FanTray2_Fan1\s+)44%", r"\g<1> 4%"),
                 {"TC-SWH-002", "TC-SWH-003", "TC-SWH-006", "TC-SWH-008"}),
    "fan_missing": (lambda d: _edit(d / "platform_fan.txt", r"(FanTray1_Fan2.*)Present   OK    ", r"\1Absent    N/A   "),
                    {"TC-SWH-002", "TC-SWH-006", "TC-SWH-008", "TC-SWH-017",
                     "TC-SWH-047T", "TC-SWH-048T", "TC-SWH-049T", "TC-SWH-050T"}),
    "psu_failed": (lambda d: _edit(d / "psustatus.txt", r"1110\.00  OK      green", "   0.00  NOT OK  red  "),
                   {"TC-SWH-010", "TC-SWH-019", "TC-SWH-027"}),
    "psu_led_wrong": (lambda d: _edit(d / "psustatus.txt", r"OK      green", "OK      amber"), {"TC-SWH-028"}),
    "port_down": (lambda d: _edit(d / "intf_status_asic1.txt", r"(Ethernet264.*trunk\s+)up  ", r"\1down"),
                  {"TC-SWH-030", "TC-SWH-031", "TC-SWH-091"}),
    "port_slow": (lambda d: _edit(d / "intf_status_asic0.txt", r"(Ethernet8\s+\S+\s+)400G", r"\g<1>200G"),
                  {"TC-SWH-032", "TC-SWH-091"}),
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
                        {"TC-SWH-036", "TC-SWA-079", "TC-SWH-037", "TC-SWH-075"}),  # also member symmetry; cabled ports in no VLAN
    "ping_loss": (lambda d: _edit(d / "afmctl_ping.txt", r"IFoE Request    : 35/35 PASS, 0/35 fail",
                                  "IFoE Request    : 33/35 PASS, 2/35 fail"),
                  {"TC-AFM-001", "TC-HW-017", *GATED_TRAFFIC}),  # pre-flight ping probe blocks traffic
    # RAS cases and TC-HW-008 re-validate with the same TransferBench / RCCL workloads
    "tb_slow": (lambda d: _edit(d / "transferbench.txt", r"612\.48", "212.48"),
                {"TC-AFM-002", "TC-AFM-003", "TC-HW-008", "TC-HW-017", "TC-AFM-LU-001", "TC-SWH-037T", *RAS_TRAFFIC}),
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
                                                   stdout="Commands:\n  install\n  list\n  remove\n"),
                              {"TC-SWA-138", "TC-SWH-069"}),
    "set_next_boot_fails": (lambda d: _edit_rule(d, "set-next-boot ", rc=1, stderr="Error: image not found"),
                            {"TC-SWA-129"}),
    "lldpd_down": (lambda d: _edit(d / "supervisor_lldp0.txt", r"^lldpd(\s+)RUNNING", r"lldpd\1EXITED "),
                   {"TC-SWA-223", "TC-SWA-140", "TC-SWH-057", "TC-SWH-105"}),   # 057: lldpd is a critical process
    "syncd_not_recovering": (lambda d: _edit(d / "supervisor_syncd0.txt", r"^syncd(\s+)RUNNING", r"syncd\1FATAL  "),
                             {"TC-SWA-141", "TC-SWH-057", "TC-SWH-060T",
                              "TC-SWH-094", "TC-SWH-095", "TC-SWH-104", "TC-SWH-106"}),   # 057/060T: syncd is a critical process; recovery waits fail
    "port_speed_mismatch": (lambda d: _edit_rule(d, "sonic-db-cli -n asic0 CONFIG_DB keys 'PORT",
                                                 stdout="Ethernet0 400000 400000\nEthernet8 400000 200000\n"),
                            {"TC-HW-002", "TC-SWH-058T"}),
    # ---- scale-out RDMA
    "rdma_cqe_error": (lambda d: _edit(d / "perftest.txt", r"^ #bytes", " Completion with error at client\n #bytes"),
                       {"TC-IBS-001", "TC-IBS-002", "TC-IBS-003", "TC-IBS-004", "TC-IBS-005", "TC-IBS-006"}),
    "rdma_low_bw": (lambda d: _edit(d / "perftest.txt", r"390\.12", "100.12"),
                    {"TC-IBS-001", "TC-IBS-002", "TC-IBS-003", "TC-IBS-004", "TC-IBS-005", "TC-IBS-006"}),
    # ---- RAS
    "datapath_reset_fails": (lambda d: _edit_rule(d, "afmctl reset", rc=1, stderr="reset failed"),
                             {"TC-RAS-reset-001", "TC-RAS-reset-002", "TC-RAS-reset-003", "TC-RAS-reset-004",
                              "TC-RAS-reset-007", "TC-RAS-reset-008", "TC-RAS-reset-009",
                              "HC-013"}),   # HC-013 runs whenever selected (not opt-in) and resets every tray
    "node_never_returns": (lambda d: _edit_rule(d, "^true$", rc=255),
                           {"TC-BUG-021", "HC-001", *GATED_TRAFFIC}),  # same SSH probe; pre-flight + health check see nodes down
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
                              "TC-RAS-reset-008", "TC-RAS-reset-009", "TC-SWH-037T"}),
    # one bad tray: the pre-flight excludes c2 - single-tray cases run on c1 as PARTIAL, two-tray cases are BLOCKED
    "gpu_not_active": (lambda d: _edit(d / "afmctl_device_c2.json", r'"config_phase": "ACTIVE"', '"config_phase": "PROVIDER"'),
                       {"TC-BU-011", "HC-003", "HC-013", *GATED_TRAFFIC}),   # HC-013 waits for every GPU ACTIVE
    "ping_no_summary": (lambda d: _edit(d / "afmctl_ping.txt", r"Summary:(.|\n)*", "ERR - Failed to start ping test\n"),
                        {"TC-AFM-001", "TC-HW-017", *GATED_TRAFFIC}),
    # MR3 failure mode: one plane silently drops a port's ingress while links/LLDP/VLAN look healthy
    "port_dropping_lifetime": (lambda d: _drops(d, "Ethernet264", live=False),
                               {"TC-SWA-074", "TC-SWH-054T", "TC-SWH-055T"}),
    "port_still_dropping": (lambda d: _drops(d, "Ethernet272", live=True),
                            {"TC-SWA-074", "TC-SWH-054T", "TC-SWH-055T", *GATED_TRAFFIC}),
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
    "rdma_nic_link_down": (lambda d: _edit(d / "rdma_link.txt", r"ionic_2/1 state ACTIVE physical_state LINK_UP",
                                           "ionic_2/1 state DOWN physical_state DISABLED"), IBS_ALL),
    "perftest_without_rocm": (lambda d: _edit(d / "perftest_help.txt", r".*use_rocm.*\n", ""), IBS_ALL),
    # the tools a datapath test declares are missing on every tray (g02-g17: TransferBench not installed on 7 trays):
    # the pre-flight excludes all trays for exactly those tests -> BLOCKED; tests without declared tools still run
    "tools_missing": (lambda d: _edit_rule(d, "^command -v", rc=1),
                      {"TC-AFM-002", "TC-AFM-003", "TC-AFM-004", "TC-AFM-005A", "SMOKE-RCCL-1N", "SMOKE-RCCL-MN",
                       "TC-SWH-037T"}),
    # ---- added switch cases (TC-SWH-037..042)
    "gpu_port_routed": (lambda d: _edit(d / "intf_status_asic0.txt", r"(Ethernet8\s+.*?)trunk ", r"\1routed"),
                        {"TC-SWH-037", "TC-SWH-091"}),
    "optics_low_rx": (lambda d: _edit(d / "xcvr_dom.txt", r"RX1Power: -1\.20dBm", "RX1Power: -14.20dBm"), {"TC-SWH-039"}),
    "fec_ber_high": (lambda d: _edit(d / "fec_asic0.txt", r"1\.2e-08", "3.4e-04"), {"TC-SWH-040"}),
    "container_restarted": (lambda d: _edit(d / "docker_restarts.txt", r"^/syncd0 0 ", "/syncd0 3 "), {"TC-SWH-041"}),
    # replay 'now' (date +%s) is 2026-10-14 17:30 UTC: a restart 5.5 h earlier fails, one 35 h earlier is only recorded
    "container_restarted_recently": (lambda d: _edit(d / "docker_restarts.txt", r"^(/swss0 0 )\S+", r"\g<1>2026-10-14T12:00:00.000000000Z"),
                                     {"TC-SWH-041"}),
    "container_restarted_long_ago": (lambda d: _edit(d / "docker_restarts.txt", r"^(/swss0 0 )\S+", r"\g<1>2026-10-13T06:06:11.000000000Z"),
                                     set()),
    "container_missing": (lambda d: _edit_rule(d, "docker\\ ps\\ \\-\\-format\\ '\\{\\{\\.Names",
                                               stdout="database\npmon\nlldp\nlldp0\nlldp1\nswss0\nswss1\nsyncd1\n"
                                                      "afm-agent\ndatabase0\ndatabase1\n"),
                          {"TC-SWH-041", "TC-SWH-016", "TC-SWA-141", "TC-SWH-094", "TC-SWH-095", "TC-SWH-104",
                           "TC-SWH-106"}),   # 016/SWA-141 read the same container list; recovery waits fail
    "kernel_panic": (lambda d: _edit_rule(d, "^show\\ reboot\\-cause$", stdout="Kernel Panic - Out of memory [Time: Tue Oct 13]\n"),
                     {"TC-SWH-042"}),
    "core_file": (lambda d: _edit_rule(d, "^ls\\ \\-1\\ /var/core$", stdout="syncd.1760313600.4242.core.gz\n"),
                  {"TC-SWH-042", "TC-SWH-044T"}),
    # ---- added switch cases, second batch (TC-SWH-043..047)
    "kernel_aer": (lambda d: _edit_rule(d, "^grep -hE", stdout="2026 Oct 14 16:20:01.000001 sw1 WARNING kernel: "
                                       "[1201.5] pcieport 0000:00:01.1: AER: Corrected error received: 0000:01:00.0\n", rc=0),
                   {"TC-SWH-043"}),
    "syslog_orchagent_err": (lambda d: _edit_rule(d, "^grep -hE", stdout="2026 Oct 14 16:21:07.100000 sw1 ERR "
                                                  "swss0#orchagent: :- doTask: Failed to set port attribute\n", rc=0),
                             {"TC-SWH-044"}),
    "link_flapping": (lambda d: _flap(d), {"TC-SWH-045", "TC-SWH-030"}),   # 030 also reads the down sample
    "pfc_storm": (lambda d: _pfc_storm(d), {"TC-SWH-046"}),
    "config_unsaved": (lambda d: _edit(d / "cfg_saved_asic0.json", r'"Vlan100\|Ethernet0"', '"Vlan100|Ethernet999"'),
                       {"TC-SWH-047"}),
    # ---- added switch cases, third batch (TC-SWH-049, 051..058; 059 needs 2 switches: unit test of _majority)
    "port_not_trained": (lambda d: _edit(d / "port_states_asic0.txt", r"'link_training_status': 'trained'",
                                         "'link_training_status': 'not_trained'"), {"TC-SWH-049"}),
    "vlan_learning_on": (lambda d: _edit(d / "vlan_asic0_Vlan100.txt", r"'learn_disable': 'true'", "'learn_disable': 'false'"),
                         {"TC-SWH-051"}),
    "queue_drops": (lambda d: _cycle(d, "queue_asic0.txt", r"(Ethernet0\s+UC0(?:\s+\S+){2}\s+)0", r"\g<1>500",
                                     "queue\\ counters\\ \\-n\\ asic0"), {"TC-SWH-052"}),
    "crm_full": (lambda d: _edit(d / "crm.txt", r"fdb_entry(\s+)204(\s+)7987", r"fdb_entry\g<1>7900\g<2> 291"), {"TC-SWH-053"}),
    "mgmt_errors": (lambda d: _cycle(d, "eth0.txt", r"(1630377338 12608432)      0", r"\1     42",
                                     "ip\\ \\-s\\ link"), {"TC-SWH-054"}),
    "voltage_warning": (lambda d: _edit(d / "voltage.txt", r"False", "True "), {"TC-SWH-055"}),
    "disk_full": (lambda d: _edit(d / "df.txt", r"2% /var/log", "95% /var/log"), {"TC-SWH-056", "TC-SWH-061T", "TC-SWH-082"}),
    "critical_process_exited": (lambda d: _edit(d / "supervisor_swss0.txt", r"^(orchagent\s+)RUNNING", r"\1EXITED "),
                                {"TC-SWH-057", "TC-SWA-141", "TC-SWH-060T",
                                 "TC-SWH-094", "TC-SWH-095", "TC-SWH-104", "TC-SWH-106"}),   # SWA-141 also checks swss processes; recovery waits fail
    "watchdog_unarmed": (lambda d: _edit(d / "watchdog.txt", r"Status: Armed", "Status: Unarmed"), {"TC-SWH-058"}),
    # full-rack health check (tests/health)
    "gpu_uncorrectable_ecc": (lambda d: _edit(d / "amd_smi_ecc.json", r'"total_uncorrectable_count": 0', '"total_uncorrectable_count": 2'),
                              {"HC-005"}),
    "tray_kernel_hw_error": (lambda d: _edit(d / "compute_kernel_err.txt", r"\A",
                                             "Oct 14 23:05:23 c1 kernel: [Hardware Error]: Uncorrected, software restartable error.\n"
                                             "Oct 14 23:05:23 c1 kernel: [Hardware Error]: CPU:5 MC26_STATUS[Over|UE|MiscV|PCC]\n"),
                             {"HC-005"}),
    "tray_corrected_mce_only": (lambda d: _edit(d / "compute_kernel_err.txt", r"\A",      # counted, not failed
                                                "Oct 14 23:05:23 c1 kernel: [Hardware Error]: Corrected error, no action required.\n"
                                                "Oct 14 23:05:23 c1 kernel: [Hardware Error]: CPU:5 MC26_STATUS[-|CE|MiscV]\n"
                                                "Oct 14 23:05:23 c1 kernel: [Hardware Error]: FRU Text: PPlErr: G4G0\n"),
                                set()),
    "tray_disk_full": (lambda d: _edit(d / "compute_df.txt", r" 3% /", "97% /"), {"HC-006"}),
    "gpu_firmware_mixed": (lambda d: _edit(d / "amd_smi_firmware.json", r'"fw_version": "2430"', '"fw_version": "2431"'),
                           {"HC-010"}),
    "tray_driver_version_missing": (lambda d: _edit_rule(d, "amdgpu/version", rc=1), {"HC-010"}),
    "thermalctld_down": (lambda d: _edit_rule(d, "thermalctld", stdout="thermalctld   EXITED\n"), {"TC-SWH-004", "TC-SWH-099"}),
    # ---- tracking-sheet switch cases (tests/switch/test_switch_platform.py)
    "temp_critical": (lambda d: _edit(d / "temperature.txt", r"(ASIC1\s+)54\.0", r"\g<1>116.0"),
                      {"TC-SWH-004", "TC-SWH-038T", "TC-SWH-042T", "TC-SWH-055"}),
    "health_summary_bad": (lambda d: _edit_rule(d, "system\\-health\\ summary",
                                                stdout="System status summary\n  Services:\n    Status: Not OK\n"
                                                       "  Hardware:\n    Status: OK\n"), {"TC-SWH-039T", "TC-SWH-041"}),
    "health_component_not_ok": (lambda d: _edit(d / "system_health_detail.txt", r"(routeCheck\s+)OK    ", r"\1Not OK"),
                                {"TC-SWH-040T"}),
    "health_monitor_inactive": (lambda d: _edit_rule(d, "is\\-active\\ system\\-health", stdout="inactive\n"),
                                {"TC-SWH-041T"}),
    "watchdog_no_disarm": (lambda d: _edit_rule(d, "watchdogutil\\ \\-\\-help",
                                                stdout="Commands:\n  arm  Arm HW watchdog\n  status  Check\n"),
                           {"TC-SWH-043T"}),
    "systemd_unit_failed": (lambda d: _edit_rule(d, "list\\-units\\ \\-\\-failed",
                                                 stdout="snmp.service loaded failed failed SNMP agent\n"), {"TC-SWH-045T"}),
    "container_exited": (lambda d: _edit(d / "docker_ps_status.txt", r"^bgp0\|Up 3 days", "bgp0|Exited (1) 2 hours ago"),
                         {"TC-SWH-046T"}),
    "fan_led_amber": (lambda d: _edit(d / "platform_fan.txt", r"green(\s+FanTray2_Fan2)", r"amber\1"), {"TC-SWH-047T"}),
    "fan_airflow_mixed": (lambda d: _edit(d / "platform_fan.txt", r"(FanTray2_Fan1\s+44%\s+)intake ", r"\1exhaust"),
                          {"TC-SWH-051T"}),
    "fan_rpm_zero": (lambda d: _edit_rule(d, "hgetall\\ 'FAN_INFO\\|FanTray1_Fan2'", stdout=repr(
        {"presence": "True", "status": "True", "speed": "0", "speed_target": "41", "drawer_name": "FanTray1"}) + "\n"),
                     {"TC-SWH-006", "TC-SWH-052T", "TC-SWH-099"}),
    "fan_off_target": (lambda d: _edit_rule(d, "hgetall\\ 'FAN_INFO\\|FanTray2_Fan1'", stdout=repr(
        {"presence": "True", "status": "True", "speed": "44", "speed_target": "90", "drawer_name": "FanTray2"}) + "\n"),
                       {"TC-SWH-099"}),
    "psu_inefficient": (lambda d: _edit_rule(d, "hgetall\\ 'PSU_INFO\\|PSU\\ 2'", stdout=repr(
        {"presence": "true", "status": "true", "model": "PWR-3000-AC", "serial": "PSU2SN0002", "revision": "A1",
         "voltage": "12.04", "current": "88.40", "power": "1064.00", "temp": "38.5", "temp_threshold": "65.0",
         "voltage_min_threshold": "11.4", "voltage_max_threshold": "12.6", "power_overload": "False",
         "led_status": "green", "input_voltage": "230.0", "input_current": "9.80"}) + "\n"), {"TC-SWH-053T"}),
    "error_counters_missing_port": (lambda d: _edit(d / "intf_errors_asic1.txt", r"^Ethernet264 .*\n", ""),
                                    {"TC-SWH-056T"}),
    "xcvr_absent": (lambda d: _edit(d / "xcvr_presence_asic0.txt", r"(Ethernet8\s+)Present", r"\1Not present"),
                    {"TC-SWH-039", "TC-SWH-057T"}),
    "service_failed": (lambda d: _edit(d / "services_active.txt", r"^syncd@1 active", "syncd@1 failed"), {"TC-SWH-059T"}),
    "cpu_busy": (lambda d: _edit(d / "vmstat.txt", r" 6  3 91  0  0\n\Z", "90  7  3  0  0\n"), {"TC-SWH-061T"}),
    "load_high": (lambda d: _edit(d / "meminfo.txt", r"^1\.63 ", "41.63 "), {"TC-SWH-056", "TC-SWH-062T"}),
    "unexpected_recent_reboot": (lambda d: [_edit(d / "uptime.txt", r"^259200\.12", "120.40"),
                                            _edit_rule(d, "^show\\ reboot\\-cause$", stdout="Kernel Panic - Oops\n")],
                                 {"TC-SWH-042", "TC-SWH-063"}),
    "no_nameserver": (lambda d: _edit(d / "resolv.txt", r"^nameserver .*\n", ""), {"TC-SWH-064", "TC-CTL-059"}),
    "running_version_differs": (lambda d: _edit_rule(d, "^show\\ version$",
                                                     stdout="SONiC Software Version: SONiC.202411.3\n"),
                                {"TC-SWH-067"}),
    "next_boot_differs": (lambda d: _edit(d / "sonic_installer_list.txt", r"^Next: .*$", "Next: SONiC-OS-helios.202411.3"),
                          {"TC-SWH-068", "TC-SWH-102", "TC-SWA-129"}),
    "asic_port_table_missing": (lambda d: [_edit(d / f, r'"PORT": \{', '"PORT_X": {')
                                           for f in ("cfg_running_asic1.json", "cfg_saved_asic1.json")],
                                {"TC-SWH-070", "TC-SWH-071", "TC-SWH-073"}),
    "interface_table_missing": (lambda d: [_edit(d / f, r'"INTERFACE": \{', '"INTERFACE_X": {')
                                           for f in ("cfg_running_asic0.json", "cfg_saved_asic0.json")],
                                {"TC-SWH-072"}),
    "vlan_id_differs": (lambda d: [_edit(d / f, r'"vlanid": "101"', '"vlanid": "102"')
                                   for f in ("cfg_running_asic1.json", "cfg_saved_asic1.json")], {"TC-SWH-074"}),
    "port_no_admin_status": (lambda d: [_edit(d / f, r'"admin_status": "up",\s*', "")
                                        for f in ("cfg_running_asic0.json", "cfg_saved_asic0.json")], {"TC-SWH-073"}),
    "only_default_route": (lambda d: _edit(d / "ip_route.txt", r"^10\.0\.0\.0/24 .*\n", ""), {"TC-SWH-077"}),
    "gateway_ping_loss": (lambda d: _edit(d / "ping_gw.txt", r"5 received, 0% packet loss", "3 received, 40% packet loss"),
                          {"TC-SWH-078"}),
    "fru_mac_count": (lambda d: _edit(d / "fru.json", r'"num-mac-address": 512', '"num-mac-address": 256'), {"TC-SWH-079"}),
    "auth_failures": (lambda d: _edit_rule(d, "authentication\\ failure", rc=0, file=None,
                                           stdout="Oct 26 10:00:01 sw1 sshd[42]: Failed password for root from 10.9.9.9\n"),
                      {"TC-SWH-080"}),
    "dmesg_critical": (lambda d: _edit_rule(d, "dmesg\\ \\-\\-level", file=None,
                                            stdout="[  12.3] mce: [Hardware Error]: Machine check events logged\n"),
                       {"TC-SWH-081"}),
    "no_rsyslog_rotation": (lambda d: _edit(d / "logrotate_d.txt", r"^rsyslog\n", ""), {"TC-SWH-082"}),
    "firewall_open": (lambda d: _edit(d / "iptables.txt", r"^-A INPUT (.|\n)*", ""), {"TC-SWH-083"}),
    "ssh_root_login": (lambda d: _edit(d / "sshd_t.txt", r"^permitrootlogin no", "permitrootlogin yes"), {"TC-SWH-084"}),
    "cli_not_locked": (lambda d: _edit_rule(d, "config\\ vlan\\ add\\ 4093", rc=0, file=None, stdout=""), {"TC-SWH-085"}),
    "extra_uid0": (lambda d: _edit(d / "passwd.txt", r"\Z", "backdoor:x:0:0::/root:/bin/bash\n"), {"TC-SWH-086"}),
    "default_hostname": (lambda d: _edit(d / "hostname.txt", r"^sw1$", "sonic"), {"TC-SWH-087"}),
    "clock_unsynced": (lambda d: _edit(d / "ntp_sync.txt", r"^yes$", "no"), {"TC-SWH-088", "TC-CTL-035"}),
    "lldp_total_wrong": (lambda d: _edit(d / "lldp_table.txt", r"displayed:  8", "displayed:  9"), {"TC-SWH-090"}),
    "fec_uncorr_climbing": (lambda d: _cycle(d, "fec_asic0.txt", r"(Ethernet8  U\s+4,705\s+)10", r"\g<1>99",
                                             "fec\\-stats\\ \\-n\\ asic0"), {"TC-SWH-040", "TC-SWH-092"}),
    "config_hash_changes": (lambda d: _cycle(d, "sha256.txt", r"^9f2c", "0000", "^sha256sum < "),
                            {"TC-SWH-093", "TC-SWH-096", "TC-SWH-097"}),
    "config_load_fails": (lambda d: _edit_rule(d, "config load -y", rc=1, stdout="", stderr="Error: invalid JSON"),
                          {"TC-SWH-094"}),
    "config_reload_fails": (lambda d: _edit_rule(d, "^config\\ reload\\ \\-y$", rc=1), {"TC-SWH-095"}),
    "config_save_fails": (lambda d: _edit_rule(d, "^config\\ save\\ \\-y$", rc=1), {"TC-SWH-097"}),
    "disk_slow": (lambda d: _edit(d / "dd.txt", r"167 MB/s", "4.2 MB/s"), {"TC-SWH-098"}),
    "syncd_restart_fails": (lambda d: _edit_rule(d, "restart\\ syncd@0", rc=1, stderr="Job failed"), {"TC-SWH-104"}),
    "techsupport_incomplete": (lambda d: _edit(d / "techsupport_list.txt", r"^.*/dump/\n.*/dump/docker\.ps\n", ""), {"TC-SWH-107"}),
    # ---- IFCP on the wire (tests/sideband/test_ifcp_wire.py)
    "ifcp_no_egress": (lambda d: [_edit(d / "ifcp_capture.txt", r"^\S+ 12:34:56:78:01:00 > .*\n\t0x0000:.*\n", "")
                                 for _ in range(3)],
                       {"TC-040", "TC-043", "TC-SBM-175"}),
    "ifcp_no_ingress": (lambda d: [_edit(d / "ifcp_capture.txt", r"^\S+ 74:27:2c:09:72:a9 > (.*\n)(\t0x.*\n)+", "")
                                  for _ in range(3)],
                        {"TC-041", "TC-043"}),
    "ifcp_wrong_priority": (lambda d: _edit(d / "ifcp_capture.txt", r"vlan 0, p 7,", "vlan 0, p 0,"), {"TC-SBM-175"}),
    "ifcp_reply_from_stranger": (lambda d: _edit(d / "ifcp_capture.txt", r"^(\S+ )74:27:2c:09:72:a9 > ", r"\g<1>74:27:2c:00:00:01 > "),
                                 {"TC-041"}),
    "ifcp_keepalive_burst": (lambda d: _edit(d / "ifcp_capture.txt", r"^1793044805\.930709", "1793044800.990709"),
                             {"TC-043"}),
    "ifcp_second_peer": (lambda d: _edit(d / "ifcp_capture.txt", r"^(1793044810\.930709 12:34:56:78:01:00 > )74:27:2c:09:72:a9",
                                         r"\g<1>74:27:2c:04:76:08"), {"TC-040"}),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_fault_is_detected(tmp_path, fault):
    mutate, must_fail = FAULTS[fault]
    verdicts = _run(tmp_path, mutate)
    failed = {k for k, v in verdicts.items() if v in ("FAIL", "PARTIAL", "BLOCKED")}
    assert must_fail <= failed, f"{fault}: expected {sorted(must_fail)} to fail, failed={sorted(failed)}"
    unexpected = failed - must_fail
    assert not unexpected, f"{fault}: collateral failures {sorted(unexpected)}"


def test_failed_ping_keeps_command_and_afmctl_output(tmp_path):
    """A failed L2 ping leaves its exact afmctl command, host, rc and afmctl's output in the result properties (the
    vendor asks for them; a long test log can be cut off before the pings)."""
    import json as _json
    _run(tmp_path, FAULTS["ping_loss"][0], extra_args=("--tc", "TC-AFM-001"))
    row = next(r for r in csv.DictReader(open(next((tmp_path / "results").glob("*/tc_results.csv"))))
               if r["tc_id"] == "TC-AFM-001")
    props = _json.loads(row["properties"])
    ev = props.get("ping_evidence", []) + props.get("probe_ping_evidence", [])
    assert ev, props.keys()
    for e in ev:
        assert e["cmd"].startswith("timeout ") and "afmctl test ping -b " in e["cmd"], e["cmd"]
        assert "2/35 fail" in e["output"] and e["host"] and e["at"].endswith("UTC") and "rc" in e
