#!/usr/bin/env bash
# Nightly backup set (Postgres dump + small state stores), rotation, off-site hook.
# Settings: see deploy/backup/plx_backup.py. Run by plx-backup.service.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="${PLX_PYTHON:-/data/venv/bin/python3}"
# A migration/maintenance window holding backups (ontology/window_lock.py): no set is
# written, so the run fails with 75 (EXIT_HELD): a refused night never reads as a backup
# taken. Only a hold (75) skips; any other check failure prints and the backup still runs.
held_rc=0
held="$(cd "$here/../.." && "$py" -m ontology.window_lock check backup 2>&1)" || held_rc=$?
if [ "$held_rc" -eq 75 ]; then
  echo "backup: refused, $held"
  exit 75
elif [ "$held_rc" -ne 0 ]; then
  echo "backup: window check failed (exit $held_rc), backing up anyway: $held" >&2
fi
if [ "$held_rc" -ne 0 ]; then
  exec "$py" "$here/plx_backup.py" backup
fi
# Through guard-exec: the backup holds its running lock while it runs, so a window taken
# meanwhile waits for it (take --blocks backup reports it busy); a hold taken since the check
# refuses here (exit 75, --exit-code); a lock-infrastructure error still runs the backup.
cd "$here/../.."
exec "$py" -m ontology.window_lock guard-exec --exit-code backup -- "$py" "$here/plx_backup.py" backup
