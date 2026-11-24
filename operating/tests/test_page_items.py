"""``page_items.apply`` (speed specs 1.1.3), the served store's only write path, on the business
views fixture with every purchase order's record items: a removed source row removes its items
and their lineage, a failure inside the apply leaves the store and the lineage as they were, an
item whose source row has no lineage refuses the whole apply, and applying N = 1, 10 or 60 pages
costs the same statements (the apply is set-based, never per page)."""
from __future__ import annotations

import unittest

import psycopg

from operating.pg import business_views as bv
from operating.pg import page_items as pi
from operating.pg import record_pages as rp
from operator_api.tests import test_page_sql as tps
from tests import scale

S = bv.SCHEMA
STAGE_FROM = (f"INSERT INTO {{t}} ({', '.join(pi.COLS)}, src_view, lin_ids) SELECT "
              + ", ".join(f"i.{c}" for c in pi.COLS)
              + ", CASE WHEN i.section IN ('header', 'cell') THEN 'purchase_orders' WHEN "
              "i.section IN ('lines', 'line_cell') THEN 'purchase_order_lines' ELSE "
              f"'record_links' END, ARRAY[i.page_key] FROM {S}.page_items i "
              "WHERE i.page_kind = 'record' AND i.page_key = ANY(%s)")


class PageItems(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tps.PageDoor.setUpClass.__func__(cls)
        rp.build(cls.dsn)

    truth = tps.PageDoor.truth

    def held(self, c):
        return c.execute(f"SELECT count(*), md5(string_agg(handle || payload::text || xmin::text, "
                         f"',' ORDER BY handle)) FROM {S}.page_items").fetchone(), c.execute(
            f"SELECT count(*) FROM {S}.lineage WHERE view = 'page_items'").fetchone()[0]

    def restage(self, c, keys):
        t = pi.stage(c, "record", S)
        c.execute(STAGE_FROM.format(t=t), (keys,))
        return t

    def keys(self):
        return [r[0] for r in self.truth("SELECT DISTINCT page_key FROM {biz}.page_items WHERE "
                                         "page_kind = 'record' ORDER BY 1")]

    def test_a_failure_inside_the_apply_leaves_everything_as_it_was(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            before = self.held(c)
            t = self.restage(c, self.keys()[:3])
            c.execute(f"UPDATE {t} SET payload = '{{\"x\": 1}}', access_class = NULL WHERE seq = 0 "
                      "AND section = 'header'")   # step 4 refuses the NULL class
            with self.assertRaises(psycopg.errors.NotNullViolation):
                pi.apply(c, "record", t, keys=self.keys()[:3], code_fp="t", job="test", schema=S)
            self.assertEqual(self.held(c), before)
            t = self.restage(c, self.keys()[:3])
            c.execute(f"UPDATE {t} SET src_handle = 'no-such-row' WHERE section = 'header'")
            with self.assertRaisesRegex(RuntimeError, "with a lineage: not applied"):
                pi.apply(c, "record", t, keys=self.keys()[:3], code_fp="t", job="test", schema=S)
            self.assertEqual(self.held(c), before)
            c.execute(f"DROP TABLE IF EXISTS {t}")

    def test_apply_stores_every_payload_masked_and_a_second_apply_changes_nothing(self):
        key = self.keys()[7]
        with psycopg.connect(self.dsn, autocommit=True) as c:
            t = self.restage(c, [key])
            c.execute(f"UPDATE {t} SET payload = payload || jsonb_build_object('note', "
                      "'card 4111 1111 1111 1111 cvv 123') WHERE section = 'header' AND "
                      "jsonb_typeof(payload) = 'object'")
            out = pi.apply(c, "record", t, keys=[key], code_fp="t", job="test", schema=S)
            self.assertEqual(out["masked"], 1)
            note = self.truth("SELECT payload ->> 'note' FROM {biz}.page_items WHERE page_key = %s "
                              "AND section = 'header'", (key,))[0][0]
            self.assertNotIn("4111 1111 1111 1111", note)
            self.assertIn("1111", note)
            t = self.restage(c, [key])      # the same staged text again: masked to the same text
            c.execute(f"UPDATE {t} SET payload = payload || jsonb_build_object('note', %s::text) "
                      "WHERE section = 'header' AND jsonb_typeof(payload) = 'object'", (note,))
            again = pi.apply(c, "record", t, keys=[key], code_fp="t", job="test", schema=S)
            self.assertEqual((again["masked"], again["updated"], again["inserted"]), (0, 0, 0))
        rp.build(self.dsn)    # the fixture's own items back (their code print, their payloads)

    def test_applying_1_10_60_pages_costs_the_same_statements(self):
        keys = self.keys()
        self.assertGreaterEqual(len(keys), 60)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            def run(n):
                t = self.restage(c, keys[:n])
                with scale.counting(c) as got:
                    out = pi.apply(c, "record", t, keys=keys[:n], code_fp=rp.code_fingerprint(),
                                   job="test", schema=S)
                run.counts[n] = got[0]
                self.assertEqual(out["unchanged"], out["staged"])
            run.counts = {}
            for n in scale.NS:
                run(n)
        self.assertEqual(len(set(run.counts.values())), 1, run.counts)

    def test_a_removed_line_removes_its_items_and_their_lineage(self):
        h, key = self.truth("SELECT l.po_handle, i.page_key FROM {biz}.purchase_order_lines l "
                            "JOIN {biz}.page_items i ON i.src_handle = l.po_handle AND "
                            "i.section = 'header' GROUP BY 1, 2 HAVING (SELECT count(*) FROM "
                            "{biz}.purchase_order_lines x WHERE x.po_handle = l.po_handle) > 1 "
                            "ORDER BY 1 LIMIT 1")[0]
        lines = lambda: self.truth("SELECT count(*) FROM {biz}.page_items WHERE page_key = %s "
                                   "AND section = 'lines'", (key,))[0][0]  # noqa: E731
        n = lines()
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"DELETE FROM {S}.purchase_order_lines WHERE handle = (SELECT handle FROM "
                      f"{S}.purchase_order_lines WHERE po_handle = %s ORDER BY handle LIMIT 1)",
                      (h,))
        out = rp.build(self.dsn)
        self.assertGreaterEqual(out["deleted"], 1)
        self.assertEqual(lines(), n - 1)
        items, lin = self.truth("SELECT (SELECT count(*) FROM {biz}.page_items), (SELECT count(*) "
                                "FROM {biz}.lineage WHERE view = 'page_items')")[0]
        self.assertEqual(items, lin)


if __name__ == "__main__":
    unittest.main()
