"""Inventory editor: a form model of the inventory YAML, validation (the real loader + uniqueness + no secrets),
diff preview, dated backup before every save, and the per-node quarantine toggle (``quarantine: {reason, since,
until}`` as helios/inventory.py and helios/quarantine.py read it).

Saving from the raw YAML text keeps comments. A form save re-writes only what changed (``merge_text``): unchanged
top-level sections and unchanged switch / compute entries keep their text and comments; a changed one is re-written
(comments inside it are lost - the preview says which). A layout it cannot split re-writes the whole file. The quarantine toggle edits only the node's lines when it can, so comments elsewhere survive.
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


class _LeafFlowDumper(yaml.SafeDumper):
    """Block style, but a mapping / list holding only scalars on one line ({dev: x, gpu: 0}) - as the inventories
    are written by hand."""


def _leaf(v) -> bool:
    vals = v.values() if isinstance(v, dict) else v
    return bool(vals) and all(not isinstance(x, (dict, list)) for x in vals)


_LeafFlowDumper.add_representer(dict, lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v,
                                                                       flow_style=_leaf(v)))
_LeafFlowDumper.add_representer(list, lambda d, v: d.represent_sequence("tag:yaml.org,2002:seq", v,
                                                                        flow_style=_leaf(v)))


def _dump_entry(e) -> str:
    """One switch / compute entry as a list item: its keys block style, collections of scalars on one line."""
    if not isinstance(e, dict):
        return _dump([e])
    out = []
    for k, v in e.items():
        if isinstance(v, (dict, list)) and v and not _leaf(v):      # nested: the key, then the value indented
            sub = yaml.dump(v, Dumper=_LeafFlowDumper, sort_keys=False, width=120, allow_unicode=True)
            out.append(_dump({k: []}).replace(" []", "", 1) + "".join("  " + ln for ln in sub.splitlines(True)))
        elif isinstance(v, (dict, list)) and v:
            out.append(_dump({k: []}).replace("[]", _flow(v), 1))
        else:
            out.append(_dump({k: v}))
    body = "".join(out).splitlines(keepends=True)
    return "".join(("- " if i == 0 else "  ") + ln for i, ln in enumerate(body))


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
    for where, kf in _key_files(data):     # e.g. an imported file made on a laptop: its key paths are not here
        f = Path(os.path.expandvars(str(kf))).expanduser()
        f = f if f.is_absolute() else path.parent / f
        if not f.is_file():
            warnings.append(f"{where}: key_file {kf} does not exist on this server - SSH falls back to the default "
                            "keys; fix the path or remove it (Setup tab: the server's keys)")
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


def _key_files(obj, where: str = "") -> list[tuple[str, str]]:
    """(where, path) of every key_file value in the inventory (entries, transport, defaults ...)."""
    out = []
    if isinstance(obj, dict):
        label = str(obj.get("name") or where or "inventory")
        for k, v in obj.items():
            if k == "key_file" and isinstance(v, str) and v.strip():
                out.append((label, v.strip()))
            elif isinstance(v, (dict, list)):
                out += _key_files(v, f"{where}.{k}".strip(".") if not obj.get("name") else label)
    elif isinstance(obj, list):
        for x in obj:
            out += _key_files(x, where)
    return out


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


# ------------------------------------------------------------------ form save: re-write only what changed

_TOP_RX = re.compile(r"^([A-Za-z_][\w.-]*)\s*:")


def _blocks(lines: list[str], start_idx: list[int], may_lead) -> list[tuple[int, int, int]]:
    """(lead, start, end) per block: the comment / blank lines right above a start line (``may_lead``) belong to it."""
    out = []
    for n, st in enumerate(start_idx):
        lead = st
        floor = out[-1][1] + 1 if out else 0
        while lead > floor and may_lead(lines[lead - 1]):
            lead -= 1
        out.append([lead, st, None])
    for n, b in enumerate(out):
        b[2] = out[n + 1][0] if n + 1 < len(out) else len(lines)
    return [tuple(b) for b in out]


def _name(e):
    return e if isinstance(e, str) else (e or {}).get("name") if isinstance(e, dict) else None


def _merge_list(key: str, body: list[str], old: list, new: list, lost: list[str]) -> list[str] | None:
    """A switches / computes section with only its changed entries re-written (matched by name)."""
    if not body or body[0].split("#")[0].strip() != f"{key}:":
        return None                                    # flow list or value on the key line
    items = [i for i in range(1, len(body)) if re.match(r"^\s*-(\s|$)", body[i])]
    if not items:
        return None
    ind = min(len(body[i]) - len(body[i].lstrip()) for i in items)
    items = [i for i in items if len(body[i]) - len(body[i].lstrip()) == ind]
    if any(body[i].strip() and not body[i].lstrip().startswith("#") for i in range(1, items[0])):
        return None
    blocks = _blocks(body, items, lambda ln: not ln.strip() or
                     (ln.lstrip().startswith("#") and len(ln) - len(ln.lstrip()) <= ind))
    if len(blocks) != len(old if isinstance(old, list) else []):
        return None
    by_name = {}
    for (lead, st, end), e in zip(blocks, old):
        try:
            if yaml.safe_load("".join(body[st:end])) != [e]:
                return None
        except yaml.YAMLError:
            return None
        by_name.setdefault(_name(e), (body[lead:end], e))
    out = body[:blocks[0][0]]
    for e in new:
        prev = by_name.get(_name(e))
        if prev and prev[1] == e:
            out += prev[0]
            continue
        if prev and any("#" in ln for ln in prev[0]):
            lost.append(f"{key} {_name(e)}")
        text = "".join(" " * ind + ln if ln.strip() else ln for ln in _dump_entry(e).splitlines(keepends=True))
        out += text.splitlines(keepends=True)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    return out


def merge_text(old_text: str, old_data: dict, new_data: dict) -> tuple[str, list[str]] | None:
    """``new_data`` written over ``old_text`` keeping the text (and comments) of everything unchanged: top-level
    sections, and single switch / compute entries. Returns (text, what lost comments) or None when the layout is
    not one this splits safely (anchors, flow style, documents ...): the caller then re-writes the whole file."""
    lines = old_text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    if any(ln.startswith(("---", "...", "%")) or re.search(r"(^|[:\-\[,{]\s*)[&*][\w-]", ln.split(" #")[0])
           for ln in lines):                          # documents, anchors / aliases: whole re-write
        return None
    starts = [i for i, ln in enumerate(lines) if _TOP_RX.match(ln)]
    if not starts or not isinstance(old_data, dict) or not isinstance(new_data, dict):
        return None
    blocks = _blocks(lines, starts, lambda ln: not ln.strip() or ln.startswith("#"))
    sections = {}
    for lead, st, end in blocks:
        key = _TOP_RX.match(lines[st]).group(1)
        try:
            if key in sections or key not in old_data or yaml.safe_load("".join(lines[st:end])) != {key: old_data[key]}:
                return None
        except yaml.YAMLError:
            return None
        sections[key] = (lines[lead:st], lines[st:end])
    if set(sections) != set(old_data):
        return None
    out, lost = lines[:blocks[0][0]], []
    for key, (lead, body) in sections.items():
        if key not in new_data:
            continue
        if new_data[key] == old_data[key]:
            out += lead + body
            continue
        merged = None
        if dict(ROLES).get(key) and isinstance(new_data[key], list):
            merged = _merge_list(key, body, old_data[key], new_data[key], lost)
        if merged is None:
            if any("#" in ln for ln in body):
                lost.append(key)
            merged = _dump({key: new_data[key]}).splitlines(keepends=True)
        out += lead + merged
    for key, v in new_data.items():
        if key not in sections:
            out += _dump({key: v}).splitlines(keepends=True)
    text = "".join(out)
    try:
        if yaml.safe_load(text) != new_data:
            return None
    except yaml.YAMLError:
        return None
    return text, lost


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

def _current(path: Path) -> str:
    """The file's text; "" when it does not exist yet (a new rack: the editor creates it on Save)."""
    return path.read_text() if path.is_file() else ""


