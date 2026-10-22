"""TC-SWH-037..047 - switch-tray checks ADDED to the test plan by helios-tests on 14 Oct 2026 (not in AMD's
BKC-26.12 workbook; listed in testplan/added.yaml, Origin "Added - helios-tests" in the execution workbook).

Read-only. Motivated by the g02-g17 L1 run (13 Oct): GPU-described ports left 'routed' and down on every switch,
identical findings on all 6 switches, no coverage for optics, FEC, container health or crashes.

Thresholds (inventory ``thresholds.switch``):
  uncabled_admin_down (false)      TC-SWH-037: also require ports without a cabling description to be admin down
  consistency_keys                 TC-SWH-038: what must match on every switch (default: all of version, images,
                                   port_layout, vlans)
  optics.{rx_power_dbm_min, tx_power_dbm_min, temp_c_max, vcc_min, vcc_max}   TC-SWH-039 DOM limits
  fec_window_s (30), fec_max_uncorr_delta (0), fec_pre_ber_max (1e-5)        TC-SWH-040
  critical_containers              TC-SWH-041: expected containers; '{n}' expands to each ASIC index
  restart_slack_s (1800)           TC-SWH-041: a container started this long after the first one was restarted
  restart_window_h (24)            TC-SWH-041: only such restarts within this many hours fail (older ones are recorded
                                   as earlier_restarts); null = any restart since boot fails
  expected_reboot_causes           TC-SWH-042: regexes for an acceptable last reboot cause
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime

import pytest

from helios.afmctl import parse_port_description
from helios.parsers import to_float
from helios.rack import fan_out

# level per test (TC-SWH-038/045/046/047 are L2)

DEFAULT_CONTAINERS = ["database", "pmon", "lldp", "database{n}", "swss{n}", "syncd{n}", "lldp{n}", "afm-agent"]
DEFAULT_REBOOT_CAUSES = [r"User issued '?(warm-|fast-)?reboot", r"^reboot$", r"warm-reboot", r"fast-reboot",
                         r"Kernel Panic.*user", r"^config reload"]


# ------------------------------------------------------------------ TC-SWH-037

@pytest.mark.l1
@pytest.mark.tc("TC-SWH-037")
def test_port_config_matches_cabling(switch, inventory, checks, record_property):
    """Every port described as a GPU link is a trunk, admin up and a VLAN member; with
    switch.uncabled_admin_down, ports without a description must be admin down."""
    strict = bool(inventory.th("switch.uncabled_admin_down", False))
    cabled, wrong = 0, Counter()
    qslots = inventory.quarantined_slots()
    for ns in switch.namespaces():
        desc = switch.port_descriptions(ns)
        members = {p for ports in switch.vlan_members(ns).values() for p in ports}
        for r in switch.interfaces_status(ns):
            port = r["Interface"]
            cab = parse_port_description(desc.get(port, ""))
            mode, admin = r.get("Vlan", "").lower(), r.get("Admin", "").lower()
            if cab and cab[0] in qslots:
                wrong["quarantined (not checked)"] += 1      # cabled to a quarantined tray
                continue
            if cab:
                cabled += 1
                where = f"{switch.name}/{ns}: {port} ({desc[port]})"
                if not checks.check(mode == "trunk", f"{where} is '{mode}', expected trunk"):
                    wrong["not trunk"] += 1
                if not checks.check(admin == "up", f"{where} admin {admin}"):
                    wrong["admin down"] += 1
                if not checks.check(port in members, f"{where} is in no VLAN"):
                    wrong["no VLAN"] += 1
            elif strict:
                checks.check(admin == "down", f"{switch.name}/{ns}: {port} has no cabling description but is admin {admin}")
    record_property("ports", {"cabled": cabled, **wrong})
    checks.check(cabled, f"{switch.name}: no port carries a cabling description")
    checks.assert_all()


# ------------------------------------------------------------------ TC-SWH-038

def _profile(sw) -> dict:
    layout, vlans = {}, {}
    for ns in sw.namespaces():
        for r in sw.interfaces_status(ns):
            layout[f"{ns}/{r['Interface']}"] = (r.get("Speed"), r.get("MTU"), r.get("Vlan"))
        vlans[ns] = tuple(sorted(sw.vlans(ns)))
    images = {c: sw.container_image(c).split()[0] for c in ("afm-agent", "afm-controller") if c in sw.containers()}
    return {"version": sw.sonic_version(), "images": images, "port_layout": layout, "vlans": vlans}


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-038")
def test_switches_configured_consistently(rack, inventory, checks, record_property):
    """Every switch matches the majority on the SONiC build, the AFM agent image (and controller image where it
    runs), the port speed/MTU/role layout and the VLAN set."""
    sws = list(rack.switches.values())
    if len(sws) < 2:
        pytest.skip("needs at least 2 switch trays")
    keys = inventory.th("switch.consistency_keys") or ["version", "images", "port_layout", "vlans"]
    profiles = {}
    for sw, prof, exc in fan_out(_profile, sws):
        if not checks.check(exc is None, f"{sw.name}: cannot read its configuration: {exc}"):
            continue
        profiles[sw.name] = prof
    record_property("versions", {n: p["version"] for n, p in profiles.items()})
    for key in keys:
        if key == "images":     # per container, controller image only where the controller runs
            for c in ("afm-agent", "afm-controller"):
                vals = {n: p["images"][c] for n, p in profiles.items() if c in p["images"]}
                _majority(f"{c} image", vals, checks)
        elif key == "port_layout":
            ports = set().union(*(p["port_layout"] for p in profiles.values())) if profiles else set()
            skip = set().union(*(sw.exclude_ports for sw in sws))   # inventory exclude_ports: deliberately unlisted
            for port in sorted(p for p in ports if p.split("/", 1)[-1] not in skip):
                vals = {n: p["port_layout"].get(port, ("absent",)) for n, p in profiles.items()}
                _majority(f"port {port} speed/MTU/role", vals, checks, limit=40)
        else:
            _majority(key, {n: p[key] for n, p in profiles.items()}, checks)
    checks.assert_all()


def _majority(what, vals: dict, checks, limit: int = 10) -> None:
    if len(set(map(repr, vals.values()))) <= 1:
        return
    common, _ = Counter(map(repr, vals.values())).most_common(1)[0]
    odd = {n: v for n, v in vals.items() if repr(v) != common}
    for n, v in list(odd.items())[:limit]:
        checks.fail(f"{n}: {what} = {v!r} differs from the other switches ({common})")


# ------------------------------------------------------------------ TC-SWH-039

@pytest.mark.l1
@pytest.mark.tc("TC-SWH-039")
def test_transceivers_and_dom(switch, inventory, checks, record_property):
    """A module is present on every cabled fabric port and its DOM readings are within the limits. Skips when no
    fabric port has a pluggable module (Helios backplane / cable-cartridge links: 'Not present' everywhere)."""
    lim = inventory.th("switch.optics") or {}
    rx_min, tx_min = lim.get("rx_power_dbm_min", -10.0), lim.get("tx_power_dbm_min", -8.0)
    t_max, v_min, v_max = lim.get("temp_c_max", 75.0), lim.get("vcc_min", 3.13), lim.get("vcc_max", 3.47)
    present_any, cabled_absent, checked = 0, [], 0
    for ns in switch.namespaces():
        desc = switch.port_descriptions(ns)
        pres = switch.transceiver_presence(ns)
        cabled = [p for p in pres if switch.is_fabric_port(p) and parse_port_description(desc.get(p, ""))]
        present_any += sum(1 for p in pres if pres[p])
        for port in cabled:
            if not pres[port]:
                cabled_absent.append(f"{ns}/{port}")
                continue
            dom = switch.transceiver_dom(port, ns)
            checked += 1
            where = f"{switch.name}/{ns}: {port}"
            for k, v in dom.items():
                if re.fullmatch(r"RX\d*Power", k, re.I) and rx_min is not None:
                    checks.check(v >= rx_min, f"{where} {k} {v} dBm < {rx_min}")
                elif re.fullmatch(r"TX\d*Power", k, re.I) and tx_min is not None:
                    checks.check(v >= tx_min, f"{where} {k} {v} dBm < {tx_min}")
                elif k.lower() == "temperature" and t_max is not None:
                    checks.check(v <= t_max, f"{where} module temperature {v} C > {t_max}")
                elif k.lower() == "vcc" and v_min is not None:
                    checks.check(v_min <= v <= v_max, f"{where} Vcc {v} V outside {v_min}-{v_max}")
    record_property("modules", {"present": present_any, "dom_checked": checked, "cabled_without_module": len(cabled_absent)})
    if not present_any:
        pytest.skip(f"{switch.name}: no pluggable transceiver on any fabric port (backplane / cable-cartridge links) - "
                    "optics checks do not apply to this platform")
    checks.check(not cabled_absent, f"{switch.name}: cabled ports without a module: {cabled_absent[:10]}")
    checks.assert_all()


# ------------------------------------------------------------------ TC-SWH-040

@pytest.mark.l1
@pytest.mark.tc("TC-SWH-040")
def test_fec_health(switch, inventory, checks, record_property):
    """Uncorrectable FEC codewords do not increase over thresholds.switch.fec_window_s (30 s) on any up fabric
    port, and the pre-FEC BER (where the platform measures it) is below fec_pre_ber_max (1e-5)."""
    window = float(inventory.th("switch.fec_window_s", 30))
    max_delta = int(inventory.th("switch.fec_max_uncorr_delta", 0))
    ber_max = float(inventory.th("switch.fec_pre_ber_max", 1e-5))
    nss = switch.namespaces()
    before = {ns: switch.fec_stats(ns) for ns in nss}
    if not any(before.values()):
        pytest.skip(f"{switch.name}: 'show interfaces counters fec-stats' lists no fabric ports")
    time.sleep(window)
    measured, worst = 0, {}
    for ns in nss:
        for port, row in switch.fec_stats(ns).items():
            if row.get("STATE", "").upper() not in ("U", "UP"):
                continue
            a, b = to_float(row.get("FEC_UNCORR")), to_float(before[ns].get(port, {}).get("FEC_UNCORR"))
            if a is not None and b is not None:
                checks.check(a - b <= max_delta, f"{switch.name}/{ns}: {port} +{a - b:.0f} uncorrectable FEC codewords in {window:.0f}s")
            try:
                ber = float(str(row.get("FEC_PRE_BER", "")).strip())
            except ValueError:
                ber = None
            if ber is not None and ber == ber:          # 'nan' = not measured on this platform
                measured += 1
                worst[f"{ns}/{port}"] = ber
                checks.check(ber <= ber_max, f"{switch.name}/{ns}: {port} pre-FEC BER {ber:.2e} > {ber_max:.0e}")
    record_property("pre_fec_ber", {"ports_measured": measured,
                                    "worst": dict(sorted(worst.items(), key=lambda kv: -kv[1])[:5])})
    checks.assert_all()


# ------------------------------------------------------------------ TC-SWH-041

def _ts(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-041")
def test_critical_containers_running(switch, inventory, checks, record_property):
    """The critical containers run (per ASIC where named with {n}; afm-controller on afm.controller_nodes), none
    has a docker restart count or started more than restart_slack_s after the others (= restarted) within the last
    restart_window_h hours (older restarts are recorded, not failed - they were reported when they were new)."""
    want = list(inventory.th("switch.critical_containers") or DEFAULT_CONTAINERS)
    asics = [ns[4:] for ns in switch.namespaces() if ns and ns.startswith("asic")]
    expected = [w.replace("{n}", n) for w in want for n in (asics if "{n}" in w else [""])]
    ctrl = (inventory.afm or {}).get("controller_nodes")
    if ctrl and switch.name in ctrl:
        expected.append("afm-controller")
    running = switch.containers()
    missing = [c for c in expected if c not in running]
    checks.check(not missing, f"{switch.name}: critical containers not running: {missing}")
    info = switch.container_restarts()
    starts = [t for _, iso in info.values() if (t := _ts(iso)) is not None]
    first = min(starts) if starts else None
    slack = float(inventory.th("switch.restart_slack_s", 1800))
    window_h = inventory.th("switch.restart_window_h", 24)
    since = switch.epoch() - float(window_h) * 3600 if window_h else None
    earlier = {}
    for c in expected:
        if c not in info:
            continue
        count, iso = info[c]
        checks.check(count == 0, f"{switch.name}: {c} restarted {count} time(s) by docker")
        t = _ts(iso)
        if first is not None and t is not None and t - first > slack:
            if since is not None and t < since:
                earlier[c] = iso[:19]
                continue
            checks.fail(f"{switch.name}: {c} started {(t - first) / 60:.0f} min after the first container "
                        f"(restarted at {iso[:19]})")
    if earlier:
        record_property("earlier_restarts", earlier)
    ok, text = switch.system_health()
    record_property("system_health", "ok" if ok else ("command crashed" if "Traceback" in text else text.strip()[:200]))
    if ok:
        checks.check("Not OK" not in text, f"{switch.name}: system health: {text.strip()[:300]}")
    checks.assert_all()


# ------------------------------------------------------------------ TC-SWH-042

@pytest.mark.l1
@pytest.mark.tc("TC-SWH-042")
def test_no_unexpected_reboot_or_crash(switch, inventory, checks, record_property):
    """The last reboot cause is an expected one (user / warm / fast reboot, config reload) and /var/core holds no
    core files."""
    cause = switch.reboot_cause()
    record_property("reboot_cause", cause[:200])
    record_property("reboot_history", [f"{r.get('Name')}: {r.get('Cause')}" for r in switch.reboot_history()[:5]])
    allowed = inventory.th("switch.expected_reboot_causes") or DEFAULT_REBOOT_CAUSES
    checks.check(any(re.search(rx, cause, re.I) for rx in allowed),
                 f"{switch.name}: unexpected last reboot cause: {cause[:200]!r}")
    cores = switch.core_files()
    record_property("core_files", cores[:20])
    checks.check(not cores, f"{switch.name}: {len(cores)} core file(s) in /var/core: {cores[:5]}")
    checks.assert_all()


# ================================================================== TC-SWH-043..047 (added 14 Oct 2026, second batch)
# thresholds.switch: log_window_h (24) - how far back the log checks look; syslog_ignore - regexes of known-noise
# messages (e.g. the 100G uplinks' link-training / PHY-temperature errors on QFX5252); link_watch_s (120),
# link_poll_s (10); pfc_window_s (30), pfc_max_pause_delta (0), pfc_enable (expected 'pfc_enable', e.g. "3,4");
# config_diff_ignore - CONFIG_DB tables allowed to differ between running and saved config

KERNEL_HW_ERRORS = (r"kernel:.*(AER:|PCIe Bus Error|Machine check|mce: |EDAC|I/O error|Remounting filesystem "
                    r"read-only|NETDEV WATCHDOG|Out of memory|oom-kill|BUG: |Call Trace|blocked for more than|"
                    r"hardware error|Hardware Error)")
_MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def _log_time(line: str) -> float | None:
    """'2026 Oct 14 16:23:38.854974 host LEVEL ...' (SONiC syslog) -> epoch seconds (UTC)."""
    m = re.match(r"(\d{4}) (\w{3}) +(\d+) (\d+):(\d+):(\d+)", line)
    if not m or m.group(2) not in _MONTHS:
        return None
    y, mo, d, hh, mm, ss = int(m.group(1)), _MONTHS[m.group(2)], *map(int, m.group(3, 4, 5, 6))
    from calendar import timegm
    return float(timegm((y, mo, d, hh, mm, ss, 0, 0, 0)))


def _recent(switch, inventory, lines: list[str]) -> list[str]:
    cutoff = switch.epoch() - float(inventory.th("switch.log_window_h", 24)) * 3600
    return [ln for ln in lines if (t := _log_time(ln)) is None or t >= cutoff]


def _short(line: str, n: int = 220) -> str:
    return re.sub(r"\s+", " ", line)[:n]


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-043")
def test_no_kernel_hardware_errors(switch, inventory, checks, record_property):
    """No hardware-error signature in the kernel messages of the last log_window_h hours: PCIe AER, machine check,
    EDAC/ECC, I/O error, read-only remount, NIC transmit hang (NETDEV WATCHDOG), OOM, kernel BUG / call trace."""
    hits = [h for h in _recent(switch, inventory, switch.syslog_grep(KERNEL_HW_ERRORS)) if re.search(KERNEL_HW_ERRORS, h)]
    record_property("kernel_hw_errors", [_short(h) for h in hits[:10]])
    kinds = Counter(re.search(KERNEL_HW_ERRORS, h).group(1) for h in hits if re.search(KERNEL_HW_ERRORS, h))
    checks.check(not hits, f"{switch.name}: {len(hits)} kernel hardware-error message(s) in the last "
                           f"{inventory.th('switch.log_window_h', 24)} h ({dict(kinds)}); latest: {_short(hits[-1]) if hits else ''}")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-044")
def test_no_new_syslog_errors(switch, inventory, checks, record_property):
    """No CRIT/ALERT/EMERG message, and no ERR message from syncd / orchagent / SAI / AFM agent that is not listed in
    thresholds.switch.syslog_ignore, in the last log_window_h hours."""
    ignore = [re.compile(x) for x in (inventory.th("switch.syslog_ignore") or [])]
    lines = [ln for ln in _recent(switch, inventory, switch.syslog_grep(r" (ERR|CRIT|ALERT|EMERG) "))
             if re.search(r" (ERR|CRIT|ALERT|EMERG) ", ln)]
    bad, ignored = [], 0
    for ln in lines:
        sev = re.search(r" (ERR|CRIT|ALERT|EMERG) ", ln).group(1)
        if any(rx.search(ln) for rx in ignore):
            ignored += 1
            continue
        if sev != "ERR" or re.search(r"syncd|orchagent|sai|afm|swss", ln, re.I):
            bad.append(ln)
    # '2026 Oct 14 16:15:24.474622 st1 ERR swss0#orchagent: ...' -> source 'swss0#orchagent'
    by_src = Counter(re.sub(r"\[\d+\]", "", (ln.split()[6] if len(ln.split()) > 6 else "?")).rstrip(":") for ln in bad)
    record_property("syslog", {"errors": len(bad), "ignored_known_noise": ignored, "by_source": dict(by_src.most_common(8))})
    checks.check(not bad, f"{switch.name}: {len(bad)} new ERR/CRIT message(s) in the last "
                          f"{inventory.th('switch.log_window_h', 24)} h by source {dict(by_src.most_common(5))}; "
                          f"latest: {_short(bad[-1]) if bad else ''}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-045")
def test_fabric_links_stable(switch, inventory, checks, record_property):
    """Every fabric port that is oper up at the start stays up for thresholds.switch.link_watch_s (120 s), sampled every
    link_poll_s (10 s); a port that goes down - even briefly between samples it shows as down - is reported."""
    watch, poll = float(inventory.th("switch.link_watch_s", 120)), float(inventory.th("switch.link_poll_s", 10))
    nss = switch.namespaces()
    up0 = {(ns, r["Interface"]) for ns in nss for r in switch.interfaces_status(ns) if r.get("Oper", "").lower() == "up"}
    changes: dict[tuple, int] = Counter()
    end, samples = time.monotonic() + watch, 0
    while time.monotonic() < end:
        time.sleep(min(poll, max(0.0, end - time.monotonic())))
        samples += 1
        now_up = {(ns, r["Interface"]) for ns in nss for r in switch.interfaces_status(ns) if r.get("Oper", "").lower() == "up"}
        for k in up0 - now_up:
            changes[k] += 1
    record_property("links", {"up_at_start": len(up0), "samples": samples, "ports_seen_down": len(changes)})
    for (ns, port), n in sorted(changes.items())[:20]:
        checks.fail(f"{switch.name}/{ns}: {port} went down during the {watch:.0f} s watch ({n} of {samples} samples)")
    checks.check(up0, f"{switch.name}: no fabric port is up")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-046")
def test_pfc_qos_consistent_no_storm(switch, inventory, checks, record_property, evidence):
    """PFC / QoS: every fabric port has the same PORT_QOS_MAP settings (and pfc_enable = thresholds.switch.pfc_enable
    when set); PFC pause frames do not grow by more than pfc_max_pause_delta over pfc_window_s; the PFC watchdog
    detects no storm. The configuration found (e.g. 'no PFC configured') is recorded."""
    want_pfc = inventory.th("switch.pfc_enable")
    window = float(inventory.th("switch.pfc_window_s", 30))
    max_delta = float(inventory.th("switch.pfc_max_pause_delta", 0))
    nss = switch.namespaces()
    summary = {}
    for ns in nss:
        maps = {p: m for p, m in switch.qos_maps(ns).items() if switch.is_fabric_port(p)}
        variants = Counter(json.dumps(m, sort_keys=True) for m in maps.values())
        summary[ns] = {"ports_with_qos_map": len(maps), "variants": len(variants),
                       "pfc_enable": sorted({m.get("pfc_enable", "") for m in maps.values()})}
        if len(variants) > 1:
            common = variants.most_common(1)[0][0]
            odd = sorted(p for p, m in maps.items() if json.dumps(m, sort_keys=True) != common)
            checks.fail(f"{switch.name}/{ns}: {len(odd)} port(s) with a different QoS map: {odd[:8]}")
        if want_pfc is not None:
            wrong = sorted(p for p, m in maps.items() if m.get("pfc_enable", "") != str(want_pfc))
            checks.check(maps and not wrong, f"{switch.name}/{ns}: pfc_enable != {want_pfc} on {wrong[:8] or 'every port (none configured)'}")
    evidence.fact(case="pfc_qos", switch=switch.name, window_s=window, max_pause_delta=max_delta, qos=summary)
    before = {ns: (switch.pfc_counters(ns), switch.pfcwd_storms(ns)) for ns in nss}
    evidence.event("sample", what="pfc counters + pfcwd stats (before)")
    time.sleep(window)
    for ns in nss:
        after, storms = switch.pfc_counters(ns), switch.pfcwd_storms(ns)
        deltas = []
        for d in ("Rx", "Tx"):
            for port, n in after.get(d, {}).items():
                b = before[ns][0].get(d, {}).get(port)
                if b is not None and switch.is_fabric_port(port):
                    if n - b > 0:
                        deltas.append((n - b, port, d))
                    checks.check(n - b <= max_delta, f"{switch.name}/{ns}: {port} {d} PFC pause frames +{n - b:.0f} in {window:.0f} s")
        new_storms = {q: n - before[ns][1].get(q, 0) for q, n in storms.items() if n > before[ns][1].get(q, 0)}
        for q, n in storms.items():
            checks.check(n <= before[ns][1].get(q, 0), f"{switch.name}/{ns}: PFC watchdog storm detected on {q}")
        summary[ns]["pfc_watchdog_queues"] = len(storms)
        evidence.event("sample", what=f"pfc counters + pfcwd stats (after {window:.0f} s)", ns=ns,
                       ports_with_pause_growth=len(deltas),
                       top_pause_growth=[{"port": p, "dir": d, "frames": round(x)} for x, p, d in sorted(deltas, reverse=True)[:5]],
                       new_watchdog_storms=new_storms)
    record_property("pfc_qos", summary)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-063")
def test_pfc_lossless_config_complete(switch, inventory, checks, record_property):
    """Lossless (PFC) configuration complete on every ASIC: each pfc_enable priority has a lossless ingress PG
    (xoff headroom), ECN marking on its queue, a PFC watchdog; every dot1p is mapped (an unmapped one falls back to
    TC0); lossy classes are not on lossless egress profiles; no static threshold above its pool. Read-only.
    thresholds.switch.pfcwd_required / ecn_required (default true) switch off a check the design does not want.
    blantons-mr3-sw 20 Oct 2026: no pfcwd, no WRED / ECN, dot1p 2-6 unmapped, TC7 on q_lossless_profile."""
    from helios.qos import lossless_findings

    th = {"pfcwd_required": inventory.th("switch.pfcwd_required", True),
          "ecn_required": inventory.th("switch.ecn_required", True)}
    summary = {}
    for ns in switch.namespaces():
        s, findings = lossless_findings(switch.running_config(ns), switch.is_fabric_port, th)
        summary[ns or "host"] = {**{k: v for k, v in s.items() if k != "dot1p_maps"}, "findings": findings}
        if not s["ports_with_pfc"]:
            continue                                     # no PFC at all: TC-SWH-046 records 'no PFC configured'
        for f in findings:
            checks.fail(f"{switch.name}/{ns or 'host'}: {f}")
    record_property("pfc_lossless", summary)
    if not any(v["ports_with_pfc"] for v in summary.values()):
        # no lossless class at all: nothing to judge, unless the inventory says PFC is expected (as TC-SWH-046)
        checks.check(inventory.th("switch.pfc_enable") is None,
                     f"{switch.name}: no port has PFC enabled, expected pfc_enable {inventory.th('switch.pfc_enable')}")
        checks.assert_all()
        pytest.skip(f"{switch.name}: no port has PFC enabled (no lossless configuration to judge)")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-047")
def test_running_config_saved(switch, inventory, checks, record_property):
    """The running CONFIG_DB of the host and of every ASIC equals the saved /etc/sonic/config_db*.json (tables in
    thresholds.switch.config_diff_ignore excepted) - otherwise a reboot or config reload would change the switch."""
    ignore = set(inventory.th("switch.config_diff_ignore") or [])
    found = {}
    for ns in [None] + [n for n in switch.namespaces() if n]:
        run, saved = switch.config_running_vs_saved(ns)
        for t in sorted((set(run) | set(saved)) - ignore):
            a, b = run.get(t, {}), saved.get(t, {})
            if a == b:
                continue
            only_run, only_saved = sorted(set(a) - set(b)), sorted(set(b) - set(a))
            changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
            where = f"{switch.name}/{ns or 'host'}"
            found[f"{ns or 'host'}/{t}"] = {"only_running": len(only_run), "only_saved": len(only_saved), "changed": len(changed)}
            checks.fail(f"{where}: {t} differs from the saved config - only running {len(only_run)} {only_run[:2]}, "
                        f"only saved {len(only_saved)} {only_saved[:2]}, changed {len(changed)} {changed[:2]}")
    record_property("config_diff", found)
    checks.assert_all()


# ================================================================== TC-SWH-049, 051..059 (added 14 Oct 2026, third batch)
# thresholds.switch: require_host_tx_ready (false: on QFX5252 without optics host_tx_ready stays false on up ports);
# queue_window_s (30), queue_max_drop_delta (0), queue_watermark_max_bytes (null = record only);
# crm_max_used_pct (80); mgmt_window_s (60), mgmt_iface (eth0); disk_max_pct (80), mem_min_available_pct (10),
# load_max_per_cpu (2.0), rss_window_s (60), rss_max_growth_kb (20480), rss_processes; watchdog_armed (true),
# ssd_min_health_pct (80)

def _fabric_up(ps: dict) -> bool:
    return ps.get("netdev_oper_status", "").lower() == "up"


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-049")
def test_fabric_port_state_healthy(switch, inventory, checks, record_property):
    """Every up fabric port: link training trained (or off), RS-FEC, oper speed = configured speed, and - only where
    thresholds.switch.require_host_tx_ready - host_tx_ready true. Ports' flap counts are recorded."""
    need_htx = bool(inventory.th("switch.require_host_tx_ready", False))
    want_fec = str(inventory.th("switch.fec", "rs"))
    summary, flaps = Counter(), {}
    for ns in switch.namespaces():
        for port, st in switch.port_states(ns).items():
            if not _fabric_up(st):
                continue
            where = f"{switch.name}/{ns}: {port}"
            summary["up"] += 1
            summary[f"host_tx_ready={st.get('host_tx_ready', '?')}"] += 1
            lt = st.get("link_training_status", "")
            checks.check(lt in ("trained", "off", ""), f"{where} up but link training '{lt}'")
            if st.get("fec"):
                checks.check(st["fec"] == want_fec, f"{where} FEC {st['fec']}, expected {want_fec}")
            if st.get("speed") and st.get("cfg_speed"):
                checks.check(st["speed"] == st["cfg_speed"], f"{where} speed {st['speed']} != configured {st['cfg_speed']}")
            if need_htx:
                checks.check(st.get("host_tx_ready") == "true", f"{where} host_tx_ready {st.get('host_tx_ready')}")
            if (st.get("flap_count") or "0").isdigit() and int(st["flap_count"]) > 0:
                flaps[f"{ns}/{port}"] = int(st["flap_count"])
    record_property("ports", dict(summary))
    record_property("flap_counts_top", dict(sorted(flaps.items(), key=lambda kv: -kv[1])[:10]))
    checks.check(summary["up"], f"{switch.name}: no up fabric port")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-051")
