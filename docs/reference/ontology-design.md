# Ontology design for the PLX Agent Workbench

Status: design decision memo, 2026-10-01.
Repo state read: `origin/main` `bb48071`; live results (main `4972de3`).
Nothing here changes the hard rule: **never write to Petra production**. Every action below ends
in a proposal, a human-approved owner-system change, and a Workbench read-back.

## Decisions at a glance

1. Add the missing **semantic layer**: one declarative schema (`ontology/schema/plx.ontology.json`)
   of object, link, event, action and monitor types, bound to the sources we already capture.
2. The ontology is a **disposable, citation-only projection** of L1 captures. Every value is a
   citation. It is not a second copy and not a second Portal reader.
3. **Identity is a separate, reversible, explainable layer** (deterministic keys → normalized
   blocking → Fellegi–Sunter scoring), with tiers. Anchors never auto-merge.
4. **Bitemporal**: valid time + recorded time on every value; `as_of` and `as_known_at` on every read.
5. **An object-centric event log (OCEL 2.0)** across Portal/FM/Sage/BC/mail/SharePoint, with
   declarative process conformance (three-way match, missing stages, peer variants).
6. **Typed action catalog** with Workbench-evaluated submission criteria, an MC approval
   reference, human execution in the owner system and read-back. Only ontology edits (identity
   reviews, corrections) execute inside the Workbench.
7. **Markings propagate** to anything derived, including model-written findings.
8. **Generic object tools** (`find/object/around/aggregate/timeline/process/compare/identity/documents`)
   with object-set handles replace per-case scripts. Scripts become verified procedures and
   benchmark fixtures.
9. **The acceptance test is a nightly benchmark** over random real transactions, not scripted
   demos: ≥85% fully correct, 0 unsupported numbers, 0 leaks by week 6 (today: 0/2).

### Decided since

Records in `docs/decisions/`; each refines this file where it speaks.

- 2026-10-01 `tools-shape`: C.8 names, one grammar and one resolver under `ontology/`.
- 2026-10-02 `capture-persistence`: L1 completeness is measured over the relations the
  domains bind (bound-first sync); relations whose ids change between reads (ETL
  re-inserts) leave the denominator with a `capture_unstable_ids` gap. Durable L1 is
  the capture stores' state dir on a persistent credentialed host, never Git (business rule).
- 2026-10-02 `bc-capture-incremental`: BC captures read `lastModifiedDateTime ge` per set
  (headers, parties, items, item and G/L ledger entries), re-read lines unless the header
  is an unchanged posted invoice/credit memo, and run a weekly key-only full pass for
  deletions (GET-only rules out webhooks).
- 2026-10-02 `store-value-conflicts`: per-property conflicts are not stored; the link's
  `property_conflict` features index them, and `plx.compare`/`plx.identity` compare the
  values at read time after the reader's marking filter.
- 2026-10-02 `stored-key-compare`: stored-key pairs compare `integer_id`/`text_id`/
  `identifier`; `text` only on `text_match` bindings.
- 2026-10-02 `oracle-pinning`: the eval runner pins the agent to mirror generations
  derived from the oracle's read time and scores drifted rows `pin_drift`.
- 2026-10-02 `mail-and-mentions-store`: one versioned mail capture (L3) behind the
  capture-store registry; a `documents` domain (Email, Attachment, SharePointFile) makes
  mentions stored `lead` links in the projection now (C.1, C.4).
- 2026-10-02 `mirror-writer-takeover`: the mirror's owner compare-and-swap is the fence;
  no lease.

## A. How the best systems work: the mechanisms that matter

What separates an enterprise intelligence system from "a database with a chat box" is a set
of concrete mechanisms, not the model. Each item below names the mechanism, the source, and
what PLX takes from it.

### A.1 Palantir Foundry Ontology and AIP

