"""afmctl (compute-side AFM CLI) parsers and helpers.

Formats taken from the full-rack execution log (afmctl 0.0.1-85):
- ``afmctl show device --json``: {"device": [{"bdf", "spec": {"accelerator_id", "config_phase", ...}}]} (ids are strings)
- ``afmctl show port -b <bdf> --json``: {"device": [{"bdf", "port": [{"id", "spec": {"admin_state"},
  "status": {"link_status"}}]}]}
- ``afmctl test ping``: always exits 0; graded on "IFoE Request : 27/27 PASS, 0/27 fail (0.00% loss)";
  not re-entrant per host ("Device or resource busy").
stderr may carry a login banner, so every command discards it before JSON parsing.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field


@dataclass
class Device:
    bdf: str
    accel_id: str
    config_phase: str


@dataclass
class Port:
    id: int
    admin_state: str
    link_status: str

    @property
    def up(self) -> bool:
        return self.admin_state.lower() == "enabled" and "UP" in self.link_status.upper()


def _devices(doc) -> list[dict]:
    # the log's own parsers accept device|devices|list
    if isinstance(doc, list):
        return doc
    for key in ("device", "devices"):
        if isinstance(doc.get(key), list):
            return doc[key]
    return []


def parse_show_device(text: str) -> list[Device]:
    out = []
    for d in _devices(json.loads(text)):
        spec = d.get("spec") or {}
        out.append(Device(str(d.get("bdf", "")), str(spec.get("accelerator_id", "")), str(spec.get("config_phase", ""))))
    return sorted(out, key=lambda x: x.bdf)


def parse_show_port(text: str) -> dict[str, list[Port]]:
    out: dict[str, list[Port]] = {}
    for d in _devices(json.loads(text)):
        ports = []
        for p in d.get("port") or []:
            pid = p.get("id", p.get("port_id"))
            if pid is None:
                continue
            ports.append(Port(int(pid), str((p.get("spec") or {}).get("admin_state", "")),
                              str((p.get("status") or {}).get("link_status", ""))))
        out[str(d.get("bdf", ""))] = sorted(ports, key=lambda x: x.id)
    return out


@dataclass
class PingResult:
    request_pass: int | None = None
    request_fail: int | None = None
    total: int | None = None
    response_fail: int | None = None          # afmctl's "IFoE Response" summary
    non_ifoe_fail: int | None = None          # afmctl's "Non-IFoE" summary
    failed_ports: list[str] = field(default_factory=list)
    port_checks: dict = field(default_factory=dict)   # port -> checks that failed on it ("req", "rsp", "non-IFoE")
    busy: bool = False

    @property
    def ok(self) -> bool:
        """No loss in ANY of afmctl's three checks (IFoE request, IFoE response, non-IFoE ping) - 16 Oct: a port can
        fail only the non-IFoE check (CT03 port 25), which the request count alone would miss."""
        return (bool(self.total) and self.request_fail == 0 and not self.response_fail and not self.non_ifoe_fail
                and not self.failed_ports)

    def describe(self) -> str:
        """'IFoE request 1/36, response 0/36, non-IFoE 1/36 failed; ports 25 (non-IFoE), 26 (req)' - afmctl's three
        summaries and, per failing port, which checks failed (the summaries count checks, not ports)."""
        t = self.total or "?"
        counts = (f"IFoE request {self.request_fail}/{t}, response {self.response_fail if self.response_fail is not None else '?'}/{t}, "
                  f"non-IFoE {self.non_ifoe_fail if self.non_ifoe_fail is not None else '?'}/{t} failed")
        ports = ", ".join(f"{p} ({'+'.join(self.port_checks.get(p, []))})" for p in self.failed_ports)
        return counts + (f"; ports {ports}" if ports else "")


_SUMMARY_RE = re.compile(r"IFoE Request\s*:\s*(\d+)/(\d+)\s+PASS,\s*(\d+)/\d+\s+fail")
_RSP_RE = re.compile(r"IFoE Response\s*:\s*\d+/\d+\s+PASS,\s*(\d+)/\d+\s+fail")
_NON_RE = re.compile(r"Non-IFoE\s*:\s*\d+/\d+\s+PASS,\s*(\d+)/\d+\s+fail")
_CHECKS = ("req", "rsp", "non-IFoE")


def parse_ping(text: str) -> PingResult:
    res = PingResult(busy="Device or resource busy" in text)
    m = _SUMMARY_RE.search(text)
    if m:
        res.request_pass, res.total, res.request_fail = int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = _RSP_RE.search(text)
    if m:
        res.response_fail = int(m.group(1))
    m = _NON_RE.search(text)
    if m:
        res.non_ifoe_fail = int(m.group(1))
    for line in text.splitlines():
        toks = line.split()
        # per-port table: col2 = port, cols 4/6/8 = Request/Response/Non-IFoE result
        if len(toks) >= 8 and toks[0].isdigit() and toks[1].isdigit() and "FAIL" in (toks[3], toks[5], toks[7]):
            res.failed_ports.append(toks[1])
            res.port_checks[toks[1]] = [c for c, v in zip(_CHECKS, (toks[3], toks[5], toks[7])) if v == "FAIL"]
    return res


_DESC_RE = re.compile(r"Slot(\d+)/GPU(\d+)\[(\d+)\]/Port(\d+)")


def parse_port_description(desc: str):
    """'To ComputeTray Slot0/GPU2[2]/Port9' -> (slot, gpu, acc, port) or None."""
    m = _DESC_RE.search(desc or "")
    return tuple(int(x) for x in m.groups()) if m else None


def check_acc_renumbering(pairs, checks, what: str, tray_of: dict | None = None) -> dict[str, str]:
    """The switch port descriptions and AFM/afmctl may number a tray's accelerators differently (on g02-g17 the
    GPU AFM calls acc 0 is described as acc 3, ...). That is a naming convention, not a cabling fault - as long
    as it is ONE consistent renumbering: every AFM accelerator maps to exactly one described accelerator and no
    two AFM accelerators map to the same one - and, with ``tray_of`` ({afm acc: tray}), only WITHIN a tray: a
    described acc must be one of that tray's own AFM accelerators (an off-by-one shift into the next tray fails).
    ``pairs``: iterable of (afm acc, described acc, context).
    Returns {afm acc: described acc} (record it); a conflict is a failed check naming both contexts."""
    fwd: dict[str, tuple[str, str]] = {}
    back: dict[str, tuple[str, str]] = {}
    for afm, desc, ctx in pairs:
        afm, desc = str(afm), str(desc)
        if afm in fwd and fwd[afm][0] != desc:
            checks.fail(f"{what}: acc {afm} is described as acc {fwd[afm][0]} ({fwd[afm][1]}) and as acc {desc} ({ctx})")
            continue
        if desc in back and back[desc][0] != afm:
            checks.fail(f"{what}: described acc {desc} is AFM acc {back[desc][0]} ({back[desc][1]}) and acc {afm} ({ctx})")
            continue
        fwd.setdefault(afm, (desc, ctx))
        back.setdefault(desc, (afm, ctx))
    if tray_of:
        tray_of = {str(k): v for k, v in tray_of.items()}
        for afm, (desc, ctx) in fwd.items():
            t = tray_of.get(afm)
            if t is not None and tray_of.get(desc) != t:
                checks.fail(f"{what}: {t} acc {afm} is described as acc {desc}, which is not one of {t}'s accelerators ({ctx})")
    return {k: v[0] for k, v in sorted(fwd.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else kv[0])}


def parse_accel_addr(text: str) -> dict[str, dict[int, dict[str, str]]]:
    """`afmctl show port accelerator-addr --json` -> {bdf: {port_id: {accelerator_id: mac}}}."""
    out: dict[str, dict[int, dict[str, str]]] = {}
    for d in _devices(json.loads(text)):
        ports = {}
        for p in d.get("port") or []:
            ports[int(p.get("port_id", p.get("id")))] = {
                str(a["accelerator_id"]): str(a["mac_addr"]).lower() for a in p.get("accelerator_addr") or []}
        out[str(d.get("bdf", ""))] = ports
    return out


def parse_vpod_accels(text: str) -> dict[str, tuple[str, set[str]]]:
    """`afmctl show device --json` -> {bdf: (own accelerator_id, set(vpod_accelerators))}."""
    out = {}
    for d in _devices(json.loads(text)):
        spec = d.get("spec") or {}
        out[str(d.get("bdf", ""))] = (str(spec.get("accelerator_id", "")),
                                      {str(a) for a in spec.get("vpod_accelerators") or []})
    return out


class Afmctl:
    """afmctl on one compute tray (always sudo, stderr discarded)."""

    def __init__(self, compute, timeout: float = 60):
        self.c = compute
        self.timeout = timeout

    def _json(self, args: str) -> str:
        return self.c.run(f"afmctl {args} 2>/dev/null", sudo=True, timeout=self.timeout).check().stdout

    def devices(self) -> list[Device]:
        return parse_show_device(self._json("show device --json"))

    def accel_addr(self) -> dict[str, dict[int, dict[str, str]]]:
        return parse_accel_addr(self._json("show port accelerator-addr --json"))

    def vpod_accels(self) -> dict[str, tuple[str, set[str]]]:
        return parse_vpod_accels(self._json("show device --json"))

    def ports_raw(self, bdf: str) -> list[dict]:
        """Raw `afmctl show port -b <bdf> --json` port objects (spec, status, status.ifcp.*)."""
        doc = json.loads(self._json(f"show port -b {bdf} --json"))
        return next((d.get("port") or [] for d in _devices(doc) if str(d.get("bdf")) == bdf), [])

    def ports(self, bdf: str) -> list[Port]:
        return parse_show_port(self._json(f"show port -b {bdf} --json")).get(bdf, [])

    def ping(self, src_bdf: str, dst_acc: str, ports: list[int], *, timeout: float = 200,
             busy_retries: int = 5, busy_wait: float = 20):
        """One bounded ping; retried while the host is busy with another ping. Returns (PingResult, CmdResult)."""
        cmd = (f"timeout {int(timeout)} afmctl test ping -b {src_bdf} -c 1 --dst-accelerator {dst_acc} "
               f"--port {','.join(map(str, ports))} 2>&1")
        for attempt in range(busy_retries + 1):
            r = self.c.run(cmd, sudo=True, timeout=timeout + 30)
            res = parse_ping(r.stdout)
            if not res.busy or attempt == busy_retries:
                return res, r
            time.sleep(busy_wait)


def ping_evidence(pair: str, r, limit: int = 8000) -> dict:
    """What a failed ``afmctl test ping`` did, for the vendor: the exact command, where / when it ran, its exit code
    and afmctl's own output (kept in the result properties, so a long test log can never cut it off)."""
    from datetime import datetime, timezone
    out = r.stdout + (f"\n[stderr]\n{r.stderr}" if r.stderr.strip() else "")
    return {"pair": pair, "host": r.host, "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "cmd": r.cmd, "rc": r.rc, "duration_s": round(r.duration, 1), "output": out[-limit:]}


def mac_stat_ports(doc: dict) -> list[tuple[str, list[dict]]]:
    """[(bdf, ports)] from ``afmctl show port statistics mac --json``. Real afmctl (g02-g17, 16 Oct) lists each
    device's ports under "ports" (each {station_id, port_id, statistics: {tx_total_packets, rx_total_packets, ...}});
    "port" / "device" are accepted too."""
    return [(d.get("bdf"), d.get("ports") or d.get("port") or [])
            for d in doc.get("devices") or doc.get("device") or []]
