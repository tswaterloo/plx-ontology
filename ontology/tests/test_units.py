"""The one unit fold and the money tax basis (ontology/units.py).

The Portal table below is copied from plx-customer-portal
portal/src/lib/mrp/product-uom.ts@fa46f2d (PRODUCT_UOM_OPTIONS values lines 30-49,
ALIASES lines 70-98); ``normalizeProductUom`` (lines 105-110) trims, lower-cases, keeps a
canonical value, maps an alias and returns any other token unchanged. Receiving's
``unitsCompatible`` is transcribed from apply-po-line-receive.ts@6e76a5b (lines 41-52).
Evidence label: isolated-fixture; the BC local currency and tax basis cases project captures
read through the real GET-only ``BcCapture`` from an in-process fake (``injected-transport``,
live-unverified; live check ``ontology.projection_bc``).
"""

from __future__ import annotations

import copy
from datetime import timedelta
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

from integrations.bc_read import CAPTURE_ENTITY_SETS
from ontology import identifiers, project, store_ddl, units
from ontology import schema as schema_loader
from ontology.generator import bc as bc_generator
from ontology.project import build_store
from ontology.project_bc import BcCaptures
from ontology.store import ProjectionStore, ReadPolicy, StoreError
from ontology.tests import test_project_bc as tp
from ontology.tests import test_projection as fixture
from operating import claim_checks
from operating import ontology_tools as ot

ROOT = Path(__file__).resolve().parents[2]

PORTAL_VALUES = ["ea", "cse", "dz", "box", "bag", "bottle", "drum", "pail", "roll", "pack",
                 "kg", "g", "mg", "lb", "oz", "l", "ml", "gal"]
PORTAL_ALIASES = {
    "each": "ea", "eac": "ea", "unit": "ea", "units": "ea", "pc": "ea", "pcs": "ea",
    "ech": "ea", "case": "cse", "cases": "cse", "cs": "cse", "dozen": "dz", "doz": "dz",
    "boxes": "box", "packs": "pack", "btl": "bottle", "drm": "drum", "kgs": "kg",
    "kilogram": "kg", "kilograms": "kg", "gram": "g", "grams": "g", "lbs": "lb",
    "pound": "lb", "pounds": "lb", "litre": "l", "liter": "l", "lt": "l"}


def normalize_product_uom(raw):
    """The Portal function, transcribed line by line, as the oracle of the table test."""
    token = (raw or "").strip().lower()
    if not token:
        return None
    if token in PORTAL_VALUES:
        return token
    return PORTAL_ALIASES.get(token, token)


def units_compatible(a, b):
    """plx-customer-portal portal/src/lib/mrp/apply-po-line-receive.ts@6e76a5b
    ``unitsCompatible`` (lines 41-52), transcribed."""
    x, y = (a or "").strip().lower(), (b or "").strip().lower()
    if not x or not y:
        return False
    if x == y:
        return True
    sets = ({"bag", "bags"}, {"drum", "drums"}, {"unit", "units", "ea", "each", "pcs", "pc"})
    return any(x in s and y in s for s in sets)


