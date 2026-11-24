#!/bin/bash
# The resident History builder on a Spark (operator_api/history_resident.py), kept up by systemd on
# the box (deploy/systemd/plx-history-resident@.service): one shard per instance, N in all.
#   history_resident.sh run --shard I/N [--env-file FILE] [--sha SHA] [--host s7d3d]
# The launcher of deploy/spark/plan_resident.sh, for the History family: the process runs on the
# Spark through spark_run.sh (the release's own code, the Spark's copy of the state, a read-only
# login on the Spark's hot standby; the people exclusion on ssh's stdin, never as a file there) and
# stays up; this script only holds its ssh. Its files land in OUT on the Spark (--spark-out) and
# the box fetches and stores them (python3 -m operating.pg.record_history pull,
# plx-history-pull.service). FILE: the plan runner's env file (/etc/plx/plan-resident.env: the
# app's non-secret PLX_* settings, PLX_PG_DSN, PLX_BATCH_HOST_CONFIG, PLX_BATCH_READER = the
# owner's employee id).
set -uo pipefail
here=$(dirname "$(readlink -f "$0")")
[ "${1:-}" = run ] || { sed -n 2,4p "$0"; exit 2; }
shift
shard=0/1 envf=${PLX_HISTORY_ENV:-/etc/plx/plan-resident.env} sha="" host=s7d3d
while [ $# -gt 0 ]; do case "$1" in
  --shard) shard=$2; shift 2 ;; --env-file) envf=$2; shift 2 ;; --sha) sha=$2; shift 2 ;;
  --host) host=$2; shift 2 ;; *) echo "history_resident.sh: unknown option $1" >&2; exit 2 ;; esac; done
shard=${shard/-of-//}   # a unit instance is named 0-of-4
[[ "$shard" =~ ^[0-9]+/[0-9]+$ ]] || { echo "--shard is I/N" >&2; exit 2; }
[ -r "$envf" ] || { echo "history_resident.sh: no env file $envf" >&2; exit 2; }
set -a; . "$envf"; set +a
unset PLX_WORKBENCH_API_KEY ANTHROPIC_API_KEY
if [ -z "$sha" ]; then   # the release being served
  sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"])' \
        "${PLX_RELEASE_DIR:-/data/plx-releases/current}/RELEASE.json") || exit 2
fi
out=${PLX_HISTORY_OUT:-/data/plx-history/out} queue=${PLX_HISTORY_QUEUE:-/data/plx-history/queue}
cd "${PLX_PLAN_TOP:-/data/OntologyProject}" || exit 2
# spark_run.sh pushes the commit to one ref on the Spark: each shard waits its turn
sleep $(( ${shard%/*} * 6 + 3 ))
SPARK_SKIP_LOGIN_CHECK=1 exec "$here/spark_run.sh" --host "$host" --pg standby --sha "$sha" \
  --ship "$(dirname "$PLX_BATCH_HOST_CONFIG")" --spark-out "$out" --spark-out "$queue" \
  --env PLX_PG_DSN --name "history-resident-${shard%/*}" -- \
  python3 -m operator_api.history_resident --out "$out" --queue "$queue" --shard "$shard"
