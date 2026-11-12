#!/bin/bash
# After a data catch-up cycle (plx-delta run by hand with the timers parked): verify it, then
# put the refresh back. The timers are RE-ARMED WHETHER OR NOT the cycle succeeded, so a failed
# catch-up never leaves the refresh parked (11-10: a failed cycle exited this script before the
# `start` of the timers, which never fires a timer whose elapse had passed; no refresh ran for
# a day). `restart`, not `start`: a timer that finished waiting needs its next run recomputed.
#
#   deploy/ops/after_catchup.sh [--log FILE]
#
# Exit 0: the cycle succeeded and the timers are armed; 1: the cycle did not succeed (the
# timers are armed anyway and the line says so); 2: a timer could not be re-armed.
# Settings: PLX_AFTER_LOG (default $PLX_RUNS/after-catchup.log). Needs sudo -n for systemctl.
set -uo pipefail
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
. "$here/../lib.sh"
LOG=${PLX_AFTER_LOG:-${PLX_RUNS:-/tmp}/after-catchup.log}
[ "${1:-}" = "--log" ] && LOG=$2
TIMERS="plx-delta.timer plx-search-follow.timer plx-search-follow.path plx-proactive.timer plx-proactive-inc.timer plx-proactive-inc.path plx-proactive-nightly.timer plx-proactive-whole.timer plx-truth-check.timer plx-truth-check.path"
log() { echo "$(TZ=America/New_York date '+%-I:%M %p') $*" | tee -a "$LOG"; }

result=$(systemctl show -p Result --value plx-delta.service 2>/dev/null)
state=$(systemctl show -p ActiveState --value plx-delta.service 2>/dev/null)
log "delta: result=${result:-unknown} state=${state:-unknown}"
status=0
if [ "$result" != success ] || [ "$state" = activating ] || [ "$state" = failed ]; then
  log "DELTA DID NOT SUCCEED: re-arming the timers anyway (the next cycle retries)"
  status=1
fi
# shellcheck disable=SC2086
if sudo -n systemctl restart $TIMERS >>"$LOG" 2>&1; then
  log "timers re-armed: $(systemctl is-active $TIMERS | sort | uniq -c | tr '\n' ' ')"
else
  log "TIMER RESTART FAILED: refresh is parked until $TIMERS are restarted"
  exit 2
fi
for t in $TIMERS; do
  case $t in *.timer)
    next=$(systemctl show -p NextElapseUSecRealtime -p NextElapseUSecMonotonic "$t" 2>/dev/null | tr '\n' ' ')
    case "$next" in *"=infinity"*"=infinity"*|*"=0 "*"=0 "*|"") log "WARNING: $t has no next run" ;; esac ;;
  esac
done
exit $status
