"""Proactive gaps (operating/proactive_gaps.py) and the console home view (/api/home).

SYNTHETIC fixtures only: cards shaped like ``plx.object`` answers, stub tools per reader and
a real ``CaseStore`` behind a fixture session. Evidence label: isolated-fixture (no network,
no business system). The live-check command is exercised in ``LiveCheckTest`` over the
injected-transport world of ``case.customer_po_missing`` (fake Workbench and fake drive)
plus Portal quotes.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from datetime import date
from http.client import HTTPConnection
import io
import sqlite3
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from verify import live_verify as lv
from integrations.http import SourceHttpError
from ontology.core import Actor
from ontology.schema import load_schema
from operating import answerers
from operating import proactive_gaps as pg
from operating.cases import CaseStore
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests.test_cases import FakeResolver
from operator_ui import object_workspace as objects
from operator_ui.case_workspace import CaseWorkspace
from operator_ui.server import make_server

ROOT = Path(__file__).resolve().parents[2]
AS_OF = date(2026, 10, 19)
OPS = Actor("plx", frozenset({"ops"}))
OTHER = Actor("plx", frozenset({"other"}))


def handle(n: int) -> str:
    return f"api://public.fixture_rows/%7B%22id%22%3A{n}%7D@g1-{n}"


def value(prop, v, n, value_type="text", **extra):
    return {"property": prop, "value_type": value_type, "value": v, **extra,
            "citation": {"l1_handle": handle(n), "field_path": prop, "digest": "a" * 64}}


def card(object_type, *, values=(), conflicts=(), leads=(), n=1):
    return {"object": {"object_id": f'{object_type}:fixture:{{"id":{n}}}',
                       "object_type": object_type},
            "values": list(values), "conflicts": list(conflicts), "leads": list(leads)}


def lead(*, cited=True):
    stated = value("customer_po", "SYNTH-PO-1", 11) if cited else {
        **value("customer_po", "SYNTH-PO-1", 11), "citation": None}
    return {"kind": "missing_in_record", "subject": "SalesOrder.customer_po",
            "values": [stated, value("customer_po", None, 12)],
            "note": "the document states a value the order holds empty",
            "document_citations": [{"l1_handle": "sharepoint://fixture/doc@v1",
                                    "source_sha256": "b" * 64, "page": 1, "start": 0,
                                    "end": 5}] if cited else []}


def disagreement(a, b, *, unit_b="EA", role_b="planned"):
    quantity = lambda v, n, unit, role: value(  # noqa: E731
        "planned_quantity", v, n, "quantity", unit=unit, quantity_role=role)
    return {"kind": "value_disagreement", "subject": "planned_quantity",
            "values": [quantity(a, 21, "EA", "planned"), quantity(b, 22, unit_b, role_b)],
            "note": "two sources differ", "tier": "lead"}


def quote(object_type, valid_until, status=None, n=1, **outcome):
    values = [value("valid_until", valid_until, 31, "date")]
    if status:
        values.append(value("status", status, 32, "enum"))
    values += [value(prop, v, 33) for prop, v in outcome.items()]
    return card(object_type, values=values, n=n)


def world(by_type):
    """Stub reader tools: ``plx.find`` lists the cards of a type (honouring ``limit``),
    ``plx.object`` answers a card; ``reads`` counts object reads per type."""
    cards = {c["object"]["object_id"]: c for found in by_type.values() for c in found}
    reads = Counter()

    def call(name, arguments):
        if name == "plx.find":
            return {"status": "ambiguous", "candidates": [
                {"object": c["object"]}
                for c in by_type.get(arguments["object_type"], [])[:arguments["limit"]]]}
        answer = cards[arguments["object_id"]]
        reads[answer["object"]["object_type"]] += 1
        return answer
    return call, reads


class DetectorTest(unittest.TestCase):
    def test_quote_outcomes_are_the_schema_status_enum_less_the_open_statuses(self):
        spec = load_schema().object_types["Quote"]
        enum = next(p["enum_values"] for p in spec["properties"] if p["name"] == "status")
        self.assertEqual(pg.quote_outcomes(), set(enum) - {"DRAFT", "SENT", "NEGOTIATION"})
        self.assertEqual(pg.quote_outcomes(), {"ACCEPTED", "REJECTED", "EXPIRED"})

    def kinds(self, answer):
        return [gap["kind"] for gap in pg.detect(answer, AS_OF)]

    def test_each_rule_raises_a_cited_gap_with_a_follow_up_and_stays_silent_when_clean(self):
        raised = {
            "missing_in_record": card("SalesOrder", leads=[lead()]),
            "conflicting_quantity": card("AssemblyOrderPacket",
                                         conflicts=[disagreement("100", "120")]),
            "expired_quote": quote("Quote", "2026-01-31", status="SENT")}
        for kind, answer in raised.items():
            with self.subTest(kind):
                [gap] = pg.detect(answer, AS_OF)
                self.assertEqual((gap["kind"], gap["detected_by"]), (kind, "rule"))
                self.assertEqual(gap["object_id"], answer["object"]["object_id"])
                self.assertTrue(gap["citations"] and gap["summary"])
                follow_up = gap["follow_up"]
                self.assertLessEqual(set(follow_up), objects.FOLLOW_UP_FIELDS)
                self.assertEqual(follow_up["object_id"], gap["object_id"])
                self.assertIn(follow_up["state"], objects.FOLLOW_UP_STATES)
                self.assertIn(follow_up["priority"], objects.FOLLOW_UP_PRIORITIES)
                self.assertTrue(follow_up["who_can_answer"] and follow_up["why"])
                for who in follow_up["who_can_answer"]:   # one resolver, never free text
                    self.assertTrue(who in answerers.departments()
                                    or who.startswith(answerers.UNDECLARED), who)
                self.assertLessEqual(len(follow_up["missing"]), objects.MAX_FOLLOW_UP_TITLE)
                self.assertEqual(pg.detect(answer, AS_OF), [gap])   # deterministic, same id
        clean = [
            card("SalesOrder"),
            card("SalesOrder", leads=[lead(cited=False)]),           # nothing to cite
            card("AssemblyOrderPacket", conflicts=[disagreement("100", "100.0")]),
            card("AssemblyOrderPacket", conflicts=[disagreement("100", "120", role_b="released")]),
            card("AssemblyOrderPacket", conflicts=[disagreement("100", "120", unit_b="KG")]),
            card("AssemblyOrderPacket", conflicts=[disagreement("100", "many")]),
            quote("ForwarderQuote", "2026-10-19"),                   # valid through today
            quote("ForwarderQuote", "2999-01-01"),
            quote("Quote", "2026-01-31", status="ACCEPTED"),
            quote("Quote", "2026-01-31", status="EXPIRED"),
            quote("Quote", "2026-01-31", status="SENT", converted_sales_order_number="SO-9"),
            quote("Quote", "2026-01-31", status="SENT", accepted_at="2026-01-02"),
            quote("SalesOrder", "2026-01-31")]                        # not a quote
        for answer in clean:
            with self.subTest(answer["object"]["object_type"], values=answer["values"][:1]):
                self.assertEqual(pg.detect(answer, AS_OF), [])

    def test_a_quote_with_no_outcome_field_is_flagged_outcome_unknown_never_as_unused(self):
        [sent] = pg.detect(quote("Quote", "2026-01-31", status="SENT"), AS_OF)
        [forwarder] = pg.detect(quote("ForwarderQuote", "2026-01-31"), AS_OF)
        self.assertNotIn("outcome unknown", sent["summary"])
        self.assertIn("outcome unknown", forwarder["summary"])
        self.assertNotIn("no accepted", forwarder["follow_up"]["why"])
        self.assertEqual(forwarder["follow_up"]["priority"], "low")

    def test_the_scan_roster_names_only_types_and_properties_the_schema_declares(self):
        from ontology.schema import load_schema

        types = load_schema().object_types
        seeds = pg.seeds(AS_OF)
        self.assertTrue({"CustomerPoDocument", "ForwarderQuote", "Quote"} <=
                        {seed["object_type"] for seed in seeds})
        allowed = json.loads((ROOT / "operating" / "ontology_tool_contracts.json")
                             .read_text("utf-8"))["$defs"]["filter"]["properties"]["op"]["enum"]
        for seed in seeds:
            declared = {p["name"] for p in types[seed["object_type"]]["properties"]}
            for found in seed["filters"]:
                self.assertIn(found["property"], declared, seed)
                self.assertIn(found["op"], allowed)


class ScanTest(unittest.TestCase):
    @staticmethod
    def tools(total, *, answers=None):
        """plx.find lists ``total`` packets (honouring ``limit``); each has a conflict."""
        ids = [f'AssemblyOrderPacket:fixture:{{"id":{n}}}' for n in range(total)]
        calls = []

        def call(name, arguments):
            calls.append(name)
            if name == "plx.find":
                if arguments["object_type"] != "AssemblyOrderPacket":
                    return {"status": "not_found", "candidates": []}
                return {"status": "ambiguous", "candidates": [
                    {"object": {"object_id": i, "object_type": "AssemblyOrderPacket"}}
                    for i in ids[:arguments["limit"]]]}
            n = ids.index(arguments["object_id"])
            answer = card("AssemblyOrderPacket", conflicts=[disagreement("100", "120")], n=n)
            answer["object"]["object_id"] = arguments["object_id"]
            return answer
        return call, calls

    def test_every_found_object_is_scanned_once_up_to_the_bound_and_truncation_is_said(self):
        for total in (1, 10, 60):
            with self.subTest(total=total):
                call, calls = self.tools(total)
                result = pg.scan(call, AS_OF)
                expected = min(total, pg.PER_SEED)
                self.assertEqual((len(result["gaps"]), result["scanned"]), (expected, expected))
                self.assertEqual(len({gap["id"] for gap in result["gaps"]}), expected)
                self.assertEqual(result["truncated"], total >= pg.PER_SEED)
                self.assertLessEqual(calls.count("plx.object"), pg.MAX_SCAN)

    def test_the_bound_is_shared_across_seeds_so_expired_quotes_in_the_last_seeds_are_read(self):
        documents = [seed["object_type"] for seed in pg.seeds(AS_OF)
                     if not seed["object_type"].endswith("Quote")]
        by_type = {name: [card(name, n=n) for n in range(25)] for name in documents}
        for name in ("ForwarderQuote", "Quote"):
            by_type[name] = [quote(name, "2026-01-31", n=n) for n in range(25)]
        call, reads = world(by_type)
        result = pg.scan(call, AS_OF)
        self.assertEqual(sum(reads.values()), pg.MAX_SCAN)
        self.assertEqual(set(reads), set(by_type))   # every seed gets reads
        self.assertGreaterEqual(min(reads.values()), pg.MAX_SCAN // len(pg.seeds(AS_OF)))
        self.assertEqual(Counter(g["object_type"] for g in result["gaps"]),
                         {"ForwarderQuote": reads["ForwarderQuote"], "Quote": reads["Quote"]})
        self.assertTrue(result["truncated"])
        self.assertEqual(result["unscanned"], 20 * len(by_type) - pg.MAX_SCAN)

    def test_a_gap_whose_evidence_the_reader_may_not_open_is_dropped_never_half_cited(self):
        call, _ = self.tools(2)
        both = {handle(21), handle(22)}
        for opened, cited in ((both, [sorted(both)] * 2), ({handle(21)}, []), (set(), [])):
            with self.subTest(opened=sorted(opened)):
                result = pg.scan(call, AS_OF, readable=lambda handles: opened & set(handles))
                self.assertEqual([sorted(g["citations"]) for g in result["gaps"]], cited)
        document = "sharepoint://fixture/doc@v1"
        call, _ = world({"CustomerPoDocument": [card("CustomerPoDocument", leads=[lead()])]})
        # the claim needs the document's statement and the record's empty read alike: a
        # reader of only one of them never sees it
        for opened, cited in (({handle(12)}, None),            # only the empty record field
                              ({document}, None),               # only the document
                              ({document, handle(12)}, [document, handle(12)]),
                              ({handle(11), handle(12)}, [handle(11), handle(12)])):
            with self.subTest(opened=sorted(opened)):
                gaps = pg.scan(call, AS_OF, readable=lambda h: opened & set(h))["gaps"]
                self.assertEqual([g["citations"] for g in gaps], [cited] if cited else [])

    def test_a_capture_shaped_like_the_live_one_reads_its_orders_though_no_document_class_is_seeded(self):
        # fab6ccab (09:39Z): the folder held certificates and unreadable scans, no customer
        # PO, so every document seed found nothing and scanned was 0 for both readers.
        for orders in (1, 10, 60):
            with self.subTest(orders=orders):
                by_type = {"CertificateOfAnalysis": [card("CertificateOfAnalysis", n=n)
                                                     for n in range(5)],
                           "SalesOrder": [card("SalesOrder", n=100 + n) for n in range(orders)]}
                call, reads = world(by_type)
                result = pg.scan(call, AS_OF)
                self.assertEqual(result["scanned"], min(orders, pg.PER_SEED))
                self.assertEqual(reads, Counter({"SalesOrder": min(orders, pg.PER_SEED)}))
                self.assertEqual(result["gaps"], [])   # no gap is made up

    def test_the_record_seeds_look_for_the_attested_field_empty(self):
        records = [seed for seed in pg.seeds(AS_OF) if seed["filters"][0]["op"] == "absent"]
        self.assertIn(("SalesOrder", "customer_po"),
                      [(s["object_type"], s["filters"][0]["property"]) for s in records])

    def test_a_tool_error_is_counted_by_code_and_never_read_as_clean(self):
        def call(name, arguments):
            return {"error": {"code": "store_unavailable", "message": "x"}}
        result = pg.scan(call, AS_OF)
        self.assertEqual((result["gaps"], result["scanned"]), ([], 0))
        self.assertEqual(result["unread"], {"store_unavailable": len(pg.seeds(AS_OF))})


    def test_a_scan_that_cites_no_gap_says_which_step_came_up_empty(self):
        def failing(name, arguments):
            return {"error": {"code": "store_unavailable", "message": "x"}}

        def unreadable(name, arguments):
            if name == "plx.object":
                return {"error": {"code": "object_not_found", "message": "x"}}
            return packets(name, arguments)
        packets, _ = self.tools(2)
        empty, _ = self.tools(0)
        clean, _ = world({"SalesOrder": [card("SalesOrder", n=100)]})
        for name, call, readable, reason in (
                ("every find fails", failing, None, "find_failed"),
                ("nothing found", empty, None, "nothing_found"),
                ("nothing read", unreadable, None, "nothing_read"),
                ("evidence withheld", packets, lambda handles: set(), "evidence_withheld"),
                ("no rule fired", clean, None, "no_rule_fired"),
                ("a cited gap", packets, None, None)):
            with self.subTest(name):
                counts = pg._counts(pg.scan(call, AS_OF, readable=readable))
                self.assertEqual(counts["no_gap_reason"], reason)
                self.assertEqual(counts["gaps_cited"] == 0, reason is not None)
                self.assertIn(reason, (*pg.NO_GAP_REASONS, None))


class LateOrderTest(unittest.TestCase):
    """The ``late_order`` rule: lines past their expected receipt and not fully received,
    one gap on the order (synthetic cards; ``plx.around`` stubbed)."""

    ORDER = 'PurchaseOrder:fixture:{"id":900}'

    @staticmethod
    def line(n, expected="2026-09-01", ordered="10", received="2", order=ORDER):
        values = [value(p, v, 400 + n, t) for p, v, t in (
            ("bc_expected_receipt_date", expected, "date"),
            ("ordered_quantity", ordered, "quantity"),
            ("received_quantity", received, "quantity")) if v is not None]
        answer = card("PurchaseOrderLine", values=values, n=n)
        answer["order"] = order
        return answer

    def tools(self, lines, orders=None):
        cards = {c["object"]["object_id"]: c for c in lines}
        calls = Counter()

        def call(name, arguments):
            calls[name] += 1
            if name == "plx.find":
                if arguments["object_type"] != "PurchaseOrderLine":
                    return {"status": "not_found", "candidates": []}
                self.found = arguments
                return {"status": "ambiguous", "candidates": [
                    {"object": c["object"]} for c in lines[:arguments["limit"]]]}
            if name == "plx.around":
                if arguments["from"] not in cards:   # an order: its supplier, if any
                    return {"edges": []}
                to = cards[arguments["from"]]["order"]
                return {"edges": [{"link_type": "PurchaseOrderLine.line_of",
                                   "from": arguments["from"], "to": to}] if to else []}
            return cards.get(arguments["object_id"]) or {
                "error": {"code": "object_not_found", "message": "x"}}
        return call, calls

    def test_two_late_lines_of_one_order_raise_one_cited_gap_on_the_order(self):
        call, _ = self.tools([self.line(1), self.line(2, expected="2026-10-05")])
        [gap] = pg.scan(call, AS_OF)["gaps"]
        self.assertEqual(self.found["filters"], [
            {"property": "bc_expected_receipt_date", "op": "lt", "value": "2026-10-19"}])
        self.assertEqual((gap["kind"], gap["object_id"], gap["object_type"]),
                         ("late_order", self.ORDER, "PurchaseOrder"))
        self.assertEqual(gap["citations"], [handle(401), handle(402)])
        self.assertEqual(gap["summary"], "2 lines expected by 2026-10-05 not fully received")
        self.assertEqual(gap["follow_up"]["priority"], "high")
        self.assertLessEqual(set(gap["follow_up"]), objects.FOLLOW_UP_FIELDS)
        self.assertEqual(gap["id"], pg.scan(call, AS_OF)["gaps"][0]["id"])   # stable

    def test_lines_not_late_or_not_decidable_raise_nothing(self):
        for name, line in (("received", self.line(1, received="10")),
                           ("over received", self.line(1, received="12")),
                           ("on time", self.line(1, expected="2026-10-19")),
                           ("future", self.line(1, expected="2026-11-01")),
                           ("no received", self.line(1, received=None)),
                           ("no ordered", self.line(1, ordered=None)),
                           ("unreadable qty", self.line(1, received="n/a")),
                           ("bad date", self.line(1, expected="soon"))):
            with self.subTest(name):
                call, _ = self.tools([line])
                self.assertEqual(pg.scan(call, AS_OF)["gaps"], [])

    def test_a_late_line_without_a_reachable_order_raises_nothing(self):
        call, _ = self.tools([self.line(1, order=None)])
        self.assertEqual(pg.scan(call, AS_OF)["gaps"], [])

    def test_the_reader_who_cannot_open_a_lines_handles_does_not_see_it(self):
        call, _ = self.tools([self.line(1), self.line(2)])
        gaps = pg.scan(call, AS_OF, readable=lambda h: {handle(401)} & set(h))["gaps"]
        self.assertEqual([g["citations"] for g in gaps], [[handle(401)]])
        self.assertIn("1 line expected", gaps[0]["summary"])
        self.assertEqual(pg.scan(call, AS_OF, readable=lambda h: set())["gaps"], [])

    def test_many_lines_and_orders_are_bounded_and_none_is_duplicated(self):
        for total in (1, 10, 60):
            with self.subTest(total=total):
                lines = [self.line(n, order=f'PurchaseOrder:fixture:{{"id":{n % 5}}}')
                         for n in range(total)]
                call, calls = self.tools(lines)
                result = pg.scan(call, AS_OF)
                reads = min(total, pg.PER_SEED)
                orders = min(reads, 5)
                self.assertEqual(len(result["gaps"]), orders)
                # One around per line and no read of the order: the scan only detects; the
                # supplier draft is the engine's (proactive_engine.late_order_draft).
                self.assertEqual(calls["plx.around"], reads)
                self.assertEqual(calls["plx.object"], reads)
                self.assertLessEqual(calls["plx.object"], pg.MAX_SCAN)
                self.assertEqual(result["unread"], {})
                self.assertEqual(sorted(g["object_id"] for g in result["gaps"]),
                                 sorted({line["order"] for line in lines[:reads]}))
                for gap in result["gaps"]:
                    self.assertNotIn("draft", gap["follow_up"])


class HomeTest(unittest.TestCase):
    """GET /api/home for the signed-in session: its open cases, their gaps and the gaps the
    workspace found itself, for that reader only."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.resolver = FakeResolver()
        for n in (1, 2):
            self.resolver.add(handle(n), {"row": {"n": n}}, {"ops"})
        self.store = CaseStore(Path(tmp.name) / "cases.db", citation_resolver=self.resolver)
        self.open_case = self.store.open(OPS, "Why is SYNTH-OPEN short?")
        self.store.gap(OPS, self.open_case, "the shipping date", "no source states it",
                       ["buyer"], "not_ingested", [handle(1)])
        self.store.gap(OPS, self.open_case, "the pallet count", "cited row now unreadable",
                       ["buyer"], "unknown", [handle(2)])
        self.resolver.revoke(handle(2), "ops")
        self.quiet_case = self.store.open(OPS, "Is SYNTH-QUIET fine?")
        closed = self.store.open(OPS, "What was SYNTH-CLOSED?")
        self.store.gap(OPS, closed, "a closed question", "was open", ["buyer"], "unknown")
        self.store.close(OPS, closed, "answered")
        self.store.gap(OTHER, self.store.open(OTHER, "Is SYNTH-OTHER late?"),
                       "someone else's gap", "not this reader's", ["buyer"], "unknown")
        self.cards = {c["object"]["object_id"]: c for c in (
            card("SalesOrder", leads=[lead()], n=1),
            card("AssemblyOrderPacket", conflicts=[disagreement("100", "120")], n=2),
            card("ForwarderQuote", values=[value("valid_until", "2026-01-31", 31, "date")], n=3),
            card("ForwarderQuote", values=[value("valid_until", "2999-01-01", 33, "date")], n=4))}

    def tools(self):
        self.reads = 0
        by_type = {}
        for object_id, answer in self.cards.items():
            by_type.setdefault(answer["object"]["object_type"], []).append(object_id)
        by_type["CustomerPoDocument"] = by_type.pop("SalesOrder")   # the lead is on the document

        def find(arguments, actor):
            ids = by_type.get(arguments.get("object_type"), []) if "ops" in actor.principals else []
            return {"status": "ambiguous" if ids else "not_found", "candidates": [
                {"object": {"object_id": i, "object_type": arguments["object_type"]}}
                for i in ids]}

        def read(arguments, actor):
            self.reads += 1
            found = self.cards.get(arguments["object_id"])
            return found if found and "ops" in actor.principals else {
                "error": {"code": "object_not_found", "message": "x"}}
        empty = {"handler": lambda arguments, actor: {}}
        return {"plx.find": {"handler": find}, "plx.object": {"handler": read},
                "plx.around": empty, "plx.timeline": empty}

    def home(self, actor, *, with_objects=True):
        session = fixture_session("plx", actor.principals, channel=CHANNEL_CONSOLE)
        workspace = CaseWorkspace(mock.Mock(cases=self.store), actor)
        server = make_server(
            case_workspace=workspace, session=session,
            object_workspace=objects.ObjectWorkspace(self.tools(), actor, sources=workspace)
            if with_objects else None)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10),
                                 server.server_close()))
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
        connection.request("GET", "/api/home",
                           headers={"Authorization": "Bearer " + server.operator_access_token})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200, body)
        return body

    def test_home_lists_the_signed_in_readers_open_cases_open_gaps_and_found_gaps(self):
        home = self.home(OPS)
        cases = {c["question"]: c for c in home["cases"]["cases"]}
        self.assertEqual(set(cases), {"Why is SYNTH-OPEN short?", "Is SYNTH-QUIET fine?"})
        busy = cases["Why is SYNTH-OPEN short?"]
        self.assertEqual(busy["case_id"], self.open_case)
        self.assertEqual([g["missing"] for g in busy["gaps"]], ["the shipping date"])
        self.assertEqual(busy["gaps_withheld"], 1)   # counted, its words never shown
        self.assertNotIn("pallet", json.dumps(home))
        counted = home["cases"]
        self.assertEqual((counted["open_cases"], counted["open_gaps"], counted["gaps_withheld"]),
                         (2, 1, 1))
        self.assertEqual(cases["Is SYNTH-QUIET fine?"]["gaps"], [])
        # Home reads the scheduled engine's inbox (operating/tests/test_proactive_engine.py),
        # never scans cards on the request: an API-only reader has no grant to trim it by.
        found = home["proactive"]
        self.assertEqual((found["status"], found["gaps"]), ("not_configured", []))
        self.assertEqual(self.reads, 0)

    def test_another_reader_sees_none_of_it_and_cases_alone_still_serve_without_objects(self):
        home = self.home(OTHER)
        self.assertEqual([c["question"] for c in home["cases"]["cases"]],
                         ["Is SYNTH-OTHER late?"])
        self.assertEqual(home["proactive"]["gaps"], [])
        self.assertNotIn("SYNTH-OPEN", json.dumps(home))
        alone = self.home(OPS, with_objects=False)
        self.assertEqual(alone["proactive"], {"status": "not_configured"})
        self.assertEqual(len(alone["cases"]["cases"]), 2)


    def test_an_inbox_failure_keeps_the_cases(self):
        with mock.patch.object(objects.ObjectWorkspace, "gap_inbox",
                               side_effect=sqlite3.OperationalError("SYNTH-locked")):
            home = self.home(OPS)
        self.assertEqual(home["proactive"],
                         {"status": "unreadable", "error": "OperationalError", "gaps": []})
        with mock.patch.object(objects.ObjectWorkspace, "gap_inbox",
                               side_effect=PermissionError("SYNTH-secret-detail")):
            refused = self.home(OPS)["proactive"]
        self.assertEqual(refused, {"status": "refused", "error": "permission_denied",
                                   "gaps": []})
        self.assertEqual(len(home["cases"]["cases"]), 2)


