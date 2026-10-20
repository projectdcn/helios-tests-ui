"""UI inventory editor: validation (loader, uniqueness, no secrets), dated backup + diff before save, form round
trip, and the per-node quarantine toggle read back by helios.inventory."""
from __future__ import annotations

import pytest
import yaml

from helios.inventory import load_inventory
from ui import inventory_edit as ie

INV = """\
# MR3-like inventory (comments must survive a quarantine toggle)
rack: mini
transport:
  kind: ssh
defaults:
  switch: {user: admin}
switches:
  - name: st1          # the only switch
    host: 10.0.0.1
computes:
  - name: ct01
    host: 10.0.0.11
    slot: 0
    gpus: 4
  - {name: ct02, host: 10.0.0.12, slot: 1}
  - name: ct03
    host: 10.0.0.13
    quarantine:
      reason: old reason
      since: '2026-10-01'
afm:
  url: https://10.0.0.5
  auth: {username: admin, password_env: HELIOS_AFM_PASSWORD}
thresholds:
  switch:
    interface_mtu: 9100   # QFX5252
"""


@pytest.fixture
def inv(tmp_path):
    f = tmp_path / "mr3.local.yaml"
    f.write_text(INV)
    return f


def test_valid_inventory_reads_with_form_and_quarantine(inv):
    d = ie.read(inv)
    assert d["errors"] == [] and d["sha"] == ie.sha(INV)
    assert [r["name"] for r in d["form"]["computes"]] == ["ct01", "ct02", "ct03"]
    assert d["form"]["computes"][2]["quarantine"]["reason"] == "old reason"
    assert d["quarantine"]["ct03"]["reason"] == "old reason"


@pytest.mark.parametrize("edit, msg", [
    (lambda t: t.replace("password_env: HELIOS_AFM_PASSWORD", "password: hunter2"), "afm.auth.password: secret"),
    (lambda t: t.replace("  switch: {user: admin}", "  switch: {user: admin, password: x}"), "defaults.switch.password"),
    (lambda t: t.replace("name: ct02", "name: ct01"), "duplicate name 'ct01'"),
    (lambda t: t.replace("host: 10.0.0.12", "host: 10.0.0.11"), "duplicate host / IP '10.0.0.11'"),
    (lambda t: t.replace("slot: 1}", "slot: 0}"), "duplicate compute slot 0"),
    (lambda t: t.replace("name: st1          # the only switch", "host_only: st1"), "switches[0]: name is required"),
    (lambda t: t.replace("reason: old reason", "reason: ''"), "ct03: quarantine needs a reason"),
    (lambda t: t.replace("thresholds:\n  switch:", "thresholds: [1]\nx:\n  switch:"), "thresholds: must be a mapping"),
    (lambda t: t + "  - broken: [\n", "YAML"),
    (lambda t: t.replace("    gpus: 4", "    gpus: four"), "ct01: gpus must be a number"),
])
def test_validation_rejects(inv, edit, msg):
    errors, _, _ = ie.validate(edit(INV), inv)
    assert any(msg in e for e in errors), errors


def test_loader_error_is_reported(inv):
    errors, _, _ = ie.validate(INV.replace("rack: mini", "rack: mini\nswitches_extra: 1").replace(
        "switches:\n  - name: st1", "switches:\n  - st1\n  - name: st1"), inv)
    assert any("duplicate" in e for e in errors)


