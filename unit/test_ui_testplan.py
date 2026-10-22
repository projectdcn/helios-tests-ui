"""Test-plan workbook upload / choice from the UI (ui/testplan.py). srv03 20 Oct: 'workbook not found:
/root/helios-ui/data/testplan/full_rack_testplan.xlsx' with no way to add one from the browser, and a fixed name."""
from __future__ import annotations

import base64
import io
import json
import threading
from pathlib import Path

import openpyxl
import pytest

from ui import testplan
from ui.config import ROOT, RackProfile
from ui.runner import JobError
from ui.server import App, make_server

from test_ui_keychain import Client, HashExecutor

REAL = ROOT / "testplan" / "full_rack_testplan.xlsx"


def _profile(tmp_path, workbook=None):
    inv = tmp_path / "rk.yaml"
    inv.write_text("rack: rk\nswitches: []\ncomputes: []\n")
    return RackProfile(name="rk", title="rk", runner="local", inventory=inv, results_dir=tmp_path / "data" / "results" / "rk",
                       workbook=workbook)


@pytest.mark.skipif(not REAL.is_file(), reason="no test-plan workbook in this checkout")
def test_upload_any_name_choose_and_keep_the_choice(tmp_path):
    configured = tmp_path / "data" / "testplan" / "full_rack_testplan.xlsx"      # racks.yaml's, missing (srv03)
    p = _profile(tmp_path, configured)
    data = base64.b64encode(REAL.read_bytes()).decode()
    d = testplan.upload(p, configured, "Helios BKC plan v3.xlsx", data)
    assert d["cases"] > 100 and p.workbook.name == "Helios BKC plan v3.xlsx" and p.workbook.is_file()
    assert p.workbook.parent == configured.parent
    # a restart reads the choice back; racks.yaml's name is only the fallback
    p2 = _profile(tmp_path, configured)
    assert testplan.chosen(p2, configured).name == "Helios BKC plan v3.xlsx"
    # the same name again: the old file is kept as .bak-<time>, not listed
    d = testplan.upload(p, configured, "Helios BKC plan v3.xlsx", data)
    assert d["replaced"].startswith("Helios BKC plan v3.bak-")
    (configured.parent / "full_rack_testplan.backup-20261013-195715.xlsx").write_bytes(b"x")   # older tools' copies
    ls = testplan.listing(p, configured)
    assert [f["name"] for f in ls["files"]] == ["Helios BKC plan v3.xlsx"] and ls["from_ui"] and ls["current_exists"]
    testplan.choose(p, configured, None)                           # back to racks.yaml's
    assert p.workbook == configured and testplan.chosen(_profile(tmp_path, configured), configured) == configured


def test_a_workbook_path_of_another_installation_falls_back_to_this_ones(tmp_path):
    """racks.yaml restored / copied from an installation in another directory: its workbook: path is not here; the
    rack uses the same name in its own <root>/testplan, else the only workbook there; uploads go there too."""
    root = tmp_path / "opt" / "helios-ui" / "data"
    p = RackProfile(name="rk", title="rk", runner="local", inventory=tmp_path / "rk.yaml",
                    results_dir=root / "results" / "runs" / "rk")
    stale = tmp_path / "home" / "old" / "helios-ui" / "data" / "testplan" / "full_rack_testplan.xlsx"
    assert testplan.default_dir(p) == root / "testplan"
    assert testplan.configured(p, stale) == stale                    # nothing here yet: shown as missing
    (root / "testplan").mkdir(parents=True)
    assert testplan.plan_dir(p, stale) == root / "testplan"          # uploads land in this installation
    (root / "testplan" / "Helios plan v4.xlsx").write_bytes(b"x")
    assert testplan.configured(p, stale) == root / "testplan" / "Helios plan v4.xlsx"   # the only one
    (root / "testplan" / "other.xlsx").write_bytes(b"x")
    assert testplan.configured(p, stale) == stale                    # two: the user picks one in the Setup tab
    (root / "testplan" / "full_rack_testplan.xlsx").write_bytes(b"x")
    assert testplan.configured(p, stale) == root / "testplan" / "full_rack_testplan.xlsx"   # same name wins
    here = root / "testplan" / "other.xlsx"
    assert testplan.configured(p, here) == here and testplan.configured(p, None) is None


