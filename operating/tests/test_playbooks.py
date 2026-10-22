import copy
import json
import re
from pathlib import Path
import tempfile
import unittest

from operating import mcp_server, playbooks
from operating.tests import test_playbook_eval as tpe
from operating.playbooks import (Playbook, PlaybookAuthorityError, PlaybookIntegrityError,
                                 PlaybookRegistry,
                                 PlaybookRegistryError, PlaybookSchemaError, PlaybookToolError,
                                 definition_sha256, diff, load_playbook, parse_playbook,
                                 validate)

DIR = Path(playbooks.__file__).with_name("playbooks")
# The published v1/v2 AO definitions name a real case in their key hints: evaluation data,
# kept byte-identical as test fixtures, never shipped in the product directory.
RETIRED_DIR = Path(__file__).with_name("fixtures") / "playbooks"
AO = json.loads((RETIRED_DIR / "ao_quantity_reconciliation.json").read_text())
AO_V2 = json.loads((RETIRED_DIR / "ao_quantity_reconciliation_v2.json").read_text())
PO = json.loads((DIR / "customer_po_enrichment.json").read_text())
AO_V3 = json.loads((DIR / "ao_quantity_reconciliation_v3.json").read_text())
PO_V2 = json.loads((DIR / "customer_po_enrichment_v2.json").read_text())
# The current versions (the ones above name host tools retired by wire-host, and
# plx.source_citation / plx.case_verify_proposal merged by host-budget).
AO_V4 = json.loads((DIR / "ao_quantity_reconciliation_v4.json").read_text())
PO_V3 = json.loads((DIR / "customer_po_enrichment_v3.json").read_text())


def mutated(base, fn):
    value = copy.deepcopy(base)
    fn(value)
    return value


RETIRED = ("ao_quantity_reconciliation", "ao_quantity_reconciliation_v2")


class ExamplesTests(unittest.TestCase):
    def test_both_examples_validate_with_hash(self):
        for name in ("ao_quantity_reconciliation", "ao_quantity_reconciliation_v2",
                     "ao_quantity_reconciliation_v3", "ao_quantity_reconciliation_v4",
                     "customer_po_enrichment", "customer_po_enrichment_v2",
                     "customer_po_enrichment_v3"):
            pb = load_playbook((RETIRED_DIR if name in RETIRED else DIR) / f"{name}.json")
            self.assertEqual(pb.id, re.sub(r"_v[0-9]+$", "", name))
            self.assertEqual(len(pb.definition_sha256), 64)

    def test_examples_write_declarations_match_tool_annotations(self):
        by_name = {t["name"]: t for t in mcp_server.TOOLS}
        for doc in (AO, AO_V2, AO_V3, PO, PO_V2):  # published versions load, as retired
            self.assertTrue(Playbook(doc).retired)
        for doc in (AO_V4, PO_V3):
            self.assertFalse(Playbook(doc).retired)
            writes = {n for n in doc["allowed_tools"]
                      if not by_name[n]["annotations"]["readOnlyHint"]}
            self.assertEqual(writes, set(doc["writes_case_state"]))
            self.assertTrue(set(doc["allowed_tools"]) <= mcp_server.CASE_RUNTIME_TOOLS)

    def test_ao_computations_match_the_reference_investigation(self):
        # v2 scores the reference investigation (tests); v1 the frozen v1-shaped one.
        from operating.tests.test_ao_26005b_case import EXPECTED
        from operating.tests.ao_v1_investigation import EXPECTED as EXPECTED_V1
        self.assertEqual({c["name"] for c in AO_V2["required_computations"]}, set(EXPECTED))
        self.assertEqual({c["name"] for c in AO["required_computations"]}, set(EXPECTED_V1))

    def test_registry_loads_shipped_directory(self):
        reg = PlaybookRegistry().load_directory()
        self.assertEqual(len(reg), 5)
        self.assertEqual(reg.get("customer_po_enrichment").version, 3)
        self.assertEqual(reg.get("ao_quantity_reconciliation").version, 4)
        self.assertEqual([item["retired"] for item in reg.listing()],
                         [True, False, True, True, False])
        # No shipped playbook names a real case; the published v1/v2 load from fixtures.
        with self.assertRaises(PlaybookRegistryError):
            reg.get("ao_quantity_reconciliation", 1)
        reg.load_directory(RETIRED_DIR)
        self.assertEqual(reg.get("ao_quantity_reconciliation", 1).definition["version"], 1)
        self.assertEqual(reg.get("ao_quantity_reconciliation", 2).definition_sha256,
                         "0906cc3a1e5571b7d4ca2ce74c9a57d261e9fd6d82b14a9cf802b0ec40c83bb4")
        self.assertEqual(reg.get("ao_quantity_reconciliation").version, 4)


