"""The company (party) and chain columns of the customer views: NULL until the parties are
built (0066), then every customer row and every row of a sales view carries its customer's
party; a company's name is shown only from a row of the same access class; views built under
other parties are stale; 1/10/60 rows each carry once."""
from __future__ import annotations

import unittest

from operating.pg import business_views as bv
from operating.pg import parties as P
from operating.tests import test_business_views as tbv


class PartyColumns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        tbv.BusinessViews.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute((MIGRATIONS_DIR / "0066_parties.sql").read_text())

    def conn(self):
        import psycopg
        c = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(c.close)
        return c

    def test_columns_are_declared_on_the_six_views_only(self):
        for name, (_, cols) in P.PARTY_VIEWS.items():
            self.assertEqual([c for c in cols if bv.VIEWS[name].col(c) is None], [], name)
        others = [n for n in bv.VIEWS if n not in P.PARTY_VIEWS]
        self.assertTrue(all(bv.VIEWS[n].col("party_id") is None for n in others))

    def test_null_until_built_then_every_customer_has_a_party_and_sales_rows_follow(self):
        c = self.conn()
        c.execute("DELETE FROM biz.party_members")
        c.execute("DELETE FROM biz.parties")
        bv.build(self.dsn, ["customers"], schema=tbv.SCHEMA, as_of=tbv.AS_OF)
        self.assertEqual(c.execute("SELECT count(*) FROM biz.customers WHERE party_id IS NOT NULL"
                                   ).fetchone()[0], 0)               # said: nothing built
        out = P.build(self.dsn)
        self.assertGreater(out["customer"]["parties"], 0)
        bv.build(self.dsn, ["customers", "sales_orders", "sales_invoices", "customer_balances",
                            "sales_invoice_lines", "receivable_documents"],
                 schema=tbv.SCHEMA, as_of=tbv.AS_OF)
        total, null = c.execute("SELECT count(*), count(*) FILTER (WHERE party_id IS NULL) "
                                "FROM biz.customers").fetchone()
        self.assertGreater(total, 0)
        self.assertEqual(null, 0)                                    # every customer has one
        # a sales row carries the party of its own customer, by handle, and nothing else
        bad = c.execute("SELECT count(*) FROM biz.sales_orders o JOIN biz.customers k ON "
                        "k.handle = o.customer_handle WHERE o.party_id IS DISTINCT FROM "
                        "k.party_id").fetchone()[0]
        self.assertEqual(bad, 0)
        rows = c.execute("SELECT count(*), count(*) FILTER (WHERE customer_handle IS NOT NULL "
                         "AND party_id IS NULL) FROM biz.sales_orders").fetchone()
        self.assertGreater(rows[0], 0)
        self.assertEqual(rows[1], 0)
        # the two accounts of one verified company are one party; the lead-only twins are not
        same = c.execute("SELECT count(DISTINCT party_id), count(*) FROM biz.customers WHERE "
                         "name ILIKE 'acme%' OR name ILIKE 'lead%'").fetchone()
        self.assertLess(same[0], same[1] + 1)
        # views built under these parties are current once the refresh noted it; other parties
        # (a changed digest) make them stale; nothing is added while no party is built
        P.record(c, list(P.PARTY_VIEWS), P.digest(P.lookup(c)))
        self.assertEqual(P.stale_with_parties(c, []), [])
        c.execute("UPDATE biz.state SET notes = notes::jsonb || '{\"parties\": \"other\"}'::jsonb "
                  "WHERE view = 'sales_orders'")
        self.assertEqual(P.stale_with_parties(c, []), ["sales_orders"])
        self.assertEqual(P.stale_with_parties(c, ["sales_orders"]), ["sales_orders"])

    def test_a_name_from_a_row_of_another_class_is_never_shown(self):
        c = self.conn()
        P.build(self.dsn)
        handle = c.execute("SELECT handle FROM biz.customers ORDER BY handle LIMIT 1").fetchone()[0]
        mine = c.execute("SELECT name FROM biz.customers WHERE handle = %s", (handle,)).fetchone()[0]
        c.execute("UPDATE biz.parties SET name = 'Another Row''s Name', access_class = "
                  "coalesce(access_class, 0) + 12345 WHERE party_id = (SELECT party_id FROM "
                  "biz.party_members WHERE handle = %s)", (handle,))
        self.addCleanup(P.build, self.dsn)
        self.assertEqual(P.lookup(c)[handle][1], mine)    # the member's own name, not that row's

    def test_carry_sets_each_row_once_for_1_10_60(self):
        class R:
            def __init__(self, h): self.handle, self.values = h, {"customer_handle": h}
        found = {f"h{i}": (f"p{i}", f"Name {i}", f"c{i}") for i in range(60)}
        for n in (1, 10, 60):
            rows = [R(f"h{i}") for i in range(n)]
            for r in rows:
                P.carry(r, "sales_orders", found)
            self.assertEqual([r.values["party_id"] for r in rows], [f"p{i}" for i in range(n)])
            self.assertEqual(rows[-1].values["customer_party"], f"Name {n - 1}")
        r = R("unknown")
        P.carry(r, "customers", found)
        self.assertEqual((r.values["party_id"], r.values["chain_id"]), (None, None))
        self.assertNotIn("customer_party", r.values)


if __name__ == "__main__":
    unittest.main()
