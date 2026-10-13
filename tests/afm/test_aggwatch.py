"""AFM AggWatch: the aggregated watch WebSocket ``wss://<afm>/configs/aggwatch/v1/watch``.

The client sends ``{"meta":{}, "watch-options":[{"group","kind","options":{"meta":{}}}]}`` after the 101
handshake; the server streams ``{"events":[{"type":"Created|Updated|Deleted","kind","group","object"}]}``
(the initial list arrives as Created events). Objects created by the tests are AlertPolicies /
FailoverPolicies named ``helios-*``, deleted on teardown. Handshake-rejection cases need no password.
"""
from __future__ import annotations

import secrets
import threading
import time

import pytest

from helios.afm import CLUSTER, MONITORING, ev_names, handshake_status, perm, wait_for

AP = f"{MONITORING}/alertPolicies"
FP = f"{CLUSTER}/failoverpolicies"


def _ap_body(name: str, severity: str = "info") -> dict:
    return {"meta": {"name": name}, "spec": {"resource": "Event", "enable": True, "severity": severity}}


def _has(name: str, etype: str):
    return lambda evs: name in ev_names(evs, etype)


class _Watches:
    """watch(*kinds, client=afm) -> open, subscribed Watch (all closed after the test). Timings from
    thresholds.afm.aggwatch: event_wait_s (30) for an expected event, settle_s (5) for the initial list,
    quiet_s (15) to wait for something that must NOT arrive."""

    def __init__(self, afm, inventory):
        self.afm, self.opened = afm, []
        t = inventory.th("afm.aggwatch", {}) or {}
        self.event_wait = float(t.get("event_wait_s", 30))
        self.settle = float(t.get("settle_s", 5))
        self.quiet = float(t.get("quiet_s", 15))

    def __call__(self, *kinds, client=None, **kw):
        w = (client or self.afm).watch(**kw)
        self.opened.append(w)
        if kinds:
            w.subscribe(*kinds)
        return w


@pytest.fixture
def watch(afm, inventory):
    ws = _Watches(afm, inventory)
    yield ws
    for w in ws.opened:
        w.close()


# ------------------------------------------------------------------ handshake / auth (no password needed)

@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-003")
def test_reject_unauthenticated(afm_anon, checks, record_property):
    """Handshake without a session cookie is refused (HTTP 401/403, never 101)."""
    code = handshake_status(afm_anon, cookie=None)
    record_property("handshake_status", code)
    checks.check(code in (401, 403), f"no-cookie handshake -> HTTP {code}, expected 401/403")
    checks.assert_all()


def _bogus_sid_rejected(afm_anon, checks, record_property):
    code = handshake_status(afm_anon, cookie=secrets.token_hex(32))
    record_property("handshake_status", code)
    checks.check(code in (401, 403), f"invalid-sid handshake -> HTTP {code}, expected 401/403")


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-004")
def test_reject_expired_session(afm_anon, checks, record_property):
    """A sid that is not a live session (expired/never issued) is refused at the handshake."""
    _bogus_sid_rejected(afm_anon, checks, record_property)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-004")
def test_ws_invalid_session_rejected(afm_anon, checks, record_property):
    """WebSocket connections with expired/invalid sessions are rejected (as TC-AFM-AGGW-004)."""
    _bogus_sid_rejected(afm_anon, checks, record_property)
    checks.assert_all()


# ------------------------------------------------------------------ connect + subscribe

@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-001")
def test_connect_valid_session(afm, watch, checks, record_property):
    """Valid sid -> 101; subscribing to ComputeNode returns the current computes as Created events."""
    w = watch("cluster/ComputeNode")
    want = {c["meta"]["name"] for c in afm.computes()}
    w.collect(watch.event_wait, lambda evs: want <= set(ev_names(evs, "Created", "ComputeNode")))
    got = set(ev_names(w.events, "Created", "ComputeNode"))
    record_property("initial_created", sorted(got))
    checks.check(want <= got, f"initial ComputeNode list missing {sorted(want - got)}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-005")
def test_watch_single_kind(afm, watch, uniq, cleanup, checks):
    """Watching only AlertPolicy: the initial list + a new policy arrive, and no other kind is delivered."""
    w = watch("monitoring/AlertPolicy")
    w.collect(watch.settle)
    name = uniq("ap")
    cleanup(f"{AP}/{name}")
    afm.post(AP, _ap_body(name))
    w.collect(watch.event_wait, _has(name, "Created"))
    checks.check(name in ev_names(w.events, "Created"), f"no Created event for {name}")
    other = {e.get("kind") for e in w.events} - {"AlertPolicy"}
    checks.check(not other, f"events of unwatched kinds delivered: {other}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-006")
