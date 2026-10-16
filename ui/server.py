"""HTTP controller (standard library): JSON API + server-sent events for the live log + static page + results files.

Bound to 127.0.0.1. State-changing requests need ``Content-Type: application/json`` and the ``X-Helios-UI`` header
(a page from another site cannot send them without a CORS preflight, which this server never grants), and the Host
header must be the loopback address (no DNS rebinding).
"""
from __future__ import annotations

import json
import mimetypes
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import inventory_edit
from .config import ROOT, load_config
from .runner import JobError, NAME_RX
from .runs import RunManager, build_report, history

STATIC = Path(__file__).resolve().parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 4 * 1024 * 1024


class App:
    def __init__(self, config: Path | None = None, executor=None, profiles=None):
        self.profiles = profiles if profiles is not None else load_config(config)
        self.runs = RunManager(self.profiles, executor)
        self.edit_lock = threading.Lock()

    def shutdown(self) -> None:
        """Stop every queued / running run (local runs are in their own process group)."""
        for run in list(self.runs.runs.values()):
            if not run.done:
                self.runs.stop(run.id)
        for r in self.profiles:
            a = self.runs.active.get(r)
            if a is not None:
                self.runs.runners[r].stop(a.id)

    def profile(self, rack: str):
        p = self.profiles.get(rack)
        if p is None:
            raise LookupError(f"unknown rack {rack!r}")
        return p


def select_cases(filters: dict) -> list[str]:
    """tools/select_cases.py on the test-plan workbook (read only)."""
    wb = filters.get("workbook") or next((str(p) for p in sorted((ROOT / "testplan").glob("*.xlsx"))
                                          if not p.name.startswith("~$")), None)
    if not wb:
        raise JobError("no test-plan workbook in testplan/")
    wbp = (ROOT / wb).resolve() if not Path(wb).is_absolute() else Path(wb)
    if not wbp.is_file() or wbp.suffix != ".xlsx":
        raise JobError(f"workbook not found: {wb}")
    args = [sys.executable, "tools/select_cases.py", str(wbp), "--suite", filters.get("suite") or "all"]
    for k in ("level", "status", "scope", "origin", "disruptive", "health"):
        v = str(filters.get(k) or "").strip()
        if v:
            args += [f"--{k}", v]
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise JobError("select_cases: " + (r.stderr or r.stdout)[-400:])
    return [t for t in r.stdout.strip().split(",") if t]


