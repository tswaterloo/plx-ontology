# Case runtime — design

`integrations/source_mirror.py` and `operating/cases.py` point at "section 1" of
this file. The code and its tests
are the contract; if this document disagrees with them, fix the document.

Architecture fit (decision 2026-09-30): the investigating agent is
**Claude itself** (any MCP client) driving a small case-centric tool family. The
Workbench owns the deterministic parts: source reads, entitlements, citations,
Decimal/unit arithmetic, and the durable record. Models propose. They never
publish facts, permissions or effects by self-consent. A fact enters a case only
with a citation the Workbench can resolve for the reader, and a proposal is only
a record.

Evidence labels: all tests of this runtime are `injected-transport` (the real
`WorkbenchReadApiClient` over an in-process fake). No `live-read` of the mirror
or case tools has been done yet (section 6).

## 1. Source mirror (`integrations/source_mirror.py`)

A disposable SQLite projection of approved Portal Workbench reporting-API
relations. It is built on the existing `WorkbenchReadApiClient` and adds no new
HTTP code.

- **Operations**: `catalog(actor)`, `rows(actor, relation, columns, where, order,
  limit, max_staleness)`, `resolve(actor, literal)`, `resolve_citation(handle,
  actor)`, `refresh(actor, relation)`. `where` supports eq/prefix/in/gte/lte/
  is_null, `limit` is at most 500 and `offset` (0..1,000,000) pages
  past it (`next_offset` is null on the last page).
- **Audience (default deny)**: the `plxwb_` key is one shared login with no row
  scoping, so the mirror enforces a relation-family audience itself
  (`plx-source-mirror-audience/v1`; example in
  `examples/source_mirror_audience.example.json`). Patterns are an exact
  `schema.relation` or a `prefix*`, and the most specific pattern wins. An empty
  list denies, and `*` principals are refused. Tenant and classification vs.
  clearance are checked too. The check runs before any fill, refresh, read,
  ranking or count, and hidden relations are not even counted. A file-backed
  map is re-read on every operation and fails closed on any read or parse
  error, so revocation hides rows and citations immediately. The map is
  configuration, not authority.
- **Fill and refresh**: each relation is filled lazily on first use with all
  catalog-approved columns, or an operator projection that always keeps the
  key and `since` column. When a relation is older than `max_staleness`
  (default 900 s), it is refreshed incrementally through the catalog `since`
  column (`timestamptz`, `timestamp-utc`, `yyyymmdd-text`). The watermark comes
  from the rows, capped at the replica's reported time, never from the local
  clock. Each read re-reads an overlap window. Relations without `since`, or
  with non-unique view keys, refresh by full traversal. If a refresh fails, the
  stored rows are returned marked `stale` with the error. Only a relation that
  was never filled fails outright.
- **Honesty flags**: every capture and relation state records
  `atomic_upstream_snapshot=false` and `deletions_proven=false`, enforced by
  SQL CHECK constraints. Pages are separate replica reads. A row missing from a
  full traversal is hidden from rows/resolve but its citations still resolve,
  and its absence is not proof of deletion. Every answer carries freshness and
  caveats.
- **Citations**: `api://<schema.relation>/<url-escaped key JSON>@<generation>-<capture-id>`
  names the durable capture that first observed that exact row content.
  Superseded contents are retained, so an old citation still resolves to what
  it cited and reports `changed_since`, `superseded_by_capture` and
  `current_citation`.
- **Lifecycle (generations)**: capture ids are SQLite AUTOINCREMENT values and
  mean something only inside one mirror file. Each file gets a random 64-bit
  `generation` (in `mirror_meta`) when it is created, and every handle carries
  it. The mirror is disposable, but **rebuilding it starts a new generation**:
  handles minted by the old file resolve as `capture_unknown` with reason
  `mirror_generation_mismatch`, never to whatever new content reused the
  capture id. Cases keep their recorded digests and values; readers see those
  findings and calculations withheld as unverifiable until they are re-cited
  from the new mirror. Legacy handles without a generation (`...@<capture-id>`)
  still parse; they resolve only in a file that already held that capture
  before generations were introduced (`legacy_capture_max`).
