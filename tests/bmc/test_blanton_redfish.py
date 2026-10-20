"""TC-BLT-7.x - Blanton switch BMC over Redfish, READ-ONLY cases of Juniper TPI-151974 v1.2 ("E-RLI42381 - Blantons:
BMC support", sections 7.2, 7.3, 7.4 and the switch-side cases of 7.5.9 / 7.5.13). Added 20 Oct 2026.

GETs only (plus one unauthenticated GET in TC-BLT-7.3.5-1). Cases whose plan steps write - SubmitTestEvent, user /
session / subscription / metric-report creation, ClearLog, CSR, PATCH, resets, firmware updates - are NOT here; a task
case that would trigger a Manager.Reset to create a task skips instead when the BMC has none. Resource ids are
discovered (helios.bmc.discover): the plan writes Managers/bmc vs Managers/BMC and Chassis/Chassis_0, real ids differ.
Exact @odata.type versions are the plan's expectation; thresholds.blanton_bmc.strict_types: false checks the type
name only. Each case runs on every switch BMC of the inventory (bmc, bmc_user, BMC password - see conftest.py).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import pytest

from helios import bmc as B

NS_PREFIX = re.compile(r"/(?:compute|switch|pshelf|scap)\d+_")      # rack-manager namespace prefixes (ARIM)
MAC_RX = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def _strict(inventory) -> bool:
    return inventory.th("blanton_bmc.strict_types", True) is not False


def run_each(bmcs, checks, fn):
    """fn(rf, d, fail) per switch BMC; fail(msg) records '<switch>: msg'. Connection / HTTP errors fail that switch."""
    out = {}
    for b in bmcs:
        def fail(msg, _n=b["name"]):
            checks.fail(f"{_n}: {msg}")
        try:
            out[b["name"]] = fn(b["rf"], B.discover(b["rf"]), fail)
        except B.BmcError as e:
            fail(f"BMC {b['address']}: {e}")
    checks.assert_all()
    return out


def odata_type(obj: dict, expected: str, fail, strict: bool, what: str) -> None:
    """@odata.type = expected ('#Name.vX_Y_Z.Name'); strict=False compares the type name only."""
    got = str(obj.get("@odata.type") or "")
    if strict and got != expected:
        fail(f"{what}: @odata.type {got!r}, the plan expects {expected!r}")
    elif not strict and got.split(".")[0] != expected.split(".")[0]:
        fail(f"{what}: @odata.type {got!r} is not a {expected.split('.')[0].lstrip('#')}")


def need(obj: dict, keys, fail, what: str) -> None:
    for k in keys:
        v = obj.get(k)
        if v in (None, "", [], {}):
            fail(f"{what}: {k} missing / empty")


def first_member(rf, path):
    m = rf.members(path)
    return m[0] if m else None


def iso(ts) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ------------------------------------------------------------------ 7.2.1 Service root & metadata
@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.1-1")
def test_service_root(each_switch_bmc, checks):
    def f(rf, d, fail):
        r = d["root"]
        if str(r.get("@odata.id", "")).rstrip("/") != "/redfish/v1":
            fail(f"service root @odata.id {r.get('@odata.id')!r}")
        need(r, ("Chassis", "Systems", "Managers"), fail, "service root")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.1-2")
def test_odata_service_document(each_switch_bmc, checks):
    def f(rf, d, fail):
        st, ctype, _ = rf.request("GET", "/redfish/v1/odata")
        if st != 200:
            return fail(f"GET /redfish/v1/odata -> HTTP {st}")
        o = rf.get("/redfish/v1/odata")
        if not str(o.get("@odata.context", "")).endswith("$metadata"):
            fail(f"@odata.context {o.get('@odata.context')!r} does not point to $metadata")
        if not isinstance(o.get("value"), list) or not o["value"]:
            fail("value (the entity sets) missing / empty")
        if "application/json" not in ctype:
            fail(f"Content-Type {ctype!r}")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.1-3")
def test_schema_metadata(each_switch_bmc, checks):
    def f(rf, d, fail):
        st, ctype, text = rf.request("GET", "/redfish/v1/$metadata")
        if st != 200:
            return fail(f"GET $metadata -> HTTP {st}")
        low = text.lower()
        for marker in ("edmx", "schema", "namespace"):
            if marker not in low:
                fail(f"$metadata has no {marker!r} (not the EDMX XML document)")
    run_each(each_switch_bmc, checks, f)


# ------------------------------------------------------------------ 7.2.2 Chassis & system
@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.2-1")
def test_chassis_collection(each_switch_bmc, checks, inventory, record_property):
    want = inventory.th("blanton_bmc.chassis_count", 1)

    def f(rf, d, fail):
        c = rf.get("/redfish/v1/Chassis")
        n = c.get("Members@odata.count")
        if n != len(c.get("Members") or []):
            fail(f"Members@odata.count {n} != {len(c.get('Members') or [])} members")
        if want is not None and len(c.get("Members") or []) != int(want):
            fail(f"{len(c.get('Members') or [])} chassis, the plan expects {want} on Blanton (thresholds.blanton_bmc.chassis_count)")
        return d["chassis"]
    record_property("chassis", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.2-2")
def test_chassis_detail(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        if not d["main_chassis"]:
            return fail("no chassis")
        c = rf.get(d["main_chassis"])
        need(c, ("Id", "Name", "ChassisType", "Manufacturer", "Model", "Status"), fail, d["main_chassis"])
        return {k: c.get(k) for k in ("Id", "ChassisType", "Manufacturer", "Model", "SerialNumber", "PartNumber")}
    record_property("chassis_detail", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.2-3")
def test_system_detail(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        if not d["system"]:
            return fail("no ComputerSystem")
        s = rf.get(d["system"])
        if s.get("PowerState") not in ("On", "Off", "PoweringOn", "PoweringOff"):
            fail(f"PowerState {s.get('PowerState')!r}")
        return {"PowerState": s.get("PowerState"), "ProcessorSummary": s.get("ProcessorSummary"),
                "MemorySummary": s.get("MemorySummary")}
    record_property("system", run_each(each_switch_bmc, checks, f))


# ------------------------------------------------------------------ 7.2.3 Sensors
def _sensors_path(rf, d):
    c = rf.get(d["main_chassis"]) if d["main_chassis"] else {}
    return (c.get("Sensors") or {}).get("@odata.id")


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.3-1")
def test_list_sensors(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        p = _sensors_path(rf, d)
        if not p:
            return fail("the chassis has no Sensors collection")
        col = rf.get(p)
        if not col.get("Members"):
            fail("Sensors collection is empty")
        return len(col.get("Members") or [])
    record_property("sensors", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.3-2")
def test_leak_detector_sensor(each_switch_bmc, checks, record_property):
    """Sensors/LeakDetector_0: Reading 0 = no leak; 1 = leak (fails, with its health)."""
    def f(rf, d, fail):
        p = _sensors_path(rf, d)
        s = rf.try_get(f"{p}/LeakDetector_0") if p else None
        if s is None:
            return fail("no Sensors/LeakDetector_0 (the plan expects one)")
        if s.get("Reading") not in (0, 0.0):
            fail(f"LeakDetector_0 Reading {s.get('Reading')!r} (1 = leak detected), health {B._health(s) or '-'}")
        return {"Reading": s.get("Reading"), "Status": s.get("Status")}
    record_property("leak_detector", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.3-3")
def test_legacy_thermal(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        c = rf.get(d["main_chassis"])
        ref = (c.get("Thermal") or {}).get("@odata.id")
        if not ref:
            return fail("the chassis has no legacy Thermal resource")
        t = rf.get(ref)
        temps = t.get("Temperatures") or []
        if not temps:
            fail("Thermal: no Temperatures")
        for x in temps:
            if not isinstance(x.get("ReadingCelsius"), (int, float)):
                fail(f"Thermal: {x.get('Name')}: no ReadingCelsius")
        return {"temperatures": len(temps), "fans": len(t.get("Fans") or [])}
    record_property("thermal", run_each(each_switch_bmc, checks, f))


# ------------------------------------------------------------------ 7.2.4 / 7.2.5 Leak detection, ThermalSubsystem
def _thermal_subsystem(rf, d):
    c = rf.get(d["main_chassis"])
    return (c.get("ThermalSubsystem") or {}).get("@odata.id")


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.4-1")
def test_leak_detection_resource(each_switch_bmc, checks):
    def f(rf, d, fail):
        ts = _thermal_subsystem(rf, d)
        ld = rf.try_get(f"{ts}/LeakDetection") if ts else None
        if ld is None:
            return fail("no ThermalSubsystem/LeakDetection")
        if not (ld.get("LeakDetectors") or {}).get("@odata.id"):
            fail("LeakDetection has no LeakDetectors link")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.4-2")
def test_leak_detectors_list(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        ts = _thermal_subsystem(rf, d)
        col = rf.try_get(f"{ts}/LeakDetection/LeakDetectors") if ts else None
        if col is None:
            return fail("no LeakDetection/LeakDetectors collection")
        if not col.get("Members"):
            fail("LeakDetectors collection is empty")
        return [m.get("@odata.id") for m in col.get("Members") or []]
    record_property("leak_detectors", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.4-3")
def test_leak_detector_detail(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        ts = _thermal_subsystem(rf, d)
        ld = rf.try_get(f"{ts}/LeakDetection/LeakDetectors/LeakDetector_0") if ts else None
        if ld is None:
            return fail("no LeakDetectors/LeakDetector_0")
        need(ld, ("DetectorState", "Status"), fail, "LeakDetector_0")
        if "Enabled" not in ld:
            fail("LeakDetector_0: Enabled missing")
        if ld.get("DetectorState") not in (None, "OK"):
            fail(f"LeakDetector_0 DetectorState {ld.get('DetectorState')!r}")
        return {k: ld.get(k) for k in ("Enabled", "DetectorState", "Status")}
    record_property("leak_detector", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.5-1")
def test_thermal_subsystem(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        ts = _thermal_subsystem(rf, d)
        if not ts:
            return fail("the chassis has no ThermalSubsystem")
        t = rf.get(ts)
        ld = (t.get("LeakDetection") or {}).get("@odata.id")
        if not ld:
            fail("ThermalSubsystem has no LeakDetection link")
        else:
            rf.get(ld)
        return {k: bool(t.get(k)) for k in ("LeakDetection", "Fans", "ThermalMetrics")}
    record_property("thermal_subsystem", run_each(each_switch_bmc, checks, f))


# ------------------------------------------------------------------ 7.2.6 Manager
@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.6-1")
def test_bmc_detail(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        if not d["manager"]:
            return fail("no Manager")
        m = rf.get(d["manager"])
        need(m, ("FirmwareVersion", "Status", "DateTime", "ManagerType"), fail, d["manager"])
        if "#Manager.Reset" not in (m.get("Actions") or {}):
            fail("Actions has no #Manager.Reset")
        return {k: m.get(k) for k in ("FirmwareVersion", "ManagerType", "DateTime")}
    record_property("manager", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.6-2")
def test_network_protocol(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        n = rf.get(d["manager"].rstrip("/") + "/NetworkProtocol")
        for p in ("HTTPS", "SSH"):
            if (n.get(p) or {}).get("ProtocolEnabled") is not True:
                fail(f"NetworkProtocol {p} not enabled: {n.get(p)}")
        return {p: n.get(p) for p in ("HTTPS", "SSH", "IPMI", "SNMP", "SSDP", "NTP") if p in n}
    record_property("network_protocol", run_each(each_switch_bmc, checks, f))


# ------------------------------------------------------------------ 7.2.7 / 7.2.8 Accounts, roles, sessions (reads)
@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.7-1")
def test_list_users(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        accts = rf.members("/redfish/v1/AccountService/Accounts")
        if not accts:
            return fail("no accounts")
        names = [rf.get(a).get("UserName") for a in accts]
        if "root" not in names:
            fail(f"no root account (accounts: {names})")
        return names
    record_property("accounts", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.7-7")
def test_list_roles(each_switch_bmc, checks):
    def f(rf, d, fail):
        roles = {r.rstrip("/").rsplit("/", 1)[-1] for r in rf.members("/redfish/v1/AccountService/Roles")}
        for r in ("Administrator", "Operator", "ReadOnly"):
            if r not in roles:
                fail(f"role {r} missing (roles: {sorted(roles)})")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.8-3")
def test_list_sessions(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        ss = rf.get("/redfish/v1/SessionService")
        ref = (ss.get("Sessions") or {}).get("@odata.id") or "/redfish/v1/SessionService/Sessions"
        return len(rf.members(ref))
    record_property("active_sessions", run_each(each_switch_bmc, checks, f))


# ------------------------------------------------------------------ 7.2.9 Event service (reads)
@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.9-1")
def test_heartbeat(each_switch_bmc, checks, inventory, record_property):
    """HEARTBEAT_COUNT GETs of the service root, HEARTBEAT_INTERVAL apart: all 200 with RedfishVersion."""
    count = int(inventory.th("blanton_bmc.heartbeat_count", 5))
    every = float(inventory.th("blanton_bmc.heartbeat_interval_s", 2))

    def f(rf, d, fail):
        ok = 0
        for i in range(count):
            try:
                r = rf.get("/redfish/v1/")
                ok += 1 if r.get("RedfishVersion") else 0
                if not r.get("RedfishVersion"):
                    fail(f"heartbeat {i + 1}: no RedfishVersion")
            except B.BmcError as e:
                fail(f"heartbeat {i + 1}: {e}")
            if i + 1 < count:
                time.sleep(every)
        return f"{ok}/{count}"
    record_property("heartbeat", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.9-14")
def test_native_chassis_uris(each_switch_bmc, checks):
    """Native local chassis / sensor URIs: no rack-manager namespace prefix (no URI rewriting on a standalone switch)."""
    def f(rf, d, fail):
        for c in d["chassis"]:
            if "_PowerShelf" in c or NS_PREFIX.search(c):
                fail(f"chassis URI {c} carries a namespace prefix")
        if rf.try_get("/redfish/v1/Chassis/PowerShelf") is not None:       # the plan's example chassis, where present
            s = rf.try_get("/redfish/v1/Chassis/PowerShelf/Sensors/ChassisTemp")
            if s is None:
                fail("Chassis/PowerShelf has no Sensors/ChassisTemp")
            elif s.get("@odata.id") != "/redfish/v1/Chassis/PowerShelf/Sensors/ChassisTemp":
                fail(f"ChassisTemp @odata.id {s.get('@odata.id')!r} is not the native local URI")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.9-18")
def test_manager_info(each_switch_bmc, checks):
    def f(rf, d, fail):
        if not d["managers"]:
            return fail("no managers")
        m = rf.get(d["manager"])
        need(m, ("FirmwareVersion", "Status"), fail, d["manager"])
        if (m.get("Status") or {}).get("State") != "Enabled":
            fail(f"Manager Status.State {(m.get('Status') or {}).get('State')!r}, expected Enabled")
    run_each(each_switch_bmc, checks, f)


# ------------------------------------------------------------------ 7.2.10 Log service (reads)
def _log_service(rf, d, fail):
    if not d["log_service"]:
        fail("the manager has no LogServices")
        return None
    return rf.get(d["log_service"])


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.10-1")
def test_log_service_accessible(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        ls = _log_service(rf, d, fail)
        if ls is None:
            return None
        ref = (ls.get("Entries") or {}).get("@odata.id")
        if not ref:
            return fail(f"{d['log_service']}: no Entries link")
        e = rf.get(ref)
        if not isinstance(e.get("Members"), list):
            fail(f"{ref}: Members is not a list")
        return {"log_service": d["log_service"], "entries": len(e.get("Members") or [])}
    record_property("log", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.10-13")
def test_log_max_records(each_switch_bmc, checks):
    def f(rf, d, fail):
        ls = _log_service(rf, d, fail)
        if ls is not None and ls.get("MaxNumberOfRecords") != 10000:
            fail(f"MaxNumberOfRecords {ls.get('MaxNumberOfRecords')!r}, the plan expects 10000 (Table 37)")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.10-14")
def test_log_overwrite_policy(each_switch_bmc, checks):
    def f(rf, d, fail):
        ls = _log_service(rf, d, fail)
        if ls is not None and ls.get("OverWritePolicy") != "WrapsWhenFull":
            fail(f"OverWritePolicy {ls.get('OverWritePolicy')!r}, the plan expects 'WrapsWhenFull'")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.10-15")
def test_log_clear_action_advertised(each_switch_bmc, checks):
    """ClearLog is ADVERTISED with a target (never invoked here)."""
    def f(rf, d, fail):
        ls = _log_service(rf, d, fail)
        if ls is not None and not ((ls.get("Actions") or {}).get("#LogService.ClearLog") or {}).get("target"):
            fail("Actions has no #LogService.ClearLog target")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.10-18")
def test_log_table37_together(each_switch_bmc, checks):
    def f(rf, d, fail):
        ls = _log_service(rf, d, fail)
        if ls is None:
            return
        bad = []
        if ls.get("MaxNumberOfRecords") != 10000:
            bad.append(f"MaxNumberOfRecords={ls.get('MaxNumberOfRecords')!r}")
        if ls.get("OverWritePolicy") != "WrapsWhenFull":
            bad.append(f"OverWritePolicy={ls.get('OverWritePolicy')!r}")
        if not ((ls.get("Actions") or {}).get("#LogService.ClearLog") or {}).get("target"):
            bad.append("no ClearLog action")
        if bad:
            fail("Table 37 not met in one GET: " + ", ".join(bad))
    run_each(each_switch_bmc, checks, f)


# ------------------------------------------------------------------ 7.2.11 / 7.4 Update service & firmware inventory
FW = "/redfish/v1/UpdateService/FirmwareInventory"


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.11-1")
def test_update_service_overview(each_switch_bmc, checks):
    def f(rf, d, fail):
        u = rf.get("/redfish/v1/UpdateService")
        if (u.get("FirmwareInventory") or {}).get("@odata.id") != FW:
            fail(f"FirmwareInventory link {(u.get('FirmwareInventory') or {}).get('@odata.id')!r}")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.11-2")
def test_firmware_inventory(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        out = {}
        for m in rf.members(FW):
            e = rf.get(m)
            out[e.get("Id")] = e.get("Version")
            if not e.get("Version"):
                fail(f"{m}: no Version")
        if not out:
            fail("FirmwareInventory is empty")
        return out
    record_property("firmware", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.4-1")
def test_update_service_configuration(each_switch_bmc, checks, inventory):
    def f(rf, d, fail):
        u = rf.get("/redfish/v1/UpdateService")
        odata_type(u, "#UpdateService.v1_17_0.UpdateService", fail, _strict(inventory), "UpdateService")
        if u.get("ServiceEnabled") is not True:
            fail("UpdateService ServiceEnabled is not true")
        acts = u.get("Actions") or {}
        for a in ("#UpdateService.SimpleUpdate", "#UpdateService.StartUpdate"):
            if a not in acts:
                fail(f"Actions has no {a}")
        if not u.get("FirmwareInventory"):
            fail("no FirmwareInventory link")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.4-2")
def test_firmware_inventory_collection(each_switch_bmc, checks):
    def f(rf, d, fail):
        c = rf.get(FW)
        if "SoftwareInventoryCollection" not in str(c.get("@odata.type")):
            fail(f"@odata.type {c.get('@odata.type')!r} is not a SoftwareInventoryCollection")
        ms = [m.get("@odata.id") for m in c.get("Members") or []]
        if not ms:
            fail("FirmwareInventory is empty")
        for m in ms[:3]:
            rf.get(m)
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.4-6")
def test_firmware_inventory_entry(each_switch_bmc, checks, inventory):
    def f(rf, d, fail):
        m = first_member(rf, FW)
        if not m:
            return fail("FirmwareInventory is empty")
        e = rf.get(m)
        odata_type(e, "#SoftwareInventory.v1_13_0.SoftwareInventory", fail, _strict(inventory), m)
        need(e, ("Version", "Name"), fail, m)
        if "Updateable" not in e:
            fail(f"{m}: Updateable missing")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.4-7")
def test_firmware_component_ids(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        ids = [m.rstrip("/").rsplit("/", 1)[-1] for m in rf.members(FW)]
        if "BMC_Firmware" not in ids:
            fail(f"no BMC_Firmware entry (ids: {ids})")
        for i in ids:
            if NS_PREFIX.search("/" + i):
                fail(f"firmware id {i} carries a rack-manager namespace prefix")
        return {"ids": ids, "has": {k: any(k.lower() in i.lower() for i in ids) for k in ("NOS", "CPLD", "BIOS")}}
    record_property("firmware_ids", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.4-8")
def test_firmware_entries_accessible(each_switch_bmc, checks):
    def f(rf, d, fail):
        for m in rf.members(FW):
            e = rf.get(m)
            if "SoftwareInventory" not in str(e.get("@odata.type")):
                fail(f"{m}: @odata.type {e.get('@odata.type')!r}")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.4-11")
def test_bmc_firmware_entry(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        ms = [m for m in rf.members(FW) if "bmc" in m.rstrip("/").rsplit("/", 1)[-1].lower()]
        if not ms:
            return fail("no firmware entry with BMC in its id")
        e = rf.get(next((m for m in ms if m.endswith("BMC_Firmware")), ms[0]))
        if not e.get("Version"):
            fail(f"{e.get('@odata.id')}: no Version")
        return e.get("Version")
    record_property("bmc_firmware_version", run_each(each_switch_bmc, checks, f))


# ------------------------------------------------------------------ 7.2.12 Network
def _eth0(rf, d):
    return rf.try_get(d["manager"].rstrip("/") + "/EthernetInterfaces/eth0")


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.12-1")
def test_ethernet_interfaces(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        ids = [m.rstrip("/").rsplit("/", 1)[-1] for m in rf.members(d["manager"].rstrip("/") + "/EthernetInterfaces")]
        if "eth0" not in ids:
            fail(f"no eth0 (interfaces: {ids})")
        return ids
    record_property("interfaces", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.12-2")
def test_eth0_details(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        e = _eth0(rf, d)
        if e is None:
            return fail("no EthernetInterfaces/eth0")
        if not MAC_RX.match(str(e.get("MACAddress") or "")):
            fail(f"eth0 MACAddress {e.get('MACAddress')!r} is not xx:xx:xx:xx:xx:xx")
        if not e.get("IPv4Addresses"):
            fail("eth0 has no IPv4Addresses")
        return {"MAC": e.get("MACAddress"), "IPv4": [a.get("Address") for a in e.get("IPv4Addresses") or []]}
    record_property("eth0", run_each(each_switch_bmc, checks, f))


# ------------------------------------------------------------------ 7.2.13 Certificates (reads)
def _https_certs(rf, d):
    return d["manager"].rstrip("/") + "/NetworkProtocol/HTTPS/Certificates"


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.13-1")
def test_certificate_service_root(each_switch_bmc, checks):
    def f(rf, d, fail):
        c = rf.get("/redfish/v1/CertificateService")
        if not c.get("CertificateLocations"):
            fail("no CertificateLocations link")
        acts = c.get("Actions") or {}
        for a in ("#CertificateService.ReplaceCertificate", "#CertificateService.GenerateCSR"):
            if a not in acts:
                fail(f"Actions has no {a}")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.13-2")
def test_https_certificates(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        m = first_member(rf, _https_certs(rf, d))
        if not m:
            return fail("no HTTPS certificate")
        c = rf.get(m)
        need(c, ("Subject", "Issuer"), fail, m)
        return {"Subject": c.get("Subject"), "Issuer": c.get("Issuer"), "ValidNotAfter": c.get("ValidNotAfter")}
    record_property("https_certificate", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.13-3")
def test_individual_certificate(each_switch_bmc, checks):
    def f(rf, d, fail):
        m = first_member(rf, _https_certs(rf, d))
        if not m:
            return fail("no HTTPS certificate")
        c = rf.get(m)
        if "Certificate" not in str(c.get("@odata.type")):
            fail(f"{m}: @odata.type {c.get('@odata.type')!r}")
        need(c, ("Issuer", "Subject", "ValidNotAfter", "ValidNotBefore"), fail, m)
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.13-6")
def test_certificate_action_targets(each_switch_bmc, checks):
    def f(rf, d, fail):
        acts = rf.get("/redfish/v1/CertificateService").get("Actions") or {}
        for a in ("ReplaceCertificate", "GenerateCSR"):
            t = str((acts.get(f"#CertificateService.{a}") or {}).get("target") or "")
            if f"CertificateService.{a}" not in t or "/Actions/" not in t:
                fail(f"{a} target {t!r} is not /redfish/v1/CertificateService/Actions/CertificateService.{a}")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.13-7")
def test_certificate_validity(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        m = first_member(rf, _https_certs(rf, d))
        if not m:
            return fail("no HTTPS certificate")
        c = rf.get(m)
        nb, na, now = iso(c.get("ValidNotBefore")), iso(c.get("ValidNotAfter")), datetime.now(timezone.utc)
        if nb is None or na is None:
            return fail(f"validity not ISO 8601: {c.get('ValidNotBefore')!r} .. {c.get('ValidNotAfter')!r}")
        if nb > now:
            fail(f"certificate not valid yet (ValidNotBefore {nb.isoformat()})")
        if na < now:
            fail(f"certificate EXPIRED (ValidNotAfter {na.isoformat()})")
        return f"{(na - now).days} days left"
    record_property("certificate_validity", run_each(each_switch_bmc, checks, f))


# ------------------------------------------------------------------ 7.2.14 Task & telemetry (reads)
TASK_STATES = {"New", "Starting", "Running", "Completed", "Exception", "Cancelled", "Pending", "Stopping", "Suspended",
               "Interrupted", "Killed", "Cancelling", "Service"}
TERMINAL = {"Completed", "Exception", "Cancelled", "Killed"}


def _a_task(rf):
    """An existing task, or None - never triggers a Manager.Reset to create one (the plan's step 2 is disruptive)."""
    ms = rf.members("/redfish/v1/TaskService/Tasks")
    return rf.get(ms[-1]) if ms else None


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-1")
def test_task_service(each_switch_bmc, checks, inventory):
    def f(rf, d, fail):
        t = rf.get("/redfish/v1/TaskService")
        odata_type(t, "#TaskService.v1_2_1.TaskService", fail, _strict(inventory), "TaskService")
        if t.get("ServiceEnabled") is not True:
            fail("TaskService ServiceEnabled is not true")
        if (t.get("Tasks") or {}).get("@odata.id") != "/redfish/v1/TaskService/Tasks":
            fail(f"Tasks link {(t.get('Tasks') or {}).get('@odata.id')!r}")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-2")
def test_tasks_collection(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        c = rf.get("/redfish/v1/TaskService/Tasks")
        if "TaskCollection" not in str(c.get("@odata.type")):
            fail(f"@odata.type {c.get('@odata.type')!r}")
        if not isinstance(c.get("Members"), list):
            fail("Members is not a list")
        return len(c.get("Members") or [])
    record_property("tasks", run_each(each_switch_bmc, checks, f))


def _task_case(each_switch_bmc, checks, judge):
    found = {}

    def f(rf, d, fail):
        t = _a_task(rf)
        found[id(rf)] = t is not None
        if t is not None:
            judge(t, fail)
    run_each(each_switch_bmc, checks, f)
    if not any(found.values()):
        pytest.skip("no task on the BMC; the plan would create one with a Manager.Reset (disruptive) - not done here")


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-3")
def test_task_required_fields(each_switch_bmc, checks, inventory):
    def judge(t, fail):
        odata_type(t, "#Task.v1_7_4.Task", fail, _strict(inventory), str(t.get("@odata.id")))
        if t.get("TaskState") not in TASK_STATES:
            fail(f"TaskState {t.get('TaskState')!r}")
        if t.get("TaskStatus") not in ("OK", "Warning", "Critical"):
            fail(f"TaskStatus {t.get('TaskStatus')!r}")
        if not isinstance(t.get("Messages"), list):
            fail("Messages is not a list")
    _task_case(each_switch_bmc, checks, judge)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-6")
def test_task_start_end_time(each_switch_bmc, checks):
    def judge(t, fail):
        if not iso(t.get("StartTime")):
            fail(f"StartTime {t.get('StartTime')!r} is not ISO 8601")
        if t.get("TaskState") in TERMINAL and not iso(t.get("EndTime")):
            fail(f"{t.get('TaskState')} task without an ISO 8601 EndTime ({t.get('EndTime')!r})")
    _task_case(each_switch_bmc, checks, judge)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-8")
def test_task_messages(each_switch_bmc, checks):
    def judge(t, fail):
        msgs = t.get("Messages")
        if not isinstance(msgs, list):
            return fail("Messages is not a list")
        for m in msgs:
            if not m.get("MessageId"):
                fail(f"a task message without MessageId: {m}")
            if m.get("Severity") not in (None, "OK", "Warning", "Critical"):
                fail(f"message Severity {m.get('Severity')!r}")
    _task_case(each_switch_bmc, checks, judge)


TS = "/redfish/v1/TelemetryService"


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-9")
def test_telemetry_service(each_switch_bmc, checks, inventory):
    def f(rf, d, fail):
        t = rf.get(TS)
        odata_type(t, "#TelemetryService.v1_4_1.TelemetryService", fail, _strict(inventory), "TelemetryService")
        if t.get("ServiceEnabled") is not True:
            fail("TelemetryService ServiceEnabled is not true")
        if not ((t.get("Actions") or {}).get("#TelemetryService.SubmitTestMetricReport") or {}).get("target"):
            fail("Actions has no #TelemetryService.SubmitTestMetricReport target")
    run_each(each_switch_bmc, checks, f)


def _collection(rf, path, type_part, fail):
    c = rf.get(path)
    if type_part not in str(c.get("@odata.type")):
        fail(f"{path}: @odata.type {c.get('@odata.type')!r} is not a {type_part}")
    if not isinstance(c.get("Members"), list):
        fail(f"{path}: Members is not a list")
    return [m.get("@odata.id") for m in c.get("Members") or []]


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-11")
def test_metric_definitions_collection(each_switch_bmc, checks, record_property):
    record_property("metric_definitions", run_each(each_switch_bmc, checks, lambda rf, d, fail:
                    len(_collection(rf, f"{TS}/MetricDefinitions", "MetricDefinitionCollection", fail))))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-12")
def test_metric_definition(each_switch_bmc, checks, inventory):
    def f(rf, d, fail):
        m = first_member(rf, f"{TS}/MetricDefinitions")
        if not m:
            return fail("MetricDefinitions is empty")
        md = rf.get(m)
        odata_type(md, "#MetricDefinition.v1_3_5.MetricDefinition", fail, _strict(inventory), m)
        if not isinstance(md.get("MetricProperties"), list) or not md["MetricProperties"]:
            fail(f"{m}: MetricProperties missing / empty")
        need(md, ("MetricType", "Units"), fail, m)
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-13")
def test_metric_report_definitions_collection(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, lambda rf, d, fail:
             _collection(rf, f"{TS}/MetricReportDefinitions", "MetricReportDefinitionCollection", fail))


def _reports_collection(rf, d, fail):
    return _collection(rf, f"{TS}/MetricReports", "MetricReportCollection", fail)


def _report(rf, d, fail, strict):
    m = first_member(rf, f"{TS}/MetricReports")
    if not m:
        return False
    r = rf.get(m)
    odata_type(r, "#MetricReport.v1_5_2.MetricReport", fail, strict, m)
    if not iso(r.get("Timestamp")):
        fail(f"{m}: Timestamp {r.get('Timestamp')!r} is not ISO 8601")
    vals = r.get("MetricValues")
    if not isinstance(vals, list):
        fail(f"{m}: MetricValues is not a list")
    elif vals:
        need(vals[0], ("MetricId", "MetricValue", "Timestamp", "MetricProperty"), fail, f"{m} MetricValues[0]")
    return True


def _definitions(rf, n=None):
    ms = rf.members(f"{TS}/MetricDefinitions")
    return [rf.get(m) for m in (ms[:n] if n else ms)]


def _structure(rf, d, fail):
    t = rf.get(TS)
    for k in ("MetricDefinitions", "MetricReportDefinitions", "MetricReports"):
        ref = (t.get(k) or {}).get("@odata.id")
        if not ref:
            fail(f"TelemetryService has no {k} link")
        else:
            rf.get(ref)


def _auto_populated(rf, d, fail):
    ms = rf.members(f"{TS}/MetricDefinitions")
    if not ms:
        return fail("MetricDefinitions is empty (not auto-populated from the switch sensors)")
    for m in ms[:3]:
        md = rf.get(m)
        if "MetricDefinition" not in str(md.get("@odata.type")):
            fail(f"{m}: @odata.type {md.get('@odata.type')!r}")


def _dedup_keys(rf, d, fail):
    for md in _definitions(rf, 5):
        need(md, ("MetricType", "Units", "MetricDataType"), fail, str(md.get("@odata.id")))


def _native_metric_properties(rf, d, fail):
    for md in _definitions(rf, 5):
        props = md.get("MetricProperties")
        if not isinstance(props, list) or not props:
            fail(f"{md.get('@odata.id')}: MetricProperties missing / empty")
            continue
        for p in props:
            if not str(p).startswith("/redfish/v1/"):
                fail(f"{md.get('@odata.id')}: MetricProperty {p!r} does not start with /redfish/v1/")
            elif NS_PREFIX.search(str(p)):
                fail(f"{md.get('@odata.id')}: MetricProperty {p!r} carries a namespace prefix")


def _browse(rf, d, fail):
    mds = _definitions(rf)
    if not mds:
        return fail("MetricDefinitions is empty")
    if not any(str(m.get("Units")) in ("W", "Cel") or re.search(r"temp|power", str(m.get("Name")) + str(m.get("Id")), re.I)
               for m in mds):
        fail("the catalog has no power or temperature metric")
    return len(mds)


def _pick(rf, d, fail):
    m = first_member(rf, f"{TS}/MetricDefinitions")
    if not m:
        return fail("MetricDefinitions is empty")
    md = rf.get(m)
    need(md, ("Name", "MetricType", "Units", "MetricDataType", "MetricProperties"), fail, m)
    for p in (md.get("MetricProperties") or [])[:5]:      # 200 or 404 (a reading sub-property) are both fine
        st, _, _ = rf.request("GET", str(p).split("#")[0])
        if st not in (200, 404):
            fail(f"MetricProperty {p} -> HTTP {st}")


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-16")
def test_metric_reports_collection(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _reports_collection)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-17")
def test_metric_report(each_switch_bmc, checks, inventory):
    seen = run_each(each_switch_bmc, checks, lambda rf, d, fail: _report(rf, d, fail, _strict(inventory)))
    if not any(seen.values()):
        pytest.skip("no MetricReport on the BMC (reports exist only once a report definition is active)")


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-19")
def test_telemetry_structure(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _structure)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-20")
def test_metric_definitions_auto_populated(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _auto_populated)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-21")
def test_metric_dedup_keys(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _dedup_keys)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-22")
def test_metric_properties_native(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _native_metric_properties)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.14-23")
def test_metric_catalog_browse(each_switch_bmc, checks, record_property):
    record_property("metric_definitions", run_each(each_switch_bmc, checks, _browse))


# 7.5.9 cases that the plan runs against the switch BMC (same checks as their 7.2.14 twins)
@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.9-4")
def test_telemetry_structure_759(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _structure)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.9-5")
def test_metric_dedup_keys_759(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _dedup_keys)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.9-6")
def test_metric_properties_native_759(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _native_metric_properties)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.9-7")
def test_metric_catalog_browse_759(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _browse)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.9-8")
def test_metric_pick_759(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _pick)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.9-13")
def test_metric_reports_collection_759(each_switch_bmc, checks):
    run_each(each_switch_bmc, checks, _reports_collection)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.9-14")
def test_metric_report_759(each_switch_bmc, checks, inventory):
    seen = run_each(each_switch_bmc, checks, lambda rf, d, fail: _report(rf, d, fail, _strict(inventory)))
    if not any(seen.values()):
        pytest.skip("no MetricReport on the BMC (reports exist only once a report definition is active)")


# ------------------------------------------------------------------ 7.3 Validation suite (Redfish side)
@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.3.1-1")
def test_vs_service_root_version(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        r = d["root"]
        need(r, ("Managers", "Systems", "Chassis"), fail, "service root")
        if not re.match(r"^\d+\.\d+\.\d+$", str(r.get("RedfishVersion") or "")):
            fail(f"RedfishVersion {r.get('RedfishVersion')!r} is not x.y.z")
        return r.get("RedfishVersion")
    record_property("redfish_version", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.3.1-2")
def test_vs_managers_network_protocol(each_switch_bmc, checks):
    def f(rf, d, fail):
        if len(d["managers"]) < 1:
            fail("no manager")
        n = rf.get(d["manager"].rstrip("/") + "/NetworkProtocol")
        for p in ("HTTPS", "SSH"):
            if (n.get(p) or {}).get("ProtocolEnabled") is not True:
                fail(f"{p} not enabled")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.3.1-3")
def test_vs_eth0(each_switch_bmc, checks):
    def f(rf, d, fail):
        e = _eth0(rf, d)
        if e is None:
            return fail("no EthernetInterfaces/eth0")
        if not MAC_RX.match(str(e.get("MACAddress") or "")):
            fail(f"eth0 MACAddress {e.get('MACAddress')!r}")
        ips = [a.get("Address") for a in e.get("IPv4Addresses") or []]
        if not ips:
            fail("eth0 has no IPv4 address")
        addr = rf.address.split("//")[-1].split(":")[0]
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", addr) and addr not in ips:
            fail(f"eth0 IPv4 {ips} does not include the address used to reach the BMC ({addr})")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.3.4-1")
def test_vs_journal_and_sel(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        base = d["manager"].rstrip("/") + "/LogServices"
        svcs = {m.rstrip("/").rsplit("/", 1)[-1]: m for m in rf.members(base)}
        out = {}
        for name in ("Journal", "EventLog", "SEL", "Log"):
            if name not in svcs:
                continue
            ref = (rf.get(svcs[name]).get("Entries") or {}).get("@odata.id")
            ents = rf.get(ref).get("Members") if ref else None
            out[name] = len(ents or [])
            for e in (ents or [])[-5:]:
                if isinstance(e, dict) and ("Message" in e or "Created" in e):
                    need(e, ("Message", "Created", "Severity"), fail, f"{name} entry {e.get('Id')}")
        if "Journal" not in out:
            fail(f"no Journal LogService (services: {sorted(svcs)})")
        elif not out["Journal"]:
            fail("Journal has no entries")
        if not ({"EventLog", "SEL", "Log"} & set(out)):
            fail(f"no SEL / EventLog LogService (services: {sorted(svcs)})")
        return out
    record_property("log_services", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.3.5-1")
def test_vs_auth_and_tls(each_switch_bmc, checks):
    """An unauthenticated GET is refused (401/403), the authenticated one works, over HTTPS with a valid certificate."""
    def f(rf, d, fail):
        path = d["system"] or "/redfish/v1/Systems"
        st, _, _ = rf.request("GET", path, auth=False)
        if st not in (401, 403):
            fail(f"GET {path} WITHOUT credentials -> HTTP {st} (expected 401 / 403)")
        rf.get(path)
        if not rf.base.startswith("https://"):
            fail(f"the BMC is reached over {rf.base.split(':')[0]}, not HTTPS")
        m = first_member(rf, _https_certs(rf, d))
        na = iso(rf.get(m).get("ValidNotAfter")) if m else None
        if na is None:
            fail("no readable HTTPS certificate")
        elif na < datetime.now(timezone.utc):
            fail(f"HTTPS certificate expired {na.isoformat()}")
    run_each(each_switch_bmc, checks, f)


def _sensor_rows(rf, d, limit):
    p = _sensors_path(rf, d)
    return [rf.get(m) for m in (rf.members(p)[:limit] if p else [])]


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.3.6-1")
def test_vs_sensor_discovery(each_switch_bmc, checks, inventory, record_property):
    """Sensors of several kinds, readings within a physical range (temperature -40..150 C, fan 0..60000 RPM)."""
    limit = int(inventory.th("bmc.sensors_max", 300))

    def f(rf, d, fail):
        rows = _sensor_rows(rf, d, limit)
        kinds = sorted({str(s.get("ReadingType") or s.get("ReadingUnits") or "?") for s in rows})
        if len(kinds) < 2:
            fail(f"only {kinds} sensors (the plan expects temperature, fan, voltage, current, power ...)")
        for s in rows:
            r, kind = s.get("Reading"), str(s.get("ReadingType") or "")
            if not isinstance(r, (int, float)):
                continue
            if kind == "Temperature" and not -40 <= r <= 150:
                fail(f"{s.get('Id')}: {r} C out of physical range")
            if kind == "Rotational" and not 0 <= r <= 60000:
                fail(f"{s.get('Id')}: {r} RPM out of physical range")
        return {"sensors": len(rows), "kinds": kinds}
    record_property("sensor_kinds", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.3.6-2")
def test_vs_threshold_health_mapping(each_switch_bmc, checks, inventory):
    """UpperCritical > UpperCaution, LowerCritical < LowerCaution; Health OK within the caution limits, Critical beyond
    a critical one."""
    limit = int(inventory.th("bmc.sensors_max", 300))

    def lim(th, k):
        v = (th.get(k) or {}).get("Reading") if isinstance(th.get(k), dict) else None
        return v if isinstance(v, (int, float)) else None

    def f(rf, d, fail):
        for s in _sensor_rows(rf, d, limit):
            th, r, h, sid = s.get("Thresholds") or {}, s.get("Reading"), B._health(s), s.get("Id")
            uc, ucr, lc, lcr = lim(th, "UpperCaution"), lim(th, "UpperCritical"), lim(th, "LowerCaution"), lim(th, "LowerCritical")
            if uc is not None and ucr is not None and not ucr > uc:
                fail(f"{sid}: UpperCritical {ucr} not above UpperCaution {uc}")
            if lc is not None and lcr is not None and not lcr < lc:
                fail(f"{sid}: LowerCritical {lcr} not below LowerCaution {lc}")
            if not isinstance(r, (int, float)) or not h:
                continue
            beyond_crit = (ucr is not None and r >= ucr) or (lcr is not None and r <= lcr)
            within = (uc is None or r < uc) and (lc is None or r > lc)
            if beyond_crit and h != "Critical":
                fail(f"{sid}: reading {r} beyond a critical limit but Health {h}")
            elif within and not beyond_crit and h == "Critical":
                fail(f"{sid}: reading {r} within the caution limits but Health Critical")
    run_each(each_switch_bmc, checks, f)


# ------------------------------------------------------------------ 7.5.13 ServiceConditions (on the switch BMC)
SC = "/redfish/v1/ServiceConditions"


def _conditions(rf, fail):
    s = rf.try_get(SC)
    if s is None:
        fail("no /redfish/v1/ServiceConditions")
        return None, []
    c = s.get("Conditions")
    if not isinstance(c, list):
        fail("ServiceConditions: Conditions is not a list")
        return s, []
    return s, c


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.13-1")
def test_service_conditions_root(each_switch_bmc, checks, inventory, record_property):
    def f(rf, d, fail):
        s, c = _conditions(rf, fail)
        if s is not None:
            odata_type(s, "#ServiceConditions.v1_0_1.ServiceConditions", fail, _strict(inventory), "ServiceConditions")
        return len(c)
    record_property("active_conditions", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.13-2")
def test_condition_structure(each_switch_bmc, checks):
    def f(rf, d, fail):
        for c in _conditions(rf, fail)[1][:5]:
            need(c, ("MessageId", "Message", "Severity", "Timestamp", "OriginOfCondition"), fail, f"condition {c.get('MessageId')}")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.13-3")
def test_condition_severities(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        out = {}
        for c in _conditions(rf, fail)[1]:
            out[c.get("Severity")] = out.get(c.get("Severity"), 0) + 1
            if c.get("Severity") not in ("Warning", "Critical"):
                fail(f"condition {c.get('MessageId')}: Severity {c.get('Severity')!r} (only Warning / Critical belong here)")
        return out
    record_property("severities", run_each(each_switch_bmc, checks, f))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.13-4")
def test_condition_origins(each_switch_bmc, checks):
    def f(rf, d, fail):
        for c in _conditions(rf, fail)[1]:
            o = str((c.get("OriginOfCondition") or {}).get("@odata.id") or "")
            if not o.startswith("/redfish/v1/"):
                fail(f"condition {c.get('MessageId')}: OriginOfCondition {o!r}")
            elif NS_PREFIX.search(o):
                fail(f"condition {c.get('MessageId')}: OriginOfCondition {o} carries a namespace prefix")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.13-5")
def test_condition_timestamps(each_switch_bmc, checks):
    def f(rf, d, fail):
        now = datetime.now(timezone.utc)
        for c in _conditions(rf, fail)[1]:
            t = iso(c.get("Timestamp"))
            if t is None:
                fail(f"condition {c.get('MessageId')}: Timestamp {c.get('Timestamp')!r} is not ISO 8601")
            elif t > now:
                fail(f"condition {c.get('MessageId')}: Timestamp {t.isoformat()} is in the future")
    run_each(each_switch_bmc, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.13-7")
def test_condition_client_workflow(each_switch_bmc, checks, record_property):
    def f(rf, d, fail):
        conds = _conditions(rf, fail)[1]
        crit = [c for c in conds if c.get("Severity") == "Critical"]
        if crit:
            o = (crit[0].get("OriginOfCondition") or {}).get("@odata.id")
            res = rf.try_get(o) if o else None
            if res is None:
                fail(f"OriginOfCondition {o} of a Critical condition is not accessible")
            elif B._health(res) not in ("Warning", "Critical"):
                fail(f"{o}: Status.Health {B._health(res) or '-'} although it has a Critical condition")
        return {"critical": len(crit), "warning": sum(c.get("Severity") == "Warning" for c in conds)}
    record_property("conditions", run_each(each_switch_bmc, checks, f))
