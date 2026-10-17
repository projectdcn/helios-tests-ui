"""In-process fake AFM controller (REST + AggWatch WebSocket) for the offline self-test of tests/afm.

Models the behaviour documented in results/extract-afm-api.md: /v1/login + sid cookie, 401 without it,
RBAC by Role permissions, Status error envelopes, audit records, metrics, techsupport and the AggWatch
stream. ``faults`` switches individual behaviours off so the self-test can prove each test catches them.

    srv = FakeAfm(faults={"cookie_flags"}).start(); ...; srv.stop()
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import queue
import re
import secrets
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

ADMIN = ("admin", "selftest-admin-pw")  # fake credentials (selftest/inventory.yaml)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CLUSTER_KINDS = {"computenodes": "ComputeNode", "switchnodes": "SwitchNode", "vpods": "VPod", "pods": "Pod",
                 "failoverpolicies": "FailoverPolicy"}
KNOWN_NO_SERIES = {"InBroadcast", "InErrors", "InMulticast", "OutDiscards", "OutErrors", "PauseRxPkts",
                   "PauseTxPkts", "PfcRxPkts", "PfcTxPkts"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def obj(kind: str, name: str, spec=None, status=None, **meta) -> dict:
    return {"kind": kind, "api-version": "v1",
            "meta": {"name": name, "tenant": "default", "uuid": str(uuid.uuid4()), "creation-time": now(),
                     "mod-time": now(), "generation-id": "1", **meta},
            "spec": spec or {}, "status": status or {}}


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else datetime.min.replace(tzinfo=timezone.utc)


def status_env(code: int, msg: str, text: str) -> dict:
    return {"T": {"kind": "Status"}, "result": {"Str": text}, "message": [msg], "code": code, "object-ref": {}}


class State:
    # fake LDAP / RADIUS directories (selftest only): directory user -> password
    DIRECTORY = {"ldap": {"ldapuser": "selftest-dir-pw"}, "radius": {"raduser": "selftest-dir-pw"}}

    def __init__(self, faults: set[str], topology: dict | None = None):
        self.f = faults
        self.lock = threading.RLock()
        self.sessions: dict[str, str] = {}
        self.users = {"admin": {"pw": ADMIN[1], "obj": obj("User", "admin", {"type": "local"},
                                                           {"roles": ["AdminRole"], "authenticators": ["local"]})}}
        self.roles: dict[str, dict] = {}
        self.bindings: dict[str, dict] = {}
        self.watchers: list[dict] = []
        self.audit: list[dict] = []
        self.techsupport: dict[str, dict] = {}
        self.alerts: list[dict] = []
        self.rv = __import__("itertools").count(1000)
        self.snapshot: dict | None = None
        self.recovery_key_taken = False
        self.version = "0.1.0-AICC-797"
        self.authn = obj("AuthenticationPolicy", "authn-policy",
                         {"authenticators": {"local": {"enabled": True}},
                          "lockout": {"max-failed-login-attempts": 5, "lockout-duration-seconds": 900}})
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from fake_rack import Sim

        topo = topology or {}
        self.sim = Sim(self, int(topo.get("switches", 1)), int(topo.get("computes", 2)), topo.get("mch"))
        self.sim.time_scale = float(topo.get("time_scale", 1))
        n_ct = int(topo.get("computes", 2))
        self.objs = {
            "ComputeNode": self.sim.computes,
            "SwitchNode": self.sim.switches,
            "Pod": {"pod-1": obj("Pod", "pod-1", {"rack-type": {"full": "heliosmch", "mini": "heliosmchmini"}.get(topo.get("mch"), "minirack"),
                                              "cabling": "mr3"}, {"state": "active"})},
            "VPod": {"pod-1.system": obj("VPod", "pod-1.system",
                                         {"GPUs": [{"slot-id": s, "index": i} for s in range(n_ct) for i in range(4)],
                                          "parent-pod": "pod-1", "failover-policy": "system-failover-policy"},
                                         {"vpod-state": "degraded", "provisioned-switch-planes": 36,
                                          "available-switch-planes": 33, "effective-switch-planes": 33,
                                          "pending-ops": [], "conditions": []})},
            "FailoverPolicy": {"system-failover-policy": obj("FailoverPolicy", "system-failover-policy",
                                                             {"datafabric-reconfig": "automatic",
                                                              "link-fail-threshold": 10})},
            "SyslogExport": {},
            "AlertPolicy": {"default-event-based-alerts": obj("AlertPolicy", "default-event-based-alerts", {   # as g02-g17
                "resource": "Event", "severity": "critical", "enable": True,
                "requirements": [{"key": "severity", "operator": "equals", "values": ["critical"]}]})},
        }
        # health-check faults (tests/afm/test_afm_health.py)
        if "leftover_test_objects" in faults:     # an aborted run left its objects behind
            self.objs["FailoverPolicy"]["helios-afm-016-policy-123"] = obj(
                "FailoverPolicy", "helios-afm-016-policy-123", {"datafabric-reconfig": "manual", "link-fail-threshold": 10})
            self.roles["helios-ctl-117-role"] = obj("Role", "helios-ctl-117-role", {"permissions": []})
        if "critical_event" in faults:
            self.sim.events.append({"kind": "Event", "meta": {"name": "crit-1", "creation-time": now(), "mod-time": now()},
                                    "severity": "critical", "type": "vpod_configuration_failed",
                                    "message": "vPOD pod-1.system configuration failed",
                                    "object-ref": {"kind": "VPod", "name": "pod-1.system"}})
        if "controller_restarted" in faults:      # afm-controller on the first switch restarted a minute ago
            self.sim.ctrl_started[0] = time.time() - 60
            self.sim.ctrl_restarts[0] += 1

    # ---------------------------------------------------------------- RBAC
    def allowed(self, user: str, group: str, kind: str, action: str) -> bool:
        if user == "admin" or "rbac_bypass" in self.f:
            return True
        for b in self.bindings.values():
            if user not in b["spec"].get("users", []):
                continue
            role = self.roles.get(b["spec"].get("role"))
            for p in (role or {}).get("spec", {}).get("permissions", []):
                if p.get("resource-group") not in (None, group):
                    continue
                if p.get("resource-kind") not in ("_All_", kind):
                    continue
                acts = p.get("actions") or []
                if "all-actions" in acts or action in acts:
                    return True
        return False

    def user_roles(self, user: str) -> list[str]:
        return [b["spec"]["role"] for b in self.bindings.values() if user in b["spec"].get("users", [])]

    # ---------------------------------------------------------------- audit + watch
    def record(self, user: str, action: str, kind: str, name: str, uri: str) -> None:
        if "audit_missing_delete" in self.f and action == "Delete":
            return
        for stage in ("requestauthorization", "requestprocessing"):
            self.audit.append({"kind": "AuditEvent", "meta": {"uuid": str(uuid.uuid4()), "creation-time": now()},
                               "action": action, "stage": stage, "outcome": "success", "user": {"name": user},
                               "resource": {"kind": kind, "name": name}, "request-uri": uri,
                               "service-name": "pen-apiserver"})

    def publish(self, etype: str, group: str, kind: str, o: dict) -> None:
        if etype == "Updated" and "no_update_event" in self.f:
            return
        for w in list(self.watchers):
            if (group, kind) in w["kinds"]:
                w["q"].put({"type": etype, "kind": kind, "group": group, "object": copy.deepcopy(o)})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    st: State  # set by FakeAfm

    def log_message(self, *a):  # quiet
        pass

    # ---------------------------------------------------------------- plumbing
    def _send(self, code: int, body, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if not isinstance(body, bytes) else "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code: int, msg: str, text: str = ""):
        texts = {400: "Bad Request", 401: "Authentication failed", 403: "Authorization failed", 404: "Not Found",
                 409: "Conflict", 412: "Failed pre conditions"}
        self._send(code, status_env(code, msg, text or texts.get(code, "")))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return ValueError

    def _user(self) -> str | None:
        m = re.search(r"sid=([^;\s]+)", self.headers.get("Cookie", ""))
        return self.st.sessions.get(m.group(1)) if m else None

    # ---------------------------------------------------------------- routing
    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self._ws()
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str):
        u = urlsplit(self.path)
        path, qs = u.path.rstrip("/"), parse_qs(u.query)
        body = self._body() if method in ("POST", "PUT") else {}
        st = self.st
        if path == "/v1/login" and method == "POST":
            return self._login(body)
        user = self._user()
        if user is None and not ("unauth_ok" in st.f and method == "GET"):
            return self._err(401, "Not authenticated")
        user = user or "admin"
        if "api_down_on_one_node" in st.f and not all(st.sim.ctrl_up.values()) and method != "GET":
            return self._err(503, "controller quorum degraded")
        if body is ValueError:
            return self._err(400, "invalid JSON body")
        with st.lock:
            st.sim.refresh(st.objs)
            return self._dispatch(method, path, qs, body, user)

    def _deny(self, user, group, kind, action) -> bool:
        if self.st.allowed(user, group, kind, action):
            return False
        self._err(403, f"user {user} not authorized for {action} on {group}/{kind}")
        return True

    def _dispatch(self, method, path, qs, body, user):
        st = self.st
        action = "read" if method == "GET" else {"POST": "create", "PUT": "update", "DELETE": "delete"}[method]
        p = path.split("/")
        # ---- cluster group
        if path.startswith("/configs/cluster/v1/"):
            res = p[4]
            if res in ("cluster", "version"):
                if self._deny(user, "cluster", res.capitalize(), "read"):
                    return
                if res == "version":
                    return self._send(200, obj("Version", "version", status={"build-version": st.version}))
                up = st.sim.ctrl_up
                down = "quorum_down" in st.f or sum(up.values()) * 2 <= len(up)
                return self._send(200, obj("Cluster", "cluster", status={
                    "leader": st.sim.leader, "conditions": [{"type": "healthy", "status": "false" if down else "true"}],
                    "quorum-status": {"members": [{"name": ip, "status": "stopped" if ("quorum_down" in st.f or not up[k])
                                                   else "started", "conditions": [{"status": "true" if up[k] else "false"}]}
                                                  for k, ip in enumerate(st.sim.quorum)]}}))
            if res == "rollouts" and method == "POST":
                if self._deny(user, "cluster", "Rollout", "create"):
                    return
                v = ((body or {}).get("spec") or {}).get("version")
                if not v:
                    return self._err(400, "rollout needs spec.version")
                if "rollout_noop" not in st.f:
                    st.version = v
                for i in range(st.sim.n_sw):            # controllers and agents restart on the new build
                    st.sim.agent(i, "restart")
                st.record(user, "Create", "Rollout", (body.get("meta") or {}).get("name", "rollout"), path)
                return self._send(200, obj("Rollout", (body.get("meta") or {}).get("name", "rollout"), body.get("spec"),
                                           {"state": "complete"}))
            if res in ("config-snapshot", "config-restore"):
                return self._snapshot(method, p, body, user)
            kind = CLUSTER_KINDS.get(res)
            if not kind:
                return self._err(404, f"unknown resource {res}")
            if kind == "ComputeNode" and len(p) > 5 and (method == "DELETE" or len(p) == 7):
                if self._deny(user, "cluster", kind, "update"):
                    return
                return self._compute_op(method, p[5], p[6] if len(p) == 7 else "Delete", body, user, path)
            if kind == "VPod" and (method != "GET"):
                return self._vpod(method, p[5] if len(p) > 5 else None, p[6] if len(p) == 7 else None, body, user,
                                  path, action)
            if kind == "SwitchNode" and len(p) > 5 and (method == "DELETE" or len(p) == 7):
                self._last_body = body
                return self._switch_op(method, p[5], p[6] if len(p) == 7 else "Delete", user)
            return self._crud(method, "cluster", kind, p[5] if len(p) > 5 else None, body, user, path, action)
        # ---- auth group
        if path.startswith("/configs/auth/v1/"):
            if path == "/configs/auth/v1/authn-policy":
                if self._deny(user, "auth", "AuthenticationPolicy", "read" if method == "GET" else "update"):
                    return
                if method == "PUT":
                    def nums(x):
                        if isinstance(x, dict):
                            return [v for y in x.values() for v in nums(y)]
                        return [x] if isinstance(x, (int, float)) and not isinstance(x, bool) else []
                    if any(v < 1 for v in nums((body or {}).get("spec") or {})) and "authn_accepts_invalid" not in st.f:
                        return self._err(400, "invalid authentication policy: values must be >= 1")
                    st.authn["spec"] = (body or {}).get("spec") or st.authn["spec"]
                return self._send(200, st.authn)
            res = p[6] if len(p) > 6 else ""
            kind = {"users": "User", "roles": "Role", "role-bindings": "RoleBinding"}.get(res)
            if not kind:
                return self._err(404, "unknown auth resource")
            return self._auth(method, kind, p[7] if len(p) > 7 else None, body, user, path, action)
        # ---- monitoring
        if path.startswith("/configs/monitoring/v1/"):
            res = p[6] if p[4] == "tenant" else p[4]
            name = (p[7] if len(p) > 7 else None) if p[4] == "tenant" else (p[5] if len(p) > 5 else None)
            if res == "alerts":
                if self._deny(user, "monitoring", "Alert", "read"):
                    return
                return self._send(200, {"kind": "AlertList", "list-meta": {"total-count": len(st.alerts)},
                                        "items": list(reversed(st.alerts))})
            if res == "syslogexports":
                return self._crud(method, "monitoring", "SyslogExport", name, body, user, path, action)
            if res == "alertPolicies":
                return self._crud(method, "monitoring", "AlertPolicy", name, body, user, path, action)
            if res == "techsupport":
                return self._techsupport(method, name, body, user)
            return self._err(404, "unknown monitoring resource")
        if path == "/events/v1/events":
            if self._deny(user, "events", "Event", "read"):
                return
            if "events_unbounded" in st.f and len(st.sim.events) < 16050:
                old = "2026-01-01T00:00:00.000000Z"
                st.sim.events[:0] = [{"kind": "Event", "meta": {"name": f"old-{k}", "creation-time": old,
                                                                "mod-time": old}, "type": "old", "message": "old"}
                                     for k in range(16050)]
            # as the real AFM (g02-g17 17 Oct): every event, by name (a uuid) - sort-by / sort-order=desc / page-size
            # are not part of its API and are ignored, so a client must not take items[0] as the newest
            evs = sorted(st.sim.events, key=lambda e: e["meta"]["name"])
            return self._send(200, {"kind": "EventList", "items": evs})
        if path.startswith("/audit/v1/events"):
            if self._deny(user, "monitoring", "AuditEvent", "read"):
                return
            if len(p) == 5:
                e = next((a for a in st.audit if a["meta"]["uuid"] == p[4]), None)
                return self._send(200, e) if e else self._err(404, "audit event not found")
            return self._send(200, {"kind": "AuditEventList", "items": self._select(st.audit, qs)})
        if path == "/telemetry/v1/metrics" and method == "POST":
            return self._metrics(body)
        if path == "/sysruntime/v1/cluster/recoverykeys":
            if st.recovery_key_taken and "recovery_key_reusable" not in st.f:
                return self._err(412, "recovery key already downloaded")
            st.recovery_key_taken = True
            return self._send(200, {"recovery-key": secrets.token_hex(16)})
        if path.startswith("/objstore/v1/downloads/"):
            return self._send(200, b"\x1f\x8b fake techsupport bundle")
        return self._err(404, f"no route {path}")

    # ---------------------------------------------------------------- handlers
    def _login(self, body):
        st = self.st
        if body is ValueError or not isinstance(body, dict) or "username" not in body:
            return self._err(400, "invalid login request")
        u = st.users.get(body.get("username"))
        if u is None:
            return self._dir_login(body)
        if u["pw"] != body.get("password"):
            if u:
                s = u["obj"]["status"]
                s["failed-login-attempts"] = int(s.get("failed-login-attempts", 0)) + 1
            # the real controller's login error has no "T"/"object-ref" (seen on MR3, AICC-797)
            return self._send(401, {"result": {"Str": "Unauthorized"}, "message": ["Invalid username/password"],
                                    "code": 401})
        sid = secrets.token_hex(16)
        st.sessions[sid] = body["username"]
        st.record(body["username"], "Login", "User", body["username"], "/v1/login")
        flags = "; Path=/; Secure; HttpOnly" + ("" if "cookie_flags" in st.f else "; SameSite=Strict")
        o = copy.deepcopy(u["obj"])
        if body["username"] != "admin":
            o["status"]["roles"] = st.user_roles(body["username"])
        self._send(200, o, {"Set-Cookie": f"sid={sid}{flags}", "Grpc-Metadata-Csrf-Token": secrets.token_hex(8)})

    def _dir_login(self, body):
        """Login through an enabled LDAP / RADIUS authenticator in the authn-policy (fake directories)."""
        st = self.st
        auths = (st.authn.get("spec") or {}).get("authenticators") or {}
        for kind, users in st.DIRECTORY.items():
            if not (auths.get(kind) or {}).get("enabled") or body["username"] not in users:
                continue
            if kind == "ldap" and "ldap_ignored" in st.f:
                continue
            if users[body["username"]] != body.get("password") and not (kind == "radius" and "radius_accepts_any" in st.f):
                continue
            sid = secrets.token_hex(16)
            st.sessions[sid] = body["username"]
            st.record(body["username"], "Login", "User", body["username"], "/v1/login")
            o = obj("User", body["username"], {"type": kind}, {"authenticators": [kind], "roles": []})
            return self._send(200, o, {"Set-Cookie": f"sid={sid}; Path=/; Secure; HttpOnly; SameSite=Strict",
                                       "Grpc-Metadata-Csrf-Token": secrets.token_hex(8)})
        return self._send(401, {"result": {"Str": "Unauthorized"}, "message": ["Invalid username/password"], "code": 401})

    def _crud(self, method, group, kind, name, body, user, path, action):
        st = self.st
        coll = st.objs[kind]
        if self._deny(user, group, kind, action):
            return
        if method == "GET":
            if name is None:
                rvs = [int(o["meta"].get("resource-version", 0)) for o in coll.values()] + [0]
                return self._send(200, {"kind": f"{kind}List", "list-meta": {"total-count": len(coll),
                                                                            "resource-version": str(max(rvs))},
                                        "items": list(coll.values())})
            return self._send(200, coll[name]) if name in coll else self._err(404, "Object get failed: object not found")
        if method == "POST" and name is None:
            if not isinstance(body, dict) or not (body.get("meta") or {}).get("name"):
                return self._err(400, "meta.name required")
            n = body["meta"]["name"]
            if kind == "VPod" and n.endswith(".system") and "system_name_ok" not in st.f:
                return self._err(400, "vPod name must not end with .system")
            if n in coll and "dup_vpod_ok" not in st.f:
                return self._err(409, f"object {n} already exists", "Conflict")
            spec = dict(body.get("spec") or {})
            if kind == "FailoverPolicy":
                spec.setdefault("link-fail-threshold", 10)
            o = obj(kind, n, spec, {"vpod-state": "online"} if kind == "VPod" else {})
            o["meta"]["resource-version"] = str(next(st.rv))
            coll[n] = o
            st.record(user, "Create", kind, n, path)
            st.publish("Created", group, kind, o)
            return self._send(200, o)
        if name not in coll:
            return self._err(404, "Object get failed: object not found")
        if method == "PUT":
            o = coll[name]
            o["spec"] = (body or {}).get("spec") or o["spec"]
            o["meta"]["mod-time"] = now()
            o["meta"]["resource-version"] = str(next(st.rv))
            st.record(user, "Update", kind, name, path)
            st.publish("Updated", group, kind, o)
            return self._send(200, o)
        if method == "DELETE":
            users = [v["meta"]["name"] for v in st.objs["VPod"].values()
                     if v["spec"].get("failover-policy") == name]
            if kind == "FailoverPolicy" and users and "policy_delete_in_use" not in st.f:
                return self._err(412, f"failover policy {name} is in use by vPods {users}")
            o = coll.pop(name)
            st.record(user, "Delete", kind, name, path)
            st.publish("Deleted", group, kind, o)
            return self._send(200, o)
        return self._err(405, "method not allowed")

    def _switch_op(self, method, name, op, user):
        st, sim = self.st, self.st.sim
        if self._deny(user, "cluster", "SwitchNode", "update"):
            return
        sw = st.objs["SwitchNode"].get(name)
        if sw is None:
            return self._err(404, "Object get failed: object not found")
        i, ph = sim.sw_index(name), sw["status"]["admission-phase"]
        body = self._last_body if isinstance(getattr(self, "_last_body", None), dict) else {}
        if op == "ReactivateSwitchNode":
            if ph != "drained":
                return self._err(412, f"switch {name} is {ph}, not drained")
            sw["status"]["admission-phase"] = "admitted"
            return self._send(200, sw)
        if op == "Delete":
            if ph not in ("decommissioned", "decommission_failed"):
                return self._err(412, f"switch {name} is {ph}: decommission it first")
            st.objs["SwitchNode"].pop(name)
            sim.event("switchnode_deleted", f"switch {name} deleted")
            return self._send(200, sw)
        others = [j for j in sim.in_service() if j != i]
        if not others and "minirack_drain_ok" not in st.f:
            return self._send(412, {**status_env(412, f"cannot {op}: {name} is the last remaining in-service switch",
                                                 "Failed pre conditions"),
                                    "object-ref": {"kind": f"{op}Request"}})
        if op == "DrainSwitchNode":
            sw["status"]["admission-phase"] = "drained"
            if "cp_disrupted_by_fabric_change" in st.f:
                sim.stopped |= set(sim.sb_pid)
            sim.event("switchnode_drained", f"switch {name} drained")
        elif op == "DecommissionSwitchNode":
            force = bool(body.get("force-decommission"))
            if ph == "admitted" and "force_decom_admitted_rejected" in st.f:
                return self._err(412, f"switch {name} is admitted: drain it first")
            ok = force or sim.agent_up[i] or "decom_with_agent_down_ok" in st.f
            sw["status"]["admission-phase"] = "decommissioned" if ok else "decommission_failed"
            sim.event("switchnode_decommissioned", f"switch {name} {sw['status']['admission-phase']}")
        else:
            return self._err(404, f"unknown switch action {op}")
        return self._send(200, sw)

    def _snapshot(self, method, p, body, user):
        st = self.st
        if self._deny(user, "cluster", "ConfigurationSnapshot", "read" if method == "GET" else "update"):
            return
        keep = ("FailoverPolicy", "VPod", "AlertPolicy")
        if p[4] == "config-snapshot":
            if method == "DELETE":
                return self._send(200, {})
            if len(p) == 6 and p[5] == "save":
                st.snapshot = {"objs": {k: copy.deepcopy(st.objs[k]) for k in keep},
                               "users": copy.deepcopy(st.users), "roles": copy.deepcopy(st.roles),
                               "bindings": copy.deepcopy(st.bindings), "authn": copy.deepcopy(st.authn["spec"])}
                name = f"snapshot-{int(time.time())}.gz"
                return self._send(200, obj("ConfigurationSnapshot", "Default-Snapshot", {},
                                           {"last-snapshot": {"uri": f"/objstore/v1/downloads/snapshots/{name}"}}))
            return self._send(200, obj("ConfigurationSnapshot", (body or {}).get("meta", {}).get("name", "x"), {}))
        if p[4] == "config-restore" and method == "POST":
            if not st.snapshot:
                return self._err(412, "no snapshot saved")
            if "restore_noop" not in st.f:
                for k, v in st.snapshot["objs"].items():
                    st.objs[k].clear()
                    st.objs[k].update(copy.deepcopy(v))
                admin = st.users["admin"]
                st.users.clear()
                st.users.update(copy.deepcopy(st.snapshot["users"]))
                st.users["admin"] = admin
                st.roles.clear(); st.roles.update(copy.deepcopy(st.snapshot["roles"]))
                st.bindings.clear(); st.bindings.update(copy.deepcopy(st.snapshot["bindings"]))
                if "restore_drops_authn" not in st.f:
                    st.authn["spec"] = copy.deepcopy(st.snapshot["authn"])
            return self._send(200, obj("SnapshotRestore", "SnapshotRestore", (body or {}).get("spec") or {},
                                       {"status": "completed"}))
        return self._err(404, "unknown snapshot path")

    def _compute_op(self, method, name, op, body, user, path):
        st, sim = self.st, self.st.sim
        c = st.objs["ComputeNode"].get(name)
        if c is None:
            return self._err(404 if op != "Reconfigure" or "rc_412_missing" not in st.f else 412,
                             "Object get failed: object not found")
        ph, slot = c["status"]["admission-phase"], c["spec"]["slot-id"]
        in_vpod = sorted({g.get("vpod-membership") for g in c["status"]["scale-up-info"]["gpu-info"].values()} - {""})
        if op == "Reconfigure":
            if ph != "admitted" and "reconfig_decom_ok" not in st.f:
                return self._err(412, f"reconfigure compute tray is not allowed: compute node {name} is {ph} (decommissioned)")
            if (slot in sim.stopped or sim.slot_host(slot) is None) and "unhealthy_reconf_ok" not in st.f:
                return self._err(412, f"reconfigure compute tray is not allowed: compute node {name} does not have scaleup info")
            if time.monotonic() - sim.reconf_at.get(name, -99) < 1:
                return self._err(412, f"reconfigure of {name} already in progress")
            if "reconfigure_500" in st.f or ("admission_reconfigure_500" in st.f
                                             and time.monotonic() - sim.readmit_at.get(slot, -99) < 2):
                return self._err(500, "internal error during reconfigure")
            sim.reconf_at[name] = time.monotonic()
            if "reconfig_clears_drain" in st.f:
                c["spec"]["drained-ports"] = []
            for u in sim.gpus[slot]:
                sim.removed_at[u] = time.monotonic()
            if "no_reset_on_reconfigure" not in st.f:
                sim.journal.setdefault(slot, []).append(time.time())
            st.record(user, "Reconfigure", "ComputeNode", name, path)
            return self._send(200, c)
        if op == "DrainGPUPort" and "cp_disrupted_by_fabric_change" in st.f:
            sim.stopped |= set(sim.sb_pid)
        if op in ("DrainGPUPort", "ActivateGPUPort"):
            ports = [(x.get("gpu-id"), x.get("port-id")) for x in (body or {}).get("ports") or []]
            cur = [(x["gpu-id"], x["port-id"]) for x in c["spec"].get("drained-ports") or []]
            new = sorted(set(cur) | set(ports)) if op == "DrainGPUPort" else [x for x in cur if x not in ports]
            c["spec"]["drained-ports"] = [{"gpu-id": g, "port-id": pt} for g, pt in new]
            return self._send(200, c)
        if op == "Decommission":
            if in_vpod and "decom_in_vpod_ok" not in st.f:
                return self._err(412, f"GPUs are part of vPods ({','.join(in_vpod)})")
            force = bool((body or {}).get("force-decommission"))
            unhealthy = slot in sim.stopped or sim.slot_host(slot) is None
            c["status"]["admission-phase"] = "decommission_failed" if (unhealthy and not force
                                                                      and "decom_unhealthy_ok" not in st.f) else "decommissioned"
            sim.lifecycle_event(c["status"]["admission-phase"], slot)
            return self._send(200, c)
        if op == "Delete":
            if ph == "admitted":
                return self._err(412, f"compute node {name} is admitted: decommission it first")
            st.objs["ComputeNode"].pop(name)
            sim.lifecycle_event("deleted", slot)
            return self._send(200, c)
        return self._err(404, f"unknown compute action {op}")

    def _vpod(self, method, name, op, body, user, path, action):
        st, sim = self.st, self.st.sim
        if self._deny(user, "cluster", "VPod", action):
            return
        coll = st.objs["VPod"]
        if op in ("Reconcile", "ReconfigureDataFabric"):
            if name not in coll:
                return self._err(404, "Object get failed: object not found")
            if op == "Reconcile" and "reconcile_clears_failure" in st.f:
                for d in sim.links_down.values():
                    d["since"] = time.monotonic()
                sim.failed_evt.clear()
            if op == "ReconfigureDataFabric":
                sim.ack.setdefault(name, set()).update(sim.failed_links(st.objs, coll[name]))
                if "no_reconfigure_event" not in st.f:
                    sim.vpod_event("vpod_status_change", name, "status changed to reconfiguredatafabric")
            return self._send(200, coll[name])
        if method == "POST":
            if isinstance(body, dict):
                err = sim.validate_gpus((body.get("spec") or {}).get("GPUs") or [])
                if err:
                    return self._err(400, err)
            r = self._crud(method, "cluster", "VPod", None, body, user, path, action)
            if "cp_disrupted_by_fabric_change" in st.f:
                sim.stopped |= set(sim.sb_pid)
            if isinstance(body, dict) and (body.get("meta") or {}).get("name") in coll:
                sim.vpod_written(body["meta"]["name"])
                sim.vpod_event("vpod_status_change", body["meta"]["name"], "created")
            return r
        if name not in coll:
            return self._err(404, "Object get failed: object not found")
        if method == "PUT":
            before = list(coll[name]["spec"].get("GPUs") or [])
            after = list(((body or {}).get("spec") or {}).get("GPUs") or [])
            had = {(g.get("slot-id"), g.get("index")) for g in before}
            err = sim.validate_gpus([g for g in after if (g.get("slot-id"), g.get("index")) not in had])
            if err:
                return self._err(400, err)
            sim.gpus_removed(before, after)
            sim.stick_slot_ports({g.get("slot-id") for g in before} ^ {g.get("slot-id") for g in after})
            if "configdone_stuck" in st.f and len(after) > len(before):
                sim.configdone.add(name)
            if "transient_zero_planes" in st.f and (body.get("spec") or {}).get("failover-policy") != \
                    coll[name]["spec"].get("failover-policy"):
                sim.zero_reads[name] = 3
            if "accid_changes_on_move" in st.f:
                gone = {(g.get("slot-id"), g.get("index")) for g in before} - \
                    {(g.get("slot-id"), g.get("index")) for g in after}
                for c in st.objs["ComputeNode"].values():
                    for gi in c["status"]["scale-up-info"]["gpu-info"].values():
                        if (c["spec"]["slot-id"], gi["index"]) in gone:
                            gi["acc-id"] += 100
            if not any(sim.agent_up.values()) and "stale_macs" in st.f:
                sim.stale_macs += max(0, len(before) - len(after))
            r = self._crud(method, "cluster", "VPod", name, body, user, path, action)
            sim.vpod_written(name)
            sim.vpod_event("vpod_status_change", name, f"updated ({len(after)} GPUs)")
            return r
        if method == "DELETE":
            n = len(coll[name]["spec"].get("GPUs") or [])
            if n:
                return self._err(412, f"vPod {name} cannot be deleted, {n} GPUs still assigned")
            sim.configfailed.discard(name)
            sim.vpod_event("vpod_deleted", name, "deleted")
            return self._crud(method, "cluster", "VPod", name, body, user, path, action)
        return self._err(405, "method not allowed")

    def _auth(self, method, kind, name, body, user, path, action):
        st = self.st
        if self._deny(user, "auth", kind, action):
            return
        store = {"User": None, "Role": st.roles, "RoleBinding": st.bindings}[kind]
        if kind == "User":
            if method == "GET":
                if name is None:
                    return self._send(200, {"items": [u["obj"] for u in st.users.values()]})
                if name not in st.users:
                    return self._err(404, "user not found")
                o = copy.deepcopy(st.users[name]["obj"])
                if name != "admin":
                    o["status"]["roles"] = st.user_roles(name)
                return self._send(200, o)
            if method == "POST":
                n = body["meta"]["name"]
                spec = dict(body.get("spec") or {})
                pw = spec.pop("password", None)
                st.users[n] = {"pw": pw, "obj": obj("User", n, spec, {"failed-login-attempts": 0,
                                                                      "authenticators": ["local"]})}
                st.record(user, "Create", "User", n, path)
                return self._send(200, st.users[n]["obj"])
            if method == "DELETE":
                if name not in st.users:
                    return self._err(404, "user not found")
                st.users.pop(name)
                st.sessions = {k: v for k, v in st.sessions.items() if v != name}
                return self._send(200, {})
        if method == "GET":
            if name is None:
                return self._send(200, {"items": list(store.values())})
            return self._send(200, store[name]) if name in store else self._err(404, f"{kind} not found")
        if method == "POST":
            n = body["meta"]["name"]
            store[n] = obj(kind, n, body.get("spec"))
            return self._send(200, store[n])
        if method == "PUT":
            if name not in store:
                return self._err(404, f"{kind} not found")
            store[name]["spec"] = body.get("spec")
            return self._send(200, store[name])
        if method == "DELETE":
            return self._send(200, store.pop(name)) if name in store else self._err(404, f"{kind} not found")

    def _select(self, items, qs):
        sel = unquote((qs.get("field-selector") or [""])[0])
        out = items
        for term in filter(None, sel.split(",")):
            if ">" in term:
                k, v = term.split(">", 1)
                out = [e for e in out if _ts((e.get("meta") or {}).get(k.split(".")[-1], "")) > _ts(v)]
                continue
            k, v = term.split("=", 1)
            a, _, b = k.partition(".")
            out = [e for e in out if ((e.get(a) or {}).get(b) if b else e.get(a)) == v]
        return list(reversed(out))[: int((qs.get("page-size") or [1000])[0])]

    def _metrics(self, body):
        st = self.st
        res = []
        for q in body.get("queries") or []:
            f = (q.get("fields") or [""])[0]
            if q.get("kind") == "Cluster":
                vals = {"AdmittedComputeNodes": len(st.objs["ComputeNode"]), "AdmittedSwitchNodes": len(st.objs["SwitchNode"]),
                        "HealthyComputeNodes": len(st.objs["ComputeNode"]), "HealthySwitchNodes": len(st.objs["SwitchNode"])}
                if "metrics_missing" in st.f and f == "HealthySwitchNodes":
                    res.append({"statement_id": 0})
                    continue
                res.append({"statement_id": 0, "series": [{"name": "Cluster", "values": [[now(), vals.get(f, 0)]]}]})
            elif q.get("kind") == "SwitchPortStats" and f not in KNOWN_NO_SERIES:
                res.append({"statement_id": 0, "series": [{"tags": {"name": "Ethernet1"}, "values": [[now(), 1234]]}]})
            else:
                res.append({"statement_id": 0})
        self._send(200, {"results": res})

    def _techsupport(self, method, name, body, user):
        st = self.st
        if self._deny(user, "monitoring", "TechSupportRequest", "read" if method == "GET" else "create"):
            return
        if method == "POST":
            names = ((body.get("spec") or {}).get("node-selector") or {}).get("names") or []
            if not names:
                return self._err(412, "node-selector names required")
            n = body["meta"]["name"]
            sw = {x: {"status": "completed", "uri": f"/objstore/v1/downloads/tenant/default/techsupport/{n}-{x}.tar.gz"}
                  for x in names if x in st.objs["SwitchNode"]}
            ct = {x: {"status": "completed", "uri": f"/objstore/v1/downloads/tenant/default/techsupport/{n}-{x}.tar.gz"}
                  for x in names if x not in st.objs["SwitchNode"]}
            st.techsupport[n] = obj("TechSupportRequest", n, body.get("spec"),
                                    {"status": "completed", "switch-node-results": sw, "ctrlr-node-results": ct})
            return self._send(200, st.techsupport[n])
        return self._send(200, st.techsupport[name]) if name in st.techsupport else self._err(404, "not found")

    # ---------------------------------------------------------------- AggWatch WebSocket
    def _ws(self):
        st = self.st
        user = self._user()
        if user is None:
            return self._err(401, "Not authenticated")
        if st.sim.ws_refuse:
            return self._err(503, "watch service unavailable")
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host") and "cswsh" not in st.f:
            return self._err(403, "cross-origin websocket refused")
        acc = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.wfile.flush()
        sock = self.connection
        self.close_connection = True
        sock.settimeout(None if "ws_no_close" in st.f else 3.0)
        try:
            op, data = _read_frame(sock)
        except (socket.timeout, OSError, ConnectionError):
            return _close(sock)
        try:
            msg = json.loads(data)
            opts = msg["watch-options"]
        except (ValueError, KeyError, TypeError):
            _write_frame(sock, 1, _error_event("invalid AggWatchOptions: invalid character in watch options"))
            return _close(sock)
        kinds, rv_of = set(), {}
        for o in opts:
            g, k = o.get("group"), o.get("kind")
            known = {"cluster": set(CLUSTER_KINDS.values()), "monitoring": {"AlertPolicy"}}
            if k not in known.get(g, set()):
                _write_frame(sock, 1, _error_event(f"unknown kind {g}/{k}"))
                continue
            kinds.add((g, k))
            rv = ((o.get("options") or {}).get("meta") or {}).get("resource-version")
            if rv is not None and "ws_rv_ignored" not in st.f:
                rv_of[(g, k)] = int(rv)
        if not opts:
            _write_frame(sock, 1, _error_event("stream error: rpc error: code = Unknown desc = invalid watch options"))
            return _close(sock)
        w = {"kinds": kinds, "q": queue.Queue(), "close": False}
        with st.lock:
            initial = [{"type": "Created", "kind": k, "group": g, "object": o}
                       for g, k in sorted(kinds) if st.allowed(user, g, k, "read") or "ws_rbac_leak" in st.f
                       for o in st.objs[k].values()
                       if (g, k) not in rv_of or (rv_of[(g, k)] >= 0
                                                  and int(o["meta"].get("resource-version", 0)) > rv_of[(g, k)])]
            st.watchers.append(w)
            if kinds and "no_watch_audit" not in st.f:
                for stage in ("requestauthorization", "requestprocessing"):
                    st.audit.append({"kind": "AuditEvent", "meta": {"uuid": str(uuid.uuid4()), "creation-time": now()},
                                     "action": "Watch", "stage": stage, "outcome": "success", "user": {"name": user},
                                     "resource": {"kind": "AggWatch", "name": "watch"},
                                     "request-uri": "/configs/aggwatch/v1/watch",
                                     "watch-options": [f"{g}/{k}" for g, k in sorted(kinds)]})
        try:
            if initial:
                _write_frame(sock, 1, json.dumps({"events": initial}).encode())
            if kinds and "no_listing_control" not in st.f:   # real AFM: end-of-list marker
                _write_frame(sock, 1, json.dumps({"events": [{"type": "Control", "control": {
                    "code": 1, "message": "Listing for watch complete"}}]}).encode())
            sock.settimeout(0.2)
            while True:
                try:
                    op, _ = _read_frame(sock)
                    if op == 8:
                        break
                except socket.timeout:
                    pass
                if w["close"]:
                    break
                while not w["q"].empty():
                    ev = w["q"].get()
                    if user not in st.users:
                        return _close(sock)
                    ok = st.allowed(user, ev["group"], ev["kind"], "read") or "ws_rbac_leak" in st.f \
                        or "ws_rbac_stale" in st.f
                    if ok:
                        _write_frame(sock, 1, json.dumps({"events": [ev]}).encode())
        except (OSError, ConnectionError):
            pass
        finally:
            with st.lock:
                if w in st.watchers:
                    st.watchers.remove(w)
            _close(sock)


def _error_event(msg: str) -> bytes:
    """Error as real AFM sends it on the watch stream (seen on MR3, AICC-797)."""
    return json.dumps({"events": [{"type": "Error", "object": {"result": {"Str": "Error"}, "message": [msg],
                                                               "code": 500}}]}).encode()


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _read_frame(sock) -> tuple[int, bytes]:
    b1, b2 = _recv_exact(sock, 2)
    op, ln = b1 & 0x0F, b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if b2 & 0x80 else b"\0\0\0\0"
    data = bytes(c ^ mask[i % 4] for i, c in enumerate(_recv_exact(sock, ln)))
    return op, data


def _write_frame(sock, op: int, data: bytes) -> None:
    n = len(data)
    hdr = bytes([0x80 | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536
                                 else bytes([127]) + struct.pack(">Q", n))
    sock.sendall(hdr + data)


def _close(sock) -> None:
    try:
        _write_frame(sock, 8, struct.pack(">H", 1000))
    except OSError:
        pass


class FakeAfm:
    """HTTPS (self-signed, generated with openssl) so Secure cookies flow exactly as on the real AFM."""

    def __init__(self, faults: set[str] | None = None, topology: dict | None = None):
        self.state = State(set(faults or ()), topology)
        handler = type("H", (Handler,), {"st": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self._tmp = tempfile.TemporaryDirectory(prefix="fake-afm-")
        crt, key = Path(self._tmp.name) / "c.pem", Path(self._tmp.name) / "k.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1",
                        "-days", "1", "-keyout", str(key), "-out", str(crt)], check=True, capture_output=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self) -> "FakeAfm":
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()


if __name__ == "__main__":  # manual poking: python selftest/fake_afm.py
    srv = FakeAfm().start()
    print(srv.url, "admin /", ADMIN[1])
    while True:
        time.sleep(3600)
