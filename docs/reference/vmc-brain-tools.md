# VMC (agentic-swarm), the brain (plx_secondbrain), local-inference, skills

A navigation map: it names the
exact files to read line by line. It does not replace them.

| Repo (branch) | HEAD read | Committed | Cloud checkout |
|---|---|---|---|
| agentic-swarm (main) | `b1117811c`; pinned files re-read 2026-10-06 at `e228067` (`src/api/fastapi_app.py`, `src/pipelines/internal_rag_ingest.py`); `apps/vmc-web/src/data/vmc-crons.json` re-read 2026-10-18 at `1751d2e` (`06b5b44b2` TASK-2838 re-enables `second-brain-lint` in the registry, §3.1) | 2026-09-28 | `/home/user/agentic-swarm` |
| plx_secondbrain (main) | `a5febed`; `eval/contract/README.md` re-read 2026-10-18 at `b7b9584` (TASK-2725 operator runner `r3-run.sh` and key tiers, first R3 recording, §1 and §6 R3) | 2026-09-29 | `/home/user/plx_secondbrain` |
| local-inference (main) | `b42a341` | 2026-09-29 | `/home/user/local-inference` |
| skills (main) | `9d5483d` | 2026-09-25 | `/home/user/skills` |
| PLX_MC (main), for the retirement spec only | `1fba083` | 2026-09-30 | `/home/user/PLX_MC` |
| Workbench, for the comparison | `9317022` | 2026-09-30 | this repo |

Read 2026-09-30 from local clones. No endpoint was called for this section, and no
upstream repo was changed. Labels: **FACT** means read in code or a doc at the cited
path. **INFERENCE** means a reading of the code; check it before you rely on it.
**LIVE** means observed against a running system. Paths
are relative to the named repo. `vmc/` means `agentic-swarm/apps/vmc-web/src/`.

---

## 0. What changes the Workbench plan (read this first)

1. **The brain's core is in agentic-swarm, which is being retired.** plx_secondbrain
   holds only a stdio MCP client, an eval harness and the R3 contract recorder.
   `plx_secondbrain/service/` does not exist yet. Build only against the 7 key-authed
   GET routes `vmc/app/api/vmc/knowledge/agent/*`. They survive the move unchanged
   (same paths, envelope and header; retirement D3/D4). Read the base URL and key
   from config, never from code.
2. **`/api/vmc/cursor/context` does not move to the brain.** The Workbench calls it
   (`integrations/vmc.py:51-71`). It is not one of the 11 routes in R6's Caddy matcher,
   so it retires with VMC (Stage 4/5). Use MC (`mc_get_context`) for task context.
3. **The Workbench's `VMC_API_KEY` can do far more than read the brain.** Every
   `auth:"key"` route on VMC accepts it, including trading POST routes. A 2026-06
   rotation of that key stopped paper trading (retirement F4, B22). Allow only GET
   calls to `agent/{search,items,node,document,subgraph,trail,status}`. Never call any
   other VMC path with it. Never log it.
4. **The legacy key stops working for brain routes at R11.** After R11, VMC's Caddy
   `@brain` block answers 401 to any key that does not start with `brn_`, and the brain
   drops `VMC_API_KEY_SHA256`. The Workbench needs its own `BRAIN_API_KEY` (`brn_…`)
   from the operator at R7. Ask for it then; don't wait.
5. **The brain holds no business transactions.** It has no order, PO, invoice,
   customer-account, shipment or lot entity type. Its graph is engineering and agent
   memory (sessions, dispatches, decisions, docs, trading research). Transaction
   reconstruction, mail/SharePoint business ingestion, entity resolution and
   permission-trimmed retrieval stay Workbench-owned (FACT: ONTOLOGY.md entity table;
   kb-contract sources).
6. **The brain's labels say less than they seem to.** Every `memory.items` row, any
   agent's `brain_ingest` text included, is mirrored as a `deterministic_link`
   "documents" fact. Its confidence is 0.9 for ingests, and it counts as eligible for
   dispatch briefs. Agent-proposed relations (`inferred_link`, at most 0.7) are
   mirrored as `deterministic_link` too (INFERENCE from code, §2.11). Treat every brain
   hit as an untrusted lead (T3). It never proves a business fact.
7. **Department keys restrict nothing** (B14). Brain reads are not permission-trimmed
   per employee. The only hard boundary is "key callers never see `personal/`". The
   Workbench must apply its own entitlements before retrieval and caching.
8. **"classification" is a name clash.** Upstream it means epistemic class
   (`validated_fact`, `llm_claim`, and so on). In `ontology/core.py:31` it means a
   sensitivity level (`public`/`internal`/`confidential`/`restricted`). Never map one
   onto the other.
9. **Use `mc_search_knowledge` by default for agent search.** It reaches the same brain
   search through a reviewed MC principal, so no trading-capable key sits in the agent
   path, and it moves with MC at R7. Use direct brain reads only for
   node, document, subgraph or trail detail.
10. **Do not write to the brain.** This includes `brain_ingest`, `brain_propose_relation`,
    `ingest`, `relations` and `session-artifact`. Those are production writes, and the
    Workbench's CLAUDE.md forbids them. Their governance is weaker than ours anyway
    (§7.3).
11. **Leave local-inference alone.** The Dell `:4000` gateway and the `local-driver` and
    `local-coder` lanes are trading-lab lanes (fleet D23), reachable only on the tailnet.
    Its extract pipeline (Docling, OCR stub) is not production OCR. Its only shared
    asset is the fleet proxy, and that needs a virtual key nobody has issued us.
12. **LIVE (2026-10-01):** `GET {VMC_API_URL}/api/vmc/knowledge/agent/search?q=test&limit=5`
    with `X-API-Key: $VMC_API_KEY` returns 200. `VMC_API_URL` is
    `https://missioncontrol.tayloralton.com`. The first call can return 503 on cold
    start. `integrations/http.py:get_json` has no retry, so add bounded retry on
    429/502/503/504, as `plx_secondbrain/src/http.ts:27` does.

---

## 1. The repos and their status

| Repo | What it is | Status | Planned destination |
|---|---|---|---|
| **agentic-swarm** | Python swarm (`src/`, FastAPI `src/api/fastapi_app.py`, pipelines `src/pipelines/`) plus the VMC Next.js web app (`apps/vmc-web`, 436 route files, 52 cron routes; F1). Hosts the brain's whole core: `vmc/lib/vmc/knowledge/` (19 files), `vmc/lib/vmc/memory-graph/`, migrations, crons, search through swarm `/memory/search/v2`. Also trading lab, dispatch, todos, email, compute. | LIVE at `missioncontrol.tayloralton.com` (EC2, `vmc-web.service`, `next start --port 3100`, Caddy TLS; B13). Being retired: R1 stops merged 2026-09-28 (`84dd6f106`, #1629, TASK-2039). | Brain → plx_secondbrain/service (R4–R12). Trading lab keeps the repo, renamed at the end (D8, T3). Every other VMC area retires unless R0 shows human use (D9). |
| **plx_secondbrain** | Operator-local stdio MCP server `plx-brain` (`index.ts`, `src/{tools,resources,http,config,schemas,budget}.ts`; 11 tools, 5 resources); retrieval eval `eval/second-brain/`; R3 contract recorder `eval/contract/`. "never touches the database directly" (README). | Client plus eval only. R3 code merged 2026-09-29 (`a5febed`, TASK-2087). Since `b7b9584` TASK-2725 (2026-10-16) `eval/contract/recordings/` holds one admin-tier recording (`vmc-api-key-r32026100621239a02.json`, 94/94 matched); the scoped-key tier has no run. `service/` absent. | Becomes the brain service: `service/` Next.js app on its own EC2 at `brain.plxcustomer.io` (D5), with its own RDS (D13). |
| **local-inference** | LLM inference control plane. Dell LiteLLM `:4000` (`litellm/config.yaml`: `local-primary`, `local-fast` = Qwen3-32B-AWQ; `local-coder` Spark A; `local-driver` Spark B). Fleet proxy config `litellm/fleet.yaml` + `deploy/fleet-proxy/` (cloud aliases only, `:4001`). `.orchestrator/sharepoint-doc-org-harness/` (VincePersonal SharePoint/Outlook organizer). | Dell gateway live, tailnet `100.103.33.54:4000` (README; trading uses it). Fleet proxy config merged 2026-09-29 (TASK-2070); no host deploy evidence in the repo. | Unchanged. The fleet proxy becomes the one agent model gateway (fleet D5). |
| **skills** | Catalog of Cursor/Claude/Codex skills: `skills/<id>/SKILL.md`, `manifest.json` (78 skills, pack `plx-engineering-core`), `schemas/manifest.schema.json`, sync and distribute scripts. No runtime, no data. | Live catalog. R7 edits `skills/session-brain/SKILL.md:47` (brain URL). | Unchanged. |

