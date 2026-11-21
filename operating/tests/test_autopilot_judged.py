"""What Auto rests on: a category's judged precision on held-out days
(``operating.autopilot_judged``), admitted only from a clean run and re-checked at read."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from operating import autopilot_judged as aj
from operating import autopilot_write as aw

FRESH = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


def run_doc(rows, source="set X, engine abc, judge sonnet", day="2026-11-21"):
    return {"schema": aj.RUN_SCHEMA, "source": source, "measured_on": day, "items": rows}


def rows(n, right, category="teams_replies", start=FRESH, person="a@plx.example",
         performed=True, prefix="i"):
    return [{"item": f"{prefix}{k}", "category": category, "person": person,
             "at": (start + timedelta(minutes=k)).isoformat(), "performed": performed,
             "right": k < right if performed else None} for k in range(n)]


class Judged(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.rules = aw.load_rules()
        self.bar = (Decimal(str(self.rules["gate"]["precision"])),
                    int(self.rules["gate"]["min_judged"]))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def admit(self, doc) -> dict:
        path = self.dir / "run.json"
        path.write_text(json.dumps(doc))
        return aj.record(path, self.dir, self.rules)

    def test_only_a_clean_run_on_held_out_days_is_admitted(self):
        """A run with any item on tuned days, an unknown category, a performed item with no
        verdict, a repeated item, no source or no items admits nothing at all."""
        tuned = FRESH.replace(month=10, day=28)          # 10-26..11-05 was tuned on
        june = FRESH.replace(month=6, day=15)            # sets E/F: dev on 2026-11-19
        bad = [
            rows(15, 15, start=tuned),
            rows(14, 14) + rows(1, 1, start=june, prefix="j"),
            rows(15, 15, category="no_such"),
            [{**r, "right": None} for r in rows(15, 15)],
            rows(15, 15) + rows(1, 1),                    # i0 twice
            [],
            [{**r, "person": "not a person"} for r in rows(15, 15)],
        ]
        for doc in [run_doc(b) for b in bad] + [run_doc(rows(15, 15), source=" "),
                                                 {**run_doc(rows(15, 15)), "schema": "x"}]:
            with self.assertRaises(ValueError, msg=str(doc)[:120]):
                self.admit(doc)
        self.assertFalse((self.dir / aj.JUDGED_FILE).exists())
        self.assertEqual(aj.load(self.dir, self.rules), {})

    def test_counts_are_items_performed_and_right_at_any_cardinality(self):
        """Judged = items Auto would have performed, matched = those right; a question, a
        to-do or nothing prepared is not judged. N = 1, 10, 60: no item lost or doubled."""
        for n in (1, 10, 60):
            right = n * 9 // 10
            self.admit(run_doc(rows(n, right) + rows(n, 0, performed=False, prefix="q")))
            got = aj.load(self.dir, self.rules)[(aj.ALL, "teams_replies")]
            self.assertEqual((got.judged, got.matched), (n, right))
            self.assertEqual(got.passes(*self.bar), n >= 15)
            self.assertEqual(got.source, "set X, engine abc, judge sonnet")
            self.assertEqual(got.measured_on, "2026-11-21")
            self.assertEqual(got.kind, "fresh")
        # the newest run replaces the category's score; other categories keep theirs
        self.admit(run_doc(rows(20, 20, category="customer_replies")))
        self.admit(run_doc(rows(20, 17)))
        got = aj.load(self.dir, self.rules)
        self.assertEqual((got[(aj.ALL, "teams_replies")].judged,
                          got[(aj.ALL, "teams_replies")].matched), (20, 17))
        self.assertEqual(got[(aj.ALL, "customer_replies")].matched, 20)

    def test_a_quiet_moment_acted_on_is_wrong_and_the_rubric_is_strict(self):
        yes = {"right_action": "yes", "right_values": "yes", "wrong_or_unsafe": "no"}
        self.assertTrue(aj.verdict(yes, quiet=False))
        self.assertFalse(aj.verdict(yes, quiet=True))
        for k, v in (("right_values", "partial"), ("right_action", "no"),
                     ("wrong_or_unsafe", "yes"), ("right_values", None)):
            self.assertFalse(aj.verdict({**yes, k: v}, quiet=False), k)

    def test_a_persons_own_failing_score_holds_back_what_everyone_earned(self):
        self.admit(run_doc(rows(30, 30, person="b@plx.example")
                           + rows(15, 12, person="a@plx.example", prefix="a")))
        scores = aj.load(self.dir, self.rules)
        self.assertTrue(scores[(aj.ALL, "teams_replies")].passes(*self.bar))   # 42/45
        self.assertTrue(aj.passes(scores, ["b@plx.example"], "teams_replies", *self.bar))
        self.assertFalse(aj.passes(scores, ["a@plx.example"], "teams_replies", *self.bar))
        self.assertEqual(aj.shown(scores, ["a@plx.example"], "teams_replies", *self.bar).matched, 12)
        self.assertEqual(aj.shown(scores, ["c@plx.example"], "teams_replies", *self.bar).judged, 45)
        self.assertFalse(aj.passes(scores, ["b@plx.example"], "customer_replies", *self.bar))
        # the person's own score passes but everyone's does not: the team score is shown
        self.admit(run_doc(rows(15, 15, person="b@plx.example")
                           + rows(30, 10, person="c@plx.example", prefix="c")))
        scores = aj.load(self.dir, self.rules)
        got = aj.shown(scores, ["b@plx.example"], "teams_replies", *self.bar)
        self.assertEqual((got.scope, got.judged, got.matched), ("team", 45, 25))
        got = aj.shown(scores, ["c@plx.example"], "teams_replies", *self.bar)
        self.assertEqual((got.scope, got.matched), ("you", 10))
        # the person's own score is found by their directory id too (what an opt-in carries)
        oid = "66666666-6666-4666-8666-666666666666"
        self.admit(run_doc(rows(30, 30, person="b@plx.example") + [
            {**r, "person_id": oid} for r in rows(15, 12, person="a@plx.example", prefix="a")]))
        scores = aj.load(self.dir, self.rules)
        self.assertFalse(aj.passes(scores, [oid], "teams_replies", *self.bar))   # 12/15 own

    def test_days_marked_tuned_later_drop_the_score_and_a_bad_file_passes_nothing(self):
        self.admit(run_doc(rows(20, 20)))
        later = json.loads(aw.RULES_FILE.read_text())
        later["tuned_windows"].append({"from": "2026-11-09T00:00:00Z",
                                       "until": "2026-11-10T00:00:00Z", "why": "fixture"})
        path = self.dir / "rules.json"
        path.write_text(json.dumps(later))
        self.assertEqual(aj.load(self.dir, aw.load_rules(path)), {})
        doc = json.loads((self.dir / aj.JUDGED_FILE).read_text())
        doc["scores"]["all"]["teams_replies"]["matched"] = 21          # more right than judged
        (self.dir / aj.JUDGED_FILE).write_text(json.dumps(doc))
        self.assertNotIn((aj.ALL, "teams_replies"), aj.load(self.dir, self.rules))
        (self.dir / aj.JUDGED_FILE).write_text("{not json")
        self.assertEqual(aj.load(self.dir, self.rules), {})


if __name__ == "__main__":
    unittest.main()
