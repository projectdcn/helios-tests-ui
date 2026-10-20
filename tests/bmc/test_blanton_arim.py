"""TC-BLT-7.5.x - ARIM rack-level Redfish aggregator, READ-ONLY cases of Juniper TPI-151974 v1.2 section 7.5. Added
20 Oct 2026. Run only with an ARIM in the inventory (arim: {url, user}, $HELIOS_ARIM_PASSWORD); else they skip.

GETs only: registering / deleting aggregation sources, resets (single, batch, rack-wide), subscriptions, metric
report definitions, SubmitTestEvent / SubmitTestMetricReport and SimpleUpdate are NOT here. Downstream names follow
the plan's examples (compute01_System, pshelf01_PowerShelf, scap01_SuperCapShelf) only where the cases need one; the
cases otherwise discover the members.
"""
from __future__ import annotations

import pytest

ROOT_LINKS = ("Systems", "Chassis", "Managers", "Fabrics", "PowerEquipment", "ComponentIntegrity", "AggregationService",
              "SessionService", "AccountService", "UpdateService", "CertificateService", "EventService",
              "TelemetryService", "Tasks", "ServiceConditions")


def _ids(rf, path):
    return [m.rstrip("/").rsplit("/", 1)[-1] for m in rf.members(path)]


def _aggregated(ids):
    return [i for i in ids if "_" in i]


