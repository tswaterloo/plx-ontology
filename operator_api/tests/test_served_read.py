"""``served.read`` (speed specs 1.2) over the served store on the business views fixture with
every purchase order's record items applied (``record_pages.build`` -> ``page_items.apply``):
for each reader (a full reader, a restricted one, one with no grant) the page's items equal the
door's own record-page read item for item (withheld cells included), a record a reader may not
see answers 404 like an unknown one, the people exclusion growing makes reads refuse until the
door follows it and then leaves the record's page to the owner alone, a restart serves the same
bytes, and N = 1, 10, 60 reads cost the same statements each."""
from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import psycopg

from operating import ontology_tools_sql as ots
from operating import page_reads as pr
from operating.pg import business_views as bv
from operating.pg import record_pages as rp
from operating.pg.access_classes import OWNER_KEY
from operating.tests import test_ontology_tools_table as tab
from operator_api import errors, served
from operator_api.tests import test_page_sql as tps
from tests import scale

FULL, OPS, NOBODY = tab.FULL, tab.OPS, tab.NOBODY


class Fixture:
    @classmethod
    def setUpClass(cls):
        tps.PageDoor.setUpClass.__func__(cls)
        rp.build(cls.dsn)

    base = tps.PageDoor.base
    truth = tps.PageDoor.truth
    page = tps.PageDoor.page

    def door(self, actor, owner=False):
        got = ots.reader_door(self.base(), actor)
        if owner:   # the owner's scope: the same grant with the owner's audience key
            g = got.grant
            got = ots.Door(ots.Grant(g.refs, tuple(sorted({*g.keys, OWNER_KEY})), g.sets, g.held,
                                     g.every_doc, g.digest + ":owner"))
        return got

    def backend(self, door):
        return SimpleNamespace(doors=SimpleNamespace(get=lambda r: (door, self.dsn),
                                                     decide_now=lambda r: None))

    def read(self, door, key):
        body, _ = served.read(self.backend(door), object(), "record", key)
        return json.loads(body)

    def keys(self):
        return [r[0] for r in self.truth("SELECT DISTINCT page_key FROM {biz}.page_items WHERE "
                                         "page_kind = 'record' ORDER BY 1")]


