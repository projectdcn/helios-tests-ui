"""tools/html_report.py on a run written before 13 Oct 2026 (MR3 12 Oct): tc_results.csv without suite / test /
description / started / log - the report builds, suite and test come from the nodeid."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_old_tc_results_layout_builds_a_report(tmp_path):
    run = tmp_path / "mr3-20261012-153952"
    run.mkdir()
    (run / "tc_results.csv").write_text(
        "tc_id,nodeid,outcome,duration_s,properties,message\n"
        "TC-SWH-001,tests/l1/test_switch_health.py::test_psu[st1],passed,1.2,{},\n"
        "TC-AFM-001,tests/afm/test_afm_compute.py::test_ping,failed,3.4,{},boom\n")
    (run / "tc_summary.csv").write_text("tc_id,verdict\nTC-SWH-001,PASS\nTC-AFM-001,FAIL\n")
    out = run / "report.html"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "html_report.py"), str(run), "-o", str(out), "--title", "old"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-800:]
    html = out.read_text()
    assert "l1/test_switch_health" in html and "afm/test_afm_compute" in html and "boom" in html
