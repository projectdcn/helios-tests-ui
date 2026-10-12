"""TC-HW — hardware checks that need only switch/compute CLI access."""
from __future__ import annotations

import pytest

from helios import datapath
from helios.parsers import parse_speed_gbps

pytestmark = pytest.mark.l3


@pytest.mark.tc("TC-HW-002")
def test_interface_speed_matches_config(switch, checks):
    """Oper speed (STATE_DB PORT_TABLE) equals configured speed (CONFIG_DB PORT) on oper-up ports.
    Falls back to the CLI speed column if this image doesn't publish oper speed."""
    for ns in switch.namespaces():
        status = {r["Interface"]: r for r in switch.interfaces_status(ns)}
        speeds = switch.port_speeds(ns)
        checks.check(speeds, f"{switch.name}/{ns}: no PORT entries in CONFIG_DB")
        for port, (cfg, oper) in speeds.items():
            row = status.get(port)
            if not row or row.get("Oper", "").lower() != "up":
                continue
            got = parse_speed_gbps(oper) if oper else parse_speed_gbps(row.get("Speed", ""))
            want = parse_speed_gbps(cfg)
            checks.check(want is not None and got == want,
                         f"{switch.name}/{ns}: {port} oper {got}G != configured {want}G")
    checks.assert_all()


@pytest.mark.tc("TC-HW-008")
def test_transferbench_smoke_a2a_in_domain(computes, inventory, checks, record_property):
    size = inventory.th("datapath.transferbench.smoke_size", "64M")
    record_property("bw_gbps", datapath.transferbench_in_domain(computes, inventory, checks, size=size))
    checks.assert_all()
