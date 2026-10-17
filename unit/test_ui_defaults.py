"""The Run form's saved default per rack (<results_dir>/ui-defaults.json): saved / loaded / deleted per rack, validated
like a preset (and so like a run), never the password; "Reset to default" deletes it (the API side) without touching
the presets. The page's original form is ORIGINAL_FORM in app.js, in one place."""
from __future__ import annotations

import http.client
import json
import re
import threading
from pathlib import Path

import pytest
import yaml

from ui import defaults, presets
from ui.config import RackProfile
from ui.runner import JobError
from ui.server import App, make_server

PW = "Default-Test-Pa55w0rd"
STATIC = Path(__file__).resolve().parent.parent / "ui" / "static"

FORM = {"kind": "pytest", "tc": "TC-SWH-002, TC-AFM-001", "marker": "", "paths": "tests/afm tests/switch",
        "switches": "st1,st2", "computes": "", "allow_disruptive": False, "pytest_args": "--ignore tests/afm",
        "filters": {"level": "L1", "scope": "Switch (AFM API)", "suite": "afm", "status": ""}}


def _profile(tmp_path: Path, name: str, results: Path) -> RackProfile:
    inv = tmp_path / f"{name}.yaml"
    inv.write_text(yaml.safe_dump({"rack": name, "switches": [], "computes": []}))
    return RackProfile(name=name, title=name, runner="local", inventory=inv, results_dir=results)


class Client:
    def __init__(self, port):
        self.port = port

    def j(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "X-Helios-UI": "1"})
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data or b"null")


@pytest.fixture
def ui(tmp_path):
    results = tmp_path / "results"               # both racks share one results folder
    app = App(profiles={n: _profile(tmp_path, n, results) for n in ("rack-a", "rack-b")})
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Client(srv.server_address[1]), app, results
    app.shutdown()
    srv.shutdown()
    srv.server_close()


def test_default_save_load_delete_per_rack_never_the_password(ui):
    c, _, results = ui
    assert c.j("GET", "/api/racks/rack-a/defaults") == (200, {"saved": None, "settings": None})
    st, d = c.j("POST", "/api/racks/rack-a/defaults",
                {"settings": {**FORM, "password": PW, "Token": PW, "remember_password": True}, "password": PW})
    assert st == 201, d
    s = d["settings"]
    assert s["tc"] == ["TC-SWH-002", "TC-AFM-001"] and s["paths"] == ["tests/afm", "tests/switch"]
    assert s["filters"] == {"level": "L1", "scope": "Switch (AFM API)", "suite": "afm"}
    assert not {"password", "Token", "remember_password"} & set(s)
    st, g = c.j("GET", "/api/racks/rack-a/defaults")
    assert st == 200 and g["settings"] == s and g["saved"] == d["saved"]
    raw = (results / "ui-defaults.json").read_text()
    assert PW not in raw and not list(results.glob(".ui-defaults-*"))     # atomic write, no temp file left
    # per rack: rack-b has none until it saves its own; overwriting is plain (one default per rack)
    assert c.j("GET", "/api/racks/rack-b/defaults")[1]["settings"] is None
    assert c.j("POST", "/api/racks/rack-b/defaults", {"settings": {"kind": "health", "known": False}})[0] == 201
    assert c.j("POST", "/api/racks/rack-a/defaults", {"settings": {"kind": "health", "tc": "HC-001"}})[0] == 201
    a, b = c.j("GET", "/api/racks/rack-a/defaults")[1], c.j("GET", "/api/racks/rack-b/defaults")[1]
    assert a["settings"]["tc"] == ["HC-001"] and b["settings"]["known"] is False
    assert c.j("GET", "/api/racks/nope/defaults")[0] == 404
    assert c.j("POST", "/api/racks/nope/defaults", {"settings": {"kind": "health"}})[0] == 404
    for f in results.rglob("*"):
        assert not f.is_file() or PW.encode() not in f.read_bytes()


