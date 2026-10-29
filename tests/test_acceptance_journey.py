"""Acceptance journey: an employee's unfamiliar purchase question through the real host.

Owner 1 and (3), 01-VISION 3.5. One persistent host state
directory, served the way an employee reaches it: ``python3 -m operating.host run`` capture
cycles and ``python3 -m operating.host mcp`` sessions (the authenticated employee from a
signed start-up ticket, the Entra directory provider actually constructed and consulted,
the durable ticket replay guard actually used), calling the generic tools. The console's
review control records the one human correction.

Sources: Business Central, a purchasing mailbox and a SharePoint library over injected
transports (the capture tests' in-process fakes behind the real GET-only clients); the
Portal reporting API answers HTTP 500 to every call (the live shape since 2026-10-03), so
it is an explicit incomplete source. The purchase is synthetic and generic: a BC order of
100 EA, 96 received, 100 invoiced; the supplier's order confirmation arrives by mail and
the packing slip sits in SharePoint.

Steps (one test each, each over its own state directory; each MCP session is a new
host process):
(0) a start-up ticket opens one session only: the replay guard is durable;
(a) BC captured and projected without the Portal, whose gap is explicit;
(b) find and investigate the BC-rooted three-way match, every value cited ``bc://``;
(c) typed supplier-confirmation / packing-slip fields linked to the order, resolvable;
(d) a person's supplier-number alias survives a host restart and links later mail;
(e) a restricted reader and a revoked reader get no hidden evidence, and nothing hidden
    becomes a confident absence or a zero;
(f) a follow-up through ``plx.act`` stops at its per-call human approval;
(g) after a restart a co-worker resumes the case: both hash chains verify, the human
    correction and the parked approval are there, and a narrower reader of the same case
    (quality, no BC grant) sees withheld markers, never the records; a case finding cites
    the captured packing slip and supplier confirmation, each read back only by the
    readers the capture serves it to.

Each step starts from the first capture cycle over a fresh state directory (``setUp``)
and builds what it needs of the earlier steps itself: (e) records the alias of (d) first,
(f) and (g) park their own case. So the steps pass alone and in any order. The host config
sets ``action_path``, so ``host mcp`` serves ``plx.act`` (register and read only).

Gaps current ``main`` cannot pass are ``expectedFailure`` tests named for the gap (never papered over).

Evidence label: ``injected-transport``. No network, no credentials.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from integrations.bc_read import CAPTURE_ENTITY_SETS
from integrations.people_exclusion import PeopleExclusion
from integrations.tests import test_bc_capture as tb
from integrations.tests import test_capture_mail as mail_t
from integrations.tests import test_capture_sharepoint as sp_t
from integrations.tests.test_live_source_adapters import ENV as SOURCE_ENV
from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.tests.test_source_mirror_persisted import Outage
from ontology.tests.test_document_fields import pdf
from ontology.tests.test_project_bc import gid, record
from operating import host as host_module
from operating import session_identity as si
from operating.case_mail_scope import MailScope
from operating.tests.test_host import HTTP_500, INIT, Lines, Router, call
from operating.cases import WITHHELD
from operating.tests.test_session_identity import (OID_A, OID_B, RULES, SECRET, Fixture,
                                                   identity_files, user_row)

SVC = "svc:workbench-host"
OPS = "staff:operations"
ENV = {**SOURCE_ENV, "SHAREPOINT_SITE_ID": sp_t.SITE_ID, "PLX_WORKBENCH_API_KEY": KEY}
MAN = tb.CO[0]
PO_NUMBER, VENDOR, ITEM, SUPPLIER_NUMBER = "PO-8840", "V-311", "ITEM-K7", "PI-77310"
PO_ID = f'PurchaseOrder:bc.purchase_orders:{{"id":"{gid(1, 1)}"}}'
# Employee A buys from the purchasing mailbox the host captures (its owner); B is in quality.
# A also works with quality. C is A's purchasing co-worker and may open the packing slip;
# D manages quality: no purchasing (BC) grant.
EMAIL_A, EMAIL_B = mail_t.UPN_A, "quality@example-plx.com"
OID_C, OID_D = "aaaaaaaa-0000-4000-8000-00000000000c", "aaaaaaaa-0000-4000-8000-00000000000d"
EMAIL_C, EMAIL_D = "buyer@example-plx.com", "planner@example-plx.com"
CONFIRMATION = [["ORDER CONFIRMATION", f"Confirmation No: {SUPPLIER_NUMBER}",
                 "Date: 2026-09-03", f"Your PO: {PO_NUMBER}", "Quantity: 100 pcs",
                 "Unit Price: USD 12.00/pc", "Total Amount: USD 1,200.00"]]
SLIP = [["PACKING LIST", "Packing List No: PL-3390", "Ship date: 2026-09-15",
         f"PO: {PO_NUMBER}", "Quantity: 96 pcs", "Cartons: 4", "Gross weight: 48.5 kg"]]
SHORT = "Why 4 of the 100 ordered units were not received."
SHORT_WHY = "BC: ordered 100 EA, received 96 EA, invoiced 100 EA."
QUESTION = "Why was this purchase short-received but fully invoiced?"
BC_FINDING = "Business Central records the receipt short of the ordered quantity."


def content(replies: dict, request_id) -> dict:
    return replies[request_id].get("structuredContent") or {}


class ChainedLines(Lines):
    """MCP stdin whose messages may be ``f(replies so far)``, so a call can use an earlier
    reply of the same session (a case id, a set handle)."""

    def __init__(self, messages, hooks, output) -> None:
        super().__init__([], hooks)
        self.messages, self.output = list(messages), output

    def readline(self, _limit=-1) -> bytes:
        if self.index >= len(self.messages):
            return b""
        hook = self.hooks.get(self.index)
        if hook is not None:
            hook()
        message = self.messages[self.index]
        self.index += 1
        if callable(message):
            message = message(replies(self.output))
        return json.dumps(message).encode() + b"\n"


def replies(output: io.BytesIO) -> dict:
    """JSON-RPC id -> result (or error) of everything a session has answered."""
    found = [json.loads(line) for line in output.getvalue().splitlines()]
    return {r["id"]: r.get("result", r.get("error")) for r in found if "id" in r}


def purchase_world() -> tb.FakeBc:
    """One BC purchase in the manufacturing company: PO -> short receipt -> full invoice
    (BC API v2.0 field names)."""
    fake = tb.FakeBc({name: 0 for name in CAPTURE_ENTITY_SETS}, companies=tb.CO)
    po, item, receipt, invoice, vendor = (gid(1, k) for k in (1, 2, 3, 4, 5))
    line = {"lineType": "Item", "lineObjectNumber": ITEM, "unitOfMeasureCode": "EA",
            "sequence": 10000}
    fake.data[(MAN, "purchaseOrders")] = [record(
        po, number=PO_NUMBER, vendorNumber=VENDOR, status="Open", orderDate="2026-09-01",
        requestedReceiptDate="2026-09-20", currencyCode="USD",
        totalAmountIncludingTax="#1200.00", fullyReceived=False)]
    fake.lines[(MAN, "purchaseOrders", po)] = [record(
        gid(1, 6), documentId=po, quantity="#100", directUnitCost="#12.00",
        receivedQuantity="#96", invoicedQuantity="#100", **line)]
    fake.data[(MAN, "items")] = [record(item, number=ITEM, displayName="Pump cap",
                                        baseUnitOfMeasureCode="EA", type="Inventory")]
    fake.data[(MAN, "vendors")] = [record(vendor, number=VENDOR, displayName="Vendor",
                                          currencyCode="USD")]
    fake.data[(MAN, "purchaseReceipts")] = [record(
        receipt, number="PR-5501", orderNumber=PO_NUMBER, vendorNumber=VENDOR,
        postingDate="2026-09-18")]
    fake.lines[(MAN, "purchaseReceipts", receipt)] = [record(
        gid(1, 7), documentId=receipt, quantity="#96", unitCost="#12.00", **line)]
    fake.data[(MAN, "purchaseInvoices")] = [record(
        invoice, number="PI-5502", orderId=po, orderNumber=PO_NUMBER, vendorId=vendor,
        vendorNumber=VENDOR, vendorInvoiceNumber="VI-77", invoiceDate="2026-09-21",
        postingDate="2026-09-21", dueDate="2026-10-21", status="Open",
        totalAmountExcludingTax="#1200.00", totalTaxAmount="#0",
        totalAmountIncludingTax="#1200.00")]
    fake.lines[(MAN, "purchaseInvoices", invoice)] = [record(
        gid(1, 8), documentId=invoice, quantity="#100", unitCost="#12.00",
        amountExcludingTax="#1200.00", **line)]
    return fake


class Directory:
    """Entra directory (GET users/{oid} and its transitive groups) for two employees."""

    def __init__(self, users) -> None:
        self.users = dict(users)
        self.reads = 0

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return mail_t.j(200, {"access_token": "eyJ.t.sig", "expires_in": 3599})
        assert method == "GET", method
        path = unquote(parts.path).lower()
        for oid, email in self.users.items():
            if path == f"/v1.0/users/{oid}":
                self.reads += 1
                return mail_t.j(200, {"id": oid, "accountEnabled": True, "mail": email,
                                      "userPrincipalName": email})
            if path == f"/v1.0/users/{oid}/transitivememberof/microsoft.graph.group":
                return mail_t.j(200, {"value": []})
        return mail_t.j(404, {"error": {"code": "ResourceNotFound"}})


def handles(value) -> list[str]:
    """Every capture handle an answer cites (``l1_handle`` and envelope origins)."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "l1_handle" or (key == "citation" and isinstance(item, str)):
                found.append(item)
            else:
                found += handles(item)
    elif isinstance(value, list):
        for item in value:
            found += handles(item)
    return found


