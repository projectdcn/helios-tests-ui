"""The AFM password in the macOS login Keychain (opt-in, per rack): service ``helios-ui``, account
``<rack>:<AFM username>``, through /usr/bin/security.

The password never goes on a command line (``ps`` shows every process's argv): it is saved with ``security -i``
(interactive mode) and the ``add-generic-password`` command written to its STDIN, the password given as
``-X <hex>`` so the interactive parser has nothing to quote (it has no reliable escape for ``"`` or ``\\``).
It is read with ``find-generic-password -w``, which prints it on stdout (argv: service and account only). That
prints printable ASCII as is and anything else as hex, so only printable ASCII (space to ``~``) is accepted: the
read-back is then exact. Whether a password is saved is checked without reading it (no ``-w``).
"""
from __future__ import annotations

import re
import subprocess
import sys
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
        raise KeychainError("the Keychain option takes passwords of printable ASCII characters (space to ~, at most "
                            "256); type this one for each run instead")
    return pw


class Keychain:
    """``run(argv, stdin) -> (rc, stdout, stderr)`` is injectable (tests never touch the real Keychain).
    ``enabled=False``: no Keychain at all (the default of an App built without one, e.g. in tests)."""

    def __init__(self, run=None, service: str = SERVICE, security: str = SECURITY, enabled: bool = True):
        self._run, self.enabled = run, enabled
        self.service, self.security = service, security
        if not re.fullmatch(r"[A-Za-z0-9._-]+", service):
            raise ValueError("service name: letters, digits, . _ -")

    def available(self) -> bool:
        return self.enabled and (self._run is not None or (sys.platform == "darwin" and Path(self.security).is_file()))

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
