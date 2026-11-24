"""``record_comms`` (speed specs 8.2): the content-free index of the messages that name a record.
Five columns and no content; every message that names a purchase order's source record is a row for
each member record of the order (record pages are keyed per member); a second build writes nothing;
a message gone from its view, or no longer naming the record, loses its row; a handle that matches no
record is listed (never dropped silently); N = 1, 10, 60 messages cost the same statements."""
from __future__ import annotations

import unittest

import psycopg

from operating.pg import business_views as bv
from operating.pg import record_comms as rc
from operating.pg import record_pages as rp
from operator_api.tests import test_page_sql as tps
from tests import scale

S = bv.SCHEMA


class RecordComms(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tps.PageDoor.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute(f"CREATE TABLE IF NOT EXISTS {S}.emails (handle text PRIMARY KEY, "
                      "received_at timestamptz, records text[] NOT NULL DEFAULT '{}')")

    truth = tps.PageDoor.truth

    def orders(self):
        """(source record handle, [member record ids]) of purchase orders with 2+ members first."""
        with psycopg.connect(self.dsn, autocommit=True) as c:
            with c.transaction():
                rp.members(c, S)
                return c.execute(
                    "SELECT r.handle, array_agg(DISTINCT m.record_id ORDER BY m.record_id) FROM "
                    f"rp_map a JOIN rp_map m USING (hdr) JOIN {S}.records r ON r.record_id = "
                    "a.record_id GROUP BY a.record_id, r.handle ORDER BY count(DISTINCT "
                    "m.record_id) DESC, 1").fetchall()

    def put(self, rows):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"DELETE FROM {S}.emails")
            for i, (handle, recs) in enumerate(rows):
                c.execute(f"INSERT INTO {S}.emails (handle, received_at, records) VALUES (%s, "
                          "now() - make_interval(secs => %s), %s)", (handle, i, recs))

    def test_the_table_holds_exactly_the_five_columns_and_no_reader_role_reads_it(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            rc.ensure(c, S)
        cols = [r[0] for r in self.truth("SELECT column_name FROM information_schema.columns "
                                         "WHERE table_schema = %s AND table_name = 'record_comms' "
                                         "ORDER BY ordinal_position", (S,))]
        self.assertEqual(cols, list(rc.COLUMNS))
        self.assertEqual(self.truth("SELECT has_table_privilege('public', %s, 'SELECT')",
                                    (f"{S}.record_comms",)), [(False,)])

    def test_a_message_naming_an_order_is_a_row_for_every_member_record_of_it(self):
        orders = self.orders()
        handle, members = orders[0]
        self.assertGreaterEqual(len(members), 1)
        self.put([("mail-1", [handle]), ("mail-2", ["api://nowhere/%7B%7D@x-1"])])
        out = rc.build(self.dsn, S)
        self.assertEqual(out["rows"], {"emails": len(members)})
        self.assertEqual(out["unmatched"], [["emails", "nowhere", 1]])
        got = self.truth(f"SELECT record_id, comm_view, comm_handle, via FROM {S}.record_comms "
                         "ORDER BY 1")
        self.assertEqual(got, [(m, "emails", "mail-1", handle) for m in members])
        again = rc.build(self.dsn, S)
        self.assertEqual((again["inserted"], again["updated"], again["deleted"]), (0, 0, 0))
        self.put([("mail-1", [])])     # no longer names it
        self.assertEqual(rc.build(self.dsn, S)["deleted"], len(members))
        self.assertEqual(self.truth(f"SELECT count(*) FROM {S}.record_comms"), [(0,)])

    def test_building_for_1_10_60_messages_costs_the_same_statements(self):
        orders = self.orders()
        self.assertGreaterEqual(len(orders), 60)
        counts = {}
        with psycopg.connect(self.dsn, autocommit=True) as c:
            for n in scale.NS:
                self.put([(f"mail-{i}", [orders[i][0]]) for i in range(n)])
                with scale.counting(c) as got:
                    rc.apply(c, S)
                counts[n] = got[0]
        self.assertEqual(len(set(counts.values())), 1, counts)


if __name__ == "__main__":
    unittest.main()
