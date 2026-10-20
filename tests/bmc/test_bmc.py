"""TC-BMC-001..006 - BMC of every switch tray and compute tray over Redfish, READ-ONLY (added 20 Oct 2026, not in
AMD's BKC-26.12 workbook; testplan/added.yaml). Nothing is powered, reset or written: GETs only.

Each node's BMC is reached at its own address (inventory ``bmc:``) as its ``bmc_user:`` with its own password
($HELIOS_BMC_PASSWORD_<NODE>) or the rack default ($HELIOS_BMC_PASSWORD) - set in the UI (Inventory tab, Setup node
table). A node without address / user / password is NOT CHECKED (listed, not failed); with none checkable the case
skips. Every BMC is read once per session (helios.bmc.snapshot) and the cases judge that snapshot.

Thresholds (inventory ``thresholds.bmc``): timeout_s (15), verify_tls (false), log_window_h (24),
log_entries_max (200), sensors_max (300).
"""
from __future__ import annotations

import pytest

from helios import bmc as B
from helios.rack import fan_out


@pytest.fixture(scope="session")
def bmc_snaps(inventory):
    """{"snaps": {node: snapshot}, "errors": {node: why}, "skipped": {node: why}, "roles": {node: role},
    "where": {node: address}} - every BMC of the inventory read once."""
    hosts = [*inventory.switches, *inventory.computes]
    todo, skipped = [], {}
    for h in hosts:
        addr, user, pw, why = B.credentials(h)
        if why:
            skipped[h.name] = why
        else:
            todo.append((h, addr, user, pw))
    th = lambda k, d: inventory.th(f"bmc.{k}", d)      # noqa: E731

    def read(t):
        h, addr, user, pw = t
        rf = B.Redfish(addr, user, pw, timeout=float(th("timeout_s", 15)), verify=bool(th("verify_tls", False)))
        try:
            return B.snapshot(rf, int(th("log_entries_max", 200)), int(th("sensors_max", 300)))
        finally:
            rf.close()
    snaps, errors = {}, {}
    for t, snap, exc in fan_out(read, todo):
        if exc is not None:
            errors[t[0].name] = str(exc)
        else:
            snaps[t[0].name] = snap
    return {"snaps": snaps, "errors": errors, "skipped": skipped, "roles": {h.name: h.role for h in hosts},
            "where": {h.name: str(h.extra.get("bmc") or "") for h in hosts}}


def _need(b, record_property, read_ok=True):
    """Record what was / was not checked; skip when nothing is checkable."""
    record_property("bmc_not_checked", b["skipped"])
    if not b["snaps"] and not b["errors"]:
        pytest.skip("no BMC checkable: " + "; ".join(f"{n}: {w}" for n, w in b["skipped"].items()) if b["skipped"]
                    else "no switch / compute in the inventory")
    if read_ok and not b["snaps"]:
        pytest.fail("no BMC could be read: " + "; ".join(f"{n} ({b['where'][n]}): {e}" for n, e in b["errors"].items()),
                    pytrace=False)


@pytest.mark.l1
@pytest.mark.tc("TC-BMC-001")
def test_bmc_reachable_and_login_works(bmc_snaps, checks, record_property):
    """Every node's BMC answers Redfish at its own address and accepts its BMC user + password."""
    b = bmc_snaps
    _need(b, record_property, read_ok=False)
    for n, e in sorted(b["errors"].items()):
        checks.fail(f"{n}: BMC {b['where'][n]}: {e}")
    record_property("bmc", {n: s["root"] for n, s in b["snaps"].items()})
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BMC-002")
def test_bmc_power_state_on(bmc_snaps, checks, record_property):
    """Every system the BMCs report is powered on (switch trays included - HC-002 covers compute trays only)."""
    b = bmc_snaps
    _need(b, record_property)
    for n, s in sorted(b["snaps"].items()):
        checks.check(s["systems"], f"{n}: BMC {b['where'][n]} reports no system")
        for f in B.bad_power(s):
            checks.fail(f"{n}: {f}")
    record_property("power", {n: [(x["id"], x["power"]) for x in s["systems"]] for n, s in b["snaps"].items()})
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BMC-003")
def test_bmc_and_system_health(bmc_snaps, checks, record_property):
    """No BMC (manager) or system reports health Critical; Warning is recorded."""
    b = bmc_snaps
    _need(b, record_property)
    warnings = {}
    for n, s in sorted(b["snaps"].items()):
        crit, warn = B.bad_health(s)
        for f in crit:
            checks.fail(f"{n}: {f}")
        if warn:
            warnings[n] = warn
    record_property("health_warnings", warnings)
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BMC-004")
def test_bmc_sensors_within_limits(bmc_snaps, checks, record_property):
    """No sensor (temperatures, fans, voltages, power supplies) is Critical or beyond its critical limit; readings
    beyond a non-critical limit are recorded."""
    b = bmc_snaps
    _need(b, record_property)
    warnings, counts = {}, {}
    for n, s in sorted(b["snaps"].items()):
        counts[n] = len(s["sensors"])
        if not s["sensors"]:
            warnings[n] = ["the BMC exposes no sensors over Redfish"]
            continue
        crit, warn = B.bad_sensors(s)
        for f in crit:
            checks.fail(f"{n}: sensor {f}")
        if warn:
            warnings[n] = warn[:30]
    record_property("sensors_read", counts)
    record_property("sensor_warnings", warnings)
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BMC-005")
def test_bmc_event_log_no_recent_critical(bmc_snaps, inventory, checks, record_property):
    """No Critical entry in the BMC event logs (SEL / event log) within the last ``log_window_h`` hours (24);
    Warning entries are counted."""
    b = bmc_snaps
    _need(b, record_property)
    hours = float(inventory.th("bmc.log_window_h", 24))
    summary = {}
    for n, s in sorted(b["snaps"].items()):
        crit, warn = B.recent_critical(s, hours)
        summary[n] = {"entries_read": len(s["log_entries"]), "critical": len(crit), "warning": warn}
        for e in crit[:10]:
            checks.fail(f"{n}: BMC log {e['log']} {e['created'] or '(no time)'}: Critical: {e['message']}")
        if len(crit) > 10:
            checks.fail(f"{n}: ... and {len(crit) - 10} more Critical BMC log entries")
    record_property("bmc_logs", summary)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-BMC-006")
def test_bmc_firmware_consistent(bmc_snaps, checks, record_property):
    """The BMC firmware version is the same on every compute tray, and on every switch tray."""
    b = bmc_snaps
    _need(b, record_property)
    groups = {}
    for role in ("compute", "switch"):
        snaps = {n: s for n, s in b["snaps"].items() if b["roles"].get(n) == role}
        if len(snaps) < 2:
            continue
        g = B.firmware_groups(snaps)
        groups[role] = g
        if len(g) > 1:
            major = max(g, key=lambda v: len(g[v]))
            for v, nodes in sorted(g.items()):
                if v != major:
                    checks.fail(f"{', '.join(nodes)}: BMC firmware {v} (most {role} trays: {major})")
    if not groups:
        pytest.skip("needs the BMCs of at least 2 compute trays or 2 switch trays")
    record_property("bmc_firmware", groups)
    checks.assert_all()
