"""AfmOps event helpers do not trust the controller's list order (g02-g17 17 Oct: AFM ignores sort-by /
sort-order=desc / page-size and lists events by name, so items[0] was a random old event and TC-E2E-055 /
TC-E2E-132 counted a 40 min old link_failed as new)."""
from __future__ import annotations

from helios.afm_ops import AfmOps


class _Afm:
    def __init__(self, events):
        self.evs, self.paths = events, []

    def items(self, path):
        self.paths.append(path)
        return list(self.evs)


def _ev(name, t, etype="link_down", msg="PortName Ethernet354 is DOWN"):
    return {"kind": "Event", "meta": {"name": name, "creation-time": t}, "type": etype, "message": msg}


# listed by name like the real controller: the newest (16:11:40) is in the middle
EVENTS = [_ev("004da2f2", "2026-10-17T15:31:52.39168224Z"),
          _ev("0143d160", "2026-10-17T15:34:32.001155229Z", "link_failed", "PortName Ethernet354 is FAILED"),
          _ev("5a000000", "2026-10-17T16:11:40.4Z", "link_down", "PortName Ethernet35 is DOWN"),
          _ev("9f000000", "2026-10-17T16:11:39.9Z")]


def test_controller_now_is_the_newest_event_not_the_first_listed():
    ops = AfmOps(_Afm(EVENTS))
    assert ops.controller_now() == "2026-10-17T16:11:40.4Z"
    assert all("sort-order=by-creation-time-reverse" in p for p in ops.afm.paths)


def test_event_time_pads_the_fraction():
    t = AfmOps.event_time
    assert t(_ev("a", "2026-10-17T16:11:40.4Z")) > t(_ev("b", "2026-10-17T16:11:40.39168224Z"))
    assert t(_ev("a", "2026-10-17T16:11:40Z")) < t(_ev("b", "2026-10-17T16:11:40.000000001Z"))
    assert t({"meta": {}}) == ""


def test_events_since_newest_first_and_page():
    ops = AfmOps(_Afm(EVENTS))
    new = ops.events(None, "2026-10-17T15:34:32.001155229Z")
    assert [e["meta"]["name"] for e in new] == ["5a000000", "9f000000"]
    assert ops.events("link_failed", "2026-10-17T16:00:00Z") == []          # the stale link_failed is not new
    assert [e["meta"]["name"] for e in ops.events(page=1)] == ["5a000000"]


def test_mentions_whole_names_only():
    e = _ev("x", "t", msg="PortName Ethernet354 is DOWN on pod-1-17-ComputeTray")
    assert AfmOps.mentions(e, "Ethernet354")
    assert not AfmOps.mentions(e, "Ethernet35")
    assert AfmOps.mentions(e, "pod-1-17-ComputeTray")
    assert not AfmOps.mentions(e, "pod-1-1")
