"""TC-BLT-* (Blanton switch BMC, Juniper TPI-151974) against a fake Blanton BMC built from the plan's expected
values: a healthy one passes, planted faults fail with their message, nothing but GETs is sent."""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc)
M, C, TS = "/redfish/v1/Managers/bmc", "/redfish/v1/Chassis/QFX5250_640E_L_Baseboard", "/redfish/v1/TelemetryService"


def ref(p):
    return {"@odata.id": p}


def coll(t, *ms):
    return {"@odata.type": f"#{t}.{t}", "Members": [ref(m) for m in ms], "Members@odata.count": len(ms)}


def blanton(**over):
    old, soon = (NOW - timedelta(days=200)).isoformat(), (NOW + timedelta(days=300)).isoformat()
    t = {
        "/redfish/v1/": {"@odata.id": "/redfish/v1", "RedfishVersion": "1.17.0", "Chassis": ref("/redfish/v1/Chassis"),
                         "Systems": ref("/redfish/v1/Systems"), "Managers": ref("/redfish/v1/Managers")},
        "/redfish/v1/odata": {"@odata.context": "/redfish/v1/$metadata", "value": [{"name": "Service", "url": "/redfish/v1/"}]},
        "/redfish/v1/$metadata": ("<?xml version='1.0'?><edmx:Edmx Version='4.0'><edmx:DataServices>"
                                  "<Schema Namespace='Service'/></edmx:DataServices></edmx:Edmx>", "application/xml"),
        "/redfish/v1/Chassis": coll("ChassisCollection", C),
        C: {"Id": "QFX5250_640E_L_Baseboard", "Name": "QFX5250_640E_L_Baseboard", "ChassisType": "RackMount",
            "Manufacturer": "QFX", "Model": "QFX5250-640E-L", "Status": {"State": "Enabled"},
            "Sensors": ref(C + "/Sensors"), "Thermal": ref(C + "/Thermal"), "ThermalSubsystem": ref(C + "/ThermalSubsystem")},
        C + "/Sensors": coll("SensorCollection", C + "/Sensors/LeakDetector_0", C + "/Sensors/Temp_CPU0", C + "/Sensors/Fan0"),
        C + "/Sensors/LeakDetector_0": {"Id": "LeakDetector_0", "Reading": 0, "ReadingType": "Percent", "Status": {"Health": "OK"}},
        C + "/Sensors/Temp_CPU0": {"Id": "Temp_CPU0", "Reading": 45, "ReadingType": "Temperature", "Status": {"Health": "OK"},
                                   "Thresholds": {"UpperCaution": {"Reading": 85}, "UpperCritical": {"Reading": 95}}},
        C + "/Sensors/Fan0": {"Id": "Fan0", "Reading": 9000, "ReadingType": "Rotational", "Status": {"Health": "OK"}},
        C + "/Thermal": {"Temperatures": [{"Name": "CPU0", "ReadingCelsius": 45, "Status": {"Health": "OK"}}], "Fans": [{"Name": "Fan0"}]},
        C + "/ThermalSubsystem": {"LeakDetection": ref(C + "/ThermalSubsystem/LeakDetection"), "Fans": ref(C + "/ThermalSubsystem/Fans")},
        C + "/ThermalSubsystem/LeakDetection": {"LeakDetectors": ref(C + "/ThermalSubsystem/LeakDetection/LeakDetectors")},
        C + "/ThermalSubsystem/LeakDetection/LeakDetectors": coll("LeakDetectorCollection", C + "/ThermalSubsystem/LeakDetection/LeakDetectors/LeakDetector_0"),
        C + "/ThermalSubsystem/LeakDetection/LeakDetectors/LeakDetector_0": {"Enabled": True, "DetectorState": "OK", "Status": {"Health": "OK"}},
        "/redfish/v1/Systems": coll("ComputerSystemCollection", "/redfish/v1/Systems/system"),
        "/redfish/v1/Systems/system": {"Id": "system", "PowerState": "On", "Status": {"Health": "OK"}},
        "/redfish/v1/Managers": coll("ManagerCollection", M),
        M: {"Id": "bmc", "FirmwareVersion": "2.14.0-blanton", "Status": {"State": "Enabled", "Health": "OK"},
            "DateTime": NOW.isoformat(), "ManagerType": "BMC", "Actions": {"#Manager.Reset": {"target": M + "/Actions/Manager.Reset"}}},
        M + "/NetworkProtocol": {"HTTPS": {"ProtocolEnabled": True, "Port": 443}, "SSH": {"ProtocolEnabled": True, "Port": 22}},
        M + "/EthernetInterfaces": coll("EthernetInterfaceCollection", M + "/EthernetInterfaces/eth0"),
        M + "/EthernetInterfaces/eth0": {"MACAddress": "aa:bb:cc:00:11:22", "IPv4Addresses": [{"Address": "127.0.0.1"}]},
        M + "/LogServices": coll("LogServiceCollection", M + "/LogServices/Log", M + "/LogServices/Journal"),
        M + "/LogServices/Log": {"MaxNumberOfRecords": 10000, "OverWritePolicy": "WrapsWhenFull", "Entries": ref(M + "/LogServices/Log/Entries"),
                                 "Actions": {"#LogService.ClearLog": {"target": M + "/LogServices/Log/Actions/LogService.ClearLog"}}},
        M + "/LogServices/Log/Entries": {"Members": [{"Id": "1", "Message": "boot", "Created": old, "Severity": "OK"}]},
        M + "/LogServices/Journal": {"Entries": ref(M + "/LogServices/Journal/Entries")},
        M + "/LogServices/Journal/Entries": {"Members": [{"Id": "j1", "Message": "bmcweb started", "Created": old, "Severity": "OK"}]},
        M + "/NetworkProtocol/HTTPS/Certificates": coll("CertificateCollection", M + "/NetworkProtocol/HTTPS/Certificates/1"),
        M + "/NetworkProtocol/HTTPS/Certificates/1": {"@odata.type": "#Certificate.v1_8_0.Certificate", "Subject": {"CommonName": "bmc"},
                                                      "Issuer": {"CommonName": "bmc"}, "ValidNotBefore": old, "ValidNotAfter": soon},
        "/redfish/v1/AccountService/Accounts": coll("ManagerAccountCollection", "/redfish/v1/AccountService/Accounts/root"),
        "/redfish/v1/AccountService/Accounts/root": {"UserName": "root"},
        "/redfish/v1/AccountService/Roles": coll("RoleCollection", *(f"/redfish/v1/AccountService/Roles/{r}" for r in ("Administrator", "Operator", "ReadOnly"))),
        "/redfish/v1/SessionService": {"Sessions": ref("/redfish/v1/SessionService/Sessions")},
        "/redfish/v1/SessionService/Sessions": coll("SessionCollection"),
        "/redfish/v1/UpdateService": {"@odata.type": "#UpdateService.v1_17_0.UpdateService", "ServiceEnabled": True,
                                      "FirmwareInventory": ref("/redfish/v1/UpdateService/FirmwareInventory"),
                                      "Actions": {"#UpdateService.SimpleUpdate": {}, "#UpdateService.StartUpdate": {}}},
        "/redfish/v1/UpdateService/FirmwareInventory": coll("SoftwareInventoryCollection", *(
            f"/redfish/v1/UpdateService/FirmwareInventory/{i}" for i in ("BMC_Firmware", "NOS", "CPLD", "BIOS"))),
        **{f"/redfish/v1/UpdateService/FirmwareInventory/{i}": {"@odata.type": "#SoftwareInventory.v1_13_0.SoftwareInventory",
                                                                "Id": i, "Name": i, "Version": "2.14.0-blanton" if i == "BMC_Firmware" else "1.0",
                                                                "Updateable": True} for i in ("BMC_Firmware", "NOS", "CPLD", "BIOS")},
        "/redfish/v1/CertificateService": {"CertificateLocations": ref("/redfish/v1/CertificateService/CertificateLocations"), "Actions": {
            "#CertificateService.ReplaceCertificate": {"target": "/redfish/v1/CertificateService/Actions/CertificateService.ReplaceCertificate"},
            "#CertificateService.GenerateCSR": {"target": "/redfish/v1/CertificateService/Actions/CertificateService.GenerateCSR"}}},
        "/redfish/v1/TaskService": {"@odata.type": "#TaskService.v1_2_1.TaskService", "ServiceEnabled": True, "Tasks": ref("/redfish/v1/TaskService/Tasks")},
        "/redfish/v1/TaskService/Tasks": coll("TaskCollection", "/redfish/v1/TaskService/Tasks/0"),
        "/redfish/v1/TaskService/Tasks/0": {"@odata.id": "/redfish/v1/TaskService/Tasks/0", "@odata.type": "#Task.v1_7_4.Task",
                                            "TaskState": "Completed", "TaskStatus": "OK", "StartTime": old, "EndTime": old,
                                            "Messages": [{"MessageId": "TaskEvent.1.0.TaskCompletedOK", "Severity": "OK"}]},
        TS: {"@odata.type": "#TelemetryService.v1_4_1.TelemetryService", "ServiceEnabled": True,
             "Actions": {"#TelemetryService.SubmitTestMetricReport": {"target": TS + "/Actions/TelemetryService.SubmitTestMetricReport"}},
             "MetricDefinitions": ref(TS + "/MetricDefinitions"), "MetricReportDefinitions": ref(TS + "/MetricReportDefinitions"),
             "MetricReports": ref(TS + "/MetricReports")},
        TS + "/MetricDefinitions": coll("MetricDefinitionCollection", TS + "/MetricDefinitions/Temperature"),
        TS + "/MetricDefinitions/Temperature": {"@odata.id": TS + "/MetricDefinitions/Temperature",
                                                "@odata.type": "#MetricDefinition.v1_3_5.MetricDefinition", "Id": "Temperature",
                                                "Name": "Temperature", "MetricType": "Gauge", "Units": "Cel", "MetricDataType": "Decimal",
                                                "PhysicalContext": "CPU", "MetricProperties": [C + "/Sensors/Temp_CPU0#/Reading"]},
        TS + "/MetricReportDefinitions": coll("MetricReportDefinitionCollection"),
        TS + "/MetricReports": coll("MetricReportCollection", TS + "/MetricReports/r1"),
        TS + "/MetricReports/r1": {"@odata.type": "#MetricReport.v1_5_2.MetricReport", "Timestamp": old, "MetricValues": [
            {"MetricId": "Temperature", "MetricValue": "45", "Timestamp": old, "MetricProperty": C + "/Sensors/Temp_CPU0#/Reading"}]},
        "/redfish/v1/ServiceConditions": {"@odata.type": "#ServiceConditions.v1_0_1.ServiceConditions", "Conditions": [
            {"MessageId": "Base.1.0.Warning", "Message": "fan slow", "Severity": "Warning", "Timestamp": old,
             "OriginOfCondition": ref(C + "/Sensors/Fan0")}]},
    }
    for k, v in over.items():
        path, _, key = k.partition("::")
        if v is None and not key:
            t.pop(path, None)
        elif key:
            t[path] = {**t[path], key: v}
        else:
            t[path] = v
    return t


