#!/bin/bash
# Full use-case scoreboard on a FIXED copy of origin/main (its own detached worktree, so a
# moving checkout can't mix versions mid-run).
# Usage: cards_run.sh <run-name> [owner|vince] [extra eval_case_cards args, e.g. --items A,B]
#   owner: the owner reader (PLX_EVAL_READER); vince: Vince with the full-read grant.
# Runs only inside the single eval slot (a capped system unit, one at a time; see eval_slot.sh):
# called from anywhere else it re-runs itself there and waits.
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
[ -z "$PLX_HEAVY_UNIT" ] && exec $PLX_OPS/eval_slot.sh "${1:?run name}" "$0" "$@"
S=$PLX_SCRATCH; name=${1:?run name}; reader=${2:-vince}; shift 2 || shift $#
repo=$PLX_REPO; wt=$S/wt/run-$name
export PATH=$PLX_VENV/bin:$PATH
cd $repo && git fetch -q origin main
rm -rf "$wt"; git worktree prune
git worktree add -q --detach "$wt" origin/main
cd "$wt"
set -a; . ~/.config/plx/plx.env; set +a
mkdir -p $S/runs/$name
git rev-parse HEAD > $S/runs/$name/CODE_SHA
host_args=()
if [ "$reader" = vince ]; then
  set -a; . ~/.config/plx/reader-vince.env; set +a
  export PLX_EVAL_READER="$PLX_EVAL_READER_VINCE"
  host_args=(--reader-env PLX_EVAL_READER_VINCE --full-read)
fi
python3 -m scripts.eval_owner_host "${host_args[@]}" --out $S/runs/$name/owner-host \
  > $S/runs/$name/owner-host.out 2>&1 || { echo "owner host failed"; exit 1; }
PLX_WORKBENCH_SESSION_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))') \
PLX_WORKBENCH_HOST_CONFIG=$S/runs/$name/owner-host/host.json \
python3 -m scripts.eval_case_cards run --work-dir $S/runs/$name/work --all --per-case 0 --workers ${PLX_EVAL_WORKERS:-5} \
  --budget-seconds 14000 --measure-only "$@" > $S/runs/$name/report.json 2> $S/runs/$name/err.txt
echo "exit $?" >> $S/runs/$name/err.txt
