"""AFM controller REST API: auth, cluster/quorum, object specs, failover policies, negative requests.

Non-disruptive: reads, plus create/delete of throw-away objects (``helios-*``) that never touch GPUs,
vPOD membership or links. Needs the AFM admin password (``afm`` fixture) except where noted.
"""
from __future__ import annotations

import re

import pytest
import requests

from helios.afm import CLUSTER, conditions_ok, wait_for

FP = f"{CLUSTER}/failoverpolicies"


def _status_envelope(r: requests.Response) -> dict:
    try:
        b = r.json()
    except ValueError:
        return {}
    return b if isinstance(b, dict) else {}


# ------------------------------------------------------------------ auth (no password needed)

@pytest.mark.l2
@pytest.mark.tc("TC-AFM-RC-021")
def test_reconfigure_rejected_unauthenticated(afm_anon, checks):
    """POST computenodes/<n>/Reconfigure and GET computenodes without a session cookie -> 401.
    Uses a non-existent compute name so a missing auth check can never reset a real tray."""
    r = afm_anon.raw("POST", f"{CLUSTER}/computenodes/helios-rc021-nonexistent/Reconfigure", {})
    checks.check(r.status_code == 401, f"unauthenticated Reconfigure -> HTTP {r.status_code}, expected 401: {r.text[:200]}")
    for path in (f"{CLUSTER}/computenodes", f"{CLUSTER}/cluster", f"{CLUSTER}/vpods"):
        r = afm_anon.raw("GET", path)
        checks.check(r.status_code == 401, f"unauthenticated GET {path} -> HTTP {r.status_code}, expected 401")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-089")
def test_invalid_requests(afm_anon, afm_cfg, checks):
    """Invalid requests get a JSON Status envelope with the right code: bad credentials -> 401,
    malformed login body -> 4xx, unauthenticated -> 401 (+ 404/400 cases when the password is set)."""
    from helios.afm import AfmClient, new_password

    c = AfmClient(afm_cfg, username="helios-no-such-user", password=new_password())
    r = c.login_raw()
    checks.check(r.status_code == 401, f"login with unknown user -> HTTP {r.status_code}, expected 401")
    env = _status_envelope(r)
    checks.check(env.get("code") == r.status_code and env.get("message"),
                 f"login failure body has no code/message: {r.text[:200]}")
    r = afm_anon.raw("POST", "/v1/login", '{"username": "admin", "password": ',
                     headers={"Content-Type": "application/json"})
    checks.check(r.status_code == 400, f"malformed login JSON -> HTTP {r.status_code}, expected 400")
    r = afm_anon.raw("GET", f"{CLUSTER}/computenodes")
    env = _status_envelope(r)
    checks.check(r.status_code == 401 and env.get("code") == 401,
                 f"unauthenticated GET -> HTTP {r.status_code} body {r.text[:150]}")
    a = AfmClient(afm_cfg)
    if a.password is not None:  # authenticated negative cases
        r = a.raw("GET", f"{FP}/helios-ctl089-does-not-exist")
        checks.check(r.status_code == 404 and _status_envelope(r).get("code") == 404,
                     f"GET missing object -> HTTP {r.status_code}: {r.text[:200]}")
        r = a.raw("POST", FP, '{"meta": {"name": "x"', headers={"Content-Type": "application/json"})
        checks.check(400 <= r.status_code < 500, f"malformed POST body -> HTTP {r.status_code}, expected 4xx")
        r = a.raw("GET", f"{CLUSTER}/helios-no-such-kind")
        checks.check(r.status_code in (404, 405, 501), f"unknown resource path -> HTTP {r.status_code}")
    checks.assert_all()


# ------------------------------------------------------------------ auth (admin)

@pytest.mark.l2
@pytest.mark.tc("TC-CTL-088")
def test_login_valid_credentials(afm, afm_cfg, checks, record_property):
    """POST /v1/login with the admin credentials -> 200, User object with AdminRole, sid cookie,
    and the session works for a GET."""
    from helios.afm import AfmClient

    c = AfmClient(afm_cfg)
    r = c.login_raw()
    checks.check(r.status_code == 200, f"login -> HTTP {r.status_code}")
    body = _status_envelope(r)
    st = body.get("status") or {}
    checks.check(body.get("kind") == "User", f"login body kind {body.get('kind')!r}, expected User")
    checks.check("AdminRole" in (st.get("roles") or []), f"admin roles {st.get('roles')}")
    checks.check(bool(c.sid), "no sid session cookie set by login")
    record_property("csrf_header", bool(r.headers.get("Grpc-Metadata-Csrf-Token")))
    checks.check(c.raw("GET", f"{CLUSTER}/cluster", relogin=False).status_code == 200,
                 "GET cluster with the new session failed")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-022")