class FakeBmc:
    def __init__(self, tree, user="root", pw="Blanton-Pw-1"):
        self.tree, self.auth, self.methods = tree, "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(), []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.methods.append(self.command)
                if self.headers.get("Authorization") != fake.auth:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = fake.tree.get(self.path.split("?")[0])
                ctype = "application/json"
                if isinstance(body, tuple):
                    body, ctype = body
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                if body is not None:
                    self.wfile.write((body if isinstance(body, str) else json.dumps(body)).encode())

            do_POST = do_PATCH = do_DELETE = do_PUT = do_GET           # recorded, so the test can assert GET-only
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def run_cases(tmp_path, tree, extra_th=None):
    f = FakeBmc(tree)
    inv = tmp_path / "inv.yaml"
    inv.write_text(yaml.safe_dump({"rack": "mr3", "transport": {"kind": "ssh"},
                                   "switches": [{"name": "st1", "host": "10.0.0.1", "bmc": f.url, "bmc_user": "root"}],
                                   "computes": [], "thresholds": {"blanton_bmc": {"heartbeat_count": 2, "heartbeat_interval_s": 0, **(extra_th or {})}}}))
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/bmc/test_blanton_redfish.py", "--inventory", str(inv),
                            "-q", "-rA", "-p", "no:cacheprovider", "--results-dir", str(tmp_path / "res")],
                           cwd=ROOT, capture_output=True, text=True, timeout=600,
                           env={**os.environ, "HELIOS_BMC_PASSWORD_ST1": "Blanton-Pw-1"})
    finally:
        f.close()
    return r.stdout + r.stderr, f.methods


