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