def test_watch_multiple_kinds(afm, watch, checks, record_property):
    """One connection watching ComputeNode + SwitchNode + VPod + FailoverPolicy gets the initial objects of each."""
    kinds = {"ComputeNode": afm.computes(), "SwitchNode": afm.switches(), "VPod": afm.vpods(),
             "FailoverPolicy": afm.items(FP)}
    w = watch(*(f"cluster/{k}" for k in kinds))
    want = {k: {o["meta"]["name"] for o in v} for k, v in kinds.items()}
    w.collect(watch.event_wait, lambda evs: all(want[k] <= set(ev_names(evs, "Created", k)) for k in want))
    counts = {}
    for k in kinds:
        got = set(ev_names(w.events, "Created", k))
        counts[k] = len(got)
        checks.check(want[k] <= got, f"{k}: initial list missing {sorted(want[k] - got)[:10]}")
    record_property("initial_counts", counts)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-014")
def test_admin_receives_all(afm, watch, uniq, cleanup, checks):
    """Admin watching cluster + monitoring kinds receives creates of both (FailoverPolicy + AlertPolicy)."""
    w = watch("cluster/FailoverPolicy", "monitoring/AlertPolicy")
    w.collect(watch.settle)
    fp, ap = uniq("fp"), uniq("ap")
    cleanup(f"{FP}/{fp}")
    cleanup(f"{AP}/{ap}")
    afm.post(FP, {"meta": {"name": fp}, "spec": {"datafabric-reconfig": "manual", "link-fail-threshold": 10}})
    afm.post(AP, _ap_body(ap))
    w.collect(watch.event_wait, lambda evs: {fp, ap} <= set(ev_names(evs, "Created")))
    for n in (fp, ap):
        checks.check(n in ev_names(w.events, "Created"), f"admin did not receive Created for {n}")
    checks.assert_all()


def _invalid_kind(afm, watch, checks, record_property):
    w = watch("cluster/HeliosNoSuchKind")
    w.collect(watch.quiet)
    record_property("server_reply", {"closed": w.closed, "errors": w.errors[:3], "events": len(w.events)})
    checks.check(w.closed or w.errors or not w.events, "invalid kind produced events")
    w2 = watch("cluster/ComputeNode")
    w2.collect(watch.event_wait, lambda evs: bool(ev_names(evs, "Created", "ComputeNode")))
    checks.check(ev_names(w2.events, "Created", "ComputeNode"), "fresh connection after an invalid kind got no events")


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-007")
def test_watch_invalid_kind(afm, watch, checks, record_property):
    """Unknown kind: the server errors or closes (no events, no crash); a fresh connection still works."""
    _invalid_kind(afm, watch, checks, record_property)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-008")
def test_empty_watch_options(afm, watch, checks, record_property):
    """``watch-options: []``: no events; the server errors/closes or stays idle; the cluster stays healthy."""
    w = watch()
    w.send({"meta": {}, "watch-options": []})
    w.collect(watch.quiet)
    record_property("server_reply", {"closed": w.closed, "errors": w.errors[:3], "events": len(w.events)})
    checks.check(not w.events, f"{len(w.events)} events for an empty subscription")
    checks.check(afm.raw("GET", f"{CLUSTER}/cluster").status_code == 200, "AFM API not answering afterwards")
    checks.assert_all()


# ------------------------------------------------------------------ CRUD events

