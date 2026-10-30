"""The case host's 22-tool listing after the host-budget merges (operating/case_tools_host.py).

Each retired case tool's
capability is reached through its successor, for the same reader and no wider:

* plx.source_citation -> plx.read: intra-page paging by text_offset with the whole
  page's SHA-256, and the original's extraction versions;
* plx.case_cite_mail -> plx.read cite_as + plx.case_add_finding (covered with the live
  Graph reader in test_case_live_citations.MailCitationTests);
* plx.case_link -> plx.case_add_finding link mode (test_case_links);
* plx.case_attach_sharepoint_original -> plx.case_attach_original drive_id + item_id
  (test_case_live_citations.SharePointOriginalTests);
* plx.document_metadata_proposals / plx.document_fields_confirm -> plx.document_fields
  (test_mcp_case_tools.DocumentMetadataToolTests, test_kind_confirmation);
* plx.case_verify_proposal -> plx.case_propose verify mode (test_case_verification);

plus plx.process on the slot that merge freed, and the case host's mail units:
``CaptureCaseHostTools`` grants ``mail:<mailbox>`` from the capture's audience check and
re-gates every message per item.

Evidence label: ``isolated-fixture`` and ``injected-transport`` (the mail capture is
filled over the capture tests' in-process FakeGraph). No network, no credentials.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, OUTSIDER,
                                                   READER_A, SCOPE, UPN_A, UPN_B, Clock,
                                                   FakeGraph,
                                                   capture_audience, reader_audience)
from ontology import mentions
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import MailCaptures, build_store
from operating.case_mail_scope import MailScope
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.case_tools_host import CaptureCaseHostTools, CaseCaptures, PagedRead, serving_tools
from operating.mcp_server import CASE_TOOL_RETIREMENTS, TOOLS, MCPServer
from operating.ontology_host import CaseHostTools
from operating.ontology_tools import load_contracts
from operating.tests import test_mcp_case_tools as case_tools

CONTRACTS = load_contracts()
NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)


def _client(server: MCPServer, name: str, arguments: dict) -> dict:
    """One tools/call as a client; the generic tools' results satisfy their contract."""
    server.request_id = getattr(server, "request_id", 1) + 1
    response = server.handle({"jsonrpc": "2.0", "id": server.request_id,
                              "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    result = response["result"]
    if not result["isError"] and name in CONTRACTS["tools"]:
        assert validate(result["structuredContent"], CONTRACTS["tools"][name]["output"],
                        root=CONTRACTS) == [], result["structuredContent"]
    return result


class ListingTests(unittest.TestCase):
    def test_the_host_lists_22_tools_and_every_retired_tool_names_a_listed_successor(self):
        names = [tool["name"] for tool in TOOLS]
        self.assertEqual(len(names), CONTRACTS["limits"]["max_case_host_tools"])
        self.assertEqual(len(names), 22)
        self.assertLessEqual({"plx.read", "plx.identity", "plx.documents", "plx.aggregate",
                              "plx.compare", "plx.process"}, set(names))
        for retired, plan in CASE_TOOL_RETIREMENTS.items():
            self.assertNotIn(retired, names)
            self.assertIn(plan["successor"], names, retired)
        process = next(tool for tool in TOOLS if tool["name"] == "plx.process")
        self.assertTrue(process["annotations"]["readOnlyHint"])
        self.assertEqual(process["inputSchema"]["properties"],
                         CONTRACTS["tools"]["plx.process"]["input"]["properties"])
        read = next(tool for tool in TOOLS if tool["name"] == "plx.read")
        self.assertLessEqual({"text_offset", "column_offset", "field_path", "page"},
                             set(read["inputSchema"]["properties"]))
        for tool in TOOLS:
            self.assertLessEqual(len(tool["description"]), 720, tool["name"])


class ReadParityTests(case_tools.CaseToolsTestCase):
    """plx.read over attached originals: paging inside a page and extraction versions,
    as plx.source_citation gave them, for the same reader only."""

    local_ocr = True

    def runtime(self, extractor=case_tools._two_page_extractor) -> CaseRuntime:
        runtime = CaseRuntime(self.state, client=case_tools.WorkbenchReadApiClient(
            case_tools.BASE, case_tools.KEY, fetch=self.fake), audience=self.audience_path,
            tenant="plx", mirror_options={"clock": self.clock, "retry_delay_seconds": 0},
            originals_options={"extractor": extractor, "local_ocr": self.local_ocr})
        self.addCleanup(runtime.close)
        return runtime

    def attach(self, server: MCPServer, runtime: CaseRuntime) -> tuple[str, dict]:
        return case_tools.OriginalReadTests.attach(self, server, runtime)

    def plx_read(self, server: MCPServer, arguments: dict) -> dict:
        result = self.call(server, "plx.read", arguments)
        self.assertFalse(result["isError"], result)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False,
                                            separators=(",", ":")).encode()),
                             case_tools.MAX_RESULT_BYTES)
        value = result["structuredContent"]
        self.assertEqual(validate(value, CONTRACTS["tools"]["plx.read"]["output"],
                                  root=CONTRACTS), [])
        return value

    def test_a_long_page_reads_to_the_end_by_text_offset_with_the_whole_page_digest(self):
        runtime = self.runtime()
        server = self.server(case_tools.OPS, runtime)
        _, original = self.attach(server, runtime)
        page = original["page_citations"][1]
        text, offset, pages = "", 0, 0
        while offset is not None:
            value = self.plx_read(server, {"citation": page, "text_offset": offset,
                                           "max_chars": 20000})
            window = value["text_window"]
            self.assertEqual((window["offset"], window["total_chars"]),
                             (offset, len(case_tools.PAGE_TWO)))
            self.assertEqual(value["content"]["origin"]["citation"], page)
            text += value["content"]["untrusted_text"]
            offset, pages = window["next_offset"], pages + 1
        self.assertGreater(pages, 1)
        self.assertEqual(text, case_tools.PAGE_TWO)
        self.assertEqual(window["sha256"],
                         hashlib.sha256(case_tools.PAGE_TWO.encode()).hexdigest())
        self.assertTrue(value["completeness"]["truncated"])
        # The short first page in one window: complete, digest over its whole text.
        first = self.plx_read(server, {"citation": original["page_citations"][0],
                                       "text_offset": 0})
        self.assertEqual((first["content"]["untrusted_text"], first["text_window"]["next_offset"],
                          first["completeness"]["complete"]),
                         (case_tools.PAGE_ONE, None, True))
        # OCR text stays labelled machine-read in a window too.
        self.assertIn("ocr_text_unconfirmed", {gap["reason"] for gap in first["gaps"]})
        beyond = self.plx_read(server, {"citation": page,
                                        "text_offset": len(case_tools.PAGE_TWO) + 1})
        self.assertEqual(beyond["error"]["code"], "invalid_input")

    def test_a_window_names_its_page_and_a_page_citation_plx_read_accepts_back(self):
        runtime = self.runtime()
        server = self.server(case_tools.OPS, runtime)
        _, original = self.attach(server, runtime)
        page_one, page_two = original["page_citations"][:2]
        value = self.plx_read(server, {"citation": page_two, "text_offset": 0,
                                       "max_chars": 200})
        window = value["text_window"]
        self.assertEqual((window["page"], window["page_citation"]), (2, page_two))
        # The window's own citation reads back to the same text window.
        again = self.plx_read(server, {"citation": window["page_citation"], "text_offset": 0,
                                       "max_chars": 200})
        self.assertEqual(again["content"]["untrusted_text"], value["content"]["untrusted_text"])
        self.assertEqual(again["text_window"]["sha256"], window["sha256"])
        first = self.plx_read(server, {"citation": page_one, "text_offset": 0})
        self.assertEqual((first["text_window"]["page"], first["text_window"]["page_citation"]),
                         (1, page_one))
        # A read of the whole file names no page: it never invents one.
        whole = self.plx_read(server, {"citation": original["citation"], "text_offset": 0})
        self.assertNotIn("page", whole["text_window"])
        self.assertNotIn("page_citation", whole["text_window"])

    def test_a_line_addressed_read_returns_the_pinned_line(self):
        runtime = self.runtime()
        server = self.server(case_tools.OPS, runtime)
        _, original = self.attach(server, runtime)
        page_one = original["page_citations"][0]
        lines = case_tools.PAGE_ONE.split("\n")
        self.assertGreater(len(lines), 1)
        for number, text in enumerate(lines, 1):
            value = self.plx_read(server, {"citation": page_one, "line": number})
            self.assertEqual(value["content"]["untrusted_text"], text)
            window = value["text_window"]
            self.assertEqual((window["first_line"], window["last_line"]), (number, number))
            self.assertEqual(window["page"], 1)
        # Lines are numbered in a page's whole text, not in the window.
        long_page = original["page_citations"][1]
        deep = self.plx_read(server, {"citation": long_page, "line": 4000})
        self.assertEqual(deep["content"]["untrusted_text"],
                         case_tools.PAGE_TWO.split("\n")[3999])
        self.assertEqual(deep["text_window"]["first_line"], 4000)
        # Plain windows say which lines they cover.
        plain = self.plx_read(server, {"citation": page_one, "text_offset": 0})
        self.assertEqual((plain["text_window"]["first_line"], plain["text_window"]["last_line"]),
                         (1, len(lines)))
        beyond = self.plx_read(server, {"citation": page_one, "line": len(lines) + 1})
        self.assertEqual(beyond["error"]["code"], "invalid_input")
        both = self.plx_read(server, {"citation": page_one, "line": 1, "text_offset": 0})
        self.assertEqual(both["error"]["code"], "invalid_input")

    def test_a_line_read_drops_a_crlf_ending_and_says_when_max_chars_cut_it(self):
        from types import SimpleNamespace
        paged = PagedRead(SimpleNamespace(contracts=CONTRACTS, limits={
            "max_untrusted_text_chars": 4000, "max_result_bytes": 64000}), None)
        text = "first line\r\nsecond line\r\nlast"
        raw = {"content": text, "content_field": "body",
               "content_citation": "graph-mail://BOX/MSG-1"}

        def window(arguments):
            return paged._window({"citation": raw["content_citation"]}, raw,
                                 dict(arguments, citation=raw["content_citation"]))

        for number, expected in enumerate(["first line", "second line", "last"], 1):
            out = window({"line": number})
            self.assertEqual(out["content"]["untrusted_text"], expected)
            self.assertIs(out["text_window"]["line_cut"], False)
        cut = window({"line": 2, "max_chars": 6})
        self.assertEqual(cut["content"]["untrusted_text"], "second")
        self.assertIs(cut["text_window"]["line_cut"], True)
        # Offset windows are not line reads and never carry line_cut.
        self.assertNotIn("line_cut", window({"text_offset": 0})["text_window"])

    def test_find_gives_the_line_and_offset_of_each_place_a_phrase_is_written(self):
        # A long document says it holds a thing on one line and that the thing is not
        # provided a thousand lines on: find locates both without paging between them.
        from types import SimpleNamespace
        paged = PagedRead(SimpleNamespace(contracts=CONTRACTS, limits={
            "max_untrusted_text_chars": 4000, "max_result_bytes": 64000}), None)
        lines = ["Intro", "", "The final proforma is held on file."] + ["filler"] * 1000 + [
            "Final\tPROFORMA: not provided."]
        text = "\n".join(lines)
        raw = {"content": text, "content_field": "body",
               "content_citation": "graph-mail://BOX/MSG-1"}
        found = paged._find({"citation": raw["content_citation"]}, raw, {
            "citation": raw["content_citation"], "find": "final  proforma"})
        hits = found["matches"]["hits"]
        self.assertEqual([hit["line"] for hit in hits], [3, len(lines)])
        for hit in hits:   # each snippet is the text at its offset, within its line
            self.assertEqual(hit["text"]["untrusted_text"],
                             text[hit["offset"]:hit["offset"] + hit["chars"]])
            self.assertEqual(hit["text"]["untrusted_text"], lines[hit["line"] - 1])
        self.assertEqual((found["matches"]["total_hits"], found["completeness"]["complete"]),
                         (2, True))
        self.assertNotIn("content", found)
        missing = paged._find({"citation": raw["content_citation"]}, raw, {
            "citation": raw["content_citation"], "find": "never written"})
        self.assertEqual((missing["matches"]["hits"], missing["matches"]["total_hits"]),
                         ([], 0))

    def test_a_file_citation_lists_every_extraction_version(self):
        self.local_ocr = False
        runtime = self.runtime(case_tools._no_ocr_extractor)
        server = self.server(case_tools.OPS, runtime)
        case_id, original = self.attach(server, runtime)
        before = self.plx_read(server, {"citation": original["citation"]})
        self.assertEqual(([e["version"] for e in before["extractions"]],
                          before["latest_extraction_version"]), ([1], 1))
        self.assertEqual(before["extractions"][0]["extraction_status"], "failed")
        self.local_ocr = True
        enabled = self.server(case_tools.OPS, self.runtime(case_tools._no_ocr_extractor))
        self.ok(enabled, "plx.case_attach_original", {
            "case_id": case_id, "citation": original["citation"], "reason": "OCR enabled"})
        after = self.plx_read(enabled, {"citation": original["citation"]})
        self.assertEqual([(e["version"], e["citation"]) for e in after["extractions"]],
                         [(1, original["citation"]),
                          (2, original["citation"] + "#extraction=2")])
        self.assertEqual((after["latest_extraction_version"],
                          after["extractions"][1]["page_count"]), (2, 2))
        # Version 2's own page reads (the attach-time page citation names version 1).
        page = self.plx_read(enabled, {"citation": original["citation"] + "#extraction=2&page=1",
                                       "text_offset": 0})
        self.assertEqual(page["content"]["untrusted_text"], case_tools.PAGE_ONE)

    def test_a_key_inside_a_json_column_reads_whole_by_field_path_and_text_offset(self):
        text = "memo line; " * 3000  # longer than one envelope
        orders = self.fake.tables["public.app_sales_order"]
        orders[0]["custom_fields"] = {"memo": text}
        server = self.server(case_tools.OPS, self.runtime())
        rows = self.read(server, "mirror.rows", {"relation": "public.app_sales_order",
                                                 "where": [["order_number", "eq",
                                                            orders[0]["order_number"]]]})
        citation = rows["rows"][0]["citation"]
        path, read, offset = "row.custom_fields.memo", "", 0
        while offset is not None:
            value = self.plx_read(server, {"citation": citation, "field_path": path,
                                           "text_offset": offset})
            self.assertEqual(value["text_window"]["field_path"], path)
            read += value["fields"][path]["untrusted_text"]
            offset = value["text_window"]["next_offset"]
        self.assertEqual(read, text)
        self.assertEqual(value["text_window"]["sha256"], hashlib.sha256(text.encode()).hexdigest())
        missing = self.plx_read(server, {"citation": citation, "text_offset": 0,
                                         "field_path": "row.custom_fields.absent"})
        self.assertEqual(missing["error"]["code"], "no_text")

    def test_paging_is_refused_outside_the_case_host_never_ignored(self):
        runtime = self.runtime()
        server = self.server(case_tools.OPS, runtime)
        citation = self.ao_citation(server)
        tools = CaseHostTools(runtime, server.actor)
        self.addCleanup(tools.close)
        for paging in ({"text_offset": 0}, {"column_offset": 0}, {"find": "AO"}):
            answer = tools.call("plx.read", {"citation": citation, **paging}, server.actor)
            self.assertEqual(answer["error"]["code"], "paging_not_served", paging)
        self.assertEqual(tools.call("plx.read", {"citation": citation}, server.actor)
                         ["status"], "resolved")

    def test_a_reader_outside_the_audience_gets_nothing_more_than_the_generic_read(self):
        runtime = self.runtime()
        _, original = self.attach(self.server(case_tools.OPS, runtime), runtime)
        outsider = self.server(case_tools.OUTSIDER, runtime)
        for arguments in ({"citation": original["citation"]},
                          {"citation": original["page_citations"][1], "text_offset": 0}):
            value = self.plx_read(outsider, arguments)
            self.assertEqual(value["status"], "unresolvable")
            for key in ("content", "fields", "extractions", "text_window", "digest"):
                self.assertNotIn(key, value)
        row = self.plx_read(outsider, {"citation": self.ao_citation(
            self.server(case_tools.OPS, runtime)), "column_offset": 0})
        self.assertEqual(row["status"], "withheld")
        self.assertNotIn("columns", row)