class RefusalTests(unittest.TestCase):
    def refuses(self, exc, fn, base=AO, code=None):
        with self.assertRaises(exc) as ctx:
            validate(mutated(base, fn))
        if code:
            self.assertEqual(ctx.exception.code, code)

    def test_unknown_keys_everywhere(self):
        self.refuses(PlaybookSchemaError, lambda d: d.update(extra=1), code="unknown_key")
        self.refuses(PlaybookSchemaError, lambda d: d["required_evidence"][0].update(x=1),
                     code="unknown_key")
        self.refuses(PlaybookSchemaError, lambda d: d["authority"].update(x=1), code="unknown_key")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["required_computations"][0]["inputs"]["planned"].update(x=1),
                     code="unknown_key")
        self.refuses(PlaybookSchemaError, lambda d: d["success_checks"][0].update(x=1),
                     code="unknown_key")

    def test_a_computation_no_check_references_is_refused(self):
        def drop_checks(d):
            d["success_checks"] = [c for c in d["success_checks"]
                                   if c.get("params", {}).get("calculation") != "planned_minus_manufactured"]
        self.refuses(PlaybookSchemaError, drop_checks, code="computation_not_checked")

    def test_a_non_arithmetic_check_does_not_cover_a_computation(self):
        def retype(d):
            for c in d["success_checks"]:
                if c.get("params", {}).get("calculation") == "planned_minus_manufactured":
                    c["type"] = "evidence_cited"
                    c["params"] = {"role": d["required_evidence"][0]["role"]}
        self.refuses(PlaybookSchemaError, retype, code="computation_not_checked")

    def test_missing_key_schema_version_and_id(self):
        self.refuses(PlaybookSchemaError, lambda d: d.pop("expected_gaps"), code="missing_key")
        self.refuses(PlaybookSchemaError, lambda d: d.update(schema="v2"),
                     code="unsupported_schema")
        self.refuses(PlaybookSchemaError, lambda d: d.update(version=True), code="invalid_version")
        self.refuses(PlaybookSchemaError, lambda d: d.update(version=0), code="invalid_version")
        self.refuses(PlaybookSchemaError, lambda d: d.update(id="Bad Id"), code="invalid_format")

    def test_tool_rules(self):
        self.refuses(PlaybookToolError, lambda d: d["allowed_tools"].append("plx.search"),
                     code="tool_not_in_case_runtime")
        self.refuses(PlaybookToolError, lambda d: d["allowed_tools"].append("plx.dispatch_action"),
                     code="business_dispatch_tool_forbidden")
        self.refuses(PlaybookToolError, lambda d: d["allowed_tools"].append("plx.case_approve"),
                     code="business_dispatch_tool_forbidden")
        self.refuses(PlaybookToolError, lambda d: d["writes_case_state"].remove("plx.case_compute"),
                     code="write_tool_not_declared")
        self.refuses(PlaybookToolError, lambda d: d["writes_case_state"].append("plx.case_get"),
                     code="write_tool_is_read_only")
        self.refuses(PlaybookToolError,
                     lambda d: d["writes_case_state"].append("plx.case_close"),
                     code="write_tool_not_allowed")
        self.refuses(PlaybookSchemaError, lambda d: d["allowed_tools"].append("plx.case_get"),
                     code="duplicate_entry")

    def test_authority(self):
        self.refuses(PlaybookAuthorityError, lambda d: d["authority"].update(business_writes=1),
                     code="business_writes_must_be_zero")
        self.refuses(PlaybookAuthorityError, lambda d: d["authority"].update(business_writes=False),
                     code="business_writes_must_be_zero")
        self.refuses(PlaybookAuthorityError, lambda d: d["authority"].update(approvals="agent"),
                     code="approvals_must_be_portal")

    def test_evidence_rules(self):
        self.refuses(PlaybookSchemaError, lambda d: d["required_evidence"][0].update(relation="x"),
                     code="invalid_format")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["required_evidence"].append(dict(d["required_evidence"][0])),
                     code="duplicate_entry")
        self.refuses(PlaybookSchemaError, lambda d: d["required_evidence"][0].update(required="no"),
                     code="invalid_boolean")
        self.refuses(PlaybookSchemaError, lambda d: d.update(required_evidence=[]),
                     code="expected_list")

    def test_computation_rules(self):
        self.refuses(PlaybookSchemaError,
                     lambda d: d["required_computations"][0]["inputs"]["planned"].update(role="nope"),
                     code="unknown_role")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["required_computations"][0].update(expression="planned ** manufactured"),
                     code="invalid_expression")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["required_computations"][0].update(expression="planned - ghost"),
                     code="expression_unknown_input")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["required_computations"][0].update(expression="planned"),
                     code="expression_unused_input")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["required_computations"][2].update(expression="damaged + 1"),
                     code="expression_repeat_misuse")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["required_computations"][0].update(unit=""), code="invalid_text")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["required_computations"][0].update(expression="__import__('os')"),
                     code="invalid_expression")

    def test_gap_rules(self):
        self.refuses(PlaybookSchemaError, lambda d: d["expected_gaps"][0].update(kind="lost"),
                     code="unknown_gap_kind")
        self.refuses(PlaybookSchemaError, lambda d: d["expected_gaps"][0].update(when="sometimes"),
                     code="unknown_condition")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["expected_gaps"][3].update(when="field_empty:ghost:qtytotal"),
                     code="unknown_role")

    def test_check_rules(self):
        self.refuses(PlaybookSchemaError, lambda d: d["success_checks"][0].update(type="vibes"),
                     code="unknown_check_type")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["success_checks"][9]["params"].update(calculation="ghost"),
                     code="unknown_calculation")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["success_checks"][2]["params"].update(role="ghost"),
                     code="unknown_role")
        self.refuses(PlaybookSchemaError,
                     lambda d: d["success_checks"][-1]["params"].update(gap="ghost"),
                     code="unknown_gap")
        self.refuses(PlaybookSchemaError, lambda d: d["success_checks"][2].pop("params"),
                     code="missing_key")
        self.refuses(PlaybookSchemaError, lambda d: d["success_checks"][0].update(params={"x": "y"}),
                     code="unknown_key")

    def test_json_level_refusals(self):
        with self.assertRaises(PlaybookSchemaError) as ctx:
            parse_playbook('{"id": "a", "id": "b"}')
        self.assertEqual(ctx.exception.code, "duplicate_json_key")
        with self.assertRaises(PlaybookSchemaError):
            parse_playbook("not json")
        with self.assertRaises(PlaybookSchemaError) as ctx:
            parse_playbook('{"version": NaN}')
        self.assertEqual(ctx.exception.code, "invalid_json_constant")
        with self.assertRaises(PlaybookSchemaError):
            parse_playbook("[]")


