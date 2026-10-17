"""The AFM password in the macOS Keychain (opt-in): ui/keychain.py drives /usr/bin/security without ever putting the
password on a command line, runs use a saved password when none is typed (a typed one wins), the log masks it, and
the API never returns it. The real Keychain is never touched: `security` is a fake here."""
from __future__ import annotations

import hashlib
import http.client
import json
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from ui import keychain as kcmod
from ui.config import RackProfile
from ui.keychain import FileSecrets, Keychain, KeychainError
from ui.runner import Executor
from ui.server import App, make_server

PW_ENV = "HELIOS_UI_KC_TEST_PW"
SAVED = "Kc saved \"pw\" $HOME \\x 'q'"         # spaces, quotes, $ and backslashes
TYPED = "Typed-Pa55w0rd!"


class FakeSecurity:
    """/usr/bin/security stand-in: an in-memory keychain; records argv and stdin of every call."""

    def __init__(self, fail: dict | None = None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.store: dict[tuple[str, str], str] = {}
        self.fail = fail or {}                 # command -> (rc, stderr)

    def __call__(self, argv, stdin):
        self.calls.append((list(argv), stdin))
        assert argv[0] == kcmod.SECURITY
        if argv[1:] == ["-i"]:
            words = stdin.split()
            cmd = words[0]
            if cmd in self.fail:
                return self.fail[cmd][0], "", self.fail[cmd][1]
            assert cmd == "add-generic-password" and "-U" in words and stdin.endswith("\n")
            s, a, hx = (words[words.index(o) + 1] for o in ("-s", "-a", "-X"))
            self.store[(s, a)] = bytes.fromhex(hx).decode("ascii")
            return 0, "", ""
        cmd = argv[1]
        if cmd in self.fail:
            return self.fail[cmd][0], "", self.fail[cmd][1]
        key = (argv[argv.index("-s") + 1], argv[argv.index("-a") + 1])
        if key not in self.store:
            return 44, "", "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain."
        if cmd == "find-generic-password":
            return 0, (self.store[key] + "\n") if "-w" in argv else 'keychain: "login.keychain-db"\n', ""
        if cmd == "delete-generic-password":
            del self.store[key]
            return 0, "password has been deleted.\n", ""
        raise AssertionError(argv)

    def argv_words(self) -> list[str]:
        return [w for argv, _ in self.calls for w in argv]


# ------------------------------------------------------------------ module

@pytest.mark.parametrize("pw", [SAVED, "Z", " lead and trail ", "0x41", "abcdef12", "".join(map(chr, range(32, 127)))])
def test_save_read_delete_never_on_argv(pw):
    fake = FakeSecurity()
    k = Keychain(run=fake)
    k.save("mr3:admin", pw)
    argv, stdin = fake.calls[-1]
    assert argv == [kcmod.SECURITY, "-i"]                                  # interactive mode, command on stdin
    assert stdin == f"add-generic-password -U -s helios-ui -a mr3:admin -l helios-ui -X {pw.encode().hex()}\n"
    assert pw not in stdin                                                # only the hex form travels, even on stdin
    assert k.has("mr3:admin") is True and fake.calls[-1][0][-1] != "-w"   # status check does not read the secret
    assert k.get("mr3:admin") == pw
    assert fake.calls[-1][0] == [kcmod.SECURITY, "find-generic-password", "-s", "helios-ui", "-a", "mr3:admin", "-w"]
    assert k.delete("mr3:admin") is True and k.delete("mr3:admin") is False
    assert k.get("mr3:admin") is None and k.has("mr3:admin") is False
    for w in fake.argv_words():
        assert pw not in w, (pw, w)


@pytest.mark.parametrize("pw, why", [("", "no password"), (None, "no password"), (5, "no password"),
                                     ("café", "printable ASCII"), ("tab\there", "printable ASCII"),
                                     ("new\nline", "printable ASCII"), ("x" * 257, "printable ASCII")])
def test_unsupported_passwords_rejected(pw, why):
    fake = FakeSecurity()
    with pytest.raises(KeychainError, match=why):
        Keychain(run=fake).save("mr3:admin", pw)
    assert fake.calls == []


@pytest.mark.parametrize("rack, user", [("mr3", "ad min"), ("mr3", "a;rm"), ("mr3", 'x"y'), ("m r3", "admin")])
def test_account_validation(rack, user):
    with pytest.raises(KeychainError, match="Keychain account"):
        kcmod.account(rack, user)
    assert kcmod.account("g02-g17", None) == "g02-g17:admin" and kcmod.account("mr3", "ops@x") == "mr3:ops@x"
    with pytest.raises(KeychainError):
        Keychain(run=FakeSecurity()).save("bad account", "pw")


def test_security_failure_is_a_clear_error_without_the_password():
    fake = FakeSecurity(fail={"add-generic-password": (51, "security: User interaction is not allowed."),
                              "find-generic-password": (36, "security: keychain locked")})
    k = Keychain(run=fake)
    with pytest.raises(KeychainError, match="save failed .*exit 51.*not allowed") as e:
        k.save("mr3:admin", SAVED)
    assert SAVED not in str(e.value) and SAVED.encode().hex() not in str(e.value)
    with pytest.raises(KeychainError, match="read failed .*exit 36"):
        k.get("mr3:admin")


def test_not_available_and_disabled(monkeypatch):
    k = Keychain(security="/nonexistent/security")
    assert k.available() is False
    with pytest.raises(KeychainError, match="not available"):
        k.get("mr3:admin")
    off = Keychain(run=FakeSecurity(), enabled=False)
    assert off.available() is False
    with pytest.raises(KeychainError, match="not enabled"):
        off.save("mr3:admin", "pw12")
    monkeypatch.setattr(kcmod.sys, "platform", "linux")
    assert Keychain().available() is False


def test_default_runner_uses_stdin_not_argv(monkeypatch):
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return R()
    monkeypatch.setattr(kcmod.subprocess, "run", fake_run)
    monkeypatch.setattr(kcmod.sys, "platform", "darwin")
    monkeypatch.setattr(kcmod.Path, "is_file", lambda self: True)
    Keychain().save("mr3:admin", SAVED)
    assert seen["argv"] == [kcmod.SECURITY, "-i"] and SAVED.encode().hex() in seen["kw"]["input"]
    assert seen["kw"]["capture_output"] and seen["kw"]["timeout"]


# ------------------------------------------------------------------ server + runs

class HashExecutor(Executor):
    """Stand-in for a run: prints the password it got (the UI must redact it) and a digest (which one it was)."""

    def popen(self, argv, env=None, cwd=None, stdin=False):
        code = ("import os, hashlib; p = os.environ.get(%r) or ''; print('pw is', p, flush=True); "
                "print('digest', hashlib.sha256(p.encode()).hexdigest()[:16], flush=True)") % PW_ENV
        return super().popen([sys.executable, "-c", code], env=env, cwd=cwd, stdin=stdin)


def _digest(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()[:16]


def _profile(tmp_path: Path, name: str, results: Path, user="admin") -> RackProfile:
    inv = tmp_path / f"{name}.yaml"
    inv.write_text(yaml.safe_dump({"rack": name, "switches": [], "computes": [],
                                   "afm": {"url": "https://x", "auth": {"username": user, "password_env": PW_ENV}}}))
    return RackProfile(name=name, title=name, runner="local", inventory=inv, results_dir=results)


class Client:
    def __init__(self, port):
        self.port = port

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "X-Helios-UI": "1"})
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def j(self, method, path, body=None):
        st, data = self.req(method, path, body)
        return st, json.loads(data or b"null")

    def wait(self, rid, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            _, d = self.j("GET", f"/api/runs/{rid}")
            if d["status"] not in ("queued", "running"):
                return d
            time.sleep(0.1)
        raise AssertionError("run did not finish")

    def log(self, rid) -> str:
        return self.req("GET", f"/api/runs/{rid}/log.txt")[1].decode()


def _serve(app):
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def ui(tmp_path):
    results = tmp_path / "results"
    fake = FakeSecurity()
    profiles = {"rack-a": _profile(tmp_path, "rack-a", results), "rack-b": _profile(tmp_path, "rack-b", results, "ops")}
    app = App(profiles=profiles, executor=HashExecutor(), keychain=Keychain(run=fake))
    srv = _serve(app)
    yield Client(srv.server_address[1]), app, results, fake
    app.shutdown()
    srv.shutdown()
    srv.server_close()


def _nowhere(root: Path, *secrets: str) -> None:
    for f in root.rglob("*"):
        if f.is_file():
            for s in secrets:
                assert s.encode() not in f.read_bytes(), f"password in {f}"


def test_password_api_never_returns_the_value(ui):
    c, _, results, fake = ui
    st, d = c.j("GET", "/api/racks/rack-a/password")
    assert st == 200 and d == {"saved": False, "available": True, "account": "rack-a:admin",
        "store": "macOS Keychain"}
    st, raw = c.req("POST", "/api/racks/rack-a/password", {"password": SAVED})
    assert st == 200 and SAVED.encode() not in raw and json.loads(raw)["saved"] is True
    assert fake.store == {("helios-ui", "rack-a:admin"): SAVED}
    st, raw = c.req("GET", "/api/racks/rack-a/password")
    assert st == 200 and SAVED.encode() not in raw and SAVED.encode().hex().encode() not in raw
    assert json.loads(raw)["saved"] is True
    assert not any(a[-1] == "-w" for a, _ in fake.calls)          # status never even reads the secret
    assert c.j("GET", "/api/racks/rack-b/password")[1] == {"saved": False, "available": True, "account": "rack-b:ops",
        "store": "macOS Keychain"}
    st, d = c.j("POST", "/api/racks/rack-a/password", {"password": "café"})
    assert st == 400 and "printable ASCII" in d["error"]
    st, d = c.j("POST", "/api/racks/rack-a/password/delete", {})
    assert st == 200 and d["deleted"] is True and d["saved"] is False and fake.store == {}
    assert c.j("POST", "/api/racks/rack-a/password/delete", {})[1]["deleted"] is False
    assert c.j("GET", "/api/racks/nope/password")[0] == 404
    # CSRF guards as for every POST
    st, _ = c.req("POST", "/api/racks/rack-a/password", {"password": SAVED}, headers={"X-Helios-UI": "0"})
    assert st == 403 and fake.store == {}
    st, _ = c.req("POST", "/api/racks/rack-a/password", {"password": SAVED}, headers={"Host": "evil.example"})
    assert st == 403 and fake.store == {}
    _nowhere(results, SAVED)


def test_run_uses_keychain_password_when_field_empty(ui):
    c, _, results, fake = ui
    fake.store[("helios-ui", "rack-a:admin")] = SAVED
    st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "health", "password": ""})
    assert st == 201 and r["keychain"] == {} and r["password_source"] == "keychain"
    d = c.wait(r["id"])
    assert d["status"] == "finished" and d["has_password"] is True and d["password_source"] == "keychain"
    log = c.log(r["id"])
    assert "AFM password from macOS Keychain (memory only)" in log.splitlines()[0]
    assert f"digest {_digest(SAVED)}" in log and "pw is <REDACTED>" in log and SAVED not in log
    _nowhere(results, SAVED)


