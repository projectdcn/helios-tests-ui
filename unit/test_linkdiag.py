"""helios.linkdiag: joining switch ports to GPU ports by MAC / description, and one root cause per problem link."""
from __future__ import annotations

import copy

from helios import linkdiag as ld

MAC = "02:00:00:02:03:19"


def _switch(oper="up", admin="up", fdb=True, desc="To ComputeTray Slot2/GPU3[11]/Port25", fec=None, fec2=None):
    a = {"status": {"Ethernet25": {"Interface": "Ethernet25", "Oper": oper, "Admin": admin}},
         "desc": {"Ethernet25": desc}, "fdb": [{"Vlan": "100", "MacAddress": MAC, "Port": "Ethernet25", "Type": "Static"}] if fdb else [],
         "counters": {"Ethernet25": {"RX_ERR": "0"}}, "fec": {"Ethernet25": fec or {"FEC_UNCORR": "10"}}, "presence": {"Ethernet25": "Not present"}}
    return {"name": "st3", "asics": {"asic0": a}, "error": None}


def _tray(link="LINK_UP", admin="enabled", up_ms=1000, ll_up="yes", peer_enabled="yes", error=None, slot=None):
    port = {"port": 25, "station": 12, "mac": MAC, "admin": admin, "link": link, "fault": "no" if link == "LINK_UP" else "yes",
            "up_ms": up_ms, "down_ms": 5000, "fec_mode": "rs-int", "loopback": "none",
            "ifcp": {"ll_up": ll_up, "peer_enabled": peer_enabled, "in_error": "no", "peer_in_error": "no"}}
    return {"name": "ct03", "host": "10.0.0.3", "slot": slot, "error": error,
            "gpus": [{"bdf": "0001:01:00.1", "accelerator": "8", "config_phase": "ACTIVE", "ports": [port]}]}


def _one(sw, tr, quarantined=None, deltas=None):
    links, learned = ld.join([sw], [tr] if tr else [], quarantined)
    assert len(links) == 1
    ln = links[0]
    ln.deltas = deltas or {}
    return ld.diagnose(ln, {t["name"]: t for t in ([tr] if tr else [])}, False, 30), learned


def test_healthy_link_joined_by_mac_and_slot_learned():
    ln, learned = _one(_switch(), _tray())
    assert ln.state == "ok" and ln.matched_by == "mac" and ln.tray == "ct03" and ln.gpu_port == 25
    assert learned["slots"] == {"ct03": 2} and learned["gpu_map"] == {"ct03:GPU3": "0001:01:00.1"}


def test_down_link_without_fdb_is_joined_by_description_once_learned():
    # a second, healthy port of the same GPU teaches slot + GPU numbering; the down port has no FDB entry
    sw, tr = _switch(), _tray()
    sw["asics"]["asic0"]["status"]["Ethernet26"] = {"Oper": "down", "Admin": "up"}
    sw["asics"]["asic0"]["desc"]["Ethernet26"] = "To ComputeTray Slot2/GPU3[11]/Port26"
    p26 = copy.deepcopy(tr["gpus"][0]["ports"][0]) | {"port": 26, "mac": "02:00:00:02:03:1a", "link": "NO_PHY_LINK, PCS_NO_BLOCK_LOCK", "up_ms": 0}
    tr["gpus"][0]["ports"].append(p26)
    links, _ = ld.join([sw], [tr])
    down = next(x for x in links if x.sw_port == "Ethernet26")
    assert down.matched_by == "description" and down.bdf == "0001:01:00.1" and down.gpu_port == 26
    ld.diagnose(down, {"ct03": tr}, False, 30)
    assert down.state == "down" and down.kind == "phys_never_up"


def test_never_up_and_went_down():
    assert _one(_switch(oper="down"), _tray(link="NO_PHY_LINK", up_ms=0))[0].kind == "phys_never_up"
    ln = _one(_switch(oper="down"), _tray(link="NO_PHY_LINK", up_ms=12345))[0]
    assert ln.kind == "phys_went_down" and "was up" in ln.cause


def test_admin_states_come_first():
    assert _one(_switch(oper="down", admin="down"), _tray(link="NO_PHY_LINK"))[0].kind == "sw_admin_down"
    ln = _one(_switch(oper="down"), _tray(link="NO_PHY_LINK", admin="idle"))[0]
    assert ln.kind == "gpu_admin" and "--admin-state enable" in ln.action and "-s 12 -p 25" in ln.action


def test_one_sided_bit_errors_ifcp_not_programmed():
    assert _one(_switch(oper="up"), _tray(link="NO_PHY_LINK"))[0].kind == "one_sided"
    assert _one(_switch(), _tray(), deltas={"FEC_UNCORR": 4})[0].kind == "bit_errors"
    assert _one(_switch(), _tray(), deltas={"FEC_CORR": 900})[0].state == "ok"      # corrected errors alone: fine
    assert _one(_switch(), _tray(ll_up="no"))[0].kind == "ifcp"
    # no FDB entry: matched by description only when the tray was learned from another link, else no tray
    sw = _switch(fdb=False)
    ln = _one(sw, _tray(slot=2))[0]
    assert ln.kind == "not_programmed"


def test_tray_unreachable_and_quarantined():
    tr = _tray(error="TimeoutError: ssh", slot=2)
    tr["gpus"] = []
    assert _one(_switch(oper="down", fdb=False), tr)[0].kind == "tray_unreachable"
    ln = _one(_switch(oper="down", fdb=False), None, quarantined={2: ("ct05", "Down")})[0]
    assert ln.kind == "tray_quarantined" and "ct05" in ln.cause
    g = ld.group_causes([ln])
    assert g[0]["cause"].endswith(": ct05") and g[0]["count"] == 1


