"""Keyed typed reads of the app tables (operating/app_tables), on the declared schema."""
from __future__ import annotations

import sqlite3
import unittest

from operating import declared_schema
from operating.app_tables import rows
from operating.declared_schema import action_path, work_log


def _store(module):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    declared_schema.apply(conn, module)
    return conn


class KeyedReads(unittest.TestCase):
    def setUp(self):
        self.conn = _store(action_path)
        for seq, kind in ((1, "intent.registered"), (2, "call.approved"), (3, "x.done")):
            self.conn.execute(
                "INSERT INTO action_event(intent_id,intent_seq,kind,actor,at,payload_json,"
                "prev_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)",
                ("i-1", seq, kind, "a", f"2099-01-0{seq}", "{}", f"p{seq}", f"h{seq}"))
        self.conn.execute(
            "INSERT INTO action_event(intent_id,intent_seq,kind,actor,at,payload_json,"
            "prev_hash,event_hash) VALUES('i-2',1,'k','a','t','{}','p','h-other')")
        self.table = rows.ACTION_PATH_ACTION_EVENT

    def test_rows_are_typed_and_read_like_sqlite_rows(self):
        found = self.table.select(self.conn, {"intent_id": "i-1"}, order_by=("intent_seq",))
        self.assertEqual([r.intent_seq for r in found], [1, 2, 3])
        first = found[0]
        self.assertIsInstance(first, rows.ActionPathActionEvent)
        self.assertEqual(first["kind"], "intent.registered")
        self.assertEqual(dict(zip(first.keys(), (first[k] for k in first.keys())))["at"],
                         "2099-01-01")
        with self.assertRaises(KeyError):
            first["no_such_column"]
        with self.assertRaises(Exception):
            first.kind = "changed"   # frozen

    def test_same_rows_as_the_sql_it_replaces(self):
        raw = self.conn.execute("SELECT * FROM action_event WHERE intent_id=? "
                                "ORDER BY intent_seq", ("i-1",)).fetchall()
        typed = self.table.select(self.conn, {"intent_id": "i-1"}, order_by=("intent_seq",))
        self.assertEqual([tuple(r) for r in raw], [tuple(r[k] for k in r.keys()) for r in typed])
        last = self.table.first(self.conn, {"intent_id": "i-1"}, order_by=("intent_seq",),
                                descending=True)
        self.assertEqual(last.event_hash, "h3")
        self.assertTrue(self.table.exists(self.conn, {"intent_id": "i-2"}))
        self.assertFalse(self.table.exists(self.conn, {"intent_id": "i-3"}))

    def test_names_are_checked_and_values_are_bound(self):
        with self.assertRaises(KeyError):
            self.table.select(self.conn, {"intent_id = intent_id OR 1": 1})
        with self.assertRaises(KeyError):
            self.table.select(self.conn, order_by=("seq; DROP TABLE action_event",))
        self.assertEqual(self.table.select(self.conn, {"intent_id": "i-1' OR '1'='1"}), [])
        with self.assertRaises(ValueError):
            self.table.select(self.conn, limit=-1)
        self.assertEqual(len(self.table.select(self.conn)), 4)

    def test_null_is_matched_as_null(self):
        conn = _store(work_log)
        conn.execute("INSERT INTO thread(reader, thread, name, at) VALUES ('r', 't', NULL, 'x')")
        found = rows.WORK_LOG_THREAD.first(conn, {"reader": "r", "name": None})
        self.assertEqual(found.thread, "t")


if __name__ == "__main__":
    unittest.main()
