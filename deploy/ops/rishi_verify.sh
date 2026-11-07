#!/bin/bash
# Compare a scrubbed export against the unscrubbed one.
# Usage: [DEVLOOP_SHIM=DIR] [JOBS=4] rishi_verify.sh UNSCRUBBED_DIR SCRUBBED_DIR [extra test modules ...]
#   Runs, inside each tree, with a clean environment (env -i: no credential variable
#   reaches the tests): python3 -m compileall -q . ; the core set exactly as
#   python3 -m unittest <core>; and, per module in its own interpreter with a 900 s
#   timeout, the core set + the PAIR-edit proof modules + any extra modules, recording
#   one outcome per test. Exit 0 only if the scrubbed tree compiles and no test that
#   passes unscrubbed is worse scrubbed (a missing, failing, erroring or timed-out test).
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
set -uo pipefail
orig=$(realpath "${1:?unscrubbed dir}"); scrub=$(realpath "${2:?scrubbed dir}"); shift 2
VENV=$PLX_VENV/bin
# Outside both trees.
PYP=${DEVLOOP_SHIM:+PYTHONPATH=$DEVLOOP_SHIM}
CORE="evals.tests.test_answer_gate_one_path evals.tests.test_final_reply_gate evals.tests.test_runner
evals.tests.test_score operating.tests.test_claim_checks operating.tests.test_agent_loop
operating.tests.test_case_tools_host tests.test_mcp_server ontology.tests.test_contracts
operating.tests.test_mcp_output_firewall"
# modules that prove the PAIR edits of rishi_scrub.py (code/data changed together)
PAIR_PROOF="tests.test_case_originals tests.test_purchasing_po_prices
integrations.tests.test_capture_teams_all_channels tests.test_mirror_measure
operating.tests.test_goal2_e2e operator_api.tests.test_app_api"
MODS="$CORE $PAIR_PROOF $*"
out=$(mktemp -d $PLX_SCRATCH/rishi-scrub-work/verify.XXXX)
run() {  # $1 tree, $2 label
  ( cd "$1" && find . -name __pycache__ -prune -exec rm -rf {} + ; \
    env -i PATH=$VENV:/usr/bin:/bin HOME=$HOME python3 -m compileall -q . > "$out/$2.compile" 2>&1; \
    echo "compileall exit $?" >> "$out/$2.compile"; \
    if [ -z "${DEVLOOP_SHIM:-}" ]; then \
      env -i PATH=$VENV:/usr/bin:/bin HOME=$HOME timeout -k 10 1200 python3 -m unittest $CORE \
        > "$out/$2.log" 2>&1; echo "unittest exit $?" >> "$out/$2.log"; \
    else echo "plain core run skipped under DEVLOOP_SHIM" > "$out/$2.log"; fi; \
    env -i PATH=$VENV:/usr/bin:/bin HOME=$HOME $PYP timeout 6000 python3 \
      $PLX_OPS/rishi_unittest_outcomes.py "$out/$2.out" --jobs ${JOBS:-4} --timeout 900 $MODS > "$out/$2.detail" 2>&1; \
    find . -name __pycache__ -prune -exec rm -rf {} + )
}
run "$orig" orig; run "$scrub" scrub
sort -o "$out/orig.out" "$out/orig.out"; sort -o "$out/scrub.out" "$out/scrub.out"
for t in orig scrub; do
  echo "== $t: $(tail -1 "$out/$t.compile"); core via python3 -m unittest: $(grep -E '^Ran |^(OK|FAILED)' "$out/$t.log" | tr '\n' ' ')"
  echo -n "   all modules, per-test outcomes: "
  awk '{print $2}' "$out/$t.out" | sort | uniq -c | tr '\n' ' '; echo
done
worse=$(join -a1 -e MISSING -o 0,1.2,2.2 "$out/orig.out" "$out/scrub.out" |
        awk '$2=="ok" && $3!="ok" {print}')
if [ -n "$worse" ]; then echo "WORSE IN SCRUBBED:"; echo "$worse"; fi
diff <(cut -d' ' -f1 "$out/orig.out") <(cut -d' ' -f1 "$out/scrub.out") > /dev/null ||
  echo "NOTE: test id sets differ (see $out/orig.out, $out/scrub.out)"
echo "logs: $out"
grep -q 'compileall exit 0' "$out/scrub.compile" && [ -z "$worse" ]
