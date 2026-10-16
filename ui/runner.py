"""Execution: what to run (Job -> command line) and where (LocalRunner: this checkout; SshRunner: the jump host).

The AFM password never appears on a command line, in a file or in a log: LocalRunner hands it to the child through
the environment variable the inventory names; SshRunner writes it to the remote shell's stdin, where ``read -r``
puts it into that variable for the remote process only (the same as ``read -s`` in ~/helios/run-health.sh).
"""
from __future__ import annotations

import io
import os
import posixpath
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT, RackProfile, find_secrets, inventory_rack_name, password_env

NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TC_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
MARKER_RX = re.compile(r"^[A-Za-z0-9_ ()]+$")
ENV_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KINDS = ("pytest", "health", "rerun")


class JobError(ValueError):
    pass


FILTER_KEYS = ("level", "scope", "disruptive", "health", "origin", "status", "suite")
SUITES = ("all", "afm", "ssh")
_FILTER_VALUE_RX = re.compile(r"^[^\x00-\x1f\x7f]{0,2000}$")


def clean_filters(d) -> dict:
    """The Run form's test-plan pick filters (tools/select_cases.py options), kept with a run / preset so the form
    can be filled again. Only known keys; each value one line of text, or a list of them (the UI's multi-selects),
    stored as the comma-separated string tools/select_cases.py takes; empty values dropped."""
    if d in (None, ""):
        return {}
    if not isinstance(d, dict):
        raise JobError("filters must be an object")
    bad = sorted(set(map(str, d)) - set(FILTER_KEYS))
    if bad:
        raise JobError(f"unknown filter(s): {bad}")
    out = {}
    for k in FILTER_KEYS:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple)) and k != "suite":
            if not all(isinstance(x, str) for x in v):
                raise JobError(f"filter {k}: a list of text values")
            v = ",".join(x.strip() for x in v if x.strip())
        if not isinstance(v, str) or not _FILTER_VALUE_RX.match(v):
            raise JobError(f"filter {k}: one line of text, at most 2000 characters")
        v = v.strip()
        if k == "suite" and v and v not in SUITES:
            raise JobError(f"filter suite must be one of {SUITES}")
        if v:
            out[k] = v
    return out


@dataclass
class Job:
    """One run request. kind: pytest (selected cases), health (tools/rack_health.py), rerun (rack_health
    --rerun-failed <base_run>)."""
    kind: str = "pytest"
    paths: list[str] = field(default_factory=lambda: ["tests"])
    tc: list[str] = field(default_factory=list)
    marker: str | None = None
    switches: str | None = None
    computes: str | None = None
    allow_disruptive: bool = False
    known: bool = True                  # health / rerun: pass the profile's --known file
    base_run: str | None = None         # rerun: the results folder whose failed checks are re-run
    pytest_args: list[str] = field(default_factory=list)

    @classmethod
    def from_request(cls, d: dict) -> "Job":
        kind = d.get("kind", "pytest")
        if kind not in KINDS:
            raise JobError(f"kind must be one of {KINDS}")
        tc = d.get("tc") or []
        if isinstance(tc, str):
            tc = [t for t in re.split(r"[,\s]+", tc) if t]
        bad = [t for t in tc if not TC_RX.match(t)]
        if bad:
            raise JobError(f"bad test case id(s): {bad}")
        marker = (d.get("marker") or "").strip() or None
        if marker and not MARKER_RX.match(marker):
            raise JobError("marker: letters, digits, '_', spaces, parentheses (and / or / not) only")
        paths = d.get("paths") or ["tests"]
        if isinstance(paths, str):
            paths = paths.split()
        for p in paths:
            pp = posixpath.normpath(p.split("::")[0])
            if pp.startswith(("/", "..")) or not (pp == "tests" or pp.startswith("tests/")):
                raise JobError(f"test path {p!r} must be under tests/")
        for k in ("switches", "computes"):
            v = (d.get(k) or "").strip()
            if v and not all(NAME_RX.match(x.strip()) for x in v.split(",") if x.strip()):
                raise JobError(f"{k}: comma-separated node names")
        base = d.get("base_run") or None
        if kind == "rerun" and not (base and NAME_RX.match(base)):
            raise JobError("rerun needs base_run (a results folder name)")
        extra = d.get("pytest_args") or []
        if isinstance(extra, str):
            extra = shlex.split(extra)
        if any(a.split("=")[0] in ("--inventory", "--results-dir", "-p") for a in extra):
            raise JobError("pytest_args must not set --inventory / --results-dir / -p")
        if kind == "pytest" and not (tc or marker or d.get("all")):
            raise JobError("select cases: test case ids, a marker, or 'all'")
        return cls(kind=kind, paths=list(paths), tc=list(tc), marker=marker,
                   switches=(d.get("switches") or "").strip() or None, computes=(d.get("computes") or "").strip() or None,
                   allow_disruptive=bool(d.get("allow_disruptive")), known=bool(d.get("known", True)),
                   base_run=base, pytest_args=list(extra))

    def as_dict(self) -> dict:
        return dict(self.__dict__)

    def selection_args(self) -> list[str]:
        a = []
        if self.tc:
            a += ["--tc", ",".join(self.tc)]
        if self.marker and self.kind == "pytest":
            a += ["-m", self.marker]
        if self.switches:
            a += ["--switches", self.switches]
        if self.computes:
            a += ["--computes", self.computes]
        if self.allow_disruptive:
            a.append("--allow-disruptive")
        return a + list(self.pytest_args)


