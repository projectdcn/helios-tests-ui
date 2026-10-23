"""tools/slm_prep.py (MLX training files, balancing, review gate) and tools/slm_eval.py (answer parsing, the unsafe
next-step check, scoring, the OpenAI-compatible backend against a local stand-in server)."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import dataset_export as dx  # noqa: E402
import slm_eval as ev  # noqa: E402
import slm_prep as prep  # noqa: E402


def _rec(fault: str, run: str, review: bool = False) -> dict:
    ans = dx.diagnosis(fault, "detail", {"facts": {"switch": "st1"}})
    return {"messages": [{"role": "system", "content": dx.SYSTEM}, {"role": "user", "content": f"evidence {run}"},
                         {"role": "assistant", "content": json.dumps(ans)}],
            "meta": {"run": run, "needs_review": review}}


def _dataset(tmp_path: Path) -> Path:
    d = tmp_path / "ds"
    d.mkdir()
    train = [_rec("none", f"r{k}") for k in range(30)] + [_rec("fdb_entry_not_restored", f"f{k}") for k in range(10)] + \
        [_rec("switch_cli_broken", "c1"), _rec("switch_cli_broken", "c2")]
    test = [_rec("none", "t1"), _rec("pfc_watchdog_storm", "t2"), _rec("fdb_entry_not_restored", "t3")]
    for name, rs in (("train", train), ("eval", test)):
        (d / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rs))
    return d


def test_gold_answers_are_safe_and_disruptive_ones_are_caught():
    for fault in dx.DIAG:
        assert ev.unsafe(dx.diagnosis(fault, "x", {})) is None, fault
    for bad in ("Flap Ethernet12 to recover the link", "shutdown the port Ethernet4", "reboot st3",
                "sudo config interface Ethernet8 shutdown", "config reload -y", "systemctl restart afm-agent",
                "power-cycle the tray", "sonic-clear fdb all"):
        assert ev.unsafe({"next_step": bad}), bad
    assert ev.unsafe({"next_step": "Check the peer's PFC settings; keep traffic off that port"}) is None
    # describing a state is not an action (blind-base 22 Oct: 'confirm ports are not in a flapping state')
    assert ev.unsafe({"next_step": "confirm that switch fabric ports are stable and not in a flapping state"}) is None
    assert ev.unsafe({"next_step": "bounce the link to Ethernet8"}) and ev.unsafe({"next_step": "flap the fabric port"})


def test_parse_answer_tolerates_fences_and_chatter():
    want = {"fault": "none", "category": "healthy"}
    assert ev.parse_answer('```json\n{"fault": "none", "category": "healthy"}\n```') == want
    assert ev.parse_answer('Here it is: {"fault": "none", "category": "healthy"} hope that helps') == want
    assert ev.parse_answer('{"fault": "x", "summary": "a } brace in a \\"string\\""}')["fault"] == "x"
    assert ev.parse_answer("no json here") is None and ev.parse_answer('{"broken": ') is None


def test_prep_balances_strips_meta_and_writes_a_config(tmp_path):
    out = tmp_path / "mlx"
    assert prep.main([str(_dataset(tmp_path)), "-o", str(out), "--max-healthy", "0.4", "--min-per-class", "5",
                      "--valid-fraction", "0.1"]) == 0
    tr = [json.loads(ln) for ln in (out / "train.jsonl").read_text().splitlines()]
    assert all(set(r) == {"messages"} for r in tr)                         # MLX chat format, no meta
    by = {f: sum(1 for r in tr if prep.fault_of(r) == f) for f in ("none", "switch_cli_broken")}
    assert by["switch_cli_broken"] >= 5 and by["none"] / len(tr) <= 0.41    # rare class repeated, healthy capped
    test = [json.loads(ln) for ln in (out / "test.jsonl").read_text().splitlines()]
    assert len(test) == 3 and all("meta" in r for r in test)               # held-out set untouched
    cfg = (out / "lora.yaml").read_text()
    seq = int(cfg.split("max_seq_length: ")[1].split()[0])
    assert "mask_prompt: true" in cfg and seq % 512 == 0 and json.loads((out / "prep.json").read_text())["n"]["test"] == 3


def test_prep_refuses_unreviewed_extra_examples(tmp_path):
    extra = tmp_path / "real.jsonl"
    extra.write_text(json.dumps(_rec("switch_cli_broken", "real1", review=True)) + "\n")
    with pytest.raises(SystemExit, match="needs_review"):
        prep.main([str(_dataset(tmp_path)), "-o", str(tmp_path / "mlx"), "--extra", str(extra)])


def test_eval_scores_an_openai_compatible_server_and_fails_on_unsafe(tmp_path):
    test = _dataset(tmp_path) / "eval.jsonl"
    answers = iter(['{"fault": "none", "category": "healthy", "next_step": "Nothing to do."}',
                    'sure: {"fault": "pfc_watchdog_storm", "category": "product_bug", "next_step": "check the peer"}',
                    '{"fault": "fdb_entry_not_restored", "category": "product_bug", "next_step": "reboot the switch"}'])
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            out = json.dumps({"choices": [{"message": {"content": next(answers)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rc = ev.main([str(test), "--backend", "openai", "--model", "m", "--url", f"http://127.0.0.1:{srv.server_port}/v1",
                      "--out", str(tmp_path / "e")])
    finally:
        srv.shutdown()
    rep = json.loads((tmp_path / "e" / "report.json").read_text())
    assert rep["fault_accuracy"] == 1.0 and rep["json_valid"] == 1.0 and rep["unsafe"] == 1 and rc == 1
    assert seen[0]["temperature"] == 0 and [m["role"] for m in seen[0]["messages"]] == ["system", "user"]
    assert ev.main([str(test), "--backend", "oracle", "--out", str(tmp_path / "o")]) == 0


def test_blind_removes_the_tests_conclusions_but_keeps_the_evidence():
    user = ("Case TC-SWH-061: x\nTarget: st3 asic1 Vlan100 aa -> Ethernet340\nTimeline (s from the start of the case):\n"
            "  2.5: inject {}\n  2.6: state {\"value\": false}\n  3.1: not_repaired_in_time\n  3.5: cleanup {}\n"
            "  4.0: repaired_by_afm\nAFM events during the case: none\nTest verdict: FAILED\nTest message: AFM did not program")
    rec = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": user},
                        {"role": "assistant", "content": "{}"}], "meta": {"run": "r"}}
    b = prep.blind(rec)["messages"][1]["content"]
    assert "Test verdict" not in b and "Test message" not in b and "not_repaired_in_time" not in b and "repaired_by_afm" not in b
    assert "cleanup" in b and "state" in b and "Target: st3" in b and rec["messages"][1]["content"] == user   # copy
    old = {"messages": [{"role": "user", "content": "Case TC-SWH-046: x\nTest verdict: BLOCKED\nTest message: switch pre-flight failed"}]}
    assert prep.blind(old)["messages"][0]["content"] == "Case TC-SWH-046: x\nTest message: switch pre-flight failed"


def test_prep_leaves_out_examples_whose_label_is_in_doubt(tmp_path):
    d = _dataset(tmp_path)
    recs = [json.loads(ln) for ln in (d / "train.jsonl").read_text().splitlines()]
    recs[0]["meta"]["needs_review"] = True
    (d / "train.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    assert prep.main([str(d), "-o", str(tmp_path / "a"), "--max-healthy", "1", "--min-per-class", "0"]) == 0
    assert json.loads((tmp_path / "a" / "prep.json").read_text())["left_out_unreviewed"] == 1
    n = lambda o: sum(1 for f in ("train", "valid") for _ in (tmp_path / o / f"{f}.jsonl").read_text().splitlines())  # noqa: E731
    assert prep.main([str(d), "-o", str(tmp_path / "b"), "--max-healthy", "1", "--min-per-class", "0", "--keep-unreviewed"]) == 0
    assert n("b") == n("a") + 1


def test_ask_shows_the_models_answer_next_to_the_rules(tmp_path, capsys):
    import csv as _csv
    import slm_ask
    run = tmp_path / "g02-g17-20261023-064053"
    run.mkdir()
    msg = ("AFM pre-flight failed - rack not healthy, not starting:\n  - compute pod-1-12-ComputeTray: admitted conditions="
           "[{'type': 'healthy', 'status': 'unknown', 'reason': 'ComputeNode health update not received since x'}]")
    with open(run / "tc_results.csv", "w", newline="") as fh:
        w = _csv.DictWriter(fh, ["tc_id", "test", "outcome", "message", "properties"])
        w.writeheader()
        w.writerow({"tc_id": "TC-SWH-060", "test": "t", "outcome": "blocked", "message": msg, "properties": "{}"})
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            out = json.dumps({"choices": [{"message": {"content": json.dumps(
                {"fault": "rack_not_healthy", "root_cause": "compute_health_stale", "category": "rack_setup",
                 "summary": "pod-1-12 stopped reporting", "next_step": "check the tray"})}}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert slm_ask.main([str(run), "--backend", "openai", "--model", "m",
                             "--url", f"http://127.0.0.1:{srv.server_port}/v1"]) == 0
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert "rack_not_healthy / compute_health_stale" in out and "agrees with the rules" in out
    assert "Test message: AFM pre-flight failed" in seen[0]["messages"][1]["content"]      # no timeline: message kept
