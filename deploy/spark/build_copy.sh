#!/bin/bash
# The Spark build copy: a big job computes on a throwaway writable copy of the s7d3d standby
# (its local disk, no WAL budget, no box load) and only its result tables come back.
#
#   build_copy.sh clone <name>     stop the standby cleanly (a restartpoint), copy its data dir
#                                  on s7d3d's own disk to ~/buildcopy/<name>/pg, restart the
#                                  standby (it resumes from its slot), start the copy writable on
#                                  its own socket (no TCP, fsync off, wal_level minimal)
#   build_copy.sh run <name> -- CMD...  run CMD on s7d3d with PGHOST/PGPORT/PGDATABASE = the copy
#   build_copy.sh ship <name> <schema.table> <schema.target>
#                                  the copy's table -> a NEW box table <target> (created from the
#                                  copy's column types; refused if it exists): binary COPY | zstd
#                                  over ssh (Spark -> box, the fast direction), loaded under the
#                                  WAL budget (operating.pg.wal_budget run) and the disk floor.
#                                  The caller builds indexes and swaps (rename / ATTACH).
#   build_copy.sh drop <name>      stop the copy and delete it
#   build_copy.sh status
#
# The copy is plaintext like the standby itself (same host, same login); it is never a backup.
# Env: PLX_PG_DSN (box, default dbname=plx), BUILD_COPY_PORT (55600), BUILD_COPY_MEM_GB (12).
set -euo pipefail
SSHC=${PLX_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
HOST=s7d3d
BIN='$HOME/plx-ai/pg17/usr/lib/postgresql/17/bin'
# the standby's own client tools need its libpq; zstd from ~/buildcopy/tools (no system one)
LDP='LD_LIBRARY_PATH=$HOME/plx-ai/pg17/usr/lib/aarch64-linux-gnu'
ZSTD='$HOME/buildcopy/tools/root/usr/bin/zstd'
PORT=${BUILD_COPY_PORT:-55600}
MEM=${BUILD_COPY_MEM_GB:-12}
DSN=${PLX_PG_DSN:-dbname=plx}
here=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)
rs() { ssh -F "$SSHC" -o BatchMode=yes -o ConnectTimeout=20 "$HOST" "$@"; }
name_ok() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "bad name: $1" >&2; exit 2; }; }
ident_ok() { [[ "$1" =~ ^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$ ]] || { echo "bad table: $1" >&2; exit 2; }; }
log() { echo "$(date '+%F %T') $*" >&2; }

cmd=${1:-}; shift || true
case "$cmd" in
clone)
  name=$1; name_ok "$name"
  free=$(rs "df -BG --output=avail \$HOME | tail -1 | tr -dc 0-9")
  used=$(rs "du -sBG \$HOME/plx-ai/pgdata | cut -f1 | tr -dc 0-9")
  [ "$free" -gt $((used + 300)) ] || { echo "s7d3d: $free GB free, copy needs $used + 300" >&2; exit 3; }
  rs "test ! -e \$HOME/buildcopy/$name" || { echo "copy $name exists" >&2; exit 3; }
  rs "test -x $ZSTD || (mkdir -p \$HOME/buildcopy/tools && cd \$HOME/buildcopy/tools && apt-get download zstd >/dev/null 2>&1 && dpkg -x zstd_*.deb root)"
  t0=$(date +%s)
  log "stopping the standby (clean: a restartpoint)"
  # from the stop on, the standby is started again on every exit path (11-21: a failed
  # check after the stop left it down 11 min)
  trap 'rs "systemctl --user start plx-spark-standby" || true' EXIT
  set +e
  # "[p]ostgres": the pattern must not match this ssh command's own shell
  rs "systemctl --user stop plx-spark-standby && ! pgrep -f \"bin/[p]ostgres -D \$HOME/plx-ai/pgdata\" >/dev/null"
  rc=$?
  t1=$(date +%s)
  [ $rc -eq 0 ] && rs "mkdir -p \$HOME/buildcopy/$name/run && cp -a \$HOME/plx-ai/pgdata \$HOME/buildcopy/$name/pg"
  rc=$?
  rs "systemctl --user start plx-spark-standby"
  trap - EXIT
  set -e
  t2=$(date +%s)
  log "standby down $((t2 - t1)) s (copy $((t2 - t1)) s, rc $rc)"
  [ $rc -eq 0 ] || { echo "copy failed rc $rc (standby restarted)" >&2; exit 4; }
  rs "cd \$HOME/buildcopy/$name/pg && rm -f standby.signal recovery.signal postmaster.pid backup_label.old && \
      : > postgresql.auto.conf && cat > \$HOME/buildcopy/$name/postgresql.conf <<EOF
