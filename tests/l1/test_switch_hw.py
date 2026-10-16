"""TC-SWH-001..036 — SONiC switch-tray platform and interface health (L1).

Every test is parametrized per switch tray. Thresholds live under
``thresholds.switch`` in the inventory; a ``null`` threshold disables that
particular comparison (the test still checks presence/status).
"""
from __future__ import annotations

import math
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from helios.parsers import parse_speed_gbps, to_float

pytestmark = pytest.mark.l1

BAD_VERSIONS = {"", "N/A", "NA", "UNKNOWN", "NONE"}


# ----------------------------------------------------------------- helpers

def _is_psu_fan(row: dict) -> bool:
    return "psu" in f"{row.get('Drawer', '')} {row.get('FAN', '')}".lower()


def _fan_name(row: dict) -> str:
    return row.get("FAN") or row.get("Drawer") or "?"


def _chassis_fans(switch) -> list[dict]:
    return [r for r in switch.fans() if not _is_psu_fan(r)]


def _ok(status: str) -> bool:
    return status.strip().upper() == "OK"


def _psu_rows(switch) -> list[dict]:
    rows = switch.psus()
    assert rows, f"{switch.name}: 'show platform psustatus' returned no PSUs"
    return rows


def _ok_psus(switch) -> dict[str, dict[str, str]]:
    """STATE_DB PSU_INFO for PSUs whose CLI status is OK."""
    ok_names = {r["PSU"] for r in _psu_rows(switch) if _ok(r.get("Status", ""))}
    info = switch.psu_info()
    return {n: v for n, v in info.items() if n in ok_names}


def _firmware_check(rows: list[dict], comps: list[str], switch, checks) -> None:
    assert rows, f"{switch.name}: firmware status returned no components"
    for comp in comps:
        # match the component name or its description: e.g. QFX5252 lists CPLDs as "PMC"/"ICB" ("PMC CPLD firmware")
        hits = [r for r in rows
                if comp.lower() in r.get("Component", "").lower() or comp.lower() in r.get("Description", "").lower()]
        if checks.check(hits, f"{switch.name}: no {comp} component listed"):
            for r in hits:
                v = r.get("Version", "").strip()
                checks.check(v.upper() not in BAD_VERSIONS, f"{switch.name}: {r['Component']} version missing ({v!r})")


def _sample(n: int, interval: float, fn):
    out = []
    for i in range(n):
        out.append(fn())
        if i < n - 1:
            time.sleep(interval)
    return out


def _health_snapshot(switch) -> dict:
    return {
        "containers": set(switch.containers()),
        "oper_up": {(ns, r["Interface"]) for ns, rows in switch.all_interfaces_status().items()
                    for r in rows if r.get("Oper", "").lower() == "up"},
        "lldp": {(ns, p) for ns in switch.namespaces() for p in switch.lldp_ports(ns)},
    }


# --------------------------------------------------------------- firmware

@pytest.mark.tc("TC-SWH-001")
def test_platform_firmware_components(switch, inventory, checks):
    comps = inventory.th("switch.firmware_components") or ["BIOS", "CPLD", "FPGA"]
    _firmware_check(switch.firmware_status(), comps, switch, checks)
    checks.assert_all()


@pytest.mark.tc("TC-SWH-015")
def test_fwutil_firmware_components(switch, inventory, checks):
    comps = inventory.th("switch.fwutil_components") or ["BIOS", "CPLD"]
    _firmware_check(switch.fwutil_status(), comps, switch, checks)
    checks.assert_all()


# ------------------------------------------------------------------- fans

@pytest.mark.tc("TC-SWH-002")
def test_fan_speeds_in_range(switch, inventory, checks):
    lo = inventory.th("switch.fan_speed_pct.min", 10)
    hi = inventory.th("switch.fan_speed_pct.max", 100)
    rows = switch.fans()
    assert rows, f"{switch.name}: no fans reported"
    for r in rows:
        spd = to_float(r.get("Speed"))
        if not checks.check(spd is not None, f"{switch.name}: {_fan_name(r)} speed unreadable ({r.get('Speed')!r})"):
            continue
        checks.check(lo <= spd <= hi, f"{switch.name}: {_fan_name(r)} speed {spd}% outside [{lo},{hi}]")
        checks.check(_ok(r.get("Status", "")), f"{switch.name}: {_fan_name(r)} status {r.get('Status')!r}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-003")
