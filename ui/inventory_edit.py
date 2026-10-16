"""Inventory editor: a form model of the inventory YAML, validation (the real loader + uniqueness + no secrets),
diff preview, dated backup before every save, and the per-node quarantine toggle (``quarantine: {reason, since,
until}`` as helios/inventory.py and helios/quarantine.py read it).

Saving from the raw YAML text keeps comments. A form save re-writes the YAML (comments are lost - the diff preview
shows it). The quarantine toggle edits only the node's lines when it can, so comments elsewhere survive.
"""
from __future__ import annotations

import copy
import datetime as dt
import difflib
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

import yaml

from helios.inventory import load_inventory

from .config import find_secrets

ROLES = (("switches", "switch"), ("computes", "compute"))
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# form columns per role; every other key of an entry is kept in 'extra' (YAML)
COLUMNS = {"switches": ("name", "host", "user", "port", "key_file", "afm_name"),
           "computes": ("name", "host", "user", "gpus", "bmc", "slot", "afm_name")}
INT_KEYS = ("port", "gpus", "slot")


class EditError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _dump(obj) -> str:
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False, width=120, allow_unicode=True)


def _flow(obj) -> str:
    return yaml.safe_dump(obj, default_flow_style=True, width=10000, sort_keys=False).strip()


def _entries(data: dict, key: str) -> list[dict]:
    return [({"name": e} if isinstance(e, str) else dict(e or {})) for e in (data.get(key) or [])]


# ------------------------------------------------------------------ validation

def validate(text: str, path: Path) -> tuple[list[str], list[str], dict]:
    """(errors, warnings, parsed data). Loads the text with helios.inventory.load_inventory from a temp file next to
    ``path`` (so relative paths like a replay dir resolve the same way)."""
    errors, warnings = [], []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"YAML: {e}"], [], {}
    if not isinstance(data, dict):
        return ["the inventory must be a YAML mapping"], [], {}
    for p in find_secrets(data):
        errors.append(f"{p}: secret values are not allowed in the inventory - use {p.rsplit('.', 1)[-1]}_env "
                      "(the name of an environment variable) or key_file")
    for key in ("thresholds", "commands", "afm", "transport", "defaults"):
        if data.get(key) is not None and not isinstance(data[key], dict):
            errors.append(f"{key}: must be a mapping")
    names: dict[str, str] = {}
    hosts: dict[str, str] = {}
    bmcs: dict[str, str] = {}
    slots: dict[str, str] = {}
    for key, role in ROLES:
        if data.get(key) is not None and not isinstance(data[key], list):
            errors.append(f"{key}: must be a list")
            continue
        for i, e in enumerate(_entries(data, key)):
            n = str(e.get("name") or "").strip()
            if not n:
                errors.append(f"{key}[{i}]: name is required")
                continue
            if n in names:
                errors.append(f"duplicate name {n!r} ({names[n]} and {role})")
            names[n] = role
            h = str(e.get("host", n))
            if h in hosts:
                errors.append(f"duplicate host / IP {h!r} ({hosts[h]} and {n})")
            hosts[h] = n
            if e.get("bmc"):
                b = str(e["bmc"])
                if b in bmcs:
                    errors.append(f"duplicate bmc {b!r} ({bmcs[b]} and {n})")
                bmcs[b] = n
            if role == "compute" and e.get("slot") is not None:
                s = str(e["slot"])
                if s in slots:
                    errors.append(f"duplicate compute slot {s} ({slots[s]} and {n})")
                slots[s] = n
            for k in INT_KEYS:
                if e.get(k) is not None and not re.fullmatch(r"-?\d+", str(e[k])):
                    errors.append(f"{n}: {k} must be a number, not {e[k]!r}")
            q = e.get("quarantine")
            if q:
                qd = q if isinstance(q, dict) else {"reason": q}
                if not str(qd.get("reason") or "").strip():
                    errors.append(f"{n}: quarantine needs a reason")
                for k in ("since", "until"):
                    if qd.get(k) is not None and not DATE_RX.match(str(qd[k])):
                        errors.append(f"{n}: quarantine {k} must be YYYY-MM-DD, not {qd[k]!r}")
                if role == "compute" and e.get("slot") is None and not e.get("afm_name"):
                    warnings.append(f"{n}: quarantined compute without 'slot' (AFM slot-id): the AFM-side and fabric "
                                    "checks cannot leave its AFM node / switch ports out")
    if not errors:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.check-", suffix=".yaml", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            load_inventory(tmp)
        except Exception as e:  # noqa: BLE001 - any loader complaint is a validation error
            errors.append(f"inventory loader: {e}")
        finally:
            os.unlink(tmp)
    return errors, warnings, data


def diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"{name} (current)", f"{name} (new)"))


# ------------------------------------------------------------------ form model <-> data

