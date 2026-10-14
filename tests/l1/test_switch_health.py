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
    for ns in switch.namespaces():
        desc = switch.port_descriptions(ns)
        members = {p for ports in switch.vlan_members(ns).values() for p in ports}
        for r in switch.interfaces_status(ns):
            port = r["Interface"]
            cab = parse_port_description(desc.get(port, ""))
            mode, admin = r.get("Vlan", "").lower(), r.get("Admin", "").lower()
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
    has a docker restart count or started more than restart_slack_s after the others (= restarted)."""
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
    for c in expected:
        if c not in info:
            continue
        count, iso = info[c]
        checks.check(count == 0, f"{switch.name}: {c} restarted {count} time(s) by docker")
        t = _ts(iso)
        if first is not None and t is not None:
            checks.check(t - first <= slack, f"{switch.name}: {c} started {(t - first) / 60:.0f} min after the "
                                             f"first container (restarted at {iso[:19]})")
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
def test_pfc_qos_consistent_no_storm(switch, inventory, checks, record_property):
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
    before = {ns: (switch.pfc_counters(ns), switch.pfcwd_storms(ns)) for ns in nss}
    time.sleep(window)
    for ns in nss:
        after, storms = switch.pfc_counters(ns), switch.pfcwd_storms(ns)
        for d in ("Rx", "Tx"):
            for port, n in after.get(d, {}).items():
                b = before[ns][0].get(d, {}).get(port)
                if b is not None and switch.is_fabric_port(port):
                    checks.check(n - b <= max_delta, f"{switch.name}/{ns}: {port} {d} PFC pause frames +{n - b:.0f} in {window:.0f} s")
        for q, n in storms.items():
            checks.check(n <= before[ns][1].get(q, 0), f"{switch.name}/{ns}: PFC watchdog storm detected on {q}")
        summary[ns]["pfc_watchdog_queues"] = len(storms)
    record_property("pfc_qos", summary)
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
