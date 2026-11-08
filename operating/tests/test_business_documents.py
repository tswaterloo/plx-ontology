"""Customer and supplier sheets read for one reader (``operating.business_documents``,
``plx.aggregate`` mode ``business_documents``, over ``operating.document_tables``): the
generic table reader, forecast accuracy against orders, price sheets against invoices
(finance), fill lists, the answer bank, the issue log and UAT status; cardinality
N=1/10/60, a restricted reader, and the people exclusion failing closed.

SYNTHETIC fixtures only (``World`` stores, made-up ids, codes and addresses); evidence
label: isolated-fixture.
"""

from __future__ import annotations

from datetime import date
import json
import sqlite3
import tempfile
import time
import unittest
import zlib
from pathlib import Path

from operating import business_documents as bd
from operating import document_tables as dt
from operating.tests.test_proactive_engine import T0, World, handle

MAILBOX = "00000000-0000-4000-8000-0000000000b1"
BOX = f"mail:{MAILBOX}"
EVERY = frozenset({"finance", "pii", "l5", "purchasing_prices", "quality_controlled"})
AS_OF = date(2026, 11, 8)


class Guard:
    """A people exclusion bound to the tenant: clears every unit but ``blocked``."""

    def __init__(self, blocked=(), unavailable=None):
        self.blocked = set(blocked)
        self.unavailable = unavailable
        self.internal = frozenset({"synth-tenant.example"})

    def unit_clear(self, key):
        return key not in self.blocked


def sheet(rows: list[list[str]], name: str = "Sheet1") -> str:
    lines = [f"[Sheet: {name}]"]
    for r, row in enumerate(rows, start=1):
        lines.append(" | ".join(f"{dt._letters(c)}{r}={v}" for c, v in enumerate(row, 1)
                                if v != ""))
    return "\n".join(lines)


class Fixture:
    def __init__(self) -> None:
        self.w = World()
        self.texts: dict[str, str] = {}
        self.codes: dict[str, str] = {}

    def customer(self, name: str, domain: str) -> str:
        return self.w.obj("Customer", {"name": name, "bc_email": f"buyer@{domain}"})

    def item(self, code: str) -> str:
        oid = self.w.obj("Product", {"code": code, "name": f"Thing {code}"})
        self.codes[oid] = code
        return oid

    def mailed(self, name: str, text: str, *, sender: str, to: str = "us@synth-tenant.example",
               sent: str = "2026-01-05T10:00:00Z", audience: str = BOX) -> str:
        msg = self.w.obj("Email", {"subject": "sheet", "sender": sender,
                                   "to_addresses": json.dumps([to]), "sent_at": sent},
                         audience=audience, binding="mail")
        att = self.w.obj("Attachment", {"name": name}, audience=audience, binding="mail")
        self.w.link("Attachment.attached_to", att, msg)
        self.texts[att] = text
        return att

    def order(self, customer: str, item: str, qty: str, day: str) -> str:
        so = self.w.obj("SalesOrder", {"order_date": day, "order_number": f"SO{self.w.n}"})
        line = self.w.obj("SalesOrderLine", {"ordered_quantity": (qty, "EA")})
        self.w.link("SalesOrder.ordered_by", so, customer)
        self.w.link("SalesOrderLine.line_of", line, so)
        self.w.link("SalesOrderLine.for_item", line, item)
        return so

    def invoice(self, customer: str, item: str, qty: str, amount: str, day: str, *,
                audience: str = "api:a") -> str:
        inv = self.w.obj("SalesInvoice", {"posting_date": day}, markings='["finance"]',
                         audience=audience)
        line = self.w.obj("SalesInvoiceLine", {"invoiced_quantity": (qty, "EA", "invoiced"),
                                               "amount_excluding_tax": amount},
                          markings='["finance"]', audience=audience)
        self.w.link("SalesInvoice.billed_to", inv, customer)
        self.w.link("SalesInvoiceLine.line_of", line, inv)
        self.w.link("SalesInvoiceLine.for_item", line, item)
        return line

    def build(self, path: Path) -> sqlite3.Connection:
        self.w.build(path)
        conn = sqlite3.connect(path)
        handles = {r["object_id"]: r["l1_handle"] for r in self.w.rows["object_versions"]}
        for oid, text in self.texts.items():
            cur = conn.execute(
                "INSERT INTO text_fields (object_id, version_handle, citation, field_path, "
                "document_kind, sender_class, markings, text_digest) VALUES "
                "(?, ?, ?, 'row.content_text', 'attachment', 'external', '[]', 'x')",
                (oid, handles[oid], handles[oid]))
            conn.execute("INSERT INTO text_chunks (field_id, start, body) VALUES (?, 0, ?)",
                         (cur.lastrowid, zlib.compress(text.encode())))
        for oid, code in self.codes.items():
            conn.execute(
                "INSERT INTO identifiers (identifier_kind, normalized, raw, role, object_id, "
                "property, l1_handle, field_path, lineage, markings, recorded_at) VALUES "
                "('item_code', ?, ?, 'primary', ?, 'code', ?, 'row.code', 'workbench', '[]', ?)",
                (code.upper(), code, oid, handles[oid], T0))
        conn.commit()
        return conn


