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


def test_link_faults_for_labelled_link_checks():
    """vrack POST /links (helios-slm linkfault.py): each kind lands on the link it names, both ends are reported (the
    label), and clear undoes all of them."""
    import vrack
    from types import SimpleNamespace as NS
    st = State(set(), {"layout": "g02-g17"})
    sim = st.sim
    nodes = [NS(name=f"st{i + 1}", role="switch", down_until=0.0, sessions=[]) for i in range(6)] + \
            [NS(name=f"ct{s + 1:02d}", role="compute", down_until=0.0, sessions=[], t=NS(slot=s)) for s in range(18)]
    r = vrack.apply_link_faults(st, nodes, {"clear": True, "add": [
        {"kind": "cable_out", "tray": "ct02", "gpu": 0, "gpu_port": 25},
        {"kind": "sw_shut", "tray": "ct03", "gpu": 1, "gpu_port": 7},
        {"kind": "gpu_idle", "tray": "ct04", "gpu": 2, "gpu_port": 19},
        {"kind": "ifcp_stuck", "tray": "ct05", "gpu": 3, "gpu_port": 1},
        {"kind": "ifcp_chip", "switch": "st6", "asic": "asic0"},
        {"kind": "mtu_low", "tray": "ct07", "gpu": 0, "gpu_port": 30, "mtu": 1500},
        {"kind": "miscabled", "tray": "ct09", "gpu": 0, "gpu_port": 24, "with": {"tray": "ct10", "gpu": 2}},
        {"kind": "tray_down", "tray": "ct11"}]})["applied"]
    by = {a["kind"]: a for a in r}
    e = by["cable_out"]["links"][0]
    assert (e["switch"], e["port"], e["tray"], e["gpu_port"]) == ("st3", "Ethernet170", "ct02", 25)   # as on g02-g17
    key = (2, "asic0", "Ethernet170")
    assert sim.link_state(*key) == "down" and sim.admin(*key) == "up"
    s = by["sw_shut"]["links"][0]
    assert sim.admin(int(s["switch"][2:]) - 1, s["asic"], s["port"]) == "down"
    assert (3, 2, 19) in sim.cmp_admin_down and (4, 3, 1) in sim.ifcp_stuck
    assert len(by["ifcp_chip"]["links"]) == 216 and all((x["tray"], x["gpu_port"]) for x in by["ifcp_chip"]["links"])
    assert 1500 in sim.mtu.values() and len(by["miscabled"]["links"]) == 2 and len(sim.desc_over) == 2
    assert nodes[6 + 10].down_until == float("inf")
    try:
        vrack.apply_link_faults(st, nodes, {"add": [{"kind": "nope"}]})
        raise AssertionError("an unknown kind must be refused")
    except ValueError:
        pass
    vrack.apply_link_faults(st, nodes, {"clear": True})
    assert not (sim.links_down or sim.cmp_admin_down or sim.ifcp_stuck or sim.mtu or sim.desc_over)
    assert nodes[6 + 10].down_until == 0.0


