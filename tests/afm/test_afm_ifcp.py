"""IFCP control-plane recovery and triggers, observed per GPU port from the compute side
(``afmctl show port --json`` status.ifcp: link_up / ll_up / peer_enabled / errors).

A port's IFCP session is "up" when it is LINK_UP with ll_up=yes, peer enabled and no error. Every recovery case
compares the set of up sessions on every tray with the baseline taken before the trigger
(thresholds.afm.ifcp_recover_s, 120 s, to get back). SPDM session / SelectedLink internals are not exposed by
afmctl or the node-agent API, so cases that need them are not automated.
"""
from __future__ import annotations

import re
import time

import pytest

from helios.afm import CLUSTER, conditions_ok
from helios.afm_ops import CN, SN, TERMINAL

VP = f"{CLUSTER}/vpods"


def _baseline(ops) -> dict:
    return {c["meta"]["name"]: ops.ifcp_up(ops.tray(c["meta"]["name"])) for c in ops.active_computes()}  # quarantined: no SSH


def _recovered(ops, checks, base: dict, what: str, only=None):
    t = float(ops.th("ifcp_recover_s", 120))
    for name, want in base.items():
        if only and name not in only:
            continue
        tray = ops.tray(name)
        ok = ops.wait(lambda tray=tray, want=want: want <= ops.ifcp_up(tray), t, 5)
        missing = sorted(want - ops.ifcp_up(tray))
        checks.check(ok, f"{name}: {len(missing)} IFCP session(s) not back {t:.0f}s after {what}: {missing[:6]}")


def _afm_gpu_index(ops, name: str, tray, bdf: str) -> int | None:
    """AFM gpu-info index of the tray GPU at ``bdf``, matched by accelerator id (afmctl accelerator_id = AFM acc-id);
    not derived from the PCI domain (TC-TRG-010 assumed domain 000N = GPU N-1)."""
    acc = next((d.accel_id for d in ops.afmctl(tray).devices() if d.bdf == bdf), None)
    return next((g.get("index") for g in ops.gpu_info(ops.compute(name)).values() if str(g.get("acc-id")) == str(acc)), None)


def _agents_connected(ops) -> list[str]:
    bad = []
    for c in ops.afm.computes():
        slot = c["spec"]["slot-id"]
        sw, _ = ops.slot_host(slot)
        h = sw.agent_api(13000 + slot, "/api/health") if sw else None
        if not h or h.get("is-connected") is not True:
            bad.append(f"slot {slot}")
    return bad


# ------------------------------------------------------------------ ifcp-negotiation

@pytest.mark.l3
@pytest.mark.tc("TC-001")
def test_baseline_session_state(ops, checks, record_property):
    """Every LINK_UP GPU port on every tray has an IFCP session up (ll_up, peer enabled, no errors)."""
    counts, bad = {}, []
    for c in ops.active_computes():           # quarantined trays left out
        try:        # a tray that is down (no SSH / no afmctl) is one failed check, not the end of the test
            tray = ops.tray(c["meta"]["name"])
            st = ops.ifcp(tray)
            sess = ops.ifcp_up(tray)
        except Exception as e:  # noqa: BLE001
            checks.fail(f"{c['meta']['name']}: cannot read IFCP state: {str(e).split(';')[0]}")
            continue
        up = [k for k, v in st.items() if v["link"] == "LINK_UP"]
        counts[tray.name] = f"{len(sess)}/{len(up)}"
        bad += [f"{tray.name}/{k[0]} port {k[1]}" for k in up if k not in sess]
    record_property("sessions_up", counts)
    checks.check(not bad, f"{len(bad)} LINK_UP ports without an IFCP session: {bad[:6]}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-002")
def test_link_flap_negotiation_recovery(ops, afm_ready, links, checks, record_property):
    """Switch-side flap of one GPU port: the compute port renegotiates IFCP; every session is back."""
    base = _baseline(ops)
    name = ops.pick_compute()
    sw, ns, port, cab = ops.gpu_links(ops.compute(name)["spec"]["slot-id"])[0]
    links.shut(sw, ns, port)
    links.restore(sw, ns, port, checks)
    _recovered(ops, checks, base, "a link flap")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-003")
def test_afm_agent_restart_negotiation(ops, afm_ready, checks, record_property):
    """afm-agent restarted on every switch: every IFCP session renegotiates cleanly."""
    base = _baseline(ops)
    for sw in ops.rack.switches.values():
        ops.afm_agent(sw, "restart")
    for c in ops.afm.computes():
        ops.wait_compute_ready(c["meta"]["name"], 600)
    _recovered(ops, checks, base, "the afm-agent restart")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-004")
