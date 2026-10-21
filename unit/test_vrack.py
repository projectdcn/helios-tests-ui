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
