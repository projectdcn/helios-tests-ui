"""Read-only checks of what AFM, the switch agents and the controllers report (automated 19 Oct 2026; the cases were in
the plan as not automated). Nothing is changed on the rack: AFM API reads, switch SSH reads (show / sonic-db-cli /
docker exec ps / curl to the agent API / openssl / find), no tray commands.

Where the plan asks about something AFM does not expose directly, the check uses the closest observable and says so
in its docstring (e.g. control sessions per switch = GPUs of the computes whose node agent the switch runs).
"""
from __future__ import annotations

import re
import time
from collections import Counter

import pytest

from helios.afm import conditions_ok

pytestmark = [pytest.mark.l2]


# ------------------------------------------------------------------ helpers

def _need_switches(ops):
    if ops.rack is None or not ops.rack.switches:
        pytest.skip("no switch SSH in this inventory")


def _admitted(objs) -> list[dict]:
    return [o for o in objs if (o.get("status") or {}).get("admission-phase") == "admitted"]


def _computes(ops) -> list[dict]:
    return _admitted(ops.active_computes())


def _switch_nodes(ops) -> list[dict]:
    return _admitted(ops.active_switches())


def _ports(sn: dict):
    """(namespace, port record) of an AFM SwitchNode - asic 'id' N (rack) or 'name' (fake) -> 'asicN'."""
    for a in ((sn.get("status") or {}).get("asic-info") or {}).get("asics") or []:
        ns = a.get("name") or (f"asic{a['id']}" if a.get("id") is not None else None)
        for p in a.get("ports") or []:
            yield ns, p


def _ssh(ops, sn: dict):
    try:
        return ops.ssh_switch(sn)
    except Exception:  # noqa: BLE001 - a switch not in the inventory is reported by the caller
        return None


def _placement(ops) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for sw in ops.rack.switches.values():
        for slot in ops.sb_agents(sw):
            out.setdefault(slot, []).append(sw.name)
    return out


def _slots(ops) -> dict[int, dict]:
    return {int(c["spec"]["slot-id"]): c for c in _computes(ops) if (c.get("spec") or {}).get("slot-id") is not None}


def _gpus(ops, c: dict) -> int:
    return len(ops.gpu_info(c))


def _balance(ops, checks, record_property, what: str):
    """Node agents (one per admitted compute slot) and the GPUs they serve, per switch."""
    _need_switches(ops)
    slots, place = _slots(ops), _placement(ops)
    nsw = len(ops.rack.switches)
    per_sw_agents = Counter(h for s, hosts in place.items() if s in slots for h in hosts)
    per_sw_gpus = Counter()
    for s, hosts in place.items():
        if s in slots and len(hosts) == 1:
            per_sw_gpus[hosts[0]] += _gpus(ops, slots[s])
    total_gpus = sum(_gpus(ops, c) for c in slots.values())
    record_property("node_agents_per_switch", {n: per_sw_agents.get(n, 0) for n in ops.rack.switches})
    record_property("gpus_per_switch", {n: per_sw_gpus.get(n, 0) for n in ops.rack.switches})
    record_property("formula", {"computes": len(slots), "switches": nsw, "gpus": total_gpus,
                                "agents_per_switch": round(len(slots) / nsw, 2) if nsw else None,
                                "gpus_per_switch": round(total_gpus / nsw, 2) if nsw else None})
    return slots, place, per_sw_agents, per_sw_gpus, total_gpus, nsw


# ------------------------------------------------------------------ node agents and control sessions

@pytest.mark.tc("TC-BU-012")
def test_one_node_agent_per_compute(ops, checks, record_property):
    """All GPU sessions of a compute are served by one node agent: every admitted compute slot has exactly one
    sb-node-agent, on exactly one switch."""
    _need_switches(ops)
    slots, place = _slots(ops), _placement(ops)
    for s in sorted(slots):
        hosts = place.get(s, [])
        checks.check(len(hosts) == 1, f"slot {s} ({slots[s]['meta']['name']}): node agent on {hosts or 'no switch'}")
    record_property("placement", {s: place.get(s) for s in sorted(slots)})
    checks.assert_all()


@pytest.mark.tc("TC-SBM-059")
def test_compute_gpus_with_one_node_agent(ops, checks, record_property):
    """All GPUs of one compute form their sessions with one node agent (as TC-BU-012: one sb-node-agent per
    admitted compute slot, on one switch)."""
    test_one_node_agent_per_compute(ops, checks, record_property)


