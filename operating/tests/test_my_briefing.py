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
from unittest import mock

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
        self.sets: dict[str, tuple[str, list[str]]] = {}
        self.paged = 0

    def add(self, oid: str, props: dict | None = None, visible: set | None = None) -> None:
        self.objects[oid] = (props or {}, None if visible is None else frozenset(visible))

    def link(self, link: str, frm: str, to: str) -> None:
        self.links.append((link, frm, to))

    def sees(self, actor: str, oid: str) -> bool:
        held = self.objects.get(oid)
        return held is not None and (held[1] is None or actor in held[1])

    def members_for(self, actor: str):
        """``HostAccess.members_set`` for ``actor``: a set handle of the ids."""
        def members(ids):
            handle = f"set:{len(self.sets)}"
            self.sets[handle] = (actor, list(ids))
            return handle
        return members

    def _walk_set(self, args: dict, actor: str) -> dict:
        """``plx.around`` from a set: members re-checked, objects paged by ``limit`` with
        a cursor, every edge reaching a listed object shown."""
        owner, roots = self.sets[args["from"]]
        if owner != actor:
            return {"error": {"code": "set_not_found"}}
        hop = args["path"][0]
        forward = hop["direction"] == "forward"
        pairs = []
        for link, frm, to in self.links:
            root, other = (frm, to) if forward else (to, frm)
            if link != hop["link_type"] or root not in roots or not self.sees(actor, root) \
                    or not self.sees(actor, other):
                continue
            props = self.objects[other][0]
            if all(props.get(f["property"]) == f["value"] for f in args.get("filters", ())):
                pairs.append((other, {"link_type": link, "from": frm, "to": to}))
        others = sorted({o for o, _ in pairs})
        start = int(args.get("cursor") or 0)
        self.paged += bool(start)
        page = others[start:start + args.get("limit", 50)]
        more = start + len(page) < len(others)
        return {"objects": [{"object_id": o} for o in page],
                "edges": [e for o, e in pairs if o in page],
                "completeness": {"complete": not more,
                                 "next_cursor": str(start + len(page)) if more else None}}

    def __call__(self, name: str, args: dict, actor: str) -> dict:
        root = args.get("object_id") or args.get("from")
        self.asked.append((name, root, actor))
        if str(root).startswith("set:"):
            return self._walk_set(args, actor)
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


# SCHEMA with the record types declared: their naming properties are read in one batch
DECLARED = SimpleNamespace(object_types={
    **SCHEMA.object_types,
    "PurchaseOrder": {"properties": [
        {"name": "buyer", "value_type": "text"}, {"name": "vendor_name", "value_type": "text"},
        {"name": "watchers", "value_type": "json"}, {"name": "amount", "value_type": "money"},
        {"name": "code", "value_type": "text", "identifier_kind": "bc_document_no"}]},
    "SalesOrder": {"properties": [{"name": "salesperson", "value_type": "text"}]},
    "Supplier": {"properties": []}, "Customer": {"properties": []}},
    link_types=SCHEMA.link_types)


