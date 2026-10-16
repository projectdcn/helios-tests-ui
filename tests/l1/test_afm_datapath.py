"""TC-AFM-001..004 + TC-AFM-005A — scale-up datapath smoke (L1).

Command lines come from ``commands.*`` templates in the inventory, because the
exact invocation (L2 ping tool, TransferBench preset/port-subset env, mpirun
flags) is site-specific. An unset template skips the test with a pointer to
the key, rather than guessing.
"""
from __future__ import annotations

import pytest

from helios import datapath

pytestmark = pytest.mark.l1


@pytest.mark.tc("TC-AFM-001")
def test_l2_ping_within_and_across_computes(datapath_ready, computes, inventory, checks, record_property):
    evidence: list[dict] = []
    record_property("pings", datapath.l2_ping(computes, inventory, checks, evidence))
    if evidence:            # each failed ping: exact command, host, time, rc and afmctl's full output
        record_property("ping_evidence", evidence[:40])
    checks.assert_all()


@pytest.mark.datapath(tools=["transferbench"])
@pytest.mark.tc("TC-AFM-002")
def test_transferbench_in_domain(datapath_ready, computes, inventory, checks, record_property):
    record_property("bw_gbps", datapath.transferbench_in_domain(computes, inventory, checks))
    checks.assert_all()


@pytest.mark.datapath(tools=["transferbench"], min_computes=2)
@pytest.mark.tc("TC-AFM-003")
def test_transferbench_cross_domain(datapath_ready, computes, inventory, checks, record_property):
    record_property("bw_gbps", datapath.transferbench_cross_domain(computes, inventory, checks))
    checks.assert_all()


@pytest.mark.datapath(tools=["rccl_tests"])
@pytest.mark.tc("TC-AFM-004")
def test_rccl_in_domain(datapath_ready, computes, collective, inventory, checks, record_property):
    record_property("avg_busbw_gbps", datapath.rccl_in_domain(computes, inventory, checks, collective))
    checks.assert_all()


@pytest.mark.datapath(tools=["rccl_tests"], min_computes=2)
@pytest.mark.tc("TC-AFM-005A")
def test_rccl_cross_domain(datapath_ready, computes, collective, inventory, checks, record_property):
    """Plan row 'TC-AFM-005 RCCL Cross Domain' (L1) — renumbered 005A because
    TC-AFM-005 is the L2 failover-policy CRUD case in the afm suite."""
    record_property("avg_busbw_gbps", datapath.rccl_cross_domain(computes, inventory, checks, collective))
    checks.assert_all()
