#!/bin/bash
# After the local landing queue ends: push local-main to GitHub main, then each landed piece to the product repo in order.
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
cd $PLX_REPO
while systemctl --user is-active -q plx-land-local; do timeout 30 tail -f /dev/null; done
git fetch -q origin main
git merge-base --is-ancestor origin/main local-main || { echo "origin/main moved past local-main: manual merge needed"; exit 1; }
git push -q origin local-main:main && echo "$(TZ=America/New_York date +%r) pushed local-main $(git rev-parse --short local-main) to origin/main"
declare -A MSG=(
 [build-fast]="Builds record where their time goes and finish their checks faster, and the app answers immediately after a restart"
 [delta2]="Changes from Business Central, SharePoint and Mission Control also show up within minutes"
 [rag]="Search stays current within minutes, Teams chats and pasted images are searchable, and answers say how fresh the data is"
 [intelligence]="The Workbench understands questions however they are typed, recognises names and codes across every system, and answers by querying the records first"
 [eval-wide]="The accuracy test set covers far more real questions, including ones in any wording and questions about files")
while read -r name sha; do
  [ -n "$sha" ] || continue
  SRC_REF=$sha bash $PLX_OPS/rishi_push.sh "${MSG[$name]:-$name}" 2>&1 | grep -E "PUSHED|UNALLOWED" | tail -2
done < $PLX_SCRATCH/runs/pending-pushes.txt
mv $PLX_SCRATCH/runs/pending-pushes.txt $PLX_SCRATCH/runs/pending-pushes.done.$(date +%s)
