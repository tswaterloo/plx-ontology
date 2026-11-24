"""Routines (``operating.assist_routines``): found in the action log with their counts and NO gate.
A planted weekly act is a clock candidate with precision 1; a planted chain (another kind of act on
the same record the day before) is a chain candidate; a weak coincidence is still stored with its low
numbers (nothing is dropped by a threshold); a date-only kind's same-day earlier act is counted as
``same_day``, never as preceding; the last third of the window is counted apart (fired / acted /
anticipated); a stored candidate is updated by a later mine, a live one never; 1/10/60 people give
exactly one clock candidate each.
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import json
import unittest

from operating import assist_routines as ar
from operating.tests import test_assist_matters as base

UTC = dt.timezone.utc
START = dt.datetime(2026, 5, 4, 9, 0, tzinfo=UTC)      # a Monday
END = START + dt.timedelta(days=84)


def act(person, kind, at, rec=None):
    return (person, kind, at, rec)


class Found(unittest.TestCase):
    def test_a_weekly_act_is_a_clock_candidate_with_its_counts(self):
        rows = [act("p1", "planner_created", START + dt.timedelta(days=1 + 7 * w, hours=3)) for w in range(12)]
        got = [c for c in ar.candidates(rows, START, END) if c["trigger"]["relation"] == "clock"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["trigger"]["weekday"], "Tue")
        self.assertEqual((got[0]["mined"]["acts"], got[0]["mined"]["support"], got[0]["mined"]["precision"]),
                         (12, 1.0, 1.0))

    def test_a_chain_is_found_and_a_weak_coincidence_is_kept_with_its_low_numbers(self):
        rows = []
        for i in range(10):          # shipment by q, the invoice by p the next day, same record
            t = START + dt.timedelta(days=3 * i, hours=2, minutes=i)   # real times, varying
            rows += [act("q", "sage_oe_shipment_t", t, f"S{i}"), act("p", "mail_x", t + dt.timedelta(hours=20), f"S{i}")]
        for i in range(7):           # p's other acts on records with no earlier act at all
            rows.append(act("p", "mail_x", START + dt.timedelta(days=2 * i, hours=5), f"Z{i}"))
        got = {(c["scope"], c["kind"], c["trigger"]["event"]): c for c in ar.candidates(rows, START, END)}
        chain = got[("p", "mail_x", "act:sage_oe_shipment_t")]
        self.assertEqual(chain["mined"]["preceded"], 10)
        self.assertEqual(chain["mined"]["support"], round(10 / 17, 3))      # kept though under 0.6
        self.assertEqual(chain["mined"]["precision"], 1.0)
        self.assertEqual(chain["trigger"]["lag_s"]["p50"], 20 * 3600)

    def test_a_date_only_kind_never_counts_a_same_day_act_as_preceding(self):
        noon = lambda d: dt.datetime(2026, 5, 4 + d, 12, 0, tzinfo=UTC)
        rows = [act("q", "sage_oe_shipment", noon(0), "R1"), act("p", "sage_ar_invoice", noon(0), "R1"),
                act("q", "sage_oe_shipment", noon(2), "R2"), act("p", "sage_ar_invoice", noon(3), "R2")]
        got = {c["trigger"]["event"]: c for c in ar.candidates(rows, START, END) if c["trigger"]["relation"] == "chain"
               and c["kind"] == "sage_ar_invoice"}
        c = got["act:sage_oe_shipment"]
        self.assertTrue(c["mined"]["date_only"])
        self.assertEqual((c["mined"]["preceded"], c["mined"]["same_day"]), (1, 1))   # R2 only; R1 said, not counted
        self.assertEqual(c["trigger"]["lag_s"]["p50"], 86400)

    def test_the_last_third_is_counted_apart_by_date(self):
        cand = {"scope": "p", "kind": "planner_created", "trigger": {"relation": "clock", "weekday": "Tue", "event": "time"}}
        rows = [act("p", "planner_created", START + dt.timedelta(days=1 + 7 * w, hours=3)) for w in range(0, 12, 2)]
        cut = ar.split_at(START, END)
        late = [r for r in rows if r[2] >= cut]
        v = ar.validation(late, cand, cut, END)
        self.assertEqual(cut, START + dt.timedelta(days=56))
        self.assertEqual((v["acts"], v["anticipated"]), (len(late), len(late)))
        self.assertGreater(v["fired"], v["acted"])        # Tuesdays with no act are counted as fired, not acted

    def test_1_10_60_people_give_one_clock_candidate_each(self):
        for n in (1, 10, 60):
            rows = [act(f"p{i}", "mail_sent", START + dt.timedelta(days=w, hours=i % 8)) for i in range(n) for w in range(3)]
            clock = [c for c in ar.candidates(rows, START, END) if c["trigger"]["relation"] == "clock"]
            self.assertEqual(sorted(c["scope"] for c in clock), sorted(f"p{i}" for i in range(n)))


class Stored(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables

    def put(self, key, person, kind, at, rec):
        self.pg.execute("INSERT INTO assist.actions (action_key, person, at, system, kind, fields, attributed_by) "
                        "VALUES (%s, %s, %s, 'sage', %s, %s, 'store_who_field')",
                        (key, person, at, kind, json.dumps({"records": [rec]})))

    def test_mine_stores_every_candidate_updates_a_candidate_and_never_a_live_routine(self):
        for w in range(12):
            self.put(f"a{w}", "p1", "planner_created", START + dt.timedelta(days=1 + 7 * w, hours=3), f"R{w}")
        out = ar.mine(self.pg, START, END)
        self.assertEqual((out["status"], out["clock"]), ("ok", 1))
        row = self.pg.execute("SELECT routine_id, status, mined->>'acts', validated->>'split' FROM assist.routines").fetchall()
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0][1], "candidate")
        self.assertIn("first two thirds", row[0][3])
        self.pg.execute("UPDATE assist.routines SET status = 'live'")
        self.put("a99", "p1", "planner_created", START + dt.timedelta(days=2, hours=3), "R99")
        ar.mine(self.pg, START, END)
        self.assertEqual(self.pg.execute("SELECT status, mined->>'acts' FROM assist.routines").fetchall(),
                         [("live", row[0][2])])                          # a live routine is left alone


if __name__ == "__main__":
    unittest.main()
