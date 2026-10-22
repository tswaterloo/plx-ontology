"""The ``mail`` section of ``scripts.po_twin_probe``: how many open Portal purchase orders
captured mail could corroborate firmly (an exact canonical PO-number mention, no veto, an
email whose supplier agrees), counts only, nothing promoted.

Evidence label: ``isolated-fixture`` (a synthetic mail capture over an injected Graph
transport, synthetic Portal and BC rows, the real projector, mention and twin steps and
the compiled schema; no source is read, no credential is used).
"""

from __future__ import annotations

import datetime as dt
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_mail import (BOX_A, CAPTURE, ENV, SCOPE, Clock,
                                                   FakeGraph, capture_audience)
from ontology import identifiers, mentions
from ontology import identity_scoring as ids
from ontology.project import MailCaptures, build_store
from ontology.schema import load_schema
from ontology.tests.test_po_identity_firm import Source, bc_po
from scripts import po_twin_probe

NOW = dt.datetime(2026, 10, 2, 12, tzinfo=dt.timezone.utc)
SCHEMA = load_schema()


def supplier(i, name, website=None):
    return {"id": f"s{i}", "code": f"{1000 + i}", "name": name, "external_id": f"fm-supp-{i}",
            "website": website}


def portal_po(i, number, supplier_id, status, *, fm_number=None, fm_id=None):
    custom = {"source": "etl-silver-to-gold"}
    if fm_number:
        custom["fmPoNumber"] = fm_number
    if fm_id:
        custom["fmGeneratedPoId"] = fm_id
    return {"id": f"p{i}", "po_number": number, "supplier_id": supplier_id, "status": status,
            "date_generated": "2026-03-02", "total_cost": "1000.00", "currency": "CAD",
            "custom_fields": custom}


class World:
    """Portal suppliers and orders, FM generated orders (``fm``), BC orders and mail
    messages (sender, subject, body)."""

    def __init__(self):
        self.suppliers, self.pos, self.fm, self.bc, self.mail = [], [], [], [], []

    def build(self, root: Path) -> Path:
        state = root / "mailstate"
        graph = FakeGraph()
        for n, (sender, subject, body) in enumerate(self.mail):
            mid = f"MSG-{n}"
            graph.put(BOX_A, mid, f"CK-{n}", subject=subject, body=body,
                      received="2026-09-20T10:00:00Z")
            graph.messages[BOX_A][mid]["from"] = {"emailAddress": {"address": sender}}
        store = MailCaptureStore(state, clock=Clock())
        try:
            api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
                sleep=lambda s: None, jitter=lambda: 0.0))
            run = MailCapture(store, graph_read.GraphMailReader(api, capture_audience()),
                              CAPTURE, sleep=lambda s: None).run(SCOPE)
            assert run["complete"], run
        finally:
            store.close()
        path = root / "ontology.sqlite"
        rows = {"portal_api": {"public.app_supplier": self.suppliers,
                               "public.app_purchase_order": self.pos,
                               "public.generated_purchase_order": self.fm},
                "bc": {"purchaseOrders": self.bc}}
        with MailCaptures(state, internal_domains=SCOPE.internal_domains) as mail:
            build_store(path, SCHEMA, {**{n: Source(n, r) for n, r in rows.items()},
                                       "graph_mail": mail},
                        normalizer=identifiers.normalize, clock=lambda: NOW,
                        post_project={"mentions": mentions.store_mention_links,
                                      "twins": ids.twin_step})
        return path


def probe(path):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = po_twin_probe.main(["--store", str(path), "--samples", "3"])
    assert code == 0
    return json.loads(out.getvalue()), out.getvalue()


