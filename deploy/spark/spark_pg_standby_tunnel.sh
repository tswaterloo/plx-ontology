#!/bin/bash
# The Spark standby's own replication path to the box's Postgres: one reverse ssh tunnel
# (compressed; WAL compresses ~3-4x) putting the box's Postgres socket at
# ~/plx-ai/standby-tun/.s.PGSQL.5432 on <spark>, a folder only the Spark login can open.
# Separate from the 16 eval tunnels (spark_pg_tunnel.sh -> the relay): replication must
# reach Postgres itself, not the relay, and a busy eval must not slow the WAL stream.
#   spark_pg_standby_tunnel.sh <spark>          (plx-spark-standby-tunnel@<spark>.service)
# The connection arrives at the box as a local socket connection of this unit's user, so
# the box needs, once (a reload, no restart):
#   pg_hba.conf:  local  replication  plx_standby  peer map=plx_standby
#   pg_ident.conf: plx_standby  <this unit's login>  plx_standby
#   CREATE ROLE plx_standby LOGIN REPLICATION;
# and the standby's primary_conninfo names user=plx_standby host=<that folder>.
set -euo pipefail
host=${1:-}
[[ $host =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "spark_pg_standby_tunnel: give a Spark host name" >&2; exit 2; }
cfg=${PLX_SPARK_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
pg=${PLX_STANDBY_PRIMARY_SOCKET:-/var/run/postgresql/.s.PGSQL.5432}
dir=plx-ai/standby-tun
remote_home=$(ssh -F "$cfg" -o BatchMode=yes -o ConnectTimeout=20 "$host" \
  "mkdir -p -m 700 $dir && chmod 700 $dir && rm -f $dir/.s.PGSQL.5432 && echo \$HOME")
[[ $remote_home == /* ]] || { echo "spark_pg_standby_tunnel: $host gave no home folder" >&2; exit 3; }
exec ssh -F "$cfg" -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 \
  -o ServerAliveCountMax=3 -o Compression=yes -o StreamLocalBindUnlink=yes \
  -R "$remote_home/$dir/.s.PGSQL.5432:$pg" "$host"
