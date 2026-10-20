"""A results folder as a zip to send OUTSIDE the team (AMD, a ticket): passwords, tokens and keys blanked out.

Every text file (and the text inside .gz files) goes through ``redact_text``: the secret values this UI knows
(saved AFM passwords, its access token, secret-named environment variables), the run-log rules of ui/runs.py,
``password: x`` / ``token = x`` lines, ``--password x`` options, ``Authorization:`` headers, ``user:pw@`` in URLs and
PEM private keys. Binary files other than images / PDFs are left out (they cannot be checked). SHARED-README.txt in
the zip lists what was changed and what was left out. Addresses and host names are NOT removed.
"""
from __future__ import annotations

import gzip
import io
import os
import re
import time
import zipfile
from pathlib import Path

from .runs import REDACTED, SECRET_NAME, redact

KEYWORDS = r"(?:password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key|access[_-]?key)"
KEY = rf"[-\w.]*{KEYWORDS}(?:[_-][\w-]*)?"      # a key whose name ends in a secret word (password, afm_password_x)
_RULES = [
    # PEM private keys (any kind), whole block
    (re.compile(r"-----BEGIN ([A-Z0-9 ]*)PRIVATE KEY-----.*?-----END \1PRIVATE KEY-----", re.S),
     lambda m: f"-----BEGIN {m.group(1)}PRIVATE KEY-----\n{REDACTED}\n-----END {m.group(1)}PRIVATE KEY-----"),
    # key: value / key = value (YAML, INI, env files, logs), the key containing a secret word
    (re.compile(rf"(?im)^(\s*[\"']?{KEY}[\"']?\s*[:=]\s*)(?!{re.escape(REDACTED)})(\S[^\n]*)"),
     lambda m: m.group(1) + REDACTED),
    # the same inside an HTML report (&quot;password&quot;: &quot;x&quot;)
    (re.compile(rf"(&quot;{KEYWORDS}&quot;\s*:\s*&quot;)(.*?)(&quot;)", re.I), lambda m: m.group(1) + REDACTED + m.group(3)),
    # command-line options: --password x, --token=x, -p x after sshpass
    (re.compile(rf"(--{KEYWORDS}(?:[= ]+))(?!{re.escape(REDACTED)})([^\s'\"]+)", re.I), lambda m: m.group(1) + REDACTED),
    (re.compile(r"(\bsshpass\s+-p\s*)(\S+)"), lambda m: m.group(1) + REDACTED),
    # HTTP auth headers
    (re.compile(r"(?i)(\bAuthorization\s*:\s*(?:Bearer|Basic|Token)\s+)([A-Za-z0-9._~+/=-]+)"), lambda m: m.group(1) + REDACTED),
    # user:password@ in URLs
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)", re.I), lambda m: m.group(1) + REDACTED + m.group(3)),
]
KEEP_BINARY = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico"}
MAX_TEXT = 256 * 1024 * 1024          # a text file larger than this is left out (not read into memory)
README = "SHARED-README.txt"


def known_secrets(extra=()) -> list[str]:
    """Secret values to blank wherever they appear: secret-named environment variables (HELIOS_UI_TOKEN, ...) and
    ``extra`` (e.g. the saved AFM passwords). Short values (< 4 chars) are skipped: they would hit ordinary text."""
    vals = {v for k, v in os.environ.items() if SECRET_NAME.search(k)} | {v for v in extra if v}
    return sorted((v for v in vals if isinstance(v, str) and len(v) >= 4), key=len, reverse=True)


def redact_text(text: str, values=()) -> tuple[str, int]:
    """(redacted text, number of replacements)."""
    n = 0
    for v in values:
        if v in text:
            n += text.count(v)
            text = text.replace(v, REDACTED)
    for rx, fn in _RULES:
        text, k = rx.subn(fn, text)
        n += k
    out = []
    for line in text.split("\n"):                 # the run-log rules (JSON keys, NAME=value)
        r = redact(line)
        if r != line:
            n += 1
        out.append(r)
    return "\n".join(out), n


def _as_text(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1") if sum(b < 9 or 13 < b < 32 for b in data[:8192]) == 0 else None
        except UnicodeDecodeError:
            return None


def share_zip(folder: Path, fh, values=(), shared_by: str = "") -> dict:
    """Write the redacted zip of ``folder`` to ``fh``; returns {files, changed: {path: n}, left_out: [path]}."""
    folder = folder.resolve()
    changed, left_out, files = {}, [], 0
    with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dp = Path(dirpath)
            dirnames[:] = sorted(x for x in dirnames if not (dp / x).is_symlink())
            for fn in sorted(filenames):
                f = dp / fn
                if f.is_symlink() or not f.is_file() or folder not in f.resolve().parents:
                    continue
                rel = f.relative_to(folder).as_posix()
                arc = f"{folder.name}/{rel}"
                if f.suffix.lower() in KEEP_BINARY:
                    z.write(f, arc)
                    files += 1
                    continue
                if f.stat().st_size > MAX_TEXT:
                    left_out.append(f"{rel} (too large to check)")
                    continue
                data = f.read_bytes()
                gz = f.suffix == ".gz"
                if gz:
                    try:
                        data = gzip.decompress(data)
                    except (OSError, EOFError):
                        left_out.append(f"{rel} (unreadable .gz)")
                        continue
                text = _as_text(data)
                if text is None:
                    left_out.append(f"{rel} (binary: cannot be checked)")
                    continue
                text, n = redact_text(text, values)
                if n:
                    changed[rel] = n
                out = text.encode("utf-8")
                z.writestr(arc, gzip.compress(out, mtime=0) if gz else out)
                files += 1
        z.writestr(f"{folder.name}/{README}", readme(folder.name, files, changed, left_out, shared_by))
    return {"files": files, "changed": changed, "left_out": left_out}


def readme(name: str, files: int, changed: dict, left_out: list, shared_by: str) -> str:
    lines = [f"{name} - shared copy of a helios-tests results folder",
             f"made {time.strftime('%Y-%m-%d %H:%M:%S %Z')}" + (f" by {shared_by}" if shared_by else ""), "",
             "Passwords, tokens, API keys, private keys, Authorization headers and user:password@ in URLs were",
             f"replaced by {REDACTED}. Addresses and host names were NOT removed.", "",
             f"{files} file(s) included; {sum(changed.values())} replacement(s) in {len(changed)} file(s):"]
    lines += [f"  {n:6d}  {p}" for p, n in sorted(changed.items())] or ["  (none)"]
    lines += ["", f"left out ({len(left_out)}):"] + ([f"  {p}" for p in left_out] or ["  (none)"])
    return "\n".join(lines) + "\n"
