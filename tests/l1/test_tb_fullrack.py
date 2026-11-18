"""TransferBench presets over the whole rack (added to the test plan 17 Nov 2026, testplan/added.yaml).

The full-rack runs are one TransferBench job with one rank per tray (rank 0 on the first selected tray), the same
as /opt/TransferBench/LaunchTransferBench.sh but started over the runner's SSH channels (no node-to-node SSH).
Bootstrap port: random, or commands.transferbench.fullrack_port.

Thresholds (optional, null = not checked) under thresholds.datapath.transferbench:
  fullrack_a2a_min_gbps   aggregate (GPU timed) of the all-trays a2a job
  poda2a_min_gbps         pod all-to-all aggregate (STotal)
  poda2a_rx_min_ratio     every GPU's receive total >= ratio x the mean (default 0.5) - one GPU far behind the others
  link_min_gbps           slowest single GPU->GPU link between trays (podp2p)
  link_min_ratio          every link >= ratio x the median link (default 0.5) - one slow link among the others
  bisection_min_gbps      bisection aggregate (TransferBench 'Aggregate (CPU)')
  bisection_min_ratio     every bisection transfer >= ratio x the median transfer (default 0.5)
  wallclock_max_skew_us   largest timestamp difference between the XCCs of a GPU (default 10 us; TC-DP-008). The
                          per-transfer figures of TC-DP-006/007 come from these timestamps: transfers sent from a tray
                          above the limit are not judged on bandwidth and the case fails naming the tray.
Commands: commands.transferbench.link_size (bytes per transfer for podp2p / bisection, default 1G),
bisection_ses (CUs per bisection transfer, default 256 = what podp2p uses; 8 CUs cap a transfer at ~63 GB/s on
gfx1250, g02-g17 18 Nov), podp2p_timeout_s (default 2400).
"""
from __future__ import annotations

import re

import pytest

from helios.datapath import _workers, run_on, skewed_trays, tb_binary, transferbench_all_ranks, wallclock_skew
from helios.parsers import parse_bandwidth, parse_tb_poda2a, parse_tb_podp2p, parse_tb_smoketest, parse_tb_transfers
from helios.rack import fan_out

pytestmark = pytest.mark.l1

GPU_TIMED = r"Aggregate bandwidth \(GPU Timed\):\s*([\d.]+)"


def _judge_smoke(tag: str, text: str, gpus: int, ranks: int, checks) -> int:
    res = parse_tb_smoketest(text)
    if not checks.check(res.rows, f"{tag}: no smoketest result table in the output"):
        return 0
    for e in res.errors[:5]:
        checks.fail(f"{tag}: {e}")
    for name in res.failed():
        checks.fail(f"{tag}: FAIL {name} {res.rows[name]}")
    checks.check(res.passed, f"{tag}: no 'All tests passed' line")
    if res.gpus is not None:
        checks.check((res.gpus, res.ranks) == (gpus, ranks),
                     f"{tag}: ran on {res.gpus} GPUs / {res.ranks} rank(s), expected {gpus} / {ranks}")
    return res.ran()


@pytest.mark.datapath(tools=["transferbench"])
@pytest.mark.tc("TC-DP-004")
def test_tb_smoketest_per_tray(datapath_ready, computes, inventory, checks, record_property):
    """TransferBench smoketest preset on every tray on its own (all trays at once): every copy / broadcast / gather /
    all-to-all check over GFX and DMA passes on the tray's GPUs."""
    cmd = f"{tb_binary(inventory)} smoketest"
    timeout = float(inventory.cmd("transferbench.timeout_s", 600))
    ran = {}
    for c, r, exc in fan_out(lambda c: run_on(c, cmd, inventory, timeout), computes,
                             _workers(inventory, "parallel_computes", len(computes), True)):
        if exc or r is None:
            checks.fail(f"{c.name}: {exc}")
            continue
        checks.check(r.ok, f"{c.name}: TransferBench smoketest exit code {r.rc}")
        ran[c.name] = _judge_smoke(c.name, r.stdout, c.gpus, 1, checks)
    record_property("checks_run", ran)
    checks.assert_all()


