"""The agent session's first reads (``operating.session_prefetch``): the records a question
names are found by the identifier grammar and the resolver, and each one found as the one
record of its mention is read by what it is, never by what the question asks."""

from __future__ import annotations

import unittest

from operating import session_prefetch as sp


def _find(*objects, status="found"):
    return {"status": status, "candidates": [{"object": o} for o in objects]}


PO = {"object_id": "PurchaseOrder:portal:ZX-9001", "object_type": "PurchaseOrder"}
PO2 = {"object_id": "PurchaseOrder:bc:ZX-9001", "object_type": "PurchaseOrder"}
FIRM = {"object_id": "Customer:portal:77", "object_type": "Customer"}


class SessionPrefetchTests(unittest.TestCase):
    def test_the_named_records_are_looked_up_however_the_question_is_worded(self):
        for question in ("when was PO#90412 supposed to land",
                         "who set up PO 90412 and is it late?",
                         "PO90412: story, next steps, whom to chase"):
            with self.subTest(question=question):
                reads = sp.first_reads(question)
                finds = [a for name, a in reads if name == "plx.find" and "query" in a]
                self.assertEqual(len(finds), 1)
                self.assertIn("90412", finds[0]["query"])
                self.assertEqual(reads[-1], ("plx.find", {"mode": "entities",
                                                          "text": question}))
        # a question naming no identifier still has its names resolved
        self.assertEqual(sp.first_reads("open orders for Zanzibar Soap Co?"),
                         [("plx.find", {"mode": "entities",
                                        "text": "open orders for Zanzibar Soap Co?"})])
        self.assertEqual(sp.first_reads("  "), [])

    def test_each_record_found_is_read_by_what_it_is(self):
        entities = {"entities": [
            {"status": "match", "object_type": "Customer",
             "records": [{"status": "match", "object": FIRM}]},
            {"status": "candidate", "object_type": "Customer",
             "records": [{"status": "candidate", "object": {"object_id": "Customer:bc:1"}}]}]}
        done = [("plx.find", {"query": "PO 90412"}, _find(PO)),
                ("plx.find", {"mode": "entities", "text": "q"}, entities)]
        self.assertEqual(sp.follow_reads(done), [
            ("plx.object", {"object_id": PO["object_id"]}),
            ("plx.process", {"from": PO["object_id"]}),     # a process runs on a PO
            ("plx.object", {"object_id": FIRM["object_id"]})])
        # several records of one mention: the session sees them and chooses
        self.assertEqual(sp.follow_reads([("plx.find", {"query": "ZX-9001"},
                                           _find(PO, PO2, status="ambiguous"))]), [])
        self.assertEqual(sp.follow_reads([("plx.find", {"query": "x"}, {"error": {}})]), [])

    def test_process_roots_are_the_schemas(self):
        self.assertIn("PurchaseOrder", sp.process_root_types())
        self.assertNotIn("Customer", sp.process_root_types())


if __name__ == "__main__":
    unittest.main()


