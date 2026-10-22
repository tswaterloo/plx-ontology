# Decision 2026-10-07: scoring the product's answers to proposed use-case items

- decided: option 3
- status: adopted; built (`scripts.eval_use_case_items
  proposal run --answer`, `evals.score` calculation op `expression`, text originals in
  `operating/case_originals.py`, live check `eval.use_case_answers`)

## Question
How do the product's plx.* answers to a captured use-case document's proposed items
(RRO Draft 8: 14 F21 comprehension, 12 F22 arithmetic) get scored honestly when no item is
human-labelled?

## Options
1. Treat proposed values as gold (score F22 "correct", F21 by rubric). Rejected: a proposed
   rubric/assumption is not truth (eval_use_case_items.py:473; label.status oracle_only).
2. LLM judge for F21 against the criterion. Rejected already (2026-10-06-use-case-evals-text-claims, option 3: scorer is deterministic).
3. Three buckets by label provenance (recommended): `scored` = items whose label.status is
   human_audited (item.schema.json label enum); `agreement_with_proposed` = oracle_only items
   whose expected value was independently verified (F22: recomputed by operating.cases.evaluate
   AND every operand found on the anchored lines); `unscored` = everything else (F21 without
   human-audited expected quotes/gaps). No pass/accuracy is ever computed over the last two
   as correctness.

## Evidence
- Admission on origin/main 117a8359 (offline): 12 F22 + 3 F21 admitted, 11 F21 empty_outcome.
- Case originals store is PDF-only (operating/case_originals.py:287,304,365): the Markdown original cannot be served; a run is blocked originals_not_held.
- Calculation ops are sum/count/equal (evals/score.py:197): 10032*2+... cannot be credited.
- Gap match is folded exact subject (evals/score.py:1045): the 3 gap-only F21 items are near-unpassable.

## Recommendation
Option 3. Gold for F21 (and for F22's interpretation) is
human-only: the document's author or the PLX owner of the release-order process records
expected quoted spans / gaps with audited_by and audited_at.

## Reversal
An owner directive that proposed items count as gold; or a human-audited label set arriving
(then F21 moves to `scored` with no code change).

## As built
- Buckets: `scored` (label `human_audited`; the only accuracy), `agreement_with_proposed`
  (`oracle_only` arithmetic whose proposed result recomputes from figures on its anchored
  lines), `unscored` (everything else, admission-refused items included); per layer and in
  total with `gap_only`, `blocked`, `refused` and `coverage`.
- Human-audited labels arrive as a card `audit` (`audited_by`, `audited_at`, and for a
  comprehension card the expected quoted values and/or gaps); such an item is `scored`
  with no code change.
- The answering agent reaches the original only through `plx.*` tools on a disposable host
  seeded from the recorded guarded capture (`OriginalStore.store_source`, provenance
  `sharepoint-recorded-capture`); its recorded permissions decide only while the capture is
  younger than 48 h (the SharePoint capture's bound). An unreachable original blocks every
  item before any session.
- Arithmetic answers use the calculation op `expression` over figures cited on the
  original's pages, evaluated by `operating.cases.evaluate` (no literal operands).
