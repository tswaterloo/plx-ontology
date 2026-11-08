"""Sheet readings (``integrations.sheet_readings``) and the alerts over them
(``operating.proactive_sheets``, ``stock_out_risk``) through the engine, the per-reader inbox
and the people exclusion: remittances matched to open invoices, supplier statements against
our payables, and on hand read from hand-kept sheets of several layouts.

SYNTHETIC fixtures only (``World`` stores, synthetic codes and sheets); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path

from integrations import sheet_readings as sr
from operating import proactive_business as pb
from operating import proactive_sheets as ps
from operating.tests.test_proactive_business import item, sales_order
from operating.tests.test_proactive_engine import (AS_OF, EXCLUDED_OID, OURS_OID, Engine,
                                                   World, people)

FIN = '["finance"]'
BOX = f"mail:{OURS_OID}"           # a mailbox of one of our people, cleared
OURS = "ops@synth-tenant.example"
EXCLUDED = "boss@synth-tenant.example"
OUTSIDE = "payables@synth-customer.example"


def day(offset: int, fmt: str = "%Y-%m-%d") -> str:
    return (AS_OF - timedelta(days=offset)).strftime(fmt)


def ar(w: World, number: str, total: str, due: str, *, party: str = "SYNTHCUST",
       paid: bool = False) -> str:
    return w.obj("RelSageArOpenDocument", {
        "idcust": party, "idinvc": number, "dateinvc": day(40, "%Y%m%d"),
        "datedue": day(10, "%Y%m%d"), "amtinvctc": total, "amtduetc": due,
        "codecurn": "CAD", "swpaid": "1" if paid else "0", "trxtypetxt": "1"},
        binding="gen.sage.ar_open_document", audience="api:sage.ar", markings=FIN)


def ap(w: World, number: str, total: str, due: str, *, party: str = "SYNTHVEND",
       paid: bool = False, dated: int = 40) -> str:
    return w.obj("RelSageApOpenDocument", {
        "idvend": party, "idinvc": number, "dateinvc": day(dated, "%Y%m%d"),
        "dateinvcdu": day(10, "%Y%m%d"), "amtinvctc": total, "amtduetc": due,
        "codecurn": "CAD", "swpaid": "1" if paid else "0", "datepaid": day(30, "%Y%m%d")
        if paid else "0", "txttrxtype": "1"},
        binding="gen.sage.ap_open_document", audience="api:sage.ap", markings=FIN)


def workbook(rows: list[list[str]], sheet: str = "Sheet1", top: int = 1) -> str:
    lines = [f"[Sheet: {sheet}]"]
    for r, row in enumerate(rows, top):
        cells = [f"{sr._letters(c)}{r}={v}" for c, v in enumerate(row, 1) if v != ""]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


class Sheets:
    """Captured files for one test: their stored text as blobs, their sources, and the
    store records (``Attachment``) that hold them."""

    def __init__(self, test: Engine, w: World) -> None:
        self.test, self.w = test, w
        self.blobs = test.dir / "blobs"
        self.sources: list[sr.Source] = []
        self.records: dict[str, tuple[str, str, str, str]] = {}

    def add(self, name: str, text: str, *, participants=(OUTSIDE, OURS),
            audience: str = BOX, received: int = 12, in_store: bool = True) -> str:
        data = text.encode("utf-8")
        digest = sha256(data).hexdigest()
        path = self.blobs / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        handle = f"graph-mail://synth/{len(self.sources)}"
        self.sources.append(sr.Source(handle, "mail", name, sha256(name.encode()).hexdigest(),
                                      digest, day(received) + "T12:00:00Z",
                                      tuple(participants)))
        if in_store:
            oid = self.w.obj("Attachment", {"name": name}, audience=audience)
            self.records[handle] = (oid, "Attachment", audience, "[]")
        return handle

    def read(self, workers: int = 1) -> dict:
        return sr.run(self.test.dir, self.test.dir / ps.READINGS_FILE, store=None,
                      workers=workers, sources=self.sources, blob_dirs=[self.blobs],
                      records=self.records)


REMIT_HEAD = ["Payment Reference", "Payment Type", "Payment Date", "Payment Amount",
              "Invoice Date", "Invoice Reference", "Amount Paid", "Discount Taken"]


def remittance(lines: list[tuple[str, str, str]], paid_days: int = 12) -> str:
    """The dominant layout: one row per invoice, the payment on every row."""
    total = str(sum(float(p) + float(d) for _r, p, d in lines))
    return workbook([["Remittance advice"], [], REMIT_HEAD] + [
        ["SYN-PAY-1", "EFT", day(paid_days, "%m/%d/%Y"), total, day(40, "%m/%d/%Y"), ref,
         paid, disc] for ref, paid, disc in lines] + [["", "", "", "", "", "Total", total]])


def bank_remittance(lines: list[tuple[str, str]]) -> str:
    """A bank's layout, as CSV, labelled payment date above the table."""
    return "\n".join(["Payment date:," + day(12, "%d-%b-%Y"), "",
                      "Invoice #,Invoice Amount,Net Paid"] +
                     [f"{ref},{paid},{paid}" for ref, paid in lines])


