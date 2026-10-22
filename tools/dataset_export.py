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
         "TC-SWH-046": "PFC / QoS consistent on the fabric ports, no PFC pause storm, no PFC watchdog storm",
         "TC-SWH-060": "Congestion on incast: several trays send to one tray at once; the switch ports towards it must "
                       "show the congestion (queue watermarks), PFC must hold lossless traffic (no drops)",
         "TC-SWH-063": "Lossless (PFC) configuration complete on the switch: headroom, maps, ECN, PFC watchdog"}

FAULTS = ["none", "fdb_entry_not_restored", "foreign_fdb_not_removed", "fdb_restored_wrong_port", "pfc_pause_storm",
          "pfc_watchdog_storm", "qos_inconsistent", "congestion_drops_lossy", "congestion_drops_lossless",
          "no_congestion_seen", "incast_not_generated", "lossless_config_incomplete", "switch_cli_broken",
          "afm_unreachable", "test_error"]
ROOT_CAUSES = ["none", "afm_did_not_detect", "afm_detected_not_repaired", "afm_programmed_wrong_port", "unknown",
               "peer_sends_pauses", "port_qos_map_differs", "traffic_class_not_lossless", "pfc_did_not_hold",
               "watermark_not_reported", "load_too_low", "no_lossless_headroom", "dot1p_unmapped",
               "lossy_on_lossless_queue", "no_pfc_watchdog", "no_ecn_marking", "foreign_network_namespace",
               "afm_api_down"]

SYSTEM = ("You diagnose AMD Helios rack test results (SONiC switch trays, AFM fabric manager, compute trays). Given "
          "the evidence a test recorded, answer with JSON only: {\"fault\", \"root_cause\", \"category\", "
          "\"component\", \"summary\", \"next_step\"}. fault: " + " | ".join(FAULTS) + ". root_cause: "
          + " | ".join(ROOT_CAUSES) + ". category: healthy | product_bug | rack_setup | test_infra. The summary names "
          "what the evidence shows (switch, port, peer, counters). Never suggest disruptive actions (flapping fabric "
          "ports, rebooting switches) as a next step; suggest what to check or who should act.")

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
    "fdb_restored_wrong_port": (
        "product_bug", "AFM FDB reconciliation",
        "AFM noticed the deleted FDB entry {target} but programmed it on the wrong port: {detail}; the test put it back.",
        "Raise an AFM defect: the entry, the port AFM chose and the right one (from the compute / plane mapping), the "
        "controller log of the repair."),
    "congestion_drops_lossy": (
        "rack_setup", "switch QoS / lossless configuration",
        "The incast dropped packets: {detail}.",
        "Make the traffic class lossless (pfc_enable for its priority, dot1p / TC maps, headroom - see TC-SWH-063) "
        "and re-run the incast."),
    "congestion_drops_lossless": (
        "product_bug", "switch data path / PFC",
        "Packets were dropped on a lossless class during the incast although PFC is configured: {detail}.",
        "Check the lossless PG headroom (xoff) and buffer pools against the cable lengths and port speed; raise it with "
        "the switch / ASIC team with the counters."),
    "no_congestion_seen": (
        "product_bug", "switch telemetry",
        "The incast ran at full rate but the switch reported no congestion: {detail}.",
        "Check that queue watermarks are collected and refreshed (watermark interval, counter polling) on those ASICs; "
        "compare with show queue counters during a load."),
    "incast_not_generated": (
        "test_infra", "traffic generation",
        "No real incast was made, so congestion was not judged: {detail}.",
        "Check the TransferBench jobs (ranks, ports, GPUs openable) and the trays' links; re-run when enough trays "
        "can send at line rate."),
    "lossless_config_incomplete": (
        "rack_setup", "switch QoS / lossless configuration",
        "The lossless configuration of {switch} is incomplete: {detail}.",
        "Complete it from the rack's QoS design (headroom, dot1p / TC maps, ECN / WRED, PFC watchdog) and re-run "
        "TC-SWH-063 and the incast case."),
    "test_error": (
        "test_infra", "test / rack access",
        "The test could not run as designed: {detail}.",
        "Read the case log; fix access (SSH key, sudo) or the inventory, then re-run."),
}