def test_fan_speeds_consistent(switch, inventory, checks):
    spread = inventory.th("switch.fan_speed_max_spread_pct", 20)
    speeds = {_fan_name(r): to_float(r.get("Speed")) for r in _chassis_fans(switch)}
    speeds = {k: v for k, v in speeds.items() if v is not None}
    assert len(speeds) >= 2, f"{switch.name}: need >=2 chassis fans with readable speed, got {speeds}"
    lo, hi = min(speeds.values()), max(speeds.values())
    assert hi - lo <= spread, (
        f"{switch.name}: chassis fan speed spread {hi - lo:.0f}% > {spread}% "
        f"(min {min(speeds, key=speeds.get)}={lo}%, max {max(speeds, key=speeds.get)}={hi}%)")


@pytest.mark.tc("TC-SWH-004")
def test_fan_speed_tracks_temperature(switch, inventory, checks):
    """Passive check: thermalctld is running, and whenever any sensor is at/over
    its high threshold the chassis fans are driven at >= ``hot_min_fan_pct``.
    Active heating is TC-SWH-009 (manual)."""
    st = switch.run("docker exec pmon supervisorctl status thermalctld", sudo=True)
    checks.check(st.ok and "RUNNING" in st.stdout, f"{switch.name}: thermalctld not RUNNING: {st.stdout.strip() or st.tail()}")
    hot_min = inventory.th("switch.hot_min_fan_pct", 60)
    temps = switch.temperatures()
    checks.check(temps, f"{switch.name}: no temperature sensors reported")
    hot = [t["Sensor"] for t in temps
           if (v := to_float(t.get("Temperature"))) is not None
           and (h := to_float(t.get("High TH"))) is not None and v >= h]
    if hot:
        slow = [f"{_fan_name(r)}={r.get('Speed')}" for r in _chassis_fans(switch)
                if (to_float(r.get("Speed")) or 0) < hot_min]
        checks.check(not slow, f"{switch.name}: sensors {hot} over high TH but fans below {hot_min}%: {slow}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-005")
def test_fan_power_within_limits(switch, inventory, checks):
    """SONiC has no generic fan-power reading; needs a platform command that
    prints one ``<fan> <watts>`` pair per line (``commands.switch.fan_power``)."""
    cmd = inventory.cmd("switch.fan_power")
    if not cmd:
        pytest.skip("commands.switch.fan_power not configured (platform-specific)")
    limit = inventory.th("switch.fan_power_max_w")
    out = switch.sh(cmd, sudo=True)
    readings = [(m.group(1), float(m.group(2))) for m in re.finditer(r"^\s*(\S.*?)\s+([\d.]+)\s*W?\s*$", out, re.M)]
    assert readings, f"{switch.name}: no '<fan> <watts>' lines from {cmd!r}:\n{out[-500:]}"
    for name, w in readings:
        checks.check(w > 0, f"{switch.name}: {name} reports {w} W")
        if limit is not None:
            checks.check(w <= limit, f"{switch.name}: {name} {w} W > {limit} W")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-006")
def test_fan_data_consistency(switch, inventory, checks):
    """CLI and STATE_DB agree on fan set, presence and speed; two reads agree."""
    tol = inventory.th("switch.fan_read_tolerance_pct", 10)
    cli = {_fan_name(r): r for r in switch.fans()}
    db = switch.fan_info()
    checks.check(set(cli) == set(db), f"{switch.name}: fan set differs CLI-only={sorted(set(cli) - set(db))} "
                                      f"DB-only={sorted(set(db) - set(cli))}")
    for name in set(cli) & set(db):
        c, d = to_float(cli[name].get("Speed")), to_float(db[name].get("speed"))
        if c is not None and d is not None:
            checks.check(abs(c - d) <= tol, f"{switch.name}: {name} CLI {c}% vs STATE_DB {d}%")
        checks.check((cli[name].get("Presence", "").lower() == "present") == (db[name].get("presence", "").lower() == "true"),
                     f"{switch.name}: {name} presence CLI={cli[name].get('Presence')} DB={db[name].get('presence')}")
    again = {_fan_name(r): to_float(r.get("Speed")) for r in switch.fans()}
    for name, r in cli.items():
        a, b = to_float(r.get("Speed")), again.get(name)
        if a is not None and b is not None:
            checks.check(abs(a - b) <= tol, f"{switch.name}: {name} read-to-read jump {a}% -> {b}%")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-008")
