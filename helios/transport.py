"""Command transports: real SSH, and replay (canned outputs) for self-tests."""
from __future__ import annotations

import logging
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from .inventory import Host

log = logging.getLogger("helios.cmd")


@dataclass
class CmdResult:
    host: str
    cmd: str
    rc: int
    stdout: str
    stderr: str
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def check(self) -> "CmdResult":
        if self.rc != 0:
            raise CommandError(self)
        return self

    def tail(self, n: int = 600) -> str:
        return (self.stderr.strip() or self.stdout.strip())[-n:]


class CommandError(RuntimeError):
    def __init__(self, result: CmdResult):
        self.result = result
        super().__init__(f"[{result.host}] rc={result.rc}: {result.cmd}\n{result.tail()}")


class CommandTimeout(RuntimeError):
    pass


class Transport(Protocol):
    host: Host

    def run(self, cmd: str, *, timeout: float = 120, sudo: bool = False,
            env: dict[str, Any] | None = None) -> CmdResult: ...

    def close(self) -> None: ...

    def reset(self) -> None: ...


def build_command(cmd: str, sudo: bool = False, env: dict[str, Any] | None = None) -> str:
    prefix = []
    if sudo:
        prefix.append("sudo -n")
    if env:
        prefix.append("env " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items()))
    if not prefix:
        return cmd
    return " ".join(prefix) + " bash -c " + shlex.quote(cmd)


LOG_OUTPUT_LIMIT = 6000


def _short(cmd: str, n: int = 110) -> str:
    cmd = " ".join(cmd.split())
    return cmd if len(cmd) <= n else cmd[: n - 3] + "..."


def _output_for_log(res: "CmdResult") -> str:
    """Command output for the --log-file trace (head of stdout + stderr), so failures are diagnosable."""
    parts = []
    for label, text in (("stdout", res.stdout), ("stderr", res.stderr)):
        text = text.rstrip()
        if text:
            cut = f"\n    ... [{len(text) - LOG_OUTPUT_LIMIT} more chars]" if len(text) > LOG_OUTPUT_LIMIT else ""
            parts.append(f"\n  --- {label} ---\n" + "\n".join("    " + ln for ln in text[:LOG_OUTPUT_LIMIT].splitlines()) + cut)
    return "".join(parts)


class SshTransport:
    """One persistent paramiko connection per host; channels are opened per command
    so a single transport is safe to share across threads."""

    def __init__(self, host: Host, connect_timeout: float = 15.0, keepalive: int = 30):
        self.host = host
        self.connect_timeout = connect_timeout
        self.keepalive = keepalive
        self._client = None
        self._lock = threading.Lock()

    def _ensure(self):
        with self._lock:
            t = self._client.get_transport() if self._client else None
            if t is None or not t.is_active():
                self._client = self._connect()
            return self._client

    def _connect(self):
        import paramiko

        h = self.host
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # Lab racks get reimaged constantly; host-key pinning would just break runs.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        use_keys = h.password is None or bool(h.key_file)
        key_file = os.path.expanduser(h.key_file) if h.key_file else None  # paramiko does not expand ~
        client.connect(
            h.host, port=h.port, username=h.user, password=h.password, key_filename=key_file,
            timeout=self.connect_timeout, banner_timeout=self.connect_timeout,
            auth_timeout=self.connect_timeout, look_for_keys=use_keys, allow_agent=use_keys,
        )
        client.get_transport().set_keepalive(self.keepalive)
        log.debug("[%s] connected %s@%s:%s", h.name, h.user, h.host, h.port)
        return client

    HEARTBEAT_S = 30
    KILL_GRACE_S = 10

    def run(self, cmd: str, *, timeout: float = 120, sudo: bool = False,
            env: dict[str, Any] | None = None) -> CmdResult:
        full = build_command(cmd, sudo, env)
        # Enforce the timeout on the remote side too: closing the SSH channel does not kill a hung tool
        # (e.g. a GPU workload stuck in the driver), so coreutils `timeout` sends TERM, then KILL.
        remote = f"timeout -k {self.KILL_GRACE_S} {max(1, int(timeout))} bash -c {shlex.quote(full)}"
        start = time.monotonic()
        log.debug("[%s] $ %s", self.host.name, full)
        chan = self._ensure().get_transport().open_session(timeout=self.connect_timeout)
        try:
            chan.exec_command(remote)
            out, err = bytearray(), bytearray()
            deadline = start + timeout + self.KILL_GRACE_S + 15  # let the remote timeout fire first (rc 124)
            next_beat = start + self.HEARTBEAT_S
            while True:
                progressed = False
                while chan.recv_ready():
                    out += chan.recv(65536)
                    progressed = True
                while chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                    progressed = True
                # OpenSSH may send exit-status BEFORE the last stdout data; stopping on exit status alone
                # truncated large outputs (a 235 KB afmctl JSON came back cut at 229 KB). Stop only when
                # the server has also sent EOF and both buffers are drained.
                if (chan.exit_status_ready() and chan.eof_received
                        and not chan.recv_ready() and not chan.recv_stderr_ready()):
                    break
                now = time.monotonic()
                if now > deadline:
                    raise CommandTimeout(f"[{self.host.name}] timed out after {timeout}s: {cmd}")
                if now >= next_beat:  # only long commands get progress lines
                    log.info("[%s] still running after %ds (timeout %ds): %s",
                             self.host.name, now - start, timeout, _short(cmd))
                    next_beat += self.HEARTBEAT_S
                if not progressed:
                    time.sleep(0.02)
            rc = chan.recv_exit_status()
        finally:
            chan.close()
        res = CmdResult(self.host.name, cmd, rc, out.decode(errors="replace"),
                        err.decode(errors="replace"), time.monotonic() - start)
        if rc == 124:
            res.stderr += f"\n[helios] killed by remote timeout after {timeout}s"
            log.warning("[%s] TIMED OUT after %ds and was killed: %s", self.host.name, timeout, _short(cmd))
        elif res.duration >= self.HEARTBEAT_S:
            log.info("[%s] finished rc=%s in %.0fs: %s", self.host.name, rc, res.duration, _short(cmd))
        log.debug("[%s] rc=%s %.2fs%s", self.host.name, rc, res.duration, _output_for_log(res))
        return res

    def close(self) -> None:
        with self._lock:
            if self._client:
                self._client.close()
                self._client = None

    reset = close  # drop a (possibly dead) connection; the next run() reconnects


