"""Date declarations (ASK-GENIE 20 #1): a declaration is only as good as the profile that backs
it, the catalog carries the declared dates first, and an unreadable file is an error."""
import copy
import json
import unittest
import unittest.mock
from types import SimpleNamespace

from operating.pg import date_declare as dd
from operating.pg import generic_views as gv


def _profile(records=100, dated=100, filled=100):
    return {"types": {"T": {"records": records, "fields": {
        "docdate": {"cells": records, "filled": filled, "dated": dated, "oldest": "2020-01-01",
                    "newest": "2026-11-01", "days": 50, "top_day_share": 0.1, "future": 0,
                    "same_as_changed_day": 0.0, "same_as_first_seen_day": 0.0,
                    "samples": ["a = 20201231"]}}}}}


def _decl(**over):
    d = {"field": "docdate", "role": "record", "means": "document date", "dated_share": 1.0,
         "evidence": {"samples": ["a = 20201231"]}}
    d.update(over)
    return {"types": {"T": {"records": 100, "dates": [d]}}}


class Declarations(unittest.TestCase):
    def test_supported_declaration_has_no_problems(self):
        self.assertEqual(dd.problems(_decl(), _profile()), [])

    def test_fails_closed_on_every_unsupported_claim(self):
        cases = {
            "no meaning": (_decl(means=""), _profile()),
            "bad role": (_decl(role="guess"), _profile()),
            "no evidence": (_decl(evidence={}), _profile()),
            "share not what the profile has": (_decl(dated_share=0.9), _profile()),
            "not in the profile": (_decl(), {"types": {}}),
            "field has no day values": (_decl(field="x"), _profile()),
            "filled cells are not days": (_decl(dated_share=0.5), _profile(dated=50, filled=100)),
        }
        for name, (decl, prof) in cases.items():
            with self.subTest(name):
                self.assertTrue(dd.problems(decl, prof), name)

    def test_undated_type_needs_its_reason(self):
        self.assertTrue(dd.problems({"types": {"T": {"records": 1, "dates": []}}}, _profile()))
        self.assertFalse(dd.problems(
            {"types": {"T": {"records": 1, "dates": [], "undated": "no day field"}}}, _profile()))

    def test_unreadable_file_is_an_error_not_an_empty_set(self):
        from pathlib import Path
        with self.assertRaises(OSError):
            dd.load(Path("/nonexistent/date_declarations.json"))


class Shipped(unittest.TestCase):
    def test_every_shipped_declaration_is_complete(self):
        decl = dd.load()
        self.assertGreater(len(decl["types"]), 200)
        for type_id, entry in decl["types"].items():
            if not entry["dates"]:
                self.assertTrue(entry.get("undated"), type_id)
            for d in entry["dates"]:
                self.assertIn(d["role"], dd.ROLES, type_id)
                self.assertTrue(d["means"], type_id)
                self.assertTrue(d["evidence"]["samples"], f"{type_id}.{d['field']}: no sample rows")

    def test_catalog_rows_put_the_declared_date_of_record_first(self):
        decl = dd.load()["types"]
        tid = next(t for t, e in decl.items() if e["dates"] and e["records"] > 1000)
        schema = SimpleNamespace(object_types={tid: {"properties": [
            {"name": "other_day", "value_type": "date"}]}}, link_types={})
        rows = gv.catalog_rows(schema, {tid: (5, None, ["b"])}, "now")
        self.assertEqual(rows[0]["date_fields"][0], decl[tid]["dates"][0]["field"])
        self.assertIn("other_day", rows[0]["date_fields"])
        self.assertEqual(rows[0]["dates"][0]["field"], decl[tid]["dates"][0]["field"])
        self.assertTrue(json.dumps(rows[0]["dates"]))

    def test_day_sql_reads_only_real_days(self):
        import re
        iso, compact = re.compile(dd.ISO.replace("(", "(", 1)), re.compile(dd.COMPACT)
        self.assertTrue(compact.match("20261105"))
        self.assertFalse(compact.match("20261305"))      # month 13
        self.assertFalse(compact.match("23919000"))      # a time of day
        self.assertTrue(iso.match("2026-11-05T01:02:03Z"))


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


