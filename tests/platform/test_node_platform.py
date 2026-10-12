"""Node-level platform checks that need only SSH: time, DNS, logs, OOB naming.

TC-CTL-035, TC-CTL-059, TC-BUG-022, TC-E2E-118, TC-BUG-021 (disruptive: reboots one switch).
"""
from __future__ import annotations

import re
import shlex
import time
from urllib.parse import urlparse

import pytest

from helios.rack import fan_out
from helios.wait import wait_for_ssh

pytestmark = pytest.mark.l3

REBOOT = "nohup sh -c 'sleep 2; systemctl reboot' >/dev/null 2>&1 &"


def _controllers(rack):
    names = rack.inv.afm.get("controller_nodes") or list(rack.switches)
    unknown = sorted(set(names) - set(rack.switches))
    if unknown:
        pytest.fail(f"afm.controller_nodes {unknown} are not switches in the inventory", pytrace=False)
    return [rack.switches[n] for n in names]


def _all_nodes(rack):
    return [*rack.switches.values(), *rack.computes.values()]


@pytest.mark.tc("TC-CTL-035")
def test_ntp_synchronized_on_all_nodes(rack, inventory, checks, record_property):
    """timedatectl reports NTPSynchronized=yes on every switch and compute, and every
    clock is within ``platform.max_clock_skew_s`` of the test runner."""
    max_skew = float(inventory.th("platform.max_clock_skew_s", 2))

    def probe(n):
        sync = n.run("timedatectl show -p NTPSynchronized --value").stdout.strip()
        t0 = time.time()
        remote = float(n.sh("date +%s.%N").strip())
        return sync, remote - (t0 + time.time()) / 2

    skews = {}
    for n, res, exc in fan_out(probe, _all_nodes(rack)):
        if exc:
            checks.fail(f"{n.name}: {exc}")
            continue
        sync, skew = res
        skews[n.name] = round(skew, 3)
        checks.check(sync == "yes", f"{n.name}: NTPSynchronized={sync or '?'}")
        checks.check(abs(skew) <= max_skew, f"{n.name}: clock off by {skew:+.2f}s vs runner (> {max_skew}s)")
    record_property("clock_skew_s", skews)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-059")
def test_dns_resolution_from_controller_nodes(rack, inventory, checks):
    """Each AFM controller node has a nameserver and resolves ``platform.dns_names``
    (default: the AFM URL's hostname)."""
    names = list(inventory.th("platform.dns_names") or [])
    afm_host = urlparse(inventory.afm.get("url", "")).hostname
    if not names and afm_host and not re.fullmatch(r"[\d.:]+", afm_host):
        names = [afm_host]
    if not names:
        pytest.skip("no names to resolve: set thresholds.platform.dns_names")

    def probe(n):
        ns = [ln.split()[1] for ln in n.sh("cat /etc/resolv.conf").splitlines()
              if ln.startswith("nameserver") and len(ln.split()) > 1]
        failed = [h for h in names if not n.run(f"getent hosts {shlex.quote(h)}", timeout=20).ok]
        return ns, failed

    for n, res, exc in fan_out(probe, _controllers(rack)):
        if exc:
            checks.fail(f"{n.name}: {exc}")
            continue
        ns, failed = res
        checks.check(ns, f"{n.name}: no nameserver in /etc/resolv.conf")
        checks.check(not failed, f"{n.name}: cannot resolve {failed}")
    checks.assert_all()


@pytest.mark.tc("TC-BUG-022")
def test_switch_log_rotation(switch, inventory, checks):
    """logrotate covers syslog, is scheduled (systemd timer or cron), and /var/log isn't filling up."""
    conf = switch.run("cat /etc/logrotate.conf /etc/logrotate.d/* 2>/dev/null", sudo=True).stdout
    checks.check(re.search(r"/var/log/syslog", conf), f"{switch.name}: no logrotate rule for /var/log/syslog")
    timer = switch.run("systemctl is-active logrotate.timer").stdout.strip()
    cron = switch.run("ls /etc/cron.d /etc/cron.daily /etc/cron.hourly 2>/dev/null").stdout
    checks.check(timer == "active" or "logrotate" in cron, f"{switch.name}: logrotate not scheduled (timer={timer!r}, no cron entry)")
    pct = switch.sh("df --output=pcent /var/log | tail -1").strip().rstrip("%")
    limit = float(inventory.th("platform.var_log_max_used_pct", 80))
    checks.check(pct.isdigit() and float(pct) < limit, f"{switch.name}: /var/log {pct}% used (>= {limit}%)")
    checks.assert_all()


