"""AFM L2 lifecycle (DISRUPTIVE, needs --allow-disruptive): agents, controllers, vPOD CRUD and agent-down
behaviour, compute and switch lifecycle, Reconfigure variants. Procedures follow the g02-g17 full-rack runs
(10-11 Oct 2026) with their script bugs fixed (see helios/afm_ops.py).

Every test starts behind ``afm_ready`` (BLOCKED if AFM is unhealthy), restores what it changed in a
``finally`` (vPOD membership, agents, switch/compute admission) and is re-checked by ``afm_ready`` after.
Switch-side steps need SSH to the switch trays (inventory switches; AFM SwitchNodes are matched by
management IP or ``afm_name:``). Switch-lifecycle cases skip on a one-switch mini rack (see TC-AFM-035A).
"""
from __future__ import annotations

import time

import pytest

from helios.afm import CLUSTER, conditions_ok
from helios.afm_ops import CN, SN, TERMINAL

FP = f"{CLUSTER}/failoverpolicies"
pytestmark = [pytest.mark.l2, pytest.mark.disruptive]


def _need_switches(afm, n: int):
    have = len(afm.switches())
    if have < n:
        pytest.skip(f"{have} switch node(s): needs {n} (switch lifecycle on a mini rack is TC-AFM-035A)")


def _need_sideband(ops):
    if not any(ops.sb_agents(sw) for sw in ops.rack.switches.values()):
        pytest.skip("no sb-node-agent found on any switch: rack is not in sideband control mode")


def _baseline(ops, checks, record_property):
    sysv = ops.system_vpod_name()
    base = ops.settled_planes(sysv, interval=float(ops.th("plane_settle_interval_s", 10)))
    record_property("baseline_planes", base)
    return sysv, base


def _planes_restored(ops, checks, sysv, base, timeout=None):
    timeout = timeout or float(ops.th("plane_restore_s", 900))
    ok = ops.wait(lambda: (ops.planes(sysv)[1] or 0) >= (base[1] or 0), timeout, 30, "available planes restored")
    checks.check(ok, f"{sysv} available planes {ops.planes(sysv)} below baseline {base} after {timeout:.0f}s "
                     "(full-rack product issue: Reconcile needed)")



def _afm_vlans(ops, sw) -> set[str]:
    """VLANs on the switch that AFM manages: all of them except thresholds.afm.non_afm_vlans (ids). g02-g17 17 Oct:
    TC-AFM-024 failed '2 VLANs after decommission' = Vlan2 on both ASICs, the stale VLAN left by the ARC run (not
    AFM's; open with AMD) - AFM's Vlan100/200/300 were gone."""
    skip = {f"Vlan{int(v)}" for v in (ops.th("non_afm_vlans", []) or [])}
    return {v for ns in sw.namespaces() for v in sw.vlans(ns)} - skip

def _all_computes_ready(ops, checks, timeout=None):
    names = [c["meta"]["name"] for c in ops.afm.computes()]
    for n in names:
        checks.check(ops.wait_compute_ready(n, timeout), f"{n} not admitted/healthy/GPUs Ready")


def _readmit_compute(ops, name: str, slot: int, entries: list[dict], sysv: str, checks):
    """Sideband readmit after DELETE: restart afm-agent on the switch hosting the slot, wait ready, re-add GPUs."""
    sw, _ = ops.slot_host(slot)
    if sw is not None:
        ops.afm_agent(sw, "restart")
    ok = ops.wait_compute_ready(name)
    checks.check(ok, f"{name} not re-admitted within {ops.th('readmit_timeout_s', 300)}s (AIFM-1512: 124-134 s seen)")
    if ok and entries:
        ops.add_gpus(sysv, entries)


# ============================================================ compute agent (sideband sb-node-agent)

@pytest.mark.tc("TC-AFM-006")
def test_compute_agent_restart(ops, afm_ready, checks, record_property):
    """Restart the compute node agent of every compute and verify re-admission. In sideband mode each compute's
    agent (sb-node-agent) runs on a switch, inside its afm-agent container, so: restart afm-agent on each switch that
    hosts node agents, one switch at a time; after each, every compute whose agent it hosted must get its agent back
    (new PID, it may move to another switch), be re-admitted healthy with GPUs Ready, and its agent REST must report
    connected. Computes not ready before the test (e.g. a tray that is down) are recorded and not judged.
    thresholds.afm.agent_restart_switches limits the switches (comma-separated names) for a shorter run."""
    _need_sideband(ops)
    slot_name = {(c.get("spec") or {}).get("slot-id"): c["meta"]["name"] for c in ops.afm.computes()}
    ready_before = {n for n in slot_name.values() if ops.compute_ready(n)}
    placement = {sw: ops.sb_agents(sw) for sw in ops.rack.switches.values()}
    only = {x.strip() for x in str(ops.th("agent_restart_switches", "") or "").split(",") if x.strip()}
    hosts = [sw for sw in sorted(placement, key=lambda s: s.name) if placement[sw] and (not only or sw.name in only)]
    checks.check(hosts, "no switch hosts an sb-node-agent")
    record_property("placement_before", {sw.name: sorted(placement[sw]) for sw in placement})
    record_property("not_judged", sorted(set(slot_name.values()) - ready_before))
    done = {}
    for sw in hosts:
        before = dict(placement[sw])               # {slot: pid} this switch hosted
        ops.afm_agent(sw, "restart")

        def respawned():
            now = {slot: ops.slot_host(slot) for slot in before}
            return all(h[1] and (h[1] != before[slot] or h[0] is not sw) for slot, h in now.items()) and now

        now = ops.wait(respawned, float(ops.th("agent_respawn_s", 480)), 5, f"node agents of {sw.name} respawned")
        res = {}
        for slot, pid in sorted(before.items()):
            name = slot_name.get(slot, f"slot {slot}")
            new_sw, new_pid = (now or {}).get(slot) or ops.slot_host(slot)
            res[name] = f"{sw.name}/{pid} -> {getattr(new_sw, 'name', None)}/{new_pid}"
            checks.check(new_pid and (new_pid != pid or new_sw is not sw),
                         f"{name} (slot {slot}): sb-node-agent did not restart after afm-agent restart on {sw.name} "
                         f"(PID {pid})")
            if name not in ready_before:
                continue                           # not ready before the test: recorded, not judged
            checks.check(ops.wait_compute_ready(name), f"{name}: not admitted+healthy after afm-agent restart on {sw.name}")
            if new_sw is not None:
                h = ops.wait(lambda: (new_sw.agent_api(13000 + slot, "/api/health") or {}).get("is-connected") is True,
                             120, 10, f"{name} agent connected")
                checks.check(h, f"{name} (slot {slot}): agent /api/health not is-connected on {new_sw.name}")
        done[sw.name] = res
    record_property("restarts", done)
    record_property("computes_checked", sorted(n for r in done.values() for n in r if n in ready_before))
    checks.assert_all()