class PortalFoldTests(unittest.TestCase):
    def test_tables_are_the_portal_tables(self):
        self.assertEqual(list(units.UOM_CANONICAL), PORTAL_VALUES)
        self.assertEqual(units.UOM_ALIASES, PORTAL_ALIASES)

    def test_every_portal_value_and_alias_round_trips_like_normalize_product_uom(self):
        for token in PORTAL_VALUES + sorted(PORTAL_ALIASES):
            for spelling in (token, token.upper(), f"  {token.title()}\t"):
                with self.subTest(spelling=spelling):
                    self.assertEqual(units.unit_token(spelling), normalize_product_uom(spelling))
                    self.assertIn(units.unit_token(spelling), PORTAL_VALUES)

    def test_an_unknown_token_is_returned_trimmed_and_lower_cased(self):
        for raw, want in ((" PALLET ", "pallet"), ("GR", "gr"), ("Pails", "pails"),
                          ("rolls", "rolls"), ("4PK", "4pk"), ("Pair", "pair")):
            with self.subTest(raw=raw):
                self.assertEqual(units.unit_token(raw), want)
                self.assertEqual(units.unit_token(raw), normalize_product_uom(raw))
        self.assertNotEqual(units.unit_token("GR"), "g")  # a BC item code is not folded back
        for empty in ("", "   ", None, 3, ["kg"]):
            self.assertIsNone(units.unit_token(empty))

    def test_the_console_carries_the_same_tables(self):
        js = (ROOT / "operator_ui/workspace.js").read_text("utf-8")
        canonical = re.search(r"const UOM_CANONICAL = (\[.*?\]);", js, re.S)
        aliases = re.search(r"const UOM_ALIASES = (\{.*?\});", js, re.S)
        self.assertIsNotNone(canonical)
        self.assertIsNotNone(aliases)
        self.assertEqual(json.loads(canonical.group(1)), list(units.UOM_CANONICAL))
        self.assertEqual(json.loads(aliases.group(1)), units.UOM_ALIASES)
        receive = re.search(r"const RECEIVE_ALIASES = (\{.*?\});", js, re.S)
        self.assertIsNotNone(receive)
        self.assertEqual(json.loads(receive.group(1)), units.RECEIVE_ALIASES)

    def test_two_spellings_are_one_unit_exactly_when_a_portal_fold_says_so(self):
        # One unit iff linked by normalizeProductUom equality or the receiving
        # unitsCompatible relation (closed transitively); every other pair stays apart.
        universe = sorted(set(PORTAL_VALUES) | set(PORTAL_ALIASES) |
                          {"bags", "drums", "pails", "rolls", "bottles", "packss", "gr",
                           "pallet", "kit", "pair"})
        parent = {t: t for t in universe}

        def root(t):
            while parent[t] != t:
                t = parent[t]
            return t
        for a in universe:
            for b in universe:
                if normalize_product_uom(a) == normalize_product_uom(b) or \
                        units_compatible(a, b):
                    parent[root(a)] = root(b)
        for a in universe:
            for b in universe:
                with self.subTest(a=a, b=b):
                    self.assertEqual(units.unit_token(a.upper()) == units.unit_token(b),
                                     root(a) == root(b))
        self.assertEqual(units.unit_token(" Bags "), "bag")
        self.assertEqual(units.unit_token("DRUMS"), "drum")

    def test_the_trim_is_javascripts(self):
        # String.prototype.trim strips a BOM and Unicode space separators, but not the
        # C0 separators U+001C-U+001F or U+0085 that Python's str.strip removes.
        for raw in ("\ufeffkg", "kg\u3000", "\u2028kg\u00a0", "\u205fkg\u202f"):
            self.assertEqual(units.unit_token(raw), "kg", repr(raw))
        for raw in ("\x1ckg", "kg\x1f", "\x85kg"):
            self.assertEqual(units.unit_token(raw), raw.lower(), repr(raw))
        self.assertIsNone(units.unit_token("\ufeff\u3000"))
        self.assertEqual(units.unit_token("\x85"), "\x85")

    def test_the_projection_conflict_key_uses_the_fold(self):
        def same(a, b):
            return (project._conflict_key({"value": "5", "unit": a, "currency": None}) ==
                    project._conflict_key({"value": "5", "unit": b, "currency": None}))
        self.assertTrue(same("KGS", "kg"))
        self.assertTrue(same("Each", "pcs"))
        self.assertTrue(same(None, "  "))
        self.assertFalse(same("GR", "g"))
        self.assertFalse(same("kg", "g"))
        self.assertFalse(project._conflict_key({"value": "5", "unit": "kg", "currency": None}) ==
                         project._conflict_key({"value": "6", "unit": "kg", "currency": None}))


