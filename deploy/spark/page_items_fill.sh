#!/bin/bash
# The record pages' first fill on a Spark build copy (speed specs SV2/SV3: "build on the standby copy"),
# never on the box: the copy is writable and private to s7d3d, so the fill's WAL and disk are the copy's.
#
#   page_items_fill.sh check                   the guards only (prints each; exit 3 on one that fails)
#   page_items_fill.sh fill <name> <sha>       check, clone the standby (deploy/spark/build_copy.sh clone: the standby
#                                              is down for the copy, ~minutes, started again on every exit path),
#                                              ship <sha>'s tree, apply migration 0064 and build the record items
#                                              (operating.pg.record_pages build: ensure + stage + page_items.apply)
#   page_items_fill.sh verify <name>           items per section, pages vs biz.records (a record with no header item
#                                              is listed by count), pages with no line item where the order has lines
#   page_items_fill.sh run <name> <sha> -- CMD  CMD on the copy with the tree of <sha> (page_equiv, the cold probe's API)
#   page_items_fill.sh drop <name>
#
# Guards (each printed; GUARDRAILS 1 "no silent limit": a guard that stops the fill says which and the number):
#   s7d3d free disk >= the standby's data + 300 GB (build_copy.sh clone checks it again), s7d3d 1-min load <= 30,
#   the box's replication lag to the standby < 8 GB (the standby is stopped for the copy and must catch up after).
# Nothing here writes to the box. The fill builds every purchase order's items (header, cell, lines, line_cell,
# linked); sections the plan adds later are built by their own builders on the same copy.
set -euo pipefail
SSHC=${PLX_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
HOST=s7d3d
here=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)
rs() { ssh -F "$SSHC" -o BatchMode=yes -o ConnectTimeout=20 "$HOST" "$@"; }
name_ok() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "bad name: $1" >&2; exit 2; }; }
BC="$here/deploy/spark/build_copy.sh"
PY='$HOME/plx-tests/venv/bin/python3'

check() {
  local bad=0 free used load lag
  free=$(rs "df -BG --output=avail \$HOME | tail -1 | tr -dc 0-9")
  used=$(rs "du -sBG \$HOME/plx-ai/pgdata | cut -f1 | tr -dc 0-9")
  echo "disk: s7d3d $free GB free, copy needs $((used + 300)) GB"; [ "$free" -gt $((used + 300)) ] || bad=1
  load=$(rs "cut -d' ' -f1 /proc/loadavg"); echo "load: s7d3d $load (limit 30)"
  awk -v l="$load" 'BEGIN{exit !(l<=30)}' || bad=1
  lag=$(psql -X -d "${PLX_PG_DSN:-dbname=plx}" -Atc "select coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)), -1)/1e9 from pg_stat_replication")
  echo "replication lag to the standby: $lag GB (limit 8; -1 = no standby connected)"
  awk -v l="$lag" 'BEGIN{exit !(l>=0 && l<8)}' || bad=1
  [ $bad -eq 0 ] || { echo "guard failed: nothing started" >&2; exit 3; }
}

cmd=${1:-}; shift || true
case "$cmd" in
check) check ;;
fill)
  name=$1 sha=$2; name_ok "$name"; sha=$(git -C "$here" rev-parse --verify "$sha^{commit}")
  check
  "$BC" status | grep -q "^$name " || "$BC" clone "$name"
  rs "rm -rf \$HOME/buildcopy/$name/code && mkdir -p \$HOME/buildcopy/$name/code"
  git -C "$here" archive "$sha" | rs "tar -x -C \$HOME/buildcopy/$name/code"
  "$BC" run "$name" -- bash -c "cd \$HOME/buildcopy/$name/code && PYTHONPATH=. $PY -m operating.pg.record_pages build"
  "$0" verify "$name"
  ;;
verify)
  name=$1; name_ok "$name"
  "$BC" run "$name" -- bash -c "cd \$HOME/buildcopy/$name/code && PYTHONPATH=. $PY -c \"
import psycopg, os
with psycopg.connect(os.environ['PLX_PG_DSN']) as c:
    for r in c.execute(\\\"select section, count(*), count(distinct page_key) from biz.page_items where page_kind='record' group by 1 order by 1\\\"): print('section', *r)
    print('records', c.execute(\\\"select count(*) from biz.records where biz and type='PurchaseOrder'\\\").fetchone()[0])
    print('pages_without_header', c.execute(\\\"select count(*) from biz.records r where r.biz and r.type='PurchaseOrder' and not exists (select 1 from biz.page_items i where i.page_kind='record' and i.page_key=r.record_id and i.section='header')\\\").fetchone()[0])
\""
  ;;
run)
  name=$1 sha=$2; name_ok "$name"; shift 2; [ "${1:-}" = "--" ] && shift
  sha=$(git -C "$here" rev-parse --verify "$sha^{commit}")
  rs "rm -rf \$HOME/buildcopy/$name/code && mkdir -p \$HOME/buildcopy/$name/code"
  git -C "$here" archive "$sha" | rs "tar -x -C \$HOME/buildcopy/$name/code"
  "$BC" run "$name" -- bash -c "cd \$HOME/buildcopy/$name/code && PYTHONPATH=. $*"
  ;;
drop) name=$1; name_ok "$name"; "$BC" drop "$name" ;;
*) sed -n 2,22p "$0"; exit 2 ;;
esac
