# process-coverage: additive plx.process fields, withheld stages and the graphs not built

- raised: 2026-10-05
- question: may `plx.process` carry three more optional single-root fields in
  `operating/ontology_tool_contracts.json`, and how is a withheld stage shown under the frozen
  stage-state enum (`present`, `missing`, `out_of_order`, `not_applicable`)?
- options: (a) additive optional fields `next_stage`, `due` (per stage: `overdue`,
  `days_overdue`, cited `due_by`, `gap`) and `conflicts` (`$defs/conflict`), withheld as
  `missing` + gap `inaccessible`/`withheld_by_permission`; (b) a new stage state `withheld`
  (an enum change every consumer, operator_ui/workspace.js included, must learn); (c) leave the
  answers in `ProcessAnswer.roots` only (not on the wire).
- evidence: precedent `2026-10-03-process-3way-2-measured-by.md` (additive optional field); `withheld_by_permission` is the name plx.around/plx.timeline already use
  (`operating/ontology_tools_traverse.py` WITHHELD); the monotonicity harness
  (tests/test_ontology_tools_process.py) now also checks `next_stage` and `overdue`.
- default built: (a). The format (`_process.schema.json`) gains `implicit`, stage `due`, the
  `present` predicate and six hops per path (an FM AO reaches its order's Portal shipment in five).
- not built, follow-ups: quote -> order (no Quote binding or Quote-to-SalesOrder link on main;
  waits on schema-coverage); Ricardo's file-intake (21) and order-lifecycle-cost (18) maps and the
  full 15-stage po-delivery-window map: their stage lists are only on SharePoint (Operations
  Ricardo/process-maps spec.json, pinned in evals/exposed/sources.json), not in the repo, so
  `po_delivery_window` is built from the consolidated record's distinctions (planned arrival,
  shipment, receipt) and Vince's map stages 11/13/16; a live capture of the three spec.json into
  docs/reference is needed before encoding them. Vince's map stages 01-10, 12, 14, 15, 17, 18 have
  no declared ontology record, so `operating/process_stage_map.json` is not retired.

- decided (option a): additive optional fields; a withheld stage is `missing` plus an `inaccessible` gap. Already built (`operating/ontology_tools_process.py:200` doc of the withheld rule, `:1049` the `inaccessible` gap).
- the not-built items above stay open follow-ups.
- status: decided