def forecast_world(n: int) -> tuple[Fixture, str, list[str]]:
    f = Fixture()
    cust = f.customer("Synth Buyer Co", "cust-a.example")
    items = [f.item(f"SYN-{i:03d}") for i in range(n)]
    rows = [["Item", "Description", "Jan-26", "Feb-26", "Mar-26"]]
    for i, item in enumerate(items):
        rows.append([f"SYN-{i:03d}", "thing", "10", "20", "30"])
        f.order(cust, item, "12", "2026-01-15")
        f.order(cust, item, "20", "2026-02-03")
        f.order(cust, item, "6", "2026-03-20")
    f.mailed("Customer Forecast 2026.xlsx", sheet(rows), sender="planner@cust-a.example")
    return f, cust, items


class BusinessDocuments(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.n = 0

    def conn(self, f: Fixture) -> sqlite3.Connection:
        self.n += 1
        conn = f.build(self.dir / f"store{self.n}.sqlite")
        self.addCleanup(conn.close)
        return conn

    def ask(self, conn, view, *, may_read=lambda k: True, held=EVERY, guard=None, **args):
        return bd.answer(conn, view=view, may_read=may_read, held=held,
                         guard=Guard() if guard is None else guard, as_of=AS_OF, args=args)

    # ------------------------------------------------------------------ reader

    def test_table_reader_finds_roles_periods_and_cells(self) -> None:
        text = sheet([["Customer forecast"], ["Item #", "Description", "Jan", "February 2026",
                                              "2026-03", "Price / Unit"],
                      ["A-100", "x", "5", "6", "7", "$4.50"]], name="2026 plan")
        [t] = dt.tables(text)
        self.assertEqual(t.header_row, 2)
        self.assertEqual(t.roles["item"], 1)
        self.assertEqual(sorted(t.periods.values()), [(2026, 1), (2026, 2), (2026, 3)])
        self.assertTrue(t.year_inferred)   # "Jan" took the sheet name's year
        self.assertEqual(t.cell(t.rows[0], "price").address, "F3")
        self.assertEqual(dt.money("$4.50"), (dt.Decimal("4.50"), None))
        self.assertEqual(dt.money("CAD 1,299.00"), (dt.Decimal("1299.00"), "CAD"))
        self.assertIsNone(dt.month_of("Janitor"))
        [plain] = dt.tables("sku,qty,ship date\nA-1,4,2026-01-02\n")
        self.assertEqual(set(plain.roles), {"item", "quantity", "date"})

    # ------------------------------------------------------------------ forecasts

    def test_forecast_accuracy_scores_against_the_customers_orders(self) -> None:
        f, cust, items = forecast_world(1)
        out = self.ask(self.conn(f), "forecast_accuracy")
        [row] = out["rows"]
        self.assertEqual((row["customer"], row["item"]), (cust, items[0]))
        self.assertEqual((row["forecast_total"], row["actual_total"]), ("60", "38"))
        # |10-12| + |20-20| + |30-6| = 26 over 38 actual
        self.assertEqual(row["wape"], "0.684")
        self.assertEqual(row["bias"], "0.579")
        self.assertIn(cust, [c["object_id"] for c in row["cites"]])

    def test_forecast_cardinality_1_10_60(self) -> None:
        for n in (1, 10, 60):
            f, cust, items = forecast_world(n)
            conn = self.conn(f)
            started = time.monotonic()
            lines = self.ask(conn, "forecasts")
            scored = self.ask(conn, "forecast_accuracy")
            self.assertLess(time.monotonic() - started, 20)
            self.assertEqual(len(lines["rows"]), 3 * n)
            self.assertEqual(len({(r["item"], r["period"]) for r in lines["rows"]}), 3 * n)
            self.assertEqual(sorted(r["item"] for r in scored["rows"]), sorted(items))

    def test_a_sheet_from_an_unknown_or_public_sender_names_no_customer(self) -> None:
        f, _, _ = forecast_world(1)
        f.mailed("Forecast copy.xlsx", sheet([["Item", "Jan-26"], ["SYN-000", "99"]]),
                 sender="someone@gmail.com")
        out = self.ask(self.conn(f), "forecasts")
        self.assertEqual(out["counts"]["forecast_sender_not_one_customer"], 1)
        self.assertNotIn("99", [r["quantity"] for r in out["rows"]])

    # ------------------------------------------------------------------ gates

    def test_restricted_reader_and_people_exclusion(self) -> None:
        f, _, _ = forecast_world(2)
        conn = self.conn(f)
        hidden = self.ask(conn, "forecasts", may_read=lambda k: k != BOX)
        self.assertEqual(hidden["rows"], [])
        self.assertGreater(hidden["withheld_counts"]["document_not_readable"], 0)
        excluded = self.ask(conn, "forecast_accuracy", guard=Guard(blocked={BOX}))
        self.assertEqual(excluded["rows"], [])
        for guard in (Guard(unavailable="excluded_people_oids_absent"), object()):
            out = self.ask(conn, "forecasts", guard=guard)
            self.assertEqual((out["rows"], out["withheld"], out["complete"]),
                             ([], "people_exclusion_unavailable", False))
        out = bd.answer(conn, view="forecasts", may_read=lambda k: True, held=EVERY,
                        guard=None, as_of=AS_OF)
        self.assertEqual(out["withheld"], "people_exclusion_unavailable")
        self.assertIn("error", self.ask(conn, "no_such_view"))

    # ------------------------------------------------------------------ prices

    def test_price_sheet_against_invoices_is_finance_only(self) -> None:
        f = Fixture()
        cust = f.customer("Synth Buyer Co", "cust-a.example")
        a, b = f.item("SYN-A1"), f.item("SYN-B2")
        f.mailed("Price list 2026.xlsx",
                 sheet([["Item #", "Description", "Price"], ["SYN-A1", "a", "USD 10.00"],
                        ["SYN-B2", "b", "USD 5.00"]]),
                 sender="rep@synth-tenant.example", to="buyer@cust-a.example")
        f.invoice(cust, a, "4", "40", "2026-02-01")       # 10.00: as the sheet says
        f.invoice(cust, b, "10", "40", "2026-02-01")      # 4.00: under the sheet's 5.00
        f.invoice(cust, b, "10", "50", "2025-06-01")      # before the sheet: not compared
        conn = self.conn(f)
        # the invoices' amounts state their currency (the store keeps it beside the value)
        conn.execute("UPDATE value_cells SET currency = 'USD' WHERE field IN (SELECT field "
                     "FROM value_fields WHERE property = 'amount_excluding_tax')")
        conn.commit()
        out = self.ask(conn, "price_sheets")
        got = {r["item"]: r for r in out["rows"]}
        self.assertEqual(got[a]["status"], "match")
        self.assertEqual((got[b]["status"], got[b]["mismatched_lines"]), ("mismatch", 1))
        self.assertEqual(got[b]["currency_basis"], "stated")
        self.assertTrue(all(r["markings"] == ["finance"] for r in out["rows"]))
        blind = self.ask(conn, "price_sheets", held=frozenset({"pii"}))
        self.assertEqual((blind["rows"], blind["withheld_counts"]),
                         ([], {"withheld_finance": 1}))

    # ------------------------------------------------------------------ fill lists

    def test_priority_list_orders_get_fill_status_and_alerts(self) -> None:
        f = Fixture()
        cust = f.customer("Synth Buyer Co", "cust-a.example")
        late = f.w.obj("SalesOrder", {"order_number": "SO-77001",
                                      "requested_delivery_date": "2026-10-01"})
        open_ = f.w.obj("SalesOrder", {"order_number": "SO-77002"})
        for so in (late, open_):
            f.w.link("SalesOrder.ordered_by", so, cust)
        f.mailed("Priority fill list.xlsx",
                 sheet([["Priority", "PO #"], ["1", "SO-77001"], ["2", "SO-77002"],
                        ["3", "150"]]), sender="buyer@cust-a.example")
        conn = self.conn(f)
        for so, number in ((late, "SO-77001"), (open_, "SO-77002")):
            conn.execute("INSERT INTO identifiers VALUES ('order_number', ?, ?, 'primary', ?, "
                         "'order_number', ?, 'row.order_number', 'workbench', '[]', ?, NULL)",
                         (number, number, so, handle(1), T0))
        conn.commit()
        out = self.ask(conn, "priority_fill")
        got = {r["listed"]: r for r in out["rows"]}
        self.assertEqual((got["SO-77001"]["status"], got["SO-77001"]["alert"]), ("late", True))
        self.assertEqual(got["SO-77002"]["status"], "not_scheduled")
        self.assertNotIn("150", got)   # a rank or quantity is no order number
        self.assertEqual(out["counts"]["listed_not_a_document_number"], 1)
        # a store without the value word index (a Postgres store that does not serve it)
        # finds the same listed orders by reading the documents whole
        class NoWordIndex:
            def __init__(self, inner):
                self.inner = inner

            def execute(self, sql, *args):
                if "MATCH" in sql:
                    raise sqlite3.OperationalError("the word index is not on Postgres")
                return self.inner.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.inner, name)
        whole = self.ask(NoWordIndex(conn), "priority_fill")
        self.assertEqual({r["listed"]: (r["status"], r["alert"]) for r in whole["rows"]},
                         {k: (r["status"], r["alert"]) for k, r in got.items()})

    # ------------------------------------------------------------------ answers, UAT

    def test_answer_bank_is_a_cited_draft(self) -> None:
        f = Fixture()
        f.customer("Synth Buyer Co", "cust-a.example")
        f.mailed("Supplier questionnaire.xlsx",
                 sheet([["Section A"], ["Do you hold a GMP certificate?", "Yes, since 2019"],
                        ["Is the site allergen free?", "TRUE"], ["FALSE", "Other"]]),
                 sender="qa@cust-a.example")
        out = self.ask(self.conn(f), "answer_bank", question="GMP certificate held?")
        self.assertTrue(out["draft"])
        self.assertEqual(out["rows"][0]["answer"], "Yes, since 2019")
        self.assertEqual(out["rows"][0]["cell"], "B2")
        self.assertNotIn("FALSE", [r["question"] for r in out["rows"]])

    def test_uat_status_classes_and_unknown_words(self) -> None:
        self.assertEqual(bd.status_class("Failed Retest"), "open")
        self.assertEqual(bd.status_class("Ready for UAT"), "fixed")
        self.assertEqual(bd.status_class("Deferred"), "answered")
        self.assertEqual(bd.status_class("Banana"), "unknown")
        self.assertEqual(bd.status_class(None), "unknown")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------- customer of a document