def _crud_events(afm, watch, uniq, cleanup, checks):
    w = watch("monitoring/AlertPolicy")
    w.collect(watch.settle)
    name = uniq("ap")
    path = f"{AP}/{name}"
    cleanup(path)
    afm.post(AP, _ap_body(name))
    w.collect(watch.event_wait, _has(name, "Created"))
    checks.check(name in ev_names(w.events, "Created"), f"no Created for {name}")
    obj = afm.get(path)
    obj["spec"]["severity"] = "warn"
    afm.put(path, {"meta": obj["meta"], "spec": obj["spec"]})
    w.collect(watch.event_wait, _has(name, "Updated"))
    upd = [e for e in w.events if e.get("type") == "Updated" and ev_names([e]) == [name]]
    checks.check(upd, f"no Updated for {name}")
    if upd:
        checks.check(((upd[-1].get("object") or {}).get("spec") or {}).get("severity") == "warn",
                     f"Updated event carries spec {(upd[-1].get('object') or {}).get('spec')}")
    afm.delete(path)
    w.collect(watch.event_wait, _has(name, "Deleted"))
    checks.check(name in ev_names(w.events, "Deleted"), f"no Deleted for {name}")
    seq = [e.get("type") for e in w.events if ev_names([e]) == [name]]
    checks.check(seq[:1] == ["Created"] and seq[-1:] == ["Deleted"], f"event order for {name}: {seq}")
    return seq


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-010")
def test_created_event(afm, watch, uniq, cleanup, checks):
    """Creating an AlertPolicy pushes a Created event (full CRUD sequence checked)."""
    _crud_events(afm, watch, uniq, cleanup, checks)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-011")
def test_updated_event(afm, watch, uniq, cleanup, checks):
    """Modifying an AlertPolicy pushes an Updated event carrying the new spec."""
    _crud_events(afm, watch, uniq, cleanup, checks)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-011")
def test_updated_event_alertpolicy(afm, watch, uniq, cleanup, checks):
    """Updated event is received when an AlertPolicy is modified (as TC-AFM-AGGW-011)."""
    _crud_events(afm, watch, uniq, cleanup, checks)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-012")
def test_deleted_event(afm, watch, uniq, cleanup, checks):
    """Deleting an AlertPolicy pushes a Deleted event, after Created/Updated."""
    _crud_events(afm, watch, uniq, cleanup, checks)
    checks.assert_all()


def _multi_kind_crud(afm, watch, uniq, cleanup, checks):
    w = watch("monitoring/AlertPolicy", "cluster/FailoverPolicy")
    w.collect(watch.settle)
    ap, fp = uniq("ap"), uniq("fp")
    cleanup(f"{AP}/{ap}")
    cleanup(f"{FP}/{fp}")
    afm.post(AP, _ap_body(ap))
    afm.post(FP, {"meta": {"name": fp}, "spec": {"datafabric-reconfig": "manual", "link-fail-threshold": 10}})
    afm.delete(f"{AP}/{ap}")
    afm.delete(f"{FP}/{fp}")
    w.collect(watch.event_wait, lambda evs: {ap, fp} <= set(ev_names(evs, "Deleted")))
    for n, k in ((ap, "AlertPolicy"), (fp, "FailoverPolicy")):
        for t in ("Created", "Deleted"):
            checks.check(n in ev_names(w.events, t, k), f"no {t} {k} event for {n}")


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-013")
def test_multi_kind_crud_stream(afm, watch, uniq, cleanup, checks):
    """One connection, AlertPolicy + FailoverPolicy: Created and Deleted for both kinds."""
    _multi_kind_crud(afm, watch, uniq, cleanup, checks)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-013")
def test_multi_kind_crud_ctl(afm, watch, uniq, cleanup, checks):
    """CRUD events from multiple watched kinds on a single connection (as TC-AFM-AGGW-013)."""
    _multi_kind_crud(afm, watch, uniq, cleanup, checks)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-006")
def test_single_kind_watch_kinds(afm, watch, checks, record_property):
    """A single-kind watch works for each cluster kind (ComputeNode, SwitchNode, VPod, FailoverPolicy, Pod):
    every connection returns that kind's current objects and nothing else."""
    lists = {"ComputeNode": afm.computes(), "SwitchNode": afm.switches(), "VPod": afm.vpods(),
             "FailoverPolicy": afm.items(FP), "Pod": afm.items(f"{CLUSTER}/pods")}
    got = {}
    for kind, objs in lists.items():
        want = {o["meta"]["name"] for o in objs}
        w = watch(f"cluster/{kind}")
        w.collect(watch.event_wait, lambda evs, k=kind, wn=want: wn <= set(ev_names(evs, "Created", k)))
        names = set(ev_names(w.events, "Created", kind))
        got[kind] = len(names)
        checks.check(want <= names, f"{kind}: missing {sorted(want - names)[:10]}")
        checks.check({e.get("kind") for e in w.events} <= {kind}, f"{kind} watch delivered {({e.get('kind') for e in w.events})}")
        w.close()
    record_property("initial_counts", got)
    checks.assert_all()


# ------------------------------------------------------------------ RBAC on the stream

