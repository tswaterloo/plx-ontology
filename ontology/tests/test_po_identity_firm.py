"""Firm Portal-BC purchase-order twins (``ontology.identity_scoring.corroborate_po_twins``,
the ``twins`` build step): a pair the schema joins only on the PO number text stays a
``lead`` unless its supplier and its order date or total agree, and is then raised by a
separate ``corroborated`` ``same_record`` link that cites what it read; never on the
number alone, never with a conflicting field, never with several candidates.

Evidence label: ``isolated-fixture`` (synthetic Portal and BC captures through the real
projector, the compiled repository schema and the read tools; no source is read). The live
count over the captured projection is the proposed spec
``ontology/tests/fixtures/identity.po_same_record.proposed.json``: it moves to
the live-check specs once a human adds ``scripts.po_twin_probe`` to
the live verifier ``ALLOWED_MODULES`` (a reviewed security change); until then the
count is live-unverified.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from datetime import datetime, timezone

from ontology import identifiers, store_ddl
from ontology import identity_scoring as ids
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema
from ontology.store import StoreBuilder
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from operating import ontology_tools_evidence as oe
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt
from operating import build_steps
from scripts import eval_flagship_path as fp
from tests.test_ontology_tools_review import CONTRACTS

T = "2026-10-01T00:00:00+00:00"
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
SCHEMA = load_schema()
TWIN = ids.TWIN_LINK
FULL = Actor("plx", frozenset({"staff:purchasing"}), "restricted")
NO_SUPPLIER = Actor("plx", frozenset({"staff:orders"}), "restricted")   # no supplier audience
NO_FINANCE = Actor("plx", frozenset({"staff:buyer"}), "restricted")    # no price marking


class Source:
    def __init__(self, source, rows):
        self.source, self.rows = source, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        digest = hashlib.sha256(json.dumps([rel, row], sort_keys=True).encode()).hexdigest()
        if self.source == "bc":
            return CapturedRecord("bc", rel, f"bc_manufacturing/{row['id']}", row,
                                  f"bc://production/bc_manufacturing/{rel}/{row['id']}@X", "row",
                                  digest, T, None, T, f"bc:production:bc_manufacturing:{rel}",
                                  capture_lineage="bc_manufacturing")
        key = json.dumps({"id": row["id"]})
        return CapturedRecord("portal_api", rel, key, row, f"api://{rel}/{row['id']}@X", "row",
                              digest, T, None, T, f"api:{rel}")


def supplier(i, name, code=None):
    return {"id": f"s{i}", "code": code or f"{1000 + i}", "name": name,
            "external_id": f"fm-supp-{i}"}


def portal_po(i, number, supplier_id, *, day="2026-03-02", total="1000.00", source=None):
    return {"id": f"p{i}", "po_number": number, "supplier_id": supplier_id,
            "date_generated": day, "total_cost": total, "currency": "CAD",
            "custom_fields": {"source": source or "etl-silver-to-gold"}}


def bc_po(i, number, vendor, *, vendor_number=None, day=None, total=1003.0):
    row = {"id": f"b{i}", "number": number, "vendorNumber": vendor_number or f"V{i:04d}",
           "vendorName": vendor, "totalAmountIncludingTax": total, "currencyCode": "CAD"}
    if day:
        row["orderDate"] = day
    return row


def world(pairs):
    """``pairs``: (portal supplier, portal PO, BC PO) rows, each part optional."""
    portal = {"public.app_supplier": [], "public.app_purchase_order": []}
    bc = {"purchaseOrders": []}
    for sup, ppo, bpo in pairs:
        if sup is not None:
            portal["public.app_supplier"].append(sup)
        if ppo is not None:
            portal["public.app_purchase_order"].append(ppo)
        if bpo is not None:
            bc["purchaseOrders"].append(bpo)
    return {"portal_api": portal, "bc": bc}


def build(path, rows):
    report = build_store(path, SCHEMA, {n: Source(n, r) for n, r in rows.items()},
                         normalizer=identifiers.normalize, clock=lambda: NOW,
                         post_project={"twins": ids.twin_step})
    return report["post_project"]["twins"]


def po(source, i):
    if source == "bc":
        return f'PurchaseOrder:bc.purchase_orders:{{"id":"b{i}"}}'
    return f'PurchaseOrder:portal.app_purchase_order:{{"id":"p{i}"}}'


def tools(case, path):
    everyone = {"staff:purchasing", "staff:orders", "staff:buyer"}
    policy = ot.ReadPolicy(tenant="plx", audiences={
        "api:public.app_purchase_order": everyone,
        "api:public.app_supplier": {"staff:purchasing", "staff:buyer"},
        "bc:*": everyone},
        # The PO total is purchasing_prices (44e13e0177); the
        # buyer holds neither it nor finance, so it is the reader without the totals.
        markings={m: ({"staff:purchasing", "staff:orders"}
                      if m in ("finance", "purchasing_prices") else everyone)
                  for m in store_ddl.MARKINGS})
    reader = ot.StoreReader.open(path)
    case.addCleanup(reader.conn.close)
    return ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(SCHEMA),
                            grammar=identifiers, contracts=CONTRACTS, clock=lambda: NOW)


class Workspace:
    """The two calls ``eval_flagship_path.ask_step`` makes of a console workspace, over the read
    tools, with an Ask whose answer cites ``citations``."""

    def __init__(self, base, traverse, actor, citations):
        self.base, self.traverse, self.actor, self.citations = base, traverse, actor, citations

    def _call(self, name, args):
        tool = self.traverse if name == "plx.around" else self.base
        return tool.call(name, args, self.actor)

    def ask_run(self, args):
        return {"claims": [{"text": "The order is open; its twin states the same supplier.",
                            "citations": self.citations}], "loop": {},
                "outcome": "answered", "reason": "answered", "withheld": 0}


AMBER = "Amber Polymers Inc."


class PoIdentityFirmTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.n = 0

    def run_world(self, pairs):
        self.n += 1
        self.path = self.dir / f"store{self.n}.sqlite"
        report = build(self.path, world(pairs))
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        links = [dict(r) for r in conn.execute(
            "SELECT * FROM links WHERE link_type = ? ORDER BY link_id", (TWIN,))]
        return report, links

    def by_tier(self, links):
        out = {}
        for link in links:
            out.setdefault(link["tier"], []).append(link)
        return out

    # ---- the rule
    def test_exact_number_agreeing_supplier_and_total_becomes_firm_with_evidence(self):
        report, links = self.run_world([(supplier(1, AMBER),
                                         portal_po(1, "PO#7001", "s1", day=None),
                                         bc_po(1, "PO7001", "AMBER POLYMERS"))])
        tiers = self.by_tier(links)
        self.assertEqual(report["counts"]["firm"], 1)
        self.assertEqual(report["link_tiers"], {"corroborated": 1})
        [lead], [firm] = tiers["lead"], tiers["corroborated"]
        self.assertEqual(lead["binding_kind"], "text_match")   # the lead stays as it was
        self.assertEqual(firm["binding_kind"], "same_record")
        self.assertEqual((firm["from_object"], firm["to_object"]), (po("api", 1), po("bc", 1)))
        reason = {f["feature"]: f for f in json.loads(firm["reason"])}
        self.assertEqual(set(reason), {"po_number", "supplier_name", "total_cost",
                                       "corroborates"})
        self.assertTrue(all(f["agrees"] is True for f in reason.values()))
        self.assertEqual(reason["corroborates"]["link_id"], lead["link_id"])
        self.assertEqual(reason["total_cost"]["currency"], "CAD")
        self.assertEqual(Decimal(reason["total_cost"]["share_apart"]), Decimal("0.0030"))
        self.assertEqual(reason["total_cost"]["tax_basis"], ["unknown", "incl_tax"])
        paths = {c["field_path"] for f in reason.values() for c in f.get("citations", ())}
        self.assertEqual(paths, {"row.po_number", "row.number", "row.name", "row.vendorName",
                                 "row.total_cost", "row.totalAmountIncludingTax"})
        self.assertEqual(json.loads(firm["evidence_handles"]), [
            "api://public.app_purchase_order/p1@X", "api://public.app_supplier/s1@X",
            "bc://production/bc_manufacturing/purchaseOrders/b1@X"])
        # the totals it read (PurchaseOrder.total_cost: purchasing_prices)
        self.assertEqual(json.loads(firm["markings"]), ["purchasing_prices"])

    def test_the_live_check_spec_passes_on_its_probe_over_a_built_projection(self):
        import contextlib
        import io
        from verify import live_verify
        from scripts import po_twin_probe
        self.run_world([(supplier(1, AMBER), portal_po(1, "PO#7001", "s1", day=None),
                         bc_po(1, "PO7001", "AMBER POLYMERS")),
                        (supplier(2, "Birch Resins"), portal_po(2, "PO#7002", "s2"),
                         bc_po(2, "PO7002", "CEDAR METALS"))])
        spec = json.loads((Path(__file__).resolve().parent / "fixtures"
                           / "identity.po_same_record.proposed.json").read_text())
        argv = [str(self.path) if a == "{session_state}/ontology.sqlite" else a
                for a in spec["command"][3:]]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = po_twin_probe.main(argv)
        document = json.loads(out.getvalue())
        self.assertEqual(live_verify.check_expectations(spec, code, document), [])
        self.assertEqual((document["firm_twins"], document["judged_firm"],
                          document["bc_pos_with_firm_twin"], document["leads"],
                          document["conflicts"]), (1, 1, 1, 2, 1))
        self.assertEqual(document["samples"][0]["outcome"], "firm")
        self.assertNotIn("7001", out.getvalue())   # codes and counts, never a value
        self.assertNotIn("AMBER", out.getvalue().upper())
        self.assertEqual(document["lead_shapes"], {"bc:PO9999|portal:PO#9999": 2})
        self.assertEqual(document["supplier_verdicts"], {"agreed": 1, "conflict": 1})
        # A firm count of 0 is never a pass.
        self.assertIn("bc_pos_with_firm_twin < 1", live_verify.check_expectations(
            spec, 0, dict(document, bc_pos_with_firm_twin=0)))
        # A missing projection is blocked, never a pass.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            po_twin_probe.main(["--store", str(self.dir / "absent.sqlite")])
        self.assertEqual(json.loads(out.getvalue()), {"sources_blocked": ["projection_missing"]})

    def test_a_different_supplier_stays_a_lead(self):
        report, links = self.run_world([(supplier(1, AMBER),
                                         portal_po(1, "PO#7001", "s1", day="2026-03-03"),
                                         bc_po(1, "PO7001", "Granite Resins Ltd",
                                               day="2026-03-03", total=1000.0))])
        self.assertEqual(set(self.by_tier(links)), {"lead"})
        self.assertEqual(report["counts"]["supplier_conflict"], 1)
        # its supplier number differing is a conflict too (a code the Portal sends to BC)
        report, links = self.run_world([(supplier(1, AMBER, code="AMB01"),
                                         portal_po(1, "PO#7001", "s1"),
                                         bc_po(1, "PO7001", "AMBER POLYMERS",
                                               vendor_number="GRA02", day="2026-03-02"))])
        self.assertEqual(set(self.by_tier(links)), {"lead"})
        self.assertEqual(report["counts"]["supplier_conflict"], 1)

    def test_two_candidates_stay_leads(self):
        report, links = self.run_world([
            (supplier(1, AMBER), portal_po(1, "PO#7001", "s1"),
             bc_po(1, "PO7001", "AMBER POLYMERS", day="2026-03-02")),
            (None, portal_po(2, "PO7001", "s1"), None)])
        tiers = self.by_tier(links)
        self.assertEqual(set(tiers), {"lead"})
        self.assertEqual(len(tiers["lead"]), 2)
        self.assertEqual(report["counts"]["ambiguous"], 2)
        self.assertEqual(report["counts"]["firm"], 0)

    def test_number_alone_or_a_conflicting_field_never_promotes(self):
        cases = {
            # supplier agrees, nothing else compared
            "no_secondary_agreement": (supplier(1, AMBER), portal_po(1, "PO#7001", "s1",
                                                                     day=None, total=None),
                                       bc_po(1, "PO7001", "AMBER POLYMERS")),
            # number and date and total, but the supplier is not stated on one side
            "supplier_not_agreed": (None, portal_po(1, "PO#7001", "s9"),
                                    bc_po(1, "PO7001", "AMBER POLYMERS", day="2026-03-02")),
            # supplier and total agree, the order date is a week apart
            "conflict": (supplier(1, AMBER), portal_po(1, "PO#7001", "s1", day="2026-03-02"),
                         bc_po(1, "PO7001", "AMBER POLYMERS", day="2026-03-09")),
            # a Portal-minted order (never authority) is never raised
            "lineage_not_authority": (supplier(1, AMBER),
                                      portal_po(1, "PO#7001", "s1",
                                                source="portal-generate-po"),
                                      bc_po(1, "PO7001", "AMBER POLYMERS", day="2026-03-02")),
        }
        for code, pair in cases.items():
            with self.subTest(code):
                report, links = self.run_world([pair])
                self.assertEqual(set(self.by_tier(links)), {"lead"})
                self.assertEqual(report["counts"][code], 1, report["counts"])

    def test_a_total_apart_on_unknown_tax_basis_is_unknown_and_a_sent_number_agrees(self):
        # the Portal total has no declared tax basis: 13% apart is neither agreement nor a
        # conflict; the date agrees, and the Sage-style code the Portal sends as the BC
        # vendor No. agrees with the order's vendor number (BC states no vendor name here)
        report, links = self.run_world([(supplier(1, AMBER, code="AMB01"),
                                         portal_po(1, "PO#7001", "s1"),
                                         bc_po(1, "PO7001", None, day="2026-03-01",
                                               vendor_number="amb01 ", total=1130.0))])
        [firm] = self.by_tier(links)["corroborated"]
        reason = {f["feature"]: f for f in json.loads(firm["reason"])}
        self.assertEqual(set(reason) - {"corroborates", "po_number"},
                         {"supplier_number", "order_date"}, reason)
        self.assertEqual(reason["order_date"]["days_apart"], 1)
        self.assertEqual(json.loads(firm["markings"]), [])   # no total was used

    def test_a_pure_digit_code_is_never_compared_with_a_vendor_number(self):
        report, links = self.run_world([(supplier(1, "Unrelated Name", code="0001"),
                                         portal_po(1, "PO#7001", "s1"),
                                         bc_po(1, "PO7001", "Other Vendor", vendor_number="0001",
                                               day="2026-03-02"))])
        self.assertEqual(report["counts"]["supplier_conflict"], 1)   # by name, not number
        self.assertEqual(set(self.by_tier(links)), {"lead"})

    def test_a_rejected_or_already_firm_pair_is_not_raised_and_a_rerun_writes_once(self):
        self.run_world([(supplier(1, AMBER), portal_po(1, "PO#7001", "s1"),
                         bc_po(1, "PO7001", "AMBER POLYMERS", day="2026-03-02"))])
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        builder = SimpleNamespace(connection=conn)

        def add(rows):   # the build's own link writer, on the built store
            return StoreBuilder._add_links(builder, rows)
        again = ids.corroborate_po_twins(conn, SCHEMA, add, normalizer=identifiers.normalize)
        self.assertEqual(again["link_tiers"], {"corroborated": 1})
        self.assertEqual(conn.execute("SELECT count(*) FROM links WHERE tier = 'corroborated'")
                         .fetchone()[0], 1)   # rewritten, never duplicated
        conn.row_factory = sqlite3.Row
        lead = conn.execute("SELECT * FROM links WHERE binding_kind = 'text_match'").fetchone()
        for tier, code in (("contradicted", "rejected"), ("reviewed", "already_firm")):
            conn.execute("DELETE FROM links WHERE binding_kind = 'reviewed'")
            add([{"link_id": f"rvw-{tier}", "link_type": TWIN, "from_object": po("bc", 1),
                  "to_object": po("api", 1), "tier": tier, "binding_kind": "reviewed",
                  "link_contract": None, "why_not": "[]",
                  "reason": json.dumps([{"feature": "person_reviewed",
                                         "agrees": tier == "reviewed"}]),
                  "evidence_handles": lead["evidence_handles"],
                  "reviewed_by": "staff:purchasing",
                  "reviewed_at": T, "review_action_id": "rev-1", "markings": "[]",
                  "recorded_at": lead["recorded_at"], "superseded_at": None}])
            report = ids.corroborate_po_twins(conn, SCHEMA, add,
                                              normalizer=identifiers.normalize)
            self.assertEqual(report["counts"][code], 1, tier)
            self.assertEqual(report["link_tiers"], {"corroborated": 0}, tier)

    def test_the_build_steps_run_the_twin_step_after_reviews(self):
        steps = build_steps.post_project(self.dir / "state", "plx")
        self.assertIs(steps["twins"], ids.twin_step)
        # ``build_store`` runs the steps in name order; twins reads the reviewed links.
        order = sorted(steps)
        self.assertLess(order.index("reviews"), order.index("twins"))

    def test_cardinality_one_ten_sixty(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                pairs = [(supplier(i, f"Supplier Number {i} Inc"),
                          portal_po(i, f"PO#{7000 + i}", f"s{i}"),
                          bc_po(i, f"PO{7000 + i}", f"Supplier Number {i}", day="2026-03-02"))
                         for i in range(n)]
                started = time.monotonic()
                report, links = self.run_world(pairs)
                tiers = self.by_tier(links)
                self.assertEqual(report["counts"]["firm"], n)
                self.assertEqual(len(tiers["corroborated"]), n)
                self.assertEqual(len({(l["from_object"], l["to_object"])
                                      for l in tiers["corroborated"]}), n)
                self.assertEqual(len(tiers["lead"]), n)
                self.assertLess(time.monotonic() - started, 60)

    # ---- readers
    def test_readers_see_the_firm_twin_only_with_everything_it_cites(self):
        self.run_world([(supplier(1, AMBER), portal_po(1, "PO#7001", "s1", day=None),
                         bc_po(1, "PO7001", "AMBER POLYMERS"))])
        base = tools(self, self.path)
        traverse = tt.TraverseTools(base)
        evidence = oe.EvidenceTools(base, traverse=traverse)
        process = op.ProcessTools(base)
        aggregate = oa.AggregateTools(base, traverse)
        path = [{"link_type": TWIN, "direction": "forward"}]
        for actor, firm in ((FULL, True), (NO_SUPPLIER, False), (NO_FINANCE, False)):
            with self.subTest(actor=sorted(actor.principals)):
                at = {floor: {o["object_id"] for o in traverse.call("plx.around", {
                    "from": po("api", 1), "path": path, "tier_floor": floor}, actor)
                    .get("objects", ())} for floor in ("corroborated", "lead")}
                self.assertEqual(at["corroborated"], {po("bc", 1)} if firm else set())
                self.assertEqual(at["lead"], {po("bc", 1)})   # the lead is there for all
                outputs = [traverse.call("plx.around", {"from": po("api", 1), "path": path,
                                                        "tier_floor": "lead"}, actor),
                           base.call("plx.object", {"object_id": po("bc", 1)}, actor),
                           base.call("plx.object", {"object_id": po("api", 1)}, actor),
                           evidence.call("plx.documents", {"object_id": po("bc", 1)}, actor),
                           process.call("plx.process", {"from": po("bc", 1)}, actor),
                           traverse.call("plx.timeline", {"from": po("bc", 1)}, actor),
                           aggregate.call("plx.aggregate", {
                               "set": base.sets.new(actor, [po("bc", 1), po("api", 1)]),
                               "measure": {"op": "count"}}, actor)]
                text = json.dumps(outputs, default=str)
                self.assertNotIn("public.app_supplier/s1", text)   # never a citation here
                self.assertEqual('"corroborated"' in text, firm)
                self.assertEqual('"supplier_name"' in text, firm)

    def test_the_flagship_never_counts_a_bc_portal_twin_as_the_second_source(self):
        # A firm twin is still the same order in two ledgers: the flagship Ask needs a
        # document naming the number as its second source (eval_flagship_path rule 8), so an
        # answer citing only the BC order and its Portal twin is not cross-system.
        self.run_world([(supplier(1, AMBER), portal_po(1, "PO#7001", "s1", day=None),
                         bc_po(1, "PO7001", "AMBER POLYMERS"))])
        base = tools(self, self.path)
        traverse = tt.TraverseTools(base)
        cites = ["bc://production/bc_manufacturing/purchaseOrders/b1@X",
                 "api://public.app_purchase_order/p1@X"]
        late = {"numbers": ["PO#7001", "PO7001"], "records": cites,
                "headers": [{"system": "bc", "citation": cites[0]},
                            {"system": "portal", "citation": cites[1]}],
                "late_lines": [{"system": "bc", "citation": cites[0], "due": "2026-09-01",
                                "date_source": cites[0], "line": False}],
                "governing_dates": ["2026-09-01"]}
        for actor in (FULL, NO_SUPPLIER):
            with self.subTest(actor=sorted(actor.principals)):
                out = fp.ask_step(Workspace(base, traverse, actor, cites), po("bc", 1),
                                  fp.QUESTION, late=late, others=set(),
                                  confirm=lambda handle, numbers: True, identify=False)
                self.assertEqual(out["status"], "fail")
                self.assertNotEqual(out["reason"], "names_late_lines_and_date_cross_system")
                self.assertEqual(out["second_sources"], 0)

if __name__ == "__main__":
    unittest.main()
