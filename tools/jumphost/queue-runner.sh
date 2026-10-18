#!/usr/bin/env bash
# queue-runner.sh - run queued g02-g17 test-case lists with the AFM password typed once (kept in this shell only).
#
#   tmux new -A -s helios-queue ~/helios/queue-runner.sh     # type the password, then detach: Ctrl-b d
#
# A job is a file ~/helios/queue/<name>.tcs holding ONLY test-case ids (comma / space separated); the runner builds
# the pytest command itself (tests, --allow-disruptive, the jump-host inventory). Jobs run one at a time in name
# order; finished ones move to queue/done/, the log is queue/logs/<name>.log, results in ~/helios/results/queue/.
# queue/state says idle / running <name>. Touch queue/STOP to end the runner.
set -u
H=~/helios
Q=$H/queue
CODE=$H/helios-tests
PY=$H/venv/bin/python
INV=$H/fullrack-jump-afm.yaml
RES=$H/results/queue
mkdir -p "$Q/done" "$Q/logs" "$RES"

IFS= read -r -s -p "AFM admin password (g02-g17): " HELIOS_AFM_PASSWORD; echo
export HELIOS_AFM_PASSWORD
if ! (cd "$CODE" && "$PY" -c "
import sys
from helios.inventory import load_inventory
from helios.afm import AfmClient
try:
    AfmClient(dict(load_inventory('$INV').afm or {})).login()
except Exception as e:
    print('AFM login error:', type(e).__name__); sys.exit(1)
print('AFM login OK')
" 2>/dev/null); then
    echo "AFM login FAILED - wrong password? Run the script again."; unset HELIOS_AFM_PASSWORD; exit 1
fi
echo "queue runner ready: waiting for jobs in $Q (detach with Ctrl-b d; touch $Q/STOP to end)"
echo idle > "$Q/state"
while [ ! -e "$Q/STOP" ]; do
    job=$(ls "$Q"/*.tcs 2>/dev/null | sort | head -1)
    if [ -z "$job" ]; then sleep 20; continue; fi
    name=$(basename "$job" .tcs)
    tcs=$(tr -s ' \n\t' ',,,' < "$job" | sed 's/^,*//; s/,*$//')
    if ! [[ "$tcs" =~ ^[A-Za-z0-9_.-]+(,[A-Za-z0-9_.-]+)*$ ]]; then
        echo "$(date -u +%FT%TZ) $name REJECTED (not a test-case id list)" | tee -a "$Q/history"
        mv "$job" "$Q/done/$name.rejected"; continue
    fi
    echo "running $name $(date -u +%FT%TZ)" > "$Q/state"
    echo "$(date -u +%FT%TZ) $name start ($(tr ',' '\n' <<<"$tcs" | wc -l) cases)" | tee -a "$Q/history"
    (cd "$CODE" && "$PY" -m pytest tests --inventory "$INV" --results-dir "$RES" -p no:cacheprovider \
        --tc "$tcs" --allow-disruptive) > "$Q/logs/$name.log" 2>&1 < /dev/null
    rc=$?
    mv "$job" "$Q/done/$name.tcs"
    echo "$(date -u +%FT%TZ) $name end rc=$rc $(grep -m1 -o 'helios results: [^ ]*' "$Q/logs/$name.log" | sed 's/helios results: //')" | tee -a "$Q/history"
    echo idle > "$Q/state"
done
rm -f "$Q/STOP"; echo stopped > "$Q/state"; unset HELIOS_AFM_PASSWORD; echo "queue runner stopped"
