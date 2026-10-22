# Decision 2026-10-10: a byte-pinned document field may supply an owner-system parameter

- decided: 2026-10-10 (gaps gap-d99b89f5f37d, gap-cb8966a68bb2), under
  owner policy
- status: decided

## Question

Rule 2 of the injection defence says a document is source text as a whole, and a parsed
value is a lead, never a parameter. May a field of a byte-pinned SharePoint file version
or mail attachment supply the value of an owner-system proposal (the customer PO of
`portal.sales_order.set_customer_po`)?

## Decision

Option 2, as built: keep the rule-2 exception. A document field supplies a parameter only
when all of these hold:

- `ActionPath.register` reads the `documentFields` row through the actor's current reader
  (`capability_guards.current_reader`: the pinned bytes, served by the capture gate to this
  reader now and re-extracted). The case's stored capture view never counts. Other bytes,
  or a read the gate refuses, refuse the proposal (`citation_changed` on the action path,
  `capability_guard_failed:value_in_cited_evidence` on `plx.act`).
- The cell is `fields.<name>.value`, a schema document binding declares its role
  (`CustomerPoDocument.po_number`: `identifier:customer_po`), and it supplies only a field
  of that role.
- The profile's `attests` rule names the proposal type for that field. The document's
  evidence is cut to those fields (`document_fields.proposed_fields`) before the catalog
  and injection traces read it.
- The catalog guards, `person_confirms_evidence_belongs_to_order` and the per-call human
  approval all still apply. The gate reason names the source and the file:
  `value source: cited <source>.documentFields row.fields.po_number.value as
  identifier:customer_po; file "<name>" sha256 <first 12 hex>`.

The playbook evaluator reads the same rows: `plx.playbook_evaluate` passes the evaluator's
own current reader (`document_rows`). `ActionPath.escalate_to_mc` takes the host's reader
and passes it to the follow-up's record and registration, as `plx.act` does.

## Rejected

Not chosen; the decision keeps the exception with its three conditions
(reads through the actor's current reader, fields cut to the profile's `attests`, human
approval per call):

1. Keep rule 2 whole: a document-cited proposal is recorded and passes the guards but never
   registers; a person copies the value into a cited Portal/Sage row or a human-written
   case question.
3. As 2, but only for a document link an operator reviewed (`ontology/reviews.py`).

## Residual risk

The document's author chooses the value and the file name. A crafted customer-PO PDF can
put a PO-shaped value before a human approver. The approver sees that the value comes from
a document field, and which file (its name is quoted and cut to 40 characters). Evidence is
isolated-fixture and injected-transport only; live-unverified.

## Evidence

- `operating/tests/test_action_document_citations.py`: registers for the document's reader
  only, with the file in the gate reason; other bytes and a revoked read refuse; a
  document-cited MC follow-up registers for its reader only; the evaluator scores the
  proposal without a flag, directly and through `plx.playbook_evaluate`.
- The question, options and first evidence are in the deleted open file (git history
  before this commit): `open-act-document-citation-rows-document-field-provenance.md`.
