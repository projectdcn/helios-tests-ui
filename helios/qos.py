"""Lossless (PFC) configuration audit of one SONiC CONFIG_DB (one ASIC namespace), used by TC-SWH-063.

Judges the running CONFIG_DB (``sonic-cfggen -d --print-data``) as a whole, so a whole ASIC is one SSH round trip.
blantons-mr3-sw 20 Oct 2026 (QFX5252, both ASICs): pfc_enable 0,1 on every port, but no PFC watchdog, no WRED / ECN,
dot1p 2-6 unmapped (-> TC0, the lossless IFoE response class), the lossy priority 7 on q_lossless_profile with a
static threshold above the egress pool, and no ingress buffer for PG7.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable


def _ports(spec: str) -> list[str]:
    return [p for p in spec.split(",") if p]


def _ids(spec: str) -> list[int]:
    """'3' / '3-4' / '0,1' -> [3] / [3, 4] / [0, 1]."""
    out: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out += range(int(a), int(b) + 1)
        elif part.strip().isdigit():
            out.append(int(part))
    return out


def _per_port_id(table: dict) -> dict[tuple[str, int], dict]:
    """BUFFER_PG / BUFFER_QUEUE / QUEUE keys ('Ethernet0,Ethernet4|3-4') -> {(port, id): fields}."""
    out = {}
    for key, fields in (table or {}).items():
        if "|" not in key:
            continue
        ports, ids = key.split("|", 1)
        for p in _ports(ports):
            for i in _ids(ids):
                out[(p, i)] = fields or {}
    return out


def _ref(v: str | None) -> str:
    """'[BUFFER_PROFILE|x]' (older SONiC) or 'x' -> 'x'."""
    v = (v or "").strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return v.split("|", 1)[-1]


def _int(v) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def lossless_findings(cfg: dict, is_fabric: Callable[[str], bool], th: dict | None = None) -> tuple[dict, list[str]]:
    """(summary, findings) for one CONFIG_DB. Each finding is one product-level sentence with the affected port count.

    th: pfcwd_required (True), ecn_required (True) - set False where the design deliberately has neither."""
    th = th or {}
    qmaps = {p: m for p, m in (cfg.get("PORT_QOS_MAP") or {}).items() if is_fabric(p)}
    pgs = _per_port_id(cfg.get("BUFFER_PG"))
    bqs = _per_port_id(cfg.get("BUFFER_QUEUE"))
    queues = _per_port_id(cfg.get("QUEUE"))
    profiles = cfg.get("BUFFER_PROFILE") or {}
    pools = cfg.get("BUFFER_POOL") or {}
    wred = cfg.get("WRED_PROFILE") or {}
    pfcwd = {p for k in (cfg.get("PFC_WD") or {}) if k != "GLOBAL" for p in _ports(k)}
    hits: dict[str, set[str]] = defaultdict(set)

    pfc_ports = {}
    for port, m in qmaps.items():
        prios = _ids(m.get("pfc_enable", ""))
        if not prios:
            continue
        pfc_ports[port] = prios
        dot1p = (cfg.get("DOT1P_TO_TC_MAP") or {}).get(_ref(m.get("dot1p_to_tc_map"))) if m.get("dot1p_to_tc_map") else None
        tc_pg = (cfg.get("TC_TO_PRIORITY_GROUP_MAP") or {}).get(_ref(m.get("tc_to_pg_map"))) or {}
        tc_q = (cfg.get("TC_TO_QUEUE_MAP") or {}).get(_ref(m.get("tc_to_queue_map"))) or {}
        if dot1p is not None:
            missing = [str(i) for i in range(8) if str(i) not in dot1p]
            if missing:
                msg = f"dot1p {','.join(missing)} not mapped in {_ref(m.get('dot1p_to_tc_map'))}"
                if 0 in prios:
                    msg += f": those frames fall back to TC0, which is lossless (pfc_enable {m.get('pfc_enable')})"
                hits[msg].add(port)
        tc_of = {int(k): _int(v) for k, v in (dot1p or {str(p): str(p) for p in prios}).items() if str(k).isdigit()}
        lossless_tcs = {tc_of.get(p, p) for p in prios}
        lossless_q = {_int(tc_q.get(str(tc), tc)) for tc in lossless_tcs}
        for p in prios:
            pg = _int(tc_pg.get(str(tc_of.get(p, p)), tc_of.get(p, p)))
            prof = profiles.get(_ref((pgs.get((port, pg)) or {}).get("profile")))
            if not prof or _int(prof.get("xoff")) in (None, 0):
                hits[f"lossless priority {p} (PG{pg}) has no lossless ingress buffer (BUFFER_PG with xoff headroom): "
                     f"PFC cannot hold the traffic"].add(port)
        for tc in sorted({_int(v) for v in tc_of.values()} - lossless_tcs - {None}):
            pg = _int(tc_pg.get(str(tc), tc))
            if (port, pg) not in pgs:
                hits[f"lossy TC{tc} -> PG{pg} has no ingress buffer (no BUFFER_PG <port>|{pg}, no lossy pool): "
                     f"it relies on the ASIC default"].add(port)
            q = _int(tc_q.get(str(tc), tc))
            qp = _ref((bqs.get((port, q)) or {}).get("profile"))
            if "lossless" in qp:
                hits[f"lossy TC{tc} uses egress queue {q} with {qp} (a lossless profile) although priority {tc} "
                     f"has no PFC: it can take lossless buffer and is not paused"].add(port)
        if th.get("ecn_required", True):
            no_ecn = [q for q in sorted(lossless_q - {None})
                      if (w := wred.get(_ref((queues.get((port, q)) or {}).get("wred_profile")))) is None
                      or w.get("ecn", "ecn_none") == "ecn_none"]
            if no_ecn:
                hits[f"no ECN marking (WRED_PROFILE with ecn) on lossless queue(s) {','.join(map(str, no_ecn))}: "
                     f"no DCQCN / CNP, congestion is handled by PFC pause alone"].add(port)
        if th.get("pfcwd_required", True) and port not in pfcwd:
            hits["no PFC watchdog (PFC_WD) on a port with lossless priorities: a PFC storm or deadlock is not "
                 "detected or broken"].add(port)

    for name, prof in profiles.items():
        st, pool = _int(prof.get("static_th")), pools.get(_ref(prof.get("pool"))) or {}
        if st is not None and _int(pool.get("size")) and st > _int(pool.get("size")):
            hits[f"BUFFER_PROFILE {name}: static_th {st} B is above the {_ref(prof.get('pool'))} size "
                 f"{pool.get('size')} B - no per-queue limit"].add("*")
    summary = {"ports_with_pfc": len(pfc_ports),
               "pfc_enable": sorted({",".join(map(str, v)) for v in pfc_ports.values()}),
               "pfc_watchdog_ports": len(pfcwd & set(pfc_ports)), "wred_profiles": sorted(wred),
               "dot1p_maps": cfg.get("DOT1P_TO_TC_MAP") or {}}
    findings = [f"{msg} ({'pool-wide' if ports == {'*'} else f'{len(ports)} port(s), e.g. {sorted(ports)[0]}'})"
                for msg, ports in sorted(hits.items())]
    return summary, findings
