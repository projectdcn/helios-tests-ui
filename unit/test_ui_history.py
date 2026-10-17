"""Case history tab: GET /api/cases/<tc>/history reads results/index.csv across racks."""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import results_index as ri  # noqa: E402

from ui.config import RackProfile  # noqa: E402
from ui.runner import JobError, index_runs  # noqa: E402
from ui.server import case_history  # noqa: E402

HDR = ["tc_id", "nodeid", "suite", "test", "description", "outcome", "duration_s", "properties", "message", "started", "log"]


def _run(root, rack, name, tc, outcome, started):
    d = root / "runs" / rack / name
    (d / "logs").mkdir(parents=True)
    with open(d / "tc_results.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HDR)
        w.writerow([tc, f"x::{tc}", "s", "test_x", "", outcome, 1, "", "m", started, "logs/001_test_x.log"])
    return d


def _app(root):
    prof = lambda n: RackProfile(name=n, title=n, runner="local", inventory=root / f"{n}.yaml",  # noqa: E731
                                 results_dir=root / "runs" / n)
    return SimpleNamespace(profiles={"mr3": prof("mr3"), "g02-g17": prof("g02-g17")})


def test_history_across_racks_with_links(tmp_path):
    _run(tmp_path, "g02-g17", "g02-g17-20261016-133000", "TC-AFM-001", "failed", "2026-10-16 13:30:12 UTC")
    d = _run(tmp_path, "mr3", "mr3-20261016-163941", "TC-AFM-001", "passed", "2026-10-16 23:37:37 UTC")
    ri.rebuild(tmp_path)
    out = case_history(_app(tmp_path), "TC-AFM-001")
    assert [(r["rack"], r["outcome"]) for r in out["rows"]] == [("mr3", "passed"), ("g02-g17", "failed")]
    assert out["rows"][0]["log_url"] == f"/results/mr3/{d.name}/logs/001_test_x.log"
    assert out["rows"][0]["report_url"] == f"/results/mr3/{d.name}/report.html"
    assert case_history(_app(tmp_path), "HC-404")["rows"] == []
    with pytest.raises(JobError):
        case_history(_app(tmp_path), "../etc")


def test_copied_back_runs_are_indexed(tmp_path):
    d = _run(tmp_path, "g02-g17", "g02-g17-20261017-010101", "HC-001", "passed", "2026-10-17 01:01:01 UTC")
    index_runs([d])
    assert [r["run"] for r in ri.read(tmp_path)] == [d.name]


def test_retention_applied_after_a_run(tmp_path):
    """The UI applies results/retention.yaml after each run (old UI live logs beyond the cap are removed)."""
    import os
    import time
    from ui.runs import RunManager
    ui = tmp_path / "runs" / "mr3" / "ui-runs"
    ui.mkdir(parents=True)
    (tmp_path / "retention.yaml").write_text("ui_logs_per_rack: 1\n")
    for i in range(3):
        f = ui / f"old{i}.log"
        f.write_text("x")
        t0 = time.time() - (100 - i) * 60
        os.utime(f, (t0, t0))
    mgr = RunManager.__new__(RunManager)
    mgr.profiles = {"mr3": _app(tmp_path).profiles["mr3"]}
    lines = []
    mgr._retention(SimpleNamespace(rack="mr3"), lines.append)
    assert sorted(p.name for p in ui.glob("*.log")) == ["old2.log"] and "retention: 2 action(s)" in lines[0]
