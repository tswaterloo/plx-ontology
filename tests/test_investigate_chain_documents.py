"""plx.investigate family ``chain`` (F1) reaches the documents that name a purchase order.

The committed live spec ``case.po_document_chain`` runs its own command over injected
sources: the in-process ``FakeWorkbench`` (synthetic Portal purchase orders, one with an FM
twin), ``FakeBc`` (the pinned companies' purchaseOrders) and ``FakeGraph`` (the purchasing
mailboxes, one message whose attachments are a supplier's proforma invoice, a packing list
and a bill of lading naming that PO, in the shared purchase-order inbox or another of them),
all behind the real GET-only clients with the network refused. The run captures, projects (document-field links: leads), picks the PO
through ``pick=PurchaseOrder+SupplierConfirmation`` (labelled as resting on a lead-tier
link) and renders its case; its expectations are checked on the real summary, and each
reader's relationship counts are the projection's ontology links by link type (the
schema's, the documents' links among them). A pick that finds nothing names its typed cause
(no confirmation held, or one naming no captured PO while a BC unit or the time-bounded
mail capture did not finish); only the principals the live audience lists read the
confirmation, whichever purchasing mailbox it came to. Over the
same projection the chain plan's answer for the PO cites the supplier confirmation, the
packing slip and the freight bill at the tier their links carry, every one labelled as
resting on a lead, and a reader without the mailbox grant sees none of them.

The chain plan's document steps follow every mention link the schema declares into the
root's and twins' types from a document-field type: over canned primitive answers, a
document type added to the schema is reached with the committed plan unedited, and a
document's mailed and SharePoint copies with the same bytes count once.

Evidence label: injected-transport (no network, synthetic rows, never live PLX data).
"""

from __future__ import annotations

import contextlib
from collections import Counter
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

from verify import live_verify as lv
from integrations.source_mirror import AudienceMap
from integrations.tests import test_bc_capture as tb
from integrations.tests import test_capture_mail as mail_t
from integrations.tests.test_live_source_adapters import ENV
from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
from ontology.core import Actor
from ontology.schema import load_schema
from ontology.tests.test_project_bc import CAPTURE_ENTITY_SETS
from operating import case_workspace_live as live_ws
from operating.tests import order_fixture as fixture

ROOT = Path(__file__).resolve().parents[1]
SPEC_ID = "case.po_document_chain"
SPEC = json.loads((ROOT / "verify" / "live-checks" / f"{SPEC_ID}.json").read_text("utf-8"))
# The purchasing and operations mailboxes a PO's mail lives in, the shared purchase-order
# inbox first: the fake Graph serves all of them, whatever the check's mail scope names.
PURCHASING_MAIL = json.loads(
    (ROOT / "examples" / "case_mail_purchasing_capture_scope.live-check.json")
    .read_text("utf-8"))["mailboxes"]
MAILBOXES = [m["address"] for m in PURCHASING_MAIL]
INBOX = MAILBOXES[0]
LIVE_AUDIENCE = ROOT / "examples" / "po_document_chain_live_audience.live-check.json"
MIRROR_AUDIENCE = ROOT / "examples" / "po_document_chain_mirror_audience.live-check.json"
PURCHASING = Actor("plx", frozenset({"staff:purchasing"}), "restricted")
PO = 'PurchaseOrder:portal.app_purchase_order:{"id":"po-a"}'   # PO100777, FM twin 3301
CONFIRMATION = ("PROFORMA INVOICE\nPI No: PI-60001\nYour PO: PO100777\n"
                "Total Amount: USD 1,250.00\n")
SLIP = "PACKING LIST\nPacking List No: PL-5512\nPO: PO100777\nQuantity: 400 pcs\n"
FREIGHT = "BILL OF LADING\nB/L No: BL-7781\nPO: PO100777\nPackages: 12\n"
DOCUMENTS = (("A1", "pi.txt", CONFIRMATION), ("A2", "pl.txt", SLIP), ("A3", "bl.txt", FREIGHT))
DOCUMENT_LINKS = ("SupplierConfirmation.confirms", "PackingSlip.for_purchase_order",
                  "FreightBill.for_purchase_order")


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def _attachment(aid: str, name: str, text: str) -> dict:
    return dict(mail_t.file_attachment(aid, text.encode(), name), contentType="text/plain")


