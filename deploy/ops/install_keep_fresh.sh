#!/bin/bash
# Install the units that keep data and readings current. Idempotent: renders the templates,
# verifies them, installs a file only when it differs, reloads only on change, and
# `enable --now` of a timer already enabled changes nothing. `--Dry-run` renders and verifies only and touches nothing.
#
#  user units (no sudo), ~/.config/systemd/user:
#     plx-sheet-readings.timer   daily 02:40 ET   sheet readings (alert rules' not_set_up -> data)
#     plx-system-activity.timer  Sunday 03:00 ET  per-system activity snapshot, 13 weeks
#     plx-work-graph.timer       Sunday 03:30 ET  role owners from what people did, 13 weeks
#   system units (sudo -n), /etc/systemd/system:
#     plx-proactive-whole.timer  every 4 h        whole-store proactive rules (not in after_build.sh)
#     plx-backup.timer           03:15 ET         nightly backup set
#     plx-restore-test.timer     04:30 ET         restore test of the newest set
#     plx-ops-health.timer       every 5 min      reliability alarms (operating/ops_alarms.py): page + Home banner
#     plx-ops-memory.timer       every 1 min      memory early warning (under 8 GB, top consumers named)
#   and ~/.local/bin/plx-release -> $PLX_REPO/deploy/release/plx-release
#
#Nothing here writes to a business system or reads a secret; the units name env files, not
# values. Needs ~/.config/plx/deploy.env (deploy/host.env.example) and a release at
# $PLX_RELEASES/current for the ExecStart paths to resolve.
set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
. "$here/../lib.sh"
dry=0; [ "${1:-}" = "--dry-run" ] && dry=1
render=$here/../render.py
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
USER_UNITS="plx-sheet-readings plx-system-activity plx-work-graph"
SYS_UNITS="plx-proactive-whole plx-backup plx-restore-test plx-ops-health plx-ops-memory"

python3 "$render" --user --out "$tmp/user" >/dev/null
python3 "$render" --out "$tmp/sys" >/dev/null
for u in $USER_UNITS; do systemd-analyze verify --user "$tmp/user/$u.service" "$tmp/user/$u.timer" 2>&1 \
  | grep -v -E "not executable|plx-suite" || true; done
for u in $SYS_UNITS; do systemd-analyze verify "$tmp/sys/$u.service" "$tmp/sys/$u.timer" 2>&1 \
  | grep -v -E "not executable|plx-suite" || true; done
for f in "$tmp"/user/plx-{sheet-readings,system-activity,work-graph}.* "$tmp"/sys/plx-{proactive-whole,backup,restore-test,ops-health,ops-memory}.*; do
  grep -q "^MemoryMax=" "$f" || [[ $f == *.timer ]] || { echo "no MemoryMax: $f" >&2; exit 1; }
  grep -q "^Nice=" "$f" || [[ $f == *.timer ]] || { echo "no Nice: $f" >&2; exit 1; }
done
[ $dry = 1 ] && { echo "dry run: rendered and verified, nothing installed"; exit 0; }

mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/bin" "$PLX_STATE/work-graph"
changed=0
for u in $USER_UNITS; do for ext in service timer; do
  dst=$HOME/.config/systemd/user/$u.$ext
  cmp -s "$tmp/user/$u.$ext" "$dst" || { install -m 644 "$tmp/user/$u.$ext" "$dst"; changed=1; }
done; done
[ $changed = 0 ] || systemctl --user daemon-reload
for u in $USER_UNITS; do systemctl --user enable --now "$u.timer"; done

schanged=0
for u in $SYS_UNITS; do for ext in service timer; do
  dst=/etc/systemd/system/$u.$ext
  cmp -s "$tmp/sys/$u.$ext" "$dst" || { sudo -n install -m 644 "$tmp/sys/$u.$ext" "$dst"; schanged=1; }
done; done
[ $schanged = 0 ] || sudo -n systemctl daemon-reload
for u in $SYS_UNITS; do sudo -n systemctl enable --now "$u.timer"; done

ln -sfn "$PLX_REPO/deploy/release/plx-release" "$HOME/.local/bin/plx-release"

# the owner workload view reads the activity snapshot from the API's data directory, else
# PLX_SYSTEM_ACTIVITY_FILE: point it at what the weekly unit writes.
grep -qs "PLX_SYSTEM_ACTIVITY_FILE" /etc/plx/workbench-api.env 2>/dev/null \
  || echo "note: set PLX_SYSTEM_ACTIVITY_FILE=$PLX_STATE/system-activity.json in the API's environment"
echo "note: the work-graph unit writes $PLX_STATE/work-graph/roles-derived.json (candidate roles); the autopilot reads it only if PLX_AUTOPILOT_ROLES_FILE names it"
systemctl list-timers --all --no-legend 'plx-sheet*' 'plx-system*' 'plx-work*' 'plx-proactive-whole*' 'plx-backup*' 'plx-restore*' 'plx-ops*' || true
