"""RcclSmokeTest (AMD helicop utils) — RCCL pass/fail sanity, single-node and multi-node.

Not a test-plan case; a fast correctness gate to run before the rccl-tests
bandwidth cases (TC-AFM-004/005A). Multi-node runs one process per GPU,
bootstrapped with NCCL_COMM_ID, so it needs neither MPI nor node-to-node SSH.

Inventory ``commands.rccl_smoke``: bin (path on the computes), env (prefix,
e.g. "NCCL_LOG_DIR=/tmp/helios-rccl-logs"), args (e.g. "--size 1gb"),
port (bootstrap port; default random), timeout_s.
"""
from __future__ import annotations

import random

import pytest

from helios.datapath import addr, run_on, run_ranks, use_sudo
from helios.parsers import parse_smoketest
from helios.rack import fan_out

pytestmark = pytest.mark.l1


def _cfg(inventory, key, default=None):
    v = inventory.cmd(f"rccl_smoke.{key}")
    return default if v is None else v


def _bin(inventory) -> str:
    b = _cfg(inventory, "bin")
    if not b:
        pytest.skip("commands.rccl_smoke.bin not set")
    return b


def _judge(tag: str, r, checks, allow_oom: bool) -> None:
    res = parse_smoketest(r.stdout)
    if not checks.check(res.rows, f"{tag}: no result table in output (rc={r.rc}) {r.tail(300)!r}"):
        return
    for cell in res.cells("F"):
        checks.fail(f"{tag}: FAIL {cell}")
    if not allow_oom:
        for cell in res.cells("S"):
            checks.fail(f"{tag}: skipped (OOM) {cell}")
    checks.check(res.passed, f"{tag}: RESULT line does not report ALL TESTS PASSED")
    checks.check(r.ok, f"{tag}: exit code {r.rc}")


@pytest.mark.datapath(tools=["rccl_smoke"])
@pytest.mark.tc("SMOKE-RCCL-1N")
def test_rccl_smoke_single_node(datapath_ready, computes, inventory, checks):
    """Each compute tray on its own: all local GPUs, all collectives x 1KB/4MB/1GB."""
    cmd = " ".join(x for x in (_cfg(inventory, "env", ""), _bin(inventory), _cfg(inventory, "args", "")) if x)
    timeout = float(_cfg(inventory, "timeout_s", 600))
    allow_oom = bool(_cfg(inventory, "allow_oom_skip", False))
    for c, r, exc in fan_out(lambda c: run_on(c, cmd, inventory, timeout), computes):
        if exc:
            checks.fail(f"{c.name}: {exc}")
            continue
        _judge(c.name, r, checks, allow_oom)
    checks.assert_all()


@pytest.mark.datapath(tools=["rccl_smoke"], min_computes=2)
@pytest.mark.tc("SMOKE-RCCL-MN")
def test_rccl_smoke_multi_node(datapath_ready, computes, inventory, checks):
    """One rank per GPU across all selected computes (cross-domain over the scale-up fabric)."""
    if len(computes) < 2:
        pytest.skip("multi-node smoke test needs >= 2 computes")
    binary = _bin(inventory)
    env, args = _cfg(inventory, "env", ""), _cfg(inventory, "args", "")
    port = int(_cfg(inventory, "port", random.randint(20000, 60000)))
    master = addr(computes[0])
    nranks = sum(c.gpus for c in computes)
    jobs, rank = [], 0
    for c in computes:
        for dev in range(c.gpus):
            parts = [f"NCCL_COMM_ID={master}:{port}", env, binary,
                     f"--nranks {nranks} --rank {rank} --device {dev}", args]
            jobs.append((c, " ".join(p for p in parts if p)))
            rank += 1
    results = run_ranks(jobs, float(_cfg(inventory, "timeout_s", 600)), sudo=use_sudo(inventory))
    for i, (node, r, exc) in enumerate(results[1:], start=1):
        checks.check(not exc and r.ok, f"rank {i} on {node.name} failed: {exc or f'rc={r.rc} {r.tail(300)!r}'}")
    node0, r0, exc0 = results[0]
    if exc0:
        checks.fail(f"rank 0 on {node0.name}: {exc0}")
    else:
        _judge(f"{nranks} ranks on {len(computes)} nodes (rank 0 {node0.name})", r0, checks,
               bool(_cfg(inventory, "allow_oom_skip", False)))
    checks.assert_all()
