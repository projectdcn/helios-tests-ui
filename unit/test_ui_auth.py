"""UI access token (HELIOS_UI_TOKEN): on a Linux server listening on 0.0.0.0 anyone who reached the port could start
runs - and, with a saved AFM password, run AFM cases without knowing it. With a token every request but the page
itself, /static/, /api/version and /api/login needs the login cookie (or Authorization: Bearer)."""
from __future__ import annotations

import json
import threading

import pytest

from ui import __main__ as uimain
from ui.server import AUTH_COOKIE, App, auth_cookie_value, make_server

from test_ui_keychain import Client, HashExecutor, _profile

TOKEN = "t0ken-for-the-ui-1234"


@pytest.fixture
def served(tmp_path):
    results = tmp_path / "results"
    app = App(profiles={"rack-a": _profile(tmp_path, "rack-a", results)}, executor=HashExecutor())
    srv = make_server(app, 0, token=TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Client(srv.server_address[1])
    app.shutdown()
    srv.shutdown()
    srv.server_close()


def test_open_paths_and_everything_else_needs_login(served):
    c = served
    assert c.req("GET", "/")[0] == 200 and c.req("GET", "/static/app.js")[0] == 200
    st, d = c.j("GET", "/api/version")
    assert st == 200 and d["auth"] is True
    for path in ("/api/racks", "/api/runs", "/api/racks/rack-a/password", "/api/racks/rack-a/inventory",
                 "/results/rack-a/x/report.html"):
        st, raw = c.req("GET", path)
        assert st == 401 and json.loads(raw)["login"] is True, path
    st, _ = c.req("POST", "/api/runs", {"rack": "rack-a", "kind": "health"})
    assert st == 401
    st, _ = c.req("POST", "/api/racks/rack-a/password", {"password": "x" * 8})
    assert st == 401


def test_login_cookie_and_bearer(served):
    c = served
    st, raw = c.req("POST", "/api/login", {"token": "wrong-token-wrong-token"})
    assert st == 401 and TOKEN.encode() not in raw
    conn_status, _ = c.req("POST", "/api/login", {"token": TOKEN})
    assert conn_status == 200
    cookie = f"{AUTH_COOKIE}={auth_cookie_value(TOKEN)}"
    assert c.req("GET", "/api/racks", headers={"Cookie": cookie})[0] == 200
    assert c.req("GET", "/api/racks", headers={"Cookie": f"{AUTH_COOKIE}=forged"})[0] == 401
    assert c.req("GET", "/api/racks", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 200
    assert c.req("GET", "/api/racks", headers={"Authorization": "Bearer nope"})[0] == 401
    # the cookie is not the token, and another token makes old cookies useless
    assert TOKEN not in auth_cookie_value(TOKEN) and auth_cookie_value(TOKEN) != auth_cookie_value(TOKEN + "x")


def test_login_sets_httponly_strict_cookie_and_logout_clears_it(served):
    import http.client
    c = served
    conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=10)
    conn.request("POST", "/api/login", body=json.dumps({"token": TOKEN}),
                 headers={"Host": f"127.0.0.1:{c.port}", "Content-Type": "application/json", "X-Helios-UI": "1"})
    r = conn.getresponse()
    r.read()
    sc = r.getheader("Set-Cookie")
    assert r.status == 200 and "HttpOnly" in sc and "SameSite=Strict" in sc and TOKEN not in sc
    conn.request("POST", "/api/logout", body="{}",
                 headers={"Host": f"127.0.0.1:{c.port}", "Content-Type": "application/json", "X-Helios-UI": "1"})
    r = conn.getresponse()
    r.read()
    assert "Max-Age=0" in r.getheader("Set-Cookie")
    conn.close()
    # login is a state-changing POST: the CSRF header is still required
    assert c.req("POST", "/api/login", {"token": TOKEN}, headers={"X-Helios-UI": "0"})[0] == 403


def test_no_token_no_login(tmp_path):
    app = App(profiles={"rack-a": _profile(tmp_path, "rack-a", tmp_path / "r")}, executor=HashExecutor())
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(srv.server_address[1])
        assert c.req("GET", "/api/racks")[0] == 200 and c.j("GET", "/api/version")[1]["auth"] is False
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()


def test_short_token_refused(tmp_path, monkeypatch, capsys):
    app = App(profiles={}, executor=HashExecutor())
    with pytest.raises(ValueError, match="at least 16"):
        make_server(app, 0, token="short")
    monkeypatch.setenv("HELIOS_UI_TOKEN", "short")
    assert uimain.main(["--port", "0"]) == 2 and "at least 16" in capsys.readouterr().err


def test_login_page_says_which_server_and_who_to_ask(served, monkeypatch):
    """The login page names the server, who runs the UI and the command that prints the token - never the token."""
    c = served
    monkeypatch.setenv("HELIOS_UI_ADMIN", "Lab admin / #helios-lab")
    st, raw = c.req("GET", "/api/version")
    d = json.loads(raw)
    assert st == 200 and d["server"] and d["admin"] == "Lab admin / #helios-lab"
    assert d["token_cmd"].endswith("token") or "HELIOS_UI_TOKEN" in d["token_cmd"]
    assert TOKEN.encode() not in raw
    monkeypatch.delenv("HELIOS_UI_ADMIN")
    assert c.j("GET", "/api/version")[1]["admin"]                 # the OS user running the UI


def test_no_login_hint_without_a_token(tmp_path):
    app = App(profiles={}, executor=HashExecutor())
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = Client(srv.server_address[1]).j("GET", "/api/version")[1]
        assert d["auth"] is False and "server" not in d and "admin" not in d
    finally:
        srv.shutdown()
        srv.server_close()
