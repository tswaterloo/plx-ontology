# Decision 2026-10-04: po-fixture-roots (fixture PO numbers and oracle PO lineage follow upstream)

- decided: 2026-10-04 (option A)
- status: decided

## Context

The eval `po_three_way` stratum on the fixture world reported 4/4 `root_not_resolved`. Every
root was a fixture PO number, `FXPO2000NN` (`app_purchase_order.po_number`) or `FXFMNNNN`
(`custom_fields.fmPoNumber`). Neither is a Petra PO form (`ontology/schema/identifiers.json`
kind `petra_po`), so the projector indexed no PO key and `plx.find` had nothing to match.
The fixture shape was wrong, not the product: upstream never writes such numbers.

The oracle's PO lineage (`evals/oracle/generator.py` `lineage`) also read either
`fmPoNumber` or `fmGeneratedPoId` as proof of FM origin. Upstream does not support that:
Generate PO, the QA smoke script and the BC number alignment write those keys on
Portal-native rows too.

## Decision

Option A: fix the fixture and the oracle. Option B (the projector indexes values its kind
cannot parse, as exact) is rejected. It loosens the conservative identity grammar. Option C (leave it) keeps an eval stratum that measures a fixture defect.

1. The fixture world writes PO numbers in the Portal's forms, in a `9` range clear of the
   live series (PO100000 up): canonical `PO9200001` (formatPoNumber `PO{n}`), legacy FM
   `PO#90001`, operations-route `PO-9FX003` (`prefixRef("PO-" + base36)`).
2. FM rows carry the upstream stamp: `custom_fields.source` `etl-silver-to-gold` or
   `mirror-fm-pos-to-gold`, plus `fmGeneratedPoId` and `fmPoNumber`. Portal-native rows stay
   as upstream writes them: Generate PO rows carry `portal-generate-po` with the FM number
   and id; operations-route rows have no custom_fields. Three extra closed rows exercise the
   staging rules: a QA smoke row (`qaSmoke: true`, source overwritten to
   `etl-silver-to-gold`), a demo replica (`demoTag`, header `OPEN`), and a row renumbered to
   a BC No. outside the PO series (`FXBC9001`; no grammar reads it).
3. Oracle PO lineage: `qaSmoke: true` is staging. Otherwise a row is `gold_etl_from_fm` only
   when its source is one of the two FM loaders (`keys.PO_FM_SOURCES`, `PO_LINEAGE_CITE`).
   `fmPoNumber` or `fmGeneratedPoId` alone is staging. The oracle still imports nothing from
   `ontology/`.
4. Document spans: the oracle's dev-set generator compares a PO number the way the Portal
   looks one up: `TRIM(po_number) ILIKE TRIM($1)` (case-insensitive equality; matches the
   `petra_po` `exact_compare: trim_casefold`). So "po9200004" in a mail names PO9200004, and
   the question is asked from the stored number. Every other number stays exact text
   (resolveOrderByNumber is exact and case-sensitive). `identifier_spans` keeps its text.
   A longer run ("PO92000099") still never names a shorter number.
5. The frozen dev items, `evals/dev/manifest.json` and `evals/heldout/manifest.fixture.json`
   were regenerated with `python3 -m scripts.eval_oracle_dev fixture --write evals/dev` and
   `heldout --source fixture --manifest-out evals/heldout/manifest.fixture.json`, never by
   hand. Roots moved because ranking digests the identifier.

## Upstream evidence (plx-customer-portal@6e76a5b)

- `portal/src/lib/mrp/po-number-series.ts:52` formatPoNumber `PO{n}`; header comment: legacy
  `PO#5280`, `UAT-PO#5283`.
- `portal/scripts/etl-silver-to-gold.ts:776-781` stamps `fmGeneratedPoId`, `fmPoNumber`,
  `source: "etl-silver-to-gold"`; `:788-830` merges it into the row matched by
  fmGeneratedPoId / fmPoNumber / po_number and keeps a canonical or LINKED number.
- `portal/scripts/mirror-fm-pos-to-gold.mjs:296-300` stamps `source: "mirror-fm-pos-to-gold"`.
- `portal/src/lib/mrp/create-fm-purchase-order.ts:311-313` Generate PO stamps
  `portal-generate-po` with `fmGeneratedPoId` and `fmPoNumber`.
- `portal/src/app/api/operations/purchase-orders/route.ts:33,55-66` writes no custom_fields;
  number `prefixRef("PO-" + base36)`.
- `portal/scripts/qa-po-receive-smoke.ts:111-116` (`qaSmoke: true`, source
  `qa-po-receive-smoke`, FM keys), `:147-156` inserts the FM twin.
- `portal/scripts/business-central/demo-july-p2p-replicas.ts:273-298` (`demoTag`, status
  `OPEN`, no source).
- `portal/src/lib/business-central/align-portal-po-number.ts:48-61` sets po_number to the BC
  No. and keeps the old number in `fmPoNumber` on any linked PO.
- `portal/src/app/api/mrp/receiving/open-po-lines/route.ts:279-280` and
  `portal/src/app/mrp/ap-invoice-entry/ap-invoice-entry-queries.ts:65`: PO lookup by
  `TRIM ... ILIKE TRIM($1)`.

## Consequences

- Fixture `po_three_way` roots resolve (0 `root_not_resolved`). `plx.process` answers
  `three_way_undecided` (`undecided_stage_receipt`): the fixture world serves 13 of the 24
  relations the process needs, so the Portal capture is not complete and a receipt link stays
  a lead. That is a product-side completeness answer, not a root failure.
- The product schema still lacks a `qaSmoke` rule : until it lands,
  the product reads the QA smoke row as `gold_etl_from_fm` while the oracle reads staging. The
  oracle never samples it, so no eval item carries the disagreement.
- The oracle's own PO root lookup (`keys.ROOT_KEYS po_number`) stays exact text; its sampled
  identifiers are stored numbers, so this never loses a root.
