"""Check access (tools/access_check.py) and its UI run kind: the rack's prerequisites as a checklist - SSH key, key
login, passwordless sudo, GPUs, tools, AFM - before any test (17 Oct: the UI server had the laptop's MR3 inventory
but not its SSH key; the first test run was what told)."""
from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import access_check as ac  # noqa: E402

from helios.inventory import load_inventory  # noqa: E402
from ui.config import RackProfile  # noqa: E402
from ui.runner import Job, build_args  # noqa: E402
from ui.server import App, latest_access, make_server  # noqa: E402

from test_ui_keychain import Client, HashExecutor  # noqa: E402


@pytest.fixture
def no_afm(monkeypatch):
    monkeypatch.setattr(ac, "afm_checks", lambda inv: [])


def _items(inv_path, **kw):
    return {(i["node"], i["check"]): i for i in ac.check(load_inventory(inv_path))}


def test_healthy_replayed_rack(no_afm, tmp_path):
    rc = ac.main(["--inventory", str(ROOT / "selftest" / "inventory.yaml"), "--results-dir", str(tmp_path)])
    out = json.loads(next(tmp_path.glob("selftest-access-*/access.json")).read_text())
    assert rc == 0 and out["rack"] == "selftest" and out["counts"]["fail"] == 0
    checks = {(i["node"], i["check"]) for i in out["items"]}
    for node in ("sw1", "c1", "c2"):
        assert (node, "passwordless sudo") in checks
    assert ("c1", "GPUs ACTIVE (afmctl)") in checks and ("sw1", "SONiC containers readable") in checks


def test_gpu_not_active_fails_with_what_to_do(no_afm, tmp_path):
    work = tmp_path / "selftest"
    shutil.copytree(ROOT / "selftest", work)
    f = work / "replay" / "afmctl_device_c2.json"
    f.write_text(f.read_text().replace('"config_phase": "ACTIVE"', '"config_phase": "PROVIDER"', 1))
    i = _items(work / "inventory.yaml")[("c2", "GPUs ACTIVE (afmctl)")]
    assert i["status"] == "fail" and "PROVIDER" in i["detail"] and i["fix"]
    assert _items(work / "inventory.yaml")[("c1", "GPUs ACTIVE (afmctl)")]["status"] == "ok"


def test_missing_key_is_one_clear_fail_per_node(no_afm, tmp_path):
    inv = tmp_path / "inv.yaml"
    inv.write_text("rack: r1\ntransport: {kind: ssh, connect_timeout: 2}\n"
                   "defaults: {common: {key_file: ~/.ssh/id_helios_test_not_here}, switch: {user: admin}}\n"
                   "switches: [{name: s1, host: 192.0.2.1}]\ncomputes: []\n")
    items = ac.check(load_inventory(inv))
    key = next(i for i in items if i["check"].startswith("SSH key"))
    assert key["status"] == "fail" and "does not exist on" in key["detail"] and "ssh-copy-id" in key["fix"]
    login = next(i for i in items if i["node"] == "s1")
    assert login["status"] == "fail" and login["detail"] == "no SSH key ~/.ssh/id_helios_test_not_here on this machine"


def test_access_run_kind_and_latest_result(tmp_path):
    job = Job.from_request({"kind": "access"})
    assert build_args(job, "inv.yaml", "res", "known.yaml", no_html=True) == \
        ["tools/access_check.py", "--inventory", "inv.yaml", "--results-dir", "res"]
    results = tmp_path / "results"
    inv = tmp_path / "rk.yaml"
    inv.write_text("rack: rk\nswitches: []\ncomputes: []\n")
    prof = RackProfile(name="rk", title="rk", runner="local", inventory=inv, results_dir=results)
    assert latest_access(prof) == {"found": False}
    for ts, verdict in (("20261017-100000", "FAIL"), ("20261017-110000", "OK")):
        d = results / f"rk-access-{ts}"
        d.mkdir(parents=True)
        (d / "access.json").write_text(json.dumps({"verdict": verdict, "counts": {}, "items": []}))
    got = latest_access(prof)
    assert got["found"] and got["verdict"] == "OK" and got["folder"] == "rk-access-20261017-110000"
    # an access run joins no session and its verdict is the check's
    app = App(profiles={"rk": prof}, executor=HashExecutor())
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(srv.server_address[1])
        st, r = c.j("POST", "/api/runs", {"rack": "rk", "kind": "access"})
        assert st == 201 and r["session"] is None
        assert c.j("GET", "/api/racks/rk/access")[1]["verdict"] == "OK"
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()


def test_a_crashing_check_still_writes_a_result(monkeypatch, tmp_path):
    """17 Oct, UI server: Check access left the panel at 'not checked yet' - a result is always written now."""
    def boom(inv):
        raise RuntimeError("something unexpected")
    monkeypatch.setattr(ac, "check", boom)
    rc = ac.main(["--inventory", str(ROOT / "selftest" / "inventory.yaml"), "--results-dir", str(tmp_path)])
    out = json.loads(next(tmp_path.glob("selftest-access-*/access.json")).read_text())
    assert rc == 1 and out["verdict"] == "FAIL"
    assert out["items"][0]["check"] == "the check itself" and "something unexpected" in out["items"][0]["detail"]
