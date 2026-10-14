"""Compute-side AFM cases (DISRUPTIVE unless noted): afmctl on the compute trays together with the AFM API -
compute link flap and port-down events, station path maps, forwarding tables, GPU config phases, accelerator
reset + Reconfigure, the node-agent lifecycle (NA-xxx) and the sideband bring-up sequence (SEQ/BU).

AFM ComputeNodes are matched to inventory trays by accelerator id (or ``afm_name:`` on the tray). Compute
ports are disabled with ``AFMCTL_FORCE_UPDATE=1 afmctl update port ... --admin-state idle`` and always
re-enabled; the station of a port is port // 2 (verified on MR3).
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from helios.afm import CLUSTER, conditions_ok
from helios.afm_ops import CN, TERMINAL

VP = f"{CLUSTER}/vpods"


def _need_sideband(ops):
    if not any(ops.sb_agents(sw) for sw in ops.rack.switches.values()):
        pytest.skip("not in sideband control mode")


def _target(ops):
    name = ops.pick_compute()
    return name, ops.tray(name)


def _up_port(ops, tray, bdf=None):
    """(bdf, port) of a healthy IFCP port, preferring GPU 0."""
    up = sorted(ops.ifcp_up(tray))
    if bdf:
        up = [x for x in up if x[0] == bdf]
    if not up:
        pytest.fail(f"{tray.name}: no LINK_UP port with IFCP up")
    return up[0]


class ComputePorts:
    """Disable compute ports through afmctl and always re-enable them."""

    def __init__(self, ops, tray):
        self.ops, self.tray, self.down = ops, tray, []

    def off(self, bdf, port):
        r = self.ops.compute_port_admin(self.tray, bdf, port, up=False)
        if r.ok:
            self.down.append((bdf, port))
        return r

    def on(self, bdf, port):
        self.ops.compute_port_admin(self.tray, bdf, port, up=True)
        if (bdf, port) in self.down:
            self.down.remove((bdf, port))

    def restore(self):
        for bdf, port in list(self.down):
            self.on(bdf, port)


def _down_events(ops, since):
    return [e for e in ops.events(None, since) if e.get("type") in ("link_down", "ifoe_netport_link_down")]


def _up_events(ops, since):
    return [e for e in ops.events(None, since) if e.get("type") in ("link_up", "ifoe_netport_link_up")]


# ================================================================== compute ports / events

@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-AFM-009")
def test_compute_link_flap(ops, afm_ready, checks, record_property):
    """One IFoE port disabled on the compute (afmctl, admin idle) and re-enabled: AFM raises a link-down event
    then a link-up event, and the port's IFCP session is back."""
    name, tray = _target(ops)
    bdf, port = _up_port(ops, tray)
    cp = ComputePorts(ops, tray)
    since = ops.controller_now()
    try:
        r = cp.off(bdf, port)
        checks.check(r.ok, f"afmctl update port idle -> rc {r.rc}: {r.tail()!r}")
        checks.check(ops.wait(lambda: _down_events(ops, since), 120, 5), "no link-down event in AFM")
    finally:
        cp.restore()
    checks.check(ops.wait(lambda: _up_events(ops, since), 120, 5), "no link-up event in AFM")
    checks.check(ops.wait(lambda: (bdf, port) in ops.ifcp_up(tray), float(ops.th("ifcp_recover_s", 120)), 5),
                 f"{bdf} port {port}: IFCP not back after re-enable")
    record_property("events", sorted({e.get("type") for e in ops.events(None, since)}))
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-E2E-092")
def test_event_compute_side_port_down(ops, afm_ready, checks, record_property):
    """A port down on the compute side: AFM raises a link-down event and, after the link-fail threshold, the GPU
    port is PortFailed; re-enabled: PortReady again."""
    name, tray = _target(ops)
    bdf, port = _up_port(ops, tray)
    gpu = int(bdf[3]) - 1                       # 0001:01:00.1 -> GPU 0 (AFM GPU index order = BDF order)
    want = {(name, gpu, port)}
    cp = ComputePorts(ops, tray)
    since = ops.controller_now()
    sysv = ops.system_vpod_name()
    try:
        cp.off(bdf, port)
        checks.check(ops.wait(lambda: _down_events(ops, since), 120, 5), "no link-down event")
        checks.check(ops.wait(lambda: want <= ops.failed_gpu_ports(), ops.link_fail_threshold(sysv) + 60, 5),
                     f"{name} GPU{gpu} port {port} not PortFailed")
    finally:
        cp.restore()
    checks.check(ops.wait(lambda: not (want & ops.failed_gpu_ports()), 300, 10), "port still PortFailed after re-enable")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-AFM-058")
