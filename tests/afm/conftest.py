"""AFM controller fixtures (REST + AggWatch). Inventory ``afm:`` section; the admin password is read from
the env var named by ``afm.auth.password_env`` (default HELIOS_AFM_PASSWORD) and never stored.

Tests that change AFM objects name them ``helios-<tc>-<epoch>`` and delete them on teardown (also when
the test fails). Disruptive tests use ``afm_ready``: rack health before (BLOCKED if unhealthy) and after.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from helios.afm import AUTH, AfmClient, new_password, rack_health
from helios.afm_ops import AfmOps


@pytest.fixture(scope="session")
def afm_cfg(inventory):
    """inventory.afm; ``afm.fake: true`` (self-test) serves it from selftest/fake_afm.py instead,
    with the faults listed in ``afm.fake_faults`` / $HELIOS_FAKE_AFM_FAULTS."""
    cfg = dict(inventory.afm or {})
    if cfg.get("fake"):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "selftest"))
        from fake_afm import FakeAfm

        faults = set(cfg.get("fake_faults") or []) | set(filter(None, os.environ.get("HELIOS_FAKE_AFM_FAULTS", "").split(",")))
        srv = FakeAfm(faults, cfg.get("fake_topology")).start()
        cfg["url"] = srv.url
        cfg["_fake_state"] = srv.state
        yield cfg
        srv.stop()
        return
    if not cfg.get("url"):
        pytest.skip("inventory has no afm.url")
    yield cfg


@pytest.fixture(scope="session")
def rack(inventory, afm_cfg):
    """The plugin's Rack; against the fake controller its switch and compute transports drive the simulated rack."""
    from helios.rack import Rack

    r = Rack(inventory)
    st = afm_cfg.get("_fake_state")
    if st is not None:
        from fake_rack import FakeSwitchTransport

        for n, (name, sw) in enumerate(r.switches.items()):
            idx = st.sim.switch_ips.index(sw.host.host) if sw.host.host in st.sim.switch_ips else n
            sw.t = FakeSwitchTransport(sw.host, st, idx, sw.t)
        from fake_rack import FakeComputeTransport

        for slot, c in enumerate(r.computes.values()):    # inventory order = compute slot in the fake
            c.t = FakeComputeTransport(c.host, st, slot, c.t)
        from fake_rack import FakeRunnerTransport

        r.runner = FakeRunnerTransport(st, r.runner, {n: k for k, n in enumerate(r.computes)},
                                       {n: (st.sim.switch_ips.index(sw.host.host) if sw.host.host in st.sim.switch_ips else k)
                                        for k, (n, sw) in enumerate(r.switches.items())})
    yield r
    r.close()


@pytest.fixture(scope="session")
def afm_anon(afm_cfg):
    """Unauthenticated client (no session cookie)."""
    return AfmClient(afm_cfg, anonymous=True)


@pytest.fixture(scope="session")
def afm(afm_cfg, inventory):
    """Admin session. Skips when the password env var is not exported in this shell. An AFM API that cannot be
    reached (no connection / reset / timeout, retried for thresholds.afm.login_wait_s, 60 s, to ride out a
    controller restart) BLOCKS every AFM test with one line, not a requests traceback (MR3 22 Oct)."""
    import requests

    from helios.afm import unreachable_reason, wait_for
    c = AfmClient(afm_cfg)
    if c.kind == "session" and c.password is None:
        pytest.skip(f"AFM password not set: export {c.auth.get('password_env', 'HELIOS_AFM_PASSWORD')}")
    err = []

    def login():
        try:
            c.login()
            return True
        except requests.RequestException as e:
            err[:] = [e]
            return False
    wait_s = float(inventory.th("afm.login_wait_s", 60) or 60) * float(inventory.th("afm.time_scale", 1) or 1)
    if not wait_for(login, wait_s, 10, f"AFM API at {c.base}"):
        pytest.fail(f"AFM pre-flight failed - the AFM API at {c.base} cannot be reached for {wait_s:.0f} s: "
                    f"{unreachable_reason(err[0]) if err else 'no answer'}. Check afm.url in the inventory and that the "
                    "controller runs there - on the switch: sonic-db-cli CONFIG_DB hgetall 'CONTROLLER|global' "
                    "(coordinates), sudo ss -ltn | grep :443, docker ps | grep afm-controller.", pytrace=False)
    return c


def _api_answers(afm, inventory, when: str) -> None:
    """Wait (thresholds.afm.api_ready_wait_s, 300 s) until the AFM API answers the cluster read: a controller restart
    by the previous test (or this one) makes it answer 503 for a while, which must not turn every following test
    into an ERROR (g02-g17 17 Oct: TC-CTL-074 stopped the leader, the next 20 AFM tests errored in their
    pre-flight within the same second)."""
    from helios.afm import transient, wait_for
    timeout = float(inventory.th("afm.api_ready_wait_s", 300) or 300) * float(inventory.th("afm.time_scale", 1) or 1)
    err = []

    def up():
        try:
            afm.cluster()
            return True
        except Exception as e:  # noqa: BLE001
            if not transient(e):
                raise
            err[:] = [e]
            return False
    if not wait_for(up, timeout, 10, "AFM API answering"):
        pytest.fail(f"AFM API not answering {when} the test within {timeout:.0f} s: {str(err[0])[:300] if err else ''}",
                    pytrace=False)


