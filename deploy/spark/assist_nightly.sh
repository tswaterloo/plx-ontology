#!/bin/bash
# Yesterday's Assist scoreboard (operating/assist_nightly.py), run on a Spark against its hot standby
# and the Spark's copy of the box's stores (the live Assist queue included), from the box's
# plx-assist-nightly.timer. Model-free. The board comes back to OUT on the box
# (<OUT>/<day>.json); a run that could not score a role exits 1 and says which (the unit fails,
# the host alarm says so).
#   assist_nightly.sh [--day YYYY-MM-DD] [--host s7d3d] [--sha SHA]
set -uo pipefail
here=$(dirname "$(readlink -f "$0")")
day="" host=s7d3d sha=""
while [ $# -gt 0 ]; do case "$1" in
  --day) day=$2; shift 2 ;; --host) host=$2; shift 2 ;; --sha) sha=$2; shift 2 ;;
  *) echo "assist_nightly.sh: unknown option $1" >&2; exit 2 ;; esac; done
unset PLX_WORKBENCH_API_KEY ANTHROPIC_API_KEY
if [ -z "$sha" ]; then   # the release being served
  sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"])' \
        "${PLX_RELEASE_DIR:-/data/plx-releases/current}/RELEASE.json") || exit 2
fi
[ -n "$day" ] || day=$(TZ=America/New_York date -d yesterday +%F)
out=${PLX_ASSIST_SCORE_DIR:-/data/plx-scores/assist}
mkdir -p "$out"
cd "${PLX_PLAN_TOP:-/data/OntologyProject}" || exit 2
SPARK_SKIP_LOGIN_CHECK=1 exec "$here/spark_run.sh" --host "$host" --pg standby --sha "$sha" \
  --out "$out" --name "assist-nightly" -- sh -c '
    gen=$(python3 -c "import json;print(json.load(open(\"/data/plx-state/projection/CURRENT\"))[\"generation\"])") &&
    PLX_ONTOLOGY_PG=only exec python3 -m operating.assist_nightly --source postgres \
      --store "/data/plx-state/projection/generations/$gen/store.sqlite" --state-dir /data/plx-state \
      --day "$1" --out-dir "$2" $(for a in $(echo "$3" | tr "," " "); do printf -- "--person %s " "$a"; done)' \
  assist-nightly "$day" "$out" "${PLX_ASSIST_PERSON_ROLES:-vince@petrasoap.com,stephen@petrasoap.com}"
