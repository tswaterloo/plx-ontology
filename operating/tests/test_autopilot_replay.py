"""The purchasing roster, item cross-references, the per-role replay harness and the live
gate (``operating.autopilot_roster``, ``item_xref``, ``autopilot_replay``,
``autopilot.gate_from``).

SYNTHETIC fixtures only; evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from operating import autopilot as ap
from operating import autopilot_replay as rp
from operating import autopilot_roster as roster
from operating import proactive_engine as pe
from operating import workflows as wf
from operating.tests.test_autopilot import (NOW, OTHER, READER, SENT, Fixture, everyone,
                                            opens_all)
from operating.tests.test_proactive_engine import EXCLUDED_OID, OURS_OID
from operating.tests.test_proactive_threads import APPROVER, APPROVER_OID, BOSS, OURS, mail, people

REP = "rep@synth-supplier.example"
DAY = datetime(SENT.year, SENT.month, SENT.day, tzinfo=timezone.utc)


def at(hour: int, day: int = 0) -> str:
    return ap._iso(DAY + timedelta(days=day, hours=hour))


class World(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.events: list[tuple[str, str, str]] = []
        self.supplier = self.w.obj("Supplier", {"name": "Synth supplier"})

    def sent(self, oid: str, hour: int = 15, day: int = 0) -> str:
        self.events.append(("Email.received", oid, at(hour, day)))
        return oid

    def store(self) -> pe._Store:
        self.build(events=self.events)
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        return st

    def po(self, i: int, *, supplier: str | None = None) -> str:
        po = self.w.obj("PurchaseOrder", {"po_number": f"P-{900 + i}", "status": "Released",
                                          "expected_date": "2026-10-20",
                                          "order_date": "2026-10-01"})
        self.w.link("PurchaseOrder.ordered_from", po, supplier or self.supplier)
        return po

    def supplier_mail(self, i: int, body: str, *, po: str | None = None) -> str:
        made = mail(self.w, SENT, sender=REP, to=(OURS,), body=body, subject="Shipping",
                    mailbox=OURS_OID, conversation=f"sup-{i}", n=50_000 + i, hour=10)
        self.w.link("Email.sent_by_supplier", made, self.supplier)
        if po:
            self.w.link("Email.mentions_po", made, po)
        return self.sent(made, 10)

    def reply(self, i: int, *, hour: int = 14, day: int = 0, sender: str = OURS,
              box: str = OURS_OID) -> str:
        made = mail(self.w, SENT + timedelta(days=day), sender=sender, to=(REP,),
                    body="Thanks, noted.", subject="Re: Shipping", mailbox=box,
                    conversation=f"sup-{i}", n=60_000 + i, hour=hour)
        return self.sent(made, hour, day)


class Roster(World):
    def po_mail(self, i: int, po: str, sender: str, box: str) -> None:
        made = mail(self.w, SENT, sender=sender, to=(REP,), body=f"Please find PO P-{900 + i}",
                    subject=f"PO P-{900 + i}", mailbox=box, conversation=f"po-{i}",
                    n=70_000 + i)
        self.w.link("Email.mentions_purchase_order", made, po)
        self.sent(made)

    def test_cardinality_who_mails_purchase_orders_is_purchasing(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    po = self.po(i)
                    self.po_mail(i, po, OURS, OURS_OID)
                    # the same message's copy in a second mailbox counts once
                    copy = mail(self.w, SENT, sender=OURS, to=(REP,), body="x",
                                subject=f"PO P-{900 + i}", mailbox=APPROVER_OID,
                                conversation=f"po-{i}", n=70_000 + i)
                    self.w.link("Email.mentions_purchase_order", copy, po)
                    self.sent(copy)
                # the excluded person mails five POs, another person one
                for i in range(n, n + 5):
                    self.po_mail(i, self.po(i), BOSS, EXCLUDED_OID)
                self.po_mail(n + 5, self.po(n + 5), APPROVER, APPROVER_OID)
                st = self.store()
                got = roster.derive(st, people(), "purchasing", at(0), at(0, 1))
                self.assertEqual(got["status"], "complete")
                self.assertEqual(got["members"], [OURS_OID] if n >= 3 else [])
                if n >= 3:
                    self.assertEqual(got["evidence"][OURS_OID]["sent"], n)
                self.assertNotIn(EXCLUDED_OID, json.dumps(got))
                self.assertEqual(got["excluded_dropped"], 1)
                self.assertNotIn("@", json.dumps(got))      # ids and counts, no addresses

    def test_exclusion_unavailable_derives_nothing(self):
        got = roster.derive(self.store(), people(unavailable=True), "purchasing", at(0),
                            at(0, 1))
        self.assertEqual((got["status"], got["members"]), ("blocked", []))
        with self.assertRaises(KeyError):
            roster.derive(self.store(), people(), "everyone", at(0), at(0, 1))


class CrossReference(World):
    def catalogue(self, number: str, supplier: str) -> str:
        w = self.w
        ours = w.obj("Product", {"code": "OUR-1", "erp_code": "OUR-1"}, binding="bc.items")
        portal = w.obj("Product", {"code": "OUR-1"}, binding="portal.products")
        w.link("Product.same_record_as", ours, portal)
        offer = w.obj("SupplierItem", {"supplier_item_number": number},
                      markings='["finance"]', binding="portal.app_supplier_product")
        w.link("SupplierItem.offered_by", offer, supplier)
        w.link("SupplierItem.for_item", offer, portal)
        po = self.po(1)
        line = w.obj("PurchaseOrderLine", {"ordered_quantity": "40", "item_code": "OUR-1"})
        w.link(ap.PO_LINE_OF, line, po)
        w.link(ap.PO_LINE_ITEM, line, ours)
        return po

    def test_a_supplier_item_number_maps_its_eta_mail_to_the_open_po(self):
        po = self.catalogue("VX-4471-B", self.supplier)
        made = self.supplier_mail(1, "Item VX-4471-B will ship on Nov 20.")
        self.store()
        summary = self.cycle([made])
        self.assertEqual(summary["prepared"], {"supplier_eta": 1})
        item = self.queue().items()[0]
        work = item["body"]["prepared"]
        self.assertEqual(work["purchase_order"]["object_id"], po)
        self.assertEqual(work["matched_by_supplier_item"][0]["supplier_number"], "VX-4471-B")
        # the cross-reference is finance marked: a reader without finance sees nothing
        self.assertIn("finance", item["markings"])
        self.assertEqual(ap.visible(self.queue(), reader=READER, may_read=everyone, held=[],
                                    decide=opens_all), [])
        self.assertEqual(len(ap.visible(self.queue(), reader=READER, may_read=everyone,
                                        held=["finance"], decide=opens_all)), 1)
        self.assertEqual(ap.visible(self.queue(), reader=READER, may_read=lambda a: False,
                                    held=["finance"], decide=opens_all), [])

    def test_another_suppliers_number_maps_nothing(self):
        other = self.w.obj("Supplier", {"name": "Other supplier"})
        self.catalogue("VX-4471-B", other)
        made = self.supplier_mail(1, "Item VX-4471-B will ship on Nov 20.")
        self.store()
        summary = self.cycle([made])
        self.assertEqual(summary["prepared"], {})
        self.assertEqual(summary["skipped"].get("supplier_eta:no_single_purchase_order_named"),
                         1)


class Replay(World):
    def scene(self, n: int, replied) -> None:
        for i in range(n):
            po = self.po(i)
            self.supplier_mail(i, f"PO P-{900 + i} will ship on Nov 20.", po=po)
            if replied(i):
                self.reply(i)

    def replay(self, *, role: str = f"person:{OURS}", end=None, probe=lambda: 64.0,
            guard=None):
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        try:
            return rp.replay(st, people() if guard is None else guard, role=role,
                             out=self.dir / "out", since=DAY, until=DAY + timedelta(days=1),
                             roles={}, config=rp.load_config(), environ={},
                             end=end or DAY + timedelta(days=10), probe=probe, floor_gb=9.0)
        finally:
            st.close()

    def test_cardinality_scored_per_workflow_resumable_and_never_doubled(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.scene(n, lambda i: i % 2 == 0)
                self.store()
                first = self.replay()
                flow = first["workflows"]["supplier_eta_receiving"]
                self.assertEqual(first["status"], "complete")
                self.assertEqual((flow["prepared"], flow["judged"], flow["matched"]),
                                 (n, n, (n + 1) // 2))
                # the person's replies are the actions; each anticipated once
                self.assertEqual(sum(v["anticipated"] for v in first["workflows"].values()),
                                 (n + 1) // 2)
                again = self.replay()
                self.assertEqual(again["workflows"], first["workflows"])
                q = ap.Queue(self.dir / "out" / f"person_{OURS.split('@')[0]}_synth_tenant_"
                             "example" / ap.QUEUE_FILE)
                try:
                    self.assertEqual(len(q.items(None, ("open",))), n)
                finally:
                    q.close()

    def test_a_reply_after_the_horizon_or_by_someone_else_is_no_match(self):
        self.scene(2, lambda i: False)
        self.reply(0, day=4)                                  # after 72 h
        self.reply(1, sender=APPROVER, box=APPROVER_OID)      # not the owner
        self.store()
        flow = self.replay()["workflows"]["supplier_eta_receiving"]
        self.assertEqual((flow["judged"], flow["matched"]), (2, 0))

    def test_an_item_whose_horizon_passes_the_data_is_unjudged(self):
        self.scene(3, lambda i: True)
        self.store()
        flow = self.replay(end=DAY + timedelta(days=1))["workflows"]["supplier_eta_receiving"]
        self.assertEqual((flow["prepared"], flow["judged"], flow["unjudged"]), (3, 0, 3))
        self.assertIsNone(flow["precision"])

    def test_waits_for_memory_then_stops_resumably(self):
        self.scene(2, lambda i: True)
        self.store()
        out = self.replay(probe=lambda: 4.0)
        self.assertEqual((out["status"], out["days_done"]), ("waiting_for_memory", 0))
        self.assertEqual(self.replay()["days_done"], 1)

    def test_exclusion_fails_closed_and_an_excluded_person_has_no_replay(self):
        self.scene(2, lambda i: True)
        self.store()
        self.assertEqual(self.replay(guard=people(unavailable=True))["status"], "blocked")
        self.assertEqual(self.replay(role=f"person:{BOSS}")["status"], "no_members")

    def test_pay_and_hr_mail_makes_no_item(self):
        po = self.po(1)
        self.supplier_mail(1, "Attached is your pay stub. Gross pay 2,310.00 Net pay "
                              "1,804.22, CPP and EI deductions. PO P-901 ships Nov 20.", po=po)
        self.store()
        out = self.replay()
        self.assertEqual(out["prepared_by_autopilot"], {})
        self.assertEqual(out["skipped"].get("pay_or_hr"), 1)

    def test_nothing_is_sent_or_written_and_the_store_is_unchanged(self):
        self.scene(3, lambda i: True)
        self.store()
        before = (self.dir / "s.sqlite").read_bytes()
        out = self.replay()
        self.assertEqual((self.dir / "s.sqlite").read_bytes(), before)
        days = rp.Progress(self.dir / "out" / Path(next((self.dir / "out").glob("person_*"))
                                                   .name) / "progress.sqlite")
        try:
            self.assertTrue(all(s["sent"] == 0 and s["writes"] == 0 for s in days.summaries()))
        finally:
            days.close()
        self.assertNotIn("Nov 20", (self.dir / "out" / ap.GATE_FILE).read_text())
        self.assertEqual(out["actions"], 3)


class Gate(unittest.TestCase):
    def gate(self, tmp: Path, body) -> tuple:
        path = tmp / ap.GATE_FILE
        if body is not None:
            path.write_text(body if isinstance(body, str) else json.dumps(body))
        return ap.gate_from({}, tmp)

    def test_on_only_at_ninety_percent_of_fifteen_or_more_per_workflow(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            role = lambda j, m: {"schema": ap.GATE_SCHEMA, "roles": {"purchasing": {  # noqa: E731
                "members": [OURS_OID], "workflows": {"supplier_eta_receiving":
                                                     {"judged": j, "matched": m}}}}}
            for body, on in ((None, False), ("not json", False),
                             ({"schema": "other", "roles": {}}, False),
                             (role(14, 14), False), (role(20, 17), False),
                             (role(20, 21), False), (role(15, 14), True),
                             (role(30, 27), True)):
                with self.subTest(body=body):
                    allowed, passed = self.gate(tmp, body)
                    self.assertEqual(allowed(OURS_OID, "supplier_eta_receiving"), on)
                    self.assertFalse(allowed(OURS_OID, "quote_pricing"))
                    self.assertFalse(allowed(APPROVER_OID, "supplier_eta_receiving"))
                    self.assertEqual(bool(passed), on)
                    (tmp / ap.GATE_FILE).unlink(missing_ok=True)

    def test_live_follow_is_off_without_a_passed_workflow(self):
        import contextlib
        import io
        import tempfile
        from operating.tests.test_proactive_engine import World as Store
        with tempfile.TemporaryDirectory() as d:
            store = Store().build(Path(d) / "s.sqlite")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ap.main(["follow", "--store", str(store),
                                "--queue", str(Path(d) / "q" / ap.QUEUE_FILE)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["reason"], "no_workflow_passed_replay")


class Config(unittest.TestCase):
    def test_unknown_provider_plugin_or_workflow_refuses(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for bad in ({"providers": ["mail_sent", "screen_scrape"]},
                        {"plugins": ["os:system"]}, {"plugins": ["operating.autopilot:nope"]},
                        {"horizon_hours": 0}):
                raw = json.loads(rp.CONFIG_FILE.read_text())
                raw.update(bad)
                path = Path(d) / "c.json"
                path.write_text(json.dumps(raw))
                with self.subTest(bad=bad), self.assertRaises(rp.ReplayInvalid):
                    rp.load_config(path)
            raw = json.loads(wf.FILE.read_text())
            raw["autopilot_workflow"]["customer_po"] = "made_up"
            path = Path(d) / "w.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(wf.WorkflowsInvalid):
                wf.load(str(path))

    def test_every_work_graph_workflow_has_a_name(self):
        for subject, side, want in (("Remittance advice", "customer", "cash_remittance"),
                                    ("Invoice 4410", "supplier", "ap_supplier_invoice"),
                                    ("Sample for the new brief", "customer",
                                     "rnd_sample_project"),
                                    ("Label artwork proof v3", "internal", "label_artwork"),
                                    ("COA for lot 22", "customer", "coa_cert_doc_request"),
                                    ("New purchase order 4512", "customer",
                                     "customer_order_intake"),
                                    ("Quote request", "customer", "quote_pricing"),
                                    ("Weekly report", "internal", "reporting"),
                                    ("Hello", "internal", "other_internal")):
            with self.subTest(subject=subject):
                self.assertEqual(wf.classify(subject, "", side), want)


if __name__ == "__main__":
    unittest.main()
