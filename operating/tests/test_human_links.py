"""A person confirms the link a blocked case is missing (operating/human_links.py).

The packaging landed-cost case blocks with ``pi_not_linked_to_po`` when a proforma
invoice names no purchase order, and the PO document chain's supplier numbers join
nothing until a person says which purchase order they name. A signed-in (here: fixture)
employee holding ``review:identity`` confirms the link in the console; it is a cited
``plx.review`` decision with author and time, the next projection build uses it, and
undo retracts it. Nothing here creates a link on its own.

Evidence label: isolated-fixture (SYNTHETIC PDFs on an injected SharePoint drive, Portal
rows from a static source), and injected-transport for the live-check command
(``verify/live-checks/case.human_link.json`` over a FakeWorkbench and an injected Graph
drive, replayed through the live checks). Nothing here is live.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

NEEDS = "the proforma invoices are read from PDFs: needs pypdf and reportlab"
HAVE_PDF = bool(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"))
PI_NUMBER = "PI-HL-0001"
PI_LINES = ["PROFORMA INVOICE", f"PI No: {PI_NUMBER}", "Date: 2026-09-12",
            "Quantity: 1,000 pcs", "Unit Price: USD 2.50/pc", "Total Amount: USD 2,500.00"]
# A second document naming the supplier's number only (never the purchase order).
NOTE_LINES = ["DELIVERY NOTE", f"Supplier confirmation: {PI_NUMBER}", "Packages: 4"]
PIN = "co-hlpi00000001"
ARGS = json.loads((ROOT / "verify" / "live-checks" / "case.packaging_landed_cost.json")
                  .read_text("utf-8"))["command"][3:]
FIXTURE = json.loads((ROOT / "operating" / "tests" / "fixtures" / "packaging_case_fixture.json")
                     .read_text("utf-8"))
[PROFORMA] = [ARGS[i + 1] for i, arg in enumerate(ARGS) if arg == "--proforma"]
DEST, AIR = [ARGS[i + 1] for i, arg in enumerate(ARGS) if arg == "--quote"]
ZIP_PIN, SECOND_PI = "co-synthzip0001", "co-synthpi00002"
# SYNTHETIC documents shaped like the packaging case's (no real body, price or name).
PIS = {"pi-a.pdf": ["PROFORMA INVOICE", "PI No: PI-SYN-A", "Date: 2026-09-10",
                    "Customer PO: PO5447", "Quantity: 40,000 pcs", "Unit Price: USD 0.94/pc",
                    "Total Amount: USD 37,600.00"],
       "pi-b.pdf": ["PROFORMA INVOICE", "PI No: PI-SYN-B", "Date: 2026-09-20",
                    "Customer PO: PO5447", "Quantity: 40,000 pcs",
                    "Unit Price: USD 1.0025/pc", "Total Amount: USD 40,100.00"]}
QUOTES = {"quote-40hq.pdf": ["FREIGHT QUOTATION", "Quote No: FQ-SYN-1", "Ref PO: PO5447",
                             "Scope: destination charges", "Basis: per 40HQ", "Containers: 1",
                             "Includes: destination charges, delivery",
                             "Excludes: duties and taxes", "Valid until: 2099-01-31",
                             "Total amount: USD 1,710.00"],
          "quote-air.pdf": ["FREIGHT QUOTATION", "Quote No: FQ-SYN-2", "Ref PO: PO5447",
                            "Scope: door to door, air freight, all-in", "Basis: per shipment",
                            "Includes: pickup, export clearance, air freight, delivery",
                            "Excludes: duties and taxes, listed surcharges and contingencies",
                            "Valid until: 2099-01-31", "All-in total: USD 83,965.56"]}
PINS = {"pi-a.pdf": PROFORMA, "pi-b.pdf": SECOND_PI, "quote-40hq.pdf": DEST,
        "quote-air.pdf": AIR}


def _pdf(lines: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, invariant=1)   # the same bytes on every build
    for y, line in enumerate(lines):
        page.drawString(60, 780 - 18 * y, line)
    page.showPage()
    page.save()
    return buffer.getvalue()


def _refuse_network(*_args, **_kwargs):
    raise AssertionError("network I/O attempted")


def _row(pin: str, kind: str, content: bytes, parent: str | None) -> dict:
    return {"id": pin, "status": "pinned", "kind": kind, "parent": parent, "source": {},
            "expected": {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}}


@unittest.skipUnless(HAVE_PDF, NEEDS)
class HumanLinkCaseTests(unittest.TestCase):
    """The case over a capture and a projection rebuilt after each decision."""

    @classmethod
    def setUpClass(cls) -> None:
        import test_investigate_landed_cost as T
        from integrations.tests import test_capture_sharepoint as tsp
        from ontology.types import Actor

        cls.T = T
        cls.case = tsp.CaptureCase("run")
        cls.case.setUp()
        cls.pi_bytes = T._quote_pdf(PI_LINES)
        cls.case.drive.file("01PIHL", "01PIHL proforma.pdf", "01AOS", cls.pi_bytes)
        cls.case.drive.file("01NOTE", "01NOTE delivery note.pdf", "01AOS",
                            T._quote_pdf(NOTE_LINES))
        run = cls.case.capture().run(tsp.scope())
        assert run["complete"], run
        cls.files, cls.audience = cls.case.store, tsp.reader_audience()
        cls.rows = [
            T.po("public.app_purchase_order", {"id": "hl-po-1"},
                 T._po("hl-po-1", "PO100975", "USD", "500.0")),
            # The purchase order was sent at $0.000: the goods come from the PI or nowhere.
            T.po("public.app_purchase_order_line", {"id": "hl-l-1"},
                 T._line("hl-l-1", "hl-po-1", "hl-p-1", "1000.0", "1000.0", "0.000", "ea")),
            T.po("public.app_product", {"id": "hl-p-1"},
                 {"id": "hl-p-1", "code": "HL-BTL", "hs_code": "3923.30",
                  "units_per_box": "24", "updated_at": T.UPDATED})]
        cls.portal = {r.l1_handle: r for r in cls.rows}
        cls.Actor = Actor

    @classmethod
    def tearDownClass(cls) -> None:
        cls.case.tearDown()

    def setUp(self) -> None:
        from ontology import reviews

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name) / "workbench"
        self.work.mkdir()
        self.builds = 0
        self.store = reviews.ReviewStore(reviews.store_path(self.work))
        self.reviewer = self.session({"staff:quality", "review:identity"})
        self.path = self.build()

    # ---------------------------------------------------------------- fixture

    def session(self, principals, channel=None):
        from operating.session_identity import CHANNEL_CONSOLE, fixture_session

        return fixture_session("plx", principals, channel=channel or CHANNEL_CONSOLE,
                               clearance="restricted")

    def build(self) -> Path:
        """A new projection over the capture and the review store as it is now."""
        from ontology import schema as schema_loader
        from ontology.project import SharePointCaptures, build_store
        from operating import build_steps

        self.builds += 1
        path = self.work.parent / f"ontology{self.builds}.sqlite"
        with SharePointCaptures(self.case.state) as captures:
            self.report = build_store(
                path, schema_loader.load_schema(),
                {"portal_api": self.T.StaticPortal(self.rows), "sharepoint": captures},
                clock=lambda: self.T.QUOTE_NOW,
                post_project=build_steps.post_project(self.work, "plx"))
        return path

    def tools(self, session):
        """The session's console call over the newest projection: the read tools, the
        landed-cost investigation and ``plx.review`` (the session is its author)."""
        from ontology import document_fields, store_ddl
        from ontology.resolve import ReviewedAliases
        from operating import ontology_tools as ot
        from operating import ontology_tools_investigate as oi
        from operating import ontology_tools_traverse as tt
        from operating.ontology_tools_review import ReviewTools

        actor = session.actor
        who = set(actor.principals)
        policy = ot.ReadPolicy(tenant="plx", audiences={"api:*": who, "sharepoint:*": who},
                               markings={m: who for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        base = ot.OntologyTools(reader, policy,
                                schema=ot.default_schema(), grammar=ot.default_grammar(),
                                item_gate=self.files.item_gate(self.audience),
                                reviews=self.store, aliases=ReviewedAliases(self.store))
        cited = document_fields.capture_citation_reader(self.files, actor, self.audience)

        def rows(handle, reader):
            record = self.portal.get(handle)
            if record is not None:
                return {"citation": handle, "relation": record.relation, "key": {},
                        "row": dict(record.row)}
            found = cited(handle)
            return ({"citation": handle, "relation": found["relation"], "key": {},
                     "row": found["row"]} if found.get("ok") else None)

        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(table, rows=rows, scope=oi.read_scope(base)))
        table["plx.review"] = {"handler": lambda arguments, caller: ReviewTools(
            base, self.store, session=session).call("plx.review", arguments, caller)}
        return lambda name, arguments: table[name]["handler"](dict(arguments), actor)

    def ids(self, call):
        po = call("plx.find", {"query": "PO100975"})["candidates"][0]["object"]["object_id"]
        [sc] = [c["object"]["object_id"] for c in call("plx.find", {"query": PI_NUMBER})[
            "candidates"] if c["object"]["object_type"] == "SupplierConfirmation"]
        return po, sc

    def landed_cost(self, call, po):
        """The case's answer and the independent oracle's verdict on it, as the reader."""
        from evals.oracle import landed_cost as oracle
        from scripts import eval_packaging_oracle_check as harness

        result = harness.investigate(call, po)
        self.assertNotIn("error", result)
        registry = {"rows": [{"id": PIN, "status": "pinned",
                              "expected": {"sha256": hashlib.sha256(self.pi_bytes).hexdigest()}}]}
        read, close = harness.cited_reader(Path(self.case.state), self.reviewer.actor,
                                           self.audience)
        try:
            verdict = oracle.check(result, read, registry, quotes=[], proformas=[PIN],
                                   text_of=harness.cited_text, held_markings=["finance"])
        finally:
            close()
        return result, verdict

    # ---------------------------------------------------------------- tests

    def test_a_confirmed_link_answers_the_blocked_case_citing_the_person_and_undo_blocks_it(
            self) -> None:
        from operating import human_links

        call = self.tools(self.reviewer)
        po, sc = self.ids(call)
        result, verdict = self.landed_cost(call, po)
        self.assertIn("pi_not_linked_to_po", verdict["missing_codes"], verdict)
        self.assertNotIn("landed_cost_goods", {e["name"] for e in result["answer"]})

        prepared = human_links.request(call, {"mode": "prepare_link", "record": po,
                                              "document": PI_NUMBER})
        self.assertEqual(prepared["status"], "ready", prepared)
        review = prepared["review"]
        self.assertEqual((review["decision"], review["subject"], review["target"],
                          review["link_type"]),
                         ("accept_link", sc, po, "SupplierConfirmation.confirms"))
        # The decision cites the fields the person read: the PI's own number and the PO's.
        self.assertEqual(sorted(e["field_path"] for e in review["evidence"]),
                         ["row.fields.document_number.value", "row.po_number"])
        self.assertEqual(self.store.link_history("plx"), [], "preparing writes nothing")

        confirmed = human_links.request(call, {"mode": "confirm", "review": review})
        self.assertEqual(confirmed["status"], "confirmed", confirmed)
        recorded = confirmed["review"]
        author = recorded["author"]["principal"]
        self.assertTrue(author.startswith("fixture:") and recorded["recorded_at"])
        self.assertEqual(confirmed["business_system_writes"], 0)

        self.path = self.build()
        call = self.tools(self.reviewer)
        result, verdict = self.landed_cost(call, po)
        self.assertNotIn("pi_not_linked_to_po", verdict["missing_codes"], verdict)
        goods = {e["name"]: e for e in result["answer"]}["landed_cost_goods"]
        self.assertEqual((goods["value"], goods["currency"], goods["basis"]),
                         ("2500.00", "USD", [sc]))
        self.assertEqual(verdict["goods"], "one_amount", verdict)
        # The link the answer stands on is the person's: who, when, which review.
        around = call("plx.around", {"from": po, "path": [
            {"link_type": "SupplierConfirmation.confirms", "direction": "reverse"}],
            "tier_floor": "lead", "response_format": "detailed"})
        [edge] = around["edges"]
        self.assertEqual(edge["review"], {
            "review_id": recorded["review_id"], "decision": "accept_link",
            "reviewed_by": author, "reviewed_at": recorded["recorded_at"],
            "state": "effective"})
        [reason] = edge["reason"]
        self.assertEqual((reason["feature"], sorted(c["field_path"] for c in reason[
            "citations"])), ("person_reviewed", sorted(e["field_path"]
                                                       for e in review["evidence"])))
        listed = human_links.request(call, {"mode": "links", "record": po})
        self.assertEqual([(link["review_id"], link["author"]["principal"])
                          for link in listed["links"]], [(recorded["review_id"], author)])

        undone = human_links.request(call, {"mode": "undo", "record": po,
                                            "review_id": recorded["review_id"]})
        self.assertEqual(undone["status"], "undone", undone)
        self.path = self.build()
        call = self.tools(self.reviewer)
        result, verdict = self.landed_cost(call, po)
        self.assertIn("pi_not_linked_to_po", verdict["missing_codes"], verdict)
        self.assertNotIn("landed_cost_goods", {e["name"] for e in result["answer"]})
        self.assertEqual(human_links.request(call, {"mode": "links", "record": po})["links"],
                         [])

    def test_a_person_without_the_grant_is_refused_in_plain_words_and_nothing_is_written(
            self) -> None:
        from operating import human_links
        from operating.session_identity import CHANNEL_MCP

        call = self.tools(self.reviewer)
        po, _ = self.ids(call)
        review = human_links.request(call, {"mode": "prepare_link", "record": po,
                                            "document": PI_NUMBER})["review"]
        staff = self.tools(self.session({"staff:quality"}))
        refused = human_links.request(staff, {"mode": "confirm", "review": review})
        self.assertEqual((refused["status"], refused["code"]),
                         ("refused", "identity_review_required"), refused)
        self.assertEqual(refused["says"], human_links.PLAIN["identity_review_required"])
        self.assertIn("review:identity", refused["says"])
        self.assertNotIn("_", refused["says"].replace("review:identity", ""))
        # An agent session proposes; only a person confirms, grant or not.
        agent = self.tools(self.session({"staff:quality", "review:identity"},
                                        channel=CHANNEL_MCP))
        self.assertEqual(human_links.request(agent, {"mode": "confirm", "review": review})[
            "code"], "review_needs_a_person")
        self.assertEqual(self.store.link_history("plx"), [])
        # Undo is as guarded as the confirmation: the person without the grant cannot.
        confirmed = human_links.request(call, {"mode": "confirm", "review": review})
        undo = {"mode": "undo", "record": po, "review_id": confirmed["review"]["review_id"]}
        self.assertEqual(human_links.request(staff, undo)["code"], "identity_review_required")
        self.assertEqual(len(self.store.link_history("plx")), 1)
        # Only a link or an alias is confirmed here; anything else fails closed, unsent.
        for bad in ({"mode": "confirm", "review": {**review, "decision": "reject_link"}},
                    {"mode": "confirm", "review": {**review, "decision": "retract"}},
                    {"mode": "confirm", "review": {**review, "extra": 1}},
                    {"mode": "approve", "record": po}, {"mode": "links"},
                    {"mode": "links", "record": po, "as": "someone"}, ["links"]):
            with self.assertRaises(ValueError, msg=bad):
                human_links.request(call, bad)
        self.assertEqual(len(self.store.link_history("plx")), 1)

    def test_a_confirmed_supplier_number_names_the_purchase_order_until_undone(self) -> None:
        from ontology.mentions import reference_alias_outcome
        from operating import human_links

        def found(call):
            return {c["object"]["object_id"]
                    for c in call("plx.find", {"query": PI_NUMBER})["candidates"]}

        def outcome():
            return reference_alias_outcome(self.report["post_project"]["mentions"]["counts"])

        call = self.tools(self.reviewer)
        po, sc = self.ids(call)
        self.assertEqual(found(call), {sc})
        self.assertNotEqual(outcome(), "alias_joined")
        prepared = human_links.request(call, {"mode": "prepare_alias", "record": po,
                                              "document": sc})
        self.assertEqual(prepared["status"], "ready", prepared)
        self.assertEqual((prepared["review"]["decision"], prepared["review"]["subject"],
                          prepared["review"]["alias"]),
                         ("alias", po, {"identifier_kind": "vendor_confirmation",
                                        "value": PI_NUMBER}))
        # A number the document does not state is refused, never recorded.
        self.assertEqual(human_links.request(call, {
            "mode": "prepare_alias", "record": po, "document": sc,
            "number": "PI-HL-0002"})["code"], "number_not_in_document")
        confirmed = human_links.request(call, {"mode": "confirm",
                                               "review": prepared["review"]})
        self.assertEqual(confirmed["status"], "confirmed", confirmed)
        self.assertEqual(found(call), {sc, po}, "plx.find reads the alias at once")
        self.path = self.build()
        self.assertEqual(outcome(), "alias_joined", self.report["post_project"]["mentions"])

        self.assertEqual(human_links.request(call, {
            "mode": "undo", "record": po,
            "review_id": confirmed["review"]["review_id"]})["status"], "undone")
        self.assertEqual(found(call), {sc})
        self.path = self.build()
        self.assertNotEqual(outcome(), "alias_joined")


@unittest.skipUnless(HAVE_PDF, NEEDS)
class HumanLinkLiveCheckTests(unittest.TestCase):
    """The live check's own command over injected transports, replayed through the live
    verifier: blocked naming the confirm step until a person confirms the link in the
    console, then the next run reads the case back answered."""

    def setUp(self) -> None:
        from verify.tests.test_live_verify import FakeRoot

        self.spec = json.loads((ROOT / "verify" / "live-checks" / "case.human_link.json")
                               .read_text("utf-8"))
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = mock.patch(target, _refuse_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.verifier = FakeRoot()
        self.addCleanup(self.verifier.close)
        # The packaging case's documents, the proforma invoice naming no purchase order.
        lines = {**PIS, **QUOTES, "pi-a.pdf": [
            line for line in PIS["pi-a.pdf"] if not line.startswith("Customer PO")]}
        lines.pop("pi-b.pdf")
        self.members = {name: _pdf(text) for name, text in lines.items()}
        # One container, byte for byte, across the runs and the console (a zip's entry
        # times would otherwise differ by the second): the file a person confirmed from is
        # the file the next capture reads.
        from ontology.tests.test_container_members import zipped
        self.container = zipped(sorted(self.members.items()))
        self.args = self.spec["command"][3:]

    def config(self) -> Path:
        examples = ROOT / "examples"
        config = json.loads((examples / "packaging_landed_cost_host.live-check.json")
                            .read_text("utf-8"))
        for key in ("capture_audience", "live_audience", "mirror_audience"):
            config[key] = str(examples / config[key])
        config["sharepoint"]["scope"] = str(examples / config["sharepoint"]["scope"])
        path = self.temp / "host.json"
        path.write_text(json.dumps(config), "utf-8")
        return path

    def transports(self):
        from integrations.tests import test_capture_sharepoint as tsp
        from integrations.tests.test_source_mirror import FakeWorkbench

        drive = tsp.FakeDrive()
        drive.folder("01USECASES", "Ontology UseCases", drive=tsp.SITE_DRIVE)
        drive.file("01PACKET", "packaging-case.zip", "01USECASES",
                   self.container, mime="application/zip",
                   drive=tsp.SITE_DRIVE)
        fake = FakeWorkbench()
        fake.catalog = copy.deepcopy(FIXTURE["catalog"])
        fake.tables = copy.deepcopy(FIXTURE["tables"])
        for line in fake.tables["public.app_purchase_order_line"]:
            line["unit_price"] = "0.000"
        return drive, fake

    def run_check(self, run: str, reviews_from: Path | None) -> tuple[int, dict, dict]:
        from integrations.tests import test_capture_sharepoint as tsp
        from integrations.tests.test_source_mirror import BASE, KEY
        from verify.tests.test_live_verify import FAKE_MODULE, SECRET
        from scripts import eval_human_link_check as link_check

        original = self.container
        registry = {"schema": "plx-case-originals/v1",
                    "rows": [_row(ZIP_PIN, "original", original, None)]
                    + [_row(PINS[name], "archive_member", data, ZIP_PIN)
                       for name, data in sorted(self.members.items())]}
        (self.temp / "registry.json").write_text(json.dumps(registry), "utf-8")
        host = self.config()
        argv = [str(host) if arg.endswith("_host.live-check.json")
                else str(self.temp / "registry.json") if arg == "examples/case_originals.json"
                else arg.replace("{tmp}", str(self.temp / run)) for arg in self.args]
        drive, fake = self.transports()
        environ = {**tsp.ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY}
        if reviews_from is not None:
            environ["PLX_WORKBENCH_STATE_DIR"] = str(reviews_from)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = link_check.main(argv, environ=environ, host_options={
                "sender": drive, "workbench_fetch": fake, "sleep": lambda _s: None})
        document = json.loads(out.getvalue())
        self.assertNotIn(KEY, out.getvalue())
        self.assertFalse([u for m, u in drive.calls if m != "GET"])
        check = self.verifier.run({**self.spec, "command": ["python3", "-m", FAKE_MODULE,
                                                            "echo"]},
                                  environ={**self.verifier.env, **tsp.ENV,
                                           "PLX_WORKBENCH_API_URL": "https://portal.invalid",
                                           "PLX_WORKBENCH_API_KEY": SECRET,
                                           "FAKE_DOC": json.dumps(document),
                                           "FAKE_EXIT": str(code)})
        return code, document, check

    def confirm_in_console(self, state: Path, console: Path, principals: set[str]) -> dict:
        """A person confirms the proforma invoice's purchase order in the console's object
        workspace over the first run's projection, into the console's review store. The
        person reads the proforma invoice's money (the host grants them the finance
        marking), so only the review grant decides; "PI-SYN-A" also matches two
        suppliers' display text, a lead the confirm step never takes for the document."""
        from ontology import reviews
        from ontology.core import Actor
        from operating import case_workspace_live as live_ws
        from operating.host import HostConfig, projection_path
        from operating.session_identity import CHANNEL_CONSOLE, fixture_session
        from operator_ui.object_workspace import ObjectWorkspace, host_access

        config = HostConfig.from_file(self.temp / "host.json")
        session = fixture_session(config.tenant, principals, channel=CHANNEL_CONSOLE,
                                  clearance="confidential")
        actor = session.actor
        self.assertIsInstance(actor, Actor)
        console.mkdir(exist_ok=True)
        # The console host, as operating.host serves its sessions: the captured units
        # granted per reader from the host's live audience (GET only, injected transport).
        from integrations.tests import test_capture_sharepoint as tsp
        from operating.case_runtime import LIVE_READER_SOURCES, build_live_readers
        from operating.ingestion import load_live_audience

        drive, _ = self.transports()
        audience = load_live_audience(config.live_audience, tsp.ENV)
        live = build_live_readers(audience, tsp.ENV, graph_sender=drive, sources=sorted(
            set(config.sources) & set(LIVE_READER_SOURCES)))
        runtime = live_ws.offline_runtime(state, config.mirror_audience, config.tenant,
                                          live=live)
        self.addCleanup(runtime.close)
        access = host_access(runtime, projection_path(state), actor,
                             sharepoint_capture_dir=state, session=session,
                             markings=("finance",),
                             reviews=reviews.ReviewStore(reviews.store_path(console)))
        self.addCleanup(access.close)
        workspace = ObjectWorkspace(access.tools(), actor, access=access)
        po = workspace._call("plx.find", {"query": "PO5447"})["candidates"][0]["object"][
            "object_id"]
        prepared = workspace.link_confirm({"mode": "prepare_link", "record": po,
                                           "document": "PI-SYN-A"})["link_confirm"]
        self.assertEqual(prepared["status"], "ready", prepared)
        return workspace.link_confirm({"mode": "confirm",
                                       "review": prepared["review"]})["link_confirm"]

    def test_blocked_on_the_named_confirm_step_until_a_person_confirms_the_link(self) -> None:
        code, document, check = self.run_check("first", None)
        self.assertEqual((code, document["reviews_source"]), (0, "none"), document)
        [gap] = document["human_link"]["gaps"]
        self.assertEqual((gap["code"], gap["pin"], gap["state"]),
                         ("pi_not_linked_to_po", PROFORMA, "confirm_needed"))
        self.assertEqual(gap["confirm_step"]["decision"], "accept_link")
        self.assertEqual(gap["confirm_step"]["link_type"], "SupplierConfirmation.confirms")
        self.assertEqual(gap["confirm_step"]["grant"], "review:identity")
        self.assertEqual(document["blocked"], "confirm_link:SupplierConfirmation.confirms")
        self.assertEqual((check["result"], check["detail"]["cause"]), ("blocked", "later_ask"),
                         check)
        self.assertIn("confirm_link:SupplierConfirmation.confirms", check["detail"]["reason"])

        console = self.temp / "console"
        state = self.temp / "first" / "state"
        refused = self.confirm_in_console(state, self.temp / "nogrant", {"staff:operations"})
        self.assertEqual(refused["code"], "identity_review_required", refused)
        confirmed = self.confirm_in_console(state, console,
                                            {"staff:operations", "review:identity"})
        self.assertEqual(confirmed["status"], "confirmed", confirmed)

        # The next run copies the console's review store (read only) and reads the case
        # back as the check's reader: answered, nothing left to confirm.
        code, document, check = self.run_check("second", console)
        self.assertEqual((code, document["reviews_source"]), (0, "copied"), document)
        self.assertEqual(document["landed_cost"]["oracle_check"]["status"], "agree", document)
        [gap] = document["human_link"]["gaps"]
        self.assertEqual(gap["state"], "answered", gap)
        self.assertEqual(document["human_link"]["projected"]["links"], 1, document)
        self.assertIsNone(document["blocked"])
        self.assertEqual(check["result"], "pass", check)
        # The console's store is only read: its one decision is still there, unchanged.
        from ontology import reviews
        self.assertEqual([r["review_id"] for r in reviews.ReviewStore(
            reviews.store_path(console)).link_history("plx")],
            [confirmed["review"]["review_id"]])


