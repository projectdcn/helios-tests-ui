"""TC-IBS-001..006 — mortaro scale-out RDMA bandwidth (perftest ib_*_bw).

Inventory: ``commands.perftest.nodes`` (server, client) and per-compute
``rdma: [{dev, ip4, ip6, gpu}, ...]`` in P1..P4 order.
"""
from __future__ import annotations

import pytest

from helios import perftest

ALL_VERBS = ["write", "write_imm", "send", "send_imm", "read"]


@pytest.fixture
def nodes(rack, inventory, pytestconfig):
    """The two scale-out nodes; fails setup (reported BLOCKED) when NIC links are down or perftest lacks ROCm."""
    pair = perftest.scaleout_nodes(rack, inventory)
    cache = pytestconfig.__dict__.setdefault("_helios_rdma_preflight", {})
    key = tuple(n.name for n in pair)
    if key not in cache:
        cache[key] = perftest.rdma_preflight(pair, inventory)
    if cache[key]:
        pytest.fail("datapath pre-flight failed - scale-out RDMA not ready, not starting perftest:\n"
                    + "\n".join(f"  - {p}" for p in cache[key]), pytrace=False)
    return pair


def _defaults(inventory):
    return dict(af=inventory.cmd("perftest.default_af", "ipv6"),
                bidir=inventory.cmd("perftest.default_dir", "bidir") == "bidir")


@pytest.mark.l1
@pytest.mark.tc("TC-IBS-001")
def test_crossnode_write_bw_all_pairs(nodes, inventory, checks, record_property):
    """ib_write_bw on all port pairs in parallel, GPU memory, IPv6, bidirectional."""
    res = perftest.run_pairs(perftest.cross_pairs(*nodes), inventory, verb="write", mem="gpu", af="ipv6", bidir=True)
    record_property("bw_gbps", perftest.check_pairs(res, inventory, checks, verb="write"))
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-IBS-002")
@pytest.mark.parametrize("verb", ALL_VERBS)
def test_multi_verb_bw(nodes, inventory, checks, record_property, verb):
    res = perftest.run_pairs(perftest.cross_pairs(*nodes), inventory, verb=verb, mem="gpu", **_defaults(inventory))
    record_property("bw_gbps", perftest.check_pairs(res, inventory, checks, verb=verb))
    checks.assert_all()


@pytest.mark.l1
@pytest.mark.tc("TC-IBS-005")
def test_single_node_loopback_bw(nodes, inventory, checks, record_property):
    """P1<->P2 and P3<->P4 on the same node: NIC-to-NIC path + GPU memory, no second node."""
    res = perftest.run_pairs(perftest.loopback_pairs(nodes[0]), inventory, verb="write", mem="gpu",
                             **_defaults(inventory))
    record_property("bw_gbps", perftest.check_pairs(res, inventory, checks, verb="write"))
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-IBS-003")
def test_qp_scaling(nodes, inventory, checks, record_property):
    """ib_write_bw, GPU memory, QPs 8..256: no errors/hangs, and bandwidth never falls more than
    ``datapath.perftest.qp_max_drop_pct`` below the 8-QP baseline."""
    qp_list = inventory.cmd("perftest.qp_sweep", [8, 16, 32, 64, 128, 256])
    max_drop = float(inventory.th("datapath.perftest.qp_max_drop_pct", 10))
    pairs = perftest.cross_pairs(*nodes)
    sweep: dict[int, dict] = {}
    for qps in qp_list:
        res = perftest.run_pairs(pairs, inventory, verb="write", mem="gpu", qps=qps, **_defaults(inventory))
        sweep[qps] = perftest.check_pairs(res, inventory, checks, verb="write", what=f"[{qps} QPs]")
    base = sweep.get(qp_list[0], {})
    for qps, bws in sweep.items():
        for tag, bw in bws.items():
            ref = base.get(tag.replace(f"[{qps} QPs]", f"[{qp_list[0]} QPs]"))
            if ref:
                checks.check(bw >= ref * (1 - max_drop / 100),
                             f"{tag}: {bw:.1f} Gb/s is >{max_drop:g}% below {qp_list[0]}-QP baseline {ref:.1f}")
    record_property("bw_gbps_by_qps", sweep)
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-IBS-004")
@pytest.mark.parametrize("direction", ["unidir", "bidir"])
@pytest.mark.parametrize("af", ["ipv4", "ipv6"])
@pytest.mark.parametrize("mem", ["cpu", "gpu"])
@pytest.mark.parametrize("verb", ALL_VERBS)
def test_full_parameter_matrix(nodes, inventory, checks, record_property, verb, mem, af, direction):
    """5 verbs x 2 mem x 2 AF x 2 dir = 40 items, each over all 4 port pairs (320 runs)."""
    res = perftest.run_pairs(perftest.cross_pairs(*nodes), inventory, verb=verb, mem=mem, af=af,
                             bidir=direction == "bidir")
    record_property("bw_gbps", perftest.check_pairs(res, inventory, checks, verb=verb))
    checks.assert_all()


@pytest.mark.l3
@pytest.mark.tc("TC-IBS-006")
def test_configurable(nodes, inventory, checks, record_property):
    """Any combination from ``commands.perftest.custom`` (verbs, qps, mem, af, dir, pairs, iterations)."""
    c = inventory.cmd("perftest.custom")
    if not c:
        pytest.skip("commands.perftest.custom not set (verbs/qps/mem/af/dir/pairs/iterations)")
    pairs = perftest.cross_pairs(*nodes, only=c.get("pairs"))
    out = {}
    for it in range(int(c.get("iterations", 1))):
        for verb in c.get("verbs", ["write"]):
            for qps in c.get("qps", [1]):
                for mem in c.get("mem", ["gpu"]):
                    for af in c.get("af", ["ipv6"]):
                        for d in c.get("dir", ["bidir"]):
                            what = f"[it{it} {verb} q{qps} {mem} {af} {d}]"
                            res = perftest.run_pairs(pairs, inventory, verb=verb, mem=mem, af=af,
                                                     bidir=d == "bidir", qps=qps)
                            out.update(perftest.check_pairs(res, inventory, checks, verb=verb, what=what))
    record_property("bw_gbps", out)
    checks.assert_all()
