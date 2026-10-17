"""UI controller end to end against the simulated rack (selftest replay + in-process fake AFM): start runs over the
HTTP API, live log (SSE), history, report serving, one run at a time per rack, stop, and the AFM password never
reaching a file, a log or a results folder."""
from __future__ import annotations

import http.client
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from ui.config import RackProfile
from ui.runner import Executor
from ui.server import App, make_server

ROOT = Path(__file__).resolve().parent.parent
PW = "selftest-admin-pw"          # the fake controller's admin password, typed in the browser in real use
PW_ENV = "HELIOS_UI_TEST_PW"


def _selftest(tmp_path: Path, break_c2: bool = False) -> Path:
    """selftest/ copied, the inventory's inline password replaced by password_env (as on a real rack)."""
    work = tmp_path / "selftest"
    shutil.copytree(ROOT / "selftest", work)
    inv = yaml.safe_load((work / "inventory.yaml").read_text())
    inv["afm"]["auth"].pop("password")
    inv["afm"]["auth"]["password_env"] = PW_ENV
    (work / "inventory.yaml").write_text(yaml.safe_dump(inv, sort_keys=False))
    if break_c2:
        rules = yaml.safe_load((work / "replay" / "responses.yaml").read_text())
        rules.insert(0, {"cmd": ".*", "host": "^c2$", "rc": 255, "stdout": "", "stderr": "ssh: connect to host c2: No route"})
        (work / "replay" / "responses.yaml").write_text(yaml.safe_dump(rules, sort_keys=False, width=200))
    return work


class Client:
    def __init__(self, port: int):
        self.port = port

    def req(self, method, path, body=None, headers=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "X-Helios-UI": "1"})
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return (r.status, data, r.getheader("Content-Type")) if raw else (r.status, json.loads(data or b"null"))

    def wait(self, rid, timeout=300):
        end = time.time() + timeout
        while time.time() < end:
            _, d = self.req("GET", f"/api/runs/{rid}")
            if d["status"] not in ("queued", "running"):
                return d
            time.sleep(0.3)
        raise AssertionError(f"run {rid} did not finish")


