# Decided: answer-correctness classes and scope

Status: decided on 2026-10-04.

## Decision

- Scope: 1(b), as defaulted. Only three-way answers and later answer evals that publish
  `eval.answer` count. Root lookup is reported apart as `find_root`.
- Classes: a hybrid of 2(a) and 2(b). `three_way_undecided` with a typed gap counts as
  `abstain_ok`. `ambiguous` counts as `missed`. `found_lead`, `not_found` and
  `root_not_resolved` count as `missed`.

Reason: the oracle confirms that each identifier names exactly one root. A find that cannot
tell the candidates apart is a miss, not an honest abstention. A typed gap in a three-way
answer states what is undecided and stays an honest abstention. `ANSWER_CLASS` in
`scripts/eval_unfamiliar_sample.py` implements this, and
`tests/test_eval_unfamiliar_sample.py` pins `ambiguous -> missed`.

## Question

1. Which items count toward `answer_correctness`? Every sampled item, or only real answers?
2. How do outcomes that are neither right nor wrong map to `abstain_ok` and `missed`?

## Options

1. Scope
   - (a) Every `eval.unfamiliar_sample` item, root lookup included.
   - (b) Only the three-way answers (`po_three_way` through `plx.process`) and any later
     answer eval that publishes `eval.answer` (for example `eval.bc_three_way`). Root lookup
     (`plx.find`) is reported apart as `find_root`.
2. Classes
   - (a) `ambiguous` and `three_way_undecided` count as `abstain_ok`. The product states the
     ambiguity or the typed gap instead of choosing. `found_lead`, `not_found` and
     `root_not_resolved` count as `missed`.
   - (b) `abstain_ok` only when the oracle agrees that the question cannot be decided.
     Everything else that is not correct or wrong counts as `missed`.

## Evidence so far

- Root lookup is not three-way success. Coverage is published beside accuracy, so that all-abstain cannot look good.
- `scripts/eval_unfamiliar_sample.py` calls `three_way_undecided` "a typed gap is honest,
  not wrong, but not a pass".
- The oracle confirms that each identifier names exactly one root. An `ambiguous` find is
  therefore a failure to tell candidates apart. The truth is not ambiguous.

## Default taken (superseded by the decision above)

- Scope: 1(b).
- Classes: 2(a). `accuracy` is correct/scored, so an abstention never raises it.
  `coverage` (scored/sampled) is published beside it.

Option 2(b) needs the oracle to say "undecidable". That would also make `abstain_ok` stricter
for `ambiguous`. Switching is a change to `ANSWER_CLASS` in
`scripts/eval_unfamiliar_sample.py`; the published shape stays the same.
