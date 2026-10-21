"""The virtual rack's models: switch QoS profiles (fake_qos), the data path (fake_fabric) and the Redfish BMCs."""
from __future__ import annotations

import base64
import json
import ssl
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "selftest"))
sys.path.insert(0, str(ROOT))

import fake_qos  # noqa: E402
import fake_redfish  # noqa: E402
from fake_afm import State  # noqa: E402
from helios.qos import lossless_findings  # noqa: E402

FABRIC = lambda p: p.startswith("Ethernet")  # noqa: E731


def test_qos_profiles_judged_by_tc_swh_063():
    ports = ["Ethernet1", "Ethernet2"]
    assert fake_qos.tables("none", ports) == {}
    mr3 = lossless_findings(fake_qos.tables("mr3", ports), FABRIC)[1]
    assert len(mr3) == 6 and any("no PFC watchdog" in f for f in mr3)            # as blantons-mr3-sw, 20 Oct
    assert lossless_findings(fake_qos.tables("lossless", ports), FABRIC)[1] == []
    merged = fake_qos.merge({"PORT": {"Ethernet1": {}}, "PORT_QOS_MAP": {"x": {}}}, fake_qos.tables("mr3", ports))
    assert "PORT" in merged and set(merged["PORT_QOS_MAP"]) == set(ports)


def _job(sim, ranks: dict[int, int], cmd: str) -> list:
    """Run one multi-rank TransferBench job (rank -> slot) the way run_ranks does: every rank at once."""
    out = [None] * len(ranks)

    def one(r, slot):
        out[r] = sim.fabric.transferbench(slot, f"TB_NUM_RANKS={len(ranks)} TB_RANK={r} TB_MASTER_ADDR=10.0.1.1 "
                                                f"TB_MASTER_PORT=47100 TB_SINGLE_LOG=1 {cmd}")
    th = [threading.Thread(target=one, args=(r, s)) for r, s in ranks.items()]
    for t in th:
        t.start()
    for t in th:
        t.join()
    return out


def _incast(qos: str):
    st = State(set(), {"layout": "g02-g17", "qos": qos})
    xfers = " ".join(f"(R{k}G{g}->R{k}G{g}->R0G0)" for k in (1, 2, 3, 4) for g in range(4))
    res = _job(st.sim, {0: 17, 1: 13, 2: 14, 3: 15, 4: 16}, f"TransferBench cmdline '16 32 {xfers}' 1G")
    return st.sim.fabric, res[0]


def test_a2a_pair_moves_g02_g17_bandwidth_without_congestion():
    st = State(set(), {"layout": "g02-g17"})
    rc, out = _job(st.sim, {0: 5, 1: 11}, "TransferBench a2a 1G")[0]
    assert rc == 0 and "Aggregate bandwidth (GPU Timed):  372.000 GB/s" in out
    fab = st.sim.fabric
    assert max(fab.qwm.values()) < 100_000 and not any(d for _p, d in fab.q.values()) and not fab.pfc


def test_one_way_incast_pauses_lossless_and_drops_lossy():
    fab, (rc, out) = _incast("lossless")
    assert rc == 0 and "1800.000 GB/s" in out                    # one GPU's 36 x 50 GB/s, saturated
    assert sum(fab.pfc.values()) > 0 and not any(d for _p, d in fab.q.values())
    assert max(fab.qwm.values()) == 645540.0
    fab, (rc, _out) = _incast("none")
    assert not fab.pfc and sum(d for _p, d in fab.q.values()) > 0


def test_a_job_with_a_missing_rank_fails_like_transferbench():
    st = State(set(), {"switches": 1, "computes": 2})
    import fake_fabric
    fake_fabric.RANK_WAIT_S, old = 0.2, fake_fabric.RANK_WAIT_S
    try:
        rc, out = st.sim.fabric.transferbench(0, "TB_NUM_RANKS=2 TB_RANK=0 TB_MASTER_ADDR=x TransferBench a2a 1G")
    finally:
        fake_fabric.RANK_WAIT_S = old
    assert rc == 1 and "waiting for 1 rank" in out


def test_redfish_bmc_reads_only():
    srv = fake_redfish.RedfishServer(fake_redfish.blanton, "switch", ("127.0.0.1", 0)).start()
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    auth = "Basic " + base64.b64encode(f"root:{fake_redfish.BMC_PW}".encode()).decode()
    try:
        req = urllib.request.Request(srv.url + "/redfish/v1/Managers/bmc", headers={"Authorization": auth})
        assert json.load(urllib.request.urlopen(req, context=ctx))["Id"] == "bmc"
        for method, code in (("POST", 405), ("GET-noauth", 401)):
            r = urllib.request.Request(srv.url + "/redfish/v1/Managers/bmc/Actions/Manager.Reset", data=b"{}",
                                       headers={"Authorization": auth}, method="POST") if method == "POST" else \
                urllib.request.Request(srv.url + "/redfish/v1/")
            try:
                urllib.request.urlopen(r, context=ctx)
                raise AssertionError(f"{method} accepted")
            except urllib.error.HTTPError as e:
                assert e.code == code
    finally:
        srv.stop()