- **Binding**: the first fill pins the tenant and a SHA-256 of the upstream
  base URL in `mirror_meta`. Reopening the file for another tenant, or with a
  client for another upstream, refuses catalog/rows/resolve/refresh/citation
  with `binding_mismatch` (the URL itself is never stored or shown). An
  offline mirror (no client) checks only the tenant. The tenant compared is
  that of the audience in force on each call (an edited audience file's
  tenant, not the host's startup value), and a client without a non-empty
  `base_url` is refused at construction. Files filled before this pin existed
  are pinned when first reopened (upstream digest once a client is given;
  the host tenant while the audience file is unusable). Each operation (and any
  capture it triggers) reads
  the audience once, so its allow and binding checks judge the same tenant,
  and a binding failure during a refresh is raised, never served as stale rows.
- **Resolve**: returns exact, trimmed, case-insensitive identifier matches
  (including JSON keys such as `app_sales_order.custom_fields.erpCode`) with
  `kind` and `identifier_strength`. It never selects a winner
  (`selection_required: true`). `app_sales_order.po_reference` is flagged
  because it can hold person names.
- **Decimals**: `parse_decimal()` keeps the raw and the exact value. It flags
  binary64 artifacts, rounds them to 15 significant digits, and says so.

## 2. Case record (`operating/cases.py`)

`CaseStore` is an append-only, hash-chained SQLite event log.

- **Events**: `case.opened`, `finding.recorded`, `finding.superseded`,
  `calculation.recorded`, `gap.recorded`, `proposal.recorded`,
  `verification.recorded`, `case.closed`.
  Each event has an actor label, a time and a dense per-case sequence. Each is
  written in one `BEGIN IMMEDIATE` transaction, so a crashed or concurrent
  writer leaves the whole event or nothing. Triggers refuse UPDATE/DELETE. The
  current view is a pure fold, and a broken chain is reported and blocks
  writes.
- **Audience**: tenant, clearance ≥ classification, and principal
  intersection. The case audience is the opener's principals ∩ the requested
  set, never `*`. An unknown case and an invisible case raise the same
  `case_not_found`.
- **Findings**: `fact` (label `cited_fact`) needs at least one citation.
  `interpretation` (label `model_output_interpretation`) may cite. Status is
  supported / contradicted / unknown. Every citation is resolved through the
  injected `citation_resolver(handle, actor)` at write time, and its digest is
  recorded. Findings are never edited: `supersede` appends a replacement link.
- **Read-time re-resolution**: every `get` re-resolves each citation for the
  *reading* actor. A finding, calculation, gap or proposal with any citation
  that no longer resolves has its content withheld (`withheld_citation_unavailable`)
  without saying why: no statement/text/payload/preconditions, and its
  citations are shown only as `{index, status}` (no handle, so a withheld item
  does not name the rows it concerns). A resolver bug also withholds; it never
  leaks. Because the case audience needs only one overlapping principal, this
  per-reader gate — not the audience — is what keeps a reader from content
  whose source relation they cannot see, and a revoked grant applies on the
  next read.
- **Read-time digest check**: a citation that still resolves but whose content
  digest differs from the one recorded at write time is reported as
  `citation_content_changed` (with the recorded and current digests). Such a
  finding moves to the `citation_content_changed` bucket with `status: null`
  and `recorded_status`; it is never listed as supported. A calculation keeps
  its recorded result and input values (history is never recomputed) and
  reports `inputs_status` plus each cited input's status.
- **Compute**: Decimal only. An ast-whitelisted grammar (+ − × ÷, parentheses,
  literals, names; no eval) runs with symbolic unit algebra (`USD/case`, `EA`,
  `m^2`, `1`). Inputs are cited fields (`row.qtytotal`) or literals. Float
  literals are refused. A cited JSON number with a fraction (decoded as a
  float) is read from its shortest round-trip text; with a fraction and more
  than 15 significant digits it is a binary artifact and is rounded half-even
  to 15 (the same rule as `parse_decimal`). The raw text, rule and
  `changed_by_normalization` are recorded as the input's `source_number`.
  Unused inputs are refused, the declared unit must match, and inexact
  results are flagged.
- **Source unit contracts** (`operating/source_units.py`,
  `operating/source_unit_contracts.json`, `plx-source-unit-contracts/v1`): a
  contract pins the unit of one `relation.column` from recorded evidence (for
  example `silver.formula_items.percentage` is a 0–1 fraction, unit `1`; see
  Portal TASK-2117, fractions shown as percentages). For an `api://` input read
  from `row.<column>`, `rows.<n>.<column>` or `key.<column>` of a contracted column, a declared
  unit that differs is refused (`unit_contradicts_source_contract`) and an
  omitted unit takes the contract's. Every input records `unit_basis`
  (`source_contract` with `unit_contract` and `unit_contract_digest`, the
  SHA-256 of the contract as applied, or `caller_declared`), echoed in the
  compute response as `input_units`. The relation is taken from the resolved
  view, not the handle text; a row index has one spelling (`rows.1`, never
  `rows.01` or non-ASCII digits), so the checked path is the path read. A contract fixes the scale only; it never says the
  values are present, current or correct. Converting between scales stays
  explicit (`share * hundred` with `hundred = 100 percent`). Calculations
  recorded before this carry no `unit_basis`.
- **Quantity roles**: planned, actual, manufactured, filled, accepted, ordered and
  damaged quantities share a dimension (`EA`) but are different quantities. An
  input may declare `role`; a source contract may pin a column's role
  (`role_basis` `source_contract` with `role_contract`, else `caller_declared`;
  builtin: AO item `quantity` planned / `actual_amount` actual, link `qtymfg_c`
  manufactured / `qtyplan_c` planned, order line `qty` ordered, damage
  `qty_damaged` damaged). Two different roles are never added, subtracted,
  multiplied or divided unless `relations` declares `{left, op, right, as}` for
  that pair (`as` is the result role; + and * also match swapped operands); a
  role beside an unroled quantity in a sum or difference is refused. Only a
  number literal or a role-less literal of unit `1` is a neutral scale; a
  role-less literal with a unit (`1502 EA`) is an unroled quantity. An unroled
  factor (a price, `pc/EA`) keeps the other side's role unless the result is
  dimensionless (`planned / x` in `1` has no role). Signs are tracked: `-p + a`
  is `a - p` and needs `(a, -, p)`, never `(p, +, a)`. Unused
  relations are refused. The calculation records `relations` and `result_role`.
  `quantity_role` is only the **combination guard and label**: the result's
  **quantity type** (`result_type`) comes from the closed type algebra of
  `operating/quantity_types.py` over the expression tree (section 6b), and every
  relation's `as` must fit the form of the subtree it labels, or compute refuses
  it (`quantity_label_inconsistent`). Calculations without roles are recorded
  exactly as before.
- **Gaps**: `absent_in_source`, `inaccessible`, `stale`, `not_ingested` or
  `unknown`, each with "what is missing", "why" and "who can answer", and
  optional `citations` (resolved and digested at write time, gated on read like
  a finding; `citations_status` is `uncited`, `resolved` or
  `citation_content_changed`).
- **Propose**: recorded only, with status "proposed — requires approval through
  the existing approval journal". Nothing is executed or dispatched. Optional
  `citations` gate capability, payload and preconditions on read. Optional
  `evidence` appends a finding with the same citations in the *same*
  transaction (`evidence_finding_id` on the proposal). An optional
  `idempotency_key` (stored, never projected) makes a repeat return the
  existing ids with `duplicate: true` and append nothing. An optional
  `expected_effect` (`relation`, `key`, `fields` as `column` or
  `column.jsonKey`, 1–5, `equals` non-empty text or an integer) says what one
  source row should show once the action is carried out; that row must be one
  of the proposal's citations (same relation and key, one row), recorded as
  `target_citation`, and the effect is gated with the proposal's content.
  Receipts say `expected_effect_recorded`; an idempotent repeat that declares
  a different effect than the stored one is refused
  (`idempotency_key_reused_with_other_expected_effect`), and a repeat of a
  proposal recorded before effects existed returns it without one (it cannot
  be verified; record a new proposal).
- **Verification** (section 6a): `verification.recorded` holds what a
  read-back of a proposal's expected effect found. It cites the row read back
  plus every citation of the proposal and is gated on read like a proposal. It
  is an observation, never an approval, dispatch or attribution.
- **Close**: records an outcome, after which writes are refused.

## 3. MCP tool family and citation resolution

`operating/case_runtime.py` holds `CaseRuntime`, which binds one `SourceMirror`
and one `CaseStore` under `<state-dir>/case-runtime/` (`source-mirror.sqlite`,
`cases.sqlite`). The host enables it with `--source-mirror-audience <file>` on
`python3 -m operating.mcp_server`. The file is validated at startup and its
tenant must equal `--tenant`. The upstream is the existing
`--enable-workbench-read-api` client (it feeds only the mirror and requires the
audience). Without that client the mirror is offline and answers only from
captures it already holds. The host is case-only: it lists only these tools and
never constructs `UnifiedPlatform` (the platform MCP host was retired); in-process callers use
`MCPServer(case_runtime=…, actor=…)` and `local_client`.
**`--source-mirror-audience` is required; the host does not start without it
(default deny).** All calls act as the host-configured session actor
(`MCPServer.actor`). There is no actor argument.

| Tool | Does |
|---|---|
| `plx.schema` / `plx.find` / `plx.object` | Generic ontology tools over the projection store (`--ontology-store`, else an empty one; `operating/ontology_tools.py`, built by `operating/ontology_host.py`, I/O contracts in `operating/ontology_tool_contracts.json`): what is modelled, find records by identifier/text/filters (candidates, never a winner), one record's values with per-value citations. The read policy is decided again on every call from the readers' audiences (a revoked grant applies to the next call; a failed BC company listing is read again next call, never remembered). When the store has no visible match, `plx.find` falls back to the host's own readers (find-live's `ontology/resolve.py` over the source mirror and BC, same policy; a live no-match is never `absent_in_source`). `plx.object` reads the store only: a live-found id answers `not_in_projection` to the reader who found it (`object_not_found` to anyone else); read its `matched.raw.origin.citation` with `plx.read` |
| `plx.around` / `plx.timeline` | `operating/ontology_tools_traverse.py`: declared link walks and events over the same store, policy and set handles |
| `plx.read` | Any citation as the session employee may read it now (`operating/ontology_tools_evidence.py`, served by `operating/case_tools_host.PagedRead`): `text_offset` pages a page, body or (with `field_path`) one long field, `text_window.sha256` over the whole text; `column_offset` pages a wide row (a row too wide pages from 0 on its own; `truncated_fields` names cut cells); an original file citation lists its `extractions`; a live mail read gives `cite_as`, the body-pinned citation (`#body_sha256=&shown_sha256=`) and its case digest, to cite with `plx.case_add_finding` (replaces `plx.source_citation` and `plx.case_cite_mail`) |
| `plx.identity` / `plx.documents` / `plx.aggregate` / `plx.compare` / `plx.process` | The remaining generic tools over the same store and per-call policy; `plx.process` (`operating/ontology_tools_process.py`) answers lifecycle and three-way-match questions. With `--mail-capture-dir` the host grants `mail:<mailbox>` units from the capture's audience check and re-gates each message per item; with `--sharepoint-capture-dir` it serves captured SharePoint files the same way, labelled `as_of_capture`, re-read live per item where a drive reader exists (`CaptureCaseHostTools`) |
| `plx.case_open` | Open a case (question, roots, optional narrower audience) |
| `plx.case_add_finding` | Fact/interpretation; `supersedes` + `supersede_reason` replaces. Link mode (`link: {from_citation, to_citation, contract_id}` only): the Workbench-checked link of the retired `plx.case_link` |
| `plx.case_compute` | Decimal + unit calculation over cited fields/literals |
| `plx.case_record_gap` | Record a visible unknown; optional `citations` gate it per reader. `property` names what is missing in schema terms (`<ObjectType>.<property>`, `<ObjectType>.<link>` (a link from the type by name or into it by reverse name) or the bare `<ObjectType>`; up to 5 names of one type) and is stored as a sorted list. An absence gap (`absent_in_source`) is a structured claim: `property` is required (`absence_needs_property`), and the gap guard (`capability_guards.check_gap`) re-reads every cited `api://` row as current content before recording: a property is checked on **every** field the schema binds it to on that relation (all carriers, e.g. `SalesOrder.customer_po` on `public.app_sales_order` = `po_reference` and the FM-migration key `custom_fields.custPoCode`, declared in `ontology/schema/domains/orders.json`); each field is read by the one field classification the coverage, the order drivers and the evaluator also use (`capability_guards.value_state`): a held value refuses (`gap_contradicted_by_cited_evidence`; the value is never echoed), a value equal to the row's own identifier named by the property's `placeholder_when_equal_to` (e.g. a PO field holding the order's own number, the FM-ensure fallback `po_reference ?? order_number`) is a `self_reference`, absent for the property and reported in `gap_guard.checked[].self_references`, a missing column, invalid JSON or a non-text value (float, boolean, list, object) refuses (`gap_property_unreadable`), an unreadable row refuses (`gap_evidence_unreadable`), and a gap citing no carrying row refuses (`gap_property_not_in_cited_rows`); a link absence must cite the subject row (`gap_subject_not_cited`; the related rows are the caller's completed read, not re-checked: `rechecked: false`); a bare-type absence is refused when a cited row is of that type, and any absence citing nothing is refused (`absence_needs_citation`). Any other state may name a property, which must be declared (`gap_property_undeclared`). Nothing is recorded on a refusal. Known limit: the guard reads, then the store appends (no read digest is pinned); the playbook evaluator re-runs the same check on the rows as it re-reads them, and an unstructured or contradicted absence is a violation (`absence_gap_unstructured`, `absence_gap_contradicted`) |
| `plx.case_propose` | Verify mode (`proposal_id` [+ `references`, `max_staleness`]): read back a proposal's expected effect and record `effect_observed` / `effect_not_observed` / `unknown` (section 6a; the retired `plx.case_verify_proposal`). Otherwise record a proposal; `dispatched: false`, `approved: false`; optional `citations` gate it per reader; optional `expected_effect` on one cited row, one expected value per field (`{relation, key, values: {field: value}}`, 1..5 fields; the one-field form `fields: [f], equals: v` is still accepted, a several-field `fields`+`equals` is refused `invalid_expected_effect_one_value_per_field`); optional `evidence` records the supporting cited fact in the same transaction. Fail closed: the capability name must already be canonical (dot-separated lower-case words; `operating/capability_guards.canonical`, also enforced where `CaseStore.propose` stores it) and an owner-system proposal type of the typed action catalog `operating/action_catalog.json` (the one source of capability guards: an owner-system type's guards are among its `preconditions`; decision action-catalog-format option b); every entry binds the relations it may write (`target_relations`; an effect on any other relation fails `effect_relation_not_bound` before the guards) and the hosts that offer it (`hosts`): `fixture.record.set_field` (`fixture.*` only) and `test.record.set_field` (mirror relations, for the mechanics tests) are `test`-only, refused `capability_not_available_on_this_host` unless the server is built with `MCPServer(test_capabilities=True)` over no session or a fixture session (the CLI never does; an employee session refuses the flag). The isolated owner's test-host types are exactly the action path's `FIXTURE_CAPABILITIES` (derived from the same `hosts` declaration). `portal.assembly_order.set_header_quantities` is declared with `registration.refused` (`owner_write_surface_does_not_set_these_fields`): recorded and guarded, never an intent. Guards read the cited rows cut to the capture's column list (`mirror.catalog` columns), so a column the capture did not serve is unreadable, never empty; its guards (generic checks in `operating/capability_guards.py`) must hold on the cited rows as they are now, else nothing is recorded. `plx.act` (propose) refuses on the same rules (`ActionPath.register` is the one place a registration enforces the capability guards, with the host's current-content reader, or on the cited content as cited when a caller such as goal2_exec passes none; its catalog hook refuses a type not in the catalog, `action_type_not_in_catalog`, and a refused registration, `action_type_registration_refused:<code>`) and the playbook evaluator re-checks them (a test-only capability is `proposal_capability_test_only`). `portal.sales_order.set_customer_po` declares: the effect writes only `po_reference` / `custom_fields.custPoCode` on a cited sales-order row and sets the value, the value is PO-shaped and held exactly in the cited evidence field, is not the evidence row's own number (ignoring case), the order's PO fields are readable and hold no PO-shaped value, and the value is not the order's number (ignoring case); every such proposal records the catalog's `person_confirms` precondition "a person at PLX confirms the evidence belongs to this order (evidence link: asserted_by_proposer_not_verified)" (added when missing; the evaluator counts a proposal without it as `person_precondition_missing`). `portal.assembly_order.set_header_quantities` declares: the effect writes only `qtytotal`/`finalqty` on a cited AO row whose header quantities are readable and empty; every written field is in the payload with the same value (numbers, compared as decimals: `7 == "7.0"`, `abc` refused); `payload.evidence` names per written field a cited `{citation, field}` that holds that value and is a declared source bound to this AO (planned `qtytotal`: the AO's one-per-unit component line `silver.assembly_order_items.quantity` with `qty_per_batch` 1 and `assembly_order_id` = the AO id, or its Portal MO `quantity`/`size_pieces` with `mo_code` = the AO code; produced `finalqty`: that component line's `actual_amount`), never the AO row itself or the FM link `qtymfg_c`. The signed packet is confirmed by a person (a human precondition) |
| `plx.case_get` | Without `case_id`: visible case summaries (`status`, `limit`). With it: case projection plus the mirror status of every resolved citation (findings, calculation inputs, gaps, proposals, verifications); whole case when it fits, else a bounded summary; `section` pages findings/calculations/gaps/proposals/verifications/timeline, `header` returns the full question/roots/outcome, `claims` runs the claim gate (section 6b) |
| `plx.case_close` | Close with outcome |
| `plx.case_attach_original` | Attach an inbox original (`inbox_name`), a SharePoint drive item (`drive_id` + `item_id` [+ `max_bytes`]; the retired `plx.case_attach_sharepoint_original`), or re-extract an attached one as a new version (`citation` + `reason`; bytes and earlier citations unchanged) |
| `plx.document_fields` | `mode` propose: Portal-taxonomy metadata proposals for one readable original (below); request / status: the person's confirmation (`operating/kind_confirmation.py`) |

