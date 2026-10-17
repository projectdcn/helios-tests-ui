"""TC-AFM-051 and the other restore tests leave quarantined nodes out of the post-restore health check (g02-g17
17 Oct: failed only on pod-1-4 = ct05, quarantined Down)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "afm")); sys.path.insert(0, str(ROOT))
import test_afm_snapshot as ts  # noqa: E402


class _Checks:
    def __init__(self):
        self.failed = []

    def check(self, ok, msg):
        if not ok:
            self.failed.append(msg)


def test_quarantined_compute_left_out():
    comps = [{"meta": {"name": "pod-1-4-ComputeTray"}}, {"meta": {"name": "pod-1-5-ComputeTray"}}]
    sws = [{"meta": {"name": "sw1"}}]
    q = lambda n: n["meta"]["name"] == "pod-1-4-ComputeTray"  # noqa: E731
    asked = []
    ops = SimpleNamespace(
        afm=SimpleNamespace(items=lambda p: [{"meta": {"name": "system-failover-policy"}}],
                            computes=lambda: comps, switches=lambda: sws),
        quarantined=q, active_computes=lambda: [c for c in comps if not q(c)], active_switches=lambda: sws,
        wait_compute_ready=lambda n, t: asked.append(n) or n != "pod-1-4-ComputeTray",
        wait=lambda fn, t, i: fn(), switch_ready=lambda n: True)
    checks, props = _Checks(), {}
    ts._healthy_after(ops, checks, lambda k, v: props.__setitem__(k, v))
    assert checks.failed == [] and asked == ["pod-1-5-ComputeTray"]
    assert props["quarantine_applied"] == ["pod-1-4-ComputeTray"]
