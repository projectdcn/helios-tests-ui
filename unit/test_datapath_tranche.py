"""Lessons from the g02-g17 L1 run (14 Oct 2026): multi-rank abort, IP rendezvous, pre-flight tray exclusion,
environment-vs-product classification in the workbook writer."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from helios.datapath import RankAborted, addr, run_ranks
from helios.transport import CmdResult

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class Node:
    """A rank that either fails fast or blocks until pkill'd (as a TransferBench rank waiting for its peer)."""

    def __init__(self, name, fail=False):
        self.name, self.fail, self.killed = name, fail, threading.Event()
        self.host = SimpleNamespace(host="127.0.0.1", extra={})
        self.cmds = []

    def run(self, cmd, timeout=120, sudo=False, env=None):
        self.cmds.append(cmd)
        if cmd.startswith("pkill"):
            for n in NODES:
                n.killed.set()
            return CmdResult(self.name, cmd, 0, "", "")
        if self.fail:
            return CmdResult(self.name, cmd, 1, "", "recv: Connection reset by peer")
        if not self.killed.wait(timeout):
            return CmdResult(self.name, cmd, 124, "", "timeout")
        return CmdResult(self.name, cmd, 143, "", "Terminated")


NODES: list[Node] = []


def test_failed_rank_kills_its_partner():
    global NODES
    NODES = [Node("ct01"), Node("ct03", fail=True)]
    t0 = time.monotonic()
    out = run_ranks([(n, "TB_RANK=0 TransferBench a2a 1G") for n in NODES], timeout=30)
    assert time.monotonic() - t0 < 5, "partner rank was not killed early"
    (n0, r0, e0), (n1, r1, e1) = out
    assert isinstance(e0, RankAborted) and "ct03" in str(e0)
    assert e1 is None and r1.rc == 1
    assert all(c.startswith("HELIOS_RUN=") for c in NODES[0].cmds[:1])
    assert any("HELIOS_RU[N]=" in c for n in NODES for c in n.cmds), "pkill pattern must not match itself"


def test_rendezvous_address_is_an_ip():
    n = SimpleNamespace(host=SimpleNamespace(host="localhost", extra={}))
    assert addr(n) in ("127.0.0.1", "::1")
    n.host.extra["data_addr"] = "10.255.4.47"
    assert addr(n) == "10.255.4.47"


def test_environment_failures_are_not_product_fails():
    from update_testplan import verdicts

    def item(tc, outcome, msg):
        return {"tc_id": tc, "outcome": outcome, "message": msg, "test": tc}
    v = verdicts([
        item("A", "failed", "2 check(s) failed:\n  - ct08: TransferBench failed: rc=127 'bash: TransferBench: command not found'\n"
                            "  - ct03: RCCL: no ROCm-capable device is detected"),
        item("B", "failed", "1 check(s) failed:\n  - ct03: TransferBench failed: rc=-1 'malloc(): unaligned tcache chunk detected'"),
        item("C", "partial", "passed on the remaining trays; excluded: ct18: ping probe: black-holed"),
        item("D", "skipped", "Skipped: platform reports no PSU fans"),
    ])
    assert v["A"][0] == "ENV" and v["A"][1].startswith("Environment:")
    assert v["B"][0] == "FAIL"
    assert v["C"][0] == "PARTIAL"
    assert v["D"] == ("SKIP", "Skipped: platform reports no PSU fans")


def test_notes_keep_history():
    from update_testplan import merge_notes

    assert merge_notes("new", "12 Oct: passed", "L") == "new\n\nEarlier: 12 Oct: passed"
    assert merge_notes("new2", "[L] new\n\nEarlier: 12 Oct: passed", "L") == "new2\n\nEarlier: 12 Oct: passed"
    assert merge_notes("x", None, "L") == "x"


def test_scope_from_access():
    from access_needed import scope

    assert scope("Switch SSH") == "Switch"
    assert scope("Compute SSH + datapath traffic") == "Compute"
    assert scope("AFM API + switch + compute SSH") == "Switch + Compute"
    assert scope("AFM API (disruptive)") == "Switch (AFM API)"


def test_switch_consistency_majority():
    """TC-SWH-038: the odd switch out is reported against the majority value."""
    from helios.rack import Checks
    from tests.l1.test_switch_health import _majority

    c = Checks()
    _majority("SONiC build", {"st1": "A", "st2": "A", "st3": "B"}, c)
    assert len(c.failures) == 1 and c.failures[0].startswith("st3: SONiC build = 'B'")
    c2 = Checks()
    _majority("vlans", {"st1": ("Vlan100",), "st2": ("Vlan100",)}, c2)
    assert not c2.failures