@pytest.mark.tc("TC-AFM-007")
def test_compute_agent_stop_and_health(ops, afm_ready, checks, record_property):
    """SIGSTOP the compute's sb-node-agent: AFM marks the compute not healthy (unknown); SIGCONT: healthy again
    and a computenode_healthy event is raised after the resume."""
    _need_sideband(ops)
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    sw, pid = ops.slot_host(slot)
    if sw is None:
        pytest.fail(f"no sb-node-agent for slot {slot}")
    t0 = time.monotonic()
    try:
        ops.signal_sb_agent(sw, pid, "STOP")
        down = ops.wait(lambda: not conditions_ok(ops.compute(name)), float(ops.th("agent_down_detect_s", 600)), 20,
                        f"{name} unhealthy")
        record_property("detect_s", round(time.monotonic() - t0, 1))
        checks.check(down, f"{name} still healthy {ops.th('agent_down_detect_s', 600)}s after SIGSTOP")
        since = ops.controller_now()
    finally:
        sw2, pid2 = ops.slot_host(slot)
        ops.signal_sb_agent(sw2 or sw, pid2 or pid, "CONT")
    checks.check(ops.wait_compute_ready(name, 180), f"{name} not healthy within 180s of SIGCONT")
    ev = ops.wait(lambda: [e for e in ops.events("computenode_healthy", since) if name in str(e)], 120, 10)
    checks.check(ev, f"no computenode_healthy event for {name} after resume")
    checks.assert_all()


# ============================================================ switch agent / controller

@pytest.mark.tc("TC-AFM-018")
def test_switch_agent_restart(ops, afm_ready, checks):
    """Restart afm-agent on every switch: switches admitted+healthy, one sb-node-agent per admitted compute
    (unique slots), computes healthy, system vPOD back to online/degraded (a 1-2 min configurationfailed
    transient is tolerated)."""
    for sw in ops.rack.switches.values():
        ops.afm_agent(sw, "restart")
    for s in ops.afm.switches():
        n = s["meta"]["name"]
        checks.check(ops.wait(lambda n=n: ops.switch_ready(n), 300, 10), f"{n} not admitted+healthy")
    computes = {c["spec"]["slot-id"] for c in ops.active_computes()}   # a quarantined tray has no agent to bring back

    def slots_ok():
        seen = [slot for sw in ops.rack.switches.values() for slot in ops.sb_agents(sw)]
        return len(seen) == len(set(seen)) and set(seen) >= computes
    if computes:
        checks.check(ops.wait(slots_ok, 480, 15), "sb-node-agent slots not unique/complete after restart")
    _all_computes_ready(ops, checks)
    st = ops.wait_vpod(ops.system_vpod_name(), timeout=600)
    checks.check(st in TERMINAL, f"system vPOD {st} 600s after the agent restart")
    checks.assert_all()


@pytest.mark.tc("TC-AFM-019")
def test_switch_agent_stop_start(ops, afm_ready, checks, record_property):
    """Stop afm-agent on one switch: AFM marks it not healthy and raises switchnode_unreachable; start it:
    switch healthy, computes healthy, vPOD online/degraded. Never Reconcile to force recovery (AIFM-1392)."""
    target = ops.pick_switch(controller=None if len(ops.afm.switches()) == 1 else False)
    name = target["meta"]["name"]
    sw = ops.ssh_switch(target)
    since = ops.controller_now()
    t0 = time.monotonic()
    try:
        ops.afm_agent(sw, "stop")
        down = ops.wait(lambda: not conditions_ok(ops.switch(name)), float(ops.th("agent_down_detect_s", 600)), 20)
        record_property("detect_s", round(time.monotonic() - t0, 1))
        checks.check(down, f"{name} still healthy after afm-agent stop")
        ev = ops.wait(lambda: [e for e in ops.events("switchnode_unreachable", since) if name in str(e)], 120, 10)
        checks.check(ev, f"no switchnode_unreachable event for {name}")
    finally:
        ops.afm_agent(sw, "start")
    checks.check(ops.wait(lambda: ops.switch_ready(name), 300, 10), f"{name} not healthy after afm-agent start")
    _all_computes_ready(ops, checks)
    st = ops.wait_vpod(ops.system_vpod_name(), timeout=600)
    checks.check(st in TERMINAL, f"system vPOD {st} after agent start")
    checks.assert_all()


