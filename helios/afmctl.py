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
    failed_ports: list[str] = field(default_factory=list)
    busy: bool = False

    @property
    def ok(self) -> bool:
        return self.request_fail == 0 and bool(self.total)


_SUMMARY_RE = re.compile(r"IFoE Request\s*:\s*(\d+)/(\d+)\s+PASS,\s*(\d+)/\d+\s+fail")


def parse_ping(text: str) -> PingResult:
    res = PingResult(busy="Device or resource busy" in text)
    m = _SUMMARY_RE.search(text)
    if m:
        res.request_pass, res.total, res.request_fail = int(m.group(1)), int(m.group(2)), int(m.group(3))
    for line in text.splitlines():
        toks = line.split()
        # per-port table: col2 = port, cols 4/6/8 = Request/Response/Non-IFoE result
        if len(toks) >= 8 and toks[0].isdigit() and toks[1].isdigit() and "FAIL" in (toks[3], toks[5], toks[7]):
            res.failed_ports.append(toks[1])
    return res


_DESC_RE = re.compile(r"Slot(\d+)/GPU(\d+)\[(\d+)\]/Port(\d+)")


def parse_port_description(desc: str):
    """'To ComputeTray Slot0/GPU2[2]/Port9' -> (slot, gpu, acc, port) or None."""
    m = _DESC_RE.search(desc or "")
    return tuple(int(x) for x in m.groups()) if m else None


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