Dispatch: VMC `todo-dispatch` and `dispatch-poll` crons (`config/vmc-web-crontab:58-59`),
swarm dispatch writes to `command_center.memory` (F23), MC `dispatch_to_swarm`
(default off; F11), and portal's dormant `swarm-dispatch-mcp`. All of it retires.
**Never integrate with swarm dispatch.** Objectives go to MC tasks.

---

## 2. The brain's data model

### 2.1 Where it lives
Postgres DB `command_center` (VMC `DATABASE_URL`), schema `memory`, next to VMC's
projects, todos, login and trading-support tables (B9). Graph tables reference trading
and economy rows only through opaque provenance, never through cross-database foreign
keys (`docs/memory-graph/provenance-contract.md`). Migrations live in
`vmc/lib/vmc/db/migrations/`: `004, 021, 022, 029, 030, 032, 042, 047, 048, 052, 053, 068`
(048 also alters `public.vmc_decisions`). There is a Python twin of the base tables in
`db/memory_schema.sql` and `src/memory/store.py:358-395`.

### 2.2 `memory.*`: 12 tables and 1 view (B23; FACT from migrations)
| Table | Key / identity | Notable columns | Migration |
|---|---|---|---|
| `items` | `id uuid`; UNIQUE `(tenant_id, namespace_key, memory_key)` | `namespace text[]` (CHECK `namespace[1]='swarm'`, ≥2 parts, NOT VALID; 022), `namespace_key`, `memory_key`, `memory_type` (note, session_artifact, dispatch_summary, document_chunk, kb_raw, knowledge_chunk, insight, rollup_brief, …), `content jsonb`, `metadata jsonb`, `importance 0-100`, `confidence real` default 1.0 (048), `is_deleted` (soft delete), `version`, `expires_at`, `summary_l0/l1/l2`, `hotness`, `access_count`, `half_life_days`, `workflow_run_id`, `todo_id`, `parent_memory_id`, `content_tsv` (GIN FTS), `embedding vector(1536)` HNSW cosine | 021, 022, 048 |
| `events` | `event_id bigserial` | item_id, namespace_key, memory_key, event_type, payload | 021 |
| `links` | `link_id bigserial`; UNIQUE `(from_item_id,to_item_id,relation_type,tenant_id)` | weight, metadata, `valid_from`/`valid_to` (048). Upserted in place, no `updated_at` (B28) | 021, 022, 048 |
| `knowledge_links` | `id bigserial`; UNIQUE `(memory_item_id,document_path,link_type,tenant_id)` | item→repo document path links | 029 |
| `attempt_packets` | uuid; UNIQUE `(tenant_id,task_fingerprint,dispatch_id)` | swarm dispatch attempts | 032 |
| `quantized_embeddings`, `quantized_index_state` | `(item_id,embedding_model,method,bits_per_dim)`; `(method,bits_per_dim)` | TurboQuant sidecar | 047 |
| `graph_episodes`, `graph_entities`, `graph_facts`, `graph_fact_sources`, `graph_quarantine` | see 2.3 | | 052, 053, 068 |
| view `items_effective` | | `effective_importance` with half-life decay; excludes `is_deleted` | 022 |

### 2.3 Graph tables (`vmc/lib/vmc/db/migrations/052_memory_graph_foundation.ts:82-215`)
- `graph_episodes` is an immutable raw source event: `domain`, `source_database`
  (command_center|trading|economy|filesystem|sharepoint|external), `source_schema`,
  `source_table`, `source_primary_key`, `source_uri`, `source_hash` (required),
  `source_version`, `observed_at`, `ingestion_run_id`, `extractor`, `payload`. Its
  idempotency key is a UNIQUE index over tenant, domain, source ref, hash, version and
  extractor.
- `graph_entities`: UNIQUE `(tenant_id, domain, entity_type, entity_key)`, `display_name`
  (presentation only), `source_authority 1-7`, `metadata`.
- `graph_facts`: `subject_entity_id` → entities (RESTRICT), `relation_type`,
  `object_entity_id` or `value jsonb` (one of them required), `fact_key` UNIQUE
  `(tenant_id, domain, fact_key)`, `classification`, `confidence numeric(4,3)` in [0,1],
  `source_authority`, `valid_at`, `invalid_at`, `invalidated_by_fact_id`,
  `invalidation_reason`. Partial indexes cover `invalid_at IS NULL`.
- `graph_fact_sources`: PK `(fact_id, episode_id)`, `evidence_path` (`<schema>.<table>:<pk>`).
- `graph_quarantine`: the source ref, `ingestion_run_id`, `extractor`, a machine-readable
  `reason`, and `payload`.
- The `domain` column is a storage partition: memory, knowledge, documents, dispatch,
  projects, lessons, trading-v2, governance (`memory-graph/types.ts:1-9`). It is **not**
  the department axis.

### 2.4 Entity types: 27 (`docs/knowledge-os/ONTOLOGY.md`; `vmc/lib/vmc/memory-graph/types.ts:19-47`; DB CHECK 068)
Core (052): `agent, project, todo, decision, memory_item, document, dispatch, module,
incident, trading_symbol, trading_rule, market_regime, trading_signal,
trading_hypothesis, validation_task`.
Extension (068): `concept, person, organization, source, formulation, process,
equipment, material, quality_spec, strategy_decision, skill, mcp_server`.
There is no business-transaction type. In practice the deterministic ingester creates
only `memory_item` entities (`memory-graph/ingest.ts:605`). The other 26 types appear
only through LLM extraction (§3.1) or swarm agent tools (B28). Adding a type takes one
PR with an owner, source, lifecycle rule, ONTOLOGY row, types.ts entry, widening
migration and the parity test
`vmc/lib/vmc/__tests__/knowledge-os-ontology-contracts.test.ts`.

### 2.5 Relation types: 22, typed and directed (`types.ts:51-74`)
Core (052/053): `mentions, documents, decides, compiled_from, supersedes, invalidates,
depends_on, blocked_by, caused_by, supports, contradicts, observed_in, tested_by,
promoted_by`.
Extension (068): `references, evolves_from, part_of, derived_from, answers, implements,
produced_by, measured_by`.
Reverse traversal is a retrieval feature, not a stored relation. From/to conventions
are in `docs/memory-graph/relation-ontology.md:38-56`. The LLM extractor maps 30
reviewed synonyms (`src/pipelines/knowledge_extract.py`, `_RELATION_TYPE_SYNONYMS`);
anything else goes to quarantine.

### 2.6 Classification, confidence and authority (FACT)
- Classification is exactly one of `validated_fact, operator_decision,
  deterministic_link, llm_claim, inferred_link, trading_hypothesis`. Only the first
  three are "dispatch eligible", and graph retrieval returns only those three
  (`memory-graph/retrieve.ts:30-34`).
- The confidence rubric (`ONTOLOGY.md`): 1.0 is an operator decision or deterministic
  row; 0.8-0.99 is deterministic extraction; 0.5-0.79 is LLM or a weak match; 0.2-0.49
  is inferred; below 0.2 is audit only. `llm_claim` is capped at 0.7 at entry. Agent
  relation proposals are capped at 0.7 (`knowledge/relations-core.ts:20`).
