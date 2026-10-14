"""Generate selftest/replay/: canned SONiC + compute outputs for a healthy 1-switch / 2-compute rack.

Run:  python selftest/make_replay.py   (regenerates replay/ in place)
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml

OUT = Path(__file__).parent / "replay"
NS = ["asic0", "asic1"]
PORTS = {"asic0": ["Ethernet0", "Ethernet8", "Ethernet16", "Ethernet24"],
         "asic1": ["Ethernet256", "Ethernet264", "Ethernet272", "Ethernet280"]}
INTERNAL = {"asic0": "Ethernet-BP0", "asic1": "Ethernet-BP256"}


def table(headers: list[str], rows: list[list], right: set[int] = frozenset()) -> str:
    """tabulate 'simple' style, as SONiC prints it."""
    cols = list(zip(headers, *rows))
    widths = [max(len(str(c)) for c in col) for col in cols]

    def fmt(cells):
        return "  ".join(str(c).rjust(w) if i in right else str(c).ljust(w)
                         for i, (c, w) in enumerate(zip(cells, widths))).rstrip()

    return "\n".join([fmt(headers), "  ".join("-" * w for w in widths)] + [fmt(r) for r in rows]) + "\n"


rules: list[dict] = []
files: dict[str, str] = {}


def add(cmd: str, text: str = "", *, name: str | None = None, rc: int = 0, host: str | None = None,
        regex: bool = False, seq: list[tuple[str, str]] | None = None, rcs: list[int] | None = None):
    """seq=[(filename, text), ...] / rcs=[...]: successive calls cycle through them."""
    rule = {"cmd": cmd if regex else f"^{re.escape(cmd)}$", "rc": rc}
    if rcs:
        rule["rcs"] = rcs
    if host:
        rule["host"] = host
    if seq:
        for fname, t in seq:
            files[fname] = t
        rule["files"] = [f for f, _ in seq]
    elif name:
        files[name] = text
        rule["file"] = name
    else:
        rule["stdout"] = text
    rules.append(rule)


def db(ns, dbname, verb, arg):
    flag = f" -n {ns}" if ns else ""
    return f"sonic-db-cli{flag} {dbname} {verb} {shlex.quote(arg)}"


# ---------------------------------------------------------------- switch
SW = "^sw"
add("ip netns list", "asic0 (id: 0)\nasic1 (id: 1)\n", host=SW)
add("docker ps --format '{{.Names}}'",
    "\n".join(["database", "pmon", "lldp0", "lldp1", "swss0", "swss1", "syncd0", "syncd1", "bgp0", "bgp1",
               "afm-agent", "afm-controller", "lldp", "database0", "database1"]) + "\n", host=SW)

fans = [["FanTray1", "N/A", "FanTray1_Fan1", "42%", "intake", "Present", "OK", "20261009 10:00:00"],
        ["FanTray1", "N/A", "FanTray1_Fan2", "41%", "intake", "Present", "OK", "20261009 10:00:00"],
        ["FanTray2", "N/A", "FanTray2_Fan1", "44%", "intake", "Present", "OK", "20261009 10:00:00"],
        ["FanTray2", "N/A", "FanTray2_Fan2", "43%", "intake", "Present", "OK", "20261009 10:00:00"],
        ["PSU 1", "N/A", "PSU 1 FAN 1", "35%", "intake", "Present", "OK", "20261009 10:00:00"],
        ["PSU 2", "N/A", "PSU 2 FAN 1", "36%", "intake", "Present", "OK", "20261009 10:00:00"]]
add("show platform fan", table(["Drawer", "LED", "FAN", "Speed", "Direction", "Presence", "Status", "Timestamp"],
                               fans, right={0, 2, 3}), name="platform_fan.txt", host=SW)
add(db(None, "STATE_DB", "keys", "FAN_INFO|*"), "\n".join(f"FAN_INFO|{f[2]}" for f in fans) + "\n", host=SW)
for f in fans:
    add(db(None, "STATE_DB", "hgetall", f"FAN_INFO|{f[2]}"),
        repr({"presence": "True", "status": "True", "speed": f[3].rstrip("%"), "drawer_name": f[0]}) + "\n", host=SW)

psus = [["PSU 1", "PWR-3000-AC", "PSU1SN0001", "A1", "12.05", "92.10", "1110.00", "OK", "green"],
        ["PSU 2", "PWR-3000-AC", "PSU2SN0002", "A1", "12.04", "88.40", "1064.00", "OK", "green"]]
add("show platform psustatus", table(["PSU", "Model", "Serial", "HW Rev", "Voltage (V)", "Current (A)", "Power (W)",
                                      "Status", "LED"], psus, right={4, 5, 6}), name="psustatus.txt", host=SW)
add(db(None, "STATE_DB", "keys", "PSU_INFO|*"), "PSU_INFO|PSU 1\nPSU_INFO|PSU 2\n", host=SW)
for p in psus:
    add(db(None, "STATE_DB", "hgetall", f"PSU_INFO|{p[0]}"), repr({
        "presence": "true", "status": "true", "model": p[1], "serial": p[2], "revision": p[3],
        "voltage": p[4], "current": p[5], "power": p[6], "temp": "38.5", "temp_threshold": "65.0",
        "voltage_min_threshold": "11.4", "voltage_max_threshold": "12.6", "power_overload": "False",
        "led_status": p[8]}) + "\n", host=SW)

add("show platform temperature", table(
    ["Sensor", "Temperature", "High TH", "Low TH", "Crit High TH", "Crit Low TH", "Warning", "Timestamp"],
    [["ASIC0", "52.0", "105.0", "N/A", "115.0", "N/A", "False", "20261009 10:00:00"],
     ["ASIC1", "54.0", "105.0", "N/A", "115.0", "N/A", "False", "20261009 10:00:00"],
     ["CPU", "41.0", "95.0", "N/A", "105.0", "N/A", "False", "20261009 10:00:00"]], right={0, 1, 2, 3, 4, 5}),
    name="temperature.txt", host=SW)
add("docker exec pmon supervisorctl status thermalctld", "thermalctld   RUNNING   pid 51, uptime 3 days\n", host=SW)
add("show platform summary", "Platform: x86_64-helios_switch-r0\nHwSKU: HELIOS-SW-2ASIC\nASIC: broadcom\n"
    "ASIC Count: 2\nSerial Number: SWSN000001\nHardware Revision: 1.0\n", host=SW)
add("show platform syseeprom", "TlvInfo Header:\n   Id String:    TlvInfo\n   Version:      1\n" + table(
    ["TLV Name", "Code", "Len", "Value"],
    [["Product Name", "0x21", "16", "HELIOS-SW-2ASIC"], ["Serial Number", "0x23", "10", "SWSN000001"],
     ["Base MAC Address", "0x24", "6", "AA:BB:CC:00:11:22"], ["Platform Name", "0x28", "28", "x86_64-helios_switch-r0"]]),
    name="syseeprom.txt", host=SW)
# QFX5252 layout (MR3): CPLDs appear only in the Description column
fw = table(["Chassis", "Module", "Component", "Version", "Description"],
           [["QFX5252-SUT-ALT", "N/A", "BIOS", "BD-01-0003", "System BIOS ROM"],
            ["", "", "BMC", "1.0-20261006162827", "Baseboard Management Controller firmware"],
            ["", "", "CB_FPGA", "0x0007", "Control Board FPGA firmware"],
            ["", "", "SWB0_FPGA", "0x0005", "Switch Board 0 FPGA firmware"],
            ["", "", "SWB1_FPGA", "0x0005", "Switch Board 1 FPGA firmware"],
            ["", "", "PMC", "0x0009", "PMC CPLD firmware"],
            ["", "", "ICB", "0x0006", "ICB CPLD firmware"]])
add("show platform firmware status", fw, name="firmware.txt", host=SW)
add("fwutil show status", fw, name="firmware.txt", host=SW)

for ns in NS:
    ports = PORTS[ns]
    st_rows = [[p, "0,1,2,3,4,5,6,7", "400G", "9100", "rs", f"etp{i}", "trunk", "up", "up", "OSFP", "N/A"]
               for i, p in enumerate(ports)]
    st_rows.append([INTERNAL[ns], "100,101", "400G", "9100", "rs", "Eth-BP", "routed", "up", "up", "N/A", "N/A"])
    add(f"show interfaces status -n {ns} -d all", table(
        ["Interface", "Lanes", "Speed", "MTU", "FEC", "Alias", "Vlan", "Oper", "Admin", "Type", "Asym PFC"],
        st_rows, right={0, 2, 3, 4}), name=f"intf_status_{ns}.txt", host=SW)
    def counters(step: int) -> str:
        rx, tx = 1_204_331 + step * 1_000, 1_198_002 + step * 1_000
        # drop columns padded to a realistic width (real MR3 counters reached 20M+), so fault injection
        # can write large values without shifting the fixed-width layout
        rows = [[p, "U", f"{rx:,}", "12.30 MB/s", "0.02%", "0", f"{3:>12}", "0", f"{tx:,}", "11.90 MB/s", "0.02%",
                 "0", f"{0:>12}", "0"] for p in ports]
        return table(["IFACE", "STATE", "RX_OK", "RX_BPS", "RX_UTIL", "RX_ERR", "RX_DRP", "RX_OVR",
                      "TX_OK", "TX_BPS", "TX_UTIL", "TX_ERR", "TX_DRP", "TX_OVR"], rows, right=set(range(14)))

    # counters only ever go up within a session (40 reads per namespace before wrapping)
    add(f"show interfaces counters -n {ns} -d all", host=SW,
        seq=[(f"intf_counters_{ns}_{i}.txt", counters(i)) for i in range(40)])
    add(rf"^for k in \$\(sonic-db-cli -n {ns} CONFIG_DB keys 'PORT\|Ethernet\*'\); do p=",
        "".join(f"{p} 400000 400000\n" for p in ports + [INTERNAL[ns]]), host=SW, regex=True)
    idx = ns.removeprefix("asic")
    add(f"systemctl restart lldp@{idx}", host=SW)
    add(f"systemctl restart swss@{idx}", host=SW)
    for ctr, procs in ((f"swss{idx}", ["orchagent", "portsyncd", "neighsyncd"]), (f"syncd{idx}", ["syncd"]),
                       (f"lldp{idx}", ["lldpd", "lldp-syncd", "lldpmgrd"])):
        add(f"docker exec {ctr} cat /etc/supervisor/critical_processes",
            "".join(f"program:{p}\n" for p in procs), host=SW)
        add(f"docker exec {ctr} supervisorctl status",
            "".join(f"{p:<32} RUNNING   pid {40 + i}, uptime 3 days, 1:02:03\n" for i, p in enumerate(procs)),
            name=f"supervisor_{ctr}.txt", host=SW)
    add(db(ns, "APPL_DB", "keys", "LLDP_ENTRY_TABLE:*"), "".join(f"LLDP_ENTRY_TABLE:{p}\n" for p in ports), host=SW)
    add(db(ns, "CONFIG_DB", "keys", "VLAN|*"), "VLAN|Vlan100\nVLAN|Vlan101\n", host=SW)
    add(db(ns, "CONFIG_DB", "keys", "VLAN_MEMBER|*"),
        "".join(f"VLAN_MEMBER|Vlan{100 + i % 2}|{p}\n" for i, p in enumerate(ports)), host=SW)

add(r"^for i in \$\(seq \$\(nproc\)\)", "", host=SW, regex=True)
add(r"^sonic-db-cli -n asic\d CONFIG_DB hget 'PORT\|Ethernet\d+' description$",
    "To ComputeTray Slot0/GPU0[0]/Port9\n", host=SW, regex=True)
# switch health (TC-SWH-037..042, added cases): formats as seen on g02-g17 QFX5252 (14 Oct 2026); the self-test
# rack has pluggable modules so the DOM path is exercised (g02-g17 itself reports 'Not present' on every port)
add("show version", "SONiC Software Version: SONiC.202505I20260915070938_x100.15\nBuild commit: 971b4d793\n", host=SW)
add(r"^docker inspect -f '\{\{\.Config\.Image\}\} \{\{\.Image\}\}' afm-(agent|controller)$",
    "docker-afm-agent:202505I20260915070938_x100.15 sha256:ecffebaf73dd\n", host=SW, regex=True)
_ctrs = ["database", "database0", "database1", "pmon", "lldp", "lldp0", "lldp1", "swss0", "swss1", "syncd0", "syncd1",
         "bgp0", "bgp1", "afm-agent", "afm-controller"]
add("docker inspect -f '{{.Name}} {{.RestartCount}} {{.State.StartedAt}}' $(docker ps -q)",
    "".join(f"/{c} 0 2026-10-13T00:02:{47 + i % 10:02d}.113015132Z\n" for i, c in enumerate(_ctrs)),
    name="docker_restarts.txt", host=SW)
add("show system-health summary", "System status summary\n\n  System status LED  green\n  Services:\n    Status: OK\n"
    "  Hardware:\n    Status: OK\n", host=SW)
for ns, ports in PORTS.items():
    add(f"show interfaces transceiver presence -n {ns}", table(["Port", "Presence"], [[p, "Present"] for p in ports]),
        name=f"xcvr_presence_{ns}.txt", host=SW)
    fec_rows = [[p, "U", f"{4695 + 10 * i:,}", "10", f"{4934 + 10 * i:,}", "1.2e-08", "nan", "N/A", "0", "0"]
                for i, p in enumerate(ports)]
    add(f"show interfaces counters fec-stats -n {ns}",
        table(["IFACE", "STATE", "FEC_CORR", "FEC_UNCORR", "FEC_SYMBOL_ERR", "FEC_PRE_BER", "FEC_POST_BER",
               "FEC_PRE_BER_MAX", "FLR(O)", "FLR(P) (Accuracy)"], fec_rows, right={0, 2, 3, 4, 5, 6, 7, 8, 9}),
        name=f"fec_{ns}.txt", host=SW)
add(r"^show interfaces transceiver eeprom --dom -n asic\d Ethernet\d+$",
    "Ethernet0: SFP EEPROM detected\n        Identifier: OSFP 8X Pluggable Transceiver\n        ChannelMonitorValues:\n"
    "                RX1Power: -1.20dBm\n                RX2Power: -1.35dBm\n                TX1Power: 0.80dBm\n"
    "                TX2Power: 0.75dBm\n        ModuleMonitorValues:\n                Temperature: 41.25C\n"
    "                Vcc: 3.29Volts\n", name="xcvr_dom.txt", host=SW, regex=True)
add("show reboot-cause", "User issued 'reboot' command [User: admin, Time: Tue Oct 13 12:01:12 AM UTC 2026]\n", host=SW)
add("show reboot-cause history", table(["Name", "Cause", "Time", "User", "Comment"],
    [["2026_10_13_00_02_35", "reboot", "Tue Oct 13 12:01:12 AM UTC 2026", "admin", "N/A"]]), host=SW)
add("ls -1 /var/core", "", host=SW)

IMG_CUR, IMG_OLD = "SONiC-OS-helios.202505.1", "SONiC-OS-helios.202411.3"
add("sonic-installer list", f"Current: {IMG_CUR}\nNext: {IMG_CUR}\nAvailable:\n{IMG_CUR}\n{IMG_OLD}\n",
    name="sonic_installer_list.txt", host=SW)
add("sonic-installer --help", """Usage: sonic-installer [OPTIONS] COMMAND [ARGS]...

