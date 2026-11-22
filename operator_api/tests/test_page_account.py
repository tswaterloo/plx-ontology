"""The one search box and the account / order pages (``operator_api.page_sql`` over
``plx.page``): one result per real thing by name or number; every number on a customer,
chain, supplier, PO and sales-order page equals the SQL truth for a full reader and what the
same door (``plx.sql``) gives a restricted one; a restricted reader never sees a row or
cell it may not, and is told what was left out; unknown input fails closed; a list keeps
every row at N = 1, 10 and 60. Postgres (the business views fixture of
``operating.tests.test_business_views``: the Take 5 chain, Acme, Beta's finance-marked
documents, Bulk 1/10/60)."""
from __future__ import annotations

import json
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote

from operating import ontology_tools_sql as sqlt
from operating import page_reads as pr
from operating.pg import business_views as bv
from operating.pg import sql_views
from operating.tests import test_business_views as tbv
from operating.tests import test_ontology_tools_table as tab
from operator_api import page_sql as ps

FULL, OPS, NOBODY = tab.FULL, tab.OPS, tab.NOBODY
BUDGET_MS = 2000


class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        from operating.tests import pg_store_mode
        cls.store = tbv.build()
        cls.dsn = pg_store_mode._load("", cls.store.conn)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            for name in ("0038_own_issued_documents.sql", "0040_people_excluded_objects.sql",
                         "0041_business_views.sql"):
                c.execute((MIGRATIONS_DIR / name).read_text())
        bv.build(cls.dsn, None, schema=tbv.SCHEMA, as_of=tbv.AS_OF)
        from operating.pg import generic_records
        # every record's own row, as served: the search box finds a shipment, a receipt, a
        # lot by the number its label carries (``records`` is a view the find reads)
        generic_records.build(cls.dsn, bv.SCHEMA, workers=1, uncovered=True, business=True)
        for attempt in range(5):   # the role is the cluster's: parallel modules may race
            try:
                sql_views.install(cls.dsn)
                break
            except psycopg.errors.InternalError:
                time.sleep(0.5 * (attempt + 1))
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute(f"GRANT {sql_views.ROLE} TO CURRENT_USER")
            c.execute(f"GRANT {sql_views.PAGE_ROLE} TO CURRENT_USER")

    @classmethod
    def tearDownClass(cls):
        for pools in (sqlt._POOLS, pr._POOLS):
            for pool in list(pools.values()):
                pool.close()
            pools.clear()

    def setUp(self):
        sqlt._DOOR.clear()
        sqlt._GRANTS.clear()

    # ---- helpers ----------------------------------------------------------------------

    def base(self):
        return tbv.BusinessViews.base(self)

    def truth(self, sql, args=()):
        """The business table itself, read as the build's role (no scope)."""
        import psycopg
        with psycopg.connect(self.dsn) as c:
            return c.execute(sql.format(biz=bv.SCHEMA), args).fetchall()

    def handle(self, table, col, value):
        got = self.truth(f"SELECT handle FROM {{biz}}.{table} WHERE {col} = %s", (value,))
        self.assertEqual(len(got), 1, (table, value, got))
        return got[0][0]

    def backend(self, actor):
        base = self.base()

        class View:
            def _call(self, name, args):
                assert name == pr.TOOL_NAME
                return pr.PageReads(base).call(args, actor)

        class Worker:
            def run(self, reader, job, timeout=0, priority=0):
                return job(View())
        return SimpleNamespace(access_worker=Worker(), reads=None)

    def sql(self, sql, actor):
        out = sqlt.SqlTool(self.base()).call("plx.sql", {"sql": sql}, actor)
        self.assertNotIn("error", out, out)
        return [[c for c in r["cells"]] for r in out["rows"]]

    def find(self, q, actor=FULL, limit=20):
        return ps.search(self.backend(actor), None, q, limit)

    def page(self, kind, actor=FULL, **key):
        t0 = time.monotonic()
        call = ps.order if kind in ("purchase_order", "sales_order") else ps.account
        if call is ps.order:
            got = call(self.backend(actor), None, kind, key["handle"])
        else:
            got = call(self.backend(actor), None, kind, **key)
        ms = (time.monotonic() - t0) * 1000
        self.assertLess(ms, BUDGET_MS, f"{kind} page took {ms:.0f} ms")
        return got

    @staticmethod
    def section(page, key):
        got = [s for s in page.sections if s.key == key]
        return got[0] if got else None

    @staticmethod
    def owed(page, key="owed"):
        """{currency: (total, documents)} of the owed section (total None: shown hidden)."""
        sec = Pages.section(page, key)
        return {r[0].text: (Decimal(r[6].money.amount) if r[6].money else None,
                            int(r[7].number)) for r in sec.rows
                if r[6].money is not None or r[6].hidden}

    @staticmethod
    def years(page):
        sec = Pages.section(page, "years")
        return {r[0].text: tuple(Decimal(c.money.amount) if c.money else None for c in r[1:])
                for r in sec.rows}

    # ---- the one search box -----------------------------------------------------------

    def test_search_finds_each_real_thing_once_by_name_or_any_number(self):
        def top(q, actor=FULL):
            got = self.find(q, actor)
            return [(s.type, s.label) for s in got]
        # a customer held in the Portal and Sage: one result, by name or code
        acme = [x for x in top("acme") if x[0] == "Customer"]
        self.assertEqual(acme, [("Customer", "Acme Ltd")])
        self.assertEqual(top("ACME")[0], ("Customer", "Acme Ltd"))
        self.assertEqual(self.find("ACME")[0].match, "exact")
        # a supplier held three times: one result (BC's separate payment vendor by the
        # same name is its own account, found as such)
        self.assertEqual([x for x in top("croda") if x[0] == "Supplier"],
                         [("Supplier", "Croda Canada"), ("Supplier", "CRODA CANADA (EFT)")])
        # a number opens its record: the PO, first, exact; its page is the order page
        po = self.find("PO-1")
        self.assertEqual((po[0].type, po[0].label, po[0].match),
                         ("PurchaseOrder", "PO-1", "exact"))
        self.assertTrue(po[0].open_url.startswith("/order?kind=purchase_order&h="))
        self.assertEqual(top("SO-1")[0], ("SalesOrder", "SO-1"))
        self.assertEqual(top("SB-1")[0][0], "Product")
        self.assertEqual(top("BC-1A"), [("Batch", "BC-1A")])
        # a chain is one result ahead of its stores; its head account is the chain
        t5 = self.find("take 5")
        self.assertEqual((t5[0].type, t5[0].label), ("Chain", "TAKE 5"))
        self.assertIn("3 accounts", t5[0].sublabel)
        self.assertEqual(sorted(s.label for s in t5[1:] if s.type == "Customer"),
                         ["TAKE 5 -Summerville", "TAKE 5 -Waverly", "Take 5 - Dallas"])
        parent = [x for x in top("parent co") if x[1] == "Parent Co"]
        self.assertEqual(parent, [("Chain", "Parent Co")])
        # an order is found by its number, never by its customer's name
        self.assertFalse([x for x in top("beta") if x[0] == "SalesOrder"])

    NUMBERED = (  # (type, system, label, whose class): records the box finds by number
        ("Shipment", "sage", "Shipment SH1000000001", "PO-1"),
        ("Shipment", "bc", "SH1000000001", "PO-1"),                 # a BC pilot copy
        ("Shipment", "sage", "Shipment SH10000000019", "PO-1"),     # starts with the number
        ("Shipment", "sage", "Shipment SH-HIDDEN-1", "PO-FIN"),
        ("Receipt", "sage", "Receipt RCP10000001", "PO-1"),
        ("Lot", "filemaker", "S220001", "PO-1"),
        ("SalesCreditMemo", "bc", "CM-1000", "PO-1"))

    def add_numbered(self):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:
            # the access class of a visible and of a finance-marked record: the PO rows' own
            cls = {po: c.execute(
                f"SELECT r.access_class FROM {bv.SCHEMA}.records r JOIN "
                f"{bv.SCHEMA}.purchase_orders p ON p.handle = r.handle WHERE p.po_number = %s "
                "LIMIT 1", (po,)).fetchone()[0] for po in ("PO-1", "PO-FIN")}
            c.execute(f"DELETE FROM {bv.SCHEMA}.records WHERE record_id LIKE 'numbered:%'")
            for i, (type_, system, label, po) in enumerate(self.NUMBERED):
                c.execute(
                    f"INSERT INTO {bv.SCHEMA}.records (rid, record_id, type, system, source, "
                    "handle, label, access_class, col_class, ai_ok, link_ok, fields, ver, biz) "
                    f"SELECT coalesce(max(rid), 0) + 1, %s, %s, %s, 'test', %s, %s, %s, 0, "
                    f"true, true, '{{}}'::jsonb, 1, true FROM {bv.SCHEMA}.records",
                    (f"numbered:{i}", type_, system, f"api://numbered/{i}", label, cls[po]))

    def test_search_finds_each_numbered_kind_exact_first_and_pilot_after_record(self):
        self.add_numbered()

        def top(q, actor=FULL):
            return [(s.type, s.label, s.match) for s in self.find(q, actor)]
        got = top("SH1000000001")
        # the Sage shipment first, then BC's pilot copy of the same number (same rank),
        # then the shipment whose number only starts with it
        self.assertEqual(got[:3], [("Shipment", "Shipment SH1000000001", "exact"),
                                   ("Shipment", "SH1000000001", "exact"),
                                   ("Shipment", "Shipment SH10000000019", "prefix")])
        self.assertEqual(top("sh1000000001")[0], got[0])        # any case
        self.assertEqual(top("RCP10000001")[0], ("Receipt", "Receipt RCP10000001", "exact"))
        self.assertEqual(top("s220001")[0], ("Lot", "S220001", "exact"))
        self.assertEqual(top("CM-1000")[0][:2], ("SalesCreditMemo", "CM-1000"))
        # a number prefix finds them after the exact ones
        self.assertEqual([x[1] for x in top("RCP100") if x[0] == "Receipt"],
                         ["Receipt RCP10000001"])
        # an invoice, a supplier document: by their numbers, exact, their own record pages
        inv = self.find("IN-1")
        self.assertEqual((inv[0].type, inv[0].label, inv[0].match), ("SalesInvoice", "IN-1",
                                                                      "exact"))
        self.assertTrue(inv[0].open_url.startswith("/object/"), inv[0].open_url)
        sup = self.find("V-INV-1")
        self.assertEqual((sup[0].type, sup[0].label), ("SupplierInvoice", "V-INV-1"))
        # a BC pilot invoice ranks after a system-of-record one of the same rank
        self.assertEqual([s.label for s in self.find("IN-") if s.type == "SalesInvoice"][:2],
                         ["IN-1", "IN-2"])
        self.assertEqual([s.label for s in self.find("PILOT-1")][:1], ["PILOT-1"])

    def test_numbered_search_shows_a_restricted_reader_nothing_it_may_not_read(self):
        self.add_numbered()
        self.assertEqual([s.label for s in self.find("SH-HIDDEN-1", FULL)],
                         ["Shipment SH-HIDDEN-1"])
        for q in ("SH-HIDDEN-1", "SH-HIDDEN", "IN-FIN", "RCP10000001"):
            with self.subTest(q=q):
                self.assertEqual([s.label for s in self.find(q, NOBODY)], [])
        for q in ("SH-HIDDEN-1", "IN-FIN"):
            self.assertFalse([s for s in self.find(q, OPS) if "HIDDEN" in s.label
                              or "FIN" in s.label], q)

    def test_search_fails_closed_and_is_never_a_pattern(self):
        for q in ("", "a", " ", "%%", "__", "%", "P%", "\\"):
            with self.subTest(q=q):
                self.assertEqual(self.find(q), [])
        out = pr.PageReads(self.base()).call({"reads": [{"name": "find", "params": {
            "q": "x" * 201, "limit": 5}}]}, FULL)
        self.assertEqual(out["error"]["code"], "invalid_request")
        out = pr.PageReads(self.base()).call({"reads": [{"name": "find", "params": {
            "q": "acme", "limit": 5, "sql": "1"}}]}, FULL)
        self.assertEqual(out["error"]["code"], "invalid_request")

    def test_search_shows_a_restricted_reader_nothing_it_may_not_read(self):
        self.assertTrue(self.find("PO-FIN", FULL))
        self.assertTrue(self.find("BC-SECRET", FULL))
        for q in ("PO-FIN", "BC-SECRET", "secret"):
            with self.subTest(q=q):
                self.assertFalse([s for s in self.find(q, OPS) if "FIN" in s.label
                                  or "SECRET" in s.label], q)
        for q in ("acme", "PO-1", "take 5", "croda"):
            self.assertEqual(self.find(q, NOBODY), [])

    # ---- the account page -------------------------------------------------------------

    def test_customer_page_numbers_are_the_sql_truth(self):
        h = self.handle("customers", "name", "Acme Ltd")
        page = self.page("customer", handle=h)
        self.assertEqual(page.title, "Acme Ltd")
        facts = {f.label: f.value.text for f in page.facts}
        self.assertIn("Sage ACME", facts["Codes"])
        self.assertIn("Portal ACME", facts["Codes"])
        truth = {cur: (Decimal(str(amt)).quantize(Decimal("0.01")), n) for cur, amt, n in self.truth(
            "SELECT amount_due_currency, sum(amount_due), count(*) FROM {biz}.customer_balances "
            "WHERE customer_handle = %s GROUP BY 1", (h,))}
        self.assertEqual({k: (v[0].quantize(Decimal("0.01")), v[1])
                          for k, v in self.owed(page).items()}, truth)
        self.assertEqual(truth["CAD"][0], Decimal("100.10"))
        # aged: INV-1 is 47 days past due on the fixture's day
        cad = [r for r in self.section(page, "owed").rows if r[0].text == "CAD"][0]
        self.assertEqual(Decimal(cad[3].money.amount), Decimal("100.10"))   # 31-60
        n_open = self.truth("SELECT count(*) FROM {biz}.sales_orders WHERE status = 'open' "
                            "AND customer_handle = %s", (h,))[0][0]
        self.assertEqual(self.section(page, "open_orders").total, n_open)
        inv = self.truth("SELECT invoice_number FROM {biz}.sales_invoices WHERE customer_handle "
                         "= %s AND NOT COALESCE(pilot, false) ORDER BY invoice_date DESC NULLS "
                         "LAST, invoice_number DESC, handle LIMIT 10", (h,))
        self.assertEqual([r[0].text for r in self.section(page, "invoices").rows],
                         [r[0] for r in inv])
        self.assertNotIn("PILOT-1", page.model_dump_json())   # BC pilot data is not used
        self.assertTrue(page.sources.startswith("Sources: "))
        self.assertNotIn("emails", [s.key for s in page.sections])   # no source yet: absent

    def test_cells_that_name_a_record_link_to_it(self):
        # an invoice opens its own record page (the record its row was read into); an item
        # code or an order number opens Records' search for it; an empty cell links nowhere
        h = self.handle("customers", "name", "Acme Ltd")
        page = self.page("customer", handle=h)
        invoices = self.section(page, "invoices").rows
        self.assertTrue(invoices)
        for r in invoices:
            self.assertTrue(r[0].href is None or r[0].href.startswith("/object/"), r[0])
            self.assertEqual(r[3].href is None, r[3].text is None, r[3])
        self.assertTrue(any(r[0].href for r in invoices))
        po = self.page("purchase_order", handle=self.handle("purchase_orders", "po_number", "PO-1"))
        for r in po.sections[0].rows:
            if r[0].text:
                self.assertEqual(r[0].href, "/records?q=" + quote(r[0].text, safe=""))
            else:
                self.assertIsNone(r[0].href)
        # a restricted reader's invoice cells link only to what its door returned
        for r in self.section(self.page("customer", OPS, handle=h), "invoices").rows:
            self.assertTrue(r[0].href is None or r[0].href.startswith("/object/"))

    def test_item_page_states_stock_from_the_stock_view_and_lists_its_batches(self):
        h = self.handle("products", "name", "Shampoo Base")
        page = ps.item(self.backend(FULL), None, h)
        self.assertEqual((page.kind, page.title), ("product", "Shampoo Base"))
        stock = self.truth("SELECT on_hand, on_hand_unit FROM {biz}.stock WHERE item_code = "
                           "(SELECT item_code FROM {biz}.products WHERE handle = %s)", (h,))
        self.assertEqual(len(stock), 1)
        on_hand = [f.value for f in page.facts if f.label == "On hand"][0]
        self.assertEqual(on_hand.number, f"{Decimal(stock[0][0]).normalize():f} {stock[0][1]}")
        batches = self.truth("SELECT batch_code FROM {biz}.batches WHERE item_code = (SELECT "
                             "item_code FROM {biz}.products WHERE handle = %s)", (h,))
        listed = self.section(page, "batches")
        self.assertEqual(sorted(r[0].text for r in listed.rows), sorted(b[0] for b in batches))
        self.assertIn("BC-1A", [r[0].text for r in listed.rows])
        for r in listed.rows:
            self.assertTrue(r[0].href is None or r[0].href.startswith("/object/Batch/"), r[0])
        # no raw field text or JSON reaches a cell
        cells = [f.value for f in page.facts] + [c for r in listed.rows for c in r]
        self.assertFalse([c for c in cells if "{" in (c.text or "")])
        # an item with no stated stock says so, never zero
        label = ps.item(self.backend(FULL), None, self.handle("products", "name", "Label"))
        self.assertTrue(label.facts)
        gone = self.truth("SELECT handle FROM {biz}.products WHERE name = 'Gone Item'")
        self.assertFalse(gone)   # a source-deleted item has no row, so no page
        with self.assertRaises(ps.NotFound):
            ps.item(self.backend(FULL), None, "no-such-handle")
        with self.assertRaises(ps.NotFound):
            ps.item(self.backend(FULL), None, "")

    def test_batch_page_shows_the_hold_and_what_a_restricted_reader_may_not_see_is_left_out(self):
        h = self.handle("batches", "batch_code", "BC-1A")
        page = ps.batch(self.backend(FULL), None, h)
        self.assertEqual((page.kind, page.title), ("batch", "Batch BC-1A"))
        facts = {f.label: f.value for f in page.facts}
        self.assertEqual(facts["Hold"].text, "QC holds this batch")
        self.assertEqual(facts["Product"].text, "Shampoo Base")
        qc = self.section(page, "qc")
        self.assertEqual([(r[0].text, r[1].text) for r in qc.rows][0], ("pH after 24 h", "5.6"))
        # the finance-marked batch has no page for a reader without finance access
        secret = self.truth("SELECT handle FROM {biz}.batches WHERE batch_code = 'BC-SECRET'")
        if secret:
            with self.assertRaises(ps.NotFound):
                ps.batch(self.backend(NOBODY), None, secret[0][0])
        with self.assertRaises(ps.NotFound):
            ps.batch(self.backend(FULL), None, "no-such-handle")

    def test_the_production_list_filters_pages_and_loses_no_batch_at_1_10_60(self):
        everything = self.truth("SELECT count(*) FROM {biz}.batches")[0][0]
        got = ps.record_list(self.backend(FULL), None, "production", limit=100)
        self.assertEqual(got.total, everything)
        # every batch exactly once across pages of 7, in the one order
        seen, offset = [], 0
        while True:
            page = ps.record_list(self.backend(FULL), None, "production", offset=offset, limit=7)
            seen += [r[0].href or r[0].text for r in page.table.rows]
            offset = page.next_offset
            if not page.more:
                break
        self.assertEqual(len(seen), everything)
        self.assertEqual(len(set(seen)), len(seen))
        # a filter is the SQL truth
        held = self.truth("SELECT count(*) FROM {biz}.batches WHERE on_hold IS TRUE")[0][0]
        self.assertEqual(ps.record_list(self.backend(FULL), None, "production", hold="yes").total, held)
        self.assertEqual(ps.record_list(self.backend(FULL), None, "production", hold="yes").table.rows[0][4].text, "On-Hold")
        by_text = ps.record_list(self.backend(FULL), None, "production", q="bc-1a")
        self.assertIn("BC-1A", [r[0].text for r in by_text.table.rows])
        # a pattern is text, never a pattern
        self.assertEqual(ps.record_list(self.backend(FULL), None, "production", q="%").total, 0)
        # a reader sees only the batches its door shows
        self.assertLessEqual(ps.record_list(self.backend(NOBODY), None, "production").total, everything)
        self.assertEqual(sorted(o.value for o in got.filters[0].options)[0], "")
        # unknown view and filter values fail closed
        for bad in (dict(view="quotes"), dict(view="production", hold="maybe")):
            with self.assertRaises(ps.NotFound):
                ps.record_list(self.backend(FULL), None, bad.pop("view"), **bad)

    def test_chain_page_rolls_up_its_stores(self):
        page = self.page("chain", chain="TAKE 5")
        self.assertEqual(page.subtitle, "Chain of 3 accounts")
        self.assertEqual(self.section(page, "accounts").total, 3)
        day = ps.business_day()
        truth = self.truth(
            "SELECT subtotal_currency, sum(subtotal) FILTER (WHERE invoice_date BETWEEN "
            "date_trunc('year', %s::date)::date AND %s::date), sum(subtotal) FILTER (WHERE "
            "invoice_date BETWEEN (date_trunc('year', %s::date) - interval '1 year')::date AND "
            "(%s::date - interval '1 year')::date) FROM {biz}.sales_invoices "
            "WHERE customer_chain = 'TAKE 5' AND NOT COALESCE(pilot, false) GROUP BY 1",
            (day, day, day, day))
        got = self.years(page)
        for cur, this_year, last in truth:
            self.assertEqual(got[cur][0], this_year)
            self.assertEqual(got[cur][1], last)
        self.assertEqual(got["CAD"][0] + got["CAD"][1], Decimal("160.00"))
        self.assertEqual(self.owed(page)["CAD"][0], Decimal("15.00"))
        self.assertEqual(self.section(page, "open_orders").total, 1)   # SO-T5, Dallas
        # a store's own page: its own figures, and the chain it belongs to
        store = self.page("customer", handle=self.handle("customers", "name",
                                                         "TAKE 5 -Summerville"))
        self.assertEqual(self.owed(store)["CAD"][0], Decimal("15.00"))
        chain = [f for f in store.facts if f.label == "Chain"][0].value
        self.assertEqual(chain.text, "TAKE 5 (3 accounts)")
        self.assertEqual(chain.href, ps.account_url("chain", chain="TAKE 5"))

    def test_restricted_reader_sees_only_what_it_may_and_hears_what_was_left_out(self):
        h = self.handle("customers", "name", "Beta Inc")
        full = self.page("customer", FULL, handle=h)
        ops = self.page("customer", OPS, handle=h)
        truth = self.truth("SELECT sum(amount_due), count(*) FROM {biz}.customer_balances "
                           "WHERE customer_handle = %s AND amount_due_currency = 'CAD'", (h,))[0]
        self.assertEqual(self.owed(full)["CAD"], (Decimal(str(truth[0])), truth[1]))
        dump = ops.model_dump_json()
        for secret in ("4444.44", "SECRET-FIN", "IN-FIN"):
            self.assertNotIn(secret, dump)
        # the same numbers the same door gives this reader through plx.sql
        # a currency holding an amount the reader may not see shows its total hidden,
        # never the sum of the rest as if it were all (11-18 staff walk: CAD 126,051.50
        # owed read "0.00")
        # (plx.sql reads the AI's views: a cell it masks beyond the page's is a NULL there,
        # so the shown totals are checked against its sums and the hidden ones by name)
        for actor, page in ((FULL, full), (OPS, ops)):
            rows = self.sql("SELECT amount_due_currency, sum(amount_due), count(*) FROM "
                            f"customer_balances WHERE customer_handle = '{h}' "
                            "AND amount_due_currency IS NOT NULL GROUP BY 1", actor)
            want = {r[0]: (Decimal(str(r[1] or 0)).normalize(), int(r[2])) for r in rows}
            got = self.owed(page)
            self.assertEqual(set(got), set(want), actor)
            for cur, (total, docs) in got.items():
                self.assertEqual(docs, want[cur][1], (actor, cur))
                if total is not None:
                    self.assertEqual(total.normalize(), want[cur][0], (actor, cur))
        self.assertEqual(self.owed(ops)["CAD"][0], None)          # INV-3's currency: hidden
        self.assertIsNotNone(self.owed(full)["CAD"][0])
        # INV-3's amount is finance-marked: counted, never summed as zero, and said so
        self.assertIn("can't see", self.section(ops, "owed").note or "")
        self.assertIsNone(self.section(full, "owed").note)
        with self.assertRaises(ps.NotFound):
            self.page("customer", NOBODY, handle=h)

    def test_supplier_page_numbers_are_the_sql_truth(self):
        h = self.handle("suppliers", "name", "Croda Canada")
        page = self.page("supplier", handle=h)
        self.assertEqual(page.title, "Croda Canada")
        truth = {cur: Decimal(str(amt)) for cur, amt in self.truth(
            "SELECT amount_due_currency, sum(amount_due) FROM {biz}.supplier_balances "
            "WHERE supplier_handle = %s GROUP BY 1", (h,))}
        self.assertEqual({k: v[0] for k, v in self.owed(page).items()}, truth)
        n_open, n_late = self.truth(
            "SELECT count(*), count(*) FILTER (WHERE verdict = 'late') FROM "
            "{biz}.purchase_orders WHERE status = 'open' AND supplier_handle = %s", (h,))[0]
        sec = self.section(page, "open_orders")
        self.assertEqual(sec.total, n_open)
        if n_late:
            self.assertIn(f"{n_late} late", sec.note)
        ops = self.page("supplier", OPS, handle=h)
        self.assertNotIn("PO-FIN", ops.model_dump_json())
        self.assertEqual(self.section(ops, "open_orders").total, int(self.sql(
            "SELECT count(*) FROM purchase_orders WHERE status = 'open' AND supplier_handle = "
            f"'{h}'", OPS)[0][0]))

    def test_a_supplier_page_lists_other_accounts_by_its_name_never_merged(self):
        """Served 11-18: BC's payment vendor "EZ CHEMICALS (EFT)" holds the balance while
        the POs name the purchasing vendor "EZ Chemicals" (the page said "None open"
        beside 57k owed). Each page lists the other account and its open POs; neither
        page's numbers include the other's."""
        croda = self.handle("suppliers", "name", "Croda Canada")
        eft = self.handle("suppliers", "name", "CRODA CANADA (EFT)")
        page = self.page("supplier", handle=eft)
        same = self.section(page, "same_name")
        open_pos = self.truth("SELECT count(*) FROM {biz}.purchase_orders WHERE status = "
                              "'open' AND supplier_handle = %s", (croda,))[0][0]
        self.assertGreater(open_pos, 0)
        self.assertEqual([(r[0].text, r[2].number) for r in same.rows],
                         [("Croda Canada", str(open_pos))])
        self.assertTrue(same.rows[0][0].href.endswith(quote(croda, safe="")))
        self.assertEqual(self.section(page, "open_orders").total, 0)   # never merged
        back = self.section(self.page("supplier", handle=croda), "same_name")
        self.assertEqual([r[0].text for r in back.rows], ["CRODA CANADA (EFT)"])
        other = self.page("supplier", handle=self.handle("suppliers", "name", "Other Chem"))
        self.assertIsNone(self.section(other, "same_name"))

    def test_a_zeroed_po_header_shows_its_lines_total_and_a_differing_header_both(self):
        def facts(number):
            page = self.page("purchase_order", handle=self.handle(
                "purchase_orders", "po_number", number))
            return {f.label: (f.value.money.amount if f.value.money else None)
                    for f in page.facts if "otal" in f.label}
        self.assertEqual(facts("PO-LN"), {"Total (sum of lines)": "112.50"})
        self.assertEqual(facts("PO-HL"), {"Total": "130", "Lines total": "100.00"})
        self.assertEqual(facts("PO-NP"), {"Total": "0"})

    # ---- Freightcom: a person sees it, the AI's path never does ------------------------

    def test_freightcom_is_on_the_persons_page_and_never_reaches_plx_sql(self):
        import psycopg
        h = self.handle("suppliers", "name", "FreightCom Inc")
        self.assertEqual(self.truth("SELECT ai_ok FROM {biz}.suppliers WHERE handle = %s",
                                    (h,)), [(False,)])
        # the person: the account page and the one search box both have it
        page = self.page("supplier", handle=h)
        self.assertEqual(page.title, "FreightCom Inc")
        self.assertEqual([s.label for s in self.find("FreightCom")], ["FreightCom Inc"])
        self.assertTrue([s for s in self.find("FC1") if "FreightCom" in s.label])
        # a reader not holding the row's class still does not (the rest of the door holds)
        self.assertFalse([s for s in self.find("FreightCom", NOBODY)])
        # the model's path: plx.sql never returns it, by name or by handle
        names = [r[0].get("untrusted_text") if isinstance(r[0], dict) else r[0]
                 for r in self.sql("SELECT name FROM suppliers", FULL)]
        self.assertIn("Croda Canada", names)
        self.assertNotIn("FreightCom Inc", names)
        self.assertEqual(int(self.sql(f"SELECT count(*) FROM suppliers WHERE handle = "
                                      f"'{h}'", FULL)[0][0]), 0)
        out = sqlt.SqlTool(self.base()).call("plx.sql", {
            "sql": f"SELECT name FROM {sql_views.page_schema()}.suppliers"}, FULL)
        self.assertIn("error", out)
        self.assertNotIn("FreightCom", json.dumps(out))
        # and the AI's role cannot read a person-only view at all (not by grant, not by
        # membership), whatever statement reaches the store
        with psycopg.connect(self.dsn) as c:
            self.assertEqual(c.execute(
                "SELECT pg_has_role(%s, %s, 'MEMBER'), pg_has_role(%s, %s, 'MEMBER'), "
                "has_schema_privilege(%s, %s, 'USAGE')",
                (sql_views.ROLE, sql_views.PAGE_ROLE, sql_views.PAGE_ROLE, sql_views.ROLE,
                 sql_views.ROLE, sql_views.page_schema())).fetchone(), (False, False, False))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with c.transaction():
                    c.execute("SET TRANSACTION READ ONLY")
                    c.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
                    c.execute(f"SELECT name FROM {sql_views.page_schema()}.suppliers")

    def test_only_the_persons_search_box_reads_the_person_views(self):
        """``suggest`` feeds instructions and forms a model works on: only the person's own
        box (``/api/suggest``, ``person=True``) goes through plx.page's person-only views."""
        import os
        from unittest import mock
        from operator_api.backend_serving import ServingMixin
        hit = ps.search(self.backend(FULL), None, "FreightCom", 5)
        self.assertEqual([s.label for s in hit], ["FreightCom Inc"])

        class Host(ServingMixin):
            def _pg_suggest(self, reader, q, limit):
                return []
        calls = []
        with mock.patch.dict(os.environ, {ps.SWITCH: "1"}), mock.patch.object(
                ps, "search", lambda *a: calls.append(a) or hit):
            for_model = Host().suggest(None, "FreightCom", 5)
            mine = Host().suggest(None, "FreightCom", 5, person=True)
        self.assertEqual((for_model.source, for_model.suggestions), ("search_entities", []))
        self.assertEqual((mine.source, [s.label for s in mine.suggestions]),
                         ("business", ["FreightCom Inc"]))
        self.assertEqual(len(calls), 1)

    def test_plx_page_is_no_model_tool(self):
        """The person-only views are read by plx.page alone: no model's catalog lists it."""
        from operating import agent_loop, mcp_server_catalog
        from operator_api import simple_ask
        catalogs = {"simple_ask.TOOLS": frozenset(simple_ask.TOOLS),
                    "agent_loop.LOOP_TOOLS": frozenset(agent_loop.LOOP_TOOLS)}
        catalogs.update({f"mcp_server_catalog.{k}": v for k, v in vars(
            mcp_server_catalog).items() if isinstance(v, frozenset)})
        for name, tools in catalogs.items():
            with self.subTest(catalog=name):
                self.assertNotIn(pr.TOOL_NAME, tools)

    # ---- the order pages --------------------------------------------------------------

    def test_order_pages_show_header_and_lines_from_the_line_tables(self):
        h = self.handle("purchase_orders", "po_number", "PO-1")
        page = self.page("purchase_order", handle=h)
        self.assertEqual((page.title, page.sections[0].key), ("PO-1", "lines"))
        truth = self.truth("SELECT ordered_quantity, received_quantity FROM "
                           "{biz}.purchase_order_lines WHERE po_handle = %s", (h,))
        got = [(Decimal(r[2].number), Decimal(r[3].number)) for r in page.sections[0].rows]
        self.assertEqual(sorted(got), sorted((Decimal(a), Decimal(b)) for a, b in truth))
        so = self.handle("sales_orders", "order_number", "SO-1")
        full = self.page("sales_order", handle=so)
        ops = self.page("sales_order", OPS, handle=so)
        self.assertEqual(len(full.sections[0].rows), len(ops.sections[0].rows))
        hidden = [r for r in ops.sections[0].rows if r[6].hidden]   # the finance-priced line
        self.assertEqual(len(hidden), 1)
        self.assertIsNone(hidden[0][6].money)
        self.assertFalse([r for r in full.sections[0].rows if r[6].hidden])
        party = [f for f in full.facts if f.label == "Customer"][0].value
        self.assertTrue(party.href.startswith("/account?kind=customer&h="))

    def test_unknown_records_and_kinds_fail_closed(self):
        for kind, key in (("customer", {"handle": "api://nope/1"}), ("customer", {"handle": ""}),
                          ("chain", {"chain": "No Such Chain"}), ("supplier", {"handle": ""}),
                          ("product", {"handle": "x"})):
            with self.subTest(kind=kind):
                with self.assertRaises(ps.NotFound):
                    ps.account(self.backend(FULL), None, kind, **key)
        with self.assertRaises(ps.NotFound):
            ps.order(self.backend(FULL), None, "purchase_order", "api://nope/1")
        out = pr.PageReads(self.base()).call({"reads": [{"name": "customer_sales_years",
                                                         "params": {"handle": "h", "chain": "",
                                                                    "as_of": "1; DROP"}}]}, FULL)
        self.assertEqual(out["error"]["code"], "invalid_request")

    def test_lists_keep_every_row_at_1_10_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                page = self.page("customer", handle=self.handle("customers", "name", f"Bulk {n}"))
                total, docs = self.owed(page)["USD"]
                self.assertEqual(docs, n)
                self.assertEqual(total, sum(Decimal(f"{k}.25") for k in range(n)))
                po = self.page("purchase_order",
                               handle=self.handle("purchase_orders", "po_number", f"PO-B{n}"))
                self.assertEqual(len(po.sections[0].rows), n)
                self.assertEqual(po.sections[0].total, n)


if __name__ == "__main__":
    unittest.main()
