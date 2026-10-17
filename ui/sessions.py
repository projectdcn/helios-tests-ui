"""Test sessions, per rack: the runs started from the UI one after another (e.g. one test case at a time) gathered in
one consolidated result.

<results_dir>/ui-sessions.json (keyed by rack, like the presets: several racks may share a results_dir)::

    {"<rack>": {"current": "<id>", "sessions": {"<id>": {"name", "created", "ui_runs": [run ids], "runs": [folders]}}}}

Every run started from the UI joins its rack's current session (the first run creates one, named by date); "New
session..." starts another. After each run that joins it, <results_dir>/<session-id>/ is rebuilt: one row per test
item (nodeid) with its LATEST result across the session's runs - the rule of tools/html_report.py (an executed result
beats a skip / pre-flight block, otherwise the later run wins) - its command log copied to logs/<run>/<file>,
tc_results.csv, tc_summary.csv (verdicts as the pytest plugin computes them), session.json (name, created, runs,
which run each case came from) and report.html. Session folders are derived (like <run>-merged): results/index.csv
does not index them, and retention protects them and the runs they list.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import ROOT, RackProfile
from .runner import JobError

FILE = "ui-sessions.json"
SESSION_NAME_RX = re.compile(r"^[A-Za-z0-9 _.-]{1,60}$")
MARK = "-session-"
_lock = threading.RLock()

for _p in (str(ROOT), str(Path(ROOT, "tools"))):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def path(profile: RackProfile) -> Path:
    return profile.results_dir / FILE


def is_session(name: str) -> bool:
    return MARK in name


def check_name(name) -> str:
    n = name.strip() if isinstance(name, str) else ""
    if not n or not SESSION_NAME_RX.match(n):
        raise JobError("session name: 1-60 letters, digits, spaces, '-', '_' or '.'")
    return n


def default_name(t: float | None = None) -> str:
    """e.g. '16 Oct 21:58'."""
    return datetime.fromtimestamp(t or time.time()).strftime("%d %b %H:%M")


def _read(f: Path) -> dict:
    if not f.exists():
        return {}
    try:
        d = json.loads(f.read_text())
    except ValueError as e:
        raise JobError(f"{f} is not valid JSON: {e}") from None
    if not isinstance(d, dict):
        raise JobError(f"{f}: unexpected content")
    return d


def _write(f: Path, d: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{f.stem}-", suffix=".tmp", dir=f.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(d, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, f)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rack(d: dict, profile: RackProfile) -> dict:
    r = d.setdefault(profile.name, {})
    if not isinstance(r.get("sessions"), dict):
        r["sessions"] = {}
    r.setdefault("current", None)
    return r


def _new(profile: RackProfile, r: dict, name: str | None) -> str:
    t = time.time()
    while True:      # <rack>-session-YYYYmmdd-HHMMSS: sorts by time; unique even for two in the same second
        sid = f"{profile.name}{MARK}{time.strftime('%Y%m%d-%H%M%S', time.localtime(t))}"
        if sid not in r["sessions"] and not (profile.results_dir / sid).exists():
            break
        t += 1
    r["sessions"][sid] = {"name": check_name(name) if name not in (None, "") else default_name(),
                          "created": time.strftime("%Y-%m-%d %H:%M:%S"), "ui_runs": [], "runs": []}
    r["current"] = sid
    return sid


def new_session(profile: RackProfile, name=None) -> dict:
    """Start a new session of the rack and make it the current one."""
    if name not in (None, ""):
        check_name(name)
    f = path(profile)
    with _lock:
        d = _read(f)
        r = _rack(d, profile)
        sid = _new(profile, r, name)
        _write(f, d)
        return {"id": sid, **r["sessions"][sid]}


def join(profile: RackProfile, run_id: str) -> str:
    """Add a UI run to the rack's current session (created, named by date, if there is none); returns its id."""
    f = path(profile)
    with _lock:
        d = _read(f)
        r = _rack(d, profile)
        sid = r["current"] if r["current"] in r["sessions"] else _new(profile, r, None)
        s = r["sessions"][sid]
        if run_id not in s.setdefault("ui_runs", []):
            s["ui_runs"].append(run_id)
        _write(f, d)
        return sid


def add_results(profile: RackProfile, sid: str, folders: list[str]) -> list[str]:
    """Record the results folders a run of the session wrote (plain runs; a <run>-merged is derived from them)."""
    plain = [n for n in folders if not n.endswith("-merged") and not is_session(n)]
    f = path(profile)
    with _lock:
        d = _read(f)
        s = _rack(d, profile)["sessions"].get(sid)
        if s is None:
            raise KeyError(f"no session {sid!r}")
        runs = s.setdefault("runs", [])
        runs += [n for n in plain if n not in runs]
        _write(f, d)
        return list(runs)


def get(profile: RackProfile, sid: str) -> dict:
    with _lock:
        s = _rack(_read(path(profile)), profile)["sessions"].get(sid)
    if s is None:
        raise KeyError(f"no session {sid!r}")
    return {"id": sid, **s}