**Case-host budget (2026-10-01).** The
host lists exactly `limits.max_case_host_tools` = 22 tools, `plx.around`/`plx.timeline`
included. A new tool is a prompt to look for one it supersedes, not a quota. Retired, with successors (`ontology_tools.HOST_RETIREMENTS`):
the raw source reads (catalog, rows, resolve, live listing) into `plx.schema`/`plx.find`/
`plx.object`; the customer-PO coverage, proposal and gap tools into `plx.find`,
`plx.case_propose` and `plx.case_record_gap`; the BC purchase case view into
`plx.find`/`plx.object`; the ingest-status tool into the freshness `_meta`
(`plx/freshness`) that every answer now carries; case listing and the claim check into
`plx.case_get`; re-extraction into `plx.case_attach_original`; the intent listing into
`plx.action_get`. Host-budget phase 1 (`mcp_server.CASE_TOOL_RETIREMENTS`) merged `plx.source_citation` and `plx.case_cite_mail`
into `plx.read`, `plx.case_link` into `plx.case_add_finding`, the SharePoint attach into
`plx.case_attach_original`, the metadata proposal and confirmation tools into
`plx.document_fields`, and `plx.case_verify_proposal` into `plx.case_propose` (the slot
`plx.process` took). `plx.source_citation` stays a host-code read for the drivers and
evaluators (`mcp_server.HOST_READS`), refused for every client. Without a store the host is not blind: `plx.find` resolves from the
mirror and BC readers (above). In-process drivers (case scripts, evaluator, live
workspace) still read rows directly through `operating/source_reads.py` (`mirror.rows`,
`mirror.catalog`, `live.list`), routed by `local_client`; an MCP client cannot reach those.
Identity is not among them:
`mirror.resolve` left the driver reads; no
case script exists and the case opener (`operating/case_workspace_live.py`) roots every case
with `plx.find` (one candidate, or one record held in several systems joined by verified
same-record links; anything else is refused as ambiguous). The one
remaining caller is `operating/case_context_tools.py` (`mirror.resolve` for the shared
context tools). Sections below that name the
retired tools describe the code before that retirement.

**Shared-context tools** (`operating/case_context_tools.py`; both read-only, rate-limited
like every tool, inputs refused on any unknown field, the actor passed through
`CaseRuntime.employee_gate` first, so an `employee:` actor without a resolver, or mixed
with an `agent:`/`sp_` principal, is refused):

- Ingest status (`case_context_tools.ingest_status {kind?, source_id?}`; `plx.ingest_status` is retired as a tool, its counts-only freshness rides on `_meta` of every `tools/call` result) needs `--ingest-sources <plx-ingest-sources/v1>`
  (the sources file of the `operating.ingestion` runs writing under the same
  `--state-dir`); without it `_meta` reads `configured: false`. The ingest store is opened
  read-only (sqlite `mode=ro`, `busy_timeout` 2 s): the store file is never created or
  modified (on a WAL store SQLite may create the `-wal`/`-shm` sidecars; `immutable=1`
  would avoid them but could read a torn state during a run), and the run lock is never probed (a probe
  would race a starting run), so a unit or run still marked running reads
  `running_or_interrupted`; a missing store, or a file without the ingest tables, is
  `runs.store: not_initialised` with every
  configured source `never_run`. Output `plx-case-ingest-status/v1`: `status_schema`
  (`plx-ingest-status/v1`), `runs` (store, total, interrupted, running_or_interrupted,
  last_run_id, last_run_state), `units` (the `plx-ingest-status/v1` unit shape — unit_id,
  source_id, kind, completeness_basis, state, last_run_id, reasons, counts, lag,
  cursor_advanced, items_current, items_absent, last_complete_age_seconds — plus
  `visibility_basis` and `counts_visible`), `summary` (over shown units), `visibility`
  (`units_visible`, `units_withheld` counted over every unit before the kind/source_id
  filters, so a filter is no existence oracle, rule), `counts_scope`,
  `completeness_basis`, `completeness_rule`, `caveats`, `business_system_writes: 0`.
  A unit is shown only when the actor may read its configured scope over every key the
  reader checks: the mirror audience (Portal relation); for mail, `mail:<identity>` for
  every directory identity of the mailbox (object id, UPN, mail, proxies; most
  restrictive wins, as `GraphMailReader._open`), taken from the owner ACL the reader
  recorded on the unit's stored messages, plus that owner ACL (an employee must own the
  mailbox) — no recorded identity set (e.g. never run) is withheld; the folder keys
  `sharepoint:<drive>:<path>` and `<path>/`; `bc:<env>:companies` or
  `bc:<env>:<company>:<entity set>` (an all-companies unit's company is recovered from its
  stored items, hash-checked against the unit id). Undecidable (source no longer
  configured, no live audience or BC reader, unconfigured mailbox/drive, company unknown)
  is withheld and only counted. A SharePoint unit is shown with `counts_visible: false`
  (`counts: {}`, item counts `null`, `lag: {}`, `cursor_advanced: null`, `withheld:<n>`
  collapsed to `withheld`) when items carry their own ACL for this actor (folder keys
  under `source_acl_required`, a `source_acl_required` pattern under the folder, or
  any folder for an employee) or when any audience or
  classification pattern at or under the folder (or a wildcard above it) does not grant
  the actor within its clearance, e.g. `/AOs*` granted but `/AOs/2026 - AOs/Rejected*`
  not (the `/AGENTS` vs `/AGENTS/Contracts` shape of
  `examples/ingestion_live_audience.live-check.json`). Counts are what the ingest
  principal read.
  Known limit: a mail unit is decided on the identity set recorded with its newest stored
  message, not the live directory. After a mailbox rename or an added proxy address, and
  until the next successful ingest run, an audience that restricts only the new identity
  still shows the unit (one-run staleness).
