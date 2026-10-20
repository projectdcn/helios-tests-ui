"""TC-SWH-060 (incast congestion): a load that did not happen is BLOCKED, never 'no congestion seen'. g02-g17 19 Oct
00:05 UTC: 2 sources, one TransferBench rank died, 6.6 s of one-to-one traffic, then a FAIL that blamed the switch."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "afm"))
import test_afm_switch_faults as T  # noqa: E402

from helios.rack import Checks  # noqa: E402


class Inv:
    def __init__(self, **th):
        self._th = {"switch.watermark_interval_s": 1, "switch.watermark_margin_s": 0, "datapath.congestion_traffic_s": 1,
                    "switch.queue_window_s": 0, "afm.plane_restore_s": 1, **th}

    def th(self, k, d=None):
        return self._th.get(k, d)

    def cmd(self, k, d=None):
        return d


class Ops:
    def __init__(self, n):
        self.n = n

    def tray(self, name):
        return SimpleNamespace(name=name)

    def system_vpod_name(self):
        return "pod-1.system"

    def planes(self, v):
        return (36, 36)

    def controller_now(self):
        return "now"

    def wait(self, fn, t, s):
        return fn()

    def events(self, since=None):
        return []


def run(monkeypatch, n_trays, load, loaded_wm=500000.0, **th):
    trays = [SimpleNamespace(name=f"ct{i:02d}") for i in range(1, n_trays + 1)]
    monkeypatch.setattr(T, "_ready_slots", lambda ops: {i: t.name for i, t in enumerate(trays)})
    monkeypatch.setattr(T, "_slot_ports", lambda rack, slot: {("st1", "asic0", "Ethernet1"): "up"})
    monkeypatch.setattr(T, "_clear_wm", lambda rack, ports: None)
    wm = iter([{("st1", "asic0", "Ethernet1"): 420.0}, {("st1", "asic0", "Ethernet1"): loaded_wm}])
    monkeypatch.setattr(T, "_watermarks", lambda rack, ports: next(wm))
    monkeypatch.setattr(T, "_drops", lambda rack, ports: {})
    monkeypatch.setattr(T, "_event_types", lambda ev: {})
    monkeypatch.setattr(T, "_required_events", lambda *a: None)
    monkeypatch.setattr(T.time, "sleep", lambda s: None)
    monkeypatch.setattr(T.datapath, "require_transferbench_cross", lambda inv: None)
    monkeypatch.setattr(T.datapath, "transferbench_incast", lambda dst, srcs, inv, checks, s: load(srcs, checks))
    props = {}
    T.test_congestion_detected_on_incast(Ops(n_trays), None, trays, Inv(**th), None, None, Checks(),
                                         lambda k, v: props.__setitem__(k, v))
    return props


def test_the_19_oct_run_is_blocked_not_a_switch_failure(monkeypatch):
    def broken(srcs, checks):
        checks.fail("ct08->ct12: rank on ct12 failed: rc=1 'recv: Invalid argument'")
        return {"rounds": 1, "seconds": 6.6, "bw_gbps": [{"ct08->ct12": None, "ct09->ct12": 364.47}]}
    with pytest.raises(pytest.fail.Exception) as e:
        run(monkeypatch, 5, broken)
    msg = str(e.value)
    assert "pre-flight failed - the load was not generated" in msg          # -> BLOCKED in the report
    assert "ct08->ct12" in msg and "congestion not judged" in msg and "no congestion seen" not in msg


def test_too_few_source_trays_is_blocked(monkeypatch):
    with pytest.raises(pytest.fail.Exception, match=r"pre-flight failed - only 2 source tray\(s\)"):
        run(monkeypatch, 3, lambda srcs, checks: {"rounds": 9, "seconds": 200, "bw_gbps": [{}]})


def test_a_real_incast_is_judged(monkeypatch):
    ok = lambda srcs, checks: {"rounds": 5, "seconds": 160.0, "bw_gbps": [{f"{s.name}->ct06": 300.0 for s in srcs}]}  # noqa: E731
    props = run(monkeypatch, 6, ok)                                          # 500 KB watermark: congestion seen
    assert props["sources"] == ["ct02", "ct03", "ct04", "ct05"] and props["destination"].startswith("ct06")
    with pytest.raises(pytest.fail.Exception, match="no congestion seen"):  # a real load, low watermark: a real FAIL
        run(monkeypatch, 6, ok, loaded_wm=20000.0)
