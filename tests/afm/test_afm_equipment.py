"""AFM cases that need lab equipment or images we may not have: LDAP/RADIUS servers, a syslog server, upgrade
images, out-of-band management-network control. Each test SKIPS until its inventory entries exist:

afm.ldap / afm.radius:   authenticator  - JSON merged into the authn-policy spec (from the AFM docs / AMD)
                         username, password_env - a directory user for the login check
afm.syslog:              create: {path, body} - AFM object that exports audit events ({marker} in body allowed)
                         check:  runner command that exits 0 when the marker reached the server ({marker})
commands.afm.rollout:    {path, body} with {version} - rollout request; thresholds.afm.versions: {n, n_minus_1}
commands.switch.*:       firmware_upgrade, local_image_install, failing_upgrade, onie_upgrade, sonic_upgrade,
                         cluster_move ({name} {image} placeholders); thresholds.switch.target_image / local_image
commands.mgmtnet:        down / up - runner commands that cut / restore a node's management network ({name} {host})
All are DISRUPTIVE. Placeholders in templates use str.format.
"""
from __future__ import annotations

import json
import secrets
import time

import pytest

from helios.afm import AUTH, CLUSTER, conditions_ok, rack_health

pytestmark = [pytest.mark.disruptive]


def _cfg(ops, key):
    return ops.afm.cfg.get(key)


