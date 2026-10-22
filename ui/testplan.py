"""Test-plan workbooks uploaded from the UI, and which one each rack uses.

Uploads keep their own file name (no fixed full_rack_testplan.xlsx) in the rack's test-plan folder: the folder of
the ``workbook:`` in ui/racks.yaml, else the ``testplan`` folder next to the results (``<root>/results/runs/<rack>``
-> ``<root>/testplan``; installed UI: <prefix>/data/testplan/, kept by upgrades). When racks.yaml's workbook is not
there (a racks.yaml restored or copied from an installation in another directory, a moved installation), the rack
uses the same file name in its own folder, else the only workbook there (``configured``). The rack's choice is ``<results_dir>/ui-workbook.json`` ({"workbook": "<file name>"}); it wins
over racks.yaml's ``workbook:``, which stays the fallback. A file is accepted only when it opens as an .xlsx and the
test-plan reader (ui/plan.py) finds its layout; the file it replaces is kept as <name>.bak-<time>.xlsx.

srv03 20 Oct: "workbook not found: /root/helios-ui/data/testplan/full_rack_testplan.xlsx" - no way to add one
from the browser, and the name was fixed.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import io
import json
import os
import re
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path

from .runner import JobError

CHOICE = "ui-workbook.json"
NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,118}\.xlsx$")
MAX_BYTES = 20 * 1024 * 1024
BACKUP_RX = re.compile(r"\.(bak|backup)-[\w-]+\.xlsx$")       # dated copies (ours and older tools'): not offered


def default_dir(profile) -> Path:
    """The test-plan folder of this installation / checkout: <root>/testplan for results in <root>/results/runs/<rack>."""
    rd = profile.results_dir
    if rd.parent.name == "runs" and len(rd.parents) > 2:
        return rd.parents[2] / "testplan"
    return rd.parent / "testplan"


def plan_dir(profile, configured: Path | None) -> Path:
    """Where this rack's workbooks live (``configured``: racks.yaml's workbook: path, as loaded)."""
    if configured and (configured.parent.is_dir() or not default_dir(profile).is_dir()):
        return configured.parent
    return default_dir(profile)                      # racks.yaml names a folder of another installation


def _workbooks(d: Path) -> list[Path]:
    return sorted((f for f in d.glob("*.xlsx") if f.is_file() and NAME_RX.match(f.name) and not BACKUP_RX.search(f.name)),
                  key=lambda f: f.name.lower()) if d.is_dir() else []


def configured(profile, path: Path | None) -> Path | None:
    """racks.yaml's ``workbook:`` as this installation finds it: the path when the file is there; else the same file
    name in the rack's own test-plan folder; else the only workbook there; else the path as written (shown missing)."""
    if path and path.is_file():
        return path
    d = default_dir(profile)
    if path and (d / path.name).is_file():
        return d / path.name
    found = _workbooks(d)
    return found[0] if len(found) == 1 else path


def _choice_file(profile) -> Path:
    return profile.results_dir / CHOICE


def chosen(profile, configured: Path | None) -> Path | None:
    """The workbook the rack uses: the one chosen in the UI (if it is still there), else racks.yaml's."""
    try:
        name = json.loads(_choice_file(profile).read_text()).get("workbook")
    except (OSError, ValueError, AttributeError):
        name = None
    if isinstance(name, str) and NAME_RX.match(name):
        f = plan_dir(profile, configured) / name
        if f.is_file():
            return f
    return configured


def listing(profile, configured: Path | None) -> dict:
    d = plan_dir(profile, configured)
    cur = profile.workbook
    files = _workbooks(d)
    return {"dir": str(d), "current": cur.name if cur else None, "current_exists": bool(cur and cur.is_file()),
            "configured": configured.name if configured else None,
            "from_ui": bool(cur and configured != cur),
            "files": [{"name": f.name, "size": f.stat().st_size, "current": bool(cur and f == cur),
                       "mtime": dt.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")} for f in files]}


def download_path(profile, configured: Path | None, name: str | None) -> Path:
    """The workbook to download: ``name`` from this rack's test-plan folder (a valid workbook name, not a dated
    backup), or the rack's current workbook when no name is given. KeyError when there is no such file."""
    if name:
        if not NAME_RX.match(name) or BACKUP_RX.search(name):
            raise KeyError(f"not a test-plan workbook name: {name!r}")
        f = plan_dir(profile, configured) / name
    else:
        f = profile.workbook
    if not f or not f.is_file():
        raise KeyError(f"no such workbook: {name or '(current)'}")
    return f


def choose(profile, configured: Path | None, name: str | None) -> Path | None:
    """Make ``name`` (a workbook in the rack's folder) the rack's test plan; None / "" = back to racks.yaml's."""
    f = _choice_file(profile)
    if not name:
        f.unlink(missing_ok=True)
        profile.workbook = configured
        return configured
    if not isinstance(name, str) or not NAME_RX.match(name) or not (plan_dir(profile, configured) / name).is_file():
        raise JobError(f"no workbook {name!r} in {plan_dir(profile, configured)}")
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.tmp")
    tmp.write_text(json.dumps({"workbook": name}, indent=1) + "\n")
    os.replace(tmp, f)
    profile.workbook = plan_dir(profile, configured) / name
    return profile.workbook


def _check(data: bytes, name: str, profile) -> dict:
    """Opens as .xlsx and the test-plan reader finds its layout; returns {cases} or raises JobError."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        if "xl/workbook.xml" not in z.namelist():
            raise JobError(f"{name}: not an Excel .xlsx workbook")
    except zipfile.BadZipFile:
        raise JobError(f"{name}: not an Excel .xlsx workbook (an .xls or .xlsm? save it as .xlsx)") from None
    from . import plan
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / name
        f.write_bytes(data)
        try:
            d = plan.load(replace(profile, workbook=f))
        except JobError as e:
            raise JobError(f"{name}: not a test plan this UI can read - {e}") from None
        except Exception as e:  # noqa: BLE001 - openpyxl on a damaged file
            raise JobError(f"{name}: cannot read it ({type(e).__name__}: {e})") from None
    return {"cases": len(d["cases"])}


def upload(profile, configured: Path | None, name: str, data_b64: str, use: bool = True) -> dict:
    """Store an uploaded workbook under its own name (the one it replaces kept as .bak-<time>), optionally make it
    the rack's test plan."""
    name = os.path.basename(str(name or "")).strip()
    if not NAME_RX.match(name):
        raise JobError("file name: letters, digits, spaces and . _ ( ) + -, ending in .xlsx")
    try:
        data = base64.b64decode(data_b64 or "", validate=True)
    except (binascii.Error, ValueError):
        raise JobError("upload: the file content did not arrive (base64)") from None
    if not data:
        raise JobError(f"{name}: empty file")
    if len(data) > MAX_BYTES:
        raise JobError(f"{name}: {len(data) // 1024 // 1024} MB - a test plan is far smaller (limit 20 MB)")
    info = _check(data, name, profile)
    d = plan_dir(profile, configured)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / name
    backup = None
    if dest.exists():
        backup = dest.with_name(f"{dest.stem}.bak-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx")
        os.replace(dest, backup)
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".part", dir=d)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)
    if use:
        choose(profile, configured, name)
    return {"name": name, "path": str(dest), "cases": info["cases"], "replaced": backup.name if backup else None,
            "current": profile.workbook.name if profile.workbook else None}