def words_of(h: "Host", actor: str, withheld: dict | None = None, log: list | None = None):
    """``ObjectWorkspace.object_words`` over ``h`` for ``actor``: visible records only,
    each one's values of the properties asked; ``withheld``: per record, properties the
    reader holds a value of it may not read."""
    def words(ids, props):
        if log is not None:
            log.append((list(ids), list(props)))
        out = {}
        for oid in ids:
            if not h.sees(actor, oid):
                continue
            hidden = set((withheld or {}).get(oid, ()))
            out[oid] = {"display": oid, "withheld": hidden & set(props), "values": {
                p: [v] for p, v in h.objects[oid][0].items() if p in props and p not in hidden}}
        return out
    return words


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

    def test_a_check_the_store_could_not_read_is_not_checked_never_not_yours(self):
        """A check whose read failed (the store could not be read: a fact read cut at the
        budget, or a raise) leaves its record not checked and the briefing partly
        checked, never "not yours"; a reason read before the failure still makes it the
        reader's. Every check runs inside ``facts_cut`` at the budget, so a cold
        whole-table fact read cannot hold the briefing past it."""
        from operating import call_limits
        h, gaps = world()
        h.add("PurchaseOrder:9", {"buyer": ADDRESS[ANN]})
        h.link("PurchaseOrder.ordered_from", "PurchaseOrder:9", "Supplier:1")
        gaps["po9"] = gap("late_po", "PurchaseOrder:9", days=9)
        cut: list[tuple[bool, float]] = []

        def flaky(name: str, args: dict, actor: str) -> dict:
            left = call_limits.seconds_left()
            cut.append((call_limits.facts_cut_now(), 99.0 if left is None else left))
            root = args.get("object_id") or args.get("from")
            if name == "plx.around" and root == "PurchaseOrder:2":
                raise RuntimeError("store gone")
            if name == "plx.around" and root in ("PurchaseOrder:1", "PurchaseOrder:9"):
                return {"error": {"code": "store_unavailable"}}
            return h(name, args, actor)
        who = identity(ANN)
        found = bf.build(inbox(*gaps.values()), routing=PROC, hidden={}, last_visit=None,
                         enabled=("eta_slipped", "customer_order_late"),
                         involved=lambda ids: inv.derive(flaky, ANN, SCHEMA, who, ids))
        involvement = found["involvement"]
        self.assertEqual(involvement["not_checked"], 2)   # PO 1 and PO 2: unread, no reason
        self.assertFalse(involvement["complete"])
        self.assertEqual(involvement["checked"] + involvement["not_checked"],
                         len({a[1] for a in h.asked if a[0] == "plx.object"}))
        yours = {i["object_id"] for i in found["items"] if i["yours"]}
        self.assertIn("PurchaseOrder:9", yours)            # named, read before the failure
        self.assertIn("PurchaseOrder:3", yours)
        self.assertTrue(cut and all(c and left <= inv.BUDGET_SECONDS for c, left in cut))
        self.assertFalse(call_limits.facts_cut_now())      # only inside the checks


    def test_the_named_check_is_one_batched_read_deciding_as_plx_object(self):
        """With the reader's batched value read (``words``) the named check of every
        record is one read of the properties that can name a person (text and JSON, no
        typed identifier), never a ``plx.object`` per record, with the same reasons and
        counts as the whole reads at N=1/10/60: a record the reader may not open or a
        property it may not read names no one; an undeclared type and a failed batch
        are read whole."""
        for n in (1, 10, 60):
            h = Host()
            ids = []
            for k in range(n):
                oid = f"PurchaseOrder:{k}"
                props = {"buyer": ADDRESS[ANN]} if k % 3 == 0 else \
                    {"watchers": f'["{ADDRESS[BOB]}", "{ADDRESS[ANN].upper()}"]'} \
                    if k % 3 == 1 else {"vendor_name": "Ann Example", "amount": "12.50"}
                h.add(oid, props, visible={ANN} if k % 5 else {OWNER})
                ids.append(oid)
            ids.append("Lot:1")
            h.add("Lot:1", {"buyer": ADDRESS[ANN]})
            whole = inv.derive(h, ANN, DECLARED, identity(ANN), ids, max_objects=200)
            h.asked.clear()
            log: list = []
            batched = inv.derive(h, ANN, DECLARED, identity(ANN), ids, max_objects=200,
                                 words=words_of(h, ANN, log=log))
            self.assertEqual(batched.by_object, whole.by_object)
            self.assertEqual((batched.checked, batched.not_checked),
                             (whole.checked, whole.not_checked))
            self.assertEqual(batched.checked, n + 1)
            # one read; money and a typed identifier never asked; the Lot read whole
            self.assertEqual(len(log), 1)
            self.assertEqual(log[0][1], ["buyer", "vendor_name", "watchers"])
            self.assertNotIn("Lot:1", log[0][0])
            self.assertEqual({c[1] for c in h.asked if c[0] == "plx.object"}, {"Lot:1"})
            mine = {o for o in batched.by_object if o.startswith("PurchaseOrder")}
            self.assertEqual(mine, {f"PurchaseOrder:{k}" for k in range(n)
                                    if k % 3 != 2 and k % 5})
        # a property the reader holds a value of it may not read names no one
        h = Host()
        h.add("PurchaseOrder:1", {"buyer": ADDRESS[ANN]})
        found = inv.derive(h, ANN, DECLARED, identity(ANN), ["PurchaseOrder:1"],
                           words=words_of(h, ANN, withheld={"PurchaseOrder:1": {"buyer"}}))
        self.assertEqual((found.by_object, found.checked), ({}, 1))

        # a failed batch: every record read whole, same answer
        def broken(ids, props):
            raise RuntimeError("store gone")
        h.asked.clear()
        found = inv.derive(h, ANN, DECLARED, identity(ANN), ["PurchaseOrder:1"],
                           words=broken)
        self.assertEqual(found.by_object, {"PurchaseOrder:1": [
            {"reason": "named", "via": "buyer"}]})
        self.assertIn(("plx.object", "PurchaseOrder:1", ANN), h.asked)

    def test_each_mail_path_is_walked_once_from_the_set_of_records(self):
        """With ``members`` each mail path is walked once from the set of every record
        (paged), never once per record, with the same reasons per reader as the
        per-record walks at N=1/10/60; a message the reader may not open never makes a
        record theirs; a walk that failed is walked per record."""
        for n in (1, 10, 60):
            h, _ = world()
            ids = []
            for k in range(n):
                po, so = f"PurchaseOrder:p{k}", f"SalesOrder:s{k}"
                h.add(po), h.add(so)
                h.link("PurchaseOrder.ordered_from", po, f"Supplier:{1 + k % 2}")
                h.link("SalesOrder.ordered_by", so, "Customer:1")
                for who in (ANN, BOB, CARL):
                    if k % 3 == (ANN, BOB, CARL).index(who):
                        h.add(f"Email:{who}{k}", {"mailbox": who}, visible={who, OWNER})
                        h.link("Email.mentions_purchase_order", f"Email:{who}{k}", po)
                ids += [po, so]
            ids += ["PurchaseOrder:1", "PurchaseOrder:2", "PurchaseOrder:3", "SalesOrder:1"]
            h.paged = 0
            for reader in (ANN, BOB, CARL, OWNER):
                h.asked.clear()
                whole = inv.derive(h, reader, SCHEMA, identity(reader), ids, max_objects=500)
                singles = len(h.asked)
                h.asked.clear()
                with mock.patch.object(inv, "WALK_PAGE", 3), \
                        mock.patch.object(inv, "WALK_ROOTS", 25):   # paged; N=60 split
                    walked = inv.derive(h, reader, SCHEMA, identity(reader), ids,
                                        max_objects=500, members=h.members_for(reader))
                self.assertEqual(walked.by_object, whole.by_object, (n, reader))
                self.assertEqual((walked.checked, walked.not_checked),
                                 (whole.checked, whole.not_checked))
                arounds = [a for a in h.asked if a[0] == "plx.around"]
                self.assertTrue(all(a[1].startswith("set:") for a in arounds), arounds[:3])
                self.assertLess(len(h.asked), singles)
            self.assertEqual(h.paged > 0, n >= 10)   # later pages were read
            # Carl's supplier-2 message is withheld from him: no PO of supplier 2 is his
            self.assertFalse(any(r["reason"] == "you_correspond" for rs in inv.derive(
                h, CARL, SCHEMA, identity(CARL), ids, members=h.members_for(CARL)
            ).by_object.values() for r in rs))

        # a walk that fails is walked per record, with the same answer
        h, _ = world()
        ids = ["PurchaseOrder:1", "PurchaseOrder:2", "SalesOrder:1"]
        whole = inv.derive(h, OWNER, SCHEMA, identity(OWNER), ids)

        def flaky(name, args, actor):
            if str(args.get("from")).startswith("set:"):
                return {"error": {"code": "store_unavailable"}}
            return h(name, args, actor)
        found = inv.derive(flaky, OWNER, SCHEMA, identity(OWNER), ids,
                           members=h.members_for(OWNER))
        self.assertEqual(found.by_object, whole.by_object)
        self.assertEqual((found.checked, found.not_checked), (3, 0))

    def test_records_read_whole_are_checked_once_per_reader_and_stamp(self):
        """``known`` (``Known.of`` under the reader's key): a second consumer of the
        same reader's briefing reads no record again and gets the same reasons; a record
        whose check could not be read is checked again; another key (another reader, a
        new data stamp) starts empty; a kept entry ends after ``seconds``."""
        h, g = world()
        now = [0.0]
        known = inv.Known(seconds=60, clock=lambda: now[0])
        mine = known.of(("ann", "stamp-1"))
        first = brief(h, ANN, list(g.values()), PROC, known=mine)
        asked = len(h.asked)
        self.assertGreater(asked, 0)
        again = brief(h, ANN, list(g.values()), PROC, known=known.of(("ann", "stamp-1")))
        self.assertEqual(len(h.asked), asked)   # nothing read again
        self.assertEqual(again["items"], first["items"])
        self.assertEqual(again["involvement"], first["involvement"])
        self.assertEqual(known.of(("carl", "stamp-1")), {})
        self.assertEqual(known.of(("ann", "stamp-2")), {})
        carl = brief(h, CARL, list(g.values()), PROC, known=known.of(("carl", "stamp-1")))
        self.assertEqual(carl["yours"], 0)   # never Ann's reasons
        now[0] = 61
        self.assertEqual(known.of(("ann", "stamp-1")), {})
        # an unread check is never kept: asked again on the next read
        failing = {"on": True}

        def flaky(name: str, args: dict, actor: str) -> dict:
            if failing["on"] and (args.get("object_id") or args.get("from")) == \
                    "PurchaseOrder:2":
                raise RuntimeError("store gone")
            return h(name, args, actor)
        held = known.of(("ann", "stamp-3"))
        inv.derive(flaky, ANN, SCHEMA, identity(ANN), ["PurchaseOrder:2", "PurchaseOrder:1"],
                   known=held)
        self.assertNotIn("PurchaseOrder:2", held)
        self.assertIn("PurchaseOrder:1", held)
        failing["on"] = False
        out = inv.derive(flaky, ANN, SCHEMA, identity(ANN), ["PurchaseOrder:2"], known=held)
        self.assertEqual(out.not_checked, 0)
        self.assertIn("PurchaseOrder:2", held)
        self.assertIsNone(known.of(None))
        self.assertIsNone(known.of(("ann", ["unhashable"])))   # nothing kept, no raise

if __name__ == "__main__":
    unittest.main()
