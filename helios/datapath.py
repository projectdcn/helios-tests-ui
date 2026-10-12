"""Reusable datapath workloads: L2 ping, TransferBench, RCCL.

Shared by the L1 datapath cases and by every case that re-validates traffic
after a trigger (RAS resets, reboots, link events). Each helper records
failures in ``checks`` and returns the measurements; the caller asserts.
"""
from __future__ import annotations

import re

import pytest

from .compute import render
from .parsers import parse_bandwidth, parse_rccl, parse_size
from .rack import Checks, fan_out


def template(inventory, key: str) -> str:
    t = inventory.cmd(key)
    if not t:
        pytest.skip(f"commands.{key} not set in {inventory.path.name}")
    return t


def compute_pairs(computes, mode: str = "ring"):
    """Ordered (src, dst) compute pairs; both directions are always covered."""
    if len(computes) < 2:
        pytest.skip("cross-compute test needs >= 2 computes")
    if mode == "all":
        return [(a, b) for a in computes for b in computes if a is not b]
    ring = [(computes[i], computes[(i + 1) % len(computes)]) for i in range(len(computes))]
    if len(computes) == 2:
        return ring  # a ring of two already holds both directions
    return ring + [(b, a) for a, b in ring]


def _workers(inventory, key: str, n: int, default: bool) -> int:
    return n if inventory.th(f"datapath.{key}", default) else 1


# ------------------------------------------------------------------ L2 ping

def l2_ping(computes, inventory, checks: Checks) -> int:
    """Within each tray (every GPU pair) and across trays (ring/all, both directions). Returns ping count."""
    tmpl = template(inventory, "l2ping.cmd")
    ok_re = inventory.cmd("l2ping.success_regex")
    timeout = float(inventory.cmd("l2ping.timeout_s", 30))

    jobs: dict[str, list[tuple]] = {c.name: [] for c in computes}
    for c in computes:
        jobs[c.name] += [("within", c, s, c, d) for s in range(c.gpus) for d in range(c.gpus) if s != d]
    if len(computes) >= 2:
        for a, b in compute_pairs(computes, inventory.cmd("l2ping.cross_pairs", "ring")):
            jobs[a.name] += [("cross", a, s, b, d) for s in range(a.gpus) for d in range(b.gpus)]

    def run_src(c):
        fails = []
        for kind, src, s, dst, d in jobs[c.name]:
            cmd = render(tmpl, src_name=src.name, src_gpu=s, src_addr=src.gpu_addr(s),
                         dst_name=dst.name, dst_host=dst.host.host, dst_gpu=d, dst_addr=dst.gpu_addr(d))
            r = src.run(cmd, timeout=timeout)
            if not (r.ok and (not ok_re or re.search(ok_re, r.stdout))):
                fails.append(f"[{kind}] {src.name}/gpu{s} -> {dst.name}/gpu{d}: rc={r.rc} {r.tail(200)!r}")
        return fails

    for c, fails, exc in fan_out(run_src, computes):
        if exc:
            checks.fail(f"{c.name}: {exc}")
        for f in fails or []:
            checks.fail(f)
    return sum(len(v) for v in jobs.values())


# ------------------------------------------------------------ TransferBench

def _tb_common(inventory, size):
    regexes = inventory.cmd("transferbench.bw_regex")
    regexes = [regexes] if isinstance(regexes, str) else regexes
    kw = dict(ports=inventory.cmd("transferbench.ports", ""), size=size or inventory.cmd("transferbench.size", "1G"))
    return regexes, kw, float(inventory.cmd("transferbench.timeout_s", 600))


def _tb_check(tag, r, exc, regexes, min_bw, checks):
    if exc or not r.ok:
        checks.fail(f"{tag}: TransferBench failed: {exc or f'rc={r.rc} {r.tail()!r}'}")
        return None
    bw = parse_bandwidth(r.stdout, regexes)
    if checks.check(bw is not None, f"{tag}: no bandwidth figure in output (calibrate commands.transferbench.bw_regex)"):
        if min_bw is not None:
            checks.check(bw >= min_bw, f"{tag}: {bw:.1f} GB/s < {min_bw} GB/s")
    return bw