@pytest.mark.tc("TC-AFM-020")
def test_switch_agent_container_restart(ops, afm_ready, checks):
    """docker restart afm-agent on one switch: its sb-node-agents respawn (polled, not a single check), switch and
    computes healthy, vPOD online/degraded."""
    target = ops.pick_switch(controller=None if len(ops.afm.switches()) == 1 else False)
    sw = ops.ssh_switch(target)
    before = set(ops.sb_agents(sw))
    sw.run("docker restart afm-agent", sudo=True, timeout=300)
    if before:
        ok = ops.wait(lambda: set().union(*(ops.sb_agents(s) for s in ops.rack.switches.values())) >= before, 480, 10)
        checks.check(ok, f"sb-node-agent slots {sorted(before)} not all back after the container restart")
    checks.check(ops.wait(lambda: ops.switch_ready(target["meta"]["name"]), 300, 10), "switch not healthy")
    _all_computes_ready(ops, checks)
    checks.check(ops.wait_vpod(ops.system_vpod_name(), timeout=600) in TERMINAL, "system vPOD not online/degraded")
    checks.assert_all()


def _cluster_ok(ops, n_members):
    st = ops.afm.cluster().get("status") or {}
    members = (st.get("quorum-status") or {}).get("members") or []
    return conditions_ok(ops.afm.cluster()) and st.get("leader") and len(members) == n_members and \
        all(m.get("status") == "started" for m in members)


@pytest.mark.tc("TC-AFM-021")
def test_non_leader_controller_restart(ops, afm_ready, checks, record_property):
    """docker restart afm-controller on a non-leader quorum member: cluster healthy again with all members
    started, leader unchanged."""
    members, leader = ops.quorum_ips(), ops.leader_ip()
    if len(members) < 2:
        pytest.skip(f"{len(members)} controller(s): no non-leader to restart")
    target = next(m for m in members if m != leader)
    ops.restart_controller(ops.ssh_by_ip(target))
    ok = ops.wait(lambda: _cluster_ok(ops, len(members)), 180, 10)
    record_property("leader", {"before": leader, "after": ops.leader_ip()})
    checks.check(ok, f"cluster not healthy with {len(members)} started members 180s after restarting {target}")
    checks.check(ops.leader_ip() == leader, f"leader changed {leader} -> {ops.leader_ip()} on a non-leader restart")
    checks.assert_all()


@pytest.mark.tc("TC-AFM-022")
def test_leader_controller_restart(ops, afm_ready, checks, record_property):
    """docker restart afm-controller on the leader: a new leader is elected (~41 s on the full rack), all
    members started, API answering."""
    members, leader = ops.quorum_ips(), ops.leader_ip()
    if len(members) < 2:
        pytest.skip(f"{len(members)} controller(s): restarting the only controller is an outage, not a failover")
    ops.restart_controller(ops.ssh_by_ip(leader))
    t0 = time.monotonic()
    ok = ops.wait(lambda: _cluster_ok(ops, len(members)) and ops.leader_ip() not in (None, ""), 180, 5)
    record_property("leader", {"before": leader, "after": ops.leader_ip(), "s": round(time.monotonic() - t0, 1)})
    checks.check(ok, "cluster not healthy 180s after restarting the leader controller")
    checks.assert_all()


# ============================================================ vPOD CRUD / agent down

def _test_computes(ops) -> list[str]:
    n = ops.th("vpod_test_computes", 1)
    names = [c["meta"]["name"] for c in sorted(ops.afm.computes(), key=lambda c: c["spec"].get("slot-id", 0))
             if ops.compute_ready(c["meta"]["name"])]
    return names if n == "all" else names[-int(n):]


@pytest.mark.tc("TC-AFM-010")
def test_vpod_crud(ops, afm_ready, uniq, cleanup, checks, record_property):
    """Move thresholds.afm.vpod_test_computes (1) computes out of the system vPOD into a new user vPOD
    (online/degraded, membership per GPU), switch its failover policy, delete it, put the GPUs back:
    system vPOD returns to baseline planes."""
    sysv, base = _baseline(ops, checks, record_property)
    names = _test_computes(ops)
    user = uniq("vp")
    moved = []
    try:
        for n in names:
            moved += ops.remove_compute(sysv, n)
        ops.create_vpod(user, moved)
        st = ops.wait_vpod(user)
        checks.check(st in TERMINAL, f"user vPOD {user} state {st}")
        for n in names:
            checks.check(ops.membership(n) == {user}, f"{n} membership {ops.membership(n)}, expected {user}")
        pol = uniq("fp")
        cleanup(f"{FP}/{pol}")
        ops.afm.post(FP, {"meta": {"name": pol}, "spec": {"datafabric-reconfig": "automatic", "link-fail-threshold": 30}})
        v = ops.vpod(user)
        v["spec"]["failover-policy"] = pol
        ops.afm.put(f"{CLUSTER}/vpods/{user}", {"kind": "VPod", "api-version": "v1", "meta": v["meta"], "spec": v["spec"]})
        checks.check(ops.vpod(user)["spec"].get("failover-policy") == pol, "failover-policy update not applied")
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} not online/degraded after policy change")
        checks.check(ops.delete_vpod(user), f"{user} still present after delete")
        for n in names:
            checks.check(ops.membership(n) <= {""}, f"{n} still member of {ops.membership(n)} after delete")
    finally:
        ops.delete_vpod(user)
        if moved:
            ops.add_gpus(sysv, moved)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded after re-add")
    for n in names:
        checks.check(ops.wait(lambda n=n: ops.membership(n) == {sysv}, 120, 10), f"{n} not back in {sysv}")
    _planes_restored(ops, checks, sysv, base)
    checks.assert_all()