def to_form(data: dict) -> dict:
    afm = data.get("afm") or {}
    auth = afm.get("auth") or {}
    form = {"rack": data.get("rack", ""),
            "afm": {"url": afm.get("url", ""), "username": auth.get("username", ""),
                    "password_env": auth.get("password_env", ""), "verify_tls": bool(afm.get("verify_tls", False))},
            "transport_yaml": _dump(data["transport"]) if data.get("transport") else "",
            "defaults_yaml": _dump(data["defaults"]) if data.get("defaults") else "",
            "thresholds_yaml": _dump(data["thresholds"]) if data.get("thresholds") else ""}
    for key, _ in ROLES:
        rows = []
        for e in _entries(data, key):
            row = {c: ("" if e.get(c) is None else e.get(c)) for c in COLUMNS[key]}
            extra = {k: v for k, v in e.items() if k not in COLUMNS[key] and k != "quarantine"}
            row["extra_yaml"] = _flow(extra) if extra else ""
            q = e.get("quarantine")
            row["quarantine"] = (q if isinstance(q, dict) else {"reason": str(q)}) if q else None
            rows.append(row)
        form[key] = rows
    return form


def _yaml_field(text: str, label: str, errors: list[str], want=dict):
    if not str(text or "").strip():
        return None
    try:
        v = yaml.safe_load(text)
    except yaml.YAMLError as e:
        errors.append(f"{label}: {e}")
        return None
    if v is not None and not isinstance(v, want):
        errors.append(f"{label}: must be a mapping")
        return None
    return v


def from_form(original: dict, form: dict) -> dict:
    """The original data with the form's fields applied (keys the form does not show are kept)."""
    errors: list[str] = []
    data = copy.deepcopy(original) if isinstance(original, dict) else {}
    if str(form.get("rack") or "").strip():
        data["rack"] = str(form["rack"]).strip()
    f_afm = form.get("afm") or {}
    if any(str(f_afm.get(k) or "").strip() for k in ("url", "username", "password_env")) or data.get("afm"):
        afm = data.setdefault("afm", {}) or {}
        data["afm"] = afm
        auth = afm.setdefault("auth", {}) or {}
        afm["auth"] = auth
        for k, tgt in (("url", afm), ("username", auth), ("password_env", auth)):
            v = str(f_afm.get(k) or "").strip()
            if v:
                tgt[k] = v
            else:
                tgt.pop(k, None)
        if "verify_tls" in f_afm and bool(f_afm["verify_tls"]) != bool(afm.get("verify_tls", False)):
            afm["verify_tls"] = bool(f_afm["verify_tls"])
        if not auth:
            afm.pop("auth", None)
    for k, label in (("transport", "transport"), ("defaults", "defaults"), ("thresholds", "thresholds")):
        if f"{k}_yaml" in form:
            v = _yaml_field(form.get(f"{k}_yaml"), label, errors)
            if v:
                data[k] = v
            else:
                data.pop(k, None)
    for key, _ in ROLES:
        if key not in form:
            continue
        out = []
        for i, row in enumerate(form.get(key) or []):
            e: dict = {}
            for c in COLUMNS[key]:
                v = row.get(c)
                if v is None or str(v).strip() == "":
                    continue
                v = str(v).strip() if not isinstance(v, (int, bool)) else v
                if c in INT_KEYS and re.fullmatch(r"-?\d+", str(v)):
                    v = int(v)
                e[c] = v
            extra = _yaml_field(row.get("extra_yaml"), f"{key}[{i}] extra", errors)
            for k, v in (extra or {}).items():
                if k in COLUMNS[key] or k == "quarantine":
                    errors.append(f"{key}[{i}] extra: {k} has its own column")
                    continue
                e[k] = v
            if row.get("quarantine"):
                e["quarantine"] = {k: v for k, v in row["quarantine"].items() if v not in (None, "")}
            out.append(e)
        data[key] = out
    if errors:
        raise EditError(errors)
    return data


# ------------------------------------------------------------------ quarantine toggle (minimal text edit)

def _find_node(data: dict, node: str) -> tuple[str, int]:
    for key, _ in ROLES:
        for i, e in enumerate(data.get(key) or []):
            if (e if isinstance(e, str) else (e or {}).get("name")) == node:
                return key, i
    raise EditError([f"no switch / compute named {node!r} in the inventory"])