# more ways to say the same diagnosis (summary, next step): an example uses one, chosen by a hash of its input, so
# the model learns the facts rather than one fixed sentence (one wording everywhere made it repeat phrases and
# invent "AFM repaired the FDB mismatch in 0.1 s" on real inputs it had not seen, 22 Oct)
PHRASES = {
    "none": [("{detail}.", "No action needed."), ("Healthy: {detail}.", "Nothing to do; keep the run as a reference.")],
    "fdb_entry_not_restored": [
        ("The static FDB entry {target} was deleted and AFM never re-programmed it within {limit} s; the test put it "
         "back itself.", "Raise an AFM defect with this entry and timeline, plus the controller log from the moment of "
                         "the delete; first check whether AFM noticed the FDB mismatch at all."),
        ("FDB reconciliation failed: {target} stayed missing for the whole {limit} s window after the delete (restored "
         "by the test, not by AFM).", "Report it to the AFM team with the controller logs; see if any FDB-mismatch "
                                      "event or log line was produced.")],
    "foreign_fdb_not_removed": [
        ("A static FDB entry AFM never programmed ({target}) stayed on the switch for {limit} s; AFM did not remove it "
         "and the test cleaned it up.", "Raise an AFM defect: include the entry, the timeline and the controller log; "
                                        "check whether AFM watches FDB entries it did not create."),
        ("FDB reconciliation failed: the foreign entry {target} was still present after {limit} s (removed by the "
         "test, not by AFM).", "Report it to the AFM team with the controller logs and this timeline.")],
    "pfc_pause_storm": [
        ("Pause frames keep arriving while no traffic test runs: {detail}.",
         "Identify the sender of the pauses (the peer of those ports), check its lossless / PFC settings and queues; "
         "keep traffic away from the port meanwhile.")],
    "pfc_watchdog_storm": [
        ("PFC watchdog storm detected: {detail}.", "Look at the stormed queue's port and the device behind it (show "
                                                    "pfcwd stats, show pfc counters); a peer that never stops pausing "
                                                    "needs its NIC / switch driver or firmware investigated."),
        ("The switch saw a PFC storm - {detail}.", "Find which peer keeps pausing that port and why (its PFC config, "
                                                   "driver, firmware); do not run traffic through it until explained.")],
    "qos_inconsistent": [
        ("The fabric ports carry different QoS maps: {detail}.", "Bring every fabric port to the one PORT_QOS_MAP of "
                                                                  "the rack's QoS design, then re-run.")],
    "switch_cli_broken": [
        ("{switch}'s SONiC CLI fails on every command ({detail}); the case did not test anything.",
         "Whoever runs containers / Kubernetes on the switch must stop them and delete the extra network namespaces "
         "(only asic0, asic1 ... may be listed by ip netns list); re-run afterwards."),
        ("Rack setup problem on {switch}: {detail}, so SONiC's database connector rejects every show command.",
         "Remove the foreign network namespaces (e.g. cni-* from k3s / CNI) on the switch and stop what creates them; "
         "then repeat the case.")],
    "afm_unreachable": [
        ("No AFM API to talk to: {detail}. The case never ran.", "Verify afm.url and that the AFM controller is up at "
                                                                 "that address (controller coordinates, afm-controller "
                                                                 "container, port 443); then re-run."),
        ("AFM could not be reached ({detail}) - a rack setup problem, not a result.",
         "Check where the AFM controller runs now and whether it serves HTTPS on 443; fix the inventory's afm.url or "
         "the controller, then repeat.")],
    "fdb_restored_wrong_port": [
        ("AFM repaired the FDB mismatch of {target} on the wrong port ({detail}).",
         "Report it to the AFM team: entry, expected and chosen port, controller log.")],
    "congestion_drops_lossy": [
        ("Incast drops on a class without PFC: {detail}.", "Configure the traffic's priority as lossless end to end, "
                                                            "then repeat the incast.")],
    "congestion_drops_lossless": [
        ("PFC did not hold the incast: {detail}.", "Review lossless headroom and pool sizes; escalate to the switch "
                                                   "team with these counters.")],
    "no_congestion_seen": [
        ("Full-rate incast, but no watermark rise on the destination ports: {detail}.",
         "Verify watermark telemetry on those ASICs before trusting congestion results.")],
    "incast_not_generated": [
        ("Congestion not judged - the load never reached an incast: {detail}.",
         "Fix the traffic generation (TransferBench ranks / trays) and re-run.")],
    "lossless_config_incomplete": [
        ("{switch}: lossless setup has gaps - {detail}.", "Fill the gaps from the QoS design, then re-run TC-SWH-063.")],
    "test_error": [
        ("The case could not run: {detail}.", "Check the case log, then fix rack access (SSH key, sudo) or the "
                                              "inventory and re-run.")],
}

