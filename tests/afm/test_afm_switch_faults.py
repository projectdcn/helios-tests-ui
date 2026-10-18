"""Switch-tray failure injection (DISRUPTIVE; added to the test plan by helios-tests on 18 Oct 2026, testplan/added.yaml):

  TC-SWH-060  congestion: many-to-one TransferBench onto one compute tray; the switch ports towards it must show
              the congestion (queue watermarks well above an idle baseline), and the fabric must recover after it
  TC-SWH-061  FDB mismatch, entry missing: one AFM-programmed static FDB entry deleted on a switch; AFM must put it
              back within thresholds.afm.fdb_reconcile_s
  TC-SWH-062  FDB mismatch, foreign entry: a static entry AFM did not program added on a fabric port; AFM must remove
              it within thresholds.afm.fdb_reconcile_s

Which AFM event or alert should report a congestion or an FDB mismatch is not documented: every event created
during the case is recorded (events_seen); once AMD names them, list the types in thresholds.afm.congestion_events /
thresholds.afm.fdb_mismatch_events and the case requires one. The cases always restore what they changed (finally).
Only EthernetN fabric ports and CONFIG_DB FDB entries in AFM VLANs are touched - never the management network.
"""
from __future__ import annotations

import random
import time

import pytest

from helios import datapath
from helios.afmctl import parse_port_description

pytestmark = [pytest.mark.l3, pytest.mark.disruptive]


def _event_types(evs) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in evs:
        t = e.get("type") or (e.get("spec") or {}).get("event-type") or (e.get("spec") or {}).get("type") or "?"
        out[t] = out.get(t, 0) + 1
    return out


def _required_events(ops, checks, key: str, seen: dict[str, int], what: str) -> None:
    want = ops.th(key, []) or []
    if want:
        checks.check(any(t in seen for t in want), f"no AFM event of {want} reported the {what} (seen: {seen or 'none'})")


def _ready_slots(ops) -> dict[int, str]:
    """{slot: AFM compute name} of Ready computes outside the quarantine."""
    return {int(c["spec"]["slot-id"]): c["meta"]["name"] for c in ops.active_computes()
            if (c.get("spec") or {}).get("slot-id") is not None and ops.compute_ready(c["meta"]["name"])}


# ================================================================== congestion

def _slot_ports(rack, slot: int) -> dict[tuple, str]:
    """{(switch, ns, port): oper} for every switch port described as cabled to compute ``slot``."""
    out = {}
    for sw in rack.switches.values():
        for ns in sw.namespaces():
            desc = sw.port_descriptions(ns)
            oper = {r.get("Interface"): str(r.get("Oper", "")).lower() for r in sw.interfaces_status(ns)}
            for p, d in desc.items():
                cab = parse_port_description(d)
                if cab and cab[0] == slot:
                    out[(sw.name, ns, p)] = oper.get(p, "")
    return out


def _watermarks(rack, keys) -> dict[tuple, float]:
    want: dict[tuple, set] = {}
    for swn, ns, p in keys:
        want.setdefault((swn, ns), set()).add(p)
    out = {}
    for (swn, ns), ports in want.items():
        wm = rack.switches[swn].queue_watermarks(ns)
        out.update({(swn, ns, p): wm.get(p, 0.0) for p in ports})
    return out


def _drops(rack, keys) -> dict[tuple, float]:
    want: dict[tuple, set] = {}
    for swn, ns, p in keys:
        want.setdefault((swn, ns), set()).add(p)
    out = {}
    for (swn, ns), ports in want.items():
        for (p, _q), n in rack.switches[swn].queue_counters(ns).items():
            if p in ports:
                out[(swn, ns, p)] = out.get((swn, ns, p), 0.0) + n
    return out


def _clear_wm(rack, keys) -> None:
    for swn, ns in {(k[0], k[1]) for k in keys}:
        rack.switches[swn].clear_queue_watermarks(ns)


