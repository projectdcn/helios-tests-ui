"""AFM controller REST + AggWatch (WebSocket) client.

Auth (AFM API guide, https://<afm>/docs): ``POST /v1/login`` with ``{"username","password","tenant"}``
returns the user object and a ``sid`` session cookie (+ ``Grpc-Metadata-Csrf-Token`` header, sent back on
every request). No cookie -> 401. Every request and response is logged to ``helios.cmd`` (the per-test
log in the HTML report) with passwords and session ids redacted.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import ssl
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

log = logging.getLogger("helios.cmd")

CLUSTER = "/configs/cluster/v1"
AUTH = "/configs/auth/v1"
MONITORING = "/configs/monitoring/v1"
WATCH = "/configs/aggwatch/v1/watch"
LOG_BODY = 3000  # chars of each request/response body kept in the per-test log
# ... and for requests that change state (PUT / POST / DELETE) and their responses: the full vPOD / node body (g02-g17
# 18 Oct: the GPU lists of membership PUTs were cut at 3000 chars, which hid where GPUs were lost)
LOG_BODY_CHANGE = 60000

_SECRET_KEYS = re.compile(r'("(?:password|private-key|recovery-key|recoveryKey|token)"\s*:\s*)"[^"]*"', re.I)
# "key" is a secret in key material (certificates, recovery keys) but a plain field name in selectors - an alert
# policy requirement {"key": "severity", ...} (TC-CTL-098 18 Oct: the log hid which field the policy matched on).
# A short identifier-like value is a selector key and stays readable; anything else is masked.
_KEY_FIELD = re.compile(r'("key"\s*:\s*)"([^"]*)"', re.I)
_SELECTOR_KEY = re.compile(r"[A-Za-z][\w.\-/]{0,31}")


def _selector_key(v: str) -> bool:
    """'severity', 'meta.name', 'status.oper-state': short, at most 2 capitals (base64 / PEM key material is not)."""
    return bool(_SELECTOR_KEY.fullmatch(v)) and sum(ch.isupper() for ch in v) <= 2


def redact(text: str) -> str:
    text = _SECRET_KEYS.sub(r'\1"<REDACTED>"', text)
    text = _KEY_FIELD.sub(lambda m: m.group(0) if _selector_key(m.group(2)) else f'{m.group(1)}"<REDACTED>"', text)
    return re.sub(r"(sid=)[^;\s\"]+", r"\1<REDACTED>", text)


class AfmError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")


class AfmClient:
    def __init__(self, cfg: dict[str, Any], *, username: str | None = None, password: str | None = None,
                 anonymous: bool = False):
        self.cfg = cfg
        self.base = cfg["url"].rstrip("/")
        self.timeout = float(cfg.get("timeout", 30))
        self.endpoints: dict[str, str] = cfg.get("endpoints") or {}
        self.auth = cfg.get("auth") or {}
        self.kind = self.auth.get("kind", "session")
        self.tenant = self.auth.get("tenant", "default")
        self.username = username or self._secret("username") or "admin"
        self._password = password
        self.anonymous = anonymous or self.kind == "none"
        self.s = requests.Session()
        self.s.verify = cfg.get("verify_tls", False)
        if self.s.verify is False:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._logged_in = False
        self.login_response: requests.Response | None = None

    # ------------------------------------------------------------- auth
    def _secret(self, key: str) -> str | None:
        if self.auth.get(key) is not None:
            return str(self.auth[key])
        env = self.auth.get(f"{key}_env")
        return os.environ.get(env) if env else None

    @property
    def password(self) -> str | None:
        return self._password if self._password is not None else self._secret("password")

    def as_user(self, username: str, password: str) -> "AfmClient":
        """Another session on the same AFM (RBAC tests)."""
        return AfmClient(self.cfg, username=username, password=password)

    def anon(self) -> "AfmClient":
        return AfmClient(self.cfg, anonymous=True)

    def login_raw(self, username: str | None = None, password: str | None = None) -> requests.Response:
        """POST /v1/login without raising; on 200 the session cookie + CSRF token are kept."""
        body = {"username": username or self.username,
                "password": password if password is not None else self.password, "tenant": self.tenant}
        r = self._send("POST", self.auth.get("login_path", "/v1/login"), json=body)
        if r.status_code == 200:
            csrf = r.headers.get("Grpc-Metadata-Csrf-Token")
            if csrf:
                self.s.headers["Grpc-Metadata-Csrf-Token"] = csrf
            self._logged_in = True
            self.login_response = r
        return r

    def login(self) -> None:
        if self.anonymous:
            return
        if self.kind == "bearer":
            self.s.headers["Authorization"] = f"Bearer {self._secret('token')}"
            self._logged_in = True
            return
        if self.password is None:
            raise AfmError("POST", "/v1/login", 0,
                           f"no AFM password: export {self.auth.get('password_env', 'HELIOS_AFM_PASSWORD')}")
        r = self.login_raw()
        if r.status_code != 200:
            raise AfmError("POST", self.base + "/v1/login", r.status_code, r.text)

    @property
    def sid(self) -> str | None:
        return self.s.cookies.get("sid")

    # ------------------------------------------------------------- HTTP
    def _send(self, method: str, path: str, **kw) -> requests.Response:
        url = path if path.startswith("http") else self.base + path
        body = kw.get("json", kw.get("data"))
        cap = LOG_BODY if method.upper() == "GET" else LOG_BODY_CHANGE
        shown = "" if body is None else " " + redact(body if isinstance(body, str) else json.dumps(body))[:cap]
        log.debug("AFM> %s %s%s%s", method, url, shown, " (no auth)" if self.anonymous else "")
        t0 = time.monotonic()
        try:
            r = self.s.request(method, url, timeout=kw.pop("timeout", self.timeout), **kw)
        except requests.RequestException as e:
            log.debug("AFM< %s %s -> %s (%.2fs)", method, url, e, time.monotonic() - t0)
            raise
        ctype = r.headers.get("Content-Type", "")
        text = r.text if "json" in ctype or "text" in ctype or len(r.content) < 2048 else \
            f"<{len(r.content)} bytes {ctype}>"
        log.debug("AFM< %s %s -> HTTP %s (%.2fs) %s", method, url, r.status_code, time.monotonic() - t0,
                  redact(text)[:cap])
        return r

    def raw(self, method: str, path: str, body: Any = None, *, relogin: bool = True, **kw) -> requests.Response:
        """One request, never raises on HTTP status (negative tests). Logs in on first use."""
        if not self._logged_in and not self.anonymous:
            self.login()
        if body is not None:
            kw["data" if isinstance(body, str) else "json"] = body
        r = self._send(method, path, **kw)
        if r.status_code == 401 and relogin and not self.anonymous and self.kind == "session":
            log.debug("AFM: 401, session expired? re-login and retry once")
            self.login()
            r = self._send(method, path, **kw)
        return r

    def request(self, method: str, path: str, body: Any = None, **kw):
        r = self.raw(method, path, body, **kw)
        if r.status_code >= 400:
            raise AfmError(method, path, r.status_code, r.text)
        try:
            return r.json() if r.content else None
        except ValueError:
            return r.content

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw):
        return self.request("POST", path, {} if body is None else body, **kw)

    def put(self, path: str, body: Any = None, **kw):
        return self.request("PUT", path, body, **kw)

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw)

    def items(self, path: str, **kw) -> list[dict]:
        return (self.get(path, **kw) or {}).get("items") or []

    def delete_quiet(self, path: str) -> int:
        """Cleanup DELETE: returns the HTTP status, never raises."""
        try:
            return self.raw("DELETE", path).status_code
        except (requests.RequestException, AfmError) as e:
            log.debug("AFM cleanup %s failed: %s", path, e)
            return 0

    def endpoint(self, name: str, **kw) -> str:
        if name not in self.endpoints:
            raise KeyError(f"afm.endpoints.{name} not configured in inventory")
        return self.endpoints[name].format(**kw)

    # ------------------------------------------------------------- cluster objects
    def cluster(self) -> dict:
        return self.get(f"{CLUSTER}/cluster")

    def version(self) -> dict:
        return (self.get(f"{CLUSTER}/version") or {}).get("status") or {}

    def computes(self) -> list[dict]:
        return self.items(f"{CLUSTER}/computenodes")

    def switches(self) -> list[dict]:
        return self.items(f"{CLUSTER}/switchnodes")

    def vpods(self) -> list[dict]:
        return self.items(f"{CLUSTER}/vpods")

    def system_vpod(self) -> dict | None:
        return next((v for v in self.vpods() if re.fullmatch(r"pod-\d+\.system", v["meta"]["name"])), None)

    # ------------------------------------------------------------- AggWatch
    def ws_url(self) -> str:
        u = urlsplit(self.base)
        return f"{'wss' if u.scheme == 'https' else 'ws'}://{u.netloc}{WATCH}"

    def watch(self, *, cookie: str | bool | None = True, origin: str | None = None, timeout: float = 15) -> "Watch":
        """Open the AggWatch WebSocket. ``cookie=True`` uses this session's sid, a str sends that sid,
        False/None sends none. Raises websocket.WebSocketBadStatusException if the handshake is refused."""
        import websocket  # websocket-client

        if cookie is True:
            if not self._logged_in and not self.anonymous:
                self.login()
            cookie = self.sid
        hdr = []
        if cookie and self.s.headers.get("Grpc-Metadata-Csrf-Token"):
            hdr.append(f"Grpc-Metadata-Csrf-Token: {self.s.headers['Grpc-Metadata-Csrf-Token']}")
        kw: dict[str, Any] = dict(header=hdr, timeout=timeout,
                                  sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False})
        if cookie:
            kw["cookie"] = f"sid={cookie}"
        if origin is not None:
            kw["origin"] = origin
        else:
            kw["suppress_origin"] = True
        url = self.ws_url()
        log.debug("AFM WS> connect %s cookie=%s origin=%s", url, "sid=<REDACTED>" if cookie else "none", origin)
        try:
            ws = websocket.create_connection(url, **kw)
        except websocket.WebSocketBadStatusException as e:
            log.debug("AFM WS< handshake refused: HTTP %s", e.status_code)
            raise
        log.debug("AFM WS< handshake HTTP %s", ws.getstatus())
        return Watch(ws)


def handshake_status(afm: AfmClient, **kw) -> int:
    """HTTP status of an AggWatch handshake (101 = accepted); closes the connection."""
    import websocket

    try:
        w = afm.watch(**kw)
    except websocket.WebSocketBadStatusException as e:
        return e.status_code
    w.close()
    return 101


class Watch:
    """An open AggWatch connection: subscribe, then collect ``{"events":[...]}`` frames."""

    def __init__(self, ws):
        self.ws = ws
        self.events: list[dict] = []      # object events: Created / Updated / Deleted with a kind
        self.controls: list[dict] = []    # {"type":"Control","control":{"code":1,"message":"Listing for watch complete"}}
        self.errors: list[str] = []       # {"type":"Error",...} events and non-event frames
        self.closed = False
        self.closed_at: float | None = None

    @staticmethod
    def options(*kinds: str, meta: dict | None = None) -> dict:
        """kinds: "group/Kind" strings, e.g. "cluster/ComputeNode", "monitoring/AlertPolicy"."""
        opts = [{"group": k.split("/")[0], "kind": k.split("/")[1], "options": {"meta": meta or {}}} for k in kinds]
        return {"meta": {}, "watch-options": opts}

    def send(self, msg: dict | str) -> None:
        text = msg if isinstance(msg, str) else json.dumps(msg)
        log.debug("AFM WS> %s", text[:LOG_BODY])
        self.ws.send(text)

    def subscribe(self, *kinds: str, meta: dict | None = None) -> "Watch":
        self.send(self.options(*kinds, meta=meta))
        return self

    def recv(self, timeout: float) -> list[dict] | None:
        """Next frame's events ([] for a non-event frame), None on timeout or close (sets ``closed``)."""
        import websocket

        self.ws.settimeout(max(timeout, 0.05))
        try:
            op, data = self.ws.recv_data(control_frame=True)
        except websocket.WebSocketTimeoutException:
            return None
        except (websocket.WebSocketConnectionClosedException, ConnectionError, OSError) as e:
            log.debug("AFM WS< closed: %s", e)
            self.closed, self.closed_at = True, time.monotonic()
            return None
        if op == websocket.ABNF.OPCODE_CLOSE:
            log.debug("AFM WS< close frame %r", data[:200])
            self.closed, self.closed_at = True, time.monotonic()
            return None
        if op not in (websocket.ABNF.OPCODE_TEXT, websocket.ABNF.OPCODE_BINARY):
            return []
        text = data.decode(errors="replace")
        log.debug("AFM WS< %s", redact(text)[:LOG_BODY])
        try:
            frame = json.loads(text)
        except ValueError:
            frame = None
        if not isinstance(frame, dict) or "events" not in frame:
            self.errors.append(text[:300])
            return []
        evs = []
        for e in frame.get("events") or []:
            t = e.get("type")
            if t == "Control":
                self.controls.append(e)
            elif t == "Error" or not e.get("kind"):
                self.errors.append(json.dumps(e)[:300])
            else:
                evs.append(e)
        self.events.extend(evs)
        return evs

    def collect(self, duration: float, until: Callable[[list[dict]], bool] | None = None,
                ping_every: float | None = None) -> list[dict]:
        """Read frames for up to ``duration`` s, stopping early when ``until(all events)`` or the server closes.
        ``ping_every``: send a WebSocket ping that often (keepalive)."""
        end = time.monotonic() + duration
        next_ping = time.monotonic() + ping_every if ping_every else None
        while not self.closed and time.monotonic() < end:
            wait = end - time.monotonic()
            if next_ping:
                wait = min(wait, max(0.05, next_ping - time.monotonic()))
            self.recv(wait)
            if next_ping and time.monotonic() >= next_ping and not self.closed:
                try:
                    self.ws.ping()
                except Exception:  # noqa: BLE001 - closed underneath us
                    pass
                next_ping = time.monotonic() + ping_every
            if until and until(self.events):
                break
        return self.events

    def listing_complete(self) -> bool:
        return any("listing" in str((c.get("control") or {}).get("message", "")).lower() for c in self.controls)

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001 - already gone
            pass
        self.closed = True


