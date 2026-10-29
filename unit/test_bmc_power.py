"""helios.bmc power control (TC-RAS-reset-008A) against a fake Redfish BMC: ForceOff -> Off -> On, an On ignored
while the host powers down, a refused login never retried, DC power events after a mark."""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from helios import bmc as B

SYS = "/redfish/v1/Systems/system"


class FakeBmc:
    """PowerState goes Off ``off_after`` GETs after ForceOff; the first ``ignore_on`` On requests are accepted (204)
    but ignored, like the g02-g17 tray BMCs while the host is still powering down."""

    def __init__(self, pw="Bmc-Pw-1", off_after=2, ignore_on=0, events=None):
        self.state, self.off_in, self.ignore_on, self.posts = "On", None, ignore_on, []
        self.off_after, self.auth = off_after, "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()
        self.events = events if events is not None else [
            {"Created": "2026-10-27T22:54:00+00:00", "Message": "Host system DC power is off"},
            {"Created": "2026-10-27T22:54:01+00:00", "Message": "Host system DC power is on"},
            {"Created": "2026-10-28T01:00:00+00:00", "Message": "Fan 3 speed low"}]
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.headers.get("Authorization") != fake.auth:
                    return self._send(401, {})
                if self.path == "/redfish/v1/Systems":
                    return self._send(200, {"Members": [{"@odata.id": SYS}]})
                if self.path == SYS:
                    if fake.off_in is not None:
                        fake.off_in -= 1
                        if fake.off_in <= 0:
                            fake.state, fake.off_in = "Off", None
                    return self._send(200, {"Id": "system", "PowerState": fake.state})
                if self.path == SYS + "/LogServices/EventLog/Entries":
                    return self._send(200, {"Members": fake.events})
                return self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                fake.posts.append((self.path, body.get("ResetType")))
                if self.headers.get("Authorization") != fake.auth:
                    return self._send(401, {})
                if self.path != SYS + "/Actions/ComputerSystem.Reset":
                    return self._send(404, {})
                t = body.get("ResetType")
                if t == "ForceOff":
                    fake.off_in = fake.off_after
                elif t == "On":
                    if fake.ignore_on > 0:
                        fake.ignore_on -= 1
                    else:
                        fake.state = "On"
                        fake.events.append({"Created": "2026-10-29T16:04:04+00:00", "Message": "Host system DC power is off"})
                        fake.events.append({"Created": "2026-10-29T16:05:13+00:00", "Message": "Host system DC power is on"})
                else:
                    return self._send(400, {"error": "bad ResetType"})
                return self._send(204)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def client(self, pw="Bmc-Pw-1"):
        return B.Redfish(self.url, "admin", pw, timeout=5)

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def fast(monkeypatch):
    """No real sleeping: hold / poll intervals are irrelevant to the logic."""
    monkeypatch.setattr("time.sleep", lambda s: None)


def test_dc_cycle_off_then_on(fast):
    f = FakeBmc(off_after=3)
    try:
        rf = f.client()
        sysp = B.system_path(rf)
        res = B.dc_cycle(rf, sysp, off_timeout=10, off_hold=0, on_timeout=10, poll=0)
        assert res["before"] == "On" and res["on_attempts"] == 1
        assert [t for _, t in f.posts] == ["ForceOff", "On"]
        assert B.power_state(rf, sysp) == "On"
    finally:
        f.close()


def test_dc_cycle_resends_an_ignored_on(fast):
    f = FakeBmc(ignore_on=1)
    try:
        rf = f.client()
        res = B.dc_cycle(rf, SYS, off_timeout=10, off_hold=0, on_timeout=0.2, poll=0)
        assert res["on_attempts"] == 2
        assert [t for _, t in f.posts] == ["ForceOff", "On", "On"]
    finally:
        f.close()


def test_dc_cycle_gives_up_after_on_attempts(fast):
    f = FakeBmc(ignore_on=5)
    try:
        with pytest.raises(B.BmcError, match="not On after 3"):
            B.dc_cycle(f.client(), SYS, off_timeout=10, off_hold=0, on_timeout=0.1, on_attempts=3, poll=0)
    finally:
        f.close()


