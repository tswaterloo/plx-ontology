# Decision 2026-10-01: tools-shape (one tool vocabulary, layered)

- decided: 2026-10-01T18:40Z
- status: decided

## Context

An earlier decision chose a layered tool surface: generic primitives, `plx.investigate` over
declarative per-family plans, and a bounded ReAct loop for the rest. That settled the
principle but left two concrete designs alive at the same time:

| | Layered primitives design | Alternative design |
|---|---|---|
| find a root | `plx.find` | `plx.discover` |
| walk links | `plx.around` | `plx.expand` |
| identifier grammar | schema (ontology) | `operating/identifiers.json` |
| resolver | ontology identity / projection | `operating/discovery.py` over SourceMirror, bc_read, graph_read |

Following either would produce a second
resolver and a second tool family. Evidence from main `09f4cb3`:

- The drift both designs warn about continued. `f83eba4` (18:09Z) added +807 lines to
  `operating/order_case_script.py`: BC-invoice and receipt picking inside a case script, the
  exact change the alternative design said not to make.
- Identifier grammars are still duplicated: one more full PO grammar sits in
  `operating/po_mail_links.py:208-219` (at 09f4cb3) beside the copies in `integrations/document_understanding.py` and
  `integrations/graph_evidence.py`. Live `case.po_mail_links`: 48 PO tokens, 0 exact links (live
  `69b2ee8`, main bd26932).
- Live `case.unfamiliar_order`: 0/2 cited (`bc_unreadable`, `none_found`).
- `operating/mcp_server.py` registers ~99 `plx.*` tool names across hosts; the case host
  alone lists 29 tools in ~40.6 KB of JSON.

## Options

1. **Generic primitives only** (the ontology design as first written). Smallest surface. Risk: multi-hop
   enterprise questions done by free ReAct are slow and error-prone.
2. **Typed per-family tools** (08's 20-40 tools such as `get_po_trace`). Highest accuracy on
   known families. Risk: tool count, overlap, and per-case code; it is the pattern that
   produced the seven find tools and the 807-line script.
3. **Layered**: primitives + declarative plans run by one generic runner + bounded ReAct.

## Evidence

- Free-form query over enterprise schemas fails: Spider 2.0 21.3% (o1-preview), BEAVER
  10-21%; constrained per-source interfaces beat agentic SQL (RUBICON).
- Tool count and overlap hurt selection. Anthropic, "Writing effective tools for agents"
  (https://www.anthropic.com/engineering/writing-tools-for-agents): "Too many tools or
  overlapping tools can also distract agents from pursuing efficient strategies"; tools
  should "consolidate functionality". RAG-MCP (Gan & Sun 2025, arXiv 2505.03275) measures
  selection accuracy collapsing as the tool pool grows (13.62% expose-all vs 43.13% with
  retrieval on its stress test; figure from a secondary summary).
- Planned execution beats step-by-step ReAct on known shapes in cost and often accuracy
  (ReWOO, arXiv 2305.18323; LLMCompiler, ICML 2024, arXiv 2312.04511).
- Verified, reusable query assets are how semantic-layer products keep accuracy without
  per-question code (Snowflake Cortex Analyst verified queries, Databricks Genie trusted
  assets).
- Moving family logic into JSON plans does not by itself show generality; report scripted,
  plan-family and untemplated results separately.

## Decision

Option 3, made concrete:

1. **One vocabulary: the layered design's names win.** `plx.discover` is `plx.find`; `plx.expand` is
   `plx.around`. No `discover`/`expand` tools are registered.
2. **The alternative design's content is kept, relocated into the ontology.** The identifier grammar
   (one entry per identifier kind: pattern, normalisation, carrying fields per system,
   cited upstream path@commit) becomes the schema's identifier section under `ontology/`
   (for example `ontology/schema/identifiers.json`), and every cross-system key becomes a link-type source binding. The single resolver lives in
   the ontology identity layer and backs `plx.find`; there is no `operating/discovery.py`.
   Until the projection exists, the resolver may read SourceMirror, `bc_read` and
   `graph_read` directly behind the same `plx.find` contract, so the unfamiliar-order fix
   ships now and keeps its interface when the projection lands.
3. **All regex grammars move to that one file.** `po_mail_links.py`,
   `integrations/document_understanding.py`, `integrations/graph_evidence.py` and `integrations/live_source_grammar.py` call it.
   The 0/48 normalizer fix is made there, once.
4. **Plans are data over primitives only.** `ontology/plans/<family>.json` may name object,
   link and event types and primitive calls with parameters; it may not contain record ids,
   expected values, or per-family Python. One plan runner executes them; each step returns
   exactly what the primitive returns, so the eval can compare plan and free runs on the same
   item. The no-hardcoded-cases gate covers `ontology/plans/`.
5. **Case-host budget: 22 tools, enforced by a test** that counts the case host's tool list.
   No tool retrieval layer at this size; reconsider only past 40 tools.
6. **Freeze kind scripts now.** `order_case_script.py` takes no more pick or key logic after
   `f83eba4`; that logic is ported into the resolver behind `plx.find`, and the script
   becomes an eval item once `case.unfamiliar_order` passes through `plx.find` 3 runs.
7. **Report three numbers.** Scripted regressions, plan-family pass rate, and untemplated
   held-out pass rate, each with run count, in state.status and the daily report.

## Consequences

- One resolver, one grammar, one find tool. The seven find tools (`plx.search`,
  `transaction_search`, `ask_transaction`, `transaction_inquiry`, `source_resolve`,
  `lookup_replica_named`, `discover_fm_procurement`) retire into `plx.find` on the case host.
- Order of work: grammar + keys + resolver first, then the ≥25-transaction hourly eval,
  then porting scripts, under the names above.
- A small up-front cost: the resolver must honour the `plx.find` I/O contract from the day-1
  contracts PR, so that contract must define `plx.find`/`plx.around` first.

## How we will know this was wrong

- Untemplated held-out pass rate below half the plan-family rate after week 2 → the
  primitives are too weak; add typed *primitives* (never per-family tools).
- Plan runs not ≥10 points better than free ReAct on the same family over 3 runs → drop
  plans for that family (simpler surface wins).
- Wrong-tool or overlapping-tool calls in more than 5% of eval transcripts → cut the budget
  or revisit tool retrieval.
- `case.unfamiliar_order` still 0/2 one week after the resolver lands → the failure is data
  (BC renumbering, unserved mapping), not tool shape.
