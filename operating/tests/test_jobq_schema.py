"""The job queue database's schema as migrations (operating/jobq/schema.py, DI-09): a
fresh database gets the schema the earlier inline scripts made, a database those scripts
made opens unchanged (its rows kept) and is recorded as current, a second open applies
nothing, and an applied migration edited afterwards is refused as drift. On a scratch
Postgres database (evidence label: isolated-fixture)."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from operating.jobq import events as ev
from operating.jobq import queue as jq
from operating.jobq import schema

try:
    from operating.pg import scratch_db
    import psycopg  # noqa: F401
    scratch_db.use_test_cluster()
    _NAME, _DSN = scratch_db.create_db()
except Exception:  # noqa: BLE001 - no local Postgres: skipped
    _NAME = _DSN = None


def tearDownModule() -> None:
    if _NAME:
        scratch_db.drop_db(_NAME)


# The scripts queue.py and events.py ran on every open before DI-09, verbatim (origin/main
# 810c949bff), pinned here so the migrations are checked against what live databases hold.
INLINE_QUEUE = """
CREATE SCHEMA IF NOT EXISTS jobq;
CREATE TABLE IF NOT EXISTS jobq.jobs (
  id bigserial PRIMARY KEY,
  class text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('unit', 'inline')),
  unit text,
  idem_key text NOT NULL,
  label text,
  priority integer NOT NULL,
  mem_mb integer NOT NULL CHECK (mem_mb >= 0),
  cpus real NOT NULL CHECK (cpus >= 0),
  state text NOT NULL CHECK (state IN ('queued', 'running', 'preempting', 'done', 'failed',
                                       'dead', 'cancelled')),
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL,
  preemptions integer NOT NULL DEFAULT 0,
  run_after timestamptz NOT NULL DEFAULT now(),
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  owner text,
  lease_until timestamptz,
  heartbeat_at timestamptz,
  host text,
  pid integer,
  pid_start bigint,
  pgid integer,
  invocation text,
  exit_code integer,
  result text,
  last_error text,
  refusals integer NOT NULL DEFAULT 0,
  last_refusal text,
  last_refusal_at timestamptz,
  waited_s real
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_queued ON jobq.jobs (idem_key) WHERE state = 'queued';
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_held ON jobq.jobs (idem_key)
  WHERE state IN ('running', 'preempting');
CREATE INDEX IF NOT EXISTS jobs_due ON jobq.jobs (priority, enqueued_at, id) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS jobs_finished ON jobq.jobs (finished_at) WHERE finished_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS jobq.events (
  id bigserial PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  job_id bigint,
  kind text NOT NULL,
  detail text
);
CREATE INDEX IF NOT EXISTS events_at ON jobq.events (at);
"""

INLINE_EVENTS = """
CREATE SCHEMA IF NOT EXISTS jobq;
CREATE TABLE IF NOT EXISTS jobq.event_queue (
  id bigserial PRIMARY KEY,
  topic text NOT NULL,
  event_id text NOT NULL,
  event_type text NOT NULL,
  group_key text NOT NULL,
  seq bigint NOT NULL,
  part integer NOT NULL,
  priority integer NOT NULL,
  body jsonb NOT NULL,
  state text NOT NULL DEFAULT 'ready' CHECK (state IN ('ready', 'inflight', 'done', 'dead')),
  receives integer NOT NULL DEFAULT 0,
  max_receives integer NOT NULL,
  visible_after timestamptz NOT NULL DEFAULT now(),
  consumer text,
  lease_until timestamptz,
  published_at timestamptz NOT NULL DEFAULT now(),
  done_at timestamptz,
  last_error text,
  UNIQUE (topic, event_id),
  UNIQUE (topic, group_key, seq)
);
CREATE INDEX IF NOT EXISTS event_queue_open ON jobq.event_queue (topic, group_key, id)
  WHERE state IN ('ready', 'inflight');
