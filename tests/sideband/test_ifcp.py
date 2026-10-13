"""IFCP (scale-up control protocol) state per GPU port, from `afmctl show port --json` status.ifcp.*

TC-BU-003  every GPU has an active IFCP session (IFCP up on every LINK_UP port, no errors)
TC-SBM-062 link-level control messaging is bidirectional (local ll_up + peer enabled, neither in error)
TC-E2E-062 topology matches cabling: the IFCP-discovered switch peer of every GPU port is the switch port
           whose CONFIG_DB description ('To ComputeTray Slot<s>/GPU<g>[<acc>]/Port<p>') names it.
           peer_port_id is switch-global (looked up across ASIC namespaces); peer_device_id identifies the
           switch: inventory `slot_id` per switch, default = position in the inventory starting at 1.
"""
from __future__ import annotations

import pytest

from helios.afmctl import Afmctl, parse_port_description
from helios.rack import fan_out

pytestmark = pytest.mark.l2


def _yes(v) -> bool:
    return str(v).lower() in ("yes", "true", "1")


def _collect(computes, checks):
    """{compute: {bdf: (acc_id, [raw port dicts])}}"""
    def one(c):
        a = Afmctl(c, timeout=90)
        return {d.bdf: (d.accel_id, a.ports_raw(d.bdf)) for d in a.devices()}

    out = {}
    for c, res, exc in fan_out(one, computes):
        if exc:
            checks.fail(f"{c.name}: afmctl failed: {exc}")
        else:
            out[c.name] = res
    return out


def _up(p) -> bool:
    return (p.get("spec") or {}).get("admin_state") == "enabled" and "UP" in str((p.get("status") or {}).get("link_status", "")).upper()


@pytest.mark.tc("TC-BU-003")
def test_every_gpu_has_active_ifcp_session(computes, checks, record_property):
    summary = {}
    for cname, gpus in _collect(computes, checks).items():
        for bdf, (acc, ports) in gpus.items():
            up = [p for p in ports if _up(p)]
            ifcp_up = [p for p in up if _yes((p["status"].get("ifcp") or {}).get("link_up"))]
            summary[f"{cname}/{bdf}"] = f"{len(ifcp_up)}/{len(up)} LINK_UP ports with IFCP up"
            checks.check(ifcp_up, f"{cname}/{bdf} (acc {acc}): no IFCP session on any port")
            bad = [p["id"] for p in up if p not in ifcp_up]
            checks.check(not bad, f"{cname}/{bdf} (acc {acc}): LINK_UP ports without IFCP: {bad[:12]}")
            err = [p["id"] for p in up if _yes((p["status"].get("ifcp") or {}).get("in_error"))]
            checks.check(not err, f"{cname}/{bdf} (acc {acc}): IFCP in_error on ports {err[:12]}")
    record_property("ifcp", summary)
    checks.assert_all()


@pytest.mark.tc("TC-SBM-062")
def test_link_level_control_bidirectional(computes, checks):
    for cname, gpus in _collect(computes, checks).items():
        for bdf, (acc, ports) in gpus.items():
            for p in (p for p in ports if _up(p)):
                i = p["status"].get("ifcp") or {}
                problems = [k for k, ok in (("ll_up", _yes(i.get("ll_up"))), ("peer_enabled", _yes(i.get("peer_enabled"))),
                                            ("peer_in_error=no", not _yes(i.get("peer_in_error")))) if not ok]
                checks.check(not problems, f"{cname}/{bdf} (acc {acc}) port {p['id']}: link-level not bidirectional: {problems}")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-062")
def test_topology_matches_cabling(rack, computes, checks, record_property):
    switches = list(rack.switches.values())
    by_id = {int(sw.host.extra.get("slot_id", i + 1)): sw for i, sw in enumerate(switches)}
    desc = {}  # (switch name, EthernetN) -> description, merged across ASIC namespaces
    for sw in switches:
        for ns in sw.namespaces():
            for port, d in sw.port_descriptions(ns).items():
                desc[(sw.name, port)] = d
    checked = 0
    for cname, gpus in _collect(computes, checks).items():
        for bdf, (acc, ports) in gpus.items():
            for p in (p for p in ports if _up(p)):
                i = p["status"].get("ifcp") or {}
                dev, peer = i.get("peer_device_id"), i.get("peer_port_id")
                tag = f"{cname}/{bdf} (acc {acc}) port {p['id']}"
                sw = by_id.get(int(dev)) if str(dev).isdigit() else None
                if sw is None and len(switches) == 1:
                    sw = switches[0]
                if not checks.check(sw, f"{tag}: IFCP peer device {dev} is not a known switch {sorted(by_id)}"):
                    continue
                d = desc.get((sw.name, f"Ethernet{peer}"))
                cab = parse_port_description(d or "")
                if checks.check(cab, f"{tag}: peer {sw.name} Ethernet{peer} has no cabling description"):
                    checked += 1
                    checks.check((str(cab[2]), cab[3]) == (str(acc), int(p["id"])),
                                 f"{tag}: cabled to {sw.name} Ethernet{peer}, which the topology registers as "
                                 f"acc {cab[2]} port {cab[3]}")
    record_property("ports_checked", checked)
    checks.check(checked, "no GPU ports checked")
    checks.assert_all()
