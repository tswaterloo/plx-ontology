"""The BC-Portal purchase-order link from BC ``Your Reference`` (the FileMaker PO number,
ODataV4 Page 50, captured as the field page ``purchaseOrderReferences``):
``ontology.identity_scoring.link_po_your_references`` / the ``your_reference`` build step.

The link stands on data evidence (decision ``2026-10-27-your-reference-evidence``, rule
``ids.your_reference_verdict``, the same rule as the flagship oracle's
``evals.oracle.po_reference.decide``, ``RuleParityTests``): a BC order whose Your Reference
names exactly one Portal order's ``fmPoNumber``, with the supplier or the order date
corroborating it and nothing conflicting, is firm (a ``corroborated`` ``same_record`` link, never an identity
tier), citing the Page 50 record, the Portal value it matched and each corroborating field.
An ambiguous, conflicting or uncorroborated match is a ``lead`` saying why; a missing,
blank or unusable reference links nothing and is counted under a typed reason.

Evidence label: ``isolated-fixture`` (synthetic Portal and BC rows through the real
projector and compiled schema; one case through a real capture store filled by the
in-process ``FakeBc``). No source is read; the live count is live-unverified.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone

from evals.oracle import po_reference
from ontology import identifiers, store_ddl
from ontology import identity_scoring as ids
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema
from ontology.types import Actor
from operating import build_steps
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt
from tests.test_ontology_tools_review import CONTRACTS

T = "2026-10-01T00:00:00+00:00"
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
GEN = "0123456789abcdef"
SCHEMA = load_schema()
LINK = ids.TWIN_LINK
COMPANY = "c0000000-0000-4000-8000-0000000000aa"
FULL = Actor("plx", frozenset({"staff:purchasing"}), "restricted")
PORTAL_ONLY = Actor("plx", frozenset({"staff:portal"}), "restricted")


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


class Source:
    """A capture source; the BC one also serves the Page 50 field page."""

    def __init__(self, source, rows, references=None):
        self.source, self.rows, self.references = source, rows, references or []
        self.cuts = []

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def field_page_records(self, relation, as_known_at=None):
        self.cuts.append(as_known_at)
        assert relation == ids.YOUR_REFERENCE_SET
        return [CapturedRecord(
            "bc", relation, f"{COMPANY}/{oid}", row,
            f"bc://production/{COMPANY}/{relation}/{oid}@{GEN}-{900 + n}", "row",
            digest(relation, row), T, None, T, f"bc:{COMPANY}/{relation}",
            capture_lineage="bc_manufacturing")
            for n, (oid, row) in enumerate(self.references)]

    def capture_gap(self, relation):
        return None

    def _record(self, rel, row):
        if self.source == "bc":
            n = int(row["id"][-6:]) + 1
            return CapturedRecord("bc", rel, f"{COMPANY}/{row['id']}", row,
                                  f"bc://production/{COMPANY}/{rel}/{row['id']}@{GEN}-{n}",
                                  "row", digest(rel, row), T, None, T,
                                  f"bc:production:{COMPANY}:{rel}",
                                  capture_lineage="bc_manufacturing")
        key = json.dumps({"id": row["id"]})
        return CapturedRecord("portal_api", rel, key, row, f"api://{rel}/{row['id']}@X", "row",
                              digest(rel, row), T, None, T, f"api:{rel}")


def portal_po(i, number, fm=None, *, source="etl-silver-to-gold", supplier=None):
    fields = {"source": source}
    if fm is not None:
        fields["fmPoNumber"] = fm
    return {"id": f"p{i}", "po_number": number, "date_generated": "2026-03-02",
            "custom_fields": fields, **({"supplier_id": supplier} if supplier else {})}


def supplier(sid, name):
    # an FM-migrated supplier (external_id fm-supp-*): a Portal-minted one is never
    # authority, so its name would agree with nothing
    return {"id": sid, "name": name, "code": sid[-2:], "external_id": f"fm-supp-{sid}"}


def fm_po(i, number):
    return {"id": f"f{i}", "ponumber": number}


def bid(i):
    return f"b0000000-0000-4000-8000-{i:012d}"


def bc_po(i, number, *, order_date="2026-03-02", vendor_name=None):
    # the order date the Portal sends (portal_po's date_generated): corroborates the link
    return {"id": bid(i), "number": number, "vendorNumber": f"V{i:04d}",
            **({"orderDate": order_date} if order_date else {}),
            **({"vendorName": vendor_name} if vendor_name else {})}


def ref(i, number, your_reference):
    return (bid(i), {"No": number, "Your_Reference": your_reference,
                      "Buy_from_Vendor_No": f"V{i:04d}"})


def po(source, i):
    if source == "bc":
        return f'PurchaseOrder:bc.purchase_orders:{{"id":"{bid(i)}"}}'
    return f'PurchaseOrder:portal.app_purchase_order:{{"id":"p{i}"}}'


def tools(case, path):
    policy = ot.ReadPolicy(tenant="plx", audiences={
        "api:public.app_purchase_order": {"staff:purchasing", "staff:portal"},
        "bc:*": {"staff:purchasing"}},
        markings={m: {"staff:purchasing", "staff:portal"} for m in store_ddl.MARKINGS})
    reader = ot.StoreReader.open(path)
    case.addCleanup(reader.conn.close)
    return ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(SCHEMA),
                            grammar=identifiers, contracts=CONTRACTS, clock=lambda: NOW)


class YourReferenceLinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.n = 0

    def run_world(self, portal=(), bc=(), refs=(), fm=(), steps=None, suppliers=()):
        self.n += 1
        self.path = self.dir / f"store{self.n}.sqlite"
        self.bc = Source("bc", {"purchaseOrders": list(bc)}, list(refs))
        sources = {"bc": self.bc,
                   "portal_api": Source("portal_api", {
                       "public.app_purchase_order": list(portal),
                       "public.generated_purchase_order": list(fm),
                       "public.app_supplier": list(suppliers)})}
        report = build_store(self.path, SCHEMA, sources, normalizer=identifiers.normalize,
                             clock=lambda: NOW,
                             post_project=steps or {"your_reference": ids.your_reference_step})
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        links = [dict(r) for r in conn.execute(
            "SELECT * FROM links WHERE link_type = ? AND substr(link_id, 1, 7) = 'yrf-po-' "
            "ORDER BY link_id", (LINK,))]
        return report["post_project"]["your_reference"], links

    def nonzero(self, report):
        """The typed outcomes (and ``reference_without_order``) that are not zero."""
        return {k: v for k, v in report["counts"].items()
                if v and k not in ("bc_orders", "references")
                and k not in ids.YOUR_REFERENCE_FIRM_BY}

    def leads(self, links):
        """(from, to, rule code) of every lead link, each checked to be a lead."""
        out = set()
        for link in links:
            self.assertEqual((link["tier"], link["binding_kind"]), ("lead", "same_record"))
            [why] = json.loads(link["why_not"])
            self.assertEqual(why["feature"], "your_reference_rule")
            out.add((link["from_object"], link["to_object"], why["code"]))
        return out

    # ---- the rule
    def test_a_unique_your_reference_corroborated_by_the_order_date_is_firm_with_evidence(self):
        report, [link] = self.run_world([portal_po(1, "PO7001", fm="PO7001")],
                                        [bc_po(1, "PO106001")], [ref(1, "PO106001", "po7001 ")])
        self.assertEqual(self.nonzero(report), {"linked": 1})
        self.assertEqual((report["counts"]["firm_by_order_date"],
                          report["counts"]["firm_by_supplier"]), (1, 0))
        self.assertEqual(report["link_tiers"], {"corroborated": 1, "lead": 0})
        self.assertEqual(json.loads(link["why_not"]), [])
        self.assertEqual(report["reference_capture"], "complete")
        self.assertEqual(self.bc.cuts, [None])
        self.assertEqual((link["from_object"], link["to_object"], link["tier"],
                          link["binding_kind"]), (po("api", 1), po("bc", 1), "corroborated",
                                                  "same_record"))
        reason = {f["feature"]: f for f in json.loads(link["reason"])}
        self.assertEqual(set(reason), {"your_reference", "bc_number", "order_date"})
        self.assertTrue(all(f["agrees"] is True for f in reason.values()))
        self.assertEqual((reason["order_date"]["days_apart"], reason["order_date"]["within_days"]),
                         (0, ids.YRF_DATE_WINDOW_DAYS))
        self.assertEqual({c["field_path"] for c in reason["order_date"]["citations"]},
                         {"row.date_generated", "row.orderDate"})
        yr = reason["your_reference"]
        self.assertEqual((yr["binding"], yr["identifier_kind"]),
                         ("bc_po_your_reference", "petra_po"))
        self.assertTrue(yr["decision"].endswith("2026-10-27-your-reference-evidence.md"))
        self.assertTrue(Path(__file__).resolve().parents[2].joinpath(yr["decision"]).is_file())
        self.assertEqual([(c["l1_handle"], c["field_path"]) for c in yr["citations"]], [
            (f"bc://production/{COMPANY}/purchaseOrderReferences/{bid(1)}@{GEN}-900",
             "row.Your_Reference"),
            ("api://public.app_purchase_order/p1@X", "row.custom_fields.fmPoNumber")])
        self.assertEqual([c["field_path"] for c in reason["bc_number"]["citations"]],
                         ["row.No", "row.number"])
        self.assertNotIn("7001", link["reason"])   # cited, never copied
        self.assertEqual(json.loads(link["evidence_handles"]), [
            "api://public.app_purchase_order/p1@X",
            f"bc://production/{COMPANY}/purchaseOrders/{bid(1)}@{GEN}-2"])
        self.assertTrue(link["link_id"].startswith("yrf-po-"))

    def test_missing_blank_and_unusable_references_link_nothing_and_are_counted(self):
        report, links = self.run_world(
            [portal_po(i, f"PO{7000 + i}", fm=f"PO{7000 + i}") for i in range(1, 8)],
            [bc_po(i, f"PO10600{i}") for i in range(1, 8)],
            [ref(2, "PO106002", "   "), ref(3, "PO106003", None), ref(4, "PO106004", "see email"),
             ref(5, "PO199999", "PO7005"), ref(6, "PO106006", "PO7999"),
             ref(7, "PO106007", "PO7007"), ref(7, "PO106007", "PO7007")])
        self.assertEqual(links, [])
        self.assertEqual(self.nonzero(report), {
            "no_reference_record": 2,      # b1 has none; b7 has two current records
            "missing": 2, "not_an_identifier": 1, "bc_number_mismatch": 1, "no_match": 1})
        self.assertEqual(report["counts"]["bc_orders"], 7)
        self.assertEqual(sum(report["counts"][c] for c in ids.YOUR_REFERENCE_OUTCOMES), 7)
        # a BC number that is no PO identifier ties nothing
        report, links = self.run_world([portal_po(1, "PO7001", fm="PO7001")],
                                       [bc_po(1, "BCX1")], [ref(1, "BCX1", "PO7001")])
        self.assertEqual((links, self.nonzero(report)), ([], {"bc_number_unread": 1}))

    def test_ambiguous_references_are_leads_never_firm_though_corroborated(self):
        # every pair here agrees on the order date: not unique, so a lead all the same
        report, links = self.run_world(
            [portal_po(1, "PO7001", fm="PO7001"), portal_po(2, "PO7002", fm="po7001"),
             portal_po(3, "PO7003", fm="PO7003")],
            [bc_po(1, "PO106001"), bc_po(2, "PO106002"), bc_po(3, "PO106003")],
            [ref(1, "PO106001", "PO7001"),                             # two Portal orders
             ref(2, "PO106002", "PO7003"), ref(3, "PO106003", "PO7003")])  # two BC orders
        self.assertEqual(self.nonzero(report), {"ambiguous_fm": 1, "ambiguous_bc": 2})
        self.assertEqual(report["link_tiers"], {"corroborated": 0, "lead": 4})
        self.assertEqual(self.leads(links), {
            (po("api", 1), po("bc", 1), "ambiguous_fm"),
            (po("api", 2), po("bc", 1), "ambiguous_fm"),
            (po("api", 3), po("bc", 2), "ambiguous_bc"),
            (po("api", 3), po("bc", 3), "ambiguous_bc")})
        # a lead never carries the corroboration it does not rest on
        self.assertTrue(all({f["feature"] for f in json.loads(l["reason"])}
                            == {"your_reference", "bc_number"} for l in links))

    def test_a_unique_match_without_corroboration_is_a_lead(self):
        # no supplier on either side and the BC order date 30 days after the Portal's
        report, links = self.run_world(
            [portal_po(1, "PO7001", fm="PO7001"), portal_po(2, "PO7002", fm="PO7002")],
            [bc_po(1, "PO106001", order_date="2026-04-01"), bc_po(2, "PO106002", order_date=None)],
            [ref(1, "PO106001", "PO7001"), ref(2, "PO106002", "PO7002")])
        self.assertEqual(self.nonzero(report), {"uncorroborated": 2})
        self.assertEqual(self.leads(links), {(po("api", 1), po("bc", 1), "uncorroborated"),
                                             (po("api", 2), po("bc", 2), "uncorroborated")})
        # the window's edge: 7 days apart corroborates, 8 does not
        for day, tier in (("2026-03-09", "corroborated"), ("2026-03-10", "lead")):
            with self.subTest(day=day):
                _report, [link] = self.run_world(
                    [portal_po(1, "PO7001", fm="PO7001")],
                    [bc_po(1, "PO106001", order_date=day)], [ref(1, "PO106001", "PO7001")])
                self.assertEqual(link["tier"], tier)

    def test_the_supplier_corroborates_and_a_supplier_conflict_is_a_lead(self):
        suppliers = [supplier("s01", "Northwind Chemical Co."), supplier("s02", "Contoso Ltd")]
        report, links = self.run_world(
            [portal_po(1, "PO7001", fm="PO7001", supplier="s01"),
             portal_po(2, "PO7002", fm="PO7002", supplier="s02")],
            # b1: the supplier agrees, the date does not; b2: the date agrees, the
            # supplier differs
            [bc_po(1, "PO106001", order_date="2026-06-01", vendor_name="NORTHWIND CHEMICAL"),
             bc_po(2, "PO106002", vendor_name="Fabrikam")],
            [ref(1, "PO106001", "PO7001"), ref(2, "PO106002", "PO7002")], suppliers=suppliers)
        self.assertEqual(self.nonzero(report), {"linked": 1, "supplier_conflict": 1})
        self.assertEqual((report["counts"]["firm_by_supplier"],
                          report["counts"]["firm_by_order_date"]), (1, 0))
        firm = next(l for l in links if l["tier"] == "corroborated")
        self.assertEqual((firm["from_object"], firm["to_object"]), (po("api", 1), po("bc", 1)))
        reason = {f["feature"]: f for f in json.loads(firm["reason"])}
        self.assertEqual(set(reason), {"your_reference", "bc_number", "supplier_name"})
        self.assertEqual(reason["supplier_name"]["level"], "exact")
        self.assertIn("api://public.app_supplier/s01@X",
                      {c["l1_handle"] for c in reason["supplier_name"]["citations"]})
        self.assertEqual(self.leads([l for l in links if l["tier"] == "lead"]),
                         {(po("api", 2), po("bc", 2), "supplier_conflict")})
        # the lead names no supplier record it does not rest on
        self.assertNotIn("app_supplier", next(l for l in links if l["tier"] == "lead")["reason"])

    def test_a_number_held_elsewhere_than_fm_po_number_is_counted_not_linked(self):
        report, links = self.run_world(
            [portal_po(1, "PO7001")], [bc_po(1, "PO106001"), bc_po(2, "PO106002")],
            [ref(1, "PO106001", "PO7001"), ref(2, "PO106002", "PO7002")], fm=[fm_po(2, "PO7002")])
        self.assertEqual(links, [])
        self.assertEqual(self.nonzero(report), {"po_number_only": 1, "fm_only": 1})

    def test_the_hash_form_the_portal_writes_links_though_the_store_holds_the_normal_form(self):
        # Live shape: fmPoNumber and Your Reference both read PO#7001; the store keeps PO7001.
        report, [link] = self.run_world(
            [portal_po(1, "PO#7001", fm="PO#7001"), portal_po(2, "PO#7002", fm="PO#7002")],
            [bc_po(1, "PO106001"), bc_po(2, "PO106002")],
            [ref(1, "PO106001", "PO#7001"), ref(2, "PO106002", "7002")])
        self.assertEqual(self.nonzero(report), {"linked": 1, "no_match": 1})  # bare digits: not PO7002
        self.assertEqual(link["to_object"], po("bc", 1))

    def test_an_fm_clash_series_number_in_po_number_links_but_a_plain_one_never_does(self):
        # Live shape: a migrated Portal order holds the FM number in po_number (no fmPoNumber)
        # and BC Your Reference writes it with a dash before the FM id.
        report, [link] = self.run_world(
            [portal_po(1, "PO2-FM-106"), portal_po(2, "PO7002")],
            [bc_po(1, "PO106001"), bc_po(2, "PO106002")],
            [ref(1, "PO106001", " po2-fm-106"), ref(2, "PO106002", "PO7002")])
        self.assertEqual(self.nonzero(report), {"linked": 1, "po_number_only": 1})
        self.assertEqual((link["from_object"], link["to_object"]), (po("api", 1), po("bc", 1)))
        yr = {f["feature"]: f for f in json.loads(link["reason"])}["your_reference"]
        self.assertEqual(yr["citations"][1]["field_path"], "row.po_number")
        # two Portal orders holding it: a lead to each, never firm
        report, links = self.run_world(
            [portal_po(1, "PO2-FM-106"), portal_po(2, "po2-fm-106")],
            [bc_po(1, "PO106001")], [ref(1, "PO106001", "PO2-FM-106")])
        self.assertEqual(self.nonzero(report), {"ambiguous_fm": 1})
        self.assertEqual(self.leads(links), {(po("api", 1), po("bc", 1), "ambiguous_fm"),
                                             (po("api", 2), po("bc", 1), "ambiguous_fm")})

    def test_a_portal_minted_order_never_qualifies(self):
        report, links = self.run_world(
            [portal_po(1, "PO7001", fm="PO7001", source="portal-generate-po")],
            [bc_po(1, "PO106001")], [ref(1, "PO106001", "PO7001")])
        self.assertEqual(links, [])
        self.assertEqual(self.nonzero(report), {"lineage_not_authority": 1})

    def test_a_pair_already_joined_elsewhere_is_a_conflict_and_a_rejected_pair_stays_out(self):
        # The Portal order's number text names a different BC order: a lead to another
        # partner, so the reference is a conflict, never a second answer.
        report, links = self.run_world(
            [portal_po(1, "PO106002", fm="PO7001")], [bc_po(1, "PO106001"), bc_po(2, "PO106002")],
            [ref(1, "PO106001", "PO7001")])
        self.assertEqual(self.leads(links), {(po("api", 1), po("bc", 1), "conflict")})
        self.assertEqual(report["counts"]["conflict"], 1)
        # A person's rejection (a contradicted link, as the reviews step writes it) wins.
        def reject(builder, projector):
            return {"link_tiers": {"contradicted": builder.add("links", [{
                "link_id": "rvw-x", "link_type": LINK, "from_object": po("bc", 1),
                "to_object": po("api", 1), "tier": "contradicted", "binding_kind": "reviewed",
                "link_contract": None, "why_not": "[]",
                "reason": json.dumps([{"feature": "person_reviewed", "agrees": False}]),
                "evidence_handles": json.dumps(
                    [f"bc://production/{COMPANY}/purchaseOrders/{bid(1)}@{GEN}-2"]),
                "reviewed_by": "staff:purchasing",
                "reviewed_at": T, "review_action_id": "rev-1", "markings": "[]",
                "recorded_at": T, "superseded_at": None}])}}
        report, links = self.run_world(
            [portal_po(1, "PO7001", fm="PO7001")], [bc_po(1, "PO106001")],
            [ref(1, "PO106001", "PO7001")],
            steps={"reviews": reject, "your_reference": ids.your_reference_step})
        self.assertEqual(links, [])
        self.assertEqual(self.nonzero(report), {"rejected": 1})

    def test_a_rerun_rewrites_once_and_a_bc_source_without_the_page_reads_none(self):
        self.run_world([portal_po(1, "PO7001", fm="PO7001")], [bc_po(1, "PO106001")],
                       [ref(1, "PO106001", "PO7001")])
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        from types import SimpleNamespace
        from ontology.store import StoreBuilder
        builder = SimpleNamespace(connection=conn)
        again = ids.link_po_your_references(
            conn, SCHEMA, self.bc.field_page_records(ids.YOUR_REFERENCE_SET),
            lambda rows: StoreBuilder._add_links(builder, rows))
        self.assertEqual(again["counts"]["linked"], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM links WHERE link_id LIKE 'yrf-po-%'")
                         .fetchone()[0], 1)
        # a source with no field page: nothing linked, every order counted, and said so
        projector = SimpleNamespace(sources={"bc": object()}, schema=SCHEMA, cut=None,
                                    normalizer=None)
        conn.execute("DELETE FROM links WHERE link_id LIKE 'yrf-po-%'")
        report = ids.your_reference_step(SimpleNamespace(
            connection=conn, add=lambda kind, rows: len(rows)), projector)
        self.assertEqual(report["reference_capture"], "source_not_captured")
        self.assertEqual(self.nonzero(report), {"no_reference_record": 1})
        # a schema without the PO bindings: nothing linked, earlier links cleared, typed
        self.run_world([portal_po(1, "PO7001", fm="PO7001")], [bc_po(1, "PO106001")],
                       [ref(1, "PO106001", "PO7001")])
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        bare = ids.link_po_your_references(
            conn, {"object_types": {}}, self.bc.field_page_records(ids.YOUR_REFERENCE_SET),
            lambda rows: self.fail("a schema without the bindings wrote a link"))
        self.assertEqual(bare["skipped"], "schema_lacks_binding")
        self.assertEqual(bare["link_tiers"], {"corroborated": 0, "lead": 0})
        self.assertEqual(bare["counts"]["linked"], 0)
        self.assertEqual(bare["counts"]["reference_without_order"], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM links WHERE link_id LIKE 'yrf-po-%'")
                         .fetchone()[0], 0)

    def test_the_build_steps_run_your_reference_after_twins_and_reviews(self):
        steps = build_steps.post_project(self.dir / "state", "plx")
        self.assertIs(steps["your_reference"], ids.your_reference_step)
        order = sorted(steps)
        self.assertLess(order.index("twins"), order.index("your_reference"))
        self.assertLess(order.index("reviews"), order.index("your_reference"))

    def test_cardinality_one_ten_sixty(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                started = time.monotonic()
                report, links = self.run_world(
                    [portal_po(i, f"PO{7000 + i}", fm=f"PO{7000 + i}") for i in range(n)],
                    [bc_po(i, f"PO{106000 + i}") for i in range(n)],
                    [ref(i, f"PO{106000 + i}", f"PO{7000 + i}") for i in range(n)])
                self.assertEqual(self.nonzero(report), {"linked": n})
                self.assertEqual(len(links), n)
                self.assertEqual({(l["from_object"], l["to_object"]) for l in links},
                                 {(po("api", i), po("bc", i)) for i in range(n)})
                self.assertLess(time.monotonic() - started, 60)

    # ---- readers
    def test_a_reader_who_may_not_read_bc_never_sees_the_link(self):
        self.run_world([portal_po(1, "PO7001", fm="PO7001")], [bc_po(1, "PO106001")],
                       [ref(1, "PO106001", "PO7001")])
        base = tools(self, self.path)
        traverse = tt.TraverseTools(base)
        process = op.ProcessTools(base)
        aggregate = oa.AggregateTools(base, traverse)
        path = [{"link_type": LINK, "direction": "forward"}]
        for actor, sees in ((FULL, True), (PORTAL_ONLY, False)):
            with self.subTest(actor=sorted(actor.principals)):
                around = traverse.call("plx.around", {"from": po("api", 1), "path": path,
                                                      "tier_floor": "corroborated"}, actor)
                self.assertEqual({o["object_id"] for o in around.get("objects", ())},
                                 {po("bc", 1)} if sees else set())
                outputs = [around,
                           base.call("plx.object", {"object_id": po("api", 1)}, actor),
                           process.call("plx.process", {"from": po("api", 1)}, actor),
                           traverse.call("plx.timeline", {"from": po("api", 1)}, actor),
                           aggregate.call("plx.aggregate", {
                               "set": base.sets.new(actor, [po("api", 1)]),
                               "measure": {"op": "count"}}, actor)]
                text = json.dumps(outputs, default=str)
                # the tools show only citations of versions the reader may read
                self.assertEqual('"your_reference"' in text, sees)
                self.assertEqual("yrf-po-" in text, sees)
                if not sees:
                    self.assertNotIn(bid(1), text)

    def test_a_reader_who_may_not_read_the_supplier_sees_a_supplier_firm_link_as_a_lead(self):
        self.run_world([portal_po(1, "PO7001", fm="PO7001", supplier="s01")],
                       [bc_po(1, "PO106001", order_date=None, vendor_name="Northwind")],
                       [ref(1, "PO106001", "PO7001")],
                       suppliers=[supplier("s01", "Northwind")])
        buyer = Actor("plx", frozenset({"staff:buyer"}), "restricted")
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:public.app_purchase_order": {"staff:purchasing", "staff:buyer"},
            "api:public.app_supplier": {"staff:purchasing"},
            "bc:*": {"staff:purchasing", "staff:buyer"}},
            markings={m: {"staff:purchasing", "staff:buyer"} for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        base = ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(SCHEMA),
                                grammar=identifiers, contracts=CONTRACTS, clock=lambda: NOW)
        traverse = tt.TraverseTools(base)
        path = [{"link_type": LINK, "direction": "forward"}]
        for actor, tier in ((FULL, "corroborated"), (buyer, "lead")):
            with self.subTest(actor=sorted(actor.principals)):
                firm = traverse.call("plx.around", {"from": po("api", 1), "path": path,
                                                    "tier_floor": "corroborated"}, actor)
                self.assertEqual({o["object_id"] for o in firm.get("objects", ())},
                                 {po("bc", 1)} if tier == "corroborated" else set())
                around = traverse.call("plx.around", {"from": po("api", 1), "path": path,
                                                      "tier_floor": "lead"}, actor)
                self.assertEqual({o["object_id"] for o in around.get("objects", ())},
                                 {po("bc", 1)})
                [edge] = around["edges"]
                self.assertEqual(edge["tier"], tier)
                self.assertEqual("supplier_name" in {f["feature"] for f in edge["reason"]},
                                 tier == "corroborated")
                self.assertEqual("corroboration_not_visible" in
                                 {f["feature"] for f in edge["why_not"]}, tier == "lead")


class RuleParityTests(unittest.TestCase):
    """The product's rule and the flagship oracle's copy (two gates forbid one importing
    the other) decide every input combination alike: tier, code and corroborating fields."""

    def test_the_two_copies_agree_on_every_input(self):
        self.assertEqual(ids.YRF_DATE_WINDOW_DAYS, po_reference.DATE_WINDOW_DAYS)
        self.assertEqual((ids.YRF_FIRM, ids.YRF_LEAD, ids.YRF_SUPPLIER, ids.YRF_ORDER_DATE),
                         (po_reference.FIRM, po_reference.LEAD, po_reference.SUPPLIER,
                          po_reference.ORDER_DATE))
        odd = (None, "1", 1.0, True, False, -1)
        grid = {"portal_matches": (0, 1, 2, *odd), "bc_claimants": (0, 1, 2, *odd),
                "authority": (True, False, None, "yes", 1),
                "other_partner": (False, True, None, 0, ""),
                "supplier": (True, False, None, "yes", 1, 0),
                "order_days_apart": (0, 7, 8, *odd, 0.5)}
        names = list(grid)
        seen = Counter()
        for values in itertools.product(*(grid[n] for n in names)):
            args = dict(zip(names, values))
            product = ids.your_reference_verdict(**args)
            oracle = po_reference.decide(**args)
            self.assertEqual(product, (oracle.tier, oracle.code, oracle.corroborated_by), args)
            seen[product[1]] += 1
        # every code is reached, so the grid exercises every branch of both copies
        self.assertEqual(set(seen), {"no_match", "lineage_not_authority", "ambiguous_fm",
                                     "ambiguous_bc", "conflict", "supplier_conflict",
                                     "uncorroborated", "firm"})


class CapturedPageTests(unittest.TestCase):
    """The step over a real capture store: ``BcCapture`` fills ``purchaseOrderReferences``
    from the in-process ``FakeBc`` Page 50 and ``BcCaptures.field_page_records`` serves it."""

    def test_a_captured_page_links_through_the_real_bc_capture_reader(self):
        from integrations.tests.test_bc_po_references import harness
        from integrations.tests.test_bc_capture import CO, FakeBc
        from ontology.project_bc import BcCaptures
        from ontology.project import ProjectionError
        fake = FakeBc({"purchaseOrders": 3})
        orders = fake.data[(CO[1], "purchaseOrders")]
        for n, order in enumerate(orders):
            order["number"] = f"PO10600{n}"
            order["orderDate"] = "2026-03-02"   # the Portal order's date: corroborates
        fake.references[CO[1]] = [
            {"@odata.etag": 'W/"R"', "No": orders[0]["number"], "Your_Reference": "PO7001",
             "Buy_from_Vendor_No": ""},
            {"@odata.etag": 'W/"R"', "No": orders[1]["number"], "Your_Reference": "",
             "Buy_from_Vendor_No": ""}]
        h = harness(self, fake)
        h.run(entity_sets=("purchaseOrders",))
        h.store.close()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.sqlite"
        with BcCaptures(h.db) as bc:
            self.assertEqual(len(bc.field_page_records(ids.YOUR_REFERENCE_SET)), 2)
            self.assertEqual(bc.field_page_records(ids.YOUR_REFERENCE_SET,
                                                   "2000-01-01T00:00:00+00:00"), [])
            with self.assertRaises(ProjectionError):
                bc.field_page_records("purchaseOrders")
            report = build_store(path, SCHEMA, {
                "bc": bc, "portal_api": Source("portal_api", {"public.app_purchase_order": [
                    portal_po(1, "PO7001", fm="PO7001")]})},
                normalizer=identifiers.normalize, clock=lambda: NOW,
                post_project={"your_reference": ids.your_reference_step}
            )["post_project"]["your_reference"]
        counts = {k: v for k, v in report["counts"].items() if v}
        self.assertEqual(counts, {"bc_orders": 3, "references": 2, "linked": 1, "missing": 1,
                                  "no_reference_record": 1, "firm_by_order_date": 1})
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        [(frm, to, reason)] = conn.execute(
            "SELECT from_object, to_object, reason FROM links WHERE link_id LIKE 'yrf-po-%'")
        self.assertEqual((frm, to), (po("api", 1),
                                     f'PurchaseOrder:bc.purchase_orders:{{"id":"{orders[0]["id"]}"}}'))
        [cite] = [c for f in json.loads(reason) if f["feature"] == "your_reference"
                  for c in f["citations"] if "purchaseOrderReferences" in c["l1_handle"]]
        self.assertEqual(cite["field_path"], "row.Your_Reference")


if __name__ == "__main__":
    unittest.main()