def test_station_map_after_link_triggers(ops, afm_ready, links, checks, record_property):
    """afmctl show station path-to-port map of the port's station after four triggers - switch link down, link
    flap, AFM DrainGPUPort, ActivateGPUPort: the port is out of the map while down/drained and back after."""
    name, tray = _target(ops)
    slot = ops.compute(name)["spec"]["slot-id"]
    sw, ns, swport, cab = next(x for x in ops.gpu_links(slot) if x[3][1] == 0)
    gpu, port = cab[1], cab[3]
    bdf = f"000{gpu + 1}:01:00.1"
    st = ops.station(cab)

    def in_map():
        return port in ops.station_map(tray, bdf).get(st, set())
    seen = {"baseline": in_map()}
    links.shut(sw, ns, swport)
    seen["link_down_removed"] = bool(ops.wait(lambda: not in_map(), 120, 5))
    links.restore(sw, ns, swport, checks)
    seen["link_up_restored"] = bool(ops.wait(in_map, 120, 5))
    links.shut(sw, ns, swport)
    links.restore(sw, ns, swport, checks)
    seen["flap_restored"] = bool(ops.wait(in_map, 120, 5))
    body = {"ports": [{"gpu-id": gpu, "port-id": port}]}
    try:
        ops.afm.post(f"{CN}/{name}/DrainGPUPort", body)
        seen["drain_removed"] = bool(ops.wait(lambda: not in_map(), 120, 5))
    finally:
        ops.afm.post(f"{CN}/{name}/ActivateGPUPort", body)
    seen["activate_restored"] = bool(ops.wait(in_map, 180, 5))
    record_property("station_map", {"gpu": gpu, "station": st, "port": port, **seen})
    for k, v in seen.items():
        checks.check(v, f"station {st} map wrong at '{k}'")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-060")
def test_station_map_matches_port_state(ops, afm_ready, checks, record_property):
    """For every GPU and station, the live (status) path-to-port map holds exactly the station's ports that are
    LINK_UP with IFCP up, and is a subset of the configured (spec) map."""
    bad = []
    for c in ops.afm.computes():
        tray = ops.tray(c["meta"]["name"])
        up = ops.ifcp_up(tray)
        for d in ops.afmctl(tray).devices():
            live, spec = ops.station_map(tray, d.bdf), ops.station_map(tray, d.bdf, "spec")
            for stn, ports in live.items():
                spec_ports = spec.get(stn, set())
                want = {p for p in spec_ports if (d.bdf, p) in up}
                if ports != want or not ports <= spec_ports:
                    bad.append(f"{tray.name}/{d.bdf} station {stn}: map {sorted(ports)}, up {sorted(want)}, spec {sorted(spec_ports)}")
    record_property("mismatches", bad[:20])
    checks.check(not bad, f"{len(bad)} station maps disagree with port state: {bad[:4]}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-E2E-099")
def test_forwarding_table_after_port_flap(ops, afm_ready, links, checks, record_property):
    """A switch-side flap of one GPU port: afterwards the compute's destination table for that port
    (afmctl show port accelerator-addr) is complete again - same accelerators and MACs as before."""
    name, tray = _target(ops)
    slot = ops.compute(name)["spec"]["slot-id"]
    sw, ns, swport, cab = ops.gpu_links(slot)[0]
    bdf, port = f"000{cab[1] + 1}:01:00.1", cab[3]
    before = ops.afmctl(tray).accel_addr().get(bdf, {}).get(port, {})
    checks.check(before, f"{bdf} port {port}: empty destination table before the flap")
    links.shut(sw, ns, swport)
    links.restore(sw, ns, swport, checks)
    ok = ops.wait(lambda: ops.afmctl(tray).accel_addr().get(bdf, {}).get(port, {}) == before, 180, 10)
    after = ops.afmctl(tray).accel_addr().get(bdf, {}).get(port, {})
    record_property("entries", {"before": len(before), "after": len(after)})
    checks.check(ok, f"{bdf} port {port}: destination table not restored ({len(after)} of {len(before)} entries)")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-E2E-110")
