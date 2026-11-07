#!/bin/bash
# Deploy origin/main as one release for every unit (deploy/release/plx-release), on the
# SERVED generation's schema: files listed in $PLX_SCHEMA_HOLD (default: proactive-truth's two
# schema files, held at 30e62f7251^1 until their in-place migration applies) are taken from
# that commit. plx-release refuses a release whose schema digest differs from the served
# projection's, so nothing restarts on a mismatch. Then the derived state made by the old
# code is recomputed: proactive claims and the briefing, then the truth check over them.
# Usage: deploy_compat.sh [ref]   (default origin/main)
set -e
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
rel="$(dirname "$(readlink -f "$0")")/../release/plx-release"
ref=${1:-origin/main}
hold_from=${PLX_SCHEMA_HOLD_FROM-30e62f7251^1}
hold=${PLX_SCHEMA_HOLD-ontology/schema/domains/purchasing.json ontology/schema/process/po_delivery_window.json}
git -C "$PLX_REPO" fetch -q origin
overlays=()
if [ -n "$hold_from" ]; then for p in $hold; do overlays+=(--overlay "$hold_from:$p"); done; fi
id=$("$rel" build "$ref" "${overlays[@]}" | tail -1)
"$rel" switch "$id"
"$rel" restart
echo "$(plx_now) deployed release $id ($(git -C "$PLX_REPO" rev-parse --short "$ref"))"
sudo systemctl start --no-block plx-proactive.service
( until ! systemctl is-active -q plx-proactive.service; do sleep 30; done; sudo systemctl start plx-truth-check.service
  echo "$(plx_now) post-deploy truth check: $(sed -n 3p "$PLX_STATE/truth-check/report.md")" >> "$PLX_SCRATCH/OVERNIGHT-LOG.md" ) >/dev/null 2>&1 &