def test_save_writes_backup_and_diff_and_checks_sha(inv):
    new = INV.replace("interface_mtu: 9100", "interface_mtu: 9000")
    pre = ie.prepare(inv, {"text": new})
    assert pre["errors"] == [] and "-    interface_mtu: 9100" in pre["diff"] and "+    interface_mtu: 9000" in pre["diff"]
    assert inv.read_text() == INV                       # a preview does not write
    with pytest.raises(ie.EditError, match="changed since"):
        ie.save(inv, {"text": new, "sha": "0" * 64})
    res = ie.save(inv, {"text": new, "sha": ie.sha(INV)})
    assert res["saved"] and inv.read_text() == new
    backups = list(inv.parent.glob("mr3.local.yaml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == INV
    with pytest.raises(ie.EditError, match="secret"):
        ie.save(inv, {"text": new.replace("password_env: HELIOS_AFM_PASSWORD", "password: x"), "sha": ie.sha(new)})
    assert inv.read_text() == new and len(list(inv.parent.glob("mr3.local.yaml.bak-*"))) == 1


def test_form_round_trip_and_edit(inv):
    data = yaml.safe_load(INV)
    form = ie.to_form(data)
    assert ie.from_form(data, form) == {**data, "switches": data["switches"],
                                        "computes": [data["computes"][0], data["computes"][1], data["computes"][2]]}
    form["computes"].append({"name": "ct04", "host": "10.0.0.14", "slot": "3", "gpus": "4", "extra_yaml": "{bmc_user: x}"})
    form["thresholds_yaml"] = "switch: {interface_mtu: 9000}\n"
    out = ie.from_form(data, form)
    assert out["computes"][3] == {"name": "ct04", "host": "10.0.0.14", "gpus": 4, "slot": 3, "bmc_user": "x"}
    assert out["thresholds"] == {"switch": {"interface_mtu": 9000}} and out["afm"] == data["afm"]
    res = ie.save(inv, {"form": form, "sha": ie.sha(INV)})
    assert res["saved"] and not res["comments_kept"]
    assert [h.name for h in load_inventory(inv).computes] == ["ct01", "ct02", "ct04"]
    form["switches"][0]["extra_yaml"] = "{password: x}"
    assert any("secret" in e for e in ie.prepare(inv, {"form": form})["errors"])


@pytest.mark.parametrize("node", ["st1", "ct01", "ct02"])
def test_quarantine_toggle_round_trip(inv, node):
    res = ie.save(inv, {"quarantine": {"node": node, "enabled": True, "reason": "PSU swap, AMD-123",
                                        "until": "2099-01-31"}, "sha": ie.sha(INV)})
    assert res["saved"] and res["comments_kept"]
    text = inv.read_text()
    assert "# the only switch" in text and "# QFX5252" in text
    i = load_inventory(inv)
    assert node in {h.name for h in i.quarantined} and node not in {h.name for h in i.switches + i.computes}
    q = i.quarantine_summary()[node]
    assert q["reason"] == "PSU swap, AMD-123" and q["until"] == "2099-01-31" and q["since"] and q["expired"] is False
    res = ie.save(inv, {"quarantine": {"node": node, "enabled": False}, "sha": ie.sha(text)})
    assert inv.read_text() == INV                       # lifting it restores the file exactly
    assert len(list(inv.parent.glob("mr3.local.yaml.bak-*"))) == 2


def test_quarantine_replaces_a_nested_entry_and_validates(inv):
    new, kept = ie.set_quarantine(INV, "ct03", True, "new reason")
    assert kept and "old reason" not in new and "  # QFX5252" in new
    assert load_inventory_text(inv, new).quarantine_summary()["ct03"]["reason"] == "new reason"
    new, kept = ie.set_quarantine(INV, "ct03", False)
    assert kept and "quarantine:" not in new
    with pytest.raises(ie.EditError, match="reason"):
        ie.set_quarantine(INV, "ct01", True, "  ")
    with pytest.raises(ie.EditError, match="past"):
        ie.set_quarantine(INV, "ct01", True, "x", until="2000-01-01")
    with pytest.raises(ie.EditError, match="no switch / compute"):
        ie.set_quarantine(INV, "ct99", True, "x")


def test_quarantine_on_compute_without_slot_is_refused(inv):
    """g02-g17 17 Oct: a quarantine saved without a slot left the AFM pre-flight blocking on that tray."""
    text = INV.replace("    slot: 0\n", "")
    inv.write_text(text)
    pre = ie.prepare(inv, {"quarantine": {"node": "ct01", "enabled": True, "reason": "down"}})
    assert any("needs its AFM 'slot'" in e for e in pre["errors"])
    pre = ie.prepare(inv, {"quarantine": {"node": "ct01", "enabled": False}})     # lifting it is always allowed
    assert not any("needs its AFM 'slot'" in e for e in pre["errors"])


def load_inventory_text(inv, text):
    f = inv.with_name("check.yaml")
    f.write_text(text)
    return load_inventory(f)


def test_new_rack_without_inventory_file(tmp_path):
    """Starting from scratch (17 Oct): no inventory file yet - the editor got no form (so the switch / compute
    tables had no column headers) and Save failed. Now: an empty form, and Save creates the file (no backup)."""
    f = tmp_path / "inventories" / "new.local.yaml"
    d = ie.read(f)
    assert d["exists"] is False and d["text"] == "" and d["sha"] == ie.sha("")
    assert d["form"]["switches"] == [] and d["form"]["computes"] == []
    form = {**d["form"], "rack": "new", "switches": [{"name": "st1", "host": "10.0.0.1"}],
            "computes": [{"name": "ct01", "host": "10.0.0.11", "slot": "0"}]}
    res = ie.save(f, {"form": form, "sha": d["sha"]})
    assert res["saved"] is True and res["backup"] is None and f.is_file()
    assert [s.name for s in load_inventory(f).switches] == ["st1"]
    assert list(f.parent.glob("*.bak-*")) == []


def test_form_save_keeps_comments_of_unchanged_parts(inv):
    """17 Oct: every form save re-wrote the whole YAML and dropped the comments. Now only changed parts are."""
    form = ie.to_form(yaml.safe_load(INV))
    form["computes"].append({"name": "ct04", "host": "10.0.0.14", "slot": "3"})
    form["computes"][1]["gpus"] = "8"                          # ct02 (flow entry, no comment) changes
    res = ie.prepare(inv, {"form": form})
    assert res["errors"] == [] and res["comments_kept"] is True
    text = res["text"]
    for c in ("# MR3-like inventory", "# the only switch", "# QFX5252"):
        assert c in text
    assert text.startswith(INV.split("  - {name: ct02")[0])   # everything above ct02 untouched
    data = yaml.safe_load(text)
    assert [e["name"] for e in data["computes"]] == ["ct01", "ct02", "ct03", "ct04"] and data["computes"][1]["gpus"] == 8
    assert data["computes"][2]["quarantine"]["reason"] == "old reason"
    # a changed section with a comment in it: re-written, and the preview says so
    form["thresholds_yaml"] = "switch: {interface_mtu: 9000}\n"
    res = ie.prepare(inv, {"form": form})
    assert res["comments_kept"] is False and "# QFX5252" not in res["text"] and "# the only switch" in res["text"]
    assert any("changed thresholds" in w for w in res["warnings"])
    # removing a node leaves the other entries' text alone
    form = ie.to_form(yaml.safe_load(INV))
    form["computes"] = [r for r in form["computes"] if r["name"] != "ct02"]
    res = ie.prepare(inv, {"form": form})
    assert res["comments_kept"] is True and "ct02" not in res["text"] and "# the only switch" in res["text"]


def test_form_save_with_anchors_rewrites_whole_file(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text("# top\nrack: a\ndefaults:\n  switch: &sw {user: admin}\nswitches:\n  - {name: s1, host: 10.0.0.1}\n")
    form = ie.to_form(yaml.safe_load(f.read_text()))
    form["rack"] = "b"
    res = ie.prepare(f, {"form": form})
    assert res["comments_kept"] is False and "# top" not in res["text"] and yaml.safe_load(res["text"])["rack"] == "b"


def test_key_file_missing_on_this_server_is_a_warning(tmp_path):
    """An inventory imported from a laptop names the laptop's key paths (19 Oct: ~/.ssh/id_ed25519_mymdbook)."""
    from ui.inventory_edit import validate
    key = tmp_path / "id_here"
    key.write_text("x")
    text = ("rack: r\nswitches:\n  - {name: st1, host: 10.0.0.1, user: admin, key_file: /nope/id_laptop}\n"
            f"computes:\n  - {{name: c1, host: 10.0.0.2, user: u, key_file: {key}}}\n")
    errors, warnings, _ = validate(text, tmp_path / "inv.yaml")
    assert errors == []
    assert [w for w in warnings if "key_file" in w] == [
        "st1: key_file /nope/id_laptop does not exist on this server - SSH falls back to the default keys; fix the "
        "path or remove it (Setup tab: the server's keys)"]
