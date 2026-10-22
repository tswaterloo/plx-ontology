# bc-three-way-price-basis: how the BC three-way price check becomes decidable

- decided: 2026-10-08 (resolves open-bc-answerability-gaps-three-way-price; raised 2026-10-07)
- status: adopted; built. Evidence label: fixture and injected-transport only (tests/test_ontology_tools_investigate.py BcHostClaimTests); live-unverified.
- question: `three_way_match` stays partial on a BC purchase order because the PO vs invoice unit price check is undecided for every case-host reader. Two blockers: (1) BC unit costs carry the `finance` marking and `case_host_policy` (`operating/ontology_host.py`) grants markings to nobody (`marking_withheld`); (2) both price sources declare `tax_basis: unknown`, so even a finance reader gets `tax_basis_unknown`.

## Decision

1. Do (c) first: a per-record `tax_basis_from` read from the header's `pricesIncludeTax` (true is `incl_tax`, false is `excl_tax`), like `currency_from`. `integrations/bc_purchase.py` (~:76, :89) already selects the field.
2. Then (b): the case host learns who holds finance from the live audience's declared finance resource set (upstream key `operations.finance`, plx-customer-portal@549f6f0 `portal/src/lib/auth/data-key-registry.ts:18,63`). No new grant source is invented.
3. Keep (a) until both land: the verdict stays undecided on BC, listed and gapped, never read as agreement.
4. Reject (d): AP three-way match usually includes price; reporting price apart changes the meaning of `three_way_match`.

Caveat: the PO side compares `directUnitCost`; the invoice side compares `unitCost`, which may include indirect cost.

## Evidence

- Fixture (BcHostClaimTests): the manager gets ordered/received/invoiced 100 EA cited and `ordered_vs_received` = `received_vs_invoiced` = true; `three_way_match` null with a `marking_withheld` gap.
- A probe that adds the `finance` marking to the same host policy stops at `tax_basis_unknown` (from `PurchaseInvoiceLine.unit_cost@bc.purchase_invoice_lines` and `PurchaseOrderLine.bc_direct_unit_cost@bc.purchase_order_lines`).
- The frozen value row has no tax-basis column, so the projector or the comparison derives it from the header.

## What would reverse it

A live read where `pricesIncludeTax` is null, or where `unitCost` differs from `directUnitCost` on matched lines. Then the tax basis cannot be read from the header, or the two prices are not the same quantity, and option (d) or a cost-basis field is reopened.
