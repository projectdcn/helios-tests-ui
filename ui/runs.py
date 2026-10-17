"""Run queue (one run at a time per rack), live log buffer, run records, results history and report building."""
from __future__ import annotations

import collections
import csv
import json
import os
import re
import random
import string
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import ROOT, RackProfile, inventory_rack_name
from .runner import Job, JobError, NAME_RX, clean_filters, make_runner

REDACTED = "<REDACTED>"
SECRET_NAME = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN", re.I)
# secret-looking text in a log line: JSON keys ("password": "x"), Python reprs ('password': 'x'), NAME=value assignments
_SECRET_TEXT = [
    re.compile(r"""(["'](?:password|passwd|private-key|recovery-key|token|secret)["']\s*:\s*)(["'])(?:(?!\2).)*\2""", re.I),
    re.compile(r"\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN)[A-Z0-9_]*=)(\S+)", re.I),
]


def redact(line: str, values=()) -> str:
    """The run's own password, any secret-valued env var of the UI process, and secret-looking text."""
    for v in values:
        if v and len(v) >= 4 and v in line:
            line = line.replace(v, REDACTED)
    line = _SECRET_TEXT[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(2)}", line)
    return _SECRET_TEXT[1].sub(lambda m: m.group(1) + REDACTED if m.group(2) != REDACTED else m.group(0), line)
FINAL = ("finished", "failed", "stopped", "error", "cancelled")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Run:
    def __init__(self, rid: str, rack: str, job: Job, password: str | None, log_dir: Path,
                 filters: dict | None = None, password_source: str | None = None, notes: list[str] | None = None):
        self.id, self.rack, self.job = rid, rack, job
        self.filters = dict(filters or {})          # test-plan pick of the form (for "Run again"), never a secret
        self._password = password or None           # memory only; dropped when the run ends
        self.has_password = bool(password)
        # "typed" | "keychain" (never the value); notes: header lines of the log (e.g. a Keychain save that failed)
        self.password_source = (password_source or "typed") if password else None
        self.notes = list(notes or [])
        self.status = "queued"
        self.created, self.started, self.ended = _now(), None, None
        self.rc: int | None = None
        self.results: list[str] = []
        self.verdict: str | None = None
        self.error: str | None = None
        self.stop_requested = False
        self.lines: list[str] = []
        self.cond = threading.Condition()
        self.log_path = log_dir / f"{rid}.log"
        self.meta_path = log_dir / f"{rid}.json"

    def as_dict(self) -> dict:
        return {"id": self.id, "rack": self.rack, "job": self.job.as_dict(), "filters": self.filters,
                "status": self.status,
                "created": self.created, "started": self.started, "ended": self.ended, "rc": self.rc,
                "results": self.results, "verdict": self.verdict, "error": self.error,
                "has_password": self.has_password, "password_source": self.password_source,
                "lines": len(self.lines)}

    def save(self) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(json.dumps(self.as_dict(), indent=1))

    @property
    def done(self) -> bool:
        return self.status in FINAL


