"""HC-001..HC-006 - health-only checks of the full-rack health check (helios/healthcheck.py, tools/rack_health.py).

Read-only, SSH only (plus the optional BMC power query from the runner). Thresholds under ``thresholds.health``.
"""
from __future__ import annotations

import json
import re

import pytest

from helios.afmctl import Afmctl
from helios.compute import bmc_cmd, render
from helios.datapath import use_sudo
from helios.preflight import _gpu_users
from helios.rack import fan_out

pytestmark = pytest.mark.l1


@pytest.mark.healthcheck("blocker")
@pytest.mark.tc("HC-001")
def test_every_node_answers_ssh(rack, computes, inventory, checks, record_property):
    """Every switch and every compute tray answers a trivial SSH command."""
    timeout = float(inventory.th("health.ssh_timeout_s", 20))
    nodes = list(rack.switches.values()) + list(computes)
    down = []
    for n, r, exc in fan_out(lambda n: n.run("true", timeout=timeout), nodes):
        why = f"{type(exc).__name__}: {str(exc)[:120]}" if exc else (f"rc={r.rc}" if not r.ok else "")
        if why:
            down.append(n.name)
            checks.fail(f"{n.name}: not answering SSH ({why})")
    record_property("unreachable", down)
    # quarantined nodes are not judged, but their reachability is recorded: one that answers again can be released
    snap = {"unreachable": sorted(down)}
    if inventory.quarantined:
        from helios.transport import make_transport

        def probe(h):
            return make_transport(h, inventory.transport).run("true", timeout=timeout).ok
        qr = {h.name: (bool(ok) if not exc else False) for h, ok, exc in fan_out(probe, inventory.quarantined)}
        snap["quarantined_reachable"] = qr
        back = sorted(n for n, ok in qr.items() if ok)
        if back:
            record_property("release_quarantine", f"{', '.join(back)} answer SSH again - consider lifting the quarantine")
    record_property("snapshot", snap)
    checks.assert_all()


@pytest.mark.healthcheck("warn")
@pytest.mark.tc("HC-002")
def test_bmc_power_on(rack, computes, inventory, checks, record_property):
    """Every tray's BMC reports chassis power on (``commands.health.bmc_power``, run on the runner with {bmc} filled
    from the tray's ``bmc:`` inventory key, e.g. ipmitool ... chassis power status)."""
    tmpl = inventory.cmd("health.bmc_power")
    if not tmpl:
        pytest.skip("not configured: commands.health.bmc_power not set (e.g. 'IPMI_PASSWORD=\"$HELIOS_BMC_PASSWORD\" ipmitool -I lanplus "
                    "-H {bmc} -U <user> -E chassis power status')")
    states = {}

    def query(c):
        bmc = c.host.extra.get("bmc")
        if not bmc:
            return None
        return rack.runner.run(bmc_cmd(render(tmpl, name=c.name, host=c.host.host, bmc=bmc), c.name), timeout=60)

    for c, r, exc in fan_out(query, computes):
        if r is None and not exc:
            checks.fail(f"{c.name}: no bmc: in the inventory")
            continue
        out = "" if exc else (r.stdout + r.stderr).strip()
        states[c.name] = out[:80] if not exc else f"error: {exc}"
        if exc or not r.ok:
            checks.fail(f"{c.name}: BMC query failed: {exc or f'rc={r.rc} {out[-120:]!r}'}")
        elif not re.search(r"\bon\b", out, re.I):
            checks.fail(f"{c.name}: chassis power not on: {out[-120:]!r}")
    record_property("bmc_power", states)
    checks.assert_all()


@pytest.mark.healthcheck("blocker")
@pytest.mark.tc("HC-003")
def test_gpu_devices_active(computes, checks, record_property):
    """afmctl lists every GPU of the tray and every device is ACTIVE."""
    phases = {}
    for c, devs, exc in fan_out(lambda c: Afmctl(c, timeout=60).devices(), computes):
        if exc:
            checks.fail(f"{c.name}: afmctl failed: {str(exc)[:160]}")
            continue
        phases[c.name] = {d.bdf: d.config_phase for d in devs}
        checks.check(not c.gpus or len(devs) >= c.gpus, f"{c.name}: afmctl lists {len(devs)} of {c.gpus} GPUs")
        bad = [f"{d.bdf}={d.config_phase}" for d in devs if d.config_phase != "ACTIVE"]
        checks.check(not bad, f"{c.name}: GPUs not ACTIVE: {bad}")
    record_property("config_phase", phases)
    record_property("snapshot", {"gpu_phase": phases})
    checks.assert_all()