class MoneyBasisTests(unittest.TestCase):
    PROP = {"name": "total", "value_type": "money", "sources": [
        {"binding": "bc.orders", "path": "totalAmountIncludingTax", "tax_basis": "incl_tax"},
        {"binding": "portal.orders", "path": "total", "tax_basis": "unknown",
         "tax_basis_by_lineage": {"gold_etl_from_fm": "excl_tax"}}]}

    def test_declared_basis_with_a_lineage_refinement(self):
        self.assertEqual(units.money_basis(self.PROP, "bc.orders", "bc_a"), "incl_tax")
        self.assertEqual(units.money_basis(self.PROP, "portal.orders", "gold_etl_from_fm"),
                         "excl_tax")
        self.assertEqual(units.money_basis(self.PROP, "portal.orders", "gold_native_staging"),
                         "unknown")

    def test_undeclared_or_disagreeing_is_unknown(self):
        self.assertEqual(units.money_basis(self.PROP, "other.binding", None), "unknown")
        self.assertEqual(units.money_basis(None, "bc.orders", None), "unknown")
        prop = {"sources": [{"binding": "b", "path": "x", "tax_basis": "incl_tax"},
                            {"binding": "b", "path": "y", "tax_basis": "excl_tax"}]}
        self.assertEqual(units.money_basis(prop, "b", None), "unknown")

    def test_curated_schema_declares_the_grounded_bases(self):
        types = schema_loader.load_schema().object_types

        def basis(tid, prop, binding, lineage):
            spec = next(p for p in types[tid]["properties"] if p["name"] == prop)
            return units.money_basis(spec, binding, lineage)
        self.assertEqual(basis("SalesOrder", "total_amount", "bc.sales_orders", "bc_x"),
                         "incl_tax")
        self.assertEqual(basis("SalesOrder", "subtotal", "bc.sales_orders", "bc_x"), "excl_tax")
        for lineage in ("gold_etl_from_fm", "gold_etl_from_sage"):
            self.assertEqual(basis("SalesOrder", "total_amount", "portal.app_sales_order",
                                   lineage), "excl_tax")
        self.assertEqual(basis("SalesOrder", "total_amount", "portal.app_sales_order",
                               "gold_native_staging"), "unknown")
        self.assertEqual(basis("PurchaseOrder", "total_cost", "bc.purchase_orders", "bc_x"),
                         "incl_tax")
        self.assertEqual(basis("PurchaseOrder", "total_cost", "portal.app_purchase_order",
                               "gold_etl_from_fm"), "unknown")
        self.assertEqual(basis("SalesInvoice", "total_tax", "bc.sales_invoices", "bc_x"),
                         "tax_only")
        # BC line prices read their header's pricesIncludeTax per record, sales lines too.
        for tid, prop, link in (("PurchaseOrderLine", "bc_direct_unit_cost",
                                 "PurchaseOrderLine.line_of"),
                                ("PurchaseInvoiceLine", "unit_cost", "PurchaseInvoiceLine.line_of"),
                                ("SalesOrderLine", "unit_price", "SalesOrderLine.line_of"),
                                ("SalesInvoiceLine", "unit_price", "SalesInvoiceLine.line_of"),
                                ("SalesCreditMemoLine", "unit_price",
                                 "SalesCreditMemoLine.line_of")):
            spec = next(p for p in types[tid]["properties"] if p["name"] == prop)
            self.assertEqual([s.get("tax_basis_from") for s in spec["sources"]
                              if s["binding"].startswith("bc.")],
                             [{"via_link": link, "path": "pricesIncludeTax"}])
        sources = [s for t in types.values() for p in t["properties"]
                   if p["value_type"] == "money" for s in p["sources"]]
        self.assertTrue(sources)
        self.assertTrue(all(("tax_basis" in s) != ("tax_basis_from" in s) for s in sources))
        declared = [s["tax_basis"] for s in sources if "tax_basis" in s]
        self.assertTrue(all(b in units.TAX_BASES for b in declared), declared)


