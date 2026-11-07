#!/bin/bash
# land_then_deploy.sh <sha> <name> "<main message>" "<product repo message>" [test modules...]
# Waits for any running landing or deploy unit, lands one reviewed SHA, pushes it to the product repo,
# then deploys main (schema must match the served generation).
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
sha=$1; name=$2; msg=$3; rmsg=$4; shift 4
cd $PLX_REPO
while systemctl --user is-active -q plx-land-queue plx-land-delta2 2>/dev/null || pgrep -f "scratch/cfg/land\.sh [0-9a-f]" >/dev/null; do timeout 30 tail -f /dev/null; done
for try in 1 2; do
  out=$(bash $PLX_OPS/land.sh "$sha" "$name" "$msg" "$@" 2>&1 | tail -4)
  echo "$(TZ=America/New_York date +%r) $name try $try: $out"
  l=$(echo "$out" | grep -oE "LANDED [0-9a-f]+" | awk '{print $2}')
  [ -n "$l" ] && { SRC_REF=$l bash $PLX_OPS/rishi_push.sh "$rmsg" 2>&1 | grep -E "PUSHED|UNALLOWED" | tail -2; break; }
  grep -E "^(FAIL|ERROR):" $PLX_SCRATCH/runs/land-$name.tests.txt | head -5
  echo "$out" | grep -q "main moved" || exit 1
done
[ -n "$l" ] || exit 1
# one release for every unit, on the served generation's schema (plx-release refuses a mismatch)
bash $PLX_OPS/deploy_compat.sh origin/main
