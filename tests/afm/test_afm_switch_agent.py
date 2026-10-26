"""Switch agent behaviour driven through AFM (DISRUPTIVE): VLAN ownership on decommission, data-fabric update on
vPOD CRUD, switch state reporting, automatic data-fabric reconfigure on the policy timer, a port detail change
reflected in AFM (TC-SWA-202, thresholds.afm.port_detail_wait_s, 60 s).

The user VLAN for TC-SWA-037 is created with commands.switch.user_vlan_add / _del (placeholders {ns}, {nsflag} =
' -n asicN' or '', {vlan}; default: write / delete VLAN|Vlan<id> in the ASIC's CONFIG_DB with sonic-db-cli - this
SONiC's ``config vlan add`` has no -n option, g02-g17 17 Oct), VLAN thresholds.afm.user_vlan, 3999.
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
    add = inventory.cmd("switch.user_vlan_add", "sonic-db-cli{nsflag} CONFIG_DB hset 'VLAN|Vlan{vlan}' vlanid {vlan}")
    delete = inventory.cmd("switch.user_vlan_del", "sonic-db-cli{nsflag} CONFIG_DB del 'VLAN|Vlan{vlan}'")
    ns = sw.namespaces()[0] or ""
    fmt = {"ns": ns, "nsflag": f" -n {ns}" if ns else "", "vlan": vid}
    non_afm = {f"Vlan{int(v)}" for v in (ops.th("non_afm_vlans", []) or [])}   # e.g. the stale ARC Vlan2
    before = _vlans(sw)
    try:
        r = sw.run(add.format(**fmt), sudo=True, timeout=60)
        checks.check(r.ok, f"creating user VLAN {vid} failed: {r.tail()!r}")
        afm_vlans = before - {f"Vlan{vid}"} - non_afm
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
        sw.run(delete.format(**fmt), sudo=True, timeout=60)
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
    needdatafabricreconfiguration; the port back restores the planes. Starts from a settled system vPOD
    (online/degraded, no pending ops): g02-g17 17 Oct it was still 'initializing' from the previous case's GPU
    re-add when the port went down, and went configurationfailed (36, 31, 31) instead."""
    sysv = ops.system_vpod_name()
    checks.check(ops.wait_vpod(sysv) in TERMINAL and ops.wait_pending_clear(sysv),
                 f"{sysv} {ops.vpod_state(sysv)} / pending ops before the test")
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



def _afm_port(ops, sw, ns, port) -> dict | None:
    """AFM's record of one inventory switch port (SwitchNode status.asic-info; asic 'id' N or 'name' -> asicN)."""
    for sn in ops.afm.switches():
        try:
            if ops.ssh_switch(sn).name != sw.name:
                continue
        except Exception:  # noqa: BLE001 - a SwitchNode without an inventory switch
            continue
        for a in ((sn.get("status") or {}).get("asic-info") or {}).get("asics") or []:
            a_ns = a.get("name") or (f"asic{a['id']}" if a.get("id") is not None else None)
            for p in a.get("ports") or []:
                if p.get("name") == port and (ns is None or a_ns == ns):
                    return p
    return None


@pytest.mark.tc("TC-SWA-202")
def test_switch_port_details_updated(ops, afm_ready, links, checks, record_property):
    """A switch port detail changed on the switch is updated in AFM: one up GPU-facing port shut (SONiC Admin down,
    Oper down) - AFM's record of it (SwitchNode status.asic-info) leaves PortReady and, where AFM lists admin-status,
    reads down, within thresholds.afm.port_detail_wait_s (60 s); started again - PortReady (admin-status up) within
    the same wait. The reversible change is the admin state: speed / FEC / duplex are not in AFM's port record and the
    VLANs are AFM's own, so they are not changed. The record AFM keeps for the port is recorded before / during /
    after with the SONiC row."""
    name = ops.pick_compute()
    sw, ns, port, _ = ops.gpu_links(ops.compute(name)["spec"]["slot-id"])[0]

    def sonic() -> dict:
        return next((r for r in sw.interfaces_status(ns) if r.get("Interface") == port), {})

    def admin(p) -> str:
        return str((p or {}).get("admin-status", "")).lower()
    before = _afm_port(ops, sw, ns, port)
    if before is None:
        pytest.fail(f"AFM does not list {sw.name} {ns} {port} in its SwitchNode status.asic-info", pytrace=False)
    has_admin = "admin-status" in before
    record_property("fields", sorted(before))
    t = float(ops.th("port_detail_wait_s", 60))
    checks.check(before.get("oper-state") == "PortReady", f"{sw.name} {port}: AFM {before.get('oper-state')} before the test")
    snap = {"before": {"afm": before, "sonic": sonic()}}
    try:
        links.shut(sw, ns, port)
        checks.check(ops.wait_oper(sw, ns, port, "down", 60), f"{sw.name} {port}: still oper up on the switch after shutdown")
        ok = ops.wait(lambda: (lambda p: p is not None and p.get("oper-state") != "PortReady"
                               and (not has_admin or admin(p) == "down"))(_afm_port(ops, sw, ns, port)), t, 5)
        snap["shut"] = {"afm": _afm_port(ops, sw, ns, port), "sonic": sonic()}
        checks.check(ok, f"{sw.name} {port} shut: AFM still shows {snap['shut']['afm']} after {t:.0f}s")
    finally:
        links.restore(sw, ns, port, checks)
    ok = ops.wait(lambda: (lambda p: p is not None and p.get("oper-state") == "PortReady"
                           and (not has_admin or admin(p) == "up"))(_afm_port(ops, sw, ns, port)), t, 5)
    snap["restored"] = {"afm": _afm_port(ops, sw, ns, port), "sonic": sonic()}
    record_property("port", snap)
    checks.check(ok, f"{sw.name} {port} started: AFM shows {snap['restored']['afm']} after {t:.0f}s")
    checks.assert_all()
