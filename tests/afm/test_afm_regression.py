"""AFM bug-regression, event and agent cases (DISRUPTIVE, needs --allow-disruptive).

The plan gives titles only for these; each test states the behaviour it asserts. "Controller stability"
means: every controller container keeps its StartedAt / RestartCount (no crash or restart) and the cluster
stays healthy.
"""
from __future__ import annotations

import json
import time

import pytest

from helios.afm import CLUSTER, MONITORING, conditions_ok
from helios.afm_ops import CN, SN, TERMINAL

AP = f"{MONITORING}/alertPolicies"
VP = f"{CLUSTER}/vpods"
pytestmark = [pytest.mark.disruptive]


def _ctl_states(ops) -> dict:
    return {ip: ops.controller_state(sw) for ip, sw in ops.controllers().items()}


def _ctl_stable(ops, checks, before):
    after = _ctl_states(ops)
    for ip, st in before.items():
        checks.check(after.get(ip) == st, f"controller on {ip} restarted/crashed: {st} -> {after.get(ip)}")
    checks.check(conditions_ok(ops.afm.cluster()), "cluster not healthy")


def _need_switches(ops, n):
    if len(ops.afm.switches()) < n:
        pytest.skip(f"needs {n} switches")


def _need_sideband(ops):
    if not any(ops.sb_agents(sw) for sw in ops.rack.switches.values()):
        pytest.skip("not in sideband control mode")


def _rack_ok(ops, checks):
    for s in ops.afm.switches():
        checks.check(ops.wait(lambda n=s["meta"]["name"]: ops.switch_ready(n), 300, 10), f"{s['meta']['name']} not healthy")
    for c in ops.afm.computes():
        checks.check(ops.wait_compute_ready(c["meta"]["name"], 600), f"{c['meta']['name']} not healthy")
    checks.check(ops.wait_vpod(ops.system_vpod_name(), timeout=600) in TERMINAL, "system vPOD not online/degraded")


# ------------------------------------------------------------------ reconfigure with drained ports

@pytest.mark.l2
@pytest.mark.tc("TC-AFM-RC-028")
def test_reconfigure_with_blacklisted_ports(ops, afm_ready, checks, record_property):
    """GPU ports drained through the API (DrainGPUPort) stay drained across a Reconfigure of the tray; after
    ActivateGPUPort they are cleared."""
    name = ops.pick_compute()
    ports = [{"gpu-id": 0, "port-id": p} for p in ops.th("rc028_ports", [10, 11])]
    want = {(x["gpu-id"], x["port-id"]) for x in ports}

    def drained():
        return {(x.get("gpu-id"), x.get("port-id")) for x in (ops.compute(name).get("spec") or {}).get("drained-ports") or []}
    try:
        r = ops.afm.raw("POST", f"{CN}/{name}/DrainGPUPort", {"ports": ports})
        checks.check(r.status_code == 200, f"DrainGPUPort -> HTTP {r.status_code}")
        checks.check(ops.wait(lambda: want <= drained(), 60, 5), f"drained-ports {drained()} missing {want}")
        r = ops.reconfigure(name)
        checks.check(r.status_code == 200, f"Reconfigure -> HTTP {r.status_code}: {r.text[:150]}")
        checks.check(ops.wait_compute_ready(name, 300), f"{name} not Ready after Reconfigure")
        record_property("drained_after_reconfigure", sorted(drained()))
        checks.check(want <= drained(), f"Reconfigure un-drained ports: {sorted(want - drained())}")
    finally:
        ops.afm.raw("POST", f"{CN}/{name}/ActivateGPUPort", {"ports": ports})
    checks.check(ops.wait(lambda: not (want & drained()), 120, 10), f"ports still drained after ActivateGPUPort: {drained()}")
    checks.assert_all()


# ------------------------------------------------------------------ controller stability

@pytest.mark.l3
@pytest.mark.tc("TC-BUG-001")
def test_controller_stable_on_decommissioned_rejoin(ops, afm_ready, checks, record_property):
    """A compute is removed from its vPOD, decommissioned, deleted and readmitted (agent restart on its slot
    host): no controller container restarts and the rack ends healthy."""
    _need_sideband(ops)
    before = _ctl_states(ops)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    try:
        ops.afm.post(f"{CN}/{name}/Decommission", {})
        ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
        ops.afm.raw("DELETE", f"{CN}/{name}")
        ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5)
        sw, _ = ops.slot_host(slot)
        if sw is not None:
            ops.afm_agent(sw, "restart")
        checks.check(ops.wait_compute_ready(name), f"{name} not readmitted")
    finally:
        if ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    _ctl_stable(ops, checks, before)
    _rack_ok(ops, checks)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-BUG-004")