class HashTests(unittest.TestCase):
    def test_hash_stable_across_key_order_and_whitespace(self):
        shuffled = json.dumps(dict(reversed(list(AO.items()))), indent=4)
        a, b = Playbook(AO), parse_playbook(shuffled)
        self.assertEqual(a.definition_sha256, b.definition_sha256)

    def test_hash_changes_with_content(self):
        changed = mutated(AO, lambda d: d.update(question_pattern="Something else?"))
        self.assertNotEqual(Playbook(AO).definition_sha256, Playbook(changed).definition_sha256)

    def test_default_params_do_not_change_hash_identity_of_explicit_form(self):
        with_params = mutated(AO, lambda d: d["success_checks"][0].update(params={}))
        self.assertEqual(Playbook(AO).definition_sha256, Playbook(with_params).definition_sha256)


class RegistryTests(unittest.TestCase):
    def write(self, folder, name, doc):
        (Path(folder) / name).write_text(json.dumps(doc))

    def test_conflicting_same_id_version_refused(self):
        other = mutated(AO, lambda d: d.update(question_pattern="Different?"))
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, "a.json", AO)
            self.write(tmp, "b.json", other)
            with self.assertRaises(PlaybookRegistryError) as ctx:
                PlaybookRegistry().load_directory(tmp)
        self.assertEqual(ctx.exception.code, "conflicting_definition")

    def test_identical_duplicate_allowed_and_sources_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, "a.json", AO)
            self.write(tmp, "b.json", AO)
            reg = PlaybookRegistry().load_directory(tmp)
        self.assertEqual(len(reg), 1)
        self.assertEqual(reg.listing()[0]["sources"], ["a.json", "b.json"])

    def test_new_version_coexists_and_latest_wins(self):
        v2 = mutated(AO, lambda d: d.update(version=2, question_pattern="v2?"))
        reg = PlaybookRegistry()
        reg.add(Playbook(AO))
        reg.add(Playbook(v2))
        self.assertEqual(reg.get(AO["id"]).version, 2)
        self.assertEqual(reg.get(AO["id"], 1).version, 1)
        with self.assertRaises(PlaybookRegistryError):
            reg.get(AO["id"], 3)
        with self.assertRaises(PlaybookRegistryError):
            reg.get("nope")

    def test_invalid_file_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, "bad.json", mutated(AO, lambda d: d.update(extra=1)))
            with self.assertRaises(PlaybookSchemaError) as ctx:
                PlaybookRegistry().load_directory(tmp)
        self.assertIn("bad.json", ctx.exception.path)

    def test_missing_directory(self):
        with self.assertRaises(PlaybookRegistryError):
            PlaybookRegistry().load_directory("/nonexistent-playbooks")


