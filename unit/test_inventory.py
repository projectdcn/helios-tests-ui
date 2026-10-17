from __future__ import annotations

import pytest

from helios.inventory import InventoryError, load_inventory


def test_defaults_env_secrets_and_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("SW_PW", "s3cret")
    p = tmp_path / "inv.yaml"
    p.write_text("""
rack: r1
defaults:
  common: {user: root}
  switch: {user: admin, password_env: SW_PW}
switches: [sw1, {name: sw2, host: 10.0.0.2}]
computes: [{name: c1, gpus: 4, gpu_addrs: [a, b, c, d]}]
thresholds: {switch: {expected_fans: null, interface_mtu: 9100}}
""")
    inv = load_inventory(p)
    assert [s.host for s in inv.switches] == ["sw1", "10.0.0.2"]
    assert inv.switches[0].user == "admin" and inv.switches[0].password == "s3cret"
    assert inv.computes[0].user == "root" and inv.computes[0].extra["gpus"] == 4
    assert inv.th("switch.interface_mtu") == 9100
    assert inv.th("switch.expected_fans", 12) is None  # explicit null disables the check
    assert inv.th("switch.missing", 7) == 7


def test_duplicate_names_rejected(tmp_path):
    p = tmp_path / "inv.yaml"
    p.write_text("switches: [sw1, sw1]\n")
    with pytest.raises(InventoryError):
        load_inventory(p)


def test_example_inventory_loads():
    from pathlib import Path

    inv = load_inventory(Path(__file__).resolve().parents[1] / "inventory" / "example-fullrack.yaml")
    assert len(inv.switches) == 6 and len(inv.computes) == 18


def test_missing_ssh_key_is_a_clear_error(tmp_path):
    """17 Oct: the MR3 inventory copied to the UI server named the laptop's key; every node showed only as
    'unreachable/unresponsive (FileNotFoundError ...)'. The error names the key, the node and this machine."""
    import pytest
    from helios.transport import key_path
    assert key_path(None, "c1") is None
    k = tmp_path / "id_test"
    k.write_text("x")
    assert key_path(str(k), "c1") == str(k)
    with pytest.raises(FileNotFoundError, match=r"SSH key .*id_missing \(inventory key_file of c1\) does not exist on "):
        key_path(str(tmp_path / "id_missing"), "c1")
