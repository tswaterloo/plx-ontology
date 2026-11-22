#!/bin/bash
# Restore test of the off-box physical backup on sb4ec (the escrow-key host never
# reads it). Newest complete base backup + the pg_receivewal archive -> a throwaway Postgres on
# sb4ec's own disk, replayed to the end of the archive and promoted; then:
#   pg_verifybackup of the base (every file's checksum against the backup manifest),
#   the migration ledger equals the box's, the big served tables hold rows.
# Prints one JSON line; exit 0 restorable, 1 a check failed, 2 no complete backup, 3 setup.
# The restored copy is deleted at the end (pass KEEP=1 to keep it for a look).
set -uo pipefail
SSHC=${PLX_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
DSN=${PLX_PG_DSN:-dbname=plx}
PORT=${RESTORE_PORT:-55700}
rs() { ssh -F "$SSHC" -o BatchMode=yes -o ConnectTimeout=20 sb4ec "$@"; }
B='$HOME/plx-replica/pg17/bin'
# RESTORE_DIR (a name under the sb4ec home): KEEP=1 with another name keeps a restored copy as a
# build copy / store without the next restore test deleting it
T='$HOME/'"${RESTORE_DIR:-plx-restore-test}"
# WAL_DIR (under the sb4ec home): the archive the copy replays (the receiver's folder since 11-22: plx-backup/wal-1122)
WAL=${WAL_DIR:-plx-backup/wal}
[[ "$WAL" =~ ^[a-z0-9][a-z0-9/_-]{0,60}$ ]] || { echo '{"restorable": false, "error": "bad WAL_DIR"}'; exit 3; }
[[ "${RESTORE_DIR:-plx-restore-test}" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo '{"restorable": false, "error": "bad RESTORE_DIR"}'; exit 3; }
t0=$(date +%s)
# a server in recovery refuses to replay with these below the primary's: read them from it
prim=$(psql -X -d "$DSN" -Atc "select string_agg(name || ' = ' || setting, E'\n') from pg_settings where name in ('max_connections', 'max_worker_processes', 'max_wal_senders', 'max_prepared_transactions', 'max_locks_per_transaction')")
[ -n "$prim" ] || { echo '{"restorable": false, "error": "primary_settings_unread"}'; exit 3; }
base=$(rs 'for d in $(ls -d $HOME/plx-backup/base-* 2>/dev/null | grep -v "\.log$" | sort -r); do grep -qx done $d.log 2>/dev/null && [ -f $d/backup_manifest ] && { echo $d; break; }; done')
[ -n "$base" ] || { echo '{"restorable": false, "error": "no_complete_base_backup"}'; exit 2; }
rs "set -e; rm -rf $T; mkdir -p -m 700 $T/run; mkdir -m 700 $T/pg; cd $T/pg
    zstd -q -d -c $base/base.tar.zst | tar -x
    # a backup taken with -X stream carries the WAL to its own end
    for w in $base/pg_wal.tar $base/pg_wal.tar.zst $base/pg_wal.tar.gz; do
      [ -f \$w ] || continue
      case \$w in *.zst) zstd -q -d -c \$w | tar -x -C pg_wal ;; *.gz) tar -xzf \$w -C pg_wal ;; *) tar -xf \$w -C pg_wal ;; esac
    done
    cp $base/backup_manifest $T/backup_manifest
    # SKIP_VERIFY=1 (a build copy in a hurry): the checksum pass runs beside the replay instead
    if [ "${SKIP_VERIFY:-0}" = 1 ]; then (nohup $B/pg_verifybackup -q -n -m $T/backup_manifest $T/pg > $T/verify.out 2>&1 || echo verify_failed > $T/verify.status) >/dev/null 2>&1 & else
    $B/pg_verifybackup -q -n -m $T/backup_manifest $T/pg > $T/verify.out 2>&1 || { echo verify_failed > $T/verify.status; }; fi
    rm -f postmaster.pid standby.signal; touch recovery.signal; : > postgresql.auto.conf
    cat > $T/postgresql.conf <<EOF
