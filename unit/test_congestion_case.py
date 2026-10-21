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


LOSSLESS = {("st1", "asic0"): {"prios": [0, 1], "pgs": ["PG0", "PG1"], "queues": ["UC0", "UC1"]}}


def snap(src_tx=0.0, q0_drop=0.0, q7_drop=0.0, pg7_drop=0.0, q0_pkts=0.0):
    """_counters() result: destination port Ethernet5 (slot 5), source ports Ethernet1..4."""
    return {"pfc": {**{("st1", "asic0", f"Ethernet{s}"): {"Rx": {0: 0.0}, "Tx": {0: src_tx}} for s in range(1, 5)},
                    ("st1", "asic0", "Ethernet5"): {"Rx": {0: 0.0}, "Tx": {0: 0.0}}},
            "queue": {("st1", "asic0", "Ethernet5", "UC0"): (q0_pkts, q0_drop),
                      ("st1", "asic0", "Ethernet5", "UC7"): (10.0, q7_drop)},
            "pg_drop": {("st1", "asic0", f"Ethernet{s}"): {"PG0": 0.0, "PG7": pg7_drop} for s in range(1, 5)}}


def run(monkeypatch, n_trays, load, loaded_wm=500000.0, snaps=None, **th):
    trays = [SimpleNamespace(name=f"ct{i:02d}") for i in range(1, n_trays + 1)]
    monkeypatch.setattr(T, "_ready_slots", lambda ops: {i: t.name for i, t in enumerate(trays)})

    def ports_by_slot(rack, slots, speeds=None, gpus=None):
        out = {s: {("st1", "asic0", f"Ethernet{s}"): "up"} for s in slots}
        if speeds is not None:
            speeds.update({k: float(th.get("_port_gbps", 400.0)) for v in out.values() for k in v})
        return out
    monkeypatch.setattr(T, "_ports_by_slot", ports_by_slot)
    monkeypatch.setattr(T, "_clear_wm", lambda rack, ports: None)
    monkeypatch.setattr(T, "_lossless", lambda rack, keys: LOSSLESS)
    it = iter(snaps or [snap(), snap(src_tx=5000.0, q0_pkts=9e6)])
    monkeypatch.setattr(T, "_counters", lambda rack, d, s: next(it))
    monkeypatch.setattr(T, "_buffer_peaks", lambda rack, d, s: {"queue": {("st1", "asic0", "Ethernet5"): {"UC0": loaded_wm}},
                                                                "pg": {}, "pool": {("st1", "asic0"): {"egress_pool": 1e6}}})
    wm = iter([{("st1", "asic0", "Ethernet1"): 420.0}, {("st1", "asic0", "Ethernet1"): loaded_wm}])
    monkeypatch.setattr(T, "_watermarks", lambda rack, ports: next(wm))
    monkeypatch.setattr(T, "_drops", lambda rack, ports: {})
    monkeypatch.setattr(T, "_event_types", lambda ev: {})
    monkeypatch.setattr(T, "_required_events", lambda *a: None)
    monkeypatch.setattr(T.time, "sleep", lambda s: None)
    monkeypatch.setattr(T.datapath, "require_transferbench_cross", lambda inv: None)
    monkeypatch.setattr(T.datapath, "transferbench_incast", lambda dst, srcs, inv, checks, s: load(srcs, checks))
    props = {}
    T.test_congestion_detected_on_incast(Ops(n_trays), None, trays, Inv(**{k: v for k, v in th.items() if k != "_port_gbps"}), None, None, Checks(),
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
    tr, ev = props["traffic"], props["incast_evidence"]
    assert tr["total_GBps"] == 1200.0 and tr["destination_line_rate_Gbps"] == 400.0 and tr["destination_utilisation"] == 24.0
    assert ev["traffic_queue"] == "UC0" and ev["traffic_queue_lossless"] is True
    assert ev["pfc_pause_frames"]["Tx_towards_sources"]["total"] == 20000.0      # 4 source ports x 5000
    assert ev["queue_watermark_peak_bytes"] == {"UC0": 500000.0}
    with pytest.raises(pytest.fail.Exception, match="no congestion seen"):  # a real load, low watermark: a real FAIL
        run(monkeypatch, 6, ok, loaded_wm=20000.0)


def test_a_load_far_below_line_rate_is_blocked_not_failed(monkeypatch):
    """g02-g17 21 Oct: 1487.7 GB/s into 144 x 400G = 0.21 of the destination's line rate, watermark 23 KB."""
    ok = lambda srcs, checks: {"rounds": 25, "seconds": 154.5, "bw_gbps": [{f"{s.name}->ct06": 372.0 for s in srcs}]}  # noqa: E731
    with pytest.raises(pytest.fail.Exception, match=r"pre-flight failed - the load used only 0\.21 of ct06's line rate") as e:
        run(monkeypatch, 6, ok, loaded_wm=23100.0, _port_gbps=57600.0)
    assert "congestion not judged" in str(e.value) and "no congestion seen" not in str(e.value)


def test_lossless_drops_fail_lossy_drops_are_only_recorded(monkeypatch):
    ok = lambda srcs, checks: {"rounds": 5, "seconds": 160.0, "bw_gbps": [{f"{s.name}->ct06": 300.0 for s in srcs}]}  # noqa: E731
    props = run(monkeypatch, 6, ok, snaps=[snap(), snap(q7_drop=40.0, pg7_drop=7.0, q0_pkts=1e6)])
    ev = props["incast_evidence"]                                          # PG7 / UC7 (lossy) drops: recorded, PASS
    assert ev["queue_drops"]["lossy"]["total"] == 40.0 and ev["pg_drops_towards_sources"]["lossy"]["total"] == 28.0
    with pytest.raises(pytest.fail.Exception, match=r"3 packet\(s\) dropped on lossless classes during the incast"):
        run(monkeypatch, 6, ok, snaps=[snap(), snap(q0_drop=3.0, q0_pkts=1e6)])


MR3_PG_DROPS = """Ingress PG dropped packets:
       Port    PG0    PG1    PG2    PG3    PG4    PG5    PG6    PG7
-----------  -----  -----  -----  -----  -----  -----  -----  -----
Ethernet257      0      0      0      0      0      0      0  16375
Ethernet258      0      0      0      0      0      0      0  16369
"""
MR3_POOL = """Shared pool maximum occupancy: (Namespace asic1)
                 Pool    Bytes
---------------------  -------
          egress_pool  1243620
ingress_lossless_pool  1990800
"""
MR3_QCOUNTERS = """Last cached time for asic1 was 2026-10-21T00:00:21.385292
       Port    TxQ    Counter/pkts    Counter/bytes    Drop/pkts    Drop/bytes
-----------  -----  --------------  ---------------  -----------  ------------
Ethernet257    UC0      41,122,977   13,945,448,796            0             0
Ethernet257    UC7               5              420            2           168
"""
MR3_PFC = """Last cached time was 2026-10-21T00:00:19.359135
    Port Rx    PFC0    PFC1    PFC2    PFC3    PFC4    PFC5    PFC6    PFC7
-----------  ------  ------  ------  ------  ------  ------  ------  ------
Ethernet257       3       0       0       0       0       0       0       0

    Port Tx    PFC0    PFC1    PFC2    PFC3    PFC4    PFC5    PFC6    PFC7
-----------  ------  ------  ------  ------  ------  ------  ------  ------
Ethernet257       0   1,204       0       0       0       0       0       0
"""


G0217_QCOUNTERS = """Ethernet257 Last cached time for asic1 was 2026-10-20T21:14:54.526519
       Port    TxQ    Counter/pkts    Counter/bytes    Drop/pkts    Drop/bytes
-----------  -----  --------------  ---------------  -----------  ------------
Ethernet257    UC0           1,710          178,024            0             0
Ethernet257    UC1               8              512            0             0

Ethernet258 Last cached time for asic1 was 2026-10-20T21:14:54.526519
       Port    TxQ    Counter/pkts    Counter/bytes    Drop/pkts    Drop/bytes
-----------  -----  --------------  ---------------  -----------  ------------
Ethernet258    UC0           9,000          900,000            7             0
"""


def test_queue_counters_read_every_port_table():
    """g02-g17 21 Oct: one table per port - only Ethernet257 was read before, so the destination's ports were missed."""
    from helios.sonic import SonicSwitch
    sw = SonicSwitch.__new__(SonicSwitch)
    sw.run = lambda cmd, **kw: SimpleNamespace(stdout=G0217_QCOUNTERS, ok=True)
    sw.sh = lambda cmd, **kw: G0217_QCOUNTERS
    assert sw.queue_stats("asic1")[("Ethernet258", "UC0")] == (9000.0, 7.0)
    assert sw.queue_counters("asic1") == {("Ethernet257", "UC0"): 0.0, ("Ethernet257", "UC1"): 0.0,
                                          ("Ethernet258", "UC0"): 7.0}


def test_sonic_counter_parsers_on_mr3_output():
    from helios.sonic import SonicSwitch
    sw = SonicSwitch.__new__(SonicSwitch)
    outs = {"show priority-group drop counters -n asic1": MR3_PG_DROPS, "show buffer_pool watermark -n asic1": MR3_POOL,
            "show queue counters -n asic1": MR3_QCOUNTERS, "show pfc counters -n asic1": MR3_PFC}
    sw.run = lambda cmd, **kw: SimpleNamespace(stdout=outs[cmd], ok=True)
    sw.sh = lambda cmd, **kw: outs[cmd]
    assert sw.pg_drops("asic1")["Ethernet257"]["PG7"] == 16375.0
    assert sw.buffer_pool_watermarks("asic1") == {"egress_pool": 1243620.0, "ingress_lossless_pool": 1990800.0}
    assert sw.queue_stats("asic1")[("Ethernet257", "UC0")] == (41122977.0, 0.0)
    assert sw.queue_stats("asic1")[("Ethernet257", "UC7")] == (5.0, 2.0)
    pfc = sw.pfc_counters_by_prio("asic1")
    assert pfc["Rx"]["Ethernet257"][0] == 3.0 and pfc["Tx"]["Ethernet257"][1] == 1204.0
    assert sw.pfc_counters("asic1") == {"Rx": {"Ethernet257": 3.0}, "Tx": {"Ethernet257": 1204.0}}