class DiffTests(unittest.TestCase):
    def test_identical(self):
        d = diff(Playbook(AO), Playbook(AO))
        self.assertTrue(d["identical"])
        self.assertFalse(d["widens_authority"])
        self.assertFalse(d["weakens_checks"])

    def test_changes_reported(self):
        def edit(d):
            d["version"] = 2
            d["allowed_tools"].remove("plx.case_attach_original")
            d["writes_case_state"].remove("plx.case_attach_original")
            d["allowed_tools"].append("plx.schema")
            d["success_checks"] = [c for c in d["success_checks"] if c["id"] != "scan_gap"]
            d["required_evidence"][0]["key_hint"] = "changed hint"
        d = diff(Playbook(AO), Playbook(mutated(AO, edit)))
        self.assertFalse(d["identical"])
        self.assertEqual(d["tools"], {"added": ["plx.schema"],
                                      "removed": ["plx.case_attach_original"]})
        self.assertEqual(d["writes_case_state"]["removed"], ["plx.case_attach_original"])
        self.assertEqual(d["success_checks"]["removed"], ["scan_gap"])
        self.assertEqual(d["required_evidence"]["changed"], ["ao"])
        self.assertTrue(d["widens_authority"])
        self.assertTrue(d["weakens_checks"])
        self.assertEqual(d["from"]["version"], 1)
        self.assertEqual(d["to"]["version"], 2)

    def test_added_write_tool_widens(self):
        def edit(d):
            d["version"] = 2
            d["allowed_tools"].append("plx.case_close")
            d["writes_case_state"].append("plx.case_close")
        d = diff(Playbook(AO), Playbook(mutated(AO, edit)))
        self.assertEqual(d["writes_case_state"]["added"], ["plx.case_close"])
        self.assertTrue(d["widens_authority"])

    def test_different_ids_refused(self):
        with self.assertRaises(PlaybookRegistryError):
            diff(Playbook(AO), Playbook(PO))

    def weakens(self, edit, base=AO):
        d = diff(Playbook(base), Playbook(mutated(base, lambda v: (edit(v), v.update(version=2)))))
        return d["weakens_checks"], d["weakening_reasons"]

    def test_weakening_changes_are_flagged(self):
        cases = {
            "evidence required to optional": (
                lambda d: d["required_evidence"][0].update(required=False),
                "evidence_required_to_optional:ao"),
            "gap when changed": (
                lambda d: d["expected_gaps"][4].update(when="role_missing:ao"),
                "gap_when_changed:original_scan_not_ingested"),
            "gap kind changed": (
                lambda d: d["expected_gaps"][4].update(kind="unknown"),
                "gap_kind_changed:original_scan_not_ingested"),
            "gap match loosened": (
                lambda d: d["expected_gaps"][4]["match"].update(terms=["scan"]),
                "gap_match_changed:original_scan_not_ingested"),
            "computation removed": (
                lambda d: (d["required_computations"].pop(), d.update(success_checks=[
                    c for c in d["success_checks"] if c["id"] != "ordered_arith"])),
                "computation_removed:ordered_minus_manufactured"),
            "computation changed": (
                lambda d: d["required_computations"][0].update(expression="planned + manufactured"),
                "computation_changed:planned_minus_manufactured"),
            "evidence relation changed": (
                lambda d: d["required_evidence"][0].update(relation="public.other"),
                "evidence_relation_changed:ao")}
        for label, (edit, reason) in cases.items():
            with self.subTest(label):
                flagged, reasons = self.weakens(edit)
                self.assertTrue(flagged)
                self.assertIn(reason, reasons)

    def test_a_pure_tightening_or_hint_change_is_not_a_weakening(self):
        flagged, _ = self.weakens(lambda d: d["required_evidence"][0].update(key_hint="new"))
        self.assertFalse(flagged)
        flagged, _ = self.weakens(lambda d: d["required_evidence"][-1].update(required=True))
        self.assertFalse(flagged)
        flagged, _ = self.weakens(lambda d: d["required_evidence"][1].update(required=True),
                                  base=PO)
        self.assertFalse(flagged)


