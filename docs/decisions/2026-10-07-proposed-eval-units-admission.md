# Decision 2026-10-07: Proposed eval units enter through one admission rule

- status: decided (decision draft proposed-eval-units-admission, option c)

## Decision

`evals/admission.py` `admissible(item)` returns reason codes; a proposed unit is admitted
only with none. It needs at least one known_outcome value or gap (score.py passes any
answer with matching answerability when none is expected), `closed_transaction` true
(2026-10-07-oracle-strata-closed-roots-only; exposed cases derive from pinned originals,
not an oracle root, and are exempt), and real lineage only as `-exposed` with values
citing pinned sources (score.py `uncited_expected`). The employee auditor's three units
(460d336e) are refused and recorded in `evals/proposed-dispositions.json`.

## Rejected

- A separate directory: a second home beside `evals/exposed`.
- A suffix filter in `evals/dev`: the generator rewrites `evals/dev`, and `-employee` ids
  would partition as synthetic dev (`evals/score.py` partition_of).

A proposed unit enters a scored set only through the existing partitions. Real roots
without values belong to the live oracle in host state, not Git.

## Reversal

An owner directive to commit unscored employee roots, or the scorer treating
empty-outcome items as unscored.