def test_fan_monitoring_stability(switch, inventory, checks):
    n = int(inventory.th("switch.stability.samples", 6))
    interval = float(inventory.th("switch.stability.interval_s", 10))
    lo = inventory.th("switch.fan_speed_pct.min", 10)
    samples = _sample(n, interval, switch.fans)
    names0 = {_fan_name(r) for r in samples[0]}
    for i, rows in enumerate(samples):
        names = {_fan_name(r) for r in rows}
        checks.check(names == names0, f"{switch.name}: sample {i} fan set changed: {sorted(names ^ names0)}")
        for r in rows:
            checks.check(r.get("Presence", "").lower() == "present" and _ok(r.get("Status", "")),
                         f"{switch.name}: sample {i} {_fan_name(r)} presence={r.get('Presence')} status={r.get('Status')}")
            spd = to_float(r.get("Speed"))
            checks.check(spd is not None and spd >= lo, f"{switch.name}: sample {i} {_fan_name(r)} speed {r.get('Speed')}")
    checks.assert_all()


@pytest.mark.manual
@pytest.mark.tc("TC-SWH-009")
def test_fan_response_to_temperature_change(switch, inventory, checks):
    """Operator raises inlet temperature (or blocks airflow); test watches for fan ramp."""
    window = float(inventory.th("switch.fan_response_window_s", 120))
    base = {_fan_name(r): to_float(r.get("Speed")) or 0 for r in _chassis_fans(switch)}
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        now = {_fan_name(r): to_float(r.get("Speed")) or 0 for r in _chassis_fans(switch)}
        if any(now[k] > base.get(k, 0) + 5 for k in now):
            return
        time.sleep(5)
    pytest.fail(f"{switch.name}: no fan ramp within {window}s (baseline {base})")


@pytest.mark.tc("TC-SWH-017")
def test_all_fans_present(switch, inventory, checks):
    expected = inventory.th("switch.expected_fans")
    rows = _chassis_fans(switch)
    if expected is not None:
        checks.check(len(rows) == expected, f"{switch.name}: {len(rows)} chassis fans, expected {expected}")
    for r in rows:
        checks.check(r.get("Presence", "").lower() == "present", f"{switch.name}: {_fan_name(r)} {r.get('Presence')!r}")
    checks.assert_all()


# ------------------------------------------------------------------- PSUs

def _identity_exempt(inventory, name: str) -> bool:
    """Busbar-fed trays report PDB hot-swap controllers ("PDB HSC Main/Standby") as PSUs;
    they have no FRU model/serial. ``switch.psu_identity_exempt_re`` names such entries."""
    rx = inventory.th("switch.psu_identity_exempt_re")
    return bool(rx and re.search(rx, name))


@pytest.mark.tc("TC-SWH-007")
def test_psu_model_and_serial(switch, inventory, checks):
    rows = [r for r in _psu_rows(switch) if not _identity_exempt(inventory, r.get("PSU", ""))]
    if not rows:
        pytest.skip(f"{switch.name}: every PSU entry is exempt from FRU identity (switch.psu_identity_exempt_re)")
    for r in rows:
        for col in ("Model", "Serial"):
            checks.check(r.get(col, "").upper() not in BAD_VERSIONS, f"{switch.name}: {r['PSU']} {col} {r.get(col)!r}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-018")
def test_all_psus_present(switch, inventory, checks):
    rows = _psu_rows(switch)
    expected = inventory.th("switch.expected_psus")
    if expected is not None:
        checks.check(len(rows) == expected, f"{switch.name}: {len(rows)} PSUs, expected {expected}")
    for r in rows:
        checks.check("NOT PRESENT" not in r.get("Status", "").upper(), f"{switch.name}: {r['PSU']} not present")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-019")
def test_psu_status_ok(switch, checks):
    for r in _psu_rows(switch):
        checks.check(_ok(r.get("Status", "")), f"{switch.name}: {r['PSU']} status {r.get('Status')!r}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-020")