class PriceDenominatorTests(unittest.TestCase):
    """A unit price is per one unit (the schema's money_measure and per_unit*)."""

    RATES = {("SalesOrderLine", "unit_price"), ("SalesInvoiceLine", "unit_price"),
             ("SalesCreditMemoLine", "unit_price"),
             ("PurchaseOrderLine", "unit_price"), ("PurchaseOrderLine", "fm_item_cost"),
             ("PurchaseOrderLine", "bc_direct_unit_cost"), ("ReceiptLine", "bc_unit_cost"),
             ("PurchaseInvoiceLine", "unit_cost"), ("Product", "bc_unit_cost"),
             ("Product", "bc_unit_price"), ("SupplierItem", "system_cost_per_kg"),
             ("SupplierItem", "entry_cost"), ("QuoteLine", "unit_price"),
             ("QuoteLine", "std_cost"), ("FgItem", "retail_price"),
             ("SupplierConfirmation", "unit_price"), ("RelBcSalesQuoteLines", "unit_price")}

    def test_denominators_fold_spellings_and_never_convert(self):
        self.assertTrue(units.same_denominator(["EA", "ea"]))
        self.assertTrue(units.same_denominator(["each", "pcs", " ea "]))
        for pair in (["ea", "kg"], ["kg", "g"], ["GR", "g"], ["ea", None], [None, None], []):
            self.assertFalse(units.same_denominator(pair), pair)
        self.assertTrue(units.is_unit_price({"money_measure": "unit_price"}))
        self.assertFalse(units.is_unit_price({"money_measure": "amount"}))
        self.assertFalse(units.is_unit_price({}))
        self.assertFalse(units.is_unit_price(None))

    def test_measures_equal_the_format(self):
        fmt = json.loads((ROOT / "ontology/schema/_format.schema.json").read_text("utf-8"))
        self.assertEqual(tuple(fmt["$defs"]["money_measure"]["enum"]), units.MONEY_MEASURES)

    def test_curated_schema_declares_every_per_unit_price(self):
        types = schema_loader.load_schema().object_types
        money = {(tid, p["name"]): p for tid, t in types.items() for p in t["properties"]
                 if p["value_type"] == "money"}
        rates = {key for key, p in money.items() if units.is_unit_price(p)}
        self.assertEqual(rates, self.RATES)
        # A money name that reads as a price or cost per something is never an amount (a
        # raw column named total_price is the line's total, an amount).
        for (tid, name), p in money.items():
            if re.search(r"unit|price|per_|item_cost|entry_cost", name) and \
                    name != "total_price":
                self.assertIn((tid, name), rates, f"{tid}.{name}")
        for key in rates:
            for source in money[key]["sources"]:
                self.assertTrue({"per_unit", "per_unit_from", "per_unit_unknown"} & set(source),
                                (key, source["binding"]))

        def per(tid, prop, binding):
            [source] = [s for s in money[(tid, prop)]["sources"] if s["binding"] == binding]
            return source
        # The line's own UoM column, unknown where a writer stores another unit there.
        line = per("SalesOrderLine", "unit_price", "portal.app_sales_order_line")
        self.assertEqual(line["per_unit_from"], "uom")
        self.assertEqual(set(line["per_unit_unknown_by_lineage"]),
                         {"gold_etl_from_sage", "gold_etl_from_fm"})
        self.assertEqual(per("SalesOrderLine", "unit_price", "bc.sales_order_lines")
                         ["per_unit_from"], "unitOfMeasureCode")
        self.assertEqual(per("SalesOrderLine", "unit_price", "portal.sage_oe_order_detail")
                         ["per_unit_from"], "ordunit")
        self.assertEqual(set(per("PurchaseOrderLine", "unit_price",
                                 "portal.app_purchase_order_line")
                             ["per_unit_unknown_by_lineage"]), {"gold_etl_from_fm"})
        self.assertIn("per_unit_unknown", per("SalesInvoiceLine", "unit_price",
                                              "portal.sage_oe_invoice_detail"))
        self.assertEqual(per("SupplierItem", "entry_cost", "portal.fm_raw_material_supplier")
                         ["per_unit"], "kg")
        # Amounts keep summing: line totals are declared amounts.
        for tid in ("SalesOrderLine", "PurchaseOrderLine"):
            self.assertNotIn((tid, "line_total"), rates)
            self.assertIn((tid, "line_total"), money)


def sources_of(types, tid, prop):
    return next(p for p in types[tid]["properties"] if p["name"] == prop)["sources"]


