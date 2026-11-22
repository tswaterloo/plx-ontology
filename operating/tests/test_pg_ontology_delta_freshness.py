"""Served freshness by cadence and a layer's freshness in ``ontology.store_meta``: split
from test_pg_ontology_delta.py, no change to a test."""
from __future__ import annotations

import json
import sqlite3
import unittest


class ServedFreshnessByCadenceTests(unittest.TestCase):
    """A unit is counted by its own cadence: the served time of a source is its newest unit
    read, and the source is stale only when some unit is overdue against its cadence (a
    stopped capture: all of them), for every source."""

    @staticmethod
    def stub(relations):
        class Captures:
            def describe(self):
                return {"relations": relations}

            def close(self):
                pass
        return lambda: Captures()

    @staticmethod
    def ago(hours):
        import datetime as dt
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")

    def state(self, source, times=None, one=None):
        from ontology import delta
        if source in ("teams", "graph_mail"):   # their stores are read by their own readers
            return delta.judge_refreshes(source, times or [one])
        rel = {"last_refresh_at": one if one else min(times)}
        if times is not None:
            rel["unit_times"] = times
        return delta.capture_refresh_state(source, self.stub({"rel": rel}), None)

    def test_dormant_units_on_their_cadence_are_not_overdue_a_stopped_capture_is(self):
        for source in ("bc", "mission_control", "meetings", "sharepoint", "teams", "graph_mail"):
            with self.subTest(source=source):
                fresh = [self.ago(0.1), self.ago(3), self.ago(5)]
                self.assertEqual(self.state(source, fresh), (max(fresh), False))
                stopped = [self.ago(h) for h in (40, 50, 60)]   # every unit aged: all overdue
                self.assertEqual(self.state(source, stopped), (max(stopped), True))
                self.assertTrue(self.state(source, one=self.ago(60))[1])   # one time only
                self.assertFalse(self.state(source, one=self.ago(1))[1])
        # teams: a dormant chat read every 12-24 h is current at 20 h, late at 30 h
        self.assertFalse(self.state("teams", [self.ago(0.1), self.ago(20)])[1])
        self.assertTrue(self.state("teams", [self.ago(0.1), self.ago(30)])[1])
        # mail: a folder read every cycle is late at 8 h
        self.assertTrue(self.state("graph_mail", [self.ago(0.1), self.ago(8)])[1])

    def test_unreadable_or_unstated_times_claim_nothing(self):
        from ontology import delta
        self.assertEqual(delta.capture_refresh_state(
            "bc", self.stub({"r": {"last_refresh_at": None, "unit_times": []}}), None),
            (None, False))
        self.assertEqual(delta.capture_refresh_state("bc", self.stub({"r": {
            "last_refresh_at": "not a time"}}), None), (None, False))
        self.assertEqual(delta.capture_refresh_state("bc", lambda: 1 / 0, None), (None, False))

    def test_the_served_entry_is_stale_only_when_a_unit_is_overdue(self):
        import datetime as dt
        import json
        from operating.pg import ontology_delta as od
        now = dt.datetime(2026, 11, 19, 12, 0, tzinfo=dt.timezone.utc)
        newest = (now - dt.timedelta(minutes=5)).isoformat()
        held = json.dumps([{"source": "teams", "captured_at": (now - dt.timedelta(
            hours=13)).isoformat(), "lag_seconds": 46800, "stale": False,
            "completeness": "complete"}])
        base = {"state": "complete", "read_from": newest, "served_through": newest,
                "captured_through": newest}
        got = json.loads(od.advance_freshness(
            held, {"teams": {**base, "refresh_overdue": False}}, now=now))[0]
        self.assertEqual((got["captured_at"], got["stale"]), (newest, False))
        late = json.loads(od.advance_freshness(
            held, {"teams": {**base, "refresh_overdue": True}}, now=now))[0]
        self.assertTrue(late["stale"])
        kept = od.merge_reads("{}", {"teams": {"read_from": newest, "state": "complete",
                                               "refresh_overdue": True,
                                               "refreshed_at": newest}}, settled=True)
        self.assertTrue(kept["teams"]["refresh_overdue"])
        kept = od.merge_reads(json.dumps(kept), {"teams": {
            "read_from": (now + dt.timedelta(minutes=1)).isoformat(), "state": "complete", "refreshed_at": newest}},
            settled=True)
        self.assertFalse(kept["teams"]["refresh_overdue"])

    def test_the_overdue_flag_survives_the_served_rule(self):
        """advance_freshness output goes through ``aged`` (answers and Home): a stopped
        capture, most units dead with one fresh, and all-dormant Teams chats."""
        import datetime as dt
        import json
        from operating.freshness_cadence import aged
        from operating.pg import ontology_delta as od
        now = dt.datetime(2026, 11, 19, 12, 0, tzinfo=dt.timezone.utc)
        bounds = {"graph_mail": 36 * 3600.0, "teams": 36 * 3600.0}

        def served(source, newest_age_h, overdue, held_age_h=1):
            newest = (now - dt.timedelta(hours=newest_age_h)).isoformat()
            held = json.dumps([{"source": source, "captured_at": (now - dt.timedelta(
                hours=held_age_h)).isoformat(), "lag_seconds": 100, "stale": False}])
            read = {"state": "complete", "read_from": newest, "served_through": newest,
                    "captured_through": newest, "refresh_overdue": overdue}
            moved = od.advance_freshness(held, {source: read}, now=now)
            entry = json.loads(moved or held)[0]     # None: nothing moves, the entry stands
            return aged(entry, bounds, now)

        # a mail capture that stopped 10 h ago: the newest unit is 10 h old, inside 36 h
        stopped = served("graph_mail", 10, True, held_age_h=10)
        self.assertTrue(stopped["stale"])
        self.assertTrue(stopped["label"])
        # most units dead, one fresh: the headline is minutes old and still overdue
        mixed = served("graph_mail", 0.1, True)
        self.assertTrue(mixed["stale"])
        # every unit read inside its own interval (dormant Teams chats: 12 h): fresh
        dormant = served("teams", 13, False, held_age_h=13)
        self.assertFalse(dormant["stale"])
        # recovered: the next read that finds none late clears the flag
        held = json.dumps([dict(mixed, served_from=None)])
        newest = now.isoformat()
        back = json.loads(od.advance_freshness(held, {"graph_mail": {
            "state": "complete", "read_from": newest, "served_through": newest,
            "captured_through": newest, "refresh_overdue": False}}, now=now))[0]
        self.assertFalse(aged(back, bounds, now)["stale"])


class LayerFreshnessTests(unittest.TestCase):
    """A layer's freshness reaches ``ontology.store_meta`` with its per-relation
    completeness: a family the layer read takes the layer's facts, or none, so a
    build-time "read in full" never vouches for a newer partial read."""

    def test_relation_completeness_follows_the_layer_per_family(self):
        import json
        from operating.pg import ontology_delta
        layer = sqlite3.connect(":memory:")
        layer.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
        partial = {"orders": {"completeness": "partial", "stale": False}}
        complete = {"orders": {"completeness": "complete", "stale": False}}
        layer.executemany("INSERT INTO store_meta VALUES (?, ?)", [
            ("delta_families", json.dumps(["bc", "mail"])),
            ("relation_completeness", json.dumps({"bc": partial}))])
        held = {"relation_completeness": json.dumps(
            {"bc": complete, "mail": complete, "portal_api": complete})}
        written = {}

        class Cursor:
            def execute(self, sql, params=()):
                if sql.startswith("INSERT"):
                    written[params[0]] = params[1]

            def fetchall(self):
                return list(held.items())

        ontology_delta._freshness(layer, Cursor())
        self.assertEqual(json.loads(written["relation_completeness"]),
                         {"bc": partial, "portal_api": complete})