@pytest.mark.healthcheck("blocker")
@pytest.mark.tc("HC-004")
def test_no_other_gpu_workload(computes, inventory, checks):
    """No process of another workload holds the GPUs (amd-smi process) - a busy tray makes traffic and disruptive
    results meaningless."""
    sudo = use_sudo(inventory)
    for c, msg, exc in fan_out(lambda c: _gpu_users(c, sudo), computes):
        if exc:
            checks.fail(f"{c.name}: amd-smi process failed: {str(exc)[:160]}")
        elif msg:
            checks.fail(msg)
    checks.assert_all()


@pytest.mark.healthcheck("warn")
@pytest.mark.tc("HC-005")
def test_no_gpu_ras_or_hw_errors(computes, inventory, checks, record_property):
    """No uncorrectable / deferred GPU ECC errors and no more correctable ones than ``health.max_correctable_ecc``
    (amd-smi metric --ecc); in the kernel log of the last ``health.kernel_window_min`` minutes no uncorrected /
    deferred machine check and no AER / amdgpu / IFoE error. Corrected CPU machine-check events are counted and
    recorded; they fail only above ``health.max_corrected_mce_per_hour`` (unset: never)."""
    from helios.hwinfo import classify_kernel_errors

    sudo = use_sudo(inventory)
    max_ce = int(inventory.th("health.max_correctable_ecc", 0))
    window = int(inventory.th("health.kernel_window_min", 60))
    max_ce_mce = inventory.th("health.max_corrected_mce_per_hour")

    def probe(c):
        ecc = c.run("amd-smi metric --ecc --json", sudo=sudo, timeout=60)
        klog = c.run(f"journalctl -k -p err --since '-{window} min' --no-pager -q", sudo=True, timeout=60)
        return ecc, klog

    summary = {}
    for c, res, exc in fan_out(probe, computes):
        if exc:
            checks.fail(f"{c.name}: cannot read GPU / kernel errors: {str(exc)[:160]}")
            continue
        ecc, klog = res
        counts = {}
        if ecc.ok:
            try:
                doc = json.loads(ecc.stdout)
            except ValueError:
                doc = {}
            for g in (doc.get("gpu_data") if isinstance(doc, dict) else doc) or []:
                e = g.get("ecc") or {}
                counts[g.get("gpu")] = tuple(int(e.get(k) or 0) if str(e.get(k, "0")).isdigit() else 0 for k in
                                             ("total_uncorrectable_count", "total_deferred_count", "total_correctable_count"))
            for gpu, (ue, de, ce) in sorted(counts.items(), key=lambda kv: str(kv[0])):
                checks.check(not ue and not de, f"{c.name}: GPU{gpu} ECC uncorrectable={ue} deferred={de}")
                checks.check(ce <= max_ce, f"{c.name}: GPU{gpu} ECC correctable={ce} (> {max_ce})")
        k = classify_kernel_errors(klog.stdout.splitlines() if klog.ok else [])
        bad = k["uncorrected"] + k["other"]
        checks.check(not bad, f"{c.name}: {len(k['uncorrected'])} uncorrected machine-check and {len(k['other'])} other "
                              f"hardware / GPU error line(s) in the last {window} min, e.g. {bad[0][-160:] if bad else ''!r}")
        per_hour = k["corrected_events"] * 60.0 / window if window else 0
        if max_ce_mce is not None:
            checks.check(per_hour <= float(max_ce_mce), f"{c.name}: {per_hour:.0f} corrected CPU machine-check events / h "
                                                        f"(> {max_ce_mce})")
        summary[c.name] = {"ecc": {str(k2): v for k2, v in counts.items()}, "corrected_mce_per_hour": round(per_hour, 1),
                           "uncorrected_mce_lines": len(k["uncorrected"]), "other_error_lines": len(k["other"])}
    record_property("errors", summary)
    checks.assert_all()


