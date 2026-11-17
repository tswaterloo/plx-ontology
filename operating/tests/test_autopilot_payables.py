"""AP invoice match and coded entry prepared from the records, dry run
(``operating.autopilot_payables``, ``operating.payables_score``): the PO and receipts an
invoice bills with its 3-way variances, the entry coded as the supplier's earlier invoices
were, translated to the BC account through the crosswalk; a duplicate flagged, never
entered; finance-marked, nothing written. Synthetic records only (isolated-fixture)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from operating import autopilot as ap
from operating import autopilot_reading
from operating import autopilot_payables as pay
from operating import payables_score as score
from operating.tests.test_autopilot_replay import World
from operating.tests.test_proactive_engine import CARDINALITY_SECONDS, EXCLUDED_OID
from operating.tests.test_proactive_threads import APPROVER_OID, people

FIN = '["finance"]'
DAY = date(2026, 10, 21)
ACCT, OTHER = "5010-01", "6020-01"


class Payables(World):
    def setUp(self) -> None:
        super().setUp()
        self.batch = 0
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.xwalk = Path(tmp.name) / "gl-crosswalk.json"
        self.xwalk.write_text(json.dumps({
            "format": "plx-gl-crosswalk/v1", "source": {"file": "x.xlsx", "sha256": "ab"},
            "mapped": [{"sage_acctid": "501001", "sage_formatted": ACCT, "currency": "CAD",
                        "bc_acctid": "5010", "bc_name": "Synth materials"},
                       {"sage_acctid": "501002", "sage_formatted": "5010-02",
                        "currency": "USD", "bc_acctid": "5010", "bc_name": "Synth materials"}],
            "added_to_chart": [{"sage_acctid": "602001", "sage_formatted": OTHER,
                                "currency": "CAD"}],
            "new_on_chart": [], "not_ported": []}))
        env = {pay.CROSSWALK_ENV: str(self.xwalk)}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k) if v is None else
                                 os.environ.__setitem__(k, v) for k, v in old.items()])

    def entry(self, vendor: str, number: str, day: str, lines: list[tuple], *,
              po: str = "", total: str | None = None, tax: str = "0", due: str = "",
              kind: str = "1", currency: str = "CAD", by: str = "lead",
              included: bool = False) -> str:
        """One Sage AP invoice entry: ``lines`` (account, amount[, item, qty, unit])."""
        self.batch += 1
        net = sum(Decimal(ln[1]) for ln in lines)
        head = self.w.obj("RelSageApIbh", {
            "idvend": vendor, "idinvc": number, "dateinvc": day.replace("-", ""),
            "datedue": due.replace("-", ""), "codecurn": currency, "ponbr": po,
            "amtinvctot": total or str(net + Decimal(tax)), "amttaxtot": tax,
            "texttrx": kind, "cntbtch": str(self.batch), "cntitem": "1", "enteredby": by,
            "codetaxgrp": "ONT", "swtaxincl1": "1" if included else "0"},
            binding=pay.HEAD_BINDING, audience="api:sage.ap", markings=FIN)
        for n, ln in enumerate(lines, 1):
            account, amount, *item = ln
            values = {"cntbtch": str(self.batch), "cntitem": "1", "cntline": str(n),
                      "idglacct": account, "amtdist": amount}
            if item:
                values.update({"iditem": item[0], "qtyinvc": item[1], "unitmeas": item[2]})
            line = self.w.obj("RelSageApIbd", values, binding=pay.LINE_BINDING,
                              audience="api:sage.ap", markings=FIN)
            self.w.link("RelSageApIbd.invoice_entry", line, head)
        return head

    def history(self, vendor: str, account: str = ACCT, n: int = 3) -> None:
        for i in range(n):
            self.entry(vendor, f"H-{vendor}-{i}", f"2026-09-0{i + 1}", [(account, "10.00")],
                       due=f"2026-10-0{i + 1}", by="boss" if i == n - 1 else "lead")

    def purchase_order(self, number: str, lines: list[tuple], supplier: str | None = None,
                       received_on: str | None = None) -> str:
        po = self.w.obj("PurchaseOrder", {"sage_po_number": number, "currency": "CAD"},
                        binding="portal.sage_po_header")
        if supplier:
            self.w.link("PurchaseOrder.ordered_from", po, supplier)
        receipt = None
        if received_on:
            receipt = self.w.obj("Receipt", {"received_date": received_on,
                                             "sage_receipt_number": "RC-1"},
                                 binding="portal.sage_po_receipt_header")
            self.w.link("Receipt.receives", receipt, po)
        for code, ordered, received, price in lines:
            values = {"item_code": code, "ordered_quantity": (ordered, "kg"),
                      "received_quantity": ("0", "kg", "received")}
            if price:
                values["unit_price"] = price
            pl = self.w.obj("PurchaseOrderLine", values, binding="portal.sage_po_detail")
            self.w.link("PurchaseOrderLine.line_of", pl, po)
            product = self.w.obj("Product", {"code": code})
            self.w.link("PurchaseOrderLine.for_item", pl, product)
            if receipt and received:
                rl = self.w.obj("ReceiptLine", {"received_quantity": (received, "kg",
                                                                      "received")},
                                binding="portal.sage_po_receipt_detail")
                self.w.link("ReceiptLine.line_of", rl, receipt)
                self.w.link("ReceiptLine.for_item", rl, product)
        return po

    def vendor(self, code: str) -> str:
        v = self.w.obj("RelSageApVendor", {"vendorid": code}, binding="gen.sage.ap_vendor",
                       audience="api:sage.ap")
        self.w.link("RelSageApVendor.portal_supplier", v, self.supplier)
        return v

    def prep(self, st, oid, registry=None):
        reg = registry or self.registry()
        spec = next(s for s in reg["autopilots"] if s["id"] == pay.HANDLER)
        st.autopilot_people = people()          # as the cycle sets it
        event = next(e for e in pay.entry_events(st, reg, DAY, people(), force=True)
                     if e["object_id"] == oid)
        return ap._prepare(st, spec, event, DAY, (), 30)

    @staticmethod
    def registry():
        reg = ap.load_registry()
        for s in reg["autopilots"]:
            if s["id"] == pay.HANDLER:
                s["enabled"] = True
        return reg

    # ------------------------------------------------------------------ tests

    def test_coded_entry_as_the_vendors_earlier_invoices_with_bc_account(self):
        self.vendor("SYNV1")
        self.history("SYNV1")
        due = self.entry("SYNV1", "SYN-INV-77", "2026-10-21", [(ACCT, "100.00")],
                         tax="13.00", due="2026-11-20")
        st = self.store()
        got = self.prep(st, due)
        work = got["work"]
        entry = work["entry"]
        self.assertEqual([(ln["sage_account"], ln["amount"], ln["basis"])
                          for ln in entry["lines"]], [(ACCT, "100.00", "vendor_account")])
        self.assertEqual(entry["lines"][0]["bc_account"]["account"], "5010")
        self.assertEqual(entry["lines"][0]["bc_account"]["crosswalk"]["crosswalk_sha256"], "ab")
        self.assertEqual((entry["subtotal"], entry["tax"], entry["total"]),
                         ("100.00", "13.00", "113.00"))
        self.assertEqual(Decimal(entry["subtotal"]) + Decimal(entry["tax"]),
                         Decimal(entry["total"]))
        # terms from the latest earlier invoice (30 days)
        self.assertEqual((entry["terms_days"], entry["due_date"]), (30, "2026-11-20"))
        self.assertEqual(entry["tax_group"], "ONT")
        self.assertEqual(work["match"]["status"], "no_po")
        self.assertFalse(entry["on_hold"])
        self.assertIn("finance", got["markings"])
        self.assertTrue(got["citations"])
        self.assertTrue(work["dry_run"])
        self.assertNotIn("write", work)
        self.assertIsNone(autopilot_reading.write_spec({"body": {"prepared": work},
                                         "citations": got["citations"]}))
        # the latest earlier keyer is excluded: the one before them; never the excluded
        self.assertEqual(work["owner_hint"], APPROVER_OID)
        self.assertEqual(next(e for e in pay.entry_events(st, self.registry(), DAY, people())
                              if e["object_id"] == due)["owner_hint"], APPROVER_OID)
        self.assertIsNone(pay.keyer({}, pay._history(pay._entries(st), pay.received_from_entry(
            pay._entries(st)["by_oid"][due])), people(unavailable=True)))
        self.assertNotIn(EXCLUDED_OID, json.dumps(work))
        # scored against what was keyed: right
        entry_rec = pay._entries(st)["by_oid"][due]
        counts = score.judge(entry_rec, work)
        self.assertEqual((counts["prepared"], counts["exact"], counts["account_right"]),
                         (1, 1, 1))

    def test_line_not_told_exactly_leaves_no_entry(self):
        self.history("SYNV2", n=2)                      # too little history
        short = self.entry("SYNV2", "SYN-INV-1", "2026-10-21", [(ACCT, "5.00")])
        self.history("SYNV3")
        self.entry("SYNV3", "H-X", "2026-09-20", [(OTHER, "4.00")])   # latest differs
        varies = self.entry("SYNV3", "SYN-INV-2", "2026-10-21", [(ACCT, "5.00")])
        self.history("SYNV4")
        item = self.entry("SYNV4", "SYN-INV-3", "2026-10-21",
                          [(ACCT, "5.00", "SYN-ITEM", "2", "kg")])     # item never billed
        self.history("SYNV5")
        incl = self.entry("SYNV5", "SYN-INV-4", "2026-10-21", [(ACCT, "5.00")], included=True)
        self.history("SYNV6")
        over = self.entry("SYNV6", "SYN-INV-5", "2026-10-21",
                          [(ACCT, "9.00", "SYN-ITEM", "1", "kg")], total="5.00")
        credit = self.entry("SYNV6", "SYN-CN-6", "2026-10-21", [(ACCT, "5.00")], kind="3")
        self.history("SYNV7")
        usd = self.entry("SYNV7", "SYN-INV-7", "2026-10-21", [(ACCT, "5.00")],
                         currency="USD")                # no USD history
        st = self.store()
        for oid, why in ((short, "history_short"), (varies, "coding_varies"),
                         (item, "item_account_unknown"), (incl, "tax_included"),
                         (over, "lines_exceed_total"), (usd, "history_short")):
            with self.subTest(why=why):
                self.assertEqual(self.prep(st, oid), {"skip": why})
        events = {e["object_id"] for e in pay.entry_events(st, self.registry(), DAY,
                                                            force=True)}
        self.assertNotIn(credit, events)                # a credit note is not an invoice

    def test_duplicate_of_an_entered_invoice_is_flagged_not_entered(self):
        self.history("SYNV1")
        self.entry("SYNV1", "INV-0042517", "2026-10-02", [(ACCT, "10.00")])
        dup = self.entry("SYNV1", "42517", "2026-10-21", [(ACCT, "10.00")])
        # a series number is not the same document; another vendor's same number neither
        series = self.entry("SYNV1", "INV-0042518", "2026-10-21", [(ACCT, "10.00")])
        self.history("SYNV9")
        other = self.entry("SYNV9", "42517", "2026-10-21", [(ACCT, "10.00")])
        # a posted open document of an earlier batch carries the number
        self.history("SYNV8")
        self.w.obj("RelSageApOpenDocument", {"idvend": "SYNV8", "idinvc": "SYN-77-A",
                                             "cntbtch": "1", "cntitem": "9",
                                             "txttrxtype": "1"},
                   binding=pay.DOC_BINDING, audience="api:sage.ap", markings=FIN)
        posted = self.entry("SYNV8", "SYN-77-A", "2026-10-21", [(ACCT, "10.00")])
        # the invoice's own posting is not a duplicate of it
        self.history("SYNV10")
        own = self.entry("SYNV10", "SYN-88", "2026-10-21", [(ACCT, "10.00")])
        self.w.obj("RelSageApOpenDocument", {"idvend": "SYNV10", "idinvc": "SYN-88",
                                             "cntbtch": str(self.batch), "cntitem": "1",
                                             "txttrxtype": "1"},
                   binding=pay.DOC_BINDING, audience="api:sage.ap", markings=FIN)
        st = self.store()
        for oid in (dup, posted):
            work = self.prep(st, oid)["work"]
            self.assertIsNone(work["entry"])
            self.assertTrue(work["duplicate_of"])
            judged = score.judge(pay._entries(st)["by_oid"][oid], work)
            self.assertEqual((judged["flagged_duplicate"], judged["prepared"],
                              judged["exact"]), (1, 1, 0))     # a flag on a kept entry: a miss
        for oid in (series, other, own):
            self.assertIsNotNone(self.prep(st, oid)["work"]["entry"])

    def test_three_way_match_variances_per_line(self):
        self.vendor("SYNV1")
        # earlier: item A coded to ACCT three times, 4 kg of A billed on this PO already
        for i in range(3):
            self.entry("SYNV1", f"H-{i}", f"2026-09-0{i + 1}",
                       [(ACCT, "10.00", "SYN-A", "4" if i == 2 else "1", "kg"),
                        (OTHER, "2.00")], po="SYN-PO-1" if i == 2 else "SYN-PO-0",
                       due=f"2026-10-0{i + 1}")
            self.entry("SYNV1", f"HB-{i}", f"2026-09-1{i}",
                       [(OTHER, "3.00", "SYN-B", "1", "kg")], due=f"2026-10-1{i}")
        self.purchase_order("SYN-PO-1", [("SYN-A", "10", "8", "2.50"),
                                         ("SYN-B", "5", "5", "")],
                            supplier=self.supplier, received_on="2026-10-20")
        due = self.entry("SYNV1", "SYN-INV-9", "2026-10-21",
                         [(ACCT, "15.00", "SYN-A", "6", "kg"), (OTHER, "3.00", "SYN-B", "5",
                                                                  "kg"),
                          (OTHER, "1.00")], po="SYN-PO-1", tax="0", due="2026-11-20")
        st = self.store()
        work = self.prep(st, due)["work"]
        m = work["match"]
        self.assertEqual((m["status"], m["supplier"]), ("variance", "agrees"))
        a, b = m["lines"]
        self.assertEqual((a["ordered"], a["received"], a["billed_before"]), ("10", "8", "4"))
        self.assertEqual(a["received_basis"], "receipts dated by the invoice date")
        self.assertIn("2 not yet received", a["variance"])
        self.assertEqual(a["variance"], ["2 not yet received"])   # price agrees: 6 x 2.50
        self.assertEqual(b["variance"], [])
        self.assertIsNone(b["po_unit_price"])
        entry = work["entry"]
        self.assertTrue(entry["on_hold"])
        self.assertEqual(sorted((ln["sage_account"], ln["amount"], ln["basis"])
                                for ln in entry["lines"]),
                         sorted([(ACCT, "15.00", "invoice_line"),
                                 (OTHER, "3.00", "invoice_line"),
                                 (OTHER, "1.00", "vendor_account")]))
        # the added-to-chart account has no BC account: a gap, never a guess
        self.assertIsNone(next(ln for ln in entry["lines"]
                               if ln["sage_account"] == OTHER)["bc_account"])
        self.assertTrue(any("added to the shared chart" in g for g in work["gaps"]))

    def test_match_price_over_billing_and_unknown_po(self):
        self.vendor("SYNV1")
        for i in range(3):
            self.entry("SYNV1", f"H-{i}", f"2026-09-0{i + 1}",
                       [(ACCT, "1.00", "SYN-A", "1", "kg")], due=f"2026-10-0{i + 1}")
        self.purchase_order("SYN-PO-2", [("SYN-A", "5", "5", "2.00")],
                            supplier=self.supplier, received_on="2026-10-01")
        priced = self.entry("SYNV1", "SYN-INV-1", "2026-10-21",
                            [(ACCT, "18.00", "SYN-A", "6", "kg")], po="SYN-PO-2")
        stray = self.entry("SYNV1", "SYN-INV-2", "2026-10-21",
                           [(ACCT, "2.00", "SYN-A", "1", "kg")], po="SYN-PO-404")
        other_sup = self.w.obj("Supplier", {"name": "Synth other"})
        self.purchase_order("SYN-PO-3", [("SYN-A", "5", "5", "")], supplier=other_sup)
        foreign = self.entry("SYNV1", "SYN-INV-3", "2026-10-21",
                             [(ACCT, "2.00", "SYN-A", "1", "kg")], po="SYN-PO-3")
        st = self.store()
        line = self.prep(st, priced)["work"]["match"]["lines"][0]
        self.assertIn("bills 1 more than ordered", line["variance"])
        self.assertIn("1 not yet received", line["variance"])
        self.assertIn("price: 18.00 billed, 12.00 at the PO price", line["variance"])
        self.assertEqual(self.prep(st, stray)["work"]["match"]["status"], "po_not_found")
        got = self.prep(st, foreign)["work"]
        self.assertEqual(got["match"]["status"], "other_supplier")
        self.assertTrue(got["entry"]["on_hold"])

    def test_item_carries_every_source_it_read_and_payroll_marks(self):
        # the item's audiences are every record's it rests on (a reader needs them all);
        # a payroll account's rows carry pii: the item does too
        self.history("SYNV1")
        pii = '["finance","pii"]'
        line = self.w.obj("RelSageApIbd", {"cntbtch": "1", "cntitem": "1", "cntline": "2",
                                           "idglacct": ACCT, "amtdist": "0.00"},
                          binding=pay.LINE_BINDING, audience="api:sage.ap.pay", markings=pii)
        due = self.entry("SYNV1", "SYN-INV-1", "2026-10-21", [(ACCT, "5.00")])
        st = self.store()
        got = self.prep(st, due)
        self.assertEqual(got["audiences"], {"api:sage.ap", "api:sage.ap.pay"})
        self.assertEqual({"finance", "pii"} - got["markings"], set())
        self.assertTrue(line)

    def test_restricted_item_line_and_vendor_reach_the_item(self):
        # the invoice's own item line (pii) and the vendor record read in the PO match (its
        # own audience) both carry onto the item: a reader lacking either never sees it
        v = self.w.obj("RelSageApVendor", {"vendorid": "SYNV1"}, binding="gen.sage.ap_vendor",
                       audience="api:sage.ap.vendor")
        self.w.link("RelSageApVendor.portal_supplier", v, self.supplier)
        for i in range(3):
            self.entry("SYNV1", f"H-{i}", f"2026-09-0{i + 1}",
                       [(ACCT, "1.00", "SYN-A", "1", "kg")], due=f"2026-10-0{i + 1}")
        self.purchase_order("SYN-PO-1", [("SYN-A", "5", "", "")], supplier=self.supplier)
        due = self.entry("SYNV1", "SYN-INV-1", "2026-10-21", [(ACCT, "2.00", "SYN-A", "1",
                                                               "kg")], po="SYN-PO-1")
        line = next(r["object_id"] for r in reversed(self.w.rows["objects"])
                    if r["object_type"] == "RelSageApIbd")
        for r in self.w.rows["object_values"] + self.w.rows["object_versions"]:
            if r["object_id"] == line:
                r["markings"] = '["finance","pii"]'
        for r in self.w.rows["objects"]:
            if r["object_id"] == line:
                r["audience_key"] = "api:sage.ap.lines"
        st = self.store()
        got = self.prep(st, due)
        self.assertLessEqual({"api:sage.ap.lines", "api:sage.ap.vendor"}, got["audiences"])
        self.assertLessEqual({"finance", "pii"}, got["markings"])
        # nothing received by the invoice date: never the PO line's current total
        m = got["work"]["match"]["lines"][0]
        self.assertIsNone(m["received"])
        self.assertIn("received quantity not told", m["variance"])

    def test_off_by_default_and_wired(self):
        reg = ap.load_registry()
        spec = next(s for s in reg["autopilots"] if s["id"] == pay.HANDLER)
        self.assertIs(spec["enabled"], False)
        self.assertTrue(spec["off_reason"])
        self.assertIn(pay.HANDLER, ap.HANDLERS)
        self.history("SYNV1")
        self.entry("SYNV1", "SYN-INV-1", "2026-10-21", [(ACCT, "5.00")])
        st = self.store()
        self.assertEqual(pay.due_events(st, reg, DAY, people()), [])
        # no crosswalk file: the entry still names the Sage account, the BC one is a gap
        os.environ[pay.CROSSWALK_ENV] = str(self.xwalk) + ".absent"
        work = self.prep(st, pay.entry_events(st, reg, DAY, force=True)[0]["object_id"])["work"]
        self.assertIsNone(work["entry"]["lines"][0]["bc_account"])
        self.assertIn("the Sage -> BC G/L crosswalk is not loaded", work["gaps"])

    def test_cardinality_one_entry_per_invoice(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.history("SYNV1")
                due = [self.entry("SYNV1", f"SYN-{1000 + i * 7}", "2026-10-21",
                                  [(ACCT, f"{i + 1}.00")], due="2026-11-20")
                       for i in range(n)]
                st = self.store()
                # CPU time, not wall clock: a busy test host never fails it, an N+1 read
                # path still does
                began = time.process_time()
                got = score.score_day(st, people(), self.registry(), DAY)["counts"]
                self.assertLess(time.process_time() - began, CARDINALITY_SECONDS * 4)
                self.assertEqual((got["events"], got["prepared"], got["exact"]), (n, n, n))
                events = pay.entry_events(st, self.registry(), DAY, force=True)
                self.assertEqual(sorted(e["object_id"] for e in events), sorted(due))
                self.assertEqual(got["total_less_tax_is_lines"], n)


if __name__ == "__main__":
    unittest.main()
