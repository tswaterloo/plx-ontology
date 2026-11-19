"""plx.search and plx.open (``operating.ontology_tools_search``): the host's own
plx.documents / plx.read for the session's reader, cut to what the Ask needs; anything
naming Freightcom never reaches the model; unknown arguments fail closed."""

from __future__ import annotations

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
        self.assertEqual(calls, [{"citation": "original://m/1", "text_offset": 4000,
                                  "max_chars": s.OPEN_MAX_CHARS}])

    def test_freightcom_is_never_shown(self):
        for body in ({"text": {"untrusted_text": "rates from FREIGHTCOM"}},
                     {"title": "Freightcom invoice", "text": "x"},
                     {"pages": [{"text": "see freightcom.com"}]}):
            got = s.open_one(lambda a: {"status": "resolved", **body},
                             {"citation": "original://m/2"})
            self.assertEqual(got["error"]["code"], "egress_withheld", body)
            self.assertNotIn("text", got)

    def test_unexpected_input_fails_closed_without_a_read(self):
        def read(arguments):
            raise AssertionError("no read for a bad call")
        for bad in ([], None, {}, {"citation": ""}, {"citation": 5},
                    {"citation": "original://m/1", "sql": "select 1"},
                    {"citation": "original://m/1", "text_offset": -1},
                    {"citation": "original://m/1", "text_offset": True},
                    {"citation": "original://m/1", "max_chars": 10 ** 9}):
            self.assertEqual(s.open_one(read, bad)["error"]["code"], "invalid_input", bad)

    def test_a_refused_read_stays_refused(self):
        refused = {"error": {"code": "not_found", "message": "no such item"}}
        self.assertEqual(s.open_one(lambda a: refused, {"citation": "original://m/3"}),
                         refused)


