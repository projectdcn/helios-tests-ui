"""AFM configuration snapshot / restore and the cluster recovery key (DISRUPTIVE, needs --allow-disruptive).

Flow (full-rack procedure, results/extract-afm-api.md 2.8): DELETE + POST config-snapshot, POST .../save ->
status.last-snapshot.uri (downloadable), make changes, POST config-restore/restore {spec.path: <basename>},
re-login, wait for the system vPOD. A restore rewinds the whole cluster configuration to the snapshot, so
every test snapshots immediately before its own change. The recovery key is a ONE-TIME download: TC-CTL-055
only runs with thresholds.afm.allow_recovery_key: true.
"""
from __future__ import annotations

import time

import pytest

from helios.afm import AUTH, CLUSTER, perm
from helios.afm_ops import TERMINAL

FP = f"{CLUSTER}/failoverpolicies"
pytestmark = [pytest.mark.l2, pytest.mark.disruptive]


def _snapshot(ops, checks, record_property) -> str:
    ops.afm.raw("DELETE", f"{CLUSTER}/config-snapshot")
    ops.afm.post(f"{CLUSTER}/config-snapshot", {"kind": "ConfigurationSnapshot", "api-version": "v1",
                                                 "meta": {"name": "Default-Snapshot"}, "spec": {}})
    r = ops.afm.post(f"{CLUSTER}/config-snapshot/save")
    uri = ((r or {}).get("status") or {}).get("last-snapshot", {}).get("uri")
    record_property("snapshot_uri", uri)
    if not uri:
        pytest.fail(f"snapshot save returned no status.last-snapshot.uri: {str(r)[:300]}")
    dl = ops.afm.raw("GET", uri, timeout=300)
    checks.check(dl.status_code == 200 and len(dl.content) > 0, f"snapshot download {uri} -> HTTP {dl.status_code}, {len(dl.content)} bytes")
    return uri.rstrip("/").rsplit("/", 1)[-1]


def _restore(ops, checks, path: str):
    r = ops.afm.raw("POST", f"{CLUSTER}/config-restore/restore", {"meta": {"name": "SnapshotRestore"}, "spec": {"path": path}})
    checks.check(r.status_code in (200, 202), f"restore -> HTTP {r.status_code}: {r.text[:200]}")
    time.sleep(float(ops.th("restore_settle_s", 60)) * float(ops.th("time_scale", 1)))
    ops.afm.login()                                  # sessions may not survive a restore
    checks.check(ops.wait_vpod(ops.system_vpod_name(), timeout=300) in TERMINAL, "system vPOD not online/degraded after restore")


def _healthy_after(ops, checks, record_property=None):
    """Every node back healthy after the restore - except the ones quarantined in the inventory (g02-g17 17 Oct:
    TC-AFM-051 failed only on pod-1-4 = ct05, quarantined Down, which was never healthy before the snapshot)."""
    names = [x["meta"]["name"] for x in ops.afm.items(FP)]
    checks.check("system-failover-policy" in names, f"system-failover-policy missing after restore: {names}")
    left_out = [n["meta"]["name"] for n in ops.afm.computes() + ops.afm.switches() if ops.quarantined(n)]
    if left_out and record_property is not None:
        record_property("quarantine_applied", left_out)
    for c in ops.active_computes():
        checks.check(ops.wait_compute_ready(c["meta"]["name"], 300), f"{c['meta']['name']} not admitted/healthy after restore")
    for s in ops.active_switches():
        checks.check(ops.wait(lambda n=s["meta"]["name"]: ops.switch_ready(n), 300, 10), f"{s['meta']['name']} not healthy")