- `plx.document_fields {mode: propose, citation, model_suggestions?, max_staleness?}`:
  `citation` is an `original://<id>` or `original://<id>#extraction=<k>` file citation
  (page citations and other schemes refused). `OriginalStore.text_document` runs the
  same audience, live source gate and byte re-hash as every read (a hidden original is
  refused exactly like a missing one), then `operating.doc_metadata.build_proposals`
  runs over that version's whole text, title = filename, OCR pages make every span
  `reading: ocr` / `requires_review`. Output is the `plx.portal-document-metadata-proposal/v1`
  shape plus `view_schema: plx-case-document-metadata/v1`, `extraction_version`,
  `original_sha256`, `extraction_text_sha256`, `span_offsets`, `applied: false`,
  `saved: false`; every evidence span with a page carries `page_citation`
  (`original://<id>#page=<n>` or `#extraction=<k>&page=<n>`). Linked records: a printed
  AO / sales order / Petra PO / lot number is looked up with `plx.find` for the actor on
  the case host's one discovery path (`CaseHostTools`: identifier kind `ao_code` /
  `so_number` / `petra_po` / `lot_number` with its alias kinds, object type where the link
  names one; the read policy filters before counting or ranking; `max_staleness` bounds
  the mirror pages read); `records` are its ranked candidates (object_id, object_type,
  rank, tier, reason, identifier_kind, binding, property, citation), `record_status`
  `one_candidate` / `several_candidates` / `no_visible_candidate` (plx.find `found` /
  `ambiguous` / `not_found`) or `lookup_failed` (`record_lookup: plx_find_<code>`),
  `selection_required: true`, `records_truncated`, and `lookup_gaps` (plx.find's typed
  gaps counted by `state:reason`). Bulk lot, customer and vendor have
  no identifier kind (`record_lookup: no_identifier_kind_for_this_link`), so
  `ItemRef`/`PartyRef` rules that need a linked record stay `unknown`. Current SharePoint
  columns are `not_read` (the case host's reader does not read listItem fields).
  `model_suggestions` (column, value, model, span, quote; optional basis) are validated
  for shape here and for content by `doc_metadata` (rejected ones listed with reasons);
  they never override a rule. Nothing is saved, applied or written.

Citation resolution: the case store's resolver is
`CaseRuntime.resolve_citation`. It accepts only `api://` handles and calls
`SourceMirror.resolve_citation(handle, actor)` for the calling actor, so the
audience check comes first. It returns only the immutable cited content
(relation, key, capture id, row), so the recorded digest fingerprints what was
cited, not refresh metadata. Any other scheme, a non-visible relation, an
unknown key or capture, or a malformed handle raises `CitationUnresolvable`:
the write is refused and the read is withheld. The MCP layer also rejects
non-`api://` citations up front with `citation_scheme_not_supported`.
Superseding is two appended events; if the second fails, the tool reports
`supersede_error` and does not pretend it was atomic.

Result bounds: `MCPServer.handle` serializes every result twice (JSON text and
`structuredContent`) and refuses anything over `MAX_RESULT_BYTES` (48,000).
`plx.case_get` and `plx.schema` measure that exact doubled envelope and
fill each page greedily up to it; `next_offset` is null on the last page and
`complete` is true only when one response holds everything (for the catalog,
also only when the upstream catalog was read without error). An item too large
to fit alone is returned without its mirror status (or freshness) and, if still
too large, as an explicit `oversized` marker with its id/seq, so paging always
advances and every item is accounted for.

Agent definitions (AGT-06, `operating/agent_definitions.py`): every MCP session runs
exactly one definition from `operating/agent_definitions.json` (format
`operating/agent_definitions.schema.json`; unknown fields fail closed):
`plx.agent.investigate` (reads, case records, plans; no `plx.act`) and `plx.agent.act`
(the same plus `plx.act`; the default, which is what the host served before definitions
existed). The host config's `agent` key (or `mcp_server --agent`) picks it. Only its
tools are listed; a served tool outside them and a catalog action type outside its
`action_types` are refused before the call (`tool_not_allowed_for_agent:<id>`,
`action_type_not_allowed_for_agent:<id>`); the run stops at `max_tool_calls` client
calls or `max_consecutive_errors` (`agent_stopped:<reason>` from then on) and parks the
stop for a human; a `plx.act` registration needing approval is recorded as an escalation
parked for Mission Control (`approval:mc_gate` -> `mc_approval`), never performed.
`data_scopes` must be every scope (`api`, `original`, `graph-mail`, `sharepoint`, `bc`):
a narrower definition is refused at load (and as a constructed `AgentDefinition`),
because narrowing is not enforced on every read path (the ontology projection behind
`plx.find`/`plx.read`/ `plx.around`/`plx.documents`, case records, the mail/SharePoint
captures and `source_reads` serve rows of every scheme). Every answer's
`_meta["plx/agent"]` names the agent id, version, digest and limits with the run's
counts and state; refusals carry no meta.

## 4. Customer-PO coverage (`operating/customer_po_coverage.py`)

Vince's pain point: Portal sales orders often lack the customer's PO. Live read
of `public.app_sales_order` (2026-09-30, 933 rows; the reporting API serves the
replica of the Portal *staging primary*, filled by FM/Sage/gold ETL and bridges,
not the UAT DB the staging UI writes): Sage imports carry it in `po_reference`
(64/64 non-empty, 42 PO-like; 21 of them words or names); FM migrations
(`custom_fields.fmId`) in the legacy `custom_fields.custPoCode` (345/380
non-empty, 177 PO-like); Portal-entered orders in no exposed field (489/489).
The Sage 64 is "non-empty", not "PO-like": the PO-shape rule has not changed
since the tool was written.
Tools (same default deny and session actor as section 3):

| Tool | Does |
|---|---|
| `plx.find` | Classifies every audience-visible sales order; filters `origin`, `status`, `date_column` + `date_from`/`date_to`; pages by `offset`/`next_offset` within the envelope |
| `plx.case_propose` | Records a cited fact finding plus a proposal to set one order's customer PO, in one transaction, both gated by both citations; refused without support; idempotent |
| `plx.case_record_gap` | Records `absent_in_source` / `unknown` gaps per order (each citing its order) and one `not_ingested` gap (citing every order it names) |

- **Read path**: `SourceMirror.rows` for the calling actor, so the audience
  check runs first (no grant: `source_mirror_not_visible`, no fill, no count).
  All matching rows are scanned in 500-row pages through `rows(offset=)` (at
  most 20,000); only the first page may refresh. `columns_read` names exactly
  the columns used; a date column must be mirrored (only `updated_at` is
  confirmed in fixtures; the live relation's 23 column names are not recorded
  in evidence, so no created/order-date column is assumed).
- **Origin rule** (reported per order with `lineage` and the rule; mirrors
  Portal staging@6c9415973 as quoted in `docs/reference/portal-code-map.md` §3.2–3.4
  and §9, no Portal checkout in the cloud container): Sage-backed as in the JS
  predicate `lib/wms/sage-ship-queue.ts:isSageBackedSalesOrder` (not Portal's SQL
  helpers, which differ): a non-empty trimmed `sageOrdUniq`/`sageSourceOrdUniq`
  first, then trimmed `source == "sage.oe_order_header"` → `sage_import`
  (lineage `sage_etl`; wins over `fmId`, as `isFmOnlySalesOrder`, and over any
  other `source` value);
  `source == "fm-oe-ensure"` + `fmId` → `fm_migrated` (`fm_oe_ensure`); `fmId`
  without `source` → `fm_migrated` (`legacy_fm_sql` when `custPoCode` is present,
  else `fm_linked_without_source`); `source == "gold-so-bridge"` →
  `portal_entered` (`gold_so_bridge`); no marker → `portal_entered` (`portal`);
  another `source` value, an empty `fmId` without a Sage marker, `fm-oe-ensure`
  without `fmId`, unparseable or non-object `custom_fields`, or `custom_fields`
  not mirrored → `unknown`. Each present PO field carries `possible_writers`,
  inferred from the lineage (never audited last-writer provenance: the replica
  has no per-field history) (`custPoCode` = legacy `migrate-fm-to-portal.sql` only; Sage `po_reference` =
  `COALESCE(ponumber, po_reference)`; FM-ensure = FM `custpocode`; legacy SQL put
  FM `reference` text in `po_reference`). Portal treats `po_reference` as an
  order identity (`resolveOrderByNumber`), not free text.
- **Equals the order number (possible fallback)**: a PO field equal to the
  order's own `order_number`, `custom_fields.fmOeNumber` or `renumberedTo` is
  `present_equals_order_number` (`matches` names which). Equality is not proof:
  the bridge computes FM `custpocode = po_reference ?? order_number` for
  Portal-created FM rows (id ≥ 900000, flagged `fm_order_entry_created_by_portal`)
  and FM-ensure copies it into an empty `po_reference`, but someone may also
  have typed the order number. Conservatively not a PO (never
  `has_customer_po`) and not a not-PO-like miss; an order with only such values
  is `absent_in_this_source` and its gap says so (possible fallback).
  `plx.case_propose` refuses a value equal to the order number
  (`customer_po_equals_order_number`) and evidence whose value equals its own
  row's number (`order_number`, `erpcode`, `ordnumber`,
  `fmOeNumber`, `renumberedTo`; e.g. FM `custpocode == erpcode`:
  `evidence_equals_order_number`).
