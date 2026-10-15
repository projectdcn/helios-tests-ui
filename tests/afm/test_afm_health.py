"""HC-000 - AFM side of the full-rack health check (helios/healthcheck.py, tools/rack_health.py).

Fails on any AFM rack-state problem (helios.afm.rack_health: cluster, switch / compute admission and health, system
vPOD state, pending operations) and records a snapshot that ``tools/rack_health.py --baseline`` compares between runs
(before / after a disruptive batch). Read-only.
"""
from __future__ import annotations

import pytest

from helios.afm import conditions_ok, rack_health
from helios.rack import fan_out

pytestmark = pytest.mark.l1


def _state(obj) -> dict:
    st = obj.get("status") or {}
    return {"admission": st.get("admission-phase"), "healthy": conditions_ok(obj)}


@pytest.mark.healthcheck("blocker")
@pytest.mark.tc("HC-000")
def test_afm_rack_state_and_snapshot(afm, ops, rack, checks, record_property):
    """AFM reports a healthy rack; snapshot of cluster, nodes, system vPOD, node-agent placement and up fabric ports."""
    snap: dict = {}
    cl = afm.cluster().get("status") or {}
    snap["cluster"] = {"healthy": conditions_ok(afm.cluster()), "leader": cl.get("leader"),
                       "quorum": sorted(m.get("name") for m in (cl.get("quorum-status") or {}).get("members") or [])}
    snap["switches"] = {s["meta"]["name"]: _state(s) for s in afm.switches()}
    snap["computes"] = {c["meta"]["name"]: _state(c) for c in afm.computes()}
    sv = afm.system_vpod()
    if sv:
        st = sv.get("status") or {}
        snap["system_vpod"] = {"name": sv["meta"]["name"], "state": st.get("vpod-state"),
                               "planes": list(ops.planes(sv["meta"]["name"])), "pending_ops": len(st.get("pending-ops") or [])}
    switches = list(rack.switches.values())
    agents, ports = {}, {}
    for sw, res, exc in fan_out(lambda s: (ops.sb_agents(s), s.namespaces()), switches):
        if exc:
            agents[sw.name] = f"error: {str(exc)[:80]}"
            continue
        slots, nss = res
        agents[sw.name] = sorted(slots)
        up = 0
        for ns in nss:
            up += sum(1 for r in sw.interfaces_status(ns) if str(r.get("Oper", "")).lower() == "up")
        ports[sw.name] = up
    snap["node_agent_slots"] = agents
    snap["fabric_ports_up"] = ports
    record_property("snapshot", snap)
    for problem in rack_health(afm):
        checks.fail(problem)
    checks.assert_all()
