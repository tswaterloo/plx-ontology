"""The briefing personalised to the signed-in reader (operating/involvement.py through
operating/briefing.py): two readers get different, correctly scoped briefings; the
reader's own items rank first; a restricted reader never gets a withheld item and a
message it may not open never makes an item its own; the owner gets the company-wide
view with their own items first; involvement is read as the reader, from the schema's
links and the reader's confirmed identity.

SYNTHETIC fixtures only (a fake permissioned tool host, synthetic ids and addresses).
Evidence label: isolated-fixture.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from operating import briefing as bf
from operating import involvement as inv
from operator_api.backend import yours_words

ANN, BOB, CARL, OWNER = (f"0000000{n}-0000-4000-8000-000000000000" for n in range(1, 5))
ADDRESS = {ANN: "ann@example.test", BOB: "bob@example.test", CARL: "carl@example.test",
           OWNER: "owner@example.test"}

SCHEMA = SimpleNamespace(
    object_types={"Email": {"properties": [{"name": "mailbox"}, {"name": "subject"}]}},
    link_types={
        "Email.sent_by_supplier": {"from": "Email", "to": "Supplier", "name": "sent_by_supplier"},
        "Email.sent_by_customer": {"from": "Email", "to": "Customer", "name": "sent_by_customer"},
        "Email.mentions_purchase_order": {"from": "Email", "to": "PurchaseOrder",
                                          "name": "mentions_purchase_order"},
        "Email.mentions_sales_order": {"from": "Email", "to": "SalesOrder",
                                       "name": "mentions_sales_order"},
        "PurchaseOrder.ordered_from": {"from": "PurchaseOrder", "to": "Supplier"},
        "SalesOrder.ordered_by": {"from": "SalesOrder", "to": "Customer"},
    })


class Host:
    """A permissioned tool host: each object is visible to the readers listed (None: to
    everyone); ``plx.around`` answers one hop with the target's ``eq`` filters, only over
    what the reader may see, as the real primitives do."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[dict, frozenset | None]] = {}
        self.links: list[tuple[str, str, str]] = []
        self.asked: list[tuple[str, str, str]] = []

    def add(self, oid: str, props: dict | None = None, visible: set | None = None) -> None:
        self.objects[oid] = (props or {}, None if visible is None else frozenset(visible))

    def link(self, link: str, frm: str, to: str) -> None:
        self.links.append((link, frm, to))

    def sees(self, actor: str, oid: str) -> bool:
        held = self.objects.get(oid)
        return held is not None and (held[1] is None or actor in held[1])

    def __call__(self, name: str, args: dict, actor: str) -> dict:
        root = args.get("object_id") or args.get("from")
        self.asked.append((name, root, actor))
        if not self.sees(actor, root):
            return {"error": {"code": "object_not_found"}}
        if name == "plx.object":
            return {"object": {"object_id": root},
                    "values": [{"property": p, "value": v}
                               for p, v in self.objects[root][0].items()]}
        hop = args["path"][0]
        forward = hop["direction"] == "forward"
        out = []
        for link, frm, to in self.links:
            if link != hop["link_type"] or (frm if forward else to) != root:
                continue
            other = to if forward else frm
            if not self.sees(actor, other):
                continue
            props = self.objects[other][0]
            if all(props.get(f["property"]) == f["value"] for f in args.get("filters", ())):
                out.append({"object_id": other})
        return {"objects": out[:args.get("limit", 50)]}


def identity(oid: str) -> inv.Identity:
    entitlement = SimpleNamespace(actor=object(), principal=SimpleNamespace(object_id=oid),
                                  source_identities=frozenset({ADDRESS[oid].upper(), oid,
                                                               "group:everyone"}))
    return inv.Identity.of(entitlement)


def gap(kind: str, oid: str, *, days: int | None = None, event: dict | None = None) -> dict:
    return {"id": f"g-{oid}", "kind": kind, "object_id": oid,
            "object_type": oid.split(":", 1)[0], "state": "open", "summary": f"{kind} {oid}",
            "days_late": days, "event": event or {}, "citations": [f"h:{oid}"],
            "first_seen": "2026-11-06T06:00:00Z", "follow_up": {}}


def inbox(*gaps: dict) -> dict:
    return {"status": "read", "gaps": list(gaps), "run_at": "2026-11-07T06:00:00Z",
            "complete": True}