@pytest.mark.datapath(tools=("transferbench",), min_computes=3)
@pytest.mark.tc("TC-SWH-060")
def test_congestion_detected_on_incast(ops, rack, computes, inventory, datapath_ready, afm_ready, checks, record_property):
    """Incast: thresholds.datapath.congestion_sources (4) trays (or congestion_source_trays, by name; destination:
    congestion_destination, default the highest Ready slot) run TransferBench with one destination tray at the same
    time for congestion_traffic_s (150 s). The switch ports cabled to the destination must show it: the highest
    unicast-queue watermark of those ports reaches congestion_wm_factor (4) x the idle peak and at least
    congestion_wm_min_bytes (100 000). Afterwards no queue drops grow on them, every port that was up is up, and the
    system vPOD's planes are back. Watermarks refresh every switch.watermark_interval_s (120 s), so each read waits for
    one interval after a clear."""
    datapath.require_transferbench_cross(inventory)
    th = lambda k, d: inventory.th(k, d)  # noqa: E731
    by_name = {t.name: t for t in computes}
    slots = _ready_slots(ops)
    trays = []                                   # (slot, tray) of Ready computes that passed the datapath pre-flight
    for slot, name in sorted(slots.items()):
        t = ops.tray(name)
        if t.name in by_name:
            trays.append((slot, by_name[t.name]))
    n_src = int(th("datapath.congestion_sources", 4))
    want_dst, want_src = th("datapath.congestion_destination", None), th("datapath.congestion_source_trays", None)
    if want_dst:                                 # named trays (e.g. to keep an unstable tray out of the load)
        trays.sort(key=lambda st: st[1].name == want_dst)
        if trays[-1][1].name != want_dst:
            pytest.skip(f"congestion_destination {want_dst} is not a Ready compute that passed the pre-flight")
    if len(trays) < 2:
        pytest.skip(f"needs a destination and at least one source tray, have {len(trays)}")
    (dst_slot, dst) = trays[-1]
    pool = [t for _, t in trays[:-1]]
    srcs = [t for t in pool if t.name in set(want_src)] if want_src else pool[-n_src:]
    if not srcs:
        pytest.skip(f"none of congestion_source_trays {want_src} is a Ready compute that passed the pre-flight")
    record_property("destination", f"{dst.name} (slot {dst_slot})")
    record_property("sources", [t.name for t in srcs])
    ports = _slot_ports(rack, dst_slot)
    up = {k for k, v in ports.items() if v == "up"}
    checks.check(up, f"no up switch port cabled to {dst.name} (slot {dst_slot})")
    if not up:
        checks.assert_all()
    interval = float(th("switch.watermark_interval_s", 120)) + float(th("switch.watermark_margin_s", 15))
    sysv = ops.system_vpod_name()
    base_planes = ops.planes(sysv)
    since = ops.controller_now()

    # idle peak over one telemetry interval
    _clear_wm(rack, up)
    time.sleep(interval)
    idle = _watermarks(rack, up)
    drops0 = _drops(rack, up)
    # loaded peak: clear, load for at least one interval, wait for the next refresh
    _clear_wm(rack, up)
    load = datapath.transferbench_incast(dst, srcs, inventory, checks,
                                         max(float(th("datapath.congestion_traffic_s", 150)), interval))
    time.sleep(interval)
    loaded = _watermarks(rack, up)
    drops1 = _drops(rack, up)
    record_property("traffic", {"rounds": load["rounds"], "seconds": load["seconds"],
                                "last_round_gbps": load["bw_gbps"][-1] if load["bw_gbps"] else {}})
    top = lambda d: {f"{k[0]}/{k[1]}/{k[2]}": v for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:6]}  # noqa: E731
    record_property("idle_watermark_top_bytes", top(idle))
    record_property("loaded_watermark_top_bytes", top(loaded))
    record_property("drops_during_load", {f"{k[0]}/{k[1]}/{k[2]}": drops1[k] - drops0.get(k, 0)
                                          for k in drops1 if drops1[k] > drops0.get(k, 0)})
    idle_max, loaded_max = max(idle.values() or [0]), max(loaded.values() or [0])
    need = max(float(th("switch.congestion_wm_factor", 4)) * idle_max, float(th("switch.congestion_wm_min_bytes", 100000)))
    checks.check(loaded_max >= need, f"no congestion seen on the {len(up)} ports towards {dst.name}: highest queue "
                                     f"watermark {loaded_max:.0f} B under load, idle {idle_max:.0f} B (needs {need:.0f} B)")

    # recovery: drops stop, links stay up, planes back
    window = float(th("switch.queue_window_s", 30))
    d_a = _drops(rack, up)
    time.sleep(window)
    d_b = _drops(rack, up)
    grow = {f"{k[0]}/{k[1]}/{k[2]}": d_b[k] - d_a.get(k, 0) for k in d_b if d_b[k] > d_a.get(k, 0)}
    checks.check(not grow, f"queue drops still growing {window:.0f} s after the load: {grow}")
    after = _slot_ports(rack, dst_slot)
    down = sorted(f"{k[0]}/{k[1]}/{k[2]}" for k in up if after.get(k) != "up")
    checks.check(not down, f"ports towards {dst.name} down after the load: {down}")
    ok = ops.wait(lambda: (ops.planes(sysv)[1] or 0) >= (base_planes[1] or 0), float(th("afm.plane_restore_s", 300)), 10)
    checks.check(ok, f"{sysv} planes {ops.planes(sysv)} after the load, before {base_planes}")
    seen = _event_types(ops.events(since=since))
    record_property("events_seen", seen)
    _required_events(ops, checks, "congestion_events", seen, "congestion")
    checks.assert_all()