@pytest.mark.healthcheck("warn")
@pytest.mark.tc("HC-006")
def test_tray_resources(computes, inventory, checks, record_property):
    """Root filesystem below ``health.max_disk_pct`` and available memory above ``health.min_mem_avail_pct``."""
    max_disk = float(inventory.th("health.max_disk_pct", 90))
    min_mem = float(inventory.th("health.min_mem_avail_pct", 10))

    def probe(c):
        return c.run("df -P /", timeout=30), c.run("free -m", timeout=30)

    out = {}
    for c, res, exc in fan_out(probe, computes):
        if exc:
            checks.fail(f"{c.name}: cannot read disk / memory: {str(exc)[:160]}")
            continue
        df, free = res
        m = re.search(r"(\d+)%\s+/\s*$", df.stdout, re.M)
        disk = int(m.group(1)) if m else None
        mem = next((ln.split() for ln in free.stdout.splitlines() if ln.startswith("Mem:")), None)
        avail = 100.0 * int(mem[6]) / int(mem[1]) if mem and len(mem) > 6 and int(mem[1]) else None
        out[c.name] = {"disk_pct": disk, "mem_avail_pct": round(avail, 1) if avail is not None else None}
        checks.check(disk is not None, f"{c.name}: cannot parse df: {df.stdout[-120:]!r}")
        checks.check(avail is not None, f"{c.name}: cannot parse free: {free.stdout[-120:]!r}")
        if disk is not None:
            checks.check(disk < max_disk, f"{c.name}: / is {disk}% full (>= {max_disk:g}%)")
        if avail is not None:
            checks.check(avail > min_mem, f"{c.name}: only {avail:.1f}% memory available (<= {min_mem:g}%)")
    record_property("resources", out)
    checks.assert_all()


VERSION_CMDS = {
    "amdgpu": "cat /sys/module/amdgpu/version",
    "rocm": "cat /opt/rocm/.info/version",
    "ifoe": "cat /sys/module/ifoe/version",
    "afmctl": "afmctl --version",
}


@pytest.mark.healthcheck("warn")
@pytest.mark.tc("HC-010")
def test_tray_versions_consistent(computes, inventory, checks, record_property):
    """amdgpu driver, ROCm, IFoE driver, afmctl and GPU firmware (amd-smi firmware) are the same on every tray - so
    before / after comparisons are not mixing builds. Recorded in the snapshot."""
    sudo = use_sudo(inventory)

    def probe(c):
        v = {k: (r.stdout.strip().splitlines() or [""])[0] if (r := c.run(cmd, timeout=30)).ok else ""
             for k, cmd in VERSION_CMDS.items()}
        fw = c.run("amd-smi firmware --json", sudo=sudo, timeout=60)
        fws = set()
        try:
            for g in json.loads(fw.stdout) if fw.ok else []:
                fws.add(tuple(sorted((f.get("fw_id"), str(f.get("fw_version"))) for f in g.get("fw_list") or [])))
        except ValueError:
            pass
        checks.check(len(fws) <= 1, f"{c.name}: GPUs run different firmware ({len(fws)} sets)")
        v["gpu_fw"] = ";".join(f"{k}={x}" for k, x in next(iter(fws))) if len(fws) == 1 else ("mixed" if fws else "")
        return v

    per = {}
    for c, v, exc in fan_out(probe, computes):
        if exc:
            checks.fail(f"{c.name}: cannot read versions: {str(exc)[:160]}")
            continue
        per[c.name] = v
    for key in list(VERSION_CMDS) + ["gpu_fw"]:
        seen: dict[str, list[str]] = {}
        for name, v in per.items():
            seen.setdefault(v.get(key, ""), []).append(name)
        if len(seen) > 1:
            parts = sorted(seen.items(), key=lambda kv: -len(kv[1]))
            checks.fail(f"{key} differs across trays: " + "; ".join(f"{(ver or '(none)')[:40]} on {', '.join(sorted(ns))}"
                                                                     for ver, ns in parts))
        missing = sorted(n for n, v in per.items() if not v.get(key))
        checks.check(not missing, f"{key}: no version reported on {missing}")
    record_property("versions", per)
    record_property("snapshot", {"tray_versions": per})
    checks.assert_all()


