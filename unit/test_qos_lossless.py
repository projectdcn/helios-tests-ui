"""TC-SWH-063 audit (helios/qos.py): the blantons-mr3-sw 20 Oct 2026 lossless config is caught, a complete one passes."""
from __future__ import annotations

import re

from helios.qos import lossless_findings

FABRIC = re.compile(r"Ethernet\d+$").match


def mr3_like(ports=("Ethernet257", "Ethernet385")) -> dict:
    cfg = {
        "PORT_QOS_MAP": {p: {"dot1p_to_tc_map": "IFOE_TC_MAP", "tc_to_pg_map": "IFOE_PG_MAP",
                             "tc_to_queue_map": "IFOE_QUEUE_MAP", "pfc_enable": "0,1"} for p in ports},
        "DOT1P_TO_TC_MAP": {"IFOE_TC_MAP": {"0": "0", "1": "1", "7": "7"}},
        "TC_TO_PRIORITY_GROUP_MAP": {"IFOE_PG_MAP": {"0": "0", "1": "1", "7": "7"}},
        "TC_TO_QUEUE_MAP": {"IFOE_QUEUE_MAP": {"0": "0", "1": "1", "7": "7"}},
        "BUFFER_POOL": {"egress_pool": {"mode": "static", "size": "259953960", "type": "egress"},
                        "ingress_lossless_pool": {"mode": "dynamic", "size": "259953960", "type": "ingress",
                                                  "xoff": "39352320"}},
        "BUFFER_PROFILE": {"pg_lossless_profile": {"pool": "ingress_lossless_pool", "xoff": "645540", "size": "10080",
                                                   "dynamic_th": "0", "xon": "0", "xon_offset": "3360"},
                           "q_lossless_profile": {"pool": "egress_pool", "size": "0", "static_th": "265332900",
                                                  "threshold_mode": "static"},
                           "q_lossy_profile": {"pool": "egress_pool", "size": "0", "dynamic_th": "0"}},
        "BUFFER_PG": {}, "BUFFER_QUEUE": {}, "QUEUE": {},
    }
    for p in ports:
        cfg["BUFFER_PG"].update({f"{p}|0": {"profile": "pg_lossless_profile"}, f"{p}|1": {"profile": "pg_lossless_profile"}})
        cfg["BUFFER_QUEUE"].update({f"{p}|{q}": {"profile": "q_lossless_profile" if q in (0, 1, 7) else "q_lossy_profile"}
                                    for q in range(8)})
        cfg["QUEUE"].update({f"{p}|0": {"scheduler": "DWRR_RESP"}, f"{p}|1": {"scheduler": "DWRR_REQ"},
                             f"{p}|7": {"scheduler": "SP"}})
    return cfg


def test_mr3_20_oct_config_is_caught():
    s, f = lossless_findings(mr3_like(), FABRIC)
    text = "\n".join(f)
    assert s["ports_with_pfc"] == 2 and s["pfc_watchdog_ports"] == 0 and s["wred_profiles"] == []
    assert "no PFC watchdog" in text and "no ECN marking" in text and "queue(s) 0,1" in text
    assert "dot1p 2,3,4,5,6 not mapped" in text and "fall back to TC0, which is lossless" in text
    assert "lossy TC7 uses egress queue 7 with q_lossless_profile" in text
    assert "lossy TC7 -> PG7 has no ingress buffer" in text
    assert "static_th 265332900 B is above the egress_pool" in text
    assert all("(2 port(s)" in x or "(pool-wide)" in x for x in f)       # one line per issue, not per port
    assert len(f) == 6


def test_complete_config_passes():
    cfg = mr3_like()
    cfg["DOT1P_TO_TC_MAP"]["IFOE_TC_MAP"] = {str(i): ("0" if i == 0 else "1" if i == 1 else "7") for i in range(8)}
    cfg["BUFFER_PROFILE"]["q_lossless_profile"]["static_th"] = "200000000"
    cfg["BUFFER_PROFILE"]["pg_lossy_profile"] = {"pool": "ingress_lossless_pool", "size": "0", "dynamic_th": "3"}
    cfg["WRED_PROFILE"] = {"AZURE_LOSSLESS": {"ecn": "ecn_all", "wred_green_enable": "true"}}
    cfg["PFC_WD"] = {"GLOBAL": {"POLL_INTERVAL": "200"},
                     "Ethernet257,Ethernet385": {"action": "drop", "detection_time": "200", "restoration_time": "200"}}
    for p in ("Ethernet257", "Ethernet385"):
        cfg["BUFFER_PG"][f"{p}|7"] = {"profile": "pg_lossy_profile"}
        cfg["BUFFER_QUEUE"][f"{p}|7"] = {"profile": "q_lossy_profile"}
        for q in (0, 1):
            cfg["QUEUE"][f"{p}|{q}"]["wred_profile"] = "AZURE_LOSSLESS"
    s, f = lossless_findings(cfg, FABRIC)
    assert f == [] and s["pfc_watchdog_ports"] == 2


def test_design_without_watchdog_or_ecn_can_be_waived_and_ranges_parse():
    cfg = mr3_like(ports=())
    cfg["PORT_QOS_MAP"] = {"Ethernet0": {"pfc_enable": "3,4"}, "Ethernet4": {"pfc_enable": "3,4"}}
    cfg["BUFFER_PG"] = {"Ethernet0,Ethernet4|3-4": {"profile": "[BUFFER_PROFILE|pg_lossless_profile]"}}
    cfg["BUFFER_PROFILE"]["q_lossless_profile"]["static_th"] = "1"
    s, f = lossless_findings(cfg, FABRIC, {"pfcwd_required": False, "ecn_required": False})
    assert s["pfc_enable"] == ["3,4"] and f == []
