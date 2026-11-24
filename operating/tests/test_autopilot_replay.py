"""The purchasing roster, item cross-references, the per-role replay harness and the live
gate (``operating.autopilot_roster``, ``item_xref``, ``autopilot_replay``,
``autopilot.gate_from``).

SYNTHETIC fixtures only; evidence label: isolated-fixture.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock

from operating import autopilot as ap
from operating import autopilot_engine
from operating import autopilot_events
from operating import autopilot_queue
from operating import autopilot_reading
from operating import autopilot_replay as rp
from operating import autopilot_roster as roster
from operating import proactive_engine as pe
from operating import workflows as wf
from operating.tests.test_autopilot import (NOW, OTHER, READER, SENT, Fixture, everyone,
                                            opens_all)
from operating.tests.test_proactive_engine import EXCLUDED_OID, OURS_OID
from operating.tests.test_proactive_threads import (APPROVER, APPROVER_OID, BOSS, OURS, OUTSIDE,
                                                    mail, people)

REP = "rep@synth-supplier.example"
SHIPPED_REGISTRY = ap.REGISTRY_FILE
DAY = datetime(SENT.year, SENT.month, SENT.day, tzinfo=timezone.utc)


def at(hour: int, day: int = 0) -> str:
    return ap._iso(DAY + timedelta(days=day, hours=hour))


class StubAsks:
    """The chat ask model's stand-in (``chat_ask_model.Classify``): "yes" for a text in
    ``yes``, "no" for one in ``no`` or (``no`` empty) any other, each batch kept.
    ``fail``: "raise" (an error), "none" (nothing parsed), "short" (a verdict missing)."""

    def __init__(self, yes=(), no=None, fail=None):
        self.yes, self.no, self.fail = set(yes), None if no is None else set(no), fail
        self.batches: list[list[str]] = []
        self.sent: list[list[dict]] = []

    def __call__(self, batch):
        self.sent.append([dict(m) for m in batch])
        self.batches.append([m["text"] for m in batch])
        if self.fail == "raise":
            raise RuntimeError("model down")
        if self.fail == "none":
            return [None] * len(batch)
        out = [(True, "ask") if m["text"] in self.yes else
               (False, "note") if self.no is None or m["text"] in self.no else None
               for m in batch]
        return out[:-1] if self.fail == "short" else out


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
        self.identified.append(("petra_po", f"P-{900 + i}", po, "po_number"))
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

    def test_who_mails_sales_orders_is_customer_service_and_purchasing_stays_apart(self):
        for i in range(4):
            order = self.w.obj("SalesOrder", {"order_number": f"SO-{i}"})
            made = mail(self.w, SENT, sender=APPROVER, to=(REP,), body=f"Order SO-{i}",
                        subject=f"SO-{i}", mailbox=APPROVER_OID, conversation=f"so-{i}",
                        n=71_000 + i)
            self.w.link("Email.mentions_sales_order", made, order)
            self.sent(made)
        st = self.store()
        got = roster.derive(st, people(), "customer_service", at(0), at(0, 1))
        self.assertEqual((got["status"], got["members"]), ("complete", [APPROVER_OID]))
        self.assertEqual(roster.derive(st, people(), "purchasing", at(0), at(0, 1))["members"],
                         [])

    def test_cardinality_receivables_is_who_mails_invoices_not_who_mails_orders(self):
        # every invoice the store links to a mail is also named by its order's number
        # (reproduced: both rosters came out the same people)
        for n in (3, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    order = self.w.obj("SalesOrder", {"order_number": f"SO-{i}"})
                    invoice = self.w.obj("SalesInvoice", {"invoice_number": f"INV-{i}"})
                    for k, (sender, box, subject, body) in enumerate((
                            (APPROVER, APPROVER_OID, f"SO-{i}",
                             f"Order SO-{i} confirmed, ships Friday."),
                            (OURS, OURS_OID, f"Invoice INV-{i} for order SO-{i}",
                             f"Please find attached invoice INV-{i}, payment due in 30 days."))):
                        made = mail(self.w, SENT, sender=sender, to=(REP,), body=body,
                                    subject=subject, mailbox=box, conversation=f"so-{i}-{k}",
                                    n=72_000 + 2 * i + k)
                        self.w.link("Email.mentions_sales_order", made, order)
                        self.w.link("Email.mentions_sales_invoice", made, invoice)
                        self.sent(made)
                st = self.store()
                cs = roster.derive(st, people(), "customer_service", at(0), at(0, 1))
                ar = roster.derive(st, people(), "receivables", at(0), at(0, 1))
                self.assertEqual(cs["members"], [APPROVER_OID])
                self.assertEqual(ar["members"], [OURS_OID])
                self.assertEqual(ar["evidence"][OURS_OID]["sent"], 2 * n)  # invoice + order

    def test_cardinality_quality_is_who_writes_about_quality_most_of_the_time(self):
        def note(i: int, sender: str, box: str, subject: str) -> None:
            made = mail(self.w, SENT, sender=sender, to=(REP,), body=subject, subject=subject,
                        mailbox=box, conversation=f"{box}-{i}", n=73_000 + len(self.events))
            self.sent(made)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):          # one person: quality holds, in n conversations
                    note(i, OURS, OURS_OID, f"QC hold on batch {i}")
                # another writes about quality too (3 conversations), but mostly about
                # everything else (focus 3 / 20): never the role by volume alone
                for i in range(20):
                    note(i, APPROVER, APPROVER_OID,
                         f"QC hold on batch {i}" if i < 3 else f"Purchase order {i} quote")
                for i in range(5):          # the excluded person: never read, never named
                    note(i, BOSS, EXCLUDED_OID, f"QC hold on batch {i}")
                st = self.store()
                got = roster.derive(st, people(), "quality", at(0), at(0, 1))
                self.assertEqual(got["status"], "complete")
                self.assertEqual(got["members"], [OURS_OID] if n >= 3 else [])
                if n >= 3:
                    self.assertEqual(got["evidence"][OURS_OID]["score"], n)
                self.assertNotIn(EXCLUDED_OID, json.dumps(got))
                self.assertNotIn("@", json.dumps(got))
                self.assertEqual(roster.derive(st, people(unavailable=True), "quality", at(0),
                                               at(0, 1))["members"], [])

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
        w.link(autopilot_queue.PO_LINE_OF, line, po)
        w.link(autopilot_queue.PO_LINE_ITEM, line, ours)
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
        self.assertEqual(autopilot_reading.visible(self.queue(), reader=READER, may_read=everyone, held=[],
                                    decide=opens_all), [])
        self.assertEqual(len(autopilot_reading.visible(self.queue(), reader=READER, may_read=everyone,
                                        held=["finance"], decide=opens_all)), 1)
        self.assertEqual(autopilot_reading.visible(self.queue(), reader=READER, may_read=lambda a: False,
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
            guard=None, ask_judge=None):
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        try:
            return rp.replay(st, people() if guard is None else guard, role=role,
                             out=self.dir / "out", since=DAY, until=DAY + timedelta(days=1),
                             roles={}, config=rp.load_config(), environ={},
                             end=end or DAY + timedelta(days=10), probe=probe, floor_gb=9.0,
                             ask_judge=ask_judge)
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
        self.assertNotIn("Nov 20", (self.dir / "out" / autopilot_engine.GATE_FILE).read_text())
        self.assertEqual(out["actions"], 3)


class StoreSource(World):
    """``--source``: the replay reads the store it was asked for, or nothing."""
    scene, replay = Replay.scene, Replay.replay

    def run_on(self, source: str, out: str) -> tuple[dict, list]:
        st = ap._store(str(self.dir / "s.sqlite"), source)
        try:
            result = rp.replay(st, people(), role=f"person:{OURS}", out=self.dir / out,
                               since=DAY, until=DAY + timedelta(days=1), roles={},
                               config=rp.load_config(), environ={},
                               end=DAY + timedelta(days=10), probe=lambda: 64.0)
        finally:
            st.close()
        q = ap.Queue(next((self.dir / out).glob("person_*")) / ap.QUEUE_FILE)
        try:
            items = sorted((i["item_id"], i["owner"], i["autopilot"], i["state"], i["event_id"],
                            json.dumps(i["citations"], sort_keys=True))
                           for i in q.items(None, ("open", "done", "settled", "dismissed")))
        finally:
            q.close()
        return result, items

    def test_cardinality_postgres_scores_the_same_items_as_the_sqlite_generation(self):
        from operating.pg import ontology_load
        from operating.tests import pg_testdb
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.scene(n, lambda i: i % 2 == 0)
                self.store()
                name, dsn = pg_testdb.create_db()
                self.addCleanup(pg_testdb.drop_db, name)
                from operating.tests.pg_store_mode import MIGRATIONS
                import psycopg
                with psycopg.connect(dsn, autocommit=True) as pg:
                    for migration in MIGRATIONS:
                        pg.execute(migration.read_text(encoding="utf-8"))
                ontology_load.load(str(self.dir / "s.sqlite"), dsn, workers=1, replace=True,
                                   log=io.StringIO())
                with mock.patch.dict(os.environ, {"PLX_PG_DSN": dsn}):
                    os.environ.pop("PLX_ONTOLOGY_PG", None)
                    lite, lite_items = self.run_on("sqlite", "lite")
                    pg_run, pg_items = self.run_on("postgres", "pg")
                self.assertEqual((lite["source"]["store"], pg_run["source"]["store"]),
                                 ("sqlite", "postgres"))
                self.assertEqual(len(pg_items), n)
                self.assertEqual(pg_items, lite_items)
                self.assertEqual(pg_run["workflows"], lite["workflows"])
                flow = pg_run["workflows"]["supplier_eta_receiving"]
                self.assertEqual((flow["judged"], flow["matched"]), (n, (n + 1) // 2))

    def test_postgres_not_holding_the_generation_is_refused_never_read_as_sqlite(self):
        self.scene(2, lambda i: True)
        self.store()
        out = self.dir / "out"
        # no server at this socket: Postgres serves nothing, so the run reads nothing
        with mock.patch.dict(os.environ, {"PLX_PG_DSN": f"postgresql:///none?host={self.dir}"}):
            os.environ.pop("PLX_ONTOLOGY_PG", None)
            with mock.patch("sys.stdout", new_callable=io.StringIO) as said:
                code = rp.main(["--store", str(self.dir / "s.sqlite"), "--out", str(out),
                                "--role", f"person:{OURS}", "--source", "postgres",
                                "--mem-max-gb", "0"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(said.getvalue())["reason"], "store_source_mismatch")
        self.assertFalse(out.exists())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as said:
            code = rp.main(["--store", str(self.dir / "s.sqlite"), "--source", "sqlite",
                            "--print-data-end", "--until", ap._iso(DAY - timedelta(days=4)),
                            "--mem-max-gb", "0"])
        ends = json.loads(said.getvalue())
        self.assertEqual((code, ends["store"]), (0, "sqlite"))
        # the last mail is the 14:00 reply; a replay to four days before judges to its end
        self.assertEqual(ends["data_end"], at(14))
        self.assertLess(ends["judged_to"], ends["data_end"])
        with self.assertRaises(ValueError):
            ap._store(str(self.dir / "s.sqlite"), "postgresql")

    def test_cardinality_short_lived_reader_threads_hold_no_connection_after_they_end(self):
        from concurrent.futures import ThreadPoolExecutor
        self.scene(2, lambda i: True)
        st = self.store()
        for pools in (1, 10, 60):
            with self.subTest(pools=pools):
                for _ in range(pools):   # proactive_threads._read: a new pool per read
                    with ThreadPoolExecutor(4) as pool:
                        self.assertEqual(set(pool.map(lambda _: st.conn.execute(
                            "SELECT COUNT(*) FROM objects").fetchone()[0], range(8))),
                            {st.conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]})
                # the main thread's and at most one per thread alive at once
                self.assertLessEqual(len(st._conns), 5)

    def test_a_queue_begun_on_one_store_is_never_resumed_on_the_other(self):
        self.scene(2, lambda i: True)
        self.store()
        first = self.replay()
        self.assertEqual((first["status"], first["source"]["store"]), ("complete", "sqlite"))
        q = ap.Queue(next((self.dir / "out").glob("person_*")) / ap.QUEUE_FILE)
        try:
            with q.conn:
                q.set_meta("store", "postgres")
        finally:
            q.close()
        again = self.replay()
        self.assertEqual((again["status"], again["reason"]),
                         ("blocked", "queue_started_on_postgres"))


class Gate(unittest.TestCase):
    def gate(self, tmp: Path, body) -> tuple:
        path = tmp / autopilot_engine.GATE_FILE
        if body is not None:
            path.write_text(body if isinstance(body, str) else json.dumps(body))
        return ap.gate_from({}, tmp)

    def test_on_only_at_ninety_percent_of_fifteen_or_more_per_workflow(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            role = lambda j, m: {"schema": autopilot_engine.GATE_SCHEMA, "roles": {"purchasing": {  # noqa: E731
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
                    (tmp / autopilot_engine.GATE_FILE).unlink(missing_ok=True)

    def test_a_pass_in_one_role_stays_in_that_role(self):
        import tempfile
        flow = "supplier_eta_receiving"
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            passed, failed = {"judged": 20, "matched": 19}, {"judged": 20, "matched": 10}
            allowed, _ = self.gate(tmp, {"schema": autopilot_engine.GATE_SCHEMA, "roles": {
                "purchasing": {"members": [OURS_OID], "workflows": {flow: passed}},
                "quality": {"members": [OURS_OID], "workflows": {flow: failed}},
                "owner": {"members": [OURS_OID], "workflows": {}}}})
            self.assertTrue(allowed(OURS_OID, flow, {"purchasing"}))
            self.assertFalse(allowed(OURS_OID, flow, {"quality"}))      # failed there
            self.assertFalse(allowed(OURS_OID, flow, {"owner"}))        # never measured
            self.assertFalse(allowed(OURS_OID, flow, {"receiving"}))    # not a member
            self.assertFalse(allowed(OURS_OID, flow))   # their own item: a role failed it
            allowed, _ = self.gate(tmp, {"schema": autopilot_engine.GATE_SCHEMA, "roles": {
                "purchasing": {"members": [OURS_OID], "workflows": {flow: passed}},
                "owner": {"members": [OURS_OID], "workflows": {}}}})
            self.assertTrue(allowed(OURS_OID, flow))
            self.assertFalse(allowed(OURS_OID, flow, {"owner"}))
            # the registry's role-bound autopilots carry their role to the gate
            reg = {a["id"]: a for a in ap.load_registry()["autopilots"]}
            self.assertEqual(ap.item_roles(reg["qc_hold"]), {"quality"})
            self.assertEqual(ap.item_roles(reg["po_followup"]), {"purchasing"})
            self.assertEqual(ap.item_roles(reg["desk"]), {"owner"})
            self.assertEqual(ap.item_roles(reg["supplier_eta"]), frozenset())

    def test_follow_and_replay_into_a_queue_are_off_without_a_passed_workflow(self):
        import contextlib
        import io
        import tempfile
        from operating.tests.test_proactive_engine import World as Store
        for command in ("follow", "replay"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as d:
                store = Store().build(Path(d) / "s.sqlite")
                buf = io.StringIO()
                queue = Path(d) / "q" / ap.QUEUE_FILE
                with contextlib.redirect_stdout(buf):
                    code = ap.main([command, "--store", str(store), "--queue", str(queue)])
                self.assertEqual(code, 0)
                # live (follow): off until a person switched drafts on themselves; a
                # replay: off until a role's replay passed
                self.assertEqual(json.loads(buf.getvalue())["reason"],
                                 "nobody_switched_drafts_on" if command == "follow"
                                 else "no_workflow_passed_replay")
                q = ap.Queue(queue)
                try:
                    self.assertEqual(
                        q.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
                finally:
                    q.close()


class SwitchedOn(unittest.TestCase):
    """Owner walk 2026-11-17: 509 items prepared by role membership for 16 people, none of
    whom asked. The live prepare gate is the person's own drafts switch (or the owner's
    Draft), read from the write path's audit journal at each cycle; a replay pass alone
    prepares nothing."""

    def test_only_what_the_person_switched_on_themselves_is_prepared(self):
        import tempfile
        from datetime import datetime, timezone
        from operating import autopilot_write as aw
        from operating.card_actions import Journal
        now = datetime(2026, 11, 17, 20, tzinfo=timezone.utc)
        me, you, boss = "aaaaaaaa-0000-4000-8000-000000000001", \
            "aaaaaaaa-0000-4000-8000-000000000002", "aaaaaaaa-0000-4000-8000-000000000003"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "audit.jsonl"
            gate = autopilot_engine.SwitchGate(path, {boss: frozenset({"mc_question"})})
            # no journal: only the owner's Draft
            self.assertEqual(gate.passed, {boss: ["mc_question"]})
            self.assertFalse(gate(me, "teams_chat", ("customer_service",)))
            journal = Journal(path)
            aw.set_drafts(journal, me, "teams_chat", True, me, now)
            self.assertTrue(gate(me, "teams_chat"))                # same object: re-read
            self.assertFalse(gate(me, "teams_group") or gate(you, "teams_chat"))
            # nobody switches for anyone else, the owner included; an unknown workflow
            # is refused; a row written by someone else counts for nothing
            with self.assertRaises(PermissionError):
                aw.set_drafts(journal, you, "teams_chat", True, boss, now)
            with self.assertRaises(ValueError):
                aw.set_drafts(journal, you, "no_such_flow", True, you, now)
            with journal.locked() as held:
                held.append({"event": aw.DRAFTS_EVENT, "person": you, "workflow": "teams_chat",
                             "on": True, "at": "x", "by": boss})
            self.assertFalse(gate(you, "teams_chat"))
            # a pause stops it; off stops it; a person's live opt-in counts as on
            aw.set_paused(journal, me, True, me, now)
            self.assertFalse(gate(me, "teams_chat"))
            aw.set_paused(journal, me, False, me, now)
            aw.set_drafts(journal, me, "teams_chat", False, me, now)
            self.assertFalse(gate(me, "teams_chat"))
            with journal.locked() as held:
                held.append({"event": "opt_in", "person": you, "workflow": "quote_pricing",
                             "on": True, "at": "x", "by": you})
            self.assertTrue(gate(you, "quote_pricing"))
            # the person's own off wins over Draft; their own on brings it back
            aw.set_drafts(journal, boss, "mc_question", False, boss, now)
            self.assertFalse(gate(boss, "mc_question"))
            aw.set_drafts(journal, boss, "mc_question", True, boss, now)
            self.assertTrue(gate(boss, "mc_question"))
            # a broken journal: a person's own off cannot be read, so nothing is
            # prepared, Draft included (fail closed)
            path.write_text(path.read_text() + "{not json\n")
            self.assertEqual(gate.passed, {})
            self.assertFalse(gate(boss, "mc_question"))


class LiveReplay(Fixture):
    def test_replay_into_the_live_queue_prepares_only_gated_workflows(self):
        import contextlib
        import io
        from unittest import mock
        texts = ("Please send the COA for lot 9.", "Where is order SO-2? When will it ship?")
        ids = [self.message(50 + n, t) for n, t in enumerate(texts)]
        store = self.w.build(self.dir / "s.sqlite")
        flows = {wf.of_item("customer_request", {"subject": "PO", "text": t,
                                                 "side": "customer"}) for t in texts}

        def items(name: str, workflows) -> int:
            queue = self.dir / name / ap.QUEUE_FILE
            queue.parent.mkdir()
            (queue.parent / autopilot_engine.GATE_FILE).write_text(json.dumps(
                {"schema": autopilot_engine.GATE_SCHEMA, "roles": {"purchasing": {
                    "members": [OURS_OID],
                    "workflows": {f: {"judged": 20, "matched": 20} for f in workflows}}}}))
            with contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch.object(ap, "window_ids", return_value=ids), \
                    mock.patch.object(pe, "_people", return_value=people()):
                code = ap.main(["replay", "--store", str(store), "--queue", str(queue),
                                "--until", ap._iso(NOW)])
            self.assertEqual(code, 0)
            q = ap.Queue(queue)
            try:
                return q.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            finally:
                q.close()

        self.assertEqual(items("other", [sorted(wf.names() - flows)[0]]), 0)
        self.assertGreater(items("passed", flows), 0)


class Config(unittest.TestCase):
    def test_unknown_provider_plugin_or_workflow_refuses(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for bad in ({"providers": ["mail_sent", "screen_scrape"]},
                        {"plugins": ["os:system"]}, {"plugins": ["operating.autopilot:nope"]},
                        {"horizon_hours": 0}, {"horizon_hours": 24 * 14 + 1}):
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

    def test_the_answer_window_counts_working_days_only(self):
        """The window (``horizon_hours``, one setting, default 24) is Monday-to-Friday time on the business's calendar: a Friday afternoon's
        trigger answered on Tuesday matches, as a Monday's answered on Thursday does; a
        Saturday's window opens on Monday; the clock change of 2026-11-01 is kept."""
        h, utc = timedelta(hours=72), timezone.utc
        for at, want in (
                (datetime(2026, 10, 19, 13, tzinfo=utc), datetime(2026, 10, 22, 13, tzinfo=utc)),
                # Friday 16:00 EDT -> Wednesday 16:00 EST (the zone changed on the Sunday)
                (datetime(2026, 10, 30, 20, tzinfo=utc), datetime(2026, 11, 4, 21, tzinfo=utc)),
                # Saturday: Monday 00:00 EDT + 72 h
                (datetime(2026, 10, 24, 14, tzinfo=utc), datetime(2026, 10, 29, 4, tzinfo=utc))):
            with self.subTest(at=at):
                self.assertEqual(rp.answer_end(at, h), want)
        self.assertEqual(rp.load_config()["horizon"], timedelta(hours=24))
        friday = datetime(2026, 10, 23, 20, tzinfo=utc)          # 16:00 EDT
        events = {f"e{i}": {"event_type": "message.received", "object_id": f"m{i}",
                            "object_type": "TeamsChatMessage", "at": ap._iso(friday),
                            "conversation_id": f"c{i}"} for i in range(3)}
        items = [{"event_id": f"e{i}", "autopilot": "teams_reply", "owner": "p",
                  "created_at": ap._iso(friday), "body": {}} for i in range(3)]
        # Tuesday 10:00, Wednesday 15:00 (in the window), Wednesday 17:00 EDT (after it)
        acts = [{"person": "p", "kind": "teams_sent", "workflow": "teams_chat",
                 "conversation": f"c{i}", "at": ap._iso(at), "records": []}
                for i, at in enumerate((datetime(2026, 10, 27, 14, tzinfo=utc),
                                        datetime(2026, 10, 28, 19, tzinfo=utc),
                                        datetime(2026, 10, 28, 21, tzinfo=utc)))]
        args = dict(horizon=h, data_end=friday + timedelta(days=9),
                    since=friday - timedelta(days=1), until=friday + timedelta(days=1))
        working = rp.score(items, events, acts, **args)["teams_chat"]
        self.assertEqual((working["judged"], working["matched"]), (3, 2))
        # one working day (24 h): Friday 16:00 EDT closes Monday 16:00; no Tuesday answer counts
        day = rp.score(items, events, acts, **{**args, "horizon": timedelta(hours=24)})
        self.assertEqual((day["teams_chat"]["judged"], day["teams_chat"]["matched"]), (3, 0))
        # a window that closes after the data ends is unjudged, never a miss
        short = rp.score(items, events, acts,
                         **{**args, "data_end": datetime(2026, 10, 28, 12, tzinfo=utc)})
        self.assertEqual((short["teams_chat"]["judged"], short["teams_chat"]["unjudged"]),
                         (0, 3))

    def test_a_pooled_item_matches_when_any_of_its_roles_people_acted(self):
        """An item the role holds (``pool:<role>``) is answered by whichever of its people
        acts on its record in the window; someone outside the role never answers it."""
        utc = timezone.utc
        at = datetime(2026, 10, 19, 13, tzinfo=utc)
        events = {f"e{i}": {"event_type": "record.change", "object_id": f"PurchaseOrder:p{i}",
                            "object_type": "PurchaseOrder", "at": ap._iso(at)} for i in range(3)}
        items = [{"event_id": f"e{i}", "autopilot": "po_followup", "owner": "pool:purchasing",
                  "created_at": ap._iso(at), "body": {}} for i in range(3)]
        acts = [{"person": who, "kind": "mail_sent", "workflow": "purchase_order_to_supplier",
                 "conversation": f"x{i}", "at": ap._iso(at + timedelta(hours=2)),
                 "records": [f"PurchaseOrder:p{i}"]}
                for i, who in enumerate(("ana", "ben", "zed"))]
        args = dict(horizon=timedelta(hours=24), data_end=at + timedelta(days=5),
                    since=at - timedelta(days=1), until=at + timedelta(days=1))
        got = rp.score(items, events, acts, pools={"pool:purchasing": ["ana", "ben"]}, **args)
        flow = got["purchase_order_to_supplier"]
        self.assertEqual((flow["judged"], flow["matched"]), (3, 2))
        alone = rp.score(items, events, acts, **args)["purchase_order_to_supplier"]
        self.assertEqual(alone["matched"], 0)

    def test_cardinality_mail_past_its_mailboxs_capture_end_is_not_judged(self):
        """One mailbox's capture stopped (its access withdrawn) while the others went on:
        a mail item of that mailbox whose window closes after its last capture read is
        unjudged, answered or not (the owner's reply copies after it are unseen); the same
        items of a mailbox still captured are judged. ``source_ends`` reads each member's
        mailbox end from the capture; a mailbox it holds nothing of gets no bound."""
        utc = timezone.utc
        t0 = datetime(2026, 10, 20, 14, tzinfo=utc)                 # a Tuesday
        stop = t0 + timedelta(hours=6)

        class Capture:
            def mailbox_seen_to(self, box):
                return {"box-a": stop}.get(box)

        class Store:
            autopilot_capture = Capture()
        ends = rp.source_ends(Store(), ["BOX-A", "box-b"])
        self.assertEqual(ends, {"mail:box-a": stop})
        # the capture's reader: its mailbox id in any case, the last read of its messages
        import sqlite3
        import tempfile
        from operating import autopilot_work as aw
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "mail-capture").mkdir()
            db = sqlite3.connect(Path(d) / "mail-capture" / "capture.db")
            db.execute("CREATE TABLE mailboxes (mailbox_id TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE messages (mailbox_id TEXT, last_seen_at TEXT)")
            db.execute("INSERT INTO mailboxes VALUES ('Box-A')")
            db.executemany("INSERT INTO messages VALUES ('Box-A', ?)",
                           [(ap._iso(stop - timedelta(hours=2)),), (ap._iso(stop),)])
            db.commit()
            db.close()
            reader = aw.CaptureTexts(d)
            try:
                self.assertEqual(reader.mailbox_seen_to("box-a"), stop)
                self.assertEqual(reader.mailbox_seen_to("BOX-A"), stop)
                self.assertIsNone(reader.mailbox_seen_to("box-b"))
            finally:
                reader.close()
        for n in (1, 10, 60):
            with self.subTest(n=n):
                events, items, acts = {}, [], []
                for box in ("box-a", "box-b"):
                    for i in range(n):
                        eid = f"{box}-{i}"
                        events[eid] = {"event_type": "message.received", "object_id": eid,
                                       "object_type": "Email", "mailbox": box,
                                       "at": ap._iso(t0), "conversation_id": f"c-{eid}"}
                        items.append({"event_id": eid, "autopilot": "desk", "owner": box,
                                      "created_at": ap._iso(t0), "body": {}})
                        if i % 2 == 0:      # answered within the hour, before the stop
                            acts.append({"person": box, "kind": "mail_sent",
                                         "workflow": "other_internal",
                                         "conversation": f"c-{eid}", "records": [],
                                         "at": ap._iso(t0 + timedelta(hours=1))})
                args = dict(horizon=timedelta(hours=24), data_end=t0 + timedelta(days=9),
                            since=t0 - timedelta(days=1), until=t0 + timedelta(days=1))
                for bound, judged, matched in (({}, 2 * n, 2 * ((n + 1) // 2)),
                                               (ends, n, (n + 1) // 2)):
                    got = rp.score(items, events, acts, source_end=bound, **args)
                    flow = next(v for v in got.values() if v["prepared"])
                    self.assertEqual((flow["prepared"], flow["judged"], flow["unjudged"],
                                      flow["matched"]),
                                     (2 * n, judged, 2 * n - judged, matched))

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


class WaitingOnYou(World):
    """``reply_prep``: an inbound message in a thread the owner already writes in (their
    own earlier sent mail, kept as digests in the queue) prepares a reply item; scored on
    dev and held-out days apart, with each miss given its cause."""

    def thread(self, i: int, *, ask: str = "Thanks. Can you confirm the quantity for next "
               "week?", earlier: bool = True, day: int = 0, cc=()) -> str:
        if earlier:      # the owner wrote in this thread two days before the window
            made = mail(self.w, SENT - timedelta(days=2), sender=OURS, to=(REP,),
                        body="Here is the spec sheet.", subject=f"Spec {i}", mailbox=OURS_OID,
                        conversation=f"th-{i}", n=80_000 + i, hour=9)
            self.sent(made, 9, -2)
        made = mail(self.w, SENT + timedelta(days=day), sender=REP, to=(OURS,), cc=cc,
                    body=ask, subject=f"Re: Spec {i}", mailbox=OURS_OID,
                    conversation=f"th-{i}", n=81_000 + i, hour=10)
        return self.sent(made, 10, day)

    def answer(self, i: int, *, hour: int = 14, day: int = 0) -> str:
        made = mail(self.w, SENT + timedelta(days=day), sender=OURS, to=(REP,),
                    body="Confirmed.", subject=f"Re: Spec {i}", mailbox=OURS_OID,
                    conversation=f"th-{i}", n=82_000 + i + 1000 * day, hour=hour)
        return self.sent(made, hour, day)

    def replay(self, *, days: int = 1, held_out: float = 0.0):
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        try:
            return rp.replay(st, people(), role=f"person:{OURS}", out=self.dir / "out",
                             since=DAY, until=DAY + timedelta(days=days), roles={},
                             config=rp.load_config(), environ={},
                             end=DAY + timedelta(days=10), probe=lambda: 64.0,
                             held_out_days=held_out)
        finally:
            st.close()

    def queue_file(self) -> ap.Queue:
        q = ap.Queue(self.dir / "out" / f"person_{OURS.split('@')[0]}_synth_tenant_example"
                     / ap.QUEUE_FILE)
        self.addCleanup(q.close)
        return q

    def test_cardinality_a_reply_in_an_engaged_thread_is_prepared_once_and_scored(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    self.thread(i)
                    if i % 2 == 0:
                        self.answer(i)
                self.store()
                out = self.replay()
                flows = {f: v for f, v in out["workflows"].items() if v["prepared"]}
                self.assertEqual(sum(v["prepared"] for v in flows.values()), n)
                self.assertEqual(sum(v["judged"] for v in flows.values()), n)
                self.assertEqual(sum(v["matched"] for v in flows.values()), (n + 1) // 2)
                self.assertEqual(out["prepared_by_autopilot"], {"reply_prep": n})
                q = self.queue_file()
                self.assertEqual({i["autopilot"] for i in q.items(None)}, {"reply_prep"})
                # the history holds digests: no address, subject or conversation id
                dump = json.dumps([list(r) for r in q.conn.execute("SELECT * FROM sent")])
                self.assertNotIn("@", dump)
                self.assertNotIn("th-", dump)
                self.assertEqual(self.replay()["workflows"], out["workflows"])  # no double

    REQUEST = "Please coordinate the filling day with Oxana for these three batches."
    FYI = "The labels were printed and handed over to shipping this morning."

    def test_cardinality_a_mail_request_without_a_question_goes_to_the_ask_model(self):
        """Replayed mail: 40 of 41 replies no rule noticed answered mail the phrase rules read
        as asking nothing ("Please coordinate the day with her", "Checking in on this").
        Mail to the reader with no phrase ask is read by the ask model: its yes is prepared,
        its no is not; mail only copied to the reader never reaches the model; with no model
        nothing changes (the phrase rules' answer). Each mail read once; N=1/10/60."""
        from operating import chat_ask_model as cam
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                # the reader wrote to the sender before (a correspondent), not in these threads
                self.sent(mail(self.w, SENT - timedelta(days=5), sender=OURS, to=(REP,),
                               body="Here are the specs.", subject="Specs", mailbox=OURS_OID,
                               conversation="intro", n=79_000, hour=9), 9, -5)
                for i in range(n):
                    self.thread(i, ask=self.REQUEST if i % 2 == 0 else self.FYI, earlier=False)
                copied = mail(self.w, SENT, sender=REP, to=(OTHER,), cc=(OURS,),
                              body=self.REQUEST, subject="Batches", mailbox=OURS_OID,
                              conversation="copied", n=78_000, hour=10)
                self.sent(copied, 10)
                self.store()
                model = StubAsks(yes=(self.REQUEST,))
                st = pe._Store(self.dir / "s.sqlite", threads=1)
                try:
                    out = rp.replay(st, people(), role=f"person:{OURS}", out=self.dir / "out",
                                    since=DAY, until=DAY + timedelta(days=1), roles={},
                                    config=rp.load_config(), environ={},
                                    end=DAY + timedelta(days=10), probe=lambda: 64.0,
                                    ask_judge=cam.Judge(model, batch=25))
                finally:
                    st.close()
                self.assertEqual(out["prepared_by_autopilot"], {"reply_prep": (n + 1) // 2})
                sent = [m for b in model.sent for m in b]
                self.assertEqual(len(sent), n)          # each mail to the reader once
                self.assertTrue(all(m.get("email") and m.get("subject") for m in sent))
                self.assertFalse(any("@" in json.dumps(m) for m in sent))
                q = self.queue_file()
                self.assertFalse([i for i in q.items(None) if "copied" in json.dumps(i)])
                # no model: the phrase rules' answer, nothing prepared for these mails
                self.setUp()
                for i in range(n):
                    self.thread(i, ask=self.REQUEST, earlier=False)
                self.store()
                self.assertEqual(self.replay()["prepared_by_autopilot"], {})

    def correspondent(self) -> None:
        """The reader wrote to the supplier before (not in the window's threads)."""
        self.sent(mail(self.w, SENT - timedelta(days=5), sender=OURS, to=(REP,),
                       body="Here are the specs.", subject="Specs", mailbox=OURS_OID,
                       conversation="intro", n=79_000, hour=9), 9, -5)

    def mail_run(self, model):
        from operating import chat_ask_model as cam
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        try:
            return rp.replay(st, people(), role=f"person:{OURS}", out=self.dir / "out",
                             since=DAY, until=DAY + timedelta(days=1), roles={},
                             config=rp.load_config(), environ={},
                             end=DAY + timedelta(days=10), probe=lambda: 64.0,
                             ask_judge=cam.Judge(model, batch=25))
        finally:
            st.close()

    def test_screen_reads_every_participant_of_mail_and_teams(self):
        """The model screen covers each participant field, not only text and names: a mail's
        sender, To and Cc addresses, a chat's sender name, a meeting's attendees."""
        from operating import chat_ask_model as cam

        class Clear:
            def identity_clear(self, _who):
                return True
        base = {"cleared": True, "text": "Please coordinate the filling day.",
                "subject": "Batches", "sender": "rep@synth-supplier.example"}
        self.assertIsNone(cam.screened({**base, "channel": "mail"}, Clear(), OURS))
        for extra in ({"sender": "billing@freightcom.com"}, {"to": ["x@freightcom.ca"]},
                      {"sender_name": "Dana (FreightCom)"}):
            with self.subTest(extra=extra):      # Freightcom is read like any other party
                self.assertIsNone(cam.screened({**base, **extra}, Clear(), OURS))

    def test_a_failed_model_call_keeps_the_phrase_rules_answer_for_mail(self):
        """A timeout, an error or an unparsed reply never turns mail into an ask: the mail
        keeps the phrase rules' answer (nothing prepared for a request without a question
        form), is not cached, and is read again on a later cycle. A phrase-rule ask is read
        by the model too (it may be automated); with no verdict it stays prepared."""
        for fail in ("raise", "none", "short"):
            with self.subTest(fail=fail):
                self.setUp()
                self.correspondent()
                for i in range(3):
                    self.thread(i, ask=self.REQUEST, earlier=False)
                self.thread(9, ask="Thanks. Can you confirm the quantity for next week?",
                            earlier=False)
                self.store()
                model = StubAsks(yes=(self.REQUEST,), fail=fail)
                out = self.mail_run(model)
                got = out["prepared_by_autopilot"].get("reply_prep", 0)
                # "short": the batch's last verdict (the phrase-rule ask's) is missing: it
                # keeps the rules' answer; the three requests are the model's yes
                self.assertEqual(got, 1 + (3 if fail == "short" else 0))
                q = self.queue_file()
                cached = q.conn.execute("SELECT count(*) FROM chat_asks").fetchone()[0]
                self.assertEqual(cached, 3 if fail == "short" else 0)

    def own_again(self, i: int) -> None:
        """A second own message in thread ``i``, the day before the window."""
        made = mail(self.w, SENT - timedelta(days=1), sender=OURS, to=(REP,),
                    body="Revised sheet attached.", subject=f"Re: Spec {i}", mailbox=OURS_OID,
                    conversation=f"th-{i}", n=83_000 + i, hour=9)
        self.sent(made, 9, -1)

    def test_cardinality_an_ask_in_a_copied_thread_the_owner_keeps_answering(self):
        """``mail_thread_turn`` (a fallback ahead of ``reply_prep``): an ask with two or more
        people copied, in a thread the owner wrote in twice or more in 14 days, is its own
        workflow; one copied person or one own message stays ``reply_prep``; never both.
        Shipped off (held-out 68-74%): switched on here; off, every one is ``reply_prep``'s."""
        copied = ("ann@synth-supplier.example", "bob@synth-supplier.example")
        self.setUp()
        for i in range(3):
            self.thread(i, cc=copied)
            self.own_again(i)
        self.store()
        self.assertEqual(self.replay()["prepared_by_autopilot"], {"reply_prep": 3})
        shipped = ap.load_registry

        def switched_on(path=None):
            reg = shipped(path)
            for a in reg["autopilots"]:
                if a["id"] == "mail_thread_turn":
                    a.pop("enabled", None)
            return reg
        patch = mock.patch.object(ap, "load_registry", switched_on)
        patch.start()
        self.addCleanup(patch.stop)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    kind = i % 3          # 0: the turn; 1: one copied; 2: one own message
                    self.thread(i, cc=copied if kind != 1 else copied[:1])
                    if kind != 2:
                        self.own_again(i)
                    if i % 2 == 0:
                        self.answer(i)
                self.store()
                out = self.replay()
                turns = (n + 2) // 3
                self.assertEqual(out["prepared_by_autopilot"],
                                 {k: v for k, v in (("mail_thread_turn", turns),
                                                    ("reply_prep", n - turns)) if v})
                self.assertEqual(out["workflows"]["mail_thread"]["judged"], turns)
                self.assertEqual(out["workflows"]["mail_thread"]["matched"],
                                 len([i for i in range(n) if i % 3 == 0 and i % 2 == 0]))
                q = self.queue_file()
                per_event = [r[0] for r in q.conn.execute(
                    "SELECT COUNT(*) FROM items GROUP BY event_id")]
                self.assertEqual((len(per_event), set(per_event)), (n, {1}))
                self.assertEqual(self.replay()["workflows"], out["workflows"])  # no double

    def test_only_mail_written_before_the_message_engages_the_thread(self):
        self.thread(1, earlier=False, ask="Here is the updated spec.")  # no ask, no history
        self.answer(1, hour=14)                     # the reply comes later the same day
        self.thread(2, earlier=False, ask="Noted, thank you.")
        self.store()
        out = self.replay()
        self.assertEqual(out["prepared_by_autopilot"], {})
        # the reply is a real action no item anticipated: the trigger was seen, and no
        # autopilot covers it; the other thread needed nothing
        self.assertEqual(sum(c.get("workflow_unknown", 0)
                             for c in out["misses"]["by_workflow"].values()), 1)

    def test_a_closing_note_or_one_greeting_another_recipient_owes_no_reply(self):
        notes = {   # each in a thread the owner wrote in before (engaged)
            1: ("Noted - thanks! Team will review it and send it back!", ()),
            2: ("Friday at 12 works for us. I will send an invite. Thanks, Sam", ()),
            3: ("Awesome, thanks!! Kindest regards, Sam Rep, Sales, 555 0100 ext 9", ()),
            4: ("Hi Lead, please send the signed forms today.", (APPROVER,)),
            5: ("Thanks so much. We are close - can we make $19 work?", ()),   # an ask
            6: ("Hi Ops, please send the signed forms today.", (APPROVER,)),   # the owner
            7: ("Hi Le, the forms are attached.", ()),                 # names no recipient
            # an @mention of another recipient and never of the owner: theirs to answer
            8: ("@Lead Person can you please send the signed forms today?", (APPROVER,)),
            9: ("Hi team, @Lead can you check it? @Ops please confirm the date?",
                (APPROVER,)),                                          # the owner too
            10: ("Can you send it to @Le or me?", ()),                 # names no recipient
        }
        for i, (body, cc) in notes.items():
            self.thread(i, ask=body, cc=cc)
        self.store()
        self.replay()
        q = self.queue_file()
        got = {json.loads(q.conn.execute("SELECT body FROM events WHERE event_id = ?",
                                         (i["event_id"],)).fetchone()[0])["conversation_id"]
               for i in q.items(None)}
        self.assertEqual(got, {"th-5", "th-6", "th-7", "th-9", "th-10"})

    def test_a_colleague_the_owner_rarely_answers_by_mail_gets_no_reply_item(self):
        def history(sender: str, k: int) -> None:   # 4 messages a week before, unanswered
            for j in range(4):
                made = mail(self.w, SENT - timedelta(days=8 + j), sender=sender, to=(OURS,),
                            body="An update for you.", subject=f"Old {k}-{j}",
                            mailbox=OURS_OID, conversation=f"old-{k}-{j}",
                            n=85_000 + 10 * k + j, hour=9)
                self.sent(made, 9, -8 - j)

        def engaged(i: int, sender: str) -> None:   # the owner wrote; now they write back
            made = mail(self.w, SENT - timedelta(days=2), sender=OURS, to=(sender,),
                        body="Here is the spec sheet.", subject=f"Spec {i}", mailbox=OURS_OID,
                        conversation=f"th-{i}", n=86_000 + i, hour=9)
            self.sent(made, 9, -2)
            made = mail(self.w, SENT, sender=sender, to=(OURS,), body="Looks right to me.",
                        subject=f"Re: Spec {i}", mailbox=OURS_OID, conversation=f"th-{i}",
                        n=87_000 + i, hour=10)
            self.sent(made, 10)
        history(APPROVER, 1)
        history(REP, 2)
        engaged(1, APPROVER)        # internal, rarely answered: no item
        engaged(2, REP)             # outside, same history: still prepared
        self.store()
        self.replay()
        q = self.queue_file()
        got = {json.loads(q.conn.execute("SELECT body FROM events WHERE event_id = ?",
                                         (i["event_id"],)).fetchone()[0])["conversation_id"]
               for i in q.items(None)}
        self.assertEqual(got, {"th-2"})

    def test_a_colleague_with_no_history_still_gets_the_reply_item(self):
        made = mail(self.w, SENT - timedelta(days=2), sender=OURS, to=(APPROVER,),
                    body="Here is the spec sheet.", subject="Spec 1", mailbox=OURS_OID,
                    conversation="th-1", n=88_000, hour=9)
        self.sent(made, 9, -2)
        made = mail(self.w, SENT, sender=APPROVER, to=(OURS,), body="Looks right to me.",
                    subject="Re: Spec 1", mailbox=OURS_OID, conversation="th-1", n=88_001,
                    hour=10)
        self.sent(made, 10)
        self.store()
        self.replay()
        self.assertEqual(len(self.queue_file().items(None)), 1)

    def test_an_excluded_participant_still_stops_the_item(self):
        self.thread(1, cc=(BOSS,))
        self.answer(1)
        self.store()
        out = self.replay()
        self.assertEqual(out["prepared_by_autopilot"], {})
        self.assertEqual(out["skipped"].get("participants_not_cleared"), 1)
        self.assertEqual(sum(c.get("participant_not_cleared", 0)
                             for c in out["misses"]["by_workflow"].values()), 1)

    def test_cardinality_a_cold_ask_from_an_unknown_sender_makes_no_item(self):
        """An ask from a sender with no customer or supplier record, in a thread the owner
        never wrote in, to someone they never wrote to (offers, trials, newsletters: 1 of
        22 answered on fresh mail) prepares nothing; the same ask from someone the owner
        wrote to before, or from a sender on both a customer and a supplier record, still
        does, and without the rule the cold ask did. N=1/10/60."""
        ask = "Can you confirm the quantity for next week?"
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    known = f"known{i}@partner-{i}.example"
                    made = mail(self.w, SENT - timedelta(days=5), sender=OURS, to=(known,),
                                body="Here is the spec sheet.", subject=f"Spec {i}",
                                mailbox=OURS_OID, conversation=f"old-{i}", n=89_000 + i,
                                hour=9)
                    self.sent(made, 9, -5)
                    both = f"both{i}@two-sided-{i}.example"   # a customer and a supplier
                    self.w.obj("Customer", {"name": f"Two sided {i}", "email": both})
                    self.w.obj("Supplier", {"name": f"Two sided {i}", "rep_email": both})
                    for kind, sender in (("cold", f"sales{i}@cold-{i}.example"),
                                         ("known", known), ("both", both)):
                        made = mail(self.w, SENT, sender=sender, to=(OURS,), body=ask,
                                    subject=f"Quote {i}", mailbox=OURS_OID,
                                    conversation=f"{kind}-{i}",
                                    n={"cold": 90_000, "known": 91_000,
                                       "both": 92_000}[kind] + i, hour=10)
                        self.sent(made, 10)
                self.store()

                def run(registry):          # the owner's desk: the approver roster
                    shutil.rmtree(self.dir / "out", ignore_errors=True)
                    st = pe._Store(self.dir / "s.sqlite", threads=1)
                    try:
                        out = rp.replay(st, people(), role=f"person:{OURS}",
                                        out=self.dir / "out", since=DAY,
                                        until=DAY + timedelta(days=1),
                                        roles={"owner": [OURS_OID]}, config=rp.load_config(),
                                        registry=registry, environ={},
                                        end=DAY + timedelta(days=10), probe=lambda: 64.0)
                    finally:
                        st.close()
                    return out["prepared_by_autopilot"], self.prepared_in()
                shipped = ap.load_registry()
                # a sender on both a customer and a supplier record reads as side unknown,
                # yet is a known party: never dropped as cold
                self.assertEqual(run(shipped), ({"desk": 2 * n}, {"known": n, "both": n}))
                # the shipped desk rule without its cold-sender condition took both
                reg = json.loads(json.dumps(shipped))
                next(a for a in reg["autopilots"] if a["id"] == "desk")["when"].pop("any_of")
                self.assertEqual(run(reg), ({"desk": 3 * n},
                                            {"known": n, "cold": n, "both": n}))

    def test_cardinality_a_fresh_opt_in_reads_their_captured_sent_mail_once(self):
        """A person opted in today has an empty queue: whom they wrote to before comes from
        their own captured sent mail (60 days, read the first time the queue meets their
        mailbox), so an ask from someone they wrote to three weeks ago is prepared from the
        first cycle; marked read already (the old empty history) it was dropped as cold.
        The history is read once: a second cycle reads nothing again. N=1/10/60."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    known = f"known{i}@partner-{i}.example"
                    made = mail(self.w, SENT - timedelta(days=21), sender=OURS, to=(known,),
                                body="Here is the spec sheet.", subject=f"Spec {i}",
                                mailbox=OURS_OID, conversation=f"old-{i}", n=93_000 + i,
                                hour=9)
                    self.sent(made, 9, -21)
                    made = mail(self.w, SENT, sender=known, to=(OURS,),
                                body="Can you confirm the quantity for next week?",
                                subject=f"Quote {i}", mailbox=OURS_OID,
                                conversation=f"known-{i}", n=94_000 + i, hour=8 + i % 8)
                    self.sent(made, 8 + i % 8)      # under the hourly cap per owner
                self.store()

                def cycle(name, *, history_read):
                    root = self.dir / name
                    root.mkdir()
                    (root / autopilot_queue.ROLES_FILE).write_text(
                        json.dumps({"roles": {"owner": [OURS_OID]}}))
                    q = ap.Queue(root / ap.QUEUE_FILE)
                    self.addCleanup(q.close)
                    if history_read:
                        with q.conn:
                            q.set_meta(f"sent_seeded:{OURS_OID.lower()}", {"days": 0})
                    st = pe._Store(self.dir / "s.sqlite", threads=1)
                    try:
                        reg = ap.load_registry()
                        ids = ap.window_ids(st, reg, ap._iso(DAY), ap._iso(DAY + timedelta(days=1)))
                        out = [ap.run_cycle(st, q, ids, people=people(), registry=reg,
                                            environ={}, now=DAY + timedelta(days=1),
                                            as_of=DAY.date(), settle_items=False,
                                            rate_by_event_hour=True)
                               for _ in range(2)]
                    finally:
                        st.close()
                    return q, out
                q, out = cycle("fresh", history_read=False)
                self.assertEqual(len(q.items(None)), n)
                self.assertEqual(out[0].get("sent_seeded"), n)
                self.assertNotIn("sent_seeded", out[1])            # read once
                q, _out = cycle("empty", history_read=True)
                self.assertEqual(q.items(None), [])

    def prepared_in(self) -> Counter:
        """The kinds of conversation (``<kind>-<i>``) the queue holds an item for."""
        q = ap.Queue(self.dir / "out" / f"person_{OURS.split('@')[0]}_synth_tenant_example"
                     / ap.QUEUE_FILE)
        try:
            return Counter(json.loads(q.conn.execute(
                "SELECT body FROM events WHERE event_id = ?", (i["event_id"],)
            ).fetchone()[0])["conversation_id"].split("-")[0] for i in q.items(None))
        finally:
            q.close()

    def test_a_mail_the_person_starts_is_self_initiated(self):
        made = mail(self.w, SENT, sender=OURS, to=(REP,), body="New topic.", subject="Hello",
                    mailbox=OURS_OID, conversation="fresh-1", n=83_000, hour=11)
        self.sent(made, 11)
        self.store()
        out = self.replay()
        self.assertEqual(sum(c.get("self_initiated", 0)
                             for c in out["misses"]["by_workflow"].values()), 1)

    def test_dev_and_held_out_days_are_scored_apart_and_the_gate_reads_held_out(self):
        for i in range(3):              # dev day: three answered
            self.thread(i)
            self.answer(i)
        for i in range(3, 5):           # held-out day: two, none answered
            self.thread(i, day=1)
        self.store()
        out = self.replay(days=2, held_out=1)
        total = lambda split, k: sum(v[k] for v in out["splits"][split]["workflows"].values())  # noqa: E731
        self.assertEqual((total("dev", "judged"), total("dev", "matched")), (3, 3))
        self.assertEqual((total("held_out", "judged"), total("held_out", "matched")), (2, 0))
        self.assertEqual(out["gate"]["on"], "held_out")
        gate = json.loads((self.dir / "out" / autopilot_engine.GATE_FILE).read_text())
        counts = gate["roles"][f"person:{OURS}"]["workflows"]
        self.assertEqual(sum(v["judged"] for v in counts.values()), 2)
        self.assertEqual(sum(v["matched"] for v in counts.values()), 0)

    def test_an_unknown_registry_flag_or_condition_refuses_the_registry(self):
        good = ap.load_registry()
        reply = next(a for a in good["autopilots"] if a["id"] == "reply_prep")
        for bad in ({"fallback": "yes"}, {"when": {"engaged_or_asks": True, "vip": True}},
                    {"when": {"quiet_sender": [3, 0.5]}},
                    {"when": {"quiet_sender": [3, 0.5, ["partner"]]}},
                    {"when": {"after_own_post": [60]}},
                    {"when": {"after_own_post": [60, 0.5]}},
                    {"when": {"cc_min": 0}}, {"when": {"cc_min": "2"}},
                    {"when": {"cc_min": True}}):
            with self.subTest(bad=bad):
                broken = json.loads(json.dumps(good))
                entry = next(a for a in broken["autopilots"] if a["id"] == "reply_prep")
                entry.update(bad)
                with self.assertRaises(ap.RegistryInvalid):
                    ap.validate_registry(broken)
        self.assertTrue(reply["fallback"])


class PastApprovals(World):
    """A replay reads today's approval records: one decided after the live feed first held
    the request (its time plus ``feed_lag_minutes``) was pending then, so it is prepared and
    the decision scores it, even on the same day; one decided sooner never could be. The
    shipped registry prepares it as ``job_approval``'s standing work (the rule-based
    recommendation; a replay never starts the job's model session)."""

    def approval(self, i: int, *, decided_hours: int) -> str:
        oid = self.w.obj("MCApproval", {
            "gate_id": f"g-{i}", "task_id": f"T-{i}", "status": "approved",
            "requested_by": APPROVER, "requested_at": at(10), "decided_by": OURS,
            "decided_at": at(10 + decided_hours), "reason": "Ship the change"})
        self.events.append(("MCApproval.requested", oid, at(10)))
        self.events.append(("MCApproval.decided", oid, at(10 + decided_hours)))
        return oid

    def test_cardinality_an_approval_decided_later_was_pending_then(self):
        from unittest import mock
        for n in (1, 10, 60):
            with self.subTest(n=n), mock.patch.object(ap, "_approvers",
                                                      lambda _d: [OURS_OID]):
                self.setUp()
                for i in range(n):
                    self.approval(i, decided_hours=20)      # the next morning
                self.approval(n, decided_hours=3)           # the same day, after the feed
                self.approval(n + 1, decided_hours=1)       # before the feed held it
                self.store()
                out = Replay.replay(self)
                flow = out["workflows"]["internal_approval"]
                self.assertEqual(out["prepared_by_autopilot"], {"job_approval": n + 1})
                self.assertEqual((flow["judged"], flow["matched"]), (n + 1, n + 1))
                # actions count inside the window: the two same-day decisions
                self.assertEqual((flow["actions"], flow["anticipated"]), (2, 1))

    def decided(self, project: str, by: str, days_before: float) -> None:
        old = self.w.obj("MCApproval", {"status": "approved", "decided_by": by,
                                        "requested_at": at(8, -days_before),
                                        "decided_at": at(9, -days_before)},
                         audience=f"mc:live/approvals/{project}")
        self.events.append(("MCApproval.decided", old, at(9, -days_before)))

    def test_cardinality_an_approval_goes_to_its_projects_recent_deciders(self):
        from unittest import mock
        for n in (1, 10, 60):
            with self.subTest(n=n), mock.patch.object(
                    ap, "_approvers", lambda _d: [OURS_OID, APPROVER_OID]):
                self.setUp()
                pending = {}
                for i, project in enumerate(("P-OURS", "P-BOTH", "P-STALE", "P-NONE",
                                             "P-STRANGER")):
                    pending[project] = [
                        self.w.obj("MCApproval", {
                            "gate_id": f"g-{i}-{k}", "status": "approved",
                            # asked by an agent no one in the directory is, outside our
                            # domains (the asker is left out: test_..._never_goes_back_to_its
                            # _asker...; one of our domains the directory does not hold is
                            # withheld: ap.approvals_cleared)
                            "requested_by": "agent@synth-agents.example",
                            "requested_at": at(10),
                            "decided_by": APPROVER, "decided_at": at(30),
                            "reason": "Ship it"}, audience=f"mc:live/approvals/{project}")
                        for k in range(n)]
                for k in range(n):
                    self.decided("P-OURS", OURS, 10 + k * 0.1)
                self.decided("P-BOTH", OURS, 29)
                self.decided("P-BOTH", APPROVER, 2)
                self.decided("P-STALE", APPROVER, 31)        # outside the 30 days
                self.decided("P-STRANGER", "nobody@synth-tenant.example", 3)  # not known
                st = self.store()
                q = ap.Queue(self.dir / "q" / ap.QUEUE_FILE)
                self.addCleanup(q.close)
                ap.run_cycle(st, q, [o for v in pending.values() for o in v], people=people(),
                             environ={}, now=DAY + timedelta(hours=12), settle_items=False)
                got = {}
                for item in q.items(None):
                    project = next(p for p in pending
                                   if f"mc:live/approvals/{p}" in item["audiences"])
                    got.setdefault(project, set()).add(item["owner"])
                both = {OURS_OID, APPROVER_OID}
                self.assertEqual(got, {"P-OURS": {OURS_OID}, "P-BOTH": both,
                                       "P-STALE": both, "P-NONE": both,
                                       "P-STRANGER": both})

    def test_cardinality_an_approval_never_goes_back_to_its_asker_when_another_decides(self):
        from unittest import mock
        for n in (1, 10, 60):
            with self.subTest(n=n), mock.patch.object(
                    ap, "_approvers", lambda _d: [OURS_OID, APPROVER_OID]):
                self.setUp()
                asked = {}
                # (project, who asked): both decide in P-BOTH; only we decide in P-OURS
                for i, (project, asker) in enumerate((
                        ("P-BOTH", OURS), ("P-BOTH", APPROVER), ("P-OURS", OURS),
                        ("P-NONE", OURS), ("P-NONE", "nobody@synth-agents.example"),
                        ("P-NONE", "nobody@synth-tenant.example"), ("P-NONE", ""))):
                    asked[(project, asker)] = [
                        self.w.obj("MCApproval", {
                            "gate_id": f"g-{i}-{k}", "status": "pending",
                            "requested_by": asker.upper(), "requested_at": at(10),
                            "reason": "Ship it"}, audience=f"mc:live/approvals/{project}")
                        for k in range(n)]
                self.decided("P-BOTH", OURS, 5)
                self.decided("P-BOTH", APPROVER, 2)
                self.decided("P-OURS", OURS, 3)
                st = self.store()
                q = ap.Queue(self.dir / "q" / ap.QUEUE_FILE)
                self.addCleanup(q.close)
                ap.run_cycle(st, q, [o for v in asked.values() for o in v], people=people(),
                             environ={}, now=DAY + timedelta(hours=12), settle_items=False)
                got: dict = {}
                for item in q.items(None):
                    oid = json.loads(q.conn.execute(
                        "SELECT body FROM events WHERE event_id = ?",
                        (item["event_id"],)).fetchone()[0])["object_id"]
                    key = next(k for k, v in asked.items() if oid in v)
                    got.setdefault(key, []).append(item["owner"])
                both = {OURS_OID, APPROVER_OID}
                self.assertEqual({k: set(v) for k, v in got.items()}, {
                    ("P-BOTH", OURS): {APPROVER_OID}, ("P-BOTH", APPROVER): {OURS_OID},
                    # the asker alone decided there: the other approvers
                    ("P-OURS", OURS): {APPROVER_OID},
                    # every approver: the asker is left out
                    ("P-NONE", OURS): {APPROVER_OID},
                    # an asker outside our domains: every approver; one of ours the
                    # directory does not hold, or none: no item (ap.approvals_cleared)
                    ("P-NONE", "nobody@synth-agents.example"): both})
                # no one else can decide: the asker keeps it
                with mock.patch.object(ap, "_approvers", lambda _d: [OURS_OID]):
                    q2 = ap.Queue(self.dir / "q2" / ap.QUEUE_FILE)
                    self.addCleanup(q2.close)
                    ap.run_cycle(st, q2, asked[("P-OURS", OURS)], people=people(), environ={},
                                 now=DAY + timedelta(hours=12), settle_items=False)
                    self.assertEqual({i["owner"] for i in q2.items(None)}, {OURS_OID})


class SenderHistory(World):
    """``answers_sender``: the owner's own record of answering a sender, from messages whose
    answer window closed before the new one (no later evidence)."""

    queue_file = WaitingOnYou.queue_file

    def registry(self):
        reg = json.loads(json.dumps(ap.load_registry()))
        entry = next(a for a in reg["autopilots"] if a["id"] == "reply_prep")
        entry["when"] = {"answers_sender": [2, 0.8], "intents_none": ["customer_po"],
                         "side_in": ["internal", "customer", "supplier", "unknown"]}
        return ap.validate_registry(reg)

    def history(self, i: int, sender: str, answered: bool, day: int) -> None:
        made = mail(self.w, SENT + timedelta(days=day), sender=sender, to=(OURS,),
                    body="Update attached.", subject=f"Old {i}", mailbox=OURS_OID,
                    conversation=f"old-{i}", n=84_000 + i, hour=10)
        self.sent(made, 10, day)
        if answered:
            made = mail(self.w, SENT + timedelta(days=day), sender=OURS, to=(sender,),
                        body="Thanks.", subject=f"Re: Old {i}", mailbox=OURS_OID,
                        conversation=f"old-{i}", n=85_000 + i, hour=12)
            self.sent(made, 12, day)

    def fresh(self, i: int, sender: str) -> None:
        made = mail(self.w, SENT, sender=sender, to=(OURS,), body="New topic, see attached.",
                    subject=f"New {i}", mailbox=OURS_OID, conversation=f"new-{i}",
                    n=86_000 + i, hour=10)
        self.sent(made, 10)

    def test_a_sender_the_owner_answers_prepares_and_one_they_ignore_does_not(self):
        answered, ignored, late = REP, "ignored@synth-supplier.example", \
            "late@synth-supplier.example"
        for k in range(3):
            self.history(k, answered, True, -5 + k)           # before the window: seeded
            self.history(10 + k, ignored, False, -5 + k)
        self.history(20, late, True, -1)      # answered, but its window is still open
        self.history(21, late, True, 0)
        self.fresh(1, answered)
        self.fresh(2, ignored)
        self.fresh(3, late)
        self.store()
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        try:
            out = rp.replay(st, people(), role=f"person:{OURS}", out=self.dir / "out",
                            since=DAY, until=DAY + timedelta(days=1), roles={},
                            config=rp.load_config(), environ={}, registry=self.registry(),
                            end=DAY + timedelta(days=10), probe=lambda: 64.0)
        finally:
            st.close()
        self.assertEqual(out["prepared_by_autopilot"], {"reply_prep": 1})
        item = self.queue_file().items(None)[0]
        self.assertEqual(item["body"]["happened"]["sender"], answered)
        bad = json.loads(json.dumps(ap.load_registry()))
        next(a for a in bad["autopilots"] if a["id"] == "reply_prep")["when"] = {
            "answers_sender": [0, 2]}
        with self.assertRaises(ap.RegistryInvalid):
            ap.validate_registry(bad)


class MCQuestions(World):
    """A question an MC agent asks a person on a task (the MC capture holds its text,
    options and the asker's recommendation): the addressee gets the prepared answer while
    it is still open when the feed (every ``feed_lag_minutes``) first holds it; their answer
    in MC is their action. Never the answer's text, never an MC write."""

    AGENT = "a3d74eb5-0000-4000-8000-00000000agnt"

    def setUp(self) -> None:
        super().setUp()
        self.capture_rows: list[tuple[str, dict]] = []
        self.q = 0
        # a capture that records when it read each version and when it first ran live
        # (None: an older layout without either)
        self.live_since: datetime | None = None
        self.held: dict[str, datetime] = {}
        self.known_to = DAY + timedelta(days=30)     # the capture's last write per unit

    def _event(self, task: str, task_oid: str, kind: str, actor: str, when: datetime,
               payload: dict) -> str:
        self.q += 1
        eid = f"{task}~{self.q}"
        oid = self.w.obj("MCTaskEvent", {"task_id": task, "kind": kind, "actor": actor,
                                         "at": ap._iso(when)},
                         audience="mc:live/task_events/PRJ", binding="mission_control.task_events")
        self.w.rows["objects"][-1]["native_key"] = json.dumps({"event_id": eid,
                                                               "scope": "live"})
        self.w.link("MCTaskEvent.event_of", oid, task_oid)
        self.events.append(("MCTaskEvent.occurred", oid, ap._iso(when)))
        self.capture_rows.append((f"live/task_events/{eid}", {
            "kind": kind, "actor": actor, "task_id": task, "event_id": eid, "ts": ap._iso(when),
            "payload": payload}))
        return oid

    def question(self, i: int, addressee: str, *, answered_after: timedelta | None,
                 answered_by: str | None = None) -> str:
        task = f"TASK-{i}"
        task_oid = self.w.obj("MCTask", {"task_id": task, "title": f"Synth task {i}",
                                         "stage": "in_progress"},
                              audience="mc:live/tasks/PRJ", binding="mission_control.tasks")
        asked = DAY + timedelta(hours=10)
        qid = f"q-{i}"
        answer = None
        if answered_after is not None:
            answer = {"answeredAt": ap._iso(asked + answered_after),
                      "answeredBy": answered_by or addressee, "commentId": "c",
                      "text": "SYNTH-ANSWER-TEXT"}
        self.capture_rows.append((f"live/tasks/{task}", {"id": task, "questions": [{
            "id": qid, "addressee": addressee, "askedAt": ap._iso(asked),
            "askedBy": self.AGENT, "text": f"Ship lot {i} with the old label or wait?",
            "options": ["Ship with the old label", "Wait for the new label"],
            "recommended": 1, "recommendationReason": "The new label is due tomorrow.",
            "context": "Synth context", "status": "answered" if answer else "open",
            **({"answer": answer} if answer else {})}]}))
        ev = self._event(task, task_oid, "question.asked", self.AGENT, asked,
                         {"addressee": addressee, "questionId": qid, "options": 2})
        if answer is not None:
            self._event(task, task_oid, "question.answered", answered_by or addressee,
                        asked + answered_after, {"questionId": qid, "choice": 1})
        return ev

    def capture(self) -> Path:
        import sqlite3
        state = self.dir / "state"
        (state / "mc-capture").mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(state / "mc-capture" / "capture.sqlite") as c:
            if self.live_since is None:
                c.execute("CREATE TABLE versions (kind TEXT, path TEXT, payload TEXT, "
                          "superseded_at TEXT)")
                for path, row in self.capture_rows:
                    c.execute("INSERT INTO versions VALUES ('mission_control', ?, ?, NULL)",
                              (path, json.dumps({"row": row})))
                return state
            c.execute("CREATE TABLE versions (kind TEXT, path TEXT, payload TEXT, "
                      "superseded_at TEXT, recorded_at TEXT)")
            c.execute("CREATE TABLE runs (kind TEXT, unit TEXT, started_at TEXT, "
                      "updated_at TEXT)")
            for unit in ("approvals", "task_events"):
                c.execute("INSERT INTO runs VALUES ('mission_control', ?, ?, ?)",
                          (f"mc:live/{unit}", ap._iso(self.live_since),
                           ap._iso(self.known_to)))
            for path, row in self.capture_rows:
                # the first sweep read what came before it; later the version of a record
                # it re-read, and before that the first one, superseded
                first = self.held.get(path, self.live_since)
                c.execute("INSERT INTO versions VALUES ('mission_control', ?, ?, ?, ?)",
                          (path, json.dumps({"row": row}), ap._iso(first + timedelta(days=1)),
                           ap._iso(first)))
                c.execute("INSERT INTO versions VALUES ('mission_control', ?, ?, NULL, ?)",
                          (path, json.dumps({"row": row}), ap._iso(first + timedelta(days=1))))
        return state

    def replayed(self, *, reader: bool = True) -> dict:
        from operating import autopilot_work as aw
        self.build(events=self.events)
        state = self.capture()
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        if reader:
            st.autopilot_mc = aw.MCQuestions(state)
        try:
            return rp.replay(st, people(), role=f"person:{OURS}", out=self.dir / "out",
                             since=DAY, until=DAY + timedelta(days=1), roles={},
                             config=rp.load_config(), environ={},
                             end=DAY + timedelta(days=10), floor_gb=0.0)
        finally:
            if reader:
                st.autopilot_mc.close()
            st.close()

    def scene(self, n: int) -> None:
        for i in range(n):
            # answered three hours on (after the feed held it), or in half an hour (before)
            self.question(i, "ops", answered_after=timedelta(hours=3) if i % 2 == 0
                          else timedelta(minutes=30))
        self.question(n, "boss", answered_after=None)          # the excluded person
        self.question(n + 1, "nobody", answered_after=None)    # no such person
        # an approval's decision as a task event: the approval provider counts it, not this
        task = self.w.obj("MCTask", {"task_id": "TASK-A"}, binding="mission_control.tasks")
        self._event("TASK-A", task, "approval.decided", OURS, DAY + timedelta(hours=11), {})
        # MC's audit marks beside a decision or an answer (the person decided their own
        # request, answered a question addressed to someone else): no second action
        self._event("TASK-A", task, "approval.owner_override", OURS, DAY + timedelta(hours=11),
                    {"gateId": "g", "decision": "approved", "reasonCode": "self_requested"})
        self._event("TASK-0", task, "question.owner_override", OURS, DAY + timedelta(hours=11),
                    {"questionId": "q-0", "addressee": "ops", "reasonCode": "not_addressee"})
        # agents working under the person's identity: an approval request and a question
        # naming their runtime, an update marked an agent's, an actor kind never seen
        at = DAY + timedelta(hours=12)
        self._event("TASK-A", task, "approval.requested", OURS, at, {"runtime": "synth-ide"})
        self._event("TASK-A", task, "question.asked", OURS, at, {"runtime": "synth-ide"})
        self._event("TASK-A", task, "task.updated", OURS, at, {"actorKind": "agent"})
        self._event("TASK-A", task, "task.moved", OURS, at, {"actorKind": "synth-service"})

    def test_cardinality_an_open_question_is_prepared_for_its_addressee_and_scored(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.scene(n)
                out = self.replayed()
                early = n // 2                       # answered inside the feed's lag
                self.assertEqual(out["prepared_by_autopilot"], {"mc_question": n - early})
                flow = out["workflows"]["mc_question"]
                self.assertEqual((flow["judged"], flow["matched"], flow["actions"],
                                  flow["anticipated"]), (n - early, n - early, n, n - early))
                self.assertNotIn("unclassified", out["workflows"])
                self.assertEqual(out["skipped"].get("mc_question:addressee_unknown"), 1)
                import re
                slug = re.sub(r"[^a-z0-9_]+", "_", f"person:{OURS}")
                q = ap.Queue(self.dir / "out" / slug / ap.QUEUE_FILE)
                self.addCleanup(q.close)
                items = q.items(None, ("open", "done", "settled", "dismissed"))
                self.assertEqual({i["owner"] for i in items}, {OURS_OID})
                for item in items:
                    draft = item["body"]["prepared"]["draft"]
                    self.assertEqual(draft["option"], "Wait for the new label")
                    self.assertNotIn("SYNTH-ANSWER-TEXT", json.dumps(item))
                    self.assertNotIn("object", item["body"]["prepared"])

    def home(self) -> None:
        """One step the person took in MC: the capture's actors name its home domain, the
        only place a bare MC name is looked up."""
        task = self.w.obj("MCTask", {"task_id": "TASK-H"}, binding="mission_control.tasks")
        self._event("TASK-H", task, "task.moved", OURS, DAY + timedelta(hours=9),
                    {"actorKind": "human"})

    def test_cardinality_a_live_captured_question_is_timed_by_its_first_read(self):
        """Once the capture reads MC live, a question is seen when its first version was
        captured, not at its time plus the configured lag: a fast poll's ten minutes make
        one answered in half an hour preparable; a paused feed's five hours make one
        answered in three hours not. One the first sweep backfilled keeps the lag."""
        asked = DAY + timedelta(hours=10)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.live_since = DAY + timedelta(hours=1)
                self.home()
                fast, paused = [], []
                for i in range(n):
                    fast.append(self.question(i, "ops", answered_after=timedelta(minutes=30)))
                    self.held[f"live/task_events/TASK-{i}~{self.q - 1}"] = \
                        asked + timedelta(minutes=10)
                for i in range(n, 3 * n):              # twice as many: counts tell them apart
                    paused.append(self.question(i, "ops", answered_after=timedelta(hours=3)))
                    self.held[f"live/task_events/TASK-{i}~{self.q - 1}"] = \
                        asked + timedelta(hours=5)
                out = self.replayed()
                self.assertEqual(out["prepared_by_autopilot"], {"mc_question": n})
                self.assertEqual(out["feed_seen"], {"capture": n})
                # every result names the scoring rules a comparison with an older run crosses
                self.assertIn("teams_burst_credit", {r["id"] for r in out["scoring_rules"]})
                self.assertIn("answer_window_working_days",
                              {r["id"] for r in out["scoring_rules"]})
                self.assertEqual(out["horizon_hours"], 24)
                flow = out["workflows"]["mc_question"]
                self.assertEqual((flow["judged"], flow["matched"]), (n, n))
                # the same questions before the capture ran live: the configured lag
                self.setUp()
                self.live_since = asked + timedelta(days=1)
                self.home()
                for i in range(n):
                    self.question(i, "ops", answered_after=timedelta(minutes=30))
                    self.question(n + i, "ops", answered_after=timedelta(hours=3))
                out = self.replayed()
                self.assertEqual(out["prepared_by_autopilot"], {"mc_question": n})
                self.assertEqual(out["feed_seen"], {"feed_lag": n})

    def test_cardinality_a_question_past_the_mc_captures_last_write_is_not_judged(self):
        """Mail goes on while the MC capture stopped writing (paused for a migration): a
        question whose 72 h are not all inside the capture is left unjudged, answered or
        not, never counted a miss; once the capture covers them, both are judged."""
        for n in (1, 10, 60):
            for stopped, judged, matched in ((True, 0, 0), (False, 2 * n, n)):
                with self.subTest(n=n, stopped=stopped):
                    self.setUp()
                    self.live_since = DAY
                    self.known_to = DAY + timedelta(days=1 if stopped else 5)
                    self.home()
                    for i in range(n):
                        self.question(i, "ops", answered_after=timedelta(hours=3))
                        self.question(n + i, "ops", answered_after=None)
                    out = self.replayed()
                    flow = out["workflows"]["mc_question"]
                    self.assertEqual(flow["prepared"], 2 * n)
                    self.assertEqual((flow["judged"], flow["matched"]), (judged, matched))
                    self.assertEqual(out["source_ends"], {"mc": ap._iso(self.known_to)})

    def test_without_the_mc_capture_no_question_is_prepared(self):
        self.question(0, "ops", answered_after=timedelta(hours=3))
        out = self.replayed(reader=False)
        self.assertEqual(out["prepared_by_autopilot"], {})
        self.assertEqual(out["skipped"].get("question_unreadable"), 1)
        # who took the answer cannot be read without the capture: no person's action
        self.assertEqual(out["workflows"], {})

    def test_only_a_step_the_person_took_is_their_task_action(self):
        task = self.w.obj("MCTask", {"task_id": "TASK-H"}, binding="mission_control.tasks")
        self._event("TASK-H", task, "task.moved", OURS, DAY + timedelta(hours=9),
                    {"actorKind": "human"})
        self._event("TASK-H", task, "task.updated", OURS, DAY + timedelta(hours=10),
                    {"runtime": "synth-ide"})
        self.assertEqual(self.replayed()["workflows"]["unclassified"]["actions"], 1)

    def test_a_bare_name_two_people_share_names_no_one(self):
        directory = {"ops@a.example": "1", "ops@b.example": "2", "lead@a.example": "3",
                     "lead@b.example": "3"}
        self.assertIsNone(ap.handle_person("ops", directory))
        # the source's own domain decides: one person there is the person
        self.assertEqual(ap.handle_person("ops", directory, ["b.example"]), "2")
        self.assertIsNone(ap.handle_person("ops", directory, []))
        self.assertIsNone(ap.handle_person("ops", directory, ["c.example"]))
        self.assertEqual(ap.handle_person("lead", directory), "3")
        self.assertIsNone(ap.handle_person("claude-code:ops@a.example", directory))
        self.assertIsNone(ap.handle_person("", directory))


class RoutineUpdate(World):
    """``routine_update``: a recurring update a person writes to colleagues (one subject
    series, the same weekday three weeks running) is drafted on its due day to the
    colleagues it usually goes to; a scheduled send, an unwritten notice, a series copying
    the excluded person and replies make none. Only that day's mail of the series scores it."""

    @staticmethod
    def word(i: int) -> str:            # digits are no part of a series: letters name it
        return "".join(chr(97 + int(d)) for d in str(i)) + "line"

    def series(self, i: int, *, weeks=(1, 2, 3), minute_free: bool = True, cc=(),
               body: str = "Hi team, the summary for this week is below.",
               subject: str = "Weekly line summary", today: int | None = 0) -> None:
        for k, w in enumerate(weeks):
            when = SENT - timedelta(days=7 * w)
            made = mail(self.w, when, sender=OURS, to=(APPROVER,), cc=cc, body=body,
                        subject=f"{subject} {self.word(i)} {when.isoformat()}", mailbox=OURS_OID,
                        conversation=f"wk-{i}-{w}", n=90_000 + i * 10 + k,
                        hour=(9 + k if minute_free else 9))
            self.sent(made, 9 + k if minute_free else 9, -7 * w)
        if today is not None:
            made = mail(self.w, SENT + timedelta(days=today), sender=OURS, to=(APPROVER,),
                        cc=cc, body=body, subject=f"{subject} {self.word(i)} {SENT.isoformat()}", mailbox=OURS_OID,
                        conversation=f"wk-{i}-0", n=90_000 + i * 10 + 9, hour=16)
            self.sent(made, 16, today)

    def replay(self):
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        try:
            return rp.replay(st, people(), role=f"person:{OURS}", out=self.dir / "out",
                             since=DAY, until=DAY + timedelta(days=1), roles={},
                             config=rp.load_config(), environ={},
                             end=DAY + timedelta(days=10), probe=lambda: 64.0,
                             held_out_days=0)
        finally:
            st.close()

    def routine_items(self):
        q = ap.Queue(self.dir / "out" / f"person_{OURS.split('@')[0]}_synth_tenant_example"
                     / ap.QUEUE_FILE)
        self.addCleanup(q.close)
        return [i for i in q.items(None) if i["autopilot"] == "routine_update"], q

    def test_cardinality_a_due_update_is_drafted_to_its_colleagues_and_scored_that_day(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    # every third is sent the next day instead: no match on its due day
                    self.series(i, today=0 if i % 3 else 1)
                self.store()
                out = self.replay()
                flow = out["workflows"]["routine_update"]
                self.assertEqual(out["prepared_by_autopilot"].get("routine_update"), n)
                self.assertEqual((flow["judged"], flow["matched"]),
                                 (n, sum(1 for i in range(n) if i % 3)))
                items, q = self.routine_items()
                self.assertEqual(len(items), n)
                draft = items[0]["body"]["prepared"]["draft"]
                self.assertEqual(draft["to"], [{"address": APPROVER}])
                self.assertEqual(items[0]["owner"], OURS_OID)
                self.assertTrue(items[0]["citations"])
                # the queue's series history keeps digests: no subject or address
                dump = json.dumps([list(r) for r in q.conn.execute("SELECT * FROM routine")])
                self.assertNotIn("@", dump)
                self.assertNotIn("summary", dump.lower())
                self.assertEqual(self.replay()["workflows"], out["workflows"])  # no double

    def test_scheduled_unwritten_excluded_and_reply_series_make_no_item(self):
        self.series(1, minute_free=False)                       # same minute: a schedule
        self.series(2, body="All checks passed.")               # no one greeted: a notice
        self.series(3, cc=(BOSS,))                              # the excluded person
        self.series(4, subject="RE: Weekly line summary")       # a reply
        self.series(5, weeks=(1, 2))                            # twice: no series yet
        self.series(6)                                          # the one due update
        self.store()
        out = self.replay()
        self.assertEqual(out["prepared_by_autopilot"].get("routine_update"), 1)
        items, _q = self.routine_items()
        subject = items[0]["body"]["prepared"]["draft"]["subject"]
        self.assertIn(self.word(6), subject)
        self.assertIn(SENT.isoformat(), subject)            # the last one's date moved to today

    def test_people_exclusion_unavailable_learns_no_series(self):
        from datetime import date
        from operating import autopilot_work as aw
        self.series(1)
        st = self.store()
        q = ap.Queue(self.dir / "q.sqlite")
        self.addCleanup(q.close)
        q.note_routine([{"owner": OURS_OID, "series": "s", "at": at(9, -7 * w),
                         "message": "Email:x", "recipients": [APPROVER], "greeted": True}
                        for w in (1, 2, 3)])
        due = aw.routine_due_events(st, q, ap.load_registry(), DAY.date(),
                                    people(unavailable=True))
        self.assertEqual(due, [])


class RdStatusUpdate(World):
    """``rd_status_update``: an R&D or sample project whose customer asked and has waited
    on us for three business days or more gets a status update drafted as a reply in its
    latest thread, owned by the person of ours who wrote in it; scored by that person
    writing in the thread. The thread is read as it stood at the day's start (a later
    answer is never read ahead); a thread we answered, one the excluded person is on and
    an unavailable exclusion make none."""

    def project(self, i: int, *, ours_last: bool = False, cc=(), answer_day: int | None = None,
                ask: str = "Could you send us an update on the sample?") -> str:
        customer = self.w.obj("Customer", {"name": f"Synth customer {i}"})
        conv = f"rd-{i}"
        steps = [(9, OUTSIDE, "Could you send a sample of the new blend?"),
                 (8, OURS, "Samples are on their way."), (7, OUTSIDE, ask)]
        if ours_last:
            steps.append((6, OURS, "Thanks, we will look into it."))
        for k, (back, who, body) in enumerate(steps):
            ours = who == OURS
            made = mail(self.w, SENT - timedelta(days=back), sender=who,
                        to=(OUTSIDE,) if ours else (OURS,), cc=cc, body=body,
                        subject=("Re: " if k else "") + f"Sample {i}", mailbox=OURS_OID,
                        conversation=conv, n=80_000 + i * 10 + k, hour=10)
            self.w.link("Email.sent_by_customer", made, customer)
            self.sent(made, 10, -back - 1)
        if answer_day is not None:
            made = mail(self.w, SENT + timedelta(days=answer_day), sender=OURS, to=(OUTSIDE,),
                        body="The sample ships Friday.", subject=f"Re: Sample {i}",
                        mailbox=OURS_OID, conversation=conv, n=80_000 + i * 10 + 9, hour=11)
            self.sent(made, 11, answer_day)
        return conv

    @staticmethod
    def measuring() -> dict:
        """The registry with the workflow on, as a replay that measures it runs (it ships
        off: ``off_reason``)."""
        reg = ap.load_registry()
        for spec in reg["autopilots"]:
            if spec["id"] == "rd_status_update":
                spec.pop("enabled", None)
        return reg

    def replay(self):
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        try:
            return rp.replay(st, people(), role=f"person:{OURS}", out=self.dir / "out",
                             since=DAY, until=DAY + timedelta(days=1), roles={},
                             config=rp.load_config(), environ={}, registry=self.measuring(),
                             end=DAY + timedelta(days=10), probe=lambda: 64.0,
                             held_out_days=0)
        finally:
            st.close()

    def rd_items(self):
        q = ap.Queue(self.dir / "out" / f"person_{OURS.split('@')[0]}_synth_tenant_example"
                     / ap.QUEUE_FILE)
        self.addCleanup(q.close)
        return [i for i in q.items(None, ("open", "done", "settled", "dismissed"))
                if i["autopilot"] == "rd_status_update"]

    def test_cardinality_a_waiting_project_gets_one_drafted_update_scored_by_the_thread(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    # every third is answered only after the horizon: no match
                    self.project(i, answer_day=1 if i % 3 else 5)
                self.store()
                out = self.replay()
                flow = out["workflows"]["rnd_status_update"]
                self.assertEqual(out["prepared_by_autopilot"].get("rd_status_update"), n)
                self.assertEqual((flow["judged"], flow["matched"]),
                                 (n, sum(1 for i in range(n) if i % 3)))
                self.assertFalse(flow["passes_gate"] and n < autopilot_engine.GATE_MIN_JUDGED)
                items = self.rd_items()
                self.assertEqual(len(items), n)
                item = items[0]
                self.assertEqual(item["owner"], OURS_OID)
                draft = item["body"]["prepared"]["draft"]
                self.assertEqual(draft["to"], [{"address": OUTSIDE}])
                self.assertTrue(draft["subject"].upper().startswith("RE: SAMPLE"))
                self.assertTrue(item["citations"])
                text = json.dumps([i["body"] for i in items])
                self.assertNotIn("new blend", text)              # never the thread's text
                self.assertNotIn("ships Friday", text)           # nor a later answer
                self.assertEqual(self.replay()["workflows"], out["workflows"])  # no double

    def test_answered_excluded_unasked_and_unavailable_make_none(self):
        from operating import autopilot_work as aw
        self.project(1, ours_last=True)                          # we answered: waits on them
        self.project(2, cc=(BOSS,))                              # the excluded person is on it
        self.project(3, ask="Thank you, all received.")          # closed by the customer
        self.project(4)                                          # the one waiting on us
        st = self.store()
        due = aw.rd_due_events(st, self.measuring(), DAY.date(), people())
        self.assertEqual([e["conversation_id"] for e in due], ["rd-4"])
        self.assertEqual(due[0]["fields"]["state"], "waiting_on_us")
        self.assertGreaterEqual(due[0]["fields"]["days"], 3)
        self.assertEqual(aw.rd_due_events(st, self.measuring(), DAY.date(),
                                          people(unavailable=True)), [])
        # it ships off: nothing is read for it
        self.assertEqual(aw.rd_due_events(st, ap.load_registry(), DAY.date(), people()), [])
        # two business days of waiting are not yet due
        self.assertEqual(aw.rd_due_events(st, self.measuring(), SENT - timedelta(days=5),
                                          people()), [])

    def test_the_projects_are_read_once_a_day(self):
        from datetime import date
        from operating import autopilot_work as aw
        self.project(1)
        st = self.store()
        q = ap.Queue(self.dir / "q.sqlite")
        self.addCleanup(q.close)
        calls = []
        real = aw.rd_due_events
        with mock.patch.object(aw, "rd_due_events",
                               side_effect=lambda *a, **k: calls.append(a[2]) or real(*a, **k)):
            for hours in (1, 2, 30):
                ap.run_cycle(st, q, [], people=people(), now=DAY + timedelta(hours=hours),
                             settle_items=False, registry=self.measuring())
        self.assertEqual(calls, [DAY.date(), DAY.date() + timedelta(days=1)])
        self.assertEqual(q.conn.execute("SELECT COUNT(*) FROM events WHERE json_extract(body, "
                                        "'$.event_type') = 'record.due'").fetchone()[0], 1)