def _ap_reader(make_user, uniq):
    return make_user(uniq("ro"), [perm("monitoring", "AlertPolicy", ("read",))])[0]


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-015")
def test_restricted_user_permitted_events(afm, make_user, watch, uniq, cleanup, checks):
    """User with monitoring/AlertPolicy read only: receives AlertPolicy events, never ComputeNode objects."""
    user = _ap_reader(make_user, uniq)
    w = watch("monitoring/AlertPolicy", "cluster/ComputeNode", client=user)
    w.collect(watch.settle)
    name = uniq("ap")
    cleanup(f"{AP}/{name}")
    afm.post(AP, _ap_body(name))
    w.collect(watch.event_wait, _has(name, "Created"))
    checks.check(name in ev_names(w.events, "Created", "AlertPolicy"), f"restricted user missed Created {name}")
    leaked = ev_names(w.events, kind="ComputeNode")
    checks.check(not leaked, f"restricted user received ComputeNode objects {leaked[:5]}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-016")
def test_restricted_user_blocked_kind(afm, make_user, watch, uniq, checks, record_property):
    """User with AlertPolicy read only watching ComputeNode alone: rejected or silent, never ComputeNode data."""
    user = _ap_reader(make_user, uniq)
    w = watch("cluster/ComputeNode", client=user)
    w.collect(watch.quiet)
    record_property("server_reply", {"closed": w.closed, "errors": w.errors[:3], "events": len(w.events)})
    checks.check(not ev_names(w.events, kind="ComputeNode"), "ComputeNode objects delivered to a user without access")
    checks.assert_all()


def _rbac_change(afm, make_user, watch, uniq, cleanup, checks, record_property):
    t = afm.tenant
    uname = uniq("rb")
    user, _ = make_user(uname, [perm("monitoring", "AlertPolicy", ("read",))])
    w = watch("monitoring/AlertPolicy", client=user)
    w.collect(watch.settle)
    before = uniq("ap1")
    cleanup(f"{AP}/{before}")
    afm.post(AP, _ap_body(before))
    w.collect(watch.event_wait, _has(before, "Created"))
    checks.check(before in ev_names(w.events, "Created"), f"stream missed {before} before the role change")
    role_path = f"/configs/auth/v1/tenant/{t}/roles/{uname}-role"
    role = afm.get(role_path)
    afm.put(role_path, {"kind": "Role", "api-version": "v1", "meta": role["meta"],
                        "spec": {"permissions": [perm("cluster", "Pod", ("read",))]}})
    time.sleep(watch.settle)
    after = uniq("ap2")
    cleanup(f"{AP}/{after}")
    afm.post(AP, _ap_body(after))
    w.collect(watch.quiet, _has(after, "Created"))
    record_property("stream_closed_after_revoke", w.closed)
    checks.check(after not in ev_names(w.events), f"{after} delivered after AlertPolicy read was revoked")


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-029")
def test_rbac_change_during_stream(afm, make_user, watch, uniq, cleanup, checks, record_property):
    """Revoking AlertPolicy read mid-stream: policies created afterwards are not delivered."""
    _rbac_change(afm, make_user, watch, uniq, cleanup, checks, record_property)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-026")
def test_rbac_change_during_stream_ctl(afm, make_user, watch, uniq, cleanup, checks, record_property):
    """Behaviour when RBAC changes during an active AggWatch connection (as TC-AFM-AGGW-029)."""
    _rbac_change(afm, make_user, watch, uniq, cleanup, checks, record_property)
    checks.assert_all()


# ------------------------------------------------------------------ connection lifecycle

@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-019")
def test_client_disconnect(afm, watch, checks):
    """Client closes mid-stream: the API keeps answering and a new connection gets events."""
    w = watch("cluster/ComputeNode")
    w.collect(watch.settle)
    w.close()
    checks.check(afm.raw("GET", f"{CLUSTER}/cluster").status_code == 200, "API not answering after disconnect")
    w2 = watch("cluster/ComputeNode")
    w2.collect(watch.event_wait, lambda evs: bool(evs))
    checks.check(w2.events, "no events on a new connection after a client disconnect")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-020")
