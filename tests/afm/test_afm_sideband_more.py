"""Sideband node-agent placement and control-path behaviour that is observable without AMD tooling
(DISRUPTIVE unless noted): sb-node-agent distribution and redistribution across switches, process restarts,
event trails, compute/switch removal and readmission, MTU changes, switch-agent start-up checks.

Agent placement is read from each switch's afm-agent process list (one ``sb-node-agent -slot-id N`` per
compute slot). "Balanced" means every switch hosting agents differs from the others by at most
thresholds.afm.agent_balance_slack (1) agents.
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter

import pytest

from helios.afm import conditions_ok, rack_health
from helios.afm_ops import CN, SN, TERMINAL

pytestmark = [pytest.mark.l3]


def _need_sideband(ops):
    if not any(ops.sb_agents(sw) for sw in ops.rack.switches.values()):
        pytest.skip("not in sideband control mode")


def _need_switches(ops, n):
    if len(ops.afm.switches()) < n:
        pytest.skip(f"needs {n} switches")


def _placement(ops) -> dict[int, list[str]]:
    """{slot: [switch names running its sb-node-agent]}"""
    out: dict[int, list[str]] = {}
    for sw in ops.rack.switches.values():
        for slot in ops.sb_agents(sw):
            out.setdefault(slot, []).append(sw.name)
    return out


def _per_switch(placement) -> Counter:
    return Counter(h for hosts in placement.values() for h in hosts)


def _slots(ops) -> set[int]:
    return {c["spec"]["slot-id"] for c in ops.afm.computes() if (c.get("status") or {}).get("admission-phase") == "admitted"}


def _one_each(ops) -> bool:
    p = _placement(ops)
    return set(p) >= _slots(ops) and all(len(v) == 1 for v in p.values())


def _balanced(ops, switches=None) -> bool:
    per = _per_switch(_placement(ops))
    names = switches or [s.name for s in ops.rack.switches.values()]
    counts = [per.get(n, 0) for n in names]
    slack = int(ops.th("agent_balance_slack", 1))
    return bool(counts) and max(counts) - min(counts) <= slack


def _healthy(ops, checks, what):
    p = rack_health(ops.afm)
    if p:
        ok = ops.wait(lambda: not rack_health(ops.afm), 600, 15)
        checks.check(ok, f"rack not healthy after {what}: {rack_health(ops.afm)[:4]}")


def _host_switch(ops, controller=False):
    t = ops.pick_switch(controller=controller)
    return t["meta"]["name"], ops.ssh_switch(t)


# ------------------------------------------------------------------ distribution (read-only)

def _distribution(ops, checks, record_property):
    _need_sideband(ops)
    p = _placement(ops)
    per = _per_switch(p)
    record_property("agents_per_switch", dict(per))
    missing = [ops.slot_label(s) for s in sorted(_slots(ops) - set(p))]
    checks.check(_one_each(ops), f"slots without exactly one agent: missing {missing}, "
                                 f"on several switches { {s: h for s, h in p.items() if len(h) > 1} }")
    n_sw, n_slots = len(ops.rack.switches), len(_slots(ops))
    if n_slots >= n_sw:
        unused = [s.name for s in ops.rack.switches.values() if per.get(s.name, 0) == 0]
        checks.check(not unused, f"switches hosting no node agent: {unused}")
    checks.check(_balanced(ops), f"node agents not balanced (slack {ops.th('agent_balance_slack', 1)}): {dict(per)}")


@pytest.mark.tc("TC-SBM-135")
def test_agents_distributed_among_switches(ops, checks, record_property):
    """Sideband Manager spreads the node agents over the available switches: one agent per compute slot, every
    switch used when there are at least as many computes as switches, counts within the balance slack."""
    _distribution(ops, checks, record_property)
    checks.assert_all()


@pytest.mark.tc("TC-SBM-136")
def test_node_agents_on_all_switches(ops, checks, record_property):
    """Node agents are distributed among all switches (as TC-SBM-135)."""
    _distribution(ops, checks, record_property)
    checks.assert_all()


@pytest.mark.tc("TC-SBM-050")
def test_sideband_agent_with_node_agent_proxy(ops, checks, record_property):
    """Every switch runs the afm-agent with its switch-agent, and the node-agent proxies (sb-node-agent) for the
    slots it serves come up in the same container."""
    _need_sideband(ops)
    per = _per_switch(_placement(ops))
    for sw in ops.rack.switches.values():
        checks.check(ops.switch_agent_pid(sw), f"{sw.name}: no switch-agent process in afm-agent")
    checks.check(sum(per.values()) >= len(_slots(ops)), f"{sum(per.values())} node agents for {len(_slots(ops))} slots")
    record_property("agents_per_switch", dict(per))
    checks.assert_all()


# ------------------------------------------------------------------ redistribution

def _stop_and_watch(ops, checks, sw, record_property):
    before = _placement(ops)
    moved_slots = [s for s, h in before.items() if sw.name in h]
    try:
        ops.afm_agent(sw, "stop")
        ok = ops.wait(lambda: all(h and sw.name not in h for s, h in _placement(ops).items() if s in moved_slots)
                      and set(_placement(ops)) >= set(moved_slots), float(ops.th("agent_respawn_s", 480)), 15)
        after = _placement(ops)
        record_property("redistributed", {s: after.get(s) for s in moved_slots})
        checks.check(ok, f"agents of {sw.name} (slots {moved_slots}) not re-homed: { {s: after.get(s) for s in moved_slots} }")
        for s in moved_slots:
            name = next((c["meta"]["name"] for c in ops.afm.computes() if c["spec"]["slot-id"] == s), None)
            if name:
                checks.check(ops.wait_compute_ready(name, 600), f"{name} not healthy after its agent moved")
    finally:
        ops.afm_agent(sw, "start")
    return moved_slots


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-137")
def test_redistribute_on_switch_failure(ops, afm_ready, checks, record_property):
    """afm-agent of a switch stopped: the node agents it hosted are re-homed on the remaining switches and their
    computes stay (or become) healthy."""
    _need_sideband(ops)
    _need_switches(ops, 2)
    name, sw = _host_switch(ops)
    _stop_and_watch(ops, checks, sw, record_property)
    checks.check(ops.wait(lambda: ops.switch_ready(name), 300, 10), f"{name} not back")
    _healthy(ops, checks, "the switch failure")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-008")
def test_redistribution_and_rebalance(ops, afm_ready, checks, record_property):
    """Switch failure re-homes its agents; after recovery the placement is balanced again (within the slack)
    and every slot has exactly one agent."""
    _need_sideband(ops)
    _need_switches(ops, 2)
    name, sw = _host_switch(ops)
    _stop_and_watch(ops, checks, sw, record_property)
    checks.check(ops.wait(lambda: ops.switch_ready(name), 300, 10), f"{name} not back")
    checks.check(ops.wait(lambda: _one_each(ops) and _balanced(ops), float(ops.th("rebalance_s", 600)), 15),
                 f"placement not balanced after recovery: {dict(_per_switch(_placement(ops)))}")
    _healthy(ops, checks, "recovery")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-009")
def test_no_thrashing_on_repeated_flap(ops, afm_ready, checks, record_property):
    """afm-agent of one switch stopped and started thresholds.afm.flap_cycles (3) times: after each recovery every
    slot has exactly one agent, and once settled the assignment stops changing (no thrashing for 60 s)."""
    _need_sideband(ops)
    _need_switches(ops, 2)
    name, sw = _host_switch(ops)
    k = float(ops.th("time_scale", 1))
    for _ in range(int(ops.th("flap_cycles", 3))):
        try:
            ops.afm_agent(sw, "stop")
            time.sleep(float(ops.th("agent_settle_s", 30)))
        finally:
            ops.afm_agent(sw, "start")
        ops.wait(lambda: ops.switch_ready(name), 300, 10)
        checks.check(ops.wait(lambda: _one_each(ops), 600, 15), "a slot without exactly one agent after a flap")
    settled = _placement(ops)
    changes = 0
    end = time.monotonic() + 60 * k
    while time.monotonic() < end:
        time.sleep(min(10 * k + 0.05, max(0.05, end - time.monotonic())))
        now = _placement(ops)
        changes += now != settled
        settled = now
    record_property("assignment_changes_after_settle", changes)
    checks.check(changes == 0, f"agent assignment changed {changes} time(s) after settling (thrashing)")
    _healthy(ops, checks, "the flaps")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-011")
def test_gpu_set_not_split(ops, afm_ready, checks, record_property):
    """When a switch fails, each compute's GPU set moves as a whole: every slot is served by exactly one agent on
    one switch before, during and after (no compute split across switches), and its GPUs stay Ready."""
    _need_sideband(ops)
    _need_switches(ops, 2)
    name, sw = _host_switch(ops)
    checks.check(_one_each(ops), "a slot already split / missing before the test")
    split = []
    try:
        ops.afm_agent(sw, "stop")
        end = time.monotonic() + 60 * float(ops.th("time_scale", 1))
        while time.monotonic() < end:
            split += [s for s, h in _placement(ops).items() if len(h) > 1]
            time.sleep(0.2 if float(ops.th("time_scale", 1)) < 1 else 5)
    finally:
        ops.afm_agent(sw, "start")
    ops.wait(lambda: ops.switch_ready(name), 300, 10)
    record_property("split_slots_seen", sorted(set(split)))
    checks.check(not split, f"slots served from two switches at once: {sorted(set(split))}")
    _healthy(ops, checks, "the failover")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-198")
def test_rebalance_after_switch_readd(ops, afm_ready, checks, record_property):
    """A switch drained, decommissioned, deleted and re-added: once re-admitted it hosts node agents again and the
    placement is balanced."""
    _need_sideband(ops)
    _need_switches(ops, 2)
    name, sw = _host_switch(ops)
    try:
        ops.afm.post(f"{SN}/{name}/DrainSwitchNode", {})
        ops.wait_switch_phase(name, "drained", 900)
        ops.afm.post(f"{SN}/{name}/DecommissionSwitchNode", {})
        ops.wait_switch_phase(name, "decommissioned", 300)
        ops.afm.raw("DELETE", f"{SN}/{name}")
        ops.wait(lambda: ops.switch_phase(name) == "absent", 60, 5)
    finally:
        ops.reset_switch_identity(sw)
    checks.check(ops.wait(lambda: ops.switch_ready(name), float(ops.th("switch_readmit_s", 600)), 15), f"{name} not readmitted")
    ok = ops.wait(lambda: _one_each(ops) and _balanced(ops) and _per_switch(_placement(ops)).get(sw.name, 0) > 0,
                  float(ops.th("rebalance_s", 600)), 15)
    record_property("agents_per_switch", dict(_per_switch(_placement(ops))))
    checks.check(ok, f"no rebalance onto re-added {name}: {dict(_per_switch(_placement(ops)))}")
    _healthy(ops, checks, "the re-add")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-006")
def test_pairwise_process_restarts(ops, afm_ready, checks, record_property):
    """Concurrent restarts of every pair of {afm-agent on switch A, afm-agent on switch B, afm-controller on a
    non-leader controller}: after each pair the rack returns to healthy (cluster, switches, computes, vPOD)."""
    _need_switches(ops, 2)
    a, b = list(ops.rack.switches.values())[:2]
    ctrl_ip = next((m for m in ops.quorum_ips() if m != ops.leader_ip()), ops.leader_ip())
    actions = {f"afm-agent@{a.name}": lambda: ops.afm_agent(a, "restart"),
               f"afm-agent@{b.name}": lambda: ops.afm_agent(b, "restart"),
               f"afm-controller@{ctrl_ip}": lambda: ops.restart_controller(ops.ssh_by_ip(ctrl_ip))}
    names = list(actions)
    took = {}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            pair = (names[i], names[j])
            ts = [threading.Thread(target=actions[n]) for n in pair]
            t0 = time.monotonic()
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            ok = ops.wait(lambda: not rack_health(ops.afm), 900, 15)
            took[" + ".join(pair)] = round(time.monotonic() - t0, 1)
            checks.check(ok, f"rack not healthy after restarting {pair}: {rack_health(ops.afm)[:3]}")
    record_property("recovery_s", took)
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-099")
def test_node_agent_controller_flap(ops, afm_ready, checks, record_property):
    """Connectivity between the node agents and the controller flapped (leader controller restarted): every node
    agent reconnects (/api/health is-connected) and every compute is healthy again."""
    _need_sideband(ops)
    ops.restart_controller(ops.ssh_by_ip(ops.leader_ip()))
    ops.wait(lambda: conditions_ok(ops.afm.cluster()), 300, 10)

    def connected():
        bad = []
        for c in ops.afm.computes():
            sw, _ = ops.slot_host(c["spec"]["slot-id"])
            h = sw.agent_api(13000 + c["spec"]["slot-id"], "/api/health") if sw else None
            if not h or h.get("is-connected") is not True:
                bad.append(c["meta"]["name"])
        return bad
    checks.check(ops.wait(lambda: not connected(), 300, 10), f"agents not reconnected: {connected()}")
    _healthy(ops, checks, "the controller flap")
    checks.assert_all()


# ------------------------------------------------------------------ removal / readmission

@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-207")
def test_control_path_terminated_on_decommission(ops, afm_ready, checks, record_property):
    """Decommissioning a compute terminates its control path: the slot's sb-node-agent either exits or reports
    is-connected false on /api/health (the g02-g17 readmit procedure keeps the process after DELETE, so its
    presence alone is not a failure; which of the two happened is recorded). After delete + readmit the agent
    is connected again and the compute is healthy."""
    _need_sideband(ops)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    host = None

    def terminated():
        sw, _ = ops.slot_host(slot)
        if sw is None:
            return "exited"
        return "disconnected" if (sw.agent_api(13000 + slot, "/api/health") or {}).get("is-connected") is not True \
            else None

    try:
        host, _ = ops.slot_host(slot)
        ops.afm.post(f"{CN}/{name}/Decommission", {})
        ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
        how = ops.wait(terminated, float(ops.th("agent_respawn_s", 480)), 10)
        record_property("control_path_after_decommission", how or "still connected")
        checks.check(bool(how), f"sb-node-agent of slot {slot} still connected to the controller after decommission")
        ops.afm.raw("DELETE", f"{CN}/{name}")
        ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5)
    finally:
        if ops.compute_phase(name) != "admitted":
            sw, _ = ops.slot_host(slot)
            if sw is not None or host is not None:
                ops.afm_agent(sw or host, "restart")
            checks.check(ops.wait_compute_ready(name), f"{name} not readmitted")
        if ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait(lambda: terminated() is None, float(ops.th("agent_recover_s", 300)), 10),
                 f"slot {slot} agent not connected again after readmit")
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-042")
def test_event_trail_compute_lifecycle(ops, afm_ready, checks, record_property):
    """Decommission, delete and readmit of a compute each leave an event naming the compute."""
    _need_sideband(ops)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    marks = {}
    host, _ = ops.slot_host(slot)

    def evs_since(t):
        return [e for e in ops.events(None, t) if name in json.dumps(e)]
    try:
        t = ops.controller_now()
        ops.afm.post(f"{CN}/{name}/Decommission", {})
        ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
        marks["decommission"] = bool(ops.wait(lambda: evs_since(t), 120, 10))
        t = ops.controller_now()
        ops.afm.raw("DELETE", f"{CN}/{name}")
        ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5)
        marks["delete"] = bool(ops.wait(lambda: evs_since(t), 120, 10))
        t = ops.controller_now()
        if host is not None:
            ops.afm_agent(host, "restart")
        checks.check(ops.wait_compute_ready(name), f"{name} not readmitted")
        marks["readmit"] = bool(ops.wait(lambda: evs_since(t), 120, 10))
    finally:
        if ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    record_property("events_per_phase", marks)
    for k, v in marks.items():
        checks.check(v, f"no event naming {name} for {k}")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-043")
def test_switch_ports_down_when_compute_deleted(ops, afm_ready, checks, record_property):
    """After a compute is deleted, the switch ports cabled to it are no longer reported PortReady in the AFM
    switch-node API (status.asic-info) - the count of ready ports drops - and recover after readmit."""
    _need_sideband(ops)

    def ready_ports():
        n = 0
        for s in ops.afm.switches():
            for a in ((s.get("status") or {}).get("asic-info") or {}).get("asics") or []:
                n += sum(1 for p in a.get("ports") or [] if p.get("oper-state") == "PortReady")
        return n
    before = ready_ports()
    if not before:
        pytest.skip("AFM switch nodes report no asic-info ports")
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    host, _ = ops.slot_host(slot)
    entries = ops.remove_compute(sysv, name)
    try:
        ops.afm.post(f"{CN}/{name}/Decommission", {})
        ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
        ops.afm.raw("DELETE", f"{CN}/{name}")
        ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5)
        dropped = ops.wait(lambda: ready_ports() < before, 180, 10)
        record_property("ready_ports", {"before": before, "deleted": ready_ports()})
        checks.check(dropped, f"switch ports still all PortReady ({ready_ports()}) after deleting {name}")
    finally:
        if ops.compute_phase(name) != "admitted":
            if host is not None:
                ops.afm_agent(host, "restart")
            ops.wait_compute_ready(name)
        if ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait(lambda: ready_ports() >= before, 300, 10), f"ready ports {ready_ports()} < {before} after readmit")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-038")
def test_readmit_with_port_down(ops, afm_ready, links, checks, record_property):
    """A compute readmitted while one of its fabric ports is down: it is admitted and healthy, that GPU port is
    reported failed, the others Ready; after the port returns everything is Ready."""
    _need_sideband(ops)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    host, _ = ops.slot_host(slot)
    sw, ns, port, cab = ops.gpu_links(slot)[0]
    want = {(name, cab[1], cab[3])}
    entries = ops.remove_compute(sysv, name)
    try:
        links.shut(sw, ns, port)
        ops.afm.post(f"{CN}/{name}/Decommission", {})
        ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
        ops.afm.raw("DELETE", f"{CN}/{name}")
        ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5)
        if host is not None:
            ops.afm_agent(host, "restart")
        ok = ops.wait(lambda: ops.compute_phase(name) == "admitted" and conditions_ok(ops.compute(name)), 600, 10)
        checks.check(ok, f"{name} not readmitted with a port down")
        ops.add_gpus(sysv, entries)
        entries = []
        checks.check(ops.wait(lambda: want <= ops.failed_gpu_ports(), ops.link_fail_threshold(sysv) + 120, 10),
                     f"down port {cab} not reported failed after readmit")
    finally:
        links.restore_all()
        if entries and ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait(lambda: not (want & ops.failed_gpu_ports()), 300, 10), "port still failed after restore")
    checks.check(ops.wait_compute_ready(name, 300), f"{name} GPUs not all Ready")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-045")
def test_delete_agent_host_mid_admission(ops, afm_ready, checks, record_property):
    """A compute is being readmitted when the switch hosting its node agent is force-decommissioned and deleted:
    the agent moves to another switch and the compute still ends admitted and healthy; the switch is readmitted."""
    _need_sideband(ops)
    _need_switches(ops, 2)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    host, _ = ops.slot_host(slot)
    host_obj = next(s for s in ops.afm.switches()
                    if ops.ssh_switch(s) is host)
    hname = host_obj["meta"]["name"]
    try:
        ops.afm.post(f"{CN}/{name}/Decommission", {})
        ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
        ops.afm.raw("DELETE", f"{CN}/{name}")
        ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5)
        ops.afm_agent(host, "restart")                         # admission starts
        ops.afm.raw("POST", f"{SN}/{hname}/DecommissionSwitchNode", {"force-decommission": True})
        ops.wait_switch_phase(hname, "decommissioned", 300)
        ops.afm.raw("DELETE", f"{SN}/{hname}")
        new_host = ops.wait(lambda: (lambda h: h[0] if h[0] is not None and h[0] is not host else None)(ops.slot_host(slot)),
                            float(ops.th("agent_respawn_s", 480)), 10)
        record_property("agent_host", {"before": host.name, "after": getattr(new_host, "name", None)})
        checks.check(new_host, f"slot {slot} agent did not move off the deleted switch {hname}")
        checks.check(ops.wait_compute_ready(name, 900), f"{name} not admitted after its host switch was deleted")
    finally:
        if ops.switch_phase(hname) != "admitted":
            if ops.switch_phase(hname) not in ("absent",):
                ops.afm.raw("DELETE", f"{SN}/{hname}")
            ops.reset_switch_identity(host)
            ops.wait(lambda: ops.switch_ready(hname), float(ops.th("switch_readmit_s", 600)), 15)
        if ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    checks.check(ops.switch_ready(hname), f"{hname} not readmitted")
    _healthy(ops, checks, "the mid-admission delete")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-030")
def test_no_stale_events_after_switch_removal(ops, afm_ready, checks, record_property):
    """After a switch is decommissioned and deleted, no new event references it while it is gone (watched for
    thresholds.afm.stale_watch_s, 120 s); then it is readmitted."""
    _need_switches(ops, 2)
    name, sw = _host_switch(ops)
    try:
        ops.afm.post(f"{SN}/{name}/DecommissionSwitchNode", {"force-decommission": True})
        ops.wait_switch_phase(name, "decommissioned", 300)
        ops.afm.raw("DELETE", f"{SN}/{name}")
        ops.wait(lambda: ops.switch_phase(name) == "absent", 60, 5)
        t = ops.controller_now()
        time.sleep(float(ops.th("stale_watch_s", 120)) * float(ops.th("time_scale", 1)))
        stale = [e for e in ops.events(None, t) if name in json.dumps(e)]
        record_property("stale_events", [e.get("type") for e in stale][:10])
        checks.check(not stale, f"{len(stale)} event(s) referencing deleted {name}: {[e.get('type') for e in stale][:5]}")
    finally:
        ops.reset_switch_identity(sw)
    checks.check(ops.wait(lambda: ops.switch_ready(name), float(ops.th("switch_readmit_s", 600)), 15), f"{name} not readmitted")
    _healthy(ops, checks, "the readmit")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-178")
def test_force_decommission_admitted_switch(ops, afm_ready, checks, record_property):
    """An admitted (not drained) switch can be force-decommissioned; then it is deleted and readmitted."""
    _need_switches(ops, 2)
    name, sw = _host_switch(ops)
    try:
        r = ops.afm.raw("POST", f"{SN}/{name}/DecommissionSwitchNode", {"force-decommission": True})
        checks.check(r.status_code == 200, f"force-decommission of admitted {name} -> HTTP {r.status_code}")
        checks.check(ops.wait_switch_phase(name, "decommissioned", 300), f"{name} {ops.switch_phase(name)}")
        ops.afm.raw("DELETE", f"{SN}/{name}")
        ops.wait(lambda: ops.switch_phase(name) == "absent", 60, 5)
    finally:
        ops.reset_switch_identity(sw)
    checks.check(ops.wait(lambda: ops.switch_ready(name), float(ops.th("switch_readmit_s", 600)), 15), f"{name} not readmitted")
    _healthy(ops, checks, "the readmit")
    checks.assert_all()


# ------------------------------------------------------------------ control plane under data-path changes

def _cp_ok(ops, names) -> list[str]:
    bad = [n for n in names if not conditions_ok(ops.compute(n))]
    for c in ops.afm.computes():
        sw, _ = ops.slot_host(c["spec"]["slot-id"])
        h = sw.agent_api(13000 + c["spec"]["slot-id"], "/api/health") if sw else None
        if not h or h.get("is-connected") is not True:
            bad.append(f"agent slot {c['spec']['slot-id']}")
    return bad


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-097")
def test_non_sideband_link_down(ops, afm_ready, links, checks, record_property):
    """A data (non-control) link between a switch and a GPU brought down: the control plane is unaffected (computes
    healthy, agents connected) and the other ports' IFCP sessions stay up; restored afterwards."""
    _need_sideband(ops)
    name = ops.pick_compute()
    tray = ops.tray(name)
    base = ops.ifcp_up(tray)
    sw, ns, port, cab = ops.gpu_links(ops.compute(name)["spec"]["slot-id"])[-1]
    links.shut(sw, ns, port)
    time.sleep(10 * float(ops.th("time_scale", 1)))
    bad = _cp_ok(ops, [c["meta"]["name"] for c in ops.afm.computes()])
    lost = sorted(base - ops.ifcp_up(tray) - {(f"000{cab[1] + 1}:01:00.1", cab[3])})
    record_property("result", {"cp_problems": bad, "other_sessions_lost": lost})
    checks.check(not bad, f"control plane disturbed: {bad}")
    checks.check(not lost, f"other IFCP sessions dropped: {lost[:6]}")
    links.restore(sw, ns, port, checks)
    checks.check(ops.wait(lambda: base <= ops.ifcp_up(tray), 120, 5), "IFCP sessions not all back")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-114")
