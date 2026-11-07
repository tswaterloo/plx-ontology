#!/bin/bash
# After a new projection publishes:
#  1. restart plx-ingest so the next (cold) build runs on origin/main's schema;
#  2. deploy the API and app at the LAST main commit whose schema digest equals the new
#     projection's (no schema drift), health-check;
#  3. run the ONE full live-ops scoreboard (dev + held-out) as Vince on that SHA.
# Usage: post_publish.sh <generation-dir>
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
set -u
S=$PLX_SCRATCH; D=$PLX_CURRENT; R=$PLX_REPO; log=$S/runs/post-publish.log
exec >>"$log" 2>&1
gen=${1:?generation dir}
echo "== $(date -u +%FT%TZ) post_publish $gen"
# each generation once, whichever watcher calls first (the plx-publish-watch.path unit or a
# hand-started watch_publish.sh <gen>): the first claims it, any later call skips
claims=$S/runs/published; mkdir -p "$claims"
if ! mkdir "$claims/$(basename "$gen")" 2>/dev/null; then
  echo "$(basename "$gen") already handled ($claims/$(basename "$gen")): skip"; exit 0
fi
want=$(timeout 60 python3 -c "
import sqlite3,sys;c=sqlite3.connect('file:$gen/store.sqlite?mode=ro',uri=True)
print(c.execute(\"select value from store_meta where key='schema_digest'\").fetchone()[0])")
echo "projection schema $want"
git -C $R fetch -q origin
pick=""; prev_tree=""; prev_d=""
for c in $(git -C $R rev-list --first-parent --reverse 42a7fedac3^..origin/main); do
  tree=$(git -C $R rev-parse $c:ontology/schema)
  if [ "$tree" != "$prev_tree" ]; then
    w=$S/wt/dg-$c; git -C $R worktree add -q --detach $w $c
    prev_d=$(cd $w && timeout 120 $PLX_VENV/bin/python3 -c "from ontology.schema import load_schema; print(load_schema().digest)" 2>/dev/null)
    git -C $R worktree remove --force $w; prev_tree=$tree
  fi
  [ "$prev_d" = "$want" ] && pick=$c
done
[ -n "$pick" ] || { echo "NO MAIN COMMIT MATCHES $want"; exit 1; }
sha=$(git -C $R rev-parse --short=10 $pick)
main=$(git -C $R rev-parse --short=10 origin/main)
echo "deploy $sha (main is $main)"
# 1. pause ingest during deploy + eval (a cold build next to the eval starves both of memory)
sudo systemctl stop plx-ingest && echo "plx-ingest stopped for the eval"
start_next() {  # next build on main's schema, after the eval
  # plx-ingest runs from the current release (deploy/release/plx-release): nothing to check out
  sudo sed -i 's/^PLX_BUILD_WORKERS=.*/PLX_BUILD_WORKERS=10/' /etc/plx/ingest.env
  # gen 8 was built by 39cf8a190c: lets the next build adopt it and build incrementally
  sudo systemctl start plx-ingest && echo "$(date -u +%FT%TZ) plx-ingest started for the next build on $main (10 workers)"
}
trap start_next EXIT
# 2. deploy: main's code with the generation's own schema when that loads to the same digest
#    (today's fixes ride on gen 8's schema), else the last main commit with that schema.
schema_from=""
rel="$(dirname "$(readlink -f "$0")")/../release/plx-release"
# main with the generation's own schema when that loads to the same digest (plx-release
# refuses any other), else the last main commit with that schema
if [ "$main" != "$sha" ] && id=$("$rel" build "$main" --overlay "$sha:ontology/schema" | tail -1); then
  schema_from=$sha; echo "deploy main $main with schema of $sha"; sha=$main
else
  id=$("$rel" build "$sha" | tail -1) || { echo "RELEASE BUILD FAILED"; exit 1; }
fi
"$rel" switch "$id" && "$rel" restart || { echo "switch or restart failed"; exit 1; }
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8780/api/health || true)
  [ "$code" = 200 ] && break; sleep 5
done
echo "deployed $sha api_health=$code"
[ "$code" = 200 ] || exit 1
# disk: the next build needs room for its seed copy and shards; only the served generation stays
( cd $PLX_CURRENT && $PLX_VENV/bin/python3 -c "from ontology.project_incremental import prune_to_current as p; print('pruned to current:', p('$PLX_STATE/projection'))" ) || echo "prune failed"
df -h /data | tail -1
# speed: the next build starts as soon as the new generation is deployed; the eval runs beside it
if [ -e $PLX_OPS_PRIVATE/QUIET_EVAL ]; then
  echo "quiet eval: the next build starts after the eval"; export PLX_EVAL_WORKERS=8
else
  trap - EXIT; start_next
fi
export PLX_EVAL_WORKERS=${PLX_EVAL_WORKERS:-4}
# 3a. the store facts every session reads (a full scan of a new generation, minutes):
#     computed once here so no session or the eval waits past its start limit
( cd "$D" && timeout 5400 nice -n 5 $PLX_VENV/bin/python3 $PLX_OPS/prime_facts.py $PLX_STATE/projection/ontology.sqlite ) || echo "prime_facts failed (sessions will compute them)"
# 3. the one scoreboard
name=lo-$(basename $gen)-$sha
echo "eval $name"
# Back to origin/main once it lands
PLX_SEARCH=hybrid SCHEMA_FROM="$schema_from" EVAL_FROM=${EVAL_FROM:-origin/main} $S/live-ops/run.sh "$name" "$sha" vince
cat $S/live-ops/runs/$name/ITEMS 2>/dev/null
echo "== $(date -u +%FT%TZ) eval finished: $S/live-ops/runs/$name/report.json"
