"""ui/assist.py and its routes: Ask / Diagnose send the run's evidence (AFM names annotated from the rack inventory,
the read-only command reference) to the configured OpenAI-compatible model server; no model configured = a clear
message; bad run names refused; a disruptive answer is flagged."""
from __future__ import annotations

import csv
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ui import assist
from ui.config import RackProfile
from ui.server import App, make_server

from test_ui_keychain import Client, HashExecutor

MSG = ("AFM pre-flight failed - rack not healthy, not starting:\n  - compute pod-1-12-ComputeTray: admitted conditions="
       "[{'type': 'healthy', 'status': 'unknown', 'reason': 'ComputeNode health update not received since x'}]")


def _profile(tmp_path) -> RackProfile:
    inv = tmp_path / "rk.yaml"
    inv.write_text("rack: rk\nswitches: []\ncomputes:\n  - {name: ct13, host: 10.255.4.59, afm_name: pod-1-12-ComputeTray}\n")
    p = RackProfile(name="rk", title="rk", runner="local", inventory=inv, results_dir=tmp_path / "runs" / "rk")
    run = p.results_dir / "rk-20261023-064053"
    run.mkdir(parents=True)
    with open(run / "tc_results.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, ["tc_id", "test", "outcome", "message", "properties"])
        w.writeheader()
        w.writerow({"tc_id": "TC-SWH-060", "test": "t", "outcome": "blocked", "message": MSG, "properties": "{}"})
    return p


@pytest.fixture
def model():
    """A stand-in model server: records the requests, answers with the next queued reply."""
    seen, replies = [], []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data": []}')

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            out = json.dumps({"choices": [{"message": {"content": replies.pop(0)}}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"env": {"HELIOS_AI_URL": f"http://127.0.0.1:{srv.server_port}/v1", "HELIOS_AI_MODEL": "m"},
           "seen": seen, "replies": replies}
    srv.shutdown()


def test_status_and_settings(model):
    assert assist.status({})["configured"] is False and "HELIOS_AI_URL" in assist.status({})["error"]
    st = assist.status(model["env"])
    assert st["configured"] and st["reachable"] and st["model"] == "m"
    st = assist.status({"HELIOS_AI_URL": "http://127.0.0.1:9/v1", "HELIOS_AI_MODEL": "m"})
    assert st["reachable"] is False and "not answering" in st["error"]


def test_ask_grounds_the_question_and_keeps_the_conversation(tmp_path, model):
    p = _profile(tmp_path)
    model["replies"] += ["ct13 (10.255.4.59) stopped reporting.", "Reboot ct13 now."]
    r = assist.ask(p, "rk-20261023-064053", "which tray?", [], model["env"])
    assert r == {"answer": "ct13 (10.255.4.59) stopped reporting.", "unsafe": None}
    user = model["seen"][0]["messages"][1]["content"]
    assert "pod-1-12-ComputeTray [= ct13, 10.255.4.59]" in user and "Command reference" in user
    assert user.endswith("Question: which tray?") and model["seen"][0]["temperature"] == 0
    r = assist.ask(p, "rk-20261023-064053", "what now?", [{"q": "which tray?", "a": r["answer"]}], model["env"])
    msgs = model["seen"][1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"] and msgs[3]["content"] == "what now?"
    assert r["unsafe"] and "reboot" in r["unsafe"].lower()
    for bad in ("../x", "rk-session-20261023-064053", "nope"):
        with pytest.raises((KeyError, Exception)):
            assist.ask(p, bad, "q", [], model["env"])
    with pytest.raises(Exception, match="no model configured"):
        assist.ask(p, "rk-20261023-064053", "q", [], {})


def test_routes(tmp_path, model, monkeypatch):
    for k, v in model["env"].items():
        monkeypatch.setenv(k, v)
    p = _profile(tmp_path)
    app = App(profiles={"rk": p}, executor=HashExecutor())
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(srv.server_address[1])
    try:
        st, d = c.j("GET", "/api/ai/status")
        assert st == 200 and d["reachable"]
        model["replies"].append("ct13.")
        st, d = c.j("POST", "/api/racks/rk/results/rk-20261023-064053/ask", {"question": "which tray?", "history": []})
        assert st == 200 and d["answer"] == "ct13."
        model["replies"].append(json.dumps({"fault": "rack_not_healthy", "root_cause": "compute_health_stale",
                                            "category": "rack_setup", "summary": "s", "next_step": "check ct13"}))
        st, d = c.j("POST", "/api/racks/rk/results/rk-20261023-064053/diagnose", {})
        assert st == 200 and d["cases"][0]["agrees"] and d["cases"][0]["rules"]["fault"] == "rack_not_healthy"
        st, _ = c.j("POST", "/api/racks/rk/results/rk-20261023-064053/ask", {"question": ""})
        assert st == 400
        st, _ = c.j("POST", "/api/racks/rk/results/nope-1/ask", {"question": "x"})
        assert st in (400, 404)
    finally:
        app.shutdown()
        srv.shutdown()
        srv.server_close()
