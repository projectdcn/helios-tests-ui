"""Datapath traffic together with AFM operations (DISRUPTIVE unless noted, behind the datapath pre-flight gate):
L2 ping / TransferBench / RCCL for the node-agent cases, port statistics after traffic, traffic across a link
flap, and the RAS reset cases that use an AFM hard reset ("reconfigure compute tray from AFM") or vPOD moves.

Traffic uses the same inventory templates as tests/l1 and tests/ras (commands.transferbench / rccl / l2ping,
commands.ras.*). On a rack whose fabric fails the pre-flight (e.g. MR3 plane 9) these report BLOCKED.
"""
from __future__ import annotations

import json
import threading

import pytest

from helios import datapath
from helios.afm_ops import CN, TERMINAL
from helios.compute import render
from helios.rack import fan_out

pytestmark = [pytest.mark.usefixtures("datapath_ready")]


def _afm_names(ops, trays) -> dict:
    """{tray name: AFM compute name}."""
    out = {}
    for c in ops.afm.computes():
        try:
            out[ops.tray(c["meta"]["name"]).name] = c["meta"]["name"]
        except RuntimeError:
            pass
    return {t.name: out[t.name] for t in trays if t.name in out}


def _traffic(scope, trays, inventory, checks, record_property, tag=""):
    colls = inventory.cmd("ras.collectives", ["all_reduce"])
    if scope == "in":
        record_property(f"tb{tag}", datapath.transferbench_in_domain(trays, inventory, checks))
        record_property(f"rccl{tag}", {c: datapath.rccl_in_domain(trays, inventory, checks, c) for c in colls})
    else:
        if len(trays) < 2:
            pytest.skip("cross-domain traffic needs 2 computes")
        datapath.require_transferbench_cross(inventory)
        record_property(f"tb{tag}", datapath.transferbench_cross_domain(trays, inventory, checks))
        record_property(f"rccl{tag}", {c: datapath.rccl_cross_domain(trays, inventory, checks, c) for c in colls})


# ================================================================== node-agent datapath

@pytest.mark.l3
@pytest.mark.tc("TC-NA-003")
def test_l2_within_compute(computes, inventory, checks, record_property):
    """L2 ping between the GPUs of one compute tray (the first selected)."""
    record_property("pairs", datapath.l2_ping(computes[:1], inventory, checks))
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-NA-004")
def test_l2_across_computes(computes, inventory, checks, record_property):
    """L2 ping between GPUs of different compute trays in the default vPOD (ring pairs)."""
    if len(computes) < 2:
        pytest.skip("needs 2 computes")
    record_property("pairs", datapath.l2_ping(computes, inventory, checks))
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-E2E-009")
def test_ping_check_default_vpod(ops, computes, inventory, checks, record_property):
    """The ping check for the default (system) vPOD: every compute in it pings its ring peers over every
    LINK_UP port."""
    sysv = ops.system_vpod_name()
    members = set(_afm_names(ops, computes).items())
    inside = [t for t in computes if any(t.name == tn and ops.membership(an) == {sysv} for tn, an in members)]
    if not inside:
        pytest.skip(f"no selected compute is in {sysv}")
    record_property("pairs", datapath.l2_ping(inside, inventory, checks))
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-NA-005")
def test_transferbench_setup(computes, inventory, checks, record_property):
    """TransferBench is installed and runs on every tray (in-domain smoke)."""
    record_property("tb", datapath.transferbench_in_domain(computes, inventory, checks))
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-NA-006")
def test_rccl_allreduce_in_domain(computes, inventory, checks, record_property):
    """rccl-tests all_reduce_perf on each compute tray."""
    record_property("rccl", datapath.rccl_in_domain(computes, inventory, checks, "all_reduce"))
    checks.assert_all()


def _mac_counters(tray) -> dict:
    """{bdf: total of every numeric MAC counter over its ports}."""
    r = tray.run("afmctl show port statistics mac --json 2>/dev/null", sudo=True, timeout=120).check()
    doc = json.loads(r.stdout)
    out = {}
    for d in doc.get("devices") or doc.get("device") or []:
        tot = 0
        for p in d.get("port") or []:
            tot += sum(int(v) for v in (p.get("statistics") or {}).values() if str(v).isdigit())
        out[d.get("bdf")] = tot
    return out


@pytest.mark.l3
@pytest.mark.tc("TC-HW-010")
def test_ifoe_port_stats_after_traffic(computes, inventory, checks, record_property):
    """HW-004: IFoE MAC counters (afmctl show port statistics mac) increase on every GPU after TransferBench."""
    tray = computes[0]
    before = _mac_counters(tray)
    datapath.transferbench_in_domain([tray], inventory, checks)
    after = _mac_counters(tray)
    record_property("counters", {b: [before.get(b), after.get(b)] for b in after})
    for bdf, v in after.items():
        checks.check(v > before.get(bdf, 0), f"{tray.name}/{bdf}: MAC counters did not increase ({before.get(bdf)} -> {v})")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-TRG-001")