def _agents(ops, action):
    for sw in ops.rack.switches.values():
        ops.afm_agent(sw, action)


@pytest.mark.tc("TC-AFM-036")
def test_vpod_create_configfailed_agent_down(ops, afm_ready, uniq, checks, record_property):
    """afm-agent stopped on every switch: a user vPOD created with the test computes goes configurationfailed
    (60 s); after the agents start it reaches online/degraded (thresholds.afm.agent_recover_s, 300 s)."""
    sysv, base = _baseline(ops, checks, record_property)
    names = _test_computes(ops)
    user, moved = uniq("vp"), []
    try:
        for n in names:
            moved += ops.remove_compute(sysv, n)
        _agents(ops, "stop")
        time.sleep(float(ops.th("agent_settle_s", 30)))
        ops.create_vpod(user, moved)
        st = ops.wait_vpod(user, ("configurationfailed",), 60)
        checks.check(st == "configurationfailed", f"{user} state {st} with agents down, expected configurationfailed")
        _agents(ops, "start")
        st = ops.wait_vpod(user, timeout=float(ops.th("agent_recover_s", 300)))
        checks.check(st in TERMINAL, f"{user} state {st} after agents started")
    finally:
        _agents(ops, "start")
        ops.delete_vpod(user)
        if moved:
            ops.add_gpus(sysv, moved)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded after restore")
    _planes_restored(ops, checks, sysv, base)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-037")
def test_vpod_delete_clears_static_mac_agent_down(ops, afm_ready, uniq, checks, record_property):
    """User vPOD online, agents stopped, vPOD deleted, agents started: no static MAC is left on any switch ASIC."""
    sysv, base = _baseline(ops, checks, record_property)
    names = _test_computes(ops)
    user, moved = uniq("vp"), []
    try:
        for n in names:
            moved += ops.remove_compute(sysv, n)
        ops.create_vpod(user, moved)
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} not online/degraded")
        _agents(ops, "stop")
        time.sleep(float(ops.th("agent_settle_s", 30)))
        checks.check(ops.delete_vpod(user), f"{user} not deleted with agents down")
        _agents(ops, "start")
        for s in ops.afm.switches():
            checks.check(ops.wait(lambda n=s["meta"]["name"]: ops.switch_ready(n), 300, 10), f"{s['meta']['name']} not healthy")
        left = {sw.name: ops.static_macs(sw) for sw in ops.rack.switches.values()}
        record_property("static_macs_after", left)
        # the system vPOD still owns its MACs; only count when every compute was moved out
        if ops.th("vpod_test_computes", 1) == "all":
            checks.check(not any(left.values()), f"static MACs left after vPOD delete: {left}")
    finally:
        _agents(ops, "start")
        ops.delete_vpod(user)
        if moved:
            ops.add_gpus(sysv, moved)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded after restore")
    _planes_restored(ops, checks, sysv, base)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-038")
def test_vpod_reconcile_fails_agent_down(ops, afm_ready, uniq, checks, record_property):
    """User vPOD with agents stopped: Reconcile answers 200 but the vPOD stays configurationfailed; with agents
    started, Reconcile recovers it to online/degraded within 120 s."""
    sysv, base = _baseline(ops, checks, record_property)
    names = _test_computes(ops)
    user, moved = uniq("vp"), []
    try:
        for n in names:
            moved += ops.remove_compute(sysv, n)
        _agents(ops, "stop")
        time.sleep(float(ops.th("agent_settle_s", 30)))
        ops.create_vpod(user, moved)
        ops.wait_vpod(user, ("configurationfailed",), 60)
        r = ops.afm.raw("POST", f"{CLUSTER}/vpods/{user}/Reconcile", {})
        checks.check(r.status_code == 200, f"Reconcile with agents down -> HTTP {r.status_code}")
        time.sleep(float(ops.th("agent_settle_s", 30)))
        checks.check(ops.vpod_state(user) == "configurationfailed",
                     f"{user} {ops.vpod_state(user)} after Reconcile with agents down, expected configurationfailed")
        _agents(ops, "start")
        for s in ops.afm.switches():
            ops.wait(lambda n=s["meta"]["name"]: ops.switch_ready(n), 300, 10)
        ops.afm.raw("POST", f"{CLUSTER}/vpods/{user}/Reconcile", {})
        st = ops.wait_vpod(user, timeout=float(ops.th("agent_recover_s", 300)))
        checks.check(st in TERMINAL, f"{user} {st} after Reconcile with agents running")
    finally:
        _agents(ops, "start")
        ops.delete_vpod(user)
        if moved:
            ops.add_gpus(sysv, moved)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded after restore")
    _planes_restored(ops, checks, sysv, base)
    checks.assert_all()


