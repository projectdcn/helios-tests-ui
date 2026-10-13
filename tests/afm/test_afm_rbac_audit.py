"""AFM RBAC (users / roles / role-bindings), audit events, telemetry metrics and techsupport.

Non-disruptive: test users, roles and policies are throw-away ``helios-*`` objects deleted on teardown.
Reconfigure permission checks target a non-existent compute, so a broken RBAC check can never reset a tray
(AFM authorizes before the object lookup, so 403 vs 404 still tells whether RBAC denied the call).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from helios.afm import AUTH, CLUSTER, MONITORING, perm, wait_for

FP = f"{CLUSTER}/failoverpolicies"


# ------------------------------------------------------------------ RBAC

def _cluster_role_rbac(afm, make_user, uniq, checks, record_property):
    t = afm.tenant
    name = uniq("u")
    user, pw = make_user(name, [perm("cluster"), {"resource-kind": "Event", "resource-namespace": "_All_",
                                                  "actions": ["read"]}])
    checks.check(user.raw("GET", f"{CLUSTER}/computenodes").status_code == 200, "cluster-role user: GET computenodes != 200")
    checks.check(user.raw("GET", "/events/v1/events?page-size=5").status_code == 200, "cluster-role user: GET events != 200")
    r = user.raw("GET", f"{AUTH}/tenant/{t}/users", relogin=False)
    checks.check(r.status_code == 403, f"cluster-role user: GET auth users -> HTTP {r.status_code}, expected 403")
    r = user.raw("GET", f"{MONITORING}/alerts", relogin=False)
    checks.check(r.status_code == 403, f"cluster-role user: GET monitoring alerts -> HTTP {r.status_code}, expected 403")
    # bad login is rejected and counted
    bad = afm.as_user(name, pw + "x").login_raw()
    checks.check(bad.status_code == 401, f"wrong password login -> HTTP {bad.status_code}, expected 401")
    u = afm.get(f"{AUTH}/tenant/{t}/users/{name}")
    st = u.get("status") or {}
    record_property("user_status", {k: st.get(k) for k in ("roles", "failed-login-attempts", "authenticators")})
    checks.check(int(st.get("failed-login-attempts") or 0) > 0, f"failed-login-attempts {st.get('failed-login-attempts')}")
    checks.check(f"{name}-role" in (st.get("roles") or []), f"user roles {st.get('roles')}")
    # role update: grant auth read -> authn-policy readable
    role = afm.get(f"{AUTH}/tenant/{t}/roles/{name}-role")
    role["spec"]["permissions"] = role["spec"]["permissions"] + [perm("auth", actions=("read",))]
    afm.put(f"{AUTH}/tenant/{t}/roles/{name}-role", {"kind": "Role", "api-version": "v1", "meta": role["meta"],
                                                      "spec": role["spec"]})
    ok = wait_for(lambda: user.raw("GET", f"{AUTH}/authn-policy", relogin=False).status_code == 200, 30, 3,
                  "authn-policy readable after role update")
    checks.check(ok, "after adding auth/read to the role, GET authn-policy still not 200")


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-043")
def test_cluster_role_rbac(afm, make_user, uniq, checks, record_property):
    """User + cluster/all + Event/read role: cluster and events 200, auth + monitoring 403; wrong password
    -> 401 and failed-login-attempts > 0; role update granting auth/read takes effect."""
    _cluster_role_rbac(afm, make_user, uniq, checks, record_property)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-045")
def test_cluster_role_bad_login(afm, make_user, uniq, checks, record_property):
    """Cluster role and bad-login behaviour (same procedure as TC-AFM-043)."""
    _cluster_role_rbac(afm, make_user, uniq, checks, record_property)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-046")
def test_auth_monitoring_workload_roles(afm, make_user, uniq, checks, record_property):
    """One user per group role: auth -> users list 200; monitoring -> alerts 200; workload -> cluster 403.
    Every role user is denied the groups it was not granted."""
    t = afm.tenant
    probes = {"auth": f"{AUTH}/tenant/{t}/users", "monitoring": f"{MONITORING}/alerts",
              "cluster": f"{CLUSTER}/computenodes"}
    seen = {}
    for group in ("auth", "monitoring", "workload"):
        u, _ = make_user(uniq(group[:4]), [perm(group)])
        for g, path in probes.items():
            code = u.raw("GET", path, relogin=False).status_code
            seen[f"{group}->{g}"] = code
            want = 200 if g == group else 403
            checks.check(code == want, f"{group}-role user GET {path} -> HTTP {code}, expected {want}")
    record_property("matrix", seen)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-RC-022")
def test_reconfigure_rejected_readonly(afm, make_user, uniq, checks, record_property):
    """Read-only ComputeNode role: GET computenodes 200, POST Reconfigure -> 403 (never 404/200: those
    would mean the call got past authorization)."""
    u, _ = make_user(uniq("ro"), [perm("cluster", "ComputeNode", ("read",))])
    checks.check(u.raw("GET", f"{CLUSTER}/computenodes").status_code == 200, "read-only user cannot GET computenodes")
    r = u.raw("POST", f"{CLUSTER}/computenodes/helios-rc022-nonexistent/Reconfigure", {}, relogin=False)
    record_property("reconfigure_response", f"HTTP {r.status_code} {r.text[:200]}")
    checks.check(r.status_code == 403, f"read-only Reconfigure -> HTTP {r.status_code}, expected 403")
    checks.assert_all()


# ------------------------------------------------------------------ audit

def _audit(afm, selector: str, page: int = 200) -> list[dict]:
    return afm.items(f"/audit/v1/events?field-selector={selector}&sort-by=meta.creation-time"
                     f"&sort-order=desc&page-size={page}")


def _audit_crud(afm, uniq, cleanup, checks, record_property):
    start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    afm.as_user(afm.username, afm.password).login()  # a fresh Login audit record
    name = uniq("audit")
    path = f"{FP}/{name}"
    cleanup(path)
    afm.post(FP, {"meta": {"name": name}, "spec": {"datafabric-reconfig": "manual", "link-fail-threshold": 15}})
    got = afm.get(path)
    afm.put(path, {"meta": got["meta"], "spec": {"datafabric-reconfig": "automatic", "link-fail-threshold": 30}})
    afm.delete(path)

    def crud_seen():
        evs = _audit(afm, f"resource.kind=FailoverPolicy,resource.name={name}")
        acts = {e.get("action") for e in evs}
        return evs if {"Create", "Update", "Delete"} <= acts else None
    evs = wait_for(crud_seen, 90, 5, "audit C/U/D") or _audit(afm, f"resource.kind=FailoverPolicy,resource.name={name}")
    pairs = [(e.get("action"), e.get("stage")) for e in evs]
    record_property("audit_pairs", pairs)
    for action in ("Create", "Update", "Delete"):
        checks.check(any(a == action for a, _ in pairs), f"no {action} audit event for {name}")
        for e in evs:
            if e.get("action") == action:
                checks.check((e.get("user") or {}).get("name") == afm.username,
                             f"{action} audit user {(e.get('user') or {}).get('name')}")
                checks.check(e.get("outcome") == "success", f"{action}/{e.get('stage')} outcome {e.get('outcome')}")
    dups = sorted({p for p in pairs if pairs.count(p) > 1})
    if dups:
        record_property("duplicate_pairs (known issue AIFM-1363)", dups)
    creates = _audit(afm, f"resource.kind=FailoverPolicy,resource.name={name},action=Create")
    checks.check(creates and all(e.get("action") == "Create" for e in creates),
                 f"action=Create selector returned {[e.get('action') for e in creates]}")
    logins = _audit(afm, f"action=Login,user.name={afm.username},meta.creation-time%3E{start}")
    checks.check(logins, f"no Login audit event for {afm.username} since {start}")
    if evs:
        uid = (evs[0].get("meta") or {}).get("uuid")
        one = afm.get(f"/audit/v1/events/{uid}")
        checks.check((one.get("meta") or {}).get("uuid") == uid, f"GET audit/{uid} returned {(one.get('meta') or {}).get('uuid')}")


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-048A")
def test_audit_events(afm, uniq, cleanup, checks, record_property):
    """Create/Update/Delete a failover policy and log in: audit records each action (user, outcome
    success), field selectors filter by kind/name/action, Login is recorded, GET by uuid works."""
    _audit_crud(afm, uniq, cleanup, checks, record_property)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-051")
def test_audit_events_crud_login(afm, uniq, cleanup, checks, record_property):
    """AFM audit events for CRUD operations and login (same procedure as TC-AFM-048A)."""
    _audit_crud(afm, uniq, cleanup, checks, record_property)
    checks.assert_all()


# ------------------------------------------------------------------ telemetry metrics

CLUSTER_FIELDS = ["AdmittedComputeNodes", "AdmittedSwitchNodes", "DecommissionedComputeNodes",
                  "DecommissionedSwitchNodes", "DisconnectedComputeNodes", "DisconnectedSwitchNodes",
                  "HealthyComputeNodes", "HealthySwitchNodes", "PendingComputeNodes", "PendingSwitchNodes",
                  "RejectedComputeNodes", "RejectedSwitchNodes", "UnhealthyComputeNodes", "UnhealthySwitchNodes"]
PORT_FIELDS = ["InOctets", "InPackets", "OutBroadcast", "OutMulticast", "OutOctets", "OutPackets", "InBroadcast",
               "InDiscards", "InErrors", "InMulticast", "OutDiscards", "OutErrors", "PauseRxPkts", "PauseTxPkts",
               "PfcRxPkts", "PfcTxPkts"]
# KI-F07-005: these SwitchPortStats fields return no series on hardware
PORT_KNOWN_NO_SERIES = ["InBroadcast", "InErrors", "InMulticast", "OutDiscards", "OutErrors", "PauseRxPkts",
                        "PauseTxPkts", "PfcRxPkts", "PfcTxPkts"]


def _metric(afm, kind: str, field: str, selectors=None, window="1h", group_by_field=None):
    q = {"kind": kind, "fields": [field], "function": "last", "start-time": f"now() - {window}", "end-time": "now()",
         "sort-order": "descending", "pagination": {"count": 1}}
    if selectors:
        q["selectors"] = [{"requirements": selectors}]
    if group_by_field:
        q["group-by-field"] = group_by_field
    res = afm.post("/telemetry/v1/metrics", {"tenant": None, "namespace": None, "queries": [q]})
    return ((res or {}).get("results") or [{}])[0].get("series") or []


def _metrics(afm, inventory, checks, record_property):
    out = {}
    n_comp, n_sw = len(afm.computes()), len(afm.switches())
    for f in CLUSTER_FIELDS:
        s = _metric(afm, "Cluster", f)
        last = s[0]["values"][0][1] if s and s[0].get("values") else None
        out[f"Cluster.{f}"] = last
        checks.check(last is not None, f"Cluster.{f}: no series")
    if out.get("Cluster.AdmittedComputeNodes") is not None:
        checks.check(out["Cluster.AdmittedComputeNodes"] == n_comp,
                     f"Cluster.AdmittedComputeNodes={out['Cluster.AdmittedComputeNodes']}, AFM lists {n_comp}")
    if out.get("Cluster.AdmittedSwitchNodes") is not None:
        checks.check(out["Cluster.AdmittedSwitchNodes"] == n_sw,
                     f"Cluster.AdmittedSwitchNodes={out['Cluster.AdmittedSwitchNodes']}, AFM lists {n_sw}")
    known = set(inventory.th("afm.metrics.known_no_series", PORT_KNOWN_NO_SERIES) or [])
    sw = afm.switches()
    reporter = sw[0]["meta"]["name"] if sw else None
    for f in PORT_FIELDS:
        sel = [{"key": "reporterID", "operator": "in", "values": [reporter]}] if reporter else None
        s = _metric(afm, "SwitchPortStats", f, sel, group_by_field="reporterID,name")
        out[f"SwitchPortStats.{f}"] = f"{len(s)} series"
        if f in known:
            continue
        checks.check(s, f"SwitchPortStats.{f} ({reporter}): no series")
    for kind in inventory.th("afm.metrics.extra_kinds", []) or []:
        k, f = kind.split(".")
        out[kind] = f"{len(_metric(afm, k, f))} series"
        checks.check(out[kind] != "0 series", f"{kind}: no series")
    record_property("metrics", out)
    record_property("known_no_series_skipped", sorted(known))


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-053")
def test_metrics(afm, inventory, checks, record_property):
    """POST /telemetry/v1/metrics: all 14 Cluster fields have data and the admitted counts match the
    REST lists; SwitchPortStats fields have series (KI-F07-005 fields excluded,
    thresholds.afm.metrics.known_no_series)."""
    _metrics(afm, inventory, checks, record_property)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-048")
def test_metrics_all_kinds(afm, inventory, checks, record_property):
    """AFM metrics telemetry (same procedure as TC-AFM-053; Node/UAL kinds via afm.metrics.extra_kinds)."""
    _metrics(afm, inventory, checks, record_property)
    checks.assert_all()


# ------------------------------------------------------------------ techsupport

@pytest.mark.l2
@pytest.mark.tc("TC-AFM-048")
def test_techsupport(afm, inventory, checks, record_property):
    """Techsupport for every switch + controller node: empty spec -> 412; request completes within
    thresholds.afm.techsupport_timeout_s (600); every node result completed with a non-empty bundle."""
    r = afm.raw("POST", f"{MONITORING}/techsupport", {"kind": "TechSupportRequest", "api-version": "v1",
                                                       "meta": {"name": f"helios-ts-empty-{int(time.time())}"},
                                                       "spec": {}})
    checks.check(r.status_code == 412, f"techsupport with empty spec -> HTTP {r.status_code}, expected 412")
    switches = [s["meta"]["name"] for s in afm.switches()]
    ctrl = [m.get("name") for m in ((afm.cluster().get("status") or {}).get("quorum-status") or {}).get("members") or []]
    names = switches + [c for c in ctrl if c]
    if inventory.th("afm.techsupport_include_computes", False):
        names += [c["meta"]["name"] for c in afm.computes()]
    ts = f"helios-ts-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    afm.post(f"{MONITORING}/techsupport", {"kind": "TechSupportRequest", "api-version": "v1", "meta": {"name": ts},
                                           "spec": {"node-selector": {"names": names}}})
    timeout = float(inventory.th("afm.techsupport_timeout_s", 600))

    def done():
        st = afm.get(f"{MONITORING}/techsupport/{ts}").get("status") or {}
        return st if st.get("status") in ("completed", "failed") else None
    st = wait_for(done, timeout, 15, "techsupport completed") or (afm.get(f"{MONITORING}/techsupport/{ts}").get("status") or {})
    checks.check(st.get("status") == "completed", f"techsupport {ts} status {st.get('status')!r} after {timeout:.0f}s")
    results = {}
    for key in ("switch-node-results", "ctrlr-node-results", "compute-node-results"):
        results.update(st.get(key) or {})
    record_property("nodes", {n: (v or {}).get("status") for n, v in results.items()})
    for n in names:
        checks.check(n in results, f"no techsupport result for {n}")
    for n, v in results.items():
        v = v or {}
        if not checks.check(v.get("status") == "completed", f"{n}: {v.get('status')}"):
            continue
        if checks.check(v.get("uri"), f"{n}: completed but no uri"):
            dl = afm.raw("GET", v["uri"], timeout=600)
            checks.check(dl.status_code == 200 and len(dl.content) > 0,
                         f"{n}: download {v['uri']} -> HTTP {dl.status_code}, {len(dl.content)} bytes")
    checks.assert_all()
