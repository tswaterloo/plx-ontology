"""Reviewed allocations (ER-06, ER-11) and source removal (ING-09, REC-04).

One AO packet serves two sales orders over a many-to-many link: ``plx.aggregate`` never
splits its produced quantity by inference (``allocation_review_required``); a person's
``plx.review`` ``allocate`` decisions, projected as reviewed allocation links, split it
and sum, checked against the packet's own value and unit; a later rejection of the link
takes a share back, and a lead naming another order (seen or hidden from the reader)
never lets the whole value fall to one order. On one pair the latest of an allocation and
a rejection is the only one in force (decision 2026-10-08-allocation-latest-decision). A
mail its source deleted withdraws the reviews that cite it and ends the reviewed and
mention links built on it, for plx.review, plx.find and plx.object alike, and a document's
removal ends its document-field links. A v1 review store, written by the v1 code itself,
keeps its history when opened. All identifiers are synthetic. Evidence: isolated-fixture.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest

from ontology import document_fields, mentions, reviews, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.store import StoreBuilder
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from operating import ontology_tools_review as rt
from tests import test_ontology_tools_aggregate as ta
from tests import test_ontology_tools_review as tr

CONTRACTS = tr.CONTRACTS
SCHEMA: ot.SchemaView | None = None
LINK = "AssemblyOrderPacket.for_sales_order"
P1 = "AssemblyOrderPacket:sharepoint.ao_packets:p1"   # one AO's packet, two orders
P2 = "AssemblyOrderPacket:sharepoint.ao_packets:p2"   # one order only
SO1, SO2 = "SalesOrder:portal.app_sales_order:so1", "SalesOrder:portal.app_sales_order:so2"
MAIL = 'Email:graph_mail.messages:{"id":"M-9","mailbox":"plan@plx.test"}'
MAIL_HANDLE = "graph-mail://plan@plx.test/M-9@CK-1"
A, B = tr.employee_session(tr.OID_A), tr.employee_session(tr.OID_B)


def setUpModule():
    global SCHEMA
    SCHEMA = ta.SCHEMA = ot.SchemaView.from_compiled(schema_loader.load_schema())


class Builder:
    """The ``post_project`` builder surface over a hand-filled store."""

    _INSERT = StoreBuilder._INSERT

    def __init__(self, conn):
        self.connection = conn

    def add(self, table, rows):
        return StoreBuilder.add(self, table, rows)


class AllocationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.reviews = reviews.ReviewStore(Path(tmp.name) / "reviews.sqlite",
                                           clock=lambda: dt.datetime(2099, 1, 20,
                                                                     tzinfo=dt.timezone.utc))
        s = self.store = ta.Store()
        for oid in (P1, P2, SO1, SO2):
            s.obj(oid)
        s.val(SO1, "order_number", "identifier", "SO-1001")
        s.val(SO2, "order_number", "identifier", "SO-1002")
        s.val(P1, "produced_quantity", "quantity", "1920", unit="each", role="produced")
        s.val(P2, "produced_quantity", "quantity", "100", unit="EA", role="produced")
        for packet, order in ((P1, SO1), (P1, SO2), (P2, SO1)):
            s.link(LINK, packet, order, tier="corroborated", kind="mention")
        # The planner's mail the allocation cites; only A holds its mailbox.
        s.conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                       (MAIL, "Email", "graph_mail.messages", '["M-9"]', "mail:plan@plx.test",
                        ta.T0, None))
        s.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                       (MAIL, MAIL_HANDLE, ta._h(MAIL_HANDLE), "mail", "[]", ta.T0, None))
        a, b = A.employee.principal, B.employee.principal
        policy = ot.ReadPolicy("plx", {"api:*": {a, b}, "mail:*": {a}},
                               {m: {a, b} for m in store_ddl.MARKINGS})
        self.base = ot.OntologyTools(
            ot.StoreReader(s.conn), policy, schema=SCHEMA, contracts=CONTRACTS,
            clock=lambda: ta.NOW, item_gate=lambda actor, handle: (
                not handle.startswith("graph-mail://") or a in actor.principals))
        self.review = rt.ReviewTools(self.base, self.reviews, session=A)
        self.written = None   # the links the build's other steps write

    def allocate(self, order, quantity, unit="each", review=None, **change):
        handle = self.store.handles[P1][0]
        return (review or self.review).call("plx.review", dict({
            "mode": "record", "decision": "allocate", "subject": P1, "target": order,
            "link_type": LINK, "allocation": {"property": "produced_quantity",
                                              "quantity": quantity, "unit": unit},
            "evidence": [{"l1_handle": handle, "digest": ta._h(handle),
                          "field_path": "row.produced_quantity"},
                         {"l1_handle": MAIL_HANDLE, "digest": ta._h(MAIL_HANDLE)}]}, **change),
            A.actor)

    def project(self):
        """The next build's review step (the fixture store stands for the new build: the
        links the other steps wrote, as first written, then this step's)."""
        conn = self.store.conn
        if self.written is None:
            cursor = conn.execute("SELECT * FROM links WHERE binding_kind <> 'reviewed'")
            names = [column[0] for column in cursor.description]
            self.written = [dict(zip(names, row)) for row in cursor]
        conn.execute("DELETE FROM links")
        Builder(conn).add("links", self.written)
        return reviews.link_step(self.reviews, "plx")(
            Builder(conn), type("Projector", (), {"links": SCHEMA.link_types})())

    def identity_review(self):
        """``plx.review`` for A entitled to identity review (link decisions); ``review``
        records allocations without it."""
        return rt.ReviewTools(self.base, self.review.reviews, session=A,
                              gate=tr.identity_reviewer)

    def decide(self, decision, order, **fields):
        handle = self.store.handles[P1][0]
        return self.identity_review().call("plx.review", dict({
            "mode": "record", "decision": decision, "subject": P1, "target": order,
            "link_type": LINK, "evidence": [{"l1_handle": handle, "digest": ta._h(handle),
                                             "field_path": "row.produced_quantity"}]},
            **fields), A.actor)

    def status(self, subject=SO2):
        out = self.review.call("plx.review", {"mode": "status", "subject": subject}, A.actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.review"]["output"],
                                  root=CONTRACTS), [])
        return [(r["decision"], r["state"], r.get("state_reason")) for r in out["reviews"]]

    def pair_links(self, kind):
        return [tuple(row) for row in self.store.conn.execute(
            "SELECT tier, recorded_at, superseded_at FROM links WHERE from_object = ? AND "
            "to_object = ? AND binding_kind = ? ORDER BY recorded_at", (P1, SO2, kind))]

    def produced_by_order(self, actor=A.actor, ids=(P1, P2)):
        out = oa.AggregateTools(self.base).call("plx.aggregate", {
            "set": self.base.sets.new(actor, list(ids)),
            "measure": {"property": "produced_quantity", "op": "sum"},
            "group_by": ["order_number"], "path": [{"link_type": LINK, "direction": "forward"}]},
            actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.aggregate"]["output"],
                                  root=CONTRACTS), [], out)
        self.assertNotIn("refused", out)
        values = {g["key"]["order_number"]["untrusted_text"]: g["value"] for g in out["groups"]}
        return values, out, {g["reason"] for g in out["gaps"]}

    def test_an_ao_serving_two_orders_is_split_only_by_reviewed_allocations(self):
        # Nothing reviewed: the shared packet is never auto-allocated; P2 serves SO1 alone.
        values, out, reasons = self.produced_by_order()
        self.assertEqual(values, {"SO-1001": "100"})
        self.assertTrue(out["partial"])
        self.assertIn("allocation_review_required", reasons)
        self.assertIn("n_to_m_single_reach", reasons)   # P2's whole 100: said, not silent

        # Fail closed before anything is written.
        for change, code in (({"link_type": "SalesOrder.references_customer_po"},
                              "allocation_needs_many_to_many"),
                             ({"allocation": {"property": "colour", "quantity": "1",
                                              "unit": "each"}}, "allocation_property_invalid"),
                             # No value of the property to split, one share over the
                             # packet's own 1920, or in another unit.
                             ({"allocation": {"property": "planned_quantity",
                                              "quantity": "1", "unit": "each"}},
                              "value_not_found"),
                             ({"allocation": {"property": "produced_quantity",
                                              "quantity": "1921", "unit": "EA"}},
                              "allocation_conflict"),
                             ({"allocation": {"property": "produced_quantity",
                                              "quantity": "920", "unit": "kg"}},
                              "allocation_conflict"),
                             ({"decision": "accept_link"}, "invalid_input")):
            self.assertEqual(self.allocate(SO1, "1000", **change)["error"]["code"], code)
        self.assertEqual(self.reviews.history("plx", P1), [])

        # One order allocated: the remainder is still never inferred.
        recorded = self.allocate(SO1, "1000")
        self.assertEqual((recorded["review"]["allocation"], recorded["projection"]),
                         ({"property": "produced_quantity", "quantity": "1000", "unit": "each"},
                          "next_projection_build"))
        self.project()
        self.assertEqual(self.produced_by_order()[0], {"SO-1001": "100"})

        # Both allocated: each order sums its share (1000 + 920 = the packet's 1920).
        self.allocate(SO2, "920")
        self.assertEqual(self.project()["counts"]["allocations"], 2)
        values, out, reasons = self.produced_by_order()
        self.assertEqual(values, {"SO-1001": "1100", "SO-1002": "920"})
        self.assertTrue(out["completeness"]["complete"])
        self.assertIn("reviewed_allocation_used", reasons)
        so2 = next(g for g in out["groups"] if g["value"] == "920")
        self.assertEqual((so2["quantity_role"], so2["inputs"]), ("produced", 1))
        self.assertEqual({c["l1_handle"] for c in so2["inputs_citations"]},
                         {self.store.handles[P1][0]})
        # A reader without the planner's mailbox sees no split, and no whole either.
        values, out, reasons = self.produced_by_order(B.actor)
        self.assertEqual(values, {"SO-1001": "100"})
        self.assertIn("group_link_withheld", reasons)
        self.assertIn("withheld", out)

        # Allocations that give out less leave the remainder in no group, and say so.
        self.allocate(SO2, "900")
        self.project()
        values, _, reasons = self.produced_by_order()
        self.assertEqual(values, {"SO-1001": "1100", "SO-1002": "900"})
        self.assertIn("allocation_remainder_unallocated", reasons)
        # Shares that each fit but together exceed the packet's value are not grouped.
        self.allocate(SO2, "1000")
        self.project()
        values, _, reasons = self.produced_by_order()
        self.assertEqual(values, {"SO-1001": "100"})
        self.assertIn("allocation_conflict", reasons)

    def test_a_share_is_checked_against_the_value_aggregate_splits(self):
        # Beside the packet's 1920 each (rank 0): a source-null row and a lower-authority
        # 900 with no unit. plx.aggregate splits the 1920; plx.review checks against it.
        self.store.val(P1, "produced_quantity", "quantity", None, unit="each", role="produced",
                       path="row.produced_null", rank=1)
        self.store.val(P1, "produced_quantity", "quantity", "900", role="produced",
                       path="row.produced_other", rank=2)
        self.assertEqual(self.allocate(SO1, "1921")["error"]["code"], "allocation_conflict")
        recorded = self.allocate(SO1, "1000")
        self.assertNotIn("error", recorded)
        self.assertNotIn("error", self.allocate(SO2, "920"))
        self.project()
        self.assertEqual(self.produced_by_order()[0], {"SO-1001": "1100", "SO-1002": "920"})
        # A share recorded in another unit (before plx.review checked units) is never
        # split: plx.aggregate still refuses it.
        self.reviews.record(tenant="plx", author=recorded["review"]["author"],
                            decision="allocate", subject=P1, target=SO2, link_type=LINK,
                            allocation={"property": "produced_quantity", "quantity": "920",
                                        "unit": "kg"},
                            evidence=recorded["review"]["evidence"])
        self.project()
        values, _, reasons = self.produced_by_order()
        self.assertEqual(values, {"SO-1001": "100"})
        self.assertIn("allocation_conflict", reasons)

    def test_a_lead_naming_another_order_is_never_auto_allocated(self):
        # Review of aa7c5fcd: real packet -> order links are lead mentions. A lead naming
        # a second order makes the packet need allocations even when the other order is
        # reached at the floor (a person's accept_link, or a corroborated link).
        def link(packet, order, tier, kind="mention", n=[0], handles=()):
            n[0] += 1
            reviewed = ("employee:x", ta.T0, "rev-" + "c" * 32) if tier == "reviewed" else (
                None, None, None)
            self.store.conn.execute(
                "INSERT INTO links(link_id, link_type, from_object, to_object, tier, "
                "binding_kind, reason, why_not, evidence_handles, reviewed_by, reviewed_at, "
                "review_action_id, markings, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"lead-{n[0]}", LINK, packet, order, tier, kind,
                 '[{"feature": "identifier_mention", "agrees": true}]', "[]",
                 json.dumps([self.store.handles[packet][0], *handles]), *reviewed, "[]", ta.T0))

        p3, p4, p5, p6, p7, p8, p9 = (
            self.store.obj(f"AssemblyOrderPacket:sharepoint.ao_packets:{k}")
            for k in ("p3", "p4", "p5", "p6", "p7", "p8", "p9"))
        for packet in (p3, p4, p5, p6, p7, p8, p9):
            self.store.val(packet, "produced_quantity", "quantity", "1920", unit="each",
                           role="produced")
        link(p3, SO1, "lead")
        link(p3, SO2, "lead")
        link(p3, SO1, "reviewed", "reviewed")         # a person accepted P3 -> SO1 only
        link(p4, SO1, "corroborated")
        link(p4, SO2, "lead")
        values, out, reasons = self.produced_by_order(ids=(p3, p4))
        self.assertEqual(values, {})
        self.assertTrue(out["partial"])
        self.assertIn("allocation_review_required", reasons)
        # A lead a person rejected names no order: P5 serves SO1 alone.
        link(p5, SO1, "corroborated")
        link(p5, SO2, "lead")
        link(p5, SO2, "contradicted", "reviewed")
        values, _, reasons = self.produced_by_order(ids=(p5,))
        self.assertEqual(values, {"SO-1001": "1920"})
        self.assertIn("n_to_m_single_reach", reasons)
        # A lead citing the planner's mail, which B may not read: A must allocate; B is told
        # a link is withheld, never handed the whole 1920 under SO1. A hidden lead naming
        # the order P7 already reaches changes nothing, so B's answer is A's.
        link(p6, SO1, "corroborated")
        link(p6, SO2, "lead", handles=[MAIL_HANDLE])
        link(p7, SO1, "corroborated")
        link(p7, SO1, "lead", handles=[MAIL_HANDLE])
        self.assertIn("allocation_review_required", self.produced_by_order(ids=(p6,))[2])
        values, out, reasons = self.produced_by_order(B.actor, ids=(p6,))
        self.assertEqual((values, out["partial"]), ({}, True))
        self.assertIn("group_link_withheld", reasons)
        self.assertNotIn(SO2, json.dumps(out))
        for actor in (A.actor, B.actor):
            self.assertEqual(self.produced_by_order(actor, ids=(p7,))[0], {"SO-1001": "1920"})
        # A person's rejection B may not read (it cites the planner's mail) names no order
        # and is no withheld link: B's answer is A's.
        link(p8, SO1, "corroborated")
        link(p8, SO2, "contradicted", "reviewed", handles=[MAIL_HANDLE])
        for actor in (A.actor, B.actor):
            values, _, reasons = self.produced_by_order(actor, ids=(p8,))
            self.assertEqual(values, {"SO-1001": "1920"}, actor)
            self.assertNotIn("group_link_withheld", reasons, actor)
        # A rejection stops inferred links only: a source key to SO2 still reaches it.
        link(p9, SO1, "corroborated")
        link(p9, SO2, "verified", "fk")
        link(p9, SO2, "contradicted", "reviewed")
        values, _, reasons = self.produced_by_order(ids=(p9,))
        self.assertEqual(values, {})
        self.assertIn("allocation_review_required", reasons)

    def test_a_later_rejection_of_the_link_takes_the_allocated_share_back(self):
        # P1 -> SO2 is also a text join the projection itself writes (seeded forward).
        self.store.link(LINK, P1, SO2, tier="stored_by_text_join", kind="text_match")
        self.allocate(SO1, "1000")
        self.allocate(SO2, "920")
        self.project()
        self.assertEqual(self.produced_by_order()[0], {"SO-1001": "1100", "SO-1002": "920"})
        rejected = self.decide("reject_link", SO2)
        self.assertEqual(rejected["review"]["state"], "effective", rejected)
        at = rejected["review"]["recorded_at"]
        counts = self.project()["counts"]
        self.assertEqual((counts["rejected_after_allocation"],
                          counts["links_ended_by_rejection"]), (1, 1))
        # The rejected pair reaches nothing by inference, so P1's share of SO1 is used.
        values, _, reasons = self.produced_by_order()
        self.assertEqual(values, {"SO-1001": "1100"})
        self.assertNotIn("allocation_review_required", reasons)
        self.assertLessEqual({"reviewed_allocation_used", "allocation_remainder_unallocated"},
                             reasons)
        # The rejected link is its contradicted row: the mention ends there (1c0959b3d22e).
        self.assertEqual(self.pair_links("mention"), [("corroborated", ta.T0, at)])
        self.assertEqual([tier for tier, _, until in self.pair_links("reviewed")
                          if until is None], ["contradicted"])
        self.assertEqual(self.status(), [("allocate", "superseded", "later_review"),
                                         ("reject_link", "effective", None)])
        # Retracting the rejection gives the share back (the decision before it applies).
        self.identity_review().call("plx.review", {
            "mode": "record", "decision": "retract",
            "retracts": rejected["review"]["review_id"]}, A.actor)
        self.project()
        self.assertEqual(self.produced_by_order()[0], {"SO-1001": "1100", "SO-1002": "920"})
        self.assertEqual(self.pair_links("mention"), [("corroborated", ta.T0, None)])

    def test_the_latest_decision_on_a_pair_is_the_only_one_in_force(self):
        hours = iter(range(1, 100))
        self.review.reviews = self.reviews = reviews.ReviewStore(
            self.reviews.path, clock=lambda: dt.datetime(2099, 1, 20, next(hours),
                                                         tzinfo=dt.timezone.utc))

        def in_force(expected):
            listed = self.status()
            self.assertEqual(sorted(d for d, state, _ in listed if state == "effective"),
                             expected, listed)

        # A rejection, then allocations: the later allocation supersedes the rejection.
        # That links the rejected pair again, an identity decision: an employee the real
        # resolver does not grant review:identity is refused and nothing is written.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def resolved(*grants):   # plx.review for A as the real resolver grants it
            return rt.ReviewTools(self.base, self.reviews, session=A, gate=tr.resolved_gate(
                Path(tmp.name) / "-".join(grants).replace(":", "_"), grants))

        unentitled = resolved("staff:planning")
        first = self.decide("reject_link", SO2)
        self.allocate(SO1, "1000")
        refused = self.allocate(SO2, "920", review=unentitled)
        self.assertEqual(refused["error"]["code"], "identity_review_required", refused)
        # The store itself refuses a writer that brings no identity gate.
        with self.assertRaises(reviews.ReviewError) as caught:
            self.reviews.record(tenant="plx", author={"principal": A.employee.principal},
                                decision="allocate", subject=SO2, target=P1, link_type=LINK,
                                allocation={"property": "produced_quantity",
                                            "quantity": "920", "unit": "each"},
                                evidence=first["review"]["evidence"])
        self.assertEqual(caught.exception.code, "identity_review_required")
        in_force(["reject_link"])
        allocated = self.allocate(SO2, "920", review=self.identity_review())
        self.assertEqual(allocated["review"]["state"], "effective", allocated)
        in_force(["allocate"])
        self.assertEqual(self.status()[0], ("reject_link", "superseded", "later_review"))
        self.project()
        self.assertEqual(self.produced_by_order()[0], {"SO-1001": "1100", "SO-1002": "920"})
        # The rejection held from its recording to the allocation, and the mention with it.
        self.assertEqual(self.pair_links("mention"),
                         [("corroborated", ta.T0, first["review"]["recorded_at"]),
                          ("corroborated", allocated["review"]["recorded_at"], None)])
        # The same rejection again is a new decision, and supersedes the allocation.
        again = self.decide("reject_link", SO2)
        self.assertEqual((again["created"], again["review"]["state"]), (True, "effective"))
        in_force(["reject_link"])
        self.project()
        self.assertEqual(self.produced_by_order()[0], {"SO-1001": "1100"})
        # Re-accepting the link does not revive the allocation before the rejection.
        self.decide("accept_link", SO2)
        in_force(["accept_link"])
        self.project()
        values, _, reasons = self.produced_by_order()
        self.assertEqual(values, {"SO-1001": "100"})
        self.assertIn("allocation_review_required", reasons)
        # A new allocation does, and agrees with the acceptance (it overrules the
        # rejections on the pair: an identity reviewer's).
        self.assertEqual(self.allocate(SO2, "920")["error"]["code"], "identity_review_required")
        renewed = self.allocate(SO2, "920", review=self.identity_review())
        self.assertEqual((renewed["created"], renewed["review"]["state"]), (True, "effective"))
        in_force(["accept_link", "allocate"])
        self.project()
        self.assertEqual(self.produced_by_order()[0], {"SO-1001": "1100", "SO-1002": "920"})
        # Rejected and allocated again: retracting that allocation puts the rejection
        # back in force, refused without review:identity and recorded for an employee
        # the policy grants it.
        self.decide("reject_link", SO2)
        overruling = self.allocate(SO2, "920", review=self.identity_review())
        in_force(["allocate"])
        retract = {"mode": "record", "decision": "retract",
                   "retracts": overruling["review"]["review_id"]}
        self.assertEqual(unentitled.call("plx.review", retract, A.actor)["error"]["code"],
                         "identity_review_required")
        in_force(["allocate"])
        done = resolved("staff:planning", rt.IDENTITY_REVIEW).call("plx.review", retract,
                                                                   A.actor)
        self.assertNotIn("error", done, done)
        in_force(["reject_link", "retract"])

    def test_a_document_removed_from_its_source_ends_its_document_field_links(self):
        # P2's packet names SO-1001 in its text; its source dropped it for a week.
        since, back = "2099-01-10T00:00:00Z", "2099-01-17T00:00:00Z"
        handle = self.store.handles[P2][0]
        self.store.val(P2, "evidence", "text", json.dumps({
            "l1_handle": handle, "source_sha256": "0" * 64, "references": [{
                "identifier_kind": "so_number", "normalized": "SO-1001", "text": "SO-1001",
                "citation": {"page": 1, "start": 0, "end": 7}}]}))
        so1 = self.store.handles[SO1]
        self.store.conn.execute(
            "INSERT INTO identifiers(identifier_kind, normalized, raw, role, object_id, "
            "property, l1_handle, field_path, lineage, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("so_number", "SO-1001", "SO-1001", "primary", SO1, "order_number", so1[0],
             "row.order_number", so1[1], ta.T0))
        self.store.conn.execute("INSERT INTO object_removals(object_id, basis, since, until) "
                                "VALUES (?, 'missing_from_source', ?, ?)", (P2, since, back))
        report = document_fields.store_document_links(
            Builder(self.store.conn), type("Projector", (), {"links": SCHEMA.link_types})())
        self.assertEqual(report["counts"]["links"], 2)
        self.assertEqual([tuple(row) for row in self.store.conn.execute(
            "SELECT from_object, to_object, tier, recorded_at, superseded_at FROM links "
            "WHERE link_id LIKE 'docfield:%' ORDER BY recorded_at")],
            [(P2, SO1, "lead", ta.T0, since), (P2, SO1, "lead", back, None)])

    def test_every_allocated_packet_is_one_share_per_order_at_1_10_60(self):
        author = {"principal": A.employee.principal, "channel": "console",
                  "session": A.session_ref}
        for n in (1, 10, 60):
            self.setUp()
            packets = []
            for i in range(n):
                packet = self.store.obj(f"AssemblyOrderPacket:sharepoint.ao_packets:n{i}")
                self.store.val(packet, "produced_quantity", "quantity", "1000", unit="each",
                               role="produced")
                handle = self.store.handles[packet][0]
                for order, share in ((SO1, "600.5"), (SO2, "399.5")):
                    self.store.link(LINK, packet, order, tier="corroborated", kind="mention")
                    self.reviews.record(
                        tenant="plx", author=author, decision="allocate", subject=packet,
                        target=order, link_type=LINK,
                        allocation={"property": "produced_quantity", "quantity": share,
                                    "unit": "each"},
                        evidence=[{"object_id": packet, "l1_handle": handle,
                                   "digest": ta._h(handle),
                                   "field_path": "row.produced_quantity"}])
                packets.append(packet)
            self.project()
            started = time.monotonic()
            values, out, _ = self.produced_by_order(ids=packets)
            self.assertLess(time.monotonic() - started, 10, n)
            self.assertEqual(values, {"SO-1001": f"{600.5 * n:g}" if n > 1 else "600.5",
                                      "SO-1002": f"{399.5 * n:g}" if n > 1 else "399.5"}, n)
            self.assertEqual([g["inputs"] for g in out["groups"]], [n, n])


