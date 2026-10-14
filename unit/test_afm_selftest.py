"""Self-test of tests/afm against selftest/fake_afm.py: all cases pass on a healthy fake controller, and
each injected controller fault fails exactly the test-plan cases that must catch it.

The fault map was established by running the whole AFM suite once per fault (only these IDs flipped);
here each fault run is limited to its IDs with --tc to keep the unit suite fast.
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAULTS = {
    "cookie_flags": {"TC-CTL-022"},
    "cswsh": {"TC-AFM-AGGW-025"},
    "ws_rbac_leak": {"TC-AFM-AGGW-015", "TC-AFM-AGGW-016", "TC-AFM-AGGW-029", "TC-CTL-026"},
    "ws_rbac_stale": {"TC-AFM-AGGW-029", "TC-CTL-026"},
    "no_update_event": {"TC-AFM-AGGW-010", "TC-AFM-AGGW-011", "TC-AFM-AGGW-012", "TC-CTL-011"},
    "rbac_bypass": {"TC-AFM-043", "TC-CTL-045", "TC-CTL-046", "TC-AFM-RC-022", "TC-AFM-AGGW-015",
                    "TC-AFM-AGGW-016", "TC-AFM-AGGW-029", "TC-CTL-026"},
    "unauth_ok": {"TC-AFM-RC-021", "TC-CTL-089"},
    "audit_missing_delete": {"TC-AFM-048A", "TC-CTL-051"},
    "dup_vpod_ok": {"TC-CTL-044", "TC-CTL-096"},
    "system_name_ok": {"TC-E2E-021"},
    "ws_no_close": {"TC-AFM-AGGW-026", "TC-CTL-023"},
    "gpu_not_ready": {"TC-CTL-090"},
    "dup_acc": {"TC-CTL-090"},
    "quorum_down": {"TC-CTL-036", "TC-AFM-AGGW-028"},
    "metrics_missing": {"TC-AFM-053", "TC-CTL-048"},
    "minirack_drain_ok": {"TC-AFM-035A"},
}


def _run(tmp_path: Path, faults: str = "", tcs: set[str] | None = None,
         inventory: str = "inventory.yaml") -> dict[str, str]:
    results = tmp_path / "results"
    args = [sys.executable, "-m", "pytest", str(ROOT / "tests" / "afm"), "--inventory",
            str(ROOT / "selftest" / inventory), "--results-dir", str(results), "--allow-disruptive",
            "-q", "-p", "no:cacheprovider", "-o", "log_cli=false"]
    if tcs:
        args += ["--tc", ",".join(sorted(tcs))]
    env = {**os.environ, "HELIOS_FAKE_AFM_FAULTS": faults}
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=600, env=env)
    summaries = list(results.glob("*/tc_summary.csv"))
    assert summaries, f"no results written\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
    with open(summaries[0]) as f:
        return {row["tc_id"]: row["verdict"] for row in csv.DictReader(f)}


# Disruptive lifecycle and link cases against the 3-switch / 3-compute fake (selftest/inventory-afm3.yaml, switch
# "SSH" simulated by selftest/fake_rack.py). One product fault per entry; each run is limited to its IDs
# because a fault that breaks the rack correctly turns every later case BLOCKED.
LIFECYCLE_FAULTS = [
    ("stale_planes", {"TC-AFM-024"}),            # planes not restored after switch delete + readmit
    ("stale_planes", {"TC-AFM-033"}),
    ("vlan_wipe", {"TC-AFM-027"}),               # AIFM-1901
    ("no_readmit", {"TC-AFM-045"}),
    ("decom_in_vpod_ok", {"TC-AFM-047"}),
    ("configfailed_sticky", {"TC-AFM-036", "TC-AFM-038"}),
    ("configfailed_never", {"TC-AFM-036", "TC-AFM-038"}),
    ("sigstop_ignored", {"TC-AFM-007", "TC-AFM-RC-017"}),
    ("agent_stop_ignored", {"TC-AFM-019"}),
    ("no_respawn", {"TC-AFM-006"}),
    ("no_unreachable_event", {"TC-AFM-019"}),
    ("no_healthy_event", {"TC-AFM-007"}),
    ("decom_with_agent_down_ok", {"TC-AFM-032"}),
    ("reconfig_decom_ok", {"TC-AFM-RC-019", "TC-AFM-RC-020"}),
    ("unhealthy_reconf_ok", {"TC-AFM-RC-017"}),
    ("planes_no_drop", {"TC-AFM-023", "TC-AFM-026"}),
    # fabric links (tests/afm/test_afm_links.py)
    ("coupled_ports", {"TC-AFM-011"}),           # full rack: shutting one port also dropped a neighbour
    ("coupled_ports", {"TC-AFM-012"}),
    ("manual_auto", {"TC-AFM-016", "TC-AFM-041", "TC-AFM-042"}),   # manual policy reconfigures on its own
    ("timer_ignored", {"TC-AFM-040"}),           # PortFailed before link-fail-threshold
    ("no_reconcile", {"TC-AFM-017"}),            # link change during agent outage never learnt
    ("flap_counts_as_failure", {"TC-AFM-011"}),  # a flap shorter than the timer still marked failed
    # failover scenarios / vPOD operations (tests/afm/test_afm_failover.py); the multi-GPU and other-fault
    # cases in each list are controls that must keep passing
    ("one_failure_per_gpu", {"TC-E2E-105", "TC-E2E-122", "TC-E2E-130", "TC-E2E-131", "TC-E2E-135",
                             "TC-E2E-141", "TC-E2E-144"}),
    ("flap_counts_as_failure", {"TC-E2E-132"}),
    ("timer_ignored", {"TC-E2E-055"}),
    ("timer_ignored", {"TC-E2E-132"}),
    ("no_auto_restore", {"TC-E2E-056"}),
    ("no_auto_restore", {"TC-E2E-145"}),
    ("no_vpod_events", {"TC-E2E-025", "TC-E2E-106"}),
    ("no_vlan_for_vpod", {"TC-E2E-087"}),
    ("policy_delete_in_use", {"TC-E2E-040"}),
    ("policy_delete_in_use", {"TC-E2E-134"}),
    ("manual_auto", {"TC-E2E-052", "TC-E2E-146"}),
    # AFM API tranche 3: AggWatch filters/audit, controller resilience, snapshot/restore, regression, stress
    ("ws_rv_ignored", {'TC-AFM-AGGW-009', 'TC-CTL-009'}),
    ("no_watch_audit", {'TC-AFM-AGGW-017', 'TC-AFM-AGGW-018'}),
    ("authn_accepts_invalid", {'TC-BUG-033'}),
    ("events_unbounded", {'TC-EVT-038'}),
    ("rbac_bypass", {'TC-CTL-117'}),
    ("no_rejoin", {'TC-CTL-074'}),
    ("no_rejoin", {'TC-CTL-134'}),
    ("no_rejoin", {'TC-STR-010'}),
    ("no_rejoin", {'TC-AR-003'}),
    ("api_down_on_one_node", {'TC-CTL-133'}),
    ("no_logs_after_restart", {'TC-CTL-124'}),
    ("watch_breaks_on_nonleader", {'TC-AFM-AGGW-023'}),
    ("ws_no_reconnect_after_failover", {'TC-AFM-AGGW-024'}),
    ("restore_noop", {'TC-AFM-051', 'TC-CTL-039', 'TC-CTL-040'}),
    ("reconfig_clears_drain", {'TC-AFM-RC-028'}),
    ("controller_unstable", {'TC-BUG-001'}),
    ("controller_unstable", {'TC-BUG-004'}),
    ("switch_stuck_unknown", {'TC-BUG-007'}),
    ("ports_not_recovered", {'TC-BUG-016'}),
    ("gpu_dup_after_restart", {'TC-BUG-009'}),
    ("accid_changes_on_move", {'TC-BUG-014'}),
    ("reconcile_clears_failure", {'TC-BUG-025'}),
    ("configdone_stuck", {'TC-BUG-027'}),
    ("planes_no_drop", {'TC-BUG-030'}),
    ("no_alerts", {'TC-CTL-098'}),
    ("no_unreachable_event", {'TC-EVT-021'}),
    ("no_healthy_event", {'TC-AR-002'}),
    ("transient_zero_planes", {'TC-STR-023'}),
    ("coupled_ports", {'TC-STR-013'}),
    ("configdone_stuck", {'TC-STR-005'}),
    ("configdone_stuck", {'TC-STR-020'}),
    ("no_respawn", {'TC-AR-001'}),
    # compute side / IFCP / switch agent / traffic + AFM (tranche 4)
    ("stats_frozen", {"TC-HW-010"}),
    ("ifcp_no_recover", {"TC-002"}),
    ("ifcp_no_recover", {"TC-003"}),
    ("ifcp_no_recover", {"TC-004"}),
    ("ifcp_no_recover", {"TC-005"}),
    ("ifcp_no_recover", {"TC-007"}),
    ("ifcp_no_recover", {"TC-008"}),
    ("ifcp_no_recover", {"TC-TRG-006"}),
    ("ifcp_no_recover", {"TC-TRG-010"}),
    ("ifcp_baseline_down", {"TC-001"}),
    ("station_map_stale", {"TC-AFM-058"}),
    ("stale_fwd_table", {"TC-E2E-099"}),
    ("fwd_table_incomplete", {"TC-NA-048"}),
    ("phase_active_when_failed", {"TC-E2E-110"}),
    ("no_reset_on_reconfigure", {"TC-NA-047"}),
    ("no_reset_on_reconfigure", {"TC-HW-025"}),
    ("reset_not_logged", {"TC-AFM-RC-007"}),
    ("accid_new_on_readmit", {"TC-NA-018"}),
    ("decom_unhealthy_ok", {"TC-NA-013"}),
    ("no_reconfigure_event", {"TC-NA-022"}),
    ("no_agent_requests", {"TC-SEQ-012"}),
    ("computes_before_switches", {"TC-BU-001"}),
    ("duplicate_node_agent", {"TC-SEQ-005"}),
    ("decommission_wipes_user_vlans", {"TC-SWA-037"}),
    ("no_vlan_for_vpod", {"TC-SWA-180"}),
    ("agent_stop_ignored", {"TC-SWA-204"}),
    ("auto_needs_manual", {"TC-SWA-214"}),
    ("cp_disrupted_by_fabric_change", {"TC-TRG-004"}),
    ("cp_disrupted_by_fabric_change", {"TC-TRG-005"}),
    ("cp_disrupted_by_fabric_change", {"TC-TRG-009"}),
    ("no_compute_port_events", {"TC-AFM-009"}),
    ("no_compute_port_events", {"TC-E2E-092"}),
    ("unhealthy_reconf_ok", {"TC-NA-049"}),
    ("sigstop_ignored", {"TC-NA-024"}),
    ("admission_reconfigure_500", {"TC-NA-050"}),
    ("reconfigure_500", {"TC-NA-040"}),
    ("reconfigure_500", {"TC-RAS-reset-010"}),
    ("reconfigure_500", {"TC-RAS-reset-012"}),
    ("reconfigure_500", {"TC-RAS-reset-014"}),
    ("reconfigure_500", {"TC-RAS-reset-016"}),
    ("configdone_stuck", {"TC-RAS-reset-005"}),
    ("configdone_stuck", {"TC-RAS-reset-006"}),
    ("no_readmit", {"TC-NA-019"}),
    ("configdone_stuck", {"TC-NA-016"}),
]
# controls run alongside a fault and must still PASS (the fault is outside what they check)
CONTROLS = {"one_failure_per_gpu": {"TC-E2E-048", "TC-E2E-137"}, "no_vpod_events": {"TC-E2E-030"},
            "manual_auto": {"TC-E2E-133"}}


def test_healthy_fake_all_pass(tmp_path):
    verdicts = _run(tmp_path)
    bad = {k: v for k, v in verdicts.items() if v not in ("PASS", "SKIP")}
    assert not bad, bad
    assert sum(v == "PASS" for v in verdicts.values()) >= 200


def test_healthy_fake_3switch_all_pass(tmp_path):
    verdicts = _run(tmp_path, inventory="inventory-afm3.yaml")
    optional = {"TC-AFM-035A",       # one-switch racks only
                "TC-AFM-AGGW-002",   # needs a bearer token (thresholds.afm.bearer_token_env)
                "TC-CTL-055"}        # one-time recovery key, opt-in (thresholds.afm.allow_recovery_key)
    bad = {k: v for k, v in verdicts.items() if v != "PASS" and not (k in optional and v == "SKIP")}
    assert not bad, bad
    assert len(verdicts) >= 233


@pytest.mark.parametrize("fault,tcs", LIFECYCLE_FAULTS, ids=[f"{f}-{sorted(t)[0]}" for f, t in LIFECYCLE_FAULTS])
def test_lifecycle_fault_detected(tmp_path, fault, tcs):
    controls = CONTROLS.get(fault, set()) if sorted(tcs)[0] != "TC-AFM-011" else set()
    verdicts = _run(tmp_path, fault, tcs | controls, inventory="inventory-afm3.yaml")
    assert {k for k, v in verdicts.items() if v == "FAIL"} == tcs, verdicts
    assert all(verdicts.get(c) == "PASS" for c in controls), verdicts


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_fault_detected(tmp_path, fault):
    verdicts = _run(tmp_path, fault, FAULTS[fault])
    assert {k for k, v in verdicts.items() if v == "FAIL"} == FAULTS[fault], verdicts
