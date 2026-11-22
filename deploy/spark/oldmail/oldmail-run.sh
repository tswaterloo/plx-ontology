#!/bin/bash
# old-mail store runner on sb4ec: oldmail-run.sh <code-dir> <workers> [extra args]
set -uo pipefail
cd "$1"; shift; W=$1; shift
export PYTHONPATH=$PWD LD_LIBRARY_PATH=$HOME/plx-replica/pg17/lib
unset PLX_WORKBENCH_API_KEY AZURE_CLIENT_SECRET
# the people exclusion (not in the repository): fail closed without it
[ -r $HOME/.config/plx/oldmail.env ] && . $HOME/.config/plx/oldmail.env
[ -n "${PLX_EXCLUDED_PEOPLE_OIDS:-}" ] || { echo '{"refused": "people_exclusion_unset"}'; exit 2; }
export PLX_EXCLUDED_PEOPLE_OIDS
exec choom -n 800 -- $HOME/plx-tests/venv/bin/python3 -m operating.embed.oldmail chunk \
  --dsn "host=$HOME/oldmail-store/run port=55710 dbname=plx" --state $HOME/oldmail-state \
  --floor-path $HOME/oldmail-store --workers $W "$@"
