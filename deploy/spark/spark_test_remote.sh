#!/bin/bash
# Runs ON the Spark (~/plx-tests): checks out <sha> from repo.git, ensures the throwaway Postgres
# (socket only, no TCP, fsync off) is up, runs the land module list through tools/main_ci.py.
# usage: spark_test_remote.sh <sha> <workers> <module>...   (modules: smoke is always included unless NOSMOKE=1)
# env: IDS=<file>  use these ready-made job ids verbatim (no smoke/extra expansion)
#      SHARD=i/n   run only share i (0-based) of n of the list (greedy by recorded timings)
set -uo pipefail
sha=$1 workers=$2; shift 2
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
IDS=${IDS:-} SHARD=${SHARD:-}
for v in $(compgen -e | grep -E '(KEY|SECRET|TOKEN|PASSWORD|PASSWD|DSN|CREDENTIAL)'); do unset "$v"; done
export PATH="$T/venv/bin:$PG:$PATH" PGHOST="$T/run" PGPORT=5433 PYTHONDONTWRITEBYTECODE=1 TZ=${TZ:-America/New_York}
cd "$wt" || exit 97
mods=$T/out/$sha.modules.txt
cp -f "$T/land-timings.json" "$T/land-timings.work.json" 2>/dev/null || echo '{}' > "$T/land-timings.work.json"
if [ -n "$IDS" ] && [ -f "$IDS" ]; then cp -f "$IDS" "$mods"
else LAND_TIMINGS=$T/land-timings.work.json python3 "$T/land_shard.py" "$@" > "$mods" || { echo "module list failed" >&2; exit 99; }; fi
if [ -n "$SHARD" ]; then
  python3 - "$SHARD" "$mods" "$T/land-timings.work.json" <<'PY' || { echo "shard split failed" >&2; exit 99; }
import json, sys
i, n = map(int, sys.argv[1].split("/")); path = sys.argv[2]
try: t = json.load(open(sys.argv[3]))
except Exception: t = {}
ids = [l.strip() for l in open(path) if l.strip()]
w = lambda m: float(t.get(m) or t.get(m.rsplit(".", 1)[0]) or 5)
bins = [[] for _ in range(n)]; load = [0.0] * n
for m in sorted(ids, key=w, reverse=True):
    k = load.index(min(load)); bins[k].append(m); load[k] += w(m)
open(path, "w").write("\n".join(bins[i]) + "\n")
PY
fi
[ -s "$mods" ] || { echo "0 modules: 0 pass, 0 known red, 0 FAIL; 0 s with $workers workers"; exit 0; }
exec python3 tools/main_ci.py --repo "$wt" --workers "$workers" --no-audit --timings "$T/land-timings.work.json" --modules @"$mods"
