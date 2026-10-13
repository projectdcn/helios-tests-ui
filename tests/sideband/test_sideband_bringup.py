"""TC-BU — sideband control-plane bring-up (sideband control mode: per-compute sb-node-agents run inside
each switch's afm-agent container; computes run no AFM agent).

Read-only. Expected computes = the inventory's compute list (``sideband.expected_slots`` overrides).
"""
from __future__ import annotations

import collections

import pytest

from helios.afmctl import Afmctl
from helios.rack import fan_out

pytestmark = pytest.mark.l2


def _all_slots(rack):
    slots = {}
    for sw, res, exc in fan_out(lambda s: s.sb_node_agent_slots(), list(rack.switches.values())):
        slots[sw.name] = (res or [], exc)
    return slots


@pytest.mark.tc("TC-BU-002")
def test_sideband_agent_and_node_agent_proxy_running(rack, checks):
    """afm-agent active (unit and container) on every switch; at least one switch hosts sb-node-agents."""
    hosting = 0
    for sw in rack.switches.values():
        checks.check(sw.run("systemctl is-active afm-agent").stdout.strip() == "active",
                     f"{sw.name}: afm-agent service not active")
        checks.check("afm-agent" in sw.containers(), f"{sw.name}: afm-agent container not running")
        hosting += bool(sw.sb_node_agent_slots())
    checks.check(hosting, "no switch hosts any sb-node-agent")
    checks.assert_all()


@pytest.mark.tc("TC-BU-006")
def test_no_agent_on_compute_nodes(computes, checks):
    """Sideband mode: no AFM agent processes on compute trays."""
    names = ("afm-agent", "inb-node-agent", "sb-node-agent", "node-agent", "switch-agent")
    for c, r, exc in fan_out(lambda c: c.run("ps -eo comm,args", timeout=30), computes):
        if exc:
            checks.fail(f"{c.name}: {exc}")
            continue
        running = sorted({n for line in r.stdout.splitlines() for n in names if line.split(None, 1)[0] == n})
        checks.check(not running, f"{c.name}: agent process(es) running on compute: {running}")
    checks.assert_all()


@pytest.mark.tc("TC-BU-009")
def test_node_agent_count_matches_compute_trays(rack, inventory, checks, record_property):
    """Exactly one sb-node-agent per compute slot across all switches, and as many slots as compute trays."""
    expected = inventory.th("sideband.expected_slots")
    n_trays = len(rack.computes)
    per_switch = _all_slots(rack)
    owners = collections.defaultdict(list)
    for sw, (slots, exc) in per_switch.items():
        if exc:
            checks.fail(f"{sw}: cannot list sb-node-agents: {exc}")
        for s in slots:
            owners[s].append(sw)
    record_property("slots", {s: v for s, v in sorted(owners.items())})
    dup = {s: v for s, v in owners.items() if len(v) > 1}
    checks.check(not dup, f"slots served by more than one agent: {dup}")
    if expected is not None:
        checks.check(sorted(owners) == sorted(expected), f"slots {sorted(owners)}, expected {sorted(expected)}")
    checks.check(len(owners) == n_trays, f"{len(owners)} sb-node-agent slots {sorted(owners)} for {n_trays} compute trays")
    checks.assert_all()


@pytest.mark.tc("TC-BU-011")
def test_compute_ifoe_interfaces_active(computes, inventory, checks, record_property):
    """Every GPU is config_phase ACTIVE and all its IFoE ports are enabled + LINK_UP
    (``datapath.known_down_ports``: {compute: {bdf: [port, ...]}} for accepted exceptions)."""
    known = inventory.th("datapath.known_down_ports") or {}

    def one(c):
        a = Afmctl(c)
        devs = a.devices()
        return devs, {d.bdf: a.ports(d.bdf) for d in devs}

    summary = {}
    for c, res, exc in fan_out(one, computes):
        if exc:
            checks.fail(f"{c.name}: afmctl failed: {exc}")
            continue
        devs, ports = res
        checks.check(len(devs) == c.gpus, f"{c.name}: {len(devs)} IFoE devices, expected {c.gpus}")
        for d in devs:
            checks.check(d.config_phase == "ACTIVE", f"{c.name}/{d.bdf}: config_phase {d.config_phase}")
            allowed = set((known.get(c.name) or {}).get(d.bdf) or [])
            bad = [f"{p.id}:{p.admin_state}/{p.link_status}" for p in ports[d.bdf] if not p.up and p.id not in allowed]
            summary[f"{c.name}/{d.bdf}"] = f"{sum(p.up for p in ports[d.bdf])}/{len(ports[d.bdf])} up"
            checks.check(ports[d.bdf], f"{c.name}/{d.bdf}: no ports reported")
            checks.check(not bad, f"{c.name}/{d.bdf}: ports not enabled+LINK_UP: {bad[:10]}")
    record_property("ports_up", summary)
    checks.assert_all()
