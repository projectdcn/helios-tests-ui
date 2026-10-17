"""tools/results_retention.py: logs compressed then deleted by age, newest N runs and protected runs untouched,
results never touched, UI live logs capped."""
from __future__ import annotations

import sys
import tarfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import results_retention as rr  # noqa: E402

NOW = datetime(2027, 1, 31, 12, 0, 0)


def _run(root, rack, days_ago, merged=False):
    name = f"{rack}-{(NOW - timedelta(days=days_ago)).strftime('%Y%m%d-%H%M%S')}" + ("-merged" if merged else "")
    d = root / "runs" / rack / name
    (d / "logs").mkdir(parents=True)
    (d / "logs" / "001_test.log").write_text("x" * 1000)
    (d / "tc_results.csv").write_text("tc_id\n")
    (d / "tc_summary.csv").write_text("tc_id\n")
    return d


def test_age_rules_newest_and_protected(tmp_path):
    (tmp_path / "retention.yaml").write_text("keep_full_runs: 2\nkeep_full_days: 30\ndelete_logs_days: 90\n")
    new1, new2 = _run(tmp_path, "mr3", 1), _run(tmp_path, "mr3", 2)
    mid, old = _run(tmp_path, "mr3", 45), _run(tmp_path, "mr3", 120)
    listed = _run(tmp_path, "mr3", 200)                     # in a report run list
    src = _run(tmp_path, "mr3", 150)                        # source of a merged result
    m = _run(tmp_path, "mr3", 150, merged=True)
    (m / "merged-from.txt").write_text(src.name + "\n")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "x-runs.txt").write_text(f"results/runs/mr3/{listed.name}\n")
    acts = rr.plan(tmp_path, now=NOW.timestamp())
    got = {(a, p.parent.name) for a, p, _ in acts}
    assert got == {("compress-logs", mid.name), ("delete-logs", old.name)}, got
    rr.apply(acts)
    assert not (mid / "logs").exists() and (mid / "logs.tar.gz").is_file()
    with tarfile.open(mid / "logs.tar.gz") as t:
        assert t.getnames() == ["logs", "logs/001_test.log"]
    assert not (old / "logs").exists()
    for d in (new1, new2, listed, src, m):                  # untouched
        assert (d / "logs" / "001_test.log").is_file()
    for d in (new1, new2, mid, old, listed, src, m):        # results always kept
        assert (d / "tc_results.csv").is_file() and (d / "tc_summary.csv").is_file()


def test_archive_deleted_after_limit_and_ui_logs_capped(tmp_path):
    (tmp_path / "retention.yaml").write_text("keep_full_runs: 0\nui_logs_per_rack: 2\n")
    d = _run(tmp_path, "g02-g17", 100)
    import shutil
    with tarfile.open(d / "logs.tar.gz", "w:gz") as t:
        t.add(d / "logs", arcname="logs")
    shutil.rmtree(d / "logs")
    ui = tmp_path / "runs" / "g02-g17" / "ui-runs"
    ui.mkdir()
    for i in range(4):
        f = ui / f"r{i}.log"
        f.write_text("log")
        (ui / f"r{i}.json").write_text("{}")
        t0 = time.time() - (10 - i) * 60
        import os
        os.utime(f, (t0, t0))
    acts = rr.plan(tmp_path, now=NOW.timestamp())
    assert {(a, p.name) for a, p, _ in acts} == {("delete-archive", "logs.tar.gz"), ("delete-ui-log", "r0.log"),
                                                 ("delete-ui-log", "r1.log")}
    rr.apply(acts)
    assert sorted(p.name for p in ui.iterdir()) == ["r0.json", "r1.json", "r2.json", "r2.log", "r3.json", "r3.log"]


def test_dry_run_changes_nothing(tmp_path):
    (tmp_path / "retention.yaml").write_text("keep_full_runs: 0\n")
    d = _run(tmp_path, "mr3", 200)
    rr.main(["--results", str(tmp_path)])
    assert (d / "logs" / "001_test.log").is_file()