@pytest.mark.datapath(tools=["transferbench"], min_computes=2)
@pytest.mark.tc("TC-DP-005")
def test_tb_smoketest_all_trays(datapath_ready, computes, inventory, checks, record_property):
    """TransferBench smoketest preset as one job over all trays (one rank per tray): the copy checks between GPUs of
    different trays pass (Broadcast/Gather/AllToAll are off by default on multi-node)."""
    tag = f"smoketest[{len(computes)} trays]"
    r0 = transferbench_all_ranks(computes, inventory, checks, "smoketest", tag)
    if r0 is not None:
        record_property("checks_run", _judge_smoke(tag, r0.stdout, sum(c.gpus for c in computes), len(computes), checks))
    checks.assert_all()


@pytest.mark.datapath(tools=["transferbench"], min_computes=2)
@pytest.mark.tc("TC-DP-002")
def test_tb_a2a_all_trays(datapath_ready, computes, inventory, checks, record_property):
    """TransferBench a2a as one job over all trays: every tray's GPUs all-to-all at the same time; aggregate reported."""
    tag = f"a2a[{len(computes)} trays]"
    r0 = transferbench_all_ranks(computes, inventory, checks, "a2a", tag)
    if r0 is not None:
        bw = parse_bandwidth(r0.stdout, [GPU_TIMED]) or parse_bandwidth(r0.stdout)
        if checks.check(bw is not None, f"{tag}: no aggregate bandwidth in the output"):
            record_property("bw_gbps", bw)
            low = inventory.th("datapath.transferbench.fullrack_a2a_min_gbps")
            if low is not None:
                checks.check(bw >= low, f"{tag}: aggregate {bw:.1f} GB/s < {low} GB/s")
        for e in re.findall(r"(?m)^\s*(ERROR\b.*)$", r0.stdout)[:5]:
            checks.fail(f"{tag}: {e}")
    checks.assert_all()


@pytest.mark.datapath(tools=["transferbench"], min_computes=2)
@pytest.mark.tc("TC-DP-003")
def test_tb_poda2a(datapath_ready, computes, inventory, checks, record_property):
    """TransferBench poda2a: every GPU of the pod sends to every other GPU (scale-up fabric, all trays): every GPU
    appears as sender and receiver, aggregate reported, no GPU's receive rate far below the others."""
    tag = f"poda2a[{len(computes)} trays]"
    gpus = sum(c.gpus for c in computes)
    r0 = transferbench_all_ranks(computes, inventory, checks, "poda2a", tag)
    if r0 is not None:
        p = parse_tb_poda2a(r0.stdout)
        for e in p.errors[:5]:
            checks.fail(f"{tag}: {e}")
        if checks.check(p.stotal is not None and p.rx, f"{tag}: no poda2a result matrix in the output"):
            checks.check(p.ranks in (None, len(computes)), f"{tag}: {p.ranks} ranks in the run, expected {len(computes)}")
            checks.check(len(p.src) == gpus and len(p.rx) == gpus,
                         f"{tag}: matrix has {len(p.src)} sending / {len(p.rx)} receiving GPUs, expected {gpus}")
            mean = sum(p.rx) / len(p.rx)
            ratio = float(inventory.th("datapath.transferbench.poda2a_rx_min_ratio", 0.5))
            labels = [f"{c.name} GPU{g}" for c in computes for g in range(c.gpus)]
            if len(labels) != len(p.rx):
                labels = [f"GPU#{i}" for i in range(len(p.rx))]
            slow = [f"{labels[i]} {v:.1f}" for i, v in enumerate(p.rx) if v < ratio * mean]
            checks.check(not slow, f"{tag}: receive rate < {ratio} x mean {mean:.1f} GB/s: {', '.join(slow)}")
            low = inventory.th("datapath.transferbench.poda2a_min_gbps")
            if low is not None:
                checks.check(p.stotal >= low, f"{tag}: aggregate {p.stotal:.1f} GB/s < {low} GB/s")
            record_property("bw_gbps", p.stotal)
            record_property("poda2a", {"gpus": len(p.rx), "stotal_gbps": p.stotal, "actual_gbps": p.actual,
                                       "rx_min": min(p.rx), "rx_max": max(p.rx), "rx_mean": round(mean, 2)})
    checks.assert_all()