- **Object types bound to datasources; the ontology models the world, not the tables.**
  - An object type is "the schema definition of a real-world entity or event", with properties
    mapped to columns ([core concepts](https://www.palantir.com/docs/foundry/ontology/core-concepts),
    [object types](https://www.palantir.com/docs/foundry/object-link-types/object-types-overview/)).
  - Multi-datasource objects join several sources on one primary key, with each property owned
    by exactly one datasource. A user who cannot see a source gets `null` for its properties
    ([MDO](https://www.palantir.com/docs/foundry/object-permissioning/multi-datasource-objects/)).
  - Value types attach business constraints, and violating data fails indexing
    ([value types](https://www.palantir.com/docs/foundry/object-link-types/use-value-type)).
  - Interfaces give polymorphism across object types, including link constraints
    ([interfaces](https://www.palantir.com/docs/foundry/interfaces/interface-overview/)).
  - *PLX takes:* per-source property ownership inside one entity, availability declared per
    type, and an `Item`/`Party`/`Document` interface.
- **Link types** are bidirectional, named on both sides, with 1:1, 1:N and N:M cardinality.
  N:M links are backed by a join datasource, and several link types may join the same pair of
  types ([link types](https://www.palantir.com/docs/foundry/object-link-types/link-types-overview/)).
- **Object sets and search-around** are the agent's traversal primitive.
  - The Object Set Service serves filter, aggregate and `searchAround` over link types
    ([object sets](https://www.palantir.com/docs/foundry/functions/api-object-sets/)).
  - Temporary set handles pass between applications
    ([temporary object set](https://www.palantir.com/docs/foundry/api/v2/ontologies-v2-resources/ontology-object-sets/create-temporary-object-set)).
  - The OSDK generates typed bindings scoped to the user's permissions
    ([OSDK](https://www.palantir.com/docs/foundry/ontology-sdk/overview/)).
- **Datasets to objects through a changelog → merge → index pipeline.**
  - User edits are layered over source data and merged by primary key, with a per-datasource
    conflict rule ("apply user edits" or "most recent")
    ([Funnel](https://www.palantir.com/docs/foundry/object-indexing/funnel-batch-pipelines),
    [edits](https://www.palantir.com/docs/foundry/object-edits/how-edits-applied)).
  - *PLX takes:* ontology edits (identity reviews, corrections) layered over immutable
    captures, never written back.
- **Actions are the only governed write path.**
  - An action type is parameters + rules + server-side submission criteria (user, groups,
    parameter values, "in a scenario") + side effects
    ([actions](https://www.palantir.com/docs/foundry/action-types/overview/),
    [criteria](https://www.palantir.com/docs/foundry/action-types/submission-criteria/)).
  - A writeback webhook runs *before* the ontology edit, and a failed webhook aborts the whole
    action ([webhooks](https://www.palantir.com/docs/foundry/action-types/webhooks/)).
  - An action log object records who, what, when, parameters and edited keys
    ([action log](https://www.palantir.com/docs/foundry/action-types/action-log/)).
  - Agents' edits can be *staged proposals* that a reviewer accepts with the decision log
    visible ([Logic + Automate](https://www.palantir.com/docs/foundry/logic/aip-logic-integration-automate/)).
- **Security is one engine for people, apps and agents, and only markings propagate.**
  - Object and property policies give row, column and cell security
    ([policies](https://www.palantir.com/docs/foundry/object-permissioning/object-security-policies)).
  - Markings are conjunctive and propagate along lineage
    ([markings](https://www.palantir.com/docs/foundry/security/markings/)).
  - Read-time filters do *not* survive into model context: a tool result "can be summarized,
    transformed, and returned through any of the model's output paths with no remaining
    association to the source policy"
    ([propagation](https://www.palantir.com/docs/foundry/security/access-control-propagation/)).
  - *PLX takes:* markings on derived values *and* on model-written findings.
- **AIP grounds models in ontology tools, not SQL.**
  - Chatbot (formerly Agent) Studio tools are object query (filter, aggregate, traverse
    links), action (auto-run or confirm-first), function, and clarification.
  - Retrieval context can be ontology, document or function-backed, with citations
    ([tools](https://www.palantir.com/docs/foundry/chatbot-studio/tools),
    [retrieval](https://www.palantir.com/docs/foundry/chatbot-studio/retrieval-context)).
  - AIP Logic composes deterministic blocks with LLM blocks
    ([Logic blocks](https://www.palantir.com/docs/foundry/logic/blocks/)).
  - AIP Evals runs suites of test cases and evaluators across models
    ([Evals](https://www.palantir.com/docs/foundry/aip-evals/overview/)).
  - Automate fires on object-set conditions
    ([Automate](https://www.palantir.com/docs/foundry/automate/overview/)).
  - Scenarios fork ontology data for what-ifs and merge atomically
    ([scenarios](https://www.palantir.com/docs/foundry/ontology/merge-scenario)).
  - Lineage spans data and workflows
    ([workflow lineage](https://www.palantir.com/docs/foundry/workflow-lineage/overview)).
- **Entity resolution is not a Foundry product.**
  - Foundry's guidance is fuzzy joins and KNN joins in pipelines
    ([forum](https://community.palantir.com/t/entity-resolution-fuzzy-matching/2160),
    [KNN join](https://www.palantir.com/docs/foundry/pb-functions-transform/complexKnnJoinV1)).
  - Gotham has reversible resolution: canonical key, winner key, constituents un-resolvable
    ([Gotham resolution](https://www.palantir.com/docs/gotham/api/revdb-resources/resolution/resolution-basics)).
  - *PLX takes:* ER as its own reversible layer, since our sources disagree on identity by
    design (FM ids, Portal cuids, BC numbers, renumbering).

### A.2 Knowledge graphs and LLMs

- **GraphRAG's split.** It extracts entities, relations and claims from text with an LLM,
  clusters them with Leiden, and pre-summarizes communities for "global" questions. Local
  search fans out from matched entities. DRIFT mixes both, and LazyGraphRAG defers the LLM to
  query time at about 0.1% of the indexing cost
  ([GraphRAG](https://microsoft.github.io/graphrag/), [paper](https://arxiv.org/abs/2404.16130),
  [DRIFT](https://microsoft.github.io/graphrag/query/drift_search/),
  [LazyGraphRAG](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/)).
- **Where it works and where it doesn't.** It is good for sensemaking over prose and weak for
  sums, counts and as-of balances; a benchmark found GraphRAG often underperforms vanilla RAG
  ([arXiv 2506.05690](https://arxiv.org/abs/2506.05690)). A curated semantic layer, by contrast,
  raised enterprise text-to-SQL accuracy from 16% to 54%
  ([arXiv 2311.07509](https://arxiv.org/abs/2311.07509)).
- **Neo4j's guidance.** Use Text2Cypher only for simple queries, with deterministic
  validation, and give frequent complex questions dedicated tools
  ([guide](https://neo4j.com/blog/genai/text2cypher-guide/),
  [retrievers](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html)).
- *PLX takes:* the graph comes from **declared bindings**, not LLM extraction. LLM extraction
  produces only *mentions* in documents. Arithmetic goes to deterministic tools.

### A.3 Entity resolution

- **Fellegi–Sunter** (as implemented in Splink).
  - Match weight = prior + Σ log₂(m/u). m is estimated by EM per blocking rule and u by random
    sampling ([theory](https://moj-analytical-services.github.io/splink/topic_guides/theory/fellegi_sunter.html),
    [training](https://moj-analytical-services.github.io/splink/demos/tutorials/04_Estimating_model_parameters.html)).
  - Many strict blocking rules beat a few loose ones
    ([blocking](https://moj-analytical-services.github.io/splink/topic_guides/blocking/blocking_rules.html)).
  - A per-pair waterfall explains each decision
    ([waterfall](https://moj-analytical-services.github.io/splink/charts/waterfall_chart.html)).
- **Senzing** (vendor-stated).
  - Entity-centric, principle-based resolution that re-resolves and *un-resolves* in real time.
  - Explanations: **why**, **why not** and **how**
    ([explainability](https://senzing.com/explainability/),
    [how it works](https://senzing.com/how-entity-resolution-works-with-senzing/)).
- **Zingg**: active learning on the most uncertain pairs, plus learned blocking
  ([Zingg](https://github.com/zinggAI/zingg)).
- **Survivorship** is per attribute with the source kept on every value
  ([survivorship](https://dataladder.com/guide-to-data-survivorship-how-to-build-the-golden-record/)).
- *PLX takes:* immutable source records, weighted and explained edges, clusters as a derived
  layer, a review queue for uncertain pairs, and per-property precedence without overwriting.

### A.4 Semantic layers, enterprise search and agent platforms

- **Join safety in semantic layers.** dbt MetricFlow infers joins from entity types, rejects
  fan-out joins and limits multi-hop to 2
  ([join logic](https://docs.getdbt.com/docs/build/join-logic)).
- **Verified questions as assets.**
  - Snowflake semantic views declare relationships, facts, dimensions and metrics, and keep
    `verified_queries` with `verified_by`/`verified_at`
    ([semantic views](https://docs.snowflake.com/en/user-guide/views-semantic/overview)).
  - Databricks metric views and Genie *trusted assets* label answers built from verified SQL
    ([metric views](https://docs.databricks.com/aws/en/metric-views/),
    [trusted assets](https://learn.microsoft.com/en-us/azure/databricks/genie/trusted-assets)).
- **Microsoft Fabric IQ.**
  - The ontology binds entity types and relationships to OneLake tables without copying
    ([Fabric IQ](https://learn.microsoft.com/en-us/fabric/iq/overview),
    [binding](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-bind-data)).
  - Data agents run as the user (RLS/CLS apply), are read-only, and route each question to
    NL2SQL, DAX, KQL or ontology ([data agent](https://learn.microsoft.com/en-us/fabric/data-science/concept-data-agent)).
- **Glean.** Permissions are captured at crawl time and checked at query time. A people,
  content and activity graph drives ranking
  ([knowledge graph](https://docs.glean.com/security/knowledge-graph),
  [context graph](https://www.glean.com/blog/how-do-you-build-a-context-graph)).
- *PLX takes:* fan-out refusal in `plx.aggregate`, verified procedures with labelled answers,
  run-as-user enforcement before ranking, and bindings with no copy (citations).

### A.5 Time, events and process

- **Bitemporal facts.**
  - Fowler separates actual time from record time ("what did we believe on R about A")
    ([bitemporal history](https://martinfowler.com/articles/bitemporal-history.html)).
  - XTDB v2 keeps `_valid_from/_valid_to` and `_system_from/_system_to` on every row
    ([XTDB time](https://docs.xtdb.com/about/time-in-xtdb.html)).
  - SQL:2011 standardizes application-time and system-versioned periods
    ([SQL:2011](https://en.wikipedia.org/wiki/SQL:2011)).
- **Event sourcing.** Append-only intent events, compensating corrections, idempotent
  projections, and replays that never re-trigger external effects
  ([pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)).
- **Object-centric process mining.**
  - Flattening many-to-many processes onto one case id causes *deficiency*, *convergence* and
    *divergence* errors. P2P (PO → receipt → invoice → payment) is the canonical victim
    ([van der Aalst 2019](https://link.springer.com/chapter/10.1007/978-3-030-30446-1_1)).
  - OCEL 2.0 fixes this with typed events and objects, E2O and O2O relations with
    *qualifiers*, and time-varying object attributes. It has a SQLite layout
    ([OCEL 2.0](https://www.ocel-standard.org/), [spec](https://arxiv.org/html/2403.01975v1)).
  - Celonis's Process Intelligence Graph is built on this object-centric model and is exposed
    to agents through an API and MCP
    ([PI Graph](https://www.celonis.com/blog/celonis-object-centric-data-model-single-source-of-truth-for-process-intelligence),
    [changelog](https://developer.celonis.com/changelog/)).
- *PLX takes:* an OCEL-shaped log with `time_basis`, declarative stage graphs, three-way match
  and peer variants.

### A.6 Agents, tools, evaluation and provenance

- **Grounding.** ReAct interleaves reasoning with grounded tool calls and reduces
  hallucination ([ReAct](https://arxiv.org/abs/2210.03629)).
- **Tool design** ([writing tools](https://www.anthropic.com/engineering/writing-tools-for-agents),
  [building agents](https://www.anthropic.com/engineering/building-effective-agents)):
  - consolidate workflows into fewer, higher-level tools;
  - namespace them;
  - return readable names with optional ids;
  - paginate and truncate;
  - write instructive errors;
  - evaluate on realistic multi-call tasks;
  - invest in the agent-computer interface.
- **MCP** adds `outputSchema`, `structuredContent`, an `isError` channel and audit logging
  ([MCP tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)).
- **Evaluation.**
  - τ-bench judges by final state and reports **pass^k** reliability
    ([τ-bench](https://arxiv.org/abs/2406.12045)).
  - ALCE measures citation recall and precision ([ALCE](https://arxiv.org/abs/2305.14627)).
  - RAGAS faithfulness ([RAGAS](https://arxiv.org/abs/2309.15217)).
  - LLM judges have position and verbosity bias ([judges](https://arxiv.org/abs/2306.05685)).
  - Enterprise SQL agents still score about 21% on Spider 2.0
    ([Spider 2.0](https://arxiv.org/html/2411.07763v2)), which is why we give the model
    objects, not tables.
- **Provenance.** W3C PROV: Entity, Activity, Agent; `wasDerivedFrom`, `wasGeneratedBy`,
  `used`, `wasAttributedTo`; qualified relations with role and time; bundles for provenance of
  provenance ([PROV-DM](https://www.w3.org/TR/prov-dm/), [PROV-O](https://www.w3.org/TR/prov-o/)).

### A.7 What actually makes these systems "intelligent"

None of it is the chat box. These are the twelve mechanisms, each something you can test.

1. **A semantic contract between the model and storage.** Typed objects and links with
   meanings, units and roles. The model never invents a join.
2. **Traversal as a primitive.** Search-around over declared links with set handles, so a
   five-hop question is one or two calls.
3. **Identity resolution as a first-class, explainable, reversible layer.**
4. **Two times on every fact**, so audit replays and "late" are computable.
5. **Object-centric events and process conformance**, so "unusual" and "missing" are detected,
   not guessed.
6. **Deterministic computation for numbers.** The graph supplies structure; arithmetic is
   exact, unit- and role-aware, and fan-out-safe.
7. **Governed actions.** Typed parameters, server-checked criteria, approval, logged execution
   and read-back.
8. **One permission engine for people and agents**, with mandatory markings that propagate
   into model output.
9. **Verified procedures and labelled answers** (trusted vs free investigation).
10. **Monitors over object sets** that open work proactively.
11. **Provenance on every claim**, checked rather than asserted.
12. **Continuous evaluation** on unfamiliar real data with reliability metrics.

The Workbench already leads on #6 (Decimal, unit and quantity-role algebra), #11 (citations
re-resolved per reader, value claims checked) and part of #8. It lacks #1–#5, #9, #10 and #12.
That is the whole problem.

## B. Gap analysis: the Workbench (origin/main `bb48071`) against those mechanisms

Measured state first, because it sets the bar. The latest live verification
(`results/latest.json`, main `4972de3`) passes 19 of 21 checks, but
the only check that tests general reasoning, `case.unfamiliar_order`, **fails 0/2**
(`pick:newest-invoiced-order:none_found`, `pick:newest-received-po:none_found`), and
`case.po_mail_links` read 50 purchase-order inbox messages, found 48 PO tokens and made
**0 exact links** (24 tokens unmatched). Every case that passes (AO-26005B, five AO packets,
PO5447, customer-PO coverage) passes through a dedicated script: `operating/ao_case_script.py`,
`operating/packaging_case_script.py`, `operating/order_case_script.py` (1,481 lines of hop code
for two root kinds). The capture, citation, permission and claim-checking core is
genuinely strong. What is missing is the layer in between: there is no ontology the agent
can reason over, so every new question needs new hop code.

| Mechanism (section A) | Workbench today | Gap | Severity |
|---|---|---|---|
| **Typed object model bound to sources** (Foundry object types and datasources, Fabric IQ entity bindings) | Three disconnected vocabularies. `ontology/core.py:register_plx_schema` hand-registers 20 Portal-gold types with 2-5 fields each, all `portal-gold` authority (a swarm-bridge path whose credential was never granted). `ontology/transactions.py` keeps a second list of 50 `_TRANSACTION_TYPES`. The live path (`integrations/source_mirror.py`) has **no types at all**, only 93 relations of rows. | The agent sees `public.order_entry_item_assembly` and its columns, not "AssemblyOrder serves SalesOrderLine". The semantics live in prose (`docs/reference/portal-code-map.md`) and in script code. | Critical |
| **Declared link types** (Foundry link types, OCEL O2O) | Link knowledge sits in five places: `core.py` contracts (30), `transactions.py` `_SPINE_RELATIONS`/`_DISPLAY_RELATIONS`/`_PORTAL_GOLD_SPINE_FK`/`_FIELD_ALIASES`, `ontology/relation_contracts.py:FM_SOURCE_FK` (23), `operating/link_contracts.json` (14, the only one on the live path), and the hop code in `order_case_script.py` and `po_mail_links.PortalPoIndex`. | Nothing can traverse. `plx.case_link` *checks* a link between two rows the caller already found, but no tool *follows* links. | Critical |
| **Search-around / object sets** (Foundry Object Set Service, OSDK `pivotTo`) | `OntologyStore.traverse/impact` exist over the unused gold store. On the live path, traversal means `plx.source_rows` with a local filter, one relation per call, page by page. | An N-hop question costs the model N×pages tool calls plus hand joins in context. This is why the scripts exist. | Critical |
| **Entity resolution / cross-system identity** (Senzing, Splink, Foundry multi-datasource objects) | Good parts: `TransactionResolver` (identifier groups, membership candidates, reviewed pairs, incremental dirty sets, F5 indexed buckets). `po_mail_links` puts mail identity through it. `plx.source_resolve` returns exact identifier matches with `selection_required`. Missing: no canonical cross-system object (Customer = `app_customer` + FM `customer` + Sage `ar_customer` + BC Customer), no scored matching (only exact or nothing, so 0 of 48 PO tokens linked), no per-property survivorship/precedence, no "why / why not" explanation. | Every case has to re-derive identity: FM id vs Portal cuid vs BC No., the `-FM<id>` suffix, the `renumberedTo` rewrite, `fmGeneratedPoId`. | Critical |
| **Temporal truth** (bitemporal, XTDB, SQL:2011) | The mirror keeps superseded captures (`api://…@gen-capture`), which gives transaction time from first observation. `OntologyStore.get(at=)` reads uni-temporally. The case store is append-only and hash-chained. No valid-time model; GOV-11/TASK-477 asks for valid/recorded time, confidence and evidence span. | We cannot ask "what did we know on Jan 5" or "what was true on Jan 5". Business dates (required date, produced date, posting date) are just columns, so "late" is a model judgement. That is the AO-26005B lateness conflict. | High |
| **Object-centric event log** (OCEL 2.0, Celonis OCPM) | None. Timelines are assembled per case (`plx.case_get` timeline is *case* events, not business events). Gap kinds exist (`absent_in_source`, `not_ingested`, …) but there is no notion of an expected process. | No process conformance: "PO received but never invoiced", "AO produced after the required date", "deposit computed before the surcharge" are found only when a script looks for them. | High |
| **Actions with typed parameters, submission criteria, approval, writeback, read-back** (Foundry action types and action log) | `plx.case_propose` records free-form `capability` + `payload` + `preconditions`. `plx.case_propose_customer_po` is the one typed action, with real submission criteria. `plx.case_verify_proposal` reads back. `goal2_mc_approval.py`/`goal2_preconditions.py` exist on the platform side. Every action adapter is a fixture (`BLOAT-MAP.md` §0.5). No action-type catalog. | Goal 2 is record-only, and each new action needs bespoke code like `propose_customer_po`. | High |
| **Object- and property-level security, marking propagation** (Foundry markings, restricted views, Glean permission-aware index) | Strong and ahead of most. Relation-family default-deny before fill/rank/count (`source_mirror.AudienceMap`). Per-employee Portal role ∩ Entra ∩ SharePoint ACL (`operating/employee_permissions*.py`, not yet live). Every case read re-resolves every citation for the reader. Missing: property-level markings (cost/margin/wage columns, contact PII are served, `portal-code-map.md` §5), and derived values do not inherit the markings of their inputs. | A computed landed cost or margin is visible to anyone who can see the case, even when an input cost column is finance/L5. | Medium |
| **Agent grounding and tools** (AIP Agent Studio tools, Anthropic tool-design guidance, ReAct) | About two dozen case-host tools, well described, bounded, cited and paged. All of them are row-level (`source_rows`, `source_resolve`) or case-level. There is no schema tool, no object/link/timeline/aggregate tool and no object-set handle. The 83 platform tools are kind-specific (`plx.bc_purchase_case`, `plx.record_freight_offer`, …). | The model is grounded in *tables*, not in *business objects*. Tool outputs are rows the model must join in context, which is token-heavy and error-prone. | Critical |
| **Lineage/provenance** (Foundry lineage, W3C PROV) | Excellent at the leaves: per-row capture citations with digests, `original://` with SHA-256 and extraction versions, Workbench-written vs caller-written statements (`claim.statement_by`), value claims checked on read (`operating/claim_checks.py`), quantity-role algebra (`operating/quantity_types.py`). | Missing only at the derived layer: objects, links, events and identity decisions have no PROV-style `wasDerivedFrom` chain, because those layers do not exist yet. | Low (design the new layers to keep it) |
| **Evaluation** (AIP Evals, held-out QA, groundedness) | `playbook_eval.py` scores *scripted* cases. The live checks run scripts. `case.unfamiliar_order` is the right idea, but it runs a script, not an agent, and fails on picks. | Nothing measures "Claude answers an unfamiliar question over unfamiliar records". | Critical |
| **Unstructured→structured** (GraphRAG extraction, Foundry document pipelines) | Mail citation with body hashes, SharePoint originals, local OCR, `document_understanding.py`, `doc_metadata.py`. Mentions (PO numbers in mail, PI numbers, AO codes on scans) are found per script. | No persistent *mention* layer linking documents to objects, so each case re-scans mail. | High |

Root cause in one sentence: **the Workbench has an excellent evidence layer and an excellent
claim layer, but no semantic layer between them**, so the "reasoning" is in Python scripts
and in the model's context window instead of in a queryable ontology.

## C. Target design for PLX

### C.0 Architecture in one picture

```
 Sources (read-only, GET-only clients; never written)
   Portal Workbench API (93 rel.)   BC Production (3 cos)   Graph mail   SharePoint   operator originals
          │                               │                    │            │               │
 L1 CAPTURE  integrations/source_mirror (api://)  + capture store for bc:// graph-mail:// sharepoint:// original://
          │   immutable, content-addressed captures; superseded content kept; ACL snapshot; digest
          ▼
 L2 ONTOLOGY  ontology/schema/plx.ontology.json  (types, properties, links, events, actions, markings)
          │   ontology/project.py  : bindings → objects, property values, links (every value = a citation)
          │   ontology/identity.py : source records → resolved entities (tiers, scores, reviewed decisions)
          │   ontology/events.py   : OCEL 2.0 event log (E2O/O2O with qualifiers) from bindings + documents
          │   ontology/store.py    : bitemporal SQLite projection (disposable; rebuildable from L1)
          ▼
 L3 REASONING  operating/cases.py (kept): findings + value claims + calculations + gaps + proposals
          │   checked by claim_checks / quantity_types / source_units (kept)
          ▼
 L4 TOOLS     case host MCP: plx.schema / find / object / around / aggregate / timeline / process /
              compare / identity / documents / read  +  case_* writes  +  action_propose / action_verify
          ▼
 Claude (any MCP client) investigates; humans approve in MC/Portal; owners execute; Workbench reads back
```

Six binding decisions:

1. **One schema file is the ontology.** `ontology/schema/plx.ontology.json` declares every
   object type, property (unit, quantity role, marking, precedence), link type, event type and
   action type, with evidence (`path@commit` into the Portal, or a live-trace reference). It
   absorbs `register_plx_schema`, `_TRANSACTION_TYPES`, `_SPINE_RELATIONS`, `_FIELD_ALIASES`,
   `FM_SOURCE_FK`, `link_contracts.json` and `source_unit_contracts.json`. Adding a source table
   becomes a schema edit with a test, not a script. The model reads the same file through
   `plx.schema`.
2. **The projection never copies uncited data.** Every property value and link in L2 *is* a
   pointer to an L1 capture (citation + field path + digest). The objection to
   projecting mirror rows was
   correct about the retired paged import, which was a second, uncitable reader. It does not
   apply here: L2 has exactly one reader (L1), and every value resolves through the same
   citation gate the case store already uses. L2 is disposable and rebuilt from L1 at any time
   (`ontology rebuild`). The one Portal-API reader stays `source_mirror`.
3. **SQLite plus recursive CTEs, FTS5 and Decimal, with no graph database and no vector store in
   v1.** PLX's volume (933 sales orders, roughly 6k PO rows, 93 relations, thousands of mails)
   fits in one SQLite file. The investigative questions are identifier- and link-driven, so BM25
   over identifiers and text beats embeddings here. This is consistent with GOV-13 ("no Neo4j for
   an ontology label") and stdlib-first. Embeddings may be added later as a ranking feature
   inside `plx.documents` only.
4. **Identity is a separate, reversible layer.** Source records are never merged in place. A
   *resolved entity* is a cluster of source records joined by typed, tiered identity edges.
   Every edge has its evidence and can be undone. Senzing-style explanations come with every
   answer ("why are these the same / why not").
5. **Facts, mentions and interpretations are different object kinds.** A property value comes
   from a source field. A *mention* is a span in a document that names an identifier
   (machine-extracted, with an extraction activity). An *interpretation* is model output in a
   case. Mentions become links only through identity tiers. Interpretations never become
   properties.
6. **Actions are declared types with submission criteria evaluated by the Workbench.** Execution
   against Petra systems is always `human_in_owner_system` under the hard rule. The Workbench
   executes only its own ontology edits (review decisions, corrections, annotations), and every
   execution is logged.

### C.1 Object types

Notation: **key** = primary key in the projection (`<type>:<source>:<native id>` for a source
record, `<type>:E<n>` for a resolved entity). Bindings name the L1 relation or entity set and
the key column. "Not served" means the Portal API excludes it (`portal-code-map.md` §6). Those types are
declared with `availability: not_served`, so tools answer `inaccessible` with the reason, never
`absent`.

| Object type (interface) | Source bindings (record types clustered into one entity) | Cross-system identity keys (tier) | Key properties (role/unit/marking) |
|---|---|---|---|
| **Customer** (`Party`) | `public.app_customer` (id: cuid or `fm-cust-N`); FM `public.customer`; `sage.ar_customer` (idcust); BC `customers` (number) | `externalId fm-cust-N` → FM id (**verified**); `erp_code` = Sage IDCUST = BC No. (**verified key, non-unique** → cluster review when >1) | name, code, erp_code, currency, price list, business line; contacts → `Person` (PII marking) |
| CustomerLocation | `app_customer_location`; `sage.ar_ship_to` | `bcShipToCode`, `legacySageShipToCode` (verified) | postal (needed for carrier quote) |
| **Supplier** (`Party`) | `app_supplier`; FM `supplier` (`fm-supp-N`); BC `vendors` | `code` = BC No. when Sage-style (verified); FM id (verified); mail domain (**lead**, from `po_mail_links`) | incoterms, currency, country |
| **Item** (interface; subtypes RawMaterial, Packaging, Bulk, FinishedGood, DistributionItem, Service) | `app_product` (code); FM `raw_material`/`packaging`/`bulk_product`/`finished_product`; `sage.ic_item`; BC `items` | `externalId fm-raw-/fm-pack-/fm-bulk-/fm-fp-` (verified); `code` = BC Item No. (verified); `erp_code` = Sage IC (verified) | uom (label ≠ conversion), hs_code, fill weight, units/box; costs (`average_cost` etc.: **finance marking**) |
| SupplierItem | `app_supplier_product`; FM `raw_material_supplier`/`packaging_supplier` | (supplier, item) | unit cost (finance), delivery cost, MOQ, incoterms, origin |
| PackSpec | (`app_product_pack_spec`, not served) | item | case dims/weight, units/case, cases/layer; no container/CBM field (stage 7 gap, declared) |
| **Formulation / BOM** (versioned) | not served (FM `formula`, `app_formulation`, `app_bill_of_materials`) | code+version | % lines (fraction vs percent per source contract), immutable per version |
| **Project** | not served (`app_project*`, FM `project`); referenced by `app_fg_item.project_key`, `app_quote.project_id` | project key text (**lead**: "project spine is text") | key, customer |
| **Quote** / QuoteLine | `app_quote`, `app_quote_line` | quote number | status, project link (mutable; history only from our captures), stdCost (soft JSON, finance) |
| **SalesOrder** | `app_sales_order`; FM `order_entry`; `sage.oe_order_header`; BC `salesOrders` | `custom_fields.fmId` = FM id (verified); `sageOrdUniq` (verified); BC `externalDocumentNumber` on invoice = order number (**lead**; number rewritten after drain); `fmOeNumber`/`renumberedTo` (verified aliases) | status (free string, normalized), fulfillment mode/ownership, required date (valid-time anchor), totals (Decimal) |
| SalesOrderLine | `app_sales_order_line` (ids unstable on draft edit); FM `order_entry_item`; `sage.oe_order_detail` | (order, product, position) **lead** across re-creation; FM id verified | quantity (**role: ordered**), unit price, uom, MAKE/STOCK |
| **CustomerPurchaseOrder** (identifier-object) | `po_reference`, FM `custpocode`, `sage ponumber`, BC sales-order `externalDocumentNumber`; mail attachments | (customer, normalized PO number); non-PO-like text excluded (`customer_po_coverage` rules) | number, document (Document link), source field |
| **PurchaseOrder** | `app_purchase_order`; FM `generated_purchase_order`; `sage.po_header`; BC `purchaseOrders` | `custom_fields.fmGeneratedPoId` (verified); `fmPoNumber`, `renumberedTo` = BC No. (verified aliases); BC "Your Reference" = FM ponumber (verified when read); `po_number` text (**lead**: unstable) | currency (never assume CAD), incoterms, status (derived per `derivePoFulfillmentCode`), shipping/duty **estimates** |
| PurchaseOrderLine | `app_purchase_order_line`; FM `generated_purchase_order_item`; BC `purchaseOrderLines` | FM id verified; BC line **lead** (not mapped per line, weak link) | ordered / received / invoiced (**three roles**) |
| **SupplierConfirmation** (PI) | Document-derived only (mail/SharePoint) | supplier's own number (alias edge to PO, **reviewed** once accepted) | amount, terms, version (PI-SWC024613/025240 = one document, governing version is a fact to resolve) |
| **Receipt** / ReceiptLine | `app_receipt` (receipt_number = lab id); FM `receipt_packaging`/`receipt_raw_material`; `sage.po_receipt_*`; BC `purchaseReceipts` | `purchase_order_id` (**stored_by_text_join**, never verified); FM `idpogenitem` (verified); BC Vendor Shipment No. = receiptNo (verified) | received qty (**role: received**), freight/duty actuals (first receipt only) |
| **PurchaseInvoice** | `app_receipt.vendor_invoice_number`; BC `purchaseInvoices` | entity map not served → number (**lead**) | amount, currency, posting date |
| **SalesInvoice** | `sage.oe_invoice_header`; BC `salesInvoices` | `externalDocumentNumber` = order number (lead) | amount, remaining |
| **Payment** (CustomerPayment, VendorPayment, Deposit) | BC payments (live), mail/bank documents | applies-to (BC) verified; mail evidence lead | amount, currency, date; a deposit is not settlement proof |
| **AssemblyOrder** (= Portal ManufacturingOrder) | FM `assembly_order` (authoritative); `app_manufacturing_order` (code = `nameamo_c`); `silver.assembly_order_items`; damage/status/task tables | `mo_code` = `nameamo_c` (verified) | planned / manufactured / filled / retained sample / damaged per component / inspected (**distinct roles**, `quantity_types`) |
| AssemblyOrderItem | FM `assembly_order_item`, `silver.assembly_order_items` | FM id | planned qty, actual amount |
| **Lot** | not served (`app_inventory_lot`); BC `itemLedgerEntries` lot no.; FM `batch` | (item, lot number) text (**lead**) | status, expiry (FEFO), ownership |
| **Shipment** | `app_shipment`; `silver.shipments`; `sage.oe_shipment_*` | shipment code | carrier, tracking, freight cost, confirm date |
| **FreightQuote** (inbound forwarder) / CarrierQuote (outbound) | Documents (mail/PDF/xlsx); Freightcom not served | quote ref (lead) | scope (destination-only/all-in), validity, currency, mode; `landed_cost.assess_freight_quote` rules |
| **Document** (interface: Email, Attachment, SharePointFile, OperatorOriginal; DocumentVersion; Page) | `graph-mail://`, `sharepoint://`, `original://` captures | content SHA-256 (verified same bytes) | type, party, dates, ACL snapshot; never business truth by itself |
| **Mention** | extraction over Document pages/bodies | span + identifier kind | value, kind (`petra_po`, `customer_po`, `supplier_ref`, `ao_code`, `lot`, `tracking`…), extractor version, confidence |
| **QualityRecord** (AOPacket, Inspection, Release, eQMS record) | AO packet scans (SharePoint QA/QC "2026 - AOs"); eQMS not served | AO code mention → AssemblyOrder (**corroborated** when code + product + date agree) | inspected qty, release date, signatures (paper, OCR, `to_confirm`) |
| **LedgerEntry** (GLEntry, ItemLedgerEntry, ValueEntry) | BC `generalLedgerEntries`, `itemLedgerEntries` | document no. → Receipt/Invoice (verified within BC) | account (41130 freight, 41131 duty), amount; ERP-D-015: expensed, not item cost |
| **Person / Employee** | Entra users; `app_user` (not served); mail participants | Entra oid; email (verified for internal) | role, department (PII marking; never a principal for agents) |
| **Task / ApprovalGate** | MC `TASK-*`, `apg_*`; Portal `AgentRun` approvals (when live) | MC ids | stage, requestedBy, decidedBy (read-only mirror; MC/Portal own them) |
| **Case / Claim / Proposal** (Workbench-owned) | `operating/cases.py` | case id | already built; becomes an object type so cases link to the objects they investigate |

### C.2 Link types (selection; full list lives in the schema)

Each link type declares: from/to types, cardinality, **binding** (`fk`, `json_path`,
`text_match`, `mention`, `reviewed`), **tier ceiling** (the strongest status its evidence can
reach), and evidence. Tiers, from strongest to weakest: `verified` (typed stored key agrees) ›
`reviewed` (a person accepted it; carries the reviewer, time and the exact source revisions) ›
`corroborated` (≥2 independent features agree under the match model; usable in findings as
*inferred*, never as fact) › `stored_by_text_join` › `lead` › `contradicted`.

| Link | Card. | Binding (ceiling) |
|---|---|---|
| SalesOrder `ordered_by` Customer | N:1 | fk customer_id (verified) |
| SalesOrderLine `line_of` SalesOrder | N:1 | fk (verified); FM idoe (verified) |
| SalesOrder `same_record_as` FM order_entry / Sage order | 1:1 | `custom_fields.fmId` / `sageOrdUniq` (verified) |
| SalesOrder `references_customer_po` CustomerPurchaseOrder | N:M | field (verified if PO-like) / mail mention (lead→reviewed) |
| AssemblyOrder `serves` SalesOrderLine | N:M | FM `order_entry_item_assembly` (verified); allocation never inferred (ER-11) |
| AssemblyOrder `produces` Item; `consumes` Item (via items) | N:1 / N:M | FK (verified) |
| QualityRecord `documents` AssemblyOrder | N:1 | AO-code mention (+ product + date → corroborated → reviewed) |
| Shipment `ships` SalesOrder; ShipmentLine `uses_lot` Lot | N:1 | fk (verified, `salesOrderId` optional) |
| SalesInvoice `bills` SalesOrder | N:1 | BC externalDocumentNumber = order number (lead) |
| PurchaseOrder `ordered_from` Supplier | N:1 | fk (verified) |
| PurchaseOrder `same_record_as` FM PO / BC PO | 1:1 | `fmGeneratedPoId` (verified) / `renumberedTo`=BC No. (verified) |
| SupplierConfirmation `confirms` PurchaseOrder | N:1 | mail mention + sender domain + amount (corroborated → reviewed); supplier number becomes alias |
| Receipt `receives` PurchaseOrder | N:1 | `purchase_order_id` (stored_by_text_join); FM `idpogenitem` (verified) |
| PurchaseInvoice `invoices` Receipt / PurchaseOrder | N:1 | BC (verified inside BC) / number (lead) |
| Payment `pays` Invoice; Deposit `prepays` PurchaseOrder | N:M | BC applies-to (verified); mail (lead) |
| LedgerEntry `posts` Receipt/Invoice | N:1 | BC document no. (verified) |
| FreightQuote `quotes_for` PurchaseOrder / Shipment | N:1 | mention (lead→reviewed) |
| Quote `for_project` Project; Quote `for_customer` Customer | N:1 | text key (lead) / fk (verified) |
| Document `mentions` any object | N:M | Mention resolution (lead/corroborated) |
| Document `attached_to` Email; `version_of` Document | N:1 | Graph structure / same SHA-256 (verified) |
| Case `investigates` any object | N:M | Workbench (verified) |

### C.3 Object-centric event log (OCEL 2.0 shape)

Tables in `ontology/store.py`: `event(id, type, time, time_basis, source_citation)`,
`event_attr`, `e2o(event, object, qualifier)`, `o2o(object, object, qualifier, valid_from,
valid_to)`, `object_change(object, property, value_citation, valid_from, recorded_at)`. This is
the OCEL 2.0 relational shape, so the log can be exported for PM4Py/Celonis-style analysis
without extra work.

Events are **declared in the schema** as bindings from date columns, status changes observed
between captures, and documents:

| Process | Event types (source) | E2O qualifiers |
|---|---|---|
| O2C | `SO.created` (created_at), `SO.confirmed` (status capture change), `SO.required` (required date: a *planned* event, `time_basis: planned`), `CustomerPO.received` (mail receivedDateTime), `AO.planned` (FM plan date), `AO.produced` (FM status/production rows), `AO.damage_recorded`, `QC.inspected` / `QC.released` (paper, OCR → `time_basis: document_ocr`), `Shipment.confirmed`, `SalesInvoice.posted` (BC), `Payment.applied` (BC) | order, line, ao, item, customer, shipment, invoice, document |
| P2P | `PR.created`, `PO.issued`, `PO.sent` (outbound mail), `Supplier.confirmed` (PI mail), `Deposit.paid` (mail/BC), `Freight.quoted` (forwarder mail), `Goods.shipped` (BOL), `Receipt.posted` (app_receipt / BC receipt), `PurchaseInvoice.posted`, `VendorPayment.applied`, `GL.posted` | po, line, supplier, receipt, invoice, document, quote |
| Q2O | `Quote.created`, `Quote.project_changed` (only from our captures; earlier changes invisible, declared), `Quote.accepted`, `SO.converted_from_quote` | quote, project, customer, order |

Every event carries `time_basis` ∈ {`source_recorded`, `source_business_date`, `planned`,
`capture_observed` (we saw the change between two captures, so the true time is within that
window), `document_stated`, `document_ocr`}. Lateness, durations and ordering are computed only
between events whose bases allow it. AO-26005B's "late" then becomes a typed conflict
(`SO.required` planned 2026-01-01 vs `AO.produced` source date 2026-01-05 vs paper 2026-01-07),
not a model opinion.

**Process conformance** (Celonis-style, declarative): `ontology/schema/processes.json` declares
expected stage graphs per object type (PO: issued → confirmed → received → invoiced → paid;
SO: confirmed → produced/picked → shipped → invoiced → paid) with convergence/divergence
rules: one PO, many receipts; one AO, many order lines; never a 1:1 assumption. `plx.process`
returns, for a root object or an object set: stages present (with citations), missing (typed:
`not_served`, `not_ingested`, `absent_in_source`), out of order, quantity mismatches across
roles (ordered vs received vs invoiced = the three-way match), and the variant frequency across
peers ("78% of packaging POs from this supplier have a PI mail before the deposit"). This is
what makes "unusual" detectable without a script.

### C.4 Entity resolution

- **Three stages, all incremental**, reusing `TransactionResolver`'s design (identifier groups,
  membership, reviewed pairs, dirty-set reconciliation):
  1. *Deterministic bindings* from the schema (fmId, fmGeneratedPoId, externalId prefixes,
     erp_code, BC No.) → `verified` same-record edges. A non-unique key produces a cluster
     candidate, never a merge.
  2. *Identifier normalization and blocking*: per identifier kind, declared normalizers
     (`PO5447` / `PO 5447` / `Our PO 5447` / `5447-FM123` → `petra_po:5447`; `PI-SWC025240` /
     `PI-25240`; AO codes `AO-YYDDDL`). These are blocking keys, so comparisons stay linear.
     This is the fix for 0/48 linked tokens.
  3. *Scored matching* (Fellegi–Sunter): comparison vectors over identifier agreement,
     counterparty (sender domain vs supplier domain), date proximity (message vs PO date),
     amount/quantity agreement (Decimal, unit- and currency-aware), and item codes. m/u
     probabilities are estimated by EM over our own data (Splink's method, reimplemented
     stdlib-only on a small scale). Thresholds: above upper → `corroborated`; between → review
     queue; below → nothing. **Anchors (orders, POs, AOs, invoices) never merge
     automatically**; only documents and mentions attach automatically, at `corroborated` at
     most.
- **Survivorship without overwriting**: an entity shows each property per source with the
  schema's declared precedence (e.g. PO display number: BC No. › gold › FM, the Portal's own
  rule). Disagreement is shown as a `conflict`, never resolved silently.
- **Explainability**: `plx.identity` returns, for any two records or a cluster, the edges, their
  features and weights, the reviewer decisions, and *why not* for the near-misses (Senzing's
  why/how).
- **Reversibility**: review decisions are events bound to exact source revisions. A source
  change invalidates the edge (already true in `transactions.py`). An un-merge is an event, not
  a delete.

### C.5 Temporal model

Every property value and link carries four times: `valid_from`/`valid_to` (business time,
from declared date columns or `capture_observed` windows), `recorded_at` (first capture in L1:
transaction time), `source_modified_at` (`updated_at` / `audittimestampmodified` / `audtdate`,
with the mode recorded, since ETL bumps them), and `superseded_at`. Two query modes on every
read tool:
- `as_known_at=T`: what the Workbench had captured by T (audit: "what did the agent see when it
  proposed this?"). Cases pin it automatically at write time.
- `as_of=T`: the best estimate of what was true at T, from valid time.

History before our first capture is declared unknowable (`history_before_first_capture`). It is
never inferred (e.g. Quote.projectId changes before observation).

### C.6 Action types

`ontology/schema/actions.json`, evaluated by `operating/actions.py` (replacing the free-form
`capability` in `plx.case_propose`):

```json
{"id": "SetCustomerPO", "target": "SalesOrder",
 "params": {"customer_po": "CustomerPurchaseOrder.number", "evidence": "Mention|Document"},
 "submission_criteria": [
   "target.customer_po_status in [absent_in_source] (all PO fields read)",
   "evidence resolves now for actor and holds exactly params.customer_po",
   "evidence.customer == target.customer (verified or reviewed)",
   "actor holds operations.sales"],
 "owner_system": "portal", "executor": "human_in_owner_system",
 "approval": {"route": "mc_request_approval", "decider": "human with approval.decide, not requester"},
 "expected_effect": {"object": "target", "property": "po_reference", "equals": "params.customer_po"},
 "verification": "read_back", "idempotency": ["target", "params.customer_po"]}
```

Lifecycle: `proposed` → `validated` (criteria re-evaluated now, with citations) →
`approval_requested` (MC gate id recorded; the MC write itself needs explicit human approval
per call) → `approved`/`rejected` (read from MC) → `dispatched_by_human` (the owner performs
it) → `effect_observed` / `effect_not_observed` / `unknown` (read-back; F7 states). Stale check:
if any cited input changed after approval, the approval is void for verification purposes.

Initial catalog, all `human_in_owner_system` unless noted: `SetCustomerPO`,
`AttachCustomerPOPdf`, `ProposeSharePointMetadata` (Portal taxonomy columns),
`CorrectAOQuantityRecord`, `FlagLandedCostAssumption`, `DraftFreightQuoteRequest` (draft
text only; never sent), `CreateFollowUpTask` (MC write; approval per call),
`ReviewIdentityLink` / `RejectIdentityLink` / `AnnotateObject` / `CorrectClaim`
(**`workbench_internal`**: executed immediately as ontology edits, logged as action events, like
Foundry edits layered over source data, and never written back to a source).

### C.7 Security

- **Object level**: an object or entity is visible if its source records are visible
  (relation-family audience ∩ per-employee resolver). A cluster shows only the members the
  reader can see. The existence of hidden members is counted only when policy allows counts.
- **Property level**: markings declared per property in the schema. `finance` (costs, margins,
  `stdCost`, landed values), `l5` (wages/rates), `pii` (contact names, emails, phones,
  addresses: served by the API per `portal-code-map.md` §5, so trimmed per actor here), `quality_controlled`.
  A marking is a principal set to hold, ANDed with the object rule.
- **Propagation**: anything derived (calculation, event, link, identity edge, aggregate,
  finding) carries the **union of its inputs' markings**, so a reader needs all of them. This is
  the Foundry marking-propagation rule, and the missing piece today.
- **Order of enforcement**: before retrieval, ranking, counting and caching (already the mirror
  rule), extended to `plx.find`/`around`/`aggregate` (aggregates over partially visible sets
  return `partial: true` with withheld counts only if policy permits).
- **Agents are never principals**. A tool call acts for the session's employee. Tests prove
  denial for each of Portal role, Entra group and SharePoint ACL separately.
- **Model output is derived data.** Foundry's documentation states that read-time filters do
  not survive once rows enter a model's context, and only mandatory markings do. So every case
  finding, interpretation, calculation and proposal is stamped with the union of the markings
  of everything it cites or used, and is re-gated per reader. The case store already re-resolves
  citations per reader; markings close the remaining hole, which is text the model wrote *from*
  marked values.

### C.8 Agent tool surface (case host)

The agent gets **business-object tools with object-set handles**, so N-hop questions are 1–3
calls and the outputs are compact.

| Tool | Purpose | Notes |
|---|---|---|
| `plx.schema` | object/link/event/action types, properties with unit/role/marking/availability, processes | the model's map; small, cached, versioned (schema digest in every answer) |
| `plx.find` | resolve identifiers (normalized, all kinds) and filter/text-search objects of a type | returns entities with match explanation; ambiguous → candidates, never a winner |
| `plx.object` | one entity: per-source property values with citations, conflicts, identity summary, linked counts by link type | `as_of` / `as_known_at` |
| `plx.around` | search-around from an object or set handle along link types (≤3 hops, tier floor, filters) | returns a new set handle + per-hop counts + top rows; tier shown on every edge |
| `plx.aggregate` | group/sum/count over a set, Decimal, unit- and role-aware (refuses role mixing like `case_compute`) | totals carry markings |
| `plx.timeline` | OCEL events for a set, ordered, with `time_basis` and conflicts | `as_of` |
| `plx.process` | conformance and variant stats for a root or set (three-way match, missing stages, peer frequency) | typed missing reasons |
| `plx.compare` | field-by-field diff of two objects/versions or object vs document mention (PO vs PI) | units/currency/roles respected |
| `plx.identity` | why/why-not for records or a cluster; review queue | review decisions go through `plx.action` |
| `plx.documents` | FTS over mail/SharePoint/originals visible to the actor, with mentions resolved to objects | bodies stored hashed; snippets bounded |
| `plx.read` | resolve any citation to content (existing `source_citation` / `read_original` / mail) | unchanged contracts |
| `plx.case_*` (existing) | open, add_finding (+value_claims, relation links), compute, gap, get, check_claims, close | kept as is; `case_link` becomes internal to identity |
| `plx.action_propose` / `plx.action_status` / `plx.action_verify` | typed action from the catalog; criteria evaluated; MC gate referenced; read-back | replaces `case_propose`, `case_propose_customer_po`, `case_verify_proposal` |

Join safety (dbt MetricFlow's rule): `plx.aggregate` resolves paths from link cardinality and
**refuses fan-out**. Summing PO header amounts across a PO→receipts hop is rejected with
"aggregate at PO grain, or sum receipt amounts". Many-to-many hops (AO ↔ order lines) can be
traversed but never aggregated through without an explicit allocation, which is never
inferred (ER-11).

Design rules (from Anthropic's tool guidance and our F8): every list answer states
`complete`/`truncated` and why; every value has a citation; responses ≤ the 48 KB envelope with
handles for more; errors are actionable ("`not_served`: Formulation is not exposed by the Portal
API; ask Portal owners; record a gap"). Results use human-readable names first with ids for
chaining, and a `response_format` of `concise` or `detailed`. **There are no kind-specific
tools.** The AO, packaging and order scripts become *verified procedures*: short method notes
(which objects, which links, which roles to keep apart) with a `verified_by`/`verified_at`
stamp, like Cortex `verified_queries` and Genie trusted assets. An answer that followed one is
labelled `procedure: <id>@<version>`; anything else is labelled free investigation.

**Decided 2026-10-01.** These names are the only
ones: `plx.discover` = `plx.find`, `plx.expand` = `plx.around`. One identifier grammar
(`ontology/schema/identifiers.json`) and one resolver in the ontology identity layer back
`plx.find`; before the projection exists the resolver may read SourceMirror, `bc_read` and
`graph_read` behind the same contract. `plx.investigate` runs `ontology/plans/<family>.json`
(types and primitive calls only; no ids, expected values or per-family code) with one runner.
The case host keeps at most 22 tools, enforced by a test. The eval reports scripted,
plan-family and untemplated held-out pass rates separately.

### C.9 Monitors (an Automate equivalent) and scenarios

- **Monitors** replace `business_watch.py` with declarations in
  `ontology/schema/monitors.json`: an object-set condition plus a process rule, e.g. "PO
  received, no purchase invoice after 30 days", "SO confirmed, required date passed, no
  shipment", "Portal-entered SO with no customer PO and a mail mention of a PO-shaped number
  from that customer's domain". The ingest worker evaluates them after each projection update
  and emits *case suggestions* (root object, rule, citations) into a per-employee inbox. They
  never emit actions. Every hit is permission-gated per reader.
- **Scenarios** answer the 100k-bottle class of question: a named, case-scoped override set
  (quantities, rates, dates, a freight option) applied to `plx.aggregate`/`case_compute`
  results, never to L1/L2. Results are labelled `scenario:<id>` and carry their override
  citations. A scenario cannot satisfy an action's submission criteria (Foundry submission
  criteria can test "in a scenario"; ours refuse).

## D. Build plan: six weeks, seven parallel workstreams

Ownership is disjoint by file, so 7–14 contributors can run at once. Every workstream ships an
isolated-fixture test, an injected-transport test and a `verify/live-checks/<id>.json`. Nothing
counts as done until the live checks pass it on `main`.

| WS | Owns | Weeks 1–2 | Weeks 3–4 | Weeks 5–6 | Acceptance (live unless noted) |
|---|---|---|---|---|---|
| **W1 Schema** | `ontology/schema/*.json`, `ontology/schema.py` (loader, validator, digest) | Port every existing contract into `plx.ontology.json`: `link_contracts.json` (14), `FM_SOURCE_FK` (23), `_FIELD_ALIASES`, `source_unit_contracts.json`, the `portal-code-map.md` crosswalk; object types of C.1 with `availability`; markings | Event bindings (C.3), `processes.json`, `actions.json` (C.6) | BC/G-L/ledger bindings; Formulation/Lot as `not_served` | Schema test: every binding names a served relation/column in the live catalog (`portal.health_catalog` columns), or is declared `not_served`; zero orphan link types; the digest is stable |
| **W2 Projection store** | `ontology/store.py`, `ontology/project.py`, rebuild CLI | Bitemporal tables (objects, values-as-citations, links, OCEL tables); projector over `SourceMirror` captures; incremental by capture id | Projector over the BC/mail/SharePoint capture store (W4); `as_of`/`as_known_at` | Performance: full rebuild <5 min on the live mirror; incremental <30 s | Rebuild from L1 twice → identical digests; every value resolves through `CaseRuntime.resolve_citation` for an allowed actor and is withheld for a denied one (0 leaks over all values) |
| **W3 Identity** | `ontology/identity.py` (absorbs `TransactionResolver` core, `po_mail_links` ledger logic, `case_links` evaluator) | Deterministic tier + normalizers + blocking | Fellegi–Sunter scoring with EM over live data; review queue; `plx.identity` explain | Un-merge/invalidation on source change; survivorship/precedence | On the purchase-order inbox: ≥70% of PO tokens resolved to a PO entity at `corroborated` or better, with a precision ≥0.98 on a 100-token labelled sample (today 0/48 linked); 0 anchor auto-merges; a cross-system Customer/Supplier/Item cluster rate is reported with a conflict list |
| **W4 Documents & captures** | `integrations/capture_store.py` (unifies `bc://`, `graph-mail://`, `sharepoint://` listing + content captures with ACL snapshots), `ontology/mentions.py` | Persist BC entity sets (POs, receipts, purchase/sales invoices, G/L, item ledger) and mail/SharePoint items as citable captures | Mention extraction (identifiers, amounts, dates) with extractor versions; FTS5 index per actor-visible scope | AO packet pages → QualityRecord mentions; PI/freight quote extraction | Mail ingestion ≥1 shared inbox complete for 90 days; every mention cites a page/body span with SHA-256; FTS returns nothing a denied actor cannot read (leak test) |
| **W5 Events & process** | `ontology/events.py`, `ontology/process.py`, `ontology/monitors.py` | OCEL derivation from W1 bindings; `time_basis` | Conformance per `processes.json`; three-way match P2P; peer variant stats | OCEL 2.0 SQLite export validated against the spec; `monitors.json` evaluated by the ingest worker (replaces business watch) | AO-26005B lateness reported as a typed conflict with three cited events; for 100 random POs, `plx.process` output agrees with an independent SQL oracle on stage presence (≥98%) |
| **W6 Tools & actions** | `operating/ontology_tools.py` (new tool family on the case host), `operating/actions.py`, `operating/mcp_server.py` registration | `plx.schema/find/object/around` with set handles; F8 completeness | `aggregate/timeline/process/compare/identity/documents`; `action_propose/status/verify`; `SetCustomerPO` re-expressed as a catalog action (deletes `case_propose_customer_po`) | MC approval reference read-back (`mc_get_task`, read-only); stale-approval and uncertain-outcome states (F7) | MCP contract tests; every tool ≤48 KB; handles expire; `SetCustomerPO` reproduces today's behaviour byte-for-byte on fixtures |
| **W7 Evaluation** | `evals/` (benchmark builder, oracle, runner, scorer), `verify/live-checks/eval.*.json` | Benchmark builder: random real roots (seeded, stratified) + 10 question families; deterministic SQL oracle **independent of W2** | Agent runner: Claude over the case host with **only generic tools**, no scripts; scorer using `claim_checks` | Held-out human cases (Vince A–D, Ricardo AO packets, Stephen when received) | See below |

**Week-by-week gate.** End of week 2: `plx.find`/`object`/`around` live on the mirror, and the
AO-26005B chain is reachable in ≤3 tool calls without `ao_case_script`. End of week 4: the
benchmark runs nightly; the PO→mail links meet the W3 bar. End of week 6: targets below, and the
case scripts are deleted (E).

**The acceptance test: the "unfamiliar transaction" benchmark** (`eval.unfamiliar`, nightly, live):
- **Sampling**: each night, a seeded random sample of 40 roots, stratified as SalesOrder × origin
  (Sage/FM/Portal-entered), PurchaseOrder × derived status, AssemblyOrder, Receipt and Shipment.
  The known cases (OR1000151885, PO5447, AO-26005B) are excluded. Each root gets 3 questions
  drawn from 10 families: reconstruct the chain; customer-PO status and evidence; ordered vs
  produced vs shipped; ordered vs received vs invoiced (three-way match); on-time vs required
  date; cost/landed cost with stated unknowns; related documents/mail; who/when; conflicts
  between sources; what is missing and who can supply it.
- **Oracle**: deterministic SQL over L1 captures, written independently of the projection, for
  structured answers. Answers needing documents are scored on citation validity and gap honesty
  only, until humans label them.
- **Scoring per question**: correct (values match the oracle exactly, roles and units
  included); `claim_checks` passes (no uncovered or mismatched number); gap honesty (when the
  oracle says `not_served`/`absent`, the agent must say so and not invent); citation recall and
  precision in the ALCE sense (each statement supported; each citation needed), measured by
  sampled LLM-judge runs with position-swapped prompts plus a human audit of 10 per week; cost
  (tool calls, tokens, latency). Each question runs k=3 times and we report **pass^3**
  (τ-bench reliability), not just best-of.
- **Targets**: baseline today 0/2 (the script fails picks). Week 4: ≥60% fully correct on
  structured families. Week 6: **≥85% fully correct, 0 unsupported numbers, 0 permission leaks,
  ≥95% gap honesty, median ≤8 tool calls**. Run the denied-actor variant (same questions,
  narrowed principals) every night; any leak fails the build.
- **Stakeholder cases as held-out regressions**: Vince A (PO → supplier confirmation → freight
  trail on a *new* PO he names), Vince B (customer-PO coverage plus a mail-sourced
  `SetCustomerPO` proposal with read-back), Vince C/D (PO5447 landed-cost reconstruction with
  every unknown typed), Ricardo (six AO packets: planned/manufactured/filled/inspected distinct,
  paper vs FM conflicts typed). Pass = the same generic tools, no case-specific code, and the
  stakeholder confirms the conclusions (WS-16).

## E. Keep, replace, delete

**Keep (the evidence and claim core: it is right)**
- `integrations/source_mirror.py`, `workbench_read_api.py`, `live_source_guard.py`,
  `graph_read.py`, `bc_read.py`, `bc_purchase.py`, `graph_evidence.py`, `http.py`: L1 capture
  and the GET-only egress rule.
- `operating/cases.py`, `claim_checks.py`, `quantity_types.py`, `source_units.py`
  (`source_unit_contracts.json` moves into the schema; the module stays the evaluator),
  `case_originals.py`, `case_mail*.py`, `case_verification.py`: L3.
- `operating/employee_permissions*.py`, `entitlements.py`: L2/L4 security (extended with markings).
- `operating/ingestion.py`, `ingest_worker.py`: they feed L1 and W4 extends them.
- `operating/landed_cost.py`, `customer_po_coverage.py` (its rules become derived properties of
  SalesOrder/CustomerPurchaseOrder), `quote_readiness.py`, `quote_cost_trace.py`:
  deterministic Portal-mirrored business logic, called by `plx.aggregate`/`compare` and actions.
- `playbook_eval.py`, `playbook_reports.py`: they become the W7 scorer's base.
- `operating/case_runtime.py`, `mcp_server.py` case host: these stay the host; tools are added.

**Replace**
- `ontology/core.py` (`OntologyStore`, `register_plx_schema`, per-observation ACL lists,
  uni-temporal reads) → `ontology/store.py` + `schema/plx.ontology.json`. Keep `Actor`,
  `EntityRef`, `LEVELS` and the validation helpers, which 42 modules import.
- `ontology/transactions.py` (2,522 lines) → `ontology/identity.py`. Keep the identifier-group
  and membership ideas and the incremental dirty-set reconcile; drop the gold-store coupling and
  the retired-source guards once the platform is gone.
- `ontology/relation_contracts.py`, `operating/link_contracts.json`, `operating/case_links.py`
  → link types in the schema plus the evaluator inside `identity.py`. `plx.case_link` becomes
  internal; findings cite link ids.
- `plx.case_propose`, `plx.case_propose_customer_po`, `plx.case_verify_proposal` →
  `plx.action_*` over `actions.json`.
- `operating/po_mail_links.py` → mention extraction (W4) + identity (W3); its rules move into
  the normalizers and features.
- `operating/business_watch.py` → `monitors.json` + `ontology/monitors.py` (suggestions only).
- `operating/ao_case_script.py`, `packaging_case_script.py`, `order_case_script.py`,
  `case_workspace_live.py` scripts → verified procedures + W7 benchmark fixtures. Delete once the
  benchmark passes the same live checks they pass today (`case.ao_case_script`,
  `case.ao_packets`, `case.packaging_case_script`) through generic tools.

**Delete (now unblocked by W7 replacing the `runtime-focused` gate)**
- `operating/unified.py`, `objectives.py`, `campaigns.py`, `autonomy_driver.py`,
  `business_watch.py`, `goal_intake.py`, `goal_campaign_planner.py`, the 34 scenario modules
  (`supply_coordination`, `order_commitment_case`, `purchase_impact`, `manufacturing_impact`,
  `quality_impact`, `delivery_planning`, `project_readiness`, …), `fixture_actions.py`,
  `service.py` fixture adapters, and the 83 platform MCP tools. That is the 114-module /
  58.1k-line closure.
- `integrations/portal_named_*.py`, `portal_bridge.py`, `portal_replica.py`, `portal_export*`
  (the swarm-bridge path, never credentialed); `ontology/fixtures.py` (retired 2026-10-02) and the synthetic JSONL.
- `operator_ui/app.js` panels for objectives/campaigns; the console is rebuilt on the W6 tools
  (one object page: properties, identity, timeline, process, documents, cases, actions).
- Do this by import closure in sub-slices, Expected net result: ~80k → ~30k production lines with a larger capability surface.
