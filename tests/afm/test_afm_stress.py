"""AFM stress cases (DISRUPTIVE + stress, needs --allow-disruptive): vPOD configuration time at scale, bulk vPOD
operations, link event storm, vPOD create with ~5 % ports down, soak, policy attach without transient 0/0
planes, Reconcile at scale, simultaneous port failures (TC-STR-022) and mass decommission (TC-STR-009). Sizes and
durations come from thresholds.afm.* (defaults below are full-rack values; the soak defaults to 4 h).

TC-STR-022: thresholds.afm.str022_computes (6), str022_settle_s (60), reconfigure_event_types
([vpod_bandwidth_change]: the AFM event of one vPOD reprogramming, as TC-BUG-030 reads it).
TC-STR-009 runs only with thresholds.afm.stress.allow_mass_decommission: true (it takes every compute out of
service); str009_decommission_s (900), str009_watch_s (60), readmit_timeout_s.
"""
from __future__ import annotations

import time

import pytest

from afm_helpers import _baseline, _degraded_port_ids, _restored
from helios.afm import CLUSTER
from helios.afm_ops import CN, TERMINAL
from helios.rack import fan_out

FP = f"{CLUSTER}/failoverpolicies"
VP = f"{CLUSTER}/vpods"
pytestmark = [pytest.mark.l3, pytest.mark.disruptive, pytest.mark.stress]


def _ready(ops) -> list[str]:
    return [c["meta"]["name"] for c in sorted(ops.afm.computes(), key=lambda c: c["spec"].get("slot-id", 0))
            if ops.compute_ready(c["meta"]["name"])]


@pytest.mark.tc("TC-STR-005")
def test_system_vpod_configuration_time(ops, afm_ready, checks, record_property):
    """Every compute taken out of the system vPOD and put back: time until it is online/degraded with no pending
    ops <= thresholds.afm.str005_max_s (600 s); every GPU a member again."""
    sysv = ops.system_vpod_name()
    names = _ready(ops)
    moved = []
    try:
        for n in names:
            moved += ops.remove_compute(sysv, n)
        t0 = time.monotonic()
        ops.add_gpus(sysv, moved)
        moved = []
        ok = ops.wait(lambda: ops.vpod_state(sysv) in TERMINAL and not (ops.vpod(sysv).get("status") or {}).get("pending-ops"),
                      float(ops.th("str005_max_s", 600)), 5)
        took = round((time.monotonic() - t0) / float(ops.th("time_scale", 1)), 1)
        record_property("configure_s", took)
        checks.check(ok, f"{sysv} not configured within {ops.th('str005_max_s', 600)} s ({ops.vpod_state(sysv)})")
    finally:
        if moved:
            ops.add_gpus(sysv, moved)
    for n in names:
        checks.check(ops.wait(lambda n=n: ops.membership(n) == {sysv}, 300, 10), f"{n} not a member of {sysv}")
    checks.assert_all()