def ev_names(events: list[dict], etype: str | None = None, kind: str | None = None) -> list[str]:
    return [((e.get("object") or {}).get("meta") or {}).get("name", "") for e in events
            if (etype is None or e.get("type") == etype) and (kind is None or e.get("kind") == kind)]


def transient(e: BaseException) -> bool:
    """An error that means "the API is not answering right now" (a controller restarting / failing over): HTTP 5xx
    or no connection. 4xx and everything else are real errors."""
    return (isinstance(e, AfmError) and e.status >= 500) or isinstance(e, requests.RequestException)


def wait_for(fn: Callable[[], Any], timeout: float, interval: float = 5, what: str = "") -> Any:
    """Poll ``fn`` until truthy; returns its last value (falsy on timeout). A transient API error (``transient``:
    HTTP 5xx, connection refused / reset / timeout) counts as "not yet", so a wait rides out a controller restart -
    g02-g17 17 Oct: TC-CTL-074 stopped the leader, the API answered 503 for ~10 s and the wait for the new leader
    crashed on the first one. The last such error is logged when the wait times out."""
    from .progress import WaitReporter
    end = time.monotonic() + timeout
    rep = WaitReporter(what or getattr(fn, "__name__", "condition"), timeout)
    last_err = None
    while True:
        try:
            v = fn()
        except Exception as e:  # noqa: BLE001 - only transient API errors are swallowed, see transient()
            if not transient(e):
                raise
            v, last_err = None, e
        if v or time.monotonic() >= end:
            if not v:
                log.debug("AFM wait: %s not reached in %ss%s", what or fn, timeout,
                          f" (last API error: {str(last_err)[:300]})" if last_err else "")
            return v
        time.sleep(max(0.0, min(interval, end - time.monotonic())))
        rep.tick()


