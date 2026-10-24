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


def test_scope_tags_match_one_tag_or_the_whole_cell(tmp_path):
    """Scope "Switch + Compute; IFCP" is both: --scope IFCP and --scope "Switch + Compute" each find it."""
    from select_cases import matches, tags, wanted
    assert tags("Switch + Compute; IFCP") == ["Switch + Compute", "IFCP"] and tags("Compute") == ["Compute"]
    row = {"scope": "Switch + Compute; IFCP"}
    assert matches(row, wanted({"scope": "IFCP"})) and matches(row, wanted({"scope": "switch + compute"}))
    assert matches(row, wanted({"scope": "Switch + Compute; IFCP"})) and not matches(row, wanted({"scope": "Compute"}))
    assert matches({"scope": ""}, wanted({"scope": "(empty)"})) and not matches(row, wanted({"scope": "(empty)"}))
    wb = _wb(tmp_path / "plan.xlsx", HEADER, ROWS + [["TC-001", "L3", "Baseline", "Yes", None, "PASS", None, None,
                                                    None, "Compute; IFCP", "AMD", "Non-disruptive", None]])
    assert select(wb, "all", scope="IFCP") == ["TC-001"]
    assert select(wb, "all", scope="Compute") == ["TC-001"]


def test_tag_scope_adds_once_removes_and_respects_the_excel_lock(tmp_path):
    import tag_scope
    assert tag_scope.retag("Switch + Compute", "IFCP", False) == "Switch + Compute; IFCP"
    assert tag_scope.retag("Switch + Compute; IFCP", "ifcp", False) == "Switch + Compute; IFCP"   # once
    assert tag_scope.retag("Switch + Compute; IFCP", "IFCP", True) == "Switch + Compute"
    assert tag_scope.retag(None, "IFCP", False) == "IFCP"
    wb = _wb(tmp_path / "plan.xlsx", HEADER, ROWS)
    (tmp_path / "c.txt").write_text("# hc\nHC-001 HC-404\n")
    (tmp_path / "~$plan.xlsx").write_text("lock")
    assert tag_scope.main([str(wb), "--tag", "IFCP", "--cases", str(tmp_path / "c.txt")]) == 2      # open in Excel
    (tmp_path / "~$plan.xlsx").unlink()
    assert tag_scope.main([str(wb), "--tag", "IFCP", "--cases", str(tmp_path / "c.txt"), "--no-backup"]) == 1  # HC-404
    assert select(wb, "all", scope="IFCP") == ["HC-001"]
    assert sorted(select(wb, "all", scope="Health check (Compute)")) == ["HC-001", "HC-005"]
