"""MCH racks (heliosmch / heliosmchmini): 18 of a GPU's 36 IFoE ports in use. Skips unless the rack is MCH
(AFM pod spec rack-type contains "mch", or thresholds.mch.enabled: true).

Used ports come from the lane mask (thresholds.mch.lane_mask, default the plan's 000fff333000fff333): one hex
digit per station, read station 0 first (thresholds.mch.mask_order: msb_first | lsb_first - VERIFY);
lanes 0-1 (0x3) = port 2s, lanes 2-3 (0xC) = port 2s+1, so f = both ports, 3 = one port, 0 = none
-> 6 two-port, 6 one-port and 6 empty stations = 18 used ports. The mask is read on the tray with
commands.mch.lane_mask_cmd ({bdf}; default the UALink sysfs station_lane_en_bitmap - VERIFY).
"""
from __future__ import annotations

import json
import time

import pytest

from helios.afm import CLUSTER, MONITORING, conditions_ok
from helios.afm_ops import CN, SN, TERMINAL

pytestmark = [pytest.mark.l3]


# ------------------------------------------------------------------ MCH model

def _mask(ops) -> str:
    return str(ops.inv.th("mch.lane_mask", "000fff333000fff333")).lower()


def _ordered(mask: str, ops) -> str:
    order = ops.inv.th("mch.mask_order", "msb_first")
    return mask if order == "msb_first" else mask[::-1]


def used_ports(mask: str) -> set[int]:
    out = set()
    for s, d in enumerate(mask):
        bits = int(d, 16)
        if bits & 0x3:
            out.add(2 * s)
        if bits & 0xC:
            out.add(2 * s + 1)
    return out


def ports_by_station(used: set[int]) -> dict[int, set[int]]:
    return {s: {p for p in (2 * s, 2 * s + 1) if p in used} for s in range(18)}


@pytest.fixture
def mch(ops):
    """The MCH model for this rack; skips on a non-MCH rack."""
    pods = ops.afm.items(f"{CLUSTER}/pods")
    rt = " ".join(str((p.get("spec") or {}).get("rack-type", "")) for p in pods).lower()
    if not (ops.inv.th("mch.enabled", False) or "mch" in rt):
        pytest.skip(f"not an MCH rack (rack-type {rt or 'unknown'}; set thresholds.mch.enabled on an MCH rack)")
    mask = _ordered(_mask(ops), ops)
    used = used_ports(mask)
    return {"mask": mask, "used": used, "stations": ports_by_station(used), "mini": "mini" in rt,
            "planes": int(ops.inv.th("mch.planes", 18)), "rack_type": rt}


def _trays(ops):
    return [(c["meta"]["name"], ops.tray(c["meta"]["name"])) for c in ops.afm.computes()
            if (c.get("status") or {}).get("admission-phase") == "admitted"]


def _lane_mask(ops, tray, bdf) -> str:
    tmpl = ops.inv.cmd("mch.lane_mask_cmd", "cat /sys/bus/pci/devices/{bdf}/ualink/station_lane_en_bitmap")
    r = tray.run(tmpl.format(bdf=bdf), sudo=True, timeout=60)
    return r.stdout.strip().lower().removeprefix("0x")


def _port_rows(ops, tray, bdf) -> list[dict]:
    return ops.afmctl(tray).ports_raw(bdf)


def _bdfs(ops, tray):
    return sorted(d.bdf for d in ops.afmctl(tray).devices())


def _gpu_of(bdf: str) -> int:
    return int(bdf[3]) - 1


