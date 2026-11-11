"""``ObjectWorkspace.cited_by``: the records citing one source, as the reader's tools show
them (isolated-fixture: scripted tools and access, no store). The source is decided before
anything is read; a record the reader's ``plx.object`` refuses is never named; the list is
bounded however many records the source was read into or links to."""

from __future__ import annotations

import contextlib
import unittest

from ontology.types import Actor
from operator_api.workspace.object_workspace import (MAX_CITED_HOLDERS, MAX_CITED_RECORDS,
                                                     ObjectWorkspace)

HANDLE = "bc://demo/purchaseOrders/90412"


class Access:
    def __init__(self, holders, withheld=False):
        self.holders, self.withheld, self.asked = holders, withheld, []

    def request(self):
        return contextlib.nullcontext()

    def decide(self, handles):
        return {h: ({"reason": "not_granted"} if self.withheld else None) for h in handles}

    def withheld_sources(self):
        return []

    def records_of(self, handle):
        self.asked.append(handle)
        return frozenset(self.holders)


def workspace(access, *, visible, linked=0):
    calls = []

    def obj(arguments, actor):
        calls.append(("plx.object", arguments["object_id"]))
        oid = arguments["object_id"]
        if oid not in visible:
            return {"error": {"code": "object_not_found"}}
        return {"object": {"object_id": oid, "object_type": "PurchaseOrder", "display": oid},
                "linked_counts": [{"link_type": "supplies", "direction": "out",
                                   "count": linked}] if linked else []}

    def around(arguments, actor):
        calls.append(("plx.around", arguments["from"]))
        return {"objects": [{"object_id": f"SalesOrder:x:{arguments['from']}-{i}",
                             "object_type": "SalesOrder", "display": f"SO {i}"}
                            for i in range(linked)]}

    none = {"handler": lambda arguments, actor: {}}
    tools = {"plx.find": none, "plx.object": {"handler": obj},
             "plx.around": {"handler": around}, "plx.timeline": none, "plx.process": none}
    return ObjectWorkspace(tools, Actor("plx", frozenset({"staff:a"})), access=access), calls


class CitedBy(unittest.TestCase):
    def test_a_withheld_or_foreign_source_is_not_found_before_anything_is_read(self):
        access = Access(["PurchaseOrder:x:1"], withheld=True)
        view, calls = workspace(access, visible={"PurchaseOrder:x:1"})
        with self.assertRaises(KeyError):
            view.cited_by(HANDLE)
        for bad in ("teams://x/y", "", "x" * 5000):
            with self.assertRaises(ValueError):
                view.cited_by(bad)
        self.assertEqual((calls, access.asked), ([], []))

    def test_only_records_the_readers_tools_show_are_named(self):
        view, _ = workspace(Access(["PurchaseOrder:x:1", "PurchaseOrder:x:2"]),
                            visible={"PurchaseOrder:x:2"}, linked=2)
        got = view.cited_by(HANDLE + "#fields")
        ids = [r["object_id"] for r in got["records"]]
        self.assertEqual(ids, ["PurchaseOrder:x:2", "SalesOrder:x:PurchaseOrder:x:2-0",
                               "SalesOrder:x:PurchaseOrder:x:2-1"])
        self.assertEqual([r["how"] for r in got["records"]], ["from", "linked", "linked"])
        self.assertNotIn("PurchaseOrder:x:1", repr(got))
        self.assertFalse(got["truncated"])

    def test_one_ten_or_sixty_records_stay_within_the_bound_without_loss_or_repeats(self):
        for holders, linked in ((1, 0), (10, 10), (60, 60)):
            ids = [f"PurchaseOrder:x:{n:03}" for n in range(holders)]
            view, calls = workspace(Access(ids), visible=set(ids), linked=linked)
            got = view.cited_by(HANDLE)
            listed = [r["object_id"] for r in got["records"]]
            self.assertEqual(len(listed), len(set(listed)))
            expected = min(holders, MAX_CITED_HOLDERS) * (1 + linked)
            self.assertEqual(len(listed), min(expected, MAX_CITED_RECORDS), (holders, linked))
            self.assertEqual(got["truncated"], holders > MAX_CITED_HOLDERS
                             or expected > MAX_CITED_RECORDS, (holders, linked))
            self.assertLessEqual(len([c for c in calls if c[0] == "plx.object"]),
                                 MAX_CITED_HOLDERS)
            self.assertEqual(listed[:min(holders, MAX_CITED_HOLDERS)],
                             ids[:min(holders, MAX_CITED_HOLDERS)])


if __name__ == "__main__":
    unittest.main()
