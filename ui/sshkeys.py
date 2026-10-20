"""SSH keys of the UI server, managed from the Setup tab (no shell on the server needed).

Per rack: which key the runs use (chosen here -> <results_dir>/ui-sshkey.json; else $HELIOS_SSH_KEY, the
inventory's key_file, the server's own ~/.ssh setup - helios/transport.resolve_keys), the server's keys (~/.ssh/*.pub
with their private key), creating one (ssh-keygen, ed25519, no passphrase: the service has no agent), and installing
the public key on the rack: the nodes of a direct rack, the jump host of a jump-host rack. Installing logs in once
with the account's password (typed in the page, used for that one request, never stored or logged), appends the key
to ~/.ssh/authorized_keys if it is not there, and checks that key login then works. The private key never leaves
the server.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
from pathlib import Path

from .config import RackProfile
from .runner import JobError

STATE = "ui-sshkey.json"
NAME_RX = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DEFAULT_NAME = "id_ed25519_helios"


def ssh_dir() -> Path:
    return Path.home() / ".ssh"


def chosen(p: RackProfile) -> str | None:
    """The key this rack's runs use when chosen in the UI (absolute path), else None."""
    try:
        k = json.loads((p.results_dir / STATE).read_text()).get("key")
    except (OSError, ValueError, AttributeError):
        return None
    return str(k) if k else None


def _key_ok(path: Path) -> bool:
    return path.is_file() and Path(str(path) + ".pub").is_file()


def choose(p: RackProfile, path: str | None) -> None:
    if path:
        k = Path(path).expanduser()
        if k.parent.resolve() != ssh_dir().resolve() or not _key_ok(k):
            raise JobError(f"{path}: not a key of this server (a private key in {ssh_dir()} with its .pub)")
        path = str(k)
    p.results_dir.mkdir(parents=True, exist_ok=True)
    tmp = p.results_dir / f".{STATE}.tmp"
    tmp.write_text(json.dumps({"key": path}) + "\n")
    os.replace(tmp, p.results_dir / STATE)


def _pub_info(pub: Path) -> dict:
    text = pub.read_text().strip()
    parts = text.split()
    return {"path": str(pub)[:-4], "pub": text, "type": parts[0] if parts else "?",
            "comment": " ".join(parts[2:]) if len(parts) > 2 else ""}


def list_keys() -> list[dict]:
    d = ssh_dir()
    if not d.is_dir():
        return []
    out = []
    for pub in sorted(d.glob("*.pub")):
        if _key_ok(Path(str(pub)[:-4])):
            try:
                out.append(_pub_info(pub))
            except OSError:
                pass
    return out


