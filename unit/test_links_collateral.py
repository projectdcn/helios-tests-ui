"""tests/afm/afm_helpers.Links: re-flap only when thresholds.afm.reflap_on_restore allows it, and neighbours of a
shut port that stay down after the restore are reported (g02-g17 17-18 Oct: st6 Ethernet257 took 261 with it)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "afm"))
from afm_helpers import Links  # noqa: E402


class Checks:
    def __init__(self):
        self.failed = []

    def check(self, ok, msg):
        if not ok:
            self.failed.append(msg)


class Switch:
    def __init__(self, coupled=None, sticky=()):
        self.name, self.oper = "st6", {"Ethernet257": "up", "Ethernet261": "up", "Ethernet265": "up"}
        self.coupled, self.sticky = coupled or {}, set(sticky)

    def interfaces_status(self, ns):
        return [{"Interface": p, "Oper": o} for p, o in self.oper.items()]


class Ops:
    def __init__(self, sw, th=None):
        self.sw, self._th, self.calls = sw, th or {}, []

    def th(self, k, d=None):
        return self._th.get(k, d)

    def port_admin(self, sw, ns, port, up):
        self.calls.append((port, up))
        if up and port in sw.sticky:
            return SimpleNamespace(ok=True)
        sw.oper[port] = "up" if up else "down"
        if not up and port in sw.coupled:          # the coupled neighbour drops and does not come back
            sw.oper[sw.coupled[port]] = "down"
        return SimpleNamespace(ok=True)

    def wait_oper(self, sw, ns, port, state, timeout):
        return sw.oper[port] == state

    def wait(self, fn, timeout, interval=10, what=""):
        return fn()


def test_clean_flap_reports_nothing():
    sw = Switch()
    lk, c = Links(Ops(sw)), Checks()
    lk.shut(sw, "asic1", "Ethernet257")
    lk.restore(sw, "asic1", "Ethernet257", c)
    assert c.failed == [] and lk.collateral == []


def test_coupled_neighbour_left_down_is_reported():
    sw = Switch(coupled={"Ethernet257": "Ethernet261"})
    lk, c = Links(Ops(sw)), Checks()
    lk.shut(sw, "asic1", "Ethernet257")
    lk.restore(sw, "asic1", "Ethernet257", c)
    assert lk.collateral == [("st6", "asic1", ["Ethernet261"])]
    assert len(c.failed) == 1 and "Ethernet261" in c.failed[0] and "avoid_ports" in c.failed[0]


def test_no_reflap_when_disabled():
    sw = Switch(sticky={"Ethernet257"})
    ops = Ops(sw, {"reflap_on_restore": False})
    lk, c = Links(ops), Checks()
    lk.shut(sw, "asic1", "Ethernet257")
    lk.restore(sw, "asic1", "Ethernet257", c)
    assert ops.calls == [("Ethernet257", False), ("Ethernet257", True)]       # no second down / up
    assert len(c.failed) == 1 and "not re-flapped" in c.failed[0]             # the port itself, not as collateral
    assert lk.collateral == []


def test_reflap_by_default():
    sw = Switch(sticky={"Ethernet257"})
    ops = Ops(sw)
    lk = Links(ops)
    lk.shut(sw, "asic1", "Ethernet257")
    lk.restore(sw, "asic1", "Ethernet257", Checks())
    assert ops.calls.count(("Ethernet257", False)) == 2 and ops.calls.count(("Ethernet257", True)) == 2


def test_neighbour_check_waits_for_the_last_shut_port_of_the_asic():
    sw = Switch()
    lk, c = Links(Ops(sw)), Checks()
    lk.shut(sw, "asic1", "Ethernet257")
    lk.shut(sw, "asic1", "Ethernet261")
    lk.restore(sw, "asic1", "Ethernet257", c)       # 261 is still ours and shut: not collateral
    assert c.failed == [] and ("st6", "asic1") in lk.before
    lk.restore(sw, "asic1", "Ethernet261", c)
    assert c.failed == [] and lk.before == {}


def test_pick_prefers_a_plane_that_is_still_available(monkeypatch):
    """MR3 19 Oct: GPU Port33's plane was already missing; picking a Port33 link meant the planes could not drop."""
    import afm_helpers

    class Sw:
        name = "st1"

        @staticmethod
        def namespaces():
            return ["asic0"]

    class Ops:
        @staticmethod
        def link_candidates(sw, slots, nss):
            return [("asic0", "Ethernet1", (0, 3, 3, 33)), ("asic0", "Ethernet5", (0, 3, 3, 34))]

        @staticmethod
        def active_computes():
            return [{}]

        @staticmethod
        def gpu_info(c):
            return {"u": {"ports": [{"id": 33, "oper-state": "PortFailed"}, {"id": 34, "oper-state": "PortReady"},
                                    {"id": 35, "oper-state": "PortDisabled"}]}}

    assert afm_helpers._pick(Ops, Sw)[1] == "Ethernet5"
    Ops.gpu_info = staticmethod(lambda c: {"u": {"ports": [{"id": 33, "oper-state": "PortFailed"},
                                                           {"id": 34, "oper-state": "PortDown"}]}})
    assert afm_helpers._pick(Ops, Sw)[1] == "Ethernet1"      # all degraded: first candidate, with a warning