def test_controller_stable_switch_agent_stuck(ops, afm_ready, checks, record_property):
    """switch-agent frozen (SIGSTOP) until AFM marks the switch unhealthy, then afm-agent restarted: the switch
    is healthy again and no controller restarts."""
    before = _ctl_states(ops)
    t = ops.pick_switch(controller=None if len(ops.afm.switches()) == 1 else False)
    name, sw = t["meta"]["name"], ops.ssh_switch(t)
    pid = ops.switch_agent_pid(sw)
    if not pid:
        pytest.fail(f"no switch-agent process on {sw.name}")
    try:
        ops.signal_sb_agent(sw, pid, "STOP")
        checks.check(ops.wait(lambda: not conditions_ok(ops.switch(name)), float(ops.th("agent_down_detect_s", 600)), 20),
                     f"{name} still healthy with switch-agent frozen")
    finally:
        ops.afm_agent(sw, "restart")
    checks.check(ops.wait(lambda: ops.switch_ready(name), 300, 10), f"{name} not healthy after afm-agent restart")
    _ctl_stable(ops, checks, before)
    _rack_ok(ops, checks)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-BUG-007")
def test_switch_state_not_stuck_unknown(ops, afm_ready, checks, record_property):
    """Three afm-agent stop/start cycles on one switch: after every start the switch condition returns to
    healthy=true (never stuck at 'unknown')."""
    t = ops.pick_switch(controller=None if len(ops.afm.switches()) == 1 else False)
    name, sw = t["meta"]["name"], ops.ssh_switch(t)
    took = []
    for i in range(int(ops.th("bug007_cycles", 3))):
        try:
            ops.afm_agent(sw, "stop")
            ops.wait(lambda: not conditions_ok(ops.switch(name)), float(ops.th("agent_down_detect_s", 600)), 20)
        finally:
            ops.afm_agent(sw, "start")
        t0 = time.monotonic()
        ok = ops.wait(lambda: ops.switch_ready(name), 300, 10)
        took.append(round(time.monotonic() - t0, 1))
        checks.check(ok, f"cycle {i + 1}: {name} stuck at {(ops.switch(name).get('status') or {}).get('conditions')}")
        if not ok:
            break
    record_property("recover_s", took)
    _rack_ok(ops, checks)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-BUG-016")
def test_switch_ports_recover_after_agent_flap(ops, afm_ready, checks, record_property):
    """Connectivity flap between switch-agent and controller (afm-agent stopped for 30 s, started): the switch
    comes back healthy, its ASIC ports report PortReady (where AFM lists them) and the system vPOD planes
    return to the baseline."""
    sysv = ops.system_vpod_name()
    base = ops.settled_planes(sysv, interval=float(ops.th("plane_settle_interval_s", 10)))
    t = ops.pick_switch(controller=None if len(ops.afm.switches()) == 1 else False)
    name, sw = t["meta"]["name"], ops.ssh_switch(t)
    try:
        ops.afm_agent(sw, "stop")
        time.sleep(float(ops.th("agent_settle_s", 30)))
    finally:
        ops.afm_agent(sw, "start")
    checks.check(ops.wait(lambda: ops.switch_ready(name), 300, 10), f"{name} not healthy")
    asics = ((ops.switch(name).get("status") or {}).get("asic-info") or {}).get("asics") or []
    qslots = ops.inv.quarantined_slots() if ops.inv is not None else set()   # ports cabled to a quarantined tray
    bad = [f"asic{a.get('id')}/port{p.get('id')}:{p.get('oper-state')}" for a in asics for p in a.get("ports") or []
           if p.get("admin-status") == "up" and p.get("oper-state") not in ("PortReady", None)
           and (p.get("peer") or {}).get("slot-id") not in qslots]
    record_property("asic_ports_not_ready", bad[:20])
    checks.check(not bad, f"{len(bad)} admin-up ports not PortReady: {bad[:6]}")
    ok = ops.wait(lambda: (ops.planes(sysv)[1] or 0) >= (base[1] or 0), float(ops.th("plane_restore_s", 900)), 30)
    checks.check(ok, f"planes {ops.planes(sysv)} below baseline {base} after the flap")
    checks.assert_all()


