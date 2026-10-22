# PLX Customer Portal code: navigation map for the Workbench

Purpose: what the Portal already owns and which file to open.
It is a map, not a copy: open the cited file before relying on a detail.
Paths are relative to `portal/` unless they start with `scripts/`, `services/`, `docs/`
or `bc-extensions/` (repo root). `S:n` = `portal/prisma/schema.prisma` line n.
FACT = read in code at the cited place. INFERENCE = a reading to verify first.

## 1. Header

| | |
|---|---|
| Repo | `petralabx/plx-customer-portal` (GitHub remote also named `taylorvalton/plx-customer-portal` in its CLAUDE.md) |
| Branch | `staging` (the only agent branch; `master` = production, never touched) |
| Commit | `6c9415973c1990ea9785c9079568aa93d0e37363` "fix(mrp): quote WMS shipments from sales-order ship-to (MRP-M-033) (#1722)", committed 2026-10-01 01:42 UTC |
| Read | 2026-09-30, read-only clone at `~/Downloads/PLX-upstream/plx-customer-portal`; cloud runs: `/home/user/plx-customer-portal` |
| Previous pin | `6c9415973` (first map, 2026-09-30), before that `19079adea`. HEAD is 46 commits ahead of `6c9415973`; see §7. **Re-synced 2026-10-03 to the checkout HEAD `59b0548`** (upstream commit `022e51d` is not in the local checkout; the cited claims were checked against `59b0548` file content, and the confirm-route change is commit `3d4da65`, TASK-2257): confirm route and `manufacturing-orders.ts` (§3.9, §3.7 notes), `eqms/service.ts` (§3.15), `load-order-lifecycle.ts` and `order-lifecycle.ts` (§3.14), schema enums and record-link models (§3.14). Dangling pins `878705b` and `8da33e4` were moved to the byte-identical `c3ffda3`. **Re-synced 2026-10-06 to checkout HEAD `12db891`** after reading each pinned file's diff (shallow checkout: pins at commits it lacks were diffed from `fa46f2d`, where their bytes still match): WORKBENCH-API database errors (§6), Sage watermark (§2), customerVisible default (§3.14), shipment notice on confirm (§3.10), read-only FM order views (§8.1), FM rail links (§3.14), PO send idempotency (`operating/process_stage_map.json`). **Re-synced 2026-10-07 to `48d76e8`** for five pins: process-flow-mapper v2 skill (§9), decision log ERP-D-017/018 Proposed (§4.10), Sage short-copy full reload (§2). **Re-synced 2026-10-08 against staging `549f6f0`** after reading each diff: sourcing-request references and links, PO sourcing route GET-only (§3.5, `fd94c6c`, `ab7711a`, `8ba6803`), MO → sales order line link (§3.8, `568f69c`), ship release DRAFT release, stock flag and ship-to default (§3.10, `b6eaa7a`), books overhead on compounding (§4.3, `e3f8f19`), PO PDF preview vs issue (§8.1, `595243f`); `S:n` after `S:2351` sit 9 lines lower from `fd94c6c` on; **(same `549f6f0`)** FM order page Sage Order tab header (§3.14, `8b08c9e`), formula sheet and sourcing-request RAW document routes (§3.14 ship-release note, `ccab693`, `116a625`) |
| Drift refresh | 2026-10-01 against staging `b46934a7b7be` "fix(mrp): New SO/DSO inherits customer commercial fields and Sales Split defaults to 100% (…TASK-2240) (#1767)" (local checkout `/home/user/plx-customer-portal`, `scripts/check_upstream_drift.py` against `verify/upstream-pins.json`): of the cited files only `portal/prisma/schema.prisma` changed since the `6c9415973` pin (new `ProductSellingPrice`; every `S:n` below re-based to this commit's line numbers). `project-landed-quote.ts` and `quote-picker.ts` also changed since `6c9415973` but §4.1/§3.12 already cite them at `487862b`. Sections citing other files were not re-read; their pins stay at the cited commit and the script flags them if they change |
| Re-verified | 2026-10-01 at staging `487862b121c8` "feat(quotes): link or change PD project on quote detail (TASK-2182) (#1740)": §3.5 receiving documents, §3.12, §3.14, §4.1, §8.1. The cloud checkout `/home/user/plx-customer-portal` was at `878705b`, which lacks `38a31ea` (TASK-2119), `fdaa88f` (TASK-2164 receiving) and `487862b`; those were read from a fresh clone of `staging` |
| Size | `portal/src/lib` ≈1,215 non-test `.ts` files; schema 8,532 lines, 359 models+enums; 158 Prisma migrations |
| Rules files | repo `CLAUDE.md` (governance/MC handshake), `AGENTS.md`, `portal/src/lib/AGENTS.md`; the operator's parent `~/Downloads/CLAUDE.md` (Portal coding rules; some paths stale, see §8.4) |

Never: push, open PRs, comment, call write routes, or run Portal scripts against any DB.
Governed Portal changes need an MC task + `MC-Checkout: dsp_…` (repo `CLAUDE.md` "CI / Mission Control handshake").

## 2. Architecture overview

**Stack.** Next.js 16 App Router + TypeScript, Prisma 7 on PostgreSQL (`@/generated/prisma/client`,
`PrismaPg` adapter), NextAuth v4 (JWT + 2FA, Entra SSO), Tailwind v4/shadcn, Zod 4, Resend, Vercel
hosting (web) + AWS (RDS, EC2 agents, Lambda pinger). Business logic lives in `src/lib/**` AND in
`src/app/**` route files and server components (e.g. `app/api/mrp/projected-revenue/queries.ts`,
`app/mrp/assembly/assembly-queries.ts`, `app/api/mrp/batching/create/route.ts`). Root `src/lib/*.ts`
files are deprecated shims (`api-auth.ts` → `lib/auth/api-auth.ts`; `db` → `lib/db/db.ts`;
`microsoft-graph.ts` → `lib/m365`; `utils.ts` retired TASK-2133).

**Databases** (single source of truth `scripts/db-targets.json`, runbook `docs/runbooks/DATABASE-TARGETING.md`):

| Host | Role | Fed by |
|---|---|---|
| `plx-postgres-uat` | What `https://staging.plxcustomer.io` reads/writes (`DATABASE_URL`). Tester data, Portal UI entries, BC outbox drained by the staging app | FM: on-demand only (nightly task Disabled); Sage staging→UAT daily |
| `plx-postgres-staging` | Dev/agent DB; FM bronze sync every ~30 min; Sage every 10 min; gold ETL | Ricardo-workstation Windows tasks (below) |
| `plx-postgres-staging-reporting` | Read-only RDS replica **of staging, not UAT**. Serves the Workbench API (`wb_api`) and Portal `REPORTING_DATABASE_URL` (`lib/db/db-reporting.ts`) | async replication |
| `plx-postgres` | production | never |

=> **The reporting API never shows rows typed into the staging Portal UI (UAT).** It shows the staging
primary: FM/Sage bronze, silver views, and `app_*` gold built by ETL + dev work (FACT:
`docs/runbooks/WORKBENCH-API.md` "No fallback… read UAT, not the replica"; DATABASE-TARGETING.md:19).

**Medallion layers** (`docs/modules/fm-migration/README.md`, `lib/fm-migration/medallion-lineage.ts`):
- Bronze FM: `public.<snake_case>` (`public.order_entry`, `assembly_order`, `formula_item`, `batch`,
  `generated_purchase_order`, `receipt_raw_material`…). Legacy FM names (`oe`, `receiptraw`, `receiptpack`,
  `po`, …) are compat views (`scripts/migrations/20260327_2205_fm_snake_case_compat.sql:52-59`).
  Portal writes FM bronze directly with raw SQL (MAX(id)+1 ids) for formulas, AOs, batches and raw receipts. Since staging `38cd3f3`
  (2026-10-12) it no longer writes FM for new MAKE orders (`898e084` TASK-2638), Generate PO (`359e2f0` TASK-2651), pack receipts
  (`5cade1b` TASK-2652) or the FG output of portal-born AOs (`eb14fe3` TASK-2643): those live only in `app_*` rows (§3.2, §3.5, §3.8).
- Bronze Sage 300: schema `sage.*` (`scripts/migrations/20260423_create_sage_schema.sql`); loader
  `scripts/sage_staging_sync.py` (ARCUS→`sage.ar_customer`, ICITEM→`ic_item`, OEORDH→`oe_order_header`…;
  watermark `audtdate*1e8+audttime` since `cfcc7db` #1861 TASK-2260 (re-read at `12db891`): the old `*1e6` let an eight-digit
  HHMMSSss audit time spill into the date, so an incremental run could skip rows; the loader now streams one ordered cursor and replays the
  boundary (`>=`). Sage rows loaded before the operator reload (`docs/runbooks/sage-invoice-reload.md`, not evidence it ran) can be missing.
  The Workbench reads Sage by the API's `audtdate` `since` and never computes the watermark: no Workbench change.
  Since `b83b1eb7` #1869 TASK-2385 (re-read at `48d76e8`, `sage_staging_sync.py:109-115,751-759`) a short copy triggers a full reload:
  when Sage has more rows than the copy by more than the rows at or after the watermark, older rows were skipped or deleted and the
  table is fully reloaded. A reloaded `sage.*` table can show rows whose `audtdate` is older than an earlier `since` read; a
  Workbench incremental read does not see them until a full re-read. No Workbench code change.
  **Re-read at `f3e3a53`:** a reload keeps grants (TASK-2969, `sage_staging_sync.py:32-36@f3e3a53`): a table
  with the same columns and key is TRUNCATEd in place; otherwise the column ACL is saved, replayed column by column after DROP/CREATE,
  and the run then calls `provision.mjs --regrant` and logs `SAGE_GRANT_ALERT` (`:687`) when `workbench_ro` cannot read an allowlisted
  sage relation. `workbench_manifest_blocker` (`:1263-1277`) reports the regrant as refused while the manifest has `pendingDiscovery`
  relations, which it has at this pin (§6 Catalog). FACT: the saved-ACL replay does not depend on that regrant. INFERENCE: a reload
  cannot add a grant for the pending sage relations; whether a live reload lost one is unrecorded. No Workbench code change.
  **Re-read at `c09de5d` (TASK-3369 `26cf705`, `0acbd28`):** a full load keeps a composite key
  (`full_load`, `sage_staging_sync.py@c09de5d:916-982`): the table keeps its `_sage_pk` text column (key values joined by `|`) and gets
  `UNIQUE ("_sage_pk")` as `uq_<dest>_sage_pk` after the rows are in; before, a reload dropped `_sage_pk`, and an empty table reloaded on
  every run without a key. A truncate-in-place reload drops that UNIQUE first and re-adds it after the rows (`:932-938`, `drop_sage_pk_unique`
  `:623-630`), so duplicate source keys keep their rows and fall back to the no-key truncate reload; column grants are untouched. In the
  read-only provisioner `termscode` passes `SAGE_COLUMN_DENY` only as `sage.ap_term.termscode` (`SAGE_APPROVED_COLUMNS`,
  `scripts/sql/workbench-readonly/lib.mjs@c09de5d:454-458`, Vince 2026-10-19, `2cfc138`), the pagination key of `sage.ap_term`;
  `termscode` stays denied on every other relation and `sage.ap_vendor_remit` stays denied (`f13bcbc`). The Workbench binds the API's
  served columns and never pages by `_sage_pk`: no Workbench code change, nothing new bound.
  **Re-read at `0323e19c` (TASK-3390 `0252da41`):** the in-place reload no longer looks the key up by
  its `uq_<dest>_sage_pk` name: `sage_pk_unique_keys` (`sage_staging_sync.py@0323e19c:621-656`) finds every unique key on `_sage_pk`
  alone by column (a unique constraint or a plain unique index, any name), `drop_sage_pk_keys` (`:658-679`) drops them before the
  truncate (`full_load:1014`) and `restore_sage_pk_keys` (`:681-704`) re-adds each under its own name and definition after the rows
  (`:1051`); `add_sage_pk_unique` (`:600-619`) counts a key of any name, so no second key is added. During the reload the read API sees
  no key for that table. Column grants are still untouched. Nothing new is bound for the sync itself; `sage.ap_vendor_remit`, which
  this key makes pageable, is bound because the manifest at this pin serves it (§6 Catalog).
- Silver: views in schema `silver` (`scripts/migrations/20260402_create_silver_schema.sql`, `20260403_enhance_silver_views.sql`, … `20260929230000_silver_formula_items_corrected_percentage.sql`).
- Gold: Prisma `app_*` tables (all in `public`; the `plx_core/plx_production/plx_warehouse/plx_quality`
  "namespaces" in `S:10-32` are comments only — no `@@schema`, no such Postgres schemas).
- BC integration tables are raw SQL, not Prisma (`scripts/migrations/20260615_create_app_bc_integration.sql`).

**FM sync** (`docs/runbooks/FM-INCREMENTAL-REFRESH-SCHEDULE.md`): Windows task `PLX-FM-Refresh-Staging`
(~30 min) → `scripts/run-fm-incremental-refresh.ps1`: (1) `scripts/fm_staging_sync.py` (ODBC → bronze,
incremental on `auditTimestampModified`; since `acc0c53` the `batch`/`batch_item` new-row sweep is capped below 9,000,000 like
`order_entry*` below 900000, `fm_staging_sync.py:145-162,551@acc0c53`, so Portal test batches never hide new FM rows), (2) `portal/scripts/etl-silver-to-gold.ts` (24 `etl*`
functions), (3) `portal/scripts/mirror-fm-pos-to-gold.mjs --apply`, (4) `portal/scripts/etl-realized-ao-fg-putaway.ts`.
Other: `portal/scripts/etl-wms-silver-to-gold.ts` (lots), `etl-sage-distribution-gold.mjs` (Sage SOs/items/bins → gold).
**Re-read at `f3e3a53` (TASK-3021):** `PORTAL_WRITE_ONCE_COLUMNS` (`fm_staging_sync.py:131-136@f3e3a53`)
names `batch.testedby`, `datetested`, `checkedby`, `datechecked` and `timestampreleased`; `upsert_update_set` (`:139-144`) writes
`COALESCE(existing, FM)` for them, so the FM sync only fills a NULL and a value the Portal wrote from the signed-in user stays. FACT:
those five `public.batch` columns can hold a Portal-born value. `ontology/schema/domains/manufacturing.json` still labels the table
`fm_bronze`; residual: the provenance label names the table's loader, not the writer of these five columns. No value or rule changes.

**External systems.**
- **Business Central** (target ERP, ERP-D-001): Portal-owned outbox→drain→serializer (ERP-D-002),
  `lib/business-central/**`, AL extension `bc-extensions/plx-receipt-bridge` (codeunits 50100-50124).
  Two companies/books: My Company = manufacturing ("PLX"), PLX Distribution ("Petra"), PLX Consol (ERP-D-012).
  Decision log: `docs/projects/business-central-erp-modernization/decision-log.md`.
- **FileMaker**: legacy system of record for formulas/AOs/batches/raw receipts, still live (bronze sync). Being retired: for orders,
  POs, pack receipts and portal-born AO lots the Portal record is now the only one (§2 bronze note); FM rows stay authoritative only
  for legacy records and where the Portal still reads them (FM order, OE line and AO junction reads, FM PO lines, FG activation's FM fp mint, `710fc7b` TASK-2709).
- **Sage 300**: being discontinued; setup-only + dual-run (ERP-D-003); live source for Sage OE orders, price lists, customers.
- **SharePoint/Graph**: `lib/m365/*` (`auth.ts` app-only client, `sharepoint.ts`, `outlook.ts` send-only,
  `retry.ts` `withRetry` 3 attempts 2^n×1000 ms, no Retry-After; `circuit-breaker.ts`). Site `petrasoap.sharepoint.com/sites/PLXCUSTOMERS`.
- **DocuSign**: `lib/docusign/docusign.ts` (JWT), templates `docusign-templates.ts:8`; webhook HMAC `app/api/webhooks/docusign/route.ts`. **Re-read at `6e76a5b` (#1808, TASK-2216):** the signed PDF's folder is no longer the hardcoded `Customers/{companyName}/Contracts`; it is `resolveStorageLocation(signedFormCategory(formType), {customer, customerCode})` (`route.ts:161-172@6e76a5b`), category `SIGNED_CREDIT_APPLICATION` for slug `customer-profile-credit-app` and `SIGNED_FORM` otherwise, both defaulting to `Customers/{customer}/Contracts` on the default drive (`lib/storage/portal-documents.ts:56-60,103-105@6e76a5b`). An admin `app_storage_location` row (not served by the reporting API) can move either to another drive/folder, so the default path is a candidate location for a signed original, not a guarantee. The webhook is POST-only; no new write-on-GET. **Re-read at `38cd3f3` (`3718061` TASK-2215):** the signed PDF is named `YYYY-MM-DD_AGR_<customer>_<form>.pdf` (`lib/documents/signed-file.ts`), never overwriting (a taken name gets `-2`, `-3`), and its PLX columns are written after upload; a failed column write is recorded as `metadataWriteFailed` on the form request. `getEnvelopeSigners` (`docusign.ts`, `5f7b299` TASK-2220) reads an envelope's signers (GET) to name the approver on an approval record (§3.17). **Re-read at `f3e3a53`:** the HMAC check is unchanged (`route.ts:21-63@f3e3a53`). A permanent storage failure answers 200 only once its error row is written, else 500 so DocuSign retries (TASK-3195, `:118-127`). The signed PDF's row takes `securityClass: portalDocumentSecurityClass(location.category)` (`:360`; `lib/storage/portal-documents.ts:119-127`), so a `SIGNED_CREDIT_APPLICATION` original is FINANCE class. `docusign.ts` adds `DocuSignApiError` and clears its token cache on a 401. No Workbench change: the Workbench reads no `security_class` and calls no DocuSign API. **Re-read at `0323e19c` (`6f197d93` TASK-3308):** `storeSignedEnvelope` now links a form request that is already completed without a signed document (the forms route can complete a DocuSign-backed request first): it sets `signedDocumentUrl` only where it is still null (`route.ts@0323e19c:442-453`, `db.formRequest.updateMany`, on this delivery or a redelivery), never replaces a URL the request has, and never notifies (the completion already did); the SharePoint list sync also runs for such a link (`:455-462`). No Workbench change: the Workbench calls no DocuSign API and holds no form-request URL.
- **AWS**: RDS; Secrets Manager only via ops scripts (`staging/ec2-secrets`, `plx/uat/postgres/admin`, `plx/staging/workbench-api/*`);
  Lambda `scripts/aws/bc-cron-ping/` paces BC crons on staging/UAT; `@aws-sdk/client-s3` declared but unused.
- **Freightcom/FedEx/UPS** rating: `lib/wms/carrier/**` (`providers.ts:129`).
- **Mission Control / VMC brain**: see 01/02 context files; Portal consumes the brain read-only via `lib/knowledge/service/vmc-client.ts`.

## 3. Domain map

Legend per entity: **Model** (`@@map` table, `S:line`) · identity keys · owners (path:function) ·
FM / Sage / BC mapping · **API relation(s)** exposed to the Workbench (`—` = not in the 103-relation manifest).
Money/quantities in gold are `Float` (not Decimal) unless noted; they arrive as float-formatted strings.

### 3.1 Customer
- **Model** `Customer` → `app_customer` (S:1299). Unique: `id` (cuid or `fm-cust-N` for FM-migrated), `userId`
  (one portal login per customer), `code` (PLX internal), `externalId`. `erpCode` = Sage IDCUST = BC customer No.
  (NOT unique); `billToErpCode`; `legacySageIdcust/Idnatacct` (audit); `priceListCode` (Sage ARCUS.priclist);
  `businessLine`; `currency`; `deletedAt` (soft delete). `externalId` forms: `fm-cust-{FM id}`, `local-cust-{cuid}`,
  `sage-cust-{IDCUST}`, `cm-012-stub:` (national-account stubs).
- Others: `CustomerContact` (S:1421), `CustomerLocation` → `app_customer_location` (S:1440; `bcShipToCode`,
  `legacySageShipToCode`, `isDefault`), `CustomerOverride` (S:4953; per-field override of FM `public.customer`, key
  `fmCustomerId`), `CustomerLocal` (S:4995; Portal-only R&D customers, no FK to Customer).
- **Owners**: `lib/customers/read.ts:loadCustomerHeader:290` (CustomerLocal first, else FM bronze + override),
  `write.ts:saveCustomer:82`, `anchor.ts:ensureCustomerAnchor:129` (creates gold row on first FK need),
  `erp-identity.ts` (`applyErpCodePromotion:35`, `resolveBillToErpCode:66`), `legacy-refs.ts:filemakerIdFromExternalId:36`,
  `ship-to-seed.ts:deriveBcShipToCode:165`.
- **BC**: `business-central/serializers.ts:resolveCustomerBcNumber:169` (= erp_code, ≤20 chars); skipped without erp_code;
  Customer Posting Group set by AL codeunit 50113 (USD→ARUSD else ARDEF).
- **API**: `public.app_customer`, `public.app_customer_location`; FM `public.customer`, `silver.customers`; Sage `sage.ar_customer`, `sage.ar_ship_to`.

### 3.2 Sales order (order entry) and line
- **Model** `SalesOrder` → `app_sales_order` (S:3662). Unique `orderNumber`. `status` is a free String (not an
  enum): code vocabulary `lib/sales-order/types.ts:14` = DRAFT, CONFIRMED, IN_PRODUCTION, READY, READY_TO_SHIP,
  PARTIALLY_SHIPPED, SHIPPED, COMPLETE, CANCELLED (+ legacy "Open"; FM ETL writes FM status text, default "OPEN").
  Transitions `lib/sales-order/status.ts` (`SO_TRANSITIONS:35`, `normalizeSalesOrderStatus:8` Open→CONFIRMED,
  READY→READY_TO_SHIP). **Edit lock (re-read at `eb964b9`, #1974 TASK-2517):** `canEditSalesOrderUntilShipped:28`
  is false for a blank status or one that normalizes into `EDIT_LOCKED:21` (PARTIALLY_SHIPPED, SHIPPED, COMPLETE, CANCELLED);
  every other status, FM status text included, may still be edited. Ship confirm still never advances SO status. **Close-out (re-read at `38cd3f3`,
  `999dac8` TASK-2396):** PARTIALLY_SHIPPED, SHIPPED and COMPLETE are reached only through `evaluateSalesOrderFulfillment` (`lib/sales-order/closeout.ts`),
  run by the STAFF route `POST /api/mrp/sales-orders/[id]/closeout` (`restate`, `short_close`, `refresh_status`; `GET` only reads the restate offer):
  PARTIALLY_SHIPPED once anything shipped and a line has an open remainder, SHIPPED when every line is shipped or short-closed, COMPLETE when it is also
  invoiced (BC finance status INVOICED, PARTIALLY_PAID or PAID; the deposit clause is not applicable until ERP-D-018), never backwards, never from DRAFT or
  CANCELLED (`SO_TRANSITIONS` gained those moves, `status.ts:36-52`; the order PATCH still takes only CONFIRMED or CANCELLED). Restate-at-yield (line quantity = Σ realized MO `yieldActual`, same UoM) and per-line
  short-close are audited and kept in `customFields.closeout` (`lines[lineId].originalQuantity/shortClosedQty`, `history[]`, `bcLineSyncFrozen`); a
  closed-out order is no longer re-serialized to BC (`BC_LINE_SYNC_FROZEN_SQL`). So a line's ordered quantity can be its restated yield, and a shortfall
  on a short-closed line is a closed remainder, not open demand (the Workbench does not read `closeout` yet).
  `releaseRequired` (`release_required`, default true, `1e40b64` TASK-2400, ERP-D-017): drives the auto release at confirm and BC `PLX Release Required`;
  the shipment confirm gate does not read it (a MAKE shipment still needs a RELEASED release, §3.10).
  `paymentStatus` (PENDING/INVOICED/PAID) is never written by code. `fulfillmentMode` MAKE|DISTRIBUTE,
  `fulfillmentOwnership` PETRA|CUSTOMER, `accountingLocationCode` (BC location 11/15/22/VMI/VMICON/VMIGR/3PL),
  `customerLocationId`, `shipToSnapshot` (JSON), `poReference`, `customFields` (JSON).
- **`customFields` keys** (FACT): `fmId` (FM `order_entry.id` — the OE↔SO bridge), `erpCode`, `source`
  (`fm-oe-ensure` | `gold-so-bridge` | `sage.oe_order_header`), `fmOeBridge`, `sageOrdUniq`, `sageSourceOrdUniq`,
  `sageOrdNumber`, `sageCustomer/Location/Salesperson`, `fmOeNumber`/`renumberedTo`/`renumberSource` (BC renumber),
  totals/commercial/tax/sales-split/FX snapshots (`lib/sales-order/{totals,commercial,tax-profile,sales-split,fx-rates,optional-fields}.ts`),
  `convertedFromQuote…`. `custPoCode` exists ONLY from legacy `scripts/migrate-fm-to-portal.sql:365`; no runtime code reads or writes it.
- **Number series**: MAKE create `SO-YYYY-####` (`service.ts:generateSalesOrderNumber:100`); FM-ensure `SO100000+`
  (`lib/mrp/sales-order-number-series.ts:allocateNextSalesOrderNumber:106`, advisory lock); DISTRIBUTE `DSO100000+`
  (`:118`); legacy `SO-FM-{id}`. `order_number` is NOT immutable: `business-central/align-portal-so-number.ts:alignPortalSoNumberToBc:21`
  rewrites it to the BC document No. after drain.
- **Line** `SalesOrderLine` → `app_sales_order_line` (S:3718): `productId` FK (required), `quantity`, `unitPrice`,
  `uom` (free string, no conversion), `totalPrice`, `lineSource` STOCK|MAKE, `warehouseId`. Line ids are only partly stable: since `f17cb54`
  TASK-2703 `updateSalesOrderDraft` keeps a resubmitted line whose content is unchanged (`planLineReplacement`, matched on product, description,
  quantity, round2 price, uom, notes, source, warehouse) with its id, recreates changed lines under new ids, and refuses with 409 `ORDER_LINES_RELEASED`
  to delete a line a DRAFT or RELEASED ship release references.
- **Owners**: `lib/sales-order/service.ts` — `createSalesOrder:224` (status DRAFT / CONFIRMED (MAKE) / READY_TO_SHIP
  (DISTRIBUTE); MAKE+confirmed → since `898e084` TASK-2638 no FM order_entry: `autoReleaseBestEffort` cuts one RELEASED single-destination ship
  release keyed on the portal order and line ids (`lib/ship-release/auto-release.ts`, idempotent, skipped when `releaseRequired` is off, no ship-to,
  no positive line or a live release exists; audited `AUTO_RELEASE` / `AUTO_RELEASE_FAILED`), and an order confirmation email is queued after commit),
  `updateSalesOrderStatus:523` (same at CONFIRMED / READY_TO_SHIP),
  `updateSalesOrderHeader:598` and `updateSalesOrderDraft:677` (since `eb964b9` both take content edits until the
  first shipment or cancel, `canEditSalesOrderUntilShipped`, else HTTP 409 `{error: "Order edits are locked after the
  first shipment or cancel", code: "ORDER_NOT_EDITABLE"}`; before, DRAFT only with a 400), `validateSalesOrderLines:114`,
  `buildBcReadiness:899`; `lib/sales-order/convert.ts:convertAcceptedQuote:179` (quote→SO, does not set poReference);
  `lib/sales-order/fm-oe-bridge-ids.ts` (`PORTAL_OE_ID_BASE = 900_000`: FM order_entry ids ≥900000 were created by the Portal).
- **FM bridges** (both write): `lib/wms/ensure-sales-order-from-fm-oe.ts:ensureSalesOrderFromFmOe:402` (FM OE → gold SO;
  picks by `fmId` → `preferSalesOrderId` → unlinked Portal SO matching PO/ERP (`findUnlinkedPortalSo:87`) → creates
  SO100000+ CONFIRMED with `poReference = custpocode`, `customFields {fmId, erpCode, source:"fm-oe-ensure"}`; on link,
  stamps fmId, fills empty PO/required date, flips DRAFT/OPEN→CONFIRMED; re-prices lines from Sage; `isHollowStubOrderNumber:35`
  = 1–6 digit order numbers are untrusted custpocode stubs). Reverse: `lib/wms/ensure-fm-oe-from-sales-order.ts:ensureFmOeFromSalesOrder:153`
  inserts FM `order_entry`/`order_entry_item` (id ≥900000) with `erpcode = order_number`, **`custpocode = po_reference ?? order_number`**,
  and overwrites `customFields.source = "gold-so-bridge"`. **Since `898e084` TASK-2638 confirm no longer calls it**: a Portal MAKE order confirmed
  at or after staging `38cd3f3` has no FM twin, no `fmId` and keeps its own `source`; its MO link is `ManufacturingOrder.salesOrderLineId` and its
  releases and shipments key on portal ids (§3.10). Orders bridged before keep their `fmId` twin, which the Portal still reads.
- **Sage**: `portal/scripts/etl-sage-distribution-gold.mjs:~1440-1550` upserts gold SOs keyed by `custom_fields->>'sageOrdUniq'`,
  `po_reference = COALESCE(sage ponumber, po_reference)`, `customFields.source = 'sage.oe_order_header'`.
  `lib/wms/sage-ship-queue.ts`: `isSageBackedSalesOrder:32` (sageOrdUniq | sageSourceOrdUniq | source=='sage.oe_order_header'),
  `isFmOnlySalesOrder:41` (fmId and NOT Sage-backed), label `sageOrdNumber ?? order_number`.
- **BC**: `serializers.ts` sales-order payload `externalDocumentNumber = po_reference` (≤35, `:1157`; `@4243499:1221`), `yourReference`/`plxMoCode`
  = linked MO codes; selected by `outbox-producer.ts:395-436` (`@4243499:442-480`; status not DRAFT/CANCELLED, not closed out
  (`BC_LINE_SYNC_FROZEN_SQL`, TASK-2396), customer LINKED, ≥1 LINKED item;
  **only LINKED lines serialized**, `:466-472`); distribution book uses `{id}::distribution` map keys (`book-routing.ts:102`).
- **API**: `public.app_sales_order`, `public.app_sales_order_line`; FM `public.order_entry`, `order_entry_item`,
  `order_entry_item_assembly`, `order_entry_item_batch`, `silver.order_entries`, `silver.order_entry_items`;
  Sage `sage.oe_order_header`, `sage.oe_order_detail`.

### 3.3 Customer PO
Every field that can hold the customer's PO (FACT):

| Field | Written by |
|---|---|
| `app_sales_order.po_reference` | Order entry `POST /api/mrp/sales-orders` → `createSalesOrder` (trimmed, `service.ts:476`); PATCH `updateSalesOrderStatus:552`/`updateSalesOrderHeader:655` (untrimmed; until the first shipment or cancel, else 409 `ORDER_NOT_EDITABLE`), `updateSalesOrderDraft:868` (trimmed; same lock) (lines at `eb964b9`); FM-ensure = `order_entry.custpocode` on create, on link only if empty (`ensure-sales-order-from-fm-oe.ts:519,544`); Sage ETL = `oe_order_header.ponumber` (COALESCE). Legacy `migrate-fm-to-portal.sql` put FM **`reference`** (free text, can be a person's name) here. |
| FM `order_entry.custpocode` | FM users; Portal bridge writes `po_reference ?? order_number` (so it can be the Portal SO number, not a PO). Not unique (values like `NA`). |
| `app_sales_order.custom_fields.custPoCode` | Legacy SQL only. |
| `sage.oe_order_header.ponumber` | Sage. |
| `app_shipment_order.order_number` | builder input (FM custpocode or Sage ord number) — not in API. |
| BC sales order `externalDocumentNumber` | = po_reference. (Historical, before TASK-2271: a Portal-posted BC sales **invoice** `externalDocumentNumber` = order_number, `serializers.ts:1049`; BC alone raises invoices now, from the SO, whose External Document No. the ship-only post overwrites with the shipment No.) |

No unique index and no normalization at write beyond trim. **Since `40f7802` (#1910 TASK-2401, re-read 2026-10-07) a customer PO is unique per customer, not across customers** (`lib/orders/customer-po.ts`): `normalizePoReference:17` (trim, collapse inner whitespace, upper case; SQL twin `PO_REFERENCE_NORM_SQL:23`) is the comparison; `findCustomerPoReuse:51` lists other non-CANCELLED orders of the same customer with the same normalized PO, and sales-order create/PATCH return it as a non-blocking `warnings` entry (`PO_REUSED_BY_CUSTOMER`, or `PO_REUSE_CHECK_FAILED` when the check itself fails; the save never fails). No DB constraint; a unique `(customer, po_reference)` index and a required PO on MAKE orders are deferred. Portal treats `po_reference` as an order identity only with the customer: `lib/orders/order-identity.ts:resolveOrderByNumber:98`. Workbench mirror: `ontology/schema/identifiers.json` `customer_po` (`identity_scope` per customer, `exact_compare` `normalized`), read by `ontology/identifiers.identity_key`; `integrations/source_mirror.py` `resolve` gives each `po_reference`/`custpocode`/`ponumber` match its customer-scoped `identity`.

### 3.4 Order identity (canonical key = gold `SalesOrder.orderNumber`)
`lib/orders/order-identity.ts` (re-read at `40f7802`, #1910 TASK-2401) — all resolvers return `CanonicalOrder {salesOrderId, orderNumber, customerId, customerName, matchedBy}`; `LIMIT 1` without ORDER BY except the PO step:
- `resolveOrderByNumber(q, raw, {customerId}):98`: exact `order_number = raw` → `"orderNumber"`; else, **only when a `customerId` is given**, `customer_id = $1 AND PO_REFERENCE_NORM_SQL = normalizePoReference(raw) AND status <> 'CANCELLED' ORDER BY order_number LIMIT 2` (`:129-138`): exactly one row → `"poReference"`; two (the customer reused the PO) → null, never a guess; no customer → the PO step is skipped. A PO alone never matches across customers.
- `resolvePortalCustomerIdFromBc(q, bcCustomerId):158` (new): `app_bc_entity_map` `entity_type='customer'`, `source_system='portal'`, `status='LINKED'` → the Portal `app_customer.id`, else null.
- `resolveOrderFromBcInvoiceId(q, id):183`: `app_bc_entity_map` (`entity_type IN ('salesInvoice','salesOrder')`, `source_system='portal'`, `bc_system_id=id`) → `"entityMap"`; else `bc_invoice_snapshot.external_document_number` (with `customer_bc_system_id`, mapped by `resolvePortalCustomerIdFromBc`) → `resolveOrderByNumber(..., {customerId})` → `"externalDocNumber"` (`:205-221`): the PO fallback is scoped to the invoice's customer.
- `resolveOrderFromFmOrderId(q, fmId):233`: `custom_fields->>'fmId'` → `"fmId"` (no hollow-stub filter).
- Callers: `lib/orders/prepayment-sync.ts:60`, `app/mrp/orders/[id]/page.tsx:80`, `app/api/mrp/orders/[id]/ownership/route.ts:17`.

### 3.5 Purchase order, supplier, requisition, receipt
- **Supplier** → `app_supplier` (S:1482): unique `code`, `externalId`; FM id as bare number or `fm-supp-{n}`
  (`lib/mrp/fm-supplier-id.ts:parseFmSupplierId:5`, `supplierIdsMatch:15`); `incoterms`, `currency`, `deletedAt`.
  `SupplierProduct` → `app_supplier_product` (S:1603; unique `[supplierId, productId]`; `unitCost`, `uomPerSellingUnit`,
  `systemCost` $/kg, `deliveryCost`, `minOrderQty`, `incoterms`, `countryOfOrigin`, `isPreferred`); `SupplierProductTier`
  (S:1647; price breaks, writer `lib/sourcing/vendor-item-card.ts:writeVendorItemCard:66`); FM tiers `public.tierpricing`.
- **PurchaseRequisition** → `app_purchase_requisition` (S:3499; `prNumber` `PR-YYYYMMDD-NNNN`, implemented twice:
  `lib/services/reorder-management.ts` (UTC) and `app/api/mrp/generate-po/requisition/route.ts:generatePRNumber:52` (local)); `PRLineItem`.
- **Sourcing request** `AssemblySourcingRequest` → `app_sourcing_request` (`schema.prisma@fd94c6c:2342`; not served, generated
  `not_served` type). It links a project (`projectId` = the PD project key; `projectNo` = the project number vendors see) and a
  customer (`customerId`, `customerName`). Since `ab7711a` #1894 TASK-2419 a PACKAGING request created from an Assembly BOM line
  stores `projectNo`, the customer, certifications and pack spec from the project's sourcing snapshot (`resolvePdSpineProject`:
  local, bronze, then gold), or null when the snapshot read fails (the 201 carries a warning); it never stores the project key as
  `projectNo` (`lib/sourcing/requests.ts@ab7711a:assemblyProjectSnapshot:900,createAssemblySourcingRequest:925`); a duplicate
  active request is CONFLICT 409. `fd94c6c` #1885 TASK-2451 adds optional formula and final-product references as snapshot labels
  with no FK (`formulaId/Code/Name`, `finalProductId/Code/Name`, `schema.prisma@fd94c6c:2354-2362`): an id there may name a
  formulation that was since de-duplicated, so it is a label, never a link. Since `8ba6803` #1915 TASK-2408
  `/api/mrp/purchase-orders/sourcing-requests` is GET-only (its do-nothing PATCH is gone, so PATCH answers 405; OpenAPI lists GET only).
  Since `89c42c1` TASK-2612 (re-read at `2cb3760`) one rule (`lib/sourcing/status.ts:isSamplePayerAllowedOnPath`) lets only a CUSTOMER-path request
  name the customer as samples payer, at create, SAVE_BRIEF and on the samples route (400 VALIDATION otherwise); `d69a3e8` TASK-2616 adds a
  "Request from customer" button on an approved customer-supplied raw (UI only).
- **PurchaseOrder** → `app_purchase_order` (S:3391): unique `poNumber`; `status` String (enum `PurchaseOrderStatus`
  unused); live values are the FileMaker words (Cancel, Complete, DRAFT, Pending, B.Order, Revised, Generated, capture 2026-10-20), from which and the line quantities `lib/mrp/po-fulfillment-status.ts:derivePoFulfillmentCode:12` derives DRAFT/SUBMITTED/PARTIAL/RECEIVED/CANCELLED (one reader: `operating/po_status.py`);
  `orderDate` is column `date_generated`; `shippingCost`, `dutiesCost` (estimates), `incoterms`, `shippedAt`,
  `fulfillmentMode`; `customFields.fmPoNumber/fmGeneratedPoId/renumberedTo`. Lines `PurchaseOrderLine` (S:3447; purchase UoM, `quantityReceived`).
  Numbers: `lib/mrp/po-number-series.ts` `PO100000+` / `DPO100000+` (advisory locks; MAX over gold, FM `generated_purchase_order.ponumber`,
  BC map); legacy `PO-{base36}`, `{poNumber}-FM{id}`; since `97ce9d6` TASK-3252 (re-read at `731dfb3`) `BC_PO_NUMBER_KEPT_PREFIXES` (empty by default) names old prefixes that stay canonical after a prefix switch (`isCanonicalPoNumber:82-86@731dfb3`), and `BC_ERA_PO_NUMBER_PATTERN:69` / `PO_REFERENCE_SQL_PATTERN:72` follow the current, kept and DPO prefixes; `business-central/align-portal-po-number.ts:alignPortalPoNumberToBc:10` renumbers to BC.
  **Generate PO (re-read at `38cd3f3`, `359e2f0` TASK-2651):** create-po and requisition convert call `lib/mrp/create-portal-purchase-order.ts:createPortalPurchaseOrder:207`:
  one `app_purchase_order` (status SUBMITTED) + lines in one transaction, numbered PO100000+ under the series lock, idempotent on an explicit key, the
  requisition ids or a short-window content hash, and no FM write; the old `createFmPurchaseOrder` (FM `generated_purchase_order` first, gold mirror
  after, skipped when supplier or item did not resolve) is gone. FM POs are legacy rows the Portal still reads (raw-material POs tab, `8320bfc` TASK-1871);
  a new PO has no `fmGeneratedPoId`/`fmPoNumber`. Packaging demand reads portal records (`packaging-reorder-demand.ts`).
- **Receipt** → `app_receipt` (S:3562): unique `receiptNumber` (= FM lab id on the MRP path); `status` String
  (INTAKE/RELEASED/QUARANTINE/REJECTED/HOLD); `vendorInvoiceNumber`, `freightCost`, `dutiesCost` (actuals, first receipt of a PO only);
  `ReceiptLine` (S:3618; `labId`, `supplierLotNumber`, `expiryDate`, `binLocationId` scalar no FK). Flow (`app/api/mrp/receiving/raw/route.ts` POST):
  `lib/mrp/receipt-lab-id.ts:allocateRawLabId` (`YYDDD-N`; pack `RP#######`) → INSERT FM `receiptraw` → `lib/mrp/apply-po-line-receive.ts:applyPoLineReceive`
  (FM + gold received qty; `receiveQtyToPurchaseUom:58` kg↔purchase UoM via FM `kgperuofm_c`) → `lib/mrp/upsert-gold-receipt.ts:upsertGoldReceipt:164`.
  QC gate `lib/mrp/receiving-qc.ts:evaluateQcRelease:223`; raw release updates FM `receiptraw.status_c` only.
  **Pack receipts on portal records (re-read at `38cd3f3`, `5cade1b` TASK-2652):** `POST /api/mrp/receiving/pack` writes one `app_receipt` (PO_RECEIPT,
  `receipt_number` = the `RP` lab id, `purchase_order_id` by exact `po_number`, `idempotency_key`), one `app_receipt_line` (`qc_results` JSON) and an
  unbinned QUARANTINE `app_inventory_lot` numbered by the lab id, with the PO line roll-up, in one transaction (`lib/mrp/pack-receipt-portal.ts`); QC and
  release/reject (`receiving/pack/[id]/release/route.ts`, `id` = the receipt line id) move the line, the receipt and its lot(s) together; a numeric
  `receiptpack` id is read-only (409 `legacy_receipt_read_only`) until the cutover load. Nothing writes FM `receiptpack` any more, so a pack receipt from
  `38cd3f3` on exists only in `app_receipt*` (served) and `app_inventory_lot` (not served). Release also needs a passed Form #012 incoming QC against
  the customer-approved artwork version (`IncomingQcRecord`, `e59f1d7` TASK-2627; a FAIL opens an NCR). **Re-read at `71f7214` (`b5311a9` TASK-2775):**
  the artwork part only warns: no approved version, a changed approved version or a different arrived version lets the release go ahead with an
  `AuditLog` row (kind `pack_receipt_release_artwork_warning`) and `warnings` in the response; a missing or non-Pass Form #012, an arrived version
  not recorded against a standard, or no retained sample still answers 409 (`lib/mrp/incoming-qc-form.ts@71f7214:incomingQcReleaseBlock:98`). Put-away decrements that unbinned lot
  (`receipt-put-away.ts:putAwayReceiptLine`).
  Since TASK-2336 (`app/api/mrp/receiving/raw/route.ts@8e3a951:37-39`, `lib/mrp/test-receiving/env.ts:isLegacyFmReceiptWriteRetired`) this POST
  (and `/pack` until `5cade1b`; the pack POST now writes portal records everywhere) answers 410 before auth on every non-production deployment,
  staging included: only production still writes FM raw receipts here.
  **Customer-supplied duplicate receipt (re-read at `2cb3760`, `ba9bbf6` TASK-2604):** when FileMaker receives customer-supplied material, the receipt
  sync writes a second gold receipt for the same lab id (a PLX-owned `PO_RECEIPT` numbered by the lab id) beside the `CUSTOMER_SUPPLIED` receipt of the
  Customer Material Request. Put-away refuses its unbinned line and the queue leaves it out (`lib/wms/receipt-put-away.ts:CUSTOMER_SUPPLIED_LAB_ID_LINE_IDS_SQL:56`,
  matched on item and trimmed lab id; refuses too when that check cannot run). So `app_receipt` can hold two receipts for one physical
  customer-supplied receipt: never count both, and never read the `PO_RECEIPT` twin as PLX-owned stock or a PLX purchase.
  **$0 PO line for a customer-supplied item (`602bf8a` TASK-2312):** buyers still raise $0 PO lines to a customer-as-vendor card for customer
  material; BC reprices such a line at the item card cost and leaves it received-not-invoiced. Generate PO now only *warns* (`customerSupplied`
  on the line, behind the customer-material receiving switch; `lib/customer-materials/zero-cost-po-line.ts:flagCustomerSuppliedPoLines:149`); it
  blocks nothing and changes no cost or PO data. A $0 PO line whose item is on a customer's supplied list is that pattern, not a pricing error,
  and the BC value of its receipt is not PLX's cost.
- **Receiving documents (vendor CoA / CoC / packing slip) — originals path (TASK-2164, `fdaa88f`; checked 2026-10-01 at `487862b`)**:
  PDFs live in SharePoint drive `SHAREPOINT_DRIVE_ID` (`lib/m365/auth.ts:48`). Until TASK-3213 there was no `Document`/`Attachment` row;
  since `d7a1c95` (`lib/mrp/receiving-doc-record.ts@b4118a3`) each new upload records an `Attachment` row (entityType `RECEIPT_RAW`/`RECEIPT_PACK`,
  entityId = receipt id) with a finalize job, and older files have none (SOURCE-NOTES "Receiving originals").
  Folder per supplier + material, **not per receipt**: raw `Receiving/Raw/{safe(supplier.name)}/{safe(raw_material.rawcode)}`
  (FM `receiptraw.idrawsupp → raw_material_supplier`, `app/api/mrp/receiving/raw/[id]/coa/route.ts:resolveFolder:35-53`);
  pack `Receiving/Packaging/{safe(packaging_supplier.suppname_c)}/{safe(packaging.packcode)}`; `safe` = trim, `[^a-zA-Z0-9._-]→_`, ≤60 chars,
  blank → `_unknown`. Names `{epoch ms}_{sanitised}` (`lib/mrp/receiving-docs.ts:buildReceivingDocFileName:42`), packing slips prefixed
  `packing-slip_`; CoC is docType `coa`. `GET …/coa` lists the folder; `?file=<plain .pdf>` streams one file with the app-only token
  (`lib/mrp/receiving-doc-file.ts`; STAFF). **API**: no files; the raw folder path is derivable from served FM rows
  (`receipt_raw_material.idrawsupp`, `raw_material_supplier.idraw/idsupplier`, `raw_material.rawcode`, `supplier.name`); the pack
  supplier segment `suppname_c` is not served. A PDF in the folder is a candidate for a receipt, never proven to be its document.
- **BC**: POs pushed when vendor + items LINKED and not DISTRIBUTE (`outbox-producer.ts:513-574`); receipts via AL receive-only bridge for
  BC-era PO numbers (`po_number ~ $1` = `BC_ERA_PO_NUMBER_PATTERN` from `lib/mrp/po-number-series.ts`, since `97ce9d67` TASK-3252,
  `outbox-producer.ts@0323e19c:714-715,1079`; before, the literal `'^(PO|DPO)10'`); historical FM receipts are NOT replayed (ERP-D-009). Since TASK-2348 (`outbox-producer.ts@8e3a951:528-623`)
  DRAFT POs are not enqueued (`BC_SKIP_DRAFT_POS`, default on everywhere), and test/UAT suppliers, POs and their receipts/invoices are not
  enqueued outside Vercel production (`BC_EXCLUDE_TEST_RECORDS`; predicate `business-central/test-record-guard.ts:isBcExcludedTestRecord`).
- **API**: `public.app_supplier`, `app_supplier_product`, `app_manufacturer`, `app_purchase_requisition`, `app_pr_line_item`,
  `app_purchase_order`, `app_purchase_order_line`, `app_receipt`, `app_receipt_line`; FM `public.supplier`, `manufacturer`,
  `generated_purchase_order(_item)`, `receipt`, `receipt_raw_material`, `receipt_packaging`, `raw_material_supplier`, `packaging_supplier`;
  `silver.suppliers`, `purchase_orders`, `purchase_order_items`, `receipts`, `receipt_raw_items`, `receipt_pack_items`, …;
  Sage `sage.po_header`, `po_detail`, `po_receipt_header`, `po_receipt_detail`. **Not exposed**: `app_supplier_product_tier`, `public.tierpricing`.

### 3.6 Item / product / SKU
- **Model** `Product` → `app_product` (S:1720). Unique `code` = Portal/FM code AND today's **BC Item No.**
  (`serializeItem` → `bcItemNumber(code)`); `erpCode` = Sage IC item No. (cutover match key, not BC No.);
  `externalId` (not unique) = `fm-raw-{id}` | `fm-pack-{id}` | `fm-bulk-{id}` | `fm-fp-{id}`; `productType` (S:119
  RAW_MATERIAL, PACKAGING, BULK, FINAL_PRODUCT, COMPONENT, SERVICE, DISTRIBUTION); `uom` (default kg); `costingMethod`
  (all AVERAGE since ERP-D-013); `standardCost`, `unitCost`, `averageCost`; `fillWeight`; `hsCode` (import HS, distinct from
  `ProductPackSpec.tariffCode`); `unitsPerBox`; `jobFulfillmentMode`; `deletedAt`. Contract: `docs/modules/order-spine/README.md` table "Three identifiers".
- `ProductSellingPrice` → `app_product_selling_price` (S:1854; MRP-M-265, `6dfa61d`; unique `[productId, priceListCode, currency, qtyUnit]`, `unitPrice` Float, FK to Product): Portal selling price per item/price list/currency/qty unit; schema comment: Sage `ic_pricp` stays a read-only copy and **orders do not read this table yet**. Served by `app/api/mrp/products/[id]/selling-prices/route.ts` (+ `distribution-products/[id]/…`), helper `lib/mrp/product-selling-price.ts`. Not in `scripts/sql/workbench-readonly/allowlist.json` at `b46934a`, so the reporting API does not serve it. Sales price for an order line stays `SalesOrderLine.unitPrice`.
- `ProductPackSpec` (S:1875, 1:1, case dims/units per case/pallet; FM + receiving), `ProductUomBarcode` (S:1923), `ProductCategory`.
- **UOM rules** (`lib/mrp/product-uom.ts`): `normalizeProductUom:121@731dfb3` folds aliases, never converts; since `64331a9` TASK-3345, `productUomOptionsForType:68-73@731dfb3` limits the `FINAL_PRODUCT` picker to ea/cse/pail/dz, and other types keep every option.
- **UOM catalogue** `lib/mrp/product-uom.ts` (`PRODUCT_UOM_OPTIONS:30` → BC codes ea→PCS, cse→CSE, g→GR…; `normalizeProductUom:105` folds aliases, never converts).
- ETL: `portal/scripts/etl-silver-to-gold.ts:etlProducts:343`; since `6641353` (TASK-3332, MRP-M-265, `@b4118a3:367-370`) it sets `uom` on INSERT only, so staff own a product's unit after that and a refresh no longer resets it to the type default. The `source_unit_contracts.json` contract (`@8da33e4:674-735`) is not re-read here. Cross-system refs: `lib/business-central/item-search-refs.ts:resolveItemSearchRefs:56`.
- **API**: `public.app_product` (cost columns `average_cost`, `total_cost`, `bulk_cost` exposed; `standard_cost`, `unit_cost` NOT),
  `app_fg_item`; FM `public.raw_material`, `packaging`, `bulk_product`, `finished_product`, `finished_product_item`; silver equivalents;
  Sage `sage.ic_item`, `sage.ic_item_uom`.

### 3.7 Formulation / BOM version
- **Authoritative formula = FM bronze** `public.formula` + `public.formula_item` (R&D lane edits it via `lib/formulas/write.ts`:
  `createFormula:352`, `nextFormulaCode:114` `F-YY-####` under advisory lock, `assignFinalLetterSuffixOnEdit:193` (first edit of a
  Final appends `-A…-Z`), MAX+1 ids). Line % columns: `percfinalwpercent_c` (0–100, Portal formula page) and `perctentry` (FM 0–1, stale/NULL).
- **Gold copy** `Formulation` → `app_formulation` (S:2071; unique `code`, `externalId` `fm-form-{id}`, `[productId, version]`;
  `status` DRAFT/IN_REVIEW/APPROVED/OBSOLETE mapped from FM Final/R&D/revised by `portal/scripts/etl-formula-status.ts`;
  `specificGravity`, `batchSizeKg`, `projectKey` (provenance only), `deletedAt`). `FormulationIngredient` (S:2134; `percentage` 0–100
  since TASK-2109, rebuilt from the formula page by `lib/formulas/gold-formula-lines.ts:planGoldFormulaLines:79`).
  Gold formulation versioning/approval routes are retired (410); `lib/services/formulation-bom.ts` remains.
- **Formula pin**: pricing follows only the explicit pin (`lib/project-development/formula-pin.ts`; `lib/costing/formulation-resolution.ts:resolveQuoteFormulation:130`).
- **BOM** `BillOfMaterials` → `app_bill_of_materials` (S:2207; unique `code`, `[productId, version, category]`; `category`
  FORMULA/ASSEMBLY/PACKAGING; `status` BomStatus; `parentBomId`; `fillWeightG`; `projectKey`; `deletedAt`), `AssemblyLayerGroup`,
  `BomLine` (S:2274; `needsSourcing`, `deletedAt`). Lifecycle `lib/assembly-bom/lifecycle.ts` (DRAFT→IN_REVIEW→APPROVED→OBSOLETE;
  edits DRAFT only; `nextAssemblyBomCode:53` `{code}-V{n}`); `lib/assembly-bom/service.ts:createAssemblyBomVersion:883`. Approval is a regulated
  signature since `4304012` TASK-2602 (§3.15). **Frozen FG (`208dd9e` TASK-2633, re-read at `2cb3760`):** once an FG is ACTIVE or RETIRED through a real
  activation (some revision pinned a formula; backfilled and FM-adopted FGs stay editable), its formula, Pack BOM, intake, pack facts and
  routing are frozen: pack-draft PATCH (artwork label slots excepted), intake PATCH, fg-routing POST, Pack BOM create or re-point and
  Assembly BOM versioning of the pinned BOM answer 409 `FG_FROZEN` (`lib/final-products/fg-freeze.ts@19f1d94:findFrozenFgItem:49`; since `e3604ce` TASK-2805
  `findFrozenFgPins:62` also returns the frozen FG's Pack BOM pin and activation time for the freeze-aware Pack BOM lock). A frozen FG's
  live BOM and routing therefore still equal what activation minted; a later change arrives as a new FG revision, never an edit.
- **API**: none of `formula`, `formula_item`, `silver.formula_items`, `app_formulation*`, `app_bill_of_materials`, `app_bom_line` are served.
  Only `public.finished_product_item` / `silver.finished_product_items` (FG component list) and `bulk_product`.

### 3.8 Assembly order (AO) / manufacturing order / job / batch
- **Authoritative AO = FM bronze** `public.assembly_order` (`nameamo_c` = `AO-{YY}{doy}{letter}`, created by
  `app/api/mrp/assembly/create/route.ts:POST:68`; `idfinprod` FM fp id; `idbatch`). Items `public.assembly_order_item`
  (`qty_c` planned component qty, `actualamount`, `qtybatch`), status/damage/production tables. Live writes: `lib/inventory/assembly-entries.ts`
  (`insertAssemblyDamage:193`, `insertAssemblyStatus:283`, `insertAssemblyProductionTask:364`).
- **Gold** `ManufacturingOrder` → `app_manufacturing_order` (S:2469) **is the AO** (do not rename; order-spine README).
  Unique `code` (column `mo_code`) = FM `nameamo_c` via `etlManufacturingOrders:674` (Portal-native `MO-{base36}`);
  `quantity`, `status` String (transitions `lib/mrp/api-handlers/manufacturing-orders.ts:manufacturingOrderStatusTransitions:37`),
  `salesOrderLineId`, `customerOrderId` (FM `idcustomerorderassembly`), `batchNumber` (column `lot_number`), `qtyDamaged`
  (FG units), `qtySample`, `sizePieces/Boxes/System`, `yieldExpected` (= FM `qtymanufold`), `yieldActual` (= FM `finalqty`), `deletedAt`.
  `ManufacturingOrderLine` (S:2571, `quantity` = required, `actual_amount` = used). `AssemblyOrderDamage`/`AssemblyOrderStatus`/
  `ProductionTask`/`BatchSchedule` are ETL-only gold copies (`etl-silver-to-gold.ts` `etlAssemblyOrderDamage:1801`, `…Statuses:1846`,
  `etlProductionTasks:1971`, `etlBatchSchedule:1894`; `@b4118a3`). `ManufacturingJob` (S:2443; `MJ-{yyyy}-{b36}`) = production demand
  (MAKE_TO_STOCK / MAKE_TO_SHIP). SO Produce line → MO: `8b0f79938` (`lib/mrp/api-handlers/manufacturing-orders.ts`). **Re-read at `59b0548`** (`resolveLinkedSalesOrderLine`, `createManufacturingOrder`): a linked line on a DISTRIBUTE order is refused with 400 "Distribution lines cannot create a manufacturing order"; a Produce (MAKE) line always qualifies; a STOCK (warehouse) line qualifies on a made-to-ship order unless the product's `jobFulfillmentMode` is MAKE_TO_STOCK (then 400); creating the MO rewrites that line to `lineSource` MAKE and clears `warehouseId` (`promoteToMake`). Earlier text, "only MAKE lines", is superseded. **Re-read at `568f69c`** (#1947 TASK-2397): an MO links to the sales order line it was raised from by `ManufacturingOrder.salesOrderLineId` (no schema change), and a line has at most one *live* MO (status not CANCELLED, `deletedAt` null): create (`resolveLinkedSalesOrderLine:108`) and tagging an existing MO onto a line (`tagExistingSalesOrderLine:753`, 409 `SOL_ALREADY_HAS_MO` at `:829`) both refuse a second; cancelled or soft-deleted MOs may still carry the id. `lib/mrp/mo-so-line-link.ts` only picks the live MO and builds the line label for display. The Workbench edge `AssemblyOrder.for_order_line` (N:1) already reads this column.
- **AO status** (FM text): Generated → Allocated → Committed → Realized, + Cancelled (`lib/inventory/assembly-lifecycle.ts:canonicalizeStatus:142`,
  "Completed"=Realized; `applyAssemblyStatusTransition:265` writes the pack/bulk/FP inventory ledger).
- **AO ↔ customer orders (many-to-many)**: FM `public.order_entry_item_assembly(idoeitem, idassembly)` maintained by
  `POST /api/mrp/assembly/[id]` (`link_oe_item_id`, rejects when OE line `idfp` ≠ AO `idfinprod`); gold keeps one link only.
  **Portal order lines (re-read at `38cd3f3`, `57bd403` TASK-2711):** a MAKE order confirmed after TASK-2638 has no FM OE line, so the same POST
  also takes `link_sales_order_line_id` / `unlink_sales_order_line_id` (`lib/mrp/ao-portal-order-link.ts:linkAoToPortalLine:178`): it sets
  `ManufacturingOrder.salesOrderLineId` on the gold MO whose `mo_code` equals the AO's `nameamo_c` (409 without one), only for an open MAKE line of
  a MAKE order of the same product and customer, and only when no other live MO holds the line. An FM-born AO linked this way therefore reaches its
  portal order through the gold MO (`AssemblyOrder.for_order_line`), not the FM junction, which stays for legacy links; `oelist_c` lists both.
- **AO number and product (re-read at `38cd3f3`):** `POST /api/mrp/assembly/create` issues `AO-YYDDD{letters}` for the plant's local today
  (America/Toronto, not the scheduled date) under a series + day advisory lock, letters bijective base 26 up to ZZ, never refilling gaps
  (`lib/numbering/day-code-series.ts`, `a5b835d` TASK-2642; batches the same `BC-` series); the `ao_code` grammar `AO-\d{3,5}[A-Z]{1,2}` still fits.
  New AOs stamp `assembly_order.portal_product_id` (`1b02309` TASK-2650, no backfill; not in the reporting manifest), and readers resolve the FM fp
  through it first (`lib/inventory/ao-product-link.ts:aoFmFpIdSql`). **Portal-born AOs** (that column set) realize into a portal `app_inventory_lot`
  only and write no FM `finished_product_inventory_item` row (`eb14fe3` TASK-2643, `assembly-lifecycle.ts`): FM FP inventory under-counts their
  output, and the lot is not served. FG activation still mints the FM fp beside the portal product (`710fc7b` TASK-2709), so FM fp ids stay valid.
- **Batch**: FM `public.batch`/`batch_item` (`BC-{YY}{doy}{letter}`, `lib/inventory/batch-lot-number.ts:regenerateBatchLotNumber:41`),
  lifecycle `lib/inventory/batch-lifecycle.ts:applyBatchStatusTransition:159`; gold `BatchRecord` → `app_batch_record` (S:2642;
  unique `batchNumber`, 1:1 `manufacturingOrderId`; no deletedAt).
- **API**: `public.app_manufacturing_order(_line)`, `app_manufacturing_job`, `app_batch_record`, `app_batch_schedule`,
  `app_production_task`, `app_assembly_order_status`, `app_assembly_order_damage`; FM `public.assembly_order`, `assembly_order_item`,
  `assembly_order_status`, `assembly_order_damage`, `batch`, `batch_calibration`, `production`; silver `manufacturing_orders`,
  `assembly_order_items`, `assembly_order_statuses`, `assembly_order_damage`, `batches`, `batch_schedule`, `production_tasks`.
  FM AO columns `totalprodqty_c`, `qtyacc_c`, `qtymfgleftoe_c` are NOT in the manifest.

### 3.9 Lot and inventory
- **Lot ledger** `InventoryLot` → `app_inventory_lot` (S:3097; unique `[productId, lotNumber]`; `quantityOnHand/Reserved/Available`,
  `status` InventoryStatus QUARANTINE (default)/RELEASED/REJECTED/ON_HOLD/EXPIRED, `ownership` PETRA|CUSTOMER, `expiryDate`,
  `costPerUnit` (never set at put-away), `receiptId` scalar no FK, `customFields.aoId`). `InventoryTransaction` → `app_inventory_transaction`
  (S:3141; types RECEIPT, ISSUE, TRANSFER, ADJUSTMENT(_UP/_DOWN), SCRAP, RETURN, PICK, PUT_AWAY, SHIP; polymorphic `referenceType/Id`).
- **Legacy ledger** `Inventory`/`InventoryItemTransaction`/`StockMovement` (S:2970/3001/3023, keyed by `Location`) — not reconciled with
  the lot ledger; never sent to BC.
- **Lot numbers** `lib/wms/lot-numbers.ts`: stored `BASE@BINCODE` (BASE = FM lab id/lot); `lotNumberBase:24`. ETL lots
  `fm-wlp-{id}`, `idlab@idloc` (`portal/scripts/etl-wms-silver-to-gold.ts:270-345`; overwrites on-hand/available/status on each run).
  Put-away `lib/wms/receipt-put-away.ts:putAwayReceiptLine:68`. Sage `SAGE-{loc}-{bin}-{item}`.
- **Customer-owned lots (re-read at `38cd3f3`, `25101f3` TASK-2310, `27f2bf5` TASK-2706):** a lot with `ownerCustomerId` is committed only to a
  pick job (AO, batch or shipment pick list) of that customer (`lib/inventory/job-customer.ts`, conditional update in `pick-stock.ts:commit`).
- **Buckets** `lib/wms/pick-stock.ts`: commit (reserved+=q, available−=q), `release:141`, `consumeCommitted:179`; realize via
  `lib/wms/consume-production.ts:consumeRealizedProduction:96`. Warehouses/bins: `Warehouse` (code = Sage site 11/15/22/VMI/3PL…), `Zone`, `BinLocation`.
- **API**: none — `app_inventory_lot`, `app_inventory_transaction`, `app_inventory`, `app_warehouse`, `app_pick_*`, FM `inventory_raw`,
  `silver.inventory_lab_tests` are all unserved. FM `public.raw_material.inventory` / `packaging.inventory` / `bulk_product.inventory` give totals only.

### 3.10 Shipment, ship release, pick list, freight
- `Shipment` → `app_shipment` (S:3785; unique `shipmentNumber` column `shipment_code`, `SH100000+` `lib/wms/shipment-number-series.ts:60`;
  `status` String (enum ShipmentStatus unused); FKs `salesOrderId`, `customerId`, `customerLocationId`, `warehouseId`, `shipReleaseId`;
  `trackingNumber`, `carrier`, `freightCost`). `ShipmentOrder` (`orderRef` = FM OE id or SalesOrder.id), `ShipmentLine` (S:3883; `orderLineRef`
  = FM `order_entry_item.id` OR `SalesOrderLine.id`, no FK; `lotId`; `aoRefs[]`), `ShipmentAllocation` (S:3915; FK `salesOrderLineId`; written
  only by distribution ship + bundler, not the WMS builder).
- Owners: `lib/wms/create-shipment.ts:createShipment:40`; `lib/wms/ensure-sales-order-for-shipment.ts:31`;
  `lib/wms/resolve-sales-order-for-shipment.ts:27`; confirm `app/api/mrp/wms/shipments/[id]/confirm/route.ts` (release gate per line: `shipmentSkipsShipRelease` from `lib/mrp/distribution-sage-map.ts:24` skips it only when the header is DISTRIBUTE and no allocated `SalesOrderLine.lineSource` is MAKE, so a DISTRIBUTE order with any MAKE line still needs a RELEASED ship release or `SHIP_RELEASE_REQUIRED` 409, `:63-70`, `:111` RELEASED only, TASK-2257;
  QC-released lots only `:203-220`, `consumeCommitted`, LPs SHIPPED, `recomputeReleaseFulfillment`; since `b485219` #1865 TASK-2297 a confirm
  also enqueues a customer shipping notice, `enqueueShipmentNotice` at `route.ts:337@12db891`: one more write side effect, no gate change).
  INFERENCE (likely bug): Sage-backed orders (cuid orderRef) return `oe_not_found` before `preferSalesOrderId` is used, leaving `salesOrderId` null
  (since `8665d4d` a shipment that already carries `salesOrderId` skips `ensureSalesOrderForShipment`).
  **Confirm at `38cd3f3`:** the release gate also reads each line's own `salesOrderLine.lineSource` (builder lines carry `salesOrderLineId` but no
  allocation, `fe53eec` TASK-2701); candidate releases are found by portal order keys (`shippingReleaseWhere`); every pick must carry the
  shipment-line provenance marker (`[[PLX-PICK-PROV v1 shipmentLineId=…]]` on the last `PickLine.notes` line, `lib/wms/pick-line-provenance.ts`,
  `baa0e1a` TASK-1108) and be attributable to exactly one shipment line, else 409 (`attribute-picks-to-shipment-lines.ts`, `18c00b8` TASK-2675);
  an over-pick is 409, a short pick rewrites that line's (and its single allocation's) quantity to the picked quantity, an ambiguous short pick
  is 409 (`5b0b352` TASK-2395); a line is stamped with a lot only when all its picks share one. `GET /api/mrp/wms/shipments/[id]` lists every pick
  per line with lot/AO/batch/LP lineage and every missing link (`shipment-pick-lineage.ts`, `119eebd` TASK-2676; `batchNumber` is a carried string,
  `bcVerified: false`). Shipping lists admit a gold order with a RELEASED release beside Sage-backed orders (`sage-ship-queue.ts:shippableSalesOrderSql`,
  `32d0274` TASK-2640).
- **Ship release** `ShipRelease` → `app_ship_release` (S:3964; `SR-YYYYMMDD-NNNN`; `orderRef` = FM OE id before TASK-2641, below; DRAFT→RELEASED→FULFILLED|CANCELLED),
  `lib/ship-release/index.ts` (`getLineOpenQuantities:174` reads FM `order_entry_item`, `createShipRelease:245`, `validateShipmentAgainstRelease:463@b6eaa7a`).
  Since `b6eaa7a` #1908 TASK-2398: a DRAFT release can be released (`releaseDraftShipRelease:412`, `PATCH /api/mrp/orders/[id]/ship-releases/[releaseId]`,
  STAFF; status-guarded DRAFT → RELEASED with `releasedBy`/`releasedAt`, audit `SHIP_RELEASE_RELEASE`; 409 otherwise); the release builder's
  `GET …/ship-releases` adds per line `qtyProduced` (Σ FM `assembly_order.finalqty` over `order_entry_item_assembly`, null without one) and
  `qtyAvailable` (Σ `app_inventory_lot.quantity_available` in RELEASED lots), and `lib/ship-release/stock-check.ts:assessReleaseStock` flags
  over-produced / over-available lines, advisory only (never blocks); the shipment builder defaults ship-to to the release's
  `customerLocationId`, else the customer's default location, else the first (`lib/ship-release/ship-to-default.ts:pickDefaultShipToId`).
  MAKE orders need a RELEASED release to confirm; DISTRIBUTE do not (unless a line is MAKE, above).
  **Portal order keys (re-read at `38cd3f3`, `8665d4d` TASK-2641):** `ShipRelease.salesOrderId`, `ShipmentOrder.salesOrderId` and the new
  `ShipReleaseLine.salesOrderLineId` / `ShipmentLine.salesOrderLineId` (FKs) are the canonical identity; `orderRef` / `orderLineRef` stay as
  compatibility refs (the portal `SalesOrder.id` / `SalesOrderLine.id` for a portal order, the FM ids for a legacy one). Migration
  `20261012080000_portal_shipping_keys` backfilled exact portal ids first, then a unique `fmId` crosswalk, and for numeric legacy line refs only a
  product unique within the order; anything ambiguous stays null. `createShipRelease` refuses duplicate lines and a customer, line or product that
  does not match the portal order (400); `getLineOpenQuantities` reads `app_sales_order_line` for a portal order and FM `order_entry_item` only for a
  legacy one; shipped and authorized quantities match on `salesOrderLineId`, else the legacy ref (`lib/wms/portal-shipping-keys.ts`). Served: the API
  still serves `app_shipment_line` and `app_ship_release` without the new columns (allowlist unchanged), so the Workbench sees `sales_order_id`
  on releases and shipments but no line key.
- **Quantities**: `GET /api/mrp/wms/shipments/orders` `qty_shipped` = Σ non-CANCELLED shipment lines (DRAFT counts), remaining =
  max(0, ordered − shipped); FM `qty_produced = qtyaomfg_c + qtybcmfg_c`.
- **Freight**: `lib/wms/carrier/freightcom/*` (`FREIGHTCOM_API_TOKEN`, `FREIGHTCOM_RATING_ENABLED=0` kill), `lib/wms/shipment-quote.ts:quoteGoldShipment:178`
  with `destinationFromShipTo:123` (Shipment.customerLocation → SO.customerLocation → SO.shipToSnapshot → default location → customer card; else 400
  `MISSING_SHIP_TO`); pallet builder `lib/wms/pallet-quote.ts:buildQuote:126` (`POST /api/mrp/wms/pallet-builder/quote`, read-only).
- **API**: `public.app_shipment`, `app_shipment_line`, `app_ship_release`; `silver.shipments` (FM, no since); Sage `sage.oe_shipment_header/detail`.
  Not served: `app_shipment_order`, `app_shipment_allocation`, `app_ship_release_line`, pick lists, LPs.

### 3.11 Invoice, payment
- **No Prisma sales-invoice model.** BC read models (raw SQL): `bc_invoice_snapshot` (PK `bc_system_id`; `number`, `external_document_number`,
  amounts, `remaining_amount`), `bc_payment_snapshot`, `bc_purchase_invoice_snapshot`, `bc_vendor_payment_snapshot`, `bc_receipt_snapshot`,
  `bc_customer/item/currency_rate_snapshot`, `bc_approval_entry_snapshot`; `bc_shipment_snapshot` written by `outbox-drain.ts:530` but has no DDL in repo.
- `OrderPrepayment` → `app_order_prepayment` (S:3756; unique `bcPaymentSystemId`), filled by `lib/orders/prepayment-sync.ts:reconcileBcPaymentsToPrepayments:42`.
- Finance matching `lib/business-central/document-finance-queries.ts` (`resolveSalesOrderFinanceMatchKeys:50`, `fetchSalesOrderFinance:166`).
- Under the current SOP the BC sync does **not** post sales/purchase invoices (`sync-runner.ts:44-56,140-147`).
- **API**: Sage `sage.oe_invoice_header`, `sage.oe_invoice_detail` only. No BC table is in the manifest.

### 3.12 Quote / project
(Re-verified 2026-10-01 at `487862b`; S: numbers below are at that commit.)
- `Quote` → `app_quote` (S:5230; unique `quoteNumber` `QUO-<year>-NNNN` `lib/quote/service.ts:generateQuoteNumber:124`, no lock; status
  DRAFT/SENT/NEGOTIATION/ACCEPTED/REJECTED/EXPIRED with transitions `lib/quote/status.ts:7`; `customerId` FK; `projectId` plain string
  holding the project KEY; `customFields.version` always 1). `QuoteVolume` (S:5270; unique `[quoteId, askedUnits]`), `QuoteLine` (S:5292; no
  Product FK — `customFields.productId/fpCode/stdCost`). PATCH with lines deletes and recreates all lines.
- **Quote.projectId is mutable (FACT, `app/api/mrp/quotes/[id]/route.ts@487862b`)**: `PatchBody.header.projectId:43` is written in place
  (`data.projectId:118`, one `$transaction`) by any STAFF+ user (`requireRole("STAFF"):58`) while DRAFT/SENT/NEGOTIATION (`EDITABLE_STATUSES:55`;
  ACCEPTED/REJECTED/EXPIRED → 400). No existence check, no `resolveQuoteProjectRef`, no project-customer vs quote-customer check; any string or
  null is accepted. The audit row is `UPDATE QUOTE "Quote N updated"` (`:158`) with no old/new value; no previous-value history exists.
  TASK-2182 (`487862b`, 2026-10-01 09:46 -0400) adds the quote-detail picker `app/mrp/quotes/[id]/quote-project-linker.tsx` (shown when
  `canEditQuoteLines(status)`) using `lib/project-development/quote-picker.ts:quoteProjectLinkHeader:29` (`projectId` = `projectKey || id`;
  "Lines stay untouched"). A header-only PATCH keeps saved offer lines (`unitCost`→`stdCost`, margin, price per volume), so they can carry
  prices from project A while the quote links project B, with no pricing-basis marker. Matrix GET/POST and workbook re-read the current
  `projectId` (`matrix/route.ts:122,224`), so the live matrix, customer ask and Theoretical FG inputs change at once. API serves only the
  current `app_quote.project_id`/`updated_at` and `app_quote_line.created_at/updated_at`; not the prior value, the actor, or the pricing project.
  Workbench handling (`operating/quote_readiness.py`, `quote_cost_trace.py`): the earlier value comes only from Workbench observation
  history; lines first observed before the latest observed change → unknown `QUOTE_PROJECT_CHANGED_AFTER_PRICING` + proposed
  `REVIEW_OFFER_AFTER_PROJECT_CHANGE` (never repriced); a census read for another project → `CENSUS_FOR_DIFFERENT_PROJECT` + refresh, not
  a failure; without history → `PROJECT_LINK_HISTORY_NOT_READ`. A change before the first observation is invisible.
- **Quote matrix unit cost (TASK-2175, 2026-10-01, `portal/src/lib/quote/matrix-fallback.ts`, `app/api/mrp/quotes/[id]/matrix/route.ts@fc13a4a`, unchanged at `487862b`)**: per volume column,
  unit cost = engine `column.unitCost` if finite >0, else the FIRST line with `stdCost>0` (`firstPositiveLineStdCost`, one value for every volume, not scaled by
  quantity), else `result.estimate.totalCostPerUnit` (Theoretical FG), else 0. **Re-read at `45673ee` (`514e1e5`, TASK-2611, G-D4):** a column with customer-supplied material (`column.customerSupplied` non-empty) skips both fallbacks and keeps the engine value (source `none`), so a PLX standard cost never prices material the quote prices at $0; the quote page lists what the customer must send (`app/mrp/quotes/[id]/customer-to-send.tsx`). Ported in `operating/landed_cost.py` (`fallback_matrix_unit_cost`), goldens re-run at `45673ee`. Price tiers are re-derived as `Math.round((cost/(1−m))*100)/100` (`repriceMarginTiers`, `priceColumnAtMargin`: no EPSILON; m=0 when marginPct is outside [0,100) or not finite); `service.ts` `round2`s the saved price. Breakdown rows stay
  engine/missing; response `reportedMissing` gets a note when a fallback was used, but the chosen source is NOT persisted: POST saves `unitCost` as soft
  `QuoteLine.customFields.stdCost` with no source tag. No currency or cost-unit field exists (assume quote currency, per finished unit; `uom` "unit" on offer lines);
  stdCost is stored unrounded, only prices are round2. Since `b1cf16c` TASK-2648 (re-read at `38cd3f3`) the matrix POST also saves each offer line's
  `planned_conversion_per_unit` (frozen assembly labour + overhead per unit at that volume, NULL when assembly costing was missing) and the SKU `fp_code`
  as columns (neither in the reporting manifest); up to `38cd3f3` the GET wrote through `quoteProjectLanded` (`matrix/route.ts:123`), and since `9108064`
  (TASK-2850, re-read at `71f7214`) the GET (`runQuoteProjectLanded`, `:123-126`) and the POST (`:227-230`) pass `persist: false` and write no snapshot. Overrides: engine cost wins whenever >0; `resolveOfferMarginPct` picks margin. Authority for stdCost: none
  (soft JSON; engine freeze, fallback, TheoreticalFG seed or manual edit are indistinguishable). Served by API: `public.app_quote_line.custom_fields` (so stdCost is
  readable; manifest allowlist, live-check 16 columns) but NOT `margin_percent`, `app_quote_volume`, `app_theoretical_fg` or project/engine inputs. Workbench
  `FIELDS["QuoteLine"]` does not map `customFields`: the Portal bridge drops Json fields (`_lib/security-manifest.ts` BLOCKED_FIELD_TYPES={Json,Bytes}) and the Workbench quote path does not yet read the served column, so `operating/quote_readiness.py` marks line cost basis unknown unless a line carries `stdCost`; then it cites it,
  checks `round2(std/(1−m/100))` vs `unitPrice` (one-cent tolerance) and flags provenance, currency and identical-across-volumes (only lines with a `quoteVolumeId`) as unknowns. See §4.2/§4.3.
- Quote→SO `lib/sales-order/convert.ts:convertAcceptedQuote:179` (ACCEPTED only, `FOR UPDATE`).
- `Project` → `app_project` (S:4508; `projectKey` unique nullable), `ProjectLocal` (S:5109; Portal-born, `id` = project key),
  key sources `lib/projects/project-identity.ts:11` (`fm-proj-` prefix; `bareProjectKey`), `ProjectSubAssembly.fmProjectId` holds id|key|bare key.
  `TheoreticalFG` → `app_theoretical_fg` (S:6601; unique `projectId` and `projectKey`), `FgItem` (S:6638; unique `projectKey`, code `FG-…` not unique),
  `FgItemRevision` (append-only frozen costs).
- **API**: `public.app_quote`, `app_quote_line`, `app_fg_item`. Not served: `app_quote_volume`, `app_project*`, `app_theoretical_fg`, FM `public.project`.

### 3.13 Cost / landed cost
See §4.1–4.3. Stores: `app_theoretical_fg` (engine snapshot), `Product.standardCost/unitCost/averageCost`, `RateSettings` (singleton),
`DraftCostOverride`, `RunVolumeTier`, `Template*CostMatrix`, `FgRouting`, `LineActionSetupStandard`, `FormulaCompoundingStandard`, `EmployeeRate` (L5 only).
**API**: only `app_product.average_cost/total_cost/bulk_cost`, `app_supplier_product.system_cost/delivery_cost`, FM `finished_product.sumcost_c`,
`finished_product_item.itemcost_c`, PO/receipt amounts.

### 3.14 Document
- `Document` → `app_document` (S:5354; `sharepointId/Url`, sync status, `metadata` JSON), `DocumentTag`, `Attachment` (S:5462; polymorphic
  `entityType/entityId`, `driveId`, `sharepointItemId`, `deletedAt`), `FormRequest` (S:6291; DocuSign `envelopeId`), `ElectronicSignature`
  (S:6382; internal 21 CFR Part 11 signatures, not DocuSign). **Re-read at `45673ee` (TASK-2597, `498777b`/`46f24b7`, migration `20261010180000_part11_append_only_audit_tables`):** `app_audit_log` and `app_electronic_signature` are append-only (row triggers refuse UPDATE/DELETE, a statement trigger TRUNCATE; a table owner can still drop the triggers), and `AuditLog.deployedCommit` (`deployed_commit`, from the `app.deployed_commit` session setting, NULL outside a deployment) records which deployed commit wrote a row. **TASK-2290 (`b7b6c09`, migration `20261009180000_mrp_m_284_contact_email_subscriptions`):** `ContactEmailSubscription` (`app_contact_email_subscription`, one row per contact and email `type_key`, exactly one of customer/supplier contact by SQL CHECK) and `PartyEmailSetting` (`app_party_email_setting`, per customer or supplier switch plus `extra_cc`/`extra_bcc`), schema only. None of the four is served; the generated schema carries them as not_served types with their Prisma `///` notes. **TASK-2594 (`e677b49`):** `lib/documents/document-types.ts` is the one category table (TYPE code, retention, short DocumentCategory choice); the generic upload doors write the row's short choice and retention (mirrored as `operating/doc_metadata_taxonomy.py` `DOCUMENT_TYPES`); a file written between `e6dc559` and `e677b49` may carry the older retention (e.g. a customer PO at 3 years, now Permanent). `updateDocumentMetadata` now records before/after tags in its audit row. **Re-read at `59b0548` (doc-only for now):** the schema adds enums `SecurityClass` (STANDARD, FINANCE, BUSINESS_CENTRAL, CUSTOMER_ORIGINAL, SCRATCH) and `StorageState` (PENDING, READY, FAILED) on `Document` and `Attachment` (`security_class`, `storage_state`; TASK-2249), and, from TASK-2219, enum `RecordLinkSource` (PORTAL, BACKFILL, FM_SYNC) and models `RecordType` (`app_record_type`), `LinkKind` (`app_link_kind`) and `RecordLink` (`app_record_link`: from/to type+id, `kindCode`, `source`, `endedAt`). Candidate source for mention links later; nothing in the Workbench reads them. **Re-read at `fa46f2d` (`7c1847e`, TASK-2278 #1802):** the schema only appends the Communications Hub Phase 1 outbound-email models `EmailSender` (`app_email_sender`), `EmailType` (`app_email_type`, per-type routing and mode OFF/TEST/REVIEW/AUTO) and `EmailMessage` (`app_email_message`, queue row then send log keyed by `dedupe_key`, polymorphic `record_type`/`record_id`, status HELD..FAILED_TERMINAL) with enums `EmailTransportKind`, `EmailTypeMode`, `EmailMessageStatus`. No earlier model or line moved, so every `S:n` above is unchanged. Nothing in the Workbench reads them; they are Portal send state, not mail evidence (Graph mail stays the evidence source). `app/api/attachments/route.ts` writes a `supersedes` RecordLink after a pack-label attachment re-upload (non-fatal), which moves the `setDocumentMetadata` call from `:496` to `:521`. **Re-read at `6e76a5b` (`0c50cb9`, #1807 TASK-2212):** `POST /api/attachments` now gets its destination from `resolveAttachmentLocation({entityType, entityId, category, hints})` (`route.ts:150-159@6e76a5b`; `lib/storage/attachment-location.ts:125-168`). While the entity's PLX library has no active `LIBRARY_*` row in `app_storage_location`, it is today's category routing with the page's tokens; once a library is live, the server loads the record, fills the folder from it (page tokens are ignored), files a record with missing names in `INBOX`, and returns 404 for an unknown record. The tokens stored on the row and passed to `buildAttachmentMetadata` are those resolved tokens (`:178`, call now at `:529`). `Attachment.driveId`/`sharepointItemId` stay the storage key; the Workbench keys files by (driveId, itemId) and is unaffected.
- Owners: `lib/services/document-governance.ts` (`MASTER_FOLDERS:12` 01-Onboarding…08-Archive; `setDocumentMetadata:118` writes SharePoint
  columns DocumentType, ReviewStatus, ExpiryDate, CustomerRef, ProjectRef, RetentionCategory, PartyType, PartyRef, DocumentCategory, ItemRef,
  DocStatus, Source, Confidentiality, and since `2167c63` TASK-2222 ProductRef and OrderRef (the finished-product code and the customer PO
  number, `SalesOrder.poReference`, walked from `app_record_link` by `lib/record-links/file-refs.ts`; empty when ambiguous, never cleared);
  `setDocumentCustomerVisible` separately PATCHes boolean CustomerVisible), `lib/documents/metadata.ts`, `lib/sharepoint/sharepoint-metadata.ts`, taxonomy spec
  `docs/projects/document-metadata-model/SPEC.md`. Since TASK-2099/2164 staff open files through the Portal, not SharePoint links
  (`7ee5697` library/folder map → `GET /api/mrp/documents/[id]/file`, `GET /api/attachments/[id]`; `9d1115b` admin queue; `fdaa88f`
  receiving CoA/CoC/packing-slip PDFs, which are folder files with no `Document` row — see §3.5). Checked 2026-10-01 at `487862b`.
- **driveId on documents (re-read 2026-10-01 at `c3ffda3`, #1773 TASK-2210).** `lib/documents/metadata.ts@c3ffda3` adds
  `customerDriveIdFromMeta(metadata)` (reads `Document.metadata.customerDriveId` if a string) and passes it as the drive argument to the SharePoint
  metadata push in `updateDocumentMetadata` and `pushSharePointMetadataForDocument`; before, the push used no per-document drive. Read side:
  `app/api/mrp/documents/[id]/file/route.ts:45@c3ffda3` picks `planes.internalDriveId` or `planes.customerDriveId`. **Re-read at `fa46f2d` (`21de0b4`, #1804 TASK-2211):** `updateDocumentMetadata` now merges the tag edit into the stored `Document.metadata` instead of replacing it: keys of the old taxonomy metadata are removed first (so clearing an optional field works), every other key (persisted SharePoint locations such as `customerDriveId`) is kept. A tag edit therefore no longer drops the drive, and `customerDriveIdFromMeta` stays valid after one. A SharePoint item id is
  meaningful only with its drive, so key a document by (driveId, itemId). The Workbench already does (`integrations/graph_evidence.py:631`).
- **CustomerVisible (re-read 2026-10-05 at `8e3a951`, `0ef0472` #1815 TASK-2214).** `Document.customerVisible` (`app_document.customer_visible`,
  default false, back to default true at `ce86564` #1860 TASK-2346 until production runs the #1815 code; both upload routes always send it, index `(customerId, customerVisible)`) decides whether a file shows on the customer's Documents and Approvals pages; customer
  uploads set it, staff tick it at upload. `pushSharePointMetadataForDocument` mirrors it to the SharePoint boolean column `CustomerVisible`
  (`columns.mjs` TARGET_COLUMNS) in a separate PATCH after `setDocumentMetadata`; a failed push is logged and never changes customer visibility,
  because the Portal database stays the source of truth. The Workbench mirrors the column (`operating/doc_metadata_taxonomy.py`), reads it as a
  boolean and never proposes or confirms it. **Staging-only test lots (`8e3a951` #1839 TASK-2336):** the schema appends `AppTestLot`
  (`app_test_lot`, TEST- lot numbers and lab ids, origin `portal_test`, expires after 14 days) and `AppTestLotAllocation`
  (`app_test_lot_allocation`, batch ids with no FK). They are Portal test data, never FileMaker data; the Workbench reads neither.
- **Storage finalize queue (re-read 2026-10-05 at `acc0c53`, `4464088` #1811 TASK-2250).** `POST /api/attachments` no longer tags SharePoint
  columns fail-open in `after()`. The `Attachment` create now nests one `StorageFinalizeJob` (`app_storage_finalize_job`, S:8633@acc0c53;
  dedupe key `ATTACHMENT_FINALIZE:<driveId>:<itemId>`, `metadata` JSON = the columns to write, status PENDING/LEASED/SUCCEEDED/
  FAILED_RETRYABLE/FAILED_TERMINAL, 8 attempts, lease, backoff 1 min to 1 h) (`app/api/attachments/route.ts:437@acc0c53`). The payload is
  `documentMetadataFields(buildAttachmentMetadata({entityType, category, tokens}))`: `document-governance.ts:120-138@acc0c53` is the old
  `setDocumentMetadata` field map moved verbatim into its own function (same 13 columns, populated values only), so the written values
  do not change. The worker (`lib/storage/finalize-worker.ts`, `finalize-graph.ts`) PATCHes `listItem/fields`, then PATCHes the driveItem
  `retentionLabel` when `SHAREPOINT_RETENTION_LABELS` maps the category (empty until IT publishes Purview labels, TASK-2207), reads both back,
  stores Graph's `quickXorHash` and the label on the new `Attachment.quick_xor_hash`/`retention_label` (S:5528, S:5531@acc0c53) and sets
  `storage_state` READY; a 400, a 404 on the item or the last attempt sets FAILED. `after()` runs the upload's own job and sweeps 3 due ones;
  pack-label proofs, never tagged before, now wait for a sweep. Reporting API: neither `app_attachment` nor `app_storage_finalize_job` is in
  `services/workbench-api/manifest/allowlist.json@acc0c53`, so the Workbench cannot see job or storage state. Impact on Workbench reads:
  a Portal upload's SharePoint columns may be absent for minutes to about two hours (or for good after FAILED), so an absent column stays
  "empty or not provisioned" and never proof of an untagged upload (`operating/doc_metadata.py`); originals keep their (driveId, itemId)
  key and bytes, and the Workbench does not read retention labels. Migration `20261004120100_storage_finalize_queue` is staging/UAT only.
  New write-on-GET: `GET /api/cron/storage-finalize` (Bearer `CRON_SECRET`, drains the queue with Graph writes): never call (§8.1). Since `324b9f7` (re-read at `2cb3760`) `portal/vercel.json` runs it every 5 minutes (Vercel Cron, release deployment only; each upload still finalizes its own job and sweeps three more), so a FAILED job is retried without an upload; the absent-column rule above is unchanged.
  **Re-read at `790256c` (`bd5b343` #1844 TASK-2352):** a job pins its target at enqueue in two new nullable columns, `drive_id` (null = the
  default drive) and `sharepoint_item_id` (null = parsed from `dedupe_key`); the worker reads and writes only that item, never the attachment's
  current pointer. The values written do not change and the table is still not served: no Workbench change. The enqueue is now `route.ts:444@790256c`.
- **Ship-release files (re-read 2026-10-07 at `8f9009d`, #1911 TASK-2399).** A customer's release request (email, pick-up
  sheet, case labels) is stored on the ship release through the same `/api/attachments` doors: `entityType=SHIP_RELEASE`,
  `entityId = app_ship_release.id`, no new column (`lib/attachments/ship-release.ts`). Both doors store the canonical type
  (`canonicalEntityType`: `ShipRelease`, `ship_release`, `SHIP_RELEASE` → `SHIP_RELEASE`; any other type unchanged), refuse a release that
  does not exist with 404 before any SharePoint call (`checkAttachmentTarget`), and keep an optional `sourceNote` (trimmed, cut to 500) on
  the upload's `AuditLog.metadata`; `GET` of a release lists each file with its `sourceNote`. Routing: `entity-routing.ts:198-201@549f6f0`
  files `CUSTOMER_EMAIL`, `PICKUP_SHEET`, `CASE_LABELS` to Supply Chain `Customers/{customerFolder}/{ref}` (ref = release number,
  `entity-record.ts:shipRelease`); other categories on a release go to Inbox `Unfiled`. `attachment-metadata.ts` adds `SHIP_RELEASE` to
  `CUSTOMER_ENTITIES`, so the columns carry PartyType Customer. The metadata payload is `route.ts:494@8f9009d`. Workbench mirror:
  `operating/doc_metadata_taxonomy.py` (`canonical_entity_type`; `ENTITY_ROUTES`, the whole table as data, Finance rows included;
  the routing function and the blank-folder-to-Inbox rule of `attachment-location.ts@71f7214:97-102,161-182` are not mirrored
  (re-read at `71f7214`: `2e36901` TASK-2835 makes the legacy path, no library binding, take the record's own names when it loads and drop a
  hint equal to the record id, `legacyLocation:104-122`),
  since nothing in the Workbench files a document); no Portal page offers the
  upload yet, so no `ENTITY_CONTEXTS` entry. **Re-read at `549f6f0`:** `ccab693` #1942 TASK-2506 adds
  `FORMULA_SHEET` under `FORMULA`, filed to Quality `Formulas/{ref}` (uploaded from the R&D formula Docs tab); `116a625` #1973
  TASK-2488 adds `SDS`, `TDS`, `SPEC_SHEET`, `COA`, `REGULATORY_CERT`, `ALLERGEN` under `SOURCING_REQUEST`, filed to Supply Chain
  `Sourcing/{ref}` for a request that names no raw material yet (one that does files them on the material). The Finance rows sit
  one line lower (`:211,234-237`); both are mirrored in `ENTITY_ROUTES`.
- **File clearance (re-read 2026-10-05 at `790256c`, `da44b78` #1845 TASK-2213).** The Portal streams bytes with an app-only token, so it now
  copies SharePoint's read lists itself, decided by `securityClass` (TASK-2249), never by category text: FINANCE is read by Finance and Executive,
  BUSINESS_CENTRAL by Finance, Executive and IT (membership read fresh from `app_user_department_membership`; a read error answers 503).
  `GET /api/attachments` leaves such rows out (`filterReadableAttachments`, as for another user's My Bench file: no 403, so existence is not
  confirmed), and `updateDocumentMetadata(…, clearance)` in `lib/documents/metadata.ts@790256c` reads a restricted row as "Document not found"
  (still so at `f3e3a53`, `metadata.ts:78`; the update now runs in one transaction with finalize locks).
  No schema change. Workbench impact: it reads no `security_class` and writes no tags; its SharePoint originals are read under default-deny
  audience grants, so an audience that grants a drive or folder holding Finance or Business Central files must not reach a reader the Portal
  would refuse (an audience-author rule, not yet enforced from `security_class`).
  **QC documents (re-read at `1d3eaf2`, `e2728afe6` TASK-1929):** a BATCH attachment of category `COA` or `WAIVER`
  (`lib/attachments/qc-document-categories.ts@1d3eaf2:8-10,27-32`, case-blind) is read only by an admin or by staff with an active
  `QUALITY`/`QA`/`QC` department membership (`:13`; `lib/attachments/qc-documents.ts`), the uploader included; others get 404
  (`access.ts@1d3eaf2:88-93`), a membership read error 503 or a failed list (`:136-140,165-169`); `POST /api/attachments` refuses such an
  upload first (`decideQcDocumentUpload`, `route.ts@1d3eaf2:273`). Both file in the QUALITY library `Batches/{year}/{ref}` (`entity-routing.ts:177`).
  Mirrored: `EmployeeEntitlement.file_class_allows` (`operating/employee_permissions.py:725`) refuses a captured
  drive item that may be a QC document (`doc_metadata_taxonomy.may_be_qc_document`, `:635`: a `Batches` folder on its path, any library and depth,
  and a DocumentCategory column outside `OPEN_BATCH_COLUMNS`, i.e. anything but `BATCH_RECORD`, or none captured) unless `qc_document_reader`
  holds (`employee_permissions.py:901`): an admin in both Portal modes (role ADMIN or higher and access level L4 or higher), or a Portal-confirmed
  active `QUALITY`/`QA`/`QC` membership the Entra department also names. An unread membership or directory, or an eval principal, reads none; it
  narrows on top of the security class. SharePoint holds only the short column, never the fine category: a `WAIVER` writes `OTHER` and a `COA`
  writes `COA` (`document-types.ts@1d3eaf2:28,93`), so the column cannot tell them from open Batches files. **Known limitation (fail closed):**
  the open `QC_RESULT`, `LAB_RESULT` and `IN_PROCESS_QC` files (column `OTHER`) and issued CoAs (`ISSUED_COA`, column `COA`) in a `Batches`
  folder are refused to non-QC readers, though the Portal serves them to all staff; the stored-name code (`WVR`) is not used to open a file,
  since a rename defeats it. The
  `access.ts` re-pin itself (bytes at `52c9116`) is pending; the pin stays at `0323e19c` until then (§7.14).
- **Gold SO lifecycle (pinned `lib/sales-order/order-lifecycle.ts@c3ffda3`, introduced `9d9ce19` #1770 TASK-2244).** `buildOrderLifecycle(facts)`
  is Prisma-free and header-`fulfillmentMode` only (MAKE or DISTRIBUTE; mixed is forbidden, one lane). Head: Quote (done, "Converted") then Sales order
  (done once status is in CONFIRMED, IN_PRODUCTION, READY, READY_TO_SHIP, PARTIALLY_SHIPPED, SHIPPED). MAKE lane: Manufacturing order (done when all
  non-CANCELLED MOs are COMPLETED/REALIZED/CLOSED), Release order (done when all are FULFILLED), Pick list (done when all COMPLETED). DISTRIBUTE lane:
  Pick list only (MRP-M-154: Dist skips Release). Pick gate: DISTRIBUTE clears once the SO is confirmed; MAKE clears when any non-cancelled ship release
  is RELEASED or FULFILLED. The earliest not-done step is marked current. Backs the future plx.process tool. **Re-read at `38cd3f3` (`999dac8`
  TASK-2396):** both lanes end with two more steps, Shipment (done once the SO status is SHIPPED or COMPLETE, "Partially shipped" at PARTIALLY_SHIPPED)
  and Complete (SO COMPLETE), and SO_CONFIRMED now holds COMPLETE; those statuses come only from the close-out evaluator (§3.2), so the rail's
  Shipment step means "every line shipped or short-closed", not "a shipment went out". **Stale against the confirm route:** the rail still treats DISTRIBUTE as skipping Release, but `shipments/[id]/confirm/route.ts@59b0548` gates per shipment line (`shipmentSkipsShipRelease`): a DISTRIBUTE order with any MAKE line needs a release to ship. The Workbench models the confirm gate (`sales_order_lifecycle.json`) and cites both. **List path (`load-order-lifecycle.ts@59b0548`):** `attachListLifecycles` batches the Orders list: it resolves FM rows to gold by `NULLIF(TRIM(custom_fields->>'fmId'), '')`, and unions releases found by `orderRef` (the trimmed FM id) with the order's own `shipReleases`, deduplicated by release id. `loadOrderLifecycle` (single order) is unchanged. `order-lifecycle.ts` gained UI helpers only (`LIFECYCLE_STEP_FILTERS`, `toOrderListLifecycle`). **FileMaker order page (re-read 2026-10-05 at `790256c`, `f4437e8` #1847 MRP-M-154):** `/mrp/orders/[id]` (`order-detail.tsx@790256c`, the rail docks right; the attachment ref token moved to `:833`) now shows the rail from `loadFmOrderLifecycle`: the gold rail when a SalesOrder's `fmId` bridges the FM order, otherwise `synthesizeFmOrderLifecycle`, a MAKE lane built from the OE header status (blank = DRAFT) with ship releases found by `orderRef` = the FM id; a read error omits the rail. The rail tests that status against SO_CONFIRMED, which holds no FM status text, so it shows every unbridged FM order unconfirmed. The Workbench graph mirrors the FM root in its own `sales_order` evidence on `fm_status` (Open and Closed done, Cancelled no order, like the gold OPEN/COMPLETE choice) and keeps the variant unknown, since FM carries no fulfillment mode. **Re-read at `12db891`:** `651d07a` #1851 TASK-2354 only points the synthesized rail's Sales order and Release links at `/mrp/orders/<fm id>` (`links` override); `75c7d6d` #1850 changed the Commercial tab text (ref token now `order-detail.tsx:832@12db891`). No lifecycle rule changed. **Sage Order tab (re-read at `549f6f0`, `8b08c9e` #1936 TASK-2503):** the page header now shows the order number, status, path and BC finance badges, moves Create shipment there, and renders `buildSageOrderTabRows` (`lib/sales-order/fm-order-header.ts`): the Sage 300 O/E Order-tab labels filled from FM `order_entry` (number, customer id and name, PO, status, order and required dates, ship-to, comments as Description, reference) and the gold twin (lifecycle path as Order Type, release-step doc numbers as No. of Shipments / Last Shipment No., first numbered BC invoice, and On Hold, Ship Via, Calculate Tax parsed from the gold order's `customFields` commercial snapshot in `load-gold-order-panels.ts`); every other Sage field is an em dash. Display only, never read from `sage.*`; no write, gate or lifecycle rule changed. The sidebar relabels (ERP OE Code → Sales order, the gold link → Gold SO) and the ref token moved to `order-detail.tsx:892@549f6f0`, rule unchanged.
- **SharePoint site design (built; re-pointing started at `f3e3a53`; no batch run recorded at `b4118a3`).** `docs/projects/plx-sharepoint-site/README.md@bbf8afc` (#1772, TASK-2246 readiness review): decisions 1-18
  (15 waits on the licence answer); twelve libraries incl. restricted Finance and Business Central; at that pin the site was a design only ("Nothing here is built").
  Record links will live in a PostgreSQL record-link table and files will be named by `PortalFileId`, not by SharePoint item id (it changes on a
  library move). **Re-read at `fa46f2d`:** the only change is a link from the Customer Uploads / working-copy routing step to a new `customer-upload-routing.md` (`21de0b4`, #1804); that file is not pinned or read here, and the README still called the site unbuilt. **Re-read at `6e76a5b`:** migration step 7 (hardcoded paths onto storage categories) now links `portal-document-categories.md` (not pinned or read here) and notes that AI skills wait for a stored drive ID; the code half shipped as TASK-2216 (DocuSign, receiving, forms, sample release, pre-weigh, Outlook pins, exports, UAT screenshots now resolve through `app_storage_location` categories whose defaults keep today's folders). The README still called the PLX site unbuilt. **Re-read at `45673ee` (`e8d0ea0` TASK-2590, `e677b49` TASK-2594):** decision 20: the portal writes the department libraries and people only read them (Inbox and Systems take hand edits); PLXCUSTOMERS is frozen with no bulk move; SharePoint carries only the main-chain codes (no `RelatedRecords` lookup); retention and TYPE codes follow `document-types.ts`. The README now says the site is built and the portal code merged, but nothing is re-pointed: no category files into PLX yet, so the Workbench still maps no source to PLX-site libraries. Finance, Business Central and Staff Scratch stay restricted (`RESTRICTED_LIBRARY_NAMES` unchanged). **Re-read at `f3e3a53`:** README `:3` says the site is built, the portal code is merged and re-pointing has started: My Bench scratch (`BENCH_SCRATCH`) moved to PLX Staff Scratch on 2026-10-11, and the first seven library bindings run on 2026-10-17/18 under TASK-2223. `repoint/RUNBOOK.md@f3e3a53` (`ea625427` TASK-3204, TASK-2223 rev 2) splits that run into batch A (Production, Maintenance, Product Development, Inbox; TASK-3298), batch B (Quality, Supply Chain, Sales; TASK-3299) and batch C (Finance, only if its preconditions hold; TASK-3300) (`:13-15`); CUSTOMER_UPLOADS (TASK-3301) and the retention-label map (TASK-3303) are not in this run (`:16-18`). It turns on `LIBRARY_*` rows in `app_storage_location` on the UAT database behind staging.plxcustomer.io (`:35`, gate `:71`); from then on NEW uploads routed by record go to the PLX library and no existing file moves (`:36`). FACT: the runbook and the README plan the run. Nothing read here shows a batch ran; the binding rows live in `app_storage_location`, which the reporting API does not serve. The Workbench capture scope stays PLXCUSTOMERS (`integrations/sharepoint_capture_scope.json`), so a file the Portal writes into a PLX-site library after a batch runs is outside capture (residual, not a permission widening: the capture reads less, never more). **Re-read at `b4118a3`:** the earlier "the first seven library bindings run on 2026-10-17/18" is superseded: README `:3` now says batches A and B were approved to run on the UAT database on 2026-10-17/18, but no run is recorded and no new date is set (TASK-3201), and TASK-2223 is split into TASK-3298..3303. TASK-2228 (narrowing the portal to `Sites.Selected`) was cancelled on 2026-10-18: the portal keeps tenant-wide `Sites.ReadWrite.All` as an accepted risk (Permissions rule 5, Migration step 8; TASK-3199 checks the secret rotation). The folder-created webhook on PLXCUSTOMERS is retired, not moved (TASK-3187). The Permissions table is unchanged in substance; Finance, Business Central and Staff Scratch stay restricted (`:86-91`, `RESTRICTED_LIBRARY_NAMES` unchanged). New task rows include TASK-3197 (the 400-character path check, `locations.ts@b4118a3:167-199`), TASK-3207 (Outlook pins; `OUTLOOK_PIN` is mirrored in `doc_metadata_taxonomy`) and TASK-3213 (direct writers on the finalize queue; receiving uploads now record an `Attachment` row). `repoint/RUNBOOK.md@b4118a3` only adds `SHAREPOINT_LABEL_SITE_IDS` before `SHAREPOINT_RETENTION_LABELS`; the batch split and gates cited above stand. The capture scope residual is unchanged.
- **API**: none.

### 3.15 Quality record / AO QC / eQMS
- FM `statusqc` On-Hold → Released/Rejected drives FG lot release (`lib/wms/fp-bridge.ts:syncFpLotOnQc:966`, `releaseFpLot:701`, `rejectFpLot:711`).
- **AO label verification and line inspection (re-read at `71f7214`, `79e7477`/`2968132` TASK-1364, ERP-D-029):** new tables
  `app_ao_label_reconciliation_line` (#013B, one row per label stock issued to the AO: received = used + rejected + returned, Decimal(14,3)),
  `app_ao_label_verification_signoff` (append-only QA sign-off tied to an `app_electronic_signature` row), `app_ao_line_inspection` (#010,
  append-only, AQL 1 % Ac 0 / Re 1) and `app_ao_qc_release_ack`, all keyed by the bronze `assembly_order.id` with no FK and none served. A
  `PATCH /api/mrp/assembly/[id]` that moves `status_qc` to Released while label or line-inspection gaps are open answers 409
  `QC_RELEASE_ACK_REQUIRED` listing them; re-sent with `qc_release_ack.reason` it goes through and records the ack in the same transaction
  (`app/api/mrp/assembly/[id]/route.ts@71f7214:379-406`). Warn-only: a Released AO is no evidence that #013B or #010 was complete. The
  Workbench's proposed `portal.assembly_order.set_header_quantities` (registration refused) sets no `status_qc`, so nothing changes there.
- `QualityTest` (S:4080; approve sets matching lots RELEASED, reject ON_HOLD — bug risk: empty lot/batch filter releases all lots of the product,
  `app/api/operations/quality/tests/[id]/approve/route.ts:77`), `QcSpecification`, `StabilityStudy` (active writer `/api/mrp/project-development/[fmProjectId]/stability`), legacy `Deviation`/`Capa`.
- **eQMS** (new, MRP-M-275/282): `EqmsSequence` (PK `[prefix, year]`), `EqmsRecord` (S:4290; `number` `{D|CC|CAPA|QRM|CCR}-{YY}{seq:3}` issued at first
  signature; type COMPLAINT takes prefix CCR, `lib/quality/eqms/types.ts:5-6,35-36@f3e3a53`), `lib/quality/eqms/service.ts` (`createEqmsDraft:235`, `signEqmsSection:375`, `closeEqmsRecord:670`, `reauthenticate:162`, all `@f3e3a53`), `rules.ts:nextStatusAfterSign:151@f3e3a53`;
  hub `lib/quality/eqms/hub-query.ts`, `GET /api/mrp/quality/hub`; at `f3e3a53` the hub shows a complaint's QC-010 72-hour report obligation (`hub-query.ts:331-347`; the complaint's ISO time zero starts the clock, `types.ts:124`). No Workbench module mirrors eQMS. **E-signature (re-read at `59b0548`, TASK-2259):** `signEqmsSection` takes an optional `password` plus `mfaVerified` and `mfaAssertion` (`SigningMfaAssertion`, `lib/auth/signing-mfa-cookie`); `reauthenticate:150` returns `authMode` `mfaStepUp` for an Azure AD user whose MFA step-up the route verified (no password), else the password re-entry path as before. The signature audit payload adds `mfaAcrs`, `mfaAmrHasMfa`, `mfaAuthTime` when an assertion is present. **Re-read at `45673ee` (`1e392ed`, TASK-2597):** an account with no portal password can no longer sign by session acknowledgement (`pocSessionAck` is gone; 403), and MFA counts only when the assertion's `userId` is the signing actor's. No Workbench module signs or mirrors this. **Regulated approvals (re-read at `2cb3760`, `4304012` TASK-2602):** four approvals outside eQMS are now regulated signatures through `lib/services/regulated-signature.ts:recordRegulatedSignature:44`: Assembly BOM approve (`assembly-boms/[bomId]/approve/route.ts:47`, after the `assertAssemblyBomApprovable` state pre-check of `6124f55`, so a wrong-state approve leaves no signature), formula customer sign-off record (`formulas/[id]/customer-signoff/route.ts:154`; revoke is not a signature), raw and pack QC release (`receiving/{raw,pack}/[id]/release/route.ts:79`; reject is not a signature). Each records an `Approved By` `app_electronic_signature` row (entity `ASSEMBLY_BOM`, `FORMULATION`, `RECEIPT_RAW`, `RECEIPT_PACK`) through `createSignature` before the status write, after a fresh re-authentication: a verified MFA step-up whose `userId` is the signer's, else the portal password (403 step-up missing or Microsoft account with neither, 400 no password, 401 wrong password). `GET /api/auth/signing-reauth` only tells the dialog whether to show the password box. Cycle-count approval is not a signature (it posts adjustments and stamps `approvedBy`). No Workbench action, adapter or approval precondition targets these routes (`operating/action_catalog.json` proposes only `portal.sales_order.set_customer_po` and `portal.assembly_order.set_header_quantities`, neither regulated), so no precondition changes; a Workbench approval or MC gate can never stand in for this signature, and a released receipt, approved BOM or signed-off formula implies a signature row the reporting API does not serve.
- **API**: none (no quality relation is served). AO-level QC is only visible via `public.batch` (FM bronze `statusqc` not on the replica per SOURCE-NOTES).

### 3.16 User / role
- `User` → `app_user` (S:778; `role` string authoritative, `userRole` enum, `accessLevel` L1–L5, `departmentId`, `departmentRoles[]`),
  `Department`, `UserDepartmentMembership`, `UserAccessScope`, `AccessPolicyGrant`, `TemporaryElevation*`, `SecurityEvent`, `ApiKey`.
- **Department → Entra group (`2cb3760` TASK-2622, unchanged at `38cd3f3`):** a signed rollout manifest maps a department code to one managed Entra
  group (`managedGroups[].departmentCode`; the SALES ↔ PLX-Sales manifest is pending with placeholder ids, which the sync refuses); the mapping sync
  writes `Department.entraGroupId` and stamps converged membership rows PROJECTED. The Workbench never reads the mapping: its file clearance takes
  the membership rows (PORTAL or PROJECTED of an active department; DRIFT grants nothing) intersected with the employee's Entra `department`
  (`operating/employee_permissions.py`), and SALES reads no restricted class (FINANCE: FIN, EXEC; BUSINESS_CENTRAL: FIN, EXEC, IT).
- **API**: none. See §5.

### 3.17 Agent registry / approval / audit (Portal-owned; Workbench consumes ids only)
`Agent` (S:7356, `app_agent`, unique `slug`), `AgentVersion` (S:7390; `loops`, `mcpAllowList`), `AgentAccess`, `AgentThread`, `AgentMessage`,
`AgentAuditLog` (S:7516), `AgentRun` (S:7559, `app_agent_run`; `correlationId` unique; status QUEUED, AWAITING_APPROVAL, RUNNING, SUCCEEDED,
FAILED, DECLINED, EXPIRED, SKIPPED_LOCKED, KILLED; `approvedById/approvedAt/approvedVersionId`), `AgentLoopState`, `AgentEval*`, `KnowledgeGap`.
Code: `lib/agents/registry.ts`, `lib/agents/runner/approval-decision.ts:decideRunApproval:92` (writes approval bound to `run.versionId`),
`runner/runs.ts:approvalIsValid:660`, `lib/agents/audit.ts` (`writeAgentAuditInTx:102` fail-closed, `logAgentAudit:58` swallow),
runner API `app/api/runner/v1/{agents,approvals,brief-inputs,runs,runs/[id]/heartbeat,runs/[id]/status}` (merged `eceb50c37`, one runner token,
`lib/agents/runner/auth.ts:isRunnerTokenValid:18`). Not in the reporting API.
**Approval records (re-read at `38cd3f3`, `5f7b299` TASK-2220):** `ApprovalRecord` → `app_approval_record` (one row per approval of a record, typed by
a `PolicyApprovalType` key such as `formula.sample_release` / `formula.approval`, with the approved file version, approver id or DocuSign signer name and
email, `approved_at`, `source` docusign | portal | fm-flag | email, and a unique `idempotency_key`), written by `lib/approvals/approval-record.ts` from
the formula sample DocuSign completion and the formula customer sign-off, read by the Document trail and PD evidence. It records that a business
approval happened; an `fm-flag` row is a FileMaker flag, not a signature. Not served, and never an authority for a Workbench action (§8.2).

## 4. Business logic to reuse / mirror exactly

Rule: the Workbench may **read** these results or **recompute** them for evidence, but a recomputation must match
these definitions line for line and must cite this commit. Never present a Workbench recomputation as Portal truth.

### 4.1 Landed cost / project landed quote — `lib/costing/project-landed-quote.ts`
- Engine `quoteProjectLanded(projectRef, {askedVolumes?, persist?}):1585` (at `487862b`) — the one costing engine (standard-cost README "Revised 2026-08-23").
  **It writes by default**: `persist` (TASK-2119, `38a31ea`, `:1194`) skips the snapshot write (`:1684`) and the primary-routing setup-minute fill
  (`:1600` → `fg-routing-store.ts:refreshPrimaryRoutingSetup:251`) only when exactly `false`; only `scripts/recalc-theoretical-fg.ts:134` passes it.
  Never call it; see `docs/reference/source-notes.md` "GET routes that WRITE".
  Facts `gatherQuoteFacts:516`; project ref `resolveQuoteProjectRef:364` (gold by id|projectKey, else ProjectLocal).
  **Since TASK-2809 (`4dc5cdf`, read at `4243499`)** the engine is load / price / write: `lib/costing/quote-inputs.ts:loadQuoteInputs:106`
  does every read (one setup-standards read; failed priced-input reads listed in `readErrors`; `persist` reaches
  `getPrimaryRouting` at `:116`), `priceQuoteInputs:2070` prices unchanged, and `runQuoteProjectLanded:2335` writes the snapshot
  (`writeCostSnapshot:2302`) unless `persist === false` (`:2350`); `quoteProjectLanded:2362` is the facade with the same result.
  TASK-2810 (`4f8c905`) adds `POST /api/mrp/projects/[id]/costing/preview` (`persist: false`, no transaction, writes nothing):
  a POST, so never a Workbench read. The Workbench port's arithmetic is unchanged (`operating/landed_cost.py` header).
  **Re-read at `1f2d42d` (2026-10-14):** `loadPrimaryRoutingSafe` always passes `persist: false` (`quote-inputs.ts@1f2d42d:90-101`,
  `9108064` TASK-2850 CT-B), and since `255ec2a` (TASK-2863 RV2a) `fg-routing-store.ts@1f2d42d:267-302` fills blank routing
  setup minutes in memory on every read (`getPrimaryRouting` ignores `persist`), so no engine run, AO setup, activation or
  `GET /api/mrp/fg-routing` saves routing minutes any more; only the snapshot guard (`project-landed-quote.ts@1f2d42d:2351`)
  still writes, unless `persist === false`. The recalc script (`scripts/recalc-theoretical-fg.ts@1f2d42d:140`, `persist: false`)
  now skips a frozen (ACTIVE/RETIRED) FG on `--apply`. `b236bef` (TASK-2859 CT-H) stops `draft-cost-persist.ts` writing
  `overheadPerUnit` and non-compounding `yieldUnits`; stored values stay and the engine still reads an assembly row's
  `yieldUnits` (`resolveExpectedUnitsOverride:401`). Both harnesses re-run on `1f2d42d`: every golden price is unchanged; one
  census label changed in `ready-to-quote.ts:461` (`35cfb18` TASK-2811), ported at `d4d1bb2` (§4.11).
  **Re-read at `d53137e` (2026-10-16):** the recalc dry run is `runQuoteProjectLanded(ref, {persist: false})`
  (`scripts/recalc-theoretical-fg.ts@d53137e:152`), its quote `landedQuoteOf(run.priced)`. Since `335dc8a` (TASK-2878, CT-E2)
  `--apply` writes (`quoteProjectLanded(ref)`, persisting) the changed rows and also the `restamp` rows: a draft FG whose costs did
  not move but whose stored fingerprint is missing or differs from the live one (`theoretical-fg-recalc.ts@d53137e:96-110`
  `recalcStatus`), so a snapshot can be rewritten with no cost change. A frozen FG is still skipped (`:18-20`). §7.9.
- Raw materials: `inclusionPct × rawCostPerKg(product)` (since `bc6e8de` TASK-2486, `lib/costing/raw-cost.ts:49@5cb95e1`, `gatherQuoteFacts:700@5cb95e1`):
  0 when the customer supplies the raw (G9, `77f1ee6`, §7.1), else
  `Product.unitCost` when > 0, else the newest preferred active card's `systemCost` (CAD/kg), else 0; treated as $/kg (no supplier
  tier, no MOQ, no currency conversion, no UoM conversion). Ported with the hours, formula pin and BOM selection in `operating/landed_cost.py`.
- Assembly BOM `findAssemblyBomForQuote:440` (ProjectSubAssembly pin, else newest ASSEMBLY BOM by version desc); fill weight `bom.fillWeightG`.
- Packaging line: `qty = line.quantity || 1` (0 becomes 1). Linked = `productId && !needsSourcing && estimatedUnitCost == null`; card =
  first active SupplierProduct by `isPreferred desc, updatedAt desc`; unlinked uses `estimatedUnitCost ?? cost ?? 0` without uplift.
- **Landed component** `lib/costing/landed-cost.ts:landedComponentCost:47`:
  `duty = round4(tierPrice × 0.065)`; `landed = round4((tierPrice + duty) × 1.38 + freight)` with `freight = SupplierProduct.deliveryCost`
  per unit. No caller passes `dutyRate`/`multiplier`, so `RateSettings.tariffRate/landedMultiplier/fxRateCadUsd` have NO effect.
  HS code / origin only raise census warnings.
- **Unit cost** `lib/costing/cost-estimate.ts:estimateUnitCost:120`:
  `fillKg = fillWeightG/1000` (engine passes `fillMassKg(g, SG)×1000`, `landed-cost.ts:fillMassKg:74` = g/1000×SG, SG default 1);
  `bulkPerKg = Σ(incl%/100 × costPerKg) × (1 + rawMaterialsAdjustmentRate/100)` (default 10);
  `bulk = bulkPerKg × fillKg`; `packaging = Σ qtyPerUnit × landedUnitCost`;
  `assemblyLab = asmHrs × 20.90`, `compoundingLab = cmpHrs × 32.00`, `overhead = (overheadHrs ?? asmHrs) × 31.30` (RateSettings defaults);
  `total = bulk+pkg+cmpLab+asmLab+OH`; `sell = total/(1 − m/100)` (m<100). Outputs `round2`, total computed from unrounded parts.
- Hours `resolveQuoteHours:717`: tier by `pickRunVolumeTierForQty(runQty ?? FgItem.targetQty)` (5k <10k ≤ 10k <25k ≤ 25k <50k ≤ 50k; <5000 → 5k;
  default "5k"); draft override row `labourPerUnit` or `headcount/(bpm×60)`; routing fallback `fg-routing.ts:quoteLabourFromRouting:137`;
  setup `setup-standards.ts:amortiseSetupHours:208`; missing hours → source "missing", priced 0, stored NULL (never a 1-hour default).
- Margin `lib/costing/margin-ladder.ts`: margin on price `cost/(1−m)`; ladder `RateSettings.marginTiersPct` [25,30,35,40]; target `marginPercent` 30.
- Volume matrix `buildVolumeMatrixColumn:1283` uses supplier price breaks (`pickSupplierCardTierPrice:879`: highest `minQty ≤ demand`); the
  snapshot path does NOT (base card price) — the snapshot matches no matrix column.
- Snapshot writer `writeTheoreticalFgSnapshot` (`lib/project-development/theoretical-fg-identity.ts:216`) to `app_theoretical_fg`; skipped when formula
  `pin_unresolved`; `assemblyCost` column holds PACKAGING cost; `componentScrapPct`, `batchYieldPct` always NULL (no waste/yield anywhere).
  Second writer exists: `lib/services/ai/price-estimator.ts:114-165`.
- **Ready-to-quote census** `lib/costing/ready-to-quote.ts:deriveReadyToQuote:274`: blockers = `fill_weight_g` missing, any unsourced line;
  warnings = SG missing ("Defaulting to 1.00"), HS code missing, incoterms/origin missing, labour missing. `ready` = no blocker.
- Audit evidence (no price effect): last PO freight allocation `allocatePoFreightPerUnit:915` (value-weighted), stock average.

### 4.2 Quote totals — `lib/quote/totals.ts`
`round2(n) = Math.round((n+EPSILON)×100)/100`; `clampMarginPercent` 0..99.9; `deriveUnitPrice(std, m) = round2(std/(1−m/100))`;
`resolveUnitPrice(line)` = derived when `stdCost>0 && marginPercent!=null`, else `round2(unitPrice??0)`; `lineTotal = round2(price×qty)`;
`computeQuoteTotals(lines, tax%) → subtotal = round2(Σ)`, `tax = round2(subtotal×clamp(tax)/100)`, `total = round2(subtotal+tax)`.
No discounts/volume tiers here; TASK-2175 matrix fallback (`lib/quote/matrix-fallback.ts@fc13a4a`) feeds `unitCost`→`stdCost`, see §3.12; tax rate not stored (re-derived `taxAmount/subtotal×100`, `service.ts:427` at `487862b`). Offer margin
`lib/quote/matrix.ts:resolveOfferMarginPct:149`. Sales-order totals are separate: `lib/sales-order/totals.ts` (Sage order discount reduces SO total, `38b22226b`).

### 4.3 Standard cost
- Stored only on `Product.standardCost` (+`unitCost`); set at FG activation from the snapshot (`lib/final-products/fg-activation.ts:777`,
  `fg-activation-store.ts:637`); distribution items from user input. No standard-cost table.
- Quote "stdCost" = `QuoteLine.customFields.stdCost`, seeded from `TheoreticalFG.totalCost` (`lib/project-development/quote-picker.ts:88-98`).
- BC: item `unitCost = unit_cost ?? standard_cost` and **`unitPrice = standard_cost`** (`serializers.ts:881-883`); PO cost anchor
  `purchase-line-cost.ts:107` (`standardCost ?? averageCost ?? unitCost`, ±50%); journal cost lot → standard → unit (`inventory-journal-sync.ts:99,224`).
- Actuals: `lib/costing/conversion-cost.ts@e3f8f19:143` (`EmployeeRate.hourly_rate × (1+adj/100)`), shared by batch/AO/backflush (`93fcd0b34`).
  Books overhead on compounding (`e3f8f19` #1913 TASK-1504, L2 / ERP-D-013 G12, `batchProdCostsCtes` `fm_prod` `:187-209`): per FM
  `production` row `costoh_c` when non-zero, else (`costrateoh` ?? `app_rate_settings.overhead_per_production_hour`) × `hourstotal_c`, so
  the books roll-up no longer charges compounding zero overhead. This amends OD-14 for books only; quotes keep OD-14 (compounding
  overhead 0, §4.1 `overhead = (overheadHrs ?? asmHrs) × 31.30`; `rate-masters.ts@e3f8f19:19-22`, `overheadRateFromSettings:71`).
- Run-size variance stays in Portal; costing method all Average (ERP-D-013 amendment).

### 4.4 Order identity and customer PO — §3.3, §3.4. Mirror `resolveOrderByNumber` precedence (order_number, then customer plus normalized po_reference; a PO without its customer names no order).

### 4.5 Units / UOM
- Line UOMs are free strings; no conversion on orders/shipments (`lib/sales-order/ship-to.ts:defaultUom:156`); BC line payloads carry no UOM (INFERENCE).
- `lib/wms/uom-barcode.ts`: `toLedgerEaches:42` (cases × unitsPerCase), `casesFromEaches:102`, `effectiveUnitsPerCase:81` (scanned barcode wins).
- `lib/mrp/apply-po-line-receive.ts:receiveQtyToPurchaseUom:58` (kg ÷ FM `kgperuofm_c`; aliases bag/drum/unit only).
- `lib/costing/fill-weight-from-size.ts:magnitudeToGrams` (L×1000, gal×3785.41, oz×29.5735, kg×1000, ml/g×1; bare "G" = gallons if ≤5 else grams), `fillMassKg` (SG).
- `lib/business-central/serializers.ts:bcUomCode:726` / `BC_UOM_CODE` (EA→PCS, CS/CASE→CSE, G→GR; default PCS); Pack and Dozen added `6f76f91e6`, `959a56a93`.
- 3PL: `lib/3pl/gold-activity.ts`, `lib/3pl/sage-etl.ts:sqtyuomAdj:355`.

### 4.6 FEFO (as implemented, not as the schema comment claims)
- True FEFO only on shipment picks: `app/api/mrp/wms/shipments/[id]/pick-list/route.ts` (filter on-hand>0, binned, status in
  `PICKABLE_LOT_STATUSES = [RELEASED, QUARANTINE, ON_HOLD]` `lib/inventory/inventory-site-lots.ts:90`; sort `expiryDate ASC NULLS LAST,
  receivedDate ASC NULLS LAST`; lots of the line's AO first; pass 1 RELEASED, pass 2 QC-hold only if allowed). Same in `pick-lists/consolidated`.
  Expired-date lots still RELEASED are not excluded.
- AO/batch pick lists `lib/wms/resolve-pick-bins.ts:resolvePickBins:71`: FM-current bin → larger FM qty → larger available → expiry → lot (FEFO is 4th).
- Batch wizard `app/api/mrp/batching/create/route.ts:getIngredientsWithLots:327`: expired last, then **FIFO** by inventory date (OpenAPI calls it FEFO).
  Since TASK-2336 (`@8e3a951:387-517, 535-581`): where test receiving is on (staging only, `lib/mrp/test-receiving/env.ts:isTestReceivingEnabled`)
  the picker reads FM lots UNION released `app_test_lot` rows in FEFO order (`lib/mrp/test-receiving/picker-sql.ts`); elsewhere FIFO as above with
  test lots dropped. A batch using a test lot writes `batch`/`batch_item` and `app_test_lot_allocation` in one transaction; off staging it is refused (403).
  **Test batch ids (re-read 2026-10-05 at `acc0c53`, #1843 TASK-2351):** off the live customer site a batch that uses a test lot takes
  `public.batch`/`batch_item` ids >= 9,000,000 and number `TEST-BC-{YY}{DOY}{letter}`; other batches take ids and day letters only from rows
  below that floor (`create/route.ts:133-173,211@acc0c53`, `lib/mrp/test-receiving/batch-ids.ts:29-31`; the same range in `batch-formula-sync.ts:85`,
  the item `POST` and the reschedule renumber). Production keeps the original SQL. `public.batch` is served and `silver.batches` copies every row
  into gold `app_batch_record`, so a test batch can reach the Workbench: `ontology/schema/domains/manufacturing.json` reads `public.batch` ids
  >= 9000000 and `app_batch_record.batch_name` `TEST-BC-*` as `gold_native_staging`, and the `batch_code` grammar never parses `TEST-BC-…`
  (its lookbehind refuses a leading `-`), so a test batch never anchors an FM batch code. No new route; all writes are POST/PATCH.
  **Split lots (re-read at `71f7214`, `0c008ee` TASK-2721):** a material's lot rows in `public.batch_item` now partition the material need: the
  create route validates every split first (`splitLotQuantities`, exact decimals, shares > 0 that sum exactly to `qty_theoretical`; no shares =
  even four-place shares, remainder on the last lot; 400 `invalid_split_quantity`) and writes the extra rows in the batch transaction
  (`create/route.ts@71f7214:138-146,256-262`), extras at 0 %. Older rows do not: `lib/inventory/split-share-detector.mjs` reads two legacy shapes
  where every lot row repeats the whole need (copied-quantity: extras at 0 %; copied-row: FileMaker copied the full % and Th QTY, e.g. batch 11475)
  and counts them once (`countedShareTotal`). The Workbench binds no `batch_item` quantity today; one that does must not sum a material's lot rows
  without that test.
  **Lots from Lab IDs (re-read at `1d3eaf2`, `0af01114c`):** the `batch_item` INSERT now also writes `idlabid` from the selected lot's lab id
  (`create/route.ts@1d3eaf2:225,240`). A batch created at a release status runs `fillLotsOnRelease` after the transaction, best-effort
  (`:335-341`): it fills a blank `lotnoth` from the Lab ID (`inventory_lab_test.lotno`, then `receiptraw.lotnoqc`, then the batch name or lot) and
  never overwrites a lot; the outcome comes back as `lotFill`/`lotFillError`. The default status (`Allocated`, `:285`) and the test-batch id range
  are unchanged. The Workbench binds no `batch_item` lot or Lab ID, so `manufacturing.json` stands; a binding of `lotnoth` must expect it to be
  filled after the batch row is written, not with it.

### 4.7 Formulation percent rules
- One definition: `lib/formulas/line-percent.ts` (TASK-2108/2110/2117): % = `COALESCE(percfinalwpercent_c, perctentry×100)` (`formulaLinePercentSql:40`);
  fraction = `COALESCE(percfinalwpercent_c×0.01, perctentry)` (`formulaLineFractionSql:49`); non-numeric text → NULL (`NUMERIC_LITERAL_PATTERN`).
  `batch_item.percentage` and `silver.formula_items.percentage` store the 0–1 fraction; display ×100 (`formatLineFraction:60`).
  A guard test forbids reading `perctentry` elsewhere (`lib/formulas/__tests__/perctentry-guard.test.ts`).
- Quantities: theoretical `qtyth_c = ROUND(fraction × batch.qty, 4)` (`lib/inventory/batch-formula-sync.ts:syncBatchFormulaItems:22@acc0c53`, unchanged; only its `batch_item` id range moved);
  WMS needs `(pct/100) × batchQty` rounded 2 dp (`lib/wms/source-needs.ts:112-125`; `@4243499:127-141`). Since `6243bb9` (MRP-M-046) a BC
  batch card's pick needs also carry the card's lab id / lot from `silver.batch_items` (`lot_number`, else `lot_in_system`), overlaid on gold
  rows with no pin (`overlayMissingSourceLots`); quantities are unchanged. Multi-lot FIFO selection at batch create (TASK-1777, `bb3760f`) lives in
  the batching route and `lib/inventory/batch-split-lots.ts`, not here.
- Sum-to-100: bronze `lib/formulas/cost.ts` `FORMULA_TOTAL_TARGET=100`, `FORMULA_TOTAL_TOLERANCE=0.01` (`isFormulaTotalComplete:181`), enforced
  only on R&D→Final (`app/api/mrp/formulas/[id]/status/route.ts:84-100`; its regex ignores `.5`/`7.5e-05` values — INFERENCE bug);
  gold `lib/services/formulation-bom.ts` tolerance 0.1, per-line 0..100, no duplicate products. Water balance `cost.ts:computeWaterPercentage:224`.
- Versioning: one Final per bulk; going Final demotes the other to `revised`; new versions by `POST /api/mrp/formulas/[id]/duplicate`
  (`new_number` or `letter_revision`); versioned BOMs via `createAssemblyBomVersion` (never edit APPROVED).

### 4.8 AO quantity fields (Portal meaning)
| FM bronze | Gold / silver | Meaning |
|---|---|---|
| `assembly_order.qtytotal` | MO `quantity` | ordered qty at create |
| `sizepieces` / `sizeboxes` (= pieces ÷ unitsperbox, 2 dp) | `sizePieces/Boxes` | ordered pieces / boxes |
| `sizesystem_c` | `sizeSystem` | FM computed ordered qty (fallback) |
| `totalprodqty_c` (= `qtymfgprod_c`) | — (not in API) | total produced incl. samples |
| `qtysample` | `qtySample` | samples |
| `qtyacc_c` | — (not in API) | accepted = produced − samples → inventory + BC output |
| `qtyleftpieces_c`, `qtymanufbox_c`, `qtymfgleftoe_c` | — | left / boxes made / remaining per order entry |
| `finalqty`, `qtymanufold` | `yieldActual`, `yieldExpected` | silver mapping |
| `assembly_order_item.qty_c` / `actualamount` | silver `assembly_order_items.quantity` / `actual_amount` | planned component qty / consumption = `qty_per_batch × totalprodqty_c` (`lib/inventory/assembly-lifecycle.ts:cascadeAssemblyActualQty:198`) |
| `aoi.qtydamage_c`; `assembly_order_damage.qty_damaged` (`raw_or_pack`) | `app_assembly_order_damage.qty_damaged` | component-level damage |
| — | MO `qtyDamaged` | FG units damaged; good output = produced − qtyDamaged (`lib/business-central/production-cost.ts:goodOutputQty:196`) |
| `order_entry_item_assembly.qtyplan(_c)`, `qtymfg(_c)` | — | FM link-table calcs; **no Portal code reads them** |
| `order_entry_item.qtyaomfg_c + qtybcmfg_c` | — | Portal's "qty produced" for an order line (WMS orders route) |

### 4.9 Customer pricing on order lines
`lib/sage/product-pricing.ts`: only `sage.ic_pricp` (`dpricetype='1'`, `unitprice>0`); list order `resolveAssignedPriceList:131` = gold
`Customer.priceListCode` → `sage.ar_customer.priclist` (by erp_code) → "" then `RETAIL` (`priceListLookupOrder:59`); contract prices and order
history excluded; no effective date. Only zero-priced lines are filled (`createSalesOrder` `service.ts:300`). FM-ensure path skips the gold
price-list code. BC producer `lib/sage/ensure-line-prices.ts:fillZeroSalesOrderLinePrices:45` (also writes missing `Product.erp_code`).

### 4.10 BC posting / sync semantics
- Tables: `app_bc_entity_map` (unique `(entity_type, source_system, source_id)`; source_system portal|fm|sage; status LINKED|ARCHIVED|BLOCKED;
  `bc_system_id`, `bc_number`), `app_bc_outbox` (PENDING|CLAIMED|SYNCED|FAILED|DEAD; idempotency key `${entityType}:${entityId}:${version}`
  `outbox.ts:34`, content-hash versioning `decideOutbox:50`; key not sent to BC), `app_bc_inbox`, `app_bc_checkpoint`, `app_bc_dead_letter`, `app_bc_sync_run`.
- Producer `outbox-producer.ts:produceOutbox:152` (masters by default; documents opt-in: sales-orders, shipments, pos, receipts,
  purchase-invoices; `produceSalesInvoices` throws since TASK-2271, BC alone raises and posts sales invoices); drain `outbox-drain.ts` (sales shipments: `PostSalesShipOnly` only, `ship-invoice` removed by TASK-2271; `FOR UPDATE SKIP LOCKED`; 429/5xx retried forever, others to DEAD after `max_attempts` 8; since TASK-2348, `outbox-drain.ts@8e3a951:1358-1450`, a vendor/purchaseOrder/purchaseReceipt/purchaseInvoice row for a test/UAT supplier or PO goes to DEAD with `last_error` `test record` and no BC call, counted as `skippedTest`, not `deadLettered`);
  orchestrator `sync-runner.ts:runBcSync:110` (FX → produce → masters → gate → documents → fulfillment ship-only/receive-only → inventory journals
  PLXISS/PLXOUT/PLXADJ). Inbound `inbound.ts`, `inbound-sync-runner.ts`, gold apply gated by `BC_INBOUND_APPLY_GOLD`.
- Named produce filter (`f916e73` TASK-1503, re-read at `2cb3760`): `produceOutbox` takes `produceFilter` RAW/PACK/COMPONENT (→ product types
  RAW_MATERIAL/PACKAGING/COMPONENT) for the item create path only; a real run is refused until the operator confirms L7a (Average default costing
  on the tenant, `BC_PRODUCE_FILTER_L7A_CONFIRMED=1`), dry-runs always pass, and the filter never touches the Dist book
  (`outbox-producer.ts@2cb3760:187-194,371`, `produce-filter.ts`). Off until L7a, so BC items stay FINAL_PRODUCT by default (ERP-D-012); sales-order
  and PO selection are byte-identical (`outbox-producer.ts@2cb3760:441-477` newest orders, `:576` draft/test PO guard).
- BC writes read at `4243499` (2026-10-14). The Portal raises only **unposted drafts**, BC staff post: the 3PL
  FINAL billing run → BC draft sales invoice (TASK-2318, `three-pl-invoice.ts:184-231`: External Document No. = run number, no order
  number, `salesInvoice` map row; `assertSalesInvoiceDraftAllowed` refuses every other sales-invoice payload in the drain,
  `outbox-drain.ts:577-593`) and a customer return → BC draft sales credit memo (TASK-803/TASK-2710, `credit-memo.ts:148-206`,
  `sales-return.ts`, `POST /api/mrp/sales-returns`; off unless `BC_CREDIT_MEMO_DRAFT_ENABLED`; post/release paths refused).
  Payment initiation is **BC-only** (TASK-1616, `payment-boundary.ts`): every write to `/api/mrp/ap-payments` and `/api/mrp/ap-settle`
  answers 403 `payment_initiation_disabled_bc_only`. Misc AP bills and vendor credit memos (TASK-805, `ap-misc.ts`, G/L Account lines only)
  are created **and posted** by the Portal. Customers carry payment terms and tax area to BC through AL `SetCustomerFinancials`
  (TASK-1502/2728, `serializers.ts:resolveCustomerFinancialsForAl:485`; an unmapped value is reported, never defaulted). `CloseSalesOrder`
  (TASK-2660, `PlxSalesBridge.Codeunit.al:231-305`) short-closes BC lines to shipped and stamps "PLX Closed"; the Portal close-out
  (TASK-2396, `lib/sales-order/closeout.ts`) keeps `shortClosedQty` in `custom_fields.closeout` and freezes BC line sync. Production cost
  (TASK-2653, `production-cost.ts:370-382`) reads Portal AO/batch records and `app_product` (fm-* crosswalk) only. No Workbench action
  reaches any of these routes (`operating/action_catalog.py` `BC_FINANCIAL_ROUTES`).
- Sales shipment lot lines (TASK-3248 `2ec4f32`, re-read at `c09de5d`): a shipment line sends the base lot
  (`lotNumberBase`: the lot text before `@BIN`, trimmed, `wms/lot-numbers.ts@c09de5d:24-27`) and that lot's expiry; a line whose picks
  hold two or more base lots sends `lots[]` (lot, quantity, expiry; bins of one lot merge, earliest expiry wins;
  `serializers.ts@c09de5d:1433-1482`, `outbox-producer.ts@0323e19c:766-819,997-1016`, `outbox-drain.ts@c09de5d:660-676`). AL
  `PlxSalesBridge` 0.9.18.0 (`PlxSalesBridge.Codeunit.al@c09de5d:395-494`) sets "Lot No." per lot and does not post the expiry. Mirror:
  `operating/reconciliation_rules.py` `shipment_line_bc_lots`, which refuses an unparseable expiry where upstream `toIsoDate` falls back to
  today. Read-only: the Workbench never sends a shipment.
- Cadence: `portal/vercel.json` bc-sync */5, bc-inbound */15 (production only); staging/UAT via EventBridge Lambda (`scripts/aws/bc-cron-ping/`).
- Email relay cron: `portal/vercel.json` gained `/api/cron/email/relay` every 5 minutes (`portal/vercel.json@357b6a3`). It sends mail: never call it.
- Decisions to respect: ERP-D-008 full O2C chain; ERP-D-009 go-forward receipts only; ERP-D-012 two books (`partitionLinesByBcBook`, shipment
  slices `{id}::manufacturing|::distribution`; distribution slices FAILED until the company is hydrated); ERP-D-014 canonical IDs; ERP-D-015
  freight/duty **expensed** to Sage G/L (RM freight 41102, pack freight 41130, RM duty 41101, pack duty 41131, dist duty 41680;
  `lib/procurement/landed-gl.ts:buildLandedExpenseLines:57`; receipt actuals beat PO estimates `resolvePostedLandedAmounts:88`);
  ERP-D-016 Incoterms drive recognition (`lib/procurement/incoterms.ts:inventoryRecognitionFromIncoterm:44`).
- **Proposed, not requirements** (decision log at `48d76e8`, `818210e86` #1878 TASK-2392; the ERP-D-015 row at L25 is byte-identical, so
  `operating/landed_cost.py:80` keeps its `@5724575#L25` citation): ERP-D-017 (S0.2) a related release stays required by default, a
  single-destination order defines its release while it is processed, and a per-order "needs related release" opt-out is wanted on
  day 1 (amends ERP-D-014; built at `1e40b64` TASK-2400 as `SalesOrder.releaseRequired`, §3.2). ERP-D-018 customer deposits on production
  orders: BC prepayment invoice or proforma + payment on account per customer, deposit % per order, drawdown sequential by default
  or proportional per order; Finance to countersign, and it conflicts with the communications-hub rule (prepayment invoice only).
  The Workbench must not treat either as a Portal rule or as evidence of how a deposit was applied.
- **Approved 2026-10-10 (Vince; `dbda562` proposed, `3f53702` Approved; decision log re-read at `2cb3760`):** ERP-D-019 each artwork version of a
  printed component is its own packaging item (new `P-xxxx`; the FG keeps its code and takes revision N+1; superseded stock on hold; open POs,
  quarantined receipts and open AOs that name it are flagged); ERP-D-020 only QA accepts an artwork version, the customer signs off the exact
  version in the portal and PLX's internal sign-off is recorded on it before FG activation and the first AO, and a printer PO before
  customer approval warns, never blocks. Both are decisions with build tasks open (TASK-2626..2629): today old and new artwork still share one
  part number and no label lot links to an artwork version, so the Workbench must not infer an artwork version from a `P-xxxx` code, and a
  printer PO without approved artwork is allowed, not a violation. ERP-D-017/018 stay Proposed. The ERP-D-015 row (L25) is byte-identical.
- **Decision log re-read at `4243499` (2026-10-14).** Approved: ERP-D-017 (2026-10-11, as written: release
  required by default, `app_sales_order.release_required` per-order flag, built by TASK-2400); ERP-D-021 (2026-10-11: L9 run-size variance
  compares each run's absorbed conversion, rate × actual hours, with the planned conversion per unit at that run's own quantity, from
  the accepted quote for the SKU and tier, else the Draft FG matrix tier picked from the run's quantity, runs under 5k included; no BC
  batch-size field; built by TASK-1509, `pickRunVolumeTierForQtyExplicit`); ERP-D-029 (2026-10-12: batch release with label
  verification #013B or line inspection #010 incomplete warns and records who released, never blocks). ERP-D-020 amended 2026-10-12:
  QA artwork acceptance is a soft gate (warning, waiver recorded). Proposed, not requirements: ERP-D-022 (overhead as one rate R per
  line-hour for quotes and books, R from budgeted overhead ÷ normal line hours, top-up of a partial shift ≥ 4 h; quotes still use
  RateSettings $/hour, `overheadBasis.source = "rate_settings"`), ERP-D-023..028 (customer certification and QA requirement rows,
  copy at project birth, evidence map and expiry, soft evidence gate). The ERP-D-015 row (L25) is byte-identical.
- **Decision log re-read at `71f7214` (2026-10-14; `1444331` TASK-2847, ERP-D-022 ninth round).** ERP-D-022 stays
  Proposed. Vince accepted: "expected utilization %" in normal line hours is uptime alone (share of paid line hours booked to AOs, no efficiency
  factor); line crew are logged on AOs at the employee rate or the agency flex rate; AO labour rates live in the existing `/mrp/employee-rates`
  table (`app_employee_rate`) and are compared per period with BC 41150 before BC supplies them; the production part of the warehouse share is
  measured manually through Consol books and BC dimensions; the lab-services share of QC wages is set by lab services' costs against total QC
  costs. Open: which BC dimension, which accounts, whether AO labour entries store the rate row. No code yet: quotes still use RateSettings
  $/hour, so `operating/landed_cost.py` is unchanged; the ERP-D-015 row (L25) is byte-identical.
- **Decision log re-read at `40237ed` (2026-10-14; `1f2d42d` TASK-2789 S1, the only change since `71f7214`).**
  Adds ERP-D-031, Proposed: one customer-facing product name and the customer's item code per FG (two nullable `app_fg_item` columns), carried
  to the AO, AO print, case label and BC (Item Description, one Customer Item Reference); frozen on open AOs at AO creation; FileMaker-origin
  FGs keep their names. Not a requirement yet. The ERP-D-015 row (L25) is byte-identical.
- **Decision log re-read at `d4d1bb2` (2026-10-15; `cc2a973` TASK-2812, the only change since `40237ed`).**
  Adds ERP-D-030, Approved 2026-10-14: the FileMaker-shaped bronze `public.*` tables (`assembly_order`, `batch`, `batch_item`, `production`,
  `*_inventory_item`, `order_entry*`) stay portal-owned stores after the FileMaker unplug (CO-010); shop-floor writes do not move to gold
  `app_*` before cutover. The Workbench's bronze reads stay valid sources after the unplug. No landed-cost or stage-map rule changes; the
  ERP-D-015 row (L25) is byte-identical.

### 4.11 Costing re-read at `d4d1bb2`: FG freeze on routing saves, overhead-rate store and flag (2026-10-15)
- **Port re-pinned.** `operating/landed_cost.py` `PORTAL_REVISION` moved from `45673ee` to `d4d1bb2`; every cited range was re-read and
  re-numbered, and both harnesses (`run.mjs`, `labour.mjs`) re-ran at `d4d1bb2`: every golden price is unchanged. The one change is
  `ready-to-quote.ts:461` (`35cfb18` TASK-2811): with no routing hours the labour census row now reads "No routing hours, so assembly and
  setup labour are priced at $0 and reported" (warning), which `derive_ready_to_quote` now matches. The estimate prices assembly labour,
  production hours and overhead at 0 in that case (never a 1-hour default). The snapshot write guard is unchanged in behaviour, now
  `project-landed-quote.ts@d4d1bb2:2386` (`opts.persist !== false`; option at `:1636`).
- **FG freeze on routing saves (`0a62186` TASK-2864, RV2a).** Every primary routing save also writes a one-band `[0, ∞)` active route plan
  (`app_fg_route_plan`, `app_fg_route_plan_band`, `f73bb3a` TASK-2852; origin `legacy-save`, demoting the old plan; pinned at FG
  activation). A frozen (ACTIVE or RETIRED) FG refuses that write with 409 `FG_FROZEN`, which rolls the routing save back
  (`fg-routing-store.ts@d4d1bb2:402-415`). This adds to the recalc refusal of §4.1 (`--apply` skips a frozen FG). Save path only: the
  engine still prices from the primary routing, never from a route plan (RV5 not shipped), so no mirror. `eddbfe3` (TASK-2914) keeps blank
  routing minutes and client throughput on save; `0f9fbee` (TASK-2948) makes the routing fill reuse the setup-standard rows the load
  already read (`quote-inputs.ts@d4d1bb2:90-104`; a failed rows read resolves to the workbook seed): same values, one read.
- **Overhead per line-hour store (ERP-D-022, still Proposed).** `b8f6e22` (TASK-2853) adds `LineOverheadRate` (`app_line_overhead_rate`:
  `line_number` (0 = plant default), `rate_per_line_hour` R, `effective_from`, immutable sizing `inputs`, `override_note` when R differs from
  the suggested R by more than 5 %, bare actor stamps); `371b4cc` (TASK-2865) adds `overhead-rate-store.ts` and `overhead-rate-sizing.ts`.
  The store says it is not read by the quote engine yet (CT-G): overhead stays `RateSettings` per operator hour
  (`quote-inputs.ts@d4d1bb2:151`, `source: "rate_settings"`), so `operating/landed_cost.py` keeps `overheadPerHour` × hours. When the engine
  reads R, the overhead line of the port must change; `upstream_drift` on `quote-inputs.ts` flags it. The table is not served to the
  Workbench (generated schema `not_served`).
- **New permission flag `User.canManageOverheadRates`** (`can_manage_overhead_rates`, default false; `lib/auth/overhead-rates-access.ts`).
  Only holders may edit R; ADMIN, L5 and `canManageEmployeeRates` do not unlock it; costing staff may read the rates. It gates a Portal
  write only, so nothing in the Workbench (read-only) maps it: `operating/employee_permissions.py` mirrors no rate flag, and no Workbench
  capability depends on it. `371b4cc` also redacts rate values in the rate-settings audit entries (`rate-masters.ts@d4d1bb2` after line
  172); `app_audit_log` is not served, so no Workbench read changes. `RATE_MASTER_DEFAULTS` (`rate-masters.ts:34-57`) is byte-identical.
- **Cost fingerprint (`b19b6d2` TASK-2861, `d4d1bb2` TASK-2862).** `TheoreticalFG` gains report-only fingerprint columns; the engine
  computes a fingerprint the snapshot stores, and `landedQuoteOf` strips it from the quote result, so no golden field changes.
- **`readErrors` (`4dc5cdf` TASK-2809, CT-A; re-read at `d53137e`): no-op.** `quote-inputs.ts@d4d1bb2:8-18` names each priced-input
  read that fails and falls back (primary routing, Standard costs tier rows, FG target quantity, compounding standard, setup standards)
  in `QuoteInputs.readErrors`; the fallback values are the old ones, so no number moves. The port has no separate field: a relation the
  caller could not read is passed as `None`, priced with the same fallback, and named as a `RECORDS_NOT_READ` unknown
  (`QUOTE_RELATIONS` in `operating/landed_cost.py`), which keeps the Workbench quote `unknown`.
  `test_an_unread_relation_is_an_unknown_never_an_empty_table` covers the routing, draft-store, FG-item and setup-standard reads; the
  compounding standard is not ported (vessel plans are an input).

## 5. Permissions / auth model and how it constrains Workbench reads

- Guards: `lib/auth/api-auth.ts` (`requireAuth:73`, `requireRole:99`, `requireMrpModule:138`, `requirePermission:203`, `requireCapability:235`,
  `requireL5EmployeeRatesAccess:310`). `getApiSession` does not exist in code (parent CLAUDE.md and `AGENTS.md` mention it).
- Roles `lib/auth/roles.ts` (CUSTOMER=0, SUPPLIER=0, STAFF=1, MANAGER=2, ADMIN=3, SUPER_ADMIN=4); RBAC mode `rbac-mode.ts` legacy|shadow|enforce.
- `lib/auth/effective-access.ts:resolveEffectiveAccess:340`: accessLevel L1–L5 + departments + scopes {departmentIds, companyKeys, dataKeys} +
  explicit denies + temporary elevations. Data keys (`lib/auth/data-key-registry.ts:11`): `operations.{warehouse,manufacturing,rnd,procurement,
  finance,sales,project-development,quality}`, `crm.customers`, `documents.controlled`, `customer.form-requests`, `identity.*`, `platform.*`.
- `AccessPolicyGrant` is DENY-only (`access-policy-grants.ts:27`). Employee rates need L5.
- Customer tenants: one portal user per `Customer` (`Customer.userId` unique); routes filter by `customerId` one by one (no central filter).
- Service credentials: Workbench API `plxwb_` keys (SHA-256 hash, 90-day expiry) → one shared DB login `wb_api` with column grants only:
  **no row-level or employee-level trimming upstream**. External `plx_` API keys now need scope `external` (`lib/auth/api-keys.ts:authorizeExternalRequest:127`, TASK-2114).
  Bridge keys `PORTAL_BRIDGE_KEY_<LABEL>` (`app/api/external/swarm/v1/_lib/auth.ts`) read UAT, not the replica.
- **Constraint for the Workbench**: it must apply its own audience before fill/read/rank (as `integrations/source_mirror.py` does) and should
  model it on Portal vocabulary: map relation families to Portal data keys (orders→`operations.sales`, purchasing→`operations.procurement`,
  manufacturing→`operations.manufacturing`, products/formula costs→`operations.rnd`/`operations.finance`, customers→`crm.customers`),
  treat cost/margin/wage columns as finance/L5, never let a customer-tenant principal see another customer's rows, and treat DENY as final.
  Manifest D3 (`services/workbench-api/manifest/allowlist.json` "decisions") excluded person names/emails/phones/addresses/tax ids/wages/formulas
  by name; on 2026-09-27 Vince kept the 305 columns review wanted removed (contact names, costs, free text) — they ARE served, so the Workbench must trim them per actor.

## 6. Reporting API contract (Portal side)

- Code `services/workbench-api/` (Node ESM, Lambda Function URL, stack `plx-staging-workbench-api`); contract `docs/modules/workbench-api/README.md`;
  runbook `docs/runbooks/WORKBENCH-API.md` (live since 2026-09-28, TASK-2014/2048); DB login runbook `docs/runbooks/WORKBENCH-READONLY-DB-ACCESS.md`;
  PRD `docs/projects/workbench-readonly-access/PRD.md`.
- Routes (`src/http/app.mjs:110-133`): `GET /v1/health`, `GET /v1/relations`, `GET /v1/relations/{schema}.{relation}/rows`; anything else 404,
  non-GET 405. Auth `Authorization: Bearer <plxwb_…>` or `X-API-Key` (`src/auth/keys.mjs`); missing/unknown/expired → 401. Optional `X-Request-Id`.
- Rows params (`src/core/query.mjs`): `limit` 1..5000 (default 1000), `cursor` (opaque base64url `{r,k,v}`; bound to relation + key; key change
  → 400 restart), `since` (ISO 8601 with Z/offset only), `columns` (comma list of approved columns; key columns always returned).
  Response `{relation, columns, rows, nextCursor}`; ≤5 MB page; a page never ends inside a run of equal keys; `null-key`/`key-run-too-long`/`row-too-large` 500s say "retry with larger limit or fewer columns".
- Key choice (`src/core/catalog.mjs:chooseKey`): primary key → narrowest NOT NULL unique → single nullable unique (NULLs first) → `id` (views; not enforced unique).
- `since` column = first of `updated_at`, `audittimestampmodified`, `audtdate`; modes `timestamptz`, `timestamp-utc` (Prisma naive UTC),
  `yyyymmdd-text` (Sage, day-granular). 89 relations have since; full-reload only: `public.batch_calibration`, `public.order_entry_item_assembly`,
  `public.order_entry_item_batch`, `silver.shipments`. `since` is `>=` (boundary rows repeat).
- Every query: `BEGIN READ ONLY; statement_timeout 30s; TimeZone UTC` (`src/core/db.mjs:15`). `wb_api` max 5 connections (6th concurrent → 503, retry).
- Database errors (`docs/runbooks/WORKBENCH-API.md@12db891` "Database errors", `367a0db` #1856 TASK-2358): a refused or failed connection answers
  **503** with a named `error.code` on every route, `/v1/health` included (a bad key stays 401): `database-login-failed`, `-permission-denied`,
  `-not-found`, `-host-not-found`, `-unreachable`, `-tls`, `-unavailable`, `-busy`, plus `statement-timeout`/`too-many-connections`. A 500 `internal`
  is not a connection failure. `WB:integrations/source_mirror.py` (`_FINAL_DATABASE_CODES`) fails a login, database, host or TLS 503 at once as
  `source_unavailable`; the others keep the throttle retries.
- Secrets (`docs/runbooks/WORKBENCH-API.md@38cd3f3`, `b5e5b54` TASK-2362, `394c4f3` TASK-2692): the function reads its DB URL and key hashes from
  Secrets Manager at runtime (TTL `SecretsTtlSeconds`, default 5 min; a login failure forces one re-read at most every 30 s; a cached copy is served up
  to 1 h while Secrets Manager fails; reads time out after 5 s). A cold instance that cannot read a secret, or a cache older than 1 h, answers
  **503 `secrets-unavailable`** on every route; it clears by itself, so `integrations/source_mirror.py` correctly keeps it out of
  `_FINAL_DATABASE_CODES` (throttle retries). A key revoke now takes effect within the TTL with no redeploy (up to 1 h while Secrets Manager fails).
- Health: `ok` only when replica (`recovery:true`), lag <60 s, no unavailable relation, login not expiring within 14 days; lists `keysExpiringSoon`.
  `wb_api` expiry per SOURCE-NOTES 2026-12-27 (renewal = Vince). Kill switch = reserved concurrency 0.
- Catalog (`manifest/allowlist.json@52c9116`, re-read 2026-10-20, TASK-3525 `d92530b52`; before it
  TASK-3355 `c2ff912`..`757625d`, TASK-3390 `0f399454`, `22361e0b`): `status: approved`, 104 relations /
  4,354 columns, `pendingDiscovery` empty; schemas public 57 (31 `app_*` + 26 FM bronze), silver 25, sage 22; domains purchasing 34,
  manufacturing 22, products 22, orders 20, customers 6. **TASK-3525 (D10, Vince 2026-10-20 4:22 PM ET):** `public.app_bc_entity_map`, the
  Portal's identity bridge from a portal/FM/Sage id to a BC `systemId` and No., is lifted from the `app_bc_` prefix deny by exact name (every
  other `app_bc_*` and `bc_*` relation stays denied), 13 pinned columns (no Prisma model), none flagged; `app_supplier_product_tier` and
  `app_approval_record` keep their D8 lists. It is listed, not yet granted: Infra runs the scoped `provision.mjs --regrant --only` for the
  three tables on staging after the merge (`WORKBENCH-READONLY-DB-ACCESS.md@52c9116`, "`public.app_bc_entity_map` (TASK-3525)"). The runbook,
  re-read 2026-10-21 at `56edb42` (`cb641a9` TASK-3538, merged `e0a5e16` #2729), adds an explicit UAT target and nothing else that reaches the
  Workbench: every admin mode, dry-run included, also accepts the UAT primary `plx-postgres-uat…` only with that exact hostname in
  `--confirm-host` (`WORKBENCH-READONLY-DB-ACCESS.md:190,259-270@56edb42`); it creates no grant, login, relation or UAT login profile,
  `--verify` stays limited to the staging reporting replica (`:298`) and the automated Sage sync still skips the UAT regrant (`:382`).
  The manifest and allowlist are unchanged, and the Workbench API the Workbench reads stays on the staging replica, so no Workbench
  binding, grant or check changes. The
  Workbench binds it as the raw type `RelPublicAppBcEntityMap`; a row is an identity lead the Portal wrote, and the curated
  `BcEntityMapping` with its `LINKED`-only joins stays in `ontology/schema/pending/bc_entity_map.json` (`python3 -m ontology.schema
  --drift` lists it `served: true`) until it is promoted into `domains/bc_finance.json`, replacing that file's not-served placeholder.
  `scripts/sql/workbench-readonly/allowlist.json` is byte-identical. The Workbench binds the same 104 relations
  (`ontology/source_registry.json`; `tests/test_source_registry.py` holds the bound set equal to the pinned manifest's), the ten added
  since `f3e3a53` included: `public.app_approval_record`, `public.app_supplier_product_tier`, `sage.ap_open_document`, `sage.ap_term`,
  `sage.ap_vendor`, `sage.gl_account`, `sage.gl_batch`, `sage.gl_je_detail`, `sage.gl_je_header` (TASK-3355, at `c09de5d`) and
  `sage.ap_vendor_remit` (TASK-3390: CoS dropped it on 2026-10-19 and Vince lifted it on its own at 5:15 PM ET, D9, outside
  `TASK_3355_RELATIONS`; 16 columns with `_sage_pk`, its D3 columns `textstre1`, `codepstl`, `textphon1`, `audtuser` out).
  `sage.ap_vendor` excludes `taxnbr`, `brn` and `amtcrlimt`, and an excluded column stays unbound. Column moves in
  `app_fg_item`, `app_purchase_order_line`, `app_purchase_requisition`, `app_quote_line`, `app_receipt`, `app_receipt_line`,
  `app_sales_order` and `app_shipment_line` reach the bindings through the regenerated schema. A listed relation is not a granted one:
  `sage.ap_vendor_remit` is granted only by `provision.mjs --regrant --only sage.ap_vendor_remit` after its migration and rediscovery
  (`WORKBENCH-READONLY-DB-ACCESS.md@0323e19c`, "Unblocking `sage.ap_vendor_remit`"), and the live `schema.portal_queryable` check counts
  a served relation the API reports unavailable under `unavailable` and fails on it. The text below is the `f3e3a53`
  reading.
  At `f3e3a53`: `status: approved` (D8, Vince 2026-10-16 4:13 PM ET, TASK-3145/3146; `:3,17`), 93 relations /
  3,752 columns, the same 93 relations the Workbench bound then. Approval grants nothing (`WORKBENCH-READONLY-DB-ACCESS.md@f3e3a53:6`): grants stay
  an operator step, and D6 (TASK-3119) removes six wage columns only when an operator applies it (`:274`); until then `wb_tanush`/`wb_ricardo`
  hold the 3,396 columns approved on 2026-09-25 (`:6`). The ten `pendingDiscovery` relations are in no entry and the Workbench binds none of
  them: `public.app_approval_record`, `public.app_supplier_product_tier`, `sage.ap_open_document`, `sage.ap_term`, `sage.ap_vendor`,
  `sage.ap_vendor_remit`, `sage.gl_account`, `sage.gl_batch`, `sage.gl_je_detail`, `sage.gl_je_header`; `--apply`, `--regrant` and `--rollback`
  refuse until discovery covers them (`:6`). **Re-read at `b4118a3` (TASK-3355):** CoS dropped
  `sage.ap_vendor_remit` on 2026-10-19 (no key to page on), so it is hard-denied again and the
  set is nine (`WORKBENCH-READONLY-DB-ACCESS.md@b4118a3:93,118,137`; `:6` still says ten).
  A full-manifest `--apply`/`--regrant` is not approved (Vince, re-confirmed 2026-10-19); the nine
  go live only through `--regrant --only` over 18 named relations: the nine whole, plus 17 new
  columns on nine recaptured relations (`:110,114-119`). `app_approval_record` excludes signer
  name/email, `file_url` and `idempotency_key` (`:98`). Nothing is granted yet and `wb_api`
  still "does not exist on any database yet" (`:6`); the allowlist pin is outside this re-read.
  **Conflict at `f3e3a53`:** `WORKBENCH-API.md:6-7` says `wb_api` exists on the staging primary
  (G1, live since 2026-09-28), but `WORKBENCH-READONLY-DB-ACCESS.md:6` says `wb_api` "does not exist on any database yet". The live
  `schema.portal_queryable` check answers through the API, so the API login works; which login and column set it holds is not recorded
  here. Earlier pins (before `f3e3a53`): `status: pending-vince`, 3,399 bundled columns (docs said 3,396): public 54 (28 `app_*` gold + 26 FM bronze), silver 25, sage 14; domains purchasing 26, manufacturing 22, orders 20,
  products 19, customers 6. A column change upstream needs `--regrant` + bundle refresh + redeploy (operator).
- **Not served** (ask upstream, do not scrape): formulas/formula items, BOMs, inventory lots/transactions/warehouses, pick lists, ship-release lines,
  shipment allocations, quote volumes, projects, theoretical FG, documents, quality/eQMS, users/RBAC, agents, all `bc_*`/`app_bc_*`, `sage.ic_pricp` (prices), FM `inventory_raw`. (Served and useful: `sage.ar_customer.priclist`/`idcust`,
  `sage.oe_order_header.ordnumber/orduniq/ponumber/pricelist`.)
- **Traps**: (1) replica of the staging primary, not UAT (§2); (2) no exact-record filter — the Workbench pages and filters locally; (3) pages are
  non-atomic, deletes are invisible to `since` (lines are hard-deleted and recreated by SO draft edit and quote PATCH; ETL rewrites lots);
  (4) `app_*.updated_at` is also bumped by ETL and bridges, not only by humans; (5) FM `audittimestampmodified` is the FM edit time, not the
  replica time; (6) Sage `audtdate` is day-granular text; (7) money/quantities are float-formatted strings → Decimal; (8) `app_customer.id`
  mixes cuid and `fm-cust-N`; (9) `po_reference` mixes real POs (Sage/FM-ensure/UI) and FM `reference` text (legacy migration);
  (10) Portal FM bridge rows: `public.order_entry.id ≥ 900000` were created by the Portal and their `custpocode` may equal the Portal SO number.

## 7. Recently changed areas (2026-09-16 → HEAD, 268 commits; ontology-relevant only)

| Commit | Date | Subject → paths |
|---|---|---|
| `6dfa61d`, `588ec78` | 09-30/10-01 | Selling prices per item/price list (`app_product_selling_price`, MRP-M-265); WAFS barcodes ETL onto gold UOM (MRP-M-170) → `lib/mrp/product-selling-price.ts`, `scripts/etl-wafs-uom-barcode.ts`, `lib/wms/wafs-uom-feed.ts` |
| `f015246`, `ff7c16f`, `b46934a` | 10-01 | New SO/DSO form: gold commercial pickers, Sage O/E tabs, customer commercial fields inherited, Sales Split default 100% → `lib/sales-order/{commercial,commercial-lookups,load-commercial-lookups,sales-split}.ts`, `app/mrp/sales-orders/new/*` (no schema change; context for `app_sales_order` commercial columns) |
| `6c9415973` | 09-30 | Freightcom quote from SO ship-to (MRP-M-033) → `lib/wms/shipment-quote.ts` |
| `24c21006c` | 09-30 | Freightcom live quotes on WMS shipments → `lib/wms/carrier/freightcom/*`, `app/api/mrp/wms/shipments/[id]/quote/route.ts` |
| `f8a8cedea`, `dbc060b3c` | 09-30 | New Quote picks PD project; stdCost from TheoreticalFG → `lib/project-development/quote-picker.ts`, `quote.ts` |
| `6f76f91e6`, `959a56a93`, `bc1272941`, `6d3ae82e4` | 09-29/30 | Product UOM override, Pack/Dozen → `lib/mrp/product-uom.ts`, `business-central/serializers.ts`, `scripts/business-central/ensure-uom-codes.ts` |
| `8b0f79938` | 09-30 | Create MO from SO Produce line (ERP-D-014) → `lib/mrp/api-handlers/manufacturing-orders.ts` |
| `043020a6a`, `dec789c72`, `44a630714` | 09-22/30 | eQMS kernel + Quality Hub → `lib/quality/eqms/*`, migration `20260922120000_mrp_m_275_eqms_poc` |
| `7a50997fe`, `2df404548`, `19079adea`, `d7a23d7e0`, `893de7196`, `070aabaa4` | 09-29/30 | Formula % = formula page 0–100; silver `formula_items.percentage` corrected; batch/pick qty from page % → `lib/formulas/{line-percent,gold-formula-lines,cost-rollup}.ts`, `lib/inventory/batch-formula-sync.ts`, `lib/wms/source-needs.ts`, `scripts/migrations/20260929230000_…sql` |
| `e2000fae7`, `9c740e608`, `c7e8ad6d0`, `04ac2eff8`, `ed6cd925f` | 09-25/26 | Pricing follows the formula pin only; formula↔Draft FG links; ETL stops guessing project_key → `lib/costing/{project-landed-quote,formulation-resolution}.ts`, `lib/project-development/formula-{pin,links}.ts` |
| `93fcd0b34` | 09-22 | Shared conversion cost (batch/AO/backflush) → `lib/costing/conversion-cost.ts`, `business-central/production-cost.ts` |
| `3938f9b9f`, `4b3706d7d`, `3fb115934`, `08c9092a8`, `38b22226b`, `d9c4980ce`, `d1b37b977` | 09-28/29 | Sage commercial header, optional fields, sales split, FX snapshot, order discount, Avalara tax profile, ship-to fallback on DRAFT SOs → `lib/sales-order/*` |
| `4c4bd7a27` | 09-28 | Customer price list + assigned-then-RETAIL pricing → migration `20260925180000_mrp_m_265_customer_price_list`, `lib/sage/*`, price-list routes |
| `e3346a90a` | 09-29 | Sage order completion by remaining qty, not COMPLETE flag → `app/api/mrp/projected-revenue/queries.ts` |
| `c497a6dc3`, `c2a5c10d7`, `d3f01fbf1`, `ea2fa80ad`, `9d53fe280`, `17225494b` | 09-22/25 | BC: carrier/tracking on ship payload, MO code + release-required on BC SOs, Sage/FM refs on BC cards, session/company probes, EventBridge pacer → `lib/business-central/*`, `bc-extensions/plx-receipt-bridge/*` |
| `eceb50c37`, `f7e11e83f`, `bccc79c5c`, `20eb99efc`, `0e6e75717` | 09-29/30 | Agent fleet: runner API, run tables, eval tables, registry contract, COS tools → `lib/agents/runner/*`, `app/api/runner/v1/*`, migrations `20260929180000/190000` |
| `e13ad2275` | 09-30 | API key scopes; `/api/external` requires `external` → `lib/auth/api-keys.ts`, migration `20260929200000_api_key_scopes` |
| `a47b9098d` … `2b656d8ad`, `f5f923cff`, `121287f8e` | 09-25/29 | Workbench read API + provisioner + health email + replica-only network → `services/workbench-api/**`, `scripts/sql/workbench-readonly/**` |
| `426df6af1`, `7ee5697b0`, `ced66ea9c` | 09-29/30 | Documents/artwork open through the Portal, not SharePoint links |
| `4b6eff0aa`, `d3736f8c5` | 09-25/28 | "Reading never writes" for UAT-feedback GETs (pattern only; business write-on-GETs remain, §8.1) |
| shim retirements `02875263c`, `23ab24394`, `e60800b26`, `aca5d0833`, `7f349792e`, `50ed47a84` | 09-30 | import paths moved to domain folders (db, utils, inventory, fm-migration, notifications, sharepoint) |

### 7.1 Re-read at `5cb95e1` (2026-10-10; repo pin moved from `549f6f0`)

| Commit | Change | Workbench |
|---|---|---|
| `260d6d0`, `b80c836`, `99fc916`, `77f1ee6` (TASK-2471/2472/2473/2465, G2/G3/G9) | `app_customer_supplied_item` registry (one row per customer and item, stop = `is_active` false); one supply-source read (`lib/customer-materials/supply-source*.ts`): an open CUSTOMER-path sourcing request is CUSTOMER_PENDING, an active registry row CUSTOMER, else PLX; quotes price customer-supplied BOM lines and formula raws at $0 (no fee), list the quantity to send (`customerSupplied`) and warn on pending lines; a failed supply read throws instead of pricing as PLX | adopted: `operating/landed_cost.py` (`resolve_supply_sources`, `quote_facts`, census), golden re-run at `5cb95e1` |
| `7a3388d`, `c37d9e6`, `2dd7071`, `10e90b9`, `424427a`, `cd9ec2e`, `7f6858b` (TASK-2300/2564/2301/2469/2303/2304/2583) | Customer Material Request (`app_customer_material_request*`, `CMR-0001`), PRStatus `CONVERTED_TO_CMR`, requisition `source_type` VENDOR/CUSTOMER + `customer_id`, ReceiptType `CUSTOMER_SUPPLIED`, receipt `customer_material_request_id`, receipt line `cmr_line_id`, lot `supply_source`; a CUSTOMER requisition converts to a CMR, never a PO; scheduling counts open CMR lines as incoming; a QC rejection reopens a CMR line | adopted: requisitions domain status enum; generated schema carries the new enums and not-served models; none of the new columns is served by the reporting API |
| `70d599f` (TASK-2573) | `sourcing_path` and sample `paid_by` check constraints allow CUSTOMER (migration only) | no Workbench effect: no curated property binds them |
| `3fb3102` (TASK-2311), `a5f1072` (TASK-2582) | 3PL billing leaves CUSTOMER_SUPPLIED receipts and lots out (`lib/3pl/billable-scope.ts`); the WMS ETL rebuilds lots of a CUSTOMER_SUPPLIED receipt line customer-owned with `supply_source`; QC routes refuse customer-supplied lab ids; cron `customer-material-qc` every 30 min | no Workbench effect: no 3PL billing or lot ownership is computed here |
| `b509dd1` (TASK-2467) | reorder raises no vendor PR for customer-only items | no Workbench effect (`computeTotalAmount` unchanged at :281) |
| `2917255`, `e6dc559` (TASK-2215) | complete document metadata: the generic upload doors write the canonical PLX DocumentCategory (`resolveDocumentCategory`; SIGNED_PO, CUSTOMER_PO, FILL_RECORD reach the column as OTHER), RetentionCategory from the category policy, Confidentiality by party; DocStatus gains Superseded; customer documents get the full payload | adopted: `operating/doc_metadata_taxonomy.py`, `doc_metadata.py` (`written_as`), `kind_confirmation.py`, `playbook_originals.py` citations |
| `2a3bf1a` (TASK-2524) | shipment-aware sales-order edit lock (`lib/sales-order/editability.ts`), the GET answers `editable`/`editLock`/`linesLocked` | citations re-pinned in `action_catalog.json` and `twin/portal.py`; the lock itself is mirrored |
| `b7f8cf2` (TASK-2553) | declared auth emails skip the non-production redirect, subject `[STAGING] ...` | no Workbench effect: never a PO send, not `[STAGING →` |
| `5cb95e1` (TASK-2500), `ea30548` (TASK-2505), `d15a605`, `7f5d19a` | pending-orders report follows realized status; ship-release GET casts FM `finalqty` text; PLX site decision 15 (E3 Option B, deletion alerts TASK-2591) and 19 (BC proved in its live environment) | no Workbench effect |

### 7.2 Re-read at `45673ee` (2026-10-11; repo pin moved from `5cb95e1`)

| Commit | Change | Workbench |
|---|---|---|
| `498777b`, `46f24b7`, `1e392ed` (TASK-2597) | `app_audit_log` and `app_electronic_signature` append-only by trigger; `AuditLog.deployed_commit`; e-signature MFA only after a real step-up; eQMS no `pocSessionAck` | generated schema: not_served types carry the Prisma `///` notes (`scripts/gen_ontology_skeleton.py` records doc comments); §3.14, §3.15 |
| `b7b6c09` (TASK-2290) | `app_contact_email_subscription`, `app_party_email_setting` (schema only, one-party CHECKs) | generated schema: new not_served types; Portal send state, not mail evidence |
| `e677b49` (TASK-2594) | one `document-types.ts` table drives TYPE code, retention and the short DocumentCategory choice | adopted: `operating/doc_metadata_taxonomy.py` (`DOCUMENT_TYPES`, `document_category_column`, `retention_for_category`), `doc_metadata.py` (`written_as`), `playbook_originals.py` (a short choice reads as every category that writes it: BATCH_RECORD consistent with an AO scan) |
| `514e1e5` (TASK-2611) | quote matrix lists what the customer must send; the unit-cost fallback skips a customer-supplied column (G-D4) | adopted: `operating/landed_cost.py` `fallback_matrix_unit_cost`, goldens re-run |
| `4b82890` (TASK-2605), `d72defb` (TASK-2600), `45673ee` (TASK-2606) | Operations receiving put-away and line PATCH answer 409 `CUSTOMER_SUPPLIED_RECEIPT`; the put-away queue skips customer-supplied lines; recording a customer receipt refuses (409 `QC_RESULT_ALREADY_PRESENT`) when FileMaker already holds a QC result for the lab id unless confirmed | no Workbench effect: the Workbench writes no receipt, put-away or QC; customer-supplied stock stays customer-owned at $0 (G-D10) |
| `550f31a` (TASK-2603), `7bdf2b8` (TASK-2607), `00e09ca` (TASK-2608) | Ready to Convert gets a Create request button (customer requisition → CMR through the existing convert route); requisition create accepts only DRAFT or PENDING_APPROVAL; a customer requisition must name a CUSTOMER-path sourcing request (422) | no Workbench effect: `requisitions.json` already models CONVERTED_TO_CMR and no create path |
| `a1a5bbf` (TASK-2613), `65a0e8b` (TASK-2614), `4f0d304` (TASK-2609), `7a1420a` (TASK-2470), `67fe7d5` (TASK-2601) | reorder shows the supply-source error and stops vendor PRs for customer-only rows; edit a supplied item's receive-under supplier (audit constant moved to `supply-source-shared.ts`); sourcing copy; CMR chips; staging pacer runs the customer-material QC job | no Workbench effect (the landed-cost golden re-pins `supply-source-shared.ts`) |
| `e8d0ea0` (TASK-2590), `ff9f68d`, `462a7c3`, `2a3c629` | PLX site decision 20 (portal writes, people read; PLXCUSTOMERS frozen); Phase G plan refresh; verify-portal docs; QMS test evidence | §3.14 site note; no capture scope or permission rule changes |

### 7.3 Re-read at `2cb3760` (2026-10-12; repo pin moved from `45673ee`)

| Commit | Change | Workbench |
|---|---|---|
| `324b9f7` | `GET /api/cron/storage-finalize` every 5 minutes in `portal/vercel.json` | §3.14 note; still a write-on-GET never called (§8.1) |
| `4304012` (TASK-2602), `d792e78` (TASK-2618), `eb4b44c` (TASK-2619) | Assembly BOM approve, formula customer sign-off, raw/pack QC release record a regulated `Approved By` signature after re-authentication; `GET /api/auth/signing-reauth`; harness docs | §3.15, §3.7, §8.2; `operating/process_stage_map.json` reading `qc_release_is_a_regulated_signature`; no action-catalog precondition mirrors these routes |
| `ba9bbf6` (TASK-2604, with TASK-2600) | put-away refuses FileMaker's duplicate `PO_RECEIPT` of a customer-supplied lab id; the queue skips it | §3.5: never count both receipts |
| `602bf8a` (TASK-2312, with TASK-2603) | Generate PO warns on a $0 vendor line for a customer-supplied item (behind the receiving switch); Create request button | §3.5 |
| `89c42c1` (TASK-2612, with TASK-2609), `d69a3e8` (TASK-2616) | customer samples payer only on the CUSTOMER path (400); customer-supplied wording; Request from customer button | §3.5; landed-cost citation `sourcing/status.ts` 74-99 unchanged |
| `f916e73` (TASK-1503) | named RAW/PACK/COMPONENT item produce filter, real runs refused until L7a | §4.10; `reconciliation_rules.py` and stage-map citations re-pinned (logic unchanged, lines moved) |
| `6124f55` (TASK-2623) | board Opportunity back on `app_project_pipeline.opp` (data-only migration `20261011180000`); BOM approve pre-check; version key; activation 409 | no Workbench effect: `landed_cost.py` reads `target_price` as the per-unit retail target, which it still is; `projects/read.ts` 119-147 unchanged |
| `208dd9e` (TASK-2633) | activated FG frozen, 409 `FG_FROZEN` | §3.7; `operating/portal_error_codes.json` carries `FG_FROZEN` (the generator now reads self-named code constants); `costing/fg-routing.ts` unchanged |
| `dbda562`, `3f53702` (TASK-2625) | ERP-D-019, ERP-D-020 Approved | §4.10; ERP-D-015 row unchanged, `landed_cost.py`/stage map citations stand |
| `1272cae` (TASK-2525), `2cb3760` (TASK-2622) | one RBAC staff mapping row; SALES department mapped to an Entra group, mapping sync and membership stamping | no change: the Workbench reads memberships live from Graph (`integrations/graph_directory.py`), never from the Portal mapping |
| `d1146f3`, `32fd922`, `e8e5dab` (TASK-2595/2599) | verify-portal and harness feature-map docs, redeploy | no Workbench effect |

### 7.4 Re-read at `38cd3f3` (2026-10-13; repo pin moved from `2cb3760`)

Orders, WMS and schema half of 112 commits (2026-10-12).

| Commit | Change | Workbench |
|---|---|---|
| `898e084` (TASK-2638), `1e40b64` (TASK-2400) | MAKE confirm writes no FM order_entry; single-destination auto release keyed on portal ids; `releaseRequired` | §3.2; `sales_order_lifecycle.json` and `fulfillment.json` name the portal keys canonical and the FM twin legacy; `operating/tests` pin it |
| `8665d4d` (TASK-2641), `f17cb54` (TASK-2703), `fe53eec` (TASK-2701) | ship releases, shipments and their lines keyed by portal SalesOrder/SalesOrderLine ids; release-referenced lines keep their ids | §3.2, §3.10; `fulfillment.json` (`order_ref` is a compatibility ref) |
| `999dac8` (TASK-2396) | portal close-out: restate at yield, short-close, the one COMPLETE evaluator; rail Shipment and Complete steps | §3.2, §3.14; `sales_order_lifecycle.json` notes; `action_catalog.json` lock unchanged (those statuses were already locked) |
| `359e2f0` (TASK-2651), `5cade1b` (TASK-2652), `e59f1d7` (TASK-2627) | Generate PO and pack receipts on portal records only; Form #012 incoming QC | §3.5; `purchase_to_invoice.json` notes; `process_stage_map.json` readings on stages 11 and 16 |
| `a5b835d` (TASK-2642), `1b02309` (TASK-2650), `eb14fe3` (TASK-2643), `57bd403` (TASK-2711) | portal-issued AO-/BC- codes; `assembly_order.portal_product_id`; portal-born AO FG lots; AO link to portal order lines | §3.8; `manufacturing.json` `AssemblyOrder.for_order_line` |
| `baa0e1a`, `18c00b8`, `119eebd`, `5b0b352`, `e951e4f`, `32d0274` | pick provenance and lineage, short and over picks at confirm, LP-split picks, released portal orders on shipping lists | §3.10; no Workbench mirror (pick lists and lines are not served) |
| `25101f3`, `27f2bf5` (TASK-2310/2706) | customer-owned lots reserved only for the owner's jobs | §3.9; no mirror |
| `5f7b299` (TASK-2220) | `app_approval_record` | §3.17; never an approval authority for a Workbench action |
| `b5e5b54` (TASK-2362), `394c4f3` | reporting API reads secrets at runtime; 503 `secrets-unavailable` | §6; `source_mirror.py` retries it (correct) |
| `b1cf16c` (TASK-2648), `b5212d5` (TASK-2636) | quote offer `planned_conversion_per_unit`/`fp_code`; Pack BOM create atomic (routing and draft-cost writes on the caller's transaction) | §3.12; `landed_cost.py` arithmetic unchanged (`fg-routing-store.ts`, `draft-cost-persist.ts` re-pinned) |

### 7.5 Re-read at `71f7214` (2026-10-14; repo pin moved from `38cd3f3`)

MRP half of `38cd3f3..71f7214`.

| Commit | Change | Workbench |
|---|---|---|
| `b5311a9` (TASK-2775) | pack receipt release: artwork gap warns (AuditLog), never blocks | §3.5; `process_stage_map.json` `qc_release_is_a_regulated_signature` |
| `79e7477`, `2968132` (TASK-1364) | AO label reconciliation, sign-off, line inspection, QC release ack (warn-only, ERP-D-029) | §3.15; tables not served; no action precondition change |
| `0c008ee` (TASK-2721) | split-lot quantities conserved; legacy copied splits counted once | §4.6; no `batch_item` binding |
| `9108064` (TASK-2850) | matrix GET and POST read-only for the cost snapshot | §3.12, §8.1, SOURCE-NOTES |
| `7fa8b7e` (TASK-2849), `35d3dd4` (TASK-2786) | AO list joins and serial queries; `fetchAssemblyLinkedOrdersResult` (dbError instead of half a set) | none: `sizepieces` "planned production qty" now `assembly-queries.ts:472` |
| `2e36901` (TASK-2835) | legacy attachment routing takes the record's names | §3.14; `doc_metadata_taxonomy.py` comment |
| `79e7477`, `c9da077`, `bf27199`, `f3321b5`, `d8fad07` | schema: AO QC tables, artwork version basis, material evidence, policy evidence map, project waivers and requirements, formula spec rows; one CustomerRequirement comment | additive, no enum or mirrored column changed; generated schema regenerated |
| `1444331` (TASK-2847) | ERP-D-022 ninth round (Proposed; rounds six to eight were read at `4243499`) | §4.10 |

### 7.6 Decision log re-read at `40237ed` (2026-10-14; per-file pin only, repo pin stays `71f7214`)

| Commit | Change | Workbench |
|---|---|---|
| `1f2d42d` (TASK-2789 S1) | ERP-D-031 added (Proposed): FG customer-facing name and customer item code | §4.10 |

### 7.7 Re-read at `19f1d94` (2026-10-14; per-file pins only, the repo pin is not moved)

Documents and storage half of `2cb3760..38cd3f3`, read up to the checkout HEAD `19f1d94`.

| Commit | Change | Workbench |
|---|---|---|
| `2167c63` (TASK-2222), `b9919cb` (TASK-2751) | ProductRef and OrderRef from the record links on every stored file and issued record; DocumentCategory and RetentionCategory stay text columns | adopted: `doc_metadata_taxonomy.py` `COLUMNS` (captured, stated, Ref mentions), never a proposal row or a confirmation (`RECORD_LINK_COLUMNS`); an OrderRef is `poReference`, which can hold a name |
| `e8a89d5` (TASK-1379), `2e6984f` (TASK-2788), `162b108` (TASK-2787) | ARTWORK files on LABEL_PROOF's customer → project tree (existing files never move); ARTWORK_SAMPLE on an artwork version, ARTWORK_TEMPLATE per printed component | adopted: `_ROUTES`, `DOCUMENT_TYPES`, `ENTITY_ROUTES` (`ARTWORK_VERSION`, `ARTWORK_COMPONENT`); `party_type_for_category("ARTWORK")` is Customer |
| `5d3a5e8` (TASK-2684/2685), `47e489d` (TASK-2745) | requirement evidence categories on raw and packaging materials; INGREDIENT_LIST on a formula | adopted: `DOCUMENT_TYPES`, `_ROUTES`, `ENTITY_ROUTES`; no new Finance route, so file clearance is unchanged |
| `3718061` (TASK-2215) | DocuSign-signed files named by rule, full columns (AGREEMENT / FORMULA_APPROVAL, DocStatus Approved) | no mirror: both are defined categories that name no document kind |
| `91840be` (TASK-2221) | template-driven Document trail (record types and attachment categories per step) | no mirror: a Portal view; the Workbench's own chain reads the same links and files |
| `e59f1d7`, `3278952` (TASK-2627), `4601fb9` (TASK-2291) | approved artwork version through PO, receipt and incoming QC; a PO email attaches the approved artwork files (a builder may attach nothing); subscriber recipients, per-party switch, extra CC/BCC | no change: `issued_sends.py` matches only whole-PO-token PDF names; a held or cancelled email is a `send_not_captured` gap as before |
| `eb14fe3` (TASK-2643), `1b02309` (TASK-2650) | portal-born AOs keep their FG lot in portal inventory only | no change: the lot number is still the AO's `mo_code` (`LOT_RULE_SOURCE`) |
| `abf1823` (TASK-2802) | NOTICE rule for QA on an artwork customer decision | `ROUTING_DEPARTMENTS` byte-identical: no permission change |
| `4dc5cdf` (TASK-2809), `e3604ce` (TASK-2805), `3835a8b` (TASK-368) | one costing run in the workbook context (still persisting); `findFrozenFgPins`; lazy Prisma client | §8.1, §3.7 and `SOURCE-NOTES.md` re-cited; no mirror |

### 7.8 Costing re-read at `d4d1bb2` (2026-10-15; repo pin moved from `71f7214`)

Costing half of `1f2d42d..d4d1bb2` (`operating/landed_cost.py` port, its goldens and the costing per-file pins). Diffs read, with what
changed: §4.11 and each pin's `reason` in `verify/upstream-pins.json`.

| Commit | Change | Workbench |
|---|---|---|
| `35cfb18` (TASK-2811) | ready-to-quote census: unrouted labour priced at $0 and reported | ported (`derive_ready_to_quote`), goldens re-run, §4.11 |
| `0f9fbee` (TASK-2948) | one setup-standard read shared with the routing fill | no-op: same minutes; citations moved |
| `eddbfe3` (TASK-2914) | blank routing minutes and client throughput kept on save | no-op: save path |
| `f73bb3a` (TASK-2852), `0a62186` (TASK-2864) | route plan tables; dual-write on each primary routing save; `FG_FROZEN` 409 rolls the save back | no-op: save path, engine reads the primary routing; §4.11 |
| `b8f6e22` (TASK-2853), `371b4cc` (TASK-2865) | `LineOverheadRate`, `User.canManageOverheadRates`, overhead-rate store and sizing, rate-settings audit redaction | no-op: engine does not read the store (ERP-D-022); write-only flag; §4.11 |
| `b19b6d2` (TASK-2861), `d4d1bb2` (TASK-2862) | `TheoreticalFG` cost fingerprint (report only) | no-op: stripped from the quote result |
| `cc2a973` (TASK-2812) | ERP-D-030 Approved: bronze tables stay portal-owned | §4.10 |

### 7.9 Re-read at `d53137e` (2026-10-16; per-file pins only, repo pin stays `d4d1bb2`)

The two costing Portal paths still changed after §7.8 were read from `d4d1bb2`/`1f2d42d` to `d53137e`; every other costing path
(`project-landed-quote.ts`, `quote-inputs.ts`, `fg-routing-store.ts`, `fg-routing.ts`, `rate-masters.ts`, the decision log) is
byte-identical at `d53137e`. `operating/landed_cost.py` keeps `PORTAL_REVISION` = `d4d1bb2`.

| Commit | Change | Workbench |
|---|---|---|
| `8c68104` (TASK-2918) | `TheoreticalFG` freshness columns (`freshness_checked_at`, `_live_hash`, `_engine_version`, `_met`, `_reasons`): the last live `cost_fresh` check, written by Recalc and Activate only | no-op: report-only columns of a table the engine does not read and the Workbench is not served |
| `21d8100` (TASK-2932) | index `app_uat_agent_event (to_status, created_at)` | no-op: an index; no column or enum changed |
| `335dc8a` (TASK-2878, CT-E2) | recalc script: `--apply` also writes `restamp` rows (costs unchanged, fingerprint missing or stale); dry run via `runQuoteProjectLanded` + `landedQuoteOf` | no-op: an operator write the Workbench never runs; §4.1 and `SOURCE-NOTES.md` re-cited |
| `4dc5cdf` (TASK-2809, CT-A), read at `d4d1bb2` | `QuoteInputs.readErrors` names the failed priced-input reads | no-op with reason: `RECORDS_NOT_READ` per relation (§4.11), test extended |
| `b59b007` (TASK-2649) | `projects/read.ts` `loadCustomerLite` reads through `customers/portal-label.ts` `loadCustomerLabel` (portal records first) | the one raw query the goldens' `db_calls` lost at `d4d1bb2`; a display label, prices nothing |

### 7.10 Re-read at `853df2e` (2026-10-16; per-file pins only, repo pin stays `d4d1bb2`)

Of the Portal paths only two changed since their pins; the other
sixteen (`api/attachments/route.ts`, the workbook `context.ts`, `assembly-detail.tsx`, `order-detail.tsx`, `db/db.ts`,
`document-types.ts`, `assembly-lifecycle.ts`, `document-governance.ts`, `folder-taxonomy.ts`, `fp-bridge.ts`, `entity-routing.ts`,
`locations.ts`, `issued-records.ts`, `email-hub/attachments.ts`, `ship-release.ts`, `fg-freeze.ts`) were adopted at `19f1d94`/`40237ed`
(§7.7) and are byte-identical at `853df2e`: no-op.

| Commit | Change | Workbench |
|---|---|---|
| `75b8e4c` (TASK-3059) | Printer order confirmation. FACT: a printer PO is one with a line that carries artwork (`artworkAttachmentId`, `artworkToFollow` or `artworkVersion > 0`; `lib/mrp/printer-order-confirmation.ts@853df2e:34`). STAFF record number, date, an existing `ORDER_CONFIRMATION` file of the PO and per-line quantities in `app_printer_order_confirmation` (+ `_line`, `S:3670-3699`; one per PO, upserted, lines replaced). A PO line with no stored row is *missing*; a row with no `po_line_id` is *extra*; quantities differ beyond 0.0001 (`:91-136`). `POST /api/mrp/receiving/pack` on a printer PO with a line still short (`quantity − quantityReceived`, `:146`) notifies the PO's creator and records Awaiting You event `purchase_order.printer_lines_open` | no mirror. The PO page's Documents categories are byte-identical (`purchase-order-detail.tsx@853df2e:791-798`, was `@19f1d94:788-795`), so `playbook_originals.py` reads `ORDER_CONFIRMATION` as before (consistent with a vendor PI, never a fill source). The new tables are not reporting-API relations: the Workbench cannot see a recorded printer confirmation, and must not read a PO without one as unconfirmed |
| `75b8e4c` (TASK-3059) | seed rule `procurement.printer_lines_open` (NOTICE, department `PROC`, discriminator `receiptId`, `isActive: false` like every seed rule; `seed-rules.ts@853df2e:278-292`) | no permission change: `ROUTING_DEPARTMENTS` (`:19-38`) is byte-identical (18 codes equal `employee_permissions.PORTAL_DEPARTMENT_CODES`); a routing notice grants no read |
| `391b263` (TASK-2979), `a94006e` (TASK-2980) | lead Verify and every project-create path send the brief's `artworkScope`, so class and Quebec checklist rows seed at birth | no mirror: the Workbench does not model the artwork checklist |

### 7.11 Re-read at `f3e3a53` (2026-10-19; per-file pins only, repo pin stays `d4d1bb2`)

Every drifted per-file pin moved to `f3e3a53` after its commits were read; each pin's `reason` names them. Mirror changes, each with a
failing-first test: `document-types.ts:45-46` REFERENCE (`doc_metadata_taxonomy`), `entity-routing.ts:256-257,312-314` CREDIT_APPLICATION
OTHER to Finance (`doc_metadata_taxonomy.ENTITY_ROUTES`; `portal_kind_columns` needs no change), `email-hub/attachments.ts:196-203` hub rows reach the column as OTHER (`issued_sends`),
and the receiving release reading (`process_stage_map.json` `qc_release`: pack release is warn only on Form #012 and QC tests,
TASK-3130/3138, and refuses only 409 `QC_CHANGED`; raw release still blocks). Readings recorded in prose: §2 (Sage regrant, FM write-once
batch columns, DocuSign), §3.14 (SharePoint site and re-point runbook), §3.15 (eQMS COMPLAINT/CCR), §6 (approved catalog, the ten unbound
relations, the `wb_api` conflict). Not adopted: `attachments/access.ts:78-86` (TASK-3075) lets a staff uploader read their own restricted
file; the Workbench capture holds no `uploadedById`, so it keeps refusing (probe in `operating/tests/test_employee_permissions.py`).
Residual: a credit-application file the email hub or the upload route files as column `OTHER` is FINANCE class in the Portal
(`securityClassForRoute`) but outside `FINANCE_CATEGORIES` in the mirror. No-op paths: `source-notes.md` section `mc-permissions`.

### 7.12 Re-read at `c09de5d` (2026-10-20; repo pin moved from `d4d1bb2`)

Each moved pin's `reason` names the commits read.

| Commit | Change | Workbench |
|---|---|---|
| `c2ff912`..`757625d` (TASK-3355) | both allowlists 93 → 102 relations: nine added, `sage.ap_vendor_remit` dropped, `ap_vendor` `taxnbr`/`brn`/`amtcrlimt` excluded, columns moved in eight `app_*` relations (§6 Catalog) | source registry and Portal schema regenerated at `c09de5d`; the nine are bound, `ap_vendor_remit` and the excluded columns are not (`tests/test_source_registry.py`). `schema.portal_queryable` names a served relation the API reports unavailable as failing, so no bound relation is left without a live check |
| `2ec4f32` (TASK-3248) | base lot, expiry and `lots[]` on BC sales shipment lines; bridge 0.9.18.0 (§4.10) | `reconciliation_rules.shipment_line_bc_lots` mirrors it (`operating/tests/test_reconciliation_rules.py` `ShipmentLotTests`); the `serializers.ts` lines `bc_purchase_case.py` cites only moved (pin `reason`), so no change there |
| `26cf705`, `0acbd28`, `2cfc138`, `f13bcbc` (TASK-3369) | Sage `_sage_pk` kept through full and truncate reloads; `termscode` allowed as the `sage.ap_term` key only (§2) | description only, nothing new bound |

### 7.13 Re-read at `0323e19c` (2026-10-20; repo pin moved from `c09de5d`)

Each moved pin's `reason` names the
commits read.

| Commit | Change | Workbench |
|---|---|---|
| `0252da41`, `0f399454`, `22361e0b` (TASK-3390) | both allowlists 102 → 103 relations: `sage.ap_vendor_remit` lifted on its own (16 columns, `_sage_pk` key, D3 columns out); runbook D9 and "Unblocking `sage.ap_vendor_remit`"; the Sage sync finds the `_sage_pk` key by column and restores it by name (§2, §6 Catalog) | source registry and Portal schema regenerated at `0323e19c`; `sage.ap_vendor_remit` is bound because the pinned manifest serves it (`tests/test_source_registry.py`); like every served relation it is named by `schema.portal_queryable`, so it is never `no_live_check` once a daily run reads it |
| `97ce9d67` (TASK-3252) | the receipt and purchase-invoice producers select BC-era POs by `BC_ERA_PO_NUMBER_PATTERN` (§3.5 BC); the shipment lot reader moves one line (`:766-819`) | citations moved in `reconciliation_rules.py` and here |
| `6f197d93` (TASK-3308) | DocuSign webhook links an already-completed request to its signed PDF, no notification (§2) | description only |
| `fc416dff` (TASK-1746) | `attachments/access.ts` comment: a legacy row is streamed from `webUrl`, never a 307; `canReadAttachment` unchanged | re-pinned, no change |

### 7.14 Re-read at `52c9116` (2026-10-20; repo pin moved from `0323e19c`)

Staging `52c9116`, superseding `1d3eaf2`. The `1d3eaf2` paths are byte-identical at `52c9116`. Each moved
pin's `reason` names the commits read. The source registry, the Portal and BC generated schema, the Graph and MCP digests and the
generated Portal error codes are regenerated at `52c9116`. PLX_MC pins are unchanged at `5bf29d2`: no permission change there.

| Commit | Change | Workbench |
|---|---|---|
| `e2728afe6` (TASK-1929) | `WAIVER` document type (`GMP("WVR")`, 7 years, column OTHER; `document-types.ts:93`) filed on a BATCH with its Quality documents (`entity-routing.ts:177`); QC documents (BATCH `COA`, `WAIVER`) readable only by admins and QC-department staff, 404 otherwise, upload refused first (§3 file clearance); the attachments route adds source notes and uploader names | `WAIVER` mirrored in `doc_metadata_taxonomy` `DOCUMENT_TYPES` and `ENTITY_ROUTES` (`tests/test_doc_metadata.py`); the route's column choice is unchanged. The QC read rule is mirrored in `operating/employee_permissions.py:725` `file_class_allows` (§3 file clearance; `operating/tests/test_employee_permissions.py`, `operating/tests/test_case_host_sharepoint.py`); the `access.ts` re-pin is pending, so it stays pinned at `0323e19c` and drifted until then |
| `0af01114c` | batch create writes `batch_item.idlabid` and fills blank lots from Lab IDs on a release status (§4.6) | description only; `manufacturing.json` stands |
| `4d9522be2` (TASK-1927) | `pack-receipt-portal.ts` appends a read-only list of a pack part's quarantined or on-hold receipt lines (`:883-926`) | re-pinned; the receive, QC and release readings `process_stage_map.json` cites are unchanged |
| `d92530b52` (TASK-3525) | both allowlists 103 → 104 relations: `public.app_bc_entity_map` (13 columns, D10); runbook section and `SCOPED_RELATIONS` 19 → 20, grant still to run (§6 Catalog) | bound as `RelPublicAppBcEntityMap` (`tests/test_source_registry.py`); four routes now resolve it; the curated `BcEntityMapping` stays pending; `receipt_packaging`/`receipt_raw_material` cited at lines 1882/1914; `schema.portal_queryable` sized for 104 relations |
| `a2cd328ed`, `1d3eaf22a` (TASK-3212), `48b6cfc67` (TASK-3315) | new route `/api/admin/feedback/archive-list` (StorageLocation only) and Sentiment Survey/Bug list bindings; `excel.ts` back to its earlier Graph calls | registry entries only (route, Graph evidence counts) |

## 8. Do-not list (Workbench must not duplicate or contradict)

### 8.1 Never call these as reads (they write)
`GET /api/mrp/wms/shipments/orders` and page `/mrp/orders/[id]` wrote a gold SalesOrder through `ensureSalesOrderFromFmOe` until `75c7d6d`
#1850 TASK-2355 (re-read at `12db891`): both now only read (`loadGoldOrderPanels`), and a gold twin of an FM order is minted only when a
shipment is created or confirmed. Still never call either from the Workbench;
`GET /api/mrp/quotes/[id]/matrix` and `GET …/workbook` (`quoteProjectLanded` → `app_theoretical_fg` + primary `FgRouting` setup minutes; `persist`
defaults to true and no route passed `false`, re-verified 2026-10-01 at `487862b`; since `4dc5cdf` TASK-2809 (read at `19f1d94`) the workbook
runs `runQuoteProjectLanded` once; since `9108064` TASK-2850 the matrix GET and POST and the workbook context pass `false`,
`matrix/route.ts@71f7214:125,229`, `workbook/_lib/context.ts@40237ed:79-82`, SOURCE-NOTES; still never call either); page `/mrp/project-development/[id]`
(`ensureTheoreticalFgForProjectKey`); `GET /api/mrp/assembly/[id]/license-plates` (`ensureFpProduct` writes `app_product`/`app_zone`);
`GET /api/mrp/fg-routing` (routing refresh; since `255ec2a` RV2a, read at `1f2d42d`, the store fills in memory and no longer saves, still never called); `GET /api/mrp/setup-standards`; `GET /api/mrp/rate-settings`; `GET /api/mrp/3pl/{customers,rate-cards,runs}`
(rate card seed); `GET /api/mrp/quotes/[id]/sign/complete`; `GET /api/runner/v1/runs/[id]/status` (expires runs);
`GET /api/operations/inventory/reorder-alerts?notify`; `GET /api/settings/ui`, `GET /api/admin/form-versions` (DDL); every `/api/cron/*` (including `GET /api/cron/storage-finalize`, which writes
SharePoint columns, retention labels and `app_attachment`, `route.ts@acc0c53`, TASK-2250).
`GET /api/mrp/purchase-orders/[id]/pdf` filed an `ISSUED_PO`/`ISSUED_CUSTOMS` record until `595243f` #1941 TASK-2504; it is now a preview
that files nothing, and `POST …/pdf/issue` (save or print) files the render, nothing new when the bytes match the current render
(`lib/mrp/api-handlers/purchase-order-pdf.ts@595243f:36-98`, `lib/storage/issued-records.ts@595243f:25-29`; mirror `operating/issued_sends.py`).
Several AO query helpers catch errors and return `[]` — empty ≠ none.

### 8.2 Do not re-implement as authority
- Order identity, FM↔SO bridging, BC number alignment, BC entity mapping/outbox/readiness: Portal owns them; the Workbench reads results and records divergence.
- Landed cost / quote engine / quote totals / standard cost / pricing: cite Portal outputs (or recompute exactly per §4 as labeled evidence). Do not invent duty rates, FX, waste or yield.
- Formula % and batch quantities: use `line-percent.ts` semantics; never `perctentry` alone.
- Approvals: Portal `AgentRun` approval (`decideRunApproval`) and MC gates are the only approval authorities; no Workbench approval journal may authorize a business write. Regulated Portal approvals (BOM approve, formula customer sign-off, QC release, eQMS signing) need the signer's own fresh re-authentication (§3.15): no Workbench action, approval or agent may perform or stand in for one. A Portal `app_approval_record` row (§3.17) is a record of a business approval, not an approval of any Workbench action.
- Document taxonomy: reuse `document-governance.ts` column set; do not fork it.

### 8.3 Do not contradict these Portal facts
- FEFO is not uniform (§4.6); do not claim a Portal pick "violated FEFO" without naming the picker.
- Freight/duty are expensed (ERP-D-015 Approved 2026-09-10); "capitalize into item cost" is a proposal that reopens that decision.
- Historical FM receipts are not BC receipts (ERP-D-009); absence of a BC receipt for an old PO is expected.
- `ManufacturingOrder` = AO; `ManufacturingJob` = production demand; SO `MAKE/DISTRIBUTE` ≠ job `MAKE_TO_STOCK/MAKE_TO_SHIP`.
- `Product.code` = BC Item No.; `Product.erpCode` = Sage item; `Customer.erpCode` = Sage IDCUST = BC customer No.
- Regulatory soft delete: Portal never hard-deletes deletedAt models; but lots, receipts, POs, transactions, batch records, quality tests have no
  deletedAt and lines can be hard-deleted — absence of a row is not evidence of anything.

### 8.4 Stale guidance to ignore
- Parent `~/Downloads/CLAUDE.md`: `getApiSession()` in `src/lib/api-auth.ts` (no such function; shim), `src/lib/db.ts` (now `lib/db/db.ts`),
  `src/lib/microsoft-graph.ts` (shim → `lib/m365`), `src/lib/utils.ts` (retired), "Form 129/130, 105/018" (code uses F1–F10, C1–C5 in `lib/form-schemas/index.ts:110-197`),
  regulatory tables `plx_quality/plx_production/plx_warehouse` (comment namespaces, not schemas).
- `docs/modules/order-spine/README.md` links `docs/projects/business-central-erp-modernization/customer-to-cash-data-map.md`, which does not exist.
- Schema comments: `SalesOrder.status` comment list, `ShipmentStatus`/`PurchaseOrderStatus`/`ReceiptStatus` enums (columns are strings with other values), "FEFO" in batching OpenAPI.

### 8.5 Corrections to `docs/reference/source-notes.md`
- Portal HEAD is now `6c9415973` (not `19079ade`). Draft PR #1659 runner API is **merged** (`eceb50c37`); agent run/eval tables merged; #1661 silver % fix merged (`2df404548`).
- "check whether the live key sees the trimmed manifest": resolved — Vince kept the full list on 2026-09-27 (PRD); bundled manifest = 3,399 columns (at `f3e3a53`: approved, 3,752 columns; at `c09de5d`: 102 relations, 4,325 columns; at `0323e19c`: 103 relations, 4,341 columns; at `52c9116`: 104 relations, 4,354 columns, §6 Catalog).
- Admin `plx_` keys now carry scopes and need `external` (TASK-2114).
- "Portal Prisma routes read UAT" is right, and it also means the reporting API does not contain staging-UI (UAT) entries.
- SOURCE-NOTES "AO plan field is `qtyplan_c`": it is a column of the FM link tables `order_entry_item_assembly`/`order_entry_item_batch`; Portal code never reads it.
  AO header ordered = `assembly_order.qtytotal`/`sizepieces`; produced = `totalprodqty_c`; accepted = `qtyacc_c` (last two not in the API).
- SOURCE-NOTES "FM-migrated `custom_fields.custPoCode`": legacy SQL only. Live FM-ensured orders hold custpocode in `po_reference`; legacy
  migration put FM `reference` there (the person-name values).
- SOURCE-NOTES write-on-GET list is incomplete — use §8.1.

## 9. Where current Workbench code diverges from the Portal (checked 2026-09-30 against this commit)

Status at Workbench `main` `bb48071` (re-checked 2026-10-01 by reading the cited code; line numbers in the
table below are the 2026-09-30 ones and have moved): row 1 fixed (`classify_origin` is now at
`operating/customer_po_coverage.py:497` and mirrors `isSageBackedSalesOrder`: trimmed `sageOrdUniq` /
`sageSourceOrdUniq` first, then the source marker); row 2 `CANDIDATE_FIELDS` is unchanged (`:156`) and the
OE id >= 900000 rule was not re-checked; row 3 fixed (`integrations/source_mirror.py`
`sales_order_po_reference_strength` re-rates each `po_reference` from its own row: legacy `SO-FM-*`
= `free_text`, Sage ETL = `external_reference`); row 4 fixed (`ontology/transactions.py:172` `"Supplier": (("name",
"vendor_name"),)`); row 5 fixed in part (`fmId` and `sageOrdNumber` aliases exist, `integrations/portal_bc_links.py`
accepts `salesInvoice` or `salesOrder` map types; that module is retired 2026-10-02, see the BC/UAT section below); row 6 changed (the AO script no longer carries the
trace constants and reads `qty_damaged`/`size_pieces`/`qty_sample` MO columns, `MO_COLUMNS`; the `qtybatch` check
was not re-verified); row 7 changed (the open landed-cost decision is a labelled gap,
`operating/packaging_case.py:896`, and `operating/landed_cost.py` carries the engine); row 8 unchanged.

| Workbench place | Portal fact | Consequence |
|---|---|---|
| `operating/customer_po_coverage.py:classify_origin:267-286` (`source=="sage.oe_order_header"` → sage; `fmId` → fm; any other `source` or both markers → unknown) | Portal's Sage test is `sageOrdUniq` OR `sageSourceOrdUniq` OR that source (`lib/wms/sage-ship-queue.ts:32`); Sage-backed wins over `fmId` (`isFmOnlySalesOrder:41`). Live FM-ensured orders carry `source:"fm-oe-ensure"`; Portal-entered MAKE orders get `fmId` + `source:"gold-so-bridge"` after confirm when the best-effort bridge succeeds (FM customer + `fm-fp-` lines) | Every live FM-ensured order and every bridged Portal-entered order is classified `unknown`; only legacy-SQL rows reach `fm_migrated`. Align the rule with `sage-ship-queue.ts` and the two bridge `source` values |
| `customer_po_coverage.py:79-80` `CANDIDATE_FIELDS` includes `custom_fields.custPoCode`; `_ORIGIN_SOURCES` points FM orders to `public.order_entry.custpocode` | `custPoCode` is legacy SQL only; FM-ensure copies custpocode into `po_reference`; Portal-created FM OE rows (id ≥ 900000) have `custpocode = po_reference ?? order_number` | Treat custpocode on OE ids ≥ 900000 equal to `erpcode` as "no PO", not a PO |
| `integrations/source_mirror.py:116-122,191` (`po_reference` = `free_text`, note "holds person names"; `custom_fields.custPoCode` = customer PO) | Portal resolves orders by `po_reference` only with the order's customer, on customer plus normalized PO (`order-identity.ts:resolveOrderByNumber:98@40f7802`); the person-name values come from the legacy FM `reference` migration; Sage ETL / FM-ensure / UI write real POs | Strength should depend on origin (Sage/FM-ensure/UI = `external_reference`; legacy `SO-FM-*` rows = `free_text`), not one label for the column |
| `ontology/transactions.py:_FIELD_ALIASES:139-155` `"Supplier": (("code","vendor_name"),)` | `Supplier.code` is a code; the name is `Supplier.name` (Customer correctly uses `companyName`) | Vendor-name aliases never match a supplier name; use `name` |
| `ontology/transactions.py` (no `fmId`/`sageOrdUniq`/`sageOrdNumber` aliases on SalesOrder) and `integrations/portal_bc_links.py` (retired 2026-10-02; the order→FM link now lives as binding `SalesOrder.same_record_as` on `custom_fields.fmId`; invoice→order only via `app_bc_entity_map` `salesInvoice`) | Portal links SO↔FM OE by `custom_fields.fmId` (`resolveOrderFromFmOrderId:233@40f7802`) and invoice→order by entity map (`salesInvoice` OR `salesOrder`) then `bc_invoice_snapshot.external_document_number` = order_number (`resolveOrderFromBcInvoiceId:183@40f7802`; since TASK-2401 the snapshot PO fallback is scoped to the invoice customer) | Workbench misses deterministic Portal links; add them as source-asserted (not inferred) edges. `order_number` changes on BC renumber (`align-portal-so-number.ts`) — key aliases on `id`, keep `fmOeNumber`/`renumberedTo` |
| `operating/ao_case_script.py:135-150` (planned = `silver.assembly_order_items.quantity`, manufactured = `actual_amount`, link `qtymfg_c`, damage summed as packs) | AO item `quantity`/`actual_amount` are component qty/consumption (`actualamount = qty_per_batch × totalprodqty_c`, includes samples); FG produced = `assembly_order.totalprodqty_c`, accepted = `qtyacc_c` (both unserved); ordered = `qtytotal`/`sizepieces` (served); FG damaged = `app_manufacturing_order.qty_damaged` (served); Portal never reads `qtymfg_c`/`qtyplan_c` | The 1,509/1,503 item figures equal FG units only if that item's `qtybatch` is 1 (not checked); cite `qtybatch` and check `sizepieces`, `qty_sample`, MO `qty_damaged`; label `qtymfg_c` as an FM calc the Portal does not use |
| `operating/packaging_case.py:571-575` + `process_stage_map.json` ("landed cost treatment is an open decision"; landed = (tier+duty)×multiplier+freight) | ERP-D-015 (Approved 2026-09-10) expenses freight/duty to Sage G/L; quote landed cost uses hard 0.065 / 1.38 (RateSettings values ignored), no FX; map cites `public.receiptraw`/`receiptpack` = compat views of served `public.receipt_raw_material`/`receipt_packaging` | Record "current Portal decision = expense (ERP-D-015); Vince's step 18 asks to reopen it", and cite the served relation names |
| `packaging_case.py` / `customer_po_coverage.py` / mirror assume `app_*` = what staff see in the Portal | Replica is of the staging primary; the staging Portal UI writes UAT | Any "Portal shows X" claim from API rows must say "staging primary (FM/Sage/ETL)", not "what staff entered on staging.plxcustomer.io" |

Re-implemented as labelled evidence (2026-10-01, Portal staging `5724575`): `operating/landed_cost.py` ports the quote / landed-cost engine
(§4.1–4.2, ERP-D-015 G/L rules) line by line in doubles and is golden-tested bit for bit against the Portal TypeScript run stand-alone
(`operating/tests/portal_engine/`, isolated-fixture, fake read-only Prisma, `persist:false`); its outputs are labelled "Portal quote engine
reproduction, not Portal output and not item cost" and every Portal zero/default substitution is a typed unknown. Labour-hour derivation,
formula/BOM selection and Q2-C advisories are not ported. The pallet builder and 3PL skid math (`lib/wms/pallet-build.ts`, `pallet-layout.ts`,
`lib/3pl/skid-math.ts` @`48d76e8`) are ported in `operating/pallet_build.py`, golden-tested against their own TypeScript output; they need
`app_product_pack_spec` case dimensions, which the reporting API does not serve. `process_stage_map.json` `portal_code_readings` cite `@5724575`
(matrix caller `project-landed-quote.ts:1437`). Not re-implemented: FEFO, pricing or formula %; `packaging_case.py` only validates the map; `source_mirror.py` matches the API contract (since modes, overlap, key cursors, 5,000 cap).

process-flow-mapper skill v2 (`48d76e8`; `bb286d7c` #1880 TASK-2457, `9f152406` #1881 TASK-2461): eight guided steps; `SYSNAME`
gains `Sage` ("Sage 300"), mirrored in `CANONICAL_SYSTEM` and the map's `vocabulary.system`. New optional stage fields `today` /
`target` / `cutover`, `views` (decided/built/practised/happened, plus a `disagree` line), `blockers` (FileMaker or `sage.*` reads in
Portal code as cutover blockers), top-level `rules` (stated/proposed/approved) and `checks` (pass/fail/open ×
quantity/money/identity/cardinality/timing), and `ontology.relations` (typed triples) in `--export-ontology`. The static
`operating/process_stage_map.json` uses none of them; `validate_map` still refuses unknown keys, so adopting one is a schema change.

## BC drain, UAT prefix and ETL defect (verified 2026-10-02)
Source: `plx-customer-portal@c3ffda3`. Live number from run `live-2026-10-02T02:02:35Z`.

**BC Production is drained from the UAT database, not from the served replica.**
- `scripts/db-targets.json`: the `staging` branch (`staging.plxcustomer.io`) has `db_target: "uat"`, so the staging Portal app reads and writes `plx-postgres-uat`. FM sync runs on `plx-postgres-staging` every 30 minutes. The UAT database syncs on demand only.
- `portal/src/lib/business-central/document-finance-queries.ts` (header comment) says BC stamps `external_document_number` with the Portal shipment code (for example `UAT-SH100017`), and that after "BC drain align" the Portal SO number may be renumbered to the BC SO.
- `portal/src/lib/utils/uat-prefix.ts` prefixes Portal-generated reference numbers with `NEXT_PUBLIC_UAT_REF_PREFIX`. `portal/.env.example` sets it to `UAT-`. That value is the example default. The deployed value was not checked.
- The reporting API serves a replica of `plx-postgres-staging` (`staging_reporting` in `db-targets.json`). It does not read the UAT database.
- Since TASK-2348 (`plx-customer-portal@8e3a951`, `outbox-producer.ts`, `outbox-drain.ts`, `test-record-guard.ts`), outside Vercel production the drain keeps test/UAT purchase-side records out of BC: suppliers whose code or name starts `UAT`/`PLXTEST`/`TEST` plus `-`, `_` or space, or carries a `UAT-` token, or `custom_fields.bcExclude`; POs numbered `UAT-`, `PLXTEST-`, the `NEXT_PUBLIC_UAT_REF_PREFIX` value, containing `PROOF`, or legacy `PO2-FM-`/`O2C-`. Sales orders and shipments are not filtered, so `UAT-SH...` shipment codes still reach BC. BC purchase documents posted before this change can still name UAT rows.
- Consequence: BC documents can name Portal rows (`UAT-...` numbers) that the served replica never holds. Live case `case.unfamiliar_order` found 40 of 40 sampled BC references unmatched. Classify these as `not_in_served_replica`, not as missing or wrong data. Q1 and Q3 in the open questions cover serving UAT rows.

**Upstream ETL defect: gold child rows grow on every run.**
- `portal/scripts/etl-silver-to-gold.ts` binds `cuid()` as the row id in the inserts for `app_manufacturing_order_line` (`@b4118a3:1348`), `app_assembly_order_damage` (`:1819`), `app_assembly_order_status` (`:1865`), `app_batch_schedule` (`:1911`) and `app_production_task` (`:1989`). `cuid()` is `Date.now()` plus `Math.random()`, so each run makes new ids.
- Each insert says `ON CONFLICT (id)`. A fresh id never conflicts, so the upsert never fires. The script has no `DELETE` for these tables, and the Prisma models for them declare no natural-key unique constraint.
- Result: all five relations gain a full copy of the silver rows every run. More than 1M rows were measured in the served relations. The code was verified, not the row counts.
- Workbench rule: never count or sum rows from these five relations without de-duplicating on the silver source key. Report the defect upstream. Do not change the upstream repo.