Commands:
  binary-version   Get version from local binary image file
  cleanup          Remove installed images which are not current and next
  install          Install image from local binary or URL
  list             Print installed images
  remove           Uninstall SONiC image
  set-default      Choose image to boot from by default
  set-next-boot    Choose image for next reboot (one time action)
  verify-next-image  Verify the next image for reboot
""", host=SW)
add(rf"^sonic-installer set-next-boot ", "", host=SW, regex=True)
add("cat /proc/loadavg", "7.92 3.10 1.20 9/412 31337\n", host=SW)

# --------------------------------------------------------------- computes
PERF = """---------------------------------------------------------------------------------------
                    RDMA_Write BW Test
 Dual-port       : OFF\t\tDevice         : ionic_0
 Number of qps   : 1\t\tTransport type : IB
 Using SRQ      : OFF
 TX depth        : 128
 CQ Moderation   : 1
 Mtu             : 4096[B]
 Link type       : Ethernet
 GID index       : 1
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
 65536      7442117          0.00               390.12\t\t   0.744097
---------------------------------------------------------------------------------------
"""
add("rdma link show", "".join(f"link ionic_{i}/1 state ACTIVE physical_state LINK_UP netdev eth{i}\n" for i in range(4)),
    name="rdma_link.txt")
add("ib_write_bw --help 2>&1", "Usage:\n  ib_write_bw            start a server and wait for connection\n"
    "      --use_rocm=<rocm device id> Use selected ROCm device for GPUDirect RDMA testing\n"
    "      --ipv6 Use IPv6 GID. Default is IPv4\n", name="perftest_help.txt")
add(r"^ib_(write|send|read)_bw ", PERF, name="perftest.txt", regex=True)
add(r"^afmctl reset device --datapath", "datapath reset: ok\n", regex=True)
add(r"^nohup sh -c 'sleep 2; systemctl reboot'", "", regex=True)
add("true", "")
add(r"^(ras-partial-stations|pdu-ctl|ipmitool) ", "", regex=True)
add(r"^ping-gpu ", "4 packets transmitted, 4 received, 0% packet loss\n", regex=True)
# afmctl discovery (formats from the full-rack log). c2 deliberately uses acc 8-11, not 4-7: IDs must be discovered.
import json as _json
BDFS = [f"000{g + 1}:01:00.1" for g in range(4)]
SLOTS = {"c1": (0, 0), "c2": (1, 8)}            # compute -> (slot, first accelerator id)
ALL_ACCS = [base + g for _, base in SLOTS.values() for g in range(4)]


def acc_mac(acc: int, port: int) -> str:
    slot, base = next(v for v in SLOTS.values() if v[1] <= acc < v[1] + 4)
    return f"02:00:00:{slot:02x}:{acc - base:02x}:{port:02x}"


def fabric_port(acc: int, port: int) -> tuple[str, str]:
    """(namespace, switch port) cabled to accelerator `acc` port `port` - planes 0-17 on asic0, 18-35 on asic1."""
    return ("asic0" if port < 18 else "asic1"), f"Ethernet{1000 + acc * 36 + port}"
for cname, base in (("c1", 0), ("c2", 8)):
    add("afmctl show device --json 2>/dev/null", _json.dumps({"device": [
        {"bdf": b, "spec": {"accelerator_id": str(base + g), "config_phase": "ACTIVE",
                            "local_accelerators": [str(base + i) for i in range(4)],
                            "vpod_accelerators": [str(a) for a in ALL_ACCS]}} for g, b in enumerate(BDFS)]}),
        name=f"afmctl_device_{cname}.json", host=f"^{cname}$")
def port_json(cname: str, g: int) -> str:
    """afmctl show port -b <bdf> --json for one GPU: port 7 physically down, the rest LINK_UP with IFCP up and
    the IFCP peer = the switch port cabled to (acc, port) (peer_port_id is switch-global, device id 1)."""
    slot, base = SLOTS[cname]
    acc = base + g
    ports = []
    for p in range(36):
        up = p != 7
        ports.append({"id": str(p), "spec": {"admin_state": "enabled", "station_id": str(p // 2)},
                      "status": {"link_status": "LINK_UP" if up else "NO_PHY_LINK, PCS_NO_BLOCK_LOCK",
                                 "ifcp": {"port_id": str(p), "link_up": "yes" if up else "no", "in_error": "no",
                                          "ll_up": "yes" if up else "no", "peer_device_id": "1",
                                          "peer_port_id": fabric_port(acc, p)[1].removeprefix("Ethernet"),
                                          "peer_enabled": "yes", "peer_in_error": "no", "peer_mtu": "4096"}}})
    return _json.dumps({"device": [{"bdf": BDFS[g], "port": ports}]})


for cname in SLOTS:
    for g, b in enumerate(BDFS):
        add(f"afmctl show port -b {b} --json 2>/dev/null", port_json(cname, g),
            name=f"afmctl_port_{cname}_{b.split(':')[0]}.json", host=f"^{cname}$")
# accelerator destination tables (TC-HW-013): every port lists all other vPOD accelerators, MAC ...:<port>
for cname, (slot, base) in SLOTS.items():
    add("afmctl show port accelerator-addr --json 2>/dev/null", _json.dumps({"device": [
        {"bdf": BDFS[g], "port": [{"port_id": str(p), "accelerator_addr": [
            {"accelerator_id": str(a), "mac_addr": acc_mac(a, p), "ip_addr": "0.0.0.0"}
            for a in ALL_ACCS if a != base + g]} for p in range(36)]} for g in range(4)]}, indent=1),
        name=f"afmctl_accel_addr_{cname}.json", host=f"^{cname}$")
# switch FDB + cabling descriptions for those MACs
for ns in NS:
    rows, descs = [], []
    for a in ALL_ACCS:
        slot, base = next(v for v in SLOTS.values() if v[1] <= a < v[1] + 4)
        for p in range(36):
            pns, sport = fabric_port(a, p)
            if pns != ns:
                continue
            rows.append([str(len(rows) + 1), str(100 + p * 100), acc_mac(a, p), sport, "Static"])
            descs.append(f"{sport}|To ComputeTray Slot{slot}/GPU{a - base}[{a}]/Port{p}")
    # the listed fabric ports (interfaces status) are cabled to a tray outside the self-test rack (slot 9)
    descs += [f"{pt}|To ComputeTray Slot9/GPU{i}[{90 + i}]/Port{i}" for i, pt in enumerate(PORTS[ns])]
    add(f"show mac -n {ns}", table(["No.", "Vlan", "MacAddress", "Port", "Type"], rows, right={0, 1}) +
        f"Total number of entries {len(rows)}\n", name=f"fdb_{ns}.txt", host=SW)
    add(rf"^for k in \$\(sonic-db-cli -n {ns} CONFIG_DB keys 'PORT\|Ethernet\*'\); do echo ", "\n".join(descs) + "\n",
        name=f"port_desc_{ns}.txt", host=SW, regex=True)
# sideband agents: sb-node-agent per compute slot inside the switch's afm-agent container; nothing on computes
add("docker exec afm-agent ps -eo args", "/usr/bin/afm_agent.sh\n/switch-agent\n" +
    "".join(f"/sb-node-agent -slot-id {slot} -controllers 10.0.0.83\n" for slot, _ in SLOTS.values()),
    name="afm_agent_ps.txt", host=SW)
add("systemctl is-active afm-agent", "active\n", host=SW)
AGENT_HEALTH = _json.dumps({"status": "", "configured-controllers": ["10.0.0.83"], "current-controllers": ["10.0.0.83"],
                            "is-connected": True, "unhealthy-services": [], "restart-required": False,
                            "health-conditions": [], "git-version": "0.1.0-AICC-797"})
AGENT_REQS = _json.dumps([{"id": f"r{i}", "created-at": f"2026-10-13T06:46:{50 + i}Z",
                           "completed-by-agent-at": f"2026-10-13T06:46:{51 + i}Z",
                           "config-status": {"config-type": 5, "state": 1,
                                             "reason": "station-enabled-ports-already-in-sync" if i else ""},
                           "version": 1, "device-reset": False} for i in range(3)])
# sb-node-agent slot 0 -> 13000, slot 1 -> 13001; switch-agent answers on 13018
for port in (13000, 13001, 13018):
    add(f"docker exec afm-agent curl -s -m 5 localhost:{port}/api/health", AGENT_HEALTH,
        name=f"agent_health_{port}.json", host=SW)
    add(f"docker exec afm-agent curl -s -m 5 localhost:{port}/api/runtime/requests", AGENT_REQS,
        name=f"agent_requests_{port}.json", host=SW)
add("ps -eo comm,args", "COMMAND         COMMAND\nsystemd         /sbin/init\nsshd            sshd: /usr/sbin/sshd -D\n"
    "amd-smi         /opt/rocm/bin/amd-smi\n", name="compute_ps.txt", host="^c")
add(r"^timeout \d+ afmctl test ping ", """PING test 0001:01:00.1 -> accelerator 1, count 1
Summary:
  IFoE Request    : 35/35 PASS, 0/35 fail (0.00% loss)
  IFoE Response   : 35/35 PASS, 0/35 fail (0.00% loss)
  Non-IFoE        : 35/35 PASS, 0/35 fail (0.00% loss)