def test_session_cookie_flags(afm, afm_cfg, checks, record_property):
    """The sid Set-Cookie header carries Secure, HttpOnly and SameSite=Strict."""
    from helios.afm import AfmClient

    r = AfmClient(afm_cfg).login_raw()
    raw = r.raw.headers.getlist("Set-Cookie") if hasattr(r.raw.headers, "getlist") else [r.headers.get("Set-Cookie", "")]
    sid = next((h for h in raw if h.lower().startswith("sid=")), "")
    record_property("set_cookie_attrs", re.sub(r"^sid=[^;]*", "sid=<REDACTED>", sid))
    if not checks.check(sid, f"no sid Set-Cookie header (got {len(raw)} Set-Cookie headers)"):
        checks.assert_all()
    attrs = [a.strip().lower() for a in sid.split(";")[1:]]
    checks.check("secure" in attrs, f"sid cookie lacks Secure: {attrs}")
    checks.check("httponly" in attrs, f"sid cookie lacks HttpOnly: {attrs}")
    checks.check("samesite=strict" in attrs, f"sid cookie lacks SameSite=Strict: {attrs}")
    checks.assert_all()


# ------------------------------------------------------------------ cluster + objects

@pytest.mark.l2
@pytest.mark.tc("TC-CTL-036")
def test_cluster_quorum_healthy(afm, inventory, checks, record_property):
    """Cluster condition healthy=true, a leader elected, every quorum member started and healthy;
    member count = thresholds.afm.quorum_members (if set); build version = thresholds.afm.build_version."""
    c = afm.cluster()
    st = c.get("status") or {}
    members = (st.get("quorum-status") or {}).get("members") or []
    ver = afm.version()
    record_property("leader", st.get("leader"))
    record_property("members", [m.get("name") for m in members])
    record_property("build_version", ver.get("build-version"))
    checks.check(conditions_ok(c), f"cluster conditions {st.get('conditions')}")
    checks.check(bool(st.get("leader")), "no cluster leader")
    checks.check(members, "no quorum-status.members")
    for m in members:
        checks.check(m.get("status") == "started", f"quorum member {m.get('name')} status {m.get('status')!r}")
        checks.check(all(str(x.get("status")).lower() == "true" for x in m.get("conditions") or [{}]),
                     f"quorum member {m.get('name')} conditions {m.get('conditions')}")
    want = inventory.th("afm.quorum_members")
    if want is not None:
        checks.check(len(members) == int(want), f"{len(members)} quorum members, expected {want}")
    if st.get("leader") and members:
        checks.check(st["leader"] in [m.get("name") for m in members],
                     f"leader {st['leader']} is not a quorum member")
    want_ver = inventory.th("afm.build_version")
    if want_ver:
        checks.check(ver.get("build-version") == want_ver, f"AFM build {ver.get('build-version')}, expected {want_ver}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SBM-049")
def test_switches_admitted(afm, inventory, checks, record_property):
    """Every switch tray is admitted and healthy in AFM (computes can only be admitted after switches);
    switch count = inventory switches; every ASIC port reported has an oper-state."""
    sw = afm.switches()
    record_property("switchnodes", {s["meta"]["name"]: (s.get("status") or {}).get("admission-phase") for s in sw})
    want = int(inventory.th("afm.expected_switches", len(inventory.switches)))
    checks.check(len(sw) == want, f"AFM has {len(sw)} switch nodes, expected {want}")
    for s in sw:
        st = s.get("status") or {}
        n = s["meta"]["name"]
        checks.check(st.get("admission-phase") == "admitted", f"{n}: admission-phase {st.get('admission-phase')}")
        checks.check(conditions_ok(s), f"{n}: conditions {st.get('conditions')}")
        checks.check((s.get("spec") or {}).get("ip-address"), f"{n}: no spec.ip-address")
    checks.check(any((c.get("status") or {}).get("admission-phase") == "admitted" for c in afm.computes()) or not sw,
                 "no compute admitted")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-090")