def world() -> tuple[Host, dict]:
    h = Host()
    for oid in ("PurchaseOrder:1", "PurchaseOrder:2", "SalesOrder:1",
                "Supplier:1", "Supplier:2", "Customer:1"):
        h.add(oid)
    h.add("PurchaseOrder:3", {"buyer": ADDRESS[ANN], "vendor_name": "Ann Example"})
    h.link("PurchaseOrder.ordered_from", "PurchaseOrder:1", "Supplier:1")
    h.link("PurchaseOrder.ordered_from", "PurchaseOrder:2", "Supplier:2")
    h.link("PurchaseOrder.ordered_from", "PurchaseOrder:3", "Supplier:2")
    h.link("SalesOrder.ordered_by", "SalesOrder:1", "Customer:1")
    # Ann's own mailbox holds a message from supplier 1
    h.add("Email:a", {"mailbox": ANN}, visible={ANN, OWNER})
    h.link("Email.sent_by_supplier", "Email:a", "Supplier:1")
    # Bob's mailbox mentions the sales order
    h.add("Email:b", {"mailbox": BOB}, visible={BOB, OWNER})
    h.link("Email.mentions_sales_order", "Email:b", "SalesOrder:1")
    # the owner's own mail mentions PO 2
    h.add("Email:o", {"mailbox": OWNER}, visible={OWNER})
    h.link("Email.mentions_purchase_order", "Email:o", "PurchaseOrder:2")
    # Carl's mailbox holds a message from supplier 2 that Carl may not open (marked)
    h.add("Email:c", {"mailbox": CARL}, visible={OWNER})
    h.link("Email.sent_by_supplier", "Email:c", "Supplier:2")
    gaps = {"po1": gap("late_po", "PurchaseOrder:1", days=3),
            "po2": gap("late_po", "PurchaseOrder:2", days=20),
            "po3": gap("eta_slipped", "PurchaseOrder:3", event={"days": 2}),
            "so1": gap("late_customer_order", "SalesOrder:1", days=5)}
    return h, gaps


def brief(h: Host, reader: str, gaps: list[dict], routing: bf.Routing, **kw) -> dict:
    who = identity(reader)
    # these two types are off in the alert registry unless the reader switches them on
    return bf.build(inbox(*gaps), routing=routing, hidden={}, last_visit=None,
                    enabled=("eta_slipped", "customer_order_late"),
                    involved=lambda ids: inv.derive(h, reader, SCHEMA, who, ids, **kw))


PROC = bf.Routing(frozenset({"PROC"}))
SALES = bf.Routing(frozenset({"SALES"}))
OWNER_ROUTING = bf.Routing(frozenset({"EXEC"}))


