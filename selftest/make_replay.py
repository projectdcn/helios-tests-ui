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


def add(cmd: str, text: str = "", *, name: str | None = None, rc: int = 0, host: str | None = None, regex: bool = False):
    rule = {"cmd": cmd if regex else f"^{re.escape(cmd)}$", "rc": rc}
    if host:
        rule["host"] = host
    if name:
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
    "\n".join(["database", "pmon", "lldp0", "lldp1", "swss0", "swss1", "syncd0", "syncd1", "bgp0", "bgp1"]) + "\n", host=SW)

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
fw = table(["Chassis", "Module", "Component", "Version", "Description"],
           [["HELIOS-SW", "N/A", "BIOS", "5.29.0.1", "BIOS - Basic Input/Output System"],
            ["", "", "CPLD1", "0x0F", "Main board CPLD"],
            ["", "", "CPLD2", "0x0A", "Fan board CPLD"],
            ["", "", "FPGA", "1.4.2", "System FPGA"]])
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
    ctr_rows = [[p, "U", "1,204,331", "12.30 MB/s", "0.02%", "0", "3", "0", "1,198,002", "11.90 MB/s", "0.02%", "0", "0", "0"]
                for p in ports]
    add(f"show interfaces counters -n {ns} -d all", table(
        ["IFACE", "STATE", "RX_OK", "RX_BPS", "RX_UTIL", "RX_ERR", "RX_DRP", "RX_OVR",
         "TX_OK", "TX_BPS", "TX_UTIL", "TX_ERR", "TX_DRP", "TX_OVR"], ctr_rows, right=set(range(14))),
        name=f"intf_counters_{ns}.txt", host=SW)
    add(db(ns, "APPL_DB", "keys", "LLDP_ENTRY_TABLE:*"), "".join(f"LLDP_ENTRY_TABLE:{p}\n" for p in ports), host=SW)
    add(db(ns, "CONFIG_DB", "keys", "VLAN|*"), "VLAN|Vlan100\nVLAN|Vlan101\n", host=SW)
    add(db(ns, "CONFIG_DB", "keys", "VLAN_MEMBER|*"),
        "".join(f"VLAN_MEMBER|Vlan{100 + i % 2}|{p}\n" for i, p in enumerate(ports)), host=SW)

add(r"^for i in \$\(seq \$\(nproc\)\)", "", host=SW, regex=True)
add("cat /proc/loadavg", "7.92 3.10 1.20 9/412 31337\n", host=SW)

# --------------------------------------------------------------- computes
add(r"^ping-gpu ", "4 packets transmitted, 4 received, 0% packet loss\n", regex=True)
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

OUT.mkdir(exist_ok=True)
for name, text in files.items():
    (OUT / name).write_text(text)
(OUT / "responses.yaml").write_text("# generated by selftest/make_replay.py — do not edit by hand\n" +
                                    yaml.safe_dump(rules, sort_keys=False, width=200))
print(f"wrote {len(rules)} rules, {len(files)} files to {OUT}")