class GapMatchTests(unittest.TestCase):
    def refuses(self, fn):
        with self.assertRaises(PlaybookSchemaError):
            validate(mutated(AO, fn))

    def test_every_shipped_gap_is_structured_or_has_content_match_terms(self):
        from operating import capability_guards
        from operating.ontology_tools import shared_schema
        for path in sorted([*DIR.glob("*.json"), *RETIRED_DIR.glob("*.json")]):
            doc = json.loads(path.read_text())
            for gap in doc["expected_gaps"]:
                if gap["kind"] == "absent_in_source" and not playbooks.retired_tools(doc):
                    self.assertIn("property", gap, (path.name, gap["id"]))
                if "property" in gap:
                    # Structured: the names resolve in the schema the gap guard reads.
                    subject, reason = capability_guards.resolve_gap_subject(
                        shared_schema(), gap["property"])
                    self.assertIsNotNone(subject, (gap["id"], reason))
                else:
                    self.assertTrue(gap["match"]["terms"], gap["id"])
                for role in gap["match"].get("cites", []):
                    self.assertIn(role, [e["role"] for e in doc["required_evidence"]])

    def test_an_absence_gap_must_name_its_property(self):
        def unstructured(doc):
            gap = next(g for g in doc["expected_gaps"] if g["kind"] == "absent_in_source")
            gap.pop("property")
            gap["match"]["terms"] = ["absent"]
        current = [json.loads((DIR / name).read_text()) for name in
                   ("ao_quantity_reconciliation_v4.json", "customer_po_enrichment_v3.json")]
        for base in current:
            with self.assertRaises(PlaybookSchemaError) as caught:
                validate(mutated(base, unstructured))
            self.assertEqual(caught.exception.code, "absence_gap_needs_property")
        # A published version that loads as retired keeps its term match.
        self.assertTrue(playbooks.retired_tools(AO))
        validate(AO)
        with self.assertRaises(PlaybookSchemaError) as caught:
            validate(mutated(current[1], lambda d: next(g for g in d["expected_gaps"]
                                                if "property" in g).update(
                property="customer po")))
        self.assertEqual(caught.exception.code, "invalid_format")

    def test_match_is_required_and_strict(self):
        self.refuses(lambda d: d["expected_gaps"][0].pop("match"))
        self.refuses(lambda d: d["expected_gaps"][0].update(match={"terms": []}))
        self.refuses(lambda d: d["expected_gaps"][0].update(match={"terms": ["x"], "cites": ["ghost"]}))
        self.refuses(lambda d: d["expected_gaps"][0].update(match={"terms": ["x"], "extra": 1}))
        self.refuses(lambda d: d["expected_gaps"][0].update(match={"terms": ["x", "X"]}))

    def test_customer_po_playbook_allows_the_generic_tools_its_flow_uses(self):
        for name in ("plx.find", "plx.object", "plx.case_propose", "plx.case_record_gap"):
            self.assertIn(name, PO_V3["allowed_tools"])
        self.assertFalse({name for name in PO_V3["allowed_tools"] if "customer_po" in name})


