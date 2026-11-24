"""Closing a purchase order in the Workbench (``operator_api.po_closing``): a person closes it with
an optional reason, anyone who may see it can, an order the reader may not see is refused, the close
shows who/when/why, undoing it works, a repeat changes nothing, new receipts and messages after a
close are said and never reopen it, and the late reads get the closed orders as their parameter.
Postgres (the migration's own table) with the door and the message index stood in for."""
from __future__ import annotations

import contextlib
import datetime as dt
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from operating.pg.migrate import MIGRATIONS_DIR
from operating.tests import pg_testdb
from operator_api import page_sql, po_closing
from operator_api.errors import UserFacing

H = "bc://x/purchaseOrders/1"


class Pool:
    def __init__(self, dsn):
        self.dsn = dsn

    @contextlib.contextmanager
    def connection(self):
        import psycopg
        with psycopg.connect(self.dsn) as c:
            yield c


class Closing(unittest.TestCase):
    def setUp(self):
        import psycopg
        name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("CREATE SCHEMA assist")
            c.execute((Path(MIGRATIONS_DIR) / "0080_po_closures.sql").read_text())
        self.backend = SimpleNamespace(memory=SimpleNamespace(pool=Pool(self.dsn)))
        self.sam = SimpleNamespace(display_name="Sam Buyer")
        self.rows = {"po_signs": [{"handle": H, "receipts": 1, "record_ids": ["r1"]}]}
        self.msgs: dict[str, list] = {}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(page_sql, "read", lambda b, r, reads, **k: (
            {x["name"]: self.rows.get(x["name"], []) for x in reads}, {})).start()
        mock.patch.object(po_closing, "_messages", lambda b, ids: {
            i: self.msgs[i] for i in ids if i in self.msgs}).start()

    def test_close_says_who_when_why_and_leaves_the_late_reads(self):
        self.assertEqual(po_closing.closed_param(self.backend), "")
        got = po_closing.close(self.backend, self.sam, H, " received ")
        self.assertEqual((got.closed, got.by, got.reason), (True, "Sam Buyer", "received"))
        self.assertTrue(got.at)
        self.assertEqual(po_closing.closed_param(self.backend), H)
        self.assertEqual(po_closing.params(self.backend, self.sam)["closed"], H)

    def test_no_reason_is_fine_and_a_repeat_adds_no_row(self):
        po_closing.close(self.backend, self.sam, H, None)
        po_closing.close(self.backend, self.sam, H, "again")
        import psycopg
        with psycopg.connect(self.dsn) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM assist.po_closures").fetchone()[0], 1)

    def test_undo_brings_it_back_and_a_new_close_is_possible(self):
        po_closing.close(self.backend, self.sam, H, "wrong data")
        self.assertFalse(po_closing.reopen(self.backend, self.sam, H).closed)
        self.assertEqual(po_closing.closed_param(self.backend), "")
        self.assertTrue(po_closing.close(self.backend, self.sam, H, None).closed)

    def test_an_order_the_reader_may_not_see_is_refused_and_nothing_is_written(self):
        self.rows["po_signs"] = []                      # the door returns no row for this reader
        for call in (lambda: po_closing.close(self.backend, self.sam, H, None),
                     lambda: po_closing.reopen(self.backend, self.sam, H),
                     lambda: po_closing.status(self.backend, self.sam, H, must_see=True)):
            with self.assertRaises(UserFacing) as e:
                call()
            self.assertEqual(e.exception.status, 404)
        self.assertEqual(po_closing.closed_param(self.backend), "")

    def test_a_long_reason_is_refused_not_cut(self):
        with self.assertRaises(UserFacing):
            po_closing.close(self.backend, self.sam, H, "x" * (po_closing.MAX_REASON + 1))
        self.assertEqual(po_closing.closed_param(self.backend), "")

    def test_activity_after_a_close_is_said_and_never_reopens_it(self):
        po_closing.close(self.backend, self.sam, H, "cancelled")
        self.assertEqual(po_closing.status(self.backend, self.sam, H).new_since, [])
        self.rows["po_signs"][0]["receipts"] = 2                    # a receipt arrives
        self.msgs["r1"] = [dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)]
        got = po_closing.status(self.backend, self.sam, H)
        self.assertTrue(got.closed)
        self.assertEqual(got.new_since, ["A receipt was recorded after it was closed",
                                         "A message names it after it was closed"])
        self.assertEqual(po_closing.closed_param(self.backend), H)  # still closed

    def test_the_very_late_orders_a_message_names_after_their_needed_by_day_are_alive(self):
        d = dt.date(2026, 1, 1)
        self.rows["late_po_candidates"] = [
            {"handle": "a", "requested_date": d, "record_ids": ["ra"]},
            {"handle": "b", "requested_date": d, "record_ids": ["rb"]},
            {"handle": "c", "requested_date": None, "record_ids": ["rc"]}]
        self.msgs = {"ra": [dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)],     # after: alive
                     "rb": [dt.datetime(2025, 12, 31, tzinfo=dt.timezone.utc)],   # before: no
                     "rc": [dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)]}     # no day: no
        self.assertEqual(po_closing.params(self.backend, self.sam)["alive"], "a")

    def test_candidates_past_one_page_are_all_asked_of_the_message_index(self):
        from operating.page_reads import MAX_ROWS
        d = dt.date(2026, 1, 1)
        total = MAX_ROWS + 30
        every = [{"handle": f"h{k:05d}", "requested_date": d, "record_ids": [f"r{k:05d}"]}
                 for k in range(total)]

        def read(b, r, reads, **kw):
            after = reads[0]["params"]["after"]
            return {"late_po_candidates": [c for c in every if c["handle"] > after][:MAX_ROWS]}, {}
        self.msgs = {f"r{total - 1:05d}": [dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)]}
        with mock.patch.object(page_sql, "read", read), mock.patch.object(po_closing, "CHUNK", 100):
            self.assertEqual(po_closing.params(self.backend, self.sam)["alive"], f"h{total - 1:05d}")

    def test_the_table_refuses_an_update_or_a_delete(self):
        import psycopg
        po_closing.close(self.backend, self.sam, H, None)
        for sql in ("UPDATE assist.po_closures SET reason = 'x'", "DELETE FROM assist.po_closures"):
            with psycopg.connect(self.dsn) as c, self.assertRaises(psycopg.errors.RaiseException):
                c.execute(sql)

    def test_an_unreadable_store_raises_never_an_empty_closed_list(self):
        bad = SimpleNamespace(memory=SimpleNamespace(pool=Pool("dbname=does_not_exist_postale")))
        with self.assertRaises(Exception):
            po_closing.closed_param(bad)


if __name__ == "__main__":
    unittest.main()