@pytest.mark.healthcheck("warn")
@pytest.mark.tc("HC-011")
def test_hardware_inventory(rack, computes, inventory, checks, record_property):
    """Hardware inventory + usage of every switch and compute tray: model / board, serial, CPU, CPU busy % and load,
    memory and disks used / free / total; switches: platform and compute-facing ports up / down; computes: GPUs (model,
    IFWI, serial, physical accelerator id, VRAM, gfx busy %, power, hotspot) and GPU ports up / down. Informational:
    fails only when a node cannot be read (thresholds are HC-006 / TC-SWH-056). Link counts go into the snapshot."""
    from helios.afmctl import parse_port_description
    from helios.hwinfo import GPU_METRIC, GPU_STATIC, HW_SCRIPT, gpu_info, node_info, parse_platform_summary, sections

    sudo = use_sudo(inventory)

    def switch(sw):
        info = node_info(sections(sw.run(HW_SCRIPT, sudo=True, timeout=90).stdout))
        info["platform"] = parse_platform_summary(sw.run("show platform summary", timeout=60).stdout)
        up = down = unknown = quarantined = 0
        down_ports = []
        qslots = inventory.quarantined_slots()
        for ns in sw.namespaces():
            cabled = set()
            for p, d in sw.port_descriptions(ns).items():
                if cab := parse_port_description(d):
                    if cab[0] in qslots:
                        quarantined += 1                 # cabled to a quarantined tray: counted apart
                    else:
                        cabled.add(p)
            # STATE_DB (one round trip, same source as TC-SWH-049), not 'show interfaces status'
            oper = {p: st.get("netdev_oper_status", "").lower() == "up" for p, st in sw.port_states(ns).items()}
            for p in cabled:
                if p not in oper:          # described as cabled but not in STATE_DB PORT_TABLE: not judged
                    unknown += 1
                elif oper[p]:
                    up += 1
                else:
                    down += 1
                    down_ports.append(f"{ns}/{p}" if ns else p)
        info["compute_links"] = {"total": up + down, "up": up, "down": down, "not_in_status": unknown,
                                 "quarantined": quarantined,
                                 "down_ports": sorted(down_ports)}
        return info

    def compute(c):
        info = node_info(sections(c.run(HW_SCRIPT, sudo=True, timeout=90).stdout))
        info["gpus"] = gpu_info(c.run(GPU_STATIC, sudo=sudo, timeout=90).stdout, c.run(GPU_METRIC, sudo=sudo, timeout=90).stdout)
        a = Afmctl(c, timeout=60)
        up = total = 0
        for d in a.devices():
            ports = [p for p in a.ports(d.bdf) if p.admin_state.lower() == "enabled"]
            total += len(ports)
            up += sum(1 for p in ports if p.up)
        info["gpu_links"] = {"total": total, "up": up, "down": total - up}
        return info

    sw_inv, ct_inv = {}, {}
    for n, res, exc in fan_out(switch, list(rack.switches.values())):
        if exc:
            checks.fail(f"{n.name}: cannot read inventory: {str(exc)[:160]}")
        else:
            sw_inv[n.name] = res
    for n, res, exc in fan_out(compute, list(computes)):
        if exc:
            checks.fail(f"{n.name}: cannot read inventory: {str(exc)[:160]}")
        else:
            ct_inv[n.name] = res
    sw_links = {k: v["compute_links"] for k, v in sw_inv.items()}
    ct_links = {k: v["gpu_links"] for k, v in ct_inv.items()}
    totals = {"switch_side": {k: sum(v[k] for v in sw_links.values()) for k in ("total", "up", "down")},
              "compute_side": {k: sum(v[k] for v in ct_links.values()) for k in ("total", "up", "down")}}
    record_property("inventory", {"switches": sw_inv, "computes": ct_inv, "links": totals})
    record_property("snapshot", {"links": {"totals": totals,
                                           "switch": {k: [v["up"], v["total"]] for k, v in sw_links.items()},
                                           "compute": {k: [v["up"], v["total"]] for k, v in ct_links.items()}}})
    checks.assert_all()


