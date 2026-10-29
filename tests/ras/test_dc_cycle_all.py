"""TC-RAS-reset-008A - DC power cycle of every compute tray through its BMC (Redfish), then full recovery.

Disruptive (--allow-disruptive): every selected compute tray (--computes for a subset) is powered off and on.

Before any power action every tray's BMC must accept the login (address ``bmc:``, ``bmc_user:``, the BMC password:
$HELIOS_BMC_PASSWORD_<NODE> / $HELIOS_BMC_PASSWORD - the UI passes them). Then, ``batch_size`` trays at a time:
  1. note the tray's boot id and its newest 'Host system DC power' BMC event
  2. Redfish ComputerSystem.Reset ForceOff, wait for PowerState Off, hold ``off_hold_s``, On until PowerState On
     (an On sent while the host is still powering down is ignored, so it is re-sent up to ``on_attempts`` times;
     a refused login is never retried - the BMC locks the account after 5 bad logins)
  3. wait for SSH (``boot_timeout_s``) and require a new boot id (the host really restarted)
  4. modprobe amdgpu (blacklisted at boot), afmctl reset device --datapath, every GPU ACTIVE (as HC-013)
  5. record the BMC's new DC power off / on events
A batch finishes before the next one starts; a tray that fails stays failed, the others continue.

Thresholds ``ras.dc_cycle_all``: batch_size (6), off_timeout_s (120), off_hold_s (40), on_timeout_s (90),
on_attempts (3), boot_timeout_s (1200), poll_interval_s (10), load_amdgpu (true), active_timeout_s (300);
``commands.ras.datapath_reset`` (afmctl reset device --datapath). AFM state is not judged here: run the rack
health check afterwards (an AFM Reconfigure may be needed for GPUs AFM marked Failed).
"""
from __future__ import annotations

import pytest

from helios import bmc as B
from helios.datapath import DATAPATH_RESET, reset_tray, reset_tray_problem
from helios.progress import action
from helios.rack import fan_out
from helios.wait import wait_for_ssh

pytestmark = [pytest.mark.l2, pytest.mark.disruptive]

BOOT_ID = "cat /proc/sys/kernel/random/boot_id"


def _th(inventory, key, default):
    return inventory.th(f"ras.dc_cycle_all.{key}", default)


def _boot_id(c) -> str | None:
    r = c.run(BOOT_ID, timeout=20)
    return r.stdout.strip() if r.ok and r.stdout.strip() else None


def _bmcs(computes, inventory) -> tuple[dict, dict]:
    """({tray: (Redfish, system path)}, {tray: why not}) - every BMC logged into once, before anything is cycled."""
    timeout = float(inventory.th("bmc.timeout_s", 15))
    verify = bool(inventory.th("bmc.verify_tls", False))
    ok, bad = {}, {}

    def login(c):
        addr, user, pw, why = B.credentials(c.host)
        if why:
            raise B.BmcError(why)
        rf = B.Redfish(addr, user, pw, timeout=timeout, verify=verify)
        try:
            system = B.system_path(rf)
            B.power_state(rf, system)
        except Exception:
            rf.close()
            raise
        return rf, system

    for c, res, exc in fan_out(login, computes):
        if exc:
            bad[c.name] = str(exc)
        else:
            ok[c.name] = res
    return ok, bad


@pytest.mark.tc("TC-RAS-reset-008A")
def test_dc_cycle_every_compute(computes, inventory, checks, record_property):
    """DC power cycle every selected compute tray over Redfish, in batches; each comes back over SSH with a new
    boot id, amdgpu loaded, its datapath reset and every GPU ACTIVE."""
    bmcs, bad = _bmcs(computes, inventory)
    if not bmcs:
        pytest.skip("no compute BMC reachable: " + "; ".join(f"{n}: {w}" for n, w in bad.items()))
    if bad:      # refuse a partial rack cycle - fix the credentials first
        for rf, _ in bmcs.values():
            rf.close()
        pytest.fail("BMC login check failed, nothing was power cycled:\n" +
                    "\n".join(f"  - {n}: {w}" for n, w in bad.items()), pytrace=False)

    cfg = {k: float(_th(inventory, k, d)) for k, d in (("off_timeout_s", 120), ("off_hold_s", 40),
                                                       ("on_timeout_s", 90), ("boot_timeout_s", 1200),
                                                       ("poll_interval_s", 10), ("active_timeout_s", 300))}
    on_attempts = int(_th(inventory, "on_attempts", 3))
    load = bool(_th(inventory, "load_amdgpu", True))
    batch = max(1, int(_th(inventory, "batch_size", 6)))
    cmd = inventory.cmd("ras.datapath_reset", DATAPATH_RESET)

    def one(c):
        rf, system = bmcs[c.name]
        out = {"boot_id_before": _boot_id(c)}
        last = (B.power_events(rf, system) or [{}])[-1].get("created", "")
        action("%s: DC power cycle over Redfish (%s)", c.name, rf.address)
        out["power"] = B.dc_cycle(rf, system, off_timeout=cfg["off_timeout_s"], off_hold=cfg["off_hold_s"],
                                  on_timeout=cfg["on_timeout_s"], on_attempts=on_attempts,
                                  poll=min(cfg["poll_interval_s"], 5))
        if not wait_for_ssh(c, cfg["boot_timeout_s"], interval=cfg["poll_interval_s"]):
            out["problem"] = f"not reachable over SSH within {cfg['boot_timeout_s']:.0f}s after power on"
            return out
        out["boot_id_after"] = _boot_id(c)
        if out["boot_id_before"] and out["boot_id_after"] == out["boot_id_before"]:
            out["problem"] = "boot id unchanged - the host did not restart"
            return out
        out["datapath"] = reset_tray(c, cmd, load, cfg["active_timeout_s"], cfg["poll_interval_s"])
        out["bmc_events"] = [f"{e['created'][:19]} {e['message']}" for e in B.power_events(rf, system, after=last)]
        out["problem"] = reset_tray_problem(out["datapath"], cmd)
        return out

    results = {}
    trays = [c for c in computes if c.name in bmcs]
    try:
        for i in range(0, len(trays), batch):
            group = trays[i:i + batch]
            action("DC power cycle batch %d/%d: %s", i // batch + 1, -(-len(trays) // batch),
                   ", ".join(c.name for c in group))
            for c, out, exc in fan_out(one, group, workers=len(group)):
                if exc:
                    results[c.name] = {"problem": f"{type(exc).__name__}: {str(exc)[:200]}"}
                else:
                    results[c.name] = out
                problem = results[c.name].get("problem")
                checks.check(problem is None, f"{c.name}: {problem}")
    finally:
        for rf, _ in bmcs.values():
            rf.close()
        record_property("dc_cycle", results)
    checks.assert_all()
