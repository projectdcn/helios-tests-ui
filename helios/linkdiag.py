"""Scale-up link diagnosis: collect both ends of every GPU link and name the root cause of each link that is down
or degraded. Rack-agnostic: works from the inventory's switches and compute trays only; which switch port belongs to
which GPU port is learned from the rack itself (the GPU port's own MAC, `afmctl show port` spec.nw_mac_addr, in the
switches' forwarding tables), with the switch port descriptions as the fallback for links AFM has not programmed.

Used by tools/link_check.py (`scan` and `path`). Read-only: SSH + `afmctl show`, `show interfaces`, `show mac`.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from .afmctl import Afmctl, parse_port_description
from .parsers import parse_table
from .rack import fan_out

COUNTERS = ("RX_ERR", "RX_DRP", "TX_ERR", "TX_DRP")
FEC = ("FEC_CORR", "FEC_UNCORR", "FEC_SYMBOL_ERR")

# owners: who acts on a cause
OPERATOR, SITE, SWITCH_VENDOR, GPU_VENDOR = "operator", "site (cabling)", "switch vendor", "GPU / AFM vendor"

# root-cause kinds: grouping key and the one-line summary of a group
KINDS = {
    "tray_unreachable": "compute tray not reachable",
    "tray_quarantined": "compute tray quarantined in the inventory",
    "tray_missing": "no compute tray in the inventory for this slot",
    "gpu_missing": "GPU port not found on the tray",
    "sw_admin_down": "switch port administratively down",
    "gpu_admin": "GPU port administratively disabled",
    "phys_never_up": "no physical link on either end, never up since the tray booted",
    "phys_went_down": "no physical link on either end, went down after being up",
    "phys_no_optic": "no transceiver / cable detected in the switch port",
    "one_sided": "one-sided link: the two ends disagree",
    "bit_errors": "bit errors growing on a link that is up",
    "ifcp": "physical link up but no IFCP session",
    "not_programmed": "link up but not programmed by AFM",
}


def num(v) -> int | None:
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def ns_flag(ns) -> str:
    return f" -n {ns}" if ns else ""


# ---------------------------------------------------------------------------------------------- collection

def collect_switch(sw) -> dict:
    """Per ASIC namespace: port state, descriptions, FDB, counters, FEC; whether the platform reports optics."""
    out = {"name": sw.host.name, "asics": {}, "error": None}
    try:
        for ns in sw.namespaces():
            key = ns or ""
            status = {r.get("Interface"): r for r in parse_table(sw.sh(f"show interfaces status{ns_flag(ns)} -d all"))}
            desc = sw.port_descriptions(ns)
            fdb = parse_table(sw.sh(f"show mac{ns_flag(ns)}", sudo=True, timeout=180))
            cnt = {r.get("IFACE"): r for r in parse_table(sw.sh(f"show interfaces counters{ns_flag(ns)} -d all", timeout=180))}
            fec = {r.get("IFACE"): r for r in parse_table(sw.sh(f"show interfaces counters fec-stats{ns_flag(ns)}", timeout=180))}
            pres = {r.get("Port"): r.get("Presence", "") for r in
                    parse_table(sw.run(f"show interfaces transceiver presence{ns_flag(ns)}", sudo=True, timeout=180).stdout)}
            out["asics"][key] = {"status": status, "desc": desc, "fdb": fdb, "counters": cnt, "fec": fec, "presence": pres}
    except Exception as e:  # noqa: BLE001 - an unreachable switch is reported, not fatal
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def sample_counters(sw, asics: list[str]) -> dict:
    out = {}
    for ns in asics:
        n = ns or None
        cnt = {r.get("IFACE"): r for r in parse_table(sw.sh(f"show interfaces counters{ns_flag(n)} -d all", timeout=180))}
        fec = {r.get("IFACE"): r for r in parse_table(sw.sh(f"show interfaces counters fec-stats{ns_flag(n)}", timeout=180))}
        fdb = parse_table(sw.sh(f"show mac{ns_flag(n)}", sudo=True, timeout=180))
        out[ns] = {"counters": cnt, "fec": fec, "fdb": fdb}
    return out


def collect_tray(tray) -> dict:
    """Every GPU port of the tray: own MAC, admin, link status, fault, up/down durations, IFCP."""
    out = {"name": tray.host.name, "host": tray.host.host, "slot": (tray.host.extra or {}).get("slot"),
           "gpus": [], "error": None}
    try:
        a = Afmctl(tray)
        accels = a.vpod_accels()
        for d in a.devices():
            ports = []
            for p in a.ports_raw(d.bdf):
                sp, st = p.get("spec") or {}, p.get("status") or {}
                i = st.get("ifcp") or {}
                ports.append({"port": int(p.get("id", p.get("port_id"))), "station": num(sp.get("station_id")),
                              "mac": str(sp.get("nw_mac_addr", "")).lower(), "admin": sp.get("admin_state"),
                              "link": st.get("link_status"), "fault": st.get("fault"),
                              "up_ms": num(st.get("link_up_duration")), "down_ms": num(st.get("link_down_duration")),
                              "fec_mode": sp.get("fec_mode"), "loopback": sp.get("loopback_mode"),
                              "ifcp": {k: i.get(k) for k in ("link_up", "ll_up", "in_error", "peer_enabled",
                                                              "peer_in_error", "peer_device_id", "peer_port_id")}})
            acc, reach = accels.get(d.bdf, ("", set()))
            out["gpus"].append({"bdf": d.bdf, "accelerator": acc, "vpod_accelerators": len(reach),
                                "config_phase": d.config_phase, "ports": sorted(ports, key=lambda x: x["port"])})
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


# ---------------------------------------------------------------------------------------------- joining

@dataclass
class Link:
    switch: str
    asic: str
    sw_port: str
    desc: str = ""
    sw_oper: str | None = None
    sw_admin: str | None = None
    tray: str | None = None
    bdf: str | None = None
    gpu_port: int | None = None
    station: int | None = None
    gpu: dict | None = None             # the tray-side port record
    matched_by: str = ""                # "mac" | "description"
    fdb_static: bool = False
    fdb_static_late: bool | None = None  # the second read of the forwarding table (None = not re-read)
    vpod_accelerators: int | None = None
    deltas: dict = field(default_factory=dict)
    fec_total: dict = field(default_factory=dict)
    presence: str = ""
    state: str = "ok"                   # ok | degraded | down
    kind: str = ""                      # KINDS key
    cause: str = ""
    evidence: list = field(default_factory=list)
    owner: str = ""
    action: str = ""


def join(switches: list[dict], trays: list[dict], quarantined: dict | None = None) -> tuple[list[Link], dict]:
    """One Link per switch port described as cabled to a compute tray, joined to its GPU port. Returns the links and
    what was learned: {tray: slot}, {(tray, desc gpu): bdf}."""
    by_mac = {}
    for t in trays:
        for g in t["gpus"]:
            for p in g["ports"]:
                if p["mac"]:
                    by_mac[p["mac"]] = (t, g, p)
    links, slot_votes, gpu_map, matched = [], {}, {}, set()
    for s in switches:
        for ns, a in s["asics"].items():
            static = {}
            for r in a["fdb"]:
                if str(r.get("Type", "")).lower() == "static":
                    static.setdefault(r.get("Port"), set()).add(str(r.get("MacAddress", "")).lower())
            for port, desc in a["desc"].items():
                cab = parse_port_description(desc)
                if not cab:
                    continue
                st = a["status"].get(port, {})
                ln = Link(s["name"], ns, port, desc, st.get("Oper"), st.get("Admin"), presence=a["presence"].get(port, ""))
                ln.fdb_static = bool(static.get(port))
                for mac in static.get(port, ()):
                    hit = by_mac.get(mac)
                    if hit and hit[2]["port"] == cab[3]:
                        t, g, p = hit
                        ln.tray, ln.bdf, ln.gpu_port, ln.station, ln.gpu, ln.matched_by = t["name"], g["bdf"], p["port"], p["station"], p, "mac"
                        matched.add((t["name"], g["bdf"], p["port"]))
                        slot_votes.setdefault(t["name"], {}).setdefault(cab[0], 0)
                        slot_votes[t["name"]][cab[0]] += 1
                        gpu_map[(t["name"], cab[1])] = g["bdf"]
                        break
                ln._cab = cab
                links.append(ln)
    slots = {t: max(v, key=v.get) for t, v in slot_votes.items()}
    for t in trays:                                   # inventory slot wins when given
        if t.get("slot") is not None:
            slots[t["name"]] = int(t["slot"])
    by_slot = {v: k for k, v in slots.items()}
    tray_by_name = {t["name"]: t for t in trays}
    for ln in links:                                  # fallback: description + what the healthy links taught
        if ln.tray:
            continue
        slot, dgpu, _, port = ln._cab
        tname = by_slot.get(slot)
        if not tname:
            if quarantined and slot in quarantined:
                ln._quarantined = quarantined[slot]
            continue
        ln.tray = tname
        bdf, how = gpu_map.get((tname, dgpu)), "description"
        t = tray_by_name.get(tname)
        if bdf is None:
            # by elimination: port p exists once per GPU; the copy no switch port claimed by MAC is this one
            left = [g["bdf"] for g in (t or {}).get("gpus", []) for q in g["ports"]
                    if q["port"] == port and (tname, g["bdf"], port) not in matched]
            bdf, how = (left[0], "elimination") if len(left) == 1 else (None, how)
        g = next((g for g in (t or {}).get("gpus", []) if g["bdf"] == bdf), None)
        p = next((p for p in (g or {}).get("ports", []) if p["port"] == port), None)
        if p:
            ln.bdf, ln.gpu_port, ln.station, ln.gpu, ln.matched_by = bdf, port, p["station"], p, how
            matched.add((tname, bdf, port))
    return links, {"slots": slots, "gpu_map": {f"{k[0]}:GPU{k[1]}": v for k, v in gpu_map.items()}}


# ---------------------------------------------------------------------------------------------- diagnosis

def _dur(ms) -> str:
    if ms is None:
        return "?"
    s = ms / 1000
    return f"{s / 86400:.1f} d" if s >= 86400 else f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min" if s >= 60 else f"{s:.0f} s"


def diagnose(ln: Link, trays: dict, optics_reported: bool, interval_s: float) -> Link:
    """Set state / cause / evidence / owner / action. First matching rule wins (bottom layer first)."""
    up = lambda v: str(v or "").lower() == "up"  # noqa: E731
    g, ev = ln.gpu, ln.evidence
    ev.append(f"switch {ln.switch} {ln.asic or ''} {ln.sw_port}: admin {ln.sw_admin}, oper {ln.sw_oper}; described as '{ln.desc}'")
    t = trays.get(ln.tray) if ln.tray else None
    if g:
        ev.append(f"GPU {ln.tray} {ln.bdf} port {ln.gpu_port}: admin {g['admin']}, link {g['link']}, fault {g['fault']}, "
                  f"up {_dur(g['up_ms'])} / down {_dur(g['down_ms'])} since boot; IFCP ll_up {g['ifcp'].get('ll_up')}, "
                  f"peer_enabled {g['ifcp'].get('peer_enabled')}")

    def set_(state, kind, cause, owner, action):
        ln.state, ln.kind, ln.cause, ln.owner, ln.action = state, kind, cause, owner, action
        return ln

    if t is not None and t.get("error"):
        return set_("down", "tray_unreachable", f"compute tray {ln.tray} not reachable over SSH", SITE,
                    "Check the tray's power and BMC; every link of the tray is affected (one root cause).")
    if not ln.tray and getattr(ln, "_quarantined", None):
        name, reason = ln._quarantined
        return set_("down" if not up(ln.sw_oper) else "degraded", "tray_quarantined",
                    f"compute tray {name} is quarantined in the inventory ({reason})", OPERATOR,
                    f"Expected while {name} is out of service; lift the quarantine when it is back and scan again.")
    if not ln.tray:
        return set_("down" if not up(ln.sw_oper) else "degraded", "tray_missing",
                    "no compute tray in the inventory answers for this switch port's slot", OPERATOR,
                    "Add the tray to the inventory, or check that it is powered and reachable.")
    if not g:
        return set_("down" if not up(ln.sw_oper) else "degraded", "gpu_missing",
                    f"GPU port not found on {ln.tray} (GPU not listed by afmctl, or numbering not learned)", GPU_VENDOR,
                    "Check `afmctl show device` on the tray: every GPU should be listed and ACTIVE.")
    if str(ln.sw_admin or "").lower() != "up":
        return set_("down", "sw_admin_down", "switch port administratively down", OPERATOR,
                    f"Bring it up: sudo config interface{ns_flag(ln.asic)} startup {ln.sw_port}")
    if str(g["admin"] or "").lower() != "enabled":
        st = (g["station"] if g["station"] is not None else ln.gpu_port // 2)
        return set_("down", "gpu_admin", f"GPU port admin state is '{g['admin']}'", OPERATOR,
                    f"Enable it: sudo env AFMCTL_FORCE_UPDATE=1 afmctl update port -b {ln.bdf} -s {st} -p {ln.gpu_port} --admin-state enable")
    gpu_up = "LINK_UP" in str(g["link"] or "") and "NO_PHY" not in str(g["link"] or "")
    if not up(ln.sw_oper) and not gpu_up:
        never = g["up_ms"] == 0
        what = "never came up since the tray booted" if never else f"was up, down for the last {_dur(g['down_ms'])}"
        if optics_reported and str(ln.presence).lower().startswith("not"):
            return set_("down", "phys_no_optic", f"no physical link: no transceiver / cable detected in {ln.switch} {ln.sw_port}", SITE,
                        "Seat or replace the cable / optic in the switch port.")
        return set_("down", "phys_never_up" if never else "phys_went_down", f"no physical link on either end ({g['link']}); {what}",
                    SITE,
                    "Reseat or swap the cable / cartridge between the two ports; if the fault stays on the port after a swap, "
                    "that port's side (switch or tray) owns it." + ("" if optics_reported else
                    " (This switch platform reports no transceiver presence or optic power, so a cable fault cannot be told "
                    "from a port fault remotely.)"))
    if up(ln.sw_oper) != gpu_up:
        return set_("down", "one_sided", f"one-sided link: switch port {'up' if up(ln.sw_oper) else 'down'}, GPU port {g['link']}",
                    SITE, "The two ends disagree: verify the cable lands where the switch port description says "
                          "(the IFCP peer of the GPU port names the switch port it really reaches).")
    fec_bad = ln.deltas.get("FEC_UNCORR", 0) > 0
    err_bad = any(ln.deltas.get(k, 0) > 0 for k in ("RX_ERR",))
    if fec_bad or err_bad:
        grow = ", ".join(f"{k} +{v}" for k, v in ln.deltas.items() if v)
        return set_("degraded", "bit_errors", f"bit errors on a link that is up ({grow} in {interval_s:.0f} s)",
                    SITE,
                    "Marginal cable / connector: reseat it, then sample again; if the errors follow the cable after a swap, "
                    "replace the cable.")
    i = g["ifcp"]
    if i.get("ll_up") != "yes" or i.get("peer_enabled") != "yes" or "yes" in (i.get("in_error"), i.get("peer_in_error")):
        return set_("degraded", "ifcp", f"physical link up but no IFCP session (ll_up {i.get('ll_up')}, peer_enabled "
                                f"{i.get('peer_enabled')}, in_error {i.get('in_error')}, peer_in_error {i.get('peer_in_error')})",
                    GPU_VENDOR, "Collect `afmctl show port -b <bdf> --json` from the tray and the switch's forwarding entries "
                                "for this port, and raise it with the GPU / AFM vendor.")
    if not ln.fdb_static and ln.fdb_static_late:
        ev.append("no static forwarding entry at the first read, present at the second: a vPOD change was in progress")
        return set_("ok", "", "", "", "")
    if not ln.fdb_static and ln.vpod_accelerators == 0:
        ev.append("afmctl lists no vPOD accelerators for this GPU")
        return set_("ok", "", "", "", "")
    if not ln.fdb_static:
        return set_("degraded", "not_programmed", "link and IFCP up but the switch port has no static forwarding entry (not programmed by AFM)",
                    GPU_VENDOR, "Check the GPU is in a vPOD and AFM shows the port PortReady; raise it with the AFM vendor.")
    return set_("ok", "", "", "", "")


def quarantined_slots(inv) -> dict:
    """{slot: (tray name, reason)} for compute trays quarantined in the inventory (with a ``slot:``)."""
    out = {}
    for h in getattr(inv, "quarantined", []) or []:
        slot = (h.extra or {}).get("slot")
        if h.role == "compute" and str(slot).lstrip("-").isdigit():
            out[int(slot)] = (h.name, (h.quarantine or {}).get("reason") or "out of service")
    return out


def scan(rack, interval_s: float = 30.0, log=print) -> dict:
    t0 = time.monotonic()
    log(f"reading {len(rack.switches)} switches and {len(rack.computes)} compute trays ...")
    sw_res, tr_res = {}, {}
    for sw, val, exc in fan_out(collect_switch, list(rack.switches.values())):
        sw_res[sw.host.name] = val if not exc else {"name": sw.host.name, "asics": {}, "error": str(exc)}
    for tr, val, exc in fan_out(collect_tray, list(rack.computes.values())):
        tr_res[tr.host.name] = val if not exc else {"name": tr.host.name, "gpus": [], "error": str(exc), "slot": None}
    links, learned = join(list(sw_res.values()), list(tr_res.values()), quarantined_slots(rack.inv))
    # second counter sample: errors are judged by what grows, not by totals since boot
    wait = max(0.0, interval_s - (time.monotonic() - t0))
    log(f"sampling counters again in {wait:.0f} s ...")
    time.sleep(wait)
    t1 = time.monotonic()
    again = {}
    for sw, val, exc in fan_out(lambda s: sample_counters(s, list(sw_res[s.host.name]["asics"])),
                                [s for s in rack.switches.values() if not sw_res[s.host.name].get("error")]):
        if not exc:
            again[sw.host.name] = val
    span = interval_s if interval_s else (time.monotonic() - t1)
    optics = {}
    for name, s in sw_res.items():
        vals = [v for a in s["asics"].values() for v in a["presence"].values()]
        optics[name] = bool(vals) and any(str(v).lower() == "present" for v in vals)
    for ln in links:
        a0 = sw_res[ln.switch]["asics"].get(ln.asic, {})
        a1 = (again.get(ln.switch) or {}).get(ln.asic, {})
        for k in COUNTERS:
            b, e = num((a0.get("counters", {}).get(ln.sw_port) or {}).get(k)), num((a1.get("counters", {}).get(ln.sw_port) or {}).get(k))
            if b is not None and e is not None and e > b:
                ln.deltas[k] = e - b
        for k in FEC:
            b, e = num((a0.get("fec", {}).get(ln.sw_port) or {}).get(k)), num((a1.get("fec", {}).get(ln.sw_port) or {}).get(k))
            ln.fec_total[k] = e
            if b is not None and e is not None and e > b:
                ln.deltas[k] = e - b
        if a1.get("fdb") is not None:
            ln.fdb_static_late = any(r.get("Port") == ln.sw_port and str(r.get("Type", "")).lower() == "static" for r in a1["fdb"])
        t = tr_res.get(ln.tray) or {}
        ln.vpod_accelerators = next((g.get("vpod_accelerators") for g in t.get("gpus", []) if g["bdf"] == ln.bdf), None)
        diagnose(ln, tr_res, optics.get(ln.switch, False), span)
    return {"switches": sw_res, "trays": tr_res, "links": links, "learned": learned, "optics_reported": optics,
            "interval_s": span}


def group_causes(links: list[Link]) -> list[dict]:
    """Problem links grouped by root-cause kind (and owner), down before degraded, most links first: a powered-off
    or quarantined tray is one line, not 144."""
    groups = {}
    for ln in links:
        if ln.state == "ok":
            continue
        tray = ln.tray if ln.kind == "tray_unreachable" else \
            (ln._quarantined[0] if ln.kind == "tray_quarantined" and getattr(ln, "_quarantined", None) else "")
        key = (ln.state, ln.kind, ln.owner, tray)
        groups.setdefault(key, []).append(ln)
    out = []
    for (state, kind, owner, tray), v in groups.items():
        label = KINDS.get(kind, kind)
        if tray:
            label = f"{label}: {tray}"
        out.append({"state": state, "kind": kind, "cause": label, "owner": owner, "count": len(v), "links": v})
    return sorted(out, key=lambda g: (g["state"] != "down", -g["count"]))


def link_dict(ln: Link) -> dict:
    d = asdict(ln)
    d.pop("gpu", None)
    return d
