#!/usr/bin/env bash
# Start the virtual rack (selftest/vrack.py): a simulated Helios rack on 127.0.0.1 - fake AFM (HTTPS), one SSH
# server per switch / compute tray, a control API - and its inventory in ~/.helios-vrack/inventory.local.yaml.
#   tools/vrack.sh                       # g02-g17 layout (6 switches, 18 trays), foreground, Ctrl-C stops it
#   tools/vrack.sh --topology afm3       # 3 switches / 3 trays;  --topology mr3: 1 switch / 2 trays
#   tools/vrack.sh --ssh-port 23100 --afm-port 19443 --faults no_alerts
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
py=""
for c in "$root/.venv/bin/python" "$root/../helios-tests/.venv/bin/python" "$(command -v python3 || true)"; do
    if [ -n "$c" ] && [ -x "$c" ] && "$c" -c "import paramiko, yaml" 2>/dev/null; then py=$c; break; fi
done
[ -n "$py" ] || { echo "vrack: no python with paramiko + PyYAML (make the venv: python3 -m venv .venv && .venv/bin/pip install -e .)" >&2; exit 1; }
case " $* " in *" --topology "*) exec "$py" "$root/selftest/vrack.py" "$@" ;; esac
exec "$py" "$root/selftest/vrack.py" --topology g02-g17 "$@"
