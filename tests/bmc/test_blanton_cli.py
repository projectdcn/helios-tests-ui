"""TC-BLT-7.1.x, 7.2.6-17 / -19 - the Blanton switch BMC from its SONiC host, READ-ONLY (Juniper TPI-151974 v1.2,
section 7.1 bmc-manager.sh and the host-side checks of 7.2.6). Added 20 Oct 2026.

Runs on each switch tray over SSH (sudo -n, as the other switch cases). Only commands that read: help, version,
status, health, flash-info, journal (stopped after a few seconds), dmesg tail / errors / grep, ssh-access status,
config-list, docker ps, STATE_DB. NOT here: check (configures the USB VRF), connect / setup / run, set-ip,
ssh-access allow / block, backup / restore, reboot, switch (boot flash), update, console - they change the BMC or
the host. A switch without bmc-manager.sh skips.
"""
from __future__ import annotations

import re

import pytest

from helios import bmc as B
from helios.rack import fan_out

TOOL = "bmc-manager.sh"


@pytest.fixture
def switch_hosts(rack):
    sws = list(rack.switches.values())
    if not sws:
        pytest.skip("no switch in the inventory")
    return sws


def per_switch(switch_hosts, checks, fn, need_tool=True):
    """fn(sw, run, fail) on every switch tray in parallel; run(cmd) = sudo -n on that switch."""
    have = {}

    def one(sw):
        def run(cmd, timeout=60):
            return sw.run(cmd, sudo=True, timeout=timeout)
        if need_tool:
            r = run(f"command -v {TOOL} || ls /usr/local/bin/{TOOL} /usr/bin/{TOOL}", timeout=20)
            have[sw.name] = r.ok and bool(r.stdout.strip())
            if not have[sw.name]:
                return None
        out = []
        fn(sw, run, lambda m: out.append(f"{sw.name}: {m}"))
        return out
    results = {}
    for sw, r, exc in fan_out(one, switch_hosts):
        if exc is not None:
            checks.fail(f"{sw.name}: {exc}")
        elif r:
            for m in r:
                checks.fail(m)
        results[sw.name] = r
    if need_tool and not any(have.values()):
        pytest.skip(f"{TOOL} is not on any switch tray")
    checks.assert_all()
    return results


def text(r) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.1-1")
def test_usb_presence(switch_hosts, checks):
    """CDC Data USB device, usb0 with 192.168.200.1/24 on the host, the BMC at 192.168.200.200 answering ping
    (bmc-manager.sh check itself is not run: it configures the VRF)."""
    def f(sw, run, fail):
        if not run("lsusb -t | grep -i 'CDC Data'").stdout.strip():
            fail("no CDC Data USB device (lsusb -t)")
        if not run("ip link show usb0").ok:
            return fail("no usb0 interface")
        if "192.168.200.1/24" not in run("ip addr show usb0").stdout:
            fail("usb0 has no 192.168.200.1/24")
        p = run("ping -c 3 -W 2 192.168.200.200 || ip vrf exec $(ip -br link show type vrf | awk 'NR==1{print $1}') "
                "ping -c 3 -W 2 192.168.200.200", timeout=40)
        if " 0% packet loss" not in p.stdout and "0.0% packet loss" not in p.stdout:
            fail("the BMC at 192.168.200.200 does not answer ping over usb0")
    per_switch(switch_hosts, checks, f, need_tool=False)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.1-2")
