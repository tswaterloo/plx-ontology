#!/bin/bash
# Install the units that keep data and readings current. Idempotent: renders the templates,
# verifies them, installs a file only when it differs, reloads only on change, and
# `enable --now` of a timer already enabled changes nothing. `--Dry-run` renders and verifies only and touches nothing.
#
#  system units (sudo -n), /etc/systemd/system; the first four run as the Workbench user
#   with its groups from /etc/group at each start (user units until 11-15: a user manager
#   started before that user joined plx-state ran them without it and they could not open
#   the capture stores the API user shares; the old user units are removed here):
#     plx-sheet-readings.timer   daily 02:40 ET   sheet readings (alert rules' not_set_up -> data)
#     plx-system-activity.timer  daily 02:50 ET   per-system activity snapshot, 13 weeks
#     plx-team-activity.timer    daily 03:30 ET   Team view snapshot (people, days, systems)
#     plx-work-graph.timer       Sunday 03:30 ET  role owners from what people did, 13 weeks
#     plx-proactive-whole.timer  every 4 h        whole-store proactive rules (not in after_build.sh)
#     plx-backup.timer           03:15 ET         nightly backup set
#     plx-restore-test.timer     04:30 ET         restore test of the newest set
#     plx-ops-health.timer       every 5 min      reliability alarms (operating/ops_alarms.py): in-app banner + /ops
#     plx-ops-memory.timer       every 1 min      memory early warning (under 8 GB, top consumers named)
#     plx-ask-canary.timer       every 20 min     live Ask canary as its own service session
#                                (question: /etc/plx/ask-canary.json, installed from
#                                deploy/ops/ask-canary.json when missing)
#     plx-expiry.timer           daily 06:20 UTC  credential expiry list (alarm within 30 days)
#     plx-answer-bank.timer      10 min after the last build: the questionnaire answer bank for the
#                                company (Templates), decided per reader at read; capture read-only
#     plx-link-marks.timer       10 min after the last run: ontology.link_marks rebuilt when stale
#                                (the delta units and the API start no build of their own)
#     plx-qbank-refresh.timer    daily 02:30 ET   Ask question bank: every item's truth re-read from the served
#                                data (evals/qbank; plx_eval, read-only; writes only QBANK_DIR)
#     plx-labour.timer           daily 05:40 ET   labour-hours build (department and week totals; capture read-only)
#     plx-doc-style.timer        daily 04:50 ET   each person's document templates re-learned from what they
#                                sent (Templates "learned from your documents"; capture read-only)
#     plx-learning.timer         hourly :17       what each person's replies and edits of drafts teach their
#                                next Autopilot drafts (operating/learning.py; stores read-only)
#     plx-spark-health.timer     every 10 min     the Sparks' health for the worker that sends suites and
#                                embedding passes there (operating/spark_worker.py; local fallback)
#     plx-autopilot-follow.timer 5 min after the last cycle, and .path on each delta publish:
#                                the autopilot's live feed; prepares My work items only (never
#                                sends, writes or approves), ordered event queue on (EVQ=1)
#     plx-autopilot-fast-serve   continuous: the fast feed (operating/autopilot_fast.py) as one
#                                consumer, a captured message read within 1 s, the same
#                                guards; prepares My work items only. While it is down
#                                plx-autopilot-follow is its backstop. Its old per-signal
#                                plx-autopilot-fast .path/.timer (RETIRED_SYS_UNITS) are removed:
#                                in a day they started 2855 times, 2837 found the consumer
#                                running and the other 18 prepared nothing.
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
MOVED_USER_UNITS="plx-sheet-readings plx-system-activity plx-team-activity plx-work-graph"
SYS_UNITS="plx-sheet-readings plx-system-activity plx-team-activity plx-work-graph plx-proactive-whole plx-backup plx-restore-test plx-ops-health plx-ops-memory plx-expiry plx-autopilot-follow plx-reply-drafts plx-labour plx-doc-style plx-learning plx-write-readiness plx-spark-health plx-ask-canary plx-exclusion-leak plx-bare-mentions plx-own-issued plx-people-excluded plx-answer-bank plx-business-views plx-alerts plx-comm-text plx-assist-readings plx-assist-worker plx-assist-precedents plx-assist-plan plx-source-coverage plx-qbank-refresh plx-link-marks"
SYS_PATHS="plx-autopilot-follow plx-assist-readings-fresh"                 # also started by a .path unit
RETIRED_SYS_UNITS="plx-autopilot-fast plx-comms-findings"               # no longer shipped: stopped and removed
SYS_DAEMONS="plx-autopilot-fast-serve"                 # a service that runs continuously
SYS_AFTER="plx-comms-indexes plx-daily-changes"                                           # started only by another unit (OnSuccess=)

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
for u in $SYS_UNITS; do verify_unit "$tmp/sys/$u.service" "$tmp/sys/$u.timer"; done
for u in $SYS_PATHS; do verify_unit "$tmp/sys/$u.service" "$tmp/sys/$u.path"; done
for u in $SYS_DAEMONS $SYS_AFTER; do verify_unit "$tmp/sys/$u.service"; done
for f in "$tmp"/sys/plx-{sheet-readings,system-activity,team-activity,work-graph,proactive-whole,backup,restore-test,ops-health,ops-memory,expiry,autopilot-follow,autopilot-fast-serve,reply-drafts,labour,write-readiness,spark-health}.*; do
  # <unit>.d/ drop-in folders (deploy/systemd/host) are installed by deploy/ops/host/install.py,
  # not here; the unit file itself must carry MemoryMax and Nice
  [ -d "$f" ] && continue
  [[ $f == *.timer || $f == *.path ]] && continue
  grep -q "^MemoryMax=" "$f" || { echo "no MemoryMax: $f" >&2; exit 1; }
  grep -q "^Nice=" "$f" || { echo "no Nice: $f" >&2; exit 1; }
