"""UI test-plan pick: GET /api/racks/<rack>/plan (dropdown values + cases, alias mapping, automation, suite, layout
check, cache per workbook mtime) and POST .../plan/select, which must match tools/select_cases.select() exactly."""
from __future__ import annotations

import http.client
import itertools
import json
import os
import sys
import threading
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import select_cases as sc  # noqa: E402

from ui import plan  # noqa: E402
from ui.config import RackProfile  # noqa: E402
from ui.runner import JobError, clean_filters  # noqa: E402
from ui.server import App, make_server  # noqa: E402

HEADER = ["TID", "Level", "Title", "Automaton(Yes/No)", "AMD Help Needed?", "Execution Status", "SW Version",
          "Execution Notes", "Access needed", "Scope", "Origin", "Disruptive", "Health check"]


def _r(tid, level, title, status, scope, origin, disr, health=None):
    return [tid, level, title, "Yes", None, status, None, None, None, scope, origin, disr, health]


ROWS = [
    _r("HC-001", "L1", "SSH", "PASS", "Health check (Compute)", "Added", "Non-disruptive", "Blocker"),
    _r("HC-005", "L1", "kernel", None, "Health check (Compute)", "Added", "Non-disruptive", "Warning"),
    _r("TC-SWH-002", "l1", "switch hw", "FAIL", "Switch", "BKC-26.12 plan", "Non-disruptive"),
    _r("TC-AFM-005", "L2", "Failover Policy CRUD", "PASS", "Switch (AFM API)", "BKC-26.12 plan", "Non-disruptive"),
    _r("TC-AFM-005", "L2", "RCCL Cross Domain traffic", None, "Switch + Compute", "BKC-26.12 plan", "Disruptive"),
    _r("TC-AFM-048", "L3", "Techsupport bundle", "PARTIAL", "Switch (AFM API)", "BKC-26.12 plan", "Non-disruptive"),
    _r("TC-AFM-048", "L3", "Audit Events log", "PASS", "Switch (AFM API)", "BKC-26.12 plan", "Non-disruptive"),
    _r(None, "L2", "Switch Decommision/Delete/Drain Not allowed in Minirack", None, "Switch (AFM API)",
       "BKC-26.12 plan", "Disruptive"),
    _r("TC-NOPE-999", "L3", "not automated yet", None, "Switch", "BKC-26.12 plan", "Disruptive"),
    _r("HC-000", "L1", "afm health", "PASS", "Health check (Switch + Compute, AFM API)", "Added", "Non-disruptive",
       "Blocker"),
    ["Notes", None, "a free-text row"],
]


def _wb(path: Path, header=HEADER, rows=ROWS) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Testcases"
    ws.append(["BKC-26.12"])
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def _profile(tmp_path, workbook, name="r"):
    return RackProfile(name=name, title=name, runner="local", inventory=tmp_path / "inv.yaml",
                       results_dir=tmp_path / "results", workbook=workbook)


class Client:
    def __init__(self, port):
        self.port = port

    def j(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "X-Helios-UI": "1"})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        st, data = r.status, r.read()
        c.close()
        return st, json.loads(data or b"null")


@pytest.fixture
def ui(tmp_path):
    plan.clear_cache()
    wb = _wb(tmp_path / "plan.xlsx")
    other = _wb(tmp_path / "other.xlsx", header=HEADER[:6], rows=[r[:6] for r in ROWS[:2]])
    profiles = {"r": _profile(tmp_path, wb, "r"), "none": _profile(tmp_path, None, "none"),
                "missing": _profile(tmp_path, tmp_path / "gone.xlsx", "missing"),
                "other": _profile(tmp_path, other, "other")}
    app = App(profiles=profiles)
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Client(srv.server_address[1]), wb
    app.shutdown()
    srv.shutdown()
    srv.server_close()
    plan.clear_cache()


