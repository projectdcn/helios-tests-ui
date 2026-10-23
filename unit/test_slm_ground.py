"""tools/slm_ground.py: the command reference holds only commands the tests run (and only read-only ones), and a
tray's cabling comes from the newest link scan, dated."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import slm_ground as g  # noqa: E402


def test_every_switch_and_compute_command_is_one_the_tests_run_and_read_only():
    code = "\n".join(p.read_text() for p in (ROOT / "helios").glob("*.py"))
    code = code.replace("{_ns_flag(ns)}", " -n asicN")
    for where, cmd, _what in g.COMMANDS:
        if where not in ("switch", "compute"):
            continue
        head = re.split(r" \||/ | -n asicN| --json| -b | 'PORT| Ethernet\d", cmd)[0].strip()
        assert head in code, f"{cmd!r}: {head!r} is not a command the tests run"
        assert not re.search(r"\b(clear|restart|stop|kill|config (interface|reload|load)|shutdown|startup|del|hset)\b",
                             cmd), cmd
    assert "give commands only from this list" in g.command_reference()


def test_tray_cabling_from_the_newest_scan(tmp_path):
    for when, oper in (("20261019-012215", "up"), ("20261021-151029", "down")):
        d = tmp_path / "link-scans" / f"scan-{when}"
        d.mkdir(parents=True)
        (d / "link_check.json").write_text(json.dumps({"time_utc": when, "learned": {"slots": {"ct13": 12}}, "links": [
            {"tray": "ct13", "switch": "st6", "asic": "asic1", "sw_port": "Ethernet261", "sw_oper": oper, "sw_admin": "up",
             "desc": "To ComputeTray Slot12/GPU2[50]/Port0"},
            {"tray": "ct13", "switch": "st1", "asic": "asic0", "sw_port": "Ethernet36", "sw_oper": "up", "sw_admin": "up"}]}))
    txt = g.tray_cabling(tmp_path, ["ct13", "ct99"])
    assert "link scan of 20261021-151029 UTC: 2 GPU links, 1 up, 1 not up" in txt
    assert "NOT UP: st6 asic1 Ethernet261 (To ComputeTray Slot12/GPU2[50]/Port0)" in txt and "grep 'Slot12/'" in txt
    assert "ct99" not in txt and g.tray_cabling(tmp_path / "none", ["ct13"]) == ""
    assert g.trays_in("look at ct13 and ct05, not ct1") == ["ct05", "ct13"]
