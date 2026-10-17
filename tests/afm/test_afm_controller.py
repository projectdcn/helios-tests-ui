"""AFM controller resilience (DISRUPTIVE, needs --allow-disruptive): controller container stop/start/restart on
the leader and non-leaders, single-node failure and rejoin, AggWatch across controller restarts, controller logs,
full controller restart. Controller nodes are the cluster's quorum members, reached over switch SSH by
management IP. Cases needing a failover skip on a one-controller mini rack.
"""
from __future__ import annotations

import time

import pytest

from helios.afm import CLUSTER, MONITORING, conditions_ok, ev_names
from helios.afm_ops import TERMINAL

AP = f"{MONITORING}/alertPolicies"
FP = f"{CLUSTER}/failoverpolicies"
pytestmark = [pytest.mark.l3, pytest.mark.disruptive]


def _need(ops, n):
    m = len(ops.quorum_ips())
    if m < n:
        pytest.skip(f"{m} controller(s): needs {n}")


def _members(ops) -> dict:
    st = ops.afm.cluster().get("status") or {}
    return {m.get("name"): m.get("status") for m in (st.get("quorum-status") or {}).get("members") or []}


def _all_started(ops) -> bool:
    """Quorum healthy and every member started; an unreachable API (a controller being restarted - possibly the one
    serving afm.url) counts as "not yet", so the wait rides the restart out instead of crashing."""
    try:
        return conditions_ok(ops.afm.cluster()) and all(v == "started" for v in _members(ops).values())
    except Exception:  # noqa: BLE001 - connection refused / reset while a controller restarts
        return False


def _non_leader(ops) -> str:
    """A non-leader controller that does not serve afm.url (skip when there is none)."""
    leader, api = ops.leader_ip(), _api_ip(ops)
    m = next((m for m in ops.quorum_ips() if m not in (leader, api)), None)
    if m is None:
        pytest.skip(f"no non-leader controller other than the API address {api} (leader {leader})")
    return m


def _api_ip(ops) -> str:
    """The controller address the tests talk to (afm.url) - restarting it cuts the test's own connection."""
    from urllib.parse import urlparse
    return urlparse(ops.afm.base).hostname or ""



_WRITE_GIVE_UP_S = 180


def _write_after_started(ops, name, record_property):
    """POST the alert policy, retrying while the API refuses it, from the moment every controller reported
    'started'. Returns the write outage in seconds (0 = first try worked), None when no write worked within
    _WRITE_GIVE_UP_S. g02-g17 17 Oct: right after a leader restart reported 'started', the POST got HTTP 503
    'dial tcp 10.255.4.72:9003: connect: connection refused' - this measures how long that lasts."""
    t0, errors = time.monotonic(), []
    k = float(ops.th("time_scale", 1))
    while True:
        try:
            r = ops.afm.raw("POST", AP, _ap(name))
            if r.status_code in (200, 201):
                break
            errors.append(f"HTTP {r.status_code}: {' '.join(r.text.split())[:160]}")
        except Exception as e:  # noqa: BLE001 - connection refused / reset while the cluster settles
            errors.append(f"{type(e).__name__}: {str(e)[:160]}")
        if time.monotonic() - t0 > _WRITE_GIVE_UP_S * k:
            record_property("write_errors", errors[:3] + ([f"... {len(errors) - 3} more"] if len(errors) > 3 else []))
            return None
        time.sleep(max(0.05, 5 * k))
    outage = round(time.monotonic() - t0, 1)
    record_property("write_outage_s", outage)
    if errors:
        record_property("write_errors", errors[:3] + ([f"... {len(errors) - 3} more"] if len(errors) > 3 else []))
    return outage

def _crud_works(ops, uniq) -> tuple[bool, str]:
    name = uniq("fp")
    r1 = ops.afm.raw("POST", FP, {"meta": {"name": name}, "spec": {"datafabric-reconfig": "manual", "link-fail-threshold": 10}})
    r2 = ops.afm.raw("DELETE", f"{FP}/{name}")
    return r1.status_code == 200 and r2.status_code in (200, 204), f"create {r1.status_code} delete {r2.status_code}"


def _rack_settled(ops, checks):
    checks.check(ops.wait(lambda: _all_started(ops), 300, 10), f"controllers not all started: {_members(ops)}")
    for s in ops.afm.switches():
        checks.check(ops.wait(lambda n=s["meta"]["name"]: ops.switch_ready(n), 300, 10), f"{s['meta']['name']} not healthy")
    for c in ops.afm.computes():
        checks.check(ops.wait_compute_ready(c["meta"]["name"], 600), f"{c['meta']['name']} not healthy")
    checks.check(ops.wait_vpod(ops.system_vpod_name(), timeout=600) in TERMINAL, "system vPOD not online/degraded")


