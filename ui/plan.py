"""The rack's test-plan workbook for the Run form's "Pick from the test plan": distinct column values for the filter
dropdowns, every case (code ID, title, columns, automated?, suite) and the pick itself. Read only, through
tools/select_cases.py (same layout check, row aliases, automation and matching as the command line), cached per
(workbook path, mtime, size); the automation map is cached per state of tests/."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from .config import ROOT
from .runner import JobError, clean_filters

sys.path.insert(0, str(ROOT / "tools"))
import select_cases as sc  # noqa: E402

_lock = threading.Lock()
_plans: dict[str, tuple[tuple, dict]] = {}
_auto: tuple[tuple, dict] | None = None
# filter columns in the order the form shows them (suite is all / afm / ssh, not a workbook column)
COLUMNS = ("level", "scope", "disruptive", "health", "origin", "status")


def _tests_signature() -> tuple:
    return tuple(sorted((str(f), f.stat().st_mtime_ns) for f in (ROOT / "tests").rglob("*.py")))


def automated() -> dict:
    global _auto
    sig = _tests_signature()
    with _lock:
        if _auto is not None and _auto[0] == sig:
            return _auto[1]
    a = sc.automated()
    with _lock:
        _auto = (sig, a)
    return a


def _sort_key(v: str):
    return (v == sc.EMPTY, v.lower())


def _workbook(profile) -> Path:
    wbp = profile.workbook
    if not wbp:
        raise JobError(f"no test-plan workbook for {profile.name}: upload one in the Setup tab (Test plan)")
    if not wbp.is_file() or wbp.suffix != ".xlsx":
        raise JobError(f"{profile.name}: workbook not found: {wbp} - upload it (or another) in the Setup tab (Test plan)")
    return wbp


def _rows(wbp: Path) -> list[dict]:
    st = wbp.stat()
    key, sig = str(wbp.resolve()), (st.st_mtime_ns, st.st_size)
    with _lock:
        hit = _plans.get(key)
        if hit and hit[0] == sig:
            return hit[1]["rows"]
    try:
        rows = sc.plan_rows(wbp)
    except sc.WorkbookLayoutError as e:
        raise JobError(f"{wbp.name}: {e}") from None
    with _lock:
        _plans[key] = (sig, {"rows": rows})
    return rows


def load(profile) -> dict:
    """{workbook, columns: {col: [distinct values, "(empty)" for blank cells, last]}, cases: [...]}"""
    wbp = _workbook(profile)
    rows = _rows(wbp)
    auto = automated()
    columns = {}
    for k in COLUMNS:
        seen = {}
        for r in rows:
            v = r[k] or sc.EMPTY
            seen.setdefault(v.lower(), v)           # matching is case-insensitive: one entry per value
        columns[k] = sorted(seen.values(), key=_sort_key)
    cases = [{"id": r["id"], "title": r["title"], **{k: r[k] for k in COLUMNS},
              "automated": r["id"] in auto, "suite": sc.suite_of(auto[r["id"]][0]) if r["id"] in auto else None}
             for r in rows]
    return {"workbook": wbp.name, "columns": columns, "cases": cases}


def pick(profile, filters) -> dict:
    """The automated cases matching the filters (select_cases.select order), plus how many matching rows have no
    test yet. Filters as a run / preset stores them (clean_filters: strings or lists)."""
    f = clean_filters(filters)
    suite = f.pop("suite", "all")
    want = sc.wanted(f)
    auto = automated()
    tc, not_auto = [], 0
    for r in _rows(_workbook(profile)):
        if not sc.matches(r, want):
            continue
        if r["id"] not in auto:
            not_auto += 1
        elif suite == "all" or suite == sc.suite_of(auto[r["id"]][0]):
            tc.append(r["id"])
    return {"tc": tc, "not_automated": not_auto, "workbook": profile.workbook.name}


def clear_cache() -> None:
    global _auto
    with _lock:
        _plans.clear()
        _auto = None
