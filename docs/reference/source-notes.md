# Source notes (read 2026-09-30)

Facts distilled from ~400 KB of project history. Sources: ACCESS.md (removed in 421ab6b4),
DEMO.md (removed in 421ab6b4), MEETING.md, docs/PORTAL_*.md, docs/FREIGHT_WORK.md,
docs/history/. Re-verify against current upstream code
before relying on a contract detail; these are pinned to Sept 24–29 reads.

## Data paths into Petra records
- **Workbench reporting API** (TASK-2014): live, read-only, 93 relations,
  keyset pages, no exact-record filter, non-atomic. Its DB login `wb_api`
  **expires 2026-12-27** — renewal is a Vince/Portal task. Advisory source
  `portal-fm-api-paged` only; never action authority.
- **Personal replica logins** `wb_tanush`/`wb_ricardo` (TASK-1996) exist on the
  staging reporting replica, **expire 2026-12-24**; still need DSN + `/32`
  network rule + TLS. Exact named FM reads (`integrations/portal_replica.py`).
- **Portal bridge** `POST /api/external/swarm/v1/query` reads the **UAT**
  database, not operational FM/Sage history. Its live sender is retired
  (2026-10-01; fixture transport only). Needs a `PORTAL_BRIDGE_*` key;
  `customer_tenant` role lacks SalesOrderLine; excludes Json fields
  (`customFields.fmId`). Admin "API Keys" page keys are broad and are NOT this.
- Ricardo's staging access pack (`docs/runbooks/TANUSH-STAGING-BC-SAGE-ACCESS.md`
  in Portal) says BC read credentials and a staging Sage replica login are
  ready to issue after Vince approves. Graph mail pending Exchange scoping.
- BC mirror in Portal was stale on 2026-09-24 (invoices last synced 09-10,
  payments 07-28, 4,900 dead letters). Check freshness before any finance claim.

## GET routes that WRITE — never call them as reads
- `GET /api/mrp/wms/shipments/orders?orderRef=<numeric>` → `ensureSalesOrderFromFmOe`
  can create/sync a Gold order. Only non-numeric Gold IDs are safe.