def test_gpu_config_phase_on_configfailed(ops, afm_ready, uniq, checks, record_property):
    """A user vPOD created while every afm-agent is stopped goes configurationfailed: its GPUs are not ACTIVE in
    afmctl, the other computes' GPUs stay ACTIVE; after the agents start, all are ACTIVE again."""
    sysv = ops.system_vpod_name()
    name, tray = _target(ops)
    others = [ops.tray(c["meta"]["name"]) for c in ops.afm.computes() if c["meta"]["name"] != name]
    user, entries = uniq("vp"), []
    try:
        entries = ops.remove_compute(sysv, name)
        for sw in ops.rack.switches.values():
            ops.afm_agent(sw, "stop")
        time.sleep(float(ops.th("agent_settle_s", 30)))
        ops.create_vpod(user, entries)
        st = ops.wait_vpod(user, ("configurationfailed",), 60)
        checks.check(st == "configurationfailed", f"{user} {st} with agents down")
        ph = ops.phases(tray)
        record_property("phases_failed_vpod", ph)
        checks.check(all(v != "ACTIVE" for v in ph.values()), f"{tray.name} GPUs ACTIVE in a configfailed vPOD: {ph}")
        for t in others:
            p2 = ops.phases(t)
            checks.check(all(v == "ACTIVE" for v in p2.values()), f"{t.name} (untouched) GPUs not ACTIVE: {p2}")
    finally:
        for sw in ops.rack.switches.values():
            ops.afm_agent(sw, "start")
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait(lambda: all(v == "ACTIVE" for v in ops.phases(tray).values()), 300, 10),
                 f"{tray.name} GPUs not ACTIVE after restore: {ops.phases(tray)}")
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


# ================================================================== reset / reconfigure (node agent)

def _reconfigure_and_check(ops, name, tray, checks):
    t0 = ops.tray_time(tray)
    r = ops.reconfigure(name)
    checks.check(r.status_code == 200, f"Reconfigure -> HTTP {r.status_code}: {r.text[:150]}")
    checks.check(ops.wait_compute_ready(name, 300), f"{name} GPUs not Ready after Reconfigure")
    return t0


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-AFM-RC-007")
def test_reconfigure_after_accelerator_reset(ops, afm_ready, checks, record_property):
    """afmctl reset device -b <bdf> on one GPU (journal confirms), the GPU returns to Ready, then Reconfigure of
    the tray: 200, all GPUs Ready and ACTIVE."""
    name, tray = _target(ops)
    bdf = sorted(ops.phases(tray))[0]
    t0 = ops.tray_time(tray)
    r = tray.run(f"afmctl reset device -b {bdf}", sudo=True, timeout=300)
    checks.check(r.ok and "reset successfully" in r.stdout, f"afmctl reset -> rc {r.rc}: {r.tail()!r}")
    checks.check(ops.reset_logged_since(tray, t0), "journalctl shows no completed afmctl reset")
    checks.check(ops.wait_compute_ready(name, 120), f"{name} GPUs not Ready after the reset")
    _reconfigure_and_check(ops, name, tray, checks)
    checks.check(ops.wait(lambda: all(v == "ACTIVE" for v in ops.phases(tray).values()), 180, 10),
                 f"{tray.name} GPUs not ACTIVE: {ops.phases(tray)}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-047")
def test_reconfigure_runs_afmctl_reset(ops, afm_ready, checks, record_property):
    """Reconfigure of a compute resets its devices: journalctl -t afmctl shows a completed reset after the call."""
    name, tray = _target(ops)
    t0 = _reconfigure_and_check(ops, name, tray, checks)
    checks.check(ops.wait(lambda: ops.reset_logged_since(tray, t0), 120, 10), "no afmctl reset logged after Reconfigure")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-HW-025")