def test_upload_refuses_what_is_not_a_readable_test_plan(tmp_path):
    p = _profile(tmp_path)
    with pytest.raises(JobError, match="not an Excel .xlsx"):
        testplan.upload(p, None, "plan.xlsx", base64.b64encode(b"hello").decode())
    wb = openpyxl.Workbook()
    wb.active["A1"] = "just a sheet"
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(JobError, match="not a test plan this UI can read"):
        testplan.upload(p, None, "other.xlsx", base64.b64encode(buf.getvalue()).decode())
    for bad in ("../x.xlsx", ".hidden.xlsx", "plan.xls", "a/b.xlsx"):
        name = Path(bad).name
        if testplan.NAME_RX.match(name):                           # basename of a path: the path part is dropped
            continue
        with pytest.raises(JobError):
            testplan.upload(p, None, bad, "")
    assert not (tmp_path / "data" / "testplan").exists() or not list((tmp_path / "data" / "testplan").glob("*.xlsx"))


@pytest.mark.skipif(not REAL.is_file(), reason="no test-plan workbook in this checkout")
def test_upload_route_then_the_plan_reads_it(tmp_path):
    p = _profile(tmp_path, tmp_path / "data" / "testplan" / "full_rack_testplan.xlsx")
    app = App(profiles={"rk": p}, executor=HashExecutor())
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(srv.server_address[1])
    try:
        st, d = c.j("GET", "/api/racks/rk/plan")
        assert st == 400 and "upload it" in d["error"]
        body = {"name": "my plan.xlsx", "data": base64.b64encode(REAL.read_bytes()).decode()}
        st, raw = c.req("POST", "/api/racks/rk/testplan/upload", body)
        assert st == 201 and json.loads(raw)["current"] == "my plan.xlsx"
        st, d = c.j("GET", "/api/racks/rk/plan")
        assert st == 200 and d["workbook"] == "my plan.xlsx" and d["cases"]
        st, d = c.j("GET", "/api/racks/rk/testplan")
        assert d["current"] == "my plan.xlsx" and d["files"][0]["current"]
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()


@pytest.mark.skipif(not REAL.is_file(), reason="no test-plan workbook in this checkout")
def test_download_the_current_or_a_named_workbook(tmp_path):
    """Setup tab Download: the rack's current workbook (no name) or one of its folder by name, byte for byte;
    a backup, a path or an unknown name is refused."""
    d = tmp_path / "data" / "testplan"
    d.mkdir(parents=True)
    cur = d / "full_rack_testplan.xlsx"
    cur.write_bytes(REAL.read_bytes())
    (d / "other plan.xlsx").write_bytes(b"PK-other")
    (d / "full_rack_testplan.bak-20261021.xlsx").write_bytes(b"PK-bak")
    p = _profile(tmp_path, cur)
    app = App(profiles={"rk": p}, executor=HashExecutor())
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(srv.server_address[1])
    try:
        st, raw = c.req("GET", "/api/racks/rk/testplan/download")
        assert st == 200 and raw == REAL.read_bytes()
        st, raw = c.req("GET", "/api/racks/rk/testplan/download?name=other%20plan.xlsx")
        assert st == 200 and raw == b"PK-other"
        for bad in ("full_rack_testplan.bak-20261021.xlsx", "..%2F..%2Frk.yaml", "nope.xlsx", "x.txt"):
            st, _ = c.req("GET", f"/api/racks/rk/testplan/download?name={bad}")
            assert st == 404, bad
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()