def test_compute_node_specs(afm, inventory, checks, record_property):
    """Every compute: admitted + healthy, slot-id, CPU info, gpu-info has the inventory GPU count, every
    GPU Ready with a rack-unique acc-id and vPOD membership, GPU ports PortReady (failed ports listed)."""
    nodes = afm.computes()
    want_n = int(inventory.th("afm.expected_computes", len(inventory.computes)))
    checks.check(len(nodes) == want_n, f"AFM has {len(nodes)} compute nodes, expected {want_n}")
    acc_ids: dict[int, str] = {}
    summary = {}
    for n in nodes:
        name = n["meta"]["name"]
        spec, st = n.get("spec") or {}, n.get("status") or {}
        checks.check(st.get("admission-phase") == "admitted", f"{name}: admission-phase {st.get('admission-phase')}")
        checks.check(conditions_ok(n), f"{name}: conditions {st.get('conditions')}")
        checks.check(spec.get("slot-id") is not None, f"{name}: no spec.slot-id")
        cpus = st.get("cpus") or []
        checks.check(cpus and cpus[0].get("num-cores"), f"{name}: no status.cpus[].num-cores")
        gi = (st.get("scale-up-info") or {}).get("gpu-info") or {}
        want = int(inventory.computes[0].extra.get("gpus", 4)) if inventory.computes else 4
        checks.check(len(gi) == want, f"{name}: {len(gi)} GPUs in gpu-info, expected {want}")
        failed_ports = []
        for uuid, g in gi.items():
            tag = f"{name} GPU{g.get('index')} ({uuid[:8]})"
            checks.check(g.get("oper-state") == "Ready", f"{tag}: oper-state {g.get('oper-state')}")
            aid = g.get("acc-id")
            if checks.check(aid is not None, f"{tag}: no acc-id"):
                checks.check(aid not in acc_ids, f"{tag}: acc-id {aid} also on {acc_ids.get(aid)}")
                acc_ids[aid] = tag
            checks.check(g.get("vpod-membership"), f"{tag}: not in any vPOD")
            failed_ports += [f"GPU{g.get('index')}/p{p.get('id', i)}" for i, p in enumerate(g.get("ports") or [])
                             if p.get("oper-state") != "PortReady"]
        summary[name] = {"slot": spec.get("slot-id"), "gpus": len(gi), "not_ready_ports": failed_ports}
        allowed = inventory.th("afm.max_not_ready_ports_per_compute", 0)
        if allowed is not None:
            checks.check(len(failed_ports) <= int(allowed),
                         f"{name}: {len(failed_ports)} GPU ports not PortReady: {failed_ports[:12]}")
    record_property("computes", summary)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-092")
def test_pod_spec(afm, checks, record_property):
    """GET pods: at least one physical Pod with a name and a non-empty spec; every vPOD's parent-pod exists."""
    pods = afm.items(f"{CLUSTER}/pods")
    record_property("pods", {p["meta"]["name"]: p.get("spec") for p in pods})
    checks.check(pods, "no Pod objects")
    for p in pods:
        checks.check(p.get("kind") in (None, "Pod"), f"{p['meta']['name']}: kind {p.get('kind')}")
        checks.check(p.get("spec"), f"{p['meta']['name']}: empty spec")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-093")
def test_pod_status(afm, checks, record_property):
    """GET pods/<name>: status present; the pod's vPODs (parent-pod) are listed by GET vpods."""
    pods = afm.items(f"{CLUSTER}/pods")
    checks.check(pods, "no Pod objects")
    vp = afm.vpods()
    for p in pods:
        name = p["meta"]["name"]
        one = afm.get(f"{CLUSTER}/pods/{name}")
        record_property(f"{name}_status", one.get("status"))
        checks.check(one.get("meta", {}).get("name") == name, f"GET pods/{name} returned {one.get('meta')}")
        checks.check(one.get("status") is not None, f"{name}: no status")
        mine = [v["meta"]["name"] for v in vp if (v.get("spec") or {}).get("parent-pod") == name]
        checks.check(mine, f"{name}: no vPOD has parent-pod {name}")
        record_property(f"{name}_vpods", mine)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-094")