class BcSalesTaxBasisTests(unittest.TestCase):
    """Sales line prices and the generated quote types take
    their tax basis from BC's own field where the entity declares it, else stay unknown
    saying so."""

    def test_a_sales_line_price_stores_its_headers_stated_basis(self):
        fake = tp.sales_credit_world()
        fake.data[(tp.MAN, "salesCreditMemos")][0]["pricesIncludeTax"] = True
        w = tp.BcWorld(self, fake)
        stored = w.reader.conn.execute(
            "SELECT tax_basis FROM object_values WHERE property = 'unit_price' AND "
            "binding_id = 'bc.sales_credit_memo_lines'").fetchall()
        self.assertEqual([tuple(r) for r in stored], [("incl_tax",)])
        fake.data[(tp.MAN, "salesCreditMemos")][0].pop("pricesIncludeTax")
        w = tp.BcWorld(self, fake)
        self.assertEqual(w.reader.conn.execute(
            "SELECT tax_basis FROM object_values WHERE property = 'unit_price' AND "
            "binding_id = 'bc.sales_credit_memo_lines'").fetchall()[0][0], "unknown")
        self.assertIn("SalesCreditMemoLine.unit_price@bc.sales_credit_memo_lines",
                      w.gaps("tax_basis_unresolved"))

    def test_generated_quote_prices_read_a_declared_field_else_say_none_is_declared(self):
        types = schema_loader.load_schema().object_types
        for tid, prop in (("RelBcSalesQuoteLines", "unit_price"),
                          ("RelBcSalesQuoteLines", "discount_amount"),
                          ("RelBcSalesQuotes", "discount_amount")):
            (source,) = sources_of(types, tid, prop)
            self.assertEqual(source["tax_basis"], "unknown")
            self.assertNotIn("tax_basis_from", source)
            note = next(p for p in types[tid]["properties"] if p["name"] == prop)[
                "evidence"][0]["note"]
            self.assertIn("declares no pricesIncludeTax", note, (tid, prop))
        # The pinned $metadata gives salesQuote no pricesIncludeTax; where it declares one,
        # the generator reads it on the header and through the line's header link.
        pin = copy.deepcopy(bc_generator.load_pin())
        pin["entity_sets"]["salesQuotes"]["properties"].append(
            {"name": "pricesIncludeTax", "type": "Edm.Boolean", "nullable": True})
        docs = [(n, d) for n, d in schema_loader.load_documents()
                if not n.startswith("generated/")]
        out, _ = bc_generator.generate(pin, docs, json.loads(
            bc_generator.REGISTRY_PATH.read_text(encoding="utf-8")))
        generated = {t["id"]: t for d in out.values() for t in d["object_types"]}
        (line,) = sources_of(generated, "RelBcSalesQuoteLines", "unit_price")
        self.assertEqual(line["tax_basis_from"], {"via_link": "RelBcSalesQuoteLines.sales_quote",
                                                  "path": "pricesIncludeTax"})
        (header,) = sources_of(generated, "RelBcSalesQuotes", "discount_amount")
        self.assertEqual(header["tax_basis_from"], "pricesIncludeTax")
        for source in (line, header):
            self.assertNotIn("tax_basis", source)
        (fixed,) = sources_of(generated, "RelBcSalesQuoteLines", "amount_including_tax")
        self.assertEqual(fixed["tax_basis"], "incl_tax")   # a declared basis stays