- Promotion of `llm_claim` or `inferred_link` to `validated_fact` happens only by
  operator action or a deterministic corroboration rule. It writes a new revision and
  never erases history. Confidence alone never promotes
  (`docs/knowledge-os/GOVERNANCE.md` "Promotion Rules").
- `source_authority` ranks 1-7 and is a retrieval signal only: 1 operator/governance,
  2 migration/code, 3 structured row, 4 hashed document, 5 dispatch summary,
  6 LLM extraction, 7 inferred (`docs/memory-graph/relation-ontology.md:101-113`). The
  deterministic ingester writes 3; LLM extraction writes 6.
- Item-level `memory.items.confidence`: human 1.0, dispatch 0.9, consolidation 0.7
  (048 header). Session artifacts and `brain_ingest` write 0.9
  (`session-artifact/route.ts:17`, `ingest-core.ts:30`).

### 2.7 Identity rules (FACT)
- `entity_key` is the stable primary key of the canonical source row, verbatim
  ("the graph never invents identity"). For `memory_item` that is `memory.items.id`.
  Entities with no DB source use a kebab-case slug of the normalized name, and
  extractors must reuse an existing key ("resolution before creation"). Re-ingesting
  upserts on the 4-tuple.
- `fact_key` is `<source_kind>:<source_pk>:<sha256(stable source payload)>`, for example
  `memory_item:<uuid>:<hash>` (`ingest.ts:617`) or `memory_link:<link_id>:<hash>`
  (`ingest.ts:958`). LLM facts use `memory_item:<item_id>:<relation_hash>`
  (`knowledge_extract.py:549`). A content change gives a new hash and therefore a new
  fact. Corrections are a new fact plus `supersedes` and `invalid_at`, never a rewrite.
- `memory_key` for `brain_ingest` and REST `ingest` is
  `brain-<first 32 hex of sha256(normalized payload)>` (`knowledge/ingest-core.ts:126-153`),
  so retries are idempotent. For session artifacts it is the `session_id`.
- Episode idempotency: same source ref and same hash is a no-op; a new hash makes a new
  episode (`provenance-contract.md` "Episode Rules").

### 2.8 Provenance and quarantine (FACT)
Every fact needs one classification, a confidence in [0,1], and at least one episode
through `graph_fact_sources`. A fact without provenance is invalid and is never
returned. Quarantine, never silent coercion, applies when: the hash is missing, the
tenant is missing, the source ref is malformed, the relation or entity type is
unknown, the confidence is out of range, the classification is missing, or a trading
hypothesis targets execution. Quarantined rows show in lint and never in retrieval
(`provenance-contract.md` "Quarantine"). Contradictions are first-class data: lint
files `contradicts` facts and `insight` items and never deletes
(`GOVERNANCE.md` "Contradiction Policy").

### 2.9 Temporal model: uni-temporal (FACT)
- Facts carry `valid_at` (default `now()`; the ingester sets it from the item's
  `updated_at`), `invalid_at`, `invalidated_by_fact_id` and `invalidation_reason`.
  Retrieval excludes invalidated facts unless `includeInvalidated` is set.
- Episodes carry `observed_at`. `links` carry `valid_from`/`valid_to`. Items carry
  `created_at`/`updated_at`, `expires_at`, `is_deleted` and `version`.
- There is no recorded-time axis and no "as known at T" read. TASK-477's bitemporal
  `valid_during`/`recorded_during` is not implemented anywhere.
- Ranking recency uses a 90-day half-life on `valid_at` (`types.ts:167`).

### 2.10 Namespaces and ACL (FACT)
- Prefixes on `namespace_key`: `personal/`, `project/<slug>/`, `dept/<id>/`, `company/`.
  Because of `chk_agent_namespace` they are stored as `swarm/...`. Legacy writers are
  classified in `knowledge/namespace-scope.ts:15-28`, and `swarm/brain-ingest` and
  `swarm/session-artifact/<runtime>` count as **unscoped**.
- Session (browser) auth sees every scope. Every key caller is denied `personal`
  (`namespace-scope.ts:135-145`, and in each agent route through `excludePersonal: true`).
  Dept grants (`VMC_SCOPED_API_KEYS`) are computed (`ctx.keyGrant`,
  `api/api-handler.ts:502-557`), but no read core calls
  `buildKeyGrantNamespacePredicate`/`isNamespaceAllowedByGrant`. A dept key therefore
  reads the whole non-personal corpus (B14; `KEY_TIERS.md` "Route adoption").
- The tenant is bound server-side to `plx` when `VMC_KEY_TENANT_BINDING_ENABLED`
  (the default) is set (`api-handler.ts:78-90`).
- Departments: `dev, research, qa, ops, trading, manufacturing`
  (`config/departments.yaml`) plus `personal`. There is no per-employee identity
  anywhere in the brain.
- Known residual: graph trail and subgraph can expose personal `memory_key` display
  names through deeper hops (`GOVERNANCE.md` "Namespace Model"). Ingest now skips
  personal items (`ingest.ts:21-25`).

### 2.11 Where the code differs from the docs (INFERENCE unless marked; verify before relying)
1. **Every item becomes a "documents" fact.** Each `memory.items` row becomes a
   `memory_item` entity plus one fact: `relation_type='documents'`, `object NULL`,
   `value` = content, `deterministic_link`, confidence = `items.confidence`
   (`ingest.ts:203-211, 764-779`; FACT). Any agent text sent through `brain_ingest`
   therefore becomes a dispatch-eligible 0.9 fact.
2. **Agent-proposed relations lose their classification in the graph.**
   `brain_propose_relation` writes `memory.links` (or `knowledge_links`) with
   `metadata.classification='inferred_link'` and weight ≤0.7
   (`relations-core.ts:96-146`; FACT). `ingestMemoryLinksToGraph` then mirrors
   `memory.links` as `deterministic_link` facts with confidence = weight. It does not
   filter on `metadata.classification` (`ingest.ts:262-300, 956-960`;
   `runDeterministicMemoryGraphIngestion` `ingest.ts:982-991` runs links too).
3. **The link mirror knows only the 14 core relations.** Its allow-list
   (`ingest.ts:44-59`) misses all 8 extension types, so a proposed `references` link
   (the R3 probe type) is quarantined as `unknown_relation_type` instead of mirrored.
4. **LLM extraction writes `domain='memory'`, source `command_center`, authority 6**
   (`knowledge_extract.py`, `_GRAPH_DOMAIN`/`_SOURCE_AUTHORITY_LLM`; FACT).
5. **Direct writes leave `embedding` NULL.** This covers `ingest`, `session-artifact`
   and MCP `brain_ingest`. No job backfills them, so they rank by keyword only (B7;
   FACT). R9 must build that backfill.
6. **The env flags in `config/aws-secrets-flags.yaml` declare every knowledge flag `"1"`
   for `prod/ec2-secrets`** (FACT, lines ~645-830). B23 confirms
   `MEMORY_GRAPH_INGEST_ENABLED=1` in production. For the real state, read
   `agent/status` → `data.flags` (§4.1). Don't trust the yaml.

### 2.12 Comparison with the Workbench model
Workbench files: `ontology/core.py` (OntologyStore, SQLite), `ontology/transactions.py`
(TransactionResolver), `operating/cases.py` (case event log).

