#!/bin/bash
# plx-search-follow's runner: the search index delta-layer follower with chunking and bge-m3
# embedding on a Spark (against the Spark's own hot standby DB and Spark GPU/CPU), and only
# lightweight state prep + WAL-budgeted Postgres apply on the box. Usage:
#   search_follow.sh [STATE_DIR] [OUT_DIR]
set -uo pipefail
umask 077
here=$(dirname "$(readlink -f "$0")")
root=$(readlink -f "$here/../..")
cd "$root" || exit 2
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
state=${1:-/data/plx-state}
out=${2:-/data/scratch/plx-search-follow}
py=${SEARCH_FOLLOW_PYTHON:-/data/venv/bin/python3}
spark_run=${SEARCH_FOLLOW_SPARK_RUN:-$here/spark_run.sh}
if [ -n "${SEARCH_FOLLOW_SHA:-}" ]; then
  sha=$SEARCH_FOLLOW_SHA
else
  sha=$(python3 - "$root/RELEASE.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["sha"])
PY
  ) || { echo "search_follow: cannot read sha from $root/RELEASE.json" >&2; exit 2; }
fi
[ -n "$sha" ] || { echo "search_follow: empty sha" >&2; exit 2; }
host=${SPARK_HOST:-s7d3d}
ship="$out/ship"
back="$out/back"
mkdir -p -m 700 "$out"
rm -rf "$ship" "$back"
mkdir -p -m 700 "$ship" "$back"
cleanup() {
  rm -rf "$ship"
}
trap cleanup EXIT

common_args=(
  --mode layers
  --state "$state"
  --layer-seconds "${SEARCH_FOLLOW_LAYER_SECONDS:-600}"
  --embed-seconds "${SEARCH_FOLLOW_EMBED_SECONDS:-120}"
  --embed-batch "${SEARCH_FOLLOW_EMBED_BATCH:-16}"
  --embed-rows "${SEARCH_FOLLOW_EMBED_ROWS:-32}"
  --embed-write-mb-per-hour "${SEARCH_FOLLOW_EMBED_WRITE_MB_PER_HOUR:-256}"
  --wait-seconds "${SEARCH_FOLLOW_WAIT_SECONDS:-300}"
  --min-free-gb "${SEARCH_FOLLOW_MIN_FREE_GB:-60}"
)

# Lightweight prep on the box: disk floor, window lock, people exclusion, tiny state + delta
# manifest (no bge-m3 load, no heavy chunking). Exits cleanly without manifest.json when idle.
"$py" -m operating.embed follow "${common_args[@]}" --prepare-dir "$ship"
prep_rc=$?
[ "$prep_rc" = 0 ] || exit "$prep_rc"
[ -f "$ship/manifest.json" ] || exit 0

# Heavy compute on the Spark: chunking + bge-m3 GPU/CPU embedding against the hot standby.
SPARK_SKIP_LOGIN_CHECK=1 "$spark_run" --cleanup-ship --host "$host" --sha "$sha" \
  --state copy --pg standby --name plx-search-follow --ship "$ship" --out "$back" -- \
  env PLX_ONTOLOGY_PG=only /data/venv/bin/python3 -m operating.embed follow \
  "${common_args[@]}" --chunk-workers "${SEARCH_FOLLOW_CHUNK_WORKERS:-4}" \
  --context-dir "$ship" --export-dir "$back"
spark_rc=$?
if [ "$spark_rc" != 0 ] && { [ "$spark_rc" != 1 ] || [ ! -f "$back/export.json" ]; }; then
  rm -rf "$back"/*
  exit "$spark_rc"
fi
[ -f "$back/export.json" ] || { echo "search_follow: the Spark pass wrote no export" >&2; exit 1; }

# Lightweight write side on the box (SEARCH_FOLLOW_APPLY=0: rehearsal only, never writes DB).
if [ "${SEARCH_FOLLOW_APPLY:-1}" = 0 ]; then
  echo "search_follow: SEARCH_FOLLOW_APPLY=0, payloads stay in $back for inspection" >&2
  exit 0
fi
"$py" -m operating.embed follow "${common_args[@]}" --apply-dir "$back"
apply_rc=$?
[ "$spark_rc" = 0 ] || exit "$spark_rc"
exit "$apply_rc"
