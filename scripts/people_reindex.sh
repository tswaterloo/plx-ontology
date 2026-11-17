#!/bin/bash
# REPROJ-1115 people: clear the queued Person objects (ontology.reproject) whose text index
# rebuilds empty from their served values, under the current people exclusion, fail closed
# (operating/pg/people_reindex.py). Writes only ontology.reproject deletes, with an undo log;
# no text row, store_meta key or digest. Refuses unless the served schema and steps digests
# are this tree's (ontology_migrate check) and the served steps digest starts with
# PEOPLE_EXPECT_STEPS (default steps:bf06102e).
#   bash scripts/people_reindex.sh --dry-run     # counts only, nothing written
#   bash scripts/people_reindex.sh --apply       # after a dry-run of the same tree
#   bash scripts/people_reindex.sh --undo        # put every cleared entry back
# The capture stores' WAL files belong to the services (group plx-state, which the user
# manager's units lack): the process takes its own user's groups (sudo -n setpriv
# --init-groups, as train_t4.sh's part() does) and stays inside the unit's cgroup.
# Runs as one unit in PEOPLE_SLICE at CPUWeight 20, --workers $(nproc)
# ranges (the run is seconds either way).
# Report: $PEOPLE_WORK/report.json (default /data/scratch/people-reindex-1115).
set -uo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
py=${PEOPLE_PYTHON:-/data/venv/bin/python3}
slice=${PEOPLE_SLICE:-lanes.slice}
work=${PEOPLE_WORK:-/data/scratch/people-reindex-1115}
expect=${PEOPLE_EXPECT_STEPS:-steps:bf06102e}
case "${1:-}" in
  --dry-run) args=(run --dry-run) ;; --apply) args=(run --apply) ;; --undo) args=(undo) ;;
  *) echo "usage: $0 --dry-run|--apply|--undo" >&2; exit 2 ;; esac
if [ "${args[0]}" = run ]; then
  args+=(--state-dir /data/plx-state --workers "$(nproc)" --expect-steps "$expect")
  if [ "$1" = --apply ] && [ ! -f "$work/dry-run.json" ]; then
    echo "refused: run --dry-run first (no $work/dry-run.json)" >&2; exit 3; fi
  [ "$1" = --apply ] && args+=(--after-dry-run "$work/dry-run.json")
fi
mkdir -p "$work" || exit 2
echo "$(TZ=America/New_York date '+%-I:%M %p ET') people_reindex $1 (slice $slice, weight 20)"
# No service env file: the unit gets no credential. The exclusion ids come from the
# proactive env file (people_exclusion.environ_with_exclusion: PLX_EXCLUDED_PEOPLE_* only);
# the DSN is PEOPLE_DSN, default the local socket's postgresql:///plx (never printed).
systemd-run --user --wait --pipe --quiet --collect --slice="$slice" -p CPUWeight=20 \
  --setenv=PGAPPNAME=people-reindex --setenv=PATH=/data/venv/bin:/usr/local/bin:/usr/bin:/bin \
  --setenv=PEOPLE_DSN="${PEOPLE_DSN:-postgresql:///plx}" --setenv=HOME="$HOME" \
  --working-directory="$repo" \
  sudo -n --preserve-env=PEOPLE_DSN,PGAPPNAME,HOME,PATH \
  setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups \
  "$py" -c 'import os, sys
from operating.pg import people_reindex
sys.exit(people_reindex.main([sys.argv[1], "--dsn", os.environ["PEOPLE_DSN"], *sys.argv[2:]]))' \
  "${args[0]}" --work "$work" "${args[@]:1}"
code=$?
echo "$(TZ=America/New_York date '+%-I:%M %p ET') people_reindex $1 exit $code"
exit $code
