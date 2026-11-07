#!/bin/bash
# Land several reviewed SHAs in ONE merge worktree and ONE test run (the direct list), then push.
# Usage: land_batch.sh "<sha1>:<name1>:<summary1>" "<sha2>:<name2>:<summary2>" ...
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
set -euo pipefail
repo=$PLX_REPO; wt=$PLX_SCRATCH/wt/land-batch-$$
export PATH=$PLX_VENV/bin:$PATH
cd $repo && git fetch -q origin main
rm -rf "$wt"; git worktree prune; git worktree add -q --detach "$wt" origin/main; cd "$wt"
an="$(git log -1 --format=%an origin/main)"; ae="$(git log -1 --format=%ae origin/main)"
for spec in "$@"; do
  sha=${spec%%:*}; rest=${spec#*:}; name=${rest%%:*}; summary=${rest#*:}
  git -c user.name="$an" -c user.email="$ae" merge --no-ff -q "$sha" -m "Land $name: $summary

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" || { echo "CONFLICT in $name"; exit 1; }
done
out=$PLX_SCRATCH/runs/land-batch-$$.tests; mkdir -p "$out"
# The direct list, one module per process, 4 at a time (was one serial run, ~8.6 min).
mods="operator_api.tests.test_backend_fixture operating.tests.test_agent_loop operating.tests.test_case_tools_host tests.test_mcp_server evals.tests.test_runner evals.tests.test_answer_gate_one_path evals.tests.test_final_reply_gate evals.tests.test_runner_presubmit evals.tests.test_score operating.tests.test_claim_checks operating.tests.test_card_answer_quality tests.test_no_hardcoded_cases tests.test_rro_eval ontology.tests.test_contracts operating.tests.test_mcp_output_firewall ontology.tests.test_identifiers operating.tests.test_messages_planner tests.test_no_write_http_paths tests.test_egress_retired tests.test_live_employee_tools"
printf '%s\n' $mods | xargs -P 4 -I{} sh -c 'env -i PATH="$PATH" HOME="$HOME" nice -n 5 python3 -m unittest {} > "'"$out"'/{}.txt" 2>&1 || true'
bad=""; total=0
for m in $mods; do
  f="$out/$m.txt"; n=$(grep -oE "^Ran [0-9]+" "$f" | awk '{print $2}'); total=$((total+${n:-0}))
  grep -qE "^OK" "$f" || bad="$bad $m"
done
echo "Ran $total tests"; [ -z "$bad" ] && echo OK || { echo "FAILED:$bad"; for m in $bad; do tail -25 "$out/$m.txt"; done; echo "TESTS NOT OK; not pushed"; exit 1; }
git fetch -q origin main; git merge-base --is-ancestor origin/main HEAD || { echo "main moved; re-run"; exit 1; }
git push -q origin HEAD:main && echo "LANDED $(git rev-parse --short HEAD)"
cd $repo && git worktree remove --force "$wt"