# ------------------------------------------------------------------ vPOD / GPU correctness

@pytest.mark.l3
@pytest.mark.tc("TC-BUG-009")
def test_gpu_info_not_duplicated_after_agent_restart(ops, afm_ready, uniq, checks, record_property):
    """A compute in a user vPOD; its node agent restarted (afm-agent on the slot host): every GPU appears in
    exactly one vPOD's gpus-status and the compute's GPUs report that one vPOD."""
    _need_sideband(ops)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    user, entries = uniq("vp"), []
    try:
        entries = ops.remove_compute(sysv, name)
        ops.create_vpod(user, entries)
        ops.wait_vpod(user)
        sw, _ = ops.slot_host(slot)
        ops.afm_agent(sw, "restart")
        ops.wait_compute_ready(name, 300)
        ops.wait_vpod(user)
        seen: dict = {}
        for v in ops.afm.vpods():
            for gs in (v.get("status") or {}).get("gpus-status") or []:
                g = gs.get("gpu") or {}
                key = g.get("id") or (g.get("gpu-index") or {}).get("slot-id"), (g.get("gpu-index") or {}).get("index")
                seen.setdefault(json.dumps(key), []).append(v["meta"]["name"])
        dups = {k: v for k, v in seen.items() if len(v) > 1}
        record_property("duplicates", dups)
        checks.check(not dups, f"GPUs listed in more than one vPOD: {list(dups.items())[:4]}")
        checks.check(ops.membership(name) == {user}, f"{name} membership {ops.membership(name)}")
    finally:
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-BUG-014")
def test_accelerator_id_stable_across_vpods(ops, afm_ready, uniq, checks, record_property):
    """A compute's GPUs move system -> user vPOD -> system: every GPU keeps its acc-id and acc-ids stay unique."""
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()

    def accs(n):
        return {u: g.get("acc-id") for u, g in ops.gpu_info(ops.compute(n)).items()}
    before = accs(name)
    user, entries = uniq("vp"), []
    try:
        entries = ops.remove_compute(sysv, name)
        ops.create_vpod(user, entries)
        ops.wait_vpod(user)
        in_user = accs(name)
        ops.remove_compute(user, name)
        ops.add_gpus(sysv, entries)
        entries = []
        ops.wait_vpod(sysv)
    finally:
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)
    after = accs(name)
    record_property("acc_ids", {"before": before, "in_user_vpod": in_user, "after": after})
    checks.check(in_user == before, f"acc-ids changed when moved to {user}: {before} -> {in_user}")
    checks.check(after == before, f"acc-ids changed after moving back: {before} -> {after}")
    allacc = [g.get("acc-id") for c in ops.afm.computes() for g in ops.gpu_info(c).values()]
    checks.check(len(allacc) == len(set(allacc)), "duplicate acc-ids across the rack")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-BUG-025")
def test_gpu_failure_not_cleared_by_reconcile(ops, afm_ready, links, checks, record_property):
    """A GPU port is PortFailed (switch port down past the threshold); Reconcile of the system vPOD must not clear
    the failure while the link is still down."""
    sysv = ops.system_vpod_name()
    cname = ops.pick_compute()
    sw, ns, port, cab = ops.gpu_links(ops.compute(cname)["spec"]["slot-id"])[0]
    want = {(cname, cab[1], cab[3])}
    links.shut(sw, ns, port)
    ok = ops.wait(lambda: want <= ops.failed_gpu_ports(), ops.link_fail_threshold(sysv) + 60, 5)
    checks.check(ok, f"{port} never PortFailed")
    r = ops.afm.raw("POST", f"{VP}/{sysv}/Reconcile", {})
    checks.check(r.status_code == 200, f"Reconcile -> HTTP {r.status_code}")
    cleared = []
    end = time.monotonic() + 30 * float(ops.th("time_scale", 1))
    while time.monotonic() < end:
        if not want <= ops.failed_gpu_ports():
            cleared.append(round(end - time.monotonic(), 1))
        time.sleep(0.2)
    checks.check(not cleared, "PortFailed cleared by Reconcile while the link was still down")
    links.restore(sw, ns, port, checks)
    checks.check(ops.wait(lambda: not (want & ops.failed_gpu_ports()), 300, 10), "port still PortFailed after restore")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-BUG-026")
