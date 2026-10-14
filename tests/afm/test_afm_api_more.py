"""More AFM API cases (non-disruptive): AggWatch resource-version filters and audit, bearer tokens, group
roles, local-user login, login-attempt policy validation, default event policies, event retention.

Where the plan gives only a title, the expectation is the documented AFM behaviour (results/extract-afm-api.md)
and the assumption is stated in the test docstring; calibrate against the first real run.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from helios.afm import AUTH, CLUSTER, MONITORING, ev_names, perm

AP = f"{MONITORING}/alertPolicies"
FP = f"{CLUSTER}/failoverpolicies"


def _ap(name):
    return {"meta": {"name": name}, "spec": {"resource": "Event", "enable": True, "severity": "info"}}


def _timings(inventory):
    t = inventory.th("afm.aggwatch", {}) or {}
    return float(t.get("event_wait_s", 30)), float(t.get("settle_s", 5)), float(t.get("quiet_s", 15))


# ------------------------------------------------------------------ AggWatch filters / audit / auth

def _rv_filter(afm, inventory, uniq, cleanup, checks, record_property, rv_from_list: bool):
    wait, settle, quiet = _timings(inventory)
    lst = afm.get(AP)
    rv = (lst.get("list-meta") or {}).get("resource-version") if rv_from_list else "-1"
    if rv is None:
        rv = str(max([int((o.get("meta") or {}).get("resource-version") or 0) for o in lst.get("items") or []] + [0]))
    existing = {o["meta"]["name"] for o in lst.get("items") or []}
    w = afm.watch()
    w.subscribe("monitoring/AlertPolicy", meta={"resource-version": str(rv)})
    w.collect(quiet)
    replayed = existing & set(ev_names(w.events, "Created"))
    record_property("resource_version", rv)
    checks.check(not replayed, f"watch from resource-version {rv} replayed existing objects {sorted(replayed)[:5]}")
    name = uniq("ap")
    cleanup(f"{AP}/{name}")
    afm.post(AP, _ap(name))
    w.collect(wait, lambda evs: name in ev_names(evs, "Created"))
    checks.check(name in ev_names(w.events, "Created"), f"no Created for {name} created after subscribing")
    w.close()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-009")
def test_watch_resource_version_filter(afm, inventory, uniq, cleanup, checks, record_property):
    """Subscribing with options.meta.resource-version = the list's current version: no replay of existing
    objects, only changes after it."""
    _rv_filter(afm, inventory, uniq, cleanup, checks, record_property, rv_from_list=True)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-009")
def test_watch_resource_version_latest(afm, inventory, uniq, cleanup, checks, record_property):
    """resource-version -1 (latest only): no initial list, new objects are delivered."""
    _rv_filter(afm, inventory, uniq, cleanup, checks, record_property, rv_from_list=False)
    checks.assert_all()


def _watch_audit(afm, inventory, checks, record_property):
    wait, settle, quiet = _timings(inventory)
    start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kinds = ["cluster/ComputeNode", "monitoring/AlertPolicy"]
    w = afm.watch()
    w.subscribe(*kinds)
    w.collect(settle)
    w.close()
    deadline = time.monotonic() + max(wait, 10)
    hits = []
    while time.monotonic() < deadline and not hits:
        evs = afm.items(f"/audit/v1/events?field-selector=user.name={afm.username},meta.creation-time%3E{start}"
                        f"&sort-by=meta.creation-time&sort-order=desc&page-size=500")
        hits = [e for e in evs if "watch" in json.dumps(e).lower()]
        if not hits:
            time.sleep(min(5.0, settle + 0.1))
    record_property("watch_audit", [{k: e.get(k) for k in ("action", "stage", "resource")} for e in hits[:4]])
    return hits, kinds


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-017")
def test_watch_generates_audit_event(afm, inventory, checks, record_property):
    """Opening an AggWatch subscription leaves an audit record for the user (looked up by user + time and
    matched on 'watch' - the record's exact action name is not documented)."""
    hits, _ = _watch_audit(afm, inventory, checks, record_property)
    checks.check(hits, f"no audit event mentioning the watch for {afm.username}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-AGGW-018")
def test_watch_audit_records_kinds(afm, inventory, checks, record_property):
    """The watch's audit record names the resource kinds that were watched."""
    hits, kinds = _watch_audit(afm, inventory, checks, record_property)
    blob = json.dumps(hits)
    for k in kinds:
        checks.check(k.split("/")[1] in blob, f"audit record does not mention watched kind {k}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-AFM-AGGW-002")
def test_watch_bearer_token(afm_cfg, inventory, checks, record_property):
    """AggWatch with an ``Authorization: Bearer`` token (thresholds.afm.bearer_token_env names the variable
    holding a token issued by AFM): handshake 101 and events; a garbage token is refused."""
    import os

    import websocket

    from helios.afm import AfmClient

    env = inventory.th("afm.bearer_token_env")
    if not env or not os.environ.get(env):
        pytest.skip("no bearer token: set thresholds.afm.bearer_token_env and export that variable")
    c = AfmClient(afm_cfg, anonymous=True)
    hdr = {"header": [f"Authorization: Bearer {os.environ[env]}"], "suppress_origin": True,
           "sslopt": {"cert_reqs": 0, "check_hostname": False}, "timeout": 15}
    try:
        ws = websocket.create_connection(c.ws_url(), **hdr)
        ws.close()
        ok = True
    except websocket.WebSocketBadStatusException as e:
        ok = False
        record_property("status", e.status_code)
    checks.check(ok, "valid bearer token refused")
    bad = dict(hdr, header=["Authorization: Bearer helios-invalid-token"])
    try:
        websocket.create_connection(c.ws_url(), **bad).close()
        checks.fail("invalid bearer token accepted")
    except websocket.WebSocketBadStatusException:
        pass
    checks.assert_all()


# ------------------------------------------------------------------ RBAC / auth

@pytest.mark.l3
@pytest.mark.tc("TC-CTL-117")
def test_group_role_crud(afm, make_user, uniq, checks, record_property):
    """A role granting several groups (cluster FailoverPolicy, monitoring AlertPolicy): the user can create and
    delete both kinds, reads computes only if granted (403 here), and cannot create users (auth group)."""
    t = afm.tenant
    u, _ = make_user(uniq("grp"), [perm("cluster", "FailoverPolicy"), perm("monitoring", "AlertPolicy")])
    fp, ap = uniq("fp"), uniq("ap")
    got = {}
    r = u.raw("POST", FP, {"meta": {"name": fp}, "spec": {"datafabric-reconfig": "manual", "link-fail-threshold": 10}})
    got["create FailoverPolicy"] = r.status_code
    got["delete FailoverPolicy"] = u.raw("DELETE", f"{FP}/{fp}").status_code
    got["create AlertPolicy"] = u.raw("POST", AP, _ap(ap)).status_code
    got["delete AlertPolicy"] = u.raw("DELETE", f"{AP}/{ap}").status_code
    got["read ComputeNode"] = u.raw("GET", f"{CLUSTER}/computenodes", relogin=False).status_code
    got["create User"] = u.raw("POST", f"{AUTH}/tenant/{t}/users",
                               {"meta": {"name": uniq("x"), "tenant": t}, "spec": {"type": "local", "password": "Xx1#abcdefgh"}},
                               relogin=False).status_code
    afm.delete_quiet(f"{FP}/{fp}")
    afm.delete_quiet(f"{AP}/{ap}")
    record_property("matrix", got)
    for k in ("create FailoverPolicy", "delete FailoverPolicy", "create AlertPolicy", "delete AlertPolicy"):
        checks.check(got[k] in (200, 201, 204), f"{k} -> HTTP {got[k]}, expected allowed")
    for k in ("read ComputeNode", "create User"):
        checks.check(got[k] == 403, f"{k} -> HTTP {got[k]}, expected 403")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-131")
def test_local_user_login(afm, make_user, uniq, checks):
    """A newly created local user logs in with username + password (200, sid) and can use its granted API."""
    u, pw = make_user(uniq("lu"), [perm("cluster", "_All_", ("read",))], login=False)
    r = u.login_raw()
    checks.check(r.status_code == 200, f"login -> HTTP {r.status_code}")
    checks.check(bool(u.sid), "no session cookie")
    checks.check(u.raw("GET", f"{CLUSTER}/cluster", relogin=False).status_code == 200, "GET cluster with the session failed")
    checks.assert_all()


def _numeric_paths(x, path=()):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _numeric_paths(v, path + (k,))
    elif isinstance(x, (int, float)) and not isinstance(x, bool):
        yield path


@pytest.mark.l3
@pytest.mark.tc("TC-BUG-033")
def test_login_attempt_config_validation(afm, checks, record_property):
    """Invalid login-attempt / lockout settings (each numeric field of the authn-policy spec set to -1 and 0)
    are rejected with 4xx and the policy is unchanged; anything wrongly accepted is put back immediately."""
    path = f"{AUTH}/authn-policy"
    orig = afm.get(path)
    fields = [p for p in _numeric_paths(orig.get("spec") or {})
              if any(w in "/".join(p).lower() for w in ("attempt", "lockout", "login", "fail"))]
    if not fields:
        pytest.skip(f"authn-policy has no numeric login-attempt fields: {list((orig.get('spec') or {}).keys())}")
    got = {}
    try:
        for f in fields:
            for bad in (-1, 0):
                body = json.loads(json.dumps(orig))
                d = body["spec"]
                for k in f[:-1]:
                    d = d[k]
                d[f[-1]] = bad
                r = afm.raw("PUT", path, {"meta": orig["meta"], "spec": body["spec"]})
                got[f"{'/'.join(f)}={bad}"] = r.status_code
                checks.check(400 <= r.status_code < 500, f"authn-policy {'/'.join(f)}={bad} -> HTTP {r.status_code}")
                if r.status_code < 300:
                    afm.raw("PUT", path, {"meta": orig["meta"], "spec": orig["spec"]})
    finally:
        now = afm.get(path)
        if now.get("spec") != orig.get("spec"):
            afm.raw("PUT", path, {"meta": now["meta"], "spec": orig["spec"]})
    record_property("responses", got)
    checks.assert_all()


# ------------------------------------------------------------------ events

@pytest.mark.l3
@pytest.mark.tc("TC-EVT-003")
def test_default_event_policies(afm, inventory, checks, record_property):
    """The default alert/event policies exist and are enabled (thresholds.afm.default_policies, default
    ['default-event-based-alerts'])."""
    want = inventory.th("afm.default_policies", ["default-event-based-alerts"])
    pols = {p["meta"]["name"]: p for p in afm.items(AP)}
    record_property("policies", {n: (p.get("spec") or {}).get("enable") for n, p in pols.items()})
    for n in want:
        if checks.check(n in pols, f"default policy {n} missing (have {sorted(pols)})"):
            checks.check((pols[n].get("spec") or {}).get("enable") is True, f"{n} not enabled")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-EVT-038")
def test_event_store_retention(afm, inventory, checks, record_property):
    """The event store keeps at most thresholds.afm.events_max (16000) events, none older than
    thresholds.afm.events_max_age_h (24 h, + 1 h slack) relative to the newest."""
    cap = int(inventory.th("afm.events_max", 16000))
    age = float(inventory.th("afm.events_max_age_h", 24)) + 1
    evs = afm.items(f"/events/v1/events?sort-by=meta.creation-time&sort-order=desc&page-size={cap + 1000}")
    times = sorted(((e.get("meta") or {}).get("creation-time") or "") for e in evs if (e.get("meta") or {}).get("creation-time"))
    record_property("events", {"count": len(evs), "oldest": times[0] if times else None, "newest": times[-1] if times else None})
    checks.check(len(evs) <= cap, f"{len(evs)} events stored, cap {cap}")
    if len(times) >= 2:
        def ts(x):
            return datetime.fromisoformat(x.replace("Z", "+00:00"))
        span = ts(times[-1]) - ts(times[0])
        checks.check(span <= timedelta(hours=age), f"events span {span} > {age} h")
    checks.assert_all()
