"""HTTP controller (standard library): JSON API + server-sent events for the live log + static page + results files.

Bound to 127.0.0.1 by default (``--host`` / HELIOS_UI_HOST for another address, e.g. 0.0.0.0 on a lab server).
With an access token (HELIOS_UI_TOKEN) every request but the page itself, /static/, /api/version and /api/login needs
the login cookie (an HMAC of the token: it survives a restart, a new token ends every login) or ``Authorization:
Bearer <token>``; without one anyone who reaches the port can use the UI. State-changing requests need ``Content-Type:
application/json`` and the ``X-Helios-UI`` header (a page from another site cannot send them without a CORS preflight, which this server never grants), and the Host
header must name this server (loopback, or its host names / addresses when bound beyond loopback; no DNS rebinding).
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import defaults, inventory_edit, plan, presence, presets, sessions, share, sshkeys
from .config import ROOT, afm_username, inventory_rack_name, load_config
from .keychain import Keychain, KeychainError, account
from .runner import JobError, NAME_RX, TC_RX
from .runs import RunManager, build_report, history
from .version import build_info, version

STATIC = Path(__file__).resolve().parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 4 * 1024 * 1024
# files of a results folder offered as single downloads (the zip has everything)
DOWNLOADABLE = ("report.html", "health.html", "tc_summary.csv", "tc_results.csv", "health.json", "health-notes.txt",
                "session.json")


def result_folder(profile, run: str) -> Path | None:
    """A results folder of the rack: a plain directory directly inside its results_dir (no symlink, no traversal,
    not the UI's own ui-runs folder), or None."""
    if not NAME_RX.match(run) or run == "ui-runs":
        return None
    base = profile.results_dir.resolve()
    d = profile.results_dir / run
    if d.is_symlink() or not d.is_dir() or d.resolve().parent != base:
        return None
    return d.resolve()


def zip_folder(folder: Path, fh) -> int:
    """Every regular file under ``folder`` (symlinks and anything resolving outside it skipped), as <name>/... in a
    zip written to ``fh``. Returns the number of files."""
    n = 0
    with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dp = Path(dirpath)
            dirnames[:] = sorted(x for x in dirnames if not (dp / x).is_symlink())
            for fn in sorted(filenames):
                f = dp / fn
                if f.is_symlink() or not f.is_file():
                    continue
                r = f.resolve()
                if folder not in r.parents:
                    continue
                z.write(r, f"{folder.name}/{r.relative_to(folder).as_posix()}")
                n += 1
    return n


class App:
    def __init__(self, config: Path | None = None, executor=None, profiles=None, keychain: Keychain | None = None):
        self.profiles = profiles if profiles is not None else load_config(config)
        self.runs = RunManager(self.profiles, executor)
        self.edit_lock = threading.Lock()
        # python -m ui passes the real one; an App built without one (tests) has no Keychain
        self.keychain = keychain if keychain is not None else Keychain(enabled=False)
        self.runs.store_label = self.store_label
        self.presence = presence.Presence()          # who is using the UI (memory only)

    def saved_passwords(self) -> list[str]:
        """The AFM passwords saved for the racks (to blank them in a shared zip); never sent anywhere."""
        out = []
        for rack in self.profiles:
            try:
                acct = self.keychain_account(rack)
                if self.keychain.available() and self.keychain.has(acct):
                    out.append(self.keychain.get(acct) or "")
            except Exception:  # noqa: BLE001 - a rack without inventory / an unreadable store: nothing to add
                pass
        return [p for p in out if p]

    @property
    def store_label(self) -> str:
        """Where a remembered AFM password goes: the macOS Keychain or the password file (Linux)."""
        return getattr(self.keychain, "label", "macOS Keychain")

    def keychain_account(self, rack: str) -> str:
        p = self.profile(rack)
        return account(p.name, afm_username(p.inventory))

    def password_status(self, rack: str) -> dict:
        """{saved, available, account[, error]}: never the password itself."""
        try:
            acct = self.keychain_account(rack)
        except KeychainError as e:
            return {"saved": False, "available": False, "account": None, "error": str(e)}
        out = {"saved": False, "available": self.keychain.available(), "account": acct,
               "store": self.store_label}
        if not out["available"]:
            out["note"] = self.keychain.unavailable_reason()
        if out["available"]:
            try:
                out["saved"] = self.keychain.has(acct)
            except KeychainError as e:
                out["error"] = str(e)
        return out

    def start_run(self, body: dict, started_by: str | None = None):
        """POST /api/runs: a typed password wins (saved to the Keychain first when ``remember_password``); an empty
        one falls back to the rack's Keychain password. Keychain trouble never stops the run: it is noted in the
        log header and in the response (``keychain``)."""
        rack = str(body.get("rack") or "")
        if rack not in self.profiles:
            raise JobError(f"unknown rack {rack!r}")
        password = body.pop("password", None) or None
        remember = body.pop("remember_password", None) is True
        source, notes, kc = None, [], {}
        if password is not None and not isinstance(password, str):
            raise JobError("password must be text")
        try:
            if password:
                source = "typed"
                if remember:
                    try:
                        self.keychain.save(self.keychain_account(rack), password)
                        kc["saved"] = True
                        notes.append(f"AFM password saved in the {self.store_label}")
                    except KeychainError as e:
                        kc["error"] = str(e)
                        notes.append(f"AFM password NOT saved in the {self.store_label}: {e}")
            elif self.keychain.available():
                try:
                    password = self.keychain.get(self.keychain_account(rack))
                    source = "keychain" if password else None
                except KeychainError as e:
                    kc["error"] = str(e)
                    notes.append(f"no AFM password typed and the {self.store_label} could not be read: {e}")
            run = self.runs.submit(rack, body, password, source, notes, started_by)
        finally:
            del password
        return run, kc

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


def select_cases(filters: dict, profile) -> list[str]:
    """tools/select_cases.py (read only) on the rack's test-plan workbook (``workbook:`` in ui/racks.yaml). No
    fallback: another workbook layout would filter on the wrong columns, so select_cases checks the header."""
    wbp = profile.workbook
    if not wbp:
        raise JobError(f"no test-plan workbook configured for {profile.name}: set 'workbook:' in ui/racks.yaml")
    if not wbp.is_file() or wbp.suffix != ".xlsx":
        raise JobError(f"{profile.name}: workbook not found: {wbp}")
    args = [sys.executable, "tools/select_cases.py", str(wbp), "--suite", filters.get("suite") or "all"]
    for k in ("level", "status", "scope", "origin", "disruptive", "health"):
        v = str(filters.get(k) or "").strip()
        if v:
            args += [f"--{k}", v]
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise JobError("select_cases: " + (r.stderr or r.stdout)[-400:])
    return [t for t in r.stdout.strip().split(",") if t]


def case_history(app, tc: str) -> dict:
    """{index, rows} for one plan id across every rack: results/index.csv lives in the results root shared by the
    profiles (<root>/runs/<rack>/)."""
    if not TC_RX.match(tc):
        raise JobError(f"not a test case id: {tc!r}")
    roots = {p.results_dir.parent.parent for p in app.profiles.values() if p.results_dir.parent.name == "runs"}
    sys.path.insert(0, str(Path(ROOT, "tools")))
    import results_index
    rows = [r for root in sorted(roots) for r in results_index.history(tc, root)]
    rows.sort(key=lambda r: r["started"], reverse=True)
    known = set(app.profiles)
    for r in rows:      # link only into racks this UI serves (the /results/<rack>/<run>/... route)
        r["log_url"] = (f"/results/{r['rack']}/{r['run']}/{r['log'].split('/', 3)[3]}"
                        if r["rack"] in known and r["log"].count("/") >= 3 else "")
        r["report_url"] = f"/results/{r['rack']}/{r['run']}/report.html" if r["rack"] in known else ""
    return {"index": [str(Path(r) / "index.csv") for r in sorted(roots)], "rows": rows}


AUTH_COOKIE = "helios_ui_auth"
AUTH_MAX_AGE = 30 * 24 * 3600                      # a login lasts 30 days in that browser
OPEN_PATHS = {"/", "/index.html", "/api/version", "/api/login", "/api/logout"}   # no login needed (no rack data)
MIN_TOKEN = 16


def latest_access(p) -> dict:
    """{found, folder, ...access.json} of the rack's newest <rack>-access-* results folder."""
    try:
        rack = inventory_rack_name(p.inventory)
    except Exception:  # noqa: BLE001
        rack = p.name
    dirs = sorted((d for d in p.results_dir.glob(f"{rack}-access-*") if (d / "access.json").is_file()),
                  key=lambda d: d.name) if p.results_dir.is_dir() else []
    if not dirs:
        return {"found": False}
    try:
        return {"found": True, "folder": dirs[-1].name, **json.loads((dirs[-1] / "access.json").read_text())}
    except (OSError, ValueError) as e:
        return {"found": False, "error": f"{dirs[-1].name}/access.json: {e}"}


def login_hint() -> dict:
    """What the login page shows (never the token): this server, who runs the UI (HELIOS_UI_ADMIN, else the OS user)
    and the command that prints the token on the server (an installed UI: <prefix>/bin/helios-ui token)."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = ""
    helper = ROOT.parent / "bin" / "helios-ui"
    return {"server": socket.gethostname().split(".")[0],
            "admin": (os.environ.get("HELIOS_UI_ADMIN") or "").strip() or user,
            "token_cmd": f"{helper} token" if helper.is_file() else "grep HELIOS_UI_TOKEN <prefix>/config/ui.env"}


def auth_cookie_value(token: str) -> str:
    return hmac.new(token.encode(), b"helios-ui-login-v1", hashlib.sha256).hexdigest()


class Handler(BaseHTTPRequestHandler):
    app: App = None          # set by make_server
    server_names: frozenset = frozenset()     # Host names accepted besides loopback (set by make_server)
    server_version = "helios-ui"
    token: str | None = None                  # access token (set by make_server); None = no login

    def log_message(self, fmt, *args):        # quiet; request bodies (passwords) are never logged anyway
        pass

    def end_headers(self):
        for c in getattr(self, "_cookies", ()):   # the browser id / name cookies of ui/presence.py
            self.send_header("Set-Cookie", c)
        self._cookies = []
        super().end_headers()

    def _cookie(self, name: str) -> str:
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return ""
        return c[name].value if name in c else ""

    def _who(self) -> tuple[str, str]:
        """(browser id, name) of this request; a browser without an id gets one (cookie set on this response)."""
        bid = self._cookie(presence.BID_COOKIE)
        if not presence.BID_RX.match(bid):
            bid = presence.new_bid()
            self._cookies = [f"{presence.BID_COOKIE}={bid}; Path=/; Max-Age={365 * 24 * 3600}; HttpOnly; SameSite=Strict"]
        return bid, presence.clean_name(unquote(self._cookie(presence.NAME_COOKIE)))

    def _touch(self, path: str) -> None:
        """Count this request as the browser being here (not the installer's /api/version check, not static)."""
        if path in OPEN_PATHS or path.startswith("/static/") or self.headers.get("Authorization"):
            return                                # (a Bearer request is a script, e.g. helios-ui users)
        had = presence.BID_RX.match(self._cookie(presence.BID_COOKIE))
        bid, name = self._who()
        self.bid = bid
        if not had:                               # no id yet: a probe / script, or a page loaded before this
            return                                # version - counted from its next request (it has the id then)
        self.app.presence.touch(bid, name, presence.client_ip(self.client_address[0], self.headers),
                                self.headers.get("User-Agent") or "")

    def _by(self) -> str:
        return self.app.presence.label(getattr(self, "bid", None))

    def _set_name(self, body: dict) -> None:
        raw = body.get("name")
        name = presence.clean_name(raw)
        if raw not in (None, "") and not name:
            return self.err(400, "name: 1-40 letters, digits, spaces and . @ ' ( ) - _")
        bid, _ = self._who()
        self.app.presence.touch(bid, name, presence.client_ip(self.client_address[0], self.headers),
                                self.headers.get("User-Agent") or "")
        v = quote(name, safe="")
        self._cookies = [c for c in getattr(self, "_cookies", [])] + [
            f"{presence.NAME_COOKIE}={v}; Path=/; Max-Age={(365 * 24 * 3600) if name else 0}; HttpOnly; SameSite=Strict"]
        return self.json(self.app.presence.snapshot(bid))

    def _share(self, rack: str, run: str) -> None:
        """The results folder as a zip with passwords / tokens / keys blanked out (ui/share.py)."""
        folder = result_folder(self.app.profile(rack), run)
        if folder is None:
            return self.err(404, "no such results folder")
        values = share.known_secrets([*self.app.saved_passwords(), *( [self.token] if self.token else [])])
        with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as fh:
            share.share_zip(folder, fh, values, shared_by=self._by())
            size = fh.tell()
            fh.seek(0)
            return self._attachment(fh, size, "application/zip", f"{folder.name}-shared.zip")

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
        """The Host header names this server: the loopback address, or (bound to another address, e.g. 0.0.0.0)
        this machine's host names, the address the request came in on, or HELIOS_UI_ALLOWED_HOSTS. Anything else
        is refused (no DNS rebinding)."""
        h = self.headers.get("Host") or ""
        host = (h.split("]")[0] + "]" if h.startswith("[") else h.split(":")[0]).lower()
        if host in ALLOWED_HOSTS:
            return True
        if not self.server_names:
            return False
        if host in self.server_names:
            return True
        try:
            local = self.connection.getsockname()[0]
        except OSError:
            return False
        return host.strip("[]") == str(local).lower()

    def _authed(self) -> bool:
        if not self.token:
            return True
        a = self.headers.get("Authorization") or ""
        if a.startswith("Bearer ") and hmac.compare_digest(a[7:].strip().encode(), self.token.encode()):
            return True
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return False
        got = c[AUTH_COOKIE].value if AUTH_COOKIE in c else ""
        return hmac.compare_digest(got.encode(), auth_cookie_value(self.token).encode())

    def _need_login(self, path: str) -> bool:
        """True (and a 401 sent) when this request needs the login it does not have."""
        if path in OPEN_PATHS or path.startswith("/static/") or self._authed():
            return False
        self.err(401, "login required: enter the UI access token", login=True)
        return True

    def _login(self, body: dict) -> None:
        tok = body.get("token")
        if not self.token:
            return self.json({"ok": True, "auth": False})
        if not isinstance(tok, str) or not hmac.compare_digest(tok.strip().encode(), self.token.encode()):
            time.sleep(1)                         # slows guessing (one thread per request)
            return self.err(401, "wrong access token", login=True)
        cookie = f"{AUTH_COOKIE}={auth_cookie_value(self.token)}; Path=/; Max-Age={AUTH_MAX_AGE}; HttpOnly; SameSite=Strict"
        self._send(200, b'{"ok": true, "auth": true}', "application/json", {"Set-Cookie": cookie})

    def _logout(self) -> None:
        self._send(200, b'{"ok": true}', "application/json",
                   {"Set-Cookie": f"{AUTH_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        d = json.loads(raw or b"{}")
        if not isinstance(d, dict):
            raise ValueError("JSON object expected")
        return d

    def _attachment(self, fh, size: int, ctype: str, filename: str) -> None:
        """Send an open file as a download (filename comes from validated names only)."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            shutil.copyfileobj(fh, self.wfile, 256 * 1024)

    def _download_result(self, rack: str, run: str, what: str) -> None:
        p = self.app.profile(rack)
        folder = result_folder(p, run)
        if folder is None:
            return self.err(404, "no such results folder")
        if what == "zip":
            with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as fh:
                zip_folder(folder, fh)
                size = fh.tell()
                fh.seek(0)
                return self._attachment(fh, size, "application/zip", f"{folder.name}.zip")
        if what not in DOWNLOADABLE:
            return self.err(404, f"downloadable files: {', '.join(DOWNLOADABLE)}")
        f = folder / what
        if f.is_symlink() or not f.is_file() or f.resolve().parent != folder:
            return self.err(404, "not found")
        ctype = {".html": "text/html", ".json": "application/json"}.get(f.suffix, "text/plain") + "; charset=utf-8"
        with open(f, "rb") as fh:
            return self._attachment(fh, f.stat().st_size, ctype, f"{folder.name}-{what}")

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
        if self._need_login(path):
            return
        self._touch(path)
        try:
            if path in ("/", "/index.html"):
                self._who()                        # the page's own API calls then share one browser id
                return self._file(STATIC, "index.html")
            if path.startswith("/static/"):
                return self._file(STATIC, path[len("/static/"):])
            m = re.fullmatch(r"/results/([^/]+)/([^/]+)/(.+)", path)
            if m:
                p = self.app.profile(m.group(1))
                if not NAME_RX.match(m.group(2)):
                    return self.err(404, "not found")
                return self._file(p.results_dir / m.group(2), m.group(3))
            if path == "/api/version":             # installer health check; no secrets
                bi = build_info() or {}
                return self.json({"version": version(), "commit": bi.get("commit"), "built": bi.get("built"),
                                  "root": str(ROOT), "auth": bool(self.token),
                                  **(login_hint() if self.token else {})})
            if path == "/api/users":               # who is using the UI now (ui/presence.py)
                return self.json(self.app.presence.snapshot(getattr(self, "bid", None)))
            if path == "/api/racks":
                return self.json([{**p.as_dict(), **self.app.runs.busy(n)} for n, p in self.app.profiles.items()])
            m = re.fullmatch(r"/api/cases/([^/]+)/history", path)
            if m:                      # every run of one case, from results/index.csv (tools/results_index.py)
                return self.json(case_history(self.app, m.group(1)))
            m = re.fullmatch(r"/api/racks/([^/]+)/history", path)
            if m:
                return self.json(history(self.app.profile(m.group(1))))
            m = re.fullmatch(r"/api/racks/([^/]+)/inventory", path)
            if m:
                return self.json(inventory_edit.read(self.app.profile(m.group(1)).inventory))
            m = re.fullmatch(r"/api/racks/([^/]+)/sshkey", path)
            if m:                                  # Setup tab: the server's keys; public keys only, never a private one
                p = self.app.profile(m.group(1))
                try:
                    tg = sshkeys.targets(p)
                except Exception as e:  # noqa: BLE001 - e.g. no inventory yet
                    tg, err = [], str(e)
                else:
                    err = None
                return self.json({"using": sshkeys.using(p), "chosen": sshkeys.chosen(p), "keys": sshkeys.list_keys(),
                                  "targets": tg, "targets_error": err, "runner": p.runner,
                                  "server": socket.gethostname().split(".")[0]})
            m = re.fullmatch(r"/api/racks/([^/]+)/access", path)
            if m:                                  # the latest Check access result (tools/access_check.py)
                return self.json(latest_access(self.app.profile(m.group(1))))
            m = re.fullmatch(r"/api/racks/([^/]+)/presets", path)
            if m:
                return self.json(presets.list_presets(self.app.profile(m.group(1))))
            m = re.fullmatch(r"/api/racks/([^/]+)/defaults", path)
            if m:
                return self.json(defaults.get(self.app.profile(m.group(1))))
            m = re.fullmatch(r"/api/racks/([^/]+)/sessions", path)
            if m:                                  # the rack's sessions (newest first) + the current one
                return self.json(sessions.list_sessions(self.app.profile(m.group(1))))
            m = re.fullmatch(r"/api/racks/([^/]+)/password", path)
            if m:                                  # {saved: bool, ...}: NEVER the password
                return self.json(self.app.password_status(m.group(1)))
            m = re.fullmatch(r"/api/racks/([^/]+)/plan", path)
            if m:
                p = self.app.profile(m.group(1))
                try:
                    return self.json(plan.load(p))
                except JobError as e:         # no / missing workbook, other layout: say so (not a server fault)
                    return self.err(400, str(e))
            m = re.fullmatch(r"/api/racks/([^/]+)/results/([^/]+)/zip", path)
            if m:
                return self._download_result(m.group(1), m.group(2), "zip")
            m = re.fullmatch(r"/api/racks/([^/]+)/results/([^/]+)/share", path)
            if m:
                return self._share(m.group(1), m.group(2))
            m = re.fullmatch(r"/api/racks/([^/]+)/results/([^/]+)/file/([^/]+)", path)
            if m:
                return self._download_result(m.group(1), m.group(2), m.group(3))
            if path == "/api/runs":
                return self.json(self.app.runs.list_runs((q.get("rack") or [None])[0]))
            m = re.fullmatch(r"/api/runs/([^/]+)", path)
            if m:
                d = self.app.runs.get(m.group(1))
                return self.json(d) if d else self.err(404, "no such run")
            m = re.fullmatch(r"/api/runs/([^/]+)/log\.txt", path)
            if m:
                if not NAME_RX.match(m.group(1)):
                    return self.err(404, "no such run")
                lines, _ = self.app.runs.log_text(m.group(1))       # running: what is there so far (redacted)
                body = "".join(ln + "\n" for ln in lines).encode()
                return self._send(200, body, "text/plain; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{m.group(1)}.log.txt"'})
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
        except JobError as e:                  # e.g. an unreadable ui-presets.json
            return self.err(500, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return None
        except OSError as e:                   # a folder / file the server cannot read: say so, do not drop the request
            return self.err(500, f"server: {e.strerror or e}{': ' + str(e.filename) if e.filename else ''}")

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
        if self._need_login(path):
            return
        self._touch(path)
        try:
            body = self._body()
            if path == "/api/login":
                return self._login(body)
            if path == "/api/logout":
                return self._logout()
            if path == "/api/me":                  # this browser's display name ("" = forget it)
                return self._set_name(body)
            if path == "/api/runs":
                run, kc = self.app.start_run(body, started_by=self._by() or None)
                return self.json({**self.app.runs.get(run.id), "keychain": kc}, 201)
            m = re.fullmatch(r"/api/racks/([^/]+)/password(/delete)?", path)
            if m:                                  # Keychain / password file: save / forget; never sent back
                rack = m.group(1)
                acct = self.app.keychain_account(rack)
                if m.group(2):
                    return self.json({"deleted": self.app.keychain.delete(acct), **self.app.password_status(rack)})
                pw = body.pop("password", None)
                self.app.keychain.save(acct, pw)
                del pw
                return self.json(self.app.password_status(rack))
            m = re.fullmatch(r"/api/racks/([^/]+)/defaults(/delete)?", path)
            if m:
                p = self.app.profile(m.group(1))
                if m.group(2):
                    return self.json({"deleted": defaults.delete(p)})
                return self.json(defaults.save(p, body.get("settings")), 201)
            m = re.fullmatch(r"/api/runs/([^/]+)/stop", path)
            if m:
                return self.json(self.app.runs.stop(m.group(1), by=self._by() or None).as_dict())
            m = re.fullmatch(r"/api/racks/([^/]+)/sessions", path)
            if m:                                  # a new session becomes the rack's current one
                return self.json(sessions.new_session(self.app.profile(m.group(1)), body.get("name")), 201)
            m = re.fullmatch(r"/api/racks/([^/]+)/sessions/([^/]+)/rebuild", path)
            if m:
                p = self.app.profile(m.group(1))
                sid = m.group(2)
                if not NAME_RX.match(sid) or not sessions.is_session(sid):
                    return self.err(404, "no such session")
                s = sessions.get(p, sid)
                if not s.get("runs"):
                    return self.err(400, "this session has no results yet")
                out = sessions.rebuild(p, sid)
                return self.json({"id": sid, "folder": out.name, "summary": sessions.summary_text(sessions.counts_of(out))})
            if path == "/api/select":
                p = self.app.profile(str(body.get("rack") or ""))
                return self.json({"tc": select_cases(body, p), "workbook": p.workbook.name})
            m = re.fullmatch(r"/api/racks/([^/]+)/plan/select", path)
            if m:
                return self.json(plan.pick(self.app.profile(m.group(1)), body.get("filters")))
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
                with self.app.edit_lock:
                    if m.group(2) == "check":
                        return self.json(inventory_edit.prepare(p.inventory, body))
                    return self.json(inventory_edit.save(p.inventory, body))
            m = re.fullmatch(r"/api/racks/([^/]+)/sshkey/(create|use|install)", path)
            if m:                                  # Setup tab; an install password is used once, never kept or logged
                p = self.app.profile(m.group(1))
                if m.group(2) == "create":
                    k = sshkeys.create(str(body.get("name") or sshkeys.DEFAULT_NAME))
                    if body.get("use", True):
                        sshkeys.choose(p, k["path"])
                    return self.json(k)
                if m.group(2) == "use":
                    sshkeys.choose(p, body.get("key") or None)
                    return self.json({"chosen": sshkeys.chosen(p), "using": sshkeys.using(p)})
                pws = body.pop("passwords", None) or {}
                npws = body.pop("node_passwords", None) or {}
                for d_, what in ((pws, "passwords: {user: password}"), (npws, "node_passwords: {node: password}")):
                    if not isinstance(d_, dict) or not all(isinstance(v, str) for v in d_.values()):
                        raise JobError(what)
                nodes = body.get("nodes") or None
                if nodes is not None and (not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes)):
                    raise JobError("nodes: [node name, ...]")
                try:
                    res = sshkeys.install(p, str(body.get("key") or sshkeys.chosen(p) or ""), pws, nodes, npws)
                finally:
                    del pws, npws
                return self.json({"results": res})
            m = re.fullmatch(r"/api/racks/([^/]+)/presets(/delete)?", path)
            if m:
                p = self.app.profile(m.group(1))
                if m.group(2):
                    presets.delete(p, body.get("name"))
                    return self.json({"deleted": presets.check_name(body.get("name"))})
                return self.json(presets.save(p, body.get("name"), body.get("preset"),
                                              overwrite=body.get("overwrite") is True), 201)
            return self.err(404, "not found")
        except presets.PresetExists as e:
            return self.err(409, str(e), exists=True)
        except inventory_edit.EditError as e:
            return self.err(400, e.errors)
        except (JobError, ValueError) as e:
            return self.err(400, str(e))
        except (LookupError, KeyError) as e:
            return self.err(404, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return None
        except OSError as e:                   # e.g. a results folder the server cannot create: an answer, not a drop
            return self.err(500, f"server: {e.strerror or e}{': ' + str(e.filename) if e.filename else ''}")


LOOPBACK = ("127.0.0.1", "::1", "localhost")


def is_loopback(host: str) -> bool:
    return host in LOOPBACK


def host_names() -> frozenset:
    """Names a browser may use for this machine when the UI listens beyond loopback: host name, FQDN, short name,
    their addresses, and HELIOS_UI_ALLOWED_HOSTS (comma-separated DNS aliases / addresses)."""
    names = set()
    try:
        hn = socket.gethostname()
        fq = socket.getfqdn()
        names |= {hn, fq, hn.split(".")[0], fq.split(".")[0]}
        for n in (hn, fq):
            try:
                names |= {ai[4][0] for ai in socket.getaddrinfo(n, None)}
            except OSError:
                pass
    except OSError:
        pass
    names |= {x.strip() for x in os.environ.get("HELIOS_UI_ALLOWED_HOSTS", "").split(",") if x.strip()}
    return frozenset(n.lower() for n in names if n)


def make_server(app: App, port: int = 8765, host: str = "127.0.0.1", token: str | None = None) -> ThreadingHTTPServer:
    """Bound to ``host``: 127.0.0.1 (default; only this machine) or e.g. 0.0.0.0 / an address of this machine.
    ``token``: the access token every browser logs in with once; None = no login (anyone who reaches the port)."""
    if token is not None and len(token) < MIN_TOKEN:
        raise ValueError(f"the access token needs at least {MIN_TOKEN} characters")
    extra = {"app": app, "token": token or None}
    if not is_loopback(host):
        extra["server_names"] = host_names() | {host.lower()}
    handler = type("BoundHandler", (Handler,), extra)
    # SO_REUSEADDR (explicit; http.server sets it too): a just-closed socket in TIME_WAIT never blocks a restart
    attrs = {"allow_reuse_address": True}
    if ":" in host:                               # IPv6 address
        attrs["address_family"] = socket.AF_INET6
    cls = type("HeliosHTTPServer", (ThreadingHTTPServer,), attrs)
    srv = cls((host, port), handler)
    srv.daemon_threads = True
    return srv
