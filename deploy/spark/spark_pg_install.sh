#!/bin/bash
# Installs a Spark's end of the Postgres path, from the box, as the Spark login (no root):
# spark_pg_balance.py and its user unit (plx-spark-pg-balance), which listens where runs always
# connected (~/plx-ai/run/.s.PGSQL.5432) and spreads connections over the box's tunnels
# plx-spark-pg-tunnel@<spark>:1..16 (deploy/spark/systemd, spark_pg_tunnel.sh).
#
#   deploy/spark/spark_pg_install.sh <spark>
#
# Cut-over without dropping a connection: a socket the old single tunnel left at
# ~/plx-ai/run/.s.PGSQL.5432 is renamed (its open connections keep running until that
# tunnel stops); the relay then listens at the usual name. A re-install restarts the relay:
# connections through it end (runs' pools reconnect).
set -euo pipefail
host=${1:?spark}
[[ $host =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "spark_pg_install: bad spark name" >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
cfg=${PLX_SPARK_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
s() { ssh -F "$cfg" -o BatchMode=yes -o ConnectTimeout=20 "$host" "$@"; }
s 'mkdir -p -m 700 plx-ai/pg plx-ai/pgtun plx-ai/run && mkdir -p .config/systemd/user && chmod 700 plx-ai/pgtun plx-ai/run'
scp -q -F "$cfg" "$here/spark_pg_balance.py" "$host:plx-ai/pg/spark_pg_balance.py.new"
scp -q -F "$cfg" "$here/spark-user/plx-spark-pg-balance.service" "$host:.config/systemd/user/"
s 'set -e
  mv -f plx-ai/pg/spark_pg_balance.py.new plx-ai/pg/spark_pg_balance.py
  # The relay must outlive the login sessions (the tunnels are sessions; between restarts
  # there may be none).
  loginctl enable-linger 2>/dev/null || true
  [ "$(loginctl show-user "$USER" -p Linger --value)" = yes ] || echo "spark_pg_install: Linger is off for $USER" >&2
  systemctl --user daemon-reload
  sock=plx-ai/run/.s.PGSQL.5432
  if [ -S $sock ] && ! systemctl --user -q is-active plx-spark-pg-balance.service; then
    mv -f $sock plx-ai/run/old-tunnel.s.PGSQL.5432
  fi
  systemctl --user enable plx-spark-pg-balance.service
  systemctl --user restart plx-spark-pg-balance.service
  sleep 1
  systemctl --user -q is-active plx-spark-pg-balance.service && [ -S $sock ]'