class ImmutableDefinitionTests(unittest.TestCase):
    """Finding 2: a loaded definition cannot be changed without raising."""

    def test_in_place_mutation_raises_and_the_hash_is_unchanged(self):
        pb = Playbook(AO)
        before = pb.definition_sha256
        for mutate in (lambda d: d["required_evidence"][0].__setitem__("relation", "public.x"),
                       lambda d: d["allowed_tools"].append("plx.case_close"),
                       lambda d: d.__setitem__("version", 9),
                       lambda d: d["authority"].update(business_writes=1),
                       lambda d: d["success_checks"].pop(),
                       lambda d: d["required_evidence"][0]["identity"].setdefault("x", 1),
                       lambda d: d["expected_gaps"][0]["match"]["terms"].sort(),
                       lambda d: d.clear()):
            with self.subTest(), self.assertRaises(TypeError):
                mutate(pb.definition)
        self.assertEqual(pb.definition_sha256, before)
        self.assertEqual(definition_sha256(pb.definition), before)

    def test_a_bypassing_mutation_is_caught_on_the_next_use(self):
        pb = Playbook(PO)
        dict.__setitem__(pb.definition["required_evidence"][0], "relation", "public.other")
        with self.assertRaises(PlaybookIntegrityError) as raised:
            pb.definition_sha256
        self.assertEqual(raised.exception.code, "definition_changed_after_load")
        with self.assertRaises(PlaybookIntegrityError):
            pb.verify()
        with self.assertRaises(PlaybookIntegrityError):
            pb.definition

    def test_a_deepcopy_is_a_plain_mutable_dict_that_builds_a_new_playbook(self):
        pb = Playbook(AO)
        copy_ = copy.deepcopy(pb.definition)
        self.assertIs(type(copy_), dict)
        copy_["version"] = 2
        second = Playbook(copy_)
        self.assertEqual((pb.version, second.version), (1, 2))
        self.assertNotEqual(pb.definition_sha256, second.definition_sha256)
        self.assertEqual(json.loads(json.dumps(pb.definition)), json.loads(json.dumps(copy_)) | {"version": 1})

    def test_the_source_dict_is_not_shared_with_the_playbook(self):
        source = copy.deepcopy(AO)
        pb = Playbook(source)
        source["version"] = 5
        self.assertEqual(pb.version, 1)


class IdentityRuleTests(unittest.TestCase):
    """Finding 1 (schema side): every evidence role declares how its rows bind to the subject."""

    def refuses(self, fn, code, base=AO):
        with self.assertRaises(PlaybookSchemaError) as ctx:
            validate(mutated(base, fn))
        self.assertEqual(ctx.exception.code, code)

    def test_identity_is_required_on_every_evidence_role(self):
        self.refuses(lambda d: d["required_evidence"][0].pop("identity"), "missing_key")

    def test_shipped_playbooks_bind_or_explicitly_declare_any(self):
        for doc in (AO, PO):
            for role in doc["required_evidence"]:
                identity = role["identity"]
                self.assertTrue(identity == "any" or identity["kind"] in ("case_root", "join"))
        self.assertEqual([r["role"] for r in PO["required_evidence"] if r["identity"] == "any"],
                         ["po_evidence", "po_evidence_row"])
        self.assertEqual([r["role"] for r in AO["required_evidence"] if r["identity"] == "any"], [])

    def test_identity_shapes_are_strict(self):
        self.refuses(lambda d: d["required_evidence"][0].update(identity=None), "invalid_identity")
        self.refuses(lambda d: d["required_evidence"][0].update(identity="all"), "invalid_identity")
        self.refuses(lambda d: d["required_evidence"][0].update(identity={"kind": "magic"}),
                     "invalid_identity")
        self.refuses(lambda d: d["required_evidence"][0].update(
            identity={"kind": "case_root", "fields": []}), "expected_list")
        self.refuses(lambda d: d["required_evidence"][0].update(
            identity={"kind": "case_root", "fields": ["a"], "x": 1}), "unknown_key")
        self.refuses(lambda d: d["required_evidence"][2]["identity"].update(to_role="ghost"),
                     "unknown_role")
        self.refuses(lambda d: d["required_evidence"][2]["identity"].pop("to_field"), "missing_key")

    def test_join_chains_must_be_acyclic_and_end_at_a_case_root(self):
        self.refuses(lambda d: d["required_evidence"][2]["identity"].update(to_role="line"),
                     "identity_cycle")
        self.refuses(lambda d: d["required_evidence"][0].update(identity="any"),
                     "identity_chain_unanchored")

    def test_loosening_identity_is_a_weakening_and_tightening_is_not(self):
        def loosen(d):
            d["version"] = 2
            d["required_evidence"][1]["identity"] = "any"
        result = diff(Playbook(AO), Playbook(mutated(AO, loosen)))
        self.assertIn("evidence_identity_loosened:order", result["weakening_reasons"])
        self.assertTrue(result["weakens_checks"])

        def change(d):
            d["version"] = 2
            d["required_evidence"][1]["identity"]["fields"] = ["id"]
        result = diff(Playbook(AO), Playbook(mutated(AO, change)))
        self.assertIn("evidence_identity_changed:order", result["weakening_reasons"])

        def tighten(d):
            d["version"] = 2
            d["required_evidence"][1]["identity"] = "any"
        old = Playbook(mutated(AO, tighten))
        new = Playbook(mutated(AO, lambda d: d.update(version=2)))
        self.assertNotIn("evidence_identity_loosened:order",
                         diff(old, new)["weakening_reasons"])


