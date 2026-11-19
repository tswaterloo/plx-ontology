"""Page numbers through the read door (``operating.page_reads`` = ``plx.page``,
``operator_api.page_sql``): every number a page shows equals the SQL truth for a full and a
restricted reader; unknown reads and parameters are refused before any SQL; the reads go
through ``plx.sql``'s own scope code and transaction (no second permission implementation)
on their own pooled connections (never waiting on the store connection's lock); late POs
page without a row lost or repeated at N = 1, 10 and 60. Postgres (the business views
fixture of ``operating.tests.test_business_views`` plus 60 more late POs)."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from operating import ontology_tools_sql as door
from operating import page_reads as pr
from operating.pg import business_views as bv
from operating.pg import sql_views
from operating.tests import test_business_views as tbv
from operating.tests import test_ontology_tools_table as tab

FULL, OPS, NOBODY = tab.FULL, tab.OPS, tab.NOBODY
MORE = 60              # more late POs beside the fixture's
FINANCE_EVERY = 5      # every fifth of them finance-marked (OPS may not see it)


def build():
    s = tbv.build()
    for k in range(MORE):
        due = (tbv.AS_OF - dt.timedelta(days=10 + k)).isoformat()
        tbv.purchase(s, f"pl{k}", f"PO-L{k:02d}", None, due, [("2", "0")],
                     markings='["finance"]' if k % FINANCE_EVERY == 0 else "[]")
    s.conn.commit()
    return s


class PageDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_store_mode
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        cls.store = build()
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

    def base(self):
        return tbv.BusinessViews.base(self)

    def truth(self, sql, args=()):
        """The business table itself, read as the build's role (no scope)."""
        import psycopg
        with psycopg.connect(self.dsn) as c:
            return c.execute(sql.format(biz=bv.SCHEMA), args).fetchall()

    def page(self, args, actor=FULL, base=None):
        return pr.PageReads(base or self.base()).call(args, actor)

    def plx_sql(self, sql, actor):
        out = door.SqlTool(self.base()).call("plx.sql", {"sql": sql}, actor)
        self.assertNotIn("error", out, out)
        return out

    def late_list(self, actor, limit, offset=0):
        out = self.page({"reads": [{"name": "late_po_list", "params": {
            "limit": limit, "offset": offset}}]}, actor)
        self.assertNotIn("error", out, out)
        got = out["reads"]["late_po_list"]
        return [r[got["columns"].index("po_number")] for r in got["rows"]]

    # ---- truth: every number equals the SQL truth for each reader ----------------------

    def test_counts_equal_the_sql_truth_for_full_restricted_and_no_reader(self):
        late_all = self.truth("SELECT count(*) FROM {biz}.purchase_orders "
                              "WHERE verdict = 'late'")[0][0]
        orders_all = self.truth("SELECT count(*) FROM {biz}.sales_orders "
                                "WHERE status = 'open' AND days_late > 0")[0][0]
        marked = len(range(0, MORE, FINANCE_EVERY)) + 1          # + the fixture's PO-FIN
        self.assertGreaterEqual(late_all, MORE + 2)
        self.assertGreaterEqual(orders_all, 1)
        reads = {"reads": [{"name": "late_po_count"}, {"name": "late_order_count"}]}
        for actor, late, orders in ((FULL, late_all, orders_all),
                                    (OPS, late_all - marked, None), (NOBODY, 0, 0)):
            with self.subTest(actor=sorted(actor.principals)):
                t0 = time.monotonic()
                out = self.page(reads, actor)
                ms = (time.monotonic() - t0) * 1000
                self.assertNotIn("error", out, out)
                got = {k: v["rows"][0][0] for k, v in out["reads"].items()}
                self.assertEqual(got["late_po_count"], late)
                if orders is not None:
                    self.assertEqual(got["late_order_count"], orders)
                # the same number plx.sql gives this reader (the same door)
                for name, sql in (("late_po_count", "SELECT count(*) AS n FROM purchase_orders "
                                   "WHERE verdict = 'late'"),
                                  ("late_order_count", "SELECT count(*) AS n FROM sales_orders "
                                   "WHERE status = 'open' AND days_late > 0")):
                    if actor is NOBODY:
                        continue
                    sql_out = self.plx_sql(sql, actor)
                    self.assertEqual(int(sql_out["rows"][0]["cells"][0]), got[name], (name, sql_out))
                self.assertLess(ms, 2000, f"page reads took {ms:.0f} ms")

    def test_restricted_reader_never_sees_a_row_it_may_not(self):
        full = set(self.late_list(FULL, pr.MAX_ROWS))
        ops = set(self.late_list(OPS, pr.MAX_ROWS))
        hidden = {f"PO-L{k:02d}" for k in range(0, MORE, FINANCE_EVERY)} | {"PO-FIN"}
        self.assertEqual(ops, full - hidden)
        self.assertTrue(hidden <= full)
        out = self.page({"reads": [{"name": "late_po_list", "params": {
            "limit": pr.MAX_ROWS, "offset": 0}}], "ids": True}, OPS)
        text = json.dumps(out, default=str)
        for word in sorted(hidden) + list(tbv.SECRET):
            self.assertNotIn(word, text)
        self.assertEqual(self.late_list(NOBODY, 10), [])

    def test_late_pos_page_without_loss_or_repeat_at_1_10_60(self):
        truth = [r[0] for r in self.truth(
            "SELECT po_number FROM {biz}.purchase_orders WHERE verdict = 'late' "
            "ORDER BY days_late DESC NULLS LAST, po_number, handle")]
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.assertEqual(self.late_list(FULL, n), truth[:n])
                walked, offset = [], 0
                while True:
                    got = self.late_list(FULL, n, offset)
                    walked += got
                    offset += len(got)
                    if len(got) < n:
                        break
                self.assertEqual(walked, truth)
                self.assertEqual(len(set(walked)), len(walked))

    def test_ids_open_the_record_of_each_shown_row(self):
        out = self.page({"reads": [{"name": "late_po_list", "params": {
            "limit": 5, "offset": 0}}], "ids": True}, OPS)
        rows = out["reads"]["late_po_list"]
        handles = [r[rows["columns"].index("handle")] for r in rows["rows"]]
        self.assertEqual(sorted(out["ids"]), sorted(handles))
        for h in handles:
            self.assertEqual(out["ids"][h]["object_type"], "PurchaseOrder")
            self.assertTrue(out["ids"][h]["object_id"].startswith("PurchaseOrder:"))

    # ---- the SQL alerts through the same door ------------------------------------------

    def alerts(self, actor, rules="po_late"):
        out = self.page({"reads": [
            {"name": "alerts_open", "params": {"rules": rules, "limit": pr.MAX_ROWS,
                                               "offset": 0}},
            {"name": "alerts_count", "params": {"rules": rules}}]}, actor)
        self.assertNotIn("error", out, out)
        got = out["reads"]["alerts_open"]
        handles = {r[got["columns"].index("handle")] for r in got["rows"]}
        counts = {r[0]: r[1] for r in out["reads"]["alerts_count"]["rows"]}
        self.assertEqual(sum(counts.values()), len(handles))
        return handles, out

    def test_alerts_a_reader_sees_are_exactly_the_records_it_may_read(self):
        """The page's alerts view (``alerts.page_view_ddl``) shows an alert only when the
        reader holds its record's class, every class it was computed from and each shown
        column's class; a mail alert (no record class) never; a column class no longer
        known hides it. Each reader's late-PO alerts equal its own late-PO list."""
        import psycopg
        from operating.pg import alerts as al
        got = al.run(self.dsn, ["po_late"], schema=bv.SCHEMA, owners={})
        self.assertEqual(got[0]["status"], "ok", got)
        truth = {r[0] for r in self.truth("SELECT handle FROM {biz}.purchase_orders "
                                          "WHERE verdict = 'late' AND days_late > 0")}
        self.assertGreaterEqual(len(truth), MORE)
        for actor in (FULL, OPS, NOBODY):
            with self.subTest(actor=sorted(actor.principals)):
                listed = self.page({"reads": [{"name": "late_po_list", "params": {
                    "limit": pr.MAX_ROWS, "offset": 0}}]}, actor)["reads"]["late_po_list"]
                mine = {r[listed["columns"].index("handle")] for r in listed["rows"]} & truth
                handles, out = self.alerts(actor)
                self.assertEqual(handles, mine)
        full, _ = self.alerts(FULL)
        ops, out = self.alerts(OPS)
        self.assertTrue(ops and ops < full)
        text = json.dumps(out, default=str)
        for word in ["PO-FIN"] + list(tbv.SECRET):
            self.assertNotIn(word, text)
        # one alert OPS sees, computed also from a row of a class OPS lacks: hidden from OPS
        some = sorted(ops)[0]
        hidden_cls = self.truth("SELECT access_class FROM {biz}.purchase_orders WHERE "
                                "handle = ANY(%s)", (sorted(full - ops),))[0][0]
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"UPDATE {bv.SCHEMA}.alerts SET needs = needs || %s::smallint "
                      "WHERE handle = %s", (hidden_cls, some))
            self.assertNotIn(some, self.alerts(OPS)[0])
            self.assertIn(some, self.alerts(FULL)[0])
            # a column class the door no longer knows: nobody
            c.execute(f"UPDATE {bv.SCHEMA}.alerts SET col_needs = '{{987654}}' "
                      "WHERE handle = %s", (some,))
            self.assertNotIn(some, self.alerts(FULL)[0])
            # a mail alert (comms units, no record class) is never on a page through here
            c.execute(f"INSERT INTO {bv.SCHEMA}.alerts (rule, kind, record_table, handle, why, "
                      "since, first_seen, last_seen, state, access_class, needs, units, ai_ok) "
                      "VALUES ('po_late', 'reply_owed', 'emails', 'mail-1', 'w', now(), now(), "
                      "now(), 'open', NULL, '{}', '{1}', true)")
            self.assertNotIn("mail-1", self.alerts(FULL)[0])
            # the AI's door has no alerts view at all
            out = door.SqlTool(self.base()).call("plx.sql", {"sql": "SELECT * FROM alerts"},
                                                 FULL)
            self.assertIn("error", out)
            c.execute(f"DELETE FROM {bv.SCHEMA}.alerts WHERE handle = 'mail-1'")
        self.assertEqual(self.alerts(FULL, rules="no_such_rule")[0], set())

    # ---- input: unknown or unexpected fails closed --------------------------------------

    def test_unknown_reads_and_parameters_are_refused_before_any_sql(self):
        bad = [
            {}, {"reads": []}, {"reads": "late_po_count"}, {"reads": [{"name": "x"}]},
            {"reads": [{"name": "late_po_count"}], "sql": "SELECT 1"},
            {"reads": [{"name": "late_po_count"}], "ids": "yes"},
            {"reads": [{"name": "late_po_count", "params": {"limit": 1}}]},
            {"reads": [{"name": "late_po_count", "sql": "SELECT 1"}]},
            {"reads": [{"name": "late_po_list", "params": {"limit": 5}}]},
            {"reads": [{"name": "late_po_list", "params": {"limit": 0, "offset": 0}}]},
            {"reads": [{"name": "late_po_list", "params": {"limit": pr.MAX_ROWS + 1,
                                                           "offset": 0}}]},
            {"reads": [{"name": "late_po_list", "params": {"limit": True, "offset": 0}}]},
            {"reads": [{"name": "late_po_list", "params": {"limit": "5", "offset": 0}}]},
            {"reads": [{"name": "late_po_list", "params": {"limit": 5, "offset": -1}}]},
            {"reads": [{"name": "late_po_list", "params": {"limit": 5, "offset": 0,
                                                           "where": "1=1"}}]},
            {"reads": [{"name": "late_po_count"}] * (pr.MAX_READS + 1)},
            {"reads": [{"name": "SELECT * FROM purchase_orders"}]},
        ]
        with mock.patch.object(door, "scoped_read", side_effect=AssertionError("ran")):
            for args in bad:
                with self.subTest(args=args):
                    out = self.page(args)
                    self.assertIn(out["error"]["code"], ("invalid_request", "unknown_read"),
                                  out)

    def test_a_sqlite_store_is_refused(self):
        base = SimpleNamespace(store=SimpleNamespace(conn=object()))
        out = self.page({"reads": [{"name": "late_po_count"}]}, base=base)
        self.assertEqual(out["error"]["code"], "not_served")

    # ---- the same door, its own connections ---------------------------------------------

    def test_the_scope_and_transaction_are_plx_sqls_own(self):
        base = self.base()
        seen = []
        real = door.scoped_read

        def spy(pg, reader, work, seconds, **kw):
            seen.append(reader)
            self.assertEqual(kw, {"person": True})   # a person's page: the page views
            return real(pg, reader, work, seconds, **kw)
        with mock.patch.object(door, "scoped_read", spy):
            self.assertNotIn("error", self.page({"reads": [{"name": "late_po_count"}]},
                                                OPS, base))
        # plx.sql's door exactly (the same grant, decided in the read's own transaction);
        # none of the communications views' settings (``COMMS_SETTINGS``) beside it
        from operating.pg import door as pg_door
        conn, scope = door.SqlTool(base)._reader(OPS)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], door.Door)
        self.assertEqual(seen[0].grant.digest, door.reader_grant(scope).digest)
        settings = seen[0].decided["settings"]
        self.assertFalse(set(settings) & door.COMMS_SETTINGS)
        self.assertIn(pg_door.HELD, settings)

    def test_a_page_never_waits_on_the_store_connection_lock(self):
        """The reads run on the page pool, never on the store connection an Ask's plx.sql
        holds for its whole statement. (The reader's scope is computed by the runtime that
        serves the call: the API's access worker, its own runtime and connection.)"""
        base = self.base()
        reader = door.reader_door(base, FULL)   # the scope, read by the serving runtime
        held, release = threading.Event(), threading.Event()

        def ask_holding_the_store():
            with base.store.conn._lock:      # an Ask's plx.sql statement in progress
                held.set()
                release.wait(20)
        t = threading.Thread(target=ask_holding_the_store, daemon=True)
        t.start()
        try:
            self.assertTrue(held.wait(5))
            t0 = time.monotonic()
            with mock.patch.object(door, "reader_door", return_value=reader):
                out = self.page({"reads": [{"name": "late_po_count"}]}, FULL, base)
            self.assertNotIn("error", out, out)
            self.assertLess(time.monotonic() - t0, 5)
            self.assertTrue(t.is_alive())    # the lock was held all along
        finally:
            release.set()
            t.join(5)

    def test_not_a_model_tool(self):
        from pathlib import Path
        from operating import mcp_server_catalog, ontology_host
        self.assertIn(pr.TOOL_NAME, ontology_host.table_tool_names())
        listed = set()
        for value in vars(mcp_server_catalog).values():
            if isinstance(value, (frozenset, set, tuple, list)):
                listed |= {v for v in value if isinstance(v, str)}
            elif isinstance(value, dict):
                listed |= {k for k in value if isinstance(k, str)}
        self.assertNotIn(pr.TOOL_NAME, listed)
        agents = Path(mcp_server_catalog.__file__).with_name("agent_definitions.json")
        self.assertNotIn(pr.TOOL_NAME, agents.read_text(encoding="utf-8"))

    # ---- the endpoints: Home and the late list --------------------------------------------

    def screens(self, actor, routing=None):
        from operating import briefing as bf
        from operator_api.backend_screens import ScreensMixin
        from operator_api import models as m
        base = self.base()
        routed = routing or bf.Routing(basis="not_an_employee", everything=True)

        class View:
            def _call(self, name, args):
                assert name == pr.TOOL_NAME
                return pr.PageReads(base).call(args, actor)

            def routing(self):
                return routed

        class Worker:
            def run(self, reader, job, timeout=0, priority=0):
                return job(View())

        class Screens(ScreensMixin):
            def __init__(self):
                self._lock, self._recent = threading.Lock(), {}
                self.access_worker, self.reads = Worker(), None

            def freshness(self, *a, **k):
                return m.Freshness()
        return Screens(), SimpleNamespace(key="r", display_name="Pat Reader")

    def test_home_and_late_list_numbers_are_the_truth_for_each_reader(self):
        from operator_api.serving import LATE_LIST_LIMIT
        late_all = self.truth("SELECT count(*) FROM {biz}.purchase_orders "
                              "WHERE verdict = 'late'")[0][0]
        marked = len(range(0, MORE, FINANCE_EVERY)) + 1
        with mock.patch.dict("os.environ", {"PLX_PAGES_DOOR": "1"}):
            for actor, total in ((FULL, late_all), (OPS, late_all - marked)):
                with self.subTest(actor=sorted(actor.principals)):
                    screens, reader = self.screens(actor)
                    t0 = time.monotonic()
                    home = screens.home(reader)
                    ms = (time.monotonic() - t0) * 1000
                    na = home.needs_attention
                    self.assertEqual((na.status, na.late_total), ("read", total))
                    self.assertEqual([p.label for p in na.late_pos],
                                     [f"PO {n}" for n in self.late_list(actor, LATE_LIST_LIMIT)])
                    self.assertTrue(all(p.object_id.startswith("PurchaseOrder:")
                                        and p.open_url.startswith("/object/PurchaseOrder/")
                                        for p in na.late_pos))
                    self.assertLess(ms, 2000)
                    seen, offset = [], 0
                    while True:
                        page = screens.late_list(reader, offset=offset, limit=25)
                        self.assertEqual(page.total, total)
                        seen += [p.label for p in page.items]
                        offset = page.next_offset
                        if not page.more:
                            break
                    self.assertEqual(len(seen), total)
                    self.assertEqual(len(set(seen)), total)
            one = screens.late_list(reader, offset=0, limit=25).items[0]
            self.assertTrue(one.amount is None or one.amount.currency)

    def test_a_failed_read_says_unavailable_never_none(self):
        from operator_api import errors
        with mock.patch.dict("os.environ", {"PLX_PAGES_DOOR": "1"}):
            screens, reader = self.screens(FULL)
            with mock.patch.object(pr.PageReads, "call", return_value={
                    "error": {"code": "timeout", "message": "x"}}):
                home = screens.home(reader)
                self.assertEqual(home.needs_attention.status, "unavailable")
                self.assertIsNone(home.needs_attention.late_total)
                self.assertEqual(home.needs_attention.late_pos, [])
                with self.assertRaises(errors.UserFacing):
                    screens.late_list(reader)

    def test_alerts_switch_home_watching_and_late_list_show_only_on_types_a_reader_may_read(self):
        """PLX_ALERTS_SQL=1: Home's late POs are the po_late alerts the reader may see, its
        at-risk list and the briefing (Watching) hold only types on for the reader (an
        off type such as customer_order_open_long never, though it is open), each type
        pages without loss, NOBODY sees nothing, and a failed read says unavailable."""
        import psycopg
        from operating.pg import alerts as al
        from operator_api import errors
        got = al.run(self.dsn, ["po_late"], schema=bv.SCHEMA, owners={})
        self.assertEqual(got[0]["status"], "ok", got)
        late = {r[0]: r[1] for r in self.truth(
            "SELECT handle, access_class FROM {biz}.purchase_orders "
            "WHERE verdict = 'late' AND days_late > 0")}
        one = sorted(late)[0]
        with psycopg.connect(self.dsn, autocommit=True) as c:
            for rule, kind in (("lot_on_hold", "quality_hold"),
                               ("customer_order_open_long", "customer_order_late")):
                self.put_alert(c, rule, kind, one, late[one])
        try:
            with mock.patch.dict("os.environ", {"PLX_ALERTS_SQL": "1"}):
                self.assertNotIn("customer_order_open_long", __import__(
                    "operator_api.page_sql", fromlist=["x"]).alert_types())
                for actor in (FULL, OPS, NOBODY):
                    with self.subTest(actor=sorted(actor.principals)):
                        handles, _ = self.alerts(actor)
                        sees_one = one in self.alerts(actor, "lot_on_hold")[0]
                        screens, reader = self.screens(actor)
                        na = screens.home(reader).needs_attention
                        self.assertEqual((na.status, na.late_total), ("read", len(handles)))
                        self.assertEqual([r.kind for r in na.at_risk],
                                         ["quality_hold"] if sees_one else [])
                        b = screens.briefing(reader)
                        self.assertEqual(b.status, "read")
                        # (the rules this fixture never ran are groups saying unavailable)
                        counts = {g.event_type: g.count for g in b.groups if g.count}
                        self.assertTrue(all(g.unavailable for g in b.groups if not g.count))
                        self.assertEqual(counts, {k: v for k, v in (
                            ("po_late", len(handles)), ("lot_on_hold", int(sees_one))) if v})
                        self.assertEqual(b.count, len(handles) + int(sees_one))
                        self.assertNotIn("customer_order_open_long",
                                         json.dumps(b.model_dump(), default=str))
                        seen, offset = [], 0
                        while True:
                            page = screens.briefing_items(reader, "po_late", offset=offset,
                                                          limit=7)
                            self.assertEqual(page.total, len(handles))
                            seen += [i.sources[0].handle for i in page.items]
                            offset += len(page.items)
                            if not page.more:
                                break
                        self.assertEqual(sorted(seen), sorted(handles))
                        listed, offset = [], 0
                        while True:
                            pg = screens.late_list(reader, offset=offset, limit=25)
                            self.assertEqual(pg.total, len(handles))
                            listed += [p.sources[0].handle for p in pg.items]
                            offset = pg.next_offset
                            if not pg.more:
                                break
                        self.assertEqual(sorted(listed), sorted(handles))
                        if actor is NOBODY:
                            self.assertEqual((b.count, na.late_total, na.at_risk), (0, 0, []))
                screens, reader = self.screens(FULL)
                with mock.patch.object(pr.PageReads, "call", return_value={
                        "error": {"code": "timeout", "message": "x"}}):
                    home = screens.home(reader)
                    self.assertEqual((home.needs_attention.status,
                                      home.needs_attention.late_pos,
                                      home.needs_attention.at_risk), ("unavailable", [], []))
                    self.assertEqual(screens.briefing(reader).status, "unavailable")
                    with self.assertRaises(errors.UserFacing):
                        screens.late_list(reader)
        finally:
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute(f"DELETE FROM {bv.SCHEMA}.alerts WHERE rule IN "
                          "('lot_on_hold', 'customer_order_open_long')")
                c.execute(f"DELETE FROM {bv.SCHEMA}.alert_runs WHERE rule IN "
                          "('lot_on_hold', 'customer_order_open_long')")


    # ---- the alerts at READ time: exclusion, markings, staleness, runs, routing ------------

    def put_alert(self, c, rule, kind, handle, cls, *, ran=True):
        """An open alert on a purchase order row (its own source row) and its rule's run."""
        c.execute(f"INSERT INTO {bv.SCHEMA}.alerts (rule, kind, record_table, handle, why, "
                  "since, first_seen, last_seen, state, access_class, needs, ai_ok, "
                  "source_table, sources, objects) VALUES (%s, %s, 'purchase_orders', %s, "
                  "'held', now(), now(), now(), 'open', %s, '{}', true, 'purchase_orders', "
                  "ARRAY[%s], '{}')", (rule, kind, handle, cls, handle))
        if ran:
            c.execute(f"INSERT INTO {bv.SCHEMA}.alert_runs (rule, run_at, status, ok_at) "
                      "VALUES (%s, now(), 'ok', now()) ON CONFLICT (rule) DO UPDATE SET "
                      "ok_at = now(), status = 'ok'", (rule,))

    def po_row(self, actor, handle):
        out = self.page({"reads": [{"name": "purchase_order", "params": {"handle": handle}}]},
                        actor)
        got = out["reads"]["purchase_order"]
        return [dict(zip(got["columns"], r)) for r in got["rows"]]

    def test_alerts_follow_the_people_exclusion_and_markings_at_read_time(self):
        """Excluded data is never shown, with no lag. The person exclusion
        growing hides an alert computed from one of its objects in the same read (before
        the tables follow it, before the rule runs again); a marking tightened in place on
        the record row hides it from exactly the readers the door now withholds the shown
        column from; nothing of the stored alert changed in between."""
        import psycopg
        from operating.pg import alerts as al
        from operating.pg import door as pg_door
        self.assertEqual(al.run(self.dsn, ["po_late"], schema=bv.SCHEMA,
                               owners={})[0]["status"], "ok")
        full, ops = self.alerts(FULL)[0], self.alerts(OPS)[0]
        some = sorted(ops)[0]
        objs, stored = self.truth("SELECT objects, col_needs FROM {biz}.alerts WHERE "
                                  "rule = 'po_late' AND handle = %s", (some,))[0]
        own = self.truth("SELECT object_ids FROM {biz}.lineage WHERE view = "
                         "'purchase_orders' AND handle = %s", (some,))[0][0]
        self.assertTrue(own and set(own) <= set(objs), (own, objs))
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('t-excl', %s, 'participant')", (own[0],))
            try:
                for actor in (FULL, OPS):
                    self.assertNotIn(some, self.alerts(actor)[0])
                self.assertLessEqual(self.alerts(FULL)[0], full - {some})
            finally:
                c.execute("DELETE FROM ontology.people_excluded_objects "
                          "WHERE l1_handle = 't-excl'")
            self.assertIn(some, self.alerts(OPS)[0])
            # a marking on a shown column (supplier) tightened in place, the door's views
            # made again as an in-place marking change makes them; no rule run
            old = c.execute(f"SELECT col_class FROM {bv.SCHEMA}.purchase_orders WHERE "
                            "handle = %s", (some,)).fetchone()[0]
            c.execute(f"INSERT INTO {bv.SCHEMA}.col_classes (id, marks, needs) VALUES "
                      "(4242, %s, '{}') ON CONFLICT (id) DO NOTHING",
                      (json.dumps({"supplier": json.dumps(["t-no-reader-holds-this"])}),))

            def remark(col_class):
                c.execute(f"UPDATE {bv.SCHEMA}.purchase_orders SET col_class = %s "
                          "WHERE handle = %s", (col_class, some))
                with c.transaction():
                    pg_door.recount(c, bv.SCHEMA, "purchase_orders")
                    sql_views.create(c, bv.VIEWS["purchase_orders"], bv.SCHEMA)
            remark(4242)
            try:
                hidden_from = []
                for actor in (FULL, OPS):
                    row = self.po_row(actor, some)
                    withheld = bool(row) and "supplier" in (row[0]["withheld"] or [])
                    self.assertEqual(some in self.alerts(actor)[0], bool(row) and not withheld,
                                     (sorted(actor.principals), row))
                    if withheld:
                        hidden_from.append(actor)
                self.assertTrue(hidden_from, "the tightened marking withheld from nobody")
                self.assertEqual(self.truth("SELECT col_needs FROM {biz}.alerts WHERE "
                                            "rule = 'po_late' AND handle = %s", (some,))[0][0],
                                 stored)
            finally:
                remark(old)
                c.execute(f"DELETE FROM {bv.SCHEMA}.col_classes WHERE id = 4242")
                with c.transaction():
                    pg_door.recount(c, bv.SCHEMA, "purchase_orders")
                    sql_views.create(c, bv.VIEWS["purchase_orders"], bv.SCHEMA)
            self.assertIn(some, self.alerts(OPS)[0])

    def test_a_rule_not_run_for_two_cycles_says_unavailable_since_never_old_items(self):
        import psycopg
        from operating.pg import alerts as al
        from operator_api import errors
        self.assertEqual(al.run(self.dsn, ["po_late"], schema=bv.SCHEMA,
                               owners={})[0]["status"], "ok")
        late = {r[0]: r[1] for r in self.truth(
            "SELECT handle, access_class FROM {biz}.purchase_orders "
            "WHERE verdict = 'late' AND days_late > 0")}
        one = sorted(late)[0]
        ago = f"UPDATE {bv.SCHEMA}.alert_runs SET ok_at = now() - %s * interval '1 minute' " \
              "WHERE rule = 'po_late'"
        with psycopg.connect(self.dsn, autocommit=True) as c:
            self.put_alert(c, "lot_on_hold", "quality_hold", one, late[one])
            try:
                with mock.patch.dict("os.environ", {"PLX_ALERTS_SQL": "1"}):
                    screens, reader = self.screens(FULL)
                    c.execute(ago, (al.STALE_MINUTES - 1,))    # within two cycles: current
                    self.assertEqual(screens.home(reader).needs_attention.status, "read")
                    self.assertTrue(self.alerts(FULL)[0])
                    c.execute(ago, (al.STALE_MINUTES + 1,))    # past them: never shown
                    self.assertEqual(self.alerts(FULL)[0], set())
                    self.assertEqual(screens.home(reader).needs_attention.status,
                                     "unavailable")
                    with self.assertRaises(errors.UserFacing) as cm:
                        screens.late_list(reader)
                    self.assertEqual(cm.exception.code, "alerts_stale")
                    self.assertIn("unavailable since", cm.exception.message)
                    self.assertIn(" ET", cm.exception.message)
                    with self.assertRaises(errors.UserFacing) as cm:
                        screens.briefing_items(reader, "po_late")
                    self.assertEqual(cm.exception.code, "alerts_stale")
                    b = screens.briefing(reader)
                    groups = {g.event_type: g for g in b.groups}
                    self.assertTrue(groups["po_late"].unavailable)
                    self.assertEqual(groups["po_late"].count, 0)
                    self.assertIsNotNone(groups["po_late"].unavailable_since)
                    self.assertNotIn("po_late", {i.event_type for i in b.items})
                    self.assertEqual((groups["lot_on_hold"].count,
                                      groups["lot_on_hold"].unavailable), (1, False))
                    self.assertIsNotNone(groups["lot_on_hold"].as_of)
                    self.assertEqual((b.count, b.refreshing), (1, True))
                    self.assertEqual(b.checked_on, groups["po_late"].unavailable_since)
                    # never finished at all: unavailable, since nothing
                    c.execute(f"DELETE FROM {bv.SCHEMA}.alert_runs WHERE rule = 'po_late'")
                    g = {g.event_type: g for g in screens.briefing(reader).groups}["po_late"]
                    self.assertEqual((g.unavailable, g.unavailable_since), (True, None))
                    # a failed or blocked run keeps the last finished one's time
                    al.run(self.dsn, ["po_late"], schema=bv.SCHEMA, owners={})
                    c.execute(f"UPDATE {bv.SCHEMA}.alert_runs SET ok_at = now() - interval "
                              "'2 hours' WHERE rule = 'po_late'")
                    kept = c.execute(f"SELECT ok_at FROM {bv.SCHEMA}.alert_runs WHERE "
                                     "rule = 'po_late'").fetchone()[0]
                    c.execute(f"ALTER TABLE {bv.SCHEMA}.purchase_orders RENAME TO po_away")
                    try:
                        got = al.run(self.dsn, ["po_late"], schema=bv.SCHEMA, owners={})
                    finally:
                        c.execute(f"ALTER TABLE {bv.SCHEMA}.po_away RENAME TO purchase_orders")
                    self.assertEqual(got[0]["status"], "blocked")
                    self.assertEqual(c.execute(f"SELECT ok_at FROM {bv.SCHEMA}.alert_runs "
                                               "WHERE rule = 'po_late'").fetchone()[0], kept)
            finally:
                c.execute(f"DELETE FROM {bv.SCHEMA}.alerts WHERE rule = 'lot_on_hold'")
                c.execute(f"DELETE FROM {bv.SCHEMA}.alert_runs WHERE rule = 'lot_on_hold'")
                al.run(self.dsn, ["po_late"], schema=bv.SCHEMA, owners={})

    def test_a_run_never_drops_the_page_view_nor_waits_on_a_reader(self):
        """The runner keeps the page view (no drop and create per run, no ALTER when the
        columns are there): a run finishes while a reader holds the view open, and the view
        is the same object after it."""
        import psycopg
        from operating.pg import alerts as al
        al.run(self.dsn, ["po_late"], schema=bv.SCHEMA, owners={})
        page = sql_views.page_schema(bv.SCHEMA)
        oid = f"SELECT to_regclass('{page}.alerts')::oid"
        before = self.truth(oid)[0][0]
        self.assertIsNotNone(before)
        with psycopg.connect(self.dsn) as reader:
            reader.execute(f"SELECT count(*) FROM {page}.alerts")      # holds its locks
            done = []
            t = threading.Thread(target=lambda: done.append(al.run(
                self.dsn, ["po_late"], schema=bv.SCHEMA, owners={})), daemon=True)
            t.start()
            t.join(60)
            self.assertFalse(t.is_alive(), "a run waited on a reader of the page view")
            reader.rollback()
        self.assertEqual(done[0][0]["status"], "ok")
        self.assertEqual(self.truth(oid)[0][0], before)

    def test_alerts_switch_a_failed_type_read_says_unavailable_never_500(self):
        from operator_api import errors
        with mock.patch.dict("os.environ", {"PLX_ALERTS_SQL": "1"}):
            screens, reader = self.screens(FULL)
            with mock.patch.object(pr.PageReads, "call", return_value={
                    "error": {"code": "timeout", "message": "x"}}):
                with self.assertRaises(errors.UserFacing) as cm:
                    screens.briefing_items(reader, "po_late")
                self.assertEqual(cm.exception.code, "alerts_not_read")

    def test_alerts_switch_a_failed_routing_read_says_unavailable_never_empty_complete(self):
        from operator_api import errors, page_sql
        with mock.patch.dict("os.environ", {"PLX_ALERTS_SQL": "1"}):
            screens, reader = self.screens(FULL)
            for bad in (RuntimeError("boom"), TimeoutError("5 s")):
                with mock.patch.object(screens.access_worker, "run", side_effect=bad):
                    got = page_sql.alert_briefing(screens, reader, since_visit=False)
                    self.assertEqual(got.status, "unavailable")
                    self.assertFalse(got.complete)
                    with self.assertRaises(errors.UserFacing) as cm:
                        screens.briefing_items(reader, "po_late")
                    self.assertEqual(cm.exception.code, "alerts_not_read")

    def test_alerts_switch_respects_the_readers_routing_and_own_types(self):
        """A type routed to other departments is counted as other teams', never listed; an
        off type is listed only for a reader whose own watch rule names it."""
        import tempfile
        import psycopg
        from pathlib import Path
        from operating import briefing as bf
        from operating import watchlists as wl
        from operating.pg import alerts as al
        al.run(self.dsn, ["po_late"], schema=bv.SCHEMA, owners={})
        late = {r[0]: r[1] for r in self.truth(
            "SELECT handle, access_class FROM {biz}.purchase_orders "
            "WHERE verdict = 'late' AND days_late > 0")}
        one = sorted(late)[0]
        full = self.alerts(FULL)[0]
        with psycopg.connect(self.dsn, autocommit=True) as c, \
                tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict("os.environ", {"PLX_ALERTS_SQL": "1"}):
            self.put_alert(c, "lot_on_hold", "quality_hold", one, late[one])
            self.put_alert(c, "customer_order_late", "customer_order_late", one, late[one])
            try:
                quality = bf.Routing(frozenset({"QUALITY"}))       # not purchasing
                screens, reader = self.screens(FULL, routing=quality)
                b = screens.briefing(reader)
                self.assertEqual({g.event_type for g in b.groups}, {"lot_on_hold"})
                self.assertEqual((b.count, b.other_teams), (1, len(full)))
                self.assertEqual(screens.briefing_items(reader, "po_late").total, 0)
                na = screens.home(reader).needs_attention
                self.assertEqual([r.kind for r in na.at_risk], ["quality_hold"])
                # nothing routed (rights unresolved): nothing listed
                screens, reader = self.screens(FULL, routing=bf.Routing(basis="x"))
                self.assertEqual(screens.briefing(reader).groups, [])
                # an off type: only with the reader's own watch rule naming it
                screens, reader = self.screens(FULL)
                path = Path(tmp) / "w.sqlite"
                ctx = SimpleNamespace(path=path, reader="r")
                with mock.patch("operator_api.watch.view_context", return_value=ctx):
                    b = screens.briefing(reader)
                    self.assertNotIn("customer_order_late", {g.event_type for g in b.groups})
                    wl.WatchStore(path).add_rule("r", "late customer orders", {
                        "schema": wl.RULE_SCHEMA, "signal": {"kinds": [
                            "late_customer_order"]}}, "late customer orders")
                    b = screens.briefing(reader)
                    self.assertEqual({g.event_type: g.count for g in b.groups}
                                     .get("customer_order_late"), 1)
                    self.assertEqual(screens.briefing_items(
                        reader, "customer_order_late").total, 1)
                    # another reader's rule switches nothing on for this one
                    other = SimpleNamespace(path=path, reader="someone-else")
                    with mock.patch("operator_api.watch.view_context", return_value=other):
                        self.assertNotIn("customer_order_late",
                                         {g.event_type for g in screens.briefing(reader).groups})
            finally:
                c.execute(f"DELETE FROM {bv.SCHEMA}.alerts WHERE rule IN "
                          "('lot_on_hold', 'customer_order_late')")
                c.execute(f"DELETE FROM {bv.SCHEMA}.alert_runs WHERE rule IN "
                          "('lot_on_hold', 'customer_order_late')")


if __name__ == "__main__":
    unittest.main()
