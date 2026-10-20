"""Who is using the UI: one entry per browser, in memory only (a restart starts an empty list).

The access token is shared, so the login cookie is the same in every browser and says nothing about who is there.
Each browser therefore gets its own random id (cookie ``helios_ui_bid``, set on its first API request) and, if the
person types one, a name (cookie ``helios_ui_name``, kept in that browser). Neither is a secret or a login: they only
label requests of browsers that are already allowed in. The address is the peer's, or - for a request from loopback
(a reverse proxy such as nginx on the same server) - the last X-Forwarded-For / X-Real-IP entry.

"Using now" = a request in the last ACTIVE_S seconds (an open page polls every 30 s); "recently" = the last KEEP_S.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time

BID_COOKIE = "helios_ui_bid"
NAME_COOKIE = "helios_ui_name"
BID_RX = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
NAME_RX = re.compile(r"^[\w .@'()-]{1,40}$", re.UNICODE)
ACTIVE_S = 120
KEEP_S = 3600
MAX_BROWSERS = 500


def new_bid() -> str:
    return secrets.token_urlsafe(18)


def short(bid: str) -> str:
    """What other browsers see of an id: a hash, so a listed id cannot be replayed as someone else's cookie."""
    return hashlib.sha256(bid.encode()).hexdigest()[:8]


def clean_name(name) -> str:
    """A person's display name, or '' (empty / not plain text)."""
    if not isinstance(name, str):
        return ""
    name = " ".join(name.split())
    return name if NAME_RX.match(name) else ""


def browser_label(ua: str) -> str:
    ua = ua or ""
    b = next((n for k, n in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"),
                             ("curl/", "curl"), ("python", "script")) if k.lower() in ua.lower()), "browser")
    o = next((n for k, n in (("Mac OS X", "Mac"), ("Macintosh", "Mac"), ("Windows", "Windows"), ("Android", "Android"),
                             ("iPhone", "iPhone"), ("Linux", "Linux")) if k in ua), "")
    return f"{b} on {o}" if o else b


def client_ip(peer: str, headers) -> str:
    """The browser's address. Behind a proxy on this server (peer = loopback) the proxy's X-Forwarded-For /
    X-Real-IP says it; from anywhere else those headers are ignored (anyone could send them)."""
    if peer in ("127.0.0.1", "::1") or peer.startswith("::ffff:127."):
        xff = [x.strip() for x in (headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
        fwd = xff[-1] if xff else (headers.get("X-Real-IP") or "").strip()
        if fwd and re.match(r"^[0-9A-Fa-f.:]{2,45}$", fwd):
            return fwd
    return peer


class Presence:
    def __init__(self):
        self.lock = threading.Lock()
        self.seen: dict[str, dict] = {}

    def touch(self, bid: str, name: str, ip: str, ua: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.lock:
            e = self.seen.get(bid)
            if e is None:
                if len(self.seen) >= MAX_BROWSERS:
                    self._prune(now, force=True)
                e = self.seen[bid] = {"first": now, "requests": 0}
            e.update(last=now, name=name, ip=ip, browser=browser_label(ua))
            e["requests"] += 1

    def _prune(self, now: float, force: bool = False) -> None:
        for b in [b for b, e in self.seen.items() if now - e["last"] > KEEP_S]:
            del self.seen[b]
        if force and len(self.seen) >= MAX_BROWSERS:          # still full: drop the longest idle
            for b in sorted(self.seen, key=lambda b: self.seen[b]["last"])[:len(self.seen) - MAX_BROWSERS + 1]:
                del self.seen[b]

    def label(self, bid: str | None) -> str:
        """'Name (address)' / 'address' of a browser, for 'started by'."""
        with self.lock:
            e = self.seen.get(bid or "")
        if not e:
            return ""
        return f"{e['name']} ({e['ip']})" if e.get("name") else e.get("ip") or ""

    def snapshot(self, me: str | None = None, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            self._prune(now)
            rows = [{"id": short(b), "you": b == me, "name": e.get("name") or "", "ip": e.get("ip") or "",
                     "browser": e.get("browser") or "", "requests": e["requests"],
                     "first": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["first"])),
                     "last": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["last"])),
                     "idle_s": int(now - e["last"])} for b, e in self.seen.items()]
        rows.sort(key=lambda r: (r["idle_s"], r["name"] or "~", r["ip"]))
        active = [r for r in rows if r["idle_s"] <= ACTIVE_S]
        me_row = next((r for r in rows if r["you"]), None)
        return {"active": active, "recent": [r for r in rows if r["idle_s"] > ACTIVE_S],
                "active_s": ACTIVE_S, "keep_s": KEEP_S, "you": me_row,
                "people": len({(r["name"] or r["ip"]) for r in active})}