def build_args(job: Job, inventory: str, results_dir: str, known: str | None, no_html: bool) -> list[str]:
    """Arguments after the python interpreter, run from the repository root."""
    if job.kind == "pytest":
        return ["-m", "pytest", *job.paths, "--inventory", inventory, "--results-dir", results_dir,
                "-p", "no:cacheprovider", *job.selection_args()]
    a = ["tools/rack_health.py"]
    if job.kind == "rerun":
        a += ["--rerun-failed", posixpath.join(results_dir, job.base_run)]
    a += ["--inventory", inventory, "--results-dir", results_dir]
    if job.known and known:
        a += ["--known", known]
    if no_html:
        a.append("--no-html")
    sel = job.selection_args()
    return a + (["--", *sel] if sel else [])


# ------------------------------------------------------------------ process execution (injectable for tests)

class Executor:
    """Runs local processes. Tests replace it to check the SSH command lines without any network."""

    def run(self, argv: list[str], input: bytes | None = None, timeout: float | None = 600,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
        if input is None:          # never let ssh read the terminal the UI was started from
            return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, cwd=cwd)
        return subprocess.run(argv, input=input, capture_output=True, timeout=timeout, cwd=cwd)

    def popen(self, argv: list[str], env: dict | None = None, cwd: Path | None = None, stdin: bool = False):
        return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)


def _result_dirs(results_dir: Path, rack: str) -> set[str]:
    if not results_dir.is_dir():
        return set()
    return {p.name for p in results_dir.iterdir() if p.is_dir() and p.name.startswith(f"{rack}-")}


def _stream(proc, log) -> int:
    for raw in proc.stdout:
        log(raw.decode("utf-8", "replace").rstrip("\n"))
    return proc.wait()


class Runner:
    def __init__(self, profile: RackProfile, executor: Executor | None = None):
        self.profile = profile
        self.ex = executor or Executor()
        self._procs: dict[str, object] = {}
        self._stopped: set[str] = set()
        self._lock = threading.Lock()

    def _start(self, run_id: str, start):
        """Start the main process unless the run was stopped while it was being prepared (sync)."""
        with self._lock:
            if run_id in self._stopped:
                raise JobError("stopped before the run started")
            proc = start()
            self._procs[run_id] = proc
            return proc

    def _mark_stopped(self, run_id: str):
        with self._lock:
            self._stopped.add(run_id)
            return self._procs.get(run_id)

    def execute(self, run_id: str, job: Job, password: str | None, log) -> tuple[int, list[str]]:
        raise NotImplementedError

    def stop(self, run_id: str) -> None:
        raise NotImplementedError


