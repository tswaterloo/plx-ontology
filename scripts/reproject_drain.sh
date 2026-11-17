#!/bin/bash
# Reprojection drain: run normal delta cycles until ontology.reproject stops shrinking.
#
# Each queued object is projected again through the normal path: ontology.delta's own
# cycle, ontology.delta.reproject_requests, finds its record (the delta state, else its
# Postgres handles), opens it past the people exclusion, and the layer apply drops the
# queue entry. No SQL writes here. The script only reads the queue count and the last
# cycle's report.
#   bash scripts/reproject_drain.sh [--dry-run] [--max-cycles N] [--system]
# Run it only after the migration that queued the objects is applied and its code is
# deployed: it refuses unless
# `ontology_migrate check` passes, because a cycle of other code is held out and re-queued.
# Every cycle runs as a unit in DRAIN_SLICE at CPUWeight 20, with
# --workers $(nproc). One delta state takes one cycle at a time (.run.lock), so the
# parallel part is the cycle's own worker pool. Set DRAIN_SLICE to use more.
# A cycle the timer starts meanwhile waits on the same lock. The two never overlap.
# --system: each cycle is a system unit (sudo -n systemd-run) as the invoking user, from the
# current release (DRAIN_REPO, default /data/plx-releases/current), in DRAIN_SLICE (default
# plx-reproject.slice) with a 20G/24G memory cap. A system unit gets the user's
# supplementary groups (initgroups, so plx-state); a user unit does not.
# Default (user unit): the capture stores' WAL files belong to the services (group plx-state,
# which the user manager's units lack): the cycle takes its own user's groups (sudo -n -E setpriv
# --init-groups, as people_reindex.sh and train_t4.sh's part() do), keeps the unit's
# environment (the service env file) and stays inside the unit's cgroup and slice.
set -uo pipefail
py=${DRAIN_PYTHON:-/data/venv/bin/python3}
max=${DRAIN_MAX_CYCLES:-200} dry=0 system=0
while [ $# -gt 0 ]; do case "$1" in
  --dry-run) dry=1; shift ;; --max-cycles) max=$2; shift 2 ;; --system) system=1; shift ;;
  *) echo "usage: $0 [--dry-run] [--max-cycles N] [--system]" >&2; exit 2 ;; esac; done
if [ "$system" = 1 ]; then
  repo=${DRAIN_REPO:-/data/plx-releases/current}
  slice=${DRAIN_SLICE:-plx-reproject.slice}
  run_unit=(sudo -n systemd-run --uid="$(id -u)" --gid="$(id -g)" -p MemoryHigh=20G -p MemoryMax=24G)
  as_user=()
else
  repo=${DRAIN_REPO:-$(cd "$(dirname "$0")/.." && pwd)}
  slice=${DRAIN_SLICE:-lanes.slice}
  run_unit=(systemd-run --user)
  as_user=(sudo -n -E setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups)
fi
cd "$repo" || exit 2
count() { env -u PLX_WORKBENCH_API_KEY -u AZURE_CLIENT_SECRET -u ANTHROPIC_API_KEY "$py" -c '
import psycopg
from operating.pg import config
with psycopg.connect(config.dsn(), connect_timeout=5) as pg:
    pg.execute("SET TRANSACTION READ ONLY")
    print(pg.execute("SELECT count(*) FROM ontology.reproject").fetchone()[0])'; }
env -u PLX_WORKBENCH_API_KEY -u AZURE_CLIENT_SECRET -u ANTHROPIC_API_KEY \
  "$py" -m operating.pg.ontology_migrate check >/dev/null || {
  echo "refused: served schema/steps differ from this code (migration pending); deploy first" >&2; exit 3; }
left=$(count) || { echo "refused: cannot read ontology.reproject" >&2; exit 4; }
echo "$(TZ=America/New_York date '+%-I:%M %p ET') queued $left"
[ "$dry" = 1 ] && exit 0
stalls=0
for i in $(seq 1 "$max"); do
  "${run_unit[@]}" --wait --quiet --collect --slice="$slice" -p CPUWeight=20 \
    -p EnvironmentFile="$HOME"/.config/plx/plx.env \
    --setenv=PLX_DELTA_PG=1 --setenv=PLX_DELTA_BASE_PG=1 --setenv=PLX_ONTOLOGY_PG=only \
    --setenv=PLX_BUILD_WORKERS="$(nproc)" --setenv=TMPDIR=/data/tmp/plx-delta \
    --setenv=SQLITE_TMPDIR=/data/tmp/plx-delta --setenv=PGAPPNAME=reproject-drain \
    --setenv=PATH=/data/venv/bin:/usr/local/bin:/usr/bin:/bin --working-directory="$repo" \
    "${as_user[@]}" "$py" -m ontology.delta run --state-dir /data/plx-state --workers "$(nproc)" \
    || echo "cycle $i failed (exit $?): the queue stays; next cycle retries"
  skipped=$(tail -1 /data/plx-state/projection/delta/cycles.jsonl | "$py" -c '
import json, sys; print(json.loads(sys.stdin.read()).get("skipped") or "")' 2>/dev/null)
  case "$skipped" in
    window) echo "a migration/maintenance window holds the delta; stopping (rerun after it)"; exit 5 ;;
    already_running) sleep 60; continue ;;   # the timer's cycle drains too; no stall counted
  esac
  now=$(count) || { echo "cannot read the queue; stopping" >&2; exit 4; }
  echo "$(TZ=America/New_York date '+%-I:%M %p ET') cycle $i: queued $left -> $now"
  if [ "$now" -ge "$left" ]; then stalls=$((stalls + 1)); else stalls=0; fi
  left=$now
  # what is left after two cycles without progress is what no record can re-project
  # (people-directory persons, gone objects, withheld mail). The cycle report counts it.
  [ "$stalls" -ge 2 ] && break
done
tail -1 /data/plx-state/projection/delta/cycles.jsonl | "$py" -c '
import json, sys; print("last cycle reproject:", json.dumps(json.loads(sys.stdin.read()).get("reproject")))'