def test_traffic_survives_link_flap(ops, afm_ready, links, computes, inventory, checks, record_property):
    """In-domain TransferBench on one tray while one of its fabric ports is flapped on the switch: the run
    still passes and the port comes back."""
    tray = computes[0]
    name = _afm_names(ops, [tray]).get(tray.name)
    if not name:
        pytest.skip(f"{tray.name} not found in AFM")
    sw, ns, port, _ = ops.gpu_links(ops.compute(name)["spec"]["slot-id"])[0]
    sub = type(checks)()
    th = threading.Thread(target=lambda: record_property("tb", datapath.transferbench_in_domain([tray], inventory, sub)))
    th.start()
    links.shut(sw, ns, port)
    links.restore(sw, ns, port, checks)
    th.join()
    for f in sub.failures:
        checks.fail(f"traffic during the flap: {f}")
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.disruptive
@pytest.mark.tc("TC-NA-019")
def test_vpod_and_datapath_after_recommission(ops, afm_ready, computes, inventory, checks, record_property):
    """A compute decommissioned, deleted, readmitted and put back in the system vPOD: L2 ping and in-domain
    TransferBench work on it again."""
    if not any(ops.sb_agents(sw) for sw in ops.rack.switches.values()):
        pytest.skip("not in sideband control mode")
    tray = computes[-1]
    name = _afm_names(ops, [tray]).get(tray.name)
    if not name:
        pytest.skip(f"{tray.name} not found in AFM")
    sysv = ops.system_vpod_name()
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    try:
        ops.afm.raw("POST", f"{CN}/{name}/Decommission", {})
        ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10)
        ops.afm.raw("DELETE", f"{CN}/{name}")
        ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5)
        sw, _ = ops.slot_host(slot)
        ops.afm_agent(sw, "restart")
        checks.check(ops.wait_compute_ready(name), f"{name} not readmitted")
    finally:
        if ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait(lambda: ops.membership(name) == {sysv}, 300, 10), f"{name} not back in {sysv}")
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    ops.__dict__.pop("_trays", None)
    record_property("pairs", datapath.l2_ping(computes if len(computes) > 1 else [tray], inventory, checks))
    record_property("tb", datapath.transferbench_in_domain([tray], inventory, checks))
    checks.assert_all()


# ================================================================== RAS reset with AFM

def _reconfigure_all(ops, trays, checks):
    names = _afm_names(ops, trays)
    for t in trays:
        n = names.get(t.name)
        if not n:
            checks.fail(f"{t.name}: no AFM compute")
            continue
        r = ops.reconfigure(n)
        checks.check(r.status_code == 200, f"Reconfigure {n} -> HTTP {r.status_code}")
    for n in names.values():
        checks.check(ops.wait_compute_ready(n, 300), f"{n} not Ready after Reconfigure")


def _datapath_reset(trays, inventory, checks):
    tmpl = inventory.cmd("ras.datapath_reset", "afmctl reset device --datapath")
    for c, r, exc in fan_out(lambda c: c.run(render(tmpl, name=c.name), sudo=True, timeout=300), trays):
        checks.check(not exc and r.ok, f"{c.name}: datapath reset failed: {exc or f'rc={r.rc} {r.tail()!r}'}")


def _partial(trays, inventory, checks, which):
    tmpl = inventory.cmd(f"ras.partial_stations_{which}")
    for c, r, exc in fan_out(lambda c: c.run(render(tmpl, name=c.name), sudo=True, timeout=120), trays):
        checks.check(not exc and r.ok, f"{c.name}: partial_stations_{which} failed: {exc or f'rc={r.rc}'}")


def _ras(ops, trays, inventory, checks, record_property, *, scope, partial, order):
    """order: 'reconfigure' | 'reset_then_reconfigure' | 'reconfigure_then_reset'."""
    if partial and not (inventory.cmd("ras.partial_stations_down") and inventory.cmd("ras.partial_stations_up")):
        pytest.skip("commands.ras.partial_stations_down / _up not set")
    if scope == "cross" and len(trays) < 2:
        pytest.skip("cross-domain case needs 2 computes")
    try:
        if partial:
            _partial(trays, inventory, checks, "down")
        if order == "reset_then_reconfigure":
            _datapath_reset(trays, inventory, checks)
        _reconfigure_all(ops, trays, checks)
        if order == "reconfigure_then_reset":
            _datapath_reset(trays, inventory, checks)
        _traffic(scope, trays, inventory, checks, record_property)
    finally:
        if partial:
            _partial(trays, inventory, checks, "up")
    checks.check(ops.wait_vpod(ops.system_vpod_name()) in TERMINAL, "system vPOD not online/degraded")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-RAS-reset-010")