- **PO-shape rule** (conservative; a false "needs review" is recoverable, a
  false "has a PO" hides a missing one): after trimming, 3–40 characters of
  `A-Z a-z 0-9 - _ / . #`, letter/digit at both ends, no whitespace, at least
  one digit, not only zeros. Other text is `present_not_po_like` and **never
  echoed** (it can be a person's name); `PO 4500` is therefore not PO-like.
  Each such field carries `reasons` (`not_text`, `too_short`, `too_long`,
  `whitespace`, `no_digit`, `only_zeros`, `disallowed_chars`,
  `edge_not_alphanumeric`) and `probable_po_after_normalization` (PO-like once
  a leading PO marker — `PO`, `P.O.`, `P/O`, `PO#`, `PO No:`, `Purchase Order`,
  optionally `Cust`/`Customer`/`Client`-qualified, or a bare `#` — and the
  whitespace between the digit groups after it are removed: `PO 4500`,
  `P.O. 4500`, `P.O. #4500`, `PO# 12345`, `Cust PO 4500`; a wrapping
  bracket/quote pair and trailing `.,;:` are dropped only next to a PO word
  marker: `(PO 4500)`, `PO 4500.`). Never a lead: unmarked values with
  punctuation (`4500.`, `1920.`, `2026.`, `4500,`, `(4500.00)`,
  `(2026-01-05)`, `(#204)`); names;
  unmarked digit groups (`4500 1234`, `905 555 1234`, `2026 01 05` — not
  separable from phones and dates without a marker); other words before a
  number (`Order 4500`, `per email 45001`, `Invoice 12345`, `Suite # 204`,
  `Customer # 4500`); a non-digit word after the number (`PO 1920 pcs`,
  `PO Box 123`). Joining stops at a second marker (`PO4500 PO4501` is two POs)
  and bare `PO` is a marker only when no letter follows (`Polo2 123`, `POA1 2`
  are not). A value over 80 characters is never flagged (known limitation: a PO
  buried in a long note is not a lead). Known leftover: `PO 2026` is flagged.
  A probable PO is
  a review lead on the order, never `has_customer_po`; its reason codes and the
  lead are written into the `unknown` gap text from
  `plx.case_record_gap` (value still withheld). Aggregates count
  reasons, probable POs, `by_lineage_status`, Portal-created FM rows and
  value-free shape classes of not-PO-like fields (at most
  `SHAPE_BUCKET_CAP` = 24 buckets per dimension in the tool output, the rest
  folded into `other`; the CLI is uncapped)
  (`by_origin_field_not_po_like_shape`: reasons, marker leading/inner/none,
  tokens, digit/alpha tokens, digit groups, max digit run, numeric pattern such
  as `3+3+4`, length bucket, probable) — counts only.
- **Live diagnostic (live checks, credentialed env)**: `python3 -m
  operating.customer_po_coverage --state-dir <tmp> --audience
  examples/source_mirror_audience.example.json --principal staff:operations
  --principal staff:finance` prints counts only (no value, key, citation or
  order number): the aggregates above plus combined shape signatures, and
  cross-checks of FM `public.order_entry.custpocode` (by `fmId`: empty,
  equals the order number (possible fallback), PO-like same/different/where
  Portal has none, not
  PO-like) and Sage `sage.oe_order_header.ponumber` (by `sageOrdUniq`, else
  `sageOrdNumber`: equals `po_reference`, empty, differs). A relation the
  audience refuses is reported as `mirror_refused`, not raised.
- **Per order**: `citation` (every claim derives only from that row), origin +
  rule, each candidate field (`po_reference`, `custom_fields.custPoCode`) as
  `present_po_like` (value shown) / `present_not_po_like` / `empty` /
  `field_absent` / `not_inspectable` (with `reason`: `column_not_mirrored`,
  `json_unparseable`, `json_not_object`), and `has_customer_po` /
  `po_fields_not_inspectable` / `po_field_not_po_like` /
  `absent_in_this_source`. A field that was never read is never `field_absent`,
  and an order with such a field and no PO-like value is never
  `absent_in_this_source`: its gap is `unknown`, and a proposal for it is
  refused (`order_po_fields_not_inspectable`). Orders without a PO list `possible_sources`: Graph
  mail + attachments, SharePoint, BC `not_ingested`; Portal attachments/other
  tables `unknown`; `sage.oe_order_header.ponumber` / `public.order_entry.custpocode`
  `not_checked` (exposed, not read by this tool). Absence here never means the
  PO does not exist.
- **Date filter**: bounds compare calendar dates or instants, never strings. A
  `YYYY-MM-DD` bound compares with each value's own calendar date (`date_to`
  includes that whole day); a timestamp bound compares instants (naive
  timestamps read as UTC; a date-only value against a timestamp bound counts
  as its whole UTC day). Impossible dates and `date_from` after `date_to` are
  refused (`invalid_date_bound`, `reversed_date_bounds`). Offsets may be
  `Z`, `+HH:MM`, `+HHMM` or Postgres hour-only `+HH`/`-HH`
  (`2026-01-05 10:11:12.123+00`); anything else is unparseable. Rows with no value
  are excluded; values that are not ISO dates are counted
  (`date_values_unparseable`) and make the result incomplete.
- **Completeness**: `completeness.complete` (and `aggregates.complete`) is true
  only when every matching mirrored row was classified from one capture that
  is within `max_staleness` and whose last refresh succeeded; reasons name
  `scan_bound_reached`, `mirror_changed_during_scan`, `mirror_capture_stale`,
  `mirror_refresh_failed_serving_stored_capture`, `date_values_unparseable`. The page's `complete` is also
  false whenever the orders are paged. Aggregates count origin × status (and
  per-field statuses) over all scanned rows, before origin/status filters; with
  a date filter they count only in-range rows (`aggregates.rows` =
  `rows_in_date_range`). With a date filter the scan asks the mirror for
  non-null date rows, so `rows_matched_in_mirror` means rows with a non-null
  date column, not all rows of the relation. In `SourceMirror.rows`,
  `truncated: true` (`total_matched > returned`) means only "this response is not
  all the matches", not that rows
  were lost; the scan pages with `next_offset`, not `truncated`.
- **Proposal preconditions (enforced)**: `customer_po` is PO-shaped; the order
  citation resolves for the actor to the *current* content of a
  `public.app_sales_order` row with no PO-like value; the evidence citation
  resolves to current content (same mirror generation, not superseded, not
  absent from the latest full traversal) whose `evidence_field` (column or
  `column.jsonKey`) holds exactly that value. "Current" is judged against a
  mirror no older than `max_staleness` (default: the mirror's 900 s): an older
  relation is refreshed first, and if that refresh fails the citation is
  refused (`<role>_citation_capture_stale`) instead of trusting the old
  capture. The same check applies to the gap tool. Duplicate keys on an order
  (a non-unique key run): the citation names the run, so a lone-row citation is
  refused as `order_citation_ambiguous`; the order citation must match exactly
  (a re-captured order has a new citation, so repeating the proposal against it
  records a new proposal), and a duplicate (`duplicate: true`) needs identical,
  non-empty citation sets, never just the same key. The evidence becomes a cited
  fact finding (digests re-checked on every read); the proposal payload records
  `evidence_link_to_order` (`same_row` or `asserted_by_proposer_not_verified`),
  `dispatched: false`, `approved: false`. No Workbench capability writes a
  customer PO anywhere.
- **Permission on read**: the finding and the proposal each carry *both*
  citations (Sage/FM evidence row and Portal order row), and each gap carries
  the order citations it names, so a case reader sees the PO value, order
  number, key or `api://` handle only while they can resolve every citation of
  that item. With `public.*` → operations and `sage.*` → finance, an
  operations-only or finance-only member of a both-principal case sees the
  finding and proposal withheld; operations still sees the order-only gaps
  (it can read that row), finance does not. Revoking a grant hides the content
  on the next read. Tested in `CaseReaderPermissionTests`.
- **Atomic and idempotent**: finding + proposal are one transaction. Repeating
  the same order (relation + key), value, evidence citation and field returns
  the existing ids (`duplicate: true`); the dedupe key is a digest that is never
  shown to readers.
- Evidence label: `injected-transport` (`operating/tests/test_customer_po_coverage.py`).

## 5. Operator-supplied originals (`operating/case_originals.py`)

Ricardo's AO scans (and any other paper original) are not in a source the
Workbench reads. An operator places the PDF in
`<state>/case-runtime/originals-inbox/<principal>/` on the Workbench host (the
directory names the one principal who may read it, e.g. `staff:quality`); the
client then calls `plx.case_attach_original(case_id, inbox_name,
inbox_principal?)`.

- **Read**: only a regular file directly inside a principal directory (no
  path, link, linked directory or URL), at most 20 MB, PDF only (`%PDF-`). Only
  directories of principals the session holds are looked at, so a session
  without the principal can neither attach the file nor learn that its name
  exists (`inbox_file_not_found`; naming the directory gives
  `inbox_principal_not_held`). The file is claimed by an atomic rename, read,
  copied to `originals/<sha256>`, recorded (immutable row) and then removed
  from the inbox; a refused or failed attach puts it back (unless a file of
  the same name was placed meanwhile). A crash between claim and store can
  leave it as a hidden `.claim-<hex>` file in the same directory: rename it
  back to attach it again. Principal directory names must match
  `[A-Za-z0-9][A-Za-z0-9:._@-]*`; a principal outside that set has no inbox
  (`inbox_file_not_found`). On a case-insensitive filesystem (macOS default)
  principals differing only by case share a directory.
- **Audience**: exactly the directory's principal, as the operator declared by
  placing the file there; classification = the actor's clearance. Same bytes +
  same audience → same `original_id` (`duplicate`).
- **Text**: `extract_document(local_pdf_ocr=...)`; OCR (macOS Vision, else
  poppler + tesseract) runs only when the host passes `--enable-local-pdf-ocr`,
  otherwise only a PDF text layer is read. OCRed pages are listed in `ocr_pages` and their page
  text resolves with `text_source: "ocr"`: machine-read, for a person to
  confirm, never a verified value; `plx.case_get` shows `text_source` and a
  machine-read note in each such citation's status. A failed extraction still stores the
  original and records why (`extraction.status: failed`).
- **Citations**: `original://<id>` (file identity + extraction summary) and
  `original://<id>#page=<n>` (page text). They resolve through the same case
  resolver as `api://` rows, for the reading actor: outside the audience, tenant
  or clearance → withheld; every resolve re-hashes the stored bytes, so a
  changed or missing file withdraws every finding citing it. A client reads
  them with `plx.read`: the file citation lists its extraction versions;
  a page citation returns the page text (OCR text labelled machine-read), one
  bounded slice per call, continued from `next_text_offset` until null, with
  `page_text_sha256` over the whole page. Outside the audience, a real
  original and a nonexistent id read the same (`original_not_visible`).
- **Re-extraction** (`plx.case_attach_original(case_id, citation, reason)`):
  after OCR is enabled or repaired on the host, a reader of the original reads
  its stored bytes again (re-hashed first) as a new immutable extraction
  version with who, when, why and the OCR setting, cited as
  `original://<id>#extraction=<k>` and `original://<id>#extraction=<k>&page=<n>`.
  The bytes, id, audience and earlier versions never change: `#page=<n>` is
  always the attach-time version 1, so earlier findings keep resolving to the
  text they cited. An identical result is `duplicate` (nothing recorded);
  otherwise a cited fact finding compares the new version with the previous
  one. At most 20 versions per original. The file read of
  `plx.read` lists every version compactly (`extractions`); when
  its page citations do not fit one response it gives `page_citation_pattern`.
  Page reads carry only their text. A version already held but not yet cited
  on this case (another case made it, or the finding write failed) is recorded
  on this case; a result identical to any held version is never a new one.
- **Bytes** (`OriginalStore.read_pdf`, console route `/api/case-original-pdf`): the
  stored PDF of a file citation only, served after the same readability check as its
  text (tenant, clearance, audience without `*`, live source gate) and only when an
  employee principal grants it (never `agent:`/`sp_`, as `operating/entitlements.py`);
  re-hashed before it is returned. A refused and a nonexistent original read the same.
- **Case record**: attaching appends a cited fact finding (size, SHA-256,
  pages, method, OCR pages). No business write.
- **Reference investigation** (history: the AO case script, retired from product code and
  its live checks; it survives as the tests' reference investigation
  `operating/tests/ao_reference_investigation.py`, without a CLI. Product AO cases open
  through the generic path, `python3 -m operating.case_workspace_live --open ao=<code>`,
  live check `case.workspace_live`): it records the case id, the link chain, the three finished-good differences,
  damage per component, the derived produced figure, related AOs on the order
  line, the Portal manufacturing order's served FG columns (`mo`), AO header
  emptiness, the three dates, `paper_leads` ([PAPER] figures from the leads file
  beside the cited FM values, unverified) and `checks` (exit 1 if a boolean check
  fails; `mo_found` and the counts are informational). Finished goods are derived
  from the AO's packaging row consumed one per unit (`quantity` and
  `actual_amount` ÷ `qty_per_batch`, chosen by rule, never by id); damage rows
  stay in their component's unit and are never one "pack" total; the link's
  `qtymfg_c` is compared, not used. Dates are compared as calendar dates; FM
  dates that say late become an `unknown` "evidence conflict" gap (FM's AO date
  is not its Filling date; paper leads add what the paper says), never a
  lateness fact, and unreadable dates become an `unknown` gap instead. Produced
  minus filled is matched against the cited Line clearance task rows; component
  rows implying another produced figure become a cited conflict gap. Paper
  figures stay out of the case (every number in a case statement must be in a
  cited row) until the original is attached; the case's agree/disagree wording
  is built from the leads file's digit-free labels. Playbook v2 enforces the
  basis (`basis_row_agrees`: the one cited item row has `qty_per_batch` 1,
  numerically, and every such source row agrees) and damage per component
  (`component_sums`: each `assembly_item_id` summed alone in its own unit, never
  across components); roles sharing a relation (item/component,
  filling/clearance) do not make each other's rows unbound. Each hop is read
  by the id the previous cited row carries (AO → link by `idassembly`, link →
  line by `idoeitem`) and the chain AO = link AO = item AO, link line = line,
  line order = resolved order is checked before anything is recorded: only a
  consistent chain yields the supported relationship fact, ordered −
  manufactured and the lateness comparison; a line of another order becomes a
  `contradicted` finding plus an `unknown` gap citing all rows
  (`checks.chain_consistent` false, exit 1). It writes only its own state directory.