@pytest.mark.tc("TC-SBM-139")
def test_node_agents_per_switch(ops, checks, record_property):
    """The node manager on each switch starts its share of node agents: every switch runs between floor and ceil
    of (admitted computes / switches) sb-node-agents, within thresholds.afm.agent_balance_slack (1)."""
    slots, place, per_sw, _g, _t, nsw = _balance(ops, checks, record_property, "agents")
    slack = int(ops.th("agent_balance_slack", 1))
    lo, hi = len(slots) // nsw, -(-len(slots) // nsw)
    for n in ops.rack.switches:
        k = per_sw.get(n, 0)
        checks.check(lo - slack <= k <= hi + slack, f"{n}: {k} node agents, expected {lo}..{hi} (+-{slack})")
    checks.assert_all()


@pytest.mark.tc("TC-BU-005")
def test_sessions_per_switch_balanced(ops, checks, record_property):
    """Each switch carries its share of GPU control sessions: GPUs served per switch (the GPUs of the computes
    whose node agent it runs; sessions themselves are not exposed) within one compute's GPUs of the average."""
    slots, _p, _a, per_gpu, total, nsw = _balance(ops, checks, record_property, "sessions")
    per_c = max((_gpus(ops, c) for c in slots.values()), default=4)
    avg = total / nsw if nsw else 0
    for n in ops.rack.switches:
        g = per_gpu.get(n, 0)
        checks.check(abs(g - avg) <= per_c * int(ops.th("agent_balance_slack", 1)),
                     f"{n}: {g} GPU sessions, average {avg:.1f} (one compute = {per_c})")
    checks.assert_all()


@pytest.mark.tc("TC-SBM-053")
def test_control_sessions_per_switch(ops, checks, record_property):
    """Every switch terminates the expected number of control sessions (12 on the full rack: 3 computes x 4 GPUs) -
    counted as GPUs of the computes whose node agent it runs, compared with total GPUs / switches."""
    test_sessions_per_switch_balanced(ops, checks, record_property)


@pytest.mark.tc("TC-SBM-001")
def test_sideband_topology_formulas(ops, checks, record_property):
    """Sideband topology and session-count formulas hold: node agents = admitted computes, control sessions (GPUs
    served) = GPUs of the admitted computes, every slot served once."""
    slots, place, per_sw, per_gpu, total, nsw = _balance(ops, checks, record_property, "formulas")
    agents = sum(per_sw.values())
    checks.check(agents == len(slots), f"{agents} node agents for {len(slots)} admitted computes")
    served = sum(per_gpu.values())
    checks.check(served == total, f"{served} GPUs served by node agents, {total} GPUs in the admitted computes")
    checks.assert_all()


@pytest.mark.tc("TC-SBM-138")
def test_switch_agent_starts_first(ops, checks, record_property):
    """In each switch's afm-agent container the switch-agent came up before (or with) every sb-node-agent: its
    elapsed time is the largest."""
    _need_switches(ops)
    out = {}
    for sw in ops.rack.switches.values():
        r = sw.run("docker exec afm-agent ps -eo pid,etimes,args", sudo=True, timeout=60)
        sa = [int(m.group(1)) for m in re.finditer(r"^\s*\d+\s+(\d+)\s+\S*switch-agent(?:\s|$)", r.stdout, re.M)]
        nas = [int(m.group(1)) for m in re.finditer(r"^\s*\d+\s+(\d+)\s+\S*sb-node-agent\s", r.stdout, re.M)]
        checks.check(sa, f"{sw.name}: no switch-agent in afm-agent")
        if sa and nas:
            checks.check(max(sa) >= max(nas), f"{sw.name}: an sb-node-agent is older ({max(nas)} s) than the switch-agent ({max(sa)} s)")
        out[sw.name] = {"switch_agent_s": max(sa) if sa else None, "oldest_node_agent_s": max(nas) if nas else None}
    record_property("elapsed", out)
    checks.assert_all()


# ------------------------------------------------------------------ compute bring-up state seen by AFM

@pytest.mark.tc("TC-SEQ-004")
def test_gpu_reachability_reported(ops, checks, record_property):
    """GPU reachability is reported to the Sideband Manager: every admitted compute lists all its GPUs in AFM, each
    Ready and with its IFoE stations reported Ready."""
    want = int(ops.th("gpus_per_compute", 4))
    seen = {}
    for c in _computes(ops):
        n = c["meta"]["name"]
        gi = ops.gpu_info(c)
        checks.check(len(gi) == want, f"{n}: {len(gi)} GPUs reported, expected {want}")
        for g in gi.values():
            checks.check(g.get("oper-state", "Ready") == "Ready", f"{n} GPU {g.get('index')}: {g.get('oper-state')}")
            st = g.get("ifoe-stations") or []
            bad = [s.get("id") for s in st if isinstance(s, dict) and (s.get("oper-status") or {}).get("oper-state") != "StationReady"]
            checks.check(st and not bad, f"{n} GPU {g.get('index')}: stations not ready {bad}" if st else f"{n} GPU {g.get('index')}: no IFoE stations reported")
        seen[n] = len(gi)
    record_property("gpus", seen)
    checks.assert_all()


