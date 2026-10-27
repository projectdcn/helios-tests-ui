"""TC-SWH-037T..062T, TC-SWH-063..107 - switch-tray cases from AMD's Helios ScaleUp test-tracking sheet
(Helios_ScaleUp_Test_Tracking_Trimmed.xlsx, "Switch HW"), merged into the test plan on 26 Oct 2026.

The tracking sheet reuses TC-SWH-037..062 for different cases than the ones helios-tests added on 14 Oct
(tests/l1/test_switch_health.py), so those 26 run here as TC-SWH-037T..062T; the tracking ID is kept in the
plan's "Tracking TID" column and as ``tracking_id`` in testplan/added.yaml.

Every test is parametrized per switch tray (except TC-SWH-037T, which drives traffic across the rack). Read-only
unless marked disruptive (config load/reload/save/restore, image cleanup/remove, process and service restarts).

Thresholds (inventory ``thresholds.switch``; a ``null`` disables that comparison):
  health_ignore                    040: system-health components whose status is ignored (regexes)
  voltage_margin_pct (0)           042: extra margin inside the voltage Low/High TH
  core_ignore, failed_units_ignore 044 / 045: file / unit name regexes that are tolerated
  expected_containers              046: containers that must be up ('{n}' = each ASIC index; default as TC-SWH-041)
  fan_redundancy.required          050: fans the chassis needs (default: all but one - redundancy = one spare OK)
  psu_efficiency.{min, max}        053: output / input power (0.80 .. 1.00)
  drops.implausible_ratio (1.0)    054: RX_DRP / RX_OK above this is implausible
  drops.max_ratio (0.001)          055: lifetime drops / OK on an up fabric port
  expected_speed (400000)          058: configured and oper speed of every up fabric port
  critical_services                059: systemd units ('{n}' = ASIC index)
  cpu_max_pct (90), mem_max_pct (90), disk_max_pct (85), load_per_cpu_max (2.0)   061 / 062
  min_uptime_s (600), expected_reboot_causes   063
  expected_sonic_version           065 (default: the version most switch trays of the rack run)
  fru_json_path (/etc/sonic/fru.json), fru_num_mac (512)                         079
  auth_fail_max (0), auth_fail_ignore                                            080
  boot_log_ignore                  081: kernel / journal message regexes that are tolerated
  firewall_min_input_rules (1)     083
  ssh.permit_root_login (no, prohibit-password, without-password), ssh.max_auth_tries (6)   084
  lockdown_probe_vlan (4093)       085: VLAN id a non-root 'config vlan add' must be refused for
  allowed_uid0 ([root]), allowed_users   086
  fec_window_s (10)                092
  disk_write_mb_s_min (20), disk_write_path (/host)   098
  fan_target_tolerance_pct (20)    099
  allow_image_cleanup (false), removable_image   100 / 101
  recover_timeout_s (300) / reload_timeout_s (900), poll_interval_s (10)   094..097, 104..106
  allow_config_reload (false), allow_config_save (false)   095 / 097 run only when true (they change AFM's switch config)
  restart_check_service (lldp)     105
  techsupport_timeout_s (1800), techsupport_since ('1 hour ago'), techsupport_required   107
``thresholds.platform.dns_names`` (064) and ``commands.switch.dsc_dump`` (058, e.g. a bcmcmd lane dump per
ASIC, '{asic}' = index) are optional.
"""
from __future__ import annotations

import json
import re
import shlex
import time
from collections import Counter

import pytest

from helios import datapath
from helios.afmctl import parse_port_description
from helios.parsers import parse_table, to_float
from helios.rack import fan_out
from helios.wait import wait_until

DEFAULT_CONTAINERS = ["database", "pmon", "lldp", "database{n}", "swss{n}", "syncd{n}", "lldp{n}", "afm-agent"]
DEFAULT_SERVICES = ["database", "pmon", "lldp", "database@{n}", "swss@{n}", "syncd@{n}", "lldp@{n}"]
DEFAULT_REBOOT_CAUSES = [r"User issued '?(warm-|fast-)?reboot", r"^reboot$", r"warm-reboot", r"fast-reboot",
                         r"^config reload"]
INSTALLER_CMDS = ["install", "list", "remove", "set-default", "set-next-boot", "cleanup", "binary-version"]
BAD_HOSTNAMES = {"", "sonic", "localhost", "localhost.localdomain"}
RFC1123 = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}"
                     r"[a-zA-Z0-9])?)*$")
AUTH_FAIL_RE = r"authentication failure|Failed password|Invalid user"
SAVED = "/etc/sonic/config_db{}.json"


# ----------------------------------------------------------------- helpers

def _expand(names, switch) -> list[str]:
    out = []
    for n in names:
        if "{n}" in n:
            out += [n.replace("{n}", ns.removeprefix("asic")) for ns in switch.namespaces() if ns]
        else:
            out.append(n)
    return out


def _ignored(text: str, patterns) -> bool:
    return any(re.search(p, text) for p in patterns or [])


def _ok(status: str) -> bool:
    return (status or "").strip().upper() == "OK"


def _fan_name(row: dict) -> str:
    return row.get("FAN") or row.get("Drawer") or "?"


def _is_psu_fan(row: dict) -> bool:
    return "psu" in f"{row.get('Drawer', '')} {row.get('FAN', '')}".lower()


def _num(v) -> float | None:
    return to_float(str(v).replace(",", "")) if v not in (None, "") else None


def _up_ports(switch, ns) -> set[str]:
    return {r["Interface"] for r in switch.interfaces_status(ns) if r.get("Oper", "").lower() == "up"}


def _oper_up(switch) -> set[tuple]:
    return {(ns, p) for ns in switch.namespaces() for p in _up_ports(switch, ns)}


def _saved_files(switch) -> list[str]:
    return [SAVED.format(ns[4:] if ns else "") for ns in [None, *[n for n in switch.namespaces() if n]]]


def _sha(switch, paths) -> dict[str, str]:
    """{path: sha256} - one 'sha256sum < path' per file ('' when unreadable)."""
    out = {}
    for p in paths:
        r = switch.run(f"sha256sum < {shlex.quote(p)}", sudo=True, timeout=120)
        out[p] = r.stdout.split()[0] if r.ok and r.stdout.strip() else ""
    return out


def _running_config(switch, ns) -> dict:
    flag = f" -n {ns}" if ns else ""
    return json.loads(switch.sh(f"sonic-cfggen{flag} -d --print-data", sudo=True, timeout=180))


def _poll(inventory, key="recover_timeout_s", default=300) -> tuple[float, float]:
    return float(inventory.th(f"switch.{key}", default)), float(inventory.th("switch.poll_interval_s", 10))


def _recovered(switch, up_before: set, why: list, bases=("swss", "syncd")) -> bool:
    running = set(switch.containers())
    for ns in switch.namespaces():
        for base in bases:
            ctr = switch.per_asic(base, ns)
            if ctr not in running:
                why[:] = [f"{ctr} not running"]
                return False
            down = switch.critical_down(ctr)
            if down:
                why[:] = [f"{ctr} critical {down}"]
                return False
    lost = up_before - _oper_up(switch)
    why[:] = [f"{len(lost)} port(s) up before still down: {sorted(p[1] for p in lost)[:8]}"] if lost else []
    return not lost


def _wait_recovered(switch, inventory, checks, up_before, what, key="recover_timeout_s", default=300, **kw):
    timeout, interval = _poll(inventory, key, default)
    why: list = []
    ok, _ = wait_until(lambda: _recovered(switch, up_before, why, **kw), timeout, interval, what=f"{switch.name} {what}")
    checks.check(ok, f"{switch.name}: not recovered within {timeout:.0f}s after {what}: {'; '.join(why)}")


def _health_detail(switch) -> list[dict]:
    """Rows of the 'System services and devices monitor list' of 'show system-health detail'."""
    r = switch.run("show system-health detail", sudo=True, timeout=180)
    text = r.stdout
    assert r.ok and "Traceback" not in text + r.stderr, f"{switch.name}: show system-health detail failed: {r.tail()!r}"
    m = re.search(r"monitor list\s*\n(.*?)(?:\n\s*System services and devices ignore list|\Z)", text, re.S)
    return parse_table(m.group(1)) if m else []


def _uptime_s(switch) -> float:
    return float(switch.sh("cat /proc/uptime", timeout=30).split()[0])


# ---------------------------------------------------------- traffic (037T)