CREATE TABLE IF NOT EXISTS jobq.event_handled (
  topic text NOT NULL,
  handler text NOT NULL,
  event_id text NOT NULL,
  at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (topic, handler, event_id)
);
"""


def catalog(conn) -> dict:
    """Every column, constraint and index of schema jobq but the migration record."""
    rows = lambda q: [tuple(r.values()) if isinstance(r, dict) else tuple(r)
                      for r in conn.execute(q).fetchall()]
    return {
        "columns": rows("SELECT table_name, column_name, data_type, is_nullable, "
                        "column_default FROM information_schema.columns "
                        "WHERE table_schema = 'jobq' AND table_name <> 'migrations' "
                        "ORDER BY 1, 2"),
        "constraints": rows("SELECT conrelid::regclass::text, pg_get_constraintdef(oid) "
                            "FROM pg_constraint WHERE connamespace = 'jobq'::regnamespace "
                            "AND conrelid <> 'jobq.migrations'::regclass ORDER BY 1, 2"),
        "indexes": rows("SELECT tablename, indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname = 'jobq' AND tablename <> 'migrations' "
                        "ORDER BY 1, 2"),
    }


@unittest.skipIf(_DSN is None, "no scratch Postgres")
class JobqMigrations(unittest.TestCase):
    def setUp(self) -> None:
        self.q = jq.Queue(_DSN, environ={})
        self.addCleanup(self.q.close)
        self.q.conn.execute("DROP SCHEMA IF EXISTS jobq CASCADE")

    def inline_catalog(self) -> dict:
        self.q.conn.execute(INLINE_QUEUE)
        self.q.conn.execute(INLINE_EVENTS)
        self.q.conn.execute("CREATE TABLE IF NOT EXISTS jobq.migrations (k int)")
        made = catalog(self.q.conn)
        self.q.conn.execute("DROP SCHEMA jobq CASCADE")
        return made

    def test_a_fresh_database_gets_the_inline_schema_and_a_second_open_applies_nothing(self):
        expected = self.inline_catalog()
        self.q.init()
        self.assertEqual(catalog(self.q.conn), expected)
        self.assertEqual(schema.status(self.q.conn)["applied"], ["0001_jobq", "0002_events"])
        self.assertEqual(schema.apply(self.q.conn), [])
        events = ev.EventQueue(_DSN, topic="t")
        self.addCleanup(events.close)
        events.init()
        self.assertEqual(catalog(self.q.conn), expected)

    def test_a_database_made_by_the_inline_script_opens_unchanged(self):
        expected = self.inline_catalog()
        self.q.conn.execute(INLINE_QUEUE)
        self.q.conn.execute(INLINE_EVENTS)
        self.q.conn.execute(
            "INSERT INTO jobq.jobs (class, kind, idem_key, priority, mem_mb, cpus, state, "
            "max_attempts) VALUES ('c', 'unit', 'k', 1, 0, 0, 'queued', 1)")
        self.q.conn.execute(
            "INSERT INTO jobq.event_handled (topic, handler, event_id) VALUES ('t', 'h', 'e')")
        self.assertEqual(schema.apply(self.q.conn), ["0001_jobq", "0002_events"])
        self.assertEqual(catalog(self.q.conn), expected)
        self.assertEqual(self.q.conn.execute("SELECT idem_key FROM jobq.jobs").fetchone(),
                         {"idem_key": "k"})
        self.assertEqual(self.q.conn.execute(
            "SELECT count(*) AS n FROM jobq.event_handled").fetchone(), {"n": 1})
        status = schema.status(self.q.conn)
        self.assertEqual((status["pending"], status["drift"]), ([], []))
        self.q.init()                                    # the next open: nothing to apply
        self.assertEqual(catalog(self.q.conn), expected)

    def test_an_applied_migration_edited_afterwards_is_refused(self):
        from operating.pg import migrate
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for path in schema.MIGRATIONS_DIR.glob("*.sql"):
            shutil.copy(path, tmp / path.name)
        run = lambda: migrate.migrate(self.q.conn, values={}, directory=tmp,
                                      ledger=schema.LEDGER, lock_key=schema.LOCK_KEY)
        self.assertEqual(run(), ["0001_jobq", "0002_events"])
        edited = tmp / "0002_events.sql"
        edited.write_text(edited.read_text() + "\n-- edited\n")
        with self.assertRaisesRegex(migrate.MigrationError, "migration_drift:0002_events"):
            run()
        self.assertEqual(schema.status(self.q.conn)["drift"], [])   # the shipped files hold

    def test_the_runner_keeps_its_default_ledger_and_parameters(self):
        """The search database's call (no ledger, embedding values) is unchanged, on a
        tuple-row and on a dict-row connection alike."""
        from operating.pg import migrate
        import psycopg
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "0001_t.sql").write_text("CREATE TABLE IF NOT EXISTS public.runner_t "
                                        "(d int DEFAULT {{embed_dim}}, m text DEFAULT {{embed_model}});")
        self.addCleanup(self.q.conn.execute,
                        "DROP TABLE IF EXISTS public.runner_t; DROP SCHEMA IF EXISTS plx_meta CASCADE")
        values = {"embed_model": "m", "embed_dim": 3}
        with psycopg.connect(_DSN, autocommit=True) as plain:
            self.assertEqual(migrate.migrate(plain, values=values, directory=tmp), ["0001_t"])
            self.assertEqual(migrate.status(plain, tmp)["applied"], ["0001_t"])
        self.assertEqual(migrate.migrate(self.q.conn, values=values, directory=tmp), [])
        self.assertEqual(self.q.conn.execute(
            "SELECT version, params FROM plx_meta.migrations").fetchall(),
            [{"version": 1, "params": values}])
        self.assertEqual(sorted(migrate.applied(self.q.conn)), [1])

    def test_the_search_ledger_is_not_touched(self):
        self.q.init()
        self.assertIsNone(self.q.conn.execute(
            "SELECT to_regclass('plx_meta.migrations') AS r").fetchone()["r"])


if __name__ == "__main__":
    unittest.main()