class LinkConfirmHttpTests(unittest.TestCase):
    """POST /api/link-confirm on an in-process console on 127.0.0.1 (the seeded fixture
    mirror; ``tests.test_object_workspace_http.PersonControlsTest``'s console, signed test
    tickets): the CSRF token is required, a request outside the modes is a 400, a person
    without the grant reads a 409 in plain words with nothing recorded, and the granted
    employee's confirmation is listed with who and when until undone. Evidence:
    isolated-fixture."""

    @classmethod
    def setUpClass(cls) -> None:
        import shutil

        from operating.tests import test_session_identity as ids
        from operator_ui import object_workspace as objects
        from operator_ui.tests import case_fixtures
        from tests import test_object_workspace_http as http

        cls.http, cls.ids = http, ids
        cls.tmp = Path(tempfile.mkdtemp(prefix="link-confirm-http-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        from operator_ui import case_console
        cls.store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, cls.store)
        cls.policy, cls.portal = ids.identity_files(cls.tmp / "identity", {
            case_fixtures.OPERATIONS: ("VIEW_OPERATIONS", "operations.sales", "ops"),
            "review:identity": ("VIEW_OPERATIONS", "operations.sales", "ops")})

    def console(self, **kwargs):
        return self.http.PersonControlsTest.console(self, **kwargs)

    def open_po(self, call):
        return self.http.PersonControlsTest.open_po(self, call)

    def decision(self, po, view):
        edge = next(e for hop in view["around"]["hops"] for e in hop["result"]["edges"]
                    if e["from"] == po)
        shown = view["card"]["values"][0]["citation"]
        return {"decision": "accept_link", "subject": edge["from"], "target": edge["to"],
                "link_type": edge["link_type"],
                "evidence": [{key: shown[key] for key in ("l1_handle", "digest",
                                                          "field_path")}]}

    def test_the_route_refuses_in_plain_words_and_the_granted_person_confirms_and_undoes(
            self) -> None:
        from operating import human_links

        staff = self.console(live=False)   # fixture operations staff: no review:identity
        po, view = self.open_po(staff)
        review = self.decision(po, view)
        route = "/api/link-confirm"
        self.assertEqual(staff("POST", route, {"mode": "links", "record": po},
                               csrf=False)[0], 403)
        for bad in ({"mode": "auto_link", "record": po},
                    {"mode": "confirm", "review": dict(review, author="someone-else")},
                    {"mode": "links", "record": po, "extra": 1}):
            self.assertEqual(staff("POST", route, bad)[0], 400, bad)
        status, refused = staff("POST", route, {"mode": "confirm", "review": review})
        self.assertEqual(status, 409, refused)
        self.assertIn(refused["error"], human_links.PLAIN)
        self.assertEqual(refused["detail"], human_links.PLAIN[refused["error"]])
        self.assertNotIn("_", refused["detail"].replace("review:identity", ""))
        status, listed = staff("POST", route, {"mode": "links", "record": po})
        self.assertEqual((status, listed["link_confirm"]["links"]), (200, []), listed)

        person = self.console(live=True)   # the signed-in employee holding the grant
        status, confirmed = person("POST", route, {"mode": "confirm", "review": review})
        self.assertEqual(status, 200, confirmed)
        answer = confirmed["link_confirm"]
        self.assertEqual((answer["status"], answer["review"]["author"]["principal"],
                          answer["business_system_writes"]),
                         ("confirmed", "employee:" + self.ids.OID_A, 0))
        self.assertIn(answer["review"]["recorded_at"], answer["says"])
        [link] = person("POST", route, {"mode": "links", "record": po})[1][
            "link_confirm"]["links"]
        self.assertEqual((link["review_id"], link["link_type"], link["author"]["principal"]),
                         (answer["review"]["review_id"], review["link_type"],
                          "employee:" + self.ids.OID_A))
        # Undo: only a link of this record, and as guarded as the decision.
        status, missing = person("POST", route, {"mode": "undo", "record": po,
                                                 "review_id": "rev-not-there"})
        self.assertEqual((status, missing["error"]), (409, "review_not_found"))
        self.assertEqual(staff("POST", route, {"mode": "undo", "record": po,
                                               "review_id": link["review_id"]})[0], 409)
        status, undone = person("POST", route, {"mode": "undo", "record": po,
                                                "review_id": link["review_id"]})
        self.assertEqual((status, undone["link_confirm"]["status"]), (200, "undone"), undone)
        self.assertEqual(person("POST", route, {"mode": "links", "record": po})[1][
            "link_confirm"]["links"], [])


if __name__ == "__main__":
    unittest.main()
