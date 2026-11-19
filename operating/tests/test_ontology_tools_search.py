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
        self.assertTrue(all(w["max_chars"] == s.CHECK_PAGE_CHARS for w in reads))
        # out of time: the rest are left out unread
        from operating import call_limits
        from unittest import mock
        with mock.patch.object(call_limits, "seconds_left", lambda: 1.0):
            got = s.run(lambda a: docs, {"query": "invoice"}, read)
        self.assertEqual(got["documents"], [])
        self.assertEqual(got["unchecked"], 4)

    def test_full_texts_are_read_as_the_hosts_plx_read_takes_them(self):
        # Live 11-18: the check asked plx.read for 200,000 chars in one read; the host
        # refuses past its contract's maximum (invalid_input), so every hit was left out
        # unchecked and no search showed a document. Here the read refuses exactly as the
        # host does and pages as it does (text_window.next_offset).
        import json
        from pathlib import Path
        from ontology.json_contract import validate
        contract = json.loads((Path(s.__file__).with_name("ontology_tool_contracts.json"))
                              .read_text(encoding="utf-8"))
        schema = contract["tools"][s.READ]["input"]
        clean = "pallet shortage on line 2. " * 4000              # ~108k chars, 6 windows
        split = "." * (7000 - 4) + "Freightcom rates" + "y" * 100   # "Frei" | "ghtcom": an edge
        huge = "z " * s.CHECK_MAX_CHARS + "Freightcom"   # past the bound
        texts = {"original://clean": clean, "original://split": split,
                 "original://short": "PO 1 short note", "original://huge": huge}
        reads = []

        def read(wanted):
            reads.append(wanted)
            problems = validate(wanted, schema, root=contract)
            if problems:
                return {"error": {"code": "invalid_input", "message": "; ".join(problems)}}
            text = texts[wanted["citation"]]
            start = wanted.get("text_offset", 0)
            end = min(len(text), start + min(wanted.get("max_chars", 4000), 7000))  # < asked
            return {"status": "resolved", "content": {"untrusted_text": text[start:end]},
                    "text_window": {"offset": start, "chars": end - start,
                                    "total_chars": len(text),
                                    "next_offset": end if end < len(text) else None}}
        docs = {"documents": [{"citation": c, "title": "t", "snippet": "s"} for c in texts],
                "searched": {"sources": ["original"]}, "gaps": [], "completeness": {}}
        got = s.run(lambda a: docs, {"query": "pallet"}, read)
        self.assertEqual([h["citation"] for h in got["documents"]],
                         ["original://clean", "original://short", "original://huge"])
        self.assertEqual(got["egress_withheld"], 1)
        self.assertNotIn("unchecked", got)
        self.assertTrue(all(validate(w, schema, root=contract) == [] for w in reads))
        clean_reads = [w.get("text_offset", 0) for w in reads
                       if w["citation"] == "original://clean"]
        self.assertEqual(clean_reads, list(range(0, len(clean), 7000)))   # every window once
        self.assertLess(max(w.get("text_offset", 0) for w in reads
                            if w["citation"] == "original://huge"), s.CHECK_MAX_CHARS)
        # a later window refused or failing: not known, never shown
        for broken in ({"error": {"code": "not_readable"}}, None, RuntimeError("down")):
            def flaky(wanted, broken=broken):
                if wanted.get("text_offset"):
                    if isinstance(broken, Exception):
                        raise broken
                    return broken
                return read(wanted)
            got = s.run(lambda a: {**docs, "documents": docs["documents"][:1]},
                        {"query": "pallet"}, flaky)
            self.assertEqual((got["documents"], got["unchecked"]), ([], 1), broken)

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
        # files too: this comms leg did not search them (no ``searched``), so the old way
        self.assertEqual(seen["comms"][0]["sources"], ["graph_mail", "sharepoint", "teams"])
        self.assertEqual(seen["docs"][0]["sources"], ["sharepoint", "original"])

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
                         ["sharepoint://s/a.pdf", "email:v1", "api://c/1"])
        self.assertEqual(got["egress_withheld"], 1)          # the record naming Freightcom
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
        self.assertEqual(s.open_one(read, {"citation": "email:v3"}, lambda a: {
            **opened, "text": {"untrusted_text": "Freightcom booked"}})["error"]["code"],
            "egress_withheld")


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
