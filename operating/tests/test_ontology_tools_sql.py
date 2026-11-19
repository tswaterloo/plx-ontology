"""plx.sql (``operating.ontology_tools_sql``) over the guarded views (``operating.pg.sql_views``).

The business-views fixture (``test_business_views``: customers in two systems, finance-marked
documents and cells, source-deleted rows, customers with 1, 10 and 60 documents) loaded into
a throwaway Postgres, built into schema biz and read through the door as the NOLOGIN role
plx_sql_reader. Checked at the boundary: the rows each reader gets (full, staff without
finance, nobody) are exactly the rows plx.business gives it; a withheld cell never leaves;
the scope guard fails closed (unset settings, a JSON null, NULL keys, hidden ids, the people
exclusion, Freightcom); every write, second statement, catalog read and function outside the
allowlist is refused before it runs, and a statement that changed its own scope returns
nothing; a reader holding ~10,000 audience keys gets the same rows fast; parallel calls each
run on a connection of their own."""
from __future__ import annotations

import json
import threading
import time
import unittest
from decimal import Decimal
from unittest import mock

from operating import ontology_tools_business as tb
from operating import ontology_tools_sql as sqlt
from operating.pg import business_views as bv
from operating.pg import sql_views
from operating.tests import test_business_views as tbv
from operating.tests import test_ontology_tools_table as tab

FULL, OPS, NOBODY = tab.FULL, tab.OPS, tab.NOBODY


def _to_v1(c, s, name, into=None):
    """The canonical table ``<s>.<name>`` as an old-shape (v1) table ``<into or s>.<name>``
    (its classes and lineage joined back as columns; a blocked class: NULL keys), the
    input ``door.convert`` exists for."""
    v = bv.VIEWS[name]
    into = into or s
    biz = ", ".join(f't."{x.name}"' for x in v.cols)
    sql_views.drop(c, name, into)
    c.execute(f"CREATE TABLE {into}.{name}__v1 AS SELECT {biz}, t.handle, l.systems, l.copies, "
              "CASE WHEN a.blocked THEN NULL ELSE a.audience_keys END AS audience_keys, "
              "a.rec_marks, cc.marks AS col_marks, l.object_ids, l.cites "
              f"FROM {s}.{name} t JOIN {s}.access_classes a ON a.id = t.access_class "
              f"JOIN {s}.col_classes cc ON cc.id = t.col_class JOIN {s}.lineage l "
              "ON l.view = %s AND l.handle = t.handle", (name,))
    c.execute(f"DROP TABLE IF EXISTS {into}.{name}")
    c.execute(f"ALTER TABLE {into}.{name}__v1 RENAME TO {name}")


def _cell(value):
    return value["untrusted_text"] if isinstance(value, dict) else value


