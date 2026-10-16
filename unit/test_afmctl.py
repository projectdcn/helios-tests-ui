"""afmctl parsers + discovery-driven L2 ping plan, checked against the commands actually issued."""
from __future__ import annotations

import re
from pathlib import Path

from helios import datapath
from helios.afmctl import parse_ping, parse_show_device, parse_show_port
from helios.inventory import load_inventory
from helios.rack import Checks, Rack

ROOT = Path(__file__).resolve().parents[1]


def test_parse_ping_summary_and_failed_ports():
    text = ("  1   0   1  PASS  1  PASS  1  PASS\n"
            "  1   7   1  FAIL  0  FAIL  1  PASS\n"
            "Summary:\n  IFoE Request    : 26/27 PASS, 1/27 fail (3.70% loss)\n")
    r = parse_ping(text)
    assert (r.request_pass, r.request_fail, r.total) == (26, 1, 27) and r.failed_ports == ["7"] and not r.ok
    assert parse_ping("Summary:\n  IFoE Request    : 27/27 PASS, 0/27 fail (0.00% loss)\n").ok
    busy = parse_ping("ERR - Failed to start ping test ... err Device or resource busy\n")
    assert busy.busy and busy.total is None and not busy.ok


def test_parse_show_device_and_port():
    devs = parse_show_device('{"device":[{"bdf":"0002:01:00.1","spec":{"accelerator_id":"9","config_phase":"ACTIVE"}},'
                             '{"bdf":"0001:01:00.1","spec":{"accelerator_id":"8","config_phase":"PROVIDER"}}]}')
    assert [(d.bdf, d.accel_id, d.config_phase) for d in devs] == [
        ("0001:01:00.1", "8", "PROVIDER"), ("0002:01:00.1", "9", "ACTIVE")]
    ports = parse_show_port('{"device":[{"bdf":"0001:01:00.1","port":['
                            '{"id":"1","spec":{"admin_state":"enabled"},"status":{"link_status":"LINK_UP"}},'
                            '{"id":"2","spec":{"admin_state":"idle"},"status":{"link_status":"LINK_UP"}},'
                            '{"id":"0","spec":{"admin_state":"enabled"},"status":{"link_status":"NO_PHY_LINK"}}]}]}')
    assert [(p.id, p.up) for p in ports["0001:01:00.1"]] == [(0, False), (1, True), (2, False)]


def test_l2_ping_plan_uses_discovered_ids_and_common_up_ports():
    inv = load_inventory(ROOT / "selftest" / "inventory.yaml")
    rack = Rack(inv)
    computes = list(rack.computes.values())
    checks = Checks()
    n = datapath.l2_ping(computes, inv, checks)
    assert not checks.failures, checks.failures
    # ring: per tray 4 within (next GPU) + 4 cross (same-index GPU on the other tray)
    assert n == 16
    pings = [c for comp in computes for c in comp.t.calls if "afmctl test ping" in c]
    assert len(pings) == 16
    c1_targets = {m.group(1) for c in rack.computes["c1"].t.calls
                  if (m := re.search(r"--dst-accelerator (\d+)", c))}
    # c1 pings its own next GPU (1,2,3,0) and c2's same-index GPU, whose IDs are 8-11 - not the naive 4-7
    assert c1_targets == {"0", "1", "2", "3", "8", "9", "10", "11"}
    for c in pings:
        ports = re.search(r"--port (\S+)", c).group(1).split(",")
        assert "7" not in ports and len(ports) == 35  # down port excluded from the common LINK_UP set


class _Checks:
    def __init__(self):
        self.failures = []

    def fail(self, msg):
        self.failures.append(msg)


def test_acc_renumbering_within_tray_is_accepted():
    from helios.afmctl import check_acc_renumbering
    tray = {str(i): f"ct{i // 4 + 1:02d}" for i in range(8)}
    c = _Checks()
    m = check_acc_renumbering([(0, 3, "a"), (1, 2, "b"), (2, 1, "c"), (3, 0, "d"), (0, 3, "again")], c, "t", tray)
    assert m == {"0": "3", "1": "2", "2": "1", "3": "0"} and not c.failures


def test_acc_renumbering_shift_into_next_tray_fails():
    from helios.afmctl import check_acc_renumbering
    tray = {str(i): f"ct{i // 4 + 1:02d}" for i in range(8)}
    c = _Checks()
    check_acc_renumbering([(i, i + 1, "x") for i in range(8)], c, "t", tray)
    assert any("not one of ct01's accelerators" in f for f in c.failures)


def test_acc_renumbering_conflict_fails():
    from helios.afmctl import check_acc_renumbering
    c = _Checks()
    check_acc_renumbering([(0, 3, "a"), (0, 2, "b"), (1, 3, "c")], c, "t")
    assert len(c.failures) == 2


G02_1337 = """0004:01:00.1                   : Ping test results (1 pings per port pair)
Accel ID    Port#     IFoE Req        IFoE Rsp        Non-IFoE    
--------    -----     --------        ---------       --------    
8           24        1/1 PASS        1/1 PASS        1/1 PASS    
8           25        1/1 PASS        1/1 PASS        0/1 FAIL    
8           26        0/1 FAIL        1/1 PASS        1/1 PASS    
8           27        1/1 PASS        1/1 PASS        1/1 PASS    

Summary:
  IFoE Request    : 35/36 PASS, 1/36 fail (2.78% loss)
  IFoE Response   : 36/36 PASS, 0/36 fail (0.00% loss)
  Non-IFoE        : 35/36 PASS, 1/36 fail (2.78% loss)
"""


def test_parse_ping_reads_all_three_checks_per_port():
    """g02-g17 16 Oct 13:37 (CT03 acc 11 -> 8): two separate failures - port 25 non-IFoE, port 26 IFoE request."""
    r = parse_ping(G02_1337)
    assert (r.request_fail, r.response_fail, r.non_ifoe_fail, r.total) == (1, 0, 1, 36)
    assert r.port_checks == {"25": ["non-IFoE"], "26": ["req"]} and not r.ok
    assert r.describe() == "IFoE request 1/36, response 0/36, non-IFoE 1/36 failed; ports 25 (non-IFoE), 26 (req)"


def test_a_non_ifoe_only_failure_is_not_ok():
    """The request count alone would call this a pass."""
    text = G02_1337.replace("8           26        0/1 FAIL", "8           26        1/1 PASS").replace(
        "IFoE Request    : 35/36 PASS, 1/36 fail", "IFoE Request    : 36/36 PASS, 0/36 fail")
    r = parse_ping(text)
    assert r.request_fail == 0 and r.non_ifoe_fail == 1 and not r.ok