| Concept | Brain (upstream) | Workbench | Verdict |
|---|---|---|---|
| Identity | `(tenant, domain, entity_type, entity_key=source PK)` | `EntityRef(source, entity_type, native_id)` + tenant (`core.py:60-71`, key `core.py:167`) | **Aligned.** Both reuse the source PK and never mint ids. Keep ours, because it keys on `source`, which the brain lacks: brain `domain` is a storage partition, not an authority. |
| Cross-system sameness | Slug reuse by extractors; no reviewed same-as | `same_as` relation only under authority `identity-review` (`core.py:1104-1106`, `map_identity` `core.py:556-563`) | **Workbench owns this.** The brain has no entity resolution. |
| Type registry | Fixed 27 types; 3-way parity (doc, types.ts, CHECK) | Runtime registry `entity_type`/`entity_authority`/`entity_field` (`core.py:175-191`); `register_plx_schema` (`core.py:1003-1106`): 23 Portal types, BC snapshots, WorkItem; `transactions.py:43-61`: ~55 transaction types | **Disjoint vocabularies.** Overlap in name only: brain `formulation`/`material`/`process`/`equipment`/`quality_spec` vs Portal `Formulation`/`Product`/`QcSpecification`. The brain's are free-text research concepts with slug keys; don't join them. |
| Relation registry | 22 untyped-endpoint relations | Typed contracts with left/right type plus authorized authorities (`relation_type` table; ~30 Portal relations; `depends_on`, `same_as`) | **Ours is stricter.** Shared names (`depends_on`, `part_of`) mean different things. Don't import. |
| "classification" | Epistemic class (6 values) | Sensitivity `LEVELS` public…restricted (`core.py:31`) | **Name clash.** In any bridge, map brain classification to our `provenance`/evidence kind, never to `classification`. |
| Authority | `source_authority` 1-7 rank (a ranking signal) | `authority` = the source label the registry allows to assert (`portal-gold`, `bc-snapshot`, `mission-control`, `identity-review`, `isolated-assessment`) and enforced (`_check_entity_authority` `core.py:414`) | Different semantics. Ours is a write gate; theirs is a ranking weight. |
| Confidence | numeric per fact; rubric; llm cap 0.7 | `transaction_candidate.confidence` plus human review status (`transactions.py:763-772`); case findings `fact`/`interpretation` + `supported`/`contradicted`/`unknown` (`cases.py:50-51`) | **Align vocab when exporting:** our human-accepted ≈ `operator_decision`, a source FK ≈ `deterministic_link`, a model suggestion ≈ `llm_claim` (≤0.7). Reuse the rubric numbers if we ever emit to TASK-477. |
| ACL | Namespace prefix; personal-only boundary; no per-user | Per-record `acl` principals + `classification` clearance + tenant; `Actor` (`core.py:74-84`); `_visible` (`core.py:602-608`); permission-quarantine tombstones (`core.py:638-645`) | **Workbench owns entitlements.** Never widen a reader's view with brain content. |
| Temporal | Uni-temporal `valid_at`/`invalid_at`; append-only facts | Append-only event log per key with `revision`, `observed_at`, `last_seen_at`, late/historical events and conflicts (`core.py:443-502`); `get(at=)` as-of reads (`core.py:647-654`) | **Ours is richer** (observation time plus sighting). Neither has a recorded-time column; `event_id` order is only an implicit one. If TASK-477 export matters, add recorded time on our side. |
| Deletion | `items.is_deleted` soft delete; facts invalidated, never deleted | `tombstone` events; current revocation hides history (`core.py:647-651`) | Compatible in spirit. |
| Provenance | Episode (source db/schema/table/PK/URI, hash, run id, extractor) + `evidence_path` | `provenance` string + `revision` on each event; transaction candidates carry `evidence_json` + `evidence_signature` (`transactions.py:763-772`); case citations re-resolved per reader with content digest (`cases.py:1-25`) | **Reuse the episode field set as our export shape** (source_database→`source`, `source_primary_key`→`native_id`, `source_hash`→`revision`, `extractor`→our resolver version `identifier-groups/v1` `transactions.py:176`). |
| Quarantine / conflict | `graph_quarantine` with reason | `Conflict` raises on revision reuse or simultaneous revisions (`core.py:27, 464-479`); `GAP_STATES` (`cases.py:52`) | Equivalent intent: never coerce. |
| Business transactions | **Absent** (no order/PO/invoice/shipment/lot types; ingest sources are repo docs, sessions, dispatches, decisions, VincePersonal) | `transactions.py`: identifier groups, membership/reference candidates, human pair review, incremental reconcile (`transactions.py:1-21`) | **Workbench must own this.** |
| Cases / investigations | Nearest: `insight` items from lint, `vmc_decisions`; no case record | `cases.py`: hash-chained append-only `case_event` (`cases.py:441-452`), findings, Decimal compute, gaps, proposals ("requires approval through the existing approval journal", `cases.py:60`), verification | **Workbench must own this.** Do not store cases in the brain. |

What to reuse: the brain's provenance field set, the confidence rubric numbers and the
"never coerce, quarantine with reason" rule as export conventions. Keep its
SessionArtifact v1 schema (`vmc/lib/vmc/knowledge/session-artifact.ts`; canonical copy
moves to plx_secondbrain in R4) in mind if writes are ever approved. Nothing in the
brain's schema should be imported into `ontology/`.

---

## 3. Ingest pipelines, crons and flags

Crons are curl calls from the VMC host's live `ubuntu` crontab into
`vmc/app/api/cron/<id>/route.ts` (`auth:"cron"`, `CRON_SECRET`). Most only post
`/pipelines/run` to swarm-prod Python. The mirror is `config/vmc-web-crontab` (header:
the live crontab is the truth, and `apps/vmc-web/src/data/vmc-crons.json` is only a UI
registry, so `enabled:false` there does not stop a job). Times are UTC unless `TZ=` is
given.

### 3.1 Jobs that write the brain (B8 lists 15)
| Job | Schedule (crontab line) | Runs | Reads → writes | Gate |
|---|---|---|---|---|
| `second-brain-ingest` | `0 */4 * * *` (115) | swarm `src/pipelines/second_brain_ingest.py` (wraps `internal_rag_ingest`), then in-route **deterministic graph ingest** of items and links | repo markdown, prompts, skills, docs → `memory.items` (kb chunks, embedded) → `graph_*` | `VMC_SECOND_BRAIN_INGEST_ENABLED`; graph `MEMORY_GRAPH_INGEST_ENABLED`; supersession soft-archive `KB_AUTO_ARCHIVE_SUPERSEDED_ENABLED` |
| `internal-rag-ingest` | `30 2 * * *` (112) | `src/pipelines/internal_rag_ingest.py` | repo docs → `document_chunk` rows in `knowledge/documents`, `swarm/rag/<slug>` | `VMC_DOC_INDEXING_ENABLED` (py:925@e228067). Since `ee0f75f` TASK-2364 it re-embeds only chunks whose `chunk_hash` changed, in batches of 100 with a 1 s gap, and a failed hash read fails the run instead of re-embedding everything |
| `second-brain-dispatch-ingest` | `0 */2 * * *` (116) | `src/pipelines/dispatch_ingest.py` | completed swarm dispatches → `kb_raw` + SessionArtifact-shaped items | `VMC_SECOND_BRAIN_DISPATCH_INGEST_ENABLED` |
| `second-brain-compile` | `0 1 * * *` (117) | `src/pipelines/knowledge_compile.py` | enriches `kb_raw`/`knowledge_chunk` in place (LLM summaries, entities, tags); optional wiki export to `data/brain-wiki/` | `VMC_SECOND_BRAIN_COMPILE_ENABLED`, `VMC_SECOND_BRAIN_COMPILE_BATCH`, `VMC_WIKI_EXPORT_ENABLED`/`_DIR` |
| `wiki-compile` | `0 3 * * *` (110) | in-route plus strict-local `knowledge_compiler` (local-driver) | wiki_article items | n/a |
| `second-brain-lint` | `0 3 * * 0` (118); crontab runs it; the registry said disabled until `06b5b44b2` TASK-2838 (2026-10-14) set it `enabled: true`, `status: active` (`apps/vmc-web/src/data/vmc-crons.json:439-440@1751d2e`) | `src/pipelines/knowledge_lint.py` (+ `knowledge_writeback.py`) | detects stale, orphans, contradictions, coverage gaps → `insight` items + `kb_lint_report`; soft-archive exceptions only | LLM stance check under `MEMORY_GRAPH_LLM_EXTRACTION_ENABLED` |
| `kb-decisions-sync` | `0 */6 * * *` (111) | `src/pipelines/kb_decisions_sync.py` | `vmc_decisions` → `kb_raw` items | n/a |
| `rollup-distill` | `0 4 * * 0` (172) | `src/pipelines/rollup_distill.py` | session_artifact/dispatch_summary/decision → `rollup_brief` in `swarm/rollup/<dept>` and `swarm/rollup/company` | `VMC_ROLLUP_DISTILL_ENABLED`, `_WINDOW_DAYS` |
| `lessons-auto-extract` | `0 8 * * *` (77) | `src/pipelines/lessons_auto_extract.py` | failed dispatches → `lessons_auto` | `LESSONS_AUTO_EXTRACT_ENABLED`, `IMMEDIATE_LESSON_ENABLED` |
| `memory-summarize` | `0 2 * * *` (108) | `src/pipelines/memory_summarize.py` | fills `summary_l1/l2` on `dispatch_summary` | n/a |
| `sharepoint-sync` | `0 */4 * * *` (109) | in-route Graph read of **VincePersonal** only, then swarm `/memory/put` namespace `sharepoint` | SharePoint metadata → items | n/a |
| `second-brain-cron-watchdog` | `15,45 * * * *` (173) | read-only streak check; alerts gated | n/a | `VMC_SECOND_BRAIN_CRON_ALERT_ENABLED` |
| Lobster lanes `kb-compile`, `kb-lint`, `institutional-distill` | Lobster scheduler (not crontab) | `src/pipelines/kb_compile.py` (writes items + embeddings), `kb_lint.py`, `institutional_distill.py` | | B8; `src/pipelines/registry.py:192-203` |
| `knowledge-extract` (LLM → graph) | **no crontab line** (B8); registry `30 */2 * * *` enabled; route `vmc/app/api/cron/knowledge-extract/route.ts` | `src/pipelines/knowledge_extract.py` via `strict_local_chat` (local-driver) | session_artifact/dispatch_summary/note/document_chunk ≥100 chars → `llm_claim` facts ≤0.7, 5-15 per source, quarantine on invalid | `MEMORY_GRAPH_LLM_EXTRACTION_ENABLED`, `KNOWLEDGE_EXTRACT_BATCH_SIZE` (20, max 200) |

