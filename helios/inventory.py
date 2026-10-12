"""Rack inventory: hosts, credentials, thresholds and command templates.

Secrets never need to live in the YAML: any ``password`` can be given as
``password_env: VAR`` and is read from the environment at load time.
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

    return Inventory(
        path=p,
        rack=str(data.get("rack", p.stem)),
        switches=_hosts(data.get("switches"), "switch", {**common, **(defaults.get("switch") or {})}),
        computes=_hosts(data.get("computes"), "compute", {**common, **(defaults.get("compute") or {})}),
        afm=data.get("afm") or {},
        transport=transport,
        thresholds=data.get("thresholds") or {},
        commands=data.get("commands") or {},
    )