def test_vlan_fdb_creation_cp_unaffected(ops, afm_ready, uniq, checks, record_property):
    """Creating a user vPOD (VLANs and forwarding entries on every switch) does not disturb the control plane:
    computes healthy and node agents connected throughout."""
    _need_sideband(ops)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    user, entries, bad = uniq("vp"), [], []
    try:
        entries = ops.remove_compute(sysv, name)
        ops.create_vpod(user, entries)
        end = time.monotonic() + 30 * float(ops.th("time_scale", 1))
        while time.monotonic() < end:
            bad += _cp_ok(ops, [c["meta"]["name"] for c in ops.afm.computes()])
            time.sleep(0.2 if float(ops.th("time_scale", 1)) < 1 else 5)
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} {ops.vpod_state(user)}")
    finally:
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)
    record_property("cp_problems", sorted(set(bad)))
    checks.check(not bad, f"control plane disturbed by VLAN/FDB creation: {sorted(set(bad))[:5]}")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-111")
def test_cp_switch_down_during_vpod_crud(ops, afm_ready, uniq, checks, record_property):
    """The switch hosting a compute's node agent loses its afm-agent while that compute is being moved into a user
    vPOD: the agent re-homes, the vPOD ends online/degraded, and everything is restored."""
    _need_sideband(ops)
    _need_switches(ops, 2)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    host, _ = ops.slot_host(slot)
    user, entries, err = uniq("vp"), [], []

    def crud():
        try:
            entries.extend(ops.remove_compute(sysv, name))
            ops.create_vpod(user, list(entries))
        except Exception as e:  # noqa: BLE001
            err.append(str(e)[:200])
    t = threading.Thread(target=crud)
    try:
        t.start()
        ops.afm_agent(host, "stop")
        t.join()
        time.sleep(float(ops.th("agent_settle_s", 30)))
    finally:
        ops.afm_agent(host, "start")
    st = ops.wait_vpod(user, timeout=float(ops.th("agent_recover_s", 600))) if not err else None
    record_property("result", {"crud_errors": err, "vpod_state": st})
    checks.check(not err, f"vPOD CRUD failed with the CP switch down: {err}")
    checks.check(st in TERMINAL, f"{user} {st} after the CP switch returned")
    ops.delete_vpod(user)
    if entries:
        ops.add_gpus(sysv, entries)
    _healthy(ops, checks, "the CP switch outage")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SBM-023")