def test_vpod_isolation(switch, inventory, checks, record_property):
    """No fabric port is a member of two VLANs, and every VLAN with members has MAC learning disabled."""
    for ns in switch.namespaces():
        members = switch.vlan_members(ns)
        where: dict[str, list[str]] = {}
        for vlan, ports in members.items():
            for p in ports:
                where.setdefault(p, []).append(vlan)
        multi = {p: v for p, v in where.items() if len(v) > 1}
        checks.check(not multi, f"{switch.name}/{ns}: ports in several VLANs: {dict(list(multi.items())[:6])}")
        for vlan, ports in members.items():
            if ports:
                ld = switch.vlan_hash(vlan, ns).get("learn_disable", "")
                checks.check(ld == "true", f"{switch.name}/{ns}: {vlan} ({len(ports)} members) has MAC learning enabled (learn_disable={ld!r})")
        record_property(f"vlans_{ns}", {v: len(p) for v, p in members.items()})
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-052")
def test_queue_drops_and_watermarks(switch, inventory, checks, record_property):
    """No fabric queue drop counter grows by more than queue_max_drop_delta over queue_window_s; unicast-queue
    watermarks stay below queue_watermark_max_bytes when set (always recorded)."""
    window = float(inventory.th("switch.queue_window_s", 30))
    max_delta = float(inventory.th("switch.queue_max_drop_delta", 0))
    wm_max = inventory.th("switch.queue_watermark_max_bytes")
    nss = switch.namespaces()
    before = {ns: switch.queue_counters(ns) for ns in nss}
    time.sleep(window)
    worst = {}
    for ns in nss:
        for (port, q), n in switch.queue_counters(ns).items():
            b = before[ns].get((port, q))
            if b is not None and switch.is_fabric_port(port):
                checks.check(n - b <= max_delta, f"{switch.name}/{ns}: {port} {q} +{n - b:.0f} dropped packets in {window:.0f} s")
        for port, wm in switch.queue_watermarks(ns).items():
            worst[f"{ns}/{port}"] = wm
            if wm_max is not None:
                checks.check(wm <= float(wm_max), f"{switch.name}/{ns}: {port} queue watermark {wm:.0f} B > {wm_max}")
    record_property("watermark_top_bytes", dict(sorted(worst.items(), key=lambda kv: -kv[1])[:5]))
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-053")
def test_asic_resources(switch, inventory, checks, record_property):
    """Every CRM resource with capacity is below crm_max_used_pct (80 %) used."""
    limit = float(inventory.th("switch.crm_max_used_pct", 80))
    crm = switch.crm_resources()
    checks.check(crm, f"{switch.name}: 'crm show resources all' returned nothing")
    top = {}
    for asic, res in crm.items():
        for name, (used, avail) in res.items():
            if used + avail == 0:
                continue
            pct = 100.0 * used / (used + avail)
            top[f"{asic}/{name}"] = round(pct, 2)
            checks.check(pct <= limit, f"{switch.name}/{asic}: {name} {pct:.1f}% used ({used} of {used + avail})")
    record_property("crm_top_pct", dict(sorted(top.items(), key=lambda kv: -kv[1])[:5]))
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-054")
def test_management_interface_healthy(switch, inventory, checks, record_property):
    """The management interface is UP, its error/drop counters and carrier changes do not grow over mgmt_window_s,
    and a default route exists."""
    iface = str(inventory.th("switch.mgmt_iface", "eth0"))
    window = float(inventory.th("switch.mgmt_window_s", 60))
    a = switch.iface_stats(iface)
    time.sleep(window)
    b = switch.iface_stats(iface)
    record_property("mgmt", {"start": a, "end": b})
    checks.check(b.get("state") == "UP", f"{switch.name}: {iface} state {b.get('state')}")
    for k in ("rx_errors", "rx_dropped", "tx_errors", "tx_dropped", "carrier_changes"):
        if k in a and k in b:
            checks.check(b[k] <= a[k], f"{switch.name}: {iface} {k} +{b[k] - a[k]} in {window:.0f} s")
    checks.check(switch.default_route(), f"{switch.name}: no default route")
    checks.assert_all()