def _merge(a: dict, b: dict) -> dict:
    out = json.loads(json.dumps(a))
    for k, v in b.items():
        out[k] = _merge(out.get(k) or {}, v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _authn(ops):
    return ops.afm.get(f"{AUTH}/authn-policy")


def _put_authn(ops, meta, spec):
    return ops.afm.raw("PUT", f"{AUTH}/authn-policy", {"meta": meta, "spec": spec})


def _dir_login(ops, cfg, checks, kind):
    import os

    pw = os.environ.get(cfg.get("password_env") or "")
    if not pw:
        pytest.skip(f"afm.{kind}.password_env not exported")
    u = ops.afm.as_user(cfg["username"], pw)
    r = u.login_raw()
    checks.check(r.status_code == 200, f"{kind} user {cfg['username']} login -> HTTP {r.status_code}")
    if r.status_code == 200:
        auth = ((r.json() or {}).get("status") or {}).get("authenticators") or []
        checks.check(kind in [a.lower() for a in auth], f"login not served by {kind}: authenticators {auth}")


def _directory_auth(ops, checks, record_property, kind):
    cfg = _cfg(ops, kind)
    if not cfg or not cfg.get("authenticator") or not cfg.get("username"):
        pytest.skip(f"afm.{kind} not configured (authenticator, username, password_env)")
    pol = _authn(ops)
    orig = pol.get("spec") or {}
    try:
        r = _put_authn(ops, pol["meta"], _merge(orig, cfg["authenticator"]))
        checks.check(r.status_code == 200, f"enabling {kind} in authn-policy -> HTTP {r.status_code}: {r.text[:200]}")
        _dir_login(ops, cfg, checks, kind)
        bad = ops.afm.as_user(cfg["username"], "helios-wrong-" + secrets.token_hex(4)).login_raw()
        checks.check(bad.status_code == 401, f"{kind} login with a wrong password -> HTTP {bad.status_code}")
    finally:
        _put_authn(ops, _authn(ops)["meta"], orig)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-044")
def test_ldap_authentication(ops, afm_ready, checks, record_property):
    """LDAP authenticator (afm.ldap) added to the authn-policy: the directory user logs in through LDAP, a wrong
    password is refused; the original policy is restored. TC-CTL-119 covers RADIUS."""
    _directory_auth(ops, checks, record_property, "ldap")


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-119")
def test_radius_authentication(ops, afm_ready, checks, record_property):
    """As TC-AFM-044 with the RADIUS authenticator (afm.radius)."""
    _directory_auth(ops, checks, record_property, "radius")


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-042")
def test_snapshot_restore_with_ldap_radius(ops, afm_ready, checks, record_property):
    """LDAP and RADIUS configured, snapshot taken, both removed, snapshot restored: the authn-policy holds both
    authenticators again (and the directory user can log in)."""
    ldap, radius = _cfg(ops, "ldap"), _cfg(ops, "radius")
    if not (ldap and radius and ldap.get("authenticator") and radius.get("authenticator")):
        pytest.skip("afm.ldap and afm.radius not both configured")
    pol = _authn(ops)
    orig = pol.get("spec") or {}
    try:
        with_both = _merge(_merge(orig, ldap["authenticator"]), radius["authenticator"])
        _put_authn(ops, pol["meta"], with_both)
        ops.afm.raw("DELETE", f"{CLUSTER}/config-snapshot")
        ops.afm.post(f"{CLUSTER}/config-snapshot", {"meta": {"name": "Default-Snapshot"}, "spec": {}})
        uri = ((ops.afm.post(f"{CLUSTER}/config-snapshot/save") or {}).get("status") or {}).get("last-snapshot", {}).get("uri", "")
        _put_authn(ops, _authn(ops)["meta"], orig)
        ops.afm.raw("POST", f"{CLUSTER}/config-restore/restore", {"meta": {"name": "SnapshotRestore"},
                                                                 "spec": {"path": uri.rsplit("/", 1)[-1]}})
        time.sleep(float(ops.th("restore_settle_s", 60)) * float(ops.th("time_scale", 1)))
        ops.afm.login()
        restored = _authn(ops).get("spec") or {}
        for kind, cfg in (("ldap", ldap), ("radius", radius)):
            want = _merge({}, cfg["authenticator"])
            checks.check(_merge(restored, want) == restored, f"{kind} authenticator not restored")
        if ldap.get("username"):
            _dir_login(ops, ldap, checks, "ldap")
    finally:
        _put_authn(ops, _authn(ops)["meta"], orig)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-101")
def test_audit_export_to_syslog(ops, afm_ready, uniq, cleanup, checks, record_property):
    """Audit export to an external syslog server: the export object (afm.syslog.create) is created, an audited
    action carrying a unique marker happens, and afm.syslog.check (runner command) finds the marker."""
    cfg = _cfg(ops, "syslog")
    if not cfg or not cfg.get("create") or not cfg.get("check"):
        pytest.skip("afm.syslog not configured (create: {path, body}, check: runner command)")
    marker = uniq("syslog")
    body = json.loads(json.dumps(cfg["create"]["body"]).replace("{marker}", marker))
    r = ops.afm.raw("POST", cfg["create"]["path"], body)
    checks.check(r.status_code in (200, 201), f"creating the syslog export -> HTTP {r.status_code}: {r.text[:200]}")
    name = (body.get("meta") or {}).get("name")
    if name:
        cleanup(f"{cfg['create']['path'].rstrip('/')}/{name}")
    fp = f"{CLUSTER}/failoverpolicies/{marker}"
    cleanup(fp)
    ops.afm.post(f"{CLUSTER}/failoverpolicies", {"meta": {"name": marker}, "spec": {"datafabric-reconfig": "manual",
                                                                                     "link-fail-threshold": 10}})
    ok = ops.wait(lambda: ops.rack.runner.run(cfg["check"].format(marker=marker), timeout=120).ok, 300, 15)
    checks.check(ok, f"audit record with {marker} not found on the syslog server")
    checks.assert_all()


# ------------------------------------------------------------------ upgrades

def _healthy(ops, checks, what, timeout=None):
    timeout = timeout or float(ops.th("upgrade_settle_s", 1800))
    ops.wait(lambda: _try(lambda: conditions_ok(ops.afm.cluster())), timeout, 30)
    _try(ops.afm.login)
    checks.check(ops.wait(lambda: _try(lambda: not ops.rack_health()), timeout, 30),
                 f"rack not healthy after {what}: {_try(lambda: ops.rack_health())}")


def _try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - controller restarting
        return None


def _members(ops) -> dict:
    return {m.get("name"): m.get("status")
            for m in ((ops.afm.cluster().get("status") or {}).get("quorum-status") or {}).get("members") or []}


def _version(ops):
    return (ops.afm.version() or {}).get("build-version")


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-054")
def test_rollout_downgrade_upgrade(ops, afm_ready, inventory, checks, record_property):
    """Combined controller + agent rollout to N-1 and back to N (commands.afm.rollout {path, body with {version}};
    thresholds.afm.versions {n, n_minus_1}): each rollout reports the requested build and the rack is healthy."""
    ro, vers = inventory.cmd("afm.rollout"), ops.th("versions", None)
    if not ro or not vers:
        pytest.skip("commands.afm.rollout and thresholds.afm.versions {n, n_minus_1} not set")
    seen = {}
    for label in ("n_minus_1", "n"):
        v = vers[label]
        body = json.loads(json.dumps(ro["body"]).replace("{version}", v))
        r = ops.afm.raw("POST", ro["path"], body)
        checks.check(r.status_code in (200, 201, 202), f"rollout to {v} -> HTTP {r.status_code}: {r.text[:200]}")
        ok = ops.wait(lambda: _try(lambda: _version(ops) == v), float(ops.th("rollout_timeout_s", 3600)), 30)
        _healthy(ops, checks, f"the rollout to {v}")
        seen[label] = _version(ops)
        checks.check(ok, f"build {seen[label]} after rolling out {v}")
    record_property("versions", seen)
    checks.assert_all()


def _switch_template(inventory, key):
    t = inventory.cmd(f"switch.{key}")
    if not t:
        pytest.skip(f"commands.switch.{key} not set")
    return t


def _current_image(sw) -> str:
    return (sw.images() or {}).get("current", "")


def _upgrade_switch(ops, inventory, checks, record_property, key, controller, expect_image=True):
    t = _switch_template(inventory, key)
    target = ops.inv.th("switch.target_image")
    sw_obj = ops.pick_switch(controller=controller) if len(ops.afm.switches()) > 1 else ops.afm.switches()[0]
    name, sw = sw_obj["meta"]["name"], ops.ssh_switch(sw_obj)
    before = _current_image(sw)
    r = sw.run(t.format(name=sw.name, image=target or ""), sudo=True, timeout=3600)
    checks.check(r.ok, f"{key} on {sw.name} -> rc {r.rc}: {r.tail()!r}")
    from helios.wait import wait_for_ssh

    wait_for_ssh(sw, float(ops.th("switch_reboot_s", 1800)))
    _healthy(ops, checks, f"{key} on {name}")
    after = _current_image(sw)
    record_property("image", {"before": before, "after": after, "target": target})
    if expect_image and target:
        checks.check(target in after, f"{sw.name} runs {after!r}, expected {target!r}")
    return before, after


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-UPGRADE-004")
def test_switch_sonic_upgrade(ops, afm_ready, inventory, checks, record_property):
    """SONiC upgrade of a (non-controller) switch with commands.switch.sonic_upgrade: it reboots into
    thresholds.switch.target_image and is re-admitted healthy."""
    _upgrade_switch(ops, inventory, checks, record_property, "sonic_upgrade", controller=False)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-HW-022")
def test_onie_clean_upgrade(ops, afm_ready, inventory, checks, record_property):
    """HW-019: clean SONiC install through ONIE (commands.switch.onie_upgrade): the switch comes back on the target
    image and AFM re-admits it."""
    _upgrade_switch(ops, inventory, checks, record_property, "onie_upgrade", controller=False)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-078")
def test_firmware_upgrade_controller_switch(ops, afm_ready, inventory, checks, record_property):
    """Firmware upgrade on a switch that runs a controller (commands.switch.firmware_upgrade): the controller
    member rejoins, the cluster and rack are healthy afterwards."""
    if len(ops.quorum_ips()) < 2:
        pytest.skip("needs 2 controllers (upgrading the only controller is an outage)")
    _upgrade_switch(ops, inventory, checks, record_property, "firmware_upgrade", controller=True, expect_image=False)
    started = ops.wait(lambda: _try(lambda: all(v == "started" for v in _members(ops).values())),
                       float(ops.th("upgrade_settle_s", 1800)), 30)
    checks.check(started, f"controller members not all started after the upgrade: {_try(lambda: _members(ops))}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-SWA-128")
def test_install_image_from_local_file(ops, afm_ready, inventory, checks, record_property):
    """sonic-installer installs an image from a local file (commands.switch.local_image_install, {image} =
    thresholds.switch.local_image): the image is listed; the current image is not changed (no reboot), and the
    installed image is removed again unless it is the current one."""
    t = _switch_template(inventory, "local_image_install")
    image = ops.inv.th("switch.local_image")
    if not image:
        pytest.skip("thresholds.switch.local_image not set")
    sw = list(ops.rack.switches.values())[-1]
    before = sw.images() or {}
    r = sw.run(t.format(name=sw.name, image=image), sudo=True, timeout=1800)
    checks.check(r.ok, f"install -> rc {r.rc}: {r.tail()!r}")
    after = sw.images() or {}
    new = [i for i in after.get("available", []) if i not in before.get("available", [])]
    record_property("installed", new)
    checks.check(new, "no new image listed after the install")
    checks.check(after.get("current") == before.get("current"), "current image changed by the install")
    for i in new:
        if i != after.get("current"):
            sw.run(f"sonic-installer remove -y {i}", sudo=True, timeout=600)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-SWA-164")
def test_rollback_on_failed_upgrade(ops, afm_ready, inventory, checks, record_property):
    """A failing upgrade (commands.switch.failing_upgrade, e.g. a corrupt image) rolls back automatically: the switch
    keeps (or returns to) its previous image and stays admitted and healthy."""
    t = _switch_template(inventory, "failing_upgrade")
    sw_obj = ops.pick_switch(controller=False) if len(ops.afm.switches()) > 1 else ops.afm.switches()[0]
    name, sw = sw_obj["meta"]["name"], ops.ssh_switch(sw_obj)
    before = _current_image(sw)
    r = sw.run(t.format(name=sw.name), sudo=True, timeout=3600)
    record_property("upgrade_rc", r.rc)
    from helios.wait import wait_for_ssh

    wait_for_ssh(sw, float(ops.th("switch_reboot_s", 1800)))
    _healthy(ops, checks, "the failed upgrade")
    checks.check(_current_image(sw) == before, f"image {_current_image(sw)!r} after a failed upgrade, expected {before!r}")
    checks.check(ops.switch_ready(name), f"{name} not admitted + healthy")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-127")
def test_cluster_move(ops, afm_ready, inventory, checks, record_property):
    """Cluster move to a new SONiC controller host without stopping afm-controller (commands.afm.cluster_move, run on
    the runner): afterwards the quorum includes thresholds.afm.cluster_move_target and the rack is healthy."""
    t, target = inventory.cmd("afm.cluster_move"), ops.th("cluster_move_target", None)
    if not t or not target:
        pytest.skip("commands.afm.cluster_move and thresholds.afm.cluster_move_target not set")
    r = ops.rack.runner.run(t.format(target=target), timeout=3600)
    checks.check(r.ok, f"cluster move -> rc {r.rc}: {r.tail()!r}")
    _healthy(ops, checks, "the cluster move")
    checks.check(target in ops.quorum_ips(), f"{target} not in the quorum {ops.quorum_ips()}")
    checks.assert_all()


# ------------------------------------------------------------------ management network

def _mgmt(inventory):
    down, up = inventory.cmd("mgmtnet.down"), inventory.cmd("mgmtnet.up")
    if not (down and up):
        pytest.skip("commands.mgmtnet.down / up not set (out-of-band control of a node's management network)")
    return down, up


def _cut(ops, tmpl, host):
    return ops.rack.runner.run(tmpl.format(name=host.name, host=host.host), timeout=300)


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-008")
def test_compute_mgmt_flap(ops, afm_ready, inventory, checks, record_property):
    """Management network of a compute cut and restored: AFM marks it unhealthy, then healthy again."""
    down, up = _mgmt(inventory)
    name = ops.pick_compute()
    tray = ops.tray(name)
    try:
        _cut(ops, down, tray.host)
        checks.check(ops.wait(lambda: not conditions_ok(ops.compute(name)), float(ops.th("agent_down_detect_s", 600)), 20),
                     f"{name} still healthy without management network")
    finally:
        _cut(ops, up, tray.host)
    checks.check(ops.wait_compute_ready(name, 600), f"{name} not healthy after the network returned")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-E2E-093")
def test_event_on_compute_mgmt_loss(ops, afm_ready, inventory, checks, record_property):
    """Losing a compute's management connectivity raises an event naming the compute; it recovers afterwards."""
    down, up = _mgmt(inventory)
    name = ops.pick_compute()
    tray = ops.tray(name)
    since = ops.controller_now()
    try:
        _cut(ops, down, tray.host)
        ev = ops.wait(lambda: [e for e in ops.events(None, since) if name in json.dumps(e)], float(ops.th("agent_down_detect_s", 600)), 20)
        record_property("events", [e.get("type") for e in ev or []])
        checks.check(ev, f"no event naming {name} after its management network was cut")
    finally:
        _cut(ops, up, tray.host)
    checks.check(ops.wait_compute_ready(name, 600), f"{name} not healthy after the network returned")
    checks.assert_all()


def _controller_states(ops):
    return {ip: ops.controller_state(sw) for ip, sw in ops.controllers().items()}


@pytest.mark.l3
@pytest.mark.tc("TC-BUG-003")
def test_controller_stable_mgmt_flap(ops, afm_ready, inventory, checks, record_property):
    """Management interface of a non-leader controller switch flapped: no controller container restarts elsewhere,
    and the cluster is healthy with all members started afterwards."""
    down, up = _mgmt(inventory)
    if len(ops.quorum_ips()) < 2:
        pytest.skip("needs 2 controllers")
    ip = next(m for m in ops.quorum_ips() if m != ops.leader_ip())
    sw = ops.ssh_by_ip(ip)
    before = {k: v for k, v in _controller_states(ops).items() if k != ip}
    try:
        _cut(ops, down, sw.host)
        time.sleep(60 * float(ops.th("time_scale", 1)))
    finally:
        _cut(ops, up, sw.host)
    _healthy(ops, checks, "the management flap", 900)
    after = _controller_states(ops)
    for k, v in before.items():
        checks.check(after.get(k) == v, f"controller on {k} restarted: {v} -> {after.get(k)}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-CTL-050")
def test_leader_moves_on_network_disruption(ops, afm_ready, inventory, checks, record_property):
    """The leader controller's switch loses its management network: a new leader is elected; after the network
    returns, the old leader rejoins as a started member."""
    down, up = _mgmt(inventory)
    if len(ops.quorum_ips()) < 2:
        pytest.skip("needs 2 controllers")
    leader = ops.leader_ip()
    sw = ops.ssh_by_ip(leader)
    try:
        _cut(ops, down, sw.host)
        moved = ops.wait(lambda: _try(lambda: ops.leader_ip() not in (None, "", leader)), 300, 10)
        record_property("leader", {"before": leader, "after": _try(ops.leader_ip)})
        checks.check(moved, f"leader still {leader} with its network down")
    finally:
        _cut(ops, up, sw.host)
    _healthy(ops, checks, "the network disruption", 900)
    members = _members(ops)
    checks.check(members.get(leader) == "started", f"old leader {leader} not rejoined: {members}")
    checks.assert_all()