""", name="afmctl_ping.txt", regex=True)
# TransferBench socket multi-rank: rank 0 prints the results, workers print nothing
# (multi-rank jobs carry a HELIOS_RUN=<tag> marker so a failed job's other ranks can be killed)
rules.append({"cmd": r"^(HELIOS_RUN=\w+ )?TB_NUM_RANKS=\d+ TB_RANK=0 ", "rc": 0, "file": "transferbench.txt"})
add(r"^(HELIOS_RUN=\w+ )?TB_NUM_RANKS=\d+ TB_RANK=[1-9]", "", regex=True)
add(r"RcclSmokeTest", """RcclSmokeTest v0.36 (96e7ac2)
Running single-node smoke test
GPUs      : 4

  Legend: P=Pass  F=Fail  S=Skip(OOM)  -=Not run

  Collective                 |   1 KB |   4 MB |   1 GB |
  --------------------------+--------+--------+--------+
  AllReduce (float32)        |    P   |    P   |    P   |
  AllGather                  |    P   |    P   |    P   |
  ReduceScatter (float32)    |    P   |    P   |    P   |
  AllToAll                   |    P   |    P   |    P   |
  Broadcast                  |    P   |    P   |    P   |
  AllReduce (bfloat16)       |    P   |    P   |    P   |
  ReduceScatter (bfloat16)   |    P   |    P   |    P   |

  RESULT: ALL TESTS PASSED!
  Test Time: 34.8 s
