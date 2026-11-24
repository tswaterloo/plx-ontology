"""Entry sheets and routine firing on Postgres with made-up actions: a live routine opens one matter per
trigger (a repeat opens nothing, a trigger older than going live opens nothing, a record the person already
acted on opens nothing), the person's own act closes it ``entered``; a candidate never fires; a sheet takes its
fields from the trigger row, a linked record or a constant and leaves the rest to the person; compare is exact
as numbers and dates; verify writes matched / mismatch with the differing fields and an entry never keyed stays
open and uncounted; 1/10/60 triggers open exactly that many matters.
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import json
import unittest

from operating import assist_routines as ar
from operating import assist_sheets as sh
from operating.tests import test_assist_matters as base

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 2, 9, 0, tzinfo=UTC)          # a Monday


class Sheets(unittest.TestCase):
    def test_a_sheet_takes_fields_from_the_trigger_a_record_and_a_constant_and_leaves_the_rest(self):
        r = {"system": "sage", "kind": "ar_invoice", "field_sources": {
            "customer": {"from": "trigger.customer", "equal_share": 1.0},
            "terms": {"from": "record.customers.terms", "equal_share": 0.9},
            "currency": {"from": "constant", "value": "CAD", "equal_share": 1.0},
            "memo": {"from": "person"}}}
        got = sh.sheet(r, {"customer": "Croda"}, {"customers": {"terms": "Net 30"}})
        by = {f["name"]: f for f in got["fields"]}
        self.assertEqual((by["customer"]["value"], by["customer"]["sure"]), ("Croda", True))
        self.assertEqual((by["terms"]["value"], by["terms"]["sure"]), ("Net 30", False))
        self.assertEqual(by["memo"]["source"], "person")
        self.assertEqual((got["ready"], got["for_you"]), (3, 1))
        empty = sh.sheet({"kind": "x"}, {})
        self.assertIn("every field is for you", empty["note"])      # no fabricated fields

    def test_compare_is_exact_after_trimming_numbers_as_numbers_dates_as_dates(self):
        prepared = [{"name": "qty", "value": "10.0", "source": "trigger.q"}, {"name": "on", "value": "2026-11-02", "source": "trigger.d"},
                    {"name": "who", "value": " Croda ", "source": "trigger.w"}, {"name": "memo", "value": None, "source": "person"}]
        ok = sh.compare(prepared, {"qty": 10, "on": dt.date(2026, 11, 2), "who": "Croda", "memo": "anything"})
        self.assertEqual((ok["matched"], ok["checked"]), (True, 3))     # the person's own field is not compared
        bad = sh.compare(prepared, {"qty": 11, "on": "2026-11-02", "who": "Croda"})
        self.assertEqual([d["name"] for d in bad["differ"]], ["qty"])
        self.assertFalse(sh.compare([{"name": "memo", "value": None, "source": "person"}], {})["matched"])


class Fire(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables

    def act(self, key, person, kind, at, rec):
        self.pg.execute("INSERT INTO assist.actions (action_key, person, at, system, kind, fields, attributed_by) "
                        "VALUES (%s, %s, %s, 'sage', %s, %s, 'store_who_field')",
                        (key, person, at, kind, json.dumps({"records": [rec]})))

    def routine(self, status="live", relation="chain", **extra):
        trig = ({"event": "act:ship", "relation": "chain"} if relation == "chain"
                else {"event": "time", "relation": "clock", "weekday": "Tue"})
        rid = ar.routine_id("p-ann", "invoice", trig)
        self.pg.execute("INSERT INTO assist.routines (routine_id, scope, kind, trigger, mined, status, updated_at) "
                        "VALUES (%s, 'p-ann', 'invoice', %s, '{}', %s, %s)", (rid, json.dumps(trig), status, T0))
        return rid

    def matters(self):
        return self.pg.execute("SELECT owner_person, kind, state, done_reason, next_act, sources->>'routine' "
                               "FROM assist.matters ORDER BY basis_at, subject_key").fetchall()

    def test_a_live_chain_routine_opens_one_matter_per_trigger_and_the_persons_act_closes_it(self):
        rid = self.routine()
        self.act("old", "p-bob", "ship", T0 - dt.timedelta(days=2), "S0")        # before it went live: never opened
        self.act("s1", "p-bob", "ship", T0 + dt.timedelta(days=1), "S1")
        self.act("s2", "p-bob", "ship", T0 + dt.timedelta(days=1, hours=1), "S2")
        self.act("done", "p-ann", "invoice", T0 + dt.timedelta(days=1, hours=2), "S2")   # already keyed
        now = T0 + dt.timedelta(days=3)
        out = ar.open_from_routines(self.pg, now)
        self.assertEqual(out["opened"], 1)
        self.assertEqual(self.matters(), [("p-ann", "routine", "open", None, "system_write", rid)])
        self.assertEqual(ar.open_from_routines(self.pg, now)["opened"], 0)       # a repeat opens nothing
        self.act("k1", "p-ann", "invoice", T0 + dt.timedelta(days=2), "S1")
        ar.reverify(self.pg)
        self.assertEqual(self.matters()[0][2:4], ("done", "entered"))

    def test_an_act_older_than_the_trigger_neither_closes_the_matter_nor_opens_and_closes_it_in_one_cycle(self):
        self.routine()
        self.act("early", "p-ann", "invoice", T0 + dt.timedelta(hours=2), "S1")       # the person's act BEFORE the trigger
        self.act("s1", "p-bob", "ship", T0 + dt.timedelta(days=1), "S1")
        now = T0 + dt.timedelta(days=3)
        self.assertEqual(ar.open_from_routines(self.pg, now)["opened"], 1)    # an earlier act of theirs does not follow it
        self.assertEqual(ar.reverify(self.pg), {})
        self.assertEqual(self.matters()[0][2], "open")
        self.act("late", "p-ann", "invoice", T0 + dt.timedelta(days=2), "S1")
        ar.reverify(self.pg)
        self.assertEqual(self.matters()[0][2:4], ("done", "entered"))

    def test_the_routine_id_ignores_the_measured_lag(self):
        a = {"event": "act:ship", "relation": "chain", "lag_s": {"p50": 1, "p90": 2}}
        b = {"event": "act:ship", "relation": "chain", "lag_s": {"p50": 9, "p90": 99}}
        self.assertEqual(ar.routine_id("p", "k", a), ar.routine_id("p", "k", b))
        self.assertNotEqual(ar.routine_id("p", "k", a), ar.routine_id("q", "k", a))

    def test_a_candidate_never_fires_and_a_clock_routine_fires_on_its_weekday_without_the_act(self):
        self.routine(status="candidate")
        self.act("s1", "p-bob", "ship", T0 + dt.timedelta(days=1), "S1")
        self.assertEqual(ar.open_from_routines(self.pg, T0 + dt.timedelta(days=3))["opened"], 0)
        self.pg.execute("DELETE FROM assist.routines")
        rid = self.routine(relation="clock")                       # Tuesdays since Mon 11-02: 11-03 and 11-10
        out = ar.open_from_routines(self.pg, T0 + dt.timedelta(days=9))
        self.assertEqual(out["opened"], 2)
        self.act("c1", "p-ann", "invoice", T0 + dt.timedelta(days=1, hours=5), "x")      # acted on 11-03
        ar.reverify(self.pg)
        self.assertEqual([r[2] for r in self.matters()], ["done", "open"])
        self.assertTrue(ar.set_status(self.pg, rid, "retired", "p-ann"))
        with self.assertRaises(ValueError):
            ar.set_status(self.pg, rid, "passed", "p-ann")

    def test_1_10_60_triggers_open_that_many_matters(self):
        self.routine()
        for n in (1, 10, 60):
            self.pg.execute("TRUNCATE assist.actions, assist.matters, assist.matter_log")
            for i in range(n):
                self.act(f"s{i}", "p-bob", "ship", T0 + dt.timedelta(days=1, minutes=i), f"S{i}")
            self.assertEqual(ar.open_from_routines(self.pg, T0 + dt.timedelta(days=3))["opened"], n)

    def test_write_up_asks_for_the_named_routines_only_and_keeps_nothing_unanswered(self):
        a = self.routine()
        asked = []
        self.assertEqual(ar.write_up(self.pg, lambda p: asked.append(p) or " You send the invoice\n after shipping. ", [a, "none"]),
                         {"written": 1, "failed": 1})
        self.assertEqual(self.pg.execute("SELECT description FROM assist.routines").fetchone()[0], "You send the invoice after shipping.")
        self.assertEqual(len(asked), 1)
        self.assertIn("Use only the facts below", asked[0])
        ar.write_up(self.pg, lambda p: None, [a])
        self.assertEqual(self.pg.execute("SELECT description FROM assist.routines").fetchone()[0], "You send the invoice after shipping.")

    def test_verify_matches_mismatches_and_leaves_an_unkeyed_entry_open(self):
        for key, rec in (("m1", "S1"), ("m2", "S2"), ("m3", "S3")):
            mid = f"matter-{key}"
            fields = [{"name": "qty", "value": 10, "source": "trigger.q"}]
            self.pg.execute("INSERT INTO assist.matters (matter_id, owner_person, kind, subject_key, sources, basis_at, title, plan) "
                            "VALUES (%s, 'p-ann', 'routine', %s, %s, %s, 't', %s)",
                            (mid, rec, json.dumps({"records": [rec], "act_kind": "invoice"}), T0,
                             json.dumps({"prepared": {"system": "sage", "sheet": {"fields": fields}}})))
        keyed = {"S1": {"qty": "10"}, "S2": {"qty": 12}}
        out = sh.verify(self.pg, lambda rec, kind: keyed.get(rec))
        self.assertEqual((out["matched"], out["mismatch"], out["not_keyed"]), (1, 1, 1))
        ev = dict(self.pg.execute("SELECT matter_id, event FROM assist.write_journal").fetchall())
        self.assertEqual(ev, {"matter-m1": "sheet_matched", "matter-m2": "sheet_mismatch"})
        again = sh.verify(self.pg, lambda rec, kind: keyed.get(rec))
        self.assertEqual((again["open"], again["not_keyed"]), (1, 1))        # decided sheets are not asked twice


if __name__ == "__main__":
    unittest.main()