def test_reset_deletes_only_the_racks_default_not_presets(ui):
    c, _, results = ui
    assert c.j("POST", "/api/racks/rack-a/presets", {"name": "keep me", "preset": FORM})[0] == 201
    for r in ("rack-a", "rack-b"):
        assert c.j("POST", f"/api/racks/{r}/defaults", {"settings": FORM})[0] == 201
    presets_before = (results / "ui-presets.json").read_text()
    assert c.j("POST", "/api/racks/rack-a/defaults/delete", {}) == (200, {"deleted": True})
    assert c.j("GET", "/api/racks/rack-a/defaults")[1]["settings"] is None          # next load: original form
    assert c.j("GET", "/api/racks/rack-b/defaults")[1]["settings"]["tc"]             # the other rack keeps its own
    assert c.j("POST", "/api/racks/rack-a/defaults/delete", {}) == (200, {"deleted": False})
    assert (results / "ui-presets.json").read_text() == presets_before
    assert [p["name"] for p in c.j("GET", "/api/racks/rack-a/presets")[1]] == ["keep me"]
    assert c.j("POST", "/api/racks/rack-b/defaults/delete", {})[1]["deleted"] is True
    assert not (results / "ui-defaults.json").exists()                               # last one: file removed
    assert c.j("POST", "/api/racks/rack-b/defaults/delete", {})[1]["deleted"] is False
    # CSRF guards as for every POST
    assert c.j("POST", "/api/racks/rack-a/defaults", {"settings": FORM}, headers={"X-Helios-UI": "0"})[0] == 403
    assert c.j("POST", "/api/racks/rack-a/defaults/delete", {}, headers={"Host": "evil.example"})[0] == 403


@pytest.mark.parametrize("settings, why", [
    ({"kind": "pytest"}, "select cases"),
    ({"kind": "pytest", "tc": "TC-1; rm -rf /"}, "bad test case"),
    ({"kind": "pytest", "tc": "A-1", "paths": "/etc"}, "under tests/"),
    ({"kind": "pytest", "tc": "A-1", "pytest_args": "--inventory other.yaml"}, "pytest_args"),
    ({"kind": "shell"}, "kind must be"),
    ({"kind": "pytest", "tc": "A-1", "filters": {"evil": "x"}}, "unknown filter"),
    ({"kind": "pytest", "tc": "A-1", "filters": {"level": "a\nb"}}, "one line"),
    (None, "object"),
    ("health", "object"),
])
def test_invalid_default_rejected_like_a_preset(ui, settings, why):
    c, _, results = ui
    st, d = c.j("POST", "/api/racks/rack-a/defaults", {"settings": settings})
    assert st == 400 and why in d["error"], d
    assert not (results / "ui-defaults.json").exists()


def test_unreadable_defaults_file(ui):
    c, _, results = ui
    results.mkdir(parents=True, exist_ok=True)
    (results / "ui-defaults.json").write_text("{broken")
    st, d = c.j("GET", "/api/racks/rack-a/defaults")
    assert st == 500 and "not valid JSON" in d["error"]
    assert c.j("POST", "/api/racks/rack-a/defaults", {"settings": {"kind": "health"}})[0] == 400
    assert (results / "ui-defaults.json").read_text() == "{broken"


def test_defaults_module_direct(tmp_path):
    p = _profile(tmp_path, "r", tmp_path / "res")
    assert defaults.get(p) == {"saved": None, "settings": None} and defaults.delete(p) is False
    defaults.save(p, {"kind": "health", "Password": PW, "api_key": PW})
    raw = (tmp_path / "res" / "ui-defaults.json").read_text()
    assert PW not in raw and set(json.loads(raw)["racks"]["r"]["settings"]) == set(presets.normalize({"kind": "health"}))
    with pytest.raises(JobError):
        defaults.save(p, {"kind": "pytest", "tc": "A-1", "filters": "nope"})
    assert defaults.delete(p) is True and not (tmp_path / "res" / "ui-defaults.json").exists()


def test_page_original_form_is_one_constant():
    """app.js defines the original Run form once (ORIGINAL_FORM) and every reset path goes through it; it matches
    what index.html shows on a fresh page."""
    js = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    m = re.search(r"const ORIGINAL_FORM = Object\.freeze\(\{(.*?)\}\);", js, re.S)
    assert m and js.count("ORIGINAL_FORM = ") == 1
    body = m.group(1)
    for frag in ('kind: "health"', "known: true", 'health_tc: ""', 'tc: ""', 'marker: ""', 'paths: "tests"',
                 'switches: ""', 'computes: ""', "allow_disruptive: false", 'pytest_args: ""', "filters:"):
        assert frag in body, frag
    assert "form.reset()" not in js                                   # no second, implicit original state
    assert js.count("fillForm(ORIGINAL_FORM)") >= 2                   # first load and the Reset button
    assert 'value="health" checked' in html and 'name="known" checked' in html and 'name="paths" value="tests"' in html
    assert 'id="default-save"' in html and 'id="default-reset"' in html
    assert 'name="remember_pw"' in html and 'id="pw-forget"' in html and 'id="pwstatus"' in html
    # the page never keeps the password: localStorage gets fullState() only, which has no password
    full = re.search(r"function fullState\(\) \{(.*?)\n\}", js, re.S).group(1)
    assert "password" not in full and "remember_pw" not in full
