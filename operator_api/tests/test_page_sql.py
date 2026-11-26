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
import os
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


from operating.measures import where_sql  # noqa: E402


class PageDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_store_mode
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        # the builders' writes reserve through the one disk budget: a state file of the test's
        # own, never the host's live reservations
        import tempfile
        budget = mock.patch.dict(os.environ, {"PLX_STATE_DIR": tempfile.mkdtemp()})
        budget.start()
        cls.addClassCleanup(budget.stop)
        cls.store = build()
        cls.dsn = pg_store_mode._load("", cls.store.conn)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            for name in ("0038_own_issued_documents.sql", "0040_people_excluded_objects.sql",
                         "0041_business_views.sql"):
                c.execute((MIGRATIONS_DIR / name).read_text())
        bv.build(cls.dsn, None, schema=tbv.SCHEMA, as_of=tbv.AS_OF)
        from operating.pg import generic_records
        # every record's own row (an alert's subject is its name there), as served
        generic_records.build(cls.dsn, bv.SCHEMA, workers=1, uncovered=True, business=True)
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
        out = self.page({"reads": [{"name": "late_po_list", "params": {"closed": "", "alive": "", 
            "limit": limit, "offset": offset}}]}, actor)
        self.assertNotIn("error", out, out)
        got = out["reads"]["late_po_list"]
        return [r[got["columns"].index("po_number")] for r in got["rows"]]

    # ---- truth: every number equals the SQL truth for each reader ----------------------

    def test_counts_equal_the_sql_truth_for_full_restricted_and_no_reader(self):
        late_all = self.truth("SELECT count(DISTINCT handle) FROM {biz}.purchase_orders "
                              "WHERE " + where_sql("late_purchase_orders"))[0][0]
        orders_all = self.truth("SELECT count(DISTINCT handle) FROM {biz}.sales_orders "
                                "WHERE " + where_sql("late_orders"))[0][0]
        marked = len(range(0, MORE, FINANCE_EVERY)) + 1          # + the fixture's PO-FIN
        self.assertGreaterEqual(late_all, MORE + 2)
        self.assertGreaterEqual(orders_all, 1)
        reads = {"reads": [{"name": "late_po_count", "params": {"closed": "", "alive": ""}}, {"name": "late_order_count"}]}
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
                for name, sql in (("late_po_count", "SELECT count(DISTINCT handle) AS n FROM purchase_orders "
                                   "WHERE " + where_sql("late_purchase_orders")),
                                  ("late_order_count", "SELECT count(DISTINCT handle) AS n "
                                   "FROM sales_orders WHERE " + where_sql("late_orders"))):
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
        out = self.page({"reads": [{"name": "late_po_list", "params": {"closed": "", "alive": "", 
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
        out = self.page({"reads": [{"name": "late_po_list", "params": {"closed": "", "alive": "", 
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
            {"name": "alerts_open", "params": {"rules": rules, "closed": "", "limit": pr.MAX_ROWS,
                                               "offset": 0}},
            {"name": "alerts_count", "params": {"rules": rules, "closed": ""}}]}, actor)
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
                listed = self.page({"reads": [{"name": "late_po_list", "params": {"closed": "", "alive": "", 
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

    def test_an_excluded_persons_record_never_titles_an_alert(self):
        """The people exclusion reaches the alert's title (``subject``, read from the
        ``records`` view) in the same read: with the record's own object an excluded
        person's, no reader, the owner included, gets its name anywhere in the alerts read,
        and the name is back once the exclusion is lifted."""
        import psycopg
        from operating.pg import alerts as al
        self.assertEqual(al.run(self.dsn, ["po_late"], schema=bv.SCHEMA, owners={})[0]["status"],
                         "ok")
        some = sorted(self.alerts(OPS)[0])[0]
        own = self.truth("SELECT object_ids FROM {biz}.lineage WHERE view = "
                         "'purchase_orders' AND handle = %s", (some,))[0][0]
        self.assertTrue(own)
        name = "Excluded Title PO 4242"

        def said(actor):
            return json.dumps(self.alerts(actor)[1]["reads"]["alerts_open"], default=str)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            before = c.execute(f"SELECT record_id, label FROM {bv.SCHEMA}.records "
                               "WHERE handle = %s", (some,)).fetchall()
            self.assertTrue(before)
            c.execute(f"UPDATE {bv.SCHEMA}.records SET label = %s WHERE handle = %s",
                      (name, some))
            try:
                for actor in (FULL, OPS):
                    self.assertIn(json.dumps(name), said(actor), actor)
                c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                          "reason) VALUES ('t-excl-title', %s, 'participant')", (own[0],))
                try:
                    for actor in (FULL, OPS):
                        self.assertNotIn(name, said(actor), actor)
                        self.assertNotIn(some, said(actor), actor)
                finally:
                    c.execute("DELETE FROM ontology.people_excluded_objects "
                              "WHERE l1_handle = 't-excl-title'")
                self.assertIn(json.dumps(name), said(FULL))
            finally:
                for rid, label in before:
                    c.execute(f"UPDATE {bv.SCHEMA}.records SET label = %s WHERE record_id = %s",
                              (label, rid))

    def test_an_alert_is_titled_by_its_records_own_name_as_the_reader_may_see_it(self):
        """``subject``: the alerted record's ``records`` label (#26: rows said "Portal
        customer"), read through the reader's own records view: a record row the reader may
        not read leaves it empty (the row is then titled by its kind), never its name."""
        import psycopg
        from operating.pg import alerts as al
        self.assertEqual(al.run(self.dsn, ["po_late"], schema=bv.SCHEMA, owners={})[0]["status"],
                         "ok")
        names = dict(self.truth("SELECT handle, label FROM {biz}.records WHERE type = "
                                "'PurchaseOrder' ORDER BY record_id DESC"))

        def subjects(actor):
            got = self.alerts(actor)[1]["reads"]["alerts_open"]
            cols = got["columns"]
            return {r[cols.index("handle")]: r[cols.index("subject")] for r in got["rows"]}
        full = subjects(FULL)
        self.assertTrue(full)
        for handle, subject in full.items():
            self.assertEqual(subject, names[handle], handle)
        ops = subjects(OPS)
        self.assertTrue(ops)
        some = sorted(ops)[0]
        hidden_cls = self.truth("SELECT access_class FROM {biz}.purchase_orders WHERE "
                                "handle = ANY(%s)", (sorted(set(full) - set(ops)),))[0][0]
        with psycopg.connect(self.dsn, autocommit=True) as c:
            before = c.execute(f"SELECT access_class FROM {bv.SCHEMA}.records WHERE handle = %s",
                               (some,)).fetchall()
            c.execute(f"UPDATE {bv.SCHEMA}.records SET access_class = %s WHERE handle = %s",
                      (hidden_cls, some))
            try:
                ops = subjects(OPS)
                self.assertIn(some, ops)             # the alert itself is still the reader's
                self.assertIsNone(ops[some])         # its record's name is not
                item = ps.alert_item({"rule": "po_late", "kind": "late", "handle": some,
                                      "subject": ops[some], "why": "", "record_table":
                                      "purchase_orders"},
                                     {some: {"object_id": "PurchaseOrder:x",
                                             "object_type": "PurchaseOrder"}}, 1, None)
                self.assertEqual(item.subject_label, "Purchase order")
                self.assertEqual(subjects(FULL)[some], names[some])
            finally:
                for (cls_,) in before[:1]:
                    c.execute(f"UPDATE {bv.SCHEMA}.records SET access_class = %s "
                              "WHERE handle = %s", (cls_, some))

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
        out = self.page({"reads": [{"name": "late_po_count", "params": {"closed": "", "alive": ""}}]}, base=base)
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
            self.assertNotIn("error", self.page({"reads": [{"name": "late_po_count", "params": {"closed": "", "alive": ""}}]},
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
                out = self.page({"reads": [{"name": "late_po_count", "params": {"closed": "", "alive": ""}}]}, FULL, base)
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
                                     [ps._numbered("PO", n)
                                      for n in self.late_list(actor, LATE_LIST_LIMIT)])
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

    def test_a_number_several_rows_hold_lists_one_choice_per_row_for_its_reader(self):
        """A PO number several records hold (two suppliers' orders; 11-22: 308 numbers live)
        lists one choice per door row: copies of one order are one choice, each labelled by
        its number and opening its row's page; a row the reader may not read is left out
        (its handle never shown), and none readable is not found."""
        from operator_api import errors
        q = ("SELECT p.handle, l.object_ids FROM {biz}.purchase_orders p JOIN {biz}.lineage "
             "l ON l.view = 'purchase_orders' AND l.handle = p.handle WHERE p.po_number = %s")
        (mh, mids), (h, ids) = self.truth(q, ("PO-L00",))[0], self.truth(q, ("PO-L01",))[0]
        every = [*mids, *ids, *ids]   # each copy, one of them twice
        with mock.patch.dict("os.environ", {"PLX_PAGES_DOOR": "1"}):
            screens, reader = self.screens(FULL)
            got = ps.choices(screens, reader, every)
            self.assertEqual(sorted(c.open_url for c in got),
                             sorted([ps.account_url("purchase_order", mh),
                                     ps.account_url("purchase_order", h)]))
            self.assertEqual(sorted(c.label.split(" ")[0] for c in got), ["PO-L00", "PO-L01"])
            ops_screens, ops_reader = self.screens(OPS)
            mine = ps.choices(ops_screens, ops_reader, every)
            self.assertEqual([c.open_url for c in mine], [ps.account_url("purchase_order", h)])
            self.assertNotIn(mh, repr(mine))
            page = ps.choices_page(ops_screens, ops_reader, "purchase_order", "PO-L0", every)
            self.assertEqual((page.status.state, len(page.choices)), ("choose", 1))
            with self.assertRaises(errors.RecordNotFound):
                ps.choices_page(ops_screens, ops_reader, "purchase_order", "PO-L00", mids)

    def doors_screens(self, actor, held):
        """``screens(actor)`` with the reader's door held ahead (``backend.doors``) and a worker
        that fails the test if it is asked anything (speed specs 9.3 V1)."""
        screens, reader = self.screens(actor)
        test = self

        class NoWorker:
            def run(self, *a, **k):
                test.fail("a page read with a door ahead asked the worker")
        screens.access_worker = NoWorker()
        screens.doors = SimpleNamespace(get=lambda r: held, decide_now=lambda r: held)
        return screens, reader

    def test_a_door_ahead_reads_every_page_as_the_worker_did_for_each_reader(self):
        """V1: ``page_sql`` reads with the reader's door held ahead, on the request's thread:
        every purchase order, sales order, customer and supplier page and every record id's
        resolve equal the worker's for FULL, OPS and NOBODY (no more, no less), and OPS's
        pages through its own door differ from FULL's where the door hides rows."""
        kinds = (("purchase_order", "purchase_orders"), ("sales_order", "sales_orders"),
                 ("customer", "customers"), ("supplier", "suppliers"))
        seen = {}
        for name, actor in (("FULL", FULL), ("OPS", OPS), ("NOBODY", NOBODY)):
            old, reader = self.screens(actor)
            new, _ = self.doors_screens(actor, (door.reader_door(self.base(), actor), self.dsn,
                                                None))
            for kind, view in kinds:
                rows = self.truth(f"SELECT p.handle, l.object_ids[1] FROM {{biz}}.{view} p "
                                  f"JOIN {{biz}}.lineage l ON l.view = '{view}' AND "
                                  "l.handle = p.handle ORDER BY p.handle")
                self.assertGreaterEqual(len(rows), 1, view)
                for handle, oid in rows:
                    got = []
                    for screens in (old, new):
                        try:
                            at = ps.resolve(screens, reader, kind, oid)
                        except ps.NotFound:
                            at = None
                        try:
                            page = (ps.order(screens, reader, kind, handle)
                                    if kind in ("purchase_order", "sales_order")
                                    else ps.account(screens, reader, kind, handle=handle))
                            page = page.model_dump(mode="json")
                            page.pop("took_ms")   # the read's own time, not the page
                        except ps.NotFound:
                            page = "not_found"
                        got.append((at, page))
                    self.assertEqual(got[0], got[1], (name, kind, handle))
                    seen[(name, kind, handle)] = got[1][1]
        for kind, view in kinds:
            full = {k[2]: v for k, v in seen.items() if k[:2] == ("FULL", kind)}
            nobody = {k[2]: v for k, v in seen.items() if k[:2] == ("NOBODY", kind)}
            self.assertTrue(any(v != "not_found" for v in full.values()), kind)
            self.assertNotEqual(full, nobody, kind)
        ops = {k: v for k, v in seen.items() if k[0] == "OPS"}
        self.assertNotEqual(ops, {("OPS", *k[1:]): v for k, v in seen.items() if k[0] == "FULL"})

    def test_a_reader_whose_door_cannot_be_decided_is_refused_never_read(self):
        screens, reader = self.doors_screens(FULL, None)
        h = self.truth("SELECT handle FROM {biz}.purchase_orders ORDER BY handle LIMIT 1")[0][0]
        for call in (lambda: ps.order(screens, reader, "purchase_order", h),
                     lambda: ps.resolve(screens, reader, "purchase_order", "x"),
                     lambda: ps.late_pos(screens, reader)):
            with self.assertRaises(ps.PageDoorUnavailable) as got:
                call()
            self.assertEqual(got.exception.code, "not_ready")

    def test_a_door_ahead_without_its_store_is_refused(self):
        out = pr.PageReads(None).call({"reads": [{"name": "late_po_list", "params": {"closed": "", "alive": "", 
            "limit": 1, "offset": 0}}]}, None, door=door.reader_door(self.base(), FULL))
        self.assertEqual(out["error"]["code"], "invalid_request")
        out = pr.PageReads(None).call({"decide_door": True}, None, door=door.reader_door(
            self.base(), FULL), dsn=self.dsn)
        self.assertEqual(out["error"]["code"], "invalid_request")

    def test_an_undated_open_po_says_so_and_a_zero_total_is_never_a_dollar_amount(self):
        row = self.truth("SELECT handle, status, requested_date, promised_date, total "
                         "FROM {biz}.purchase_orders WHERE po_number = %s", ("PO-L01",))[0]
        handle, status, requested, promised, total = row
        screens, reader = self.screens(FULL)
        page = ps.order(screens, reader, "purchase_order", handle)
        labels = {f.label: f.value.text for f in page.facts}
        undated = status == "open" and requested is None and promised is None
        self.assertEqual("No date on record" in (labels.get("Needed by") or ""), undated)
        if total is not None and total == 0:
            self.assertIn("Not on record", labels.get("Total", ""))

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
        # the page names the systems the order comes from, never "the business records"
        held = self.truth("SELECT source_systems FROM {biz}.purchase_orders WHERE handle = %s",
                          (h,))[0][0]
        if held:
            self.assertNotIn("the business records", page.sources)
            for system in str(held).split(","):
                self.assertIn(ps._SYSTEMS.get(system.strip().lower(), system.strip().title()),
                              page.sources)
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

    def test_an_old_held_value_is_read_again_off_the_request_and_never_served_past_its_age(self):
        now = dt.datetime.now(dt.timezone.utc)
        ok = [now]
        reads = []
        backend = SimpleNamespace(pg_dsn="x")
        backend.pg = lambda: self.pool(lambda: reads.append(1) or [(r, ok[0]) for r in RULES])
        with mock.patch.object(ps, "_paged", lambda name: True):
            before = ps.runs_fresh(backend)
            ok[0] = now - dt.timedelta(hours=3)          # the rules stopped running
            at, held = backend.__dict__["_runs_fresh"]
            # past RUNS_AHEAD_SECONDS, under RUNS_SECONDS: the held value at once, the read
            # started off the request (exactly one, however many ask)
            backend.__dict__["_runs_fresh"] = (at - ps.RUNS_AHEAD_SECONDS - 1, held)
            self.assertEqual(ps.runs_fresh(backend), before)
            ps.runs_fresh(backend)
            deadline = time.monotonic() + 5
            while backend.__dict__.get("_runs_ahead") and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(reads), 2)              # the first read, the one ahead
            self.assertNotEqual(ps.runs_fresh(backend), before)
            # at RUNS_SECONDS the request reads for itself: never a value older than that
            ok[0] = now
            at, held = backend.__dict__["_runs_fresh"]
            backend.__dict__["_runs_fresh"] = (at - ps.RUNS_SECONDS - 1, held)
            self.assertIn(("po_late", True), ps.runs_fresh(backend))

    def test_an_unreadable_table_is_one_constant_never_a_failure(self):
        backend = SimpleNamespace(pg_dsn="x", pg=lambda: 1 / 0)
        self.assertEqual(ps.runs_fresh(backend), "unread")
        self.assertEqual(ps.runs_fresh(SimpleNamespace(pg_dsn=None)), "unread")


class AlertNextStep(unittest.TestCase):
    """An alert carries its kind's own next step (the one the briefing's items say); a kind
    with none keeps the plain step, never another kind's."""

    def item(self, rule: str):
        row = {"rule": rule, "kind": "x", "handle": "api://public.app_batch/1", "topic": "",
               "why": "Batch B1 is on quality hold.", "first_seen": None,
               "record_table": "batches"}
        return ps.alert_item(row, {}, 1, None)

    def test_each_kind_says_its_own_step(self):
        from typing import get_args
        from operating.briefing import NEXT_ACTIONS
        from operator_api import models as m
        listed = set(get_args(m.BriefingItem.model_fields["event_type"].annotation))
        for rule in [r for r in RULES if r in listed]:
            got = self.item(rule).next_action
            self.assertEqual((got.text, got.rule),
                             NEXT_ACTIONS.get(rule, ("Open the record and check it.", rule)), rule)
        hold = self.item("lot_on_hold").next_action
        self.assertEqual(hold.rule, "hold_ask_quality")
        self.assertTrue(hold.text.startswith("Ask Quality"))
        without = {k: v for k, v in NEXT_ACTIONS.items() if k != "lot_on_hold"}
        with mock.patch.dict(NEXT_ACTIONS, without, clear=True):
            self.assertEqual(self.item("lot_on_hold").next_action.text,
                             "Open the record and check it.")


class HomeRefreshingTypes(unittest.TestCase):
    """Live 11-21 walk: Home said "Refreshing" for good and the app asked for it again
    every few seconds: needs_my_reply (a mail rule, which this page path never pages, so
    its run is never "current" here) counted as a stale Home type. As in the briefing, only
    a paged type whose rule is not current holds Home in refreshing."""

    def test_a_mail_type_never_holds_home_in_refreshing(self):
        now = dt.datetime.now(dt.timezone.utc)
        ok = [{"rule": r, "ok_at": now} for r in RULES]   # every rule just finished
        rows = {"alerts_late_po_count": [{"late": 0}], "alerts_late_po_list": [],
                "alerts_late_po_stale_count": [{"late": 0}], "late_po_candidates": [],
                "alert_runs": ok, "alerts_count": [], "alerts_open": []}
        scope = SimpleNamespace(enabled=(), routing=None)
        types = ["needs_my_reply", "lot_on_hold", "po_late"]
        self.assertTrue(RULES["needs_my_reply"].comms)
        with mock.patch.object(ps, "reader_scope", lambda *a: scope), \
                mock.patch.object(ps, "alert_types", lambda *a, **k: types), \
                mock.patch.object(ps, "routed", lambda *a: True), \
                mock.patch.object(ps, "read", lambda *a, **k: (rows, {})):
            stale = ps.alert_home(None, None, limit=5)[4]
            self.assertEqual(stale, {})
            # a paged type not current still does
            rows["alert_runs"] = [r for r in ok if r["rule"] != "lot_on_hold"]
            self.assertEqual(ps.alert_home(None, None, limit=5)[4], {"lot_on_hold": None})


class HomeAtRiskTotal(unittest.TestCase):
    """Home lists the first ``limit`` open alerts of its types and says how many there are
    in all (live 11-22: 12 listed, no total, a limit of 25)."""

    def test_the_total_is_every_open_alert_the_reader_may_read_not_the_listed_ones(self):
        now = dt.datetime.now(dt.timezone.utc)
        ok = [{"rule": r, "ok_at": now} for r in RULES]
        listed = [{"rule": "price_below_cost", "kind": "margin_loss", "record_table": "x",
                   "handle": f"h{i}", "topic": "", "why": "w", "since": now, "first_seen": now,
                   "subject": "S"} for i in range(5)]
        rows = {"alerts_late_po_count": [{"late": 0}], "alerts_late_po_list": [],
                "alerts_late_po_stale_count": [{"late": 0}], "late_po_candidates": [],
                "alert_runs": ok, "alerts_open": listed,
                "alerts_count": [{"rule": "price_below_cost", "open": 67, "oldest": now},
                                 {"rule": "lot_on_hold", "open": 4, "oldest": now}]}
        scope = SimpleNamespace(enabled=(), routing=None)
        with mock.patch.object(ps, "reader_scope", lambda *a: scope), \
                mock.patch.object(ps, "alert_types",
                                  lambda *a, **k: ["price_below_cost", "lot_on_hold", "po_late"]), \
                mock.patch.object(ps, "routed", lambda *a: True), \
                mock.patch.object(ps, "read", lambda *a, **k: (rows, {})):
            got = ps.alert_home(None, None, limit=5)
        self.assertEqual((len(got[3]), got[5]), (5, 71))
        rows["alerts_count"] = []                 # nothing counted: never fewer than listed
        with mock.patch.object(ps, "reader_scope", lambda *a: scope), \
                mock.patch.object(ps, "alert_types", lambda *a, **k: ["price_below_cost", "po_late"]), \
                mock.patch.object(ps, "routed", lambda *a: True), \
                mock.patch.object(ps, "read", lambda *a, **k: (rows, {})):
            self.assertEqual(ps.alert_home(None, None, limit=5)[5], 5)


class RoutingLatenessAndCosAlerts(unittest.TestCase):
    def test_qc_hold_routing_blocks_super_admin_non_qa_and_allows_owner_and_quality(self):
        from operating import briefing as bf
        self.assertFalse(ps.routed("lot_on_hold", bf.Routing(frozenset({"PROD"}), role="SUPER_ADMIN")))
        self.assertFalse(ps.routed("quality_hold_aging", bf.Routing(frozenset({"IT"}), role="SUPER_ADMIN")))
        self.assertTrue(ps.routed("lot_on_hold", bf.Routing(frozenset({"IT"}), role="SUPER_ADMIN", is_owner=True)))
        self.assertTrue(ps.routed("lot_on_hold", bf.Routing(frozenset({"EXEC"}))))
        self.assertTrue(ps.routed("lot_on_hold", bf.Routing(frozenset({"QUALITY"}))))
        self.assertTrue(ps.routed("lot_on_hold", bf.Routing(basis="not_an_employee", everything=True)))

    def test_cos_system_monitoring_reply_alerts_are_dropped_while_business_stays(self):
        now = dt.datetime(2026, 11, 26, 6, 0, tzinfo=dt.timezone.utc)
        for why in (
            'cos@petrasoap.com wrote "Email relay failed on staging (email-relay)" on Nov 25.',
            'cos@petrasoap.com wrote "Storage finalize failed on staging (storage-finalize)" on Nov 25.',
            'cos@petrasoap.com wrote "Work-routing GHA dispatch failed (relay)" on Nov 25.',
            'cos@petrasoap.com wrote "Work-routing GHA dispatch recovered (relay)" on Nov 25.',
        ):
            row = {"rule": "needs_my_reply", "kind": "reply_owed", "since": now, "why": why}
            self.assertTrue(ps._drop_reply_alert(row, now=now), why)
        real_cos = {
            "rule": "needs_my_reply",
            "kind": "reply_owed",
            "since": now,
            "why": 'cos@petrasoap.com wrote "[ACTION REQUIRED] New Sourcing Request: Bluzen" on Nov 25.',
        }
        self.assertFalse(ps._drop_reply_alert(real_cos, now=now))

    def test_copy_of_record_so_and_bc_only_po_suppress_days_late_consistently(self):
        self.assertIsNone(ps._sales_order_live_days_late({"status": "open", "open_state": "copy_of_record", "days_late": 42}))
        self.assertIsNone(ps._purchase_order_live_days_late({"status": "open", "verdict": "late", "source_systems": "bc", "days_late": 133}))
        self.assertIsNone(ps._purchase_order_live_days_late({"status": "open", "verdict": "late", "handle": "bc://po/100109", "days_late": 133}, {"bc://po/100109"}))
        self.assertEqual(ps._purchase_order_live_days_late({"status": "open", "verdict": "late", "source_systems": "bc,portal", "days_late": 14}), 14)

    def test_alert_item_uses_earlier_since_for_first_seen_while_preserving_new_flag(self):
        t_since = dt.datetime(2026, 10, 21, 0, 0, tzinfo=dt.timezone.utc)
        t_seen = dt.datetime(2026, 11, 18, 21, 0, tzinfo=dt.timezone.utc)
        item = ps.alert_item(
            {"rule": "lot_on_hold", "kind": "quality_hold", "handle": "api://batch/1", "topic": "",
             "why": "Batch B1 is on quality hold.", "since": t_since, "first_seen": t_seen,
             "record_table": "batches"},
            {}, 1, "2026-11-17T00:00:00+00:00",
        )
        self.assertTrue(item.new_since_visit)
        self.assertEqual(item.first_seen, t_since.isoformat())
