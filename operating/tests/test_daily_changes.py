"""biz.daily_changes: the counts equal a recount of the object versions by day, system, type and
permission class (n of m); a second run adds only what is new, in chunks of any size; a
record of an excluded person counts under the owner-only class and a reader who holds the
other classes sees none of it; a record whose audience narrows or whose owner is excluded after
it was counted moves class at the next run; a version whose object is missing is counted and
reported."""
from __future__ import annotations

import unittest
from collections import Counter
from unittest import mock

from operating.pg import business_views as bv
from operating.pg import daily_changes as dc
from operating.pg import door
from operating.tests import test_business_views as tbv
from operating.tests import test_sql_views_install as tsv


class DailyChanges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        tsv.InstallOnLiveStore._install = tsv.InstallOnLiveStore._install
        cls._install = classmethod(tsv.InstallOnLiveStore._install.__func__)
        tsv.InstallOnLiveStore.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute((MIGRATIONS_DIR / "0070_daily_changes.sql").read_text())
            # one record of an excluded person (the people exclusion's set, migration 0040)
            oid = c.execute("SELECT object_id FROM ontology.object_versions ORDER BY ver "
                            "LIMIT 1").fetchone()[0]
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('h-excluded', %s, 'own_unit')", (oid,))

    def reset(self, c):
        c.execute("DELETE FROM biz.daily_changes")
        c.execute("DELETE FROM biz.daily_changes_objects")
        c.execute("DELETE FROM biz.daily_changes_mark")

    def conn(self):
        import psycopg
        c = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(c.close)
        return c

    def expected(self, c):
        """A recount in Python over every version: (day, system, type, audience, marks,
        owner_only) -> [created, changed]."""
        rows = c.execute(
            "SELECT v.object_id, v.ver, v.recorded_at, v.markings, o.binding_id, o.object_type, "
            "o.audience_key FROM ontology.object_versions v JOIN ontology.objects o USING "
            "(object_id) ORDER BY v.object_id, v.ver").fetchall()
        excluded = {r[0] for r in c.execute("SELECT object_id FROM "
                                            "ontology.people_excluded_objects").fetchall()}
        now_marks = {r[0]: r[1] for r in c.execute(
            "SELECT object_id, markings FROM ontology.object_versions WHERE superseded_at IS NULL"
        ).fetchall()}
        seen, out = set(), {}
        for oid, ver, at, marks, binding, kind, aud in rows:
            marks = now_marks.get(oid, "[]")   # what the object is now
            day = c.execute("SELECT (%s::timestamptz AT TIME ZONE %s)::date", (at, dc.ZONE)
                            ).fetchone()[0]
            system = binding[4:].split(".")[0] if binding.startswith("gen.") else binding.split(".")[0]
            key = (day, system, kind, aud, marks, oid in excluded)
            cell = out.setdefault(key, [0, 0])
            cell[0 if oid not in seen else 1] += 1
            seen.add(oid)
        return out

    def table(self, c):
        return {(d, s, k, a, m, o): [cr, ch] for d, s, k, a, m, o, cr, ch in c.execute(
            "SELECT day, system, object_type, audience_key, markings, owner_only, created, "
            "changed FROM biz.daily_changes").fetchall()}

    def test_counts_equal_a_recount_in_chunks_and_a_second_run_adds_only_what_is_new(self):
        c = self.conn()
        self.reset(c)
        want = self.expected(c)
        self.assertGreater(len(want), 0)
        total = c.execute("SELECT count(*) FROM ontology.object_versions").fetchone()[0]
        got = dc.run(c, chunk=1)
        self.assertEqual(got["chunks"], total)                        # one version a chunk
        self.assertEqual(self.table(c), want)                         # n of m: every group equal
        self.assertEqual(got["versions"], total)
        self.assertEqual(sum(a + b for a, b in want.values()), total)
        # a second run with nothing new changes nothing
        again = dc.run(c, chunk=3)
        self.assertEqual((again["versions"], self.table(c)), (0, want))
        # one new version of an existing record and one of a new record: exactly those are added
        c.execute("INSERT INTO ontology.object_versions SELECT * FROM ontology.object_versions "
                  "WHERE ver = (SELECT min(ver) FROM ontology.object_versions) LIMIT 0")
        cols = [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE "
                                        "table_schema = 'ontology' AND table_name = "
                                        "'object_versions' ORDER BY ordinal_position").fetchall()]
        top = c.execute("SELECT max(ver) FROM ontology.object_versions").fetchone()[0]
        sel = ", ".join("ver + %s" if x == "ver" else x for x in cols)
        c.execute(f"INSERT INTO ontology.object_versions ({', '.join(cols)}) SELECT {sel} FROM "
                  "ontology.object_versions WHERE ver = (SELECT min(ver) FROM "
                  "ontology.object_versions)", (top + 1000,))
        after = dc.run(c, chunk=3)
        self.assertEqual(after["versions"], 1)
        want2 = self.expected(c)
        self.assertEqual(self.table(c), want2)
        self.assertEqual(sum(a + b for a, b in want2.values()), total + 1)

    def test_a_record_whose_audience_narrows_or_owner_is_excluded_moves_class_at_the_next_run(self):
        c = self.conn()
        self.reset(c)
        dc.run(c, chunk=100)
        victim = c.execute("SELECT o.object_id, o.audience_key FROM ontology.objects o JOIN "
                           "biz.daily_changes_objects s USING (object_id) WHERE s.created > 0 AND "
                           "NOT EXISTS (SELECT 1 FROM ontology.people_excluded_objects p WHERE "
                           "p.object_id = o.object_id) ORDER BY o.object_id LIMIT 1").fetchone()
        oid, old_aud = victim
        before = c.execute("SELECT sum(created) FROM biz.daily_changes WHERE audience_key = %s "
                           "AND NOT owner_only", (old_aud,)).fetchone()[0]
        old_cls = {r[0] for r in c.execute("SELECT DISTINCT access_class FROM biz.daily_changes "
                                           "WHERE audience_key = %s AND NOT owner_only",
                                           (old_aud,)).fetchall()}
        # its audience narrows to a unit nobody of the old readers holds
        c.execute("UPDATE ontology.objects SET audience_key = 'unit:narrowed' WHERE object_id = %s",
                  (oid,))
        got = dc.run(c, chunk=100)
        self.assertEqual(got["versions"], 0)                    # nothing new was counted ...
        self.assertGreater(got["added_rows"], 0)                # ... yet the groups moved
        moved = c.execute("SELECT sum(created), array_agg(DISTINCT access_class) FROM "
                          "biz.daily_changes WHERE audience_key = 'unit:narrowed'").fetchone()
        self.assertGreater(moved[0], 0)
        self.assertTrue(old_cls.isdisjoint(set(moved[1])))      # a class the old readers lack
        after = c.execute("SELECT coalesce(sum(created), 0) FROM biz.daily_changes WHERE "
                          "audience_key = %s AND NOT owner_only", (old_aud,)).fetchone()[0]
        self.assertEqual(before - after, moved[0])              # exactly its counts left
        self.assertEqual(self.table(c), self.expected(c))
        # its owner is added to the exclusion set: the counts become owner-only
        c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, reason) "
                  "VALUES ('h-late', %s, 'own_unit')", (oid,))
        self.addCleanup(c.execute, "DELETE FROM ontology.people_excluded_objects WHERE "
                        "l1_handle = 'h-late'")
        dc.run(c, chunk=100)
        self.assertEqual(c.execute("SELECT count(*) FROM biz.daily_changes WHERE audience_key = "
                                   "'unit:narrowed' AND NOT owner_only").fetchone()[0], 0)
        self.assertGreater(c.execute("SELECT count(*) FROM biz.daily_changes WHERE audience_key "
                                     "= 'unit:narrowed' AND owner_only").fetchone()[0], 0)
        c.execute("UPDATE ontology.objects SET audience_key = %s WHERE object_id = %s",
                  (old_aud, oid))

    def test_a_version_whose_object_is_missing_is_counted_and_reported(self):
        c = self.conn()
        self.reset(c)
        dc.run(c, chunk=100)
        c.execute("INSERT INTO biz.daily_changes_objects (day, object_id, created) VALUES "
                  "(current_date, 'gone-object', 4)")
        self.addCleanup(c.execute, "DELETE FROM biz.daily_changes_objects WHERE object_id = "
                        "'gone-object'")
        got = dc.run(c, chunk=100)
        self.assertEqual(got["orphans"], 1)
        row = c.execute("SELECT system, created, owner_only FROM biz.daily_changes WHERE "
                        "system = 'unknown'").fetchone()
        self.assertEqual(tuple(row), ("unknown", 4, True))

    def test_the_view_shows_a_reader_the_groups_of_its_classes_only(self):
        c = self.conn()
        self.reset(c)
        dc.run(c, chunk=100)
        owner_rows = c.execute("SELECT count(*) FROM biz.daily_changes WHERE owner_only"
                               ).fetchone()[0]
        classes = [r[0] for r in c.execute("SELECT DISTINCT access_class FROM biz.daily_changes "
                                           "ORDER BY 1").fetchall()]
        only = {r[0] for r in c.execute("SELECT DISTINCT access_class FROM biz.daily_changes "
                                        "WHERE owner_only").fetchall()}
        self.assertTrue(only.isdisjoint({r[0] for r in c.execute(
            "SELECT DISTINCT access_class FROM biz.daily_changes WHERE NOT owner_only")}))
        with c.transaction():
            c.execute("SELECT set_config('plx.held', %s, true)",
                      (door.ranges(set(classes) - only),))
            n_other = c.execute(f"SELECT count(*) FROM {dc_view()}").fetchone()[0]
        with c.transaction():
            c.execute("SELECT set_config('plx.held', %s, true)", (door.ranges(classes),))
            n_all = c.execute(f"SELECT count(*) FROM {dc_view()}").fetchone()[0]
        with c.transaction():
            c.execute("SELECT set_config('plx.held', %s, true)", ("{}",))
            n_none = c.execute(f"SELECT count(*) FROM {dc_view()}").fetchone()[0]
        total = c.execute("SELECT count(*) FROM biz.daily_changes").fetchone()[0]
        self.assertEqual((n_all, n_none), (total, 0))
        self.assertEqual(n_other, total - owner_rows)
        self.assertGreater(owner_rows, 0)       # the excluded person's records count, owner only


def dc_view():
    from operating.pg import sql_views
    return f"{sql_views.guarded_schema()}.daily_changes"


if __name__ == "__main__":
    unittest.main()
