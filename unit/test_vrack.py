"""selftest/vrack.py: the standalone virtual rack - command unwrapping, the small shell, and a real SSH session."""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import paramiko
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "selftest"))
import vrack  # noqa: E402

from helios.transport import CmdResult  # noqa: E402


def test_unwrap_strips_only_the_transport_wrappers():
    q = "timeout -k 5 120 bash -c 'sudo -n bash -c '\"'\"'env AFMCTL_FORCE_UPDATE=1 afmctl update port -b x'\"'\"''"
    assert vrack.unwrap(q) == ("env AFMCTL_FORCE_UPDATE=1 afmctl update port -b x", True, {})   # a test's own env stays
    assert vrack.unwrap("sudo -n env A=1 bash -c 'show version'") == ("show version", True, {"A": "1"})
    assert vrack.unwrap("sudo show interfaces status") == ("show interfaces status", True, {})
    assert vrack.unwrap("show version") == ("show version", False, {})
    # helios-debug wraps its own scripts: sw.sh("bash -c " + quote(script), sudo=True)
    assert vrack.unwrap("sudo -n bash -c 'bash -c '\"'\"'for k in x; do echo $k; done'\"'\"''") == \
        ("for k in x; do echo $k; done", True, {})


def test_split_shell_respects_quotes():
    assert vrack.split_shell("a 'x|y' | grep -c b; c && d || e") == [
        ("", "a 'x|y'"), ("|", "grep -c b"), (";", "c"), ("&&", "d"), ("||", "e")]


@pytest.mark.parametrize("argv,data,want", [
    (["grep", "-c", "up"], "up\ndown\nup\n", (0, "2\n")),
    (["grep", "-v", "-i", "UP"], "up\ndown\n", (0, "down\n")),
    (["grep", "-E", "a|b"], "a\nc\nb\n", (0, "a\nb\n")),
    (["head", "-n", "2"], "1\n2\n3\n", (0, "1\n2\n")),
    (["tail", "-1"], "1\n2\n3\n", (0, "3\n")),
    (["wc", "-l"], "1\n2\n", (0, "2\n")),
    (["sort", "-n", "-r"], "2\n10\n1\n", (0, "10\n2\n1\n")),
    (["uniq", "-c"], "a\na\nb\n", (0, "      2 a\n      1 b\n")),
    (["cut", "-d", ",", "-f", "2"], "a,b,c\n", (0, "b\n")),
    (["awk", "{print $2, $NF}"], "a b c\n", (0, "b c\n")),
    (["sed", "s/o/0/g"], "foo\n", (0, "f00\n")),
])
def test_filters(argv, data, want):
    assert vrack._filter(argv, data) == want


def test_unknown_pipe_command_is_refused():
    class T:
        def run(self, c, **k):
            return CmdResult("x", c, 0, "a\nb\n", "") if c == "list" else CmdResult("x", c, 127, "", "replay: no rule matches")
    n = vrack.Node("x", "switch", 0, T())
    assert n.run("list | grep b && echo ok") == (0, "b\nok\n", "")
    rc, _, err = n.run("list | perl -ne print")
    assert rc == 127 and "not supported in a pipe" in err


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_ssh_session_against_a_simulated_switch(tmp_path):
    """A real SSH exec (paramiko client, key login) on a simulated MR3-size switch: the command goes through the same
    fake transport the self-tests use; a pipeline is run by the small shell; an unknown key is refused."""
    from fake_afm import State
    from fake_rack import FakeSwitchTransport
    from helios.inventory import Host
    from helios.transport import ReplayTransport

    st = State(set(), {"switches": 1, "computes": 2})
    h = Host(name="sw1", host=st.sim.switch_ips[0], role="switch")
    node = vrack.Node("sw1", "switch", _free_port(),
                      FakeSwitchTransport(h, st, 0, ReplayTransport(h, str(ROOT / "selftest" / "replay"))))
    client_key = paramiko.Ed25519Key.from_private_key_file(str(_keygen(tmp_path / "client")))
    keys = {(client_key.get_name(), client_key.asbytes())}
    threading.Thread(target=vrack._listen, args=(node, "127.0.0.1", vrack._host_key(tmp_path), keys), daemon=True).start()
    time.sleep(0.3)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("127.0.0.1", port=node.port, username="admin", pkey=client_key, look_for_keys=False, allow_agent=False)
    try:
        _, o, _ = c.exec_command("sudo show interfaces status -n asic0 -d all")
        out = o.read().decode()
        assert o.channel.recv_exit_status() == 0 and "Ethernet0" in out and "trunk" in out
        _, o, _ = c.exec_command("show interfaces status -n asic0 -d all | grep -c trunk")
        assert o.read().decode().strip() == str(out.count(" trunk "))
    finally:
        c.close()
    other = paramiko.Ed25519Key.from_private_key_file(str(_keygen(tmp_path / "other")))
    c2 = paramiko.SSHClient()
    c2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    with pytest.raises(paramiko.AuthenticationException):
        c2.connect("127.0.0.1", port=node.port, username="admin", pkey=other, look_for_keys=False, allow_agent=False)


