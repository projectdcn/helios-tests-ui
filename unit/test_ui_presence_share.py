"""Who is using the UI (ui/presence.py) and the redacted share zip (ui/share.py), 19 Oct: several people share one
access token on srv03 (behind nginx) and send results to AMD."""
from __future__ import annotations

import gzip
import http.client
import io
import json
import threading
import zipfile

import pytest

from ui import presence, share
from ui.server import AUTH_COOKIE, App, auth_cookie_value, make_server

from test_ui_keychain import Client, HashExecutor, _profile

TOKEN = "t0ken-for-the-ui-1234"


def test_client_ip_trusts_forwarded_only_from_loopback():
    h = {"X-Forwarded-For": "10.1.2.3, 10.9.9.9"}
    assert presence.client_ip("127.0.0.1", h) == "10.9.9.9"          # nginx on this server: its $remote_addr
    assert presence.client_ip("10.5.5.5", h) == "10.5.5.5"           # anyone else: the header is ignored
    assert presence.client_ip("127.0.0.1", {"X-Real-IP": "10.7.7.7"}) == "10.7.7.7"
    assert presence.client_ip("127.0.0.1", {"X-Forwarded-For": "<script>"}) == "127.0.0.1"


def test_presence_active_recent_and_names():
    p = presence.Presence()
    p.touch("a" * 20, "Asha", "10.0.0.1", "Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/120 Safari/537", now=1000)
    p.touch("b" * 20, "", "10.0.0.2", "Mozilla/5.0 (Windows NT 10.0) Firefox/120", now=1000)
    p.touch("a" * 20, "Asha", "10.0.0.1", "", now=1100)
    s = p.snapshot(me="a" * 20, now=1200)
    assert [u["ip"] for u in s["active"]] == ["10.0.0.1"] and s["active"][0]["you"] and s["active"][0]["requests"] == 2
    assert [u["browser"] for u in s["recent"]] == ["Firefox on Windows"] and s["people"] == 1
    assert "a" * 20 not in json.dumps(s)                                # ids are hashed: no cookie to replay
    assert p.label("a" * 20) == "Asha (10.0.0.1)" and p.label("b" * 20) == "10.0.0.2"
    assert p.snapshot(now=1100 + presence.KEEP_S + 1)["recent"] == []    # forgotten after an hour
    assert presence.clean_name("  Asha   R ") == "Asha R" and presence.clean_name("<b>") == "" and presence.clean_name("x" * 41) == ""


def test_redact_text_rules_and_what_stays():
    t = "\n".join(["password: hunter22", "passed: 12", "  \"password\": \"abc\"", "HELIOS_AFM_PASSWORD=zzzz",
                   "sshpass -p s3cret ssh x", "curl -H 'Authorization: Bearer abcdef123' https://u:pw@h/x",
                   "cmd --password=qqqq --token tttt", "-----BEGIN OPENSSH PRIVATE KEY-----", "AAAA",
                   "-----END OPENSSH PRIVATE KEY-----", "tests/test_afm.py::test_password_policy PASSED",
                   "&quot;token&quot;: &quot;xyz&quot;", "the saved one mysecretvalue here", "switch 10.0.0.5 st1"])
    r, n = share.redact_text(t, ["mysecretvalue"])
    for s in ("hunter22", "abc", "zzzz", "s3cret", "abcdef123", ":pw@", "qqqq", "tttt", "AAAA", "xyz", "mysecretvalue"):
        assert s not in r, s
    for s in ("passed: 12", "test_password_policy PASSED", "switch 10.0.0.5 st1"):
        assert s in r, s
    assert n >= 11


