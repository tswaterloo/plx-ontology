#!/usr/bin/env bash
# Off-site hook TEMPLATE (not active). plx_backup.py runs the executable named by
# PLX_BACKUP_OFFSITE_HOOK once per new backup set, with three arguments:
#   $1 plx-<stamp>.dump   $2 state-<stamp>.tar.gz   $3 manifest-<stamp>.json
# The owner must choose TWO destinations in other regions or providers (Rishi, 29 Oct:
# "backups in three places"). Until then this hook is not installed and the run reports
# offsite: not_configured. Credentials belong in the destination tool's own config or
# ~/.config/plx/backup.env (mode 600), never in this repository.
# Copy to e.g. ~/.config/plx/offsite_hook.sh, fill in, chmod 700, and set
# PLX_BACKUP_OFFSITE_HOOK in ~/.config/plx/backup.env. Exit non-zero on any failure.
set -euo pipefail
echo "offsite hook not configured: choose two destinations first" >&2
exit 1
# Destination 1 (example: Azure Blob, another region):
#   azcopy copy "$1" "https://<account>.blob.core.windows.net/<container>/plx/" ...
#   (same for "$2" and "$3")
# Destination 2 (example: S3, another provider/region):
#   aws s3 cp "$1" "s3://<bucket>/plx/" --storage-class STANDARD_IA
#   (same for "$2" and "$3")
