"""An identifier two systems both carry (a Portal record and its FileMaker twin) is one
root when the question names the system just before it, or when the resolver matched both
as the same thing; it stays "which did you mean" otherwise (link-coverage eval 11-13: 13 of
the first 25 dev answers were a "which did you mean" on "the FileMaker batch ...")."""

from __future__ import annotations

import unittest

from operating import objective_runner

PORTAL = 'Batch:portal.app_batch_record:{"id":"p1"}'
FM = 'Batch:portal.fm_batch:{"id":1}'
PORTAL_AO = 'AssemblyOrder:portal.app_manufacturing_order:{"id":"p2"}'
FM_AO = 'AssemblyOrder:portal.fm_assembly_order:{"id":2}'


def finder(by_code: dict[str, list[str]], entities: list[dict] | None = None):
    def find(arguments):
        if arguments.get("mode") == "entities":
            return {"status": "resolved", "entities": entities or []}
        ids = by_code.get(arguments.get("query"), [])
        return {"status": "not_found" if not ids else "found" if len(ids) == 1
                else "ambiguous",
                "candidates": [{"object": {"object_id": i, "object_type": i.split(":")[0]}}
                               for i in ids]}
    return find


class NamedSystem(unittest.TestCase):
    def test_the_system_named_before_the_identifier_picks_its_record(self):
        find = finder({"BC-12345Z": [PORTAL, FM]})
        found = objective_runner.find_roots(
            "How many filled into orders are there for the FileMaker batch BC-12345Z?", find)
        self.assertEqual([r["object_id"] for r in found["roots"]], [FM])
        self.assertEqual(found["roots"][0]["found_by"], "identifier_system")
        self.assertEqual(found["ambiguous"], [])

    def test_a_system_named_elsewhere_does_not_qualify_it(self):
        find = finder({"BC-12345Z": [PORTAL, FM]})
        found = objective_runner.find_roots(
            "The Portal batch BC-12345Z is the same record as a batch in FileMaker. "
            "How many orders does that record have?", find)
        self.assertEqual([r["object_id"] for r in found["roots"]], [PORTAL])
        found = objective_runner.find_roots(
            "BC-12345Z: how many orders does the batch in FileMaker have?", find)
        self.assertEqual(found["roots"], [])
        self.assertEqual(len(found["ambiguous"]), 1)

    def test_a_system_qualifying_an_earlier_identifier_does_not_carry_over(self):
        find = finder({"BC-12345Z": [PORTAL], "BC-67890Y": [PORTAL, FM]})
        found = objective_runner.find_roots(
            "Compare the Portal batch BC-12345Z with BC-67890Y.", find)
        self.assertEqual([r["object_id"] for r in found["roots"]], [PORTAL])
        self.assertEqual([a["mention"] for a in found["ambiguous"]], ["BC-67890Y"])

    def test_no_system_or_two_systems_stay_ambiguous(self):
        find = finder({"AO-12345Z": [PORTAL_AO, FM_AO]})
        for question in ("Count the components of AO AO-12345Z.",
                         "Count the components of the Portal or FileMaker AO AO-12345Z."):
            found = objective_runner.find_roots(question, find)
            self.assertEqual(found["roots"], [], question)
            self.assertEqual(len(found["ambiguous"]), 1, question)

    def test_two_records_of_the_named_system_stay_ambiguous(self):
        other = 'Batch:portal.fm_batch:{"id":3}'
        find = finder({"BC-12345Z": [PORTAL, FM, other]})
        found = objective_runner.find_roots(
            "How many orders for the FileMaker batch BC-12345Z?", find)
        self.assertEqual(found["roots"], [])
        self.assertEqual(len(found["ambiguous"]), 1)


class SameRecord(unittest.TestCase):
    def entity(self, status_fm="match"):
        return {"mention": "AO-12345Z", "object_type": "AssemblyOrder", "confidence": 0.95,
                "status": "match", "systems": ["Portal", "FileMaker"],
                "records": [{"object": {"object_id": PORTAL_AO}, "system": "Portal",
                             "confidence": 0.95, "status": "match"},
                            {"object": {"object_id": FM_AO}, "system": "FileMaker",
                             "confidence": 0.95, "status": status_fm}]}

    def test_copies_the_resolver_matched_as_one_are_one_root(self):
        find = finder({"AO-12345Z": [PORTAL_AO, FM_AO]}, [self.entity()])
        found = objective_runner.find_roots("Count the components of AO AO-12345Z.", find)
        self.assertEqual(found["ambiguous"], [])
        self.assertEqual(len(found["roots"]), 1)
        root = found["roots"][0]
        self.assertEqual({root["object_id"], *root["same_as"]}, {PORTAL_AO, FM_AO})
        self.assertEqual(root["found_by"], "identifier_same_record")

    def test_a_lead_twin_is_never_folded_in(self):
        find = finder({"AO-12345Z": [PORTAL_AO, FM_AO]}, [self.entity(status_fm="lead")])
        found = objective_runner.find_roots("Count the components of AO AO-12345Z.", find)
        self.assertEqual(found["roots"], [])
        self.assertEqual(len(found["ambiguous"]), 1)


if __name__ == "__main__":
    unittest.main()
