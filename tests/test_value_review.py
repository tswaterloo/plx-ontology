"""A person confirms or corrects one value (``plx.review`` ``confirm_value`` /
``correct_value``, gap WS-13 5aa4d9b5b066; decision 2026-10-07-doc-fields-close-value-review).

A supplier's proforma invoice arrives as a mail attachment in the orders mailbox; the
parser reads its quantity as 400,000. A second supplier mail, in the finance mailbox only
employee A reads, states 40,000. Employee A corrects the quantity citing the span of that
mail, and confirms the unit price. Through a restart (the review store and the
projection store reopened from disk) ``plx.object`` shows the source value untouched with
the correction beside it, each with its tier; the next ``plx.investigate`` keeps the source
value under its own citation with the correction beside it, an engine taking reviewed
inputs computes from the person's value and cites the correction, ``plx.aggregate`` sums
the person's value citing the correction and ``plx.compare`` shows it beside the side's
value; a retraction takes it back. A correction without a page or span is refused, and a
cited page is taken exactly when ``plx.read`` resolves it for the same reader (a mail
attachment, a zip member and a SharePoint PDF page, a revoked reader, a version captured
again since, a page whose text was never read: a typed unknown); a handle two records
share is read as the record the decision is about.
Employee B, who may read the invoice but not the finance mailbox, sees neither the
correction nor its citation anywhere. A confirmation reads ``human_reviewed`` until the
invoice is captured again with other content. Mail captured over an injected Graph
transport; no network, no business-system write. Evidence: injected-transport.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
import zipfile

from integrations import capture_sharepoint as cs
from integrations import graph_read
from integrations.tests import test_capture_sharepoint as tcs
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.tests import test_bc_capture as tb
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests import test_capture_mail as tm
from ontology import reviews, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.mentions import page_citation
from ontology.project import MailCaptures, SharePointCaptures, build_store
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from operating import ontology_tools_evidence as ev
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_review as rt
from operating.session_identity import (CHANNEL_AUTHOR, CHANNEL_CONSOLE, PROOF_CONFIGURED,
                                        SessionIdentity)
from operating.case_mail_scope import MailScope
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.cases import CitationUnresolvable
from operating.case_tools_host import CaptureCaseHostTools
from operating.employee_permissions import EmployeePermissionPolicy, EmployeePermissionResolver
from operating.tests import test_employee_permissions as hp
from tests import test_investigate_landed_cost as lt
from tests import test_investigate_order_margin as margin
from tests import test_ontology_tools_review as tr

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
# The review module as committed before the value reviews (7f7a6ba:ontology/reviews.py),
# byte for byte (its git blob id is checked): the v2 store below is one v2 wrote.
V2_SOURCE = Path(__file__).parent / "fixtures" / "reviews_v2.py.txt"
V2_BLOB = "563af0e6b0b094598f7fee54c1bce435696af050"
A, B = f"employee:{tr.OID_A}", f"employee:{tr.OID_B}"
READER_B = tr.employee_session(tr.OID_B).actor
REVIEWED_AT = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)
RECAPTURED = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
LATER = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)
INVOICE = ("PROFORMA INVOICE\nPI No: PI-60311\nDate: 2026-09-14\nQuantity: {qty} pcs\n"
           "Unit Price: USD 0.04125/pc\nTotal Amount: USD 16,500.00\n")
SAYS = "The PI quantity is 40,000 pcs, not 400,000."
PLAN = {"$schema": "_plan.schema.json", "format": oi.PLAN_FORMAT, "id": "value_read",
        "families": ["value-read"], "title": "t", "description": "d",
        "root": {"object_types": ["SupplierConfirmation"]}, "step_budget": 4,
        "steps": [{"id": "read", "tool": "plx.object", "from": "root", "bind": "object_id"}],
        "evidence": [{"name": "quantity", "kind": "property",
                      "object_type": "SupplierConfirmation", "properties": ["quantity"]},
                     {"name": "total", "kind": "compute", "engine": "rereads",
                      "engine_args": {}},
                     {"name": "doubled", "kind": "compute", "engine": "doubles",
                      "engine_args": {}}]}


def _doubles(args, produced, edges, **kw):
    (quantity,) = produced["quantity"]
    return {"entries": [{"name": "doubled", "kind": "computed",
                         "value": str(Decimal(quantity["value"]) * 2), "unit": quantity["unit"],
                         "inputs": ["quantity"], "steps": ["quantity x 2"],
                         "check": "not_applicable"}], "gaps": [], "reasons": []}


# An engine that re-reads its input's citation (as landed cost does): its entry names it.
# One that takes reviewed inputs computes from the value it is handed.
ENGINES = {"rereads": {"check": lambda args, doc, index, schema: [],
                       "run": lambda args, produced, edges, **kw: {
                           "entries": [{"name": "total", "kind": "computed", "value": None,
                                        "inputs": ["quantity"], "steps": ["re-read"],
                                        "check": "not_applicable"}],
                           "gaps": [], "reasons": []}},
           "doubles": {"check": lambda args, doc, index, schema: [], "run": _doubles,
                       "reviewed_inputs": True}}
HAS_PDF = bool(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"))


def zipped(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)
    return buffer.getvalue()


def pdf(pages: list[list[str]]) -> bytes:
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    for lines in pages:
        for row, line in enumerate(lines):
            page.drawString(60, 780 - 18 * row, line)
        page.showPage()
    page.save()
    return buffer.getvalue()


def mirror_audience(root: Path) -> Path:
    """A Portal mirror audience granting no relation (the case runtime needs one)."""
    mirror = Path(tempfile.mkdtemp(dir=root)) / "mirror-audience.json"
    mirror.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                  "audiences": {}}), encoding="utf-8")
    return mirror


class World:
    """Both mailboxes captured and projected under ``tmp``; ``recapture`` captures the
    invoice again with another quantity and projects a new store."""

    def __init__(self, tmp: Path, paged: bool = False, pack: bool = False) -> None:
        self.tmp, self.clock, self.graph = tmp, tm.Clock(), tm.FakeGraph()
        self.paged = paged   # the invoice is a two-page PDF (its terms on page 2)
        self.put_invoice("CK-1-1", "400,000")
        self.graph.put(tm.BOX_B, "MSG-2", "CK-2-1", subject="Corrected PI", body=SAYS)
        if pack:   # a zip holding a two-page PDF, and a PDF of which no text is read
            self.graph.put(tm.BOX_A, "MSG-3", "CK-3-1", subject="Pack", body="Pack.",
                           attachments=[
                               dict(tm.file_attachment("ATT-Z", zipped([("terms.pdf", pdf(
                                   [["Terms"], ["Freight: USD 1,250.00"]]))]), "pack.zip"),
                                    contentType="application/zip"),
                               tm.file_attachment("ATT-S", pdf([[], []]), "scan.pdf")])
        self.mail = MailCaptureStore(tmp / "state", clock=self.clock)
        self.store = self.capture("ontology-1.sqlite")
        self.audience = LiveSourceAudience({f"mail:{tm.UPN_A}": [A, B], f"mail:{tm.UPN_B}": [A]},
                                           tenant="plx", classifications={"mail:*": "internal"})

    def put_invoice(self, change_key: str, quantity: str) -> None:
        text = INVOICE.format(qty=quantity)
        content, name, mime = ((pdf([text.splitlines(), ["Terms: net 30 days"]]), "pi.pdf",
                                "application/pdf") if self.paged
                               else (text.encode(), "pi.txt", "text/plain"))
        attachment = dict(tm.file_attachment("ATT-1", content, name), contentType=mime,
                          lastModifiedDateTime=self.clock.now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.graph.put(tm.BOX_A, "MSG-1", change_key, subject="PI", body="PI attached.",
                       attachments=[attachment])

    def capture(self, name: str) -> Path:
        api, _ = graph_read.build_graph(tm.ENV, sender=self.graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(self.mail, GraphMailReader(api, tm.capture_audience()), tm.CAPTURE,
                          sleep=lambda s: None).run(tm.SCOPE)
        assert run["complete"], run
        path = self.tmp / name
        with MailCaptures(self.tmp / "state", internal_domains=tm.SCOPE.internal_domains) as m:
            build_store(path, schema_loader.load_schema(), {"graph_mail": m},
                        clock=lambda: self.clock.now)
        return path

    def recapture(self) -> None:
        self.clock.now = RECAPTURED
        self.put_invoice("CK-1-2", "410,000")
        self.store = self.capture("ontology-2.sqlite")

    def tools(self, test: unittest.TestCase, review_file: Path, clock: dt.datetime
              ) -> ot.OntologyTools:
        """Tools as a restarted host opens them: both stores reopened from disk."""
        reader = ot.StoreReader.open(self.store)
        test.addCleanup(reader.conn.close)
        everyone = {A, B}
        policy = ot.ReadPolicy(tenant="plx", audiences={"mail:*": everyone},
                               markings={m: everyone for m in store_ddl.MARKINGS})
        return ot.OntologyTools(reader, policy, item_gate=self.mail.item_gate(self.audience),
                                schema=ot.SchemaView.from_compiled(schema_loader.load_schema()),
                                contracts=CONTRACTS, clock=lambda: clock,
                                reviews=reviews.ReviewStore(review_file))

    def reader(self, audience: LiveSourceAudience | None = None) -> ev.RuntimeCitations:
        """The ``plx.read`` reader of a host over the mail capture and a runtime admitting
        ``audience`` (default this world's)."""
        runtime = types.SimpleNamespace(
            live=types.SimpleNamespace(audience=audience or self.audience),
            employee_gate=lambda actor: actor, mail_scope=None,
            _employee_acl_denial=lambda *a: False,
            people_exclusion=PeopleExclusion.from_env(tm.ENV))
        return ev.RuntimeCitations(runtime, mail_capture_dir=self.tmp / "state")

    def pages(self, audience: LiveSourceAudience | None = None) -> object:
        """``ReviewTools.pages`` as a host reads them (``original_pages``): through the
        host's ``plx.read`` reader."""
        return rt.original_pages(types.SimpleNamespace(
            evidence=types.SimpleNamespace(reader=self.reader(audience))))

    def object_id(self, object_type: str, message: str) -> str:
        reader = ot.StoreReader.open(self.store)
        try:
            return reader.conn.execute(
                "SELECT object_id FROM objects WHERE object_type = ? AND object_id LIKE ?",
                (object_type, f"%{message}%")).fetchone()[0]
        finally:
            reader.conn.close()


class ValueReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory(prefix="value-review-")
        cls.world = World(Path(cls.tmp.name))
        cls.invoice = cls.world.object_id("SupplierConfirmation", "MSG-1")
        cls.supplier_mail = cls.world.object_id("Email", "MSG-2")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.world.mail.close()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.review_file = Path(tempfile.mkdtemp(dir=self.tmp.name)) / reviews.STORE_FILE

    def ok(self, name: str, result: dict) -> dict:
        self.assertEqual(validate(result, CONTRACTS["tools"][name]["output"], root=CONTRACTS),
                         [], json.dumps(result)[:1500])
        self.assertNotIn("error", result, json.dumps(result)[:1500])
        return result

    def review(self, arguments: dict, world: World | None = None, **options) -> dict:
        """One ``plx.review`` call by employee A's console session."""
        session = tr.employee_session(tr.OID_A)
        store = reviews.ReviewStore(self.review_file, clock=lambda: REVIEWED_AT)
        world = world or self.world
        options.setdefault("pages", world.pages())
        tools = rt.ReviewTools(world.tools(self, self.review_file, REVIEWED_AT), store,
                               session=session, **options)
        return tools.call("plx.review", arguments, session.actor)

    def values(self, actor: Actor, *, clock: dt.datetime = NOW,
               world: World | None = None) -> dict:
        tools = (world or self.world).tools(self, self.review_file, clock)
        out = self.ok("plx.object", tools.call("plx.object", {"object_id": self.invoice}, actor))
        return {v["property"]: v for v in out["values"]}

    def investigate(self, actor: Actor, *, clock: dt.datetime = NOW,
                    world: World | None = None) -> dict[str, dict]:
        """The answer entries by name of one ``plx.investigate`` run for ``actor``."""
        table: dict = {}
        ot.register(table, (world or self.world).tools(self, self.review_file, clock))
        self.assertEqual(oi.check_plan(PLAN, ot.SchemaView.from_compiled(
            schema_loader.load_schema()), CONTRACTS, ENGINES), [])
        tools = oi.InvestigateTools(table, plans={"value_read": oi.Plan(
            PLAN, "procedure:value_read@" + "0" * 16)}, rows=lambda handle, reader: None,
            engines=ENGINES)
        result = tools.call("plx.investigate", {"family": "value-read", "root": self.invoice},
                            actor)
        self.ok("plx.investigate", result)
        entries = {e["name"]: e for e in result["answer"]}
        corrected = entries["quantity"].get("review", {}).get("decision") == "correct_value"
        # Only the engine that re-reads the source says it used the source value.
        self.assertEqual([g["subject"] for g in result["gaps"]
                          if g["reason"] == "input_person_corrected"],
                         ["total"] if corrected else [])
        self.assertEqual("reviews" in entries["doubled"], corrected)
        return entries

    def aggregate(self, actor: Actor, *, clock: dt.datetime = NOW) -> dict:
        base = self.world.tools(self, self.review_file, clock)
        return self.ok("plx.aggregate", oa.AggregateTools(base).call("plx.aggregate", {
            "set": base.sets.new(actor, [self.invoice]),
            "measure": {"op": "sum", "property": "quantity"}}, actor))

    def compare(self, actor: Actor) -> dict:
        tools = oa.AggregateTools(self.world.tools(self, self.review_file, NOW))
        out = self.ok("plx.compare", tools.call("plx.compare", {
            "left": self.invoice, "right": self.invoice, "properties": ["quantity"]}, actor))
        (row,) = out["rows"]
        return row

    def mail_span(self, world: World | None = None) -> dict:
        """The span of the supplier mail stating the quantity, as plx.object cites it."""
        tools = (world or self.world).tools(self, self.review_file, NOW)
        mail = tools.call("plx.object", {"object_id": self.supplier_mail},
                          tr.employee_session(tr.OID_A).actor)
        (body,) = [v for v in mail["values"] if v["property"] == "body_preview"]
        start = SAYS.index("40,000")
        return {key: body["citation"][key] for key in ("l1_handle", "digest", "field_path")} \
            | {"start": start, "end": start + len("40,000 pcs")}

    def correct(self, evidence: list[dict], value: dict | None = None,
                world: World | None = None, **options) -> dict:
        return self.review({"mode": "record", "decision": "correct_value",
                            "subject": self.invoice, "evidence": evidence,
                            "value": value or {"property": "quantity", "value": "40000",
                                               "unit": "ea"}}, world, **options)

    def test_correct_confirm_and_retract_round_trip_across_a_restart(self):
        a = tr.employee_session(tr.OID_A).actor
        before = self.values(a)
        self.assertEqual((before["quantity"]["value"], before["quantity"]["read_by"]),
                         ("400000", "parser"))
        recorded = self.ok("plx.review", self.correct([self.mail_span()]))
        self.assertEqual((recorded["review"]["state"], recorded["projection"],
                          recorded["business_system_writes"]),
                         ("effective", "plx_object_now", 0))
        price = before["unit_price"]["citation"]
        confirmed = self.ok("plx.review", self.review({
            "mode": "record", "decision": "confirm_value", "subject": self.invoice,
            "value": {"property": "unit_price"},
            "evidence": [{k: price[k] for k in ("l1_handle", "digest", "field_path")}]}))
        self.assertEqual(confirmed["review"]["value"],
                         {"property": "unit_price", "value": "0.04125", "unit": "ea",
                          "currency": "USD"})

        # Restarted: the source value stays; the person's value is beside it, both tiered.
        after = self.values(a)
        quantity = after["quantity"]
        self.assertEqual((quantity["value"], quantity["unit"], quantity["read_by"],
                          quantity["tier"]), ("400000", "ea", "parser", "lead"))
        self.assertEqual({k: quantity["review"][k] for k in (
            "decision", "value", "unit", "tier", "reviewed_by", "review_id", "evidence")},
            {"decision": "correct_value", "value": "40000", "unit": "ea", "tier": "reviewed",
             "reviewed_by": A, "review_id": recorded["review"]["review_id"],
             # The corrected value's own version is cited too: a new one withdraws it.
             "evidence": [self.mail_span(), {k: before["quantity"]["citation"][k] for k in (
                 "l1_handle", "digest", "field_path")}]})
        self.assertEqual((after["unit_price"]["value"], after["unit_price"]["read_by"],
                          after["unit_price"]["tier"], after["unit_price"]["review"]["decision"]),
                         ("0.04125", "human_reviewed", "reviewed", "confirm_value"))
        # The next investigation keeps the source value under its own citation, the
        # correction beside it; an engine taking reviewed inputs computes from the
        # person's value and cites the correction.
        answer = self.investigate(a)
        entry = answer["quantity"]
        self.assertEqual((entry["value"], entry["unit"], entry["citation"], entry["check"],
                          entry["code"], entry["review"]),
                         ("400000", "ea", before["quantity"]["citation"], "unverified",
                          "person_reviewed", quantity["review"]))
        self.assertEqual((answer["doubled"]["value"], answer["doubled"]["reviews"]),
                         ("80000", [{"input": "quantity", "review": quantity["review"]}]))
        # plx.aggregate sums the person's value citing the correction; plx.compare shows
        # it beside each side's value and compares what the sources state.
        out = self.aggregate(a)
        (group,) = out["groups"]
        self.assertEqual((group["value"], group["unit"]), ("40000", "ea"))
        cited = {(c["l1_handle"], c["field_path"]) for c in group["inputs_citations"]}
        span = self.mail_span()
        self.assertEqual(cited, {(span["l1_handle"], span["field_path"]),
                                 (before["quantity"]["citation"]["l1_handle"],
                                  before["quantity"]["citation"]["field_path"])})
        self.assertIn("person_corrected_value_used", {g["reason"] for g in out["gaps"]})
        row = self.compare(a)
        self.assertEqual((row["comparison"], row["left"]["value"], row["left"]["review"],
                          row["right"]["review"]),
                         ("equal", "400000", quantity["review"], quantity["review"]))

        retracted = self.ok("plx.review", self.review({
            "mode": "record", "decision": "retract",
            "retracts": recorded["review"]["review_id"]}))
        self.assertEqual(retracted["projection"], "plx_object_now")
        quantity = self.values(a)["quantity"]
        self.assertEqual((quantity["value"], quantity["read_by"]), ("400000", "parser"))
        self.assertNotIn("review", quantity)
        self.assertEqual(self.investigate(a)["doubled"]["value"], "800000")
        self.assertEqual(self.aggregate(a)["groups"][0]["value"], "400000")
        self.assertNotIn("review", self.compare(a)["left"])
        status = self.review({"mode": "status", "subject": self.invoice})
        self.assertEqual(sorted((r["decision"], r["state"]) for r in status["reviews"]),
                         [("confirm_value", "effective"), ("correct_value", "retracted"),
                          ("retract", "effective")])

    def test_a_correction_without_a_page_or_span_or_its_unit_is_refused(self):
        span = self.mail_span()
        whole = {k: span[k] for k in ("l1_handle", "digest", "field_path")}
        quantity = {"property": "quantity", "value": "40000"}
        for evidence, value, code in (
                ([whole], None, "citation_required"),
                ([dict(span, end=len(SAYS) + 1)], None, "evidence_span_invalid"),
                ([span], dict(quantity, value="4O000", unit="ea"), "value_invalid"),
                ([span], quantity, "unit_required"),
                ([span], dict(quantity, unit="ea", currency="USD"), "unit_not_applicable")):
            with self.subTest(code=code):
                self.assertEqual(self.correct(evidence, value)["error"]["code"], code)
        # Source text is never echoed outside its envelope: a text value is not reviewed.
        number = self.values(tr.employee_session(tr.OID_A).actor)["document_number"]
        self.assertEqual(self.review({
            "mode": "record", "decision": "confirm_value", "subject": self.invoice,
            "value": {"property": "document_number"}, "evidence": [{
                k: number["citation"][k] for k in ("l1_handle", "digest", "field_path")}]}
        )["error"]["code"], "value_not_reviewable")
        self.assertEqual(reviews.ReviewStore(self.review_file).history("plx", self.invoice), [])

    def test_a_reader_who_may_not_read_the_cited_mail_sees_neither_correction_nor_citation(self):
        span = self.mail_span()
        self.ok("plx.review", self.correct([span]))
        self.assertIn("review", self.values(tr.employee_session(tr.OID_A).actor)["quantity"])
        tools = self.world.tools(self, self.review_file, NOW)
        out = self.ok("plx.object", tools.call("plx.object", {"object_id": self.invoice},
                                               READER_B))
        (quantity,) = [v for v in out["values"] if v["property"] == "quantity"]
        self.assertEqual((quantity["value"], quantity["read_by"]), ("400000", "parser"))
        self.assertNotIn("review", quantity)
        self.assertNotIn(span["l1_handle"], json.dumps(out))
        self.assertNotIn("40000", json.dumps(out).replace("400000", ""))
        answer = self.investigate(READER_B)
        self.assertEqual((answer["quantity"]["value"], answer["doubled"]["value"]),
                         ("400000", "800000"))
        self.assertNotIn("review", answer["quantity"])
        aggregated, row = self.aggregate(READER_B), self.compare(READER_B)
        self.assertEqual(aggregated["groups"][0]["value"], "400000")
        self.assertNotIn("review", row["left"])
        for out in (answer, aggregated, row):
            self.assertNotIn(span["l1_handle"], json.dumps(out))
        status = rt.ReviewTools(tools, reviews.ReviewStore(self.review_file),
                                session=None).call("plx.review", {"mode": "status",
                                                                  "subject": self.invoice},
                                                   READER_B)
        self.assertEqual((status["reviews"], status["withheld"]["reviews"]), ([], 1))

    def test_a_new_version_of_the_record_withdraws_its_confirmation_and_correction(self):
        world = World(Path(tempfile.mkdtemp(dir=self.tmp.name)))
        self.addCleanup(world.mail.close)
        a = tr.employee_session(tr.OID_A).actor
        shown = self.values(a, world=world)["unit_price"]["citation"]
        self.ok("plx.review", self.review({
            "mode": "record", "decision": "confirm_value", "subject": self.invoice,
            "value": {"property": "unit_price"},
            "evidence": [{k: shown[k] for k in ("l1_handle", "digest", "field_path")}]}, world))
        # The correction cites only the supplier mail, which does not change.
        self.ok("plx.review", self.correct([self.mail_span(world)], world=world))
        values = self.values(a, world=world)
        self.assertEqual((values["unit_price"]["read_by"], values["unit_price"]["tier"],
                          values["quantity"]["review"]["value"]),
                         ("human_reviewed", "reviewed", "40000"))
        world.recapture()
        values = self.values(a, clock=LATER, world=world)
        self.assertEqual((values["quantity"]["value"], values["quantity"]["read_by"],
                          values["unit_price"]["read_by"]), ("410000", "parser", "parser"))
        self.assertNotIn("review", values["quantity"])
        self.assertNotIn("review", values["unit_price"])
        entry = self.investigate(a, clock=LATER, world=world)["quantity"]
        self.assertEqual(entry["value"], "410000")
        self.assertNotIn("review", entry)
        # As known before the new capture, both still held.
        tools = world.tools(self, self.review_file, LATER)
        then = {v["property"]: v for v in self.ok("plx.object", tools.call("plx.object", {
            "object_id": self.invoice, "as_known_at": NOW.isoformat()}, a))["values"]}
        self.assertEqual((then["unit_price"]["read_by"], then["quantity"]["review"]["value"]),
                         ("human_reviewed", "40000"))


    def test_aggregate_groups_by_a_corrected_value_and_keeps_a_unit_price_denominator(self):
        a = tr.employee_session(tr.OID_A).actor
        span = self.mail_span()
        self.ok("plx.review", self.correct([span]))
        # A unit price corrected in its currency only stays per its source denominator.
        self.ok("plx.review", self.correct([span], {"property": "unit_price", "value": "0.05",
                                                    "currency": "USD"}))
        base = self.world.tools(self, self.review_file, NOW)

        def aggregate(actor: Actor, arguments: dict) -> dict:
            return self.ok("plx.aggregate", oa.AggregateTools(base).call("plx.aggregate", dict(
                arguments, set=base.sets.new(actor, [self.invoice])), actor))
        by_quantity = {"measure": {"op": "count"}, "group_by": ["quantity"]}
        price = {"measure": {"op": "max", "property": "unit_price"}}
        out = aggregate(a, by_quantity)
        (group,) = out["groups"]
        self.assertEqual((group["key"]["quantity"]["untrusted_text"], group["value"]),
                         ("40000", "1"))
        # The key is shown as read where the correction cites it, and the group cites it.
        self.assertEqual(group["key"]["quantity"]["origin"]["citation"], span["l1_handle"])
        self.assertIn(span["l1_handle"], {c["l1_handle"] for c in group["inputs_citations"]})
        self.assertIn("person_corrected_value_used", {g["reason"] for g in out["gaps"]})
        (group,) = aggregate(a, price)["groups"]
        self.assertEqual((group["value"], group["unit"], group["currency"]),
                         ("0.05", "ea", "USD"))
        # A reader who may not read the cited mail groups by the source value, citing nothing
        # of the correction.
        for arguments, value in ((by_quantity, "400000"), (price, "0.04125")):
            out = aggregate(READER_B, arguments)
            (group,) = out["groups"]
            self.assertEqual(group["key"]["quantity"]["untrusted_text"]
                             if "group_by" in arguments else group["value"], value)
            self.assertNotIn(span["l1_handle"], json.dumps(out))

    @unittest.skipUnless(HAS_PDF, "a paged original is a PDF: needs pypdf and reportlab")
    def test_a_cited_page_is_checked_against_the_original(self):
        world = World(Path(tempfile.mkdtemp(dir=self.tmp.name)), paged=True)
        self.addCleanup(world.mail.close)
        version = world.tools(self, self.review_file, NOW).store.conn.execute(
            "SELECT l1_handle, digest FROM object_versions WHERE object_id = ?",
            (self.invoice,)).fetchone()
        page = {"l1_handle": version[0], "digest": version[1]}
        terms = len("Terms: net 30 days\n")

        def failed(*args):
            raise OSError("capture unreadable")
        for cited, options, code in (
                (dict(page, page=3), {}, "page_not_found"),     # the PDF has two pages
                (dict(page, page=2), {"pages": None}, "page_not_found"),  # host reads none
                (dict(page, page=2, start=0, end=terms + 1), {}, "evidence_span_invalid"),
                # A read that failed, or a page resolved with no text under a span, is
                # unknown; a status the review does not know is refused.
                (dict(page, page=2), {"pages": failed}, "page_text_unknown"),
                (dict(page, page=2, start=0, end=5),
                 {"pages": lambda *a: {"status": "resolved", "content": ""}},
                 "page_text_unknown"),
                (dict(page, page=2), {"pages": lambda *a: {"status": "partial"}},
                 "page_not_found")):
            with self.subTest(code=code, page=cited["page"], options=options):
                self.assertEqual(self.correct([cited], world=world, **options)["error"]["code"],
                                 code)
        recorded = self.ok("plx.review", self.correct([dict(page, page=2, start=0, end=5)],
                                                      world=world))
        # The handle the attachment and its document record share is read as the subject's.
        self.assertEqual(recorded["review"]["evidence"],
                         [dict(page, object_id=self.invoice, page=2, start=0, end=5)])

    @unittest.skipUnless(HAS_PDF, "a paged original is a PDF: needs pypdf and reportlab")
    def test_a_cited_page_is_taken_exactly_when_plx_read_resolves_it(self):
        """Direction review of 1843d3d14c: the page check is ``plx.read``'s own read
        (``CitationReader.read`` with ``page``), for the same reader, of a mail attachment,
        a zip member, a PDF of which no text was read and a message, for a reader the host
        admits and one it no longer admits (withheld before anything of the page is read,
        never an unknown to ask again about), and of a version captured again since; the
        case runtime's mail resolver refuses a page past the attachment's last page as
        ``plx.read`` does (review-page-one-read minors 3 and 4)."""
        world = World(Path(tempfile.mkdtemp(dir=self.tmp.name)), paged=True, pack=True)
        self.addCleanup(world.mail.close)
        actor = tr.employee_session(tr.OID_A).actor
        conn = sqlite3.connect(world.store)
        self.addCleanup(conn.close)
        versions = dict(conn.execute("SELECT object_id, l1_handle FROM object_versions"))
        cited = {"invoice": self.invoice, **{name: next(
            o for o in versions if o.startswith(kind) and needle in o) for name, kind, needle
            in (("member", "ContainerMember:", "terms.pdf"), ("scan", "Attachment:", "ATT-S"),
                ("message", "Email:", "MSG-3"))}}
        digests = dict(conn.execute("SELECT l1_handle, digest FROM object_versions"))
        revoked = LiveSourceAudience({f"mail:{tm.UPN_A}": [B], f"mail:{tm.UPN_B}": [A]},
                                     tenant="plx", classifications={"mail:*": "internal"})
        old_store = world.store
        outcomes = {}
        for case, audience in (("admitted", None), ("revoked", revoked), ("recaptured", None)):
            if case == "recaptured":
                world.recapture()
                world.store = old_store   # the projection the person read: the old version
            for name, object_id in cited.items():
                handle = versions[object_id]
                for number in (1, 2, 3):
                    read = ev.EvidenceTools(world.tools(self, self.review_file, NOW),
                                            reader=world.reader(audience)).call(
                        "plx.read", {"citation": page_citation(handle, number)}, actor)
                    review = self.correct([{"l1_handle": handle, "digest": digests[handle],
                                            "page": number}], world=world,
                                          pages=world.pages(audience))
                    with self.subTest(case=case, cited=name, page=number):
                        self.assertEqual("error" not in review, read["status"] == "resolved",
                                         (read, review))
                        if case == "revoked":
                            self.assertEqual(read["status"], "withheld")
                            self.assertNotIn("ask again", json.dumps(review).lower())
                    outcomes[case, name, number] = review.get("error", {}).get("code", "taken")
        # A case citing a page: the runtime's one mail resolver, through the same gates
        # (a staff reader of the orders mailbox, one of the finance mailbox only: no
        # employee entitlements here).
        buyer, outsider = (Actor("plx", frozenset({p})) for p in ("staff:buyer", "staff:x"))
        runtime = CaseRuntime(Path(tempfile.mkdtemp(dir=self.tmp.name)), client=None,
                              audience=mirror_audience(world.tmp), tenant="plx",
                              live=LiveReaders(audience=LiveSourceAudience(
                                  {f"mail:{tm.UPN_A}": ["staff:buyer"],
                                   f"mail:{tm.UPN_B}": ["staff:x"]}, tenant="plx",
                                  classifications={"mail:*": "internal"})),
                              mail_scope=MailScope.unrestricted(),
                              mail_capture_dir=world.tmp / "state",
                              people_exclusion=PeopleExclusion.from_env(tm.ENV))
        self.addCleanup(runtime.close)
        for name in ("invoice", "member", "scan"):
            handle = versions[cited[name]]
            for number in (1, 2, 3):
                cites = page_citation(handle, number)
                with self.subTest(case="case citation", cited=name, page=number):
                    self.assertIsNone(runtime.mail_capture(cites, outsider))   # tells nothing
                    if name != "scan" and number == 3:   # past the last page of the text
                        with self.assertRaisesRegex(CitationUnresolvable, "page_not_found"):
                            runtime.mail_capture(cites, buyer)
                    else:   # a page it holds, or one whose existence the capture cannot tell
                        self.assertEqual(runtime.mail_capture(cites, buyer)["citation"], cites)
        unknown, refused = ("page_text_unknown",) * 3, ("page_not_found",) * 3
        self.assertEqual({key: tuple(outcomes[key[0], key[1], n] for n in (1, 2, 3))
                          for key in {k[:2] for k in outcomes}}, {
            ("admitted", "invoice"): ("taken", "taken", "page_not_found"),
            ("admitted", "member"): ("taken", "taken", "page_not_found"),
            ("admitted", "scan"): unknown,
            ("admitted", "message"): ("page_not_found",) * 3,   # a message has no pages
            ("revoked", "invoice"): refused, ("revoked", "member"): refused,
            ("revoked", "scan"): refused, ("revoked", "message"): refused,
            # An attachment is pinned to its bytes: a later capture of its message does
            # not change the cited page.
            ("recaptured", "invoice"): ("taken", "taken", "page_not_found"),
            ("recaptured", "member"): ("taken", "taken", "page_not_found"),
            ("recaptured", "scan"): unknown,
            ("recaptured", "message"): ("page_not_found",) * 3})

    def test_a_shared_handle_is_read_only_as_the_record_the_decision_is_about(self):
        shown = self.values(tr.employee_session(tr.OID_A).actor)["document_number"]["citation"]
        holders = {row[0] for row in sqlite3.connect(self.world.store).execute(
            "SELECT object_id FROM object_versions WHERE l1_handle = ?", (shown["l1_handle"],))}
        self.assertEqual(len(holders), 2)   # the attachment and the document read from it
        refused = self.review({
            "mode": "record", "decision": "alias", "subject": self.supplier_mail,
            "alias": {"identifier_kind": "pi_ref", "value": "PI-60311"},
            "evidence": [{k: shown[k] for k in ("l1_handle", "digest", "field_path")}]},
            gate=tr.identity_reviewer)
        self.assertEqual(refused["error"]["code"], "evidence_ambiguous")
        self.assertEqual(reviews.ReviewStore(self.review_file).history(
            "plx", self.supplier_mail), [])

class ReviewedEngineTests(unittest.TestCase):
    """The landed-cost and order-margin engines take reviewed inputs (decision: a computed
    entry over a person-corrected input computes from the corrected value and cites the
    correction): each re-reads for the actor the value a correction corrects and every
    record it cites, computes from the person's value, and its entries, the totals over
    them included, cite the correction; no ``input_person_corrected`` gap stands."""
    MAIL = "graph-mail://finance@plx.example/MSG-9@CK-9"

    def landed(self, corrections: dict, readable: bool = True) -> tuple[dict, dict]:
        """``plx.investigate`` landed_cost over the canned host of
        ``tests/test_investigate_landed_cost.py``, its ``plx.object`` showing a person's
        correction (citing a mail span and the corrected value) of each named property;
        ``readable``: the citation reader resolves that mail for this reader."""
        host = lt.CannedHostTests()
        table = host.table()
        shown = table["plx.object"]["handler"]

        def obj(arguments, actor):
            out = json.loads(json.dumps(shown(arguments, actor)))
            for value in out["values"]:
                if value["property"] in corrections:
                    cited = value["citation"]
                    value["review"] = {
                        "review_id": "rev-" + "1" * 32, "decision": "correct_value",
                        "tier": "reviewed", "reviewed_by": A,
                        "reviewed_at": REVIEWED_AT.isoformat(),
                        "evidence": [{"l1_handle": self.MAIL, "digest": "c" * 64,
                                      "field_path": "row.body_preview", "start": 0, "end": 5},
                                     {k: cited[k] for k in ("l1_handle", "digest",
                                                            "field_path")}],
                        **corrections[value["property"]]}
            return out
        table["plx.object"] = {"handler": obj}

        def rows(handle, actor):
            if handle == self.MAIL:
                return {"citation": handle, "relation": "mail", "key": {},
                        "row": {"body_preview": "60 USD"}} if readable else None
            return host.rows(handle, actor)
        result = oi.InvestigateTools(table, rows=rows, scope=host.scope).call(
            "plx.investigate", {"family": "landed_cost", "root": host.PO, "as_known_at": lt.AT},
            Actor("plx", frozenset({"x"})))
        lt._ok(self, result)
        return result, {e["name"]: e for e in result["answer"]}

    def test_landed_cost_computes_from_corrected_freight_and_a_unit_less_price(self):
        for prop, fix, line, total in (
                # freight 60 / 40 ea; goods 2.5 x 40 = 100; duty 12: 172 / 40
                ("shipping_cost_estimate", {"value": "60", "currency": "USD"}, "4.3", "172"),
                # goods 3 per ea (the price's own denominator) x 40 = 120: 232 / 40
                ("unit_price", {"value": "3", "currency": "USD"}, "5.8", "232")):
            with self.subTest(prop=prop):
                result, entries = self.landed({prop: fix})
                (name,) = [n for n, e in entries.items() if e.get("property") == prop]
                corrected = entries[name]
                # The source value stays under its citation, the correction beside it.
                self.assertEqual((corrected["value"], corrected["review"]["value"],
                                  corrected["code"]),
                                 ("100" if prop == "shipping_cost_estimate" else "2.5",
                                  fix["value"], "person_reviewed"))
                cites = [{"input": name, "review": corrected["review"]}]
                for entry, value in ((entries["landed_cost_line_1"], line),
                                     (entries["landed_cost"], total)):
                    self.assertEqual((entry["value"], entry["reviews"]), (value, cites))
                self.assertNotIn("input_person_corrected",
                                 {g["reason"] for g in result["gaps"]})
        # A correction citing a record this reader's citation reader does not resolve is not
        # used: nothing is computed from it, nor from the source value it corrects.
        result, entries = self.landed({"shipping_cost_estimate": {"value": "60",
                                                                  "currency": "USD"}},
                                      readable=False)
        self.assertEqual((entries["landed_cost_line_1"]["value"], entries["landed_cost"]["value"]),
                         (None, None))
        self.assertNotEqual(result["answerability"]["state"], "answerable")

    def test_order_margin_computes_from_a_corrected_invoice_amount(self):
        def ask(currency: str) -> tuple[dict, dict, dict]:
            """SO-3 of the order-margin world with revenue_1 (10 on a CAD invoice) corrected
            to 15 ``currency``."""
            case = margin.OrderMarginTests("test_a_wholly_invoiced_order_gets_line_and_total_"
                                           "margin_with_bc_citations")
            case.setUp()
            self.addCleanup(case.doCleanups)
            line = {e["name"]: e for e in case.ask(tb.CO[0], "SO-3")["answer"]}["revenue_1"]
            tmp = Path(tempfile.mkdtemp(prefix="value-review-margin-"))
            self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
            at = case.world.base.clock()
            store = case.world.base.reviews = reviews.ReviewStore(tmp / reviews.STORE_FILE,
                                                                  clock=lambda: at)
            cited = line["citation"]
            store.record(tenant=margin.FULL.tenant, author={"principal": A},
                         decision="correct_value", subject=line["object_id"],
                         evidence=[{"object_id": line["object_id"],
                                    "l1_handle": cited["l1_handle"], "digest": cited["digest"],
                                    "field_path": cited["field_path"], "start": 0, "end": 2}],
                         value={"property": line["property"], "value": "15",
                                "currency": currency})
            result = case.ask(tb.CO[0], "SO-3")
            self.assertIn("answer", result, json.dumps(result)[:1500])
            return result, {e["name"]: e for e in result["answer"]}, line
        result, entries, line = ask("CAD")
        self.assertEqual((entries["revenue_1"]["value"], entries["revenue_1"]["review"]["value"]),
                         (line["value"], "15"))
        # Revenue 15 + 10 = 25 less booked cost 8: margin 17, 68% on price.
        self.assertEqual([entries[n]["value"] for n in (
            "order_margin_line_1_revenue", "order_margin_line_1_margin", "order_margin_revenue",
            "order_margin", "order_margin_margin_pct")], ["25", "17", "25", "17", "68"])
        cites = [{"input": "revenue_1", "review": entries["revenue_1"]["review"]}]
        for name in ("order_margin_line_1_revenue", "order_margin_line_1_margin",
                     "order_margin", "order_margin_margin_pct"):
            self.assertEqual(entries[name]["reviews"], cites, name)
        self.assertNotIn("reviews", entries["order_margin_line_1_booked_cost"])
        self.assertNotIn("input_person_corrected", {g["reason"] for g in result["gaps"]})
        # A correction in another currency than the cited invoice's is never labelled with
        # the invoice's currency: nothing is converted and no figure rests on it.
        result, entries, _ = ask("USD")
        self.assertEqual([entries[n]["value"] for n in (
            "order_margin_line_1_revenue", "order_margin_line_1_margin", "order_margin_revenue",
            "order_margin")], [None, None, None, None])
        self.assertIn("correction_currency_differs", {g["reason"] for g in result["gaps"]})
        self.assertNotIn("reviews", entries["order_margin"])   # computed nothing from it


@unittest.skipUnless(HAS_PDF, "a paged file is a PDF: needs pypdf and reportlab")
class SharePointPageTests(unittest.TestCase):
    """A SharePoint PDF page and an operator original's page on the case host
    (``CaptureCaseHostTools`` over a SharePoint capture filled over the capture tests'
    in-process drive, and its projection): the page reader the host gives ``plx.review``
    (``original_pages``) is the reader its ``plx.read`` serves, through the capture's
    gates, for a reader on the file's ACL and one who is not, and for a version captured
    again since the projection (taken as the version the person read and cites: decision
    of review-page-one-read minor 1); a page past the last page of a file whose text was
    read does not exist, one of a file of which no text was read is unknown."""

    def setUp(self):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        temp = tempfile.TemporaryDirectory(prefix="value-review-sp-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.drive = tcs.seeded_drive()
        self.drive.file("01TERMS", "terms.pdf", "01LOTS",
                        pdf([["Terms"], ["Freight: USD 1,250.00"]]), acl=(tcs.RICARDO, tcs.SALES))
        self.drive.file("01SCAN", "scan.pdf", "01LOTS", pdf([[], []]),
                        acl=(tcs.RICARDO, tcs.SALES))
        store = cs.SharePointCaptureStore(self.root / "sharepoint", clock=tcs.Clock())
        self.addCleanup(store.close)
        api, transport = graph_read.build_graph(tcs.ENV, sender=self.drive, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        self.capture = cs.SharePointCapture(store, lambda d: graph_read.SharePointReader(
            api, tcs.capture_audience(), d, transport=transport, read_permissions=True,
            max_acl_reads=cs.MAX_ACL_READS), tcs.CAPTURE)
        self.assertTrue(self.capture.run(tcs.scope())["complete"])
        self.projection = self.root / "ontology.sqlite"
        with SharePointCaptures(self.root / "sharepoint") as files:
            build_store(self.projection, schema_loader.load_schema(), {"sharepoint": files},
                        clock=lambda: NOW)
        self.employee = Actor("plx", frozenset({"employee:" + hp.OID}))

    def host(self, identity: str) -> CaptureCaseHostTools:
        """The case host for the employee: quality, with the source identity
        ``identity`` (on the files' own ACL or not)."""
        resolver = EmployeePermissionResolver(
            EmployeePermissionPolicy.from_json({**hp.POLICY, "principals": {
                "staff:quality": hp.POLICY["principals"]["staff:quality"]}}),
            hp.Calls(hp.portal_user("MANAGER", email=identity)), hp.Calls(hp.member(hp.QA_GROUP)))
        runtime = CaseRuntime(Path(tempfile.mkdtemp(dir=self.root)), client=None,
                              audience=mirror_audience(self.root), tenant="plx",
                              live=LiveReaders(audience=tcs.reader_audience()),
                              employee_permissions=resolver)
        self.addCleanup(runtime.close)
        host = CaptureCaseHostTools(runtime, self.employee, store_path=self.projection,
                                    sharepoint_capture_dir=self.root / "sharepoint",
                                    clock=lambda: tcs.Clock().now + dt.timedelta(hours=1))
        self.addCleanup(host.close)
        return host

    def review(self, host: CaptureCaseHostTools, handle: str, digest: str, page: int) -> dict:
        """``plx.review`` as ``register`` builds it on ``host``, for the employee's console
        session: correct the file's size citing ``page``."""
        session = SessionIdentity(tenant="plx", channel=CHANNEL_CONSOLE,
                                  author_kind=CHANNEL_AUTHOR[CHANNEL_CONSOLE],
                                  proof=PROOF_CONFIGURED,
                                  employee=tr.EmployeePrincipal(tr.ENTRA, hp.OID),
                                  principals=self.employee.principals,
                                  identities=frozenset({hp.OID}))
        self.assertEqual(session.actor, self.employee)
        tools = rt.ReviewTools(host.tools, reviews.ReviewStore(
            Path(tempfile.mkdtemp(dir=self.root)) / reviews.STORE_FILE), session=session,
            gate=host.runtime.employee_gate, policy_for=host.policy_now,
            pages=rt.original_pages(host))
        (subject,) = sqlite3.connect(self.projection).execute(
            "SELECT object_id FROM object_versions WHERE l1_handle = ?", (handle,)).fetchone()
        return tools.call("plx.review", {
            "mode": "record", "decision": "correct_value", "subject": subject,
            "value": {"property": "size", "value": "1250"},
            "evidence": [{"l1_handle": handle, "digest": digest, "page": page}]}, self.employee)

    def test_a_cited_page_is_taken_exactly_when_plx_read_resolves_it(self):
        versions = dict(sqlite3.connect(self.projection).execute(
            "SELECT l1_handle, digest FROM object_versions WHERE l1_handle LIKE ? OR "
            "l1_handle LIKE ?", ("%01TERMS%", "%01SCAN%")))
        outcomes = {}
        for case, identity in (("admitted", tcs.SALES), ("recaptured", tcs.SALES),
                               ("not_on_acl", "someone.else@example-plx.com")):
            if case == "recaptured":
                self.drive.change("01TERMS", pdf([["Terms"], ["Freight: USD 1,300.00"]]))
                self.assertTrue(self.capture.run(tcs.scope())["complete"])
            host = self.host(identity)
            for handle, digest in versions.items():
                item = "01TERMS" if "01TERMS" in handle else "01SCAN"
                for number in (1, 2, 3):
                    read = host.call("plx.read", {"citation": handle, "page": number},
                                     self.employee)
                    review = self.review(host, handle, digest, number)
                    with self.subTest(case=case, item=item, page=number):
                        self.assertEqual("error" not in review, read["status"] == "resolved",
                                         (read, review))
                        if case == "not_on_acl":   # the file's refusal: no page told apart
                            self.assertEqual(read["status"], "withheld")
                    outcomes[case, item, number] = review.get("error", {}).get("code", "taken")
                    if (case, item, number) == ("recaptured", "01TERMS", 2):
                        # The superseded version's page, as read, under its own citation.
                        self.assertIn("1,250", read["content"]["untrusted_text"])
                        self.assertIn("superseded_in_capture",
                                      [gap["reason"] for gap in read["gaps"]])
                        self.assertEqual(review["review"]["evidence"][0]["l1_handle"], handle)
        unknown = ("page_text_unknown",) * 3
        self.assertEqual({key: tuple(outcomes[key[0], key[1], n] for n in (1, 2, 3))
                          for key in {k[:2] for k in outcomes}}, {
            ("admitted", "01TERMS"): ("taken", "taken", "page_not_found"),
            ("admitted", "01SCAN"): unknown,
            # plx.read serves the version the person read, labelled superseded in capture.
            ("recaptured", "01TERMS"): ("taken", "taken", "page_not_found"),
            ("recaptured", "01SCAN"): unknown,
            # The file is not served to this reader: no record, before any page is read.
            ("not_on_acl", "01TERMS"): ("object_not_found",) * 3,
            ("not_on_acl", "01SCAN"): ("object_not_found",) * 3})

    def test_an_original_page_is_read_by_the_reader_plx_read_uses(self):
        host = self.host(tcs.SALES)
        originals = host.runtime.originals
        inbox = originals.inbox / "staff:quality"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "packet.pdf").write_bytes(pdf([["Packet"], ["Filled 1,502"]]))
        handle = originals.attach(self.employee, "packet.pdf")["citation"]
        pages = rt.original_pages(host)
        outsider = Actor("plx", frozenset({"staff:outsider"}))
        for actor in (self.employee, outsider):
            for number in (1, 2, 3):
                read = host.call("plx.read", {"citation": handle, "page": number}, actor)
                with self.subTest(actor=sorted(actor.principals), page=number):
                    self.assertEqual(pages(handle, number, actor)["status"], read["status"])
                    self.assertEqual(read["status"] == "resolved",
                                     actor is self.employee and number < 3)


class StoreFormatTest(unittest.TestCase):
    def test_a_v2_store_keeps_its_history_and_takes_value_reviews(self):
        source = V2_SOURCE.read_bytes()
        self.assertEqual(hashlib.sha1(b"blob %d\0" % len(source) + source).hexdigest(), V2_BLOB)
        v2 = types.ModuleType("reviews_v2")
        exec(compile(source, str(V2_SOURCE), "exec"), v2.__dict__)
        cited = [{"object_id": "Email:x:1", "l1_handle": "graph-mail://b/M@CK", "digest": "0" * 64}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / reviews.STORE_FILE
            old = v2.ReviewStore(path, clock=lambda: REVIEWED_AT)
            author = {"principal": A}
            link, _ = old.record(tenant="plx", author=author, decision="accept_link",
                                 subject="A:x:1", target="B:x:2", link_type="A.to_b",
                                 evidence=cited)
            share, _ = old.record(tenant="plx", author=author, decision="allocate",
                                  subject="A:x:1", target="B:x:2", link_type="A.to_b",
                                  allocation={"property": "qty", "quantity": "2", "unit": "ea"},
                                  evidence=cited)
            store = reviews.ReviewStore(path, clock=lambda: NOW)
            self.assertEqual(store.link_history("plx"), old.link_history("plx"))
            record, _ = store.record(tenant="plx", author=author, decision="correct_value",
                                     subject="A:x:1", evidence=[dict(cited[0], start=0, end=4)],
                                     value={"property": "qty", "value": "3", "unit": "ea"})
            self.assertEqual((record["seq"], record["value"]),
                             (share["seq"] + 1, {"property": "qty", "value": "3", "unit": "ea"}))
            self.assertEqual([r["review_id"] for r in store.history("plx", "A:x:1")],
                             [link["review_id"], share["review_id"], record["review_id"]])
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT value FROM review_meta WHERE key = 'format'"
                                            ).fetchone()[0], reviews.FORMAT)
                for statement in ("UPDATE reviews SET subject = 'x'", "DELETE FROM reviews"):
                    with self.assertRaisesRegex(sqlite3.DatabaseError, "reviews_append_only"):
                        db.execute(statement)


if __name__ == "__main__":
    unittest.main()
