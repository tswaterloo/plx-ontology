#!/bin/bash
# Run a commit's land module list (smoke set + extra modules) on the Sparks, mirroring land-fast.sh.
#   spark_test.sh <sha> [-t <module>|@<file>]... [--ids <file>]
#   env: SPARK_HOST=s7d3d|sb4ec|both (default s7d3d), SPARK_JOBS (16 workers per host; sb4ec capped at SPARK_SB4EC_CORES=12, pinned), SPARK_LOGS (/data/scratch/runs/spark)
#   --ids <file>: ready-made job ids (one per line) run verbatim instead of smoke + -t modules.
# Pushes only the git objects of <sha> (no secrets, no .env, no data) to ~/plx-tests/repo.git over ssh,
# runs in a fresh detached checkout against a throwaway socket-only Postgres 17 (+pgvector).
# SPARK_HOST=both splits the list across both hosts (greedy by recorded timings); an unreachable host is
# left out, none reachable = exit 96. Prints the log path and one combined "N modules: ..." summary;
# exit code = main_ci's (0 green; 1 = a module FAILed), 96-99 = setup failures (caller may fall back local).
set -uo pipefail
sha="" extra=() ids=""
while [ $# -gt 0 ]; do case "$1" in
  -t) extra+=("$2"); shift 2 ;; --ids) ids=$2; shift 2 ;; -h|--help) sed -n 2,9p "$0"; exit 2 ;; *) if [ -z "$sha" ]; then sha=$1; else extra+=("$1"); fi; shift ;; esac; done  # 11-14: bare names after <sha> = -t
[ -n "$sha" ] || { echo "usage: $0 <sha> [-t module|@file]... [--ids file]" >&2; exit 2; }
for m in "${extra[@]+"${extra[@]}"}"; do [[ "$m" =~ ^@?[A-Za-z0-9_./-]+$ ]] || { echo "not a module name: $m" >&2; exit 2; }; [[ "$m" == @* || "$m" == *.* ]] || { echo "module must be a full dotted path (e.g. operating.tests.test_x): $m" >&2; exit 2; }; done
[ -z "$ids" ] || [ -f "$ids" ] || { echo "no ids file $ids" >&2; exit 2; }
repo=${LAND_REPO:-/data/OntologyProject}; here=$(dirname "$(readlink -f "$0")")
logs=${SPARK_LOGS:-/data/scratch/runs/spark}; mkdir -p "$logs"
sha=$(git -C "$repo" rev-parse --verify -q "$sha^{commit}") || { echo "unknown commit $sha" >&2; exit 2; }
log="$logs/${sha:0:10}-$(date +%Y%m%d-%H%M%S).log"
ssh_cfg=${PLX_SPARK_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
started=$(date +%s)
SPARK_HOST=${SPARK_HOST:-s7d3d}  # unset: s7d3d, as before 11-14
case "$SPARK_HOST" in both) hosts=(s7d3d sb4ec) ;; s7d3d|sb4ec) hosts=("$SPARK_HOST") ;; *) echo "SPARK_HOST must be s7d3d|sb4ec|both" >&2; exit 2 ;; esac
# @file module lists are read here and sent as plain names
mods=(); for m in "${extra[@]+"${extra[@]}"}"; do
  if [[ "$m" == @* ]]; then while read -r l; do l=${l%%#*}; l=${l// /}; [ -n "$l" ] && mods+=("$l"); done < "${m#@}"; else mods+=("$m"); fi; done
# keep only reachable hosts
up=(); for h in "${hosts[@]}"; do ssh -F $ssh_cfg -o ConnectTimeout=8 -o BatchMode=yes $h true 2>/dev/null && up+=("$h") || echo "host $h unreachable" >> "$log"; done
[ ${#up[@]} -gt 0 ] || { echo "spark_test ${sha:0:10} exit 96: no spark reachable; log $log"; exit 96; }
n=${#up[@]}
run_host() {  # <host> <shard index>
  local h=$1 i=$2 R="ssh -F $ssh_cfg $1" shard=""
  [ "$n" -gt 1 ] && shard="SHARD=$i/$n"
  GIT_SSH_COMMAND="ssh -F $ssh_cfg" git -C "$repo" push -q $h:plx-tests/repo.git "$sha:refs/plx/$sha" \
    || { echo "push to $h failed" >&2; return 96; }
  $R 'mkdir -p ~/plx-tests' && scp -q -F $ssh_cfg "$here/spark_test_remote.sh" "$here/land_shard.py" $h:plx-tests/ \
  && { [ ! -f /data/scratch/land-timings.json ] || scp -q -F $ssh_cfg /data/scratch/land-timings.json $h:plx-tests/land-timings.json; } \
  && { [ -z "$ids" ] || scp -q -F $ssh_cfg "$ids" $h:plx-tests/ids-${sha:0:10}.txt; } \
  || { echo "copy to $h failed" >&2; return 96; }
  local idenv=""; [ -z "$ids" ] || idenv="IDS=\$HOME/plx-tests/ids-${sha:0:10}.txt"
  # At most SPARK_SB4EC_CORES (12) workers, pinned to that many CPUs
  local w=${SPARK_JOBS:-16} pin=""
  if [ "$h" = sb4ec ]; then local cap=${SPARK_SB4EC_CORES:-12}; [ "$w" -le "$cap" ] || w=$cap; pin="taskset -c 0-$((cap-1))"; fi
  $R "$idenv $shard $pin nice -n 5 bash ~/plx-tests/spark_test_remote.sh $sha $w ${mods[*]+${mods[*]}}"
}
pids=(); for i in "${!up[@]}"; do run_host "${up[$i]}" "$i" > "$log.${up[$i]}" 2>&1 & pids+=($!); done
code=0; for i in "${!up[@]}"; do wait "${pids[$i]}"; c=$?
  if [ "$c" -ge 96 ] && [ "$c" -le 99 ]; then code=$c; elif [ "$c" != 0 ] && [ "$code" -lt 96 ]; then code=$c; fi; done
for h in "${up[@]}"; do echo "== $h"; cat "$log.$h"; done >> "$log"
# one combined summary line in main_ci's format
tot=0 pass=0 kr=0 fail=0
for h in "${up[@]}"; do l=$(grep -aE '^[0-9]+ modules:' "$log.$h" | tail -1)
  [[ "$l" =~ ^([0-9]+)\ modules:\ ([0-9]+)\ pass,\ ([0-9]+)\ known\ red,\ ([0-9]+)\ FAIL ]] && { tot=$((tot+BASH_REMATCH[1])); pass=$((pass+BASH_REMATCH[2])); kr=$((kr+BASH_REMATCH[3])); fail=$((fail+BASH_REMATCH[4])); }; done
[ "$code" -ge 96 ] && [ "$code" -le 99 ] || echo "$tot modules: $pass pass, $kr known red, $fail FAIL; $(( $(date +%s) - started )) s on ${up[*]}" >> "$log"
summary=$(grep -aE '^[0-9]+ modules:' "$log" | tail -1)
echo "spark_test ${sha:0:10} exit $code ($(( $(date +%s) - started )) s): ${summary:-no summary}; log $log"
exit $code