def test_vpod_not_stuck_operation_failed(ops, afm_ready, checks, record_property):
    """Reconcile and ReconfigureDataFabric on a healthy system vPOD: it returns to online/degraded with no
    pending ops and never reports operation-failed."""
    sysv = ops.system_vpod_name()
    states = set()
    for op in ("Reconcile", "ReconfigureDataFabric"):
        r = ops.afm.raw("POST", f"{VP}/{sysv}/{op}", {})
        checks.check(r.status_code == 200, f"{op} -> HTTP {r.status_code}")
        ok = ops.wait(lambda: (states.add(ops.vpod_state(sysv)) or ops.vpod_state(sysv)) in TERMINAL
                      and not (ops.vpod(sysv).get("status") or {}).get("pending-ops"), 300, 5)
        checks.check(ok, f"{sysv} {ops.vpod_state(sysv)} after {op}")
    record_property("states_seen", sorted(s for s in states if s))
    checks.check("operation-failed" not in states, "vPOD went operation-failed with healthy GPUs")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-BUG-027")
def test_vpod_update_not_stuck_configdone(ops, afm_ready, checks, record_property):
    """Adding a compute back to the system vPOD: the vPOD reaches online/degraded within 300 s and does not stay
    in configDone (full-rack check: != configDone within 120 s)."""
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    entries = ops.remove_compute(sysv, name)
    ops.add_gpus(sysv, entries)
    states = set()
    ok = ops.wait(lambda: (states.add(ops.vpod_state(sysv)) or ops.vpod_state(sysv)) in TERMINAL, 300, 5)
    record_property("states_seen", sorted(s for s in states if s))
    checks.check(ok, f"{sysv} {ops.vpod_state(sysv)} after adding {name} back")
    checks.check(ops.vpod_state(sysv) != "configDone", f"{sysv} stuck in configDone")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-BUG-030")
def test_switch_drain_reprograms_gpus(ops, afm_ready, checks, record_property):
    """Draining a switch reprograms the GPUs: available planes drop and a vpod_bandwidth_change event names the
    system vPOD; reactivating restores both."""
    _need_switches(ops, 2)
    sysv = ops.system_vpod_name()
    base = ops.settled_planes(sysv, interval=float(ops.th("plane_settle_interval_s", 10)))
    t = ops.pick_switch(controller=False)
    name = t["meta"]["name"]
    since = ops.controller_now()
    try:
        ops.afm.post(f"{SN}/{name}/DrainSwitchNode", {})
        checks.check(ops.wait_switch_phase(name, "drained", 900), f"{name} not drained")
        checks.check(ops.wait(lambda: (ops.planes(sysv)[1] or 0) < (base[1] or 0), 120, 10), "planes did not drop on drain")
        ev = ops.wait(lambda: [e for e in ops.events("vpod_bandwidth_change", since) if sysv in json.dumps(e)], 120, 10)
        checks.check(ev, "no vpod_bandwidth_change event for the drain")
    finally:
        ops.afm.raw("POST", f"{SN}/{name}/ReactivateSwitchNode", {})
    checks.check(ops.wait(lambda: ops.switch_ready(name), 600, 15), f"{name} not healthy after reactivate")
    checks.check(ops.wait(lambda: (ops.planes(sysv)[1] or 0) >= (base[1] or 0), float(ops.th("plane_restore_s", 900)), 30),
                 f"planes {ops.planes(sysv)} not back to {base}")
    checks.assert_all()


# ------------------------------------------------------------------ alerts / events

def _flap(ops, links, checks, sw, ns, port):
    """Shut a port, wait until it is oper down (a 0.3 s admin flap may never be seen by the switch agent), restore."""
    links.shut(sw, ns, port)
    checks.check(ops.wait_oper(sw, ns, port, "down", 60), f"{sw.name} {ns} {port} never went oper down")
    links.restore(sw, ns, port, checks)


def _policy(ops, name: str) -> dict | None:
    """The stored alert policy (None when AFM does not list it)."""
    return next((p for p in ops.afm.items(f"{MONITORING}/alertPolicies") if (p.get("meta") or {}).get("name") == name), None)


def _hits(policy: dict | None):
    """status.total-hits of an alert policy (AFM's default event policy shows one), None when AFM reports none."""
    return ((policy or {}).get("status") or {}).get("total-hits")


