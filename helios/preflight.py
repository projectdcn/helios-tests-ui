"""Datapath pre-flight: refuse to start IFoE traffic on a fabric that is known to be broken.

Learned on MR3: a switch plane silently dropping ~100% of one tray's ingress made the first RCCL
collective hang forever (and the tray later stopped serving SSH). Every link, LLDP, VLAN/FDB entry
and RX_ERR counter looked healthy; only the drop counters and a per-port ping showed it. Running
traffic into that state wastes a timeout per test at best and wedges the host at worst.

Checks (fast, read-only). Per tray - a tray that fails one is EXCLUDED from the test (reason recorded), and the
run is blocked only when fewer than the test's minimum trays remain (``exclude_unhealthy: false`` blocks instead):
  * answers SSH within ``datapath.preflight.ssh_timeout_s`` (20 s)
  * every GPU on it is config_phase ACTIVE (afmctl)
  * the GPUs can be opened and are all visible (``amd-smi list --json``, run like the workloads: with sudo when
    commands.datapath.sudo is set). g02-g17, 14 Oct: tenant-hpe is not in 'render' -> /dev/kfd permission denied ->
    RCCL 'no ROCm-capable device is detected'
  * no other process is using the GPUs (``amd-smi process --json``; ``busy_check``)
  * the tools the test declares are installed (``@pytest.mark.datapath(tools=[...])``: transferbench,
    rccl_tests, rccl_smoke). g02-g17, 14 Oct: TransferBench missing on 7 trays
  * ping probe (below) - a tray with a black-holed port is excluded
Rack-wide (always blocking):
  * no switch fabric port is dropping faster than ``max_rate_pps`` (100/s) over ``window_s`` (5 s)
Ping probe (``ping_probe``, default on): one afmctl L2 ping per GPU to the next GPU on the same tray over
    every LINK_UP port, so every switching plane carries a frame before RCCL/TransferBench does. Needed
    because a broken plane only starts dropping once traffic is sent on it - an idle fabric shows no drops.
"""
from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass, field

from .afmctl import Afmctl, parse_port_description
from .parsers import to_float
from .rack import fan_out


def _drop_rates(switch, window: float) -> dict[tuple[str | None, str], float]:
    nss = switch.namespaces()
    before = {ns: switch.interfaces_counters(ns) for ns in nss}
    t0 = time.monotonic()
    time.sleep(window)
    after = {ns: switch.interfaces_counters(ns) for ns in nss}
    dt = max(time.monotonic() - t0, 1e-6)
    rates = {}
    for ns in nss:
        for port, row in after[ns].items():
            for col in ("RX_DRP", "TX_DRP"):
                a, b = to_float(row.get(col)), to_float(before[ns].get(port, {}).get(col))
                if a is not None and b is not None and a > b:
                    rates[(ns, f"{port} {col}")] = (a - b) / dt
    return rates


@dataclass
class Assessment:
    excluded: dict[str, str] = field(default_factory=dict)    # tray -> reason it was left out
    problems: list[str] = field(default_factory=list)          # blocking (rack-wide, or too few trays left)
    evidence: list[dict] = field(default_factory=list)         # failed probe pings: command, rc, afmctl output

    @property
    def ok(self) -> bool:
        return not self.problems


TOOLS = {   # tool -> (inventory command key whose first program is the binary, fallback path)
    "transferbench": ("transferbench.in_domain", "TransferBench"),
    "rccl_smoke": ("rccl_smoke.bin", None),
    "rccl_tests": (None, None),
}


def tool_path(inventory, tool: str) -> str | None:
    if tool == "rccl_tests":
        if not inventory.cmd("rccl.in_domain"):
            return None       # not configured: the test skips on its own
        return f"{inventory.cmd('rccl.tests_dir', '/opt/rccl-tests')}/build/all_reduce_perf"
    key, default = TOOLS[tool]
    t = inventory.cmd(key) if key else None
    if not t:
        return default if key and inventory.cmd(key.split(".")[0]) else None
    for tok in shlex.split(str(t)):
        if "=" in tok.split("/")[0] or tok.startswith("{"):
            continue        # env assignments / placeholders before the program
        return tok
    return default


def _gpu_access(c, inventory, sudo: bool) -> str:
    r = c.run("amd-smi list --json", sudo=sudo, timeout=60)
    text = r.stdout + r.stderr
    if "permission denied" in text.lower():
        return (f"{c.name}: cannot open the GPU device nodes as {c.host.user} (/dev/kfd permission denied - user not in "
                f"the 'render' group); set commands.datapath.sudo: true or add the user to 'render'")
    if r.rc == 127 or not r.ok:
        return ""          # amd-smi not available here: nothing to judge
    try:
        n = len(json.loads(r.stdout))
    except ValueError:
        return ""
    if c.gpus and n < c.gpus:
        return f"{c.name}: only {n} of {c.gpus} GPUs visible (amd-smi list)"
    return ""


def _gpu_users(c, sudo: bool) -> str:
    r = c.run("amd-smi process --json", sudo=sudo, timeout=60)
    if not r.ok:
        return ""
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        return ""
    users = []
    for g in doc if isinstance(doc, list) else []:
        for pr in g.get("process_list") or []:
            info = pr.get("process_info")
            if isinstance(info, dict):
                users.append(f"GPU{g.get('gpu')}: {info.get('name', '?')}({info.get('pid', '?')})")
    return f"{c.name}: GPUs in use by another workload: {', '.join(users[:6])}" if users else ""