# ============================================================ compute lifecycle

def _out_of_vpod(ops, sysv, name):
    return ops.remove_compute(sysv, name)


@pytest.mark.tc("TC-AFM-047")
def test_failed_decommission_in_vpod(ops, afm_ready, checks, record_property):
    """Decommission and force-decommission of a compute whose GPUs are in a vPOD -> 412 'GPUs are part of
    vPods'; the compute stays admitted."""
    name = ops.pick_compute()
    got = {}
    for label, body in (("decommission", {}), ("force", {"force-decommission": True})):
        r = ops.afm.raw("POST", f"{CN}/{name}/Decommission", body)
        got[label] = f"HTTP {r.status_code} {r.text[:150]}"
        checks.check(r.status_code == 412, f"{label} of in-vPOD {name} -> HTTP {r.status_code}, expected 412")
    record_property("responses", got)
    checks.check(ops.compute_phase(name) == "admitted", f"{name} now {ops.compute_phase(name)}")
    checks.assert_all()


def _decommission_cycle(ops, checks, record_property, *, force: bool, between=None):
    """Remove from vPOD -> Decommission -> decommissioned -> [between()] -> DELETE (404) -> readmit -> re-add."""
    _need_sideband(ops)
    sysv, base = _baseline(ops, checks, record_property)
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    try:
        r = ops.afm.raw("DELETE", f"{CN}/{name}")
        checks.check(r.status_code == 412, f"DELETE of admitted {name} -> HTTP {r.status_code}, expected 412")
        r = ops.afm.raw("POST", f"{CN}/{name}/Decommission", {"force-decommission": True} if force else {})
        checks.check(r.status_code == 200, f"Decommission -> HTTP {r.status_code}: {r.text[:200]}")
        ok = ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
        checks.check(ok, f"{name} {ops.compute_phase(name)} 300s after Decommission")
        if between:
            between(name)
        r = ops.afm.raw("DELETE", f"{CN}/{name}")
        checks.check(r.status_code in (200, 204), f"DELETE decommissioned {name} -> HTTP {r.status_code}")
        checks.check(ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5), f"{name} still listed after DELETE")
    finally:
        if ops.compute_phase(name) != "admitted":
            if ops.compute_phase(name) != "absent":
                ops.afm.raw("DELETE", f"{CN}/{name}")
            _readmit_compute(ops, name, slot, entries, sysv, checks)
        else:
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded after readmit")
    checks.check(ops.wait(lambda: ops.membership(name) == {sysv}, 180, 10), f"{name} not back in {sysv}")
    _planes_restored(ops, checks, sysv, base)


@pytest.mark.tc("TC-AFM-045")
def test_compute_node_life_cycle(ops, afm_ready, checks, record_property):
    """Remove from vPOD, DELETE admitted -> 412, Decommission -> decommissioned, DELETE -> gone, readmit via
    afm-agent restart (allow thresholds.afm.readmit_timeout_s, 300 s), back in the system vPOD."""
    _decommission_cycle(ops, checks, record_property, force=False)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-046")
def test_forced_decommission_compute(ops, afm_ready, checks, record_property):
    """As TC-AFM-045 with force-decommission."""
    _decommission_cycle(ops, checks, record_property, force=True)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-RC-019")
def test_reconfigure_decommissioned(ops, afm_ready, checks, record_property):
    """Reconfigure of a decommissioned compute -> 412 (then deleted and readmitted)."""
    def probe(name):
        r = ops.reconfigure(name, retry_412_s=30)
        record_property("reconfigure", f"HTTP {r.status_code} {r.text[:150]}")
        checks.check(r.status_code == 412, f"Reconfigure decommissioned {name} -> HTTP {r.status_code}, expected 412")
    _decommission_cycle(ops, checks, record_property, force=False, between=probe)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-RC-020")
