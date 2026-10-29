#!/usr/bin/env bash
# Nightly backup set (Postgres dump + small state stores), rotation, off-site hook.
# Settings: see deploy/backup/plx_backup.py. Run by plx-backup.service.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${PLX_PYTHON:-/data/venv/bin/python3}" "$here/plx_backup.py" backup
