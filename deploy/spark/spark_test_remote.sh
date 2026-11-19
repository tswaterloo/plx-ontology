#!/bin/bash
# Runs ON the Spark (~/plx-tests): checks out <sha> from repo.git, ensures the throwaway Postgres
# (socket only, no TCP, fsync off) is up, runs the land module list through tools/main_ci.py.
# usage: spark_test_remote.sh <sha> <workers> <module>...   (modules: the smoke set is added unless NOSMOKE=1)
# env: IDS=<file>  use these ready-made job ids verbatim (no smoke/extra expansion)
#      SHARD=i/n   run only share i (0-based) of n of the list (greedy by recorded timings)
#      SKIP=<file> ids that passed on this tree (tools/run_cache.py): not run, printed as "cached pass"
#                  by share 0 with a "cached: N" line (spark_test.sh counts them as passes)
set -uo pipefail
sha=$1 workers=$2; shift 2
here=$(cd "$(dirname "$0")" && pwd)
T=$HOME/plx-tests; PG=$T/pg/bin
wt=$T/wt/$sha
mkdir -p "$T/wt" "$T/out"
if [ ! -d "$wt" ]; then git -C "$T/repo.git" worktree prune; git -C "$T/repo.git" worktree add -q --detach "$wt" "$sha" || { echo "checkout failed" >&2; exit 97; }; fi
# prune old trees (keep newest 4; never one under 3 h old: concurrent runs pruned each other's live trees, 11-14)
ls -1dt "$T"/wt/*/ 2>/dev/null | tail -n +5 | while read -r d; do [ -n "$(find "$d" -maxdepth 0 -mmin +180)" ] || continue; git -C "$T/repo.git" worktree remove --force "$d" 2>/dev/null || rm -rf "$d"; done
"$PG/pg_ctl" -D "$T/data" status >/dev/null 2>&1 || "$PG/pg_ctl" -D "$T/data" -l "$T/pg.log" -w start >/dev/null || { echo "postgres failed to start" >&2; exit 98; }
# drop leftover throwaway databases of dead runs
for d in $("$PG/psql" -h "$T/run" -p 5433 -d postgres -Atc "select datname from pg_database where datname like 'plx\_%'"); do
  pid=$(echo "$d" | sed -E 's/^plx_[a-z]+(_[a-z]+)?_([0-9]+)(_[0-9]+)?$/\2/'); [[ "$pid" =~ ^[0-9]+$ ]] && [ ! -d /proc/$pid ] && "$PG/psql" -h "$T/run" -p 5433 -d postgres -Atqc "drop database if exists \"$d\" with (force)" >/dev/null
done
# clean env: no credential-shaped variables; only the non-secret test env
IDS=${IDS:-} SHARD=${SHARD:-} SKIP=${SKIP:-}
for v in $(compgen -e | grep -E '(KEY|SECRET|TOKEN|PASSWORD|PASSWD|DSN|CREDENTIAL)'); do unset "$v"; done
[ -d "$HOME/plx-ocr/bin" ] && PATH="$HOME/plx-ocr/bin:$PATH"
# OCR for the tests: tesseract (a conda env in ~/plx-ocr) is linked into the test venv, inside
# sys.prefix, because the extraction sandbox (integrations/extract_sandbox.py) runs no tool under
# $HOME; a copy left in ~/plx-ocr made every sandboxed OCR read "ocr_unavailable" (11-17).
O=$HOME/plx-ocr/env
if [ -x "$O/bin/tesseract.real" ] && [ ! -x "$T/venv/bin/tesseract" ]; then
  if [ ! -d "$T/venv/ocr-env" ]; then   # hard links: no space; a temp name, then one rename
    cp -al "$O" "$T/venv/ocr-env.$$" && mv -T "$T/venv/ocr-env.$$" "$T/venv/ocr-env" 2>/dev/null; rm -rf "$T/venv/ocr-env.$$"
  fi
  printf '%s\n' '#!/bin/sh' "export TESSDATA_PREFIX=$T/venv/ocr-env/share/tessdata" \
    "exec $T/venv/ocr-env/bin/tesseract.real \"\$@\"" > "$T/venv/bin/.tesseract.$$" \
    && chmod 755 "$T/venv/bin/.tesseract.$$" && mv -f "$T/venv/bin/.tesseract.$$" "$T/venv/bin/tesseract"
fi
export PATH="$T/venv/bin:$PG:$PATH" PGHOST="$T/run" PGPORT=5433 PYTHONDONTWRITEBYTECODE=1 TZ=${TZ:-America/New_York}
cd "$wt" || exit 97
mods=$T/out/$sha.$$.modules.txt   # per run: two runs of one commit on a host never share a list
cp -f "$T/land-timings.json" "$T/land-timings.work.json" 2>/dev/null || echo '{}' > "$T/land-timings.work.json"
if [ -n "$IDS" ] && [ -f "$IDS" ]; then cp -f "$IDS" "$mods"
else LAND_TIMINGS=$T/land-timings.work.json python3 "$here/land_shard.py" "$@" > "$mods" || { echo "module list failed" >&2; exit 99; }; fi
# drop the ids that passed on this tree, then take this host's share (cached ids weigh nothing)
cached=$T/out/$sha.cached.$$
python3 "$here/run_cache.py" split --mods "$mods" ${SKIP:+--skip "$SKIP"} ${SHARD:+--shard "$SHARD"} \
  --timings "$T/land-timings.work.json" --cached-out "$cached" || { echo "shard split failed" >&2; exit 99; }
if [ -s "$cached" ]; then sed 's/^/cached pass    0.0s /' "$cached"; echo "cached: $(grep -c . "$cached")"; fi
rm -f "$cached"
[ -s "$mods" ] || { echo "0 modules: 0 pass, 0 known red, 0 FAIL; 0 s with $workers workers"; exit 0; }
exec python3 tools/main_ci.py --repo "$wt" --workers "$workers" --no-audit --timings "$T/land-timings.work.json" --modules @"$mods"