class LocalCurrencyTests(unittest.TestCase):
    """1-(3): a blank BC currencyCode read as its company's local
    currency cites the companyInformation version in force beside the document; a reader
    who may not read that record, and the claim gate re-reading for one, gets no currency;
    no version in force is unknown with a typed gap; the declaration is generic."""

    INVOICE = f'PurchaseInvoice:bc.purchase_invoices:{{"id":"{tp.gid(1, 4)}"}}'

    @staticmethod
    def world(test, fake=None) -> tp.BcWorld:
        """The purchase world with company 1's invoice in its local currency (blank code)."""
        fake = fake or tp.purchase_world()
        fake.data[(tp.MAN, "purchaseInvoices")][0]["currencyCode"] = ""
        return tp.BcWorld(test, fake)

    @staticmethod
    def company(fake) -> None:
        fake.data[(tp.MAN, "companyInformation")] = [tp.record(
            tp.gid(1, 30), displayName="Company", currencyCode="USD")]

    def rows(self, w: tp.BcWorld, prop: str = "total_amount") -> list[sqlite3.Row]:
        return w.reader.conn.execute(
            "SELECT * FROM object_values WHERE object_id = ? AND property = ? "
            "ORDER BY recorded_at", (self.INVOICE, prop)).fetchall()

    def test_the_local_currency_cites_its_company_and_a_reader_who_cannot_read_it_gets_none(self):
        fake = tp.purchase_world()
        self.company(fake)
        w = self.world(self, fake)
        (row,) = self.rows(w)
        cite = json.loads(row["currency_citation"])
        self.assertEqual(row["currency"], "USD")
        self.assertTrue(cite["object_id"].startswith("RelBcCompanyInformation:"))
        self.assertEqual((cite["field_path"], cite["lineage"]),
                         ("row.currencyCode", "bc_manufacturing"))
        self.assertEqual(w.resolved(cite["l1_handle"])["row"]["currencyCode"], "USD")
        company = w.reader.conn.execute("SELECT audience_key, (SELECT digest FROM "
                                        "object_versions WHERE object_id = o.object_id) FROM "
                                        "objects o WHERE object_id = ?",
                                        (cite["object_id"],)).fetchone()
        self.assertEqual(company[1], cite["digest"])
        handle = cite["l1_handle"]

        def reads(w: tp.BcWorld) -> dict:
            value = next(v for v in w.call("plx.object", {"object_id": self.INVOICE})["values"]
                         if v["property"] == "total_amount")
            invoices = w.around(tp.po_id(1), "PurchaseInvoice.invoices")["set"]
            groups = w.call("plx.aggregate", {"set": invoices, "measure": {
                "property": "total_amount", "op": "sum"}, "group_by": ["currency"]})
            check = next(c for c in w.call("plx.process", {
                "from": tp.po_id(1), "process": "purchase_to_invoice"})["quantity_checks"]
                if c["roles"] == ["ordered", "invoiced"])
            price = next(v for v in check["values"] if v["property"] == "unit_cost")
            return {"value": value, "groups": groups, "price": price}

        full = reads(w)
        self.assertEqual((full["value"]["currency"], full["value"]["currency_citation"]),
                         ("USD", {k: cite[k] for k in ("l1_handle", "field_path", "digest",
                                                      "lineage")}))
        self.assertEqual([(g["currency"], g["value"]) for g in full["groups"]["groups"]],
                         [("USD", "1200")])
        self.assertEqual(full["price"]["currency"], "USD")

        # A reader who may read every BC unit but this company's companyInformation.
        held = {m: {tp.READER} for m in store_ddl.MARKINGS}
        w.tools(ot.ReadPolicy(tenant="plx", audiences={
            "bc:*": {tp.READER}, "api:*": {tp.READER}, company[0]: set()}, markings=held))
        restricted = reads(w)
        self.assertNotIn(handle, json.dumps(restricted))
        self.assertIsNone(restricted["value"]["currency"])
        self.assertEqual(restricted["value"]["currency_gap"]["reason"], "currency_unknown")
        self.assertEqual(restricted["value"]["citation"], full["value"]["citation"])
        self.assertNotIn("USD", {g["currency"] for g in restricted["groups"]["groups"]})
        self.assertIsNone(restricted["price"]["currency"])
        with ProjectionStore(w.store_path, ReadPolicy(
                {"bc": lambda actor, rest: f"bc:{rest}" != company[0]},
                {m: frozenset({tp.READER}) for m in store_ddl.MARKINGS})) as store:
            (stored,) = store.values(tp.FULL, self.INVOICE, property="total_amount")
            self.assertEqual((stored["currency"], stored["currency_citation"]), (None, None))

        # The claim gate re-reads the company record for the reader: a sum stated in USD
        # holds only while the reader can read the currency there.
        returned = claim_checks.returned_evidence(
            "plx.object", {"object_id": self.INVOICE}, {"values": [full["value"]]})
        money = full["value"]["citation"]
        claim = claim_checks.parse_value_claims([{
            "value": "1200.00", "op": "sum", "currency": "USD",
            "of": [{"citation": money["l1_handle"], "field": money["field_path"]}]}],
            [money["l1_handle"]], [])

        def gate(readable: set[str]) -> list[str]:
            report = claim_checks.check_items(
                [{"id": "F1", "texts": ["The invoice totals 1200.00 USD."],
                  "citations": [{"handle": money["l1_handle"], "status": "resolved"}],
                  "value_claims": claim}],
                rows=lambda h: w.resolved(h) if h in readable else None, returned=returned)
            return [f["code"] for f in report["failures"]]
        self.assertEqual(gate({money["l1_handle"], handle}), [])
        self.assertEqual(gate({money["l1_handle"]}), ["claim_currency_mismatch"])

    def test_no_company_version_in_force_at_the_records_time_is_unknown_never_another(self):
        w = self.world(self)                # T0: no companyInformation captured yet
        self.assertEqual([r["currency"] for r in self.rows(w)], [None])
        self.company(w.h.fake)              # T0 + 1 day: the company and a changed invoice
        w.h.fake.data[(tp.MAN, "purchaseInvoices")][0].update(
            {"status": "Paid", "@odata.etag": 'W/"paid"',
             "lastModifiedDateTime": "2026-10-02T10:00:00Z"})
        w.h.clock.now += timedelta(days=1)
        w.h.run(entity_sets=CAPTURE_ENTITY_SETS)
        path = w.store_path.with_name("again.sqlite")
        with BcCaptures(w.h.db) as source:
            w.build = build_store(path, schema_loader.load_schema(), {"bc": source},
                                  normalizer=identifiers.normalize)
        w.reader = ot.StoreReader.open(path)
        self.addCleanup(w.reader.conn.close)
        first, second = self.rows(w)
        self.assertEqual((first["currency"], first["currency_citation"]), (None, None))
        self.assertEqual(second["currency"], "USD")
        company = json.loads(second["currency_citation"])
        self.assertEqual(w.reader.conn.execute(
            "SELECT recorded_at FROM object_versions WHERE l1_handle = ?",
            (company["l1_handle"],)).fetchone()[0], second["recorded_at"])
        def samples(code: str) -> set[str]:
            return {h for g in w.build["gaps"] if g["code"] == code and
                    g["where"] == "PurchaseInvoice.total_amount@bc.purchase_invoices"
                    for h in g["samples"]}
        self.assertIn(first["l1_handle"], samples("local_currency_unversioned"))
        self.assertNotIn(first["l1_handle"], samples("currency_unresolved"))

    def test_a_company_read_later_in_the_same_capture_pass_is_in_force_for_its_documents(self):
        """A real capture clock advances: BC reads companyInformation after the document
        sets, so its first version is recorded after the invoice's in the same pass."""
        class Ticking(tp.tb.Clock):
            def __call__(self):
                self.now += timedelta(seconds=1)
                return self.now
        fake = tp.purchase_world()
        self.company(fake)
        with mock.patch.object(tp.tb, "Clock", Ticking):
            w = self.world(self, fake)
        (row,) = self.rows(w)
        self.assertEqual(row["currency"], "USD")
        company = json.loads(row["currency_citation"])
        self.assertLess(row["recorded_at"], w.reader.conn.execute(
            "SELECT recorded_at FROM object_versions WHERE l1_handle = ?",
            (company["l1_handle"],)).fetchone()[0])
        self.assertNotIn("PurchaseInvoice.total_amount@bc.purchase_invoices",
                         w.gaps("local_currency_unversioned"))

    def test_local_currency_is_declared_on_any_binding_with_a_currency_field(self):
        def compiled(**local):
            doc = copy.deepcopy(fixture.DOMAIN)
            po = next(t for t in doc["object_types"] if t["id"] == "PurchaseOrder")
            po["bindings"][0]["local_currency"] = dict(
                {"blank": "currency", "binding": "t.supplier", "path": "code",
                 "evidence": fixture.EVIDENCE}, **local)
            return doc
        # A Portal binding (no BC capture) reading a record of its own lineage compiles.
        schema_loader.compile_documents([("domains/fixture_purchasing.json", compiled())])
        disjoint = compiled()
        supplier = next(t for t in disjoint["object_types"] if t["id"] == "Supplier")
        supplier["bindings"][0]["lineage"] = {"default": "fm_bronze"}
        for doc, problem in ((compiled(blank="status"), "is not a currency field"),
                             (disjoint, "shares no lineage")):
            with self.subTest(problem=problem), \
                    self.assertRaises(schema_loader.SchemaLoadError) as caught:
                schema_loader.compile_documents([("domains/fixture_purchasing.json", doc)])
            self.assertIn(problem, str(caught.exception))


class StoreFormatTests(unittest.TestCase):
    """Every reader checks the store format with one function."""

    def test_an_older_or_unnamed_store_format_is_refused_by_both_readers_alike(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        path = Path(scratch.name) / "ontology.sqlite"
        build_store(path, ot.default_schema(), {}, normalizer=identifiers.normalize)
        policy = ReadPolicy({})
        for name, sql in (("v2", "UPDATE store_meta SET value = 'plx-ontology-store/v2' "
                                 "WHERE key = 'format'"),
                          ("none", "DELETE FROM store_meta WHERE key = 'format'")):
            with self.subTest(store=name):
                with sqlite3.connect(path) as db:
                    db.execute(sql)
                with self.assertRaises(StoreError) as caught:
                    ProjectionStore(path, policy)
                self.assertEqual(caught.exception.code, "store_format_mismatch")
                reader = ot.StoreReader.open(path)
                self.addCleanup(reader.conn.close)
                self.assertEqual(reader.format_problem(), str(caught.exception))
                self.assertIn(store_ddl.FORMAT, reader.format_problem())


if __name__ == "__main__":
    unittest.main()