@pytest.mark.l2
@pytest.mark.datapath(tools=["transferbench"], min_computes=2)
@pytest.mark.tc("TC-SWH-037T")
def test_interface_counters_traffic_flow(datapath_ready, rack, computes, inventory, checks, record_property):
    """Tracking TC-SWH-037: cross-domain TransferBench between two vPOD computes; the fabric ports' RX_OK / TX_OK
    counters must grow on every switch tray carrying the traffic (at least one up port per switch)."""
    def snap(sw):
        return {ns: sw.interfaces_counters(ns) for ns in sw.namespaces()}

    before = {sw.name: r for sw, r, _ in fan_out(snap, rack.switches.values())}
    record_property("bw_gbps", datapath.transferbench_cross_domain(computes[:2], inventory, checks))
    after = {sw.name: r for sw, r, _ in fan_out(snap, rack.switches.values())}
    grew = {}
    for name, per_ns in after.items():
        n = 0
        for ns, rows in (per_ns or {}).items():
            for port, row in rows.items():
                b = ((before.get(name) or {}).get(ns) or {}).get(port) or {}
                for col in ("RX_OK", "TX_OK"):
                    d0, d1 = _num(b.get(col)), _num(row.get(col))
                    if d0 is not None and d1 is not None and d1 > d0:
                        n += 1
                        break
        grew[name] = n
        checks.check(n > 0, f"{name}: no fabric port counter grew during the traffic")
    record_property("ports_with_traffic", grew)
    checks.assert_all()


# ------------------------------------------------- platform health 038T..046T

@pytest.mark.l1
@pytest.mark.tc("TC-SWH-038T")
def test_temperature_sensors_below_critical(switch, checks):
    """Tracking TC-SWH-038: no sensor at or above its critical-high threshold and no sensor in warning."""
    rows = switch.temperatures()
    assert rows, f"{switch.name}: 'show platform temperature' returned no sensors"
    for r in rows:
        t, crit = _num(r.get("Temperature")), _num(r.get("Crit High TH"))
        name = r.get("Sensor", "?")
        if t is not None and crit is not None:
            checks.check(t < crit, f"{switch.name}: {name} {t} C >= critical {crit} C")
        checks.check(str(r.get("Warning", "")).strip().lower() != "true", f"{switch.name}: {name} in warning ({t} C)")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-039T")
def test_system_health_summary_ok(switch, record_property):
    """Tracking TC-SWH-039: 'show system-health summary' runs and reports Services and Hardware status OK."""
    ok, text = switch.system_health()
    record_property("summary", text[-1500:])
    assert ok, f"{switch.name}: show system-health summary failed: {text[-300:]!r}"
    status = re.findall(r"^\s*(Services|Hardware):\s*\n\s*Status:\s*(\S.*?)\s*$", text, re.M)
    assert {s for s, _ in status} >= {"Services", "Hardware"}, f"{switch.name}: no Services/Hardware status in summary"
    bad = [f"{s}={v}" for s, v in status if not _ok(v)]
    assert not bad, f"{switch.name}: system health not OK: {bad}"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-040T")
def test_system_health_detail_all_ok(switch, inventory, checks, record_property):
    """Tracking TC-SWH-040: every component of the system-health monitor list reports OK."""
    rows = _health_detail(switch)
    assert rows, f"{switch.name}: system-health detail lists no monitored components"
    ignore = inventory.th("switch.health_ignore") or []
    bad = [f"{r.get('Name')}={r.get('Status')}" for r in rows
           if not _ok(r.get("Status", "")) and not _ignored(r.get("Name", ""), ignore)]
    record_property("components", len(rows))
    checks.check(not bad, f"{switch.name}: components not OK: {bad[:20]}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-041T")
def test_system_health_monitor_running(switch, checks):
    """Tracking TC-SWH-041: the system-health service is active and has populated STATE_DB SYSTEM_HEALTH_INFO."""
    st = switch.run("systemctl is-active system-health", timeout=30).stdout.strip()
    checks.check(st == "active", f"{switch.name}: system-health service is {st or 'unknown'}")
    keys = switch.db_keys("STATE_DB", "SYSTEM_HEALTH_INFO*")
    checks.check(keys, f"{switch.name}: STATE_DB has no SYSTEM_HEALTH_INFO")
    if keys:
        info = switch.db_hash("STATE_DB", keys[0])
        checks.check(info, f"{switch.name}: {keys[0]} is empty")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-042T")
def test_environment_sensors_within_thresholds(switch, inventory, checks, record_property):
    """Tracking TC-SWH-042: every voltage sensor inside its Low/High thresholds (switch.voltage_margin_pct) and
    every temperature sensor below its High threshold."""
    margin = float(inventory.th("switch.voltage_margin_pct", 0) or 0) / 100
    volts = switch.voltages()
    checks.check(volts, f"{switch.name}: 'show platform voltage' returned no sensors")
    for r in volts:
        v = _num(re.sub(r"[^\d.\-]", "", r.get("Voltage", "")))
        lo, hi = _num(r.get("Low TH")), _num(r.get("High TH"))
        if v is None or lo is None or hi is None:
            continue
        span = (hi - lo) * margin
        checks.check(lo + span <= v <= hi - span, f"{switch.name}: {r.get('Sensor')} {v} outside [{lo}, {hi}]")
    for r in switch.temperatures():
        t, hi = _num(r.get("Temperature")), _num(r.get("High TH"))
        if t is not None and hi is not None:
            checks.check(t < hi, f"{switch.name}: {r.get('Sensor')} {t} C >= high threshold {hi} C")
    record_property("voltage_sensors", len(volts))
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-043T")
def test_watchdog_manageable(switch, checks, record_property):
    """Tracking TC-SWH-043: watchdogutil offers status/arm/disarm and reports a definite state."""
    helptext = switch.run("watchdogutil --help", sudo=True, timeout=60).stdout
    for c in ("status", "arm", "disarm"):
        checks.check(re.search(rf"^\s*{c}\b", helptext, re.M), f"{switch.name}: watchdogutil has no '{c}' command")
    st = switch.watchdog_status()
    record_property("watchdog", st)
    checks.check(re.search(r"Status:\s*(Armed|Unarmed)", st), f"{switch.name}: watchdog status unreadable: {st[:200]!r}")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-044T")
def test_no_core_dumps(switch, inventory, record_property):
    """Tracking TC-SWH-044: /var/core holds no core files (switch.core_ignore patterns excepted)."""
    cores = [c for c in switch.core_files() if not _ignored(c, inventory.th("switch.core_ignore"))]
    record_property("cores", cores[:50])
    assert not cores, f"{switch.name}: {len(cores)} core file(s) in /var/core: {cores[:10]}"


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-045T")
def test_no_failed_systemd_units(switch, inventory, record_property):
    """Tracking TC-SWH-045: 'systemctl list-units --failed' is empty (switch.failed_units_ignore excepted)."""
    r = switch.run("systemctl list-units --failed --no-legend --plain", timeout=60)
    assert r.ok, f"{switch.name}: systemctl list-units failed rc={r.rc}"
    units = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    failed = [u for u in units if not _ignored(u, inventory.th("switch.failed_units_ignore"))]
    record_property("failed_units", units)
    assert not failed, f"{switch.name}: failed systemd units: {failed}"


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-046T")
def test_docker_containers_healthy(switch, inventory, checks, record_property):
    """Tracking TC-SWH-046: every expected container exists and is Up; no container is restarting, exited or
    unhealthy."""
    out = switch.sh("docker ps -a --format '{{.Names}}|{{.Status}}'", sudo=True, timeout=60)
    status = dict(ln.split("|", 1) for ln in out.splitlines() if "|" in ln)
    record_property("containers", status)
    for name in _expand(inventory.th("switch.expected_containers") or DEFAULT_CONTAINERS, switch):
        checks.check(name in status, f"{switch.name}: container {name} missing")
    for name, st in status.items():
        checks.check(st.startswith("Up") and "unhealthy" not in st and "Restarting" not in st,
                     f"{switch.name}: container {name} is '{st}'")
    checks.assert_all()


# -------------------------------------------------------------- fans 047T..052T

