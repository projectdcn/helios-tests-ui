"""Switch QoS / PFC configuration profiles for the simulated switches (CONFIG_DB tables per ASIC).

- ``none``     : no QoS at all (no PORT_QOS_MAP, buffers or pools) - g02-g17 as read on 21 Oct 2026.
- ``mr3``      : blantons-mr3-sw as captured 20 Oct 2026 (QFX5252, both ASICs): pfc_enable 0,1 on every port,
                 IFOE maps for dot1p 0 / 1 / 7 only, lossless PG0 / PG1 (xoff 645540), queue 7 on q_lossless_profile
                 with strict priority, static_th above the egress pool, no PFC watchdog, no WRED / ECN.
- ``lossless`` : the same classes, complete: every dot1p mapped, ECN (WRED) on the lossless queues, a PFC watchdog
                 on every port, a lossy PG7 / queue 7, thresholds within the pools - TC-SWH-063 passes on it.
"""
from __future__ import annotations

import copy

PROFILES = ("none", "mr3", "lossless")
QOS_TABLES = ("PORT_QOS_MAP", "DOT1P_TO_TC_MAP", "TC_TO_PRIORITY_GROUP_MAP", "TC_TO_QUEUE_MAP", "BUFFER_POOL",
              "BUFFER_PROFILE", "BUFFER_PG", "BUFFER_QUEUE", "QUEUE", "SCHEDULER", "WRED_PROFILE", "PFC_WD",
              "DSCP_TO_TC_MAP", "MAP_PFC_PRIORITY_TO_QUEUE", "PFC_PRIORITY_TO_PRIORITY_GROUP_MAP")
POOL = "259953960"


def tables(profile: str, ports: list[str]) -> dict[str, dict]:
    """CONFIG_DB QoS tables of one ASIC for ``ports`` (the cabled fabric ports)."""
    if profile in (None, "", "none"):
        return {}
    if profile not in PROFILES:
        raise ValueError(f"unknown qos profile {profile!r} (one of {', '.join(PROFILES)})")
    t: dict[str, dict] = {
        "DOT1P_TO_TC_MAP": {"IFOE_TC_MAP": {"0": "0", "1": "1", "7": "7"}},
        "TC_TO_PRIORITY_GROUP_MAP": {"IFOE_PG_MAP": {"0": "0", "1": "1", "7": "7"}},
        "TC_TO_QUEUE_MAP": {"IFOE_QUEUE_MAP": {"0": "0", "1": "1", "7": "7"}},
        "BUFFER_POOL": {"egress_pool": {"mode": "static", "size": POOL, "type": "egress"},
                        "ingress_lossless_pool": {"mode": "dynamic", "size": POOL, "type": "ingress", "xoff": "39352320"}},
        "BUFFER_PROFILE": {
            "pg_lossless_profile": {"dynamic_th": "0", "pool": "ingress_lossless_pool", "size": "10080",
                                    "threshold_mode": "dynamic", "xoff": "645540", "xon": "0", "xon_offset": "3360"},
            "q_lossless_profile": {"pool": "egress_pool", "size": "0", "static_th": "265332900", "threshold_mode": "static"},
            "q_lossy_profile": {"dynamic_th": "0", "pool": "egress_pool", "size": "0", "threshold_mode": "dynamic"}},
        "SCHEDULER": {"DWRR_REQ": {"type": "DWRR", "weight": "10"}, "DWRR_RESP": {"type": "DWRR", "weight": "90"},
                      "SP": {"type": "STRICT"}},
        "PORT_QOS_MAP": {}, "BUFFER_PG": {}, "BUFFER_QUEUE": {}, "QUEUE": {},
    }
    for p in ports:
        t["PORT_QOS_MAP"][p] = {"dot1p_to_tc_map": "IFOE_TC_MAP", "tc_to_pg_map": "IFOE_PG_MAP",
                                "tc_to_queue_map": "IFOE_QUEUE_MAP", "pfc_enable": "0,1"}
        t["BUFFER_PG"].update({f"{p}|0": {"profile": "pg_lossless_profile"}, f"{p}|1": {"profile": "pg_lossless_profile"}})
        t["BUFFER_QUEUE"].update({f"{p}|{q}": {"profile": "q_lossless_profile" if q in (0, 1, 7) else "q_lossy_profile"}
                                  for q in range(8)})
        t["QUEUE"].update({f"{p}|0": {"scheduler": "DWRR_RESP"}, f"{p}|1": {"scheduler": "DWRR_REQ"},
                           f"{p}|7": {"scheduler": "SP"}})
    if profile == "lossless":
        t["DOT1P_TO_TC_MAP"]["IFOE_TC_MAP"] = {str(d): ("0" if d == 0 else "1" if d == 1 else "7") for d in range(8)}
        t["BUFFER_PROFILE"]["q_lossless_profile"]["static_th"] = "200000000"
        t["BUFFER_PROFILE"]["pg_lossy_profile"] = {"pool": "ingress_lossless_pool", "size": "0", "dynamic_th": "3"}
        t["WRED_PROFILE"] = {"IFOE_LOSSLESS": {"ecn": "ecn_all", "wred_green_enable": "true", "green_min_threshold":
                                               "1048576", "green_max_threshold": "2097152", "green_drop_probability": "5"}}
        t["PFC_WD"] = {"GLOBAL": {"POLL_INTERVAL": "200"}}
        for p in ports:
            t["BUFFER_PG"][f"{p}|7"] = {"profile": "pg_lossy_profile"}
            t["BUFFER_QUEUE"][f"{p}|7"] = {"profile": "q_lossy_profile"}
            for q in (0, 1):
                t["QUEUE"][f"{p}|{q}"]["wred_profile"] = "IFOE_LOSSLESS"
            t["PFC_WD"][p] = {"action": "drop", "detection_time": "200", "restoration_time": "200"}
    return t


def merge(config: dict, qos: dict) -> dict:
    """A CONFIG_DB dump with its QoS tables replaced by ``qos`` (recorded dumps carry the recording switch's)."""
    out = {k: v for k, v in config.items() if k not in QOS_TABLES}
    out.update(copy.deepcopy(qos))
    return out


def lossless(profile: str) -> dict:
    """{'prios', 'pgs', 'queues'} of the profile's lossless classes (empty for none)."""
    if profile in (None, "", "none"):
        return {"prios": [], "pgs": [], "queues": []}
    return {"prios": [0, 1], "pgs": ["PG0", "PG1"], "queues": ["UC0", "UC1"]}
