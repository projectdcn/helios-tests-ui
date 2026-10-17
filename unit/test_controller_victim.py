"""Controller tests never restart the controller that serves afm.url (g02-g17 17 Oct: st1 = 10.255.4.71 = afm.url was
picked as the 'non-leader', the API went away and the next tests errored)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "afm")); sys.path.insert(0, str(ROOT))
import test_afm_controller as tc  # noqa: E402


def _ops(quorum, leader, url):
    return SimpleNamespace(quorum_ips=lambda: quorum, leader_ip=lambda: leader, afm=SimpleNamespace(base=url))


def test_non_leader_is_not_the_api_controller():
    assert tc._non_leader(_ops(["10.255.4.71", "10.255.4.72", "10.255.4.73"], "10.255.4.72", "https://10.255.4.71")) == "10.255.4.73"
    assert tc._non_leader(_ops(["10.255.4.71", "10.255.4.72", "10.255.4.73"], "10.255.4.71", "https://10.255.4.71")) == "10.255.4.72"


def test_skip_when_only_the_api_controller_is_a_non_leader():
    with pytest.raises(pytest.skip.Exception, match="other than the API address 10.255.4.71"):
        tc._non_leader(_ops(["10.255.4.71", "10.255.4.72"], "10.255.4.72", "https://10.255.4.71"))


def test_all_started_rides_out_an_unreachable_api():
    def boom():
        raise ConnectionError("refused")
    assert tc._all_started(SimpleNamespace(afm=SimpleNamespace(cluster=boom))) is False


class _Resp:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


def _writer(answers):
    """ops whose POST answers from the list (an Exception instance is raised); time_scale tiny."""
    it = iter(answers)

    def raw(method, path, body=None):
        a = next(it)
        if isinstance(a, Exception):
            raise a
        return a
    return SimpleNamespace(afm=SimpleNamespace(raw=raw), th=lambda k, d: 0.001 if k == "time_scale" else d)


def test_write_after_started_measures_the_outage():
    props = {}
    out = tc._write_after_started(_writer([_Resp(503, "dial tcp 10.255.4.72:9003: connect: connection refused"),
                                           ConnectionError("reset"), _Resp(200)]), "ap", lambda k, v: props.__setitem__(k, v))
    assert out is not None and out >= 0
    assert props["write_outage_s"] == out
    assert "9003" in props["write_errors"][0] and "ConnectionError" in props["write_errors"][1]


def test_write_after_started_first_try():
    props = {}
    assert tc._write_after_started(_writer([_Resp(200)]), "ap", lambda k, v: props.__setitem__(k, v)) is not None
    assert "write_errors" not in props


def test_write_after_started_gives_up():
    props = {}
    assert tc._write_after_started(_writer([_Resp(503)] * 100000), "ap", lambda k, v: props.__setitem__(k, v)) is None
    assert props["write_errors"][0].startswith("HTTP 503") and props["write_errors"][-1].startswith("...")