def test_plan_columns_and_cases(ui):
    c, _ = ui
    st, d = c.j("GET", "/api/racks/r/plan")
    assert st == 200, d
    assert d["workbook"] == "plan.xlsx"
    cols = d["columns"]
    assert cols["level"] == ["L1", "L2", "L3"]                 # "l1" and "L1" are one value (matching ignores case)
    assert cols["status"] == ["FAIL", "PARTIAL", "PASS", "(empty)"]     # blank cells last, as the "(empty)" value
    assert cols["health"] == ["Blocker", "Warning", "(empty)"]
    assert "Health check (Switch + Compute, AFM API)" in cols["scope"]
    assert cols["disruptive"] == ["Disruptive", "Non-disruptive"]
    assert set(cols) == {"level", "scope", "disruptive", "health", "origin", "status"}
    by = {x["id"]: x for x in d["cases"]}
    # shared TIDs map to their code IDs, the TID-less row to TC-AFM-035A; the free-text row is not a case
    assert [x["id"] for x in d["cases"]] == ["HC-001", "HC-005", "TC-SWH-002", "TC-AFM-005", "TC-AFM-005A",
                                             "TC-AFM-048", "TC-AFM-048A", "TC-AFM-035A", "TC-NOPE-999", "HC-000"]
    assert by["TC-AFM-005A"]["title"] == "RCCL Cross Domain traffic" and by["TC-AFM-005A"]["disruptive"] == "Disruptive"
    assert by["TC-AFM-048A"]["title"] == "Audit Events log"
    assert by["TC-AFM-005"]["suite"] == "afm" and by["TC-AFM-005A"]["suite"] == "ssh"
    assert by["TC-AFM-035A"]["automated"] and by["TC-AFM-035A"]["suite"] == "afm"
    assert by["HC-001"] == {"id": "HC-001", "title": "SSH", "level": "L1", "scope": "Health check (Compute)",
                            "disruptive": "Non-disruptive", "health": "Blocker", "origin": "Added", "status": "PASS",
                            "automated": True, "suite": "ssh"}
    assert by["HC-005"]["status"] == ""
    assert by["TC-NOPE-999"]["automated"] is False and by["TC-NOPE-999"]["suite"] is None


def test_plan_errors(ui):
    c, _ = ui
    st, d = c.j("GET", "/api/racks/nosuch/plan")
    assert st == 404 and "unknown rack" in d["error"]
    st, d = c.j("GET", "/api/racks/none/plan")
    assert st == 400 and "no test-plan workbook for none: upload one" in d["error"]
    st, d = c.j("GET", "/api/racks/missing/plan")
    assert st == 400 and "workbook not found" in d["error"]
    st, d = c.j("GET", "/api/racks/other/plan")
    assert st == 400 and "not the full-rack test-plan layout" in d["error"] and "column 10" in d["error"]
    st, d = c.j("POST", "/api/racks/other/plan/select", {"filters": {"level": "L1"}})
    assert st == 400 and "not the full-rack test-plan layout" in d["error"]
    st, d = c.j("POST", "/api/racks/nosuch/plan/select", {"filters": {}})
    assert st == 404
    st, d = c.j("POST", "/api/racks/r/plan/select", {"filters": {"evil": "x"}})
    assert st == 400 and "unknown filter" in d["error"]


def test_plan_cache_follows_the_workbook_mtime(ui, tmp_path):
    c, wb = ui
    calls = []
    real = sc.plan_rows

    def counting(p):
        calls.append(p)
        return real(p)
    sc.plan_rows, saved = counting, sc.plan_rows
    try:
        assert c.j("GET", "/api/racks/r/plan")[0] == 200
        assert c.j("GET", "/api/racks/r/plan")[0] == 200
        assert c.j("POST", "/api/racks/r/plan/select", {"filters": {}})[0] == 200
        assert len(calls) == 1                                   # read once
        st0 = wb.stat()
        _wb(wb, rows=ROWS[:1] + [_r("TC-SWH-002", "L1", "switch hw", "PASS", "Switch", "x", "Non-disruptive")])
        os.utime(wb, ns=(st0.st_atime_ns, st0.st_mtime_ns + 5_000_000_000))
        st, d = c.j("GET", "/api/racks/r/plan")
        assert len(calls) == 2 and [x["id"] for x in d["cases"]] == ["HC-001", "TC-SWH-002"]
        assert d["columns"]["status"] == ["PASS"]
    finally:
        sc.plan_rows = saved


FILTER_SETS = [
    {}, {"level": "L1"}, {"level": "l1,L3"}, {"scope": "Switch,Switch (AFM API)"},
    {"scope": "Health check (Switch + Compute, AFM API)"}, {"disruptive": "Non-disruptive", "health": "Blocker"},
    {"status": "(empty)"}, {"status": "(empty),fail"}, {"health": "(EMPTY)", "level": "L2"},
    {"origin": "added"}, {"origin": "nothing like it"},
]


@pytest.mark.parametrize("suite,filters", list(itertools.product(("all", "afm", "ssh"), FILTER_SETS)))
def test_pick_matches_select_cases(ui, tmp_path, suite, filters):
    c, wb = ui
    want = sc.select(wb, suite, **filters)
    st, d = c.j("POST", "/api/racks/r/plan/select", {"filters": {**filters, "suite": suite}})
    assert st == 200, d
    assert d["tc"] == want and d["workbook"] == "plan.xlsx"
    # the same as lists (the UI's multi-selects)
    lists = {k: sc.split_values(v) for k, v in filters.items()}
    assert c.j("POST", "/api/racks/r/plan/select", {"filters": {**lists, "suite": suite}})[1]["tc"] == want


