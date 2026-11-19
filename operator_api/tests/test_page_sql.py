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
from operator_api import page_sql as ps

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
                self.warmed = []

            def warm(self, reader, **kw):
                self.warmed.append(reader.key)
                return False

            def freshness(self, *a, **k):
                return m.Freshness()

            def _serving(self, reader, name, read, inputs_of=None, reading=None):
                return read()   # nothing kept here: the read itself

            def _screen_inputs(self, **kw):
                return lambda: None
        return Screens(), SimpleNamespace(key="r", display_name="Pat Reader", session=None)

    def assert_sidebar_is_the_late_list(self, screens, reader):
        """The sidebar's count and suppliers equal the reader's whole late list paged."""
        from collections import Counter
        listed, offset = [], 0
        while True:
            pg = screens.late_list(reader, offset=offset, limit=25)
            listed += pg.items
            offset = pg.next_offset
            if not pg.more:
                break
        by = Counter(p.supplier for p in listed if p.supplier)
        side = screens.sidebar(reader)
        self.assertEqual(side.late_total, len(listed))
        self.assertEqual(side.supplier_total, len(by))
        from operator_api.backend_screens import SIDEBAR_SUPPLIERS
        self.assertEqual([(s.supplier, s.late) for s in side.late_suppliers],
                         sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))[:SIDEBAR_SUPPLIERS])
        self.assertFalse(side.partial)
        return side

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
                    # each opens its PO page through the door (not the old record page)
                    self.assertTrue(all(p.object_id.startswith("PurchaseOrder:")
                                        and p.open_url == ps.account_url(
                                            "purchase_order", p.sources[0].handle)
                                        for p in na.late_pos), [p.open_url
                                                                for p in na.late_pos])
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
                    side = self.assert_sidebar_is_the_late_list(screens, reader)
                    self.assertTrue(side.late_suppliers)   # the fixture's own suppliers
            one = screens.late_list(reader, offset=0, limit=25).items[0]
            self.assertTrue(one.amount is None or one.amount.currency)

    def test_a_record_id_opens_its_rows_page_only_for_a_reader_who_may_read_it(self):
        """A link naming a record (the old record page's id) opens the page of the door's
        row it was read into; a reader who may not read the row, an id no row was read
        from and a kind the door has no page for all say not found (the handle found for
        the record never reaches the reader)."""
        from operator_api import backend_serving
        marked = self.truth("SELECT p.handle, l.object_ids FROM {biz}.purchase_orders p "
                            "JOIN {biz}.lineage l ON l.view = 'purchase_orders' AND "
                            "l.handle = p.handle WHERE p.po_number = %s", ("PO-L00",))
        plain = self.truth("SELECT p.handle, l.object_ids FROM {biz}.purchase_orders p "
                           "JOIN {biz}.lineage l ON l.view = 'purchase_orders' AND "
                           "l.handle = p.handle WHERE p.po_number = %s", ("PO-L01",))
        (mh, mids), (h, ids) = marked[0], plain[0]
        screens, reader = self.screens(FULL)
        for oid in ids:
            self.assertEqual(ps.resolve(screens, reader, "purchase_order", oid), h)
        page = ps.order(screens, reader, "purchase_order", ps.resolve(
            screens, reader, "purchase_order", ids[0]))
        self.assertEqual(page.title, "PO-L01")
        with mock.patch.dict("os.environ", {"PLX_PAGES_DOOR": "1"}):
            got = backend_serving.ServingMixin.biz_page(screens, reader, "purchase_order",
                                                        record=ids[0])
            self.assertEqual(got.title, page.title)
            ops_screens, ops_reader = self.screens(OPS)
            from operator_api import errors
            with self.assertRaises(errors.UserFacing) as no:
                backend_serving.ServingMixin.biz_page(ops_screens, ops_reader,
                                                      "purchase_order", record=mids[0])
            self.assertEqual(no.exception.status, 404)
            self.assertNotIn(mh, str(no.exception))
            for kind, oid in (("purchase_order", "PurchaseOrder:none:{}"),
                              ("sales_order", ids[0]), ("chain", ids[0])):
                with self.subTest(kind=kind, oid=oid), self.assertRaises(errors.UserFacing):
                    backend_serving.ServingMixin.biz_page(screens, reader, kind, record=oid)
        # one page kind at a time: the kinds named are on, every other says pages_off and
        # its links stay on the record's own page; the search box and Home need them all
        with mock.patch.dict("os.environ", {"PLX_PAGES_DOOR": "customer,chain,sales_order"}):
            self.assertEqual((ps.on("customer"), ps.on("sales_order"), ps.on("purchase_order"),
                              ps.on("bogus"), ps.on()), (True, True, False, False, False))
            with self.assertRaises(errors.UserFacing) as off:
                backend_serving.ServingMixin.biz_page(screens, reader, "purchase_order",
                                                      record=ids[0])
            self.assertEqual(off.exception.code, "pages_off")
            self.assertEqual(ps.record_url("purchase_orders", h, "PurchaseOrder", ids[0]),
                             ps.object_url("PurchaseOrder", ids[0]))
            self.assertEqual(ps.record_url("sales_orders", "api://so/1"),
                             ps.account_url("sales_order", "api://so/1"))
        for bad in ({"resolve": {"view": "lineage", "object_id": "x"}},
                    {"resolve": {"view": "purchase_orders", "object_id": ""}},
                    {"resolve": {"view": "purchase_orders", "object_id": "x"}, "reads": []}):
            self.assertEqual(pr.PageReads(self.base()).call(bad, FULL)["error"]["code"],
                             "invalid_request")

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
                        # Home still reads the reader's other screens ahead
                        self.assertEqual(screens.warmed, ["r"])
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
                        side = self.assert_sidebar_is_the_late_list(screens, reader)
                        if actor is NOBODY:
                            self.assertEqual((side.late_total, side.late_suppliers), (0, []))
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
                    # the sidebar not read: no count at all, never "0 late"
                    side = screens.sidebar(reader)
                    self.assertEqual((side.late_total, side.late_suppliers), (None, []))
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
                    # nor a sidebar count from the stale rule
                    self.assertIsNone(screens.sidebar(reader).late_total)
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
                    # another reader's rule switches nothing on for this one (a fresh
                    # server: the reader's scope is kept for a screen's burst of reads)
                    screens, reader = self.screens(FULL)
                    other = SimpleNamespace(path=path, reader="someone-else")
                    with mock.patch("operator_api.watch.view_context", return_value=other):
                        self.assertNotIn("customer_order_late",
                                         {g.event_type for g in screens.briefing(reader).groups})
            finally:
                c.execute(f"DELETE FROM {bv.SCHEMA}.alerts WHERE rule IN "
                          "('lot_on_hold', 'customer_order_late')")
                c.execute(f"DELETE FROM {bv.SCHEMA}.alert_runs WHERE rule IN "
                          "('lot_on_hold', 'customer_order_late')")

    def test_alerts_switch_suggestion_cards_are_the_alerts_home_lists(self):
        """PLX_ALERTS_SQL=1: the suggestion cards are made from the reader's open SQL
        alerts of the suggestion types, so each card's subject is a late PO Home lists for
        that reader (FULL, OPS without the finance-marked ones, NOBODY none); a type off
        for the reader (customer_order_late) or no suggestion (lot_on_hold) never; nothing
        routed, nothing; a stale rule is not complete; a failed read is unavailable."""
        import psycopg
        from operating import briefing as bf
        from operating import proactive_suggestions as sug
        from operating.pg import alerts as al
        # every suggestion rule runs, as on the timer: a type with no finished run would
        # make the list incomplete
        rules = ["po_late", "po_due", "po_undated", "eta_slipped", "negative_margin",
                 "customer_order_at_risk"]
        got = al.run(self.dsn, rules, schema=bv.SCHEMA, owners={})
        self.assertEqual({g["status"] for g in got}, {"ok"}, got)
        late = {r[0]: r[1] for r in self.truth(
            "SELECT handle, access_class FROM {biz}.purchase_orders "
            "WHERE verdict = 'late' AND days_late > 0")}
        one = sorted(late)[0]
        with psycopg.connect(self.dsn, autocommit=True) as c, \
                mock.patch.dict("os.environ", {"PLX_ALERTS_SQL": "1"}):
            self.put_alert(c, "lot_on_hold", "quality_hold", one, late[one])
            self.put_alert(c, "customer_order_late", "customer_order_late", one, late[one])
            try:
                for actor in (FULL, OPS, NOBODY):
                    with self.subTest(actor=sorted(actor.principals)):
                        handles, _ = self.alerts(actor)
                        screens, reader = self.screens(actor)
                        out = ps.alert_suggestion_items(screens, reader, sug.KIND_OF_TYPE)
                        # every page-path rule is current; the mail rule (no comms door
                        # here) is said unavailable, so the list is never called complete
                        self.assertEqual((out["status"], out["complete"],
                                          out["unavailable_types"]),
                                         ("read", False, ["vendor_no_reply"]))
                        items = out["items"]
                        self.assertEqual({i["event_type"] for i in items},
                                         {"po_late"} if handles else set())
                        self.assertEqual(sorted(i["sources"][0]["handle"] for i in items),
                                         sorted(handles))    # each once, none hidden
                        home = screens.home(reader).needs_attention
                        built = sug.build(items, labels={}, trails={}, late_drafts={})
                        self.assertEqual(built["total"], len(handles))
                        self.assertEqual(built["collapsed"], {})
                        self.assertTrue({s["subject"]["object_id"]
                                         for s in built["suggestions"]}
                                        <= {p.object_id for p in home.late_pos})
                self.assertGreater(len(self.alerts(FULL)[0]), len(self.alerts(OPS)[0]))
                # nothing routed (rights unresolved): no cards, never an error
                screens, reader = self.screens(FULL, routing=bf.Routing(basis="x"))
                self.assertEqual(ps.alert_suggestion_items(
                    screens, reader, sug.KIND_OF_TYPE)["items"], [])
                # a type the registry does not know is never asked for
                screens, reader = self.screens(FULL)
                self.assertEqual(ps.alert_suggestion_items(
                    screens, reader, {"no_such_rule"})["items"], [])
                # the rule's last run too old: its alerts are left out, not complete
                c.execute(f"UPDATE {bv.SCHEMA}.alert_runs SET ok_at = now() - interval "
                          "'3 days' WHERE rule = 'po_late'")
                screens, reader = self.screens(FULL)
                out = ps.alert_suggestion_items(screens, reader, sug.KIND_OF_TYPE)
                self.assertEqual((out["status"], out["complete"], out["items"]),
                                 ("read", False, []))
                # the read fails: unavailable, never an empty, complete list
                screens, reader = self.screens(FULL)
                with mock.patch.object(ps, "read", side_effect=ps.PageDoorUnavailable("x")):
                    out = ps.alert_suggestion_items(screens, reader, sug.KIND_OF_TYPE)
                self.assertEqual((out["status"], out["complete"]), ("unavailable", False))
                # a suggested type this path cannot show (a mail rule) is said unavailable
                screens, reader = self.screens(FULL)
                out = ps.alert_suggestion_items(screens, reader, {"vendor_no_reply"})
                self.assertEqual((out["items"], out["complete"], out["unavailable_types"]),
                                 ([], False, ["vendor_no_reply"]))
            finally:
                c.execute(f"DELETE FROM {bv.SCHEMA}.alerts WHERE rule IN "
                          "('lot_on_hold', 'customer_order_late')")
                c.execute(f"DELETE FROM {bv.SCHEMA}.alert_runs WHERE rule IN "
                          "('lot_on_hold', 'customer_order_late')")
                al.run(self.dsn, rules, schema=bv.SCHEMA, owners={})

    def test_at_risk_card_is_visible_through_the_page_door_and_follows_the_exclusion(self):
        """customer_order_at_risk rests on a late PO's chain but its alert is a row of
        sales_orders: the page view must show it (sources = the order's own handle) to a
        reader who holds both rows, and the exclusion of the late PO's own object hides it
        (the PO's lineage objects are recorded on the alert)."""
        import psycopg
        from operating.pg import alerts as al
        late = self.truth("SELECT p.handle FROM {biz}.purchase_orders p WHERE p.verdict = "
                          "'late' AND p.status = 'open' "
                          "ORDER BY 1 LIMIT 1")
        self.assertTrue(late)
        po = late[0][0]
        pobj = self.truth("SELECT object_ids[1] FROM {biz}.lineage WHERE view = "
                          "'purchase_orders' AND handle = %s", (po,))[0][0]
        links = [("PurchaseOrderLine.line_of", "L:z", pobj), ("PurchaseOrderLine.for_item", "L:z", "I:z"),
                 ("AssemblyOrderComponent.uses_item", "C:z", "I:z"),
                 ("AssemblyOrderComponent.component_of", "C:z", "AO:z"),
                 ("AssemblyOrderAllocation.allocates", "AL:z", "AO:z"),
                 ("AssemblyOrderAllocation.for_order_line", "AL:z", "OL:z"),
                 ("SalesOrderLine.line_of", "OL:z", "SO:z")]
        with psycopg.connect(self.dsn, autocommit=True) as c:
            cols = [r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name = 'sales_orders' ORDER BY ordinal_position", (bv.SCHEMA,))]
            self.assertTrue({"handle", "order_number", "status", "required_date", "of_record"}
                            <= set(cols))
            try:
                src = c.execute(f"SELECT handle FROM {bv.SCHEMA}.sales_orders ORDER BY 1 "
                                "LIMIT 1").fetchone()[0]
                lst = ", ".join(f"r.{k}" for k in cols if k != "handle")
                c.execute(f"INSERT INTO {bv.SCHEMA}.sales_orders (handle, {', '.join(k for k in cols if k != 'handle')}) "
                          f"SELECT 'zz-so', {lst} FROM {bv.SCHEMA}.sales_orders r WHERE r.handle = %s",
                          (src,))
                c.execute(f"UPDATE {bv.SCHEMA}.sales_orders SET status = 'open', "
                          "required_date = %s, of_record = true, order_number = 'ZZ1' "
                          "WHERE handle = 'zz-so'", (tbv.AS_OF - dt.timedelta(days=400),))
                c.execute(f"INSERT INTO {bv.SCHEMA}.lineage (view, handle, object_ids, systems, copies) "
                          "VALUES ('sales_orders', 'zz-so', ARRAY['SO:z'], ARRAY['sage'], 1)")
                for n, (k, a, b) in enumerate(links):
                    c.execute("INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                              "to_object, tier, binding_kind, why_not, markings, recorded_at, reason, "
                              "evidence_handles) "
                              "VALUES (%s, %s, %s, %s, 'verified', 'test', '', '[]', '2026-01-01', 'test', '[]')",
                              (f"zz-{n}", k, a, b))
                got = al.run(self.dsn, ["customer_order_at_risk"], schema=bv.SCHEMA, owners={},
                             day=tbv.AS_OF.isoformat())
                self.assertEqual(got[0]["status"], "ok", got)
                stored = self.truth("SELECT source_table, sources, objects FROM {biz}.alerts "
                                    "WHERE rule = 'customer_order_at_risk' AND handle = 'zz-so'")
                self.assertEqual(len(stored), 1, stored)
                self.assertEqual((stored[0][0], list(stored[0][1])), ("sales_orders", ["zz-so"]))
                self.assertTrue({"SO:z", pobj} <= set(stored[0][2]), stored)
                handles, _ = self.alerts(FULL, "customer_order_at_risk")
                self.assertIn("zz-so", handles)
                c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                          "reason) VALUES ('t-excl-po', %s, 'participant')", (pobj,))
                try:
                    self.assertNotIn("zz-so", self.alerts(FULL, "customer_order_at_risk")[0])
                finally:
                    c.execute("DELETE FROM ontology.people_excluded_objects "
                              "WHERE l1_handle = 't-excl-po'")
            finally:
                c.execute(f"DELETE FROM {bv.SCHEMA}.alerts WHERE rule = 'customer_order_at_risk'")
                c.execute(f"DELETE FROM {bv.SCHEMA}.alert_runs WHERE rule = 'customer_order_at_risk'")
                c.execute(f"DELETE FROM {bv.SCHEMA}.sales_orders WHERE handle = 'zz-so'")
                c.execute(f"DELETE FROM {bv.SCHEMA}.lineage WHERE handle = 'zz-so'")
                c.execute("DELETE FROM ontology.link_rows WHERE link_id LIKE 'zz-%'")