def conditions_ok(obj: dict, ctype: str = "healthy") -> bool:
    return any(c.get("type") == ctype and str(c.get("status")).lower() == "true"
               for c in (obj.get("status") or {}).get("conditions") or [])


def rack_health(afm: AfmClient, skip=None, label=None) -> list[str]:
    """Pre/post check for AFM tests: cluster healthy, every switch + compute admitted and healthy,
    system vPOD online/degraded with no pending ops. Returns the problems found. ``skip(obj)`` -> True leaves a
    switch / compute node out (quarantined in the inventory: Inventory.is_quarantined_afm). ``label(name)`` names a
    node in the messages (AfmOps.label: 'pod-1-4-ComputeTray (ct05)')."""
    p = []
    c = afm.cluster()
    if not conditions_ok(c):
        p.append(f"cluster not healthy: {(c.get('status') or {}).get('conditions')}")
    for kind, objs in (("switch", afm.switches()), ("compute", afm.computes())):
        if skip is not None:
            objs = [o for o in objs if not skip(o)]
        if not objs:
            p.append(f"no {kind} nodes in AFM")
        for o in objs:
            st = o.get("status") or {}
            if st.get("admission-phase") != "admitted" or not conditions_ok(o):
                nm = label(o["meta"]["name"]) if (label and kind == "compute") else o["meta"]["name"]
                p.append(f"{kind} {nm}: {st.get('admission-phase')} conditions={st.get('conditions')}")
    v = afm.system_vpod()
    if v is None:
        p.append("no system vPOD (pod-N.system)")
    else:
        st = v.get("status") or {}
        if st.get("vpod-state") not in ("online", "degraded"):
            p.append(f"{v['meta']['name']} state {st.get('vpod-state')}")
        if st.get("pending-ops"):
            p.append(f"{v['meta']['name']} pending-ops {st.get('pending-ops')}")
    return p


def perm(group: str, kind: str = "_All_", actions=("all-actions",)) -> dict:
    """One RBAC Role permission entry."""
    return {"resource-group": group, "resource-kind": kind, "resource-namespace": "_All_", "actions": list(actions)}


def new_password() -> str:
    """Throw-away password for test users (mixed character classes for the AFM password policy)."""
    return f"Hx{secrets.token_urlsafe(10)}#9a"
