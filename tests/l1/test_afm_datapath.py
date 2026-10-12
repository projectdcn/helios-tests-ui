"""TC-AFM-001..004 + TC-AFM-005A — scale-up datapath smoke (L1).

Command lines come from ``commands.*`` templates in the inventory, because the
exact invocation (L2 ping tool, TransferBench preset/port-subset env, mpirun
flags) is site-specific. An unset template skips the test with a pointer to
the key, rather than guessing.
"""
from __future__ import annotations

import re

import pytest

from helios.compute import render
from helios.parsers import parse_bandwidth, parse_rccl, parse_size
from helios.rack import fan_out

pytestmark = pytest.mark.l1


def _template(inventory, key: str) -> str:
    t = inventory.cmd(key)
    if not t:
        pytest.skip(f"commands.{key} not set in {inventory.path.name}")
    return t


def _pairs(computes, mode: str):
    """Ordered (src, dst) compute pairs; both directions are always covered."""
    if len(computes) < 2:
        pytest.skip("cross-compute test needs >= 2 computes")
    if mode == "all":
        return [(a, b) for a in computes for b in computes if a is not b]
    ring = [(computes[i], computes[(i + 1) % len(computes)]) for i in range(len(computes))]
    if len(computes) == 2:
        return ring  # ring of two already holds both directions
    return ring + [(b, a) for a, b in ring]


def _workers(inventory, key: str, n: int) -> int:
    return n if inventory.th(f"datapath.{key}", True) else 1


@pytest.mark.tc("TC-AFM-001")
def test_l2_ping_within_and_across_computes(computes, inventory, checks, record_property):
    tmpl = _template(inventory, "l2ping.cmd")
    ok_re = inventory.cmd("l2ping.success_regex")
    timeout = float(inventory.cmd("l2ping.timeout_s", 30))

    jobs: dict[str, list[tuple]] = {c.name: [] for c in computes}
    for c in computes:
        jobs[c.name] += [("within", c, s, c, d) for s in range(c.gpus) for d in range(c.gpus) if s != d]
    for a, b in _pairs(computes, inventory.cmd("l2ping.cross_pairs", "ring")):
        jobs[a.name] += [("cross", a, s, b, d) for s in range(a.gpus) for d in range(b.gpus)]

    def run_src(c):
        fails = []
        for kind, src, s, dst, d in jobs[c.name]:
            cmd = render(tmpl, src_name=src.name, src_gpu=s, src_addr=src.gpu_addr(s),
                         dst_name=dst.name, dst_host=dst.host.host, dst_gpu=d, dst_addr=dst.gpu_addr(d))
            r = src.run(cmd, timeout=timeout)
            passed = r.ok and (not ok_re or re.search(ok_re, r.stdout))
            if not passed:
                fails.append(f"[{kind}] {src.name}/gpu{s} -> {dst.name}/gpu{d}: rc={r.rc} {r.tail(200)!r}")
        return fails

    total = sum(len(v) for v in jobs.values())
    record_property("pings", total)
    for c, fails, exc in fan_out(run_src, computes):
        if exc:
            checks.fail(f"{c.name}: {exc}")
        for f in fails or []:
            checks.fail(f)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-002")
def test_transferbench_in_domain(computes, inventory, checks, record_property):
    tmpl = _template(inventory, "transferbench.in_domain")
    regexes = inventory.cmd("transferbench.bw_regex")
    regexes = [regexes] if isinstance(regexes, str) else regexes
    min_bw = inventory.th("datapath.transferbench.in_domain_min_gbps")
    timeout = float(inventory.cmd("transferbench.timeout_s", 600))
    kw = dict(ports=inventory.cmd("transferbench.ports", ""), size=inventory.cmd("transferbench.size", "1G"))

    def run(c):
        return c.run(render(tmpl, name=c.name, gpus=c.gpus, **kw), timeout=timeout)

    bws = {}
    for c, r, exc in fan_out(run, computes, _workers(inventory, "parallel_computes", len(computes))):
        if exc or not r.ok:
            checks.fail(f"{c.name}: TransferBench failed: {exc or f'rc={r.rc} {r.tail()!r}'}")
            continue
        bw = parse_bandwidth(r.stdout, regexes)
        bws[c.name] = bw
        if checks.check(bw is not None, f"{c.name}: no bandwidth figure in output (calibrate commands.transferbench.bw_regex)"):
            if min_bw is not None:
                checks.check(bw >= min_bw, f"{c.name}: {bw:.1f} GB/s < {min_bw} GB/s")
    record_property("bw_gbps", bws)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-003")