class RunManager:
    def __init__(self, profiles: dict[str, RackProfile], executor=None):
        self.profiles = profiles
        self.runners = {n: make_runner(p, executor) for n, p in profiles.items()}
        self.runs: dict[str, Run] = {}
        self.queues: dict[str, collections.deque] = {n: collections.deque() for n in profiles}
        self.active: dict[str, Run | None] = {n: None for n in profiles}
        self.workers: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------- queue
    def submit(self, rack: str, req: dict, password: str | None = None, password_source: str | None = None,
               notes: list[str] | None = None) -> Run:
        if rack not in self.profiles:
            raise JobError(f"unknown rack {rack!r}")
        job = Job.from_request(req)
        filters = clean_filters(req.get("filters"))
        p = self.profiles[rack]
        rid = f"{rack}-{time.strftime('%Y%m%d-%H%M%S')}-" + "".join(random.choices(string.ascii_lowercase, k=4))
        run = Run(rid, rack, job, password, p.results_dir / "ui-runs", filters, password_source, notes)
        with self.lock:
            self.runs[rid] = run
            self.queues[rack].append(run)
            run.save()
            w = self.workers.get(rack)
            if w is None or not w.is_alive():
                w = threading.Thread(target=self._worker, args=(rack,), name=f"ui-runs-{rack}", daemon=True)
                self.workers[rack] = w
                w.start()
        return run

    def position(self, run: Run) -> int:
        with self.lock:
            q = list(self.queues[run.rack])
        return q.index(run) + 1 if run in q else 0

    def _worker(self, rack: str) -> None:
        while True:
            with self.lock:
                if not self.queues[rack]:
                    self.workers.pop(rack, None)
                    return
                run = self.queues[rack].popleft()
                self.active[rack] = run
            try:
                if run.status == "queued":
                    self._execute(run)
            finally:
                with self.lock:
                    self.active[rack] = None

    def _log(self, run: Run, fh, line: str) -> None:
        line = redact(line, (run._password, *(v for k, v in os.environ.items() if SECRET_NAME.search(k))))
        fh.write(line + "\n")
        fh.flush()
        with run.cond:
            run.lines.append(line)
            run.cond.notify_all()

    def _execute(self, run: Run) -> None:
        run.status, run.started = "running", _now()
        run.save()
        runner = self.runners[run.rack]
        run.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(run.log_path, "w") as fh:
            log = lambda line: self._log(run, fh, line)   # noqa: E731
            try:
                pw = {"typed": "given (memory only)", "keychain": "from macOS Keychain (memory only)"}
                log(f"# run {run.id} on {run.rack} ({self.profiles[run.rack].runner}); "
                    f"AFM password {pw.get(run.password_source, 'not given')}")
                for note in run.notes:
                    log(f"# {note}")
                rc, new = runner.execute(run.id, run.job, run._password, log)
                run.rc, run.results = rc, new
                self._post(run, log)
                run.status = "stopped" if run.stop_requested else "finished"
            except JobError as e:
                run.status, run.error = ("stopped" if run.stop_requested else "error"), str(e)
                log(f"ERROR: {e}")
            except Exception as e:  # noqa: BLE001 - keep the worker alive
                run.status, run.error = "error", f"{type(e).__name__}: {e}"
                log(f"ERROR: {run.error}")
            finally:
                run._password = None
                run.ended = _now()
                log(f"# {run.status} rc={run.rc} results={', '.join(run.results) or '-'}")
                run.save()
                with run.cond:
                    run.cond.notify_all()

    def _post(self, run: Run, log) -> None:
        """Reports built here (no rack access): report.html for pytest runs; health.html when the jump host ran
        rack_health with --no-html; the verdict of the run."""
        p = self.profiles[run.rack]
        for name in run.results:
            d = p.results_dir / name
            if not (d / "tc_summary.csv").exists():
                continue
            if (d / "health.json").exists():
                h = json.loads((d / "health.json").read_text())
                run.verdict = h.get("verdict")
                if not (d / "health.html").exists():
                    build_health_html(d, log)
            elif run.job.kind == "pytest":
                build_report(d, f"{run.rack}: {summary_text(d)}", log)
                run.verdict = run.verdict or summary_text(d)

    def stop(self, rid: str) -> Run:
        run = self.runs.get(rid)
        if run is None:
            raise KeyError(rid)
        with self.lock:
            if run.status == "queued":
                if run in self.queues[run.rack]:
                    self.queues[run.rack].remove(run)
                run.status, run.ended, run._password = "cancelled", _now(), None
                run.save()
                with run.cond:
                    run.cond.notify_all()
                return run
        if run.status == "running":
            run.stop_requested = True
            threading.Thread(target=self.runners[run.rack].stop, args=(rid,), daemon=True).start()
        return run

    def wait_lines(self, rid: str, start: int, timeout: float = 15) -> tuple[list[str], bool]:
        run = self.runs[rid]
        with run.cond:
            if len(run.lines) <= start and not run.done:
                run.cond.wait(timeout)
            return run.lines[start:], run.done

    def get(self, rid: str) -> dict | None:
        run = self.runs.get(rid)
        if run is not None:
            d = run.as_dict()
            d["queue_position"] = self.position(run)
            return d
        for p in self.profiles.values():
            f = p.results_dir / "ui-runs" / f"{rid}.json"
            if NAME_RX.match(rid) and f.exists():
                return json.loads(f.read_text())
        return None

    def log_text(self, rid: str, start: int = 0) -> tuple[list[str], bool]:
        run = self.runs.get(rid)
        if run is not None:
            with run.cond:
                return run.lines[start:], run.done
        meta = self.get(rid)
        if meta is None:
            raise KeyError(rid)
        f = self.profiles[meta["rack"]].results_dir / "ui-runs" / f"{rid}.log"
        lines = f.read_text().splitlines() if f.exists() else []
        return lines[start:], True

    def list_runs(self, rack: str | None = None) -> list[dict]:
        out = {r.id: {**r.as_dict(), "queue_position": self.position(r)} for r in list(self.runs.values())}
        for n, p in self.profiles.items():
            if rack and n != rack:
                continue
            for f in (p.results_dir / "ui-runs").glob("*.json") if (p.results_dir / "ui-runs").is_dir() else []:
                if f.stem not in out:
                    try:
                        d = json.loads(f.read_text())
                    except ValueError:
                        continue
                    if d.get("rack") == n:
                        if d.get("status") in ("queued", "running"):
                            d["status"] = "lost"          # the UI was restarted while it ran
                        out[f.stem] = d
        rows = [d for d in out.values() if not rack or d["rack"] == rack]
        return sorted(rows, key=lambda d: d["created"], reverse=True)

    def busy(self, rack: str) -> dict:
        with self.lock:
            a = self.active.get(rack)
            return {"running": a.id if a else None, "queued": [r.id for r in self.queues[rack]]}


