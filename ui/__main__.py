"""python -m ui [--host 127.0.0.1] [--port 8765] [--config ui/racks.yaml] - the helios-tests web UI on
http://<host>:<port>/ (default 127.0.0.1: only this machine)."""
from __future__ import annotations

import argparse
import errno
import os
import socket
import sys
from pathlib import Path

from .keychain import Keychain
from .server import App, host_names, is_loopback, make_server
from .version import version

DEFAULT_HOST = "127.0.0.1"


def parse_args(argv=None, environ=None) -> argparse.Namespace:
    env = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(prog="python -m ui", description=__doc__)
    ap.add_argument("--host", default=(env.get("HELIOS_UI_HOST") or "").strip() or DEFAULT_HOST,
                    help="address to listen on (default 127.0.0.1, or $HELIOS_UI_HOST); 0.0.0.0 = every address: "
                         "the UI has no login, so anyone who reaches the port can use it")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", type=Path, help="rack profiles (default ui/racks.yaml)")
    ap.add_argument("--version", action="version", version=f"helios-ui {version()}")
    a = ap.parse_args(argv)
    if not a.host or any(c.isspace() or c in "/[]" for c in a.host):
        ap.error(f"--host {a.host!r}: an address (e.g. 127.0.0.1, 0.0.0.0, ::) or a host name")
    return a


def urls(host: str, port: int) -> list[str]:
    """Where a browser reaches the UI: the loopback URL, or for 0.0.0.0 / :: the host name and its addresses."""
    if is_loopback(host):
        return [f"http://127.0.0.1:{port}/"]
    if host in ("0.0.0.0", "::"):
        fq = socket.getfqdn()
        ips = {n for n in host_names() if n.replace(".", "").isdigit() and not n.startswith("127.")}
        try:                                      # the address of the default route (UDP connect sends nothing)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.connect(("10.255.255.255", 1))
                ips.add(u.getsockname()[0])
        except OSError:
            pass
        ips = sorted(i for i in ips if not i.startswith(("127.", "0.")))
        return [f"http://{fq}:{port}/"] + [f"http://{ip}:{port}/" for ip in ips] + [f"http://127.0.0.1:{port}/"]
    return [f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"]


def main(argv=None) -> int:
    a = parse_args(argv)
    app = App(a.config, keychain=Keychain())
    try:
        srv = make_server(app, a.port, a.host)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"helios-ui: port {a.port} on {a.host} is already in use (another UI running?): stop it or "
                  "start with --port N", file=sys.stderr, flush=True)
            return 2
        if e.errno == errno.EADDRNOTAVAIL or isinstance(e, socket.gaierror):
            print(f"helios-ui: cannot listen on {a.host}: not an address of this machine ({e})", file=sys.stderr,
                  flush=True)
            return 2
        raise
    where = urls(a.host, srv.server_address[1])
    print(f"helios-tests UI {version()} on {where[0]}  (listening on {a.host}:{srv.server_address[1]}; Ctrl-C stops "
          "it; runs in progress are stopped with it)", flush=True)
    if len(where) > 1:
        print("  also: " + "  ".join(where[1:]), flush=True)
    if not is_loopback(a.host):
        print("  NOTE: the UI has no login: anyone who can reach this port can start runs on the racks and read the "
              "results. Restrict it with the host firewall (team subnet only), or use --host 127.0.0.1 + an SSH "
              "tunnel.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