@pytest.mark.tc("TC-STR-011")
def test_bulk_vpod_operations(ops, afm_ready, uniq, checks, record_property):
    """thresholds.afm.str011_cycles (20) back-to-back create/delete cycles of a user vPOD per compute (up to
    3 in parallel paths): every cycle succeeds, the controller stays healthy, the system vPOD recovers."""
    from concurrent.futures import ThreadPoolExecutor

    sysv = ops.system_vpod_name()
    names = _ready(ops)[-int(ops.th("str011_computes", 3)):]
    cycles = int(ops.th("str011_cycles", 20))
    moved = {n: ops.remove_compute(sysv, n) for n in names}
    errors = []

    def churn(n):
        for i in range(cycles):
            v = uniq(f"b{names.index(n)}x{i}")    # per-compute names: threads may share a millisecond
            try:
                ops.create_vpod(v, moved[n])
                if ops.wait_vpod(v) not in TERMINAL:
                    errors.append(f"{n} cycle {i}: {ops.vpod_state(v)}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{n} cycle {i}: {e}"[:200])
            finally:
                ops.delete_vpod(v)
    try:
        with ThreadPoolExecutor(len(names)) as ex:
            list(ex.map(churn, names))
    finally:
        for n, e in moved.items():
            ops.add_gpus(sysv, e)
    record_property("cycles", {"computes": len(names), "per_compute": cycles, "errors": len(errors)})
    checks.check(not errors, f"{len(errors)} failed cycles: {errors[:4]}")
    from helios.afm import conditions_ok
    checks.check(conditions_ok(ops.afm.cluster()), "cluster not healthy after the churn")
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.tc("TC-STR-013")
def test_link_event_storm(ops, afm_ready, links, checks, record_property):
    """One port per compute (up to thresholds.afm.str013_computes, 6) flapped thresholds.afm.str013_flaps (5)
    times in quick succession: afterwards no GPU port is PortFailed, planes are back to the baseline and the
    system vPOD is online/degraded."""
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    sel = []
    for n in _ready(ops)[-int(ops.th("str013_computes", 6)):]:
        ls = ops.gpu_links(ops.compute(n)["spec"]["slot-id"])
        if ls:
            sel.append(ls[0])
    before = ops.failed_gpu_ports()
    sws = {sw.name: sw for sw, _, _, _ in sel}
    down0 = {n: ops.oper_down(sw) for n, sw in sws.items()}
    for _ in range(int(ops.th("str013_flaps", 5))):
        for sw, ns, port, _c in sel:
            ops.port_admin(sw, ns, port, up=False)
        for sw, ns, port, _c in sel:
            ops.port_admin(sw, ns, port, up=True)
    for sw, ns, port, _c in sel:
        checks.check(ops.wait_oper(sw, ns, port, "up", 120), f"{sw.name} {port} not up after the storm")
    for n, sw in sws.items():
        extra = ops.oper_down(sw) - down0[n]
        checks.check(not extra, f"{n}: {sorted(extra)} still down after the storm (coupled ports?)")
    checks.check(ops.wait(lambda: not (ops.failed_gpu_ports() - before), 300, 10),
                 f"PortFailed left after the storm: {sorted(ops.failed_gpu_ports() - before)[:5]}")
    _restored(ops, checks, sysv, base, "after the storm")
    record_property("ports", len(sel))
    checks.assert_all()