class PoMailFirmTests(unittest.TestCase):
    def setUp(self):
        for key in [k for k in os.environ if k.startswith(("PLX_WORKBENCH_API", "BC_",
                                                           "MICROSOFT_GRAPH"))]:
            os.environ.pop(key)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_open_exact_agreeing_mail_counts_firm_and_every_other_case_does_not(self):
        w = World()
        w.suppliers += [supplier(1, "Amber Polymers Inc.", "https://www.amberpoly.example"),
                        supplier(2, "Birch Resins", "birchresins.example"),
                        supplier(3, "Cedar Metals", None),
                        supplier(4, "Dune Plastics", "duneplastics.example")]
        # the BC order of the same number is named too: several targets (a number-text
        # lead is no twin)
        w.pos += [portal_po(1, "PO#7001", "s1", "Pending"),
                  portal_po(2, "PO#7002", "s2", "Draft"),          # sender of another domain
                  portal_po(3, "PO#7003", "s3", "B.Order"),        # name in the subject
                  portal_po(4, "PO#7004", "s1", "Complete"),       # closed
                  portal_po(5, "PO123456", "s4", "Pending",        # its stored FM number
                            fm_number="PO#7005"),
                  portal_po(6, "PO#7006", "s4", "Shipped Somewhere"),   # unknown label
                  portal_po(7, "PO#7007", "s1", "Pending"),        # quoted history only
                  portal_po(8, "PO#7008", "s2", "Pending"),        # several targets
                  portal_po(9, "PO#7009", "s2", "Pending")]
        w.bc += [bc_po(1, "PO7001", "AMBER POLYMERS")]
        w.mail += [
            ("sales@amberpoly.example", "Order confirmation PO#7001", "We confirm PO#7001."),
            ("ops@otherfreight.example", "Order confirmation PO#7002", "Booked PO#7002."),
            ("cedar.metals.sales@gmail.com", "Cedar Metals order confirmation",
             "Order confirmation PO#7003 attached."),
            ("sales@amberpoly.example", "Order confirmation PO#7004", "We confirm PO#7004."),
            ("sales@duneplastics.example", "Order confirmation PO#7005", "Confirm PO#7005."),
            ("sales@duneplastics.example", "Order confirmation PO#7006", "Confirm PO#7006."),
            ("sales@amberpoly.example", "Re: hello",
             "Thanks.\n\n> On Monday you wrote:\n> Please confirm PO#7007"),
            ("sales@birchresins.example", "Two orders", "Confirming PO#7008 and PO#7009."),
        ]
        path = w.build(self.root)
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        snapshot = "SELECT link_id, tier, reason FROM links ORDER BY link_id"
        before = conn.execute(snapshot).fetchall()
        document, text = probe(path)
        mail = document["mail"]
        self.assertEqual(mail["portal_pos"], 9)
        self.assertEqual(mail["status_buckets"], {"BORDER": 1, "COMPLETE": 1, "DRAFT": 1,
                                                  "PENDING": 5, "other": 1})
        self.assertEqual(mail["open_portal_pos"], 7)
        self.assertEqual(mail["with_mention"], 9)
        self.assertEqual(mail["with_mention_open"], 7)
        self.assertEqual((mail["exact_canonical"], mail["exact_canonical_open"]), (8, 6))
        self.assertEqual((mail["exact_stored_fm"], mail["exact_stored_fm_open"]), (1, 1))
        self.assertEqual((mail["exact"], mail["exact_open"]), (9, 7))
        self.assertEqual((mail["alias_only"], mail["alias_only_open"]), (0, 0))
        self.assertEqual(mail["alias_shapes"], {})
        self.assertEqual(mail["alias_only_open_supplier"],
                         {"agreed": 0, "conflict": 0, "unknown": 0, "vetoed": 0})
        # per vetoing mention: PO 1 twice (subject and body name it and the BC order),
        # PO 8 and PO 9 once each
        self.assertEqual(mail["mention_vetoes_open"], {"quoted_history": 1,
                                                       "several_targets": 4})
        self.assertEqual(mail["exact_unvetoed_open"], 3)
        self.assertEqual(mail["supplier"], {"agreed": 2, "conflict": 1, "unknown": 0})
        self.assertEqual(mail["agreed_by"], {"sender_domain": 1, "supplier_name": 1})
        self.assertEqual((mail["firm"], mail["firm_with_bc_lead"],
                          mail["firm_without_bc_lead"]), (2, 0, 2))
        # the build raised the stored-FM order (sender domain); the name-only agreement
        # is the probe's rule, never a corroborating feature
        self.assertEqual((mail["corroborated_open"], mail["corroborated_not_firm_open"],
                          mail["firm_not_corroborated_open"]), (1, 0, 1))
        self.assertEqual((mail["confirmed_date"], mail["promoted"]), (0, 0))
        # read-only, nothing promoted, existing keys unchanged
        self.assertEqual(document["links_written"], 0)
        self.assertEqual(document["leads"], 1)
        self.assertEqual(conn.execute(snapshot).fetchall(), before)
        # counts and labels only: no number, name, domain or address
        for leak in ("7001", "7005", "123456", "amber", "Amber", "birch", "cedar",
                     "example", "@", "Shipped"):
            self.assertNotIn(leak, text)

    def test_unknown_status_and_unknown_supplier_fail_closed(self):
        self.assertEqual(po_twin_probe.status_label({"Complete"}), "COMPLETE")
        self.assertEqual(po_twin_probe.status_label({"b.order"}), "BORDER")
        self.assertEqual(po_twin_probe.status_label({"Shipped Somewhere"}), "other")
        self.assertEqual(po_twin_probe.status_label(set()), "none")
        self.assertEqual(po_twin_probe.status_label({"Draft", "Cancel"}), "ambiguous")
        for label in ("other", "none", "ambiguous"):
            self.assertNotIn(label, po_twin_probe.OPEN_STATUSES)
        verdict = po_twin_probe._email_verdict
        held = lambda counterpart: set()   # noqa: E731
        self.assertEqual(verdict("a@x.example", "external", "", None, held), ("unknown", None))
        sup = {"id": "s1", "sites": {"x.example"}, "names": {"ab"}}
        # a too-short name never agrees; a shared provider never conflicts
        self.assertEqual(verdict("a@gmail.com", "external", "ab order", sup, held),
                         ("unknown", None))
        self.assertEqual(verdict("a@sub.x.example", "external", "", sup, held),
                         ("agreed", "sender_domain"))
        self.assertEqual(verdict("a@x.example", "external", "", sup,
                                 lambda c: {"s1", "s2"}), ("unknown", None))

    def test_cardinality_one_ten_sixty(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                root = self.root / f"n{n}"
                root.mkdir()
                w = World()
                w.suppliers.append(supplier(1, "Amber Polymers Inc.", "amberpoly.example"))
                for i in range(n):
                    w.pos.append(portal_po(i + 1, f"PO#{8000 + i}", "s1", "Pending"))
                    w.mail.append(("sales@amberpoly.example", f"Order confirmation PO#{8000 + i}",
                                   f"We confirm PO#{8000 + i}."))
                mail = probe(w.build(root))[0]["mail"]
                self.assertEqual((mail["open_portal_pos"], mail["with_mention"],
                                  mail["exact_unvetoed_open"], mail["firm"],
                                  mail["firm_without_bc_lead"]), (n, n, n, n, n))
                self.assertEqual(mail["supplier"], {"agreed": n, "conflict": 0, "unknown": 0})

    def test_twin_pairs_cardinality_one_ten_sixty(self):
        # each email names a Portal order by its stored FM number, which its FM twin
        # holds too: n twin pairs, n firm and n corroborated, none lost or doubled
        for n in (1, 10, 60):
            with self.subTest(n=n):
                root = self.root / f"t{n}"
                root.mkdir()
                w = World()
                w.suppliers.append(supplier(1, "Amber Polymers Inc.", "amberpoly.example"))
                for i in range(n):
                    w.pos.append(portal_po(i + 1, f"PO{900000 + i}", "s1", "Pending",
                                           fm_number=f"PO#{8000 + i}", fm_id=str(600 + i)))
                    w.fm.append({"id": 600 + i, "ponumber": f"PO#{8000 + i}"})
                    w.mail.append(("sales@amberpoly.example", "Order confirmation",
                                   f"We confirm PO#{8000 + i}."))
                started = time.monotonic()
                mail = probe(w.build(root))[0]["mail"]
                self.assertLess(time.monotonic() - started, 60)
                self.assertEqual((mail["open_portal_pos"], mail["exact_stored_fm_open"],
                                  mail["exact_unvetoed_open"], mail["firm"],
                                  mail["corroborated_open"], mail["mention_links"]),
                                 (n, n, n, n, n, 2 * n))


class StoredFmNumberFirmTests(unittest.TestCase):
    """Decision 2026-10-20-po-fm-number-own-identifier: the FM PO number a Portal order
    stores is its own identifier, so an exact mention of it corroborates like the
    canonical number (``mentions.corroborate_links``), and an email naming the order and
    its own FM twin names one record, not several. Synthetic fixtures only."""

    @classmethod
    def setUpClass(cls):
        for key in [k for k in os.environ if k.startswith(("PLX_WORKBENCH_API", "BC_",
                                                           "MICROSOFT_GRAPH"))]:
            os.environ.pop(key)
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        w = World()
        w.suppliers += [supplier(1, "Amber Polymers Inc.", "amberpoly.example"),
                        supplier(2, "Birch Resins", "birchresins.example")]
        w.pos += [
            portal_po(11, "PO123411", "s1", "Pending", fm_number="PO#7011"),   # stored field
            portal_po(12, "PO123412", "s1", "Pending", fm_number="PO#7012",    # + its twin
                      fm_id="512"),
            portal_po(13, "PO123413", "s1", "Pending"),         # no stored FM number
            portal_po(14, "PO123414", "s1", "Pending", fm_number="PO#7014", fm_id="514"),
            portal_po(15, "PO123415", "s1", "Pending"),         # unrelated third order
            portal_po(16, "PO123416", "s1", "Pending", fm_number="PO#7016"),
            portal_po(18, "PO123418", "s2", "Pending", fm_number="PO#7018"),   # conflict
            # its FM twin is joined by the stored number only (no stored FM id)
            portal_po(19, "PO123419", "s1", "Pending", fm_number="PO#7019")]
        w.fm += [{"id": n, "ponumber": f"PO#{n + 6500}"} for n in (512, 513, 514, 517, 519)]
        w.bc += [bc_po(1, "PO123411", "AMBER POLYMERS")]
        w.mail += [
            ("sales@amberpoly.example", "Order confirmation", "We confirm PO#7011."),
            ("sales@amberpoly.example", "Order confirmation", "We confirm PO#7012."),
            # the FM-shaped number of an FM order no Portal order stores
            ("sales@amberpoly.example", "Order confirmation", "We confirm PO#7013."),
            ("sales@amberpoly.example", "Two orders", "Confirm PO#7014 and PO123415."),
            # a Portal order plus an FM order that is not its twin
            ("sales@amberpoly.example", "Two orders", "Confirm PO#7016 and PO#7017."),
            ("ops@otherfreight.example", "Order confirmation", "We confirm PO#7018."),
            ("sales@amberpoly.example", "Order confirmation", "We confirm PO#7019.")]
        cls.world = w
        cls.path = w.build(root)
        cls.state = root / "mailstate"
        cls.conn = sqlite3.connect(cls.path)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def tiers(self):
        """(email number, target relation, target id) -> tier of every mention link."""
        out = {}
        for row in self.conn.execute(
                "SELECT l.tier, l.to_object, json_extract(e.native_key, '$.id') AS mail, "
                "t.binding_id FROM links l JOIN objects e ON e.object_id = l.from_object "
                "JOIN objects t ON t.object_id = l.to_object WHERE l.link_type = ?",
                (po_twin_probe.MAIL_LINK,)):
            target = json.loads(row["to_object"].split(":", 2)[2])["id"]
            out[(row["mail"], row["binding_id"].split(".", 1)[1], target)] = row["tier"]
        return out

    def link(self, mail, target):
        return self.conn.execute(
            "SELECT l.* FROM links l JOIN objects e ON e.object_id = l.from_object WHERE "
            "l.link_type = ? AND json_extract(e.native_key, '$.id') = ? AND "
            "l.to_object LIKE ?", (po_twin_probe.MAIL_LINK, mail, f'%"{target}"%')).fetchone()

    def test_stored_field_and_twin_pair_corroborate_and_every_veto_holds(self):
        tiers = self.tiers()
        self.assertEqual(tiers, {
            # the stored FM number, one target, the supplier's domain agrees
            ("MSG-0", "app_purchase_order", "p11"): "corroborated",
            # the order and its own FM twin: one record, both raised
            ("MSG-1", "app_purchase_order", "p12"): "corroborated",
            # (the FM order has no supplier record: its own link has one feature)
            ("MSG-1", "fm_generated_purchase_order", 512): "lead",
            # an FM order no Portal order stores: the Portal order is never reached
            ("MSG-2", "fm_generated_purchase_order", 513): "lead",
            # twin plus an unrelated order: several targets
            ("MSG-3", "app_purchase_order", "p14"): "lead",
            ("MSG-3", "fm_generated_purchase_order", 514): "lead",
            ("MSG-3", "app_purchase_order", "p15"): "lead",
            # a Portal order plus an FM order that is not its twin
            ("MSG-4", "app_purchase_order", "p16"): "lead",
            ("MSG-4", "fm_generated_purchase_order", 517): "lead",
            # supplier conflict: another distinctive sender domain
            ("MSG-5", "app_purchase_order", "p18"): "lead",
            # the twin pair by the stored number only: one record
            ("MSG-6", "app_purchase_order", "p19"): "corroborated",
            ("MSG-6", "fm_generated_purchase_order", 519): "lead"})
        # evidence: the email handle first, the stored field, the supplier agreement
        own = self.link("MSG-0", "p11")
        reason = {f["feature"]: f for f in json.loads(own["reason"])}
        self.assertEqual(reason["identity_candidate"]["property"], "fm_po_number")
        self.assertEqual(reason["identity_candidate"]["role"], "alias")
        self.assertIn("party_domain", reason)
        self.assertNotIn("same_record_twin", reason)
        handles = json.loads(own["evidence_handles"])
        self.assertTrue(handles[0].startswith("graph-mail:") or "mail" in handles[0])
        self.assertIn("api://public.app_supplier/", " ".join(handles))
        twin = self.link("MSG-1", "p12")
        reason = {f["feature"]: f for f in json.loads(twin["reason"])}
        self.assertEqual((reason["same_record_twin"]["basis"],
                          reason["same_record_twin"]["property"]),
                         ("same_record_link", "fm_po_number"))
        self.assertIn("party_domain", reason)
        self.assertIn("api://public.generated_purchase_order/",
                      " ".join(json.loads(twin["evidence_handles"])))
        stored = {f["feature"]: f for f in json.loads(self.link("MSG-6", "p19")["reason"])}
        self.assertEqual((stored["same_record_twin"]["basis"],
                          stored["same_record_twin"]["twin_property"]),
                         ("stored_field", "fm_po_number"))
        # the binding kind and tier stay within the store constraints
        self.assertEqual({(r[0], r[1]) for r in self.conn.execute(
            "SELECT DISTINCT binding_kind, tier FROM links WHERE link_type = ?",
            (po_twin_probe.MAIL_LINK,))}, {("mention", "lead"), ("mention", "corroborated")})

    def test_the_probe_counts_firm_by_the_stored_field_rule(self):
        document, text = probe(self.path)
        mail = document["mail"]
        self.assertEqual(mail["open_portal_pos"], 8)
        self.assertEqual((mail["exact_canonical_open"], mail["exact_stored_fm_open"],
                          mail["exact_open"]), (1, 6, 7))
        # per vetoing mention: the twin pair's two links, the third order, the non-twin
        self.assertEqual(mail["mention_vetoes_open"], {"several_targets": 4})
        self.assertEqual(mail["supplier"], {"agreed": 3, "conflict": 1, "unknown": 0})
        self.assertEqual((mail["firm"], mail["firm_with_bc_lead"],
                          mail["firm_without_bc_lead"]), (3, 1, 2))
        self.assertEqual((mail["corroborated_open"], mail["corroborated_open_with_bc_lead"],
                          mail["corroborated_open_without_bc_lead"],
                          mail["corroborated_not_firm_open"]), (3, 1, 2, 0))
        self.assertEqual(document["links_written"], 0)
        for leak in ("7011", "7012", "123411", "amber", "Amber", "example", "@", "512"):
            self.assertNotIn(leak, text)

    def test_a_reader_without_the_features_sees_the_lead(self):
        from integrations.capture_mail import MailCaptureStore as Store
        from integrations.tests.test_capture_mail import READER_A, reader_audience
        from ontology import store_ddl
        from operating import ontology_tools as ot
        from operating import ontology_tools_traverse as tt
        contracts = json.loads((Path(__file__).resolve().parents[2]
                                / "operating/ontology_tool_contracts.json").read_text("utf-8"))
        store = Store(self.state, clock=Clock())
        self.addCleanup(store.close)

        def edge(target, relations):
            policy = ot.ReadPolicy(tenant="plx", audiences={
                **{f"api:{r}": {"staff:orders"} for r in relations},
                "mail:*": {"staff:orders"}},
                markings={m: {"staff:orders"} for m in store_ddl.MARKINGS})
            reader = ot.StoreReader.open(self.path)
            self.addCleanup(reader.conn.close)
            base = ot.OntologyTools(reader, policy, item_gate=store.item_gate(reader_audience()),
                                    schema=ot.SchemaView.from_compiled(SCHEMA), grammar=None,
                                    contracts=contracts, clock=lambda: NOW)
            row = self.link({"p11": "MSG-0", "p12": "MSG-1"}[target], target)
            around = tt.TraverseTools(base).call("plx.around", {
                "from": row["to_object"],
                "path": [{"link_type": po_twin_probe.MAIL_LINK, "direction": "reverse"}],
                "tier_floor": "lead", "response_format": "detailed"}, READER_A)
            [found] = [e for e in around["edges"] if e["from"] == row["from_object"]]
            counts = base.call("plx.object", {"object_id": row["to_object"]}, READER_A)
            [count] = [c for c in counts["linked_counts"]
                       if c["link_type"] == po_twin_probe.MAIL_LINK]
            return found, count["tiers"]

        everything = ("public.app_purchase_order", "public.app_supplier",
                      "public.generated_purchase_order")
        for target in ("p11", "p12"):
            with self.subTest(target=target, reader="all"):
                found, tiers = edge(target, everything)
                self.assertEqual(found["tier"], "corroborated")
                self.assertIn("corroborated", tiers)
            with self.subTest(target=target, reader="no supplier"):
                found, tiers = edge(target, everything[:1] + everything[2:])
                self.assertEqual(found["tier"], "lead")
                self.assertIn("corroboration_not_visible",
                              {f["feature"] for f in found["why_not"]})
                self.assertNotIn("party_domain", {f["feature"] for f in found["reason"]})
                self.assertNotIn("corroborated", tiers)
                self.assertNotIn("app_supplier", json.dumps(found))
        # the twin fold reads the FM order: a reader who may not read it sees the lead
        found, tiers = edge("p12", everything[:2])
        self.assertEqual(found["tier"], "lead")
        self.assertNotIn("same_record_twin", {f["feature"] for f in found["reason"]})
        self.assertNotIn("generated_purchase_order", json.dumps(found))
        self.assertNotIn("corroborated", tiers)


if __name__ == "__main__":
    unittest.main()
