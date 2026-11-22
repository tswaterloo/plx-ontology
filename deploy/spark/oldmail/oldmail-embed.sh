#!/bin/bash
# old-mail store embedding on sb4ec's GPU: oldmail-embed.sh <code-dir> [extra args]
set -uo pipefail
C="$1"; shift; cd "$C"
export PYTHONPATH=$PWD LD_LIBRARY_PATH=$HOME/plx-replica/pg17/lib PLX_PG_DSN="host=$HOME/oldmail-store/run port=55710 dbname=plx"
unset PLX_WORKBENCH_API_KEY AZURE_CLIENT_SECRET
# the people exclusion (not in the repository): fail closed without it
[ -r $HOME/.config/plx/oldmail.env ] && . $HOME/.config/plx/oldmail.env
[ -n "${PLX_EXCLUDED_PEOPLE_OIDS:-}" ] || { echo '{"refused": "people_exclusion_unset"}'; exit 2; }
export PLX_EXCLUDED_PEOPLE_OIDS
mkdir -p $HOME/oldmail-store/embed-state/search
# the GPU shares this host's memory (11-17: an over-commit hung both Sparks): 31 GB free before start
avail=$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo)
[ "$avail" -ge 31 ] || { echo "{\"waiting\": \"memory_available_gb_$avail\"}"; exit 75; }
# only once every planned unit is chunked (after a reboot both units start; this one waits)
Q="$HOME/plx-replica/pg17/bin/psql -X -h $HOME/oldmail-store/run -p 55710 -d plx -At"
while :; do
  left=$($Q -c "SELECT count(DISTINCT p.unit) FROM search.oldmail_plan p WHERE NOT EXISTS (SELECT 1 FROM search.oldmail_units u WHERE u.unit = p.unit)") || exit 1
  [ "$left" = 0 ] && break; echo "{\"waiting\": \"units_left_$left\"}"; sleep 120
done
W="bash -c 'cd $HOME/plx-embed && HF_HOME=$HOME/plx-embed/hf HF_HUB_OFFLINE=1 exec choom -n 800 -- venv/bin/python $C/operating/embed/remote_worker.py --model BAAI/bge-m3 --precision fp16 --batch 64 --memory-fraction 0.2'"
exec choom -n 800 -- $HOME/plx-tests/venv/bin/python3 -m operating.embed remote --state $HOME/oldmail-store/embed-state \
  --host sb4ec-gpu --local-cmd "$W" --free-path $HOME/oldmail-store --min-free-gb 300 "$@"
