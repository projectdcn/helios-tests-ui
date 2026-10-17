"""AFM fabric-link failure handling (DISRUPTIVE, needs --allow-disruptive): switch port flap / prolonged
down on one or both ASICs, automatic vs manual data-fabric reconfiguration, link-fail timers, failover
policy edits, reconciliation after an agent outage, vPOD create with a link down, switch decommission with
a port down. Ports are shut with ``config interface -n asicN shutdown`` on a trunk port cabled to a GPU
(CONFIG_DB description), never one listed in thresholds.afm.avoid_ports.

Every test re-enables what it shut in ``finally`` and, if a manual-policy vPOD is left waiting, issues
ReconfigureDataFabric (the full-rack TC-AFM-041 first run left the vPOD in needdatafabricreconfiguration).
Policy-second timings scale with thresholds.afm.time_scale (1 on a rack).
"""
from __future__ import annotations

import time

import pytest

from helios.afm import CLUSTER, wait_for
from helios.afm_ops import TERMINAL

FP = f"{CLUSTER}/failoverpolicies"
pytestmark = [pytest.mark.l2, pytest.mark.disruptive]


from afm_helpers import (_baseline, _pick, _reconfigure_after_restore, _restored, _scale,  # noqa: E402
                         _target_switch, _user_vpod_ctx, _user_vpod_restore)


# ============================================================ flap / prolonged down

def _flap(ops, links, checks, record_property, asics):
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    name, sw = _target_switch(ops)
    picks = [_pick(ops, sw, asics=[a]) for a in asics]
    record_property("ports", [f"{sw.name}/{ns}/{p}" for ns, p, _ in picks])
    before_down = ops.oper_down(sw)
    for ns, port, _ in picks:
        links.shut(sw, ns, port)
    for ns, port, _ in picks:
        checks.check(ops.wait_oper(sw, ns, port, "down", 60), f"{port} ({ns}) never went down")
    for ns, port, _ in picks:
        links.restore(sw, ns, port, checks)
    extra = ops.oper_down(sw) - before_down
    checks.check(not extra, f"after the flap {sorted(extra)} stayed down (coupled ports?)")
    _restored(ops, checks, sysv, base)
    watch = float(ops.th("link_watch_s", 60))       # full rack: vPOD went configurationfailed minutes after a flap
    end = time.monotonic() + watch
    while time.monotonic() < end:
        st = ops.vpod_state(sysv)
        if st not in TERMINAL:
            checks.fail(f"{sysv} went {st} within {watch:.0f}s after the flap")
            break
        time.sleep(min(10, max(0.05, end - time.monotonic())))


@pytest.mark.tc("TC-AFM-011")
def test_switch_link_flap_single_asic(ops, afm_ready, links, checks, record_property):
    """Shut + restore one fabric port on asic0 (before the link-fail threshold): vPOD stays online/degraded,
    planes back to the baseline, and it stays that way for thresholds.afm.link_watch_s (60 s)."""
    _flap(ops, links, checks, record_property, [0])
    checks.assert_all()


@pytest.mark.tc("TC-AFM-013")
def test_switch_link_flap_both_asics(ops, afm_ready, links, checks, record_property):
    """As TC-AFM-011 with one port on each ASIC at once."""
    _flap(ops, links, checks, record_property, [0, 1])
    checks.assert_all()