def test_dc_cycle_never_powers_on_when_off_is_not_reached(fast):
    f = FakeBmc(off_after=10**6)
    try:
        with pytest.raises(B.BmcError, match="still 'On'.*after ForceOff"):
            B.dc_cycle(f.client(), SYS, off_timeout=0.2, off_hold=0, on_timeout=1, poll=0)
        assert [t for _, t in f.posts] == ["ForceOff"]
    finally:
        f.close()


def test_refused_login_is_not_retried(fast):
    f = FakeBmc()
    try:
        with pytest.raises(B.BmcError, match="login refused"):
            B.reset(f.client(pw="wrong"), SYS, "ForceOff")
        assert len(f.posts) == 1
        with pytest.raises(B.BmcError, match="login refused"):
            B.system_path(f.client(pw="wrong"))
    finally:
        f.close()


def test_power_events_after_mark(fast):
    f = FakeBmc()
    try:
        rf = f.client()
        before = B.power_events(rf, SYS)
        assert [e["message"] for e in before] == ["Host system DC power is off", "Host system DC power is on"]
        mark = before[-1]["created"]
        B.dc_cycle(rf, SYS, off_timeout=10, off_hold=0, on_timeout=10, poll=0)
        new = B.power_events(rf, SYS, after=mark)
        assert [e["created"][:19] for e in new] == ["2026-10-29T16:04:04", "2026-10-29T16:05:13"]
    finally:
        f.close()


# ------------------------------------------------------------------ tests/ras/test_dc_cycle_all.py on the self-test rack
def _run_cycle_all(tmp_path, pw_c2="Bmc-Pw-1"):
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import yaml
    root = Path(__file__).resolve().parent.parent
    work = tmp_path / "selftest"
    shutil.copytree(root / "selftest", work)
    fakes = {"c1": FakeBmc(), "c2": FakeBmc(pw=pw_c2 if pw_c2 == "Bmc-Pw-1" else "other-pw", ignore_on=1)}
    inv = yaml.safe_load((work / "inventory.yaml").read_text())
    for c in inv["computes"]:
        if c["name"] in fakes:
            c["bmc"], c["bmc_user"] = fakes[c["name"]].url, "admin"
    inv["thresholds"].setdefault("ras", {})["dc_cycle_all"] = {
        "off_hold_s": 0, "poll_interval_s": 0, "on_timeout_s": 0.3, "boot_timeout_s": 1, "active_timeout_s": 1}
    (work / "inventory.yaml").write_text(yaml.safe_dump(inv, sort_keys=False))
    env = {**os.environ, "HELIOS_BMC_PASSWORD": "Bmc-Pw-1"}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/ras/test_dc_cycle_all.py", "--inventory",
                            str(work / "inventory.yaml"), "--allow-disruptive", "-q", "-p", "no:cacheprovider",
                            "--results-dir", str(tmp_path / "res"), "-rA"],
                           cwd=root, capture_output=True, text=True, timeout=300, env=env)
    finally:
        for f in fakes.values():
            f.close()
    return r.stdout + r.stderr, {n: [t for _, t in f.posts] for n, f in fakes.items()}


def test_cycle_all_trays_end_to_end(tmp_path):
    out, posts = _run_cycle_all(tmp_path)
    assert "PASSED tests/ras/test_dc_cycle_all.py::test_dc_cycle_every_compute" in out, out[-3000:]
    assert posts == {"c1": ["ForceOff", "On"], "c2": ["ForceOff", "On", "On"]}   # c2's first On ignored
    assert "Bmc-Pw-1" not in out


def test_cycle_all_refuses_when_a_bmc_login_fails(tmp_path):
    out, posts = _run_cycle_all(tmp_path, pw_c2="wrong")
    assert "FAILED tests/ras/test_dc_cycle_all.py::test_dc_cycle_every_compute" in out, out[-3000:]
    assert "nothing was power cycled" in out and "c2:" in out and "login refused" in out
    assert posts == {"c1": [], "c2": []}
