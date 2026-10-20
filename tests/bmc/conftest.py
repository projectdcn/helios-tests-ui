"""Fixtures of the Blanton switch-BMC cases (TC-BLT-*, Juniper TPI-151974 "E-RLI42381 - Blantons: BMC support").

``switch_bmcs``: one read-only Redfish client per SWITCH whose inventory entry has ``bmc`` + ``bmc_user`` and a BMC
password ($HELIOS_BMC_PASSWORD_<NODE> or $HELIOS_BMC_PASSWORD - the UI saves them); switches without them are
recorded as not checked. ``arim``: the rack-level Redfish aggregator of section 7.5, only when the inventory has
``arim: {url, user}`` and $HELIOS_ARIM_PASSWORD is set - else those cases skip.
"""
from __future__ import annotations

import os

import pytest

from helios import bmc as B


@pytest.fixture(scope="session")
def switch_bmcs(inventory):
    th = lambda k, d: inventory.th(f"bmc.{k}", d)      # noqa: E731
    out, skipped = [], {}
    for h in inventory.switches:
        addr, user, pw, why = B.credentials(h)
        if why:
            skipped[h.name] = why
            continue
        out.append({"name": h.name, "address": addr, "rf": B.Redfish(addr, user, pw, timeout=float(th("timeout_s", 15)),
                                                                   verify=bool(th("verify_tls", False)))})
    yield {"bmcs": out, "not_checked": skipped}
    for b in out:
        b["rf"].close()


@pytest.fixture
def each_switch_bmc(switch_bmcs, record_property):
    """The switch BMCs to check (skips when there is none), with what was not checked recorded."""
    record_property("bmc_not_checked", switch_bmcs["not_checked"])
    if not switch_bmcs["bmcs"]:
        why = "; ".join(f"{n}: {w}" for n, w in switch_bmcs["not_checked"].items()) or "no switch in the inventory"
        pytest.skip(f"no switch BMC checkable: {why}")
    return switch_bmcs["bmcs"]


@pytest.fixture(scope="session")
def arim(inventory):
    import yaml
    cfg = (yaml.safe_load(inventory.path.read_text()) or {}).get("arim")
    if not isinstance(cfg, dict) or not cfg.get("url"):
        pytest.skip("no ARIM (rack-level Redfish aggregator) in the inventory: add arim: {url, user} to run section 7.5")
    pw = os.environ.get(str(cfg.get("password_env") or "HELIOS_ARIM_PASSWORD"), "")
    if not cfg.get("user") or not pw:
        pytest.skip("ARIM user / password missing: arim.user in the inventory, $HELIOS_ARIM_PASSWORD")
    rf = B.Redfish(str(cfg["url"]), str(cfg["user"]), pw, timeout=float(inventory.th("bmc.timeout_s", 15)),
                   verify=bool(cfg.get("verify_tls", False)))
    yield rf
    rf.close()