def test_idle_connection_alive(afm, inventory, watch, uniq, cleanup, checks, record_property):
    """Subscribed connection idle for thresholds.afm.aggwatch.idle_s (120 s) is still open and delivers a new event."""
    idle = float(inventory.th("afm.aggwatch.idle_s", 120))
    w = watch("monitoring/AlertPolicy")
    w.collect(idle)
    record_property("closed_while_idle", w.closed)
    checks.check(not w.closed, f"server closed the subscribed connection within {idle:.0f}s idle")
    if not w.closed:
        name = uniq("ap")
        cleanup(f"{AP}/{name}")
        afm.post(AP, _ap_body(name))
        w.collect(watch.event_wait, _has(name, "Created"))
        checks.check(name in ev_names(w.events, "Created"), f"no Created for {name} after {idle:.0f}s idle")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-021")
def test_concurrent_connections(afm, inventory, watch, uniq, cleanup, checks):
    """N (thresholds.afm.aggwatch.concurrent, 5) connections all receive the same Created event."""
    n = int(inventory.th("afm.aggwatch.concurrent", 5))
    ws = [watch("monitoring/AlertPolicy") for _ in range(n)]
    for w in ws:
        w.collect(watch.settle / 2)
    name = uniq("ap")
    cleanup(f"{AP}/{name}")
    afm.post(AP, _ap_body(name))
    threads = [threading.Thread(target=w.collect, args=(watch.event_wait, _has(name, "Created"))) for w in ws]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    missing = [i for i, w in enumerate(ws) if name not in ev_names(w.events, "Created")]
    checks.check(not missing, f"connections {missing} of {n} missed Created {name}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-022")
def test_reconnect(afm, watch, uniq, cleanup, checks):
    """Disconnect, then reconnect: the new stream re-lists existing objects (incl. one created while
    disconnected) and receives new events."""
    w = watch("monitoring/AlertPolicy")
    w.collect(watch.settle)
    w.close()
    gap = uniq("gap")
    cleanup(f"{AP}/{gap}")
    afm.post(AP, _ap_body(gap))
    w2 = watch("monitoring/AlertPolicy")
    w2.collect(watch.event_wait, _has(gap, "Created"))
    checks.check(gap in ev_names(w2.events, "Created"), f"policy {gap} created while disconnected not listed on reconnect")
    name = uniq("ap")
    cleanup(f"{AP}/{name}")
    afm.post(AP, _ap_body(name))
    w2.collect(watch.event_wait, _has(name, "Created"))
    checks.check(name in ev_names(w2.events, "Created"), f"no Created for {name} after reconnect")
    checks.assert_all()


# ------------------------------------------------------------------ security / robustness

@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-025")
def test_cross_origin_cswsh(afm, watch, uniq, cleanup, checks, record_property):
    """Valid sid + foreign Origin: handshake refused, or accepted but no events (never CSWSH-vulnerable)."""
    code = handshake_status(afm, origin="http://cswsh-test.invalid")
    verdict = "HANDSHAKE_REJECTED" if code != 101 else None
    if code == 101:
        w = watch("monitoring/AlertPolicy", origin="http://cswsh-test.invalid")
        w.collect(watch.settle)
        name = uniq("ap")
        cleanup(f"{AP}/{name}")
        afm.post(AP, _ap_body(name))
        w.collect(watch.quiet, lambda evs: bool(evs))
        verdict = "CSWSH_VULNERABLE" if w.events else "HANDSHAKE_ACCEPTED_NO_EVENT"
    record_property("verdict", f"{verdict} (HTTP {code})")
    checks.check(verdict != "CSWSH_VULNERABLE", "cross-origin WebSocket with a valid cookie received events")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-021")
def test_same_and_no_origin(afm, watch, checks, record_property):
    """Same-origin and no-Origin connections both work (101 + events)."""
    for origin in (afm.base, None):
        w = watch("cluster/ComputeNode", origin=origin)
        w.collect(watch.event_wait, lambda evs: bool(evs))
        checks.check(w.events, f"origin={origin!r}: no events")
        w.close()
    checks.assert_all()


def _no_options(afm, inventory, watch, checks, record_property):
    wait = float(inventory.th("afm.aggwatch.no_options_s", 60))
    w = watch()
    t0 = time.monotonic()
    w.collect(wait + 30)
    took = time.monotonic() - t0
    verdict = "CONN_CLOSED" if w.closed else "HANG_DETECTED"
    record_property("verdict", f"{verdict} after {took:.0f}s")
    checks.check(w.closed, f"server kept a connection with no watch-options open for {took:.0f}s")
    checks.check(afm.raw("GET", f"{CLUSTER}/cluster").status_code == 200, "API not answering afterwards")


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-026")
def test_no_watch_options_hang(afm, inventory, watch, checks, record_property):
    """Connect and never send watch-options: the server closes the connection (no hang)."""
    _no_options(afm, inventory, watch, checks, record_property)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-023")