def _used_link(ops, slot, used, two_port: bool | None = None, mch_stations=None):
    """A switch link cabled to a used GPU port (optionally on a 2-port / 1-port station)."""
    for link in ops.gpu_links(slot):
        pid = link[3][3]
        if pid not in used:
            continue
        if two_port is not None and mch_stations is not None:
            n = len(mch_stations[pid // 2])
            if (n == 2) != two_port:
                continue
        return link
    pytest.fail(f"slot {slot}: no up link on a used port (two_port={two_port})")


# ------------------------------------------------------------------ layout (read-only)

@pytest.mark.tc("TC-MCH-002")
def test_station_port_distribution(ops, mch, checks, record_property):
    """Every GPU's configured (spec) station map has 6 two-port, 6 one-port and 6 empty stations."""
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            spec = ops.station_map(tray, bdf, "spec")
            dist = sorted(len(spec.get(s, set())) for s in range(18))
            got = {n: dist.count(n) for n in (0, 1, 2)}
            checks.check(got == {0: 6, 1: 6, 2: 6}, f"{tray.name}/{bdf}: stations by port count {got}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-004")
def test_lane_bitmask_value(ops, mch, checks, record_property):
    """The station lane-enable bitmask on every GPU equals thresholds.mch.lane_mask (000fff333000fff333)."""
    want = _mask(ops)
    seen = {}
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            got = _lane_mask(ops, tray, bdf)
            seen[f"{tray.name}/{bdf}"] = got
            checks.check(got == want, f"{tray.name}/{bdf}: lane mask {got!r}, expected {want}")
    record_property("masks", seen)
    checks.assert_all()


@pytest.mark.tc("TC-MCH-012")
def test_lane_strategy(ops, mch, checks, record_property):
    """The selected lane strategy is 6x2 / 6x1 / 6x0 (six f, six 3, six 0 digits), not the 9x2 / 9x0 alternative."""
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            m = _lane_mask(ops, tray, bdf)
            got = {c: m.count(c) for c in "f30"}
            checks.check(got == {"f": 6, "3": 6, "0": 6} and len(m) == 18, f"{tray.name}/{bdf}: mask {m} -> {got}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-006")
def test_macs_only_on_used_ports(ops, mch, checks, record_property):
    """Network MAC addresses are assigned only to the 18 used ports (afmctl spec.nw_mac_addr)."""
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            with_mac = {int(p["id"]) for p in _port_rows(ops, tray, bdf) if (p.get("spec") or {}).get("nw_mac_addr")}
            checks.check(with_mac == mch["used"], f"{tray.name}/{bdf}: MACs on {sorted(with_mac ^ mch['used'])} differ from the used ports")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-008")
def test_mi450_numbering(ops, mch, checks, record_property):
    """Port and station ids follow MI450 numbering: every port's station_id is port // 2 and the used ports are the
    mask's ports."""
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            rows = _port_rows(ops, tray, bdf)
            bad = [p["id"] for p in rows if str((p.get("spec") or {}).get("station_id")) != str(int(p["id"]) // 2)]
            checks.check(not bad, f"{tray.name}/{bdf}: ports with station_id != port//2: {bad[:6]}")
            checks.check(len(rows) == 36, f"{tray.name}/{bdf}: {len(rows)} ports listed, MI450 has 36")
    checks.assert_all()


def _disabled(row) -> bool:
    sp, st = row.get("spec") or {}, row.get("status") or {}
    return "disable" in str(sp.get("admin_state", "")).lower() or "disable" in str(st.get("link_status", "")).lower()


@pytest.mark.tc("TC-MCH-009")
def test_unused_ports_flagged_disabled(ops, mch, checks, record_property):
    """The 18 unused ports are visible in the get APIs and flagged disabled: afmctl admin/link state and AFM
    gpu-info port oper-state."""
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            rows = {int(p["id"]): p for p in _port_rows(ops, tray, bdf)}
            unused = set(range(36)) - mch["used"]
            checks.check(unused <= set(rows), f"{tray.name}/{bdf}: unused ports missing from afmctl: {sorted(unused - set(rows))[:6]}")
            notdis = [p for p in unused if p in rows and not _disabled(rows[p])]
            checks.check(not notdis, f"{tray.name}/{bdf}: unused ports not flagged disabled: {notdis[:6]}")
        for g in ops.gpu_info(ops.compute(name)).values():
            ports = g.get("ports") or []
            notdis = [i for i, p in enumerate(ports) if i not in mch["used"] and "disable" not in str(p.get("oper-state", "")).lower()]
            checks.check(not notdis, f"{name} GPU{g.get('index')}: AFM ports not flagged disabled: {notdis[:6]}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-019")
def test_used_up_unused_disabled(ops, mch, checks, record_property):
    """IFoE firmware reports the used ports LINK_UP and the unused ports disabled (not down)."""
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            rows = {int(p["id"]): p for p in _port_rows(ops, tray, bdf)}
            down = [p for p in mch["used"] if (rows.get(p, {}).get("status") or {}).get("link_status") != "LINK_UP"]
            checks.check(not down, f"{tray.name}/{bdf}: used ports not LINK_UP: {down[:6]}")
            wrong = [p for p in set(rows) - mch["used"] if not _disabled(rows[p])]
            checks.check(not wrong, f"{tray.name}/{bdf}: unused ports reported down/up instead of disabled: {wrong[:6]}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-015")
def test_link_count_per_gpu(ops, mch, checks, record_property):
    """Every GPU has thresholds.mch.links_per_gpu (18) links up."""
    want = int(ops.inv.th("mch.links_per_gpu", 18))
    for name, tray in _trays(ops):
        up = ops.ifcp_up(tray)
        for bdf in _bdfs(ops, tray):
            n = sum(1 for b, _ in up if b == bdf)
            checks.check(n == want, f"{tray.name}/{bdf}: {n} links up, expected {want}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-014")
def test_switch_slot_ids(ops, mch, checks, record_property):
    """heliosmch full rack: the switch slot ids are thresholds.mch.switch_slots (1, 2, 3)."""
    if mch["mini"]:
        pytest.skip("heliosmchmini: slot ids are checked on the full MCH rack")
    want = set(ops.inv.th("mch.switch_slots", [1, 2, 3]))
    got = {(s.get("spec") or {}).get("slot-id") for s in ops.afm.switches()}
    record_property("slot_ids", sorted(got))
    checks.check(got == want, f"switch slot ids {sorted(got)}, expected {sorted(want)}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-022")
def test_dest_addr_only_used_ports(ops, mch, checks, record_property):
    """afmctl destination accelerator addresses exist only for the used ports."""
    for name, tray in _trays(ops):
        table = ops.afmctl(tray).accel_addr()
        for bdf, ports in table.items():
            extra = [p for p, accs in ports.items() if accs and p not in mch["used"]]
            checks.check(not extra, f"{tray.name}/{bdf}: destination entries on unused ports {extra[:6]}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-025")
def test_path_to_port_bit_positions(ops, mch, checks, record_property):
    """Path-to-port positions are MI450's: within station s, path 0 maps to port 2s and path 1 to port 2s+1."""
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            doc = json.loads(tray.run(f"afmctl show station -b {bdf} --json 2>/dev/null", sudo=True, timeout=120).stdout)
            for d in doc.get("device") or []:
                for s in d.get("station") or []:
                    sid = int(s["id"])
                    for a in (s.get("spec") or {}).get("accel_path_map") or []:
                        for m in a.get("path_to_port_map") or []:
                            if int(m["port"]) != 2 * sid + int(m["path"]):
                                checks.fail(f"{tray.name}/{bdf} station {sid}: path {m['path']} -> port {m['port']}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-028")
def test_no_config_on_disabled_ports(ops, mch, checks, record_property):
    """AFM pushes no configuration to the unused ports: they appear in no station path-to-port map (spec or
    status) and are not admin-enabled."""
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            for which in ("spec", "status"):
                mapped = set().union(*ops.station_map(tray, bdf, which).values()) if ops.station_map(tray, bdf, which) else set()
                extra = mapped - mch["used"]
                checks.check(not extra, f"{tray.name}/{bdf}: unused ports in the {which} path map: {sorted(extra)[:6]}")
            enabled = [int(p["id"]) for p in _port_rows(ops, tray, bdf)
                       if int(p["id"]) not in mch["used"] and (p.get("spec") or {}).get("admin_state") == "enabled"]
            checks.check(not enabled, f"{tray.name}/{bdf}: unused ports admin-enabled: {enabled[:6]}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-007")
def test_zero_port_stations_excluded(ops, mch, checks, record_property):
    """0-port stations take no part in path computation (empty live maps) and raise no alarm: no event or alert
    in the last thresholds.mch.alarm_window_s (300 s) names an unused port of an MCH GPU, and AFM never marks
    those ports PortFailed."""
    empty = [s for s, ports in mch["stations"].items() if not ports]
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            live = ops.station_map(tray, bdf)
            busy = [s for s in empty if live.get(s)]
            checks.check(not busy, f"{tray.name}/{bdf}: 0-port stations with live paths {busy}")
    failed = [f for f in ops.failed_gpu_ports() if f[2] not in mch["used"]]
    checks.check(not failed, f"unused ports marked PortFailed: {failed[:6]}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-043")
def test_no_alarm_for_disabled_ports(ops, mch, checks, record_property):
    """No RAS / health event or alert refers to a disabled port or station, and AFM health stays clean for them."""
    unused = set(range(36)) - mch["used"]
    bad = []
    for e in ops.events(None, None, page=1000):
        msg = json.dumps(e)
        if "port-id: " in msg:
            try:
                pid = int(msg.split("port-id: ", 1)[1].split(")", 1)[0])
            except ValueError:
                continue
            if pid in unused:
                bad.append(e.get("type"))
    alerts = [a for a in ops.afm.items(f"{MONITORING}/alerts") if any(f"port-id: {p})" in json.dumps(a) for p in unused)]
    record_property("events_on_disabled_ports", bad[:10])
    checks.check(not bad, f"{len(bad)} events name a disabled port: {bad[:5]}")
    checks.check(not alerts, f"{len(alerts)} alerts name a disabled port")
    checks.check(not [f for f in ops.failed_gpu_ports() if f[2] in unused], "disabled ports reported PortFailed")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-048")
def test_disabled_ports_in_telemetry_without_data(ops, mch, checks, record_property):
    """Disabled ports are listed in the port statistics inventory but report no traffic (all counters 0)."""
    for name, tray in _trays(ops):
        doc = json.loads(tray.run("afmctl show port statistics mac --json 2>/dev/null", sudo=True, timeout=120).stdout)
        for d in doc.get("devices") or doc.get("device") or []:
            ports = {int(p["port_id"]): p for p in d.get("port") or []}
            missing = sorted((set(range(36)) - mch["used"]) - set(ports))
            checks.check(not missing, f"{tray.name}/{d.get('bdf')}: disabled ports missing from statistics: {missing[:6]}")
            busy = [p for p, row in ports.items() if p not in mch["used"]
                    and any(str(v).isdigit() and int(v) for v in (row.get("statistics") or {}).values())]
            checks.check(not busy, f"{tray.name}/{d.get('bdf')}: disabled ports with traffic counters: {busy[:6]}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-060")
def test_mini_rack_trunk_ports(ops, mch, inventory, checks, record_property):
    """heliosmchmini: each switch ASIC has thresholds.mch.trunk_ports_per_asic (144) trunk ports, all VLAN members
    priority-tagged (CONFIG_DB VLAN_MEMBER tagging_mode = thresholds.mch.tagging_mode, priority_tagged)."""
    if not mch["mini"]:
        pytest.skip("heliosmchmini only")
    want = int(ops.inv.th("mch.trunk_ports_per_asic", 144))
    mode = ops.inv.th("mch.tagging_mode", "priority_tagged")
    got = {}
    for sw in ops.rack.switches.values():
        for ns in sw.namespaces():
            trunks = [r for r in sw.interfaces_status(ns) if r.get("Vlan") == "trunk"]
            got[f"{sw.name}/{ns}"] = len(trunks)
            checks.check(len(trunks) == want, f"{sw.name}/{ns}: {len(trunks)} trunk ports, expected {want}")
            keys = sw.db_keys("CONFIG_DB", "VLAN_MEMBER|*", ns)
            wrong = [k for k in keys[:400] if sw.db_hash("CONFIG_DB", k, ns).get("tagging_mode") != mode]
            checks.check(not wrong, f"{sw.name}/{ns}: {len(wrong)} VLAN members not {mode}: {wrong[:3]}")
    record_property("trunk_ports", got)
    checks.assert_all()


@pytest.mark.tc("TC-MCH-063")
def test_switch_ignores_unused_lanes(ops, mch, checks, record_property):
    """The switch programs no VLAN membership on ports cabled to unused GPU ports (by CONFIG_DB description)."""
    from helios.afmctl import parse_port_description

    bad = []
    for sw in ops.rack.switches.values():
        for ns in sw.namespaces():
            desc = sw.port_descriptions(ns)
            members = {k.split("|")[-1] for k in sw.db_keys("CONFIG_DB", "VLAN_MEMBER|*", ns)}
            for port, d in desc.items():
                cab = parse_port_description(d)
                if cab and cab[3] not in mch["used"] and port in members:
                    bad.append(f"{sw.name}/{port}")
    checks.check(not bad, f"VLAN membership on ports cabled to unused lanes: {bad[:6]}")
    checks.assert_all()


# ------------------------------------------------------------------ dynamic behaviour (disruptive)

@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-021")
def test_path_map_updates_per_station_type(ops, afm_ready, mch, links, checks, record_property):
    """Live path-to-port maps follow link state for each station type: a 2-port station drops to 1 port, a 1-port
    station to 0, and both return to their full set."""
    name, tray = _trays(ops)[0]
    slot = ops.compute(name)["spec"]["slot-id"]
    for two in (True, False):
        sw, ns, port, cab = _used_link(ops, slot, mch["used"], two, mch["stations"])
        bdf, st = f"000{cab[1] + 1}:01:00.1", cab[3] // 2
        full = mch["stations"][st]
        links.shut(sw, ns, port)
        checks.check(ops.wait(lambda: ops.station_map(tray, bdf).get(st, set()) == full - {cab[3]}, 120, 5),
                     f"{'2' if two else '1'}-port station {st}: map {ops.station_map(tray, bdf).get(st)} with port {cab[3]} down")
        links.restore(sw, ns, port, checks)
        checks.check(ops.wait(lambda: ops.station_map(tray, bdf).get(st, set()) == full, 120, 5),
                     f"station {st} map not restored: {ops.station_map(tray, bdf).get(st)}")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-026")
def test_path_map_link_flap_per_station_type(ops, afm_ready, mch, links, checks, record_property):
    """A quick flap on a 2-port and on a 1-port station: each station's live map is back to its full set."""
    name, tray = _trays(ops)[0]
    slot = ops.compute(name)["spec"]["slot-id"]
    for two in (True, False):
        sw, ns, port, cab = _used_link(ops, slot, mch["used"], two, mch["stations"])
        bdf, st = f"000{cab[1] + 1}:01:00.1", cab[3] // 2
        links.shut(sw, ns, port)
        links.restore(sw, ns, port, checks)
        checks.check(ops.wait(lambda: ops.station_map(tray, bdf).get(st, set()) == mch["stations"][st], 120, 5),
                     f"station {st} map after flap: {ops.station_map(tray, bdf).get(st)}")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-040")
def test_lane_mask_on_link_down(ops, afm_ready, mch, links, checks, record_property):
    """A used link down clears its lanes in the station lane mask (the station's digit changes); restored after."""
    name, tray = _trays(ops)[0]
    slot = ops.compute(name)["spec"]["slot-id"]
    sw, ns, port, cab = _used_link(ops, slot, mch["used"], True, mch["stations"])
    bdf, st = f"000{cab[1] + 1}:01:00.1", cab[3] // 2
    before = _lane_mask(ops, tray, bdf)
    links.shut(sw, ns, port)
    changed = ops.wait(lambda: _lane_mask(ops, tray, bdf) != before, 120, 5)
    during = _lane_mask(ops, tray, bdf)
    record_property("mask", {"before": before, "link_down": during})
    checks.check(changed, f"lane mask unchanged with port {cab[3]} down: {during}")
    idx = st if ops.inv.th("mch.mask_order", "msb_first") == "msb_first" else len(before) - 1 - st
    if changed and len(during) == len(before):
        diff = [i for i, (a, b) in enumerate(zip(before, during)) if a != b]
        checks.check(diff == [idx], f"mask digits changed at {diff}, expected only station {st} (digit {idx})")
    links.restore(sw, ns, port, checks)
    checks.check(ops.wait(lambda: _lane_mask(ops, tray, bdf) == before, 120, 5), f"lane mask not restored: {_lane_mask(ops, tray, bdf)}")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-044")
def test_events_mi450_port_ids(ops, afm_ready, mch, links, checks, record_property):
    """A used port's link-down event carries the MI450 port id of that GPU port."""
    name, tray = _trays(ops)[0]
    slot = ops.compute(name)["spec"]["slot-id"]
    sw, ns, port, cab = _used_link(ops, slot, mch["used"])
    since = ops.controller_now()
    links.shut(sw, ns, port)
    ev = ops.wait(lambda: [e for e in ops.events(None, since) if f"port-id: {cab[3]})" in json.dumps(e)], 120, 5)
    links.restore(sw, ns, port, checks)
    record_property("events", [e.get("type") for e in ev or []])
    checks.check(ev, f"no event naming port-id {cab[3]} (MI450 id of the shut port)")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-039")
def test_unused_port_flap_no_event(ops, afm_ready, mch, links, checks, record_property):
    """Flapping a switch port cabled to an unused GPU lane (if any is cabled) raises no AFM event."""
    from helios.afmctl import parse_port_description

    cand = None
    for sw in ops.rack.switches.values():
        for ns in sw.namespaces():
            for port, d in sw.port_descriptions(ns).items():
                cab = parse_port_description(d)
                if cab and cab[3] not in mch["used"]:
                    cand = (sw, ns, port)
                    break
            if cand:
                break
        if cand:
            break
    if not cand:
        pytest.skip("no switch port is cabled to an unused GPU lane on this rack")
    since = ops.controller_now()
    links.shut(*cand)
    links.restore(*cand)
    time.sleep(30 * float(ops.th("time_scale", 1)))
    evs = [e for e in ops.events(None, since) if cand[2] in json.dumps(e)]
    checks.check(not evs, f"events for unused-lane port {cand[2]}: {[e.get('type') for e in evs][:5]}")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-024")
def test_drain_activate_used_ports_only(ops, afm_ready, mch, checks, record_property):
    """DrainGPUPort / ActivateGPUPort work on a used port (it leaves and rejoins the live path map); draining an
    unused port is rejected (4xx) or changes nothing; the AFM port list has 18 non-disabled ports per GPU."""
    name, tray = _trays(ops)[0]
    p = min(mch["used"])
    bdf, st = "0001:01:00.1", p // 2
    body = {"ports": [{"gpu-id": 0, "port-id": p}]}
    try:
        ops.afm.post(f"{CN}/{name}/DrainGPUPort", body)
        checks.check(ops.wait(lambda: p not in ops.station_map(tray, bdf).get(st, set()), 120, 5), f"drained port {p} still mapped")
    finally:
        ops.afm.post(f"{CN}/{name}/ActivateGPUPort", body)
    checks.check(ops.wait(lambda: p in ops.station_map(tray, bdf).get(st, set()), 180, 5), f"port {p} not back after activate")
    unused = min(set(range(36)) - mch["used"])
    r = ops.afm.raw("POST", f"{CN}/{name}/DrainGPUPort", {"ports": [{"gpu-id": 0, "port-id": unused}]})
    record_property("drain_unused_port", r.status_code)
    if r.status_code < 300:
        ops.afm.raw("POST", f"{CN}/{name}/ActivateGPUPort", {"ports": [{"gpu-id": 0, "port-id": unused}]})
        checks.check(not set().union(*ops.station_map(tray, bdf).values()) - mch["used"], "draining an unused port changed the map")
    for g in ops.gpu_info(ops.compute(name)).values():
        live = [i for i, x in enumerate(g.get("ports") or []) if "disable" not in str(x.get("oper-state", "")).lower()]
        checks.check(len(live) == len(mch["used"]), f"{name} GPU{g.get('index')}: {len(live)} non-disabled ports, expected {len(mch['used'])}")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-023")
def test_path_to_port_ras(ops, afm_ready, mch, checks, record_property):
    """Path-to-port RAS on MCH: after afmctl reset of a GPU the live station maps return to the MCH layout and the
    GPU is Ready again."""
    name, tray = _trays(ops)[0]
    bdf = _bdfs(ops, tray)[0]
    base = ops.station_map(tray, bdf)
    r = tray.run(f"afmctl reset device -b {bdf}", sudo=True, timeout=300)
    checks.check(r.ok, f"reset -> rc {r.rc}")
    checks.check(ops.wait_compute_ready(name, 180), f"{name} not Ready after reset")
    checks.check(ops.wait(lambda: ops.station_map(tray, bdf) == base, 180, 5), "station maps differ after reset")
    checks.assert_all()


def _vpod_cycle(ops, mch, uniq, checks, record_property, extra=None):
    sysv = ops.system_vpod_name()
    name = ops.pick_compute()
    user, entries = uniq("vp"), []
    try:
        entries = ops.remove_compute(sysv, name)
        ops.create_vpod(user, entries)
        checks.check(ops.wait_vpod(user) in TERMINAL, f"{user} {ops.vpod_state(user)}")
        prov = ops.planes(user)[0]
        record_property("provisioned_planes", prov)
        checks.check(prov == mch["planes"], f"{user}: {prov} provisioned planes, expected {mch['planes']}")
        if extra:
            extra(user)
        checks.check(ops.delete_vpod(user), f"{user} not deleted")
    finally:
        ops.delete_vpod(user)
        if entries:
            ops.add_gpus(sysv, entries)
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-031")
def test_vpod_lifecycle_18_links(ops, afm_ready, mch, uniq, checks, record_property):
    """vPOD create / delete on MCH: the user vPOD has thresholds.mch.planes (18) provisioned switching planes."""
    _vpod_cycle(ops, mch, uniq, checks, record_property)
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-062")
def test_vlan_port_mac_on_vpod_crud(ops, afm_ready, mch, uniq, checks, record_property):
    """vPOD CRUD on MCH programs VLANs on every switch while the vPOD exists and removes them afterwards."""
    before = {s.name: ops.vlan_count(s) for s in ops.rack.switches.values()}

    def during(user):
        ok = ops.wait(lambda: all(ops.vlan_count(s) > before[s.name] for s in ops.rack.switches.values()), 120, 10)
        checks.check(ok, "no VLAN programmed for the MCH vPOD on every switch")
    _vpod_cycle(ops, mch, uniq, checks, record_property, during)
    checks.check(ops.wait(lambda: all(ops.vlan_count(s) == before[s.name] for s in ops.rack.switches.values()), 180, 10),
                 "VLANs not removed after the vPOD delete")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-032")
def test_reconfigure_and_reconcile(ops, afm_ready, mch, checks, record_property):
    """Compute Reconfigure and system vPOD Reconcile work on MCH: GPUs Ready, vPOD online/degraded, maps intact."""
    name, tray = _trays(ops)[0]
    r = ops.reconfigure(name)
    checks.check(r.status_code == 200, f"Reconfigure -> HTTP {r.status_code}")
    checks.check(ops.wait_compute_ready(name, 300), f"{name} not Ready")
    sysv = ops.system_vpod_name()
    checks.check(ops.afm.raw("POST", f"{CLUSTER}/vpods/{sysv}/Reconcile", {}).status_code == 200, "Reconcile failed")
    checks.check(ops.wait_vpod(sysv) in TERMINAL, f"{sysv} not online/degraded")
    for bdf in _bdfs(ops, tray):
        checks.check(set().union(*ops.station_map(tray, bdf).values()) <= mch["used"], f"{bdf}: unused ports mapped after reconfigure")
    checks.assert_all()


def _degrade(ops, mch, links, checks, record_property, ports):
    sysv = ops.system_vpod_name()
    base = ops.settled_planes(sysv, interval=float(ops.th("plane_settle_interval_s", 10)))
    for sw, ns, port, _ in ports:
        links.shut(sw, ns, port)
    ok = ops.wait(lambda: ops.vpod_state(sysv) == "degraded" and (ops.planes(sysv)[1] or 0) < (base[1] or 0),
                  ops.link_fail_threshold(sysv) + 120, 5)
    record_property("during", {"state": ops.vpod_state(sysv), "planes": ops.planes(sysv), "baseline": base})
    checks.check(ok, f"{sysv} {ops.vpod_state(sysv)} / planes {ops.planes(sysv)} with {len(ports)} of the 18 links "
                     f"down, expected degraded with fewer available planes than {base}")
    checks.check(ops.vpod_state(sysv) != "online", f"{sysv} still online")
    for sw, ns, port, _ in ports:
        links.restore(sw, ns, port, checks)
    checks.check(ops.wait(lambda: ops.planes(sysv)[1] == base[1], float(ops.th("plane_restore_s", 300)), 10),
                 f"planes {ops.planes(sysv)} not back to {base}")


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-033")
def test_partial_bringup_degraded(ops, afm_ready, mch, links, checks, record_property):
    """Fewer than 18 links (one used link down): the vPOD is marked degraded, not online."""
    name, _ = _trays(ops)[0]
    _degrade(ops, mch, links, checks, record_property, [_used_link(ops, ops.compute(name)["spec"]["slot-id"], mch["used"])])
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-034")
def test_one_plane_down_degraded(ops, afm_ready, mch, links, checks, record_property):
    """One of the 18 switching planes down (the same used port on every compute's GPU 0): the vPOD goes degraded
    and recovers when the plane returns."""
    pid = min(mch["used"])
    sel = []
    for name, _ in _trays(ops):
        slot = ops.compute(name)["spec"]["slot-id"]
        sel += [l for l in ops.gpu_links(slot, 0) if l[3][3] == pid]
    if not sel:
        pytest.fail(f"no switch link found for port {pid}")
    _degrade(ops, mch, links, checks, record_property, sel)
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-030")
def test_lifecycle_unchanged(ops, afm_ready, mch, checks, record_property):
    """Compute decommission / delete / readmit works on MCH unchanged and the GPU keeps the MCH port layout."""
    sysv = ops.system_vpod_name()
    name, tray = _trays(ops)[0]
    slot = ops.compute(name)["spec"]["slot-id"]
    entries = ops.remove_compute(sysv, name)
    host, _ = ops.slot_host(slot)
    try:
        ops.afm.post(f"{CN}/{name}/Decommission", {})
        checks.check(ops.wait(lambda: ops.compute_phase(name) == "decommissioned", 300, 10), "not decommissioned")
        ops.afm.raw("DELETE", f"{CN}/{name}")
        ops.wait(lambda: ops.compute_phase(name) == "absent", 60, 5)
        if host is not None:
            ops.afm_agent(host, "restart")
        checks.check(ops.wait_compute_ready(name), f"{name} not readmitted")
    finally:
        if ops.compute_phase(name) == "admitted":
            ops.add_gpus(sysv, entries)
    for bdf in _bdfs(ops, tray):
        checks.check(set().union(*ops.station_map(tray, bdf, "spec").values()) == mch["used"], f"{bdf}: port layout changed after readmit")
    if len(ops.afm.switches()) >= 2:
        t = ops.pick_switch(controller=False)
        n = t["meta"]["name"]
        try:
            ops.afm.post(f"{SN}/{n}/DrainSwitchNode", {})
            checks.check(ops.wait_switch_phase(n, "drained", 900), f"{n} not drained")
        finally:
            ops.afm.raw("POST", f"{SN}/{n}/ReactivateSwitchNode", {})
        checks.check(ops.wait(lambda: ops.switch_ready(n), 600, 15), f"{n} not back")
    checks.assert_all()


def _layout_snapshot(ops) -> dict:
    out = {}
    for name, tray in _trays(ops):
        for bdf in _bdfs(ops, tray):
            out[f"{name}/{bdf}"] = {"spec": {k: sorted(v) for k, v in ops.station_map(tray, bdf, "spec").items()},
                                    "disabled": sorted(int(p["id"]) for p in _port_rows(ops, tray, bdf) if _disabled(p))}
    return out


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-053")
def test_topology_persists_controller_restart(ops, afm_ready, mch, checks, record_property):
    """The 18-link layout and path-to-port maps are identical after every controller is restarted."""
    before = _layout_snapshot(ops)
    for ip, sw in ops.controllers().items():
        ops.restart_controller(sw)
    ops.wait(lambda: conditions_ok(ops.afm.cluster()), 300, 10)
    for c in ops.afm.computes():
        ops.wait_compute_ready(c["meta"]["name"], 600)
    after = _layout_snapshot(ops)
    checks.check(after == before, f"layout changed across the controller restart: {[k for k in before if before[k] != after.get(k)][:4]}")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-055")
def test_disabled_state_persists(ops, afm_ready, mch, checks, record_property):
    """Disabled/unused port state is unchanged after a controller restart, an afm-agent (node agent) restart on
    every switch, and a configuration snapshot + restore."""
    before = _layout_snapshot(ops)
    steps = {}
    for ip, sw in ops.controllers().items():
        ops.restart_controller(sw)
    ops.wait(lambda: conditions_ok(ops.afm.cluster()), 300, 10)
    steps["controller restart"] = _layout_snapshot(ops)
    for sw in ops.rack.switches.values():
        ops.afm_agent(sw, "restart")
    for c in ops.afm.computes():
        ops.wait_compute_ready(c["meta"]["name"], 600)
    steps["agent restart"] = _layout_snapshot(ops)
    ops.afm.raw("DELETE", f"{CLUSTER}/config-snapshot")
    ops.afm.post(f"{CLUSTER}/config-snapshot", {"meta": {"name": "Default-Snapshot"}, "spec": {}})
    uri = ((ops.afm.post(f"{CLUSTER}/config-snapshot/save") or {}).get("status") or {}).get("last-snapshot", {}).get("uri", "")
    ops.afm.raw("POST", f"{CLUSTER}/config-restore/restore", {"meta": {"name": "SnapshotRestore"},
                                                             "spec": {"path": uri.rsplit("/", 1)[-1]}})
    time.sleep(float(ops.th("restore_settle_s", 60)) * float(ops.th("time_scale", 1)))
    ops.afm.login()
    ops.wait_vpod(ops.system_vpod_name())
    steps["snapshot restore"] = _layout_snapshot(ops)
    for k, v in steps.items():
        checks.check(v == before, f"MCH port state changed after {k}")
    checks.assert_all()


# ------------------------------------------------------------------ traffic / templates

@pytest.mark.usefixtures("datapath_ready")
@pytest.mark.tc("TC-MCH-045")
def test_traffic_over_18_links(ops, mch, computes, inventory, checks, record_property):
    """L2 ping and in-domain TransferBench over the 18 used links."""
    from helios import datapath

    record_property("pairs", datapath.l2_ping(computes, inventory, checks))
    record_property("tb", datapath.transferbench_in_domain(computes, inventory, checks))
    checks.assert_all()


@pytest.mark.usefixtures("datapath_ready")
@pytest.mark.tc("TC-MCH-047")
def test_throughput_18_links(ops, mch, computes, inventory, checks, record_property):
    """In-domain TransferBench bandwidth on MCH is at least thresholds.mch.min_gbps (the expected 18-link figure)."""
    from helios import datapath

    floor = ops.inv.th("mch.min_gbps")
    if floor is None:
        pytest.skip("thresholds.mch.min_gbps not set (expected 18-link bandwidth)")
    bw = datapath.transferbench_in_domain(computes, inventory, checks)
    record_property("tb_gbps", bw)
    for k, v in (bw or {}).items():
        checks.check(v is not None and v >= float(floor), f"{k}: {v} GB/s < {floor}")
    checks.assert_all()


@pytest.mark.tc("TC-MCH-011")
def test_mock_ifoe_driver_bringup(ops, mch, inventory, checks, record_property):
    """With the mock IFoE driver (commands.mch.mock_driver_check, run on every tray, rc 0 = loaded) every GPU still
    comes up with the MCH layout (18 used ports in the spec maps)."""
    tmpl = inventory.cmd("mch.mock_driver_check")
    if not tmpl:
        pytest.skip("commands.mch.mock_driver_check not set")
    for name, tray in _trays(ops):
        r = tray.run(tmpl.format(name=tray.name), sudo=True, timeout=60)
        checks.check(r.ok, f"{tray.name}: mock IFoE driver not loaded ({r.tail()!r})")
        for bdf in _bdfs(ops, tray):
            checks.check(set().union(*ops.station_map(tray, bdf, "spec").values()) == mch["used"], f"{tray.name}/{bdf}: layout")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-018")
def test_invalid_rack_type_rejected(ops, mch, inventory, checks, record_property):
    """Bootstrap with an invalid rack type is rejected: commands.mch.bootstrap_invalid (run on the runner) must exit
    non-zero, and the running cluster is unaffected."""
    tmpl = inventory.cmd("mch.bootstrap_invalid")
    if not tmpl:
        pytest.skip("commands.mch.bootstrap_invalid not set (bootstrap with an unsupported rack type)")
    r = ops.rack.runner.run(tmpl, timeout=600)
    record_property("bootstrap", {"rc": r.rc, "out": r.tail(300)})
    checks.check(not r.ok, "bootstrap with an invalid rack type succeeded")
    checks.check(conditions_ok(ops.afm.cluster()), "cluster not healthy afterwards")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-MCH-054")
def test_upgrade_preserves_mch(ops, afm_ready, mch, inventory, checks, record_property):
    """An AFM upgrade (commands.afm.upgrade, run on the runner) keeps the MCH rack type and the 18-link layout."""
    tmpl = inventory.cmd("afm.upgrade")
    if not tmpl:
        pytest.skip("commands.afm.upgrade not set")
    before, rt = _layout_snapshot(ops), mch["rack_type"]
    r = ops.rack.runner.run(tmpl, timeout=3600)
    checks.check(r.ok, f"upgrade command failed: {r.tail()!r}")
    ops.wait(lambda: conditions_ok(ops.afm.cluster()), 900, 15)
    ops.afm.login()
    rt2 = " ".join(str((p.get("spec") or {}).get("rack-type", "")) for p in ops.afm.items(f"{CLUSTER}/pods")).lower()
    checks.check(rt2 == rt, f"rack type changed: {rt} -> {rt2}")
    checks.check(_layout_snapshot(ops) == before, "MCH layout changed after the upgrade")
    checks.assert_all()