def test_node_agent_pause_resume(ops, afm_ready, checks, record_property):
    """A compute's node agent proxy paused (SIGSTOP) for thresholds.afm.pause_s (30 s) and resumed: its IFCP
    sessions are all up afterwards."""
    if not any(ops.sb_agents(sw) for sw in ops.rack.switches.values()):
        pytest.skip("not in sideband control mode")
    base = _baseline(ops)
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    sw, pid = ops.slot_host(slot)
    try:
        ops.signal_sb_agent(sw, pid, "STOP")
        time.sleep(float(ops.th("pause_s", 30)) * float(ops.th("time_scale", 1)))
    finally:
        sw2, pid2 = ops.slot_host(slot)
        ops.signal_sb_agent(sw2 or sw, pid2 or pid, "CONT")
    ops.wait_compute_ready(name, 300)
    _recovered(ops, checks, base, "the agent pause", only={name})
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-005")
def test_controller_restart_recovery(ops, afm_ready, checks, record_property):
    """All afm-controller containers restarted: IFCP sessions (switch <-> GPU, local to the rack) are all up
    afterwards and the agents reconnect."""
    base = _baseline(ops)
    for ip, sw in ops.controllers().items():
        ops.restart_controller(sw)
    ops.wait(lambda: conditions_ok(ops.afm.cluster()), 300, 10)
    _recovered(ops, checks, base, "the controller restart")
    checks.check(ops.wait(lambda: not _agents_connected(ops), 300, 10), f"agents not reconnected: {_agents_connected(ops)}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-007")
def test_compute_port_shut_restore(ops, afm_ready, checks, record_property):
    """A GPU port disabled on the compute (afmctl admin idle): its IFCP session drops; re-enabled: it is back."""
    base = _baseline(ops)
    name = ops.pick_compute()
    tray = ops.tray(name)
    bdf, port = sorted(base[name])[0]
    try:
        ops.compute_port_admin(tray, bdf, port, up=False)
        checks.check(ops.wait(lambda: (bdf, port) not in ops.ifcp_up(tray), 60, 5), "IFCP session stayed up on a disabled port")
    finally:
        ops.compute_port_admin(tray, bdf, port, up=True)
    _recovered(ops, checks, base, "the compute port restore", only={name})
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-008")
def test_all_links_down_up(ops, afm_ready, links, checks, record_property):
    """Every fabric port of one GPU shut on the switches, then brought back: all its IFCP sessions recover."""
    base = _baseline(ops)
    name = ops.pick_compute()
    sel = ops.gpu_links(ops.compute(name)["spec"]["slot-id"], 0)
    record_property("ports", len(sel))
    for sw, ns, port, _ in sel:
        links.shut(sw, ns, port)
    tray = ops.tray(name)
    gone = ops.wait(lambda: not any(k[0] == "0001:01:00.1" for k in ops.ifcp_up(tray)), 120, 5)
    checks.check(gone, "GPU0 still has IFCP sessions with every link shut")
    for sw, ns, port, _ in sel:
        links.restore(sw, ns, port, checks)
    _recovered(ops, checks, base, "all links up", only={name})
    checks.assert_all()


# ------------------------------------------------------------------ ifcp-triggers

def _watch_cp(ops, seconds: float, names) -> list[str]:
    """Sample compute health and agent connectivity for ``seconds``; return what went wrong."""
    bad = set()
    end = time.monotonic() + seconds
    while True:
        for n in names:
            if not conditions_ok(ops.compute(n)):
                bad.add(f"{n} unhealthy")
        bad |= set(_agents_connected(ops))
        if time.monotonic() >= end:
            return sorted(bad)
        time.sleep(min(5.0, max(0.05, end - time.monotonic())))


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-TRG-004")
def test_switch_drain_cp_unaffected(ops, afm_ready, checks, record_property):
    """Draining a (non-controller) switch: computes stay healthy and every node agent stays connected; IFCP
    sessions on ports not cabled to that switch are untouched."""
    if len(ops.afm.switches()) < 2:
        pytest.skip("needs 2 switches")
    base = _baseline(ops)
    t = ops.pick_switch(controller=False)
    name = t["meta"]["name"]
    names = [c["meta"]["name"] for c in ops.afm.computes()]
    try:
        ops.afm.post(f"{SN}/{name}/DrainSwitchNode", {})
        ops.wait_switch_phase(name, "drained", 900)
        bad = _watch_cp(ops, 30 * float(ops.th("time_scale", 1)), names)
        record_property("cp_problems", bad)
        checks.check(not bad, f"control plane disturbed by the drain: {bad}")
    finally:
        ops.afm.raw("POST", f"{SN}/{name}/ReactivateSwitchNode", {})
    ops.wait(lambda: ops.switch_ready(name), 600, 15)
    _recovered(ops, checks, base, "drain + reactivate")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-TRG-005")