@pytest.mark.l2
@pytest.mark.tc("TC-SWH-047T")
def test_fan_led_matches_status(switch, checks):
    """Tracking TC-SWH-047: a fan with status OK shows a green LED, any other status a non-green LED."""
    rows = [r for r in switch.fans() if r.get("LED", "N/A").strip().upper() not in ("", "N/A")]
    if not rows:
        pytest.skip(f"{switch.name}: the platform reports no fan LED colour ('show platform fan' LED is N/A)")
    for r in rows:
        led = r["LED"].strip().lower()
        if _ok(r.get("Status", "")):
            checks.check(led == "green", f"{switch.name}: {_fan_name(r)} OK but LED {led}")
        else:
            checks.check(led != "green", f"{switch.name}: {_fan_name(r)} {r.get('Status')} but LED green")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-048T")
def test_all_fans_operational(switch, checks):
    """Tracking TC-SWH-048: every fan (chassis and PSU) reports status OK."""
    rows = switch.fans()
    assert rows, f"{switch.name}: no fans reported"
    for r in rows:
        checks.check(_ok(r.get("Status", "")), f"{switch.name}: {_fan_name(r)} status {r.get('Status')!r}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-049T")
def test_fan_drawers_present_ok(switch, checks, record_property):
    """Tracking TC-SWH-049: every chassis fan drawer is present and all its fans are OK."""
    drawers: dict[str, list[dict]] = {}
    for r in switch.fans():
        if not _is_psu_fan(r):
            drawers.setdefault(r.get("Drawer") or "?", []).append(r)
    assert drawers, f"{switch.name}: no chassis fan drawers reported"
    record_property("drawers", {d: len(f) for d, f in drawers.items()})
    for d, fans in drawers.items():
        for f in fans:
            checks.check(f.get("Presence", "").strip().lower() == "present", f"{switch.name}: {d}/{_fan_name(f)} not present")
            checks.check(_ok(f.get("Status", "")), f"{switch.name}: {d}/{_fan_name(f)} status {f.get('Status')!r}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-050T")
def test_fan_redundancy(switch, inventory, record_property):
    """Tracking TC-SWH-050: the chassis still has a spare fan - OK fans exceed switch.fan_redundancy.required
    (default: all chassis fans but one), so it survives one more fan failure."""
    fans = [r for r in switch.fans() if not _is_psu_fan(r)]
    assert len(fans) >= 2, f"{switch.name}: {len(fans)} chassis fan(s): no redundancy possible"
    required = inventory.th("switch.fan_redundancy.required")
    required = len(fans) - 1 if required is None else int(required)
    ok = [f for f in fans if _ok(f.get("Status", "")) and f.get("Presence", "").strip().lower() == "present"]
    record_property("fans", {"total": len(fans), "ok": len(ok), "required": required})
    assert len(ok) > required, (f"{switch.name}: {len(ok)}/{len(fans)} chassis fans OK, {required} required - "
                                f"a further fan failure is not survivable")


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-051T")
def test_fan_airflow_direction_uniform(switch, record_property):
    """Tracking TC-SWH-051: all fans that report an airflow direction report the same one."""
    dirs = Counter(r.get("Direction", "").strip().lower() for r in switch.fans()
                   if r.get("Direction", "").strip().upper() not in ("", "N/A"))
    record_property("directions", dict(dirs))
    if not dirs:
        pytest.skip(f"{switch.name}: no fan reports an airflow direction")
    assert len(dirs) == 1, f"{switch.name}: mixed fan airflow directions {dict(dirs)}"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-052T")
def test_fan_rpm_readable_nonzero(switch, checks, record_property):
    """Tracking TC-SWH-052: STATE_DB FAN_INFO has a numeric, non-zero speed for every fan."""
    info = switch.fan_info()
    assert info, f"{switch.name}: STATE_DB has no FAN_INFO"
    speeds = {}
    for name, d in info.items():
        v = _num(d.get("speed_rpm") or d.get("speed"))
        speeds[name] = v
        if checks.check(v is not None, f"{switch.name}: {name} speed unreadable ({d.get('speed')!r})"):
            checks.check(v > 0, f"{switch.name}: {name} speed is 0")
    record_property("speeds", speeds)
    checks.assert_all()


# ------------------------------------------------------------------- PSU 053T

@pytest.mark.l2
@pytest.mark.tc("TC-SWH-053T")
def test_psu_efficiency(switch, inventory, checks, record_property):
    """Tracking TC-SWH-053: output power / (input voltage x input current) of every OK PSU is within
    switch.psu_efficiency.{min,max} (0.80 .. 1.00). Skips when the PSUs report no input readings (busbar-fed)."""
    lo = float(inventory.th("switch.psu_efficiency.min", 0.80))
    hi = float(inventory.th("switch.psu_efficiency.max", 1.00))
    eff = {}
    for name, d in switch.psu_info().items():
        vin, iin, pout = _num(d.get("input_voltage")), _num(d.get("input_current")), _num(d.get("power"))
        if None in (vin, iin, pout) or vin * iin <= 0:
            continue
        eff[name] = round(pout / (vin * iin), 3)
        checks.check(lo <= eff[name] <= hi, f"{switch.name}: {name} efficiency {eff[name]:.2f} outside [{lo}, {hi}]")
    if not eff:
        pytest.skip(f"{switch.name}: no PSU reports input voltage/current (PSU_INFO input_voltage/input_current)")
    record_property("efficiency", eff)
    checks.assert_all()


# ------------------------------------------------------ interfaces 054T..058T

@pytest.mark.l2
@pytest.mark.tc("TC-SWH-054T")
def test_drop_counter_plausibility(switch, inventory, checks):
    """Tracking TC-SWH-054: on up fabric ports RX_DRP is not implausibly high against RX_OK
    (ratio > switch.drops.implausible_ratio, default 1.0)."""
    limit = float(inventory.th("switch.drops.implausible_ratio", 1.0))
    for ns in switch.namespaces():
        up = _up_ports(switch, ns)
        for port, row in switch.interfaces_counters(ns).items():
            drp, ok = _num(row.get("RX_DRP")), _num(row.get("RX_OK"))
            if port in up and drp is not None and ok is not None and drp > 0:
                checks.check(drp <= ok * limit, f"{switch.name}/{ns}: {port} RX_DRP {drp:.0f} vs RX_OK {ok:.0f}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-055T")
def test_no_excessive_drops(switch, inventory, checks):
    """Tracking TC-SWH-055: lifetime RX and TX drops on every up fabric port are at most switch.drops.max_ratio
    (default 0.1%) of the packets."""
    limit = float(inventory.th("switch.drops.max_ratio", 0.001))
    for ns in switch.namespaces():
        up = _up_ports(switch, ns)
        for port, row in switch.interfaces_counters(ns).items():
            if port not in up:
                continue
            for d, o in (("RX_DRP", "RX_OK"), ("TX_DRP", "TX_OK")):
                drp, ok = _num(row.get(d)), _num(row.get(o))
                if drp and ok:
                    checks.check(drp / ok <= limit, f"{switch.name}/{ns}: {port} {d}/{o} = {drp / ok:.4%} > {limit:.2%}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-056T")
def test_error_counters_readable(switch, checks):
    """Tracking TC-SWH-056: 'show interfaces counters errors' lists every fabric port with numeric counters."""
    for ns in switch.namespaces():
        flag = f" -n {ns}" if ns else ""
        r = switch.run(f"show interfaces counters errors{flag} -d all", timeout=120)
        if not checks.check(r.ok, f"{switch.name}/{ns}: show interfaces counters errors rc={r.rc} {r.tail()!r}"):
            continue
        rows = {x.get("IFACE"): x for x in parse_table(r.stdout) if switch.is_fabric_port(x.get("IFACE", ""))}
        missing = sorted({x["Interface"] for x in switch.interfaces_status(ns)} - set(rows))
        checks.check(not missing, f"{switch.name}/{ns}: no error counters for {missing[:10]}")
        for port, x in rows.items():
            bad = [c for c in ("RX_ERR", "TX_ERR") if _num(x.get(c)) is None]
            checks.check(not bad, f"{switch.name}/{ns}: {port} unreadable {bad}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-057T")