@pytest.mark.tc("TC-STR-019")
def test_vpod_with_ports_down(ops, afm_ready, links, uniq, checks, record_property):
    """~5 % (thresholds.afm.str019_down_pct) of the test computes' fabric ports down while their user vPOD is
    created: the vPOD comes up degraded, not configurationfailed; restored afterwards."""
    sysv = ops.system_vpod_name()
    names = _ready(ops)[-int(ops.th("vpod_test_computes", 1)):]
    pool = [x for n in names for x in ops.gpu_links(ops.compute(n)["spec"]["slot-id"])]
    k = max(1, round(len(pool) * float(ops.th("str019_down_pct", 5)) / 100))
    sel = pool[::max(1, len(pool) // k)][:k]
    user, moved = uniq("vp"), []
    try:
        for n in names:
            moved += ops.remove_compute(sysv, n)
        for sw, ns, port, _ in sel:
            links.shut(sw, ns, port)
        time.sleep((ops.link_fail_threshold(sysv) + 5) * float(ops.th("time_scale", 1)))
        ops.create_vpod(user, moved)
        st = ops.wait_vpod(user, ("degraded", "configurationfailed", "online"))
        record_property("result", {"ports_down": len(sel), "of": len(pool), "state": st})
        checks.check(st == "degraded", f"{user} {st} with {len(sel)}/{len(pool)} ports down, expected degraded")
    finally:
        links.restore_all()
        ops.delete_vpod(user)
        if moved:
            ops.add_gpus(sysv, moved)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.tc("TC-STR-020")
def test_soak_periodic_vpod_ops(ops, afm_ready, uniq, checks, record_property):
    """Soak for thresholds.afm.soak_s (14400 = 4 h): every thresholds.afm.soak_interval_s (300) move a compute
    into a user vPOD and back; every cycle succeeds and the cluster stays healthy throughout."""
    from helios.afm import conditions_ok

    sysv = ops.system_vpod_name()
    k = float(ops.th("time_scale", 1))
    end = time.monotonic() + float(ops.th("soak_s", 14400)) * k
    cycles, errors = 0, []
    name = ops.pick_compute()
    while time.monotonic() < end:
        v, entries = uniq(f"s{cycles}"), []
        try:
            entries = ops.remove_compute(sysv, name)
            ops.create_vpod(v, entries)
            if ops.wait_vpod(v) not in TERMINAL:
                errors.append(f"cycle {cycles}: {v} {ops.vpod_state(v)}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"cycle {cycles}: {e}"[:200])
        finally:
            ops.delete_vpod(v)
            if entries:
                ops.add_gpus(sysv, entries)
        if not conditions_ok(ops.afm.cluster()):
            errors.append(f"cycle {cycles}: cluster unhealthy")
        cycles += 1
        time.sleep(max(0.0, min(float(ops.th("soak_interval_s", 300)) * k, end - time.monotonic())))
    record_property("soak", {"cycles": cycles, "errors": len(errors)})
    checks.check(cycles > 0 and not errors, f"{len(errors)} of {cycles} soak cycles failed: {errors[:4]}")
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded after the soak")
    checks.assert_all()


@pytest.mark.tc("TC-STR-023")
def test_policy_attach_no_transient_zero_planes(ops, afm_ready, uniq, cleanup, checks, record_property):
    """Attaching a new (automatic) failover policy to the system vPOD and back: available planes, sampled about
    once a second, never read 0 and the vPOD stays online/degraded."""
    sysv = ops.system_vpod_name()
    pol = uniq("fp")
    cleanup(f"{FP}/{pol}")
    ops.afm.post(FP, {"meta": {"name": pol}, "spec": {"datafabric-reconfig": "automatic", "link-fail-threshold": 10}})
    orig = (ops.vpod(sysv).get("spec") or {}).get("failover-policy") or "system-failover-policy"
    samples = []

    def set_policy(name):
        v = ops.vpod(sysv)
        v["spec"]["failover-policy"] = name
        ops.afm.put(f"{VP}/{sysv}", {"kind": "VPod", "api-version": "v1", "meta": v["meta"], "spec": v["spec"]})
        end = time.monotonic() + 30 * float(ops.th("time_scale", 1)) + 0.5
        while time.monotonic() < end:
            samples.append((ops.planes(sysv)[1], ops.vpod_state(sysv)))
            time.sleep(min(1.0, 0.05 + float(ops.th("time_scale", 1))))
    try:
        set_policy(pol)
    finally:
        set_policy(orig)
    zeros = [s for s in samples if not s[0]]
    bad = {s[1] for s in samples} - set(TERMINAL)
    record_property("samples", {"n": len(samples), "min_planes": min((s[0] or 0) for s in samples) if samples else None})
    checks.check(not zeros, f"{len(zeros)} samples with 0 available planes during the policy change")
    checks.check(not bad, f"vPOD states seen during the policy change: {bad}")
    checks.check((ops.vpod(sysv).get("spec") or {}).get("failover-policy") == orig, "system vPOD policy not restored")
    checks.assert_all()


@pytest.mark.tc("TC-STR-024")
def test_reconcile_at_scale(ops, afm_ready, checks, record_property):
    """Reconcile of the system vPOD at full scale: afterwards every GPU is Ready, every switch healthy, no pending
    ops and the available planes at the baseline."""
    sysv = ops.system_vpod_name()
    base = ops.settled_planes(sysv, interval=float(ops.th("plane_settle_interval_s", 10)))
    t0 = time.monotonic()
    r = ops.afm.raw("POST", f"{VP}/{sysv}/Reconcile", {})
    checks.check(r.status_code == 200, f"Reconcile -> HTTP {r.status_code}")
    ok = ops.wait(lambda: ops.vpod_state(sysv) in TERMINAL and not (ops.vpod(sysv).get("status") or {}).get("pending-ops"),
                  600, 10)
    record_property("reconcile_s", round(time.monotonic() - t0, 1))
    checks.check(ok, f"{sysv} {ops.vpod_state(sysv)} after Reconcile")
    for c in ops.afm.computes():
        checks.check(ops.wait_compute_ready(c["meta"]["name"], 300), f"{c['meta']['name']} GPUs not all Ready")
    for s in ops.afm.switches():
        checks.check(ops.switch_ready(s["meta"]["name"]), f"{s['meta']['name']} not healthy")
    checks.check((ops.planes(sysv)[1] or 0) >= (base[1] or 0), f"planes {ops.planes(sysv)} below baseline {base}")
    checks.assert_all()


def _etype(e: dict):
    return e.get("type") or (e.get("spec") or {}).get("event-type") or (e.get("spec") or {}).get("type")


@pytest.mark.tc("TC-STR-022")
def test_simultaneous_port_failures_single_reconfigure(ops, afm_ready, links, checks, record_property):
    """One GPU link shut at the same moment on each of min(str022_computes, Ready computes) trays (a different GPU
    port id per tray where possible, never one whose plane is already degraded) and held past the link-fail
    threshold: every one of those GPU ports is PortFailed, the system vPOD's available planes drop, and AFM
    reprograms the vPOD ONCE - exactly one reconfigure_event_types event naming the system vPOD from the shut until
    str022_settle_s after the last port failed. Links restored: nothing left PortFailed, planes back to the baseline."""
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    bad, used, sel, want = _degraded_port_ids(ops), set(), [], set()
    for n in _ready(ops)[-int(ops.th("str022_computes", 6)):]:
        c = ops.compute(n)
        ls = ops.gpu_links(c["spec"]["slot-id"])
        good = [x for x in ls if x[3][3] not in bad]
        link = next((x for x in good if x[3][3] not in used), (good or ls or [None])[0])
        if link is None:
            continue
        cab = link[3]
        used.add(cab[3])
        gidx = next((g.get("index") for g in ops.gpu_info(c).values() if str(g.get("acc-id")) == str(cab[2])), cab[1])
        sel.append(link)
        want.add((n, gidx, cab[3]))
    if len(sel) < 2:
        pytest.skip(f"needs up GPU links on 2 Ready computes, found {len(sel)}")
    types = set(ops.th("reconfigure_event_types", ["vpod_bandwidth_change"]))
    record_property("ports", [f"{sw.name} {ns} {port} -> Slot{cab[0]}/GPU{cab[1]}/Port{cab[3]}" for sw, ns, port, cab in sel])
    since = ops.controller_now()
    try:
        for x, r, exc in fan_out(lambda x: links.shut(x[0], x[1], x[2]), sel, len(sel)):
            checks.check(exc is None and r.ok, f"{x[0].name} {x[2]}: shutdown failed: {exc or r.tail()}")
        failed = ops.wait(lambda: want <= ops.failed_gpu_ports(), ops.link_fail_threshold(sysv) + 120, 5)
        checks.check(failed, f"shut GPU ports not all PortFailed: missing {sorted(want - ops.failed_gpu_ports())}")
        time.sleep(float(ops.th("str022_settle_s", 60)) * float(ops.th("time_scale", 1)))
        planes = ops.planes(sysv)
        evs = [e for e in ops.events(None, since) if _etype(e) in types and ops.mentions(e, sysv)]
        record_property("result", {"planes": planes, "baseline": base, "reconfigure_events": len(evs),
                                   "events": [(_etype(e), (e.get("meta") or {}).get("creation-time"),
                                               str(e.get("message", ""))[:120]) for e in evs[:10]]})
        checks.check((planes[1] or 0) < (base[1] or 0), f"{sysv} available planes {planes} did not drop from {base}")
        checks.check(len(evs) == 1, f"{sysv} reconfigured {len(evs)} times for {len(sel)} simultaneous port failures "
                                    f"({sorted(types)} events), expected exactly 1")
    finally:
        for sw, ns, port, _ in sel:
            links.restore(sw, ns, port, checks)
    checks.check(ops.wait(lambda: not (want & ops.failed_gpu_ports()), 300, 10),
                 f"still PortFailed after the restore: {sorted(want & ops.failed_gpu_ports())}")
    _restored(ops, checks, sysv, base, "after the links came back")
    checks.assert_all()


def _controller_problems(ops) -> list[str]:
    """Controller health right now: cluster condition, every quorum member started, the API answering a list."""
    from helios.afm import conditions_ok

    try:
        cl = ops.afm.cluster()
    except Exception as e:  # noqa: BLE001 - an API that does not answer is the finding
        return [f"cluster read failed: {str(e)[:120]}"]
    out = [] if conditions_ok(cl) else ["cluster not healthy"]
    members = ((cl.get("status") or {}).get("quorum-status") or {}).get("members") or []
    down = [m.get("name") for m in members if m.get("status") != "started"]
    if down:
        out.append(f"quorum members not started: {down}")
    r = ops.afm.raw("GET", CN)
    if r.status_code != 200:
        out.append(f"GET computenodes -> HTTP {r.status_code}")
    return out


@pytest.mark.tc("TC-STR-009")
def test_mass_decommission_controller_survives(ops, afm_ready, checks, record_property):
    """Opt-in (thresholds.afm.stress.allow_mass_decommission: true): every Ready compute (18 on the full rack) taken
    out of the system vPOD and decommissioned in parallel. While they decommission and for str009_watch_s after,
    the controller stays healthy (cluster healthy, every quorum member started, the API answering). Then every
    compute is deleted and readmitted (afm-agent restarted once on each switch hosting their node agents), Ready
    within readmit_timeout_s and back in the system vPOD with the planes at the baseline."""
    if not ops.th("stress.allow_mass_decommission", False):
        pytest.skip("decommissions every compute: opt in with thresholds.afm.stress.allow_mass_decommission: true")
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    names = _ready(ops)
    hosts = {}
    for n in names:
        sw, _ = ops.slot_host(ops.compute(n)["spec"]["slot-id"])
        if sw is not None:
            hosts[sw.name] = sw
    entries, problems = {}, []
    record_property("computes", len(names))
    try:
        for n in names:
            entries[n] = ops.remove_compute(sysv, n)
        for n, r, exc in fan_out(lambda n: ops.afm.raw("POST", f"{CN}/{n}/Decommission", {}), names, len(names)):
            checks.check(exc is None and r.status_code == 200,
                         f"{n}: Decommission -> {exc or f'HTTP {r.status_code} {r.text[:150]}'}")

        def all_decommissioned():
            problems.extend(_controller_problems(ops))
            return all(ops.compute_phase(n) == "decommissioned" for n in names)
        ok = ops.wait(all_decommissioned, float(ops.th("str009_decommission_s", 900)), 10)
        phases = {n: ops.compute_phase(n) for n in names}
        checks.check(ok, f"not every compute decommissioned: {({n: p for n, p in phases.items() if p != 'decommissioned'})}")
        k = float(ops.th("time_scale", 1))
        end = time.monotonic() + float(ops.th("str009_watch_s", 60)) * k
        while True:
            problems.extend(_controller_problems(ops))
            if time.monotonic() >= end:
                break
            time.sleep(min(10 * k, max(0.05, end - time.monotonic())))
        record_property("controller_problems", sorted(set(problems))[:20])
        checks.check(not problems, f"controller disturbed by the mass decommission: {sorted(set(problems))[:6]}")
    finally:
        for n in names:
            if ops.compute_phase(n) not in ("admitted", "absent"):
                ops.afm.raw("DELETE", f"{CN}/{n}")
        ops.wait(lambda: all(ops.compute_phase(n) in ("admitted", "absent") for n in names), 120, 5)
        for sw in hosts.values():
            ops.afm_agent(sw, "restart")
        back = [n for n in names if ops.wait_compute_ready(n)]
        checks.check(len(back) == len(names), f"not readmitted: {sorted(set(names) - set(back))}")
        ops.add_gpus(sysv, [e for n in back for e in entries.get(n, [])])
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded after the readmit")
    for n in names:
        checks.check(ops.wait(lambda n=n: ops.membership(n) == {sysv}, 300, 10), f"{n} not back in {sysv}")
    _restored(ops, checks, sysv, base, "after readmitting every compute")
    checks.assert_all()
