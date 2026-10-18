"""AFM compute-tray Reconfigure (GPU reset + fabric re-program) and mini-rack switch guards.

DISRUPTIVE: Reconfigure resets the tray's GPUs and interrupts any traffic on them; needs
--allow-disruptive. ``afm_ready`` gates on rack health first (BLOCKED if AFM is not healthy) and checks it
again afterwards.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from helios.afm import CLUSTER, wait_for

CN = f"{CLUSTER}/computenodes"


def _gpus_ready(afm, name: str) -> bool:
    n = afm.get(f"{CN}/{name}")
    gi = ((n.get("status") or {}).get("scale-up-info") or {}).get("gpu-info") or {}
    return bool(gi) and all(g.get("oper-state") == "Ready" for g in gi.values())


def _settled(afm) -> bool:
    v = afm.system_vpod() or {}
    st = v.get("status") or {}
    return st.get("vpod-state") in ("online", "degraded") and not st.get("pending-ops")


def _planes(afm):
    st = (afm.system_vpod() or {}).get("status") or {}
    return st.get("available-switch-planes")


def _reconfigure(afm, name: str, retry_412_s: float = 0, interval: float = 10):
    """POST Reconfigure; with retry_412_s, retries while AFM answers 412 (another reconfigure in flight)."""
    end = time.monotonic() + retry_412_s
    while True:
        r = afm.raw("POST", f"{CN}/{name}/Reconfigure", {})
        if r.status_code != 412 or time.monotonic() >= end:
            return r
        time.sleep(interval)


def _retry(inventory) -> float:
    return float(inventory.th("afm.retry_interval_s", 10))


def _wait_recovered(afm, inventory, names, checks, baseline_planes):
    t = float(inventory.th("afm.reconfigure_timeout_s", 180))
    for n in names:
        checks.check(wait_for(lambda n=n: _gpus_ready(afm, n), t, 10, f"{n} GPUs Ready"),
                     f"{n}: GPUs not all Ready {t:.0f}s after Reconfigure")
    checks.check(wait_for(lambda: _settled(afm), t, 10, "system vPOD settled"),
                 f"system vPOD not online/degraded with empty pending-ops {t:.0f}s after Reconfigure")
    if baseline_planes is not None:
        now = _planes(afm)
        checks.check(now is not None and now >= baseline_planes,
                     f"available-switch-planes {now} < pre-test baseline {baseline_planes}")


def _computes(afm, pytestconfig) -> list[str]:
    # quarantined trays left out (g02-g17 18 Oct 03:50: TC-AFM-RC-002 / 003 sent Reconfigure to pod-1-4 = ct05, Down -> 412)
    inv = getattr(pytestconfig, "_helios_inventory", None)
    names = [c["meta"]["name"] for c in afm.computes() if inv is None or not inv.is_quarantined_afm(c)]
    if not names:
        pytest.fail("no compute nodes in AFM")
    return names


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-AFM-RC-001")
def test_reconfigure_single(afm, afm_ready, inventory, pytestconfig, checks, record_property):
    """Reconfigure one compute (the first, or thresholds.afm.reconfigure_compute): 200, its GPUs return to
    Ready, the system vPOD settles and keeps its available switch planes."""
    name = inventory.th("afm.reconfigure_compute") or _computes(afm, pytestconfig)[0]
    base = _planes(afm)
    t0 = time.monotonic()
    r = _reconfigure(afm, name)
    checks.check(r.status_code == 200, f"Reconfigure {name} -> HTTP {r.status_code}: {r.text[:200]}")
    _wait_recovered(afm, inventory, [name], checks, base)
    record_property("recovery_s", round(time.monotonic() - t0, 1))
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-AFM-RC-002")
def test_reconfigure_all_sequential(afm, afm_ready, inventory, pytestconfig, checks, record_property):
    """Reconfigure every compute one after another, each recovering before the next."""
    base = _planes(afm)
    took = {}
    for name in _computes(afm, pytestconfig):
        t0 = time.monotonic()
        r = _reconfigure(afm, name, retry_412_s=180, interval=_retry(inventory))
        checks.check(r.status_code == 200, f"Reconfigure {name} -> HTTP {r.status_code}: {r.text[:200]}")
        _wait_recovered(afm, inventory, [name], checks, base)
        took[name] = round(time.monotonic() - t0, 1)
    record_property("recovery_s", took)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-AFM-RC-003")
def test_reconfigure_all_parallel(afm, afm_ready, inventory, pytestconfig, checks, record_property):
    """Reconfigure every compute at once (each retried while AFM answers 412 'in progress'); all recover."""
    names = _computes(afm, pytestconfig)
    base = _planes(afm)
    with ThreadPoolExecutor(len(names)) as ex:
        res = dict(zip(names, ex.map(lambda n: _reconfigure(afm, n, retry_412_s=180, interval=_retry(inventory)), names)))
    record_property("status", {n: r.status_code for n, r in res.items()})
    for n, r in res.items():
        checks.check(r.status_code == 200, f"Reconfigure {n} -> HTTP {r.status_code}: {r.text[:200]}")
    _wait_recovered(afm, inventory, names, checks, base)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-AFM-RC-004")
def test_reconfigure_back_to_back(afm, afm_ready, inventory, pytestconfig, checks, record_property):
    """Two immediate Reconfigures of one compute: the first 200, the second 412 (in progress, AIFM-1702
    guard) or 200 (idempotent) - never 5xx; the compute recovers once."""
    name = inventory.th("afm.reconfigure_compute") or _computes(afm, pytestconfig)[0]
    base = _planes(afm)
    r1 = _reconfigure(afm, name, retry_412_s=180, interval=_retry(inventory))  # wait out a Reconfigure still in flight from an earlier test
    r2 = _reconfigure(afm, name)
    record_property("status", [r1.status_code, r2.status_code])
    checks.check(r1.status_code == 200, f"1st Reconfigure -> HTTP {r1.status_code}: {r1.text[:200]}")
    checks.check(r2.status_code in (200, 412), f"2nd Reconfigure -> HTTP {r2.status_code}: {r2.text[:200]}")
    _wait_recovered(afm, inventory, [name], checks, base)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-AFM-035A")
def test_minirack_switch_ops_rejected(afm, afm_ready, checks, record_property):
    """Mini rack (one switch tray): DrainSwitchNode, DecommissionSwitchNode (also forced) and DELETE of the
    last in-service switch are rejected with 412. If AFM wrongly accepts one, the switch is reactivated."""
    sw = afm.switches()
    if len(sw) != 1:
        pytest.skip(f"{len(sw)} switch nodes: TC-AFM-035A is for a single-switch mini rack")
    name = sw[0]["meta"]["name"]
    base = f"{CLUSTER}/switchnodes/{name}"
    got = {}
    accepted = False
    for label, method, path, body in (("drain", "POST", f"{base}/DrainSwitchNode", {}),
                                      ("decommission", "POST", f"{base}/DecommissionSwitchNode", {}),
                                      ("force-decommission", "POST", f"{base}/DecommissionSwitchNode",
                                       {"force-decommission": True}),
                                      ("delete", "DELETE", base, None)):
        r = afm.raw(method, path, body)
        got[label] = f"HTTP {r.status_code} {r.text[:160]}"
        if not checks.check(r.status_code == 412, f"{label} {name} -> HTTP {r.status_code}, expected 412"):
            accepted = accepted or r.status_code < 300
            if accepted:
                break  # stop before making it worse
    record_property("responses", got)
    if accepted:
        afm.raw("POST", f"{base}/ReactivateSwitchNode", {})
        checks.check(wait_for(lambda: (afm.get(base).get("status") or {}).get("admission-phase") == "admitted", 300, 10),
                     f"{name} not re-admitted after recovery ReactivateSwitchNode")
    checks.assert_all()
