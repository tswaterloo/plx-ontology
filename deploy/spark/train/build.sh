#!/bin/bash
# build.sh TRAIN VIEW...: on the Spark holding ~/train-<TRAIN>/pg (copy.sh), promote the copy into
# a writable server of its own (port 55721, own socket dir, peer auth only, db and role statement
# and idle timeouts 0: the cause of the 11-23 attachments failure), then build each VIEW whole, in
# order, with the train's code in ~/train-<TRAIN>/code (git archive of the train SHA) and the
# capture extract in ~/train-<TRAIN>/state (deploy/spark/train/extract.py). One line per step in
# steps.log with rc, seconds, the print the build recorded and row counts; a step with rc != 0
# stops the chain. The server stays up (ship reads it) until ~/train-<TRAIN>/keep is removed.
# Secrets are unset; the build reads nothing but the copy and the extract.
set -uo pipefail
TRAIN=${1:?usage: build.sh TRAIN VIEW...}; shift
H=$HOME/train-$TRAIN; S=$H/steps.log; B=$HOME/plx-ai/pg17/usr/lib/postgresql/17/bin; PORT=55721
export LD_LIBRARY_PATH=$HOME/plx-ai/pg17/usr/lib/aarch64-linux-gnu
log() { echo "$(date -u +%FT%TZ) $*" >> "$S"; }
grep -qx done "$H/copy.log" || { log no_copy; exit 1; }
[ -s "$H/state/mail-capture/capture.db" ] || { log no_extract; exit 1; }
[ -d "$H/code/operating" ] || { log no_code; exit 1; }
avail=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
log "memory available ${avail} GB"
[ "$avail" -ge "${TRAIN_MIN_MEM_GB:-40}" ] || { log "refused: ${avail} GB available, under ${TRAIN_MIN_MEM_GB:-40}"; exit 1; }
mkdir -p -m 700 "$H/run"; cd "$H/pg" || exit 1
rm -f standby.signal postmaster.pid; touch recovery.signal; : > postgresql.auto.conf; rm -rf pg_replslot/*
cat > "$H/postgresql.conf" <<EOC
data_directory = '$H/pg'
hba_file = '$H/pg_hba.conf'
ident_file = '$H/pg_ident.conf'
listen_addresses = ''
port = $PORT
unix_socket_directories = '$H/run'
unix_socket_permissions = 0700
shared_buffers = 6GB
work_mem = 256MB
maintenance_work_mem = 2GB
max_parallel_workers_per_gather = 4
max_connections = 200
max_worker_processes = 16
hot_standby = on
restore_command = 'false'
recovery_target_timeline = 'latest'
recovery_target_action = 'promote'
fsync = off
synchronous_commit = off
archive_mode = off
jit = off
statement_timeout = 0
idle_in_transaction_session_timeout = 0
EOC
echo 'local all all peer map=b' > "$H/pg_hba.conf"
printf 'b %s %s\nb %s plx_owner\nb %s postgres\n' "$USER" "$USER" "$USER" "$USER" > "$H/pg_ident.conf"
nohup choom -n 800 -- "$B/postgres" -D "$H/pg" -c config_file="$H/postgresql.conf" > "$H/pg.log" 2>&1 < /dev/null &
r=""
for i in $(seq 1 900); do r=$("$B/psql" -X -h "$H/run" -p $PORT -d plx -Atc 'select pg_is_in_recovery()' 2>/dev/null); [ "$r" = f ] && break; sleep 2; done
[ "$r" = f ] || { log not_promoted; exit 1; }
Q="$B/psql -X -h $H/run -p $PORT -d plx -At"
# every role that can connect, the build's own OS-user role included: the copy of the box's
# database carries a role-level `idle_in_transaction_session_timeout = 15min` on the OS user
# (11-23: the emails build, connected as that user, was ended after 15.8 min idle in a
# transaction while its workers read); a role setting wins over the database's
for role in $($Q -c "select rolname from pg_roles where rolname !~ '^pg_'"); do
  $Q -c "ALTER ROLE $role SET statement_timeout = 0" -c "ALTER ROLE $role SET idle_in_transaction_session_timeout = 0" > /dev/null
done
$Q -c "ALTER DATABASE plx SET statement_timeout = 0" -c "ALTER DATABASE plx SET idle_in_transaction_session_timeout = 0" > /dev/null
# fail closed: no role or database setting may leave a timeout above 0, and a session as the
# build's own user must read 0 (a long build is never ended by a request-sized guard)
bad=$($Q -c "select count(*) from pg_db_role_setting d, unnest(d.setconfig) c where c ~ '^(statement_timeout|idle_in_transaction_session_timeout)=' and c !~ '=0$'")
mine=$($Q -c "select current_setting('idle_in_transaction_session_timeout') || '/' || current_setting('statement_timeout')")
[ "$bad" = 0 ] && [ "$mine" = "0/0" ] || { log "timeouts_not_off: $bad settings, session $mine"; exit 1; }
log "promoted lsn=$($Q -c 'select pg_current_wal_lsn()')"
cd "$H/code" || exit 1
# the people exclusion's object ids (the same setting the host's builds read; never printed)
[ -r "$HOME/.config/plx/oldmail.env" ] && . "$HOME/.config/plx/oldmail.env"
[ -n "${PLX_EXCLUDED_PEOPLE_OIDS:-}" ] || { log "refused: PLX_EXCLUDED_PEOPLE_OIDS not set"; exit 1; }
export PLX_EXCLUDED_PEOPLE_OIDS
for v in $(env | cut -d= -f1 | grep -E 'KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL'); do unset "$v"; done
export PYTHONPATH=$PWD PLX_PG_DSN="host=$H/run port=$PORT dbname=plx" PLX_STATE_DIR=$H/state \
  PLX_DISK_FLOOR_GB_DATA=0 PLX_BIZ_MIN_FREE_GB=0 PLX_WAL_STANDBY_SLOTS= PLX_BIZ_READ_WORKERS=${TRAIN_READ_WORKERS:-8} \
  PLX_ONTOLOGY_PG=only PLX_BIZ_OWNERS_REQUIRED=1
for view in "$@"; do
  before=$($Q -c "select count(*) from biz.$view" 2>/dev/null)
  t=$(date +%s)
  choom -n 800 -- "$HOME/plx-tests/venv/bin/python3" -m operating.pg.business_views build --view "$view" \
    > "$H/build-$view.out" 2> "$H/build-$view.err"; rc=$?
  log "build $view rc=$rc seconds=$(( $(date +%s) - t )) rows_before=$before rows_after=$($Q -c "select count(*) from biz.$view") code=$($Q -c "select notes->>'code' from biz.state where view='$view'")"
  [ $rc = 0 ] || exit 1
done
log "chain done"
touch "$H/keep"; while [ -f "$H/keep" ]; do sleep 30; done
"$B/pg_ctl" -D "$H/pg" stop -m fast -w -t 120
