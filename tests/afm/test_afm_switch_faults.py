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

import os
import random
import time

import pytest

from helios import datapath
from helios.afmctl import parse_port_description
from helios.evidence import afm_events

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
    return _ports_by_slot(rack, {slot}).get(slot, {})


def _ports_by_slot(rack, slots: set[int], speeds: dict | None = None, gpus: dict | None = None) -> dict[int, dict[tuple, str]]:
    """{slot: {(switch, ns, port): oper}} for the ports cabled to each of ``slots``, in one pass over the switches;
    ``speeds`` (if given) is filled with {(switch, ns, port): Gb/s} from the Speed column, ``gpus`` with
    {(switch, ns, port): GPU index} from the description."""
    out: dict[int, dict[tuple, str]] = {}
    for sw in rack.switches.values():
        for ns in sw.namespaces():
            desc = sw.port_descriptions(ns)
            rows = {r.get("Interface"): r for r in sw.interfaces_status(ns)}
            for p, d in desc.items():
                cab = parse_port_description(d)
                if cab and cab[0] in slots:
                    out.setdefault(cab[0], {})[(sw.name, ns, p)] = str(rows.get(p, {}).get("Oper", "")).lower()
                    if gpus is not None:
                        gpus[(sw.name, ns, p)] = cab[1]
                    if speeds is not None:
                        sp = str(rows.get(p, {}).get("Speed", "")).upper().rstrip("G")
                        speeds[(sw.name, ns, p)] = float(sp) if sp.replace(".", "").isdigit() else 0.0
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
        rack.switches[swn].clear_buffer_watermarks(ns)


def _by_ns(keys) -> dict[tuple, set]:
    want: dict[tuple, set] = {}
    for swn, ns, p in keys:
        want.setdefault((swn, ns), set()).add(p)
    return want


def _lossless(rack, keys) -> dict[tuple, dict]:
    """{(switch, ns): {'prios', 'pgs', 'queues'}} - the lossless classes (pfc_enable, mapped through the port's
    dot1p -> TC -> PG / queue maps) of the first of ``keys`` on each ASIC."""
    out = {}
    for (swn, ns), ports in _by_ns(keys).items():
        sw = rack.switches[swn]
        m = sw.db_hash("CONFIG_DB", f"PORT_QOS_MAP|{sorted(ports)[0]}", ns)
        prios = [int(x) for x in m.get("pfc_enable", "").split(",") if x.strip().isdigit()]
        mp = lambda tbl, f: sw.db_hash("CONFIG_DB", f"{tbl}|{m[f].strip('[]').split('|')[-1]}", ns) if m.get(f) else {}  # noqa: E731
        dot1p, tc_pg, tc_q = mp("DOT1P_TO_TC_MAP", "dot1p_to_tc_map"), mp("TC_TO_PRIORITY_GROUP_MAP", "tc_to_pg_map"), \
            mp("TC_TO_QUEUE_MAP", "tc_to_queue_map")
        tcs = [dot1p.get(str(p), str(p)) for p in prios]
        out[(swn, ns)] = {"prios": prios, "pgs": sorted({f"PG{tc_pg.get(tc, tc)}" for tc in tcs}),
                          "queues": sorted({f"UC{tc_q.get(tc, tc)}" for tc in tcs})}
    return out


def _counters(rack, dst_keys, src_keys) -> dict:
    """One read of the incast evidence counters: PFC pause frames per priority (ports towards the destination and the
    sources), packets / drops per queue (towards the destination), ingress drops per PG (towards the sources)."""
    snap = {"pfc": {}, "queue": {}, "pg_drop": {}}
    for (swn, ns), ports in _by_ns(set(dst_keys) | set(src_keys)).items():
        sw = rack.switches[swn]
        pfc = sw.pfc_counters_by_prio(ns)
        for p in ports:
            snap["pfc"][(swn, ns, p)] = {d: pfc.get(d, {}).get(p, {}) for d in ("Rx", "Tx")}
        if any((swn, ns, p) in dst_keys for p in ports):
            for (p, q), v in sw.queue_stats(ns).items():
                if (swn, ns, p) in dst_keys:
                    snap["queue"][(swn, ns, p, q)] = v
        if any((swn, ns, p) in src_keys for p in ports):
            for p, v in sw.pg_drops(ns).items():
                if (swn, ns, p) in src_keys:
                    snap["pg_drop"][(swn, ns, p)] = v
    return snap


