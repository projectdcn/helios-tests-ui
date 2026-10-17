#!/usr/bin/env bash
# install.sh - install, upgrade or roll back the helios-tests web UI for the CURRENT user (no root needed).
#
#   ./install.sh                     install to ~/helios-ui (offline: wheels from ./wheelhouse), start it, check it
#   ./install.sh                     again with a newer bundle = upgrade: app/ + venv/ replaced (old ones kept as
#                                    app.prev / venv.prev), config/ and data/ kept
#   ~/helios-ui/bin/install.sh --rollback      back to app.prev / venv.prev (run it again to roll forward)
#
# Options: --prefix DIR (default ~/helios-ui)  --host ADDR (default 0.0.0.0 = every address; 127.0.0.1 = this
# server only, reach it through an SSH tunnel)  --port N (default 8765)  --python PATH  --online (pip from PyPI
# instead of ./wheelhouse)  --no-systemd (nohup + bin/helios-ui helper)  --no-start  --force (upgrade even while a
# run is going; it is stopped)  --no-token (no access token, see below)  --dry-run (print what would be done,
# change nothing)  -h|--help
#
# Listening beyond 127.0.0.1, the UI gets an access token (HELIOS_UI_TOKEN in config/ui.env, generated once, kept by
# upgrades): each browser logs in with it once. Without it (--no-token) anyone who can reach host:port can start runs
# on the racks and read the results. Restrict the port with the host firewall (team subnet) either way.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PREFIX="$HOME/helios-ui"
PORT=8765
HOST=0.0.0.0
HOST_SET=0
PYTHON=""
ONLINE=0
DRY=0
ROLLBACK=0
NO_START=0
NO_SYSTEMD=0
FORCE=0
NO_TOKEN=0
TOKEN_NEW=""
PORT_SET=0
UNIT=helios-ui.service
MIN_PY=3.10

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# run CMD...: echo it; in --dry-run only echo
run()  { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
# write_file PATH [MODE] < content
write_file() {
    if [ "$DRY" = 1 ]; then
        printf '  [dry-run] write %s:\n' "$1"; sed 's/^/  | /'
    else
        cat > "$1.tmp.$$" && mv "$1.tmp.$$" "$1"
        [ -z "${2:-}" ] || chmod "$2" "$1"
    fi
}

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)      [ $# -ge 2 ] || die "--prefix needs a directory"; PREFIX=$2; shift 2 ;;
        --prefix=*)    PREFIX=${1#*=}; shift ;;
        --host)        [ $# -ge 2 ] || die "--host needs an address"; HOST=$2; HOST_SET=1; shift 2 ;;
        --host=*)      HOST=${1#*=}; HOST_SET=1; shift ;;
        --port)        [ $# -ge 2 ] || die "--port needs a number"; PORT=$2; PORT_SET=1; shift 2 ;;
        --port=*)      PORT=${1#*=}; PORT_SET=1; shift ;;
        --python)      [ $# -ge 2 ] || die "--python needs a path"; PYTHON=$2; shift 2 ;;
        --python=*)    PYTHON=${1#*=}; shift ;;
        --online)      ONLINE=1; shift ;;
        --dry-run)     DRY=1; shift ;;
        --rollback)    ROLLBACK=1; shift ;;
        --no-start)    NO_START=1; shift ;;
        --no-systemd)  NO_SYSTEMD=1; shift ;;
        --force)       FORCE=1; shift ;;
        --no-token)    NO_TOKEN=1; shift ;;
        -h|--help)     usage 0 ;;
        *)             warn "unknown option: $1"; usage 2 ;;
    esac
done

# ------------------------------------------------------------------ prefix, port, state
case "$PREFIX" in
    "~"|"~/"*) PREFIX="$HOME${PREFIX#\~}" ;;
