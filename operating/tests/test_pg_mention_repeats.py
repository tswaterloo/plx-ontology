"""CC-C-04 marker (``operating.pg.mention_repeats``): its keeper is the read side's, a row is
marked only when no reader would see it in the keeper's place, the reader's ``links``
without the marked rows equals the read side's dedupe of the whole set, a stale marker
never hides a pair's only link, no build step reads the table, and the build writes no
link row."""
from __future__ import annotations

import itertools
import json
import random
import re
import unittest
from unittest import mock
from pathlib import Path

from operating.ontology_tools_policy import collapse_mention_rows, mention_keeper_order
from operating.pg import mention_repeats as mr

ROOT = Path(__file__).resolve().parents[2]
DOC = "graph-mail://box/m1"
T0 = "2026-11-01T00:00:00Z"
TIERS = ("verified", "corroborated", "lead", "contradicted", "stored_by_text_join")


def _row(link_id, *, tier="lead", to="PurchaseOrder:1", doc=DOC, markings="[]",
         recorded=T0, cites=None, handles=("graph-bc://supplier/9",), sup=None,
         link_type="Mail.mentions_po", frm="Mail:1", kind="mention"):
    reason = []
    if tier == "corroborated":
        reason.append({"feature": "supplier_name", "agrees": True,
                       "citations": [{"l1_handle": h} for h in handles]})
    if cites:
        reason.append({"feature": "corroborates", "link_id": cites})
    return {"link_id": link_id, "link_type": link_type, "from_object": frm, "to_object": to,
            "tier": tier, "binding_kind": kind, "markings": markings, "recorded_at": recorded,
            "superseded_at": sup, "evidence_handles": json.dumps([doc]),
            "reason": json.dumps(reason)}


def _reader_tier(row, readable):
    return "lead" if row["tier"] == "corroborated" and not readable else row["tier"]


def _code_of(path: Path) -> str:
    """The code of ``path``: a Python file without its docstrings and comments (a module
    named in prose is no read of it), any other file as it is."""
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    if path.suffix != ".py":
        return text
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body[0] = ast.Pass()
    return ast.unparse(tree)