@pytest.mark.tc("TC-SEQ-008")
def test_compatibility_check_completed(ops, checks, record_property):
    """The node agent's bring-up (incl. the compatibility check) completed: every admitted compute reports
    init-phase success and is connected to the controller."""
    out = {}
    for c in _computes(ops):
        st = c.get("status") or {}
        n = c["meta"]["name"]
        checks.check(st.get("init-phase") == "success", f"{n}: init-phase {st.get('init-phase')!r}")
        checks.check(st.get("is-connected-to-controller") is True, f"{n}: not connected to the controller")
        out[n] = (st.get("init-phase"), st.get("is-connected-to-controller"))
    record_property("computes", out)
    checks.assert_all()


@pytest.mark.tc("TC-SEQ-015")
def test_node_agent_responses_applied(ops, checks, record_property):
    """Node agent responses reach the node manager: every admitted compute slot's agent API answers
    /api/runtime/requests and no request is left failed."""
    _need_switches(ops)
    place = _placement(ops)
    out = {}
    for s in sorted(_slots(ops)):
        hosts = place.get(s) or []
        if not hosts:
            checks.fail(f"slot {s}: no node agent")
            continue
        sw = ops.rack.switches[hosts[0]]
        reqs = sw.agent_api(13000 + s, "/api/runtime/requests")
        checks.check(isinstance(reqs, list), f"slot {s} on {sw.name}: no JSON list from /api/runtime/requests")
        if isinstance(reqs, list):
            bad = [r for r in reqs if str(r.get("status", "")).lower() in ("failed", "error")]
            checks.check(not bad, f"slot {s}: {len(bad)} failed request(s): {bad[:2]}")
            out[s] = Counter(str(r.get("status")) for r in reqs)
    record_property("requests", {k: dict(v) for k, v in out.items()})
    checks.assert_all()


# ------------------------------------------------------------------ switch topology and ports in AFM

@pytest.mark.tc("TC-SWA-170")
def test_switches_in_pod_topology(ops, checks, record_property):
    """Every switch of the inventory is in AFM's topology: a SwitchNode with its IP, admitted, all in one pod, each
    with its own slot id."""
    _need_switches(ops)
    sns = ops.afm.switches()
    mapped = {getattr(_ssh(ops, s), "name", None) for s in sns}   # afm_name, then management IP (as ssh_switch)
    for sw in ops.rack.switches.values():
        checks.check(sw.name in mapped, f"{sw.name} ({sw.host.host}): no SwitchNode in AFM")
    pods = Counter((s.get("spec") or {}).get("pod") for s in sns)
    checks.check(len(pods) == 1, f"switches in several pods: {dict(pods)}")
    sl = Counter((s.get("spec") or {}).get("slot-id") for s in sns)
    dup = [k for k, v in sl.items() if v > 1]
    checks.check(not dup, f"duplicate switch slot ids: {dup}")
    record_property("switch_nodes", {s["meta"]["name"]: ((s.get("spec") or {}).get("ip-address"), (s.get("spec") or {}).get("slot-id"),
                                                         (s.get("status") or {}).get("admission-phase")) for s in sns})
    checks.assert_all()


@pytest.mark.tc("TC-SWA-173")
def test_switching_planes_assigned(ops, checks, record_property):
    """The Switch Manager assigned switching planes: every switch port cabled to a GPU carries a switching-plane-id,
    one plane per GPU port number across the whole rack, a different plane for each GPU port number."""
    plane_of: dict = {}
    missing = 0
    for sn in _switch_nodes(ops):
        for ns, p in _ports(sn):
            peer = p.get("peer") or {}
            if peer.get("port-id") is None:
                continue
            pl = p.get("switching-plane-id")
            if pl is None:
                missing += 1
                continue
            plane_of.setdefault(peer["port-id"], set()).add(pl)
    checks.check(plane_of, "no switch port with a GPU peer and a plane in AFM")
    checks.check(missing == 0, f"{missing} cabled switch port(s) without a switching-plane-id")
    multi = {k: sorted(v) for k, v in plane_of.items() if len(v) > 1}
    checks.check(not multi, f"GPU port numbers on several planes: {dict(list(multi.items())[:6])}")
    planes = [next(iter(v)) for v in plane_of.values() if len(v) == 1]
    dup = [k for k, v in Counter(planes).items() if v > 1]
    checks.check(not dup, f"one plane shared by several GPU port numbers: {dup}")
    record_property("planes", len(set(planes)))
    checks.assert_all()


