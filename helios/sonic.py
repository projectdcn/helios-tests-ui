"""SONiC switch-tray driver (multi-ASIC aware)."""
from __future__ import annotations

import re
import shlex

from .inventory import Host
from .parsers import parse_db_hash, parse_kv, parse_table
from .transport import CmdResult, Transport

DEFAULT_FABRIC_PORT_RE = r"^Ethernet\d+$"  # excludes Ethernet-BP*/-IB*/-Rec* internal ports


def _ns_flag(ns: str | None) -> str:
    return f" -n {ns}" if ns else ""


class SonicSwitch:
    def __init__(self, host: Host, transport: Transport, fabric_port_re: str = DEFAULT_FABRIC_PORT_RE,
                 exclude_ports: list[str] | None = None):
        self.host = host
        self.name = host.name
        self.t = transport
        self.fabric_port_re = re.compile(host.extra.get("fabric_port_re", fabric_port_re))
        self.exclude_ports = set(host.extra.get("exclude_ports", exclude_ports or []))
        self._ns: list[str | None] | None = None

    # -- primitives ---------------------------------------------------------
    def run(self, cmd: str, **kw) -> CmdResult:
        return self.t.run(cmd, **kw)

    def sh(self, cmd: str, **kw) -> str:
        return self.run(cmd, **kw).check().stdout

    # -- namespaces ---------------------------------------------------------
    def netns(self) -> list[str]:
        out = self.sh("ip netns list")
        return sorted({ln.split()[0] for ln in out.splitlines() if ln.strip()})

    def namespaces(self) -> list[str | None]:
        """ASIC namespaces (asic0, asic1, ...) or [None] on a single-ASIC image."""
        if self._ns is None:
            asics = [n for n in self.netns() if re.fullmatch(r"asic\d+", n)]
            self._ns = sorted(asics, key=lambda n: int(n[4:])) or [None]
        return self._ns

    def containers(self) -> list[str]:
        return [ln.strip() for ln in self.sh("docker ps --format '{{.Names}}'", sudo=True).splitlines()
                if ln.strip()]

    # -- platform -----------------------------------------------------------
    def fans(self) -> list[dict]:
        return parse_table(self.sh("show platform fan"))

    def psus(self) -> list[dict]:
        return parse_table(self.sh("show platform psustatus"))

    def temperatures(self) -> list[dict]:
        return parse_table(self.sh("show platform temperature"))

    def platform_summary(self) -> dict[str, str]:
        return parse_kv(self.sh("show platform summary"))

    def syseeprom(self) -> dict[str, str]:
        rows = parse_table(self.sh("show platform syseeprom", sudo=True))
        return {r.get("TLV Name", ""): r.get("Value", "") for r in rows}

    def firmware_status(self) -> list[dict]:
        return parse_table(self.sh("show platform firmware status", sudo=True))

    def fwutil_status(self) -> list[dict]:
        return parse_table(self.sh("fwutil show status", sudo=True))

    # -- databases ----------------------------------------------------------
    def db_keys(self, db: str, pattern: str, ns: str | None = None) -> list[str]:
        out = self.sh(f"sonic-db-cli{_ns_flag(ns)} {db} keys {shlex.quote(pattern)}")
        return sorted(ln.strip() for ln in out.splitlines() if ln.strip())

    def db_hash(self, db: str, key: str, ns: str | None = None) -> dict[str, str]:
        return parse_db_hash(self.sh(f"sonic-db-cli{_ns_flag(ns)} {db} hgetall {shlex.quote(key)}"))

    def psu_info(self) -> dict[str, dict[str, str]]:
        """STATE_DB PSU_INFO keyed by PSU name ('PSU 1')."""
        return {k.split("|", 1)[1]: self.db_hash("STATE_DB", k)
                for k in self.db_keys("STATE_DB", "PSU_INFO|*")}

    def fan_info(self) -> dict[str, dict[str, str]]:
        return {k.split("|", 1)[1]: self.db_hash("STATE_DB", k)
                for k in self.db_keys("STATE_DB", "FAN_INFO|*")}

    def vlans(self, ns: str | None) -> list[str]:
        return [k.split("|", 1)[1] for k in self.db_keys("CONFIG_DB", "VLAN|*", ns)]

    def vlan_members(self, ns: str | None) -> dict[str, list[str]]:
        members: dict[str, list[str]] = {}
        for k in self.db_keys("CONFIG_DB", "VLAN_MEMBER|*", ns):
            _, vlan, port = k.split("|", 2)
            members.setdefault(vlan, []).append(port)
        return members

    def lldp_ports(self, ns: str | None) -> list[str]:
        """Local ports with an LLDP neighbour (APPL_DB LLDP_ENTRY_TABLE)."""
        return [k.split(":", 1)[1] for k in self.db_keys("APPL_DB", "LLDP_ENTRY_TABLE:*", ns)]

    # -- interfaces ---------------------------------------------------------
    def is_fabric_port(self, name: str) -> bool:
        return bool(self.fabric_port_re.match(name)) and name not in self.exclude_ports

    def interfaces_status(self, ns: str | None) -> list[dict]:
        rows = parse_table(self.sh(f"show interfaces status{_ns_flag(ns)} -d all"))
        return [r for r in rows if self.is_fabric_port(r.get("Interface", ""))]

    def interfaces_counters(self, ns: str | None) -> dict[str, dict]:
        rows = parse_table(self.sh(f"show interfaces counters{_ns_flag(ns)} -d all"))
        return {r["IFACE"]: r for r in rows if self.is_fabric_port(r.get("IFACE", ""))}

    def all_interfaces_status(self) -> dict[str | None, list[dict]]:
        return {ns: self.interfaces_status(ns) for ns in self.namespaces()}

    def port_speeds(self, ns: str | None) -> dict[str, tuple[str, str]]:
        """{port: (configured speed, oper speed)} from CONFIG_DB PORT / STATE_DB PORT_TABLE, in one round trip."""
        flag = _ns_flag(ns)
        script = (f"for k in $(sonic-db-cli{flag} CONFIG_DB keys 'PORT|Ethernet*'); do p=${{k#PORT|}}; "
                  f"echo \"$p $(sonic-db-cli{flag} CONFIG_DB hget \"$k\" speed) "
                  f"$(sonic-db-cli{flag} STATE_DB hget \"PORT_TABLE|$p\" speed)\"; done")
        out = {}
        for line in self.sh(script, timeout=120).splitlines():
            toks = line.split()
            if toks and self.is_fabric_port(toks[0]):
                out[toks[0]] = (toks[1] if len(toks) > 1 else "", toks[2] if len(toks) > 2 else "")
        return out

    # -- services / containers ------------------------------------------------
    @staticmethod
    def per_asic(base: str, ns: str | None, sep: str = "") -> str:
        """swss + asic1 -> 'swss1' (container) or 'swss@1' (service, sep='@')."""
        return f"{base}{sep}{ns.removeprefix('asic')}" if ns else base

    def restart_service(self, base: str, ns: str | None, timeout: float = 600) -> CmdResult:
        return self.run(f"systemctl restart {self.per_asic(base, ns, '@')}", sudo=True, timeout=timeout)

    def critical_processes(self, container: str) -> list[str]:
        out = self.run(f"docker exec {container} cat /etc/supervisor/critical_processes", sudo=True)
        names = []
        for line in out.stdout.splitlines():
            kind, _, name = line.strip().partition(":")
            if kind == "program" and name:
                names.append(name)
        return names

    def supervisor_status(self, container: str) -> dict[str, str]:
        # supervisorctl exits non-zero when anything isn't RUNNING; the table is still what we want.
        out = self.run(f"docker exec {container} supervisorctl status", sudo=True).stdout
        status = {}
        for line in out.splitlines():
            toks = line.split()
            if len(toks) >= 2:
                status[toks[0].split(":")[-1]] = toks[1]
        return status

    # -- images ---------------------------------------------------------------
    def images(self) -> dict:
        """sonic-installer list -> {'current': str, 'next': str, 'available': [..]}"""
        out = self.sh("sonic-installer list", sudo=True)
        info: dict = {"current": "", "next": "", "available": []}
        section = None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Current:"):
                info["current"] = s.split(":", 1)[1].strip()
            elif s.startswith("Next:"):
                info["next"] = s.split(":", 1)[1].strip()
            elif s.startswith("Available:"):
                section = "available"
            elif section == "available" and s:
                info["available"].append(s)
        return info

    def close(self) -> None:
        self.t.close()
