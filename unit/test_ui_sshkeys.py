"""Setup tab: the UI server's SSH key per rack - create, choose (runs use it), install on the rack with a one-time
password, never the private key or the password in a response (17 Oct: the UI server had no key for MR3 and the only
fix was a shell on the server)."""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
import yaml

from ui import sshkeys
from ui.config import RackProfile, SshProfile
from ui.runner import JobError, LocalRunner, SshRunner
from ui.server import App, make_server

from test_ui_keychain import Client, HashExecutor

PW = "Node-Pa55!"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("HELIOS_SSH_KEY", raising=False)
    return h


def _profile(tmp_path, runner="local"):
    inv = tmp_path / "rk.yaml"
    inv.write_text(yaml.safe_dump({"rack": "rk", "defaults": {"switch": {"user": "admin"}, "compute": {"user": "ops"}},
                                   "switches": [{"name": "st1", "host": "10.0.0.1"}],
                                   "computes": [{"name": "c1", "host": "10.0.0.11"}, {"name": "c2", "host": "10.0.0.12"}]}))
    ssh = SshProfile(host="jump.example", port=8059, user="tenant") if runner == "ssh" else None
    return RackProfile(name="rk", title="rk", runner=runner, inventory=inv, results_dir=tmp_path / "res", ssh=ssh)


def _fake_key(home, name="id_test"):
    k = home / ".ssh" / name
    k.write_text("PRIVATE KEY - must never be sent\n")
    Path(str(k) + ".pub").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample helios-ui@test\n")
    return k


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="no ssh-keygen")
def test_create_list_and_choose(home, tmp_path):
    p = _profile(tmp_path)
    k = sshkeys.create("id_ed25519_helios")
    assert k["path"] == str(home / ".ssh" / "id_ed25519_helios") and k["type"] == "ssh-ed25519"
    assert (home / ".ssh" / "id_ed25519_helios").stat().st_mode & 0o077 == 0
    assert sshkeys.create("id_ed25519_helios")["pub"] == k["pub"]          # existing key kept, not overwritten
    assert [x["path"] for x in sshkeys.list_keys()] == [k["path"]]
    with pytest.raises(JobError):
        sshkeys.create("../evil")
    assert sshkeys.chosen(p) is None
    sshkeys.choose(p, k["path"])
    assert sshkeys.chosen(p) == k["path"] and sshkeys.using(p)["source"] == "chosen here"
    with pytest.raises(JobError, match="not a key of this server"):
        sshkeys.choose(p, "/etc/passwd")
    sshkeys.choose(p, None)
    assert sshkeys.chosen(p) is None


def test_runs_use_the_chosen_key(home, tmp_path):
    k = _fake_key(home)
    p = _profile(tmp_path)
    sshkeys.choose(p, str(k))
    seen = {}

    class Rec(HashExecutor):
        def popen(self, argv, env=None, cwd=None, stdin=False):
            seen.update(env or {})
            return super().popen(argv, env=env, cwd=cwd, stdin=stdin)
    from ui.runner import Job
    LocalRunner(p, Rec()).execute("r1", Job.from_request({"kind": "access"}), None, lambda line: None)
    assert seen["HELIOS_SSH_KEY"] == str(k) and "chosen in the UI" in seen["HELIOS_SSH_KEY_FROM"]
    pj = _profile(tmp_path, "ssh")
    sshkeys.choose(pj, str(k))
    argv = SshRunner(pj, HashExecutor()).ssh()
    assert argv[argv.index("-i") + 1] == str(k) and "IdentitiesOnly=yes" in argv


def test_targets_direct_and_jump(tmp_path):
    assert [(t["name"], t["user"]) for t in sshkeys.targets(_profile(tmp_path))] == \
        [("st1", "admin"), ("c1", "ops"), ("c2", "ops")]
    assert sshkeys.targets(_profile(tmp_path, "ssh")) == \
        [{"name": "jump host", "role": "jump", "user": "tenant", "host": "jump.example", "port": 8059}]


