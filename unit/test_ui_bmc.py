"""BMC passwords from the UI (20 Oct): a rack default (Inventory tab) and per node (Setup node table - a switch and a
tray may differ); runs get HELIOS_BMC_PASSWORD / HELIOS_BMC_PASSWORD_<NODE>, never in a log; helios.compute.bmc_cmd
makes a node's BMC command use its own variable."""
from __future__ import annotations

import sys
import threading

import pytest
import yaml

from helios.compute import bmc_cmd, bmc_env
from ui.config import RackProfile
from ui.keychain import FileSecrets
from ui.runner import Executor
from ui.server import App, make_server

from test_ui_keychain import Client

DEFAULT, ST1 = "Bmc-Default-Pw1", "Bmc-St1-Pw-22"


class EnvExecutor(Executor):
    """Stand-in run: prints which BMC variables it got and their values (the log must redact the values)."""

    def popen(self, argv, env=None, cwd=None, stdin=False):
        code = ("import os; ks = sorted(k for k in os.environ if k.startswith('HELIOS_BMC_PASSWORD')); "
                "print('vars', ','.join(ks), flush=True); [print('val', os.environ[k], flush=True) for k in ks]")
        return super().popen([sys.executable, "-c", code], env=env, cwd=cwd, stdin=stdin)


@pytest.fixture
def served(tmp_path):
    inv = tmp_path / "mr3.yaml"
    inv.write_text(yaml.safe_dump({"rack": "mr3", "switches": [{"name": "st1", "host": "10.153.80.52", "bmc": "10.153.80.84"}],
                                   "computes": [{"name": "c1", "host": "10.153.80.81", "bmc": "10.153.80.82"},
                                                {"name": "c2", "host": "10.153.80.86", "bmc": "10.153.80.87"}]}))
    p = RackProfile(name="mr3", title="MR3", runner="local", inventory=inv, results_dir=tmp_path / "res")
    app = App(profiles={"mr3": p}, executor=EnvExecutor(), keychain=FileSecrets(tmp_path / "cfg" / "secrets.json"))
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Client(srv.server_address[1])
    app.shutdown()
    srv.shutdown()
    srv.server_close()


def test_default_and_per_node_bmc_passwords_reach_the_run_not_the_log(served):
    c = served
    st, d = c.j("GET", "/api/racks/mr3/bmc-passwords")
    assert st == 200 and [(n["name"], n["bmc"], n["saved"]) for n in d["nodes"]] == [
        ("st1", "10.153.80.84", False), ("c1", "10.153.80.82", False), ("c2", "10.153.80.87", False)]
    assert c.j("POST", "/api/racks/mr3/bmc-password", {"password": DEFAULT})[0] == 200
    st, d = c.j("POST", "/api/racks/mr3/nodes/st1/bmc-password", {"password": ST1})
    assert st == 200 and [n["saved"] for n in d["nodes"]] == [True, False, False] and d["default"]["saved"]
    assert DEFAULT not in str(d) and ST1 not in str(d)
    assert c.j("POST", "/api/racks/mr3/nodes/evil/bmc-password", {"password": "x" * 8})[0] == 404
    st, run = c.j("POST", "/api/runs", {"rack": "mr3", "kind": "health"})
    assert st == 201
    done = c.wait(run["id"])
    log = c.log(run["id"])
    assert "vars HELIOS_BMC_PASSWORD,HELIOS_BMC_PASSWORD_ST1" in log, log
    assert DEFAULT not in log and ST1 not in log and done["has_bmc_password"]
    assert "BMC password(s) given (memory only): default, st1" in log
    # forget st1's own: only the default is handed over
    c.j("POST", "/api/racks/mr3/nodes/st1/bmc-password/delete", {})
    run = c.j("POST", "/api/runs", {"rack": "mr3", "kind": "health"})[1]
    c.wait(run["id"])
    assert "vars HELIOS_BMC_PASSWORD\n" in c.log(run["id"]) + "\n"


def test_bmc_cmd_uses_the_nodes_own_variable_only_when_there_is_one():
    cmd = 'IPMI_PASSWORD="$HELIOS_BMC_PASSWORD" ipmitool -I lanplus -H 10.153.80.84 -U admin -E chassis power status'
    assert bmc_env("st1") == "HELIOS_BMC_PASSWORD_ST1" and bmc_env("ct-05.a") == "HELIOS_BMC_PASSWORD_CT_05_A"
    assert bmc_cmd(cmd, "st1", {"HELIOS_BMC_PASSWORD_ST1": "x"}) == cmd.replace("$HELIOS_BMC_PASSWORD", "$HELIOS_BMC_PASSWORD_ST1")
    assert bmc_cmd(cmd, "c1", {"HELIOS_BMC_PASSWORD_ST1": "x"}) == cmd
    assert bmc_cmd("X=${HELIOS_BMC_PASSWORD} y", "st1", {"HELIOS_BMC_PASSWORD_ST1": "x"}) == "X=$HELIOS_BMC_PASSWORD_ST1 y"
    assert bmc_cmd("$HELIOS_BMC_PASSWORD_OTHER", "st1", {"HELIOS_BMC_PASSWORD_ST1": "x"}) == "$HELIOS_BMC_PASSWORD_OTHER"


def test_bmc_address_and_user_go_into_the_inventory_yaml_with_a_backup(served, tmp_path):
    """Setup node table: st1 had no bmc address (20 Oct, MR3: CPU-BMC 10.153.80.84) - saved into its inventory
    entry, comments elsewhere kept, the old file backed up; passwords never go there."""
    c = served
    inv = tmp_path / "mr3.yaml"
    inv.write_text("# MR3 mini rack\n" + inv.read_text())
    st, d = c.j("POST", "/api/racks/mr3/nodes/st1/bmc", {"bmc": "10.153.80.84", "bmc_user": "admin"})
    assert st == 200 and next(n for n in d["nodes"] if n["name"] == "st1")["bmc"] == "10.153.80.84"
    data = yaml.safe_load(inv.read_text())
    assert data["switches"][0]["bmc"] == "10.153.80.84" and data["switches"][0]["bmc_user"] == "admin"
    assert inv.read_text().startswith("# MR3 mini rack") and list(tmp_path.glob("mr3.yaml.bak-*"))
    assert c.j("POST", "/api/racks/mr3/nodes/st1/bmc", {"bmc": "10.0.0.1; rm -rf /"})[0] == 400
    assert c.j("POST", "/api/racks/mr3/nodes/st1/bmc", {"bmc_password": "x"})[0] == 400
    st, d = c.j("POST", "/api/racks/mr3/nodes/st1/bmc", {"bmc_user": ""})          # empty = removed
    assert st == 200 and "bmc_user" not in yaml.safe_load(inv.read_text())["switches"][0]
