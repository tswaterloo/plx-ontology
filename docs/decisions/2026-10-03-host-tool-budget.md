# 2026-10-03 host tool budget: list the generic tools within 22 by merging, not raising

Main d26a04c4. Resolves
`case-host-budget`, `case-host-budget-answers` and
`documents-metadata-and-host-mail`. The listed set was checked against operating/mcp_server.py:223-228.

## Context
The case host lists exactly 22 tools on main: 15 case/document tools, action_get and
action_propose, schema, find and object, around and timeline. plx.identity, plx.documents
and plx.read (ef6e8ae8, f82d8d68) and plx.aggregate and plx.compare (dd0ed98b) are on
`CaseHostTools`. No production caller registers them, so no agent can reach them, and
they cannot move the unfamiliar-transaction pass rate. plx.process is not on
CaseHostTools, and plx.investigate has no code yet.

## Options
(a) Keep the cap at 22 and merge the case tools that ontology-design C.8 already names. (b) Raise the
cap with a C0 change. (c) List over the budget with an expiry.

## Evidence
- Anthropic says tool selection degrades past about 30-50 tools and recommends tool
  search above about 10 tools or 10K tokens of definitions:
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool .
  With tool search, Opus 4 went from 49% to 74% on large MCP sets:
  https://www.anthropic.com/engineering/advanced-tool-use .
- RAG-MCP measured 13.6% selection accuracy on a large toolset without retrieval and
  43.1% with it: https://arxiv.org/html/2505.03275v1 .
- Our 22 specs are about 10K tokens. The case-tool specs average about 1.5K characters,
  and the contract allows 600 for its own tools. The weight is in legacy specs, not in
  the count.
- No one has measured 22 in this repo (it is only stated). 29 would still be
  under the documented ceiling. Merges cost nothing and cut duplicate paths.

## Decision
Option (a), in two phases. The cap stays at 22.
- **Phase 1 (now).** List plx.read, plx.identity, plx.documents,
  plx.aggregate and plx.compare. Retire these 5 slots:
  1. `plx.source_citation` -> plx.read. Port first: paging by offset inside a page, and
     the list of extraction versions.
  2. `plx.case_cite_mail` -> plx.read of `graph-mail://`, then `plx.case_add_finding`
     with that citation. Keep the body_sha256 pin in the read digest.
  3. `plx.case_attach_sharepoint_original` -> a source mode of
     `plx.case_attach_original`. This follows the reextract merge
     (ontology_tools.py:2137).
  4. `plx.case_link` -> a link mode of `plx.case_add_finding` ("+relation links").
  5. `plx.document_metadata_proposals` and `plx.document_fields_confirm` -> one
     document-fields tool.
  The same commit updates every file that names a retired tool: question_bank.json,
  playbooks, test_mcp_server.py and test_mcp_case_tools.py.
- **Phase 2 (with the goal-2 MCP tools).** `plx.case_propose` -> plx.action_propose, and
  `plx.case_verify_proposal` -> plx.action_verify. This frees the slots for
  plx.process and plx.investigate.
- **documents-metadata-and-host-mail.** Keep (a) the C0 contract unchanged now. Ship (c)
  with phase 1: the case host grants `mail:<mailbox>` units from
  `MailCaptureStore.audience_check`, and graph-mail handles are re-gated with
  `item_gate`. Take (b), the C0 metadata block, only if the confirm action stays unbuilt.
- **Spec weight.** Each retained case-tool spec is trimmed toward the 600-character
  contract budget in the same change.

## Consequences
The generic tools become reachable over MCP, so the unfamiliar eval can measure them.
Two legacy paths go away: source_citation duplicated plx.read, and the separate
SharePoint attach duplicated attach_original. The question bank and playbooks change in
the same commit.

## How we will know it was wrong
Run an A/B of the unfamiliar pass rate with 22 tools listed and with 29 tools listed
(phase 1 without the merges). Phase 1 was wrong if 29 shows no drop. It was also wrong
if eval traces show wrong-mode calls on the merged tools (attach or add_finding modes).
