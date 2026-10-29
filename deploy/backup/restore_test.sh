#!/usr/bin/env bash
# Restore the newest backup set into a throwaway database and check every table's row
# count against the manifest, the file digests and each SQLite store. Exit 0: restorable.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${PLX_PYTHON:-/data/venv/bin/python3}" "$here/plx_backup.py" restore-test
