#!/usr/bin/env bash
# uninstall.sh - remove the helios-tests web UI of the current user.
#
#   ~/helios-ui/bin/uninstall.sh            stop + disable the service, remove app/ venv/ (and .prev) bin/;
#                                           KEEP config/ and data/ (results, inventories, workbook) and logs/
#   ~/helios-ui/bin/uninstall.sh --purge    move the whole ~/helios-ui aside to ~/helios-ui.removed-<time>
#                                           (asks first; --yes does not ask): a later install.sh takes its
#                                           config/ and data/ back (install.sh --fresh does not)
#   ~/helios-ui/bin/uninstall.sh --purge --delete   really delete it (results, inventories, token: gone)
#
# Options: --prefix DIR (default: the installation this script is in, else ~/helios-ui)  --purge  --delete  --yes  --dry-run
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
UNIT=helios-ui.service
PURGE=0
DELETE=0
YES=0
DRY=0
PREFIX=""
if [ -f "$HERE/../.install-state" ]; then PREFIX=$(cd "$HERE/.." && pwd); fi

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run() { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)   [ $# -ge 2 ] || die "--prefix needs a directory"; PREFIX=$2; shift 2 ;;
        --prefix=*) PREFIX=${1#*=}; shift ;;
        --purge)    PURGE=1; shift ;;
        --delete)   DELETE=1; shift ;;
        --yes|-y)   YES=1; shift ;;
        --dry-run)  DRY=1; shift ;;
        -h|--help)  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)          die "unknown option: $1" ;;
    esac
done
[ -n "$PREFIX" ] || PREFIX="$HOME/helios-ui"
case "$PREFIX" in "~"|"~/"*) PREFIX="$HOME${PREFIX#\~}" ;; esac
PREFIX=${PREFIX%/}
[ -d "$PREFIX" ] || die "$PREFIX does not exist"
[ -f "$PREFIX/.install-state" ] || [ -d "$PREFIX/app" ] || die "$PREFIX is not a helios-ui installation (no .install-state, no app/)"
[ "$PREFIX" != "$HOME" ] && [ "$PREFIX" != "/" ] || die "refusing to touch $PREFIX"

if [ "$PURGE" = 1 ] && [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then
    if [ "$DELETE" = 1 ]; then
        printf 'DELETE ALL of %s, including config/ and data/ (results, inventories, workbook, access token)? [y/N] ' "$PREFIX"
    else
        printf 'Remove %s (moved aside to %s.removed-<time>; a later install takes its config/ and data/ back)? [y/N] ' "$PREFIX" "$PREFIX"
    fi
    read -r ans || ans=""
    case "$ans" in y|Y|yes|YES) ;; *) say "nothing changed"; exit 1 ;; esac
fi
say "== uninstall the helios-tests UI of $PREFIX"
UNITF="$HOME/.config/systemd/user/$UNIT"
if [ -f "$UNITF" ] && grep -q "$PREFIX/app" "$UNITF"; then
    if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        run systemctl --user disable --now "$UNIT" || true
    fi
    run rm -f "$UNITF"
    if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        run systemctl --user daemon-reload || true
    fi
    say "service $UNIT stopped and removed"
fi
if [ -x "$PREFIX/bin/helios-ui" ] && [ -f "$PREFIX/logs/helios-ui.pid" ]; then
    run "$PREFIX/bin/helios-ui" stop || true
fi

if [ "$PURGE" = 1 ]; then
    if [ "$DELETE" = 1 ]; then
        run rm -rf "$PREFIX"
        say "deleted $PREFIX (the settings backups in ~/.helios-ui-backups/ are kept)"
    else
        aside="$PREFIX.removed-$(date +%Y%m%d-%H%M%S)"
        run mv "$PREFIX" "$aside"
        say "moved $PREFIX to $aside - a later install.sh takes its config/ and data/ back;"
        say "delete it for good with: rm -rf $aside"
    fi
    exit 0
fi

for d in app app.prev venv venv.prev bin; do
    if [ -e "$PREFIX/$d" ]; then run rm -rf "${PREFIX:?}/$d"; say "removed $PREFIX/$d"; fi
done
run rm -f "$PREFIX/.install-state" "$PREFIX/README-INSTALL.txt"
say "kept: $PREFIX/config/ $PREFIX/data/ $PREFIX/logs/  (a later install.sh uses them again; --purge removes them)"
