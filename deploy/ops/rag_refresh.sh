#!/bin/bash
# Refresh the RAG index (search.chunks / search.entities) from the SERVED projection
# generation: chunk every captured text, upsert only new or changed rows (unchanged rows
# keep their vectors), delete rows whose source is gone (shrink guard on), purge the
# people exclusion's units and withheld senders first, re-cluster duplicates, embed only
# pending rows. Idempotent and resumable: a killed run is simply run again (rows already
# written stay; pending embeddings are picked up where they stopped). The HNSW index is
# kept (--keep-index): live searches never lose dense retrieval during a refresh.
#
# Usage:  rag_refresh.sh                    run inline (one at a time, flock)
#         heavy.sh rag-refresh 24G $PLX_OPS/rag_refresh.sh   (as a capped unit)
# Env:    RAG_REFRESH_CODE   checkout to run (default: the deployed $PLX_CURRENT)
#         RAG_CHUNK_WORKERS  chunking processes (default: 8 quiet host, 3 beside an eval)
#         RAG_EMBED_WORKERS  embedding processes (default: 8 quiet host, 2 beside an eval)
#         RAG_EMBED_THREADS  torch threads per embed worker (default: cpus / workers)
#         RAG_WINDOW_DAYS    mail/Teams written for the last N days (default 30; 0 = all)
# Log:    $PLX_SCRATCH/runs/rag-refresh.log (one JSON report per run + a summary line)
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
set -u
S=$PLX_SCRATCH
log=$S/runs/rag-refresh.log
code=${RAG_REFRESH_CODE:-$PLX_CURRENT}
state=$PLX_STATE
exec 9>"$S/runs/rag-refresh.lock"
flock -n 9 || { echo "$(date -u +%FT%TZ) rag_refresh: another refresh is running" >>"$log"; exit 0; }

# disk: keep headroom on /data for builds (refuse below RAG_MIN_FREE_GB, default 230)
free=$(df --output=avail -BG /data | tail -1 | tr -dc 0-9)
if [ "$free" -lt "${RAG_MIN_FREE_GB:-230}" ]; then
  echo "$(date -u +%FT%TZ) rag_refresh: refused, /data has ${free}G free" >>"$log"; exit 1
fi
gen=$(python3 -c "import json;print(json.load(open('$state/projection/CURRENT'))['generation'])") \
  || { echo "$(date -u +%FT%TZ) rag_refresh: no CURRENT generation" >>"$log"; exit 1; }
store=$state/projection/generations/$gen/store.sqlite
[ -f "$store" ] || store=$state/projection/ontology.sqlite

if systemctl list-units 'plx-eval*' --state=active --no-legend 2>/dev/null | grep -q .; then
  busy=1; cw=${RAG_CHUNK_WORKERS:-3}; ew=${RAG_EMBED_WORKERS:-2}; cpus=${RAG_CPUS:-10-15}
else
  busy=0; cw=${RAG_CHUNK_WORKERS:-8}; ew=${RAG_EMBED_WORKERS:-8}; cpus=${RAG_CPUS:-0-15}
fi
ncpu=$(( $(echo "$cpus" | awk -F- '{print $2-$1+1}') ))
threads=${RAG_EMBED_THREADS:-$(( ncpu / ew > 0 ? ncpu / ew : 1 ))}

flags=(--state "$state" --projection "$store" --workers "$ew" --order recent)
help=$(cd "$code" && $PLX_VENV/bin/python3 -m operating.embed run --help 2>&1)
grep -q -- --keep-index <<<"$help" && flags+=(--keep-index)
grep -q -- --chunk-workers <<<"$help" && flags+=(--chunk-workers "$cw")
# Mail of the last RAG_WINDOW_DAYS only (Teams, documents, maps, records: all). The
# served mail scope holds ~3.2M messages (~14M chunks, ~150 GB and weeks of CPU to
# embed); older mail already indexed stays. 0 = no window.
window=${RAG_WINDOW_DAYS:-30}
[ "$window" != 0 ] && grep -q -- --window-days <<<"$help" && flags+=(--window-days "$window")
old_code=0
if ! grep -q -- --keep-index <<<"$help"; then
  # an older checkout would drop the live HNSW index for a large load: chunk in full,
  # then embed a bounded share (under its bulk threshold); the next refresh continues
  old_code=1; flags+=(--no-embed)
