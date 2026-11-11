"""``ontology_tools.snapshot_reads``: a record page's margin reads tens of plan runs over the
same records as of one moment, each object's visible version read once per scope. A
reader never sees through another reader's kept answer, and the answers are the ones
read without the snapshot."""

from __future__ import annotations

import datetime as dt
import json
import itertools
import sqlite3
import unittest

from ontology import store_ddl
from operating import customer_profit as cp
from operating import ontology_tools as ot
from tests.test_customer_profit import FULL, READER, TODAY, World


class SnapshotReadsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.case = unittest.TestCase()
        cls._cleanups_ = []
        cls.case.addCleanup = cls._cleanups_.append   # type: ignore[method-assign]
        cls.world = World(cls.case)
        cls.customer = cls.world.customer("C-50")
        # the same store read by a reader granted no Business Central customer records
        policy = ot.ReadPolicy(tenant="plx", audiences={"api:*": {READER}},
                               markings={m: {READER} for m in store_ddl.MARKINGS})
        cls.narrow = ot.OntologyTools(cls.world.base.store, policy,
                                      schema=ot.default_schema(),
                                      grammar=ot.default_grammar())

    @classmethod
    def tearDownClass(cls) -> None:
        for cleanup in reversed(cls._cleanups_):
            cleanup()

    def read(self, base, actor=FULL) -> dict:
        table: dict = {}
        ot.register(table, base)
        return table["plx.object"]["handler"]({"object_id": self.customer}, actor)

    def test_one_read_per_scope_and_the_same_answer(self) -> None:
        store = self.world.base.store
        plain = self.read(self.world.base)
        reads = []
        original = store._visible_versions_read

        def counted(scope, ids):
            reads.append(list(ids))
            return original(scope, ids)

        store._visible_versions_read = counted   # type: ignore[method-assign]
        self.addCleanup(lambda: delattr(store, "_visible_versions_read"))
        idents = []
        original_ids = store._primary_identifiers_read

        def counted_ids(scope, ids):
            idents.append(list(ids))
            return original_ids(scope, ids)

        store._primary_identifiers_read = counted_ids   # type: ignore[method-assign]
        self.addCleanup(lambda: delattr(store, "_primary_identifiers_read"))
        with ot.snapshot_reads():
            first = self.read(self.world.base)
            second = self.read(self.world.base)
        self.assertNotIn([self.customer], idents[1:])   # its identifiers read once too
        self.assertEqual(first["meta"]["as_known_at"], second["meta"]["as_known_at"])
        self.assertEqual(first["object"], plain["object"])
        self.assertEqual(second["object"], plain["object"])
        self.assertTrue(reads)
        self.assertNotIn([self.customer], reads[1:])   # the customer was read once
        # outside the snapshot every call reads again
        before = len(reads)
        self.read(self.world.base)
        self.assertGreater(len(reads), before)

    def test_an_objects_links_are_read_once_and_judged_for_each_reader(self) -> None:
        store = self.world.base.store
        plain, narrow_plain = self.read(self.world.base), self.read(self.narrow)
        reads = []
        original = store._links_of_read

        def counted(object_id):
            reads.append(object_id)
            return original(object_id)

        store._links_of_read = counted   # type: ignore[method-assign]
        self.addCleanup(lambda: delattr(store, "_links_of_read"))
        with ot.snapshot_reads():
            first, narrow = self.read(self.world.base), self.read(self.narrow)
            second = self.read(self.world.base)
        self.assertEqual(reads.count(self.customer), 1)
        self.assertEqual((first["object"], second["object"]),
                         (plain["object"], plain["object"]))
        self.assertEqual(narrow["error"], narrow_plain["error"])   # still refused
        self.assertEqual(set(narrow), {"error", "meta"})   # nothing of the links

    def test_a_narrower_reader_never_sees_through_the_kept_answer(self) -> None:
        self.assertIn("error", self.read(self.narrow))
        with ot.snapshot_reads():
            self.assertNotIn("error", self.read(self.world.base))
            narrow = self.read(self.narrow)
            self.assertEqual(narrow["error"]["code"], "object_not_found")
            self.assertNotIn("error", self.read(self.world.base))

    def test_a_whole_margin_reads_as_of_one_moment_of_the_tools_own_clock(self) -> None:
        """Every primitive a margin's plan runs call (plx.object, plx.around, the plan's
        own scope reads) builds its scope from the tools' clock: inside the snapshot all
        of them read the first moment that clock gave, never the wall clock, and the
        answer is the one read without the snapshot."""
        start = dt.datetime(2026, 10, 30, 12, tzinfo=dt.timezone.utc)
        ticks = itertools.count()
        world = World(self, clock=lambda: start + dt.timedelta(seconds=next(ticks)))
        customer = world.customer("C-50")
        store = world.base.store
        seen: list = []
        original = store._visible_versions_read

        def counted(scope, ids):
            seen.append(scope.params()["at"])
            return original(scope, ids)

        store._visible_versions_read = counted   # type: ignore[method-assign]
        self.addCleanup(lambda: delattr(store, "_visible_versions_read"))
        plain = cp.profitability(world.call, customer, today=TODAY)
        self.assertGreater(len(set(seen)), 1)   # outside it each call reads its own now
        seen.clear()
        with ot.snapshot_reads():
            first = next(ticks)
            pinned = cp.profitability(world.call, customer, today=TODAY)
        self.assertTrue(seen)
        moments = set(seen)
        self.assertEqual(len(moments), 1, moments)
        # the tools' own clock, read once (the tick after ``first``), not the wall clock
        self.assertEqual(moments.pop(),
                         ot._format_time(start + dt.timedelta(seconds=first + 1)))
        self.assertEqual(pinned["totals"], plain["totals"])
        self.assertEqual([o["margin"] for o in pinned["orders"]],
                         [o["margin"] for o in plain["orders"]])



