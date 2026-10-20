"""helios.bmc + tests/bmc (TC-BMC-001..006) against a fake Redfish BMC (read-only GETs, basic auth)."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from helios import bmc as B

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc)


def tree(power="On", fw="2.14.0", temp=45.0, crit_log=False):
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


class Fake:
    def __init__(self, data, user="admin", pw="Bmc-Pw-1"):
        self.data, self.auth, self.paths = data, "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(), []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.paths.append((self.command, self.path))
                if self.headers.get("Authorization") != fake.auth:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = fake.data.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_snapshot_and_judging():
    f = Fake(tree(power="Off", temp=97.0, crit_log=True))
    try:
        s = B.snapshot(B.Redfish(f.url, "admin", "Bmc-Pw-1"))
        assert all(m == "GET" for m, _ in f.paths)                              # read-only
        assert s["root"]["product"] == "OpenBMC" and s["managers"][0]["firmware"] == "2.14.0"
        assert B.bad_power(s) == ["system system: power 'Off'"]
        crit, warn = B.bad_sensors(s)
        assert len(crit) == 1 and "CPU = 97.0" in crit[0] and warn == []
        crit, nwarn = B.recent_critical(s, 24)
        assert [e["message"] for e in crit] == ["PSU1 lost input"] and nwarn == 1   # the 3-day-old one is outside
        with pytest.raises(B.BmcError, match="login refused"):
            B.Redfish(f.url, "admin", "wrong").get("/redfish/v1/")
    finally:
        f.close()
    assert B.firmware_groups({"c1": {"managers": [{"firmware": "1"}]}, "c2": {"managers": [{"firmware": "2"}]},
                              "c3": {"managers": [{"firmware": "1"}]}}) == {"1": ["c1", "c3"], "2": ["c2"]}


def test_sensor_schema_thresholds():
    s = {"sensors": [B._sensor("x/inlet", 41, "Cel", {"Thresholds": {"UpperCaution": {"Reading": 40},
                                                                      "UpperCritical": {"Reading": 50}}})]}
    crit, warn = B.bad_sensors(s)
    assert crit == [] and len(warn) == 1


def test_bmc_cases_end_to_end(tmp_path):
    """tests/bmc on an inventory whose BMCs are fakes: c1 healthy, c2 powered off with a newer firmware, st1 without
    a BMC user (not checked), c3 with its own password (HELIOS_BMC_PASSWORD_C3)."""
    good, off, own = Fake(tree()), Fake(tree(power="Off", fw="2.15.0")), Fake(tree(), pw="C3-own-pw")
    inv = tmp_path / "inv.yaml"
    inv.write_text(yaml.safe_dump({"rack": "bmc-fake", "transport": {"kind": "ssh"},
                                   "switches": [{"name": "st1", "host": "10.0.0.1", "bmc": "10.0.0.2"}],
                                   "computes": [{"name": "c1", "host": "10.0.0.11", "bmc": good.url, "bmc_user": "admin"},
                                                {"name": "c2", "host": "10.0.0.12", "bmc": off.url, "bmc_user": "admin"},
                                                {"name": "c3", "host": "10.0.0.13", "bmc": own.url, "bmc_user": "admin"}]}))
    env = {**os.environ, "HELIOS_BMC_PASSWORD": "Bmc-Pw-1", "HELIOS_BMC_PASSWORD_C3": "C3-own-pw"}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/bmc", "--inventory", str(inv), "-q", "-p",
                            "no:cacheprovider", "--results-dir", str(tmp_path / "res"), "-rA"],
                           cwd=ROOT, capture_output=True, text=True, timeout=300, env=env)
    finally:
        for f in (good, off, own):
            f.close()
    out = r.stdout + r.stderr
    assert "PASSED tests/bmc/test_bmc.py::test_bmc_reachable_and_login_works" in out, out[-3000:]
    assert "FAILED tests/bmc/test_bmc.py::test_bmc_power_state_on" in out and "c2: system system: power 'Off'" in out
    assert "PASSED tests/bmc/test_bmc.py::test_bmc_sensors_within_limits" in out
    assert "FAILED tests/bmc/test_bmc.py::test_bmc_firmware_consistent" in out and "c2: BMC firmware 2.15.0" in out
    assert "Bmc-Pw-1" not in out and "C3-own-pw" not in out