Other writers: key-authed `POST ingest`/`relations`/`session-artifact` and MCP write
tools (§4); IDE session-end hooks (`scripts/cursor-hooks/session_artifact_lib.py`,
PLX_MC `scripts/compliance-closeout.mjs`), whose failures queue in
`artifacts/session-brain/` with no auto replay (B19); swarm dispatch completions and
`memory_store` (F23); research-run failure lessons in `swarm/lessons/research/<project>`
(B20); swarm agent tool `brain_propose_relation` writes `graph_*` (B28). Dormant and
never referenced: `src/pipelines/kb_email_sync.py`, `kb_sharepoint_sync.py`,
`kb_meeting_sync.py`. `email-ingest` (`15 * * * *`) reads one Graph mailbox into VMC's
`email_triage_queue`, **not** the brain.

Not ingested by anything: Portal business records, FM/Sage, BC, PLXCUSTOMERS
SharePoint, business mailboxes, mail attachments, scans/OCR.

### 3.2 Flags (`GOVERNANCE.md` "Flag Registry"; declared values `config/aws-secrets-flags.yaml`)
| Flag | Meaning | Default | Declared prod |
|---|---|---|---|
| `VMC_MEMORY_CANONICAL_ENABLED` | memory substrate prerequisite (`docs/VMC_MEMORY_CUTOVER_RUNBOOK.md`) | 0 | 1 |
| `VMC_SECOND_BRAIN_INGEST_ENABLED` | repo corpus ingest pipeline | 0 | 1 |
| `VMC_SECOND_BRAIN_DISPATCH_INGEST_ENABLED` | dispatch outputs → items | 0 | 1 |
| `VMC_SECOND_BRAIN_COMPILE_ENABLED` | LLM compile/enrich | 0 | 1 |
| `VMC_SECOND_BRAIN_CRON_ALERT_ENABLED` | watchdog email/escalation todo | 0 | 1 |
| `VMC_ROLLUP_DISTILL_ENABLED` | dept/company rollup briefs | 0 | 1 |
| `VMC_WIKI_EXPORT_ENABLED` | wiki markdown export | 0 | 1 |
| `KB_AUTO_ARCHIVE_SUPERSEDED_ENABLED` | soft-archive superseded chunks | 0 | 1 |
| `MEMORY_GRAPH_INGEST_ENABLED` | deterministic graph writes (crons + after every ingest/session-artifact write) | 0 | 1 (B23 confirms) |
| `MEMORY_GRAPH_RETRIEVAL_ENABLED` | graph search modes (`graph|local|global|path|brief`); off = hybrid only | 0 | 1 |
| `MEMORY_GRAPH_LLM_EXTRACTION_ENABLED` | LLM extraction + lint stance check | 0 | 1 |
| `MEMORY_GRAPH_AGENT_TOOLS_ENABLED` | swarm LangChain graph tools (`src/tools/brain_graph_ops.py`) | 0 | 1 |
| `MEMORY_GRAPH_DISPATCH_BRIEFS_ENABLED` | `[GRAPH_BRIEF]` in swarm dispatch context | 0 | 1 |
| `NEXT_PUBLIC_VMC_MEMORY_GRAPH_UI_ENABLED` | UI visibility (build time) | 0 | 1 |
| `BRAIN_MCP_HTTP_ENABLED` | kill switch for `/api/vmc/knowledge/mcp`: `0` gives 503; unset = on | unset | n/a |
| `BRAIN_MCP_ENABLED` | kill switch for stdio plx-brain: `0` exits | unset | n/a |
| `VMC_KEY_TENANT_BINDING_ENABLED` | key callers bound to tenant `plx` | on | n/a |
| `KNOWLEDGE_SEARCH_MIN_SIMILARITY` | search floor (default 0.30) | 0.30 | n/a |
| R4+ (planned) `BRAIN_WRITES_PAUSED` | all write routes and MCP writes give 503 | n/a | n/a |

---

## 4. Endpoints

Base today: `https://missioncontrol.tayloralton.com` (cloud env `VMC_API_URL`; stdio
MCP reads `VMC_BASE_URL`, default `http://localhost:3100`, `plx_secondbrain/src/config.ts:14`).
Envelope `{data, meta?}` on success and `{error:{code,message,details?}}` on failure
(`vmc/lib/vmc/api/api-handler.ts:366-380`). Auth: header `X-API-Key: <key>`, or
`Authorization: Bearer <key>` (`api/key-scope.ts:155-166`). A 401 carries the header
`X-VMC-Auth-Failure-Reason` (`api_key_not_configured`|`invalid_key`). Optional
attribution headers are `X-Agent-Name` (becomes `actor`) and `X-MCP-Session`.
`X-Tenant-Id` is ignored while tenant binding is on. Route budgets give 503 TIMEOUT
on overrun, and the first call after a restart can 503 (LIVE note §0.12).

