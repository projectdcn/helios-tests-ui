"""TC-040, TC-041, TC-043, TC-SBM-175 - IFCP control frames on the wire, captured read-only on the switch trays.

The IFCP frames between a switch tray and a GPU reach the switch CPU on the fabric port's netdev inside its ASIC
namespace (QFX5252 / g02-g17, 26 Oct 2026): switch->compute (egress) frames are 802.1Q VLAN 0, PCP 7, inner
ethertype 0x88b6; compute->switch (ingress) frames are ethertype 0xeb87. In steady state each side sends one
keepalive every 5 s on every link (all 6 links of a GPU-switch pair). ``SonicSwitch.ifcp_capture`` runs tcpdump on a
few cabled, oper-up fabric ports per switch at the same time; the four cases share that one capture per switch.

SPDM session, SelectedLink and per-session statistics are not on the wire in a readable form and not exposed by
afmctl or the sb-node-agent API (/api/health, /api/runtime/requests only), so TC-BU-004/008/010 and
TC-SBM-003/004/010 stay unautomated.

Thresholds (inventory ``thresholds.ifcp``):
  wire_ports_per_switch (2)   ports sampled per switch tray (cabled to a compute, oper up, spread over the ASICs)
  capture_s (12)              capture window per port
  keepalive_s (5.0), keepalive_tolerance_s (1.0)   expected keepalive period and allowed deviation (TC-043)
  reply_max_s (1.0)           a compute keepalive must answer each switch keepalive within this (TC-043)
  tx_ethertype (0x88b6), rx_ethertype (0xeb87), pcp (7)   frame format (TC-040 / 041 / SBM-175)
"""
from __future__ import annotations

import math

import pytest

from helios.afmctl import parse_port_description
from helios.rack import fan_out

pytestmark = pytest.mark.l3


def _th(inventory, key, default):
    v = inventory.th(f"ifcp.{key}", default)
    return default if v is None else v


def _sample_ports(switch, inventory) -> list[tuple[str | None, str]]:
    """Up to ``wire_ports_per_switch`` (ns, port) that are oper up and cabled to a compute (by description),
    taken round-robin over the ASIC namespaces; quarantined trays excepted."""
    want = int(_th(inventory, "wire_ports_per_switch", 2))
    qslots = inventory.quarantined_slots()
    per_ns = []
    for ns in switch.namespaces():
        desc = switch.port_descriptions(ns)
        up = sorted((r["Interface"] for r in switch.interfaces_status(ns) if r.get("Oper", "").lower() == "up"),
                    key=lambda p: int(p[8:]) if p[8:].isdigit() else 0)
        cab = [p for p in up if (c := parse_port_description(desc.get(p, ""))) and c[0] not in qslots]
        per_ns.append([(ns, p) for p in cab])
    out = []
    while len(out) < want and any(per_ns):
        for lst in per_ns:
            if lst and len(out) < want:
                out.append(lst.pop(0))
    return out


@pytest.fixture
def capture(switch, inventory, pytestconfig):
    """{(ns, port): [frames]} of this switch, captured once per session (all sampled ports in parallel)."""
    cache = pytestconfig.__dict__.setdefault("_helios_ifcp_wire", {})
    if switch.name not in cache:
        ports = _sample_ports(switch, inventory)
        secs = float(_th(inventory, "capture_s", 12))
        res, err = {}, None
        if ports:
            for (ns, port), frames, exc in fan_out(lambda np: switch.ifcp_capture(np[1], np[0], secs), ports):
                if exc is not None:
                    err = str(exc)
                else:
                    res[(ns, port)] = frames
        cache[switch.name] = (res, err, secs)
    res, err, secs = cache[switch.name]
    if not res:
        if err:
            pytest.fail(f"{switch.name}: IFCP capture failed: {err}")
        pytest.skip(f"{switch.name}: no oper-up fabric port cabled to a compute")
    return res, secs


def _summary(frames) -> dict:
    tx = [f for f in frames if f["dir"] == "tx"]
    rx = [f for f in frames if f["dir"] == "rx"]
    return {"tx": len(tx), "rx": len(rx)}


def _min_frames(secs: float, period: float) -> int:
    return max(1, math.floor(secs / period) - 1)


@pytest.mark.tc("TC-040")
def test_ifcp_egress_switch_to_compute(switch, inventory, capture, checks, record_property):
    """Every sampled port carries switch->compute IFCP frames: 802.1Q-tagged with the IFCP ethertype
    (ifcp.tx_ethertype) and one destination (the GPU port's MAC)."""
    res, _ = capture
    want_et = str(_th(inventory, "tx_ethertype", switch.IFCP_TX_ETHERTYPE)).lower()
    out = {}
    for (ns, port), frames in res.items():
        where = f"{switch.name}/{ns}/{port}"
        tx = [f for f in frames if f["dir"] == "tx"]
        out[where] = {**_summary(frames), "dst": sorted({f["dst"] for f in tx})}
        if not checks.check(tx, f"{where}: no switch->compute IFCP frame captured"):
            continue
        checks.check(all(f["ethertype"] == want_et and f["pcp"] is not None for f in tx),
                     f"{where}: egress IFCP frames not 802.1Q-tagged {want_et}")
        dsts = sorted({f["dst"] for f in tx})
        checks.check(len(dsts) == 1, f"{where}: egress IFCP frames to {len(dsts)} MACs {dsts} (re-negotiation or "
                                     f"a second peer during the capture)")
    record_property("egress", out)
    checks.assert_all()


