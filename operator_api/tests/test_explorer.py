"""The link explorer reads a record's neighbours as the reader: grouped by kind, firm and
suggested apart, capped, and a trimmed walk says so without a number."""
from __future__ import annotations

import json
import unittest

from operator_api import explorer
from operator_api import models as m

OID = "Customer:bc.customer:{1}"


def url(kind, oid):
    return f"/object/{kind}/{oid}"


class View:
    """A scripted reader: plx.object lists link kinds, plx.around answers per link."""

    def __init__(self, links, walks, error=None):
        self.links, self.walks, self.error, self.calls = links, walks, error, []

    def _call(self, name, args):
        self.calls.append((name, args))
        if name == "plx.object":
            if self.error:
                return {"error": {"code": self.error}}
            return {"object": {"object_type": "Customer", "display": {"untrusted_text": "Acme"}},
                    "linked_counts": [{"link_type": lt, "direction": d, "count": n,
                                       "tiers": {"verified": n}}
                                      for lt, d, n in self.links]}
        hop = args["path"][0]
        return self.walks[(hop["link_type"], hop["direction"])]


def around(ids, tier="verified", *, more=False, lost=False, kind="SalesOrder"):
    objects = [{"object_id": f"{kind}:bc:{i}", "object_type": kind,
                "display": {"untrusted_text": f"SO {i}"}} for i in ids]
    out = {"objects": objects, "gaps": [],
           "edges": [{"from": OID, "to": o["object_id"], "tier": tier,
                      "link_type": "x"} for o in objects],
           "completeness": {"complete": not more, "next_cursor": "c" if more else None}}
    if lost:
        out["gaps"] = [{"reason": "withheld_by_permission"}]
        out["completeness"] = {"complete": False, "reason": "withheld_by_permission"}
    return out


class ExploreTests(unittest.TestCase):
    def run_explore(self, view):
        return explorer.explore(view, OID, open_url=url)

    def test_groups_by_kind_with_tiers_and_firm_first(self):
        view = View([("SalesOrder.ordered_by", "reverse", 2), ("Mail.mentions", "reverse", 1)], {
            ("SalesOrder.ordered_by", "reverse"): around([1, 2], "stored_by_text_join"),
            ("Mail.mentions", "reverse"): around([9], "verified", kind="Mail")})
        out = self.run_explore(view)
        self.assertEqual([g.nodes[0].type for g in out.groups], ["Mail", "SalesOrder"])
        self.assertEqual(out.groups[0].nodes[0].tier, "firm")
        self.assertEqual(out.groups[1].nodes[0].tier, "suggested")
        self.assertIn("ordered by", out.groups[1].label)
        self.assertFalse(out.partial)
        self.assertIsNone(out.message)

    def test_a_trimmed_walk_says_so_and_gives_no_number(self):
        view = View([("SalesOrder.ordered_by", "reverse", 41), ("Invoice.billed_to", "reverse", 17)], {
            ("SalesOrder.ordered_by", "reverse"): around([1], lost=True),
            ("Invoice.billed_to", "reverse"): {"objects": [], "gaps": [
                {"reason": "marking_withheld"}], "edges": [],
                "completeness": {"complete": False, "reason": "marking_withheld"}}})
        out = self.run_explore(view)
        self.assertTrue(out.partial)
        self.assertEqual(out.message, explorer.PARTIAL_MESSAGE)
        self.assertEqual(len(out.groups), 1)   # the kind with nothing readable is not shown
        text = json.dumps(out.model_dump(mode="json"))
        self.assertNotIn("41", text)
        self.assertNotIn("17", text)

    def test_nothing_visible_reads_as_nothing_visible_not_nothing_exists(self):
        out = self.run_explore(View([], {}))
        self.assertEqual(out.message, explorer.EMPTY_MESSAGE)
        self.assertEqual(out.groups, [])

    def test_a_failed_kind_is_partial_never_a_silent_zero(self):
        view = View([("A.b", "forward", 1)], {("A.b", "forward"): {"error": {"code": "boom"}}})
        out = self.run_explore(view)
        self.assertTrue(out.partial)

    def test_unreadable_record_is_not_found(self):
        for code in ("object_not_found", "invalid_input", "something_else"):
            with self.assertRaises(KeyError):
                self.run_explore(View([], {}, error=code))

    def test_cardinality_caps_per_kind_kinds_and_total(self):
        for n in (1, 10, 60):
            view = View([("SalesOrder.ordered_by", "reverse", n)],
                        {("SalesOrder.ordered_by", "reverse"): around(range(n), more=n > 8)})
            out = self.run_explore(view)
            shown = len(out.groups[0].nodes)
            self.assertEqual(shown, min(n, explorer.NODES_PER_KIND))
            self.assertEqual(out.groups[0].more, n > 8)
            self.assertEqual(out.capped, n > 8)
            ids = [x.object_id for x in out.groups[0].nodes]
            self.assertEqual(len(ids), len(set(ids)))
        links = [(f"T{i}.rel", "forward", 1) for i in range(30)]
        walks = {(lt, d): around([i], kind=f"T{i}") for i, (lt, d, _) in enumerate(links)}
        out = self.run_explore(View(links, walks))
        self.assertEqual(len(out.groups), explorer.MAX_KINDS)
        self.assertTrue(out.capped)
        self.assertLessEqual(sum(len(g.nodes) for g in out.groups), explorer.MAX_NODES)

    def test_same_record_link_reads_as_what_it_means(self):
        self.assertEqual(explorer.kind_label("Customer.same_record_as", "forward", "Customer"),
                         explorer.SAME_RECORD)


class Roundtrip(unittest.TestCase):
    def test_model_is_closed(self):
        with self.assertRaises(Exception):
            m.ExploreNode(object_id="a", type="b", label="c", open_url="/", tier="maybe")


if __name__ == "__main__":
    unittest.main()
