#!/bin/bash
# Run a commit's test modules on the Sparks, mirroring land-fast.sh.
#   spark_test.sh <sha> [-t <module>|@<file>]... [--affected <base>] [--ids <file>] [--no-wait]
#   spark_test.sh --status        the slot queue: held/free slots, what runs, who waits (no wait)
#   --affected <base>: also the modules tools/affected_tests.py selects for <sha> against <base>, computed
#     once per (tree, base) for every caller (tools/run_cache.py affected; "ALL" runs every test module).
#   --app [typecheck|test]: run the app's `npm run typecheck` and vitest for <sha> on one Spark (SPARK_HOST, default
#     sb4ec), node_modules cached per package-lock.json (spark_app_remote.sh); exit 0 green, 1 a failure, 96-98 setup.
#   --ids <file>: ready-made job ids (one per line) run verbatim (the lander's list, smoke included).
#   --no-wait (or SPARK_NO_WAIT=1): no slot free -> print the queue and exit 75 instead of waiting.
#   The lander runs it on every landing.
#     SPARK_CACHE=0 runs every module even when it passed on this tree (tools/run_cache.py).
#     Both at most (MemAvailable - SPARK_MEM_RESERVE_GB 16) / SPARK_WORKER_GB 2,
#     and the run's scope holds MemoryMax = MemAvailable - reserve. SPARK_JOBS sets the count (still memory-bounded).
# Pushes only the git objects of <sha> (no secrets, no .env, no data) to ~/plx-tests/repo.git over ssh,
# plus origin/main as refs/remotes/origin/main (tests that diff against main, e.g. tools/tests/test_timing_policy,
# otherwise fall back to a dated baseline), and
# runs in a fresh detached checkout against a throwaway socket-only Postgres 17 (+pgvector).
# A test id that passed on this commit's tree (same tree = same code and data) on a Spark within 24 h
# is not run again: the host reports it as "cached pass" and counts it as a pass. Every complete result
# is recorded for the next run of that tree (the reviewer, the lander, "red on base too":
# python3 tools/run_cache.py base-known --base origin/main <module>...).
# SPARK_HOST=both splits the list across both hosts (greedy by recorded timings); an unreachable host is
# left out, none reachable = exit 96. Prints the log path and one combined "N modules: ..." summary;
# exit code = main_ci's (0 green; 1 = a module FAILed), 96-99 = setup failures (caller may fall back local).
set -uo pipefail
here=$(dirname "$(readlink -f "$0")")
cache_py="$here/../../tools/run_cache.py"
[ -f "$cache_py" ] || { echo "spark_test.sh runs from a repository checkout or release (no tools/run_cache.py beside it)" >&2; exit 2; }
slot_dir=${SPARK_SLOT_DIR:-/data/scratch}
sha="" extra=() ids="" affected="" nowait=${SPARK_NO_WAIT:-0} app="" appwhat=both
while [ $# -gt 0 ]; do case "$1" in
  --app) app=1; if [ "${2:-}" = typecheck ] || [ "${2:-}" = test ]; then appwhat=$2; shift; fi; shift ;;
  -t) extra+=("$2"); shift 2 ;; --ids) ids=$2; shift 2 ;; --affected) affected=$2; shift 2 ;; --no-wait) nowait=1; shift ;;
  --status) exec python3 "$cache_py" slots ;;
  -h|--help) sed -n 2,21p "$0"; exit 2 ;; *) if [ -z "$sha" ]; then sha=$1; else extra+=("$1"); fi; shift ;; esac; done  # 11-14: bare names after <sha> = -t