class Router:
    """One injected sender for Graph (every mailbox of the check's scope, an inbox and a
    sent-items folder each) and BC; ``documents``: the attachments of the one message,
    received in ``mailbox``; ``bc_down``: a company whose purchaseOrders never answer (its
    capture unit does not finish); ``directory_refused``: every mailbox lookup answers
    403 Authorization_RequestDenied (an app without User.Read.All)."""

    def __init__(self, documents=DOCUMENTS, bc_down: str | None = None,
                 mailbox: str = INBOX, directory_refused: bool = False) -> None:
        self.bc = tb.FakeBc({name: 1 if name == "purchaseOrders" else 0
                             for name in CAPTURE_ENTITY_SETS}, companies=tb.CO)
        if bc_down:
            self.bc.fail(rf"companies\({bc_down}\)/purchaseOrders",
                         *[(500, {}, b'{"error":{"code":"internal"}}')] * 20)
        self.mail = mail_t.FakeGraph()
        for state in (self.mail.users, self.mail.folders, self.mail.messages):
            state.clear()
        for n, address in enumerate(MAILBOXES):
            box = f"12345678-aaaa-bbbb-cccc-{n:012x}"
            self.mail.users[box] = {"id": box, "userPrincipalName": address, "mail": address,
                                    "proxyAddresses": [f"SMTP:{address}"]}
            folders = {name: f"AAMkFOLDER-{n}-{name}=" for name in ("inbox", "sentitems")}
            self.mail.folders[box] = {**folders, **{f.lower(): f for f in folders.values()}}
            self.mail.messages[box] = {}
        if directory_refused:
            self.mail.directory_forbidden = {"*"}
        [holder] = [b for b, u in self.mail.users.items() if u["mail"] == mailbox]
        received = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        self.mail.put(holder, "m1", "ck1", subject="PI and packing list",
                      body="Attached.", received=received,
                      attachments=[_attachment(*each) for each in documents] or None)

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if "businesscentral" in (urlsplit(url).hostname or ""):
            return self.bc(method, url, headers, body, timeout, max_bytes)
        return self.mail(method, url, headers, body, timeout, max_bytes)


def run_spec(temp: str, *, fetch=None, mail_budget=None, **router) -> tuple[int, dict, str]:
    """The spec's own command, its host config copied with a books map that pins the
    fixture companies (and ``mail_budget`` for its mail budget), over the injected
    sources."""
    fake = FakeWorkbench()
    fake.catalog = json.loads(json.dumps(fixture.CATALOG))
    fake.tables = fixture.tables()
    config = json.loads((ROOT / "examples" / "po_document_chain_host.live-check.json")
                        .read_text("utf-8"))
    for key in ("capture_audience", "live_audience", "mirror_audience"):
        config[key] = str(ROOT / "examples" / config[key])
    config["mail"]["scope"] = str(ROOT / "examples" / config["mail"]["scope"])
    if mail_budget is not None:
        config["mail"]["budget_seconds"] = mail_budget
    (Path(temp) / "books.json").write_text(json.dumps({
        "schema": "plx-bc-company-books/v1", "environments": tb.BOOKS}), "utf-8")
    config["bc"]["books"] = "books.json"
    (Path(temp) / "host.json").write_text(json.dumps(config), "utf-8")
    argv = [str(Path(temp) / "host.json") if arg.endswith("_host.live-check.json")
            else str(ROOT / arg) if arg.startswith("examples/")
            else arg.replace("{tmp}", temp) for arg in SPEC["command"][3:]]
    router = Router(**router)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = live_ws.main(argv, environ={
            **ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY},
            graph_sender=router, host_options={
                "sender": router, "workbench_fetch": fetch or fake, "sleep": lambda _s: None})
    return code, json.loads(out.getvalue()), out.getvalue()