def test_vpod_change_in_progress_is_not_a_fault():
    links, _ = ld.join([_switch(fdb=False)], [_tray(slot=2)])
    ln = links[0]
    ln.fdb_static_late = True                     # the second read found the entry
    assert ld.diagnose(ln, {"ct03": _tray(slot=2)}, False, 30).state == "ok"
    links, _ = ld.join([_switch(fdb=False)], [_tray(slot=2)])
    ln = links[0]
    ln.fdb_static_late, ln.vpod_accelerators = False, 0   # GPU in no vPOD: nothing to program
    assert ld.diagnose(ln, {"ct03": _tray(slot=2)}, False, 30).state == "ok"


def test_ifcp_peer_check_learns_mapping_and_flags_a_miscabled_port():
    tr = _tray()
    tr["gpus"][0]["ports"][0]["ifcp"] |= {"peer_device_id": "3", "peer_port_id": "25"}
    links, _ = ld.join([_switch()], [tr])
    m = ld.learn_ifcp(links)
    assert m == {"device": {"st3": "3"}, "port_is_ethernet": True, "checked_links": 1}
    assert ld.diagnose(links[0], {"ct03": tr}, False, 30, m).state == "ok"
    tr2 = copy.deepcopy(tr)
    tr2["gpus"][0]["ports"][0]["ifcp"]["peer_port_id"] = "29"          # the cable really lands on Ethernet29
    links2, _ = ld.join([_switch()], [tr2])
    ln = ld.diagnose(links2[0], {"ct03": tr2}, False, 30, m)
    assert ln.kind == "miscabled" and "st3 Ethernet29" in ln.cause


def test_ping_map_tells_sticky_from_moving_loss():
    from types import SimpleNamespace
    from helios.afmctl import Device, PingResult, Port
    calls = {"n": 0}

    class FakeAfmctl:
        def __init__(self, tray):
            self.t = tray.host.name

        def vpod_accels(self):
            base = {"ct01": 0, "ct02": 4}[self.t]
            return {"0001:01:00.1": (str(base), set())}

        def devices(self):
            return [Device("0001:01:00.1", "x", "ACTIVE")]

        def ports(self, bdf):
            return [Port(p, "enabled", "LINK_UP") for p in (0, 1, 2)]

        def ping(self, bdf, acc, ports):
            calls["n"] += 1
            res = PingResult(port_checks={"1": ["req"]})            # port 1 always fails (sticky)
            if self.t == "ct02" and calls["n"] <= 2:
                res.port_checks["2"] = ["rsp"]                      # port 2 fails in the first round only (moving)
            return res, SimpleNamespace(stdout="")
    trays = [SimpleNamespace(host=SimpleNamespace(name=n)) for n in ("ct01", "ct02")]
    pm = ld.ping_map(trays, rounds=2, wait_s=0, afmctl=FakeAfmctl, log=lambda *a: None, sleep=lambda s: None)
    assert pm["ports"][1]["fails"] == 4 and pm["ports"][0]["fails"] == 0
    text = " ".join(ld.judge_ping_map(pm, {0: "st6 asic1", 1: "st5 asic1", 2: "st6 asic1"}))
    assert "Sticky" in text and "port 1" in text and "Moving" in text and "st5 asic1" in text


def test_compare_lists_new_recovered_and_changed():
    prev = [{"switch": "st3", "asic": "asic0", "sw_port": "Ethernet25", "state": "down", "kind": "phys_went_down", "cause": "x"},
            {"switch": "st3", "asic": "asic0", "sw_port": "Ethernet26", "state": "ok", "kind": "", "cause": ""}]
    a = ld.Link("st3", "asic0", "Ethernet25", tray="ct03", gpu_port=25, state="ok")
    b = ld.Link("st3", "asic0", "Ethernet26", tray="ct03", gpu_port=26, state="down", kind="phys_went_down", cause="no link")
    d = ld.compare(prev, [a, b])
    assert len(d["recovered"]) == 1 and "Ethernet25" in d["recovered"][0]
    assert len(d["new_problems"]) == 1 and "Ethernet26" in d["new_problems"][0]


def test_suspects_point_at_a_gpu_with_several_down_links():
    mk = lambda sw, port: ld.Link(sw, "asic0", f"Ethernet{port}", tray="ct17", bdf="0002:01:00.1", gpu_port=port,  # noqa: E731
                                  state="down", kind="phys_went_down")
    s = ld.suspects([mk("st4", 24), mk("st4", 28), mk("st3", 25), ld.Link("st1", "asic0", "Ethernet1", state="ok")])
    assert "ct17 0002:01:00.1: 3 problem links" in s[0] and "GPU / tray side" in s[0]
    assert any("st4 asic0: 2" in x for x in s) is False          # 2 links on one ASIC is not yet a pattern


def test_suspects_same_gpu_position_on_many_trays_and_adjacent_ports():
    mk = lambda tray, sw, port, gp: ld.Link(sw, "asic0", f"Ethernet{port}", tray=tray, bdf="0002:01:00.1",  # noqa: E731
                                            gpu_port=gp, state="down", kind="phys_went_down")
    s = ld.suspects([mk("ct17", "st4", 70, 24), mk("ct18", "st4", 66, 28), mk("ct17", "st4", 69, 28), mk("ct13", "st6", 261, 0)])
    text = " | ".join(s)
    assert "GPU position 0002:01:00.1: problem links on 3 trays" in text and "shared by that GPU position" in text
    assert "st4 asic0: 3 problem links (Ethernet66, Ethernet69, Ethernet70) on adjacent ports" in text
