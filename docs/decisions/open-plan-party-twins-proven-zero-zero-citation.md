# Open: how an answer states a proven zero count

2026-10-21. Owner of the answer format and scorer
decides; `evals/score.py` is not changed here.

## Question

`plx.investigate` now returns a count of 0 only with a typed proof (plan item
`zero: complete_search`, answer entry `proof`: a complete root identifier search, a
carrier of the identifier on each counted relation with no candidate on it, and the host's
`relation_complete` proving the relation wholly captured and readable by the reader now).
A 0 counts no record, so it has nothing to cite. How may an eval answer (and the agent's
close-outcome claim) state it?

## Evidence

- `evals/score.py` `calculation_problem`: a `count` calculation needs at least 1 input.
- `evals/baseline.py` `DeterministicInvestigator.calculation`: a count is stated only when
  `value > 0` and its records are cited; anything else was an uncited figure, which turns
  the answer partial (`uncited_entries_not_stated`). Measured: stating the proven 0 that way
  makes dev-F2-0004/0006 `answerability_mismatch` on top of `value_missing` (9 instead
  of 7 answerability misses; 17/30 either way). The baseline now leaves a proven
  zero unstated and not counted as uncited.
- A stated value without a citation is `claim_uncited` (blocking) at the claim gate.
- Dev items expecting 0: dev-F2-0002 (shipment_count, shipments carry no customer PO, so
  no search can prove it), dev-F2-0004 and dev-F2-0006 (sage_order_count, provable).

## Options

a. A cited completeness claim: the answer value carries the proof (searched kinds, each
   relation and its `complete_at`) and the scorer and claim gate recompute it from the
   store's `relation_completeness` for that reader. Strong; needs a scorer version bump
   and a claim-gate rule.
b. Allow `citations_required: false` for a zero. Weak: an unsupported 0 is the confident
   absence the gate exists to refuse.
c. Keep it unstated (current): honest, but the three items stay `value_missing`.

## Default

(c) now; (a) is the named follow-up `eval-cite-proven-zero` (scorer + claim gate).