class SqlDoor(unittest.TestCase):
    PARITY = (("customer_balances", "document_number"), ("customers", "name"),
              ("sales_invoices", "invoice_number"), ("sales_orders", "order_number"))

    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        from operating.tests import pg_store_mode
        cls.store = tbv.build()
        cls.dsn = pg_store_mode._load("", cls.store.conn)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            for name in ("0040_people_excluded_objects.sql", "0041_business_views.sql"):
                c.execute((MIGRATIONS_DIR / name).read_text())
        bv.build(cls.dsn, None, schema=tbv.SCHEMA, as_of=tbv.AS_OF)
        for attempt in range(5):   # the role is the cluster's: parallel modules may race
            try:
                cls.views = sql_views.install(cls.dsn)
                break
            except psycopg.errors.InternalError:
                time.sleep(0.5 * (attempt + 1))
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute(f"GRANT {sql_views.ROLE} TO CURRENT_USER")

    @classmethod
    def tearDownClass(cls):
        for pool in list(sqlt._POOLS.values()):
            pool.close()
        sqlt._POOLS.clear()

    def setUp(self):
        sqlt._DOOR.clear()
        sqlt._GRANTS.clear()
        # a refusal is answered at once here; the wait itself is its own test
        patcher = mock.patch.object(sqlt, "BUSY_WAIT_SECONDS", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def names(self, actor=FULL):
        return set(SqlDoor.column(self, self.sql("SELECT name FROM customers", actor)))

    def base(self):
        return tbv.BusinessViews.base(self)

    def sql(self, statement, actor=FULL, base=None):
        return sqlt.SqlTool(base or self.base()).call("plx.sql", {"sql": statement}, actor)

    def column(self, out, i=0):
        self.assertNotIn("error", out, out)
        return [_cell(r["cells"][i]) for r in out["rows"]]

    def business(self, view, field, actor):
        out = tb.BusinessTool(self.base()).call(
            "plx.business", {"view": view, "select": [field], "limit": 200}, actor)
        return sorted(_cell(r["values"][field]["value"]) for r in out["rows"])

    def raw(self, statement, settings, prepare=()):
        """``statement`` as the role with ``settings`` (None: left unset) in one rolled-back
        transaction; ``prepare`` runs first as the owner (changes to the business tables)."""
        import psycopg
        with psycopg.connect(self.dsn) as c:
            try:
                for step in prepare:
                    c.execute(step)
                for name, value in settings.items():
                    if value is not None:
                        c.execute("SELECT set_config(%s, %s, true)", (name, value))
                c.execute(f"SET LOCAL search_path = {sql_views.guarded_schema()}, pg_catalog")
                c.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
                return c.execute(statement).fetchall()
            finally:
                c.rollback()

    def scope(self, actor=FULL, **override):
        """The door settings plx.sql sets for ``actor`` (decided as a call decides them)."""
        import psycopg
        door = sqlt.reader_door(self.base(), actor)
        with psycopg.connect(self.dsn) as c:
            settings = sqlt._decide(c, door.grant)["settings"]
        return {**settings, **override}

    # ------------------------------------------------------------------ parity

    def test_each_reader_gets_exactly_the_rows_plx_business_gives_it(self):
        for actor in (FULL, OPS, NOBODY):
            for view, field in self.PARITY:
                # plx.business leaves pilot / not-of-record rows out unless asked: the
                # same filters, stated
                cols = {c.name for c in bv.VIEWS[view].cols}
                where = " AND ".join(f"{name} = {str(kept).lower()}" for name, kept, *_ in
                                     tb.DEFAULT_FILTERS if name in cols)
                ours = sorted(self.column(self.sql(
                    f"SELECT {field} FROM {view}" + (f" WHERE {where}" if where else ""),
                    actor)))
                self.assertEqual(ours, self.business(view, field, actor), (actor, view))
        self.assertEqual(self.column(self.sql("SELECT count(*) FROM customers", NOBODY)), ["0"])
        self.assertNotIn("SECRET-FIN", json.dumps(self.sql(
            "SELECT document_number FROM customer_balances", OPS)))

    def test_a_withheld_cell_never_leaves_and_the_answer_says_partial(self):
        out = self.sql("SELECT document_number, amount_due, amount_due_currency FROM "
                       "customer_balances WHERE document_number = 'INV-3'", OPS)
        self.assertEqual(out["rows"][0]["cells"][1], None)
        self.assertTrue(out["partial"])
        self.assertEqual(out["withheld"]["customer_balances"]["amount_due"], 1)
        total = self.sql("SELECT amount_due_currency, sum(amount_due) FROM customer_balances "
                         "GROUP BY 1 ORDER BY 1", OPS)
        self.assertEqual([(_cell(r["cells"][0]), Decimal(r["cells"][1])) for r in total["rows"]
                          if _cell(r["cells"][0]) == "CAD"], [("CAD", Decimal("120.10"))])
        for secret in tbv.SECRET + ("50.00",):
            self.assertNotIn(secret, json.dumps(total) + json.dumps(out))
        full = self.sql("SELECT amount_due FROM customer_balances WHERE document_number = "
                        "'INV-3'", FULL)
        self.assertEqual((Decimal(full["rows"][0]["cells"][0]), full["partial"]),
                         (Decimal("50.00"), False))

    def test_cardinality_1_10_60(self):
        out = self.sql("SELECT customer, count(*) FROM customer_balances WHERE customer LIKE "
                       "'Bulk %' GROUP BY 1 ORDER BY 1")
        self.assertEqual([(_cell(r["cells"][0]), r["cells"][1]) for r in out["rows"]],
                         [("Bulk 1", "1"), ("Bulk 10", "10"), ("Bulk 60", "60")])

    # ------------------------------------------------------------------ the guard

    # ------------------------------------------------------------------ statements

    # the per-call budget of a warm plx.sql call (ARCH section 14: a later call costs its
    # SQL + under 20 ms): statements sent on its one connection (the door check, the
    # settings, EXPLAIN, the column read, the cursor, view_state, the scope check, the
    # role reset, the pool's hand-back), bytes sent, wall time
    CALL_STATEMENTS = 12
    CALL_BYTES = 16_000
    CALL_SECONDS = 0.1

    def test_a_warm_call_stays_inside_the_per_call_budget_at_1_10_60_calls(self):
        """Once a reader's door is decided, a call decides nothing again (no grant
        encoded, no ``reader_scope``), borrows one connection and sends at most
        ``CALL_STATEMENTS`` statements and ``CALL_BYTES`` bytes, however many audience
        keys the reader holds (10,000 here: the grant is never sent), and its wall time
        stays inside ``CALL_SECONDS``, at 1, 10 and 60 calls (no row lost, no growth)."""
        import dataclasses
        import statistics
        import psycopg
        from operating.pg import door
        base = self.base()
        real = base._scope
        many = tuple(f"mail:box{i}@x" for i in range(10_000))
        grown = {}

        def scope(actor, at):   # the same grant objects every call, as a session's
            got = real(actor, at)
            if actor != FULL:
                return got
            held = grown.get(id(got.audience_keys))
            if held is None or held[0] is not got.audience_keys:
                held = grown[id(got.audience_keys)] = (
                    got.audience_keys, tuple(got.audience_keys) + many)
            return dataclasses.replace(got, audience_keys=held[1])
        base._scope = scope
        tool = sqlt.SqlTool(base)
        q = {"sql": "SELECT count(*) FROM customer_balances"}
        for actor in (FULL, OPS):
            want = tool.call("plx.sql", q, actor)   # the first call decides the door
            self.assertNotIn("error", want)
            sent, lent = [], []
            real_execute, real_server = psycopg.Cursor.execute, psycopg.ServerCursor.execute
            real_enter = sqlt._lent.__enter__

            def counted(real_fn):
                def execute(cur, query, params=None, *a, **k):
                    sent.append(len(str(query)) + sum(len(str(p)) for p in (
                        params.values() if isinstance(params, dict) else params or ())))
                    return real_fn(cur, query, params, *a, **k)
                return execute

            def enter(self_):
                lent.append(1)
                return real_enter(self_)
            times = {}
            with mock.patch.object(psycopg.Cursor, "execute", counted(real_execute)), \
                    mock.patch.object(psycopg.ServerCursor, "execute", counted(real_server)), \
                    mock.patch.object(sqlt._lent, "__enter__", enter), \
                    mock.patch.object(door, "reader_scope",
                                      side_effect=AssertionError("door decided again")):
                done = 0
                for n in (1, 10, 60):
                    del sent[:], lent[:]
                    took = []
                    for _ in range(n):
                        t0 = time.monotonic()
                        out = tool.call("plx.sql", q, actor)
                        took.append(time.monotonic() - t0)
                        self.assertNotIn("error", out)
                        self.assertEqual(out["rows"][0]["cells"], want["rows"][0]["cells"])
                    done += n
                    self.assertEqual(len(lent), n, (actor, n))   # one connection a call
                    self.assertLessEqual(len(sent), self.CALL_STATEMENTS * n, (actor, n))
                    self.assertLessEqual(sum(sent), self.CALL_BYTES * n, (actor, n))
                    times[n] = statistics.median(took)
                    self.assertLess(times[n], self.CALL_SECONDS, (actor, n, times))
            self.assertLess(times[60], max(2 * times[10], 0.02), (actor, times))

    def test_every_write_second_statement_and_catalog_read_is_refused(self):
        before = self.sql("SELECT count(*) FROM customers")
        for statement in (
                "DELETE FROM customers", "UPDATE customers SET name = 'x'",
                "INSERT INTO customers (name) VALUES ('x')", "CREATE TABLE t (a int)",
                "DROP VIEW customers", "COPY customers TO '/tmp/x'", "TRUNCATE customers",
                "SELECT 1; DELETE FROM customers", "SELECT 1; SELECT 2",
                "WITH d AS (DELETE FROM customers RETURNING *) SELECT * FROM d",
                "SELECT * FROM customers FOR UPDATE", "SET ROLE postgres",
                "SELECT set_config('plx.reader_audience', '[\"*\"]', false)",
                "SELECT \"set_config\"('plx.reader_marks', '[]', true)",
                "SELECT current_setting('plx.reader_hidden')", "SELECT pg_sleep(5)",
                "SELECT pg_read_file('/etc/passwd')", "SELECT lo_import('/etc/passwd')",
                "SELECT query_to_xml('select 1', true, true, '')",
                "SELECT dblink('host=x', 'select 1')", "SELECT * FROM pg_class",
                "SELECT * FROM information_schema.tables", "SELECT 'customers'::regclass",
                f"SELECT * FROM {bv.SCHEMA}.customers", "SELECT * FROM ontology.objects",
                "SELECT * FROM public.customers", "SELECT $$x$$", "SELECT 1 -- x",
                "SELECT /* x */ 1", "SELECT E'\\x41'", "TABLE customers",
                "VALUES (1)", "EXPLAIN SELECT 1", "", "   ", None, 7):
            out = sqlt.SqlTool(self.base()).call("plx.sql", {"sql": statement}, FULL)
            self.assertIn(out.get("error", {}).get("code"), ("invalid_sql", "invalid_request"),
                          (statement, out))
        for args in ({}, {"sql": "SELECT 1", "describe": True}, {"bogus": 1},
                     {"sql": "SELECT 1", "as_reader": "owner"}, {"describe": "nope"}):
            out = sqlt.SqlTool(self.base()).call("plx.sql", args, FULL)
            self.assertIn("error", out, args)
        self.assertEqual(self.sql("SELECT count(*) FROM customers"), {
            **before, "result": mock.ANY, "rows": mock.ANY, "seconds": mock.ANY})
        self.assertEqual(before["rows"][0]["cells"],
                         self.sql("SELECT count(*) FROM customers")["rows"][0]["cells"])

    # ------------------------------------------------------------------ connections

    def test_copy_converts_v1_tables_and_keeps_comms_tables_as_they_are(self):
        """``door.copy``: a comms table (its own unit rule, no v1 columns) and
        ``comms_units`` reach the copy unchanged; ``convert`` refuses them; the v1
        business tables are converted; the source is untouched."""
        if type(self) is not SqlDoor:
            self.skipTest("the canonical fixture is the copy's source")
        import psycopg
        from operating.pg import door
        src, dst = "biz_doorv1src", "biz_doorcopytest"
        comms = bv.View("emails_probe", "probe", [bv.Col("subject", "text", "s")],
                        lambda rd, as_of: iter(()), ())
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {src}")
            c.execute(f"CREATE TABLE {src}.state AS SELECT * FROM {bv.SCHEMA}.state")
            _to_v1(c, bv.SCHEMA, "customers", into=src)
            self.addCleanup(lambda: psycopg.connect(self.dsn, autocommit=True).execute(
                f"DROP SCHEMA IF EXISTS {src} CASCADE"))
            c.execute(f"CREATE TABLE {src}.comms_units (unit serial PRIMARY KEY, "
                      "audience_key text, marks text[])")
            c.execute(f"INSERT INTO {src}.comms_units (audience_key, marks) VALUES "
                      "('mbx:a', '{}'), ('mbx:b', '{pii}')")
            c.execute(f"CREATE TABLE {src}.emails_probe (handle text, subject text, "
                      "unit int, ai_ok boolean, excluded boolean)")
            c.execute(f"INSERT INTO {src}.emails_probe VALUES ('m1', 'Hi', 1, true, false),"
                      " ('m2', 'Freight', 2, false, false)")
            self.addCleanup(lambda: psycopg.connect(self.dsn, autocommit=True).execute(
                f"DROP TABLE IF EXISTS {src}.comms_units, {src}.emails_probe; "
                f"DROP SCHEMA IF EXISTS {dst} CASCADE; DROP SCHEMA IF EXISTS {dst}_sql CASCADE"))
            before = {n: sorted(c.execute(f"SELECT * FROM {src}.{n}").fetchall(), key=repr)
                      for n in ("comms_units", "emails_probe", "customers")}
            with self.assertRaises(ValueError):
                door.convert(c, src, "emails_probe", v=comms)
            with mock.patch.dict(bv.VIEWS, {"emails_probe": comms}), \
                    mock.patch.object(sql_views, "install", lambda *a, **k: None):
                out = {o["view"]: o for o in door.copy(self.dsn, src, dst)}
            self.assertTrue(out["emails_probe"].get("kept"))
            self.assertTrue(out["comms_units"].get("kept"))
            self.assertFalse(out["customers"].get("kept"))
            for n in ("comms_units", "emails_probe"):
                got = sorted(c.execute(f"SELECT * FROM {dst}.{n}").fetchall(), key=repr)
                self.assertEqual(got, before[n], n)
                self.assertFalse(door.is_v2(c, dst, n), n)
            self.assertTrue(door.is_v2(c, dst, "customers"))
            self.assertFalse(door.is_v1(c, dst, "customers"))
            for n, rows in before.items():   # the source untouched
                got = sorted(c.execute(f"SELECT * FROM {src}.{n}").fetchall(), key=repr)
                self.assertEqual(got, rows, n)
            self.assertTrue(door.is_v1(c, src, "customers"))

    def test_parallel_calls_each_run_on_their_own_connection(self):
        base = self.base()
        tool = sqlt.SqlTool(base)
        got, errors = [], []

        def one(i):
            try:
                got.append(tool.call("plx.sql", {"sql": "SELECT count(*), "
                                                 f"{i} FROM customer_balances"}, FULL))
            except Exception as exc:   # noqa: BLE001 - recorded for the assertion
                errors.append(exc)
        threads = [threading.Thread(target=one, args=(i,)) for i in range(sqlt.POOL_SIZE)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(int(r["rows"][0]["cells"][1]) for r in got),
                         list(range(sqlt.POOL_SIZE)))
        self.assertEqual({r["rows"][0]["cells"][0] for r in got}, {got[0]["rows"][0]
                                                                   ["cells"][0]})
        # the host's own connection was never used for them: still idle, still usable
        self.assertEqual(base.store.conn.execute("SELECT 1").fetchone()[0], 1)

    def test_scope_guard_fails_closed(self):
        import psycopg
        from operating.pg import door
        count = "SELECT count(*) FROM customers"
        full = self.scope()
        (n,), = self.raw(count, full)
        self.assertGreater(n, 0)
        # the views read the held classes as ranges: unset, empty or an id no class has,
        # and no older int4[] list set: no row
        for bad in (None, "", "{}", "{[-1,0)}"):
            self.assertEqual(self.raw(count, {**full, door.HELD: bad}), [(0,)], bad)
        self.assertNotIn("plx.classes", full)   # every view reads ranges: not sent
        # the int4[] list is read only while the ranges are unset (code from before them):
        # a ranges setting, even an empty one, is the only rule
        every = "{" + ",".join(map(str, range(1, 5000))) + "}"
        self.assertEqual(self.raw(count, {"plx.classes": every, door.HELD: "{}"}), [(0,)])
        self.assertEqual(self.raw(count, {"plx.classes": "{}"}), [(0,)])
        self.assertEqual(self.raw(count, {"plx.classes": ""}), [(0,)])
        # a malformed setting fails the statement (never read as some other scope)
        with self.assertRaises(psycopg.Error):
            self.raw(count, {**full, door.HELD: "{1,2}"})
        # a masked column whose allow-list is unset, or a class added after the reader's
        # scope was read, reads NULL even for the full reader
        sql = "SELECT amount_due FROM customer_balances WHERE document_number = 'INV-3'"
        self.assertEqual(self.raw(sql, full), [(Decimal("50.00"),)])
        for bad in (None, "", "{}"):
            self.assertEqual(self.raw(sql, {**full, door.range_setting_for("amount_due"): bad}),
                             [(None,)], bad)
        # an unknown class masks every masked column of the row (document_number too):
        # read it by its handle
        (handle,), = self.raw("SELECT handle FROM customer_balances WHERE document_number "
                              "= 'INV-3'", full)
        sql = f"SELECT amount_due FROM customer_balances WHERE handle = '{handle}'"
        self.assertEqual(self.raw(sql, full, [
            f"INSERT INTO {bv.SCHEMA}.col_classes VALUES (999, '{{\"handle\": \"[]\"}}', "
            "'{}')",
            f"UPDATE {bv.SCHEMA}.customer_balances SET col_class = 999 "
            "WHERE document_number = 'INV-3'"]), [(None,)])

    def _install(self):
        import psycopg
        for attempt in range(5):   # the role is the cluster's: parallel modules may race
            try:
                return sql_views.install(self.dsn)
            except psycopg.errors.InternalError:
                time.sleep(0.5 * (attempt + 1))
        return sql_views.install(self.dsn)

    def _reads(self, actor, read):
        """What ``actor`` reads: each parity column and one masked cell, via ``read``."""
        got = {view: sorted(map(str, read(f"SELECT {field} FROM {view}", actor)))
               for view, field in self.PARITY}
        got["masked"] = read("SELECT amount_due FROM customer_balances WHERE "
                             "document_number = 'INV-3'", actor)
        return got

    def test_views_made_now_give_code_from_before_the_ranges_its_own_rows(self):
        """A rollback or a revert: the old code sets only the int4[] lists (plx.classes,
        plx.dclasses, plx.u_<col>) and reads views this code made. It reads exactly the
        rows and cells this code's ranges give the same reader, never zero rows."""
        from operating.pg import door
        old_keys = ("plx.classes", "plx.dclasses", "plx.d_hidden")
        for actor in (FULL, OPS, NOBODY):
            new = self.scope(actor)
            self.assertIn(door.HELD, new)
            sqlt._DOOR.clear()
            with mock.patch.object(sqlt, "_ranges_only", return_value=False):
                both = self.scope(actor)   # what this code sends beside old-shape views
            old = {k: v for k, v in both.items()
                   if k in old_keys or k.startswith("plx.u_")}
            self.assertFalse(any(k in old for k in (door.HELD, door.DHELD)))
            self.assertTrue(any(k.startswith("plx.u_") for k in old), old)

            def under(settings):
                return lambda stmt, _a: [r[0] for r in self.raw(stmt, settings)]
            want = self._reads(actor, under(new))
            self.assertEqual(self._reads(actor, under(old)), want, actor)
            if actor is FULL:
                self.assertTrue(all(want[v] for v, _ in self.PARITY), want)
                self.assertEqual(want["masked"], [Decimal("50.00")])
            # the old code's lists after a rolled-back transaction left the ranges '':
            # still the old code's own rows
            self.assertEqual(self._reads(actor, under({**old, door.HELD: "",
                                                       door.DHELD: ""})), want, actor)

    def test_this_code_reads_views_made_before_the_ranges_with_its_own_rows(self):
        """This code over views an older release made (int4[] rule only, before
        ``sql_views install`` runs here): it sends the lists too, same rows and cells."""
        from operating.pg import door

        def read(stmt, actor):
            return self.column(self.sql(stmt, actor))
        want = {a: self._reads(a, read) for a in (FULL, OPS, NOBODY)}
        def old_rule(expr, held, listed):   # the rule as releases before the ranges made it
            return f"({expr} = ANY({door._ids(listed)}))"
        import psycopg
        try:
            with mock.patch.object(door, "_in", old_rule):
                self._install()
            with psycopg.connect(self.dsn) as c:
                self.assertFalse(sqlt._ranges_only(c))   # old-shape views are there
            for actor, rows in want.items():
                sqlt._DOOR.clear()
                self.assertEqual(self._reads(actor, read), rows, actor)
        finally:
            self._install()
            sqlt._DOOR.clear()
        with psycopg.connect(self.dsn) as c:
            self.assertTrue(sqlt._ranges_only(c))

    def test_a_full_result_fits_one_response(self):
        """Reproduced: the host sends a result twice (text and structured content) within
        MAX_RESULT_BYTES, so a 200-row result of ~28 KB, and the view list, were refused
        whole (result_too_large, nothing shown); rows are cut at MAX_BYTES instead and the
        list names each view's columns, its meanings one describe away."""
        from operating import mcp_server
        from operating.mcp_server_evidence import MAX_RESULT_BYTES
        client, tool = mcp_server._TO_CLIENT.set(True), mcp_server._TOOL.set("plx.sql")
        try:
            for statement in ("SELECT format('%300s', c.name) AS wide, g FROM customers c "
                              "CROSS JOIN generate_series(1, 400) g",
                              "SELECT c.name, g FROM customers c CROSS JOIN "
                              "generate_series(1, 400) g"):
                out = self.sql(statement)
                self.assertNotIn("error", out, out)
                self.assertTrue(out["truncated"] and out["rows"], out.get("row_count"))
                self.assertLessEqual(mcp_server._envelope_bytes(out), MAX_RESULT_BYTES)
            out = sqlt.SqlTool(self.base()).call("plx.sql", {"describe": True}, FULL)
            self.assertIn(" status, ", out["views"]["purchase_orders"])
            self.assertLessEqual(mcp_server._envelope_bytes(out), MAX_RESULT_BYTES)
        finally:
            mcp_server._TOOL.reset(tool)
            mcp_server._TO_CLIENT.reset(client)

    def test_a_refusal_while_the_tables_are_swapped_is_asked_again(self):
        """A psql session waits on a swap's lock; plx.sql asks again inside the call, so a
        refusal that ends within the wait returns the rows, one that does not end is still
        refused with no rows, and a call with no room for another statement does not wait."""
        import psycopg
        from operating import call_limits
        from operating.pg import door
        real, fails = door.check, [1]

        def swapped(pg, schema):
            if fails[0]:
                fails[0] -= 1
                raise psycopg.errors.LockNotAvailable("a swap holds the lock")
            return real(pg, schema)
        with mock.patch.object(sqlt, "BUSY_WAIT_SECONDS", 5.0), \
                mock.patch.object(sqlt, "BUSY_POLL_SECONDS", 0.05), \
                mock.patch.object(door, "check", swapped):
            self.assertIn("Acme Ltd", self.names())
            self.assertEqual(fails[0], 0)
            fails[0] = 10 ** 6
            started = time.monotonic()
            out = self.sql("SELECT name FROM customers")
            self.assertEqual(out.get("error", {}).get("code"), "not_ready", out)
            self.assertNotIn("rows", out)
            self.assertGreaterEqual(time.monotonic() - started, 4.9)
            fails[0] = 1
            with call_limits.sql_deadline(time.monotonic() + 8.0):
                started = time.monotonic()
                out = self.sql("SELECT name FROM customers")
            self.assertEqual(out.get("error", {}).get("code"), "not_ready", out)
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(fails[0], 0)

    def test_a_statement_meeting_a_swap_is_asked_again(self):
        """A refresh swapping a table under a running statement (the plan's table dropped:
        "could not open relation with OID", or its lock held past lock_timeout) is asked
        again inside the call and returns the rows; one that never ends is not_ready with
        no rows; any other internal error is refused at once."""
        import psycopg
        real, fails = sqlt.scoped_read, [0, "could not open relation with OID 288176778"]

        def flaky(pg, door_, work, *a, **k):
            def once(conn):
                if fails[0]:
                    fails[0] -= 1
                    raise (psycopg.errors.LockNotAvailable if fails[1] == "lock"
                           else psycopg.errors.InternalError_)(fails[1])
                return work(conn)
            return real(pg, door_, once, *a, **k)
        with mock.patch.object(sqlt, "BUSY_WAIT_SECONDS", 3.0), \
                mock.patch.object(sqlt, "BUSY_POLL_SECONDS", 0.05), \
                mock.patch.object(sqlt, "scoped_read", flaky):
            for why in ("could not open relation with OID 288176778", "lock"):
                fails[:] = [2, why]
                self.assertIn("Acme Ltd", self.names(), why)
                self.assertEqual(fails[0], 0)
            fails[:] = [10 ** 6, "could not open relation with OID 1"]
            out = self.sql("SELECT name FROM customers")
            self.assertEqual(out.get("error", {}).get("code"), "not_ready", out)
            self.assertNotIn("rows", out)
            fails[:] = [1, "some other internal failure"]
            started = time.monotonic()
            out = self.sql("SELECT name FROM customers")
            self.assertEqual(out.get("error", {}).get("code"), "query_failed", out)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_an_empty_door_state_is_not_ready(self):
        import psycopg
        from operating.ontology_tools_identity import ToolError
        from operating.pg import door
        grant = sqlt.reader_door(self.base(), FULL).grant
        for cached in (False, True):   # the tables found first, or already known
            if not cached:
                door._CHECK_SQL.clear()
            with psycopg.connect(self.dsn) as c:
                try:
                    c.execute(f"DELETE FROM {bv.SCHEMA}.{door.STATE}")
                    self.assertIsNone(door.check(c, bv.SCHEMA))
                    with self.assertRaises(ToolError) as e:
                        sqlt._decide(c, grant)
                    self.assertEqual(e.exception.code, "not_ready")
                finally:
                    c.rollback()
            with psycopg.connect(self.dsn) as c:
                self.assertIsNotNone(door.check(c, bv.SCHEMA))

    def test_held_classes_as_ranges_hold_exactly_the_held_ids(self):
        """``door.ranges``: as Postgres reads it, an id is inside exactly when it is held
        (runs, gaps, a single id, none, ids past every held one)."""
        import random
        import psycopg
        from operating.pg import door
        rng = random.Random(1118)
        sets = [set(), {5}, {1, 2, 3}, set(range(1, 629)), set(range(1, 629)) - {300},
                *({rng.randrange(1, 200) for _ in range(rng.randrange(1, 120))}
                  for _ in range(20))]
        with psycopg.connect(self.dsn) as c:
            for held in sets:
                got = {x for (x,) in c.execute(
                    "SELECT x FROM generate_series(-2, 700) x WHERE x <@ %s::int4multirange",
                    (door.ranges(held),)).fetchall()}
                self.assertEqual(got, held, door.ranges(held))

    def test_a_full_reader_reads_as_at_a_psql_prompt_and_no_one_else_does(self):
        # the ceiling run 11-18 (plain Claude, 60 s psql): a full reader's statement has
        # no plan-cost cap and 60 s; every other reader keeps the cap and 10 s; the rows
        # are the guarded views' either way (exclusion and Freightcom inside them)
        statement = ("SELECT corr(amount_due, amount_due) c, count(*) n "
                     "FROM customer_balances")
        from operating.pg import people_excluded
        as_full = mock.patch.object(people_excluded, "full_reader",
                                    lambda base, a: "tag" if a is FULL else "")
        with mock.patch.object(sqlt, "MAX_COST", 1.0), as_full:
            full = self.sql(statement, FULL)
            ops = self.sql(statement, OPS)
        self.assertNotIn("error", full, full)
        self.assertEqual(ops["error"]["code"], "too_costly")
        tool = sqlt.SqlTool(self.base())
        self.assertEqual(tool._seconds(True), sqlt.FULL_STATEMENT_MS / 1000.0)
        self.assertEqual(tool._seconds(), sqlt.STATEMENT_MS / 1000.0)
        self.assertEqual(tool._seconds(sqlt.SEARCH_FULL_MS), sqlt.SEARCH_FULL_MS / 1000.0)
        # undecidable fullness is no full reader (fail closed)
        with mock.patch.object(people_excluded, "full_reader", side_effect=RuntimeError):
            self.assertFalse(tool._full(FULL))

    def test_costly_full_reader_statements_share_a_few_box_wide_slots(self):
        # each Ask process has its own pool: without a box-wide bound N owner sessions
        # could run 4N uncapped statements, each spilling up to temp_file_limit
        import psycopg
        statement = "SELECT count(*) n FROM customer_balances"
        from operating.pg import people_excluded
        as_full = mock.patch.object(people_excluded, "full_reader",
                                    lambda base, a: "tag" if a is FULL else "")
        costly = mock.patch.object(sqlt, "MAX_COST", 1.0)
        short = mock.patch.object(sqlt, "FULL_COSTLY_WAIT_SECONDS", 1.0)
        holders = [psycopg.connect(self.dsn, autocommit=True)
                   for _ in range(sqlt.FULL_COSTLY_SLOTS)]
        try:
            for slot, other in enumerate(holders):   # other processes' long statements
                other.execute("SELECT pg_advisory_lock(%s, %s)",
                              (sqlt.FULL_COSTLY_LOCK, slot))
            with as_full, costly, short:
                import threading
                started, got = time.monotonic(), {}
                ask = threading.Thread(target=lambda: got.update(busy=self.sql(statement)))
                ask.start()
                # while it waits for a slot it holds no connection in a transaction
                waiting = []
                while ask.is_alive() and len(waiting) < 8:
                    time.sleep(0.1)
                    waiting.append(holders[0].execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND state LIKE 'idle in transaction%%'"
                        ).fetchone()[0])
                ask.join(10)
                busy = got["busy"]
                self.assertGreaterEqual(time.monotonic() - started, 0.9)
            self.assertEqual(busy["error"]["code"], "busy", busy)
            self.assertGreaterEqual(len(waiting), 5, waiting)
            self.assertLessEqual(sum(1 for n in waiting if n), 1, waiting)
            with as_full:   # under the cost every reader is held to: no slot needed
                self.assertNotIn("error", self.sql(statement))
            holders[0].execute("SELECT pg_advisory_unlock(%s, 0)", (sqlt.FULL_COSTLY_LOCK,))
            with as_full, costly, short:
                self.assertNotIn("error", self.sql(statement))
            # the slot was the call's transaction's: free again once it ended
            self.assertTrue(holders[0].execute("SELECT pg_try_advisory_lock(%s, 0)",
                                               (sqlt.FULL_COSTLY_LOCK,)).fetchone()[0])
        finally:
            for other in holders:
                other.close()

    def test_hidden_excluded_and_freightcom_rows_are_out_for_everyone(self):
        import psycopg
        from operating.pg import door
        self.assertIn("Acme Ltd", self.names())
        with psycopg.connect(self.dsn) as c:
            oid = c.execute(f"SELECT object_ids[1] FROM {bv.SCHEMA}.lineage l JOIN "
                            f"{bv.SCHEMA}.customers t USING (handle) WHERE t.name = "
                            "'Acme Ltd' AND l.view = 'customers'").fetchone()[0]
            try:
                c.execute("INSERT INTO ontology.people_excluded_objects VALUES "
                          "('x://later', %s, 'own_unit')", (oid,))
                self.assertGreaterEqual(door.exclude(c, bv.SCHEMA), 1)
                # (uncommitted: read on this connection, rolled back after)
                self.assertEqual(c.execute(f"SELECT count(*) FROM {bv.SCHEMA}.customers "
                                           "WHERE name = 'Acme Ltd'").fetchone()[0], 0)
                n = c.execute(f"SELECT sum(rows) FROM {bv.SCHEMA}.class_counts WHERE "
                              "view = 'customers'").fetchone()[0]
                self.assertEqual(n, c.execute(f"SELECT count(*) FROM {bv.SCHEMA}.customers"
                                              ).fetchone()[0])
            finally:
                c.rollback()

    def test_a_reader_holding_ten_thousand_keys_gets_the_same_rows_fast(self):
        import psycopg
        from operating.pg import door
        scope = self.base()._scope(FULL, self.base().clock())
        keys = sorted(scope.audience_keys) + [f"mail:box{i}@x" for i in range(10_000)]
        with psycopg.connect(self.dsn) as c:
            started = time.monotonic()
            many = door.reader_scope(c, bv.SCHEMA, audience_keys=keys,
                                     marking_sets=scope.marking_sets, held=scope.held,
                                     holds_every_doc=True)
            self.assertLess(time.monotonic() - started, 1.0)
            one = door.reader_scope(c, bv.SCHEMA, audience_keys=scope.audience_keys,
                                    marking_sets=scope.marking_sets, held=scope.held,
                                    holds_every_doc=True)
        self.assertEqual(many["settings"], one["settings"])

    def test_the_door_settings_cannot_be_read_or_set_from_a_query(self):
        before = self.names(OPS)
        for statement in (
                "SELECT current_setting('plx.classes')",
                "SELECT current_setting('plx.u_amount_due', true)",
                "SELECT current_setting('plx.held', true)",
                "SELECT set_config('plx.classes', '{1,2,3,4,5,6,7,8,9}', true)",
                "SELECT set_config('plx.held', '{[1,100000)}', true)",
                "SELECT name FROM customers WHERE set_config('plx.u_amount_due', '{1}', "
                "true) IS NOT NULL",
                "SET plx.classes = '{1,2,3}'", "RESET plx.classes", "RESET ALL",
                "SELECT name FROM customers; SET LOCAL plx.classes = '{1}'",
                "SELECT * FROM lineage", "SELECT * FROM access_classes",
                "SELECT * FROM col_classes", "SELECT name FROM customers -- ; DROP VIEW x"):
            out = self.sql(statement, OPS)
            if "error" not in out:   # a comment is harmless: the rows are the reader's own
                self.assertEqual(statement, "SELECT name FROM customers -- ; DROP VIEW x")
                self.assertEqual(set(self.column(out)), before)
                continue
            self.assertNotIn("rows", out, statement)
        self.assertEqual(self.names(OPS), before)

    def test_a_class_added_after_the_readers_door_was_read_shows_nothing_until_rebuilt(self):
        import psycopg
        from operating.pg import door
        self.assertIn("Acme Ltd", self.names(FULL))
        full_before, ops_before = self.names(FULL), self.names(OPS)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            (old,) = c.execute(f"SELECT access_class FROM {bv.SCHEMA}.customers WHERE name = "
                               "'Acme Ltd' LIMIT 1").fetchone()
            try:
                # written past ``door``'s own writers (no stamp change): every reader's
                # cached door predates the class, so the row is out for all of them
                c.execute(f"INSERT INTO {bv.SCHEMA}.access_classes SELECT 90000, audience_keys,"
                          " rec_marks, doc_marks || '{door-new}'::text[], blocked FROM "
                          f"{bv.SCHEMA}.access_classes WHERE id = %s", (old,))
                # an id past int2's range: the door's casts are int4 (the contract)
                c.execute(f"UPDATE {bv.SCHEMA}.customers SET access_class = 90000 WHERE name ="
                          " 'Acme Ltd'")
                self.assertNotIn("Acme Ltd", self.names(FULL))
                self.assertNotIn("Acme Ltd", self.names(OPS))
                # the door's stamp moves (as its own writers move it): the class is read
                # again, held only by a reader holding its new document marking (no
                # fixture reader holds it: still out); a reader holding every document
                # holds it
                door._bump(c, bv.SCHEMA)
                self.assertNotIn("Acme Ltd", self.names(FULL))
                self.assertNotIn("Acme Ltd", self.names(OPS))
                scope = self.base()._scope(FULL, self.base().clock())
                for every, held in ((True, True), (False, False)):
                    got = door.reader_scope(c, bv.SCHEMA, audience_keys=scope.audience_keys,
                                            marking_sets=scope.marking_sets, held=scope.held,
                                            holds_every_doc=every)["settings"]["plx.classes"]
                    self.assertEqual(90000 in {int(x) for x in str(got).strip("{}").split(",")
                                             if x.strip()}, held, got)
            finally:
                c.execute(f"UPDATE {bv.SCHEMA}.customers SET access_class = %s WHERE name = "
                          "'Acme Ltd'", (old,))
                c.execute(f"DELETE FROM {bv.SCHEMA}.access_classes WHERE id = 90000")
                door._bump(c, bv.SCHEMA)
        self.assertEqual(self.names(FULL), full_before)
        self.assertEqual(self.names(OPS), ops_before)

    def test_past_the_checker_the_store_still_refuses_and_a_changed_scope_returns_nothing(self):
        with mock.patch.object(sqlt, "check", lambda sql, **kw: sql):
            for statement, codes in (
                    ("SELECT name, set_config('plx.dheld', '{[1,100000)}', true) "
                     "FROM customers", ("integrity",)),
                    ("SELECT name, set_config('plx.d_hidden', '', true) "
                     "FROM customers", ("integrity",)),
                    ("SELECT name, set_config('plx.held', '{[1,100000)}', true) "
                     "FROM customers", ("integrity",)),
                    ("SELECT name, set_config('plx.r_amount_due', '{[1,100000)}', true) "
                     "FROM customers", ("integrity",)),
                    (f"SELECT * FROM {bv.SCHEMA}.lineage", ("not_readable",)),
                    (f"SELECT * FROM {bv.SCHEMA}.access_classes", ("not_readable",))):
                out = self.sql(statement, OPS)
                self.assertIn(out.get("error", {}).get("code"), codes, (statement, out))
                self.assertNotIn("rows", out)

    def test_a_view_whose_marks_outrun_its_masks_is_refused(self):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:
            before = c.execute(f"SELECT masked FROM {bv.SCHEMA}.door_views WHERE view = "
                               "'customer_balances'").fetchone()[0]
            self.assertIn("amount_due", before)
            c.execute(f"UPDATE {bv.SCHEMA}.door_views SET masked = '{{}}' WHERE view = "
                      "'customer_balances'")
            try:
                out = self.sql("SELECT count(*) FROM customer_balances", OPS)
                self.assertEqual(out.get("error", {}).get("code"), "not_ready", out)
            finally:
                c.execute(f"UPDATE {bv.SCHEMA}.door_views SET masked = %s WHERE view = "
                          "'customer_balances'", (before,))

    def test_sources_come_from_lineage_by_handle(self):
        out = self.sql("SELECT handle, name FROM customers WHERE name = 'Acme Ltd'")
        self.assertEqual(out.get("sources"), 1, out)
        entry = sqlt.RESULTS.get(out["result"].split("//", 1)[1])
        self.assertTrue(entry["sources"][0]["object_ids"])
        self.assertNotIn("sources", self.sql("SELECT handle FROM customers WHERE name = "
                                             "'Acme Ltd'", NOBODY))
        # a handle the reader cannot read, written out as a literal, finds no sources
        (handle,), = self.raw("SELECT handle FROM customers WHERE name = 'Acme Ltd'",
                              self.scope())
        for actor in (NOBODY,):
            out = self.sql(f"SELECT '{handle}' AS handle WHERE 'customers' <> ''", actor)
            self.assertNotIn("sources", out, out)
            entry = sqlt.RESULTS.get(out["result"].split("//", 1)[1])
            self.assertEqual(entry["sources"], [])

    def test_a_rebuild_swap_after_a_reader_was_cached_decides_their_door_again(self):
        """A document-resolved row the full reader holds; a rebuild's
        swap rewrites its lineage so it reads a document they may not open and re-creates
        the guarded view (``sql_views.create``, as ``build_view`` does). The next call,
        with the reader's door cached from before, no longer shows the row."""
        import psycopg
        from operating.pg import door
        from operating.search import document_marks
        s = bv.SCHEMA
        q = "SELECT count(*) FROM customers WHERE name = 'Acme Ltd'"
        marked = (frozenset({"door:doc-x"}), frozenset())
        with mock.patch.object(document_marks, "withheld_set", lambda held: marked), \
                psycopg.connect(self.dsn, autocommit=True) as c:
            old, handle = c.execute(f"SELECT access_class, handle FROM {s}.customers WHERE "
                                    "name = 'Acme Ltd'").fetchone()
            c.execute(f"INSERT INTO {s}.access_classes SELECT 90010, audience_keys, "
                      f"rec_marks, %s, blocked FROM {s}.access_classes WHERE id = %s",
                      ([door.UNREADABLE], old))
            c.execute(f"UPDATE {s}.customers SET access_class = 90010 WHERE handle = %s",
                      (handle,))
            door._bump(c, s)
            try:
                self.assertEqual(self.column(self.sql(q)), ["1"])
                with c.transaction():
                    c.execute(f"UPDATE {s}.lineage SET read_ids = read_ids || "
                              "'{door:doc-x}' WHERE view = 'customers' AND handle = %s",
                              (handle,))
                    sql_views.create(c, bv.VIEWS["customers"])
                self.assertEqual(self.column(self.sql(q)), ["0"])   # cache not cleared
            finally:
                c.execute(f"UPDATE {s}.lineage SET read_ids = array_remove(read_ids, "
                          "'door:doc-x') WHERE view = 'customers' AND handle = %s", (handle,))
                c.execute(f"UPDATE {s}.customers SET access_class = %s WHERE handle = %s",
                          (old, handle))
                c.execute(f"DELETE FROM {s}.access_classes WHERE id = 90010")
                door._bump(c, s)

    def test_the_door_is_decided_in_the_querys_own_snapshot(self):
        """The door's stamp and the people exclusion are read, and the door decided, on
        the query's own connection inside its REPEATABLE READ transaction (one snapshot);
        a door kept for an older stamp is never used at a newer one (a swap committed
        between two calls: the next one decides again)."""
        import psycopg
        from operating.pg import door
        tool = sqlt.SqlTool(self.base())
        seen = []
        real_check, real_scope = door.check, door.reader_scope

        def check(pg, schema):
            seen.append(("check", id(pg), pg.info.transaction_status, pg.read_only,
                         pg.isolation_level))
            return real_check(pg, schema)

        def scope(pg, schema, **kw):
            seen.append(("scope", id(pg), pg.info.transaction_status))
            return real_scope(pg, schema, **kw)
        q = {"sql": "SELECT count(*) FROM customers"}
        with mock.patch.object(door, "check", check), \
                mock.patch.object(door, "reader_scope", scope):
            first = tool.call("plx.sql", q, FULL)
            again = tool.call("plx.sql", q, FULL)
            with psycopg.connect(self.dsn, autocommit=True) as c:
                door._bump(c, bv.SCHEMA)   # a swap commits between two calls
                stamp = door.stamp(c, bv.SCHEMA)
            after = tool.call("plx.sql", q, FULL)
        self.assertNotIn("error", first)
        self.assertEqual(first["rows"][0]["cells"], again["rows"][0]["cells"])
        self.assertEqual(after["rows"][0]["cells"], first["rows"][0]["cells"])
        intrans = psycopg.pq.TransactionStatus.INTRANS
        checks = [e for e in seen if e[0] == "check"]
        self.assertEqual(len(checks), 3)
        for e in checks:
            self.assertEqual(e[2:], (intrans, True, psycopg.IsolationLevel.REPEATABLE_READ))
        # decided at the first call and again after the bump only, each on the connection
        # its check read, in the same transaction
        scopes = [i for i, e in enumerate(seen) if e[0] == "scope"]
        self.assertEqual(len(scopes), 2, seen)
        for i in scopes:
            self.assertEqual((seen[i - 1][1], seen[i][2]), (seen[i][1], intrans))
        self.assertEqual({k[2] for k in sqlt._DOOR}, {stamp - 1, stamp})

    def test_a_grown_exclusion_hides_the_row_and_a_door_behind_the_set_refuses(self):
        """The people exclusion's update grows the set; the v2 tables follow it
        (``door.exclude`` on every v2 schema). While they have not, every reader is
        refused (the door's recorded stamp is not the set's), never served the row."""
        import psycopg
        from operating.pg import door
        from operating.pg import people_excluded as pe
        s = bv.SCHEMA
        late = "Late Excluded Co"

        class Inputs:   # the decision side of the update: the newly excluded object
            tag = "probe-tag"

            def __init__(self, *a, **k):
                pass

            def rows(self, *a, **k):
                return [("x://late", "door:late", "own_unit")]

            def close(self):
                pass

        with psycopg.connect(self.dsn, autocommit=True) as c:
            saved = c.execute(f"SELECT exclusion, covered_ver FROM {pe.STATE}").fetchone()
            # (handles are unique: the copy is renamed before it goes in)
            c.execute(f"CREATE TEMP TABLE door_late AS SELECT * FROM {s}.customers WHERE "
                      "name = 'Acme Ltd' LIMIT 1")
            c.execute("UPDATE door_late SET name = %s, handle = 'api://door/late'", (late,))
            c.execute(f"INSERT INTO {s}.customers SELECT * FROM door_late")
            c.execute("DROP TABLE door_late")
            c.execute(f"INSERT INTO {s}.lineage (view, handle, object_ids) VALUES "
                      "('customers', 'api://door/late', ARRAY['door:late'])")
            door.recount(c, s, "customers")
            if saved:
                c.execute(f"UPDATE {pe.STATE} SET exclusion = 'probe-tag'")
            else:
                c.execute(f"INSERT INTO {pe.STATE} VALUES (true, 'probe-tag', 'x', 0, 0, 0, "
                          "NULL)")
            door.exclude_everywhere(c)
            try:
                self.assertIn(late, self.names())
                env = {"PLX_ONTOLOGY_PG": "1"}
                low = c.execute(f"SELECT covered_ver FROM {pe.STATE}").fetchone()[0]
                with mock.patch.object(pe, "current_exclusion", lambda e=None: "probe-tag"), \
                        mock.patch.object(pe, "_Inputs", Inputs), \
                        mock.patch.object(pe, "watermark", lambda conn: low + 1), \
                        mock.patch.object(door, "exclude", side_effect=RuntimeError("down")):
                    with psycopg.connect(self.dsn) as u:
                        got = pe.update(u, "/nonexistent", environ=env)
                self.assertTrue(str(got["door"][s]).startswith("failed"), got)
                # the set grew and the door did not follow: refused for every reader
                for actor in (FULL, OPS, NOBODY):
                    out = self.sql("SELECT name FROM customers", actor)
                    self.assertEqual(out.get("error", {}).get("code"), "not_ready", out)
                # the next update, even one that finds nothing new, brings the door up to
                # the set
                with mock.patch.object(pe, "current_exclusion", lambda e=None: "probe-tag"), \
                        mock.patch.object(pe, "watermark", lambda conn: low + 1):
                    with psycopg.connect(self.dsn) as u:
                        got = pe.update(u, "/nonexistent", environ=env)
                self.assertEqual(got["door"][s], 1, got)
                self.assertNotIn(late, self.names())
                self.assertIn("Acme Ltd", self.names())
                self.assertEqual(c.execute(f"SELECT count(*) FROM {s}.lineage WHERE handle = "
                                           "'api://door/late'").fetchone()[0], 0)
                # and a set changed by a whole build (another list tag) refuses again
                c.execute(f"UPDATE {pe.STATE} SET exclusion = 'another-tag'")
                out = self.sql("SELECT count(*) FROM customers")
                self.assertEqual(out.get("error", {}).get("code"), "not_ready", out)
            finally:
                c.execute(f"DELETE FROM {pe.TABLE} WHERE object_id = 'door:late'")
                c.execute(f"DELETE FROM {s}.customers WHERE handle = 'api://door/late'")
                c.execute(f"DELETE FROM {s}.lineage WHERE handle = 'api://door/late'")
                if saved:
                    c.execute(f"UPDATE {pe.STATE} SET exclusion = %s, covered_ver = %s", saved)
                else:
                    c.execute(f"DELETE FROM {pe.STATE}")
                door.recount(c, s, "customers")
                c.execute(f"UPDATE {s}.door_state SET excluded_at = %s",
                          (door.exclusion_stamp(c),))

    def test_readers_are_served_while_the_exclusion_update_runs(self):
        """Reproduced: the people exclusion's update (every 10 minutes) committed the grown
        set, then followed it on the v2 tables in a transaction of minutes whose door
        ``ensure`` ALTERed door_state: every plx.sql call in that time waited and was
        refused. Then (fd30fe7d22) the set and a whole-set follow committed together: no
        refusal, but the old rows were served for the follow's minutes after the set was
        decided. Now the set and the follow of the objects it added (the lineage's indexes)
        commit together in one short transaction: a reader inside it reads the old,
        committed set's rows at once (never refused, never a lock wait); from the commit no
        reader reads the excluded row; the whole-set test is never run for a door that was
        current. A door already behind the set is refused at once (no wait that cannot
        end) and decided whole after the commit."""
        import psycopg
        from operating.pg import door
        from operating.pg import people_excluded as pe
        s = bv.SCHEMA
        late = "Late Excluded Mid Co"

        class Inputs:   # the decision side of the update: the newly excluded object
            tag = "probe-tag"

            def __init__(self, *a, **k):
                pass

            def rows(self, *a, **k):
                return [("x://late-mid", "door:late-mid", "own_unit")]

            def close(self):
                pass

        real, during, calls = door.exclude, [], []

        def exclude(write, schema, **kw):
            calls.append(kw.get("among"))
            got = real(write, schema, **kw)
            # still inside the update's transaction: another reader's call, on its own
            # connection (a lock wait would end at the statement limit: refused)
            started = time.monotonic()
            during.append((self.sql("SELECT name FROM customers"), time.monotonic() - started))
            return got
        with psycopg.connect(self.dsn, autocommit=True) as c:
            saved = c.execute(f"SELECT exclusion, covered_ver FROM {pe.STATE}").fetchone()
            c.execute(f"CREATE TEMP TABLE door_late AS SELECT * FROM {s}.customers WHERE "
                      "name = 'Acme Ltd' LIMIT 1")
            c.execute("UPDATE door_late SET name = %s, handle = 'api://door/late-mid'",
                      (late,))
            c.execute(f"INSERT INTO {s}.customers SELECT * FROM door_late")
            c.execute("DROP TABLE door_late")
            c.execute(f"INSERT INTO {s}.lineage (view, handle, object_ids) VALUES "
                      "('customers', 'api://door/late-mid', ARRAY['door:late-mid'])")
            door.recount(c, s, "customers")
            if saved:
                c.execute(f"UPDATE {pe.STATE} SET exclusion = 'probe-tag'")
            else:
                c.execute(f"INSERT INTO {pe.STATE} VALUES (true, 'probe-tag', 'x', 0, 0, 0, "
                          "NULL)")
            door.exclude_everywhere(c)
            try:
                self.assertIn(late, self.names())
                low = c.execute(f"SELECT covered_ver FROM {pe.STATE}").fetchone()[0]
                with mock.patch.object(pe, "current_exclusion", lambda e=None: "probe-tag"), \
                        mock.patch.object(pe, "_Inputs", Inputs), \
                        mock.patch.object(pe, "watermark", lambda conn: low + 1), \
                        mock.patch.object(door, "exclude", exclude):
                    with psycopg.connect(self.dsn) as u:
                        got = pe.update(u, "/nonexistent", environ={"PLX_ONTOLOGY_PG": "1"})
                self.assertEqual(got["door"][s], 1, got)
                self.assertEqual(got["objects_added"], 1, got)
                # only the added object was tested (the indexes), never the whole set
                self.assertEqual(calls, [["door:late-mid"]])
                (out, seconds), = during
                self.assertNotIn("error", out, out)   # the old, committed set's rows
                self.assertIn(late, {_cell(r["cells"][0]) for r in out["rows"]})
                self.assertLess(seconds, 5.0)
                self.assertNotIn(late, self.names())
                self.assertIn("Acme Ltd", self.names())
                # a door already behind the set: refused at once (no Busy wait), then
                # decided whole after the update's commit, and served again
                c.execute(f"UPDATE {s}.door_state SET excluded_at = 'behind'")
                with mock.patch.object(sqlt, "BUSY_WAIT_SECONDS", 5.0):
                    started = time.monotonic()
                    out = self.sql("SELECT name FROM customers")
                    self.assertEqual(out.get("error", {}).get("code"), "not_ready", out)
                    self.assertLess(time.monotonic() - started, 2.0)
                calls.clear()
                Inputs.rows = lambda self, *a, **k: []   # PROBE: nothing new decided
                low = c.execute(f"SELECT covered_ver FROM {pe.STATE}").fetchone()[0]
                with mock.patch.object(pe, "current_exclusion", lambda e=None: "probe-tag"), \
                        mock.patch.object(pe, "_Inputs", Inputs), \
                        mock.patch.object(pe, "watermark", lambda conn: low + 1), \
                        mock.patch.object(door, "exclude", exclude):
                    with psycopg.connect(self.dsn) as u:
                        got = pe.update(u, "/nonexistent", environ={"PLX_ONTOLOGY_PG": "1"})
                self.assertEqual(got["objects_added"], 0, got)   # already in the set
                self.assertEqual(calls, [None], got)   # the whole-set catch-up only
                self.assertIsInstance(got["door"][s], int, got)
                self.assertIn("Acme Ltd", self.names())
                self.assertNotIn(late, self.names())
            finally:
                c.execute(f"DELETE FROM {pe.TABLE} WHERE object_id = 'door:late-mid'")
                c.execute(f"DELETE FROM {s}.customers WHERE handle = 'api://door/late-mid'")
                c.execute(f"DELETE FROM {s}.lineage WHERE handle = 'api://door/late-mid'")
                if saved:
                    c.execute(f"UPDATE {pe.STATE} SET exclusion = %s, covered_ver = %s", saved)
                else:
                    c.execute(f"DELETE FROM {pe.STATE}")
                door.recount(c, s, "customers")
                c.execute(f"UPDATE {s}.door_state SET excluded_at = %s",
                          (door.exclusion_stamp(c),))

    def test_a_padded_or_doubled_string_never_reaches_the_api_whole(self):
        """Cells are cut by the statement and rows stream until the byte cap: a query
        building 5 MB cells returns cut cells, never the whole text."""
        import psycopg
        fetched = []
        real = psycopg.ServerCursor.fetchmany

        def counting(cur, size=0):
            got = real(cur, size)
            fetched.extend(got)
            return got

        with mock.patch.object(psycopg.ServerCursor, "fetchmany", counting):
            out = self.sql("SELECT format('%5000000s', name) AS a, "
                           "replace(format('%5000000s', name), ' ', 'xx') AS b, "
                           "ARRAY[format('%5000000s', name)] AS c, g AS n "
                           "FROM customers, generate_series(1, 300) g")
        self.assertNotIn("error", out, out)
        self.assertTrue(out["truncated"])
        # every text, and every element of an array, cut by the statement
        self.assertLessEqual(max(len(str(e)) for r in fetched for x in r
                                 for e in (x if isinstance(x, list) else [x])),
                             sqlt.MAX_CELL + 1)
        self.assertLessEqual(len(fetched), sqlt.MAX_ROWS + 1)
        self.assertLessEqual(sum(len(str(x)) for r in fetched for x in r),
                             sqlt.FETCH_BYTES + sqlt.FETCH_BATCH * 4 * (sqlt.MAX_CELL + 60))
        a, b, c, n = out["rows"][0]["cells"]
        self.assertTrue(a.get("truncated") and b.get("truncated"), (a, b))
        self.assertIsInstance(c, list)
        self.assertEqual(n, str(int(n)))   # a number stays a number

    def test_cte_and_from_alias_column_lists_are_accepted(self):
        out = self.sql("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r "
                       "WHERE n < 3) SELECT sum(n) FROM r")
        self.assertEqual(out["rows"][0]["cells"], ["6"], out)
        out = self.sql("SELECT sum(a) FROM unnest(ARRAY[1, 2, 4]) f(a)")
        self.assertEqual(out["rows"][0]["cells"], ["7"], out)
        out = self.sql("WITH t(x, y) AS MATERIALIZED (SELECT 1, 2) SELECT x + y FROM t")
        self.assertEqual(out["rows"][0]["cells"], ["3"], out)
        # a call is still a call: arguments that are not bare names, or no AS ( after
        for statement in ("SELECT 1, set_config('plx.classes', '{1}', true) AS (",
                          "SELECT (1) set_config('plx.classes', '{1}')",
                          "WITH r(n) AS (SELECT 1), pg_sleep(n) AS (SELECT 1) SELECT 1",
                          "SELECT lpad(name, 900000000) FROM customers"):
            out = self.sql(statement)
            self.assertIn(out.get("error", {}).get("code"), ("invalid_sql", "sql_error"),
                          (statement, out))
            self.assertNotIn("rows", out)

    def test_a_needs_cell_the_reader_lacks_is_masked_counted_and_partial(self):
        """The contract's ``needs``: a column read from another record (a mailed promise)
        is read only by a reader holding that record too; any other reader gets NULL, the
        cell counted withheld, the answer partial. A malformed or blocked need masks for
        everyone."""
        import psycopg
        from operating.pg import door
        s, view = bv.SCHEMA, "customer_balances"
        need = {"customer": {"audience_keys": ["mail:needs-probe@x"], "rec_marks": ["[]"],
                             "doc_marks": [], "blocked": False}}
        scope = self.base()._scope(FULL, self.base().clock())
        with psycopg.connect(self.dsn, autocommit=True) as c:
            handle, old = c.execute(f"SELECT handle, col_class FROM {s}.{view} WHERE customer "
                                    "IS NOT NULL ORDER BY handle LIMIT 1").fetchone()
            sql = f"SELECT customer FROM {view} WHERE handle = '{handle}'"
            self.assertIsNotNone(self.column(self.sql(sql))[0])

            def door_of(cid, keys):
                got = door.reader_scope(c, s, audience_keys=keys,
                                        marking_sets=scope.marking_sets, held=scope.held,
                                        holds_every_doc=True)["settings"]
                return cid in {int(x) for x in got.get("plx.u_customer", "{}").strip("{}")
                               .split(",") if x}
            try:
                for cid, needs in ((90001, need), (90002, {"customer": "bogus"}),
                                   (90003, {"customer": {**need["customer"], "blocked": True}})):
                    c.execute(f"INSERT INTO {s}.col_classes SELECT %s, marks, %s::jsonb FROM "
                              f"{s}.col_classes WHERE id = %s", (cid, json.dumps(needs), old))
                c.execute(f"UPDATE {s}.{view} SET col_class = 90001 WHERE handle = %s",
                          (handle,))
                door.recount(c, s, view)
                sql_views.create(c, bv.VIEWS[view])
                door._bump(c, s)
                out = self.sql(sql)
                self.assertEqual(self.column(out), [None], out)
                self.assertTrue(out["partial"])
                self.assertEqual(out["withheld"][view]["customer"], 1)
                keys = sorted(scope.audience_keys)
                self.assertFalse(door_of(90001, keys))
                self.assertTrue(door_of(90001, keys + ["mail:needs-probe@x"]))
                for bad in (90002, 90003):
                    self.assertFalse(door_of(bad, keys + ["mail:needs-probe@x"]), bad)
            finally:
                c.execute(f"UPDATE {s}.{view} SET col_class = %s WHERE handle = %s",
                          (old, handle))
                c.execute(f"DELETE FROM {s}.col_classes WHERE id IN (90001, 90002, 90003)")
                door.recount(c, s, view)
                sql_views.create(c, bv.VIEWS[view])
                door._bump(c, s)
        self.assertIsNotNone(self.column(self.sql(sql))[0])

    def test_a_document_row_is_checked_by_id_over_its_own_and_read_records(self):
        """doc_marks UNREADABLE (the builder's 'resolve documents by id'): the row is read
        only while no record of it, own or read, is a document the reader may not open."""
        import psycopg
        from operating.pg import door
        s = bv.SCHEMA
        scope = self.base()._scope(FULL, self.base().clock())
        count = "SELECT count(*) FROM customers WHERE name = 'Acme Ltd'"
        with psycopg.connect(self.dsn) as c:
            try:
                (old, handle) = c.execute(f"SELECT access_class, handle FROM {s}.customers "
                                          "WHERE name = 'Acme Ltd'").fetchone()
                c.execute(f"INSERT INTO {s}.access_classes SELECT 90010, audience_keys, "
                          "rec_marks, %s, blocked FROM " f"{s}.access_classes WHERE id = %s",
                          ([door.UNREADABLE], old))
                c.execute(f"UPDATE {s}.customers SET access_class = 90010 WHERE handle = %s",
                          (handle,))
                c.execute(f"UPDATE {s}.lineage SET read_ids = read_ids || '{{door:doc-x}}' "
                          "WHERE view = 'customers' AND handle = %s", (handle,))

                def scoped(hidden):
                    return door.reader_scope(c, s, audience_keys=scope.audience_keys,
                                             marking_sets=scope.marking_sets, held=scope.held,
                                             holds_every_doc=True, hidden_docs=hidden
                                             )["settings"]
                open_ = scoped([])
                self.assertIn("90010", open_["plx.dclasses"])
                self.assertNotIn("90010", open_["plx.classes"])
                shut = scoped(["door:doc-x"])   # a read record the reader may not open
                self.assertIn(handle, shut["plx.d_hidden"])

                def rows(settings):   # as the role, its settings undone after
                    c.execute("SAVEPOINT r")
                    try:
                        for k, val in settings.items():
                            if val is not None:
                                c.execute("SELECT set_config(%s, %s, true)", (k, val))
                        c.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
                        return c.execute(count.replace("customers", sql_views.guarded_schema()
                                                       + ".customers")).fetchone()[0]
                    finally:
                        c.execute("ROLLBACK TO SAVEPOINT r")
                self.assertEqual(rows(open_), 1)
                self.assertEqual(rows(shut), 0)
                self.assertEqual(rows({**open_, "plx.d_hidden": None}), 0)   # unset: out

                # the marked documents unreadable (``withheld_set`` None): a reader not
                # holding every document holds no document-resolved class (fail closed)
                from operating.search import document_marks
                with mock.patch.object(document_marks, "withheld_set", lambda held: None):
                    hidden, known, _ = sqlt.reader_grant(scope).documents()
                self.assertFalse(known)

                def lazily(known):
                    return door.reader_scope(c, s, audience_keys=scope.audience_keys,
                                             marking_sets=scope.marking_sets, held=scope.held,
                                             holds_every_doc=False,
                                             hidden_docs=lambda: ((), known))["settings"]
                self.assertNotIn("90010", lazily(False)["plx.dclasses"])
                self.assertEqual(rows(lazily(False)), 0)
                self.assertIn("90010", lazily(True)["plx.dclasses"])
                self.assertEqual(rows(lazily(True)), 1)
            finally:
                c.rollback()

    def test_the_exclusion_follows_read_records_and_column_records(self):
        """``door.exclude``: a row one of whose READ records is an excluded person's goes; a
        cell read only from one is emptied, its row stays."""
        import psycopg
        from operating.pg import door
        from operating.pg import people_excluded as pe
        s, view = bv.SCHEMA, "customer_balances"
        with psycopg.connect(self.dsn) as c:
            try:
                (h1,), (h2,) = c.execute(f"SELECT handle FROM {s}.{view} WHERE customer IS "
                                         "NOT NULL ORDER BY handle LIMIT 2").fetchall()
                c.execute(f"UPDATE {s}.lineage SET read_ids = read_ids || '{{door:read-x}}' "
                          "WHERE view = %s AND handle = %s", (view, h1))
                c.execute(f"UPDATE {s}.lineage SET col_objects = col_objects || "
                          "'{\"customer\": [\"door:col-x\"]}' WHERE view = %s AND handle = %s",
                          (view, h2))
                c.execute(f"INSERT INTO {pe.TABLE} VALUES ('x://r', 'door:read-x', 'own_unit'),"
                          " ('x://c', 'door:col-x', 'own_unit')")
                self.assertEqual(door.exclude(c, s), 2)
                self.assertEqual(c.execute(f"SELECT count(*) FROM {s}.{view} WHERE handle = %s",
                                           (h1,)).fetchone()[0], 0)
                self.assertEqual(c.execute(f"SELECT customer FROM {s}.{view} WHERE handle = %s",
                                           (h2,)).fetchall(), [(None,)])
                self.assertEqual(c.execute(f"SELECT col_objects ? 'customer' FROM {s}.lineage "
                                           "WHERE view = %s AND handle = %s",
                                           (view, h2)).fetchone()[0], False)
            finally:
                c.rollback()

    def test_view_state_says_when_never_how_many(self):
        self.assertEqual(self.raw("SELECT * FROM view_state LIMIT 1", self.scope())[0][1:],
                         self.raw("SELECT built_at FROM view_state LIMIT 1", self.scope())[0])
        import psycopg
        with self.assertRaises(psycopg.errors.UndefinedColumn):
            self.raw("SELECT rows FROM view_state", self.scope())