esac
case "$PREFIX" in
    /*) ;;
    *)  PREFIX="$(pwd)/$PREFIX" ;;
esac
PREFIX=${PREFIX%/}
# the prefix goes into YAML, a systemd unit and shell scripts: keep it plain
if ! printf '%s' "$PREFIX" | grep -Eq '^/[A-Za-z0-9._/+-]+$'; then
    die "--prefix '$PREFIX': use letters, digits and . _ / + - only (no spaces)"
fi
[ "$PREFIX" != "$HOME" ] && [ "$PREFIX" != "/" ] || die "--prefix must be a directory of its own, not $PREFIX"

STATE="$PREFIX/.install-state"
if [ -f "$STATE" ]; then
    # written by this script: INSTALLED_PORT, INSTALLED_HOST, INSTALLED_MODE, INSTALLED_VERSION
    # shellcheck disable=SC1090
    . "$STATE"
fi
if [ "$PORT_SET" = 0 ] && [ -n "${INSTALLED_PORT:-}" ]; then
    PORT=$INSTALLED_PORT                  # an upgrade keeps the port of the installation
fi
if [ "$HOST_SET" = 0 ] && [ -n "${INSTALLED_HOST:-}" ]; then
    HOST=$INSTALLED_HOST                  # ... and its address (an install from before --host: 0.0.0.0)
fi
printf '%s' "$HOST" | grep -Eq '^[A-Za-z0-9.:-]+$' || die "--host '$HOST': an address such as 0.0.0.0, 127.0.0.1 or ::"
case "$PORT" in
    ''|*[!0-9]*) die "--port '$PORT': a number" ;;
esac
[ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || die "--port $PORT: use 1024-65535 (no root needed)"

# ------------------------------------------------------------------ helpers
systemd_user_ok() {
    [ "$NO_SYSTEMD" = 0 ] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user show-environment >/dev/null 2>&1
}

service_installed() { [ -f "$HOME/.config/systemd/user/$UNIT" ]; }

# stop whatever runs the UI of this prefix (systemd unit or the nohup helper)
stop_ui() {
    if service_installed && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        if systemctl --user is-active --quiet "$UNIT"; then
            say "stopping $UNIT"
            run systemctl --user stop "$UNIT"
        fi
    fi
    if [ -x "$PREFIX/bin/helios-ui" ] && [ -f "$PREFIX/logs/helios-ui.pid" ]; then
        run "$PREFIX/bin/helios-ui" stop || true
    fi
    [ "$DRY" = 1 ] || wait_ui_gone
}

# processes of THIS installation's UI (venv python -m ui ... --config <prefix>/config/racks.yaml)
our_ui_pids() {
    if command -v pgrep >/dev/null 2>&1; then
        pgrep -f -- "-m ui .*--config $PREFIX/config/racks.yaml" 2>/dev/null || true
    else
        ps -eo pid=,args= 2>/dev/null | grep -F -- "--config $PREFIX/config/racks.yaml" | grep -F -- "-m ui" \
            | grep -v grep | awk '{print $1}' || true
    fi
}

# after a stop: wait (up to 20 s) until our old UI has exited; still there: TERM, then KILL. The port check comes
# after this, so a UI that is just shutting down is never reported as "another program"
wait_ui_gone() {
    local i pids
    for i in $(seq 1 40); do
        pids=$(our_ui_pids)
        [ -n "$pids" ] || return 0
        [ "$i" = 1 ] && say "waiting for the previous UI to exit (pid $(echo $pids))"
        sleep 0.5
    done
    say "the previous UI (pid $(echo $pids)) did not exit in 20 s: sending TERM"
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true
    for i in $(seq 1 20); do
        [ -n "$(our_ui_pids)" ] || return 0
        sleep 0.5
    done
    pids=$(our_ui_pids)
    # shellcheck disable=SC2086
    [ -z "$pids" ] || kill -KILL $pids 2>/dev/null || true
    sleep 1
}

port_free() {   # $1 python, $2 port, $3 host (exit 1: taken, 2: not an address of this machine)
    "$1" - "$2" "$3" <<'PYEOF'
import errno, socket, sys
port, host = int(sys.argv[1]), sys.argv[2]
for target in {"127.0.0.1", host} - {"0.0.0.0", "::"}:
    c = socket.socket(socket.AF_INET6 if ":" in target else socket.AF_INET)
    c.settimeout(2)
    try:                              # something answers there: taken
        c.connect((target, port))
        sys.exit(1)
    except OSError:
        pass
    finally:
        c.close()
s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # as the UI binds (old TIME_WAIT connections are fine)
try:
    s.bind((host, port))
except OSError as e:
    sys.exit(2 if e.errno == errno.EADDRNOTAVAIL or isinstance(e, socket.gaierror) else 1)
finally:
    s.close()
PYEOF
}

check_port() {   # $1 python, $2 advice
    local rc=0 i
    for i in $(seq 1 20); do              # a socket being closed right now: give it a few seconds
        rc=0
        port_free "$1" "$PORT" "$HOST" || rc=$?
        [ "$rc" = 1 ] || break
        [ -n "$(our_ui_pids)" ] && wait_ui_gone && continue
        sleep 0.5
    done
    if [ "$rc" = 1 ] && [ -n "$(our_ui_pids)" ]; then
        die "port $PORT is still held by this installation's previous UI (pid $(echo $(our_ui_pids))): stop it ($PREFIX/bin/helios-ui stop) and run install.sh again"
    fi
    case "$rc" in
        0) ;;
        2) die "--host $HOST is not an address of this server (use 0.0.0.0 for every address, or 127.0.0.1)" ;;
        *) die "port $PORT on $HOST is already in use by another program (see: ss -ltnp | grep :$PORT). $2" ;;
    esac
}

# runs queued / running in the UI on this port (0 when it does not answer)
active_runs() {   # $1 python
    "$1" - "$PORT" "$PREFIX/config/ui.env" <<'PYEOF' 2>/dev/null || echo 0
import json, re, sys, urllib.request
o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    req = urllib.request.Request(f"http://127.0.0.1:{sys.argv[1]}/api/runs")
    try:                                   # the UI's access token, if it has one
        m = re.search(r"^HELIOS_UI_TOKEN=(\S+)", open(sys.argv[2]).read(), re.M)
        if m:
            req.add_header("Authorization", "Bearer " + m.group(1).strip("'\""))
    except OSError:
        pass
    runs = json.load(o.open(req, timeout=3))
    print(sum(1 for r in runs if r.get("status") in ("queued", "running")))
except Exception:
    print(0)
PYEOF
}

health_check() {   # prints the version on success
    "$PREFIX/venv/bin/python" - "$PORT" <<'PYEOF'
import json, sys, time, urllib.request
port = sys.argv[1]
o = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 127.0.0.1 never through a proxy
deadline, last = time.time() + 45, None
while time.time() < deadline:
    try:
        with o.open(f"http://127.0.0.1:{port}/", timeout=3) as r:
            if r.status == 200:
                v = json.load(o.open(f"http://127.0.0.1:{port}/api/version", timeout=3))
                print(v.get("version", "?"))
                sys.exit(0)
            last = f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001
        last = e
    time.sleep(1)
print(f"the UI did not answer on 127.0.0.1:{port} within 45 s ({last})", file=sys.stderr)
sys.exit(1)
PYEOF
}

# "--host ADDR " when the code in app/ knows --host (a rolled-back version from before it does not)
host_arg() { if grep -q -- '"--host"' "$PREFIX/app/ui/__main__.py" 2>/dev/null; then printf -- '--host %s ' "$HOST"; fi; }

bundle_version() { "$1" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version","?"))' "$2" 2>/dev/null || echo "?"; }

write_state() {   # $1 mode, $2 version
    write_file "$STATE" <<EOF
# written by install.sh - read by install.sh, uninstall.sh and bin/helios-ui
INSTALLED_PORT=$PORT
INSTALLED_HOST=$HOST
INSTALLED_MODE=$1
INSTALLED_VERSION=$2
EOF
}

write_helper() {
    write_file "$PREFIX/bin/helios-ui" 755 <<EOF
#!/usr/bin/env bash
# helios-ui {start|stop|restart|status|logs|token [new]} - the helios-tests UI of $PREFIX (written by install.sh)
#   token       print the UI access token (give it to the people who use the UI)
#   token new   a new random token, then restart: every browser has to log in again with the new one
set -euo pipefail
PREFIX=$PREFIX
UNIT=$UNIT
. "\$PREFIX/.install-state"
PIDF="\$PREFIX/logs/helios-ui.pid"
LOG="\$PREFIX/logs/helios-ui.log"
CMD=\${1:-status}
ENVF="\$PREFIX/config/ui.env"
if [ "\$CMD" = token ]; then
    case "\${2:-show}" in
        show)
            if tok=\$(grep '^HELIOS_UI_TOKEN=' "\$ENVF" 2>/dev/null | tail -n 1 | cut -d= -f2-) && [ -n "\$tok" ]; then
                echo "\$tok"
            else
                echo "no access token set in \$ENVF (the UI has no login): \$0 token new creates one" >&2; exit 1
            fi ;;
        new)
            "\$PREFIX/venv/bin/python" - "\$ENVF" <<'PYEOF'
import os, re, secrets, sys
f = sys.argv[1]
try:
    text = open(f).read()
except FileNotFoundError:
    text = ""
tok = secrets.token_urlsafe(24)
line = "HELIOS_UI_TOKEN=" + tok
if re.search(r"^HELIOS_UI_TOKEN=.*$", text, re.M):
    text = re.sub(r"^HELIOS_UI_TOKEN=.*$", line, text, flags=re.M)
else:
    text += ("" if not text or text.endswith("\n") else "\n") + \
        "# access token of the UI (each browser logs in with it once; change it to end every login)\n" + line + "\n"
fd = os.open(f + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    fh.write(text)
os.replace(f + ".tmp", f)
os.chmod(f, 0o600)
PYEOF
            "\$0" restart >/dev/null || echo "WARNING: restart failed - run \$0 restart (the old token works until then)" >&2
            echo "new access token (every browser logs in again with it):"
            "\$0" token ;;
        *) echo "usage: \$0 token [new]" >&2; exit 2 ;;
    esac
    exit 0
fi
if [ "\$INSTALLED_MODE" = systemd ]; then
    case "\$CMD" in
        start|stop|restart|status) exec systemctl --user "\$CMD" "\$UNIT" ;;
        logs) exec tail -n 200 -F "\$LOG" ;;
    esac
else
    running() { [ -f "\$PIDF" ] && kill -0 "\$(cat "\$PIDF")" 2>/dev/null; }
    case "\$CMD" in
        start)
            if running; then echo "helios-ui already running (pid \$(cat "\$PIDF"))"; exit 0; fi
            cd "\$PREFIX/app"
            # a rolled-back app/ from before --host listens on 127.0.0.1 and takes no --host
            HOSTARG=""; grep -q -- '"--host"' ui/__main__.py && HOSTARG="--host \${INSTALLED_HOST:-0.0.0.0}"
            if [ -f "\$PREFIX/config/ui.env" ]; then set -a; . "\$PREFIX/config/ui.env"; set +a; fi
            HELIOS_RESULTS_ROOT="\$PREFIX/data/results" PYTHONUNBUFFERED=1 \\
                nohup "\$PREFIX/venv/bin/python" -m ui \$HOSTARG --port "\$INSTALLED_PORT" --config "\$PREFIX/config/racks.yaml" \\
                >> "\$LOG" 2>&1 < /dev/null &
            echo \$! > "\$PIDF"
            sleep 1
            if running; then echo "helios-ui started (pid \$(cat "\$PIDF")) on \${INSTALLED_HOST:-0.0.0.0}:\$INSTALLED_PORT"
            else rm -f "\$PIDF"; echo "helios-ui did not start; last lines of \$LOG:" >&2; tail -n 20 "\$LOG" >&2; exit 1; fi ;;
        stop)
            if running; then
                pid=\$(cat "\$PIDF"); kill -TERM "\$pid"
                for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do kill -0 "\$pid" 2>/dev/null || break; sleep 1; done
                kill -0 "\$pid" 2>/dev/null && kill -KILL "\$pid" 2>/dev/null || true
                echo "helios-ui stopped"
            else echo "helios-ui not running"; fi
            rm -f "\$PIDF" ;;
        restart) "\$0" stop; "\$0" start ;;
        status)
            if running; then echo "helios-ui running (pid \$(cat "\$PIDF")) on \${INSTALLED_HOST:-0.0.0.0}:\$INSTALLED_PORT"
            else echo "helios-ui not running"; exit 3; fi ;;
        logs) exec tail -n 200 -F "\$LOG" ;;
        *) echo "usage: \$0 {start|stop|restart|status|logs|token [new]}" >&2; exit 2 ;;
    esac
    exit 0
fi
echo "usage: \$0 {start|stop|restart|status|logs|token [new]}" >&2
exit 2
EOF
}

write_unit() {
    run mkdir -p "$HOME/.config/systemd/user"
    write_file "$HOME/.config/systemd/user/$UNIT" <<EOF
# helios-tests web UI of $PREFIX (written by install.sh; uninstall.sh removes it)
[Unit]
Description=helios-tests web UI (listening on $HOST:$PORT)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$PREFIX/app
Environment=PYTHONUNBUFFERED=1
Environment=HELIOS_RESULTS_ROOT=$PREFIX/data/results
# yours, kept by upgrades: e.g. HELIOS_UI_ALLOWED_HOSTS=alias1,alias2 (DNS aliases the page is opened with)
EnvironmentFile=-$PREFIX/config/ui.env
ExecStart=$PREFIX/venv/bin/python -m ui $(host_arg)--port $PORT --config $PREFIX/config/racks.yaml
Restart=on-failure
RestartSec=5
# exit 2 = the port is taken: restarting would not help
RestartPreventExitStatus=2
StandardOutput=append:$PREFIX/logs/helios-ui.log
StandardError=inherit
TimeoutStopSec=40

[Install]
WantedBy=default.target
EOF
}

# start the UI ($1 = mode) and check it answers
start_and_check() {
    if [ "$NO_START" = 1 ]; then
        say "not started (--no-start): $PREFIX/bin/helios-ui start"
        return 0
    fi
    if [ "$DRY" = 0 ]; then check_port "$PREFIX/venv/bin/python" "Install again with --port N"; fi
    if [ "$1" = systemd ]; then
        run systemctl --user daemon-reload
        run systemctl --user enable "$UNIT"
        run systemctl --user restart "$UNIT"
    else
        run "$PREFIX/bin/helios-ui" start
    fi
    if [ "$DRY" = 1 ]; then
        say "  [dry-run] health check: GET http://127.0.0.1:$PORT/ must answer 200"
        return 0
    fi
    step "health check"
    if v=$(health_check); then
        say "OK: http://127.0.0.1:$PORT/ answers (HTTP 200), version $v"
    else
        warn "the UI does not answer. Last lines of $PREFIX/logs/helios-ui.log:"
        tail -n 25 "$PREFIX/logs/helios-ui.log" >&2 || true
        [ -d "$PREFIX/app.prev" ] && warn "roll back with: $PREFIX/bin/install.sh --rollback"
        exit 1
    fi
}

is_loopback() { case "$HOST" in 127.0.0.1|::1|localhost) return 0 ;; esac; return 1; }

# firewalld / ufw active and the port not open: say what an admin would run (never changes the firewall)
firewall_advice() {
    is_loopback && return 0
    local sudo=""
    [ "$(id -u)" = 0 ] || sudo="sudo "
    if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        if firewall-cmd --query-port="$PORT/tcp" >/dev/null 2>&1; then
            say "firewalld: port $PORT/tcp is open"
        else
            say "firewalld is running and port $PORT/tcp is not open (or this user cannot query it). To open it to"
            say "the team's subnet only (an admin runs this; replace 10.0.0.0/24 with the team's subnet):"
            say "    ${sudo}firewall-cmd --permanent --add-rich-rule='rule family=\"ipv4\" source address=\"10.0.0.0/24\" port port=\"$PORT\" protocol=\"tcp\" accept' && ${sudo}firewall-cmd --reload"
        fi
    elif command -v ufw >/dev/null 2>&1 && { systemctl is-active --quiet ufw 2>/dev/null || ufw status 2>/dev/null | grep -q 'Status: active'; }; then
        if ufw status 2>/dev/null | grep -Eq "^$PORT(/tcp)? .*ALLOW"; then
            say "ufw: port $PORT/tcp is allowed"
        else
            say "ufw is active and port $PORT/tcp is not allowed (or this user cannot query it). To allow the"
            say "team's subnet only (an admin runs this; replace 10.0.0.0/24 with the team's subnet):"
            say "    ${sudo}ufw allow from 10.0.0.0/24 to any port $PORT proto tcp"
        fi
    fi
}

print_access() {
    local host ips
    host=$(hostname -f 2>/dev/null || hostname)
    if is_loopback; then
        say ""
        say "The UI listens on $HOST:$PORT only (this server). From your laptop:"
        say "    ssh -L $PORT:127.0.0.1:$PORT $(id -un)@$host"
        say "then open  http://127.0.0.1:$PORT/"
        say "(if $PORT is taken on the laptop: ssh -L 8780:127.0.0.1:$PORT ... and open http://127.0.0.1:8780/)"
    else
        say ""
        say "Open:   http://$host:$PORT/"
        if [ "$HOST" = 0.0.0.0 ] || [ "$HOST" = "::" ]; then
            ips=$( (hostname -I 2>/dev/null || true) | tr ' ' '\n' | grep -E '^[0-9.]+$' | grep -v '^127\.' | sed "s|.*|http://&:$PORT/|" | tr '\n' ' ' || true)
            [ -z "$ips" ] || say "        $ips"
        fi
        say "        (alternative, SSH tunnel: ssh -L $PORT:127.0.0.1:$PORT $(id -un)@$host, then http://127.0.0.1:$PORT/)"
        say ""
        if [ -n "$TOKEN_NEW" ]; then
            say "Access token (each browser logs in with it once; give it to the team):"
            say "    $TOKEN_NEW"
            say "  show it again: $PREFIX/bin/helios-ui token    new one (ends every login): $PREFIX/bin/helios-ui token new"
        elif grep -q '^HELIOS_UI_TOKEN=' "$PREFIX/config/ui.env" 2>/dev/null; then
            say "Access token: unchanged (show it: $PREFIX/bin/helios-ui token)"
        else
            say "WARNING: the UI has NO login. Anyone who can reach $host:$PORT can start runs on the racks (including"
            say "disruptive ones) and read every result. Install again without --no-token for an access token,"
            say "or with --host 127.0.0.1 (then only the SSH tunnel reaches it)."
        fi
        say "Restrict the port with the host firewall to the team's subnet as well."
        firewall_advice
    fi
    say ""
    say "Manage it:   $PREFIX/bin/helios-ui {start|stop|restart|status|logs|token [new]}"
    say "Upgrade:     run install.sh of a newer bundle (config/ and data/ are kept; so are --host and --port)"
    say "Roll back:   $PREFIX/bin/install.sh --rollback"
    say "Uninstall:   $PREFIX/bin/uninstall.sh   (keeps config/ and data/; --purge removes everything)"
}

linger_advice() {
    command -v loginctl >/dev/null 2>&1 || return 0
    if loginctl show-user "$(id -un)" -p Linger 2>/dev/null | grep -q '=yes'; then
        say "linger is enabled for $(id -un): the UI keeps running after you log out and starts at boot"
    else
        say "NOTE: without 'linger' systemd stops the UI when your last session ends. Ask an admin to run once:"
        say "    sudo loginctl enable-linger $(id -un)"
    fi
}

# ------------------------------------------------------------------ checks
step "checks"
if [ "$(uname -s)" != Linux ]; then
    if [ "${HELIOS_UI_INSTALL_ANY_OS:-0}" = 1 ]; then
        warn "not Linux ($(uname -s)): continuing because HELIOS_UI_INSTALL_ANY_OS=1 (for testing the installer)"
    else
        die "this installer is for Linux (this is $(uname -s))"
    fi
fi

if [ "$ROLLBACK" = 1 ]; then
    [ -d "$PREFIX/app.prev" ] && [ -d "$PREFIX/venv.prev" ] || die "nothing to roll back to: no $PREFIX/app.prev + venv.prev"
    MODE=${INSTALLED_MODE:-nohup}
    cur=$(bundle_version "$PREFIX/venv/bin/python" "$PREFIX/app/BUILD_INFO.json")
    prev=$(bundle_version "$PREFIX/venv.prev/bin/python" "$PREFIX/app.prev/BUILD_INFO.json")
    step "rollback $cur -> $prev"
    stop_ui
    for d in app venv; do
        run mv "$PREFIX/$d" "$PREFIX/$d.rollback-tmp"
        run mv "$PREFIX/$d.prev" "$PREFIX/$d"
        run mv "$PREFIX/$d.rollback-tmp" "$PREFIX/$d.prev"
    done
    write_state "$MODE" "$prev"
    [ "$MODE" != systemd ] || write_unit          # ExecStart matching the rolled-back code (--host or not)
    start_and_check "$MODE"
    say "rolled back to $prev ($cur is now app.prev: run --rollback again to return to it)"
    print_access
    exit 0
fi

[ -d "$HERE/app/ui" ] && [ -f "$HERE/app/BUILD_INFO.json" ] && [ -f "$HERE/requirements.txt" ] \
    || die "run install.sh from the unpacked bundle (helios-ui-<version>/install.sh); app/ or requirements.txt missing"
if [ "$ONLINE" = 0 ]; then
    [ -d "$HERE/wheelhouse" ] || die "no wheelhouse/ in the bundle: use --online (needs PyPI access)"
fi
SUPPORTED=$(cat "$HERE/wheelhouse/PYTHONS" 2>/dev/null || echo "")

py_ok() {   # $1: >= MIN_PY, and (online or the wheelhouse has its version)
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null || return 1
    [ "$ONLINE" = 1 ] && return 0
    local v; v=$("$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    case " $SUPPORTED " in *" $v "*) return 0 ;; esac
    return 1
}
if [ -n "$PYTHON" ]; then
    command -v "$PYTHON" >/dev/null 2>&1 || die "--python $PYTHON: not found"
    "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "--python $PYTHON is $("$PYTHON" -V 2>&1); Python >= $MIN_PY is needed"
    py_ok "$PYTHON" || die "--python $PYTHON ($("$PYTHON" -V 2>&1)): the bundle has wheels for Python $SUPPORTED only; use one of those or --online"
else
    for c in python3 python3.12 python3.11 python3.10; do
        if command -v "$c" >/dev/null 2>&1 && py_ok "$c"; then PYTHON=$(command -v "$c"); break; fi
    done
    [ -n "$PYTHON" ] || die "no Python >= $MIN_PY found$([ "$ONLINE" = 0 ] && echo " with wheels in this bundle ($SUPPORTED)"). Install python3 (>= $MIN_PY), or give --python /path/to/python3.X"
fi
PYV=$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
say "python: $PYTHON ($PYV)"
if ! "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    PYMM=${PYV%.*}
    die "the Python venv module is not available for $PYTHON. Install it (needs an admin), e.g.:
    sudo apt install python3-venv        (or python$PYMM-venv)   Debian / Ubuntu
    sudo dnf install python3             (venv is included)       RHEL / Rocky / Fedora"
fi

# free space: the venv + code take ~2-3x the bundle; at least 250 MB
mkdir_parent=$PREFIX
while [ ! -d "$mkdir_parent" ]; do mkdir_parent=$(dirname "$mkdir_parent"); done
need_kb=$(( $(du -sk "$HERE" | awk '{print $1}') * 3 ))
[ "$need_kb" -ge 256000 ] || need_kb=256000
free_kb=$(df -Pk "$mkdir_parent" | awk 'NR==2 {print $4}')
[ "$free_kb" -ge "$need_kb" ] || die "not enough free space in $mkdir_parent: $((free_kb / 1024)) MB free, $((need_kb / 1024)) MB needed"
say "disk: $((free_kb / 1024)) MB free in $mkdir_parent (need ~$((need_kb / 1024)) MB)"

NEW_VERSION=$(bundle_version "$PYTHON" "$HERE/app/BUILD_INFO.json")
if systemd_user_ok; then MODE=systemd; else MODE=nohup; fi
say "bundle: $NEW_VERSION   prefix: $PREFIX   listen: $HOST:$PORT   start: $MODE   pip: $([ "$ONLINE" = 1 ] && echo online || echo "offline (wheelhouse)")"

UPGRADE=0
OLD_VERSION=""
if [ -d "$PREFIX/app" ]; then
    UPGRADE=1
    OLD_VERSION=$(bundle_version "$PYTHON" "$PREFIX/app/BUILD_INFO.json")
    say "existing installation: $OLD_VERSION -> upgrade to $NEW_VERSION (config/ and data/ are kept)"
    if [ -x "$PREFIX/venv/bin/python" ]; then
        n=$(active_runs "$PREFIX/venv/bin/python")
        if [ "${n:-0}" != 0 ]; then
            if [ "$FORCE" = 1 ]; then warn "$n run(s) queued / running in the UI: they are stopped by the upgrade (--force)"
            else die "$n run(s) queued / running in the UI on port $PORT: an upgrade would stop them. Wait, or use --force"; fi
        fi
    fi
fi

# ------------------------------------------------------------------ install
step "stop the running UI (if any)"
stop_ui
if [ "$DRY" = 0 ] && [ "$NO_START" = 0 ]; then check_port "$PYTHON" "Use --port N"; fi

if [ "$UPGRADE" = 1 ] || [ -d "$PREFIX/venv" ]; then
    step "keep the current code as app.prev / venv.prev (one rollback)"
    for d in app venv; do
        if [ -d "$PREFIX/$d" ]; then
            run rm -rf "$PREFIX/$d.prev"
            run mv "$PREFIX/$d" "$PREFIX/$d.prev"
        fi
    done
fi
restore_prev() {
    warn "install failed: restoring the previous app/ and venv/"
    for d in app venv; do
        if [ -d "$PREFIX/$d.prev" ]; then rm -rf "${PREFIX:?}/$d"; mv "$PREFIX/$d.prev" "$PREFIX/$d"; fi
    done
}
# any failure from here until the start: put the previous version back (the ERR trap misses die / exit)
if [ "$DRY" = 0 ] && [ "$UPGRADE" = 1 ]; then trap 'rc=$?; [ "$rc" = 0 ] || restore_prev' EXIT; fi

step "directories"
for d in bin config logs data/results/runs data/inventories data/testplan; do
    run mkdir -p "$PREFIX/$d"
done

step "code -> $PREFIX/app"
run mkdir -p "$PREFIX/app"
if [ "$DRY" = 1 ]; then say "  [dry-run] copy $HERE/app/. -> $PREFIX/app/"; else (cd "$HERE/app" && tar -cf - .) | (cd "$PREFIX/app" && tar -xf -); fi

step "venv ($PYTHON) + dependencies"
run "$PYTHON" -m venv "$PREFIX/venv"
PIP="$PREFIX/venv/bin/python -m pip --disable-pip-version-check --no-input"
if [ "$ONLINE" = 1 ]; then
    # shellcheck disable=SC2086
    run $PIP install -q -r "$HERE/requirements.txt"
else
    # shellcheck disable=SC2086
    run $PIP install -q --no-index --find-links "$HERE/wheelhouse" -r "$HERE/requirements.txt"
fi
# the code is importable from anywhere (tools/*.py, pytest) without installing it as a package
if [ "$DRY" = 1 ]; then
    say "  [dry-run] write <venv site-packages>/helios-ui-app.pth -> $PREFIX/app"
else
    SITE=$("$PREFIX/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    printf '%s\n' "$PREFIX/app" > "$SITE/helios-ui-app.pth"
    "$PREFIX/venv/bin/python" -c 'import yaml, paramiko, requests, websocket, openpyxl, pytest' \
        || die "dependencies missing in the venv"
    say "dependencies OK; $(cd "$PREFIX/app" && "$PREFIX/venv/bin/python" -m ui --version)"
fi

step "config"
if [ -f "$PREFIX/config/racks.yaml" ]; then
    say "kept $PREFIX/config/racks.yaml (yours; the starter profile of this bundle: $PREFIX/app/tools/installer/racks.yaml.template)"
else
    sed "s|@PREFIX@|$PREFIX|g" "$HERE/app/tools/installer/racks.yaml.template" | write_file "$PREFIX/config/racks.yaml"
    say "wrote the starter $PREFIX/config/racks.yaml"
fi
if [ "$DRY" = 0 ]; then
    (cd "$PREFIX/app" && "$PREFIX/venv/bin/python" - "$PREFIX/config/racks.yaml" <<'PYEOF'
import sys
from ui.config import load_config
for n, r in load_config(sys.argv[1]).items():
    state = "OK     " if r.inventory.is_file() else "MISSING"
    print("  %-10s inventory %s %s" % (n, state, r.inventory))
    if r.workbook and not r.workbook.is_file():
        print("  %-10s workbook  MISSING %s" % ("", r.workbook))
PYEOF
    ) || warn "$PREFIX/config/racks.yaml does not load: fix it, then $PREFIX/bin/helios-ui restart"
fi

step "access token"
ENVF="$PREFIX/config/ui.env"
if is_loopback; then
    say "listening on $HOST only (SSH tunnel): no access token needed"
elif [ "$NO_TOKEN" = 1 ]; then
    say "--no-token: the UI on $HOST:$PORT has no login"
    if grep -q '^HELIOS_UI_TOKEN=' "$ENVF" 2>/dev/null; then warn "but $ENVF still sets HELIOS_UI_TOKEN: remove that line for no login"; fi
elif grep -q '^HELIOS_UI_TOKEN=' "$ENVF" 2>/dev/null; then
    say "kept the access token in $ENVF (HELIOS_UI_TOKEN)"
elif [ "$DRY" = 1 ]; then
    say "  [dry-run] generate an access token -> HELIOS_UI_TOKEN in $ENVF (mode 600)"
    TOKEN_NEW="<generated at install>"
else
    TOKEN_NEW=$("$PREFIX/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')
    ( umask 077; [ -f "$ENVF" ] || : > "$ENVF"
      printf '# access token of the UI (each browser logs in with it once): show it with bin/helios-ui token,\n# a new one (ends every login) with bin/helios-ui token new\nHELIOS_UI_TOKEN=%s\n# who to ask for it, shown on the login page (default: the user running the UI)\n# HELIOS_UI_ADMIN=Name / team channel\n' "$TOKEN_NEW" >> "$ENVF" )
    chmod 600 "$ENVF"
    say "generated an access token -> $ENVF (HELIOS_UI_TOKEN, mode 600)"
fi

step "helper scripts"
run cp "$HERE/install.sh" "$PREFIX/bin/install.sh"
run cp "$HERE/uninstall.sh" "$PREFIX/bin/uninstall.sh"
run chmod 755 "$PREFIX/bin/install.sh" "$PREFIX/bin/uninstall.sh"
run cp "$HERE/README-INSTALL.txt" "$PREFIX/README-INSTALL.txt"
write_state "$MODE" "$NEW_VERSION"
write_helper

if [ "$MODE" = systemd ]; then
    step "systemd --user service $UNIT"
    write_unit
else
    step "start without systemd"
    say "systemd --user is not available here: the UI is started with nohup by the helper"
    say "  $PREFIX/bin/helios-ui {start|stop|restart|status|logs}"
    say "  (same as: cd $PREFIX/app && HELIOS_RESULTS_ROOT=$PREFIX/data/results nohup $PREFIX/venv/bin/python -m ui --host $HOST --port $PORT --config $PREFIX/config/racks.yaml >> $PREFIX/logs/helios-ui.log 2>&1 &)"
    say "  it does not start again after a reboot by itself: run the helper's start (e.g. from crontab: @reboot $PREFIX/bin/helios-ui start)"
    if service_installed; then
        warn "a $UNIT from an earlier install exists but systemd --user is not usable now; leaving it"
    fi
fi

trap - EXIT
start_and_check "$MODE"
[ "$MODE" = systemd ] && linger_advice

if [ "$UPGRADE" = 1 ]; then
    step "upgrade summary"
    say "version: $OLD_VERSION -> $NEW_VERSION"
    if [ "$DRY" = 0 ] && [ -d "$PREFIX/app.prev" ]; then
        changed=$(diff -rq -x __pycache__ -x .pytest_cache "$PREFIX/app.prev" "$PREFIX/app" 2>/dev/null | grep -c '^Files ' || true)
        added=$(diff -rq -x __pycache__ -x .pytest_cache "$PREFIX/app.prev" "$PREFIX/app" 2>/dev/null | grep -c "^Only in $PREFIX/app[:/]" || true)
        removed=$(diff -rq -x __pycache__ -x .pytest_cache "$PREFIX/app.prev" "$PREFIX/app" 2>/dev/null | grep -c "^Only in $PREFIX/app.prev" || true)
        say "code: $changed file(s) changed, $added added, $removed removed (diff -rq $PREFIX/app.prev $PREFIX/app)"
    fi
    say "kept: $PREFIX/config/ (rack profiles) and $PREFIX/data/ (results, sessions, presets, defaults, inventories, workbook)"
    say "previous version kept as app.prev / venv.prev: $PREFIX/bin/install.sh --rollback"
else
    cat <<EOF

Next: copy what the racks need (nothing secret is copied by this installer):
    $PREFIX/data/inventories/mr3.local.yaml                 (MR3 inventory)
    $PREFIX/data/inventories/fullrack-jump-afm.local.yaml   (g02-g17: copy of the jump host's ~/helios/fullrack-jump-afm.yaml)
    $PREFIX/data/testplan/full_rack_testplan.xlsx           (test-plan workbook)
    SSH keys in ~/.ssh of $(id -un): the jump host (tenant-hpe@165.204.38.103 -p 8059) and the MR3 nodes, usable
    without a prompt (the service has no ssh-agent). Profiles: $PREFIX/config/racks.yaml
AFM password: type it in the Run form; 'Remember' saves it per rack in ~/.config/helios-ui/secrets.json of
    $(id -un) (mode 600, clear text: as safe as that account; \$HELIOS_UI_SECRETS in config/ui.env moves it).
EOF
fi
print_access