@pytest.mark.tc("TC-041")
def test_ifcp_ingress_compute_to_switch(switch, inventory, capture, checks, record_property):
    """Every sampled port receives compute->switch IFCP frames from the GPU port the switch sends to (rx source MAC ==
    tx destination MAC), in either compute format: ifcp.rx_ethertype (0xeb87, untagged) or the switch's own
    802.1Q 0x88b6 (the compute answers in that format since ~Nov 2026)."""
    res, _ = capture
    want_et = {str(_th(inventory, "rx_ethertype", switch.IFCP_RX_ETHERTYPE)).lower(),
               str(_th(inventory, "tx_ethertype", switch.IFCP_TX_ETHERTYPE)).lower()}
    out = {}
    for (ns, port), frames in res.items():
        where = f"{switch.name}/{ns}/{port}"
        rx = [f for f in frames if f["dir"] == "rx"]
        tx_dst = {f["dst"] for f in frames if f["dir"] == "tx"}
        out[where] = {**_summary(frames), "src": sorted({f["src"] for f in rx}),
                      "rx_ethertype": sorted({f["ethertype"] for f in rx})}
        if not checks.check(rx, f"{where}: no compute->switch IFCP frame captured"):
            continue
        checks.check(all(f["ethertype"] in want_et for f in rx), f"{where}: ingress IFCP frames not {sorted(want_et)}")
        if tx_dst:
            stray = sorted({f["src"] for f in rx} - tx_dst)
            checks.check(not stray, f"{where}: ingress IFCP from {stray}, switch sends to {sorted(tx_dst)}")
    record_property("ingress", out)
    checks.assert_all()


@pytest.mark.tc("TC-043")
def test_ifcp_keepalive_bidirectional(switch, inventory, capture, checks, record_property):
    """Steady-state keepalive in both directions on every sampled port: at least floor(capture/period)-1 frames each
    way, switch keepalives every ifcp.keepalive_s (5 s) +- tolerance, each answered within ifcp.reply_max_s."""
    res, secs = capture
    period = float(_th(inventory, "keepalive_s", 5.0))
    tol = float(_th(inventory, "keepalive_tolerance_s", 1.0))
    reply_max = float(_th(inventory, "reply_max_s", 1.0))
    need = _min_frames(secs, period)
    out = {}
    for (ns, port), frames in res.items():
        where = f"{switch.name}/{ns}/{port}"
        tx = [f["t"] for f in frames if f["dir"] == "tx"]
        rx = [f["t"] for f in frames if f["dir"] == "rx"]
        gaps = [round(b - a, 2) for a, b in zip(tx, tx[1:])]
        late = [t for t in tx if not any(0 <= r - t <= reply_max for r in rx)]
        out[where] = {"tx": len(tx), "rx": len(rx), "tx_intervals_s": gaps, "unanswered": len(late)}
        checks.check(len(tx) >= need, f"{where}: {len(tx)} switch keepalive(s) in {secs:.0f}s, expected >= {need}")
        checks.check(len(rx) >= need, f"{where}: {len(rx)} compute keepalive(s) in {secs:.0f}s, expected >= {need}")
        bad = [g for g in gaps if abs(g - period) > tol]
        kind = ("burst" if any(g < period - tol for g in bad) else "") + \
               ("/gap" if any(g > period + tol for g in bad) else "")
        checks.check(not bad, f"{where}: keepalive intervals {bad} s ({kind.strip('/')}), expected {period} +- {tol} s")
        checks.check(not late, f"{where}: {len(late)} switch keepalive(s) not answered within {reply_max}s")
    record_property("keepalive", out)
    checks.assert_all()


@pytest.mark.tc("TC-SBM-175")
def test_ifcp_priority_marking(switch, inventory, capture, checks, record_property):
    """Every IFCP frame the switch sends carries 802.1Q priority ifcp.pcp (7), whatever its message type (the
    payload's first 6 bytes); the message types seen in the window are recorded - in steady state only keepalives,
    so metrics / event messages are checked only when they occur during the capture."""
    res, _ = capture
    want = int(_th(inventory, "pcp", 7))
    types: dict[str, set] = {}
    for (ns, port), frames in res.items():
        where = f"{switch.name}/{ns}/{port}"
        tx = [f for f in frames if f["dir"] == "tx"]
        if not checks.check(tx, f"{where}: no switch->compute IFCP frame to check"):
            continue
        for f in tx:
            types.setdefault(f["payload"][:12] or "?", set()).add(f["pcp"])
        wrong = sorted({f["pcp"] for f in tx if f["pcp"] != want}, key=str)
        checks.check(not wrong, f"{where}: IFCP frames with priority {wrong}, expected {want}")
    record_property("message_types_pcp", {k: sorted(v, key=str) for k, v in types.items()})
    checks.assert_all()