@unittest.skipIf(_DSN is None, "no scratch Postgres")
class Apply(unittest.TestCase):
    """``apply`` writes the declared day of records that have none into the side table
    (never into ``biz.records``), under the WAL budget's lag and disk guards."""

    def setUp(self):
        self.pg = psycopg.connect(_DSN, autocommit=True)
        self.pg.execute("DROP SCHEMA IF EXISTS biz CASCADE; CREATE SCHEMA biz; CREATE TABLE "
                        "biz.records (rid bigint PRIMARY KEY, type text, record_date text, "
                        "ver bigint, fields_open jsonb)")
        self.pg.execute(gv.DATES_DDL.format(s="biz"))
        self.pg.execute("CREATE TABLE biz.record_types (type text, rows bigint, date_fields text[]); "
                        "INSERT INTO biz.record_types VALUES ('T', 4, '{docdate,audtdate}'), "
                        "('U', 1, '{docdate}')")
        rows = [(1, "T", None, {"docdate": "20261105", "audtdate": "20261106"}),
                (2, "T", None, {"docdate": "0", "audtdate": "20261107"}),     # unset: next field
                (3, "T", None, {"docdate": "20261399"}),                      # not a real day
                (4, "T", "2020-01-01", {"docdate": "20261105"}),              # already dated
                (5, "U", None, {"docdate": "20261105"})]                      # another type
        for r in rows:
            self.pg.execute("INSERT INTO biz.records VALUES (%s, %s, %s, %s, %s::jsonb)",
                            (r[0], r[1], r[2], r[0] * 10, json.dumps(r[3])))

    def tearDown(self):
        self.pg.close()

    def side(self):
        return {r[0]: r[1:] for r in self.pg.execute(
            "SELECT rid, day, field, ver FROM biz.record_dates ORDER BY rid").fetchall()}

    def budget(self, lag=0, free=10**12, floor=75 * 1024 ** 3):
        from operating.pg import wal_budget
        return wal_budget.Budget(lambda: (lag, "standby"), free_path="/", min_free=floor,
                                 free=lambda path: free, check_seconds=0.0, log=None, job="t")

    def test_writes_only_declared_days_on_records_without_one(self):
        before = self.pg.execute("SELECT rid, record_date, fields_open FROM biz.records "
                                 "ORDER BY rid").fetchall()
        n = dd.apply_type(self.pg, "T", ["docdate", "audtdate"], budget=self.budget())
        again = dd.apply_type(self.pg, "T", ["docdate", "audtdate"], budget=self.budget())
        self.assertEqual((n, again), (3, 0))
        # rid 4 holds an older rule's day (2020-01-01): the declared day is written beside it
        self.assertEqual(self.side(), {1: ("2026-11-05", "docdate", 10),
                                       2: ("2026-11-07", "audtdate", 20),
                                       4: ("2026-11-05", "docdate", 40)})
        self.assertEqual(self.pg.execute("SELECT rid, record_date, fields_open FROM "
                                         "biz.records ORDER BY rid").fetchall(), before)
        got = dd.declared_shares(self.pg, {"types": {"T": {"dates": [{"field": "docdate"}]}}})
        self.assertEqual(got["T"], (4, 2))
        self.assertEqual(dd.dated_shares(self.pg)["T"], (4, 3))   # rid 3 has no day at all
        self.pg.execute("DELETE FROM biz.record_dates")
        self.assertEqual(dd.dated_shares(self.pg)["T"], (4, 1))   # only the build's own day

    def test_a_changed_record_is_written_again_and_a_stale_row_pruned(self):
        self.pg.execute("UPDATE biz.records SET record_date = '2026-11-05' WHERE rid = 4")
        dd.apply_type(self.pg, "T", ["docdate"], budget=self.budget())
        self.assertNotIn(4, self.side())             # the build's day already is the declared day
        self.pg.execute("UPDATE biz.records SET ver = 11, fields_open = '{\"docdate\": "
                        "\"20270101\"}' WHERE rid = 1")
        self.assertEqual(dd.prune(self.pg), 1)       # rid 1's row is for a version gone
        dd.apply_type(self.pg, "T", ["docdate"], budget=self.budget())
        self.assertEqual(self.side()[1], ("2027-01-01", "docdate", 11))

    def test_apply_refuses_while_the_catalog_lacks_the_declared_dates(self):
        self.pg.execute("UPDATE biz.record_types SET date_fields = '{other_day}' WHERE type = 'T'")
        with self.assertRaises(dd.CatalogBehind):
            dd.apply_type(self.pg, "T", ["docdate", "audtdate"], budget=self.budget())
        self.assertEqual(self.side(), {})
        self.assertEqual(dd.catalog_behind(self.pg, {"types": {"T": {"dates": [
            {"field": "docdate"}]}, "Z": {"dates": [{"field": "x"}]}}}), ["T", "Z"])

    def test_disk_below_the_floor_stops_the_job_before_it_writes(self):
        from operating.pg import wal_budget
        with unittest.mock.patch.object(dd, "_longest", return_value=0.2):
            with self.assertRaises(wal_budget.BudgetTimeout) as ctx:
                dd.apply_type(self.pg, "T", ["docdate"], budget=self.budget(free=70 * 1024 ** 3))
        self.assertIn("disk_below_min_free", str(ctx.exception))
        self.assertEqual(self.side(), {})

    def test_standby_lag_stops_the_job_and_an_unreadable_lag_fails_closed(self):
        from operating.pg import wal_budget
        with unittest.mock.patch.object(dd, "_longest", return_value=0.2):
            with self.assertRaises(wal_budget.BudgetTimeout) as ctx:
                dd.apply_type(self.pg, "T", ["docdate"], budget=self.budget(lag=9 * 1024 ** 3))
            self.assertIn("standby_lag", str(ctx.exception))
            b = self.budget()
            b.read_lag = unittest.mock.Mock(side_effect=RuntimeError("no view"))
            with self.assertRaises(wal_budget.BudgetTimeout):
                dd.apply_type(self.pg, "T", ["docdate"], budget=b)
        self.assertEqual(self.side(), {})


