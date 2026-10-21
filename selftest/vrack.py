"""Standalone virtual rack: the self-test simulation (fake_afm.State + fake_rack.Sim) served over real protocols, so
the UI, pytest with a normal SSH inventory, helios-debug, ssh and curl all talk to it as to a rack.

    python selftest/vrack.py [--topology afm3] [--ssh-port 22100] [--afm-port 18443] [--faults a,b]

- AFM: the fake controller on https://127.0.0.1:<afm-port> (self-signed). Simulator-only login admin / see
  selftest/fake_afm.py ADMIN - not a real credential.
- SSH: one server per switch and compute tray on 127.0.0.1:<ssh-port + n> (switches first, then trays), key login
  only: the public keys in <state-dir>/authorized_keys (default: your ~/.ssh/*.pub). Commands run through the same
  FakeSwitchTransport / FakeComputeTransport the self-tests use; ones they do not model fall back to the recorded
  outputs in selftest/replay (rc 127 "no rule matches" when none fits). An interactive shell is a line REPL.
- Control: http://127.0.0.1:<afm-port + 1>/  GET state, GET/POST /faults {"add": [...], "remove": [...]}.
- Writes <state-dir>/inventory.local.yaml (hosts + ports for pytest / the UI) and keeps its SSH host key there.

Bound to 127.0.0.1 only. The fabric behaviour is the simulation's (counters, watermarks, PFC are modelled values),
not a measurement - use it to develop and check tests, not to judge the product.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "selftest"))

from fake_afm import ADMIN, FakeAfm, State  # noqa: E402
from fake_rack import FakeComputeTransport, FakeSwitchTransport  # noqa: E402
from helios.inventory import Host  # noqa: E402
from helios.transport import ReplayTransport  # noqa: E402

TOPOLOGIES = {
    # name: (switches, computes, extra fake_topology keys, self-test inventory whose commands / thresholds it uses,
    #        switch name format, compute name format)
    "afm3": (3, 3, {}, "inventory-afm3.yaml", "sw{n}", "c{n}"),
    "mr3": (1, 2, {}, "inventory.yaml", "sw{n}", "c{n}"),
    "g02-g17": (6, 18, {"layout": "g02-g17"}, "inventory-g02-g17.yaml", "st{n}", "ct{n:02d}"),
}


def unwrap(cmd: str) -> tuple[str, bool, dict]:
    """The command as helios' SshTransport sent it -> (command, sudo, env): strips
    'timeout -k G N bash -c ...', 'sudo -n', 'env K=V' and their 'bash -c' wrappers."""
    sudo, env = False, {}
    for _ in range(6):
        try:
            tok = shlex.split(cmd)
        except ValueError:
            return cmd, sudo, env
        if len(tok) >= 5 and tok[0] == "timeout" and tok[1] == "-k" and tok[4:6] == ["bash", "-c"] and len(tok) == 7:
            cmd = tok[6]
            continue
        if tok[:1] == ["sudo"]:
            sudo = True
            rest = tok[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
            if rest[:1] == ["env"]:
                kv, r2 = {}, rest[1:]
                while r2 and "=" in r2[0] and not r2[0].startswith("-"):
                    k, v = r2.pop(0).split("=", 1)
                    kv[k] = v
                if r2[:2] == ["bash", "-c"] and len(r2) == 3:     # sudo -n env K=V bash -c '...' (build_command)
                    env.update(kv)
                    cmd = r2[2]
                    continue
            if rest[:2] == ["bash", "-c"] and len(rest) == 3:
                cmd = rest[2]
                continue
            cmd = shlex.join(rest) if rest else ""
            continue
        if tok[:1] == ["env"] and len(tok) > 1 and "=" in tok[1]:
            # only build_command's own wrapper (env K=V bash -c '...'); a test's 'env K=V tool ...' is the command
            rest = tok[1:]
            kv = {}
            while rest and "=" in rest[0]:
                k, v = rest.pop(0).split("=", 1)
                kv[k] = v
            if rest[:2] == ["bash", "-c"] and len(rest) == 3:
                env.update(kv)
                cmd = rest[2]
                continue
        break
    return cmd, sudo, env


NO_RULE = "no rule matches"


def split_shell(line: str) -> list[tuple[str, str]]:
    """[(operator before, segment)] split on unquoted '|', ';', '&&', '||' (operator '' for the first segment);
    quotes and backslashes are kept, so each segment is the text a rule would see."""
    out, cur, op, q, i = [], [], "", None, 0
    while i < len(line):
        ch = line[i]
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            elif ch == "\\" and q == '"' and i + 1 < len(line):
                cur.append(line[i + 1])
                i += 1
        elif ch in "'\"":
            q = ch
            cur.append(ch)
        elif ch == "\\" and i + 1 < len(line):
            cur += [ch, line[i + 1]]
            i += 1
        elif line.startswith(("&&", "||"), i) or ch in "|;":
            tok = line[i:i + 2] if line.startswith(("&&", "||"), i) else ch
            out.append((op, "".join(cur).strip()))
            cur, op = [], tok
            i += len(tok)
            continue
        else:
            cur.append(ch)
        i += 1
    out.append((op, "".join(cur).strip()))
    return [(o, s) for o, s in out if s]


def _filter(argv: list[str], data: str) -> tuple[int, str] | None:
    """The text filters a pipeline may use on a node (grep, head, tail, wc, sort, uniq, cut, awk print, tr -d,
    cat); None when the command is not one of them."""
    import re as _re

    lines = data.splitlines()
    name, args = argv[0], argv[1:]
    if name == "cat" and not args:
        return 0, data
    if name == "grep":
        flags, pats = set(), []
        while args and args[0].startswith("-") and len(args[0]) > 1:
            a = args.pop(0)
            if a in ("-e",):
                pats.append(args.pop(0))
                continue
            flags |= set(a[1:])
        pats += args[:1]
        if not pats:
            return 2, ""
        rx = [(_re.escape(p) if "F" in flags else (p if "E" in flags else p.replace("\\|", "|").replace("\\(", "(").replace("\\)", ")")))
              for p in pats]
        cre = _re.compile("|".join(f"(?:{r})" for r in rx), _re.I if "i" in flags else 0)
        hit = [ln for ln in lines if bool(cre.search(ln)) != ("v" in flags)]
        if "o" in flags and "v" not in flags:
            hit = [m.group(0) for ln in hit for m in cre.finditer(ln)]
        if "c" in flags:
            return (0 if hit else 1), f"{len(hit)}\n"
        if "q" in flags:
            return (0 if hit else 1), ""
        return (0 if hit else 1), "".join(ln + "\n" for ln in hit)
    if name in ("head", "tail"):
        n = 10
        it = iter(args)
        for a in it:
            if a == "-n":
                a = next(it, "10")
                n_s = a
            elif a.startswith("-n"):
                n_s = a[2:]
            elif a.startswith("-") and a[1:].lstrip("+").isdigit():
                n_s = a[1:]
            else:
                continue
            if name == "tail" and n_s.startswith("+"):
                return 0, "".join(ln + "\n" for ln in lines[int(n_s[1:]) - 1:])
            n = int(n_s)
        sel = lines[:n] if name == "head" else (lines[-n:] if n else [])
        return 0, "".join(ln + "\n" for ln in sel)
    if name == "wc":
        if "-l" in args:
            return 0, f"{len(lines)}\n"
        if "-w" in args:
            return 0, f"{len(data.split())}\n"
        if "-c" in args:
            return 0, f"{len(data.encode())}\n"
        return 0, f"{len(lines)} {len(data.split())} {len(data.encode())}\n"
    if name == "sort":
        fl = "".join(a[1:] for a in args if a.startswith("-"))
        key = (lambda s: float(_re.match(r"\s*(-?[\d.]+)", s).group(1)) if _re.match(r"\s*-?[\d.]+", s) else 0.0) if "n" in fl else None
        out = sorted(set(lines) if "u" in fl else lines, key=key, reverse="r" in fl)
        return 0, "".join(ln + "\n" for ln in out)
    if name == "uniq":
        out, prev, n = [], None, 0
        for ln in lines + [None]:
            if ln == prev:
                n += 1
                continue
            if prev is not None:
                out.append(f"{n:>7} {prev}" if "-c" in args else prev)
            prev, n = ln, 1
        return 0, "".join(ln + "\n" for ln in out)
    if name == "cut":
        d, fields = "\t", []
        it = iter(args)
        for a in it:
            if a == "-d":
                d = next(it, "\t")
            elif a.startswith("-d"):
                d = a[2:]
            elif a == "-f":
                fields = next(it, "1")
            elif a.startswith("-f"):
                fields = a[2:]
        idx = []
        for part in str(fields).split(","):
            a_, _, b_ = part.partition("-")
            idx += list(range(int(a_), int(b_) + 1)) if b_ else [int(a_)]
        return 0, "".join(d.join(ln.split(d)[k - 1] for k in idx if k - 1 < len(ln.split(d))) + "\n" for ln in lines)
    if name == "awk":
        prog = next((a for a in args if a.startswith("{")), "")
        m = _re.fullmatch(r"\{\s*print\s+((?:\$\w+|NF|\$NF)(?:\s*,\s*(?:\$\w+|\$NF))*)\s*\}", prog.strip())
        if not m:
            return None
        cols = [c.strip() for c in m.group(1).split(",")]
        out = []
        for ln in lines:
            f = ln.split()
            vals = []
            for c in cols:
                k = len(f) if c == "$NF" else (0 if c == "$0" else int(c[1:]))
                vals.append(ln if k == 0 else (f[k - 1] if 0 < k <= len(f) else ""))
            out.append(" ".join(vals))
        return 0, "".join(ln + "\n" for ln in out)
    if name == "sed" and len(args) == 1 and (m := _re.fullmatch(r"s(.)(.*?)\1(.*?)\1(g?)", args[0])):
        rx, rep, g = _re.compile(m.group(2)), _re.sub(r"\\(\d)", r"\\\1", m.group(3)), m.group(4)
        return 0, "".join(rx.sub(rep, ln, count=0 if g else 1) + "\n" for ln in lines)
    if name == "tr" and args[:1] == ["-d"] and len(args) == 2:
        return 0, data.translate({ord(ch): None for ch in args[1].replace("\\n", "\n")})
    return None


class Node:
    """One simulated host: its SSH port and the fake transport that answers its commands."""

    def __init__(self, name: str, role: str, port: int, transport):
        self.name, self.role, self.port, self.t = name, role, port, transport

    def run(self, raw: str, timeout: float = 120) -> tuple[int, str, str]:
        cmd, sudo, env = unwrap(raw)
        if not cmd.strip():
            return 0, "", ""
        r = self.t.run(cmd, timeout=timeout, sudo=sudo, env=env or None)
        if r.rc == 127 and NO_RULE in (r.stderr or "") and len(split_shell(cmd)) > 1:
            # not modelled as a whole: run it as a small shell (pipes, ';', '&&', '||' and the common filters)
            return self.shell(cmd, sudo, timeout)
        return r.rc, r.stdout, r.stderr

    def shell(self, line: str, sudo: bool = False, timeout: float = 120) -> tuple[int, str, str]:
        rc, out, err, data = 0, "", "", None
        for op, seg in split_shell(line):
            if op in (";", "&&", "||"):
                out += data if data is not None else ""
                data = None
                if (op == "&&" and rc != 0) or (op == "||" and rc == 0):
                    continue
            if op == "|" and data is not None:
                try:
                    argv = shlex.split(seg)
                except ValueError:
                    argv = []
                if argv[:1] == ["sudo"]:
                    argv = [a for a in argv[1:] if a != "-n"]
                f = _filter(argv, data) if argv else None
                if f is None:
                    rc, data, err = 127, "", err + f"vrack shell: '{argv[0] if argv else seg}' is not supported in a pipe\n"
                    continue
                rc, data = f
                continue
            try:
                argv = shlex.split(seg)
            except ValueError:
                argv = []
            if argv[:1] in (["echo"], ["true"], ["false"]):
                rc, o, e = (0, " ".join(argv[1:]) + "\n", "") if argv[0] == "echo" else (0 if argv[0] == "true" else 1, "", "")
            else:
                rc, o, e = self.run(f"sudo -n bash -c {shlex.quote(seg)}" if sudo else seg, timeout)
            data, err = o, err + (e or "")
        out += data if data is not None else ""
        return rc, out, err


class _Server(paramiko.ServerInterface):
    """One SSH connection: every exec / shell request starts its own thread on that channel, so the parallel
    commands helios sends over one connection never share state."""

    def __init__(self, node: Node, keys: set[tuple[str, bytes]]):
        self.node, self.keys = node, keys

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if (key.get_name(), key.asbytes()) in self.keys else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    # paramiko answers the request (channel success) only after these return: the worker starts on a timer so its
    # output can never overtake that answer (the client would drop the channel, ssh rc 255)
    def check_channel_exec_request(self, channel, command):
        threading.Timer(0.02, _exec, args=(self.node, channel, command.decode("utf-8", "replace"))).start()
        return True

    def check_channel_shell_request(self, channel):
        threading.Timer(0.02, _shell, args=(self.node, channel)).start()
        return True

    def check_channel_pty_request(self, *a):
        return True

    def check_channel_env_request(self, *a):
        return True


def _exec(node: Node, chan: paramiko.Channel, cmd: str) -> None:
    try:
        rc, out, err = node.run(cmd)
        chan.sendall(out.encode())
        if err:
            chan.sendall_stderr(err.encode())
        chan.send_exit_status(rc)
    except Exception as e:  # noqa: BLE001 - one broken command must not stop the node
        try:
            chan.sendall_stderr(f"vrack: {e}\n".encode())
            chan.send_exit_status(255)
        except Exception:  # noqa: BLE001
            pass
    finally:
        chan.close()


def _shell(node: Node, chan: paramiko.Channel) -> None:
    """A line REPL: each line runs like an exec request (no job control, no pipes beyond what the rules model)."""
    prompt = f"admin@{node.name}:~$ ".encode()
    try:
        chan.sendall(f"virtual rack node {node.name} ({node.role}) - simulated; 'exit' leaves\r\n".encode() + prompt)
        buf = b""
        while True:
            ch = chan.recv(1)
            if not ch or ch == b"\x04":
                return
            if ch in (b"\r", b"\n"):
                line, buf = buf.decode("utf-8", "replace").strip(), b""
                chan.sendall(b"\r\n")
                if line in ("exit", "logout"):
                    chan.send_exit_status(0)
                    return
                if line:
                    rc, out, err = node.run(line)
                    chan.sendall((out + err).replace("\n", "\r\n").encode())
                chan.sendall(prompt)
            elif ch in (b"\x7f", b"\x08"):
                if buf:
                    buf = buf[:-1]
                    chan.sendall(b"\b \b")
            elif ch == b"\x03":
                buf = b""
                chan.sendall(b"^C\r\n" + prompt)
            else:
                buf += ch
                chan.sendall(ch)
    except Exception:  # noqa: BLE001
        pass
    finally:
        chan.close()


def _client(node: Node, sock: socket.socket, host_key: paramiko.PKey, keys: set) -> None:
    tr = paramiko.Transport(sock)
    tr.add_server_key(host_key)
    try:
        tr.start_server(server=_Server(node, keys))
    except Exception:  # noqa: BLE001 - bad handshake
        return
    open_chans: list = []        # served by the exec / shell request handlers; held here because paramiko closes a
    while tr.is_active():        # channel object that is garbage-collected
        ch = tr.accept(30)
        open_chans = [c for c in open_chans if not c.closed] + ([ch] if ch is not None else [])


def _listen(node: Node, bind: str, host_key, keys) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind, node.port))
    s.listen(64)
    while True:
        conn, _ = s.accept()
        threading.Thread(target=_client, args=(node, conn, host_key, keys), daemon=True).start()


def _control(st: State, bind: str, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, indent=1, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            with st.lock:
                if self.path.startswith("/faults"):
                    return self._send(200, {"faults": sorted(st.f)})
                vp = st.objs.get("VPod", {})
                self._send(200, {"faults": sorted(st.f),
                                 "computes": {n: (o.get("status") or {}).get("admission-phase") for n, o in st.objs["ComputeNode"].items()},
                                 "switches": {n: (o.get("status") or {}).get("admission-phase") for n, o in st.objs["SwitchNode"].items()},
                                 "vpods": {n: (o.get("status") or {}).get("vpod-state") for n, o in vp.items()}})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "JSON body expected: {\"add\": [...], \"remove\": [...]}"})
            with st.lock:
                st.f |= set(req.get("add") or [])
                st.f -= set(req.get("remove") or [])
                self._send(200, {"faults": sorted(st.f)})

    srv = ThreadingHTTPServer((bind, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _keys(state_dir: Path) -> list[paramiko.PKey]:
    ak = state_dir / "authorized_keys"
    if not ak.exists():
        pubs = sorted(Path("~/.ssh").expanduser().glob("*.pub"))
        ak.write_text("".join(p.read_text().strip() + "\n" for p in pubs))
        ak.chmod(0o600)
    out = []
    for line in ak.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("#"):
            try:
                out.append(paramiko.PKey.from_type_string(parts[0], __import__("base64").b64decode(parts[1])))
            except Exception:  # noqa: BLE001 - a key type paramiko does not know
                pass
    return out


def _host_key(state_dir: Path) -> paramiko.PKey:
    f = state_dir / "ssh_host_ed25519_key"
    if not f.exists():
        import subprocess
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "vrack", "-f", str(f)], check=True)
    return paramiko.Ed25519Key.from_private_key_file(str(f))


def write_inventory(path: Path, st: State, nodes: list[Node], afm_url: str, topo_name: str, base: str,
                    template: str | None = None) -> None:
    sw = [n for n in nodes if n.role == "switch"]
    ct = [n for n in nodes if n.role == "compute"]
    lines = [f"# virtual rack ({topo_name}) - written by selftest/vrack.py; simulated, nothing here is a real host",
             f"rack: vrack-{topo_name}",
             "transport:", "  kind: ssh", "  connect_timeout: 10", "defaults:",
             "  switch: {user: admin}", "  compute: {user: admin, gpus: 4}", "switches:"]
    # afm_name pairs each node with its AFM object: every node is the same loopback address here, so the tests'
    # by-IP lookups (controller -> switch, ComputeNode -> tray) cannot work
    lines += [f"  - {{name: {n.name}, host: {base}, port: {n.port}, afm_name: {st.sim.sname(k)}}}" for k, n in enumerate(sw)]
    lines += ["computes:"] + [f"  - {{name: {n.name}, host: {base}, port: {n.port}, afm_name: {st.sim.cname(k)}}}"
                              for k, n in enumerate(ct)]
    lines += ["afm:", f"  url: {afm_url}", "  verify_tls: false", "  auth:", "    kind: session",
              f"    username: {ADMIN[0]}", "    password_env: HELIOS_AFM_PASSWORD   # simulator password: selftest/fake_afm.py ADMIN",
              "    tenant: default"]
    if template:
        # the self-test inventory's commands / thresholds (TransferBench, l2ping, ...): the same tool commands the fake
        # trays answer; everything else in it (replay transport, fake AFM) is replaced above
        import yaml
        tpl = yaml.safe_load((ROOT / "selftest" / template).read_text()) or {}
        rest = {k: tpl[k] for k in ("commands", "thresholds") if k in tpl}
        if rest:
            lines.append(yaml.safe_dump(rest, sort_keys=False).rstrip())
    path.write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--topology", default="afm3", choices=sorted(TOPOLOGIES))
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--ssh-port", type=int, default=22100, help="first SSH port (switches, then compute trays)")
    ap.add_argument("--afm-port", type=int, default=18443, help="AFM HTTPS port; the control API is on the next port")
    ap.add_argument("--faults", default=os.environ.get("HELIOS_FAKE_AFM_FAULTS", ""))
    ap.add_argument("--state-dir", default=str(Path("~/.helios-vrack").expanduser()))
    ap.add_argument("--time-scale", type=float, default=1.0, help="simulated delays x this (self-tests use 0.05)")
    a = ap.parse_args(argv)
    if a.bind not in ("127.0.0.1", "localhost", "::1"):
        ap.error("the virtual rack binds to the loopback address only")
    state_dir = Path(a.state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    n_sw, n_ct, extra, template, sw_fmt, ct_fmt = TOPOLOGIES[a.topology]
    st = State(set(filter(None, a.faults.split(","))),
               {"switches": n_sw, "computes": n_ct, "time_scale": a.time_scale, **extra})
    afm = FakeAfm(state=st, bind=(a.bind, a.afm_port)).start()
    replay = str(ROOT / "selftest" / "replay")
    nodes: list[Node] = []
    port = a.ssh_port
    for i in range(n_sw):
        h = Host(name=sw_fmt.format(n=i + 1), host=st.sim.switch_ips[i], role="switch")
        nodes.append(Node(h.name, "switch", port, FakeSwitchTransport(h, st, i, ReplayTransport(h, replay))))
        port += 1
    for s in range(n_ct):
        h = Host(name=ct_fmt.format(n=s + 1), host=f"10.0.1.{s + 1}", role="compute")
        nodes.append(Node(h.name, "compute", port, FakeComputeTransport(h, st, s, ReplayTransport(h, replay))))
        port += 1
    host_key, keys = _host_key(state_dir), {(k.get_name(), k.asbytes()) for k in _keys(state_dir)}
    if not keys:
        print(f"no public key in {state_dir / 'authorized_keys'} - SSH logins will fail", file=sys.stderr)
    for n in nodes:
        threading.Thread(target=_listen, args=(n, a.bind, host_key, keys), daemon=True).start()
    _control(st, a.bind, a.afm_port + 1)
    inv = state_dir / "inventory.local.yaml"
    write_inventory(inv, st, nodes, afm.url, a.topology, a.bind, template)
    print(f"virtual rack '{a.topology}' up ({n_sw} switches, {n_ct} compute trays) - simulated, loopback only")
    print(f"  AFM      {afm.url}   (simulator login: user {ADMIN[0]}, password in selftest/fake_afm.py ADMIN)")
    print(f"  control  http://{a.bind}:{a.afm_port + 1}/   (GET state; POST /faults {{\"add\": [..], \"remove\": [..]}})")
    for n in nodes:
        print(f"  ssh -p {n.port} admin@{a.bind}   # {n.name} ({n.role})")
    print(f"  inventory: {inv}")
    sys.stdout.flush()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        afm.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
