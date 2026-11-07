#!/bin/bash
# Run a heavy job (capture cycle, scoreboard, measurement) as its own system unit, outside the
# login session, with a memory cap: if it runs away, only it is killed (never the session, the
# agents or Postgres), and it survives a session/Claude crash. Usage:
#   heavy.sh <unit-name> <MemoryMax e.g. 24G> <command...>
# HEAVY_WAIT=1 blocks until the job ends (used by eval_slot.sh to hold its lock).
# Watch: systemctl status plx-<name>; journalctl -u plx-<name>; stop: sudo systemctl stop plx-<name>
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
name=${1:?unit name}; mem=${2:?MemoryMax}; shift 2
high=$(python3 -c "import re,sys;m=re.fullmatch(r'(\d+)G',sys.argv[1]);print(f'{int(m[1])*870}M' if m else sys.argv[1])" "$mem")
wait=(); [ "${HEAVY_WAIT:-0}" = 1 ] && wait=(--wait)
[ -n "${PLX_SEARCH:-}" ] && wait+=(-E PLX_SEARCH="$PLX_SEARCH")
exec sudo -n systemd-run "${wait[@]}" --unit="plx-$name" --uid=$PLX_USER --gid=$PLX_USER --collect \
  -p MemoryHigh="$high" -p MemoryMax="$mem" -p MemorySwapMax=4G -p OOMScoreAdjust=500 \
  -p WorkingDirectory=$PLX_SCRATCH -E PLX_HEAVY_UNIT="plx-$name" -E HOME=$PLX_HOME -E PATH=$PLX_VENV/bin:$PLX_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin \
  -- "$@"