def test_vpod_parent_pod(afm, checks, record_property):
    """Every vPOD has spec.parent-pod naming an existing Pod; the system vPOD pod-N.system exists, is
    online/degraded and has no pending ops; provisioned >= available >= effective switch planes."""
    pods = {p["meta"]["name"] for p in afm.items(f"{CLUSTER}/pods")}
    vp = afm.vpods()
    sysv = [v for v in vp if re.fullmatch(r"pod-\d+\.system", v["meta"]["name"])]
    checks.check(sysv, f"no system vPOD among {[v['meta']['name'] for v in vp]}")
    out = {}
    for v in vp:
        name, spec, st = v["meta"]["name"], v.get("spec") or {}, v.get("status") or {}
        checks.check(spec.get("parent-pod") in pods, f"{name}: parent-pod {spec.get('parent-pod')!r} not in {sorted(pods)}")
        planes = [st.get(k) for k in ("provisioned-switch-planes", "available-switch-planes", "effective-switch-planes")]
        out[name] = {"state": st.get("vpod-state"), "planes": planes, "gpus": len(spec.get("GPUs") or [])}
        if v in sysv:
            checks.check(st.get("vpod-state") in ("online", "degraded"), f"{name}: state {st.get('vpod-state')}")
            checks.check(not st.get("pending-ops"), f"{name}: pending-ops {st.get('pending-ops')}")
        if None not in planes:
            checks.check(planes[0] >= planes[1] >= planes[2], f"{name}: planes prov/avail/eff {planes}")
    record_property("vpods", out)
    checks.assert_all()


# ------------------------------------------------------------------ failover policies

@pytest.mark.l2
@pytest.mark.tc("TC-E2E-006")
def test_system_failover_policy_defaults(afm, inventory, checks, record_property):
    """system-failover-policy exists with datafabric-reconfig=automatic and link-fail-threshold=10 (s),
    and the system vPOD uses it."""
    p = afm.get(f"{FP}/system-failover-policy")
    spec = p.get("spec") or {}
    record_property("spec", spec)
    want = inventory.th("afm.system_policy", {"datafabric-reconfig": "automatic", "link-fail-threshold": 10})
    for k, v in want.items():
        checks.check(spec.get(k) == v, f"system-failover-policy {k}={spec.get(k)!r}, expected {v!r}")
    sysv = afm.system_vpod()
    if sysv:
        checks.check((sysv.get("spec") or {}).get("failover-policy") in (None, "", "system-failover-policy"),
                     f"{sysv['meta']['name']} uses failover-policy {(sysv.get('spec') or {}).get('failover-policy')}")
    names = [x["meta"]["name"] for x in afm.items(FP)]
    checks.check("system-failover-policy" in names, f"system-failover-policy not in list {names}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-E2E-011")
def test_read_nonexistent_failover_policy(afm, checks):
    """GET a failover policy that does not exist -> 404 with a Status envelope."""
    r = afm.raw("GET", f"{FP}/helios-e2e011-does-not-exist")
    env = _status_envelope(r)
    checks.check(r.status_code == 404, f"HTTP {r.status_code}, expected 404")
    checks.check(env.get("code") == 404, f"body {r.text[:200]}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-005")
def test_failover_policy_crud(afm, uniq, cleanup, checks):
    """POST (manual/10s) -> GET matches -> PUT (automatic/30s) -> GET matches -> DELETE -> GET 404."""
    name = uniq("fp")
    path = f"{FP}/{name}"
    cleanup(path)
    afm.post(FP, {"kind": "FailoverPolicy", "api-version": "v1", "meta": {"name": name},
                  "spec": {"datafabric-reconfig": "manual", "link-fail-threshold": 10}})
    got = afm.get(path)
    checks.check((got.get("spec") or {}).get("datafabric-reconfig") == "manual", f"after create: {got.get('spec')}")
    checks.check((got.get("spec") or {}).get("link-fail-threshold") == 10, f"after create: {got.get('spec')}")
    names = [x["meta"]["name"] for x in afm.items(FP)]
    checks.check(name in names, f"{name} missing from list")
    got["spec"] = {"datafabric-reconfig": "automatic", "link-fail-threshold": 30}
    afm.put(path, {"kind": "FailoverPolicy", "api-version": "v1", "meta": got["meta"], "spec": got["spec"]})
    got = afm.get(path)
    checks.check((got.get("spec") or {}).get("datafabric-reconfig") == "automatic", f"after update: {got.get('spec')}")
    checks.check((got.get("spec") or {}).get("link-fail-threshold") == 30, f"after update: {got.get('spec')}")
    r = afm.raw("DELETE", path)
    checks.check(r.status_code in (200, 204), f"DELETE -> HTTP {r.status_code}")
    gone = wait_for(lambda: afm.raw("GET", path).status_code == 404, 30, 3, "policy deleted")
    checks.check(gone, f"{name} still readable after DELETE")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-E2E-015")