def _num(v):
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)", str(v or ""))
    return float(m.group(1)) if m else None


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-055")
def test_sensors_within_thresholds(switch, inventory, checks, record_property):
    """Every temperature and voltage sensor reads between its low and high thresholds (where set) and raises no
    warning."""
    n = 0
    for kind, rows, val_col in (("temperature", switch.temperatures(), "Temperature"),
                                ("voltage", switch.voltages(), "Voltage")):
        for r in rows:
            v, hi, lo = _num(r.get(val_col)), _num(r.get("High TH")), _num(r.get("Low TH"))
            if v is None:
                continue
            n += 1
            name = r.get("Sensor", "?")
            if hi is not None:
                checks.check(v <= hi, f"{switch.name}: {kind} {name} {v} above high threshold {hi}")
            if lo is not None:
                checks.check(v >= lo, f"{switch.name}: {kind} {name} {v} below low threshold {lo}")
            checks.check(str(r.get("Warning", "False")).strip().lower() != "true", f"{switch.name}: {kind} {name} warning raised")
    record_property("sensors_checked", n)
    checks.check(n, f"{switch.name}: no sensor readings")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-056")
def test_resources_and_memory_growth(switch, inventory, checks, record_property):
    """Disk use on /, /var/log and /host <= disk_max_pct (80 %), available memory >= mem_min_available_pct (10 %),
    1-minute load <= load_max_per_cpu x CPUs, and syncd / orchagent / switch-agent / sb-node-agent RSS does not grow by
    more than rss_max_growth_kb over rss_window_s."""
    for path, pct in switch.disk_usage().items():
        checks.check(pct <= float(inventory.th("switch.disk_max_pct", 80)), f"{switch.name}: {path} {pct}% full")
    m = switch.memory_load()
    if m.get("MemTotal"):
        avail = 100.0 * m.get("MemAvailable", 0) / m["MemTotal"]
        checks.check(avail >= float(inventory.th("switch.mem_min_available_pct", 10)), f"{switch.name}: only {avail:.1f}% memory available")
    checks.check(m["load1"] <= float(inventory.th("switch.load_max_per_cpu", 2.0)) * m["nproc"],
                 f"{switch.name}: load {m['load1']} on {m['nproc']:.0f} CPUs")
    names = tuple(inventory.th("switch.rss_processes") or ("syncd", "orchagent", "switch-agent", "sb-node-agent"))
    a = switch.process_rss(names)
    time.sleep(float(inventory.th("switch.rss_window_s", 60)))
    b = switch.process_rss(names)
    grow = float(inventory.th("switch.rss_max_growth_kb", 20480))
    for name in sorted(set(a) & set(b)):
        checks.check(b[name] - a[name] <= grow, f"{switch.name}: {name} RSS grew {b[name] - a[name]} kB during the watch")
    record_property("resources", {"memory_available_pct": round(100.0 * m.get("MemAvailable", 0) / m.get("MemTotal", 1), 1),
                                  "load1": m["load1"], "rss_kb": b})
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-057")
def test_critical_processes_running(switch, inventory, checks, record_property):
    """In every running container that declares /etc/supervisor/critical_processes, each listed program is RUNNING."""
    seen = 0
    for c in switch.containers():
        if not switch.critical_processes(c):
            continue
        seen += 1
        for d in switch.critical_down(c):     # dsserve-style wrappers count when their process runs
            prog, _, state = d.partition(":")
            checks.fail(f"{switch.name}: {c}/{prog} is {state}")
    record_property("containers_checked", seen)
    checks.check(seen, f"{switch.name}: no container declares critical processes")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-058")
