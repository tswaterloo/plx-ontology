# Decision 2026-10-10: every customer-PO entry point reaches one scope gate

- decided: 2026-10-10 (gap-0c1337845944)
- status: decided

## Question

A customer PO asked in the
registry's declared marked form (`PO#PO-6000`, `PO #PO-6000`, `PO# 7000`) bypassed the
scope rule. `plx.find` answered it with one `display_text` candidate. That kind is not
scoped, so `ontology_tools.scoped_root_lead` passed it, and both the case opener and
`plx.investigate` rooted a PO with no customer (`so-4`) and a case/spacing variant (`so-5`).

Options: (a) a second gate that refuses a `display_text` or `filter_match` candidate on a
scoped kind's object type; (b) resolve the marked forms in `plx.find` to their kind
(`customer_po`), so the one gate sees every entry point.

## Decision

Option (b), with no second gate on `display_text`.

- `ontology/schema/identifiers.json`: the `customer_po` `marked` form declares its
  `marker` (a PO word and `#` before the value, matched case-insensitively at the start).
  `ontology/identifiers.marked_value` reads the value a marked text names, and `lead_key`
  is the blocking key without whitespace.
- `plx.find` reads a whole query in a declared marked form as a grammar lead (reason and
  feature `marked_form`, `identifier_kind` `customer_po`). The lead goes to the rows of
  that kind whose lead key the marked value shares. The Portal compares the whole text
  (`normalizePoReference` keeps the marker), so a marked text is never the PO it marks.
  `scoped_root_lead` returns the lead's reason, and the case path and `plx.investigate`
  refuse it as `root_lead_only:marked_form`. A found marked lead also stops the
  display-text search.
- BC scope is company plus customer number: the BC carrier declares
  `scope_within: company`. `ontology_tools.identity_scopes` prefixes the scope value with
  the environment and company of the record's capture handle. Two companies' customers
  that share a number are then two scopes, so `plx.find` reports
  `identity_scope_ambiguous` and `plx.identity` keeps them apart.
- Carried minors: `_scope_apart` keeps a pair apart only when both records name a
  customer of one scope space and the customers differ (agrees false). A record without
  a customer stays an `ambiguous_identity` question. The Portal carrier declares
  `scope_excludes` (`status` `CANCELLED`, order-identity.ts:125-137). A cancelled order
  is a lead (`scope_status_excluded`) and never makes the PO ambiguous. Beside a root it
  is listed as a related lead, not as a second candidate.

## Evidence

`tests/test_customer_po_scope.py`, isolated fixture plus injected BC transport. Marked
forms are tested through plx.find, CaseOpener and plx.investigate. The test file also
covers a cancelled order, a record without a customer in identity review, and two BC
companies. Each test failed with the registry declarations removed. A marked lead
matches by a whole-text `LIKE` suffix scan of the kind's rows. The scan is capped at
`LEAD_SCAN_CAP`, and a capped scan reports `truncated`.
