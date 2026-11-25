#!/bin/bash
# plx-proactive-whole's runner: evaluate the whole-store proactive rules on a Spark, return
# only their fold payloads, apply them to the box's gap inbox, then use any time left for
# the vendor stage on the box. The Spark pass reads its state copy and hot standby; the
# shared inbox and the change feed are written only here. Usage:
#   proactive_whole.sh STATE_DIR HOST_CONFIG [OUT_DIR]
set -uo pipefail
here=$(dirname "$(readlink -f "$0")")
root=$(readlink -f "$here/../..")
state=${1:?state dir} config=${2:?host config} out=${3:-/data/scratch/plx-proactive-whole}
budget=${WHOLE_BUDGET_SECONDS:-6300} refresh=${WHOLE_REFRESH_SECONDS:-5400} bound=${WHOLE_BOUND:-400}
sha=${WHOLE_SHA:-$(python3 - "$root/RELEASE.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["sha"])
PY
)}
folds="$out/folds"
mkdir -p -m 700 "$folds"
started=$(date +%s)
host=${SPARK_HOST:-s7d3d}
rules=()
[ -z "${WHOLE_RULES:-}" ] || rules=(--whole-rules "$WHOLE_RULES")
SPARK_SKIP_LOGIN_CHECK=1 "$here/spark_run.sh" --host "$host" --sha "$sha" --state copy --pg standby \
  --name plx-proactive-whole --ship "$(dirname "$config")" --out "$folds" -- \
  env PLX_ONTOLOGY_PG=only /data/venv/bin/python3 -m operating.vendor_watch \
  --state-dir "$state" --config "$config" --refresh whole --refresh-seconds "$refresh" \
  --budget-seconds "$budget" --bound "$bound" --whole-export-dir "$folds" --stop-after-refresh \
  "${rules[@]}"
spark_rc=$?
if [ "${WHOLE_APPLY:-1}" = 1 ]; then
  /data/venv/bin/python3 -m operating.vendor_watch --state-dir "$state" --config "$config" \
    --apply-folds "$folds"
  apply_rc=$?
else
  apply_rc=0   # a rehearsal of the Spark pass only: nothing writes the live inbox
fi
now=$(date +%s)
left=$((budget - (now - started) - 60))   # the vendor stage's share, with a minute to end
if [ "$left" -gt 120 ]; then
  /data/venv/bin/python3 -m operating.vendor_watch --state-dir "$state" --config "$config" \
    --refresh never --budget-seconds "$left" --bound "$bound"
  vendor_rc=$?
else
  vendor_rc=0
fi
[ "$spark_rc" = 0 ] && [ "$apply_rc" = 0 ] && [ "$vendor_rc" = 0 ]