@pytest.mark.tc("TC-BUG-018")
def test_port_oper_status_matches_link(ops, checks, record_property):
    """AFM's port oper-state matches the real link: PortReady ports are oper up on the switch, ports oper down on the
    switch are not PortReady (a mismatch is read again after thresholds.afm.oper_recheck_s, 15 s, before it counts)."""
    _need_switches(ops)

    def mismatches():
        out = []
        for sn in _switch_nodes(ops):
            sw = _ssh(ops, sn)
            if sw is None:
                continue
            oper = {}
            for ns in sw.namespaces():
                oper.update({(ns, r.get("Interface")): str(r.get("Oper", "")).lower() for r in sw.interfaces_status(ns)})
            for ns, p in _ports(sn):
                o = oper.get((ns, p.get("name")))
                if o is None:
                    continue
                afm_up = p.get("oper-state") == "PortReady"
                if afm_up != (o == "up"):
                    out.append(f"{sw.name} {ns} {p.get('name')}: AFM {p.get('oper-state')}, switch {o}")
        return out
    first = mismatches()
    if first:
        time.sleep(float(ops.th("oper_recheck_s", 15)) * float(ops.th("time_scale", 1)))
        first = [m for m in mismatches() if m in first]
    record_property("mismatches", first[:30])
    checks.check(not first, f"{len(first)} port(s) where AFM and the switch disagree: {first[:6]}")
    checks.assert_all()


@pytest.mark.tc("TC-SWA-208")
def test_switch_configuration_maintained(ops, checks, record_property):
    """The Switch Manager keeps the switches configured: every switch port AFM reports PortReady towards a GPU is a
    VLAN member and has a static forwarding entry on the switch."""
    _need_switches(ops)
    checked = 0
    for sn in _switch_nodes(ops):
        sw = _ssh(ops, sn)
        if sw is None:
            continue
        for ns in sw.namespaces():
            members = {p for ports in sw.vlan_members(ns).values() for p in ports}
            with_fdb = {p for _v, _m, p in sw.fdb_static(ns)}
            for ns2, p in _ports(sn):
                if ns2 != ns or p.get("oper-state") != "PortReady" or not (p.get("peer") or {}).get("display"):
                    continue
                checked += 1
                n = p.get("name")
                checks.check(n in members, f"{sw.name} {ns} {n}: PortReady in AFM but in no VLAN")
                checks.check(n in with_fdb, f"{sw.name} {ns} {n}: PortReady in AFM but no static forwarding entry")
    checks.check(checked, "no PortReady GPU-facing port found to check")
    record_property("ports_checked", checked)
    checks.assert_all()


# ------------------------------------------------------------------ agents, controllers, certificates, logs

@pytest.mark.tc("TC-CTL-103")
def test_nodes_keep_reporting(ops, checks, record_property):
    """Admitted switches and computes keep reporting to the controller (heartbeat and stats are not exposed, a
    missing report turns the node unhealthy): every admitted node is healthy at two reads
    thresholds.afm.heartbeat_window_s (60 s) apart."""
    win = float(ops.th("heartbeat_window_s", 60)) * float(ops.th("time_scale", 1))

    def unhealthy():
        return sorted(o["meta"]["name"] for o in _switch_nodes(ops) + _computes(ops) if not conditions_ok(o))
    a = unhealthy()
    time.sleep(win)
    b = unhealthy()
    record_property("unhealthy", {"first": a, "second": b})
    checks.check(not a and not b, f"not reporting healthy: first read {a}, second read {b}")
    checks.assert_all()


@pytest.mark.tc("TC-CTL-123")
def test_controller_logs_updating(ops, checks, record_property):
    """The controller logs are being written: on every controller node a log file under
    /opt/amd/afm/controller/logs changed within thresholds.afm.log_fresh_s (900 s)."""
    fresh = int(ops.th("log_fresh_s", 900))
    out = {}
    for ip, sw in ops.controllers().items():
        if sw is None:
            checks.fail(f"controller {ip}: not in the inventory")
            continue
        files = ops.controller_logs_since(sw, ops.epoch(sw) - fresh)
        out[sw.name] = files[:5]
        checks.check(files, f"{sw.name}: no controller log written in the last {fresh} s")
    record_property("recent_logs", out)
    checks.assert_all()