def test_ras_010(ops, afm_ready, computes, inventory, checks, record_property):
    """In-domain TransferBench + RCCL after a hard reset (AFM Reconfigure of every tray), all ports up."""
    _ras(ops, computes, inventory, checks, record_property, scope="in", partial=False, order="reconfigure")


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-RAS-reset-011")
def test_ras_011(ops, afm_ready, computes, inventory, checks, record_property):
    """In-domain TransferBench + RCCL after a hard reset (AFM Reconfigure of every tray), partial stations down (commands.ras.partial_stations_*)."""
    _ras(ops, computes, inventory, checks, record_property, scope="in", partial=True, order="reconfigure")


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-RAS-reset-012")
def test_ras_012(ops, afm_ready, computes, inventory, checks, record_property):
    """Cross-domain TransferBench + RCCL after a hard reset (AFM Reconfigure of every tray), all ports up."""
    _ras(ops, computes, inventory, checks, record_property, scope="cross", partial=False, order="reconfigure")


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-RAS-reset-013")
def test_ras_013(ops, afm_ready, computes, inventory, checks, record_property):
    """Cross-domain TransferBench + RCCL after a hard reset (AFM Reconfigure of every tray), partial stations down (commands.ras.partial_stations_*)."""
    _ras(ops, computes, inventory, checks, record_property, scope="cross", partial=True, order="reconfigure")


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-RAS-reset-014")
def test_ras_014(ops, afm_ready, computes, inventory, checks, record_property):
    """Cross-domain TransferBench + RCCL after afmctl datapath reset, then AFM Reconfigure, all ports up."""
    _ras(ops, computes, inventory, checks, record_property, scope="cross", partial=False, order="reset_then_reconfigure")


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-RAS-reset-015")
def test_ras_015(ops, afm_ready, computes, inventory, checks, record_property):
    """Cross-domain TransferBench + RCCL after afmctl datapath reset, then AFM Reconfigure, partial stations down (commands.ras.partial_stations_*)."""
    _ras(ops, computes, inventory, checks, record_property, scope="cross", partial=True, order="reset_then_reconfigure")


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-RAS-reset-016")
def test_ras_016(ops, afm_ready, computes, inventory, checks, record_property):
    """Cross-domain TransferBench + RCCL after AFM Reconfigure, then afmctl datapath reset, all ports up."""
    _ras(ops, computes, inventory, checks, record_property, scope="cross", partial=False, order="reconfigure_then_reset")


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-RAS-reset-017")
def test_ras_017(ops, afm_ready, computes, inventory, checks, record_property):
    """Cross-domain TransferBench + RCCL after AFM Reconfigure, then afmctl datapath reset, partial stations down (commands.ras.partial_stations_*)."""
    _ras(ops, computes, inventory, checks, record_property, scope="cross", partial=True, order="reconfigure_then_reset")


def _vpod_split(ops, trays, inventory, checks, record_property, uniq, *, rejoin: bool):
    if len(trays) < 2:
        pytest.skip("needs 2 computes")
    names = _afm_names(ops, trays[:2])
    n1, n2 = (names.get(t.name) for t in trays[:2])
    if not (n1 and n2):
        pytest.skip("computes not found in AFM")
    sysv = ops.system_vpod_name()
    va, vb = uniq("va"), uniq("vb")
    e1, e2 = [], []
    try:
        e1 = ops.remove_compute(sysv, n1)
        e2 = ops.remove_compute(sysv, n2)
        ops.create_vpod(va, e1 + e2)
        checks.check(ops.wait_vpod(va) in TERMINAL, f"{va} {ops.vpod_state(va)}")
        _traffic("cross", trays[:2], inventory, checks, record_property, "_both_in_A")
        ops.remove_compute(va, n1)
        ops.create_vpod(vb, e1)
        checks.check(ops.wait_vpod(vb) in TERMINAL, f"{vb} {ops.vpod_state(vb)}")
        _traffic("in", trays[:2], inventory, checks, record_property, "_split")
        if rejoin:
            ops.remove_compute(va, n2)
            ops.add_gpus(vb, e2)
            checks.check(ops.wait_vpod(vb) in TERMINAL, f"{vb} {ops.vpod_state(vb)} with both computes")
            _traffic("cross", trays[:2], inventory, checks, record_property, "_both_in_B")
    finally:
        ops.delete_vpod(vb)
        ops.delete_vpod(va)
        ops.add_gpus(sysv, e1 + e2)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-RAS-reset-005")
def test_ras_005(ops, afm_ready, computes, inventory, uniq, checks, record_property):
    """Two computes in one user vPOD: cross-domain TransferBench/RCCL works; node1 moved to a different user vPOD:
    in-domain TransferBench/RCCL works on both."""
    _vpod_split(ops, computes, inventory, checks, record_property, uniq, rejoin=False)


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-RAS-reset-006")
def test_ras_006(ops, afm_ready, computes, inventory, uniq, checks, record_property):
    """From the TC-RAS-reset-005 state, node2 joins node1's user vPOD: cross-domain TransferBench/RCCL works."""
    _vpod_split(ops, computes, inventory, checks, record_property, uniq, rejoin=True)
