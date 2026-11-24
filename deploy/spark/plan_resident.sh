#!/bin/bash
# The resident plan runner on a Spark (operator_api/plan_resident.py), kept up by systemd on the box
# (deploy/systemd/plx-plan-resident@.service): one shard per instance, N shards in all.
#   plan_resident.sh run --shard I/N [--env-file FILE] [--sha SHA] [--host H]
# The process runs on the Spark (through spark_run.sh: the release's own code, the Spark's copy of
# the state, a read-only login on the Spark's hot standby) and stays up; this script only holds
# its ssh. It is launched from the box on purpose: the people exclusion goes to a Spark run on
# ssh's stdin and never as a file there (spark_run_spec.py), so a Spark-side unit could not give
# the process the current list; when the list changes the runner ends (below) and this script's
# unit starts it again with the new one. The runner refuses to run on anything but a standby.
# Its answers land in OUT on the Spark (a --spark-out folder, never copied back); the box fetches
# them (operating/pg/plan_sections_pull.py).
# FILE (outside the repository, like /etc/plx): the app's non-secret PLX_* settings and
# PLX_PG_DSN=postgresql:///plx, PLX_BATCH_HOST_CONFIG, PLX_BATCH_READER (the owner's employee id).
set -uo pipefail
here=$(dirname "$(readlink -f "$0")")
[ "${1:-}" = run ] || { sed -n 2,4p "$0"; exit 2; }
shift
shard=0/1 envf=${PLX_PLAN_ENV:-/etc/plx/plan-resident.env} sha="" host=""
while [ $# -gt 0 ]; do case "$1" in
  --shard) shard=$2; shift 2 ;; --env-file) envf=$2; shift 2 ;; --sha) sha=$2; shift 2 ;;
  --host) host=$2; shift 2 ;; *) echo "plan_resident.sh: unknown option $1" >&2; exit 2 ;; esac; done
shard=${shard/-of-//}   # a unit instance is named 0-of-4
[[ "$shard" =~ ^[0-9]+/[0-9]+$ ]] || { echo "--shard is I/N" >&2; exit 2; }
[ -r "$envf" ] || { echo "plan_resident.sh: no env file $envf" >&2; exit 2; }
set -a; . "$envf"; set +a
# Which Spark runs this shard: --host, else shard I takes PLX_PLAN_HOSTS (space separated, in the
# settings file; default s7d3d) word I mod the number of hosts, so 8 shards over "s7d3d sb4ec" put
# four on each. The box's puller reads the same list (operating/pg/plan_sections_pull.py).
if [ -z "$host" ]; then
  read -ra hosts <<< "${PLX_PLAN_HOSTS:-s7d3d}"
  host=${hosts[$(( ${shard%/*} % ${#hosts[@]} ))]}
fi
unset PLX_WORKBENCH_API_KEY ANTHROPIC_API_KEY
if [ -z "$sha" ]; then   # the release being served
  sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"])' \
        "${PLX_RELEASE_DIR:-/data/plx-releases/current}/RELEASE.json") || exit 2
fi
out=${PLX_PLAN_OUT:-/data/plx-plan/out} queue=${PLX_PLAN_QUEUE:-/data/plx-plan/queue}
cd "${PLX_PLAN_TOP:-/data/OntologyProject}" || exit 2
# the shards' units start together and spark_run.sh pushes the commit to one ref on the Spark: a
# push that finds the ref being written is refused, so each shard waits its turn
sleep $(( ${shard%/*} * 6 ))
SPARK_SKIP_LOGIN_CHECK=1 exec "$here/spark_run.sh" --host "$host" --pg standby --sha "$sha" \
  --ship "$(dirname "$PLX_BATCH_HOST_CONFIG")" --spark-out "$out" --spark-out "$queue" \
  --env PLX_PG_DSN --name "plan-resident-${shard%/*}" -- \
  python3 -m operator_api.plan_resident --out "$out" --queue "$queue" --shard "$shard"
