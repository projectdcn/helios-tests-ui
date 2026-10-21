"""AFM failover scenarios (TC-E2E, FO-xxx) and vPOD operations (DISRUPTIVE, needs --allow-disruptive).

Fabric ports are shut on the switch side (``config interface ... shutdown``) on trunk ports cabled to a
GPU. A GPU's IFoE station is ``gpu_port // thresholds.afm.ports_per_station`` (2: ``afmctl show station``
maps station k to ports 2k, 2k+1 - verified on MR3 c1, 2026-10-13).
Failed GPU ports are read from every compute (``PortFailed``); planes are the vPOD's
``available-switch-planes``. Every port shut is brought back by the ``links`` fixture, user vPODs and
policies are deleted, GPUs go back to the system vPOD. Policy-second timings scale with
thresholds.afm.time_scale (1 on a rack).
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from afm_helpers import (_baseline, _reconfigure_after_restore, _restored, _scale,  # noqa: E402
                         _user_vpod_ctx, _user_vpod_restore)
from helios.afm import CLUSTER
from helios.afm_ops import TERMINAL

FP = f"{CLUSTER}/failoverpolicies"
VP = f"{CLUSTER}/vpods"
pytestmark = [pytest.mark.l2, pytest.mark.disruptive]


# ------------------------------------------------------------------ helpers

def _cname(ops, slot: int) -> str:
    return next(c["meta"]["name"] for c in ops.afm.computes() if (c.get("spec") or {}).get("slot-id") == slot)


def _expected(ops, sel) -> set[tuple]:
    return {(_cname(ops, cab[0]), cab[1], cab[3]) for _, _, _, cab in sel}


def _tray_view(ops, slot: int, gpu: int) -> tuple[dict[int, dict], str | None]:
    """The GPU's own view of its ports, read on the tray: {GPU port id: {link, admin, ll_up, peer_enabled, error}}
    from `afmctl show port` (the GPU found through AFM's acc-id for that index), or ({}, why) when unreadable."""
    try:
        name = _cname(ops, slot)
        acc = next(str(g.get("acc-id")) for g in ops.gpu_info(ops.compute(name)).values() if g.get("index") == gpu)
        tray = ops.tray(name)
        bdf = next((d.bdf for d in ops.afmctl(tray).devices() if d.accel_id == acc), None)
        if bdf is None:
            return {}, f"{tray.name}: no afmctl device with accelerator id {acc}"
        return {port: v for (b, port), v in ops.ifcp(tray).items() if b == bdf}, None
    except Exception as e:  # noqa: BLE001 - evidence only: a tray that does not answer must not decide the case
        return {}, f"tray view unavailable: {str(e).splitlines()[0][:200]}"


def _full_gpu(ops) -> tuple[int, int, list, dict]:
    """(slot, gpu, up links, census) of a GPU whose cabled switch ports are ALL up (avoid_ports excepted): the target
    tray's GPUs first, then the other ready trays'. census = {"slot/gpu": "up/cabled"} of every GPU looked at. One
    status + description read per switch ASIC. FO-037 cuts a GPU off completely - a GPU already missing links would
    test a partial cut (g02-g17 21 Oct: ct18 GPU0 had 9 of 36 ports up, the rack 25 of 36 planes)."""
    from helios.afmctl import parse_port_description

    avoid = set(ops.th("avoid_ports", []) or [])
    cabled: dict[tuple, int] = {}
    up: dict[tuple, list] = {}
    for sw in ops.rack.switches.values():
        for ns in sw.namespaces():
            desc = sw.port_descriptions(ns)
            for r in sw.interfaces_status(ns):
                port = r.get("Interface")
                cab = parse_port_description(desc.get(port, ""))
                if not cab or f"{sw.name}:{port}" in avoid:
                    continue
                key = (cab[0], cab[1])
                cabled[key] = cabled.get(key, 0) + 1
                if r.get("Vlan") == "trunk" and r.get("Oper") == "up":
                    up.setdefault(key, []).append((sw, ns, port, cab))
    target = _target(ops)
    ready = sorted({c["spec"]["slot-id"] for c in ops.active_computes()
                    if ops.compute_ready(c["meta"]["name"])} - {target})
    census = {}
    for slot in [target] + ready:
        for gpu in range(4):
            n, have = cabled.get((slot, gpu), 0), len(up.get((slot, gpu), []))
            census[f"{slot}/{gpu}"] = f"{have}/{n}"
            if n and have == n:
                return slot, gpu, sorted(up[(slot, gpu)], key=lambda x: x[3][3]), census
    return -1, -1, [], census


def _shut(links, sel):
    for sw, ns, port, _ in sel:
        links.shut(sw, ns, port)


def _restore(links, checks, sel):
    for sw, ns, port, _ in sel:
        links.restore(sw, ns, port, checks)


def _fail_window(ops, vpod, extra: float = 60) -> float:
    return ops.link_fail_threshold(vpod) + extra


def _target(ops) -> int:
    return ops.compute(ops.pick_compute())["spec"]["slot-id"]


def _gpu_with_stations(ops, slot, n_stations, both_ports=0):
    """(gpu, {station: links}) for the first GPU with >= n_stations stations, of which >= both_ports have
    both ports up."""
    for gpu in range(4):
        st = ops.stations(slot, gpu)
        full = [s for s, ls in st.items() if len(ls) >= 2]
        if len(st) >= n_stations and len(full) >= both_ports:
            return gpu, st
    pytest.skip(f"slot {slot}: no GPU with {n_stations} stations up ({both_ports} with both ports up)")


def _still_up(ops, sel, timeout: float = 60) -> list[str]:
    """Shut switch ports of ``sel`` not oper down within ``timeout`` (one status read per switch ASIC per poll).
    g02-g17 17 Oct TC-E2E-144: GPU0 ports 34/35 (st1 asic0 Ethernet186, st2 asic0 Ethernet187) never went PortFailed
    and the trimmed log could not show whether the switch ports went down - now it is checked and recorded."""
    groups: dict[tuple, list] = {}
    for sw, ns, port, _ in sel:
        groups.setdefault((sw.name, ns), []).append((sw, port))

    def up():
        out = []
        for (name, ns), items in groups.items():
            oper = {r.get("Interface"): r.get("Oper") for r in items[0][0].interfaces_status(ns)}
            out += [f"{name}/{ns}/{p}" for _, p in items if oper.get(p) != "down"]
        return out
    left = up()
    end = time.monotonic() + timeout * _scale(ops)
    while left and time.monotonic() < end:
        time.sleep(max(0.05, 2 * _scale(ops)))
        left = up()
    return left


def _planes_after(ops, vpod, changed, timeout: float) -> int:
    """Available planes once AFM has acted on the last link change: wait (``timeout``) for a terminal state with
    ``changed(planes, previous)``, then for the value to settle. g02-g17 17 Oct TC-E2E-141: PortFailed comes ~2 s
    after a shut but the reconfiguration only after the link-fail threshold; reading the planes at once (the vPOD
    was 'degraded' already) gave failing [33, 33, 33] and recovering [33, 32, 32] - the late reconfigurations."""
    ops.wait(lambda: ops.vpod_state(vpod) in TERMINAL and changed(ops.planes(vpod)[1] or 0), timeout, 5)
    return ops.settled_planes(vpod, interval=float(ops.th("plane_settle_interval_s", 10)))[1] or 0


def _events_for(ops, etype, since, needles) -> set[str]:
    """Which of ``needles`` appear (as a whole name: Ethernet35 is not Ethernet354) in an event of ``etype`` newer
    than ``since``."""
    evs = ops.events(etype, since)
    return {n for n in needles if any(ops.mentions(e, n) for e in evs)}


def _run_failure(ops, links, checks, record_property, sel, vpod=None, sequential=False):
    """Shut ``sel``: every expected GPU port goes PortFailed, the vPOD degrades (not configurationfailed) with
    fewer planes; restore: ports PortReady, planes back to the baseline."""
    vpod = vpod or ops.system_vpod_name()
    base = _baseline(ops, vpod, record_property)
    want = _expected(ops, sel)
    before = ops.failed_gpu_ports()
    record_property("ports", [f"{sw.name}/{ns}/{p} -> {c}" for sw, ns, p, c in sel])
    _shut(links, sel)
    ops.wait(lambda: want <= ops.failed_gpu_ports() - before, _fail_window(ops, vpod), 5)
    missing = want - (ops.failed_gpu_ports() - before)
    checks.check(not missing, f"GPU ports not PortFailed: {sorted(missing)}")
    ok = ops.wait(lambda: ops.vpod_state(vpod) == "degraded" and (ops.planes(vpod)[1] or 0) < (base[1] or 0), 120, 5)
    record_property("during", {"state": ops.vpod_state(vpod), "planes": ops.planes(vpod)})
    checks.check(ok, f"{vpod} {ops.vpod_state(vpod)} planes {ops.planes(vpod)} (baseline {base}): expected degraded with fewer planes")
    _restore(links, checks, sel)
    _restored(ops, checks, vpod, base)
    checks.check(ops.wait(lambda: not (want & ops.failed_gpu_ports()), 120, 10),
                 f"still PortFailed after restore: {sorted(want & ops.failed_gpu_ports())}")
    return base


# ================================================================== station failure patterns

@pytest.mark.tc("TC-E2E-122")
def test_fo003_both_ports_same_station(ops, afm_ready, links, checks, record_property):
    """FO-003: both ports of one IFoE station of one GPU down -> both PortFailed, vPOD degraded; recovers."""
    slot = _target(ops)
    gpu, st = _gpu_with_stations(ops, slot, 1, both_ports=1)
    sel = next(ls for ls in st.values() if len(ls) >= 2)[:2]
    _run_failure(ops, links, checks, record_property, sel)
    checks.assert_all()


@pytest.mark.tc("TC-E2E-130")
def test_fo013_single_port_multiple_stations(ops, afm_ready, links, checks, record_property):
    """FO-013: one port on each of up to 3 stations of one GPU (partial station failures)."""
    slot = _target(ops)
    gpu, st = _gpu_with_stations(ops, slot, 2)
    sel = [ls[0] for _, ls in sorted(st.items())[:3]]
    _run_failure(ops, links, checks, record_property, sel)
    checks.assert_all()


@pytest.mark.tc("TC-E2E-131")
def test_fo014_full_and_partial_station(ops, afm_ready, links, checks, record_property):
    """FO-014: one station fully down (both ports) + one port of another station."""
    slot = _target(ops)
    gpu, st = _gpu_with_stations(ops, slot, 2, both_ports=1)
    full = next(s for s, ls in sorted(st.items()) if len(ls) >= 2)
    other = next(s for s in sorted(st) if s != full)
    sel = st[full][:2] + st[other][:1]
    _run_failure(ops, links, checks, record_property, sel)
    checks.assert_all()


@pytest.mark.tc("TC-E2E-137")
def test_fo026_multiple_trays_simultaneously(ops, afm_ready, links, checks, record_property):
    """FO-026: one port on each of 2+ compute trays at the same time -> PortFailed on every tray."""
    slots = sorted((c["spec"]["slot-id"] for c in ops.afm.computes() if ops.compute_ready(c["meta"]["name"])))
    if len(slots) < 2:
        pytest.skip("needs 2 ready compute trays")
    sel = []
    for s in slots[-int(ops.th("fo026_trays", 3)):]:
        ls = ops.gpu_links(s)
        if ls:
            sel.append(ls[0])
    _run_failure(ops, links, checks, record_property, sel)
    checks.assert_all()


@pytest.mark.tc("TC-E2E-144")
def test_fo037_all_stations_one_gpu(ops, afm_ready, links, checks, record_property):
    """FO-037: every up port of one GPU shut in rapid sequence: all PortFailed, the vPOD never reaches
    operation-failed, and everything recovers after the ports return. With the ports down, all three views of each
    link are recorded (three_views): the switch port (oper), the GPU port read on the tray (afmctl: link / IFCP) and
    AFM (oper-state) - g02-g17 17 Oct: ports 22-27 / 34-35 of ct18 GPU0 not PortFailed with every switch port down;
    the tray's own view was not recorded then.

    Pre-flight: the GPU must have ALL its cabled links up (_full_gpu: the target tray first, then any ready tray), else
    BLOCKED - a GPU already missing links is a partial cut, not FO-037 (g02-g17 21 Oct: 9 of 36). The three views of
    every port are recorded before the cut too (before_views), and ports AFM already marks failed while the switch
    has them up are reported (afm_failed_before_cut)."""
    slot, gpu, sel, census = _full_gpu(ops)
    record_property("gpu_ports_up", census)
    if not sel:
        best = max(census.items(), key=lambda kv: int(kv[1].split("/")[0]), default=("-", "0/0"))
        pytest.fail(f"FO-037 pre-flight failed - no GPU with all its links up (best: slot/GPU {best[0]} {best[1]} up; "
                    f"checked {len(census)} GPUs) - a full cut-off cannot be tested; the rack's down links need fixing "
                    f"first", pytrace=False)
    record_property("target", f"slot {slot} GPU{gpu}: {len(sel)} of {len(sel)} links up")
    cap = int(ops.th("fo037_max_ports", 0))
    sel = sel[:cap] if cap else sel
    tray0, why0 = _tray_view(ops, slot, gpu)
    failed0 = ops.failed_gpu_ports()
    name = _cname(ops, slot)
    record_property("before_views", {f"GPU port {c[3]}": {
        "switch": f"{sw.name}/{ns}/{p} up", "tray": (f"{tray0[c[3]].get('link')} ll_up={tray0[c[3]].get('ll_up')}"
                                                     if c[3] in tray0 else (why0 or "not read")),
        "afm": "PortFailed" if (name, c[1], c[3]) in failed0 else "not failed"} for sw, ns, p, c in sel})
    stale = sorted(c[3] for *_x, c in sel if (name, c[1], c[3]) in failed0)
    record_property("afm_failed_before_cut", stale)
    checks.check(not stale, f"{name} GPU{gpu}: AFM marks port(s) {stale} PortFailed while their switch ports are up "
                            f"(before the cut)")
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    want = _expected(ops, sel)
    _shut(links, sel)
    up = _still_up(ops, sel)
    ok = ops.wait(lambda: want <= ops.failed_gpu_ports(), _fail_window(ops, sysv), 5)
    st = ops.vpod_state(sysv)
    record_property("with_all_ports_down", {"ports": len(sel), "state": st, "planes": ops.planes(sysv),
                                            "switch_ports_not_down": up})
    checks.check(not up, f"switch ports still oper up after shutdown: {up}")
    failed = ops.failed_gpu_ports()
    missing = sorted(want - failed)
    tray, why = _tray_view(ops, slot, gpu)
    oper: dict[tuple, str] = {}
    for sw, ns in {(sw, ns) for sw, ns, _p, _c in sel}:       # one status read per switch ASIC
        rows = {r.get("Interface"): r.get("Oper") for r in sw.interfaces_status(ns)}
        oper.update({(sw.name, ns, p): rows.get(p) for s2, n2, p, _c in sel if s2 is sw and n2 == ns})
    views = {}
    for sw, ns, p, c in sel:
        afm_state = "PortFailed" if (_cname(ops, c[0]), c[1], c[3]) in failed else "not failed"
        tv = tray.get(c[3]) or {}
        views[f"GPU port {c[3]}"] = {"switch": f"{sw.name}/{ns}/{p} {oper.get((sw.name, ns, p)) or 'not listed'}",
                                     "tray": (f"{tv.get('link')} ll_up={tv.get('ll_up')}" if tv else (why or "not read")),
                                     "afm": afm_state}
    record_property("three_views", views)
    if why:
        record_property("tray_view_error", why)
    tray_up = sorted(port for port, v in tray.items() if any(port == c[3] for *_, c in sel) and v.get("link") == "LINK_UP")
    record_property("tray_link_up_while_switch_down", tray_up)
    shown = [f"port {m[2]}: switch {views[f'GPU port {m[2]}']['switch']}, tray {views[f'GPU port {m[2]}']['tray']}, AFM not failed"
             for m in missing]
    checks.check(ok, f"not all {len(sel)} GPU ports PortFailed: {len(missing)} missing - " + "; ".join(shown[:12])
                     + (f" (+{len(shown) - 12} more, see three_views)" if len(shown) > 12 else ""))
    checks.check(st != "operation-failed", f"{sysv} operation-failed with one GPU fully disconnected")
    _restore(links, checks, sel)
    _restored(ops, checks, sysv, base)
    checks.check(ops.wait(lambda: not (want & ops.failed_gpu_ports()), 120, 10), "ports still PortFailed after restore")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-141")
def test_fo033_sequential_failures(ops, afm_ready, links, checks, record_property):
    """FO-033: 3 ports (different stations) fail one after another: planes never go up while failing, never down
    while recovering, and end at the baseline."""
    slot = _target(ops)
    gpu, st = _gpu_with_stations(ops, slot, 3)
    sel = [ls[0] for _, ls in sorted(st.items())[:3]]
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    down_seq, up_seq, prev = [], [], base[1]
    for link in sel:
        want = _expected(ops, [link])
        _shut(links, [link])
        checks.check(ops.wait(lambda: want <= ops.failed_gpu_ports(), _fail_window(ops, sysv), 5),
                     f"{link[2]} not PortFailed")
        down_seq.append(prev := _planes_after(ops, sysv, lambda p, q=prev: p < q, _fail_window(ops, sysv) + 60))
    for link in sel:
        _restore(links, checks, [link])
        ops.wait(lambda: not (_expected(ops, [link]) & ops.failed_gpu_ports()), 120, 5)
        up_seq.append(prev := _planes_after(ops, sysv, lambda p, q=prev: p > q, _fail_window(ops, sysv) + 60))
    record_property("planes", {"baseline": base[1], "failing": down_seq, "recovering": up_seq})
    checks.check(all(a >= b for a, b in zip([base[1]] + down_seq, down_seq)), f"planes rose while failing: {down_seq}")
    checks.check(down_seq and down_seq[-1] < base[1], f"planes {down_seq} never below baseline {base[1]}")
    checks.check(all(a <= b for a, b in zip(up_seq, up_seq[1:])), f"planes fell while recovering: {up_seq}")
    _restored(ops, checks, sysv, base)
    checks.assert_all()


@pytest.mark.tc("TC-E2E-135")
def test_fo024_second_port_during_reconfigure(ops, afm_ready, links, checks, record_property):
    """FO-024: a second port (other station) fails right as AFM reconfigures for the first: both end
    PortFailed, the vPOD settles degraded (not configurationfailed), and recovers."""
    slot = _target(ops)
    gpu, st = _gpu_with_stations(ops, slot, 2)
    a, b = [ls[0] for _, ls in sorted(st.items())[:2]]
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    want_a, want = _expected(ops, [a]), _expected(ops, [a, b])
    _shut(links, [a])
    ops.wait(lambda: want_a <= ops.failed_gpu_ports(), _fail_window(ops, sysv), 1)
    _shut(links, [b])                         # immediately: AFM is reprogramming for port A
    ok = ops.wait(lambda: want <= ops.failed_gpu_ports(), _fail_window(ops, sysv), 5)
    checks.check(ok, f"not both PortFailed: missing {sorted(want - ops.failed_gpu_ports())}")
    settled = ops.wait(lambda: ops.vpod_state(sysv) == "degraded" and not (ops.vpod(sysv).get("status") or {}).get("pending-ops"),
                       180, 5)
    record_property("final", {"state": ops.vpod_state(sysv), "planes": ops.planes(sysv)})
    checks.check(settled, f"{sysv} {ops.vpod_state(sysv)} after the second failure, expected degraded with no pending ops")
    _restore(links, checks, [a, b])
    _restored(ops, checks, sysv, base)
    checks.assert_all()


@pytest.mark.tc("TC-E2E-105")
def test_multiple_compute_ports_down_notifications(ops, afm_ready, links, checks, record_property):
    """Several ports of one compute tray (different switches/ASICs where cabled) go down: a link_down event
    per port, then PortFailed for each; after restore a link_up per port."""
    slot = _target(ops)
    pool = ops.gpu_links(slot)
    sel, seen = [], set()
    for link in pool:                              # spread over switches/ASICs first
        if (link[0].name, link[1]) not in seen:
            sel.append(link)
            seen.add((link[0].name, link[1]))
    sel = (sel + [x for x in pool if x not in sel])[:int(ops.th("e2e105_ports", 3))]
    since = ops.controller_now()
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    _shut(links, sel)
    ports = {p for _, _, p, _ in sel}
    ops.wait(lambda: _events_for(ops, "link_down", since, ports) == ports, 60, 10)
    got = _events_for(ops, "link_down", since, ports)
    checks.check(got == ports, f"link_down events missing for {sorted(ports - got)}")
    want = _expected(ops, sel)
    checks.check(ops.wait(lambda: want <= ops.failed_gpu_ports(), _fail_window(ops, sysv), 5),
                 f"not PortFailed: {sorted(want - ops.failed_gpu_ports())}")
    _restore(links, checks, sel)
    ops.wait(lambda: _events_for(ops, "link_up", since, ports) == ports, 60, 10)
    got = _events_for(ops, "link_up", since, ports)
    checks.check(got == ports, f"link_up events missing for {sorted(ports - got)}")
    _restored(ops, checks, sysv, base)
    checks.assert_all()


@pytest.mark.tc("TC-E2E-048")
def test_multiple_links_one_switch_events(ops, afm_ready, links, checks, record_property):
    """2+ links between one compute and one switch go down and come back: link_down and link_up events for
    each port."""
    slot = _target(ops)
    by_sw: dict[str, list] = {}
    for link in ops.gpu_links(slot):
        by_sw.setdefault(link[0].name, []).append(link)
    sel = next((ls[:3] for ls in by_sw.values() if len(ls) >= 2), None)
    if not sel:
        pytest.skip(f"slot {slot}: no switch with 2 up links to it")
    since = ops.controller_now()
    sysv = ops.system_vpod_name()
    base = _baseline(ops, sysv, record_property)
    ports = {p for _, _, p, _ in sel}
    _shut(links, sel)
    ops.wait(lambda: _events_for(ops, "link_down", since, ports) == ports, 60, 10)
    got = _events_for(ops, "link_down", since, ports)
    checks.check(got == ports, f"link_down missing for {sorted(ports - got)}")
    _restore(links, checks, sel)
    ops.wait(lambda: _events_for(ops, "link_up", since, ports) == ports, 60, 10)
    got = _events_for(ops, "link_up", since, ports)
    checks.check(got == ports, f"link_up missing for {sorted(ports - got)}")
    _restored(ops, checks, sysv, base)
    checks.assert_all()


# ================================================================== timers / policies

@pytest.mark.tc("TC-E2E-145")
def test_fo038_automatic_golden_path(ops, afm_ready, links, checks, record_property):
    """FO-038, automatic mode end to end with no user action: link_down -> (threshold) link_failed + PortFailed
    -> vPOD degraded with one plane less (never needdatafabricreconfiguration) -> link_up -> PortReady ->
    planes and state back to the baseline."""
    sysv = ops.system_vpod_name()
    slot = _target(ops)
    sel = ops.gpu_links(slot)[:1]
    port = sel[0][2]
    since = ops.controller_now()
    states = set()
    base = _baseline(ops, sysv, record_property)
    want = _expected(ops, sel)
    _shut(links, sel)
    checks.check(ops.wait(lambda: port in _events_for(ops, "link_down", since, {port}), 60, 5), "no link_down event")

    def failed():
        states.add(ops.vpod_state(sysv))
        return want <= ops.failed_gpu_ports()
    checks.check(ops.wait(failed, _fail_window(ops, sysv), 2), "port not PortFailed")
    checks.check(ops.wait(lambda: port in _events_for(ops, "link_failed", since, {port}), 60, 5), "no link_failed event")
    checks.check(ops.wait(lambda: (states.add(ops.vpod_state(sysv)) or ops.vpod_state(sysv) == "degraded")
                          and (ops.planes(sysv)[1] or 0) < (base[1] or 0), 120, 5),
                 f"{sysv} not auto-degraded: {ops.vpod_state(sysv)} {ops.planes(sysv)}")
    _restore(links, checks, sel)
    checks.check(ops.wait(lambda: port in _events_for(ops, "link_up", since, {port}), 60, 5), "no link_up event")
    checks.check(ops.wait(lambda: not (want & ops.failed_gpu_ports()), 120, 5), "port still PortFailed")
    _restored(ops, checks, sysv, base)
    record_property("states_seen", sorted(s for s in states if s))
    checks.check("needdatafabricreconfiguration" not in states, "automatic mode waited for manual reconfiguration")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-056")
def test_link_recovers_after_degraded(ops, afm_ready, links, checks, record_property):
    """After the vPOD was reconfigured to degraded for a failed link, bringing the link back restores the planes
    and state automatically (no Reconcile / ReconfigureDataFabric)."""
    slot = _target(ops)
    sel = ops.gpu_links(slot)[:1]
    _run_failure(ops, links, checks, record_property, sel)
    checks.assert_all()


def _manual_hold(ops, links, checks, record_property, uniq, cleanup, hold_s: float):
    sysv, user, pol, names, moved = _user_vpod_ctx(
        ops, uniq, cleanup, {"datafabric-reconfig": "manual", "link-fail-threshold": 10})
    try:
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} not online/degraded")
        base = _baseline(ops, user, record_property)
        slots = {ops.compute(n)["spec"]["slot-id"] for n in names}
        sel = ops.gpu_links(min(slots))[:1]
        _shut(links, sel)
        need = ops.wait(lambda: ops.vpod_state(user) == "needdatafabricreconfiguration", _fail_window(ops, user), 2)
        checks.check(need, f"{user} {ops.vpod_state(user)}: manual policy did not wait for the user")
        planes_waiting = ops.planes(user)
        end = time.monotonic() + hold_s * _scale(ops)
        while time.monotonic() < end:
            if ops.vpod_state(user) != "needdatafabricreconfiguration":
                checks.fail(f"{user} went {ops.vpod_state(user)} without ReconfigureDataFabric")
                break
            time.sleep(min(10 * _scale(ops) + 0.05, max(0.05, end - time.monotonic())))
        record_property("planes", {"baseline": base, "waiting": planes_waiting})
        # available planes may drop by the failed link(s) - AFM noticing the failure, not a reconfiguration (that is
        # the state held above + reprogram_df only after ReconfigureDataFabric). g02-g17 17 Oct: (36,36,36) ->
        # (36,35,18) while waiting, 36 -> 35 failed the old 'unchanged' check; effective 18 is reported to AMD.
        checks.check(base[1] - len(sel) <= planes_waiting[1] <= base[1],
                     f"available planes changed by more than the {len(sel)} failed link(s) before user action: "
                     f"{base} -> {planes_waiting}")
        r = ops.afm.raw("POST", f"{VP}/{user}/ReconfigureDataFabric", {})
        checks.check(r.status_code == 200, f"ReconfigureDataFabric -> HTTP {r.status_code}")
        checks.check(ops.wait(lambda: ops.vpod_state(user) == "degraded", 300, 5),
                     f"{user} {ops.vpod_state(user)} after ReconfigureDataFabric")
        _restore(links, checks, sel)
        _reconfigure_after_restore(ops, checks, user, base, 10)
        _restored(ops, checks, user, base)
    finally:
        links.restore_all()
        _user_vpod_restore(ops, sysv, user, moved)