@pytest.mark.tc("TC-CTL-074")
def test_stop_start_leader_controller(ops, afm_ready, uniq, checks, record_property):
    """docker stop afm-controller on the leader: a new leader is elected and the API keeps working (CRUD);
    docker start: the old leader rejoins as a started member."""
    _need(ops, 2)
    leader = ops.leader_ip()
    sw = ops.ssh_by_ip(leader)
    try:
        ops.controller_action(sw, "stop")
        moved = ops.wait(lambda: ops.leader_ip() not in (None, "", leader), 180, 5)
        record_property("leader", {"before": leader, "after": ops.leader_ip()})
        checks.check(moved, f"no new leader within 180s of stopping {leader}")
        ok, how = _crud_works(ops, uniq)
        checks.check(ok, f"API CRUD with the leader stopped: {how}")
    finally:
        ops.controller_action(sw, "start")
    checks.check(ops.wait(lambda: _members(ops).get(leader) == "started", 300, 10),
                 f"{leader} did not rejoin: {_members(ops)}")
    _rack_settled(ops, checks)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-133")
def test_cluster_tolerates_single_node_failure(ops, afm_ready, uniq, checks, record_property):
    """One non-leader controller stopped: the cluster stays healthy (quorum kept), the leader does not change,
    API reads and writes keep working, agents stay connected."""
    _need(ops, 3)
    leader = ops.leader_ip()
    victim = _non_leader(ops)                  # not the controller serving afm.url (its API would go away)
    sw = ops.ssh_by_ip(victim)
    try:
        ops.controller_action(sw, "stop")
        ops.wait(lambda: _members(ops).get(victim) != "started", 120, 5)
        record_property("members_with_one_down", _members(ops))
        checks.check(conditions_ok(ops.afm.cluster()), "cluster not healthy with one of three controllers down")
        checks.check(ops.leader_ip() == leader, f"leader moved {leader} -> {ops.leader_ip()}")
        ok, how = _crud_works(ops, uniq)
        checks.check(ok, f"API CRUD with one controller down: {how}")
        checks.check(all(ops.switch_ready(s["meta"]["name"]) for s in ops.afm.switches()), "a switch went unhealthy")
    finally:
        ops.controller_action(sw, "start")
    _rack_settled(ops, checks)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-134")
def test_failed_node_rejoins(ops, afm_ready, checks, record_property):
    """A controller node that was down (stopped for thresholds.afm.node_down_s, 60 s) rejoins: started again,
    cluster healthy, and its container is serving (StartedAt advanced)."""
    _need(ops, 2)
    victim = _non_leader(ops)
    sw = ops.ssh_by_ip(victim)
    before = ops.controller_state(sw)
    try:
        ops.controller_action(sw, "stop")
        time.sleep(float(ops.th("node_down_s", 60)) * float(ops.th("time_scale", 1)))
    finally:
        ops.controller_action(sw, "start")
    ok = ops.wait(lambda: _all_started(ops) and _members(ops).get(victim) == "started", 300, 10)
    after = ops.controller_state(sw)
    record_property("container", {"before": before, "after": after})
    checks.check(ok, f"{victim} did not rejoin: {_members(ops)}")
    checks.check(after[0] != before[0], "controller container StartedAt did not change after start")
    _rack_settled(ops, checks)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-124")
def test_controller_logs_after_restart(ops, afm_ready, checks, record_property):
    """After docker restart afm-controller, the controller writes fresh log files under
    /opt/amd/afm/controller/logs (switch clock, so runner skew does not matter)."""
    ip = _non_leader(ops)
    sw = ops.ssh_by_ip(ip)
    t0 = ops.epoch(sw)
    ops.restart_controller(sw)
    got = ops.wait(lambda: ops.controller_logs_since(sw, t0), 120, 10)
    record_property("new_logs", ops.controller_logs_since(sw, t0)[:5])
    checks.check(got, f"no controller log written on {ip} after the restart")
    _rack_settled(ops, checks)
    checks.assert_all()


def _restart_all_controllers(ops, checks, record_property):
    t0 = time.monotonic()
    for ip, sw in ops.controllers().items():
        ops.restart_controller(sw)
    _rack_settled(ops, checks)
    record_property("recovery_s", round(time.monotonic() - t0, 1))


