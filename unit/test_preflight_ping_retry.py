"""preflight._ping_probe: a port that lost one frame is pinged again; only a repeated loss keeps the tray out."""
from __future__ import annotations

from types import SimpleNamespace

from helios import preflight
from helios.afmctl import Device, PingResult, Port


def _fake_afmctl(results: list[PingResult], calls: list):
    class FakeAfmctl:
        def __init__(self, c, timeout=60):
            pass

        def devices(self):
            return [Device("0001:01:00.1", "0", "ACTIVE"), Device("0002:01:00.1", "1", "ACTIVE")]

        def ports(self, bdf):
            return [Port(i, "enabled", "LINK_UP") for i in range(36)]

        def ping(self, bdf, acc, ports, timeout=200):
            calls.append((bdf, list(ports)))
            res = results.pop(0) if results else PingResult(36, 0, 36, 0, 0)
            return res, SimpleNamespace(stdout="", stderr="", host="ct01", cmd="afmctl test ping", rc=0, duration=1.0)
    return FakeAfmctl


def _lost(port: str) -> PingResult:
    return PingResult(35, 1, 36, 1, 0, failed_ports=[port], port_checks={port: ["req", "rsp"]})


def _probe(monkeypatch, results, retries=1):
    calls, ev = [], []
    monkeypatch.setattr(preflight, "Afmctl", _fake_afmctl(results, calls))
    out = preflight._ping_probe([SimpleNamespace(name="ct01")], 200, ev, retries=retries)
    return out, calls, ev


def test_single_drop_that_does_not_repeat_keeps_the_tray(monkeypatch):
    out, calls, ev = _probe(monkeypatch, [_lost("29"), PingResult(1, 0, 1, 0, 0)])
    assert out == []
    assert calls[1] == ("0001:01:00.1", [29])            # only the lossy port is pinged again
    assert "ok" in ev[-1]["pair"]


def test_repeated_loss_excludes_the_tray(monkeypatch):
    out, _, _ = _probe(monkeypatch, [_lost("29"), PingResult(0, 1, 1, 1, 0, failed_ports=["29"])])
    assert len(out) == 1 and "lost frames" in out[0][1]


def test_retries_zero_keeps_the_strict_probe(monkeypatch):
    out, calls, _ = _probe(monkeypatch, [_lost("29")], retries=0)
    assert len(out) == 1 and len(calls) == 2              # one ping per GPU, no retry
