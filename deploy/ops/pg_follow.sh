#!/bin/bash
# Postgres ontology follower, owner's plan of 11-06: loads the served
# generation now (resumable, 150 GB floor, 8 workers: 14 left 1 GB of RAM free on 11-06) once /data has room, then applies
# every delta layer each minute; reloads in the background when a new generation is
# served. Started by the pg-cutover agent; stop with kill.
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
echo $$ > $PLX_SCRATCH/pg-cutover/follow_now.pid
# text chunks (~20 GB) wait for the new build to publish and gen 11 to be pruned: disk
export PLX_ONTOLOGY_DEFER=${PLX_ONTOLOGY_DEFER-text_chunks}
# the loader keeps its own 150 GB floor (a reload stops there, resumable)
while true; do
  cd "$PLX_CURRENT"   # each pass on the current release (deploy/release/plx-release)
  echo "$(date -u +%FT%TZ) follow"
  $PLX_VENV/bin/python3 -m operating.pg.ontology_delta --follow $PLX_STATE --min-free-gb 150 --workers ${PG_WORKERS:-8} 2>&1 | tail -30
  echo "$(date -u +%FT%TZ) follow exit=${PIPESTATUS[0]}"
  sleep 60
done