class ServedRead(Fixture, unittest.TestCase):
    def test_every_page_equals_the_doors_record_page_for_each_reader(self):
        keys = self.keys()
        self.assertGreaterEqual(len(keys), 60)
        shown, ops_seen = {}, []
        for name, actor in (("FULL", FULL), ("OPS", OPS), ("NOBODY", NOBODY)):
            door = self.door(actor)
            for key in keys:
                out = self.page({"reads": [{"name": "record_page", "params": {
                    "record_id": key, "limit": 2000}}]}, actor)["reads"]["record_page"]
                want = {}
                for r in (dict(zip(out["columns"], x)) for x in out["rows"]):
                    want.setdefault(r["section"], []).append((r["payload"], sorted(r["withheld"])))
                try:
                    got = self.read(door, key)
                except errors.UserFacing as exc:
                    self.assertEqual(exc.status, 404)
                    # nothing shown: the door gives this reader no item of the page, or only
                    # links of a record it may not open
                    self.assertEqual(set(want) - {"linked"}, set(), (name, key))
                    continue
                self.assertEqual(got["status"], "ok")
                items = {s: [(i["v"], sorted(i["withheld"])) for i in v]
                         for s, v in got["sections"].items()}
                self.assertEqual(items, want, (name, key))
                shown[name] = shown.get(name, 0) + 1
                if name == "OPS":
                    ops_seen.append(key)
        self.assertEqual(shown["FULL"], len(keys))
        self.assertLess(shown.get("OPS", 0), len(keys))    # the restricted reader sees fewer
        self.assertEqual(shown.get("NOBODY", 0), 0)
        withheld = [i for k in ops_seen for s in self.read(self.door(OPS), k)["sections"].values()
                    for i in s if i["withheld"]]
        for item in withheld:   # a refused cell: payload NULL, named withheld, never the value
            self.assertEqual((item["v"], item["withheld"]), (None, ["payload"]))

    def hidden(self, door, key):
        """The count-only view's answer for a page, per section, read through the reader's door."""
        from operating.page_reads import pool_for
        with pool_for(self.dsn).connection() as pg:
            rows = ots.scoped_read(pg, ots.Door(door.grant), lambda pg: pg.execute(
                "SELECT section, n FROM page_items_hidden WHERE page_kind = 'record' AND "
                "page_key = %s", (key,)).fetchall(), 5, person=True)
        return {sec: n for sec, n in rows}

    def test_the_hidden_count_is_what_the_door_holds_back_and_never_owner_only_rows(self):
        keys = self.keys()
        full, ops, owner = self.door(FULL), self.door(OPS), self.door(FULL, owner=True)
        seen_hidden = 0
        for key in keys:
            try:
                f, o = self.read(full, key)["sections"], self.read(ops, key)["sections"]
            except errors.UserFacing:
                continue      # a record OPS may not open: no page, no count asked
            want = {sec: len(f.get(sec, ())) - len(o.get(sec, ())) for sec in f
                    if len(f[sec]) != len(o.get(sec, ()))}
            self.assertEqual(self.hidden(ops, key), want, key)
            seen_hidden += sum(want.values())
            self.assertEqual(self.hidden(full, key), {}, key)      # a full reader holds all
        self.assertGreater(seen_hidden, 0, "the fixture hides items from OPS")
        # an item the people exclusion made the owner's alone is counted for nobody else
        self.assertEqual(self.hidden(self.door(NOBODY), keys[0]).get("nothing", 0), 0)

    def test_unknown_record_is_404_and_a_built_record_without_items_is_not_run(self):
        with self.assertRaises(errors.UserFacing) as ctx:
            self.read(self.door(FULL), "PurchaseOrder:nope")
        self.assertEqual(ctx.exception.status, 404)
        other = self.truth("SELECT record_id FROM {biz}.records r WHERE r.biz AND NOT EXISTS ("
                           "SELECT 1 FROM {biz}.page_items i WHERE i.page_key = r.record_id) "
                           "ORDER BY 1 LIMIT 1")[0][0]
        served.PENDING.set(None)
        got = self.read(self.door(FULL), other)
        self.assertEqual((got["status"], got["sections"], got["why"]),
                         ("not_run", {}, "this page is being prepared"))
        self.assertEqual(served.PENDING.get()[1:], ("record", other))
        served.write_request(*served.PENDING.get())
        self.assertEqual(self.truth("SELECT page_key FROM {biz}.page_requests"), [(other,)])

    CARD = "4111 1111 1111 1111"

    def test_a_card_number_in_any_free_text_field_is_masked_in_what_is_served(self):
        """Every free-text field a page carries (a header's receipt note, status reason and
        verdict reason, a line's description, a linked record's label) is stored masked by the
        one write path (``page_items.apply``), so what ``served.read`` sends has no card number
        (a read-time mask costs ~27 ms on a 200 KB body, past the 3 ms the spec allows)."""
        from operating.pg import page_items as pi
        from operating.tests.test_page_items import STAGE_FROM
        fields = (("header", "receipt_note"), ("header", "status_reason"),
                  ("header", "verdict_reason"), ("lines", "description"), ("linked", "label"))
        key = self.truth("SELECT page_key FROM {biz}.page_items WHERE page_kind = 'record' "
                         "GROUP BY 1 HAVING count(*) FILTER (WHERE section = 'header') > 0 "
                         "AND count(*) FILTER (WHERE section = 'lines') > 0 AND "
                         "count(*) FILTER (WHERE section = 'linked') > 0 ORDER BY 1 LIMIT 1")[0][0]
        with psycopg.connect(self.dsn, autocommit=True) as c:
            t = pi.stage(c, "record", bv.SCHEMA)
            c.execute(STAGE_FROM.format(t=t), ([key],))
            for section, field in fields:
                c.execute(f"UPDATE {t} SET payload = payload || jsonb_build_object(%s::text, "
                          "%s::text) WHERE section = %s AND jsonb_typeof(payload) = 'object' AND "
                          f"seq = (SELECT min(seq) FROM {t} WHERE section = %s)",
                          (field, f"paid by card {self.CARD} exp 12/29", section, section))
            try:
                out = pi.apply(c, "record", t, keys=[key], code_fp="mask-test", job="test",
                               schema=bv.SCHEMA)
                self.assertGreaterEqual(out["masked"], 3)
                body, _ = served.read(self.backend(self.door(FULL)), object(), "record", key)
                self.assertNotIn(self.CARD, body.decode())
                page = json.loads(body)
                for sec, f in fields:
                    shown = [i["v"][f] for i in page["sections"].get(sec, ())
                             if isinstance(i["v"], dict) and isinstance(i["v"].get(f), str)]
                    self.assertTrue(shown and all("1111" in v and self.CARD not in v
                                                  for v in shown), (sec, f, shown))
            finally:   # the fixture's own items back (one build, one write path)
                from operating.pg import record_pages as rp
                rp.build(self.dsn)

    def test_a_record_the_reader_sees_whose_items_are_all_hidden_says_so_and_asks_for_nothing(self):
        """Not 'being prepared' forever: the page exists, nothing on it is open to this reader."""
        key = self.keys()[5]
        served.PENDING.set(None)
        with mock.patch.object(served, "BUILT_SQL", "SELECT true"), \
                mock.patch.object(served, "PAGE_SQL", served.PAGE_SQL.replace(
                    "page_kind = %(kind)s", "false AND page_kind = %(kind)s")):
            got = self.read(self.door(FULL), key)
        self.assertEqual((got["status"], got["sections"], got["why"]),
                         ("partial", {}, "nothing on this page is open to you"))
        self.assertIsNone(served.PENDING.get())
        self.assertEqual(self.truth("SELECT count(*) FROM {biz}.page_requests WHERE page_key = %s",
                                    (key,))[0][0], 0)

    def test_the_page_schema_holds_the_build_request_and_the_built_functions(self):
        """plx_api asks for a build through the page schema (migration 0064), not the business schema."""
        rows = self.truth("SELECT to_regprocedure('{biz}_page.request_build(text,text)') IS NOT "
                          "NULL, to_regprocedure('{biz}_page.page_built(text,text)') IS NOT NULL")
        self.assertEqual(rows, [(True, True)])

    def test_grant_ahead_decides_the_door_through_the_hosts_page_tool_not_the_wrapped_table(self):
        """The view the workers hold wraps every tool into a handler only (no ``reads``):
        the door is decided by calling ``plx.page`` itself, with the PageReads of the host's
        own table, and is the door a page read of that reader decides."""
        base = self.base()
        reads = pr.PageReads(base)
        host_table = {pr.TOOL_NAME: {"handler": reads.call, "reads": reads}}
        import contextlib

        class View:
            actor = FULL
            access = SimpleNamespace(host=SimpleNamespace(table=host_table))
            tools = {pr.TOOL_NAME: {"handler": lambda a, actor: reads.call(a, actor)}}

            def _call(self, name, arguments):
                return self.tools[name]["handler"](dict(arguments), self.actor)

            @contextlib.contextmanager
            def _request(self):
                yield
        door, dsn, stamp, comms = pr.grant_ahead(View())
        self.assertEqual(sorted(comms), sorted({*ots.COMMS_SETTINGS, "plx.reader_files", "plx.reader_files_ok", "plx.reader_files_state"}))   # decided with the door
        self.assertEqual(door.grant.digest, ots.reader_door(base, FULL).grant.digest)
        self.assertEqual(dsn, self.dsn)
        self.assertIsInstance(stamp, int)
        key = self.keys()[2]
        served_body, _ = served.read(SimpleNamespace(doors=SimpleNamespace(
            get=lambda r: (door, dsn), decide_now=lambda r: None)), object(), "record", key)
        self.assertEqual(json.loads(served_body), self.read(self.door(FULL), key))
        View.access = SimpleNamespace(host=SimpleNamespace(table={}))
        with self.assertRaises(pr.PageReadError):
            pr.grant_ahead(View())

    def test_a_restart_serves_the_same_bytes(self):
        key = self.keys()[3]
        door = self.door(FULL)
        first, _ = served.read(self.backend(door), object(), "record", key)
        with mock.patch.dict(pr._POOLS, clear=True), mock.patch.dict(ots._DOOR, clear=True):
            fresh = self.door(FULL)
            with pr.pool_for(self.dsn).connection() as pg:   # decided ahead, as doors does
                ots.scoped_read(pg, fresh, lambda pg: None, 5, person=True)
            t = time.perf_counter()
            again, _ = served.read(self.backend(fresh), object(), "record", key)
            cold = time.perf_counter() - t
            t = time.perf_counter()
            served.read(self.backend(fresh), object(), "record", key)
            warm = time.perf_counter() - t
        self.assertEqual(first, again)
        self.assertLessEqual(cold, 2 * warm + 0.05)

    def test_reads_cost_the_same_statements_each_at_1_10_60(self):
        keys, door = self.keys(), self.door(FULL)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            pool = SimpleNamespace(connection=lambda: mock.MagicMock(
                __enter__=lambda s: conn, __exit__=lambda s, *a: False))
            with mock.patch("operating.page_reads.pool_for", return_value=pool):
                got = scale.measure(lambda n: [served.read(self.backend(door), object(), "record",
                                                           k) for k in keys[:n]], pg=conn)
        per = {n: c / n for n, (c, _) in got.items()}
        self.assertEqual(len(set(per.values())), 1, per)
        self.assertLessEqual(per[1], 4)      # the door's check, the settings, the read, the check
        self.assertLessEqual(got[60][1] / 60, 3 * got[1][1] + 0.05)


