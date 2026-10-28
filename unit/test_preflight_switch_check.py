from types import SimpleNamespace

from helios import preflight


class _BrokenSwitch:
    name = "st1"

    def namespaces(self):
        raise RuntimeError("host key changed")


class _Inv:
    def __init__(self, th):
        self._th = th

    def th(self, key, default=None):
        return self._th.get(key, default)

    def cmd(self, key, default=None):
        return default


def _assess(th):
    rack = SimpleNamespace(switches={"st1": _BrokenSwitch()})
    return preflight.assess(rack, [], _Inv(th), min_computes=0)


def test_unreadable_switch_blocks_by_default():
    assert any("st1: cannot read counters" in p for p in _assess({}).problems)


def test_switch_check_false_skips_switch_counters():
    assert _assess({"datapath.preflight.switch_check": False}).problems == []