def outcome(out, tc_test):
    m = re.search(rf"^(PASSED|FAILED|SKIPPED|ERROR)[^\n]*{re.escape(tc_test)}", out, re.M)
    return m.group(1) if m else None


def test_a_healthy_blanton_bmc_passes_and_only_gets(tmp_path):
    out, methods = run_cases(tmp_path, blanton())
    assert set(methods) == {"GET"}                                        # read-only
    failed = re.findall(r"^FAILED (\S+)", out, re.M)
    # plain http to the fake: only the TLS part of the auth case can fail
    assert failed == ["tests/bmc/test_blanton_redfish.py::test_vs_auth_and_tls"], out[-4000:]
    assert "not HTTPS" in out
    assert re.search(r"(\d+) passed", out) and int(re.search(r"(\d+) passed", out).group(1)) >= 74
    assert "Blanton-Pw-1" not in out


def test_planted_faults_fail_with_the_plans_expectation(tmp_path):
    expired = (NOW - timedelta(days=2)).isoformat()
    tree = blanton(**{
        M + "/LogServices/Log::MaxNumberOfRecords": 5000,
        C + "/Sensors/LeakDetector_0::Reading": 1,
        M + "/NetworkProtocol/HTTPS/Certificates/1::ValidNotAfter": expired,
        "/redfish/v1/TaskService::@odata.type": "#TaskService.v1_2_0.TaskService",
        "/redfish/v1/UpdateService/FirmwareInventory/BMC_Firmware": None,
        "/redfish/v1/ServiceConditions": {"@odata.type": "#ServiceConditions.v1_0_1.ServiceConditions",
                                          "Conditions": [{"MessageId": "x", "Message": "m", "Severity": "OK", "Timestamp": expired,
                                                          "OriginOfCondition": ref("/redfish/v1/Chassis/compute01_Sled")}]},
    })
    tree["/redfish/v1/UpdateService/FirmwareInventory"] = coll("SoftwareInventoryCollection", *(
        f"/redfish/v1/UpdateService/FirmwareInventory/{i}" for i in ("NOS", "CPLD", "BIOS")))
    out, _ = run_cases(tmp_path, tree)
    for test, msg in (("test_log_max_records", "MaxNumberOfRecords 5000"),
                      ("test_leak_detector_sensor", "LeakDetector_0 Reading 1"),
                      ("test_certificate_validity", "certificate EXPIRED"),
                      ("test_task_service", "the plan expects '#TaskService.v1_2_1.TaskService'"),
                      ("test_firmware_component_ids", "no BMC_Firmware entry"),
                      ("test_condition_severities", "Severity 'OK'"),
                      ("test_condition_origins", "namespace prefix")):
        assert outcome(out, test) == "FAILED", (test, out[-3000:])
        assert msg in out, msg
    # strict_types: false compares the type name only
    out, _ = run_cases(tmp_path, blanton(**{"/redfish/v1/TaskService::@odata.type": "#TaskService.v1_2_0.TaskService"}),
                       {"strict_types": False})
    assert outcome(out, "test_task_service") == "PASSED"


