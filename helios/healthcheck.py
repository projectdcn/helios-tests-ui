"""Full-rack health check: which cases make up the gate, and how a run is judged GO / NO-GO.

Run it with ``tools/rack_health.py`` (or ``pytest tests -m healthcheck``) before every disruptive batch, after each one
(to confirm the rack recovered) and whenever the rack state is unclear. It is read-only: none of these cases is
disruptive.

Gates:
  blocker - the rack is not fit for disruptive tests while this fails (NO-GO)
  warn    - recorded and reported, does not block

Existing plan cases join the check through ``CASES`` (the plugin adds the ``healthcheck`` marker at collection);
the health-only checks HC-001..HC-006 and the AFM snapshot HC-000 carry the marker themselves.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

BLOCKER, WARN = "blocker", "warn"

# plan case -> gate (what each one proves, for the report)
CASES: dict[str, tuple[str, str]] = {
    # AFM control plane
    "TC-CTL-036": (BLOCKER, "AFM cluster healthy: quorum, leader, members started"),
    "TC-SEQ-010": (BLOCKER, "every compute tray registered in AFM with its accelerator ids"),
    "TC-CTL-090": (BLOCKER, "every ComputeNode admitted, healthy, GPUs ready"),
    "TC-CTL-094": (BLOCKER, "system vPOD online or degraded, not configurationfailed"),
    # sideband
    "TC-BU-002": (BLOCKER, "afm-agent service + container up on every switch"),
    "TC-SEQ-006": (BLOCKER, "node-agent control channels up"),
    "TC-BU-009": (BLOCKER, "one sb-node-agent per compute tray"),
    "TC-SBM-135": (BLOCKER, "node agents spread over the switches"),
    "TC-SBM-136": (BLOCKER, "node agents on every switch"),
    # fabric
    "TC-SWH-030": (BLOCKER, "interfaces admin/oper state as configured"),
    "TC-SWH-037": (BLOCKER, "fabric port config matches the cabling (no routed / down GPU ports)"),
    "TC-SWH-049": (BLOCKER, "fabric port state healthy (link training, FEC, no flaps)"),
    "TC-BU-011": (BLOCKER, "every enabled GPU port LINK_UP on the computes"),
    "TC-BU-003": (BLOCKER, "IFCP session on every LINK_UP GPU port (compute view)"),
    "TC-001": (BLOCKER, "IFCP session on every LINK_UP GPU port (AFM view)"),
    "TC-AFM-001": (BLOCKER, "L2 ping within and across trays: no loss, no black-holed port"),
    # switch software
    "TC-SWH-041": (BLOCKER, "switch critical containers running, no recent restarts"),
    # warnings
    "TC-SWH-042": (WARN, "no unexpected switch reboot, no new core files"),
    "TC-SWH-057": (WARN, "critical processes running inside the switch containers"),
    "TC-CTL-035": (WARN, "NTP synchronised, clock skew within limit"),
    "TC-SWH-043": (WARN, "no kernel / hardware errors on the switches"),
    "TC-SWH-002": (WARN, "switch fan speeds in range"),
    "TC-SWH-021": (WARN, "switch power: output voltage"),
    "TC-SWH-022": (WARN, "switch power: output current"),
    "TC-SWH-023": (WARN, "switch power: power draw"),
    "TC-SWH-024": (WARN, "switch power: total vs capacity"),
    "TC-SWH-025": (WARN, "switch power: temperature"),
    "TC-SWH-055": (WARN, "switch sensors within thresholds"),
    "TC-SWH-034": (WARN, "no RX/TX errors on fabric interfaces"),
    "TC-SWH-052": (WARN, "no queue drops growing on fabric queues"),
    "TC-SWH-047": (WARN, "running configuration saved"),
    "TC-SWH-056": (WARN, "switch disk / memory / load"),
    "TC-SWH-038": (WARN, "switches configured consistently"),
    "TC-SWH-059": (WARN, "firmware consistent across switches"),
}

# health-only checks (tests/health, tests/afm/test_afm_health.py): gate + meaning
HEALTH_ONLY: dict[str, tuple[str, str]] = {
    "HC-000": (BLOCKER, "AFM rack state (cluster, admission, system vPOD, pending ops) + baseline snapshot"),
    "HC-001": (BLOCKER, "every switch and compute tray answers SSH"),
    "HC-002": (WARN, "every tray's BMC reports chassis power on"),
    "HC-003": (BLOCKER, "afmctl: every GPU device present and ACTIVE"),
    "HC-004": (BLOCKER, "no other workload using the GPUs"),
    "HC-005": (WARN, "no GPU ECC / RAS or kernel hardware errors on the trays"),
    "HC-006": (WARN, "tray disk and memory headroom"),
}


def gate_of(tc: str) -> str | None:
    hit = CASES.get(tc) or HEALTH_ONLY.get(tc)
    return hit[0] if hit else None


def meaning_of(tc: str) -> str:
    hit = CASES.get(tc) or HEALTH_ONLY.get(tc)
    return hit[1] if hit else ""


BAD = ("FAIL", "BLOCKED", "PARTIAL")      # PARTIAL: passed only on some trays - not healthy


@dataclass
class Judgement:
    verdict: str                                         # GO / NO-GO / INCOMPLETE
    blockers: list[tuple[str, str, str]] = field(default_factory=list)    # (tc, verdict, message)
    warnings: list[tuple[str, str, str]] = field(default_factory=list)
    known: list[tuple[str, str, str]] = field(default_factory=list)       # accepted failures (tc, verdict, reason)
    not_checked: list[tuple[str, str]] = field(default_factory=list)      # blocker cases skipped / missing (tc, why)
    passed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "blockers": self.blockers, "warnings": self.warnings, "known": self.known,
                "not_checked": self.not_checked, "passed": self.passed}


def judge(verdicts: dict[str, str], messages: dict[str, str] | None = None,
          known: dict[str, str] | None = None, expected: set[str] | None = None) -> Judgement:
    """verdicts {tc: PASS/FAIL/...} from a health run -> GO / NO-GO / INCOMPLETE.

    NO-GO: a blocker case FAIL / BLOCKED / PARTIAL that is not in ``known`` (tc -> accepted reason).
    INCOMPLETE: no NO-GO, but a blocker case was skipped or not run (``expected`` = the cases that should have run).
    GO: every blocker case passed (or is known). Warn-gate failures are listed, never block."""
    messages, known = messages or {}, known or {}
    expected = expected if expected is not None else {t for t in {**CASES, **HEALTH_ONLY} if gate_of(t) == BLOCKER}
    j = Judgement("GO")
    for tc in sorted(set(verdicts) | {t for t in expected if gate_of(t) == BLOCKER}):
        gate, v, msg = gate_of(tc), verdicts.get(tc), messages.get(tc, "")
        if gate is None:
            continue
        if v is None:
            if gate == BLOCKER:
                j.not_checked.append((tc, "not run"))
            continue
        if v in BAD:
            if tc in known:
                j.known.append((tc, v, known[tc]))
            elif gate == BLOCKER:
                j.blockers.append((tc, v, msg))
            else:
                j.warnings.append((tc, v, msg))
        elif v == "SKIP":
            if gate == BLOCKER:
                j.not_checked.append((tc, msg or "skipped"))
        else:
            j.passed.append(tc)
    j.verdict = "NO-GO" if j.blockers else "INCOMPLETE" if j.not_checked else "GO"
    return j


def read_run(run_dir: Path) -> tuple[dict[str, str], dict[str, str], dict]:
    """(verdict per tc, first failure / skip message per tc, HC-000 snapshot) from a pytest results folder."""
    verdicts = {r["tc_id"]: r["verdict"] for r in csv.DictReader(open(run_dir / "tc_summary.csv", newline=""))}
    messages: dict[str, str] = {}
    snapshot: dict = {}
    for r in csv.DictReader(open(run_dir / "tc_results.csv", newline="")):
        if r["outcome"] not in ("passed",) and r.get("message") and r["tc_id"] not in messages:
            messages[r["tc_id"]] = " ".join(r["message"].removeprefix("Skipped: ").split())[:400]
        if r["tc_id"] == "HC-000" and r.get("properties"):
            try:
                snapshot = json.loads(r["properties"]).get("snapshot") or {}
            except ValueError:
                pass
    return verdicts, messages, snapshot


def diff_snapshots(before: dict, after: dict, path: str = "") -> list[str]:
    """Human-readable differences between two HC-000 snapshots (nested dicts / lists / scalars)."""
    out: list[str] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for k in sorted(set(before) | set(after), key=str):
            p = f"{path}.{k}" if path else str(k)
            if k not in after:
                out.append(f"{p}: {before[k]!r} -> (gone)")
            elif k not in before:
                out.append(f"{p}: (new) -> {after[k]!r}")
            else:
                out += diff_snapshots(before[k], after[k], p)
    elif before != after:
        out.append(f"{path}: {before!r} -> {after!r}")
    return out