class DecideTests(unittest.TestCase):
    def test_keeper_order_is_the_read_side_collapse(self):
        """The marker's keeper and ``collapse_mention_rows``' kept row agree, and the read
        side's answer over the unmarked rows equals its answer over all of them for every
        reader (each corroboration readable or not)."""
        rnd = random.Random(7)
        for trial in range(300):
            rows = []
            for i in range(rnd.randint(2, 6)):
                tier = rnd.choice(TIERS)
                rows.append(_row(f"{rnd.choice(['mention', 'crb-mid', 'docfield'])}:"
                                 f"{rnd.randrange(16 ** 6):06x}{i}", tier=tier,
                                 handles=(rnd.choice(["graph-bc://s/1", "graph-bc://s/2"]),),
                                 markings=rnd.choice(["[]", "[]", '["hr"]'])))
            kept = collapse_mention_rows([(r["tier"], r) for r in rows])
            self.assertEqual(len(kept), 1)
            keeper = min(rows, key=lambda r: mention_keeper_order(r["tier"], r["link_id"]))
            self.assertIs(kept[0][1], keeper)
            marked = mr.decide(rows)
            self.assertNotIn(keeper["link_id"], marked)
            self.assertTrue(all(v == keeper["link_id"] for v in marked.values()))
            left = [r for r in rows if r["link_id"] not in marked]
            for readable in itertools.product((True, False), repeat=len(rows)):
                see = dict(zip((r["link_id"] for r in rows), readable))
                whole = collapse_mention_rows([(_reader_tier(r, see[r["link_id"]]), r)
                                               for r in rows])
                part = collapse_mention_rows([(_reader_tier(r, see[r["link_id"]]), r)
                                              for r in left])
                # one reader reads a record or not: rows citing it agree
                if self._consistent(rows, see):
                    self.assertEqual([(t, r["link_id"]) for t, r in part],
                                     [(t, r["link_id"]) for t, r in whole], (trial, see))

    @staticmethod
    def _consistent(rows, see):
        """Readability that one reader can have: rows citing the same record agree."""
        by_handle = {}
        for r in rows:
            if r["tier"] != "corroborated":
                continue
            h = json.loads(r["reason"])[0]["citations"][0]["l1_handle"]
            if by_handle.setdefault(h, see[r["link_id"]]) != see[r["link_id"]]:
                return False
        return True

    def test_marks_only_a_row_the_keeper_stands_in_for(self):
        keeper = _row("crb-mid:aa", tier="corroborated", cites="mention:01")
        lead_cited = _row("mention:01")
        twin = _row("mention:02")
        other_marks = _row("mention:03", markings='["hr"]')
        later = _row("mention:04", recorded="2026-11-02T00:00:00Z")
        other_doc = _row("mention:05", doc="graph-mail://box/m2")
        marked = mr.decide([keeper, lead_cited, twin, other_marks, later, other_doc])
        # the lead the kept firm row cites stays; markings / recorded time differ: stays;
        # another document's mention is another pair
        self.assertEqual(marked, {"mention:02": "crb-mid:aa"})

    def test_a_weaker_corroborated_keeper_never_hides_a_stronger_view(self):
        """A corroborated keeper is a lead to a reader who may not read its supplier; a
        corroborated twin citing another record could stay corroborated: not marked."""
        keeper = _row("crb-mid:aa", tier="corroborated", handles=("graph-bc://s/1",))
        twin = _row("crb-mid:bb", tier="corroborated", handles=("graph-bc://s/2",))
        same = _row("crb-mid:cc", tier="corroborated", handles=("graph-bc://s/1",))
        text_join = _row("keyjoin:01", tier="stored_by_text_join")
        self.assertEqual(mr.decide([keeper, twin, same, text_join]),
                         {"crb-mid:cc": "crb-mid:aa"})

    def test_not_a_mention_is_never_marked(self):
        rows = [_row("L:1", kind="fk"), _row("L:2", kind="fk")]
        self.assertEqual(mr.decide(rows), {})

    def test_switch(self):
        self.assertEqual(mr.search_path({}), "ontology, public")
        self.assertEqual(mr.search_path({mr.ENV: "1"}), "ontology_seen, ontology, public")
        self.assertEqual(mr.search_path({mr.ENV: "0"}), "ontology, public")

    def test_no_step_reads_the_marker(self):
        """No build step's code names the table, the function or the schema, and none of
        the marker's files is a step's code (so no digest moves)."""
        from ontology.step_code import step_files
        files = step_files()
        mine = {"operating/pg/mention_repeats.py", "operating/pg/ontology_delta.py",
                "operating/pg/ontology_reader.py", "operating/pg/sqlite_compat.py"}
        names = re.compile(r"mention_repeats|links_seen|ontology_seen")
        for step, paths in files.items():
            self.assertFalse(mine & set(paths), step)
            for path in paths:
                self.assertIsNone(names.search(_code_of(ROOT / path)), (step, path))
        for path in (ROOT / "ontology").glob("*.py"):
            self.assertIsNone(names.search(_code_of(path)), path)


