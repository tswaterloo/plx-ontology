"""plx.around lists the records one hop reaches most specific link first (CC-NY-3,
audit 11-14): within one tier, a document whose own name carries the number comes before
one that only lists it, a document listing fewer records before one listing hundreds
(``list_file``), and an earlier mention before a later one; so a plan's ``limit`` and
``max_each`` keep a record's own PDF ahead of the trackers that name it. Read off the
hop's own link rows (no other query). Evidence label: isolated-fixture (hand-filled store,
the real compiled schema).
"""

from __future__ import annotations

import json
import unittest

from operating.tests import test_link_tier_rule as base
from operating.ontology_tools_traverse import _specificity, with_leads

ORDER = "SalesOrder:portal.app_sales_order:so1"


def setUpModule():
    base.setUpModule()


class Specificity(unittest.TestCase):
    def store(self, files, tier="lead"):
        s = base.Store()
        base.order(s, "so1", "original")
        for key, field, start, listed in files:
            oid = s.obj(f"Attachment:graph_mail.attachments:{key}", "mail")
            s.n += 1
            reason = [{"feature": "identifier_mention", "agrees": True,
                       "identifier_kind": "so_number", "normalized": "SO1",
                       "span": [start, start + 3], "mention_group": f"g{key}",
                       "citations": [{"l1_handle": s.handles[oid], "field_path": field}]},
                      {"feature": "identity_candidate", "agrees": True,
                       "via": "identifier_index"}]
            why_not = [{"feature": "uncorroborated", "agrees": None}]
            if listed:
                why_not.append({"feature": "list_file", "agrees": False, "records": listed})
            s.conn.execute(
                "INSERT INTO links(link_id, link_type, from_object, to_object, tier, "
                "binding_kind, reason, why_not, evidence_handles, markings, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"mention:{key}", "Attachment.mentions_sales_order", oid, ORDER, tier,
                 "mention", json.dumps(reason), json.dumps(why_not),
                 json.dumps([s.handles[oid], s.handles[ORDER]]), "[]", base.T0))
        return s

    def around(self, s, **args):
        tools = base.Tools(self, s)
        out = tools._valid("plx.around", tools.around_tools.call("plx.around", dict({
            "from": ORDER, "tier_floor": "lead",
            "path": [{"link_type": "Attachment.mentions_sales_order",
                      "direction": "reverse"}]}, **args), base.STAFF))
        return [o["object_id"].rsplit(":", 1)[1] for o in with_leads(out)["objects"]]

    # keys sort the opposite way to specificity, so an object-id order fails the test
    FILES = [("a-tracker", "row.content_text", 40, 300),
             ("b-statement", "row.content_text", 9000, 12),
             ("c-report", "row.content_text", 50, 12),
             ("d-later", "row.content_text", 700, 0),
             ("e-early", "row.content_text", 20, 0),
             ("z-own-pdf", "row.name", 3, 0)]

    def test_own_document_then_fewer_records_then_earlier_mention(self):
        self.assertEqual(self.around(self.store(self.FILES)),
                         ["z-own-pdf", "e-early", "d-later", "c-report", "b-statement",
                          "a-tracker"])

    def test_the_limit_keeps_the_most_specific(self):
        self.assertEqual(self.around(self.store(self.FILES), limit=2), ["z-own-pdf", "e-early"])

    def test_solid_links_are_ranked_alike(self):
        self.assertEqual(self.around(self.store(self.FILES, tier="corroborated"), limit=3),
                         ["z-own-pdf", "e-early", "d-later"])

    def test_unreadable_features_rank_as_a_plain_mention(self):
        # malformed reason / why_not never raise and never jump the queue
        row = {"binding_kind": "mention", "reason": "not json",
               "why_not": '[{"feature": "list_file", "records": "many"}]'}
        self.assertEqual(_specificity(row), (1, 0, 0))
        self.assertEqual(_specificity({"binding_kind": "fk", "reason": "[]", "why_not": "[]"}),
                         (0, 0, 0))

    def test_cardinality_one_ten_sixty(self):
        for n in (1, 10, 60):
            files = [(f"f{i:03d}", "row.content_text", 10 * (n - i), 0) for i in range(n)]
            got = self.around(self.store(files), limit=50)
            expected = [f"f{i:03d}" for i in reversed(range(n))][:50]
            self.assertEqual(got, expected, n)


if __name__ == "__main__":
    unittest.main()
