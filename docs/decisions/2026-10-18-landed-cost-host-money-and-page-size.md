# Decided: landed cost on the general host path (money markings, page size vs capture labels)

2026-10-18. Status: DECIDED, 2026-10-18 23:10Z: Q1 option (a), Q2 option (a). The question
and evidence below are as raised, the decision and its implementation follow each question.

## Question 1: who holds the `finance` marking on the case host?
- Rule: money read from a document is always marked `finance` (ontology/schema/document_fields.json, description).
- On the host, the read policy is `ReadPolicy(actor.tenant, grants)` with no markings (operating/ontology_host.py:300). Its docstring says markings are "held by nobody".
- Result: on the general path, SupplierConfirmation.amount/unit_price and ForwarderQuote.amount are withheld from every reader. landed_cost can only report `unpriced_line` and `freight_quote_amount_unread`. Quantities and text fields come through.
- Evidence: verify/tests/test_live_verify_landed_cost.py, run through a probe at d61bf8a2c. The projected confirmations carry only `quantity`; there is no amount entry.
- Options:
  - (a) a host-config marking grant (marking -> principals), for example `finance` to staff:operations and staff:finance;
  - (b) the live audience document carries marking grants;
  - (c) keep "nobody": the live check runs as a finance reader through another path.
- Default: (a), declared next to `live_audience` in the host config and default deny. It is a permission change, so it needs an owner decision.
- **Decision (2026-10-18 23:10Z): (a).** A host-config `marking_grants`
  (marking -> principals), declared next to `live_audience`, default deny: a marking no grant
  names is held by nobody. No employee principal holds `finance` until PLX names them
  (later_ask: **finance-marking-principals**). The live check `case.packaging_landed_cost`
  runs as a verifier principal (`svc:landed-cost-verifier`) granted `finance` in its own
  example host config (examples/packaging_landed_cost_host.live-check.json), so landed cost
  is proven end to end without widening any employee access.
- Implementation: `operating/ontology_host.py` (`check_marking_grants`, fail closed: an
  unknown marking, a non-list, a wildcard or a principal without a kind is
  `marking_grants_invalid`; `case_host_policy` holds a marking only for the principals
  granted it), `operating/host.py` (`marking_grants` config key, `host_config_invalid:
  marking_grants`; passed to the served tools and as `--marking-grant` to the MCP server),
  `operating/mcp_server.py`, `operating/case_workspace_live.py`. Test:
  verify/tests/test_live_verify_landed_cost.py (no grant: no confirmed amount reaches
  staff:operations or the verifier; with the grant, staff:operations alone still sees none,
  the granted verifier does; an invalid grant fails closed before any read).

## Question 2: paged investigate pages leave no room for the host's capture labels
- How pages are built: `_bounds` packs each answer page up to `max_result_bytes`, 48,000 (operating/ontology_tools_investigate.py:2061-2066).
- The host then adds `as_of_capture` gaps for the SharePoint versions it served, and returns `result_too_large` when the page no longer fits (operating/case_tools_host.py:1063-1066).
- Measured: the landed_cost answer for the synthetic case is 138,328 bytes as an envelope, so it is paged. Page 1 is 46,634 bytes before the labels.
- Every multi-page plx.investigate answer that cites captured SharePoint files can hit this.
- Neither a plan `limit` nor a more compact plan avoids it:
  - the limit does not bind (14 chain objects against limits of 50);
  - any paged answer is packed to the cap.
- Options:
  - (a) the host passes a label reserve to the engine's page bound;
  - (b) `_label` moves the overflow into the summary gap and trims the chain/values the engine's `_fit` trims.
- Default: (a).
- **Decision (2026-10-18 23:10Z): (a).** The host passes a label reserve to
  the engine's page bound so `as_of_capture` labels always fit within the page.
- Implementation: `InvestigateTools(label_reserve=...)`; `_bounds` and `_fit` answer to
  `page_cap` (`max_result_bytes` less the reserve), the final check to the whole limit.
  `CaptureCaseHostTools` reserves `LABEL_RESERVE_BYTES` (2048: the summary label and the
  freshness entry, both counted twice in the envelope); `_label` now ends with the summary gap
  whenever labels are cut, never dropping them silently.
- Found while implementing: with money visible the answer has units (a supplier
  confirmation's 6 entries, a freight quote's 8) that fit no page beside the entries and
  gaps every page repeats, so the result was `result_too_large`. A unit that fits no page
  even as `_fit` trims it is now split across consecutive pages in its order (the computed
  entry first); the cursor names an entry position, so no entry is lost or repeated. Tests:
  tests/test_investigate_landed_cost.py (1 and 10 lines, padding swept to the limit, every
  page within the limit less the reserve) and the live-spec test above (fails with no
  reserve).

## Repair: a linked confirmation whose amount is unread
- Finding: only confirmations with a parsed amount counted as versions, so a purchase order
  with a proforma at 2,500 and a revised one whose total line was not read took 2,500 as
  goods, and the oracle graded it `agree`.
- Decision: an unread amount blocks goods. Goods is a value only when every linked
  confirmation has a readable amount, they agree, and their unit prices agree too.
- Code chosen: a sibling code, `PI_AMOUNT_UNREAD` (gap reason `pi_amount_unread`, subject
  the goods, who can answer: PROC), not `PI_VERSION_UNRESOLVED`. Why: an unread amount is
  not known to differ. `PI_VERSION_UNRESOLVED` asserts versions that disagree and lists
  their values; this case has no second value to list. A person is asked to read the named
  confirmation's amount, not to choose a version. When amounts or unit prices also differ,
  both are reported: the `PI_VERSION_UNRESOLVED` conflict lists every differing figure, and
  the `PI_AMOUNT_UNREAD` gap names the unread confirmation. The line's goods step names
  `PI_VERSION_UNRESOLVED` first, because that needs a choice whatever the unread amount says.
- Oracle: it groups confirmation figures by object. A goods value beside a confirmation
  with no readable amount is `goods_picked_beside_an_unread_amount`; differing unit prices
  count as versions (`goods_picked_a_version`). Unknown goods needs the
  `pi_amount_unread` gap (`unread_amount_without_gap`).
- Tests: tests/test_investigate_landed_cost.py
  (`test_a_confirmation_whose_amount_is_unread_blocks_the_goods`) and
  verify/tests/test_live_verify_landed_cost.py (`LandedCostOracleTests`).
- Quantities (repair on 7075bd6d): versions also differ when their cited quantities differ
  (unit price x quantity), even with equal amounts and unit prices: PI_VERSION_UNRESOLVED
  lists the quantities, and the oracle counts differing quantities as versions
  (`goods_picked_a_version`). Tests:
  `test_confirmations_differing_in_quantity_are_versions_and_agreeing_ones_are_one` and
  `test_one_amount_beside_a_second_confirmation_needs_every_figure_to_agree`.