- **Proof** (`operating/tests/test_ao_26005b_case.py`, injected transport +
  synthetic scan): the AO-26005B case through the MCP tools — resolve and cite
  the AO, link, line, order, the one finished-good component row, filling and
  damage rows; compute planned − produced = 6 EA, produced − filled = 1 EA,
  ordered − produced = 417 EA (finished goods = component ÷ `qty_per_batch`,
  each difference through its declared role relation) and damage per component
  (3 / 7 / 20, three units, no total); record the unfilled unit as the cited
  Line clearance quantity and the Portal manufacturing order's samples / FG
  damage / size; record the gaps (related AOs on the line, the lateness
  evidence conflict, empty AO header quantities, paper original not attached); scored by playbook
  `ao_quantity_reconciliation` v2; attach the scan and cite its OCR page; read the same case
  from a fresh runtime; an operations-only member of the case can neither
  attach nor probe the quality scan and reads neither its content nor
  `silver.*` quantities; tampering with the stored scan withdraws its findings;
  changed rows change the computed numbers (they are never the constants).

## 6a. Read-back verification (`operating/case_verification.py`)

What follows a proposal: a person carries it out in the owning system
(approval and execution stay with Portal/MC), and the
Workbench reads the source back. `plx.case_propose(case_id,
proposal_id, references?, max_staleness?)` (verify mode), same default deny and session
actor as section 3:

- **Readable proposal only**: unknown id → `proposal_not_found`; a proposal
  withheld from the caller → `proposal_withheld`; no `expected_effect` →
  `proposal_has_no_expected_effect`; a closed case → `case_closed`;
  `max_staleness` outside 0..315360000 s → `invalid_max_staleness`. Refusals
  record nothing.