def test_gpu_port_drain_cp_unaffected(ops, afm_ready, checks, record_property):
    """DrainGPUPort on one GPU port: compute healthy, agents connected, the other ports' IFCP sessions stay up;
    ActivateGPUPort restores it."""
    base = _baseline(ops)
    name = ops.pick_compute()
    tray = ops.tray(name)
    bdf, port = sorted(base[name])[0]
    gpu = int(bdf[3]) - 1
    body = {"ports": [{"gpu-id": gpu, "port-id": port}]}
    try:
        ops.afm.post(f"{CN}/{name}/DrainGPUPort", body)
        bad = _watch_cp(ops, 30 * float(ops.th("time_scale", 1)), [name])
        others = base[name] - {(bdf, port)}
        lost = sorted(others - ops.ifcp_up(tray))
        record_property("result", {"cp_problems": bad, "other_sessions_lost": lost})
        checks.check(not bad, f"control plane disturbed by the port drain: {bad}")
        checks.check(not lost, f"other IFCP sessions dropped: {lost[:6]}")
    finally:
        ops.afm.post(f"{CN}/{name}/ActivateGPUPort", body)
    _recovered(ops, checks, base, "ActivateGPUPort", only={name})
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-TRG-006")
def test_vpod_membership_change_renegotiates(ops, afm_ready, uniq, checks, record_property):
    """A compute moved into a user vPOD and back: its IFCP sessions are all up after each move."""
    base = _baseline(ops)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    user, entries = uniq("vp"), []
    try:
        entries = ops.remove_compute(sysv, name)
        ops.create_vpod(user, entries)
        ops.wait_vpod(user)
        _recovered(ops, checks, base, "the move to a user vPOD", only={name})
    finally:
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)
    ops.wait_vpod(sysv)
    _recovered(ops, checks, base, "the move back", only={name})
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-TRG-009")
def test_vlan_creation_cp_unaffected(ops, afm_ready, uniq, checks, record_property):
    """Creating a user vPOD (new VLANs on every switch): the untouched computes stay healthy with their node
    agents connected and IFCP sessions up throughout."""
    base = _baseline(ops)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    others = [n for n in base if n != name]
    user, entries = uniq("vp"), []
    try:
        entries = ops.remove_compute(sysv, name)
        ops.create_vpod(user, entries)
        bad = _watch_cp(ops, 30 * float(ops.th("time_scale", 1)), others)
        for n in others:
            lost = sorted(base[n] - ops.ifcp_up(ops.tray(n)))
            if lost:
                bad.append(f"{n} lost {len(lost)} IFCP sessions")
        record_property("cp_problems", bad)
        checks.check(not bad, f"control plane disturbed by the VLAN creation: {bad}")
    finally:
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-TRG-010")
def test_compute_port_shut_link_down_recovery(ops, afm_ready, checks, record_property):
    """Port shut on the compute node: AFM sees the link go down (event, PortFailed after the threshold); port
    re-enabled: PortReady and the IFCP session are back."""
    base = _baseline(ops)
    name = ops.pick_compute()
    tray = ops.tray(name)
    bdf, port = sorted(base[name])[0]
    want = {(name, _afm_gpu_index(ops, name, tray, bdf), port)}
    since = ops.controller_now()
    sysv = ops.system_vpod_name()
    try:
        ops.compute_port_admin(tray, bdf, port, up=False)
        checks.check(ops.wait(lambda: [e for e in ops.events(None, since) if "down" in str(e.get("type"))], 120, 5),
                     "no link-down event")
        checks.check(ops.wait(lambda: want <= ops.failed_gpu_ports(), ops.link_fail_threshold(sysv) + 60, 5), "not PortFailed")
    finally:
        ops.compute_port_admin(tray, bdf, port, up=True)
    checks.check(ops.wait(lambda: not (want & ops.failed_gpu_ports()), 300, 10), "still PortFailed")
    _recovered(ops, checks, base, "the compute port restore", only={name})
    checks.assert_all()