class SheetAlerts(Engine):

    def run_rules(self, w: World, *rules: str, **options) -> dict:
        return self.run_engine(w, only=list(rules or ps.RULES), **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    def test_remittances_of_two_layouts_raise_unapplied_and_short_for_finance_only(self):
        w = World()
        ar(w, "SYN-IN-1001", "500.00", "500.00")            # paid in full, still open
        short = ar(w, "SYN-IN-1002", "800.00", "800.00")     # paid 700 + 20 discount
        ar(w, "SYN-IN-1003", "300.00", "0", paid=True)        # applied: fine
        ar(w, "SYN-IN-2001", "1250.00", "1250.00", party="SYNTHOTHER")
        ap(w, "SYN-VI-77", "90.00", "90.00")
        sheets = Sheets(self, w)
        sheets.add("Remittance SYN.xlsx", remittance(
            [("SYN-IN-1001", "500.00", "0.00"), ("SYN-IN-1002", "700.00", "20.00"),
             ("IN-1003", "300.00", "0"), ("SYN-NOPE-9", "1.00", "0")]))
        sheets.add("eft_advice.csv", bank_remittance([("SYN-IN-2001", "1250.00")]))
        sheets.add("Payment advice.xlsx", remittance([("SYN-VI-77", "90.00", "0")]))  # ours
        counts = sheets.read()
        self.assertEqual(counts["readings"], {"remittance": 6})
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][ps.CASH_RULE], "complete")
        unapplied = {g["summary"].split(" from customer ")[1].split()[0]: g
                     for g in self.kinds(ps.UNAPPLIED)}
        self.assertEqual(set(unapplied), {"SYNTHCUST", "SYNTHOTHER"})
        self.assertEqual(unapplied["SYNTHCUST"]["summary"],
                         f"Remittance of {day(12)} from customer SYNTHCUST pays 1 invoice "
                         "still open in our books (500.00 CAD open in our books)")
        (gap,) = self.kinds(ps.SHORT)
        self.assertEqual(gap["object_id"], short)
        self.assertIn("pays 1 invoice less than they owe (80.00 CAD short", gap["summary"])
        self.assertEqual(summary["not_evaluated"][ps.CASH_RULE]["reference_not_matched"], 1)
        # finance only; a reader who may not open the mailbox sees nothing it supports
        self.assertEqual(self.kinds(ps.UNAPPLIED, held=frozenset({"l5", "pii"})), [])
        self.assertEqual(self.kinds(ps.SHORT, may_read=lambda a: a != BOX), [])
        self.assertEqual(self.kinds(ps.UNAPPLIED, may_read=lambda a: a != BOX), [])

    def test_a_supplier_statement_differs_and_one_we_sent_is_not_a_supplier_statement(self):
        w = World()
        ap(w, "SYN-B-501", "100.00", "100.00")               # agrees
        differs = ap(w, "SYN-B-502", "250.00", "250.00")     # they say 275
        ap(w, "SYN-B-503", "60.00", "0", paid=True)           # we paid; they say open
        ap(w, "SYN-B-504", "40.00", "40.00", dated=20)        # ours, not on the statement
        ap(w, "SYN-B-505", "70.00", "70.00", party="SYNTHELSE", dated=20)
        ar(w, "SYN-IN-3001", "10.00", "10.00")
        sheets = Sheets(self, w)
        head = ["Date", "Invoice No.", "Amount", "Balance"]
        rows, running = [], 0.0
        for ref, amount in (("SYN-B-501", 100), ("SYN-B-502", 275), ("SYN-B-503", 60),
                            ("SYN-B-599", 15)):
            running += amount
            rows.append([day(35, "%m/%d/%Y"), ref, f"{amount:.2f}", f"{running:.2f}"])
        sheets.add("Statement of account.xlsx", workbook(
            [["Statement"], ["Statement Date:", day(5, "%m/%d/%Y")], [], head] + rows))
        sheets.add("Statement older.xlsx", workbook(  # superseded by the later one
            [["Statement"], ["Statement Date:", day(60, "%m/%d/%Y")], [], head,
             [day(70, "%m/%d/%Y"), "SYN-B-501", "999.00", "999.00"]]))
        sheets.add("Statement.xlsx", workbook(       # one we sent: our receivable
            [["Statement"], [], ["Date", "Invoice", "Amount", "Balance"],
             [day(30, "%m/%d/%Y"), "SYN-IN-3001", "10.00", "10.00"]]))
        sheets.read()
        summary = self.run_rules(w)
        self.assertEqual(summary["not_evaluated"][ps.STATEMENT_RULE],
                         {"superseded_statement": 1})
        (gap,) = self.kinds(ps.DIFFERENCE)
        self.assertEqual(gap["object_id"], differs)
        self.assertEqual(gap["event"]["by_class"], {"amount_differs": 1,
                                                    "not_in_our_books": 1,
                                                    "not_on_statement": 1,
                                                    "paid_in_our_books": 1})
        self.assertEqual(gap["summary"],
                         f"Statement of {day(5)} from supplier SYNTHVEND differs from our "
                         "payables on 4 items (1 amount differs, 1 not in our books, 1 not "
                         "on statement, 1 paid in our books; net 60.00 CAD)")
        self.assertEqual(self.kinds(ps.DIFFERENCE, held=frozenset({"l5"})), [])

    def test_a_statement_listing_none_of_our_open_invoices_is_hidden_with_its_mailbox(self):
        w = World()
        ap(w, "SYN-B-501", "100.00", "100.00")
        ap(w, "SYN-B-504", "40.00", "40.00", dated=20)   # open, not on the statement
        sheets = Sheets(self, w)
        sheets.add("Statement of account.xlsx", workbook(
            [["Statement"], ["Statement Date:", day(5, "%m/%d/%Y")], [],
             ["Date", "Invoice No.", "Amount"],
             [day(35, "%m/%d/%Y"), "SYN-B-501", "100.00"]]))
        sheets.read()
        self.run_rules(w, ps.STATEMENT_RULE)
        (gap,) = self.kinds(ps.DIFFERENCE)
        self.assertIn("1 not on statement", gap["summary"])
        self.assertEqual(self.kinds(ps.DIFFERENCE, may_read=lambda a: a != BOX), [])

    def test_bank_and_account_details_are_never_stored(self):
        text = workbook([["Remittance advice"],
                         ["Payment Date:", day(12, "%m/%d/%Y")],
                         ["Account No:", "SYN-1234567"], ["Bank Account", "9988776655"],
                         ["IBAN", "SYN00ZZ123"], ["Routing / Transit", "000123456"],
                         ["Vendor", "Synthetic Person"], ["Card number: 4111111111111111"],
                         ["Currency", "Synthetic Bank Account CAD"],
                         ["Statement Date", "Synthetic Account Holder"],
                         [], ["Invoice Reference", "Amount Paid", "Bank Ref"],
                         ["SYN-IN-1", "10.00", "SYN-BANK-REF-9"]])
        (table,) = sr.read_text(text, "workbook", "remittance.xlsx")
        stored = json.dumps(table)
        for secret in ("SYN-1234567", "9988776655", "SYN00ZZ123", "000123456",
                       "Synthetic Person", "4111111111111111", "SYN-BANK-REF-9",
                       "Synthetic Bank", "Account Holder"):
            self.assertNotIn(secret, stored)
        self.assertEqual(set(table["facts"]), {"payment date", "_first_date", "currency"})
        self.assertEqual(table["facts"]["currency"][0], "CAD")
        self.assertNotIn("Bank Ref", table["roles"].values())

    def test_one_ten_and_sixty_sheets_of_three_layouts_each_count_exactly_once(self):
        layouts = (
            lambda code, qty: workbook([["Inventory", "", day(3, "%m/%d/%Y")], [],
                                        ["Item", "Description", "On Hand", "Unit"],
                                        [code, "Synthetic soap", qty, "kg"]]),
            lambda code, qty: workbook([["Stock report"], ["As of:", day(3, "%Y-%m-%d")],
                                        ["Pack Code", "Customer Item #", "Description",
                                         "On Hand", "UOM"],
                                        [code, "CUST-1", "Synthetic", qty, "kg"]],
                                       sheet="Stock"),
            lambda code, qty: "ITEMNO,DESC,QTY ON HAND,STOCKING UNIT\n"
                              f"{code},Synthetic,{qty},kg\n",
        )
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                w = World()
                products = []
                sheets = Sheets(self, w)
                for i in range(n):
                    code = f"SYNTH-ITEM-{i}"
                    p = item(w, ())
                    w.rows["object_values"] = [
                        {**v, "value": code} if v["object_id"] == p and v["property"] == "code"
                        else v for v in w.rows["object_values"]]
                    products.append(p)
                    # dates spread: many orders sharing one date read as a placeholder date
                    sales_order(w, p, "50", day(-1 - i % 25))
                    name = ("inventory.xlsx", "stock.xlsx", f"onhand_{day(3)}.csv")[i % 3]
                    sheets.add(name, layouts[i % 3](code, "20"))
                    ar(w, f"SYN-IN-{5000 + i}", "10.00", "10.00")
                    sheets.add(f"remit{i}.xlsx", remittance([(f"SYN-IN-{5000 + i}", "10.00",
                                                              "0")]))
                counts = sheets.read(workers=4)
                self.assertEqual(counts["readings"], {"on_hand": n, "remittance": n})
                self.run_rules(w, pb.STOCK_OUT, ps.CASH_RULE, on_hand_sources=())
                found = [g["object_id"] for g in self.kinds(pb.STOCK_OUT)]
                self.assertEqual(sorted(found), sorted(products))
                self.assertIn("30 kg short", self.kinds(pb.STOCK_OUT)[0]["summary"])
                self.assertEqual(len(self.kinds(ps.UNAPPLIED)), n)

    def test_people_are_cleared_first_and_an_unloaded_exclusion_blocks(self):
        w = World()
        ar(w, "SYN-IN-1001", "500.00", "500.00")
        ar(w, "SYN-IN-1002", "40.00", "40.00")
        ar(w, "SYN-IN-1003", "30.00", "30.00")
        p = item(w, ())
        sales_order(w, p, "50", day(-5))
        sheets = Sheets(self, w)
        sheets.add("remit a.xlsx", remittance([("SYN-IN-1001", "500.00", "0")]),
                   participants=(OUTSIDE, EXCLUDED))
        sheets.add("remit b.xlsx", remittance([("SYN-IN-1002", "40.00", "0")]),
                   participants=("",))            # an address that cannot be decided
        sheets.add("remit c.xlsx", remittance([("SYN-IN-1003", "30.00", "0")]),
                   in_store=False)                # no record to cite
        sheets.add("remit d.xlsx", remittance([("SYN-IN-1003", "30.00", "0")]),
                   audience=f"mail:{EXCLUDED_OID}")   # an excluded person's mailbox
        sheets.add("inventory.xlsx", workbook([["Item", "On Hand", "Unit"],
                                               ["SYNTH-ITEM", "20", "kg"]]),
                   participants=(EXCLUDED,))
        sheets.read()
        summary = self.run_rules(w, ps.CASH_RULE, pb.STOCK_OUT, on_hand_sources=())
        self.assertEqual(self.kinds(ps.UNAPPLIED), [])
        self.assertEqual(self.kinds(pb.STOCK_OUT), [])
        self.assertEqual(summary["not_evaluated"][ps.CASH_RULE],
                         {"people_not_cleared": 3, "source_not_in_store": 1})
        self.setUp()
        sheets.read()
        blocked = self.run_rules(w, *ps.RULES, pb.STOCK_OUT, on_hand_sources=(),
                                 people=people(unavailable=True))
        self.assertEqual(blocked["detectors"][ps.CASH_RULE], "blocked")
        self.assertEqual(blocked["detectors"][ps.STATEMENT_RULE], "blocked")
        self.assertEqual(blocked["detectors"][pb.STOCK_OUT], "blocked")   # fails closed
        self.assertEqual(self.kinds(pb.STOCK_OUT), [])

    def test_without_a_readings_file_the_sheet_rules_are_not_set_up_and_the_read_complete(
            self):
        w = World()
        ar(w, "SYN-IN-1001", "500.00", "500.00")
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"], {ps.CASH_RULE: "not_set_up",
                                                ps.STATEMENT_RULE: "not_set_up"})
        read = self.read()
        self.assertFalse(set(ps.RULES) & set(read["not_run"]))

    def test_a_pdf_laid_out_by_position_keeps_its_columns(self):
        page = "\n".join([
            "                         REMITTANCE ADVICE",
            "  Payment Date      Payment Reference",
            "  10/02/2026        SYN-PAY-7",
            "",
            "                                                  Amount",
            "  Invoice Number      Invoice Date      Discount      Paid",
            "  SYN-IN-1001         09/01/2026            0.00    500.00",
            "  SYN-IN-1002         09/03/2026           10.00  1,250.00",
            "  SYN-IN-1003                                        75.5"])
        (table,) = sr.read_text(page + "\f" + "Page 2 notes", "layout", "advice.pdf")
        self.assertEqual(table["kind"], "remittance")
        self.assertNotIn("payment date", table["facts"])    # a label beside a label
        self.assertEqual([(r["ref"][0], r["amount_paid"][0], r.get("discount", [None])[0],
                           r["ref"][1]) for r in table["rows"]],
                         [("SYN-IN-1001", "500.00", "0.00", "p1L7"),
                          ("SYN-IN-1002", "1250.00", "10.00", "p1L8"),
                          ("SYN-IN-1003", "75.5", None, "p1L9")])

    def test_layout_detection_types_and_cites_cells(self):
        tables = sr.read_text(workbook(
            [["Inventory count"], ["Count Date:", "45580"],
             ["Item #", "Lot", "Qty On Hand", "Exp Date"],
             ["SYN-1", "L-9", "1,250 cs", "31/12/2026"], ["SYN-2", "", "(5)", "01/02/2027"],
             ["Total", "", "1245"]], top=3), "workbook", "count.xlsx")
        (table,) = tables
        self.assertEqual(table["kind"], "on_hand")
        first, second = table["rows"]
        self.assertEqual(tuple(first["qty"]), ("1250", "C6"))
        self.assertEqual(tuple(first["unit"]), ("cs", "C6"))
        self.assertEqual(tuple(first["expiry"]), ("2026-12-31", "D6"))   # day first
        self.assertEqual(tuple(second["qty"]), ("-5", "C7"))
        self.assertEqual(sr.amount("1,234.50-"), sr.amount("(1234.50)"))
        self.assertIsNone(sr.amount("12 Main St"))
        self.assertIsNone(sr.parse_date("31/31/2026"))
        # a header with no key column, or prose, is no table
        self.assertEqual(sr.read_text(workbook([["Date", "Amount"], ["x", "1"]]),
                                      "workbook", "a.xlsx"), [])
        self.assertEqual(sr.read_text("Dear customer, please find attached.", "text",
                                      "statement.pdf"), [])

    def test_the_step_reads_each_version_once_and_picks_up_new_ones(self):
        w = World()
        sheets = Sheets(self, w)
        sheets.add("remit.xlsx", remittance([("SYN-IN-1", "1.00", "0")]))
        self.assertEqual(sheets.read()["versions_new"], 1)
        again = sheets.read()
        self.assertEqual((again["versions_new"], again["versions_known"]), (0, 1))
        sheets.add("late arrival.xlsx", remittance([("SYN-IN-2", "2.00", "0")]))
        self.assertEqual(sheets.read()["versions_new"], 1)
        missing = sr.Source("graph-mail://synth/x", "mail", "gone.xlsx", "0" * 64, "1" * 64,
                            None)
        sheets.sources.append(missing)
        self.assertEqual(sheets.read()["status"], {"text_missing": 1})


if __name__ == "__main__":
    import unittest
    unittest.main()
