"""Entry assist (``operating.entry_assist``): a customer PO pre-fills the Sage order, every
field cited or ``check``, never a guessed value. SYNTHETIC fixtures; evidence label:
isolated-fixture."""

from __future__ import annotations

import time
import unittest

from operating import autopilot as ap
from operating import autopilot_handlers
from operating import entry_assist as ea
from operating import proactive_engine as pe
from operating.tests.test_autopilot import NOW, Fixture


class OrderEntry(Fixture):

    def ctx(self):
        self.build()
        import sqlite3
        with sqlite3.connect(self.dir / "s.sqlite") as c:   # the builder's customer numbers
            for v in self.w.rows["object_values"]:
                if v["object_id"].startswith("Customer:") and v["property"] == "erp_code":
                    c.execute("INSERT INTO identifiers (identifier_kind, normalized, raw, "
                              "role, object_id, property, l1_handle, field_path, lineage, "
                              "recorded_at) VALUES ('customer_erp_code', ?, ?, 'primary', ?, "
                              "'erp_code', 'h', 'row.erp_code', 'workbench', "
                              "'2026-01-01T00:00:00Z')",
                              (v["value"].upper(), v["value"], v["object_id"]))
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        return autopilot_handlers.Ctx(st, NOW.date(), time.monotonic() + 60, ())

    def test_a_po_mail_prefills_customer_po_and_lines_and_flags_what_it_cannot_read(self):
        self.items_known("SYN-10", "SYN-20")
        msg = self.message(2, "Customer #: C100\nPlease ship:\n1. SYN-10 Gallon soap: "
                              "6 cases\n2. ZZ-404 Unknown thing: 2 cases",
                           subject="New PO 88123")
        got = ea.order_entry(self.ctx(), email=msg)
        h = got["header"]
        self.assertTrue(got["dry_run"])
        self.assertEqual((h["customer"]["status"], h["customer"]["value"]), ("cited", "C100"))
        self.assertIn("customer number the PO states", h["customer"]["source"]["basis"])
        self.assertEqual((h["po_number"]["status"], h["po_number"]["value"]), ("cited", "88123"))
        self.assertEqual(h["ship_to"]["status"], "check")
        known, unknown = got["lines"]
        self.assertEqual((known["item"]["status"], known["item"]["value"]), ("cited", "SYN-10"))
        self.assertEqual(known["quantity"]["value"], "6")
        # no price anywhere: flagged, never filled
        self.assertEqual(known["unit_price"], {"value": None, "status": "check",
                                               "reason": known["unit_price"]["reason"]})
        # an item number the records do not hold is never invented
        self.assertEqual((unknown["item"]["status"], unknown["item"]["value"]), ("check", None))
        self.assertEqual(got["counts"]["check"] + got["counts"]["cited"],
                         got["counts"]["fields"])

    def test_the_sender_alone_never_fills_the_customer_it_is_only_suggested(self):
        # the mail is linked to C100 (its sender's record) but the PO names no customer:
        # a distributor or broker sends POs too, so the sender is no proof of the buyer
        msg = self.message(3, "Please ship 6 cases of SYN-10", subject="PO 88124")
        c = ea.order_entry(self.ctx(), email=msg)["header"]["customer"]
        self.assertEqual((c["status"], c["value"]), ("check", None))
        self.assertEqual([s["value"] for s in c["suggestions"]], ["C100"])
        self.assertIn("sender's customer record", c["suggestions"][0]["basis"])

    def test_a_po_naming_another_customer_than_the_senders_is_a_check_with_both(self):
        # a distributor (D200, the sender's record) sends its end customer's PO
        dist = self.w.obj("Customer", {"name": "Wholesale Distribution Inc", "code": "D200",
                                       "erp_code": "D200"})
        msg = self.message(4, "Ship to: Synth customer\nSYN-10: 6 cases", subject="PO 88125",
                           customer=False)
        self.w.link("Email.sent_by_customer", msg, dist)
        named = self.message(5, "Customer #: C100\nSYN-10: 6 cases", subject="PO 88126",
                             customer=False)
        self.w.link("Email.sent_by_customer", named, dist)
        ctx = self.ctx()
        c = ea.order_entry(ctx, email=named)["header"]["customer"]
        self.assertEqual((c["status"], c["value"]), ("check", None))
        self.assertEqual(sorted(c["candidates"]), ["C100", "D200"])
        # the numbers are never in the words (a reader may open neither record)
        self.assertNotIn("C100", c["reason"])
        self.assertNotIn("D200", c["reason"])
        # named only by name: a check, the named one and the sender's both suggested
        c = ea.order_entry(ctx, email=msg)["header"]["customer"]
        self.assertEqual((c["status"], c["value"], c["candidates"]), ("check", None, ["C100"]))
        self.assertEqual([s["value"] for s in c["suggestions"]], ["C100", "D200"])

    def test_a_customer_named_only_by_name_is_a_check_and_two_named_are_a_check(self):
        self.w.obj("Customer", {"name": "Northern Wholesale Ltd", "code": "N300",
                                "erp_code": "N300"})
        self.w.obj("Customer", {"name": "Street numbered", "erp_code": "1200"})
        ctx = self.ctx()
        # re-measure 08-03..09-19: every customer cited by its name alone was wrong (a
        # contact's name in a forwarded thread, a generic name, a parent company's name
        # on a branch's order): a name is suggested, never cited
        one = ea.order_entry(ctx, text="Bill to: Synth customer\nPO 77001\nSYN-10: 6 cases")
        c = one["header"]["customer"]
        self.assertEqual((c["status"], c["value"], c["candidates"]), ("check", None, ["C100"]))
        (hint,) = c["suggestions"]
        self.assertEqual((hint["value"], hint["basis"]), ("C100", "the customer's name in the PO"))
        self.assertNotIn("C100", c["reason"])
        labelled = ea.order_entry(ctx, text="Customer #: C100\nPO 77001")["header"]["customer"]
        self.assertEqual((labelled["status"], labelled["value"]), ("cited", "C100"))
        two = ea.order_entry(ctx, text="Sold to Northern Wholesale Ltd, ship to Synth "
                                       "customer\nPO 77002")
        c = two["header"]["customer"]
        self.assertEqual((c["status"], sorted(c["candidates"])), ("check", ["C100", "N300"]))
        # a labelled number beats names; a vendor's own number is never the buyer's
        got = ea.order_entry(ctx, text="Vendor #: N300\nCustomer No: C100\nNorthern "
                                       "Wholesale Ltd\nPO 77003")["header"]["customer"]
        self.assertEqual((got["status"], got["value"]), ("cited", "C100"))
        none = ea.order_entry(ctx, text="Vendor #: N300\nPO 77004")["header"]["customer"]
        self.assertEqual(none["status"], "check")
        # an address block's street number is no customer number, nor an unlabelled one
        for text in ("Bill To: 1200 Industrial Pkwy\nPO 77005",
                     "Customer 1200 asked\nPO 77006"):
            got = ea.order_entry(ctx, text=text)["header"]["customer"]
            self.assertEqual(got["status"], "check", text)

    def test_several_po_numbers_or_no_sender_are_checks_with_candidates(self):
        self.items_known("SYN-10")
        ctx = self.ctx()
        got = ea.order_entry(ctx, text="PO 4512\nPO 4513\nSYN-10 soap: 6 cases")
        self.assertEqual(got["header"]["po_number"]["status"], "check")
        self.assertEqual(got["header"]["po_number"]["candidates"], ["4512", "4513"])
        self.assertEqual(got["header"]["customer"]["status"], "check")
        self.assertEqual(got["lines"][0]["unit_price"]["status"], "check")
        with self.assertRaises(ValueError):
            ea.order_entry(ctx)

    def test_a_last_price_is_cited_only_in_the_lines_unit_and_the_customers_currency(self):
        from operating.tests.test_proactive_business import money
        for per, ordered, reused in (("CASE", "6 cases", True), ("KG", "6 cases", False)):
            with self.subTest(per=per):
                self.setUp()
                self.w.rows["object_values"].append({
                    **[v for v in self.w.rows["object_values"]
                       if v["object_id"] == self.customer][0],
                    "property": "currency", "value": "CAD", "value_type": "text"})
                self.items_known("SYN-10")
                inv = self.w.obj("SalesInvoice", {"invoice_number": "INV-9",
                                                  "invoice_date": "2026-09-01",
                                                  "currency": "CAD"})
                self.w.link("SalesInvoice.billed_to", inv, self.customer)
                line = self.w.obj("SalesInvoiceLine", {"unit_price": "4.00",
                                                       "invoiced_quantity": ("10", per)})
                money(self.w, line, "unit_price", "CAD", per=per)
                self.w.link("SalesInvoiceLine.line_of", line, inv)
                self.w.link("SalesInvoiceLine.for_item", line, self.items["SYN-10"])
                msg = self.message(95, "Account No. C100\nPlease ship:\n"
                                       f"1. SYN-10 Gallon soap: {ordered}",
                                   subject="New PO 88130")
                bc = self.w.obj("Customer", {"name": "Synth customer", "erp_code": "C100"},
                                binding="bc.customers")
                handle = ("bc://production/c0c0c0c0-1111-4111-8111-000000000001/customers/"
                          "b1b1b1b1-2222-4222-8222-000000000002@v1")
                for row in self.w.rows["object_versions"] + self.w.rows["object_values"]:
                    if row["object_id"] == bc:
                        row["l1_handle"] = handle
                self.w.link("Customer.same_record_as", self.customer, bc)
                ctx = self.ctx()
                pre = ea.order_entry(ctx, email=msg)
                (ln,) = pre["lines"]
                # an earlier price is no proof of this order's price: never cited; in the
                # line's unit and currency it is only suggested
                self.assertEqual((ln["unit_price"]["status"], ln["unit_price"]["value"]),
                                 ("check", None))
                # A price that is a check holds the whole BC entry (a
                # line sent without one takes BC's item default) until a person confirms it
                self.assertEqual(ea.bc_header(ctx, pre, "order"), {"refused": [
                    "Line 1: its price is a check: enter or confirm it first (BC would "
                    "otherwise use the item's default price)."]})
                if reused:
                    (hint,) = ln["unit_price"]["suggestions"]
                    self.assertEqual((hint["value"], hint["document"], hint["currency"]),
                                     ("4.00", "INV-9", "CAD"))
                    # the person confirms the suggested price: the entry carries it
                    ln["unit_price"] = ea.cited(hint["value"], {
                        "basis": "confirmed by the person", "record": hint["record"]})
                    entry = ea.bc_header(ctx, pre, "order")
                    (bl,) = entry["payload"]["lines"]
                    self.assertEqual((bl["lineObjectNumber"], bl["quantity"], bl["unitPrice"]),
                                     ("SYN-10", "6", "4.00"))
                    self.assertTrue({self.customer, bc, self.items["SYN-10"], hint["record"]}
                                    <= set(entry["records"]))
                    self.assertEqual(entry["party"]["bc_id"],
                                     "b1b1b1b1-2222-4222-8222-000000000002")
                    # an unreadable or negative confirmed price still holds it
                    for bad in ("", "abc", "-1", "NaN", "Infinity"):
                        ln["unit_price"] = ea.cited(bad, {})
                        self.assertIn("Line 1: its price is a check",
                                      " ".join(ea.bc_header(ctx, pre, "order")["refused"]))
                else:
                    self.assertNotIn("suggestions", ln["unit_price"])

    def test_our_own_company_named_in_the_po_is_never_the_customer(self):
        # every PO a customer sends names us, the vendor; our house account (a customer
        # record with an address at our own domain) is never read as the buyer
        self.w.obj("Customer", {"name": "Synth House Supplies", "erp_code": "H010",
                                "email": "orders@synth-house.example"})
        text = "Vendor: Synth House Supplies\nBill to: Synth customer\nPO 77010"
        ctx = self.ctx()
        ctx.domains = ("synth-house.example",)
        c = ea.order_entry(ctx, text=text)["header"]["customer"]
        # named by name only: a check, and the house account is no candidate
        self.assertEqual((c["status"], c["candidates"]), ("check", ["C100"]))
        labelled = ea.order_entry(ctx, text="Vendor: Synth House Supplies\nCustomer #: C100"
                                            "\nPO 77013")["header"]["customer"]
        self.assertEqual((labelled["status"], labelled["value"]), ("cited", "C100"))
        alone = ea.order_entry(ctx, text="Customer #: H010\nPO 77011")
        self.assertEqual(alone["header"]["customer"]["status"], "check")
        self.assertNotIn("candidates", alone["header"]["customer"])
        # sent by one of us (an internal order), the house account may be the customer
        inside = ea.order_entry(ctx, text="Customer #: H010\nPO 77012",
                                sender="buyer@synth-house.example")["header"]["customer"]
        self.assertEqual((inside["status"], inside["value"]), ("cited", "H010"))
        # a customer PO one of us forwards still names us as its vendor: decided by the
        # buyer the PO names, the house account is dropped, never cited or a candidate
        for body, want in (("Fwd: " + text, ("check", ["C100"])),
                           ("Fwd: Vendor: Synth House Supplies\nCustomer #: C100\nPO 77014",
                            ("cited", "C100"))):
            fwd = ea.order_entry(ctx, text=body,
                                 sender="Rep <rep@synth-house.example>")["header"]["customer"]
            self.assertEqual((fwd["status"], fwd.get("candidates") or fwd["value"]), want)
        self.w.obj("Customer", {"name": "Synth Other Buyer", "erp_code": "C200"})
        ctx = self.ctx()
        ctx.domains = ("synth-house.example",)
        two = ea.order_entry(ctx, text=text + "\nShip to: Synth Other Buyer",
                             sender="rep@synth-house.example")["header"]["customer"]
        self.assertEqual((two["status"], sorted(two["candidates"])), ("check", ["C100", "C200"]))
        # our own domains unreadable: a named customer cannot be told from us, so a check
        ctx.domains = ()
        from integrations import capture_mail
        real = capture_mail.DEFAULT_SCOPE
        capture_mail.DEFAULT_SCOPE = self.dir / "missing-scope.json"
        self.addCleanup(setattr, capture_mail, "DEFAULT_SCOPE", real)
        unknown = ea.order_entry(ctx, text="Vendor: Synth House Supplies\nCustomer #: C100"
                                           "\nPO 77015")["header"]["customer"]
        self.assertEqual((unknown["status"], unknown["value"]), ("check", None))

    def test_an_item_that_is_blocked_inactive_or_on_a_spreadsheet_row_is_a_check(self):
        # re-measure 08-03..09-19: a customer's old code named a blocked, inactive item
        # (staff keyed its replacement); an order form's row read its pack size as the
        # quantity of an item not ordered. Each item is suggested, never cited
        self.items_known("SYN-10", "SYN-20", "SYN-30", "SYN-40")
        for code, state in (("SYN-20", {"blocked": "true"}), ("SYN-30", {"is_active": "false"})):
            twin = self.w.obj("Product", {"code": code, **state}, binding="portal.app_product")
            self.w.link("Product.same_record_as", self.items[code], twin)
        ctx = self.ctx()
        got = ea.order_entry(ctx, text="Customer #: C100\nPO 88140\n"
                                       "1. SYN-10 Gallon soap: 6 cases\n"
                                       "2. SYN-20 Old wipes: 2 cases\n"
                                       "3. SYN-30 Retired gel: 3 cases\n"
                                       "D31=0 | E31=4 | F31=SYN-40 Eucalyptus oil 4 gallon")
        ok, blocked, inactive, sheet = got["lines"]
        self.assertEqual((ok["item"]["status"], ok["item"]["value"]), ("cited", "SYN-10"))
        for ln, code, why in ((blocked, "SYN-20", "blocked or inactive"),
                              (inactive, "SYN-30", "blocked or inactive"),
                              (sheet, "SYN-40", "spreadsheet row")):
            self.assertEqual((ln["item"]["status"], ln["item"]["value"]), ("check", None), code)
            self.assertIn(why, ln["item"]["reason"])
            self.assertEqual(ln["item"]["suggestions"][0]["value"], code)

    def test_a_po_label_is_a_whole_word_and_never_a_word_it_opens(self):
        # tuning 02-02..05-03: both wrong cited PO numbers were the next line's code under
        # a word "po" opens ("Poids : 1.6 LBS" / "Reference 1: 979266"; "...keep you
        # posted" / "#169BIW Conair dryer"); a P.O. Box is an address
        self.items_known("SYN-10")
        ctx = self.ctx()
        for body in ("Poids : 1.6 LBS\nReference Number 1:     979266",
                     "I wanted to keep you posted that we have these now.\n#169BIW Conair",
                     "Ship to P.O. Box 4471\nVancouver BC",
                     "Purchase Orders\nAB12345",
                     "PO attached\nSYN-10 6 cases"):
            got = ea.order_entry(ctx, text=body)["header"]["po_number"]
            self.assertEqual((got["status"], got["value"]), ("check", None), body)
        for body, po in (("Please use PO number:DC00056417.", "DC00056417"),
                         ("PO Number:\nDC00057336", "DC00057336"),
                         ("P.O. # 4500123", "4500123"), ("po 88124", "88124")):
            got = ea.order_entry(ctx, text=body)["header"]["po_number"]
            self.assertEqual((got["status"], got["value"]), ("cited", po), body)

    def test_a_reply_thread_is_read_one_message_at_a_time_and_quoted_lines_are_checks(self):
        # tuning 02-02..05-03: one reply's lines were read across three quoted messages
        # (an item twice with two quantities, items of another truck); a thread whose
        # newest message has no lines can have changed the order it quotes
        self.items_known("SYN-10", "SYN-20", "SYN-30")
        newest = self.message(
            11, "Monday for the whole order is ok.\n\nFrom: Desk <desk@ours.example>\n"
                "Sent: Tuesday\nTo: Buyer\nSubject: RE: order\n\nCan you wait?\n\n"
                "-----Original Message-----\nPlease ship:\nSYN-10 Gallon soap 6 cases\n"
                "SYN-20 Wash 2 cases\n\nOn Mon, Apr 13, 2026 at 9:00 AM Buyer wrote:\n"
                "> SYN-30 Wipes 9 cases", subject="RE: order")
        mixed = self.message(
            12, "Please ship:\nSYN-10 Gallon soap 6 cases\n\nFrom: Desk <desk@ours.example>"
                "\nDate: Monday\nSubject: truck\n\nSYN-10 Gallon soap 32 cases\n"
                "SYN-30 Wipes 9 cases", subject="RE: truck")
        ctx = self.ctx()
        got = ea.order_entry(ctx, email=newest)
        self.assertEqual([ln["item"]["suggestions"][0]["value"] for ln in got["lines"]],
                         ["SYN-10", "SYN-20"])           # one message only, never SYN-30
        for ln in got["lines"]:
            for field in ("item", "quantity"):
                self.assertEqual((ln[field]["status"], ln[field]["value"]), ("check", None))
                self.assertIn("earlier message", ln[field]["reason"])
        self.assertEqual(got["lines"][0]["quantity"]["suggestions"][0]["value"], "6")
        # the cite is the body's own line, not the quoted message's
        self.assertEqual(got["lines"][0]["quantity"]["suggestions"][0]["cite"], "L12")
        got = ea.order_entry(ctx, email=mixed)
        self.assertEqual([(ln["item"]["status"], ln["item"]["value"], ln["quantity"]["value"])
                          for ln in got["lines"]], [("cited", "SYN-10", "6")])

    def test_an_item_the_mail_says_is_back_ordered_or_replaced_is_a_check(self):
        # tuning 02-02..05-03: "FNA300/BR900 b/o" in the subject; staff keyed the order
        # without them
        self.items_known("SYN-10", "SYN-20", "SYN-30")
        msg = self.message(13, "Please ship:\nSYN-10 Gallon soap 6 cases\nSYN-20 Wash 2 cases\n"
                               "SYN-30 Wipes 1 case\nNote: SYN-30 is out of stock",
                           subject="RE: SYN-20 b/o PO 88150")
        got = ea.order_entry(self.ctx(), email=msg)
        self.assertEqual([(ln["item"]["status"], ln["item"]["value"]) for ln in got["lines"]],
                         [("cited", "SYN-10"), ("check", None), ("check", None)])
        for ln in got["lines"][1:]:
            self.assertIn("back-ordered", ln["item"]["reason"])

    def test_a_line_naming_another_product_or_unit_than_the_items_is_a_check(self):
        # tuning 02-02..05-03: "JT360 Hair And Body Shampoo" was a spa soap's number;
        # "MC3017 Razors (4 Boxes)" ordered boxes of an item sold by the case. Staff keyed
        # other items both times. Agreeing words ("Bodywash" for "Body Wash") stay cited;
        # a unit the item is not sold in is a check too
        self.items = {code: self.w.obj("Product", {"code": code, "name": name, "uom": uom},
                                       binding="bc.items")
                      for code, name, uom in (("SYN-10", "Spa Soap Combo Clear", "CSE"),
                                              ("SYN-20", "Premium Razors 32/Pkg", "CSE"),
                                              ("SYN-30", "For & Against Body Wash", "CSE"),
                                              ("SYN-40", "Hand Soap", ""))}
        ctx = self.ctx()
        got = ea.order_entry(ctx, text="PO 88160\nSYN-10 Hair And Body Shampoo (4 Cases)\n"
                                       "SYN-20 Razors (4 Boxes)\nSYN-30 Bodywash (7 Cases)\n"
                                       "SYN-40 Hand soap 3 cases\nSYN-30 wash 2 pails")
        shampoo, boxes, bodywash, hand, pails = got["lines"]
        self.assertEqual((shampoo["item"]["status"], shampoo["item"]["value"]), ("check", None))
        self.assertIn("another product", shampoo["item"]["reason"])
        self.assertEqual(shampoo["item"]["suggestions"][0]["value"], "SYN-10")
        for ln in (boxes, pails):
            self.assertEqual((ln["item"]["status"], ln["quantity"]["status"]),
                             ("check", "check"), ln["text"])
            self.assertIn("not sold in", ln["item"]["reason"])
        for ln, code in ((bodywash, "SYN-30"), (hand, "SYN-40")):
            self.assertEqual((ln["item"]["status"], ln["item"]["value"]), ("cited", code))

    def test_cardinality_every_line_is_kept_once_in_order(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.items_known(*(f"SYN-{i:03d}" for i in range(n)))
                text = "PO 88170\n" + "\n".join(f"SYN-{i:03d} Item {i}: {i + 1} cases"
                                                   for i in range(n))
                got = ea.order_entry(self.ctx(), text=text)
                self.assertEqual([(ln["item"]["value"], ln["quantity"]["value"])
                                  for ln in got["lines"]],
                                 [(f"SYN-{i:03d}", str(i + 1)) for i in range(n)])

    def test_accuracy_counts_a_check_apart_from_a_wrong_value(self):
        pre = {"header": {"customer": ea.cited("C100", {}), "po_number": ea.check("none")},
               "lines": [{"item": ea.cited("SYN-10", {}), "quantity": ea.cited("6", {}),
                          "unit_price": ea.cited("4.00", {})},
                         {"item": ea.cited("SYN-20", {}), "quantity": ea.cited("2", {}),
                          "unit_price": ea.check("none")}]}
        got = ea.accuracy(pre, {"customer": ["C100"], "po_number": "88",
                                "lines": [("SYN-10", "6", "4"), ("SYN-30", "2", "1")]})
        # a line whose item is wrong leaves its quantity unscored, not wrong
        self.assertEqual(got, {"customer_right": 1, "po_number_check": 1, "item_right": 1,
                               "quantity_right": 1, "unit_price_right": 1, "item_wrong": 1,
                               "quantity_unscored": 1, "unit_price_check": 1,
                               "lines_keyed": 2, "lines_prefilled": 2})
        keyed_with_words = {"header": {"customer": ea.check("x"),
                                       "po_number": ea.cited("PO-153", {})}, "lines": []}
        self.assertEqual(ea.accuracy(keyed_with_words, {"po_number": "PO-153 Paw lotion"})
                         ["po_number_right"], 1)
        self.assertEqual(ea.accuracy(keyed_with_words, {"po_number": "PO-1534"})
                         ["po_number_wrong"], 1)


class PurchaseOrderAssist(Fixture):

    def ctx(self):
        self.build()
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        return autopilot_handlers.Ctx(st, NOW.date(), time.monotonic() + 60, ())

    def po(self, supplier, day, lines, *, per="KG"):
        """A PO of ``lines`` (item, quantity, price); ``per``: the unit the money model reads
        the price as per (None: a lineage that stores a line total or another unit's cost)."""
        from operating.tests.test_proactive_business import money
        po = self.w.obj("PurchaseOrder", {"po_number": f"P-{day}", "order_date": day,
                                          "currency": "CAD"})
        self.w.link("PurchaseOrder.ordered_from", po, supplier)
        for item, qty, price in lines:
            ln = self.w.obj("PurchaseOrderLine", {"ordered_quantity": qty, "unit_price": price,
                                                  "uom": "KG"})
            money(self.w, ln, "unit_price", "CAD", per=per)
            self.w.link("PurchaseOrderLine.line_of", ln, po)
            self.w.link("PurchaseOrderLine.for_item", ln, self.items[item])
        return po

    def test_named_items_suggest_their_last_supplier_and_price_and_fill_neither(self):
        self.items_known("SYN-10", "SYN-20")
        sup = self.w.obj("Supplier", {"name": "Synth supplier"})
        self.po(sup, "2026-09-01", [("SYN-10", "100", "2.00")])
        last = self.po(sup, "2026-10-01", [("SYN-10", "120", "2.10"), ("SYN-20", "5", "9")])
        self.po(sup, "2026-12-01", [("SYN-10", "999", "7.00")])     # after the day: unread
        so = self.w.obj("SalesOrderLine", {"outstanding_quantity": "30"})
        self.w.link("SalesOrderLine.for_item", so, self.items["SYN-10"])
        ctx = self.ctx()
        got = ea.purchase_order(ctx, items=["SYN-10"])
        # the items' last supplier is only suggested: the person names the supplier
        self.assertEqual((got["header"]["supplier"]["status"], got["header"]["supplier"]["value"]),
                         ("check", None))
        self.assertEqual([s["value"] for s in got["header"]["supplier"]["suggestions"]], [sup])
        self.assertEqual(got["lines"][0]["unit_price"]["status"], "check")
        got = ea.purchase_order(ctx, supplier=sup, items=["SYN-10"])
        self.assertEqual(got["header"]["supplier"]["status"], "cited")
        (ln,) = got["lines"]
        # an earlier price is never the new PO's price: a check, the last one suggested
        self.assertEqual((ln["unit_price"]["status"], ln["unit_price"]["value"]),
                         ("check", None))
        (hint,) = ln["unit_price"]["suggestions"]
        self.assertEqual((hint["value"], hint["record"], hint["unit"], hint["currency"]),
                         ("2.10", last, "KG", "CAD"))
        self.assertEqual((ln["unit"]["status"], ln["unit"]["suggestions"][0]["value"]),
                         ("check", "KG"))
        self.assertEqual(got["counts"]["cited"], 2)            # the supplier and the item
        self.assertEqual((ln["quantity"]["status"], ln["quantity"]["value"]), ("check", None))
        self.assertEqual([s["value"] for s in ln["quantity"]["suggestions"]], ["30", "120"])

    def test_a_price_whose_unit_is_unknown_or_whose_last_lines_disagree_is_never_offered(self):
        # held-out 11-06..11-13: cited last prices that were line totals (6 x 400 stored as
        # 2400), a cost per another unit, or two prices the same day: none is offered
        for case, setup in (
                ("unit unknown", lambda sup: self.po(sup, "2026-10-01",
                                                     [("SYN-10", "6", "2400")], per=None)),
                ("disagree", lambda sup: self.po(sup, "2026-10-01",
                                                 [("SYN-10", "6", "400"), ("SYN-10", "1", "2400")])),
                ("none", lambda sup: None)):
            with self.subTest(case=case):
                self.setUp()
                self.items_known("SYN-10")
                sup = self.w.obj("Supplier", {"name": "Synth supplier"})
                setup(sup)
                (ln,) = ea.purchase_order(self.ctx(), supplier=sup, items=["SYN-10"])["lines"]
                for field in ("unit_price", "unit"):
                    self.assertEqual((ln[field]["status"], ln[field]["value"]), ("check", None))
                    self.assertNotIn("suggestions", ln[field])
                self.assertIn({"unit unknown": "do not state the unit", "disagree": "disagree",
                               "none": "no earlier price"}[case], ln["unit_price"]["reason"])

    def test_one_supplier_under_twin_records_is_one_supplier(self):
        # the vendor's Portal record on earlier POs and its BC record named now: one
        # supplier, its history and its suggestion; N twins of other vendors counted once
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.items_known("SYN-10")
                portal = self.w.obj("Supplier", {"name": "Synth supplier"})
                bc = self.w.obj("Supplier", {"name": "Synth supplier"}, binding="bc.vendors")
                fm = self.w.obj("Supplier", {"name": "Synth supplier"}, binding="fm")
                self.w.link("Supplier.same_record_as", bc, portal)
                self.w.link("Supplier.same_record_as", portal, fm)
                for i in range(n):
                    self.po(fm if i % 2 else portal, f"2026-09-{1 + i % 28:02d}",
                            [("SYN-10", "10", "2.00")])
                self.po(portal, "2026-10-01", [("SYN-10", "120", "2.10")])
                ctx = self.ctx()
                both = ea.purchase_order(ctx, items=["SYN-10"])["header"]["supplier"]
                (sg,) = both["suggestions"]
                self.assertEqual((sg["value"], sg["basis"]), (portal, "the items' last PO"))
                (ln,) = ea.purchase_order(ctx, supplier=bc, items=["SYN-10"])["lines"]
                self.assertEqual(ln["unit_price"]["suggestions"][0]["value"], "2.10")
                self.assertEqual(ln["quantity"]["suggestions"][-1]["value"], "120")
                alone = ea.purchase_order(ctx, supplier=bc)["lines"]
                self.assertEqual([x["item"]["value"] for x in alone], ["SYN-10"])

    def test_a_twin_link_that_is_not_identifier_confirmed_is_never_followed(self):
        # one bad lead or text-join twin must not pull another vendor's POs, prices and
        # items into this supplier's history; a verified or corroborated twin is followed
        for tier, merged in (("lead", False), ("stored_by_text_join", False),
                             ("contradicted", False), ("verified", True),
                             ("corroborated", True)):
            with self.subTest(tier=tier):
                self.setUp()
                self.items_known("SYN-10", "SYN-20")
                mine = self.w.obj("Supplier", {"name": "Synth supplier"})
                other = self.w.obj("Supplier", {"name": "Another vendor"}, binding="bc.vendors")
                self.w.link("Supplier.same_record_as", mine, other, tier)
                self.po(mine, "2026-09-01", [("SYN-10", "10", "2.00")])
                self.po(other, "2026-10-01", [("SYN-10", "50", "9.99"),
                                              ("SYN-20", "5", "3.00")])
                ctx = self.ctx()
                (ln,) = ea.purchase_order(ctx, supplier=mine, items=["SYN-10"])["lines"]
                self.assertEqual(ln["unit_price"]["suggestions"][0]["value"],
                                 "9.99" if merged else "2.00")
                alone = ea.purchase_order(ctx, supplier=mine)["lines"]
                self.assertEqual(sorted(x["item"]["value"] for x in alone),
                                 ["SYN-10", "SYN-20"] if merged else ["SYN-10"])

    def test_a_price_cell_without_a_money_reading_is_never_offered(self):
        # a store row whose price cell is a bare value (no money model reading): no unit
        # or currency is known, so no suggestion, and nothing fails
        self.items_known("SYN-10")
        sup = self.w.obj("Supplier", {"name": "Synth supplier"})
        self.po(sup, "2026-10-01", [("SYN-10", "6", "2.00")])
        ctx = self.ctx()
        real = ctx.read

        def bare(ids, props):
            got = real(ids, props)
            for row in got.values():
                if isinstance(row.get("unit_price"), dict):
                    row["unit_price"] = row["unit_price"].get("value")
            return got

        for cell in ("bare", None):
            with self.subTest(cell=cell):
                ctx.read = bare if cell else real
                (ln,) = ea.purchase_order(ctx, supplier=sup, items=["SYN-10"])["lines"]
                self.assertEqual((ln["unit_price"]["status"], ln["unit_price"]["value"]),
                                 ("check", None))
                self.assertEqual("suggestions" in ln["unit_price"], cell is None)

    def test_cardinality_a_demand_sum_is_suggested_only_while_every_line_is_cited(self):
        for n, shown in ((1, True), (20, True), (21, False), (60, False)):
            with self.subTest(n=n):
                self.setUp()
                self.items_known("SYN-10")
                sup = self.w.obj("Supplier", {"name": "Synth supplier"})
                self.po(sup, "2026-10-01", [("SYN-10", "120", "2.10")])
                for _ in range(n):
                    so = self.w.obj("SalesOrderLine", {"outstanding_quantity": "1"})
                    self.w.link("SalesOrderLine.for_item", so, self.items["SYN-10"])
                (ln,) = ea.purchase_order(self.ctx(), items=["SYN-10"])["lines"]
                demand = [s for s in ln["quantity"]["suggestions"]
                          if s["basis"] == "open sales order demand"]
                self.assertEqual(bool(demand), shown)
                if shown:
                    self.assertEqual((demand[0]["value"], len(demand[0]["records"])),
                                     (str(n), n))

    def test_a_supplier_alone_suggests_its_latest_items_and_two_suppliers_are_a_check(self):
        self.items_known("SYN-10", "SYN-20")
        a = self.w.obj("Supplier", {"name": "Synth A"})
        b = self.w.obj("Supplier", {"name": "Synth B"})
        self.po(a, "2026-10-01", [("SYN-10", "120", "2.10"), ("SYN-20", "5", "9")])
        self.po(b, "2026-10-02", [("SYN-10", "50", "2.50")])
        ctx = self.ctx()
        got = ea.purchase_order(ctx, supplier=a)
        self.assertEqual(sorted(ln["item"]["value"] for ln in got["lines"]),
                         ["SYN-10", "SYN-20"])
        both = ea.purchase_order(ctx, items=["SYN-10"])
        self.assertEqual(both["header"]["supplier"]["status"], "check")
        self.assertEqual([s["value"] for s in both["header"]["supplier"]["suggestions"]], [b])
        self.assertIn("of 2 suppliers", both["header"]["supplier"]["suggestions"][0]["basis"])
        self.assertEqual(both["lines"][0]["unit_price"]["status"], "check")
        none = ea.purchase_order(ctx, items=["ZZ-404"])
        self.assertEqual(none["header"]["supplier"]["status"], "check")
        # a named item no record holds stays on the PO as a check, never dropped
        self.assertEqual([(ln["item"]["status"], ln["item"]["value"]) for ln in none["lines"]],
                         [("check", None)])


class SupplierInvoiceAssist(Fixture):

    def ctx(self):
        self.build()
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        return autopilot_handlers.Ctx(st, NOW.date(), time.monotonic() + 60, ())

    def test_an_invoice_prefills_vendor_number_date_total_and_its_po(self):
        sup = self.w.obj("Supplier", {"name": "Synth supplier", "rep_email": "ar@synth-supplier.example"})
        po = self.w.obj("PurchaseOrder", {"po_number": "4512", "order_date": "2026-09-01"})
        self.w.link("PurchaseOrder.ordered_from", po, sup)
        other = self.w.obj("Supplier", {"name": "Other synth"})
        twin = self.w.obj("PurchaseOrder", {"po_number": "4513", "order_date": "2026-09-01"})
        self.w.link("PurchaseOrder.ordered_from", twin, other)
        ctx = self.ctx()
        got = ea.supplier_invoice(ctx, sender="ar@synth-supplier.example", text=(
            "INVOICE # INV-77\nInvoice Date: Oct 3, 2026\nPO 4512\n"
            "SYN-10 Gallon soap 6 x 4.00 24.00\nTotal Due: $1,204.50"))
        h = got["header"]
        self.assertEqual({k: (f["status"], f["value"]) for k, f in h.items()}, {
            "vendor": ("cited", sup), "invoice_number": ("cited", "INV-77"),
            "invoice_date": ("cited", "2026-10-03"), "total": ("cited", "1204.50"),
            "purchase_order": ("cited", po)})
        # the same PO number from another vendor is never cited for this invoice
        other_po = ea.supplier_invoice(ctx, sender="ar@synth-supplier.example",
                                       text="Invoice # 9\nPO 4513\nTotal Due: 1.00")
        self.assertEqual(other_po["header"]["purchase_order"]["status"], "check")

    def test_an_ambiguous_date_two_totals_and_an_unknown_sender_are_checks(self):
        got = ea.supplier_invoice(self.ctx(), sender="x@unknown.example", text=(
            "Invoice No: 88\nInvoice Date: 10/03/2026\nTotal Due: 10.00\nAmount Due: 12.00"))
        h = got["header"]
        for k in ("vendor", "invoice_date", "total", "purchase_order"):
            self.assertEqual((h[k]["status"], h[k]["value"]), ("check", None), k)
        self.assertEqual(h["total"]["candidates"], ["10.00", "12.00"])
        self.assertEqual(h["invoice_number"]["value"], "88")


if __name__ == "__main__":
    unittest.main()