- Project Development page load → `ensureTheoreticalFgForProjectKey` writes rows.
- `GET /api/mrp/quotes/[id]/matrix` → `quoteProjectLanded` wrote the FG snapshot until
  `9108064` (TASK-2850, 2026-10-14): the matrix `GET` and `POST` now pass `persist: false`
  (`app/api/mrp/quotes/[id]/matrix/route.ts@71f7214:125,229`), so viewing a quote or saving
  an offer writes no `app_theoretical_fg` snapshot (the `POST` still writes quote lines). The
  same commit's message says the workbook context and `loadQuoteInputs` pass it too;
  `loadQuoteInputs` is re-read at `1f2d42d` below. A snapshot `calculatedAt` before 2026-10-14 can
  still come from a matrix view.
  **Write-on-read trap, re-verified 2026-10-13 at Portal staging `38cd3f3`** (`project-landed-quote.ts` unchanged since `5cb95e1`)
  (TASK-2119, `38a31ea`): `QuoteVolumeOptions.persist?: boolean`
  (`portal/src/lib/costing/project-landed-quote.ts@5cb95e1:1433`) defaults to
  writing. Only `persist === false` skips the writes: the guards are
  `opts.persist !== false` at `:1868` (passed to `getPrimaryRouting`) and
  `:1957` (`writeTheoreticalFgSnapshot` to `app_theoretical_fg`). At `4243499` (TASK-2809
  split, `4dc5cdf`) the same rule holds with the option at `project-landed-quote.ts@4243499:1628`
  and the guards at `quote-inputs.ts@4243499:116` (`getPrimaryRouting`) and
  `project-landed-quote.ts@4243499:2350` (`writeCostSnapshot`, `:2302-2303`).
  `fg-routing-store.ts@38cd3f3:251-286` `refreshPrimaryRoutingSetup` also
  saves blank setup/changeover minutes on the primary `FgRouting` unless
  `persist === false`. The only caller that passes `false` is the operator
  script `portal/scripts/recalc-theoretical-fg.ts@487862b:134` (dry run by
  default, `--apply` writes, host guard `assertAgentWritable`). Every route
  call omitted it up to `38cd3f3`, so all of these wrote: matrix `GET` (`app/api/mrp/quotes/[id]/matrix/route.ts@38cd3f3:123`)
  and `POST` (`:226`; TASK-2611 `514e1e5` only added a G-D4 comment; TASK-2648 `b1cf16c` saves planned conversion and `fp_code` on offer lines; both read-only for the snapshot since `9108064`, above), workbook `GET` (`…/workbook/_lib/context.ts@19f1d94:78`: since `4dc5cdf` TASK-2809 one `runQuoteProjectLanded` run; read-only since `9108064`, `context.ts@40237ed:79-82` passes `persist: false`),
  and both recalc `POST`s. `getPrimaryRouting` without the option also writes
  from `final-products/ao-setup-snapshot.ts:278` and `fg-activation-store.ts:297` (`@38cd3f3`).
  **Re-read at Portal staging `1f2d42d` (2026-10-14):** the routing fill no longer writes on
  any path. `9108064` (TASK-2850, CT-B) makes `quote-inputs.ts@1f2d42d:90-101`
  `loadPrimaryRoutingSafe` always pass `persist: false` (the parameter is gone), and
  `255ec2a` (TASK-2863, RV2a) makes `fg-routing-store.ts@1f2d42d:267-302`
  (`withSetupFilled`, `refreshPrimaryRoutingSetup`) fill blank setup/changeover minutes in
  memory only; `getPrimaryRouting` (`:139-145`) accepts `persist` and ignores it, and
  `listRoutingsForProject` (`:120-131`, `GET /api/mrp/fg-routing`) fills the primary in
  memory instead of saving and re-reading. So AO setup, activation and the fg-routing GET no
  longer save routing minutes; values saved before `255ec2a` stay stored. The snapshot guard
  is unchanged (`project-landed-quote.ts@1f2d42d:2351`, `opts.persist !== false`). The
  recalc script still runs the engine with `persist: false`
  (`recalc-theoretical-fg.ts@1f2d42d:140`) and `--apply` now skips a frozen (ACTIVE or
  RETIRED) FG, keeping its snapshot and counting it as frozen (`:18-20`, `writeChangedSnapshots`).
  The quote-inputs header (`:15-18`) still says the other callers save the fill "until RV2a";
  that comment is stale upstream, the store code above is the contract.
  **Re-read at Portal staging `d4d1bb2` (2026-10-15):** the read
  path is unchanged in behaviour; only lines moved. `quote-inputs.ts@d4d1bb2:90-104`
  (`loadPrimaryRoutingSafe`) still passes `persist: false`, now with the setup-standard rows
  this load already read (`0f9fbee` TASK-2948, one read shared with the fill; a failed rows
  read resolves to the workbook seed), and its header (`:15-18`) is no longer stale.
  `fg-routing-store.ts@d4d1bb2:274-298` (`withSetupFilled`) and `:307-318`
  (`refreshPrimaryRoutingSetup`) fill in memory only; `getPrimaryRouting` (`:156-161`,
  options `PrimaryRoutingReadOptions` `:141-150`) still ignores `persist`;
  `listRoutingsForProject` is at `:126-138`. The snapshot guard is
  `project-landed-quote.ts@d4d1bb2:2386` (option at `:1636`); the recalc script is unchanged
  (`recalc-theoretical-fg.ts@d4d1bb2:140`). Save-path changes, none read by the Workbench:
  `0a62186` (TASK-2864) makes every primary routing save also write a one-band active route
  plan (origin `legacy-save`, `fg-routing-store.ts@d4d1bb2:402-415`), and a frozen FG refuses
  that write with 409 `FG_FROZEN`, which rolls the routing save back; `eddbfe3` (TASK-2914)
  keeps blank minutes and client throughput on save. `b8f6e22` (TASK-2853) adds the
  `LineOverheadRate` table and `User.canManageOverheadRates`; `371b4cc` (TASK-2865) adds its
  store, which says it is not read by the quote engine yet, so overhead per line-hour
  (ERP-D-022) is still `RateSettings` (`quote-inputs.ts@d4d1bb2:151`,
  `source: "rate_settings"`). `35cfb18` (TASK-2811) census text for unrouted labour
  (`ready-to-quote.ts@d4d1bb2:461`, priced at $0 and reported) is ported in
  `operating/landed_cost.py` (`PORTAL_REVISION` = `d4d1bb2`).
  **Re-read at Portal staging `d53137e` (2026-10-16):** the
  engine files are byte-identical to `d4d1bb2`. The recalc script's dry run is now
  `runQuoteProjectLanded(ref, { persist: false })` (`recalc-theoretical-fg.ts@d53137e:152`)
  with `landedQuoteOf(run.priced)`, and `335dc8a` (TASK-2878, CT-E2) makes `--apply` write
  (`quoteProjectLanded(ref)`, persisting) the `restamp` rows too: a draft FG whose costs did
  not move but whose stored fingerprint is missing or differs from the live one
  (`theoretical-fg-recalc.ts@d53137e:96-110` `recalcStatus`), so a snapshot can be rewritten
  with no cost change; a frozen FG is still skipped (`:18-20`). `8c68104` (TASK-2918) adds the
  `TheoreticalFG` freshness columns (`freshness_checked_at` … `freshness_reasons`), which only
  Recalc and Activate write; the Workbench is not served them. Both are writes the Workbench
  never makes. CT-A `readErrors` (`quote-inputs.ts@d4d1bb2:8-18`) is not mirrored as a field:
  a relation the caller could not read reaches `quote_landed` as `None`, is priced with the
  Portal fallback and is reported as `RECORDS_NOT_READ`. The raw query that
  dropped out of the goldens' `db_calls` when they were re-run at `d4d1bb2` is `b59b007`
  (TASK-2649, #2115): `projects/read.ts` `loadCustomerLite` now reads through
  `customers/portal-label.ts` `loadCustomerLabel` (portal customer records first, FM
  `public.customer` only as the legacy fallback). It is a display label and prices nothing;
  it is not the setup-standard read.
  **Consequence for the Workbench**: never call `quoteProjectLanded`, the
  matrix or workbook routes, or a Portal recalc, even "to read": the routes
  that now pass `persist:false` still run the engine, and the matrix `POST`
  writes quote lines. Up to `9108064` a GET of the matrix persisted a new
  snapshot (`calculatedAt`) and routing minutes, so an `app_theoretical_fg`
  timestamp from before 2026-10-14 is not evidence of a deliberate recalc. The Workbench reads served columns and mirrors the
  formulas; it never triggers them.
- Several AO query helpers catch errors and return `[]`: empty ≠ complete.

- **ERP decision log re-read 2026-10-14 at Portal staging `40237ed`** (diff `71f7214..40237ed`, only `1f2d42d` TASK-2789 S1):
  adds ERP-D-031, **Proposed** 2026-10-14 (one customer-facing product name and the customer's item code per FG, two nullable
  `app_fg_item` columns, frozen on open AOs at AO creation, BC Item Description plus one Customer Item Reference; FileMaker-origin
  FGs keep their names). The ERP-D-015 row (L25) is byte-identical, so the `@5724575#L25` citations of `operating/landed_cost.py`
  and `operating/process_stage_map.json` stand; no landed-cost or stage-map rule changes.

## Semantic traps (real contracts)
- **Quote.projectId is mutable** (re-verified 2026-10-01 at `487862b`):
  `PATCH /api/mrp/quotes/[id]` (`app/api/mrp/quotes/[id]/route.ts@487862b:39-160`)
  overwrites `header.projectId` in place for STAFF+ while DRAFT/SENT/NEGOTIATION
  (`EDITABLE_STATUSES:55`). It does no existence or customer check, and the
  audit row says only "Quote N updated" (`:158`). TASK-2182 (`487862b`) put it
  on the quote detail page (`quote-project-linker.tsx`; `quoteProjectLinkHeader`,
  "Lines stay untouched"). Saved offer lines keep prices from the earlier
  project. The reporting API serves only the current `app_quote.project_id`.
  `operating/quote_readiness.py` and `quote_cost_trace.py` find a change in the
  Workbench's own observation history and flag lines first observed before it
  (`QUOTE_PROJECT_CHANGED_AFTER_PRICING`); a census for another project is a
  stale finding (`CENSUS_FOR_DIFFERENT_PROJECT`), not a failure.
- WMS picker: `qtyShipped` counts non-cancelled (possibly unconfirmed) shipment
  lines; `dateRequired` is actually `SalesOrder.orderDate`; null ordered qty
  yields remaining 0 (treat as unknown); one lot can be attached to several lines.
- BC readiness route omits the order ID (cannot prove which order); `ready` is
  an enqueue prerequisite only. The outbox producer can select an order once its
  customer and at least one item map are linked and serializes **only** linked
  lines, so a partially mapped order can be a candidate with omitted lines while
  readiness still reports each unlinked line as a blocker (a risk, not proof an
  incomplete order was sent; `docs/PORTAL_BC_READINESS_READ.md`).
  Distribution-book orders use `::distribution` map keys the route doesn't read.
- Ship releases: MAKE lines need a released ship release; DISTRIBUTE don't.
- `InventoryLot.receiptId` is a scalar without FK. `QuoteLine` has no Product FK.
  Sourcing request `projectId` is a legacy scalar; `Formulation.projectKey` is
  provenance only. BOM `projectKey` vs `projectId` can conflict.
- FM: `custpocode` is not unique; order-line units are NOT in the read grant;
  AO plan field is `qtyplan_c`; physical FKs `idoe`/`idfp`; one AO can serve
  several customer orders (allocation review). FM replica lacks bronze `statusqc`.
- Customer PO lives in different fields by origin: Sage imports
  `app_sales_order.po_reference`; FM-migrated `custom_fields.custPoCode`;
  Portal-entered orders had neither on 2026-09-30 (see EVIDENCE/live/).
- Pricing: zero-priced lines resolve from the customer list (or Sage ARCUS
  `priclist`), then RETAIL; no effective date; not historical approval.
- Money arrives as float strings; parse with Decimal and keep the raw value.
- Printer order confirmations (`75b8e4c` TASK-3059, read at `853df2e`) live in
  `app_printer_order_confirmation` (+ `_line`), which the reporting API does not
  serve: a PO without a visible confirmation is unknown, not unconfirmed. In the
  Portal's comparison a PO line with no confirmation row is *missing* (never 0)
  and a row without `po_line_id` is *extra*. Its `GET` route only reads; the
  `POST` and the pack receipt `POST` write (`lib/mrp/printer-order-confirmation.ts@853df2e`).

## Portal capabilities to consume, not rebuild
- Landed-cost quote engine `portal/src/lib/costing/project-landed-quote.ts` and
  the ready-to-quote census (fill weight, SG, sourcing, **HS code**, vendor card,
  labour) — needs a Portal-owned read-only export (page path writes).
- Pallet-builder quote `POST /api/mrp/wms/pallet-builder/quote` (can rate with
  live carriers: third-party contact, so the Workbench never calls it; the HTTP
  reader was retired 2026-10-01; caller-attested responses only) (warehouse
  session) for freight/pallet calculations — Vince's packaging case.
- Quote totals contract `portal/src/lib/quote/totals.ts`; order identity
  resolver `portal/src/lib/orders/order-identity.ts`; BC entity map.

## Receiving originals: vendor CoA / CoC / packing-slip PDFs (TASK-2164)
Read 2026-10-01 at Portal staging `487862b` (commit `fdaa88f`, 2026-10-01 08:12 -0400;
earlier TASK-2164 commits `7ee5697` library/folder map, `9d1115b` admin document queue).
- **Where they are stored**: SharePoint, drive `SHAREPOINT_DRIVE_ID` (`lib/m365/auth.ts:48`),
  uploaded by `POST /api/mrp/receiving/{raw|pack}/[id]/coa`. Until TASK-3213 (`d7a1c95`,
  re-read at `b4118a3` below) there was no DB row, no `Attachment`/`Document` record and no
  per-receipt link; a file uploaded before it still has none.
  Raw: `Receiving/Raw/{safe(supplier.name)}/{safe(raw_material.rawcode)}` via FM
  `receiptraw.idrawsupp → raw_material_supplier → raw_material / supplier`
  (`app/api/mrp/receiving/raw/[id]/coa/route.ts@487862b:35-53`, now `@6e76a5b:34-58`). Pack:
  `Receiving/Packaging/{safe(packaging_supplier.suppname_c)}/{safe(packaging.packcode)}`.
  `safe` = trim, `[^a-zA-Z0-9._-]→_`, first 60 chars, blank → `_unknown`; a query
  error falls back to `Receiving/{Raw|Packaging}/_unknown/{receiptId}`.
- **Traps**: the folder is per supplier + material, **not per receipt**. Every receipt of the
  same material from the same supplier lists the same PDFs, so a file in the folder is
  not proof it belongs to a given receipt or lot. File name = `{epoch ms}_{sanitised name}`
  (`lib/mrp/receiving-docs.ts:buildReceivingDocFileName`). Packing slips get a
  `packing-slip_` prefix. A CoC is stored as docType `coa`, and the type is
  classified from the filename only.
- **How Portal serves them**: `GET …/coa` lists the folder's PDFs. Since TASK-2164,
  `?file=<plain .pdf name>` streams one file from that folder with the app-only token
  (`lib/mrp/receiving-doc-file.ts@6e76a5b`, unchanged since `487862b`: no slashes, no leading dot, ≤255 chars;
  404 missing, 502 SharePoint failure). STAFF session only. The Workbench does not call Portal routes.
- **Reporting API**: serves no file or folder. It serves the FM rows that name the raw
  folder (`receipt_raw_material.idrawsupp`, `raw_material_supplier.idraw/idsupplier`,
  `raw_material.rawcode`, `supplier.name`), so the raw folder path can be derived
  exactly. The pack folder's supplier segment `packaging_supplier.suppname_c` is **not
  served** (`idsupp` is), so a pack folder path from the API is an inference.
- **Originals path for the Workbench**: list the derived folder through the GET-only
  Graph client (`integrations/live_source_guard.py`) on the same drive, and cite
  drive item id + eTag. Treat a file as a candidate for the receipt, never as
  its proven document, until a lot or receipt identifier is read from its content.
  Live-unverified until a `verify/live-checks/` spec has a result.
  Whether the Workbench's `SHAREPOINT_DRIVE_ID` value equals Portal's is unverified.
- **Re-read at `6e76a5b` (2026-10-03, #1808 TASK-2216; `487862b` is absent from the
  shallow checkout, the pinned bytes are identical at `6946969`)**: the folder is no longer
  hardcoded. CoAs and packing slips each have a storage category, `RECEIVING_{RAW|PACK}_{COA|SLIP}`
  (`lib/storage/portal-documents.ts:69-81@6e76a5b`, `receivingDocCategory` `:119`), resolved by
  `resolveStorageLocation` from an admin `app_storage_location` row or the code default. The
  defaults are today's folders (`Receiving/Raw/{supplier}/{ref}`, `Receiving/Packaging/{supplier}/{ref}`;
  `safe` and the `_unknown` fallback are unchanged, `coa/route.ts:34-58@6e76a5b`), on
  `SHAREPOINT_DRIVE_ID` unless the row names a drive. An admin row can re-point CoAs and slips to
  different drives/folders; files never move, so `GET …/coa` lists the PDFs of every distinct
  folder (CoA, slip, and the code-default "legacy" folder), and `?file=` looks in the folder its
  name's type points to first (`lib/mrp/receiving-doc-file.ts:44-57,60-72,81-97,115-151@6e76a5b`;
  POST writes to the category folder of the doc type, `coa/route.ts:126-135`). GET still writes
  nothing. For the Workbench the derived path is now **the default only**: `app_storage_location`
  is not among the reporting API's `public.app_*` relations (`EVIDENCE/live/2026-09-30-portal-live-check.json`),
  so a re-pointed receiving folder is invisible to us; a listing of the derived folder that finds
  nothing is "not found at the default", never "no CoA".