class PgTests(unittest.TestCase):
    """build / links_seen / refresh / verify / rows_hash on a throwaway database."""

    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_testdb
        from operating.pg import migrate
        cls.pg_testdb = pg_testdb
        cls.name, cls.dsn = pg_testdb.create_db()
        with pg_testdb.psycopg.connect(cls.dsn, autocommit=True) as conn:
            migrate.migrate(conn)

    @classmethod
    def tearDownClass(cls):
        cls.pg_testdb.drop_db(cls.name)

    def conn(self):
        return self.pg_testdb.psycopg.connect(self.dsn, autocommit=True)

    def load(self, rows):
        with self.conn() as c:
            c.execute("TRUNCATE ontology.link_rows")
            c.execute("TRUNCATE ontology.mention_repeats")
            for r in rows:
                c.execute("INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                          "to_object, tier, binding_kind, why_not, markings, recorded_at, "
                          "superseded_at, reason, evidence_handles) VALUES (%s, %s, %s, %s, "
                          "%s, %s, '[]', %s, %s, %s, %s, %s)",
                          (r["link_id"], r["link_type"], r["from_object"], r["to_object"],
                           r["tier"], r["binding_kind"], r["markings"], r["recorded_at"],
                           r["superseded_at"], r["reason"], r["evidence_handles"]))

    def rows(self):
        return [
            _row("crb-mid:aa", tier="corroborated", cites="mention:01"),
            _row("mention:01"), _row("mention:02"), _row("mention:03"),
            _row("mention:04", markings='["hr"]'),
            _row("mention:10", to="PurchaseOrder:2"), _row("mention:11", to="PurchaseOrder:2"),
            _row("mention:20", frm="Mail:2"),   # alone: its pair's only link
            _row("L:1", kind="fk", frm="Mail:2", to="Vendor:1", link_type="Mail.vendor"),
        ]

    def seen(self, where=""):
        with self.conn() as c:
            c.execute("SET search_path = ontology_seen, ontology, public")
            return sorted(r[0] for r in c.execute(f"SELECT link_id FROM links {where}"))

    def test_build_view_refresh_verify(self):
        rows = self.rows()
        self.load(rows)
        before = mr.rows_hash(self.dsn)
        report = mr.build(self.dsn, workers=2)
        self.assertTrue(report["complete"], report)
        self.assertEqual(mr.rows_hash(self.dsn), before)   # no link row written
        with self.conn() as c:
            stored = dict(c.execute("SELECT link_id, keeper_id FROM ontology.mention_repeats"
                                    ).fetchall())
        self.assertEqual(stored, mr.decide(rows))
        self.assertEqual(stored, {"mention:02": "crb-mid:aa", "mention:03": "crb-mid:aa",
                                  "mention:11": "mention:10"})
        # the reader's links: the read side's dedupe of the whole set, only repeats gone
        every = sorted(r["link_id"] for r in rows)
        self.assertEqual(self.seen(), sorted(set(every) - set(stored)))
        whole = collapse_mention_rows([(r["tier"], r) for r in rows])
        left = [r for r in rows if r["link_id"] not in stored]
        self.assertEqual([r["link_id"] for _, r in collapse_mention_rows(
            [(r["tier"], r) for r in left])], [r["link_id"] for _, r in whole])
        # off: ontology.links reads every row
        with self.conn() as c:
            c.execute("SET search_path = ontology, public")
            self.assertEqual(sorted(r[0] for r in c.execute("SELECT link_id FROM links")),
                             every)
        check = mr.verify(self.dsn, workers=2)
        self.assertTrue(check["ok"], check)
        # a stale marker (keeper superseded, nothing refreshed) shows the repeat
        with self.conn() as c:
            c.execute("UPDATE ontology.link_rows SET superseded_at = '2026-11-03T00:00:00Z' "
                      "WHERE link_id = 'mention:10'")
        self.assertIn("mention:11", self.seen())
        stale = mr.verify(self.dsn, workers=2, recompute=False)
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["not_hidden"], 1, stale)   # mention:11 shows, still marked
        # refresh of the pair's end: re-marked over what stands now
        got = mr.refresh(self.dsn, ["PurchaseOrder:2"])
        self.assertEqual(got["removed"], 1)
        self.assertIn("mention:11", self.seen())
        self.assertTrue(mr.verify(self.dsn, workers=2)["ok"])
        # a keeper whose evidence changed in place no longer stands in for the repeats
        with self.conn() as c:
            c.execute("UPDATE ontology.link_rows SET evidence_handles = '[\"x\"]' "
                      "WHERE link_id = 'crb-mid:aa'")
        self.assertTrue({"mention:02", "mention:03"} <= set(self.seen()))
        mr.clear(self.dsn)
        self.assertEqual(self.seen(), every)

    def test_reader_switch(self):
        """``PgStoreReader`` reads ``links`` without the repeats only with the switch on."""
        from unittest import mock
        from operating.pg.ontology_reader import PgStoreReader
        self.load(self.rows())
        mr.build(self.dsn, workers=1)
        for env, hidden in (({}, False), ({mr.ENV: "1"}, True)):
            with mock.patch.dict("os.environ", env, clear=False):
                if not env:
                    import os
                    os.environ.pop(mr.ENV, None)
                reader = PgStoreReader.open(self.dsn, only=False)
                try:
                    ids = {r[0] for r in reader.conn.execute(
                        "SELECT link_id FROM links").fetchall()}
                finally:
                    reader.close()
            self.assertEqual("mention:02" not in ids, hidden)

    def test_mail_exclusion_purge_removes_the_markers(self):
        """A purged record's id leaves the marker too (no excluded person's id stays)."""
        from operating import mail_exclusion_purge as purge
        self.load(self.rows())
        mr.build(self.dsn, workers=1)
        with self.conn() as c:
            c.execute("DELETE FROM ontology.object_versions")
            c.execute("DELETE FROM ontology.objects")
            c.execute("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                      "native_key, audience_key, first_recorded_at) VALUES "
                      "('Mail:1', 'Mail', 'b', 'k', 'a', 'x')")
            c.execute("INSERT INTO ontology.object_versions (ver, object_id, l1_handle, "
                      "digest, lineage, markings, recorded_at) VALUES (1, 'Mail:1', "
                      "'graph:mail:secret-1', 'd', 'l', '[]', 'x')")
        out = purge.purge_pg(self.dsn, ["graph:mail:secret-1"], apply=True)
        self.assertEqual(out["ontology.mention_repeats"], 3)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM ontology.mention_repeats WHERE "
                                       "from_object = 'Mail:1' OR to_object = 'Mail:1'"
                                       ).fetchone()[0], 0)

    def test_verify_runs_each_check_read_only_with_only_its_own_settings(self):
        """The served-scale not_hidden plan (no hash/merge join, so no temp spill) holds for
        that check alone, and verify cannot write."""
        self.load(self.rows())
        mr.build(self.dsn, workers=1)
        with self.conn() as c:
            c.execute("BEGIN")
            for setting, value in mr.VERIFY_SETTINGS["not_hidden"]:
                c.execute("SELECT set_config(%s, %s, true)", (setting, value))
            plan = "\n".join(r[0] for r in c.execute("EXPLAIN " + mr.VERIFY_SQL["not_hidden"]))
            c.execute("ROLLBACK")
        self.assertNotIn("Hash", plan)
        self.assertNotIn("Merge", plan)
        probe = {"after_hashjoin_off": "SELECT (current_setting('enable_hashjoin') = 'off')::int",
                 "after_mergejoin_off":
                     "SELECT (current_setting('enable_mergejoin') = 'off')::int"}
        with mock.patch.dict(mr.VERIFY_SQL, probe):
            got = mr.verify(self.dsn, workers=1, recompute=False)
        self.assertEqual((got["after_hashjoin_off"], got["after_mergejoin_off"]), (0, 0))
        self.assertTrue(got["ok"], got)
        with mock.patch.dict(mr.VERIFY_SQL, {"writes": f"DELETE FROM {mr.TABLE} RETURNING 0"}):
            with self.assertRaises(self.pg_testdb.psycopg.errors.ReadOnlySqlTransaction):
                mr.verify(self.dsn, workers=1, recompute=False)
        with self.conn() as c:
            self.assertEqual(c.execute(f"SELECT count(*) FROM {mr.TABLE}").fetchone()[0], 3)

    def test_build_refuses_while_another_runs(self):
        with self.conn() as c:
            c.execute("SELECT pg_advisory_lock(hashtext(%s))", (mr.LOCK,))
            self.assertEqual(mr.build(self.dsn, workers=1)["reason"], "busy")
            self.assertEqual(mr.refresh(self.dsn, ["Mail:1"])["skipped"], "busy")
            c.execute("SELECT pg_advisory_unlock(hashtext(%s))", (mr.LOCK,))


if __name__ == "__main__":
    unittest.main()
