"""AFM controller REST client.

Endpoint paths are inventory-driven (``afm.endpoints``) because the AFM API
reference isn't vendored here yet; tests call ``client.endpoint("name", **kw)``.
"""
from __future__ import annotations

import os
from typing import Any

import requests


class AfmError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")


class AfmClient:
    def __init__(self, cfg: dict[str, Any]):
        self.base = cfg["url"].rstrip("/")
        self.timeout = float(cfg.get("timeout", 30))
        self.endpoints: dict[str, str] = cfg.get("endpoints") or {}
        self.auth = cfg.get("auth") or {}
        self.s = requests.Session()
        self.s.verify = cfg.get("verify_tls", False)
        if self.s.verify is False:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._logged_in = False

    def _secret(self, key: str) -> str | None:
        if self.auth.get(key) is not None:
            return str(self.auth[key])
        env = self.auth.get(f"{key}_env")
        return os.environ.get(env) if env else None

    def login(self) -> None:
        kind = self.auth.get("kind", "session")
        if kind == "bearer":
            self.s.headers["Authorization"] = f"Bearer {self._secret('token')}"
        elif kind == "session":
            body = {self.auth.get("user_field", "username"): self._secret("username"),
                    self.auth.get("password_field", "password"): self._secret("password")}
            url = self.base + self.auth.get("login_path", "/v1/login")
            r = self.s.post(url, json=body, timeout=self.timeout)
            if r.status_code >= 400:
                raise AfmError("POST", url, r.status_code, r.text)
        elif kind != "none":
            raise ValueError(f"unknown afm.auth.kind {kind!r}")
        self._logged_in = True

    def request(self, method: str, path: str, *, expect_error: bool = False, **kw) -> requests.Response | Any:
        if not self._logged_in:
            self.login()
        url = path if path.startswith("http") else self.base + path
        r = self.s.request(method, url, timeout=self.timeout, **kw)
        if r.status_code == 401 and self.auth.get("kind", "session") == "session" and not expect_error:
            self.login()
            r = self.s.request(method, url, timeout=self.timeout, **kw)
        if expect_error:
            return r
        if r.status_code >= 400:
            raise AfmError(method, url, r.status_code, r.text)
        return r.json() if r.content else None

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw):
        return self.request("POST", path, json=body, **kw)

    def put(self, path: str, body: Any = None, **kw):
        return self.request("PUT", path, json=body, **kw)

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw)

    def endpoint(self, name: str, **kw) -> str:
        if name not in self.endpoints:
            raise KeyError(f"afm.endpoints.{name} not configured in inventory")
        return self.endpoints[name].format(**kw)