def test_transceiver_presence_matches_medium(switch, inventory, checks, record_property):
    """Tracking TC-SWH-057: a port whose type names a pluggable module (OSFP/QSFP/SFP) has one present; a
    backplane port (type N/A or switch.port_medium: backplane) reports not present."""
    forced = (inventory.th("switch.port_medium") or "").lower()
    seen = Counter()
    for ns in switch.namespaces():
        pres = switch.transceiver_presence(ns)
        for r in switch.interfaces_status(ns):
            port, typ = r["Interface"], r.get("Type", "").strip()
            pluggable = forced != "backplane" and re.match(r"(OSFP|QSFP|SFP)", typ, re.I)
            seen["pluggable" if pluggable else "backplane"] += 1
            if port not in pres:
                checks.fail(f"{switch.name}/{ns}: {port} missing from transceiver presence")
            elif pluggable:
                checks.check(pres[port], f"{switch.name}/{ns}: {port} type {typ} but no module present")
            else:
                checks.check(not pres[port], f"{switch.name}/{ns}: {port} backplane port reports a module present")
    record_property("ports", dict(seen))
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-058T")
def test_port_speed_profile(switch, inventory, checks, record_property):
    """Tracking TC-SWH-058: every up fabric port is configured and running at switch.expected_speed (400000);
    with commands.switch.dsc_dump set, the per-ASIC lane dump must not report a lane at another rate."""
    want = str(inventory.th("switch.expected_speed", 400000))
    for ns in switch.namespaces():
        up = _up_ports(switch, ns)
        for port, (cfg, oper) in switch.port_speeds(ns).items():
            if port in up:
                checks.check(cfg == want and oper == want, f"{switch.name}/{ns}: {port} speed cfg {cfg} oper {oper}, want {want}")
    tmpl = inventory.cmd("switch.dsc_dump")
    if tmpl:
        lane_rx = re.compile(inventory.th("switch.dsc_lane_regex", r"(\d+(?:\.\d+)?)G"))
        rate = inventory.th("switch.dsc_lane_rate")
        for ns in switch.namespaces():
            r = switch.run(tmpl.format(asic=(ns or "asic0")[4:]), sudo=True, timeout=300)
            if checks.check(r.ok, f"{switch.name}/{ns}: dsc dump rc={r.rc}") and rate:
                bad = sorted({m for m in lane_rx.findall(r.stdout) if str(m) != str(rate)})
                checks.check(not bad, f"{switch.name}/{ns}: lanes at {bad}, want {rate}G")
    else:
        record_property("dsc_dump", "commands.switch.dsc_dump not set: speeds checked from CONFIG_DB/STATE_DB only")
    checks.assert_all()


# -------------------------------------------------- services, load 059T..063

@pytest.mark.l1
@pytest.mark.tc("TC-SWH-059T")
def test_critical_services_active(switch, inventory, record_property):
    """Tracking TC-SWH-059: every critical systemd service (switch.critical_services, '{n}' = ASIC) is active."""
    units = _expand(inventory.th("switch.critical_services") or DEFAULT_SERVICES, switch)
    out = switch.run("for u in " + " ".join(shlex.quote(u) for u in units) +
                     "; do echo \"$u $(systemctl is-active $u)\"; done", timeout=120).stdout
    state = dict(ln.split(None, 1) for ln in out.splitlines() if len(ln.split()) == 2)
    record_property("services", state)
    bad = {u: state.get(u, "unknown") for u in units if state.get(u) != "active"}
    assert not bad, f"{switch.name}: services not active: {bad}"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-060T")
def test_critical_processes_running(switch, checks):
    """Tracking TC-SWH-060: the critical processes of swssN / syncdN (orchagent, syncd, ...) are running."""
    for ns in switch.namespaces():
        for base in ("swss", "syncd"):
            ctr = switch.per_asic(base, ns)
            checks.check(switch.critical_processes(ctr), f"{switch.name}: {ctr} lists no critical processes")
            down = switch.critical_down(ctr)
            checks.check(not down, f"{switch.name}: {ctr} critical processes down: {down}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-061T")
def test_cpu_memory_disk_within_limits(switch, inventory, checks, record_property):
    """Tracking TC-SWH-061: CPU busy (vmstat, 1 s sample) <= cpu_max_pct, memory used <= mem_max_pct, every disk
    mount <= disk_max_pct."""
    out = switch.sh("vmstat 1 2", timeout=60).strip().splitlines()
    hdr = next((ln.split() for ln in out if ln.split()[:2] == ["r", "b"]), [])
    idle = _num(dict(zip(hdr, out[-1].split())).get("id")) if hdr else None
    mem = switch.memory_load()
    used = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"]) if mem.get("MemTotal") else None
    disk = switch.disk_usage()
    record_property("usage", {"cpu_busy_pct": None if idle is None else 100 - idle, "mem_used_pct": used, "disk": disk})
    cpu_max, mem_max = float(inventory.th("switch.cpu_max_pct", 90)), float(inventory.th("switch.mem_max_pct", 90))
    disk_max = float(inventory.th("switch.disk_max_pct", 85))
    if checks.check(idle is not None, f"{switch.name}: vmstat idle unreadable"):
        checks.check(100 - idle <= cpu_max, f"{switch.name}: CPU {100 - idle:.0f}% busy > {cpu_max:.0f}%")
    if checks.check(used is not None, f"{switch.name}: /proc/meminfo unreadable"):
        checks.check(used <= mem_max, f"{switch.name}: memory {used:.0f}% used > {mem_max:.0f}%")
    for path, pct in disk.items():
        checks.check(pct <= disk_max, f"{switch.name}: {path} {pct}% full > {disk_max:.0f}%")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-062T")
def test_load_average(switch, inventory, record_property):
    """Tracking TC-SWH-062: 1-minute load average per CPU <= switch.load_per_cpu_max (2.0)."""
    mem = switch.memory_load()
    per_cpu = mem["load1"] / max(mem["nproc"], 1)
    record_property("load", {"load1": mem["load1"], "nproc": mem["nproc"]})
    limit = float(inventory.th("switch.load_per_cpu_max", 2.0))
    assert per_cpu <= limit, f"{switch.name}: load {mem['load1']} on {mem['nproc']:.0f} CPUs ({per_cpu:.2f}/CPU > {limit})"


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-063")
def test_uptime_stable(switch, inventory, record_property):
    """Up longer than switch.min_uptime_s (600); a more recent boot must have an expected reboot cause."""
    up = _uptime_s(switch)
    cause = switch.reboot_cause()
    record_property("uptime", {"seconds": up, "reboot_cause": cause})
    if up >= float(inventory.th("switch.min_uptime_s", 600)):
        return
    pats = inventory.th("switch.expected_reboot_causes") or DEFAULT_REBOOT_CAUSES
    assert any(re.search(p, cause) for p in pats), f"{switch.name}: up {up:.0f}s after an unexpected reboot: {cause!r}"


# ------------------------------------------------------- system 064..069

@pytest.mark.l2
@pytest.mark.tc("TC-SWH-064")
def test_dns_configured_resolvable(switch, inventory, checks, record_property):
    """/etc/resolv.conf names a nameserver; every thresholds.platform.dns_names entry resolves (getent hosts)."""
    conf = switch.run("cat /etc/resolv.conf", timeout=30).stdout
    servers = re.findall(r"^\s*nameserver\s+(\S+)", conf, re.M)
    record_property("nameservers", servers)
    checks.check(servers, f"{switch.name}: no nameserver in /etc/resolv.conf")
    names = inventory.th("platform.dns_names") or []
    for n in names:
        r = switch.run(f"getent hosts {shlex.quote(n)}", timeout=30)
        checks.check(r.ok and r.stdout.strip(), f"{switch.name}: {n} does not resolve")
    if not names:
        record_property("resolution", "thresholds.platform.dns_names not set: name resolution not exercised")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-SWH-065")
def test_sonic_version_expected(switch, rack, inventory, record_property):
    """SONiC version is switch.expected_sonic_version, or (unset) the version most switch trays of the rack run."""
    mine = switch.sonic_version()
    want = inventory.th("switch.expected_sonic_version")
    if not want:
        versions = Counter(v for _, v, e in fan_out(lambda s: s.sonic_version(), rack.switches.values()) if v and not e)
        want = versions.most_common(1)[0][0] if versions else mine
    record_property("version", {"running": mine, "expected": want})
    assert mine and mine == want, f"{switch.name}: SONiC {mine or 'unknown'}, expected {want}"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-066")
def test_images_listable(switch, record_property):
    """'sonic-installer list' shows at least one installed image, and the current one among them."""
    img = switch.images()
    record_property("images", img)
    assert img["available"], f"{switch.name}: sonic-installer lists no images"
    assert img["current"] in img["available"], f"{switch.name}: current image {img['current']!r} not listed"


