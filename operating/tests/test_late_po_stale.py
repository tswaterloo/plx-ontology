"""The late purchase orders' two groups and the closed ones (``operating.page_reads``):
live, stale ("probably closed or missed in the data") and closed are exclusive and together
every late order; a closed order is in no list, count or supplier tally; a sign of life (a
receipt, a message after the needed-by date, a change to the record) keeps a very late order
live; no closed order to measure from (no longest lead time) means nothing is stale. The
queries run on a throwaway Postgres holding the minimal views they read."""
from __future__ import annotations

import unittest

from operating import page_reads as pr
from operating.tests import pg_testdb

SETUP = """
CREATE TABLE purchase_orders (handle text, po_number text, supplier text, days_late int,
  promised_date date, requested_date date, order_date date, status text, total numeric,
  total_currency text, purchaser text, line_count int, withheld boolean, source_systems text,
  verdict text);
CREATE TABLE record_links (link_type text, to_handle text);
CREATE TABLE alerts (rule text, state text, handle text);
CREATE TABLE records (handle text, record_id text, changed_at text, first_seen text);
"""
D = "2026-01-01"


def po(h, late, supplier="S", status="open", ordered="2025-12-01", requested=D, verdict="late"):
    return (h, h.upper(), supplier, late, None, requested, ordered, status, None, None, None, 1,
            False, "bc,portal", verdict)


class Stale(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        self.c = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.c.close)
        self.c.execute(SETUP)

    def add(self, *pos):
        for row in pos:
            self.c.execute("INSERT INTO purchase_orders VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                           "%s,%s,%s,%s)", row)

    def q(self, name, **params):
        params.setdefault("closed", "")
        if "after" in pr.QUERIES[name].params:
            params.setdefault("after", "")
        if "alive" in pr.QUERIES[name].params:
            params.setdefault("alive", "")
        cols = pr.QUERIES[name]
        self.assertEqual(set(params), set(cols.params), name)   # every parameter named, none extra
        return self.c.execute(cols.sql, params).fetchall()

    def split(self, closed="", alive=""):
        a = {"closed": closed, "alive": alive}
        live = [r[9] for r in self.q("late_po_list", **a, limit=500, offset=0)]
        stale = [r[9] for r in self.q("late_po_stale_list", **a, limit=500, offset=0)]
        self.assertEqual(self.q("late_po_count", **a)[0][0], len(live))
        self.assertEqual(self.q("late_po_stale_count", **a)[0][0], len(stale))
        return live, stale

    def lead_basis(self):
        """One closed order given 45 days: the longest ever asked."""
        self.add(po("c1", None, status="closed", ordered="2025-11-01", requested="2025-12-16",
                    verdict="neither"))

    def test_nothing_is_stale_while_there_is_no_closed_order_to_measure(self):
        self.add(po("a", 400))
        self.assertEqual(self.split(), (["a"], []))

    def test_a_very_late_order_with_no_sign_of_life_is_stale_and_a_less_late_one_is_not(self):
        self.lead_basis()
        self.add(po("old", 136), po("recent", 30), po("edge", 45), po("edge1", 46))
        live, stale = self.split()
        self.assertEqual(sorted(stale), ["edge1", "old"])       # later than 45 days, nothing else
        self.assertEqual(sorted(live), ["edge", "recent"])

    def test_each_sign_of_life_keeps_a_very_late_order_live(self):
        self.lead_basis()
        self.add(*[po(h, 200) for h in ("rcpt", "mail", "chat", "chg", "bare")])
        self.c.execute("INSERT INTO record_links VALUES ('Receipt.receives', 'rcpt')")
        self.c.execute("INSERT INTO records VALUES ('chg', 'r-chg', '2026-03-01T10:00:00Z', "
                       "'2025-12-02T10:00:00Z')")
        # a message after the needed-by date is a sign of life the API reads and passes as ``alive``
        live, stale = self.split(alive="mail\nchat")
        self.assertEqual(sorted(live), ["chat", "chg", "mail", "rcpt"])
        self.assertEqual(stale, ["bare"])

    def test_activity_before_the_needed_by_date_or_no_change_at_all_is_no_sign_of_life(self):
        self.lead_basis()
        self.add(po("early", 200), po("same", 200))
        self.c.execute("INSERT INTO records VALUES ('same', 'r-same', '2025-12-02T10:00:00Z', "
                       "'2025-12-02T10:00:00Z')")      # first seen = last changed: never changed
        self.assertEqual(self.split(), ([], ["early", "same"]))
        cands = [r[0] for r in self.q("late_po_candidates")]     # both are asked of the mail index
        self.assertEqual(sorted(cands), ["early", "same"])

    def test_a_closed_order_leaves_every_list_count_and_supplier_tally(self):
        self.lead_basis()
        self.add(*[po(f"p{k:02d}", 10 + k, supplier=f"S{k % 3}") for k in range(60)],
                 po("s1", 500, supplier="S0"))
        for n in (1, 10, 60):                                  # no order lost, none repeated
            closed = [f"p{k:02d}" for k in range(n)]
            live, stale = self.split("\n".join(closed))
            self.assertEqual(set(live), {f"p{k:02d}" for k in range(60) if 10 + k <= 45}
                             - set(closed))               # the 45-day lead: later ones are stale
            self.assertTrue(set(closed).isdisjoint(live) and set(closed).isdisjoint(stale))
            self.assertEqual(len(live) + len(stale), 61 - n)
            self.assertEqual(len(set(live)), len(live))
            tally = sum(r[1] for r in self.q("late_po_suppliers", closed="\n".join(closed),
                                             limit=500))
            self.assertEqual(tally, len(live))
        before = self.split("")[1]
        after = self.split("s1")[1]                            # closing a stale one leaves its group
        self.assertEqual(after, [h for h in before if h != "s1"])

    def test_an_order_only_the_alert_finds_late_is_also_asked_of_the_mail_index(self):
        self.lead_basis()
        self.add(po("viaalert", 300, verdict="neither"))
        self.c.execute("INSERT INTO alerts VALUES ('po_late', 'open', 'viaalert')")
        self.assertEqual([r[0] for r in self.q("late_po_candidates")], ["viaalert"])

    def test_closing_one_order_never_closes_another_whose_name_it_holds(self):
        self.add(po("p1", 5), po("p10", 6))
        live, _ = self.split("p1")
        self.assertEqual(live, ["p10"])


if __name__ == "__main__":
    unittest.main()
