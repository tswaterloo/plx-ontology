"""Module size: no hand-written Python module over 2,000 lines.

A module past the limit is split into cohesive siblings (``<name>_<area>.py``). Exempt:
generated files (rewritten by their generator) and files pinned whole-file by a step,
derivation or memo digest (``ontology/project.py`` ``derivation_files``,
``text_index._memo_code``): a split there re-keys stored work, so it waits for a
by-definition digest. ``OVER_LIMIT`` lists the modules already past the limit with a cap
each may not grow past (its size at 2026-11-15 rounded up, with a little room for work in
flight); lower a cap when a module shrinks and delete its entry once it is under the limit.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 2000
GENERATED = {"operating/typed_graph/models.py", "operator_api/models.py"}
PINNED = {"integrations/document_understanding.py", "ontology/document_fields.py",
          "ontology/mentions.py", "ontology/project.py", "ontology/store.py"}
OVER_LIMIT = {
    "devloop/tests/test_live_verify.py": 3250,
    "evals/score.py": 3500,
    "evals/tests/test_score.py": 3000,
    "integrations/bc_read.py": 3500,
    "integrations/capture_mail.py": 7500,
    "integrations/capture_sharepoint.py": 4750,
    "integrations/capture_teams.py": 3500,
    "integrations/graph_read.py": 2750,
    "integrations/live_source_guard.py": 3250,
    "integrations/source_mirror.py": 4750,
    "integrations/tests/test_bc_capture.py": 2500,
    "integrations/tests/test_source_mirror.py": 3000,
    "ontology/delta.py": 3750,
    "ontology/identity_scoring.py": 2500,
    "ontology/pg_step_inplace.py": 3250,
    "ontology/project_incremental.py": 2750,
    "ontology/schema.py": 2250,
    "ontology/step_migrate.py": 3250,
    "ontology/tests/test_document_fields.py": 2750,
    "operating/action_path.py": 3750,
    "operating/agent_session.py": 4000,
    "operating/ai_watcher.py": 2750,
    "operating/answer_gate.py": 3000,
    "operating/business_documents.py": 2250,
    "operating/case_runtime.py": 2250,
    "operating/case_tools_host.py": 2500,
    "operating/case_workspace_live.py": 3000,
    "operating/cases.py": 2750,
    "operating/customer_profit.py": 2250,
    "operating/fast_answer.py": 5000,
    "operating/host.py": 4000,
    "operating/impact_chain.py": 2250,
    "operating/landed_cost_plan.py": 3000,
    "operating/landed_cost_portal.py": 2250,
    "operating/mcp_server.py": 2500,
    "operating/ontology_tools.py": 2750,
    "operating/ontology_tools_aggregate.py": 2750,
    "operating/ontology_tools_evidence.py": 4000,
    "operating/ontology_tools_traverse.py": 5250,
    "operating/ops_alarms.py": 2250,
    "operating/playbook_eval.py": 2500,
    "operating/po_status.py": 3250,
    "operating/proactive_engine.py": 2750,
    "operating/proactive_events.py": 2250,
    "operating/query_understanding.py": 2750,
    "operating/tests/test_action_path.py": 3250,
    "operating/tests/test_autopilot_replay.py": 2250,
    "operating/tests/test_briefing.py": 2250,
    "operating/tests/test_case_host_sharepoint.py": 3000,
    "operating/tests/test_case_workspace_live.py": 2250,
    "operating/tests/test_claim_checks.py": 4500,
    "operating/tests/test_fast_answer.py": 3000,
    "operating/tests/test_goal2_e2e.py": 2750,
    "operating/tests/test_ingest_forever.py": 2250,
    "operating/tests/test_mcp_case_tools.py": 2500,
    "operating/tests/test_playbook_eval.py": 2250,
    "operating/tests/test_proactive_engine.py": 2500,
    "operating/tests/test_use_case_intake.py": 2500,
    "operator_api/app.py": 2250,
    "operator_api/backend.py": 3800,
    "operator_api/tests/test_backend_fixture.py": 2500,
    "operator_api/workspace/object_workspace.py": 3750,
    "scripts/case_originals.py": 3000,
    "scripts/eval_dev_baseline.py": 2250,
    "scripts/eval_live_ops.py": 2250,
    "scripts/eval_oracle_dev.py": 2250,
    "scripts/eval_unfamiliar_sample.py": 2500,
    "scripts/ontology_projection_probe.py": 3000,
    "tests/test_case_originals.py": 2500,
    "tests/test_chunks_embed.py": 2500,
    "tests/test_eval_oracle_dev.py": 2500,
    "tests/test_eval_unfamiliar_sample.py": 3750,
    "tests/test_ontology_tools.py": 2500,
    "tests/test_ontology_tools_evidence.py": 2500,
    "tests/test_ontology_tools_investigate.py": 3250,
    "tests/test_ontology_tools_process.py": 2500,
    "tests/test_ontology_tools_traverse.py": 2750,
    "tests/test_process_three_way.py": 2250,
    "verify/live_verify.py": 2750,
}


def python_files() -> list[str]:
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"], capture_output=True,
                            text=True)
    if listed.returncode == 0 and listed.stdout.strip():
        return listed.stdout.split()
    return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
            if not {"node_modules", ".git", ".worktrees"} & set(p.parts)]


def lines(rel: str) -> int:
    with open(ROOT / rel, encoding="utf-8") as handle:
        return sum(1 for _ in handle)


class ModuleSizeTests(unittest.TestCase):
    def test_no_module_grows_past_its_cap(self):
        files = [rel for rel in python_files() if (ROOT / rel).is_file()]
        self.assertGreater(len(files), 500)   # the listing found the tree
        over = {}
        for rel in files:
            if rel in GENERATED or rel in PINNED:
                continue
            size = lines(rel)
            if size > OVER_LIMIT.get(rel, LIMIT):
                over[rel] = (size, OVER_LIMIT.get(rel, LIMIT))
        self.assertEqual(over, {}, "split these modules: (lines, cap)")

    def test_every_listed_module_exists_and_is_still_over_the_limit(self):
        # a stale entry would let a module grow back past the limit unnoticed
        stale = sorted(rel for rel in OVER_LIMIT
                       if not (ROOT / rel).is_file() or lines(rel) <= LIMIT)
        self.assertEqual(stale, [], "delete these OVER_LIMIT entries")
        self.assertEqual(sorted(rel for rel in GENERATED | PINNED
                                if not (ROOT / rel).is_file()), [])


if __name__ == "__main__":
    unittest.main()