class CustomerResolution(unittest.TestCase):
    """``CustomerOf``: each basis in its order, conflicts and shared domains name nobody."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite"

    def resolve(self, f: Fixture, att: str):
        conn = f.build(self.path)
        self.addCleanup(conn.close)
        gate = dt.Gate(conn, may_read=lambda k: True, held=EVERY, guard=Guard())
        found = dt.find_documents(conn, ["sheet*"])
        [doc] = [d for d in found.docs if d.object_id == att]
        dt.texts(conn, [doc])
        return bd.CustomerOf(conn, gate)(doc)

    def test_sender_link_wins(self) -> None:
        f = Fixture()
        cust = f.customer("Linked Buyer Co", "linked.example")
        att = f.mailed("sheet one.xlsx", "x", sender="a@other.example")
        msg = [l["to_object"] for l in f.w.rows["links"] if l["from_object"] == att][0]
        f.w.link("Email.sent_by_customer", msg, cust)
        self.assertEqual(self.resolve(f, att), (cust, "sender_link", []))

    def test_two_linked_customers_name_nobody(self) -> None:
        f = Fixture()
        a = f.customer("First Buyer Co", "first.example")
        b = f.customer("Second Buyer Co", "second.example")
        att = f.mailed("sheet one.xlsx", "x", sender="buyer@first.example")
        msg = [l["to_object"] for l in f.w.rows["links"] if l["from_object"] == att][0]
        f.w.link("Email.sent_by_customer", msg, a)
        f.w.link("Email.sent_by_customer", msg, b)
        self.assertEqual(self.resolve(f, att)[0], None)

    def test_sender_domain(self) -> None:
        f = Fixture()
        cust = f.customer("Domain Buyer Co", "dom-buyer.example")
        att = f.mailed("sheet one.xlsx", "x", sender="planner@dom-buyer.example")
        self.assertEqual(self.resolve(f, att), (cust, "sender_domain", []))

    def _thread(self, f: Fixture, sender: str, customer: str) -> str:
        so = f.w.obj("SalesOrder", {"order_number": f"SO{f.w.n}"})
        f.w.link("SalesOrder.ordered_by", so, customer)
        msg = f.w.obj("Email", {"subject": "order", "sender": sender}, audience=BOX,
                      binding="mail")
        f.w.link("Email.about_order", msg, so)
        return msg

    def test_sender_threads_cite_the_tied_messages(self) -> None:
        f = Fixture()
        cust = f.w.obj("Customer", {"name": "Thread Buyer Co"})   # no address on record
        tied = [self._thread(f, "ops@threadbuyer.example", cust) for _ in range(2)]
        att = f.mailed("sheet one.xlsx", "x", sender="planner@threadbuyer.example")
        who, basis, evidence = self.resolve(f, att)
        self.assertEqual((who, basis), (cust, "sender_threads"))
        self.assertEqual(sorted(evidence), sorted(tied))

    def test_threads_split_between_customers_name_nobody(self) -> None:
        f = Fixture()
        a = f.w.obj("Customer", {"name": "Split Buyer One"})
        b = f.w.obj("Customer", {"name": "Split Buyer Two"})
        for c in (a, a, b, b):
            self._thread(f, "ops@splitbuyer.example", c)
        att = f.mailed("sheet one.xlsx", "x", sender="planner@splitbuyer.example")
        self.assertEqual(self.resolve(f, att)[0], None)

    def test_domain_shared_by_many_customers_does_not_resolve(self) -> None:
        f = Fixture()
        for name in ("Alpha Holdings Ltd", "Beta Supplies Inc", "Gamma Retail Co"):
            f.customer(name, "shared-group.example")
        att = f.mailed("sheet one.xlsx", "x", sender="buyer@shared-group.example")
        self.assertEqual(self.resolve(f, att), (None, None, []))

    def test_forwarded_original_sender(self) -> None:
        f = Fixture()
        cust = f.customer("Forward Buyer Co", "fwd-buyer.example")
        att = f.mailed("sheet one.xlsx", "x", sender="rep@synth-tenant.example")
        msg = [l["to_object"] for l in f.w.rows["links"] if l["from_object"] == att][0]
        f.w.rows["object_values"].append(dict(
            [r for r in f.w.rows["object_values"] if r["object_id"] == msg][0],
            property="body_preview", field_path="row.body_preview",
            value="FYI\n-----Original Message-----\nFrom: Pat <pat@fwd-buyer.example>\n"))
        self.assertEqual(self.resolve(f, att), (cust, "forwarded_sender_domain", []))

    def test_our_sheet_to_one_customer(self) -> None:
        f = Fixture()
        cust = f.customer("Recipient Buyer Co", "rcpt-buyer.example")
        att = f.mailed("sheet one.xlsx", "x", sender="rep@synth-tenant.example",
                       to="buyer@rcpt-buyer.example")
        self.assertEqual(self.resolve(f, att), (cust, "recipient_domain", []))

    def test_named_in_document_only_when_one_name_matches(self) -> None:
        f = Fixture()
        cust = f.customer("Northwind Grooming Ltd", "nw.example")
        f.customer("Southwind Traders Ltd", "sw.example")
        att = f.mailed("Northwind Grooming sheet.xlsx", "x", sender="x@unknown.example")
        self.assertEqual(self.resolve(f, att), (cust, "named_in_document", []))
        g = Fixture()
        g.customer("Northwind Grooming Ltd", "nw.example")
        g.customer("Southwind Traders Ltd", "sw.example")
        att2 = g.mailed("Northwind Grooming and Southwind Traders sheet.xlsx", "x",
                        sender="x@unknown.example")
        self.path = self.path.with_name("store2.sqlite")
        self.assertEqual(self.resolve(g, att2)[0], None)


# ---------------------------------------------------------------------- the inbox rule

class PriorityAlertRule(unittest.TestCase):
    """``priority_fill_unmet`` raises ``priority_order_unfilled`` for listed orders late or
    unscheduled, citing what a reader must be able to read; blocked without the exclusion."""

    def setUp(self) -> None:
        from unittest import mock
        from operating import proactive_engine as pe
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Fixture()
        cust = f.customer("Synth Buyer Co", "cust-a.example")
        self.late = f.w.obj("SalesOrder", {"order_number": "SO-88001",
                                           "requested_delivery_date": "2026-10-01"})
        self.open = f.w.obj("SalesOrder", {"order_number": "SO-88002"})
        self.ahead = f.w.obj("SalesOrder", {"order_number": "SO-88003",
                                            "requested_delivery_date": "2026-12-01"})
        for so in (self.late, self.open, self.ahead):
            f.w.link("SalesOrder.ordered_by", so, cust)
        self.att = f.mailed("Priority fill list.xlsx",
                            sheet([["Priority", "PO #"], ["1", "SO-88001"], ["2", "SO-88002"],
                                   ["3", "SO-88003"]]), sender="buyer@cust-a.example")
        path = Path(tmp.name) / "store.sqlite"
        conn = f.build(path)
        for so, n in ((self.late, "SO-88001"), (self.open, "SO-88002"),
                      (self.ahead, "SO-88003")):
            conn.execute("INSERT INTO identifiers VALUES ('order_number', ?, ?, 'primary', ?, "
                         "'order_number', ?, 'row.order_number', 'workbench', '[]', ?, NULL)",
                         (n, n, so, handle(1), T0))
        conn.commit()
        conn.close()
        self.pe, self.mock = pe, mock
        self.st = pe._Store(path)
        self.addCleanup(self.st.close)
        self.msg = [l["to_object"] for l in f.w.rows["links"] if l["from_object"] == self.att][0]

    def run_rule(self, guard):
        pe = self.pe
        with self.mock.patch.object(pe, "_people", return_value=guard):
            return bd.priority_fill_unmet(self.st, AS_OF, gap=pe._gap, cites=pe._cites,
                                          result=pe._result, settled=pe.SETTLED,
                                          skips=pe._Skips)

    def test_late_and_unscheduled_orders_alert_with_their_citations(self) -> None:
        out = self.run_rule(Guard())
        by_order = {g["object_id"] if "object_id" in g else g["groups"][0][0][1]: g
                    for g in out["gaps"]}
        kinds = {g["kind"] for g in out["gaps"]}
        self.assertEqual(kinds, {bd.KIND})
        self.assertEqual(len(out["gaps"]), 2)
        self.assertEqual(out["counts"]["late"], 1)
        self.assertEqual(out["counts"]["not_scheduled"], 1)
        self.assertEqual(out["counts"]["scheduled"], 1)
        for g in out["gaps"]:
            cited = {c[1] for grp in g["groups"] for c in grp}
            # a reader who may not open the list's mailbox cannot read the alert
            self.assertTrue({self.att, self.msg} <= cited)
            self.assertTrue(cited & {self.late, self.open})
            self.assertNotIn(self.ahead, cited)
        self.assertTrue(by_order)

    def test_a_restricted_reader_gets_no_listed_order(self) -> None:
        out = bd.answer(self.st.conn, view="priority_fill", may_read=lambda k: k != BOX,
                        held=EVERY, guard=Guard(), as_of=AS_OF)
        self.assertEqual(out["rows"], [])

    def test_people_exclusion_blocks_the_rule(self) -> None:
        for guard in (None, Guard(unavailable="excluded_people_oids_absent")):
            out = self.run_rule(guard)
            self.assertEqual((out["status"], out["gaps"]), ("blocked", []))
        excluded = self.run_rule(Guard(blocked={BOX}))
        self.assertEqual(excluded["gaps"], [])


# ---------------------------------------------------------------------- review probes

class ReviewProbes(unittest.TestCase):
    """A figure only from lines the reader may open; a list matched only to its own
    customer's orders; one customer's specific answer never in another's draft."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def conn(self, f: Fixture, name: str = "s.sqlite") -> sqlite3.Connection:
        conn = f.build(self.dir / name)
        self.addCleanup(conn.close)
        return conn

    def test_price_figures_use_only_lines_the_reader_may_open(self) -> None:
        f = Fixture()
        cust = f.customer("Synth Buyer Co", "cust-a.example")
        item = f.item("SYN-P1")
        f.mailed("Price list.xlsx", sheet([["Item #", "Price"], ["SYN-P1", "USD 10.00"]]),
                 sender="rep@synth-tenant.example", to="buyer@cust-a.example")
        seen = f.invoice(cust, item, "1", "10", "2026-02-01")
        hidden = f.invoice(cust, item, "1", "3", "2026-02-02", audience="api:secret")
        conn = self.conn(f)
        conn.execute("UPDATE value_cells SET currency = 'USD' WHERE field IN (SELECT field "
                     "FROM value_fields WHERE property = 'amount_excluding_tax')")
        conn.commit()
        full = bd.answer(conn, view="price_sheets", may_read=lambda k: True, held=EVERY,
                         guard=Guard(), as_of=AS_OF)["rows"][0]
        self.assertEqual((full["invoice_lines"], full["status"], full["worst_invoiced_price"]),
                         (2, "mismatch", "3.0000"))
        out = bd.answer(conn, view="price_sheets", may_read=lambda k: k != "api:secret",
                        held=EVERY, guard=Guard(), as_of=AS_OF)
        [row] = out["rows"]
        self.assertEqual((row["invoice_lines"], row["mismatched_lines"], row["status"],
                          row["worst_invoiced_price"]), (1, 0, "match", "10.0000"))
        cited = {c["object_id"] for c in row["cites"]}
        self.assertIn(seen, cited)
        self.assertNotIn(hidden, cited)
        self.assertEqual(out["withheld_counts"]["invoice_line_not_readable"], 1)

    def _list_world(self, sender: str):
        f = Fixture()
        a = f.customer("Alpha Buyer Co", "alpha.example")
        b = f.customer("Beta Buyer Co", "beta.example")
        b_order = f.w.obj("SalesOrder", {"order_number": "SO-99001"})
        f.w.link("SalesOrder.ordered_by", b_order, b)
        f.mailed("Priority list.xlsx", sheet([["Priority", "PO #"], ["1", "SO-99001"]]),
                 sender=sender)
        conn = self.conn(f, sender.split("@")[1] + ".sqlite")
        conn.execute("INSERT INTO identifiers VALUES ('order_number', 'SO-99001', 'SO-99001', "
                     "'primary', ?, 'order_number', ?, 'row.order_number', 'workbench', '[]', "
                     "?, NULL)", (b_order, handle(1), T0))
        conn.commit()
        return conn

    def test_customer_a_list_never_attaches_customer_b_order(self) -> None:
        conn = self._list_world("buyer@alpha.example")
        out = bd.answer(conn, view="priority_fill", may_read=lambda k: True, held=EVERY,
                        guard=Guard(), as_of=AS_OF)
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["counts"]["listed_not_this_customers_order"], 1)

    def test_a_list_from_no_known_customer_attaches_nothing(self) -> None:
        conn = self._list_world("someone@unknown-sender.example")
        out = bd.answer(conn, view="priority_fill", may_read=lambda k: True, held=EVERY,
                        guard=Guard(), as_of=AS_OF)
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["counts"]["list_customer_unresolved"], 1)

    def test_customer_a_specific_answer_stays_out_of_customer_b_draft(self) -> None:
        f = Fixture()
        a = f.customer("Alpha Buyer Co", "alpha.example")
        b = f.customer("Beta Buyer Co", "beta.example")
        f.supplier = f.w.obj("Supplier", {"name": "Gamma Materials", "bc_email":
                                          "qa@gamma-supply.example"})
        f.mailed("Customer questionnaire.xlsx",
                 sheet([["Section"], ["Do you hold a GMP certificate?", "Yes, since 2019"],
                        ["What price will you hold for this year?", "USD 4.10 per case"]]),
                 sender="qa@alpha.example")
        f.mailed("Supplier questionnaire.xlsx",
                 sheet([["Section"], ["Do you hold a GMP certificate?", "No, pending"]]),
                 sender="qa@gamma-supply.example")
        conn = self.conn(f)

        def draft(customer):
            out = bd.answer(conn, view="answer_bank", may_read=lambda k: True, held=EVERY,
                            guard=Guard(), as_of=AS_OF, args={"subject": customer})
            return {r["answer"]: r for r in out["rows"]}, out["counts"]

        for_b, counts = draft(b)
        self.assertEqual(set(for_b), {"Yes, since 2019"})
        self.assertEqual(for_b["Yes, since 2019"]["scope"], "generic")
        self.assertEqual(for_b["Yes, since 2019"]["party_id"], a)
        self.assertEqual(counts["specific_to_another_customer"], 1)
        self.assertEqual(counts["supplier_questionnaire"], 1)   # never pooled
        for_a, _ = draft(a)
        self.assertEqual(set(for_a), {"Yes, since 2019", "USD 4.10 per case"})
        self.assertEqual(for_a["USD 4.10 per case"]["scope"], "customer_specific")
        anyone, _ = draft(None)
        self.assertEqual(set(anyone), {"Yes, since 2019"})


