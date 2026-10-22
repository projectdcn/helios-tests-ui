"""Results tab Clear / Trash / Restore (ui/trash.py): nothing is deleted, a cleared run leaves Case history and comes
back with Restore, a cleared session keeps its runs, a busy rack and bad names are refused."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from ui import sessions, trash
from ui.config import ROOT, RackProfile
from ui.server import App, make_server

from test_ui_keychain import Client, HashExecutor

sys.path.insert(0, str(ROOT / "tools"))
import results_index as ri  # noqa: E402


def _profile(tmp_path) -> RackProfile:
    inv = tmp_path / "rk.yaml"
    inv.write_text("rack: rk\nswitches: []\ncomputes: []\n")
    return RackProfile(name="rk", title="rk", runner="local", inventory=inv, results_dir=tmp_path / "res" / "runs" / "rk")


def _run(p: RackProfile, name: str, tc="TC-1") -> Path:
    d = p.results_dir / name
    (d / "logs").mkdir(parents=True)
    (d / "logs" / "001.log").write_text("log\n")
    (d / "tc_summary.csv").write_text("tc_id,verdict\nTC-1,PASS\n")
    (d / "tc_results.csv").write_text(f"tc_id,nodeid,outcome,started,log\n{tc},t::x,passed,2026-10-21 10:00:00,logs/001.log\n")
    ri.update_run(d)
    return d


def _indexed(p: RackProfile) -> set[str]:
    return {r["run"] for r in ri.read(p.results_dir.parent.parent)}


def test_clear_and_restore_a_run(tmp_path):
    p = _profile(tmp_path)
    _run(p, "rk-20261021-100000")
    assert _indexed(p) == {"rk-20261021-100000"}
    out = trash.clear_run(p, "rk-20261021-100000", by="alice")
    assert not (p.results_dir / "rk-20261021-100000").exists() and _indexed(p) == set()
    entry = p.results_dir / ".trash" / out["entry"]
    assert (entry / "logs" / "001.log").read_text() == "log\n"                       # moved, not deleted
    [e] = trash.listing(p)
    assert (e["name"], e["kind"], e["by"], e["restorable"]) == ("rk-20261021-100000", "run", "alice", True)
    trash.restore(p, e["entry"])
    assert (p.results_dir / "rk-20261021-100000" / "logs" / "001.log").is_file() and not trash.listing(p)
    assert _indexed(p) == {"rk-20261021-100000"} and not (p.results_dir / "rk-20261021-100000" / ".trash.json").exists()


def test_clear_a_session_keeps_its_runs_and_restores_its_entry(tmp_path):
    p = _profile(tmp_path)
    run = _run(p, "rk-20261021-110000")
    sid = sessions.new_session(p, "morning")["id"]
    sessions.add_results(p, sid, [run.name])
    sessions.rebuild(p, sid)
    out = trash.clear_session(p, sid)
    assert run.is_dir() and not (p.results_dir / sid).exists()
    lst = sessions.list_sessions(p)
    assert lst["sessions"] == [] and lst["current"] is None
    assert out["was_current"] and trash.listing(p)[0]["kind"] == "session"
    trash.restore(p, out["entry"])
    s = sessions.get(p, sid)
    assert s["name"] == "morning" and s["runs"] == [run.name] and (p.results_dir / sid / "session.json").is_file()


def test_refusals(tmp_path):
    p = _profile(tmp_path)
    _run(p, "rk-20261021-120000")
    for bad in ("../x", ".trash", "rk-session-20261021-120000"):
        with pytest.raises((KeyError, Exception)):
            trash.clear_run(p, bad)
    out = trash.clear_run(p, "rk-20261021-120000")
    _run(p, "rk-20261021-120000")                    # the name exists again: restore refuses to overwrite it
    assert trash.listing(p)[0]["restorable"] is False
    with pytest.raises(Exception, match="exists again"):
        trash.restore(p, out["entry"])
    with pytest.raises(Exception):
        trash.restore(p, "../../etc@20261021-120000")


def test_routes_and_a_busy_rack(tmp_path):
    p = _profile(tmp_path)
    _run(p, "rk-20261021-130000")
    app = App(profiles={"rk": p}, executor=HashExecutor())
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(srv.server_address[1])
    try:
        app.runs.busy = lambda rack: {"running": "r1", "queued": []}           # a run in progress
        st, d = c.j("POST", "/api/racks/rk/results/rk-20261021-130000/clear", {})
        assert st == 409 and "running" in d["error"]
        app.runs.busy = lambda rack: {"running": None, "queued": []}
        st, d = c.j("POST", "/api/racks/rk/results/rk-20261021-130000/clear", {})
        assert st == 200 and d["kind"] == "run"
        st, lst = c.j("GET", "/api/racks/rk/trash")
        assert st == 200 and lst[0]["name"] == "rk-20261021-130000"
        enc = lst[0]["entry"].replace("@", "%40")             # as the browser's encodeURIComponent sends it
        st, d = c.j("POST", f"/api/racks/rk/trash/{enc}/restore", {})
        assert st == 200 and (p.results_dir / "rk-20261021-130000").is_dir()
        st, _ = c.j("POST", "/api/racks/rk/results/nope-1/clear", {})
        assert st == 404
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()


def test_clear_selected_delete_and_empty(tmp_path):
    """Clear selected moves all (index rewritten once) or none (an unknown name); Delete permanently / Empty trash
    remove entries for good, logged in .trash/deleted.log; only real entries inside .trash are accepted."""
    p = _profile(tmp_path)
    for n in ("rk-20261021-140000", "rk-20261021-140100", "rk-20261021-140200"):
        _run(p, n)
    with pytest.raises(KeyError):
        trash.clear_runs(p, ["rk-20261021-140000", "rk-nope-1"])
    assert len(_indexed(p)) == 3 and not trash.listing(p)                       # all or none
    out = trash.clear_runs(p, ["rk-20261021-140000", "rk-20261021-140100"], by="bob")
    assert len(out["cleared"]) == 2 and _indexed(p) == {"rk-20261021-140200"}
    first, second = sorted(e["entry"] for e in trash.listing(p))
    d = trash.delete(p, first, by="bob")
    assert d["deleted"] == [first] and d["bytes"] > 0 and not (p.results_dir / ".trash" / first).exists()
    outside = tmp_path / "keep"
    outside.mkdir()
    (p.results_dir / ".trash" / "evil@20261021-150000").symlink_to(outside)     # a link: never followed / deleted
    for bad in ("evil@20261021-150000", "../rk-20261021-140200", "rk-20261021-140200"):
        with pytest.raises((KeyError, Exception)):
            trash.delete(p, bad)
    assert outside.is_dir() and (p.results_dir / "rk-20261021-140200").is_dir()
    assert [x["entry"] for x in trash.listing(p)] == [second]                 # the link is not offered
    e = trash.empty(p, by="bob")
    assert outside.is_dir()
    assert e["deleted"] == [second] and not trash.listing(p)
    log = (p.results_dir / ".trash" / "deleted.log").read_text().splitlines()
    assert len(log) == 2 and all("\tdeleted\t" in ln and ln.endswith("\tbob") for ln in log)


def test_bulk_and_delete_routes(tmp_path):
    p = _profile(tmp_path)
    for n in ("rk-20261021-160000", "rk-20261021-160100"):
        _run(p, n)
    app = App(profiles={"rk": p}, executor=HashExecutor())
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(srv.server_address[1])
    try:
        st, d = c.j("POST", "/api/racks/rk/results/clear", {"names": ["rk-20261021-160000", "rk-20261021-160100"]})
        assert st == 200 and len(d["cleared"]) == 2
        st, _ = c.j("POST", "/api/racks/rk/results/clear", {"names": "x"})
        assert st == 400
        st, lst = c.j("GET", "/api/racks/rk/trash")
        enc = lst[0]["entry"].replace("@", "%40")
        st, d = c.j("POST", f"/api/racks/rk/trash/{enc}/delete", {})
        assert st == 200 and d["deleted"] == [lst[0]["entry"]]
        st, d = c.j("POST", "/api/racks/rk/trash/empty", {})
        assert st == 200 and len(d["deleted"]) == 1
        st, lst = c.j("GET", "/api/racks/rk/trash")
        assert lst == []
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()