class MyBriefingTest(unittest.TestCase):

    def test_two_readers_get_their_own_items_first_and_only_their_own(self):
        h, g = world()
        every = list(g.values())
        ann = brief(h, ANN, every, PROC)
        bob = brief(h, BOB, every, SALES)
        ann_ids = [i["object_id"] for i in ann["items"]]
        # Ann: her supplier's PO and the PO naming her as buyer first, then her team's
        self.assertEqual(set(ann_ids[:2]), {"PurchaseOrder:1", "PurchaseOrder:3"})
        self.assertEqual(ann_ids[2:], ["PurchaseOrder:2"])
        self.assertEqual((ann["yours"], ann["other_teams"]), (2, 1))
        why = {i["object_id"]: [r["reason"] for r in i["involvement"]] for i in ann["items"]}
        self.assertEqual(why["PurchaseOrder:1"], ["you_correspond"])
        self.assertEqual(why["PurchaseOrder:3"], ["named"])
        # a name in another property is never identity (conservative)
        self.assertEqual(ann["items"][ann_ids.index("PurchaseOrder:3")]["involvement"],
                         [{"reason": "named", "via": "buyer"}])
        # Bob: the order his mail mentions, nothing of Ann's
        self.assertEqual([i["object_id"] for i in bob["items"]], ["SalesOrder:1"])
        self.assertEqual((bob["yours"], bob["other_teams"]), (1, 3))
        self.assertEqual(bob["items"][0]["involvement"],
                         [{"reason": "your_mail", "via": "Email:b"}])
        self.assertTrue(ann["involvement"]["complete"] and bob["involvement"]["complete"])

    def test_an_own_item_reaches_the_reader_even_when_not_routed_to_their_team(self):
        h, g = world()
        # a sales reader whose own mail is about a purchase order: it is theirs
        h.link("Email.mentions_purchase_order", "Email:b", "PurchaseOrder:1")
        h.objects["PurchaseOrder:1"] = ({}, None)
        bob = brief(h, BOB, list(g.values()), SALES)
        self.assertEqual({i["object_id"] for i in bob["items"]},
                         {"SalesOrder:1", "PurchaseOrder:1"})
        self.assertEqual(bob["other_teams"], 2)

    def test_a_restricted_reader_never_gets_a_withheld_item_or_a_withheld_reason(self):
        h, g = world()
        # Carl's trimmed inbox (``read_inbox``) does not hold PO 1: withheld from him
        h.objects["PurchaseOrder:1"] = ({}, frozenset({ANN, OWNER}))
        carl = brief(h, CARL, [g["po2"], g["po3"], g["so1"]], PROC)
        ids = [i["object_id"] for i in carl["items"]]
        self.assertNotIn("PurchaseOrder:1", ids)
        self.assertNotIn("PurchaseOrder:1", repr(carl))
        # involvement only ever asked about what his inbox held
        self.assertNotIn("PurchaseOrder:1", {root for _, root, who in h.asked if who == CARL})
        # supplier 2 wrote to his mailbox in a message he may not open: not his
        self.assertEqual(carl["yours"], 0)
        self.assertFalse(any(i["yours"] for i in carl["items"]))
        self.assertNotIn("Email:c", repr(carl))

    def test_the_owner_gets_the_company_wide_view_with_their_own_first(self):
        h, g = world()
        owner = brief(h, OWNER, list(g.values()), OWNER_ROUTING)
        ids = [i["object_id"] for i in owner["items"]]
        self.assertEqual(ids[0], "PurchaseOrder:2")
        self.assertEqual(set(ids), {"PurchaseOrder:1", "PurchaseOrder:2", "PurchaseOrder:3",
                                    "SalesOrder:1"})
        self.assertEqual((owner["count"], owner["other_teams"], owner["yours"]), (4, 0, 1))
        # without involvement the same reader's order is the company ranking
        plain = bf.build(inbox(*g.values()), routing=OWNER_ROUTING, hidden={},
                         last_visit=None, enabled=("eta_slipped", "customer_order_late"))
        self.assertEqual(set(i["object_id"] for i in plain["items"]), set(ids))
        self.assertIsNone(plain["involvement"])

    def test_no_identity_is_involved_in_nothing_and_reads_nothing(self):
        h, g = world()
        for entitlement in (None, SimpleNamespace(actor=None),
                            SimpleNamespace(actor=object(), principal=None,
                                            source_identities=frozenset({"group:x", "Ann"}))):
            who = inv.Identity.of(entitlement)
            self.assertFalse(who.known)
            found = inv.derive(h, ANN, SCHEMA, who, ["PurchaseOrder:3"])
            self.assertEqual((found.by_object, found.checked), ({}, 0))
        self.assertEqual(h.asked, [])
        # an unknown reason is never put into words
        self.assertEqual(yours_words([{"reason": "guess", "via": "x"},
                                      {"reason": "you_correspond", "via": "Supplier:1"},
                                      {"reason": "named", "via": "accountable_owner"}]),
                         ["Its supplier writes to your mailbox",
                          "You are named as its accountable owner"])

    def test_cardinality_one_ten_sixty_loses_nothing_and_counts_what_was_not_checked(self):
        for n in (1, 10, 60):
            h = Host()
            gaps = []
            for k in range(n):
                oid = f"PurchaseOrder:{k}"
                h.add(oid, {"buyer": ADDRESS[ANN]} if k % 2 == 0 else {})
                gaps.append(gap("late_po", oid, days=k + 1))
            found = brief(h, ANN, gaps, PROC, max_objects=50)
            self.assertEqual(found["count"], n)
            ids = [i["object_id"] for i in found["items"]]
            self.assertEqual(len(ids), len(set(ids)))
            checked = min(n, 50)
            self.assertEqual(found["involvement"]["checked"], checked)
            self.assertEqual(found["involvement"]["not_checked"], n - checked)
            self.assertEqual(found["involvement"]["complete"], n <= 50)
            yours = [i for i in found["items"] if i["yours"]]
            self.assertEqual(ids[:len(yours)], [i["object_id"] for i in yours])
            self.assertLessEqual(len([c for c in h.asked if c[0] == "plx.object"]), 50)


if __name__ == "__main__":
    unittest.main()