# ------------------------------------------------------------------ GPU fault: other GPUs' control plane unaffected

@pytest.fixture
def gpu_fault_target(ops):
    """(compute, tray, bdf, AFM GPU index) to fault: thresholds.afm.target_compute (else the highest ready slot), its
    GPU with the most IFCP sessions up (thresholds.afm.fault_gpu_bdf to choose). Pre-flight: no other workload may be
    using that tray's GPUs (amd-smi process) - g02-g17 18 Oct, CT12 GPUs were running HPE gpu-collectives."""
    from helios import preflight

    name = ops.pick_compute()
    try:
        tray = ops.tray(name)
    except RuntimeError as e:
        pytest.fail(f"GPU fault pre-flight failed - {e}", pytrace=False)
    bad = _not_active(ops, tray)
    if bad:     # MR3 19 Oct: after the reset part, c2 0001:01:00.1 stayed PROVIDER / accelerator 0 for 11+ min
        pytest.fail(f"GPU fault pre-flight failed - {tray.name}: GPU(s) not ACTIVE (an earlier fault not "
                    f"recovered?): {bad}", pytrace=False)
    busy = preflight._gpu_users(tray, True) if ops.th("gpu_fault_busy_check", True) else ""
    if busy:
        pytest.fail(f"GPU fault pre-flight failed - not faulting a GPU in use: {busy}", pytrace=False)
    sessions = ops.ifcp_up(tray)
    per = {}
    for bdf, _p in sessions:
        per[bdf] = per.get(bdf, 0) + 1
    bdf = ops.th("fault_gpu_bdf", None) or (max(sorted(per), key=lambda b: per[b]) if per else None)
    if not bdf:
        pytest.fail(f"GPU fault pre-flight failed - {name}: no GPU with an IFCP session up", pytrace=False)
    return name, tray, bdf, _afm_gpu_index(ops, name, tray, bdf)


def _not_active(ops, tray) -> list[str]:
    return [f"{d.bdf} accelerator {d.accel_id} {d.config_phase}" for d in ops.afmctl(tray).devices()
            if d.config_phase != "ACTIVE"]


def _device(ops, tray, bdf: str):
    return next((d for d in ops.afmctl(tray).devices() if d.bdf == bdf), None)


def _watch_others(ops, base: dict, name: str, bdf: str, gidx, afm_before: set, window: float) -> tuple[list, list]:
    """Poll every tray for ``window`` s: IFCP sessions lost on any GPU other than (name, bdf), and new PortFailed GPU
    ports in AFM other than (name, gidx). Returns (lost sessions, new AFM failures), each de-duplicated."""
    lost, afm_new = set(), set()
    end = time.monotonic() + window
    while True:
        for n, want in base.items():
            have = ops.ifcp_up(ops.tray(n))
            lost |= {(n,) + k for k in want - have if not (n == name and k[0] == bdf)}
        afm_new |= {f for f in ops.failed_gpu_ports() - afm_before if not (f[0] == name and f[1] == gidx)}
        if time.monotonic() >= end:
            return sorted(lost), sorted(afm_new)
        time.sleep(5 * float(ops.th("time_scale", 1)))


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-TRG-012")
def test_gpu_reset_other_gpus_cp_unaffected(ops, afm_ready, gpu_fault_target, checks, record_property):
    """GPU fault by device reset (afmctl reset device -b <bdf>, one GPU): no IFCP session of any OTHER GPU on any
    tray drops and AFM marks no other GPU port PortFailed during thresholds.afm.gpu_fault_watch_s (90 s); the reset
    GPU's sessions are back within ifcp_recover_s afterwards. Skipped if afmctl cannot reset a single device."""
    name, tray, bdf, gidx = gpu_fault_target
    base = _baseline(ops)
    afm_before = ops.failed_gpu_ports()
    since = ops.tray_time(tray)
    acc0 = getattr(_device(ops, tray, bdf), "accel_id", None)
    record_property("target", {"compute": name, "tray": tray.name, "bdf": bdf, "afm_gpu": gidx, "accelerator": acc0,
                               "sessions": len([k for k in base[name] if k[0] == bdf])})
    r = tray.run(f"afmctl reset device -b {bdf}", sudo=True, timeout=300)
    if not r.ok and re.search(r"unknown|usage|invalid|not supported", r.stdout + r.stderr, re.I):
        pytest.skip(f"afmctl cannot reset one device on {tray.name}: {(r.stdout + r.stderr).strip()[:200]}")
    checks.check(r.ok, f"{tray.name}: afmctl reset device -b {bdf} -> rc {r.rc}: {(r.stdout + r.stderr).strip()[:200]}")
    lost, afm_new = _watch_others(ops, base, name, bdf, gidx, afm_before,
                                  float(ops.th("gpu_fault_watch_s", 90)) * float(ops.th("time_scale", 1)))
    record_property("other_gpus", {"sessions_lost": lost[:20], "afm_port_failed": afm_new[:20]})
    checks.check(not lost, f"{len(lost)} IFCP session(s) of other GPUs dropped during the reset of {tray.name} {bdf}: {lost[:6]}")
    checks.check(not afm_new, f"AFM marked {len(afm_new)} port(s) of other GPUs PortFailed: {afm_new[:6]}")
    checks.check(ops.reset_logged_since(tray, since), f"{tray.name}: no completed afmctl reset in the journal since {since}")
    _recovered(ops, checks, base, f"the reset of {bdf}", only={name})
    t = float(ops.th("gpu_reset_reprogram_s", 180))
    back = lambda: (lambda d: d is not None and d.config_phase == "ACTIVE" and d.accel_id == acc0)(_device(ops, tray, bdf))  # noqa: E731
    ok = ops.wait(back, t, 10)
    d = _device(ops, tray, bdf)
    record_property("after_reset", {"accelerator": getattr(d, "accel_id", None), "phase": getattr(d, "config_phase", None)})
    checks.check(ok, f"{tray.name} {bdf}: not re-programmed {t:.0f}s after the reset - accelerator "
                     f"{getattr(d, 'accel_id', None)} (was {acc0}), phase {getattr(d, 'config_phase', None)}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-TRG-012")
