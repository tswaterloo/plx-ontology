"""Link kinds over one connected component (ER-06).

One fixture component joins a Portal sales order to: a mail that mentions it (a document
mention lead), its FM twin (a person's reviewed ``same_record_as``), an AO packet a person
allocated part of its produced quantity to (``plx.review`` ``allocate``, projected by the
review step) and a manufacturing order that pegs one of its lines (a link a process graph
declares as a served dependency, ``pegged_by``). ``plx.around`` labels every edge with
exactly one kind (reference, identity, allocation, dependency) and its tier, the
review/evidence state; no ontology tool returns the component as one transaction: no
object id outside the records, no record merged into its twin, no total summed across
link kinds. Identity is one rule in every tool (``ot.identity_link_type``): the order's
verified ``same_record`` key to its customer PO (``SalesOrder.references_customer_po``,
two types) is a reference in plx.around, plx.object, plx.identity and plx.aggregate, and
the reviewed twin an identity in all four. All identifiers are synthetic. Evidence:
isolated-fixture.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from ontology import reviews, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from operating import ontology_tools_evidence as ev
from operating import ontology_tools_process as op
from operating import ontology_tools_review as rt
from operating import ontology_tools_traverse as tt
from tests import test_ontology_tools_aggregate as ta
from tests import test_ontology_tools_review as tr
from tests.test_review_allocation import Builder

CONTRACTS = tr.CONTRACTS
SCHEMA: ot.SchemaView | None = None
SO = "SalesOrder:portal.app_sales_order:so1"
TWIN = "SalesOrder:portal.fm_order_entry:351"
SO2 = "SalesOrder:portal.app_sales_order:so2"
LINE = "SalesOrderLine:portal.app_sales_order_line:l1"
MO = "AssemblyOrder:portal.app_manufacturing_order:mo1"
PACKET = "AssemblyOrderPacket:sharepoint.ao_packets:p1"
MAIL = 'Email:graph_mail.messages:{"id":"M-7","mailbox":"plan@plx.test"}'
MAIL_HANDLE = "graph-mail://plan@plx.test/M-7@CK-1"
CPO = "CustomerPurchaseOrder:portal.app_sales_order.po_reference:so1"
RECORDS = {SO, TWIN, SO2, LINE, MO, PACKET, MAIL, CPO}
A, B = tr.employee_session(tr.OID_A), tr.employee_session(tr.OID_B)


def setUpModule():
    global SCHEMA
    SCHEMA = ta.SCHEMA = ot.SchemaView.from_compiled(schema_loader.load_schema())


def object_ids(value):
    """Every object id string anywhere in a tool output."""
    if isinstance(value, dict):
        return {i for v in value.values() for i in object_ids(v)}
    if isinstance(value, list):
        return {i for v in value for i in object_ids(v)}
    if isinstance(value, str) and ot._OBJECT_ID.match(value):
        return {value}
    return set()


class LinkKindTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.reviews = reviews.ReviewStore(Path(tmp.name) / "reviews.sqlite",
                                           clock=lambda: dt.datetime(2099, 1, 20,
                                                                     tzinfo=dt.timezone.utc))
        s = self.store = ta.Store()
        for oid in (SO, TWIN, SO2, LINE, MO, PACKET, CPO):
            s.obj(oid)
        s.val(SO, "order_number", "identifier", "SO-1001")
        s.val(SO2, "order_number", "identifier", "SO-1002")
        s.val(TWIN, "order_number", "identifier", "SO-1001")
        s.val(PACKET, "produced_quantity", "quantity", "1920", unit="each", role="produced")
        s.link("SalesOrderLine.line_of", LINE, SO)
        s.link("AssemblyOrder.for_order_line", MO, LINE)
        # A verified stored key between two types: a reference, never an identity.
        s.link("SalesOrder.references_customer_po", SO, CPO, kind="same_record")
        for order in (SO, SO2):   # the packet names both orders: a shared AO
            s.link("AssemblyOrderPacket.for_sales_order", PACKET, order, tier="corroborated",
                   kind="mention")
        s.conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                       (MAIL, "Email", "graph_mail.messages", '["M-7"]', "mail:plan@plx.test",
                        ta.T0, None))
        s.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                       (MAIL, MAIL_HANDLE, ta._h(MAIL_HANDLE), "mail", "[]", ta.T0, None))
        s.handles[MAIL] = (MAIL_HANDLE, "mail")
        s.link("Email.mentions_sales_order", MAIL, SO, tier="lead", kind="mention")
        a, b = A.employee.principal, B.employee.principal
        policy = ot.ReadPolicy("plx", {"api:*": {a, b}, "sharepoint:*": {a, b},
                                       "mail:*": {a}},
                               {m: {a, b} for m in store_ddl.MARKINGS})
        self.base = ot.OntologyTools(
            ot.StoreReader(s.conn), policy, schema=SCHEMA, contracts=CONTRACTS,
            clock=lambda: ta.NOW, item_gate=lambda actor, handle: (
                not handle.startswith("graph-mail://") or a in actor.principals))
        self.walk = tt.TraverseTools(self.base)
        review = rt.ReviewTools(self.base, self.reviews, session=A, gate=tr.identity_reviewer)

        def evidence(oid, path):
            handle = s.handles[oid][0]
            return {"l1_handle": handle, "digest": ta._h(handle), "field_path": path}

        for arguments in (
                {"decision": "accept_link", "subject": SO, "target": TWIN,
                 "link_type": "SalesOrder.same_record_as",
                 "evidence": [evidence(SO, "row.order_number"),
                              evidence(TWIN, "row.order_number")]},
                *({"decision": "allocate", "subject": PACKET, "target": order,
                   "link_type": "AssemblyOrderPacket.for_sales_order",
                   "allocation": {"property": "produced_quantity", "quantity": share,
                                  "unit": "each"},
                   "evidence": [evidence(PACKET, "row.produced_quantity")]}
                  for order, share in ((SO, "600"), (SO2, "1320")))):
            out = review.call("plx.review", dict(arguments, mode="record"), A.actor)
            self.assertNotIn("error", out, out)
        reviews.link_step(self.reviews, "plx")(
            Builder(s.conn), type("Projector", (), {"links": SCHEMA.link_types})())

    def call(self, tools, name, arguments, actor=A.actor):
        out = tools.call(name, arguments, actor)
        self.assertEqual(validate(out, CONTRACTS["tools"][name]["output"], root=CONTRACTS),
                         [], out)
        self.assertNotIn("error", out, out)
        self.assertTrue(object_ids(out) <= RECORDS, object_ids(out) - RECORDS)
        return out

    def around(self, root, path, actor=A.actor):
        return self.call(self.walk, "plx.around", {
            "from": root, "path": path, "tier_floor": "lead",
            "response_format": "detailed"}, actor)

    def test_every_hop_names_its_kind_and_state_and_nothing_is_one_transaction(self):
        dependency_chain = self.around(MO, [
            {"link_type": "AssemblyOrder.for_order_line", "direction": "forward"},
            {"link_type": "SalesOrderLine.line_of", "direction": "forward"},
            {"link_type": "SalesOrder.same_record_as", "direction": "forward"}])
        packets = self.around(SO, [{"link_type": "AssemblyOrderPacket.for_sales_order",
                                    "direction": "reverse"}])
        mail = self.around(SO, [{"link_type": "Email.mentions_sales_order",
                                 "direction": "reverse"}])
        edges = {(e["link_type"], e["kind"], e["tier"])
                 for out in (dependency_chain, packets, mail) for e in out["edges"]}
        self.assertEqual(edges, {
            ("AssemblyOrder.for_order_line", "dependency", "verified"),
            ("SalesOrderLine.line_of", "reference", "verified"),
            ("SalesOrder.same_record_as", "identity", "reviewed"),
            ("AssemblyOrderPacket.for_sales_order", "reference", "corroborated"),
            ("AssemblyOrderPacket.for_sales_order", "allocation", "reviewed"),
            ("Email.mentions_sales_order", "reference", "lead")})
        # The reviewed twin is a second record reached by an identity hop, not a merge.
        self.assertEqual([o["object_id"] for o in dependency_chain["objects"]], [TWIN])

        # A reader without the mailbox reaches no mention and learns nothing of it.
        hidden = self.around(SO, [{"link_type": "Email.mentions_sales_order",
                                   "direction": "reverse"}], B.actor)
        self.assertEqual((hidden["edges"], hidden["objects"]), ([], []))

        # plx.object names each record by itself, never the pair as one.
        for oid in (SO, TWIN):
            out = self.call(self.base, "plx.object", {"object_id": oid,
                                                      "response_format": "detailed"})
            self.assertEqual(out["object"]["object_id"], oid)

        # plx.identity joins the two orders only: an allocation is never an identity.
        identity = self.call(ev.EvidenceTools(self.base), "plx.identity",
                             {"object_ids": [PACKET, SO]})
        self.assertEqual({(e["from"], e["link_type"], e["to"]) for e in identity["edges"]},
                         {(SO, "SalesOrder.same_record_as", TWIN)})

        # No total across kinds: one order held in two systems (a person's reviewed
        # identity) is never counted twice, and the shared packet gives each order its
        # reviewed share only, never its whole quantity or a share plus the mention.
        aggregate = oa.AggregateTools(self.base)
        twins = self.call(aggregate, "plx.aggregate", {
            "set": self.base.sets.new(A.actor, [SO, TWIN]), "measure": {"op": "count"}})
        self.assertEqual([g["value"] for g in twins["groups"]], [None])
        self.assertIn("cross_system_twins", {g["reason"] for g in twins["gaps"]})
        produced = self.call(aggregate, "plx.aggregate", {
            "set": self.base.sets.new(A.actor, [PACKET]),
            "measure": {"property": "produced_quantity", "op": "sum"},
            "group_by": ["order_number"],
            "path": [{"link_type": "AssemblyOrderPacket.for_sales_order",
                      "direction": "forward"}]})
        values = {g["key"]["order_number"]["untrusted_text"]: g["value"]
                  for g in produced["groups"]}
        self.assertEqual(values, {"SO-1001": "600", "SO-1002": "1320"})
        self.assertIn("reviewed_allocation_used", {g["reason"] for g in produced["gaps"]})

    def test_identity_is_one_rule_in_every_tool(self):
        cross = self.around(SO, [{"link_type": "SalesOrder.references_customer_po",
                                  "direction": "forward"}])
        self.assertEqual({(e["kind"], e["tier"]) for e in cross["edges"]},
                         {("reference", "verified")})
        # plx.object: the reviewed twin is its identity, the customer PO key a count.
        out = self.call(self.base, "plx.object", {"object_id": SO,
                                                  "response_format": "detailed"})
        self.assertEqual({(e["link_type"], e["to"], e["tier"]) for e in out["identity"]},
                         {("SalesOrder.same_record_as", TWIN, "reviewed")})
        counted = {c["link_type"] for c in out["linked_counts"]}
        self.assertIn("SalesOrder.references_customer_po", counted)
        self.assertNotIn("SalesOrder.same_record_as", counted)
        # plx.identity: the twin only, from either root.
        identity = self.call(ev.EvidenceTools(self.base), "plx.identity",
                             {"object_ids": [SO, CPO]})
        self.assertEqual({(e["from"], e["link_type"], e["to"]) for e in identity["edges"]},
                         {(SO, "SalesOrder.same_record_as", TWIN)})
        # plx.aggregate: the order and its customer PO are two records, never twins.
        aggregate = oa.AggregateTools(self.base)
        pair = self.call(aggregate, "plx.aggregate", {
            "set": self.base.sets.new(A.actor, [SO, CPO]), "measure": {"op": "count"}})
        self.assertNotIn("cross_system_twins", {g["reason"] for g in pair["gaps"]})
        self.assertEqual(sum(int(g["value"]) for g in pair["groups"]), 2)

    def test_a_reviewed_identity_survives_the_cut_behind_text_matched_leads(self):
        # 60 text-matched same_record_as leads whose link ids sort before the review's.
        leads = [f"SalesOrder:portal.app_sales_order:t{n:02d}" for n in range(60)]
        for lead in leads:
            self.store.obj(lead)
            self.store.link("SalesOrder.same_record_as", SO, lead, tier="lead",
                            kind="text_match")
        out = self.base.call("plx.object", {"object_id": SO}, A.actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.object"]["output"],
                                  root=CONTRACTS), [], out)
        self.assertEqual(len(out["identity"]), ot.MAX_IDENTITY)
        self.assertEqual((out["identity"][0]["to"], out["identity"][0]["tier"]),
                         (TWIN, "reviewed"))
        self.assertEqual({e["tier"] for e in out["identity"][1:]}, {"lead"})
        cut = [g for g in out["gaps"] if g["reason"] == "identity_truncated"]
        self.assertEqual([g["subject"] for g in cut], [SO])
        self.assertIn("61 identity links", cut[0]["detail"])

    def test_unloadable_process_graphs_are_a_gap_and_a_changed_graph_is_reread(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        graphs = Path(tmp.name) / "process"
        shutil.copytree(op.PROCESS_DIR, graphs)
        graph = graphs / "manufacturing_order.json"
        good = graph.read_text(encoding="utf-8")
        path = [{"link_type": "AssemblyOrder.for_order_line", "direction": "forward"},
                {"link_type": "SalesOrderLine.line_of", "direction": "forward"},
                {"link_type": "SalesOrder.same_record_as", "direction": "forward"}]
        with mock.patch.object(op, "PROCESS_DIR", graphs):
            graph.write_text("{", encoding="utf-8")
            broken = self.around(MO, path)
            self.assertEqual({(e["link_type"], e.get("kind")) for e in broken["edges"]}, {
                ("AssemblyOrder.for_order_line", None), ("SalesOrderLine.line_of", None),
                ("SalesOrder.same_record_as", "identity")})
            gap = [g for g in broken["gaps"] if g["reason"] == "process_graphs_unavailable"]
            self.assertEqual([g["subject"] for g in gap],
                             ["AssemblyOrder.for_order_line, SalesOrderLine.line_of"])
            graph.write_text(good, encoding="utf-8")   # fixed: read again, no restart
            fixed = self.around(MO, path)
            self.assertIn(("AssemblyOrder.for_order_line", "dependency"),
                          {(e["link_type"], e["kind"]) for e in fixed["edges"]})
            self.assertNotIn("process_graphs_unavailable",
                             {g["reason"] for g in fixed["gaps"]})
            # Broken again by an edit that keeps the size and modification time.
            stat = graph.stat()
            graph.write_text("[" + good[1:], encoding="utf-8")
            os.utime(graph, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertEqual((graph.stat().st_size, graph.stat().st_mtime_ns),
                             (stat.st_size, stat.st_mtime_ns))
            self.assertIn("process_graphs_unavailable",
                          {g["reason"] for g in self.around(MO, path)["gaps"]})
            # Many edge gaps (25 malformed links) never cut the unknown-kind gap.
            for n in range(25):
                line = f"SalesOrderLine:portal.app_sales_order_line:m{n:02d}"
                self.store.obj(line)
                self.store.link("AssemblyOrder.for_order_line", MO, line)
                self.store.conn.execute("UPDATE links SET reason = '[1]' WHERE "
                                        "link_id = ?", (f"lnk-{self.store.n}",))
            out = self.walk.call("plx.around", {
                "from": MO, "path": path[:1], "tier_floor": "lead", "limit": 50,
                "response_format": "detailed"}, A.actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.around"]["output"],
                                  root=CONTRACTS), [], out)
        reasons = [g["reason"] for g in out["gaps"]]
        self.assertEqual(len(reasons), tt.MAX_GAPS)
        self.assertIn("malformed_link", reasons)
        self.assertIn("process_graphs_unavailable", reasons)

    def test_link_kind_is_decided_by_evidence_and_declaration_not_by_name(self):
        # A reviewed link with no allocation feature is the link type's kind, and an
        # allocation feature on an unreviewed link is not an allocation.
        allocated = json.dumps([{"feature": reviews.ALLOCATED, "agrees": True}])
        row = {"link_type": "AssemblyOrderPacket.for_sales_order", "binding_kind": "mention",
               "reason": allocated}
        self.assertEqual(self.walk._link_kind(row, frozenset()), "reference")
        row = dict(row, binding_kind="reviewed", reason='[{"feature": "person_reviewed"}]')
        self.assertEqual(self.walk._link_kind(row, frozenset()), "reference")
        # A same_record binding between two types is a reference, not an identity.
        row = {"link_type": "ItemLedgerEntry.posted_by_receipt", "binding_kind": "same_record",
               "reason": "[]"}
        self.assertEqual(self.walk._link_kind(row, frozenset()), "reference")
        # An undeclared link type is never given a kind: it fails closed.
        with self.assertRaises(ot.ToolError):
            self.walk._link_kind(dict(row, link_type="Nope.not_declared"), frozenset())


if __name__ == "__main__":
    unittest.main()