class SpecificByDefault(unittest.TestCase):
    """An answer is generic only with no figure, no customer name word and no terms word."""

    def test_reviewer_cases_stay_with_their_customer(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Fixture()
        f.customer("Alpha Buyer Co", "alpha.example")
        beta = f.customer("Beta Buyer Co", "beta.example")
        f.customer("Gammaco", "gammaco.example")
        f.w.obj("Contact", {"name": "Pat Lindqvist"})   # a contact of the store, lower-cased
        f.mailed("Customer questionnaire.xlsx", sheet([
            ["Section"],
            ["What is your minimum run?", "MOQ 500 cases at 12.40/case"],
            ["Do you supply anyone exclusively?", "Exclusive supply agreement with Alpha until 2027"],
            ["Who audits your site?", "audited jointly with Gammaco"],
            ["Is your site certified?", "We are GMP certified"],
            ["How large is your plant?", "35,000 litres a month"],
            ["Who is your contact?", "Jane Whitfield, buyer"],
            ["Where do notices go?", "notices go to buyer@cust-a.example"],
            ["Who signs off?", "our planner pat lindqvist"],
            ["Where is the plant?", "120 Harbour Road, Toronto ON M5V 2T6"],
            ["Is there a phone line?", "call +1 (416) 555-0199"],
            ["Is there a web page?", "see www.synth-site.example"],
            ["Which portal do you use?", "orders via synthportal.xyz"],
            ["Is your facility certified?", "JANE WHITFIELD signs our GMP file"],
            ["What is your preferred delivery day?", "Tuesdays"],
            ["Does your warehouse hold stock?", "Yes, consignment stock held free of charge for you"],
            ["What is your standard lead time?", "Yes, guaranteed five weeks with late penalties paid to you"],
            ["What is the standard lead time?", "Five weeks, contractually committed; late shipments are discounted"]]),
            sender="qa@alpha.example")
        conn = f.build(Path(tmp.name) / "s.sqlite")
        self.addCleanup(conn.close)
        out = bd.answer(conn, view="answer_bank", may_read=lambda k: True, held=EVERY,
                        guard=Guard(), as_of=AS_OF, args={"subject": beta})
        self.assertEqual([r["answer"] for r in out["rows"]], ["We are GMP certified"])
        self.assertEqual(out["rows"][0]["scope"], "generic")
        # a neutral answer to a question that is no company fact stays with its customer
        self.assertEqual(out["counts"]["specific_to_another_customer"], 16)
