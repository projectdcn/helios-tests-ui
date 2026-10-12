"""TC-SWA — SONiC switch-agent cases that need only the switch CLI/DBs (no AFM API).

Disruptive ones (container / SWSS restarts) need --allow-disruptive.
"""
from __future__ import annotations

import time

import pytest

from helios.parsers import to_float
from helios.wait import wait_until

pytestmark = pytest.mark.l3


def _oper_up(switch) -> set[tuple]:
    return {(ns, r["Interface"]) for ns, rows in switch.all_interfaces_status().items()
            for r in rows if r.get("Oper", "").lower() == "up"}


def _poll(inventory) -> tuple[float, float]:
    return (float(inventory.th("switch.recover_timeout_s", 300)), float(inventory.th("switch.poll_interval_s", 10)))


@pytest.mark.tc("TC-SWA-074")
def test_interface_counters_no_errors(switch, inventory, checks):
    """Lifetime error counters on fabric ports are within ``switch.max_err_absolute`` (default 0)."""
    cols = inventory.th("switch.error_columns") or ["RX_ERR", "TX_ERR"]
    limit = inventory.th("switch.max_err_absolute")
    limit = 0 if limit is None else limit
    for ns in switch.namespaces():
        for port, row in switch.interfaces_counters(ns).items():
            for c in cols:
                v = to_float(row.get(c))
                if v is not None:
                    checks.check(v <= limit, f"{switch.name}/{ns}: {port} {c}={v:.0f} (> {limit})")
    checks.assert_all()


@pytest.mark.tc("TC-SWA-075")
def test_traffic_flowing(switch, inventory, checks, record_property):
    """RX_OK and TX_OK advance on every oper-up fabric port over ``switch.traffic_window_s``
    (default 35 s, long enough to include an LLDP interval on an idle fabric)."""
    window = float(inventory.th("switch.traffic_window_s", 35))
    min_delta = float(inventory.th("switch.min_pkts_delta", 1))
    before = {ns: switch.interfaces_counters(ns) for ns in switch.namespaces()}
    time.sleep(window)
    deltas = {}
    for ns in switch.namespaces():
        for port, row in switch.interfaces_counters(ns).items():
            if row.get("STATE", "").upper() != "U":
                continue
            for c in ("RX_OK", "TX_OK"):
                b, a = to_float(before[ns].get(port, {}).get(c)), to_float(row.get(c))
                if a is None or b is None:
                    checks.fail(f"{switch.name}/{ns}: {port} {c} unreadable")
                    continue
                deltas[f"{ns}/{port}/{c}"] = a - b
                checks.check(a - b >= min_delta, f"{switch.name}/{ns}: {port} {c} +{a - b:.0f} in {window:.0f}s")
    checks.check(deltas, f"{switch.name}: no oper-up fabric ports to check")
    record_property("pkt_deltas", deltas)
    checks.assert_all()


@pytest.mark.tc("TC-SWA-079")
def test_vlan_symmetry_across_asics(switch, inventory, checks):
    nss = [ns for ns in switch.namespaces() if ns]
    if len(nss) < 2:
        pytest.skip(f"{switch.name}: single-ASIC switch")
    vlans = {ns: set(switch.vlans(ns)) for ns in nss}
    members = {ns: switch.vlan_members(ns) for ns in nss}
    ref = nss[0]
    for ns in nss[1:]:
        checks.check(vlans[ns] == vlans[ref], f"{switch.name}: VLANs differ {ref}-only={sorted(vlans[ref] - vlans[ns])} "
                                              f"{ns}-only={sorted(vlans[ns] - vlans[ref])}")
        if inventory.th("switch.vlan_member_symmetry", True):
            for v in sorted(vlans[ref] & vlans[ns]):
                a, b = len(members[ref].get(v, [])), len(members[ns].get(v, []))
                checks.check(a == b, f"{switch.name}: {v} has {a} members on {ref} but {b} on {ns}")
    checks.assert_all()


@pytest.mark.tc("TC-SWA-138")
def test_image_management_commands(switch, checks):
    help_out = switch.run("sonic-installer --help", sudo=True)
    if checks.check(help_out.ok, f"{switch.name}: sonic-installer --help rc={help_out.rc}"):
        for sub in ("install", "list", "set-default", "set-next-boot", "remove", "cleanup"):
            checks.check(sub in help_out.stdout, f"{switch.name}: sonic-installer has no '{sub}' command")
    img = switch.images()
    checks.check(img["current"], f"{switch.name}: no current image reported")
    checks.check(img["current"] in img["available"], f"{switch.name}: current {img['current']!r} not in available list")
    checks.check(img["next"] in img["available"], f"{switch.name}: next {img['next']!r} not in available list")
    checks.assert_all()


