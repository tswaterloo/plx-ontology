#!/bin/bash
# Install the precomputed plan sections end to end (which has
# the Spark ssh login and sudo -n; each root step is its own `sudo -n`). Nothing here writes to a
# business system; every phase prints what it did and stops on the first failure.
#
#   install_plan_resident.sh [--sha SHA] [--shards N] PHASE...
#   phases, in this order, each safe to run again:
#     check    Spark memory, the standby, disk, the release (no change)
#     env      /etc/plx/plan-resident.env (non-secret settings the runner's console starts with)
#     units    plx-plan-pull.service and plx-plan-resident@.service into /etc/systemd/system (not started)
#     views    remake the guarded views (the plan view gains its "current" column)
#     fill     the first full fill: every open order, all four families, on the Spark (plan_batch,
#              14 shards), then loaded in place of the table; prints the pass's wall time
#     fill-closed  the closed orders (the rest of the purchase orders), 1,500 ids a round on the Spark,
#              each round put in place with `plan_sections upsert` (the open orders' rows stay);
#              safe to leave running for hours: the app is not waiting on it
#     start    enable and start plx-plan-pull and plx-plan-resident@0-of-N .. @(N-1)-of-N
#     switch   PLX_PLAN_SECTIONS=1 in the API's environment (a drop-in) -- restart only through
#              plx-release (a warm switch), which this phase prints and does NOT run
#     measure  tools/plan_sections_measure.py (change-to-fresh, full pass, served-vs-fallback)
#   rollback:  sudo -n rm /etc/systemd/system/plx-api.service.d/plan-sections.conf && sudo -n systemctl
#              daemon-reload && plx-release restart ; the rows are inert with the switch off.
#   stop:      sudo -n systemctl disable --now plx-plan-pull 'plx-plan-resident@*'
# Order that matters: units and views before fill; fill before start (a load replaces the table);
# start before switch (a switch with an empty table only reads every plan live, as today).
set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
repo=$(cd "$here/../.." && pwd)
. "$here/../lib.sh" 2>/dev/null || true
sha="" shards=8 phases=()
while [ $# -gt 0 ]; do case "$1" in
  --sha) sha=$2; shift 2 ;; --shards) shards=$2; shift 2 ;;
  check|env|units|views|fill|fill-closed|start|switch|measure) phases+=("$1"); shift ;;
  *) sed -n 2,26p "$0"; exit 2 ;; esac; done