# ================================================================== FDB mismatch

def _fdb_target(ops, rack):
    """(switch, ns, vlan, mac, port): one AFM static FDB entry for a GPU port of a Ready compute, on an up port not in
    thresholds.afm.avoid_ports; deterministic (last switch, last matching entry) so reruns hit the same kind of entry."""
    slots = set(_ready_slots(ops))
    avoid = set(ops.th("avoid_ports", []) or [])
    for sw in reversed(list(rack.switches.values())):
        for ns in sw.namespaces():
            desc = sw.port_descriptions(ns)
            oper = {r.get("Interface"): str(r.get("Oper", "")).lower() for r in sw.interfaces_status(ns)}
            for vlan, mac, port in reversed(sw.fdb_static(ns)):
                cab = parse_port_description(desc.get(port, ""))
                if cab and cab[0] in slots and oper.get(port) == "up" and f"{sw.name}:{port}" not in avoid \
                        and vlan.lower() != "vlan2":
                    return sw, ns, vlan, mac, port
    return None


def _present(sw, vlan, mac, ns) -> bool:
    return any(v == vlan and m == mac.lower() for v, m, _p in sw.fdb_static(ns))


@pytest.mark.tc("TC-SWH-061")
def test_fdb_mismatch_missing_entry_restored(ops, rack, afm_ready, checks, record_property):
    """Delete one AFM-programmed static FDB entry (CONFIG_DB) on a switch: AFM must detect the mismatch and program it
    again within thresholds.afm.fdb_reconcile_s (300 s). The test puts the entry back itself if AFM does not."""
    if rack is None or not rack.switches:
        pytest.skip("no switch SSH in this inventory")
    tgt = _fdb_target(ops, rack)
    if not tgt:
        pytest.skip("no AFM static FDB entry on an up port of a Ready compute")
    sw, ns, vlan, mac, port = tgt
    orig = sw.fdb_entry(vlan, mac, ns) or {"port": port, "type": "static"}
    record_property("entry", f"{sw.name} {ns} {vlan} {mac} -> {port}")
    since = ops.controller_now()
    t0 = time.monotonic()
    restored_by_afm = False
    try:
        r = sw.fdb_delete(vlan, mac, ns)
        checks.check(r.ok, f"delete of FDB|{vlan}|{mac} failed: rc {r.rc} {r.tail()!r}")
        gone = ops.wait(lambda: not _present(sw, vlan, mac, ns), 30, 3)
        checks.check(gone, f"FDB|{vlan}|{mac} still present after the delete")
        restored_by_afm = bool(ops.wait(lambda: _present(sw, vlan, mac, ns),
                                        float(ops.th("fdb_reconcile_s", 300)), 10))
        record_property("restored_by_afm_s", round(time.monotonic() - t0, 1) if restored_by_afm else None)
        now = sw.fdb_entry(vlan, mac, ns)
        checks.check(restored_by_afm, f"AFM did not program {sw.name} {ns} FDB|{vlan}|{mac} again within "
                                      f"{ops.th('fdb_reconcile_s', 300)} s (FDB mismatch not repaired)")
        if restored_by_afm:
            checks.check(now.get("port") == orig.get("port"), f"FDB|{vlan}|{mac} came back on {now.get('port')}, "
                                                              f"was {orig.get('port')}")
    finally:
        if not _present(sw, vlan, mac, ns):
            sw.fdb_set(vlan, mac, orig.get("port", port), ns, orig.get("type", "static"))
            record_property("restored_by_test", True)
    checks.check(_present(sw, vlan, mac, ns), f"FDB|{vlan}|{mac} missing at the end")
    seen = _event_types(ops.events(since=since))
    record_property("events_seen", seen)
    _required_events(ops, checks, "fdb_mismatch_events", seen, "FDB mismatch")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-062")