def _prolonged(ops, links, checks, record_property, asics):
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    name, sw = _target_switch(ops)
    picks = [_pick(ops, sw, asics=[a]) for a in asics]
    before_down = ops.oper_down(sw)
    since = ops.controller_now()
    for ns, port, _ in picks:
        links.shut(sw, ns, port)
    hold = ops.link_fail_threshold(sysv) * _scale(ops) + float(ops.th("link_fail_margin_s", 20)) * _scale(ops)
    dropped = wait_for(lambda: (ops.planes(sysv)[1] or 0) <= (base[1] or 0) - len(picks), hold + 60 * _scale(ops), 5)
    during = ops.planes(sysv)
    record_property("planes", {"baseline": base, "during": during, "ports": [p for _, p, _ in picks]})
    checks.check(dropped, f"available planes {during} did not drop by {len(picks)} from {base} after the link-fail threshold")
    checks.check(ops.vpod_state(sysv) in TERMINAL, f"{sysv} {ops.vpod_state(sysv)} with the link(s) down")
    extra = ops.oper_down(sw) - before_down - {(ns, p) for ns, p, _ in picks}
    checks.check(not extra, f"shutting {[p for _, p, _ in picks]} also took down {sorted(extra)} (coupled ports)")
    evs = ops.wait(lambda: [e for e in ops.events("link_down", since)
                            if any(ops.mentions(e, p) for _, p, _ in picks)], 60, 10)
    checks.check(evs, "no link_down event for the shut port(s)")
    for ns, port, _ in picks:
        links.restore(sw, ns, port, checks)
    _restored(ops, checks, sysv, base)


@pytest.mark.tc("TC-AFM-012")
def test_switch_prolonged_link_down_single_asic(ops, afm_ready, links, checks, record_property):
    """One asic0 port down past the link-fail threshold: available planes drop by 1, vPOD degraded (not failed),
    link_down event, no other port goes down with it (full rack: coupled .48 Eth243/251); restored after."""
    _prolonged(ops, links, checks, record_property, [0])
    checks.assert_all()


@pytest.mark.tc("TC-AFM-014")
def test_switch_prolonged_link_down_both_asics(ops, afm_ready, links, checks, record_property):
    """As TC-AFM-012 with one port per ASIC: planes drop by 2 and come back."""
    _prolonged(ops, links, checks, record_property, [0, 1])
    checks.assert_all()


# ============================================================ automatic / manual reconfiguration

@pytest.mark.tc("TC-AFM-015")
def test_vpod_auto_reconfigure_on_link_down(ops, afm_ready, links, checks, record_property):
    """System vPOD (automatic policy): a port down past the threshold -> that GPU port PortFailed (looked up on
    every compute) and the vPOD reconfigures itself to degraded with one plane less - no user action."""
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    name, sw = _target_switch(ops)
    ns, port, cab = _pick(ops, sw)
    before = ops.failed_gpu_ports()
    links.shut(sw, ns, port)
    t = ops.link_fail_threshold(sysv) * _scale(ops) + 60 * _scale(ops)
    new = wait_for(lambda: ops.failed_gpu_ports() - before, t, 5)
    record_property("port_failed", sorted(new or []))
    checks.check(new, f"no GPU port PortFailed within {t:.0f}s of shutting {port} (slot {cab[0]} GPU{cab[1]} port {cab[3]})")
    ok = wait_for(lambda: ops.vpod_state(sysv) == "degraded" and (ops.planes(sysv)[1] or 0) < (base[1] or 0), t, 5)
    checks.check(ok, f"{sysv} not auto-reconfigured: {ops.vpod_state(sysv)} planes {ops.planes(sysv)}")
    checks.check(ops.vpod_state(sysv) != "needdatafabricreconfiguration", "automatic policy waited for manual action")
    links.restore(sw, ns, port, checks)
    _restored(ops, checks, sysv, base)
    checks.check(ops.wait(lambda: not (ops.failed_gpu_ports() - before), 120, 10), "GPU port still PortFailed after restore")
    checks.assert_all()


