#!/bin/bash
# Publish watcher: run post_publish.sh (deploy + the one scoreboard) once per new generation.
#   watch_publish.sh --any        the CURRENT generation, unless a run already claimed it, then
#                                 again for any newer one published meanwhile (the
#                                 plx-publish-watch.path user unit runs this on every publish)
#   watch_publish.sh --seed       mark every generation post-publish.log already handled, and the
#                                 CURRENT one, as claimed (once, before enabling the unit)
#   watch_publish.sh <generation> wait for that generation to be CURRENT, then post_publish (hand mode)
# post_publish.sh claims each generation ($S/runs/published/<gen>): no generation runs twice.
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
S=$PLX_SCRATCH; P=$PLX_STATE/projection; claims=$S/runs/published
current() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "$P/CURRENT" 2>/dev/null; }
case "${1:?generation id, --any or --seed}" in
  --seed)
    mkdir -p "$claims"
    for g in $(grep -o 'post_publish $PLX_STATE/projection/generations/[0-9]*' $S/runs/post-publish.log | awk -F/ '{print $NF}' | sort -u) $(current); do
      mkdir -p "$claims/$g" && echo "claimed $g"
    done;;
  --any)
    while g=$(current) && [ -n "$g" ] && [ ! -d "$claims/$g" ]; do
      echo "$(TZ=America/New_York date +%r) gen $g published: post_publish"
      bash $S/cfg/post_publish.sh "$P/generations/$g"
      echo "post_publish $g exit $?"
      [ -d "$claims/$g" ] || { echo "gen $g not claimed after post_publish: stop"; exit 1; }
    done;;
  *)
    want=$1
    while ! grep -q "\"generation\":\"$want\"" $P/CURRENT 2>/dev/null; do
      if ! systemctl is-active -q plx-ingest; then echo "$(TZ=America/New_York date +%r) plx-ingest not active"; fi
      timeout 60 tail -f /dev/null
    done
    echo "$(TZ=America/New_York date +%r) gen $want published"
    bash $S/cfg/post_publish.sh $P/generations/$want
    echo "post_publish exit $?";;
esac
