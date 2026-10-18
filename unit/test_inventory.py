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


def test_ssh_key_lookup_order(tmp_path, monkeypatch):
    """17 Oct: an inventory naming one laptop's key file failed on the UI server. Order now: $HELIOS_SSH_KEY (this
    machine's key), the inventory's key_file, this machine's ~/.ssh/config IdentityFile, then agent + default keys."""
    import pytest
    from helios.transport import key_path, resolve_keys
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HELIOS_SSH_KEY", raising=False)
    inv_key, cfg_key, env_key = (home / ".ssh" / n for n in ("id_inv", "id_cfg", "id_env"))
    for k in (inv_key, cfg_key, env_key):
        k.write_text("x")
    assert resolve_keys(None, "c1", "10.0.0.1") == (None, "default SSH keys / ssh-agent")
    (home / ".ssh" / "config").write_text("Host 10.0.0.*\n  IdentityFile ~/.ssh/id_cfg\n")
    assert resolve_keys(None, "c1", "10.0.0.1") == ([str(cfg_key)], "~/.ssh/config")
    assert resolve_keys(None, "c1", "192.0.2.9") == (None, "default SSH keys / ssh-agent")
    assert resolve_keys("~/.ssh/id_inv", "c1", "10.0.0.1") == ([str(inv_key)], "inventory key_file")
    with pytest.raises(FileNotFoundError, match=r"id_missing \(inventory key_file of c1\) does not exist on .*HELIOS_SSH_KEY"):
        resolve_keys("~/.ssh/id_missing", "c1", "10.0.0.1")
    monkeypatch.setenv("HELIOS_SSH_KEY", "~/.ssh/id_env")           # wins, even over a key_file that is missing here
    assert resolve_keys("~/.ssh/id_missing", "c1", "10.0.0.1") == ([str(env_key)], "$HELIOS_SSH_KEY")
    assert key_path("~/.ssh/id_missing", "c1", "10.0.0.1") == str(env_key)
    monkeypatch.setenv("HELIOS_SSH_KEY", "~/.ssh/nope")
    with pytest.raises(FileNotFoundError, match=r"\$HELIOS_SSH_KEY\) does not exist"):
        resolve_keys(None, "c1", "10.0.0.1")
