# 2026-10-19 supplier-number alias leads and their rejection

- decided: 2026-10-19
- paths: ontology/schema/identifiers.json (reference_alias), ontology/identifiers.py (reference_alias_of), ontology/mentions.py (propose_reference_aliases, reference_alias_outcome, reader_view), ontology/reviews.py (reject_alias, BuildAliases.rejected, alias_proposals, format plx-reviews/v4), operating/ontology_tools_review.py, operating/ontology_tool_contracts.json, operating/case_workspace_live.py (_reference_alias), verify/live-checks/case.po_document_chain.json
- status: decided

Evidence base: ER-02 (an alias comes only from a human review) and ER-05 (a machine link is identity only when two independent features agree). A supplier confirmation often states the supplier's own number (an SO or PI number) next to Petra's PO number; later documents (packing slip, invoice, freight) may carry only the supplier's number. Today that number answers no record, so those documents join no chain.

## Question

ER-02 and ER-05 say who may make an alias and when a machine link is identity. They do not say:
1. When the machine may propose an alias.
2. How a person turns a proposal down so that it stays down.

## Decision

1. **Which kinds.** An identifier kind may name a supplier's reference to another record. Only a kind whose `reference_alias.of` (identifiers.json) declares that record type can be proposed. Today that is `vendor_confirmation` and `pi_ref`, of `PurchaseOrder`. No supplier or order is named in code.
2. **When a lead is proposed.** The build proposes a lead when a document version meets all of these:
   - it states such a number, and no index row or reviewed alias answers the number now (a stretch an alias held before its rejection does not answer it now);
   - it is not quoted history and has no label/kind conflict;
   - the same document version's mention links reach exactly one object of a declared type at tier `corroborated`, so two independent features agree (ER-05).

   The lead is a `reference_alias_lead` reason feature on those corroborated links. It cites the field the number is read in. Several corroborated targets, or none, give no lead and are counted by typed reason.
3. **A lead is never identity.** Nothing joins through a lead. Only a person's `alias` review through `plx.review` makes the number an alias (ER-02). The review has the identity gate `review:identity`, and the grammar re-reads the number in the cited field. `ontology.reviews.alias_proposals` gives each lead as that exact review payload. After acceptance, the next build resolves later documents that carry only the number through the reviewed alias, like any reviewed alias. The link says `via: reviewed_alias` and cites the review's evidence.
4. **Rejection: `reject_alias`.** This is a new identity decision with the same key as the alias (subject, kind, normalized value), so the later of the two is in force and `retract` gives the earlier back. While a rejection is in force:
   - the alias joins nothing (`alias_spans` gives only `alias` stretches). A link made while an accepted alias held keeps that past stretch, closed at the rejection, as any bitemporal link does; none is in force after it;
   - the build does not propose that pair again (`BuildAliases.rejected`).

   The review store format becomes `plx-reviews/v4`. The table CHECK lists the new decision, and an older table is copied row for row on the writer's open, as v1 to v3 were.
5. **Readers.** A reader who may not read the corroboration does not see the lead (`reader_view` strips it with the corroborating features). `alias_proposals` is actor-free, like the links. Its caller shows a proposal only to a reader who may read every one of its `evidence_handles`.
6. **Live summary.** `capture.projection.reference_alias.outcome` reports one of:
   - `alias_joined` (a link through a reviewed alias in force now, `reviewed_alias_links_open`);
   - `alias_lead_pending`;
   - a typed reason no candidate was found: `alias_rejected`, `several_targets`, `no_corroborated_target`, `no_supplier_number` or `mentions_not_run`.

   It also reports the counts.

## Amendment 2026-10-19 (9a50b8524)

- The upstream citation moves from "no upstream writer found in plx-customer-portal@82549dd" to plx-customer-portal@55e78ca: `PrinterOrderConfirmation.confirmationNumber`, `purchaseOrderId @unique` (`portal/prisma/schema.prisma`, about 3631-3632 and 3677-3690, added by 90f9d1d on 2026-10-16). A buyer records the supplier's number on the PO there. The commit was not reachable from the build checkout, so this is cited as stated and not verified here.
- `vendor_confirmation` declares that relation as a carrier with `role: index`, `served: false` and `gap: unserved:public.app_printer_order_confirmation`: the reporting API allow-list does not serve the table, so the Workbench reads no recorded number. Once it is served, the carrier indexes the number on its PurchaseOrder and a number answered that way is no longer a lead. Until then the mail-read lead path above is the only one. A PO without a stored row is not unconfirmed (the table is not visible).
- The recorded number is a printer-PO field. A non-printer supplier confirmation still yields an alias lead from mail.
- `alias_proposals` reads only links in force (`superseded_at IS NULL`): a lead on a closed link stretch is not shown pending.

## Residual risks

- A rejection stays in force when the cited mail changes. A person said "this number is not this PO", and that does not depend on the bytes. Only a later `alias`, or a retraction, lifts it.
- A lead on a mail whose corroboration rests on `party_domain` trusts the party's website domain. A supplier that shares a domain with another party never corroborates, so it gets no lead.
- Several copies of one mail give one lead per copy (`alias_leads` counts each). `alias_proposals` folds them per subject and alias.
- No console screen lists proposals yet. `alias_proposals` is the library entry point, and the counts reach the live summary.

## What would show it wrong

- A reviewer accepts a lead whose number names another PO. That is a sign that two features agreeing on the PO link does not carry over to a second number in the same mail.
- A rejected number joins a chain on a later build.