data_directory = '\$HOME/buildcopy/$name/pg'
hba_file = '\$HOME/buildcopy/$name/pg_hba.conf'
listen_addresses = ''
port = $PORT
unix_socket_directories = '\$HOME/buildcopy/$name/run'
unix_socket_permissions = 0700
max_connections = 100
shared_buffers = ${MEM}GB
work_mem = 256MB
maintenance_work_mem = 16GB
max_worker_processes = 24
max_parallel_workers = 18
max_parallel_maintenance_workers = 16
max_parallel_workers_per_gather = 8
fsync = off
synchronous_commit = off
full_page_writes = off
wal_level = minimal
max_wal_senders = 0
archive_mode = off
max_wal_size = 64GB
hot_standby = off
shared_preload_libraries = 'pg_stat_statements'
jit = off
timezone = 'Etc/UTC'
EOF
      echo 'local all all peer' > \$HOME/buildcopy/$name/pg_hba.conf"
  rs "nohup $BIN/postgres -D \$HOME/buildcopy/$name/pg -c config_file=\$HOME/buildcopy/$name/postgresql.conf \
      > \$HOME/buildcopy/$name/pg.log 2>&1 < /dev/null & for i in \$(seq 1 600); do \
      $LDP $BIN/pg_isready -q -h \$HOME/buildcopy/$name/run -p $PORT && break; sleep 2; done; \
      $LDP $BIN/psql -X -h \$HOME/buildcopy/$name/run -p $PORT -d plx -Atc 'select pg_is_in_recovery()'" | grep -qx f \
    || { echo "copy did not come up writable (see s7d3d:~/buildcopy/$name/pg.log)" >&2; exit 5; }
  t3=$(date +%s)
  echo "{\"clone\": \"$name\", \"standby_down_s\": $((t2 - t1)), \"seconds\": $((t3 - t0)), \"gb\": $used}"
  ;;
run)
  name=$1; name_ok "$name"; shift; [ "${1:-}" = "--" ] && shift
  [ $# -gt 0 ] || { echo "no command" >&2; exit 2; }
  q=$(printf '%q ' "$@")
  rs "export PGHOST=\$HOME/buildcopy/$name/run PGPORT=$PORT PGDATABASE=plx PLX_PG_DSN='host='\$HOME/buildcopy/$name/run' port=$PORT dbname=plx'; $q"
  ;;
ship)
  name=$1; src=$2; dst=$3; name_ok "$name"; ident_ok "$src"; ident_ok "$dst"
  PSQLC="$LDP $BIN/psql -X -h \$HOME/buildcopy/$name/run -p $PORT -d plx -At"
  exists=$(psql -X -d "$DSN" -Atc "select to_regclass('$dst') is not null")
  [ "$exists" = f ] || { echo "$dst exists on the box: refused" >&2; exit 3; }
  cols=$(rs "$PSQLC -c \"select string_agg(format('%I %s', attname, format_type(atttypid, atttypmod)), ', ' order by attnum) from pg_attribute where attrelid = '$src'::regclass and attnum > 0 and not attisdropped\"")
  [ -n "$cols" ] || { echo "no columns for $src on the copy" >&2; exit 3; }
  bytes=$(rs "$PSQLC -c \"select pg_table_size('$src')\"")
  floor=$(( ${PLX_WRITE_MIN_FREE_GB:-75} ))
  free=$(df -BG --output=avail /data | tail -1 | tr -dc 0-9)
  [ $((free - bytes / 1073741824)) -gt "$floor" ] || { echo "/data $free GB free - $((bytes / 1073741824)) GB < floor $floor" >&2; exit 3; }
  psql -X -d "$DSN" -v ON_ERROR_STOP=1 -qc "CREATE TABLE $dst ($cols)"
  t0=$(date +%s.%N)
  load="ssh -F $SSHC -o BatchMode=yes $HOST \"$PSQLC -c 'COPY $src TO STDOUT (FORMAT binary)' | $ZSTD -q -3 -T8\" | zstd -q -d | psql -X -d '$DSN' -v ON_ERROR_STOP=1 -qc 'COPY $dst FROM STDIN (FORMAT binary)'"
  set +e
  ( cd "$here" && python3 -m operating.pg.wal_budget --dsn "$DSN" run -- bash -c "set -o pipefail; $load" )
  rc=$?
  set -e
  t1=$(date +%s.%N)
  [ $rc -eq 0 ] || { psql -X -d "$DSN" -qc "DROP TABLE IF EXISTS $dst"; echo "ship failed rc $rc: $dst dropped" >&2; exit 4; }
  n_src=$(rs "$PSQLC -c 'select count(*) from $src'"); n_dst=$(psql -X -d "$DSN" -Atc "select count(*) from $dst")
  [ "$n_src" = "$n_dst" ] || { echo "rows differ: copy $n_src, box $n_dst ($dst kept for a look)" >&2; exit 6; }
  python3 -c "import sys; b, s = int(sys.argv[1]), float(sys.argv[3]) - float(sys.argv[2]); print('{\"shipped\": \"%s\", \"rows\": %s, \"mb\": %.0f, \"seconds\": %.1f, \"mb_per_s\": %.1f}' % (sys.argv[4], sys.argv[5], b / 1e6, s, b / 1e6 / s))" "$bytes" "$t0" "$t1" "$dst" "$n_dst"
  ;;
drop)
  name=$1; name_ok "$name"
  rs "f=\$HOME/buildcopy/$name; test -d \$f || exit 0; $BIN/pg_ctl -D \$f/pg stop -m fast -w -t 120 >/dev/null 2>&1; rm -rf \$f"
  echo "{\"dropped\": \"$name\"}"
  ;;
status)
  rs "for d in \$HOME/buildcopy/*/; do [ -d \$d ] || continue; n=\$(basename \$d); up=\$($LDP $BIN/pg_isready -q -h \$d/run -p $PORT && echo up || echo down); echo \"\$n \$up \$(du -sh \$d/pg 2>/dev/null | cut -f1)\"; done"
  ;;
*) sed -n 2,22p "$0"; exit 2 ;;
esac