done
[ $dry = 1 ] && { echo "dry run: rendered and verified, nothing installed"; exit 0; }

mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/bin" "$PLX_STATE/work-graph" "$PLX_STATE/labour" "$PLX_STATE/learning"
# these were user units until 11-15 (now system units): the old ones go (the user manager
# itself is only reloaded, never restarted: it runs other jobs)
changed=0
for u in $MOVED_USER_UNITS; do
  old=$HOME/.config/systemd/user/$u
  if [ -e "$old.timer" ] || [ -e "$old.service" ] || [ -d "$old.service.d" ]; then
    systemctl --user disable --now "$u.timer" 2>/dev/null || true
    rm -rf "$old.timer" "$old.service" "$old.service.d"; changed=1
    systemctl --user reset-failed "$u.service" 2>/dev/null || true
  fi
done
[ $changed = 0 ] || systemctl --user daemon-reload

# system units no longer shipped: stopped, disabled and removed (their files only)
schanged=0
for u in $RETIRED_SYS_UNITS; do
  if ls /etc/systemd/system/"$u".{service,timer,path} >/dev/null 2>&1; then
    sudo -n systemctl disable --now "$u.timer" "$u.path" 2>/dev/null || true
    sudo -n rm -f /etc/systemd/system/"$u".service /etc/systemd/system/"$u".timer /etc/systemd/system/"$u".path
    sudo -n systemctl reset-failed "$u.service" 2>/dev/null || true
    schanged=1
  fi
done

for u in $SYS_UNITS; do for ext in service timer; do
  dst=/etc/systemd/system/$u.$ext
  cmp -s "$tmp/sys/$u.$ext" "$dst" || { sudo -n install -m 644 "$tmp/sys/$u.$ext" "$dst"; schanged=1; }
done; done
for u in $SYS_PATHS; do
  dst=/etc/systemd/system/$u.path
  cmp -s "$tmp/sys/$u.path" "$dst" || { sudo -n install -m 644 "$tmp/sys/$u.path" "$dst"; schanged=1; }