class _Traced(dict):
    """A definition that records the path of every value read from it (``name[]`` steps into
    a list item, ``inputs.*`` into a computation input): by subscript, ``get``, ``in``,
    iteration, ``dict()``/``copy()``, ``copy.deepcopy`` and ``json.dumps`` (which reads a dict
    subclass through ``items``)."""

    def __init__(self, data, path, reads):
        super().__init__(data)
        self._path, self._reads = path, reads

    def _key(self, key):
        return (f"{self._path}.*" if self._path.endswith(".inputs")
                else f"{self._path}.{key}" if self._path else key)

    def __getitem__(self, key):
        return _traced(dict.__getitem__(self, key), self._key(key), self._reads)

    def __contains__(self, key):
        self._reads.add(self._key(key))
        return dict.__contains__(self, key)

    def get(self, key, default=None):
        return self[key] if key in self else default

    def keys(self):
        return list(dict.keys(self))

    def __iter__(self):
        return iter(self.keys())

    def items(self):
        return [(key, self[key]) for key in self.keys()]

    def values(self):
        return [self[key] for key in self.keys()]

    def copy(self):
        return dict(self)  # through keys() and __getitem__, as __iter__ is overridden

    def __deepcopy__(self, memo):  # a plain copy, not one holding a copy of ``reads``
        return {key: copy.deepcopy(value, memo) for key, value in self.items()}


class _TracedList(list):
    """Items are wrapped up front (unrecorded): ``json.dumps`` reads a list subclass's items
    directly, so a nested item records its own reads."""

    def __init__(self, data, path, reads):
        super().__init__(_wrap(item, path + "[]", reads) for item in data)
        self._path, self._reads = path, reads

    def __getitem__(self, index):
        self._reads.add(self._path + "[]")
        return list.__getitem__(self, index)

    def __iter__(self):
        return (self[index] for index in range(len(self)))

    def __contains__(self, value):
        return value in copy.deepcopy(self)  # an item compared is an item read whole

    def __deepcopy__(self, memo):
        return [copy.deepcopy(item, memo) for item in self]


def _wrap(value, path, reads):
    if isinstance(value, dict):
        return _Traced(value, path, reads)
    return _TracedList(value, path, reads) if isinstance(value, list) else value


def _traced(value, path, reads):
    reads.add(path)
    return value if isinstance(value, (_Traced, _TracedList)) else _wrap(value, path, reads)


def _leaves(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, f"{path}.*" if path.endswith(".inputs")
                               else f"{path}.{key}" if path else key)
    elif isinstance(value, list) and value and isinstance(value[0], (dict, list)):
        for item in value:
            yield from _leaves(item, path + "[]")
    else:
        yield path


class _CustomerPoCase(tpe.CustomerPoEvalBase):
    """The customer-PO evaluation fixture, cleaned up by the test that holds it."""

    def __init__(self, test):
        self.addCleanup = test.addCleanup