class Stamps(unittest.TestCase):
    """A last-write stamp is never presented as a business date (review 11-24)."""

    def test_every_stamp_column_is_declared_a_stamp_and_only_those(self):
        for type_id, entry in dd.load()["types"].items():
            for d in entry["dates"]:
                self.assertEqual(d["role"] == "stamp", d["field"] in dd.STAMP_FIELDS,
                                 f"{type_id}.{d['field']}")

    def test_a_business_date_filled_in_90_percent_leads_over_a_stamp(self):
        stamped = 0
        for type_id, entry in dd.load()["types"].items():
            if not entry["dates"] or entry["dates"][0]["role"] != "stamp":
                continue
            stamped += entry["records"]
            for d in entry["dates"][1:]:
                self.assertFalse(d["role"] != "stamp" and d["dated_share"] >= dd.LEAD_SHARE,
                                 f"{type_id}: {d['field']} should lead")
        self.assertGreater(stamped, 1_000_000)   # the Sage types whose only day is audtdate
        sage = dd.load()["types"]["RelSageArIbs"]["dates"]
        self.assertEqual((sage[0]["field"], sage[0]["role"]), ("datedue", "record"))
        self.assertIn(("audtdate", "stamp"), [(d["field"], d["role"]) for d in sage])

    def test_the_check_rejects_a_stamp_declared_as_a_record_or_a_stamp_leading_a_filled_date(self):
        prof = _profile()
        bad = _decl(field="docdate", role="stamp")
        self.assertTrue(any("last-write" in m for m in dd.problems(bad, prof)))
        lead = {"types": {"T": {"records": 100, "dates": [
            {"field": "audtdate", "role": "stamp", "means": "x", "dated_share": 1.0,
             "evidence": {"samples": ["a"]}},
            {"field": "docdate", "role": "record", "means": "x", "dated_share": 0.95,
             "evidence": {"samples": ["a"]}}]}}}
        p2 = _profile(dated=95, filled=95)
        p2["types"]["T"]["fields"]["audtdate"] = dict(p2["types"]["T"]["fields"]["docdate"],
                                                      dated=100, filled=100)
        self.assertTrue(any("business date" in m for m in dd.problems(lead, p2)))

    def test_the_prompt_and_the_dictionary_mark_every_stamp_shown(self):
        from operating import ontology_tools_sql_catalog as cat
        self.assertEqual(cat.date_bit(["audtdate", "docdate"], {"audtdate"}),
                         "date audtdate/docdate (audtdate: last-write stamp, not a business date)")
        self.assertEqual(cat.date_bit(["docdate", "audtdate"], {"audtdate"}),
                         "date docdate/audtdate (audtdate: last-write stamp, not a business date)")
        self.assertEqual(cat.date_bit(["docdate", "x"], {"audtdate"}), "date docdate/x")
        self.assertEqual(cat.date_bit([], {"audtdate"}), "")
        about = cat.GENERIC_ABOUT["records"][1]["record_date"]
        self.assertIn("stamp", about)
        self.assertIn("stamp", cat.GENERIC_ABOUT["record_types"][1]["dates"])


if __name__ == "__main__":
    unittest.main()