@pytest.mark.tc("TC-E2E-118")
def test_topology_logs_present_and_fresh(rack, inventory, checks):
    """Topology-service logs exist under ``platform.topo_log_dir`` (default /var/log/pensando)
    on the controller nodes, and the newest was written within ``platform.topo_log_max_age_h``."""
    d = inventory.th("platform.topo_log_dir", "/var/log/pensando")
    pattern = inventory.th("platform.topo_log_glob", "*topo*")
    max_age = float(inventory.th("platform.topo_log_max_age_h", 24)) * 3600

    def probe(n):
        out = n.run(f"find {shlex.quote(d)} -maxdepth 2 -type f -name {shlex.quote(pattern)} -printf '%T@ %s %p\\n'; date +%s",
                    sudo=True).stdout.split("\n")
        now = float(out[-2] if out[-1] == "" else out[-1])
        files = [ln.split(" ", 2) for ln in out if ln.count(" ") >= 2]
        return now, [(float(t), int(sz), p) for t, sz, p in files]

    for n, res, exc in fan_out(probe, _controllers(rack)):
        if exc:
            checks.fail(f"{n.name}: {exc}")
            continue
        now, files = res
        if not checks.check(files, f"{n.name}: no {pattern} files under {d}"):
            continue
        newest = max(files)
        checks.check(now - newest[0] <= max_age, f"{n.name}: newest topo log {newest[2]} is "
                                                 f"{(now - newest[0]) / 3600:.1f}h old")
        checks.check(any(sz > 0 for _, sz, _ in files), f"{n.name}: all topo logs are empty")
    checks.assert_all()


def _oob_links(switch, iface_re: re.Pattern) -> dict[str, str]:
    """{interface: MAC} for out-of-band (management) interfaces."""
    out = {}
    for line in switch.sh("ip -o link show").splitlines():
        m = re.match(r"^\d+:\s+([^:@]+)[@:].*link/ether\s+([0-9a-f:]{17})", line)
        if m and iface_re.match(m.group(1)):
            out[m.group(1)] = m.group(2)
    return out


@pytest.mark.disruptive
@pytest.mark.tc("TC-BUG-021")
def test_oob_interface_names_stable_across_reboot(rack, inventory, checks):
    """Reboot one switch (``platform.reboot_switch``, default the first) and require the same
    OOB interface names bound to the same MACs afterwards."""
    name = inventory.th("platform.reboot_switch") or next(iter(rack.switches), None)
    if name not in rack.switches:
        pytest.skip(f"no switch {name!r} to reboot")
    sw = rack.switches[name]
    iface_re = re.compile(inventory.th("platform.oob_iface_re", r"^(eth\d+|mgmt\d*|Management\d+)$"))
    before = _oob_links(sw, iface_re)
    assert before, f"{name}: no OOB interfaces match /{iface_re.pattern}/"
    assert sw.run(REBOOT, sudo=True, timeout=30).ok, f"{name}: reboot command failed"
    timeout = float(inventory.th("platform.boot_timeout_s", 900))
    assert wait_for_ssh(sw, timeout, interval=float(inventory.th("platform.poll_interval_s", 10)),
                        down_first=float(inventory.th("platform.down_wait_s", 60))), \
        f"{name}: not back on SSH within {timeout:.0f}s"
    after = _oob_links(sw, iface_re)
    checks.check(after == before, f"{name}: OOB interfaces changed across reboot: before={before} after={after}")
    checks.assert_all()