class SnapshotGateTests(unittest.TestCase):
    """Inside a snapshot one item gate per reader serves every plx.around call: each
    handle is decided once for that reader, another reader decides its own, and the
    gate is dropped when the snapshot ends."""

    def test_each_reader_decides_each_item_once_and_only_its_own(self) -> None:
        from tests import test_ontology_tools_traverse as tot
        t, _ = tot.tools()
        decided: list[tuple] = []   # the SQL gate's decisions (its per-handle cache misses)
        made = t._gate_function

        def counted(actor):
            check = made(actor)
            seen: set = set()

            def gate(handle):
                if handle not in seen:
                    seen.add(handle)
                    decided.append((actor, handle))
                return check(handle)
            return gate
        t._gate_function = counted   # type: ignore[method-assign]
        args = {"from": tot.O1, "path": [{"link_type": "Document.documents_order",
                                          "direction": "reverse"}]}
        plain = {who: t.call("plx.around", args, who) for who in (tot.OPS, tot.FULL)}
        once = len([1 for a, _h in decided if a == tot.OPS])
        decided.clear()
        with ot.snapshot_reads():
            ops = [t.call("plx.around", args, tot.OPS) for _ in range(3)]
            full = t.call("plx.around", args, tot.FULL)
            gates = [g[2] for g in ot.current_snapshot()["gates"].values()]
            self.assertEqual(len(gates), 2)   # one gate per reader
            gate = lambda name, h: t.store.conn.execute(  # noqa: E731
                f"SELECT {name}(?)", (h,)).fetchone()[0]
            self.assertEqual([gate(g, tot.D2_H) for g in gates], [0, 1])  # ops, full
        self.assertEqual([tot.ids(o) for o in ops], [tot.ids(plain[tot.OPS])] * 3)
        self.assertEqual(tot.ids(ops[0]), [tot.D1])        # the denied item stays hidden
        self.assertEqual(tot.ids(full), [tot.D1, tot.D2])  # never through ops' decisions
        self.assertTrue(once)
        self.assertEqual(len([1 for a, _h in decided if a == tot.OPS]), once)   # not 3x
        self.assertTrue([1 for a, _h in decided if a == tot.FULL])   # its own gate
        for name in gates:   # the snapshot's gates are gone with it
            with self.assertRaises(sqlite3.OperationalError):
                t.store.conn.execute(f"SELECT {name}('graph-mail://x')").fetchone()