@pytest.mark.stress
@pytest.mark.tc("TC-STR-010")
def test_full_controller_restart(ops, afm_ready, checks, record_property):
    """All controller containers restarted together: cluster, every switch and compute, and the system vPOD
    recover (state rebuilt from etcd) within 600 s each."""
    _restart_all_controllers(ops, checks, record_property)
    checks.assert_all()


@pytest.mark.tc("TC-AR-003")
def test_restart_afm_controller(ops, afm_ready, checks, record_property):
    """Restart the AFM controller (all controller containers): agents reconnect and the rack returns to
    healthy (as TC-STR-010)."""
    _restart_all_controllers(ops, checks, record_property)
    checks.assert_all()


# ------------------------------------------------------------------ AggWatch across restarts

def _ap(name):
    return {"meta": {"name": name}, "spec": {"resource": "Event", "enable": True, "severity": "info"}}


@pytest.mark.tc("TC-AFM-AGGW-023")
def test_watch_survives_nonleader_restart(ops, afm_ready, uniq, cleanup, checks, record_property):
    """An open AggWatch stream keeps delivering events across a non-leader controller restart."""
    _need(ops, 2)
    wait = float((ops.th("aggwatch", {}) or {}).get("event_wait_s", 30))
    w = ops.afm.watch()
    w.subscribe("monitoring/AlertPolicy")
    w.collect(min(wait, 5))
    # a non-leader that does not serve afm.url: restarting the one we talk to closes the stream by itself (g02-g17
    # 17 Oct: st1 = 10.255.4.71 = afm.url was picked, the API went away for ~60 s and the next tests errored)
    leader, api = ops.leader_ip(), _api_ip(ops)
    victim = next((m for m in ops.quorum_ips() if m not in (leader, api)), None)
    record_property("leader_api_victim", {"leader": leader, "api": api, "victim": victim})
    if victim is None:
        w.close()
        pytest.skip(f"no non-leader controller other than the API address {api} (leader {leader})")
    ops.restart_controller(ops.ssh_by_ip(victim))
    ops.wait(lambda: _all_started(ops), 300, 10)
    name = uniq("ap")
    cleanup(f"{AP}/{name}")
    ops.afm.post(AP, _ap(name))
    w.collect(wait, lambda evs: name in ev_names(evs, "Created"))
    record_property("stream_closed", w.closed)
    checks.check(not w.closed, "stream closed by a non-leader restart")
    checks.check(name in ev_names(w.events, "Created"), f"no Created for {name} after the non-leader restart")
    w.close()
    checks.assert_all()


@pytest.mark.tc("TC-AFM-AGGW-024")
def test_watch_during_leader_failover(ops, afm_ready, uniq, cleanup, checks, record_property):
    """Leader controller restarted under an open stream: the stream either keeps delivering or closes cleanly;
    a reconnect within 120 s succeeds and lists the object created after the failover."""
    _need(ops, 2)
    wait = float((ops.th("aggwatch", {}) or {}).get("event_wait_s", 30))
    w = ops.afm.watch()
    w.subscribe("monitoring/AlertPolicy")
    w.collect(min(wait, 5))
    ops.restart_controller(ops.ssh_by_ip(ops.leader_ip()))
    ops.wait(lambda: _all_started(ops), 300, 10)
    name = uniq("ap")
    cleanup(f"{AP}/{name}")
    outage = _write_after_started(ops, name, record_property)
    limit = float((ops.th("aggwatch", {}) or {}).get("write_recover_s", 30))
    if outage is None:
        w.close()
        pytest.fail(f"no write worked within {_WRITE_GIVE_UP_S:.0f} s after every controller reported 'started' "
                    "(see write_errors)")
    checks.check(outage <= limit, f"writes failed for {outage:.0f} s after every controller reported 'started' "
                                  f"(limit {limit:.0f} s, thresholds.afm.aggwatch.write_recover_s)")
    w.collect(wait, lambda evs: name in ev_names(evs, "Created"))
    kept = name in ev_names(w.events, "Created")
    w.close()
    seen = {"kept_stream": kept}
    if not kept:
        def reconnect():
            try:
                w2 = ops.afm.watch()
            except Exception:  # noqa: BLE001 - handshake refused while failing over
                return False
            w2.subscribe("monitoring/AlertPolicy")
            w2.collect(wait, lambda evs: name in ev_names(evs, "Created"))
            w2.close()
            return name in ev_names(w2.events, "Created")
        seen["reconnected"] = bool(ops.wait(reconnect, 120, 10))
        checks.check(seen["reconnected"], "no working AggWatch connection within 120 s of the leader failover")
    record_property("behaviour", seen)
    checks.assert_all()
