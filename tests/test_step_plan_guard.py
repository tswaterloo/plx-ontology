"""The step build's reads plan as searches on the served store's shape: every statement the
``post_project`` steps issue on a fixture build is explained against the step input's
indexes (``step_migrate.READ_INDEXES`` with the store's own) under the served store's
link statistics, and none reads ``link_rows`` whole when it constrains it, nor takes a
link id range when it names the links' objects (``mc_task_links.task_leads`` took the
mention prefix's 14M-row range over an object range: 25 minutes on the 11-08 build)."""

from __future__ import annotations

import functools
import re
import sqlite3
import unittest
from unittest import mock

# link_rows as served on 2026-11-08 (Postgres: 25.6M rows; 4 binding kinds, 5 tiers;
# ~8 links per from object and ~20 per to object): sqlite_stat1 rows of the step
# input's link indexes after ``read_indexes`` (whole) and ``analyze_input``
SERVED_LINKS = 25_572_268
SERVED_STATS = {
    "sqlite_autoindex_link_rows_1": f"{SERVED_LINKS} 1",
    "links_from": f"{SERVED_LINKS} 8 3",
    "links_to": f"{SERVED_LINKS} 20 6",
    "link_rows_by_kind_tier_from": f"{SERVED_LINKS} 6393067 1278613 4",
    "link_rows_by_id_tier_from": f"{SERVED_LINKS} 1 1 1",
}
_OBJECT_COLUMNS = re.compile(r"\b(from_object|to_object)\b")
_LINK_TABLE = re.compile(r"\b(link_rows|links)\b")
# A step clearing its own links by their id's head reads the links once: measured on
# 2.5M links, 0.1 s per million (warm) against 6 s per million to index each head
# length, so that scan is the cheaper plan and stays allowed.
_HEAD_CLEAR = re.compile(r"DELETE FROM links WHERE substr\(link_id, 1, \d+\) = '[^']*'$")


# the modules whose worker processes read the step input (their own queries)
WORKER_MODULES = ("ontology.finish_parallel", "ontology.finish_documents",
                  "ontology.mention_firm_parallel", "ontology.sender_parallel",
                  "ontology.step_migrate", "ontology.project_parallel")


def _literal_sql(node, names) -> str | None:
    """The text of a string literal, or of an f-string whose values are names of the
    module's string constants; None otherwise."""
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name) \
                    and isinstance(names.get(part.value.id), str):
                out.append(names[part.value.id])
            else:
                return None
        return "".join(out)
    return None


def explained(conn: sqlite3.Connection, sql: str) -> list[str]:
    params = [None] * sql.count("?")   # the plan does not read the values
    return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def misplans(conn: sqlite3.Connection, sql: str) -> list[str]:
    """What is wrong with ``sql``'s plan over ``link_rows`` (nothing: [])."""
    if not _LINK_TABLE.search(sql) or " WHERE " not in sql.upper() or _HEAD_CLEAR.match(sql):
        return []
    wrong = []
    for line in explained(conn, sql):
        if re.match(r"SCAN (link_rows|l)\b(?! USING COVERING INDEX link_rows_by_id)", line):
            wrong.append(f"full scan: {line}")
        if "sqlite_autoindex_link_rows_1 (link_id>? AND link_id<?)" in line and \
                _OBJECT_COLUMNS.search(sql.split(" WHERE ", 1)[1]):
            wrong.append(f"link id range over an object one: {line}")
    return wrong


def served_shape(conn: sqlite3.Connection) -> None:
    from ontology.step_migrate import analyze_input, read_indexes
    read_indexes(conn)
    analyze_input(conn)
    conn.execute("DELETE FROM sqlite_stat1 WHERE tbl = 'link_rows'")
    conn.executemany("INSERT INTO sqlite_stat1 VALUES ('link_rows', ?, ?)",
                     SERVED_STATS.items())
    conn.commit()
    conn.execute("ANALYZE sqlite_master")


