#!/bin/bash
# copy.sh TRAIN: on the Spark that holds the standby (s7d3d), a base backup OF the running standby
# (never stopped) into ~/train-<TRAIN>/pg: the input of a comms train (master plan specs M4 step 4).
# A replication line is added to the standby's hba/ident for the copy only (originals kept and
# restored on exit, also on failure). One line per step in ~/train-<TRAIN>/steps.log.
# Refuses when the target exists. Rate-capped so the standby keeps replaying (600 MB/s).
set -uo pipefail
TRAIN=${1:?usage: copy.sh TRAIN}
H=$HOME/train-$TRAIN; ST=$HOME/plx-ai/standby; B=$HOME/plx-ai/pg17/usr/lib/postgresql/17/bin
export LD_LIBRARY_PATH=$HOME/plx-ai/pg17/usr/lib/aarch64-linux-gnu
log() { echo "$(date -u +%FT%TZ) $*" >> "$H/steps.log"; }
[ -e "$H/pg" ] && { echo "refused: $H/pg exists"; exit 1; }
mkdir -p -m 700 "$H"
cp -p "$ST/pg_hba.conf" "$ST/pg_hba.conf.bak-$TRAIN"; cp -p "$ST/pg_ident.conf" "$ST/pg_ident.conf.bak-$TRAIN"
restore() {
  cp -p "$ST/pg_hba.conf.bak-$TRAIN" "$ST/pg_hba.conf"; cp -p "$ST/pg_ident.conf.bak-$TRAIN" "$ST/pg_ident.conf"
  systemctl --user reload plx-spark-standby; log hba_restored
}
trap restore EXIT
echo "local replication plx_standby peer map=repl$TRAIN" >> "$ST/pg_hba.conf"
echo "repl$TRAIN $USER plx_standby" >> "$ST/pg_ident.conf"
systemctl --user reload plx-spark-standby; sleep 2
log copy_start
t=$(date +%s)
"$B/pg_basebackup" -h "$HOME/plx-ai/standby-run" -U plx_standby -D "$H/pg" -Fp -X stream --max-rate=600M -c fast \
  >> "$H/copy.err" 2>&1 && { log "copy_done $(( $(date +%s) - t ))s"; echo done > "$H/copy.log"; } \
  || { log "copy_failed $(tail -c 300 "$H/copy.err" | tr '\n' ' ')"; exit 1; }