# injected virtual-rack fault -> the fault each case must then report (None: the case is not affected)
INJECTED = {
    "no_fdb_reconcile": {"TC-SWH-061": "fdb_entry_not_restored", "TC-SWH-062": "foreign_fdb_not_removed"},
    "fdb_detect_no_repair": {"TC-SWH-061": "fdb_entry_not_restored", "TC-SWH-062": "foreign_fdb_not_removed"},
    "fdb_wrong_port": {"TC-SWH-061": "fdb_restored_wrong_port"},
    "pfc_storm": {"TC-SWH-046": "pfc_watchdog_storm"},          # params watchdog false: pfc_pause_storm
    "qos_odd_port": {"TC-SWH-046": "qos_inconsistent"},
    "lossless_incast_drops": {"TC-SWH-060": "congestion_drops_lossless"},
    "no_congestion_watermark": {"TC-SWH-060": "no_congestion_seen"},
    "tb_slow": {"TC-SWH-060": "incast_not_generated"},
    "afm_unreachable": {"TC-SWH-061": "afm_unreachable", "TC-SWH-062": "afm_unreachable", "TC-SWH-060": "afm_unreachable"},
    "cni_netns": {t: "switch_cli_broken" for t in ("TC-SWH-061", "TC-SWH-062", "TC-SWH-046", "TC-SWH-060", "TC-SWH-063")},
    # the switch QoS profile of the virtual rack (selftest/fake_qos.py), as a pseudo fault "qos:<profile>"
    "qos:none": {"TC-SWH-060": "congestion_drops_lossy"},
    "qos:mr3": {"TC-SWH-063": "lossless_config_incomplete"},
}
# when several apply, the one a case reports first (a broken CLI hides everything else)
PRIORITY = ["switch_cli_broken", "afm_unreachable", "test_error", "incast_not_generated", "no_congestion_seen",
            "congestion_drops_lossy", "congestion_drops_lossless", "fdb_restored_wrong_port", "fdb_entry_not_restored",
            "foreign_fdb_not_removed", "pfc_watchdog_storm", "pfc_pause_storm", "qos_inconsistent",
            "lossless_config_incomplete", "none"]


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
    if tc == "TC-SWH-060" and m.startswith("incast pre-flight failed"):
        return _rule_congestion(outcome, m, ev.get("facts") or {})
    if outcome in ("error", "blocked") and not ev.get("timeline"):
        return "test_error", (m.splitlines()[0] if m else outcome)[:200]
    # runs from before the evidence timeline (19 Oct and earlier): the check message says what happened
    if (fm := re.search(r"AFM did not program (\S+ \S+ FDB\|\S+) again within (\d+) s", m)):
        return "fdb_entry_not_restored", f"{fm.group(1)} not restored within {fm.group(2)} s"
    if (fm := re.search(r"AFM did not remove the foreign (\S+ \S+ FDB\|\S+) within (\d+) s", m)):
        return "foreign_fdb_not_removed", f"{fm.group(1)} not removed within {fm.group(2)} s"
    f = ev.get("facts") or {}
    if tc == "TC-SWH-060":
        return _rule_congestion(outcome, m, f)
    if tc == "TC-SWH-063":
        fs = [x for q in (f.get("pfc_lossless") or {}).values() for x in q.get("findings") or []]
        if fs:
            return "lossless_config_incomplete", "; ".join(dict.fromkeys(fs))[:400]
        if outcome in ("error", "failed"):
            return "test_error", (m.splitlines()[0] if m else outcome)[:200]
        return "none", f"lossless configuration of {f.get('switch', 'the switch')} complete"
    if (wp := re.search(r"came back on (\S+), was (\S+)", m)):
        return "fdb_restored_wrong_port", f"on {wp.group(1)} instead of {wp.group(2)}"
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