def test_psu_model_and_manufacturer(switch, inventory, checks):
    all_info = switch.psu_info()
    assert all_info, f"{switch.name}: STATE_DB has no PSU_INFO"
    info = {n: v for n, v in all_info.items() if not _identity_exempt(inventory, n)}
    if not info:
        pytest.skip(f"{switch.name}: every PSU entry is exempt from FRU identity (switch.psu_identity_exempt_re)")
    mfr_fields = ("manufacturer", "mfr_id", "vendor")
    if not any(f in v for v in info.values() for f in mfr_fields):
        pytest.skip(f"{switch.name}: platform exposes no PSU manufacturer field ({mfr_fields}) in PSU_INFO")
    for name, v in info.items():
        if v.get("presence", "true").lower() != "true":
            continue
        checks.check(v.get("model", "").upper() not in BAD_VERSIONS, f"{switch.name}: {name} model {v.get('model')!r}")
        mfr = next((v[f] for f in mfr_fields if f in v), "")
        checks.check(mfr.upper() not in BAD_VERSIONS, f"{switch.name}: {name} manufacturer {mfr!r}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-021")
def test_psu_output_voltage(switch, inventory, checks):
    for name, v in _ok_psus(switch).items():
        volt = to_float(v.get("voltage"))
        lo = to_float(v.get("voltage_min_threshold")) or inventory.th("switch.psu_voltage.min")
        hi = to_float(v.get("voltage_max_threshold")) or inventory.th("switch.psu_voltage.max")
        if not checks.check(volt is not None, f"{switch.name}: {name} voltage unreadable"):
            continue
        if lo is not None:
            checks.check(volt >= lo, f"{switch.name}: {name} {volt} V < min {lo} V")
        if hi is not None:
            checks.check(volt <= hi, f"{switch.name}: {name} {volt} V > max {hi} V")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-022")
def test_psu_output_current(switch, inventory, checks):
    limit = inventory.th("switch.psu_current_max_a")
    for name, v in _ok_psus(switch).items():
        cur = to_float(v.get("current"))
        if checks.check(cur is not None, f"{switch.name}: {name} current unreadable") and limit is not None:
            checks.check(cur <= limit, f"{switch.name}: {name} {cur} A > {limit} A")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-023")
def test_psu_power_draw(switch, inventory, checks):
    limit = inventory.th("switch.psu_power_max_w")
    for name, v in _ok_psus(switch).items():
        pw = to_float(v.get("power"))
        if not checks.check(pw is not None, f"{switch.name}: {name} power unreadable"):
            continue
        checks.check(v.get("power_overload", "False").lower() != "true", f"{switch.name}: {name} power_overload set")
        crit = to_float(v.get("power_critical_threshold")) or limit
        if crit is not None:
            checks.check(pw <= crit, f"{switch.name}: {name} {pw} W > {crit} W")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-024")
def test_psu_total_power_vs_capacity(switch, inventory, record_property):
    cap = inventory.th("switch.psu_capacity_w")
    psus = _ok_psus(switch)
    total = sum(to_float(v.get("power")) or 0 for v in psus.values())
    record_property("total_power_w", total)
    assert total > 0, f"{switch.name}: total PSU output is 0 W"
    if cap is not None:
        budget = cap * len(psus)
        assert total <= budget, f"{switch.name}: total {total:.0f} W > capacity {budget:.0f} W ({len(psus)} x {cap} W)"


@pytest.mark.tc("TC-SWH-010")
def test_psu_load_distribution(switch, inventory, checks):
    if not inventory.th("switch.psu_load_balance", True):
        pytest.skip(f"{switch.name}: supplies don't load-share on this platform (switch.psu_load_balance: false)")
    max_dev = inventory.th("switch.psu_load_balance_max_dev_pct", 30)
    loads = {n: to_float(v.get("power")) for n, v in _ok_psus(switch).items()}
    loads = {n: w for n, w in loads.items() if w is not None}
    assert len(loads) >= 2, f"{switch.name}: need >=2 healthy PSUs with power readings, got {loads}"
    mean = sum(loads.values()) / len(loads)
    for n, w in loads.items():
        dev = abs(w - mean) / mean * 100 if mean else 100
        checks.check(dev <= max_dev, f"{switch.name}: {n} {w:.0f} W deviates {dev:.0f}% from mean {mean:.0f} W")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-011")
def test_psu_temperature_vs_load(switch, inventory, checks, record_property):
    """Every loaded PSU reports a sane temperature; the (load, temp) pairs are
    recorded for trend review. A hard correlation assert needs a controlled load
    step, which belongs in a stress tier."""
    tmax = inventory.th("switch.psu_temp_max_c", 70)
    pairs = {}
    for n, v in _ok_psus(switch).items():
        t, w = to_float(v.get("temp")), to_float(v.get("power"))
        pairs[n] = (w, t)
        if checks.check(t is not None, f"{switch.name}: {n} temperature unreadable"):
            checks.check(t > 0, f"{switch.name}: {n} reports {t} C under {w} W load")
            checks.check(t <= tmax, f"{switch.name}: {n} {t} C > {tmax} C")
    record_property("psu_load_temp", pairs)
    checks.assert_all()


