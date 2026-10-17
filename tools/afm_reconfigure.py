"""Clear a pending data-fabric reprogram (reprogram_df) on the system vPOD: show its state, then - after you confirm -
POST Reconfigure to every compute named in the pending operation and wait until the pending ops are empty.

    read -s "HELIOS_AFM_PASSWORD?AFM admin password: " && export HELIOS_AFM_PASSWORD && echo
    .venv/bin/python tools/afm_reconfigure.py --inventory inventory/mr3.local.yaml          # show + ask
    .venv/bin/python tools/afm_reconfigure.py --inventory inventory/mr3.local.yaml --show   # show only
    unset HELIOS_AFM_PASSWORD

This changes the data fabric of those computes (traffic on them is disturbed while it reprograms). The AFM password
is read from the variable the inventory names (afm.auth.password_env); it is never printed or stored.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from helios.afm import AfmClient  # noqa: E402
from helios.afm_ops import AfmOps  # noqa: E402
from helios.inventory import load_inventory  # noqa: E402


def state(ops: AfmOps, name: str) -> tuple[dict, list]:
    st = ops.vpod(name).get("status") or {}
    return st, st.get("pending-ops") or []


def show(ops: AfmOps, name: str) -> list:
    st, pend = state(ops, name)
    print(f"system vPOD {name}: state {st.get('vpod-state') or st.get('state')}, pending ops {len(pend)}")
    for p in pend:
        gpus = (p.get("pending-components") or {}).get("gpus") or []
        print(f"  - {p.get('operation')} version {p.get('version')}: {len(gpus)} GPU(s) {', '.join(gpus[:8])}"
              + (" ..." if len(gpus) > 8 else ""))
    try:
        fp = ops.afm.items("/configs/cluster/v1/failoverpolicies")
        for f in fp:
            print(f"  failover policy {f['meta']['name']}: datafabric-reconfig={(f.get('spec') or {}).get('datafabric-reconfig')}")
    except Exception as e:  # noqa: BLE001 - informational only
        print(f"  (failover policies not readable: {e})")
    return pend


def computes_of(pend: list) -> list[str]:
    names = set()
    for p in pend:
        for g in (p.get("pending-components") or {}).get("gpus") or []:
            m = re.match(r"(.+)-GPU-\d+$", g)
            if m:
                names.add(m.group(1))
    return sorted(names)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--show", action="store_true", help="only show the state, change nothing")
    ap.add_argument("--yes", action="store_true", help="do not ask before reconfiguring")
    ap.add_argument("--timeout", type=float, default=600, help="seconds to wait for the pending ops to clear")
    a = ap.parse_args(argv)
    inv = load_inventory(a.inventory)
    afm = AfmClient(dict(inv.afm or {}))
    if afm.kind == "session" and afm.password is None:
        print(f"AFM password not set: export {afm.auth.get('password_env', 'HELIOS_AFM_PASSWORD')} first", file=sys.stderr)
        return 2
    afm.login()
    ops = AfmOps(afm, None, inv)
    name = ops.system_vpod_name()
    pend = show(ops, name)
    if a.show:
        return 0
    targets = computes_of(pend)
    if not targets:
        print("nothing pending - no Reconfigure needed")
        return 0
    if not a.yes and input(f"POST Reconfigure to {', '.join(targets)}? This reprograms their data fabric. [y/N] ").strip().lower() != "y":
        print("not changed")
        return 1
    for c in targets:
        r = ops.reconfigure(c)
        body = r.text if len(r.text) < 300 else r.text[:300] + " ..."
        print(f"Reconfigure {c}: HTTP {r.status_code} {body}")
    ok = ops.wait_pending_clear(name, a.timeout)
    show(ops, name)
    print("pending ops cleared" if ok else f"pending ops NOT cleared within {a.timeout:.0f} s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