def test_port_mtu_below_ifcp_mtu(ops, afm_ready, inventory, checks, record_property):
    """A switch port's MTU lowered below the negotiated IFCP MTU (peer_mtu, 4096 on MR3) and restored: the GPU
    port's IFCP session recovers after the restore and nothing else is disturbed. What happens while the MTU is
    low is recorded (thresholds.afm.mtu_low_expect: 'down' asserts the session drops)."""
    _need_sideband(ops)
    name = ops.pick_compute()
    tray = ops.tray(name)
    base = ops.ifcp_up(tray)
    sw, ns, port, cab = ops.gpu_links(ops.compute(name)["spec"]["slot-id"])[0]
    key = (f"000{cab[1] + 1}:01:00.1", cab[3])
    orig = next((r.get("MTU") for r in sw.interfaces_status(ns) if r.get("Interface") == port), "9100")
    low = str(ops.th("mtu_low", 1500))
    tmpl = inventory.cmd("switch.port_mtu", "config interface -n {ns} mtu {port} {mtu}")
    try:
        r = sw.run(tmpl.format(ns=ns, port=port, mtu=low), sudo=True, timeout=60)
        checks.check(r.ok, f"setting MTU {low} on {port} failed: {r.tail()!r}")
        time.sleep(30 * float(ops.th("time_scale", 1)))
        during = key in ops.ifcp_up(tray)
        record_property("ifcp_up_with_low_mtu", during)
        if ops.th("mtu_low_expect", "") == "down":
            checks.check(not during, f"IFCP session on {key} stayed up with MTU {low}")
    finally:
        sw.run(tmpl.format(ns=ns, port=port, mtu=orig), sudo=True, timeout=60)
    checks.check(ops.wait(lambda: base <= ops.ifcp_up(tray), 180, 10), f"IFCP sessions not all back after MTU {orig}")
    checks.check(conditions_ok(ops.compute(name)), f"{name} not healthy")
    checks.assert_all()