def _buffer_peaks(rack, dst_keys, src_keys) -> dict:
    """Peaks since the last clear: unicast-queue watermarks per queue (towards the destination), PG shared-pool
    watermarks (towards the sources), buffer-pool watermarks (per ASIC)."""
    out = {"queue": {}, "pg": {}, "pool": {}}
    for (swn, ns), ports in _by_ns(set(dst_keys) | set(src_keys)).items():
        sw = rack.switches[swn]
        qw = sw.queue_watermarks_by_queue(ns)
        pw = sw.pg_watermarks(ns)
        for p in ports:
            if (swn, ns, p) in dst_keys:
                out["queue"][(swn, ns, p)] = qw.get(p, {})
            if (swn, ns, p) in src_keys:
                out["pg"][(swn, ns, p)] = pw.get(p, {})
        out["pool"][(swn, ns)] = sw.buffer_pool_watermarks(ns)
    return out


def _best_effort(errors: list, what: str, fn, *a, default=None):
    """Evidence reads must not decide the case: a switch without the command (older image) is recorded, not fatal."""
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001 - CommandError, parse errors
        errors.append(f"{what}: {str(e).splitlines()[0][:200]}")
        return default


def _key(k) -> str:
    return "/".join(str(x) for x in k)


def _incast_evidence(c0: dict, c1: dict, peaks: dict, lossless: dict) -> dict:
    """Deltas between two _counters() reads + the loaded peaks, summarised per class (lossless / lossy)."""
    def cls(swn, ns, name, field):
        return "lossless" if name in (lossless.get((swn, ns)) or {}).get(field, ()) else "lossy"
    pfc = {"Tx_towards_sources": {}, "Rx_from_destination": {}, "other": {}}
    for k, v in c1["pfc"].items():
        for d in ("Rx", "Tx"):
            for prio, n in v.get(d, {}).items():
                delta = n - c0["pfc"].get(k, {}).get(d, {}).get(prio, 0)
                if delta <= 0:
                    continue
                if d == "Tx" and k in c1.get("_src", ()):
                    where = "Tx_towards_sources"            # the switch pausing the sources: the incast's back-pressure
                elif d == "Rx" and k in c1.get("_dst", ()):
                    where = "Rx_from_destination"           # the destination tray pausing the switch
                else:
                    where = "other"
                pfc[where][f"{_key(k)} {d} PFC{prio}"] = delta
    q_pkts, q_drop = {}, {"lossless": {}, "lossy": {}}
    for k, (n, d) in c1["queue"].items():
        n0, d0 = c0["queue"].get(k, (n, d))
        if n - n0 > 0:
            q_pkts[k[3]] = q_pkts.get(k[3], 0) + n - n0
        if d - d0 > 0:
            q_drop[cls(k[0], k[1], k[3], "queues")][_key(k)] = d - d0
    pg_drop = {"lossless": {}, "lossy": {}}
    for k, v in c1["pg_drop"].items():
        for pg, n in v.items():
            delta = n - c0["pg_drop"].get(k, {}).get(pg, n)
            if delta > 0:
                pg_drop[cls(k[0], k[1], pg, "pgs")][f"{_key(k)} {pg}"] = delta
    qpeak: dict[str, float] = {}
    for k, v in peaks["queue"].items():
        for q, b in v.items():
            qpeak[q] = max(qpeak.get(q, 0.0), b)
    pgpeak: dict[str, float] = {}
    for k, v in peaks["pg"].items():
        for pg, b in v.items():
            pgpeak[pg] = max(pgpeak.get(pg, 0.0), b)
    used = max(q_pkts, key=q_pkts.get) if q_pkts else None
    return {"pfc_pause_frames": {w: dict(sorted(d.items(), key=lambda kv: -kv[1])[:12]) | {"total": sum(d.values())}
                                 for w, d in pfc.items()},
            "queue_packets_towards_destination": dict(sorted(q_pkts.items())),
            "traffic_queue": used,
            "traffic_queue_lossless": used is not None and any(used in v.get("queues", ()) for v in lossless.values()),
            "queue_drops": {c: dict(sorted(d.items(), key=lambda kv: -kv[1])[:12]) | {"total": sum(d.values())}
                            for c, d in q_drop.items()},
            "pg_drops_towards_sources": {c: dict(sorted(d.items(), key=lambda kv: -kv[1])[:12]) | {"total": sum(d.values())}
                                         for c, d in pg_drop.items()},
            "queue_watermark_peak_bytes": dict(sorted(qpeak.items())),
            "pg_watermark_peak_bytes": dict(sorted(pgpeak.items())),
            "buffer_pool_peak_bytes": {_key(k): v for k, v in peaks["pool"].items()},
            "lossless_classes": {_key(k): v for k, v in lossless.items()}}


