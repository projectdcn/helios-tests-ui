"""Build a --tc list from the test-plan workbook's columns (Level, Scope, Disruptive, Origin, status) - e.g. every
non-disruptive automated case whose scope is the switch or the AFM API, or the health-check cases (--health
Blocker,Warning) - split into the AFM suite (needs the AFM password) and the SSH suites.

    python tools/select_cases.py testplan/full_rack_testplan.xlsx --disruptive Non-disruptive \\
        --scope "Switch,Switch (AFM API)" --suite afm -o results/afm-nondisruptive-tcs.txt
    .venv/bin/pytest tests/afm --inventory <inv> --tc "$(cat results/afm-nondisruptive-tcs.txt)"

Only automated cases are listed (a test with that plan ID exists). Filters are comma-separated and exact
(case-insensitive); --suite: afm (tests/afm), ssh (everything else), all.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from access_needed import automated  # noqa: E402
from update_testplan import ROW_ALIASES  # noqa: E402

COLS = {"level": 2, "status": 6, "scope": 10, "origin": 11, "disruptive": 12, "health": 13}
# header (row 2 of the Testcases sheet) each column must start with - full_rack_testplan.xlsx layout
HEADERS = {1: "tid", 2: "level", 6: "execution status", 10: "scope", 11: "origin", 12: "disruptive", 13: "health check"}


class WorkbookLayoutError(ValueError):
    pass


def check_layout(ws) -> None:
    """Refuse a workbook whose Testcases columns are not where COLS expects them (another plan layout would
    silently filter on the wrong columns)."""
    hdr = next(ws.iter_rows(min_row=2, max_row=2, values_only=True), ())
    bad = [f"column {c}: expected '{want}', found '{(hdr[c - 1] if c <= len(hdr) else None) or ''}'"
           for c, want in HEADERS.items()
           if not str((hdr[c - 1] if c <= len(hdr) else None) or "").strip().lower().startswith(want)]
    if bad:
        raise WorkbookLayoutError("not the full-rack test-plan layout (Testcases row 2): " + "; ".join(bad))


def select(wb_path: Path, suite: str = "all", **filters) -> list[str]:
    auto = automated()
    wb = openpyxl.load_workbook(wb_path, read_only=True)
    if "Testcases" not in wb.sheetnames:
        raise WorkbookLayoutError("no 'Testcases' sheet")
    ws = wb["Testcases"]
    check_layout(ws)
    want = {k: {x.strip().lower() for x in v.split(",") if x.strip()} for k, v in filters.items() if v}
    out = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        tid = row[0]
        if not tid or not str(tid).startswith(("TC-", "HC-")):
            continue
        vals = {k: str((row[c - 1] if c <= len(row) else None) or "").strip().lower() for k, c in COLS.items()}
        if any(vals[k] not in v for k, v in want.items()):
            continue
        # the row's code ID (shared TIDs: TC-AFM-005 RCCL row = TC-AFM-005A, ...)
        code = next((t for t, (a, frag) in ROW_ALIASES.items() if a == tid and frag.lower() in str(row[2] or "").lower()), tid)
        if code not in auto:
            continue
        is_afm = auto[code][0].startswith("tests/afm/")
        if suite == "all" or (suite == "afm") == is_afm:
            out.append(code)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", type=Path)
    for k in COLS:
        ap.add_argument(f"--{k}", default="", help=f"comma-separated values of the {k} column")
    ap.add_argument("--suite", choices=("afm", "ssh", "all"), default="all")
    ap.add_argument("-o", "--output", type=Path, help="write the comma-separated list here (else print it)")
    a = ap.parse_args()
    try:
        ids = select(a.workbook, a.suite, **{k: getattr(a, k) for k in COLS})
    except WorkbookLayoutError as e:
        print(f"{a.workbook}: {e}", file=sys.stderr)
        return 2
    text = ",".join(ids)
    if a.output:
        a.output.write_text(text)
        print(f"{len(ids)} case(s) -> {a.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