def _skew_note(tag: str, skewed: dict, checks) -> None:
    if skewed:
        checks.fail(f"{tag}: GPU wall clock skew between XCCs on {len(skewed)} tray(s) - their GPU-timed transfer "
                    "figures are not valid and were not judged (see TC-DP-008): "
                    + ", ".join(f"{n} {'no figure' if v is None else f'{v:.0f} us'}" for n, v in sorted(skewed.items())))


def _errors(tag: str, text: str, checks) -> None:
    for e in re.findall(r"(?m)^\s*(\[?ERROR\]?\b.*)$", text)[:5]:
        checks.fail(f"{tag}: {e}")


@pytest.mark.datapath(tools=["transferbench"], min_computes=2)
@pytest.mark.tc("TC-DP-006")
def test_tb_link_bandwidth(datapath_ready, computes, inventory, checks, record_property):
    """Every GPU->GPU link between trays measured on its own (TransferBench podp2p, unidirectional; tray pairs in
    parallel round-robin, so each tray talks to one other tray at a time): every link has a figure, none far below
    the median, the slowest is reported."""
    tag = f"podp2p[{len(computes)} trays]"
    skewed = skewed_trays(computes, inventory)
    size = inventory.cmd("transferbench.link_size", "1G")
    r0 = transferbench_all_ranks(computes, inventory, checks, "podp2p", tag,
                                 env="PARALLEL_LVL=1 P2P_MODE=1 OUTPUT_FORMAT=0", args=str(size),
                                 timeout=float(inventory.cmd("transferbench.podp2p_timeout_s", 2400)))
    if r0 is None:
        checks.assert_all()
        return
    _errors(tag, r0.stdout, checks)
    gpus = [(r, g) for r, c in enumerate(computes) for g in range(c.gpus)]
    name = {r: c.name for r, c in enumerate(computes)}
    links = parse_tb_podp2p(r0.stdout)
    want = [(a, b, x, y) for a, b in gpus for x, y in gpus if a != x]
    missing = [k for k in want if links.get(k) is None]
    if not checks.check(len(missing) < len(want), f"{tag}: no per-link result in the output"):
        checks.assert_all()
        return
    checks.check(not missing, f"{tag}: {len(missing)} of {len(want)} cross-tray links without a figure, e.g. "
                 + ", ".join(f"{name[a]} GPU{b}->{name[x]} GPU{y}" for a, b, x, y in missing[:5]))
    _skew_note(tag, skewed, checks)
    vals = sorted((v, k) for k in want if (v := links.get(k)) is not None and name[k[0]] not in skewed)
    if not checks.check(vals, f"{tag}: no link from a tray with a valid GPU clock to judge"):
        checks.assert_all()
        return
    median = vals[len(vals) // 2][0]
    label = lambda k: f"{name[k[0]]} GPU{k[1]}->{name[k[2]]} GPU{k[3]}"  # noqa: E731
    ratio = float(inventory.th("datapath.transferbench.link_min_ratio", 0.5))
    slow = [f"{label(k)} {v:.1f}" for v, k in vals if v < ratio * median]
    checks.check(not slow, f"{tag}: {len(slow)} link(s) < {ratio} x median {median:.1f} GB/s: {', '.join(slow[:10])}")
    low = inventory.th("datapath.transferbench.link_min_gbps")
    if low is not None:
        checks.check(vals[0][0] >= low, f"{tag}: slowest link {label(vals[0][1])} {vals[0][0]:.1f} GB/s < {low} GB/s")
    record_property("bw_gbps", vals[0][0])
    record_property("links", {"judged": len(vals), "expected": len(want), "skewed_trays": sorted(skewed),
                              "min_gbps": vals[0][0],
                              "median_gbps": median, "max_gbps": vals[-1][0],
                              "slowest": [f"{label(k)} {v:.2f}" for v, k in vals[:5]]})
    checks.assert_all()


@pytest.mark.datapath(tools=["transferbench"], min_computes=2)
@pytest.mark.tc("TC-DP-007")
def test_tb_bisection(datapath_ready, computes, inventory, checks, record_property):
    """Bisection: the first half of the trays and the second half exchange data at the same time, every GPU g of
    tray i with GPU g of tray i + n/2, both directions (TransferBench cmdline, one rank per tray); aggregate reported."""
    half = len(computes) // 2
    xfers = [f"R{a}G{g}->R{a}G{g}->R{b}G{g}" for i in range(half)
             for a, b in ((i, i + half), (i + half, i)) for g in range(min(computes[i].gpus, computes[i + half].gpus))]
    tag = f"bisection[{half}<->{half} trays, {len(xfers)} transfers]"
    skewed = skewed_trays(computes[:2 * half], inventory)
    size = inventory.cmd("transferbench.link_size", "1G")
    ses = int(inventory.cmd("transferbench.bisection_ses", 256))
    r0 = transferbench_all_ranks(computes[:2 * half], inventory, checks, "cmdline", tag,
                                 args=f"{size} '{len(xfers)} {ses} {' '.join(xfers)}'")
    if r0 is not None:
        _errors(tag, r0.stdout, checks)
        res = parse_tb_transfers(r0.stdout)
        if checks.check(res.transfers and res.aggregate_cpu is not None, f"{tag}: no per-transfer results in the output"):
            checks.check(len(res.transfers) == len(xfers),
                         f"{tag}: {len(res.transfers)} transfer results, expected {len(xfers)}")
            name = {r: c.name for r, c in enumerate(computes)}
            _skew_note(tag, skewed, checks)
            vals = sorted((t[4], t) for t in res.transfers.values() if name.get(t[0]) not in skewed)
            if not checks.check(vals, f"{tag}: no transfer from a tray with a valid GPU clock to judge"):
                checks.assert_all()
                return
            median = vals[len(vals) // 2][0]
            ratio = float(inventory.th("datapath.transferbench.bisection_min_ratio", 0.5))
            slow = [f"{name.get(t[0], t[0])} GPU{t[1]}->{name.get(t[2], t[2])} GPU{t[3]} {v:.1f}"
                    for v, t in vals if v < ratio * median]
            checks.check(not slow, f"{tag}: transfer(s) < {ratio} x median {median:.1f} GB/s: {', '.join(slow[:10])}")
            low = inventory.th("datapath.transferbench.bisection_min_gbps")
            if low is not None:
                checks.check(res.aggregate_cpu >= low, f"{tag}: aggregate {res.aggregate_cpu:.1f} GB/s < {low} GB/s")
            record_property("bw_gbps", res.aggregate_cpu)
            record_property("bisection", {"judged": len(vals), "skewed_trays": sorted(skewed),
                                          "aggregate_gbps": res.aggregate_cpu,
                                          "min_gbps": vals[0][0], "median_gbps": median, "max_gbps": vals[-1][0]})
    checks.assert_all()


@pytest.mark.datapath(tools=["transferbench"])
@pytest.mark.tc("TC-DP-008")
def test_gpu_wallclock_consistent(datapath_ready, computes, inventory, checks, record_property):
    """GPU wall clock consistent inside each GPU (TransferBench wallclock): the timestamp counters of a GPU's XCCs
    agree within thresholds.datapath.transferbench.wallclock_max_skew_us (default 10 us). Healthy trays: < 1.1 us;
    g02-g17 18 Nov: 5.5-6.9 ms on 5 trays, which corrupts every GPU-timed measurement there."""
    limit = float(inventory.th("datapath.transferbench.wallclock_max_skew_us", 10))
    skew = wallclock_skew(computes, inventory)
    for n, v in sorted(skew.items()):
        if v is None:
            checks.fail(f"{n}: TransferBench wallclock gave no 'Maximum Delta' figure")
        else:
            checks.check(v <= limit, f"{n}: XCC wall clock skew {v:.1f} us > {limit} us")
    record_property("wallclock_skew_us", skew)
    checks.assert_all()
