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
    # SONiC DB reads use sudo: an account outside the redis group (g02-g17 tenant_hpe) is refused the
    # per-ASIC redis sockets ("Unable to connect to redis ... Permission denied")
    def db_keys(self, db: str, pattern: str, ns: str | None = None) -> list[str]:
        out = self.sh(f"sonic-db-cli{_ns_flag(ns)} {db} keys {shlex.quote(pattern)}", sudo=True)
        return sorted(ln.strip() for ln in out.splitlines() if ln.strip())

    def db_hash(self, db: str, key: str, ns: str | None = None) -> dict[str, str]:
        return parse_db_hash(self.sh(f"sonic-db-cli{_ns_flag(ns)} {db} hgetall {shlex.quote(key)}", sudo=True))

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

    def port_description(self, port: str, ns: str | None) -> str:
        """CONFIG_DB PORT description, e.g. 'To ComputeTray Slot0/GPU0[0]/Port9' ('' if unset)."""
        return self.run(f"sonic-db-cli{_ns_flag(ns)} CONFIG_DB hget {shlex.quote('PORT|' + port)} description",
                        sudo=True, timeout=30).stdout.strip()

    def port_descriptions(self, ns: str | None) -> dict[str, str]:
        """All CONFIG_DB PORT descriptions in one round trip: {EthernetN: 'To ComputeTray Slot0/GPU0[0]/Port9'}."""
        flag = _ns_flag(ns)
        script = (f"for k in $(sonic-db-cli{flag} CONFIG_DB keys 'PORT|Ethernet*'); do "
                  f"echo \"${{k#PORT|}}|$(sonic-db-cli{flag} CONFIG_DB hget \"$k\" description)\"; done")
        out = {}
        for line in self.sh(script, sudo=True, timeout=180).splitlines():
            port, _, desc = line.partition("|")
            if port:
                out[port.strip()] = desc.strip()
        return out

    def fdb(self, ns: str | None) -> list[dict]:
        """`show mac` rows: [{'Vlan', 'MacAddress', 'Port', 'Type'}]."""
        return parse_table(self.sh(f"show mac{_ns_flag(ns)}", timeout=120))

    def sb_node_agent_slots(self) -> list[int]:
        """Slot ids of the sb-node-agent processes inside this switch's afm-agent container (sideband mode)."""
        r = self.run("docker exec afm-agent ps -eo args", sudo=True, timeout=60)
        return [int(m.group(1)) for m in re.finditer(r"sb-node-agent\s+-slot-id\s+(\d+)", r.stdout)]

    def agent_api(self, port: int, path: str):
        """GET an AFM agent REST endpoint inside the afm-agent container (sb-node-agent: 13000+slot,
        switch-agent: 13018+). Returns parsed JSON, or None when nothing answers."""
        import json

        r = self.run(f"docker exec afm-agent curl -s -m 5 localhost:{port}{path}", sudo=True, timeout=30)
        if not r.ok or not r.stdout.strip():
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None

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
        for line in self.sh(script, sudo=True, timeout=120).splitlines():
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

    # -- health (TC-SWH-037..042) ---------------------------------------------
    def sonic_version(self) -> str:
        m = re.search(r"SONiC Software Version:\s*(\S+)", self.sh("show version", timeout=60))
        return m.group(1) if m else ""

    def container_image(self, name: str) -> str:
        r = self.run(f"docker inspect -f '{{{{.Config.Image}}}} {{{{.Image}}}}' {shlex.quote(name)}", sudo=True, timeout=60)
        return r.stdout.strip() if r.ok else ""

    def container_restarts(self) -> dict[str, tuple[int, str]]:
        """{container: (docker RestartCount, StartedAt ISO)} for every running container."""
        out = self.run("docker inspect -f '{{.Name}} {{.RestartCount}} {{.State.StartedAt}}' $(docker ps -q)",
                       sudo=True, timeout=120).stdout
        res = {}
        for line in out.splitlines():
            toks = line.split()
            if len(toks) == 3 and toks[1].isdigit():
                res[toks[0].lstrip("/")] = (int(toks[1]), toks[2])
        return res

    def system_health(self) -> tuple[bool, str]:
        """(command worked, output) of 'show system-health summary' (it crashes on some builds)."""
        r = self.run("show system-health summary", sudo=True, timeout=120)
        text = r.stdout + r.stderr
        return (r.ok and "Traceback" not in text), text

    def transceiver_presence(self, ns: str | None) -> dict[str, bool]:
        rows = parse_table(self.sh(f"show interfaces transceiver presence{_ns_flag(ns)}", timeout=120))
        return {r["Port"]: r.get("Presence", "").strip().lower() == "present" for r in rows if r.get("Port")}

    def transceiver_dom(self, port: str, ns: str | None) -> dict[str, float]:
        """Numeric DOM readings ('RX1Power' -> dBm, 'Temperature' -> C, 'Vcc' -> V ...) of one module."""
        out = self.run(f"show interfaces transceiver eeprom --dom{_ns_flag(ns)} {shlex.quote(port)}",
                       sudo=True, timeout=120).stdout
        dom = {}
        for m in re.finditer(r"^\s*([A-Za-z][\w ]*?)\s*:\s*(-?\d+(?:\.\d+)?)\s*(dBm|mW|C|Volts|V|mA)\b", out, re.M):
            dom[m.group(1).replace(" ", "")] = float(m.group(2))
        return dom

    def fec_stats(self, ns: str | None) -> dict[str, dict[str, str]]:
        rows = parse_table(self.sh(f"show interfaces counters fec-stats{_ns_flag(ns)}", timeout=120))
        return {r["IFACE"]: r for r in rows if self.is_fabric_port(r.get("IFACE", ""))}

    def reboot_cause(self) -> str:
        return self.sh("show reboot-cause", timeout=60).strip()

    def reboot_history(self) -> list[dict]:
        return parse_table(self.run("show reboot-cause history", timeout=60).stdout)

    def core_files(self) -> list[str]:
        r = self.run("ls -1 /var/core", sudo=True, timeout=60)
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()] if r.ok else []

    # -- logs, PFC, saved config (TC-SWH-043..047) -----------------------------
    SYSLOGS = "/var/log/syslog.1 /var/log/syslog"

    def epoch(self) -> int:
        return int(self.sh("date +%s", timeout=30).strip())

    def syslog_grep(self, pattern: str) -> list[str]:
        """Lines of the current and previous syslog matching an extended regex (sudo; files may be absent)."""
        r = self.run(f"grep -hE {shlex.quote(pattern)} {self.SYSLOGS}", sudo=True, timeout=180)
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    def pfc_counters(self, ns: str | None) -> dict[str, dict[str, float]]:
        """{'Rx'|'Tx': {port: total pause frames over PFC0..7}} from 'show pfc counters'."""
        out = self.sh(f"show pfc counters{_ns_flag(ns)}", timeout=120)
        res: dict[str, dict[str, float]] = {}
        for block in re.split(r"\n\s*\n", out):
            rows = parse_table(block)
            if not rows:
                continue
            key = next(iter(rows[0]))                  # 'Port Rx' / 'Port Tx'
            d = "Rx" if "Rx" in key else "Tx"
            res[d] = {r[key]: sum(float(v.replace(",", "")) for k, v in r.items()
                                  if k.startswith("PFC") and v.replace(",", "").replace(".", "").isdigit())
                      for r in rows if r.get(key)}
        return res

    def pfcwd_storms(self, ns: str | None) -> dict[str, int]:
        """{port:queue: storms detected} from 'show pfcwd stats' (empty when the PFC watchdog is not configured)."""
        out = self.run(f"show pfcwd stats{_ns_flag(ns)}", timeout=120).stdout
        res = {}
        for r in parse_table(out):
            sd = r.get("STORM DETECTED/RESTORED", "")
            if r.get("QUEUE") and "/" in sd:
                res[r["QUEUE"]] = int(sd.split("/")[0] or 0)
        return res

    def qos_maps(self, ns: str | None) -> dict[str, dict[str, str]]:
        return {k.split("|", 1)[1]: self.db_hash("CONFIG_DB", k, ns)
                for k in self.db_keys("CONFIG_DB", "PORT_QOS_MAP|*", ns)}

    def config_running_vs_saved(self, ns: str | None) -> tuple[dict, dict]:
        """(running CONFIG_DB, saved /etc/sonic/config_db[N].json) of the host (ns None) or one ASIC."""
        import json

        run = json.loads(self.sh(f"sonic-cfggen{_ns_flag(ns)} -d --print-data", sudo=True, timeout=180))
        f = f"/etc/sonic/config_db{ns[4:] if ns else ''}.json"
        saved = json.loads(self.sh(f"cat {f}", sudo=True, timeout=120))
        return run, saved

    def close(self) -> None:
        self.t.close()
