"""A step build's guarded input (``ontology.step_migrate.guard_inputs``): by default the
held rows of undeclared types are out of sight and the guarded views are the declared
rows alone, so a step's indexed read stays indexed (key_links' lead query probes
link_rows by key, never scans every link once per record); with the tripwire
(``PLX_STEP_TRIPWIRE=1``, as the step migration tests run) a read reaching a held row
fails. Where the tripwire would not raise, both read alike. ``analyze_input`` measures the input and keeps the store's fixed statistics."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ontology import store_ddl
from ontology.step_migrate import TRIPWIRE_ENV, analyze_input, guard_inputs
from ontology.store import StoreBuilder

# identity_scoring's key_links lead query (one probe per BC record)
LEAD = ("SELECT * FROM links WHERE link_id IN (SELECT link_id FROM link_rows "
        "WHERE from_object = :o AND link_type = :t UNION SELECT link_id FROM "
        "link_rows WHERE to_object = :o AND link_type = :t) AND binding_kind = "
        "'same_record' AND tier = 'lead' AND superseded_at IS NULL ORDER BY link_id")


def link(i, a, z, kind="Thing.same"):
    return {"link_id": f"l:{i}", "link_type": kind, "from_object": a, "to_object": z,
            "tier": "lead", "binding_kind": "same_record", "link_contract": None,
            "reason": '[{"k": "v"}]', "why_not": "[]", "evidence_handles": '["h"]',
            "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
            "markings": "[]", "recorded_at": "2026-01-01T00:00:00Z", "superseded_at": None}


class GuardTests(unittest.TestCase):

    def guarded(self, tripwire):
        root = Path(tempfile.mkdtemp(prefix="guard-"))
        b = StoreBuilder(root / "in.sqlite", foreign_keys=False)
        self.addCleanup(b.abort)
        objs = [{"object_id": f"{t}:fm:{n}", "object_type": t, "binding_id": "fm",
                 "native_key": f'["{n}"]', "audience_key": "all",
                 "first_recorded_at": "2026-01-01T00:00:00Z",
                 "missing_from_source_since": None}
                for t in ("Thing", "Other") for n in range(30)]
        b.add("objects", objs)
        b.add("links", [link(n, f"Thing:fm:{n}", f"Thing:fm:{n + 1}") for n in range(29)]
              + [link(100 + n, f"Other:fm:{n}", f"Other:fm:{n + 1}", "Other.same")
                 for n in range(29)]
              + [link(200, "Thing:fm:0", "Other:fm:0", "Thing.other")])
        conn = b.connection
        with mock.patch.dict(os.environ, {TRIPWIRE_ENV: "1" if tripwire else "0"}):
            guard_inputs(conn, ["Thing"])
        analyze_input(conn)
        return conn

    def test_by_default_held_rows_are_out_of_sight_and_reads_stay_indexed(self):
        conn = self.guarded(tripwire=False)
        types = {r[0] for r in conn.execute("SELECT DISTINCT link_type FROM links")}
        self.assertEqual(types, {"Thing.same", "Thing.other"})   # Other's own: held
        self.assertEqual(conn.execute("SELECT count(*) FROM links WHERE link_type = "
                                      "'Other.same'").fetchone()[0], 0)
        plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + LEAD,
                                           {"o": "Thing:fm:3", "t": "Thing.same"})]
        outer = [d for d in plan if d.startswith(("SCAN l", "SEARCH l ", "SCAN link_rows",
                                                  "SEARCH link_rows", "SCAN links"))]
        self.assertTrue(outer, plan)
        self.assertFalse([d for d in plan if d.startswith("SCAN") and "INDEX" not in d
                          and "SUBQUERY" not in d.upper() and "CO-ROUTINE" not in d],
                         plan)   # every link_rows read is by key or index
        self.assertTrue(any("sqlite_autoindex_link_rows_1" in d or "PRIMARY KEY" in d
                            for d in plan), plan)   # the IN list probes link_rows' key
        got = conn.execute(LEAD, {"o": "Thing:fm:3", "t": "Thing.same"}).fetchall()
        self.assertEqual(sorted(r[0] for r in got), ["l:2", "l:3"])
        # the store's fixed value statistics are kept beside the measured ones
        stats = set(conn.execute("SELECT tbl, idx, stat FROM sqlite_stat1"))
        self.assertTrue(set(store_ddl.PLANNER_STATS) <= stats)
        self.assertTrue(any(t == "link_rows" for t, _i, _s in stats))

    def test_with_the_tripwire_a_read_reaching_a_held_row_fails(self):
        conn = self.guarded(tripwire=True)
        with self.assertRaisesRegex(sqlite3.OperationalError, "malformed JSON"):
            conn.execute("SELECT * FROM links WHERE link_type = 'Other.same'").fetchall()
        with self.assertRaisesRegex(sqlite3.OperationalError, "malformed JSON"):
            conn.execute("SELECT * FROM objects WHERE object_type = 'Other'").fetchall()

    def test_by_default_held_objects_are_out_of_sight_whatever_the_plan(self):
        conn = self.guarded(tripwire=False)
        self.assertEqual(conn.execute("SELECT * FROM objects WHERE object_id = 'Other:fm:1'"
                                      ).fetchall(), [])
        self.assertEqual(conn.execute("SELECT count(*) FROM objects").fetchone()[0], 30)
        # a join reaching objects by key from every link end: the declared ends only
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM link_rows l JOIN objects o ON o.object_id = l.to_object"
            ).fetchone()[0], 29)

    def test_both_ways_read_alike_where_the_tripwire_does_not_raise(self):
        reads = ("SELECT * FROM objects WHERE object_type = 'Thing' ORDER BY 1",
                 "SELECT * FROM links WHERE link_type = 'Thing.same' ORDER BY 1",
                 "SELECT * FROM links WHERE from_object = 'Thing:fm:0' ORDER BY 1")
        plain, trip = self.guarded(tripwire=False), self.guarded(tripwire=True)
        for sql in reads:
            self.assertEqual(plain.execute(sql).fetchall(), trip.execute(sql).fetchall(), sql)
        # (the lead query scans the tripwire's links view, so there it raises)

if __name__ == "__main__":
    unittest.main()
