"""The adjudication of undecided party pairs: a model's 'same' merges nothing by itself; a name
alone, two accounts of one system, an unreadable reply and a cited non-fact are never eligible;
a withheld pair is never sent; merge is refused below 48 of 50 and, when applied, a rebuild
keeps it (tier model) and never offers a rejected pair again; 1/10/60 pairs are each read once."""
from __future__ import annotations

import json
import unittest

from operating.pg import parties as P
from operating.pg import party_adjudication as A


def item(i, a_codes="bc:A", b_codes="portal:B", shared=("phone",), blocked="", withheld=""):
    return {"kind": "customer", "a": f"a{i}", "b": f"b{i}", "pair_evidence": {"why": "x"},
            "a_sheet": {"name": "Acme", "codes": a_codes}, "b_sheet": {"name": "Acme", "codes": b_codes},
            "verified_shared": list(shared), "blocked": blocked, "withheld": withheld}


def model(verdicts):
    """A stand-in: answers pair n from ``verdicts`` (a list per call is consumed in order)."""
    calls = []

    def read(batch):
        calls.append(len(batch))
        return [verdicts(it) for it in batch]
    read.calls = calls
    return read


class Reading(unittest.TestCase):
    def test_eligibility_needs_same_and_a_fact_and_no_blocked_system(self):
        it = item(1)
        ok = A.reading_of(it, {"verdict": "same", "used": ["phone"], "reason": "r"})
        self.assertTrue(ok["eligible"])
        for raw, why in ((({"verdict": "same", "used": [], "reason": "same name"}), "a name alone"),
                         ({"verdict": "same", "used": ["name"]}, "not facts"),
                         ({"verdict": "different", "used": ["phone"]}, "verdict different"),
                         ({"verdict": "cannot_tell", "used": []}, "verdict cannot_tell"),
                         (None, "unreadable"), ({"verdict": "maybe"}, "unreadable")):
            got = A.reading_of(it, raw)
            self.assertFalse(got["eligible"], raw)
            self.assertIn(why.split()[0], got["why_not"] + got["status"], raw)
        self.assertEqual(A.reading_of(it, None)["status"], "failed")
        self.assertEqual(A.reading_of(it, {"verdict": "same", "used": ["name"]})["status"], "invalid")
        for used in (["currency"], ["chain"], ["salesperson", "source_systems"]):
            got = A.reading_of(item(4, shared=("currency", "chain", "salesperson")),
                               {"verdict": "same", "used": used})
            self.assertFalse(got["eligible"], used)   # context facts are never identity
        claimed = A.reading_of(item(5, shared=()), {"verdict": "same", "used": ["phone"]})
        self.assertFalse(claimed["eligible"])         # the code found no phone equal
        self.assertTrue(A.reading_of(item(6, shared=()), {"verdict": "same",
                                                          "used": ["activity"]})["eligible"])
        two = A.reading_of(item(2, blocked="two sage accounts"),
                           {"verdict": "same", "used": ["phone"]})
        self.assertFalse(two["eligible"])
        self.assertEqual(two["why_not"], "two sage accounts")

    def test_two_accounts_of_one_system_are_blocked_by_the_same_rule_as_the_build(self):
        a = {"codes": "sage:ROYA03"}
        b = {"codes": "sage:ROYA04"}
        self.assertEqual(A._blocked(a, b), "two sage accounts")
        self.assertEqual(A._blocked({"codes": "sage:X"}, {"codes": "portal:7"}), "")

    def test_shared_facts_are_found_in_code_and_a_name_is_none_of_them(self):
        a = {"name": "Acme", "phone": "(416) 743-1347", "email": "A@x.ca", "codes": "bc:A1",
             "contact_person": "Jo  Smith"}
        b = {"name": "Acme", "phone": "416.743.1347", "email": "a@x.ca", "codes": "sage:A1",
             "contact_person": "jo smith"}
        self.assertEqual(sorted(A.shared(a, b)), ["contact_person", "email", "phone"])
        self.assertEqual(A.shared({"name": "Acme"}, {"name": "Acme"}), [])


