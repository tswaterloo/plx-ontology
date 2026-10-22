# Open: the real agent's final answer diverges from the answer the gate passed

2026-10-21. Owner of the eval runner and the answer
gate decides (`evals/runner.py` scores the final answer as the
agent wrote it, by design: "never a rewrite here").

## Question

On the eval.real_agent run (`evals/bench/real_agent_failures.json`
`after.lane_run`, k=1, n=10, +/-30 points) all 3 confident_unsupported answers passed the
Workbench answer gate (`plx.case_get` answer mode, `operating.answer_gate`) and then wrote
a final answer the gate never saw. Should a final answer that differs from the gated one
be refused or sent back for one repair?

## Evidence (counts and codes only)

- dev-F2-0001, dev-F3-0004: the gated claims stated an identifier (customer PO, sales
  order number); the final answer values omit it while the text still names it. The
  scorer's close-outcome check flags its digits `claim_unstructured`.
- dev-F4-0001: the final text was rewritten after the gate and adds a line ordinal no
  value states (`claim_unstructured`).
- The same items' values and gaps pass; post_rebase_run (5/10, cu 0) passed dev-F2-0001
  and dev-F3-0004.

## Options

a. The runner re-runs `answer_gate.check_text` on the final answer's text against the
   final answer's values and returns one repair when it fails (as the gate does).
b. The final answer must carry the gate's submission id; a text or value set differing
   from the gated one is `answer_not_gated`.
c. Keep it (current): the scorer counts it, the run records the cause.

## Default

(c) now; (a) is the named follow-up `real-agent-final-answer-regate`.
