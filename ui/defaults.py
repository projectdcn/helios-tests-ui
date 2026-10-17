"""The Run form's saved default, per rack, in <results_dir>/ui-defaults.json (keyed by rack, like the presets: several
racks may share a results_dir). Stored exactly like a preset (presets.normalize: validated like a run, a whitelist of
the Job's fields plus the test-plan pick filters), so the AFM password or any other key never reaches the file.
The page fills the form from it when the Run tab opens or the rack changes; "Reset to default" in the page goes
back to the built-in original form and may delete it (never touching the presets)."""
from __future__ import annotations

import time
from pathlib import Path

from . import presets
from .config import RackProfile

FILE = "ui-defaults.json"


def path(profile: RackProfile) -> Path:
    return profile.results_dir / FILE


def get(profile: RackProfile) -> dict:
    """{saved, settings} of the rack, or {saved: None, settings: None}."""
    with presets._lock:
        d = presets._read(path(profile))["racks"].get(profile.name) or {}
    return {"saved": d.get("saved"), "settings": d.get("settings") or None}


def save(profile: RackProfile, settings) -> dict:
    stored = presets.normalize(settings)
    f = path(profile)
    with presets._lock:
        d = presets._read(f)
        d["racks"][profile.name] = {"saved": time.strftime("%Y-%m-%d %H:%M:%S"), "settings": stored}
        presets._write(f, d)
    return {"saved": d["racks"][profile.name]["saved"], "settings": stored}


def delete(profile: RackProfile) -> bool:
    """Remove the rack's saved default (True if there was one); the file goes when no rack has one left."""
    f = path(profile)
    with presets._lock:
        if not f.exists():
            return False
        d = presets._read(f)
        had = d["racks"].pop(profile.name, None) is not None
        if had:
            if d["racks"]:
                presets._write(f, d)
            else:
                f.unlink()
    return had
