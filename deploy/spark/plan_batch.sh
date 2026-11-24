#!/bin/bash
# The batch plan runner on a Spark (operator_api/plan_batch.py): the record pages' question plans run
# once per open record against the s7d3d standby as the owner's full reader, N shards in parallel;
# the JSON lines come back to OUT/ and are loaded on the box by `python3 -m operating.pg.plan_sections load`.
#   plan_batch.sh --out DIR --env-file FILE [--shards N] [--sha SHA] [--family F] [--ids FILE] [--host s7d3d]
# FILE (kept outside the repository, like /etc/plx): the app's own non-secret PLX_* settings (PLX_ONTOLOGY_PG,
# PLX_PG_FACTS_DIR, PLX_EXCLUDED_PEOPLE_OIDS, PLX_FULL_READERS ...), PLX_PG_DSN=postgresql:///plx,
# PLX_BATCH_HOST_CONFIG (the host config the app serves with) and PLX_BATCH_READER (the owner's employee id).
# Reads only: spark_run's Postgres login is read-only; a standby that fails its guard aborts (never the box).
set -uo pipefail
here=$(dirname "$(readlink -f "$0")")
out="" envf="" shards=4 sha=HEAD host=s7d3d families=() ids=""
while [ $# -gt 0 ]; do case "$1" in
  --out) out=$2; shift 2 ;; --env-file) envf=$2; shift 2 ;; --shards) shards=$2; shift 2 ;;
  --sha) sha=$2; shift 2 ;; --family) families+=(--family "$2"); shift 2 ;; --ids) ids=$2; shift 2 ;;
  --host) host=$2; shift 2 ;; *) echo "plan_batch.sh: unknown option $1" >&2; exit 2 ;; esac; done
[ -n "$out" ] && [ -r "$envf" ] || { echo "usage: plan_batch.sh --out DIR --env-file FILE [...]" >&2; exit 2; }
[[ "$shards" =~ ^[0-9]+$ ]] && [ "$shards" -ge 1 ] && [ "$shards" -le 16 ] || { echo "--shards is 1..16" >&2; exit 2; }
mkdir -p "$out"
set -a; . "$envf"; set +a
unset PLX_WORKBENCH_API_KEY ANTHROPIC_API_KEY
extra=(); [ -z "$ids" ] || extra=(--ship "$ids")
q_out=$(printf '%q' "$out"); q_ids=""; [ -z "$ids" ] || q_ids="--ids $(printf '%q' "$ids")"
q_fam=""; for f in "${families[@]+"${families[@]}"}"; do q_fam+=" $(printf '%q' "$f")"; done
inner="set -u; i=0; while [ \$i -lt $shards ]; do (cd /data/OntologyProject && python3 -m operator_api.plan_batch run --out $q_out/shard-\$i.jsonl --shard \$i/$shards$q_fam $q_ids) & i=\$((i+1)); done; fail=0; for p in \$(jobs -p); do wait \$p || fail=1; done; exit \$fail"
SPARK_SKIP_LOGIN_CHECK=1 exec "$here/spark_run.sh" --host "$host" --pg standby --sha "$sha" --ship "$(dirname "$PLX_BATCH_HOST_CONFIG")" \
  "${extra[@]}" --out "$out" --env PLX_PG_DSN --name plan-batch -- bash -c "$inner"
