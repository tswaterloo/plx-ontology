#!/bin/bash
# Mint a demo access link for the Workbench app running in --dev --dev-ticket-login mode.
#
#   deploy/demo-access-link.sh [TTL_MINUTES]      # default 120 (the console's maximum)
#
# Writes https://<PLX_APP_HOST>/#ticket=<ticket> to ~/.config/plx/workbench-access-link
# (mode 0600). The link is never printed: open the file yourself and paste the link into
# the browser (or into "I have an access link" on the sign-in page). The ticket is a
# console session ticket (operator_ui.console_ticket mint) for the reader in
# PLX_EVAL_READER, signed with the API's own PLX_WORKBENCH_SESSION_SECRET, verified
# against the host config's employee policy and Portal snapshot; the mint appends one
# audit row to <state>/console-ticket-audit.jsonl. A ticket lives at most two hours;
# the app session ends with it. Revoke early with:
#   python3 -m operator_ui.console_ticket revoke --state-dir /data/plx-state \
#       --ticket-file ~/.config/plx/workbench-ticket.url
set -euo pipefail
TTL=${1:-120}
CHECKOUT=${PLX_DEPLOY_CHECKOUT:-/data/scratch/wt/deploy}
STATE=${PLX_DEPLOY_STATE:-/data/plx-state}
CFG=${PLX_DEPLOY_CONFIG:-$HOME/.config/plx}
# The reader (a variable holding the claims JSON) and the host config directory; for the
# full-read demo: PLX_DEPLOY_READER_ENV=PLX_EVAL_READER_VINCE (from reader-vince.env) and
# PLX_DEPLOY_HOST_DIR=$CFG/workbench-host-vince.
READER_ENV=${PLX_DEPLOY_READER_ENV:-PLX_EVAL_READER}
HOST_DIR=${PLX_DEPLOY_HOST_DIR:-$CFG/workbench-host}
HOST=$(sed -n 's/^PLX_APP_HOST=//p' /etc/plx/workbench-caddy.env)
[ -n "$HOST" ] || { echo "PLX_APP_HOST missing in /etc/plx/workbench-caddy.env" >&2; exit 2; }
umask 077
cd "$CHECKOUT"
set -a; . "$CFG/plx.env"; . "$CFG/workbench-session.env"
# The full-read reader's claims only when that reader was asked for.
if [ -n "${PLX_DEPLOY_READER_ENV:-}" ] && [ -f "$CFG/reader-vince.env" ]; then . "$CFG/reader-vince.env"; fi
set +a
TICKET_FILE="$CFG/workbench-ticket.url"
rm -f "$TICKET_FILE"
/data/venv/bin/python3 -m operator_ui.console_ticket mint --state-dir "$STATE" \
    --host-config "$HOST_DIR/host.json" --reader-env "$READER_ENV" \
    --ttl-minutes "$TTL" --out "$TICKET_FILE"
# Re-point the console URL (http://127.0.0.1:PORT/#access=T) at the app (#ticket=T).
HOST="$HOST" TICKET_FILE="$TICKET_FILE" OUT="$CFG/workbench-access-link" /data/venv/bin/python3 -I -c '
import os
url = open(os.environ["TICKET_FILE"], encoding="utf-8").read().strip()
ticket = url.split("#access=", 1)[1]
fd = os.open(os.environ["OUT"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("https://%s/#ticket=%s\n" % (os.environ["HOST"], ticket))
'
echo "access link written to $CFG/workbench-access-link (expires in $TTL min)"
