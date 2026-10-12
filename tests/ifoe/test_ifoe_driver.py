"""TC-AFM-LU-001, TC-HW-017 — IFoE driver unload/reload on a live rack (disruptive).

Inventory ``commands.ifoe``:
  unload / load       e.g. "modprobe -r ifoe" / "modprobe ifoe"           (VERIFY module name)
  loaded_check        exits 0 iff the driver is loaded, e.g. "lsmod | grep -q '^ifoe '"
  ready_check         optional; polled until rc=0 after reload (IFoE ports/stations back)
``commands.gpu_health`` (default "amd-smi list") must list one ``GPU: N`` line per GPU.
"""
from __future__ import annotations

import re
import time

import pytest

from helios import datapath
from helios.compute import render
from helios.rack import fan_out
from helios.wait import wait_until

pytestmark = [pytest.mark.l2, pytest.mark.disruptive]


def _tmpl(inventory, key):
    t = inventory.cmd(f"ifoe.{key}")
    if not t:
        pytest.skip(f"commands.ifoe.{key} not set")
    return t


def _gpu_health(computes, inventory, checks, when: str) -> None:
    cmd = inventory.cmd("gpu_health", "amd-smi list")
    for c, r, exc in fan_out(lambda c: c.run(cmd, sudo=True, timeout=120), computes):
        if exc or not r.ok:
            checks.fail(f"{c.name}: {when}: '{cmd}' failed: {exc or r.tail(200)}")
            continue
        n = len(re.findall(r"(?m)^\s*GPU:\s*\d+", r.stdout))
        checks.check(n >= c.gpus, f"{c.name}: {when}: {n} GPUs listed, expected {c.gpus}")


def _cycle(computes, inventory, checks, it: int) -> None:
    unload, load, loaded = (_tmpl(inventory, k) for k in ("unload", "load", "loaded_check"))
    ready = inventory.cmd("ifoe.ready_check")
    timeout = float(inventory.th("ifoe.ready_timeout_s", 300))
    interval = float(inventory.th("ifoe.poll_interval_s", 10))

    def one(c):
        c.run(render(unload, name=c.name), sudo=True, timeout=180).check()
        if c.run(loaded, sudo=True).ok:
            return "driver still loaded after unload"
        c.run(render(load, name=c.name), sudo=True, timeout=180).check()
        if not c.run(loaded, sudo=True).ok:
            return "driver not loaded after load"
        if ready and not wait_until(lambda: c.run(render(ready, name=c.name), sudo=True, timeout=60).ok, timeout, interval)[0]:
            return f"not ready within {timeout:.0f}s after reload"
        return ""

    for c, msg, exc in fan_out(one, computes):
        checks.check(not exc and not msg, f"{c.name}: cycle {it}: {exc or msg}")
    if not ready:
        time.sleep(float(inventory.th("ifoe.settle_s", 30)))


@pytest.mark.tc("TC-AFM-LU-001")
def test_ifoe_driver_load_unload_cycles(computes, inventory, checks, record_property):
    """N unload/load cycles (default 5) with GPU health after each; TransferBench in-domain
    before and after must not regress more than ``ifoe.max_bw_drop_pct``."""
    for k in ("unload", "load", "loaded_check"):
        _tmpl(inventory, k)
    datapath.template(inventory, "transferbench.in_domain")
    cycles = int(inventory.th("ifoe.cycles", 5))
    max_drop = float(inventory.th("ifoe.max_bw_drop_pct", 10))

    baseline = datapath.transferbench_in_domain(computes, inventory, checks)
    _gpu_health(computes, inventory, checks, "baseline")
    for it in range(1, cycles + 1):
        _cycle(computes, inventory, checks, it)
        _gpu_health(computes, inventory, checks, f"after cycle {it}")
    after = datapath.transferbench_in_domain(computes, inventory, checks)
    for name, bw in after.items():
        ref = baseline.get(name)
        if ref and bw is not None:
            checks.check(bw >= ref * (1 - max_drop / 100),
                         f"{name}: TransferBench {bw:.1f} GB/s after {cycles} cycles vs baseline {ref:.1f} (> {max_drop:g}% drop)")
    record_property("bw_gbps", {"baseline": baseline, "after": after})
    checks.assert_all()


@pytest.mark.tc("TC-HW-017")
def test_ifoe_driver_remove_readd(computes, inventory, checks, record_property):
    """One remove/re-add, then the accelerator table must work again: L2 ping and TransferBench in-domain."""
    for k in ("unload", "load", "loaded_check"):
        _tmpl(inventory, k)
    datapath.template(inventory, "l2ping.cmd")
    _cycle(computes, inventory, checks, 1)
    _gpu_health(computes, inventory, checks, "after re-add")
    record_property("pings", datapath.l2_ping(computes, inventory, checks))
    record_property("bw_gbps", datapath.transferbench_in_domain(computes, inventory, checks))
    checks.assert_all()
