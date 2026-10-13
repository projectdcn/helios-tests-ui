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
    """The plugin's Rack; against the fake controller its switch transports drive the simulated rack."""
    from helios.rack import Rack

    r = Rack(inventory)
    st = afm_cfg.get("_fake_state")
    if st is not None:
        from fake_rack import FakeSwitchTransport

        for n, (name, sw) in enumerate(r.switches.items()):
            idx = st.sim.switch_ips.index(sw.host.host) if sw.host.host in st.sim.switch_ips else n
            sw.t = FakeSwitchTransport(sw.host, st, idx, sw.t)
    yield r
    r.close()


@pytest.fixture(scope="session")
def afm_anon(afm_cfg):
    """Unauthenticated client (no session cookie)."""
    return AfmClient(afm_cfg, anonymous=True)


@pytest.fixture(scope="session")
def afm(afm_cfg):
    """Admin session. Skips when the password env var is not exported in this shell."""
    c = AfmClient(afm_cfg)
    if c.kind == "session" and c.password is None:
        pytest.skip(f"AFM password not set: export {c.auth.get('password_env', 'HELIOS_AFM_PASSWORD')}")
    c.login()
    return c


@pytest.fixture
def afm_ready(afm):
    """Rack-health gate for state-changing AFM tests: BLOCKED when AFM is not healthy before the test;
    the post-test health is checked too (failure = the test left the rack broken)."""
    before = rack_health(afm)
    if before:
        pytest.fail("AFM pre-flight failed - rack not healthy, not starting:\n"
                    + "\n".join(f"  - {p}" for p in before), pytrace=False)
    yield
    after = rack_health(afm)
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
