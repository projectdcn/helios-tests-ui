"""No test can change the admin state of a switch management interface (overnight disruptive runs on g02-g17
exclude anything that risks switch management): AfmOps.port_admin accepts EthernetN fabric ports only."""
from __future__ import annotations

import pytest

from helios.afm_ops import AfmOps


class _Sw:
    def __init__(self):
        self.cmds = []

    def run(self, cmd, sudo=False, timeout=None):
        self.cmds.append(cmd)
        return cmd


@pytest.mark.parametrize("port", ["eth0", "eth1", "mgmt0", "Ethernet", "Ethernet12; reboot", "Loopback0", ""])
def test_management_and_odd_names_refused(port):
    sw = _Sw()
    with pytest.raises(ValueError, match="not a fabric port"):
        AfmOps.port_admin(sw, "asic0", port, up=False)
    assert sw.cmds == []


def test_fabric_port_allowed():
    sw = _Sw()
    AfmOps.port_admin(sw, "asic1", "Ethernet128", up=False)
    AfmOps.port_admin(sw, None, "Ethernet0", up=True)
    assert sw.cmds == ["config interface -n asic1 shutdown Ethernet128", "config interface startup Ethernet0"]