def test_fdb_mismatch_foreign_entry_removed(ops, rack, afm_ready, checks, record_property):
    """Add a static FDB entry AFM did not program (an unused MAC 02:00:00:7f:xx:xx in an AFM VLAN, on a fabric port of
    that VLAN): AFM must detect the mismatch and remove it within thresholds.afm.fdb_reconcile_s (300 s). The test
    removes it itself if AFM does not."""
    if rack is None or not rack.switches:
        pytest.skip("no switch SSH in this inventory")
    tgt = _fdb_target(ops, rack)
    if not tgt:
        pytest.skip("no AFM static FDB entry on an up port of a Ready compute")
    sw, ns, vlan, _mac, port = tgt
    mac = "02:00:00:7f:%02x:%02x" % (random.randrange(256), random.randrange(256))   # slot 0x7f: no such tray
    record_property("entry", f"{sw.name} {ns} {vlan} {mac} -> {port}")
    since = ops.controller_now()
    t0 = time.monotonic()
    removed_by_afm = False
    try:
        r = sw.fdb_set(vlan, mac, port, ns)
        checks.check(r.ok, f"adding FDB|{vlan}|{mac} failed: rc {r.rc} {r.tail()!r}")
        checks.check(ops.wait(lambda: _present(sw, vlan, mac, ns), 30, 3), f"FDB|{vlan}|{mac} not present after the add")
        removed_by_afm = bool(ops.wait(lambda: not _present(sw, vlan, mac, ns),
                                       float(ops.th("fdb_reconcile_s", 300)), 10))
        record_property("removed_by_afm_s", round(time.monotonic() - t0, 1) if removed_by_afm else None)
        checks.check(removed_by_afm, f"AFM did not remove the foreign {sw.name} {ns} FDB|{vlan}|{mac} within "
                                     f"{ops.th('fdb_reconcile_s', 300)} s (FDB mismatch not repaired)")
    finally:
        if _present(sw, vlan, mac, ns):
            sw.fdb_delete(vlan, mac, ns)
            record_property("removed_by_test", True)
    checks.check(not _present(sw, vlan, mac, ns), f"foreign FDB|{vlan}|{mac} still present at the end")
    seen = _event_types(ops.events(since=since))
    record_property("events_seen", seen)
    _required_events(ops, checks, "fdb_mismatch_events", seen, "FDB mismatch")
    checks.assert_all()
