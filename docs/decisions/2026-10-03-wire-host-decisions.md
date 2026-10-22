# 2026-10-03 wire-host decisions: absence evidence, driver reads, AO header sources

Main b86e78e; Portal c3ffda3. Each key claim was re-checked against the cited lines.

## Context
The wire-host stack (166d0ad..2643a25) tightened absence and field rules in
`operating/capability_guards.py`. Three `open-wire-host-*.md` decisions came in with it. One more
issue was found in the same area (WH-NUMERIC-EVIDENCE-1).

## A. uncited-absence: adopt option 1, and make the object check real
- **Decision.** Option 1 is adopted: a root search that finds no row records `unknown`,
  with the searched relations and literals in the why text. This is a rule, not a
  default. Option 3 is rejected. Option 2 (a `search://` handle) is deferred. A
  replayed search shows only that the search can be repeated, not that the row is
  absent. A renumbered PO still matches nothing on every replay
  (`test_po_key_roots_on_the_row_after_the_number_is_rewritten`). Replay is also
  impossible today: `SourceMirror.rows()` reads only `row_current`, and capture ids are
  per relation. Option 2 would touch about 11 production files.
- **Evidence.** Audit practice records the search (population, criteria, items) but does
  not treat it as proof of absence (PCAOB AS 1105 ¶.10:
  https://pcaobus.org/oversight/standards/auditing-standards/details/AS1105). Wikidata
  keeps `novalue` (asserted, with a reference) apart from `somevalue`. Palantir AIP
  citations point only to objects and documents
  (https://palantir.com/docs/foundry/agent-studio/citations).
- **Defect found (material, verified).** `absence_needs_citation` (2643a25) does not
  protect the object kind. `check_gap` skips every handle that is not `api://`
  (capability_guards.py:655). The object branch (:666) passes when no subject row is
  cited. `plx.case_record_gap` accepts `original://`, `graph-mail://` and other live
  schemes (mcp_server.py:825). So `absent_in_source PurchaseOrder` citing only
  `original://z` passes, and test_capability_guards.py:799 locks this in.
  **Rule:** an object-kind `absent_in_source` must cite at least one readable `api://`
  row of a relation that holds a link or identifier to the subject type, from which the
  object is absent. A citation that is not `api://` never counts toward absence.
  Otherwise the guard refuses with `absence_needs_citation`, and the caller records
  `unknown`.
- **One absence prover.** `plx.find`/`plx.object` emit `absent_in_source` on their own
  (ontology_tools.py:1097-1115 `_absence_proven`, :1683). The projector reads a missing
  column as absent with no self-reference check (ontology/project.py:530). That is a
  second prover with different rules.
  **Rule:** every `absent_in_source` claim, from any tool, uses the
  `capability_guards.value_state`/`check_gap` classification. A key no-match on
  `plx.find` is `unknown` (`no_match`) unless `_absence_proven` holds *and* the search
  covered every identifier carrier of the type (renumbered keys included).
- **Wrong if:** a frozen-dev or held-out item labelled root `absent_in_source` fails only
  because a driver recorded `unknown`. Then build option 2 on as-of-capture reads.

## B. driver-source-reads: converge the identity reads now; delete source_reads after (b)
- **Decision.** Option (c) as written cannot work. `SourceResolver` looks up typed
  identifier keys only (ontology/resolve.py:14-17). It has no foreign-key hops, paging,
  catalog or SharePoint listing. Teaching it to traverse would build a second
  `plx.around`. Therefore:
  1. Now: the identity reads move to `plx.find`. That covers `mirror.resolve` in
     ao_case_script.py:348, :364 and case_workspace_live.py:251. The column comes from
     `matched.binding`/property, not `_by_column`. Citations must stay byte-identical,
     which holds because MirrorLookup sets `l1_handle` to the mirror citation
     (resolve.py:512). `resolve` leaves `source_reads.READS`, and the evaluator
     exemption (playbook_eval.py:1629) shrinks to the remaining reads.
  2. After (b), the host and drivers open a bound-relations projection at start
     (direction-batch §A: 21,091 objects in 35.7 s). Then the drivers move to
     `plx.around`/`plx.schema`, and `operating/source_reads.py` plus the evaluator
     exemption are deleted. `live.list` waits for a SharePoint binding or the documents
     primitive.
- **Why now.** While the exemption exists, a driver pass can overstate what an MCP agent
  can do (playbook_eval.py:1629), so eval passes are not a clean capability measure.
- **Wrong if:** `plx.find` misses AO-26005B where `mirror.resolve` hits, or the bound
  projection does not fit the host start budget.
- Minor: `CaseHostTools.close` is defined twice (ontology_host.py:110, :173). The second
  definition wins and leaves `_reader` open.

## C. ao-header-evidence-sources: defaulted (a)
- The Portal PATCH writes only `totalprodqty_c` and `qtysample`
  (plx-customer-portal@c3ffda3 route.ts). Registration of `qtytotal`/`finalqty` writes
  stays refused, so the declared mirror-row sources carry no execution risk. Option (b)
  waits until confirmed packet fields carry a machine-checkable value per field. Which
  figure each header field means is a later_ask for Quality not a blocker.
- **Related fix (WH-NUMERIC-EVIDENCE-1, verified).** `capability_guards._text`
  (:158-163) accepts only `str`/`int`. A JSON float quantity (1509.0, ratio 1.0) is
  therefore `unreadable` and over-refuses a valid AO proposal. **Rule:** the schema or
  the catalog declares a field's value kind. Numeric evidence parses as a finite
  `Decimal` that keeps its raw provenance. Identifier fields (customer PO) still reject
  floats.

## Consequences
1. absence-guard: rules A.object and A.one-prover, plus the numeric value
   kind (C). It also folds `case_verification._field_value` (case_verification.py:66-120,
   a near-copy that disagrees on float, bool and list values; it feeds
   goal2_preconditions.py:81) into `capability_guards.read_field`/`value_state`.
2. driver-resolve: B.1.
3. host-budget: the case host lists aggregate/compare/identity/documents/read/investigate/act by
   consolidating `case_*` tools. Today 15 of the 22 slots are case/doc tools and
   `plx.process` is not listed (tests/test_mcp_server.py:150 pins 22).