@pytest.mark.tc("TC-AFM-051")
def test_snapshot_collect_and_restore(ops, afm_ready, uniq, checks, record_property):
    """Snapshot, then create a failover policy and a user vPOD (one compute moved out of the system vPOD),
    restore: both are gone, the compute is back in the system vPOD, everything admitted and healthy."""
    sysv = ops.system_vpod_name()
    path = _snapshot(ops, checks, record_property)
    pol, user = uniq("fp"), uniq("vp")
    name = ops.pick_compute()
    moved = []
    try:
        ops.afm.post(FP, {"meta": {"name": pol}, "spec": {"datafabric-reconfig": "manual", "link-fail-threshold": 30}})
        moved = ops.remove_compute(sysv, name)
        ops.create_vpod(user, moved)
        ops.wait_vpod(user)
        _restore(ops, checks, path)
        checks.check(ops.afm.raw("GET", f"{FP}/{pol}").status_code == 404, f"policy {pol} survived the restore")
        checks.check(ops.afm.raw("GET", f"{CLUSTER}/vpods/{user}").status_code == 404, f"vPOD {user} survived the restore")
        checks.check(ops.wait(lambda: ops.membership(name) == {sysv}, 300, 10),
                     f"{name} not back in {sysv} after restore: {ops.membership(name)}")
        _healthy_after(ops, checks, record_property)
    finally:
        if ops.afm.raw("GET", f"{CLUSTER}/vpods/{user}").status_code == 200:
            ops.delete_vpod(user)
        if moved and ops.membership(name) != {sysv}:
            ops.add_gpus(sysv, moved)
        ops.afm.delete_quiet(f"{FP}/{pol}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-039")
def test_restore_auth_objects(ops, afm_ready, make_user, uniq, checks, record_property):
    """User, role and role-binding exist at snapshot time, are deleted, and come back with the restore; the user
    can log in again."""
    t = ops.afm.tenant
    name = uniq("su")
    u, pw = make_user(name, [perm("cluster", "_All_", ("read",))], login=False)
    path = _snapshot(ops, checks, record_property)
    for kind, obj in (("role-bindings", f"{name}-bind"), ("users", name), ("roles", f"{name}-role")):
        ops.afm.delete_quiet(f"{AUTH}/tenant/{t}/{kind}/{obj}")
    _restore(ops, checks, path)
    for kind, obj in (("users", name), ("roles", f"{name}-role"), ("role-bindings", f"{name}-bind")):
        checks.check(ops.afm.raw("GET", f"{AUTH}/tenant/{t}/{kind}/{obj}").status_code == 200, f"{kind}/{obj} not restored")
    checks.check(ops.afm.as_user(name, pw).login_raw().status_code == 200, f"restored user {name} cannot log in")
    _healthy_after(ops, checks, record_property)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-040")
def test_restore_after_compute_removed(ops, afm_ready, checks, record_property):
    """Snapshot with every compute in the system vPOD, remove one compute, restore: it is back in the system vPOD."""
    sysv = ops.system_vpod_name()
    path = _snapshot(ops, checks, record_property)
    name = ops.pick_compute()
    moved = ops.remove_compute(sysv, name)
    try:
        _restore(ops, checks, path)
        checks.check(ops.wait(lambda: ops.membership(name) == {sysv}, 300, 10),
                     f"{name} not back in {sysv} after restore: {ops.membership(name)}")
        _healthy_after(ops, checks, record_property)
    finally:
        if ops.membership(name) != {sysv}:
            ops.add_gpus(sysv, moved)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-055")
def test_recovery_key_disaster_recovery(ops, afm_ready, checks, record_property):
    """The cluster recovery key downloads once (200 with a key), a second download is refused; then a
    snapshot + restore round trip succeeds. The key is ONE-TIME: needs thresholds.afm.allow_recovery_key: true,
    and the downloaded key is not logged or stored by the test."""
    if not ops.th("allow_recovery_key", False):
        pytest.skip("one-time recovery key: set thresholds.afm.allow_recovery_key: true to download it")
    r1 = ops.afm.raw("GET", "/sysruntime/v1/cluster/recoverykeys")
    body = r1.json() if r1.status_code == 200 else {}
    has_key = any(body.get(k) for k in ("private-key", "recovery-key", "recoveryKey", "key"))
    r2 = ops.afm.raw("GET", "/sysruntime/v1/cluster/recoverykeys")
    record_property("downloads", [r1.status_code, r2.status_code])
    checks.check(r1.status_code == 200 and has_key, f"first recovery-key download -> HTTP {r1.status_code}, key present {has_key}")
    checks.check(r2.status_code != 200, "recovery key downloadable twice")
    path = _snapshot(ops, checks, record_property)
    _restore(ops, checks, path)
    _healthy_after(ops, checks, record_property)
    checks.assert_all()
