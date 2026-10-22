"""One request bound per tool call on the case host (operating/case_tools_host.py).

``max_live_gets`` bounds every live request made while serving one tool call: mail live
reads, the live source check of an original, BC reads and their token POSTs share it
with the SharePoint re-reads. A read the bound cuts off is a ``live_read_budget``
refusal, nothing of it served. Driven through ``MCPServer`` over
``CaptureCaseHostTools`` and the real GET-only readers over the adapter tests'
in-process ``FakeSender``.

Evidence label: ``injected-transport``. No network, no credentials.
"""

from __future__ import annotations

import unittest

from integrations.tests.test_live_source_adapters import COMPANY, DRIVE, j
from ontology.core import Actor
from operating.case_runtime import CaseRuntime
from operating.case_tools_host import CaptureCaseHostTools
from operating.mcp_server import MCPServer
from operating.tests import test_case_live_citations as live_citations
from operating.tests.test_case_live_citations import (LIVE_AUDIENCE, MAIL_METADATA, QUALITY,
                                                      SP_BASE, VINCE, LiveCitationTestCase)


class BudgetCase(LiveCitationTestCase):
    def host_server(self, runtime, principals, **host) -> MCPServer:
        actor = Actor("plx", principals, clearance="restricted")
        self.host = CaptureCaseHostTools(runtime, actor, bc_books={COMPANY: "manufacturing"},
                                         **host)
        server = MCPServer(case_runtime=runtime, actor=actor, ontology=self.host)
        self.addCleanup(server.close)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "budget-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def sent(self, live) -> int:
        return live.graph_transport.requests_sent + live.bc_transport.requests_sent

    def assert_cut_off(self, read: dict, source: str) -> None:
        self.assertEqual(read["status"], "unresolvable")
        self.assertEqual([(g["reason"], g["subject"]) for g in read["gaps"]],
                         [("live_read_budget", source)])
        self.assertFalse(read.get("fields"))
        self.assertNotIn("content", read)
        self.assertNotIn("cite_as", read)


class MailAndBcReadBudgetTests(BudgetCase):
    def test_a_mail_read_cut_off_by_the_bound_serves_nothing(self):
        live = self.live()
        server = self.host_server(self.runtime(live), VINCE)
        before = live.graph_transport.requests_sent
        whole = self.ok(server, "plx.read", {"citation": MAIL_METADATA})
        needed = live.graph_transport.requests_sent - before
        self.assertEqual(whole["status"], "resolved")
        self.assertIn("cite_as", whole)
        self.assertGreater(needed, 2)
        for bound in (1, needed // 2, needed - 1):
            live = self.live()
            server = self.host_server(self.runtime(live), VINCE, max_live_gets=bound)
            before = live.graph_transport.requests_sent
            read = self.ok(server, "plx.read", {"citation": MAIL_METADATA})
            self.assertLessEqual(live.graph_transport.requests_sent - before, bound)
            if bound < needed // 2 + 1:
                self.assert_cut_off(read, "graph-mail")
            else:
                # Room for the read but not the pin of its body: the read is whole and
                # the missing pin is said, never silent.
                self.assertEqual(read["status"], "resolved")
                self.assertEqual(read["content"]["untrusted_text"],
                                 whole["content"]["untrusted_text"])
                self.assertNotIn("cite_as", read)
                self.assertIn("live_read_budget", [g["reason"] for g in read["gaps"]])
        self.assertTrue(all(method == "GET" for method, _ in self.source_calls()))

    def test_one_call_never_exceeds_the_bound_across_mail_and_bc(self):
        plain = self.server(self.runtime(self.live()), VINCE)
        company = self.ok(plain, "live.list", {"source": "bc"})["items"][0]["id"]
        bc = self.ok(plain, "live.list", {"source": "bc", "company_id": company,
                                          "entity_set": "purchaseOrders"}
                     )["items"][0]["citation"]
        actor = Actor("plx", VINCE, clearance="restricted")

        def call(bound=None) -> tuple[list, int]:
            live = self.live()
            self.host_server(self.runtime(live), VINCE,
                             **({} if bound is None else {"max_live_gets": bound}))
            host, reads = self.host, []
            host.register_tool("test.mail_then_bc", lambda arguments, _actor: reads.extend(
                host.evidence.reader.read(h, actor) for h in (MAIL_METADATA, bc))
                or {"ok": True})
            before = self.sent(live)
            host.call("test.mail_then_bc", {}, actor)
            return reads, self.sent(live) - before

        reads, needed = call()
        self.assertEqual([r["status"] for r in reads], ["resolved", "resolved"])
        reads, sent = call(needed)
        self.assertEqual(([r["status"] for r in reads], sent), (["resolved", "resolved"],
                                                                needed))
        # One request short: the mail read is served, the BC read (on its own
        # transport) shares the bound and is cut off, and nothing of it is served.
        reads, sent = call(needed - 1)
        self.assertLessEqual(sent, needed - 1)
        self.assertEqual(reads[0]["status"], "resolved")
        self.assertEqual((reads[1]["status"], reads[1]["reason"]),
                         ("unresolvable", "live_read_budget"))
        self.assertFalse(reads[1].get("fields"))
        self.assertEqual(reads[1]["gaps"][0]["subject"], "bc")
        self.assertTrue(all(method == "GET" for method, _ in self.source_calls()))


class OriginalReadBudgetTests(BudgetCase):
    """A stored original's every read re-checks its SharePoint item live (metadata and
    permissions); those checks are inside the call's bound."""

    def attach(self) -> tuple[CaseRuntime, str]:
        self.sender.routes[f"{SP_BASE}/items/AO1/permissions"] = lambda url: j(
            200, live_citations.SharePointOriginalAccessTests.PERMS)
        runtime = self.runtime(self.live(
            LIVE_AUDIENCE, identities={"staff:quality": ["ricardo@petralabx.com"]},
            source_acl_required=[f"sharepoint:{DRIVE}:*"]))
        server = self.server(runtime, QUALITY)
        case_id = self.open_case(server)
        return runtime, self.ok(server, "plx.case_attach_original", {
            "case_id": case_id, "drive_id": DRIVE, "item_id": "AO1"})["original"]["citation"]

    def test_an_original_read_cut_off_by_the_bound_serves_nothing(self):
        runtime, handle = self.attach()
        live = runtime.live

        def read(bound=None) -> tuple[dict, int]:
            server = self.host_server(runtime, QUALITY,
                                      **({} if bound is None else {"max_live_gets": bound}))
            before = self.sent(live)
            return self.ok(server, "plx.read", {"citation": handle}), self.sent(live) - before

        whole, needed = read()
        self.assertEqual(whole["status"], "resolved")
        self.assertIn("PLANNED", whole["content"]["untrusted_text"])
        self.assertTrue(whole["extractions"])
        # The read checks the item three times (read, digest, text), the extraction
        # list once more: two GETs (metadata, permissions) each.
        self.assertEqual(needed, 8)
        for bound in (0, 5):
            cut, sent = read(bound)
            self.assertLessEqual(sent, bound)
            self.assert_cut_off(cut, "original")
        # Room for the read but not its extraction list: the read is whole and the
        # missing list is said, never silent.
        partial, sent = read(6)
        self.assertEqual((partial["status"], sent), ("resolved", 6))
        self.assertIn("PLANNED", partial["content"]["untrusted_text"])
        self.assertNotIn("extractions", partial)
        self.assertIn("live_read_budget", [g["reason"] for g in partial["gaps"]])
        self.assertTrue(all(method == "GET" for method, _ in self.source_calls()))


if __name__ == "__main__":
    unittest.main()