def test_watchdog_and_ssd(switch, inventory, checks, record_property):
    """The hardware watchdog is armed (thresholds.switch.watchdog_armed, true) and the SSD health is at least
    ssd_min_health_pct (80 %)."""
    wd = switch.watchdog_status()
    ssd = switch.ssd_health()
    record_property("watchdog", wd[:120])
    record_property("ssd", ssd)
    if inventory.th("switch.watchdog_armed", True):
        checks.check(re.search(r"Status:\s*Armed", wd) is not None, f"{switch.name}: hardware watchdog not armed ({wd[:80]!r})")
    h = _num(ssd.get("Health"))
    checks.check(h is not None, f"{switch.name}: SSD health not reported")
    if h is not None:
        checks.check(h >= float(inventory.th("switch.ssd_min_health_pct", 80)), f"{switch.name}: SSD health {h}%")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-059")
def test_firmware_consistent_across_switches(rack, inventory, checks, record_property):
    """Every firmware component (BIOS, BMC, CPLDs, FPGAs) has the same version on all switch trays."""
    sws = list(rack.switches.values())
    if len(sws) < 2:
        pytest.skip("needs at least 2 switch trays")
    fw = {}
    for sw, rows, exc in fan_out(lambda s: s.firmware_status(), sws):
        if checks.check(exc is None, f"{sw.name}: cannot read firmware status: {exc}"):
            fw[sw.name] = {r.get("Component"): r.get("Version") for r in rows if r.get("Component")}
    comps = sorted(set().union(*fw.values())) if fw else []
    for comp in comps:
        _majority(f"{comp} firmware", {n: v.get(comp, "absent") for n, v in fw.items()}, checks)
    record_property("firmware", fw)
    checks.assert_all()