- **Re-read at `b4118a3` (2026-10-19, `2b32e55`/`d7a1c95` TASK-3213 slice 1; `6e76a5b` is
  absent from the shallow checkout, the pinned bytes are identical at `727baa8`)**: a POST now
  records an `Attachment` row with its storage finalize job (`recordReceivingDoc`,
  `lib/mrp/receiving-doc-record.ts@b4118a3:75-120`, called `coa/route.ts@b4118a3:146`):
  entityType `RECEIPT_RAW`/`RECEIPT_PACK`, entityId = the receipt id, the receiving storage
  category, its `siteId`, and a null `driveId` for the default drive. The finalize worker
  writes the columns as entity `RECEIPT` with DocumentCategory `COA` (raw), `COC` (pack) or
  `PACKING_LIST` (slip), ItemRef = the material code (none for `_unknown`) and no PartyRef
  (`:35-38,57-70`). The upload refuses a name clash (`onConflict: "fail"`, `coa/route.ts:143`)
  and the finalize kick runs after the 201 (`:159-163`). The POST folder comes from
  `resolveReceivingFolders("raw", tokens)` (`:130-131`; `safe`/`receiptTokens` now `:38-64`),
  and `ReceivingFolder` carries the `siteId` (`receiving-doc-file.ts@b4118a3:27-32,46-52`).
  `GET …/coa` and `?file=` still read the folders, unchanged. So a file uploaded since
  TASK-3213 has a per-receipt record; an older file in the same folder has none, and the folder
  stays per supplier + material. The Workbench reads no `Attachment` row for receiving
  documents, and whether the reporting API serves the table is unverified, so the folder
  listing above is still a candidate, not proof.

## Engineering lessons from the history
- Date-dependent fixtures were "fixed" by moving dates, then rotted again.
  Inject a clock instead.
