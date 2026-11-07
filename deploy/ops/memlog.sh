#!/bin/bash
# Every 60 s: available memory; under 30% available (or every 10 min) also the 12 biggest
# processes with their command lines, so the next memory spike names its cause.
# Log: $PLX_SCRATCH/runs/mem.log
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
log=$PLX_SCRATCH/runs/mem.log; i=0
while true; do
  avail=$(awk '/MemAvailable/{a=$2}/MemTotal/{t=$2}END{printf "%d %d", a/1048576, 100*a/t}' /proc/meminfo)
  set -- $avail
  echo "$(date -u +%FT%TZ) avail ${1}G ${2}% swap_used $(awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{printf "%.1fG",(t-f)/1048576}' /proc/meminfo)" >> $log
  if [ "$2" -lt 30 ] || [ $((i % 10)) -eq 0 ]; then
    ps -eo rss,pid,etimes,args --sort=-rss | head -13 | awk 'NR>1{printf "    %5.1fG pid %s age %ss %s\n", $1/1048576, $2, $3, substr($0, index($0,$4), 150)}' >> $log
  fi
  i=$((i+1)); sleep 60
done
