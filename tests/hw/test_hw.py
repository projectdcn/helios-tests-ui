"""TC-HW — hardware checks that need only switch/compute CLI access."""
from __future__ import annotations

import pytest

from helios import datapath
from helios.parsers import parse_speed_gbps
from helios.rack import fan_out

pytestmark = pytest.mark.l3


@pytest.mark.tc("TC-HW-002")
def test_interface_speed_matches_config(switch, checks):
    """Oper speed (STATE_DB PORT_TABLE) equals configured speed (CONFIG_DB PORT) on oper-up ports.
    Falls back to the CLI speed column if this image doesn't publish oper speed."""
    for ns in switch.namespaces():
        status = {r["Interface"]: r for r in switch.interfaces_status(ns)}
        speeds = switch.port_speeds(ns)
        checks.check(speeds, f"{switch.name}/{ns}: no PORT entries in CONFIG_DB")
        for port, (cfg, oper) in speeds.items():
            row = status.get(port)
            if not row or row.get("Oper", "").lower() != "up":
                continue
            got = parse_speed_gbps(oper) if oper else parse_speed_gbps(row.get("Speed", ""))
            want = parse_speed_gbps(cfg)
            checks.check(want is not None and got == want,
                         f"{switch.name}/{ns}: {port} oper {got}G != configured {want}G")
    checks.assert_all()


@pytest.mark.tc("TC-HW-008")
def test_transferbench_smoke_a2a_in_domain(datapath_ready, computes, inventory, checks, record_property):
    size = inventory.th("datapath.transferbench.smoke_size", "64M")
    record_property("bw_gbps", datapath.transferbench_in_domain(computes, inventory, checks, size=size))
    checks.assert_all()


@pytest.mark.tc("TC-HW-013")
def test_dest_accelerator_mac(rack, computes, checks, record_property):
    """Destination accelerator MAC tables are complete and agree with the switch.

    Compute side (`afmctl show port accelerator-addr --json`), per GPU and port:
      * the destination set equals the vPOD accelerators minus the GPU itself
      * every MAC's last octet equals the port id (one switching plane per port)
      * an (accelerator, port) maps to the same MAC on every observer; no MAC is shared
    Switch side: each of those MACs is a Static FDB entry on the switch port whose
    description ('To ComputeTray Slot<s>/GPU<g>[<acc>]/Port<p>') names that accelerator and port.
    """
    from helios.afmctl import Afmctl, parse_port_description

    observed: dict[tuple[str, int], set[str]] = {}
    for c, res, exc in fan_out(lambda c: (Afmctl(c).vpod_accels(), Afmctl(c).accel_addr()), computes):
        if exc:
            checks.fail(f"{c.name}: afmctl failed: {exc}")
            continue
        vpod, addr = res
        for bdf, (own, members) in vpod.items():
            want = members - {own}
            for port, table in sorted(addr.get(bdf, {}).items()):
                got = set(table)
                checks.check(got == want, f"{c.name}/{bdf} port {port}: destinations {sorted(got - want)} extra, "
                                          f"{sorted(want - got)} missing")
                for acc, mac in table.items():
                    checks.check(mac.split(":")[-1] == f"{port:02x}",
                                 f"{c.name}/{bdf} port {port}: acc {acc} MAC {mac} not on plane {port:02x}")
                    observed.setdefault((acc, port), set()).add(mac)
    for (acc, port), macs in sorted(observed.items()):
        checks.check(len(macs) == 1, f"acc {acc} port {port}: observers disagree on MAC {sorted(macs)}")
    owner: dict[str, tuple[str, int]] = {}
    for key, macs in observed.items():
        for mac in macs:
            if mac in owner and owner[mac] != key:
                checks.fail(f"MAC {mac} used by acc/port {owner[mac]} and {key}")
            owner[mac] = key

    fdb, desc = {}, {}
    for sw in rack.switches.values():
        for ns in sw.namespaces():
            desc.update({(sw.name, ns, p): d for p, d in sw.port_descriptions(ns).items()})
            for row in sw.fdb(ns):
                fdb[row.get("MacAddress", "").lower()] = (sw.name, ns, row.get("Port"), row.get("Type"), row.get("Vlan"))
    unmatched = 0
    for mac, (acc, port) in sorted(owner.items(), key=lambda kv: kv[1]):
        hit = fdb.get(mac)
        if not checks.check(hit, f"acc {acc} port {port}: MAC {mac} missing from switch FDB"):
            unmatched += 1
            continue
        sw, ns, sport, typ, vlan = hit
        checks.check(str(typ).lower() == "static", f"{sw}/{ns} {sport}: MAC {mac} is {typ}, expected Static")
        cab = parse_port_description(desc.get((sw, ns, sport), ""))
        if checks.check(cab, f"{sw}/{ns} {sport}: no cabling description for MAC {mac}"):
            checks.check(str(cab[2]) == acc and cab[3] == port,
                         f"{sw}/{ns} {sport} (Vlan{vlan}): FDB has MAC {mac} for acc {acc} port {port}, "
                         f"but the port is cabled to acc {cab[2]} port {cab[3]}")
    record_property("mac_entries", len(owner))
    checks.check(owner, "no accelerator addresses found on any compute")
    checks.assert_all()