[ ${#phases[@]} -gt 0 ] || { sed -n 2,26p "$0"; exit 2; }
[[ "$shards" =~ ^[0-9]+$ ]] && [ "$shards" -ge 1 ] && [ "$shards" -le 16 ] || { echo "--shards is 1..16"; exit 2; }
releases=${PLX_RELEASES:-/data/plx-releases}
[ -n "$sha" ] || sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"])' "$releases/current/RELEASE.json")
envf=/etc/plx/plan-resident.env
fill_dir=${PLAN_FILL_DIR:-/data/scratch/plan-fill-$(date +%m%d%H%M)}
unset PLX_WORKBENCH_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY
say() { printf '== %s\n' "$*"; }

# The id-valued settings (who reads, who is excluded, the company ids) are never written in the repo: each is
# taken at install time from the operator's own env, the same sources the API reads, and the install stops by
# NAME (never a value) when one is missing. Order: this process's env, ~/.config/plx/plx.env, the API's
# /etc/plx/workbench-api.env, and the env file this phase installed last time.
ID_SETTINGS="PLX_BATCH_READER PLX_AUTOPILOT_BC_COMPANY_ID PLX_AUTOPILOT_BC_COMPANY_IDS PLX_EXCLUDED_PEOPLE_OIDS PLX_FULL_READERS"
env_value() {   # env_value NAME: the first non-empty value of NAME from the sources; prints it (callers never echo it)
  local n=$1 v f
  v=${!n:-}
  for f in ${ID_SOURCE_FILES:-"$HOME/.config/plx/plx.env" /etc/plx/workbench-api.env /etc/plx/plan-resident.env}; do
    [ -n "$v" ] && break
    v=$( { cat "$f" 2>/dev/null || sudo -n cat "$f" 2>/dev/null; } | sed -n "s/^$n=//p" | tail -1 | tr -d "\"'")
  done
  printf '%s' "$v"
}
fill_ids() {    # stdin: the env text with @NAME@ placeholders; stdout: values in; exit 1 naming what is missing
  local text n v missing=""
  text=$(cat)
  for n in $ID_SETTINGS; do
    v=$(env_value "$n")
    if [ -z "$v" ]; then missing="$missing $n"; continue; fi
    text=${text//"@$n@"/$v}
  done
  [ -z "$missing" ] || { echo "env: no value for$missing in the operator's env (~/.config/plx/plx.env, /etc/plx/workbench-api.env): stop" >&2; return 1; }
  printf '%s\n' "$text"
}

write_env() {   # the non-secret settings the app's console runs with (names checked: no key, token or secret)
  cat <<'EOF'
# Written by deploy/ops/install_plan_resident.sh. Non-secret settings only; read by plan_resident.sh and
# plan_batch.sh (they unset every credential-shaped variable before they start the Spark run).
PLX_PG_DSN=postgresql:///plx
PLX_BATCH_HOST_CONFIG=@HOME@/.config/plx/workbench-host-vince/host.json
PLX_BATCH_READER=@PLX_BATCH_READER@
PGUSER=plx_api
PLX_ALERTS_SQL=1
PLX_APP_HYBRID_SEARCH=0
PLX_APP_UNDERSTANDING=1
PLX_AUTOPILOT_BC_COMPANY_ID=@PLX_AUTOPILOT_BC_COMPANY_ID@
PLX_AUTOPILOT_BC_COMPANY_IDS=@PLX_AUTOPILOT_BC_COMPANY_IDS@
PLX_AUTOPILOT_ROLES_FILE=/data/plx-state/work-graph/roles-derived.json
PLX_BC_GO_LIVE_DATE=2026-03-28
PLX_EXCLUDED_PEOPLE_OIDS=@PLX_EXCLUDED_PEOPLE_OIDS@
PLX_FRONT_DOOR=off
PLX_FULL_READERS=@PLX_FULL_READERS@
PLX_HUMANIZE=rewrite
PLX_ONTOLOGY_PG=only
PLX_PAGES_DOOR=1
PLX_PG_BARE_MENTIONS=1
PLX_PG_FACTS_DIR=@HOME@/.cache/plx/pg-facts
PLX_PRECEDENCE_TABLE=/data/plx-state/precedence/measured.json
PLX_RERANK=off
PLX_SEARCH=hybrid
PLX_SYSTEM_ACTIVITY_FILE=/data/plx-state/system-activity.json
PLX_WORKBENCH_STATE_DIR=/data/plx-state
EOF
  # the Sparks the shards run on (shard I takes word I mod the count): PLX_PLAN_HOSTS="s7d3d sb4ec" to
  # split evenly; the box's puller reads this line too. Set it when running the env phase.
  # quoted: the file is sourced by sh (an unquoted list would run its second word as a command)
  printf 'PLX_PLAN_HOSTS="%s"\n' "${PLX_PLAN_HOSTS:-s7d3d}"
}

for phase in "${phases[@]}"; do
  case "$phase" in
  check)
    say "check: release $sha, Spark memory, standby, disk"
    free_gb=$(ssh -F /data/plx-state/ts-petra/ssh_config s7d3d "awk '/MemAvailable/{printf \"%d\", \$2/1048576}' /proc/meminfo")
    need=$(( shards * 3 + 16 ))
    echo "s7d3d MemAvailable ${free_gb} GB; $shards shards need ~$need GB (3 GB each + 16 reserve)"
    [ "$free_gb" -ge "$need" ] || { echo "not enough memory on s7d3d: use fewer --shards or wait"; exit 1; }
    echo "standby:"; psql postgresql:///plx -Atc "SELECT application_name, state, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)/1048576 AS lag_mb FROM pg_stat_replication"
    echo "disk /data free: $(df -BG --output=avail /data | tail -1 | tr -d ' ')"
    ;;
  env)
    say "env: $envf"
    (umask 077; write_env | sed "s#@HOME@#$HOME#g" | fill_ids > /tmp/plan-resident.env.$$) || { rm -f /tmp/plan-resident.env.$$; exit 1; }
     grep -v '^#' /tmp/plan-resident.env.$$ | grep -qiE 'KEY|TOKEN|SECRET|PASSWORD' && { echo "a credential-shaped name in the env: stop"; rm -f /tmp/plan-resident.env.$$; exit 1; }
    sudo -n install -m 644 -o root -g root /tmp/plan-resident.env.$$ "$envf"; rm -f /tmp/plan-resident.env.$$
    ;;
  units)
    say "units: render and install plx-plan-pull.service, plx-plan-resident@.service"
    tmp=$(mktemp -d); python3 "$repo/deploy/render.py" --out "$tmp" >/dev/null
    for u in plx-plan-pull.service plx-plan-resident@.service; do
      systemd-analyze verify "$tmp/$u" 2>&1 | grep -v -E 'not executable|^\s*$' || true
      sudo -n install -m 644 "$tmp/$u" /etc/systemd/system/$u
    done
    sudo -n systemctl daemon-reload; rm -rf "$tmp"
    ;;
  views)
    say "views: remake the guarded views (plan_section_items gains \"current\")"
    ( set -a; . /etc/plx/ingest.env; . "$HOME/.config/plx/plx.env"; set +a
      cd "$repo" && PLX_ONTOLOGY_PG=only python3 -m operating.pg.generic_views install )
    ;;
  fill)
    say "fill: every open order, all families, $fill_dir (14 shards)"
    mkdir -p "$fill_dir"; t0=$(date +%s)
    "$repo/deploy/spark/plan_batch.sh" --out "$fill_dir" --env-file "$envf" --shards 14 --sha "$sha"
    t1=$(date +%s); echo "batch on the Spark: $(( t1 - t0 )) s"
    ( set -a; . /etc/plx/ingest.env; . "$HOME/.config/plx/plx.env"; set +a
      cd "$repo" && PLX_ONTOLOGY_PG=only python3 -m operating.pg.plan_sections load "$fill_dir"/shard-*.jsonl )
    echo "full first pass: batch $(( t1 - t0 )) s + load $(( $(date +%s) - t1 )) s"
    ;;
  fill-closed)
    say "fill-closed: every purchase order record not open, in rounds of 1,500"
    ids=$fill_dir.closed-ids; mkdir -p "$fill_dir"
    psql postgresql:///plx -Atc "SELECT DISTINCT i.record_id FROM biz.record_page_items i JOIN biz.purchase_orders p ON p.handle = i.src_handle WHERE i.section = 'header' AND NOT (p.verdict IN ('late','unknown') OR p.status IN ('open','draft','unknown')) ORDER BY 1" > "$ids"
    echo "$(wc -l < "$ids") closed records"
    rm -rf "$ids.parts"; mkdir -p "$ids.parts"; split -l 1500 -d "$ids" "$ids.parts/r"
    for part in "$ids.parts"/r*; do
      out="$fill_dir/closed-$(basename "$part")"; t0=$(date +%s)
      "$repo/deploy/spark/plan_batch.sh" --out "$out" --env-file "$envf" --shards 8 --ids "$part" --sha "$sha"
      ( set -a; . /etc/plx/ingest.env; . "$HOME/.config/plx/plx.env"; set +a
        cd "$repo" && PLX_ONTOLOGY_PG=only python3 -m operating.pg.plan_sections upsert "$out"/shard-*.jsonl )
      echo "round $(basename "$part"): $(( $(date +%s) - t0 )) s"
    done
    ;;
  start)
    say "start: puller and $shards resident shards"
    sudo -n systemctl enable --now plx-plan-pull.service
    for i in $(seq 0 $(( shards - 1 ))); do sudo -n systemctl enable --now "plx-plan-resident@${i}-of-${shards}.service"; done
    systemctl list-units 'plx-plan-*' --no-legend
    ;;
  switch)
    say "switch: PLX_PLAN_SECTIONS=1 for the API"
    printf '[Service]\nEnvironment=PLX_PLAN_SECTIONS=1\n' | sudo -n install -D -m 644 /dev/stdin /etc/systemd/system/plx-api.service.d/plan-sections.conf
    sudo -n systemctl daemon-reload
    echo "now restart ONLY through the warm switch:  plx-release restart"
    echo "NOTE: with the release unchanged, plx-release restart 'keeps' the running API and ignores this drop-in; force it:  PLX_RELEASE_NO_ROLLBACK=1 plx-release restart  (then check the API process has PLX_PLAN_SECTIONS=1)"
    ;;
  measure)
    say "measure"
    ( cd "$repo" && PYTHONPATH="$repo" python3 tools/plan_sections_measure.py --hours 24 )
    ;;
  esac
done