def _manual_flow(ops, links, checks, record_property, uniq, cleanup, threshold: int):
    sysv, user, pol, names, moved = _user_vpod_ctx(
        ops, uniq, cleanup, {"datafabric-reconfig": "manual", "link-fail-threshold": threshold})
    try:
        st0 = ops.wait_vpod(user)
        checks.check(st0 in TERMINAL, f"{user} {st0} before the link test")
        base = _baseline(ops, user, record_property)
        slots = {ops.compute(n)["spec"]["slot-id"] for n in names}
        name, sw = _target_switch(ops)
        ns, port, cab = _pick(ops, sw, slots=slots)
        t0 = time.monotonic()
        links.shut(sw, ns, port)
        need = wait_for(lambda: ops.vpod_state(user) == "needdatafabricreconfiguration",
                        (threshold + 60) * _scale(ops), 2)
        record_property("needdatafabric_after_s", round(time.monotonic() - t0, 1))
        checks.check(need, f"{user} {ops.vpod_state(user)}: manual policy never asked for reconfiguration")
        hold = 2 * 10 * _scale(ops)
        time.sleep(hold)  # must not reconfigure on its own
        checks.check(ops.vpod_state(user) == "needdatafabricreconfiguration",
                     f"{user} left needdatafabricreconfiguration without user action ({ops.vpod_state(user)})")
        r = ops.afm.raw("POST", f"{CLUSTER}/vpods/{user}/ReconfigureDataFabric", {})
        checks.check(r.status_code == 200, f"ReconfigureDataFabric -> HTTP {r.status_code}")
        ok = wait_for(lambda: ops.vpod_state(user) == "degraded", 300 * _scale(ops) + 5, 5)
        checks.check(ok, f"{user} {ops.vpod_state(user)} after ReconfigureDataFabric, expected degraded")
        links.restore(sw, ns, port, checks)
        _reconfigure_after_restore(ops, checks, user, base, threshold)
        _restored(ops, checks, user, base)
    finally:
        links.restore_all()
        _user_vpod_restore(ops, sysv, user, moved)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded after restore")


