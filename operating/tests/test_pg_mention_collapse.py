"""CC-C-04 physical collapse (``operating.pg.mention_collapse``) on a real Postgres: the folded
store gives the occurrence-aware unique_mentions the same firm links, the stored firm rows
are what that step writes, verify passes, and undo puts every row back exactly."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
import sqlite3
import unittest

from ontology import mc_task_links
from ontology import mention_firm as mf
from ontology import mention_occurrences as MO
from operating.tests import pg_testdb

OWNER = {"petra_po": "PurchaseOrder"}
LT = "Email.mentions_purchase_order"
TYPES = [LT, "MCTask.mentions_purchase_order"]
UNC = {"agrees": None, "detail": "x", "feature": "uncorroborated"}
QUOTED = {"agrees": False, "feature": "quoted_history"}
COLS = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind",
        "link_contract", "reason", "why_not", "evidence_handles", "reviewed_by", "reviewed_at",
        "review_action_id", "markings", "recorded_at", "superseded_at")


def canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def mid(seed):
    return "mention:" + sha256(seed.encode()).hexdigest()[:32]


def ordered(prefix, low_first):
    """Two ids, the first sorting before the second iff ``low_first``."""
    a, b = sorted((mid(prefix + ":1"), mid(prefix + ":2")))
    return (a, b) if low_first else (b, a)


def row(lid, frm, to, value, span, group, why_not=(UNC,), markings="[]", suffix="",
        recorded="2026-10-01T00:00:00Z", lt=LT):
    reason = [{"agrees": True, "feature": "identifier_mention", "identifier_kind": "petra_po",
               "mention_group": group, "normalized": value, "span": span},
              {"agrees": True, "feature": "identity_candidate", "property": "number"}]
    return {"link_id": lid + suffix, "link_type": lt, "from_object": frm, "to_object": to,
            "tier": "lead", "binding_kind": "mention", "link_contract": None,
            "reason": canon(reason), "why_not": canon(list(why_not)),
            "evidence_handles": canon([f"h:{frm}", "h:field", f"h:{to}"]),
            "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
            "markings": markings, "recorded_at": recorded, "superseded_at": None}


class _Conn:
    """The SQLite side ``judge_groups`` reads (``one_record``'s same_record links: none)."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE link_rows (from_object, to_object, link_type, tier, "
                        "superseded_at)")

    def __getattr__(self, name):
        return getattr(self.db, name)


def firm_of(leads, conn):
    out = []
    by_from = {}
    for lead in leads:
        by_from.setdefault(lead["from_object"], []).append(lead)
    for frm in sorted(by_from):
        for lead in mf.judge_groups(conn, by_from[frm], OWNER, Counter()):
            out.append(mf.firm_row(lead))
    return MO.one_per_lead(out)


class MentionCollapseTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        from operating.pg.migrate import migrate
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            migrate(pg)
        self.side = _Conn()

    def sql(self, statement, params=None):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            cur = pg.execute(statement, params)
            return cur.fetchall() if cur.description else None

    def insert(self, rows):
        for r in rows:
            self.sql(f"INSERT INTO ontology.link_rows ({', '.join(COLS)}) VALUES "
                     f"({', '.join(['%s'] * len(COLS))})", [r[c] for c in COLS])

    def links(self, prefix):
        got = self.sql(f"SELECT {', '.join(COLS)} FROM ontology.links WHERE "
                       "left(link_id, %s) = %s AND superseded_at IS NULL ORDER BY link_id",
                       (len(prefix), prefix))
        return [dict(zip(COLS, r)) for r in got]

    def snapshot(self):
        return sorted(r[0] for r in self.sql(
            "SELECT to_jsonb(r)::text FROM ontology.link_rows r"))

    def scenario(self):
        rows, D = [], "Email:m:"
        # 1: keeper vetoed (quoted history), its twin the firm one -> the firm row moves
        k, t = ordered("s1", True)
        rows += [row(k, D + "1", "PurchaseOrder:a:1", "PO907711", [4, 12], "g1a", (UNC, QUOTED)),
                 row(t, D + "1", "PurchaseOrder:a:1", "PO907711", [40, 48], "g1b")]
        # 2 / 3: an ambiguous number (two orders) and, in another occurrence, a number only one
        # of them holds; both orders of ids (a lost firm link, and a false one)
        for n, low in (("2", True), ("3", False)):
            k, t = ordered("s" + n, low)
            other = mid(f"s{n}:other")
            rows += [row(k, D + n, "PurchaseOrder:b:1", "PO111111", [0, 8], f"g{n}c"),
                     row(other, D + n, "PurchaseOrder:b:2", "PO111111", [0, 8], f"g{n}c"),
                     row(t, D + n, "PurchaseOrder:b:1", "PO222222", [20, 28], f"g{n}d")]
        # 4: both occurrences firm -> the keeper's firm row stays, the twin's goes
        k, t = ordered("s4", True)
        rows += [row(k, D + "4", "PurchaseOrder:c:1", "PO333333", [0, 8], "g4a"),
                 row(t, D + "4", "PurchaseOrder:c:1", "PO333333", [30, 38], "g4b")]
        # 5: markings differ -> both rows stay
        k, t = ordered("s5", True)
        rows += [row(k, D + "5", "PurchaseOrder:d:1", "PO444444", [0, 8], "g5a"),
                 row(t, D + "5", "PurchaseOrder:d:1", "PO444444", [30, 38], "g5b",
                     markings='["finance"]')]
        # 6: review pieces of one pair (same @ suffix) fold as their own key
        k, t = ordered("s6", True)
        rows += [row(k, D + "6", "PurchaseOrder:e:1", "PO555555", [0, 8], "g6a",
                     suffix="@2026-10-05T00:00:00Z", recorded="2026-10-05T00:00:00Z"),
                 row(t, D + "6", "PurchaseOrder:e:1", "PO555555", [30, 38], "g6b",
                     suffix="@2026-10-05T00:00:00Z", recorded="2026-10-05T00:00:00Z")]
        return rows

    def test_folded_store_gives_the_same_firm_links_and_undo_is_exact(self):
        from operating.pg import mention_collapse as mc
        leads = self.scenario()
        firm_before = firm_of(leads, self.side)
        # an MC task's twin holding the crb-mcid row (task_ids' own row shape)
        k, t = ordered("s7", True)
        tasks = [row(k, "MCTask:t:1", "PurchaseOrder:f:1", "PO666666", [0, 8], "g7a",
                     lt="MCTask.mentions_purchase_order"),
                 row(t, "MCTask:t:1", "PurchaseOrder:f:1", "PO666666", [9, 17], "g7b",
                     lt="MCTask.mentions_purchase_order")]
        mcid = mc_task_links._firm_row(tasks[1])
        self.insert(leads + tasks + firm_before + [mcid])
        before = self.snapshot()
        self.assertEqual({r["to_object"] for r in firm_before},
                         {"PurchaseOrder:a:1", "PurchaseOrder:b:1", "PurchaseOrder:c:1",
                          "PurchaseOrder:d:1", "PurchaseOrder:e:1"})

        out = mc.run(self.dsn, workers=2, types=TYPES, log=lambda m: None)
        self.assertEqual(out["failed"], [], out)
        self.assertEqual(out["totals"].get("twins_deleted"), 6, out)   # s1-s4, s6, s7; not s5
        check = mc.verify(self.dsn)
        self.assertTrue(check["ok"], check)

        after = self.links("mention:")
        self.assertEqual(len(after), len(leads) + 2 - 6)
        stored_firm = {(r["link_id"], r["reason"], r["to_object"]) for r in self.links("crb-mid:")}
        # the occurrence-aware step on the folded store writes exactly the stored firm rows
        computed = {(r["link_id"], r["reason"], r["to_object"]) for r in
                    firm_of([r for r in after if r["from_object"].startswith("Email:")],
                            self.side)}
        self.assertEqual(stored_firm, computed)
        # the firm link set is unchanged: same (type, from, to) pairs
        self.assertEqual({(r["link_type"], r["from_object"], r["to_object"])
                          for r in firm_before},
                         {(r["link_type"], r["from_object"], r["to_object"])
                          for r in self.links("crb-mid:")})
        # without the occurrence views the folded store would lose s1/s2 and invent s3's
        naive = {r["to_object"] for r in firm_of(
            [dict(r, reason=MO.own_reason(r["reason"])) for r in after
             if r["from_object"].startswith("Email:")], self.side)}
        self.assertNotEqual(naive, {r["to_object"] for r in firm_before})
        # task_ids: the crb-mcid row now cites the keeper
        mc_rows = self.links("crb-mcid:")
        self.assertEqual([r["link_id"] for r in mc_rows], [mc_task_links._firm_row(
            dict(tasks[0]))["link_id"]])
        self.assertTrue(mc_rows[0]["reason"].endswith(f'"link_id":"{tasks[0]["link_id"]}"}}]'))
        # a second run folds nothing again
        again = mc.run(self.dsn, workers=2, types=TYPES, log=lambda m: None)
        self.assertEqual(again["done"], 0)

        undone = mc.undo(self.dsn)
        self.assertGreater(undone["rows_restored"], 0)
        self.assertEqual(self.snapshot(), before)

    def test_train_runs_only_for_a_plan_naming_it_over_its_served_steps(self):
        import sys
        import types
        from unittest import mock
        from operating.pg import mention_collapse as mc
        leads = self.scenario()
        self.insert(leads + firm_of(leads, self.side))
        before = self.snapshot()
        plan = {"steps_to": "steps:new", "steps": {"run": {}, "collapsed": ["task_ids",
                                                                            "unique_mentions"]}}
        out = mc.train(self.dsn, {"steps_to": "steps:new", "steps": {"run": {}}})
        self.assertEqual(out, {"ok": True, "collapsed": [], "skipped": "not_in_plan"})
        out = mc.train(self.dsn, plan)   # the served step code is not the plan's
        self.assertEqual((out["ok"], out["refused"]), (False, "steps_not_served"), out)
        self.assertEqual(self.snapshot(), before)

        self.sql("INSERT INTO ontology.store_meta (key, value) VALUES ('steps_digest', "
                 "'steps:new') ON CONFLICT (key) DO UPDATE SET value = excluded.value")
        refreshed = []
        stub = types.ModuleType("operating.pg.mention_repeats")
        stub.TABLE = "public.c04_markers_stub"
        stub.refresh = lambda dsn, objects: refreshed.append(list(objects)) or {
            "objects": len(objects), "marked": 0}
        self.sql("CREATE TABLE public.c04_markers_stub (link_id text)")
        with mock.patch.dict(sys.modules, {"operating.pg.mention_repeats": stub}):
            out = mc.train(self.dsn, plan, workers=2, log=lambda m: None)
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["markers"]["skipped"], "not_built")   # an empty table
            self.assertEqual(refreshed, [])
            self.sql("INSERT INTO public.c04_markers_stub VALUES ('x')")
            self.assertEqual(mc.main(["markers", "--dsn", self.dsn]), 0)
            folded = ["Email:m:1", "Email:m:2", "Email:m:3", "Email:m:4", "Email:m:6"]
            self.assertEqual(refreshed, [folded])
            self.assertTrue(out["verify"]["ok"])
            self.assertEqual(out["run"]["totals"].get("twins_deleted"), 5, out)
            again = mc.train(self.dsn, plan, workers=2, log=lambda m: None)
            self.assertEqual((again["ok"], again["run"]["done"]), (True, 0), again)
            self.assertEqual(mc.main(["undo", "--dsn", self.dsn]), 0)
            self.assertEqual(refreshed[-1], folded)   # the restored twins' pairs again
        self.assertEqual(self.snapshot(), before)

    def rows_of(self, frm):
        return sorted(r[0] for r in self.sql(
            "SELECT to_jsonb(v)::text FROM ontology.links v WHERE from_object = %s", (frm,)))

    def test_undo_after_a_later_delta_restores_only_untouched_keys(self):
        from operating.pg import mention_collapse as mc
        leads = self.scenario()
        firm = firm_of(leads, self.side)
        self.insert(leads + firm)
        before = {d: self.rows_of(d) for d in (f"Email:m:{n}" for n in range(1, 7))}
        self.assertTrue(mc.run(self.dsn, workers=2, types=TYPES, log=lambda m: None)["done"])
        self.assertTrue(mc.verify(self.dsn)["ok"])
        # capture resumes: a layer replaces document 1's rows (re-derived, uncollapsed, a
        # newer version) and a review supersedes document 4's keeper
        self.sql("DELETE FROM ontology.link_rows WHERE from_object = 'Email:m:1'")
        newer = [dict(r, recorded_at="2026-11-16T00:00:00Z")
                 for r in leads + firm if r["from_object"] == "Email:m:1"]
        self.insert(newer)
        self.sql("UPDATE ontology.link_rows SET superseded_at = '2026-11-16T00:00:00Z' WHERE "
                 "from_object = 'Email:m:4' AND left(link_id, 8) = 'mention:'")
        layer = {d: self.rows_of(d) for d in ("Email:m:1", "Email:m:4")}

        out = mc.undo(self.dsn)
        self.assertEqual(out["keys_skipped"], 2, out)   # m:1 and m:4
        self.assertEqual(out["keys_restored"], 3, out)  # m:2, m:3, m:6
        self.assertEqual(out["undone"], 0, out)
        self.assertEqual(sum(out["marks_kept"].values()), 2, out)
        for d in ("Email:m:2", "Email:m:3", "Email:m:5", "Email:m:6"):
            self.assertEqual(self.rows_of(d), before[d], d)
        # the layer's rows stand: no old version revived, no re-created firm row dropped
        self.assertEqual({d: self.rows_of(d) for d in layer}, layer)
        self.assertEqual(mc.main(["undo", "--dsn", self.dsn]), 1)   # still skipped
        self.assertEqual({d: self.rows_of(d) for d in layer}, layer)

        held = mc.retire(self.dsn)   # inside the window
        self.assertEqual((held["ok"], held["refused"]), (False, "inside_window"), held)
        done = mc.retire(self.dsn, now="2099-01-01T00:00:00Z")
        self.assertTrue(done["retired"], done)
        self.assertEqual(self.sql("SELECT to_regclass('ontology.mention_collapse_undo')"),
                         [(None,)])
        self.assertEqual(mc.undo(self.dsn)["skipped"], "no_undo_table")
        self.assertEqual(mc.verify(self.dsn)["ok"], True)
        self.assertEqual({d: self.rows_of(d) for d in layer}, layer)

    def test_undo_table_has_a_primary_key_fresh_and_after_the_old_shape(self):
        # migrate_chunks refuses any undo log while an ontology table lacks a primary key
        import psycopg
        from operating.pg import mention_collapse as mc
        pk = ("SELECT array_agg(a.attname::text) FROM pg_index i JOIN pg_attribute a ON "
              "a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) WHERE i.indrelid = "
              "'ontology.mention_collapse_undo'::regclass AND i.indisprimary")
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS ontology.mention_collapse_undo")
            mc.ensure(conn)
            self.assertEqual(conn.execute(pk).fetchone()[0], ["undo_id"])
            conn.execute("DROP TABLE ontology.mention_collapse_undo")
            conn.execute("CREATE TABLE ontology.mention_collapse_undo (mark text NOT NULL, "
                         "action text NOT NULL, link_id text NOT NULL, link_type text NOT "
                         "NULL, image jsonb, keeper text, from_object text, to_object text)")
            conn.execute("INSERT INTO ontology.mention_collapse_undo (mark, action, link_id, "
                         "link_type) VALUES ('m', 'delete', 'l', 't')")
            mc.ensure(conn)
            mc.ensure(conn)   # the served shape (key already there) is a no-op
            self.assertEqual(conn.execute(pk).fetchone()[0], ["undo_id"])
            self.assertEqual(conn.execute(
                "SELECT count(*), count(undo_id) FROM ontology.mention_collapse_undo"
            ).fetchone(), (1, 1))
            conn.execute("DROP TABLE ontology.mention_collapse_undo")

    def test_exclusion_purge_drops_the_undo_keys_of_a_purged_document(self):
        from operating import mail_exclusion_purge as purge
        from operating.pg import mention_collapse as mc
        leads = self.scenario()
        self.insert(leads + firm_of(leads, self.side))
        self.sql("INSERT INTO ontology.object_versions (ver, object_id, l1_handle, digest, "
                 "lineage, markings, recorded_at) VALUES (1, 'Email:m:1', 'graph:mail:x-1', "
                 "'d', 'l', '[]', 'x')")
        self.assertTrue(mc.run(self.dsn, workers=2, types=TYPES, log=lambda m: None)["done"])
        undo_of = ("SELECT count(*) FROM ontology.mention_collapse_undo WHERE "
                   "image ->> 'from_object' = 'Email:m:1'")
        self.assertGreater(self.sql(undo_of)[0][0], 0)
        dry = purge.purge_pg(self.dsn, ["graph:mail:x-1"], apply=False)
        self.assertGreater(dry["ontology.mention_collapse_undo"], 0, dry)
        out = purge.purge_pg(self.dsn, ["graph:mail:x-1"], apply=True)
        self.assertEqual(out["ontology.mention_collapse_undo"],
                         dry["ontology.mention_collapse_undo"])
        self.assertEqual(self.sql(undo_of)[0][0], 0)
        self.assertEqual(self.rows_of("Email:m:1"), [])
        out = mc.undo(self.dsn)
        self.assertEqual(out["keys_skipped"], 0, out)
        self.assertEqual(self.rows_of("Email:m:1"), [])   # nothing purged comes back

    def test_views_of_an_unfolded_row_are_the_row(self):
        r = row(mid("x"), "Email:m:9", "PurchaseOrder:z:1", "PO1", [0, 3], "g")
        self.assertEqual(list(MO.views([r])), [r])
        self.assertEqual(MO.one_per_lead([{"link_id": "a", "x": 1}]), [{"link_id": "a", "x": 1}])


if __name__ == "__main__":
    unittest.main()