class SourceRemovalTests(unittest.TestCase):
    def test_a_removed_mail_withdraws_its_reviews_and_ends_its_links(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = reviews.ReviewStore(root / "reviews.sqlite", clock=lambda: tr.REVIEWED_AT)
        subject = f"Order confirmation {tr.NUMBER} for PO100236"
        gone = "2026-10-03T09:00:00.000000Z"

        def build(path, mails):
            document = mentions.Document(tr.MAIL_HANDLE, "email", (
                mentions.TextField(tr.MAIL_HANDLE, "row.subject", subject),))
            steps = {"mentions": lambda builder, projector: mentions.store_mention_links(
                         builder, projector, documents=[document]),
                     "reviews": reviews.link_step(store, "plx")}
            from ontology import identifiers
            from ontology.project import build_store
            build_store(path, schema_loader.load_schema(),
                        {"portal_api": tr.Static("portal_api", [tr.po(9, "PO100236")]),
                         "graph_mail": tr.Static("graph_mail", mails)},
                        normalizer=identifiers.normalize, clock=lambda: tr.LATER,
                        post_project=steps)

        def tools(path):
            base = tr.ontology(path, store, tr.LATER)
            self.addCleanup(base.store.conn.close)
            return base, rt.ReviewTools(base, store, session=A, gate=tr.identity_reviewer)

        build(root / "held.sqlite", tr.FIRST)
        _, review = tools(root / "held.sqlite")
        evidence = [{"l1_handle": tr.MAIL_HANDLE, "digest": tr.D1, "field_path": "row.subject"}]
        for arguments in ({"decision": "alias", "subject": tr.PO9, "alias": {
                               "identifier_kind": "vendor_confirmation", "value": tr.NUMBER}},
                          {"decision": "accept_link", "subject": tr.EMAIL, "target": tr.PO9,
                           "link_type": tr.LINK}):
            recorded = review.call("plx.review", dict(arguments, mode="record",
                                                      evidence=evidence), A.actor)
            self.assertEqual(recorded["review"]["state"], "effective", recorded)

        removed = [dataclasses.replace(tr.FIRST[0], missing_from_source_since=gone)]
        build(root / "removed.sqlite", removed)
        base, review = tools(root / "removed.sqlite")
        status = review.call("plx.review", {"mode": "status", "subject": tr.PO9}, A.actor)
        self.assertEqual(validate(status, CONTRACTS["tools"]["plx.review"]["output"],
                                  root=CONTRACTS), [])
        self.assertEqual([(r["decision"], r["state"], r.get("state_reason"))
                          for r in status["reviews"]],
                         [("alias", "withdrawn", "source_removed"),
                          ("accept_link", "withdrawn", "source_removed")])
        before = "2026-10-03T08:00:00Z"
        for as_known_at, found, tiers in ((None, False, None),
                                          (before, True, {"lead": 1, "reviewed": 1})):
            at = {"as_known_at": as_known_at} if as_known_at else {}
            answer = base.call("plx.find", dict(at, query=tr.NUMBER), A.actor)
            self.assertEqual(tr.PO9 in [c["object"]["object_id"] for c in answer["candidates"]],
                             found, as_known_at)
            answer = base.call("plx.object", dict(at, object_id=tr.PO9), A.actor)
            self.assertEqual({c["link_type"]: c.get("tiers") for c in answer["linked_counts"]
                              }.get(tr.LINK), tiers, as_known_at)


# The v1 review module as committed before ``allocate`` (4614a1dfb^:ontology/reviews.py),
# byte for byte: its git blob id is checked, so the v1 file below is the one v1 wrote.
V1_SOURCE = Path(__file__).parent / "fixtures" / "reviews_v1.py.txt"
V1_BLOB = "c051a3315dc6ff7bb7a1658683d5ce803ebbf289"


def v1_reviews():
    source = V1_SOURCE.read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(source) + source).hexdigest()
    assert blob == V1_BLOB, f"{V1_SOURCE} is not the v1 module ({blob})"
    module = types.ModuleType("reviews_v1")
    exec(compile(source, str(V1_SOURCE), "exec"), module.__dict__)
    return module


