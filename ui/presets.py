"""Run-form presets, per rack, in <results_dir>/ui-presets.json (several racks may share a results_dir, so the file is
keyed by rack). A preset is validated exactly like a run (Job.from_request) and stored from a whitelist of the Job's
fields plus the test-plan pick filters: the AFM password (or any other key) can never end up in the file."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from .config import SECRET_KEY, RackProfile
from .runner import Job, JobError, clean_filters

FILE = "ui-presets.json"
PRESET_NAME_RX = re.compile(r"^[A-Za-z0-9 _.-]{1,60}$")
_lock = threading.Lock()


class PresetExists(JobError):
    pass


def path(profile: RackProfile) -> Path:
    return profile.results_dir / FILE


def check_name(name) -> str:
    n = name.strip() if isinstance(name, str) else ""
    if not n or not PRESET_NAME_RX.match(n):
        raise JobError("preset name: 1-60 letters, digits, spaces, '-', '_' or '.'")
    return n


def normalize(settings) -> dict:
    """The stored form of a preset: what a run would accept (or JobError), nothing else."""
    if not isinstance(settings, dict):
        raise JobError("preset must be an object")
    clean = {k: v for k, v in settings.items() if not SECRET_KEY.match(str(k))}
    try:
        job = Job.from_request(clean)
    except (TypeError, AttributeError) as e:
        raise JobError(f"invalid preset: {e}") from None
    return {**job.as_dict(), "filters": clean_filters(clean.get("filters"))}


def _read(f: Path) -> dict:
    if not f.exists():
        return {"version": 1, "racks": {}}
    try:
        d = json.loads(f.read_text())
    except ValueError as e:
        raise JobError(f"{f} is not valid JSON: {e}") from None
    if not isinstance(d, dict) or not isinstance(d.get("racks"), dict):
        raise JobError(f"{f}: unexpected content")
    return d


def _write(f: Path, d: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ui-presets-", suffix=".tmp", dir=f.parent)
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


def list_presets(profile: RackProfile) -> list[dict]:
    with _lock:
        rack = _read(path(profile))["racks"].get(profile.name) or {}
    return [{"name": n, "saved": p.get("saved"), "settings": p.get("settings") or {}}
            for n, p in sorted(rack.items(), key=lambda kv: kv[0].lower())]


def save(profile: RackProfile, name, settings, overwrite: bool = False) -> dict:
    name = check_name(name)
    stored = normalize(settings)
    f = path(profile)
    with _lock:
        d = _read(f)
        rack = d["racks"].setdefault(profile.name, {})
        if name in rack and not overwrite:
            raise PresetExists(f"preset {name!r} exists")
        rack[name] = {"saved": time.strftime("%Y-%m-%d %H:%M:%S"), "settings": stored}
        _write(f, d)
    return {"name": name, "saved": rack[name]["saved"], "settings": stored}


def delete(profile: RackProfile, name) -> None:
    name = check_name(name)
    f = path(profile)
    with _lock:
        d = _read(f)
        rack = d["racks"].get(profile.name) or {}
        if name not in rack:
            raise KeyError(f"no preset {name!r}")
        del rack[name]
        if not rack:
            d["racks"].pop(profile.name, None)
        _write(f, d)
