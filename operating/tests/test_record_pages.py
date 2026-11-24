"""The precomputed record page (``operating.pg.record_pages`` into ``page_items``, ``page_reads`` ``record_page``)
through the page door, on the business views fixture plus 60 late POs (``test_page_sql``):
for every purchase order and each reader (a full reader, a restricted one, one with no grant)
the page's facts equal the door's own row for the same order (set of orders, each value, each
withheld cell), a cell a reader may not read never reaches it, no row is lost or repeated at
N = 1, 10 and 60 lines, the AI's schema holds no such view, an unknown read fails closed, and a
newly excluded person's record's page is the owner's alone."""
from __future__ import annotations

import json
import unittest
from decimal import Decimal

import psycopg

from operating import ontology_tools_sql as door
from operating import page_reads as pr
from operating.pg import business_views as bv
from operating.pg import record_pages as rp
from operating.tests import test_business_views as tbv
from operating.tests import test_ontology_tools_table as tab
from operator_api.tests import test_page_sql as tps

FULL, OPS, NOBODY = tab.FULL, tab.OPS, tab.NOBODY
FACTS = ("po_number", "supplier", "status", "verdict", "days_late", "total", "total_currency",
         "lines_total", "lines_total_currency", "promised_date", "requested_date", "order_date",
         "line_count", "supplier_handle", "source_systems")


def norm(value):
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value)).normalize()
    return str(value)


class RecordPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tps.PageDoor.setUpClass.__func__(cls)
        cls.built = rp.build(cls.dsn)

    base = tps.PageDoor.base
    truth = tps.PageDoor.truth
    page = tps.PageDoor.page

    def items(self, record_id, actor):
        out = self.page({"reads": [{"name": "record_page", "params": {
            "record_id": record_id, "limit": 2000}}]}, actor)
        self.assertNotIn("error", out, out)
        got = out["reads"]["record_page"]
        return [dict(zip(got["columns"], r)) for r in got["rows"]]

    def orders(self):
        """handle -> its member record ids, from the table itself (no scope)."""
        out: dict[str, list[str]] = {}
        for handle, rid in self.truth(
                "SELECT src_handle, page_key FROM {biz}.page_items WHERE page_kind = 'record' "
                "AND section = 'header' ORDER BY 1, 2"):
            out.setdefault(handle, []).append(rid)
        return out

    def door_row(self, handle, actor):
        out = self.page({"reads": [{"name": "purchase_order", "params": {"handle": handle}}]},
                        actor)
        got = out["reads"]["purchase_order"]
        rows = [dict(zip(got["columns"], r)) for r in got["rows"]]
        return rows[0] if rows else None

    def facts_of(self, items):
        """header payload plus the cells the reader may read; withheld cells named."""
        facts, withheld = {}, set()
        for it in items:
            if it["section"] == "header":
                facts.update(it["payload"] or {})
            elif it["section"] == "cell":
                if it["payload"] is None:
                    self.assertIn("payload", it["withheld"])
                    withheld.add("?")
                else:
                    facts.update(it["payload"])
        return facts, withheld

    def test_every_order_has_a_page_for_every_copy_and_none_for_a_stranger(self):
        orders = self.orders()
        every = {r[0] for r in self.truth("SELECT handle FROM {biz}.purchase_orders")}
        self.assertEqual(set(orders), every)
        self.assertGreater(self.built["items"], len(every))
        for handle, ids in orders.items():
            for rid in ids:
                self.assertTrue(self.items(rid, FULL), rid)
        self.assertEqual(self.items("PurchaseOrder:portal.app_purchase_order:nope", FULL), [])

    def test_a_pages_facts_are_the_doors_own_row_for_each_reader(self):
        orders = self.orders()
        seen = {"FULL": 0, "OPS": 0}
        for name, actor in (("FULL", FULL), ("OPS", OPS), ("NOBODY", NOBODY)):
            for handle, ids in orders.items():
                row = self.door_row(handle, actor)
                for rid in ids:
                    items = self.items(rid, actor)
                    if row is None:
                        # an order row it may not read has no header, cell or line for it; the
                        # links it reads are those record_links shows it (the same door)
                        self.assertEqual({i["section"] for i in items} - {"linked"}, set(),
                                         (name, handle))
                        continue
                    seen[name] = seen.get(name, 0) + 1
                    facts, withheld = self.facts_of(items)
                    for col in FACTS:
                        if col in row["withheld"]:
                            self.assertNotIn(col, facts, (name, handle, col))
                        elif col in ("total_basis",):
                            continue
                        else:
                            self.assertEqual(norm(facts.get(col)), norm(row.get(col)),
                                             (name, handle, col))
                    # a withheld cell is an empty cell item naming its column, one per column
                    # the door withholds that the row's class marks
                    self.assertLessEqual(len(withheld), 1)
                    if row["withheld"]:
                        self.assertTrue(withheld or not any(
                            i["section"] == "cell" for i in items), (name, handle))
        self.assertGreater(seen["FULL"], 60)
        self.assertGreater(seen["OPS"], 50)
        self.assertEqual(seen.get("NOBODY", 0), 0)

    def test_a_cell_a_reader_may_not_read_never_reaches_it(self):
        orders = self.orders()
        marked = [h for h, _ in orders.items()
                  if (self.door_row(h, OPS) or {}).get("withheld")]
        self.assertTrue(marked, "the fixture has a row whose cell OPS may not read")
        for handle in marked:
            truth_row = self.door_row(handle, FULL)
            for rid in orders[handle]:
                full = json.dumps(self.items(rid, FULL), default=str)
                ops = json.dumps(self.items(rid, OPS), default=str)
                for col in self.door_row(handle, OPS)["withheld"]:
                    value = truth_row.get(col)
                    if value in (None, "") or col in ("status", "verdict_reason"):
                        continue
                    self.assertIn(json.dumps(value, default=str).strip('"'), full)
                    # its cell is there, empty, naming the column it was withheld from
                    cells = [i for i in self.items(rid, OPS) if i["section"] == "cell"
                             and i["src_handle"] == handle and i["payload"] is None]
                    self.assertTrue(cells or col not in ("total", "lines_total"), (handle, col))
                for word in tbv.SECRET:
                    self.assertNotIn(word, ops)

    def test_lines_without_loss_or_repeat_at_1_10_60(self):
        by_po = {}
        for po, handle in self.truth("SELECT p.po_number, p.handle FROM {biz}.purchase_orders p "
                                     "WHERE po_number IN ('PO-B1', 'PO-B10', 'PO-B60')"):
            by_po[po] = handle
        self.assertEqual(sorted(by_po), ["PO-B1", "PO-B10", "PO-B60"])
        for po, n in (("PO-B1", 1), ("PO-B10", 10), ("PO-B60", 60)):
            handle = by_po[po]
            truth = [r[0] for r in self.truth(
                "SELECT handle FROM {biz}.purchase_order_lines WHERE po_handle = %s", (handle,))]
            self.assertEqual(len(truth), n)
            rid = self.orders()[handle][0]
            lines = [i for i in self.items(rid, FULL) if i["section"] == "lines"]
            self.assertEqual(sorted(i["src_handle"] for i in lines), sorted(truth), po)
            self.assertEqual([i["seq"] for i in lines], list(range(n)), po)

    def test_linked_records_are_the_links_both_ways_once(self):
        handle = next(h for (h,) in self.truth(
            "SELECT handle FROM {biz}.purchase_orders WHERE po_number = 'PO-1'"))
        rids = self.orders()[handle]
        for rid in rids:
            linked = [i for i in self.items(rid, FULL) if i["section"] == "linked"]
            seqs = [i["seq"] for i in linked]
            self.assertEqual(seqs, list(range(len(linked))))
            keys = [(i["payload"]["dir"], i["src_handle"]) for i in linked]
            self.assertEqual(len(keys), len(set(keys)))   # none twice
        everything = {i["src_handle"] for rid in rids for i in self.items(rid, FULL)
                      if i["section"] == "linked"}
        truth = {h for (h,) in self.truth(
            "SELECT k.handle FROM {biz}.record_links k JOIN {biz}.records r ON r.rid IN "
            "(k.from_rid, k.to_rid) WHERE r.record_id = ANY(%s)", (rids,))}
        self.assertEqual(everything, truth)

    def test_unknown_input_fails_closed_and_the_ai_reads_no_such_view(self):
        for params in ({}, {"record_id": "x"}, {"record_id": "x", "limit": 0},
                       {"record_id": "x", "limit": 2001},
                       {"record_id": "x", "limit": 5, "extra": 1}, {"record_id": 5, "limit": 5}):
            out = pr.PageReads(self.base()).call(
                {"reads": [{"name": "record_page", "params": params}]}, FULL)
            self.assertIn("error", out, params)
        out = door.SqlTool(self.base()).call(
            "plx.sql", {"sql": "SELECT * FROM page_items"}, FULL)
        self.assertIn("error", out)
        with psycopg.connect(self.dsn) as c:
            self.assertIsNone(c.execute(
                "SELECT to_regclass(%s)", (f"{bv.SCHEMA}_sql.page_items",)).fetchone()[0])
            self.assertIsNotNone(c.execute(
                "SELECT to_regclass(%s)", (f"{bv.SCHEMA}_page.page_items",)).fetchone()[0])

    def test_every_item_has_a_lineage_and_a_class_and_a_rebuild_writes_nothing(self):
        def held(c):
            return c.execute(
                f"SELECT count(*), md5(string_agg(handle || access_class || ':' || col_class || "
                f"':' || payload::text || ':' || xmin::text, ',' ORDER BY handle)) FROM "
                f"{bv.SCHEMA}.page_items WHERE page_kind = 'record'").fetchone()
        with psycopg.connect(self.dsn) as c:
            items, before = held(c)
            lin = c.execute(f"SELECT count(*) FROM {bv.SCHEMA}.lineage WHERE view = "
                            "'page_items'").fetchone()[0]
            counted = c.execute(f"SELECT COALESCE(sum(rows), 0) FROM {bv.SCHEMA}.class_counts "
                                "WHERE view = 'page_items'").fetchone()[0]
        self.assertEqual((items, lin, counted), (items, items, items))
        self.assertFalse(rp.stale(self.dsn))
        again = rp.build(self.dsn)
        # applied in place: nothing changed, so no row is written (xmin of every row unmoved)
        self.assertEqual((again["staged"], again["inserted"], again["updated"], again["deleted"],
                          again["unchanged"]), (items, 0, 0, 0, items))
        with psycopg.connect(self.dsn) as c:
            self.assertEqual(held(c), (items, before))

    def test_a_source_view_moving_makes_the_items_stale_and_follow_builds_again(self):
        self.assertEqual(rp.follow(self.dsn), {})
        with psycopg.connect(self.dsn, autocommit=True) as c:
            # the generic refresh rewrites its own state every cycle: no reason to build
            c.execute(f"INSERT INTO {bv.SCHEMA}.state (view, built_at, rows) VALUES ('records', "
                      "'t1', 1) ON CONFLICT (view) DO UPDATE SET built_at = 't2'")
        self.assertFalse(rp.stale(self.dsn))
        self.assertEqual(rp.follow(self.dsn), {})
        with psycopg.connect(self.dsn, autocommit=True) as c:   # an order's link goes: stale
            c.execute(f"DELETE FROM {bv.SCHEMA}.record_links WHERE handle = (SELECT k.handle FROM "
                      f"{bv.SCHEMA}.record_links k JOIN {bv.SCHEMA}.records r ON r.rid = k.from_rid "
                      "WHERE r.type = 'PurchaseOrder' LIMIT 1)")
        self.assertTrue(rp.stale(self.dsn))
        self.assertIn("record_pages", rp.follow(self.dsn))
        self.assertFalse(rp.stale(self.dsn))
        with psycopg.connect(self.dsn, autocommit=True) as c:
            # the order view built again (a later build time): the items are behind it
            c.execute(f"UPDATE {bv.SCHEMA}.state SET built_at = (built_at::timestamptz + "
                      "interval '1 minute')::text WHERE view = 'purchase_orders'")
        self.assertTrue(rp.stale(self.dsn))
        out = rp.follow(self.dsn)
        self.assertIn("record_pages", out, out)
        self.assertFalse(rp.stale(self.dsn))

    def test_refresh_makes_the_pages_right_after_the_order_views_and_still_builds_the_rest(self):
        from unittest import mock
        from operating.pg import business_views_parallel as parallel
        calls = []
        real_build, real_follow = parallel.build, rp.follow

        def build(dsn, names, **kw):
            calls.append(("build", sorted(names)))
            return real_build(dsn, names, **kw)

        def follow(dsn, schema=None):
            calls.append(("follow", None))
            return real_follow(dsn, schema)
        with mock.patch.object(parallel, "build", build), mock.patch.object(rp, "follow", follow):
            bv.refresh(self.dsn, as_of=tbv.AS_OF, force=True)
        kinds = [c[0] for c in calls]
        self.assertEqual(kinds[:3], ["build", "follow", "build"], calls)
        self.assertEqual(calls[0][1], sorted(rp.SOURCES))
        self.assertTrue(set(rp.SOURCES).isdisjoint(calls[2][1]))
        # a failure of the order views never stops the others; without biz.view_state to
        # record it in, the run raises after they were tried (and no page follows them)
        def broken(dsn, names, **kw):
            calls.append(("build", sorted(names)))
            if set(names) & set(rp.SOURCES):
                return [], {n: "RuntimeError: order views" for n in names}
            return [], {}
        calls.clear()
        with mock.patch.object(parallel, "build", broken):
            with self.assertRaises(RuntimeError):
                bv.refresh(self.dsn, as_of=tbv.AS_OF, force=True)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("follow", [c[0] for c in calls])
        rp.build(self.dsn)