class LocalRunner(Runner):
    """pytest / tools/rack_health.py in this checkout (a rack reachable from this machine, e.g. MR3)."""

    def command(self, job: Job) -> list[str]:
        p = self.profile
        known = str(Path(ROOT, p.known)) if p.known else None
        return [sys.executable, *build_args(job, str(p.inventory), str(p.results_dir), known, no_html=False)]

    def execute(self, run_id, job, password, log):
        p = self.profile
        if not p.inventory.is_file():
            raise JobError(f"inventory not found: {p.inventory}")
        if job.kind == "rerun" and not (p.results_dir / job.base_run).is_dir():
            raise JobError(f"no results folder {job.base_run} in {p.results_dir}")
        rack = inventory_rack_name(p.inventory)
        before = _result_dirs(p.results_dir, rack)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if password:
            env[password_env(p.inventory)] = password
        argv = self.command(job)
        log("$ " + " ".join(shlex.quote(a) for a in argv))
        proc = self._start(run_id, lambda: self.ex.popen(argv, env=env, cwd=ROOT))
        try:
            rc = _stream(proc, log)
        finally:
            with self._lock:
                self._procs.pop(run_id, None)
        return rc, sorted(_result_dirs(p.results_dir, rack) - before)

    def stop(self, run_id):
        proc = self._mark_stopped(run_id)
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)    # rack_health's pytest child is in the same process group
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def rq(p: str) -> str:
    """Quote for the remote shell, keeping a leading ~/ expandable."""
    if p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


