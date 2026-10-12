"""Rack: the set of switch/compute drivers built from an inventory."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

import pytest

from .afm import AfmClient
from .compute import ComputeTray
from .inventory import Host, Inventory
from .sonic import DEFAULT_FABRIC_PORT_RE, SonicSwitch
from .transport import make_transport

T = TypeVar("T")
R = TypeVar("R")


class Rack:
    def __init__(self, inv: Inventory):
        self.inv = inv
        fabric_re = inv.th("switch.fabric_port_re") or DEFAULT_FABRIC_PORT_RE
        excl = inv.th("switch.exclude_ports") or []
        self.switches = {
            h.name: SonicSwitch(h, make_transport(h, inv.transport), fabric_port_re=fabric_re, exclude_ports=excl)
            for h in inv.switches
        }
        self.computes = {h.name: ComputeTray(h, make_transport(h, inv.transport)) for h in inv.computes}
        # Commands that run on the test runner itself (BMC/PDU power control). Replayed in replay mode.
        runner = Host(name="runner", host="localhost", role="runner")
        local_cfg = inv.transport if inv.transport.get("kind") == "replay" else {"kind": "local"}
        self.runner = make_transport(runner, local_cfg)
        self._afm: AfmClient | None = None

    @property
    def afm(self) -> AfmClient | None:
        if self._afm is None and self.inv.afm.get("url"):
            self._afm = AfmClient(self.inv.afm)
        return self._afm

    def close(self) -> None:
        for node in [*self.switches.values(), *self.computes.values()]:
            node.close()


def fan_out(fn: Callable[[T], R], items: Iterable[T], workers: int | None = None
            ) -> list[tuple[T, R | None, BaseException | None]]:
    """Run ``fn`` over items in parallel; returns (item, result, exception) in input order."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=workers or min(32, len(items))) as ex:
        futs = [ex.submit(fn, it) for it in items]
        out = []
        for it, f in zip(items, futs):
            try:
                out.append((it, f.result(), None))
            except Exception as e:  # noqa: BLE001 — reported per item by the caller
                out.append((it, None, e))
        return out


class Checks:
    """Soft assertions: collect every failure across nodes, then fail once with all of them."""

    def __init__(self):
        self.failures: list[str] = []

    def check(self, cond, msg: str) -> bool:
        if not cond:
            self.failures.append(msg)
        return bool(cond)

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def assert_all(self, limit: int = 200) -> None:
        if self.failures:
            shown = self.failures[:limit]
            more = len(self.failures) - len(shown)
            text = f"{len(self.failures)} check(s) failed:\n" + "\n".join(f"  - {f}" for f in shown)
            if more:
                text += f"\n  ... and {more} more"
            pytest.fail(text, pytrace=False)