def test_reconfigure_during_decommissioning(ops, afm_ready, checks, record_property):
    """Reconfigure immediately after Decommission (while decommissioning) is rejected (non-200)."""
    _need_sideband(ops)
    sysv, base = _baseline(ops, checks, record_property)
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    try:
        r1 = ops.afm.raw("POST", f"{CN}/{name}/Decommission", {})
        r2 = ops.reconfigure(name, retry_412_s=30)  # past an 'in progress' guard left by an earlier Reconfigure
        record_property("responses", [r1.status_code, f"{r2.status_code} {r2.text[:150]}"])
        checks.check(r1.status_code == 200, f"Decommission -> HTTP {r1.status_code}")
        checks.check(r2.status_code != 200, "Reconfigure during decommissioning accepted (200)")
        ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
    finally:
        if ops.compute_phase(name) != "admitted":
            ops.afm.raw("DELETE", f"{CN}/{name}")
            _readmit_compute(ops, name, slot, entries, sysv, checks)
        else:
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    _planes_restored(ops, checks, sysv, base)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-RC-005")
def test_reconfigure_then_add_to_vpod(ops, afm_ready, uniq, checks, record_property):
    """Compute out of the system vPOD, Reconfigure it (200, GPUs Ready), then add it to a user vPOD: accepted
    (400 'configuration failed state' retried for up to 180 s) and online/degraded."""
    sysv, base = _baseline(ops, checks, record_property)
    name = ops.pick_compute()
    user, entries = uniq("vp"), []
    try:
        entries = ops.remove_compute(sysv, name)
        r = ops.reconfigure(name)
        checks.check(r.status_code == 200, f"Reconfigure -> HTTP {r.status_code}: {r.text[:200]}")
        checks.check(ops.wait_compute_ready(name, 180), f"{name} GPUs not Ready after Reconfigure")
        ops.create_vpod(user, entries)
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} not online/degraded")
        checks.check(ops.membership(name) == {user}, f"{name} membership {ops.membership(name)}")
    finally:
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    _planes_restored(ops, checks, sysv, base)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-RC-006")
def test_reconfigure_then_remove_from_vpod(ops, afm_ready, checks, record_property):
    """Reconfigure a compute in the system vPOD, then remove it: every GPU's membership clears (checked per GPU,
    not a non-existent status.vpod-membership); re-added afterwards."""
    sysv, base = _baseline(ops, checks, record_property)
    name = ops.pick_compute()
    entries = []
    try:
        r = ops.reconfigure(name)
        checks.check(r.status_code == 200, f"Reconfigure -> HTTP {r.status_code}")
        checks.check(ops.wait_compute_ready(name, 180), f"{name} not Ready after Reconfigure")
        entries = ops.remove_compute(sysv, name)
        checks.check(ops.membership(name) <= {""}, f"{name} still member: {ops.membership(name)}")
        checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded without {name}")
    finally:
        if entries:
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait(lambda: ops.membership(name) == {sysv}, 180, 10), f"{name} not back in {sysv}")
    _planes_restored(ops, checks, sysv, base)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-RC-017")
def test_reconfigure_unhealthy_compute(ops, afm_ready, checks, record_property):
    """sb-node-agent SIGSTOPped until AFM marks the compute unhealthy: Reconfigure is accepted (200, as the plan
    expects); after SIGCONT the compute is healthy again. g02-g17: 12 Oct AFM answered 412 'does not have scaleup
    info' and the test was bent to expect 4xx; 17 Oct AFM answered 200 = the plan's expectation."""
    _need_sideband(ops)
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    sw, pid = ops.slot_host(slot)
    try:
        ops.signal_sb_agent(sw, pid, "STOP")
        checks.check(ops.wait(lambda: not conditions_ok(ops.compute(name)), float(ops.th("agent_down_detect_s", 600)), 20),
                     f"{name} never went unhealthy")
        r = ops.reconfigure(name)  # retries the 'already in progress' 412 guard; a real rejection is final
        record_property("reconfigure", f"HTTP {r.status_code} {r.text[:200]}")
        checks.check(r.status_code == 200, f"Reconfigure on unhealthy {name} -> HTTP {r.status_code} {r.text[:160]}, "
                                           "plan expects 200")
    finally:
        sw2, pid2 = ops.slot_host(slot)
        ops.signal_sb_agent(sw2 or sw, pid2 or pid, "CONT")
    checks.check(ops.wait_compute_ready(name, 300), f"{name} not healthy after SIGCONT")
    checks.assert_all()


@pytest.mark.tc("TC-AFM-RC-026")
def test_reconfigure_degraded_vpod(ops, afm_ready, checks, record_property):
    """With the system vPOD degraded (some planes down), Reconfigure a member compute: 200, GPUs Ready, vPOD
    back to degraded/online with available planes not below the baseline."""
    sysv, base = _baseline(ops, checks, record_property)
    if ops.vpod_state(sysv) != "degraded":
        pytest.skip(f"{sysv} is {ops.vpod_state(sysv)}, not degraded")
    name = ops.pick_compute()
    r = ops.reconfigure(name)
    checks.check(r.status_code == 200, f"Reconfigure -> HTTP {r.status_code}: {r.text[:200]}")
    checks.check(ops.wait_compute_ready(name, 180), f"{name} not Ready")
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not degraded/online")
    _planes_restored(ops, checks, sysv, base, 300)
    checks.assert_all()


# ============================================================ switch lifecycle (>= 2 switches)

def _readmit_switch(ops, name: str, sw, checks):
    ops.reset_switch_identity(sw)
    checks.check(ops.wait(lambda: ops.switch_ready(name), float(ops.th("switch_readmit_s", 600)), 15),
                 f"{name} not re-admitted+healthy after agent identity reset")


def _switch_recover(ops, name, sw, checks):
    """Bring a switch back to admitted whatever state the test left it in."""
    ph = ops.switch_phase(name)
    if ph == "drained":
        ops.afm.raw("POST", f"{SN}/{name}/ReactivateSwitchNode", {})
    elif ph in ("decommissioned", "decommission_failed"):
        ops.afm.raw("DELETE", f"{SN}/{name}")
        _readmit_switch(ops, name, sw, checks)
    elif ph == "absent":
        _readmit_switch(ops, name, sw, checks)
    ops.wait(lambda: ops.switch_ready(name), 600, 15)