""", name="rccl_smoke.txt", regex=True)
add(r"^TransferBench ", """TransferBench v1.50
Test 1: a2a  1073741824 bytes
...
Summary: 12 transfers
Aggregate bandwidth (GPU Timed):   612.48 GB/s
""", name="transferbench.txt", regex=True)
rccl_rows = []
size = 1024
while size <= 1 << 30:
    t = 20 + size / 4e5
    rccl_rows.append(f"{size:>12} {size // 4:>14}     float     sum      -1  {t:8.2f}  {size / t / 1e3:6.2f}  {size / t / 1e3 * 1.5:6.2f}      0  "
                     f"{t:8.2f}  {size / t / 1e3:6.2f}  {size / t / 1e3 * 1.5:6.2f}      0")
    size *= 2
add(r"(^|/)\w+_perf |mpirun ", """# nThread 1 nGpus 4 minBytes 1024 maxBytes 1073741824 step: 2(factor) warmup iters: 5 iters: 20
#
#                                                              out-of-place                       in-place
#       size         count      type   redop    root     time   algbw   busbw #wrong     time   algbw   busbw #wrong
#        (B)    (elements)                               (us)  (GB/s)  (GB/s)            (us)  (GB/s)  (GB/s)
""" + "\n".join(rccl_rows) + """
# Out of bounds values : 0 OK
# Avg bus bandwidth    : 152.37
#
""", name="rccl.txt", regex=True)

# ------------------------------------------------- platform (all nodes)
add("timedatectl show -p NTPSynchronized --value", "yes\n")
add("date +%s.%N", "@@NOW@@\n")
add("cat /etc/resolv.conf", "search lab.example\nnameserver 10.0.0.53\n")
add(r"^getent hosts ", "10.0.0.10       helios-afm.lab.example\n", regex=True)
add("cat /etc/logrotate.conf /etc/logrotate.d/* 2>/dev/null",
    "weekly\nrotate 4\n/var/log/syslog\n{\n    size 100M\n    rotate 5\n    compress\n}\n", host=SW)
add("systemctl is-active logrotate.timer", "active\n", host=SW)
add("ls /etc/cron.d /etc/cron.daily /etc/cron.hourly 2>/dev/null", "/etc/cron.d:\n", host=SW)
add("df --output=pcent /var/log | tail -1", " 23%\n", host=SW)
add(r"^find /opt/amd/afm/controller/logs ", "1760000000.5 20480 /opt/amd/afm/controller/logs/pen-afm-core-pod-1/toposvc.log\n"
    "1759990000.1 1048576 /opt/amd/afm/controller/logs/pen-afm-core-pod-1/toposvc-2026-10-12T19-11-22.142.log.gz\n1760003600\n", name="topo_find.txt", host=SW, regex=True)
add("ip -o link show", """1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000\\    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default qlen 1000\\    link/ether aa:bb:cc:00:11:22 brd ff:ff:ff:ff:ff:ff
3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default qlen 1000\\    link/ether aa:bb:cc:00:11:23 brd ff:ff:ff:ff:ff:ff
4: Ethernet0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9100 qdisc pfifo_fast state UP mode DEFAULT group default qlen 1000\\    link/ether aa:bb:cc:00:11:30 brd ff:ff:ff:ff:ff:ff
""", name="ip_link.txt", host=SW)

# ------------------------------------------------- metrics exporter (computes)
def metrics(host: str) -> str:
    lines = ["# HELP gpu_power_usage GPU power in watts", "# TYPE gpu_power_usage gauge"]
    def g(name, gpu, val):
        lines.append(f'{name}{{gpu_id="{gpu}",hostname="{host}",card_model="AMD Instinct MI455X",serial_number="SN{host}{gpu}"}} {val}')
    for gpu in range(4):
        g("gpu_power_usage", gpu, 812.5 + gpu)
        g("gpu_edge_temperature", gpu, 41.0 + gpu)
        g("gpu_junction_temperature", gpu, 55.0 + gpu)
        g("gpu_gfx_activity", gpu, 12)
        g("gpu_total_vram", gpu, 442368)
        g("gpu_used_vram", gpu, 1024)
        g("gpu_free_vram", gpu, 441344)
        g("gpu_ecc_correct_total", gpu, 0)
        g("gpu_ecc_uncorrect_total", gpu, 0)
        g("gpu_ecc_correct_umc", gpu, 0)
        g("gpu_ecc_uncorrect_umc", gpu, 0)
        g("pcie_bandwidth", gpu, 31.5)
        g("pcie_speed", gpu, 32)
        g("pcie_link_width", gpu, 16)
        g("pcie_replay_count", gpu, 0)
        g("xgmi_neighbor_0_tx_throughput", gpu, 0)
        g("xgmi_link_rx_beats", gpu, 0)
    for port in range(4):
        lab = f'{{hostname="{host}",port="eth{port}",lif="lif{port}"}}'
        lines += [f"nic_port_stats_frames_rx_ok{lab} 120034", f"nic_port_stats_octets_rx_bytes{lab} 9932001",
                  f"nic_lif_stats_rx_unicast_packets{lab} 99871",
                  f"rdma_req_rx_pkts{lab} 5021", f"rdma_resp_tx_pkts{lab} 5021", f"rdma_cnp_sent{lab} 0",
                  f"rdma_ecn_marked_packets{lab} 0", f"rdma_qp_count{lab} 8"]
    return ("HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
            "Date: Mon, 12 Oct 2026 10:00:00 GMT\r\n\r\n" + "\n".join(lines) + "\n")

for cname in ("c1", "c2"):
    host = f"^{cname}$"
    add(r"^curl -s -m 15 -D - ", metrics(cname), name=f"metrics_{cname}.txt", host=host, regex=True)
    add("hostname", f"{cname}.lab.example\n", host=host)
    add("hostname -s", f"{cname}\n", host=host)
add("docker ps --filter name=^device-metrics-exporter$ --format '{{.Names}}'", "device-metrics-exporter\n")
add("ss -ltnH 'sport = :5000'", "LISTEN 0      4096               *:5000            *:*\n")
add("docker restart device-metrics-exporter", "device-metrics-exporter\n")
add(r"^docker logs --since \d+s device-metrics-exporter", 'time="2026-10-12T10:00:01Z" level=info msg="serving metrics on :5000"\n',
    name="exporter_logs.txt", regex=True)

# ------------------------------------------------- IFoE driver / GPU health (computes)
add("amd-smi list", "".join(f"GPU: {i}\n    BDF: 0000:{0x11 + i:02x}:00.0\n    UUID: 1234-{i}\n" for i in range(4)),
    name="amd_smi_list.txt")
# datapath pre-flight (helios/preflight.py): GPU access/visibility, other GPU users, tools installed
add("amd-smi list --json", _json.dumps([{"gpu": i, "bdf": f"0000:{0x11 + i:02x}:00.0"} for i in range(4)]),
    name="amd_smi_list.json")
add("amd-smi process --json", _json.dumps([{"gpu": i, "process_list": [{"process_info": "No running processes detected"}]}
                                          for i in range(4)]), name="amd_smi_process.json")
add(r"^command -v \S+ >/dev/null 2>&1 \|\| test -x \S+$", "", regex=True)
add(r"^pkill -TERM -f 'HELIOS_RU\[N\]=\w+'$", "", regex=True)
add(r"^modprobe (-r )?ifoe$", "", regex=True)
add("lsmod | grep -q '^ifoe '", "", rcs=[1, 0])   # unloaded after unload, loaded after load

# pre-flight probes first: broad traffic rules (RcclSmokeTest, *_perf) would otherwise match "command -v <tool>"
_first = [r for r in rules if r["cmd"].startswith(("^command\\ -v", "^command -v", "^pkill", "^amd\\-smi\\ list\\ \\-\\-json",
                                                   "^amd\\-smi\\ process"))]
rules[:] = _first + [r for r in rules if r not in _first]

OUT.mkdir(exist_ok=True)
for name, text in files.items():
    (OUT / name).write_text(text)
(OUT / "responses.yaml").write_text("# generated by selftest/make_replay.py — do not edit by hand\n" +
                                    yaml.safe_dump(rules, sort_keys=False, width=200))
print(f"wrote {len(rules)} rules, {len(files)} files to {OUT}")