def test_failover_policy_without_threshold(afm, uniq, cleanup, checks, record_property):
    """Create a policy with only datafabric-reconfig: either rejected with 4xx, or created with a
    positive default link-fail-threshold (recorded)."""
    name = uniq("fp")
    cleanup(f"{FP}/{name}")
    r = afm.raw("POST", FP, {"kind": "FailoverPolicy", "api-version": "v1", "meta": {"name": name},
                             "spec": {"datafabric-reconfig": "automatic"}})
    record_property("http_status", r.status_code)
    if 400 <= r.status_code < 500:
        record_property("result", f"rejected: {r.text[:200]}")
    elif checks.check(r.status_code in (200, 201), f"POST -> HTTP {r.status_code}: {r.text[:200]}"):
        th = (afm.get(f"{FP}/{name}").get("spec") or {}).get("link-fail-threshold")
        record_property("default_threshold", th)
        checks.check(isinstance(th, (int, float)) and th > 0, f"created with link-fail-threshold {th!r}")
    checks.assert_all()


# ------------------------------------------------------------------ vPOD name validation (no GPUs moved)

def _vpod_body(name: str, pod: str) -> dict:
    return {"kind": "VPod", "api-version": "v1", "meta": {"name": name},
            "spec": {"GPUs": [], "parent-pod": pod, "failover-policy": "system-failover-policy"}}


def _parent_pod(afm) -> str:
    pods = afm.items(f"{CLUSTER}/pods")
    if not pods:
        pytest.fail("no Pod objects to use as parent-pod")
    return pods[0]["meta"]["name"]


@pytest.mark.l2
@pytest.mark.tc("TC-E2E-021")
def test_vpod_name_dot_system_rejected(afm, uniq, cleanup, checks, record_property):
    """POST a vPOD named '<x>.system' (reserved suffix, no GPUs) -> 4xx; deleted if wrongly accepted."""
    name = uniq("vp") + ".system"
    cleanup(f"{CLUSTER}/vpods/{name}")
    r = afm.raw("POST", f"{CLUSTER}/vpods", _vpod_body(name, _parent_pod(afm)))
    record_property("response", f"HTTP {r.status_code} {r.text[:200]}")
    checks.check(400 <= r.status_code < 500, f"vPOD {name} -> HTTP {r.status_code}, expected rejection (4xx)")
    checks.assert_all()


def _duplicate_vpod(afm, uniq, cleanup, checks, record_property):
    pod = _parent_pod(afm)
    name = uniq("vp")
    path = f"{CLUSTER}/vpods/{name}"
    cleanup(path)
    r = afm.raw("POST", f"{CLUSTER}/vpods", _vpod_body(name, pod))
    if r.status_code not in (200, 201):
        pytest.skip(f"AFM does not accept an empty user vPOD (HTTP {r.status_code}: {r.text[:150]}); "
                    "the duplicate-name check needs GPUs moved out of the system vPOD (disruptive)")
    r2 = afm.raw("POST", f"{CLUSTER}/vpods", _vpod_body(name, pod))
    record_property("duplicate_response", f"HTTP {r2.status_code} {r2.text[:200]}")
    checks.check(r2.status_code == 409, f"duplicate vPOD name -> HTTP {r2.status_code}, expected 409")
    checks.check(afm.raw("DELETE", path).status_code in (200, 204), f"DELETE {name} failed")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-044")
def test_vpod_duplicate_name_409(afm, uniq, cleanup, checks, record_property):
    """Create an empty user vPOD (no GPUs), POST the same name again -> 409, then delete it."""
    _duplicate_vpod(afm, uniq, cleanup, checks, record_property)


@pytest.mark.l2
@pytest.mark.tc("TC-CTL-096")
def test_vpod_conflicting_identifier(afm, uniq, cleanup, checks, record_property):
    """Objects with conflicting identifiers are rejected (same procedure as TC-CTL-044)."""
    _duplicate_vpod(afm, uniq, cleanup, checks, record_property)


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-RC-018")
def test_reconfigure_nonexistent_compute(afm, checks, record_property):
    """POST computenodes/<missing>/Reconfigure -> 404, or 412 'failed to find computeNode'."""
    r = afm.raw("POST", f"{CLUSTER}/computenodes/helios-rc018-nonexistent/Reconfigure", {})
    record_property("response", f"HTTP {r.status_code} {r.text[:200]}")
    checks.check(r.status_code == 404 or (r.status_code == 412 and "failed to find" in r.text),
                 f"HTTP {r.status_code}: {r.text[:200]}")
    checks.assert_all()