def _settled_health(afm, inventory, skip, give_wait: bool) -> list[str]:
    """rack_health, and when it reports problems, wait up to thresholds.afm.preflight_settle_s (600 s) for them to
    clear: right after a controller restart AFM reports the cluster "not healthy" for a while (g02-g17 17 Oct 13:21:
    TC-AR-003 passed, its post-check saw 'node 10.255.4.7x is not healthy' on all three controllers and the next 12
    AFM tests were BLOCKED in the same second). ``give_wait`` False: no wait (the previous gate already waited the
    full time and the rack did not recover - do not spend 10 min per test on a broken rack)."""
    from helios.afm import wait_for
    p = rack_health(afm, skip)
    if not p or not give_wait:
        return p
    k = float(inventory.th("afm.time_scale", 1) or 1)
    settle = float(inventory.th("afm.preflight_settle_s", 600) or 0) * k
    if settle <= 0:
        return p
    last = [p]

    def clear():
        last[0] = rack_health(afm, skip)
        return not last[0]
    wait_for(clear, settle, max(0.05, 30 * k), "AFM rack healthy (settling)")
    return last[0]


@pytest.fixture
def afm_ready(afm, inventory, request):
    """Rack-health gate for state-changing AFM tests: BLOCKED when AFM is not healthy before the test;
    the post-test health is checked too (failure = the test left the rack broken). Both give a just-disturbed rack
    thresholds.afm.preflight_settle_s to settle; after a gate that waited in vain, the next gates do not wait again
    until the rack has been seen healthy."""
    skip = inventory.is_quarantined_afm          # quarantined nodes do not block
    cfg = request.config
    _api_answers(afm, inventory, "before")
    before = _settled_health(afm, inventory, skip, not getattr(cfg, "_helios_afm_unsettled", False))
    cfg._helios_afm_unsettled = bool(before)
    if before:
        pytest.fail("AFM pre-flight failed - rack not healthy, not starting:\n"
                    + "\n".join(f"  - {p}" for p in before), pytrace=False)
    yield
    _api_answers(afm, inventory, "after")
    after = _settled_health(afm, inventory, skip, True)
    cfg._helios_afm_unsettled = bool(after)
    if after:
        pytest.fail("AFM rack not healthy after the test:\n" + "\n".join(f"  - {p}" for p in after), pytrace=False)


@pytest.fixture
def uniq(request):
    """uniq("policy") -> "helios-<tc>-policy-<epoch>" (lower case, AFM-safe)."""
    m = request.node.get_closest_marker("tc")
    tag = (m.args[0] if m else request.node.name).lower().replace("tc-", "").replace("_", "-")

    def make(what: str = "") -> str:
        return "-".join(x for x in ("helios", tag, what, str(int(time.time() * 1000) % 10**9)) if x)
    return make


@pytest.fixture
def cleanup(afm):
    """cleanup(path) registers an object to DELETE after the test (reverse order)."""
    paths: list[str] = []
    yield paths.append
    for p in reversed(paths):
        afm.delete_quiet(p)


@pytest.fixture
def make_user(afm, cleanup):
    """make_user(name, permissions) -> (client logged in as that user, password). Creates Role + User +
    RoleBinding in the AFM tenant; all three are deleted after the test."""
    t = afm.tenant

    def make(name: str, permissions: list[dict] | None, login: bool = True):
        pw = new_password()
        if permissions is not None:
            afm.post(f"{AUTH}/tenant/{t}/roles", {"kind": "Role", "api-version": "v1",
                                                  "meta": {"name": f"{name}-role", "tenant": t},
                                                  "spec": {"permissions": permissions}})
            cleanup(f"{AUTH}/tenant/{t}/roles/{name}-role")
        afm.post(f"{AUTH}/tenant/{t}/users", {"kind": "User", "api-version": "v1", "meta": {"name": name, "tenant": t},
                                              "spec": {"fullname": name, "password": pw, "type": "local",
                                                       "email": f"{name}@example.invalid"}})
        cleanup(f"{AUTH}/tenant/{t}/users/{name}")
        if permissions is not None:
            afm.post(f"{AUTH}/tenant/{t}/role-bindings",
                     {"kind": "RoleBinding", "api-version": "v1", "meta": {"name": f"{name}-bind", "tenant": t},
                      "spec": {"users": [name], "role": f"{name}-role"}})
            cleanup(f"{AUTH}/tenant/{t}/role-bindings/{name}-bind")
        u = afm.as_user(name, pw)
        if login:
            u.login()
        return u, pw
    return make


@pytest.fixture
def ops(afm, rack, inventory):
    """AFM lifecycle operations (REST + switch SSH) for the disruptive suites."""
    return AfmOps(afm, rack, inventory)


@pytest.fixture
def links(ops):
    """Shut fabric ports through it; every port it shut is brought back after the test."""
    from afm_helpers import Links

    lk = Links(ops)
    yield lk
    lk.restore_all()