def _norm_version(v: str) -> str:
    return re.sub(r"^(SONiC-OS-|SONiC\.)", "", v or "")


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-067")
def test_running_version_matches_current_image(switch):
    """'show version' and 'sonic-installer list' Current name the same build."""
    run, cur = switch.sonic_version(), switch.images()["current"]
    assert _norm_version(run) == _norm_version(cur), f"{switch.name}: running {run!r}, installer Current {cur!r}"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-068")
def test_next_boot_is_current(switch):
    """The image set for the next boot is the running one (Current == Next)."""
    img = switch.images()
    assert img["current"] and img["current"] == img["next"], (
        f"{switch.name}: next boot {img['next']!r} differs from current {img['current']!r}")


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-069")
def test_image_commands_available(switch):
    """sonic-installer offers install, list, remove, set-default, set-next-boot, cleanup and binary-version."""
    helptext = switch.run("sonic-installer --help", sudo=True, timeout=60).stdout
    missing = [c for c in INSTALLER_CMDS if not re.search(rf"^\s*{re.escape(c)}\b", helptext, re.M)]
    assert not missing, f"{switch.name}: sonic-installer lacks {missing}"


# ---------------------------------------------------- CONFIG_DB 070..077

@pytest.mark.l2
@pytest.mark.tc("TC-SWH-070")
def test_running_config_valid(switch, checks):
    """The running config of the host and of every ASIC is valid JSON; the host has DEVICE_METADATA, every ASIC
    a PORT table."""
    for ns in [None, *[n for n in switch.namespaces() if n]]:
        where = ns or "host"
        try:
            cfg = _running_config(switch, ns)
        except ValueError as e:
            checks.fail(f"{switch.name}/{where}: running config is not JSON: {e}")
            continue
        need = "PORT" if ns else "DEVICE_METADATA"
        checks.check(cfg.get(need), f"{switch.name}/{where}: running config has no {need}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-071")
def test_configdb_core_tables(switch, inventory, checks):
    """Every ASIC namespace's CONFIG_DB has the core tables (switch.core_tables: DEVICE_METADATA, PORT, VLAN)."""
    tables = inventory.th("switch.core_tables") or ["DEVICE_METADATA", "PORT", "VLAN"]
    for ns in switch.namespaces():
        cfg = _running_config(switch, ns)
        missing = [t for t in tables if not cfg.get(t)]
        checks.check(not missing, f"{switch.name}/{ns}: CONFIG_DB lacks {missing}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-072")
def test_configdb_interface_table(switch, checks):
    """Every ASIC namespace's CONFIG_DB INTERFACE table is populated."""
    for ns in switch.namespaces():
        checks.check(_running_config(switch, ns).get("INTERFACE"), f"{switch.name}/{ns}: CONFIG_DB INTERFACE empty")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-073")
def test_configdb_port_table(switch, checks):
    """Every fabric port in CONFIG_DB PORT has a speed and an admin_status, on every ASIC."""
    for ns in switch.namespaces():
        ports = {p: v for p, v in (_running_config(switch, ns).get("PORT") or {}).items() if switch.is_fabric_port(p)}
        if not checks.check(ports, f"{switch.name}/{ns}: CONFIG_DB PORT has no fabric ports"):
            continue
        for p, v in ports.items():
            missing = [k for k in ("speed", "admin_status") if not v.get(k)]
            checks.check(not missing, f"{switch.name}/{ns}: PORT|{p} lacks {missing}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-074")
def test_configdb_vlans_consistent(switch, record_property):
    """Every ASIC has the same CONFIG_DB VLAN set with the same vlanid values."""
    per_ns = {ns: {k: v.get("vlanid") for k, v in (_running_config(switch, ns).get("VLAN") or {}).items()}
              for ns in switch.namespaces()}
    record_property("vlans", per_ns)
    distinct = {json.dumps(v, sort_keys=True) for v in per_ns.values()}
    assert len(distinct) == 1, f"{switch.name}: VLAN tables differ between ASICs: {per_ns}"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-075")
def test_vlan_membership_symmetric(switch, record_property):
    """Every VLAN has the same number of member ports on every ASIC."""
    counts = {ns: {v: len(p) for v, p in switch.vlan_members(ns).items()} for ns in switch.namespaces()}
    record_property("members", counts)
    vlans = set().union(*[set(c) for c in counts.values()])
    bad = {v: {ns: c.get(v, 0) for ns, c in counts.items()} for v in sorted(vlans)
           if len({c.get(v, 0) for c in counts.values()}) > 1}
    assert vlans, f"{switch.name}: no VLAN members on any ASIC"
    assert not bad, f"{switch.name}: asymmetric VLAN membership {bad}"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-076")
def test_fdb_readable(switch, record_property):
    """'show mac' is readable on every ASIC and lists at least one entry on the switch."""
    per_ns = {ns: len(switch.fdb(ns)) for ns in switch.namespaces()}
    record_property("fdb_entries", per_ns)
    assert sum(per_ns.values()) > 0, f"{switch.name}: FDB empty on every ASIC"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-077")
def test_routing_table_populated(switch, record_property):
    """The host routing table has a default route and at least one further route."""
    routes = [ln for ln in switch.run("ip route show", timeout=60).stdout.splitlines() if ln.strip()]
    record_property("routes", routes[:30])
    assert any(r.startswith("default") for r in routes), f"{switch.name}: no default route"
    assert len(routes) >= 2, f"{switch.name}: only {len(routes)} route(s)"


# --------------------------------------------- management / security 078..088

@pytest.mark.l1
@pytest.mark.tc("TC-SWH-078")
def test_management_reachable(switch, inventory, checks, record_property):
    """Ping (5 packets) the default gateway and every switch.ping_targets address with 0% loss."""
    gw = re.search(r"default via (\S+)", switch.default_route())
    targets = list(inventory.th("switch.ping_targets") or []) + ([gw.group(1)] if gw else [])
    assert targets, f"{switch.name}: no default gateway and no switch.ping_targets"
    res = {}
    for t in targets:
        out = switch.run(f"ping -c 5 -W 2 {shlex.quote(t)}", timeout=60).stdout
        m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out)
        res[t] = m.group(1) if m else "?"
        checks.check(m and float(m.group(1)) == 0, f"{switch.name}: ping {t}: {res[t]}% loss")
    record_property("ping_loss_pct", res)
    checks.assert_all()


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    return None


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-079")
def test_fru_json_valid(switch, inventory, record_property):
    """The switch FRU JSON (switch.fru_json_path, else afm.fru_json_path, else /etc/sonic/fru.json) is valid JSON
    with num-mac-address == switch.fru_num_mac (512)."""
    path = inventory.th("switch.fru_json_path") or inventory.th("afm.fru_json_path") or "/etc/sonic/fru.json"
    r = switch.run(f"cat {shlex.quote(path)}", sudo=True, timeout=60)
    assert r.ok, f"{switch.name}: cannot read {path}: {r.tail()!r}"
    try:
        fru = json.loads(r.stdout)
    except ValueError as e:
        pytest.fail(f"{switch.name}: {path} is not valid JSON: {e}")
    n = _find_key(fru, "num-mac-address")
    record_property("num_mac_address", n)
    want = int(inventory.th("switch.fru_num_mac", 512))
    assert n is not None and int(n) == want, f"{switch.name}: {path} num-mac-address {n}, expected {want}"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-080")
def test_auth_log_clean(switch, inventory, record_property):
    """/var/log/auth.log exists and holds at most switch.auth_fail_max (0) authentication failures
    (switch.auth_fail_ignore regexes excepted)."""
    assert switch.run("test -s /var/log/auth.log", sudo=True, timeout=30).ok, f"{switch.name}: /var/log/auth.log missing or empty"
    out = switch.run(f"grep -hE {shlex.quote(AUTH_FAIL_RE)} /var/log/auth.log", sudo=True, timeout=120).stdout
    fails = [ln for ln in out.splitlines() if ln.strip() and not _ignored(ln, inventory.th("switch.auth_fail_ignore"))]
    record_property("auth_failures", fails[-20:])
    limit = int(inventory.th("switch.auth_fail_max", 0))
    assert len(fails) <= limit, f"{switch.name}: {len(fails)} authentication failure(s) (> {limit}): {fails[-3:]}"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-081")