def _keygen(path: Path) -> Path:
    import subprocess
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return path


def test_state_survives_a_restart_with_the_clock_shifted(tmp_path, monkeypatch):
    """--state-dir: the rack state is saved and restored; monotonic timestamps move with the clock, so a link down
    for 40 s before the restart is still down for 40 s after it; another topology / QoS starts fresh."""
    from fake_afm import State

    st = State(set(), {"switches": 1, "computes": 2})
    st.sim.links_down[(0, "asic0", "Ethernet0")] = {"since": 1000.0, "known": True}
    meta = {"topology": "mr3", "qos": None, "time_scale": 1.0, "realistic": False}
    f = tmp_path / vrack.STATE_FILE
    monkeypatch.setattr(vrack.time, "monotonic", lambda: 1040.0)
    vrack.save_state(st, f, meta)
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    monkeypatch.setattr(vrack.time, "monotonic", lambda: 50.0)          # machine restarted
    st2, why = vrack.load_state(f, meta)
    assert "restored" in why and st2.sim.links_down[(0, "asic0", "Ethernet0")]["since"] == 10.0   # 50 - 40
    assert vrack.load_state(f, {**meta, "qos": "lossless"})[0] is None


def test_g02_g17_answers_what_the_guide_models_ask():
    """10 Nov: in the 9 Nov gates' guided evals ~1/3 of the steps were 'no rule matches' (rc 127) - fec-histogram,
    lspci, journalctl -k, status / counters of one port, afmctl FEC statistics - and the cases failed on that."""
    import sys
    sys.path.insert(0, str(ROOT / "selftest"))
    from fake_afm import State
    from fake_rack import FakeComputeTransport, FakeSwitchTransport
    from helios.inventory import Host
    from helios.transport import ReplayTransport

    st = State(set(), {"switches": 6, "computes": 18, "layout": "g02-g17"})
    rp = str(ROOT / "selftest" / "replay")
    hs = Host(name="st1", host=st.sim.switch_ips[0], role="switch")
    sw = FakeSwitchTransport(hs, st, 0, ReplayTransport(hs, rp))
    port = next(p for p, _e in st.sim.all_ports(0, "asic0") if p in st.sim.ports(0, "asic0"))
    for cmd, want in ((f"show interfaces counters fec-histogram -n asic0 {port}", "BIN15"),
                      (f"show interfaces status -n asic0 {port}", port), ("show interfaces status -n asic0", port),
                      (f"show interfaces counters -n asic0 -i {port}", port), (f"show interfaces counters fec-stats -n asic0 {port}", port),
                      (f"show interfaces flap -n asic0 {port}", "Flap Count")):
        r = sw.run(cmd)
        assert r.rc == 0 and want in r.stdout, (cmd, r.rc, r.stdout[:200], r.stderr[:200])
    one = sw.run(f"show interfaces status -n asic0 {port}").stdout
    assert one.count("Ethernet") == 2                    # the port's own row (name and alias), not the chip's 257
    hc = Host(name="ct01", host="127.0.0.1", role="compute")
    ct = FakeComputeTransport(hc, st, 0, ReplayTransport(hc, rp))
    for cmd, want in (("lspci -d 1002:", "0001:01:00.0 Processing accelerators"), ("lspci -k -d 1002:", "Kernel driver in use: amdgpu"),
                      ("journalctl -k -p err -n 200 --no-pager", "No entries"), ("uname -r", "7.0.0-38-generic"),
                      ("dkms status", "amdgpu/"), ("cat /proc/cmdline", "BOOT_IMAGE"),
                      ("afmctl show port statistics fec -b 0001:01:00.1 -p 5", "FEC CW symbol errs 15")):
        r = ct.run(cmd)
        assert r.rc == 0 and want in r.stdout, (cmd, r.rc, r.stdout[:200], r.stderr[:200])
    assert ct.run("afmctl show port statistics fec -b 0001:01:00.0 -p 5").rc == 1    # .0 is lspci's: No such device