done
for u in $SYS_DAEMONS $SYS_AFTER; do
  dst=/etc/systemd/system/$u.service
  cmp -s "$tmp/sys/$u.service" "$dst" || { sudo -n install -m 644 "$tmp/sys/$u.service" "$dst"; schanged=1; }
done
[ $schanged = 0 ] || sudo -n systemctl daemon-reload
for u in $SYS_UNITS; do sudo -n systemctl enable --now "$u.timer"; done
for u in $SYS_PATHS; do sudo -n systemctl enable --now "$u.path"; done
# a changed daemon runs its new release at once (a restart waits for its cycle to end)
for u in $SYS_DAEMONS; do sudo -n systemctl enable "$u.service"
  if [ $schanged = 1 ]; then sudo -n systemctl restart "$u.service"; else sudo -n systemctl start "$u.service"; fi; done

# the first run makes the readings' tables and the guarded findings view (its ExecStartPre,
# operating.assist_readings install), so a landing never leaves biz_sql.comms_findings missing
sudo -n systemctl start --no-block plx-assist-readings.service || true

ln -sfn "$PLX_REPO/deploy/release/plx-release" "$HOME/.local/bin/plx-release"

# the live Ask canary's question (operating/ask_canary.py): the repo's default when the host
# has none (root's, readable by the canary's user; a host's own file is never overwritten),
# so the timer enabled above never runs unconfigured
# the canary signs in through plx-mint-link --canary (its own link file): the root copy is
# refreshed from the sealed release only (root-owned, nothing group/other-writable; never the
# repo checkout), and only when it differs and the host has one installed already
MINT_SRC=$PLX_CURRENT/deploy/ops/plx-mint-link MINT_DST=/usr/local/sbin/plx-mint-link
if [ -x "$MINT_DST" ] && [ -f "$MINT_SRC" ] && ! cmp -s "$MINT_SRC" "$MINT_DST"; then
  if [ "$(stat -L -c %U "$MINT_SRC")" = root ] && [ -z "$(find -L "$MINT_SRC" -perm /022)" ]; then
    sudo -n install -m 755 -o root -g root "$MINT_SRC" "$MINT_DST"
    echo "installed plx-mint-link from the sealed release"
  else
    echo "note: $MINT_SRC is not sealed (root-owned, not group/other-writable): plx-mint-link not updated; the Ask canary cannot sign in until it is" >&2
  fi
fi
CANARY_CONFIG=${PLX_ASK_CANARY_CONFIG:-/etc/plx/ask-canary.json}
if ! sudo -n test -e "$CANARY_CONFIG"; then
  sudo -n install -D -m 644 -o root -g root "$here/ask-canary.json" "$CANARY_CONFIG"
  echo "installed the default Ask canary question at $CANARY_CONFIG"
fi

# the Team view reads its snapshot from the API's data directory, else PLX_TEAM_ACTIVITY_FILE;
# who sees everyone and who is hidden are PLX_TEAM_VIEW_READERS and PLX_TEAM_HIDDEN_PEOPLE.
for v in PLX_TEAM_ACTIVITY_FILE PLX_TEAM_VIEW_READERS PLX_TEAM_HIDDEN_PEOPLE; do
  grep -qs "^$v=" /etc/plx/workbench-api.env 2>/dev/null \
    || echo "note: set $v in the API's environment (see operator_api/team_view.py)"
done
[ -s "$PLX_STATE/autopilot/autopilot-gate.json" ] \
  || echo "note: no $PLX_STATE/autopilot/autopilot-gate.json: the live feed prepares only the Draft people's workflows (operating/autopilot_draft.json) until a passing replay's gate file is copied there"
echo "note: the work-graph unit writes $PLX_STATE/work-graph/roles-derived.json (candidate roles); the autopilot reads it only if PLX_AUTOPILOT_ROLES_FILE names it"
systemctl list-timers --all --no-legend 'plx-sheet*' 'plx-system*' 'plx-team*' 'plx-work*' 'plx-proactive-whole*' 'plx-backup*' 'plx-restore*' 'plx-ops*' 'plx-labour*' 'plx-autopilot*' || true