def _load_summary(load: dict, dst_speeds: dict[tuple, float], up: set, gpus: dict | None = None) -> dict:
    """Per-source GB/s of the last round, their total and how much of the destination's line rate they used.
    TransferBench reports achieved (not offered) bandwidth, so ~1.0 means the destination's ports were saturated."""
    last = load["bw_gbps"][-1] if load["bw_gbps"] else {}
    total = sum(v for v in last.values() if v)
    if load.get("mode") == "one-way" and gpus:                          # one destination GPU: only its ports
        up = {k for k in up if gpus.get(k) == load.get("dst_gpu")}
    line = sum(dst_speeds.get(k, 0.0) for k in up)                       # Gb/s
    return {"per_source_GBps": last, "total_GBps": round(total, 1), "destination_line_rate_Gbps": line,
            "load": load.get("mode", "a2a-pairs") + (f" into GPU{load['dst_gpu']}" if load.get("mode") == "one-way" else ""),
            "destination_utilisation": round(total * 8 / line, 2) if line else None}


@pytest.mark.datapath(tools=("transferbench",), min_computes=3)
@pytest.mark.tc("TC-SWH-060")
def test_congestion_detected_on_incast(ops, rack, computes, inventory, datapath_ready, afm_ready, checks, record_property):
    """Incast: thresholds.datapath.congestion_sources (4) trays (or congestion_source_trays, by name; destination:
    congestion_destination, default the highest Ready slot) run TransferBench with one destination tray at the same
    time for congestion_traffic_s (150 s). The switch ports cabled to the destination must show it: the highest
    unicast-queue watermark of those ports reaches congestion_wm_factor (4) x the idle peak and at least
    congestion_wm_min_bytes (100 000). Afterwards no queue drops grow on them, every port that was up is up, and the
    system vPOD's planes are back. Watermarks refresh every switch.watermark_interval_s (120 s), so each read waits for
    one interval after a clear.

    Evidence recorded (incast_evidence, traffic), read before and after the load: PFC pause frames per priority on the
    ports towards the sources (Tx: the switch's back-pressure) and the destination; packets and drops per queue towards
    the destination (which queue the traffic used, lossless or not); ingress drops per PG towards the sources; peak
    watermarks per queue, per PG and per buffer pool; GB/s per source, total, and the share of the destination's
    line rate it used (achieved bandwidth: ~1.0 = destination ports saturated). Drops on a lossless class (the port's pfc_enable priorities, through its maps) above
    switch.congestion_lossless_drops_max (0) fail the case; lossy drops are only recorded.

    BLOCKED (not FAILED) when no real incast can be made: fewer than congestion_min_sources (3) Ready trays that passed
    the datapath pre-flight, or a load that did not run - a TransferBench job failed or it ran shorter than the
    watermark interval. g02-g17 19 Oct 00:05 UTC: 2 sources, one rank died ('recv: Invalid argument', shared master
    port - fixed by incast_base_port), 6.6 s of one-to-one traffic; the run then reported 'no congestion seen', which
    judged the switch on a load that never happened (it ran during the port-bouncing storm, 18 Oct 15:18 - 19 Oct
    01:00 UTC, with few trays Ready)."""
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
    record_property("ready_trays", [f"{t.name} (slot {s_})" for s_, t in trays])      # evidence that survives a cut log
    min_src = int(th("datapath.congestion_min_sources", min(3, n_src)))
    if len(srcs) < min_src:
        pytest.fail(f"incast pre-flight failed - only {len(srcs)} source tray(s) ({', '.join(t.name for t in srcs) or 'none'}) "
                    f"are Ready and passed the datapath pre-flight, an incast needs {min_src} "
                    f"(thresholds.datapath.congestion_min_sources); Ready trays: {[t.name for _, t in trays]}", pytrace=False)
    speeds: dict[tuple, float] = {}
    port_gpu: dict[tuple, int] = {}
    src_slot = {t.name: s_ for s_, t in trays}
    by_slot = _ports_by_slot(rack, {dst_slot} | {src_slot[t.name] for t in srcs}, speeds, port_gpu)
    ports = by_slot.get(dst_slot, {})
    up = {k for k, v in ports.items() if v == "up"}
    src_up = {k for t in srcs for k, v in by_slot.get(src_slot[t.name], {}).items() if v == "up"} - up
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
    ev_err: list[str] = []
    lossless = _best_effort(ev_err, "lossless classes", _lossless, rack, up | src_up, default={})
    c0 = _best_effort(ev_err, "counters before", _counters, rack, up, src_up, default={"pfc": {}, "queue": {}, "pg_drop": {}})
    # loaded peak: clear, load for at least one interval, wait for the next refresh
    _clear_wm(rack, up)
    want_s = max(float(th("datapath.congestion_traffic_s", 150)), interval)
    load = datapath.transferbench_incast(dst, srcs, inventory, checks, want_s)
    broken = [k for rnd in load["bw_gbps"] for k, v in rnd.items() if v is None]
    if broken or load["seconds"] < interval:
        record_property("traffic", {"rounds": load["rounds"], "seconds": load["seconds"], "failed_jobs": sorted(set(broken))})
        pytest.fail(f"incast pre-flight failed - the load was not generated: {load['rounds']} round(s), "
                    f"{load['seconds']} s of the {want_s:.0f} s wanted"
                    + (f", TransferBench failed for {', '.join(sorted(set(broken)))}" if broken else "")
                    + " - congestion not judged.\n" + "\n".join(f"  - {f}" for f in checks.failures), pytrace=False)
    c1 = _best_effort(ev_err, "counters after", _counters, rack, up, src_up, default={"pfc": {}, "queue": {}, "pg_drop": {}})
    c1["_dst"], c1["_src"] = up, src_up
    time.sleep(interval)
    loaded = _watermarks(rack, up)
    peaks = _best_effort(ev_err, "buffer peaks", _buffer_peaks, rack, up, src_up,
                         default={"queue": {}, "pg": {}, "pool": {}})
    drops1 = _drops(rack, up)
    summary = _load_summary(load, speeds, up, port_gpu)
    record_property("traffic", {"rounds": load["rounds"], "seconds": load["seconds"], **summary})
    ev = _incast_evidence(c0, c1, peaks, lossless)
    if ev_err:
        ev["errors"] = ev_err
    record_property("incast_evidence", ev)
    max_ll = float(th("switch.congestion_lossless_drops_max", 0))
    ll = ev["queue_drops"]["lossless"]["total"] + ev["pg_drops_towards_sources"]["lossless"]["total"]
    checks.check(ll <= max_ll, f"{ll:.0f} packet(s) dropped on lossless classes during the incast (PFC did not hold "
                               f"them): queues {ev['queue_drops']['lossless']}, PGs "
                               f"{ev['pg_drops_towards_sources']['lossless']}")
    top = lambda d: {f"{k[0]}/{k[1]}/{k[2]}": v for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:6]}  # noqa: E731
    record_property("idle_watermark_top_bytes", top(idle))
    record_property("loaded_watermark_top_bytes", top(loaded))
    record_property("drops_during_load", {f"{k[0]}/{k[1]}/{k[2]}": drops1[k] - drops0.get(k, 0)
                                          for k in drops1 if drops1[k] > drops0.get(k, 0)})
    idle_max, loaded_max = max(idle.values() or [0]), max(loaded.values() or [0])
    need = max(float(th("switch.congestion_wm_factor", 4)) * idle_max, float(th("switch.congestion_wm_min_bytes", 100000)))
    util, min_util = summary["destination_utilisation"], float(th("switch.congestion_min_utilisation", 0.9))
    if loaded_max < need and util is not None and util < min_util:
        # g02-g17 21 Oct 04:13 UTC: 4 two-rank a2a jobs moved 1487.7 GB/s into ct12's 144 x 400G ports (0.21 of
        # them) - nothing can queue at that load, so 'no congestion seen' would judge the switch on a non-incast
        pytest.fail(f"incast pre-flight failed - the load used only {util} of {dst.name}'s line rate "
                    f"({summary['total_GBps']} GB/s into {summary['destination_line_rate_Gbps']:.0f} Gb/s, "
                    f"switch.congestion_min_utilisation {min_util}): no incast, congestion not judged "
                    f"(highest queue watermark {loaded_max:.0f} B, idle {idle_max:.0f} B)", pytrace=False)
    checks.check(loaded_max >= need, f"no congestion seen on the {len(up)} ports towards {dst.name}: highest queue "
                                     f"watermark {loaded_max:.0f} B under load, idle {idle_max:.0f} B (needs {need:.0f} B); "
                                     f"load {summary['total_GBps']} GB/s into {summary['destination_line_rate_Gbps']:.0f} Gb/s "
                                     f"({summary['destination_utilisation']} of it), traffic on {ev['traffic_queue']}, PFC Tx towards "
                                     f"the sources {ev['pfc_pause_frames']['Tx_towards_sources']['total']:.0f}")

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
    seed = os.environ.get("HELIOS_FDB_TARGET_SEED")     # helios-slm: helios_slm/vrack_dataset.py: a random eligible entry per run
    found = []
    for sw in reversed(list(rack.switches.values())):
        for ns in sw.namespaces():
            desc = sw.port_descriptions(ns)
            oper = {r.get("Interface"): str(r.get("Oper", "")).lower() for r in sw.interfaces_status(ns)}
            for vlan, mac, port in reversed(sw.fdb_static(ns)):
                cab = parse_port_description(desc.get(port, ""))
                if cab and cab[0] in slots and oper.get(port) == "up" and f"{sw.name}:{port}" not in avoid \
                        and vlan.lower() != "vlan2":
                    if not seed:
                        return sw, ns, vlan, mac, port
                    found.append((sw, ns, vlan, mac, port))
    return random.Random(seed).choice(found) if found else None


