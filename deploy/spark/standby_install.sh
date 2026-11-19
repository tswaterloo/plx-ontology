#!/bin/bash
# Installs the Spark hot standby's settings and user unit, from the box, as the Spark login
# (no root): deploy/spark/standby/* rendered into ~/plx-ai/standby/ (@HOME@, @USER@ = the
# Spark login) and spark-user/plx-spark-standby.service, enabled but NOT started. Start it
# only after standby_copy.py --go has filled ~/plx-ai/pgdata and the replication tunnel
# (plx-spark-standby-tunnel@<spark>) is up:
#
#   deploy/spark/standby_install.sh <spark>
#   ssh <spark> systemctl --user start plx-spark-standby
#
# Re-running it rewrites the settings and reloads a running standby (settings that need a
# restart say so in its journal).
set -euo pipefail
host=${1:?spark}
[[ $host =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "standby_install: bad spark name" >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
cfg=${PLX_SPARK_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
s() { ssh -F "$cfg" -o BatchMode=yes -o ConnectTimeout=20 "$host" "$@"; }
s 'mkdir -p -m 700 plx-ai/standby plx-ai/standby-run plx-ai/standby/new && mkdir -p .config/systemd/user
   chmod 700 plx-ai/standby plx-ai/standby-run
   test -x plx-ai/pg17/usr/lib/postgresql/17/bin/postgres
   test -e plx-ai/pg17/usr/lib/postgresql/17/lib/pg_stat_statements.so'
scp -q -F "$cfg" "$here"/standby/postgresql.conf "$here"/standby/pg_hba.conf "$here"/standby/pg_ident.conf \
  "$host:plx-ai/standby/new/"
scp -q -F "$cfg" "$here/spark-user/plx-spark-standby.service" "$host:.config/systemd/user/"
s 'set -e
  cd plx-ai/standby
  for f in postgresql.conf pg_hba.conf pg_ident.conf; do
    sed -e "s#@HOME@#$HOME#g" -e "s#@USER@#$USER#g" "new/$f" > "$f.tmp"
    ! grep -q @[A-Z]*@ "$f.tmp"
    chmod 600 "$f.tmp" && mv -f "$f.tmp" "$f"
  done
  rm -rf new
  systemctl --user daemon-reload
  systemctl --user enable plx-spark-standby.service
  if systemctl --user is-active -q plx-spark-standby.service; then
    systemctl --user reload plx-spark-standby.service
  fi'
echo "standby_install: $host settings in ~/plx-ai/standby, unit enabled ($(s 'systemctl --user is-active plx-spark-standby.service || true'))"