def _switch_case(ops, checks, record_property, steps, *, controller=False):
    _need_switches(ops.afm, 2)
    sysv, base = _baseline(ops, checks, record_property)
    target = ops.pick_switch(controller=controller)
    name = target["meta"]["name"]
    sw = ops.ssh_switch(target)
    vlans0 = ops.vlan_count(sw)
    record_property("target", {"switch": name, "vlans": vlans0})
    try:
        steps(name, sw)
    finally:
        _switch_recover(ops, name, sw, checks)
        for s in ops.rack.switches.values():
            ops.afm_agent(s, "start")
    checks.check(ops.switch_ready(name), f"{name} not admitted+healthy at the end")
    checks.check(ops.wait(lambda: ops.vlan_count(sw) >= vlans0, 300, 15),
                 f"{name}: {ops.vlan_count(sw)} VLANs, {vlans0} before (AIFM-1901 VLAN wipe?)")
    _all_computes_ready(ops, checks)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    _planes_restored(ops, checks, sysv, base)


def _post(ops, name, action, body=None):
    return ops.afm.raw("POST", f"{SN}/{name}/{action}", {} if body is None else body)


def _drain(ops, checks, name):
    r = _post(ops, name, "DrainSwitchNode")
    checks.check(r.status_code == 200, f"Drain -> HTTP {r.status_code}: {r.text[:150]}")
    checks.check(ops.wait_switch_phase(name, "drained", 900), f"{name} {ops.switch_phase(name)}, expected drained")


def _decommission(ops, checks, name, force=False, expect="decommissioned"):
    r = _post(ops, name, "DecommissionSwitchNode", {"force-decommission": True} if force else {})
    checks.check(r.status_code == 200, f"Decommission(force={force}) -> HTTP {r.status_code}: {r.text[:150]}")
    checks.check(ops.wait_switch_phase(name, expect, 300), f"{name} {ops.switch_phase(name)}, expected {expect}")


def _delete(ops, checks, name):
    r = ops.afm.raw("DELETE", f"{SN}/{name}")
    checks.check(r.status_code in (200, 204), f"DELETE switch -> HTTP {r.status_code}")
    checks.check(ops.wait(lambda: ops.switch_phase(name) == "absent", 60, 5), f"{name} still listed")


def _restart_leader(ops):
    ops.restart_controller(ops.ssh_by_ip(ops.leader_ip()))
    ops.wait(lambda: conditions_ok(ops.afm.cluster()) and ops.leader_ip(), 180, 5)