@pytest.fixture
def ui(tmp_path):
    started = []

    def start(profiles=None, executor=None, break_c2=False):
        work = _selftest(tmp_path, break_c2)
        profiles = profiles or {"sim": RackProfile(name="sim", title="sim", runner="local",
                                                   inventory=work / "inventory.yaml", results_dir=tmp_path / "results")}
        app = App(profiles=profiles, executor=executor)
        srv = make_server(app, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append((app, srv))
        return Client(srv.server_address[1]), app, work

    yield start
    for app, srv in started:
        app.shutdown()
        srv.shutdown()
        srv.server_close()


def _no_password_anywhere(*dirs: Path) -> None:
    for d in dirs:
        for f in d.rglob("*"):
            if f.is_file():
                assert PW.encode() not in f.read_bytes(), f"password found in {f}"


def test_run_with_password_live_log_history_report(ui, tmp_path):
    c, app, work = ui()
    st, r = c.req("POST", "/api/runs", {"rack": "sim", "kind": "pytest", "paths": "tests/afm/test_afm_health.py",
                                        "tc": "HC-000", "password": PW})
    assert st == 201, r
    assert "password" not in r and "password" not in r["job"] and PW not in json.dumps(r) and r["has_password"]
    # live log over server-sent events, until the run ends
    st, body, ctype = c.req("GET", f"/api/runs/{r['id']}/stream", raw=True)
    assert ctype == "text/event-stream"
    events = body.decode()
    assert "event: end" in events and "HC-000" in events
    d = c.wait(r["id"])
    assert d["status"] == "finished" and d["rc"] == 0, d
    run = d["results"][0]
    assert d["verdict"] == "1 PASS"                         # the password reached the run: HC-000 logged in
    # history + reports
    _, hist = c.req("GET", "/api/racks/sim/history")
    row = next(h for h in hist if h["name"] == run)
    assert row["counts"] == {"PASS": 1} and "report.html" in row["reports"]
    st, html, ctype = c.req("GET", f"/results/sim/{run}/report.html", raw=True)
    assert st == 200 and ctype.startswith("text/html") and b"HC-000" in html
    assert c.req("GET", f"/results/sim/{run}/../../selftest/inventory.yaml", raw=True)[0] == 404
    # the log endpoint (polling fallback) and the run list
    _, log = c.req("GET", f"/api/runs/{r['id']}/log")
    assert log["done"] and any("AFM password given (memory only)" in ln for ln in log["lines"])
    _, runs = c.req("GET", "/api/runs?rack=sim")
    assert runs[0]["id"] == r["id"]
    # nothing written anywhere holds the password: results, run logs, run records
    _no_password_anywhere(tmp_path / "results")
    assert app.runs.runs[r["id"]]._password is None


def test_without_password_afm_cases_skip(ui):
    c, _, _ = ui()
    _, r = c.req("POST", "/api/runs", {"rack": "sim", "kind": "pytest", "paths": "tests/afm/test_afm_health.py",
                                       "tc": "HC-000"})
    assert c.wait(r["id"])["verdict"] == "1 SKIP"


def test_health_run_then_rerun_failed(ui, tmp_path):
    c, _, _ = ui(break_c2=True)
    _, r = c.req("POST", "/api/runs", {"rack": "sim", "kind": "health", "pytest_args": "--ignore tests/afm",
                                       "password": PW})
    d = c.wait(r["id"])
    assert d["status"] == "finished" and d["verdict"] in ("NO-GO", "INCOMPLETE"), d
    base = d["results"][0]
    _, hist = c.req("GET", "/api/racks/sim/history")
    assert next(h for h in hist if h["name"] == base)["health"]["verdict"] == d["verdict"]
    assert (tmp_path / "results" / base / "health.html").exists()
    _, r2 = c.req("POST", "/api/runs", {"rack": "sim", "kind": "rerun", "base_run": base, "password": PW})
    d2 = c.wait(r2["id"])
    assert d2["status"] == "finished", d2
    merged = [n for n in d2["results"] if n.endswith("-merged")]
    assert merged and (tmp_path / "results" / merged[0] / "health.html").exists()
    assert (tmp_path / "results" / merged[0] / "merged-from.txt").read_text().split()[0] == base
    _no_password_anywhere(tmp_path / "results")


def test_one_run_at_a_time_per_rack(ui):
    c, app, _ = ui()
    req = {"rack": "sim", "kind": "pytest", "paths": "tests/afm/test_afm_health.py", "tc": "HC-000", "password": PW}
    _, a = c.req("POST", "/api/runs", req)
    _, b = c.req("POST", "/api/runs", req)
    assert b["status"] == "queued" and b["queue_position"] >= 1
    _, racks = c.req("GET", "/api/racks")
    sim = next(x for x in racks if x["name"] == "sim")
    assert b["id"] in sim["queued"] or sim["running"] == b["id"] or c.req("GET", f"/api/runs/{b['id']}")[1]["status"] != "queued"
    da, db = c.wait(a["id"]), c.wait(b["id"])
    assert da["status"] == db["status"] == "finished"
    assert db["started"] >= da["ended"]                # b waited for a
    assert app.runs.runs[b["id"]]._password is None


class SleepExecutor(Executor):
    """Runs a stand-in for pytest that prints the password variable it got, then sleeps (stop test)."""

    def popen(self, argv, env=None, cwd=None, stdin=False):
        code = (f"import os,time; print('got', os.environ.get({PW_ENV!r}), flush=True); "
                "print('working', flush=True); time.sleep(120)")
        return super().popen([sys.executable, "-c", code], env=env, cwd=cwd, stdin=stdin)


def test_stop_cancel_and_redaction(ui, tmp_path):
    c, _, _ = ui(executor=SleepExecutor())
    _, a = c.req("POST", "/api/runs", {"rack": "sim", "kind": "pytest", "tc": "HC-000", "password": PW})
    _, b = c.req("POST", "/api/runs", {"rack": "sim", "kind": "pytest", "tc": "HC-000", "password": PW})
    end = time.time() + 30
    while time.time() < end:
        _, log = c.req("GET", f"/api/runs/{a['id']}/log")
        if any("working" in ln for ln in log["lines"]):
            break
        time.sleep(0.2)
    # the child got the password through the environment; the log shows it redacted
    assert "got <REDACTED>" in log["lines"]
    _, x = c.req("POST", f"/api/runs/{b['id']}/stop", {})
    assert x["status"] == "cancelled"
    t0 = time.time()
    c.req("POST", f"/api/runs/{a['id']}/stop", {})
    d = c.wait(a["id"], timeout=30)
    assert d["status"] == "stopped" and time.time() - t0 < 20
    _no_password_anywhere(tmp_path / "results")


def test_requests_from_other_sites_are_refused(ui):
    c, _, _ = ui()
    st, _ = c.req("POST", "/api/runs", {"rack": "sim", "kind": "health"}, headers={"X-Helios-UI": "0"})
    assert st == 403
    st, _ = c.req("GET", "/api/racks", headers={"Host": "evil.example:8765"})
    assert st == 403
    st, d = c.req("POST", "/api/runs", {"rack": "nope", "kind": "health"})
    assert st == 400 and "unknown rack" in d["error"]
    st, body, ctype = c.req("GET", "/", raw=True)
    assert st == 200 and b"Helios" in body


def test_inventory_api_and_quarantine_toggle(ui):
    c, _, work = ui()
    inv = work / "inventory.yaml"
    _, d = c.req("GET", "/api/racks/sim/inventory")
    assert d["errors"] == [] and d["quarantine"] == {} and [r["name"] for r in d["form"]["computes"]] == ["c1", "c2"]
    q = {"node": "c2", "enabled": True, "reason": "tray down, ticket 42", "until": "2099-12-31"}
    _, pre = c.req("POST", "/api/racks/sim/inventory/check", {"quarantine": q})
    assert any("needs its AFM 'slot'" in e for e in pre["errors"])   # no slot yet: refused (g02-g17 17 Oct)
    st, _ = c.req("POST", "/api/racks/sim/inventory/save", {"quarantine": q, "sha": d["sha"]})
    assert st == 400 and "quarantine:" not in inv.read_text()
    with_slot = d["text"].replace("- name: c2\n", "- name: c2\n  slot: 1\n", 1)
    st, res = c.req("POST", "/api/racks/sim/inventory/save", {"text": with_slot, "sha": d["sha"]})
    assert st == 200 and res["saved"]
    _, d = c.req("GET", "/api/racks/sim/inventory")
    _, pre = c.req("POST", "/api/racks/sim/inventory/check", {"quarantine": q})
    assert "+  quarantine: {reason: 'tray down, ticket 42'" in pre["diff"] and pre["errors"] == []
    assert "quarantine:" not in inv.read_text()                       # preview only
    st, res = c.req("POST", "/api/racks/sim/inventory/save", {"quarantine": q, "sha": d["sha"]})
    assert st == 200 and res["saved"] and Path(res["backup"]).read_text() == d["text"]
    _, d2 = c.req("GET", "/api/racks/sim/inventory")
    assert d2["quarantine"]["c2"]["reason"] == "tray down, ticket 42"
    st, err = c.req("POST", "/api/racks/sim/inventory/save", {"quarantine": q, "sha": d["sha"]})
    assert st == 400 and "changed since" in err["error"]                 # stale sha
    bad = d2["text"].replace(f"password_env: {PW_ENV}", "password: hunter2")
    st, err = c.req("POST", "/api/racks/sim/inventory/save", {"text": bad, "sha": d2["sha"]})
    assert st == 400 and "secret" in err["error"] and "hunter2" not in inv.read_text()
    st, res = c.req("POST", "/api/racks/sim/inventory/save", {"quarantine": {"node": "c2", "enabled": False},
                                                               "sha": d2["sha"]})
    assert st == 200 and inv.read_text() == d["text"]