def test_link_faults_switch_state_and_counters_for_labelled_link_checks():
    """The newer /links kinds (fdb_no_port, vlan_missing / vlan_wrong, errors, coupled, gpu_unprogrammed): the sim
    state they set, what the switch answers helios-debug's own commands with, and clear undoing them."""
    import fake_rack
    import vrack
    from types import SimpleNamespace as NS
    st = State(set(), {"layout": "g02-g17"})
    sim = st.sim
    nodes = [NS(name=f"st{i + 1}", role="switch", down_until=0.0, sessions=[]) for i in range(6)] + \
            [NS(name=f"ct{s + 1:02d}", role="compute", down_until=0.0, sessions=[], t=NS(slot=s)) for s in range(18)]
    r = vrack.apply_link_faults(st, nodes, {"clear": True, "add": [
        {"kind": "fdb_no_port", "switch": "st1", "asic": "asic0", "count": 2},
        {"kind": "fdb_no_port", "switch": "st2", "asic": "asic1", "source": "hardware"},
        {"kind": "vlan_missing", "tray": "ct02", "gpu": 0, "gpu_port": 25},
        {"kind": "vlan_wrong", "tray": "ct03", "gpu": 1, "gpu_port": 25},
        {"kind": "errors", "tray": "ct04", "gpu": 2, "gpu_port": 25, "rx_err": 5},
        {"kind": "coupled", "tray": "ct05", "gpu": 3, "gpu_port": 25},
        {"kind": "gpu_unprogrammed", "tray": "ct06", "gpu": 1},
        {"kind": "tray_down", "tray": "ct07", "power_off": True}]})["applied"]
    by = {a["kind"]: a for a in r if a["kind"] != "fdb_no_port"}
    assert [len(a["entries"]) for a in r if a["kind"] == "fdb_no_port"] == [2, 2]
    assert len(by["coupled"]["links"]) == 2 and len(by["gpu_unprogrammed"]["links"]) == 36 == len(by["tray_down"]["links"]) / 4
    assert all(sim.link_state(int(x["switch"][2:]) - 1, x["asic"], x["port"]) == "down" for x in by["tray_down"]["links"])
    a, b = by["coupled"]["links"]
    ka, kb = [(int(x["switch"][2:]) - 1, x["asic"], x["port"]) for x in (a, b)]
    assert sim.admin(*ka) == "down" and sim.admin(*kb) == "up" and sim.link_state(*kb) == "down"
    assert (5, 1) in sim.unprogrammed
    sw = lambda n: fake_rack.FakeSwitchTransport(NS(name=f"st{n + 1}"), st, n, None)  # noqa: E731
    r1 = sw(0).run("show mac -n asic0")
    assert r1.rc == 1 and "KeyError: 'SAI_FDB_ENTRY_ATTR_BRIDGE_PORT_ID'" in r1.stderr
    hw = sw(0).run("sonic-db-cli -n asic0 ASIC_DB keys 'ASIC_STATE:SAI_OBJECT_TYPE_FDB_ENTRY:*' | while IFS= read -r k; do "
                   "sonic-db-cli -n asic0 ASIC_DB hgetall \"$k\" | grep -q SAI_FDB_ENTRY_ATTR_BRIDGE_PORT_ID || echo \"$k\"; done")
    assert hw.stdout.count('"mac":"00:11:22:33:44:5') == 2
    k = sw(0).run("sonic-db-cli -n asic0 APPL_DB keys 'FDB*00:11:22:33:44:55'").stdout.strip()
    assert k == "FDB_TABLE:Vlan100:00:11:22:33:44:55" and "'port'" in sw(0).run(f"sonic-db-cli -n asic0 APPL_DB hgetall {k}").stdout
    assert sw(1).run("sonic-db-cli -n asic1 APPL_DB keys 'FDB*00:11:22:33:44:55'").stdout == ""   # hardware only
    vl = lambda n, ns: sw(n).run(f"for k in $(sonic-db-cli -n {ns} CONFIG_DB keys 'VLAN|*'); do echo; done; "  # noqa: E731
                                 f"for k in $(sonic-db-cli -n {ns} CONFIG_DB keys 'VLAN_MEMBER|*'); do echo; done").stdout
    x, y = by["vlan_missing"]["links"][0], by["vlan_wrong"]["links"][0]
    v = vl(int(x["switch"][2:]) - 1, x["asic"])
    assert "V Vlan100" in v and f"|{x['port']} " not in v and v.count("M Vlan100|") > 100
    assert f"M Vlan200|{y['port']} priority_tagged" in vl(int(y["switch"][2:]) - 1, y["asic"])
    e = by["errors"]["links"][0]
    t = sw(int(e["switch"][2:]) - 1)
    rows = lambda: {ln.split()[0]: ln.split() for ln in t.run(f"show interfaces counters -n {e['asic']} -d all").stdout.splitlines()[2:]}  # noqa: E731
    r1, r2 = rows(), rows()
    assert int(r2[e["port"]][6]) - int(r1[e["port"]][6]) == 5 and len(r1) > 100
    fl = sw(ka[0]).run(f"for k in $(sonic-db-cli -n {ka[1]} APPL_DB keys 'PORT_TABLE:Ethernet*'); do echo; done").stdout
    assert f"{ka[2]}|" in fl and "'flap_count': '1'" in fl
    vrack.apply_link_faults(st, nodes, {"clear": True})
    assert not (sim.fdb_noport or sim.vlan_over or sim.port_err or sim.unprogrammed or sim.links_down)
    assert sw(0).run("show mac -n asic0").rc == 0
