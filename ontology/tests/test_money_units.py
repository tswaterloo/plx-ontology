"""Money units rules: currency spellings, and price denominators decided per record.

The session projection left ``currency_invalid`` and ``price_denominator_unknown`` on
most FM currency cells and Sage invoice prices (daily 2026-10-19: 77,711 and 275,734).
Each rule here is tested at the projection boundary: hand-made capture rows (no
business data) projected through the real compiled schema, reading the stored value's
unit and currency, the build report's gaps and its rule counts. Undecided cells stay
unknown with their gap, never guessed. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ontology import identifiers, schema, units
from ontology.project import CapturedRecord, build_store, object_id_of
from ontology.schema import load_schema
from ontology.units import (CURRENCY_SIGN_FORMS, ISO_4217, currency_code, currency_shape,
                            js_trim)
from devloop import live_verify
from scripts import money_units_probe
from scripts.money_units_probe import summarize

ROOT = Path(__file__).resolve().parents[2]
DOMAINS = ROOT / "ontology/schema/domains"
T = "2026-10-01T00:00:00.000000Z"
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
SUPPLIER = "portal.app_supplier_product"
SAGE = "portal.sage_oe_invoice_detail"
SPEC = json.loads((ROOT / "devloop/live-checks/ontology.money_units.json").read_text(
    encoding="utf-8"))

# app_supplier_product: id, FM source column, system_cost, currency cell.
SUPPLIER_ROWS = [
    ("sp-cad-sign", "raw", "4.10", "CAD$"),
    ("sp-usd-sign", "raw", "4.20", " $usd "),
    ("sp-us-form", "pack", "4.30", "US$"),
    ("sp-c-form", "pack", "4.40", "c$"),
    ("sp-plain", None, "4.50", "cad"),
    ("sp-bare-sign", "raw", "4.60", "$"),
    ("sp-foreign-sign", "raw", "4.70", "CAD€"),
    ("sp-not-iso", "raw", "4.80", "XYZ$"),
    ("sp-two-signs", "raw", "4.90", "$CAD$"),
    ("sp-word", "raw", "5.00", "Canadian"),
]
# sage oe_invoice_detail: _sage_pk, invunit, ordunit, priceunit (Sage right-pads text).
SAGE_ROWS = [
    ("inv-1|1", "EA      ", "ea", "EA"),
    ("inv-1|2", "CSE", "CSE ", "case"),     # one unit after the Portal fold
    ("inv-1|3", "EA", "CSE", "EA"),          # disagree
    ("inv-1|4", "EA", "", "EA"),             # one blank
    ("inv-1|5", "kg", None, "kg"),           # one null
    ("inv-1|6", "KG", "kg", "G"),            # kg and g are two units, never converted
]
ROWS = {
    "public.app_supplier_product": [
        {"id": i, "source": src, "system_cost": cost, "currency": cur}
        for i, src, cost, cur in SUPPLIER_ROWS],
    "sage.oe_invoice_header": [{"invuniq": "inv-1", "insourcurr": "CAD$"}],
    "sage.oe_invoice_detail": [
        {"_sage_pk": pk, "invuniq": "inv-1", "unitprice": "12.5", "invunit": inv,
         "ordunit": ordu, "priceunit": pri} for pk, inv, ordu, pri in SAGE_ROWS],
}
KEYS = {"sage.oe_invoice_detail": "_sage_pk", "sage.oe_invoice_header": "invuniq"}


class Source:
    source = "portal_api"

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        out = {}
        for rel in relations:
            key = KEYS.get(rel, "id")
            out[rel] = [CapturedRecord(
                "portal_api", rel, json.dumps({key: row[key]}), row,
                f"api://{rel}/{row[key]}@1", "row",
                hashlib.sha256(row[key].encode()).hexdigest(), T, None, T, f"api:{rel}")
                for row in ROWS.get(rel, [])]
        return out


class MoneyUnitsProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        cls.report = build_store(path, load_schema(), {"portal_api": Source()},
                                 normalizer=identifiers.normalize, clock=lambda: NOW)
        cls.conn = sqlite3.connect(str(path), check_same_thread=False)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def stored(self, tid, binding, key, native, prop):
        row = self.conn.execute(
            "SELECT value, unit, currency FROM object_values WHERE object_id = ? AND "
            "property = ?", (object_id_of(tid, binding, {key: native}), prop)).fetchone()
        return tuple(row) if row else None

    def supplier(self, native):
        return self.stored("SupplierItem", SUPPLIER, "id", native, "system_cost_per_kg")

    def sage(self, pk):
        return self.stored("SalesInvoiceLine", SAGE, "_sage_pk", pk, "unit_price")

    def gap(self, code, where):
        found = [g for g in self.report["gaps"] if g["code"] == code and g["where"] == where]
        return found[0] if found else None

    def count(self, name):
        return self.report["counts"].get(name, 0)

    # -- currency ------------------------------------------------------------------

    def test_an_iso_code_with_its_own_sign_attached_is_that_currency(self):
        self.assertEqual(self.supplier("sp-cad-sign")[2], "CAD")
        self.assertEqual(self.supplier("sp-usd-sign")[2], "USD")
        self.assertEqual(self.count(f"money_rule.currency.{SUPPLIER}.sign_attached"), 2)
        # A line reading its header's currency through the link folds the same way.
        self.assertEqual(self.sage("inv-1|1")[2], "CAD")
        self.assertEqual(self.count(f"money_rule.currency.{SAGE}.sign_attached"), 6)

    def test_a_sign_qualified_dollar_abbreviation_names_one_currency(self):
        self.assertEqual(self.supplier("sp-us-form")[2], "USD")
        self.assertEqual(self.supplier("sp-c-form")[2], "CAD")
        self.assertEqual(self.count(f"money_rule.currency.{SUPPLIER}.sign_form"), 2)

    def test_three_letters_in_any_case_stay_a_code_without_a_rule(self):
        # The base projection upper-cased before its [A-Z]{3} test, so "cad" was a code
        # already: no rule fires and nothing is counted as a decided currency_invalid.
        self.assertEqual(self.supplier("sp-plain")[2], "CAD")
        rules = sum(n for k, n in self.report["counts"].items()
                    if k.startswith(f"money_rule.currency.{SUPPLIER}."))
        self.assertEqual(rules, 4)   # the four signed cells, not the plain code

    def test_a_bare_or_foreign_sign_or_non_iso_core_stays_unknown_with_its_shape(self):
        for native in ("sp-bare-sign", "sp-foreign-sign", "sp-not-iso", "sp-two-signs",
                       "sp-word"):
            value = self.supplier(native)
            self.assertIsNotNone(value, native)          # the cost is still projected
            self.assertIsNone(value[2], native)           # its currency is unknown
        gap = self.gap("currency_invalid", f"SupplierItem.system_cost_per_kg@{SUPPLIER}")
        self.assertEqual(gap["count"], 5)
        # Shapes are counted, never the cell text.
        self.assertEqual({k: n for k, n in self.report["counts"].items()
                          if k.startswith("currency_shape.")},
                         {"currency_shape.$": 1, "currency_shape.AAA€": 1,
                          "currency_shape.AAA$": 1, "currency_shape.$AAA$": 1,
                          "currency_shape.AAAAAAAA": 1})
        self.assertNotIn("Canadian", json.dumps(self.report))
        self.assertNotIn("XYZ", json.dumps(self.report))

    def test_currency_code_refuses_what_it_cannot_decide(self):
        for raw in (None, 12, "", "   ", "$", "C", "CA", "USD$CAD", "U$D", "CAD$$",
                    "¥EUR", "£USD", "CAD €", "$ CAD"):
            self.assertEqual(currency_code(raw), (None, None), repr(raw))
        self.assertEqual(currency_code("£gbp"), ("GBP", "sign_attached"))
        self.assertEqual(currency_code("CDN$"), ("CAD", "sign_form"))
        self.assertEqual(currency_code(" $can"), ("CAD", "sign_form"))
        self.assertEqual(currency_code("$US"), ("USD", "sign_form"))
        # Three letters in any case are read as the base projection read them.
        self.assertEqual(currency_code(" CDN "), ("CDN", None))
        self.assertEqual(currency_code("cad"), ("CAD", None))
        self.assertEqual(currency_code("cdn"), ("CDN", None))
        self.assertTrue({"CAD", "USD", "EUR", "GBP", "JPY", "CNY"} <= ISO_4217)
        self.assertEqual(currency_shape("Ab9-x" * 3), "AA9-AAA9-AAA+")

    # -- price denominators --------------------------------------------------------

    def test_a_sage_invoice_price_is_per_the_unit_all_three_columns_agree_on(self):
        self.assertEqual(self.sage("inv-1|1"), ("12.5", "EA", "CAD"))   # trimmed text
        self.assertEqual(self.sage("inv-1|2")[1], "CSE")                # case = cse
        self.assertEqual(self.count(f"money_rule.price_denominator.{SAGE}.units_agree"), 2)

    def test_disagreeing_or_blank_sage_units_stay_unknown_with_a_typed_reason(self):
        for pk in ("inv-1|3", "inv-1|4", "inv-1|5", "inv-1|6"):
            self.assertEqual(self.sage(pk)[1], None, pk)
        gap = self.gap("price_denominator_unknown", f"SalesInvoiceLine.unit_price@{SAGE}")
        self.assertEqual(gap["count"], 4)
        prefix = f"money_rule.price_denominator.{SAGE}.undecided."
        self.assertEqual(self.count(prefix + "units_disagree"), 2)
        self.assertEqual(self.count(prefix + "unit_blank"), 2)
        self.assertIsNone(self.gap("per_unit_unresolved", f"SalesInvoiceLine.unit_price@{SAGE}"))

    def test_an_fm_raw_supplier_cost_is_per_kg_and_packaging_stays_unknown(self):
        self.assertEqual(self.supplier("sp-cad-sign")[:2], ("4.10", "kg"))
        for native in ("sp-us-form", "sp-c-form", "sp-plain"):   # pack, Portal-native
            self.assertIsNone(self.supplier(native)[1], native)
        self.assertEqual(
            self.count(f"money_rule.price_denominator.{SUPPLIER}.fm_raw_per_kg"), 7)
        self.assertEqual(
            self.count(f"money_rule.price_denominator.{SUPPLIER}.undecided.no_rule_holds"), 3)
        gap = self.gap("price_denominator_unknown",
                       f"SupplierItem.system_cost_per_kg@{SUPPLIER}")
        self.assertEqual(gap["count"], 3)

    def test_every_decided_or_undecided_record_is_counted_once(self):
        # before (one gap per record) = decided + still unknown, per source.
        for binding, tid, prop, rows in (
                (SAGE, "SalesInvoiceLine", "unit_price", SAGE_ROWS),
                (SUPPLIER, "SupplierItem", "system_cost_per_kg", SUPPLIER_ROWS)):
            prefix = f"money_rule.price_denominator.{binding}."
            decided = sum(n for k, n in self.report["counts"].items()
                          if k.startswith(prefix) and ".undecided." not in k)
            undecided = sum(n for k, n in self.report["counts"].items()
                            if k.startswith(prefix + "undecided."))
            unknown = self.gap("price_denominator_unknown", f"{tid}.{prop}@{binding}")
            self.assertEqual(decided + unknown["count"], len(rows), binding)
            self.assertEqual(undecided, unknown["count"], binding)

    # -- the live check's summary over this build -----------------------------------

    def test_the_live_check_passes_on_this_build_and_names_no_cell_text(self):
        out = summarize(self.report, {"price_denominator_unknown": 275734,
                                      "currency_invalid": 77711})
        out.update(sources=["portal_api"], blocked_upstream=[],
                   session_projection={"opened": True})
        self.assertEqual(live_verify.check_expectations(SPEC, 0, out), [])
        self.assertTrue(out["price_denominator_halved"] and out["currency_invalid_halved"])
        # Same generation: after + decided = without rules, per code.
        self.assertEqual(out["without_rules"]["price_denominator_unknown"],
                         len(SAGE_ROWS) + len(SUPPLIER_ROWS))
        self.assertEqual(out["gap_codes"]["price_denominator_unknown"], 7)
        self.assertEqual(out["rules"]["currency"], {
            f"{SAGE}.sign_attached": 6,
            f"{SUPPLIER}.sign_attached": 2, f"{SUPPLIER}.sign_form": 2})
        self.assertEqual(out["gap_codes_by_source"]["currency_invalid"], {"portal_app": 5})
        self.assertEqual(out["gap_codes_by_source"]["price_denominator_unknown"],
                         {"portal_app": 3, "sage": 4})
        self.assertEqual(out["ruled_price_conflicts"], 0)
        self.assertEqual(out["ratio_to_previous"]["currency_invalid"],
                         round(5 / 77711, 4))
        text = json.dumps(out)
        for _, _, _, cell in SUPPLIER_ROWS:
            if len(cell.strip()) > 1:
                self.assertNotIn(f'"{cell.strip()}', text)
        for _, inv, _, _ in SAGE_ROWS:
            self.assertNotIn(f'"{inv.strip()}"', text)

    def test_the_live_check_fails_when_rules_decide_too_little_or_make_a_conflict(self):
        report = {"counts": {f"money_rule.price_denominator.{SAGE}.units_agree": 4,
                             f"money_rule.currency.{SUPPLIER}.sign_form": 6,
                             "money_rule.price_denominator.conflicts."
                             "SalesInvoiceLine.unit_price": 1},
                  "gaps": [{"code": "price_denominator_unknown", "count": 5,
                            "where": f"SalesInvoiceLine.unit_price@{SAGE}"},
                           {"code": "currency_invalid", "count": 5,
                            "where": f"SupplierItem.system_cost_per_kg@{SUPPLIER}"}]}
        out = summarize(report, {})
        out.update(sources=["portal_api"], blocked_upstream=[],
                   session_projection={"opened": True})
        failures = live_verify.check_expectations(SPEC, 0, out)
        self.assertEqual(len(failures), 3, failures)   # price halving, conflicts x2
        self.assertTrue(out["currency_invalid_halved"])   # 6 of 11 decided
        self.assertFalse(out["previous"]["recorded"])
        # Nothing decided and nothing unknown is no evidence of halving.
        self.assertFalse(summarize({"counts": {}, "gaps": []})["price_denominator_halved"])

    def test_a_rule_fires_only_on_a_cell_the_base_projection_held_invalid(self):
        # The base test (project.py@c65c2df822): trim, upper-case, [A-Z]{3}. A rule that
        # fired on a cell it accepted would count a gap that never was (halving inflated).
        base = re.compile(r"^[A-Z]{3}$")
        letters = ["cad", "Cad", "CAD", "cdn", "usd", "xyz", "eur"]
        cells = [*letters, *(f"{a}{l}{b}" for l in letters for a, b in
                             (("$", ""), ("", "$"), ("€", ""), ("", "£"), ("$", "$"))),
                 *CURRENCY_SIGN_FORMS, *(f.lower() for f in CURRENCY_SIGN_FORMS),
                 "$", "C", "Canadian", " cad ", "\u00a0usd"]
        for cell in cells:
            code, rule = currency_code(cell)
            was_valid = bool(base.fullmatch(js_trim(cell).upper()))
            if was_valid:
                self.assertEqual((code, rule), (js_trim(cell).upper(), None), cell)
            elif code is not None:
                self.assertIsNotNone(rule, cell)
        # 100 invalid cells and 100 lower-case codes: nothing decided, never halved.
        report = {"counts": {}, "gaps": [{"code": "currency_invalid", "count": 100,
                                          "where": f"SupplierItem.x@{SUPPLIER}"}]}
        for _ in range(100):
            code, rule = currency_code("cad")
            if rule is not None:
                report["counts"][f"money_rule.currency.{SUPPLIER}.{rule}"] = \
                    report["counts"].get(f"money_rule.currency.{SUPPLIER}.{rule}", 0) + 1
        out = summarize(report)
        self.assertFalse(out["currency_invalid_halved"])
        self.assertEqual(out["without_rules"]["currency_invalid"], 100)

    def test_a_conflict_on_a_rule_read_currency_fails_the_check(self):
        report = {"counts": {f"money_rule.currency.{SUPPLIER}.sign_form": 10,
                             "money_rule.currency.conflicts.SupplierItem.x": 2},
                  "gaps": [{"code": "currency_invalid", "count": 1,
                            "where": f"SupplierItem.x@{SUPPLIER}"},
                           {"code": "conflict_across_records", "count": 2,
                            "where": "SupplierItem.x"}]}
        out = summarize(report)
        self.assertEqual(out["rules"]["currency"], {f"{SUPPLIER}.sign_form": 10})
        self.assertEqual(out["ruled_currency_conflicts"], 2)
        self.assertFalse(out["no_new_conflicts"])
        out.update(sources=["portal_api"], blocked_upstream=[],
                   session_projection={"opened": True})
        failures = live_verify.check_expectations(SPEC, 0, out)
        self.assertTrue(any("ruled_currency_conflicts" in f for f in failures), failures)

    def test_no_generation_of_this_session_is_an_error_never_a_pass(self):
        with tempfile.TemporaryDirectory() as empty:
            for directory, env in ((empty, {"PLX_CAPTURE_FRESH_SINCE": T}),
                                   (empty, {}), (None, {"PLX_CAPTURE_FRESH_SINCE": T})):
                out = money_units_probe.run(directory, None, environ=env)
                self.assertEqual(out["error"], "session_projection_unavailable")
                self.assertTrue(live_verify.check_expectations(SPEC, 1, out))

    def test_the_previous_daily_is_read_as_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "latest.json"
            daily.write_text(json.dumps({"checks": [
                {"id": "ontology.session_projection", "detail": {"fields": {
                    "gap_codes": {"currency_invalid": 77711, "other_code": 3}}}}]}))
            self.assertEqual(money_units_probe.previous_gap_codes(str(daily)),
                             {"currency_invalid": 77711, "other_code": 3})
            self.assertEqual(money_units_probe.previous_gap_codes(str(daily) + ".x"), {})
        self.assertEqual(money_units_probe.family("A.b@bc.sales_invoice_lines"), "bc")
        self.assertEqual(money_units_probe.family("A.b@portal.fm_x"), "fm")
        self.assertEqual(money_units_probe.family("A.b@sharepoint.drive"), "other")
        self.assertEqual(money_units_probe.family("A.b"), "cross_record")


CATALOG = schema.load_catalog()   # the served columns a rule may read


def loader_errors(edit) -> list[str]:
    profiles = json.loads(schema.DOCUMENT_FIELDS_PATH.read_text(encoding="utf-8"))
    docs = []
    for p in sorted(DOMAINS.glob("*.json")):
        doc, errors = schema.derive_document_types(
            p.stem, json.loads(p.read_text(encoding="utf-8")), profiles)
        assert not errors, errors
        if p.stem == "orders":
            edit(doc)
        docs.append((f"domains/{p.stem}.json", doc))
    try:
        schema.compile_documents(docs, catalog=CATALOG)
    except schema.SchemaLoadError as exc:
        return exc.errors
    return []


def sage_source(doc):
    t = next(t for t in doc["object_types"] if t["id"] == "SalesInvoiceLine")
    p = next(p for p in t["properties"] if p["name"] == "unit_price")
    return next(s for s in p["sources"] if s["binding"] == SAGE)


class PerUnitRulesSchemaTests(unittest.TestCase):
    def test_the_shipped_rules_load(self):
        self.assertEqual(loader_errors(lambda doc: None), [])

    def test_rules_only_refine_a_declared_unknown_denominator(self):
        def edit(doc):
            del sage_source(doc)["per_unit_unknown"]
            sage_source(doc)["per_unit_from"] = "invunit"
        self.assertTrue(any("missing required property 'per_unit_unknown'" in e
                            for e in loader_errors(edit)))

    def test_a_rule_reads_served_columns_of_the_record_itself(self):
        def unknown_column(doc):
            sage_source(doc)["per_unit_rules"][0]["agreeing"] = ["invunit", "no_such_unit"]
        self.assertTrue(any("unknown column 'no_such_unit'" in e
                            for e in loader_errors(unknown_column)))

        def hop(doc):
            sage_source(doc)["per_unit_rules"] = [{
                "id": "hop", "when": [{"via_link": "SalesInvoiceLine.line_of",
                                       "path": "insourcurr", "op": "eq", "value": "CAD"}],
                "per_unit": "kg", "evidence": [{"kind": "ref",
                                                "id": "workbench.sage_invoice_units_agree"}]}]
        self.assertTrue(any("never via_link" in e for e in loader_errors(hop)))

    def test_a_rule_unit_is_a_known_literal_and_ids_are_unique(self):
        def placeholder(doc):
            rule = copy.deepcopy(sage_source(doc)["per_unit_rules"][0])
            sage_source(doc)["per_unit_rules"] = [
                rule, dict(rule, id="units_agree")]
            sage_source(doc)["per_unit_rules"].append({
                "id": "lit", "when": [{"path": "invunit", "op": "eq", "value": "EA"}],
                "per_unit": "item_uom", "evidence": rule["evidence"]})
        errors = loader_errors(placeholder)
        self.assertTrue(any("per_unit 'item_uom' is not a known literal unit" in e
                            for e in errors), errors)
        self.assertTrue(any("per_unit_rules ids repeat" in e for e in errors), errors)

    def test_a_rule_is_either_agreeing_cells_or_a_tested_literal(self):
        def both(doc):
            sage_source(doc)["per_unit_rules"][0]["per_unit"] = "kg"
        self.assertTrue(any("format:" in e for e in loader_errors(both)))


class OneCurrencyTableTests(unittest.TestCase):
    """Lane currency-one-table: a record cell (``currency_code``), a document field
    (``ontology/document_fields.py``) and a document price
    (``integrations/document_understanding.py``) read one written currency the same way,
    from the one table in ``ontology/units.py``, and never emit a code outside ISO 4217."""

    MONEY = {"name": "amount", "kind": "money"}

    def field(self, form: str, declared: tuple[str, ...] = ()) -> str | None:
        from ontology import document_fields
        value, _problem, _ = document_fields._parse(self.MONEY, f"{form}12.00", None,
                                                    declared)
        return value and value["currency"]

    def price(self, form: str, declared: str | None = None) -> str | None:
        from integrations.document_understanding import extract_document
        text = (f"Currency: {declared}\n" if declared else "") + f"Unit price: {form}12.00\n"
        claims = [c for c in extract_document(text.encode(), "text/plain",
                                              original_url="https://example.test/po")
                  .claims if c["kind"] == "price"]
        self.assertLessEqual(len(claims), 1)
        return next((c["currency"] for c in claims if c["currency"] != "unknown"), None)

    def test_every_sign_form_reads_one_iso_currency_in_all_three_readers(self):
        signs = {sign: codes for sign, codes in units.SIGN_CURRENCIES.items()}
        forms = {form: code for form, code in CURRENCY_SIGN_FORMS.items()}
        for sign, codes in signs.items():
            for code in codes:
                forms[f"{code}{sign}"] = forms[f"{sign}{code}"] = code
        forms.update({code: code for code in ("USD", "CAD", "EUR", "GBP", "AUD", "JPY")})
        for form, code in forms.items():
            for written in (form, form.lower()):
                with self.subTest(form=written):
                    self.assertEqual((currency_code(written)[0], self.field(written),
                                      self.price(written)), (code, code, code))

    def test_a_sign_is_the_same_currency_or_the_same_ambiguity_in_both_document_readers(self):
        self.assertTrue(units.SIGN_CURRENCIES)
        for sign, codes in units.SIGN_CURRENCIES.items():
            only = next(iter(codes)) if len(codes) == 1 else None
            with self.subTest(sign=sign):
                self.assertEqual((self.field(sign), self.price(sign)), (only, only))
                self.assertEqual(currency_code(sign), (None, None))   # a cell: no declaration
            for declared in sorted(ISO_4217):
                expected = declared if declared in codes else None
                with self.subTest(sign=sign, declared=declared):
                    self.assertEqual((self.field(sign, (declared,)),
                                      self.price(sign, declared)), (expected, expected))

    def test_no_reader_emits_a_code_outside_iso_4217(self):
        self.assertTrue(set().union(*units.SIGN_CURRENCIES.values()) <= ISO_4217)
        self.assertTrue(set(CURRENCY_SIGN_FORMS.values()) <= ISO_4217)
        for form in ("RMB", "RMB¥", "¥RMB", "XYZ", "XYZ$", "NT$", "CAD€", "€USD"):
            with self.subTest(form=form):
                self.assertIsNone(self.field(form))
                self.assertIsNone(self.price(form))
                self.assertIsNone(self.field("¥", (form,)))
                self.assertIsNone(self.price("$", form))
        self.assertIsNone(currency_code("RMB¥")[0])

    def test_a_code_that_is_an_english_word_is_no_currency_in_document_text(self):
        self.assertTrue(units.WORD_CODES <= ISO_4217)
        for word in sorted(units.WORD_CODES):
            for written in (word, word.lower(), word.title()):
                with self.subTest(word=written):
                    self.assertIsNone(self.field(f"{written} "))
                    self.assertIsNone(self.price(f"{written} "))
        from ontology import document_fields
        for text in ("USD 3,100.00 all-in", "3,100.00 USD ALL IN", "2,450.00 all-in",
                     "USD 4.20 KGS", "4.20 kgs"):
            with self.subTest(text=text):
                value, _problem, _ = document_fields._parse(self.MONEY, text, None, ())
                self.assertNotIn(value and value["currency"], units.WORD_CODES, text)
            with self.subTest(text=text, declared="USD"):
                value, problem, _ = document_fields._parse(self.MONEY, text, None, ("USD",))
                self.assertNotIn("conflict", str(problem), text)
        for text in ("Price: all $12.00", "Unit price: ALL 12.00", "Freight quote: 2,100.00 ALL IN"):
            claims = [c for c in __import__("integrations.document_understanding", fromlist=["x"])
                      .extract_document(text.encode(), "text/plain",
                                        original_url="https://example.test/po").claims
                      if c["kind"] == "price"]
            with self.subTest(text=text):
                self.assertFalse([c for c in claims if c["currency"] == "ALL"], claims)


if __name__ == "__main__":
    unittest.main()
