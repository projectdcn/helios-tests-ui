"""Rack inventory: hosts, credentials, thresholds and command templates.

Secrets never need to live in the YAML: any ``password`` can be given as
``password_env: VAR`` and is read from the environment at load time.

Quarantine: a switch or compute entry with ``quarantine: <reason>`` (or ``{reason, since, until}``) stays in the file
but is left out of ``Inventory.switches`` / ``computes`` - every test skips it - and is listed in
``Inventory.quarantined``. A quarantined compute's AFM slot (``slot:``, the AFM ComputeNode spec slot-id) lets the
AFM-side and fabric checks drop its AFM node, node-agent slot and switch ports too. Passing tests are then reported
PARTIAL (quarantined), never plain PASS.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_HOST_KEYS = ("name", "host", "user", "port", "key_file", "password", "password_env")


class InventoryError(ValueError):
    pass


@dataclass
class Host:
    name: str
    host: str
    role: str
    user: str = "admin"
    port: int = 22
    key_file: str | None = None
    password: str | None = field(default=None, repr=False)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def quarantine(self) -> dict | None:
        """{reason, since, until} when the node is quarantined, else None."""
        q = self.extra.get("quarantine")
        if not q:
            return None
        q = {"reason": str(q)} if not isinstance(q, dict) else {k: str(v) for k, v in q.items() if v is not None}
        q.setdefault("reason", "quarantined")
        return q


@dataclass
class Inventory:
    path: Path
    rack: str
    switches: list[Host]
    computes: list[Host]
    afm: dict[str, Any]
    transport: dict[str, Any]
    thresholds: dict[str, Any]
    commands: dict[str, Any]
    quarantined: list[Host] = field(default_factory=list)

    @property
    def all_switches(self) -> list[Host]:
        return self.switches + [h for h in self.quarantined if h.role == "switch"]

    @property
    def all_computes(self) -> list[Host]:
        return self.computes + [h for h in self.quarantined if h.role == "compute"]

    def quarantine_summary(self) -> dict[str, dict]:
        """{node name: {role, reason, since, until, slot, expired}} of the quarantined nodes."""
        import datetime as _dt
        today = _dt.date.today().isoformat()
        out = {}
        for h in self.quarantined:
            q = dict(h.quarantine or {})
            q["role"] = h.role
            if h.extra.get("slot") is not None:
                q["slot"] = h.extra.get("slot")
            q["expired"] = bool(q.get("until") and str(q["until"])[:10] < today)
            out[h.name] = q
        return out

    def quarantined_slots(self) -> set[int]:
        """AFM slot-ids of quarantined compute trays (inventory ``slot:``)."""
        return {int(h.extra["slot"]) for h in self.quarantined
                if h.role == "compute" and str(h.extra.get("slot", "")).lstrip("-").isdigit()}

    def quarantined_afm_names(self) -> set[str]:
        return {str(h.extra["afm_name"]) for h in self.quarantined if h.extra.get("afm_name")}

    def is_quarantined_afm(self, obj: dict) -> bool:
        """An AFM SwitchNode / ComputeNode object that belongs to a quarantined node (by afm_name, compute slot-id or
        switch management IP)."""
        if not self.quarantined:
            return False
        name = ((obj.get("meta") or {}).get("name")) or ""
        if name in self.quarantined_afm_names():
            return True
        spec = obj.get("spec") or {}
        if obj.get("kind") == "ComputeNode" or "ComputeTray" in name:
            sid = spec.get("slot-id")
            return sid is not None and str(sid).isdigit() and int(sid) in self.quarantined_slots()
        ips = {h.host for h in self.quarantined if h.role == "switch"}
        return bool(ips) and any(str(v) in ips for v in (spec.get("ip-address"), spec.get("mgmt-ip"),
                                                       (obj.get("status") or {}).get("ip-address")) if v)

    def th(self, dotted: str, default: Any = None) -> Any:
        """Threshold lookup. An explicit ``null`` in YAML returns None (= don't check)."""
        return _lookup(self.thresholds, dotted, default)

    def cmd(self, dotted: str, default: Any = None) -> Any:
        return _lookup(self.commands, dotted, default)


def _lookup(tree: dict, dotted: str, default: Any) -> Any:
    node: Any = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _secret(entry: dict, key: str) -> str | None:
    if entry.get(key) is not None:
        return str(entry[key])
    env = entry.get(f"{key}_env")
    return os.environ.get(env) if env else None


def _hosts(entries: list | None, role: str, defaults: dict) -> list[Host]:
    hosts: list[Host] = []
    for raw in entries or []:
        if isinstance(raw, str):
            raw = {"name": raw}
        merged = {**defaults, **raw}
        if "name" not in merged:
            raise InventoryError(f"{role} entry missing 'name': {raw}")
        hosts.append(
            Host(
                name=str(merged["name"]),
                host=str(merged.get("host", merged["name"])),
                role=role,
                user=str(merged.get("user", "admin")),
                port=int(merged.get("port", 22)),
                key_file=merged.get("key_file"),
                password=_secret(merged, "password"),
                extra={k: v for k, v in merged.items() if k not in _HOST_KEYS},
            )
        )
    names = [h.name for h in hosts]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        raise InventoryError(f"duplicate {role} names: {dups}")
    return hosts


def load_inventory(path: str | os.PathLike) -> Inventory:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise InventoryError(f"inventory not found: {p}")
    data = yaml.safe_load(p.read_text()) or {}
    defaults = data.get("defaults") or {}
    common = defaults.get("common") or {}

    transport = dict(data.get("transport") or {"kind": "ssh"})
    if transport.get("kind") == "replay" and "dir" in transport:
        transport["dir"] = str((p.parent / transport["dir"]).resolve())

    switches = _hosts(data.get("switches"), "switch", {**common, **(defaults.get("switch") or {})})
    computes = _hosts(data.get("computes"), "compute", {**common, **(defaults.get("compute") or {})})
    quarantined = [h for h in switches + computes if h.quarantine]
    return Inventory(
        path=p,
        rack=str(data.get("rack", p.stem)),
        switches=[h for h in switches if not h.quarantine],
        computes=[h for h in computes if not h.quarantine],
        quarantined=quarantined,
        afm=data.get("afm") or {},
        transport=transport,
        thresholds=data.get("thresholds") or {},
        commands=data.get("commands") or {},
    )