def test_rerun_failed_uses_keychain_password(ui):
    c, _, results, fake = ui
    fake.store[("helios-ui", "rack-a:admin")] = SAVED
    (results / "rack-a-20261016-1").mkdir(parents=True)
    (results / "rack-a-20261016-1" / "tc_summary.csv").write_text("tc,verdict\n")
    st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "rerun", "base_run": "rack-a-20261016-1",
                                      "known": True, "password": ""})
    assert st == 201, r
    log = (c.wait(r["id"]), c.log(r["id"]))[1]
    assert "from macOS Keychain" in log.splitlines()[0] and f"digest {_digest(SAVED)}" in log and SAVED not in log
    _nowhere(results, SAVED)


def test_typed_password_wins_and_remember_saves_it(ui):
    c, _, results, fake = ui
    fake.store[("helios-ui", "rack-a:admin")] = SAVED
    st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "health", "password": TYPED})
    assert st == 201 and r["password_source"] == "typed"
    log = (c.wait(r["id"]), c.log(r["id"]))[1]
    assert "AFM password given (memory only)" in log and f"digest {_digest(TYPED)}" in log and TYPED not in log
    assert fake.store[("helios-ui", "rack-a:admin")] == SAVED          # not ticked: Keychain unchanged
    assert not any(a[1:] == ["-i"] for a, _ in fake.calls)
    # ticked: saved first, then used
    st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "health", "password": TYPED, "remember_password": True})
    assert st == 201 and r["keychain"] == {"saved": True}
    log = (c.wait(r["id"]), c.log(r["id"]))[1]
    assert "# AFM password saved in the macOS Keychain" in log and TYPED not in log
    assert fake.store[("helios-ui", "rack-a:admin")] == TYPED
    rec = c.j("GET", f"/api/runs/{r['id']}")[1]
    assert "remember_password" not in json.dumps(rec) and TYPED not in json.dumps(rec)
    # the next run with an empty field gets it from the Keychain
    st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "health"})
    log = (c.wait(r["id"]), c.log(r["id"]))[1]
    assert "from macOS Keychain" in log and f"digest {_digest(TYPED)}" in log
    for a, _ in fake.calls:
        assert TYPED not in " ".join(a) and SAVED not in " ".join(a)
    _nowhere(results, TYPED, SAVED)


