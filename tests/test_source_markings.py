"""Value markings on source reads: a reader without a price or finance marking reads no
marked value through any source path (plx.read of a Portal mirror row, a captured or live
BC record, its column pages and field windows, and the page source view /api/source), and
no unmarked column repeating a marked figure; a reader holding the marking reads them.

The markings are the committed schema's (``ontology.raw_markings``); every row, id and
amount here is synthetic. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import json
import unittest

from integrations.capture_store import BC_KIND, make_handle
from ontology import identifiers
from ontology import raw_markings as rm
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_evidence as ev
from tests.test_ontology_tools_evidence import (CO, CONTRACTS, NOW, SCHEMA, _Live, _Mirror,
                                                _Runtime, build, ok)

QUALITY = Actor("plx", frozenset({"qa"}))
BUYER = Actor("plx", frozenset({"buy"}))
FINANCE = Actor("plx", frozenset({"fin"}))
POLICY = ot.ReadPolicy(tenant="plx", audiences={"*": {"qa", "buy", "fin"}},
                       markings={"finance": {"fin"}, "purchasing_prices": {"buy"}})
EVERY = ("qa", "buy", "fin")


def api(relation: str, key: dict) -> str:
    from urllib.parse import quote
    return f"api://{relation}/{quote(json.dumps(key, separators=(',', ':')))}@0000000000000001-1"


def bc_capture(entity_set: str, record_id: str) -> str:
    return make_handle(BC_KIND, ("production", CO, entity_set, record_id), "00000000000000a1", 2)


# (relation, key columns, row, {secret figure: the marking that opens it}) per kind and
# system. Each secret appears in a marked column; some also in an unmarked twin.
PORTAL = {
    "purchase order": ("public.app_purchase_order", {"id": "po-t1"}, {
        "id": "po-t1", "po_number": "PO-T1", "status": "SUBMITTED", "currency": "CAD",
        "total_cost": 48213.77, "subtotal": 48213.77, "discount": 0,
        "shipping_cost": 311.4, "duties_cost": 92.15, "tax_amount": 0,
        "custom_fields": {"fmPoNumber": "PO#T1", "legacyTotal": "48213.77"},
        "notes": "rush"}, {"48213.77": "purchasing_prices", "311.4": "finance",
                           "92.15": "finance"}),
    "sales order": ("public.app_sales_order", {"id": "so-t1"}, {
        "id": "so-t1", "order_number": "SO-T1", "status": "OPEN",
        "subtotal": "7311.25", "total_amount": "7311.25", "tax_amount": "0",
        "grand_total_v2": "7311.25"}, {"7311.25": "finance"}),
}
SAGE = {
    "purchase order": ("sage.po_header", {"porhseq": "77"}, {
        "porhseq": "77", "ponumber": "PO-T2", "doctotal": "5120.000",
        "extended": "5120.000", "descriptio": "resin", "values": "5120.000"},
        {"5120": "purchasing_prices"}),
    "sales order": ("sage.oe_order_header", {"orduniq": "55"}, {
        "orduniq": "55", "ordnumber": "SO-T2", "ordtotal": "880.500"}, {"880.5": "finance"}),
    "invoice": ("sage.oe_invoice_header", {"invuniq": "9001"}, {
        "invuniq": "9001", "invnumber": "IN-T1", "invnetnotx": "1500.000",
        "invnetwtx": "1695.000", "invitmtot": "1452.250"},
        {"1500": "finance", "1695": "finance", "1452.25": "finance"}),
    # Money no name rule reads: Sage keeps amounts as text under abbreviations, and a
    # whole amount as whole text ("2225"): its column's other figures make it money.
    "shipment": ("sage.oe_shipment_header", {"shiuniq": "4401"}, {
        "shiuniq": "4401", "shinumber": "SH-T1", "shidate": "20240105",
        "shitotal": "2225", "shinetnotx": "2225", "shiweight": "125.0000"},
        {"2225": "finance"}),
    "order line": ("sage.oe_order_detail", {"_sage_pk": "55-32"}, {
        "_sage_pk": "55-32", "orduniq": "55", "linenum": "32", "item": "RM-T1",
        "qtyordered": "100.0000",
        "cosuntcst": "12.340", "pribasprc": "15.750", "priuntprc": "15.750"},
        {"12.34": "finance", "15.75": "finance"}),
    "GL balances": ("sage.gl_afs", {"_sage_pk": "4000-2024"}, {
        "_sage_pk": "4000-2024", "acctid": "4000", "fscsyr": "2024", "openbal": "-35000",
        "netperd1": "120.500", "audtdate": "20240105"},
        {"-35000": "finance", "120.5": "finance"}),
}
BC = {
    "purchase order": ("purchaseOrders", {
        "id": "a1b2c3d4-0000-4000-8000-000000000001", "number": "PO-T3",
        "vendorName": "Vendor T", "status": "Open", "totalAmountExcludingTax": 9120.5,
        "totalAmountIncludingTax": 9120.5, "totalTaxAmount": 0, "discountAmount": 14.25},
        {"9120.5": "purchasing_prices", "14.25": "purchasing_prices"}),
    "sales order": ("salesOrders", {
        "id": "a1b2c3d4-0000-4000-8000-000000000002", "number": "SO-T3",
        "customerName": "Customer T", "totalAmountExcludingTax": 655.0,
        "totalAmountIncludingTax": 740.15, "totalTaxAmount": 85.15}, {
        "655": "finance", "740.15": "finance", "85.15": "finance"}),
    "invoice": ("salesInvoices", {
        "id": "a1b2c3d4-0000-4000-8000-000000000003", "number": "IN-T3",
        "customerName": "Customer T", "totalAmountExcludingTax": 1200.0,
        "totalAmountIncludingTax": 1356.0, "totalTaxAmount": 156.0,
        "remainingAmount": 1356.0}, {"1200": "finance", "1356": "finance", "156": "finance"}),
}


def tools(runtime) -> ev.EvidenceTools:
    base = ot.OntologyTools(ot.StoreReader(build().conn), POLICY, item_gate=lambda a, h: True,
                            schema=SCHEMA, grammar=identifiers, contracts=CONTRACTS,
                            clock=lambda: NOW)
    return ev.EvidenceTools(base, reader=ev.RuntimeCitations(
        runtime, bc_books={CO.upper(): "manufacturing"}))


def figures(out) -> set:
    """Every non-zero figure a result shows, wherever it is (a typed number, a numeric
    text, a number inside JSON text), by value (``5120.000`` is 5120)."""
    return rm.numbers(out)


def secret_set(secrets, keep=lambda marking: True) -> set:
    from decimal import Decimal
    return {Decimal(s) for s, marking in secrets.items() if keep(marking)}


def leaks(out, secrets, actor) -> set:
    held = POLICY.held_markings(actor)
    return figures(out) & secret_set(secrets, lambda marking: marking not in held)


def visible(out, secrets) -> set:
    return figures(out) & secret_set(secrets)


def mirror_runtime(relation, key, row, lineage):
    return _Runtime(mirror=_Mirror({"ok": True, "relation": relation, "key": key,
                                    "capture": {"capture_id": 1}, "row": row,
                                    "lineage": lineage, "changed_since": False}),
                    live=live_audience())


class CapturedBc(_Runtime):
    def __init__(self, row, **kwargs):
        super().__init__(**kwargs)
        self.row = row

    def resolve_citation(self, handle, actor):
        return {"citation": handle, "source": "bc", "kind": "bc", "key": {"id": self.row["id"]},
                "row": self.row, "lineage": "bc_manufacturing", "source_version": 'W/"JzQ1Oyc="',
                "digest": "e" * 64, "recorded_at": "2099-01-01T00:00:00Z", "status": "current",
                "changed_since": False}


class LiveBc(_Runtime):
    """A live BC GET answered in process: the reader's record envelope
    (``BusinessCentralReader._record``) around the BC columns."""

    def __init__(self, entity_set, row, **kwargs):
        super().__init__(**kwargs)
        self.record = {"source": "business-central", "environment": "Production",
                       "company_id": CO, "entity_set": entity_set, "id": row["id"],
                       "etag": 'W/"JzQ0Oyc="', "last_modified": None, "fields": row,
                       "content_sha256": "f" * 64}

    def read_live(self, handle, actor):
        return {"ok": True, "record": dict(self.record, citation=handle)}

    def resolve_citation(self, handle, actor):
        return {"citation": handle, "record": self.record, "status": "current"}


def live_audience():
    from integrations.live_source_guard import LiveSourceAudience
    return _Live(LiveSourceAudience({f"bc:production:{CO}:*": list(EVERY)}, tenant="plx"))


def cases():
    """(name, handle, runtime factory, secrets) of every source path and record kind."""
    from integrations import bc_read
    out = []
    for system, table, lineage in (("Portal", PORTAL, "gold_etl_from_fm"),
                                   ("Sage", SAGE, "sage_bronze")):
        for kind, (relation, key, row, secrets) in table.items():
            out.append((f"{system} {kind}", api(relation, key),
                        lambda r=relation, k=key, w=row, g=lineage: mirror_runtime(r, k, w, g),
                        secrets))
    for kind, (entity_set, row, secrets) in BC.items():
        out.append((f"BC captured {kind}", bc_capture(entity_set, row["id"]),
                    lambda w=row: CapturedBc(w, live=live_audience()), secrets))
        out.append((f"BC live {kind}",
                    bc_read.citation("Production", CO, entity_set, row["id"], 'W/"JzQ0Oyc="'),
                    lambda e=entity_set, w=row: LiveBc(e, w, live=live_audience()), secrets))
    return out


class RelationMarkingTests(unittest.TestCase):
    """The committed schema's markings of raw columns, shadowed generated types and name
    rules included, and the fail-closed default."""

    def test_columns_no_curated_property_reads_keep_the_generated_marking(self):
        po = rm.relation_markings(rm.PORTAL, "public.app_purchase_order")
        self.assertEqual(po.of("subtotal"), {"purchasing_prices"})   # shadowed generated
        self.assertEqual(po.of("total_cost"), {"purchasing_prices"})
        self.assertEqual(po.of("shipping_cost"), {"finance"})
        self.assertEqual(po.of("po_number"), frozenset())
        self.assertEqual(rm.relation_markings(rm.PORTAL, "sage.po_header").of("extended"),
                         {"purchasing_prices"})

    def test_bc_columns_beside_a_marked_total_are_marked(self):
        po = rm.relation_markings(rm.BC, "purchaseOrders")
        self.assertEqual(po.of("totalAmountIncludingTax"), {"purchasing_prices"})
        self.assertEqual(po.of("totalAmountExcludingTax"), {"purchasing_prices"})
        self.assertEqual(po.of("totalTaxAmount"), {"finance"})
        self.assertEqual(po.of("number"), frozenset())
        self.assertEqual(po.of("pricesIncludeTax"), frozenset())   # a boolean, not money

    def test_a_column_of_unknown_status_on_a_money_relation_is_finance(self):
        for source, relation in ((rm.PORTAL, "public.app_purchase_order"),
                                 (rm.BC, "purchaseOrders")):
            self.assertEqual(rm.column_markings(source, relation, "addedUpstreamLater"),
                             {"finance"})
        # A money name is marked wherever it appears.
        self.assertEqual(rm.column_markings(rm.PORTAL, "public.no_such_relation",
                                            "freight_cost"), {"finance"})
        self.assertEqual(rm.column_markings(rm.PORTAL, "public.no_such_relation", "note"),
                         frozenset())

    def test_twins_are_withheld_with_the_figure_they_repeat(self):
        row = {"id": "1234.5", "total_cost": "1234.50", "po_number": "P1",
               "custom_fields": '{"fm": {"total": 1234.5}}', "lead_time": 0,
               "line_count": 3, "discount": 0}
        hidden = rm.withheld_columns(rm.PORTAL, "public.app_purchase_order", row, ())
        self.assertEqual(hidden, {"total_cost", "custom_fields", "discount"})
        # The key is never a twin; a zero is never a figure; a holder sees everything.
        self.assertEqual(rm.withheld_columns(rm.PORTAL, "public.app_purchase_order", row,
                                             {"purchasing_prices"}), set())

    def test_a_number_past_the_decimal_range_is_withheld_not_an_error(self):
        row = {"id": "p", "total_cost": "1E+999999999", "notes": "1e999999999",
               "po_number": "P1"}
        self.assertEqual(rm.withheld_columns(rm.PORTAL, "public.app_purchase_order", row, ()),
                         {"total_cost", "notes"})

    def test_money_is_decided_by_what_a_column_holds_on_finance_relations(self):
        """No name rule reads Sage's shipment total, unit cost or GL balance: a column of a
        purchasing, sales or GL relation whose figures are amounts (the relation's value
        profile) is finance on every row, a whole "2225" or "-35000" too; a quantity, a
        weight, an identifier, a compact date stays visible."""
        for relation, row, hidden in (
                ("sage.oe_shipment_header", SAGE["shipment"][2], {"shitotal", "shinetnotx"}),
                ("sage.oe_order_detail", SAGE["order line"][2],
                 {"cosuntcst", "pribasprc", "priuntprc"}),
                ("sage.gl_afs", SAGE["GL balances"][2], {"openbal", "netperd1"})):
            with self.subTest(relation):
                self.assertTrue(rm.relation_markings(rm.PORTAL, relation).sensitive)
                self.assertEqual(rm.withheld_columns(rm.PORTAL, relation, row, ()), hidden)
                self.assertEqual(rm.withheld_columns(rm.PORTAL, relation, row, {"finance"}),
                                 set())
        # A manufacturing relation is not finance-sensitive: its decimals stay visible.
        self.assertFalse(rm.relation_markings(rm.PORTAL,
                                              "public.app_batch_record").sensitive)

    def test_filemaker_whole_figures_written_n_0_stay_visible(self):
        """FileMaker writes flags, identifiers and counts as "1.0": a column whose figures
        are whole is no money, on every row; Sage's normal-balance flag too."""
        for relation, row, hidden in (
                ("public.receipt", {"id": "r1", "acceptdamages": "1.0", "acceptodors": "0.0",
                                    "amount": "125.50"}, {"amount"}),
                ("public.order_entry", {"id": "o1", "oecust_c": "1234.0"}, set()),
                ("public.customer", {"id": "c1", "certvegan": "1.0"}, set()),
                ("sage.gl_account", {"acctid": "4000", "acctbal": "1"}, set())):
            with self.subTest(relation):
                self.assertEqual(rm.withheld_columns(rm.PORTAL, relation, row, ()), hidden)

    def test_the_profile_reads_a_column_by_its_figures_across_the_relation(self):
        from ontology.money_profile import classify
        rows = [{"total": "2210.400", "flag": "1.0", "invoice": str(1000 + n), "note": "x",
                 "empty": None} for n in range(300)]
        rows[0].update(total="2225", invoice="1012.5")   # one stray fraction in 300 ids
        self.assertEqual(classify(rows), {"total": "money", "flag": "whole",
                                          "invoice": "whole", "note": "text",
                                          "empty": "empty"})

    def test_container_references_are_codes_not_money(self):
        """FileMaker container data (``receipt_raw_material.coadata``) holds encoded
        references such as -9.519e18, never an amount: a code, visible; its cost is not."""
        from ontology.money_profile import classify
        rows = [{"coadata": f"-95190191{n:04d}.5e10", "costitem": "12.5"} for n in range(50)]
        self.assertEqual(classify(rows), {"coadata": "code", "costitem": "money"})
        self.assertEqual(rm.withheld_columns(rm.PORTAL, "public.receipt_raw_material",
                                             {"id": "r1", "coadata": "-9.51901913080919e+18",
                                              "costitem": "12.5"}, ()), {"costitem"})

    def test_a_sage_optional_field_is_withheld_where_its_row_declares_decimals(self):
        row = {"_sage_pk": "C1-SHPMINDOL", "idcust": "C1", "optfield": "SHPMINDOL",
               "type": "100", "decimals": "3", "value": "50.000"}
        self.assertEqual(rm.withheld_columns(rm.PORTAL, "sage.ar_cuso", row, ()), {"value"})
        self.assertEqual(rm.withheld_columns(rm.PORTAL, "sage.ar_cuso", row, {"finance"}),
                         set())
        count = {**row, "optfield": "SHPMINCSE", "type": "6", "decimals": "0", "value": "6"}
        self.assertEqual(rm.withheld_columns(rm.PORTAL, "sage.ar_cuso", count, ()), set())

    def test_a_whole_column_serving_a_fraction_is_withheld_and_the_profile_stale(self):
        rm.STALE.discard(("public.receipt", "acceptdamages"))
        flag = {"id": "r1", "acceptdamages": "1.0"}
        self.assertEqual(rm.withheld_columns(rm.PORTAL, "public.receipt", flag, ()), set())
        self.assertNotIn(("public.receipt", "acceptdamages"), rm.stale())
        odd = {"id": "r1", "acceptdamages": "0.5"}
        self.assertEqual(rm.withheld_columns(rm.PORTAL, "public.receipt", odd, ()),
                         {"acceptdamages"})
        self.assertEqual(rm.withheld_columns(rm.PORTAL, "public.receipt", odd, {"finance"}),
                         set())
        self.assertIn(("public.receipt", "acceptdamages"), rm.stale())

    def test_a_state_profile_newer_than_the_committed_one_decides(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as state:
            relation = "public.order_entry"
            row = {"id": "o1", "oecust_c": "1234.0"}
            self.assertEqual(rm.withheld_columns(rm.PORTAL, relation, row, ()), set())
            Path(state, rm.STATE_PROFILE).write_text(json.dumps({
                "profiled_at": "2999-01-01T00:00:00Z",
                "relations": {relation: {"money": ["oecust_c"], "whole": ["id"]}}}))
            saved = list(rm._state_dirs)
            try:
                rm.use_state_dir(state)
                rm._profile_seen_at[0] = None   # re-read the profiles now
                self.assertEqual(rm.withheld_columns(rm.PORTAL, relation, row, ()),
                                 {"oecust_c"})
            finally:
                rm._state_dirs[:] = saved
                rm._profile_seen_at[0] = None

    def test_the_profile_check_fails_when_old_or_of_another_mirror(self):
        import datetime
        import sqlite3
        import tempfile
        from pathlib import Path
        from ontology import money_profile as mp
        with tempfile.TemporaryDirectory() as state:
            mirror = Path(state) / mp.MIRROR
            mirror.parent.mkdir(parents=True)
            db = sqlite3.connect(mirror)
            db.execute("CREATE TABLE mirror_meta (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO mirror_meta VALUES ('generation', 'g2')")
            db.commit()
            db.close()
            Path(state, rm.STATE_PROFILE).write_text(json.dumps({
                "mirror_generation": "g2", "profiled_at": "2026-11-01T00:00:00Z",
                "relations": {}}))
            now = datetime.datetime(2026, 11, 3, tzinfo=datetime.timezone.utc)
            self.assertTrue(mp.check(Path(state), now=now)["ok"])
            later = datetime.datetime(2026, 11, 20, tzinfo=datetime.timezone.utc)
            self.assertEqual(mp.check(Path(state), now=later)["problems"],
                             ["profile_too_old"])
            Path(state, rm.STATE_PROFILE).write_text(json.dumps({
                "mirror_generation": "g1", "profiled_at": "2026-11-01T00:00:00Z"}))
            self.assertEqual(mp.check(Path(state), now=now)["problems"],
                             ["mirror_generation_not_profiled"])

    def test_a_purchasing_reader_keeps_the_line_amount_a_finance_twin_repeats(self):
        line = {"id": "l1", "description": "resin", "quantity": 10, "directUnitCost": 91.205,
                "amountExcludingTax": 912.05, "amountIncludingTax": 912.05,
                "netAmount": 912.05, "netAmountIncludingTax": 912.05, "totalTaxAmount": 0}
        self.assertEqual(rm.withheld_columns(rm.BC, "purchaseOrderLines", line,
                                             {"purchasing_prices"}), {"totalTaxAmount"})
        self.assertEqual(rm.withheld_columns(rm.BC, "purchaseOrderLines", line, ()),
                         {"directUnitCost", "amountExcludingTax", "amountIncludingTax",
                          "netAmount", "netAmountIncludingTax", "totalTaxAmount"})

    def test_a_row_marking_withholds_the_row(self):
        self.assertIsNone(rm.withheld_columns(rm.PORTAL, "public.app_user", {"id": "u"}, ()))
        self.assertIsNone(rm.withheld_columns(rm.BC, "bankAccounts", {"id": "b"}, ()))


class SourceReadTests(unittest.TestCase):
    """plx.read of every source path for a PO, SO and invoice from the Portal, Sage and BC."""

    def read(self, handle, runtime, actor, **arguments):
        return ok(self, "plx.read", tools(runtime).call(
            "plx.read", {"citation": handle, **arguments}, actor))

    def test_no_reader_reads_a_figure_a_marking_withholds(self):
        for name, handle, runtime, secrets in cases():
            with self.subTest(name):
                quality = self.read(handle, runtime(), QUALITY)
                self.assertEqual(quality["status"], "resolved", quality)
                self.assertEqual(leaks(quality, secrets, QUALITY), set())
                self.assertIn("marking_withheld", [g["reason"] for g in quality["gaps"]])
                self.assertNotIn("f" * 64, json.dumps(quality))   # no digest of the columns
                buyer = self.read(handle, runtime(), BUYER)
                self.assertEqual(leaks(buyer, secrets, BUYER), set())
                self.assertEqual(visible(buyer, secrets), secret_set(
                    secrets, lambda marking: marking == "purchasing_prices"))
                finance = self.read(handle, runtime(), FINANCE)
                self.assertEqual(visible(finance, secrets), secret_set(secrets), finance)

    def test_unmarked_columns_stay_readable(self):
        _name, handle, runtime, _secrets = cases()[0]
        out = self.read(handle, runtime(), QUALITY)
        self.assertIn("PO-T1", json.dumps(out["fields"]["row.po_number"]))
        self.assertIn("row.status", out["fields"])
        self.assertNotIn("row.custom_fields", out["fields"])   # it repeats the PO total

    def test_one_marked_field_reads_as_withheld_never_absent(self):
        handle, runtime = cases()[0][1], cases()[0][2]
        out = self.read(handle, runtime(), QUALITY, field_path="row.total_cost")
        reasons = [g["reason"] for g in out["gaps"]]
        self.assertIn("marking_withheld", reasons)
        self.assertNotIn("no_value", reasons)
        self.assertEqual(leaks(out, PORTAL["purchase order"][3], QUALITY), set())

    def test_a_row_marking_withholds_the_whole_mirror_row(self):
        handle = api("public.app_user", {"id": "u-1"})
        runtime = mirror_runtime("public.app_user", {"id": "u-1"},
                                 {"id": "u-1", "email": "person@example.test"}, "gold_native_staging")
        out = self.read(handle, runtime, FINANCE)
        self.assertEqual(out["status"], "withheld")
        self.assertNotIn("example.test", json.dumps(out))

    def test_paged_reads_cut_from_the_marked_row(self):
        from operating.case_tools_host import PagedRead
        for name, handle, runtime, secrets in cases():
            with self.subTest(name):
                paged = PagedRead(tools(runtime()), runtime=None)
                for arguments in ({"column_offset": 0},
                                  {"field_path": "row.custom_fields", "text_offset": 0},
                                  {"find": "48213"}):
                    out = paged.call({"citation": handle, **arguments}, QUALITY)
                    self.assertEqual(leaks(out, secrets, QUALITY), set(), (arguments, out))


class SourceViewTests(unittest.TestCase):
    """The page source view (``/api/source``) of a Portal or Sage row: ``ObjectWorkspace``
    opens it through ``CaseWorkspace.citation`` with the reader's markings."""

    class Runtime:
        def __init__(self, relation, key, row):
            self.view = {"citation": "", "relation": relation, "key": key, "capture_id": 1,
                         "row": row}

        def citation_status(self, handle, actor):
            return {"ok": True, "status": "current"}

        def resolve_citation(self, handle, actor):
            return dict(self.view, citation=handle)

    def workspace(self, relation, key, row, grant):
        from operator_ui.case_workspace import CaseWorkspace
        from operator_ui.object_workspace import ObjectWorkspace

        class Access:
            read = None

            def request(self):
                import contextlib
                return contextlib.nullcontext()

            def decide(self, handles):
                return {h: None for h in handles}

            def withheld_sources(self):
                return []

        access = Access()
        if grant is not None:
            access.grant = lambda: (lambda key: True, POLICY.held_markings(grant))
        sources = CaseWorkspace(self.Runtime(relation, key, row), QUALITY)
        return ObjectWorkspace({n: {"handler": None} for n in
                                ("plx.find", "plx.object", "plx.around", "plx.timeline")},
                               QUALITY, sources=sources, access=access)

    def test_the_source_view_withholds_what_the_reader_lacks(self):
        for system, table in (("Portal", PORTAL), ("Sage", SAGE)):
            for kind, (relation, key, row, secrets) in table.items():
                with self.subTest(f"{system} {kind}"):
                    handle = api(relation, key)
                    for actor in (None, QUALITY, BUYER, FINANCE):
                        view = self.workspace(relation, key, row, actor).source(handle)
                        reader = actor or QUALITY   # no grant: no marking held
                        self.assertEqual(leaks(view, secrets, reader), set(), view)
                        if actor is FINANCE:
                            self.assertEqual(visible(view, secrets), secret_set(secrets))

    def test_a_row_the_reader_may_not_see_is_not_found(self):
        with self.assertRaises(KeyError):
            self.workspace("public.app_user", {"id": "u"}, {"id": "u", "email": "e@x.test"},
                           FINANCE).source(api("public.app_user", {"id": "u"}))



class AnswerGateTests(unittest.TestCase):
    """The answer gate reads a cited row as plx.read does: a value a marking withholds is
    neither filled into a claim left to the gate nor confirmed or refuted when stated."""

    HANDLE = api(*PORTAL["purchase order"][:2])

    def gate(self, held, claim):
        from types import SimpleNamespace
        from operating import answer_gate
        relation, key, row, _ = PORTAL["purchase order"]

        class Runtime:
            def claim_row(self, handle, actor):
                return {"citation": handle, "relation": relation, "key": key,
                        "capture_id": 1, "row": dict(row)}
        server = SimpleNamespace(case_runtime=Runtime(), actor=QUALITY,
                                 **({} if held is None else
                                    {"held_markings": lambda: frozenset(held)}))
        return answer_gate.gate_answer(
            {"claims": [{"id": "c1", "citation": self.HANDLE, "field": "row.total_cost",
                         **claim}]}, server, seen={self.HANDLE}, returned={},
            read_values=True, may_repair=False)

    def test_a_withheld_value_is_never_filled_in(self):
        for held in (None, ()):   # a server that cannot say what it holds: none
            out = self.gate(held, {})
            self.assertEqual(out["outcome"], "abstained")
            self.assertNotIn("48213", json.dumps(out, default=str))
        out = self.gate({"purchasing_prices"}, {})
        self.assertEqual(out["outcome"], "answered")
        self.assertIn("48213.77", out["text"])

    def test_a_stated_withheld_value_is_refused_alike_right_or_wrong(self):
        right, wrong = (json.dumps(self.gate((), {"value": value}), default=str,
                                   sort_keys=True).replace(value, "V")
                        for value in ("48213.77", "1234.56"))
        self.assertEqual(right, wrong)
        self.assertIn("withheld from this reader", right)
        self.assertNotIn("claim_value_mismatch", right)


if __name__ == "__main__":
    unittest.main()