def test_gpu_reset_via_reconfigure(ops, afm_ready, checks, record_property):
    """HW-022: GPU reset through AFM Reconfigure - afmctl reset logged, every GPU back (amd-smi list shows all
    of them) and Ready in AFM."""
    name, tray = _target(ops)
    t0 = _reconfigure_and_check(ops, name, tray, checks)
    checks.check(ops.wait(lambda: ops.reset_logged_since(tray, t0), 120, 10), "no reset logged")
    r = tray.run("amd-smi list", sudo=True, timeout=120)
    n = r.stdout.count("GPU:")
    record_property("amd_smi_gpus", n)
    checks.check(r.ok and n >= int(tray.host.extra.get("gpus", 4)), f"amd-smi list shows {n} GPUs (rc {r.rc})")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-048")
def test_afmctl_state_after_reconfigure(ops, afm_ready, checks, record_property):
    """After Reconfigure: afmctl accelerator ids equal AFM's acc-ids, vpod_accelerators equal the system vPOD's
    accelerators, and every up port's destination table lists every other vPOD accelerator."""
    name, tray = _target(ops)
    _reconfigure_and_check(ops, name, tray, checks)
    a = ops.afmctl(tray)
    afm_acc = {str(g.get("acc-id")) for g in ops.gpu_info(ops.compute(name)).values()}
    devs = a.devices()
    checks.check({d.accel_id for d in devs} == afm_acc, f"afmctl acc ids {sorted(d.accel_id for d in devs)} vs AFM {sorted(afm_acc)}")
    sysv = ops.system_vpod_name()
    vacc = {str(g.get("acc-id")) for c in ops.afm.computes() for g in ops.gpu_info(c).values()
            if g.get("vpod-membership") == sysv}
    va = a.vpod_accels()
    for bdf, (acc, accs) in va.items():
        checks.check(set(accs) == vacc, f"{bdf}: vpod_accelerators {len(accs)} vs AFM {len(vacc)}")
    up = ops.ifcp_up(tray)
    table = a.accel_addr()
    for bdf, port in sorted(up):
        acc = next(d.accel_id for d in devs if d.bdf == bdf)
        want = vacc - {acc}
        got = set(table.get(bdf, {}).get(port, {}))
        if got != want:
            checks.fail(f"{bdf} port {port}: {len(got)} destinations, expected {len(want)}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-049")
def test_reconfigure_only_when_healthy(ops, afm_ready, checks, record_property):
    """Reconfigure is accepted on a healthy compute (200) and rejected (4xx) while its node agent is paused."""
    _need_sideband(ops)
    name, tray = _target(ops)
    _reconfigure_and_check(ops, name, tray, checks)
    slot = ops.compute(name)["spec"]["slot-id"]
    sw, pid = ops.slot_host(slot)
    try:
        ops.signal_sb_agent(sw, pid, "STOP")
        ops.wait(lambda: not conditions_ok(ops.compute(name)), float(ops.th("agent_down_detect_s", 600)), 20)
        r = ops.reconfigure(name)
        record_property("unhealthy_reconfigure", f"HTTP {r.status_code} {r.text[:150]}")
        checks.check(400 <= r.status_code < 500, f"Reconfigure on unhealthy {name} -> HTTP {r.status_code}")
    finally:
        sw2, pid2 = ops.slot_host(slot)
        ops.signal_sb_agent(sw2 or sw, pid2 or pid, "CONT")
    checks.check(ops.wait_compute_ready(name, 300), f"{name} not healthy after resume")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-040")
def test_reconfigure_multiple_computes(ops, afm_ready, checks, record_property):
    """Reconfigure of several computes at once (thresholds.afm.na040_computes, 2): all accepted, all Ready."""
    names = [c["meta"]["name"] for c in ops.afm.computes()][-int(ops.th("na040_computes", 2)):]
    with ThreadPoolExecutor(len(names)) as ex:
        res = dict(zip(names, ex.map(lambda n: ops.reconfigure(n), names)))
    record_property("status", {n: r.status_code for n, r in res.items()})
    for n, r in res.items():
        checks.check(r.status_code == 200, f"Reconfigure {n} -> HTTP {r.status_code}")
        checks.check(ops.wait_compute_ready(n, 300), f"{n} not Ready")
    checks.check(ops.wait_vpod(ops.system_vpod_name()) in TERMINAL, "system vPOD not online/degraded")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-022")
