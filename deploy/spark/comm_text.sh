#!/bin/bash
# plx-comm-text's runner: the sidecar refresh with its reading on a Spark (open-speed5's
# pattern, as proactive_whole.sh): the fused upsert's heavy half — the scan of the chunk
# words and the tsvector aggregation — runs read-only against the Spark's own hot standby
# (CPU and I/O the Spark's, never the box's), the rows ship back small (the window's tail
# only), and the box writes them through the very upsert the fused pass used. Nothing writes
# biz.comm_text but the box's apply; the state mark moves only to what the compute saw, so a
# field the standby had not replayed is read again on the next pass.
# Usage: comm_text.sh [OUT_DIR]
set -uo pipefail
umask 077
here=$(dirname "$(readlink -f "$0")")
root=$(readlink -f "$here/../..")
out=${1:-/data/scratch/plx-comm-text}
if [ -n "${CT_SHA:-}" ]; then
  sha=$CT_SHA
else
  sha=$(python3 - "$root/RELEASE.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["sha"])
PY
  ) || { echo "comm_text: cannot read sha from $root/RELEASE.json" >&2; exit 2; }
fi
[ -n "$sha" ] || { echo "comm_text: empty sha" >&2; exit 2; }
host=${SPARK_HOST:-s7d3d}
back="$out/back"
mkdir -p -m 700 "$out" "$back"
rm -f "$back/rows.jsonl.gz"
# compute on the Spark, read-only against its hot standby: the scan and the aggregation
# are the Spark's, never the box's
SPARK_SKIP_LOGIN_CHECK=1 "$here/spark_run.sh" --host "$host" --sha "$sha" --state none --pg standby \
  --name plx-comm-text --out "$back" -- \
  /data/venv/bin/python3 -m operating.pg.comm_text compute --file "$back/rows.jsonl.gz"
spark_rc=$?
if [ "$spark_rc" != 0 ]; then
  rm -f "$back/rows.jsonl.gz"
  exit "$spark_rc"
fi
[ -s "$back/rows.jsonl.gz" ] || { echo "comm_text: the Spark pass wrote no rows" >&2; exit 1; }
# the write side, on the box: the very upsert the fused pass used (CT_APPLY=0: a rehearsal
# of the compute only — nothing writes the sidecar)
if [ "${CT_APPLY:-1}" = 0 ]; then
  echo "comm_text: CT_APPLY=0, the rows stay in $back/rows.jsonl.gz for inspection" >&2
  exit 0
fi
/data/venv/bin/python3 -m operating.pg.comm_text apply --file "$back/rows.jsonl.gz"
apply_rc=$?
rm -f "$back/rows.jsonl.gz"
exit "$apply_rc"
