#!/bin/bash
# Install the units that keep data and readings current. Idempotent: renders the templates,
# verifies them, installs a file only when it differs, reloads only on change, and
# `enable --now` of a timer already enabled changes nothing. `--Dry-run` renders and verifies only and touches nothing.
#
#  user units (no sudo), ~/.config/systemd/user:
#     plx-sheet-readings.timer   daily 02:40 ET   sheet readings (alert rules' not_set_up -> data)
#     plx-system-activity.timer  daily 02:50 ET   per-system activity snapshot, 13 weeks
#     plx-team-activity.timer    daily 03:30 ET   Team view snapshot (people, days, systems)
#     plx-work-graph.timer       Sunday 03:30 ET  role owners from what people did, 13 weeks
#   system units (sudo -n), /etc/systemd/system:
#     plx-proactive-whole.timer  every 4 h        whole-store proactive rules (not in after_build.sh)
#     plx-backup.timer           03:15 ET         nightly backup set
#     plx-restore-test.timer     04:30 ET         restore test of the newest set
#     plx-ops-health.timer       every 5 min      reliability alarms (operating/ops_alarms.py): in-app banner + /ops
#     plx-ops-memory.timer       every 1 min      memory early warning (under 8 GB, top consumers named)
#     plx-expiry.timer           daily 06:20 UTC  credential expiry list (alarm within 30 days)
#     plx-autopilot-follow.timer 5 min after the last cycle, and .path on each delta publish:
#                                the autopilot's live feed; prepares My work items only (never
#                                sends, writes or approves), ordered event queue on (EVQ=1)
#     plx-autopilot-fast.timer   1 min after the last start, and .path on each captured Teams or
#                                mail message: the fast feed (operating/autopilot_fast.py), the
#                                same guards; prepares My work items only
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
USER_UNITS="plx-sheet-readings plx-system-activity plx-team-activity plx-work-graph"
SYS_UNITS="plx-proactive-whole plx-backup plx-restore-test plx-ops-health plx-ops-memory plx-expiry plx-autopilot-follow plx-autopilot-fast"
SYS_PATHS="plx-autopilot-follow plx-autopilot-fast"   # also started by a .path unit

python3 "$render" --user --out "$tmp/user" >/dev/null
python3 "$render" --out "$tmp/sys" >/dev/null
# systemd-analyze verify: a non-zero exit is a failure unless every line it printed is a
# documented harmless diagnostic (a binary of the release not installed yet, the plx-suite
# unit of the nightly run, owned by another installer). Nothing is copied on any other error.
NOISE='not executable|plx-suite'
verify_unit() {  # verify_unit [--user] service timer
  local out rc=0
  out=$(systemd-analyze verify "$@" 2>&1) || rc=$?
  local rest; rest=$(printf '%s\n' "$out" | grep -v -E "$NOISE" | grep -v -E '^[[:space:]]*$' || true)
  if [ -n "$rest" ] || { [ $rc -ne 0 ] && [ -z "$out" ]; }; then
    printf 'systemd-analyze verify failed (exit %s) for %s:\n%s\n' "$rc" "$*" "${rest:-$out}" >&2
    exit 1
  fi
}
for u in $USER_UNITS; do verify_unit --user "$tmp/user/$u.service" "$tmp/user/$u.timer"; done
for u in $SYS_UNITS; do verify_unit "$tmp/sys/$u.service" "$tmp/sys/$u.timer"; done
for u in $SYS_PATHS; do verify_unit "$tmp/sys/$u.service" "$tmp/sys/$u.path"; done
for f in "$tmp"/user/plx-{sheet-readings,system-activity,team-activity,work-graph}.* "$tmp"/sys/plx-{proactive-whole,backup,restore-test,ops-health,ops-memory,expiry,autopilot-follow,autopilot-fast}.*; do
  [[ $f == *.timer || $f == *.path ]] && continue
  grep -q "^MemoryMax=" "$f" || { echo "no MemoryMax: $f" >&2; exit 1; }
  grep -q "^Nice=" "$f" || { echo "no Nice: $f" >&2; exit 1; }
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
for u in $SYS_PATHS; do
  dst=/etc/systemd/system/$u.path
  cmp -s "$tmp/sys/$u.path" "$dst" || { sudo -n install -m 644 "$tmp/sys/$u.path" "$dst"; schanged=1; }
done
[ $schanged = 0 ] || sudo -n systemctl daemon-reload
for u in $SYS_UNITS; do sudo -n systemctl enable --now "$u.timer"; done
for u in $SYS_PATHS; do sudo -n systemctl enable --now "$u.path"; done

ln -sfn "$PLX_REPO/deploy/release/plx-release" "$HOME/.local/bin/plx-release"

# the Team view reads its snapshot from the API's data directory, else PLX_TEAM_ACTIVITY_FILE;
# who sees everyone and who is hidden are PLX_TEAM_VIEW_READERS and PLX_TEAM_HIDDEN_PEOPLE.
for v in PLX_TEAM_ACTIVITY_FILE PLX_TEAM_VIEW_READERS PLX_TEAM_HIDDEN_PEOPLE; do
  grep -qs "^$v=" /etc/plx/workbench-api.env 2>/dev/null \
    || echo "note: set $v in the API's environment (see operator_api/team_view.py)"
done
[ -s "$PLX_STATE/autopilot/autopilot-gate.json" ] \
  || echo "note: no $PLX_STATE/autopilot/autopilot-gate.json: the live feed prepares only the Draft people's workflows (operating/autopilot_draft.json) until a passing replay's gate file is copied there"
echo "note: the work-graph unit writes $PLX_STATE/work-graph/roles-derived.json (candidate roles); the autopilot reads it only if PLX_AUTOPILOT_ROLES_FILE names it"
systemctl list-timers --all --no-legend 'plx-sheet*' 'plx-system*' 'plx-team*' 'plx-work*' 'plx-proactive-whole*' 'plx-backup*' 'plx-restore*' 'plx-ops*' 'plx-autopilot*' || true
