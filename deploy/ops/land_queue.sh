#!/bin/bash
# One serial landing queue: each entry lands (affected tests), is logged, and pushed to the product repo before the next.
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
q() {
  local sha=$1 name=$2 msg=$3 rmsg=$4; shift 4
  out=$(bash $PLX_OPS/land.sh "$sha" "$name" "$msg" "$@" 2>&1 | tail -4); echo "$out"
  l=$(echo "$out" | grep -oE "LANDED [0-9a-f]+" | cut -d" " -f2)
  if [ -z "$l" ]; then echo "$(TZ=America/New_York date +%-I:%M\ %p): $name did NOT land: $(echo "$out" | tail -1)" >> $PLX_SCRATCH/OVERNIGHT-LOG.md; return 1; fi
  echo "$(TZ=America/New_York date +%-I:%M\ %p): $name landed as $l" >> $PLX_SCRATCH/OVERNIGHT-LOG.md
  SRC_REF=$l bash $PLX_OPS/rishi_push.sh "$rmsg" 2>&1 | grep -E "PUSHED|UNALLOWED|NOT|WORSE" | tail -2
}
# Usage: land_queue.sh <queue file>: one entry per line, fields split by "|":
#   sha|name|main message|product message|test modules (space separated)
# Lines starting with # are skipped; the queue stops at the first entry that does not land.
while IFS='|' read -r sha name msg rmsg tests; do
  case $sha in ''|'#'*) continue ;; esac
  # shellcheck disable=SC2086
  q "$sha" "$name" "$msg" "$rmsg" $tests || break
done < "${1:?queue file}"
