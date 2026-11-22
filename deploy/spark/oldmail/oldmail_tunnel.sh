#!/bin/bash
# Box side of the older-mail search leg: /run/plx-oldmail/.s.PGSQL.55710 is the old-mail
# store's socket on sb4ec (its login's ~/oldmail-store/run), over the existing ssh login.
set -euo pipefail
cfg=${PLX_SPARK_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
host=${PLX_OLDMAIL_HOST:-sb4ec}
remote_home=$(ssh -F "$cfg" -o BatchMode=yes -o ConnectTimeout=20 "$host" 'echo $HOME')
[[ $remote_home == /* ]] || { echo "oldmail_tunnel: $host gave no home folder" >&2; exit 3; }
exec ssh -F "$cfg" -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=10 \
  -o ServerAliveCountMax=3 -o StreamLocalBindUnlink=yes \
  -L "/run/plx-oldmail/.s.PGSQL.55710:$remote_home/oldmail-store/run/.s.PGSQL.55710" "$host"