def test_share_zip_redacts_text_and_gz_leaves_out_binaries(tmp_path):
    f = tmp_path / "rack-a-20261019-010101"
    (f / "raw").mkdir(parents=True)
    (f / "log.txt").write_text("login with password: hunter22\nok\n")
    (f / "raw" / "dump.json.gz").write_bytes(gzip.compress(b'{"token": "abc123", "n": 1}'))
    (f / "blob.bin").write_bytes(b"\0\1\2secret")
    (f / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\0")
    buf = io.BytesIO()
    out = share.share_zip(f, buf, ["hunter22"], shared_by="Asha (10.0.0.1)")
    z = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    names = set(z.namelist())
    assert f"{f.name}/blob.bin" not in names and f"{f.name}/shot.png" in names
    assert b"hunter22" not in z.read(f"{f.name}/log.txt")
    assert b"abc123" not in gzip.decompress(z.read(f"{f.name}/raw/dump.json.gz"))
    readme = z.read(f"{f.name}/{share.README}").decode()
    assert "blob.bin (binary" in readme and "Asha (10.0.0.1)" in readme and "NOT removed" in readme
    assert out["changed"]["log.txt"] >= 1


@pytest.fixture
def served(tmp_path):
    results = tmp_path / "results"
    app = App(profiles={"rack-a": _profile(tmp_path, "rack-a", results)}, executor=HashExecutor())
    srv = make_server(app, 0, token=TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Client(srv.server_address[1]), results
    app.shutdown()
    srv.shutdown()
    srv.server_close()


def _req(port, method, path, body=None, cookies="", extra=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    h = {"Host": f"127.0.0.1:{port}", "Cookie": f"{AUTH_COOKIE}={auth_cookie_value(TOKEN)}" + (f"; {cookies}" if cookies else "")}
    if body is not None:
        h.update({"Content-Type": "application/json", "X-Helios-UI": "1"})
    h.update(extra or {})
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    out = (r.status, data, r.getheader("Set-Cookie") or "")
    c.close()
    return out


def test_users_names_and_run_attribution_over_http(served):
    c, results = served
    st, _, sc = _req(c.port, "GET", "/api/users")
    assert st == 200 and presence.BID_COOKIE in sc and "HttpOnly" in sc       # a new browser gets its id
    bid = sc.split(";")[0].split("=", 1)[1]
    me = f"{presence.BID_COOKIE}={bid}"
    st, raw, sc = _req(c.port, "POST", "/api/me", {"name": "Asha R"}, cookies=me, extra={"X-Forwarded-For": "10.83.1.5"})
    assert st == 200 and presence.NAME_COOKIE in sc
    name = sc.split(";")[0].split("=", 1)[1]
    assert _req(c.port, "POST", "/api/me", {"name": "<b>x</b>"}, cookies=me)[0] == 400
    cookies = f"{me}; {presence.NAME_COOKIE}={name}"
    _req(c.port, "GET", "/api/racks", cookies="helios_ui_bid=" + "z" * 20, extra={"User-Agent": "Firefox/1"})
    st, raw, _ = _req(c.port, "GET", "/api/users", cookies=cookies, extra={"X-Forwarded-For": "10.83.1.5"})
    d = json.loads(raw)
    assert st == 200 and len(d["active"]) == 2 and d["you"]["name"] == "Asha R" and d["you"]["ip"] == "10.83.1.5"
    # unauthenticated requests are refused and never listed; a Bearer script is not counted
    assert c.req("GET", "/api/users")[0] == 401
    _req(c.port, "GET", "/api/users", extra={"Authorization": f"Bearer {TOKEN}", "Cookie": ""})
    assert len(json.loads(_req(c.port, "GET", "/api/users", cookies=cookies)[1])["active"]) == 2
    # a run records who started it
    st, raw, _ = _req(c.port, "POST", "/api/runs", {"rack": "rack-a", "kind": "health"}, cookies=cookies,
                      extra={"X-Forwarded-For": "10.83.1.5"})
    run = json.loads(raw)
    assert st == 201 and run["started_by"] == "Asha R (10.83.1.5)"


def test_share_route_redacts_the_token_and_saved_values(served):
    c, results = served
    f = results / "rack-a-20261019-020202"
    f.mkdir(parents=True)
    (f / "notes.txt").write_text(f"the UI token {TOKEN} leaked\n")
    st, raw, _ = _req(c.port, "GET", f"/api/racks/rack-a/results/{f.name}/share")
    assert st == 200
    z = zipfile.ZipFile(io.BytesIO(raw))
    assert TOKEN.encode() not in z.read(f"{f.name}/notes.txt") and f"{f.name}/{share.README}" in z.namelist()
    assert _req(c.port, "GET", "/api/racks/rack-a/results/..%2Fx/share")[0] == 404


def test_the_page_hands_out_the_browser_id(served):
    """The page fires several API calls at once: without an id from the page itself each made its own browser."""
    c, _ = served
    st, _, sc = _req(c.port, "GET", "/")
    assert st == 200 and presence.BID_COOKIE in sc
    _, _, sc2 = _req(c.port, "GET", "/", cookies=sc.split(";")[0])
    assert presence.BID_COOKIE not in sc2                                  # kept, not replaced


def test_a_results_folder_link_opens_its_report(served):
    """Runs tab, 20 Oct: the results link of mr3-20261020-054251 opened tc_summary.csv instead of the report."""
    c, results = served
    f = results / "rack-a-20261020-054251"
    f.mkdir(parents=True)
    (f / "tc_summary.csv").write_text("tc,verdict\n")
    st, _, _ = _req(c.port, "GET", f"/results/rack-a/{f.name}/")
    assert st == 302
    c2 = http.client.HTTPConnection("127.0.0.1", c.port, timeout=10)
    c2.request("GET", f"/results/rack-a/{f.name}/", headers={"Host": f"127.0.0.1:{c.port}",
                                                             "Cookie": f"{AUTH_COOKIE}={auth_cookie_value(TOKEN)}"})
    assert c2.getresponse().getheader("Location").endswith("/tc_summary.csv")
    (f / "report.html").write_text("<html></html>")
    c2 = http.client.HTTPConnection("127.0.0.1", c.port, timeout=10)
    c2.request("GET", f"/results/rack-a/{f.name}", headers={"Host": f"127.0.0.1:{c.port}",
                                                            "Cookie": f"{AUTH_COOKIE}={auth_cookie_value(TOKEN)}"})
    assert c2.getresponse().getheader("Location") == f"/results/rack-a/{f.name}/report.html"
    assert _req(c.port, "GET", "/results/rack-a/nope/")[0] == 404
