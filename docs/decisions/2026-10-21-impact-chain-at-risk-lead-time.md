# Decision: impact-chain at-risk lead time (option 1)

**Decided** 2026-10-21 (was open-impact-chain-at-risk-lead-time).

**Question.** Days at risk = material date + recorded lead time - the order's required date.
Which lead time?

**Decision.** Option 1: lead 0, with the typed gap `assembly_lead_time_not_recorded`
(state not_ingested) on every answer; days at risk is a lower bound and the summary says so.

**Evidence (captured schema, no live call).** The replica's public schema records no
build (assembly/make) lead time:
- `AssemblyOrder` and `AssemblyOrderComponent` (public.assembly_order, public.assembly_order_item)
  carry no lead-time column.
- The lead times that exist are purchase-side: Supplier/SupplierItem `lead_time_days`
  (app_supplier, app_supplier_product), FileMaker `lt` on packaging_supplier and
  raw_material_supplier. The PO's own dates already include these (adding them double counts).
- `lead_time` on app_product and the silver finished-product/packaging/raw-material relations
  (`ontology/schema/generated/portal_products.json`) is mapped to no property, and nothing in
  the Portal prisma metadata says whether it means buy or build time. Treating an unlabelled
  column as a build time would invent a number.

**Material date** (unchanged). The late/at-risk class comes from the shared po-status rule
(`operating.po_status.lateness_verdict`, via `plx.process`'s `late` / `at_risk`): a late PO is
in at its received date, else the later of today and the supplier's revised promise; an at-risk
PO at the supplier's latest promise.

**Revisit when** the catalog owner labels a public column as build time: map it to a property,
add it to the material date and drop the gap.
