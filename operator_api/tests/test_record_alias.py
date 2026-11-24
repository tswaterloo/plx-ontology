"""The record page's ``alias`` items (speed specs 8.3, ``operating.pg.record_alias``) on the business
views fixture: a number two business rows hold lists both candidates to a reader who may open both,
only the one it may open to a restricted reader (nothing says the other exists), copies of one order
are one candidate, an unknown code and a code whose every candidate is hidden answer 404, a marked
identifier refuses the build (never a silent drop), and a code reads the same typed in lower case."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import psycopg

from operating.pg import record_alias as ra
from operator_api import errors, served
from operator_api.tests import test_served_read as tsr

FULL, OPS, NOBODY = tsr.FULL, tsr.OPS, tsr.NOBODY
S = "biz"


# the build's disk floor (PLX_BIZ_MIN_FREE_GB) is the host's, not what this module tests: the
# fixture's tmp disk is not the served one (the real floor in the code is untouched)
_NO_FLOOR = mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "0"})


def setUpModule():
    _NO_FLOOR.start()


def tearDownModule():
    _NO_FLOOR.stop()


class Alias(tsr.Fixture, unittest.TestCase):
    def setUp(self):
        rows = self.truth(
            "SELECT DISTINCT ON (i.src_handle) i.page_key, i.src_handle FROM {biz}.page_items i "
            "WHERE i.page_kind = 'record' AND i.section = 'header' ORDER BY i.src_handle, "
            "i.page_key")
        self.assertGreaterEqual(len(rows), 4)
        ops = {k for k in self.keys() if self._opens(OPS, k)}
        marked = {r[0] for r in self.truth(
            "SELECT i.page_key FROM {biz}.page_items i JOIN {biz}.purchase_orders p ON p.handle = "
            "i.src_handle JOIN {biz}.col_classes c ON c.id = p.col_class WHERE i.section = "
            "'header' AND i.page_kind = 'record' AND (c.marks ?| %s OR c.needs ?| %s)"
            .replace("%s", "ARRAY['po_number','supplier','source_systems','status']"))}
        self.marked = sorted(marked)
        self.mine_ok = [k for k, _ in rows if k in ops and k not in marked]
        self.mine_no = [k for k, _ in rows if k not in ops]
        handle = dict(rows)[self.mine_no[0]]
        with psycopg.connect(self.dsn, autocommit=True) as c:    # its label columns carry no marking
            was = c.execute("SELECT col_class FROM biz.purchase_orders WHERE handle = %s",
                            (handle,)).fetchone()[0]
            c.execute("UPDATE biz.purchase_orders SET col_class = 0 WHERE handle = %s", (handle,))
        self.addCleanup(lambda: psycopg.connect(self.dsn, autocommit=True).execute(
            "UPDATE biz.purchase_orders SET col_class = %s WHERE handle = %s", (was, handle)))
        self.assertTrue(self.mine_ok and self.mine_no, "the fixture needs a row OPS may not open")
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("DELETE FROM ontology.identifiers WHERE identifier_kind = 'alias_test'")
            self.add(c, "DUP-1", self.mine_ok[0])
            self.add(c, "DUP-1", self.mine_no[0])
            self.add(c, "HID-1", self.mine_no[0])
            self.add(c, "ONE-1", self.mine_ok[1])
        self.addCleanup(self.clean)
        self.build()

    def add(self, c, code, oid, markings="[]"):
        c.execute("INSERT INTO ontology.identifiers (identifier_kind, normalized, raw, role, "
                  "object_id, property, l1_handle, field_path, lineage, markings, recorded_at) "
                  "VALUES ('alias_test', %s, %s, 'primary', %s, 'po_number', 'x://h', '', '', %s, "
                  "'2026-01-01') ON CONFLICT DO NOTHING", (code, code, oid, markings))

    def clean(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("DELETE FROM ontology.identifiers WHERE identifier_kind = 'alias_test'")
            c.execute("DELETE FROM biz.page_items WHERE page_kind = 'alias'")

    def build(self):
        return ra.build(self.dsn)

    def _opens(self, actor, key):
        try:
            self.read(self.door(actor), key)
            return True
        except errors.UserFacing:
            return False

    def alias(self, actor, code):
        body, _ = served.read(self.backend(self.door(actor)), object(), "alias", code)
        return json.loads(body)["sections"]["alias"]

    def test_a_reader_receives_only_the_candidates_it_may_open(self):
        full = self.alias(FULL, "DUP-1")
        self.assertEqual(sorted(i["v"]["record_id"] for i in full),
                         sorted([self.mine_ok[0], self.mine_no[0]]))
        part = self.alias(OPS, "DUP-1")
        self.assertEqual([i["v"]["record_id"] for i in part], [self.mine_ok[0]])
        for i in full:   # the label's inputs, the old choices' label
            self.assertEqual(sorted(i["v"]), ["number", "party", "record_id", "status", "systems"])

    def test_denied_cases_read_as_not_found(self):
        for actor, code in ((OPS, "HID-1"), (NOBODY, "DUP-1"), (FULL, "NO-SUCH")):
            with self.assertRaises(errors.UserFacing, msg=(actor, code)) as ctx:
                self.alias(actor, code)
            self.assertEqual(ctx.exception.status, 404)

    def test_a_code_is_found_in_either_way_of_writing_it_and_is_one_candidate_per_row(self):
        self.assertEqual([i["v"]["record_id"] for i in self.alias(FULL, " one-1 ")],
                         [self.mine_ok[1]])

    def test_a_marked_identifier_refuses_the_build(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            self.add(c, "MARK-1", self.mine_ok[0], '["finance"]')
        before = self.truth("SELECT count(*) FROM {biz}.page_items WHERE page_kind = 'alias'")
        with self.assertRaises(RuntimeError):
            self.build()
        self.assertEqual(self.truth("SELECT count(*) FROM {biz}.page_items WHERE page_kind = "
                                    "'alias'"), before)

    def test_a_marked_label_column_refuses_the_build(self):
        other = [k for k in self.marked if k != self.mine_no[0]]
        self.assertTrue(other, "the fixture marks a label column of a row other than the hidden one")
        with psycopg.connect(self.dsn, autocommit=True) as c:
            self.add(c, "LEAK-1", other[0])
        with self.assertRaises(RuntimeError):
            self.build()

    def test_one_statement_per_way_of_writing_a_code_at_1_10_60_candidates_is_flat(self):
        seen = []
        for n in (1, 10, 60):
            with psycopg.connect(self.dsn, autocommit=True) as c:
                for k in ([x for x in self.keys() if x not in self.marked or x == self.mine_no[0]] * 60)[:n]:
                    self.add(c, "MANY-1", k)
            self.build()
            got = self.alias(FULL, "MANY-1")
            seen.append(len(got))
        self.assertEqual(seen[0], 1)
        self.assertLessEqual(seen[1], 10)
        self.assertGreaterEqual(seen[2], seen[1])


if __name__ == "__main__":
    unittest.main()