### 4.1 Key-authed REST: the 11 routes that survive the move (B4)
| Method + path (`/api/vmc/knowledge/…`) | Inputs | Output (`data`) | R/W | Notes |
|---|---|---|---|---|
| GET `agent/search` | `q` (req), `source` all\|memory\|documents (all), `mode` (hybrid; graph modes graph/local/global/path/brief only if retrieval flag on), `limit` 1-50 (10) | `{query, source, mode, total, partial, memoryDegraded, documentsDegraded, results[{id, kind memory\|document, score (blended), rawScore, item, sourcePath, projectSlug}]}`; ids `memory:<uuid>`, `document:<catalog id>`, `doc-chunk:<path>`, `memory-graph:<factId>` | R | budget 5 s; **only route that calls swarm** (`/memory/search/v2`, 15 s, embeds the query; since `84966c0` TASK-2363 the swarm caps the embedding at 2.5 s and on a timeout or error silently runs keyword search, marking `semanticDegraded: true`, and an unknown `mode` answers 400, `src/api/fastapi_app.py:676-717@e228067`; a hit list is then lexical only, so an empty result is weaker evidence of absence); in-process cache; side effect: bumps `access_count` on hits; personal excluded (`agent/search/route.ts:13-40`, `knowledge/search-core.ts:496-686`) |
| GET `agent/items` | `q, source, memoryType, since` (ISO), `order` importance\|recent, `limit` 1-100 (25), `offset` | item list + total | R | **GET only. POST gives 405.** Portal `scripts/markitdown/ingest_to_brain.py` and local-inference `harness/ledger/brain.py` POST here (broken; R7 fixes them) |
| GET `agent/node/{id}` | `include=content` | node view (+ facts/backlinks) and content when a memory item | R | personal → 404 |
| GET `agent/document/{...id}` | catalog id | `{title, markdown, …}` | R | reads VMC host repo copies (`VMC_PLX_MC_ROOT`, `VMC_PLX_PORTAL_ROOT`, `VMC_SWARM_ROOT`) and SharePoint (B11) |
| GET `agent/subgraph` | `center` (req), `hops` 1-3, `relationTypes` (22 enum), `minConfidence` | bounded neighbourhood | R | |
| GET `agent/trail` | `from`, `to`, `maxHops` 1-4 (3) | trails | R | |
| GET `agent/status` | none | `{available, flags{MEMORY_GRAPH_INGEST_ENABLED, MEMORY_GRAPH_RETRIEVAL_ENABLED, VMC_SECOND_BRAIN_INGEST_ENABLED, VMC_SECOND_BRAIN_COMPILE_ENABLED}, lint{health,lastLintAt,staleCount,orphanCount,report}, writeback{…}}` (`knowledge/agent-status.ts:18-41`) | R | the cheapest liveness and flag probe; Portal `selfCheck` uses it |
| POST `ingest` | `title`≤300, `content`≤100k, `sourceType`, `domain?`, `department?`, `projectSlug?`, `tags?`≤20, `url?` | `{itemId, memoryKey, memoryType:"note", namespaceKey:"swarm/brain-ingest", graphIngestion}` | **W** | 30/min/tenant; no embedding |
| POST `relations` | `from`, `to` (item uuid/memory_key or `document:<path>`), `relationType` (22), `rationale`, `confidence`≤0.7, `proposedBy` | `{lane memory_links\|knowledge_links, linkId, …, classification:"inferred_link"}` | **W** | 30/min |
| POST `session-artifact` | SessionArtifact v1 (`schema_version:1, runtime cursor\|claude-code\|hermes\|swarm\|other, session_id, started_at, ended_at, repo, branch, title, summary, decisions[], lessons[], files_touched[], evidence[{kind,ref}], tags[], project_slug?, department?`) | `{itemId, memoryKey, memoryType, tags, graphIngestion}` | **W** | body ≤512 KB; secrets redacted; namespace `swarm/session-artifact/<runtime>` |
| GET/POST/DELETE `mcp` | Streamable HTTP JSON-RPC, stateless (`sessionIdGenerator: undefined`) | MCP | R+W | keyless browser GET returns an HTML help page |

There are also 9 session-only (browser login) routes, unusable by agents:
`knowledge` (root), `search`, `items`, `graph`, `node/[id]`, `trail`, `notes` (GET,
POST, PATCH), `compile` (POST), `insights/[id]` (PATCH). The 4
`/api/vmc/second-brain/*` routes are also login-only. All of these retire at R10 (B4).

### 4.2 MCP tools: 11, identical names on HTTP and stdio
HTTP is in-process (`vmc/lib/vmc/knowledge/mcp-http-server.ts`,
`mcp-http-tools.ts:31-41, 175-232`). Stdio proxies the REST routes
(`plx_secondbrain/src/tools.ts`). Writes are in-process on HTTP, not through REST (B21).
| Tool | Inputs | R/W | Behaviour |
|---|---|---|---|
| `brain_self_check` | none | R | auth, DB and flag probe (stdio also probes `/api/health`) |
| `brain_search` | `query`, `mode?`, `domain?`, `types?`≤10, `limit?`≤50, `since?` | R | `agent/search` core + post-filters; snippet budget |
| `brain_get_node` | `id`, `include?` ⊂ facts/backlinks/provenance/content | R | `content` only on request (token budget) |
| `brain_get_subgraph` | `center`, `hops?`1-3, `relationTypes?`, `minConfidence?` | R | |
| `brain_trail` | `from`, `to`, `maxHops?`1-4 | R | |
| `brain_query` | `question`, `domains?`≤8, `synthesize?` | R | search(10) + 1-hop expansion of the top hit; **no server synthesis** (returns a note) |
| `brain_find_gaps` | `domain?`, `kind?` | R | lint summary + 25 recent `insight` items |
| `brain_timeline` | `domain?`, `since?`, `limit?`≤50 | R | recent items, personal excluded |
| `brain_run_lint` | `scope?` | R | **does not run lint**; returns the operator curl with `CRON_SECRET` (`mcp-http-tools.ts:49-50`) |
| `brain_ingest` | as POST `ingest` | **W** | |
| `brain_propose_relation` | `from`, `to`, `relationType`, `rationale`, `confidence?`≤0.7 | **W** | |

Stdio-only resources: `brain://index`, `brain://node/{id}`, `brain://wiki/{slug}`,
`brain://search?q=`, `brain://insights` (`plx_secondbrain/src/resources.ts`). Stdio
transport: timeout `BRAIN_MCP_TIMEOUT_MS` (8000), retries `BRAIN_MCP_RETRY_COUNT` (2) on
429/502/503/504 with jitter, and headers `X-MCP-Session: plx-brain`,
`X-Agent-Name: plx-brain` (`src/http.ts:27, 96-98`).

### 4.3 MC's door to the brain (PLX_MC `1fba083`)
`mc_search_knowledge {q, limit 1-25 (8)}` is served at
`https://mc.plxcustomer.io/api/cursor/mcp` (not `/mcp`;
`PLX_MC/docs/modules/mcp/README.md:22`). It calls brain `agent/search?q=&limit=` with
MC's own key and returns hits with `id, source, namespace, score` and
`status ok|not_configured|upstream_unreachable|upstream_error`
(`PLX_MC/src/lib/mcp/read-actions.ts:156-166, 214-221`;
`src/lib/brain-ask/client.ts:62-81`). It needs an MC principal with `task.read`. Ask the
Brain article open uses `agent/document/{id}` or `agent/node/{id}?include=content`
(`brain-ask/client.ts:99-101`). MC's REST twin is `GET /api/cursor/knowledge/search` (B1).

### 4.4 The Workbench's current client
`integrations/vmc.py` (`VmcAgentReadClient`) GETs `/api/vmc/cursor/context` (retires;
§0.2) and `agent/search` (source all/memory/documents, mode hybrid, limit ≤25). Both
are behind explicit "broad read authorized" opt-ins. It is wired by
`operating/mcp_server.py:2969-2974, 3143-3152` (`--vmc-live-read`, `--vmc-base-url`,
`--vmc-agent-name plx-agent-workbench`, `--vmc-recipient-principal`, env `VMC_API_KEY`)
and exposed as tools `plx.vmc_context` and `plx.vmc_knowledge_search`
(`mcp_server.py:78-81, 1391-1398`; `operating/unified.py:5950-5967`). Gaps: no retry; no
node/document/subgraph/trail/status reads; the base URL defaults to the old host in
code; it does not read the cloud env name `VMC_API_URL`.

---

## 5. local-inference and skills: use them or not

**local-inference** provides an OpenAI-compatible LiteLLM proxy. Agents name an alias,
never a model id.
- Dell `:4000`, tailnet `100.103.33.54`. Aliases `local-primary`/`local-fast`
  (Qwen3-32B-AWQ, tools + JSON), `local-coder` (Spark A), `local-driver` (Spark B)
  (`litellm/config.yaml:1-24`). Master key `LOCAL_LITELLM_MASTER_KEY`, local only. This
  is the **trading gateway**. Fleet D23 forbids fleet work from loading it or the
  `local-driver` lane. The brain's own extraction and wiki compile still use
  `local-driver` today (`knowledge_extract.py` `strict_local_chat`; GOVERNANCE
  "Token-Cost Policy"), and R9 moves brain chat jobs to the fleet proxy's cloud aliases.