@pytest.mark.tc("TC-SWH-025")
def test_psu_temperature(switch, inventory, checks):
    for n, v in _ok_psus(switch).items():
        t = to_float(v.get("temp"))
        th = to_float(v.get("temp_threshold")) or inventory.th("switch.psu_temp_max_c", 70)
        if checks.check(t is not None, f"{switch.name}: {n} temperature unreadable"):
            checks.check(t < th, f"{switch.name}: {n} {t} C >= threshold {th} C")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-026")
def test_psu_internal_fans(switch, checks):
    rows = [r for r in switch.fans() if _is_psu_fan(r)]
    if not rows:
        pytest.skip(f"{switch.name}: platform reports no PSU fans in 'show platform fan'")
    for r in rows:
        checks.check(r.get("Presence", "").lower() == "present" and _ok(r.get("Status", "")),
                     f"{switch.name}: {_fan_name(r)} presence={r.get('Presence')} status={r.get('Status')}")
        checks.check((to_float(r.get("Speed")) or 0) > 0, f"{switch.name}: {_fan_name(r)} speed {r.get('Speed')}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-027")
def test_psu_redundancy(switch, inventory):
    spare = int(inventory.th("switch.psu_redundant_spares", 1))
    cap = inventory.th("switch.psu_capacity_w")
    psus = _ok_psus(switch)
    total = sum(to_float(v.get("power")) or 0 for v in psus.values())
    needed = max(1, math.ceil(total / cap)) if cap else 1
    assert len(psus) >= needed + spare, (
        f"{switch.name}: {len(psus)} healthy PSUs; load {total:.0f} W needs {needed} + {spare} spare")


@pytest.mark.tc("TC-SWH-028")
def test_psu_led_matches_state(switch, inventory, checks):
    good = {c.lower() for c in inventory.th("switch.psu_led_ok", ["green"])}
    for r in _psu_rows(switch):
        led = r.get("LED", "").lower()
        if _ok(r.get("Status", "")):
            checks.check(led in good, f"{switch.name}: {r['PSU']} OK but LED {led!r}")
        else:
            checks.check(led not in good, f"{switch.name}: {r['PSU']} {r.get('Status')!r} but LED {led!r}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-029")
def test_psu_input_type(switch, inventory, checks):
    fields = ("input_type", "type", "ac_dc")
    info = switch.psu_info()
    if not any(f in v for v in info.values() for f in fields):
        pytest.skip(f"{switch.name}: platform exposes no PSU input type ({fields}) in PSU_INFO")
    expected = inventory.th("switch.psu_input_type")
    for n, v in info.items():
        typ = next((v[f] for f in fields if f in v), "").upper()
        checks.check(typ in ("AC", "DC"), f"{switch.name}: {n} input type {typ!r}")
        if expected:
            checks.check(typ == expected.upper(), f"{switch.name}: {n} input {typ}, expected {expected}")
    checks.assert_all()


# --------------------------------------------------------------- platform

@pytest.mark.stress
@pytest.mark.tc("TC-SWH-012")
def test_behaviour_under_high_cpu_load(switch, inventory, checks, record_property):
    dur = int(inventory.th("switch.cpu_stress_s", 60))
    before = _health_snapshot(switch)
    burn = f"for i in $(seq $(nproc)); do timeout {dur} sh -c 'while :; do :; done' & done; wait"
    with ThreadPoolExecutor(1) as ex:
        fut = ex.submit(switch.run, burn, timeout=dur + 60)
        time.sleep(dur / 2)
        probe = switch.run("cat /proc/loadavg", timeout=30)
        checks.check(probe.ok, f"{switch.name}: unresponsive under CPU load")
        record_property("loadavg_under_stress", probe.stdout.strip())
        fut.result()
    after = _health_snapshot(switch)
    checks.check(not before["containers"] - after["containers"],
                 f"{switch.name}: containers lost under load: {sorted(before['containers'] - after['containers'])}")
    checks.check(not before["oper_up"] - after["oper_up"],
                 f"{switch.name}: ports went down under load: {sorted(before['oper_up'] - after['oper_up'])[:20]}")
    checks.check(not before["lldp"] - after["lldp"],
                 f"{switch.name}: LLDP neighbours lost under load: {sorted(before['lldp'] - after['lldp'])[:20]}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-013")