def _odata_ids(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "@odata.id" and isinstance(v, str):
                out.append(v)
            else:
                _odata_ids(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _odata_ids(x, out)
    return out


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.1-1")
def test_aggregator_service_root(arim, checks):
    r = arim.get("/redfish/v1/")
    for k in ROOT_LINKS:
        checks.check(r.get(k), f"service root has no {k} link")
    for k in ("UUID", "RedfishVersion", "ProtocolFeaturesSupported"):
        checks.check(r.get(k), f"service root has no {k}")
    mps = ((r.get("Links") or {}).get("ManagerProvidingService") or {}).get("@odata.id")
    checks.check(mps == "/redfish/v1/Managers/RackManager", f"ManagerProvidingService {mps!r}, expected /redfish/v1/Managers/RackManager")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.1-2")
def test_redfish_version_object(arim, checks):
    v = arim.get("/redfish")
    checks.check(str(v.get("v1", "")).rstrip("/") == "/redfish/v1", f"/redfish -> {v}, expected {{'v1': '/redfish/v1/'}}")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.1-3")
def test_json_schemas(arim, checks):
    ms = arim.members("/redfish/v1/JsonSchemas")
    checks.check(ms, "JsonSchemas is empty")
    if ms:
        arim.get(ms[0])
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.1-4")
def test_registries(arim, checks):
    ids = _ids(arim, "/redfish/v1/Registries")
    checks.check(any(i.startswith("Base") for i in ids), f"no Base registry (registries: {ids})")
    for m in arim.members("/redfish/v1/Registries")[:3]:
        arim.get(m)
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.2-4")
def test_connection_methods(arim, checks, record_property):
    types = [arim.get(m).get("ConnectionMethodType") for m in arim.members("/redfish/v1/AggregationService/ConnectionMethods")]
    record_property("connection_methods", types)
    checks.check("Redfish" in types, f"no Redfish connection method ({types})")
    checks.assert_all()


def _a_system(arim):
    agg = _aggregated(_ids(arim, "/redfish/v1/Systems"))
    if not agg:
        pytest.skip("no aggregated (downstream) system on the ARIM")
    return agg[0]


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.3-1")
def test_transparent_proxy(arim, checks):
    """Every @odata.id in a proxied system resource is an aggregated URI that resolves (no dangling 404)."""
    sid = _a_system(arim)
    s = arim.get(f"/redfish/v1/Systems/{sid}")
    for ref in sorted(set(_odata_ids(s)))[:30]:
        st, _, _ = arim.request("GET", ref.split("#")[0])
        checks.check(st == 200, f"{ref} (in Systems/{sid}) -> HTTP {st}")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.3-2")
def test_uri_rewriting(arim, checks):
    """@odata.id and action targets rewritten into the aggregated namespace; @odata.type untouched."""
    sid = _a_system(arim)
    prefix = sid.split("_")[0] + "_"
    s = arim.get(f"/redfish/v1/Systems/{sid}")
    for ref in _odata_ids(s):
        if ref.startswith("/redfish/v1/Systems/"):
            checks.check(prefix in ref, f"{ref} not rewritten with the source prefix {prefix}")
    for a, v in (s.get("Actions") or {}).items():
        t = str((v or {}).get("target") or "")
        checks.check(prefix in t, f"action {a} target {t!r} not rewritten")
    checks.check(prefix not in str(s.get("@odata.type")), f"@odata.type {s.get('@odata.type')!r} was rewritten")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.4-1")
def test_systems_membership(arim, checks, record_property):
    ids = _ids(arim, "/redfish/v1/Systems")
    record_property("systems", ids)
    checks.check(ids, "Systems is empty")
    for i in ids:
        checks.check(not i.lower().startswith(("pshelf", "scap")), f"{i}: power shelves / super caps have no ComputerSystem")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.4-2")
def test_chassis_membership(arim, checks, record_property):
    ids = _ids(arim, "/redfish/v1/Chassis")
    record_property("chassis", ids)
    checks.check(len(_aggregated(ids)) <= 40, f"{len(_aggregated(ids))} aggregated chassis (at most 40)")
    rack = arim.try_get("/redfish/v1/Chassis/Rack")
    if rack is None:
        checks.fail("no Chassis/Rack")
    else:
        contained = {x.get("@odata.id", "").rsplit("/", 1)[-1] for x in (rack.get("Links") or {}).get("Contains") or []}
        missing = [i for i in _aggregated(ids) if i not in contained]
        checks.check(not missing, f"Rack Links.Contains misses {missing[:10]}")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.4-3")
def test_managers_membership(arim, checks, record_property):
    ids = _ids(arim, "/redfish/v1/Managers")
    record_property("managers", ids)
    checks.check("RackManager" in ids, f"no local RackManager (managers: {ids})")
    checks.check(len(_aggregated(ids)) <= 40, f"{len(_aggregated(ids))} aggregated managers (at most 40)")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.4-4")
def test_containment_hierarchy(arim, checks):
    rack = arim.try_get("/redfish/v1/Chassis/Rack")
    if rack is None:
        pytest.fail("no Chassis/Rack", pytrace=False)
    for x in ((rack.get("Links") or {}).get("Contains") or [])[:40]:
        c = arim.get(x["@odata.id"])
        up = [y.get("@odata.id") for y in (c.get("Links") or {}).get("ContainedBy") or []] if isinstance(
            (c.get("Links") or {}).get("ContainedBy"), list) else [((c.get("Links") or {}).get("ContainedBy") or {}).get("@odata.id")]
        checks.check("/redfish/v1/Chassis/Rack" in up, f"{x['@odata.id']}: Links.ContainedBy {up} does not point back to the Rack")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.5-1")
def test_component_integrity(arim, checks):
    ms = arim.members("/redfish/v1/ComponentIntegrity")
    checks.check(ms, "ComponentIntegrity is empty")
    for m in ms[:3]:
        c = arim.get(m)
        checks.check(c.get("@odata.id") == m, f"{m}: @odata.id {c.get('@odata.id')!r} not the aggregated URI")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.7-1")
def test_power_equipment(arim, checks, record_property):
    pe = arim.get("/redfish/v1/PowerEquipment")
    ref = (pe.get("PowerShelves") or {}).get("@odata.id")
    checks.check(ref, "PowerEquipment has no PowerShelves")
    if ref:
        n = len(arim.members(ref))
        record_property("power_shelves", n)
        checks.check(n <= 16, f"{n} power shelves / super caps (at most 10 + 6)")
    checks.assert_all()


def _chassis_like(arim, word):
    hit = [i for i in _ids(arim, "/redfish/v1/Chassis") if word.lower() in i.lower()]
    if not hit:
        pytest.skip(f"no {word} chassis on the ARIM")
    return hit[0]


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.7-2")
def test_power_shelf_sub_resources(arim, checks):
    cid = _chassis_like(arim, "PowerShelf")
    for sub in ("EnvironmentMetrics", "PowerSubsystem/PowerSupplies", "Sensors"):
        st, _, _ = arim.request("GET", f"/redfish/v1/Chassis/{cid}/{sub}")
        checks.check(st == 200, f"Chassis/{cid}/{sub} -> HTTP {st}")
    psus = arim.members(f"/redfish/v1/Chassis/{cid}/PowerSubsystem/PowerSupplies")
    if psus:
        checks.check("#PowerSupply.Reset" in (arim.get(psus[0]).get("Actions") or {}), f"{psus[0]}: no PowerSupply.Reset action")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.7-3")
def test_super_cap_batteries(arim, checks):
    cid = _chassis_like(arim, "SuperCap")
    ms = arim.members(f"/redfish/v1/Chassis/{cid}/PowerSubsystem/Batteries")
    checks.check(ms, f"Chassis/{cid} has no batteries")
    for m in ms[:3]:
        arim.get(m)
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.8-1")
def test_arim_event_service(arim, checks):
    """EventService with SSE; SubmitTestEvent advertised (not invoked here)."""
    e = arim.get("/redfish/v1/EventService")
    checks.check(e.get("ServerSentEventUri"), "EventService has no ServerSentEventUri (SSE)")
    checks.check("#EventService.SubmitTestEvent" in (e.get("Actions") or {}), "no SubmitTestEvent action")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.9-1")
def test_deduplicated_metric_definitions(arim, checks):
    seen = {}
    for m in arim.members("/redfish/v1/TelemetryService/MetricDefinitions"):
        md = arim.get(m)
        key = (md.get("MetricType"), md.get("Units"), md.get("MetricDataType"), md.get("PhysicalContext"), md.get("Name"))
        checks.check(key not in seen, f"{m} duplicates {seen.get(key)} (one definition per metric type, not per BMC)")
        seen.setdefault(key, m)
        checks.check(isinstance(md.get("MetricProperties"), list) and md["MetricProperties"], f"{m}: no MetricProperties")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.10-1")
def test_arim_task_service(arim, checks):
    t = arim.get("/redfish/v1/TaskService")
    checks.check(t.get("ServiceEnabled") is True, "TaskService not enabled")
    arim.get("/redfish/v1/TaskService/Tasks")
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.5.11-1")
def test_aggregated_firmware_inventory(arim, checks, record_property):
    ids = _ids(arim, "/redfish/v1/UpdateService/FirmwareInventory")
    record_property("firmware_ids", ids)
    checks.check(_aggregated(ids), "no downstream firmware entry ({sourceId}_ namespace) in the unified catalog")
    checks.assert_all()
