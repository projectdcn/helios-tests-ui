"""Polling helpers for state that converges (containers, links, reboots)."""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def wait_until(fn: Callable[[], T], timeout: float, interval: float = 5.0) -> tuple[bool, T | None]:
    """Call ``fn`` until it returns truthy or ``timeout`` elapses. Exceptions count as "not yet".
    Returns (succeeded, last value)."""
    deadline = time.monotonic() + timeout
    last: T | None = None
    while True:
        try:
            last = fn()
            if last:
                return True, last
        except Exception:  # noqa: BLE001 — node may be mid-reboot / mid-restart
            last = None
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(interval)


def wait_for_ssh(node, timeout: float, interval: float = 10.0, down_first: float = 0.0) -> bool:
    """After a reboot/power cycle: optionally wait ``down_first`` s for the node to go away,
    then poll until a fresh SSH session answers."""
    if down_first:
        time.sleep(down_first)

    def probe():
        node.t.reset()
        return node.run("true", timeout=15).ok

    ok, _ = wait_until(probe, timeout, interval)
    return ok