class KnownRootsTests(unittest.TestCase):
    def test_records_the_ask_found_are_read_without_looking_them_up_again(self):
        roots = [{**PO, "same_as": [PO2["object_id"]]}, FIRM, {"object_id": None}]
        reads = sp.root_reads(roots)
        self.assertNotIn("plx.find", [name for name, _ in reads])
        cards = [a["object_id"] for name, a in reads if name == "plx.object"]
        self.assertEqual(cards, [PO["object_id"], PO2["object_id"], FIRM["object_id"]])
        if "PurchaseOrder" in sp.process_root_types():
            self.assertIn(("plx.process", {"from": PO["object_id"]}), reads)
        self.assertEqual(sp.root_reads([]), [])

    def test_the_agent_told_known_roots_starts_from_their_cards(self):
        from operating.agent_session import ClaudeCodeAgent
        agent = ClaudeCodeAgent()
        agent.known_roots = [FIRM]
        told = agent.for_question("open orders for the customer?")
        self.assertEqual(told.first_reads, [("plx.object", {"object_id": FIRM["object_id"]})])
        self.assertIsNone(told.follow_reads)
        self.assertEqual(agent.first_reads, [])      # the agent itself is unchanged

    def test_what_the_ask_looked_up_is_not_looked_up_again(self):
        question = "how many lines of PO 90412 and ZX-9001 for Zanzibar Soap Co?"
        everything = sp.first_reads(question)
        queries = [a["query"] for name, a in everything if "query" in a]
        self.assertEqual(len(queries), 2)
        # the Ask looked up PO 90412 (written its own way) and ran the resolver
        looked = {"mentions": ["po#90412"], "entities": True}
        reads = sp.first_reads(question, looked)
        self.assertEqual([a.get("query") for _n, a in reads],
                         [q for q in queries if "90412" not in q])
        self.assertNotIn("entities", [a.get("mode") for _n, a in reads])
        # the resolver failed at the Ask: it runs in the session
        self.assertIn(("plx.find", {"mode": "entities", "text": question}),
                      sp.first_reads(question, {"mentions": [], "entities": False}))
        self.assertEqual(sp.first_reads(question, None), everything)
        # through the session's agent: nothing it looked up, and no follow-up read of it
        from operating.agent_session import ClaudeCodeAgent
        agent = ClaudeCodeAgent()
        agent.looked_up = {"mentions": [q for q in queries], "entities": True}
        told = agent.for_question(question)
        self.assertEqual(told.first_reads, [])
        self.assertIsNone(told.follow_reads)
        self.assertEqual(ClaudeCodeAgent().for_question(question).first_reads, everything)

    def test_the_session_is_told_what_was_looked_up_from_the_asks_own_lookup(self):
        from operator_api.backend import looked_up
        from operator_api.preread import brief
        found = {"roots": [{"mention": "R-0162", "object_id": "Item:bc:R-0162",
                            "found_by": "identifier"},
                           {"mention": "Zanzibar Soap", "object_id": FIRM["object_id"],
                            "found_by": "resolver"}],
                 "ambiguous": [],
                 "unfound": [{"mention": "PO 90412", "reason": "not_found"},
                             {"mention": "ZX-9001", "reason": "mentions_bounded"}],
                 "entities": []}
        # a mention bounded out was never looked up, nor is a resolved name an identifier
        self.assertEqual(looked_up(found), {"mentions": ["R-0162", "PO 90412"],
                                            "entities": True})
        self.assertFalse(looked_up({**found, "entities": None})["entities"])
        self.assertIsNone(looked_up(None))
        told = brief(None, [], unfound=found["unfound"])
        self.assertIn('"PO 90412"', told)
        self.assertIn("never say it does not exist", told)
        self.assertNotIn("ZX-9001", told)
        self.assertEqual(brief(None, [], unfound=[]), "")

    def test_an_ask_lookup_that_timed_out_is_looked_up_again_in_the_session(self):
        from operating.agent_session import ClaudeCodeAgent
        from operator_api.backend import looked_up
        from operator_api.preread import brief
        question = "where is PO 90412 at?"
        for reason in ("timeout", "internal_error", "invalid_input", "not_found"):
            with self.subTest(reason=reason):
                unfound = {"mention": "PO 90412", "reason": reason}
                if reason == "not_found":   # found nothing in the time it had
                    unfound["gaps"] = [{"state": "unknown", "reason": "time_budget"}]
                found = {"roots": [], "ambiguous": [], "unfound": [unfound],
                         "entities": []}
                looked = looked_up(found)
                self.assertEqual(looked["mentions"], [])
                agent = ClaudeCodeAgent()
                agent.looked_up = looked
                finds = [a.get("query") for name, a in
                         agent.for_question(question).first_reads if name == "plx.find"]
                self.assertEqual(len(finds), 1)
                self.assertIn("90412", finds[0])
                # the brief claims no absence for a lookup that never completed
                self.assertEqual(brief(None, [], unfound=found["unfound"]), "")


class WarmHelperTests(unittest.TestCase):
    def test_a_sessions_helpers_are_taken_warm_from_the_readers_spares(self):
        from pathlib import Path
        import tempfile
        from unittest import mock
        from operating import agent_session as runner

        class Host:
            ticket_env = "PLX_AGENT_SESSION_TICKET"
            n = 0

            def mcp_server(self):
                Host.n += 1   # each server its own made-up ticket
                return {"command": "python3", "args": ["-m", "host"], "cwd": None,
                        "env": {self.ticket_env: f"ticket-{Host.n}"}}

            def spare_identity(self):
                return ["reader-zx"]

        class Spares:
            def __init__(self):
                self.held = {}

            def start(self, key, command, env, cwd):
                self.envs = getattr(self, "envs", []) + [env]
                self.held[key] = runner.Spare(mock.Mock(), Path(work) / f"d{len(self.held)}",
                                              Path(work), 1e12)

            def take(self, key):
                return self.held.pop(key, None)

        work = tempfile.mkdtemp()
        spares = Spares()
        agent = runner.ClaudeCodeAgent(helpers=2)
        tools = [{"name": "plx.object", "read_only": True}]
        with mock.patch.object(runner, "SPARES", spares):
            agent._start_spare(Host())
            self.assertEqual(len(spares.held), 3)   # the primary and two helpers
            tickets = [e.get("PLX_AGENT_SESSION_TICKET") for e in spares.envs]
            self.assertTrue(all(tickets))
            self.assertEqual(len(set(tickets)), 3)   # each host its own single-use ticket
            taken = []
            server_env = agent._server_env(Host().mcp_server(), None)   # as a session's
            pool = agent._pool(Host(), tools, ["plx.object"], server_env, lambda: 123.0,
                               taken, Host().mcp_server())
            first, second = pool.start(), pool.start()
        self.assertIsNot(first, second)
        self.assertEqual(len(taken), 2)
        self.assertEqual([s.deadline.read_text() for s in taken], ["123.000"] * 2)
        self.assertEqual(len(spares.held), 1)       # the primary's is left for the primary