data_directory = '\$HOME/${RESTORE_DIR:-plx-restore-test}/pg'
hba_file = '\$HOME/${RESTORE_DIR:-plx-restore-test}/pg_hba.conf'
listen_addresses = ''
port = $PORT
unix_socket_directories = '\$HOME/${RESTORE_DIR:-plx-restore-test}/run'
unix_socket_permissions = 0700
shared_buffers = 8GB
$prim
hot_standby = on
restore_command = 'f=\$HOME/$WAL/%f; if [ -f \\\$f.lz4 ]; then \$HOME/plx-backup/tools/root/usr/bin/lz4 -q -d -c \\\$f.lz4 > %p; elif [ -f \\\$f ]; then cp \\\$f %p; else exit 1; fi'
recovery_target_timeline = 'latest'
recovery_target_action = 'promote'
fsync = off
archive_mode = off
shared_preload_libraries = ''
jit = off
EOF
    echo 'local all all peer' > $T/pg_hba.conf
    nohup $B/postgres -D $T/pg -c config_file=$T/postgresql.conf > $T/pg.log 2>&1 < /dev/null &" \
  || { echo '{"restorable": false, "error": "setup_failed"}'; exit 3; }
t1=$(date +%s)
# replayed and promoted: not in recovery any more (the archive ends at the receiver's last
# full segment); a server that shut down (recovery failed) ends the wait
up=$(rs "for i in \$(seq 1 5400); do r=\$($B/psql -X -h $T/run -p $PORT -d plx -Atc 'select pg_is_in_recovery()' 2>/dev/null); [ \"\$r\" = f ] && { echo f; break; }; grep -q 'database system is shut down' $T/pg.log && break; sleep 2; done")
t2=$(date +%s)
verify=$(rs "cat $T/verify.status 2>/dev/null || echo ok")
# the migration ledger is equal; the big served tables hold rows (counts move on the box
# after the backup, so they are compared as present, not equal)
q_ledger="select string_agg(name, ',' order by name) from plx_meta.migrations"
q_rows="select (select count(*) > 0 from search.chunks) and (select count(*) > 0 from biz.records) and (select count(*) > 0 from ontology.link_rows)"
problems=()
[ "$up" = f ] || problems+=("not_promoted")
[ "$verify" = ok ] || problems+=("pg_verifybackup_failed")
# the copy must have replayed from the archive, not only from WAL carried in the base
# (11-22: an escaped-away restore_command read nothing and two tests still passed)
arch=$(rs "grep -c 'restored log file' $T/pg.log" 2>/dev/null)
[ "${arch:-0}" -gt 0 ] 2>/dev/null || problems+=("archive_never_read")
if [ "$up" = f ]; then
  box=$(psql -X -d "$DSN" -Atc "$q_ledger" 2>/dev/null)
  got=$(rs "$B/psql -X -h $T/run -p $PORT -d plx -Atc \"$q_ledger\"" 2>/dev/null)
  [ -n "$box" ] && [ "$box" = "$got" ] || problems+=("ledger_differs")
  rows=$(rs "$B/psql -X -h $T/run -p $PORT -d plx -Atc \"$q_rows\"" 2>/dev/null)
  [ "$rows" = t ] || problems+=("served_tables_empty")
fi
[ "${KEEP:-0}" = 1 ] || rs "$B/pg_ctl -D $T/pg stop -m fast -w -t 120 >/dev/null 2>&1; rm -rf $T"
t3=$(date +%s)
ok=$([ ${#problems[@]} -eq 0 ] && echo true || echo false)
printf '{"restorable": %s, "base": "%s", "problems": [%s], "extract_s": %d, "replay_s": %d, "seconds": %d}\n' \
  "$ok" "$(basename "$base")" "$([ ${#problems[@]} -gt 0 ] && printf '"%s",' "${problems[@]}" | sed 's/,$//')" $((t1 - t0)) $((t2 - t1)) $((t3 - t0))
[ "$ok" = true ]
