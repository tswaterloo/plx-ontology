"""Background work's Postgres backends run below the API's (deploy/ops/pg_priority.py), the
API's warm-up logs in under its own name (operator_api.warm_process.warm_app_name), and the
deploy's room check still counts the warm-up's sessions as the API's (plx-release pg_room's
statement, run on a throwaway Postgres). Evidence label: ``isolated-fixture``."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pg_priority", ROOT / "deploy/ops/pg_priority.py")
pp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pp)


class Nices:
    def __init__(self, start):
        self.now = dict(start)
        self.set_calls = []

    def get(self, which, pid):
        if pid not in self.now:
            raise ProcessLookupError(pid)
        return self.now[pid]

    def set(self, which, pid, value):
        if pid == 99:
            raise PermissionError(pid)
        self.set_calls.append((pid, value))
        self.now[pid] = value


class PriorityTests(unittest.TestCase):
    def test_only_named_background_units_and_the_api_warm_up_are_lowered(self):
        for app in ("plx-delta", "plx-proactive-inc", "plx-search-follow", "plx-api-warm",
                    "plx-api@standby-warm", "plx-business-views", "plx-link-marks@x"):
            self.assertEqual(pp.target(app, 10), 10, app)
        # never lowered: the API's own requests, a new or unlisted unit (a serving one may
        # be added any day), autopilots, the default reader names, anything else
        for app in ("plx-api", "plx-api@standby", "plx-app", "plx-new-serving-unit",
                    "plx-autopilot-follow", "plx-reply-drafts", "plx-reader", "plx-workbench",
                    "plx-delta-x", "other-warm", "plx-app-warm", "psql", "", "walsender",
                    None, "PLX-DELTA", "plxdelta"):
            self.assertIsNone(pp.target(app, 10), app)

    def test_every_listed_unit_exists_in_deploy(self):
        units = {p.stem.rstrip("@") for p in (ROOT / "deploy/systemd").glob("*.service")}
        self.assertEqual(sorted(pp.BACKGROUND_UNITS - units), [])
        self.assertNotIn("plx-api", pp.BACKGROUND_UNITS)

    def test_only_background_backends_are_lowered_never_raised_and_gone_ones_skipped(self):
        rows = pp.parse("11|plx-api|client backend\n12|plx-delta|client backend\n"
                        "13|plx-delta|parallel worker\n14|plx-api-warm|client backend\n"
                        "15|psql|client backend\n16|plx-delta|client backend\n"
                        "17|plx-proactive-inc|client backend\n99|plx-delta|client backend\n"
                        "garbage\n|x|y\n")
        nices = Nices({11: 0, 12: 0, 13: 0, 14: 0, 15: 0, 17: 15, 99: 0})   # 16 ended
        got = pp.apply(rows, 10, nices.get, nices.set)
        self.assertEqual(sorted(nices.set_calls), [(12, 10), (13, 10), (14, 10)])
        self.assertEqual((nices.now[11], nices.now[15], nices.now[17]), (0, 0, 15))
        self.assertEqual(got, {"lowered": 3, "kept": 1, "left": 2, "gone": 2})
        # cardinality: 1, 10 and 60 background backends, each lowered once, none twice
        for n in (1, 10, 60):
            rows = [(1000 + i, "plx-delta") for i in range(n)]
            nices = Nices({1000 + i: 0 for i in range(n)})
            pp.apply(rows, 10, nices.get, nices.set)
            pp.apply(rows, 10, nices.get, nices.set)
            self.assertEqual(len(nices.set_calls), n)

    def test_out_of_range_settings_are_refused(self):
        self.assertEqual(pp.main(["--once", "--background-nice", "0"]), 2)
        self.assertEqual(pp.main(["--once", "--interval", "0"]), 2)

    def test_the_warm_up_logs_in_under_the_apis_name_with_warm(self):
        from operator_api.warm_process import warm_app_name
        self.assertEqual(warm_app_name("plx-api"), "plx-api-warm")
        self.assertEqual(warm_app_name("plx-api@standby"), "plx-api@standby-warm")
        self.assertEqual(warm_app_name("plx-api-warm"), "plx-api-warm")
        self.assertEqual(warm_app_name(None), "plx-workbench-warm")
        self.assertLessEqual(len(warm_app_name("x" * 80)), 63)
        self.assertEqual(pp.target(warm_app_name("plx-api@standby"), 10), 10)


class PgRoomCountsTheWarmUp(unittest.TestCase):
    """plx-release pg_room's own statement: the warm-up's sessions are the API's (an idle
    one is not counted at work), so naming them never refuses a warm switch."""

    def test_the_room_statement_counts_the_warm_ups_sessions_as_the_apis(self):
        import psycopg
        from operating.tests import pg_testdb
        name, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        text = (ROOT / "deploy/release/plx-release").read_text()
        sql = re.search(r"<<'SQL' 2>/dev/null\n(.*?)\nSQL\n", text[text.index("pg_room()"):],
                        re.S).group(1)
        with psycopg.connect(dsn, application_name="plx-api") as a, \
                psycopg.connect(dsn, application_name="plx-api-warm") as w, \
                psycopg.connect(dsn, application_name="plx-api-warm") as busy, \
                psycopg.connect(dsn, application_name="plx-delta") as other:
            busy.execute("SELECT 1")   # left in a transaction: at work
            role = a.execute("SELECT current_user").fetchone()[0]
            a.commit()   # idle again
            out = subprocess.run(["psql", dsn, "-XAtq", "-v", "ON_ERROR_STOP=1", "-v",
                                  f"role={role}", "-v", "app=plx-api"], input=sql,
                                 capture_output=True, text=True, check=True).stdout.strip()
            limit, used, api, active = (int(x) for x in out.split("|"))
            self.assertEqual(api, 3)          # plx-api and both warm-up sessions
            self.assertEqual(active, 1)       # only the one in a transaction
            self.assertGreaterEqual(used, 4)
            del limit, w, other


if __name__ == "__main__":
    unittest.main()