def test_boot_logs_clean(switch, inventory, record_property):
    """No emerg/alert/crit kernel messages (dmesg) and no journal entries of priority crit or worse this boot."""
    ignore = inventory.th("switch.boot_log_ignore") or []
    lines = []
    for cmd in ("dmesg --level=emerg,alert,crit", "journalctl -b -p 2 --no-pager -q"):
        lines += [f"{cmd.split()[0]}: {ln}" for ln in switch.run(cmd, sudo=True, timeout=120).stdout.splitlines()
                  if ln.strip() and not _ignored(ln, ignore)]
    record_property("critical_messages", lines[:30])
    assert not lines, f"{switch.name}: {len(lines)} critical boot-log message(s): {lines[:3]}"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-082")
def test_log_rotation_configured(switch, inventory, checks):
    """logrotate has an rsyslog policy and is scheduled (timer or cron); /var/log is below switch.disk_max_pct."""
    conf = switch.run("ls -1 /etc/logrotate.d", timeout=30).stdout.split()
    checks.check("rsyslog" in conf, f"{switch.name}: no /etc/logrotate.d/rsyslog ({conf})")
    sched = switch.run("systemctl is-enabled logrotate.timer || test -f /etc/cron.d/logrotate", timeout=30)
    checks.check(sched.ok, f"{switch.name}: logrotate is not scheduled (no enabled timer, no cron entry)")
    pct = switch.disk_usage().get("/var/log")
    limit = float(inventory.th("switch.disk_max_pct", 85))
    checks.check(pct is not None and pct <= limit, f"{switch.name}: /var/log {pct}% full")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-083")
def test_host_firewall_active(switch, inventory, checks, record_property):
    """iptables has the INPUT, FORWARD and OUTPUT chains and at least switch.firewall_min_input_rules rules in
    INPUT (SONiC caclmgrd)."""
    out = switch.sh("iptables -S", sudo=True, timeout=60)
    chains = set(re.findall(r"^-P (\S+)", out, re.M))
    rules = [ln for ln in out.splitlines() if ln.startswith("-A INPUT")]
    record_property("iptables", {"chains": sorted(chains), "input_rules": len(rules)})
    for c in ("INPUT", "FORWARD", "OUTPUT"):
        checks.check(c in chains, f"{switch.name}: iptables has no {c} chain")
    want = int(inventory.th("switch.firewall_min_input_rules", 1))
    checks.check(len(rules) >= want, f"{switch.name}: {len(rules)} INPUT rule(s), expected >= {want}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-084")
def test_ssh_hardening(switch, inventory, checks, record_property):
    """sshd effective config: root login refused (switch.ssh.permit_root_login), no empty passwords, MaxAuthTries
    <= switch.ssh.max_auth_tries (6)."""
    cfg = {t[0].lower(): t[1] for t in (ln.split(None, 1) for ln in switch.sh("sshd -T", sudo=True, timeout=60).splitlines())
           if len(t) == 2}
    record_property("sshd", {k: cfg.get(k) for k in ("permitrootlogin", "permitemptypasswords", "maxauthtries",
                                                      "passwordauthentication")})
    allowed = inventory.th("switch.ssh.permit_root_login") or ["no", "prohibit-password", "without-password"]
    checks.check(cfg.get("permitrootlogin") in allowed, f"{switch.name}: PermitRootLogin {cfg.get('permitrootlogin')}")
    checks.check(cfg.get("permitemptypasswords") == "no", f"{switch.name}: PermitEmptyPasswords {cfg.get('permitemptypasswords')}")
    tries = _num(cfg.get("maxauthtries"))
    limit = int(inventory.th("switch.ssh.max_auth_tries", 6))
    checks.check(tries is not None and tries <= limit, f"{switch.name}: MaxAuthTries {cfg.get('maxauthtries')} > {limit}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-085")
def test_cli_lockdown(switch, inventory, checks):
    """A configuration command without root privileges is refused and changes nothing ('config vlan add
    <switch.lockdown_probe_vlan, 4093>' as the login user)."""
    vid = int(inventory.th("switch.lockdown_probe_vlan", 4093))
    ns = switch.namespaces()[0]
    before = set(switch.vlans(ns))
    r = switch.run(f"config vlan add {vid}", timeout=60)
    created = f"Vlan{vid}" in set(switch.vlans(ns)) - before
    if created:
        switch.run(f"config vlan del {vid}", sudo=True, timeout=60)
    checks.check(not r.ok and re.search(r"root privileges|permission denied", r.stdout + r.stderr, re.I),
                 f"{switch.name}: non-root 'config vlan add' not refused (rc={r.rc} {r.tail()!r})")
    checks.check(not created, f"{switch.name}: Vlan{vid} was created without root privileges (removed again)")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-086")
def test_user_accounts(switch, inventory, checks, record_property):
    """Only switch.allowed_uid0 (root) has UID 0; with switch.allowed_users, no other login account exists."""
    users = [ln.split(":") for ln in switch.sh("getent passwd", timeout=60).splitlines() if ln.count(":") >= 6]
    uid0 = sorted(u[0] for u in users if u[2] == "0")
    login = sorted(u[0] for u in users if not re.search(r"(nologin|false|sync|halt|shutdown)$", u[6]))
    record_property("accounts", {"uid0": uid0, "login": login})
    extra0 = [u for u in uid0 if u not in (inventory.th("switch.allowed_uid0") or ["root"])]
    checks.check(not extra0, f"{switch.name}: extra UID-0 accounts {extra0}")
    allowed = inventory.th("switch.allowed_users")
    if allowed:
        extra = [u for u in login if u not in allowed]
        checks.check(not extra, f"{switch.name}: unexpected login accounts {extra}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-087")
def test_hostname_valid(switch, checks):
    """The hostname is not a default (sonic, localhost), is RFC 1123 valid and matches CONFIG_DB
    DEVICE_METADATA."""
    name = switch.sh("hostname", timeout=30).strip()
    checks.check(name.lower() not in BAD_HOSTNAMES, f"{switch.name}: default hostname {name!r}")
    checks.check(RFC1123.match(name), f"{switch.name}: hostname {name!r} not RFC 1123 valid")
    meta = ((_running_config(switch, None).get("DEVICE_METADATA") or {}).get("localhost") or {}).get("hostname")
    checks.check(meta == name, f"{switch.name}: hostname {name!r}, CONFIG_DB DEVICE_METADATA {meta!r}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-088")
def test_ntp_synchronized(switch, checks, record_property):
    """CONFIG_DB has NTP servers and the system clock is NTP-synchronized (timedatectl)."""
    servers = [k.split("|", 1)[1] for k in switch.db_keys("CONFIG_DB", "NTP_SERVER|*")]
    synced = switch.run("timedatectl show -p NTPSynchronized --value", timeout=30).stdout.strip()
    record_property("ntp", {"servers": servers, "synchronized": synced})
    checks.check(servers, f"{switch.name}: no NTP_SERVER in CONFIG_DB")
    checks.check(synced == "yes", f"{switch.name}: clock not NTP-synchronized ({synced or 'unknown'})")
    checks.assert_all()


# ------------------------------------------------------------ LLDP 089..091

def _lldp_enabled(switch) -> bool:
    state = switch.db_hash("CONFIG_DB", "FEATURE|lldp").get("state", "enabled")
    return state.lower() in ("enabled", "always_enabled")


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-089")
def test_lldp_neighbors(switch, checks, record_property):
    """With the LLDP feature enabled, every ASIC discovers LLDP neighbours; skips when LLDP is disabled."""
    if not _lldp_enabled(switch):
        pytest.skip(f"{switch.name}: LLDP feature disabled (CONFIG_DB FEATURE|lldp)")
    per_ns = {ns: len(switch.lldp_ports(ns)) for ns in switch.namespaces()}
    record_property("neighbours", per_ns)
    for ns, n in per_ns.items():
        checks.check(n > 0, f"{switch.name}/{ns}: no LLDP neighbours")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-090")
def test_show_lldp_table_parses(switch, checks, record_property):
    """AFOS-43 guard: 'show lldp table' exits 0 without a traceback, every row has the five columns and the
    'Total entries displayed' count equals the rows."""
    if not _lldp_enabled(switch):
        pytest.skip(f"{switch.name}: LLDP feature disabled (CONFIG_DB FEATURE|lldp)")
    r = switch.run("show lldp table", timeout=120)
    text = r.stdout + r.stderr
    checks.check(r.ok and "Traceback" not in text, f"{switch.name}: show lldp table failed rc={r.rc} {r.tail()!r}")
    rows = [x for x in parse_table(r.stdout.split("Total entries")[0]) if x.get("LocalPort")]
    m = re.search(r"Total entries displayed:\s*(\d+)", r.stdout)
    record_property("entries", len(rows))
    checks.check(m and int(m.group(1)) == len(rows), f"{switch.name}: {len(rows)} rows, total line {m.group(0) if m else 'missing'}")
    cols = ("LocalPort", "RemoteDevice", "RemotePortID", "Capability", "RemotePortDescr")
    for x in rows:
        missing = [c for c in cols if c not in x]
        checks.check(not missing, f"{switch.name}: lldp row {x.get('LocalPort')} lacks {missing}")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-091")
