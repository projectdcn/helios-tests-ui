"""Per-test item log over its limit keeps the start and the end (g02-g17 17 Oct: logs were cut in the pre-checks,
before the test's own commands)."""
from __future__ import annotations

import logging

from helios import pytest_plugin as pp


def _rec(msg):
    return logging.LogRecord("helios.cmd", logging.DEBUG, __file__, 1, msg, None, None)


def test_item_log_keeps_head_and_tail(monkeypatch):
    monkeypatch.setattr(pp, "ITEM_LOG_LIMIT", 2000)
    h = pp._ItemLog()
    for k in range(200):
        h.emit(_rec(f"line {k:03d} " + "x" * 40))
    text = h.text()
    assert "line 000" in text and "line 199" in text      # the first and the last command survive
    assert "line 100" not in text
    assert "chars dropped here" in text
    assert len(text) < 2000 + 300


def test_item_log_small_is_complete():
    h = pp._ItemLog()
    for k in range(5):
        h.emit(_rec(f"line {k}"))
    assert h.text().count("line ") == 5 and "dropped" not in h.text()