def _rule_congestion(outcome: str, m: str, f: dict) -> tuple[str, str]:
    """TC-SWH-060 from its recorded evidence: drops (lossless / lossy), watermarks, pauses, load."""
    if m.startswith("incast pre-flight failed"):
        return "incast_not_generated", m.split(" - ", 1)[1].split(" - congestion not judged")[0].split(": no incast")[0][:240] \
            if " - " in m else m[:240]
    ev = f.get("incast_evidence") or {}
    if not ev:
        return ("test_error", (m.splitlines()[0] if m else outcome)[:200]) if outcome != "passed" else ("none", "no evidence")
    qd, pd = ev.get("queue_drops") or {}, ev.get("pg_drops_towards_sources") or {}
    tot = lambda d, k: float(((d.get(k) or {}).get("total")) or 0)  # noqa: E731
    ll, ly = tot(qd, "lossless") + tot(pd, "lossless"), tot(qd, "lossy") + tot(pd, "lossy")
    tq, lossless = ev.get("traffic_queue", "?"), ev.get("traffic_queue_lossless")
    prios = sorted({x for c in (ev.get("lossless_classes") or {}).values() for x in c.get("prios") or []})
    pause = float((((ev.get("pfc_pause_frames") or {}).get("Tx_towards_sources")) or {}).get("total") or 0)
    tr = f.get("traffic") or {}
    load = f"{tr.get('total_GBps', '?')} GB/s into {tr.get('destination_line_rate_Gbps', '?')} Gb/s " \
           f"(utilisation {tr.get('destination_utilisation', '?')})"
    if ll > 0:
        return "congestion_drops_lossless", f"{ll:.0f} drops on lossless {tq} (pfc_enable {','.join(map(str, prios)) or '?'}), " \
                                            f"{pause:.0f} pause frames sent towards the sources, load {load}"
    if ly > 0:
        why = "no priority is lossless (no PFC configured)" if not prios else \
            f"the traffic is on {tq}, not one of the lossless classes (pfc_enable {','.join(map(str, prios))})"
        return "congestion_drops_lossy", f"{ly:.0f} drops on {tq}: {why}; load {load}"
    if "no congestion seen" in m:
        return "no_congestion_seen", m.split("no congestion seen", 1)[1].strip(" :")[:240]
    if outcome == "passed":
        wm = max(((ev.get("queue_watermark_peak_bytes") or {}).values()) or [0])
        return "none", f"PFC held the incast on {tq} ({'lossless' if lossless else 'lossy'}): {pause:.0f} pause frames " \
                       f"towards the sources, no drops, queue watermark peak {wm:.0f} B; load {load}"
    return "test_error", (m.splitlines()[0] if m else outcome)[:200]


def root_cause(fault: str, ev: dict, msg: str = "") -> str:
    """The root cause of ``fault`` from the evidence (one of ROOT_CAUSES)."""
    f = ev.get("facts") or {}
    if fault in ("fdb_entry_not_restored", "foreign_fdb_not_removed"):
        evs = f.get("afm_events")
        if evs is None:
            return "unknown"                          # an older run: no AFM events recorded
        return "afm_detected_not_repaired" if any("fdb" in str(e.get("type", "")).lower() for e in evs) \
            else "afm_did_not_detect"
    if fault == "lossless_config_incomplete":
        fs = " ".join(x for q in (f.get("pfc_lossless") or {}).values() for x in q.get("findings") or [])
        for pat, rc in (("no lossless ingress buffer", "no_lossless_headroom"), ("not mapped", "dot1p_unmapped"),
                        ("lossless profile", "lossy_on_lossless_queue"), ("no PFC watchdog", "no_pfc_watchdog"),
                        ("no ECN", "no_ecn_marking")):
            if pat in fs:
                return rc
        return "unknown"
    return {"none": "none", "fdb_restored_wrong_port": "afm_programmed_wrong_port",
            "pfc_watchdog_storm": "peer_sends_pauses", "pfc_pause_storm": "peer_sends_pauses",
            "qos_inconsistent": "port_qos_map_differs", "congestion_drops_lossy": "traffic_class_not_lossless",
            "congestion_drops_lossless": "pfc_did_not_hold", "no_congestion_seen": "watermark_not_reported",
            "incast_not_generated": "load_too_low", "switch_cli_broken": "foreign_network_namespace",
            "afm_unreachable": "afm_api_down"}.get(fault, "unknown")