- MCP responses are serialized twice; keep tool outputs well under 48 KB and page.
- The `operating/cloud_*` OpenAI Responses adapters were **deleted 2026-10-01**
  (third-party data egress). The investigating agent is Claude via MCP; no
  in-product model call exists. `tests/test_egress_retired.py` keeps it so.

## Local-only assets on the operator's Mac (cloud cannot see them)
- `~/Downloads/AO-26005B.pdf`, `~/Downloads/OneDrive_2026-09-26.zip` (six AO scans).

## Live Microsoft sources: Graph mail, SharePoint, BC (read-only adapters, 2026-10-01)
Modules: `integrations/live_source_guard.py` (guard, credentials, audience,
citation parsing), `integrations/graph_read.py` (mail + SharePoint drive items
and content), `integrations/bc_read.py` (BC API v2.0). Evidence so far:
`injected-transport` only (`integrations/tests/test_live_source_adapters.py`);
the credentialed live check runs the probes for `live-read`.
- **Read-only is enforced only by our code**: the Entra app holds write/admin
  roles (incl. Mail.Send). One guarded entry point: GET only, except the
  client-credentials POST to `login.microsoftonline.com/<tenant>/oauth2/v2.0/token`;
  hosts exactly `graph.microsoft.com`, `login.microsoftonline.com`,
  `api.businesscentral.dynamics.com`; per-host path prefixes (Graph
  `/v1.0/users|drives|sites`, BC `/v2.0/`); action-like segments (send, reply,
  forward, createUploadSession, move, copy, delete, `Microsoft.NAV.*`, ...)
  refused, also percent-encoded; no redirects at all; `MC_GO_LIVE_TEAMS_*` URLs
  refused; no Azure Document Intelligence / Storage. All checked before any I/O
  (a refused URL does not even fetch a token).
- **One credential-free exception**: SharePoint bytes come from the item's
  `@microsoft.graph.downloadUrl`, requested only after the audience check, GET
  without Authorization, only on the configured site host (first part of
  `SHAREPOINT_SITE_ID`; unset → downloads refused), byte-capped, then the eTag
  is re-read (a moving item fails) and Graph's `sha256Hash` checked if present.
- **Trap (live audit 2026-10-01)**: Graph silently ignores instance-annotation
  terms in `$select` — `$select=...,@microsoft.graph.downloadUrl` answers 200
  without the URL on PLXCUSTOMERS, so every content read failed. Select
  `content.downloadUrl` instead (the URL still arrives under the
  `@microsoft.graph.downloadUrl` key): `live_source_grammar.drive_item_select`.
- **One Graph request path**: `integrations/graph_evidence.py` (delta readers)
  also goes through `GuardedTransport` + grammar templates (folder/drive delta,
  keyed `mailFolders('<id>')` continuation, attachments, listItem). Its
  mailboxes are object ids (GUID) only; downloads only on the
  `SHAREPOINT_SITE_ID` host (no `*.1drv.com`, no explicit port); a single
  file-attachment GET may return up to 28 MB of base64 JSON. The SharePoint
  tag writer (`operating/sharepoint_tag_execution.py`, `sharepoint_tag_host.py`) was
  deleted: Workbench has no SharePoint write path and no PATCH to Graph.
- **Redesigned contract** (second review round, 9c9c8af): (A) closed request
  grammar `integrations/live_source_grammar.py` — every URL from a fixed
  per-operation template with typed parameters (Graph ids `[A-Za-z0-9_=-]`,
  enumerated well-known mail folders, GUIDs, allowlisted BC entity sets); the
  guard accepts only paths that parse back into a template and refuses any
  segment decoding to `/`, `\`, `%` or `..`. (B) identity sets, most
  restrictive wins: a mailbox's audience is judged over its object id, UPN,
  mail and every SMTP proxy address (a deny on any address applies); mail
  citations use the object id; a folder answers to `/X` and `/X/`. (C)
  post-read provenance: messages must sit in a folder that resolves under the
  resolved user, children under the resolved folder and drive; others are
  dropped and counted. **SharePoint keys are drive-relative server paths**
  (`/Customers/Acme`, not `/Shared Documents/Customers/Acme`).
- **Canonical identity before audience** (review of 7c270ee): the caller's
  mailbox (UPN any case or object id) is resolved by
  `/users/{x}?$select=id,userPrincipalName` to the lower-case UPN; a SharePoint
  folder path or item id is resolved by a metadata-only GET to the server's
  `parentReference.path` + `name`; the audience is judged on that key only and
  reads go by id (`/users/{id}/messages`, `/items/{id}/children`). Caller paths
  with trailing space/dot, backslash, empty or dot segments are refused. No
  grant in the family → no I/O. Limitations: module docstring of
  `live_source_guard.py`.
- **Entitlements** (`plx-live-source-audience/v1`, example
  `examples/live_source_audience.example.json`, default deny, source-mirror
  semantics): `mail:<mailbox>`, `sharepoint:<drive-id>:<path>`,
  `bc:<env>:companies`, `bc:<env>:<company-id>:<entitySet>`, checked before the
  read. `source_acl_required` patterns also need an actor identity (declared in
  `identities`) in the item's own ACL: mailbox owner for mail; SharePoint item
  permissions (group membership not expanded; unreadable or partial → deny);
  BC exposes no record ACL, so required → always withheld.
- **Citations** (re-read on resolve → `current` / `changed_since` +
  `current_citation`; nothing cached, so older content is not retained):
  `graph-mail://<mailbox-object-id>/<immutable-id>@<changeKey>` (all mail reads send
  `Prefer: IdType="ImmutableId"`), `sharepoint://<drive>/<item>@<eTag>[#sha256=<hex>]`,
  `bc://<env>/<company>/<entitySet>/<id>@<@odata.etag>`. Not yet accepted by
  `CaseRuntime.resolve_citation` (still `api://` / `original://` only).
- **Bounds / F8**: timeouts ≤ 60 s, response and content byte caps, page and
  item caps; every listing has `completeness` (`complete`, `truncated`,
  `truncation_reason`, `pages_read`, `withheld_count`). BC numbers are parsed
  as Decimal and stored as exact text.
- Live facts relayed (not yet re-verified by our code): Graph app-only token
  works; site PLXCUSTOMERS drive root has Customers, Customer Documents,
  Documents, ...; `users?$top=1` and `/users/{id}/messages?$top=1` return 200;
  BC `companies` returns 3 companies; `purchaseOrders?$top=1` works (api/v2.0).
- **BC case reads** (2026-10-01, `injected-transport`; tests
  `integrations/tests/test_bc_read_entities.py`): purchaseOrders, purchaseReceipts,
  purchaseInvoices, salesOrders, salesInvoices (+ their lines via the literal nested
  template `<header>(<id>)/<lines>`, lines must name that `documentId`), items, vendors,
  customers. No employees, no `Microsoft.NAV.*`. The free `$filter` is gone: queries come
  only from `live_source_grammar.bc_query` (one equality on an allowlisted number/GUID
  field, `$top` 1..1000, allowlisted `$select`). Lines need both the header and the line
  audience key (`bc:<env>:<company>:purchaseOrderLines`).