def test_fabric_reconfigure_reported(ops, afm_ready, checks, record_property):
    """A data-fabric reconfiguration (ReconfigureDataFabric on the system vPOD) is reported: a vPOD event names
    the vPOD and the reconfiguration, and the vPOD returns to online/degraded."""
    sysv = ops.system_vpod_name()
    since = ops.controller_now()
    r = ops.afm.raw("POST", f"{VP}/{sysv}/ReconfigureDataFabric", {})
    checks.check(r.status_code == 200, f"ReconfigureDataFabric -> HTTP {r.status_code}")
    ev = ops.wait(lambda: [e for e in ops.events(None, since) if sysv in json.dumps(e)
                           and "reconfigure" in json.dumps(e).lower()], 180, 10)
    record_property("events", [e.get("type") for e in ev or []])
    checks.check(ev, "no event reporting the fabric reconfiguration")
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-016")
def test_update_vpod_constituent_computes(ops, afm_ready, uniq, checks, record_property):
    """A user vPOD's computes are swapped (A out, B in) with one PUT: memberships follow, vPOD online/degraded."""
    names = [c["meta"]["name"] for c in ops.afm.computes() if ops.compute_ready(c["meta"]["name"])][-2:]
    if len(names) < 2:
        pytest.skip("needs 2 ready computes")
    a, b = names
    sysv = ops.system_vpod_name()
    user, ea, eb = uniq("vp"), [], []
    try:
        ea = ops.remove_compute(sysv, a)
        eb = ops.remove_compute(sysv, b)
        ops.create_vpod(user, ea)
        ops.wait_vpod(user)
        v = ops.vpod(user)
        v["spec"]["GPUs"] = [{k: e[k] for k in ("id", "slot-id", "index")} for e in eb]
        ops._retry_400(lambda: ops.afm.raw("PUT", f"{VP}/{user}", {"kind": "VPod", "api-version": "v1",
                                                                     "meta": v["meta"], "spec": v["spec"]}))
        checks.check(ops.wait(lambda: ops.membership(b) == {user} and ops.membership(a) <= {""}, 120, 10),
                     f"memberships after swap: {a} {ops.membership(a)}, {b} {ops.membership(b)}")
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} {ops.vpod_state(user)}")
    finally:
        ops.delete_vpod(user)
        for e in (ea, eb):
            if e:
                ops.add_gpus(sysv, e)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


# ================================================================== compute lifecycle (node agent)

def _readmit(ops, name, slot):
    sw, _ = ops.slot_host(slot)
    if sw is not None:
        ops.afm_agent(sw, "restart")
    return ops.wait_compute_ready(name)


def _cycle(ops, checks, name, *, observe=None, force=False):
    """remove from vPOD -> decommission -> DELETE -> readmit -> back in the system vPOD."""
    sysv = ops.system_vpod_name()
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    try:
        ops.afm.raw("POST", f"{CN}/{name}/Decommission", {"force-decommission": True} if force else {})
        ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
        if observe:
            observe("decommissioned")
        ops.afm.raw("DELETE", f"{CN}/{name}")
        ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5)
        if observe:
            observe("deleted")
        checks.check(_readmit(ops, name, slot), f"{name} not readmitted")
        if observe:
            observe("readmitted")
    finally:
        if ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-018")
def test_readd_compute_reuses_acc_ids(ops, afm_ready, checks, record_property):
    """A compute decommissioned, deleted and readmitted comes back with the same accelerator ids."""
    _need_sideband(ops)
    name = ops.pick_compute()
    before = sorted(g.get("acc-id") for g in ops.gpu_info(ops.compute(name)).values())
    _cycle(ops, checks, name)
    after = sorted(g.get("acc-id") for g in ops.gpu_info(ops.compute(name)).values())
    record_property("acc_ids", {"before": before, "after": after})
    checks.check(after == before, f"acc-ids changed on readmit: {before} -> {after}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-024")
