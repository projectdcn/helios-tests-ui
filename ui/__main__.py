"""python -m ui [--port 8765] [--config ui/racks.yaml] - the helios-tests web UI on http://127.0.0.1:<port>/"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .server import App, make_server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ui", description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", type=Path, help="rack profiles (default ui/racks.yaml)")
    a = ap.parse_args(argv)
    app = App(a.config)
    srv = make_server(app, a.port)
    print(f"helios-tests UI on http://127.0.0.1:{srv.server_address[1]}/  (Ctrl-C stops it; runs in progress are "
          "stopped with it)", flush=True)
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
