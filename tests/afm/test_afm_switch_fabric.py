"""TC-SWH-048 and TC-SWH-050 - switch fabric vs AFM (added to the test plan by helios-tests on 14 Oct 2026; see
testplan/added.yaml). Read-only: AFM API (ComputeNodes) + switch CONFIG_DB / interface state over SSH.

The switch side is identified by the CONFIG_DB port descriptions 'To ComputeTray Slot<s>/GPU<g>[<acc>]/Port<p>'.
"""
from __future__ import annotations

from collections import Counter

import pytest

from helios.afmctl import parse_port_description

pytestmark = [pytest.mark.l2]


def _afm_computes(ops) -> dict[int, dict]:
    """{slot: {"name", "admitted", "accs": {gpu index: acc id}}} from the AFM ComputeNodes."""
    out = {}
    for c in ops.afm.computes():
        slot = (c.get("spec") or {}).get("slot-id")
        if slot is None:
            continue
        accs = {g.get("index"): g.get("acc-id") for g in ops.gpu_info(c).values()}
        out[int(slot)] = {"name": c["meta"]["name"], "accs": accs,
                          "admitted": (c.get("status") or {}).get("admission-phase") == "admitted"}
    return out


def _switch_fabric(sw) -> dict:
    """{ns: {"desc": {port: (slot, gpu, acc, gpu_port)}, "oper": {port: up?}, "members": {port, ...}}}"""
    out = {}
    for ns in sw.namespaces():
        desc = {p: cab for p, d in sw.port_descriptions(ns).items() if (cab := parse_port_description(d))}
        oper = {r["Interface"]: r.get("Oper", "").lower() == "up" for r in sw.interfaces_status(ns)}
        members = {p for ports in sw.vlan_members(ns).values() for p in ports}
        out[ns] = {"desc": desc, "oper": oper, "members": members}
    return out


@pytest.mark.tc("TC-SWH-048")
def test_afm_and_switch_topology_agree(ops, checks, record_property):
    """For every switch: each AFM-admitted compute's GPUs are cabled (port descriptions) with at least one up port
    per GPU, the accelerator id in the description equals AFM's acc-id for that slot/GPU, and no port cabled to a
    compute AFM does not admit is in service (VLAN member)."""
    if ops.rack is None or not ops.rack.switches:
        pytest.skip("no switch SSH in this inventory")
    comps = _afm_computes(ops)
    admitted = {s for s, c in comps.items() if c["admitted"]}
    record_property("afm_computes", {"admitted": sorted(admitted), "not_admitted": sorted(set(comps) - admitted)})
    for sw in ops.rack.switches.values():
        fab = _switch_fabric(sw)
        cabled_up: dict[tuple[int, int], int] = Counter()
        for ns, f in fab.items():
            for port, (slot, gpu, acc, _gp) in f["desc"].items():
                where = f"{sw.name}/{ns}: {port} (Slot{slot}/GPU{gpu}[{acc}])"
                c = comps.get(slot)
                if c and gpu in c["accs"]:
                    checks.check(c["accs"][gpu] == acc, f"{where}: AFM says acc-id {c['accs'][gpu]} for slot {slot} GPU{gpu}")
                if slot in admitted and f["oper"].get(port):
                    cabled_up[(slot, gpu)] += 1
                if slot not in admitted:
                    checks.check(port not in f["members"], f"{where}: in service (VLAN member) but AFM does not admit slot {slot}")
        for slot in sorted(admitted):
            for gpu in sorted(comps[slot]["accs"]):
                checks.check(cabled_up[(slot, gpu)] > 0, f"{sw.name}: admitted {comps[slot]['name']} GPU{gpu} has no up cabled port")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-050")
def test_static_macs_match_fabric(ops, checks, record_property):
    """Every static FDB entry is on a port cabled to an AFM-admitted compute and that port is a member of the entry's
    VLAN; every VLAN-member port cabled to an admitted compute has at least one static entry in that VLAN."""
    if ops.rack is None or not ops.rack.switches:
        pytest.skip("no switch SSH in this inventory")
    comps = _afm_computes(ops)
    admitted = {s for s, c in comps.items() if c["admitted"]}
    summary = {}
    for sw in ops.rack.switches.values():
        stale = Counter()
        for ns in sw.namespaces():
            desc = {p: cab for p, d in sw.port_descriptions(ns).items() if (cab := parse_port_description(d))}
            vm = sw.vlan_members(ns)
            member_of = {(v, p) for v, ports in vm.items() for p in ports}
            fdb = sw.fdb_static(ns)
            with_entry = {(v, p) for v, _mac, p in fdb}
            for vlan, mac, port in fdb:
                cab = desc.get(port)
                why = ("port not cabled to a GPU" if cab is None else
                       f"slot {cab[0]} not admitted in AFM" if cab[0] not in admitted else
                       f"port is not a member of {vlan}" if (vlan, port) not in member_of else None)
                if why:
                    kind = why.replace(f"slot {cab[0]} ", "slot ") if cab else why
                    kind = kind.replace(f" of {vlan}", "")
                    stale[kind] += 1
                    if sum(stale.values()) <= 10:
                        checks.fail(f"{sw.name}/{ns}: static MAC {mac} in {vlan} on {port}: {why}")
            for vlan, port in sorted(member_of):
                cab = desc.get(port)
                if cab and cab[0] in admitted:
                    checks.check((vlan, port) in with_entry, f"{sw.name}/{ns}: {port} is in {vlan} but has no static MAC entry")
        if sum(stale.values()) > 10:
            checks.fail(f"{sw.name}: {sum(stale.values())} stale static MAC entries in all ({dict(stale)})")
        summary[sw.name] = dict(stale)
    record_property("stale_static_macs", summary)
    checks.assert_all()
