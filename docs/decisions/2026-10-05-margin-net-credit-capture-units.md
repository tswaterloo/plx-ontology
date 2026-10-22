# Decided 2026-10-05: the credit-memo capture proof of order margin (P9) at capture-unit level

## Decision
Option 1. `InvestigateTools` takes a host `captured(relation, root_id)`
(`capture_claims` over `OntologyTools`, wired in `operating/ontology_host.py`). It reads the
complete `company=<id>` claims in `store_meta.source_freshness` with the logic of
`plx.process` (`_scope_claims` / `_claim_cause`). An engine that takes `captured` gets
`captured(relation)` for the plan's roots: True, False (a claim did not finish) or None
(no claim, or not a BC root). `order_margin` drops `CREDIT_MEMOS_NOT_CAPTURED` when the run
is complete, the customer was reached, the reader's scope holds (the `loose_memos`
withheld path still keeps the gap) and both `salesCreditMemos` and `salesCreditMemoLines`
are claimed complete for the order's company. None keeps the family-level proof. The cost
side keeps `COST_COMPLETENESS_UNPROVEN` unchanged.

## Question
`CREDIT_MEMOS_NOT_CAPTURED` should depend on the order company's
`salesCreditMemos` (+ `salesCreditMemoLines`) capture units being complete and in the
reader's scope, not on the whole BC family. The cost side keeps its own completeness gap when
the ledger is capped. How does the plan engine (`operating/order_margin.py`) learn about
unit-level completeness?

## Evidence so far
- Reader scope is done in this branch. The plan's `loose_memos` step (`plx.around` from the
  order to its customer's credit memos) reports `marking_withheld` / `withheld_by_permission`
  for a reader whose grant leaves out a credit-memo unit. The engine then keeps the gap. The
  gap is policy-derived, so it appears whether or not a memo exists
  (`tests/test_investigate_order_margin.py`).
- A plan engine receives only evidence entries, edges, `complete`, a citation resolver and
  `covers(link_type)` (`operating/ontology_tools_investigate.py` `_compute`). The only capture
  state among them is `meta.freshness`, which is per source family
  (`ontology/project.py` `freshness`).
- Per-unit completeness exists as scoped claims in `store_meta.source_freshness`
  (`bc.<relation>.scopes`, `company=<id>`, written by `ontology/project_bc.py`). Only
  `plx.process` reads those claims (`ontology_tools_process._scope_claims` / `_claim_cause`).
  No primitive output carries them.

## Options
1. Host input (smallest). In `InvestigateTools._compute`, pass the engine a
   `captured(relation) -> bool | None`. It is true when a complete `company=` claim of the
   root's company covers `relation` in `store_meta.source_freshness`. `plan_run` takes it as
   an optional keyword. The engine then clears the gap when `salesCreditMemos` and
   `salesCreditMemoLines` are claimed complete, the reader scope holds and the customer was
   reached. About 15 lines in the host plus one test.
2. A process graph rooted at `SalesInvoice` (a credits stage with an all-status predicate).
   Its absence and every() proofs use the claims. Rejected: it stretches stage semantics to
   carry a proof, and `operator_ui/object_workspace.py` calls `plx.process` without naming a
   process, so a new graph for an existing root type changes what that view shows.
3. Keep the family-level proof (current fallback). This is honest but too strict: live
   ledgers stop at their record caps, so every live answer keeps the gap.

## Default (superseded by the decision above)
Option 3 stayed until option 1 was taken; it is now the fallback for an
unknown claim. The cost side is unchanged and keeps its own `COST_COMPLETENESS_UNPROVEN`.

## Decision 2026-10-05: a credit memo that cannot be attributed

With the capture gap droppable, a credit memo of the order's customer that no invoice takes
would let the product state a confident gross margin. A memo
whose `invoiceId` says nothing (the nil Guid) and whose `invoiceNumber` names 0 or more than
1 invoice of its company — or no number at all — was netted nowhere and caveated nowhere, in
product and oracle alike (the plan's `loose_memos` filter counted only memos with no number).

Such a memo raises `credit_memo_unattributed` on the margin answer of any
order of that customer whose margin it could affect (the customer the `loose_memos` step
reaches, same company), with the memo cited. It is never netted — nothing says which order it
credits — so the answer is not a complete, confident net margin. A memo withheld from the
reader raises no such gap and is in no citation: the existing withheld path keeps
`CREDIT_MEMOS_NOT_CAPTURED` instead, so the gap never reveals that a memo exists. The oracle
reaches the same rule from the raw BC fields alone and scores an answer that states figures
without the caveat as wrong.

Built as:
- `SalesCreditMemo.applies_to_invoice_id` (text, `invoiceId`): the plan can now filter the
  memos BC states no invoice id for, instead of the memos with no invoice *number*. The nil
  Guid is matched positively (`op: in`), so a memo BC does name an invoice for — including a
  repeat customer's whole memo history — is never read.
- `ontology/plans/order_margin.json`: `loose_memos` filters on that property and
  `loose_applies` resolves their `applies_to` (one call over the set); no memo is read one
  by one. The `loose_credits` count select is gone.
- `operating/order_margin.py` `_unattributed`: a posted BC memo `loose_memos` reached
  (its `SalesCreditMemo.credited_to` edge) with no `applies_to` edge is applied to nothing
  (the schema's `unique_target` refuses an ambiguous or unknown number). The answer counts
  every such memo the reader may read in the entry `<name>_unattributed_credits` and cites at
  most `CITED_MEMOS` (3) of them by object id in its `basis`; the gap names that entry. So
  the answer stays one response however many there are (reading every memo made a customer with 8+ such memos fail with `result_too_large`).
  A memo applied by number to another order's invoice resolves, raises nothing and is not
  read.
- `evals/oracle/order_margin.py`: `UNATTRIBUTED_CAVEAT` now fires when `invoiceId` is blank or
  nil and the `invoiceNumber` is blank or names 0 or 2+ invoices of the company.

Residual (nil-Guid-only filter accepted): the product's filter matches the nil Guid,
not a blank `invoiceId` (BC API v2.0 serialises an empty Edm.Guid as the nil Guid; the oracle
treats both as no id). A capture that
left `invoiceId` blank would reach no memo and raise no gap, while the oracle would caveat it:
the eval would score that as wrong (loud), never silently net or over-state a figure.