def case_evidence(tc: str, row: dict, props: dict, ev: dict) -> dict:
    """TC-SWH-060 / 063 record their evidence as properties (incast_evidence, traffic ..., pfc_lossless): the same
    shape as an evidence timeline's facts."""
    if tc == "TC-SWH-060" and (props.get("incast_evidence") is not None or props.get("traffic")):
        keep = ("destination", "sources", "traffic", "incast_evidence", "loaded_watermark_top_bytes",
                "idle_watermark_top_bytes", "drops_during_load", "events_seen")
        return {"facts": {"case": "congestion", **{k: props[k] for k in keep if k in props}}}
    if tc == "TC-SWH-063" and props.get("pfc_lossless") is not None:
        sw = re.search(r"\[(\w+)\]$", row.get("test") or row.get("nodeid") or "")
        return {"facts": {"case": "lossless_config", "switch": sw.group(1) if sw else None, "pfc_lossless": props["pfc_lossless"]}}
    return ev


def facts_from_message(m: str) -> dict:
    """Target facts of a run from before the evidence timeline, read from its message (switch, ns, VLAN, MAC, limit)."""
    f: dict = {}
    if (x := re.search(r"(?:program|foreign) (\S+) (asic\d+) FDB\|(\S+?)\|(\S+) (?:again )?within (\d+) s", m or "")):
        f.update(switch=x.group(1), ns=x.group(2), vlan=x.group(3), mac=x.group(4), fdb_reconcile_s=float(x.group(5)))
    elif (x := re.search(r"\[(st\d+|sw\d+)\] rc=|SONiC CLI on (\S+) refuses", m or "")):
        f["switch"] = x.group(1) or x.group(2)
    return f


def injected_fault(tc: str, faults: list[str], switch: str | None = None, scope: dict | None = None,
                   params: dict | None = None) -> str:
    """The fault ``tc`` must report given the injected ``faults`` (and "qos:<profile>"); ``scope`` {fault:
    [switches]} limits a fault to some switches, checked against the switch the case looked at (cases that look at
    the whole rack - TC-SWH-060 - are not limited); ``params`` details a fault (pfc_storm watchdog false: the pauses
    grow without a watchdog storm)."""
    scope, params = scope or {}, params or {}
    hits = []
    for f in faults:
        if f not in INJECTED or tc not in INJECTED[f]:
            continue
        if f in scope and switch is not None and switch not in scope[f] and tc != "TC-SWH-060":
            continue
        hit = INJECTED[f][tc]
        if f == "pfc_storm" and (params.get("pfc_storm") or {}).get("watchdog") is False:
            hit = "pfc_pause_storm"
        hits.append(hit)
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
    if "entry_before" in f:
        lines.append(f"Entry before: port {(f.get('entry_before') or {}).get('port', '-')}")
    if "entry_restored_by_afm" in f:
        rb = f.get("entry_restored_by_afm") or {}
        lines.append("Entry after AFM's repair window: " + (f"port {rb.get('port')}" if rb else "absent"))
    if f.get("case") == "congestion":
        lines += _congestion_lines(f)
    if f.get("case") == "lossless_config":
        lines.append("Evidence (recorded by the case):")
        for ns, q in (f.get("pfc_lossless") or {}).items():
            lines.append(f"  {ns}: {q.get('ports_with_pfc')} ports with PFC, pfc_enable "
                         f"{','.join(q.get('pfc_enable') or []) or 'none'}, PFC watchdog on {q.get('pfc_watchdog_ports')} "
                         f"port(s), WRED profiles {','.join(q.get('wred_profiles') or []) or 'none'}")
            for x in q.get("findings") or []:
                lines.append(f"    finding: {x}")
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
        lines.append("Test message: " + message_excerpt(msg))
    return "\n".join(lines)


