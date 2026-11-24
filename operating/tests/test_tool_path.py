"""The Ask's tool path (no cut-offs): no refusal rule and no
statement or call time limit on the Ask's reads; ``describe`` of a comms view never samples it
live; the view docs carry no system verdict. Fakes only: no database."""
import unittest

from operating import ontology_tools_sql as sql


class EveryRow(unittest.TestCase):
    """No row cap. A result's every row is kept (memory up to HELD_ROWS,
    then disk), read in order, paged and dropped with its result."""

    def test_a_result_past_the_held_rows_is_kept_whole_on_disk_and_read_in_pages(self):
        from operating.result_rows import RowStore
        from operating.ontology_tools_sql_rows import _shown
        for n in (1, 10, 60, 4000):    # N = 1/10/60: no row lost or doubled; 4000 spills
            store = RowStore(held=50)
            store.extend((i, f"p{i}") for i in range(n))
            self.assertEqual(len(store), n)
            self.assertEqual(store.spilled(), n > 50)
            self.assertEqual([r[0] for r in store], list(range(n)))
            self.assertEqual(store[n - 1], [n - 1, f"p{n - 1}"])
            shown, cut, _ = _shown("sql://x", ["a", "b"], store, 0)
            self.assertEqual(shown[0]["cite"], "sql://x#row=0")
            self.assertEqual(shown[-1]["cite"] == f"sql://x#row={n - 1}", n <= 200 and not cut)
            # the next page starts where the first stopped
            if cut or n > 200:
                more, _, _ = _shown("sql://x", ["a", "b"], store, len(shown))
                self.assertEqual(more[0]["cite"], f"sql://x#row={len(shown)}")
            store.close()

    def test_the_file_goes_with_the_result_and_the_cap_constants_are_gone(self):
        import os
        from operating.result_rows import RowStore
        from operating.ontology_tools_sql_rows import _Results
        for name in ("MAX_ROWS", "FETCH_BYTES"):
            self.assertFalse(hasattr(sql, name), name)
        results = _Results(most=1)
        big = RowStore(held=2)
        big.extend([i] for i in range(10))
        path = big._path
        self.assertTrue(os.path.exists(path))
        results.put("a", {"rows": big})
        results.put("b", {"rows": []})       # "a" is the oldest past ``most``: dropped
        self.assertIsNone(results.get("a"))
        self.assertFalse(os.path.exists(path))


class NoCutOffs(unittest.TestCase):
    """Ask gets every tool with no limits: a statement is never refused for
    what it would read and has no statement time limit; the tool-path constants of the old
    refusal, deadline and budgets are gone."""

    def test_no_refusal_rule_and_no_read_budget_remains_in_the_tool_path(self):
        for name in ("COMMS_SCAN_COST", "COMMS_TABLES", "_comms_scan", "_scan_message",
                     "_bounded", "BOUND_DAYS", "STATEMENT_MS", "FULL_STATEMENT_MS",
                     "SEARCH_FULL_MS", "CANDIDATE_MS", "STEM_MS", "COUNT_MS", "ATTACH_MS",
                     "FULL_COSTLY_WAIT_SECONDS", "_costly_until"):
            self.assertFalse(hasattr(sql, name), name)

    def test_a_statement_has_no_time_limit(self):
        tool = sql.SqlTool.__new__(sql.SqlTool)
        self.assertEqual(tool._seconds(), 0.0)          # statement_timeout 0: none
        self.assertEqual(tool._seconds(True), 0.0)

    def test_the_asks_evidence_reads_are_never_cut_by_a_tool_call_limit(self):
        from operating import call_limits
        on = {call_limits.LIMITS_ENV: "on"}
        for name in ("plx.sql", "plx.search", "plx.open", "plx.sql_comms_search",
                     "plx.sql_comms_open", "plx.sql_records_search", "plx.legs",
                     "plx.read_document", "plx.insight", "plx.prepare_write"):
            self.assertIsNone(call_limits.limit_for(name, on), name)
        self.assertIsNotNone(call_limits.limit_for("plx.investigate", on))   # others keep theirs

    def test_a_costly_statement_queues_for_a_slot_and_is_never_refused_for_waiting(self):
        import inspect
        self.assertNotIn("busy", inspect.getsource(sql.SqlTool._query)
                         if hasattr(sql.SqlTool, "_query") else "")


