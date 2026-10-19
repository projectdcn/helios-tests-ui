"""Read-only maintenance helper (not a test-plan case): summarise AFM's audit log for a time window, to find who changed
what on the rack. Runs only when named:

    pytest tests/afm/test_afm_audit_dump.py --inventory <inv> --tc MAINT-AUDIT

Window: thresholds.afm.audit_since (ISO time, e.g. 2026-10-18T15:00:00Z; default the last 12 h of the controller's
clock). Records per (user, action, resource kind) counts, write actions per hour, the records that touch switch ports
(SwitchNode objects or a body mentioning admin state / EthernetN), and their first and last times. Nothing is changed.
g02-g17 19 Oct: 1,300+ fabric port admin down / up cycles on all six switches from 18 Oct 15:18 UTC, not from the CLI.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

WRITES = {"Create", "Update", "Delete", "Patch", "create", "update", "delete", "patch"}
PORTISH = re.compile(r"admin[-_ ]?(state|status)|\bEthernet\d+\b|\"port", re.I)


def _ctime(e: dict) -> str:
    return (e.get("meta") or {}).get("creation-time") or ""


@pytest.mark.tc("MAINT-AUDIT")
def test_audit_summary(afm, inventory, request, record_property):
    if "MAINT-AUDIT" not in str(request.config.getoption("--tc") or ""):
        pytest.skip("maintenance: runs only when asked for by ID (--tc MAINT-AUDIT)")
    newest = sorted(afm.items("/audit/v1/events?sort-by=meta.creation-time&sort-order=desc"), key=_ctime, reverse=True)
    now = _ctime(newest[0]) if newest else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    since = inventory.th("afm.audit_since") or (
        datetime.strptime(now[:19], "%Y-%m-%dT%H:%M:%S") - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    evs = sorted((e for e in afm.items(f"/audit/v1/events?field-selector=meta.creation-time%3E{since}")
                  if _ctime(e) > since), key=_ctime)
    by = Counter()
    hours = Counter()
    users = Counter()
    port = []
    for e in evs:
        u = (e.get("user") or {}).get("name") or "?"
        act = e.get("action") or "?"
        res = e.get("resource") or {}
        kind = res.get("kind") or "?"
        by[(u, act, kind)] += 1
        users[u] += 1
        if act in WRITES:
            hours[_ctime(e)[:13]] += 1
        body = json.dumps(e)
        if kind == "SwitchNode" or (act in WRITES and PORTISH.search(body)):
            port.append({"t": _ctime(e), "user": u, "action": act, "kind": kind, "name": res.get("name"),
                         "client": e.get("client-ip") or (e.get("request") or {}).get("remote-addr") or e.get("source"),
                         "uri": (e.get("request") or {}).get("uri") or e.get("uri"), "body": body[:600]})
    record_property("window", {"since": since, "until": now, "records": len(evs)})
    record_property("users", dict(users.most_common()))
    record_property("by_user_action_kind", {f"{u} {a} {k}": n for (u, a, k), n in by.most_common(40)})
    record_property("writes_per_hour", dict(sorted(hours.items())))
    record_property("port_related", {"count": len(port), "first": port[0]["t"] if port else None,
                                     "last": port[-1]["t"] if port else None,
                                     "users": dict(Counter(p["user"] for p in port)),
                                     "actions": dict(Counter(f"{p['action']} {p['kind']}" for p in port))})
    record_property("port_related_samples", port[:5] + port[-5:])
    record_property("raw_samples", [json.dumps(e)[:1500] for e in (evs[:2] + evs[-2:])])
    print(f"audit {since} .. {now}: {len(evs)} records; users {dict(users.most_common())}; "
          f"port-related {len(port)}")