def _link_check_tool(inventory) -> tuple[str | None, str]:
    """(path of helios-debug's tools/link_check.py, python to run it) - thresholds.health.link_check.dir, then
    $HELIOS_DEBUG_DIR, ../helios-debug next to this repo, ~/helios-debug, ~/dev/helios-debug."""
    import os
    import sys
    from pathlib import Path
    cfg = inventory.th("health.link_check") or {}
    here = Path(__file__).resolve().parents[2]
    for d in [cfg.get("dir"), os.environ.get("HELIOS_DEBUG_DIR"), here.parent / "helios-debug",
              Path.home() / "helios-debug", Path.home() / "dev" / "helios-debug"]:
        if d and (Path(d).expanduser() / "tools" / "link_check.py").is_file():
            root = Path(d).expanduser()
            py = cfg.get("python") or (str(root / ".venv" / "bin" / "python") if (root / ".venv" / "bin" / "python").is_file()
                                       else sys.executable)
            return str(root / "tools" / "link_check.py"), py
    return None, sys.executable


@pytest.mark.healthcheck("warn")
@pytest.mark.tc("HC-012")
def test_link_scan(inventory, request, checks, record_property):
    """Every GPU link of the rack (switch port <-> GPU port) checked by helios-debug's link check - kept in its own repo
    and run here as a separate program on the same inventory: state on both ends, growing bit errors, IFCP, forwarding
    entry, the switch ASIC's per-lane view for physical problems. One failure line per root cause (trays quarantined in
    the inventory are recorded, not failed); the full report (ASIC to GPU map, every link, command logs) is kept under
    <results>/runs/<rack>/link-scans/scan-<UTC time>/ (property 'report'). thresholds.health.link_check: {dir, python, interval_s (30), timeout_s (900),
    enabled (true)}."""
    import json
    import subprocess
    from pathlib import Path
    cfg = inventory.th("health.link_check") or {}
    if cfg.get("enabled") is False:
        pytest.skip("thresholds.health.link_check.enabled is false")
    if str((inventory.transport or {}).get("kind", "ssh")) != "ssh" or inventory.th("afm.fake"):
        pytest.skip("link scan needs real SSH to the rack (replay / fake inventory)")
    tool, py = _link_check_tool(inventory)
    if not tool:
        pytest.skip("helios-debug not found (thresholds.health.link_check.dir or $HELIOS_DEBUG_DIR)")
    import time
    res = Path(__file__).resolve().parents[2] / "results"
    base = Path(request.config.getoption("--results-dir") or res / "runs" / (inventory.rack or "rack"))
    out = base / "link-scans" / f"scan-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"   # the run folder exists only at the end
    cmd = [py, tool, "scan", "--inventory", str(inventory.path), "--interval", str(cfg.get("interval_s", 30)), "--out", str(out)]
    record_property("command", " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=float(cfg.get("timeout_s", 900)))
    except subprocess.TimeoutExpired:
        pytest.fail(f"link scan did not finish within {cfg.get('timeout_s', 900)} s", pytrace=False)
    record_property("report", str(out / "report.html"))
    record_property("scan_output", p.stdout[-6000:])
    js = out / "link_check.json"
    checks.check(p.returncode in (0, 1, 2) and js.is_file(), f"link scan failed to run (rc {p.returncode}): {(p.stderr or p.stdout)[-400:]}")
    if not js.is_file():
        checks.assert_all()
    d = json.loads(js.read_text())
    links = d.get("links") or []
    states = {k: sum(1 for x in links if x.get("state") == k) for k in ("ok", "degraded", "down")}
    record_property("links", {"total": len(links), **states})
    record_property("patterns", d.get("patterns") or [])
    quarantined = []
    for g in d.get("root_causes") or []:
        if g.get("kind") == "tray_quarantined" or str(g.get("cause", "")).startswith("compute tray quarantined"):
            quarantined.append(f"{g.get('count')} links: {g.get('cause')}")
            continue
        first = ", ".join((g.get("links") or [])[:4])
        checks.fail(f"{str(g.get('state', '')).upper()} {g.get('count')} link(s): {g.get('cause')} [{g.get('owner')}] - "
                    f"{first}{' ...' if g.get('count', 0) > 4 else ''}")
    record_property("quarantined", quarantined)
    if p.returncode == 2:
        nc = [ln.strip() for ln in p.stdout.splitlines() if ln.startswith(("  switch ", "  tray "))]
        checks.fail("link scan could not read every node: " + "; ".join(nc[:6]))
    record_property("snapshot", {"link_scan": {"total": len(links), **states}})
    checks.assert_all()