class SqlDoorV2(SqlDoor):
    """The same fixture as OLD-SHAPE (v1) tables converted by ``door.convert`` into the
    canonical classes contract: every test of the canonical fixture holds over the
    conversion too. Before the conversion the fixture gains rows the conversion must
    decide: a NULL audience key, a Freightcom name, an excluded person's record, a
    document marked beyond OPS and a PLX-issued document held as a supplier's."""

    EXTRA = {"Nullkey Co": "audience_keys = NULL", "Freightcom Logistics": None,
             "Excluded Co": None, "Marked Doc Co": None, "Own Issued Co": None}

    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg import door
        from operating.search import document_marks as dm
        super().setUpClass()
        # plx.business reads the v1 columns: its answers, taken before the conversion
        probe = cls("test_each_reader_gets_exactly_the_rows_plx_business_gives_it")
        cls.expected = {(actor, view, field): SqlDoor.business(probe, view, field, actor)
                        for actor in (FULL, OPS, NOBODY) for view, field in cls.PARITY}
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            built = [n for n in bv.VIEWS if bv.VIEWS[n].fill is None and c.execute(
                "SELECT to_regclass(%s)", (f"{bv.SCHEMA}.{n}",)).fetchone()[0]]
            for name in built:
                _to_v1(c, bv.SCHEMA, name)
            for i, name in enumerate(cls.EXTRA):
                c.execute(f"INSERT INTO {bv.SCHEMA}.customers SELECT * FROM {bv.SCHEMA}."
                          "customers WHERE name = 'Acme Ltd' LIMIT 1")
                c.execute(f"UPDATE {bv.SCHEMA}.customers SET name = %s, handle = %s, "
                          "object_ids = ARRAY[%s] WHERE ctid = (SELECT max(ctid) FROM "
                          f"{bv.SCHEMA}.customers)", (name, f"api://door/{i}", f"door:{i}"))
            c.execute(f"UPDATE {bv.SCHEMA}.customers SET audience_keys = NULL WHERE "
                      "name = 'Nullkey Co'")
            c.execute("INSERT INTO ontology.people_excluded_objects VALUES "
                      "('x://door', 'door:2', 'own_unit')")
            # a document marking the full reader holds and OPS does not
            base = probe.base()
            held = {a: frozenset(base._scope(a, base.clock()).held) for a in (FULL, OPS)}
            probe.doCleanups()
            token = sorted(held[FULL] - held[OPS])[0]
            marked = ({"door:3": frozenset({token})}, {})
            with mock.patch.object(dm, "enabled", lambda: True), \
                    mock.patch.object(dm._MARKED, "get", lambda: marked), \
                    mock.patch.object(dm, "_own_issued", lambda: frozenset({"door:4"})):
                for name in built:
                    door.convert(c, bv.SCHEMA, name)

    def business(self, view, field, actor):
        return self.expected[(actor, view, field)]

    def column(self, out, i=0):
        got = super().column(out, i)
        return [v for v in got if v not in self.EXTRA]

    def test_the_tables_are_narrow_and_the_build_decided_every_row(self):
        import psycopg
        with psycopg.connect(self.dsn) as c:
            cols = {r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name = 'customers'", (bv.SCHEMA,))}
            excluded = c.execute(f"SELECT count(*) FROM {bv.SCHEMA}.customers WHERE "
                                 "name = 'Excluded Co'").fetchone()[0]
        self.assertTrue({"access_class", "col_class", "ai_ok"} <= cols)
        self.assertFalse(cols & set(sql_views.INTERNAL))
        self.assertEqual(excluded, 0)
        full = self.names()
        self.assertIn("Acme Ltd", full)
        self.assertIn("Marked Doc Co", full)   # the full reader holds the document's marking
        for gone in ("Nullkey Co", "Freightcom Logistics", "Excluded Co", "Own Issued Co"):
            self.assertNotIn(gone, full)
        self.assertNotIn("Marked Doc Co", self.names(OPS))


if __name__ == "__main__":
    unittest.main()