@pytest.mark.tc("TC-SWA-129")
def test_set_next_boot_image(switch, checks):
    """Set next-boot to the running image (always a safe choice), verify, then restore the original."""
    img = switch.images()
    cur, orig_next = img["current"], img["next"]
    assert cur, f"{switch.name}: no current image"
    try:
        r = switch.run(f"sonic-installer set-next-boot {cur}", sudo=True)
        checks.check(r.ok, f"{switch.name}: set-next-boot rc={r.rc} {r.tail()!r}")
        checks.check(switch.images()["next"] == cur, f"{switch.name}: next-boot not {cur} after set-next-boot")
    finally:
        if orig_next and orig_next != cur:
            back = switch.run(f"sonic-installer set-next-boot {orig_next}", sudo=True)
            checks.check(back.ok and switch.images()["next"] == orig_next,
                         f"{switch.name}: could not restore next-boot to {orig_next}")
    checks.assert_all()


@pytest.mark.tc("TC-SWA-223")
def test_lldp_discovers_links(switch, inventory, checks):
    running = set(switch.containers())
    for ns in switch.namespaces():
        ctr = switch.per_asic("lldp", ns)
        if not checks.check(ctr in running, f"{switch.name}: container {ctr} not running"):
            continue
        st = switch.supervisor_status(ctr)
        checks.check(st.get("lldpd") == "RUNNING", f"{switch.name}: lldpd in {ctr} is {st.get('lldpd')}")
        checks.check(switch.lldp_ports(ns), f"{switch.name}/{ns}: lldpd running but no neighbours discovered")
        # GPU-facing scale-up ports don't speak LLDP, so this is opt-in (e.g. for switch-to-switch links).
        if inventory.th("switch.lldp_required_on_all_up_ports", False):
            up = {r["Interface"] for r in switch.interfaces_status(ns) if r.get("Oper", "").lower() == "up"}
            missing = sorted(up - set(switch.lldp_ports(ns)))
            checks.check(not missing, f"{switch.name}/{ns}: oper-up ports without an LLDP neighbour: {missing[:20]}")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SWA-140")
def test_restart_container_recovers(switch, inventory, checks):
    """Restart one per-ASIC container (``switch.restart_container``, default lldp) and wait for
    it and its critical processes to come back (and, for lldp, the neighbours)."""
    base = inventory.th("switch.restart_container", "lldp")
    timeout, interval = _poll(inventory)
    for ns in switch.namespaces():
        ctr = switch.per_asic(base, ns)
        lldp_before = set(switch.lldp_ports(ns)) if base == "lldp" else set()
        r = switch.restart_service(base, ns)
        if not checks.check(r.ok, f"{switch.name}: systemctl restart {switch.per_asic(base, ns, '@')} rc={r.rc} {r.tail()!r}"):
            continue

        def recovered():
            if ctr not in switch.containers():
                return False
            st = switch.supervisor_status(ctr)
            if any(st.get(p) != "RUNNING" for p in switch.critical_processes(ctr)):
                return False
            return lldp_before <= set(switch.lldp_ports(ns))

        ok, _ = wait_until(recovered, timeout, interval)
        checks.check(ok, f"{switch.name}: {ctr} not recovered within {timeout:.0f}s")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-SWA-141")
def test_restart_swss_dependents_recover(switch, inventory, checks):
    """Restart swss@N; swssN + syncdN come back with all critical processes RUNNING and every
    previously oper-up port up again. Namespaces: ``switch.swss_restart_namespaces`` (default: first ASIC)."""
    timeout, interval = _poll(inventory)
    targets = inventory.th("switch.swss_restart_namespaces") or switch.namespaces()[:1]
    for ns in targets:
        up_before = {p for p in _oper_up(switch) if p[0] == ns}
        r = switch.restart_service("swss", ns)
        if not checks.check(r.ok, f"{switch.name}: restart {switch.per_asic('swss', ns, '@')} rc={r.rc} {r.tail()!r}"):
            continue

        def recovered():
            running = set(switch.containers())
            for base in ("swss", "syncd"):
                ctr = switch.per_asic(base, ns)
                if ctr not in running:
                    return False
                st = switch.supervisor_status(ctr)
                if any(st.get(p) != "RUNNING" for p in switch.critical_processes(ctr)):
                    return False
            return up_before <= _oper_up(switch)

        ok, _ = wait_until(recovered, timeout, interval)
        checks.check(ok, f"{switch.name}: swss/syncd on {ns} not fully recovered within {timeout:.0f}s")
    checks.assert_all()
