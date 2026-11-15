"""SonicSwitch helpers: IFCP capture direction by the port's own MAC (the compute answers in the switch's 802.1Q 0x88b6
format since ~Nov 2026, ethertype fallback) and critical_down for radv without VLAN interfaces."""
from __future__ import annotations

from helios.sonic import SonicSwitch
from helios.transport import CmdResult

NEW = """12:34:56:78:01:00
1794770359.324731 12:34:56:78:01:00 > 74:27:2c:04:76:15, ethertype 802.1Q (0x8100), length 25: vlan 0, p 7, ethertype Unknown (0x88b6), 
\t0x0000:  0120 0003 0101 00
1794770359.325311 74:27:2c:04:76:15 > 12:34:56:78:01:00, ethertype 802.1Q (0x8100), length 60: vlan 0, p 7, ethertype Unknown (0x88b6), 
\t0x0000:  0120 0003 0101 0000
"""
OLD = """1793044805.930709 12:34:56:78:01:00 > 74:27:2c:09:72:a9, ethertype 802.1Q (0x8100), length 25: vlan 0, p 7, ethertype Unknown (0x88b6), 
1793044806.430709 74:27:2c:09:72:a9 > 12:34:56:78:01:00, ethertype Unknown (0xeb87), length 56: 
"""


class _T:
    def __init__(self, out: str):
        self.out, self.cmds = out, []

    def run(self, cmd, **kw):
        self.cmds.append(cmd)
        return CmdResult("st1", cmd, 124, self.out, "")


def _sw(out: str) -> SonicSwitch:
    sw = SonicSwitch.__new__(SonicSwitch)
    sw.t = _T(out)
    return sw


def test_compute_reply_in_switch_format_is_ingress():
    f = _sw(NEW).ifcp_capture("Ethernet257", "asic1", 12)
    assert [(x["dir"], x["src"], x["ethertype"]) for x in f] == [
        ("tx", "12:34:56:78:01:00", "0x88b6"), ("rx", "74:27:2c:04:76:15", "0x88b6")]


def test_without_port_mac_direction_falls_back_to_ethertype():
    assert [x["dir"] for x in _sw(OLD).ifcp_capture("Ethernet1", "asic0", 12)] == ["tx", "rx"]


class _Cmds:
    """Answers by command substring."""
    def __init__(self, answers: dict[str, str]):
        self.answers = answers

    def run(self, cmd, **kw):
        out = next((v for k, v in self.answers.items() if k in cmd), "")
        return CmdResult("st1", cmd, 0, out, "")


def _radv(vlan_keys: str) -> SonicSwitch:
    sw = SonicSwitch.__new__(SonicSwitch)
    sw.t = _Cmds({"critical_processes": "program:radvd\n",
                  "supervisorctl status": "rsyslogd   RUNNING   pid 22, uptime 10:19:52\nstart   EXITED   Nov 15 09:09 AM\n",
                  "ps -eo args": "/usr/bin/python3 /usr/local/bin/supervisord\n/usr/sbin/rsyslogd -n\n",
                  "VLAN_INTERFACE": vlan_keys})
    return sw


def test_radvd_not_started_without_vlan_interfaces_is_not_down():
    assert _radv("").critical_down("radv") == []


def test_radvd_missing_with_vlan_interfaces_is_down():
    assert _radv("VLAN_INTERFACE|Vlan1000\n").critical_down("radv") == ["radvd:not running"]