@pytest.mark.tc("TC-AFM-016")
def test_vpod_manual_reconfigure_on_link_down(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """User vPOD with a manual policy: port down -> needdatafabricreconfiguration and it holds (no automatic
    action); ReconfigureDataFabric -> degraded; port back -> baseline restored."""
    _manual_flow(ops, links, checks, record_property, uniq, cleanup, threshold=10)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-041")
def test_manual_failover_link_fail_timer(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """Manual policy, 10 s threshold: needdatafabricreconfiguration within threshold + 60 s and held 2 x 10 s;
    ReconfigureDataFabric recovers; the vPOD is never left waiting."""
    _manual_flow(ops, links, checks, record_property, uniq, cleanup, threshold=10)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-042")
def test_edit_failover_policy_manual_to_automatic(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """Plan order: a manual policy with a 300 s timer is edited to automatic with a 10 s timer, THEN a link goes
    down: AFM reconfigures on its own within 10 s + 60 s (degraded with a plane less, never
    needdatafabricreconfiguration) - proves the edit took effect; port back -> baseline restored.
    g02-g17 17 Oct: the old flow edited the policy while the vPOD already waited in needdatafabricreconfiguration
    (not the plan's order); AFM then stayed waiting 300 s (reported to AMD as a question)."""
    sysv, user, pol, names, moved = _user_vpod_ctx(
        ops, uniq, cleanup, {"datafabric-reconfig": "manual", "link-fail-threshold": 300})
    try:
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} not online/degraded")
        base = _baseline(ops, user, record_property)
        p = ops.afm.get(f"{FP}/{pol}")
        ops.afm.put(f"{FP}/{pol}", {"meta": p["meta"], "spec": {**p["spec"], "datafabric-reconfig": "automatic",
                                                                "link-fail-threshold": 10}})
        checks.check(ops.link_fail_threshold(user) == 10, f"{pol}: link-fail-threshold {ops.link_fail_threshold(user)} after the edit")
        slots = {ops.compute(n)["spec"]["slot-id"] for n in names}
        name, sw = _target_switch(ops)
        ns, port, cab = _pick(ops, sw, slots=slots)
        states, t0 = set(), time.monotonic()
        links.shut(sw, ns, port)
        ok = wait_for(lambda: (states.add(ops.vpod_state(user)) or ops.vpod_state(user)) == "degraded"
                      and (ops.planes(user)[1] or 0) < (base[1] or 0), (10 + 60) * _scale(ops), 2)
        record_property("degraded_after_s", round((time.monotonic() - t0) / _scale(ops), 1))
        record_property("states_seen", sorted(s for s in states if s))
        checks.check(ok, f"{user} {ops.vpod_state(user)} planes {ops.planes(user)}: not reconfigured within 70 s of the "
                         f"link going down after the edit to automatic / 10 s")
        checks.check("needdatafabricreconfiguration" not in states, "still waited for manual action after the edit")
        links.restore(sw, ns, port, checks)
        _restored(ops, checks, user, base)
    finally:
        links.restore_all()
        _user_vpod_restore(ops, sysv, user, moved)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded after restore")
    checks.assert_all()


@pytest.mark.tc("TC-AFM-040")
def test_custom_failover_link_fail_timer(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """Automatic policy with link-fail-threshold 30 s: the GPU port is NOT PortFailed before ~threshold, and is
    by threshold + 15 s; the vPOD then degrades and recovers after the port returns."""
    th = int(ops.th("custom_link_fail_threshold", 30))
    sysv, user, pol, names, moved = _user_vpod_ctx(
        ops, uniq, cleanup, {"datafabric-reconfig": "automatic", "link-fail-threshold": th})
    try:
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} not online/degraded")
        base = _baseline(ops, user, record_property)
        slots = {ops.compute(n)["spec"]["slot-id"] for n in names}
        name, sw = _target_switch(ops)
        ns, port, cab = _pick(ops, sw, slots=slots)
        before = ops.failed_gpu_ports()
        t0 = time.monotonic()
        links.shut(sw, ns, port)
        new = wait_for(lambda: ops.failed_gpu_ports() - before, (th + 15) * _scale(ops) + 1, 1)
        took = time.monotonic() - t0
        record_property("port_failed_after_s", round(took, 1))
        checks.check(new, f"no PortFailed within {th + 15}s (threshold {th}s)")
        checks.check(took >= (th - 5) * _scale(ops), f"PortFailed after {took:.1f}s, before the {th}s threshold")
        checks.check(wait_for(lambda: ops.vpod_state(user) == "degraded", 60 * _scale(ops) + 2, 2),
                     f"{user} {ops.vpod_state(user)} after the port failed")
        links.restore(sw, ns, port, checks)
        _restored(ops, checks, user, base)
    finally:
        links.restore_all()
        _user_vpod_restore(ops, sysv, user, moved)
    checks.assert_all()


# ============================================================ reconciliation / create with link down

@pytest.mark.tc("TC-AFM-017")
def test_link_down_reconciliation(ops, afm_ready, links, checks, record_property):
    """Port shut while the switch's afm-agent is stopped; after the agent starts AFM reconciles: link_down
    event and planes below the baseline; port back -> restored."""
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    name, sw = _target_switch(ops)
    ns, port, cab = _pick(ops, sw)
    since = ops.controller_now()
    try:
        ops.afm_agent(sw, "stop")
        time.sleep(float(ops.th("agent_settle_s", 30)))
        links.shut(sw, ns, port)
        time.sleep(float(ops.th("agent_settle_s", 30)))
    finally:
        ops.afm_agent(sw, "start")
    checks.check(ops.wait(lambda: ops.switch_ready(name), 300, 10), f"{name} not healthy after agent start")
    t = ops.link_fail_threshold(sysv) * _scale(ops) + 120 * _scale(ops)
    ok = wait_for(lambda: (ops.planes(sysv)[1] or 0) < (base[1] or 0), t, 5)
    checks.check(ok, f"planes {ops.planes(sysv)} never dropped below baseline {base}: link-down not reconciled")
    checks.check(ops.wait(lambda: [e for e in ops.events("link_down", since) if ops.mentions(e, port)], 60, 10),
                 f"no link_down event for {port} after reconciliation")
    links.restore(sw, ns, port, checks)
    _restored(ops, checks, sysv, base)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-039")
def test_vpod_degraded_on_link_down_during_create(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """A port of the test computes is down when their user vPOD is created: the vPOD comes up degraded (not
    configurationfailed); port back (re-flapped if startup does not stick) -> planes restored."""
    sysv = ops.system_vpod_name()
    names = [c["meta"]["name"] for c in sorted(ops.afm.computes(), key=lambda c: c["spec"].get("slot-id", 0))
             if ops.compute_ready(c["meta"]["name"])][-int(ops.th("vpod_test_computes", 1)):]
    sysbase = _baseline(ops, sysv, record_property)
    user, moved = uniq("vp"), []
    try:
        for n in names:
            moved += ops.remove_compute(sysv, n)
        slots = {ops.compute(n)["spec"]["slot-id"] for n in names}
        name, sw = _target_switch(ops)
        ns, port, cab = _pick(ops, sw, slots=slots)
        links.shut(sw, ns, port)
        time.sleep((ops.link_fail_threshold(sysv) + 5) * _scale(ops))
        ops.create_vpod(user, moved)
        st = ops.wait_vpod(user, ("degraded", "configurationfailed"))
        record_property("state_with_link_down", st)
        checks.check(st == "degraded", f"{user} {st} when created with a link down, expected degraded")
        links.restore(sw, ns, port, checks)
        ok = ops.wait(lambda: ops.planes(user)[1] == ops.planes(user)[0] or ops.vpod_state(user) == "online", 300, 10)
        checks.check(ok or ops.vpod_state(user) in TERMINAL, f"{user} not recovered after the port came back")
    finally:
        links.restore_all()
        ops.delete_vpod(user)
        if moved:
            ops.add_gpus(sysv, moved)
    _restored(ops, checks, sysv, sysbase, "after restore")
    checks.assert_all()


@pytest.mark.tc("TC-AFM-035")
def test_switch_decommission_readmit_with_port_down(ops, afm_ready, links, checks, record_property):
    """A fabric port on a non-controller switch is down; drain, decommission, delete and readmit that switch;
    bring the port up: switch healthy and system vPOD planes back to the baseline."""
    if len(ops.afm.switches()) < 2:
        pytest.skip("switch lifecycle needs >= 2 switches (mini rack: TC-AFM-035A)")
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    target = ops.pick_switch(controller=False)
    name, sw = target["meta"]["name"], ops.ssh_switch(target)
    ns, port, cab = _pick(ops, sw)
    links.shut(sw, ns, port)
    try:
        for action, body in (("DrainSwitchNode", {}), ("DecommissionSwitchNode", {})):
            r = ops.afm.raw("POST", f"{CLUSTER}/switchnodes/{name}/{action}", body)
            checks.check(r.status_code == 200, f"{action} -> HTTP {r.status_code}")
        checks.check(ops.wait_switch_phase(name, "decommissioned", 300), f"{name} {ops.switch_phase(name)}")
        r = ops.afm.raw("DELETE", f"{CLUSTER}/switchnodes/{name}")
        checks.check(r.status_code in (200, 204), f"DELETE -> HTTP {r.status_code}")
        ops.reset_switch_identity(sw)
        checks.check(ops.wait(lambda: ops.switch_ready(name), float(ops.th("switch_readmit_s", 600)), 15),
                     f"{name} not re-admitted")
    finally:
        links.restore(sw, ns, port, checks)
        if not ops.switch_ready(name):
            ph = ops.switch_phase(name)
            if ph == "drained":
                ops.afm.raw("POST", f"{CLUSTER}/switchnodes/{name}/ReactivateSwitchNode", {})
            elif ph != "admitted":
                ops.afm.raw("DELETE", f"{CLUSTER}/switchnodes/{name}")
                ops.reset_switch_identity(sw)
            ops.wait(lambda: ops.switch_ready(name), 600, 15)
    _restored(ops, checks, sysv, base)
    checks.assert_all()