- **BC number lookups are leads**: `find_by_number` returns every match
  (`ambiguous`, `status` none_found/single_candidate/ambiguous/truncated/partially_withheld;
  `case_exact`, since BC may compare Code fields ignoring case) and never picks one.
  `integrations/bc_case_evidence.py` (`bc_leads_for_number`) searches PO/SO/invoice number
  fields per company and attaches the caveats: Portal po_number/order_number are rewritten
  to the BC No.; the ship-only bridge overwrote sales "External Document No." for shipments
  posted before PlxSalesBridge@1c5dddb:185-186 (historical; from then the shipment number
  goes to "PLX Shipment No."/"PLX Release No.", table-extension fields 50126/50127 that no
  API page exposes, so the Workbench cannot read them); an invoice External Document No. equal
  to the Portal order_number is historical for bridge-posted invoices (the bridged invoice
  copies the shipment's External Document No., :289-294, :291); identity comes from Portal's
  `app_bc_entity_map`, not a number match.
- **Unverified live (probe confirms)**: `purchaseInvoices.orderNumber`,
  `salesInvoices.orderNumber`, whether api/v2.0 exposes `purchaseReceipts`, top-level
  get-by-id of a line (`purchaseOrderLines(<id>)`, used by line citations) and the
  `$select` allowlists. Run `python3 -m integrations.bc_read probe --case-entities`
  (counts, field names, `select_allowlist_absent`, `filter_fields_absent`, line counts and
  citation round-trips per company index; no values). FM PO number sits in BC
  "Your Reference", which api/v2.0 `purchaseOrders` does not carry.
- **BC queries are guarded too**: `check_request` refuses any BC GET (nextLinks included)
  whose query `live_source_grammar.bc_request_query_ok` rejects: only what `bc_query`
  builds, plus nextLink `aid`/`$skiptoken` (unverified live); get-by-id carries no query;
  `$expand`/`$apply`/`$search`/`$orderby`/`$count`/`$skip` are refused before I/O.
- **A continuation is checked whole where it arrives (p0-f-bc)**: `_same_collection_next`
  accepts only a link the guard sends (`check_request`, as given: no raw space, control
  character or malformed `%` escape) and, for BC (`_bc_continuation_ok`), only the
  request's own query: every option with its value (a dropped `$filter` refuses too),
  plus `aid`, `$skiptoken` and a `$top` no higher than the request's. A refusal is a typed
  `nextlink_refused` (`SourceUnavailable`, detail `nextlink_escaped`), and stored cursors
  get the same check before any request. `BcCapture` fails the unit `nextlink_refused`
  and drops its cursor (the next run restarts it; a `ReadOnlyGuardError` on a cursor GET
  is treated the same), a refused lines continuation marks that document incomplete,
  and mail capture drops the folder's cursors for a fresh round. Before this, one such
  link was stored as the cursor and every later run raised `ReadOnlyGuardError`.

- **Graph continuation allowlist and '+'-aligned comparison** (`live_source_guard`
  `_graph_continuation_ok` / `_same_plus_aligned`): a Graph nextLink query may carry only
  the request's own options plus bounded continuation parameters (`$skiptoken` and the
  like; `$top` never above the request's). Landed in steps: 1fe5deb added the '+'-safe
  compare and the `$top` cap; 0153fae added `_same_plus_aligned` (in main before
  29e41d4); 29e41d4 added the lone-surrogate refusal. A repeated option must be
  byte-identical, or percent-decode equal with no `+`, or keep every raw `+` at the same
  position with the other bytes percent-decoding equal. Only additions are bounded: a
  server may drop options (documented known limitation; post-read provenance and
  audience checks still apply).
- **Live lesson (live run 13:37Z)**: Graph echoes `$filter` in
  the nextLink with `+` for spaces and unescaped `:` (sent as `%3A`). The old rule
  accepted a repeated option only when byte-identical, or decode-equal when it had no
  `+`, and refused any non-identical value containing `+`; that refused every second
  page. `_same_plus_aligned` fixed it without comparing a `+` as a space. Evidence:
  isolated-fixture tests plus the live run; live-unverified beyond that run.
- **HTTP egress gate is a tripwire, not a sandbox** (`scripts/check_http_egress.py`,
  gate entry `http-egress`). Two known escapes (probed with `scan_source`): (1) a
  curl/wget command or tool name built outside the call (`cmd = ["curl", url];
  subprocess.run(cmd)`, or `TOOL = "curl"` at module level) is not seen; a literal
  `subprocess.run(["curl", url])` is flagged. (2) A `lambda`, or code directly in the
  body of an allowlisted function such as `urllib_sender` or `wake_bridge.fire`, is
  trusted by the (path, enclosing function) allowlist; a nested `def` is flagged. Code
  review and `urllib_sender`'s own refusals are the real controls.
- **Graph refusals: one rule, one part shape** (2026-10-18). A Graph read is blocked only under `plx_hub_guard.upstream_blocks(...,
  "graph")` (HTTP 401/403, `GRAPH_BLOCKS`). The mail, Teams and SharePoint list captures
  build its `blocked_upstream` part through one helper, `capture_mail.graph_block`:
  `part` (`<capture>.<call>`: `mail.mailbox_lookup`, `mail.folder_read`, `teams.chats`,
  `sharepoint_list.item_permissions` ...), `source` microsoft-graph, `http_status`,
  `error_code` `missing_permission` (403) or `unauthorized` (401), on 403 the
  `permission` the call's grammar operation needs (an op-to-permission map per capture;
  mail: `graph.user` User.Read.All, `graph.mail_folder` Mail.Read), cause
  `host_misconfiguration`. Graph's own body code (`Authorization_RequestDenied`,
  `ErrorAccessDenied`) is not carried: before this rule mail put it in `error_code` and
  named the folder grant `Mail.Read/ApplicationAccessPolicy`.
- **Partial vs total refusal: mail differs on purpose.** Teams and lists report every
  refused call as a part (one refusal makes the check `blocked`): their application
  permissions (`*.Read.All`, `Sites.Read.All`) are tenant-wide, so a refused call means
  the grant is missing. Mail reports parts only when every bound mailbox (allowlist
  refusals aside) is refused, on its lookup or on every folder (`refused_parts`); a
  refusal of some mailboxes while others read stays a capture gap (`forbidden`,
  `source_error:http_401`). Mail.Read is scoped per mailbox by an Exchange application
  access policy (or RBAC for Applications), so a 403 beside readable mailboxes means the
  grant exists and the policy leaves that mailbox out: a scope edge the capture reports,
  not a host fault; and `case.mail_evidence` / `case.po_sent_chain` excuse unread mail
  only when it is blocked, which a partial refusal must not do. Unverified live: a 401
  on some mailboxes only (a token refused mid-run) is treated as partial as well.

## Mission Control and brain: read guards, approvals, leads (2026-10-01)
Modules: `integrations/plx_hub_guard.py` (closed grammar, config pairs, guarded
retrying transport), `integrations/mc.py` (`MissionControlReadClient` REST,
`MissionControlMcpClient` MCP), `integrations/vmc.py` (`BrainReadClient`; the compat `VmcAgentReadClient`
was removed), `integrations/knowledge_leads.py`, probe
`integrations/mc_brain_probe.py`. Pinned to `docs/reference/mission-control.md` (PLX_MC `1fba0833`) and
`docs/reference/vmc-brain-tools.md` (agentic-swarm `b1117811c`). Evidence: `injected-transport`
(`integrations/tests/test_mc_brain_guard.py`) until the live check runs the probe.
- **MC is the goal-2 governance envelope; reads are free but audited** (each
  call writes `mcp.tool.invoked`). The client allowlists `mission-control.md` §3 rows 1-12 with
  closed argument sets; every other tool is refused before I/O, including
  `mc_suggest_work` (writes a routing session) and `mc_request_approval`,
  with no flag or env override. Goal-2 approval requests will need a per-call
  human approval record naming that exact call (`mission-control.md` §3 row 29 W-A, §8 R1);
  designed in a later piece. REST POST/PATCH routes, `/api/events`, other
  methods and paths are refused.
