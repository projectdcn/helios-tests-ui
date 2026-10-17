"""The AFM password saved on the UI server (opt-in, per rack), account ``<rack>:<AFM username>``: in the macOS
login Keychain (service ``helios-ui``, through /usr/bin/security), or - on Linux and any other UI server without
it - in a password file only the UI's user can read (:class:`FileSecrets`).

The password never goes on a command line (``ps`` shows every process's argv): it is saved with ``security -i``
(interactive mode) and the ``add-generic-password`` command written to its STDIN, the password given as
``-X <hex>`` so the interactive parser has nothing to quote (it has no reliable escape for ``"`` or ``\\``).
It is read with ``find-generic-password -w``, which prints it on stdout (argv: service and account only). That
prints printable ASCII as is and anything else as hex, so only printable ASCII (space to ``~``) is accepted: the
read-back is then exact. Whether a password is saved is checked without reading it (no ``-w``).
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path

from .runner import JobError

SERVICE = "helios-ui"
SECURITY = "/usr/bin/security"
NOT_FOUND = 44                      # security's exit code for "item could not be found"
ACCOUNT_RX = re.compile(r"^[A-Za-z0-9._@:+-]{1,200}$")
PASSWORD_RX = re.compile(r"^[\x20-\x7e]{1,256}$")


class KeychainError(JobError):
    pass


def _subprocess_run(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
    r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout, r.stderr


def account(rack: str, username: str | None) -> str:
    a = f"{rack}:{username or 'admin'}"
    if not ACCOUNT_RX.match(a):
        raise KeychainError(f"Keychain account {a!r}: rack and AFM user may use letters, digits and . _ @ + - only")
    return a


def check_password(pw) -> str:
    if not isinstance(pw, str) or not pw:
        raise KeychainError("no password to save")
    if not PASSWORD_RX.match(pw):
        raise KeychainError("saving takes passwords of printable ASCII characters (space to ~, at most "
                            "256); type this one for each run instead")
    return pw


class Keychain:
    """``run(argv, stdin) -> (rc, stdout, stderr)`` is injectable (tests never touch the real Keychain).
    ``enabled=False``: no Keychain at all (the default of an App built without one, e.g. in tests)."""

    label = "macOS Keychain"

    def __init__(self, run=None, service: str = SERVICE, security: str = SECURITY, enabled: bool = True):
        self._run, self.enabled = run, enabled
        self.service, self.security = service, security
        if not re.fullmatch(r"[A-Za-z0-9._-]+", service):
            raise ValueError("service name: letters, digits, . _ -")

    def available(self) -> bool:
        return self.enabled and (self._run is not None or (sys.platform == "darwin" and Path(self.security).is_file()))

    def unavailable_reason(self) -> str | None:
        """Why the Keychain cannot be used here (shown on the page next to the disabled Remember box), or None."""
        if not self.enabled:
            return "the macOS Keychain is not enabled in this UI: type the AFM password for each run"
        if not self.available():
            return "macOS Keychain not available on this system: type the AFM password for each run"
        return None

    def _call(self, args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        if not self.enabled:
            raise KeychainError("the macOS Keychain is not enabled in this UI")
        if not self.available():
            raise KeychainError("macOS Keychain not available here (needs macOS and /usr/bin/security)")
        try:
            return (self._run or _subprocess_run)([self.security, *args], stdin)
        except (OSError, subprocess.SubprocessError) as e:
            raise KeychainError(f"security: {type(e).__name__}") from None

    @staticmethod
    def _fail(what: str, rc: int, err: str) -> KeychainError:
        last = [ln for ln in (err or "").splitlines() if ln.strip()][:1]
        return KeychainError(f"Keychain {what} failed (security exit {rc}{': ' + last[0][:200] if last else ''})")

    def save(self, acct: str, password: str) -> None:
        if not ACCOUNT_RX.match(acct):
            raise KeychainError(f"bad Keychain account {acct!r}")
        hexpw = check_password(password).encode("ascii").hex()
        line = f"add-generic-password -U -s {self.service} -a {acct} -l {self.service} -X {hexpw}\n"
        rc, _, err = self._call(["-i"], line)
        if rc != 0:
            raise self._fail("save", rc, (err or "").replace(hexpw, "<hex>"))

    def get(self, acct: str) -> str | None:
        rc, out, err = self._call(["find-generic-password", "-s", self.service, "-a", acct, "-w"])
        if rc == NOT_FOUND:
            return None
        if rc != 0:
            raise self._fail("read", rc, err)
        pw = out[:-1] if out.endswith("\n") else out
        return pw or None

    def has(self, acct: str) -> bool:
        rc, _, err = self._call(["find-generic-password", "-s", self.service, "-a", acct])
        if rc == NOT_FOUND:
            return False
        if rc != 0:
            raise self._fail("lookup", rc, err)
        return True

    def delete(self, acct: str) -> bool:
        rc, _, err = self._call(["delete-generic-password", "-s", self.service, "-a", acct])
        if rc == NOT_FOUND:
            return False
        if rc != 0:
            raise self._fail("delete", rc, err)
        return True


def default_secrets_path(environ=None) -> Path:
    """$HELIOS_UI_SECRETS, else $XDG_CONFIG_HOME/helios-ui/secrets.json (~/.config/...): outside the install
    prefix, so an upgrade or reinstall keeps the saved passwords."""
    env = os.environ if environ is None else environ
    if (env.get("HELIOS_UI_SECRETS") or "").strip():
        return Path(env["HELIOS_UI_SECRETS"].strip()).expanduser()
    base = (env.get("XDG_CONFIG_HOME") or "").strip() or str(Path.home() / ".config")
    return Path(base) / "helios-ui" / "secrets.json"


class FileSecrets:
    """The Keychain's stand-in on a UI server without one (Linux): ``{account: password}`` in one JSON file, mode
    0600 in a 0700 folder, written atomically. The password is in clear text on that disk - as safe as the UI user's
    account; a file other users can read is refused (fix with chmod 600). Same interface as :class:`Keychain`."""

    enabled = True

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return f"password file {self.path}"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str | None:
        return None

    def _load(self) -> dict:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise KeychainError(f"{self.path}: {e.strerror}") from None
        if st.st_mode & 0o077:
            raise KeychainError(f"{self.path} can be read by other users (mode {stat.filemode(st.st_mode)}): "
                                f"chmod 600 {self.path}")
        try:
            data = json.loads(self.path.read_text() or "{}")
        except (OSError, ValueError) as e:
            raise KeychainError(f"{self.path}: unreadable ({type(e).__name__})") from None
        if not isinstance(data, dict):
            raise KeychainError(f"{self.path}: not a JSON object")
        return data

    def _write(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)    # an old tmp with a looser mode keeps it through O_CREAT
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as e:
            raise KeychainError(f"saving {self.path} failed: {e.strerror}") from None

    def save(self, acct: str, password: str) -> None:
        if not ACCOUNT_RX.match(acct):
            raise KeychainError(f"bad account {acct!r}")
        pw = check_password(password)
        with self._lock:
            data = self._load()
            data[acct] = pw
            self._write(data)

    def get(self, acct: str) -> str | None:
        with self._lock:
            pw = self._load().get(acct)
        return pw if isinstance(pw, str) and pw else None

    def has(self, acct: str) -> bool:
        return self.get(acct) is not None

    def delete(self, acct: str) -> bool:
        with self._lock:
            data = self._load()
            if acct not in data:
                return False
            del data[acct]
            self._write(data)
        return True


def password_store(environ=None):
    """The macOS Keychain where there is one, else the password file."""
    kc = Keychain()
    return kc if kc.available() else FileSecrets(default_secrets_path(environ))
