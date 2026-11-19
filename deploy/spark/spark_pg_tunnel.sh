#!/bin/bash
# One of the box's reverse tunnels to a Spark's Postgres path (plx-spark-pg-tunnel@<host>:<n>).
# Each tunnel is its own ssh connection, so its own TCP stream through the user-space Tailscale
# (about 1 MB/s each from the box to the Spark): several tunnels per Spark, compressed, so one
# run's large result no longer holds up every other connection's first byte. 16 per Spark:
# measured 12 parallel streams at ~9 MB/s together where one stream carried ~0.9 MB/s.
# The Spark end is ~/plx-ai/pgtun/t<n>/s-<start time> (0700 folders, socket 0600: sshd's
# StreamLocalBindMask), a new name per start (the ending session of the previous start unlinks
# its own name, never this one); spark_pg_balance.py uses the newest. The box end is the
# plx-sparkpg relay socket (peer -> plx_eval only).
set -euo pipefail
inst=${1:-}
host=${inst%%:*}
n=${inst##*:}
if [[ ! $inst =~ ^[a-z0-9][a-z0-9-]*:([1-9]|1[0-6])$ ]]; then
  echo "spark_pg_tunnel: instance must be <spark>:<1-16>, got '$inst'" >&2
  exit 2
fi
cfg=${PLX_SPARK_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
relay=${PLX_SPARK_PG_RELAY:-/run/plx-spark-pg/.s.PGSQL.5432}
dir=plx-ai/pgtun/t$n
name=s-$(date +%s%N)
remote_home=$(ssh -F "$cfg" -o BatchMode=yes -o ConnectTimeout=20 "$host" \
  "mkdir -p -m 700 plx-ai/pgtun $dir && chmod 700 plx-ai/pgtun $dir && rm -f $dir/s-* $dir/.s.PGSQL.5432 && echo \$HOME")
[[ $remote_home == /* ]] || { echo "spark_pg_tunnel: $host gave no home folder" >&2; exit 3; }
exec ssh -F "$cfg" -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 \
  -o ServerAliveCountMax=3 -o Compression=yes \
  -R "$remote_home/$dir/$name:$relay" "$host"
