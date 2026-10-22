# Decision 2026-10-08: one capture path for brain knowledge items

- decided: 2026-10-08 (main b5332d08); resolves open-gov13-knowledge-one-brain-capture

- question: `mc_sources` `brain_items` and `knowledge_source` (kind `brain`) both capture the brain's `agent/items`, so a reader with both grants sees one item as two objects.
- decision: option 1. Retire `brain_items` (collection, `KnowledgeEntry` brain binding, ingestion options, tests). `knowledge_source` is the only brain-item path. MC `mc_search_knowledge` hits stay `KnowledgeEntry` in `mc_sources`.
- rejected: option 2 changes the `mc://` grammar and still duplicates the path without a `brain` family. Option 3 keeps a duplicate and an unenforced personal-namespace boundary.
- evidence: integrations/knowledge_source.py:29-32, 102 (namespace authority, personal refused, never stored), :44-47; integrations/mc_sources.py:135, 276 (`brain_items=False` default), 488-490, 671-700, 853-859, 877; operating/ingestion.py:358-363, 1336, 1373-1374, 1635; ontology/project_mc.py:53; ontology/schema/domains/mission_control.json:6, 719 and the `mission_control.brain_items` property sources; verify/live-checks/mc.sources_capture.json:103 ("Brain items ... are not part of this check"). No host enables it.
- follow-up: one removal over those files plus integrations/tests/test_mc_sources.py and ontology/tests/test_project_mc.py. Host wiring of `brain:<namespace>` grants (operating/host.py, ontology_host.py) is the GOV-13 item that makes employees see brain items.
- reversal: an operator-scope brain item that knowledge_source cannot reach under any namespace authority.