class PoDocumentChainTest(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = mock.patch(target, _refuse_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = temp.name

    def session(self, actor: Actor, *, mailbox: bool = True):
        """A case session over the projection the spec run built, as ``actor``; without
        ``mailbox`` the live audience withdraws the inbox grant (BC stays granted)."""
        from integrations.live_source_guard import LiveSourceAudience
        from operating.case_runtime import build_live_readers
        from operating.ingestion import load_live_audience

        router = Router()
        audience = load_live_audience(LIVE_AUDIENCE, ENV) if mailbox else LiveSourceAudience(
            {"bc:production:*": sorted(actor.principals)}, tenant="plx",
            default_classification="confidential")
        live = build_live_readers(audience, ENV, graph_sender=router, bc_sender=router)
        state = Path(self.temp) / "state"
        runtime = live_ws.offline_runtime(state, MIRROR_AUDIENCE, "plx", live=live,
                                          environ=ENV)
        self.addCleanup(runtime.close)
        session = live_ws._Session(runtime, actor, AudienceMap.from_file(MIRROR_AUDIENCE),
                                   live_ws.projection_file(state, actor.tenant),
                                   captures=state)
        self.addCleanup(session.close)
        return session

    def test_the_spec_command_meets_its_expectations_and_records_the_document_links(self):
        self.assertEqual(lv.validate_spec(SPEC, stem=SPEC_ID), [])
        self.assertEqual(SPEC["cadence"], "daily")
        code, document, text = run_spec(self.temp)
        self.assertEqual(lv.check_expectations(SPEC, code, document), [], document)
        # The pick opened the PO a supplier confirmation names (the only one), and the
        # links around its case are the projection's, counted per schema link type for the
        # investigator (the two documents' links to the PO among them); the reader without
        # any grant here reads nothing of it.
        [case] = document["cases"]
        self.assertEqual((case["request"], case["status"]), ("pick", "opened"))
        # The confirmation's link is a machine-read reference: the pick says it chose the
        # root only through a lead-tier link (the case records the same as a gap).
        self.assertEqual(case["pick"], {
            "mode": "linked", "scanned": 1, "lead_tier": True, "candidates": 2,
            "candidates_joined": 2, "linked_held": 1, "linked_to_start": 1,
            "linked_refs_unheld": 0})
        investigator = next(r for r in case["readers"] if r["investigator"])
        edges = investigator["relationship_edges"]
        self.assertLessEqual(set(edges), set(load_schema().link_types), edges)
        self.assertLessEqual(set(DOCUMENT_LINKS), set(edges), edges)
        self.assertTrue(all(isinstance(n, int) and n > 0 for n in edges.values()), edges)
        self.assertGreaterEqual(investigator["relationship_nodes"], 2)
        self.assertEqual(investigator["relationship_truncated"], [])
        outsider = next(r for r in case["readers"] if not r["investigator"])
        self.assertEqual(outsider["status"], "not_readable")
        recorded = lv._recorded(SPEC, document)
        self.assertIn("relationship_edges", json.dumps(recorded))
        for value in ("PO100777", "PI-60001", "PL-5512", "BL-7781", "po-a", "api://",
                      "graph-mail://"):
            self.assertNotIn(value, text)

    def test_a_pick_that_finds_nothing_names_its_cause_and_an_outage_is_blocked(self):
        from integrations.http import SourceHttpError

        # No confirmation captured; one naming a PO no capture holds, with every unit read
        # (none joined) or while a BC company's purchaseOrders unit did not finish (the PO
        # may be the one the capture missed: never "none found").
        unheld = (("A1", "pi.txt", CONFIRMATION.replace("PO100777", "PO100555")),)
        # The host config's mail budget bounds the mail capture: a cycle it cuts is
        # incomplete, so a confirmation not held is not captured, never "none". A cause
        # that is a capture's state (live 2026-10-20: mail 6 of 10 folders, BC complete)
        # still roots the newest readable PO, its confirmation a typed gap, and the run
        # fails naming the source (``unlinked``: never a rendered linked case); no
        # confirmation held while SharePoint, the type's other source, is never captured
        # is no evidence of none either.
        for router, status, missing in (
                ({"documents": ()}, "unlinked", "pick_source_not_configured:sharepoint"),
                ({"documents": unheld}, "failed", "pick_linked_unjoined"),
                ({"documents": unheld, "bc_down": tb.CO[1]}, "unlinked",
                 "pick_source_not_captured:bc"),
                ({"mail_budget": 1e-9}, "unlinked", "pick_source_not_captured:graph_mail")):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                code, document, text = run_spec(temp, **router)
                self.assertEqual([(c["status"], c.get("missing")) for c in document["cases"]],
                                 [(status, missing)], document)
                self.assertEqual((code, document["failed"], document["failed_unnamed"],
                                  document["cases_rendered"], document["cases_unlinked"]),
                                 (1, 1, 0, 0, int(status == "unlinked")), document)
                self.assertIn("cases_rendered < 1",
                              " ".join(lv.check_expectations(SPEC, code, document)))
                if status == "unlinked":
                    [case] = document["cases"]
                    investigator = next(r for r in case["readers"] if r["investigator"])
                    self.assertEqual(investigator["status"], "read", case)
                    self.assertGreaterEqual(investigator["gaps_by_state"]["not_ingested"], 1)
                    self.assertEqual(document["row_leaks"], 0)
                    # The reader without the grant reads none of the unlinked case, and
                    # nothing printed names the linked type the gap stands for.
                    outsider = next(r for r in case["readers"] if not r["investigator"])
                    self.assertEqual((outsider["status"], outsider["case_text_leaks"],
                                      outsider["row_leaks"]), ("not_readable", 0, 0), case)
                    self.assertNotIn("SupplierConfirmation", text)
                sources = document["capture"]["sources"]
                self.assertEqual(sources["bc"]["complete"], "bc_down" not in router)
                self.assertEqual((sources["mail"]["complete"], sources["mail"]["reasons"]),
                                 (False, ["time_budget"]) if "mail_budget" in router
                                 else (True, []))

        def portal_down(url, headers, *, max_bytes):
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        with tempfile.TemporaryDirectory() as temp:
            code, document, _text = run_spec(temp, fetch=portal_down)
        self.assertTrue(lv.blocked_parts(document), document)
        self.assertEqual(lv.check_expectations(SPEC, code, document, SPEC["expect_blocked"]),
                         [], document)

        # Live 2026-10-18 (f3e2b5ed7): Graph refused every mailbox lookup (403, no
        # User.Read.All), so no confirmation could be captured: the pick is blocked on
        # the host's recorded refusal, never failed as not captured, and the run names
        # the refused part (pass only once the permission is granted).
        with tempfile.TemporaryDirectory() as temp:
            code, document, text = run_spec(temp, directory_refused=True)
            [case] = document["cases"]
            self.assertEqual((case["status"], case.get("missing")),
                             ("blocked", "blocked_upstream:graph_mail"), document)
            self.assertEqual((code, document["failed"], document["blocked"]), (0, 0, 1))
            self.assertIn({"part": "mail.mailbox_lookup", "source": "microsoft-graph",
                           "http_status": 403, "error_code": "missing_permission",
                           "cause": "host_misconfiguration", "permission": "User.Read.All"},
                          document["blocked_upstream"])
            self.assertEqual(lv.check_expectations(SPEC, code, document,
                                                   SPEC["expect_blocked"]), [], document)
            self.assertNotEqual(lv.check_expectations(SPEC, code, document), [])
            for address in MAILBOXES:
                self.assertNotIn(address, text)
            # A ledger row that does not read keeps the source blocked, never cleared.
            state = Path(temp) / "state"
            conn = sqlite3.connect(state / "host" / "host.sqlite")
            try:
                with conn:
                    conn.execute("UPDATE source_blocked SET parts = '{' WHERE source = 'mail'")
            finally:
                conn.close()
            self.assertEqual(live_ws.host_refusals(state), {"graph_mail": [
                {"source": "microsoft-graph", "part": "mail",
                 "error_code": "host_ledger_unreadable"}]})
            # The grant restored, the next cycle reads the mail: the refusal is cleared
            # and the pick opens.
            code, document, _text = run_spec(temp)
            self.assertEqual(lv.check_expectations(SPEC, code, document), [], document)
            self.assertEqual(document["blocked_upstream"], [])

    def test_a_confirmation_mailed_to_another_purchasing_mailbox_is_picked_for_grantees_only(self):
        # Live 2026-10-14 (pick_linked_absent, mail 3 messages in 1 folder): the check
        # captured the shared purchase-order inbox alone, so a supplier's confirmation
        # mailed to any other purchasing mailbox was never captured. The capture reads the
        # whole purchasing scope, every folder of it to the end, and the pick opens the PO
        # the confirmation names.
        other = MAILBOXES[-1]
        code, document, _text = run_spec(self.temp, mailbox=other)
        self.assertEqual(lv.check_expectations(SPEC, code, document), [], document)
        mail = document["capture"]["sources"]["mail"]
        folders = sum(len(m["folders"]) for m in PURCHASING_MAIL)
        self.assertEqual((mail["complete"], mail["counts"]["folders"],
                          mail["counts"]["messages_seen"]), (True, folders, 1), mail)
        [case] = document["cases"]
        self.assertEqual((case["status"], case["pick"]),
                         ("opened", {"mode": "linked", "scanned": 1, "lead_tier": True,
                                     "candidates": 2, "candidates_joined": 2,
                                     "linked_held": 1, "linked_to_start": 1,
                                     "linked_refs_unheld": 0}))
        result = self.session(PURCHASING)("plx.investigate", {"family": "chain", "root": PO})
        counted = {e["name"]: e for e in result["answer"] if e["kind"] == "count"}
        entry = counted["supplier_confirmation_count"]
        self.assertEqual(entry["value"], 1, entry)
        [confirmation] = entry["basis"]
        # The grant is the purchasing role's and the capture principal's alone: through
        # every tool that reads it the investigator sees the confirmation (its object or its
        # mail citation), a principal the audience does not list (the check's second reader,
        # the mail-capture service another check grants these mailboxes) sees none of it.
        marks = (json.dumps(confirmation)[1:-1], "graph-mail://")
        calls = (("plx.object", {"object_id": confirmation}),
                 ("plx.around", {"from": PO, "tier_floor": "lead", "path": [
                     {"link_type": "SupplierConfirmation.confirms", "direction": "reverse"}]}),
                 ("plx.find", {"query": "PI-60001"}),
                 ("plx.documents", {"query": "PI-60001"}),
                 ("plx.investigate", {"family": "chain", "root": PO}))
        for principal, sees in (("staff:purchasing", True), ("staff:operations", False),
                                ("svc:mail-capture", False)):
            session = self.session(Actor("plx", frozenset({principal}), "restricted"))
            for tool, arguments in calls:
                with self.subTest(principal=principal, tool=tool):
                    text = json.dumps(session(tool, arguments))
                    self.assertEqual(any(mark in text for mark in marks), sees, text[:300])

    def test_the_chain_answer_cites_the_po_documents_at_their_tiers_and_labels_the_leads(self):
        code, _document, _text = run_spec(self.temp)
        self.assertEqual(code, 0)
        result = self.session(PURCHASING)("plx.investigate", {"family": "chain", "root": PO})
        self.assertNotIn("error", result, result)
        counted = {e["name"]: e for e in result["answer"] if e["kind"] == "count"}
        for name, binding in (("supplier_confirmation_count",
                               "SupplierConfirmation:graph_mail.supplier_confirmations:"),
                              ("packing_slip_count", "PackingSlip:graph_mail.packing_slips:"),
                              ("freight_bill_count", "FreightBill:graph_mail.freight_bills:")):
            entry = counted[name]
            self.assertEqual(entry["value"], 1, entry)
            self.assertTrue(entry["basis"][0].startswith(binding), entry)
            # Reached through a machine-read reference only: the entry says it rests on one.
            self.assertTrue(entry.get("lead_basis"), entry)
        self.assertNotIn("lead_basis", counted["purchase_order_count"])
        # Each document is cited by its own edge to the PO (and to the PO's FM twin), at
        # the tier the projection stored for that link, never raised by the walk.
        edges = [e for e in result["chain"]["edges"] if e["link_type"] in DOCUMENT_LINKS]
        self.assertEqual({(e["link_type"], e["to"]) for e in edges},
                         {(link, end) for link in DOCUMENT_LINKS for end in (
                             PO, 'PurchaseOrder:portal.fm_generated_purchase_order:{"id":3301}')})
        self.assertEqual({e["tier"] for e in edges}, {"lead"})
        # A lead is never followed without the label: on each entry resting on one and as a
        # gap per step, never as an answerability reason; the answer is partial for the
        # evidence it could not read (this capture holds no BC purchase invoices).
        self.assertEqual(result["answerability"], {"state": "partial",
                                                   "reasons": ["gap_not_ingested"]})
        self.assertIn(("not_ingested", "PurchaseInvoice.names_order reverse"),
                      {(g["state"], g["subject"]) for g in result["gaps"]})
        labelled = {g["subject"] for g in result["gaps"]
                    if g["reason"] == "lead_tier_links_followed"}
        self.assertLessEqual({"documents", "twin_documents"}, labelled)
        followed = {s["step"] for s in result["trace"] if s["objects"]}
        self.assertLessEqual({"documents", "twin_documents"}, followed)
        self.assertNotIn("step_budget_exhausted", result["answerability"]["reasons"])
        # The plan asks for a lead only through its labelled retry, never by default.
        from operating.ontology_tools_investigate import load_plans
        for step in load_plans()["chain"].doc["steps"]:
            self.assertNotEqual((step.get("args") or {}).get("tier_floor"), "lead", step)
            if (step.get("retry") or {}).get("set_args", {}).get("tier_floor") == "lead":
                self.assertEqual(step["retry"]["note_gap"], "lead_tier_links_followed")

        # The same reader once the mailbox grant is withdrawn: the PO still answers, but
        # no document, no edge to one and no count of one.
        hidden = self.session(PURCHASING, mailbox=False)(
            "plx.investigate", {"family": "chain", "root": PO})
        self.assertEqual(hidden["root"]["object_id"], PO)
        text = json.dumps(hidden)
        for value in ("SupplierConfirmation:", "PackingSlip:", "FreightBill:", "PI-60001",
                      "PL-5512", "BL-7781"):
            self.assertNotIn(value, text)


class DerivedDocumentLinksTest(unittest.TestCase):
    """The committed chain plan over canned primitive answers (no store) and the compiled
    schema plus one document type it does not know: its document steps follow every
    mention link into the PO's type from a document-field type, the new one included, and
    a count of documents counts one file's copies once (by the bytes their citations pin)."""
    PO = 'PurchaseOrder:portal.app_purchase_order:{"id":"p"}'
    SAME, OTHER = "1" * 64, "2" * 64
    MAILBOX = "12345678-aaaa-bbbb-cccc-00000000000a"
    NOTICE = "ShippingNotice:graph_mail.shipping_notices:n1"

    def schema(self):
        from dataclasses import replace

        from operating.ontology_tools import default_schema
        base = default_schema()
        notice = {"id": "ShippingNotice", "properties": [], "bindings": [
            {"id": "graph_mail.shipping_notices", "source": "graph_mail",
             "relation": "documentFields", "key": ["source_key"]}]}
        link = {"id": "ShippingNotice.for_purchase_order", "from": "ShippingNotice",
                "to": "PurchaseOrder", "name": "for_purchase_order", "cardinality": "N:M",
                "bindings": [{"kind": "mention", "tier_ceiling": "lead"}]}
        return replace(base, object_types={**base.object_types, "ShippingNotice": notice},
                       link_types={**base.link_types, link["id"]: link})

    def documents(self):
        """link type -> (object id, l1 handle) of what links into the PO: one confirmation
        mailed and filed (same bytes), another mailed, and the first again as a member of a
        mailed zip (its own pin: never merged)."""
        mail = f"graph-mail://{self.MAILBOX}/m1@ck1#attachment="
        sc = "SupplierConfirmation:{}.supplier_confirmations:{}"
        return {"SupplierConfirmation.confirms": [
                    (sc.format("graph_mail", "a1"), f"{mail}A1&sha256={self.SAME}"),
                    (sc.format("sharepoint", "f1"), f"sharepoint://d1/i1@e1#sha256={self.SAME}"),
                    (sc.format("graph_mail", "a2"), f"{mail}A2&sha256={self.OTHER}"),
                    (sc.format("graph_mail", "z1"),
                     f"{mail}A9&sha256={self.OTHER}&member=pi.txt&member_sha256={self.SAME}")],
                "ShippingNotice.for_purchase_order": [
                    (self.NOTICE, f"{mail}A4&sha256={'3' * 64}")]}

    @staticmethod
    def shown(handle: str, principals) -> bool:
        """The mailbox grant shows mail copies, the files grant filed ones."""
        return ("staff:files" if handle.startswith("sharepoint") else "staff:mail") \
            in principals

    def table(self, calls: list):
        done = {"complete": True, "truncated": False, "returned": 0}
        meta = {"schema_digest": "a" * 64, "as_known_at": "2099-01-02", "freshness": []}
        documents = self.documents()

        def ref(oid, handle=None):
            display = None if handle is None else {"untrusted_text": "D-1", "origin": {
                "citation": handle, "field_path": "row.fields.document_number.value"}}
            return {"object_id": oid, "object_type": oid.split(":")[0], "display": display,
                    "lineage": "sharepoint" if handle and handle.startswith("sharepoint")
                    else "mail" if handle else "gold_native_staging"}

        def obj(arguments, actor):
            return {"object": ref(arguments["object_id"]), "values": [], "conflicts": [],
                    "identity": [], "linked_counts": [], "gaps": [], "completeness": done,
                    "meta": meta}

        def around(arguments, actor):
            [hop] = arguments["path"]
            calls.append(hop["link_type"])
            found = [(oid, handle) for oid, handle in documents.get(hop["link_type"], [])
                     if self.shown(handle, actor.principals)]
            base = {"hops": [dict(hop, count=len(found))], "completeness": done, "meta": meta}
            if found and arguments.get("tier_floor") != "lead":
                return dict(base, edges=[], objects=[], gaps=[{
                    "state": "unknown", "reason": "below_tier_floor",
                    "subject": hop["link_type"]}])
            return dict(base, gaps=[], objects=[ref(oid, handle) for oid, handle in found],
                        edges=[{"link_id": f"{hop['link_type']}:{n}",
                                "link_type": hop["link_type"], "from": oid,
                                "to": arguments["from"], "tier": "lead", "why_not": [],
                                "reason": [{"feature": "document_reference", "agrees": True}]}
                               for n, (oid, _h) in enumerate(found)])
        return {"plx.object": {"handler": obj}, "plx.around": {"handler": around}}

    def test_a_new_document_type_is_reached_and_copies_of_one_file_count_once(self):
        from operating import ontology_tools_investigate as oi

        schema = self.schema()
        plans = oi.load_plans(schema=schema)   # the committed plan files, unedited
        self.assertNotIn("ShippingNotice", json.dumps(plans["chain"].doc))
        for principals, basis in (
                (("staff:mail", "staff:files"), ("a1", "a2", "z1")),
                # A reader shown only the filed copy counts that one, never the mail.
                (("staff:files",), ("f1",))):
            with self.subTest(principals=principals):
                calls: list = []
                result = oi.InvestigateTools(self.table(calls), schema=schema, plans=plans,
                                             rows=lambda h, a: None).call(
                    "plx.investigate", {"family": "chain", "root": self.PO},
                    Actor("plx", frozenset(principals)))
                self.assertNotIn("error", result, result)
                # One plx.around per declared document link into the PO (the new one
                # included), asked again at lead tier only where the tool said so.
                hops = {hop["link_type"] for hop in oi.link_hops(schema, "PurchaseOrder", {
                    "binding_kind": "mention", "direction": "reverse",
                    "far_relation": "documentFields"})}
                self.assertIn("ShippingNotice.for_purchase_order", hops)
                self.assertNotIn("Email.mentions_purchase_order", hops)
                shown = {link for link, found in self.documents().items()
                         if any(self.shown(handle, principals) for _oid, handle in found)}
                self.assertEqual(Counter(c for c in calls if c in hops),
                                 {link: 2 if link in shown else 1 for link in hops})
                self.assertFalse({c for c in calls if ".mentions_" in c}, calls)
                reached = {ref["object_id"] for ref in result["chain"]["objects"]}
                self.assertEqual(self.NOTICE in reached, "staff:mail" in principals)
                entry = next(e for e in result["answer"]
                             if e["name"] == "supplier_confirmation_count")
                self.assertEqual(entry["value"], len(basis), entry)
                self.assertEqual([oid.rsplit(":", 1)[1] for oid in entry["basis"]],
                                 list(basis))
                self.assertEqual(len(entry["records"]), len(basis))
                self.assertTrue(entry["lead_basis"])


class PickCandidatesTest(unittest.TestCase):
    def test_a_record_a_document_names_is_a_candidate_however_many_are_newer(self):
        """Reproduction: the pick's candidates were the newest PICK_SCAN * 10 records of
        the type, so a purchase order a confirmation names behind 600 newer ones was never
        tried (pick_none_found). A record its source soft-deleted is never a candidate,
        nor reached through a soft-deleted document (``ontology.store.removed_at_sql``).
        Store tables reduced to the columns the pick reads."""
        import sqlite3

        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(
            "CREATE TABLE objects(object_id TEXT PRIMARY KEY, object_type TEXT, "
            "first_recorded_at TEXT, missing_from_source_since TEXT);"
            "CREATE TABLE object_removals(object_id TEXT, basis TEXT, since TEXT, "
            "until TEXT);"
            "CREATE TABLE links(link_type TEXT, from_object TEXT, to_object TEXT);"
            "CREATE TABLE events(event_id TEXT, time TEXT, window_end TEXT);"
            "CREATE TABLE event_objects(event_id TEXT, object_id TEXT);")
        for n in range(603):
            oid = f"PurchaseOrder:portal.app_purchase_order:{n}"
            db.execute("INSERT INTO objects VALUES (?, 'PurchaseOrder', '2026-01-01', NULL)",
                       (oid,))
            db.execute("INSERT INTO events VALUES (?, ?, NULL)",
                       (oid, "2020-01-01" if n < 3 else "2026-06-01"))
            db.execute("INSERT INTO event_objects VALUES (?, ?)", (oid, oid))

        def document(n: int, type_id: str = "SupplierConfirmation") -> str:
            oid = f"{type_id}:graph_mail.documents:d{n}"
            db.execute("INSERT INTO objects VALUES (?, ?, '2026-01-01', NULL)",
                       (oid, type_id))
            db.execute("INSERT INTO links VALUES (?, ?, ?)", (
                f"{type_id}.for_purchase_order", oid,
                f"PurchaseOrder:portal.app_purchase_order:{n}"))
            return oid

        def soft_delete(oid: str) -> None:
            db.execute("INSERT INTO object_removals VALUES (?, 'source_soft_delete', "
                       "'2026-02-01T00:00:00.000Z', NULL)", (oid,))

        named = "PurchaseOrder:portal.app_purchase_order:0"
        document(0)
        document(1)
        soft_delete("PurchaseOrder:portal.app_purchase_order:1")   # the PO deleted
        soft_delete(document(2))                                    # the document deleted
        soft_delete(document(3, "CertificateOfAnalysis"))
        self.assertEqual(live_ws._newest(db, "PurchaseOrder", "SupplierConfirmation"), [named])
        self.assertEqual(live_ws._link_paths(db, named, "SupplierConfirmation"), [[
            {"link_type": "SupplierConfirmation.for_purchase_order", "direction": "reverse"}]])
        # A type whose only object its source deleted is not held (pick_linked_absent).
        self.assertEqual((live_ws._held(db, "SupplierConfirmation"),
                          live_ws._held(db, "CertificateOfAnalysis")), (True, False))

    def test_no_linked_record_held_names_the_source_never_captured(self):
        """Reproduction (live 2026-10-20, case.customer_po_missing): no customer purchase
        order held, SharePoint captured in full and mail never configured, and the pick said
        pick_linked_absent, naming no source. A source of the linked type that the captures'
        freshness never names is that cause (an unreadable entry for it too); an unfinished
        one comes first; with every source captured in full, or no freshness at all to read,
        the absence stands."""
        import sqlite3

        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(
            "CREATE TABLE objects(object_id TEXT PRIMARY KEY, object_type TEXT, "
            "first_recorded_at TEXT, missing_from_source_since TEXT);"
            "CREATE TABLE object_removals(object_id TEXT, basis TEXT, since TEXT, "
            "until TEXT);"
            "CREATE TABLE store_meta(key TEXT PRIMARY KEY, value TEXT);")
        db.execute("INSERT INTO objects VALUES ('SalesOrder:portal.app_sales_order:1', "
                   "'SalesOrder', '2026-01-01', NULL)")
        done = {"documentFields": {"last_full_traversal_at": "2026-10-20T00:00:00Z",
                                   "scopes": [{"complete": True}]}}
        cut = {"documentFields": {"last_full_traversal_at": "2026-10-20T00:00:00Z",
                                  "scopes": [{"complete": False}]}}
        portal = {"portal_api": {"public.app_sales_order": done["documentFields"]}}
        for freshness, cause in (
                (None, "pick_linked_absent"),
                ("{", "pick_linked_absent"),
                ("[1]", "pick_linked_absent"),
                ({}, "pick_linked_absent"),
                ({**portal, "graph_mail": done, "sharepoint": done}, "pick_linked_absent"),
                ({**portal, "sharepoint": done}, "pick_source_not_configured:graph_mail"),
                ({**portal, "graph_mail": done}, "pick_source_not_configured:sharepoint"),
                ({**portal, "graph_mail": "x", "sharepoint": done},
                 "pick_source_not_configured:graph_mail"),
                ({**portal, "graph_mail": {}, "sharepoint": done},
                 "pick_source_not_configured:graph_mail"),
                ({**portal, "sharepoint": cut}, "pick_source_not_captured:sharepoint")):
            with self.subTest(freshness=freshness):
                db.execute("DELETE FROM store_meta")
                if freshness is not None:
                    db.execute("INSERT INTO store_meta VALUES ('source_freshness', ?)", (
                        freshness if isinstance(freshness, str) else json.dumps(freshness),))
                self.assertEqual(live_ws.CaseOpener._none_found(
                    db, "SalesOrder", "CustomerPoDocument", False), cause)
                # Only a capture-state cause roots a case without the linked record.
                self.assertEqual(cause.startswith(live_ws.UNLINKED_CAUSES),
                                 cause != "pick_linked_absent")

    def test_an_unlinked_root_is_the_newest_readable_within_the_scan_bound(self):
        """N=1/10/60 held records, only the oldest readable: the case is rooted at it while
        it is within PICK_SCAN reads, each read once, else the cause is raised; a recorded
        refusal or a Portal outage stays blocked, and a cause about the records
        (pick_linked_unjoined) roots nothing."""
        import sqlite3

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "projection.sqlite"
        sqlite3.connect(path).close()
        for n in (1, 10, 60):
            for cause, upstream, refused in (
                    ("pick_source_not_captured:graph_mail", None, {}),
                    ("pick_source_not_configured:sharepoint", None, {}),
                    ("pick_source_not_captured:graph_mail", "portal", {}),
                    ("pick_source_not_captured:graph_mail", None, {"graph_mail": [{}]}),
                    ("pick_linked_unjoined", None, {})):
                with self.subTest(n=n, cause=cause, upstream=upstream, refused=refused):
                    opener = object.__new__(live_ws.CaseOpener)
                    opener.projection = lambda: path
                    opener.upstream, opener.refused = upstream, refused
                    opener._pick_stats, opener._unlinked = None, None
                    opener._aliases = lambda: None
                    ids = [f"PurchaseOrder:bc.purchase_orders:{k}" for k in range(n)]
                    reads = []

                    def call(tool, args):
                        reads.append(args["object_id"])
                        if args["object_id"] != ids[-1]:
                            return {"error": {"code": "not_found"}}
                        return {"object": {"object_id": ids[-1], "display": "PO"}}
                    with mock.patch.object(live_ws, "_newest", lambda db, t, near=None:
                                           [] if near else list(ids)), \
                            mock.patch.object(live_ws, "_join_counts", lambda *a: {}), \
                            mock.patch.object(live_ws.CaseOpener, "_none_found",
                                              staticmethod(lambda *a: cause)):
                        try:
                            root, _display, _extra = opener._pick(
                                call, "PurchaseOrder+SupplierConfirmation")
                        except live_ws.OpenError as error:
                            root, code = None, error.code
                    rooted = (cause.startswith(live_ws.UNLINKED_CAUSES) and not upstream
                              and not refused and n <= live_ws.PICK_SCAN)
                    self.assertEqual(root and root["object_id"], ids[-1] if rooted else None)
                    self.assertEqual(opener._unlinked, cause if rooted else None)
                    self.assertEqual(len(reads), len(set(reads)))
                    self.assertLessEqual(len(reads), live_ws.PICK_SCAN)
                    if not rooted:
                        self.assertEqual(code, live_ws.BLOCKED_UPSTREAM if upstream else
                                         "blocked_upstream:graph_mail" if refused else cause)

if __name__ == "__main__":
    unittest.main()
