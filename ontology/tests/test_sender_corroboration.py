"""Supplier confirmations firmed by their sender (2026-11-02: every
one of the 444 live confirmation links was a lead). A link is ``corroborated`` only when
the PO number is read in the document, or in the subject, body or attachment name of the
message carrying it, AND that message came from a distinctive domain of the PO's own
supplier; anything weaker stays a lead, and the lead stays beside the firm link.

Evidence label: ``isolated-fixture`` (a synthetic mail capture over an injected Graph
transport, synthetic Portal rows, the real projector, mention, document-field and
sender-corroboration steps and the compiled schema; no source read, no credential used).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_mail import (BOX_A, CAPTURE, ENV, SCOPE, UPN_A, Clock,
                                                   FakeGraph, capture_audience,
                                                   file_attachment)
from ontology import document_fields, identifiers, mentions, store_ddl
from ontology import sender_corroboration as sc
from ontology.json_contract import validate
from ontology.project import MailCaptures, build_store
from ontology.schema import load_schema
from ontology.tests.test_document_fields import pdf
from ontology.tests.test_po_identity_firm import Source
from ontology.tests.test_po_mail_firm import portal_po, supplier
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_process as op

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2026, 10, 2, 12, tzinfo=dt.timezone.utc)
SCHEMA = load_schema()
LINK = "SupplierConfirmation.confirms"


def oc(*lines):
    return pdf([["ORDER CONFIRMATION", *lines]])


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "confirmations are read from PDFs: needs pypdf and reportlab")
class SenderCorroborationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for key in [k for k in os.environ if k.startswith(("PLX_WORKBENCH_API", "BC_",
                                                           "MICROSOFT_GRAPH"))]:
            os.environ.pop(key)
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        suppliers = [supplier(1, "Amber Polymers Inc.", "https://www.amberpoly.example"),
                     supplier(3, "Cedar Metals", None),
                     supplier(6, "Shared One", "sharedpack.example"),
                     supplier(7, "Shared Two", "sharedpack.example")]
        pos = [portal_po(1, "PO7001", "s1", "Pending"),
               portal_po(3, "PO7003", "s3", "Pending"),
               portal_po(4, "PO7004", "s1", "Pending"),
               {**portal_po(10, "PO7010", "s1", "Pending"), "date_generated": "2026-12-01"},
               portal_po(11, "PO7011", "s6", "Pending")]
        mail = [  # (sender, subject, body, attachment lines)
            ("sales@amberpoly.example", "OC", "We will deliver your order Oct 20th.",
             ["Your PO: PO#7001"]),                                   # firm
            ("cedar.metals.sales@gmail.com", "OC", "attached", ["Your PO: PO#7003"]),
            ("sales@amberpoly.example", "Order confirmation PO#7004", "attached",
             ["Total Amount: USD 10.00"]),                            # carrier, firm
            (UPN_A, "FW: OC", "see below", ["Your PO: PO#7001"]),      # a colleague's forward
            ("sales@amberpoly.example", "OC", "attached", ["Your PO: PO#7010"]),  # predates
            ("sales@sharedpack.example", "OC", "attached", ["Your PO: PO#7011"]),
            ("sales@amberpoly.example", "POs PO#7001 and PO#7004", "attached",
             ["Total Amount: USD 20.00"])]                            # names two: no carrier
        graph = FakeGraph()
        for n, (sender, subject, body, lines) in enumerate(mail):
            mid = f"MSG-{n}"
            graph.put(BOX_A, mid, f"CK-{n}", subject=subject, body=body,
                      attachments=[file_attachment(f"ATT-{n}", oc(*lines), f"oc{n}.pdf")],
                      received="2026-09-20T10:00:00Z")
            graph.messages[BOX_A][mid]["from"] = {"emailAddress": {"address": sender}}
        state = root / "mailstate"
        store = MailCaptureStore(state, clock=Clock())
        try:
            api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
                sleep=lambda s: None, jitter=lambda: 0.0))
            run = MailCapture(store, graph_read.GraphMailReader(api, capture_audience()),
                              CAPTURE, sleep=lambda s: None).run(SCOPE)
            assert run["complete"], run
        finally:
            store.close()
        cls.path = root / "ontology.sqlite"
        for po in pos:   # a Portal order with no FileMaker twin to read (decision 4)
            po["custom_fields"] = {**po["custom_fields"], "fmTable": "po", "fmId": po["id"]}
        lines = [{"id": f"l{po['id']}", "purchase_order_id": po["id"], "quantity": "10",
                  "quantity_received": "0", "uom": "EA", "unit_cost": "1"} for po in pos]
        rows = {"public.app_supplier": suppliers, "public.app_purchase_order": pos,
                "public.app_purchase_order_line": lines}
        with MailCaptures(state, internal_domains=SCOPE.internal_domains) as captures:
            cls.report = build_store(
                cls.path, SCHEMA, {"portal_api": Source("portal_api", rows),
                                   "graph_mail": captures},
                normalizer=identifiers.normalize, clock=lambda: NOW,
                post_project={"document_fields": document_fields.store_document_links,
                              "mentions": mentions.store_mention_links,
                              "sender_corroboration": sc.sender_corroboration_step})
        cls.conn = sqlite3.connect(cls.path)
        cls.conn.row_factory = sqlite3.Row
        # the synthetic Portal source states no capture state: read it as a complete,
        # fresh capture (as tests.test_ontology_tools_process's store), so an open PO's
        # lines decide its status
        with cls.conn:
            cls.conn.execute("INSERT OR REPLACE INTO store_meta VALUES ('freshness', ?)", (
                json.dumps([{"source": "portal_api", "captured_at": "2026-10-02T11:00:00Z",
                             "lag_seconds": 3600, "completeness": "complete",
                             "stale": False}]),))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def links(self):
        """message index -> {(PO number, tier, link id prefix)}."""
        out: dict[int, set] = {}
        for row in self.conn.execute("SELECT * FROM links WHERE link_type = ?", (LINK,)):
            n = int(row["from_object"].split("ATT-", 1)[1].split("\\", 1)[0])
            number = self.conn.execute(
                "SELECT value FROM object_values WHERE object_id = ? AND property = "
                "'po_number'", (row["to_object"],)).fetchone()[0]
            out.setdefault(n, set()).add((number, row["tier"], row["link_id"].split(":")[0]))
        return out

    def test_only_a_number_read_and_the_suppliers_own_domain_make_a_link_firm(self):
        links = self.links()
        # read in the document, sent from the supplier's own domain: firm beside the lead
        self.assertEqual(links[0], {("PO7001", "lead", "docfield"),
                                    ("PO7001", "corroborated", "docfirm")})
        # a free-mail sender, a colleague's forward, a message dated before the order and
        # a domain two suppliers hold: leads only
        for n in (1, 3, 4, 5):
            self.assertEqual({tier for _, tier, _ in links[n]}, {"lead"}, n)
        # a number only the subject names: a carrier lead, firm from the supplier's domain
        self.assertEqual(links[2], {("PO7004", "lead", "doccarrier"),
                                    ("PO7004", "corroborated", "docfirm")})
        # a message naming two orders says not which one its document confirms: nothing
        self.assertNotIn(6, links)

    def test_the_firm_link_cites_the_party_and_the_sender_and_a_reader_blind_to_them_reads_no_promise(self):
        firm = self.conn.execute(
            "SELECT * FROM links WHERE link_type = ? AND link_id LIKE 'docfirm:%' AND "
            "from_object LIKE '%ATT-0%' ORDER BY link_id", (LINK,)).fetchone()
        features = {f["feature"] for f in json.loads(firm["reason"])}
        self.assertTrue({"document_reference", "identity_candidate", "party_domain",
                         "sender_domain"} <= features)
        handles = json.loads(firm["evidence_handles"])
        self.assertTrue(any(h.startswith("api://public.app_supplier/") for h in handles))
        self.assertTrue(any(h.startswith("graph-mail://") and "#" not in h for h in handles))
        # plx.process: the firm link is counted, so the message's promise is judged
        po = self.conn.execute("SELECT to_object FROM links WHERE link_id = ?",
                               (firm["link_id"],)).fetchone()[0]

        def vendor(audiences):
            policy = ot.ReadPolicy(tenant="plx", audiences=audiences,
                                   markings={m: {"staff:x"} for m in store_ddl.MARKINGS})
            reader = ot.StoreReader.open(self.path)
            self.addCleanup(reader.conn.close)
            tools = op.ProcessTools(ot.OntologyTools(
                reader, policy, item_gate=lambda actor, handle: True,
                schema=ot.SchemaView.from_compiled(SCHEMA), grammar=None,
                contracts=CONTRACTS, clock=lambda: NOW))
            out = tools.call("plx.process", {"from": po, "process": "po_delivery_window"},
                             Actor("plx", frozenset({"staff:x"}), "restricted"))
            self.assertEqual(validate(out, CONTRACTS["tools"]["plx.process"]["output"],
                                      root=CONTRACTS), [], json.dumps(out)[:1500])
            (entry,) = [e for e in out["lateness"] if e["stage"] == "receipt"]
            self.last = entry
            return entry.get("vs_vendor_promise")

        everyone = {"api:*": {"staff:x"}, "mail:*": {"staff:x"}, "bc:*": {"staff:x"}}
        judged = vendor(everyone)
        self.assertIsNotNone(judged, json.dumps(self.last)[:3000])
        self.assertEqual((judged["basis"], judged["promised_date"]),
                         ("vendor_promise", "2026-10-20"))
        blind = vendor({"api:public.app_purchase_order": {"staff:x"},
                        "api:public.app_purchase_order_line": {"staff:x"},
                        "mail:*": {"staff:x"}, "bc:*": {"staff:x"}})
        # the firm link cites the supplier record this reader may not read: the promise
        # is withheld (fail closed), never stated, judged or offered as a possible one
        self.assertIsNone(blind)
        self.assertNotIn("2026-10-20", json.dumps(self.last))
        self.assertIn("withheld_by_permission", [g["reason"] for g in self.last["gaps"]])


if __name__ == "__main__":
    unittest.main()