def test_platform_summary(switch, inventory, checks):
    s = switch.platform_summary()
    for key in ("Platform", "HwSKU", "ASIC"):
        checks.check(s.get(key, "").upper() not in BAD_VERSIONS, f"{switch.name}: platform summary {key}={s.get(key)!r}")
    expected = inventory.th("switch.platform") or {}
    for key, want in expected.items():
        checks.check(s.get(key) == str(want), f"{switch.name}: {key}={s.get(key)!r}, expected {want!r}")
    n_ns = len(inventory.th("switch.namespaces") or ["asic0", "asic1"])
    if "ASIC Count" in s:
        checks.check(s["ASIC Count"] == str(n_ns), f"{switch.name}: ASIC Count {s['ASIC Count']}, expected {n_ns}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-014")
def test_platform_eeprom(switch, checks):
    e = switch.syseeprom()
    for key in ("Product Name", "Serial Number", "Base MAC Address"):
        checks.check(e.get(key, "").upper() not in BAD_VERSIONS, f"{switch.name}: EEPROM {key}={e.get(key)!r}")
    mac = e.get("Base MAC Address", "")
    checks.check(re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac) and mac not in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"),
                 f"{switch.name}: invalid Base MAC {mac!r}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-016")
def test_multi_asic_namespaces(switch, inventory, checks):
    expected = inventory.th("switch.namespaces") or ["asic0", "asic1"]
    netns = set(switch.netns())
    checks.check(set(expected) <= netns, f"{switch.name}: missing namespaces {sorted(set(expected) - netns)} (have {sorted(netns)})")
    running = set(switch.containers())
    for ns in expected:
        idx = ns.removeprefix("asic")
        for svc in ("swss", "syncd", "bgp"):
            if svc == "bgp" and not inventory.th("switch.require_bgp_containers", False):
                continue
            checks.check(f"{svc}{idx}" in running, f"{switch.name}: container {svc}{idx} not running")
    checks.assert_all()


# ------------------------------------------------------------- interfaces

def _expected_up(inventory, switch):
    """``switch.expected_up_ports``: "described" (ports with a cabling description), a list of port
    names, or a regex string, for the ports that are actually cabled (a mini rack leaves most fabric ports admin-up but unconnected).
    Per-switch override: ``expected_up_ports`` on the switch's inventory entry."""
    spec = switch.host.extra.get("expected_up_ports", inventory.th("switch.expected_up_ports"))
    if spec is None:
        return None
    if spec == "described":
        # a port with a CONFIG_DB cabling description ('To ComputeTray Slot../GPU..[..]/Port..') is cabled;
        # on MR3 these are exactly the GPU-facing ports, unused ports carry no description
        described = {p for ns in switch.namespaces() for p, d in switch.port_descriptions(ns).items() if d}
        return lambda port: port in described
    if isinstance(spec, str):
        rx = re.compile(spec)
        return lambda port: bool(rx.fullmatch(port))
    names = set(spec)
    return lambda port: port in names


@pytest.mark.tc("TC-SWH-030")
def test_interfaces_admin_oper_state(switch, inventory, checks):
    all_up = inventory.th("switch.expect_all_admin_up", False)
    expected = _expected_up(inventory, switch)
    from helios.quarantine import quarantined_ports
    for ns, rows in switch.all_interfaces_status().items():
        checks.check(rows, f"{switch.name}/{ns}: no fabric interfaces listed")
        skip = quarantined_ports(switch, ns, inventory)     # cabled to a quarantined tray
        try:            # cabling context for the messages ('To ComputeTray Slot4/GPU3[19]/Port33')
            desc = switch.port_descriptions(ns)
        except Exception:  # noqa: BLE001 - context only
            desc = {}
        for r in rows:
            if r["Interface"] in skip:
                continue
            admin, oper = r.get("Admin", "").lower(), r.get("Oper", "").lower()
            port = r["Interface"] + (f" ({desc[r['Interface']]})" if desc.get(r["Interface"]) else "")
            if expected is not None:
                if expected(r["Interface"]):
                    checks.check(admin == "up" and oper == "up",
                                 f"{switch.name}/{ns}: {port} expected up, admin {admin} oper {oper}")
                continue
            if all_up:
                checks.check(admin == "up", f"{switch.name}/{ns}: {port} admin {admin}")
            if admin == "up":
                checks.check(oper == "up", f"{switch.name}/{ns}: {port} admin up but oper {oper}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-031")