fi

started=$(date +%s)
echo "== $(date -u +%FT%TZ) rag_refresh gen=$gen code=$(git -C "$code" rev-parse --short=10 HEAD) busy=$busy chunk_workers=$cw embed_workers=$ew threads=$threads cpus=$cpus window=$window flags=${flags[*]}" >>"$log"
out=$(mktemp "$S/runs/rag-refresh.XXXXXX.json")
( cd "$code" && exec env PLX_EMBED_THREADS=$threads OMP_NUM_THREADS=$threads \
    nice -n 5 taskset -c "$cpus" $PLX_VENV/bin/python3 -m operating.embed run "${flags[@]}" ) \
    >"$out" 2>>"$log" &
run=$!
# disk watchdog: a build beside the refresh can eat the headroom; stop (rows written stay,
# the next refresh continues) below RAG_STOP_FREE_GB (default 205)
( while kill -0 $run 2>/dev/null; do
    f=$(df --output=avail -BG /data | tail -1 | tr -dc 0-9)
    if [ "$f" -lt "${RAG_STOP_FREE_GB:-205}" ]; then
      echo "$(date -u +%FT%TZ) rag_refresh: stopped, /data has ${f}G free" >>"$log"
      pkill -TERM -P $run 2>/dev/null; kill -TERM $run 2>/dev/null; break
    fi
    sleep 30
  done ) &
wait $run
rc=$?
if [ $rc = 0 ] && [ $old_code = 1 ]; then
  ( cd "$code" && PLX_EMBED_THREADS=$threads nice -n 5 taskset -c "$cpus" \
      $PLX_VENV/bin/python3 -m operating.embed embed --workers "$ew" --limit 15000 ) >>"$log" 2>&1
  rc=$?
fi
secs=$(( $(date +%s) - started ))
cat "$out" >>"$log"
python3 - "$out" "$gen" "$secs" "$rc" >>"$log" <<'PY'
import json, sys
path, gen, secs, rc = sys.argv[1:]
try:
    r = json.load(open(path))
except Exception:
    print(f"rag_refresh SUMMARY gen={gen} rc={rc} seconds={secs} report=unreadable"); sys.exit()
w, e = r.get("write") or {}, r.get("embed") or {}
stale = sum(v.get("deleted", 0) for v in (r.get("stale") or {}).values() if isinstance(v, dict))
guard = [k for k, v in (r.get("stale") or {}).items() if isinstance(v, dict) and v.get("refused")]
print(f"rag_refresh SUMMARY gen={gen} rc={rc} seconds={secs} refused={r.get('refused')} "
      f"chunks={(r.get('chunking') or {}).get('chunks_total')} inserted={w.get('inserted')} "
      f"updated={w.get('updated')} unchanged={w.get('unchanged')} stale_deleted={stale} "
      f"shrink_guard={guard} purged={(r.get('purged') or {}).get('rows')} "
      f"sender_purged={(r.get('purged') or {}).get('sender_rows')} "
      f"embedded={e.get('texts_embedded')} reused={e.get('reused_rows')} "
      f"pending_after={e.get('pending_after')} chunk_s={(r.get('chunking') or {}).get('seconds')} "
      f"embed_s={e.get('seconds')} window_not_written={(r.get('window') or {}).get('not_written')} "
      f"entities_s={(r.get('entities') or {}).get('seconds')} dedup_s={(r.get('dedup') or {}).get('seconds')}")
PY
rm -f "$out"
exit $rc
