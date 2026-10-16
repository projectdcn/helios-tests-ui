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
    qslots = inventory.quarantined_slots()          # agents of quarantined trays are not counted
    if qslots:
        record_property("quarantined_slots", {s: owners.get(s, []) for s in sorted(qslots)})
        owners = collections.defaultdict(list, {s: v for s, v in owners.items() if s not in qslots})
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


def _check_agent(tag, health, requests, checks):
    if not checks.check(health is not None, f"{tag}: /api/health not answering"):
        return None
    checks.check(health.get("is-connected") is True, f"{tag}: is-connected={health.get('is-connected')}")
    cfg, cur = set(health.get("configured-controllers") or []), set(health.get("current-controllers") or [])
    checks.check(cfg and cfg == cur, f"{tag}: configured controllers {sorted(cfg)} != current {sorted(cur)}")
    checks.check(not health.get("unhealthy-services"), f"{tag}: unhealthy services {health.get('unhealthy-services')}")
    checks.check(not health.get("restart-required"), f"{tag}: restart-required")
    checks.check(not health.get("health-conditions"), f"{tag}: health conditions {health.get('health-conditions')}")
    if requests:
        # state 1 = applied; a `reason` alone is informational (e.g. 'station-enabled-ports-already-in-sync')
        bad = [r for r in requests if (r.get("config-status") or {}).get("state") != 1 or not r.get("completed-by-agent-at")]
        checks.check(not bad, f"{tag}: {len(bad)} config request(s) not applied/completed, e.g. "
                              f"{[(r.get('created-at', '')[:19], r.get('config-status')) for r in bad[:3]]}")
    return health.get("git-version")


@pytest.mark.tc("TC-SEQ-006")
def test_node_agent_control_channel(rack, inventory, checks, record_property):
    """Every sb-node-agent (REST 13000+slot) and the switch-agent (first answering port in 13018..13035)
    is connected to its configured controllers, healthy, and has applied every config request it received;
    all agents run the same build."""
    versions = {}
    for sw in rack.switches.values():
        for slot in sw.sb_node_agent_slots():
            tag = f"{sw.name} sb-node-agent slot {slot}"
            versions[tag] = _check_agent(tag, sw.agent_api(13000 + slot, "/api/health"),
                                         sw.agent_api(13000 + slot, "/api/runtime/requests"), checks)
        lo, hi = inventory.th("sideband.switch_agent_ports", [13018, 13035])
        port = next((p for p in range(int(lo), int(hi) + 1) if sw.agent_api(p, "/api/health") is not None), None)
        if checks.check(port, f"{sw.name}: no switch-agent health endpoint on ports {lo}-{hi}"):
            tag = f"{sw.name} switch-agent :{port}"
            versions[tag] = _check_agent(tag, sw.agent_api(port, "/api/health"), None, checks)
    builds = {v for v in versions.values() if v}
    checks.check(len(builds) <= 1, f"agents run different builds: {versions}")
    record_property("agent_versions", versions)
    checks.check(versions, "no AFM agents found")
    checks.assert_all()