def test_transferbench_cross_domain(computes, inventory, checks, record_property):
    tmpl = _template(inventory, "transferbench.cross_domain")
    regexes = inventory.cmd("transferbench.bw_regex")
    regexes = [regexes] if isinstance(regexes, str) else regexes
    min_bw = inventory.th("datapath.transferbench.cross_domain_min_gbps")
    timeout = float(inventory.cmd("transferbench.timeout_s", 600))
    pairs = _pairs(computes, inventory.cmd("transferbench.cross_pairs", "ring"))
    kw = dict(ports=inventory.cmd("transferbench.ports", ""), size=inventory.cmd("transferbench.size", "1G"))

    def run(pair):
        a, b = pair
        return a.run(render(tmpl, src_name=a.name, dst_name=b.name, src_host=a.host.host, dst_host=b.host.host,
                            gpus=a.gpus, **kw), timeout=timeout)

    bws = {}
    workers = len(pairs) if inventory.th("datapath.parallel_pairs", False) else 1
    for (a, b), r, exc in fan_out(run, pairs, workers):
        tag = f"{a.name}->{b.name}"
        if exc or not r.ok:
            checks.fail(f"{tag}: TransferBench failed: {exc or f'rc={r.rc} {r.tail()!r}'}")
            continue
        bw = parse_bandwidth(r.stdout, regexes)
        bws[tag] = bw
        if checks.check(bw is not None, f"{tag}: no bandwidth figure in output (calibrate commands.transferbench.bw_regex)"):
            if min_bw is not None:
                checks.check(bw >= min_bw, f"{tag}: {bw:.1f} GB/s < {min_bw} GB/s")
    record_property("bw_gbps", bws)
    checks.assert_all()


def _check_rccl(tag: str, r, collective: str, inventory, checks, section: str) -> float | None:
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


def _rccl_kw(inventory) -> dict:
    return dict(
        rccl_tests_dir=inventory.cmd("rccl.tests_dir", "/opt/rccl-tests"),
        min_bytes=inventory.cmd("rccl.min_bytes", "1K"),
        max_bytes=inventory.cmd("rccl.max_bytes", "1G"),
        iters=inventory.cmd("rccl.iters", 20),
    )


@pytest.mark.tc("TC-AFM-004")
def test_rccl_in_domain(computes, collective, inventory, checks, record_property):
    tmpl = _template(inventory, "rccl.in_domain")
    timeout = float(inventory.cmd("rccl.timeout_s", 900))
    kw = _rccl_kw(inventory)

    def run(c):
        return c.run(render(tmpl, collective=collective, gpus=c.gpus, name=c.name, **kw), timeout=timeout)

    busbw = {}
    for c, r, exc in fan_out(run, computes, _workers(inventory, "parallel_computes", len(computes))):
        if exc:
            checks.fail(f"{c.name}: {collective}: {exc}")
            continue
        busbw[c.name] = _check_rccl(c.name, r, collective, inventory, checks, "rccl")
    record_property("avg_busbw_gbps", busbw)
    checks.assert_all()


@pytest.mark.tc("TC-AFM-005A")
def test_rccl_cross_domain(computes, collective, inventory, checks, record_property):
    """Plan row 'TC-AFM-005 RCCL Cross Domain' (L1) — renumbered 005A because
    TC-AFM-005 is the L2 failover-policy CRUD case in the afm suite."""
    tmpl = _template(inventory, "rccl.cross_domain")
    if len(computes) < 2:
        pytest.skip("cross-domain RCCL needs >= 2 computes")
    timeout = float(inventory.cmd("rccl.timeout_s", 900))
    launcher = computes[0]
    hostlist = ",".join(f"{c.host.host}:{c.gpus}" for c in computes)
    np_ = sum(c.gpus for c in computes)
    r = launcher.run(render(tmpl, collective=collective, hostlist=hostlist, np=np_,
                            nodes=len(computes), **_rccl_kw(inventory)), timeout=timeout)
    record_property("avg_busbw_gbps", _check_rccl(f"cross[{len(computes)} nodes]", r, collective, inventory, checks, "rccl_cross"))
    checks.assert_all()
