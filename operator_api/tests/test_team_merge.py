"""One person, one Team row. Rows of the snapshot join only when they share the local part of
an address at the directory's own domains, one of them has a Teams display name and the other
has none or the same one, and neither is a directory group. Two name-less rows are never
joined (a role mailbox at two brands, or two people with one first name), nor a calendar's
name with anything, nor an address outside the directory's domains. Counts add, none twice."""
from __future__ import annotations

import unittest
from collections import Counter

from operator_api import team_view as tv

DOMAINS = ("petrasoap.com", "petrasoap.onmicrosoft.com", "1hourafter.com", "petralabx.com")


def row(name, source, addresses, days=None, **kw):
    return {"name": name, "name_from": source, "addresses": addresses, "roles": kw.get("roles", []),
            "department": kw.get("department"), "days": days or {}, "hours": [1] + [0] * 23,
            "group_address": kw.get("group", False), "with": kw.get("with", {}),
            "reply": {"7": None, "28": 10}, "reply_n": {"7": 0, "28": kw.get("n", 1)}, "reply_weeks": {}}


PEOPLE = {
    "V": row("Vince Alton", "teams", ["vince@petrasoap.com", "vince2614@petrasoap.onmicrosoft.com"],
             {"2026-11-20": {"mail_sent": 3}}, department="Marketing", n=5),
    "W": row("vince2674", "address", ["vince2674@petrasoap.onmicrosoft.com", "vince@1hourafter.com"],
             {"2026-11-20": {"mail_sent": 1}, "2026-11-21": {"teams": 2}}),
    "M": row("Marie Alton", "calendar", ["admin@petrasoap.com"]),
    "X": row("admin", "address", ["admin@petrasoap.onmicrosoft.com"]),
    "C": row("info", "address", ["info@petrasoap.com"], {"2026-11-20": {"mail_sent": 4}}),
    "D": row("info", "address", ["info@petralabx.com"], {"2026-11-20": {"mail_sent": 2}}),
    "E": row("Greg Mitchell", "teams", ["greg@petrasoap.com"]),
    "F": row("Greg Other", "teams", ["greg@1hourafter.com"]),
    "G": row("dana", "address", ["dana@petrasoap.com"]),
    "H": row("dana", "address", ["dana@1hourafter.com"]),
    "T": row("Tess Roe", "teams", ["tess@petrasoap.com"]),
    "Y": row("tess", "address", ["tess@outside-vendor.com"]),   # not the directory's domain
    "L": row("Tess list", "address", ["tess@1hourafter.com"], group=True),   # a directory group
    "P": row("Pat Kay", "teams", ["pat@petrasoap.com"]),
    "Q": row("pat", "address", ["pat@petrasoap.com"]),
    "R": row("pat", "address", ["pat@1hourafter.com"]),   # two name-less rows beside one named person
}
PEOPLE["V"]["with"] = {"W": 3, "G": 2}
PEOPLE["G"]["with"] = {"V": 2}


class MergeIdentities(unittest.TestCase):
    def merge(self, shown=None, domains=DOMAINS):
        return tv.merge_identities(PEOPLE, shown or list(PEOPLE),
                                   {"V": Counter(tasks=1), "W": Counter(tasks=2)}, domains)

    def test_a_teams_named_row_and_a_nameless_row_of_one_local_part_are_one_person(self):
        merged, counts, members = self.merge()
        self.assertEqual(members["V"], frozenset({"V", "W"}))
        self.assertNotIn("W", merged)
        self.assertEqual(merged["V"]["name"], "Vince Alton")
        self.assertEqual(merged["V"]["days"]["2026-11-20"], {"mail_sent": 4})   # 3 + 1, none twice
        self.assertEqual(merged["V"]["days"]["2026-11-21"], {"teams": 2})
        self.assertEqual(counts["V"]["tasks"], 3)
        self.assertEqual(merged["V"]["reply_n"]["28"], 5)
        self.assertEqual(merged["V"]["hours"][0], 2)
        self.assertEqual(merged["V"]["department"], "Marketing")
        self.assertEqual(merged["V"]["with"], {"G": 2})   # no partner of itself

    def test_role_mailboxes_and_two_nameless_people_are_never_joined(self):
        merged, _c, members = self.merge()
        # info@ at two brands, dana at two brands: nothing says they are one
        for a, b in (("C", "D"), ("G", "H")):
            self.assertEqual((members[a], members[b]), (frozenset({a}), frozenset({b})))
            self.assertIn(a, merged)
            self.assertIn(b, merged)
        self.assertEqual(merged["C"]["days"]["2026-11-20"], {"mail_sent": 4})

    def test_two_name_less_rows_beside_one_named_person_stay_apart(self):
        _m, _c, members = self.merge()
        self.assertEqual((members["P"], members["Q"], members["R"]),
                         (frozenset({"P"}), frozenset({"Q"}), frozenset({"R"})))

    def test_only_the_directorys_own_domains_and_never_a_group_or_a_calendar_name(self):
        _m, _c, members = self.merge()
        self.assertEqual((members["T"], members["Y"], members["L"]),
                         (frozenset({"T"}), frozenset({"Y"}), frozenset({"L"})))
        self.assertEqual((members["M"], members["X"]), (frozenset({"M"}), frozenset({"X"})))   # a calendar name never anchors
        # no known domains: nothing is joined
        _m2, _c2, none = self.merge(domains=())
        self.assertTrue(all(len(v) == 1 for v in none.values()))

    def test_two_given_names_that_disagree_are_never_merged(self):
        _m, _c, members = self.merge()
        self.assertEqual((members["E"], members["F"]), (frozenset({"E"}), frozenset({"F"})))

    def test_only_rows_the_reader_may_see_are_merged(self):
        merged, _c, members = self.merge(["V", "C"])
        self.assertEqual(members["V"], frozenset({"V"}))   # W is not shown, so not folded in
        self.assertEqual(merged["V"]["days"]["2026-11-20"], {"mail_sent": 3})
        self.assertIn("W", merged)   # kept for lookups that name it, never shown

    def test_the_live_snapshot_shape_conserves_activity(self):
        merged, _c, _m = self.merge()
        total = lambda P: sum(sum(c.values()) for p in P.values() for c in p["days"].values())
        self.assertEqual(total(merged), total(PEOPLE))


if __name__ == "__main__":
    unittest.main()