- Fleet proxy `:4001` on its own EC2 (`litellm/fleet.yaml`, `deploy/fleet-proxy/README.md`):
  cloud aliases only (`cloud-claude-sonnet`, `cloud-claude-haiku`, `cloud-gpt`,
  `cloud-gpt-mini`, `cloud-gemini-*`, `cloud-grok*`, `cloud-mistral-*`, DeepSeek,
  OpenRouter). Per-agent virtual keys; fail-closed; spend logs only, no prompt text.
  P1b adds `local-primary`/`local-fast`. Config merged; host deployment and keys are
  not evidenced.
- The SharePoint organizer harness (`.orchestrator/sharepoint-doc-org-harness/`)
  covers VincePersonal only and performs writes (rename, stamp, harvest upload) with a
  SQLite reverse journal. Its `harness/extract/pipeline.py:14-40` runs Docling if
  installed, then an OCRmyPDF **stub**, then a filename stub. `harness/mail/*` is
  delegated Graph (MSAL cache on the Dell). `harness/ledger/brain.py:45-51` POSTs
  `agent/items` (405).

**Verdict for the Workbench.** Don't call the Dell gateway or Spark lanes: they are a
trading lane, tailnet-only, and the cloud cannot reach them. Don't reuse the harness:
it writes, covers the wrong site, and its OCR is a stub. You may borrow its ideas
(reverse journal, provenance query) for read-only work. For model calls outside the
agent session, the sanctioned path is a fleet proxy virtual key under fleet D11/D21.
Nobody has issued us one; ask the operator first. For OCR use only
offline Mac Vision OCR evidence, MarkItDown for Office/PDF text, and no Azure Document
Intelligence. local-inference adds no production OCR.

**skills** provides `manifest.json` entries (`id, name, description, status
draft|pending_review|published|deprecated|retired, tier company|personal_submission,
contentPath, runtimes cursor|claude|codex, tags, owner, sourceProvenance`;
`schemas/manifest.schema.json`). Packs are distributed by
`scripts/distribute-to-repos.sh` into consumer repos' `.cursor/skills/` and synced
locally by `scripts/sync-skills.sh` into `~/.cursor/skills` and `~/.claude/skills`.
Relevant skills:
- `session-brain`: writes a SessionArtifact to VMC, falls back to an offline queue.
  This is a production write; don't use it here.
- `mc-sync`: the MC checkout/progress/complete loop. Use it when governed upstream work
  is approved.
- `vmc-sync`: VMC todos. It retires; don't use it.
- `plx-graph-mail`: sends mail; forbidden here.
- `plx-portal-agent-access`: staging/UAT staff login; not our scope.

**Verdict for the Workbench.** Skills are a packaging format for agent instructions,
not a runtime. If the Workbench later publishes "how to query the Workbench" for other
PLX agents, ship it as a `skills/<id>/SKILL.md` + manifest entry (`runtimes:["claude",
"cursor"]`, `tier: company`) through a governed PR. Inside this repo keep
`.claude/agents/*` and the verify docs; no skills dependency.

---

## 6. Retirement plan (PLX_MC `docs/specs/swarm-retirement-spec.md`, r13, D1-D13 confirmed 2026-09-28)