def test_rtm_ports_up_per_tray(switch, inventory, checks, record_property):
    """Every port cabled (by description) to a compute tray is oper up at switch.expected_speed in trunk mode; a
    tray never has only some of its links down (quarantined trays excepted)."""
    want = str(int(int(inventory.th("switch.expected_speed", 400000)) / 1000)) + "G"
    qslots = inventory.quarantined_slots()
    per_tray: dict[int, Counter] = {}
    for ns in switch.namespaces():
        desc = switch.port_descriptions(ns)
        for r in switch.interfaces_status(ns):
            cab = parse_port_description(desc.get(r["Interface"], ""))
            if not cab or cab[0] in qslots:
                continue
            up = r.get("Oper", "").lower() == "up"
            per_tray.setdefault(cab[0], Counter())["up" if up else "down"] += 1
            if up:
                checks.check(r.get("Speed") == want and r.get("Vlan", "").lower() == "trunk",
                             f"{switch.name}/{ns}: {r['Interface']} (slot {cab[0]}) {r.get('Speed')} {r.get('Vlan')}")
    record_property("per_tray", {s: dict(c) for s, c in per_tray.items()})
    assert per_tray, f"{switch.name}: no port described as cabled to a compute tray"
    for slot, c in per_tray.items():
        checks.check(not (c["up"] and c["down"]), f"{switch.name}: tray slot {slot} has {c['down']} of "
                                                  f"{c['up'] + c['down']} links down")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-092")
def test_fec_uncorrectable_not_climbing(switch, inventory, checks, record_property):
    """FEC_UNCORR does not grow on any up fabric port over switch.fec_window_s (10 s)."""
    window = float(inventory.th("switch.fec_window_s", 10))
    before = {ns: switch.fec_stats(ns) for ns in switch.namespaces()}
    time.sleep(window)
    grew = {}
    for ns in switch.namespaces():
        up = _up_ports(switch, ns)
        for port, row in switch.fec_stats(ns).items():
            a, b = _num((before[ns].get(port) or {}).get("FEC_UNCORR")), _num(row.get("FEC_UNCORR"))
            if port in up and a is not None and b is not None and b > a:
                grew[f"{ns}/{port}"] = b - a
    record_property("uncorr_growth", grew)
    checks.check(not grew, f"{switch.name}: FEC_UNCORR climbing on {sorted(grew)[:10]}")
    checks.assert_all()


# ----------------------------------------------- configuration 093..097

@pytest.mark.l2
@pytest.mark.tc("TC-SWH-093")
def test_config_backup_identical(switch, checks):
    """Copy every saved config_db*.json to /tmp; the copies are byte-identical (sha256); the copies are removed."""
    stamp = int(time.time())
    files = _saved_files(switch)
    copies = {f: f"/tmp/helios-backup-{stamp}-{f.rsplit('/', 1)[1]}" for f in files}
    try:
        for src, dst in copies.items():
            r = switch.run(f"cp -p {src} {dst}", sudo=True, timeout=60)
            checks.check(r.ok, f"{switch.name}: cp {src} failed: {r.tail()!r}")
        sums = _sha(switch, [*files, *copies.values()])
        for src, dst in copies.items():
            checks.check(sums.get(src) and sums.get(src) == sums.get(dst), f"{switch.name}: backup of {src} differs")
    finally:
        switch.run("rm -f " + " ".join(copies.values()), sudo=True, timeout=60)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-SWH-094")
def test_config_load_no_restart(switch, inventory, checks):
    """'config load -y' of the current running config (host) succeeds without restarting any container or
    dropping a port."""
    up, starts = _oper_up(switch), {k: v[1] for k, v in switch.container_restarts().items()}
    dump = f"/tmp/helios-running-{int(time.time())}.json"
    try:
        r = switch.run(f"sonic-cfggen -d --print-data > {dump} && config load -y {dump}", sudo=True, timeout=300)
        checks.check(r.ok, f"{switch.name}: config load rc={r.rc} {r.tail()!r}")
    finally:
        switch.run(f"rm -f {dump}", sudo=True, timeout=60)
    after = {k: v[1] for k, v in switch.container_restarts().items()}
    restarted = sorted(k for k in starts if after.get(k) != starts[k])
    checks.check(not restarted, f"{switch.name}: containers restarted by config load: {restarted}")
    _wait_recovered(switch, inventory, checks, up, "config load")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-SWH-095")
def test_config_reload_recovers(switch, inventory, checks):
    """'config reload -y' (forwarding-disruptive): swss/syncd and every previously up port recover within
    switch.reload_timeout_s (900) and the running config is unchanged. Opt-in (switch.allow_config_reload): on an
    AFM-managed rack the reload drops AFM's unsaved runtime config (trunk/VLAN ports, node agents) - g02-g17,
    27 Oct 2026: 48 GPU links lost their VLANs and every sb-node-agent stopped until afm-agent was restarted."""
    if not inventory.th("switch.allow_config_reload", False):
        pytest.skip(f"{switch.name}: config reload drops AFM's runtime switch config; set "
                    "thresholds.switch.allow_config_reload (and restart afm-agent afterwards)")
    up = _oper_up(switch)
    cfg_before = {ns: _running_config(switch, ns).get("PORT") for ns in switch.namespaces()}
    r = switch.run("config reload -y", sudo=True, timeout=float(inventory.th("switch.reload_timeout_s", 900)))
    checks.check(r.ok, f"{switch.name}: config reload rc={r.rc} {r.tail()!r}")
    _wait_recovered(switch, inventory, checks, up, "config reload", key="reload_timeout_s", default=900)
    for ns, ports in cfg_before.items():
        checks.check(_running_config(switch, ns).get("PORT") == ports, f"{switch.name}/{ns}: PORT table changed by reload")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-SWH-096")
def test_config_restore_sha_guarded(switch, checks):
    """Back up every saved config_db*.json with its sha256, restore the backup over the original and verify the
    restored files hash to the original values."""
    files = _saved_files(switch)
    orig = _sha(switch, files)
    assert all(orig.get(f) for f in files), f"{switch.name}: cannot hash {files}"
    bak = {f: f"{f}.helios-restore" for f in files}
    try:
        for f, b in bak.items():
            checks.check(switch.run(f"cp -p {f} {b}", sudo=True, timeout=60).ok, f"{switch.name}: backup {f} failed")
        sums = _sha(switch, list(bak.values()))
        for f, b in bak.items():
            checks.check(sums.get(b) == orig[f], f"{switch.name}: backup of {f} has a different sha256")
            if sums.get(b) == orig[f]:
                checks.check(switch.run(f"cp -p {b} {f}", sudo=True, timeout=60).ok, f"{switch.name}: restore {f} failed")
    finally:
        switch.run("rm -f " + " ".join(bak.values()), sudo=True, timeout=60)
    after = _sha(switch, files)
    for f in files:
        checks.check(after.get(f) == orig[f], f"{switch.name}: {f} sha256 changed by restore")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-SWH-097")
def test_config_save_idempotent(switch, inventory, checks):
    """'config save -y' succeeds twice and the saved files are identical after both saves. Opt-in
    (switch.allow_config_save): it writes AFM's runtime config into the switch's startup config."""
    if not inventory.th("switch.allow_config_save", False):
        pytest.skip(f"{switch.name}: config save writes AFM's runtime config into the startup config; set "
                    "thresholds.switch.allow_config_save")
    files = _saved_files(switch)
    sums = []
    for i in range(2):
        r = switch.run("config save -y", sudo=True, timeout=300)
        checks.check(r.ok, f"{switch.name}: config save #{i + 1} rc={r.rc} {r.tail()!r}")
        sums.append(_sha(switch, files))
    checks.check(sums[0] == sums[1] and all(sums[0].get(f) for f in files),
                 f"{switch.name}: saved config differs between two saves")
    checks.assert_all()


# ------------------------------------------------ disk, fans, images 098..102