def _patch_text(text: str, key: str, node: str, q: dict | None) -> str | None:
    """Edit only the node's entry; None when the layout is not one this handles (caller re-writes the YAML)."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^{key}:\s*(#.*)?$", ln)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[^\s#-]", lines[i])), len(lines))
    nq = re.escape(node)
    for i in range(start + 1, end):
        ln = lines[i]
        m = re.match(rf"^(\s*)-\s+name:\s*(['\"]?){nq}\2\s*(#.*)?$", ln.rstrip("\n"))
        if m:                                           # block entry, name first
            ind = " " * (len(m.group(1)) + 2)
            j = i + 1
            body_end = j
            while body_end < end and (not lines[body_end].strip() or
                                      (len(lines[body_end]) - len(lines[body_end].lstrip())) >= len(ind)):
                body_end += 1
            while body_end > j and not lines[body_end - 1].strip():
                body_end -= 1
            body = lines[j:body_end]
            out, k = [], 0
            while k < len(body):                       # drop an existing quarantine key (and its nested lines)
                b = body[k]
                if b.startswith(ind + "quarantine:"):
                    k += 1
                    while k < len(body) and body[k].strip() and \
                            (len(body[k]) - len(body[k].lstrip())) > len(ind):
                        k += 1
                    continue
                out.append(b)
                k += 1
            new = [ind + "quarantine: " + _flow(q) + "\n"] if q else []
            return "".join(lines[:j] + new + out + lines[body_end:])
        m = re.match(r"^(\s*)-\s*\{(.*)\}\s*(#.*)?$", ln.rstrip("\n"))
        if m and re.search(rf"(^|,)\s*name:\s*(['\"]?){nq}\2\s*(,|$)", m.group(2)):
            nl = "\n" if ln.endswith("\n") else ""
            core = ln.rstrip("\n")
            core = re.sub(r",\s*quarantine:\s*(\{[^{}]*\}|[^,{}]*)(?=\s*[,}])", "", core)   # drop the old one
            if q:
                pos = core.rfind("}")
                core = core[:pos] + ", quarantine: " + _flow(q) + core[pos:]
            return "".join(lines[:i] + [core + nl] + lines[i + 1:])
        m = re.match(rf"^(\s*)-\s+(['\"]?){nq}\2\s*(#.*)?$", ln.rstrip("\n"))
        if m:
            if not q:
                return text
            return "".join(lines[:i] + [f"{m.group(1)}- {{name: {node}, quarantine: {_flow(q)}}}\n"] + lines[i + 1:])
    return None


def set_quarantine(text: str, node: str, enabled: bool, reason: str = "", until: str | None = None,
                   since: str | None = None) -> tuple[str, bool]:
    """(new text, comments kept). Raises EditError on a bad request."""
    data = yaml.safe_load(text) or {}
    key, idx = _find_node(data, node)
    q = None
    if enabled:
        reason = (reason or "").strip()
        errs = []
        if not reason:
            errs.append("a quarantine needs a reason")
        if until and not DATE_RX.match(until):
            errs.append("until must be YYYY-MM-DD")
        if until and DATE_RX.match(until) and until < dt.date.today().isoformat():
            errs.append("until is in the past")
        if errs:
            raise EditError(errs)
        q = {"reason": reason, "since": since or dt.date.today().isoformat()}
        if until:
            q["until"] = until
    expected = copy.deepcopy(data)
    e = expected[key][idx]
    e = {"name": e} if isinstance(e, str) else e
    expected[key][idx] = e
    if q:
        e["quarantine"] = q
    else:
        e.pop("quarantine", None)
    new = _patch_text(text, key, node, q)
    if new is not None:
        try:
            if yaml.safe_load(new) == expected:
                return new, True
        except yaml.YAMLError:
            pass
    return _dump(expected), False


# ------------------------------------------------------------------ read / save

def read(path: Path) -> dict:
    if not path.is_file():
        return {"path": str(path), "exists": False}
    text = path.read_text()
    errors, warnings, data = validate(text, path)
    out = {"path": str(path), "exists": True, "text": text, "sha": sha(text), "errors": errors, "warnings": warnings}
    if data:
        out["form"] = to_form(data)
        try:
            out["quarantine"] = load_inventory(path).quarantine_summary()
        except Exception:  # noqa: BLE001
            out["quarantine"] = {}
    return out


def prepare(path: Path, req: dict) -> dict:
    """New text from {text} (raw YAML) or {form}, or a quarantine toggle {quarantine: {node, enabled, reason,
    until}}; returns {text, diff, errors, warnings, comments_kept}."""
    old = path.read_text()
    kept = True
    if "quarantine" in req:
        qr = req["quarantine"] or {}
        new, kept = set_quarantine(old, str(qr.get("node") or ""), bool(qr.get("enabled")), qr.get("reason") or "",
                                   (qr.get("until") or "").strip() or None)
    elif "form" in req:
        new = _dump(from_form(yaml.safe_load(old) or {}, req["form"]))
        kept = False
    elif "text" in req:
        new = str(req["text"])
    else:
        raise EditError(["send text, form or quarantine"])
    errors, warnings, _ = validate(new, path)
    if not kept:
        warnings.append("the YAML is re-written: comments and formatting of the file are not kept")
    return {"text": new, "diff": diff(old, new, path.name), "errors": errors, "warnings": warnings,
            "comments_kept": kept, "sha": sha(old)}


def save(path: Path, req: dict) -> dict:
    """Validate, then write a dated backup (<file>.bak-YYYYmmdd-HHMMSS) and the new file. ``sha`` must match the
    file as it was loaded (someone else may have changed it)."""
    current = path.read_text()
    if req.get("sha") and req["sha"] != sha(current):
        raise EditError(["the file changed since it was loaded - reload and redo the edit"])
    res = prepare(path, req)
    if res["errors"]:
        raise EditError(res["errors"])
    if res["text"] == current:
        return {**res, "saved": False, "backup": None}
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
        n += 1
    backup.write_text(current)
    tmp = path.with_name(f".{path.name}.ui-tmp")
    tmp.write_text(res["text"])
    os.replace(tmp, path)
    return {**res, "saved": True, "backup": str(backup), "sha": sha(res["text"])}