def _warn_policy_spec(ops) -> dict:
    """An event alert policy matching warn-severity events, built from AFM's own default event policy (which raises
    alerts on this rack - 270 open on 17 Oct, matched requirement 'equals critical') with only the matched value
    changed to warn, so its keys / format are what AFM accepts. Falls back to severity == warn."""
    import copy
    for p in ops.afm.items(f"{MONITORING}/alertPolicies"):
        sp = p.get("spec") or {}
        reqs = sp.get("requirements") or []
        if (p.get("meta") or {}).get("name", "").startswith("default-event") and any(r.get("values") == ["critical"] for r in reqs):
            out = copy.deepcopy(sp)
            out["requirements"] = [dict(r, values=["warn"]) if r.get("values") == ["critical"] else r for r in reqs]
            out["enable"] = True
            return out
    return {"resource": "Event", "enable": True, "severity": "info",
            "requirements": [{"key": "severity", "operator": "equals", "values": ["warn"]}]}


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-098")
def test_alerts_from_alert_policy(ops, afm_ready, uniq, cleanup, links, checks, record_property):
    """An enabled event-based alert policy (thresholds.afm.alert_policy_spec, default resource Event, severity info,
    requirement event severity == warn - a link_down is 'warn') raises an alert referencing it when a switch port
    goes down (held until oper down, then restored). g02-g17 17 Oct: the policy had no requirements - AFM API: 'a
    selector with no Requirements does not select anything' - so no alert could come (alerts stayed at 236).
    The policy is read back after the create (spec as sent, status recorded) and again after the flap: its
    status.total-hits shows whether AFM matched any event to it, so 'matched but no alert' and 'never matched'
    are told apart (18 Oct: no alert, warn link events present, policy state not recorded)."""
    name = uniq("ap")
    spec = ops.th("alert_policy_spec", None) or _warn_policy_spec(ops)
    record_property("policy_spec", spec)
    cleanup(f"{MONITORING}/alertPolicies/{name}")
    ops.afm.post(f"{MONITORING}/alertPolicies", {"meta": {"name": name}, "spec": spec})
    # read it back: a policy AFM stored but changed, disabled or flagged is told apart from one that never fires
    stored = _policy(ops, name)
    record_property("policy_stored", {"spec": stored.get("spec"), "status": stored.get("status")} if stored else None)
    checks.check(stored, f"policy {name} not found after the create (HTTP 200 on the POST)")
    if stored:
        diff = {k: (v, (stored.get("spec") or {}).get(k)) for k, v in spec.items() if (stored.get("spec") or {}).get(k) != v}
        checks.check(not diff, f"policy {name} stored with a different spec: {diff} (sent, stored)")
    hits0 = _hits(stored)
    slot = ops.compute(ops.pick_compute())["spec"]["slot-id"]
    sw, ns, port, _ = ops.gpu_links(slot)[0]
    since = ops.controller_now()
    # held down long enough for AFM's link events (17 Oct 23:11: 1.5 s down, no alert, events not recorded)
    links.shut(sw, ns, port)
    checks.check(ops.wait_oper(sw, ns, port, "down", 60), f"{sw.name} {ns} {port} never went oper down")
    time.sleep(float(ops.th("alert_hold_s", 30)) * float(ops.th("time_scale", 1)))
    links.restore(sw, ns, port, checks)
    got = ops.wait(lambda: [a for a in ops.afm.items(f"{MONITORING}/alerts") if name in json.dumps(a)], 300, 10)
    warn = [e.get("type") for e in ops.events(since=since) if e.get("severity") == "warn"]
    after = _policy(ops, name)
    hits1 = _hits(after)
    record_property("alerts", len(got or []))
    record_property("warn_events_during_flap", sorted(set(warn)))
    record_property("policy_after", {"status": (after or {}).get("status"), "total_hits": [hits0, hits1]})
    checks.check(got, f"no alert referencing policy {name} after a link flap (warn events meanwhile: "
                      f"{sorted(set(warn)) or 'none'}; policy total-hits {hits0} -> {hits1}"
                      + ("; status " + json.dumps((after or {}).get("status"))[:300] if (after or {}).get("status") else "")
                      + ")")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-EVT-021")
