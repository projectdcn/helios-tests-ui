"""Rack profiles (ui/racks.yaml): which inventory, where results go, local or jump-host execution. No secrets."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path(__file__).resolve().parent / "racks.yaml"

# keys that would hold a secret value; ``*_env`` (the NAME of an environment variable) and key_file are fine
SECRET_KEY = re.compile(r"^(password|passwd|pass|secret|token|api_key|apikey|private_key|privatekey)$", re.I)


class ConfigError(ValueError):
    pass


@dataclass
class SshProfile:
    host: str
    port: int = 22
    user: str = ""
    code_dir: str = "~/helios/helios-tests"
    python: str = "~/helios/venv/bin/python"
    inventory: str = "~/helios/inventory.yaml"
    ref: str = "HEAD"
    remote_html: bool = False
    options: list[str] = field(default_factory=list)   # extra ssh -o options

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


@dataclass
class RackProfile:
    name: str
    title: str
    runner: str                     # local | ssh
    inventory: Path                 # local inventory file (for ssh: the copy pushed to the jump host)
    results_dir: Path
    known: str | None = None        # --known for health runs (repo-relative)
    ssh: SshProfile | None = None
    workbook: Path | None = None

    def as_dict(self) -> dict:
        d = {"name": self.name, "title": self.title, "runner": self.runner, "inventory": str(self.inventory),
             "inventory_exists": self.inventory.is_file(), "results_dir": str(self.results_dir),
             "known": self.known, "password_env": password_env(self.inventory),
             "workbook": str(self.workbook) if self.workbook else None,
             "workbook_exists": bool(self.workbook and self.workbook.is_file())}
        if self.ssh:
            d["ssh"] = {"target": self.ssh.target, "port": self.ssh.port, "code_dir": self.ssh.code_dir,
                        "inventory": self.ssh.inventory, "ref": self.ssh.ref}
        return d


def _path(v: str | None, root: Path) -> Path | None:
    if v is None:
        return None
    p = Path(str(v)).expanduser()
    return p if p.is_absolute() else root / p


def find_secrets(tree, path: str = "") -> list[str]:
    """Dotted paths of keys that carry a secret VALUE (password: x, token: y) anywhere in a YAML tree."""
    out = []
    if isinstance(tree, dict):
        for k, v in tree.items():
            p = f"{path}.{k}" if path else str(k)
            if SECRET_KEY.match(str(k)) and v not in (None, ""):
                out.append(p)
            out += find_secrets(v, p)
    elif isinstance(tree, list):
        for i, v in enumerate(tree):
            out += find_secrets(v, f"{path}[{i}]")
    return out


def password_env(inventory: Path) -> str:
    """Environment variable the inventory reads the AFM password from (afm.auth.password_env)."""
    try:
        data = yaml.safe_load(inventory.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return "HELIOS_AFM_PASSWORD"
    auth = ((data.get("afm") or {}).get("auth") or {}) if isinstance(data, dict) else {}
    return str(auth.get("password_env") or "HELIOS_AFM_PASSWORD")


def afm_username(inventory: Path) -> str:
    """AFM user of the inventory (afm.auth.username, default admin): part of the Keychain account name."""
    try:
        data = yaml.safe_load(inventory.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return "admin"
    auth = ((data.get("afm") or {}).get("auth") or {}) if isinstance(data, dict) else {}
    return str(auth.get("username") or "admin")


def inventory_rack_name(inventory: Path) -> str:
    try:
        data = yaml.safe_load(inventory.read_text()) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    return str(data.get("rack") or inventory.stem) if isinstance(data, dict) else inventory.stem


def load_config(path: Path | None = None, root: Path = ROOT) -> dict[str, RackProfile]:
    path = Path(path or DEFAULT_CONFIG)
    data = yaml.safe_load(path.read_text()) or {}
    bad = find_secrets(data)
    if bad:
        raise ConfigError(f"{path}: rack profiles must not hold secrets ({', '.join(bad)})")
    out: dict[str, RackProfile] = {}
    for name, r in (data.get("racks") or {}).items():
        if not re.fullmatch(r"[A-Za-z0-9._-]+", str(name)):
            raise ConfigError(f"rack profile name {name!r}: letters, digits, '.', '_', '-' only")
        runner = r.get("runner", "local")
        if runner not in ("local", "ssh"):
            raise ConfigError(f"{name}: runner must be local or ssh")
        ssh = None
        if runner == "ssh":
            s = dict(r.get("ssh") or {})
            if not s.get("host"):
                raise ConfigError(f"{name}: ssh.host is required for runner: ssh")
            ssh = SshProfile(host=str(s["host"]), port=int(s.get("port", 22)), user=str(s.get("user", "")),
                             code_dir=str(s.get("code_dir", SshProfile.code_dir)),
                             python=str(s.get("python", SshProfile.python)),
                             inventory=str(s.get("inventory", SshProfile.inventory)),
                             ref=str(s.get("ref", "HEAD")), remote_html=bool(s.get("remote_html", False)),
                             options=[str(o) for o in s.get("options") or []])
        if not r.get("inventory"):
            raise ConfigError(f"{name}: inventory is required")
        out[str(name)] = RackProfile(
            name=str(name), title=str(r.get("title", name)), runner=runner,
            inventory=_path(r["inventory"], root), results_dir=_path(r.get("results_dir", "results"), root),
            known=r.get("known"), ssh=ssh, workbook=_path(r.get("workbook"), root))
    return out