class ServedExclusion(Fixture, unittest.TestCase):
    def test_reads_refuse_while_the_set_grows_then_the_page_is_the_owners_alone(self):
        from operating.pg import door as pdoor
        rows = self.truth("SELECT src_handle, page_key FROM {biz}.page_items WHERE page_kind = "
                          "'record' AND section = 'header' ORDER BY 1, 2")
        by = {}
        for h, k in rows:
            by.setdefault(h, []).append(k)
        victim = next(ks[0] for ks in by.values() if len(ks) == 1)
        other = next(k for k, in ((r[1],) for r in rows) if k != victim)
        before_other = self.read(self.door(FULL), other)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('x://excluded', %s, 'participant')", (victim,))
            c.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, "
                      "objects, handles, covered_ver) VALUES ('t', 'now', 1, 1, 1)")
        with self.assertRaises(served.Unavailable) as ctx:   # the door is behind the set
            self.read(self.door(FULL), victim)
        self.assertEqual(ctx.exception.code, "not_ready")
        with psycopg.connect(self.dsn, autocommit=True) as c:
            pdoor.exclude(c, bv.SCHEMA)
        with self.assertRaises(errors.UserFacing):
            self.read(self.door(FULL), victim)
        owner = self.read(self.door(FULL, owner=True), victim)
        self.assertIn("header", owner["sections"])
        self.assertEqual(self.read(self.door(FULL), other), before_other)


if __name__ == "__main__":
    unittest.main()
