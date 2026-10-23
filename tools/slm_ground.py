"""Facts for the model's answers that are not in a run (tools/slm_ask.py, the UI's Ask panel):

- COMMANDS: a reference of READ-ONLY commands, each one the tests themselves run (helios/sonic.py, helios/compute.py,
  helios/afm*.py), with what it shows. The model is told to give commands only from it - without it, it invented
  'sonet show port' / 'sonic-cmd' (23 Oct).
- tray_cabling(): which switch ports a compute tray is cabled to and their state, from the newest link scan of the
  rack (results/runs/<rack>/link-scans/scan-*/link_check.json), dated - so 'is ct13's link down?' gets real port names
  and the command to check them now.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

# (command, what it shows) - read-only; asicN is asic0 / asic1 on a multi-ASIC switch (g02-g17), left out on MR3
COMMANDS = [
    ("switch", "show interfaces status -n asicN -d all", "every port: admin / oper state, speed, description - the "
     "description names the cabled tray ('To ComputeTray Slot12/GPU1[49]/Port3' = slot 12 = ct13)"),
    ("switch", "show interfaces status -n asicN -d all | grep 'Slot12/'", "only the ports cabled to slot 12 (ct13); "
     "slot = ctNN - 1"),
    ("switch", "show interfaces counters -n asicN -d all", "per-port RX / TX packets, errors, drops"),
    ("switch", "show interfaces counters fec-stats -n asicN", "per-port FEC corrected / uncorrectable codewords"),
    ("switch", "show interfaces transceiver presence -n asicN", "which optics / cables are seated"),
    ("switch", "show interfaces transceiver eeprom --dom -n asicN Ethernet12", "one port's optic: type, DOM levels"),
    ("switch", "show pfc counters -n asicN", "PFC pause frames sent / received per port and priority"),
    ("switch", "show pfcwd stats -n asicN", "PFC watchdog: storms detected / restored per port queue"),
    ("switch", "show queue counters -n asicN", "packets and drops per egress queue"),
    ("switch", "show queue watermark unicast -n asicN", "peak bytes per unicast queue since the last clear"),
    ("switch", "show priority-group drop counters -n asicN", "ingress drops per priority group"),
    ("switch", "show mac -n asicN", "the forwarding (FDB) table: VLAN, MAC, port, static / dynamic"),
    ("switch", "sonic-db-cli -n asicN CONFIG_DB hgetall 'PORT_QOS_MAP|Ethernet12'", "one port's QoS maps / pfc_enable"),
    ("switch", "sonic-cfggen -n asicN -d --print-data", "the ASIC's whole running CONFIG_DB (QoS, buffers, PFC_WD)"),
    ("switch", "ip netns list", "network namespaces: only asic0, asic1 ... belong there"),
    ("switch", "show version", "SONiC version, uptime"),
    ("switch", "show system-health summary", "SONiC's own health summary of services and hardware"),
    ("switch", "show platform summary", "platform, HwSKU, ASIC count, serial"),
    ("switch", "show platform psustatus / temperature / fan", "power supplies, temperatures, fans"),
    ("switch", "show reboot-cause history", "why and when the switch rebooted"),
    ("switch", "docker ps --format '{{.Names}}'", "running containers (afm-agent, afm-controller, swss, syncd ...)"),
    ("switch", "docker exec afm-agent ps -eo pid,args", "AFM agent processes (switch-agent, node agents)"),
    ("compute", "amd-smi list --json", "GPUs the tray sees (BDF, UUID)"),
    ("compute", "amd-smi metric --usage --power --temperature --mem-usage --json", "GPU load, power, temperature"),
    ("compute", "amd-smi process --json", "processes using the GPUs (another workload?)"),
    ("compute", "afmctl show port accelerator-addr --json", "the tray's GPU fabric ports and accelerator addresses"),
    ("compute", "afmctl show station -b 0004:01:00.1 --json", "one GPU's ports (link up / down) as the tray sees them"),
    ("jump host", "ssh <tray address> true", "is the tray's management network / SSH up (port 22 here is SSH, not a "
     "switch port)"),
    ("AFM", "AFM web UI: Compute nodes / Switch nodes / vPODs / Events", "admission, health conditions, vPOD state, "
     "pending operations, events"),
]


def command_reference() -> str:
    out = ["Command reference (read-only; give commands only from this list, else say it is not in your reference; "
           "asicN = asic0 / asic1):"]
    for where, cmd, what in COMMANDS:
        out.append(f"- [{where}] {cmd}  -- {what}")
    return "\n".join(out)


def latest_link_scan(rack_results: Path) -> Path | None:
    """The newest results/runs/<rack>/link-scans/scan-*/link_check.json."""
    scans = sorted((Path(rack_results) / "link-scans").glob("scan-*/link_check.json"))
    return scans[-1] if scans else None


def trays_in(text: str) -> list[str]:
    return sorted(set(re.findall(r"\bct\d{2}\b", text or "")))


def tray_cabling(rack_results: Path, trays: list[str], max_ports: int = 6) -> str:
    """For each tray: its links per switch ASIC from the newest link scan, the ones not up in full, and the command
    that shows them now. '' when there is no scan or none of ``trays`` is in it."""
    scan = latest_link_scan(rack_results) if rack_results else None
    if not scan or not trays:
        return ""
    data = json.loads(scan.read_text())
    when = data.get("time_utc", scan.parent.name.replace("scan-", ""))
    slots = (data.get("learned") or {}).get("slots") or {}
    out = []
    for tray in trays:
        links = [x for x in data.get("links") or [] if x.get("tray") == tray]
        if not links:
            continue
        per = defaultdict(list)
        for x in links:
            per[(x.get("switch"), x.get("asic"))].append(x)
        down = [x for x in links if x.get("sw_oper") != "up"]
        slot = slots.get(tray, int(tray[2:]) - 1)
        lines = [f"Cabling of {tray} (slot {slot}) in the link scan of {when} UTC: {len(links)} GPU links, "
                 f"{len(links) - len(down)} up, {len(down)} not up."]
        for (sw, asic), xs in sorted(per.items()):
            ports = sorted((x.get("sw_port") for x in xs), key=lambda p: int(re.sub(r"\D", "", p) or 0))
            show = ", ".join(ports[:max_ports]) + (f" ... ({len(ports)} ports)" if len(ports) > max_ports else "")
            lines.append(f"  {sw} {asic}: {show}")
        for x in down[:12]:
            lines.append(f"  NOT UP: {x.get('switch')} {x.get('asic')} {x.get('sw_port')} ({x.get('desc')}) oper "
                         f"{x.get('sw_oper')} admin {x.get('sw_admin')}")
        lines.append(f"  check now (on each switch, read-only): show interfaces status -n asic0 -d all | grep 'Slot{slot}/' "
                     f"and the same with -n asic1")
        out.append("\n".join(lines))
    return "\n".join(out)
