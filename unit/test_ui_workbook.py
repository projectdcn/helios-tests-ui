"""UI "test plan pick": select_cases reads only the rack's configured workbook and refuses another layout."""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from select_cases import WorkbookLayoutError, select  # noqa: E402

from ui.config import RackProfile  # noqa: E402
from ui.runner import JobError  # noqa: E402
from ui.server import select_cases  # noqa: E402

HEADER = ["TID", "Level", "Title", "Automaton(Yes/No)", "AMD Help Needed?", "Execution Status", "SW Version",
          "Execution Notes", "Access needed", "Scope", "Origin", "Disruptive", "Health check"]


def _wb(path: Path, header=HEADER, rows=()):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Testcases"
    ws.append(["BKC-26.12"])
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


ROWS = [["HC-001", "L1", "SSH", "Yes", None, "PASS", None, None, None, "Health check (Compute)", "Added", "Non-disruptive", "Blocker"],
        ["HC-005", "L1", "kernel", "Yes", None, "PASS", None, None, None, "Health check (Compute)", "Added", "Non-disruptive", "Warning"]]


def test_select_filters_on_the_full_rack_layout(tmp_path):
    wb = _wb(tmp_path / "plan.xlsx", rows=ROWS)
    assert select(wb, "all", health="Blocker") == ["HC-001"]
    assert sorted(select(wb, "all", scope="Health check (Compute)")) == ["HC-001", "HC-005"]


def test_select_refuses_another_layout(tmp_path):
    wb = _wb(tmp_path / "other.xlsx", header=HEADER[:6], rows=[r[:6] for r in ROWS])   # no Scope/Origin/... columns
    with pytest.raises(WorkbookLayoutError, match="column 10: expected 'scope'"):
        select(wb, "all", scope="Compute")


def _profile(tmp_path, workbook):
    return RackProfile(name="r", title="r", runner="local", inventory=tmp_path / "inv.yaml",
                       results_dir=tmp_path / "results", workbook=workbook)


def test_ui_uses_only_the_racks_workbook(tmp_path):
    with pytest.raises(JobError, match="no test-plan workbook for r: upload one"):
        select_cases({"health": "Blocker"}, _profile(tmp_path, None))
    with pytest.raises(JobError, match="workbook not found"):
        select_cases({}, _profile(tmp_path, tmp_path / "missing.xlsx"))
    wb = _wb(tmp_path / "plan.xlsx", rows=ROWS)
    assert select_cases({"health": "Blocker", "workbook": "/etc/passwd"}, _profile(tmp_path, wb)) == ["HC-001"]


def test_ui_reports_a_wrong_layout(tmp_path):
    wb = _wb(tmp_path / "other.xlsx", header=HEADER[:6], rows=[r[:6] for r in ROWS])
    with pytest.raises(JobError, match="not the full-rack test-plan layout"):
        select_cases({"scope": "Compute"}, _profile(tmp_path, wb))