def test_status_change_events(ops, afm_ready, uniq, links, checks, record_property):
    """GPU, switch and vPOD status changes each raise events: link_down/link_up for a port (held until oper down),
    switchnode_unreachable + switchnode_healthy for an agent outage, vpod_* for a vPOD create/delete.
    g02-g17 17 Oct: 'no link_down / link_up event' was the check's own bug - it kept ONE event per type
    ({type: event}), so only the last link_down of the whole list was looked at."""
    since = ops.controller_now()
    slot = ops.compute(ops.pick_compute())["spec"]["slot-id"]
    sw, ns, port, _ = ops.gpu_links(slot)[0]
    _flap(ops, links, checks, sw, ns, port)
    t = ops.pick_switch(controller=None if len(ops.afm.switches()) == 1 else False)
    name, ssw = t["meta"]["name"], ops.ssh_switch(t)
    try:
        ops.afm_agent(ssw, "stop")
        ops.wait(lambda: not conditions_ok(ops.switch(name)), float(ops.th("agent_down_detect_s", 600)), 20)
    finally:
        ops.afm_agent(ssw, "start")
    ops.wait(lambda: ops.switch_ready(name), 300, 10)
    sysv, cname = ops.system_vpod_name(), ops.pick_compute()
    user, entries = uniq("vp"), []
    try:
        entries = ops.remove_compute(sysv, cname)
        ops.create_vpod(user, entries)
        ops.wait_vpod(user)
    finally:
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)

    def have():
        evs = ops.events(None, since, page=1000)

        def seen(match, needle):
            return any(match(e.get("type") or "") and ops.mentions(e, needle) for e in evs)
        return {
            "link_down": seen(lambda t: t == "link_down", port),
            "link_up": seen(lambda t: t == "link_up", port),
            "switchnode_unreachable": seen(lambda t: t == "switchnode_unreachable", name),
            "switchnode_healthy": seen(lambda t: t == "switchnode_healthy", name),
            "vpod": seen(lambda t: t.startswith("vpod_"), user),
        }
    ops.wait(lambda: all(have().values()), 240, 10)
    got = have()
    record_property("events", got)
    for k, v in got.items():
        checks.check(v, f"no {k} event")
    checks.assert_all()


# ------------------------------------------------------------------ agent-restart category

@pytest.mark.l3
@pytest.mark.tc("TC-AR-001")
def test_restart_sideband_agent(ops, afm_ready, checks, record_property):
    """Restart afm-agent on every switch: one sb-node-agent per admitted compute again, all agents connected,
    switches and computes healthy, system vPOD online/degraded."""
    _need_sideband(ops)
    pids0 = {slot: pid for sw in ops.rack.switches.values() for slot, pid in ops.sb_agents(sw).items()}
    for sw in ops.rack.switches.values():
        ops.afm_agent(sw, "restart")
    slots = {c["spec"]["slot-id"] for c in ops.active_computes()}   # a quarantined tray has no agent to bring back

    def all_back():
        seen = [s for sw in ops.rack.switches.values() for s in ops.sb_agents(sw)]
        return len(seen) == len(set(seen)) and set(seen) >= slots
    checks.check(ops.wait(all_back, 480, 15), "sb-node-agents not back one per slot")
    pids1 = {slot: pid for sw in ops.rack.switches.values() for slot, pid in ops.sb_agents(sw).items()}
    same = sorted(s for s in pids0 if pids1.get(s) == pids0[s])
    record_property("pids", {"before": pids0, "after": pids1})
    checks.check(not same, f"sb-node-agent of slot(s) {same} kept its PID: not restarted")
    for slot in slots:
        sw, _ = ops.slot_host(slot)
        if sw is not None:
            checks.check(ops.wait(lambda s=sw, sl=slot: (s.agent_api(13000 + sl, "/api/health") or {}).get("is-connected") is True,
                                  120, 10), f"slot {slot} agent not connected")
    _rack_ok(ops, checks)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-AR-002")
def test_pause_resume_node_agent(ops, afm_ready, checks, record_property):
    """SIGSTOP the node agent proxy (sb-node-agent) of one compute: AFM marks it unhealthy; SIGCONT: healthy,
    with a computenode_healthy event."""
    _need_sideband(ops)
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    sw, pid = ops.slot_host(slot)
    try:
        ops.signal_sb_agent(sw, pid, "STOP")
        checks.check(ops.wait(lambda: not conditions_ok(ops.compute(name)), float(ops.th("agent_down_detect_s", 600)), 20),
                     f"{name} still healthy with its agent paused")
        since = ops.controller_now()
    finally:
        sw2, pid2 = ops.slot_host(slot)
        ops.signal_sb_agent(sw2 or sw, pid2 or pid, "CONT")
    checks.check(ops.wait_compute_ready(name, 180), f"{name} not healthy after resume")
    checks.check(ops.wait(lambda: [e for e in ops.events("computenode_healthy", since) if name in json.dumps(e)], 120, 10),
                 "no computenode_healthy event")
    checks.assert_all()
