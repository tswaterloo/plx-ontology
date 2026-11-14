#!/bin/bash
# Opt-in step after a landing's "LANDED <sha>" push (tools/land.sh, deploy/ops/land_batch.sh):
#   rishi_after_land.sh <landed-sha> "<landing message>"
# Does nothing unless PLX_RISHI_PUSH=1. Then runs
#   SRC_REF=<landed sha> deploy/ops/rishi_push.sh "<landing message>"
# and prints one line: "RISHI PUSHED|UNALLOWED|WORSE|FAILED: <last line of its output>"
# (UNALLOWED: the scrub found an unallowed hit; WORSE: a test is worse scrubbed; FAILED:
# anything else, e.g. a secret pattern or a rejected push). It always exits 0: the landing
# on main is done and never fails because the second push did not happen.
# RISHI_PUSH overrides the script (the tests stub it); RISHI_PUSH_TIMEOUT bounds it (1800 s).
[ "${PLX_RISHI_PUSH:-}" = 1 ] || exit 0
sha=${1:?landed sha}; msg=${2:?landing message}
push=${RISHI_PUSH:-$(dirname "$(readlink -f "$0")")/rishi_push.sh}
out=$(SRC_REF="$sha" timeout "${RISHI_PUSH_TIMEOUT:-1800}" "$push" "$msg" 2>&1 < /dev/null) && rc=0 || rc=$?
last=$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | tail -1 | cut -c1-300)
if [ "$rc" = 0 ] && printf '%s\n' "$out" | grep -q '^PUSHED '; then word=PUSHED
elif printf '%s\n' "$out" | grep -q 'CODE SCRUB NOT CLEAN\|DOC SCRUB VERIFICATION FAILED'; then word=UNALLOWED
elif printf '%s\n' "$out" | grep -q 'SCRUBBED TESTS WORSE'; then word=WORSE
else word=FAILED
fi
echo "RISHI $word: $last"
exit 0