class HopReachTests(unittest.TestCase):
    """A hop read once without the item gate and gated in Python keeps exactly the
    rows the SQL gate keeps, in the same order, and says it held something back
    exactly when the reach with every item open is larger."""

    def test_python_gate_keeps_what_the_sql_gate_keeps(self) -> None:
        from tests import test_ontology_tools_traverse as tot
        store = tot.build()
        self.check_alike(store.conn, lambda gate: tot.tools(store, gate=gate)[0])

    def check_alike(self, conn, opened) -> None:
        """Every link type both ways from every object, for three readers and four item
        gates (none denied, alternate halves of every handle, all denied)."""
        from tests import test_ontology_tools_traverse as tot
        handles = sorted({r[0] for r in conn.execute(
            "SELECT l1_handle FROM object_versions UNION SELECT value FROM links, "
            "json_each(links.evidence_handles)") if r[0]})
        self.assertGreater(len(handles), 3)
        held_seen: set = set()
        sizes: set = set()
        shown: set = set()
        pairs = [(lt, d) for (lt,) in conn.execute("SELECT DISTINCT link_type FROM links")
                 for d in ("forward", "reverse")]
        objects = sorted(o for (o,) in conn.execute("SELECT object_id FROM objects"))
        self.assertTrue(pairs)
        for denied in ([], handles[::2], handles[1::2], handles):   # incl. version handles
            gate = (lambda deny: lambda actor, h: "full" in actor.principals
                    and h not in deny)(set(denied))
            t = opened(gate)
            for actor in (tot.FULL, tot.OPS, tot.OUTSIDER):
                with ot.snapshot_reads(), t.store.lock:
                    name = t._snapshot_gate(ot.current_snapshot(), actor)
                    scope = t._scope(actor, {}, name)
                    for lt, d in pairs:
                        sql = t._hop_edges(scope, lt, d, objects)
                        rows, held = t._hop_reach(scope, lt, d, objects)
                        what = (len(denied), actor, lt, d)
                        self.assertEqual([r["link_id"] for r in rows],
                                         [r["link_id"] for r in sql], what)
                        self.assertEqual(held, t._owner_hidden(scope, lt, d, objects,
                                                               len(sql)), what)
                        held_seen.add(held)
                    py_ids = t._visible_ids(scope, objects)
                    py_shown = t._displays(scope, objects)
                    check = t._gate_checks.pop(name)   # the SQL gate's answer
                    try:
                        sql_ids = t._visible_ids(scope, objects)
                        sql_shown = t._displays(scope, objects)
                    finally:
                        t._gate_checks[name] = check
                    self.assertEqual(py_ids, sql_ids, (len(denied), actor))
                    self.assertEqual(py_shown, sql_shown, (len(denied), actor))
                    shown.add(json.dumps(sql_shown, sort_keys=True))
                    sizes.add(len(sql_ids))
        self.assertEqual(held_seen, {True, False})   # both outcomes exercised
        self.assertGreater(len(sizes), 2)            # gates that hid differently
        self.assertGreater(len(shown), 2)            # displays that differ by gate

    def test_on_postgres_too(self) -> None:
        import contextlib, io, sqlite3 as sq, tempfile
        from pathlib import Path
        from operating import ontology_tools_traverse as tt
        from tests import test_ontology_tools_traverse as tot
        try:
            import psycopg
            from operating.pg import ontology_load
            from operating.pg.migrate import MIGRATIONS_DIR
            from operating.tests import pg_testdb
            name, dsn = pg_testdb.create_db()
        except (ImportError, unittest.SkipTest) as exc:
            self.skipTest(f"no Postgres: {exc}")
        self.addCleanup(pg_testdb.drop_db, name)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "ontology.sqlite"
        source = tot.build().conn
        with contextlib.closing(sq.connect(path)) as target:
            source.backup(target)
        with psycopg.connect(dsn, autocommit=True) as pg:
            for migration in sorted(MIGRATIONS_DIR.glob("00*.sql")):
                if migration.name >= "0006":
                    pg.execute(migration.read_text(encoding="utf-8"))
        ontology_load.load(str(path), dsn, workers=1, log=io.StringIO())
        facts = tempfile.TemporaryDirectory()
        self.addCleanup(facts.cleanup)
        from unittest import mock
        import os
        self.enterContext(mock.patch.dict(os.environ, {"PLX_PG_FACTS_DIR": facts.name}))
        from operating.pg.ontology_reader import PgStoreReader

        def opened(gate):
            reader = PgStoreReader.open(dsn, only=True)
            self.addCleanup(reader.conn.close)
            return tt.TraverseTools(ot.OntologyTools(
                reader, tot.POLICY, item_gate=gate, schema=tot.SCHEMA, grammar=None,
                contracts=tot.CONTRACTS, clock=lambda: tot.NOW,
                monotonic=tot.Clock().monotonic))
        self.check_alike(source, opened)

    def test_an_unknown_gate_reads_as_before_and_an_unparsable_list_fails_closed(self
                                                                                 ) -> None:
        from operating import ontology_tools_traverse as tt
        from tests import test_ontology_tools_traverse as tot
        t, _ = tot.tools()
        with t.store.lock:
            t.store.conn.create_function("plx_l9_gate_x", 1, lambda h: 1)
            scope = t._scope(tot.FULL, {}, "plx_l9_gate_x")
            lt = t.store.conn.execute("SELECT link_type FROM links").fetchone()[0]
            self.assertIsNone(t._hop_reach(scope, lt, "forward", [tot.O1])[1])
        self.assertFalse(tt._all_pass(lambda h: 1, "[not json"))
        self.assertFalse(tt._all_pass(t._gate_function(tot.FULL), '[null]'))
        self.assertTrue(tt._all_pass(lambda h: 1, None))
        self.assertFalse(tt._all_pass(lambda h: 1 // 0, '["a://b"]'))


if __name__ == "__main__":
    unittest.main()