def test_gpu_isolated_other_gpus_cp_unaffected(ops, afm_ready, gpu_fault_target, checks, record_property):
    """GPU fault by isolation (every up port of one GPU set idle on the compute side, afmctl update port): that GPU's
    sessions all go down and AFM marks its ports PortFailed, while no session of any OTHER GPU drops and no other GPU
    port is PortFailed; ports enabled again -> every session back, nothing left PortFailed."""
    name, tray, bdf, gidx = gpu_fault_target
    base = _baseline(ops)
    afm_before = ops.failed_gpu_ports()
    mine = sorted(k for k in base[name] if k[0] == bdf)
    record_property("target", {"compute": name, "tray": tray.name, "bdf": bdf, "afm_gpu": gidx, "ports": len(mine)})
    sysv = ops.system_vpod_name()
    want_failed = {(name, gidx, p) for _b, p in mine}
    try:
        for _b, port in mine:
            ops.compute_port_admin(tray, bdf, port, up=False)
        down = ops.wait(lambda: not (set(mine) & ops.ifcp_up(tray)), 120, 5)
        checks.check(down, f"{tray.name} {bdf}: sessions still up after idling its ports: "
                           f"{sorted(set(mine) & ops.ifcp_up(tray))[:6]}")
        window = (ops.link_fail_threshold(sysv) + float(ops.th("gpu_fault_watch_s", 90))) * float(ops.th("time_scale", 1))
        lost, afm_new = _watch_others(ops, base, name, bdf, gidx, afm_before, window)
        record_property("other_gpus", {"sessions_lost": lost[:20], "afm_port_failed": afm_new[:20]})
        checks.check(not lost, f"{len(lost)} IFCP session(s) of other GPUs dropped while {tray.name} {bdf} was isolated: {lost[:6]}")
        checks.check(not afm_new, f"AFM marked {len(afm_new)} port(s) of other GPUs PortFailed: {afm_new[:6]}")
        missing = sorted(want_failed - ops.failed_gpu_ports())
        checks.check(gidx is None or not missing, f"isolated GPU ports not PortFailed in AFM: {missing[:6]}")
    finally:
        for _b, port in mine:
            ops.compute_port_admin(tray, bdf, port, up=True)
    _recovered(ops, checks, base, f"enabling {bdf} again", only={name})
    checks.check(ops.wait(lambda: not (want_failed & ops.failed_gpu_ports()), 300, 10), "isolated GPU ports still PortFailed")
    checks.assert_all()
