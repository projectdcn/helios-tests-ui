"""HC-000, HC-007..HC-009 - AFM side of the full-rack health check (helios/healthcheck.py, tools/rack_health.py).

HC-000 fails on any AFM rack-state problem (helios.afm.rack_health: cluster, switch / compute admission and health,
system vPOD state, pending operations) and records the snapshot that ``tools/rack_health.py --baseline`` compares
between runs (before / after a disruptive batch): cluster, nodes, system vPOD, node-agent placement, up fabric ports
and per-tray IFCP state. HC-007 leftover test objects, HC-008 recent critical events, HC-009 controller restarts.
Read-only.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from helios.afm import AUTH, CLUSTER, MONITORING, conditions_ok, rack_health
from helios.rack import fan_out

pytestmark = pytest.mark.l1


def _state(obj) -> dict:
    st = obj.get("status") or {}
    return {"admission": st.get("admission-phase"), "healthy": conditions_ok(obj)}


def _ifcp(ops, tray) -> dict:
    """Per-tray IFCP state for the snapshot: LINK_UP ports, sessions, and the ports that are enabled but down or up
    without a session."""
    st = ops.ifcp(tray)
    up = {k for k, v in st.items() if v["link"] == "LINK_UP"}
    sess = {k for k in up if st[k]["ll_up"] and st[k]["peer_enabled"] and not st[k]["error"]}
    down = sorted(f"{b} p{p}" for (b, p), v in st.items() if v["admin"] == "enabled" and v["link"] != "LINK_UP")
    return {"link_up": len(up), "sessions": len(sess),
            "no_session": sorted(f"{b} p{p}" for b, p in up - sess), "enabled_down": down}


@pytest.mark.healthcheck("blocker")
@pytest.mark.tc("HC-000")
def test_afm_rack_state_and_snapshot(afm, ops, rack, inventory, checks, record_property):
    """AFM reports a healthy rack; snapshot of cluster, nodes, system vPOD, node-agent placement, up fabric ports and
    per-tray IFCP state."""
    snap: dict = {}
    cl = afm.cluster().get("status") or {}
    snap["cluster"] = {"healthy": conditions_ok(afm.cluster()), "leader": cl.get("leader"),
                       "quorum": sorted(m.get("name") for m in (cl.get("quorum-status") or {}).get("members") or [])}
    snap["switches"] = {s["meta"]["name"]: _state(s) for s in afm.switches()}
    snap["computes"] = {c["meta"]["name"]: _state(c) for c in afm.computes()}
    if inventory.quarantined:
        snap["quarantined"] = inventory.quarantine_summary()
    sv = afm.system_vpod()
    if sv:
        st = sv.get("status") or {}
        snap["system_vpod"] = {"name": sv["meta"]["name"], "state": st.get("vpod-state"),
                               "planes": list(ops.planes(sv["meta"]["name"])), "pending_ops": len(st.get("pending-ops") or [])}
    switches = list(rack.switches.values())
    agents, ports = {}, {}
    for sw, res, exc in fan_out(lambda s: (ops.sb_agents(s), s.namespaces()), switches):
        if exc:
            agents[sw.name] = f"error: {str(exc)[:80]}"
            continue
        slots, nss = res
        agents[sw.name] = sorted(slots)
        up = 0
        for ns in nss:
            up += sum(1 for r in sw.interfaces_status(ns) if str(r.get("Oper", "")).lower() == "up")
        ports[sw.name] = up
    snap["node_agent_slots"] = agents
    snap["fabric_ports_up"] = ports
    snap["ifcp"] = {c.name: (res if not exc else "unreachable")
                    for c, res, exc in fan_out(lambda t: _ifcp(ops, t), list(rack.computes.values()))}
    try:
        tm = ops.tray_map()
    except Exception:  # noqa: BLE001 - naming only
        tm = {}
    slots = {c["meta"]["name"]: (c.get("spec") or {}).get("slot-id") for c in afm.computes()}
    snap["tray_slots"] = {t: {"slot": slots.get(n), "afm": n} for n, t in sorted(tm.items(), key=lambda kv: kv[1])}
    record_property("snapshot", snap)
    for problem in rack_health(afm, inventory.is_quarantined_afm, ops.label if tm else None):
        checks.fail(problem)
    checks.assert_all()


@pytest.mark.healthcheck("blocker")
@pytest.mark.tc("HC-007")
def test_no_leftover_test_objects(afm, checks, record_property):
    """No helios-* object left behind by an earlier (aborted) run: vPODs, failover / alert policies, users, roles,
    role bindings. Leftovers skew the next batch (a vPOD holds GPUs; policies and users change behaviour)."""
    t = afm.tenant
    kinds = {"vPOD": f"{CLUSTER}/vpods", "failover policy": f"{CLUSTER}/failoverpolicies",
             "alert policy": f"{MONITORING}/alertPolicies", "user": f"{AUTH}/tenant/{t}/users",
             "role": f"{AUTH}/tenant/{t}/roles", "role binding": f"{AUTH}/tenant/{t}/role-bindings"}
    found = {}
    for kind, path in kinds.items():
        try:
            names = [o["meta"]["name"] for o in afm.items(path)]
        except Exception as e:  # noqa: BLE001 - one unreadable kind is reported, the rest still checked
            checks.fail(f"cannot list {kind}s ({path}): {str(e)[:120]}")
            continue
        left = sorted(n for n in names if n.startswith("helios-"))
        found[kind] = left
        checks.check(not left, f"{len(left)} leftover {kind}(s): {left[:8]} - delete them before the next batch")
    record_property("leftovers", found)
    checks.assert_all()


def _ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@pytest.mark.healthcheck("warn")
@pytest.mark.tc("HC-008")
def test_no_recent_critical_events(ops, inventory, checks, record_property):
    """No severity=critical AFM event in the last ``health.event_window_min`` minutes (default 60), counted back from
    the newest event (controller clock - the runner's may be skewed) - or, when tools/rack_health.py runs with a
    baseline, since the previous health run started ($HELIOS_HEALTH_SINCE, UTC), so nothing between runs is missed
    (the longer of the two windows)."""
    window = int(inventory.th("health.event_window_min", 60))
    if window <= 0:
        pytest.skip("health.event_window_min: 0 (disabled)")
    evs = ops.events(page=int(inventory.th("health.event_page", 1000)))
    newest = max((t for e in evs if (t := _ts((e.get("meta") or {}).get("creation-time", "")))), default=None)
    if newest is None:
        record_property("critical_events", {})
        return
    since = newest - timedelta(minutes=window)
    prev = _ts(os.environ.get("HELIOS_HEALTH_SINCE", "").replace(" ", "T"))
    if prev is not None and prev < since:
        since = prev
    record_property("events_since", since.strftime("%Y-%m-%d %H:%M:%S UTC"))
    crit = [e for e in evs if str(e.get("severity", "")).lower() == "critical"
            and (t := _ts((e.get("meta") or {}).get("creation-time", ""))) and t >= since]
    by_type: dict[str, list] = {}
    for e in crit:
        by_type.setdefault(e.get("type") or "?", []).append(e)
    record_property("critical_events", {k: len(v) for k, v in by_type.items()})
    for typ, es in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        e = es[0]
        ref = e.get("object-ref") or {}
        checks.fail(f"{len(es)} critical '{typ}' event(s) since {since:%Y-%m-%d %H:%M} UTC, e.g. {ref.get('name', '')}: "
                    f"{str(e.get('message', ''))[:140]}")
    checks.assert_all()


@pytest.mark.healthcheck("warn")
@pytest.mark.tc("HC-009")
def test_controllers_not_recently_restarted(ops, inventory, checks, record_property):
    """Every AFM controller container has been up at least ``health.controller_min_uptime_min`` minutes (default 60).
    StartedAt and RestartCount go into the snapshot, so --baseline shows any restart between two runs."""
    min_up = float(inventory.th("health.controller_min_uptime_min", 60))   # 0: record only, never fail
    ctrls = ops.controllers()
    state = {}

    def probe(item):
        ip, sw = item
        started, restarts = ops.controller_state(sw)
        now = sw.run("date -u +%Y-%m-%dT%H:%M:%S", timeout=30).stdout.strip()
        return started, restarts, now

    for (ip, sw), res, exc in fan_out(probe, list(ctrls.items())):
        name = getattr(sw, "name", ip)
        if exc or sw is None:
            checks.fail(f"controller {ip}: cannot read container state: {exc or 'no inventory switch'}")
            continue
        started, restarts, now = res
        state[name] = {"ip": ip, "started": started, "restarts": restarts}
        t0, t1 = _ts(started), _ts(now)
        if not checks.check(t0 and t1, f"{name} ({ip}): afm-controller not running or no start time ({started!r})"):
            continue
        up_min = (t1 - t0).total_seconds() / 60
        checks.check(up_min >= min_up, f"{name} ({ip}): afm-controller restarted {up_min:.0f} min ago "
                                       f"(started {started}, restart count {restarts})")
    record_property("snapshot", {"controllers": state})
    checks.assert_all()