- **Approvals**: only a signed-in MC admin/owner, different from the requester,
  decides. `approval_decision(task, gate, expected_reason=...)` (a read)
  returns `authorizes: true` only when the gate is `approved`, `requestedBy`
  is our configured operator, the gate reason equals the caller's expected
  action (SHA-256), `decidedBy` is a human email (not `sp_*`/`agent:`), not our
  operator and not the requester, and `decidedAt` is present (not before
  `requestedAt`); otherwise `authorizes: false` with a `reason_code`. Checkouts (refused here) must pass `repo=petralabx/<name>`;
  a 409 `checkout_held` maps to `CheckoutHeld` (not retried).
- **Brain**: GET only, only the 7 `/api/vmc/knowledge/agent/*` routes with
  closed query keys; trading/ingest/cursor paths refused before I/O. Pair rule:
  `BRAIN_BASE_URL`+`BRAIN_API_KEY` (`brn_` required) win, else
  `VMC_API_URL`+`VMC_API_KEY`; the VMC key is never sent to `brain.plxcustomer.io`.
  `/api/vmc/cursor/context` is retired: no client reads it,
  and `UnifiedPlatform.vmc_context` now returns MC compact context (REST twin of
  `mc_get_context`) under the MC recipient mapping; a VMC `project` filter is
  refused rather than widened.
- **Retries**: 429/502/503/504, at most 2 retries, jittered backoff capped at
  4 s (`Retry-After` honoured up to the cap), GETs and read-only MCP calls
  only. `mc_search_knowledge` non-`ok` status is unavailable, not empty
  (cold-start `upstream_error` retried in band).
- **Hosts pinned** to those `mission-control.md` names: MC `mc.plxcustomer.io`; brain
  `missioncontrol.tayloralton.com` or `brain.plxcustomer.io`. Any other
  `MC_BASE_URL`/`VMC_API_URL`/`BRAIN_BASE_URL` is refused.
- **Hard guards**: `MC_GO_LIVE_TEAMS_*` URLs refused, scheme-less values too
  (an origin-only value blocks its whole host); secrets live in `Secret`, scrubbed from errors, absent
  from reprs and probe output.
- **Knowledge = untrusted leads**: `knowledge_leads` labels every hit
  `untrusted_lead`/T3/`business_fact: false` with provenance (route, query,
  read time, upstream id/score/path) and drops upstream `confidence`/
  `classification` (agent text arrives as 0.9 `deterministic_link`). Default-deny
  `plx-knowledge-audience/v1` grants (`namespace:<ns>`, `document:<path>`,
  trailing `*`) trim per actor *before* the per-actor cache; no grant → no I/O.
  Keys with `..`/`.`/empty segments, backslash, `%` or control characters are
  withheld, so traversal never reaches a prefix grant.
  MC search is the default path. No brain write-back.
- **Live probe** (counts/shapes/principal id only):
  `python3 -m integrations.mc_brain_probe --output EVIDENCE/live/mc-brain-probe.json`
  (add `--operator-email cos@petrasoap.com` if `MC_OPERATOR_EMAIL` is unset).
- **Missing inputs (Vince)**: (1) a dedicated Workbench service
  principal in PLX_MC (`sp_mcp_workbench`, `MCP_TOOL_ALLOWLISTS` entry naming
  the read tools + later `mc_request_approval`, its key in
  `plx/prod/mc/mcp-agent-keys/v1`, operator email on `PLX_MC_ALLOWED_USERS`;
  `mission-control.md` §8 R5) — today's key is likely the shared `sp_mcp_cursor` key; (2) a
  `brn_` `BRAIN_API_KEY` at R7 (VMC key stops working for brain routes at R11).
  Item (1) is now a blocker: since 2026-10-11 21:05Z MC answers our key with 401
  `invalid_api_key` (live `mc.brain_probe`, `mc.sources_capture`). INFERENCE: the
  PLX_MC TASK-619 key rotation rollout (`feec59d`) ends by retiring the shared key;
  a 401 alone cannot show which slot was refused. The missing value is a working
  key for `MC_MCP_API_KEY` in `PLX development`.
- MCP wiring (2026-10-01, injected-transport): `plx.vmc_knowledge_search`
  goes through `KnowledgeLeadReader` (`operating.mcp_server.KnowledgeSearchHost`,
  `build_knowledge_reader`). Route: MC `mc_search_knowledge` whenever
  `MC_MCP_API_KEY` is set; then any MC config error (foreign `MC_BASE_URL`,
  missing/invalid operator email, bad `MC_REPO`) is a startup error, never a
  silent brain fallback. Only with the MC key absent: direct brain search from
  `BrainConfig.from_env` (pinned hosts; `--vmc-base-url` only overrides
  `VMC_API_URL`). The chosen route is logged on stderr at startup. Gates, all
  before I/O: `--vmc-broad-knowledge-read-authorized`, the
  `--vmc-recipient-principal` mapping, then default-deny grants from
  `--knowledge-audience` (none given = everyone denied) for the session actor.
  Grant prefixes end at a `/` boundary (`x*` = `x` or `x/...`); the bare kind
  colon (`namespace:*`) is the only `:` boundary; patterns without `:` or that
  can never match (empty body, empty/`.`/`..` segments) are refused at load.
  `plx.vmc_context` has no arguments and reads MC compact context;
  `--vmc-broad-context-read-authorized` is retired (rejected at startup).
  `UnifiedPlatform.vmc_knowledge_search` and its `vmc_client`/
  `vmc_recipient_principals` parameters are deleted. Not yet: no live read of
  this path; `withheld_count` still reveals how many ungranted hits matched.

## process-flow-mapper (Portal skill, read 2026-10-01)
Source: `plx-customer-portal/.claude/skills/process-flow-mapper/` at
`69469696aa8b7ab34b2705c5dd8ee58e61e3a4ca` (TASK-2234, PR 1760): `SKILL.md`,
`references/` (`map-format.md`, `plx-system-map.md`, `evidence-handling.md`,
`worked-example-packaging.md`), `scripts/build_map.py`.
- **Reuse Stages 1-6 only** (frame, investigate by stage, draft the map, owner
  walkthrough, redacted worked examples, data model and crosswalk).
- **Vocabulary adopted** as canonical enums in `operating/process_stage_map.json`
  (`vocabulary`): status `built|partial|policy|decision|missing`; link type
  `fk|loose|json|map|bcfield|gap`; system `FM|Portal|BC|Map`. Our existing labels
  keep their text and map through an explicit alias table; an unmapped label fails
  validation (the validator module retired; the map is data only).
- The read-only importer `operating/process_map_import.py` was deleted (no caller).
- **Stage 7 is out of bounds for every Workbench agent**
  (`references/evidence-handling.md` L81-93): uploading the source bundle to
  SharePoint with `AZURE_*` credentials (`Sites.ReadWrite.All`) and publishing an
  Artifact are production writes. The hard rule forbids them. Workbench SharePoint
  access is GET-only through `integrations/live_source_guard.py`.

## sales-bridge (2026-10-17)

