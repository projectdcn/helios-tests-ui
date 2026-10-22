"""Clearing results from the UI without losing them: a run folder or a session moves to <results_dir>/.trash/, from
where the Results tab can restore it. Nothing here deletes a file; emptying the trash is left to the user.

<results_dir>/.trash/<name>@<YYYYmmdd-HHMMSS>/  - the run / session folder as it was, plus .trash.json:
    {"name", "kind": "run" | "session", "removed", "by", "session": {...its ui-sessions.json entry}}

A cleared run leaves results/index.csv (Case history) and is put back on restore; a session it belonged to keeps
listing it, and Rebuild skips it while it is in the trash. A cleared session leaves ui-sessions.json (its runs stay
where they are); if it was the current one, the next UI run starts a new session.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

from . import sessions
from .config import ROOT, RackProfile
from .runner import JobError

DIR = ".trash"
ENTRY_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}@\d{8}-\d{6}$")


def trash_dir(profile: RackProfile) -> Path:
    return profile.results_dir / DIR


def _index(profile: RackProfile, run: Path | None = None, drop: str | None = None) -> None:
    """Case history (results/index.csv): drop a cleared run's rows / add a restored run's. Never fails the action."""
    try:
        sys.path.insert(0, str(Path(ROOT, "tools")))
        import results_index as ri
        root = profile.results_dir.parent.parent        # <root>/runs/<rack>
        if drop:
            rows = [r for r in ri.read(root) if not (r["rack"] == profile.results_dir.name and r["run"] == drop)]
            ri._write(ri.index_path(root), rows)
        if run is not None:
            ri.update_run(run)
    except Exception:  # noqa: BLE001 - the index is a convenience; the folder move is what matters
        pass


def _move(profile: RackProfile, src: Path, kind: str, by: str | None, extra: dict | None = None) -> dict:
    t = time.time()
    while True:                                           # two clears of the same name in one second: the next one
        dst = trash_dir(profile) / f"{src.name}@{time.strftime('%Y%m%d-%H%M%S', time.localtime(t))}"
        if not dst.exists():
            break
        t += 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    meta = {"name": src.name, "kind": kind, "removed": time.strftime("%Y-%m-%d %H:%M:%S"), "by": by or "", **(extra or {})}
    (dst / ".trash.json").write_text(json.dumps(meta, indent=1) + "\n")
    return {"entry": dst.name, **meta}


def clear_run(profile: RackProfile, name: str, by: str | None = None) -> dict:
    """Move one run folder of this rack to the trash."""
    if sessions.is_session(name) or "/" in name or name.startswith("."):
        raise JobError(f"{name}: not a run folder")
    src = profile.results_dir / name
    if not src.is_dir() or not (src / "tc_summary.csv").exists():
        raise KeyError(f"no run {name!r}")
    out = _move(profile, src, "run", by)
    _index(profile, drop=name)
    return out


def clear_session(profile: RackProfile, sid: str, by: str | None = None) -> dict:
    """Remove a session (its ui-sessions.json entry and consolidated folder) to the trash; its runs stay."""
    if not sessions.is_session(sid):
        raise JobError(f"{sid}: not a session")
    f = sessions.path(profile)
    with sessions._lock:
        d = sessions._read(f)
        r = sessions._rack(d, profile)
        entry = r["sessions"].pop(sid, None)
        if entry is None:
            raise KeyError(f"no session {sid!r}")
        was_current = r.get("current") == sid
        if was_current:
            r["current"] = None
        folder = profile.results_dir / sid
        if not folder.is_dir():                           # never built: keep the entry in an empty folder
            folder.mkdir(parents=True)
        out = _move(profile, folder, "session", by, {"session": entry, "was_current": was_current})
        sessions._write(f, d)
    return out


def listing(profile: RackProfile) -> list[dict]:
    """The trash, newest first: [{entry, name, kind, removed, by, size}]."""
    d = trash_dir(profile)
    out = []
    for p in d.iterdir() if d.is_dir() else []:
        if not p.is_dir() or not ENTRY_RX.match(p.name):
            continue
        try:
            meta = json.loads((p / ".trash.json").read_text())
        except (OSError, ValueError):
            meta = {"name": p.name.split("@")[0], "kind": "run"}
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        out.append({"entry": p.name, "name": meta.get("name"), "kind": meta.get("kind"), "removed": meta.get("removed"),
                    "by": meta.get("by") or "", "size": size,
                    "restorable": not (profile.results_dir / str(meta.get("name"))).exists()})
    return sorted(out, key=lambda x: x["entry"].split("@")[1], reverse=True)


def restore(profile: RackProfile, entry: str) -> dict:
    """Put a trash entry back where it was (refused when a folder of that name exists again)."""
    if not ENTRY_RX.match(entry):
        raise JobError(f"{entry}: not a trash entry")
    src = trash_dir(profile) / entry
    if not src.is_dir():
        raise KeyError(f"no trash entry {entry!r}")
    try:
        meta = json.loads((src / ".trash.json").read_text())
    except (OSError, ValueError):
        meta = {"name": entry.split("@")[0], "kind": "run"}
    dst = profile.results_dir / meta["name"]
    if dst.exists():
        raise JobError(f"{meta['name']} exists again in the results - rename or clear it first")
    (src / ".trash.json").unlink(missing_ok=True)
    shutil.move(str(src), str(dst))
    if meta.get("kind") == "session":
        f = sessions.path(profile)
        with sessions._lock:
            d = sessions._read(f)
            r = sessions._rack(d, profile)
            r["sessions"][meta["name"]] = meta.get("session") or {"name": meta["name"], "runs": [], "ui_runs": []}
            sessions._write(f, d)
    else:
        _index(profile, run=dst)
    return {"name": meta["name"], "kind": meta.get("kind")}
