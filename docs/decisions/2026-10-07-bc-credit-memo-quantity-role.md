# Decision 2026-10-07: BC credit memo line quantity has role returned

- decided: 2026-10-07 (against main 488da5b7)
- status: decided

## Decision

Option 1: `credited_quantity` keeps quantity role `returned` (resolves open-bc-credit-memos-quantity-role).

## Evidence

- Posting a credit memo Item line puts the goods back into inventory (ontology/schema/domains/bc_finance.json:715).
- operating/order_margin.py:505-512,723-727 already reads line_type first, so only Item lines carry goods.
- 2026-10-05-margin-net-credit-capture-units builds on this role.
- Option 2 (a new role) would change the frozen QUANTITY_TYPES (operating/quantity_types.py:47-50).

## Follow-up

Remove the 'Open decision:' pointer at ontology/schema/domains/orders.json:3568 (done in this change).
