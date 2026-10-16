"""Quarantine helpers shared by the switch-side checks (see helios/inventory.py for the inventory side)."""
from __future__ import annotations

from .afmctl import parse_port_description


def quarantined_ports(switch, ns, inventory) -> set[str]:
    """Ports of one switch namespace whose cabling description ('To ComputeTray Slot<s>/...') names a compute slot
    quarantined in the inventory - left out of the port checks."""
    slots = inventory.quarantined_slots()
    if not slots:
        return set()
    return {p for p, d in switch.port_descriptions(ns).items() if (cab := parse_port_description(d)) and cab[0] in slots}
