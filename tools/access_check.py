"""Prerequisites check of a rack before any test: what the tests need from this machine, the AFM controller, every
switch and every compute tray - read-only, a few seconds per node, nothing is changed on the rack.

    python tools/access_check.py --inventory inventory/mr3.local.yaml [--results-dir results/runs/mr3]

Checks (each OK / FAIL / WARN / SKIP with what to do about a problem):
  this machine   the SSH key files the inventory names exist; the AFM password is given (else AFM cases skip)
  AFM            the controller answers; the login works with the given password
  every node     SSH login with the inventory's user and key (no password prompt); passwordless sudo (sudo -n)
  switches       SONiC containers readable (sudo docker ps)
  computes       afmctl answers and every GPU is config_phase ACTIVE; the GPUs can be opened (amd-smi; 'render'
                 group or commands.datapath.sudo); no other workload on the GPUs; the traffic tools the inventory
                 configures are installed (TransferBench, RCCL tests / smoke)
FAIL = tests that need it cannot run (they block or fail); WARN = some cases will skip or leave the tray out.

Writes <results-dir>/<rack>-access-<YYYYmmdd-HHMMSS>/access.json (the UI shows it as a checklist) and exits 1 when
anything FAILed. The UI runs it like a test run ("Check access" on the Run tab): on the UI server for a direct rack,
on the jump host for a jump-host rack (its results are copied back).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from helios.inventory import load_inventory  # noqa: E402

OK, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"
MARK = {OK: "OK  ", FAIL: "FAIL", WARN: "WARN", SKIP: "SKIP"}


def item(area: str, node: str, check: str, status: str, detail: str = "", fix: str = "") -> dict:
    return {"area": area, "node": node, "check": check, "status": status, "detail": detail, "fix": fix}


def _unknown(r) -> bool:
    """A replayed (self-test) command without a canned answer: nothing to judge."""
    return r.rc == 127 and "replay:" in (r.stderr or "")


def runner_checks(inv) -> list[dict]:
    from helios.transport import key_path
    out, seen = [], set()
    for h in [*inv.switches, *inv.computes]:
        if not h.key_file or h.key_file in seen or inv.transport.get("kind") == "replay":
            continue
        seen.add(h.key_file)
        try:
            out.append(item("this machine", socket.gethostname().split(".")[0], f"SSH key {h.key_file}", OK,
                            key_path(h.key_file, h.name)))
        except FileNotFoundError as e:
            out.append(item("this machine", socket.gethostname().split(".")[0], f"SSH key {h.key_file}", FAIL, str(e),
                            "copy the private key here (chmod 600), or create a key here, ssh-copy-id it to the nodes "
                            "and set key_file"))
    auth = (inv.afm or {}).get("auth") or {}
    if inv.afm.get("url"):
        var = auth.get("password_env", "HELIOS_AFM_PASSWORD")
        given = bool(os.environ.get(var)) or auth.get("kind") in ("none", "bearer")
        out.append(item("this machine", "AFM password", "AFM password given", OK if given else WARN,
                        "given for this check" if given else f"not given ({var})",
                        "" if given else "type it (or save it) in the Run form - without it the AFM cases skip"))
    return out


def afm_checks(inv) -> list[dict]:
    if not inv.afm.get("url"):
        return [item("AFM", "controller", "AFM configured", SKIP, "no afm.url in the inventory: AFM cases skip")]
    import requests
    from helios.afm import AfmClient
    url = inv.afm["url"]
    c = AfmClient(inv.afm)
    out = []
    try:
        r = c.s.get(url, timeout=10)
        out.append(item("AFM", url, "controller answers", OK, f"HTTP {r.status_code}"))
    except requests.RequestException as e:
        why = ("name not resolved" if "NameResolution" in str(e) or "Name or service" in str(e) else
               "timed out" if isinstance(e, requests.Timeout) else "connection refused" if "refused" in str(e) else
               f"{type(e).__name__}: {str(e)[:160]}")
        return [item("AFM", url, "controller answers", FAIL, why,
                     "check the address in the inventory (afm.url) and that this machine can reach it")]
    if c.password is None and c.kind not in ("none", "bearer"):
        out.append(item("AFM", url, "login", SKIP, "no AFM password given"))
        return out
    try:
        c.login()
        out.append(item("AFM", url, "login", OK, f"as {c.username}"))
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        out.append(item("AFM", url, "login", FAIL, msg[:300],
                        "wrong user / password?" if " 401" in msg or "401" in msg[:40] else "see the detail"))
    return out


def node_checks(node, role: str, inv) -> list[dict]:
    from helios import preflight
    from helios.datapath import use_sudo
    h = node.host
    area = "switch" if role == "switch" else "compute"
    out = []
    try:
        r = node.run("true", timeout=30)
    except Exception as e:  # noqa: BLE001
        if isinstance(e, FileNotFoundError):
            return [item(area, h.name, f"SSH login as {h.user}", FAIL, f"no SSH key {h.key_file} on this machine",
                         "see the 'SSH key' check of this machine")]
        return [item(area, h.name, f"SSH login as {h.user}", FAIL, f"{type(e).__name__}: {str(e)[:200]}",
                     f"check that this machine reaches {h.host} and that {h.user} accepts the key (ssh-copy-id)")]
    if _unknown(r):
        out.append(item(area, h.name, f"SSH login as {h.user}", SKIP, "replayed rack: not judged"))
    elif not r.ok:
        return [item(area, h.name, f"SSH login as {h.user}", FAIL, f"rc={r.rc} {(r.stderr or r.stdout)[-200:]}")]
    else:
        out.append(item(area, h.name, f"SSH login as {h.user}", OK, f"{h.user}@{h.host}"))
    r = node.run("true", sudo=True, timeout=30)
    if _unknown(r):
        out.append(item(area, h.name, "passwordless sudo", SKIP, "replayed rack: not judged"))
    elif r.ok:
        out.append(item(area, h.name, "passwordless sudo", OK, "sudo -n true"))
    else:
        out.append(item(area, h.name, "passwordless sudo", FAIL, (r.stderr or r.stdout).strip()[-200:],
                        f"add a NOPASSWD sudoers entry for {h.user} (e.g. '{h.user} ALL=(ALL) NOPASSWD: ALL' in "
                        f"/etc/sudoers.d/helios) - checks run as 'sudo -n' and never answer a prompt"))
    if role == "switch":
        r = node.run("docker ps --format '{{.Names}}'", sudo=True, timeout=60)
        if _unknown(r):
            out.append(item(area, h.name, "SONiC containers readable", SKIP, "replayed rack: not judged"))
        elif r.ok and r.stdout.strip():
            out.append(item(area, h.name, "SONiC containers readable", OK,
                            f"{len(r.stdout.split())} containers running"))
        else:
            out.append(item(area, h.name, "SONiC containers readable", FAIL, (r.stderr or "no containers")[-200:],
                            "needs passwordless sudo for docker"))
        return out
    # compute tray: what the datapath pre-flight judges before traffic
    sudo = use_sudo(inv)
    try:
        from helios.afmctl import Afmctl
        devs = Afmctl(node, timeout=60).devices()
        bad = [f"{d.bdf}={d.config_phase}" for d in devs if d.config_phase != "ACTIVE"]
        if not devs:
            out.append(item(area, h.name, "GPUs ACTIVE (afmctl)", FAIL, "afmctl lists no GPU",
                            "is afmctl installed and the node agent up?"))
        elif bad:
            out.append(item(area, h.name, "GPUs ACTIVE (afmctl)", FAIL, f"not ACTIVE: {', '.join(bad)}",
                            "the tray is left out of traffic tests until its GPUs are ACTIVE"))
        else:
            out.append(item(area, h.name, "GPUs ACTIVE (afmctl)", OK, f"{len(devs)} GPUs ACTIVE"))
    except Exception as e:  # noqa: BLE001
        out.append(item(area, h.name, "GPUs ACTIVE (afmctl)", FAIL, f"{type(e).__name__}: {str(e)[:200]}",
                        "afmctl must answer (with passwordless sudo)"))
    msg = preflight._gpu_access(node, inv, sudo)
    out.append(item(area, h.name, "GPUs can be opened (amd-smi)", FAIL if msg else OK, msg or "visible and openable",
                    "add the user to the 'render' group, or set commands.datapath.sudo: true" if msg else ""))
    msg = preflight._gpu_users(node, sudo)
    out.append(item(area, h.name, "GPUs idle", WARN if msg else OK, msg or "no other workload",
                    "stop the other workload before traffic tests" if msg else ""))
    paths = {t: p for t in preflight.TOOLS if (p := preflight.tool_path(inv, t))}
    if paths:
        msg = preflight._tools_missing(node, paths)
        out.append(item(area, h.name, "traffic tools installed", WARN if msg else OK,
                        msg or ", ".join(f"{t} ({p})" for t, p in paths.items()),
                        "install them, or the traffic cases leave this tray out" if msg else ""))
    else:
        out.append(item(area, h.name, "traffic tools installed", SKIP, "no traffic tool configured in the inventory"))
    return out


def check(inv) -> list[dict]:
    from helios.rack import Rack, fan_out
    items = runner_checks(inv) + afm_checks(inv)
    rack = Rack(inv)
    try:
        nodes = [(n, "switch") for n in rack.switches.values()] + [(n, "compute") for n in rack.computes.values()]
        for (node, role), res, exc in fan_out(lambda nr: node_checks(nr[0], nr[1], inv), nodes):
            items += res if exc is None else [item(role, node.host.name, "checks", FAIL,
                                                   f"{type(exc).__name__}: {str(exc)[:250]}")]
    finally:
        rack.close()
    for n in inv.quarantined:
        items.append(item("quarantine", n.name, "quarantined", SKIP, "left out of every test (inventory quarantine)"))
    return items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", required=True, type=Path)
    ap.add_argument("--results-dir", type=Path, default=ROOT / "results")
    a = ap.parse_args(argv)
    inv = load_inventory(a.inventory)
    t0 = time.time()
    items = check(inv)
    counts = {s: sum(i["status"] == s for i in items) for s in (OK, FAIL, WARN, SKIP)}
    print(f"\nPrerequisites of rack {inv.rack} (checked from {socket.gethostname().split('.')[0]}):")
    for i in items:
        print(f"  {MARK[i['status']]}  {i['area']:<12} {i['node']:<14} {i['check']:<30} {i['detail']}")
        if i["fix"] and i["status"] in (FAIL, WARN):
            print(f"        -> {i['fix']}")
    verdict = "FAIL" if counts[FAIL] else "WARN" if counts[WARN] else "OK"
    print(f"\n{verdict}: {counts[OK]} ok, {counts[FAIL]} fail, {counts[WARN]} warn, {counts[SKIP]} skip "
          f"({time.time() - t0:.0f} s)")
    out = a.results_dir / f"{inv.rack}-access-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "access.json").write_text(json.dumps({
        "rack": inv.rack, "checked_from": socket.gethostname().split(".")[0],
        "at": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "verdict": verdict, "counts": counts, "items": items},
        indent=1) + "\n")
    print(f"- helios access check: {out / 'access.json'} -")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