def absences(value) -> list[dict]:
    """Every gap (anywhere in an answer) that claims absence from the source."""
    found: list[dict] = []
    if isinstance(value, dict):
        if value.get("state") == "absent_in_source":
            found.append(value)
        for item in value.values():
            found += absences(item)
    elif isinstance(value, list):
        for item in value:
            found += absences(item)
    return found


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "document fields are read from PDFs: needs pypdf and reportlab")
class AcceptanceJourney(unittest.TestCase):
    def setUp(self) -> None:
        """The first capture cycle over a fresh state directory and world."""
        for patcher in (patch("socket.socket.connect", side_effect=AssertionError("network")),
                        patch("socket.create_connection",
                              side_effect=AssertionError("network")),
                        # synthetic mailboxes (the test opt-out of test_capture_mail)
                        patch.object(MailScope, "builtin", return_value=mail_t.SYNTHETIC)):
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.clock = Clock()
        self.router = Router()
        self.router.bc = purchase_world()
        self.router.mail.put(
            mail_t.BOX_A, "m1", "ck1", subject=f"Order confirmation {SUPPLIER_NUMBER} for "
            f"{PO_NUMBER}", body="Our confirmation is attached.",
            received=(self.clock.now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            attachments=[mail_t.file_attachment("att1", pdf(CONFIRMATION), "confirmation.pdf")])
        self.router.drive.file("01SLIP", "packing list.pdf", "01AOS", pdf(SLIP),
                               acl=(EMAIL_A, EMAIL_C))
        self.portal = Outage(FakeWorkbench())
        self.portal.down_all = HTTP_500
        self.directory = Directory({OID_A: EMAIL_A, OID_B: EMAIL_B, OID_C: EMAIL_C,
                                    OID_D: EMAIL_D})
        self.config = self.write_config()
        self.first_cycle = self.cycle()

    def write(self, name: str, value) -> Path:
        path = self.root / "config" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), "utf-8")
        return path

    def write_config(self) -> Path:
        self.write("live-audience.json", {
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "default_classification": "internal",
            "audiences": {f"mail:{mail_t.UPN_A}": [SVC, OPS], "sharepoint:*": [SVC, OPS],
                          "bc:production:*": [SVC, OPS]},
            "classifications": {"mail:*": "confidential"}})
        self.write("mirror-audience.json", {"schema": "plx-source-mirror-audience/v1",
                                           "tenant": "plx", "audiences": {"public.*": [SVC, OPS]}})
        self.write("mail-scope.json", {
            "schema": "plx-mail-capture-scope/v1", "window_days": 90,
            "internal_domains": ["example-plx.com"],
            "mailboxes": [{"address": mail_t.UPN_A, "folders": ["inbox"]}]})
        self.write("sharepoint-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "targets": [{"id": "quality", "drive_id": sp_t.DRIVE,
                         "folders": [sp_t.QUALITY_FOLDER], "max_depth": 3, "max_folders": 50}]})
        self.write("books.json", {"schema": "plx-bc-company-books/v1", "environments": tb.BOOKS})
        # A: operations (purchasing); B: quality only. The Portal snapshot is re-read per call.
        self.users = [user_row(OID_A, EMAIL_A, "MANAGER", departments=("ops", "qa")),
                      user_row(OID_B, EMAIL_B, "STAFF", departments=("qa",)),
                      user_row(OID_C, EMAIL_C, "MANAGER", departments=("ops",)),
                      user_row(OID_D, EMAIL_D, "MANAGER", departments=("qa",))]
        # Purchasing (ops) also grants identity review: A records the supplier's number.
        rules = dict(RULES, **{"review:identity": RULES["staff:operations"]})
        self.policy, self.portal_access = identity_files(self.root / "config" / "identity",
                                                         rules, users=self.users)
        return self.write("host.json", {
            "schema": host_module.CONFIG_SCHEMA, "tenant": "plx", "capture_principal": SVC,
            "capture_audience": "live-audience.json", "live_audience": "live-audience.json",
            "mirror_audience": "mirror-audience.json", "entra_directory": True,
            "action_path": True,
            "employee_policy": str(self.policy), "portal_access": str(self.portal_access),
            "portal": {"base_url": BASE, "relations": ["public.app_purchase_order"]},
            "bc": {"books": "books.json"}, "mail": {"scope": "mail-scope.json"},
            "sharepoint": {"scope": "sharepoint-scope.json"}})

    def host(self, *argv, sender=None, **kwargs) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = host_module.main(
                    list(argv), environ=ENV, sender=sender or self.router,
                    workbench_fetch=self.portal, clock=self.clock, sleep=lambda s: None,
                    portal_options={"clock": self.clock, "retry_delay_seconds": 0}, **kwargs)
        except SystemExit as exit_:
            code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def cycle(self) -> dict:
        code, out, err = self.host("run", "--state-dir", str(self.state), "--config",
                                   str(self.config))
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def start(self, ticket: str, messages, hooks=None) -> tuple[int, str, dict]:
        """One ``host mcp`` process started with ``ticket``: exit code, stderr, replies."""
        output = io.BytesIO()
        # ``mcp_server`` reads the people exclusion from its process environment.
        excluded = "PLX_EXCLUDED_PEOPLE_OIDS"
        with patch.dict(os.environ, {"T": ticket, si.SESSION_TICKET_SECRET_ENV: SECRET,
                                     excluded: ENV[excluded]}):
            code, _out, err = self.host(
                "mcp", "--state-dir", str(self.state), "--config", str(self.config),
                "--session-ticket-env", "T", sender=self.directory,
                input_stream=ChainedLines(INIT + messages, hooks, output),
                output_stream=output)
        return code, err, replies(output)

    def session(self, messages, *, oid=OID_A, email=EMAIL_A, hooks=None) -> dict:
        """One ``host mcp`` process for one employee (a fresh agent start-up ticket):
        JSON-RPC id -> result."""
        code, err, answered = self.start(
            Fixture.ticket(oid=oid, email=email, author=si.AUTHOR_AGENT), messages, hooks)
        self.assertEqual(code, 0, err)
        return answered

    def ok(self, result) -> dict:
        self.assertFalse(result.get("isError"), json.dumps(result)[:1500])
        out = result["structuredContent"]
        self.assertNotIn("error", out, json.dumps(out)[:1500])
        return out

    @staticmethod
    def around(request_id, root, link_type, direction="reverse", **extra):
        return call(request_id, "plx.around", {"from": root, "path": [
            {"link_type": link_type, "direction": direction}], **extra})

    @staticmethod
    def values(answer) -> dict[str, dict]:
        return {v["property"]: v for v in answer["values"]}

    # ------------------------------------------------------------------ (0)

    def test_0_a_start_up_ticket_opens_one_session_only(self):
        ticket = Fixture.ticket(oid=OID_A, email=EMAIL_A, author=si.AUTHOR_AGENT)
        code, err, first = self.start(ticket, [call(2, "plx.find", {"query": PO_NUMBER})])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ok(first[2])["status"], "found")
        # The same ticket again, to a new host process over the same state directory: the
        # durable guard there refuses it before any session or tool exists.
        reads = self.directory.reads
        code, err, second = self.start(ticket, [call(2, "plx.find", {"query": PO_NUMBER})])
        self.assertEqual((code, second), (2, {}))
        self.assertIn("session identity refused: session_ticket_replayed", err)
        self.assertEqual(self.directory.reads, reads)
        self.assertTrue((self.state / si.REPLAY_GUARD_FILE).is_file())

    # ------------------------------------------------------------------ (a)

    def test_1_bc_is_projected_without_the_portal_and_the_portal_gap_is_explicit(self):
        report = self.first_cycle
        self.assertEqual({name: s["state"] for name, s in report["sources"].items()},
                         {"portal": "failed", "bc": "captured", "mail": "captured",
                          "sharepoint": "captured", "teams": "not_configured"}, report)
        self.assertTrue(report["sources"]["portal"]["reasons"])
        self.assertTrue(report["sources"]["bc"]["complete"])
        self.assertEqual((report["projection"]["state"], report["projection"]["sources"]),
                         ("built", ["bc", "graph_mail", "sharepoint"]))
        code, out, err = self.host("status", "--state-dir", str(self.state))
        self.assertEqual(code, 0, err)
        portal = next(s for s in json.loads(out)["sources"] if s["source"] == "portal")
        self.assertEqual((portal["generation"], portal["served_from"]), (0, "none"))
        self.assertTrue(portal["last_error"])
        # GET-only: the only other method any source saw is the OAuth token POST.
        for method, url in self.router.calls:
            self.assertEqual(method, "POST" if urlsplit(url).hostname
                             == "login.microsoftonline.com" else "GET", url)

        reads = self.directory.reads
        replies = self.session([call(2, "plx.find", {"query": PO_NUMBER}),
                                call(3, "plx.find", {"query": "SO-4410"})])
        self.assertGreater(self.directory.reads, reads)   # the employee was resolved
        found, sales_order = self.ok(replies[2]), self.ok(replies[3])
        self.assertEqual(found["status"], "found", json.dumps(found)[:1500])
        (candidate,) = found["candidates"]
        self.assertEqual(candidate["object"]["object_id"], PO_ID)
        self.assertTrue(candidate["matched"]["raw"]["origin"]["citation"].startswith("bc://"))
        # A question only the Portal could answer names it unread, never empty: the host
        # configures the Portal, so its gap is the failed capture, never "not configured".
        self.assertNotEqual(sales_order["status"], "found")
        portal_gaps = {(g["state"], g["reason"]) for g in sales_order["gaps"]
                       if g["subject"] == "portal_api"}
        self.assertIn(("inaccessible", "source_unavailable"), portal_gaps)
        self.assertNotIn("source_not_configured", {reason for _state, reason in portal_gaps})
        self.assertEqual(absences([found, sales_order]), [])

    @unittest.expectedFailure
    def test_1_gap_an_answer_names_the_portal_outage(self):
        """GAP outage-without-capture: while the Portal has never been captured the host
        projects BC, mail and SharePoint alone and the store says nothing about the Portal
        (no ``portal_api`` freshness entry), so an answer found in BC carries no word of
        the outage; with a persisted capture it is labelled ``live source unavailable``
        (``operating/tests/test_host.py``). The host ledger knows (``host status``)."""
        found = self.ok(self.session([call(2, "plx.find", {"query": PO_NUMBER})])[2])
        (portal,) = [f for f in found["meta"]["freshness"] if f["source"] == "portal_api"]
        self.assertTrue(portal["stale"])
        self.assertIn("live source unavailable", portal["label"])

    # ------------------------------------------------------------------ (b)

    def three_way(self) -> dict:
        replies = self.session([
            call(2, "plx.object", {"object_id": PO_ID}),
            self.around(3, PO_ID, "PurchaseOrderLine.line_of"),
            self.around(4, PO_ID, "Receipt.receives"),
            self.around(5, PO_ID, "PurchaseInvoice.invoices"),
            call(6, "plx.timeline", {"from": PO_ID}),
            call(7, "plx.process", {"from": PO_ID, "process": "purchase_to_invoice"}),
            call(8, "plx.investigate", {"family": "three_way_match", "root": PO_ID})])
        return {request: self.ok(replies[request]) for request in range(2, 9)}

    def test_2_an_unfamiliar_bc_purchase_is_investigated_with_bc_citations(self):
        answers = self.three_way()
        self.assertEqual(self.values(answers[2])["po_number"]["value"], "PO8840")
        for request, kind in ((3, "PurchaseOrderLine"), (4, "Receipt"),
                              (5, "PurchaseInvoice")):
            self.assertEqual([o["object_type"] for o in answers[request]["objects"]], [kind])
        posted = {e["event_type"]: e["time"] for e in answers[6]["events"]}
        self.assertEqual((posted["Receipt.posted"], posted["PurchaseInvoice.posted"]),
                         ("2026-09-18", "2026-09-21"))
        process = answers[7]
        stages = {s["stage"]: s for s in process["stages"]}
        self.assertEqual([stages[s]["state"] for s in ("purchase_order", "receipt",
                                                       "vendor_invoice")], ["present"] * 3)
        quantities = {(v["quantity_role"], v["value"], v["unit"])
                      for check in process["quantity_checks"] for v in check["values"]
                      if v["value_type"] == "quantity"}
        self.assertEqual(quantities, {("ordered", "100", "EA"), ("received", "96", "EA"),
                                      ("invoiced", "100", "EA")})
        # 100 ordered against 96 received is never reported as agreeing.
        self.assertNotIn(True, {c["agree"] for c in process["quantity_checks"]})
        # The verdict is decided for the employee: 100 ordered vs 96 received disagree.
        checks = {tuple(c["roles"]): c["agree"] for c in process["quantity_checks"]}
        self.assertIs(checks[("ordered", "received")], False)
        self.assertIs(checks[("received", "invoiced")], False)
        investigation = answers[8]
        answered = {a["name"]: a["value"] for a in investigation["answer"]}
        self.assertEqual((answered["ordered"], answered["received"]), ("100", "96"))
        self.assertNotEqual(investigation["answerability"]["state"], "complete")
        self.assertTrue(investigation["claim_checks"]["passed"], investigation["claim_checks"])
        cited = handles(answers)
        self.assertTrue(cited)
        self.assertEqual([h for h in cited if not h.startswith("bc://")], [])
        # A captured bc:// citation resolves on the host from the BC capture.
        handle = next(h for h in handles(process) if "/purchaseReceiptLines/" in h)
        read = self.ok(self.session([call(2, "plx.read", {"citation": handle})])[2])
        self.assertEqual(read["status"], "resolved", json.dumps(read)[:800])
        # The only absence is one BC states: the open invoice has reached no payment.
        # The G/L stage and the Portal twin are unknown, never absent.
        self.assertEqual([(a["subject"], a["reason"]) for a in absences(answers)],
                         [("vendor_payment", "status_not_reached")])

    # ------------------------------------------------------------------ (c)

    def documents(self) -> dict:
        replies = self.session([
            self.around(2, PO_ID, "SupplierConfirmation.confirms", tier_floor="lead"),
            self.around(3, PO_ID, "PackingSlip.for_purchase_order", tier_floor="lead"),
            self.around(4, PO_ID, "SupplierConfirmation.confirms")])
        answers = {request: self.ok(replies[request]) for request in (2, 3, 4)}
        (confirmation,) = answers[2]["objects"]
        (slip,) = answers[3]["objects"]
        # A machine-read document is a lead: never traversed at the default tier floor.
        self.assertEqual(answers[4]["objects"], [])
        self.assertEqual({e["tier"] for a in (answers[2], answers[3]) for e in a["edges"]},
                         {"lead"})
        replies = self.session([call(2, "plx.object", {"object_id": confirmation["object_id"]}),
                                call(3, "plx.object", {"object_id": slip["object_id"]})])
        return {"confirmation": self.ok(replies[2]), "slip": self.ok(replies[3])}

    def test_3_typed_document_fields_are_linked_to_the_order_and_resolvable(self):
        docs = self.documents()
        confirmation, slip = self.values(docs["confirmation"]), self.values(docs["slip"])
        self.assertEqual((confirmation["quantity"]["value"], confirmation["quantity"]["unit"],
                          confirmation["document_date"]["value"], confirmation["tier"]["value"]),
                         ("100", "ea", "2026-09-03", "lead"))
        self.assertEqual({name: (slip[name]["value"], slip[name].get("unit"))
                          for name in ("quantity", "packages", "ship_date")},
                         {"quantity": ("96", "ea"), "packages": ("4", "carton"),
                          "ship_date": ("2026-09-15", None)})
        # Each value cites the byte-pinned original, which plx.read resolves for the reader.
        pinned = {"graph-mail://": confirmation["quantity"]["citation"]["l1_handle"],
                  "sharepoint://": slip["quantity"]["citation"]["l1_handle"]}
        replies = self.session([call(2, "plx.read", {"citation": pinned["graph-mail://"]}),
                                call(3, "plx.read", {"citation": pinned["sharepoint://"]})])
        for request, (scheme, handle) in zip((2, 3), pinned.items()):
            self.assertTrue(handle.startswith(scheme) and "sha256=" in handle, handle)
            self.assertEqual(self.ok(replies[request])["status"], "resolved")
        self.assertIn("Quantity: 96 pcs", self.ok(replies[3])["content"]["untrusted_text"])

    @unittest.expectedFailure
    def test_3_gap_page_and_span_evidence_is_served_to_the_employee(self):
        """GAP host-policy-coverage (markings): the page/span citations of every typed field
        live in the document's ``evidence`` property, which the schema marks ``finance``
        (so are ``amount`` and ``unit_price``); the case host grants no marking, so an
        employee never sees where on the page a value was read."""
        for answer in self.documents().values():
            evidence = json.loads(self.values(answer)["evidence"]["value"]["untrusted_text"])
            citation = evidence["fields"]["quantity"]["citations"][0]
            self.assertEqual(citation["page"], 1)
            self.assertLess(citation["start"], citation["end"])

    # ------------------------------------------------------------------ (d)

    def console(self):
        """The employee's console session (a human session ticket) and its case runtime
        over the host's state directory, as ``operator_ui.case_console`` builds them."""
        from operating.case_runtime import CaseRuntime, build_live_readers
        from operating.employee_permissions import (EmployeePermissionPolicy,
                                                    EmployeePermissionResolver,
                                                    PortalAccessSnapshot)

        resolver = EmployeePermissionResolver(EmployeePermissionPolicy.from_file(self.policy),
                                              PortalAccessSnapshot(self.portal_access))
        person = si.employee_session(resolver, channel=si.CHANNEL_CONSOLE, secret=SECRET,
                                     ticket=Fixture.ticket(oid=OID_A, email=EMAIL_A))
        runtime = CaseRuntime(self.state, client=None,
                              audience=self.root / "config" / "mirror-audience.json",
                              tenant="plx", employee_permissions=resolver,
                              require_employee=True, live=build_live_readers(
                                  self.root / "config" / "live-audience.json"),
                              people_exclusion=PeopleExclusion.from_env(ENV))
        self.addCleanup(runtime.close)
        return person, runtime

    def console_review(self, review) -> dict:
        """A person's review in the console: ``plx.review`` as
        ``object_workspace.host_access`` serves it with the state directory's review store
        (the console's own entry, ``object_workspace_for``, also checks the store's
        provenance: test 4). ``review(tool, actor)`` reads what the person sees and returns the
        ``plx.review`` arguments."""
        from ontology.reviews import ReviewStore, store_path
        from operator_ui.object_workspace import host_access

        person, runtime = self.console()
        access = host_access(runtime, host_module.projection_path(self.state), person.actor,
                             mail_capture_dir=self.state, sharepoint_capture_dir=self.state,
                             session=person, reviews=ReviewStore(store_path(self.state)))
        self.addCleanup(access.close)
        tools = access.tools()
        with access.request():
            def tool(name, arguments):
                return tools[name]["handler"](arguments, person.actor)
            recorded = tool("plx.review", review(tool))
        self.assertNotIn("error", recorded, json.dumps(recorded)[:1500])
        self.assertEqual((recorded["review"]["state"], recorded["business_system_writes"]),
                         ("effective", 0))
        return recorded

    @staticmethod
    def evidence(citation) -> dict:
        return {key: citation[key] for key in ("l1_handle", "digest", "field_path")}

    def console_alias(self) -> dict:
        """The person records the supplier's number, read in the confirmation mail's
        subject, as an alias of the order."""
        def review(tool):
            mails = tool("plx.around", {"from": PO_ID, "path": [
                {"link_type": "Email.mentions_purchase_order", "direction": "reverse"}],
                "tier_floor": "lead"})
            (mail,) = mails["objects"]
            subject = self.values(tool("plx.object", {"object_id": mail["object_id"]}))
            return {"mode": "record", "decision": "alias", "subject": PO_ID,
                    "alias": {"identifier_kind": "vendor_confirmation",
                              "value": SUPPLIER_NUMBER},
                    "evidence": [self.evidence(subject["subject"]["citation"])]}
        return self.console_review(review)

    def test_4_a_confirmed_supplier_number_survives_a_restart_and_links_later_mail(self):
        find = call(2, "plx.find", {"query": SUPPLIER_NUMBER})
        before = self.ok(self.session([find])[2])
        self.assertNotIn(PO_ID, [c["object"]["object_id"] for c in before["candidates"]])
        self.console_alias()
        # The supplier then writes naming only its own number, never the PO.
        self.router.mail.put(
            mail_t.BOX_A, "m2", "ck2", subject=f"Shipping update {SUPPLIER_NUMBER}",
            body="The balance ships next week.",
            received=self.clock.now.strftime("%Y-%m-%dT%H:%M:%SZ"))

        # Restart: a new capture cycle and a new host process over the same state.
        cycle = self.cycle()
        self.assertEqual(cycle["projection"]["state"], "built")
        self.assertGreaterEqual(cycle["projection"]["steps"]["mentions"]["reviewed_alias_links"],
                                1, cycle["projection"]["steps"])
        mails = self.ok(self.session([self.around(
            2, PO_ID, "Email.mentions_purchase_order", tier_floor="lead")])[2])
        later = [edge for edge in mails["edges"] if any(
            c["l1_handle"].startswith(f"graph-mail://{mail_t.BOX_A}/m2@")
            for reason in edge["reason"] for c in reason.get("citations", ()))]
        self.assertEqual([(e["to"], e["link_type"]) for e in later],
                         [(PO_ID, "Email.mentions_purchase_order")], mails["edges"])
        after = self.ok(self.session([find])[2])
        # The order now answers to the supplier's number, beside the confirmation that
        # states it: two records, so ranked candidates and never a single winner.
        self.assertEqual(after["status"], "ambiguous", json.dumps(after)[:1500])
        (candidate,) = [c for c in after["candidates"] if c["object"]["object_id"] == PO_ID]
        self.assertEqual((candidate["reason"], candidate["tier"]), ("alias_match", "reviewed"))
        self.assertTrue(candidate["matched"]["raw"]["origin"]["citation"]
                        .startswith("graph-mail://"))

    def test_4_the_console_opens_the_host_projection_during_a_portal_outage(self):
        """Closed GAP console-store-status: while the Portal has
        never been captured the host projects BC, mail and SharePoint alone; the employee
        console (``host serve``) opens that projection, its BC capture generation the
        state's own, with the Portal labelled not projected, never refused as foreign."""
        from operator_ui.case_console import object_workspace_for

        person, runtime = self.console()
        workspace = object_workspace_for(
            runtime, host_module.projection_path(self.state), person.actor,
            state_dir=self.state, mail_capture_dir=self.state,
            sharepoint_capture_dir=self.state, session=person)
        try:
            status = workspace.store_status.read()
        finally:
            workspace.close()
        self.assertEqual(status["state"], "current", status)
        self.assertIn("bc", status["projected"])
        self.assertNotIn("portal_api", status["projected"])
        self.assertIn("portal_api", status["not_projected"])

    # ------------------------------------------------------------------ (e)

    def test_5_restricted_and_revoked_readers_get_no_hidden_evidence_and_no_absence(self):
        self.console_alias()   # (d): a person confirmed the supplier's number
        self.cycle()
        questions = [call(2, "plx.find", {"query": PO_NUMBER}),
                     call(3, "plx.find", {"object_type": "PurchaseOrder", "filters": [
                         {"property": "po_number", "op": "eq", "value": PO_NUMBER}]}),
                     call(4, "plx.process", {"from": PO_ID, "process": "purchase_to_invoice"}),
                     call(5, "plx.investigate", {"family": "three_way_match", "root": PO_ID}),
                     self.around(6, PO_ID, "PackingSlip.for_purchase_order", tier_floor="lead"),
                     call(7, "plx.documents", {"object_id": PO_ID}),
                     # the alias confirmed above, and the packing slip's number
                     call(8, "plx.find", {"query": SUPPLIER_NUMBER}),
                     call(9, "plx.find", {"query": "PL-3390"})]
        self.assert_nothing_hidden(self.session(questions, oid=OID_B, email=EMAIL_B))

        def revoke() -> None:   # A leaves the purchasing department in the Portal
            self.portal_access.write_text(json.dumps({
                **json.loads(self.portal_access.read_text("utf-8")),
                "users": [user_row(OID_A, EMAIL_A, "MANAGER", departments=("qa",)),
                          *self.users[1:]]}), "utf-8")

        self.addCleanup(self.portal_access.write_text,
                        self.portal_access.read_text("utf-8"), "utf-8")
        # Before the revocation A holds a set of the order's lines; after it the set is
        # another reader's: it is refused, never counted (not zero, not partial).
        lines = self.around(100, PO_ID, "PurchaseOrderLine.line_of")
        revoked = self.session(
            [lines] + questions
            + [lambda r: call(101, "plx.aggregate", {"set": content(r, 100)["set"],
                                                     "measure": {"op": "count"}})],
            hooks={len(INIT) + 1: revoke})
        self.assertEqual(len(self.ok(revoked.pop(100))["objects"]), 1)
        count = content(revoked, 101)
        self.assertEqual(count["error"]["code"], "handle_expired", count)
        self.assertNotIn("groups", count)
        self.assert_nothing_hidden(revoked)

    def assert_nothing_hidden(self, replies: dict) -> None:
        # A gap names the reader's own query ("query:<text>"); that echo is not evidence.
        text = json.dumps(replies)
        for asked in (SUPPLIER_NUMBER, "PL-3390"):
            text = text.replace(f"query:{asked}", "query:<asked>")
        for hidden in ("bc://", "graph-mail://", "sharepoint://", "PO8840", "PL-3390",
                       SUPPLIER_NUMBER):
            self.assertNotIn(hidden, text)
        out = {request: content(replies, request) for request in range(2, 10)}
        self.assertEqual(absences(out), [])
        for request in (2, 8, 9):
            self.assertNotEqual(out[request].get("status"), "found", out[request])
            self.assertEqual(out[request].get("candidates"), [], out[request])
        # A filtered listing the reader cannot see whole is never "complete and none".
        self.assertEqual(out[3].get("candidates"), [])
        self.assertTrue(out[3]["withheld"]["reason"])
        self.assertIn(("unknown", "no_visible_match"),
                      {(g["state"], g["reason"]) for g in out[3]["gaps"]})
        for request in (4, 6, 7):
            self.assertEqual(out[request].get("error", {}).get("code"), "object_not_found",
                             out[request])
        self.assertEqual(out[5]["answerability"]["state"], "blocked")

    # ------------------------------------------------------------------ (f)

    def parked_case(self) -> dict:
        """A's agent session opens a case shared with quality, records a BC-cited fact and
        a BC-cited gap, proposes the follow-up (citing BC too) and registers it with
        ``plx.act``: one session, each call using the replies before it."""
        receipt_line = next(h for h in handles(self.three_way()[7])
                            if "/purchaseReceiptLines/" in h)
        case = lambda r: content(r, 2)["case_id"]   # noqa: E731
        replies = self.session([
            call(2, "plx.case_open", {"question": QUESTION,
                                      "audience": ["staff:operations", "staff:quality"]}),
            lambda r: call(3, "plx.case_add_finding", {
                "case_id": case(r), "statement": BC_FINDING, "kind": "fact",
                "status": "supported", "citations": [receipt_line]}),
            lambda r: call(4, "plx.case_record_gap", {
                "case_id": case(r), "missing": SHORT, "why": SHORT_WHY,
                "who_can_answer": ["purchasing"], "state": "unknown",
                "citations": [receipt_line]}),
            lambda r: call(5, "plx.case_propose", {
                "case_id": case(r), "capability": "mc.task.create_follow_up",
                "payload": {"gap_id": content(r, 4)["gap"]["gap_id"], "title": SHORT,
                            "description": SHORT_WHY, "priority": "high"},
                "citations": [receipt_line]}),
            lambda r: call(6, "plx.act", {
                "mode": "propose", "case_id": case(r),
                "proposal_id": content(r, 5)["proposal"]["proposal_id"]})])
        answers = {request: self.ok(replies[request]) for request in range(2, 7)}
        self.assertEqual((answers[5]["approved"], answers[5]["dispatched"]), (False, False))
        return {"case_id": answers[2]["case_id"],
                "finding_id": answers[3]["finding"]["finding_id"],
                "gap_id": answers[4]["gap"]["gap_id"],
                "proposal_id": answers[5]["proposal"]["proposal_id"],
                "intent": answers[6]["intent"], "receipt_line": receipt_line}

    def test_6_a_justified_follow_up_stops_at_its_human_approval(self):
        intent = self.parked_case()["intent"]
        self.assertEqual((intent["state"], intent["capability"], intent["call_approved"],
                          intent["dispatches"], intent["outcome"]),
                         ("proposed", "mc.task.create_follow_up", False, [], "not_dispatched"))
        # After a restart the intent is where it was: an agent can neither approve nor
        # send it; the next step is the host's, then a person's per-call approval.
        self.cycle()
        replies = self.session([call(2, "plx.act", {"mode": "status",
                                                    "intent_id": intent["intent_id"]}),
                                call(3, "plx.act", {"mode": "verify",
                                                    "intent_id": intent["intent_id"]}),
                                {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                                call(5, "plx.act", {"mode": "approve",
                                                    "intent_id": intent["intent_id"]})])
        status, verify = self.ok(replies[2]), self.ok(replies[3])
        self.assertEqual((status["intent"]["state"], status["intent"]["dispatches"],
                          status["mc_calls"], status["business_system_writes"]),
                         ("proposed", [], 0, 0))
        self.assertIs(status["approval_rule"]["per_call_human_approval"], True)
        self.assertEqual((status["intent"]["next_safe_step"]["by"],
                          status["intent"]["next_safe_step"]["agent_tool"]), ("host", None))
        self.assertEqual((verify["state"], verify["step"]), ("proposed", None))
        # No tool an agent session is offered approves, decides or dispatches, and plx.act
        # has no such mode: asking for one is refused and changes nothing.
        tools = {tool["name"]: tool for tool in replies[4]["tools"]}
        self.assertIn("plx.act", tools)
        self.assertEqual([name for name in tools if any(
            verb in name for verb in ("approv", "decid", "dispatch", "send"))], [])
        self.assertEqual(set(tools["plx.act"]["inputSchema"]["properties"]["mode"]["enum"]),
                         {"propose", "status", "verify"})
        self.assertTrue(replies[5].get("isError"), replies[5])
        self.assertIn("mode", json.dumps(replies[5]))
        again = self.ok(self.session([call(2, "plx.act", {
            "mode": "status", "intent_id": intent["intent_id"]})])[2])
        self.assertEqual((again["intent"]["state"], again["intent"]["call_approved"],
                          again["intent"]["history"]), ("proposed", False,
                                                        status["intent"]["history"]))

    # ------------------------------------------------------------------ (g)

    def test_7_a_co_worker_resumes_the_case_after_a_restart(self):
        parked = self.parked_case()
        case_id, intent_id = parked["case_id"], parked["intent"]["intent_id"]

        def confirm_slip(tool):   # the person confirms the machine-read slip's link
            (slip,) = tool("plx.around", {"from": PO_ID, "path": [
                {"link_type": "PackingSlip.for_purchase_order", "direction": "reverse"}],
                "tier_floor": "lead"})["objects"]
            quantity = self.values(tool("plx.object", {"object_id": slip["object_id"]}))
            return {"mode": "record", "decision": "accept_link", "subject": slip["object_id"],
                    "target": PO_ID, "link_type": "PackingSlip.for_purchase_order",
                    "evidence": [self.evidence(quantity["quantity"]["citation"])]}
        review_id = self.console_review(confirm_slip)["review"]["review_id"]

        # Restart: a capture cycle, then new host processes for two of A's co-workers.
        self.assertEqual(self.cycle()["projection"]["state"], "built")
        questions = [call(2, "plx.case_get", {"case_id": case_id}),
                     call(3, "plx.act", {"mode": "status", "intent_id": intent_id}),
                     self.around(4, PO_ID, "PackingSlip.for_purchase_order")]
        resumed = self.session(questions, oid=OID_C, email=EMAIL_C)
        case, status, slip = (self.ok(resumed[n]) for n in (2, 3, 4))
        # Both hash chains verify, read back by someone who did not write them.
        self.assertIs(case["integrity"]["chain_ok"], True)
        self.assertIs(status["intent"]["chain_ok"], True)
        self.assertEqual([(f["id"], f["statement"]["untrusted_text"])
                          for f in case["findings"]["supported"]],
                         [(parked["finding_id"], BC_FINDING)])
        self.assertEqual([(g["id"], g["missing"]["untrusted_text"]) for g in case["gaps"]],
                         [(parked["gap_id"], SHORT)])
        self.assertEqual([(p["id"], p["capability"]) for p in case["proposals"]],
                         [(parked["proposal_id"], "mc.task.create_follow_up")])
        # The approval is still parked: proposed, never approved or sent.
        self.assertEqual((status["intent"]["state"], status["intent"]["call_approved"],
                          status["intent"]["dispatches"], status["mc_calls"]),
                         ("proposed", False, [], 0))
        # The person's correction holds: the slip is a reviewed link of the order now,
        # traversed at the default tier floor, with the review that made it.
        (edge,) = slip["edges"]
        self.assertEqual((edge["tier"], [r["feature"] for r in edge["reason"]]),
                         ("reviewed", ["person_reviewed"]), edge)
        self.assertIn(review_id.removeprefix("rev-"), edge["link_id"])

        # D (quality) is in the case's audience but holds no BC grant: the same case, its
        # chain intact, every BC-derived item a withheld marker, nothing of BC anywhere.
        narrow = self.session(questions, oid=OID_D, email=EMAIL_D)
        case = self.ok(narrow[2])
        self.assertEqual((case["case_id"], case["integrity"]["chain_ok"]), (case_id, True))
        self.assertEqual([f["id"] for f in case["findings"]["withheld"]],
                         [parked["finding_id"]])
        self.assertEqual([item for bucket, items in case["findings"].items()
                          if bucket != "withheld" for item in items], [])
        (finding,) = case["findings"]["withheld"]
        self.assertEqual((finding["statement"], finding["content"]["untrusted_text"]),
                         (None, WITHHELD))
        for item in case["gaps"] + case["proposals"]:
            self.assertEqual(item["content"]["untrusted_text"], WITHHELD, item)
        self.assertEqual([item["id"] for item in case["gaps"] + case["proposals"]],
                         [parked["gap_id"], parked["proposal_id"]])
        # The parked intent: its state, and a withheld marker in place of what it does.
        intent = content(narrow, 3)["intent"]
        self.assertEqual((intent["state"], intent["withheld"],
                          intent["content"]["untrusted_text"], intent.get("capability")),
                         ("proposed", True, WITHHELD, None))
        self.assertEqual(content(narrow, 4)["error"]["code"], "object_not_found")
        text = json.dumps(narrow)
        # (The limitations text names the scheme; no BC citation handle appears.)
        self.assertIsNone(re.search(r"bc://[A-Za-z0-9%]", text))
        for hidden in (parked["receipt_line"], PO_ID, PO_NUMBER, "PO8840", BC_FINDING,
                       SHORT, SHORT_WHY, review_id.removeprefix("rev-")):
            self.assertNotIn(hidden, text)

    def test_7_b_a_case_cites_the_captured_slip_and_confirmation(self):
        """The packing slip (SharePoint capture) and the supplier's confirmation (a mail
        attachment in A's captured mailbox) cited in A's case resolve through the gates
        ``plx.read`` reads the captures with: C, on the slip's permissions but not the
        mailbox owner, reads only the slip finding; D reads neither."""
        docs = self.documents()
        slip, confirmation = (self.values(docs[name])["quantity"]["citation"]["l1_handle"]
                              for name in ("slip", "confirmation"))
        statements = {"slip": "The packing list ships fewer units than the order.",
                      "confirmation": "The supplier confirmed the full ordered quantity."}
        case = lambda r: content(r, 2)["case_id"]   # noqa: E731
        replies = self.session([
            call(2, "plx.case_open", {"question": QUESTION,
                                      "audience": ["staff:operations", "staff:quality"]}),
            lambda r: call(3, "plx.case_add_finding", {
                "case_id": case(r), "kind": "fact", "status": "supported",
                "statement": statements["slip"], "citations": [slip]}),
            lambda r: call(4, "plx.case_add_finding", {
                "case_id": case(r), "kind": "fact", "status": "supported",
                "statement": statements["confirmation"], "citations": [confirmation]}),
            lambda r: call(5, "plx.case_get", {"case_id": case(r)})])
        ids = {"slip": self.ok(replies[3])["finding"]["finding_id"],
               "confirmation": self.ok(replies[4])["finding"]["finding_id"]}

        def findings(answer) -> dict[str, list[str]]:
            return {bucket: sorted(f["id"] for f in items)
                    for bucket, items in answer["findings"].items() if items}

        self.assertEqual(findings(self.ok(replies[5])), {"supported": sorted(ids.values())})
        read = [call(2, "plx.case_get", {"case_id": content(replies, 2)["case_id"]})]
        coworker = self.ok(self.session(read, oid=OID_C, email=EMAIL_C)[2])
        self.assertEqual(findings(coworker), {"supported": [ids["slip"]],
                                              "withheld": [ids["confirmation"]]})
        narrow = self.ok(self.session(read, oid=OID_D, email=EMAIL_D)[2])
        self.assertEqual(findings(narrow), {"withheld": sorted(ids.values())})
        text = json.dumps(narrow) + json.dumps(coworker["findings"]["withheld"])
        for hidden in (slip, confirmation, statements["slip"], statements["confirmation"],
                       "Quantity: 96 pcs", SUPPLIER_NUMBER):
            self.assertNotIn(hidden, text)

if __name__ == "__main__":
    unittest.main()
