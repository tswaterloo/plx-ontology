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
            for name in ("0040_people_excluded_objects.sql", "0041_business_views.sql"):
                c.execute((MIGRATIONS_DIR / name).read_text())
        bv.build(cls.dsn, None, schema=tbv.SCHEMA, as_of=tbv.AS_OF)
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
        """{currency: (total, documents)} of the owed section."""
        sec = Pages.section(page, key)
        return {r[0].text: (Decimal(r[6].money.amount), int(r[7].number)) for r in sec.rows}

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
        # a supplier held three times: one result
        self.assertEqual([x for x in top("croda") if x[0] == "Supplier"],
                         [("Supplier", "Croda Canada")])
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
        for actor, page in ((FULL, full), (OPS, ops)):
            rows = self.sql("SELECT amount_due_currency, sum(amount_due), count(*) FROM "
                            f"customer_balances WHERE customer_handle = '{h}' "
                            "AND amount_due_currency IS NOT NULL GROUP BY 1", actor)
            want = {r[0]: (Decimal(str(r[1] or 0)), int(r[2])) for r in rows}
            self.assertEqual({k: (v[0].normalize(), v[1]) for k, v in self.owed(page).items()},
                             {k: (v[0].normalize(), v[1]) for k, v in want.items()}, actor)
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