class SearchTest(unittest.TestCase):
    def test_freightcom_hits_left_out_and_counted(self):
        got = s.compact({"documents": [
            {"citation": "original://a", "title": "PO 1", "snippet": "fine"},
            {"citation": "original://b", "title": "x", "snippet": "Freightcom quote"},
            {"citation": "original://c", "title": "y", "snippet": "ok", "mentions": ["z"]}]})
        self.assertEqual([h["citation"] for h in got["documents"]],
                         ["original://a", "original://c"])
        self.assertEqual(got["egress_withheld"], 1)
        self.assertNotIn("mentions", got["documents"][1])

    def test_a_hit_is_shown_only_when_no_text_of_it_names_freightcom(self):
        # a field not shown (mentions), or the full text behind a plain snippet, naming
        # Freightcom leaves the hit out; an unreadable full text leaves it out too
        docs = {"documents": [
            {"citation": "original://a", "title": "PO 1", "snippet": "fine"},
            {"citation": "original://b", "title": "x", "snippet": "ok",
             "mentions": ["FREIGHTCOM INC"]},
            {"citation": "original://c", "title": "y", "snippet": "invoice attached"},
            {"citation": "original://d", "title": "z", "snippet": "see below"},
            {"citation": "original://e", "title": "w", "snippet": "plain"}],
            "searched": {"sources": ["original"]}, "gaps": [], "completeness": {}}
        texts = {"original://a": {"text": "all fine"},
                 "original://c": {"text": "Quote from Freightcom, rate 1,200"},
                 "original://d": {"error": {"code": "not_readable"}}}
        reads = []

        def read(wanted):
            reads.append(wanted)
            if wanted["citation"] == "original://e":
                raise RuntimeError("store down")
            return texts[wanted["citation"]]

        got = s.run(lambda a: docs, {"query": "invoice"}, read)
        self.assertEqual([h["citation"] for h in got["documents"]], ["original://a"])
        self.assertEqual(got["egress_withheld"], 2)
        self.assertEqual(got["unchecked"], 2)
        self.assertNotIn("original://b", [w["citation"] for w in reads])   # never read
        self.assertTrue(all(w["max_chars"] == s.CHECK_MAX_CHARS for w in reads))
        # out of time: the rest are left out unread
        from operating import call_limits
        from unittest import mock
        with mock.patch.object(call_limits, "seconds_left", lambda: 1.0):
            got = s.run(lambda a: docs, {"query": "invoice"}, read)
        self.assertEqual(got["documents"], [])
        self.assertEqual(got["unchecked"], 4)

    def test_unknown_arguments_fail_closed(self):
        got = s.run(lambda a: self.fail("no search"), {"query": "x", "object_id": "y"})
        self.assertEqual(got["error"]["code"], "invalid_input")

    def test_listed_only_as_agenda_tools(self):
        for name in (s.TOOL_NAME, s.OPEN_NAME):
            self.assertIn(name, catalog.AGENDA_TOOLS)
            self.assertIn(name, catalog.SELF_ENVELOPING_TOOLS)


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
        self.assertEqual(seen["comms"][0]["sources"], ["graph_mail", "teams"])
        self.assertEqual(seen["docs"][0]["sources"], ["sharepoint", "original"])

    def test_participant_and_comms_cursor_search_mail_only(self):
        for args in ({"participant": "a@b.example"},
                     {"query": "pallets", "cursor": "c1:2026-10-02T00:00:00Z|email:v1"}):
            with self.subTest(args=args):
                seen, comms, docs = self.calls()
                s.run(docs, args, call_comms=comms)
                self.assertEqual(len(seen["comms"]), 1)
                self.assertEqual(seen["docs"], [])

    def test_other_cursor_and_file_sources_never_reach_the_comms_path(self):
        for args in ({"query": "pallets", "cursor": "opaque-docs-cursor"},
                     {"query": "pallets", "sources": ["sharepoint"]}):
            with self.subTest(args=args):
                seen, comms, docs = self.calls()
                s.run(docs, args, call_comms=comms)
                self.assertEqual(seen["comms"], [])
                self.assertEqual(len(seen["docs"]), 1)

    def test_not_served_falls_back_and_other_errors_stay(self):
        for code in ("not_served", "not_ready"):
            seen, comms, docs = self.calls({"error": {"code": code, "message": "x"}})
            got = s.run(docs, {"query": "pallets"}, call_comms=comms)
            self.assertEqual(seen["docs"][0].get("sources"), None)   # the old path, whole ask
            self.assertEqual([d["citation"] for d in got["documents"]],
                             ["sharepoint://s/a.pdf"])
        seen, comms, docs = self.calls({"error": {"code": "invalid_input", "message": "x"}})
        self.assertEqual(s.run(docs, {"query": "pallets"}, call_comms=comms)["error"]["code"],
                         "invalid_input")
        self.assertEqual(seen["docs"], [])

    def test_file_hits_are_full_text_checked_and_comms_hits_decided_by_the_views(self):
        seen, comms, docs = self.calls()
        reads = []

        def read(arguments):
            reads.append(arguments["citation"])
            return {"text": {"untrusted_text": "booked with Freightcom"}}
        got = s.run(docs, {"query": "pallets"}, call_read=read, call_comms=comms)
        self.assertEqual(reads, ["sharepoint://s/a.pdf"])   # never a comms hit: the view's ai_ok
        self.assertEqual([d["citation"] for d in got["documents"]], ["email:v1"])
        self.assertEqual(got["egress_withheld"], 1)
        seen, comms, docs = self.calls()
        got = s.run(docs, {"query": "pallets"}, call_read=lambda a: None, call_comms=comms)
        self.assertEqual([d["citation"] for d in got["documents"]], ["email:v1"])
        self.assertEqual(got["unchecked"], 1)   # an unread file text is never shown

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
        self.assertEqual(s.open_one(read, {"citation": "email:v3"}, lambda a: {
            **opened, "text": {"untrusted_text": "Freightcom booked"}})["error"]["code"],
            "egress_withheld")
