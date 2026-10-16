"""tools/html_report.py: case IDs in the embedded health tab link to their row in the Test report tab."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from html_report import TAB_JS, link_ids  # noqa: E402


def test_link_ids_links_only_known_ids_and_escapes():
    out = link_ids("TC-AFM-001 <fail> on ct03; HC-001 and TC-NOPE-9", {"TC-AFM-001", "HC-001"})
    assert '<a href="#report:case-TC-AFM-001">TC-AFM-001</a>' in out
    assert '<a href="#report:case-HC-001">HC-001</a>' in out
    assert "TC-NOPE-9" in out and "#report:case-TC-NOPE-9" not in out
    assert "&lt;fail&gt;" in out


def test_link_ids_without_ids_is_plain_escaped_text():
    assert link_ids("TC-AFM-001 <x>", None) == "TC-AFM-001 &lt;x&gt;"


def test_tab_script_handles_report_links():
    assert "#report:" in TAB_JS and "showTab('report')" in TAB_JS
