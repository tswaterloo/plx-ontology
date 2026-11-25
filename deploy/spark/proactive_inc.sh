#!/bin/bash
# plx-proactive-inc's runner: the incremental proactive pass with its compute on a Spark
# against the Spark's own hot standby DB (following proactive_whole.sh and comm_text.sh).
# On the box, prepare extracts a compact snapshot of the open-gap inbox, changes metadata
# and delta pointer; the Spark computes fingerprints, scope and detector folds read-only and
# writes only the fold/delta JSON payloads; the box applies them under the inbox lock and
# advances the state watermarks only to what compute saw. Usage:
#   proactive_inc.sh STATE_DIR [OUT_DIR]
set -uo pipefail
umask 077
here=$(dirname "$(readlink -f "$0")")
root=$(readlink -f "$here/../..")
state=${1:?state dir}
out=${2:-/data/scratch/plx-proactive-inc}
py=${INC_PYTHON:-/data/venv/bin/python3}
spark_run=${INC_SPARK_RUN:-$here/spark_run.sh}
if [ -n "${INC_SHA:-}" ]; then
  sha=$INC_SHA
else
  sha=$(python3 - "$root/RELEASE.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["sha"])
PY
  ) || { echo "proactive_inc: cannot read sha from $root/RELEASE.json" >&2; exit 2; }
fi
[ -n "$sha" ] || { echo "proactive_inc: empty sha" >&2; exit 2; }
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

# compact read-only snapshot on the box (skipped cleanly if no inbox, window-locked or busy)
"$py" -m operating.proactive_incremental --state-dir "$state" --prepare-dir "$ship"
prep_rc=$?
[ "$prep_rc" = 0 ] || exit "$prep_rc"
[ -f "$ship/context.json" ] || exit 0

extra=()
[ -z "${INC_AS_OF:-}" ] || extra=(--as-of "$INC_AS_OF")
SPARK_SKIP_LOGIN_CHECK=1 "$spark_run" --cleanup-ship --host "$host" --sha "$sha" \
  --state copy --pg standby --name plx-proactive-inc --ship "$ship" --out "$back" -- \
  env PLX_ONTOLOGY_PG=only /data/venv/bin/python3 -m operating.proactive_incremental \
  --state-dir "$state" --context-dir "$ship" --export-dir "$back" "${extra[@]}"
spark_rc=$?
if [ "$spark_rc" != 0 ]; then
  rm -f "$back"/*.json
  exit "$spark_rc"
fi

# the write side, on the box: the exact transaction/lock path (INC_APPLY=0: rehearsal only)
if [ "${INC_APPLY:-1}" = 0 ]; then
  echo "proactive_inc: INC_APPLY=0, the payloads stay in $back for inspection" >&2
  exit 0
fi
"$py" -m operating.proactive_incremental --state-dir "$state" --apply-folds "$back"