def create(name: str = DEFAULT_NAME) -> dict:
    """A new ed25519 key without passphrase in ~/.ssh (an existing one of that name is kept and returned)."""
    if not NAME_RX.match(name or ""):
        raise JobError("key name: letters, digits, . _ - only")
    d = ssh_dir()
    d.mkdir(mode=0o700, exist_ok=True)
    path = d / name
    if _key_ok(path):
        return _pub_info(Path(str(path) + ".pub"))
    if path.exists() or Path(str(path) + ".pub").exists():
        raise JobError(f"{path} exists without its pair: choose another name")
    comment = f"helios-ui@{socket.gethostname().split('.')[0]}"
    try:
        r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise JobError("ssh-keygen is not installed on this server (package openssh-client)") from None
    if r.returncode != 0:
        raise JobError(f"ssh-keygen failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return _pub_info(Path(str(path) + ".pub"))


FP_RX = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")


def known_hosts() -> Path:
    return Path("~/.ssh/known_hosts").expanduser()


def _presented_key(t: dict):
    """The host key the node presents now (no login)."""
    import paramiko
    tr = paramiko.Transport((t["host"], t["port"]))
    try:
        tr.start_client(timeout=15)
        return tr.get_remote_server_key()
    finally:
        tr.close()


def accept_host_key(p: RackProfile, node: str, fp: str, by: str = "") -> dict:
    """Replace the known_hosts record of one of the rack's nodes with the key it presents NOW - only when that key
    has the fingerprint the person confirmed (read again here: what they checked is what gets trusted). known_hosts
    is backed up first; every change is logged in ~/.ssh/helios-ui-hostkeys.log. 20 Oct: srv03 had a stale record
    for MR3 c2."""
    from helios.transport import fingerprint
    if not isinstance(fp, str) or not FP_RX.match(fp):
        raise JobError("fingerprint: SHA256:... as shown in the message")
    t = next((x for x in targets(p) if x["name"] == node), None)
    if t is None:
        raise JobError(f"{node!r} is not a node of this rack")
    key = _presented_key(t)
    now = fingerprint(key)
    if now != fp:
        raise JobError(f"{node} ({t['host']}) now presents {now}, not the {fp} you confirmed: nothing changed - "
                       "check the key on the host again")
    import datetime as _dt
    import shutil
    import paramiko
    kh = known_hosts()
    name = t["host"] if t["port"] == 22 else f"[{t['host']}]:{t['port']}"
    old, backup = [], None
    if kh.is_file():
        try:
            hk = paramiko.HostKeys(str(kh))
            old = sorted({fingerprint(k) for k in (hk.lookup(name) or {}).values()})
        except Exception:  # noqa: BLE001 - an unreadable line: ssh-keygen -R still handles the file
            pass
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = kh.with_name(f"known_hosts.bak-{stamp}")
        shutil.copy2(kh, backup)
        r = subprocess.run(["ssh-keygen", "-R", name, "-f", str(kh)], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise JobError(f"ssh-keygen -R {name} failed: {(r.stderr or r.stdout).strip()[-200:]}")
        kh.with_name("known_hosts.old").unlink(missing_ok=True)      # ssh-keygen's own copy: ours is dated
    else:
        kh.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(kh, "a") as f:
        f.write(f"{name} {key.get_name()} {key.get_base64()}\n")
    os.chmod(kh, 0o600)
    with open(kh.with_name("helios-ui-hostkeys.log"), "a") as f:
        f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')} {p.name} {node} {name}: "
                f"{', '.join(old) or '(no record)'} -> {now}{' by ' + by if by else ''}\n")
    return {"node": node, "host": name, "fingerprint": now, "replaced": old,
            "backup": str(backup) if backup else None}


def targets(p: RackProfile) -> list[dict]:
    """Where the key must be accepted: the jump host (jump-host rack), else every switch and compute."""
    if p.runner == "ssh" and p.ssh:
        user, _, host = p.ssh.target.rpartition("@")
        return [{"name": "jump host", "role": "jump", "user": user or os.environ.get("USER", ""), "host": host,
                 "port": int(p.ssh.port)}]
    from helios.inventory import load_inventory
    inv = load_inventory(p.inventory)
    return [{"name": h.name, "role": h.role, "user": h.user or "", "host": h.host, "port": int(h.port or 22)}
            for h in [*inv.switches, *inv.computes]]


def using(p: RackProfile) -> dict:
    """Which key this rack's runs use and why (for a direct rack: as the first node resolves it)."""
    c = chosen(p)
    if c:
        return {"path": c, "source": "chosen here", "exists": _key_ok(Path(c))}
    if p.runner == "ssh":
        return {"path": None, "source": "this server's own SSH setup for the jump host (~/.ssh/config, "
                                        "~/.ssh/id_ed25519, id_rsa)", "exists": True}
    from helios.transport import resolve_keys
    try:
        from helios.inventory import load_inventory
        inv = load_inventory(p.inventory)
        h = next(iter([*inv.switches, *inv.computes]), None)
        files, src = resolve_keys(h.key_file, h.name, h.host) if h else (None, "no nodes in the inventory")
        return {"path": ", ".join(files) if files else None, "source": src, "exists": True}
    except FileNotFoundError as e:
        return {"path": None, "source": str(e), "exists": False}
    except Exception as e:  # noqa: BLE001 - e.g. no inventory yet
        return {"path": None, "source": f"cannot tell: {e}", "exists": False}


def _connect(t: dict, *, password: str | None = None, key: str | None = None):
    import paramiko
    c = paramiko.SSHClient()
    c.load_system_host_keys()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(t["host"], port=t["port"], username=t["user"], password=password, key_filename=key,
              look_for_keys=False, allow_agent=False, timeout=15, banner_timeout=15, auth_timeout=15)
    return c


def _key_login(t: dict, key: str) -> str | None:
    """None when key-only login works, else why not."""
    try:
        c = _connect(t, key=key)
    except Exception as e:  # noqa: BLE001
        from helios.transport import host_key_changed
        return host_key_changed(e, t["host"]) or f"{type(e).__name__}: {str(e)[:160]}"
    try:
        _, out, _ = c.exec_command("true", timeout=20)
        return None if out.channel.recv_exit_status() == 0 else "logged in, but 'true' failed"
    finally:
        c.close()


def _auth_methods(t: dict) -> list[str] | None:
    """The login methods the account's server offers (no credentials sent), or None when it cannot be asked."""
    import paramiko
    try:
        tr = paramiko.Transport((t["host"], t["port"]))
        tr.start_client(timeout=10)
    except Exception:  # noqa: BLE001
        return None
    try:
        tr.auth_none(t["user"])
        return []
    except paramiko.BadAuthenticationType as e:
        return list(e.allowed_types)
    except Exception:  # noqa: BLE001
        return None
    finally:
        tr.close()


def _password_failure(t: dict, e: Exception) -> str:
    """Why the one-time password login failed, with what to try. 17 Oct, MR3 st1: 'wrong password?' while the user's
    own 'ssh admin@st1' worked - that ssh used a key, so it never checked the password."""
    acct = f"{t['user']}@{t['host']}"
    from helios.transport import host_key_changed
    hk = host_key_changed(e, t["host"])
    if hk:
        return f"cannot log in to {acct}: {hk}"
    if type(e).__name__ not in ("AuthenticationException", "BadAuthenticationType"):
        return f"cannot log in to {acct} ({type(e).__name__}: {str(e)[:160]})"
    methods = _auth_methods(t)
    if methods is not None and "password" not in methods and "keyboard-interactive" not in methods:
        return (f"{acct} does not accept password logins (it allows: {', '.join(methods) or 'none'}): add the public "
                "key there by hand (Copy public key, then append it to ~/.ssh/authorized_keys of that account)")
    return (f"{acct} refused this password (password login is enabled there): wrong password, or the account is "
            "locked. If your own 'ssh' to it works without asking, it logs in with a key and never checks the "
            f"password - test the password with: ssh -o PubkeyAuthentication=no {acct}")


def install(p: RackProfile, key: str, passwords: dict[str, str], names: list[str] | None = None,
            node_passwords: dict[str, str] | None = None) -> list[dict]:
    """Add ``key``.pub to every target's authorized_keys (password login once per account), then check key login.
    ``passwords``: {user: password}; ``node_passwords``: {node name: password} - typed in the node's own row, wins
    over the account's. Per target {name, user, host, status: installed / already / failed, detail}."""
    node_passwords = node_passwords or {}
    k = Path(key).expanduser()
    if k.parent.resolve() != ssh_dir().resolve() or not _key_ok(k):
        raise JobError(f"{key}: not a key of this server")
    pub = Path(str(k) + ".pub").read_text().strip()
    if not re.fullmatch(r"(ssh|ecdsa)-[\w@.-]+ [A-Za-z0-9+/=]+( [\w@.+-]*)?", pub):
        raise JobError(f"{k}.pub does not look like an OpenSSH public key")
    q = shlex.quote(pub)
    cmd = ("umask 077 && mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
           f"(grep -qxF {q} ~/.ssh/authorized_keys || echo {q} >> ~/.ssh/authorized_keys) && echo ok")
    out = []
    for t in targets(p):
        if names and t["name"] not in names:
            continue
        row = {"name": t["name"], "user": t["user"], "host": t["host"]}
        if _key_login(t, str(k)) is None:
            out.append({**row, "status": "already", "detail": "key login already works"})
            continue
        pw = node_passwords.get(t["name"]) or passwords.get(t["user"])
        if not pw:
            out.append({**row, "status": "failed",
                        "detail": f"no password given for {t['user']} (type it in this row, or for every {t['user']} below)"})
            continue
        try:
            c = _connect(t, password=pw)
        except Exception as e:  # noqa: BLE001 - the password never appears in paramiko's messages
            out.append({**row, "status": "failed", "detail": _password_failure(t, e)})
            continue
        try:
            _, so, se = c.exec_command(cmd, timeout=30)
            rc = so.channel.recv_exit_status()
            err = se.read().decode("utf-8", "replace").strip()
        finally:
            c.close()
        if rc != 0:
            out.append({**row, "status": "failed", "detail": f"adding the key failed (rc={rc}): {err[-200:]}"})
            continue
        why = _key_login(t, str(k))
        out.append({**row, "status": "installed" if why is None else "failed",
                    "detail": "key added; key login works" if why is None else f"key added, but key login fails: {why}"})
    return out