def test_pick_counts_matching_cases_without_a_test(ui):
    c, _ = ui
    d = c.j("POST", "/api/racks/r/plan/select", {"filters": {"level": "L3"}})[1]
    assert d["tc"] == ["TC-AFM-048", "TC-AFM-048A"] and d["not_automated"] == 1
    d = c.j("POST", "/api/racks/r/plan/select", {"filters": {"level": "L2", "suite": "afm"}})[1]
    assert d["tc"] == ["TC-AFM-005", "TC-AFM-035A"] and d["not_automated"] == 0


def test_select_value_rules(tmp_path):
    wb = _wb(tmp_path / "plan.xlsx")
    assert sc.split_values("Switch, Switch (AFM API),,") == ["Switch", "Switch (AFM API)"]
    assert sc.split_values("Health check (Switch + Compute, AFM API),Compute") == [
        "Health check (Switch + Compute, AFM API)", "Compute"]
    assert sc.split_values(["a", " ", "b "]) == ["a", "b"]
    assert sc.select(wb, "all", scope="Health check (Switch + Compute, AFM API)") == ["HC-000"]
    assert sc.select(wb, "all", status="(empty)") == ["HC-005", "TC-AFM-005A", "TC-AFM-035A"]
    assert "TC-AFM-035A" in sc.select(wb, "afm")                # the TID-less row of the plan


def test_clean_filters_accepts_lists():
    assert clean_filters({"level": ["L1", " L2 ", ""], "scope": ["Switch", "Switch (AFM API)"], "suite": "afm",
                          "status": []}) == {"level": "L1,L2", "scope": "Switch,Switch (AFM API)", "suite": "afm"}
    assert clean_filters({"level": "L1,L2"}) == {"level": "L1,L2"}                 # old presets / run records
    with pytest.raises(JobError, match="list of text"):
        clean_filters({"level": ["L1", 2]})
    with pytest.raises(JobError, match="one line"):
        clean_filters({"level": ["a\nb"]})
    with pytest.raises(JobError, match="one line"):
        clean_filters({"suite": ["afm"]})
    with pytest.raises(JobError, match="suite"):
        clean_filters({"suite": "everything"})


def test_preset_stores_list_filters_as_strings():
    from ui import presets
    s = presets.normalize({"kind": "pytest", "tc": "TC-SWH-002", "filters": {"level": ["L1", "L2"], "suite": "ssh"}})
    assert s["filters"] == {"level": "L1,L2", "suite": "ssh"}


def test_any_column_filter_and_scope_disruptive_status_together():
    """20 Oct: run cases by scope / disruptive / last status (the fixed filters) and filter on ANY workbook column by
    its header (values, or 'contains' for free-text columns)."""
    import json as _json
    from ui import plan as P
    rows = [{"id": "TC-1", "title": "link flap", "level": "L1", "status": "FAIL", "scope": "Switch", "origin": "",
             "disruptive": "Yes", "health": "", "cells": {"Automaton(Yes/No)": "Yes", "Title": "link flap", "Notes": "ct05 down"}},
            {"id": "TC-2", "title": "bmc power", "level": "L1", "status": "", "scope": "Compute", "origin": "",
             "disruptive": "", "health": "", "cells": {"Automaton(Yes/No)": "No", "Title": "bmc power", "Notes": ""}}]
    cols = {c["name"]: c for c in P.any_columns(rows)}
    assert cols["Automaton(Yes/No)"]["values"] == ["No", "Yes"]
    rules = P.parse_cols(_json.dumps([{"col": "Automaton(Yes/No)", "values": ["yes"]}, {"col": "Notes", "contains": "CT05"}]))
    assert [r["id"] for r in rows if P.match_cols(r, rules)] == ["TC-1"]
    assert [r["id"] for r in rows if P.match_cols(r, P.parse_cols('[{"col": "Notes", "values": ["(empty)"]}]'))] == ["TC-2"]
    assert P.parse_cols('[{"col": "Notes", "values": []}, {"col": "Title", "contains": " "}]') == []     # empty = no filter
    with pytest.raises(JobError):
        P.parse_cols('{"col": "x"}')
    with pytest.raises(JobError):
        P.parse_cols('[{"col": "x", "values": "Yes"}]')
    from ui.runner import clean_filters
    assert clean_filters({"disruptive": ["Yes"], "status": ["FAIL", "(empty)"], "scope": "Switch",
                          "cols": '[{"col": "Notes", "contains": "ct05"}]'})["cols"].startswith("[")
