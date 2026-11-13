#!/bin/bash
# The host's separate Postgres test cluster (17/test, socket-only on port 5433), which
# tools/testslot.py points every wrapped test run at when its socket exists (REL-R-16).
#
# Why: DROP DATABASE waits for every backend of its cluster to absorb a ProcSignalBarrier,
# and a backend blocked writing to a slow client (wait event ClientWrite) does not absorb it
# until the client reads. On the served cluster a delta, follower or app reader in
# ClientWrite therefore held every test teardown for up to ~15 minutes (11-12). A test
# cluster has its own postmaster: no served reader can hold its barriers.
#
# Throwaway data only: fsync, full-page writes and synchronous commit are off (a crash loses
# test databases, nothing else). Data under /var/lib/postgresql, off the nearly full /data.
# Idempotent: creates the cluster once, then (re)applies the settings and starts it.
# Needs sudo. Usage: deploy/ops/test_pg_cluster.sh [--check]
set -euo pipefail
VERSION=17 NAME=test PORT=${PLX_TEST_PG_PORT:-5433}
DATA=/var/lib/postgresql/$VERSION/$NAME
CONF=/etc/postgresql/$VERSION/$NAME/conf.d/plx-test.conf
ROLE=${PLX_TEST_PG_ROLE:-$(id -un)}

check() {
  psql -p "$PORT" -d postgres -Atc "SELECT current_setting('port'), current_setting('fsync'),
    (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)" \
    | grep -qx "$PORT|off|t" || { echo "test cluster on $PORT: not ready" >&2; return 1; }
  echo "test cluster on $PORT: ready"
}
[ "${1:-}" = "--check" ] && { check; exit; }

if ! pg_lsclusters -h | awk -v v="$VERSION" -v n="$NAME" '$1 == v && $2 == n {f=1} END {exit !f}'; then
  sudo pg_createcluster "$VERSION" "$NAME" --port "$PORT" --datadir "$DATA" \
    --start-conf auto -- --encoding UTF8 --locale C.UTF-8 --auth-local peer
fi
# socket only: peer for local, every TCP line refused (initdb's default there is trust)
sudo sed -i -E 's/^(host[[:space:]].*[[:space:]])trust$/\1reject/' "/etc/postgresql/$VERSION/$NAME/pg_hba.conf"
before=$(sudo cat "$CONF" "/etc/postgresql/$VERSION/$NAME/pg_hba.conf" 2>/dev/null | md5sum)
sudo tee "$CONF" >/dev/null <<EOF
# deploy/ops/test_pg_cluster.sh: throwaway test databases only
listen_addresses = ''
port = $PORT
max_connections = 300
shared_buffers = 512MB
work_mem = 64MB
maintenance_work_mem = 256MB
max_locks_per_transaction = 256
fsync = off
synchronous_commit = off
full_page_writes = off
wal_level = minimal
max_wal_senders = 0
max_wal_size = 2GB
temp_file_limit = 4GB
jit = off
EOF
sudo systemctl enable "postgresql@$VERSION-$NAME" >/dev/null 2>&1 || true
after=$(sudo cat "$CONF" "/etc/postgresql/$VERSION/$NAME/pg_hba.conf" | md5sum)
# a restart ends the connections of any test run: only when down or the settings changed
if ! pg_lsclusters -h | awk -v v="$VERSION" -v n="$NAME" '$1 == v && $2 == n && $4 == "online" {f=1} END {exit !f}'; then
  sudo pg_ctlcluster "$VERSION" "$NAME" start
elif [ "$before" != "$after" ]; then
  sudo pg_ctlcluster "$VERSION" "$NAME" restart
fi
for _ in $(seq 30); do [ -S "/var/run/postgresql/.s.PGSQL.$PORT" ] && break; sleep 1; done
sudo -u postgres psql -p "$PORT" -d postgres -qAtc \
  "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$ROLE') THEN
     CREATE ROLE \"$ROLE\" SUPERUSER LOGIN; END IF; END \$\$"
check