@pytest.mark.tc("TC-E2E-146")
def test_fo039_manual_no_auto_reconfigure(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """FO-039: manual policy - after a link failure the vPOD stays in needdatafabricreconfiguration with
    unchanged planes for thresholds.afm.manual_hold_s (120 s); only ReconfigureDataFabric reconfigures."""
    _manual_hold(ops, links, checks, record_property, uniq, cleanup, float(ops.th("manual_hold_s", 120)))
    checks.assert_all()


@pytest.mark.tc("TC-E2E-052")
def test_manual_fabric_reconfiguration_port_down(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """Manual fabric reconfiguration when a switch port goes down (20 s hold, then ReconfigureDataFabric)."""
    _manual_hold(ops, links, checks, record_property, uniq, cleanup, 20)
    checks.assert_all()


def _custom_policy_ctx(ops, uniq, cleanup, mode="automatic", th=None):
    th = th or int(ops.th("custom_link_fail_threshold", 30))
    return _user_vpod_ctx(ops, uniq, cleanup, {"datafabric-reconfig": mode, "link-fail-threshold": th}) + (th,)


@pytest.mark.tc("TC-E2E-055")
def test_link_failed_event_after_threshold(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """link_failed is raised after the policy's link-fail-threshold (custom 30 s): not before threshold - 5 s,
    and by threshold + 20 s."""
    sysv, user, pol, names, moved, th = _custom_policy_ctx(ops, uniq, cleanup)
    try:
        slot = min(ops.compute(n)["spec"]["slot-id"] for n in names)
        sel = ops.gpu_links(slot)[:1]
        port = sel[0][2]
        since = ops.controller_now()
        t0 = time.monotonic()
        _shut(links, sel)
        ok = ops.wait(lambda: port in _events_for(ops, "link_failed", since, {port}), th + 20, 1)
        took = (time.monotonic() - t0) / _scale(ops)
        record_property("link_failed_after_s", round(took, 1))
        checks.check(ok, f"no link_failed for {port} within {th + 20}s")
        checks.check(not ok or took >= th - 5, f"link_failed after {took:.1f}s, before the {th}s threshold")
    finally:
        links.restore_all()
        _user_vpod_restore(ops, sysv, user, moved)
    checks.assert_all()


@pytest.mark.tc("TC-E2E-132")
def test_fo021_recover_before_threshold(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """FO-021: the port comes back 10 s before the 30 s link-fail-threshold: no PortFailed, no link_failed,
    planes untouched - also after the threshold would have expired."""
    sysv, user, pol, names, moved, th = _custom_policy_ctx(ops, uniq, cleanup)
    try:
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} not online/degraded")
        base = _baseline(ops, user, record_property)
        slot = min(ops.compute(n)["spec"]["slot-id"] for n in names)
        sel = ops.gpu_links(slot)[:1]
        port = sel[0][2]
        since = ops.controller_now()
        before = ops.failed_gpu_ports()
        _shut(links, sel)
        early = set()
        end = time.monotonic() + (th - 10) * _scale(ops)
        while time.monotonic() < end:          # watch the whole down window, not just the end
            early |= ops.failed_gpu_ports() - before
            time.sleep(min(5 * _scale(ops) + 0.05, max(0.05, end - time.monotonic())))
        checks.check(not early, f"PortFailed {sorted(early)} while down for less than the {th}s threshold")
        _restore(links, checks, sel)
        time.sleep(20 * _scale(ops))           # past where the threshold would have expired
        new = ops.failed_gpu_ports() - before
        checks.check(not new, f"PortFailed although the port recovered before the threshold: {sorted(new)}")
        checks.check(port not in _events_for(ops, "link_failed", since, {port}), "link_failed raised for a short flap")
        checks.check(ops.planes(user)[1] == base[1], f"planes {ops.planes(user)} changed (baseline {base})")
    finally:
        links.restore_all()
        _user_vpod_restore(ops, sysv, user, moved)
    checks.assert_all()


@pytest.mark.tc("TC-E2E-133")
def test_fo022_change_mode_while_port_down(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """FO-022: manual policy, port goes down, policy switched to automatic before the threshold: when the link
    fails AFM reconfigures on its own (degraded, never needdatafabricreconfiguration)."""
    sysv, user, pol, names, moved, th = _custom_policy_ctx(ops, uniq, cleanup, mode="manual")
    try:
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} not online/degraded")
        base = _baseline(ops, user, record_property)
        slot = min(ops.compute(n)["spec"]["slot-id"] for n in names)
        sel = ops.gpu_links(slot)[:1]
        _shut(links, sel)
        time.sleep(5 * _scale(ops))
        p = ops.afm.get(f"{FP}/{pol}")
        ops.afm.put(f"{FP}/{pol}", {"meta": p["meta"], "spec": {**p["spec"], "datafabric-reconfig": "automatic"}})
        states = set()
        ok = ops.wait(lambda: (states.add(ops.vpod_state(user)) or ops.vpod_state(user)) == "degraded",
                      th + 60, 2)
        record_property("states_seen", sorted(s for s in states if s))
        checks.check(ok, f"{user} {ops.vpod_state(user)}: not auto-reconfigured after switching to automatic")
        checks.check("needdatafabricreconfiguration" not in states, "still waited for manual action after the mode change")
        _restore(links, checks, sel)
        _restored(ops, checks, user, base)
    finally:
        links.restore_all()
        _user_vpod_restore(ops, sysv, user, moved)
    checks.assert_all()


@pytest.mark.tc("TC-E2E-134")
def test_fo023_remove_policy_while_degraded(ops, afm_ready, links, uniq, cleanup, checks, record_property):
    """FO-023: deleting the failover policy of a degraded vPOD is rejected while the vPOD uses it; the vPOD
    stays degraded and recovers when the port returns."""
    sysv, user, pol, names, moved, th = _custom_policy_ctx(ops, uniq, cleanup, th=10)
    try:
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} not online/degraded")
        base = _baseline(ops, user, record_property)
        slot = min(ops.compute(n)["spec"]["slot-id"] for n in names)
        sel = ops.gpu_links(slot)[:1]
        _shut(links, sel)
        checks.check(ops.wait(lambda: ops.vpod_state(user) == "degraded", th + 60, 2), f"{user} not degraded")
        r = ops.afm.raw("DELETE", f"{FP}/{pol}")
        record_property("delete_policy", f"HTTP {r.status_code} {r.text[:150]}")
        checks.check(400 <= r.status_code < 500, f"DELETE of in-use policy -> HTTP {r.status_code}, expected 4xx")
        checks.check(ops.vpod_state(user) in TERMINAL, f"{user} {ops.vpod_state(user)} after the policy delete attempt")
        _restore(links, checks, sel)
        _restored(ops, checks, user, base)
    finally:
        links.restore_all()
        _user_vpod_restore(ops, sysv, user, moved)
    checks.assert_all()


# ================================================================== vPOD operations

def _ready_slots(ops):
    return sorted(c["meta"]["name"] for c in ops.afm.computes() if ops.compute_ready(c["meta"]["name"]))


@pytest.mark.tc("TC-E2E-030")
def test_move_multiple_computes_to_user_vpod(ops, afm_ready, uniq, checks, record_property):
    """Move 2+ computes from the system vPOD into one user vPOD: all GPUs members, vPOD online/degraded;
    moved back afterwards."""
    names = _ready_slots(ops)[-int(ops.th("e2e030_computes", 2)):]
    if len(names) < 2:
        pytest.skip("needs 2 ready computes")
    sysv = ops.system_vpod_name()
    user, moved = uniq("vp"), []
    try:
        for n in names:
            moved += ops.remove_compute(sysv, n)
        ops.create_vpod(user, moved)
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} {ops.vpod_state(user)}")
        for n in names:
            checks.check(ops.membership(n) == {user}, f"{n} membership {ops.membership(n)}")
        checks.check(len(ops.vpod_gpus(user)) == len(moved), f"{user} has {len(ops.vpod_gpus(user))} GPUs, moved {len(moved)}")
    finally:
        ops.delete_vpod(user)
        if moved:
            ops.add_gpus(sysv, moved)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-033")
def test_system_vpod_fully_populated(ops, afm_ready, checks, record_property):
    """After a compute is moved out and back, the system vPOD again holds every GPU of every admitted compute;
    re-applying the full GPU list is accepted and changes nothing."""
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    entries = ops.remove_compute(sysv, name)
    ops.add_gpus(sysv, entries)
    ops.wait_vpod(sysv)
    # a tray quarantined in the inventory is not expected back (g02-g17 18 Oct: ct05 / slot 4, Down, was counted)
    want = {(e["slot-id"], e["index"]) for c in ops.active_computes()
            if (c.get("status") or {}).get("admission-phase") == "admitted" for e in ops.gpu_entries(c["meta"]["name"])}
    quarantined = {(c.get("spec") or {}).get("slot-id") for c in ops.afm.computes() if ops.quarantined(c)}
    have = {(g.get("slot-id"), g.get("index")) for g in ops.vpod_gpus(sysv) if g.get("slot-id") not in quarantined}
    record_property("gpus", {"system": len(have), "admitted": len(want)})
    checks.check(have == want, f"{sysv} GPUs differ: missing {sorted(want - have)[:12]}, extra {sorted(have - want)[:12]}")
    before = ops.planes(sysv)
    ops.add_gpus(sysv, [])                      # PUT the same full list
    checks.check(ops.wait_vpod(sysv) in TERMINAL and ops.planes(sysv)[1] == before[1],
                 f"re-applying the full list changed {sysv}: {before} -> {ops.planes(sysv)}")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-025")
def test_remove_compute_from_user_vpod_event(ops, afm_ready, uniq, checks, record_property):
    """Removing a compute from a user vPOD (2 computes) raises a vPOD event naming that vPOD."""
    names = _ready_slots(ops)[-2:]
    if len(names) < 2:
        pytest.skip("needs 2 ready computes")
    sysv = ops.system_vpod_name()
    user, moved = uniq("vp"), []
    try:
        for n in names:
            moved += ops.remove_compute(sysv, n)
        ops.create_vpod(user, moved)
        ops.wait_vpod(user)
        since = ops.controller_now()
        back = ops.remove_compute(user, names[0])
        ops.add_gpus(sysv, back)
        moved = [m for m in moved if m not in back]
        ev = ops.wait(lambda: [e for e in ops.events(None, since) if user in json.dumps(e)
                               and str(e.get("type", "")).startswith("vpod_")], 120, 10)
        record_property("events", sorted({e.get("type") for e in ev or []}))
        checks.check(ev, f"no vpod_* event for {user} after removing {names[0]}")
    finally:
        ops.delete_vpod(user)
        if moved:
            ops.add_gpus(sysv, moved)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-106")
def test_vpod_crud_events(ops, afm_ready, uniq, checks, record_property):
    """vPOD create, update (GPU removed) and delete each raise an event naming the vPOD (vpod_deleted on delete)."""
    sysv = ops.system_vpod_name()
    names = _ready_slots(ops)[-2:]
    user, moved = uniq("vp"), []
    seen = {}
    try:
        for n in names:
            moved += ops.remove_compute(sysv, n)
        t = ops.controller_now()
        ops.create_vpod(user, moved)
        ops.wait_vpod(user)

        def mine(since, etype=None):
            return [e for e in ops.events(etype, since) if user in json.dumps(e)]
        seen["create"] = bool(ops.wait(lambda: mine(t), 120, 10))
        t = ops.controller_now()
        back = ops.remove_compute(user, names[-1])
        ops.add_gpus(sysv, back)
        moved = [m for m in moved if m not in back]
        seen["update"] = bool(ops.wait(lambda: mine(t), 120, 10))
        t = ops.controller_now()
        ops.delete_vpod(user)
        seen["delete"] = bool(ops.wait(lambda: mine(t, "vpod_deleted"), 240, 10))  # ~2.5-3 min on the full rack
    finally:
        ops.delete_vpod(user)
        if moved:
            ops.add_gpus(sysv, moved)
    record_property("events", seen)
    for k, v in seen.items():
        checks.check(v, f"no event for vPOD {k}")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-040")
def test_delete_policy_after_vpod_deleted(ops, afm_ready, uniq, cleanup, checks, record_property):
    """A failover policy used by a vPOD cannot be deleted; once the vPOD is deleted, it can (then GET 404)."""
    sysv, user, pol, names, moved = _user_vpod_ctx(
        ops, uniq, cleanup, {"datafabric-reconfig": "automatic", "link-fail-threshold": 20})
    try:
        r = ops.afm.raw("DELETE", f"{FP}/{pol}")
        record_property("delete_in_use", f"HTTP {r.status_code}")
        checks.check(400 <= r.status_code < 500, f"DELETE of in-use policy -> HTTP {r.status_code}, expected 4xx")
    finally:
        _user_vpod_restore(ops, sysv, user, moved)
    r = ops.afm.raw("DELETE", f"{FP}/{pol}")
    checks.check(r.status_code in (200, 204), f"DELETE after the vPOD was deleted -> HTTP {r.status_code}")
    checks.check(ops.afm.raw("GET", f"{FP}/{pol}").status_code == 404, "policy still readable after delete")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-042")
def test_parallel_vpod_crud(ops, afm_ready, uniq, checks, record_property):
    """One user vPOD per compute (up to 3) created in parallel, all online/degraded; deleted in parallel."""
    names = _ready_slots(ops)[-int(ops.th("e2e042_vpods", 3)):]
    if len(names) < 2:
        pytest.skip("needs 2 ready computes")
    sysv = ops.system_vpod_name()
    vps = {n: uniq(f"p{i}") for i, n in enumerate(names)}
    moved: dict[str, list] = {}
    try:
        for n in names:
            moved[n] = ops.remove_compute(sysv, n)
        with ThreadPoolExecutor(len(names)) as ex:
            errs = [f.exception() for f in [ex.submit(ops.create_vpod, vps[n], moved[n]) for n in names]]
        checks.check(not any(errs), f"parallel create errors: {[str(e)[:120] for e in errs if e]}")
        for n in names:
            checks.check(ops.wait_vpod(vps[n]) in TERMINAL, f"{vps[n]} {ops.vpod_state(vps[n])}")
        with ThreadPoolExecutor(len(names)) as ex:
            ok = list(ex.map(ops.delete_vpod, vps.values()))
        checks.check(all(ok), f"parallel delete left vPODs: {[v for v, o in zip(vps.values(), ok) if not o]}")
    finally:
        for v in vps.values():
            ops.delete_vpod(v)
        for n, e in moved.items():
            if e:
                ops.add_gpus(sysv, e)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-079")
def test_move_gpus_between_user_vpods(ops, afm_ready, uniq, checks, record_property):
    """GPUs move from user vPOD A to user vPOD B (re-add retried on the transient 400): membership becomes B,
    both vPODs stay consistent; everything goes back to the system vPOD."""
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    a, b = uniq("va"), uniq("vb")
    entries = []
    try:
        entries = ops.remove_compute(sysv, name)
        ops.create_vpod(a, entries)
        checks.check(ops.wait_vpod(a) in TERMINAL, f"{a} {ops.vpod_state(a)}")
        ops.remove_compute(a, name)
        ops.create_vpod(b, entries)
        checks.check(ops.wait_vpod(b) in TERMINAL, f"{b} {ops.vpod_state(b)}")
        checks.check(ops.membership(name) == {b}, f"{name} membership {ops.membership(name)}, expected {b}")
        checks.check(not ops.vpod_gpus(a), f"{a} still lists GPUs {ops.vpod_gpus(a)}")
    finally:
        ops.delete_vpod(b)
        ops.delete_vpod(a)
        if entries:
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-087")
def test_new_vpod_fabric_setup(ops, afm_ready, uniq, checks, record_property):
    """Creating a user vPOD programs its scale-up fabric: every admitted switch gains VLANs for it and the vPOD
    reports provisioned/available planes; deleting it removes them again."""
    sysv = ops.system_vpod_name()
    sws = {s.name: s for s in ops.rack.switches.values()}
    before = {n: ops.vlan_count(s) for n, s in sws.items()}
    name = ops.pick_compute()
    user, entries = uniq("vp"), []
    try:
        entries = ops.remove_compute(sysv, name)
        ops.create_vpod(user, entries)
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} {ops.vpod_state(user)}")
        prov, avail, eff = ops.planes(user)
        record_property("planes", [prov, avail, eff])
        checks.check(prov and avail and avail <= prov, f"{user} planes {prov}/{avail}/{eff}")
        grown = ops.wait(lambda: all(ops.vlan_count(s) > before[n] for n, s in sws.items()), 120, 10)
        record_property("vlans", {"before": before, "with_vpod": {n: ops.vlan_count(s) for n, s in sws.items()}})
        checks.check(grown, "not every switch gained a VLAN for the new vPOD")
        ops.delete_vpod(user)
        back = ops.wait(lambda: all(ops.vlan_count(s) == before[n] for n, s in sws.items()), 180, 10)
        checks.check(back, f"VLANs not removed after delete: {({n: ops.vlan_count(s) for n, s in sws.items()})} vs {before}")
    finally:
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()