Order (§12): R1 (stops) → R0 (usage audit) → lift **R3 → R4 → R5 → R6 → R6a → R7** →
simplify **R8 → R9 → R10 → R11 → R12** (starts after R6). R5 and R8 wait on fleet P1
(the `tag:fleet-proxy` tailnet tag and the fleet's OpenAI organisation). R9 waits on
fleet P2 (the brain's fleet key). **The spec gives no calendar dates**, only order and
gates. Nothing stops until its logs show 14 quiet days (D1, SC-3).

| Phase | What | State 2026-09-30 | Workbench impact |
|---|---|---|---|
| R1 | pause governance-repo-sync, lessons-rule-promote, quick tunnel, 2 portal scripts | swarm PR merged 2026-09-28 (`84dd6f106`); operator steps unknown (spec still says "PR is open") | none |
| R0 | read-only usage audit (Caddy/VMC logs, callers by `X-Agent-Name`) | next | our calls show up as `X-Agent-Name: plx-agent-workbench`. Keep that header stable so we count as a caller to move. |
| R2 | portal client fixed | shipped (portal #1589) | none |
| R3 | record the HTTP contract (`plx_secondbrain/eval/contract/`) | code merged; since `b7b9584` TASK-2725 (2026-10-16) one admin-tier (`VMC_API_KEY`) recording, run `r32026100621239a02`, 94/94 matched with complete coverage; the scoped tier (`VMC_SCOPED_API_KEYS`) is not run, so R3 stays incomplete (`eval/contract/recordings/README.md@b7b9584`); the operator runner `eval/contract/r3-run.sh` selects the tier (`--tier`, `--key-env`; `eval/contract/README.md:154-191@b7b9584`) | the request set (`eval/contract/requests.ts`) is the best contract spec for our client tests |
| R4 | `plx_secondbrain/service/`: the 11 routes copied unchanged; accepts `sha256(key)==VMC_API_KEY_SHA256` or `BRAIN_API_KEYS`; `BRAIN_WRITES_PAUSED`; logs key tier | not started | none if we stay on the 7 GET routes |
| R5 | run the service on the brain host (own RDS role limited to schema `memory`) | not started | none |
| R6 | Caddy on the VMC host proxies the 11 paths to the brain host; the block stays for good | not started | MCP sessions drop at cutover; reconnect. Our stateless GETs are unaffected. |
| R6a | DNS `brain.plxcustomer.io` | not started | |
| R7 | callers read `BRAIN_BASE_URL` (default `https://brain.plxcustomer.io`) **paired with** `BRAIN_API_KEY` (`brn_…`); without a brain key they call the old host with `VMC_API_KEY`; never send `VMC_API_KEY` to the brain's own name | not started | **Implement this pairing rule now** (§7.1). Ask the operator for a `BRAIN_API_KEY`. |
| R8 | search ported in-process (same `text-embedding-3-small` 1536) | not started | scores may shift ≤0.02 |
| R9 | keep or retire each of the 15 jobs; build the embedding backfill; chat jobs → fleet proxy | not started | swarm-only content (dispatch summaries, swarm lessons) stops growing |
| R10 | the brain's own database (D13); the login routes and the VMC UI retire | not started | writes pause for the window (`BRAIN_WRITES_PAUSED`) |
| R11 | key scopes wired; `brn_` keys; VMC's Caddy forwards only `brn_*`; brain drops the legacy hashes after 14 quiet days | not started | **`VMC_API_KEY` stops working for brain routes on both hosts** |
| R12 | remove the search fallback marked "remove after 2026-09-30" (`search-core.ts:370-375`, B15) | not started | document-lane lexical fallback disappears |

Env names (§7 execution contract): `BRAIN_BASE_URL` (callers; the frontier spec's
`BRAIN_URL` is the same value), `BRAIN_API_KEY` (one caller's key), `BRAIN_API_KEYS`
(the service's accepted list; never a caller variable), `BRAIN_DATABASE_URL`,
`BRAIN_RUNS_DATABASE_URL`, `BRAIN_CRON_SECRET`, `BRAIN_SWARM_SEARCH_TOKEN`, and the
secret `prod/plx-brain` → `/etc/plx-brain/brain.env`. SC-8: no step changes
`VMC_BASE_URL`/`VMC_API_KEY`/`SWARM_API_KEY` or `prod/ec2-secrets`. The Cursor Team MCP
entry is left to frontier P14 (r13).

**Integrations must target:** brain paths `/api/vmc/knowledge/agent/*` (path stable),
host and key from config as a pair, and MC (`mc_search_knowledge`, `mc_get_context`)
for task context. **Do not target:** `/api/vmc/cursor/*`, `/api/vmc/second-brain/*`,
login routes, swarm `/memory/*`, `/pipelines/run`, VMC crons, swarm dispatch, Lobster,
`vmc-sync`, the Dell gateway, or the `localhost:3100` default.

---

## 7. Recommendations for the Workbench

### 7.1 Reading from the brain
1. **Only these reads, GET only:** `agent/status` (probe and flags), `agent/search`,
   `agent/node/{id}?include=content`, `agent/document/{id}`, `agent/subgraph`,
   `agent/trail`, and optionally `agent/items` (order=recent, since). Enforce the
   allow-list in `integrations/vmc.py` with a path + method allow-list test, so a
   trading or POST path cannot be reached with the key (§0.3).
2. **Use the pair rule from R7 now.** If `BRAIN_API_KEY` is set, use `BRAIN_BASE_URL`
   (default `https://brain.plxcustomer.io`). Otherwise use `VMC_API_URL`
   (`https://missioncontrol.tayloralton.com`) with `VMC_API_KEY`. Never send
   `VMC_API_KEY` to `brain.plxcustomer.io`. Keep `X-Agent-Name: plx-agent-workbench`.
3. **Retry** 429/502/503/504 at most 2 times with jittered backoff and an 8 s timeout,
   and report `upstream_unreachable` versus `upstream_error` the way MC does.
4. **Drop `vmc_context`** (`/api/vmc/cursor/context`), or mark it retiring, and use MC
   for tasks.
5. **Store brain hits as leads, not facts.** Record `id` (`memory:<uuid>`, etc.),
   `sourcePath`, `score`/`rawScore`, `item.updatedAt`, the query and the read time, as
   a citation of kind `derived_knowledge`/T3. They may inform a case `interpretation`
   (`cases.py:50`). They must never become a `cited_fact` about a business record,
   never assert an `Observation` in `ontology/core.py`, and never feed a transaction
   candidate. The Portal does the same: its knowledge facade treats the brain as
   T3/untrusted.
6. **Trim before showing.** Brain content has no per-employee ACL. Before ranking,
   caching or display, intersect with the reader's Workbench entitlement: who may see
   engineering/agent knowledge at all, plus department. Do not cache brain results
   across actors.
7. Good uses: engineering and governance context for agents (how a Portal module
   works, past decisions, runbooks, session lessons) and SOP/document pointers from the
   repo catalog. Not uses: customer, order or supplier facts. Those come from the
   Portal reporting API, Graph and BC under our own reads.

### 7.2 `mc_search_knowledge` or brain search
- **An agent inside a case asking "what do we know about X"** uses
  `mc_search_knowledge` (MC principal, audited, insulated from R6/R7/R11 key changes,
  read-only by contract). It is search only, `limit ≤25`, hybrid mode.
- **The Workbench host needing node content, a document body, a subgraph, a trail or
  the status flags** uses direct brain GETs with our own key (§7.1), because MC exposes
  no MCP tool for those.
- **Never both for one question.** Never `brain_query` with `synthesize:true` (it does
  nothing). Never the brain MCP write tools.

### 7.3 Writing derived knowledge back: not now
- Workbench rules forbid production writes. Brain writes also fall short of our bar:
  ingested text becomes a 0.9 dispatch-eligible `deterministic_link` fact; dept scopes
  are unenforced; there is no per-employee ACL; and proposed relations lose their
  `inferred_link` label in the graph (§2.11).
- Ingesting business transactions would also put customer and commercial data under
  `VMC_API_KEY`-wide read and inside the retiring `command_center` DB.
- If the operator or Vince later approve write-back, use this gate:
  1. engineering learnings only (no business records), as SessionArtifact v1 to
     `session-artifact`, or `ingest` with a narrow `projectSlug` and `department`;
  2. only through an approved MC task + checkout;
  3. only to the brain's own host with a `brn_` key after R7;
  4. never during an R10 write pause;
  5. with a local outbox and replay, because B19 shows hooks lose records;
  6. with redaction, since only the session-artifact route redacts secrets;
  7. relations proposed only with a rationale and confidence ≤0.7, after the §2.11
     classification mirroring is fixed upstream.
- Business assertions belong to the Workbench, shaped for TASK-477 (valid and recorded
  time, confidence, evidence span, responsible identity). If an upstream ledger exists
  later, export them there, not to the brain.

### 7.4 Align, don't merge
Keep `ontology/` vocabularies separate. If an export is ever built, map brain
classification ↔ our evidence kind, reuse the episode field names (§2.12), and keep
`classification` meaning sensitivity on our side.

---

## 8. Corrections to earlier architecture notes (sections 0-2 on these repos)

| Earlier text | Correction |
|---|---|
| §0 table: PLX_MC `7f797126…` "identical to pin"; §2 "Open #264 (docs only)" | PLX_MC main is now `1fba083` (2026-09-30). #264 merged (`be0c741`), so retirement **r13** is current. #265 (agent-report reader, task-create idempotency), #266 and #267 (merge-queue gate) followed. |
| §0.1(5) "brain callers move to `brain.plxcustomer.io` with new `BRAIN_API_KEYS`" | Callers get a `BRAIN_API_KEY` (`brn_…`) paired with `BRAIN_BASE_URL`. `BRAIN_API_KEYS` is the service's accepted list. Callers without a brain key keep calling the **old host** with `VMC_API_KEY` until R11, and must never send it to the new name (R7). |
| §1.2 "migrations `{021,029,048,052,053,068}`" | The full set is `004, 021, 022, 029, 030, 032, 042, 047, 048, 052, 053, 068` (B9). `022` adds the `chk_agent_namespace` CHECK and the `items_effective` view; `032` adds `attempt_packets`; `047` adds the quantized tables. |
| §1.2 "Time semantics … (INFERENCE…)" | FACT: migration 052 columns plus `relation-ontology.md` "Validity Windows" (append-only, `includeInvalidated`). |
| §1.2 identity: only `fact_key=<source_kind>:<pk>:<sha256>` | LLM facts use `memory_item:<item_id>:<relation_hash>`. Ingest `memory_key` is `brain-<hex32>`. The deterministic graph has only `memory_item` entities. |
| §1.2 ACL: dept keys unenforced | Add: the **legacy `VMC_API_KEY` itself is trading-critical**. It passes every VMC `auth:"key"` route, and the paper dispatchers depend on it (F4). |
| §1.4 ingestion list | Add `knowledge-extract` (route and registry only, no crontab line), `rollup-distill`, `wiki-compile`, `memory-summarize`, `kb-decisions-sync`, `lessons-auto-extract`, Lobster `kb-compile`/`kb-lint`/`institutional-distill`, research-run lessons (B20) and swarm dispatch/`memory_store` writes (F23). `second-brain-lint` runs from the crontab (registry-enabled again since `06b5b44b2` TASK-2838). |
| §1.4 "Contract bug (INFERENCE …)" POST `agent/items` | FACT: B4 "`agent/items` exports GET only", and R7 names both broken callers. |
| §1.5 tools: brain_trail 1-3? / resources | Trail `maxHops` is 1-**4** (`TRAIL_MAX_HOPS=4`) and subgraph 1-3. The 5 `brain://` resources are **stdio only**; the HTTP MCP registers none. HTTP MCP accepts `Authorization: Bearer` as well as `X-API-Key`. |
| §1.5 "9 other routes are browser-session only" | Plus 4 login-only `/api/vmc/second-brain/*` routes (B4). |
| (missing) | Classification leak: proposed `inferred_link` relations are mirrored as `deterministic_link` facts, and every item becomes a `documents` fact (§2.11, INFERENCE from `ingest.ts`). |
| (missing) | `/api/vmc/cursor/context`, which the Workbench calls, is not a brain route and retires with VMC. |
| §4.2(2) brain endpoints "usable today (none called by me)" | LIVE (2026-10-01): `agent/search` returns 200 with the cloud `VMC_API_KEY`; cold-start 503 is possible. |

---

## 9. Verify before relying
- The real flag state: call `agent/status` once with the key and record `data.flags`
  (four flags only). Do not log the key.
- Which key `VMC_API_KEY` is (legacy or dept) cannot be told from the client side, and
  both are trading-capable on VMC.
- §2.11 items 2-3 (inferred → deterministic mirroring; extension relations quarantined)
  were read from code, not observed in data.
- R0 results, actual R-phase dates, fleet proxy deployment, and whether the operator
  will issue the Workbench a `brn_` key and a fleet virtual key.
- `doc-chunk:` vs `document:` id shapes in live search results (fixture labels differ
  from live ids; `plx_secondbrain/eval/contract/README.md` "Search ids").
