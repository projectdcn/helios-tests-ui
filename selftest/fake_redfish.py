"""Redfish BMCs of the simulated rack (selftest/vrack.py): one read-only Redfish service per switch and compute tray.

The switch BMC is a healthy Juniper Blanton BMC (the tree the TC-BLT-* cases are checked against, unit/test_blanton_bmc),
the compute BMC a healthy OpenBMC (TC-BMC-*, unit/test_bmc_redfish). Both are rebuilt per request, so clocks and
timestamps are current. GET only: anything else answers 405, like a read-only test account would see it refused.
Basic auth with the simulator's BMC user / password (BMC_USER / BMC_PW - not real credentials).
"""
from __future__ import annotations

import base64
import json
import ssl
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BMC_USER = {"switch": "root", "compute": "admin"}
BMC_PW = "vrack-bmc-pw"                 # simulator only
M, C, TS = "/redfish/v1/Managers/bmc", "/redfish/v1/Chassis/QFX5250_640E_L_Baseboard", "/redfish/v1/TelemetryService"


def ref(p):
    return {"@odata.id": p}


def coll(t, *ms):
    return {"@odata.type": f"#{t}.{t}", "Members": [ref(m) for m in ms], "Members@odata.count": len(ms)}


def blanton(**over):
    NOW = datetime.now(timezone.utc)
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



def openbmc(power="On", fw="2.14.0", temp=45.0, crit_log=False):
    NOW = datetime.now(timezone.utc)
    old = (NOW - timedelta(days=3)).isoformat()
    new = (NOW - timedelta(hours=1)).isoformat()
    return {
        "/redfish/v1/": {"Product": "OpenBMC", "RedfishVersion": "1.17.0"},
        "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/system"}]},
        "/redfish/v1/Systems/system": {"Id": "system", "PowerState": power, "Status": {"Health": "OK"},
                                       "LogServices": {"@odata.id": "/redfish/v1/Systems/system/LogServices"}},
        "/redfish/v1/Systems/system/LogServices": {"Members": [{"@odata.id": "/redfish/v1/Systems/system/LogServices/EventLog"}]},
        "/redfish/v1/Systems/system/LogServices/EventLog": {"Entries": {"@odata.id": "/redfish/v1/Systems/system/LogServices/EventLog/Entries"}},
        "/redfish/v1/Systems/system/LogServices/EventLog/Entries": {"Members": [
            {"Id": "1", "Severity": "Critical", "Created": old, "Message": "old fan failure"},
            {"Id": "2", "Severity": "Warning", "Created": new, "Message": "inlet warm"},
            *([{"Id": "3", "Severity": "Critical", "Created": new, "Message": "PSU1 lost input"}] if crit_log else [])]},
        "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/bmc"}]},
        "/redfish/v1/Managers/bmc": {"Id": "bmc", "FirmwareVersion": fw, "Status": {"Health": "OK"}},
        "/redfish/v1/Managers/bmc/LogServices": {"Members": []},
        "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/chassis"}]},
        "/redfish/v1/Chassis/chassis": {"Id": "chassis", "Thermal": {"@odata.id": "/redfish/v1/Chassis/chassis/Thermal"}},
        "/redfish/v1/Chassis/chassis/Thermal": {"Temperatures": [
            {"Name": "CPU", "ReadingCelsius": temp, "UpperThresholdNonCritical": 80, "UpperThresholdCritical": 95,
             "Status": {"Health": "OK"}}], "Fans": [{"Name": "Fan0", "Reading": 9000, "ReadingUnits": "RPM",
                                                     "LowerThresholdCritical": 500, "Status": {"Health": "OK"}}]},
    }



class RedfishServer:
    """HTTPS Redfish service over one tree builder (self-signed certificate, like a BMC)."""

    def __init__(self, build, role: str, bind: tuple[str, int], power=lambda: "On"):
        auth = "Basic " + base64.b64encode(f"{BMC_USER[role]}:{BMC_PW}".encode()).decode()

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.headers.get("Authorization") != auth:
                    return self._send(401, b'{"error": "unauthorized"}')
                tree = build(power=power()) if role == "compute" else build()
                path = self.path.split("?")[0]
                body = tree.get(path) if path in tree else tree.get(path.rstrip("/") + "/") or tree.get(path.rstrip("/"))
                if body is None:
                    return self._send(404, b'{"error": "not found"}')
                ctype = "application/json"
                if isinstance(body, tuple):
                    body, ctype = body
                self._send(200, (body if isinstance(body, str) else json.dumps(body)).encode(), ctype)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._send(405, b'{"error": "read-only simulated BMC"}')

            do_PATCH = do_PUT = do_DELETE = do_POST

        self.httpd = ThreadingHTTPServer(bind, H)
        self.httpd.daemon_threads = True
        self._tmp = tempfile.TemporaryDirectory(prefix="vrack-bmc-")
        crt, key = Path(self._tmp.name) / "c.pem", Path(self._tmp.name) / "k.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=vrack-bmc", "-days", "30",
                        "-keyout", str(key), "-out", str(crt)], check=True, capture_output=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.url = f"https://{bind[0]}:{self.httpd.server_address[1]}"

    def start(self) -> "RedfishServer":
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()
