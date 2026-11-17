"""The per-system layer check (CAP-L220) on a scratch Postgres database: only current
records are sampled and only current firm links count as firm, a layer no sampled record
reaches is a gap, an unreadable or undecided layer is unknown (never 0) and fails the
check, an unknown system is refused, and the sample neither loses nor repeats records."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import unittest.mock

from tools import system_layers as sl

try:
    from operating.pg import scratch_db
    import psycopg
    scratch_db.use_test_cluster()
    _NAME, _DSN = scratch_db.create_db()
except Exception:  # noqa: BLE001 - no local Postgres: skipped
    _NAME = _DSN = None


def tearDownModule() -> None:
    if _NAME:
        scratch_db.drop_db(_NAME)


TEAMS, PLANNER = "gen.teams.chat_messages", "meetings.planner_tasks"


def oid(object_type: str, binding: str, key: str) -> str:
    return f'{object_type}:{binding}:{{"id":"{key}"}}'


M1, M2, M3, GONE, DELETED, BACK = (oid("TeamsChatMessage", TEAMS, k)
                                  for k in ("m1", "m2", "m3", "gone", "deleted", "back"))
P1, P2 = (oid("PlannerTask", PLANNER, k) for k in ("p1", "p2"))
CUST = oid("Customer", "portal.app_customer", "c1")


@unittest.skipIf(_DSN is None, "no scratch Postgres")
class SystemLayersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pg = psycopg.connect(_DSN, autocommit=True)
        self.pg.execute(
            "DROP SCHEMA IF EXISTS ontology CASCADE; DROP SCHEMA IF EXISTS search CASCADE;"
            "CREATE SCHEMA ontology; CREATE SCHEMA search;"
            "CREATE TABLE ontology.objects (object_id text PRIMARY KEY, object_type text, "
            "binding_id text, audience_key text, missing_from_source_since text);"
            "CREATE TABLE ontology.object_removals (object_id text, basis text, since text, "
            "until text);"
            "CREATE TABLE ontology.link_rows (link_id text PRIMARY KEY, from_object text, "
            "to_object text, tier text, superseded_at text);"
            "CREATE TABLE search.chunks (chunk_id bigint PRIMARY KEY, object_id text)")
        objects = [(M1, "TeamsChatMessage", TEAMS, "teams:chat:a", None),
                   (M2, "TeamsChatMessage", TEAMS, "teams:chat:a", None),
                   (M3, "TeamsChatMessage", TEAMS, "teams:chat:a", None),
                   (GONE, "TeamsChatMessage", TEAMS, "teams:chat:a", "2026-11-01"),
                   (DELETED, "TeamsChatMessage", TEAMS, "teams:chat:a", None),
                   (BACK, "TeamsChatMessage", TEAMS, "teams:chat:a", None),
                   (P1, "PlannerTask", PLANNER, "meetings:plan:x", None),
                   (P2, "PlannerTask", PLANNER, "", None),
                   (CUST, "Customer", "portal.app_customer", "portal", None)]
        links = [("l1", M1, CUST, "verified", None),
                 ("l2", CUST, M2, "lead", None),
                 ("l3", M3, CUST, "verified", "2026-11-02"),   # superseded: not current
                 ("l4", M3, CUST, "contradicted", None),       # current, never firm
                 ("l5", GONE, CUST, "verified", None),         # its record is gone
                 ("l6", DELETED, CUST, "verified", None)]      # soft-deleted at source
        with self.pg.cursor() as cur:
            cur.executemany("INSERT INTO ontology.objects VALUES (%s, %s, %s, %s, %s)", objects)
            cur.executemany("INSERT INTO ontology.link_rows VALUES (%s, %s, %s, %s, %s)", links)
            cur.execute("INSERT INTO search.chunks VALUES (1, %s), (2, %s)", (M1, CUST))
            cur.executemany("INSERT INTO ontology.object_removals VALUES (%s, %s, %s, %s)",
                            [(DELETED, "source_soft_delete", "2026-08-27T20:33:21Z", None),
                             (BACK, "missing_from_source", "2026-10-01T00:00:00Z",
                              "2026-10-02T00:00:00Z")])   # ended: held again
        self.work = Path(tempfile.mkdtemp())
        self.inbox = self.work / "inbox.sqlite"
        with contextlib.closing(sqlite3.connect(self.inbox)) as db, db:
            db.executescript(
                "CREATE TABLE gaps (gap_id TEXT PRIMARY KEY, object_id TEXT, state TEXT);"
                "CREATE TABLE gap_cites (gap_id TEXT, object_id TEXT);")
            db.executemany("INSERT INTO gaps VALUES (?, ?, ?)",
                           [("g1", CUST, "open"), ("g2", CUST, "resolved")])
            db.executemany("INSERT INTO gap_cites VALUES (?, ?)",
                           [("g1", P1), ("g1", P2), ("g2", None)])

    def tearDown(self) -> None:
        self.pg.close()

    def rows(self, report):
        return {(r["binding"], r["type"]): r for rs in report["systems"].values() for r in rs}

    def gaps(self, report, key="gaps"):
        return {(g["system"], g["type"], g["layer"]) for g in report[key]}

    def test_layers_per_system_and_gaps(self) -> None:
        report = sl.check(self.pg, inbox=self.inbox, systems=["teams", "meetings"])
        self.assertEqual(set(report["systems"]), {"teams", "meetings"})
        teams = self.rows(report)[(TEAMS, "TeamsChatMessage")]
        # the gone and the soft-deleted records are neither current nor sampled (the one
        # whose removal ended is); only m1's current verified link is firm (m3's verified
        # link is superseded, its contradicted one never firm)
        self.assertEqual((teams["current"], teams["sampled"], teams["firm"], teams["lead"],
                          teams["search"], teams["readable"]), (4, 4, 1, 1, 1, 4))
        self.assertEqual(teams["alerts"], {"open": 0, "resolved": 0})
        planner = self.rows(report)[(PLANNER, "PlannerTask")]
        # one open gap cites both tasks: counted once for the binding
        self.assertEqual(planner["alerts"], {"open": 1, "resolved": 0})
        self.assertEqual(planner["readable"], 1)
        self.assertEqual(self.gaps(report), {
            ("teams", None, "alerts"),   # no record of the system ever alerted
            ("meetings", "PlannerTask", "firm"), ("meetings", "PlannerTask", "lead"),
            ("meetings", "PlannerTask", "search")})
        self.assertEqual(report["unknown"], [])

    def test_unreadable_layers_are_unknown_and_fail(self) -> None:
        self.pg.execute("DROP SCHEMA search CASCADE")
        report = sl.check(self.pg, inbox=self.work / "absent.sqlite", systems=["teams"])
        teams = self.rows(report)[(TEAMS, "TeamsChatMessage")]
        self.assertIsNone(teams["search"])
        self.assertIsNone(teams["alerts"])
        self.assertEqual(self.gaps(report, "unknown"),
                         {("teams", "TeamsChatMessage", "search"), ("teams", None, "alerts")})
        self.assertNotIn("search", {g["layer"] for g in report["gaps"]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sl.main(["--dsn", _DSN, "--system", "teams", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(len(json.loads(out.getvalue())["unknown"]), 2)

    def test_hub_past_the_probe_cap_is_undecided_not_a_gap(self) -> None:
        with self.pg.cursor() as cur:
            cur.executemany("INSERT INTO ontology.link_rows VALUES (%s, %s, %s, %s, NULL)",
                            [(f"h{i}", CUST, P1, "contradicted") for i in range(3)]
                            + [("h-lead", CUST, P1, "lead")])
        with unittest.mock.patch.object(sl, "PROBE_CAP", 3):
            report = sl.check(self.pg, inbox=self.inbox, systems=["meetings"])
        planner = self.rows(report)[(PLANNER, "PlannerTask")]
        self.assertIsNone(planner["firm"])   # P1's probe stopped at the cap: undecided
        self.assertIn(("meetings", "PlannerTask", "firm"), self.gaps(report, "unknown"))
        self.assertNotIn(("meetings", "PlannerTask", "firm"), self.gaps(report))
        with unittest.mock.patch.object(sl, "PROBE_CAP", 5):
            planner = self.rows(sl.check(self.pg, inbox=self.inbox, systems=["meetings"]))[
                (PLANNER, "PlannerTask")]
        self.assertEqual((planner["firm"], planner["lead"]), (0, 1))

    def test_unknown_system_refused(self) -> None:
        with self.assertRaises(ValueError):
            sl.check(self.pg, inbox=self.inbox, systems=["teams", "no_such_system"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(sl.main(["--dsn", _DSN, "--system", "no_such_system"]), 2)
        with self.assertRaises(ValueError):
            sl.check(self.pg, n=0)

    def test_sample_cardinality(self) -> None:
        with self.pg.cursor() as cur:
            cur.executemany("INSERT INTO ontology.objects VALUES (%s, 'MCTask', "
                            "'mission_control.tasks', 'mc:project:1', NULL)",
                            [(oid("MCTask", "mission_control.tasks", f"t{i}"),) for i in range(60)])
            cur.executemany("INSERT INTO ontology.link_rows VALUES (%s, %s, %s, 'verified', NULL)",
                            [(f"t{i}", oid("MCTask", "mission_control.tasks", f"t{i}"), CUST)
                             for i in range(60)])
        for n, want in ((1, 1), (10, 10), (60, 60), (500, 60)):
            mc = self.rows(sl.check(self.pg, systems=["mission_control"], n=n))[
                ("mission_control.tasks", "MCTask")]
            self.assertEqual((mc["current"], mc["sampled"], mc["firm"]), (60, want, want))


if __name__ == "__main__":
    unittest.main()