class SshRunner(Runner):
    """The full rack behind the jump host, mirroring the manual procedure (docs/how-to-run.html, section 4):
    ``git archive <ref> | ssh -p 8059 tenant-hpe@165.204.38.103 'tar -xf -'`` into ~/helios/helios-tests, the
    inventory copied next to it, tools/rack_health.py / pytest run there with the venv, and the new results folders
    copied back with tar. One multiplexed SSH connection (the jump host throttles rapid logins)."""

    def ssh(self) -> list[str]:
        s = self.profile.ssh
        o = ["ssh", "-p", str(s.port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
             "-o", "ServerAliveInterval=30", "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/helios-ui-%C",
             "-o", "ControlPersist=120"]
        for opt in s.options:
            o += ["-o", opt]
        return o + [s.target]

    def _remote(self, cmd: str, input: bytes | None = None, timeout: float = 600, check: bool = True):
        r = self.ex.run(self.ssh() + [cmd], input=input, timeout=timeout)
        if check and r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            raise JobError(f"ssh {self.profile.ssh.target}: '{cmd[:80]}' exited {r.returncode}: {err}")
        return r

    @property
    def _parent_base(self) -> tuple[str, str]:
        return posixpath.split(self.profile.ssh.code_dir.rstrip("/"))

    def pidfile(self, run_id: str) -> str:
        return rq(posixpath.join(self._parent_base[0], f".helios-ui-{run_id}.pid"))

    def results(self) -> str:
        return rq(posixpath.join(self.profile.ssh.code_dir, "results"))

    def sync_code(self, log) -> None:
        s = self.profile.ssh
        parent, base = self._parent_base
        argv = ["git", "-C", str(ROOT), "archive", "--format=tar", f"--prefix={base}/", s.ref, "--", ".",
                ":(exclude)testplan/*.xlsx"]
        log(f"== sync code: git archive {s.ref} -> {s.target}:{s.code_dir}")
        r = self.ex.run(argv)
        if r.returncode != 0:
            raise JobError("git archive failed: " + (r.stderr or b"").decode("utf-8", "replace")[-300:])
        self._remote(f"mkdir -p {rq(parent)} && tar -xf - -C {rq(parent)}", input=r.stdout)

    def sync_inventory(self, log) -> None:
        s, inv = self.profile.ssh, self.profile.inventory
        import yaml
        text = inv.read_bytes()
        bad = find_secrets(yaml.safe_load(text) or {})
        if bad:
            raise JobError(f"{inv} holds secret values ({', '.join(bad)}): use password_env")
        dst, tmp = rq(s.inventory), rq(s.inventory + ".ui-tmp")
        log(f"== sync inventory: {inv.name} -> {s.target}:{s.inventory}")
        # keep a dated copy of the jump host's inventory when it changes
        self._remote(f"cat > {tmp} && if cmp -s {tmp} {dst}; then rm -f {tmp}; else "
                     f"{{ [ ! -f {dst} ] || cp -p {dst} {dst}.bak-$(date +%Y%m%d-%H%M%S); }} && mv {tmp} {dst}; fi",
                     input=text)

    def _ls(self) -> set[str]:
        r = self._remote(f"mkdir -p {self.results()} && ls -1 {self.results()}")
        return {x for x in r.stdout.decode("utf-8", "replace").split() if NAME_RX.match(x)}

    def ensure_base_run(self, name: str, log) -> None:
        if self._remote(f"test -d {self.results()}/{shlex.quote(name)}", check=False).returncode == 0:
            return
        local = self.profile.results_dir / name
        if not local.is_dir():
            raise JobError(f"no results folder {name} here or on the jump host")
        log(f"== upload {name} to the jump host (not there)")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            t.add(local, arcname=name)
        self._remote(f"mkdir -p {self.results()} && tar -xzf - -C {self.results()}", input=buf.getvalue())

    def command(self, run_id: str, job: Job, with_password: bool) -> str:
        s = self.profile.ssh
        args = build_args(job, s.inventory, "results", self.profile.known, no_html=not s.remote_html)
        parts = [f"cd {rq(s.code_dir)}", "export PYTHONUNBUFFERED=1"]
        if with_password:
            var = password_env(self.profile.inventory)
            if not ENV_RX.match(var):
                raise JobError(f"bad password_env name {var!r}")
            parts.append(f"IFS= read -r {var} && export {var}")
        parts.append(f"echo $$ > {self.pidfile(run_id)}")
        parts.append("exec " + " ".join(rq(a) for a in [s.python, *args]))
        return " && ".join(parts)

    def fetch(self, names: list[str], log) -> None:
        if not names:
            return
        log(f"== copy back: {', '.join(names)}")
        r = self._remote(f"cd {self.results()} && tar -czf - -- " + " ".join(shlex.quote(n) for n in names),
                         timeout=1800)
        dest = self.profile.results_dir
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:gz") as t:
            members = [m for m in t.getmembers() if m.name.split("/")[0] in names]
            t.extractall(dest, members=members, filter="data")

    def execute(self, run_id, job, password, log):
        p = self.profile
        if not p.inventory.is_file():
            raise JobError(f"local inventory copy not found: {p.inventory}")
        self.sync_code(log)
        self.sync_inventory(log)
        if job.kind == "rerun":
            self.ensure_base_run(job.base_run, log)
        before = self._ls()
        cmd = self.command(run_id, job, bool(password))
        log(f"$ ssh {p.ssh.target} '{cmd}'")
        proc = self._start(run_id, lambda: self.ex.popen(self.ssh() + [cmd], stdin=True))
        try:
            if password:
                proc.stdin.write((password + "\n").encode())
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            rc = _stream(proc, log)
        finally:
            with self._lock:
                self._procs.pop(run_id, None)
        self._remote(f"rm -f {self.pidfile(run_id)}", check=False)
        rack = inventory_rack_name(p.inventory)
        new = sorted(n for n in self._ls() - before if n.startswith(f"{rack}-"))
        self.fetch(new, log)
        return rc, new

    def stop(self, run_id):
        self._mark_stopped(run_id)
        pf = self.pidfile(run_id)
        self._remote(f'p=$(cat {pf} 2>/dev/null) && {{ pkill -TERM -P "$p"; kill -TERM "$p"; }}', timeout=60,
                     check=False)
        with self._lock:
            proc = self._procs.get(run_id)
        if proc is not None:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.terminate()


def make_runner(profile: RackProfile, executor: Executor | None = None) -> Runner:
    return (SshRunner if profile.runner == "ssh" else LocalRunner)(profile, executor)
