"""Live progress at INFO (pytest's live log, the UI's live log): a line when a test starts, a heartbeat while it runs,
'waiting: <what> (elapsed / timeout)' while a framework wait polls, and one line per rack change. The full command
trace stays at DEBUG in each test's command log."""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("helios.progress")
WAIT_EVERY_S = 30.0         # a waiting line at most this often per wait
HEARTBEAT_S = 60.0          # a 'still running' line this often per test


class WaitReporter:
    """Call tick() inside a polling loop; logs 'waiting: what (30/120 s)' every WAIT_EVERY_S."""

    def __init__(self, what: str, timeout: float):
        self.what, self.timeout = what or "condition", timeout
        self.t0 = self.last = time.monotonic()

    def tick(self) -> None:
        now = time.monotonic()
        if now - self.last >= WAIT_EVERY_S:
            self.last = now
            log.info("waiting: %s (%.0f/%.0f s)", self.what, now - self.t0, self.timeout)


def action(msg: str, *args) -> None:
    """A change made on the rack (port down / up, agent or controller restart ...)."""
    log.info(msg, *args)


class Heartbeat:
    """'still running <name> (2m10s)' every HEARTBEAT_S from a daemon thread, until stopped."""

    def __init__(self, name: str, every: float = HEARTBEAT_S):
        self.name, self.every = name, every
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="helios-heartbeat", daemon=True)

    def _run(self):
        t0 = time.monotonic()
        while not self._stop.wait(self.every):
            s = int(time.monotonic() - t0)
            log.info("still running %s (%dm%02ds)", self.name, s // 60, s % 60)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=2)