def _congestion_lines(f: dict) -> list[str]:
    ev, tr = f.get("incast_evidence") or {}, f.get("traffic") or {}
    out = ["Evidence (recorded by the case):"]
    if f.get("destination"):
        out.append(f"  destination {f['destination']}; sources {', '.join(f.get('sources') or [])}")
    if tr:
        out.append(f"  load: {tr.get('rounds', '?')} round(s), {tr.get('seconds', '?')} s, {tr.get('total_GBps', '?')} GB/s "
                   f"into {tr.get('destination_line_rate_Gbps', '?')} Gb/s line rate (utilisation "
                   f"{tr.get('destination_utilisation', '?')})" + (f", {tr['load']}" if tr.get("load") else "")
                   + (f", failed jobs {tr['failed_jobs']}" if tr.get("failed_jobs") else ""))
    if ev:
        cls = sorted({(tuple(c.get("prios") or []), tuple(c.get("queues") or [])) for c in (ev.get("lossless_classes") or {}).values()
                      if c.get("prios")})
        out.append(f"  traffic queue {ev.get('traffic_queue', '?')} (lossless: {ev.get('traffic_queue_lossless')}); "
                   f"lossless classes: " + ("; ".join(f"prios {','.join(map(str, p))} -> {','.join(q)}" for p, q in cls)
                                            or "none"))
        pf = ev.get("pfc_pause_frames") or {}
        tot = lambda d: f"{float((d or {}).get('total') or 0):.0f}"  # noqa: E731
        out.append(f"  PFC pause frames: Tx towards the sources {tot(pf.get('Tx_towards_sources'))}, Rx from the "
                   f"destination {tot(pf.get('Rx_from_destination'))}, other {tot(pf.get('other'))}")
        qd, pd = ev.get("queue_drops") or {}, ev.get("pg_drops_towards_sources") or {}
        out.append(f"  drops: queues towards the destination lossless {tot(qd.get('lossless'))} / lossy {tot(qd.get('lossy'))}; "
                   f"ingress PGs towards the sources lossless {tot(pd.get('lossless'))} / lossy {tot(pd.get('lossy'))}")
        wm = {k: v for k, v in (ev.get("queue_watermark_peak_bytes") or {}).items() if v}
        out.append("  queue watermark peaks: " + (", ".join(f"{k} {v:.0f} B" for k, v in wm.items()) or "all 0"))
    lw, iw = f.get("loaded_watermark_top_bytes") or {}, f.get("idle_watermark_top_bytes") or {}
    if lw or iw:
        out.append(f"  highest port watermark: idle {max(iw.values() or [0]):.0f} B, under load {max(lw.values() or [0]):.0f} B")
    if "drops_during_load" in f:
        d = f["drops_during_load"] or {}
        out.append("  ports with drops during the load: " + (", ".join(f"{k} +{v:.0f}" for k, v in list(d.items())[:4]) or "none"))
    if "events_seen" in f:
        out.append("  AFM events: " + (", ".join(f"{k} x{v}" for k, v in (f["events_seen"] or {}).items()) or "none"))
    return out