def transferbench_in_domain(computes, inventory, checks: Checks, size: str | None = None) -> dict:
    tmpl = template(inventory, "transferbench.in_domain")
    regexes, kw, timeout = _tb_common(inventory, size)
    min_bw = inventory.th("datapath.transferbench.in_domain_min_gbps")

    def run(c):
        return c.run(render(tmpl, name=c.name, gpus=c.gpus, **kw), timeout=timeout)

    return {c.name: _tb_check(c.name, r, exc, regexes, min_bw, checks)
            for c, r, exc in fan_out(run, computes, _workers(inventory, "parallel_computes", len(computes), True))}


def transferbench_cross_domain(computes, inventory, checks: Checks, size: str | None = None) -> dict:
    tmpl = template(inventory, "transferbench.cross_domain")
    regexes, kw, timeout = _tb_common(inventory, size)
    min_bw = inventory.th("datapath.transferbench.cross_domain_min_gbps")
    pairs = compute_pairs(computes, inventory.cmd("transferbench.cross_pairs", "ring"))

    def run(pair):
        a, b = pair
        return a.run(render(tmpl, src_name=a.name, dst_name=b.name, src_host=a.host.host, dst_host=b.host.host,
                            gpus=a.gpus, **kw), timeout=timeout)

    return {f"{a.name}->{b.name}": _tb_check(f"{a.name}->{b.name}", r, exc, regexes, min_bw, checks)
            for (a, b), r, exc in fan_out(run, pairs, _workers(inventory, "parallel_pairs", len(pairs), False))}


# --------------------------------------------------------------------- RCCL

def _rccl_kw(inventory) -> dict:
    return dict(
        rccl_tests_dir=inventory.cmd("rccl.tests_dir", "/opt/rccl-tests"),
        min_bytes=inventory.cmd("rccl.min_bytes", "1K"),
        max_bytes=inventory.cmd("rccl.max_bytes", "1G"),
        iters=inventory.cmd("rccl.iters", 20),
    )


def check_rccl(tag: str, r, collective: str, inventory, checks: Checks, section: str) -> float | None:
    if not r.ok:
        checks.fail(f"{tag}: {collective} rc={r.rc} {r.tail()!r}")
        return None
    res = parse_rccl(r.stdout)
    max_bytes = parse_size(inventory.cmd("rccl.max_bytes", "1G"))
    if not checks.check(res.rows, f"{tag}: {collective} produced no result rows"):
        return None
    checks.check(res.max_size >= max_bytes, f"{tag}: {collective} stopped at {res.max_size} B (< {max_bytes} B)")
    for row in res.wrong_rows:
        checks.fail(f"{tag}: {collective} data errors at {row.size} B (oop={row.oop_wrong}, ip={row.ip_wrong})")
    checks.check(not res.out_of_bounds, f"{tag}: {collective} {res.out_of_bounds} out-of-bounds values")
    checks.check(not res.errors, f"{tag}: {collective} errors: {res.errors[:3]}")
    floor = inventory.th(f"datapath.{section}.{collective}_min_busbw_gbps")
    if floor is not None and res.avg_busbw is not None:
        checks.check(res.avg_busbw >= floor, f"{tag}: {collective} avg busbw {res.avg_busbw} < {floor} GB/s")
    return res.avg_busbw


def rccl_in_domain(computes, inventory, checks: Checks, collective: str) -> dict:
    tmpl = template(inventory, "rccl.in_domain")
    timeout = float(inventory.cmd("rccl.timeout_s", 900))
    kw = _rccl_kw(inventory)

    def run(c):
        return c.run(render(tmpl, collective=collective, gpus=c.gpus, name=c.name, **kw), timeout=timeout)

    busbw = {}
    for c, r, exc in fan_out(run, computes, _workers(inventory, "parallel_computes", len(computes), True)):
        if exc:
            checks.fail(f"{c.name}: {collective}: {exc}")
            continue
        busbw[c.name] = check_rccl(c.name, r, collective, inventory, checks, "rccl")
    return busbw


def rccl_cross_domain(computes, inventory, checks: Checks, collective: str) -> float | None:
    tmpl = template(inventory, "rccl.cross_domain")
    if len(computes) < 2:
        pytest.skip("cross-domain RCCL needs >= 2 computes")
    timeout = float(inventory.cmd("rccl.timeout_s", 900))
    hostlist = ",".join(f"{c.host.host}:{c.gpus}" for c in computes)
    r = computes[0].run(render(tmpl, collective=collective, hostlist=hostlist, np=sum(c.gpus for c in computes),
                               nodes=len(computes), **_rccl_kw(inventory)), timeout=timeout)
    return check_rccl(f"cross[{len(computes)} nodes]", r, collective, inventory, checks, "rccl_cross")