[ -n "$sha" ] || { echo "usage: $0 <sha> [-t module|@file]... [--affected base] [--ids file] | --status" >&2; exit 2; }
for m in "${extra[@]+"${extra[@]}"}"; do [[ "$m" =~ ^@?[A-Za-z0-9_./-]+$ ]] || { echo "not a module name: $m" >&2; exit 2; }; [[ "$m" == @* || "$m" == *.* ]] || { echo "module must be a full dotted path (e.g. operating.tests.test_x): $m" >&2; exit 2; }; done
[ -z "$ids" ] || [ -f "$ids" ] || { echo "no ids file $ids" >&2; exit 2; }
[ -z "$ids" ] || SPARK_LANDER=${SPARK_LANDER:-1}   # --ids is the lander's form (land-fast.sh, a suite fill)
repo=${LAND_REPO:-/data/OntologyProject}
logs=${SPARK_LOGS:-/data/scratch/runs/spark}; mkdir -p "$logs"
sha=$(git -C "$repo" rev-parse --verify -q "$sha^{commit}") || { echo "unknown commit $sha" >&2; exit 2; }
tree=$(git -C "$repo" rev-parse --verify -q "$sha^{tree}")
log="$logs/${sha:0:10}-$(date +%Y%m%d-%H%M%S).log"
ssh_cfg=${PLX_SPARK_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
if [ -n "$app" ]; then   # the app's typecheck + vitest on a Spark: nothing runs on this box
  h=${SPARK_HOST:-sb4ec}; [ "$h" != both ] || h=sb4ec; [[ "$h" == s7d3d || "$h" == sb4ec ]] || { echo "SPARK_HOST must be s7d3d|sb4ec" >&2; exit 2; }
  R="ssh -F $ssh_cfg -o ConnectTimeout=8 -o BatchMode=yes $h"
  $R true 2>/dev/null || { echo "spark_test --app ${sha:0:10} exit 96: $h unreachable"; exit 96; }
  GIT_SSH_COMMAND="ssh -F $ssh_cfg" git -C "$repo" push -q -f $h:plx-tests/repo.git "$sha:refs/plx/$sha" || { echo "push to $h failed" >&2; exit 96; }
  f="$here/spark_app_remote.sh"; rh=$(sha256sum "$f" | cut -c1-12)
  scp -q -F $ssh_cfg "$f" $h:plx-tests/.spark_app_remote.$rh.$$ && $R "mv -f ~/plx-tests/.spark_app_remote.$rh.$$ ~/plx-tests/spark_app_remote.$rh.sh" || { echo "copy to $h failed" >&2; exit 96; }
  $R "pin=; systemd-run --user --scope -q true >/dev/null 2>&1 && pin='systemd-run --user --scope -q -p CPUWeight=100 -p MemoryMax=8G --'; \$pin bash ~/plx-tests/spark_app_remote.$rh.sh $sha $appwhat 2>&1" | tee "$log"
  exit "${PIPESTATUS[0]}"
fi
tmpd=$(mktemp -d); run_file=""
trap 'rm -rf "$tmpd"; [ -z "$run_file" ] || rm -f "$run_file"' EXIT
if [ -n "$affected" ]; then
  python3 "$cache_py" affected --repo "$repo" --sha "$sha" --base "$affected" > "$tmpd/affected.txt" 2> "$tmpd/affected.err" \
    || { tail -5 "$tmpd/affected.err" >&2; echo "affected selection failed" >&2; exit 2; }
  extra+=("@$tmpd/affected.txt"); echo "affected against $affected: $(grep -c . "$tmpd/affected.txt") modules" >&2
fi
# @file module lists are read here and sent as plain names ("ALL" is expanded on the host)
mods=(); for m in "${extra[@]+"${extra[@]}"}"; do
  if [[ "$m" == @* ]]; then while read -r l; do l=${l%%#*}; l=${l// /}; [ -n "$l" ] && mods+=("$l"); done < "${m#@}"; else mods+=("$m"); fi; done
nids=${#mods[@]}; [ -z "$ids" ] || nids=$(grep -c . "$ids")
# The run's status record (spark_test.sh --status): waiting, then running on a slot.
status() {  # <state> [slot]
  [ -n "$run_file" ] || { mkdir -p "$slot_dir/spark-runs" 2>/dev/null && run_file="$slot_dir/spark-runs/$$.json"; } || return 0
  printf '{"pid": %s, "sha": "%s", "ids": %s, "state": "%s", "slot": "%s", "since": %s, "who": "%s"}\n' \
    $$ "$sha" "$nids" "$1" "${2:-}" "$(date +%s)" "$(basename "$PWD" | tr -cd 'A-Za-z0-9._-')" > "$run_file.tmp" && mv -f "$run_file.tmp" "$run_file"
}
# 11-18 owner: no slot pins its CPUs any more (workers come from the host's free cores).
if [ "${SPARK_LANDER:-0}" != 1 ] && [ -z "${SPARK_SLOT_HELD:-}" ]; then
  status waiting
  for _i in $(seq 1 7200); do for _s in 1 2 3 4; do exec {SPARK_SLOT_FD}>"$slot_dir/spark-slot.$_s.lock"; flock -n $SPARK_SLOT_FD && { export SPARK_SLOT_HELD=$_s; break 2; }; exec {SPARK_SLOT_FD}>&-; done
    if [ "$nowait" = 1 ]; then python3 "$cache_py" slots >&2; echo "spark_test ${sha:0:10}: no Spark slot free (exit 75; --status shows the queue)" >&2; exit 75; fi
    sleep 2; done
  [ -n "${SPARK_SLOT_HELD:-}" ] || { echo "spark_test ${sha:0:10}: no Spark slot in 4 h" >&2; exit 75; }
fi
status running "${SPARK_SLOT_HELD:-}"
# The lander
# keeps the whole budget. A slot another run holds is one whose lock cannot be taken here.
lanes=1
if [ "${SPARK_LANDER:-0}" != 1 ]; then lanes=0
  for _s in 1 2 3 4; do if [ "$_s" = "${SPARK_SLOT_HELD:-}" ]; then lanes=$((lanes+1)); continue; fi
    exec {_probe_fd}>"$slot_dir/spark-slot.$_s.lock"; flock -n $_probe_fd || lanes=$((lanes+1)); exec {_probe_fd}>&-; done
  [ "$lanes" -ge 1 ] || lanes=1
fi
started=$(date +%s)
SPARK_HOST=${SPARK_HOST:-both}  # both Sparks (s7d3d unified memory: tests starve its GPU when it embeds)
case "$SPARK_HOST" in both) hosts=(s7d3d sb4ec) ;; s7d3d|sb4ec) hosts=("$SPARK_HOST") ;; *) echo "SPARK_HOST must be s7d3d|sb4ec|both" >&2; exit 2 ;; esac
# ids that already passed on this tree (tools/run_cache.py): the hosts skip them
skip="$tmpd/passed.txt"; : > "$skip"
if [ "${SPARK_CACHE:-1}" != 0 ]; then python3 "$cache_py" passed --tree "$tree" --env spark > "$skip" 2>/dev/null || : > "$skip"; fi
# keep only reachable hosts
up=(); for h in "${hosts[@]}"; do ssh -F $ssh_cfg -o ConnectTimeout=8 -o BatchMode=yes $h true 2>/dev/null && up+=("$h") || echo "host $h unreachable" >> "$log"; done
[ ${#up[@]} -gt 0 ] || { echo "spark_test ${sha:0:10} exit 96: no spark reachable; log $log"; exit 96; }
n=${#up[@]}
run_host() {  # <host> <shard index>
  local h=$1 i=$2 R="ssh -F $ssh_cfg $1" shard=""
  [ "$n" -gt 1 ] && shard="SHARD=$i/$n"
  GIT_SSH_COMMAND="ssh -F $ssh_cfg" git -C "$repo" push -q -f $h:plx-tests/repo.git "$sha:refs/plx/$sha" \
    "$(git -C "$repo" rev-parse --verify -q refs/remotes/origin/main || echo "$sha"):refs/remotes/origin/main" \
    || { echo "push to $h failed" >&2; return 96; }
  # Concurrent runs overwrote ~/plx-tests/spark_test_remote.sh mid-read (bash "unexpected EOF" -> a host's half never ran).
  #  The scripts go to content-hashed names, copied to a temp name then renamed atomically.
  local files=("$here/spark_test_remote.sh" "$here/land_shard.py" "$cache_py")
  local rh f b; rh=$(cat "${files[@]}" | sha256sum | cut -c1-12)
  $R "mkdir -p ~/plx-tests/r-$rh" || { echo "copy to $h failed" >&2; return 96; }
  for f in "${files[@]}"; do b=$(basename "$f")
    $R "test -s ~/plx-tests/r-$rh/$b" 2>/dev/null || { scp -q -F $ssh_cfg "$f" $h:plx-tests/r-$rh/.$b.$$ && $R "mv -f ~/plx-tests/r-$rh/.$b.$$ ~/plx-tests/r-$rh/$b"; } \
      || { echo "copy to $h failed" >&2; return 96; }; done
  { [ ! -f /data/scratch/land-timings.json ] || scp -q -F $ssh_cfg /data/scratch/land-timings.json $h:plx-tests/land-timings.json; } \
  && { [ -z "$ids" ] || scp -q -F $ssh_cfg "$ids" $h:plx-tests/ids-${sha:0:10}-$$.txt; } \
  && { [ ! -s "$skip" ] || scp -q -F $ssh_cfg "$skip" $h:plx-tests/skip-${sha:0:10}-$$.txt; } \
  || { echo "copy to $h failed" >&2; return 96; }
  local idenv=""; [ "${SPARK_SMOKE:-0}" = 1 ] || idenv="NOSMOKE=1"; [ -z "$ids" ] || idenv="IDS=\$HOME/plx-tests/ids-${sha:0:10}-$$.txt"
  [ ! -s "$skip" ] || idenv="$idenv SKIP=\$HOME/plx-tests/skip-${sha:0:10}-$$.txt"
  # 11-18 owner (NO THROTTLING): no nice, no taskset, no core cap. The host's cores, load and free memory now. The memory guard
  # stays: workers bounded by MemAvailable less a reserve (the GPU shares it), and the run's scope capped there.
  local probe cores load mem scope w pin busy memw cap cw budget per=${SPARK_WORKER_GB:-2} keep=${SPARK_MEM_RESERVE_GB:-16}
  [[ "$per" =~ ^[1-9][0-9]*$ ]] || per=2; [[ "$keep" =~ ^[0-9]+$ ]] || keep=16   # a bad setting: the default
  probe=$($R 'echo "$(nproc) $(cut -d" " -f1 /proc/loadavg) $(awk "/MemAvailable/ {print int(\$2/1048576)}" /proc/meminfo) $(systemd-run --user --scope -q -p CPUWeight=100 true >/dev/null 2>&1 && echo 1 || echo 0)"' 2>/dev/null)
  read -r cores load mem scope _ <<< "$probe"
  if ! [[ "$cores" =~ ^[1-9][0-9]*$ && "$load" =~ ^[0-9]+(\.[0-9]+)?$ && "$mem" =~ ^[0-9]+$ && "$scope" =~ ^[01]$ ]]; then
    echo "host $h: unreadable resources '${probe//$'\n'/ }': 4 workers, no CPU weight or memory cap" >&2
    cores=4 load=0 mem=0 scope=0; memw=4
  else
    budget=$(( (mem - keep) / lanes ))   # a lane's share of the free memory (above)
    memw=$(( budget / per )); [ "$memw" -ge 1 ] || memw=1
  fi
  if [ "${SPARK_LANDER:-0}" = 1 ]; then w=$cores cw=${SPARK_LANDER_WEIGHT:-1000}; [[ "$cw" =~ ^[1-9][0-9]{0,3}$ ]] || cw=1000
  else busy=$(awk -v l="$load" 'BEGIN { printf "%d", l + 0.5 }'); w=$(( cores - busy )); [ "$w" -ge 2 ] || w=2; cw=100; fi
  [[ ! "${SPARK_JOBS:-}" =~ ^[1-9][0-9]*$ ]] || w=$SPARK_JOBS
  [ "$w" -le "$memw" ] || w=$memw
  pin=""
  if [ "$scope" = 1 ]; then
    cap=$budget; [ "$cap" -ge "$per" ] || cap=$per
    # oom_score_adj 800: a test run is killed before the host's standby (11-22 OOM on s7d3d)
    pin="systemd-run --user --scope -q -p CPUWeight=$cw -p MemoryMax=${cap}G -- choom -n 800 --"
  fi
  echo "host $h: $cores cores, load $load, ${mem} GB free -> $w workers${pin:+, CPU weight $cw, memory cap ${cap:-} GB}"
  $R "$idenv $shard $pin bash ~/plx-tests/r-$rh/spark_test_remote.sh $sha $w ${mods[*]+${mods[*]}}"
  local rc=$?; $R "rm -f ~/plx-tests/ids-${sha:0:10}-$$.txt ~/plx-tests/skip-${sha:0:10}-$$.txt" 2>/dev/null; return $rc
}
pids=(); for i in "${!up[@]}"; do run_host "${up[$i]}" "$i" > "$log.${up[$i]}" 2>&1 & pids+=($!); done
code=0; for i in "${!up[@]}"; do wait "${pids[$i]}"; c=$?
  if [ "$c" -ge 96 ] && [ "$c" -le 99 ]; then code=$c; elif [ "$c" != 0 ] && [ "$code" -lt 96 ]; then code=$c; fi; done
for h in "${up[@]}"; do echo "== $h"; cat "$log.$h"; done >> "$log"
# every complete host report is recorded for the next run of this tree (never the cached lines)
hl=(); for h in "${up[@]}"; do hl+=("$log.$h"); done
python3 "$cache_py" record --tree "$tree" --env spark --source "$log" "${hl[@]}" >> "$log" 2>&1 || true
# one combined summary line in main_ci's format; cached passes count as passes
tot=0 pass=0 kr=0 fail=0 cached=0
for h in "${up[@]}"; do l=$(grep -aE '^[0-9]+ modules:' "$log.$h" | tail -1)
  # A host with no summary is a FAIL, never silently dropped from the totals
  [ -n "$l" ] || { fail=$((fail+1)); echo "HOST $h GAVE NO RESULT (counted as 1 FAIL)" >> "$log"; [ "$code" != 0 ] || code=1; }
  # A module named on the command line that does not exist is a FAIL (a typo silently skipped tests and looked green)
  mm=$(grep -ao "skipped modules that do not exist in the tree: \[[^]]*\]" "$log.$h" | head -1 | sed "s/.*tree: //")
  if [ -n "$mm" ] && [ "$mm" != "[]" ]; then fail=$((fail+1)); echo "HOST $h: MODULES NOT FOUND $mm (counted as 1 FAIL)" >> "$log"; [ "$code" != 0 ] || code=1; fi
  ch=$(grep -aoE '^cached: [0-9]+' "$log.$h" | tail -1 | cut -d' ' -f2); [ -z "$l" ] || cached=$((cached+${ch:-0}))
  [[ "$l" =~ ^([0-9]+)\ modules:\ ([0-9]+)\ pass,\ ([0-9]+)\ known\ red,\ ([0-9]+)\ FAIL ]] && { tot=$((tot+BASH_REMATCH[1])); pass=$((pass+BASH_REMATCH[2])); kr=$((kr+BASH_REMATCH[3])); fail=$((fail+BASH_REMATCH[4])); }; done
[ "$code" -ge 96 ] && [ "$code" -le 99 ] || echo "$((tot+cached)) modules: $((pass+cached)) pass, $kr known red, $fail FAIL; $(( $(date +%s) - started )) s on ${up[*]}; $((tot)) run, $cached cached" >> "$log"
summary=$(grep -aE '^[0-9]+ modules:' "$log" | tail -1)
echo "spark_test ${sha:0:10} exit $code ($(( $(date +%s) - started )) s): ${summary:-no summary}; log $log"
exit $code
