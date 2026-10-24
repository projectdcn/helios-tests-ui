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
# "any column" filter: a column with at most this many distinct short values gets value checkboxes, else "contains"
CHOICE_MAX, CHOICE_LEN = 40, 80


def any_columns(rows: list[dict]) -> list[dict]:
    """[{name, values} | {name, free: True}] of every titled workbook column (header text as in row 2)."""
    names = list(rows[0]["cells"]) if rows else []
    out = []
    for n in names:
        seen = {}
        for r in rows:
            v = r["cells"].get(n, "") or sc.EMPTY
            seen.setdefault(v.lower(), v)
        vals = sorted(seen.values(), key=_sort_key)
        if len(vals) <= CHOICE_MAX and all(len(v) <= CHOICE_LEN for v in vals):
            out.append({"name": n, "values": vals})
        else:
            out.append({"name": n, "free": True})
    return out


def parse_cols(text) -> list[dict]:
    """The "any column" filter as the form stores it (filters["cols"], JSON text): [{col, values: [...]} or
    {col, contains: "text"}]. Raises JobError on anything else."""
    if text in (None, ""):
        return []
    import json
    try:
        rules = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        raise JobError("filter cols: JSON list of {col, values | contains}") from None
    if not isinstance(rules, list):
        raise JobError("filter cols: JSON list of {col, values | contains}")
    out = []
    for r in rules:
        if not isinstance(r, dict) or not isinstance(r.get("col"), str) or not r["col"].strip():
            raise JobError("filter cols: every rule needs a column name (col)")
        if isinstance(r.get("contains"), str):
            if r["contains"].strip():
                out.append({"col": r["col"], "contains": r["contains"].strip().lower()})
        elif isinstance(r.get("values"), list) and all(isinstance(v, str) for v in r["values"]):
            vals = {v.strip().lower() for v in r["values"] if v.strip()}
            if vals:
                out.append({"col": r["col"], "values": vals})
        else:
            raise JobError(f"filter cols: {r['col']!r} needs values (a list) or contains (text)")
    return out


def match_cols(row: dict, rules: list[dict]) -> bool:
    for r in rules:
        cell = str(row.get("cells", {}).get(r["col"], "")).lower()
        if "contains" in r:
            if r["contains"] not in cell:
                return False
        elif (cell or sc.EMPTY.lower()) not in r["values"]:
            return False
    return True


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
            for v in sc.tags(r[k]) or [sc.EMPTY]:    # one entry per tag ("Switch + Compute; IFCP" -> both)
                seen.setdefault(v.lower(), v)           # matching is case-insensitive: one entry per value
        columns[k] = sorted(seen.values(), key=_sort_key)
    cases = [{"id": r["id"], "title": r["title"], **{k: r[k] for k in COLUMNS},
              "automated": r["id"] in auto, "suite": sc.suite_of(auto[r["id"]][0]) if r["id"] in auto else None}
             for r in rows]
    return {"workbook": wbp.name, "columns": columns, "cases": cases, "any_columns": any_columns(rows)}


def pick(profile, filters) -> dict:
    """The automated cases matching the filters (select_cases.select order), plus how many matching rows have no
    test yet. Filters as a run / preset stores them (clean_filters: strings or lists)."""
    f = clean_filters(filters)
    suite = f.pop("suite", "all")
    rules = parse_cols(f.pop("cols", ""))
    want = sc.wanted(f)
    auto = automated()
    tc, not_auto = [], 0
    for r in _rows(_workbook(profile)):
        if not sc.matches(r, want) or not match_cols(r, rules):
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