def test_no_task_skips_instead_of_resetting_the_bmc(tmp_path):
    tree = blanton()
    tree["/redfish/v1/TaskService/Tasks"] = coll("TaskCollection")
    out, methods = run_cases(tmp_path, tree)
    assert re.search(r"^SKIPPED \[3\] [^\n]*Manager\.Reset \(disruptive\) - not done", out, re.M), out[-2000:]   # 7.2.14-3/-6/-8
    assert set(methods) == {"GET"}


def test_cli_cases_on_a_fake_switch():
    """bmc-manager.sh output parsing, with the plan's example outputs."""
    sys.path.insert(0, str(ROOT / "tests" / "bmc"))
    import test_blanton_cli as T
    from helios.rack import Checks
    from helios.transport import CmdResult
    outputs = {
        "bmc-manager.sh flash-info": "Boot Source: PRIMARY (CE0)\nWDT2 Register: 0x00000000\n" + "".join(f"mtd{n} part{n} 512K\n" for n in range(12)),
        "bmc-manager.sh ssh-access status": "Dropbear socket status:\n Active: active (listening)\n dropbear.socket\n"
                                           "LISTEN 0 128 192.168.200.200:22 0.0.0.0:*\nSSH Access Mode: BLOCKED\n",
        "bmc-manager.sh config-list": "".join(f"{p}: {'present' if i < 6 else 'not found'}\n" for i, p in enumerate(
            ["/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow", "/etc/ssh", "/etc/ipmi", "/etc/ssl/certs/https",
             "/etc/systemd/network", "/etc/nslcd.conf", "/home/root/.ssh", "/home/root/bmcweb_persistent_data.json"])),
    }

    class Sw:
        name = "st1"

        def run(self, cmd, sudo=False, timeout=60):
            if cmd.startswith("command -v"):
                return CmdResult("st1", cmd, 0, "/usr/local/bin/bmc-manager.sh\n", "")
            return CmdResult("st1", cmd, 0, outputs.get(cmd, ""), "")
    for fn in (T.test_default_boot_flash, T.test_flash_partitions, T.test_ssh_access_status, T.test_config_list):
        fn([Sw()], Checks())                                           # no failure raised
    outputs["bmc-manager.sh flash-info"] = "Boot Source: ALTERNATE (CE1)\nWDT2 Register: 0x1\n"
    import pytest
    with pytest.raises(pytest.fail.Exception, match=r"the plan expects PRIMARY \(CE0\)"):
        T.test_default_boot_flash([Sw()], Checks())
