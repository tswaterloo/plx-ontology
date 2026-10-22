# Read-only PLX source bridges

This directory contains read-only source clients and ingestion contracts for
engineering and business evidence. No included fixture is production business
state. The GitHub repository snapshot importer (`integrations/github.py`) and
its pinned snapshots were deleted:
no live-check, MCP tool or console view read GitHub through it, and the upstream
repos are read as checked-out source, never as business evidence.

## Modules

| Area | Modules |
|---|---|
| Portal reporting API | `workbench_read_api.py` (live reader), `source_mirror.py` (permission-enforcing local mirror, config `source_mirror_ingest.json`), `mirror_capture.py` (last complete capture, served stale while the API is down) |
| Microsoft live sources | `live_source_guard.py` (the one Graph/BC sender), `live_source_grammar.py`, `http.py`, `graph_read.py`, `graph_evidence.py`, `graph_directory.py`, `bc_read.py`, `bc_purchase.py`, `bc_case_evidence.py` (config `bc_company_books.json`) |
| Versioned captures (L1) | `capture_store.py`, `capture_mail.py` (scope `mail_capture_scope.json`), `capture_sharepoint.py` (scope `sharepoint_capture_scope.json`; every readable library: `sharepoint_estate_scope.json`), `capture_teams.py` (Teams channel and chat messages; scope `teams_capture_scope.json`) |
| Documents | `document_understanding.py` (see `DOCUMENT_UNDERSTANDING.md`), `doc_convert.py`, `xlsx_extract.py`, `macos_pdf_ocr.swift` |
| Mission Control and brain | `mc.py`, `plx_hub_guard.py`, `vmc.py`, `mc_brain_probe.py` |

All clients are labelled live-unverified until a `verify/live-checks/<id>.json` spec has run
on reviewed main; the tests inject transports.

## Live read clients

`MissionControlReadClient(api_key, operator_email,
repo, transport=...)` calls only MC `GET /api/cursor/self-check`, `/tasks`,
`/context`, `/conflicts` and one exact `/tasks/[id]` (closed grammar in `plx_hub_guard`), using MC's per-agent service key and repository binding. The MC
client rejects an envelope whose `meta.actor.repo` does not match the requested
repo. It never supplies checkout or task mutation methods. Redirects are
rejected before credential forwarding and exception text omits credentials.
Every REST read gets a local `McAuditLog` record (route, argument names, a
salted HMAC of the arguments, outcome; never the key, an email or argument values).

`BrainReadClient(BrainConfig.from_env(...))` (`integrations/vmc.py`) calls
only the brain's 7 key-authenticated GET routes
`/api/vmc/knowledge/agent/{search,items,node,document,subgraph,trail,status}`;
`integrations/plx_hub_guard.py` refuses every other path and method before
I/O. VMC's `/api/vmc/cursor/context` retired with VMC: no client reads it and
task context comes from Mission Control (`mc_get_context` /
`MissionControlReadClient.context`). Brain reads are not permission-trimmed per
employee and every hit is an untrusted lead; no Workbench tool serves them (the
MCP knowledge search and `integrations/knowledge_leads.py` were retired with the
platform MCP host). `MissionControlMcpClient` is the guarded
MCP client; hosts are pinned to the reference-named MC and brain hosts.

- **Read-only by default.** `call_tool` sends only the read tools of mission-control.md
  §3 rows 1-12 (retried on 429/502/503/504 with jittered backoff capped at 4 s;
  `Retry-After` seconds or HTTP-date honoured up to the cap; injected sleep,
  jitter and clock). Every call, refusals included, gets a local audit record
  (`McAuditLog`, optional JSONL path).
- **Writes need explicit opt-in.** Only the reference's W-A tools
  (`mc_create_task`, `mc_checkout_task`, `mc_report_progress`,
  `mc_complete_task`, `mc_request_approval`) can be opted in, via
  `write_tools=` plus `write_approvers=` (humans, never the MC operator or a
  `+tag` alias of it) plus a required `write_ledger=WriteAuthorizationLedger(path)`.
  Each `call_write_tool` needs a `plx_hub_guard.McWriteAuthorization`
  (schema `plx-mc-write-authorization/v1`): a recorded human approval naming
  that exact tool and canonical arguments, inside a window of at most 24 h.
  Its origin is not cryptographically verified; MC's decided approval gate
  stays the authority for business actions. Single use is durable: the
  ledger (flock, fsync, salted HMAC of tool + arguments + normalized approver
  + approval time, not `evidence_ref`) is checked and appended before any
  I/O and shared by every client and process using that file. The write is
  sent once (a lost answer is an unknown outcome: read back, never resend). The NO rows (projects, buckets, routing,
  conflicts, skills submit, swarm tools) can never be opted in.
- **Checkout** (`checkout_task`) always passes `repo=petralabx/<name>` and
  returns `CheckoutResult`: `checked_out` only when MC's `taskId`,
  `actor.repo` (case-insensitive; MC's canonical spelling is kept) and
  `prBodyLine` match the request, or `checkout_held` for the
  409 lease (MC's expiry in `held_until`; never create a new task to escape it).
- Knowledge: `mc_search_knowledge` is the default search path; direct brain
  reads use only the 7 `agent/*` GETs with the R7 pair `BRAIN_BASE_URL` +
  `BRAIN_API_KEY` (`brn_`), else `VMC_API_URL` + `VMC_API_KEY`. Hits are
  untrusted leads (upstream `deterministic_link` 0.9 labels dropped), trimmed
  per actor before the per-actor cache. No write-back path exists.
See `docs/reference/source-notes.md` "Mission Control and brain".

The MC contracts are grounded in MC `src/app/api/cursor/{tasks,context,self-check}/route.ts`,
`src/app/api/cursor/tasks/[id]/route.ts`, `src/lib/mcp/read-actions.ts`,
`auth.ts`, and `envelope.ts` at `aedf50f863289455d14654d676989c79567b1284`.
VMC routes are pinned to agentic-swarm `3d62f72ba5ad481d699f990cacd54b321dc595b5`.
The deployed MC source remains authoritative for task ACLs and checkout.
No MC or VMC service key was available in this environment, so tests inject
deterministic transport responses.

Tests:

```sh
PYTHONPATH=. python3 -m unittest discover -s integrations/tests -v
```
