"""Switch agent behaviour driven through AFM (DISRUPTIVE): VLAN ownership on decommission, data-fabric update on
vPOD CRUD, switch state reporting, automatic data-fabric reconfigure on the policy timer.

The user VLAN for TC-SWA-037 is created with commands.switch.user_vlan_add / _del
(default ``config vlan add|del -n {ns} {vlan}``, VLAN thresholds.afm.user_vlan, 3999).
"""
from __future__ import annotations

import time

import pytest

from helios.afm import conditions_ok
from helios.afm_ops import SN, TERMINAL

pytestmark = [pytest.mark.l3, pytest.mark.disruptive]


def _vlans(sw) -> set[str]:
    return {v for ns in sw.namespaces() for v in sw.vlans(ns)}


@pytest.mark.tc("TC-SWA-037")
def test_decommission_keeps_user_vlans(ops, afm_ready, inventory, checks, record_property):
    """Decommission removes only the AIFM-managed VLANs: a user-created VLAN on the switch survives the
    drain + decommission, the AFM VLANs are gone; the switch is then deleted and readmitted."""
    if len(ops.afm.switches()) < 2:
        pytest.skip("needs 2 switches (mini rack: decommission is refused, TC-AFM-035A)")
    t = ops.pick_switch(controller=False)
    name, sw = t["meta"]["name"], ops.ssh_switch(t)
    vid = int(ops.th("user_vlan", 3999))
    add = inventory.cmd("switch.user_vlan_add", "config vlan add -n {ns} {vlan}")
    delete = inventory.cmd("switch.user_vlan_del", "config vlan del -n {ns} {vlan}")
    ns = sw.namespaces()[0] or ""
    before = _vlans(sw)
    try:
        r = sw.run(add.format(ns=ns, vlan=vid), sudo=True, timeout=60)
        checks.check(r.ok, f"creating user VLAN {vid} failed: {r.tail()!r}")
        afm_vlans = before - {f"Vlan{vid}"}
        ops.afm.post(f"{SN}/{name}/DrainSwitchNode", {})
        ops.wait_switch_phase(name, "drained", 900)
        ops.afm.post(f"{SN}/{name}/DecommissionSwitchNode", {})
        checks.check(ops.wait_switch_phase(name, "decommissioned", 300), f"{name} {ops.switch_phase(name)}")
        after = _vlans(sw)
        record_property("vlans", {"before": sorted(before), "decommissioned": sorted(after)})
        checks.check(f"Vlan{vid}" in after, f"user VLAN {vid} removed by decommission")
        checks.check(not (afm_vlans & after), f"AFM VLANs left after decommission: {sorted(afm_vlans & after)[:6]}")
        ops.afm.raw("DELETE", f"{SN}/{name}")
        ops.wait(lambda: ops.switch_phase(name) == "absent", 60, 5)
        ops.reset_switch_identity(sw)
        checks.check(ops.wait(lambda: ops.switch_ready(name), float(ops.th("switch_readmit_s", 600)), 15), f"{name} not readmitted")
    finally:
        sw.run(delete.format(ns=ns, vlan=vid), sudo=True, timeout=60)
        if not ops.switch_ready(name):
            ph = ops.switch_phase(name)
            if ph == "drained":
                ops.afm.raw("POST", f"{SN}/{name}/ReactivateSwitchNode", {})
            elif ph != "admitted":
                ops.afm.raw("DELETE", f"{SN}/{name}")
                ops.reset_switch_identity(sw)
            ops.wait(lambda: ops.switch_ready(name), 600, 15)
    checks.check(ops.wait(lambda: _vlans(sw) >= afm_vlans, 300, 15), f"{name}: AFM VLANs not restored after readmit")
    checks.assert_all()


@pytest.mark.tc("TC-SWA-180")
def test_data_fabric_update_on_vpod_crud(ops, afm_ready, uniq, checks, record_property):
    """vPOD create programs the data fabric on every switch (more VLANs, vPOD planes provisioned) and vPOD
    delete removes it (VLAN count back)."""
    sysv = ops.system_vpod_name()
    sws = list(ops.rack.switches.values())
    before = {s.name: ops.vlan_count(s) for s in sws}
    name = ops.pick_compute()
    user, entries = uniq("vp"), []
    try:
        entries = ops.remove_compute(sysv, name)
        ops.create_vpod(user, entries)
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} {ops.vpod_state(user)}")
        checks.check(ops.wait(lambda: all(ops.vlan_count(s) > before[s.name] for s in sws), 120, 10),
                     "a switch got no VLAN for the new vPOD")
        checks.check((ops.planes(user)[0] or 0) > 0, f"{user} has no provisioned planes")
        ops.delete_vpod(user)
        checks.check(ops.wait(lambda: all(ops.vlan_count(s) == before[s.name] for s in sws), 180, 10),
                     f"VLANs not removed after delete: {({s.name: ops.vlan_count(s) for s in sws})} vs {before}")
    finally:
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)
    checks.assert_all()