class DescriptiveFieldsTests(tpe.AoEvalCase):
    """``behavioural_body`` (the rule a proposal is matched on) drops VERSION_FIELDS and
    DESCRIPTIVE_FIELDS: they must be exactly what validate accepts and no evaluation reads."""

    # Read by no evaluation, yet part of the rule: the registry key, and what validate pins
    # (authority is fixed; writes_case_state must equal the allowed write tools).
    CONSTRAINED = {"id", "authority.business_writes", "authority.approvals", "writes_case_state"}

    def test_descriptive_fields_are_disjoint_from_the_constrained_fields(self):
        """A validate-pinned field changes what a version may do: dropping it from the rule
        would let a widened version match a recorded regression's rule."""
        dropped = set(playbooks.VERSION_FIELDS + playbooks.DESCRIPTIVE_FIELDS)
        self.assertEqual({path for path in self.CONSTRAINED for d in dropped
                          if path == d or path.startswith(d + ".") or d.startswith(path + ".")},
                         set(), "a descriptive field is one validate pins")

    def test_the_tracer_counts_every_read_form(self):
        """``in`` reads the key it tests; dict() and copy() each value (a nested one records
        its own reads later); deepcopy, json.dumps and list membership every leaf below."""
        definition = {"question_pattern": "q", "required_evidence": [{"key_hint": "k"}],
                      "required_computations": [{"inputs": {"a": "b"}}]}
        leaves = set(_leaves(definition))
        for name, read, expected in (
                ("in", lambda d: "question_pattern" in d, {"question_pattern"}),
                ("dict", dict, set(definition)), ("copy", lambda d: d.copy(), set(definition)),
                ("deepcopy", copy.deepcopy, leaves), ("json.dumps", json.dumps, leaves),
                ("list in", lambda d: {"key_hint": "k"} in d["required_evidence"],
                 {"required_evidence[].key_hint"})):
            with self.subTest(name):
                reads = set()
                read(_Traced(copy.deepcopy(definition), "", reads))
                self.assertLessEqual(expected, reads)

    def trace(self, playbook, call, case_id, transcript, reads):
        """Evaluate ``playbook`` (retired or not) over a traced copy of its definition."""
        class Recording:
            id, version, definition_sha256 = playbook.id, playbook.version, \
                playbook.definition_sha256
            retired, retired_tools = False, []
            definition = property(lambda self: _Traced(
                json.loads(json.dumps(dict(playbook.definition))), "", reads))
        return tpe.pe.evaluate(Recording(), call, case_id, transcript=transcript)

    def test_descriptive_fields_are_the_paths_no_evaluation_reads(self):
        shipped = [Playbook(json.loads(path.read_text())) for path in sorted(DIR.glob("*.json"))]
        self.assertEqual({p.id for p in shipped},
                         {"ao_quantity_reconciliation", "customer_po_enrichment"})
        transcript = []

        def call(name, arguments):
            transcript.append(name)
            return self.call(name, arguments)
        ao_case = tpe.ao_reference.investigate(call, ao_code="AO-26005B")["case_id"]
        po = _CustomerPoCase(self)
        po.setUp()
        po_case = po.build()
        reads = set()
        for playbook in [tpe.AO_V2, *shipped]:
            with self.subTest(playbook.id, version=playbook.version):
                if playbook.id == "customer_po_enrichment":
                    report = self.trace(playbook, po.call, po_case, po.actual.names, reads)
                else:
                    report = self.trace(playbook, self.call, ao_case, transcript, reads)
                # every check ran and passed, so every path a check reads was read (CPO v1
                # binds evidence by other roles: its passed_all is qualified, not its checks)
                checks = playbook.definition["success_checks"]
                self.assertEqual(report["summary"]["failed_ids"], [], report["checks"])
                self.assertEqual(len(report["checks"]), len(checks))
        unread = set(playbooks.VERSION_FIELDS + playbooks.DESCRIPTIVE_FIELDS)
        self.assertEqual({p for p in reads for u in unread if p == u or p.startswith(u + ".")},
                         set(), "an evaluation reads a field behavioural_body drops")
        accepted = set(_leaves(dict(tpe.AO_V2.definition)))
        for path in [*DIR.glob("*.json"), *RETIRED_DIR.glob("*.json")]:
            accepted |= set(_leaves(validate(json.loads(path.read_text()))))
        self.assertEqual(accepted - reads, unread | self.CONSTRAINED,
                         "a field no evaluation reads is neither descriptive nor constrained")


if __name__ == "__main__":
    unittest.main()