def test_lldp_connected_interfaces_up(switch, inventory, checks):
    total = 0
    for ns, rows in switch.all_interfaces_status().items():
        oper = {r["Interface"]: r.get("Oper", "").lower() for r in rows}
        lldp = [p for p in switch.lldp_ports(ns) if switch.is_fabric_port(p)]
        total += len(lldp)
        for p in lldp:
            checks.check(oper.get(p) == "up", f"{switch.name}/{ns}: {p} has LLDP neighbour but oper {oper.get(p)}")
    expected = inventory.th("switch.expected_lldp_neighbors")
    checks.check(total > 0, f"{switch.name}: no LLDP neighbours on any fabric port")
    if expected is not None:
        checks.check(total == expected, f"{switch.name}: {total} LLDP neighbours, expected {expected}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-032")
def test_fabric_interfaces_speed(switch, inventory, checks):
    default = parse_speed_gbps(str(inventory.th("switch.interface_speed", "400G")))
    # per-port exceptions, e.g. 100G uplinks: {Ethernet513: 100G}
    overrides = {p: parse_speed_gbps(str(v)) for p, v in (inventory.th("switch.port_speed_overrides") or {}).items()}
    for ns, rows in switch.all_interfaces_status().items():
        for r in rows:
            if r.get("Admin", "").lower() != "up":
                continue
            want = overrides.get(r["Interface"], default)
            got = parse_speed_gbps(r.get("Speed", ""))
            checks.check(got == want, f"{switch.name}/{ns}: {r['Interface']} speed {r.get('Speed')}, expected {want:g}G")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-033")
def test_interface_mtu(switch, inventory, checks):
    want = int(inventory.th("switch.interface_mtu", 9100))
    for ns, rows in switch.all_interfaces_status().items():
        for r in rows:
            checks.check(to_float(r.get("MTU")) == want, f"{switch.name}/{ns}: {r['Interface']} MTU {r.get('MTU')}, expected {want}")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-034")
def test_no_rx_tx_errors(switch, inventory, checks, record_property):
    """Counter deltas over a window (no clear, so concurrent tests aren't disturbed)."""
    window = float(inventory.th("switch.err_window_s", 30))
    max_delta = int(inventory.th("switch.max_err_delta", 0))
    max_abs = inventory.th("switch.max_err_absolute")
    cols = ("RX_ERR", "TX_ERR")
    before = {ns: switch.interfaces_counters(ns) for ns in switch.namespaces()}
    time.sleep(window)
    for ns in switch.namespaces():
        after = switch.interfaces_counters(ns)
        for port, row in after.items():
            for c in cols:
                b, a = to_float(before[ns].get(port, {}).get(c)), to_float(row.get(c))
                if a is None:
                    continue
                if b is not None:
                    checks.check(a - b <= max_delta, f"{switch.name}/{ns}: {port} {c} +{a - b:.0f} in {window:.0f}s")
                if max_abs is not None:
                    checks.check(a <= max_abs, f"{switch.name}/{ns}: {port} {c}={a:.0f} (> {max_abs})")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-035")
def test_vlan_table_on_all_asics(switch, inventory, checks):
    min_vlans = int(inventory.th("switch.min_vlans", 1))
    for ns in switch.namespaces():
        vlans = switch.vlans(ns)
        checks.check(len(vlans) >= min_vlans, f"{switch.name}/{ns}: {len(vlans)} VLANs configured (< {min_vlans})")
    checks.assert_all()


@pytest.mark.tc("TC-SWH-036")
def test_vlan_membership_on_all_asics(switch, checks):
    for ns in switch.namespaces():
        vlans = switch.vlans(ns)
        members = switch.vlan_members(ns)
        checks.check(vlans, f"{switch.name}/{ns}: no VLANs configured")
        for v in vlans:
            checks.check(members.get(v), f"{switch.name}/{ns}: {v} has no members")
        orphans = sorted(set(members) - set(vlans))
        checks.check(not orphans, f"{switch.name}/{ns}: VLAN_MEMBER entries for undefined VLANs {orphans}")
    checks.assert_all()
