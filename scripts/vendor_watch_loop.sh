#!/bin/bash
# The vendor-mail pass (operating/vendor_watch.py) on a schedule: one pass every
# VENDOR_WATCH_INTERVAL seconds (default 2 h), each capped at VENDOR_WATCH_CAP seconds
# (default 90 min: the pass's own budget ends 5 min before, `timeout` kills it at the cap).
# Read only: no live source reader is built and no credential reaches the pass (every
# credential-shaped variable is unset after the env file is read). Start it as its own unit:
#   /data/scratch/cfg/heavy.sh vendor-watch 12G scripts/vendor_watch_loop.sh STATE HOST.json [ENV]
# ENV: an env file naming the people exclusion (PLX_EXCLUDED_PEOPLE_OIDS ...); without one
# every unanswered-request check is "not decided". Stop: touch STATE/vendor-watch.stop (takes effect within 30 s between
# passes, or when the pass running ends).
set -u
state=${1:?state dir}; config=${2:?host config}; envfile=${3:-}
repo=$(cd "$(dirname "$0")/.." && pwd)
interval=${VENDOR_WATCH_INTERVAL:-7200}
cap=${VENDOR_WATCH_CAP:-5400}
python=${VENDOR_WATCH_PYTHON:-python3}
budget=$(( cap > 600 ? cap - 300 : cap * 9 / 10 ))
while true; do
  start=$(date +%s)
  (
    if [ -n "$envfile" ]; then set -a; . "$envfile"; set +a; fi
    for v in $(compgen -e | grep -E 'KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|DSN'); do
      unset "$v"
    done
    cd "$repo" && exec timeout --kill-after=60 "$cap" "$python" -m operating.vendor_watch \
      --state-dir "$state" --config "$config" --budget-seconds "$budget"
  )
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) vendor pass exit $?"
  if [ -e "$state/vendor-watch.stop" ] || [ "${VENDOR_WATCH_ONCE:-0}" = 1 ]; then exit 0; fi
  # wait for the next pass in short steps: a stop file takes effect within a step
  next=$(( start + interval )); [ "$next" -gt "$(( $(date +%s) + 60 ))" ] || next=$(( $(date +%s) + 60 ))
  while [ "$(date +%s)" -lt "$next" ]; do
    if [ -e "$state/vendor-watch.stop" ]; then echo "stopped by $state/vendor-watch.stop"; exit 0; fi
    sleep "${VENDOR_WATCH_STEP:-30}"
  done
done
