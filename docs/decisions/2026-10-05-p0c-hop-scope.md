# Decision 2026-10-05: which identifier kinds may seed a plx.find linked_identifier hop

- decided: 2026-10-05
- status: decided; built in `operating/ontology_tools.py` `_linked_matches`

## Question

plx.find follows a typed identifier match on another type's record through a declared
single-valued link to a record of the asked type (`linked_identifier`): an order number
asked of Customer returns the order's customer. When FM ids became self identifiers
(`Customer.fm_id`, `Product.fm_id`, keyed `within_binding`), an FM order or product whose
small integer id equalled a customer's hopped to its owner, and that owner came back as a
verified candidate. The first repair stopped every `within_binding` key from seeding a
hop, which also stopped order, PO and invoice numbers.

## Decision

1. A hop is suppressed for an identifier kind that is ambiguous across tables: two or more
   object types declare a `within_binding` identity key of that kind. Derived from the
   schema, never a list of kind names (today it selects `fm_id`).
2. Independently, no hop from another type's match of a kind the asked type itself keys
   `within_binding`: the value then names the asked type's own record.
3. Every other hop is unchanged (order number to Customer, PO number to Supplier, invoice
   number to Customer).

The rule counts object types, not bindings: one type that keys its number in several
bindings of the same record (SalesOrder over Portal, FM and BC) names one record, not one
per table.

## Evidence

`ontology/tests/test_find_identity.py`
`test_a_linked_identifier_hop_is_followed_unless_the_kind_is_ambiguous` (isolated
fixture): FM ids 89 and 5 asked of Customer return no owner, and order and PO numbers
still hop to their party.
