# Live sources — initial access proof 2026-10-01; latest recorded checks 2026-10-04

Credentials live only in the credentialed environment **`PLX development`**,
which the live checks (`verify/live_verify.py`) use. Development and CI run keyless and
never read live data. Adapters are built against injected transports plus a
`verify/live-checks/<id>.json` spec, and the live-check runner runs those specs against
the credentialed environment on a schedule.

## Sources

The table retains initial access observations. The latest-run section below
supersedes stale counts/status; an earlier successful read is not current universal
availability or proof of every business case.

| Source | Variables (names only) | Proven live read | Notes / traps |
|---|---|---|---|
| **Portal Workbench reporting API** (Portal `public.app_*`, FileMaker `public.*`, `sage.*`, `silver.*`) | `PLX_WORKBENCH_API_URL`, `PLX_WORKBENCH_API_KEY` | **DOWN: HTTP 500 `internal` since 2026-10-03T03:33Z** (last pass 2026-10-02T07:05Z; see the latest live-check result). When up: 93 relations; keyset pages (`app_sales_order` 50 rows/page) | Read-only, no exact-record filter, non-atomic pages. Use `integrations/workbench_read_api.py`. The login expires 2026-12-27. |
| **Microsoft Graph: mail** | `MICROSOFT_GRAPH_TENANT_ID`, `MICROSOFT_GRAPH_CLIENT_ID`, `MICROSOFT_GRAPH_CLIENT_SECRET` (`AZURE_*` is the same app) | app token 200; `users?$top=1` 200; a mailbox `messages?$top=1` 200 | App-only, tenant-wide. Read only. |
| **SharePoint** | the Graph variables plus `SHAREPOINT_SITE_ID`, `SHAREPOINT_DRIVE_ID`, list ids | site **PLXCUSTOMERS** 200; drive root 10 folders (AGENTS, Business, CursorInbox, Customer Documents, Customer_Onboarding, Customers, Documentation, Documents, Exports, General); `sites?search=Quality` gives **QA/QC Department** and petra-staging | Ricardo's originals are in the QA/QC Department site, folder "2026 - AOs" (IDs in `docs/reference/cases-and-artifacts.md`). `SHAREPOINT_DIRECTORY_ID` has a stray trailing character, so use `MICROSOFT_GRAPH_TENANT_ID`. **Built, not yet used for capture:** Portal's one `PLX` site with twelve libraries (`docs/projects/plx-sharepoint-site/README.md@bbf8afc`, readiness review #1772; built per README `@b4118a3:3`, where no library re-point batch run is recorded); today's sources above stay the live truth. Key every file by (driveId, itemId); item ids change when a file moves library. Since Portal `6e76a5b` (TASK-2212/2216, `docs/projects/plx-sharepoint-site/README.md@6e76a5b` step 7) Portal-written files (attachments, DocuSign PDFs, receiving CoAs/slips) resolve their folder through admin `app_storage_location` rows the reporting API does not serve; a folder derived from code defaults is a candidate, not proof of absence. |
| **Business Central** (Production) | `BC_TENANT_ID`, `BC_CLIENT_ID`, `BC_CLIENT_SECRET`, `BC_ENVIRONMENT=Production` | token 200; companies 200: **My Company, PLX Consol, PLX Distribution**; `purchaseOrders?$top=1` 200 | **GET only, always.** It is production and there is no sandbox. |
| **Mission Control MCP** | `MC_MCP_API_KEY` | POST `https://mc.plxcustomer.io/api/cursor/mcp`: initial `initialize` 200 and `tools/list` 200 with **29 tools**; latest pinned run reports **31 tools** | Headers `x-api-key`, `x-mc-operator-email` (allowlisted), `x-mc-repo`, `x-mc-runtime`, and `accept: application/json, text/event-stream`. A GET with the key opens an SSE stream (no reply), so use POST. The key allows writes; see `docs/reference/mission-control.md` for which tools are read-only. Every MC write is mirrored to production SharePoint lists, so it needs human approval. |
| **VMC brain search** | `VMC_API_URL` (missioncontrol.tayloralton.com), `VMC_API_KEY` | `GET /api/vmc/knowledge/agent/search?q=…&limit=…` 200 (a cold start can return 503 first; retry) | The key can also hit trading POST routes, so call only the 7 `/api/vmc/knowledge/agent/*` GETs. Prefer `mc_search_knowledge`. It retires at R11 (needs a `brn_` key); see `docs/reference/vmc-brain-tools.md`. |
| **petralabx GitHub repos** | session git proxy (Claude GitHub App installed on the org) | plx-customer-portal (staging), PLX_MC, agentic-swarm, plx_secondbrain clone in cloud runs (local-inference and skills too) | Read-only reference. Never push. |
| **Trading reporting replica** (Postgres) | none in the cloud | DNS resolves; TCP 5432 **times out** | Private, reachable only through Tailscale on the operator's workstation. It is a separate trading system, not an ontology dependency. |
| **AWS Secrets Manager** | `AWS_*` present | **not usable**: the cloud safety classifier refuses STS and Secrets Manager calls | Use the direct variables above. |

## Hard guards (apply everywhere)

- **Never write to Petra production.** All Graph, SharePoint, BC and Portal access is
  read-only. The shared Entra app has tenant-admin write roles (Mail.Send,
  Sites.Manage.All, Application.ReadWrite.All, BC API.ReadWrite.All …), so read-only
  is enforced **in our code**: one GET-only Microsoft/BC client. The only POST it
  allows is the OAuth token request to login.microsoftonline.com; the other allowed POST
  is the guarded MC JSON-RPC call (`plx_hub_guard`: read tools, writes only with a recorded
  per-call human approval). The CI check `http-egress` forbids other HTTP clients outside
  that module.
- **Never call `MC_GO_LIVE_TEAMS_*` URLs**; they post Teams messages. No Azure
  Document Intelligence (billed per page), no Azure Storage, no AWS.
- **No deploys, spending, third-party contact or external messages.** A test company
  inside BC Production is not a sandbox. Writes inside our own isolated test setup
  proceed. Credentials never grant business-action authority. No Chrome or computer use.
- **Never call write-on-GET Portal routes** (GOV-15).
- **HTTP egress check.** `integrations/live_source_guard.py` is the one Graph/BC sender;
  its OAuth token POST and the guarded MC JSON-RPC POST (`plx_hub_guard`) are the only
  non-GET, and `urllib_sender` refuses every other shape. The CI check `http-egress`
  (`scripts/check_http_egress.py`, AST scan of all product code incl. `scripts/` and
  `verify/`) fails any non-GET `Request` (aliased, getattr/partial, `opener.open(url,
  data)`), `http.client`/`requests`-style imports and `urllib.request` imports outside
  its file allowlist. It is a tripwire, not a sandbox.
- **Credentials: use every credential PLX provided, for whatever the project needs.** Keys live in
  the credentialed environment **`PLX development`** (names in the Sources table above) and, for
  the Portal API key, in the operator's macOS Keychain under service `plx-workbench-api-key`
  (URL in `PLX_WORKBENCH_API_URL`). Development and CI run keyless because code under
  review must not run next to credentials. `AWS_*` is not used.
- **For MC MCP, use read tools only.** Any MC write (create, checkout, report,
  approval request) is an action and needs human approval.
- **Never print, log or commit** secret values, tokens, message bodies, names, PO
  values or bulk business rows. Evidence is counts, ids, status codes and field
  names only.
- **Live checks run only merged code**, in the credentialed environment. Results are
  written to the runner's `results/latest.json`.

## Live-check status (latest recorded run)

Read the current numbers from `results/latest.json`; counts copied
here go stale every 2 h. Durable facts: `portal.health_catalog` fails HTTP 500/internal since
2026-10-03T03:33Z, so every check that `requires` it is skipped (case, eval, profile, schema
and mirror checks), and skips are not evidence. BC, Graph mail, SharePoint, MC and VMC reads
pass.

Bounded successful reads:
- `ao-originals`: six PDFs read, six expected SHA-256 matches.
  `graph.sharepoint_listitem`: six listItems, only ContentType; 162 fields withheld.
- `ingest.use_cases`: AGENTS /Ontology UseCases reached; one ZIP pinned,
  25 members, 13 extracted, five images preserved, one dev stub, zero stated
  outcomes. Extraction review remains for member bounds/unextracted members/images.
  Counts omit original IDs/hashes; they do not establish all curated questions ran.
- `graph.mail_capture`: one mailbox/one folder, four messages/versions,
  four handles resolved offline, zero attachment bytes verified.
  `graph.mail_paging` correctly reports incomplete at `cap:page_cap`.
- `bc.capture_full` passes its check but reports **40/45 units complete** and
  `all_complete=false`. Five manufacturing units are still
  `in_progress/source_unavailable`: purchaseOrderLines, salesOrderLines,
  salesInvoiceLines, items and itemLedgerEntries. BC case-entity/purchase probes
  pass; the single-order purchase probe retains `BC_INVOICES_NOT_FOUND`.
- MC initialize/tools-list reports 31 tools; the brain read probe and VMC search
  pass. `mc.action_journal` is skipped as explicitly disabled pending its
  recorded task/configuration/module prerequisites.

Daily `eval.dev_baseline`, `eval.heldout`, mirror and ontology checks were not
run in this hourly record; `daily_last_run_at` is 2026-10-03T21:28:13Z.
Do not infer their results from this run. The Oct 1 evidence remains historical.
Source access, captured data, question/oracle binding, employee acceptance and
completed business execution remain separate claims. No all-API coverage is
established by these probes.

## Environment bindings (checked 2026-10-14)

- The live checks run in **PLX development**, the credentialed environment.
- Other environments are keyless: the Graph, BC, Portal, MC and VMC variables are absent. The AWS_ACCESS_KEY_ID that is present there is rejected by AWS STS (InvalidClientTokenId, checked about 17:55Z).