def _present(sw, vlan, mac, ns) -> bool:
    return any(v == vlan and m == mac.lower() for v, m, _p in sw.fdb_static(ns))


@pytest.mark.tc("TC-SWH-061")
def test_fdb_mismatch_missing_entry_restored(ops, rack, afm_ready, checks, record_property, evidence):
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
    evidence.fact(case="fdb_missing_entry", switch=sw.name, ns=ns, vlan=vlan, mac=mac, port=port,
                  entry_before=orig, fdb_reconcile_s=float(ops.th("fdb_reconcile_s", 300)))
    since = ops.controller_now()
    t0 = time.monotonic()
    restored_by_afm = False
    try:
        evidence.watch("fdb_present", _present(sw, vlan, mac, ns))
        r = sw.fdb_delete(vlan, mac, ns)
        evidence.event("inject", action="fdb_delete", rc=r.rc)
        checks.check(r.ok, f"delete of FDB|{vlan}|{mac} failed: rc {r.rc} {r.tail()!r}")
        gone = ops.wait(lambda: not evidence.watch("fdb_present", _present(sw, vlan, mac, ns)), 30, 3)
        checks.check(gone, f"FDB|{vlan}|{mac} still present after the delete")
        restored_by_afm = bool(ops.wait(lambda: evidence.watch("fdb_present", _present(sw, vlan, mac, ns)),
                                        float(ops.th("fdb_reconcile_s", 300)), 10))
        evidence.event("repaired_by_afm" if restored_by_afm else "not_repaired_in_time")
        record_property("restored_by_afm_s", round(time.monotonic() - t0, 1) if restored_by_afm else None)
        now = sw.fdb_entry(vlan, mac, ns)
        evidence.fact(entry_restored_by_afm=now or None)     # before the cleanup: where AFM put it, if it did
        checks.check(restored_by_afm, f"AFM did not program {sw.name} {ns} FDB|{vlan}|{mac} again within "
                                      f"{ops.th('fdb_reconcile_s', 300)} s (FDB mismatch not repaired)")
        if restored_by_afm:
            checks.check(now.get("port") == orig.get("port"), f"FDB|{vlan}|{mac} came back on {now.get('port')}, "
                                                              f"was {orig.get('port')}")
    finally:
        cur = sw.fdb_entry(vlan, mac, ns)
        if not cur or cur.get("port") != orig.get("port", port):
            # missing, or AFM put it back on another port: the original entry again
            sw.fdb_set(vlan, mac, orig.get("port", port), ns, orig.get("type", "static"))
            record_property("restored_by_test", True)
            evidence.event("cleanup", action="fdb_set by the test" + (f" (it was on {cur.get('port')})" if cur else ""))
    checks.check(_present(sw, vlan, mac, ns), f"FDB|{vlan}|{mac} missing at the end")
    evs = ops.events(since=since)
    seen = _event_types(evs)
    record_property("events_seen", seen)
    evidence.fact(afm_events=afm_events(evs), entry_after=sw.fdb_entry(vlan, mac, ns))
    _required_events(ops, checks, "fdb_mismatch_events", seen, "FDB mismatch")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-062")
