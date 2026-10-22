# Decision 2026-10-20: a supplier lot traces through the receipt line

**Decided: Option 2 of the open record this replaces
(`open-documents-typed-lot-trace-served-lot.md`).**

**Decision.**
- `Lot` stays unserved where it is declared (`domains/items.json` placeholder,
  `pending/people_lots.json` over `public.app_inventory_lot`). It is never redeclared in
  `quality.json`. There is no loader change, store version or key change.
- The trace is keyed on `ReceiptLine.supplier_lot_number`, a `lot_number` reference from
  FM `lotnoqc` (raw material and packaging receipts) and the gold line's
  `supplier_lot_number`.
- `CertificateOfAnalysis.for_receipt_line` (mention, lead, N:M) links a certificate to
  the receipt lines whose supplier lot its `lot` field states. From the line the trace
  runs over served links:
  - `line_of` to the Receipt, then `supplied_by` to the Supplier;
  - gold: Receipt `receives` to the PurchaseOrder; FM: `receives_line` to the PO line,
    then `line_of` to the PurchaseOrder;
  - gold: `for_item` to the Product. FM receipt rows bind no product (residual).

**How a supplier lot stays a supplier lot.** The `lot_number` kind's alias kinds
(`receipt_lab_id`, `batch_code`) would let a lot candidate-match a PLX lab id. The COA
`lot` field therefore declares `refers_to_property`
(`ReceiptLine.supplier_lot_number`, kind `lot_number`), a new profile key in
`ontology/document_fields.py`:
- its reference resolves only on index rows of that kind on that property, never on an
  alias kind or another holder;
- references the identifier grammar extracts inside the field's own span (an RP-shaped
  lot read as a `receipt_lab_id`) are dropped, so the lot is one reference;
- the profile loader refuses a malformed target (`document_field_refers_to_property_invalid`).
`refers_to: lot_number` was not possible: the loader requires a mention link to the
kind's object type (Lot), which this decision forbids.

**Evidence.** `ontology/tests/test_lot_trace.py`: certificates from PDFs over gold and FM
receipts link only the matching lines and reach Supplier, PurchaseOrder and Product
through `plx.around`; an RP-shaped lot equal to another line's lab id links nothing; a
reader without the SharePoint file sees no certificate from the line. Disabling the
property filter fails two of them. The live measure is `ontology.lot_trace`
(`scripts/eval_lot_trace.py`, run as `scripts.eval_document_fields_live --lot-trace`),
live-unverified.

**Residuals.**
- `plx.find` on a supplier lot still searches the alias kinds (a free query, not a
  field). Closing it needs a `SupplierLot` kind (open Option 3) or an `identifiers.json`
  change, by that file's owner.
- `operating/case_workspace_live.py` resolves `refers_to` references through
  `mentions.mention_candidates` and does not apply the property filter to
  `refers_to_property` ones.
- `scripts/eval_document_fields_live.py` `_linkable` still reads identifier carriers only,
  so `ontology.document_links` counts a COA lot as not_linkable; the lot trace has its
  own check.
- Option 3 (a served supplier-lot type) remains the path if a certificate must resolve to
  one lot object.
