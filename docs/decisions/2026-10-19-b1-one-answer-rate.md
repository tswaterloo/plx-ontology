# Decision 2026-10-19: B1 is one selective-prediction rate pair on every path

- decided: 2026-10-19
- cites: docs/decisions/2026-10-04-t2-metric-answer-classes.md (partly superseded, see below)
- paths: evals/score.py (answer_rates, confident_unsupported), scripts/eval_dev_baseline.py (answer_block), scripts/eval_real_agent.py (part_summary, _unsupported_rate), verify/live-checks/eval.dev_baseline.json and eval.heldout_product.json (notes)
- status: decided; implemented

## Question
The B1 bar (accuracy >= 0.8, coverage >= 0.8, confident unsupported <= 0.02) was applied
to two different metrics. `answer_block` counted a justified abstention (`abstain_ok`) as
covered and in the accuracy denominator; `outcome_summary` (agent, Ask and use-case paths)
used coverage = answered/runs and accuracy = correct/answered; the confident-unsupported
rate was over scored in one and over runs minus blocked in the other.

## Decision
B1 "accuracy at coverage" is the selective-prediction pair, computed by one function,
`evals.score.answer_rates(runs=, answered=, correct=, unsupported=)`:

- coverage = answered / runs. An abstention, justified or not, is not covered.
- accuracy = correct / answered.
- confident_unsupported_rate = confident unsupported / answered, where confident
  unsupported (`evals.score.confident_unsupported`) is a wrong answer stated without
  abstaining that the claim gate refused (`unsupported_assertion` or a failed claim check).
- Each rate is null when its denominator is 0; a null rate fails the bar.

`outcome_summary`, `answer_block` and `_unsupported_rate` all call it. Runs are every
scored row, blocked ones included (a blocked run is never answered); answered excludes
every abstention (in `answer_block`, correct + wrong; in `outcome_summary` an unjustified
abstention is wrong but not answered). `_unsupported_rate` recomputes the rates after the
Ask path adds its gate-refused stated answers to the numerator, so the Ask part reports
the same function's output.

## Relation to the 2026-10-04 record
Its answer classes stay for labels: `abstain_ok` is still the honest-abstention class,
reported in the block's counts. Its accuracy denominator (scored, which included
`abstain_ok`) is superseded for B1 rates only. An all-abstain run now reads coverage 0
and accuracy null, which answers that record's concern that all-abstain cannot look good.

## Consequences
The dev-baseline rates on the live run will move: abstentions now lower coverage instead of
raising accuracy. The `accuracy`/`coverage` strings in the block read `correct/answered` and
`answered/ran` (ran = sampled less blocked upstream, the rate's own denominator), and
`verify/live_verify.py` `answer_correctness` (and a tier's `answers`) give the same two
fractions over the summed counts, so one live report carries one accuracy.
`eval.unfamiliar_sample` keeps its own fractions and is not a B1 rate.

The Ask path's stated answers that the claim gate refused whole are scored abstentions, yet
the agent stated them: `_unsupported_rate` counts each as answered and confident
unsupported both (`gate_refused_answers`), so numerator and denominator agree, no rate
exceeds 1, and the error leans towards failure (accuracy and the unsupported rate both
worsen). `answer_rates` refuses counts that would lift a rate above 1.

## Known limitations of the held-out delta build (B1 part 2)
- Mixed world: a variant's delta re-projects over every relation the base holds, while
  only the group's plan relations (those its transform touches) are perturbed; relations
  outside the plan stay as the base read them. Kept: on the dev fixture, a comparison over
  all 6 transforms gave item outcomes and reasons identical to a full perturbed rebuild
  (`HeldOutProductTests.test_a_variant_group_is_a_delta_over_the_base_build_that_scores_as_a_full_one`).
- Each group copies the base mirror and seeds a byte copy of the base store (its text and
  vocabulary rows included). Their disk and time at the live 1.6M-object scale are not
  measured: live-unverified until the daily eval.heldout_product records them.
- A touched relation the variant's sync does not capture again (other than the source
  `partial_source_failure` breaks on purpose) is counted `sync_failed` in
  `variant_products` and makes the run incomplete (`variant_sync_failed`).