def test_help_menu(switch_hosts, checks):
    def f(sw, run, fail):
        a, b = text(run(f"{TOOL} --help")), text(run(f"{TOOL} -h"))
        if a.strip() != b.strip():
            fail("--help and -h print different text")
        for c in ("check", "connect", "setup", "run", "version", "status", "health", "update", "reboot", "flash-info"):
            if not re.search(rf"^\s*{re.escape(c)}\b", a, re.M):
                fail(f"help lists no command {c!r}")
        for o in ("--ip", "--force", "--primary", "--reboot", "--persistent"):
            if o not in a:
                fail(f"help lists no option {o}")
    per_switch(switch_hosts, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.1-4")
def test_version(switch_hosts, checks, switch_bmcs):
    """Version output with the boot flash; the version is the one Redfish reports (when the BMC is reachable)."""
    rf_version = {}
    for b in switch_bmcs["bmcs"]:
        try:
            rf_version[b["name"]] = B.discover(b["rf"]) and b["rf"].get(B.discover(b["rf"])["manager"]).get("FirmwareVersion")
        except B.BmcError:
            pass

    def f(sw, run, fail):
        out = text(run(f"{TOOL} version"))
        if not out.strip():
            return fail("version printed nothing")
        if not re.search(r"PRIMARY|ALTERNATE", out):
            fail("version shows no boot flash (PRIMARY / ALTERNATE)")
        v = rf_version.get(sw.name)
        if v and v not in out:
            fail(f"Redfish FirmwareVersion {v!r} not in the CLI version output")
    per_switch(switch_hosts, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.1-5")
def test_status(switch_hosts, checks):
    def f(sw, run, fail):
        out = text(run(f"{TOOL} status", timeout=90))
        for k in ("Hostname", "Uptime", "Boot Flash", "Memory", "Disk", "eth0", "usb0", "Failed Services"):
            if k.lower() not in out.lower():
                fail(f"status shows no {k}")
    per_switch(switch_hosts, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.1-6")
def test_health(switch_hosts, checks):
    """Each metric reported; the overall health is the worst one."""
    def f(sw, run, fail):
        out = text(run(f"{TOOL} health", timeout=90))
        for svc in ("bmcweb", "dbus-broker", "systemd-networkd", "dropbear.socket"):
            if svc not in out:
                fail(f"health reports no {svc}")
        for k in ("Memory", "Root FS", "Journal"):
            if k.lower() not in out.lower():
                fail(f"health reports no {k}")
        levels = re.findall(r"\[(OK|WARNING|CRITICAL)\]", out)
        overall = re.search(r"Overall[^\n]*?(OK|WARNING|CRITICAL|HEALTHY|DEGRADED)", out, re.I)
        worst = "CRITICAL" if "CRITICAL" in levels else "WARNING" if "WARNING" in levels else "OK"
        if not overall:
            fail("health prints no overall result")
        elif worst != "OK" and worst.lower() not in overall.group(0).lower():
            fail(f"overall {overall.group(0).strip()!r} does not reflect the worst metric ({worst})")
        for line in re.findall(r"\[CRITICAL\][^\n]*", out):
            fail(f"BMC health: {line.strip()}")
    per_switch(switch_hosts, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.1-9")
def test_default_boot_flash(switch_hosts, checks):
    def f(sw, run, fail):
        out = text(run(f"{TOOL} flash-info"))
        m = re.search(r"Boot Source:\s*([^\n]+)", out)
        if not m:
            return fail("flash-info shows no Boot Source")
        if "PRIMARY" not in m.group(1) and "CE0" not in m.group(1):
            fail(f"boot source {m.group(1).strip()!r}, the plan expects PRIMARY (CE0)")
    per_switch(switch_hosts, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.1-10")
def test_flash_partitions(switch_hosts, checks):
    def f(sw, run, fail):
        out = text(run(f"{TOOL} flash-info"))
        if not re.search(r"CE[01]", out):
            fail("flash-info shows no boot source CE0 / CE1")
        if not re.search(r"WDT2", out):
            fail("flash-info shows no WDT2 register")
        for n in range(12):
            if f"mtd{n}" not in out:
                fail(f"flash-info lists no mtd{n} (mtd0-5 PRIMARY, mtd6-11 ALTERNATE)")
                break
    per_switch(switch_hosts, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.2-1")
def test_journal_stream_stops_cleanly(switch_hosts, checks):
    """journal streams; interrupted after 8 s it stops cleanly and leaves no SSH process behind."""
    def f(sw, run, fail):
        out = text(run(f"timeout -s INT 8 {TOOL} journal", timeout=40))
        if "stream" not in out.lower():
            fail("journal printed no streaming header")
        if "Streaming stopped" not in out:
            fail("no '[INFO] Streaming stopped.' after Ctrl+C")
        left = run("pgrep -af 'ssh.*192.168.200.200.*journalctl' || true").stdout.strip()
        if left:
            fail(f"SSH process left behind: {left[:120]}")
    per_switch(switch_hosts, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.2-2")
def test_dmesg_modes(switch_hosts, checks):
    """tail (last 50 lines), errors, grep filter (case-insensitive); follow mode is not run."""
    def f(sw, run, fail):
        t = run(f"{TOOL} dmesg tail")
        lines = [ln for ln in text(t).splitlines() if re.match(r"^\[\s*\d+\.\d+\]", ln)]
        if not t.ok or not lines:
            fail("dmesg tail printed no kernel lines")
        elif len(lines) > 50:
            fail(f"dmesg tail printed {len(lines)} kernel lines (expected the last 50)")
        if not run(f"{TOOL} dmesg errors").ok:
            fail("dmesg errors failed")
        g = [ln for ln in text(run(f"{TOOL} dmesg usb")).splitlines() if re.match(r"^\[\s*\d+\.\d+\]", ln)]
        bad = [ln for ln in g if "usb" not in ln.lower()]
        if bad:
            fail(f"dmesg usb shows lines without 'usb': {bad[0][:100]}")
    per_switch(switch_hosts, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.3-3")
def test_ssh_access_status(switch_hosts, checks):
    """dropbear.socket active; the listeners match the mode: ALLOW = *:22 / 0.0.0.0:22, BLOCK = 192.168.200.200:22."""
    def f(sw, run, fail):
        out = text(run(f"{TOOL} ssh-access status"))
        if "active" not in out.lower() or "dropbear" not in out.lower():
            fail("dropbear.socket not reported active")
        mode = re.search(r"SSH Access Mode:\s*(\w+)", out)
        if not mode:
            return fail("no SSH Access Mode line")
        all_if = bool(re.search(r"(0\.0\.0\.0|\*|\[::\]):22\b", out))
        usb_only = "192.168.200.200:22" in out and not all_if
        m = mode.group(1).upper()
        if m.startswith("ALLOW") and not all_if:
            fail("mode ALLOW but SSH does not listen on all interfaces")
        if m.startswith("BLOCK") and not usb_only:
            fail("mode BLOCKED but SSH is not limited to 192.168.200.200:22")
    per_switch(switch_hosts, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.1.4-1")
def test_config_list(switch_hosts, checks):
    """The 11 BACKUP_FILES paths, each present / not found; the core account files present."""
    def f(sw, run, fail):
        out = text(run(f"{TOOL} config-list"))
        rows = dict(re.findall(r"^\s*(/[^\s:]+):\s*(present|not found)\s*$", out, re.M))
        if len(rows) != 11:
            fail(f"config-list shows {len(rows)} paths, the plan expects 11")
        for p in ("/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow"):
            if rows.get(p) != "present":
                fail(f"{p}: {rows.get(p, 'not listed')}")
        if re.search(r"\bunknown\b", out, re.I):
            fail("config-list shows an 'unknown' result")
    per_switch(switch_hosts, checks, f)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.6-17")
def test_redfish_container_matches_cooling(switch_hosts, checks):
    """The redfish container runs exactly when the platform is liquid cooled (platform_env.conf liquid_cooled)."""
    def f(sw, run, fail):
        env = run("grep -i liquid_cooled /usr/share/sonic/platform/platform_env.conf || true").stdout
        liquid = bool(re.search(r"liquid_cooled\s*=\s*(1|true|yes)", env, re.I))
        running = bool(run("docker ps --format '{{.Names}}' | grep -i redfish || true").stdout.strip())
        if liquid != running:
            fail(f"redfish container {'running' if running else 'not running'} on a "
                 f"{'liquid' if liquid else 'air'}-cooled platform (platform_env.conf: {env.strip() or 'no liquid_cooled'})")
    per_switch(switch_hosts, checks, f, need_tool=False)


@pytest.mark.l1
@pytest.mark.tc("TC-BLT-7.2.6-19")
def test_rack_manager_reachable(switch_hosts, checks):
    """STATE_DB RACK_MANAGER_STATE|rack-manager reachability = REACHABLE (a rack without a rack manager skips)."""
    seen = {}

    def f(sw, run, fail):
        out = run("redis-cli -n 6 HGETALL 'RACK_MANAGER_STATE|rack-manager'").stdout
        vals = dict(zip(out.split()[::2], out.split()[1::2]))
        seen[sw.name] = bool(vals)
        if vals and vals.get("reachability") != "REACHABLE":
            fail(f"rack manager reachability {vals.get('reachability')!r} (STATE_DB RACK_MANAGER_STATE)")
    per_switch(switch_hosts, checks, f, need_tool=False)
    if not any(seen.values()):
        pytest.skip("no RACK_MANAGER_STATE|rack-manager in STATE_DB on any switch (no rack manager on this rack)")
