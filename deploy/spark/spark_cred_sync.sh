#!/bin/bash
# The Sparks' copy of the Workbench AI login, as /usr/local/sbin/plx-claude-cred-sync does on the
# box: the signed-in operator account's CURRENT access token only (never the refresh token), written 0600
# into the two Claude homes spark_run.sh jobs use (~/plx-ai/home/{plx-workbench,autopilot}/.claude).
# Runs every 5 min (plx-spark-cred-sync.timer); a Spark that cannot be reached is skipped.
set -uo pipefail
SRC=${PLX_SPARK_CRED_SRC:-$HOME/.claude/.credentials.json}
ssh_cfg=${PLX_SPARK_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
body=$(python3 -c '
import json, sys
o = json.load(open(sys.argv[1]))["claudeAiOauth"]
print(json.dumps({"claudeAiOauth": {k: o[k] for k in ("accessToken", "expiresAt", "scopes", "subscriptionType") if k in o}}))
' "$SRC") || { echo "spark_cred_sync: cannot read the source login" >&2; exit 1; }
rc=0
for h in ${PLX_SPARK_HOSTS:-s7d3d sb4ec}; do
  printf '%s\n' "$body" | ssh -F "$ssh_cfg" -o BatchMode=yes -o ConnectTimeout=15 "$h" '
    umask 077; read -r line || exit 3
    for d in plx-workbench autopilot; do
      mkdir -p ~/plx-ai/home/$d/.claude && printf "%s\n" "$line" > ~/plx-ai/home/$d/.claude/.cred.$$ \
        && mv -f ~/plx-ai/home/$d/.claude/.cred.$$ ~/plx-ai/home/$d/.claude/.credentials.json || exit 4
    done' || { echo "spark_cred_sync: $h skipped (exit $?)" >&2; rc=1; }
done
exit $rc