class Judging(unittest.TestCase):
    def test_each_pair_is_read_once_in_batches_for_1_10_60(self):
        for n in (1, 10, 60):
            items = [item(i) for i in range(n)]
            read = model(lambda it: {"verdict": "same", "used": ["phone"], "reason": "r"})
            rows, left = A.judge(items, read, "stand-in")
            self.assertEqual((len(rows), left), (n, 0))
            self.assertEqual(sorted((r["a"], r["b"]) for r in rows),
                             sorted((i["a"], i["b"]) for i in items))
            self.assertEqual(sum(read.calls), n)
            self.assertEqual(len(read.calls), -(-n // A.BATCH))

    def test_withheld_pairs_are_not_sent_and_a_failed_batch_is_stored_failed(self):
        items = [item(1, withheld="freightcom"), item(2)]
        read = model(lambda it: {"verdict": "same", "used": ["phone"]})
        rows, _ = A.judge(items, read, "m")
        self.assertEqual(sum(read.calls), 1)
        by = {r["a"]: r for r in rows}
        self.assertEqual((by["a1"]["status"], by["a1"]["eligible"]), ("withheld", False))
        rows, _ = A.judge([item(3)], lambda batch: None, "m")
        self.assertEqual((rows[0]["status"], rows[0]["verdict"], rows[0]["eligible"]),
                         ("failed", "cannot_tell", False))

    def test_a_deadline_says_what_is_left(self):
        items = [item(i) for i in range(20)]
        rows, left = A.judge(items, model(lambda it: None), "m", deadline=0.0)
        self.assertEqual((len(rows), left), (0, 20))

    def test_parse_pairs_the_reply_by_number_and_drops_a_doubled_answer(self):
        text = 'sure [{"n":1,"verdict":"same"},{"n":2,"verdict":"different"},{"n":2,"verdict":"same"}]'
        got = A.parse(text, 3)
        self.assertEqual(got[0]["verdict"], "same")
        self.assertIsNone(got[1])
        self.assertIsNone(got[2])
        self.assertEqual(A.parse("no json", 2), [None, None])


class Pairs(unittest.TestCase):
    def test_a_pair_listed_for_two_reasons_is_read_once_with_both(self):
        built = {"_parties": [(1, "customer", "A", None, 0, 0)],
                 "_members": [("a", 1), ("b", 1)],
                 "_candidates": [("a", "b", json.dumps({"name": "x", "why": "same name"}), "undecided", None),
                                 ("a", "b", json.dumps({"lead_link": "lead"}), "undecided", None),
                                 ("a", "c", "{}", "merged", "model")]}
        got = A._undecided(built)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][:3], ("customer", "a", "b"))
        self.assertEqual(set(got[0][3]), {"name", "why", "lead_link"})


class Merge(unittest.TestCase):
    def test_merge_is_refused_below_the_sample_bar(self):
        for n, right in ((50, 47), (49, 49), (10, 10)):
            with self.assertRaises(A.SampleNotMet):
                A.merge(None, n, right)

    def test_a_decided_pair_outlives_a_rebuild_and_a_rejected_one_is_not_offered_again(self):
        rows = [{"handle": h, "name": n, "codes": c} for h, n, c in (
            ("a", "Acme", "bc:A"), ("b", "Acme Ltd", "portal:B"), ("c", "Beta", "bc:C"),
            ("d", "Beta Inc", "portal:D"), ("e", "Gamma", "bc:E"), ("f", "Gamma", "portal:F"))]
        links = [("a", "b", "lead"), ("c", "d", "lead"), ("e", "f", "lead")]
        sets, und = P.decide(rows, links)
        self.assertEqual({(a, b) for a, b, _ in und}, {("a", "b"), ("c", "d"), ("e", "f")})
        sets, und = P.decide(rows, links, [("a", "b", "merged", "model"),
                                           ("c", "d", "rejected", "person"),
                                           ("x", "y", "merged", "model")])   # gone rows ignored
        self.assertEqual(sets.find("a"), sets.find("b"))
        self.assertEqual(sets.why["a"][0], "model")
        self.assertNotEqual(sets.find("c"), sets.find("d"))
        self.assertEqual({(a, b) for a, b, _ in und}, {("e", "f")})
        sets, _ = P.decide(rows, links, [("a", "b", "merged", "person")])
        self.assertEqual(sets.why["a"][0], "person")


if __name__ == "__main__":
    unittest.main()