def message_excerpt(msg: str, limit: int = 700) -> str:
    """The test message in one line, at most ``limit`` characters. A traceback is cut to what says what went wrong:
    its error lines (pytest's 'E ...', 'XxxError: ...') and the failing call, not its first frames - the first 600
    characters of the 22 Oct switch traceback held none of 'validateNamespace: Namespace cni-...'."""
    ls = [ln.strip() for ln in msg.splitlines() if ln.strip()]
    if len(" | ".join(ls)) <= limit:
        return " | ".join(ls)
    err = [ln for ln in ls if ln.startswith("E ") or re.match(r"^[\w.]*(Error|Exception|Timeout)\b[:(]", ln)]
    if err:
        err = list(dict.fromkeys(e.lstrip("E ").strip() for e in err))      # each distinct error once
        where = next((ln for ln in ls if re.match(r"^[\w/.-]+\.py:\d+: in ", ln)), "")
        head = [where] if where else []
        out = " | ".join(head + err[-4:])                                   # the last errors are the cause
        return out if len(out) <= limit else out[:limit // 3] + " ... " + out[-(limit - limit // 3 - 5):]
    joined = " | ".join(ls)
    return joined[:limit // 2] + " ... " + joined[-(limit // 2 - 5):]


def diagnosis(fault: str, detail: str, ev: dict, variant_key: str = "") -> dict:
    """The expected answer; ``variant_key`` (the input text) picks one of the wordings (DIAG + PHRASES) stably."""
    cat, comp, summ, step = DIAG[fault]
    forms = [(summ, step)] + PHRASES.get(fault, [])
    if variant_key:
        summ, step = forms[int(hashlib.sha256(variant_key.encode()).hexdigest(), 16) % len(forms)]
    f = ev.get("facts") or {}
    target = " ".join(str(f[k]) for k in ("switch", "ns", "vlan", "mac") if f.get(k)) + \
        (f" -> {f['port']}" if f.get("port") else "")
    what = {"fdb_missing_entry": "the deleted FDB entry", "fdb_foreign_entry": "the foreign FDB entry",
            "pfc_qos": "PFC / QoS"}.get(f.get("case"), "the case")
    summary = summ.format(target=target or "?", limit=f.get("fdb_reconcile_s", "?"), detail=detail or "-",
                          switch=f.get("switch") or "the switch", what=what)
    return {"fault": fault, "root_cause": root_cause(fault, ev), "category": cat, "component": comp,
            "summary": summary, "next_step": step}


def examples(run: Path, source: str | None = None) -> list[dict]:
    scen = {}
    if (run / "scenario.json").is_file():
        scen = json.loads((run / "scenario.json").read_text())
    out = []
    for row in _load_rows(run):
        tc = row.get("tc_id", "")
        ev, props = _ev_of(row)
        ev = case_evidence(tc, row, props, ev)
        if tc not in CASES or row.get("outcome") == "skipped" or not (ev or row.get("outcome") in ("error", "blocked", "failed")):
            continue                     # a skipped case judged nothing (e.g. TC-SWH-063 on a switch without PFC)
        rack = scen.get("rack") or run.name.rsplit("-", 2)[0]
        rf, detail = rule_fault(tc, row.get("outcome", ""), row.get("message", ""), ev)
        if not (ev.get("facts") or {}):                 # an older run: what the message says about the target
            ev = {**ev, "facts": facts_from_message(row.get("message", ""))}
        src = source or ("vrack" if scen else "real")
        meta = {"tc_id": tc, "run": run.name, "test": row.get("test") or row.get("nodeid", ""), "source": src,
                "rule_fault": rf}
        if scen:
            faults = (scen.get("faults") or []) + ([f"qos:{scen['qos']}"] if scen.get("qos") else [])
            inj = injected_fault(tc, faults, (ev.get("facts") or {}).get("switch"), scen.get("scope"), scen.get("params"))
            meta.update(injected=scen.get("faults") or [], qos=scen.get("qos"), seed=scen.get("seed"),
                        label_source="injected", needs_review=inj != rf)
            fault = inj
        else:
            fault = rf
            meta.update(label_source="rules", needs_review=True)
        text = evidence_text(tc, row, ev, rack)
        out.append({"input": text, "output": diagnosis(fault, detail, ev, _strip_ids(text)), "meta": meta,
                    "facts": ev.get("facts") or {}})
    return out


# ------------------------------------------------------------------ older-style variants (training augmentation)

LEGACY_FAULTS = ("switch_cli_broken", "afm_unreachable", "fdb_entry_not_restored", "foreign_fdb_not_removed")
_FIRST_FRAME = {"TC-SWH-046": "tests/l1/test_switch_health.py:390: in test_pfc_qos_consistent_no_storm",
                "TC-SWH-061": "tests/afm/test_afm_switch_faults.py:421: in test_fdb_mismatch_missing_entry_restored",
                "TC-SWH-062": "tests/afm/test_afm_switch_faults.py:462: in test_fdb_mismatch_foreign_entry_removed"}


def legacy_message(fault: str, tc: str, facts: dict, rng) -> tuple[str, str]:
    """(outcome, message) of a run from before the one-line BLOCKED reasons and the evidence timeline, as the real
    19-22 Oct runs reported the same fault: a raw traceback or a check message."""
    sw = facts.get("switch") or rng.choice([f"st{n}" for n in range(1, 7)])
    ns = facts.get("ns") or rng.choice(["asic0", "asic1"])
    if fault == "switch_cli_broken":
        cmd = f"show pfc counters -n {ns}" if tc == "TC-SWH-046" else f"show interfaces status -n {ns} -d all"
        cni = "cni-%08x-%04x-%04x-%04x-%012x" % tuple(rng.getrandbits(b) for b in (32, 16, 16, 16, 48))
        return "failed", (f"{_FIRST_FRAME[tc]}\n    ...\nhelios/sonic.py:33: in sh\n    return self.run(cmd, **kw).check().stdout\n"
                          f"helios/transport.py:124: in check\n    raise CommandError(self)\n"
                          f"E   helios.transport.CommandError: [{sw}] rc=1: {cmd}\n"
                          f"E     File \"/usr/lib/python3/dist-packages/swsscommon/swsscommon.py\", line 2065, in get_db_list\n"
                          f"E       return _swsscommon.SonicV2Connector_Native_get_db_list(self)\n"
                          f"E   RuntimeError: :- validateNamespace: Namespace {cni} is not a valid namespace name in config file")
    if fault == "afm_unreachable":
        errno, err = rng.choice([(54, "ConnectionResetError: [Errno 54] Connection reset by peer"),
                                 (104, "ConnectionResetError: [Errno 104] Connection reset by peer"),
                                 (61, "ConnectionRefusedError: [Errno 61] Connection refused")])
        return "error", ("tests/afm/conftest.py:79: in afm\n    c.login()\nhelios/afm.py:122: in login\n    r = self.login_raw()\n"
                         ".venv/lib/python3.14/site-packages/urllib3/connectionpool.py:793: in urlopen\n"
                         f"E   {err}\nE   requests.exceptions.ConnectionError: ('Connection aborted.', "
                         f"{err.split(':')[0]}({errno}, '{err.split('] ')[1]}'))")
    vlan, mac = facts.get("vlan") or "Vlan100", facts.get("mac") or "02:00:00:%02x:%02x:%02x" % (
        rng.randrange(1, 18), rng.randrange(4), rng.randrange(1, 40))
    limit = rng.choice([300, 300, 120, 600])
    if fault == "fdb_entry_not_restored":
        return "failed", (f"1 check(s) failed:\n  - AFM did not program {sw} {ns} FDB|{vlan}|{mac} again within {limit} s "
                          "(FDB mismatch not repaired)")
    return "failed", (f"1 check(s) failed:\n  - AFM did not remove the foreign {sw} {ns} FDB|{vlan}|{mac} within {limit} s "
                      "(FDB mismatch not repaired)")


def legacy_variant(ex: dict, rng) -> dict | None:
    """The same fault of ``ex`` as an older run reported it (no timeline, a raw traceback or check message), or
    None for a fault no older run reported differently. Labelled like ``ex``; needs_review when the rules disagree."""
    fault, tc = ex["output"]["fault"], ex["meta"]["tc_id"]
    if fault not in LEGACY_FAULTS or tc not in _FIRST_FRAME:
        return None
    facts = {k: v for k, v in (ex.get("facts") or {}).items() if k in ("switch", "ns", "vlan", "mac")}
    outcome, msg = legacy_message(fault, tc, facts, rng)
    row = {"outcome": outcome, "message": msg}
    rf, detail = rule_fault(tc, outcome, msg, {})
    ev = {"facts": facts_from_message(msg)}
    rack = ex["input"].split("Rack: ", 1)[1].split("\n", 1)[0] if "Rack: " in ex["input"] else "?"
    text = evidence_text(tc, row, {}, rack)
    meta = {**ex["meta"], "variant": "legacy", "rule_fault": rf, "needs_review": rf != fault}
    return {"input": text, "output": diagnosis(fault, detail, ev, _strip_ids(text)), "meta": meta}


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