if __name__ == "__main__":
    unittest.main()


class ReaderScopeBurst(unittest.TestCase):
    """11-18 walk: one Home visit asked the reader's scope (visit, routing, rule types) 7
    times on the access workers (at-risk, briefing, each group's next page). It is read
    once per reader for SCOPE_SECONDS; never shared between readers, sign-in modes or
    backends; a visit marked seen reads it again; a scope that could not be read is not
    kept."""

    def test_one_read_per_reader_burst_and_none_shared(self):
        jobs = []

        class Worker:
            served: set = set()

            def run(self, reader, job, timeout, priority=0):
                jobs.append(reader.key)
                if reader.key == "broken":
                    raise TimeoutError("busy")
                return ps.ReaderScope("2026-11-18", {"routed": reader.key}, [])

        def who(key, mode="dev"):
            return SimpleNamespace(key=key, mode=mode,
                                   session=SimpleNamespace(principals=(key,)))
        one, other = SimpleNamespace(access_worker=Worker()), SimpleNamespace(
            access_worker=Worker())
        for _ in range(7):
            self.assertEqual(ps.reader_scope(one, who("a")).routing, {"routed": "a"})
        self.assertEqual(jobs, ["a"])
        self.assertEqual(ps.reader_scope(one, who("b")).routing, {"routed": "b"})
        ps.reader_scope(one, who("a", mode="entra"))
        ps.reader_scope(other, who("a"))
        self.assertEqual(jobs, ["a", "b", "a", "a"])
        ps.forget_scope(one, who("a"))       # their visit marked seen
        ps.reader_scope(one, who("a"))
        self.assertEqual(jobs[-1:], ["a"])
        self.assertEqual(len(jobs), 5)
        with mock.patch.object(ps, "SCOPE_SECONDS", 0):
            time.sleep(0.01)
            ps.reader_scope(one, who("b"))
        self.assertEqual(len(jobs), 6)
        for _ in range(2):   # unreadable: refused each time, never kept
            with self.assertRaises(ps.PageDoorUnavailable):
                ps.reader_scope(one, who("broken"))
        self.assertEqual(jobs[-2:], ["broken", "broken"])

    def test_a_watch_rule_added_is_read_on_the_next_screen_not_after_the_burst(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "watch.sqlite"
            rules.write_text("one rule")
            jobs = []

            class Worker:
                def run(self, reader, job, timeout, priority=0):
                    jobs.append(1)
                    return ps.ReaderScope(None, None, [str(len(jobs))],
                                          (rules, ps._file_stamp(rules)))
            backend = SimpleNamespace(access_worker=Worker())
            reader = SimpleNamespace(key="r", mode="dev",
                                     session=SimpleNamespace(principals=("r",)))
            self.assertEqual(ps.reader_scope(backend, reader).enabled, ("1",))
            self.assertEqual(ps.reader_scope(backend, reader).enabled, ("1",))
            Path(str(rules) + "-wal").write_text("a rule added")   # a SQLite write
            self.assertEqual(ps.reader_scope(backend, reader).enabled, ("2",))
            rules.write_text("two rules, checkpointed")
            self.assertEqual(ps.reader_scope(backend, reader).enabled, ("3",))
            self.assertEqual(ps.reader_scope(backend, reader).enabled, ("3",))


class ColdReaderBudget(unittest.TestCase):
    """A reader's first door read on a worker also decides their workspace and policy:
    it waits up to PAGE_COLD_TIMEOUT (11-18 walk: the late list's first read ran past 5 s
    and said "could not be read"); once the worker holds their workspace, PAGE_TIMEOUT."""

    def test_only_a_reader_served_on_every_access_worker_gets_the_short_budget(self):
        # a workspace made but its first job still deciding (in ``cache``, not ``served``)
        # is cold too: 11-18 walk, the sign-in's policy job held it and the late list's
        # first read ran out at 5 s behind it; with several access workers on one queue
        # the read goes to whichever is free, so it is warm only once warm on each
        from operator_api.serving import Worker
        first = Worker(lambda session: None)
        second = Worker(lambda session: None, first.jobs)
        first.cache = {"p:a,p:b": object(), "p:c": object()}
        first.served = {"p:a,p:b"}
        warm = SimpleNamespace(session=SimpleNamespace(principals=("p:b", "p:a")))
        cold = SimpleNamespace(session=SimpleNamespace(principals=("p:c",)))
        self.assertEqual(ps._timeout(first, warm), ps.PAGE_COLD_TIMEOUT)   # not on second
        second.served = {"p:a,p:b", "p:c"}
        self.assertEqual(ps._timeout(first, warm), ps.PAGE_TIMEOUT)
        self.assertEqual(ps._timeout(second, warm), ps.PAGE_TIMEOUT)
        self.assertEqual(ps._timeout(second, cold), ps.PAGE_COLD_TIMEOUT)
        self.assertGreater(ps.PAGE_COLD_TIMEOUT, ps.PAGE_TIMEOUT)
        self.assertEqual(ps._timeout(SimpleNamespace(), cold), ps.PAGE_TIMEOUT)   # unknown
        self.assertEqual(ps._timeout(first, None), ps.PAGE_TIMEOUT)
        lone = Worker(lambda session: None)   # a queue of its own: its own served only
        lone.served = {"p:c"}
        self.assertEqual(ps._timeout(lone, cold), ps.PAGE_TIMEOUT)

    def test_the_watchlist_on_the_access_worker_gets_the_same_cold_budget(self):
        """11-18 staff walk: Home's Watching read (/api/watch) 503'd at 5 s while the
        reader's first job on the access worker was still deciding their policy."""
        from operator_api import watch
        asked = []

        class Worker:
            stopped = threading.Event()

            def warm_for(self, reader):
                return tuple(reader.session.principals) == ("p:a",)

            def run(self, reader, job, timeout, priority=0):
                asked.append(timeout)
                return "ok"

        backend = SimpleNamespace(access_worker=Worker())
        for principals in (("p:a",), ("p:c",)):
            reader = SimpleNamespace(session=SimpleNamespace(principals=principals))
            self.assertEqual(watch.WatchMixin._watch(backend, reader, lambda c: c, fast=True),
                             "ok")
        self.assertEqual(asked, [watch.WATCH_SECONDS, ps.PAGE_COLD_TIMEOUT])


from operating.pg.alerts import RULES


class AlertRunsAsScreenInput(unittest.TestCase):
    """A screen kept from the alert rules stops being current when the rules stop running."""

    def pool(self, rows):
        class Conn:
            def execute(self_, sql):
                self.sql = sql
                return SimpleNamespace(fetchall=lambda: rows())

            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False
        return SimpleNamespace(connection=lambda: Conn())

    def test_the_value_moves_when_a_rule_goes_stale_and_is_held_between_reads(self):
        now = dt.datetime.now(dt.timezone.utc)
        ok = [now]
        backend = SimpleNamespace(pg_dsn="x")
        backend.pg = lambda: self.pool(lambda: [(r, ok[0]) for r in RULES])
        with mock.patch.object(ps, "_paged", lambda name: True):
            before = ps.runs_fresh(backend)
            self.assertIn(("po_late", True), before)
            ok[0] = now - dt.timedelta(hours=3)          # the rules stopped running
            self.assertEqual(ps.runs_fresh(backend), before)   # held for RUNS_SECONDS
            backend.__dict__.pop("_runs_fresh")
            after = ps.runs_fresh(backend)
            self.assertNotEqual(after, before)
            self.assertIn(("po_late", False), after)

    def test_an_unreadable_table_is_one_constant_never_a_failure(self):
        backend = SimpleNamespace(pg_dsn="x", pg=lambda: 1 / 0)
        self.assertEqual(ps.runs_fresh(backend), "unread")
        self.assertEqual(ps.runs_fresh(SimpleNamespace(pg_dsn=None)), "unread")
