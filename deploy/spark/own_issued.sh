#!/bin/bash
# plx-own-issued's runner: the guard's incremental pass with the judging on a Spark
# (following proactive_whole.sh): the box bundles the unjudged documents' inputs (the
# digests to judge, their capture rows, and their blobs), the Spark judges them there (the
# extraction and matching CPU and memory are the Spark's, never the box's), and the box
# applies the decisions in refresh's own transaction. Nothing writes the guard's tables
# but the box's apply. Stale bundle/output files are cleared before each pass and shipped
# blobs are removed from both the box and the Spark when the pass ends.
# Usage: own_issued.sh [OUT_DIR]
set -uo pipefail
here=$(dirname "$(readlink -f "$0")")
root=$(readlink -f "$here/../..")
out=${1:-/data/scratch/plx-own-issued}
py=${OWN_PYTHON:-/data/venv/bin/python3}
sha=${OWN_SHA:-$(python3 - "$root/RELEASE.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["sha"])
PY
)}
host=${SPARK_HOST:-s7d3d}
bundle="$out/bundle"
back="$out/back"
mkdir -p -m 700 "$bundle" "$back"
cleanup() {
  rm -rf "$bundle/blobs"
  rm -f "$bundle/tasks.txt" "$bundle/rows.json"
}
trap cleanup EXIT
# clear any stale inputs or decisions from an earlier run before bundling
cleanup
rm -f "$back/decisions.jsonl"
# the inputs, on the box: the unjudged digests, their capture rows, and their blobs
"$py" -m operating.pg.own_issued bundle --if-migrated --out "$bundle"
bundle_rc=$?
[ "$bundle_rc" = 0 ] || exit "$bundle_rc"
# bundle leaves no tasks.txt when skipped (table_missing or never_built)
[ -f "$bundle/tasks.txt" ] || { echo "own_issued: skipped (not migrated or not built)" >&2; exit 0; }
# empty tasks.txt: no unjudged digests, so skip Spark, but still hide any new objects of
# already-judged PLX-own documents via incremental apply
if [ ! -s "$bundle/tasks.txt" ]; then
  if [ "${OWN_APPLY:-1}" = 0 ]; then
    echo "own_issued: nothing to judge (OWN_APPLY=0)" >&2
    exit 0
  fi
  "$py" -m operating.pg.own_issued apply --if-migrated --incremental /dev/null
  exit $?
fi
# judge on the Spark: the rows and blobs of the bundle, never the box's capture stores
SPARK_SKIP_LOGIN_CHECK=1 "$here/spark_run.sh" --cleanup-ship --host "$host" --sha "$sha" \
  --state none --pg standby --name plx-own-issued --ship "$bundle" --out "$back" -- \
  env PLX_OWN_ROWS="$bundle/rows.json" \
      PLX_OWN_MAIL_BLOBS="$bundle/blobs/mail" \
      PLX_OWN_SHAREPOINT_BLOBS="$bundle/blobs/sharepoint" \
  /data/venv/bin/python3 -m operating.pg.own_issued build --shas "$bundle/tasks.txt" \
  --decisions "$back/decisions.jsonl" --workers "${OWN_WORKERS:-12}"
spark_rc=$?
[ "$spark_rc" = 0 ] || exit "$spark_rc"
# the write side, on the box: refresh's own transaction, never a replace (OWN_APPLY=0:
# a rehearsal of bundle and judge only — nothing writes the guard's tables)
if [ "${OWN_APPLY:-1}" = 0 ]; then
  echo "own_issued: OWN_APPLY=0, the decisions stay in $back for inspection" >&2
  exit 0
fi
if [ -s "$back/decisions.jsonl" ]; then
  "$py" -m operating.pg.own_issued apply --if-migrated --incremental "$back/decisions.jsonl"
  apply_rc=$?
  cp -f "$back/decisions.jsonl" "$out/decisions-last.jsonl" 2>/dev/null || true
  rm -f "$back/decisions.jsonl"
  exit "$apply_rc"
fi
echo "own_issued: the Spark pass wrote no decisions" >&2
exit 1
