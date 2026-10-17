"""Which code this UI runs: an installed bundle (tools/build_ui_installer.py writes BUILD_INFO.json and MANIFEST.txt
next to the code) or a git checkout (``git describe``)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import ROOT

BUILD_INFO = "BUILD_INFO.json"
MANIFEST = "MANIFEST.txt"


def build_info(root: Path = ROOT) -> dict | None:
    """{version, commit, built, ...} of an installed bundle, or None (a git checkout)."""
    try:
        d = json.loads((root / BUILD_INFO).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("version") else None


def is_git_checkout(root: Path = ROOT) -> bool:
    return (root / ".git").exists()        # a directory, or a file in a git worktree


def manifest(root: Path = ROOT) -> list[str] | None:
    """The files of an installed bundle (repo-relative, as git archive listed them), or None."""
    try:
        lines = (root / MANIFEST).read_text().splitlines()
    except OSError:
        return None
    return [ln for ln in lines if ln and not ln.startswith("#")]


def version(root: Path = ROOT) -> str:
    bi = build_info(root)
    if bi and not is_git_checkout(root):
        return str(bi["version"])
    try:
        r = subprocess.run(["git", "-C", str(root), "describe", "--tags", "--always", "--dirty"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return f"git {r.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        pass
    return str(bi["version"]) if bi else "unknown"