def _tools_missing(c, paths: dict[str, str]) -> str:
    miss = []
    for tool, path in paths.items():
        q = shlex.quote(path)
        r = c.run(f"command -v {q} >/dev/null 2>&1 || test -x {q}", timeout=30)
        if r.rc not in (0, 127):          # 127: no shell answer (replay) - cannot judge
            miss.append(f"{tool} ({path})")
    return f"{c.name}: not installed: {', '.join(miss)}" if miss else ""


def assess(rack, computes, inventory, tools=(), min_computes: int = 1) -> Assessment:
    """Judge every tray (see module doc); never raises for one bad tray."""
    from .datapath import use_sudo

    th = lambda k, d: inventory.th(f"datapath.preflight.{k}", d)  # noqa: E731
    sudo = use_sudo(inventory)
    paths = {t: p for t in tools if (p := tool_path(inventory, t))}
    out = Assessment()

    def probe(c):
        r = c.run("true", timeout=float(th("ssh_timeout_s", 20)))
        if not r.ok:
            return f"{c.name}: not responding (rc={r.rc})", set()
        devs = Afmctl(c, timeout=60).devices()
        accs = {int(d.accel_id) for d in devs if d.accel_id.isdigit()}
        bad = [f"{d.bdf}={d.config_phase}" for d in devs if d.config_phase != "ACTIVE"]
        if bad:
            return f"{c.name}: GPUs not ACTIVE: {bad}", accs
        for msg in (_gpu_access(c, inventory, sudo) if th("gpu_check", True) else "",
                    _gpu_users(c, sudo) if th("busy_check", True) else "",
                    _tools_missing(c, paths) if paths else ""):
            if msg:
                return msg, accs
        return "", accs

    accs: set[int] = set()
    for c, res, exc in fan_out(probe, computes):
        if exc:
            out.excluded[c.name] = (f"{c.name}: {exc}" if isinstance(exc, FileNotFoundError) else   # missing SSH key
                                    f"{c.name}: unreachable/unresponsive ({type(exc).__name__}: {str(exc)[:120]})")
            continue
        msg, a = res
        accs |= a
        if msg:
            out.excluded[c.name] = msg

    healthy = [c for c in computes if c.name not in out.excluded]
    if th("ping_probe", True) and healthy:
        for name, msg in _ping_probe(healthy, float(th("ping_timeout_s", 200)), out.evidence):
            out.excluded.setdefault(name, msg)

    window, max_rate = float(th("window_s", 5)), float(th("max_rate_pps", 100))
    for sw, rates, exc in fan_out(lambda s: _drop_rates(s, window), list(rack.switches.values())):
        if exc:
            out.problems.append(f"{sw.name}: cannot read counters: {exc}")
            continue
        hot = sorted(((r, ns, p) for (ns, p), r in rates.items() if r > max_rate), reverse=True)
        relevant = []
        for r, ns, p in hot:
            desc = sw.port_description(p.split()[0], ns)
            cab = parse_port_description(desc)
            # only ports cabled to the accelerators under test matter; unknown cabling counts (conservative)
            if cab is None or not accs or cab[2] in accs:
                relevant.append(f"{ns}/{p} {r:,.0f}/s ({desc or 'no description'})")
        if relevant:
            out.problems.append(f"{sw.name}: {len(relevant)} fabric port(s) dropping now: " + "; ".join(relevant[:8]))

    left = len(computes) - len(out.excluded)
    if out.excluded and not th("exclude_unhealthy", True):
        out.problems += list(out.excluded.values())
    elif left < min_computes:
        out.problems.append(f"only {left} healthy compute tray(s) left, the test needs {min_computes}")
        out.problems += list(out.excluded.values())
    return out


def run(rack, computes, inventory) -> list[str]:
    """Blocking problems with every unhealthy tray counted as blocking (the pre-tranche behaviour)."""
    a = assess(rack, computes, inventory)
    return a.problems + [m for m in a.excluded.values() if m not in a.problems]


def _ping_probe(computes, timeout: float, evidence: list | None = None) -> list[tuple[str, str]]:
    """One in-tray L2 ping per GPU (ring: GPU i -> GPU i+1) over all LINK_UP ports on both ends. Each failed
    ping's command and afmctl output is appended to ``evidence``."""
    from .afmctl import ping_evidence

    def one(c):
        a = Afmctl(c, timeout=60)
        devs = a.devices()
        if len(devs) < 2:
            return []
        up = {d.bdf: {p.id for p in a.ports(d.bdf) if p.up} for d in devs}
        fails = []
        for i, d in enumerate(devs):
            dst = devs[(i + 1) % len(devs)]
            common = sorted(up[d.bdf] & up[dst.bdf])
            if not common:
                fails.append(f"in-tray ping probe {c.name} GPU {d.bdf} (acc {d.accel_id}) -> acc {dst.accel_id}: "
                             "no LINK_UP port in common")
                continue
            res, r = a.ping(d.bdf, dst.accel_id, common, timeout=timeout)
            if evidence is not None and (res.total is None or not res.ok):
                evidence.append(ping_evidence(f"[probe] {c.name}/{d.bdf} -> acc {dst.accel_id}", r))
            if res.total is None:
                fails.append(f"in-tray ping probe {c.name} GPU {d.bdf} (acc {d.accel_id}) -> acc {dst.accel_id}: "
                             f"no ping summary from afmctl (rc={r.rc})")
            elif not res.ok:
                fails.append(f"in-tray ping probe {c.name} GPU {d.bdf} (acc {d.accel_id}) -> acc {dst.accel_id} "
                             f"lost frames: {res.describe()}")
        return fails

    out = []
    for c, fails, exc in fan_out(one, computes):
        if exc:
            out.append((c.name, f"in-tray ping probe on {c.name} could not run: {exc}"))
        out += [(c.name, f) for f in fails or []]
    return out