def counts_of(folder: Path) -> dict:
    c: dict[str, int] = {}
    try:
        with open(folder / "tc_summary.csv", newline="") as fh:
            for r in csv.DictReader(fh):
                c[r["verdict"]] = c.get(r["verdict"], 0) + 1
    except OSError:
        pass
    return c


def summary_text(counts: dict[str, int]) -> str:
    """'12 cases: 9 PASS 2 SKIP 1 FAIL' (most frequent verdict first)."""
    n = sum(counts.values())
    if not n:
        return "no cases yet"
    order = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return f"{n} case{'s' if n != 1 else ''}: " + " ".join(f"{c} {v}" for v, c in order)


def list_sessions(profile: RackProfile) -> dict:
    """{current, sessions: [...newest first]} with the consolidated folder's summary and files."""
    with _lock:
        r = _rack(_read(path(profile)), profile)
    out = []
    for sid, s in r["sessions"].items():
        folder = profile.results_dir / sid
        counts = counts_of(folder) if folder.is_dir() else {}
        written = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((folder / "session.json").stat().st_mtime))
                   if (folder / "session.json").is_file() else None)
        out.append({"id": sid, "name": s.get("name"), "created": s.get("created"), "runs": s.get("runs") or [],
                    "ui_runs": s.get("ui_runs") or [], "counts": counts, "summary": summary_text(counts),
                    "written": written, "folder": folder.is_dir(),
                    "reports": sorted(x.name for x in folder.glob("*.html")) if folder.is_dir() else [],
                    "current": sid == r["current"]})
    out.sort(key=lambda x: x["id"], reverse=True)
    return {"current": r["current"] if r["current"] in r["sessions"] else None, "sessions": out}


# ------------------------------------------------------------------ the consolidated results folder

def _key(row: dict) -> str:
    return row.get("nodeid") or f"{row.get('tc_id', '')}::{row.get('test', '')}"


def consolidate(runs: list[Path], out: Path, info: dict) -> dict:
    """Write ``out`` from ``runs`` (oldest first): latest result per test item (html_report.supersedes), its log,
    tc_results.csv, tc_summary.csv and session.json. Returns the session.json content."""
    from html_report import supersedes
    from helios.pytest_plugin import SUMMARY_FIELDS, summary_rows

    picked: dict[str, tuple[Path, dict]] = {}
    fields: list[str] = []
    used = []
    for r in runs:
        f = r / "tc_results.csv"
        if not f.is_file():
            continue
        used.append(r)
        with open(f, newline="") as fh:
            rd = csv.DictReader(fh)
            fields += [x for x in rd.fieldnames or [] if x not in fields]
            for row in rd:
                k = _key(row)
                if supersedes(picked.get(k, (None, None))[1], row):
                    picked.pop(k, None)              # re-insert: the item moves to where it last ran
                    picked[k] = (r, row)
    tmp = out.parent / f".{out.name}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "logs").mkdir(parents=True)
    rows, cases = [], {}
    for k, (r, row) in picked.items():
        row = dict(row)
        if row.get("log"):
            src = r / row["log"]
            row["log"] = f"logs/{r.name}/{Path(row['log']).name}"
            if src.is_file():
                (tmp / "logs" / r.name).mkdir(exist_ok=True)
                shutil.copy2(src, tmp / row["log"])
        rows.append(row)
        cases[k] = {"tc_id": row.get("tc_id") or "", "outcome": row.get("outcome") or "", "run": r.name,
                    "started": row.get("started") or "", "log": row.get("log") or ""}
    with open(tmp / "tc_results.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields or ["tc_id", "nodeid", "outcome", "log"])
        w.writeheader()
        w.writerows(rows)
    with open(tmp / "tc_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(SUMMARY_FIELDS)
        w.writerows(summary_rows(rows))
    meta = {**info, "runs": [r.name for r in runs], "built": time.strftime("%Y-%m-%d %H:%M:%S"),
            "items": len(rows), "cases": cases}
    (tmp / "session.json").write_text(json.dumps(meta, indent=1) + "\n")
    if out.exists():
        shutil.rmtree(out)
    os.replace(tmp, out)
    return meta


def rebuild(profile: RackProfile, sid: str, log=lambda s: None) -> Path:
    """(Re)build <results_dir>/<sid>/ from the session's runs, with report.html."""
    from .runs import _tool
    with _lock:
        s = get(profile, sid)
        runs = [profile.results_dir / n for n in s.get("runs") or []]
        out = profile.results_dir / sid
        meta = consolidate(runs, out, {"id": sid, "name": s.get("name"), "created": s.get("created"),
                                       "rack": profile.name, "ui_runs": s.get("ui_runs") or []})
        # the report from the source runs (each row says which run it came from), the same latest-per-item rule
        src = [str(r) for r in runs if (r / "tc_results.csv").is_file()]
        title = f"{profile.name} session {s.get('name')}"
        sub = f"{len(meta['runs'])} run(s), {summary_text(counts_of(out))}"
        if src:
            _tool(["tools/html_report.py", *src, "-o", str(out / "report.html"), "--title", title,
                   "--subtitle", sub], log)
        return out
