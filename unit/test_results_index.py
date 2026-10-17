"""results/index.csv (tools/results_index.py) and the default results/runs/<rack>/ location of new runs."""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import results_index as ri  # noqa: E402

HDR = ["tc_id", "nodeid", "suite", "test", "description", "outcome", "duration_s", "properties", "message", "started", "log"]


def _run(root: Path, rack: str, name: str, rows):
    d = root / "runs" / rack / name
    (d / "logs").mkdir(parents=True)
    with open(d / "tc_results.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HDR)
        for tc, out, started in rows:
            w.writerow([tc, f"x::{tc}", "s", f"test_{tc}", "", out, 1, "", f"msg {tc}\n- detail", started, f"logs/{tc}.log"])
    return d


def test_rebuild_history_runs_and_merged_excluded(tmp_path):
    _run(tmp_path, "g02-g17", "g02-g17-20261014-100000", [("TC-AFM-001", "failed", "2026-10-14 10:00:00")])
    _run(tmp_path, "g02-g17", "g02-g17-20261016-133000", [("TC-AFM-001", "failed", "2026-10-16 13:30:12 UTC"),
                                                          ("HC-001", "passed", "2026-10-16 13:31:00 UTC")])
    _run(tmp_path, "g02-g17", "g02-g17-20261016-133000-merged", [("TC-AFM-001", "failed", "2026-10-16 13:30:12 UTC")])
    _run(tmp_path, "mr3", "mr3-20261016-160000", [("TC-AFM-001", "passed", "2026-10-16 16:00:00 UTC")])
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "x-tz-args.txt").write_text("--tz g02-g17-20261014-100000=-7\n")
    assert ri.rebuild(tmp_path) == 4                          # the -merged copy is not indexed
    h = ri.history("TC-AFM-001", tmp_path)
    assert [(r["rack"], r["run"]) for r in h] == [("mr3", "mr3-20261016-160000"), ("g02-g17", "g02-g17-20261016-133000"),
                                                  ("g02-g17", "g02-g17-20261014-100000")]
    assert h[-1]["started"] == "2026-10-14 17:00:00 UTC"      # PDT runner clock converted with the --tz list
    assert h[1]["log"] == "runs/g02-g17/g02-g17-20261016-133000/logs/TC-AFM-001.log"
    assert h[1]["message"] == "msg TC-AFM-001 - detail"
    assert [r["run"] for r in ri.runs(tmp_path, "g02-g17")] == ["g02-g17-20261016-133000", "g02-g17-20261014-100000"]


def test_update_run_replaces_that_run_only(tmp_path):
    a = _run(tmp_path, "mr3", "mr3-1", [("HC-001", "failed", "2026-10-16 10:00:00 UTC")])
    b = _run(tmp_path, "mr3", "mr3-2", [("HC-001", "passed", "2026-10-16 11:00:00 UTC")])
    assert ri.update_run(a) and ri.update_run(b) and ri.update_run(b)      # re-adding a run does not duplicate it
    assert [r["run"] for r in ri.read(tmp_path)] == ["mr3-1", "mr3-2"]
    assert not ri.update_run(tmp_path / "elsewhere")           # a folder outside runs/<rack>/ is not indexed


def test_a_run_without_results_dir_lands_in_runs_rack_and_is_indexed(tmp_path):
    r = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "tests" / "health" / "test_rack_health.py"),
                        "--inventory", str(ROOT / "selftest" / "inventory.yaml"), "--tc", "HC-001", "-q",
                        "-p", "no:cacheprovider", "--rootdir", str(ROOT)], cwd=tmp_path, capture_output=True, text=True,
                       timeout=300)
    runs = list((tmp_path / "results" / "runs").glob("*/*/tc_results.csv"))
    assert runs, r.stdout[-1500:] + r.stderr[-800:]
    rows = ri.read(tmp_path / "results")
    assert rows and rows[0]["tc_id"] == "HC-001" and rows[0]["run"] == runs[0].parent.name


def test_ui_session_folders_not_indexed(tmp_path):
    """<rack>-session-<time>/ (the UI's consolidated session result) repeats rows of its runs: not indexed."""
    _run(tmp_path, "mr3", "mr3-20261016-160000", [("HC-001", "passed", "2026-10-16 16:00:00 UTC")])
    s = _run(tmp_path, "mr3", "mr3-session-20261016-155900", [("HC-001", "passed", "2026-10-16 16:00:00 UTC")])
    assert ri.run_rows(s, "mr3") == []
    assert ri.rebuild(tmp_path) == 1 and [r["run"] for r in ri.read(tmp_path)] == ["mr3-20261016-160000"]
    assert ri.update_run(s) and [r["run"] for r in ri.read(tmp_path)] == ["mr3-20261016-160000"]
