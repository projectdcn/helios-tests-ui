"""Compute port re-enable: afmctl sets "enable" (shows "enabled"); a refused value falls back to the one afmctl lists;
a port that cannot be re-enabled fails the test loudly instead of leaving the rack degraded (MR3, 16 Oct)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests" / "afm"))
from helios.afm_ops import AfmOps  # noqa: E402


class Tray:
    def __init__(self, accept):
        self.accept, self.cmds, self.name = accept, [], "c2"

    def run(self, cmd, sudo=False, timeout=None):
        self.cmds.append(cmd)
        val = cmd.rsplit(" ", 1)[1]
        ok = val in self.accept
        return SimpleNamespace(ok=ok, rc=0 if ok else 255, stdout="",
                               stderr="" if ok else f"Invalid admin state : {val} (valid: {', '.join(sorted(self.accept | {'power-off'}))})",
                               tail=lambda n=600: "Invalid admin state")


def _ops():
    o = AfmOps.__new__(AfmOps)
    o.th = lambda k, d=None: d
    return o


def test_enable_is_used():
    t = Tray({"idle", "enable"})
    assert _ops().compute_port_admin(t, "0001:01:00.1", 0, up=True).ok
    assert t.cmds == ["env AFMCTL_FORCE_UPDATE=1 afmctl update port -b 0001:01:00.1 -s 0 -p 0 --admin-state enable"]


def test_falls_back_to_the_value_afmctl_lists():
    t = Tray({"idle", "enabled"})                 # an afmctl that wants "enabled"
    assert _ops().compute_port_admin(t, "0001:01:00.1", 3, up=True).ok
    assert [c.rsplit(" ", 1)[1] for c in t.cmds] == ["enable", "enabled"] and " -s 1 -p 3 " in t.cmds[0]


def test_port_left_down_fails_loudly():
    from test_afm_compute import ComputePorts
    t = Tray({"idle"})                            # re-enable refused, nothing valid to fall back to
    cp = ComputePorts(_ops(), t)
    assert cp.off("0001:01:00.1", 0).ok
    with pytest.raises(pytest.fail.Exception, match="PORT LEFT DOWN"):
        cp.restore()