class StoreFormatTests(unittest.TestCase):
    def test_a_v1_store_keeps_its_history_and_stays_append_only(self):
        v1 = v1_reviews()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.sqlite"
            old = v1.ReviewStore(path, clock=lambda: dt.datetime(2026, 10, 1,
                                                                 tzinfo=dt.timezone.utc))
            author = {"principal": "employee:x"}
            cited = [{"object_id": MAIL, "l1_handle": MAIL_HANDLE, "digest": "0" * 64}]
            kind, _ = old.record(tenant="plx", author=author, decision="document_kind",
                                 subject=MAIL, document_kind="QUOTE", evidence=cited)
            retract, _ = old.record(tenant="plx", author=author, decision="retract",
                                    subject=MAIL, evidence=[], retracts=kind["review_id"])
            link, _ = old.record(tenant="plx", author=author, decision="accept_link",
                                 subject=P1, target=SO1, link_type=LINK, evidence=cited)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT value FROM review_meta WHERE key = "
                                            "'format'").fetchone()[0], "plx-reviews/v1")
            store = reviews.ReviewStore(path)
            self.assertEqual([(r["seq"], r["review_id"], r.get("retracts"))
                              for r in store.history("plx", MAIL)],
                             [(kind["seq"], kind["review_id"], None),
                              (retract["seq"], retract["review_id"], kind["review_id"])])
            self.assertEqual(store.link_history("plx"), old.link_history("plx"))
            record, _ = store.record(
                tenant="plx", author=author, decision="allocate",
                subject=P1, target=SO1, link_type=LINK,
                allocation={"property": "produced_quantity", "quantity": "1.50", "unit": "ea"},
                evidence=[{"object_id": P1, "l1_handle": "api://x/1@g", "digest": "0" * 64}])
            self.assertEqual((record["seq"], record["allocation"]["quantity"]),
                             (link["seq"] + 1, "1.5"))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT value FROM review_meta WHERE key = 'format'"
                                            ).fetchone()[0], reviews.FORMAT)
                for statement in ("UPDATE reviews SET subject = 'x'", "DELETE FROM reviews"):
                    with self.assertRaisesRegex(sqlite3.DatabaseError, "reviews_append_only"):
                        db.execute(statement)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main()
