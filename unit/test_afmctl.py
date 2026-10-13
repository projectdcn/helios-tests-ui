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
