"""tools/select_cases.py refuses a workbook whose Testcases columns are not the full-rack layout."""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from select_cases import WorkbookLayoutError, select  # noqa: E402

HEADER = ["TID", "Level", "Title", "Automaton(Yes/No)", "AMD Help Needed?", "Execution Status", "SW Version",
          "Execution Notes", "Access needed", "Scope", "Origin", "Disruptive", "Health check"]
ROWS = [["HC-001", "L1", "SSH", "Yes", None, "PASS", None, None, None, "Health check (Compute)", "Added", "Non-disruptive", "Blocker"],
        ["HC-005", "L1", "kernel", "Yes", None, "PASS", None, None, None, "Health check (Compute)", "Added", "Non-disruptive", "Warning"]]


def _wb(path: Path, header, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Testcases"
    ws.append(["BKC-26.12"])
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def test_full_rack_layout_filters(tmp_path):
    wb = _wb(tmp_path / "plan.xlsx", HEADER, ROWS)
    assert select(wb, "all", health="Blocker") == ["HC-001"]


def test_other_layout_is_refused(tmp_path):
    wb = _wb(tmp_path / "other.xlsx", HEADER[:6], [r[:6] for r in ROWS])
    with pytest.raises(WorkbookLayoutError, match="column 10: expected 'scope'"):
        select(wb, "all", scope="Compute")