# ------------------------------------------------------------------ results history / reports

def summary_counts(run_dir: Path) -> dict[str, int]:
    c: dict[str, int] = {}
    try:
        for r in csv.DictReader(open(run_dir / "tc_summary.csv", newline="")):
            c[r["verdict"]] = c.get(r["verdict"], 0) + 1
    except OSError:
        pass
    return c


def summary_text(run_dir: Path) -> str:
    c = summary_counts(run_dir)
    return ", ".join(f"{n} {v}" for v, n in sorted(c.items())) or "no cases"


def history(profile: RackProfile) -> list[dict]:
    d = profile.results_dir
    if not d.is_dir():
        return []
    rack = inventory_rack_name(profile.inventory) if profile.inventory.is_file() else profile.name
    out = []
    for p in d.iterdir():
        if not p.is_dir() or not (p / "tc_summary.csv").exists() or not p.name.startswith(f"{rack}-"):
            continue
        row = {"name": p.name, "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
               "counts": summary_counts(p), "reports": sorted(x.name for x in p.glob("*.html")),
               "health": None, "merged_from": None}
        if (p / "health.json").exists():
            try:
                h = json.loads((p / "health.json").read_text())
                row["health"] = {"verdict": h.get("verdict"), "blockers": len(h.get("blockers") or []),
                                 "warnings": len(h.get("warnings") or []), "judged": h.get("judged")}
            except ValueError:
                row["health"] = {"verdict": "?"}
        if (p / "merged-from.txt").exists():
            row["merged_from"] = (p / "merged-from.txt").read_text().split()
        out.append(row)
    return sorted(out, key=lambda r: r["mtime"], reverse=True)


def _tool(args: list[str], log) -> int:
    r = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=600)
    for ln in (r.stdout + r.stderr).splitlines()[-5:]:
        log(ln)
    return r.returncode


def build_report(run_dir: Path, title: str, log=lambda s: None) -> Path:
    out = run_dir / "report.html"
    _tool(["tools/html_report.py", str(run_dir), "-o", str(out), "--title", title], log)
    return out


def build_health_html(run_dir: Path, log=lambda s: None) -> Path:
    """The same report rack_health.py builds, for a run judged with --no-html (jump host)."""
    h = json.loads((run_dir / "health.json").read_text())
    out = run_dir / "health.html"
    args = ["tools/html_report.py", str(run_dir), "-o", str(out), "--title", f"Rack health check - {h.get('verdict')}",
            "--subtitle", f"{len(h.get('blockers') or [])} blocker(s), {len(h.get('warnings') or [])} warning(s); "
                          f"judged {h.get('judged')}"]
    if (run_dir / "health-notes.txt").exists():
        args += ["--notes", str(run_dir / "health-notes.txt")]
    _tool(args, log)
    return out
