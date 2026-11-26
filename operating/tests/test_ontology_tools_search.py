"""plx.search and plx.open (``operating.ontology_tools_search``): the host's own
plx.documents / plx.read for the session's reader, cut to what the Ask needs;
unknown arguments fail closed."""

from __future__ import annotations

import json
import unittest

from operating import ontology_tools_search as s
from operating import mcp_server_catalog as catalog


class OpenTest(unittest.TestCase):
    def test_reads_through_the_hosts_own_read(self):
        calls = []

        def read(arguments):
            calls.append(arguments)
            return {"status": "resolved", "text": {"untrusted_text": "Order 4411 ships Friday"}}
        got = s.open_one(read, {"citation": "original://m/1", "text_offset": 4000})
        self.assertEqual(got["status"], "resolved")
        # the window asked for (one plx.read page; a longer window is read on in pages
        # once the gates pass)
        self.assertEqual(calls, [{"citation": "original://m/1", "text_offset": 4000,
                                  "max_chars": min(s.OPEN_MAX_CHARS, s.CHECK_PAGE_CHARS)}])

    def test_a_long_mail_from_the_guarded_views_is_shown_in_windows(self):
        # the comms door decided Freightcom on the whole text at build (ai_ok): a mail past
        # OPEN_MAX_CHARS is shown window by window, with no read of the old path
        def comms(arguments):
            start = arguments.get("text_offset", 0)
            return {"citation": "graph-mail://u/m1", "kind": "email",
                    "text": {"untrusted_text": "assay " * 10, "truncated": True},
                    "text_offset": start, "next_offset": start + s.OPEN_MAX_CHARS}

        def read(arguments):
            raise AssertionError("a guarded views' row is never read again")
        for args in ({"citation": "graph-mail://u/m1"},
                     {"citation": "graph-mail://u/m1", "text_offset": s.OPEN_MAX_CHARS}):
            got = s.open_one(read, args, comms)
            self.assertEqual(got["text"]["untrusted_text"], "assay " * 10, args)

    def test_unexpected_input_fails_closed_without_a_read(self):
        def read(arguments):
            raise AssertionError("no read for a bad call")
        for bad in ([], None, {}, {"citation": ""}, {"citation": 5},
                    {"citation": "original://m/1", "sql": "select 1"},
                    {"citation": "original://m/1", "text_offset": -1},
                    {"citation": "original://m/1", "text_offset": True},
                    {"citation": "original://m/1", "max_chars": 10 ** 9}):
            self.assertEqual(s.open_one(read, bad)["error"]["code"], "invalid_input", bad)

    def test_several_citations_open_each_through_its_own_gates_in_order(self):
        # 11-21 (Ask listed a supplier's mails and concluded unread): one plx.open reads
        # several, each exactly as one open reads it; near words go to the mail leg only
        comms_calls, read_calls = [], []

        def comms(arguments):
            comms_calls.append(arguments)
            c = arguments["citation"]
            if c.startswith("original://"):
                return {"not_comms": True}
            return {"citation": c, "kind": "email",
                    "text": {"untrusted_text": f"ships 2026-12-0{len(comms_calls)}"}}

        def read(arguments):
            read_calls.append(arguments)
            assert "near" not in arguments, "plx.read takes no near"
            return {"status": "resolved", "text": {"untrusted_text": "COA lot 77"}}
        shown = {"graph-mail://r.aaaaaaaaaa": "graph-mail://u/m1"}
        many = ["graph-mail://r.aaaaaaaaaa", "graph-mail://u/m2", "original://m/9",
                "graph-mail://u/fc", "graph-mail://r.bbbbbbbbbb"]
        got = s.open_one(read, {"citations": many, "near": "ship date, promised"}, comms,
                         resolve=lambda c: shown.get(c, None if "r." in c else c))
        items = got["opened"]
        self.assertEqual([i["citation"] for i in items],
                         ["graph-mail://u/m1", "graph-mail://u/m2", "original://m/9",
                          "graph-mail://u/fc", "graph-mail://r.bbbbbbbbbb"])
        self.assertIn("ships", items[0]["text"]["untrusted_text"])
        self.assertEqual(items[2]["status"], "resolved")                 # the old path
        self.assertIn("text", items[3])                                  # Freightcom reads like any
        self.assertEqual(items[4]["error"]["code"], "not_found")         # ref not shown
        self.assertEqual({tuple(c.get("near") or ()) for c in comms_calls},
                         {("ship", "date", "promised")})
        self.assertEqual(got["window_chars"], min(s.MANY_CHARS, s.MANY_TEXT // len(many)))
        self.assertTrue(all(c["max_chars"] == got["window_chars"]
                            for c in comms_calls + read_calls))
        self.assertNotIn("graph-mail://r.bbbbbbbbbb", [c["citation"] for c in comms_calls])

    def test_several_citations_cardinality_and_no_time_cut(self):
        def comms(arguments):
            return {"citation": arguments["citation"], "text": {"untrusted_text": "ok"}}
        for n in (1, 10, s.MANY_MAX):
            asked = [f"graph-mail://u/m{i}" for i in range(n)]
            got = s.open_one(None, {"citations": asked}, comms)["opened"]
            self.assertEqual([g["citation"] for g in got], asked, n)   # none lost or doubled
        # no time cut: a slow item is still waited for and every item is opened
        ticks = iter(range(0, 10_000, 100))
        opened = []
        got = s.open_one(None, {"citations": ["graph-mail://u/a", "graph-mail://u/b",
                                              "graph-mail://u/c"]},
                         lambda a: opened.append(a) or comms(a), clock=lambda: next(ticks))
        self.assertEqual([g.get("error") for g in got["opened"]], [None, None, None])
        self.assertEqual(len(opened), 3)

    def test_several_citations_fit_one_response(self):
        # 11-22: 8 mails at 2,500 characters each failed the whole call for its size; the
        # windows now share one text budget and what still does not fit comes back not
        # opened, by the citation sent, never lost or doubled
        sent = []

        def comms(arguments):
            sent.append(arguments["max_chars"])
            c = arguments["citation"]
            return {"citation": c, "kind": "email", "title": {"untrusted_text": "t" * 400},
                    "text": {"untrusted_text": "\u00e9" * arguments["max_chars"]},
                    "fields_served": [{"field": "row.body_text", "version": "v",
                                       "offset": 0, "chars": arguments["max_chars"],
                                       "total_chars": 99999, "at": 0}]}
        for n in (1, 6, 8, s.MANY_MAX):
            sent.clear()
            asked = [f"graph-mail://u/m{i}" for i in range(n)]
            got = s.open_one(None, {"citations": asked}, comms)
            items = got["opened"]
            # a page, then the rest handed back whole: nothing lost, nothing opened twice
            self.assertEqual([i["citation"] for i in items] + got.get("next_citations", []),
                             asked, n)
            if n == 1:
                self.assertNotIn("next_citations", got, n)
            self.assertLessEqual(sum(sent), s.MANY_TEXT, n)
            self.assertTrue(all(m == got["window_chars"] for m in sent), n)
            self.assertLessEqual(s._encoded(got), s.MANY_BYTES + 2000, n)
            self.assertIn("text", items[0])
            for item in items:
                self.assertIn("text", item)   # every item of a page is opened
        # mail text full of line breaks and quotes (escaped twice in the host's text): the
        # result as the host sends it fits its cap
        from operating import mcp_server_evidence as me
        from operating.mcp_server_results import _envelope_size

        def crlf(a):
            got = comms(a)
            got["text"] = {"untrusted_text": ('"x"\r\n' * a["max_chars"])[:a["max_chars"]]}
            return got
        for n in (8, s.MANY_MAX):
            value = s.open_one(None, {"citations": [f"graph-mail://u/q{i}" for i in range(n)]},
                               crlf)
            envelope = {"content": [{"type": "text", "text": json.dumps(
                value, ensure_ascii=False, separators=(",", ":"))}],
                "_meta": {"freshness": "x" * 4000}}
            self.assertLessEqual(_envelope_size(envelope), me.ASK_RESULT_BYTES, n)
            # shown whole by the client: its JSON text within CLIENT_SHOWN_CHARS
            self.assertLessEqual(len(envelope["content"][0]["text"]), me.CLIENT_SHOWN_CHARS, n)
        # a result that still does not fit (a long envelope): the rest are handed back to send
        big = lambda a: {**comms(a), "title": {"untrusted_text": "x" * int(s.MANY_BYTES * 0.6)}}  # noqa: E731
        got = s.open_one(None, {"citations": ["graph-mail://u/a", "graph-mail://u/b",
                                              "graph-mail://u/c"]}, big)
        self.assertEqual(len(got["opened"]), 1)
        self.assertIn("text", got["opened"][0])
        self.assertEqual(got["next_citations"], ["graph-mail://u/b", "graph-mail://u/c"])

    def test_a_long_text_opens_as_one_window_read_in_pages_within_the_read_bound(self):
        """A text longer than one plx.read holds opens as one window of up to OPEN_MAX_CHARS
        (owner 11-22: Ask reads whole texts), every read within plx.read's own bound, the
        whole text checked first; past the window it says where to read on."""
        body = "".join(f"line {i:06d} " for i in range(s.OPEN_MAX_CHARS // 6))
        calls = []

        def read(arguments):
            calls.append(arguments)
            self.assertLessEqual(arguments["max_chars"], s.CHECK_PAGE_CHARS)
            start = arguments.get("text_offset", 0)
            window = body[start:start + arguments["max_chars"]]
            more = start + arguments["max_chars"] < len(body)
            return {"status": "resolved",
                    "content": {"untrusted_text": window, **({"truncated": True} if more else {})},
                    "text_window": {"offset": start,
                                    **({"next_offset": start + len(window)} if more else {})}}
        got = s.open_one(read, {"citation": "original://m/9"})
        self.assertEqual(got["content"]["untrusted_text"], body[:s.OPEN_MAX_CHARS])
        self.assertTrue(got["content"]["truncated"])
        self.assertEqual(got["text_window"]["next_offset"], s.OPEN_MAX_CHARS)
        # the window as the client is sent it is shown whole (CLIENT_SHOWN_CHARS), even a
        # text of quotes and line breaks (escaped as JSON at 1.67 characters a character)
        from operating import mcp_server_evidence as me
        for text in (body, '"x"\r\n' * s.OPEN_MAX_CHARS):
            body = text
            got = s.open_one(read, {"citation": "original://m/11"})
            self.assertLessEqual(len(json.dumps(got, ensure_ascii=False, separators=(",", ":"))),
                                 me.CLIENT_SHOWN_CHARS)
        # a short text is one read, unchanged
        body = "Order 4411 ships Friday"
        got = s.open_one(read, {"citation": "original://m/10"})
        self.assertEqual(got["content"]["untrusted_text"], body)
        self.assertNotIn("truncated", got["content"])

    def test_an_opened_text_says_how_much_of_it_is_shown(self):
        """A1a: each opened text carries coverage {chars, shown: [start, end]} taken from
        the result's own window; a result with no text window gets none."""
        body = "".join(f"line {i:06d} " for i in range(s.OPEN_MAX_CHARS // 6))

        def read(arguments):
            start = arguments.get("text_offset", 0)
            most = arguments["max_chars"]
            window = body[start:start + most]
            more = start + len(window) < len(body)
            return {"status": "resolved", "content": {"untrusted_text": window},
                    "text_window": {"offset": start, "chars": len(window),
                                    "total_chars": len(body),
                                    **({"next_offset": start + len(window)} if more else {})}}
        got = s.open_one(read, {"citation": "original://m/1"})
        self.assertEqual(got["coverage"], {"chars": len(body), "shown": [0, s.OPEN_MAX_CHARS]})
        got = s.open_one(read, {"citation": "original://m/1", "text_offset": 20})
        self.assertEqual(got["coverage"]["shown"][0], 20)
        short = "Order 4411 ships Friday"
        got = s.open_one(lambda a: {
            "status": "resolved", "content": {"untrusted_text": short},
            "text_window": {"offset": 0, "chars": len(short), "total_chars": len(short)}},
            {"citation": "original://m/2"})
        self.assertEqual(got["coverage"], {"chars": len(short), "shown": [0, len(short)]})
        # a mail or Teams item through the comms open: its own length and window
        mail = {"citation": "graph-mail://u/m1", "text": {"untrusted_text": "x" * 100},
                "text_offset": 0, "text_chars": 250, "next_offset": 100}
        got = s.open_one(read, {"citation": "graph-mail://u/m1"}, lambda a: dict(mail))
        self.assertEqual(got["coverage"], {"chars": 250, "shown": [0, 100]})
        # no text window (a record, or a host that does not say): no coverage, not a guess
        got = s.open_one(lambda a: {"status": "resolved", "fields": {"n": 1}},
                         {"citation": "original://m/3"})
        self.assertNotIn("coverage", got)
        # an error carries none; several citations each carry their own
        refused = {"error": {"code": "not_found", "message": "x"}}
        self.assertEqual(s.open_one(lambda a: refused, {"citation": "original://m/4"}), refused)
        many = [f"original://m/{i}" for i in range(s.MANY_MAX)]
        got = s.open_one(read, {"citations": many})
        self.assertEqual(len(got["opened"]) + len(got.get("next_citations", [])), s.MANY_MAX)
        self.assertTrue(all(o["coverage"]["chars"] == len(body) for o in got["opened"]
                            if "error" not in o))

    def test_more_citations_than_one_response_holds_are_all_read_a_page_per_call(self):
        """No cap on citations (owner 11-23): 41, 200 and 1,000 are all read, a page per call,
        each once and in order: what a response cannot hold comes back whole in
        ``next_citations`` (not read yet), and sending it again continues."""
        def comms(arguments):
            return {"citation": arguments["citation"], "kind": "email",
                    "text": {"untrusted_text": "x" * arguments["max_chars"]}}
        for n in (41, 200, 1000):
            pending = [f"graph-mail://u/m{i}" for i in range(n)]
            read, rounds = [], 0
            while pending:
                reads = []
                got = s.open_one(None, {"citations": pending},
                                 lambda a: reads.append(a) or comms(a))
                page = [g["citation"] for g in got["opened"]]
                self.assertEqual(page, pending[:len(page)], n)
                self.assertEqual(len(reads), len(page), n)     # every item of a page is read
                self.assertTrue(all("text" in g for g in got["opened"]), n)
                read += page
                pending = got.get("next_citations", [])
                self.assertEqual(len(page) + len(pending), len(page) + len(pending))
                rounds += 1
                self.assertLess(rounds, n + 2)
            self.assertEqual(read, [f"graph-mail://u/m{i}" for i in range(n)], n)

    def test_every_cut_leg_of_a_search_names_its_own_cursor(self):
        """A search whose mail leg and records leg are both cut states both cursors with
        the sources each continues (before, only the first leg's cursor was given and the
        other leg's remainder could not be reached by paging)."""
        def leg(source, cursor, n):
            return {"documents": [{"date": f"2026-11-{n:02d}"}],
                    "searched": {"sources": [source]},
                    "completeness": {"complete": False, "truncated": True, "returned": 1,
                                     "next_cursor": cursor, "reason": "page_cap"}}
        got = s._merged([leg("graph_mail", "c1:abc", 3), leg("records", "r1:def", 2)])
        done = got["completeness"]
        self.assertEqual(done["next_cursor"], "c1:abc")
        self.assertEqual(done["continue"], [{"sources": ["graph_mail"], "cursor": "c1:abc"},
                                            {"sources": ["records"], "cursor": "r1:def"}])
        one = s._merged([leg("graph_mail", "c1:abc", 3), {
            **leg("records", None, 2), "completeness": {"complete": True, "truncated": False,
                                                        "returned": 1}}])
        self.assertNotIn("continue", one["completeness"])

    def test_several_citations_bad_input_fails_closed_without_a_read(self):
        def read(arguments):
            raise AssertionError("no read for a bad call")
        for bad in ({"citations": []}, {"citations": "graph-mail://u/m1"},
                    {"citations": ["a://x"] * 2}, {"citations": [5]},
                    {"citations": ["a://x"], "citation": "a://x"},
                    {"citations": ["a://x"], "text_offset": 10},
                    {"citations": ["a://x"], "near": ""}, {"citations": ["a://x"], "near": 3},
                    {"citations": ["a://x"], "near": "--"},
                    {"citation": "a://x", "near": "x" * 201}):
            self.assertEqual(s.open_one(read, bad, read)["error"]["code"], "invalid_input",
                             bad)

    def test_a_refused_read_stays_refused(self):
        refused = {"error": {"code": "not_found", "message": "no such item"}}
        self.assertEqual(s.open_one(lambda a: refused, {"citation": "original://m/3"}),
                         refused)


class OpenNearTest(unittest.TestCase):
    """A ``near`` window that opens past a mail's top serves the top first (11-22: Ask
    opened a supplier's reply at "complaint", inside the quoted history, and never saw the
    answer above it)."""

    @staticmethod
    def rows(*texts):
        import zlib
        return [("emails", "graph-mail://u/m1", "RE: PO 5461", "care@vendor.example", None,
                 None, None, i, 0, zlib.compress(t.encode("utf-8")), "unknown",
                 f"row.f{i}", f"d{i}") for i, t in enumerate(texts)]

    def served(self, got, texts):
        from operating import mcp_server_evidence as me
        spans = me._opened_texts(got)
        self.assertEqual(len(spans), len(got["fields_served"]))   # every span checks out
        window = got["text"]["untrusted_text"]
        for entry in got["fields_served"]:
            source = texts[int(entry["field"][5:])]
            self.assertEqual(window[entry["at"]:entry["at"] + entry["chars"]],
                             source[entry["offset"]:entry["offset"] + entry["chars"]])
        return spans

    def test_the_top_is_served_with_a_window_past_it(self):
        from operating.pg import comms_search as cs
        reply = ("You don't often get email from care@vendor.example. Learn why this is "
                 "important<https://safe.example/?u=" + "a" * 900 + ">\n\nHello Ross,\n"
                 "Very happy to hear that!\n\n" + "Notice. " * 150 + "\nFrom: Paula\n"
                 "Sent: 10 Nov\nSubject: RE: PO 5461 Complaint\n\nCan you confirm?\n"
                 + "older " * 900)
        for most in (s.MANY_CHARS, 1250, s.OPEN_MAX_CHARS):
            got = cs.opened(self.rows(reply), offset=0, most=most, near=("complaint",))
            window = got["text"]["untrusted_text"]
            self.assertIn("Very happy to hear that!", window, most)
            self.assertIn("Complaint", window, most)
            self.assertLessEqual(len(window), most)
            self.served(got, [reply])
        # a place inside the top: one window from the start, no head
        got = cs.opened(self.rows(reply), offset=0, most=2500, near=("happy",))
        self.assertEqual(got["text_offset"], 0)
        self.assertNotIn("head_chars", got)
        # a window too small for a head as well: the window alone, as before
        got = cs.opened(self.rows(reply), offset=0, most=8, near=("complaint",))
        self.assertNotIn("head_chars", got)
        self.assertEqual(len(got["text"]["untrusted_text"]), 8)
        # an explicit offset reads on from there (paging is never redirected)
        got = cs.opened(self.rows(reply), offset=3000, most=2500, near=("complaint",))
        self.assertEqual(got["text_offset"], 3000)
        self.assertNotIn("head_chars", got)

    def test_spans_across_fields(self):
        from operating.pg import comms_search as cs
        texts = ["Top line answer. " * 10, "filler " * 600 + " the promised date is 3 Dec"]
        got = cs.opened(self.rows(*texts), offset=0, most=2500, near=("promised",))
        window = got["text"]["untrusted_text"]
        self.assertIn("Top line answer.", window)
        self.assertIn("promised date is 3 Dec", window)
        self.served(got, texts)


class SearchTest(unittest.TestCase):
    def test_unknown_arguments_fail_closed(self):
        got = s.run(lambda a: self.fail("no search"), {"query": "x", "object_id": "y"})
        self.assertEqual(got["error"]["code"], "invalid_input")

    def test_listed_only_as_agenda_tools(self):
        for name in (s.TOOL_NAME, s.OPEN_NAME):
            self.assertIn(name, catalog.AGENDA_TOOLS)
            self.assertIn(name, catalog.SELF_ENVELOPING_TOOLS)


class CoverageTest(unittest.TestCase):
    """A1a: every plx.search result says what it covered: how many matched (exact when the
    search read every match), how many are shown, the sources and dates, its depth."""

    def result(self, n, more=False, withheld=0):
        out = {"documents": [{"citation": f"graph-mail://m/{i}", "kind": "email",
                              "title": None, "date": None, "snippet": None} for i in range(n)],
               "searched": {"sources": ["graph_mail", "teams"], "since": "2026-11-01",
                            "depth": "attachments read in the newest 200 mails"},
               "gaps": [], "completeness": {"complete": not more, "truncated": more,
                                            "returned": n,
                                            "next_cursor": "c1:x" if more else None}}
        if withheld:
            out["egress_withheld"] = withheld
        return out

    def test_matched_and_shown_for_1_10_60_hits_and_a_cut_page(self):
        for n in (1, 10, 60):
            got = s.with_coverage(self.result(n), {"query": "x"})["coverage"]
            self.assertEqual((got["matched"], got["shown"], got["matched_basis"]),
                             (n, n, "every match read"), n)
            self.assertEqual((got["sources"], got["since"], got["until"]),
                             (["graph_mail", "teams"], "2026-11-01", None))
            self.assertIn("newest 200", got["depth"])
        # hits kept from the model are matches too (counted, never shown)
        self.assertEqual(s.with_coverage(self.result(3, withheld=2), {})["coverage"]["matched"], 5)
        # a next page continues the list: not counted, and it says so
        cut = s.with_coverage(self.result(50, more=True), {})["coverage"]
        self.assertEqual((cut["matched"], cut["shown"]), (None, 50))
        self.assertIn("cursor", cut["matched_basis"])
        # a list too large for one response is returned as it is (``paged`` reads a smaller
        # page of it, with its legs' cursors): no hit is cut away here
        whole = self.result(51)
        self.assertIs(s.fitted(whole, lambda r: len(r["documents"]) <= 50), whole)
        # an error is never given coverage
        self.assertEqual(s.with_coverage({"error": {"code": "x"}}, {}), {"error": {"code": "x"}})


if __name__ == "__main__":
    unittest.main()


class RoutingTest(unittest.TestCase):
    """Which path answers a plx.search / plx.open: mail and Teams through the guarded
    views (the host's internal plx.sql_comms_*), everything else through the host's
    plx.documents / plx.read; a host that does not serve the mail tables falls back."""

    def calls(self, comms_answer=None):
        seen = {"comms": [], "docs": []}

        def comms(arguments):
            seen["comms"].append(arguments)
            return comms_answer if comms_answer is not None else {
                "documents": [{"citation": "email:v1", "kind": "email", "date": "2026-10-02"}],
                "completeness": {"complete": True, "truncated": False}}

        def docs(arguments):
            seen["docs"].append(arguments)
            return {"documents": [{"citation": "sharepoint://s/a.pdf",
                                   "kind": "sharepoint_file", "date": "2026-10-03"}],
                    "completeness": {"complete": True, "truncated": False}}
        return seen, comms, docs

    def test_search_splits_by_source_and_merges_newest_first(self):
        seen, comms, docs = self.calls()
        got = s.run(docs, {"query": "pallets"}, call_comms=comms)
        self.assertEqual([d["citation"] for d in got["documents"]],
                         ["sharepoint://s/a.pdf", "email:v1"])
        # files too: this comms leg did not search them (no ``searched``), so the old way
        self.assertEqual(seen["comms"][0]["sources"], ["graph_mail", "sharepoint", "teams"])
        self.assertEqual(seen["docs"][0]["sources"], ["sharepoint", "original"])

    def test_a_slow_leg_is_waited_for_and_returns_all_its_hits(self):
        """No deadline cut. A leg that takes long still finishes and its
        hits are in the answer; nothing is named unfinished."""
        import time as _t

        def slow_comms(arguments):
            _t.sleep(0.4)
            return {"documents": [{"citation": "email:v1", "kind": "email",
                                   "date": "2026-10-02"}],
                    "searched": {"sources": ["graph_mail"]},
                    "completeness": {"complete": True, "truncated": False}}

        def records(arguments):
            return {"documents": [{"citation": "api://r/1", "kind": "record",
                                   "date": "2026-10-04"}],
                    "searched": {"sources": ["records"]},
                    "completeness": {"complete": True, "truncated": False}}
        got = s.run(lambda a: {"documents": [], "completeness": {"complete": True,
                                                                   "truncated": False}},
                    {"query": "croda", "sources": ["graph_mail", "records"]},
                    call_comms=slow_comms, call_records=records)
        self.assertEqual([d["citation"] for d in got["documents"]], ["api://r/1", "email:v1"])
        self.assertEqual(got["searched"]["sources"], ["graph_mail", "records"])
        self.assertFalse(got.get("gaps"))
        self.assertFalse(hasattr(s, "SEARCH_DEADLINE"))

    def test_legs_that_finish_in_time_are_unchanged_and_run_together(self):
        import threading
        both = threading.Barrier(2, timeout=5)

        def comms(arguments):
            both.wait()    # only passes when the records leg runs at the same time
            return {"documents": [{"citation": "email:v1", "kind": "email",
                                   "date": "2026-10-02"}],
                    "searched": {"sources": ["graph_mail"]},
                    "completeness": {"complete": True, "truncated": False}}

        def records(arguments):
            both.wait()
            return {"documents": [], "searched": {"sources": ["records"]},
                    "completeness": {"complete": True, "truncated": False}}
        got = s.run(lambda a: {"documents": []}, {"query": "croda",
                                                  "sources": ["graph_mail", "records"]},
                    call_comms=comms, call_records=records)
        self.assertEqual([d["citation"] for d in got["documents"]], ["email:v1"])
        self.assertEqual(got["searched"]["sources"], ["graph_mail", "records"])
        self.assertNotIn("gaps", {k: v for k, v in got.items() if v})

    def test_each_legs_time_is_on_the_calls_record_and_never_in_its_result(self):
        from operating import call_phases
        seen, comms, docs = self.calls()
        with call_phases.record() as took:
            got = s.run(docs, {"query": "pallets"}, call_comms=comms)
        self.assertEqual(sorted(took), ["comms", "documents"])
        self.assertTrue(all(n == 1 and ms >= 0 for n, ms in took.values()))
        self.assertNotIn("phases", json.dumps(got))
        got = s.run(docs, {"query": "pallets"}, call_comms=comms)   # no record: nothing kept
        self.assertEqual(len(got["documents"]), 2)

    def test_the_gates_end_line_puts_the_phases_on_the_calls_event(self):
        from operating.agent_session import attach_call_timing
        events = [{"type": "tool_call", "name": "plx.search", "arguments": {"query": "a"}},
                  {"type": "tool_call", "name": "plx.sql", "arguments": {"sql": "x"}}]
        lines = [json.dumps(x) for x in (
            {"event": "start", "id": 1, "name": "plx.search", "arguments": {"query": "a"},
             "at": 10.0},
            {"event": "start", "id": 2, "name": "plx.sql", "arguments": {"sql": "x"}, "at": 10.1},
            {"event": "end", "id": 2, "at": 10.3},
            {"event": "end", "id": 1, "at": 19.0,
             "phases": {"comms": [1, 6100], "documents": [1, 2400]}})]
        attach_call_timing(events, lines, 10.0)
        self.assertEqual(events[0]["phases"], {"comms": [1, 6100], "documents": [1, 2400]})
        self.assertEqual(events[0]["elapsed_ms"], 9000)
        self.assertNotIn("phases", events[1])

    def test_a_search_too_large_for_one_response_is_a_smaller_page_with_cursors(self):
        """Live: an all-sources search of 50 hits a leg was refused whole as too large after
        27 s (result_too_large). A result that fits is untouched (1 or 10 hits a leg); one
        that does not has its snippets cut shorter (every hit kept), else it is read again
        as a smaller page of at most the contract's 50 hits whose legs' cursors continue
        it. Checked on the contract; the host's plx.search answers so."""
        import json
        from pathlib import Path
        from ontology.json_contract import validate
        from operating.mcp_server_calls import CallsMixin, search_fits as fits
        contract = json.loads((Path(s.__file__).with_name("ontology_tool_contracts.json"))
                              .read_text(encoding="utf-8"))
        paged_n = 0
        for n, words in ((1, 16), (10, 16), (60, 1300), (20, 1300), (15, 1300)):
            def leg(kind, n=n, words=words, prefix="c1"):
                def answer(arguments):
                    start = int(arguments["cursor"].split(":")[1]) if arguments.get(
                        "cursor") else 0
                    stop = min(n, start + int(arguments.get("limit", 20)))
                    more = stop < n
                    return {"documents": [
                        {"citation": f"graph-mail://{kind}/{i}", "kind": "email",
                         "title": {"untrusted_text": "t " * 40,
                                   "origin": {"citation": f"graph-mail://{kind}/{i}"}},
                         "snippet": {"untrusted_text": "purchase order " * words,
                                     "origin": {"citation": f"graph-mail://{kind}/{i}"}},
                         "date": f"2026-10-{1 + i % 28:02d}"} for i in range(start, stop)],
                        "searched": {"sources": ["graph_mail"]}, "gaps": [],
                        "completeness": {"complete": not more, "truncated": more,
                                         "returned": stop - start, "reason":
                                         "page_cap" if more else None,
                                         "next_cursor": f"{prefix}:{stop}" if more else None}}
                return answer
            legs = {s.COMMS_SEARCH: leg("mail"), s.RECORDS_SEARCH: leg("records", prefix="r1"),
                    s.DOCUMENTS: lambda wanted: {"documents": [], "completeness": {}}}

            class Host(CallsMixin):
                def _generic_call(self, name, wanted):
                    return legs[name](wanted)

                def _refs(self):
                    class Refs:
                        search = staticmethod(lambda result: result)
                    return Refs()
            got = Host()._call(s.TOOL_NAME, {"query": "purchase order", "limit": 50})
            with self.subTest(n=n, words=words):
                self.assertNotIn("error", got)
                self.assertTrue(fits(got))
                self.assertEqual(validate(got, contract["tools"][s.TOOL_NAME]["output"],
                                          root=contract), [])
                self.assertLessEqual(len(got["documents"]), s.MAX_HITS)
                done = got["completeness"]
                if done["complete"]:      # every hit kept (snippets cut shorter to fit)
                    self.assertEqual(len(got["documents"]), 2 * n)
                    continue
                paged_n += 1
                self.assertTrue(done["truncated"] and not done["complete"], done)
                self.assertTrue(done["next_cursor"] or done.get("continue"), done)
                self.assertNotIn("result_budget", json.dumps(got))
        self.assertTrue(paged_n, "no result needed a smaller page")
        # an error answer is never touched
        self.assertEqual(s.fitted({"error": {"code": "x"}}, lambda r: False),
                         {"error": {"code": "x"}})
        # a result with no hits to leave out is never said to have left some out
        bare = {"documents": [], "gaps": [{"state": "unknown", "detail": "x" * 10}],
                "completeness": {"complete": True, "returned": 0}}
        self.assertIs(s.fitted(bare, lambda r: False), bare)

    def test_participant_and_comms_cursor_search_mail_only(self):
        for args in ({"participant": "a@b.example"},
                     {"query": "pallets", "cursor": "c1:2026-10-02T00:00:00Z|email:v1"}):
            with self.subTest(args=args):
                seen, comms, docs = self.calls()
                s.run(docs, args, call_comms=comms)
                self.assertEqual(len(seen["comms"]), 1)
                self.assertEqual(seen["docs"], [])

    def test_another_cursor_never_reaches_the_comms_path(self):
        seen, comms, docs = self.calls()
        s.run(docs, {"query": "pallets", "cursor": "opaque-docs-cursor"}, call_comms=comms)
        self.assertEqual(seen["comms"], [])
        self.assertEqual(len(seen["docs"]), 1)

    def test_files_the_comms_leg_did_not_search_are_searched_the_old_way(self):
        # not searched (a reader who is not a full reader), or the statement failed
        for answer in ({"documents": [], "completeness": {"complete": True,
                                                           "truncated": False}},
                       {"error": {"code": "timeout", "message": "past 25 s"}}):
            with self.subTest(answer=answer):
                seen, comms, docs = self.calls(answer)
                got = s.run(docs, {"query": "pallets", "sources": ["sharepoint"]},
                            call_comms=comms)
                self.assertEqual(seen["comms"][0]["sources"], ["sharepoint"])
                self.assertEqual(seen["docs"][0]["sources"], ["sharepoint"])
                self.assertEqual([d["citation"] for d in got["documents"]],
                                 ["sharepoint://s/a.pdf"])
                self.assertEqual(got.get("gaps") or [], [])
        # searched by the comms leg (a full reader): never again the old way
        seen, comms, docs = self.calls({
            "documents": [], "searched": {"sources": ["sharepoint"]},
            "completeness": {"complete": True, "truncated": False}})
        s.run(docs, {"query": "pallets", "sources": ["sharepoint"]}, call_comms=comms)
        self.assertEqual(seen["docs"], [])

    def test_not_served_falls_back_and_other_errors_stay(self):
        for code in ("not_served", "not_ready"):
            seen, comms, docs = self.calls({"error": {"code": code, "message": "x"}})
            got = s.run(docs, {"query": "pallets"}, call_comms=comms)
            self.assertEqual(seen["docs"][0].get("sources"), None)   # the old path, whole ask
            self.assertEqual([d["citation"] for d in got["documents"]],
                             ["sharepoint://s/a.pdf"])
        # the query itself is wrong: the whole search says so, nothing else searched
        seen, comms, docs = self.calls({"error": {"code": "invalid_input", "message": "x"}})
        self.assertEqual(s.run(docs, {"query": "pallets"}, call_comms=comms)["error"]["code"],
                         "invalid_input")
        self.assertEqual(seen["docs"], [])

    def test_mail_and_teams_not_read_is_a_gap_beside_the_other_hits(self):
        """A comms leg that timed out, was busy, failed or raised: the files' and records'
        hits stand with a gap naming mail and Teams; asked for mail alone, the error."""
        rec = {"documents": [{"citation": "api://c/1", "kind": "record", "date": "2026-10-01",
                              "title": {"untrusted_text": "Acme Ltd"}}],
               "searched": {"sources": ["records"]}, "gaps": [],
               "completeness": {"complete": True, "truncated": False, "returned": 1}}
        for answer in ({"error": {"code": "timeout", "message": "past 25 s"}},
                       {"error": {"code": "busy", "message": "x"}}, RuntimeError("boom")):
            with self.subTest(answer=answer):
                seen, comms, docs = self.calls(answer if isinstance(answer, dict) else None)
                if not isinstance(answer, dict):
                    def comms(arguments, error=answer):
                        raise error
                got = s.run(docs, {"query": "acme"}, call_comms=comms,
                            call_records=lambda a: rec)
                self.assertEqual([d["citation"] for d in got["documents"]],
                                 ["sharepoint://s/a.pdf", "api://c/1"])
                self.assertEqual([g["subject"] for g in got["gaps"]], ["mail and Teams"])
                self.assertFalse(got["completeness"]["complete"])
        seen, comms, docs = self.calls({"error": {"code": "timeout", "message": "x"}})
        got = s.run(docs, {"query": "acme", "sources": ["graph_mail"]}, call_comms=comms)
        self.assertEqual(got["error"]["code"], "timeout")

    def test_records_leg_with_query_words_only_its_cursor_alone_and_skips_as_a_gap(self):
        asked = []

        def records(answer):
            def call(arguments):
                asked.append(arguments)
                return answer
            return call
        rec = {"documents": [{"citation": "api://c/1", "kind": "record", "date": "2026-10-01",
                              "title": {"untrusted_text": "Acme Ltd"}},
                             {"citation": "api://c/2", "kind": "record", "date": "2026-10-04",
                              "title": {"untrusted_text": "Freightcom Inc"}}],
               "searched": {"sources": ["records"]}, "gaps": [],
               "completeness": {"complete": True, "truncated": False, "returned": 2}}
        seen, comms, docs = self.calls()
        got = s.run(docs, {"query": "acme"}, call_comms=comms, call_records=records(rec))
        self.assertEqual([d["citation"] for d in got["documents"]],
                         ["api://c/2", "sharepoint://s/a.pdf", "email:v1", "api://c/1"])
        self.assertNotIn("egress_withheld", got)   # a record naming Freightcom is read like any
        self.assertEqual(asked[-1], {"query": "acme", "limit": 20})   # never a source list
        self.assertEqual(seen["docs"][0]["sources"], ["sharepoint", "original"])
        for args in ({"participant": "a@b.example"},
                     {"query": "acme", "sources": ["graph_mail"]},
                     {"query": "acme", "cursor": "c1:2026-10-02T00:00:00Z|email:v1"},
                     {"query": "acme", "cursor": "opaque-docs-cursor"}):
            asked.clear()
            s.run(docs, args, call_comms=comms, call_records=records(rec))
            self.assertEqual(asked, [], args)
        seen, comms, docs = self.calls()
        got = s.run(docs, {"query": "acme", "cursor": "r1:20"}, call_comms=comms,
                    call_records=records(rec))
        self.assertEqual((seen["comms"], seen["docs"]), ([], []))
        self.assertEqual(asked[-1]["cursor"], "r1:20")
        self.assertEqual(s.run(docs, {"participant": "a@b.example", "sources": ["records"]},
                               call_records=records(rec))["error"]["code"], "invalid_input")
        def boom(arguments):
            raise RuntimeError("pool gone")
        # any failure of the records leg (a full plx.sql pool, a failed or refused
        # statement, an unknown code, a broken answer, a raise) is a gap: the mail and
        # file hits stand and no record is shown
        for code, leg in [(c, records({"error": {"code": c, "message": "m"}})) for c in (
                "not_served", "not_ready", "too_broad", "timeout", "busy", "query_failed",
                "integrity", "not_readable", "something_new")] + [
                ("query_failed", records("junk")), ("query_failed", boom),
                ("query_failed", records({"error": "flat"}))]:
            seen, comms, docs = self.calls()
            got = s.run(docs, {"query": "acme"}, call_comms=comms, call_records=leg)
            self.assertNotIn("error", got, code)
            self.assertEqual([d["citation"] for d in got["documents"]],
                             ["sharepoint://s/a.pdf", "email:v1"], code)
            self.assertEqual(got["gaps"][0]["reason"], code)
            self.assertEqual(got["gaps"][0]["subject"], "records")
            self.assertFalse(got["completeness"]["complete"])
        got = s.run(docs, {"query": "acme"}, call_comms=comms, call_records=records(
            {"error": {"code": "invalid_input", "message": "m"}}))
        self.assertEqual(got["error"]["code"], "invalid_input")   # the query's own error

    def test_open_routes_comms_then_read_and_a_withheld_item_never_falls_back(self):
        reads = []

        def read(arguments):
            reads.append(arguments)
            return {"status": "resolved", "text": {"untrusted_text": "ok"}}
        opened = {"citation": "email:v1", "kind": "email",
                  "text": {"untrusted_text": "Order 4411 ships"}}
        self.assertEqual(s.open_one(read, {"citation": "email:v1"}, lambda a: opened), opened)
        self.assertEqual(reads, [])
        for answer in ({"not_comms": True}, {"error": {"code": "not_served", "message": ""}},
                       {"error": {"code": "not_ready", "message": ""}},
                       # the store refused the comms statement itself (a missing grant)
                       {"error": {"code": "not_readable", "message": ""}}):
            reads.clear()
            s.open_one(read, {"citation": "sharepoint://s/a.pdf"}, lambda a: answer)
            self.assertEqual(len(reads), 1, answer)
        reads.clear()
        refused = {"error": {"code": "egress_withheld", "message": "no"}}
        self.assertEqual(s.open_one(read, {"citation": "email:v2"}, lambda a: refused),
                         refused)
        self.assertEqual(reads, [], "a comms item kept from the AI is never re-read")


class ReplayCeilingTest(unittest.TestCase):
    """A replay of a past moment (``read_ceiling``, ``PLX_READ_CEILING``): every leg of
    plx.search is asked for nothing after it, and plx.open shows a non-comms item only when
    it carries a time before it; an unreadable ceiling answers nothing at all."""

    def setUp(self):
        import os
        from unittest import mock
        from operating import read_ceiling
        self.env = lambda value: mock.patch.dict(os.environ, {read_ceiling.CEILING_ENV: value})

    def test_every_leg_searches_before_the_moment(self):
        seen = []

        def leg(arguments):
            seen.append(arguments)
            return {"documents": [], "completeness": {"complete": True, "truncated": False}}
        with self.env("2026-07-14T14:00:00Z"):
            for until, want in ((None, "2026-07-14T14:00:00.000Z"),
                                ("2026-12-31", "2026-07-14T14:00:00.000Z"),
                                ("2026-07-14T15:00:00Z", "2026-07-14T14:00:00.000Z"),
                                ("2026-07-10", "2026-07-10")):
                seen.clear()
                s.run(leg, {"query": "pallets", **({"until": until} if until else {})},
                      call_comms=leg, call_records=leg)
                with self.subTest(until=until):
                    self.assertEqual(len(seen), 3)
                    self.assertEqual({a.get("until") for a in seen}, {want})

    def test_open_shows_only_what_was_there_then(self):
        def read_at(**fields):
            return lambda arguments: {"status": "resolved", "fields": fields,
                                      "text": {"untrusted_text": "ok"}}
        with self.env("2026-07-14T14:00:00Z"):
            ok = s.open_one(read_at(received_date_time="2026-07-14T13:00:00Z"),
                            {"citation": "graph-mail://a/att1"}, lambda a: {"not_comms": True})
            self.assertEqual(ok["status"], "resolved")
            for fields in ({"received_date_time": "2026-07-14T14:00:00Z"},
                           {"created_at": "2026-07-01", "last_modified": "2026-08-02T00:00Z"},
                           {}, {"received_date_time": "not a time"}):
                with self.subTest(fields=fields):
                    got = s.open_one(read_at(**fields), {"citation": "sharepoint://s/a.pdf"},
                                     lambda a: {"not_comms": True})
                    self.assertEqual(got["error"]["code"], "not_found")

    def test_an_unreadable_ceiling_answers_nothing(self):
        calls = []

        def leg(arguments):
            calls.append(arguments)
            return {"documents": [], "completeness": {"complete": True, "truncated": False}}
        for bad in ("yesterday", "2026-07-14T14:00:00"):
            with self.env(bad), self.subTest(ceiling=bad):
                self.assertEqual(s.run(leg, {"query": "x"}, call_comms=leg)["error"]["code"],
                                 "not_ready")
                self.assertEqual(s.open_one(leg, {"citation": "email:v1"}, leg)["error"]
                                 ["code"], "not_ready")
        self.assertEqual(calls, [])

    def test_paged_scales_limit_proportionally_and_depth_said_explains_tiers(self):
        from operating.pg import comms_search as cs
        # Proportional byte-budget scaling avoids dropping 50 -> 25 when 38 hits fit
        seen_limits = []

        def search(args):
            lim = args.get("limit", 20)
            seen_limits.append(lim)
            doc = {"citation": "graph-mail://box/m", "kind": "email",
                   "snippet": {"untrusted_text": "x" * 1050}}
            return {
                "documents": [doc] * lim,
                "completeness": {"complete": False, "truncated": True, "returned": lim,
                                 "next_cursor": "c1:1|2026-11-25T00:00:00+00:00|graph-mail://box/m",
                                 "reason": "page_cap"},
            }

        got = s.paged(search, {"query": "croda", "limit": 50},
                      lambda r: s._encoded(r) <= s.MANY_BYTES)
        self.assertEqual(seen_limits[0], 50)
        self.assertGreater(seen_limits[1], 25)
        self.assertEqual(len(seen_limits), 2)
        self.assertLessEqual(s._encoded(got), s.MANY_BYTES)

        # depth_said explains header->body tier order and exact-complete widened cursors
        d_more = cs.depth_said("croda", {}, later=False, attachments=False, more=True)
        self.assertIn("header matches newest first, then body-only matches newest first", d_more)
        d_exact = cs.depth_said("20311 terminal", {}, later=False, attachments=False,
                                more=False, exact_complete=True, exact_returned=0)
        self.assertIn("all 0 exact every-word matches in this window are shown", d_exact)
        sql_party = cs.count_sql(["emails"], guarded="biz_sql", biz="biz", since=True,
                                 until=False, query=False, party_kind="domain")
        self.assertIn("e.domains @> ARRAY[%(party)s]", sql_party)
        self.assertIn("e.received_at >= %(since)s", sql_party)
