"""The one identifier grammar (ontology/schema/identifiers.json + ontology/identifiers.py).

Evidence label: ``isolated-fixture`` (no source is read). Golden forms come from
docs/reference/cases-and-artifacts.md and portal-code-map.md and the Portal code
they cite (plx-customer-portal@b46934a).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import random
import re
import time
import unittest

from ontology import identifiers as ids

ROOT = Path(__file__).resolve().parents[2]

# (kind, as written, normalised, blocking key)
GOLDEN = [
    # Petra PO (po-number-series.ts, create-fm-purchase-order.ts, cases-and-artifacts.md PO5447 case)
    ("petra_po", "PO5447", "PO5447", "5447"),
    ("petra_po", "PO#5447", "PO5447", "5447"),
    ("petra_po", "PO 5447", "PO5447", "5447"),
    ("petra_po", "Our PO 5447", "PO5447", "5447"),
    ("petra_po", "P.O. # 5447", "PO5447", "5447"),
    ("petra_po", "PO-5447", "PO5447", "5447"),
    ("petra_po", "po05447", "PO5447", "5447"),
    # A bare stored number (Sage / FM ponumber) is series-less, never PO4821.
    ("petra_po", "04821", "4821", "4821"),
    ("petra_po", "PO5470", "PO5470", "5470"),
    ("petra_po", "Purchase Order No. 106089", "PO106089", "106089"),
    ("petra_po", "PO100000", "PO100000", "100000"),
    ("petra_po", "DPO100000", "DPO100000", "DPO100000"),
    ("petra_po", "PO#5280", "PO5280", "5280"),
    ("petra_po", "UAT-PO#5283", "UAT-PO5283", "5283"),
    ("petra_po", "PO#5300-FM77", "PO5300-FM77", "5300FM77"),
    ("petra_po", "PO-K3J9X2", "PO-K3J9X2", "K3J9X2"),
    # Proforma invoices (cases-and-artifacts.md F02: one document, three numbers)
    ("pi_ref", "PI-SWC024613", "PI-SWC024613", "24613"),
    ("pi_ref", "PI-SWC025240", "PI-SWC025240", "25240"),
    ("pi_ref", "PI-25240", "PI-25240", "25240"),
    ("pi_ref", "PI23219", "PI-23219", "23219"),
    # Customer PO (portal-code-map.md s3.3; SalesOrder 351 customer PO PO12172025)
    ("customer_po", "PO12172025", "PO12172025", "12172025"),
    ("customer_po", " PO# 4500 12 ", "PO# 4500 12", "450012"),
    ("customer_po", "Customer P.O. 4500", "CUSTOMER P.O. 4500", "4500"),
    ("customer_po", "Jane Smith", "JANE SMITH", "Jane Smith".upper()),
    # Sales orders (portal-code-map.md s3.2)
    ("so_number", "SO-2026-0012", "SO-2026-0012", "SO-2026-0012"),
    ("so_number", "SO100000", "SO100000", "SO100000"),
    ("so_number", "dso100001", "DSO100001", "DSO100001"),
    ("so_number", "SO-FM-351", "SO-FM-351", "SO-FM-351"),
    ("so_number", "OR1000151885", "OR1000151885", "OR1000151885"),
    # BC numbers (renumbered POs / SOs; portal-code-map.md s4.10)
    ("bc_document_no", "po100523", "PO100523", "PO100523"),
    ("bc_external_document_no", "PO12172025", "PO12172025", "PO12172025"),
    ("bc_system_id", "BBBBBBBB-0000-0000-0000-000000000009",
     "bbbbbbbb-0000-0000-0000-000000000009", "bbbbbbbb-0000-0000-0000-000000000009"),
    # AO / MO / batch / lot / receipt (portal-code-map.md s3.5, s3.8, s3.9)
    ("ao_code", "AO-26005B", "AO-26005B", "AO-26005B"),
    ("ao_code", "ao-26119a", "AO-26119A", "AO-26119A"),
    ("mo_code", "MO-k3j9x2", "MO-K3J9X2", "MO-K3J9X2"),
    ("batch_code", "BC-26119A", "BC-26119A", "BC-26119A"),
    ("receipt_lab_id", "26161-1", "26161-1", "26161-1"),
    ("receipt_lab_id", "RP0008270", "RP0008270", "RP0008270"),
    ("lot_number", "26161-1@a01", "26161-1@A01", "26161-1"),
    ("lot_number", "fm-wlp-123", "FM-WLP-123", "FM-WLP-123"),
    ("lot_number", "SAGE-11-A1-DVS-5003", "SAGE-11-A1-DVS-5003", "SAGE-11-A1-DVS-5003"),
    # Quotes, shipments, releases, requisitions, eQMS, formulas, jobs
    ("quote_number", "QUO-2026-0042", "QUO-2026-0042", "QUO-2026-0042"),
    ("quote_number", "QT.280426.01", "QT.280426.01", "QT.280426.01"),
    ("shipment_number", "SH100000", "SH100000", "SH100000"),
    # Environment-prefixed (create-shipment.ts prefixRef@c3ffda3): the prefix is kept.
    ("shipment_number", " uat-sh100017 ", "UAT-SH100017", "UAT-SH100017"),
    ("ship_release", "SR-20260115-0003", "SR-20260115-0003", "SR-20260115-0003"),
    ("pr_number", "PR-20260901-0001", "PR-20260901-0001", "PR-20260901-0001"),
    ("eqms_number", "capa-26004", "CAPA-26004", "CAPA-26004"),
    ("formula_code", "F-26-0123-A", "F-26-0123-A", "F-26-0123-A"),
    ("manufacturing_job", "MJ-2026-K3J9", "MJ-2026-K3J9", "MJ-2026-K3J9"),
    ("tracking_number", "1Z 999 AA1 0123 4567 84", "1Z999AA10123456784", "1Z999AA10123456784"),
    # Items (trimmed, case-insensitive, whole: BC's 20-character cut is the sent form)
    ("item_code", "DVS-5003", "DVS-5003", "DVS-5003"),
    ("item_code", "dvs-5003-3.78l-a", "DVS-5003-3.78L-A", "DVS-5003-3.78L-A"),
    ("item_code", "P-02458", "P-02458", "P-02458"),
    ("item_code", "ABCDEFGHIJKLMNOPQRSTUVWX", "ABCDEFGHIJKLMNOPQRSTUVWX",
     "ABCDEFGHIJKLMNOPQRSTUVWX"),
    ("erp_item_code", "dvs5003", "DVS5003", "DVS5003"),
    ("customer_erp_code", "take5", "TAKE5", "TAKE5"),
    ("supplier_code", "acme", "ACME", "ACME"),
    # FM / Sage / project keys (portal-code-map.md s3.1, s3.2, s3.12; AO trace)
    ("fm_id", "10718", "10718", "10718"),
    ("fm_id", "351.0", "351", "351"),
    ("fm_id", 89, "89", "89"),
    ("fm_external_id", "FM-CUST-89", "fm-cust-89", "fm-cust-89"),
    ("fm_external_id", "fm-supp-7", "fm-supp-7", "fm-supp-7"),
    ("fm_external_id", "fm-raw-123", "fm-raw-123", "fm-raw-123"),
    ("fm_external_id", "fm-pack-45", "fm-pack-45", "fm-pack-45"),
    ("fm_external_id", "fm-form-12", "fm-form-12", "fm-form-12"),
    ("fm_external_id", "sage-cust-take5", "sage-cust-TAKE5", "sage-cust-TAKE5"),
    ("sage_ord_uniq", "123456.0", "123456", "123456"),
    ("project_key", " 95 ", "95", "95"),
    ("project_key", "clx0abcdefghijklmnopqrst", "clx0abcdefghijklmnopqrst",
     "clx0abcdefghijklmnopqrst"),
]
NOT_VALID = [
    ("petra_po", "UAT-5447"), ("petra_po", "PO box 12"), ("petra_po", "REPO123"),
    ("petra_po", "PO-Box"), ("ao_code", "AO-12"), ("pi_ref", "PI-AB"),
    ("fm_id", "12.5"), ("fm_id", True), ("bc_system_id", "not-a-guid"),
    ("sage_ord_uniq", "abc"), ("project_key", "my project"), ("shipment_number", "SH12"),
]


class SchemaTests(unittest.TestCase):
    def test_schema_is_self_describing_and_every_kind_is_complete(self):
        data = ids.schema()
        self.assertEqual(data["format"], "plx-identifiers/v1")
        self.assertEqual(ids.FORMAT, "plx-identifiers/v1")
        names = set(ids.kinds())
        for kind in ids.kinds():
            spec = ids.kind_spec(kind)
            with self.subTest(kind=kind):
                for key in ("object_type", "description", "value_pattern", "normalizer",
                            "identity", "alias_kinds", "forms", "carriers"):
                    self.assertIn(key, spec)
                self.assertIn(spec["identity"], ("anchor", "alias", "lead"))
                self.assertIs(spec["auto_merge"], False)
                self.assertTrue(set(spec["alias_kinds"]) <= names | {"petra_po_fm_number",
                                                                     "petra_po_fm_twin_number"})
                self.assertTrue(spec["forms"] and spec["carriers"])
                for carrier in spec["carriers"]:
                    self.assertTrue(carrier["upstream"])
                    self.assertIn(carrier["system"], ("portal", "fm", "sage", "bc", "mail",
                                                      "sharepoint"))
                for form in spec["forms"]:
                    self.assertIsNotNone(ids.normalize(kind, form["example"]),
                                         (kind, form["example"]))

    def test_cross_system_keys_are_declared_as_link_bindings(self):
        bindings = {b["name"]: b for b in ids.link_bindings()}
        wanted = {
            "fm_order_entry_to_sales_order": ("custom_fields.fmId", "verified"),
            "sage_order_to_sales_order": ("custom_fields.sageOrdUniq", "verified"),
            "portal_entity_to_bc_number": ("number", "verified"),
            "portal_entity_to_bc_system_id": ("id", "verified"),
            "sales_order_po_reference_to_bc_sales_order": ("externalDocumentNumber", "lead"),
            "sales_order_number_to_bc_sales_invoice": ("externalDocumentNumber", "lead"),
            "product_to_bc_item": ("number", "verified"),
            "product_to_sage_item": ("itemno", "verified"),
            "project_key": ("id", "verified"),
            "gold_po_to_fm_po": ("id", "verified"),
            "gold_po_renumbered_to_bc": ("number", "verified"),
        }
        for name, (to_field, tier) in wanted.items():
            with self.subTest(binding=name):
                self.assertEqual(bindings[name]["to"]["field"], to_field)
                self.assertEqual(bindings[name]["tier"], tier)
                self.assertIn("@", bindings[name]["upstream"] + "@")
        # app_bc_entity_map is not served by the reporting API: declared, not readable.
        self.assertFalse(bindings["portal_entity_to_bc_number"]["served"]["from"])
        for name in ("supplier_name", "mail_sender_domain", "mail_and_sharepoint_mentions"):
            self.assertEqual(bindings[name]["tier"], "lead")
        for binding in bindings.values():
            self.assertIn(binding["tier"], ("verified", "lead"))
            kind = binding["identifier_kind"]
            self.assertTrue(kind == "*" or kind in ids.kinds(), kind)

    def test_every_profile_kind_maps_to_a_declared_kind(self):
        known = set(ids.kinds())
        mapped = ids.schema()["label_kind_map"]
        for name, profile in ids.schema()["mention_profiles"].items():
            for entry in profile["patterns"]:
                with self.subTest(profile=name, kind=entry["kind"]):
                    self.assertIn(mapped.get(entry["kind"], entry["kind"]), known)


class SearchKeysTests(unittest.TestCase):
    def test_reverse_alias_kinds_are_the_kinds_that_declare_the_alias(self):
        self.assertEqual(ids.alias_of_kinds("batch_code"), ("lot_number",))
        self.assertEqual(ids.alias_of_kinds("quote_number"), ())
        for kind in ids.kinds():
            for other in ids.alias_of_kinds(kind):
                self.assertIn(kind, ids.alias_kinds(other))

    def test_search_keys_cover_own_alias_and_reverse_alias_kinds(self):
        self.assertEqual(ids.search_keys("petra_po", "PO100012", "PO100012"),
                         [("petra_po", "PO100012"), ("bc_document_no", "PO100012")])
        # petra_po declares bc_document_no as its alias: searching the BC number also
        # searches the PO kind (the reverse), each by its own normaliser.
        reverse = [kind for kind, _ in ids.search_keys("bc_document_no", "PO100012",
                                                       "PO100012")]
        self.assertEqual(reverse[0], "bc_document_no")
        self.assertIn("petra_po", reverse)
        self.assertEqual(len(reverse), len(set(reverse)))
        self.assertEqual(ids.search_keys("no_such_kind", "X", "X"), [("no_such_kind", "X")])


class ExactCompareTests(unittest.TestCase):
    """find-root-exact-compare (b): every kind declares the owning system's literal
    comparison; Portal SO numbers compare by case, PO numbers by TRIM ILIKE."""

    def test_every_kind_declares_its_exact_compare(self):
        for kind in ids.kinds():
            self.assertIn(ids.kind_spec(kind)["exact_compare"], ids.EXACT_COMPARES, kind)
        self.assertEqual(ids.kind_spec("so_number")["exact_compare"], "trim")
        self.assertEqual(ids.kind_spec("petra_po")["exact_compare"], "trim_casefold")
        self.assertIn("route.ts:279", ids.kind_spec("petra_po")["exact_compare_upstream"])

    def test_literal_comparison_per_kind(self):
        self.assertTrue(ids.exact_compare("so_number", " SO100201", "SO100201 "))
        self.assertFalse(ids.exact_compare("so_number", "SO100201", "so100201"))
        self.assertTrue(ids.exact_compare("petra_po", "PO100777", " po100777 "))
        # ILIKE without wildcards: % and _ are literal characters, never patterns.
        self.assertFalse(ids.exact_compare("petra_po", "PO100777", "PO1007%"))
        self.assertFalse(ids.exact_compare("petra_po", "PO100777", "PO10077_"))
        # A normalised equal form is not a literal match.
        self.assertFalse(ids.exact_compare("petra_po", "PO#4821", "PO4821"))
        # An undeclared kind takes the stricter rule; blanks never match.
        self.assertFalse(ids.exact_compare("no_such_kind", "ab", "AB"))
        self.assertFalse(ids.exact_compare("so_number", " ", " "))
        self.assertFalse(ids.exact_compare("so_number", None, "SO1"))
        # Customer PO (Portal normalizePoReference on both sides, customer-po.ts@40f7802).
        self.assertTrue(ids.exact_compare("customer_po", " po  4500 12", "PO 4500 12 "))
        self.assertFalse(ids.exact_compare("customer_po", "PO 450012", "PO 4500 12"))

    def test_a_customer_po_identity_is_scoped_by_its_customer(self):
        # Portal TASK-2401 (order-identity.ts@40f7802 resolveOrderByNumber): one PO
        # number used by two customers names two orders; with no customer, none.
        space, field = ids.identity_scope("customer_po", "public.app_sales_order",
                                          "po_reference")
        self.assertEqual(field, "customer_id")
        first = ids.identity_key("customer_po", "PO 4500", (space, "cust-A"))
        self.assertIsNotNone(first)
        self.assertEqual(first, ids.identity_key("customer_po", " po  4500 ", (space, "cust-A ")))
        self.assertNotEqual(first, ids.identity_key("customer_po", "PO 4500", (space, "cust-B")))
        for scope in (None, (space, None), (space, " "), (space, True), ("", "cust-A"),
                      ("cust-A",)):
            self.assertIsNone(ids.identity_key("customer_po", "PO 4500", scope), scope)
        # Two systems' customer ids are two spaces, never one identity.
        fm_space, _ = ids.identity_scope("customer_po", "public.order_entry", "custpocode")
        self.assertNotEqual(ids.identity_key("customer_po", "PO 4500", (fm_space, "7")),
                            ids.identity_key("customer_po", "PO 4500", (space, "7")))
        # BC scopes by its own customer number, a space of its own; a field no carrier
        # declares, and unscoped or unknown kinds, have none.
        bc_space, bc_field = ids.identity_scope("customer_po", "salesOrders",
                                                "externalDocumentNumber")
        self.assertEqual(bc_field, "customerNumber")
        self.assertNotIn(bc_space, (space, fm_space))
        self.assertIsNone(ids.identity_scope("customer_po", "salesOrders", "number"))
        self.assertEqual(ids.identity_key("so_number", " SO100201 "), ("SO100201",))
        self.assertIsNone(ids.identity_key("no_such_kind", "x", (space, "cust-A")))
        # A carrier's scope qualifiers are closed: an unknown one, or one without a scope
        # field, is refused.
        data = ids.schema()
        spec = data["kinds"]["customer_po"]
        for bad in ({"scope_within": "planet"}, {"scope_excludes": {"field": "status"}},
                    {"scope_field": None, "scope_within": "company"}):
            carriers = [{**spec["carriers"][0], **bad}, *spec["carriers"][1:]]
            broken = {**data, "kinds": {**data["kinds"], "customer_po": {
                **spec, "carriers": carriers}}}
            with self.subTest(bad=bad), self.assertRaises(ids.IdentifierSchemaError):
                ids._Compiled(broken)

    def test_a_kind_without_a_valid_exact_compare_is_refused(self):
        data = ids.schema()
        broken = {**data, "kinds": {**data["kinds"],
                                    "so_number": {**data["kinds"]["so_number"],
                                                  "exact_compare": "fuzzy"}}}
        with self.assertRaises(ids.IdentifierSchemaError):
            ids._Compiled(broken)


class KindFormatTests(unittest.TestCase):
    """roles-declared: a kind's value pattern is kind-specific (it rejects generic text
    shapes, injection_guard.kind_specific) or the kind declares that upstream has no
    fixed format, with the upstream code that shows it; never an invented pattern."""

    def test_every_kind_is_kind_specific_or_declares_no_fixed_format(self):
        from operating.injection_guard import kind_specific
        for kind in ids.kinds():
            free = ids.kind_spec(kind).get("no_fixed_format")
            with self.subTest(kind=kind):
                # A declaration on a kind-specific pattern would be stale.
                self.assertIsNot(kind_specific(kind), free is not None)
                if free is not None:
                    self.assertTrue(free["why"].strip())
                    # Upstream code at a commit, or the upstream search that found no
                    # writer; never a Workbench file standing in for upstream.
                    self.assertRegex(free["upstream"],
                                     r"^(plx-customer-portal@[0-9a-f]{7,40}:portal/|no upstream "
                                     r"writer found in plx-customer-portal@[0-9a-f]{7,40} "
                                     r"paths searched: )")
                    self.assertNotRegex(free["upstream"],
                                        r"(operating|ontology|integrations|verify|evals)/|"
                                        r"EVIDENCE/")
        self.assertIn("free text", ids.kind_spec("customer_po")["no_fixed_format"]["why"])
        # SalesOrder.poReference ("Customer's PO number"), not ManufacturingOrder's.
        self.assertIn("schema.prisma:3673",
                      ids.kind_spec("customer_po")["no_fixed_format"]["upstream"])

    def test_a_column_carries_a_kind_only_when_one_kind_declares_it(self):
        # The column-role declaration the action catalog reads for a structured column
        # the schema reads both as text and as an identifier (synthetic relations).
        self.assertEqual(ids.carrier_kind("public.app_sales_order", "po_reference"),
                         "customer_po")
        self.assertEqual(ids.carrier_kind("public.order_entry", "custpocode"), "customer_po")
        # Free text "PO Reference" on the MO (migrated from the FM OE number): no kind.
        self.assertIsNone(ids.carrier_kind("public.app_manufacturing_order", "po_reference"))
        # Two kinds declare it (customer_po, bc_external_document_no): ambiguous, none.
        self.assertIsNone(ids.carrier_kind("salesOrders", "externalDocumentNumber"))
        self.assertIsNone(ids.carrier_kind("public.app_sales_order", "notes"))

    def test_a_declared_customer_po_carrier_is_a_customer_po_column_in_the_schema(self):
        # A carrier the catalog takes a role from must be one the ontology schema binds
        # as that identifier kind (plus the raw text reading), never another role.
        from ontology.schema import load_schema
        schema = load_schema()
        bound: dict[tuple[str, str], set[str]] = {}
        for object_type in schema.object_types.values():
            for prop in object_type.get("properties") or ():
                role = prop.get("value_type")
                if role == "identifier":
                    role = "identifier:" + prop["identifier_kind"]
                for source in prop.get("sources") or ():
                    found = schema.bindings.get(source.get("binding"))
                    if found is not None:
                        bound.setdefault((found[1].get("relation"), source.get("path")),
                                         set()).add(role)
        checked = []
        for carrier in ids.carriers("customer_po"):
            column = (carrier["relation"], carrier["field"])
            if ids.carrier_kind(*column) is None or column not in bound:
                continue
            checked.append(column)
            with self.subTest(column=column):
                self.assertEqual(bound[column], {"text", "identifier:customer_po"})
        self.assertIn(("public.app_sales_order", "po_reference"), checked)

    def test_a_malformed_no_fixed_format_is_refused(self):
        data = ids.schema()
        for bad in ({"why": "free text"}, {"why": " ", "upstream": "x"}, "free text"):
            broken = {**data, "kinds": {**data["kinds"], "customer_po": {
                **data["kinds"]["customer_po"], "no_fixed_format": bad}}}
            with self.subTest(bad=bad), self.assertRaises(ids.IdentifierSchemaError):
                ids._Compiled(broken)

    def test_legacy_base36_po_is_the_operations_route_shape_only(self):
        # PO- plus Date.now() in base 36 (8 characters until 2059), prefixRef'd.
        for written in ("PO-MUT20W00", "uat-po-mut20w00", "PO-9FX003"):
            with self.subTest(written=written):
                self.assertTrue(ids.valid("petra_po", written))
                self.assertEqual(ids.parse("petra_po", written).part("code"),
                                 written.upper().rsplit("-", 1)[1])
        # No DPO- base-36 form upstream (the distribution series is numeric only), no
        # code over 8 characters, and a code always holds a digit.
        for written in ("DPO-WIRENOW9", "DPO-MUT20W00", "PO-MUT20W00123", "PO-WIRENOW",
                        "UAT-DPO-MUT20W00"):
            with self.subTest(written=written):
                self.assertIsNone(ids.parse("petra_po", written))
        self.assertEqual([m.text for m in ids.find_identifiers(
            "see PO-MUT20W00, DPO-WIRENOW9 and PO-MUT20W00123")], ["PO-MUT20W00"])

    def test_ao_and_batch_letter_suffixes_are_at_most_two_letters(self):
        # lot-alpha.ts alphaForIndex: A..Z, then AA..ZZ.
        for kind, good, bad in (("ao_code", "AO-26119AB", "AO-12345PAY"),
                                ("batch_code", "BC-26119AB", "BC-26119ABC"),
                                ("mo_code", "AO-26119AB", "AO-26119ABC")):
            with self.subTest(kind=kind):
                self.assertTrue(ids.valid(kind, good))
                self.assertTrue(ids.valid(kind, "UAT-" + good) or kind == "mo_code")
                self.assertFalse(ids.valid(kind, bad))
        self.assertEqual(ids.find_identifiers("AO-12345PAY or BC-26119ABC"), [])
        self.assertEqual(ids.document_form("bc_document_no", "PLX-AO-AO-26119ABC"), None)
        self.assertEqual([lead.kind for lead in ids.prefix_leads("AO-26119")], ["ao_code"])


class NormalizerTests(unittest.TestCase):
    def test_golden_forms_from_context_packs_02_and_03(self):
        for kind, written, normalized, key in GOLDEN:
            with self.subTest(kind=kind, written=written):
                self.assertEqual(ids.normalize(kind, written), normalized)
                self.assertEqual(ids.blocking_key(kind, written), key)

    def test_invalid_forms_do_not_parse(self):
        for kind, written in NOT_VALID:
            with self.subTest(kind=kind, written=written):
                self.assertIsNone(ids.parse(kind, written))
                self.assertIsNone(ids.normalize(kind, written))

    def test_spellings_of_one_po_share_a_blocking_key_and_other_pos_do_not(self):
        same = ["PO5447", "PO#5447", "PO 5447", "Our PO 5447", "P.O. # 5447", "PO-5447",
                "5447"]
        self.assertEqual({ids.blocking_key("petra_po", v) for v in same}, {"5447"})
        self.assertNotEqual(ids.blocking_key("petra_po", "PO5470"),
                            ids.blocking_key("petra_po", "PO5447"))
        self.assertNotEqual(ids.blocking_key("petra_po", "DPO100000"),
                            ids.blocking_key("petra_po", "PO100000"))
        self.assertEqual(ids.blocking_key("pi_ref", "PI-SWC025240"),
                         ids.blocking_key("pi_ref", "PI-25240"))
        self.assertNotEqual(ids.blocking_key("pi_ref", "PI-SWC024613"),
                            ids.blocking_key("pi_ref", "PI-25240"))

    def test_property_normalize_of_format_is_idempotent(self):
        values = [(kind, written) for kind, written, _n, _k in GOLDEN]
        for kind in ids.kinds():
            values += [(kind, form["example"]) for form in ids.kind_spec(kind)["forms"]]
        rng = random.Random(4190)
        for _ in range(400):
            series = rng.choice(["PO", "DPO", "po", "Po"])
            label = rng.choice(["", "#", "-", " ", " # ", ". "]) if series.upper() == "PO" else ""
            prefix = rng.choice(["", "UAT-", "Our "]) if not label.startswith(".") else ""
            number = rng.randrange(1, 10 ** rng.randrange(1, 8))
            zeros = "0" * rng.randrange(0, 3)
            fm = rng.choice(["", f"-FM{rng.randrange(1, 9999)}"])
            written = f"{prefix}{series}{label}{zeros}{number}{fm}"
            values.append(("petra_po", written))
            expected = (("UAT-" if prefix == "UAT-" else "") + series.upper() + str(number)
                        + fm)
            self.assertEqual(ids.normalize("petra_po", written), expected, written)
        for _ in range(100):
            values.append(("pi_ref", "PI" + rng.choice(["", "-"])
                           + "".join(rng.choice("SWCabc") for _ in range(rng.randrange(0, 4)))
                           + str(rng.randrange(100, 10 ** 8))))
            values.append(("fm_id", rng.choice([rng.randrange(0, 10 ** 9),
                                                f"{rng.randrange(0, 10 ** 6)}.0"])))
        for kind, written in values:
            with self.subTest(kind=kind, written=written):
                parsed = ids.parse(kind, written)
                self.assertIsNotNone(parsed)
                canonical = ids.format(parsed)
                self.assertEqual(ids.normalize(kind, canonical), canonical)
                self.assertEqual(ids.format(ids.parse(kind, canonical)), canonical)
                self.assertEqual(ids.normalize(kind, ids.normalize(kind, written)), canonical)

    def test_property_normalize_is_idempotent_for_every_kind_on_random_text(self):
        rng = random.Random(2671)
        alphabet = "AB0123456789 -#./@_:poPOcfmFMsuqi"
        for kind in ids.kinds():
            for _ in range(1500):
                written = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 24)))
                once = ids.normalize(kind, written)
                if once is not None:
                    self.assertEqual(ids.normalize(kind, once), once, (kind, written))
        # Separators no longer count towards the minimum length.
        self.assertIsNone(ids.normalize("tracking_number", "AB-12"))
        self.assertEqual(ids.normalize("tracking_number", "AB-123"), "AB123")

    def test_environment_prefix_is_a_diagnosis_never_a_normaliser(self):
        # uat-prefix.ts@c3ffda3: numbers a prefix-setting Portal database issues.
        self.assertEqual(ids.environment_prefix(" uat-SH100017"), "UAT")
        self.assertEqual(ids.environment_prefix("UAT-PO#5283"), "UAT")
        for value in ("SH100017", "UAT-", "UATSH1", None, 12):
            self.assertIsNone(ids.environment_prefix(value), value)
        # Two databases allocate one series independently: never merged.
        self.assertNotEqual(ids.normalize("shipment_number", "UAT-SH100017"),
                            ids.normalize("shipment_number", "SH100017"))
        self.assertNotEqual(ids.normalize("so_number", "UAT-SO100018"),
                            ids.normalize("so_number", "SO100018"))

    def test_environment_prefixed_kinds_take_the_declared_prefixes_from_one_list(self):
        prefixes = [entry["prefix"] for entry in ids.schema()["environment_prefixes"]]
        marked = [k for k in ids.kinds() if ids.kind_spec(k).get("environment_prefixed")]
        self.assertTrue({"shipment_number", "ao_code", "batch_code",
                         "receipt_lab_id"} <= set(marked))
        for kind in marked:
            with self.subTest(kind=kind):
                # No value_pattern writes a prefix itself: the list is the one source.
                for prefix in prefixes:
                    self.assertNotIn(prefix.rstrip("-"), ids.kind_spec(kind)["value_pattern"])
        self.assertEqual(ids.normalize("shipment_number", "uat-sh100017"), "UAT-SH100017")
        self.assertIsNone(ids.normalize("shipment_number", "QA-SH100017"))
        # A second declared prefix is accepted without touching any value_pattern.
        data = json.loads(json.dumps(ids.schema()))
        data["environment_prefixes"].append({"prefix": "QA-"})
        compiled = ids._Compiled(data)
        self.assertTrue(compiled.values["shipment_number"].fullmatch("QA-SH100017"))
        self.assertTrue(compiled.values["ao_code"].fullmatch("UAT-AO-12345C"))
        self.assertFalse(compiled.values["shipment_number"].fullmatch("PROD-SH100017"))

    def test_sent_form_follows_the_carriers_declared_rule(self):
        # serializers.ts@c3ffda3:149-161, 204-214: only a Sage-style supplier code (a
        # letter in it) becomes the BC vendor No., cut to 20 characters.
        form = ids.sent_form
        self.assertEqual(form("supplier_code", "public.app_supplier", "code", " EZCH01 ",
                              "bc"), "EZCH01")
        for held in ("225", 225, " 0042 ", "", None):
            self.assertIsNone(form("supplier_code", "public.app_supplier", "code", held,
                                   "bc"), held)
        self.assertEqual(form("supplier_code", "public.app_supplier", "code", "A" * 25,
                              "bc"), "A" * 20)
        self.assertEqual(form("customer_erp_code", "public.app_customer", "erp_code",
                              "C" * 22, "bc"), "C" * 20)
        # bcItemNumber (serializers.ts@82549dd:828-836): a product code reaches BC cut to
        # 20 characters; its own normalised form keeps the whole code.
        self.assertEqual(form("item_code", "public.app_product", "code",
                              " DVS-5003-3.78L-A-CASE4 ", "bc"), "DVS-5003-3.78L-A-CAS")
        # No rule for that system or carrier: the trimmed value as held.
        self.assertEqual(form("supplier_code", "public.app_supplier", "code", "225",
                              "sage"), "225")
        self.assertEqual(form("supplier_code", "vendors", "number", "225", "bc"), "225")

    def test_po_shapes_are_value_free_classes(self):
        self.assertEqual([ids.petra_po_shape(v) for v in
                          ("PO100523", "PO#5447", "5447", "PO#5300-FM77", "PO-K3J9X2",
                           "UAT-PO#5283", "hello")],
                         ["canonical_series", "legacy_number", "bare_digits", "fm_suffixed",
                          "base36", "uat", "other"])

    def test_fm_suffix_base(self):
        self.assertEqual(ids.fm_suffix_base("PO#5300-FM77"), "PO#5300")
        self.assertEqual(ids.fm_suffix_base("po#5300-fm77"), "po#5300")
        self.assertIsNone(ids.fm_suffix_base("PO#5300-FM"))
        self.assertIsNone(ids.fm_suffix_base("PO#5300-FM77\n"))
        self.assertIsNone(ids.fm_suffix_base("PO#5300"))


class ScannerTests(unittest.TestCase):
    TEXT = ("Customer PO PO12172025 for Our PO 5447 (PO#5300-FM77); PI No. PI-SWC024613, "
            "file PI-SWC025240; AO-26005B / BC-26119A; Quote: QUO-2026-0042; sales order: "
            "SO100123; shipped SH100000; lab RP0008270; customer fm-cust-89; CAPA-26004.")

    def test_find_identifiers_returns_spans_kinds_and_normalised_values(self):
        found = ids.find_identifiers(self.TEXT)
        for mention in found:
            self.assertEqual(self.TEXT[mention.start:mention.end], mention.text)
        got = [(m.kind, m.text, m.normalized, m.form) for m in found]
        self.assertEqual(got, [
            ("customer_po", "PO12172025", "PO12172025", "labelled"),
            ("petra_po", "5447", "PO5447", "labelled"),
            ("petra_po", "PO#5300-FM77", "PO5300-FM77", "shape"),
            ("pi_ref", "PI-SWC024613", "PI-SWC024613", "shape"),
            ("pi_ref", "PI-SWC025240", "PI-SWC025240", "shape"),
            ("ao_code", "AO-26005B", "AO-26005B", "shape"),
            ("batch_code", "BC-26119A", "BC-26119A", "shape"),
            ("quote_number", "QUO-2026-0042", "QUO-2026-0042", "labelled"),
            ("so_number", "SO100123", "SO100123", "labelled"),
            ("shipment_number", "SH100000", "SH100000", "shape"),
            ("receipt_lab_id", "RP0008270", "RP0008270", "shape"),
            ("fm_external_id", "fm-cust-89", "fm-cust-89", "shape"),
            ("eqms_number", "CAPA-26004", "CAPA-26004", "shape"),
        ])
        labelled = found[1]
        self.assertEqual((labelled.label_start, labelled.label), (self.TEXT.index("PO 5447"), "PO"))
        self.assertEqual(ids.find_identifiers(self.TEXT), found)  # deterministic

    def test_bare_numbers_and_po_boxes_are_not_identifiers(self):
        for text in ("Invoice 5447 dated 2026", "deposit 1234", "PO box 12", "REPO123",
                     "PO-Box", "SPO100123", "call 905 555 1234", "", None):
            with self.subTest(text=text):
                self.assertEqual([m for m in ids.find_identifiers(text)
                                  if m.kind == "petra_po"], [])

    def test_scan_work_grows_linearly_on_adversarial_text(self):
        """Counts the Python-level operations (value normalisations and mention
        constructions) per scan, so load cannot flake it. The regex engine's own
        steps are not countable; the grammar's patterns have no nested quantifiers."""
        from unittest import mock

        def work(n: int) -> int:
            samples = ["PO" + " " * n, "quote" + " " * n + "!", "P.O." * (n // 4),
                       "PO-" + "a" * n, "invoice no" + "\t" * n, "AO-" + "1" * n,
                       "PO#" + "1" * n + "-FM", "customer PO" + " " * n]
            calls = {"value": 0, "mention": 0}
            real_value, real_mention = ids._mention_value, ids.Mention

            def counting_value(*args):
                calls["value"] += 1
                return real_value(*args)

            def counting_mention(*args, **kwargs):
                calls["mention"] += 1
                return real_mention(*args, **kwargs)

            with mock.patch.object(ids, "_mention_value", counting_value), \
                    mock.patch.object(ids, "Mention", counting_mention):
                for sample in samples:
                    ids.find_identifiers(sample)
            return calls["value"] + calls["mention"]

        begin = time.perf_counter()
        small, large = work(5_000), work(40_000)
        self.assertLess(time.perf_counter() - begin, 5.0)  # catastrophic backtracking
        # 8x the input: linear work is at most ~8x; quadratic would be ~64x.
        self.assertLessEqual(large, max(small, 16) * 8)


class ProseRecordLabelTests(unittest.TestCase):
    """find-upstream-form-roots: a receipt's raw lab id (``YYDDD-N``) and a Business Central
    document No. (a free Code[20]) have no self-delimiting shape, so prose names them only
    behind a label; the label reads the value under the kind's own grammar."""

    QUESTION = "Which records across Portal, FileMaker, Sage and Business Central are linked to {}?"

    def read(self, text: str) -> list[tuple[str, str, str | None, str]]:
        return [(m.kind, m.text, m.normalized, m.form) for m in ids.find_identifiers(text)]

    def test_a_receipt_lab_id_is_read_behind_a_receipt_label(self):
        for text, value in ((self.QUESTION.format("receipt 98106-1"), "98106-1"),
                            ("Receipt no. 26161-12 arrived", "26161-12"),
                            ("receipt lab id: uat-26161-1", "UAT-26161-1"),
                            ("lab id # RP0008270.", "RP0008270")):
            with self.subTest(text=text):
                [(kind, written, normalized, form)] = self.read(text)
                self.assertEqual((kind, normalized, form), ("receipt_lab_id", value, "labelled"))
                self.assertEqual(written.upper(), value)
        # Not a lab id: no digits in the grammar's form, a four-digit day, a five-digit
        # sequence, a plural, and a bare "lab" still reads the RP shape only.
        for text in ("the receipt for 12 cases", "receipt 2616-1", "receipt 26161-12345",
                     "receipts 26161-1", "lab 26161-1"):
            with self.subTest(text=text):
                self.assertEqual(self.read(text), [])
        self.assertEqual(self.read("lab RP0008270"),
                         [("receipt_lab_id", "RP0008270", "RP0008270", "shape")])

    def test_a_bc_document_no_is_read_behind_a_business_central_label(self):
        for text, value in ((self.QUESTION.format("Business Central document FXSI300009"),
                             "FXSI300009"),
                            ("BC document No. si-1002, thanks", "SI-1002"),
                            ("bc no: PLX-AO-AO-26197B", "PLX-AO-AO-26197B")):
            with self.subTest(text=text):
                [(kind, _written, normalized, form)] = self.read(text)
                self.assertEqual((kind, normalized, form), ("bc_document_no", value, "labelled"))
        # A Code[20] holds at most 20 characters; a label value holds a digit; the
        # batch-code shape BC-... is not a label.
        for text in ("Business Central is down", "BC document ABCDEFGHIJKLMNOPQRSTU1",
                     "BC document INVOICE", "Business Central document"):
            with self.subTest(text=text):
                self.assertEqual(self.read(text), [])
        self.assertEqual(self.read("BC-26119A"),
                         [("batch_code", "BC-26119A", "BC-26119A", "shape")])

    def test_the_labels_scan_in_linear_work(self):
        for label in ("receipt", "receipt lab id", "BC document", "Business Central no"):
            text = (label + " " * 20_000 + ":") * 4
            begin = time.perf_counter()
            self.assertEqual(ids.find_identifiers(text), [])
            self.assertLess(time.perf_counter() - begin, 2.0, label)

    def test_a_bc_sales_invoice_number_is_an_identifier_like_a_purchase_invoice_number(self):
        from ontology.schema import load_schema
        schema = load_schema()

        def bc_number(object_type: str) -> dict:
            return next(p for p in schema.object_types[object_type]["properties"]
                        if p["name"] == "bc_number")

        sales, purchase = bc_number("SalesInvoice"), bc_number("PurchaseInvoice")
        for prop in (sales, purchase):
            self.assertEqual((prop["value_type"], prop["identifier_kind"]),
                             ("identifier", "bc_document_no"))
        self.assertEqual(sales["sources"], [{"binding": "bc.sales_invoices", "path": "number",
                                             "parser": "identifier"}])
        self.assertEqual(schema.bindings["bc.sales_invoices"][1]["relation"], "salesInvoices")


class FixtureIdentifierFormTests(unittest.TestCase):
    """The eval fixture world writes every identifier in an upstream form: each value on a
    declared carrier parses as its kind, so the projector never records it
    ``identifier_unparsed`` (docs/decisions/2026-10-04-po-fixture-roots.md: fix the
    fixture, never loosen the grammar). The one exception is declared there: a PO
    renumbered to a code no grammar reads."""

    # FXBC9001 is a PO renumbered to a BC No. (well-formed, declined by the PO grammar). The
    # customer erp_code "FX B&C" is the world's one declared malformed value: the object_view
    # unparsed-denominator case (tests/test_object_workspace_http.py), never a question root.
    DECLARED_UNPARSED = {("petra_po", "public.app_purchase_order", "po_number"),
                         ("petra_po", "public.app_purchase_order", "custom_fields.renumberedTo"),
                         ("customer_erp_code", "public.app_customer", "erp_code")}

    @staticmethod
    def field(row, path: str):
        for part in path.split("."):
            if not isinstance(row, dict):
                return None
            row = row.get(part)
        return row

    def test_every_carrier_value_of_the_eval_world_parses_as_its_kind(self):
        from evals.oracle.fixture_world import build_eval_world
        world = build_eval_world()
        unparsed: dict[tuple[str, str, str], set[str]] = {}
        checked: dict[str, int] = {}
        for kind in ids.kinds():
            for carrier in ids.carriers(kind):
                if carrier.get("role") == "mention":
                    continue
                for relation in carrier["relation"].split("|"):
                    rows = (world.bc if carrier["system"] == "bc" else world.relations
                            ).get(relation) or ()
                    for path in carrier["field"].split("|"):
                        for row in rows:
                            # A BC record keeps its API fields under "fields" (bc_record).
                            value = self.field(row["fields"] if carrier["system"] == "bc"
                                               else row, path)
                            if value is None or value == "":
                                continue
                            checked[kind] = checked.get(kind, 0) + 1
                            if ids.normalize(kind, value) is None:
                                unparsed.setdefault((kind, relation, path), set()).add(str(value))
        self.assertEqual(set(unparsed), self.DECLARED_UNPARSED, unparsed)
        self.assertEqual({v for values in unparsed.values() for v in values},
                         {"FXBC9001", "FX B&C"})
        for kind in ("receipt_lab_id", "ao_code", "mo_code", "shipment_number",
                     "sage_ord_uniq", "so_number", "bc_document_no"):
            self.assertGreater(checked.get(kind, 0), 0, kind)   # not vacuous

    def test_a_sage_backed_sales_order_carries_its_source_orduniq(self):
        from evals.oracle.fixture_world import build_eval_world
        sage = [row["custom_fields"] for row in build_eval_world().relations[
            "public.app_sales_order"] if "sageOrdUniq" in (row.get("custom_fields") or {})]
        self.assertTrue(sage)
        for custom in sage:
            self.assertEqual(custom.get("sageSourceOrdUniq"), custom["sageOrdUniq"], custom)
        self.assertIn("custom_fields.sageSourceOrdUniq",
                      {c["field"] for c in ids.carriers("sage_ord_uniq")})


class SystemGrammarTests(unittest.TestCase):
    def test_system_identifiers(self):
        cases = {
            "guid": (["BBBBBBBB-0000-0000-0000-000000000009"], ["bbbb", "x" * 36]),
            "upn": (["vince@petralabx.com", "a.b+c@x.y.com"], ["a@b", "a..b@x.com", "@x.com"]),
            "bc_number_literal": (["PO100523", "PO 1", "A/B#C-1.2_3"],
                                  ["PO  1", "PO1 ", " PO1", "PO'1", "x" * 36]),
            "dns_domain": (["acme-bottles.com"], ["localhost", "Acme.com"]),
        }
        for name, (good, bad) in cases.items():
            for value in good:
                self.assertTrue(ids.valid(name, value), (name, value))
            for value in bad:
                self.assertFalse(ids.valid(name, value), (name, value))
        self.assertFalse(ids.valid("guid", None))

    def test_sharepoint_host_and_site_name_match_the_moved_regexes(self):
        old = {"sharepoint_host": re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.sharepoint\.com$"),
               "sharepoint_site_name": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")}
        rng = random.Random(7)
        samples = ["petra.sharepoint.com", "petra.sharepoint.com\n", "QualityQC", "a",
                   "-x", "x" * 129, "Petra.sharepoint.com", "a.b.sharepoint.com"]
        samples += ["".join(rng.choice("aZ09-_.\n ") for _ in range(rng.randrange(0, 20)))
                    + rng.choice(["", ".sharepoint.com"]) for _ in range(3000)]
        for name, pattern in old.items():
            for value in samples:
                self.assertEqual(ids.valid(name, value), pattern.fullmatch(value) is not None,
                                 (name, value))

    def test_bc_number_literal_accepts_exactly_what_the_old_two_regex_check_did(self):
        old = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/#-]| (?! )){0,34}$")
        tail = re.compile(r"[A-Za-z0-9._/#-]$")
        rng = random.Random(20)
        for _ in range(5000):
            value = "".join(rng.choice("A1 ./#-_'\n") for _ in range(rng.randrange(0, 38)))
            expected = old.fullmatch(value) is not None and tail.search(value) is not None
            self.assertEqual(ids.valid("bc_number_literal", value), expected, repr(value))


# The four modules that used to own private identifier grammars (pre-move regex
# literals copied here verbatim). The moved grammars must find the
# same matches on the same text; the rewrites only remove ambiguous adjacent
# whitespace quantifiers (linear-time, same language).
_OLD = {
    "petra_po_attached": [(r"(?<![A-Za-z0-9#-])((?:UAT-)?D?PO(?:#|-)?\d{1,9}(?:-FM\d{1,9})?"
                           r"|D?PO-(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{3,16})(?![A-Za-z0-9])", re.I)],
    "petra_po_labelled": [(r"(?<![A-Za-z0-9])(?:P\.?\s?O\.?|purchase\s+order)(?:\s*(?:no\.?|number|num\.?|#))?"
                           r"\s*[:#.]?\s*(\d{3,10}(?:-FM\d{1,9})?)(?![A-Za-z0-9])", re.I)],
    "supplier_reference": [(r"(?<![A-Za-z0-9])(?:our\s+)?(sales\s+order|order|S/?O|confirmation|conf\.?|"
                            r"proforma(?:\s+invoice)?|PI|invoice|reference|ref\.?)\s*(?:no\.?|number|#)\s*[:#]?\s*"
                            r"([A-Z0-9][A-Z0-9/-]{2,30})(?![A-Za-z0-9])", re.I)],
    "pi_attached": [(r"(?<![A-Za-z0-9])(PI-?[A-Z]{0,5}\d{3,12})(?![A-Za-z0-9])", re.I)],
    "mail_labelled": [
        (r"\b(?:quote|quotation)\s*(?:number|no\.?)?\s*(?:[:#]\s*)?([A-Z0-9][A-Z0-9._/-]{1,79})", re.I),
        (r"\bcustomer\s+(?:PO|purchase\s+order)\s*(?:number\s*)?(?:[:#]\s*)?([A-Z0-9][A-Z0-9._/-]{1,79})", re.I),
        (r"\bPetra\s+(?:PO|purchase\s+order)\s*(?:number\s*)?(?:[:#]\s*)?([A-Z0-9][A-Z0-9._/-]{1,79})", re.I),
        (r"\b(?:vendor|supplier)\s+confirmation\s*(?:number\s*)?(?:[:#]\s*)?([A-Z0-9][A-Z0-9._/-]{1,79})", re.I),
        (r"\bshipment\s+(?:reference|ref)\s*(?:number\s*)?(?:[:#]\s*)?([A-Z0-9][A-Z0-9._/-]{1,79})", re.I),
        (r"\binvoice\s+(?:number|no\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9._/-]{1,79})", re.I),
        (r"\bsales\s+order\s*(?:number\s*)?(?:[:#]\s*)?([A-Z0-9][A-Z0-9._/-]{1,79})", re.I),
    ],
}
# Labelled patterns added to a moved profile since the move (find-upstream-form-roots: a
# receipt lab id and a BC document No. written in prose), appended after the moved ones.
_ADDED_SINCE_MOVE = {"mail_labelled": ("receipt_lab_id", "bc_document_no")}
_CORPUS = [
    "PO100123, DPO100002, PO#5280, UAT-PO#5283, PO#5300-FM77, PO-K3J9X2, P.O. # 5447 and "
    "Purchase Order No. 106089", "Your PO 5447", "Confirmed PO-5447, thanks.",
    "PO  #  5447", "po.5447", "P O 5447", "PO no. 5447", "purchase order : 5447-FM3",
    "Our order no. SO-88812, ship 25 Oct.", "Ref # AB-123/4", "proforma invoice no: PI-77812",
    "conf. no 99812", "PI-SWC024613.pdf PI23219 PI 2",
    "Quote number: QT-1 quotation no 22 quote : X9", "customer PO number 45-12",
    "Petra purchase order # PO5447", "vendor confirmation VC12", "shipment ref 77/1",
    "invoice no. INV-7 invoice number : 8", "sales order SO100023", "quote\n\n  \t 7",
    "invoice no" + " " * 50 + "5", "PO-" + "a" * 20 + "1", "PO-abcdefghijklmno1 x",
    "DPO-MUT20W00, PO-MUT20W00, DPO-12345678901 and po-mut20w00x",
]
# The one intended change since the move (roles-declared): the base-36 branch of
# petra_po_attached is the operations-route shape only (PO- plus at most 8 base-36
# characters, operations/purchase-orders/route.ts:33): no DPO- form, no longer code.
_NUMERIC_PO = re.compile(r"(?:UAT-)?D?PO(?:#|-)?\d{1,9}(?:-FM\d{1,9})?", re.I)


def _narrowed_base36(profile: str, match: re.Match[str]) -> bool:
    found = re.fullmatch(r"(D?)PO-([A-Za-z0-9]+)", match.group(1), re.I)
    return (profile == "petra_po_attached" and found is not None
            and _NUMERIC_PO.fullmatch(match.group(1)) is None
            and (bool(found.group(1)) or len(found.group(2)) > 8))


class MovedGrammarEquivalenceTests(unittest.TestCase):
    def test_moved_patterns_find_the_same_matches_as_before(self):
        dropped = []
        for name, olds in _OLD.items():
            news = ids.profile_patterns(name)
            # Patterns added after the move come last, by kind; the moved ones are unchanged.
            added = _ADDED_SINCE_MOVE.get(name, ())
            self.assertEqual([kind for kind, _ in news[len(olds):]], list(added), name)
            news = news[:len(olds)]
            self.assertEqual(len(news), len(olds), name)
            for (pattern, flags), (_kind, new) in zip(olds, news):
                old = re.compile(pattern, flags)
                for text in _CORPUS:
                    kept = [m for m in old.finditer(text) if not _narrowed_base36(name, m)]
                    dropped += [m.group(1) for m in old.finditer(text)
                                if _narrowed_base36(name, m)]
                    with self.subTest(profile=name, text=text):
                        self.assertEqual([(m.span(), m.groups()) for m in kept],
                                         [(m.span(), m.groups()) for m in new.finditer(text)])
        # The intended difference is exercised, and only base-36 PO forms are dropped.
        self.assertEqual(sorted(set(dropped)), ["DPO-12345678901", "DPO-MUT20W00",
                                                "PO-abcdefghijklmno1", "po-mut20w00x"])


# Module-level and inline regexes allowed to remain in these modules: none of
# them is an identifier grammar (keyword spans, quoted-history structure, amounts,
# dates, parties, request-template compilation, path characters).
_ALLOWED_REGEX_NAMES = {
    # Quoted-history structure moved to the mentions projection with quoted_ranges.
    "ontology/mentions.py": {"_HISTORY_START", "_QUOTED_LINE", "_BOTTOM_POST",
                             "<quoted_ranges>"},
    "integrations/document_understanding.py": {
        "_QUANTITY", "_PRICE", "_CURRENCY_DECLARATION", "_ATTACHMENT_SECTION", "_DATE",
        "_PARTY", "_MAIL_PARTY", "_MAIL_DATE", "_SPEC",
        # OCR tool output lines and the word-box layout mark line, PDF page sizes, pptx
        # markdown escapes, attachment marker sanitisation; written numbers and their
        # thousands groups.
        "<_only_line>", "_LAYOUT_LINE", "<_portable_pdf_text>", "<_converted_claims>", "<extract_document>",
        "NUMBER_TOKEN", "<_grouped>"},
    "integrations/graph_evidence.py": set(),
    # _INSTANT is an ISO-instant time format and _VERSION_ID a SharePoint file version
    # label (major.minor), not identifier grammars.
    "integrations/live_source_grammar.py": {"_DRIVE_SEGMENT_BAD", "_FIELD", "_INSTANT",
                                            "_VERSION_ID", "<_compile>"},
}
_RE_CALLS = {"compile", "search", "match", "fullmatch", "finditer", "findall", "sub", "subn",
             "split"}


def _regex_sites(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites: set[str] = set()
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and isinstance(inner.func.value, ast.Name) and inner.func.value.id == "re"
                    and inner.func.attr in _RE_CALLS):
                if targets:
                    sites.update(targets)
                elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    sites.add(f"<{node.name}>")
                else:
                    sites.add("<module>")
    return sites


class NoPrivateIdentifierGrammarTests(unittest.TestCase):
    def test_the_four_modules_keep_no_identifier_regex(self):
        for relative, allowed in _ALLOWED_REGEX_NAMES.items():
            with self.subTest(module=relative):
                sites = _regex_sites(ROOT / relative)
                self.assertTrue(sites <= allowed, sorted(sites - allowed))

    def test_the_modules_use_the_shared_grammar(self):
        from ontology import mentions

        # PO mentions come from the mentions projection, which scans with the one
        # grammar (ontology.mentions.extract -> find_identifiers).
        text = "Our PO 4821, PO#5280 and Customer PO: PO100234"
        petra = []
        for m in ids.find_identifiers(text):
            petra += [r for r in (m, *m.secondary) if r.kind == "petra_po"]
        tokens = mentions.extract(text, kinds=["petra_po"])
        self.assertEqual([(t["start"], t["end"]) for t in tokens],
                         [(m.start, m.end) for m in petra])
        # The PO shape under a customer-PO label is still a mention, flagged.
        self.assertEqual([t["label_kind_conflict"] for t in tokens], [False, False, True])

    def test_a_shape_under_another_kinds_label_is_a_secondary_reading(self):
        for text, label_kind in (("Customer PO: PO100234", "customer_po"),
                                 ("Customer PO Number: PO100234", "customer_po"),
                                 ("Customer PO # PO100234", "customer_po"),
                                 ("Customer PO DPO100234", "customer_po"),
                                 ("Quote: PO100234", "quote_number")):
            with self.subTest(text=text):
                [found] = ids.find_identifiers(text)
                self.assertEqual(found.kind, label_kind)
                self.assertFalse(found.label_kind_conflict)
                [secondary] = found.secondary
                self.assertEqual(secondary.kind, "petra_po")
                self.assertTrue(secondary.label_kind_conflict)
                self.assertEqual(text[secondary.start:secondary.end], secondary.text)
                self.assertEqual(secondary.normalized, ids.normalize("petra_po", secondary.text))
        # No conflict: a label of the same kind, or no label at all.
        for text in ("Petra PO: PO100234", "PO100234", "Our PO 4821"):
            with self.subTest(text=text):
                self.assertTrue(all(not m.secondary for m in ids.find_identifiers(text)))

    def test_a_bare_number_under_another_kinds_label_keeps_its_po_reading(self):
        """Review men-2: ``Customer PO: 5280`` also reads ``PO: 5280``, a form the
        Petra PO grammar accepts (identifiers.json petra_po legacy_fm/mail_labelled)."""
        for text, label_kind, value in (("Customer PO: 5280", "customer_po", "5280"),
                                        ("Customer PO Number 106089", "customer_po", "106089"),
                                        ("Customer PO # 5280-FM7", "customer_po", "5280-FM7")):
            with self.subTest(text=text):
                [found] = ids.find_identifiers(text)
                self.assertEqual((found.kind, found.text), (label_kind, value))
                [secondary] = found.secondary
                self.assertEqual((secondary.kind, secondary.form, secondary.text),
                                 ("petra_po", "labelled", value))
                self.assertTrue(secondary.label_kind_conflict)
                self.assertEqual(secondary.normalized,
                                 ids.normalize("petra_po", text[secondary.label_start:
                                                                secondary.end]))
        # Not when the PO grammar rejects the number, nor without a PO label.
        for text in ("Customer PO: 12", "Quote: 5280", "Customer PO: AB-77X"):
            with self.subTest(text=text):
                self.assertTrue(all(not m.secondary for m in ids.find_identifiers(text)))
        # One reading per kind and value: the shape, not also its "PO" + digits split.
        [found] = ids.find_identifiers("Customer PO: PO100234")
        self.assertEqual([(m.form, m.text) for m in found.secondary], [("shape", "PO100234")])

    def test_secondary_readings_scale_linearly_with_the_mentions(self):
        """Review men-2: 4000 'Customer PO: PO...' lines took 3.16 s (each labelled
        mention scanned every skipped one). Counts the overlap-scan steps instead of
        timing them."""
        from unittest import mock

        class CountingList(list):
            reads = 0

            def __getitem__(self, index):
                CountingList.reads += 1
                return super().__getitem__(index)

            def __iter__(self):
                for item in super().__iter__():
                    CountingList.reads += 1
                    yield item

        real = ids._with_secondary

        def counting(accepted, skipped):
            return real(CountingList(accepted), skipped)

        def steps(n: int) -> int:
            text = "\n".join(f"Customer PO: PO{100000 + i}" for i in range(n))
            CountingList.reads = 0
            with mock.patch.object(ids, "_with_secondary", counting):
                found = ids.find_identifiers(text)
            self.assertEqual(sum(len(m.secondary) for m in found), n)
            return CountingList.reads

        small, large = steps(1000), steps(4000)
        self.assertLessEqual(large, small * 5 + 8)  # linear; the quadratic scan was ~16x


class UnsuffixedAoTests(unittest.TestCase):
    """grammar-ao-forms: the Portal writes AO-<digits> without a letter only from an
    assembly_order row id (fp-bridge.ts:890 lot fallback, production-cost-sync.ts:269
    BC document), never as an AO name, so ao_code keeps its letter suffix."""

    def test_the_row_id_forms_are_recognised_under_their_own_kinds(self):
        self.assertEqual(ids.normalize("lot_number", "AO-9035@A05-A01"), "AO-9035@A05-A01")
        self.assertEqual(ids.blocking_key("lot_number", "AO-9035@A05-A01"), "AO-9035")
        self.assertEqual(ids.normalize("bc_document_no", "plx-ao-9035"), "PLX-AO-9035")
        forms = {kind: {f["form"]: f["example"] for f in ids.kind_spec(kind)["forms"]}
                 for kind in ("lot_number", "bc_document_no")}
        self.assertEqual(forms["lot_number"]["fp_bridge_ao_row_id"], "AO-9035")
        self.assertEqual(forms["bc_document_no"]["production_cost_ao"], "PLX-AO-9035")
        self.assertIn("never match", ids.kind_spec("ao_code")["description"])

    def test_an_unsuffixed_code_is_never_an_ao_code(self):
        for written in ("AO-26006", "ao-26006", "AO-12345", "AO-9035", "AO-0001"):
            with self.subTest(written=written):
                self.assertIsNone(ids.parse("ao_code", written))
                self.assertIsNone(ids.blocking_key("ao_code", written))
                self.assertFalse(ids.exact_compare("ao_code", "AO-26006B", written))
        for form in ids.kind_spec("ao_code")["forms"]:
            self.assertRegex(form["example"], r"^AO-\d{5}[A-Z]+$")

    def test_an_unsuffixed_code_never_reaches_a_suffixed_ao(self):
        self.assertEqual([(m.kind, m.text) for m in
                          ids.find_identifiers("lot AO-26006 vs AO-26006B")],
                         [("ao_code", "AO-26006B")])
        self.assertEqual(ids.find_identifiers("FG lot AO-12345@FL1"), [])
        keys = ids.search_keys("lot_number", "AO-26006", "AO-26006")
        self.assertNotIn("ao_code", [kind for kind, _ in keys])
        self.assertNotIn("AO-26006B", [value for _, value in keys])
        self.assertNotIn("lot_number", ids.alias_kinds("ao_code"))
        self.assertNotIn("ao_code", ids.alias_kinds("lot_number"))



class LeadGrammarTests(unittest.TestCase):
    """grammar-2: series-less stored PO numbers and their same-number twins, the AO day
    prefix and the BC document forms the Portal writes for assembly orders. Each is a
    lead to records, never one record's identity."""

    def test_a_bare_stored_number_is_series_less_never_the_po_series(self):
        parsed = ids.parse("petra_po", " 04821 ")
        self.assertEqual((parsed.part("series"), parsed.part("number")), ("", "4821"))
        self.assertEqual(ids.normalize("petra_po", "4821-FM12"), "4821-FM12")
        self.assertNotEqual(ids.normalize("petra_po", "4821"), ids.normalize("petra_po", "PO4821"))
        self.assertFalse(ids.exact_compare("petra_po", "4821", "PO4821"))
        self.assertEqual(ids.petra_po_shape("4821"), "bare_digits")

    def test_same_number_keeps_series_and_uat_and_a_bare_number_matches_any_series(self):
        for first, second in (("PO#4821", "4821"), ("PO-04821-FM9", "PO 4821"),
                              ("4821-FM3", "DPO4821"), ("UAT-PO#4821", "UAT-PO4821")):
            with self.subTest(first=first, second=second):
                self.assertTrue(ids.same_number("petra_po", first, second))
                self.assertTrue(ids.same_number("petra_po", second, first))
        for first, second in (("DPO4821", "PO4821"), ("UAT-PO4821", "4821"),
                              ("PO4821", "PO48210"), ("PO-K3J9X2", "PO-K3J9X2"),
                              ("PO4821", None)):
            with self.subTest(first=first, second=second):
                self.assertFalse(ids.same_number("petra_po", first, second))
        self.assertIsNone(ids.same_number_key("ao_code", "AO-26042A"))  # no rule declared

    def test_a_day_prefix_is_a_lead_to_both_spellings_of_the_day_never_an_ao(self):
        (lead,) = ids.prefix_leads(" ao-26042 ")
        self.assertEqual((lead.kind, lead.form, lead.prefixes),
                         ("ao_code", "day_prefix", ("AO-26042", "AO-2642")))
        self.assertIsNone(ids.parse("ao_code", "AO-26042"))
        for stored, led in (("AO-26042A", True), ("AO-2642B", True), ("ao-26042ab", True),
                            ("AO-26042", False), ("AO-260421A", False), ("AO-26043A", False),
                            ("AO-26042ABC", False), ("UAT-AO-26042A", False), (None, False)):
            with self.subTest(stored=stored):
                self.assertEqual(lead.matches(stored), led)
        (day4,) = ids.prefix_leads("AO-264")
        self.assertEqual(day4.prefixes, ("AO-264", "AO-26004"))
        self.assertFalse(day4.matches("AO-2642A"))         # day 42, not day 4 + "2A"
        (uat,) = ids.prefix_leads("UAT-AO-26042")
        self.assertEqual(uat.prefixes, ("UAT-AO-26042", "UAT-AO-2642"))
        self.assertTrue(uat.matches("UAT-AO-2642C") and not uat.matches("AO-2642C"))
        for value in ("AO-26042A", "AO-26000", "AO-26367", "AO-12", "PO-26042", "", None):
            with self.subTest(value=value):
                self.assertEqual(ids.prefix_leads(value), ())
        form = ids.kind_spec("ao_code")["prefix_forms"][0]
        self.assertTrue(ids.prefix_leads(form["example"]))
        self.assertIn("assembly-order-number.ts", form["upstream"])

    def test_each_bc_assembly_order_form_reads_its_kind_with_upstream_evidence(self):
        expected = {
            "PLX-AO-AO-26197B": ("production_cost_ao_code", "ao_code", "AO-26197B",
                                 "production-cost-sync.ts:269"),
            "plx-ao-uat-ao-26197b": ("production_cost_ao_code", "ao_code", "UAT-AO-26197B",
                                     "production-cost-sync.ts:269"),
            "PLX-AO-2026-10-04": ("production_cost_date", None, "2026-10-04",
                                  "production-cost.ts:265-271"),
            "PLX-AO-9035": ("production_cost_ao", "fm_id", "9035",
                            "production-cost-sync.ts:268-269"),
            "PLX-FMAO-9035": ("fm_material_relief_ao", "fm_id", "9035",
                              "fm-material-relief-sync.ts:308"),
        }
        forms = {f["form"]: f for f in ids.kind_spec("bc_document_no")["forms"]}
        for written, (form, kind, value, cited) in expected.items():
            with self.subTest(written=written):
                found = ids.document_form("bc_document_no", written)
                self.assertEqual(found.form, form)
                ((_part, named, object_type, read),) = found.reads
                self.assertEqual((named, read), (kind, value))
                self.assertEqual(object_type, "AssemblyOrder" if kind else None)
                upstream = forms[form]["upstream"]
                self.assertTrue(upstream.startswith("plx-customer-portal@6e76a5b:"), upstream)
                self.assertIn(cited, upstream)
                self.assertTrue(ids.document_form("bc_document_no", forms[form]["example"]))
        for written in ("PO100523", "PLX-AO-", "PLX-BAT-12", "PLX-FMAO-AO-26197B"):
            with self.subTest(written=written):
                self.assertIsNone(ids.document_form("bc_document_no", written))
        # The AO a document names is a lead: the document number never parses as it.
        self.assertIsNone(ids.parse("ao_code", "PLX-AO-AO-26197B"))


if __name__ == "__main__":
    unittest.main()