def test_compute_states(ops, afm_ready, checks, record_property):
    """Every compute state is reported: admitted+healthy, admitted+unhealthy (agent paused), decommissioned,
    deleted (absent) and readmitted+healthy."""
    _need_sideband(ops)
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    seen = {}

    def obs(label):
        r = ops.afm.raw("GET", f"{CN}/{name}")
        o = r.json() if r.status_code == 200 else {}
        seen[label] = (ops.compute_phase(name), ((o.get("status") or {}).get("conditions") or [{}])[0].get("status"))
    obs("start")
    sw, pid = ops.slot_host(slot)
    try:
        ops.signal_sb_agent(sw, pid, "STOP")
        ops.wait(lambda: not conditions_ok(ops.compute(name)), float(ops.th("agent_down_detect_s", 600)), 20)
        obs("agent_paused")
    finally:
        sw2, pid2 = ops.slot_host(slot)
        ops.signal_sb_agent(sw2 or sw, pid2 or pid, "CONT")
    ops.wait_compute_ready(name, 300)
    _cycle(ops, checks, name, observe=obs)
    record_property("states", seen)
    want = {"start": ("admitted", "true"), "decommissioned": ("decommissioned", None), "deleted": ("absent", None),
            "readmitted": ("admitted", "true")}
    for k, (ph, health) in want.items():
        got = seen.get(k, (None, None))
        checks.check(got[0] == ph and (health is None or got[1] == health), f"{k}: {got}, expected ({ph}, {health})")
    checks.check(seen.get("agent_paused", ("", "true"))[1] != "true", f"agent paused but health {seen.get('agent_paused')}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-013")
def test_force_decommission_unhealthy(ops, afm_ready, checks, record_property):
    """Compute out of its vPOD with the node agent paused: plain Decommission ends in a failed-decommission
    state (not decommissioned); force-decommission then succeeds; the compute is deleted and readmitted."""
    _need_sideband(ops)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    sw, pid = ops.slot_host(slot)
    try:
        ops.signal_sb_agent(sw, pid, "STOP")
        ops.wait(lambda: not conditions_ok(ops.compute(name)), float(ops.th("agent_down_detect_s", 600)), 20)
        r = ops.afm.raw("POST", f"{CN}/{name}/Decommission", {})
        ph = ops.wait(lambda: (lambda p: p if p in ("decommission_failed", "decommissioned") else None)(ops.compute_phase(name)),
                      300, 10) or ops.compute_phase(name)
        record_property("plain_decommission", {"http": r.status_code, "phase": ph})
        checks.check(ph != "decommissioned" or r.status_code >= 400, f"plain decommission of an unhealthy compute -> {ph}")
        r = ops.afm.raw("POST", f"{CN}/{name}/Decommission", {"force-decommission": True})
        checks.check(r.status_code == 200, f"force-decommission -> HTTP {r.status_code}")
        checks.check(ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10),
                     f"{name} {ops.compute_phase(name)} after force-decommission")
    finally:
        sw2, pid2 = ops.slot_host(slot)
        ops.signal_sb_agent(sw2 or sw, pid2 or pid, "CONT")
        if ops.compute_phase(name) not in ("admitted", "absent"):
            ops.afm.raw("DELETE", f"{CN}/{name}")
        if ops.compute_phase(name) != "admitted":
            checks.check(_readmit(ops, name, slot), f"{name} not readmitted")
        if ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-050")
def test_reconfigure_during_admission(ops, afm_ready, checks, record_property):
    """Compute deleted and readmitted; Reconfigure sent right after the agent restart (initial config still being
    pushed): answered 200 or 4xx (never 5xx), and the compute ends Ready."""
    _need_sideband(ops)
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    codes = []
    try:
        ops.afm.raw("POST", f"{CN}/{name}/Decommission", {})
        ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
        ops.afm.raw("DELETE", f"{CN}/{name}")
        ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5)
        sw, _ = ops.slot_host(slot)
        ops.afm_agent(sw, "restart")
        end = time.monotonic() + 120 * float(ops.th("time_scale", 1)) + 1
        while time.monotonic() < end and ops.compute_phase(name) == "absent":
            time.sleep(0.2)
        codes.append(ops.afm.raw("POST", f"{CN}/{name}/Reconfigure", {}).status_code)
        checks.check(all(c < 500 for c in codes), f"Reconfigure during admission -> {codes}")
        checks.check(ops.wait_compute_ready(name), f"{name} not Ready after admission")
    finally:
        if ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    record_property("reconfigure_codes", codes)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