def read(path: Path) -> dict:
    if not path.is_file():   # starting from scratch: an empty form (with its column headers), saved as a new file
        return {"path": str(path), "exists": False, "text": "", "sha": sha(""), "errors": [], "warnings": [],
                "form": to_form({})}
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


def _slotless_quarantined(text: str) -> set[str]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return set()
    return {str(e.get("name")) for e in _entries(data, "computes")
            if e.get("quarantine") and e.get("slot") is None and not e.get("afm_name")}


def prepare(path: Path, req: dict) -> dict:
    """New text from {text} (raw YAML) or {form}, or a quarantine toggle {quarantine: {node, enabled, reason,
    until}}; returns {text, diff, errors, warnings, comments_kept}."""
    old = _current(path)
    partial = ""
    if not old:
        path.parent.mkdir(parents=True, exist_ok=True)   # validate() loads a copy next to where the file will be
    kept = True
    if "quarantine" in req:
        qr = req["quarantine"] or {}
        new, kept = set_quarantine(old, str(qr.get("node") or ""), bool(qr.get("enabled")), qr.get("reason") or "",
                                   (qr.get("until") or "").strip() or None)
    elif "form" in req:
        old_data = yaml.safe_load(old) or {}
        new_data = from_form(old_data, req["form"])
        merged = merge_text(old, old_data, new_data) if old.strip() else None
        if merged:
            new, lost = merged
            kept = not lost
            if lost:
                partial = f"comments inside the changed {', '.join(lost)} are not kept; the rest of the file is unchanged"
        else:
            new, kept = _dump(new_data), False
    elif "text" in req:
        new = str(req["text"])
    else:
        raise EditError(["send text, form or quarantine"])
    errors, warnings, _ = validate(new, path)
    # setting a quarantine on a compute without slot / afm_name is refused (g02-g17 17 Oct: saved like that, the AFM
    # pre-flight still saw pod-1-4 = ct05 and blocked every AFM test); one already in the file only warns
    for n in sorted(_slotless_quarantined(new) - _slotless_quarantined(old)):
        errors.append(f"{n}: a quarantined compute needs its AFM 'slot' (slot-id, e.g. ct05 = 4) or 'afm_name' - "
                      "without it the AFM checks cannot tell which AFM node to leave out; set it first (Compute trays "
                      "table or raw YAML), then quarantine")
    if not kept:
        warnings.append(partial if partial else "the YAML is re-written: comments and formatting of the file are not kept")
    return {"text": new, "diff": diff(old, new, path.name), "errors": errors, "warnings": warnings,
            "comments_kept": kept, "sha": sha(old)}


def save(path: Path, req: dict) -> dict:
    """Validate, then write a dated backup (<file>.bak-YYYYmmdd-HHMMSS) and the new file. ``sha`` must match the
    file as it was loaded (someone else may have changed it). A file that does not exist yet is created (no
    backup)."""
    existed = path.is_file()
    current = _current(path)
    if req.get("sha") and req["sha"] != sha(current):
        raise EditError(["the file changed since it was loaded - reload and redo the edit"])
    res = prepare(path, req)
    if res["errors"]:
        raise EditError(res["errors"])
    if existed and res["text"] == current:
        return {**res, "saved": False, "backup": None}
    backup = None
    if existed:
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        n = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
            n += 1
        backup.write_text(current)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.ui-tmp")
    tmp.write_text(res["text"])
    os.replace(tmp, path)
    return {**res, "saved": True, "backup": str(backup) if backup else None, "sha": sha(res["text"])}