def test_keychain_trouble_never_stops_a_run(ui):
    c, _, results, fake = ui
    fake.fail = {"add-generic-password": (51, "security: User interaction is not allowed."),
                 "find-generic-password": (36, "security: keychain locked")}
    st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "health", "password": TYPED, "remember_password": True})
    assert st == 201 and "save failed" in r["keychain"]["error"]
    log = (c.wait(r["id"]), c.log(r["id"]))[1]
    assert "AFM password NOT saved in the macOS Keychain" in log and "given (memory only)" in log and TYPED not in log
    st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "health"})
    assert st == 201 and "read failed" in r["keychain"]["error"] and r["has_password"] is False
    log = (c.wait(r["id"]), c.log(r["id"]))[1]
    assert "AFM password not given" in log and "Keychain could not be read" in log
    st, d = c.j("GET", "/api/racks/rack-a/password")
    assert st == 200 and d["saved"] is False and "lookup failed" in d["error"]
    _nowhere(results, TYPED)


def test_no_keychain_in_app_by_default(tmp_path):
    """An App built without a Keychain (tests, other platforms): nothing saved, runs work as before."""
    results = tmp_path / "results"
    app = App(profiles={"rack-a": _profile(tmp_path, "rack-a", results)}, executor=HashExecutor())
    srv = _serve(app)
    try:
        c = Client(srv.server_address[1])
        d = c.j("GET", "/api/racks/rack-a/password")[1]
        assert d["available"] is False and d["saved"] is False
        st, d = c.j("POST", "/api/racks/rack-a/password", {"password": TYPED})
        assert st == 400 and "not enabled" in d["error"]
        st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "health", "password": TYPED,
                                          "remember_password": True})
        assert st == 201 and "not enabled" in r["keychain"]["error"]
        log = (c.wait(r["id"]), c.log(r["id"]))[1]
        assert "given (memory only)" in log and TYPED not in log
        st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "health"})
        assert st == 201 and r["keychain"] == {} and r["has_password"] is False
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()
    _nowhere(results, TYPED)


