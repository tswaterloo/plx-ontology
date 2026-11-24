"""The coverage ledger (Ask plan A3, ``operator_api.ask_legs``): the parts the session
declares are ticked only by reads that ran (HARD20 h02, h10: Teams never searched)."""
import json
import unittest

from operator_api import ask_legs as al

VIEWS = ["emails", "purchase_orders", "teams_messages"]


def legs(*parts):
    return {"type": "tool_call", "name": "mcp__plx__plx_legs",
            "arguments": {"legs": [{"part": p, "sources": s} for p, s in parts]},
            "result": json.dumps({"legs": len(parts)})}


def sql(views, rows):
    return {"type": "tool_call", "name": "plx.sql", "arguments": {"sql": "x"},
            "result": json.dumps({"result": "sql://" + "a" * 20, "views": views,
                                  "profile": {"rows": rows}, "rows": []})}


def search(sources=None, hits=2, error=False):
    out = {"error": {"code": "x"}} if error else {"documents": [{}] * hits, "searched": {
        "sources": sources or list(al.SEARCH_SOURCES)}}
    return {"type": "tool_call", "name": "plx.search",
            "arguments": {"query": "croda", **({"sources": sources} if sources else {})},
            "result": json.dumps(out)}


def opened(*citations):
    return {"type": "tool_call", "name": "plx.open", "arguments": {"citations": ["r1"]},
            "result": json.dumps({"items": [{"citation": c, "text": "x"} for c in citations]})}


class Ledger(unittest.TestCase):
    def test_an_open_ticks_only_the_sources_of_what_it_opened(self):
        """A3b-1 (h02: Teams never read passed unseen when any open ticked every source)."""
        book = al.ledger([legs(("mail", ["graph_mail"]), ("Teams", ["teams"])),
                          opened("graph-mail://m/1")])
        self.assertEqual([leg.reads for leg in book.legs], [1, 0])
        book = al.ledger([legs(("Teams", ["teams"])), opened("teams-chat://c/9")])
        self.assertEqual(book.legs[0].reads, 1)

    def test_a_search_ticks_what_it_says_it_searched_and_no_legs_is_asked_for(self):
        """A3b-2/3: a search result's own sources tick, never its arguments; a session that
        read and declared no parts is asked to list them (the one repair)."""
        said = {"type": "tool_call", "name": "plx.search", "arguments": {},
                "result": json.dumps({"documents": [], "searched": {"sources": ["graph_mail"]}})}
        book = al.ledger([legs(("Teams", ["teams"])), said])
        self.assertEqual(book.legs[0].reads, 0)
        self.assertEqual(al.repair_part(al.ledger([said]), [said]), al.NO_LEGS)
        self.assertIsNone(al.repair_part(al.ledger([]), []))

    def test_the_ledger_is_one_pass_over_the_events(self):
        import time
        many = [sql(["emails"], 1)] * 60
        def took(events):
            t = time.perf_counter()
            al.ledger([legs(*[(f"p{i}", ["emails"]) for i in range(12)]), *events])
            return time.perf_counter() - t
        self.assertLess(took(many), 3 * max(took(many[:1]), 1e-4) * 60)
        self.assertEqual(al.ledger([legs(("p", ["emails"])), *many]).legs[0].reads, 60)
        self.assertEqual(len(al.ledger([legs(("p", ["emails"]))]).payload()), 1)

    def test_each_part_is_ticked_only_by_a_read_of_its_sources(self):
        events = [legs(("late POs", ["purchase_orders"]), ("angry mail", ["graph_mail"]),
                       ("Teams", ["teams"])),
                  sql(["purchase_orders"], 2), search(["graph_mail"], 3),
                  search(["teams"], error=True)]
        book = al.ledger(events)
        self.assertEqual([(leg.part, leg.reads, leg.found) for leg in book.legs],
                         [("late POs", 1, 2), ("angry mail", 1, 3), ("Teams", 0, 0)])
        self.assertIn("Teams (teams)", al.repair_part(book))
        self.assertEqual([(p["part"], p["reads"]) for p in book.payload()],
                         [("late POs", 1), ("angry mail", 1), ("Teams", 0)])
        # a search over every source reads them all; a read before the legs counts too
        book = al.ledger([search(), *events])
        self.assertIsNone(al.repair_part(book))
        self.assertEqual(book.unread, [])

    def test_no_legs_declared_is_nothing_to_tick_and_the_last_declaration_counts(self):
        self.assertEqual(al.ledger([sql(["emails"], 1)]).legs, [])
        book = al.ledger([legs(("a", ["emails"])), legs(("b", ["teams"]))])
        self.assertEqual([leg.part for leg in book.legs], ["b"])

    def test_the_rows_are_built_from_the_reads_when_no_parts_were_declared(self):
        """Step 6 (spec 15.3): an answer with no plx.legs call still carries a row per
        source it read, with the window each read covered; a source a declared part lists
        gets no second row; a read without a window says so, never a guessed one."""
        def dated(source, since=None, until=None, hits=3):
            out = {"documents": [{}] * hits, "searched": {"sources": [source]}}
            args = {"query": "croda", **({"since": since} if since else {}),
                    **({"until": until} if until else {})}
            return {"type": "tool_call", "name": "plx.search", "arguments": args,
                    "result": json.dumps(out)}
        events = [dated("teams", since="2026-09-01"), dated("graph_mail", "2026-01-01",
                                                            "2026-06-30", 4),
                  dated("graph_mail", since="2026-09-01", hits=2), sql(["purchase_orders"], 5)]
        rows = {r["sources"][0]: r for r in al.ledger(events).payload()}
        self.assertEqual(set(rows), {"teams", "graph_mail", "purchase_orders"})
        self.assertEqual((rows["teams"]["period"], rows["teams"]["reads"],
                          rows["teams"]["found"]), ("since 2026-09-01", 1, 3))
        self.assertEqual((rows["graph_mail"]["period"], rows["graph_mail"]["reads"],
                          rows["graph_mail"]["found"]),
                         ("2026-01-01 to 2026-06-30; since 2026-09-01", 2, 6))
        self.assertEqual(rows["purchase_orders"]["period"], al.NO_WINDOW)
        self.assertEqual(al.ledger([dated("teams")]).payload()[0]["period"], "any date")
        # a declared part listing teams owns that source's row; the other reads still show
        book = al.ledger([legs(("Teams", ["teams"])), *events])
        self.assertEqual([r["part"] for r in book.payload()],
                         ["Teams", "Read: graph_mail", "Read: purchase_orders"])
        self.assertEqual(al.ledger([]).payload(), [])

    def test_read_rows_at_1_10_60_reads_lose_and_double_nothing(self):
        for n in (1, 10, 60):
            rows = al.ledger([sql(["emails"], 2)] * n).payload()
            self.assertEqual([(r["reads"], r["found"]) for r in rows], [(n, 2 * n)])

    def test_an_unknown_source_is_refused_never_dropped(self):
        got = al.call({"legs": [{"part": "x", "sources": ["slack"]}]}, VIEWS)
        self.assertEqual(got["error"]["code"], "invalid_request")
        self.assertIn("slack", got["error"]["message"])
        self.assertEqual(al.call({"legs": [{"part": "x", "sources": ["teams"]}]}, VIEWS)["legs"], 1)
        enum = al.spec(VIEWS)["inputSchema"]["properties"]["legs"]["items"]["properties"][
            "sources"]["items"]["enum"]
        self.assertTrue({"emails", "graph_mail", "teams"} <= set(enum))


if __name__ == "__main__":
    unittest.main()
