"""UI sessions (ui/sessions.py): runs started one after another on a rack are consolidated in one results folder -
latest result per test item (an executed result beats a later skip / block), logs, csv files, session.json and the
report; results/index.csv does not index session folders and retention protects them and their runs."""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import results_index as ri  # noqa: E402
import results_retention as rr  # noqa: E402

from ui import sessions  # noqa: E402
from ui.config import RackProfile  # noqa: E402
from ui.runner import NAME_RX, JobError  # noqa: E402
from ui.runs import history  # noqa: E402

from test_ui_server import PW, ui  # noqa: E402,F401 - the simulated-rack UI fixture

HDR = ["tc_id", "nodeid", "suite", "test", "description", "outcome", "duration_s", "properties", "message", "started", "log"]


def _profile(results_dir: Path, name: str = "mr3") -> RackProfile:
    return RackProfile(name=name, title=name, runner="local", inventory=results_dir / f"{name}.yaml",
                       results_dir=results_dir)


def _run(results_dir: Path, name: str, items: list[tuple[str, str, str]]) -> Path:
    """A results folder as the pytest plugin writes it: items = [(tc_id, test, outcome)]."""
    d = results_dir / name
    (d / "logs").mkdir(parents=True)
    rows = []
    for i, (tc, test, outcome) in enumerate(items, start=1):
        log = f"logs/{i:03d}_{test}.log"
        (d / log).write_text(f"{name} {test} {outcome}\n")
        rows.append([tc, f"tests/x.py::{test}", "x", test, "", outcome, 1, "", f"{outcome} in {name}",
                     "2026-10-16 10:00:00 UTC", log])
    with open(d / "tc_results.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HDR)
        w.writerows(rows)
    sys.path.insert(0, str(ROOT))
    from helios.pytest_plugin import write_summary
    write_summary(d / "tc_summary.csv", [{"tc_id": r[0], "outcome": r[5]} for r in rows])
    return d


def _summary(d: Path) -> dict[str, str]:
    return {r["tc_id"]: r["verdict"] for r in csv.DictReader(open(d / "tc_summary.csv", newline=""))}


def test_auto_create_join_and_new_session(tmp_path):
    p = _profile(tmp_path)
    assert sessions.list_sessions(p) == {"current": None, "sessions": []}
    sid = sessions.join(p, "mr3-20261016-100000-abcd")              # the first run creates one, named by date
    assert re.fullmatch(r"mr3-session-\d{8}-\d{6}", sid) and NAME_RX.match(sid)
    assert sessions.join(p, "mr3-20261016-100100-efgh") == sid      # the next run joins it
    s = sessions.get(p, sid)
    assert s["ui_runs"] == ["mr3-20261016-100000-abcd", "mr3-20261016-100100-efgh"]
    assert re.fullmatch(r"\d\d [A-Z][a-z]{2} \d\d:\d\d", s["name"])          # e.g. "16 Oct 21:58"
    new = sessions.new_session(p, "PSU checks")                       # becomes the current one
    assert new["id"] != sid and new["name"] == "PSU checks"
    assert sessions.join(p, "mr3-20261016-110000-ijkl") == new["id"]
    lst = sessions.list_sessions(p)
    assert lst["current"] == new["id"] and [s["id"] for s in lst["sessions"]] == [new["id"], sid]
    d = json.loads((tmp_path / "ui-sessions.json").read_text())       # {rack: {current, sessions}}
    assert d["mr3"]["current"] == new["id"] and set(d["mr3"]["sessions"]) == {sid, new["id"]}
    for bad in ("", "x" * 61, "../etc", "a/b", "semi;colon"):
        with pytest.raises(JobError):
            sessions.new_session(p, bad or " ")
    # racks sharing a results_dir keep their own sessions
    other = _profile(tmp_path, "g02-g17")
    assert sessions.list_sessions(other)["sessions"] == []
    assert sessions.join(other, "g-run").startswith("g02-g17-session-")
    assert sessions.list_sessions(p)["current"] == new["id"]


def test_consolidated_latest_per_case_logs_summary_report(tmp_path):
    p = _profile(tmp_path)
    r1 = _run(tmp_path, "mr3-20261016-100000", [("TC-A", "test_a", "failed"), ("TC-B", "test_b", "passed"),
                                                ("TC-D", "test_d1", "passed"), ("TC-D", "test_d2", "passed")])
    r2 = _run(tmp_path, "mr3-20261016-101500", [("TC-A", "test_a", "passed"), ("TC-B", "test_b", "skipped"),
                                                ("TC-C", "test_c", "skipped"), ("TC-D", "test_d2", "failed")])
    r3 = _run(tmp_path, "mr3-20261016-103000", [("TC-C", "test_c", "blocked"), ("TC-E", "test_e", "partial")])
    sid = sessions.join(p, "ui-1")
    sessions.add_results(p, sid, [r1.name])
    sessions.add_results(p, sid, [r2.name, r2.name + "-merged"])      # a derived -merged folder is not recorded
    sessions.add_results(p, sid, [r3.name])
    assert sessions.get(p, sid)["runs"] == [r1.name, r2.name, r3.name]
    out = sessions.rebuild(p, sid)
    assert out == tmp_path / sid
    rows = {r["nodeid"].split("::")[1]: r for r in csv.DictReader(open(out / "tc_results.csv", newline=""))}
    got = {k: (r["outcome"], r["log"].split("/")[1]) for k, r in rows.items()}
    assert got == {
        "test_a": ("passed", r2.name),        # FAIL then PASS -> PASS
        "test_b": ("passed", r1.name),        # PASS then SKIP -> PASS (the skip does not hide the executed result)
        "test_c": ("blocked", r3.name),       # SKIP then BLOCKED -> the later one
        "test_d1": ("passed", r1.name),
        "test_d2": ("failed", r2.name),       # PASS then FAIL -> FAIL
        "test_e": ("partial", r3.name)}
    # logs copied, paths relative to the session folder
    for r in rows.values():
        assert r["log"].startswith(f"logs/{r['log'].split('/')[1]}/")
        assert (out / r["log"]).read_text().startswith(r["log"].split("/")[1])
    assert (out / rows["test_a"]["log"]).read_text() == f"{r2.name} test_a passed\n"
    # summary recomputed from the merged rows like the plugin
    assert _summary(out) == {"TC-A": "PASS", "TC-B": "PASS", "TC-C": "BLOCKED", "TC-D": "FAIL", "TC-E": "PARTIAL"}
    meta = json.loads((out / "session.json").read_text())
    assert meta["runs"] == [r1.name, r2.name, r3.name] and meta["name"] == sessions.get(p, sid)["name"]
    assert meta["cases"]["tests/x.py::test_b"] == {"tc_id": "TC-B", "outcome": "passed", "run": r1.name,
                                                   "started": "2026-10-16 10:00:00 UTC",
                                                   "log": f"logs/{r1.name}/002_test_b.log"}
    page = (out / "report.html").read_text()
    assert f"mr3 session {meta['name']}" in page and "TC-A" in page and r2.name in page
    # the listing: one entry with the consolidated summary
    s = sessions.list_sessions(p)["sessions"][0]
    assert s["summary"] == "5 cases: 2 PASS 1 BLOCKED 1 FAIL 1 PARTIAL" and s["written"] and "report.html" in s["reports"]
    # rebuilding replaces the folder (no stale files) and the plain run list does not show session folders
    (out / "stale.txt").write_text("x")
    sessions.rebuild(p, sid)
    assert not (out / "stale.txt").exists()
    (tmp_path / "mr3.yaml").write_text("rack: mr3\n")
    assert sid not in [h["name"] for h in history(p)] and r1.name in [h["name"] for h in history(p)]


def test_summary_text():
    assert sessions.summary_text({}) == "no cases yet"
    assert sessions.summary_text({"PASS": 9, "SKIP": 2, "FAIL": 1}) == "12 cases: 9 PASS 2 SKIP 1 FAIL"
    assert sessions.summary_text({"PASS": 1}) == "1 case: 1 PASS"


def test_index_skips_session_folders(tmp_path):
    rack = tmp_path / "runs" / "mr3"
    r1 = _run(rack, "mr3-20261016-100000", [("TC-A", "test_a", "passed")])
    p = _profile(rack)
    sid = sessions.join(p, "ui-1")
    sessions.add_results(p, sid, [r1.name])
    sessions.rebuild(p, sid)
    assert ri.run_rows(rack / sid, "mr3") == []
    ri.rebuild(tmp_path)
    assert {r["run"] for r in ri.read(tmp_path)} == {r1.name}
    assert ri.update_run(rack / sid) and {r["run"] for r in ri.read(tmp_path)} == {r1.name}


def test_retention_protects_sessions_and_their_runs(tmp_path):
    now = datetime(2027, 1, 31, 12, 0, 0)
    (tmp_path / "retention.yaml").write_text("keep_full_runs: 0\nkeep_full_days: 30\ndelete_logs_days: 90\n")
    rack = tmp_path / "runs" / "mr3"
    name = lambda days: f"mr3-{(now - timedelta(days=days)).strftime('%Y%m%d-%H%M%S')}"  # noqa: E731
    inside = _run(rack, name(120), [("TC-A", "test_a", "passed")])
    outside = _run(rack, name(121), [("TC-A", "test_a", "passed")])
    sess = rack / f"mr3-session-{(now - timedelta(days=200)).strftime('%Y%m%d-%H%M%S')}"
    (sess / "logs" / inside.name).mkdir(parents=True)
    (sess / "logs" / inside.name / "001_test_a.log").write_text("x")
    (sess / "session.json").write_text(json.dumps({"name": "s", "runs": [inside.name]}))
    keep = rr.protected(tmp_path)
    assert {inside.name, sess.name} <= keep and outside.name not in keep
    acts = rr.plan(tmp_path, now=now.timestamp())
    assert {p.parent.name for _, p, _ in acts} == {outside.name}
    # a session folder without a readable session.json is still protected itself
    (sess / "session.json").write_text("{not json")
    assert sess.name in rr.protected(tmp_path)


# ------------------------------------------------------------------ end to end on the simulated rack

def test_ui_runs_join_session_and_consolidate(ui, tmp_path):
    c, app, work = ui()
    results = tmp_path / "results"
    st, d = c.req("GET", "/api/racks/sim/sessions")
    assert st == 200 and d == {"current": None, "sessions": []}
    req = {"rack": "sim", "kind": "pytest", "paths": "tests/afm/test_afm_health.py", "tc": "HC-000"}
    # run 1 with the password: PASS; run 2 without: SKIP -> the session keeps the PASS
    _, a = c.req("POST", "/api/runs", {**req, "password": PW})
    da = c.wait(a["id"])
    assert da["status"] == "finished" and da["verdict"] == "1 PASS", da
    sid = da["session"]
    assert sid and sid.startswith("sim-session-")
    time.sleep(1.1)                                     # result folders are named by the second
    _, b = c.req("POST", "/api/runs", req)
    db = c.wait(b["id"])
    assert db["verdict"] == "1 SKIP" and db["session"] == sid
    rec = json.loads((results / "ui-runs" / f"{b['id']}.json").read_text())
    assert rec["session"] == sid                        # the run record says which session it joined
    _, d = c.req("GET", "/api/racks/sim/sessions")
    s = d["sessions"][0]
    assert d["current"] == sid and s["runs"] == da["results"] + db["results"] and s["ui_runs"] == [a["id"], b["id"]]
    assert s["summary"] == "1 case: 1 PASS" and "report.html" in s["reports"]
    folder = results / sid
    meta = json.loads((folder / "session.json").read_text())
    (case,) = meta["cases"].values()
    assert case["run"] == da["results"][0] and case["outcome"] == "passed"
    assert (folder / case["log"]).is_file() and _summary(folder) == {"HC-000": "PASS"}
    st, html, ctype = c.req("GET", f"/results/sim/{sid}/report.html", raw=True)
    assert st == 200 and b"sim session" in html and b"HC-000" in html
    # the session's zip download (the folder lives in results_dir)
    st, data, ctype = c.req("GET", f"/api/racks/sim/results/{sid}/zip", raw=True)
    assert st == 200 and ctype == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert f"{sid}/session.json" in names and f"{sid}/{case['log']}" in names and f"{sid}/report.html" in names
    # the plain run list does not repeat the session folder; the runs are still there
    _, hist = c.req("GET", "/api/racks/sim/history")
    assert sid not in [h["name"] for h in hist] and set(da["results"] + db["results"]) <= {h["name"] for h in hist}
    # rebuild on demand
    (folder / "report.html").unlink()
    st, r = c.req("POST", f"/api/racks/sim/sessions/{sid}/rebuild", {})
    assert st == 200 and r["summary"] == "1 case: 1 PASS" and (folder / "report.html").is_file()
    # a new session becomes current; the next run joins it
    st, n = c.req("POST", "/api/racks/sim/sessions", {"name": "second try"})
    assert st == 201 and n["name"] == "second try" and n["id"] != sid
    time.sleep(1.1)
    _, e = c.req("POST", "/api/runs", req)
    de = c.wait(e["id"])
    assert de["session"] == n["id"]
    _, d = c.req("GET", "/api/racks/sim/sessions")
    assert d["current"] == n["id"] and [x["summary"] for x in d["sessions"]] == ["1 case: 1 SKIP", "1 case: 1 PASS"]
    for x in results.rglob("*"):                        # the password is in no session file either
        if x.is_file():
            assert PW.encode() not in x.read_bytes(), x


def test_session_api_errors_and_csrf(ui):
    c, _, _ = ui()
    st, d = c.req("POST", "/api/racks/sim/sessions", {"name": "bad/name"})
    assert st == 400 and "session name" in d["error"]
    assert c.req("POST", "/api/racks/sim/sessions", {"name": "x"}, headers={"X-Helios-UI": "0"})[0] == 403
    assert c.req("POST", "/api/racks/sim/sessions", {"name": "x"}, headers={"Content-Type": "text/plain"})[0] == 403
    assert c.req("POST", "/api/racks/sim/sessions", {"name": "x"}, headers={"Host": "evil.example"})[0] == 403
    assert c.req("GET", "/api/racks/nope/sessions")[0] == 404
    assert c.req("POST", "/api/racks/sim/sessions/sim-20261016-100000/rebuild", {})[0] == 404      # not a session
    assert c.req("POST", "/api/racks/sim/sessions/sim-session-20990101-000000/rebuild", {})[0] == 404
    st, n = c.req("POST", "/api/racks/sim/sessions", {})                                          # default name
    assert st == 201 and n["name"]
    st, d = c.req("POST", f"/api/racks/sim/sessions/{n['id']}/rebuild", {})
    assert st == 400 and "no results" in d["error"]


def test_session_folder_is_not_a_new_run_result(tmp_path):
    """The runner finds a run's new results folders by listing <rack>-*: a session folder is not one of them."""
    from ui.runner import _result_dirs
    (tmp_path / "selftest-20261016-100000").mkdir()
    (tmp_path / "selftest-session-20261016-100000").mkdir()
    assert _result_dirs(tmp_path, "selftest") == {"selftest-20261016-100000"}


def test_the_suggested_default_name_is_accepted():
    """The page suggests '16 Oct 21:58'; that name (with ':') must pass the same check the server applies."""
    from ui.sessions import check_name, default_name
    assert check_name(default_name()) == default_name()
    assert check_name("16 Oct 21:58") == "16 Oct 21:58"