class FakeNodes:
    """paramiko stand-in: per host an authorized_keys list and an account password."""

    def __init__(self, passwords):
        self.keys = {h: [] for h in passwords}
        self.passwords = passwords
        self.commands = []

    def connect(self, t, *, password=None, key=None):
        nodes = self

        class Auth(Exception):
            pass
        Auth.__name__ = "AuthenticationException"
        if key is not None:
            if not any(k in nodes.keys[t["host"]] for k in [Path(key + ".pub").read_text().strip()]):
                raise Auth("Authentication failed.")
        elif password != nodes.passwords[t["host"]]:
            raise Auth("Authentication failed.")

        class Chan:
            def __init__(self, rc):
                self.rc = rc

            def recv_exit_status(self):
                return self.rc

        class Out:
            def __init__(self, rc):
                self.channel = Chan(rc)

            def read(self):
                return b""

        class C:
            def exec_command(self, cmd, timeout=None):
                nodes.commands.append((t["host"], cmd))
                if "authorized_keys" in cmd:
                    pub = Path(next(iter(Path.home().glob(".ssh/*.pub")))).read_text().strip()
                    if pub not in nodes.keys[t["host"]]:
                        nodes.keys[t["host"]].append(pub)
                return None, Out(0), Out(0)

            def close(self):
                pass
        return C()


def test_install_with_one_time_passwords(home, tmp_path, monkeypatch):
    k = _fake_key(home)
    p = _profile(tmp_path)
    nodes = FakeNodes({"10.0.0.1": "sw-pw", "10.0.0.11": PW, "10.0.0.12": PW})
    nodes.keys["10.0.0.12"].append(Path(str(k) + ".pub").read_text().strip())     # c2 already has it
    monkeypatch.setattr(sshkeys, "_connect", nodes.connect)
    monkeypatch.setattr(sshkeys, "_auth_methods", lambda t: ["publickey", "password"])
    res = {r["name"]: r for r in sshkeys.install(p, str(k), {"admin": "wrong", "ops": PW})}
    assert res["c2"]["status"] == "already"
    assert res["c1"]["status"] == "installed" and "key login works" in res["c1"]["detail"]
    assert res["st1"]["status"] == "failed" and "refused this password" in res["st1"]["detail"]
    assert "ssh -o PubkeyAuthentication=no admin@10.0.0.1" in res["st1"]["detail"]
    assert all(PW not in json.dumps(r) for r in res.values())
    res = {r["name"]: r for r in sshkeys.install(p, str(k), {"admin": "sw-pw"})}
    assert res["st1"]["status"] == "installed" and res["c1"]["status"] == "already"
    assert all(len(v) == 1 for v in nodes.keys.values())                 # never added twice
    assert all("grep -qxF" in c for _, c in nodes.commands if "authorized_keys" in c)   # append only if missing
    with pytest.raises(JobError, match="not a key of this server"):
        sshkeys.install(p, "/etc/hostname", {})


def test_api_never_returns_a_private_key_or_password(home, tmp_path, monkeypatch):
    k = _fake_key(home)
    p = _profile(tmp_path)
    nodes = FakeNodes({"10.0.0.1": PW, "10.0.0.11": PW, "10.0.0.12": PW})
    monkeypatch.setattr(sshkeys, "_connect", nodes.connect)
    app = App(profiles={"rk": p}, executor=HashExecutor())
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(srv.server_address[1])
        st, raw = c.req("GET", "/api/racks/rk/sshkey")
        d = json.loads(raw)
        assert st == 200 and b"PRIVATE KEY" not in raw and d["keys"][0]["pub"].startswith("ssh-ed25519 ")
        assert [t["name"] for t in d["targets"]] == ["st1", "c1", "c2"] and d["chosen"] is None
        assert c.j("POST", "/api/racks/rk/sshkey/use", {"key": str(k)})[1]["chosen"] == str(k)
        st, raw = c.req("POST", "/api/racks/rk/sshkey/install", {"passwords": {"admin": PW, "ops": PW}})
        assert st == 200 and PW.encode() not in raw and b"PRIVATE KEY" not in raw
        assert {r["status"] for r in json.loads(raw)["results"]} == {"installed"}
        assert c.j("POST", "/api/racks/rk/sshkey/use", {"key": "/etc/passwd"})[0] == 400
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()


def test_account_without_password_login_says_add_by_hand(home, tmp_path, monkeypatch):
    k = _fake_key(home)
    p = _profile(tmp_path)
    nodes = FakeNodes({"10.0.0.1": PW, "10.0.0.11": PW, "10.0.0.12": PW})
    monkeypatch.setattr(sshkeys, "_connect", nodes.connect)
    monkeypatch.setattr(sshkeys, "_auth_methods", lambda t: ["publickey"])
    r = sshkeys.install(p, str(k), {"admin": "x"}, ["st1"])[0]
    assert r["status"] == "failed" and "does not accept password logins (it allows: publickey)" in r["detail"]


