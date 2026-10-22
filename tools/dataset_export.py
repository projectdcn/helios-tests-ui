"""Training data for a small language model (SLM) from test runs: one example per case result that has an evidence
timeline (helios/evidence.py) - TC-SWH-061 / 062 (FDB mismatch repair) and TC-SWH-046 (PFC / QoS, storms).

    python tools/dataset_export.py results/runs/vrack-g02-g17 -o dataset/all.jsonl      # every run folder below
    python tools/dataset_export.py RUN_DIR [RUN_DIR ...] -o out.jsonl --format prompt

Each example: the evidence as a field engineer sees it (case, target, timeline, AFM events, counters, verdict and the
test's message) -> a JSON diagnosis {fault, category, component, summary, next_step}.

Labels: from the faults the run injected when it is a virtual-rack dataset run (scenario.json in the run folder,
written by tools/vrack_dataset.py), else from rules on the evidence and the verdict. Both are computed; when they
disagree the example gets "needs_review": true (in meta) - check those before training. Real-rack examples are
always marked for review: the rules are a draft, a person confirms the diagnosis.

Formats: chat (default; {"messages": [system, user, assistant], "meta": {...}} - MLX-LM / TRL / Unsloth chat
fine-tuning) or prompt ({"prompt", "completion", "meta"}). Nothing secret is in the evidence (no passwords, tokens);
MAC / IP addresses of the rack are.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

csv.field_size_limit(10**9)

CASES = {"TC-SWH-061": "AFM repairs a missing static FDB entry (one AFM-programmed entry deleted on a switch)",
         "TC-SWH-062": "AFM removes a foreign static FDB entry (one entry AFM did not program added on a switch)",
         "TC-SWH-046": "PFC / QoS consistent on the fabric ports, no PFC pause storm, no PFC watchdog storm"}

SYSTEM = ("You diagnose AMD Helios rack test results (SONiC switch trays, AFM fabric manager, compute trays). Given "
          "the evidence a test recorded, answer with JSON only: {\"fault\", \"category\", \"component\", \"summary\", "
          "\"next_step\"}. fault: none | fdb_entry_not_restored | foreign_fdb_not_removed | pfc_pause_storm | "
          "pfc_watchdog_storm | qos_inconsistent | switch_cli_broken | afm_unreachable | test_error. category: healthy "
          "| product_bug | rack_setup | test_infra. Never suggest disruptive actions (flapping fabric ports, "
          "rebooting switches) as a next step; suggest what to check or who should act.")

# fault -> (category, component, summary template, next step)
DIAG = {
    "none": ("healthy", "-", "{detail}.", "Nothing to do."),
    "fdb_entry_not_restored": (
        "product_bug", "AFM FDB reconciliation",
        "AFM did not program the deleted static FDB entry {target} again within {limit} s; the test restored it.",
        "File an AFM bug with the entry, the timeline and the AFM controller log around the delete; check whether AFM "
        "logged an FDB mismatch at all."),
    "foreign_fdb_not_removed": (
        "product_bug", "AFM FDB reconciliation",
        "AFM did not remove the foreign static FDB entry {target} within {limit} s; the test removed it.",
        "File an AFM bug with the entry, the timeline and the AFM controller log; check whether AFM watches CONFIG_DB "
        "FDB entries it did not program."),
    "pfc_pause_storm": (
        "product_bug", "switch data path / PFC",
        "PFC pause frames keep growing with no traffic test running: {detail}.",
        "Find what sends the pauses: the peer of the listed port(s) (compute tray NIC or switch), its PFC / lossless "
        "settings and its queues; keep traffic off that port until explained."),
    "pfc_watchdog_storm": (
        "product_bug", "switch data path / PFC",
        "The PFC watchdog detected a storm: {detail}.",
        "Check the stormed queue's port and its peer (show pfcwd stats, show pfc counters); a device that never stops "
        "pausing needs its driver / firmware looked at."),
    "qos_inconsistent": (
        "rack_setup", "switch QoS configuration",
        "Fabric ports do not share one QoS map: {detail}.",
        "Apply the same PORT_QOS_MAP to every fabric port (config from the rack's QoS design), then re-run."),
    "switch_cli_broken": (
        "rack_setup", "switch software",
        "The SONiC CLI on {switch} refuses every command: {detail}. Not a test result.",
        "Stop what created the extra network namespaces (e.g. k3s / Kubernetes pods) and remove them; ip netns list "
        "must show only asic0, asic1 ...; then re-run."),
    "afm_unreachable": (
        "rack_setup", "AFM controller",
        "The AFM API cannot be reached: {detail}. Not a test result.",
        "Check afm.url in the inventory and that the controller runs at that address (controller coordinates, "
        "afm-controller container, port 443)."),
    "test_error": (
        "test_infra", "test / rack access",
        "The test could not run as designed: {detail}.",
        "Read the case log; fix access (SSH key, sudo) or the inventory, then re-run."),
}

# injected virtual-rack fault -> the fault each case must then report (None: the case is not affected)
INJECTED = {
    "no_fdb_reconcile": {"TC-SWH-061": "fdb_entry_not_restored", "TC-SWH-062": "foreign_fdb_not_removed"},
    "pfc_storm": {"TC-SWH-046": "pfc_watchdog_storm"},
    "cni_netns": {"TC-SWH-061": "switch_cli_broken", "TC-SWH-062": "switch_cli_broken", "TC-SWH-046": "switch_cli_broken"},
}
# when several apply, the one a case reports first (a broken CLI hides everything else)
PRIORITY = ["switch_cli_broken", "afm_unreachable", "test_error", "fdb_entry_not_restored", "foreign_fdb_not_removed",
            "pfc_watchdog_storm", "pfc_pause_storm", "qos_inconsistent", "none"]


def _load_rows(run: Path) -> list[dict]:
    f = run / "tc_results.csv"
    if not f.is_file():
        return []
    with open(f, newline="") as fh:
        return list(csv.DictReader(fh))


def _ev_of(row: dict) -> tuple[dict, dict]:
    try:
        props = json.loads(row.get("properties") or "{}")
    except ValueError:
        props = {}
    return props.get("evidence") or {}, props


def rule_fault(tc: str, outcome: str, msg: str, ev: dict) -> tuple[str, str]:
    """(fault, detail) from the evidence and the verdict alone."""
    m = msg or ""
    if "SONiC CLI on" in m and "refuses every command" in m:
        ns = m.split("network namespace", 1)[1].split()[0] if "network namespace" in m else "?"
        return "switch_cli_broken", f"network namespace {ns} is not an ASIC namespace"
    if (raw := re.search(r"validateNamespace: Namespace (\S+) is not a valid namespace", m)):
        # runs from before the one-line BLOCKED reason (22 Oct): the raw swsscommon traceback
        return "switch_cli_broken", f"network namespace {raw.group(1)} is not an ASIC namespace"
    if "AFM API" in m and "cannot be reached" in m:
        return "afm_unreachable", m.split(": ", 1)[1].split(". Check")[0][:200] if ": " in m else m[:200]
    if re.search(r"helios/afm\.py|requests/adapters\.py|urllib3/", m) and \
            re.search(r"ConnectionResetError|Connection reset by peer|ConnectTimeout|Connection refused|Max retries", m):
        # the AFM login traceback of runs from before the one-line BLOCKED reason (MR3 21 Oct)
        why = "connection reset" if "reset" in m.lower() else "connection refused" if "refused" in m else "timed out"
        return "afm_unreachable", f"the AFM API did not answer the login ({why})"
    if outcome in ("error", "blocked") and not ev.get("timeline"):
        return "test_error", (m.splitlines()[0] if m else outcome)[:200]
    # runs from before the evidence timeline (19 Oct and earlier): the check message says what happened
    if (fm := re.search(r"AFM did not program (\S+ \S+ FDB\|\S+) again within (\d+) s", m)):
        return "fdb_entry_not_restored", f"{fm.group(1)} not restored within {fm.group(2)} s"
    if (fm := re.search(r"AFM did not remove the foreign (\S+ \S+ FDB\|\S+) within (\d+) s", m)):
        return "foreign_fdb_not_removed", f"{fm.group(1)} not removed within {fm.group(2)} s"
    kinds = [x.get("kind") for x in ev.get("timeline", [])]
    if tc in ("TC-SWH-061", "TC-SWH-062"):
        if "not_repaired_in_time" in kinds:
            return ("fdb_entry_not_restored" if tc == "TC-SWH-061" else "foreign_fdb_not_removed"), ""
        if "repaired_by_afm" in kinds and outcome == "passed":
            t_inj = next((x["t"] for x in ev["timeline"] if x.get("kind") == "inject"), None)
            t_rep = next((x["t"] for x in ev["timeline"] if x.get("kind") == "repaired_by_afm"), None)
            took = f" in {t_rep - t_inj:.1f} s" if t_inj is not None and t_rep is not None else ""
            return "none", f"AFM repaired the FDB mismatch{took}"
        if outcome in ("error", "failed"):
            return "test_error", (m.splitlines()[0] if m else outcome)[:200]
        return "none", "nothing unusual recorded"
    if tc == "TC-SWH-046":
        samples = [x for x in ev.get("timeline", []) if x.get("kind") == "sample" and "ns" in x]
        storms = {f"{x['ns']} {q}": n for x in samples for q, n in (x.get("new_watchdog_storms") or {}).items()}
        pauses = [f"{x['ns']} {p['port']} {p['dir']} +{p['frames']}" for x in samples for p in x.get("top_pause_growth") or []]
        qos = (ev.get("facts") or {}).get("qos") or {}
        odd = {ns: q.get("variants") for ns, q in qos.items() if (q.get("variants") or 0) > 1}
        if storms:
            return "pfc_watchdog_storm", "storms on " + ", ".join(f"{k} (+{v})" for k, v in list(storms.items())[:4]) + \
                ("; pause frames " + ", ".join(pauses[:4]) if pauses else "")
        if pauses and outcome != "passed":
            return "pfc_pause_storm", "pause frames " + ", ".join(pauses[:4])
        if odd:
            return "qos_inconsistent", ", ".join(f"{ns}: {n} different maps" for ns, n in odd.items())
        if outcome in ("error", "failed"):
            return "test_error", (m.splitlines()[0] if m else outcome)[:200]
        cfg = ", ".join(f"{ns} pfc_enable {','.join(q.get('pfc_enable') or []) or 'none'}" for ns, q in qos.items())
        return "none", f"No PFC pause growth and no watchdog storm on {(ev.get('facts') or {}).get('switch', 'the switch')} " \
                       f"({cfg or 'no QoS map'})"
    return ("none" if outcome == "passed" else "test_error"), (m.splitlines()[0] if m else "")[:200]


def facts_from_message(m: str) -> dict:
    """Target facts of a run from before the evidence timeline, read from its message (switch, ns, VLAN, MAC, limit)."""
    f: dict = {}
    if (x := re.search(r"(?:program|foreign) (\S+) (asic\d+) FDB\|(\S+?)\|(\S+) (?:again )?within (\d+) s", m or "")):
        f.update(switch=x.group(1), ns=x.group(2), vlan=x.group(3), mac=x.group(4), fdb_reconcile_s=float(x.group(5)))
    elif (x := re.search(r"\[(st\d+|sw\d+)\] rc=|SONiC CLI on (\S+) refuses", m or "")):
        f["switch"] = x.group(1) or x.group(2)
    return f


def injected_fault(tc: str, faults: list[str], switch: str | None = None, scope: dict | None = None) -> str:
    """The fault ``tc`` must report given the injected ``faults``; ``scope`` {fault: [switches]} limits a fault to
    some switches (pfc_storm: the first one), checked against the switch the case looked at."""
    scope = scope or {}
    hits = [INJECTED[f][tc] for f in faults if f in INJECTED and tc in INJECTED[f]
            and (f not in scope or switch is None or switch in scope[f])]
    return min(hits, key=PRIORITY.index) if hits else "none"


def evidence_text(tc: str, row: dict, ev: dict, rack: str) -> str:
    f = ev.get("facts") or {}
    lines = [f"Case {tc}: {CASES.get(tc, row.get('test', ''))}", f"Rack: {rack}"]
    if f.get("switch"):
        tgt = " ".join(str(f[k]) for k in ("switch", "ns", "vlan", "mac") if f.get(k))
        lines.append(f"Target: {tgt}" + (f" -> {f['port']}" if f.get("port") else ""))
    for k in ("fdb_reconcile_s", "window_s", "max_pause_delta"):
        if k in f:
            lines.append(f"{k}: {f[k]}")
    if f.get("qos"):
        lines.append("QoS found: " + "; ".join(
            f"{ns}: {q.get('ports_with_qos_map')} ports with a QoS map, {q.get('variants')} variant(s), "
            f"pfc_enable {','.join(q.get('pfc_enable') or []) or 'none'}, watchdog queues {q.get('pfc_watchdog_queues', '?')}"
            for ns, q in f["qos"].items()))
    tl = ev.get("timeline") or []
    if tl:
        lines.append("Timeline (s from the start of the case):")
        for x in tl:
            rest = {k: v for k, v in x.items() if k not in ("t", "kind")}
            lines.append(f"  {x.get('t')}: {x.get('kind')} " + json.dumps(rest, sort_keys=True) if rest else
                         f"  {x.get('t')}: {x.get('kind')}")
    evs = f.get("afm_events")
    if evs is not None:
        lines.append("AFM events during the case: " + ("; ".join(f"{e['type']} {e['severity']} {e['message']}".strip()
                                                                 for e in evs) or "none"))
    lines.append(f"Test verdict: {row.get('outcome', '').upper()}")
    msg = (row.get("message") or "").strip()
    if msg:
        lines.append("Test message: " + " | ".join(ln.strip() for ln in msg.splitlines() if ln.strip())[:600])
    return "\n".join(lines)


def diagnosis(fault: str, detail: str, ev: dict) -> dict:
    cat, comp, summ, step = DIAG[fault]
    f = ev.get("facts") or {}
    target = " ".join(str(f[k]) for k in ("switch", "ns", "vlan", "mac") if f.get(k)) + \
        (f" -> {f['port']}" if f.get("port") else "")
    what = {"fdb_missing_entry": "the deleted FDB entry", "fdb_foreign_entry": "the foreign FDB entry",
            "pfc_qos": "PFC / QoS"}.get(f.get("case"), "the case")
    summary = summ.format(target=target or "?", limit=f.get("fdb_reconcile_s", "?"), detail=detail or "-",
                          switch=f.get("switch", "the switch"), what=what)
    return {"fault": fault, "category": cat, "component": comp, "summary": summary, "next_step": step}


def examples(run: Path, source: str | None = None) -> list[dict]:
    scen = {}
    if (run / "scenario.json").is_file():
        scen = json.loads((run / "scenario.json").read_text())
    out = []
    for row in _load_rows(run):
        tc = row.get("tc_id", "")
        ev, _props = _ev_of(row)
        if tc not in CASES or not (ev or row.get("outcome") in ("error", "blocked", "failed")):
            continue
        rack = scen.get("rack") or run.name.rsplit("-", 2)[0]
        rf, detail = rule_fault(tc, row.get("outcome", ""), row.get("message", ""), ev)
        if not (ev.get("facts") or {}):                 # an older run: what the message says about the target
            ev = {**ev, "facts": facts_from_message(row.get("message", ""))}
        src = source or ("vrack" if scen else "real")
        meta = {"tc_id": tc, "run": run.name, "test": row.get("test") or row.get("nodeid", ""), "source": src,
                "rule_fault": rf}
        if scen:
            inj = injected_fault(tc, scen.get("faults") or [], (ev.get("facts") or {}).get("switch"), scen.get("scope"))
            meta.update(injected=scen.get("faults") or [], qos=scen.get("qos"), seed=scen.get("seed"),
                        label_source="injected", needs_review=inj != rf)
            fault = inj
        else:
            fault = rf
            meta.update(label_source="rules", needs_review=True)
        out.append({"input": evidence_text(tc, row, ev, rack), "output": diagnosis(fault, detail, ev), "meta": meta})
    return out


def as_record(ex: dict, fmt: str) -> dict:
    answer = json.dumps(ex["output"], ensure_ascii=False)
    if fmt == "prompt":
        return {"prompt": f"{SYSTEM}\n\n{ex['input']}\n\nDiagnosis:", "completion": " " + answer, "meta": ex["meta"]}
    return {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": ex["input"]},
                         {"role": "assistant", "content": answer}], "meta": ex["meta"]}


def run_dirs(paths: list[Path]) -> list[Path]:
    out = []
    for p in paths:
        if (p / "tc_results.csv").is_file():
            out.append(p)
        elif p.is_dir():                     # a UI session folder only repeats its runs' results: left out
            out += sorted(d for d in p.iterdir() if d.is_dir() and (d / "tc_results.csv").is_file()
                          and "-session-" not in d.name)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", type=Path, help="run folders, or folders of run folders")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--format", choices=("chat", "prompt"), default="chat")
    ap.add_argument("--no-dedupe", action="store_true", help="keep examples whose input and answer repeat")
    a = ap.parse_args(argv)
    seen, n_dup, recs = set(), 0, []
    for run in run_dirs(a.paths):
        for ex in examples(run):
            h = hashlib.sha256((_strip_ids(ex["input"]) + json.dumps(ex["output"], sort_keys=True)).encode()).hexdigest()
            if h in seen and not a.no_dedupe:
                n_dup += 1
                continue
            seen.add(h)
            recs.append(as_record(ex, a.format))
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with open(a.output, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    review = sum(1 for r in recs if r["meta"].get("needs_review"))
    by = {}
    for r in recs:
        k = json.loads(r["messages"][-1]["content"] if a.format == "chat" else r["completion"])["fault"]
        by[k] = by.get(k, 0) + 1
    print(f"{a.output}: {len(recs)} examples ({n_dup} duplicates dropped, {review} need review); by fault: "
          + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
    return 0


def _strip_ids(text: str) -> str:
    """The input without run-specific timings, for de-duplication (same target and story = same example)."""
    return re.sub(r"^\s+[\d.]+: ", "  ", text, flags=re.M)


if __name__ == "__main__":
    sys.exit(main())