# ------------------------------------------------------------------ Linux UI server: the password file
# g02-g17 17 Oct: the UI installed on a Linux server greyed out "Remember" (no macOS Keychain) - nothing saved.

def test_password_store_falls_back_to_file_without_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(kcmod.sys, "platform", "linux")
    st = kcmod.password_store({"HELIOS_UI_SECRETS": str(tmp_path / "s.json")})
    assert isinstance(st, FileSecrets) and st.path == tmp_path / "s.json" and st.available()
    assert kcmod.default_secrets_path({"XDG_CONFIG_HOME": "/x"}) == Path("/x/helios-ui/secrets.json")


def test_file_secrets_save_get_delete_mode_600(tmp_path):
    f = tmp_path / "cfg" / "helios-ui" / "secrets.json"
    st = FileSecrets(f)
    assert st.get("mr3:admin") is None and st.has("mr3:admin") is False and st.delete("mr3:admin") is False
    st.save("mr3:admin", SAVED)
    st.save("g02-g17:admin", TYPED)
    assert (f.stat().st_mode & 0o777) == 0o600 and (f.parent.stat().st_mode & 0o777) == 0o700
    assert st.get("mr3:admin") == SAVED and FileSecrets(f).has("g02-g17:admin")
    assert st.delete("mr3:admin") is True and st.get("mr3:admin") is None and st.get("g02-g17:admin") == TYPED
    with pytest.raises(KeychainError, match="printable ASCII"):
        st.save("mr3:admin", "café")
    with pytest.raises(KeychainError, match="bad account"):
        st.save("m r3", SAVED)


def test_file_secrets_refuses_a_file_others_can_read(tmp_path):
    f = tmp_path / "secrets.json"
    st = FileSecrets(f)
    st.save("mr3:admin", SAVED)
    f.chmod(0o644)
    with pytest.raises(KeychainError, match="chmod 600") as e:
        st.get("mr3:admin")
    assert SAVED not in str(e.value)


def test_remember_on_a_linux_ui_server(tmp_path):
    """Remember + Start run saves to the password file; the next run with the field empty uses it."""
    results, f = tmp_path / "results", tmp_path / "home" / "secrets.json"
    app = App(profiles={"rack-a": _profile(tmp_path, "rack-a", results)}, executor=HashExecutor(),
              keychain=FileSecrets(f))
    srv = _serve(app)
    try:
        c = Client(srv.server_address[1])
        d = c.j("GET", "/api/racks/rack-a/password")[1]
        assert d["available"] is True and d["saved"] is False and d["store"] == f"password file {f}"
        st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "health", "password": TYPED,
                                          "remember_password": True})
        assert st == 201 and r["keychain"] == {"saved": True}
        assert "saved in the password file" in (c.wait(r["id"]), c.log(r["id"]))[1]
        assert c.j("GET", "/api/racks/rack-a/password")[1]["saved"] is True
        st, r = c.j("POST", "/api/runs", {"rack": "rack-a", "kind": "health"})
        assert st == 201 and r["has_password"] is True
        st, d = c.j("POST", "/api/racks/rack-a/password/delete", {})
        assert d["deleted"] is True and d["saved"] is False and json.loads(f.read_text()) == {}
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()
    _nowhere(results, TYPED)