`bc-extensions/plx-receipt-bridge/src/PlxSalesBridge.Codeunit.al`, plx-customer-portal staging, read `4243499..1c5dddb` (the file's log shows one commit, `2743ebc96`; the diff itself was read in full). `PostSalesShipOnly` no longer writes `shipmentNo` to External Document No.: it goes to "PLX Shipment No." (`:185-186`) and the customer PO is preserved; the bridge also reopens a released SO, validates `shipToCode`, and returns `Last Shipping No.`. `PostSalesInvoice` now invoices one posted shipment (`postedShipmentNo`). Mirrored in `operating/reconciliation_rules.py` (docstring and a `@1c5dddb:185-186` citation, test in `test_upstream_citations_name_the_overwrite`); the classes are unchanged because orders shipped earlier keep the overwritten value. The `@8802bee:162-164` citation still names that historical overwrite. Pin moved to `1c5dddb`.

## mc (2026-10-18)

Read `8cd548a..5fdf375` (PLX_MC origin/main, 23 commits), `b1117811c..1751d2e`
(agentic-swarm) and `a5febed..b7b9584` (plx_secondbrain) for every drifted pinned
file; each pin moved to the checkout HEAD. Per path:

- `src/lib/permissions/grants.ts@5fdf375`, `src/lib/permissions/types.ts@5fdf375`
  (`fe1eb39` #339, `bb13b7d` #337, `b719274` #343, `de6f694` #349): `task.cancel` is now
  held by `sp_mcp_cos`, `sp_mcp_ledger` and `sp_mcp_cursor` (cancel only), `task.reopen`
  by `sp_mcp_ledger` only (`grants.ts:88-113,144-149`); session capabilities
  `bucket.comment`, `repo.request`, `skill.submit`, `skill.install` (member) and
  `compliance.reconcile` (admin), held by no service principal (`grants.ts:27-30,46`,
  `types.ts:41-51`). Mirrored in `docs/reference/mission-control.md` (§0 item 3,
  §2 Permission decisions, §3 row 19, §4 Authorization, §6 Principals; the false "no
  other service principal holds either" is replaced). No-op for code: no Workbench
  module models MC capabilities; `integrations/mc_sources.py` captures read-only
  through `CAPTURE_TOOLS`, and MC's read visibility is the project ACL, not a
  capability. Every change broadens a grant; none narrows a Workbench read.
- `docs/modules/permissions/README.md@5fdf375` (`bb13b7d`, `5d0c206` #338, `da0996a`
  #341, `b719274`, `de6f694`, `9630a7c` #351): the grants above, Entra `MC.*` app-role
  admission writing `mc_users` at sign-in, `repo.approve` read from `mc_users` and now
  recorded, `PLX_MC_ALLOWED_USERS` kept as the MCP operator allowlist, session-ended
  401 `signin_required` for `/api/*`. The approval decide route and `requireSessionActor` are
  unchanged since `8cd548a`; TASK-3043 comment ops and TASK-3161 are Entra-session
  only: no-op.
- `docs/FLEET-SECRETS-SOP.md@5fdf375` (`5d0c206`): the allowlist row now says MCP
  operator allowlist plus transitional sign-in.
- `docs/modules/mcp/README.md@5fdf375`, `docs/AGENT-PR-SOP.md@5fdf375`,
  `docs/modules/compliance/README.md@5fdf375`, `tools/plx-mc-mcp/index.ts@5fdf375`
  (`bfae8ee` #352, `b719274`, `de6f694`): the cancel/reopen holders and the STE-lite
  evidence standard (`mc_create_task`/`mc_update_task` descriptions and the
  `mc_complete_task` summary of work a person must verify; warn-only until
  2026-10-24, then 400 `evidence_standard`). `index.ts` changes two tool
  descriptions only; `sp_mcp_workbench` and its six-tool allowlist are unchanged.
  No-op for code: the Workbench sends no
  `labels`/`kind`, so its tasks are out of scope, and it completes nothing; a refusal
  is a definite MC refusal on a consumed authorization.
- Unchanged since `8cd548a` although named in the plan: `scripts/sync-mcp-agent-keys.py`,
  `src/lib/mcp/auth.ts`, `src/lib/permissions/project-acl.ts`,
  `tools/plx-mc-mcp/lib/key-resolution.mjs` (empty `git log 8cd548a..5fdf375`); their
  pins stay.
- `0cca756` #344 TASK-3026 (no pinned file): a shadow-mode `mc_get_context` /
  `mc_search_tasks` / `mc_list_checkouts` read that sees a restricted project only
  through the operator email appends one `authz.shadow_mismatch` row; the answer is
  unchanged. Recorded in the `integrations/mc_sources.py` docstring.
- agentic-swarm `apps/vmc-web/src/data/vmc-crons.json@1751d2e` (`06b5b44b2` TASK-2838):
  `second-brain-lint` registry `enabled: true`, `status: active` (`:439-440`); the
  crontab already ran it; no code reads it.
- plx_secondbrain `eval/contract/README.md@b7b9584` (TASK-2725): versioned operator
  runner `r3-run.sh` and key tiers; the first R3 recording (admin tier, 94/94) exists,
  the scoped tier is not run; no code reads it.

Repo-level `repo_pins` stay at `8cd548a`/`e228067`/`a5febed`: moving them regenerates
`ontology/source_registry.json` and the MCP tool pin, and a Portal checkout is needed to reproduce the registry. `source_registry --check` stays ok.

Pending in `integrations/plx_hub_guard.py` (not edited here):
- `plx_hub_guard.py:153` says `task.cancel` is held only by humans and `sp_mcp_cos`,
  "never the Workbench key". False since `fe1eb39`/`b719274`: `sp_mcp_ledger` and
  `sp_mcp_cursor` hold it, and a shared legacy `MC_MCP_API_KEY` resolves to
  `sp_mcp_cursor`. Behaviour stays fail-closed (the cancel fields are never sent).
- `plx_hub_guard.py:168-169` says only `sp_mcp_cos` adds `task.cancel` to the agent
  bundle. Now `sp_mcp_cos`, `sp_mcp_ledger` and `sp_mcp_cursor` do (`grants.ts:88-113@5fdf375`).
- `UPSTREAM_CODES` (`plx_hub_guard.py:774-779`) lacks `evidence_standard` (TASK-3165,
  from 2026-10-24), so such a refusal reads `other` in diagnostics (fail-closed).
- Same stale cancel claim elsewhere: `operating/goal2_exec.py:177`,
  `integrations/tests/test_mc_write_rules.py:230-231` (comments only).

Optional fold-in not done: `integrations/mc_brain_probe.py:160-164` accepts any string
of 80 characters or fewer as the principal where `mc_sources._PRINCIPAL` requires
`sp_[a-z0-9_]{1,60}`, and it reads `meta.actor` from `mc_get_context`, which over MCP
returns only `meta.filter` (`src/lib/mcp/create-http-server.ts:185@5fdf375`; only
`mc_search_tasks` carries `meta.actor`, `:195-203`), so the live probe's
`service_principal` is always null. The fix adds `mc_search_tasks` to `PROBE_TOOLS`
and changes `verify/live-checks/mc.brain_probe.json` and the probe tests.

## mc-permissions (2026-10-19)

Read every commit since each drifted pin: plx-customer-portal staging up to `f3e3a53`
and PLX_MC origin/main up to `9b4b19f`. Each pin moved to that commit; its `reason`
names the commits. Mirror changes and prose readings are in `docs/reference/portal-code-map.md`
§7.11 and `docs/reference/mission-control.md`. These paths changed upstream with no
Workbench effect (no-op):

- `.claude/skills/process-flow-mapper/SKILL.md@f3e3a53` (`07c736e2`): skill text edits;
  no stage or rule that `operating/process_stage_map.json` mirrors changed.
- `docs/projects/document-metadata-model/SPEC.md@f3e3a53` (`b882f335`): no column or
  rule the doc-metadata mirrors read changed.
- `docs/projects/business-central-erp-modernization/decision-log.md@f3e3a53` (TASK-3215):
  ERP-D-015 (`:25`) is unchanged, so `landed_cost.py` and its stage-map citation stay
  true. New rows ERP-D-046..056 are reported, not coded (cut-over 2026-11-27, ERP-D-055).
- `portal/prisma/schema.prisma@f3e3a53`: additive models and fields only.
- `portal/src/lib/documents/storage-category.ts`, `tag-taxonomy.ts@f3e3a53` (`5e8c4522`
  TASK-3157): `DOCTYPE_TO_STORAGE`, `DOCTYPE_TO_SP` and `resolveDocType` unchanged.
- `portal/src/lib/sharepoint/attachment-metadata.ts@f3e3a53` (`bc144cd`): finalize-queue
  coverage; the `buildAttachmentMetadata` columns the mirror reads are unchanged.
- `portal/src/lib/storage/locations.ts@f3e3a53` (TASK-3157, TASK-3212 `LIST_*` bindings):
  `BENCH_SCRATCH` Staff Scratch/{ref}/{year} unchanged. Re-read at `b4118a3` (TASK-3197): see
  the portal-storage section below.
- `portal/src/lib/storage/attachment-location.ts@f3e3a53`: a restricted library files to
  its own Unfiled folder; `resolveAttachmentLocation` is not mirrored.
- `portal/src/app/api/attachments/route.ts@f3e3a53` (TASK-3075, 3182, 3194, 3061): the
  mirrored column-choice rule is unchanged.
- `portal/src/app/mrp/purchase-orders/[id]/purchase-order-detail.tsx@f3e3a53`
  (TASK-3059, TASK-3111): printer-confirmation UI; the cited upload category is unchanged.
- `portal/src/lib/mrp/upsert-gold-receipt.ts@f3e3a53`: `resolvePurchaseOrderId` unchanged
  (now `:128-141`, called `:192`).
- `portal/src/lib/documents/metadata.ts@f3e3a53`: one transaction with finalize locks; a
  restricted row still reads "Document not found" (`:78`).
- `portal/src/app/api/webhooks/docusign/route.ts`, `portal/src/lib/docusign/docusign.ts@f3e3a53`:
  HMAC unchanged.
- `portal/src/lib/quality/eqms/service.ts`, `hub-query.ts@f3e3a53`: COMPLAINT (CCR) records
  and the QC-010 72-hour obligation; no Workbench module mirrors eQMS.
- `docs/runbooks/WORKBENCH-API.md`, `docs/projects/workbench-readonly-access/PRD.md`,
  `docs/modules/workbench-api/README.md@f3e3a53`: restate the approved 93/3,752 manifest.
- `portal/src/lib/attachments/access.ts@f3e3a53` (`:78-86`, TASK-3075): a staff uploader
  reads their own restricted file. Not adopted: the capture holds no `uploadedById`;
  `operating/tests/test_employee_permissions.py` probes that the refusal stays.
- `operating/portal_kind_columns.py` needs no change: its drifted pins are the paths above,
  and its other pins (`order-detail.tsx`, `document-governance.ts`, `folder-taxonomy.ts`,
  `ship-release.ts` at `19f1d94`) are byte-identical at `f3e3a53`.
- PLX_MC `docs/AGENT-PR-SOP.md`, `docs/modules/compliance/README.md@9b4b19f`
  (TASK-3165/3166/3167/3168): human-verification, visual-evidence, video and
  `filesChanged` gates with warn-until dates; `src/lib/permissions/grants.ts`, `types.ts`,
  `scripts/sync-mcp-agent-keys.py`, `tools/plx-mc-mcp/lib/key-resolution.mjs@9b4b19f`
  (TASK-3225 `sp_mcp_cip_auto_undraft`, the read-only Workbench principal): no Workbench code models MC capabilities.

## portal-storage (2026-10-19)

Read every commit since each drifted pin up to plx-customer-portal staging `b4118a3`
(`6c94159`, `6e76a5b` and `c3ffda3` are absent from the shallow checkout; the pinned bytes
are identical at `727baa8`). Each pin moved to `b4118a3`; its `reason` names the commits.

- Mirrored: `document-types.ts@b4118a3:41` adds `OUTLOOK_PIN: PROJECT("EML")` and
  `entity-routing.ts@b4118a3:236-237` routes an RD_PROJECT `OUTLOOK_PIN` file to Product
  Development `Customers/{customerFolder}/{project}` (TASK-3207). Both are in
  `operating/doc_metadata_taxonomy.py`; `FINANCE_CATEGORIES` is unchanged
  (`tests/test_doc_metadata.py`).
- `portal/src/lib/storage/locations.ts@b4118a3:167-199` (TASK-3197): a folder may use
  400 − 64 (site and library) − 2 − the longest stored name of SharePoint's 400-character path.
  `renderFolderTemplate` renders in full, re-renders with customer and supplier names cut to
  60 characters when too long, and throws `StoragePathTooLongError` if still too long; codes are
  never cut. `BENCH_SCRATCH` is unchanged (`:127-132`). Not mirrored: the Workbench renders no
  folder and files nothing.
- `portal/src/lib/storage/attachment-location.ts@b4118a3`: `resolveAttachmentLocation`
  (`:173-182`) answers 422 on `StoragePathTooLongError`; an RD_PROJECT `OUTLOOK_PIN` upload
  before the library is live goes to the OUTLOOK_PIN row, else the PROJECT_BRIEF folder +
  `Communications/Outlook/<year>`, never the page hints (`:92-94,120-121,142-155`). Not mirrored.
- `portal/src/app/api/attachments/route.ts@b4118a3` (TASK-3218): `readUploadTextFields`
  (`:192`) answers 400 to a file part sent as a text field; the mirrored column-choice rule
  is unchanged.
- `portal/src/lib/documents/metadata.ts@b4118a3`: `DocumentLockedByRetentionLabelError`
  (`:38-51`) also names a copy whose finalize job failed (requeue message); a restricted row
  still reads "Document not found" (`:85`).
- `portal/src/app/api/mrp/receiving/raw/[id]/coa/route.ts`, `lib/mrp/receiving-doc-file.ts`:
  receiving uploads now get an `Attachment` row; see "Receiving originals" above.
- `portal/scripts/etl-silver-to-gold.ts@b4118a3` (`6641353` TASK-3332, MRP-M-265):
  `etlProducts` sets `uom` on INSERT only; staff own it after that. Lines after `:364` moved
  down 2. The `source_unit_contracts.json` contract cites
  `@8da33e4`, still true there.
- Docs: `docs/projects/plx-sharepoint-site/README.md` and `repoint/RUNBOOK.md@b4118a3`,
  `docs/runbooks/WORKBENCH-READONLY-DB-ACCESS.md@b4118a3`.
  The restricted libraries (Finance, Business Central, Staff Scratch) are unchanged
  (README `:86-91`).