class LocalTransport:
    """Runs commands on the test runner itself (BMC/PDU power control, ipmitool)."""

    def __init__(self, host: Host):
        self.host = host

    def run(self, cmd: str, *, timeout: float = 120, sudo: bool = False,
            env: dict[str, Any] | None = None) -> CmdResult:
        import subprocess

        full = build_command(cmd, sudo, env)
        start = time.monotonic()
        log.debug("[local] $ %s", full)
        try:
            p = subprocess.run(["bash", "-c", full], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise CommandTimeout(f"[local] timed out after {timeout}s: {cmd}") from e
        return CmdResult(self.host.name, cmd, p.returncode, p.stdout, p.stderr, time.monotonic() - start)

    def close(self) -> None:
        pass

    reset = close


@dataclass
class _Rule:
    cmd: re.Pattern
    host: re.Pattern | None
    stdouts: list[str]  # >1 entry: successive calls cycle through them (e.g. counters that advance)
    stderr: str
    rcs: list[int]      # likewise for exit codes (e.g. "driver loaded?" false after unload, true after load)


_RULE_CACHE: dict[str, list[_Rule]] = {}


def _load_rules(directory: Path) -> list[_Rule]:
    key = str(directory)
    if key not in _RULE_CACHE:
        spec = yaml.safe_load((directory / "responses.yaml").read_text()) or []
        rules = []
        for r in spec:
            if r.get("files"):
                stdouts = [(directory / f).read_text() for f in r["files"]]
            elif r.get("file"):
                stdouts = [(directory / r["file"]).read_text()]
            else:
                stdouts = [r.get("stdout", "")]
            rules.append(_Rule(
                cmd=re.compile(r["cmd"]),
                host=re.compile(r["host"]) if r.get("host") else None,
                stdouts=stdouts, stderr=r.get("stderr", ""),
                rcs=[int(x) for x in r["rcs"]] if r.get("rcs") else [int(r.get("rc", 0))],
            ))
        _RULE_CACHE[key] = rules
    return _RULE_CACHE[key]


class ReplayTransport:
    """Serves canned outputs from ``<dir>/responses.yaml`` (first matching rule wins;
    ``files``/``rcs`` lists cycle per call; ``@@NOW@@`` becomes the current epoch time).
    Lets the full suite run without a rack — for framework self-tests and demos."""

    def __init__(self, host: Host, directory: str | Path):
        self.host = host
        self.rules = _load_rules(Path(directory))
        self.calls: list[str] = []
        self._hits: dict[int, int] = {}

    def run(self, cmd: str, *, timeout: float = 120, sudo: bool = False,
            env: dict[str, Any] | None = None) -> CmdResult:
        self.calls.append(cmd)
        for i, rule in enumerate(self.rules):
            if rule.host and not rule.host.search(self.host.name):
                continue
            if rule.cmd.search(cmd):
                n = self._hits.get(i, 0)
                self._hits[i] = n + 1
                out = rule.stdouts[n % len(rule.stdouts)].replace("@@NOW@@", f"{time.time():.6f}")
                return CmdResult(self.host.name, cmd, rule.rcs[n % len(rule.rcs)], out, rule.stderr)
        return CmdResult(self.host.name, cmd, 127, "", f"replay: no rule matches {cmd!r}")

    def close(self) -> None:
        pass

    reset = close


def make_transport(host: Host, cfg: dict[str, Any]) -> Transport:
    kind = cfg.get("kind", "ssh")
    if kind == "ssh":
        return SshTransport(host, connect_timeout=float(cfg.get("connect_timeout", 15)))
    if kind == "replay":
        return ReplayTransport(host, cfg["dir"])
    if kind == "local":
        return LocalTransport(host)
    raise ValueError(f"unknown transport kind {kind!r}")
