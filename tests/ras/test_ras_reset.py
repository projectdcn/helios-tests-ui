"""TC-RAS-reset-001..004, 007..009 — datapath reset / reboot / power cycle, then re-validate traffic.

Not covered here (need the AFM API): 005/006 (vPOD moves) and 010..017
(hard reset = "reconfigure compute tray from AFM").

All cases are disruptive (--allow-disruptive). Inventory keys under ``commands.ras``:
  datapath_reset   default "afmctl reset device --datapath"   placeholders: {name}
  ready_check      optional; polled until rc=0 after any reset   placeholders: {name}
  partial_stations_down / partial_stations_up   take some IFoE stations down / restore (002, 004)
  dc_cycle / ac_cycle   run on the test runner (BMC / PDU)     placeholders: {name} {host} {bmc} {pdu_outlet}
"""
from __future__ import annotations

import time

import pytest

from helios import datapath
from helios.compute import render
from helios.rack import fan_out
from helios.wait import wait_for_ssh, wait_until

pytestmark = [pytest.mark.l2, pytest.mark.disruptive]

REBOOT = "nohup sh -c 'sleep 2; systemctl reboot' >/dev/null 2>&1 &"


def _require(inventory, scope: str) -> None:
    """Fail fast (skip) on missing workload templates *before* resetting anything."""
    keys = ["transferbench.in_domain", "rccl.in_domain"] if scope == "in" else \
           ["transferbench.cross_domain", "rccl.cross_domain"]
    for k in keys:
        datapath.template(inventory, k)


def _wait_ready(nodes, inventory, checks) -> None:
    ready = inventory.cmd("ras.ready_check")
    if not ready:
        time.sleep(float(inventory.th("ras.settle_s", 30)))
        return
    timeout = float(inventory.th("ras.ready_timeout_s", 300))
    interval = float(inventory.th("ras.poll_interval_s", 10))

    def wait(c):
        return wait_until(lambda: c.run(render(ready, name=c.name), timeout=60).ok, timeout, interval)[0]

    for c, ok, exc in fan_out(wait, nodes):
        checks.check(ok and not exc, f"{c.name}: not ready within {timeout:.0f}s after reset ({exc or 'ready_check failing'})")


def _datapath_reset(nodes, inventory, checks) -> None:
    tmpl = inventory.cmd("ras.datapath_reset", "afmctl reset device --datapath")
    for c, r, exc in fan_out(lambda c: c.run(render(tmpl, name=c.name), sudo=True, timeout=300), nodes):
        checks.check(not exc and r.ok, f"{c.name}: datapath reset failed: {exc or f'rc={r.rc} {r.tail()!r}'}")
    _wait_ready(nodes, inventory, checks)


def _traffic(scope: str, computes, inventory, checks, record_property) -> None:
    colls = inventory.cmd("ras.collectives", ["all_reduce"])
    if scope == "in":
        record_property("tb_bw_gbps", datapath.transferbench_in_domain(computes, inventory, checks))
        record_property("rccl", {c: datapath.rccl_in_domain(computes, inventory, checks, c) for c in colls})
    else:
        record_property("tb_bw_gbps", datapath.transferbench_cross_domain(computes, inventory, checks))
        record_property("rccl", {c: datapath.rccl_cross_domain(computes, inventory, checks, c) for c in colls})


def _partial_stations(computes, inventory, checks, which: str) -> None:
    tmpl = inventory.cmd(f"ras.partial_stations_{which}")
    for c, r, exc in fan_out(lambda c: c.run(render(tmpl, name=c.name), sudo=True, timeout=120), computes):
        checks.check(not exc and r.ok, f"{c.name}: partial_stations_{which} failed: {exc or f'rc={r.rc} {r.tail()!r}'}")