class StepPlanGuardTests(unittest.TestCase):

    def test_task_leads_take_the_object_range_not_the_mention_range(self):
        from ontology.store import StoreBuilder
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            b = StoreBuilder(Path(tmp) / "s.sqlite", foreign_keys=False)
            try:
                conn = b.connection
                served_shape(conn)
                sql = ("SELECT * FROM links WHERE from_object >= 'MCTask:' AND from_object < "
                       "'MCTask;' AND binding_kind = 'mention' AND tier = 'lead' AND "
                       "superseded_at IS NULL AND link_id >= 'mention:' AND link_id < "
                       "'mention;'")
                self.assertEqual(misplans(conn, sql), [], explained(conn, sql))
                lead_objects = ("SELECT DISTINCT from_object FROM link_rows WHERE link_id >= "
                                "'mention:' AND link_id < 'mention;' AND tier = 'lead'")
                self.assertIn("COVERING INDEX", " ".join(explained(conn, lead_objects)))
            finally:
                b.abort()

    def test_the_worker_processes_statements_plan_as_searches(self):
        """The statements the steps' worker processes issue (read from their modules: a
        literal or an f-string whose parts are the module's own string constants) plan
        as searches on the step input under the served shape."""
        import ast
        import importlib
        import tempfile
        from pathlib import Path
        from ontology.store import StoreBuilder
        found: list[str] = []
        for module in WORKER_MODULES:
            mod = importlib.import_module(module)
            for node in ast.walk(ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))):
                sql = _literal_sql(node, vars(mod))
                if sql and re.match(r"(?is)\s*(SELECT|WITH)\b", sql) and _LINK_TABLE.search(sql):
                    found.append(" ".join(sql.split()))
        self.assertGreater(len(found), 5, found)
        with tempfile.TemporaryDirectory() as tmp:
            b = StoreBuilder(Path(tmp) / "s.sqlite", foreign_keys=False)
            try:
                served_shape(b.connection)
                wrong, planned = {}, 0
                for sql in found:
                    try:
                        bad = misplans(b.connection, sql)
                        planned += 1
                    except sqlite3.Error:
                        continue   # a table of the worker's own file
                    if bad:
                        wrong[sql[:300]] = bad
            finally:
                b.abort()
        self.assertGreater(planned, 3, found)
        self.maxDiff = None
        self.assertEqual(wrong, {})

    def test_every_step_statement_plans_as_a_search(self):
        """Every statement the steps issue in a fixture build (the build's own process)."""
        from operating import build_steps
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        seen: dict[str, None] = {}
        original = build_steps.post_project

        def traced(step):
            @functools.wraps(step)
            def run(builder, projector):
                builder.connection.set_trace_callback(lambda s: seen.setdefault(s.strip()))
                try:
                    return step(builder, projector)
                finally:
                    builder.connection.set_trace_callback(None)
            return run

        def post_project(*a, **k):
            return {n: traced(s) for n, s in original(*a, **k).items()}

        fixture = DeltaEquivalenceTests(
            "test_changed_records_equal_a_full_build_and_nothing_else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        with mock.patch.object(build_steps, "post_project", post_project):
            store = fixture.full("plan.sqlite")
        reads = [s for s in seen if s.upper().startswith(("SELECT", "WITH", "DELETE", "UPDATE"))
                 and _LINK_TABLE.search(s)]
        self.assertGreater(len(reads), 10)
        conn = sqlite3.connect(store)
        try:
            served_shape(conn)
            wrong = {}
            for sql in reads:
                try:
                    found = misplans(conn, sql)
                except sqlite3.Error:
                    continue   # names a temporary table of the step's own
                if found:
                    wrong[sql[:300]] = found
        finally:
            conn.close()
        self.maxDiff = None
        self.assertEqual(wrong, {})


class StepReadsDeclaredTests(unittest.TestCase):
    """Every step reads only links of the steps it declares (``step_migrate.step_waits_for``):
    each statement a step issues in a build of every step (values written in) is mapped
    to the steps whose links it names, by link type (the built store's links) and link id
    prefix. A step reading another's links undeclared would run beside it and miss them
    (unique_mentions read the party twins). A statement naming no type or prefix cannot
    be told: the build runs that step one at a time (``undetermined`` is allowed here)."""

    def _check(self, build) -> dict[str, list[str]]:
        from operating import build_steps
        from ontology import step_migrate as sm
        from ontology.schema import load_schema
        seen: dict[str, list[str]] = {}
        original = build_steps.post_project

        def traced(name, step):
            @functools.wraps(step)
            def run(builder, projector):
                builder.connection.set_trace_callback(seen.setdefault(name, []).append)
                try:
                    return step(builder, projector)
                finally:
                    builder.connection.set_trace_callback(None)
            return run
        store = build(lambda *a, **k: {n: traced(n, s) for n, s in original(*a, **k).items()})
        conn = sqlite3.connect(store)
        try:
            type_steps: dict[str, set[str]] = {}
            for link_id, link_type in conn.execute("SELECT link_id, link_type FROM links"):
                owner = sm.step_of(link_id)
                if owner is not None:
                    type_steps.setdefault(link_type, set()).add(owner)
            stepped = set().union(*type_steps.values()) if type_steps else set()
        finally:
            conn.close()
        schema = load_schema()
        for name, spec in sm.STEPS.items():
            for t in (spec.writes(schema) if spec.writes is not None else ()):
                type_steps.setdefault(t, set()).add(name)
        wrong = {name: sorted(sm.undeclared_reads(name, statements, type_steps, sm.STEPS)
                              - {"undetermined"})
                 for name, statements in seen.items()
                 if name in sm.STEPS and sm.side_by_side(sm.STEPS[name])}   # others: alone
        self.type_steps = type_steps
        return {k: v for k, v in wrong.items() if v}, stepped, seen

    def test_party_twins_and_every_step_read_only_declared_links(self):
        import json as _json
        import tempfile
        from pathlib import Path
        from ontology import identifiers
        from ontology.project import build_store
        from ontology.schema import load_schema
        from ontology.tests import test_party_twins as P

        with tempfile.TemporaryDirectory() as tmp:
            def build(post_project):
                path = Path(tmp) / "twins.sqlite"
                rows = _json.loads(_json.dumps(P.ROWS))
                build_store(path, load_schema(), {n: P.Source(n, r) for n, r in rows.items()},
                            normalizer=identifiers.normalize, clock=lambda: P.NOW,
                            post_project=post_project(Path(tmp) / "state", "plx"))
                return path
            wrong, stepped, seen = self._check(build)
        self.assertIn("twins_parties", stepped)   # the fixture holds party twins
        self.assertIn("unique_mentions", seen)
        self.assertEqual(wrong, {})
        # the fixture's mails name no reused order number, so unique_mentions' resolver
        # (po_mention_resolve) issues nothing here: its party twin read as the build
        # issues it, under the declaration it had and the one it has
        import dataclasses
        from ontology import step_migrate as sm
        read = ("SELECT from_object, to_object, link_type FROM link_rows WHERE link_type IN "
                "('Supplier.same_record_as', 'Customer.same_record_as') AND tier IN "
                "('verified', 'reviewed', 'corroborated') AND superseded_at IS NULL")
        before = {**sm.STEPS, "unique_mentions": dataclasses.replace(
            sm.STEPS["unique_mentions"], reads=("mentions",))}
        self.assertEqual(sm.undeclared_reads("unique_mentions", [read], self.type_steps,
                                             before), {"twins_parties"})
        self.assertEqual(sm.undeclared_reads("unique_mentions", [read], self.type_steps,
                                             sm.STEPS), set())

    def test_the_portal_fixture_steps_read_only_declared_links(self):
        from operating import build_steps
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        fixture = DeltaEquivalenceTests(
            "test_changed_records_equal_a_full_build_and_nothing_else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)

        def build(post_project):
            with mock.patch.object(build_steps, "post_project", post_project):
                return fixture.full("reads.sqlite")
        wrong, _stepped, seen = self._check(build)
        self.assertGreater(len(seen), 10)
        self.assertEqual(wrong, {})


class SideBySideFallbackTests(unittest.TestCase):
    """A forked step that reads an undeclared step's links, or outlives its time, is run
    again in the build's process (its process alone ended); one that keeps to its
    declared reads hands back its part."""

    def test_undeclared_read_and_timeout_run_here(self):
        import tempfile
        import time as _time
        from pathlib import Path
        from ontology import step_migrate as sm
        from ontology.schema import load_schema
        from ontology.store import StoreBuilder
        link = {"link_id": "aa:1", "link_type": "X.t", "from_object": "A:1",
                "to_object": "B:1", "tier": "lead", "binding_kind": "mention",
                "link_contract": None, "reason": "[{\"r\": 1}]", "why_not": "[]",
                "evidence_handles": "[\"h\"]", "reviewed_by": None, "reviewed_at": None,
                "review_action_id": None, "markings": "[]",
                "recorded_at": "2026-11-01T00:00:00Z", "superseded_at": None}
        specs = {n: sm.Spec((f"{n}:",), ()) for n in ("aa", "bb", "cc", "dd")}

        def reads_aa(builder, projector):
            builder.connection.execute("SELECT * FROM links WHERE link_type = 'X.t'").fetchall()
            return {}

        def slow(builder, projector):
            _time.sleep(30)
            return {}

        def writes(builder, projector):
            builder.connection.execute("DELETE FROM link_rows WHERE link_id >= 'dd:' AND "
                                       "link_id < 'dd;'")
            return {"links": builder.add("links", [{**link, "link_id": "dd:1"}])}
        with tempfile.TemporaryDirectory() as tmp:
            b = StoreBuilder(Path(tmp) / "in.sqlite", foreign_keys=False)
            b.add("links", [link])
            store = b.keep_as_part()
            started = _time.monotonic()
            done = sm.run_side_by_side(["bb", "cc", "dd"], {"bb": reads_aa, "cc": slow,
                                                            "dd": writes},
                                       load_schema(), {}, None, str(store), Path(tmp), {},
                                       specs, {"X.t": {"aa"}}, timeout=3)
            self.assertLess(_time.monotonic() - started, 20)
            self.assertEqual(done["bb"], ("here", "undeclared_read:aa"))
            self.assertEqual(done["cc"], ("here", "timeout"))
            self.assertEqual((done["dd"][0], done["dd"][1]), ("ok", {"links": 1}))


if __name__ == "__main__":
    unittest.main()