@pytest.mark.tc("TC-STR-015")
def test_controller_log_rotation(ops, checks, record_property):
    """Controller logs stay bounded (rotation works): no single file above thresholds.afm.max_log_file_mb (1024) and
    the log folder below thresholds.afm.max_log_dir_mb (8192) on every controller node. (The plan's 2-hour full-rack
    load is not generated here; the sizes are those of the rack's own activity.)"""
    max_f = float(ops.th("max_log_file_mb", 1024)) * 2**20
    max_d = float(ops.th("max_log_dir_mb", 8192)) * 2**20
    out = {}
    for ip, sw in ops.controllers().items():
        if sw is None:
            continue
        r = sw.run("find /opt/amd/afm/controller/logs -type f -printf '%s %f\\n'", sudo=True, timeout=60)
        files = [(int(a), b) for a, _, b in (ln.partition(" ") for ln in r.stdout.splitlines()) if a.isdigit()]
        checks.check(files, f"{sw.name}: no controller log files")
        big = [f"{n} {s / 2**20:.0f} MB" for s, n in files if s > max_f]
        total = sum(s for s, _ in files)
        checks.check(not big, f"{sw.name}: log file(s) above {max_f / 2**20:.0f} MB: {big[:4]}")
        checks.check(total <= max_d, f"{sw.name}: controller logs {total / 2**20:.0f} MB, above {max_d / 2**20:.0f} MB")
        out[sw.name] = {"files": len(files), "total_mb": round(total / 2**20, 1),
                        "largest_mb": round(max((s for s, _ in files), default=0) / 2**20, 1),
                        "rotated": sum(1 for _s, n in files if re.search(r"\.(gz|\d+)$|-\d{4}-\d\d-\d\d", n))}
    record_property("logs", out)
    checks.assert_all()


CERT_CMD = ("echo | openssl s_client -connect 127.0.0.1:443 2>/dev/null "
            "| openssl x509 -noout -fingerprint -sha256 2>/dev/null")


@pytest.mark.tc("TC-CTL-129")
def test_same_certificate_on_all_controllers(ops, checks, record_property):
    """The same server certificate is deployed on every controller node: the certificate served on port 443 of each
    controller (read on the switch itself) has the same SHA-256 fingerprint."""
    fps = {}
    for ip, sw in ops.controllers().items():
        if sw is None:
            checks.fail(f"controller {ip}: not in the inventory")
            continue
        out = sw.run(CERT_CMD, sudo=True, timeout=60).stdout
        m = re.search(r"Fingerprint=([0-9A-F:]+)", out, re.I)
        checks.check(m, f"{sw.name}: no certificate read on port 443")
        if m:
            fps[sw.name] = m.group(1).upper()
    record_property("fingerprints", fps)
    checks.check(len(set(fps.values())) <= 1, f"controllers serve different certificates: {fps}")
    checks.assert_all()


AGENT_CERT_CMD = ("for f in /opt/amd/afm/agent/sysconfig/config*/*.pem; do "
                  "s=$(openssl x509 -noout -subject -issuer -in \"$f\" 2>/dev/null) || continue; "
                  "echo \"== $f\"; echo \"$s\"; openssl x509 -noout -checkend 0 -in \"$f\" >/dev/null 2>&1 "
                  "&& echo VALID || echo EXPIRED; done")


@pytest.mark.tc("TC-CTL-136")
def test_switch_agent_has_signed_certificate(ops, checks, record_property):
    """Each admitted switch's agent holds a certificate from its admission: at least one X.509 certificate in the
    agent's sysconfig, signed by a CA (issuer differs from subject) and not expired."""
    out = {}
    for sn in _switch_nodes(ops):
        sw = _ssh(ops, sn)
        if sw is None:
            continue
        text = sw.run(AGENT_CERT_CMD, sudo=True, timeout=60).stdout
        certs = []
        for blk in text.split("== ")[1:]:
            lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
            subj = next((ln.split("=", 1)[1].strip() for ln in lines if ln.lower().startswith("subject")), "")
            iss = next((ln.split("=", 1)[1].strip() for ln in lines if ln.lower().startswith("issuer")), "")
            certs.append({"file": lines[0], "signed": bool(iss) and iss != subj, "valid": "VALID" in lines})
        signed = [c for c in certs if c["signed"]]
        checks.check(signed, f"{sw.name}: no CA-signed certificate in the agent sysconfig ({len(certs)} certificate(s))")
        checks.check(all(c["valid"] for c in signed), f"{sw.name}: expired agent certificate: {[c['file'] for c in signed if not c['valid']]}")
        out[sw.name] = certs
    record_property("agent_certificates", out)
    checks.assert_all()
