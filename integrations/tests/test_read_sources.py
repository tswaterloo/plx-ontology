from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

from integrations.http import SourceUnavailable
from integrations.mc import MissionControlReadClient


class MissionControlReadClientTests(unittest.TestCase):
    def test_documented_get_envelopes_and_repo_binding(self):
        calls = []

        def transport(url, headers):
            calls.append((url, headers))
            actor = {"repo": "petralabx/PLX_MC", "servicePrincipalId": "sp_mcp_codex"}
            if "/self-check" in url:
                data = {"ok": True, "boringGateMet": False}
            elif "/tasks?" in url:
                data = {"tasks": [{"id": "TASK-477", "title": "Knowledge"}], "total": 1}
            else:
                data = {"tasks": [{"id": "TASK-477"}], "buckets": [], "lastSweep": None}
            return {"data": data, "meta": {"actor": actor, "requestId": "r1"}}

        client = MissionControlReadClient("secret", "operator@example.com", "petralabx/PLX_MC", transport=transport)
        self.assertTrue(client.self_check()["data"]["ok"])
        self.assertEqual(client.search_tasks(query="TASK-477")["data"]["total"], 1)
        self.assertEqual(client.context(task_ids=["TASK-477"], full=True)["data"]["tasks"][0]["id"], "TASK-477")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(h["X-MC-Repo"] == "petralabx/PLX_MC" for _, h in calls))
        self.assertTrue(all(h["X-API-Key"] == "secret" for _, h in calls))
        self.assertEqual(parse_qs(urlparse(calls[2][0]).query)["taskIds"], ["TASK-477"])

    def test_fails_closed_on_wrong_actor_repo_or_missing_credentials(self):
        with self.assertRaises(SourceUnavailable):
            MissionControlReadClient(None, "operator@example.com", "petralabx/PLX_MC")
        client = MissionControlReadClient("secret", "operator@example.com", "petralabx/PLX_MC",
                                          transport=lambda *_: {"data": {"tasks": [], "total": 0},
                                                                "meta": {"actor": {"repo": "petralabx/plx-customer-portal"}}})
        with self.assertRaises(SourceUnavailable):
            client.search_tasks()

    def test_exact_task_evidence_refuses_usable_checkout_id(self):
        def response(checkout):
            return {"data": {"taskId": "TASK-477",
                "task": {"id": "TASK-477", "title": "Knowledge"},
                "events": [], "checkouts": [{"checkoutRef": checkout}]},
                "meta": {"actor": {"repo": "petralabx/PLX_MC",
                                   "servicePrincipalId": "sp_mcp_codex"}}}
        client = MissionControlReadClient("secret", "operator@example.com",
            "petralabx/PLX_MC", transport=lambda *_: response("dsp_…1234"))
        self.assertEqual(client.get_task("TASK-477")["data"]["checkouts"][0]
                         ["checkoutRef"], "dsp_…1234")
        client = MissionControlReadClient("secret", "operator@example.com",
            "petralabx/PLX_MC", transport=lambda *_: response("dsp_abcdefgh1234"))
        with self.assertRaises(SourceUnavailable):
            client.get_task("TASK-477")


if __name__ == "__main__":
    unittest.main()
