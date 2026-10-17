"""Live progress at INFO: test start line, heartbeat while a test runs, 'waiting: ...' from framework waits."""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from helios import progress  # noqa: E402
from helios.afm import wait_for  # noqa: E402
from helios.wait import wait_until  # noqa: E402


def test_waits_report_every_interval(caplog, monkeypatch):
    monkeypatch.setattr(progress, "WAIT_EVERY_S", 0.05)
    caplog.set_level(logging.INFO, logger="helios.progress")
    n = {"i": 0}

    def later():
        n["i"] += 1
        return n["i"] > 6
    assert wait_for(later, 5, 0.02, "AFM link-down event")
    assert any("waiting: AFM link-down event (" in r.getMessage() and "/5 s)" in r.getMessage() for r in caplog.records)
    caplog.clear()
    n["i"] = 0
    ok, _ = wait_until(later, 5, 0.02, what="ct05 answers SSH")
    assert ok and any("waiting: ct05 answers SSH" in r.getMessage() for r in caplog.records)


def test_heartbeat(caplog):
    caplog.set_level(logging.INFO, logger="helios.progress")
    with progress.Heartbeat("tests/x.py::test_y", every=0.05):
        time.sleep(0.18)
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("still running tests/x.py::test_y (" in m for m in msgs) >= 2


def test_start_line_in_live_output(tmp_path):
    r = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "tests" / "health" / "test_rack_health.py"),
                        "--inventory", str(ROOT / "selftest" / "inventory.yaml"), "--tc", "HC-001",
                        "--results-dir", str(tmp_path), "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    assert "start tests/health/test_rack_health.py::test_every_node_answers_ssh [HC-001]" in r.stdout, r.stdout[-2000:]
