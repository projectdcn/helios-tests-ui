"""What a case saw, as a timeline, for training data (tools/dataset_export.py): the target, the state before, the
fault it injected, every change of the watched state, the repair (or its absence), AFM events, counter deltas.

Stored with the case's results (tc_results.csv properties, key "evidence"); it never decides a verdict. A watched
value is recorded when it changes, not on every poll, so a 300 s wait stays a few lines.

    def test_x(evidence):                         # the ``evidence`` fixture (helios/pytest_plugin.py)
    ev = evidence
    ev.fact(target="st6 asic0 Vlan100 aa:.. -> Ethernet12")
    ev.event("inject", action="fdb_delete")
    ok = ops.wait(lambda: ev.watch("fdb_present", _present(...)), 300, 10)
"""
from __future__ import annotations

import time

MAX_TIMELINE = 200


class Evidence:
    def __init__(self, user_properties: list | None = None):
        self.t0 = time.monotonic()
        self.data: dict = {"facts": {}, "timeline": []}
        self._last: dict = {}
        if user_properties is not None:
            user_properties.append(("evidence", self.data))      # the same dict: later additions are included

    def _t(self) -> float:
        return round(time.monotonic() - self.t0, 1)

    def fact(self, **kw) -> None:
        """Facts of the case (target, configuration found, thresholds used)."""
        self.data["facts"].update(kw)

    def event(self, kind: str, **kw) -> None:
        """One step on the timeline (inject, repair, cleanup, error ...)."""
        tl = self.data["timeline"]
        if len(tl) < MAX_TIMELINE:
            tl.append({"t": self._t(), "kind": kind, **kw})
        else:
            self.data["truncated"] = True

    def watch(self, what: str, value):
        """Record ``value`` of a polled state when it changes (first value included); returns it, so it can wrap the
        predicate of a wait: ``ops.wait(lambda: ev.watch("fdb_present", present()), ...)``."""
        if what not in self._last or self._last[what] != value:
            self._last[what] = value
            self.event("state", what=what, value=value)
        return value


def afm_events(evs, limit: int = 20) -> list[dict]:
    """AFM events, compact: [{type, severity, message}] (newest last, at most ``limit``)."""
    out = []
    for e in evs or []:
        spec = e.get("spec") or {}
        out.append({"type": e.get("type") or spec.get("event-type") or spec.get("type") or "?",
                    "severity": str(e.get("severity") or spec.get("severity") or ""),
                    "message": str(e.get("message") or spec.get("message") or "")[:160]})
    return out[-limit:]