class RecordPageExclusion(unittest.TestCase):
    """A record of a person the exclusion names: its page, every copy's, is the owner's alone
    once the door follows the set (the items' lineage names the record), never served to a
    full reader that is not the owner, and no other order's page moves."""

    @classmethod
    def setUpClass(cls):
        tps.PageDoor.setUpClass.__func__(cls)
        rp.build(cls.dsn)

    base = tps.PageDoor.base
    truth = tps.PageDoor.truth
    page = tps.PageDoor.page
    items = RecordPages.items

    def test_an_excluded_persons_order_page_is_the_owners_alone(self):
        from operating.pg import door
        rows = self.truth("SELECT src_handle, page_key FROM {biz}.page_items WHERE "
                          "page_kind = 'record' AND section = 'header' ORDER BY 1, 2")
        by = {}
        for handle, rid in rows:
            by.setdefault(handle, []).append(rid)
        target = next(h for h, ids in by.items() if len(ids) == 1)
        victim = by[target][0]
        others = [rid for h, ids in by.items() if h != target for rid in ids]
        before = {rid: len(self.items(rid, FULL)) for rid in [victim, *others[:20]]}
        self.assertTrue(all(before.values()))
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('x://excluded', %s, 'participant')", (victim,))
            c.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, "
                      "objects, handles, covered_ver) VALUES ('t', 'now', 1, 1, 1)")
            door.exclude(c, bv.SCHEMA)
        self.assertEqual(self.items(victim, FULL), [])
        self.assertEqual(self.items(victim, OPS), [])
        for rid in others[:20]:
            self.assertEqual(len(self.items(rid, FULL)), before[rid], rid)
        with psycopg.connect(self.dsn) as c:   # still there, in the owner's class
            self.assertTrue(c.execute(
                f"SELECT count(*) FROM {bv.SCHEMA}.page_items WHERE page_kind = 'record' AND "
                "page_key = %s",
                (victim,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