def test_changed_host_key_is_explained_with_fingerprints_and_the_fix():
    """20 Oct srv03 -> MR3 c2: BadHostKeyException showed two cut-off base64 keys and no way forward."""
    import base64
    import paramiko
    from helios.transport import host_key_changed
    new = paramiko.PKey.from_type_string("ssh-ed25519", base64.b64decode(
        "AAAAC3NzaC1lZDI1NTE5AAAAIGqtClRGRGPWIpY6/6PBTt3a6xjcRKnmDxCQvN6vWnNb"))
    old = paramiko.PKey.from_type_string("ssh-ed25519", base64.b64decode(
        "AAAAC3NzaC1lZDI1NTE5AAAAIC9g2ecSennknFqf8DNIJc6Km1L959UpCZl1T9X7fLFh"))
    msg = host_key_changed(paramiko.BadHostKeyException("10.153.80.86", new, old), "10.153.80.86")
    assert "SHA256:6ab9qp9Bc4+j7fQpVpsOwvqsObZqugjlHn6JiqLyl1w" in msg and "ssh-keygen -R 10.153.80.86" in msg
    assert host_key_changed(OSError("x"), "h") is None


def test_a_node_password_from_its_own_row_wins_and_installs_only_that_node(home, tmp_path, monkeypatch):
    """Node table: one password per row (each tray its own), Install on one row = that node only."""
    k = _fake_key(home)
    p = _profile(tmp_path)
    nodes = FakeNodes({"10.0.0.1": "sw-pw", "10.0.0.11": "c1-pw", "10.0.0.12": PW})
    monkeypatch.setattr(sshkeys, "_connect", nodes.connect)
    monkeypatch.setattr(sshkeys, "_auth_methods", lambda t: ["publickey", "password"])
    res = sshkeys.install(p, str(k), {"ops": PW}, ["c1"], {"c1": "c1-pw"})
    assert [(r["name"], r["status"]) for r in res] == [("c1", "installed")]
    res = {r["name"]: r for r in sshkeys.install(p, str(k), {"ops": PW}, None, {"st1": "wrong"})}
    assert res["c2"]["status"] == "installed" and res["c1"]["status"] == "already"
    assert res["st1"]["status"] == "failed"
    res = {r["name"]: r for r in sshkeys.install(p, str(k), {}, ["st1"])}
    assert "type it in this row" in res["st1"]["detail"]


C2_NEW = "AAAAC3NzaC1lZDI1NTE5AAAAIGqtClRGRGPWIpY6/6PBTt3a6xjcRKnmDxCQvN6vWnNb"
C2_OLD = "AAAAC3NzaC1lZDI1NTE5AAAAIC9g2ecSennknFqf8DNIJc6Km1L959UpCZl1T9X7fLFh"


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="no ssh-keygen")
def test_accept_a_changed_host_key_only_with_the_confirmed_fingerprint(home, tmp_path, monkeypatch):
    """'Accept the new key': the key is read again and must have the fingerprint the person confirmed; the old
    record is replaced (other hosts kept), known_hosts backed up, the change logged."""
    import base64
    import paramiko
    from helios.transport import fingerprint
    new = paramiko.PKey.from_type_string("ssh-ed25519", base64.b64decode(C2_NEW))
    monkeypatch.setattr(sshkeys, "_presented_key", lambda t: new)
    kh = home / ".ssh" / "known_hosts"
    kh.write_text(f"10.0.0.12 ssh-ed25519 {C2_OLD}\n10.0.0.11 ssh-ed25519 {C2_OLD}\n")
    p = _profile(tmp_path)
    fp = fingerprint(new)
    assert fp == "SHA256:6ab9qp9Bc4+j7fQpVpsOwvqsObZqugjlHn6JiqLyl1w"
    with pytest.raises(JobError, match="nothing changed"):          # what was checked must be what is presented
        sshkeys.accept_host_key(p, "c2", "SHA256:" + "A" * 43)
    with pytest.raises(JobError, match="not a node of this rack"):
        sshkeys.accept_host_key(p, "evil", fp)
    assert kh.read_text().count(C2_OLD) == 2                          # refused = untouched
    d = sshkeys.accept_host_key(p, "c2", fp, by="Asha (10.0.0.9)")
    text = kh.read_text()
    assert f"10.0.0.12 ssh-ed25519 {C2_NEW}" in text and f"10.0.0.11 ssh-ed25519 {C2_OLD}" in text
    assert f"10.0.0.12 ssh-ed25519 {C2_OLD}" not in text
    assert d["replaced"] == ["SHA256:u0Fh0BAB9tEkAXWijpdbcM8AuplPFIS4J+84vYvjmk8"] and Path(d["backup"]).is_file()
    log = (home / ".ssh" / "helios-ui-hostkeys.log").read_text()
    assert "rk c2 10.0.0.12" in log and fp in log and "Asha (10.0.0.9)" in log
    assert oct(kh.stat().st_mode)[-3:] == "600"
