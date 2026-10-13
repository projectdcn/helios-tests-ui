"""Datapath pre-flight: refuse to start IFoE traffic on a fabric that is known to be broken.

Learned on MR3: a switch plane silently dropping ~100% of one tray's ingress made the first RCCL
collective hang forever (and the tray later stopped serving SSH). Every link, LLDP, VLAN/FDB entry
and RX_ERR counter looked healthy; only the drop counters and a per-port ping showed it. Running
traffic into that state wastes a timeout per test at best and wedges the host at worst.

Checks (fast, read-only):
  * each selected compute answers SSH within ``datapath.preflight.ssh_timeout_s`` (20 s)
  * every GPU on it is config_phase ACTIVE (afmctl)
  * no switch fabric port is dropping faster than ``max_rate_pps`` (100/s) over ``window_s`` (5 s)
  * ping probe (``ping_probe``, default on): one afmctl L2 ping per GPU to the next GPU on the same tray over
    every LINK_UP port, so every switching plane carries a frame before RCCL/TransferBench does. Needed
    because a broken plane only starts dropping once traffic is sent on it - an idle fabric shows no drops.
"""
from __future__ import annotations

import time

from .afmctl import Afmctl, parse_port_description
from .parsers import to_float
from .rack import fan_out


def _drop_rates(switch, window: float) -> dict[tuple[str | None, str], float]:
    nss = switch.namespaces()
    before = {ns: switch.interfaces_counters(ns) for ns in nss}
    t0 = time.monotonic()
    time.sleep(window)
    after = {ns: switch.interfaces_counters(ns) for ns in nss}
    dt = max(time.monotonic() - t0, 1e-6)
    rates = {}
    for ns in nss:
        for port, row in after[ns].items():
            for col in ("RX_DRP", "TX_DRP"):
                a, b = to_float(row.get(col)), to_float(before[ns].get(port, {}).get(col))
                if a is not None and b is not None and a > b:
                    rates[(ns, f"{port} {col}")] = (a - b) / dt
    return rates


def run(rack, computes, inventory) -> list[str]:
    """Return a list of blocking problems (empty == datapath may run)."""
    th = lambda k, d: inventory.th(f"datapath.preflight.{k}", d)  # noqa: E731
    problems: list[str] = []

    def probe(c):
        r = c.run("true", timeout=float(th("ssh_timeout_s", 20)))
        if not r.ok:
            return f"{c.name}: not responding (rc={r.rc})", set()
        devs = Afmctl(c, timeout=60).devices()
        bad = [f"{d.bdf}={d.config_phase}" for d in devs if d.config_phase != "ACTIVE"]
        return (f"{c.name}: GPUs not ACTIVE: {bad}" if bad else ""), {int(d.accel_id) for d in devs if d.accel_id.isdigit()}

    accs: set[int] = set()
    for c, res, exc in fan_out(probe, computes):
        if exc:
            problems.append(f"{c.name}: unreachable/unresponsive ({type(exc).__name__}: {str(exc)[:120]})")
            continue
        msg, a = res
        accs |= a
        if msg:
            problems.append(msg)

    if th("ping_probe", True) and not problems:
        problems += _ping_probe(computes, float(th("ping_timeout_s", 200)))

    window, max_rate = float(th("window_s", 5)), float(th("max_rate_pps", 100))
    for sw, rates, exc in fan_out(lambda s: _drop_rates(s, window), list(rack.switches.values())):
        if exc:
            problems.append(f"{sw.name}: cannot read counters: {exc}")
            continue
        hot = sorted(((r, ns, p) for (ns, p), r in rates.items() if r > max_rate), reverse=True)
        relevant = []
        for r, ns, p in hot:
            desc = sw.port_description(p.split()[0], ns)
            cab = parse_port_description(desc)
            # only ports cabled to the accelerators under test matter; unknown cabling counts (conservative)
            if cab is None or not accs or cab[2] in accs:
                relevant.append(f"{ns}/{p} {r:,.0f}/s ({desc or 'no description'})")
        if relevant:
            problems.append(f"{sw.name}: {len(relevant)} fabric port(s) dropping now: " + "; ".join(relevant[:8]))
    return problems


def _ping_probe(computes, timeout: float) -> list[str]:
    """One in-tray L2 ping per GPU (ring: GPU i -> GPU i+1) over all LINK_UP ports on both ends."""

    def one(c):
        a = Afmctl(c, timeout=60)
        devs = a.devices()
        if len(devs) < 2:
            return []
        up = {d.bdf: {p.id for p in a.ports(d.bdf) if p.up} for d in devs}
        fails = []
        for i, d in enumerate(devs):
            dst = devs[(i + 1) % len(devs)]
            common = sorted(up[d.bdf] & up[dst.bdf])
            if not common:
                fails.append(f"{c.name}/{d.bdf} -> acc {dst.accel_id}: no LINK_UP port in common")
                continue
            res, r = a.ping(d.bdf, dst.accel_id, common, timeout=timeout)
            if res.total is None:
                fails.append(f"{c.name}/{d.bdf} -> acc {dst.accel_id}: no ping summary (rc={r.rc})")
            elif not res.ok:
                ports = ",".join(res.failed_ports) or "?"
                fails.append(f"{c.name}/{d.bdf} -> acc {dst.accel_id}: {res.request_fail}/{res.total} port(s) "
                             f"black-holed (port {ports})")
        return fails

    out = []
    for c, fails, exc in fan_out(one, computes):
        if exc:
            out.append(f"{c.name}: ping probe failed: {exc}")
        out += fails or []
    return [f"ping probe: {f}" for f in out]