@pytest.mark.l2
@pytest.mark.tc("TC-SWH-098")
def test_disk_write_throughput(switch, inventory, record_property):
    """64 MiB written with fdatasync to switch.disk_write_path (/host) reaches switch.disk_write_mb_s_min (20)."""
    path = f"{inventory.th('switch.disk_write_path', '/host').rstrip('/')}/helios-dd-{int(time.time())}.bin"
    try:
        out = switch.run(f"dd if=/dev/zero of={path} bs=1M count=64 conv=fdatasync 2>&1", sudo=True, timeout=300).stdout
    finally:
        switch.run(f"rm -f {path}", sudo=True, timeout=60)
    m = re.search(r"([\d.]+)\s*([MG])B/s", out)
    assert m, f"{switch.name}: dd throughput unreadable: {out[-200:]!r}"
    mbs = float(m.group(1)) * (1000 if m.group(2) == "G" else 1)
    record_property("write_mb_s", mbs)
    want = float(inventory.th("switch.disk_write_mb_s_min", 20))
    assert mbs >= want, f"{switch.name}: disk write {mbs:.1f} MB/s < {want} MB/s"


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-099")
def test_fan_auto_control(switch, inventory, checks, record_property):
    """thermalctld runs in pmon and every fan with a target speed runs within switch.fan_target_tolerance_pct
    (20) of it."""
    st = switch.run("docker exec pmon supervisorctl status thermalctld", sudo=True, timeout=60).stdout
    checks.check("RUNNING" in st, f"{switch.name}: thermalctld not running ({st.strip()[:80]!r})")
    tol = float(inventory.th("switch.fan_target_tolerance_pct", 20))
    seen = {}
    for name, d in switch.fan_info().items():
        cur, tgt = _num(d.get("speed")), _num(d.get("speed_target"))
        if cur is None or tgt is None:
            continue
        seen[name] = (cur, tgt)
        checks.check(abs(cur - tgt) <= tol, f"{switch.name}: {name} at {cur}% vs target {tgt}%")
    record_property("fans", seen)
    checks.check(seen, f"{switch.name}: no fan reports a target speed (FAN_INFO speed_target)")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-SWH-100")
def test_image_cleanup(switch, inventory, checks):
    """'sonic-installer cleanup -y' keeps the current and next images. With other images installed it runs only
    when switch.allow_image_cleanup is true (it deletes them)."""
    img = switch.images()
    keep = {img["current"], img["next"]}
    others = [i for i in img["available"] if i not in keep]
    if others and not inventory.th("switch.allow_image_cleanup", False):
        pytest.skip(f"{switch.name}: cleanup would delete {others}; set thresholds.switch.allow_image_cleanup")
    r = switch.run("sonic-installer cleanup -y", sudo=True, timeout=600)
    checks.check(r.ok, f"{switch.name}: sonic-installer cleanup rc={r.rc} {r.tail()!r}")
    after = switch.images()
    checks.check(keep <= set(after["available"]), f"{switch.name}: cleanup removed the current/next image")
    checks.check(not [i for i in after["available"] if i not in keep], f"{switch.name}: images left after cleanup")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-SWH-101")
def test_image_remove(switch, inventory, checks):
    """'sonic-installer remove -y <switch.removable_image>' removes that non-current, non-next image."""
    name = inventory.th("switch.removable_image")
    img = switch.images()
    if not name or name not in img["available"]:
        pytest.skip(f"{switch.name}: set thresholds.switch.removable_image to an installed spare image")
    assert name not in (img["current"], img["next"]), f"{switch.name}: refusing to remove the current/next image {name}"
    r = switch.run(f"sonic-installer remove -y {shlex.quote(name)}", sudo=True, timeout=600)
    checks.check(r.ok, f"{switch.name}: sonic-installer remove rc={r.rc} {r.tail()!r}")
    checks.check(name not in switch.images()["available"], f"{switch.name}: {name} still installed")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-102")
def test_set_default_boot_image(switch, checks):
    """'sonic-installer set-default <current>' succeeds and Next shows the current image; the original next-boot
    image is restored."""
    img = switch.images()
    cur, orig = img["current"], img["next"]
    assert cur, f"{switch.name}: no current image"
    try:
        r = switch.run(f"sonic-installer set-default {shlex.quote(cur)}", sudo=True, timeout=120)
        checks.check(r.ok, f"{switch.name}: set-default rc={r.rc} {r.tail()!r}")
        checks.check(switch.images()["next"] == cur, f"{switch.name}: Next not {cur} after set-default")
    finally:
        if orig and orig != cur:
            back = switch.run(f"sonic-installer set-default {shlex.quote(orig)}", sudo=True, timeout=120)
            checks.check(back.ok, f"{switch.name}: could not restore the default image {orig}")
    checks.assert_all()


# ------------------------------------------------------- restarts 104..106

@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-SWH-104")
def test_syncd_restart_recovers(switch, inventory, checks):
    """Restart syncd@0 (forwarding-disruptive on that ASIC); swss/syncd and every previously up port recover."""
    ns = switch.namespaces()[0]
    up = _oper_up(switch)
    r = switch.restart_service("syncd", ns)
    checks.check(r.ok, f"{switch.name}: restart {switch.per_asic('syncd', ns, '@')} rc={r.rc} {r.tail()!r}")
    _wait_recovered(switch, inventory, checks, up, f"{switch.per_asic('syncd', ns, '@')} restart")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-SWH-105")
def test_service_restart_capability(switch, inventory, checks):
    """Restart switch.restart_check_service (lldp) on the first ASIC; it is active again with its container's
    critical processes running."""
    base = inventory.th("switch.restart_check_service", "lldp")
    ns = switch.namespaces()[0]
    unit, ctr = switch.per_asic(base, ns, "@"), switch.per_asic(base, ns)
    r = switch.restart_service(base, ns)
    checks.check(r.ok, f"{switch.name}: systemctl restart {unit} rc={r.rc} {r.tail()!r}")
    timeout, interval = _poll(inventory)

    def back():
        return (switch.run(f"systemctl is-active {unit}", timeout=30).stdout.strip() == "active"
                and ctr in switch.containers() and not switch.critical_down(ctr))
    ok, _ = wait_until(back, timeout, interval, what=f"{switch.name} {unit}")
    checks.check(ok, f"{switch.name}: {unit} not back within {timeout:.0f}s")
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.disruptive
@pytest.mark.tc("TC-SWH-106")
def test_swss_restart_interfaces_recover(switch, inventory, checks):
    """Restart swss@N on switch.swss_restart_namespaces (first ASIC); swss/syncd and every previously up port
    recover."""
    up = _oper_up(switch)
    for ns in inventory.th("switch.swss_restart_namespaces") or switch.namespaces()[:1]:
        r = switch.restart_service("swss", ns)
        checks.check(r.ok, f"{switch.name}: restart {switch.per_asic('swss', ns, '@')} rc={r.rc} {r.tail()!r}")
        _wait_recovered(switch, inventory, checks, up, f"{switch.per_asic('swss', ns, '@')} restart")
    checks.assert_all()


# ------------------------------------------------------------ techsupport 107

TECHSUPPORT_REQUIRED = ["dump/", "log/syslog", "etc/sonic/config_db"]


@pytest.mark.l2
@pytest.mark.tc("TC-SWH-107")
def test_techsupport_content(switch, inventory, checks, record_property):
    """'show techsupport --since <switch.techsupport_since>' produces an archive that contains the dump, syslog and
    config_db entries (switch.techsupport_required); the archive is removed afterwards."""
    since = inventory.th("switch.techsupport_since", "1 hour ago")
    r = switch.run(f"show techsupport --since {shlex.quote(since)}", sudo=True,
                   timeout=float(inventory.th("switch.techsupport_timeout_s", 1800)))
    m = re.findall(r"(/var/dump/\S+\.tar\.gz)", r.stdout)
    assert r.ok and m, f"{switch.name}: techsupport failed rc={r.rc} {r.tail()!r}"
    archive = m[-1]
    try:
        names = switch.run(f"tar -tzf {shlex.quote(archive)}", sudo=True, timeout=600).stdout.splitlines()
        record_property("archive", {"path": archive, "entries": len(names)})
        for want in inventory.th("switch.techsupport_required") or TECHSUPPORT_REQUIRED:
            checks.check(any(want in n for n in names), f"{switch.name}: techsupport archive has no {want}")
    finally:
        switch.run(f"rm -f {shlex.quote(archive)}", sudo=True, timeout=60)
    checks.assert_all()