# ------------------------------------------------------------------ switch-agent start-up checks (templates)

@pytest.mark.disruptive
@pytest.mark.tc("TC-BUG-019")
def test_agent_mgmt_ip_on_eth1(ops, afm_ready, inventory, checks, record_property):
    """Switch agent starts correctly when the management IP is on a non-default interface: commands.switch.mgmt_to_eth1
    / mgmt_restore (templates, {name}) move it; afm-agent is restarted and the switch must come back admitted and
    healthy. Skips until the templates are set (needs out-of-band access to the switch)."""
    to, back = inventory.cmd("switch.mgmt_to_eth1"), inventory.cmd("switch.mgmt_restore")
    if not (to and back):
        pytest.skip("commands.switch.mgmt_to_eth1 / mgmt_restore not set")
    _need_switches(ops, 2)
    name, sw = _host_switch(ops)
    try:
        r = sw.run(to.format(name=sw.name), sudo=True, timeout=300)
        checks.check(r.ok, f"mgmt_to_eth1 failed: {r.tail()!r}")
        ops.afm_agent(sw, "restart")
        checks.check(ops.wait(lambda: ops.switch_ready(name), 600, 15), f"{name} not healthy with mgmt on eth1")
    finally:
        sw.run(back.format(name=sw.name), sudo=True, timeout=300)
        ops.afm_agent(sw, "restart")
    checks.check(ops.wait(lambda: ops.switch_ready(name), 600, 15), f"{name} not healthy after restoring mgmt")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-BUG-020")