- **Read-back**: the `target_citation` is resolved for the caller with
  `max_staleness` (default the mirror's 900 s), so an older relation is
  refreshed first; a superseded capture is followed to its
  `current_citation`. The current row decides:
  `effect_observed` only when every listed field holds its own expected value
  (text trimmed and exact; an integer compares numerically, `5 == "5" == 5.0`,
  never a boolean; a record from before per-field values, one `equals` for
  several fields, does not say which field the write sets, so some-but-not-all
  holding the value is `unknown` (`legacy_effect_one_value_several_fields`),
  whatever the catalog declares today), otherwise
  `unknown` (`field_not_inspectable`) when any field could not be read,
  `effect_not_observed` when every field was read and not
  all hold their values, and
  `unknown` with a reason when it cannot be judged: `relation_refresh_failed`,
  `row_not_returned_by_latest_full_traversal`, `row_not_current_in_mirror`,
  `key_not_unique`, `target_citation_unresolvable`, `field_not_inspectable`
  (column not mirrored, JSON unparseable or not an object).
- **Per field**: `equals_expected`, `differs` (`value_withheld: true`, the
  value is never echoed: it can be a person's name), `empty`,
  `field_absent`, `not_inspectable` with its reason.
- **Honesty**: `effect_observed` says what the replica row shows now, not
  who changed it or whether it was approved (`attribution:
  not_established`); `held_when_proposed` says whether the cited row already
  held the value when the proposal was made. `effect_not_observed` is not
  proof that nothing was done (replica lag; a change the relation's `since`
  column does not show appears only after a full traversal). There is no
  "failed" outcome. `references` (up to 10, e.g. an approval or MC task id)
  are recorded verbatim with `references_verified: false`; they grant nothing.
- **Record**: each call appends one `verification.recorded` (`V<n>`) citing
  the row read back (or the target when no current row was read) plus every
  proposal citation, so an operations-only reader of a customer-PO case sees
  it withheld, and revoking a grant hides it on the next read. Repeated calls
  append; history is never rewritten.
- **Customer PO**: `plx.case_propose` records the effect
  automatically (`public.app_sales_order`, the order key, fields
  `po_reference` and `custom_fields.custPoCode`, the proposed PO), so the
  proposal can be verified once someone sets the PO in Portal.
- Evidence label: `injected-transport`
  (`operating/tests/test_case_verification.py`).

## 6b. Checked value claims (`operating/claim_checks.py`)

A resolving citation does not prove the sentence (RSN-06), and parsing which prose word
goes with which number fails both ways. So quantitative statements are
**structured**: `plx.case_add_finding` and `plx.case_record_gap` take `value_claims`
(`{value, citation + field (row.<column>) | calculation (C<n>), role, unit}`; each
citation must be one of the item's own; shape checked at write, `CaseStore.finding/gap
(value_claims=...)`). Models propose claims; the Workbench checks each on read, over the
row re-read for the reader and digest-checked:
- `claim_value_mismatch`: the field (or the recomputed calculation) holds another value;
- `claim_role_mismatch`: the field's role contract says another quantity type (or the
  claim states none on a role-pinned field; `produced` accepts `manufactured`/`actual`).
  A calculation's **quantity type** is derived by a closed type algebra over its
  expression tree (`operating/quantity_types.py`; this replaced the name-based,
  left-operand contract wholesale after finding caller-chosen names and
  operand orders relabelling planned figures):
  - inputs are typed by their re-read source contracts only (declared roles, literal
    roles, relation names and operand order are never trusted); a cited input is the
    source cell it reads, a literal its recorded number;
  - the expression is simplified exactly (Fraction coefficients) into a polynomial over
    those cells; division by a sum is an opaque factor;
  - **typed T** only when every term is one T-typed cell with coefficient +1 and no
    constant (`T`, `T + T'` of two cells of the same type T), optionally minus cells of a
    documented part of T (`SUBTRACTIONS`: manufactured/actual/produced − damaged,
    Portal `portal/prisma/schema.prisma@fd94c6c` line 2570 "good qty = produced −
    this", line 2534 at `878705b`; planned − damaged has no such evidence and is untyped);
  - **untyped** otherwise: mixed-type sums, any other difference (also `T − T`),
    products, ratios, scalings (`2 * p`, `p / per_unit`), constants, a cell counted
    twice (`p + p` is `2p`, also across two captures or generations of one row: a cell
    is the row's relation, canonical key and column, without the handle's `@` tail; one
    cell read at two different values, a row changed between captures, makes the value
    untyped and refuses every label, `one_cell_two_values`; the gate compares the values re-read from the digest-pinned rows, an absent cell never equal to anything, so `m + p − q` over a
    recaptured planned cell is never `m`),
    division by a sum nested deeper than 8 (bounded work, fail closed); identities cancel, so `m + (p − m)` is the planned `p`,
    `p − (p − a)` the actual `a`, `p + a − a` and `p + a * 0` the planned `p`, and
    `p + d` is untyped.
  A relation's `as` and a derived claim role are **labels** that type nothing. A label
  may not be a source role (`planned - damaged as produced`), and must fit its form: the
  type words before the first connective (`not`, `minus`, `less`, `net`, `over`,
  `after`, ...) name exactly the types of the positive terms, those after it only types
  of the negative terms (`planned_not_produced` fits `planned − actual`;
  `produced_total` cannot name `m + (p − m)`, `damaged_total` cannot name
  `planned + damaged`, `planned_x` cannot name `p − (p − a)`). A claimed quantity type
  must be the computed type (or its alias); a derived claim role must be the recomputed
  label; a claim stating no role passes only on an untyped value;
- `claim_role_unverified`: a quantity type (planned, produced, filled, inspected,
  accepted, damaged, ordered, ...) on a field no role contract grounds, or on a
  calculation the algebra leaves untyped (`planned − actual` claimed as `planned`);
- `claim_unit_mismatch`: against the contract unit, the unit a calculation recorded for
  that field, or the calculation's unit;
- `claim_uncited`: the row is not readable now, or a calculation reads rows the item does
  not cite or does not recompute from them;
- `claim_unstructured` (**prose fail-closed**): any number in a finding statement, a gap's
  `missing`/`why` or the close outcome (against all claims of the case) that is not a
  handle, date, time, identifier (`SO-2026-0003`) or known-prefix name (`SHA-256`, `F2`)
  and that no claim covers with the same value (`SKU-1509`, `x1509` are numbers); numbers
  in words count from ten up; text claims (`PO#5447`) cover their exact text.
- `claim_wording_role_mismatch` / `_unverified`: a quantity-type word bound to a number
  (closest in its clause; commas, `and`, `but`, `than`, line breaks separate clauses; a
  word reaches into a parenthesis but not out of one) whose claims carry another quantity
  type, or none. The type is the one the checker grounded (contract role; a calculation's
  type by the algebra), never a caller-chosen name. An untyped calculation is cited as a
  number only: a type word bound to it, or unbound in its sentence, blocks (`6 planned
  but not produced` blocks; `a shortfall of 6` passes). `passed QC`, `QC passed` and
  `passed inspection` are inspected-family words. A word bound to no number
  (`Produced, 1509`, `produced the full batch of 1509`) is checked against the claimed
  numbers of its sentence that no other word bound: blocking when one carries another
  quantity type and none agrees, a lead when they are only untyped or identifiers. Next
  to an identifier claim (only on an id-shaped column: `id`, `*_id`, `id*`, `*code`,
  `*number`) it is an advisory lead. The close outcome gets the same wording check
  against all claims of the case (failures under item `outcome`).
- Workbench-written texts are exempt from the prose checks: checked links, and
  `statement_by: workbench` set only by tool code (attach / re-extract / mail findings,
  the customer-PO proposal evidence and coverage gaps; mail subjects and operator file
  names in them are not checked).
- Roles pinned for this: `public.app_manufacturing_order.quantity` / `size_pieces`
  planned and `qty_damaged` damaged (Portal `assembly-queries.ts`, `api-schemas/assembly.ts`,
  `schema.prisma` @8da33e4); `silver.production_tasks.qty` filled **when** `task_type` is
  `Filling` (new conditional contracts in `source_units.py`).
- `case_verification.check_claims(runtime, actor, case_id)`: read-only; passes only with
  no failure and no withheld or changed finding/gap. `verify_proposal` returns
  `evidence_claims` for the proposal's evidence finding.
- `playbook_eval.evaluate` runs the gate on every evaluation (evaluator `/8`): any failure
  or unchecked item blocks `passed_all` (`claim_checks` in the report, `claim_*` in the
  summary). The AO and packaging scripts emit claims for every number they write; the AO paper/FM conflict gaps claim the FM figure on its damage row, damage sum, Filling task or calculation, and the paper figure on the attached original's page citation (`field: "text"`: the page's machine-read text, re-read, holds the figure as a number token, `1,509` read as one figure; presence on the page, not in a labelled field; untyped, so it stays a lead and types no word; a unit on it is refused). In a `page N` / `pages N, M` phrase a number is a locator only when the item cites that page (`original://…#page=N`); the run ends at the first other number, which stays a quantity (`pages 3 and 4, 1600 units` needs a claim for 1600); singular `page` takes one number and a repeated number ends the run (`Page 3, 3 cartons` needs a claim). The wording check blanks the same locators first, so a type word never binds to a cited page number (`Produced (page 3): 1509` is checked against 1509). Only `page`/`pages` are page words: abbreviations (`p. 3`, `pp. 3-4`, `pg 4`) stay quantities that need a claim, digits right after a letter and a period (`p.3`) are numbers, and an abbreviation's period (`p.`, `No.`, `vs.`, `ref.`, `Fig.`, `approx.`) ends no sentence for the wording check only when a digit follows it directly or after one space (`p. 4`, `No. 3`); otherwise it is a sentence end (`…produced on Line E. The FM row counts 1503.` is two sentences). `i.e.` and `e.g.` are whole tokens. Fail-closed backstop: a number equal to a claimed value of the item (any claimed value of the case for the close outcome) is never a locator, so it stays a quantity for both checks and its claim must fit (`Pages 4 and 3 units were produced` with 3 claimed damaged blocks). Remaining limit: an unclaimed number that equals a different cited page right after a page list (`pages 3 and 20 boxes`, pages 3 and 20 cited, 20 unclaimed) is read as a locator; so is the singular `page 3 units were produced` with page 3 cited and 3 unclaimed (the sentence then states no figure). An unclaimed number never covers or types anything. The playbook evaluator re-reads only `api://` rows, so on a case with an attached original its claim gate reports the page claim `claim_uncited` (AO_V2 `passed_all` was already false on that case before this change).
- `plx.case_get` (read-only MCP tool) returns the same verdict. Proposal
  `evidence` may carry `value_claims` (Python API).
- Known limits: the sentence backstop does not block a type word beside only untyped
  source fields or identifier numbers (a lead; untyped calculations block); the type
  algebra is sound but not complete (division by a sum is opaque, so `p * (a+b)/(a+b)`
  is untyped); a label on a negated difference is checked against the sign-normalised
  form, so `-(d − p)` labelled `damaged_over_plan` is refused (a safe over-refusal); digits under a known id prefix (`AO-1509`, `MO1509`) are a
  name and escape both prose checks; numbers in words below ten are not counted; the
  wording check is a heuristic over English phrasing, the structured claims are exact.
- Evidence label: `isolated-fixture` and `injected-transport`
  (`operating/tests/test_claim_checks.py`, AO-26005B and PO5447 cases).

## 7. Live sources in cases: `graph-mail://`, `sharepoint://`, `bc://`

Mail, SharePoint and BC are product sources, cited in
cases exactly like `api://` rows and `original://` files.

- **Host**: `python3 -m operating.mcp_server --state-dir S --source-mirror-audience M
  --live-source-audience L [--sharepoint-drive <QA/QC drive id>] --principal ...`.
  `L` is a `plx-live-source-audience/v1` file (`integrations/live_source_guard.py`;
  keys `mail:<mailbox>`, `sharepoint:<drive-id>:<drive-relative path>`,
  `bc:<environment>:<company-id>:<entitySet>`; default deny). `build_live_readers`
  (`operating/case_runtime.py`) builds the GET-only readers from
  `MICROSOFT_GRAPH_*`, `SHAREPOINT_DRIVE_ID` (+ `--sharepoint-drive`, e.g. Ricardo's
  QCQA drive) and `BC_*`. A source without its variables
  (every keyless/test host) stays unconfigured: its scheme refuses with the typed
  `source_not_configured` (MCP error text `source_not_configured: ...`; on case reads
  the item is withheld). No request is made at startup.
- **Resolution** (`CaseRuntime.resolve_citation`): dispatch by scheme to the reader's
  `resolve_citation(handle, actor)` (mail with a body pin: `get_message` with the
  bounded body). The reader checks its audience for the **reading actor** (the session
  actor) before any content read; not visible, nonexistent, malformed and source down
  read the same (`live_citation_unresolvable`). `current` gives a content-stable view
  of the cited version (record without ACL grants / record hash / handle);
  `changed_since` gives a marker without the record, so its digest never matches and
  the case shows `citation_content_changed`. `CaseStore` refuses a `supported`
  finding and any calculation input on a citation the resolver marks
  `changed_since` (`citation_changed_since`; cite `current_citation`). Nothing is
  cached: every case read re-reads the source; SharePoint status is eTag-based (bytes
  are not re-downloaded on resolve).
- **Tools**: `plx.find` (one bounded page of mail / a SharePoint folder /
  BC companies or an entity set, each item with its citation; withheld items only
  counted; mail is one Graph page, at most 50 messages); `plx.read` of a message (its `cite_as`, cited with
  `plx.case_add_finding`, is
  `graph-mail://<mailbox-id>/<message-id>@<changeKey>#body_sha256=<hex>&shown_sha256=<hex>`:
  the hash of the first 20,000 characters of the plain-text body plus a hash of the
  fields the statement shows (sender, recipients, subject, times, attachments flag);
  such a pinned citation stays `current` while those are unchanged -- a changeKey
  drift alone (read, flag, category, folder move) is reported as `change_key_changed`,
  not as a content change; the body is returned to the caller and never stored);
  `plx.case_attach_original` with `drive_id` + `item_id` (below); the host read
  `plx.source_citation` (drivers only) reads a live citation's status and metadata;
  `plx.case_add_finding` / `plx.case_compute` / gaps / proposals accept the three
  schemes (e.g. BC `record.fields.totalAmountIncludingTax`, exact decimal text).
- **SharePoint originals**: `plx.case_attach_original` (`drive_id` + `item_id`) resolves the item for
  the actor (drive/folder audience, item ACL where `source_acl_required`), downloads
  under `min(max_bytes, document MAX_BYTES)` (over the cap: `original_too_large`, no
  download), stores the bytes content-addressed (SHA-256) with immutable provenance
  (`original_source` table: drive, item, eTag, cTag, path, size, hashes, source ACL,
  `sharepoint://...#sha256=` citation, audience keys, read time), extracts through the
  existing pipeline (OCR, re-extraction) and records a fact finding citing the
  `original://` and the `sharepoint://` item version. On every read of the original
  (resolve, page text, re-extraction, case reads) the drive's reader re-reads the item
  for the reader live (`SharePointReader.get_item`: metadata, plus permissions where the
  audience requires the item ACL; never a download), so the item's *current* path and
  ACL decide: access removed upstream, a move under a denied folder, or a source outage
  deny; a host without that drive's reader cannot read it (fail closed). Listing
  (`OriginalStore.list_visible`) makes at most `max_source_checks` (20) live checks and
  leaves unverified source originals out. The attach-time ACL is kept in the immutable
  provenance row as history only; readers see its status and grant count, never the
  grantee list. A later item change shows the finding as `citation_content_changed`
  while the stored bytes stay verifiable. Same item version + bytes + audience = same
  original.
- Evidence label: `injected-transport` (`operating/tests/test_case_live_citations.py`).
  **Not live-verified.** Limitations: reading a SharePoint original costs one or two
  Graph GETs per read; group grants are not expanded (reader limitation); listing is
  one page (no continuation token); PDF originals only; whether Exchange bumps the
  changeKey on read/flag is live-unverified (pinned mail citations do not depend on it).

## 7a. Mail in cases: related messages, attachments, people (`operating/case_mail.py`)

WS-08 / ING-02 / ING-13 / DOC-04. For one case and one
reader, `case_mail_evidence(runtime, actor, case_id, mail_capture_dir=...)` answers
which mail belongs to the case, what it carried and who was on it, from what the
reader may read now.

- **Scope**: only mailboxes of the case mail scope (`operating/case_mail_scope.json`,
  `plx-case-mail-scope/v1`: the reported 12-address Graph allowlist at petrasoap.com;
  `accounting1@` stays out while `pending_confirmation`). A mailbox is in scope when
  any of its directory identities equals a listed address. Every `CaseRuntime`
  applies the built-in scope by default (the MCP host and the case console included):
  every mail read outside it (cite, list, resolve, attachment, original) is refused
  before any message is requested and every record read is re-checked. The only
  opt-out is the explicitly named `MailScope.unrestricted()`, passed to `CaseRuntime`
  or carried by `LiveReaders` that `build_live_readers` built over an injected
  in-process Graph sender (tests, the console's fixture world: no request leaves the
  process, the mailboxes are synthetic); readers over the real transport never carry
  it. `case_mail` refuses an unrestricted runtime. `case_mail` reads the mail
  capture index narrowed to the scope (identities recorded at capture), so an
  out-of-scope mailbox is not even looked up.
- **Which mail**: the versioned mail capture (`integrations/capture_mail.py`, the one
  L1 mail store) is the index, read as
  the reader (live audience over the mailbox identities and owner ACL recorded at
  capture); its completeness (delta rounds, attachment lists) decides
  `mail_source.complete`; nothing is listed again. The ingestion mail unit
  (`operating.ingestion`) is no longer read here. Relations: the case's PO rows (roots
  or cited `api://public.app_purchase_order/...`) through the projection's stored
  `Email.mentions_purchase_order` links (`ontology/mentions.py`; reviewed and
  corroborated links are identity, leads need a person, a marked link is counted as
  withheld; the per-scan `operating/po_mail_links.py` is retired); mail the case already
  cites; and `same_conversation` siblings in the same mailbox (a lead). Every message
  is re-read for the reader (`CaseRuntime.mail_message`).
- **Citations**: messages body-pinned
  (`graph-mail://<mailbox-id>/<message-id>@<changeKey>#body_sha256=..&shown_sha256=..`);
  file attachments byte-pinned
  (`graph-mail://<mailbox-id>/<message-id>@<changeKey>#attachment=<id>&sha256=<hex>`,
  `integrations/graph_evidence.py`), resolved by `GraphMailReader.get_attachment`
  (mailbox audience, owner ACL and message folder provenance first, then one GET of
  the attachment, decoded and re-hashed). `CaseRuntime.read_mail_original(handle,
  actor)` returns the body / bytes of a pinned citation through that GET-only path,
  only while they are the cited ones, and only for a person's principals (never an
  `agent:`/`sp_` identity; `mail_attachment` and `attach_mail_attachment_original`
  apply the same rule). A case view re-reads the message metadata for the reader on
  every resolve but downloads the attachment again only when the message's changeKey
  moved since this runtime hashed it. A PDF attachment can be stored as an `original://`
  (`attach_mail_attachment_original`; provenance `graph-mail-attachment`) whose every
  read re-reads the message metadata for the reader. Item and reference attachments
  are listed, never cited.
- **People**: From/To/Cc addresses of the visible related messages
  (`graph_evidence.mail_people`), each with the message citations it appears in;
  addresses only, never merged, never resolved to a contact or employee; the
  mailbox's own addresses are marked `is_mailbox`.
- **Permissions**: a mailbox the reader may not read is a withheld marker (how many
  of its messages relate is not shown); `record=True` writes one Workbench-written
  fact finding per related message citing the message, its attachments, stored
  originals and the PO row, so a reader who cannot resolve all of them sees the
  finding withheld.
- **CLI / live check**: `python3 -m operating.case_mail scan` (counts only):
  ingests the in-scope mail sources once, builds up to N PO cases chosen from the
  ingested mail's PO-number proposals (data, never a hardcoded case), records the
  findings, re-reads every citation and checks a second reader without a mail grant.
  Spec `verify/live-checks/case.mail_evidence.json`. **Live-unverified**
  (`injected-transport`: `operating/tests/test_case_mail.py`,
  `integrations/tests/test_graph_mail_evidence.py`).
- Cost: directory lookups are shared within one call / CLI run and the CLI reuses
  its PO survey per case; the CLI reports `graph_requests_sent` (injected probe with
  100 ingested messages and 2 cases: 535 requests, down from 1865).
- Limits: related mail is only what was ingested (not found is not absent); the
  allowlist is
  reported, not verified against the Exchange policy group; no MCP tool exposes
  `case_mail` yet (`operating/mcp_server.py` is owned elsewhere).

## 6. Not done yet (limitations, not claims)

- **No live verification**: the mirror and case tools have not been run
  against the deployed Workbench API. Only `injected-transport` tests exist.
- **No approve / execute**: there is no link from `proposal.recorded` to
  Portal/MC approvals or dispatch records (brief queue items 4 and 5);
  proposals stay records. Outcome read-back exists (section 6a) but cannot
  tell who made a change, and approval/dispatch ids passed as `references`
  are not checked.
- **Live sources not live-verified**: `graph-mail://`, `sharepoint://` and `bc://`
  citations, SharePoint originals and mail in cases (section 7a) are proven on
  injected transports only (spec `verify/live-checks/case.mail_evidence.json`).
- **Uncited text is not gated**: gaps and proposals are gated only by the
  citations recorded with them. The generic `plx.case_record_gap` /
  `plx.case_propose` accept caller-typed text and cannot tell whether it quotes
  source data; a caller that quotes rows without passing `citations` publishes
  that text to the whole case audience. The case question, roots and outcome
  are never gated. The customer-PO tools always cite.
- **Mirror entitlement is relation-family, not row-level**: nothing scopes rows
  per customer or supplier yet. The audience map needs PLX-confirmed principals
  before real use.
- **Extreme single items**: a finding at the validation maxima (20 citations of
  2 KB plus 4,000 quote-heavy characters), or a case header whose question,
  roots and outcome are all near their maxima with heavy escaping, cannot fit
  one 48 KB response. The finding is paged as an `oversized` marker and the
  header section fails with `result_too_large`; the events remain in the store.
- **`plx.object` is not paged by bytes**: a 500-row wide page can still hit
  `result_too_large`; narrow `columns`/`limit` and page with `offset`.
- **Customer-PO coverage reads only the two exposed fields** per order (the
  CLI diagnostic counts FM `order_entry.custpocode` and Sage `ponumber` but
  never cites or proposes from them): it does not
  follow Sage/FM legacy tables, mail, SharePoint, BC (whose External Document
  No. the ship-only bridge overwrites with the shipment number) or attachments, and the
  proposal cannot verify that evidence from another row belongs to the order
  (it is labelled as asserted). A duplicate proposal is recognised only when
  the evidence capture is the same; re-citing the value from a new capture
  records a new proposal. The per-order gap writes are separate transactions
  (every citation is checked first, but a crash mid-call can leave some of
  them). Live counts through this tool: auditor runs on ad8b281 (2026-09-30)
  and 5f234e3 (2026-10-01) — identical Sage 42/22 and FM 177/168/35; the
  Portal-aligned origin rule, equals-order-number status and new leads await the
  next live run. The MCP tool schema/description in `operating/mcp_server.py`
  still lists only the old field statuses and no lineage.
- **Originals**: PDF only; one principal per original (share with more
  principals by placing a copy in each directory); anyone with filesystem
  access to the host can read the inbox and store; OCR has no orientation detection;
  numbers read by OCR are not machine-compared with the cited rows (a person
  confirms them).
- **Proven on fixtures, not on two real cases**: the brief's bar ("prove on
  two different cases") is still open. AO-26005B is scripted end to end on
  fixture rows shaped from the live trace; its live run is queued for the
  live checks, and Ricardo's real scan has not been attached anywhere.
- **Packaging case (`operating/packaging_case_script.py`, retired;
  purchase orders open through the generic path)**: the case is rooted
  on a stable row id (Portal `app_purchase_order.id` or FM
  `generated_purchase_order.id`); a PO number only finds candidates (every
  spelling, a digit-run scan of both rootable relations and custom_fields
  links, all collected before deciding) and is recorded as a lead; several
  candidates not provably one PO are refused, never picked. Receipt-to-PO
  links are always labelled leads. Not done: Sage-rooted cases
  (`sage.po_header`/`po_receipt_header` are searched and cited as leads only),
  `silver.receipt_pack_items`, supplier/product for FM roots (no verified join),
  BC reads, attached PI/freight originals; landed cost stays an analytic
  estimate (ERP-D-015 expenses freight and duty), never item cost. A live
  number lookup pages the number column of each searched relation, so the CLI
  waits for the MCP server's per-minute call window rather than failing.