class Handler(BaseHTTPRequestHandler):
    app: App = None          # set by make_server
    server_version = "helios-ui"

    def log_message(self, fmt, *args):        # quiet; request bodies (passwords) are never logged anyway
        pass

    # ------------------------------------------------------------- helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def err(self, code: int, msg, **extra) -> None:
        self.json({"error": msg if isinstance(msg, str) else "; ".join(msg),
                   "errors": msg if isinstance(msg, list) else [msg], **extra}, code)

    def _host_ok(self) -> bool:
        h = self.headers.get("Host") or ""
        host = h.split("]")[0] + "]" if h.startswith("[") else h.split(":")[0]
        return host in ALLOWED_HOSTS

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        d = json.loads(raw or b"{}")
        if not isinstance(d, dict):
            raise ValueError("JSON object expected")
        return d

    def _file(self, base: Path, rel: str) -> None:
        base = base.resolve()
        f = (base / rel).resolve()
        if base not in f.parents or not f.is_file():
            return self.err(404, "not found")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.suffix in (".log", ".txt", ".csv", ".yaml"):
            ctype = "text/plain"
        if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)

    # ------------------------------------------------------------- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self.err(403, "bad Host header")
        u = urlparse(self.path)
        path, q = unquote(u.path), parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self._file(STATIC, "index.html")
            if path.startswith("/static/"):
                return self._file(STATIC, path[len("/static/"):])
            m = re.fullmatch(r"/results/([^/]+)/([^/]+)/(.+)", path)
            if m:
                p = self.app.profile(m.group(1))
                if not NAME_RX.match(m.group(2)):
                    return self.err(404, "not found")
                return self._file(p.results_dir / m.group(2), m.group(3))
            if path == "/api/racks":
                return self.json([{**p.as_dict(), **self.app.runs.busy(n)} for n, p in self.app.profiles.items()])
            m = re.fullmatch(r"/api/racks/([^/]+)/history", path)
            if m:
                return self.json(history(self.app.profile(m.group(1))))
            m = re.fullmatch(r"/api/racks/([^/]+)/inventory", path)
            if m:
                return self.json(inventory_edit.read(self.app.profile(m.group(1)).inventory))
            if path == "/api/runs":
                return self.json(self.app.runs.list_runs((q.get("rack") or [None])[0]))
            m = re.fullmatch(r"/api/runs/([^/]+)", path)
            if m:
                d = self.app.runs.get(m.group(1))
                return self.json(d) if d else self.err(404, "no such run")
            m = re.fullmatch(r"/api/runs/([^/]+)/log", path)
            if m:
                lines, done = self.app.runs.log_text(m.group(1), int((q.get("from") or ["0"])[0]))
                return self.json({"lines": lines, "done": done})
            m = re.fullmatch(r"/api/runs/([^/]+)/stream", path)
            if m:
                last = self.headers.get("Last-Event-ID")          # EventSource reconnect resumes there
                start = int(last) if last and last.isdigit() else int((q.get("from") or ["0"])[0])
                return self._stream(m.group(1), start)
            return self.err(404, "not found")
        except (LookupError, KeyError) as e:
            return self.err(404, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return None

    def _stream(self, rid: str, start: int) -> None:
        """Server-sent events: one 'data:' per log line, 'event: end' with the run record when it is over."""
        runs = self.app.runs
        if rid not in runs.runs:
            lines, _ = runs.log_text(rid, start)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            for ln in lines:
                self.wfile.write(f"data: {json.dumps(ln)}\n\n".encode())
            self.wfile.write(f"event: end\ndata: {json.dumps(runs.get(rid), default=str)}\n\n".encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        pos = start
        try:
            while True:
                lines, done = runs.wait_lines(rid, pos, timeout=15)
                if lines:
                    self.wfile.write("".join(f"id: {pos + i + 1}\ndata: {json.dumps(ln)}\n\n"
                                             for i, ln in enumerate(lines)).encode())
                    pos += len(lines)
                elif not done:
                    self.wfile.write(b": keepalive\n\n")
                if done and not lines:
                    self.wfile.write(f"event: end\ndata: {json.dumps(runs.get(rid), default=str)}\n\n".encode())
                    self.wfile.flush()
                    return
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        if not self._host_ok():
            return self.err(403, "bad Host header")
        if self.headers.get("X-Helios-UI") != "1" or \
                not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.err(403, "missing X-Helios-UI header / JSON content type")
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/runs":
                password = body.pop("password", None) or None
                run = self.app.runs.submit(str(body.get("rack") or ""), body, password)
                del password
                return self.json(self.app.runs.get(run.id), 201)
            m = re.fullmatch(r"/api/runs/([^/]+)/stop", path)
            if m:
                return self.json(self.app.runs.stop(m.group(1)).as_dict())
            if path == "/api/select":
                return self.json({"tc": select_cases(body)})
            m = re.fullmatch(r"/api/racks/([^/]+)/report", path)
            if m:
                p = self.app.profile(m.group(1))
                name = str(body.get("run") or "")
                if not NAME_RX.match(name) or not (p.results_dir / name / "tc_summary.csv").exists():
                    return self.err(404, "no such results folder")
                out = build_report(p.results_dir / name, f"{p.name}: {name}")
                return self.json({"report": out.name})
            m = re.fullmatch(r"/api/racks/([^/]+)/inventory/(check|save)", path)
            if m:
                p = self.app.profile(m.group(1))
                if not p.inventory.is_file():
                    return self.err(404, f"inventory not found: {p.inventory}")
                with self.app.edit_lock:
                    if m.group(2) == "check":
                        return self.json(inventory_edit.prepare(p.inventory, body))
                    return self.json(inventory_edit.save(p.inventory, body))
            return self.err(404, "not found")
        except inventory_edit.EditError as e:
            return self.err(400, e.errors)
        except (JobError, ValueError) as e:
            return self.err(400, str(e))
        except (LookupError, KeyError) as e:
            return self.err(404, str(e))


def make_server(app: App, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the UI binds to the loopback address only")
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv
