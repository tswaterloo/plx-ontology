# Use-case answer misses: the 26 RRO Draft-8 cards

Date: 2026-10-16. Evidence label: live-observed.

## Run

- Check `eval.use_case_answers`, exact spec command. It ran from a detached worktree of
  main `9ac148d`, with `ClaudeCodeAgent`, reader `staff:operations` and a
  host seeded from the recorded capture. Run started 2026-10-16T08:19Z.
- Totals: 26 items, 11 refused, 3 gap-only, 7 agreement-with-proposed (0 agree, 7
  disagree), 0 scored runs. The spec fails `answers.scored.runs >= 1`. The totals
  match the 2026-10-15 run.
- Per-card classes and evidence: `evals/dev/rro/answer_review.json`. This record holds
  no transcript bodies. Run outputs stay outside Git.

## Counts per class

The primary class is the cause that still blocks the card after every other listed
cause is fixed. The table also counts each class as a contributing cause.

| Class | Primary | Contributing |
|---|---|---|
| prompt_or_policy_refusal | 11 | 11 |
| agent_wrong | 9 | 11 |
| eval_harness_defect (added) | 4 | 15 |
| unit_or_scope_mismatch | 2 | 3 |
| tool_missing | 0 | 3 |
| permission_or_audience | 0 | 1 (non-causal) |
| source_not_captured | 0 | 0 |
| proposed_value_wrong | 0 | 0 |

The given enum has no class for a fault in the measuring code. This record therefore
adds `eval_harness_defect`: the scorer or the item compiler rejects a right answer.
Every one of the 10 well-formed arithmetic answers holds the proposed value
(Decimal compare), so no card shows `proposed_value_wrong`. Every original read
resolved, so no card shows `source_not_captured`.

## Findings

1. **Arithmetic expected values never match (10 of 10 well-formed answers).**
   `scripts/eval_use_case_items.py` pins each expected value as name `result`, with
   citation `original://sha256/<sha>` and field `row.text`. `evals/score.py
   cite_identity()` returns None for that handle. `_match` therefore falls back to the
   name. The agent never names a value `result`, so every card scores `value_missing`.
   A scratch re-score with the agent value renamed to `result` turns A02, A04, A09 and
   A11 into `correct`.
2. **11 comprehension cards are refused before any session.** These cards hold only a
   prose criterion. The proposal compiler makes gaps only from pinned provenance
   conflicts, so admission refuses the item as `empty_outcome`. No agent ran.
3. **Uncited prose numbers (6 cards: A01, A03, A05, A06, A07, A10).** After the
   rename, each card still fails with `claim_uncited_number`. The prose restates rates,
   received amounts and a lot code that are not value claims.
4. **A malformed calculation drops the whole answer (A08, A12).** An `expression` value
   also carried `citations`. `calculation_problem()` returns
   `citations_beside_calculation`, the answer is malformed and the card scores
   `no_answer`.
5. **Expression input units (A05, A07).** A percent figure as input gives
   `calculation_input_unresolved`, although the page prints it. A currency given in
   the input `unit`, not `currency`, gives `calculation_currency_mismatch`. A03 also
   shows a non-blocking `claim_wording_role_mismatch` (ordered vs produced).
6. **Gap-only cards (RRO-01, RRO-06, RRO-14).** No agent gap names the pinned
   attachment-provenance conflict. RRO-06 and RRO-14 also answer `answerable` where the
   card expects `partial`. Two latent faults are also present:
   - The expected gap carries a verbatim subject and no `concerns`. `_states()` needs
     an exact folded subject, so a paraphrased conflict gap would also miss.
   - `plx.read` is page-addressed and shows no line numbers, but the cards pin line
     ranges. In RRO-01 a `text_offset` window read gave no page handle, so the agent
     cited the whole handle and the gate gave `claim_uncited`.
   - RRO-14 also met an `audience_not_granted` fixture denial on mail and SharePoint.
     That denial is non-causal: the answer needs only the original.

## Fixes ranked by cards fixed

"Needed by" counts the cards that cannot pass without the fix. "Fixes" counts the
cards that pass once this fix and every fix ranked above it land, judged by scratch
re-score where one was run.

| Rank | Fix | Class | Needed by | Fixes (cumulative) |
|---|---|---|---|---|
| 1 | Harness: match an expected arithmetic value by its recomputed value over the pinned page, resolve `original://sha256` pins against page citations and drop the name fallback as the only path. Match expected gaps by state, `source_conflict` reason or `concerns`, not a verbatim subject. | eval_harness_defect | 15 (all 12 arithmetic + 3 gap-only; latent on A08, A12 and the gap-only cards) | 4 (A02, A04, A09, A11) |
| 2 | Agent contract: a pre-submit answer check that runs `calculation_problem()` and the claim gate on the draft answer and returns the codes, plus a prompt rule that every number in prose is a value claim. Optionally the claim gate also accepts numbers taken verbatim from the question. | agent_wrong | 8 arithmetic (A01, A03, A05, A06, A07, A08, A10, A12) | +6 (A01, A03, A06, A08, A10, A12; A08 and A12 are expected, not re-scored) |
| 3 | Expression inputs: accept a percent figure as its fraction when the cited page prints it, and accept the currency in `unit` or from a currency sign. Alternatively the prompt and the pre-submit check name the field. | unit_or_scope_mismatch | 2 (A05, A07) | +2 (A05, A07) |
| 4 | `plx.read`: a window read (`text_offset`) returns its page number and a page citation, plus line addressing for line-pinned questions. The prompt also asks the agent to record contradictory source statements as `source_conflict` gaps. | tool_missing / agent_wrong | 3 (RRO-01, RRO-06, RRO-14) | +3 at most; not re-scored |
| 5 | Card audit: the owner turns each prose criterion into values or gaps the compiler can admit, or marks the card unscored by design. This is not product work. | prompt_or_policy_refusal | 11 | +11 at most; depends on the audit |

A note on `permission_or_audience`: one card met it, and the fixture denial was
correct for the reader. No fix is proposed.
