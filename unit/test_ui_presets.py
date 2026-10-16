"""UI presets (per rack, validated like a run, never the password), downloads (run log, results zip, single report
files; nothing outside the rack's results folder) and the run record "Run again" fills the form from."""
from __future__ import annotations

import http.client
import io
import json
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
import yaml

from ui import presets
from ui.config import RackProfile
from ui.runner import Executor, JobError
from ui.server import App, make_server

PW = "Preset-Test-Pa55w0rd"
PW_ENV = "HELIOS_UI_PRESET_TEST_PW"


class Client:
    def __init__(self, port: int):
        self.port = port

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "X-Helios-UI": "1"})
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data, hdrs = r.read(), {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, data, hdrs

    def j(self, method, path, body=None):
        st, data, _ = self.req(method, path, body)
        return st, json.loads(data or b"null")


class EchoExecutor(Executor):
    """Stand-in for pytest: prints the password variable it got (the UI must redact it) and exits."""

    def popen(self, argv, env=None, cwd=None, stdin=False):
        code = f"import os; print('pw is', os.environ.get({PW_ENV!r}), flush=True); print('done', flush=True)"
        return super().popen([sys.executable, "-c", code], env=env, cwd=cwd, stdin=stdin)


def _profile(tmp_path: Path, name: str, results: Path) -> RackProfile:
    inv = tmp_path / f"{name}.yaml"
    inv.write_text(yaml.safe_dump({"rack": name, "switches": [], "computes": [],
                                   "afm": {"url": "https://x", "auth": {"username": "admin", "password_env": PW_ENV}}}))
    return RackProfile(name=name, title=name, runner="local", inventory=inv, results_dir=results)


@pytest.fixture
def ui(tmp_path):
    results = tmp_path / "results"           # both racks share one results folder (as in ui/racks.yaml)
    profiles = {n: _profile(tmp_path, n, results) for n in ("rack-a", "rack-b")}
    app = App(profiles=profiles, executor=EchoExecutor())
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Client(srv.server_address[1]), app, results
    app.shutdown()
    srv.shutdown()
    srv.server_close()


def _no_pw(root: Path) -> None:
    for f in root.rglob("*"):
        if f.is_file():
            assert PW.encode() not in f.read_bytes(), f"password in {f}"


PRESET = {"kind": "pytest", "tc": "TC-SWH-002, TC-AFM-001", "marker": "", "paths": "tests/afm tests/switch",
          "switches": "st1,st2", "computes": "", "allow_disruptive": False, "pytest_args": "--ignore tests/afm",
          "filters": {"level": "L1", "scope": "Switch (AFM API)", "suite": "afm", "status": ""}}


# ------------------------------------------------------------------ presets

def test_preset_round_trip_and_password_never_stored(ui):
    c, _, results = ui
    st, d = c.j("POST", "/api/racks/rack-a/presets",
                {"name": "smoke L1", "preset": {**PRESET, "password": PW, "token": PW}, "password": PW})
    assert st == 201, d
    s = d["settings"]
    assert s["tc"] == ["TC-SWH-002", "TC-AFM-001"] and s["paths"] == ["tests/afm", "tests/switch"]
    assert s["switches"] == "st1,st2" and s["pytest_args"] == ["--ignore", "tests/afm"]
    assert s["filters"] == {"level": "L1", "scope": "Switch (AFM API)", "suite": "afm"}
    assert "password" not in s and "token" not in s
    st, lst = c.j("GET", "/api/racks/rack-a/presets")
    assert st == 200 and [p["name"] for p in lst] == ["smoke L1"] and lst[0]["settings"] == s
    f = results / "ui-presets.json"
    assert f.exists() and PW not in f.read_text()
    assert not list(results.glob(".ui-presets-*"))          # atomic write left no temp file
    # a health preset (with --known off) and a re-run preset
    st, d = c.j("POST", "/api/racks/rack-a/presets", {"name": "health-quick_1.0",
                                                      "preset": {"kind": "health", "known": False, "tc": "HC-001"}})
    assert st == 201 and d["settings"]["known"] is False and d["settings"]["tc"] == ["HC-001"]
    st, d = c.j("POST", "/api/racks/rack-a/presets", {"name": "rerun",
                                                      "preset": {"kind": "rerun", "base_run": "rack-a-20261016-1"}})
    assert st == 201 and d["settings"]["base_run"] == "rack-a-20261016-1"
    # overwrite needs the flag (the page asks first)
    st, d = c.j("POST", "/api/racks/rack-a/presets", {"name": "smoke L1", "preset": {"kind": "pytest", "marker": "l1"}})
    assert st == 409 and d["exists"]
    st, d = c.j("POST", "/api/racks/rack-a/presets",
                {"name": "smoke L1", "preset": {"kind": "pytest", "marker": "l1"}, "overwrite": True})
    assert st == 201 and d["settings"]["marker"] == "l1" and d["settings"]["tc"] == []
    # delete
    st, _ = c.j("POST", "/api/racks/rack-a/presets/delete", {"name": "smoke L1"})
    assert st == 200
    _, lst = c.j("GET", "/api/racks/rack-a/presets")
    assert [p["name"] for p in lst] == ["health-quick_1.0", "rerun"]
    st, _ = c.j("POST", "/api/racks/rack-a/presets/delete", {"name": "smoke L1"})
    assert st == 404
    _no_pw(results)


def test_preset_per_rack_isolation(ui):
    c, _, _ = ui
    assert c.j("POST", "/api/racks/rack-a/presets", {"name": "p", "preset": {"kind": "health"}})[0] == 201
    assert c.j("POST", "/api/racks/rack-b/presets", {"name": "p", "preset": {"kind": "pytest", "tc": "X-1"}})[0] == 201
    _, a = c.j("GET", "/api/racks/rack-a/presets")
    _, b = c.j("GET", "/api/racks/rack-b/presets")
    assert a[0]["settings"]["kind"] == "health" and b[0]["settings"]["kind"] == "pytest"
    c.j("POST", "/api/racks/rack-b/presets/delete", {"name": "p"})
    assert c.j("GET", "/api/racks/rack-b/presets")[1] == [] and len(c.j("GET", "/api/racks/rack-a/presets")[1]) == 1
    assert c.j("GET", "/api/racks/nope/presets")[0] == 404
    assert c.j("POST", "/api/racks/nope/presets", {"name": "p", "preset": {"kind": "health"}})[0] == 404


def test_unreadable_presets_file_is_an_error_not_a_crash(ui):
    c, _, results = ui
    results.mkdir(parents=True, exist_ok=True)
    (results / "ui-presets.json").write_text("{broken")
    st, d = c.j("GET", "/api/racks/rack-a/presets")
    assert st == 500 and "not valid JSON" in d["error"]
    st, d = c.j("POST", "/api/racks/rack-a/presets", {"name": "p", "preset": {"kind": "health"}})
    assert st == 400 and (results / "ui-presets.json").read_text() == "{broken"


@pytest.mark.parametrize("preset, why", [
    ({"kind": "pytest"}, "select cases"),                                   # a run would refuse it too
    ({"kind": "pytest", "tc": "TC-1; rm -rf /"}, "bad test case"),
    ({"kind": "pytest", "marker": "l1 && reboot"}, "marker"),
    ({"kind": "pytest", "tc": "A-1", "paths": "/etc"}, "under tests/"),
    ({"kind": "pytest", "tc": "A-1", "paths": "tests/../../x"}, "under tests/"),
    ({"kind": "pytest", "tc": "A-1", "switches": "st1;x"}, "node names"),
    ({"kind": "pytest", "tc": "A-1", "pytest_args": "--inventory other.yaml"}, "pytest_args"),
    ({"kind": "rerun"}, "base_run"),
    ({"kind": "shell"}, "kind must be"),
    ({"kind": "pytest", "tc": "A-1", "filters": {"evil": "x"}}, "unknown filter"),
    ({"kind": "pytest", "tc": "A-1", "filters": {"suite": "everything"}}, "suite"),
    ({"kind": "pytest", "tc": "A-1", "filters": {"level": "a\nb"}}, "one line"),
    ({"kind": "pytest", "tc": 5}, "invalid preset"),
    ("health", "object"),
])
def test_invalid_preset_rejected_like_a_run(ui, preset, why):
    c, _, results = ui
    st, d = c.j("POST", "/api/racks/rack-a/presets", {"name": "bad", "preset": preset})
    assert st == 400 and why in d["error"], d
    assert not (results / "ui-presets.json").exists()
    if isinstance(preset, dict) and "filters" not in preset and preset.get("tc") != 5:
        st, _ = c.j("POST", "/api/runs", {"rack": "rack-a", **preset})        # the same request as a run: refused
        assert st == 400


@pytest.mark.parametrize("name", ["", "   ", "a" * 61, "../x", "a/b", "semi;colon", "tab\tname", None, 7])
def test_preset_name_validation(ui, name):
    c, _, _ = ui
    st, d = c.j("POST", "/api/racks/rack-a/presets", {"name": name, "preset": {"kind": "health"}})
    assert st == 400 and "preset name" in d["error"]


def test_preset_module_direct(tmp_path):
    p = _profile(tmp_path, "r", tmp_path / "res")
    assert presets.check_name("  ok name-1_2.3 ") == "ok name-1_2.3" and len(presets.check_name("x" * 60)) == 60
    with pytest.raises(JobError):
        presets.save(p, "x", {"kind": "pytest", "tc": "A-1", "filters": "nope"})
    presets.save(p, "x", {"kind": "health", "Password": PW, "api_key": PW})
    raw = (tmp_path / "res" / "ui-presets.json").read_text()
    assert PW not in raw and set(json.loads(raw)["racks"]["r"]["x"]["settings"]) == \
        {"kind", "paths", "tc", "marker", "switches", "computes", "allow_disruptive", "known", "base_run",
         "pytest_args", "filters"}
    (tmp_path / "res" / "ui-presets.json").write_text("{broken")
    with pytest.raises(JobError, match="not valid JSON"):
        presets.list_presets(p)


# ------------------------------------------------------------------ run record for "Run again" + log download

def _wait(c, rid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        _, d = c.j("GET", f"/api/runs/{rid}")
        if d["status"] not in ("queued", "running"):
            return d
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_run_record_for_run_again_and_log_download(ui):
    c, app, results = ui
    req = {"rack": "rack-a", "kind": "pytest", "tc": "TC-SWH-002", "marker": "l1", "paths": "tests/switch",
           "switches": "st1", "computes": "ct01", "allow_disruptive": True, "pytest_args": "-x",
           "filters": {"level": "L1", "suite": "ssh"}, "password": PW}
    st, r = c.j("POST", "/api/runs", req)
    assert st == 201, r
    d = _wait(c, r["id"])
    assert d["status"] == "finished", d
    # what "Run again" reads (GET /api/runs/<id>, and the run list): the settings, never the password
    for rec in (c.j("GET", f"/api/runs/{r['id']}")[1], c.j("GET", "/api/runs?rack=rack-a")[1][0]):
        assert PW not in json.dumps(rec) and "password" not in rec and "password" not in rec["job"]
        assert rec["rack"] == "rack-a" and rec["has_password"] is True
        assert rec["filters"] == {"level": "L1", "suite": "ssh"}
        j = rec["job"]
        assert (j["kind"], j["tc"], j["marker"], j["paths"], j["switches"], j["computes"], j["allow_disruptive"],
                j["known"], j["pytest_args"]) == ("pytest", ["TC-SWH-002"], "l1", ["tests/switch"], "st1", "ct01",
                                                  True, True, ["-x"])
    # the record after a UI restart (read back from ui-runs/<id>.json)
    app.runs.runs.clear()
    rec = c.j("GET", f"/api/runs/{r['id']}")[1]
    assert rec["filters"] == {"level": "L1", "suite": "ssh"} and rec["job"]["tc"] == ["TC-SWH-002"]
    # log download: the whole log as text, attachment, password redacted
    st, body, hdrs = c.req("GET", f"/api/runs/{r['id']}/log.txt")
    assert st == 200 and hdrs["content-type"].startswith("text/plain")
    assert hdrs["content-disposition"] == f'attachment; filename="{r["id"]}.log.txt"'
    text = body.decode()
    assert "pw is <REDACTED>" in text and "done" in text and PW not in text
    assert c.req("GET", "/api/runs/nope-123/log.txt")[0] == 404
    assert c.req("GET", "/api/runs/..%2Fx/log.txt")[0] == 404
    _no_pw(results)
    # a filter a run would not accept
    st, d = c.j("POST", "/api/runs", {**req, "filters": {"suite": "bogus"}})
    assert st == 400 and "suite" in d["error"]


def test_log_download_while_running(ui, tmp_path):
    c, app, _ = ui

    class Slow(Executor):
        def popen(self, argv, env=None, cwd=None, stdin=False):
            code = "import time; print('first line', flush=True); time.sleep(60)"
            return super().popen([sys.executable, "-c", code], env=env, cwd=cwd, stdin=stdin)

    app.runs.runners["rack-b"].ex = Slow()
    _, r = c.j("POST", "/api/runs", {"rack": "rack-b", "kind": "health"})
    end = time.time() + 30
    while time.time() < end:
        st, body, _ = c.req("GET", f"/api/runs/{r['id']}/log.txt")
        if b"first line" in body:
            break
        time.sleep(0.1)
    assert st == 200 and b"first line" in body and b"# running" not in body
    c.j("POST", f"/api/runs/{r['id']}/stop", {})
    _wait(c, r["id"])


# ------------------------------------------------------------------ results downloads

def _folder(results: Path, name: str) -> Path:
    d = results / name
    (d / "logs").mkdir(parents=True)
    (d / "tc_summary.csv").write_text("tc_id,verdict\nHC-001,PASS\n")
    (d / "tc_results.csv").write_text("tc_id,verdict,detail\n")
    (d / "health.json").write_text('{"verdict": "GO"}')
    (d / "report.html").write_text("<html>report</html>")
    (d / "logs" / "HC-001.log").write_text("log line\n")
    return d


def test_results_zip_and_files(ui, tmp_path):
    c, _, results = ui
    d = _folder(results, "rack-a-20261016-101010")
    outside = tmp_path / "secret-outside.txt"
    outside.write_text("OUTSIDE-CONTENT")
    (tmp_path / "outside-dir").mkdir()
    (tmp_path / "outside-dir" / "x.txt").write_text("OUTSIDE-DIR")
    os.symlink(outside, d / "link.txt")
    os.symlink(tmp_path / "outside-dir", d / "logs" / "linkdir")
    (results / "ui-presets.json").write_text('{"version": 1, "racks": {}}')

    st, body, hdrs = c.req("GET", "/api/racks/rack-a/results/rack-a-20261016-101010/zip")
    assert st == 200 and hdrs["content-type"] == "application/zip"
    assert hdrs["content-disposition"] == 'attachment; filename="rack-a-20261016-101010.zip"'
    z = zipfile.ZipFile(io.BytesIO(body))
    names = sorted(z.namelist())
    assert names == sorted(f"rack-a-20261016-101010/{n}" for n in
                           ("health.json", "logs/HC-001.log", "report.html", "tc_results.csv", "tc_summary.csv"))
    assert z.read("rack-a-20261016-101010/logs/HC-001.log") == b"log line\n"
    blob = b"".join(z.read(n) for n in names)
    assert b"OUTSIDE" not in blob and PW.encode() not in blob

    st, body, hdrs = c.req("GET", "/api/racks/rack-a/results/rack-a-20261016-101010/file/report.html")
    assert st == 200 and body == b"<html>report</html>" and hdrs["content-type"].startswith("text/html")
    assert hdrs["content-disposition"] == 'attachment; filename="rack-a-20261016-101010-report.html"'
    for f in ("tc_summary.csv", "tc_results.csv", "health.json"):
        assert c.req("GET", f"/api/racks/rack-a/results/rack-a-20261016-101010/file/{f}")[0] == 200
    # only the listed names; none missing / symlinked / outside
    for bad in ("logs", "link.txt", "health.html", "health-notes.txt", "..", "inventory.yaml"):
        assert c.req("GET", f"/api/racks/rack-a/results/rack-a-20261016-101010/file/{bad}")[0] == 404, bad
    assert c.req("GET", "/api/racks/rack-a/results/rack-a-20261016-101010/file/logs%2FHC-001.log")[0] == 404
    (d / "health.html").symlink_to(outside)
    assert c.req("GET", "/api/racks/rack-a/results/rack-a-20261016-101010/file/health.html")[0] == 404

    # traversal / symlinked folder / the UI's own folder / unknown rack
    os.symlink(tmp_path / "outside-dir", results / "rack-a-linked")
    (results / "ui-runs").mkdir(exist_ok=True)
    for path in ("/api/racks/rack-a/results/../zip", "/api/racks/rack-a/results/..%2F..%2Fx/zip",
                 "/api/racks/rack-a/results/%2E%2E/zip", "/api/racks/rack-a/results/rack-a-linked/zip",
                 "/api/racks/rack-a/results/ui-runs/zip", "/api/racks/rack-a/results/ui-presets.json/zip",
                 "/api/racks/rack-a/results/rack-a-missing/zip", "/api/racks/nope/results/rack-a-20261016-101010/zip"):
        st, body, _ = c.req("GET", path)
        assert st in (400, 404), (path, st)
        assert b"OUTSIDE" not in body


def test_password_absent_from_real_run_zip_and_log(tmp_path):
    """Selftest run (simulated rack + fake AFM) with the password: the results zip and log download hold no
    password (the zip's entries are checked decompressed)."""
    import shutil
    SPW = "selftest-admin-pw"                    # the fake controller's admin password
    work = tmp_path / "selftest"
    shutil.copytree(Path(__file__).resolve().parent.parent / "selftest", work)
    inv = yaml.safe_load((work / "inventory.yaml").read_text())
    inv["afm"]["auth"].pop("password")
    inv["afm"]["auth"]["password_env"] = PW_ENV
    (work / "inventory.yaml").write_text(yaml.safe_dump(inv, sort_keys=False))
    profiles = {"sim": RackProfile(name="sim", title="sim", runner="local", inventory=work / "inventory.yaml",
                                   results_dir=tmp_path / "results")}
    app = App(profiles=profiles)
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(srv.server_address[1])
        _, r = c.j("POST", "/api/runs", {"rack": "sim", "kind": "pytest", "paths": "tests/afm/test_afm_health.py",
                                         "tc": "HC-000", "password": SPW})
        d = _wait(c, r["id"], timeout=300)
        assert d["verdict"] == "1 PASS", d                           # the password reached the run
        st, body, _ = c.req("GET", f"/api/racks/sim/results/{d['results'][0]}/zip")
        z = zipfile.ZipFile(io.BytesIO(body))
        assert st == 200 and any(n.endswith("tc_summary.csv") for n in z.namelist())
        assert all(SPW.encode() not in z.read(n) for n in z.namelist())
        st, body, _ = c.req("GET", f"/api/runs/{r['id']}/log.txt")
        assert st == 200 and b"HC-000" in body and SPW.encode() not in body
        st, body, _ = c.req("GET", f"/api/racks/sim/results/{d['results'][0]}/file/report.html")
        assert st == 200 and SPW.encode() not in body
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()