class StandbyRoute(unittest.TestCase):
    """Ask's reads run on the standby when it answers as a replica; no
    lag threshold refuses or cuts; the primary (said, with why) when it cannot be used."""

    def setUp(self):
        from operating import standby_route
        self.route = standby_route
        standby_route.reset()
        self.addCleanup(standby_route.reset)

    def test_the_standby_when_it_is_a_replica_else_the_primary_with_the_reason(self):
        env = {self.route.DSN_ENV: "postgresql://standby/plx"}
        self.assertEqual(self.route.route("postgresql:///plx", env, probe=lambda d: None),
                         ("postgresql://standby/plx", None))
        self.route.reset()
        got = self.route.route("postgresql:///plx", env,
                               probe=lambda d: "the standby could not be reached (OSError)")
        self.assertEqual(got, ("postgresql:///plx",
                               "the standby could not be reached (OSError)"))
        # no standby set: the primary, said
        dsn, why = self.route.route("postgresql:///plx", {}, probe=lambda d: None)
        self.assertEqual(dsn, "postgresql:///plx")
        self.assertIn("no standby", why)

    def test_there_is_no_lag_threshold_in_the_route(self):
        import inspect
        src = inspect.getsource(self.route)
        for word in ("lag", "behind", "STALE", "MAX_BEHIND"):
            self.assertNotIn(word, src.replace("no lag threshold", ""), word)

    def test_a_standby_that_fails_to_connect_is_the_primary_until_the_next_probe(self):
        env = {self.route.DSN_ENV: "postgresql://standby/plx"}
        self.route.failed("postgresql://standby/plx", "OperationalError")
        dsn, why = self.route.route("postgresql:///plx", env, probe=lambda d: None)
        self.assertEqual(dsn, "postgresql:///plx")
        self.assertIn("OperationalError", why)

    def test_the_result_says_where_it_was_read_and_as_of_when(self):
        import datetime as dt

        class Pg:
            def __init__(self, recovering):
                self.recovering = recovering

            def execute(self, text):
                at = dt.datetime(2026, 11, 23, 22, 30, 5, tzinfo=dt.timezone.utc)
                now = dt.datetime(2026, 11, 23, 22, 31, 0, tzinfo=dt.timezone.utc)

                class R:
                    def fetchone(_):
                        return (self.recovering, at if self.recovering else None, now)
                return R()
        sql._ROUTE.last = ("standby", None)
        self.assertEqual(sql.data_as_of(Pg(True)),
                         {"read_from": "standby", "as_of": "2026-11-23T22:30:05+00:00"})
        sql._ROUTE.last = ("primary", "the standby could not be reached (OSError)")
        got = sql.data_as_of(Pg(False))
        self.assertEqual((got["read_from"], got["as_of"]), ("primary", "2026-11-23T22:31:00+00:00"))
        self.assertIn("the standby could not be reached", got["note"])

    def test_a_call_waits_for_a_connection_however_long_never_refused(self):
        import inspect
        self.assertNotIn("PoolTimeout", inspect.getsource(sql._lent))
        self.assertIn("timeout=None", inspect.getsource(sql._pool))


class Describe(unittest.TestCase):
    def test_a_missing_or_ungranted_column_health_is_said_never_a_live_scan(self):
        class Pg:
            def transaction(self):
                raise PermissionError("denied")
        got = sql.SqlTool._health(Pg(), "emails")
        self.assertEqual(got["columns"], [])
        self.assertIn("not been measured", got["note"])

    def test_what_the_refresh_measured_is_what_describe_says(self):
        import contextlib
        import datetime as dt

        class Pg:
            @contextlib.contextmanager
            def transaction(self):
                yield

            def execute(self, text, params):
                assert params == ("emails",)

                class R:
                    def fetchall(_):
                        return [("subject", 0.9876543, 1200, 0.0, dt.date(2026, 11, 23),
                                 "text", 0.0123456, None, None),
                                ("ordered", 1.0, 80, None, dt.date(2026, 11, 23), "date",
                                 0.417, "2026-03-28", "2026-11-20")]
                return R()
        got = sql.SqlTool._health(Pg(), "emails")
        self.assertEqual(got["columns"], [
            {"column": "subject", "fill": 0.9877, "distinct": 1200, "cut_share": 0.0,
             "measured": "2026-11-23", "kind": "text", "commonest_share": 0.0123},
            {"column": "ordered", "fill": 1.0, "distinct": 80, "cut_share": None,
             "measured": "2026-11-23", "kind": "date", "commonest_share": 0.417,
             "range": ["2026-03-28", "2026-11-20"]}])


class Docs(unittest.TestCase):
    def test_the_payables_views_state_what_their_rows_are_without_a_verdict(self):
        d = sql._dictionary()
        for view in ("supplier_balances", "supplier_invoices"):
            about = d[view]["about"]
            for word in ("pilot data", "not used", "system of record", "official",
                         "still a pilot"):
                self.assertNotIn(word, about, view)
        self.assertIn("supplier_invoices (source_systems bc)", d["supplier_balances"]["about"])


if __name__ == "__main__":
    unittest.main()