@pytest.mark.tc("TC-SWA-204")
def test_switch_states(ops, afm_ready, checks, record_property):
    """The switch states AFM reports across an agent outage: Ready (healthy=true) -> Unknown (agent stopped) ->
    Ready again; with 2+ switches also admitted -> drained -> admitted."""
    t = ops.pick_switch(controller=None if len(ops.afm.switches()) == 1 else False)
    name, sw = t["meta"]["name"], ops.ssh_switch(t)
    seen = []

    def obs(label):
        o = ops.switch(name)
        seen.append((label, (o.get("status") or {}).get("admission-phase"),
                     ((o.get("status") or {}).get("conditions") or [{}])[0].get("status")))
    obs("start")
    try:
        ops.afm_agent(sw, "stop")
        ops.wait(lambda: not conditions_ok(ops.switch(name)), float(ops.th("agent_down_detect_s", 600)), 20)
        obs("agent_stopped")
    finally:
        ops.afm_agent(sw, "start")
    ops.wait(lambda: ops.switch_ready(name), 300, 10)
    obs("agent_started")
    if len(ops.afm.switches()) >= 2:
        try:
            ops.afm.post(f"{SN}/{name}/DrainSwitchNode", {})
            ops.wait_switch_phase(name, "drained", 900)
            obs("drained")
        finally:
            ops.afm.raw("POST", f"{SN}/{name}/ReactivateSwitchNode", {})
        ops.wait(lambda: ops.switch_ready(name), 600, 15)
        obs("reactivated")
    record_property("states", seen)
    d = {k: (ph, h) for k, ph, h in seen}
    checks.check(d["start"] == ("admitted", "true"), f"start {d['start']}")
    checks.check(d["agent_stopped"][1] != "true", f"agent stopped but healthy: {d['agent_stopped']}")
    checks.check(d["agent_started"] == ("admitted", "true"), f"after start {d['agent_started']}")
    if "drained" in d:
        checks.check(d["drained"][0] == "drained", f"drained {d['drained']}")
        checks.check(d["reactivated"] == ("admitted", "true"), f"reactivated {d['reactivated']}")
    checks.assert_all()


@pytest.mark.tc("TC-SWA-214")
def test_auto_reconfigure_on_policy_timer(ops, afm_ready, links, checks, record_property):
    """With the automatic system policy, a switch port down past the link-fail-threshold triggers the data-fabric
    reconfiguration on its own: the vPOD degrades with fewer planes, never waits in
    needdatafabricreconfiguration; the port back restores the planes."""
    sysv = ops.system_vpod_name()
    base = ops.settled_planes(sysv, interval=float(ops.th("plane_settle_interval_s", 10)))
    name = ops.pick_compute()
    sw, ns, port, _ = ops.gpu_links(ops.compute(name)["spec"]["slot-id"])[0]
    states = set()
    t0 = time.monotonic()
    links.shut(sw, ns, port)
    ok = ops.wait(lambda: (states.add(ops.vpod_state(sysv)) or ops.vpod_state(sysv)) == "degraded"
                  and (ops.planes(sysv)[1] or 0) < (base[1] or 0), ops.link_fail_threshold(sysv) + 60, 2)
    record_property("reconfigured_after_s", round((time.monotonic() - t0) / float(ops.th("time_scale", 1)), 1))
    checks.check(ok, f"{sysv} not auto-reconfigured: {ops.vpod_state(sysv)} {ops.planes(sysv)}")
    checks.check("needdatafabricreconfiguration" not in states, "automatic policy waited for a manual reconfigure")
    links.restore(sw, ns, port, checks)
    checks.check(ops.wait(lambda: (ops.planes(sysv)[1] or 0) >= (base[1] or 0) and ops.vpod_state(sysv) in TERMINAL,
                          float(ops.th("plane_restore_s", 300)), 10), f"planes {ops.planes(sysv)} not back to {base}")
    checks.assert_all()

