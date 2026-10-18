"""Rack maintenance (not a test-plan case): bring the system vPOD back to its baseline after tests whose clean-up
did not finish. DISRUPTIVE (changes vPOD membership); run on its own, by ID:

    pytest tests/afm/test_afm_maintenance.py --inventory <inv> --allow-disruptive --tc MAINT-SYSVPOD

Deletes leftover test vPODs (helios-*), puts every GPU of every admitted, non-quarantined compute back into the system
vPOD, waits for its pending operations to clear and, if it is still configurationfailed / needdatafabricreconfiguration,
sends ReconfigureDataFabric once. The state before and after is recorded (vpods_before / _after).
g02-g17 18 Oct 01:00 UTC: after batch 4 the system vPOD held 60 of 68 GPUs and an add_vpod for pod-1-17 stayed pending.
"""
from __future__ import annotations

from collections import Counter

import pytest

from helios.afm import CLUSTER
from helios.afm_ops import TERMINAL

pytestmark = [pytest.mark.disruptive]


def _state(ops) -> dict:
    out = {}
    for v in ops.afm.vpods():
        st = v.get("status") or {}
        slots = Counter(g.get("slot-id") for g in (v.get("spec") or {}).get("GPUs") or [])
        out[v["meta"]["name"]] = {"state": st.get("vpod-state"), "gpus": sum(slots.values()),
                                  "per_slot": dict(sorted(slots.items())),
                                  "pending": [(p.get("operation"), (p.get("pending-components") or {}).get("gpus"))
                                              for p in st.get("pending-ops") or []]}
    return out


@pytest.mark.tc("MAINT-SYSVPOD")
def test_restore_system_vpod(ops, checks, record_property, request):
    if "MAINT-SYSVPOD" not in str(request.config.getoption("--tc") or ""):
        pytest.skip("maintenance: runs only when asked for by ID (--tc MAINT-SYSVPOD)")
    sysv = ops.system_vpod_name()
    record_property("vpods_before", _state(ops))
    for v in ops.afm.vpods():
        n = v["meta"]["name"]
        if n.startswith("helios-"):
            checks.check(ops.delete_vpod(n, 300), f"leftover test vPOD {n} not deleted")
    want = []
    for c in ops.active_computes():
        name = c["meta"]["name"]
        if (c.get("status") or {}).get("admission-phase") != "admitted":
            checks.fail(f"{name} is {(c.get('status') or {}).get('admission-phase')}, not admitted - not re-added")
            continue
        if ops.membership(name) != {sysv}:
            want += ops.gpu_entries(name)
    record_property("readded", sorted({(g["slot-id"], g["index"]) for g in want}))
    if want:
        ops.add_gpus(sysv, want)
    checks.check(ops.wait_pending_clear(sysv, 900), f"{sysv} pending ops not cleared: {_state(ops).get(sysv)}")
    if ops.vpod_state(sysv) in ("configurationfailed", "needdatafabricreconfiguration"):
        # stuck with every GPU a member and nothing pending (g02-g17 18 Oct 02:44-03:14 after TC-AFM-RC-005): the
        # operator's way back is ReconfigureDataFabric, sent once
        r = ops.afm.raw("POST", f"{CLUSTER}/vpods/{sysv}/ReconfigureDataFabric", {})
        record_property("reconfigure_data_fabric", f"{ops.vpod_state(sysv)} -> HTTP {r.status_code} {r.text[:160]}")
    checks.check(ops.wait_vpod(sysv, timeout=600) in TERMINAL, f"{sysv} not online/degraded")
    after = _state(ops)
    record_property("vpods_after", after)
    expect = sum(len(ops.gpu_entries(c["meta"]["name"])) for c in ops.active_computes())
    checks.check(after.get(sysv, {}).get("gpus") == expect, f"{sysv} has {after.get(sysv, {}).get('gpus')} GPUs, expected {expect}")
    checks.assert_all()