@pytest.mark.tc("TC-AFM-023")
def test_switch_drain_reactivate(ops, afm_ready, checks, record_property):
    """Drain a non-controller switch: drained, available planes drop; Reactivate: admitted+healthy, VLANs and
    planes restored."""
    def steps(name, sw):
        sysv = ops.system_vpod_name()
        before = ops.planes(sysv)
        _drain(ops, checks, name)
        checks.check(ops.wait(lambda: (ops.planes(sysv)[1] or 0) < (before[1] or 0), 120, 10),
                     f"available planes did not drop on drain ({before} -> {ops.planes(sysv)})")
        r = _post(ops, name, "ReactivateSwitchNode")
        checks.check(r.status_code == 200, f"Reactivate -> HTTP {r.status_code}")
        checks.check(ops.wait(lambda: ops.switch_ready(name), 600, 15), f"{name} not admitted+healthy after reactivate")
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-024")
def test_switch_decommission_and_delete(ops, afm_ready, checks, record_property):
    """Drain -> decommission: switch has 0 VLANs and 0 static MACs; DELETE -> 404; readmit (agent identity
    reset); planes restored (full rack: needed a vPOD Reconcile - product issue)."""
    def steps(name, sw):
        _drain(ops, checks, name)
        _decommission(ops, checks, name)
        checks.check(ops.wait(lambda: not _afm_vlans(ops, sw), 120, 10),
                     f"{name}: AFM VLANs left after decommission: {sorted(_afm_vlans(ops, sw))}")
        checks.check(ops.static_macs(sw) == 0, f"{name}: static MACs left after decommission")
        _delete(ops, checks, name)
        _readmit_switch(ops, name, sw, checks)
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-025")
def test_switch_force_decommission(ops, afm_ready, checks, record_property):
    """Force-decommission without draining -> decommissioned; DELETE; readmit; planes restored."""
    def steps(name, sw):
        _decommission(ops, checks, name, force=True)
        _delete(ops, checks, name)
        _readmit_switch(ops, name, sw, checks)
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-026")
def test_switch_decommission_vpod_planes(ops, afm_ready, checks, record_property):
    """Decommission a switch: the system vPOD's available planes drop by that switch's planes; after delete +
    readmit they return to the baseline without a manual Reconcile."""
    def steps(name, sw):
        sysv = ops.system_vpod_name()
        before = ops.planes(sysv)
        _drain(ops, checks, name)
        _decommission(ops, checks, name)
        after = ops.settled_planes(sysv, interval=float(ops.th("plane_settle_interval_s", 10)))
        record_property("planes", {"before": before, "decommissioned": after})
        checks.check((after[1] or 0) < (before[1] or 0), f"planes did not drop: {before} -> {after}")
        _delete(ops, checks, name)
        _readmit_switch(ops, name, sw, checks)
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-027")
def test_switch_drain_restart_controller(ops, afm_ready, checks, record_property):
    """Drain, restart the leader controller, Reactivate: switch healthy AND its VLAN programming intact
    (AIFM-1901: VLANs wiped while AFM reported healthy)."""
    def steps(name, sw):
        _drain(ops, checks, name)
        _restart_leader(ops)
        checks.check(ops.switch_phase(name) == "drained", f"{name} {ops.switch_phase(name)} after controller restart")
        _post(ops, name, "ReactivateSwitchNode")
        checks.check(ops.wait(lambda: ops.switch_ready(name), 600, 15), f"{name} not healthy after reactivate")
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-028")
def test_switch_decommission_restart_controller(ops, afm_ready, checks, record_property):
    """Decommission, restart the leader controller: still decommissioned; DELETE; readmit."""
    def steps(name, sw):
        _drain(ops, checks, name)
        _decommission(ops, checks, name)
        _restart_leader(ops)
        checks.check(ops.switch_phase(name) == "decommissioned", f"{name} {ops.switch_phase(name)} after restart")
        _delete(ops, checks, name)
        _readmit_switch(ops, name, sw, checks)
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-029")
def test_switch_delete_restart_controller(ops, afm_ready, checks, record_property):
    """Delete a decommissioned switch, restart the leader: the switch is NOT re-added on its own (120 s);
    readmit via identity reset."""
    def steps(name, sw):
        _decommission(ops, checks, name, force=True)
        _delete(ops, checks, name)
        _restart_leader(ops)
        time.sleep(float(ops.th("absent_hold_s", 120)))
        checks.check(ops.switch_phase(name) == "absent", f"{name} re-appeared ({ops.switch_phase(name)}) after restart")
        _readmit_switch(ops, name, sw, checks)
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-030")
def test_switch_force_decommission_restart_controller(ops, afm_ready, checks, record_property):
    """Force-decommission, restart the leader: still decommissioned; DELETE; readmit."""
    def steps(name, sw):
        _decommission(ops, checks, name, force=True)
        _restart_leader(ops)
        checks.check(ops.switch_phase(name) == "decommissioned", f"{name} {ops.switch_phase(name)} after restart")
        _delete(ops, checks, name)
        _readmit_switch(ops, name, sw, checks)
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-031")
def test_switch_drain_agent_down(ops, afm_ready, checks, record_property):
    """Drain with the target's afm-agent stopped: no 5xx; the result is recorded (full rack: drain succeeded,
    contrary to the plan title); after the agent starts, Reactivate restores the switch."""
    def steps(name, sw):
        ops.afm_agent(sw, "stop")
        ops.wait(lambda: not conditions_ok(ops.switch(name)), float(ops.th("agent_down_detect_s", 600)), 20)
        r = _post(ops, name, "DrainSwitchNode")
        time.sleep(float(ops.th("agent_settle_s", 30)))
        record_property("drain", {"http": r.status_code, "phase": ops.switch_phase(name)})
        checks.check(r.status_code < 500, f"Drain with agent down -> HTTP {r.status_code}")
        ops.afm_agent(sw, "start")
        if ops.switch_phase(name) == "drained":
            _post(ops, name, "ReactivateSwitchNode")
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-032")
def test_switch_decommission_agent_down(ops, afm_ready, checks, record_property):
    """Drain, stop the agent, Decommission -> decommission_failed (not decommissioned); force-decommission ->
    decommissioned; agent started, DELETE, readmit (the plan's order: g02-g17 17 Oct 23:47 the test skipped the drain
    and AFM refused the Decommission with 412 'not allowed ... in admission phase admitted')."""
    def steps(name, sw):
        _drain(ops, checks, name)
        ops.afm_agent(sw, "stop")
        ops.wait(lambda: not conditions_ok(ops.switch(name)), float(ops.th("agent_down_detect_s", 600)), 20)
        _post(ops, name, "DecommissionSwitchNode")
        ph = ops.wait(lambda: (lambda p: p if p in ("decommission_failed", "decommissioned") else None)(ops.switch_phase(name)), 300, 10)
        record_property("phase_with_agent_down", ph)
        checks.check(ph == "decommission_failed", f"{name} {ph} after Decommission with agent down")
        _decommission(ops, checks, name, force=True)
        ops.afm_agent(sw, "start")
        _delete(ops, checks, name)
        _readmit_switch(ops, name, sw, checks)
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-033")
def test_switch_delete_agent_down(ops, afm_ready, checks, record_property):
    """Agent stopped, force-decommission + DELETE; agent started with identity reset -> readmitted; available
    planes restored without a manual Reconcile (full rack: stuck - product issue)."""
    def steps(name, sw):
        ops.afm_agent(sw, "stop")
        ops.wait(lambda: not conditions_ok(ops.switch(name)), float(ops.th("agent_down_detect_s", 600)), 20)
        _decommission(ops, checks, name, force=True)
        _delete(ops, checks, name)
        _readmit_switch(ops, name, sw, checks)
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-034")
def test_switch_force_decommission_agent_down(ops, afm_ready, checks, record_property):
    """Agent stopped, force-decommission -> decommissioned; agent started; DELETE; readmit."""
    def steps(name, sw):
        ops.afm_agent(sw, "stop")
        ops.wait(lambda: not conditions_ok(ops.switch(name)), float(ops.th("agent_down_detect_s", 600)), 20)
        _decommission(ops, checks, name, force=True)
        ops.afm_agent(sw, "start")
        _delete(ops, checks, name)
        _readmit_switch(ops, name, sw, checks)
    _switch_case(ops, checks, record_property, steps)
    checks.assert_all()