# ================================================================== sideband sequence / bring-up (read-only)

@pytest.mark.l3
@pytest.mark.tc("TC-SEQ-005")
def test_node_agent_launched(ops, checks, record_property):
    """Sideband Manager launched one node agent per admitted compute: exactly one sb-node-agent process per slot
    across the switches, each pointing at controllers that are cluster quorum members."""
    _need_sideband(ops)
    quorum = set(ops.quorum_ips())
    slots = {c["spec"]["slot-id"] for c in ops.afm.computes() if (c.get("status") or {}).get("admission-phase") == "admitted"}
    found = {}
    for sw in ops.rack.switches.values():
        r = sw.run("docker exec afm-agent ps -eo pid,args", sudo=True, timeout=60)
        for line in r.stdout.splitlines():
            if "sb-node-agent" in line and "-slot-id" in line:
                toks = line.split()
                slot = int(toks[toks.index("-slot-id") + 1])
                ctl = toks[toks.index("-controllers") + 1].split(",") if "-controllers" in toks else []
                found.setdefault(slot, []).append((sw.name, ctl))
    record_property("agents", {s: [h for h, _ in v] for s, v in found.items()})
    for s in slots:
        checks.check(len(found.get(s, [])) == 1, f"slot {s}: {len(found.get(s, []))} sb-node-agents")
        for host, ctl in found.get(s, []):
            checks.check(ctl and set(ctl) <= quorum, f"slot {s} on {host}: controllers {ctl} not in quorum {sorted(quorum)}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-SEQ-010")
def test_compute_registered(ops, inventory, checks, record_property):
    """Every compute tray in the inventory is registered in AFM: a ComputeNode whose acc-ids match the tray,
    admitted, with a slot id."""
    names = {}
    for c in ops.afm.computes():
        try:
            names[ops.tray(c["meta"]["name"]).name] = c
        except RuntimeError:
            pass
    record_property("registered", {t: c["meta"]["name"] for t, c in names.items()})
    for h in inventory.computes:
        c = names.get(h.name)
        if checks.check(c, f"{h.name}: no AFM ComputeNode with its accelerator ids"):
            checks.check((c.get("status") or {}).get("admission-phase") == "admitted", f"{h.name}: not admitted")
            checks.check((c.get("spec") or {}).get("slot-id") is not None, f"{h.name}: no slot-id")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-SEQ-012")
def test_node_manager_api_responding(ops, checks, record_property):
    """The node agent's operational API answers for every admitted compute slot: /api/runtime/requests returns
    JSON on the switch hosting that slot's agent."""
    _need_sideband(ops)
    bad = []
    for c in ops.afm.computes():
        slot = c["spec"]["slot-id"]
        sw, _ = ops.slot_host(slot)
        if sw is None:
            bad.append(f"slot {slot}: no agent")
            continue
        if sw.agent_api(13000 + slot, "/api/runtime/requests") is None:
            bad.append(f"slot {slot} on {sw.name}: no JSON from /api/runtime/requests")
    checks.check(not bad, f"node-manager API not responding: {bad}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-BU-001")
def test_switches_admitted_before_computes(ops, checks, record_property):
    """Bring-up order: the first switch node was admitted (created) before any compute node."""
    sw = sorted((s["meta"].get("creation-time") or "") for s in ops.afm.switches())
    cn = sorted((c["meta"].get("creation-time") or "") for c in ops.afm.computes())
    record_property("first", {"switch": sw[0] if sw else None, "compute": cn[0] if cn else None})
    checks.check(sw and cn and sw[0] <= cn[0], f"first compute {cn[:1]} created before the first switch {sw[:1]}")
    checks.assert_all()