def _paged_extractor(count: int, pad: int = 0, figure_last: bool = False):
    """An extractor of ``count`` pages, page n reading ``Page n holds <figure n>.`` (then
    ``pad`` characters of words; ``figure_last``: the words first, the figure at the page's
    end): each page's figure occurs on that page only."""
    filler = ("and more words " * (pad // 15 + 1))[:pad]
    texts = [(f"Page {n} {filler.strip()} holds {_figure(n)}." if figure_last
              else f"Page {n} holds {_figure(n)}. {filler}".rstrip())
             for n in range(1, count + 1)]

    def extract(content, mime_type, *, original_url, filename="", local_pdf_ocr=False):
        pages, start = [], 0
        for number, text in enumerate(texts, 1):
            pages.append({"page": number, "start": start, "end": start + len(text)})
            start += len(text) + 1
        return case_tools.ExtractedDocument("application/pdf", "x", "extracted", "stub",
                                            "\f".join(texts), (), (), (), tuple(pages))
    return extract, texts


def _figure(page: int) -> int:
    return 4000 + 37 * page


def _page_answer(handle: str, value: int) -> dict:
    text = f"The page reads {value}."
    return {"answer": {"claims": [{"text": text, "citations": [handle], "value_claims": [
        {"value": str(value), "citation": handle, "field": "text"}]}], "text": text}}


class PageCitationTests(case_tools.CaseToolsTestCase):
    """plx.read returns the page citations of the text it serves (a page read its page's,
    a whole-text read and a text_offset window every page they cover), the session takes
    them as returned evidence, and the answer gate still checks a page claim against that
    page's own text only."""

    local_ocr = True

    def runtime(self, count: int = 3, pad: int = 0, figure_last: bool = False) -> CaseRuntime:
        extractor, self.texts = _paged_extractor(count, pad, figure_last)
        return ReadParityTests.runtime(self, extractor=extractor)

    def attach(self, server: MCPServer, runtime: CaseRuntime) -> tuple[str, dict]:
        return case_tools.OriginalReadTests.attach(self, server, runtime)

    def plx_read(self, server: MCPServer, arguments: dict) -> dict:
        return ReadParityTests.plx_read(self, server, arguments)

    def submit(self, server: MCPServer, handle: str, value: int) -> dict:
        result = _client(server, "plx.case_get", _page_answer(handle, value))
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def codes(self, answer: dict) -> list[str]:
        return [f["code"] for c in answer.get("unsupported_claims") or ()
                for f in c.get("failures") or ()]

    def test_a_page_read_cites_its_page_and_a_figure_on_it_passes_the_gate(self):
        runtime = self.runtime()
        server = self.server(case_tools.OPS, runtime)
        _, original = self.attach(server, runtime)
        file, page_three = original["citation"], original["page_citations"][2]
        value = self.plx_read(server, {"citation": file, "page": 3})
        self.assertEqual(value["citation"], page_three)
        self.assertEqual(value["content"]["origin"]["citation"], page_three)
        self.assertEqual(value["content"]["untrusted_text"], self.texts[2])
        self.assertNotIn("pages", value)
        passed = self.submit(server, page_three, _figure(3))
        self.assertEqual(passed["outcome"], "answered", json.dumps(passed)[:1500])
        # A page past the last: nothing is returned, so nothing of it can be cited.
        missing = self.plx_read(server, {"citation": file, "page": 9})
        self.assertNotEqual(missing["status"], "resolved")
        self.assertEqual(missing["citation"], file)
        refused = self.submit(server, file + "#page=9", _figure(3))
        self.assertIn("cites_what_this_run_did_not_return", json.dumps(refused))

    def test_a_claim_citing_one_page_with_the_figure_of_another_is_refused(self):
        runtime = self.runtime()
        server = self.server(case_tools.OPS, runtime)
        _, original = self.attach(server, runtime)
        file, page_three = original["citation"], original["page_citations"][2]
        whole = self.plx_read(server, {"citation": file})
        self.assertIn(page_three, [p["page_citation"] for p in whole["pages"]])
        self.assertIn(str(_figure(2)), whole["content"]["untrusted_text"])
        refused = self.submit(server, page_three, _figure(2))
        self.assertNotEqual(refused["outcome"], "answered")
        self.assertEqual(self.codes(refused), ["claim_value_mismatch"])
        self.assertNotIn("cites_what_this_run_did_not_return", json.dumps(refused))

    def test_a_window_read_lists_exactly_the_pages_its_text_covers(self):
        for count in (1, 10, 60):
            with self.subTest(pages=count):
                self.setUp()
                runtime = self.runtime(count)
                server = self.server(case_tools.OPS, runtime)
                _, original = self.attach(server, runtime)
                file, cited = original["citation"], original["page_citations"]
                text = "\f".join(self.texts)
                for arguments in ({"citation": file}, {"citation": file, "text_offset": 0,
                                                       "max_chars": 20000}):
                    value = self.plx_read(server, arguments)
                    pages = value["pages"]
                    # Every page once, in order, each span the page's own text.
                    self.assertEqual([p["page"] for p in pages], list(range(1, count + 1)))
                    self.assertEqual([p["page_citation"] for p in pages], cited)
                    for entry, own in zip(pages, self.texts):
                        self.assertEqual(text[entry["offset"]:entry["offset"]
                                              + entry["chars"]], own)
                # A window inside the last page names that page alone.
                last = len(text) - len(self.texts[-1])
                window = self.plx_read(server, {"citation": file, "text_offset": last + 2,
                                                "max_chars": 200})
                self.assertEqual([p["page"] for p in window["pages"]], [count])
                self.assertNotIn("page", window["text_window"])
                # The page cited from the window passes; a page not covered was never
                # returned by the window and is refused as such.
                fresh = self.server(case_tools.OPS, runtime)
                self.plx_read(fresh, {"citation": file, "text_offset": last + 2,
                                      "max_chars": 200})
                passed = self.submit(fresh, cited[-1], _figure(count))
                self.assertEqual(passed["outcome"], "answered", json.dumps(passed)[:1500])
                if count > 1:
                    other = self.server(case_tools.OPS, runtime)
                    self.plx_read(other, {"citation": file, "text_offset": last + 2,
                                          "max_chars": 200})
                    refused = self.submit(other, cited[0], _figure(1))
                    self.assertIn("cites_what_this_run_did_not_return", json.dumps(refused))

    def test_page_citations_that_do_not_fit_are_cut_with_a_gap_never_silently(self):
        runtime = self.runtime(600)
        server = self.server(case_tools.OPS, runtime)
        _, original = self.attach(server, runtime)
        file = original["citation"]
        cited = [f"{file}#page={n}" for n in range(1, 601)]
        for arguments in ({"citation": file}, {"citation": file, "text_offset": 0,
                                               "max_chars": 20000}):
            value = self.plx_read(server, arguments)
            listed = [p["page_citation"] for p in value["pages"]]
            self.assertGreater(len(listed), 0)
            self.assertEqual(listed, cited[:len(listed)])   # in order, none lost inside
            (gap,) = [g for g in value["gaps"] if g["reason"] == "page_citations_cut"]
            self.assertEqual(gap["subject"], cited[len(listed)])
            # The text keeps its place: at least as much as the pages listed cover.
            last = value["pages"][-1]
            self.assertGreaterEqual(len(value["content"]["untrusted_text"]),
                                    min(8000, last["offset"]))

    def test_a_page_served_only_in_part_makes_a_claim_on_it_partial(self):
        # Each page prints its figure at its end, past 900 characters of words: a part of
        # the page that stops before the figure never served it (a figure the served part
        # holds reads in full: ``answer_gate._in_full``, the case below the loop).
        runtime = self.runtime(3, pad=900, figure_last=True)
        server = self.server(case_tools.OPS, runtime)
        _, original = self.attach(server, runtime)
        file, (one, two, _three) = original["citation"], original["page_citations"]
        state = lambda answer: answer["answerability"]["state"]   # noqa: E731
        # A page read cut by max_chars before the figure: the page is partly read.
        cut = self.plx_read(server, {"citation": file, "page": 2, "max_chars": 200})
        self.assertTrue(cut["content"]["truncated"])
        self.assertNotIn(str(_figure(2)), cut["content"]["untrusted_text"])
        self.assertTrue(server.unread(two, "text"))
        partial = self.submit(server, two, _figure(2))
        self.assertEqual((partial["outcome"], state(partial)), ("answered", "partial"),
                         json.dumps(partial)[:1500])
        # Read whole, the page is read: answerable.
        whole = self.server(case_tools.OPS, runtime)
        self.plx_read(whole, {"citation": file, "page": 2, "max_chars": 200})
        self.plx_read(whole, {"citation": two, "text_offset": 0, "max_chars": 20000})
        self.assertFalse(whole.unread(two, "text"))
        self.assertEqual(state(self.submit(whole, two, _figure(2))), "answerable")
        # A window of the whole text holding page 1 whole and the start of page 2.
        reach = len(self.texts[0]) + 1 + 40
        for page, cited, expected in ((1, one, "answerable"), (2, two, "partial")):
            session = self.server(case_tools.OPS, runtime)
            window = self.plx_read(session, {"citation": file, "text_offset": 0,
                                             "max_chars": reach})
            self.assertEqual([p["page"] for p in window["pages"]], [1, 2])
            self.assertEqual(session.unread(cited, "text"), expected == "partial")
            answer = self.submit(session, cited, _figure(page))
            self.assertEqual((answer["outcome"], state(answer)), ("answered", expected),
                             json.dumps(answer)[:1500])
        # The default read of the whole file, cut at max_chars, holds page 1 whole only.
        session = self.server(case_tools.OPS, runtime)
        default = self.plx_read(session, {"citation": file, "max_chars": reach})
        self.assertTrue(default["content"]["truncated"])
        self.assertEqual((session.unread(one, "text"), session.unread(two, "text")),
                         (False, True))
        # A window of page 2 that ends after its figure: still part of the page, but the
        # claim's figure was served whole, so the claim reads in full.
        session = self.server(case_tools.OPS, runtime)
        tail = self.plx_read(session, {"citation": two,
                                       "text_offset": len(self.texts[1]) - 200})
        self.assertIn(str(_figure(2)), tail["content"]["untrusted_text"])
        self.assertTrue(session.unread(two, "text"))
        self.assertEqual(state(self.submit(session, two, _figure(2))), "answerable")

    def test_a_find_hit_is_served_text_of_its_page_and_bounded(self):
        # Each page prints its figure past 900 characters of words: a find of the figure
        # serves only its snippet, on the page it is on, and a claim on that page's figure
        # reads in full from it (``answer_gate._in_full``), as from a window.
        runtime = self.runtime(3, pad=900, figure_last=True)
        server = self.server(case_tools.OPS, runtime)
        _, original = self.attach(server, runtime)
        file, three = original["citation"], original["page_citations"][2]
        found = self.plx_read(server, {"citation": file, "find": f"holds {_figure(3)}"})
        (hit,) = found["matches"]["hits"]
        whole = "\f".join(self.texts)
        self.assertEqual(hit["text"]["untrusted_text"],
                         whole[hit["offset"]:hit["offset"] + hit["chars"]])
        self.assertEqual((hit["page"], hit["page_citation"], hit["text"]["origin"]["citation"]),
                         (3, three, three))
        self.assertEqual(hit["text"]["untrusted_text"],
                         self.texts[2][hit["page_offset"]:hit["page_offset"] + hit["chars"]])
        self.assertLess(hit["chars"], 200)
        self.assertNotIn("content", found)
        self.assertTrue(server.unread(three, "text"))
        answer = self.submit(server, three, _figure(3))
        self.assertEqual((answer["outcome"], answer["answerability"]["state"]),
                         ("answered", "answerable"), json.dumps(answer)[:1500])
        # Its offset reads on from it.
        window = self.plx_read(self.server(case_tools.OPS, runtime), {
            "citation": file, "text_offset": hit["offset"], "max_chars": 200})
        self.assertTrue(window["content"]["untrusted_text"].startswith(
            hit["text"]["untrusted_text"]))
        # At most 50 hits, all of them counted, the list said cut.
        many = self.plx_read(server, {"citation": file, "find": "MORE words"})
        self.assertEqual(len(many["matches"]["hits"]), 50)
        self.assertEqual(many["matches"]["total_hits"], whole.count("more words"))
        self.assertGreater(many["matches"]["total_hits"], 50)
        self.assertEqual((many["completeness"]["truncated"], many["completeness"]["reason"]),
                         (True, "find_hits_cut"))
        offsets = [h["offset"] for h in many["matches"]["hits"]]
        self.assertEqual(offsets, sorted(offsets))
        both = self.plx_read(server, {"citation": file, "find": "holds", "text_offset": 0})
        self.assertEqual(both["error"]["code"], "invalid_input")
        # A reader outside the audience gets the generic refusal: no hit, page or text.
        outsider = self.plx_read(self.server(case_tools.OUTSIDER, runtime),
                                 {"citation": file, "find": f"holds {_figure(3)}"})
        self.assertEqual(outsider["status"], "unresolvable")
        self.assertNotIn("matches", outsider)
        self.assertNotIn("page=", json.dumps(outsider))
        self.assertNotIn(str(_figure(3)), json.dumps(outsider))

    def test_a_reader_outside_the_audience_gets_no_page_citation(self):
        runtime = self.runtime()
        _, original = self.attach(self.server(case_tools.OPS, runtime), runtime)
        outsider = self.server(case_tools.OUTSIDER, runtime)
        file, page_three = original["citation"], original["page_citations"][2]
        for arguments in ({"citation": file, "page": 3}, {"citation": file},
                          {"citation": file, "text_offset": 0}):
            value = self.plx_read(outsider, arguments)
            self.assertEqual(value["status"], "unresolvable")
            self.assertEqual(value["citation"], file)
            self.assertNotIn("pages", value)
            self.assertNotIn("page=", json.dumps(value))
        refused = self.submit(outsider, page_three, _figure(3))
        self.assertNotEqual(refused["outcome"], "answered")


class AgendaOnTheCaseHostTests(unittest.TestCase):
    """plx.agenda end to end through the case host (``serving_tools`` -> ``CaseHostTools``
    tool table) on the synthetic R&D store of operating/tests/test_rnd_process_maps.py:
    units are granted by the runtime's live audience, Teams messages pass the runtime's
    citation gate, pii by the host's marking grants."""

    class Runtime:
        """The case runtime surface the host reads: audiences, employee gate, item gate."""

        def __init__(self, allowed):
            self.allowed = allowed   # principal -> predicate over audience resources
            allows = lambda actor, resource: any(  # noqa: E731
                self.allowed.get(p, lambda r: False)(resource) for p in actor.principals)
            self.mirror = mock.Mock(_audience=lambda: mock.Mock(allows=allows))
            self.live = mock.Mock(audience=mock.Mock(allows=allows), bc=None, mail=None)
            self.client_configured = False

        def employee_gate(self, actor):
            return actor

        def resolve_citation(self, handle, actor):
            if not any(self.allowed.get(p, lambda r: False)(handle) for p in actor.principals):
                raise PermissionError(handle)
            return {}

    def test_plx_agenda_answers_through_the_case_host_for_each_reader(self):
        from operating.meeting_agenda import TOOL_NAME
        from operating.tests import test_rnd_process_maps as rnd
        rnd.Fixture.build()
        full, chat = next(iter(rnd.FULL.principals)), next(iter(rnd.CHAT_ONLY.principals))
        meeting = f"teams:chat:{rnd.MEETING}"
        runtime = self.Runtime({full: lambda r: True,
                                chat: lambda r: r == meeting or (
                                    r.startswith(rnd.chat_handle(rnd.MEETING))
                                    and rnd.HIDDEN_MESSAGE not in r)})
        arguments = {"agenda": "rnd_biweekly", "scope": rnd.SCOPE}

        def agenda(actor):
            host = serving_tools(runtime, actor, store_path=rnd.Fixture.path,
                                 marking_grants={"pii": [full]})
            self.addCleanup(host.close)
            return host.call(TOOL_NAME, arguments, actor)
        out = agenda(rnd.FULL)
        self.assertEqual(out["since"]["basis"], "previous_agenda")
        self.assertEqual({rnd._native(l) for l in out["open_projects"]},
                         {"TASK-S1", "TASK-S2", "TASK-S4"})
        self.assertEqual([rnd._native(l) for l in out["decisions_needed"]], ["apg_s4"])
        self.assertIn(rnd.HIDDEN_MESSAGE, {rnd._native(l) for l in out["changes"]})
        self.assertTrue(all(l["citations"] for l in out["changes"]))
        self.assertTrue(any(l["owner"] for l in out["open_projects"]))
        # The chat-only reader: its two admitted messages, nothing of MC or the QA chat.
        narrow = agenda(rnd.CHAT_ONLY)
        self.assertEqual({rnd._native(l) for l in narrow["changes"]}, {"m-after-1", "m-after-2"})
        self.assertEqual(narrow["open_projects"] + narrow["decisions_needed"], [])
        text = json.dumps(narrow)
        for hidden in (rnd.HIDDEN_MESSAGE, "TASK-S", "q-1", "synth.example"):
            self.assertNotIn(hidden, text)


class MailPinGapTests(unittest.TestCase):
    """plx.read's mail body pin (``PagedRead._extras``): a pin it cannot offer is always
    a gap, also when the extras do not fit and the generic read is returned."""

    HANDLE = "graph-mail://mailbox-a/MSG-1@CK-1"

    def paged(self, resolve, max_result_bytes=None) -> PagedRead:
        evidence = mock.Mock(contracts=CONTRACTS, limits=dict(CONTRACTS["limits"]))
        if max_result_bytes is not None:
            evidence.limits["max_result_bytes"] = max_result_bytes
        return PagedRead(evidence, mock.Mock(resolve_citation=resolve))

    def result(self, text="body") -> dict:
        return {"citation": self.HANDLE, "status": "resolved", "gaps": [],
                "content": {"untrusted_text": text, "origin": {"citation": self.HANDLE}}}

    def extras(self, paged: PagedRead, result: dict) -> dict:
        raw = {"values_citation": self.HANDLE, "content_pin": self.HANDLE + "#body_sha256="
               + "a" * 64 + "&shown_sha256=" + "b" * 64}
        return paged._extras(result, raw, {"citation": self.HANDLE}, object())

    def test_a_changed_body_keeps_cite_as_withheld_when_the_extras_do_not_fit(self):
        result = self.result("x" * 4000)
        changed = lambda handle, actor: {"status": "changed_since"}  # noqa: E731
        fitting = self.extras(self.paged(changed), result)
        self.assertEqual([g["reason"] for g in fitting["gaps"]], ["cite_as_withheld"])
        from operating import ontology_tools as ot
        tight = self.paged(changed, max_result_bytes=ot._envelope_bytes(result) + 16)
        fallback = self.extras(tight, result)
        self.assertNotIn("cite_as", fallback)
        self.assertEqual([g["reason"] for g in fallback["gaps"]], ["cite_as_withheld"])
        self.assertEqual(fallback["content"], result["content"])

    def test_an_exception_while_resolving_the_pin_is_a_gap_not_silence(self):
        def broken(handle, actor):
            raise RuntimeError("store unavailable")
        out = self.extras(self.paged(broken), self.result())
        self.assertNotIn("cite_as", out)
        (gap,) = out["gaps"]
        self.assertEqual((gap["state"], gap["reason"], gap["subject"]),
                         ("unknown", "cite_as_unresolvable", self.HANDLE))
        self.assertIn("RuntimeError", gap["detail"])
        self.assertEqual(validate(gap, CONTRACTS["$defs"]["gap"], root=CONTRACTS), [])


class ProcessOnTheHostTests(case_tools.CaseToolsTestCase):
    def test_plx_process_answers_through_the_host_under_its_contract(self):
        server = self.server(case_tools.OPS, self.runtime())
        result = _client(server, "plx.process",
                         {"from": "PurchaseOrder:portal.app_purchase_order:x"})
        self.assertFalse(result["isError"], result)
        self.assertIn("error", result["structuredContent"])  # nothing projected: no root
        refused = _client(server, "plx.process", {"from": "x", "extra": 1})
        self.assertEqual(refused["structuredContent"]["error"]["code"], "invalid_input")


class LinkModeTests(case_tools.CaseToolsTestCase):
    def test_link_mode_takes_only_the_link_and_refuses_a_partial_finding(self):
        server = self.server(case_tools.OPS, self.runtime())
        case_id, citation = self.open_case_with_fact(server)
        link = {"from_citation": citation, "to_citation": citation,
                "contract_id": "fm-ao-link-to-assembly-order"}
        self.assertIn("link_mode_takes_only", self.refused(server, "plx.case_add_finding", {
            "case_id": case_id, "link": link, "statement": "x"}))
        self.assertIn("unknown_arguments", self.refused(server, "plx.case_add_finding", {
            "case_id": case_id, "link": dict(link, note="x")}))
        self.assertIn("missing_argument:statement", self.refused(
            server, "plx.case_add_finding", {"case_id": case_id}))


class MailUnitsOnTheCaseHostTests(unittest.TestCase):
    """Projected mail on the case host: the capture's audience check grants the mailbox
    unit for the reader, and every message is re-gated per item."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "mail"
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-1", "CK-1-1", body="Please confirm our PO100234 by Friday.",
                  subject="Order confirmation")
        graph.put(BOX_B, "MSG-B1", "CKB-1", body="Finance note on PO100234.",
                  subject="Finance")
        store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        assert run["complete"], run
        store.close()
        cls.projection = Path(cls.tmp.name) / "ontology.sqlite"
        with MailCaptures(cls.state, internal_domains=SCOPE.internal_domains) as mail:
            build_store(cls.projection, schema_loader.load_schema(), {"graph_mail": mail},
                        clock=lambda: NOW,
                        post_project={"mentions": mentions.store_mention_links})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.audience = self.root / "audience.json"
        self.audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                             "audiences": {}}), encoding="utf-8")

    def server(self, actor, *, scope=None, host=CaptureCaseHostTools,
               exclusion=None) -> MCPServer:
        runtime = CaseRuntime(self.root / "state", client=None, audience=self.audience,
                              tenant="plx", live=LiveReaders(audience=reader_audience()),
                              mail_scope=scope or MailScope.unrestricted(),
                              people_exclusion=exclusion or PeopleExclusion.from_env(ENV))
        self.addCleanup(runtime.close)
        self.runtime = runtime
        options = {"mail_capture_dir": self.state} if host is CaptureCaseHostTools else {}
        if options:   # case citations of the capture, as SessionHost wires them
            runtime.captures = CaseCaptures(runtime, **options)
        tools = host(runtime, actor, store_path=self.projection, **options)
        server = MCPServer(case_runtime=runtime, actor=actor, ontology=tools)
        self.addCleanup(server.close)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "mail-host-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def documents(self, server: MCPServer) -> dict:
        result = _client(server, "plx.documents", {"query": "PO100234",
                                                   "sources": ["graph_mail"]})
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    @staticmethod
    def messages(answer: dict) -> set[str]:
        return {doc["citation"].split("/")[3].split("@")[0]
                for doc in answer.get("documents") or ()}

    def test_a_reader_sees_the_mail_of_the_mailboxes_the_capture_admits_and_no_more(self):
        answer = self.documents(self.server(READER_A))
        self.assertEqual(self.messages(answer), {"MSG-1"})
        handle = next(doc["citation"] for doc in answer["documents"])
        self.assertTrue(handle.startswith(f"graph-mail://{BOX_A}/MSG-1@"))
        read = _client(self.server(READER_A), "plx.read", {"citation": handle})
        self.assertEqual(read["structuredContent"]["status"], "resolved")
        self.assertIn("PO100234", read["structuredContent"]["content"]["untrusted_text"])

    def test_an_outsider_sees_no_mail_and_the_search_says_it_was_partial(self):
        answer = self.documents(self.server(OUTSIDER))
        self.assertEqual(self.messages(answer), set())
        self.assertIn("inaccessible", {gap["state"] for gap in answer["gaps"]})
        handle = f"graph-mail://{BOX_A}/MSG-1@CK-1-1"
        self.assertEqual(_client(self.server(OUTSIDER), "plx.read", {"citation": handle})
                         ["structuredContent"]["status"], "withheld")

    def test_without_the_mail_grant_the_case_host_lists_no_mail(self):
        self.assertEqual(self.messages(self.documents(self.server(
            READER_A, host=CaseHostTools))), set())

    def test_a_mailbox_grant_never_exposes_a_message_the_employee_acl_withholds(self):
        server = self.server(READER_A)
        self.assertEqual(server.ontology.policy_now(READER_A).audiences[f"mail:{BOX_A}"],
                         frozenset({"staff:orders"}))  # the unit is granted
        with mock.patch.object(self.runtime, "_employee_acl_denial",
                               return_value="employee_acl_denied"):
            answer = self.documents(server)
            read = _client(server, "plx.read",
                           {"citation": f"graph-mail://{BOX_A}/MSG-1@CK-1-1"})
        self.assertEqual(self.messages(answer), set())
        self.assertNotEqual(read["structuredContent"].get("status"), "resolved")
        self.assertNotIn("content", read["structuredContent"])

    def test_a_mailbox_whose_messages_the_item_gate_refuses_is_not_granted(self):
        # The search scope (hybrid search filters by these grants in SQL) never holds a
        # mailbox every message of which the item gate refuses: an undetermined shared
        # mailbox ACL, the people guard down, or a decision that fails.
        server = self.server(READER_A)
        unit = f"mail:{BOX_A}"
        for patch in (mock.patch.object(self.runtime, "_employee_acl_denial",
                                        return_value="undetermined: shared mailbox"),
                      mock.patch("operating.ontology_tools_evidence.RuntimeCitations"
                                 "._exclusion_refusal", return_value="guard_down"),
                      mock.patch("operating.ontology_tools_evidence.RuntimeCitations"
                                 ".visible_mailboxes", side_effect=RuntimeError("boom"))):
            with self.subTest(patch=patch), patch:
                self.assertEqual(server.ontology.policy_now(READER_A).audiences[unit],
                                 frozenset())
                self.assertEqual(self.messages(self.documents(server)), set())
        # admitted again, the same unit is granted (narrowing only, never widening)
        self.assertEqual(server.ontology.policy_now(READER_A).audiences[unit],
                         frozenset({"staff:orders"}))
        self.assertEqual(server.ontology.policy_now(OUTSIDER).audiences.get(unit, frozenset()),
                         frozenset())

    def test_a_case_finding_cites_a_captured_message_through_the_same_gate(self):
        handle = f"graph-mail://{BOX_A}/MSG-1@CK-1-1"
        server = self.server(READER_A)
        case_id = _client(server, "plx.case_open", {"question": "Was PO100234 confirmed?"}
                          )["structuredContent"]["case_id"]
        added = _client(server, "plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported",
            "statement": "The supplier asked to confirm the order.", "citations": [handle]})
        self.assertFalse(added["isError"], added)
        view = _client(server, "plx.case_get", {"case_id": case_id})["structuredContent"]
        (finding,) = view["findings"]["supported"]
        (cited,) = finding["citations"]
        # The case records the digest plx.read reports for the captured message.
        read = _client(server, "plx.read", {"citation": handle})["structuredContent"]
        self.assertEqual((cited["status"], cited["digest"]), ("resolved", read["digest"]))
        # The same reader outside the case mail scope: the finding is withheld.
        outside_scope = MailScope(["someone-else@example-plx.com"],
                                  internal_domains=["example-plx.com"])
        view = _client(self.server(READER_A, scope=outside_scope), "plx.case_get",
                       {"case_id": case_id})["structuredContent"]
        self.assertEqual([f["statement"] for f in view["findings"]["withheld"]], [None])
        self.assertNotIn(handle, json.dumps(view))

    def test_a_message_superseded_with_no_current_version_is_never_current_support(self):
        # Removed at the source: the capture supersedes the version and holds none newer.
        self.state = self.root / "mail"
        shutil.copytree(type(self).state, self.state)
        handle = f"graph-mail://{BOX_A}/MSG-1@CK-1-1"
        server = self.server(READER_A)
        case_id = _client(server, "plx.case_open", {"question": "Was PO100234 confirmed?"}
                          )["structuredContent"]["case_id"]
        self.assertFalse(_client(server, "plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported",
            "statement": "The supplier asked to confirm the order.",
            "citations": [handle]})["isError"])
        store = MailCaptureStore(self.state, clock=Clock())
        self.assertEqual(store._supersede_current(BOX_A, "MSG-1", NOW.isoformat(),
                                                  "removed"), 1)
        store.db.commit()
        store.close()
        view = _client(self.server(READER_A), "plx.case_get",
                       {"case_id": case_id})["structuredContent"]
        self.assertEqual(view["findings"]["supported"], [])
        (finding,) = view["findings"]["citation_content_changed"]
        (cited,) = finding["citations"]
        self.assertEqual((cited["handle"], cited["status"]), (handle, "citation_content_changed"))

    def test_every_message_is_re_gated_per_item_by_the_case_mail_scope(self):
        # The capture's audience admits mailbox A for this reader, but the case mail
        # scope does not: the unit is granted, the item gate still denies each message.
        outside_scope = MailScope(["someone-else@example-plx.com"],
                                  internal_domains=["example-plx.com"])
        self.assertFalse(outside_scope.allows([UPN_A]))
        answer = self.documents(self.server(READER_A, scope=outside_scope))
        self.assertEqual(self.messages(answer), set())


    def test_a_search_never_lists_mail_of_an_excluded_person_or_without_a_guard(self):
        # The search decides each message on the capture's rows; the people exclusion
        # (PLX_EXCLUDED_PEOPLE_OIDS) still holds there, and a guard that cannot decide
        # serves no captured message.
        self.assertEqual(self.messages(self.documents(self.server(READER_A))), {"MSG-1"})
        excluded = PeopleExclusion.from_env({**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_A})
        answer = self.documents(self.server(READER_A, exclusion=excluded))
        self.assertEqual(self.messages(answer), set())
        self.assertNotIn("MSG-1", json.dumps(answer))
        undecided = PeopleExclusion(unavailable="excluded_people_oids_absent")
        self.assertEqual(self.messages(self.documents(self.server(READER_A,
                                                                  exclusion=undecided))),
                         set())


class CapturedTextStatusLabelTests(unittest.TestCase):
    """A typed OCR failure (``ocr_failed:<cause>``) is labelled like a plain one."""

    def test_a_typed_ocr_failure_is_a_not_ingested_gap_like_a_plain_one(self):
        host = object.__new__(CaptureCaseHostTools)
        host._served = {f"h-{status}": {
            "captured_at": "2026-01-01T00:00:00Z", "basis": "recorded", "changed": False,
            "failure": "", "text_status": status, "content_status": "", "size": 10}
            for status in ("ocr_failed", "ocr_failed:deadline")}
        for status in ("ocr_failed", "ocr_failed:deadline"):
            gaps = {(gap["reason"], gap["state"]) for gap in host._labels(f"h-{status}")}
            self.assertIn(("ocr_failed", "not_ingested"), gaps, status)


class CapturedDocumentDirectionTests(unittest.TestCase):
    """``CaseCaptures.document`` serves a mail attachment with the sender provenance the
    mail projection reads its direction from, so the customer-PO guard reads the same
    direction: the mailbox's own send is ``own_issued`` and refused, a received PO and
    its forward are the customer's."""

    def test_an_attachment_the_mailbox_sent_itself_is_no_customer_po_for_the_guard(self):
        from integrations.tests.test_capture_mail import file_attachment
        from operating import capability_guards as cg
        from operating.tests import test_attested_missing as am
        po = am.pdf(am.customer_po("4500012345", "OR1000777001"))
        forwarded = am.pdf(am.customer_po("4500012399", "OR1000777002"))
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-SENT", "CK-1", subject="PO", body="attached",
                  attachments=[file_attachment("ATT-S", po, "po sent.pdf")])
        graph.messages[BOX_A]["MSG-SENT"]["from"] = {"emailAddress": {"address": UPN_A}}
        graph.put(BOX_A, "MSG-CUST", "CK-2", subject="PO", body="attached",
                  attachments=[file_attachment("ATT-C", forwarded, "po.pdf")])
        graph.put(BOX_A, "MSG-FWD", "CK-3", subject="FW: PO", body="see below",
                  attachments=[file_attachment("ATT-F", forwarded, "po.pdf")])
        graph.messages[BOX_A]["MSG-FWD"]["from"] = {"emailAddress": {"address": UPN_A}}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        store = MailCaptureStore(root / "mail", clock=Clock())
        self.addCleanup(store.close)
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        self.assertTrue(MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                                    sleep=lambda s: None).run(SCOPE)["complete"])
        handles = {row[1]: row[0] for row in store.db.execute(
            "SELECT l1_handle, attachment_id FROM attachments")}
        with MailCaptures(root / "mail", internal_domains=SCOPE.internal_domains) as mail:
            projected = sorted(rec.row["profile"] for rec in
                               mail.records(["documentFields"])["documentFields"])
        self.assertEqual(projected, ["customer_po", "customer_po"])   # ATT-C and ATT-F
        audience = root / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {}}), encoding="utf-8")
        runtime = CaseRuntime(root / "case", client=None, audience=audience, tenant="plx",
                              live=LiveReaders(audience=reader_audience()),
                              mail_scope=MailScope.unrestricted(),
                              people_exclusion=PeopleExclusion.from_env(ENV))
        self.addCleanup(runtime.close)
        runtime.captures = CaseCaptures(runtime, mail_capture_dir=root / "mail")
        documents = cg.current_reader(runtime, READER_A)
        self.assertIsNone(documents(handles["ATT-S"]))
        for attachment in ("ATT-C", "ATT-F"):
            self.assertEqual(documents(handles[attachment])["row"]["profile"], "customer_po")
        # The customer-PO guard refuses the own send as evidence of a customer's PO.
        order = "api://public.app_sales_order/so1@7-0123456789abcdef"
        verdict = cg.check("portal.sales_order.set_customer_po", {
            "proposed_customer_po": "4500012345", "evidence_citation": handles["ATT-S"],
            "evidence_field": "fields.po_number.value"}, {
            "relation": "public.app_sales_order", "key": {"id": "so1"},
            "values": {"po_reference": "4500012345"}}, [handles["ATT-S"], order],
            lambda cited: ({"relation": "public.app_sales_order", "key": {"id": "so1"},
                            "row": {"id": "so1", "order_number": "OR1000777001",
                                    "po_reference": None, "custom_fields": {}}}
                           if cited == order else documents(cited)))
        self.assertFalse(verdict["passed"], verdict)
        self.assertIn("value_in_cited_evidence", {f["id"] for f in verdict["failures"]})

    def test_the_guard_and_the_projection_read_one_direction_per_mailbox(self):
        """``CaseCaptures.document`` and the projection read an attachment's direction
        from one function (``ontology.project.mail_attachment_sender``): the mailbox's
        received copy decides it whatever object id it was captured under (here a
        mailbox re-created under BOX_B's id, still ``orders@``), and another mailbox's
        received copy never does."""
        from integrations.tests.test_capture_mail import file_attachment
        from ontology import document_fields
        from operating import capability_guards as cg
        from operating.tests import test_attested_missing as am
        moved = am.pdf(am.customer_po("4500012402", "OR1000777012"))
        other = am.pdf(am.customer_po("4500012403", "OR1000777013"))
        graph = FakeGraph()
        # Received by this mailbox under its earlier object id, then forwarded.
        graph.put(BOX_B, "MSG-OLD", "CKB-1", subject="PO",
                  attachments=[file_attachment("ATT-O", moved, "po.pdf")])
        graph.put(BOX_A, "MSG-FWD2", "CK-3", subject="FW: PO",
                  attachments=[file_attachment("ATT-M", moved, "po.pdf")])
        # Received by another mailbox only: this mailbox's send of it is its own.
        graph.put(BOX_B, "MSG-B", "CKB-2", subject="PO",
                  attachments=[file_attachment("ATT-B", other, "po.pdf")])
        graph.put(BOX_A, "MSG-SENT", "CK-4", subject="PO",
                  attachments=[file_attachment("ATT-S", other, "po.pdf")])
        for sent in ("MSG-FWD2", "MSG-SENT"):
            graph.messages[BOX_A][sent]["from"] = {"emailAddress": {"address": UPN_A}}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        store = MailCaptureStore(root / "mail", clock=Clock())
        self.addCleanup(store.close)
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        self.assertTrue(MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                                    sleep=lambda s: None).run(SCOPE)["complete"])
        with store.db:
            store.db.execute("UPDATE messages SET record=json_set(record, '$.mailbox_upn', ?) "
                             "WHERE message_id='MSG-OLD'", (UPN_A,))
        handles = {row[1]: row[0] for row in store.db.execute(
            "SELECT a.l1_handle, a.attachment_id FROM attachments a JOIN messages m ON "
            "m.l1_handle=a.message_handle AND m.superseded_at IS NULL")}
        with MailCaptures(root / "mail", internal_domains=SCOPE.internal_domains) as mail:
            projected = {rec.row["source_key"] for rec in
                         mail.records(["documentFields"])["documentFields"]}
        served = lambda attachment: any(attachment in key for key in projected)
        self.assertEqual({a: served(a) for a in ("ATT-M", "ATT-S")},
                         {"ATT-M": True, "ATT-S": False})
        audience = root / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {}}), encoding="utf-8")
        runtime = CaseRuntime(root / "case", client=None, audience=audience, tenant="plx",
                              live=LiveReaders(audience=reader_audience()),
                              mail_scope=MailScope.unrestricted(),
                              people_exclusion=PeopleExclusion.from_env(ENV))
        self.addCleanup(runtime.close)
        runtime.captures = CaseCaptures(runtime, mail_capture_dir=root / "mail")
        documents = cg.current_reader(runtime, READER_A)
        for attachment, issued in (("ATT-M", None),
                                   ("ATT-S", "own_mailbox_send")):
            row = runtime.captures.document(handles[attachment], READER_A)["row"]
            self.assertEqual(document_fields._issued_by(row, document_fields._received(
                row["mailbox_copies"])), issued, attachment)
            # Never another mailbox's copy: B's received copy is not on A's row.
            self.assertNotIn(UPN_B, {c.get("mailbox_upn") for c in row["mailbox_copies"]
                                     if "content_sha256" in c}, attachment)
            self.assertEqual(documents(handles[attachment]) is not None, served(attachment),
                             attachment)


if __name__ == "__main__":
    unittest.main()
