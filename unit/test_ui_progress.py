"""Live progress in the UI: a partial line (pytest's 'path::test ' before PASSED) is shown after ~1 s; the running
test is tracked from helios.progress 'start <nodeid>' lines."""
from __future__ import annotations

import subprocess
import sys
import time
from types import SimpleNamespace

from ui.runner import _stream
from ui.runs import RunManager

SCRIPT = ("import sys, time\n"
          "print('collected 1 item', flush=True)\n"
          "sys.stdout.write('tests/afm/test_x.py::test_flap '); sys.stdout.flush()\n"
          "time.sleep(2.5)\n"
          "print('PASSED [100%]', flush=True)\n")


def test_partial_line_shown_before_its_end():
    p = subprocess.Popen([sys.executable, "-c", SCRIPT], stdout=subprocess.PIPE)
    seen = []
    t0 = time.monotonic()
    rc = _stream(p, lambda line: seen.append((round(time.monotonic() - t0, 1), line)), partial_after=0.5)
    assert rc == 0
    lines = [l for _, l in seen]
    assert lines == ["collected 1 item", "tests/afm/test_x.py::test_flap ", "PASSED [100%]"], seen
    partial_at = next(t for t, l in seen if l.startswith("tests/afm"))
    assert partial_at < 2.0                       # shown while the test still ran, not at its end (2.5 s)


def test_current_test_tracked(tmp_path):
    from ui.runner import Job
    from ui.runs import Run
    run = Run("r1", "mr3", Job.from_request({"kind": "pytest", "tc": "TC-AFM-009"}), None, tmp_path)
    run.status = "running"
    mgr = RunManager.__new__(RunManager)
    with open(tmp_path / "x.log", "w") as fh:
        mgr._log(run, fh, "20:42:15 INFO start tests/afm/test_afm_compute.py::test_compute_link_flap [TC-AFM-009]")
    assert run.as_dict()["current"] == "tests/afm/test_afm_compute.py::test_compute_link_flap"
    run.status = "finished"
    assert run.as_dict()["current"] is None
