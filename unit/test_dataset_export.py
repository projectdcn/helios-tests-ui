"""tools/dataset_export.py: SLM examples from case results with an evidence timeline - labels from the injected faults
(virtual-rack runs, scenario.json) or from rules (real runs, always marked for review); chat format; de-duplication."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import dataset_export as dx  # noqa: E402

FDB_OK = {"facts": {"case": "fdb_missing_entry", "switch": "st6", "ns": "asic0", "vlan": "Vlan100",
                    "mac": "02:00:00:07:03:16", "port": "Ethernet256", "fdb_reconcile_s": 300.0, "afm_events": []},
          "timeline": [{"t": 0.5, "kind": "state", "what": "fdb_present", "value": True},
                       {"t": 0.6, "kind": "inject", "action": "fdb_delete", "rc": 0},
                       {"t": 0.7, "kind": "state", "what": "fdb_present", "value": False},
                       {"t": 12.7, "kind": "state", "what": "fdb_present", "value": True},
                       {"t": 12.7, "kind": "repaired_by_afm"}]}
FDB_BAD = {"facts": dict(FDB_OK["facts"]),
           "timeline": FDB_OK["timeline"][:3] + [{"t": 300.9, "kind": "not_repaired_in_time"},
                                                 {"t": 301.0, "kind": "cleanup", "action": "fdb_set by the test"}]}


def _pfc(switch: str, storm: bool) -> dict:
    samples = [{"t": 21.5, "kind": "sample", "ns": "asic0", "what": "after", "ports_with_pause_growth": int(storm),
                "top_pause_growth": [{"port": "Ethernet64", "dir": "Rx", "frames": 40000}] if storm else [],
                "new_watchdog_storms": {"Ethernet64:3": 2} if storm else {}}]
    return {"facts": {"case": "pfc_qos", "switch": switch, "window_s": 30.0, "max_pause_delta": 0.0,
                      "qos": {"asic0": {"ports_with_qos_map": 216, "variants": 1, "pfc_enable": ["0,1"]}}},
            "timeline": [{"t": 19.0, "kind": "sample", "what": "before"}] + samples}


def _run(tmp_path: Path, name: str, rows: list[tuple], scenario: dict | None = None) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    with open(d / "tc_results.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, ["tc_id", "test", "outcome", "message", "properties"])
        w.writeheader()
        for tc, outcome, msg, ev in rows:
            w.writerow({"tc_id": tc, "test": f"t::{tc}", "outcome": outcome, "message": msg,
                        "properties": json.dumps({"evidence": ev}) if ev else ""})
    if scenario is not None:
        (d / "scenario.json").write_text(json.dumps(scenario))
    return d


def _fault(ex) -> str:
    return ex["output"]["fault"]


def test_injected_labels_and_the_per_switch_scope(tmp_path):
    run = _run(tmp_path, "vrack-g02-g17-20261022-010000", [
        ("TC-SWH-061", "failed", "1 check(s) failed: AFM did not program ...", FDB_BAD),
        ("TC-SWH-046", "passed", "", _pfc("st2", storm=False)),          # the storm is on st1: st2 is healthy
    ], {"rack": "vrack-g02-g17", "faults": ["no_fdb_reconcile", "pfc_storm"], "scope": {"pfc_storm": ["st1"]},
        "seed": 7, "qos": "mr3"})
    exs = {e["meta"]["tc_id"]: e for e in dx.examples(run)}
    assert _fault(exs["TC-SWH-061"]) == "fdb_entry_not_restored" and exs["TC-SWH-061"]["output"]["category"] == "product_bug"
    assert _fault(exs["TC-SWH-046"]) == "none"
    assert all(e["meta"]["label_source"] == "injected" and not e["meta"]["needs_review"] for e in exs.values())
    assert "Ethernet256" in exs["TC-SWH-061"]["output"]["summary"] and "not_repaired_in_time" in exs["TC-SWH-061"]["input"]


def test_rules_label_real_runs_and_flag_a_disagreement(tmp_path):
    real = _run(tmp_path, "g02-g17-20261022-052902", [
        ("TC-SWH-061", "passed", "", FDB_OK),
        ("TC-SWH-046", "failed", "PFC watchdog storm detected", _pfc("st1", storm=True)),
        ("TC-SWH-062", "blocked", "switch pre-flight failed - the SONiC CLI on st6 refuses every command: network "
                                  "namespace cni-7cf2 is not an ASIC namespace (container networking ...)", None),
        ("TC-SWH-061", "blocked", "AFM pre-flight failed - the AFM API at https://x cannot be reached for 60 s: "
                                  "connection reset during the TLS handshake. Check afm.url", None),
    ])
    exs = dx.examples(real)
    assert [_fault(e) for e in exs] == ["none", "pfc_watchdog_storm", "switch_cli_broken", "afm_unreachable"]
    assert all(e["meta"]["label_source"] == "rules" and e["meta"]["needs_review"] for e in exs)
    assert "12.1 s" in exs[0]["output"]["summary"] and "cni-7cf2" in exs[2]["output"]["summary"]
    assert exs[2]["output"]["category"] == "rack_setup" and "flap" not in json.dumps([e["output"] for e in exs]).lower()
    # injected says healthy, the evidence shows a storm: kept with the injected label, marked for review
    odd = _run(tmp_path, "vrack-x-20261022-020000", [("TC-SWH-046", "failed", "", _pfc("st1", storm=True))],
               {"faults": [], "scope": {}})
    [e] = dx.examples(odd)
    assert _fault(e) == "none" and e["meta"]["needs_review"] and e["meta"]["rule_fault"] == "pfc_watchdog_storm"


def test_cli_writes_chat_jsonl_and_drops_repeats(tmp_path):
    for k in (1, 2):                    # the same story twice (only when it was first read differs): one example
        ev = json.loads(json.dumps(FDB_OK))
        ev["timeline"][0]["t"] += k / 10
        _run(tmp_path / "runs", f"vrack-g02-g17-2026102{k}-010000", [("TC-SWH-061", "passed", "", ev)],
             {"faults": [], "scope": {}})
    out = tmp_path / "all.jsonl"
    assert dx.main([str(tmp_path / "runs"), "-o", str(out)]) == 0
    [rec] = [json.loads(ln) for ln in out.read_text().splitlines()]
    roles = [m["role"] for m in rec["messages"]]
    assert roles == ["system", "user", "assistant"] and json.loads(rec["messages"][2]["content"])["fault"] == "none"
    assert dx.main([str(tmp_path / "runs"), "-o", str(out), "--format", "prompt", "--no-dedupe"]) == 0
    recs = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert len(recs) == 2 and recs[0]["prompt"].endswith("Diagnosis:") and recs[0]["completion"].startswith(" {")


def test_runs_from_before_the_evidence_timeline_are_labelled_from_their_message(tmp_path):
    """Older real runs: no evidence, only the message (raw tracebacks, check messages) - still labelled, with the
    target read from the message; UI session folders (copies of runs) left out."""
    tb_ns = ("helios/sonic.py:33: in sh\nE   helios.transport.CommandError: [st6] rc=1: show interfaces status -n asic0\n"
             "E   RuntimeError: :- validateNamespace: Namespace cni-7cf2 is not a valid namespace name in config file")
    tb_afm = (".venv/lib/python3.14/site-packages/urllib3/connectionpool.py:793: in urlopen\n"
              "E   ConnectionResetError: [Errno 54] Connection reset by peer")
    chk = "1 check(s) failed:\n  - AFM did not program st6 asic0 FDB|Vlan200|02:00:00:11:00:14 again within 300 s (FDB mismatch not repaired)"
    root = tmp_path / "runs"
    _run(root, "g02-g17-20261019-000533", [("TC-SWH-061", "failed", chk, None), ("TC-SWH-046", "failed", tb_ns, None)])
    _run(root, "mr3-20261021-205810", [("TC-SWH-062", "error", tb_afm, None)])
    _run(root, "mr3-session-20261021-205636", [("TC-SWH-062", "error", tb_afm, None)])
    exs = [e for run in dx.run_dirs([root]) for e in dx.examples(run)]
    got = {(e["meta"]["run"], e["meta"]["tc_id"]): e["output"] for e in exs}
    assert set(got) == {("g02-g17-20261019-000533", "TC-SWH-061"), ("g02-g17-20261019-000533", "TC-SWH-046"),
                        ("mr3-20261021-205810", "TC-SWH-062")}
    fdb = got[("g02-g17-20261019-000533", "TC-SWH-061")]
    assert fdb["fault"] == "fdb_entry_not_restored" and "st6 asic0 Vlan200 02:00:00:11:00:14" in fdb["summary"] and "300" in fdb["summary"]
    cli = got[("g02-g17-20261019-000533", "TC-SWH-046")]
    assert cli["fault"] == "switch_cli_broken" and "st6" in cli["summary"] and "cni-7cf2" in cli["summary"]
    assert got[("mr3-20261021-205810", "TC-SWH-062")]["fault"] == "afm_unreachable"


def test_a_long_traceback_keeps_its_cause():
    """22 Oct: the first 600 characters of the switch traceback held only stack frames; the excerpt keeps the failing
    call and the error lines, so 'validateNamespace: Namespace cni-...' reaches the model."""
    frames = "\n".join(f".venv/lib/python3.14/site-packages/x/mod{k}.py:{k}: in f{k}\n    call_{k}(arg)" for k in range(40))
    msg = (f"tests/l1/test_switch_health.py:390: in test_pfc\n{frames}\n"
           "E   helios.transport.CommandError: [st6] rc=1: show pfc counters -n asic0\n"
           "E   RuntimeError: :- validateNamespace: Namespace cni-7cf2 is not a valid namespace name in config file")
    ex = dx.message_excerpt(msg)
    assert len(ex) <= 700 and "validateNamespace: Namespace cni-7cf2" in ex and ex.startswith("tests/l1/test_switch_health.py:390")
    assert dx.message_excerpt("1 check(s) failed:\n  - short") == "1 check(s) failed: | - short"