class LiveCheckTest(unittest.TestCase):
    """The retired live spec ``case.proactive_gaps`` (kept as
    ``operating/tests/fixtures/case.proactive_gaps.spec.json``; the scheduled A9 check is
    ``case.proactive_engine``) over injected sources: its own
    command and host config (the ``case.customer_po_missing`` world plus Portal quotes in
    the FakeWorkbench, a fake drive for the site), the network refused. Evidence label:
    injected-transport."""

    SPEC_ID = "case.proactive_gaps"

    def setUp(self) -> None:
        import socket

        def refuse(*_args, **_kwargs):
            raise AssertionError("network access attempted in an offline test")

        for target in ("connect", "connect_ex"):
            patcher = mock.patch.object(socket.socket, target, refuse)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.spec = json.loads((ROOT / "operating" / "tests" / "fixtures"
                                / f"{self.SPEC_ID}.spec.json").read_text("utf-8"))

    def run_spec(self, *, fetch=None) -> tuple[int, dict, str]:
        from integrations.tests import test_capture_sharepoint as tsp
        from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
        from operating.tests import order_fixture as fixture
        from operating.tests.test_case_workspace_live import _customer_drive

        fake = FakeWorkbench()
        fake.catalog = json.loads(json.dumps(fixture.CATALOG))
        fake.tables = fixture.tables()
        fake.tables["public.app_sales_order"].append(
            fixture._so("so-m", "OR1000888001", "cust-1", "CONFIRMED", "28"))
        # One open quote past validity, one accepted (an outcome) and one still valid.
        fake.catalog["public.app_quote"] = fixture._cat(
            ["id", "quote_number", "status", "issue_date", "valid_until", "accepted_at",
             "rejected_at", "updated_at"])
        fake.tables["public.app_quote"] = [
            fixture._row(f"q-{n}", quote_number=f"QUO-2026-09{n}", status=status,
                         issue_date=fixture._ts("01"), valid_until=until, accepted_at=accepted,
                         rejected_at=None)
            for n, status, until, accepted in (
                (1, "SENT", fixture._ts("15"), None),
                (2, "ACCEPTED", fixture._ts("15"), fixture._ts("10")),
                (3, "SENT", "2099-01-01T00:00:00Z", None))]
        host = self.spec["command"][self.spec["command"].index("--host-config") + 1]
        config = json.loads((ROOT / host).read_text("utf-8"))
        for key in ("capture_audience", "live_audience", "mirror_audience"):
            config[key] = str(ROOT / "examples" / config[key])
        config["sharepoint"]["scope"] = str(ROOT / "examples" / config["sharepoint"]["scope"])
        (self.temp / "host.json").write_text(json.dumps(config), "utf-8")
        argv = [str(self.temp / "host.json") if arg.endswith("_host.live-check.json")
                else str(ROOT / arg) if arg.startswith("examples/")
                else arg.replace("{tmp}", str(self.temp)) for arg in self.spec["command"][3:]]
        drive = _customer_drive()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pg.main(argv, environ={
                **tsp.ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY},
                graph_sender=drive, host_options={"sender": drive, "workbench_fetch": fetch or fake,
                              "sleep": lambda _s: None})
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_the_spec_passes_on_a_cited_detected_gap_and_reads_blocked_on_a_portal_outage(self):
        lv.validate_spec(self.spec, stem=self.SPEC_ID,
                         allowed_modules={*lv.ALLOWED_MODULES, "operating.proactive_gaps"})
        code, summary, text = self.run_spec()
        self.assertEqual(lv.check_expectations(self.spec, code, summary), [])
        self.assertGreaterEqual(summary["gaps_cited"], 1)
        self.assertEqual(summary["gaps_by_kind"]["expired_quote"], 1)   # q-1 only
        self.assertGreaterEqual(summary["gaps_by_kind"]["missing_in_record"], 1)
        self.assertEqual(sum(summary["gaps_by_kind"].values()), summary["gaps_found"])
        self.assertNotIn("OR1000888001", text)   # counts and enums only
        self.assertNotIn("QUO-2026-09", text)
        other = summary["readers"][1]            # reads the orders, no document, no quote
        self.assertEqual((other["reader"], other["gaps_found"], other["gaps_by_kind"]),
                         (["staff:operations"], 0, {}))
        # A reader whose scan cites nothing says why, as a typed reason the record keeps.
        self.assertIsNone(summary["no_gap_reason"])
        self.assertIn(other["no_gap_reason"], pg.NO_GAP_REASONS)
        recorded = lv._recorded(self.spec, summary)
        self.assertEqual((recorded["no_gap_reason"], recorded["readers[*].no_gap_reason"]),
                         (None, [None, other["no_gap_reason"]]))
        # The mirror grants the quotes to the sales reader and the capture principal only.
        audience = json.loads((ROOT / self.spec["command"][
            self.spec["command"].index("--mirror-audience") + 1]).read_text("utf-8"))
        self.assertEqual(sorted(audience["audiences"]["public.app_quote"]),
                         ["staff:sales", "svc:workbench-host"])

        def portal_down(url, headers, *, max_bytes):
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        code, summary, _text = self.run_spec(fetch=portal_down)
        self.assertTrue(lv.blocked_parts(summary))
        self.assertEqual(lv.check_expectations(
            self.spec, code, summary, self.spec["expect_blocked"]), [])


if __name__ == "__main__":
    unittest.main()