def test_fdb_mismatch_foreign_entry_removed(ops, rack, afm_ready, checks, record_property, evidence):
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
    evidence.fact(case="fdb_foreign_entry", switch=sw.name, ns=ns, vlan=vlan, mac=mac, port=port,
                  fdb_reconcile_s=float(ops.th("fdb_reconcile_s", 300)))
    since = ops.controller_now()
    t0 = time.monotonic()
    removed_by_afm = False
    try:
        evidence.watch("fdb_present", _present(sw, vlan, mac, ns))
        r = sw.fdb_set(vlan, mac, port, ns)
        evidence.event("inject", action="fdb_set (foreign MAC)", rc=r.rc)
        checks.check(r.ok, f"adding FDB|{vlan}|{mac} failed: rc {r.rc} {r.tail()!r}")
        checks.check(ops.wait(lambda: evidence.watch("fdb_present", _present(sw, vlan, mac, ns)), 30, 3),
                     f"FDB|{vlan}|{mac} not present after the add")
        removed_by_afm = bool(ops.wait(lambda: not evidence.watch("fdb_present", _present(sw, vlan, mac, ns)),
                                       float(ops.th("fdb_reconcile_s", 300)), 10))
        evidence.event("repaired_by_afm" if removed_by_afm else "not_repaired_in_time")
        record_property("removed_by_afm_s", round(time.monotonic() - t0, 1) if removed_by_afm else None)
        checks.check(removed_by_afm, f"AFM did not remove the foreign {sw.name} {ns} FDB|{vlan}|{mac} within "
                                     f"{ops.th('fdb_reconcile_s', 300)} s (FDB mismatch not repaired)")
    finally:
        if _present(sw, vlan, mac, ns):
            sw.fdb_delete(vlan, mac, ns)
            record_property("removed_by_test", True)
            evidence.event("cleanup", action="fdb_delete by the test")
    checks.check(not _present(sw, vlan, mac, ns), f"foreign FDB|{vlan}|{mac} still present at the end")
    evs = ops.events(since=since)
    seen = _event_types(evs)
    record_property("events_seen", seen)
    evidence.fact(afm_events=afm_events(evs))
    _required_events(ops, checks, "fdb_mismatch_events", seen, "FDB mismatch")
    checks.assert_all()