def test_agent_rejects_invalid_fru(ops, afm_ready, inventory, checks, record_property):
    """With an invalid fru.json (thresholds.afm.fru_json_path, VERIFY) the switch agent does not start (no
    switch-agent process, switch not healthy); the original file is restored and the agent starts again."""
    path = ops.th("fru_json_path", None)
    if not path:
        pytest.skip("thresholds.afm.fru_json_path not set (location of the switch agent's fru.json)")
    _need_switches(ops, 2)
    name, sw = _host_switch(ops)
    bak = f"{path}.helios-bak"
    r = sw.run(f"cp -p {path} {bak}", sudo=True, timeout=60)
    if not r.ok:
        pytest.fail(f"cannot back up {path}: {r.tail()!r}")
    try:
        sw.run(f"sh -c 'echo {{not-json > {path}'", sudo=True, timeout=60)
        ops.afm_agent(sw, "restart")
        time.sleep(float(ops.th("agent_settle_s", 30)))
        checks.check(not ops.switch_agent_pid(sw), "switch-agent running with an invalid fru.json")
        checks.check(ops.wait(lambda: not conditions_ok(ops.switch(name)), float(ops.th("agent_down_detect_s", 600)), 20),
                     f"{name} still healthy with an invalid fru.json")
    finally:
        sw.run(f"mv -f {bak} {path}", sudo=True, timeout=60)
        ops.afm_agent(sw, "restart")
    checks.check(ops.wait(lambda: ops.switch_ready(name), 600, 15), f"{name} not healthy after restoring fru.json")
    checks.assert_all()