def _reset_then_traffic(scope, partial, computes, inventory, checks, record_property):
    _require(inventory, scope)
    if partial and not (inventory.cmd("ras.partial_stations_down") and inventory.cmd("ras.partial_stations_up")):
        pytest.skip("commands.ras.partial_stations_down / _up not set")
    if scope == "cross" and len(computes) < 2:
        pytest.skip("cross-domain case needs >= 2 computes")
    try:
        if partial:
            _partial_stations(computes, inventory, checks, "down")
        _datapath_reset(computes, inventory, checks)
        _traffic(scope, computes, inventory, checks, record_property)
    finally:
        if partial:
            _partial_stations(computes, inventory, checks, "up")
    checks.assert_all()


@pytest.mark.tc("TC-RAS-reset-001")
def test_in_domain_after_datapath_reset_all_ports(computes, inventory, checks, record_property):
    _reset_then_traffic("in", False, computes, inventory, checks, record_property)


@pytest.mark.tc("TC-RAS-reset-002")
def test_in_domain_after_datapath_reset_partial_stations(computes, inventory, checks, record_property):
    _reset_then_traffic("in", True, computes, inventory, checks, record_property)


@pytest.mark.tc("TC-RAS-reset-003")
def test_cross_domain_after_datapath_reset_all_ports(computes, inventory, checks, record_property):
    _reset_then_traffic("cross", False, computes, inventory, checks, record_property)


@pytest.mark.tc("TC-RAS-reset-004")
def test_cross_domain_after_datapath_reset_partial_stations(computes, inventory, checks, record_property):
    _reset_then_traffic("cross", True, computes, inventory, checks, record_property)


def _power_event(kind, rack, computes, inventory, checks, record_property):
    """Take one node down (OS reboot / DC cycle / AC cycle), wait for it, datapath-reset the
    nodes that stayed up, then run cross-domain TransferBench + RCCL across all of them."""
    _require(inventory, "cross")
    if len(computes) < 2:
        pytest.skip("needs >= 2 computes")
    name = inventory.cmd("ras.power_target") or computes[0].name
    target = next((c for c in computes if c.name == name), None)
    if target is None:
        pytest.skip(f"ras.power_target {name!r} not among selected computes")
    others = [c for c in computes if c is not target]

    if kind == "reboot":
        r = target.run(REBOOT, sudo=True, timeout=30)
    else:
        tmpl = inventory.cmd(f"ras.{kind}")
        if not tmpl:
            pytest.skip(f"commands.ras.{kind} not set (runs on the test runner, e.g. ipmitool/PDU)")
        extra = target.host.extra
        if "{bmc}" in tmpl and not extra.get("bmc"):
            pytest.skip(f"{target.name}: no 'bmc' in inventory")
        if "{pdu_outlet}" in tmpl and not extra.get("pdu_outlet"):
            pytest.skip(f"{target.name}: no 'pdu_outlet' in inventory")
        r = rack.runner.run(render(tmpl, name=target.name, host=target.host.host, bmc=extra.get("bmc", ""),
                                   pdu_outlet=extra.get("pdu_outlet", "")), timeout=120)
    assert r.ok, f"{target.name}: {kind} command failed rc={r.rc} {r.tail()!r}"

    boot_timeout = float(inventory.th("ras.boot_timeout_s", 900))
    back = wait_for_ssh(target, boot_timeout, interval=float(inventory.th("ras.poll_interval_s", 10)),
                        down_first=float(inventory.th("ras.down_wait_s", 60)))
    assert back, f"{target.name}: not reachable over SSH within {boot_timeout:.0f}s after {kind}"
    _wait_ready([target], inventory, checks)
    _datapath_reset(others, inventory, checks)
    _traffic("cross", computes, inventory, checks, record_property)
    checks.assert_all()


@pytest.mark.tc("TC-RAS-reset-007")
def test_os_reboot_one_node(rack, computes, inventory, checks, record_property):
    _power_event("reboot", rack, computes, inventory, checks, record_property)


@pytest.mark.tc("TC-RAS-reset-008")
def test_dc_cycle_one_node(rack, computes, inventory, checks, record_property):
    _power_event("dc_cycle", rack, computes, inventory, checks, record_property)


@pytest.mark.tc("TC-RAS-reset-009")
def test_ac_cycle_one_node(rack, computes, inventory, checks, record_property):
    _power_event("ac_cycle", rack, computes, inventory, checks, record_property)