def test_no_watch_options_ctl(afm, inventory, watch, checks, record_property):
    """Server does not hang when the client never sends watch options (as TC-AFM-AGGW-026)."""
    _no_options(afm, inventory, watch, checks, record_property)
    checks.assert_all()


def _malformed(afm, watch, checks, record_property):
    w = watch()
    w.send("{invalid")
    w.collect(watch.quiet)
    verdict = "SERVER_CLOSED" if w.closed else "ERROR_EVENT" if w.errors else "NO_REPLY"
    record_property("verdict", verdict)
    checks.check(not w.events, "malformed options produced object events")
    w2 = watch("cluster/ComputeNode")
    w2.collect(watch.event_wait, lambda evs: bool(ev_names(evs, "Created")))
    checks.check(ev_names(w2.events, "Created"), "fresh connection after malformed JSON got no Created events")


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-027")
def test_malformed_json(afm, watch, checks, record_property):
    """Literal ``{invalid`` as watch options: error or close, no crash; a fresh connection still works."""
    _malformed(afm, watch, checks, record_property)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-024")
def test_malformed_json_ctl(afm, watch, checks, record_property):
    """Server handles malformed JSON watch options gracefully (as TC-AFM-AGGW-027)."""
    _malformed(afm, watch, checks, record_property)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.stress
@pytest.mark.tc("TC-AFM-AGGW-028")
def test_rapid_connect_cycles(afm, inventory, watch, checks, record_property):
    """thresholds.afm.aggwatch.cycles (50) rapid connect/subscribe/first-event/disconnect cycles all succeed;
    the cluster stays healthy and the API answers afterwards."""
    n = int(inventory.th("afm.aggwatch.cycles", 50))
    bad = []
    t0 = time.monotonic()
    for i in range(n):
        try:
            w = afm.watch()
            w.subscribe("monitoring/AlertPolicy")
            w.collect(10, lambda evs: bool(evs))
            if not w.events:
                bad.append(f"cycle {i}: no initial events")
            w.close()
        except Exception as e:  # noqa: BLE001
            bad.append(f"cycle {i}: {type(e).__name__}: {e}")
    record_property("cycles", f"{n - len(bad)}/{n} ok in {time.monotonic() - t0:.0f}s")
    checks.check(not bad, f"{len(bad)} failed cycles: {bad[:5]}")
    from helios.afm import conditions_ok
    checks.check(conditions_ok(afm.cluster()), "cluster not healthy after the connect cycles")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.stress
@pytest.mark.tc("TC-AFM-AGGW-030")
def test_event_burst(afm, inventory, watch, uniq, cleanup, checks, record_property):
    """thresholds.afm.aggwatch.burst (100) back-to-back AlertPolicy creates then deletes: exactly one Created
    and one Deleted per policy, Created in creation order."""
    n = int(inventory.th("afm.aggwatch.burst", 100))
    w = watch("monitoring/AlertPolicy")
    w.collect(watch.settle)
    base = uniq("burst")
    names = [f"{base}-{i:03d}" for i in range(n)]
    for nm in names:
        cleanup(f"{AP}/{nm}")
    t0 = time.monotonic()
    for nm in names:
        afm.post(AP, _ap_body(nm))
    for nm in names:
        afm.delete(f"{AP}/{nm}")
    record_property("api_seconds", round(time.monotonic() - t0, 1))
    w.collect(max(60, n), lambda evs: set(names) <= set(ev_names(evs, "Deleted")))
    created = [x for x in ev_names(w.events, "Created") if x in set(names)]
    deleted = [x for x in ev_names(w.events, "Deleted") if x in set(names)]
    record_property("received", {"created": len(created), "deleted": len(deleted)})
    checks.check(len(created) == n and len(set(created)) == n, f"Created: {len(created)} events, {len(set(created))} unique, expected {n}")
    checks.check(len(deleted) == n and len(set(deleted)) == n, f"Deleted: {len(deleted)} events, {len(set(deleted))} unique, expected {n}")
    checks.check(created == [x for x in names if x in created], "Created events out of creation order")
    checks.assert_all()
