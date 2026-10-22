"""Capability-keyed proposal guards (the owner entries of operating/action_catalog.json + operating/capability_guards.py).

Retiring the customer-PO proposal tool removed its guards, and
three wrong proposals passed the playbook and the claim gate. The guards are now data per
capability, checked by plx.case_propose, plx.act (mode propose) and the playbook evaluator.
Evidence label: injected-transport (FakeWorkbench mirror).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from integrations.tests.test_source_mirror_persisted import (HTTP_500, LABEL, SALES,
                                                             PersistedTestCase)
from operating import capability_guards as cg
from operating.ontology_tools import shared_schema
from operating import customer_po_coverage as cpo
from operating import playbook_eval as pe
from operating.tests import test_customer_po_coverage as cpo_tests
from operating.tests.test_playbook_eval import CPO, AoEvalCase, CustomerPoEvalBase

CAPABILITY = "portal.sales_order.set_customer_po"


class RegistryTests(unittest.TestCase):
    def test_the_po_shape_is_the_coverage_rule(self):
        shape = cg.registry()["shapes"]["po_like"]
        values = {str(row.get(key)) for row in cpo_tests.ORDERS
                  for key in ("po_reference", "order_number") if row.get(key) is not None}
        values |= set(cpo_tests.NON_PO_TEXT) | {"000", "PO-1", "12345", "ab", "x" * 41,
                                                 "-PO1", "PO1-", "PO#12", "4500 12"}
        for value in sorted(values):
            self.assertEqual(bool(shape.fullmatch(value.strip())), cpo.po_like(value), value)

    def test_every_declared_capability_is_canonical_and_only_fixtures_go_unguarded(self):
        from operating.action_path import (FIXTURE_CAPABILITY, MC_CALL_CAPABILITIES,
                                           REAL_CAPABILITIES, REAL_SYSTEMS)
        declared = cg.registry()["capabilities"]
        self.assertLessEqual(set(REAL_CAPABILITIES) | {FIXTURE_CAPABILITY}, set(declared))
        for capability, entry in declared.items():
            self.assertEqual(cg.canonical(capability), capability)
            self.assertTrue(entry["target_relations"])  # every capability binds relations
            system = capability.split(".", 1)[0]
            if system in ("fixture", "test"):
                # Workbench-internal, declared empty, and never on the production host.
                self.assertEqual((entry["guards"], entry["hosts"]), ([], ["test"]))
                self.assertFalse(cg.available(capability))
                self.assertTrue(cg.available(capability, test_host=True))
                continue
            self.assertIn(system, REAL_SYSTEMS)
            self.assertTrue(cg.available(capability))
            checks = {guard["check"] for guard in entry["guards"]}
            if capability in MC_CALL_CAPABILITIES:
                # Mission Control's own write creates a task: no cited row, no row guard.
                self.assertEqual((checks, entry["target_relations"]), (set(), ["mc.task"]))
                continue
            self.assertLessEqual(checks, cg.CHECKS)
            # A real-system write names its writable fields and a cited target row, on
            # exactly named relations (no prefix patterns).
            self.assertIn("effect_fields_within", checks)
            self.assertTrue(all("*" not in item for item in entry["target_relations"]))
        self.assertEqual(declared[FIXTURE_CAPABILITY]["target_relations"], ["fixture.*"])

    def test_a_capability_writes_only_its_bound_relations(self):
        entry = cg.registry()["capabilities"]["fixture.record.set_field"]
        self.assertTrue(cg.bound(entry, "fixture.orders"))
        for relation in ("public.app_sales_order", "fixtureX.orders", "fixture", None):
            self.assertFalse(cg.bound(entry, relation), relation)
        # Any effect outside the bound relations fails, before the capability's guards.
        effect = {"relation": cpo.RELATION, "key": {"id": 3}, "values": {"po_reference": "X"}}
        verdict = cg.check("fixture.record.set_field", {}, effect, [], lambda h: None)
        self.assertEqual([f["id"] for f in verdict["failures"]], ["effect_relation_not_bound"])
        effect["relation"] = "public.assembly_order"
        verdict = cg.check(CAPABILITY, {"proposed_customer_po": "X", "evidence_citation": "e",
                                        "evidence_field": "f"}, effect, [], lambda h: None)
        self.assertEqual(verdict["failures"][0]["id"], "effect_relation_not_bound")

    def test_an_entry_without_bound_relations_or_hosts_is_a_malformed_registry(self):
        import json
        import tempfile
        from pathlib import Path
        document = json.loads(cg.GUARDS_PATH.read_text(encoding="utf-8"))
        for change in ({"target_relations": []}, {"target_relations": None},
                       {"hosts": []}, {"hosts": ["everywhere"]}):
            broken = json.loads(json.dumps(document))
            broken["actions"][CAPABILITY].update(change)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "action_catalog.json"
                path.write_text(json.dumps(broken), encoding="utf-8")
                with self.assertRaises(cg.GuardError, msg=change):
                    cg.registry(str(path))

    def test_an_unregistered_capability_is_unguarded_and_never_passes(self):
        verdict = cg.check("portal.sales_order.set_anything", {}, None, [], lambda h: None)
        self.assertEqual((verdict["guarded"], verdict["passed"]), (False, False))


class FieldStateTests(unittest.TestCase):
    def test_unreadable_is_never_empty(self):
        row = {"po_reference": None, "custom_fields": '{"custPoCode": ""}', "bad": "{x",
               "list": "[1]"}
        self.assertEqual(cg.field_state(row, "po_reference")[0], "absent")
        self.assertEqual(cg.field_state(row, "custom_fields.custPoCode")[0], "absent")
        self.assertEqual(cg.field_state(row, "custom_fields.other")[0], "absent")
        for path in ("missing_column", "missing_column.key", "bad.key", "list.key"):
            self.assertEqual(cg.field_state(row, path)[0], "unreadable", path)
        # A target row whose PO JSON column is not there at all refuses the guard.
        effect = {"relation": cpo.RELATION, "key": {"id": 3}, "fields": ["po_reference"],
                  "equals": "PO-7788"}
        rows = {"api://e": {"relation": "sage.oe_order_header", "key": {"_sage_pk": "3"},
                            "row": {"ponumber": "PO-7788", "ordnumber": "S3"}},
                "api://t": {"relation": cpo.RELATION, "key": {"id": 3},
                            "row": {"po_reference": None, "order_number": "OR0003"}}}
        verdict = cg.check(CAPABILITY, {"proposed_customer_po": "PO-7788",
                                        "evidence_citation": "api://e",
                                        "evidence_field": "ponumber"}, effect,
                           ["api://e", "api://t"], rows.get)
        self.assertEqual([f["id"] for f in verdict["failures"]], ["target_has_no_po"])
        rows["api://t"]["row"]["custom_fields"] = None
        self.assertTrue(cg.check(CAPABILITY, {"proposed_customer_po": "PO-7788",
                                              "evidence_citation": "api://e",
                                              "evidence_field": "ponumber"}, effect,
                                 ["api://e", "api://t"], rows.get)["passed"])


class NoOverwriteTests(unittest.TestCase):
    """The owner-side re-check of a type's no-overwrite guards: on the
    owner's record every written field such a guard names must hold nothing."""

    def test_any_held_value_of_a_guarded_written_field_fails_and_empty_passes(self):
        for row in ({"po_reference": None}, {"po_reference": ""}, {"po_reference": "  "}):
            self.assertEqual(cg.no_overwrite_failures(CAPABILITY, row, ["po_reference"]), [])
        # PO-shaped, not PO-shaped (a name) and the order's own number all refuse here:
        # registration's shape allowances judge cited content, not the owner just before
        # a write; a field the owner did not return, or not as text, is unreadable.
        for row in ({"po_reference": "PO-5501"}, {"po_reference": "Vince Ramos"},
                    {"po_reference": "OR0003", "order_number": "OR0003"}, {},
                    {"po_reference": 1.5}):
            with self.subTest(row=row):
                failures = cg.no_overwrite_failures(CAPABILITY, row, ["po_reference"])
                self.assertEqual([f["id"] for f in failures], ["target_has_no_po"])
        # Only the fields the write sets; an undeclared capability fails.
        self.assertEqual(cg.no_overwrite_failures(
            CAPABILITY, {"po_reference": "PO-5501"}, ["notes"]), [])
        self.assertEqual([f["id"] for f in cg.no_overwrite_failures(
            "portal.sales_order.set_anything", {"po_reference": None}, ["po_reference"])],
            ["capability_undeclared"])
        self.assertLessEqual(cg.NO_OVERWRITE_CHECKS, cg.CHECKS)


class NumericEvidenceTests(unittest.TestCase):
    """WH-NUMERIC-EVIDENCE-1: the catalog declares the AO header
    quantities numeric, so a JSON float (1509.0, a 1.0 ratio) is the number it says and
    keeps its raw value; identifiers (the customer PO) still never parse a float."""

    AO = {"relation": "public.assembly_order", "key": {"id": 7},
          "row": {"id": 7, "nameamo_c": "AO-X", "qtytotal": None, "finalqty": None}}

    def verdict(self, quantity, ratio=1, values=None, payload=None, target=None):
        values = values or {"qtytotal": "1509"}
        item = {"relation": "silver.assembly_order_items", "key": {"id": 1},
                "row": {"id": 1, "assembly_order_id": 7, "quantity": quantity,
                        "qty_per_batch": ratio}}
        rows = {"api://ao": target or self.AO, "api://item": item}
        payload = dict(payload or values,
                       evidence={field: {"citation": "api://item", "field": "quantity"}
                                 for field in values})
        effect = {"relation": "public.assembly_order", "key": {"id": 7}, "values": values}
        out = cg.check(AO_CAPABILITY, payload, effect, ["api://ao", "api://item"], rows.get)
        return [f["id"] for f in out["failures"]], out

    def test_a_float_or_decimal_text_quantity_is_the_number_it_says(self):
        for quantity, ratio in ((1509.0, 1), (1509, 1.0), (1509.0, 1.0), ("1509.0", "1.0"),
                                (1509, 1), ("1509", "1")):
            self.assertEqual(self.verdict(quantity, ratio)[0], [], (quantity, ratio))
        # A float in the payload and the effect too.
        self.assertEqual(self.verdict("1509", values={"qtytotal": 1509.0})[0], [])

    def test_a_different_or_non_finite_number_is_refused(self):
        for quantity, ratio in ((1509.5, 1), ("1509.5", 1), (1509, 3.975), (1509, "3.975")):
            self.assertEqual(self.verdict(quantity, ratio)[0], ["values_in_cited_evidence"],
                             (quantity, ratio))
        for quantity in (float("nan"), float("inf"), "NaN", "Infinity", True, [1509],
                         "1509 units"):
            self.assertEqual(self.verdict(quantity)[0], ["values_in_cited_evidence"],
                             quantity)
        self.assertIn("payload_carries_the_written_values",
                      self.verdict(1509, values={"qtytotal": float("inf")},
                                   payload={"qtytotal": float("inf")})[0])

    def test_number_state_keeps_the_raw_value_and_identifiers_still_refuse_floats(self):
        state = cg.number_state(True, 1509.0)
        self.assertEqual((state["state"], state["value"]), ("value", 1509.0))
        self.assertIsInstance(state["value"], float)  # raw provenance, as sent
        self.assertEqual(state["number"], 1509)
        self.assertEqual(cg.number_state(True, "1.0")["number"], 1)
        self.assertEqual(cg.number_state(True, None)["state"], "absent")
        self.assertEqual(cg.number_state("column_not_mirrored", None)["state"], "unreadable")
        # Numeric text is a plain ASCII decimal only: no exponent,
        # underscore, non-ASCII digit or spelled-out infinity.
        for text in ("1509", " -1509.50 ", "+1509", ".5", "1509."):
            self.assertEqual(cg.number_state(True, text)["state"], "value", text)
        for text in ("1e400", "1.509e3", "1_509", "\u0661\u0665\u0660\u0669", "１５０９",
                     "0x5e5", "Infinity", "1,509"):
            self.assertEqual(cg.number_state(True, text)["state"], "unreadable", text)
        for raw in (float("nan"), float("-inf"), "nan", False, {"q": 1}):
            self.assertEqual(cg.number_state(True, raw),
                             {"state": "unreadable", "reason": "value_not_number"}, raw)
        # The identifier reading is unchanged: a float customer PO is never text.
        self.assertEqual(cg.value_state(True, 4500123.0)["state"], "unreadable")
        self.assertEqual(cpo.classify_field(True, 4500123.0, {})["status"], "not_inspectable")

    def test_a_float_zero_in_a_target_quantity_is_a_held_value(self):
        target = {**self.AO, "row": {**self.AO["row"], "qtytotal": 0.0}}
        ids, out = self.verdict("1509", target=target)
        self.assertEqual(ids, ["target_quantities_empty"])
        self.assertIn("already holds qtytotal", out["failures"][0]["reason"])
        target["row"]["qtytotal"] = [0]
        ids, out = self.verdict("1509", target=target)
        self.assertIn("could not be read", out["failures"][0]["reason"])


class CurrentReaderTests(unittest.TestCase):
    def test_a_column_outside_the_captures_column_list_is_unreadable(self):
        # The row as stored may carry a null for a column the capture did not serve;
        # the reader keeps only the capture's columns, so that field is unknown.
        class Mirror:
            default_max_staleness_seconds = 900

            def resolve_citation(self, handle, actor, max_staleness=None):
                return {"ok": True, "status": "current", "relation": "public.assembly_order",
                        "key": {"id": 1}, "changed_since": False,
                        "row": {"id": 1, "qtytotal": None, "finalqty": None}}

            def catalog(self, actor):
                return {"relations": [{"name": "public.assembly_order",
                                       "columns": ["id", "qtytotal"]}]}

        class Runtime:
            mirror = Mirror()

        found = cg.current_reader(Runtime(), object())("api://public.assembly_order/x@1-1")
        self.assertEqual(found["row"], {"id": 1, "qtytotal": None})
        self.assertEqual(cg.field_state(found["row"], "qtytotal")[0], "absent")
        self.assertEqual(cg.field_state(found["row"], "finalqty")[0], "unreadable")

        class NoCatalog(Mirror):
            def catalog(self, actor):
                raise RuntimeError("down")
        Runtime.mirror = NoCatalog()
        self.assertIsNone(cg.current_reader(Runtime(), object())("api://x.y/k@1-1"))


class CopiedRowPreconditionTests(PersistedTestCase):
    """Decision 2026-10-04-scope-freshness: a row the mirror filled from the persisted
    capture while the Portal API was out never satisfies an action precondition, even
    when the copy is minutes old and the mirror calls its content current."""

    def test_a_copied_row_is_refused_and_the_same_row_read_live_is_accepted(self):
        self.persist_live()
        mirror = self.running()
        self.transport.down = HTTP_500
        handle = mirror.rows(SALES, "public.app_sales_order")["rows"][0]["citation"]
        cited = mirror.resolve_citation(handle, SALES)
        # The copy is fresh: the mirror's own status is current, no refresh error.
        self.assertEqual((cited["status"], cited["source_freshness"]["label"]),
                         ("current", LABEL), cited)
        self.assertNotIn("error", cited.get("refresh") or {})

        class Runtime:
            pass
        runtime = Runtime()
        runtime.mirror = mirror
        self.assertIsNone(cg.current_reader(runtime, SALES)(handle))

        # Read live, the same relation's row is precondition evidence again.
        live = self.running("live.sqlite", store=None)
        self.transport.down = None
        runtime.mirror = live
        handle = live.rows(SALES, "public.app_sales_order")["rows"][0]["citation"]
        self.assertIsNotNone(cg.current_reader(runtime, SALES)(handle))


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "the cited documents are PDFs: needs pypdf and reportlab")
class DocumentCitationReaderTests(unittest.TestCase):
    """Decision chain-document-evidence (option 2): the guard reader resolves a byte-pinned
    SharePoint file or mail attachment citation of a captured customer PO for the session
    actor, through the case runtime's capture gate, to its re-extracted ``documentFields``
    row, so a missing-in-record draft citing the page passes the customer-PO guards for a
    reader who may read the document and for nobody else. Captures over the capture tests'
    in-process fakes. Evidence label: injected-transport / isolated-fixture."""

    PO = "4500012345"

    def setUp(self):
        from operating.tests import test_attested_missing as am
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pdf = am.pdf(am.customer_po(self.PO, "OR1000777001"))
        self.digest = hashlib.sha256(self.pdf).hexdigest()

    def runtime(self, live_audience, **captures):
        from integrations.people_exclusion import PeopleExclusion
        from integrations.tests import test_capture_mail as tm
        from operating.case_mail_scope import MailScope
        from operating.case_runtime import CaseRuntime, LiveReaders
        from operating.case_tools_host import CaseCaptures
        mirror = self.root / "mirror-audience.json"
        mirror.write_text(json.dumps({"schema": "plx-source-mirror-audience/v1",
                                      "tenant": "plx", "audiences": {}}), encoding="utf-8")
        runtime = CaseRuntime(self.root / "case", client=None, audience=mirror, tenant="plx",
                              live=LiveReaders(audience=live_audience),
                              mail_scope=MailScope.unrestricted(),
                              people_exclusion=PeopleExclusion.from_env(tm.ENV))
        self.addCleanup(runtime.close)
        runtime.captures = CaseCaptures(runtime, **captures)
        return runtime

    def assert_po_row(self, found, relation):
        self.assertEqual(found["relation"], relation)
        row = found["row"]
        self.assertEqual(cg.field_value(row, "fields.po_number.value"), (True, self.PO))
        self.assertEqual(row["content_sha256"], self.digest)
        # What the page states, never a record's own fields.
        self.assertNotIn("order_number", row)

    @staticmethod
    def repinned(handle, digest):
        return handle.rsplit("sha256=", 1)[0] + "sha256=" + digest

    def test_a_pinned_sharepoint_file_resolves_for_its_reader_only(self):
        from integrations import capture_sharepoint as cs, graph_read
        from integrations.captured_text import byte_pinned
        from integrations.graph_read import SharePointReader
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests import test_capture_sharepoint as sp
        drive = sp.seeded_drive()
        drive.file("01CPO", "customer-po.pdf", "01AOS", self.pdf, acl=(sp.RICARDO, sp.SALES))
        drive.file("01CPOR", "customer-po-r.pdf", "01AOS", self.pdf, acl=(sp.RICARDO,))
        state = self.root / "sharepoint"
        store = cs.SharePointCaptureStore(state, clock=sp.Clock())
        self.addCleanup(store.close)
        api, transport = graph_read.build_graph(sp.ENV, sender=drive, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        capture = cs.SharePointCapture(store, lambda d: SharePointReader(
            api, sp.capture_audience(), d, transport=transport, read_permissions=True,
            max_acl_reads=cs.MAX_ACL_READS), sp.CAPTURE)
        self.assertTrue(capture.run(sp.scope())["complete"])
        soon = dt.datetime(2026, 10, 1, 13, 0, tzinfo=dt.timezone.utc)
        runtime = self.runtime(sp.reader_audience(), sharepoint_capture_dir=state,
                               clock=lambda: soon)
        handle = byte_pinned(cs.version_handle(sp.DRIVE, "01CPO", '"{01CPO},1"'),
                             self.digest)
        self.assert_po_row(cg.current_reader(runtime, sp.EMPLOYEE)(handle),
                           "sharepoint.documentFields")
        self.assertIsNone(cg.current_reader(runtime, sp.OUTSIDER)(handle))
        self.assertIsNone(cg.current_reader(runtime, sp.EMPLOYEE)(
            self.repinned(handle, "0" * 64)))
        # The same bytes in a file only Ricardo's identity may open: the item ACL decides.
        restricted = byte_pinned(cs.version_handle(sp.DRIVE, "01CPOR", '"{01CPOR},1"'),
                                 self.digest)
        self.assertIsNone(cg.current_reader(runtime, sp.EMPLOYEE)(restricted))
        self.assert_po_row(cg.current_reader(runtime, sp.QUALITY)(restricted),
                           "sharepoint.documentFields")
        # A page of the file pins no whole document; a later version supersedes this one.
        self.assertIsNone(cg.current_reader(runtime, sp.EMPLOYEE)(handle + "&page=1"))
        drive.change("01CPO", self.pdf + b"\n")
        self.assertTrue(capture.run(sp.scope())["complete"])
        self.assertIsNone(cg.current_reader(runtime, sp.EMPLOYEE)(handle))

    def test_a_pinned_mail_attachment_passes_the_customer_po_guards_for_its_reader_only(self):
        from integrations import graph_read
        from integrations.capture_mail import MailCapture, MailCaptureStore
        from integrations.graph_read import GraphMailReader
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests import test_capture_mail as tm
        graph = tm.FakeGraph()
        graph.put(tm.BOX_A, "MSG-1", "CK-1", subject="Order", body="See PDF.",
                  attachments=[tm.file_attachment("ATT-1", self.pdf, "po.pdf")])
        state = self.root / "mail"
        store = MailCaptureStore(state, clock=tm.Clock())
        self.addCleanup(store.close)
        api, _ = graph_read.build_graph(tm.ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        self.assertTrue(MailCapture(store, GraphMailReader(api, tm.capture_audience()),
                                    tm.CAPTURE, sleep=lambda s: None).run(tm.SCOPE)["complete"])
        (handle,) = [row[0] for row in store.db.execute("SELECT l1_handle FROM attachments")]
        self.assertTrue(handle.endswith("&sha256=" + self.digest), handle)
        runtime = self.runtime(tm.reader_audience(), mail_capture_dir=state)
        self.assert_po_row(cg.current_reader(runtime, tm.READER_A)(handle),
                           "graph_mail.documentFields")
        self.assertIsNone(cg.current_reader(runtime, tm.OUTSIDER)(handle))
        self.assertIsNone(cg.current_reader(runtime, tm.READER_A)(
            self.repinned(handle, "0" * 64)))
        # The message itself pins no document bytes; a host without captures reads none.
        self.assertIsNone(cg.current_reader(runtime, tm.READER_A)(handle.split("#")[0]))
        captures, runtime.captures = runtime.captures, None
        self.assertIsNone(cg.current_reader(runtime, tm.READER_A)(handle))
        runtime.captures = captures

        # The missing-in-record draft (operating/attested_conflicts.py) against an order
        # holding no PO: every guard holds for the reader of the document; for anyone
        # else its evidence is unreadable and nothing would be recorded.
        order = "api://public.app_sales_order/so1@7-0123456789abcdef"
        payload = {"proposed_customer_po": self.PO, "evidence_citation": handle,
                   "evidence_field": "fields.po_number.value"}
        effect = {"relation": "public.app_sales_order", "key": {"id": "so1"},
                  "values": {"po_reference": self.PO}}

        def reader(actor):
            documents = cg.current_reader(runtime, actor)
            return lambda cited: ({"relation": "public.app_sales_order", "key": {"id": "so1"},
                                   "row": {"id": "so1", "order_number": "OR1000777001",
                                           "po_reference": None, "custom_fields": {}}}
                                  if cited == order else documents(cited))

        verdict = cg.check(CAPABILITY, payload, effect, [handle, order], reader(tm.READER_A))
        self.assertTrue(verdict["passed"], verdict)
        refused = cg.check(CAPABILITY, payload, effect, [handle, order], reader(tm.OUTSIDER))
        self.assertEqual({f["id"] for f in refused["failures"]},
                         {"value_in_cited_evidence", "evidence_value_is_not_its_own_number"})
        wrong = cg.check(CAPABILITY, dict(payload, proposed_customer_po="4500099999"), dict(
            effect, values={"po_reference": "4500099999"}), [handle, order],
            reader(tm.READER_A))
        self.assertEqual([f["id"] for f in wrong["failures"]], ["value_in_cited_evidence"])


class CustomerPoGuardTests(CustomerPoEvalBase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        [self.order] = self.coverage(origins=["sage_import"],
                                     statuses=["absent_in_this_source"])["orders"]
        rows = self.actual("mirror.rows", {"relation": cpo.RELATION, "limit": 50})["rows"]
        self.has_po = next(r for r in rows if r["row"].get("po_reference") == "PO-5501")
        sage = self.actual("mirror.rows", {"relation": "sage.oe_order_header",
                                           "where": [["ordnumber", "eq", "S3"]]})
        self.evidence = sage["rows"][0]["citation"]

    def propose(self, target, po, *, field="ponumber", evidence=None):
        key = target["key"]
        number = target["row"]["order_number"] if "row" in target else target["order_number"]
        case_id = self.actual("plx.case_open", {"question": "Missing customer POs",
                                                "roots": [str(number)]})["case_id"]
        evidence = evidence or self.evidence
        arguments = {
            "case_id": case_id, "capability": CAPABILITY,
            "payload": {"proposed_customer_po": po, "evidence_citation": evidence,
                        "evidence_field": field},
            "citations": [evidence, target["citation"]],
            "expected_effect": {"relation": cpo.RELATION, "key": key,
                                "fields": ["po_reference"], "equals": po},
            "evidence": {"statement": f"The cited Sage order header holds {po} in ponumber; "
                                      f"it is proposed as the customer PO of sales order "
                                      f"{number}."}}
        return case_id, arguments

    def refused(self, arguments) -> str:
        return self.h.refused(self.server, "plx.case_propose", arguments)

    def test_the_three_wrong_proposals_of_the_review_are_refused(self):
        for target, po, guard in (
                (self.order, "PO-9911", "value_in_cited_evidence"),
                (self.has_po, "PO-7788", "target_has_no_po"),
                (self.order, str(self.order["order_number"]), "value_is_not_the_order_number")):
            with self.subTest(guard=guard):
                case_id, arguments = self.propose(target, po)
                text = self.refused(arguments)
                self.assertIn("capability_guard_failed:", text)
                self.assertIn(guard, text)
                view = self.call("plx.case_get", {"case_id": case_id})
                self.assertEqual((view["proposals"], view["findings"]["supported"]), ([], []))

    def test_the_order_number_in_another_case_is_still_the_order_number(self):
        number = str(self.order["order_number"])
        for row in self.h.fake.tables["sage.oe_order_header"]:
            if row.get("ordnumber") == "S3":
                row.update(ponumber=number.lower(), audtdate="20260930")
        self.h.clock.advance(3600)
        sage = self.actual("mirror.rows", {"relation": "sage.oe_order_header",
                                           "where": [["ordnumber", "eq", "S3"]],
                                           "max_staleness": 0})
        self.assertEqual(sage["rows"][0]["row"]["ponumber"], number.lower())
        _, arguments = self.propose(self.order, number.lower(),
                                    evidence=sage["rows"][0]["citation"])
        self.assertIn("value_is_not_the_order_number", self.refused(arguments))

    def test_missing_evidence_keys_and_a_foreign_evidence_row_are_refused(self):
        _, arguments = self.propose(self.order, "PO-7788")
        del arguments["payload"]["evidence_field"]
        self.assertIn("payload_required", self.refused(arguments))
        _, arguments = self.propose(self.order, "PO-7788", field="ordnumber")
        self.assertIn("value_in_cited_evidence", self.refused(arguments))
        _, arguments = self.propose(self.order, "PO-7788")
        arguments["citations"] = [self.order["citation"]]  # evidence not cited
        self.assertIn("value_in_cited_evidence", self.refused(arguments))

    def test_a_supported_proposal_passes_and_records_the_verdict(self):
        case_id, arguments = self.propose(self.order, "PO-7788")
        receipt = self.actual("plx.case_propose", arguments)
        self.assertEqual((receipt["capability_guards"]["guarded"],
                          receipt["capability_guards"]["passed"]), (True, True))
        self.record_po_gaps(case_id, [self.order])
        report = pe.evaluate(CPO, self.call, case_id, transcript=self.actual.names)
        self.assertTrue(report["summary"]["passed_all"], report["checks"])
        self.assertEqual(report["authority_violations"], 0)

    def test_the_evaluator_rechecks_guards_on_a_proposal_recorded_around_the_tool(self):
        # A proposal written straight into the case store (no tool, so no guard) and one
        # under an unguarded capability name both fail the evaluation.
        runtime, actor = self.server.case_runtime, self.server.actor
        for capability, po, kind in ((CAPABILITY, "PO-9911", "capability_guard_failed"),
                                     ("portal.sales_order.set_po", "PO-7788",
                                      "proposal_capability_undeclared"),
                                     ("fixture.record.set_field", "PO-7788",
                                      "proposal_capability_test_only")):
            with self.subTest(capability=capability):
                case_id, arguments = self.propose(self.order, po)
                runtime.cases.propose(
                    actor, case_id, capability, dict(arguments["payload"]),
                    [], arguments["citations"], expected_effect=arguments["expected_effect"],
                    evidence={"statement": arguments["evidence"]["statement"]})
                self.record_po_gaps(case_id, [self.order])
                report = pe.evaluate(CPO, self.call, case_id, transcript=self.actual.names)
                self.assertFalse(report["summary"]["passed_all"])
                kinds = {v["kind"] for v in report["authority_violation_details"]}
                self.assertIn(kind, kinds)

    def test_only_canonical_declared_capability_names_are_recorded(self):
        # 'name ' skipped the guards (lookup by raw text, store
        # by trimmed text). One canonical spelling everywhere; anything else is refused.
        for capability, code in ((CAPABILITY + " ", "capability_not_canonical"),
                                 (CAPABILITY.upper(), "capability_not_canonical"),
                                 (" " + CAPABILITY, "capability_not_canonical"),
                                 ("portal.sales_order.set_po_reference", "capability_undeclared")):
            with self.subTest(capability=capability):
                case_id, arguments = self.propose(self.order, "PO-7788")
                arguments["capability"] = capability
                self.assertIn(code, self.refused(arguments))
                self.assertEqual(self.call("plx.case_get", {"case_id": case_id})["proposals"], [])
        from ontology.core import ValidationError
        case_id, arguments = self.propose(self.order, "PO-7788")
        with self.assertRaisesRegex(ValidationError, "capability_not_canonical"):
            self.server.case_runtime.cases.propose(
                self.server.actor, case_id, CAPABILITY + " ", arguments["payload"], [],
                arguments["citations"])

    def test_the_effect_may_write_only_the_capabilitys_fields(self):
        # fields ['order_number'] with equals 'PO-7788' passed every guard.
        for fields in (["order_number"], ["po_reference", "order_number"], []):
            with self.subTest(fields=fields):
                _, arguments = self.propose(self.order, "PO-7788")
                arguments["expected_effect"]["fields"] = fields
                text = self.refused(arguments)
                self.assertIn("capability_guard_failed", text)
                self.assertIn("effect_fields_writable", text)
        _, arguments = self.propose(self.order, "PO-7788")
        arguments["expected_effect"]["fields"] = ["custom_fields.custPoCode"]
        self.assertTrue(self.actual("plx.case_propose", arguments)["capability_guards"]["passed"])

    def test_a_target_po_field_that_cannot_be_read_refuses(self):
        # Unparseable JSON, a non-object, or a missing column is not "no PO".
        for value in ('{"custPoCode": "PO-4444", "sage', ["PO-4444"]):
            with self.subTest(value=value):
                for row in self.h.fake.tables[cpo.RELATION]:
                    if row["id"] == self.order["key"]["id"]:
                        row["custom_fields"] = value
                        row["updated_at"] = "2026-09-29T11:30:00"
                self.h.clock.advance(3600)
                rows = self.actual("mirror.rows", {"relation": cpo.RELATION, "limit": 50})
                target = next(r for r in rows["rows"] if r["key"] == self.order["key"])
                _, arguments = self.propose(target, "PO-7788")
                text = self.refused(arguments)
                self.assertIn("target_has_no_po", text)
                self.assertIn("could not be read", text)

    def test_act_propose_refuses_a_proposal_its_guards_would_refuse(self):
        from operating.action_path import ActionPath
        from operating.mcp_server import MCPServer, local_client
        runtime = self.server.case_runtime
        case_id, arguments = self.propose(self.order, "PO-7788")
        arguments["expected_effect"]["fields"] = ["order_number"]
        # Recorded around the tool (straight into the case store): registration refuses.
        receipt = runtime.cases.propose(
            self.server.actor, case_id, CAPABILITY, arguments["payload"],
            [{"check": "field_empty", "citation": self.order["citation"],
              "field": "po_reference"}], arguments["citations"],
            expected_effect=arguments["expected_effect"],
            evidence={"statement": arguments["evidence"]["statement"]})
        path = ActionPath(self.h.root / "actions-fields.db", runtime.cases)
        call = local_client(MCPServer(case_runtime=runtime, actor=self.server.actor,
                                      action_path=path), "actions")
        with self.assertRaises(RuntimeError) as caught:
            call("plx.act", {"mode": "propose", "case_id": case_id,
                             "proposal_id": receipt["proposal_id"]})
        self.assertIn("effect_fields_writable", str(caught.exception))
        self.record_po_gaps(case_id, [self.order])
        report = pe.evaluate(CPO, self.call, case_id, transcript=self.actual.names)
        self.assertFalse(report["summary"]["passed_all"])
        self.assertIn("capability_guard_failed",
                      {v["kind"] for v in report["authority_violation_details"]})

    def test_act_propose_rechecks_the_guards_on_current_rows(self):
        from operating.action_path import ActionPath
        from operating.mcp_server import MCPServer, local_client
        runtime = self.server.case_runtime
        case_id, arguments = self.propose(self.order, "PO-7788")
        arguments["preconditions"] = [{"check": "field_empty", "citation": self.order["citation"],
                                       "field": "po_reference"}]
        proposal = self.actual("plx.case_propose", arguments)["proposal"]
        path = ActionPath(self.h.root / "actions.db", runtime.cases)
        call = local_client(MCPServer(case_runtime=runtime, actor=self.server.actor,
                                      action_path=path), "actions")
        # The order gains a PO in the source after the proposal was recorded.
        for row in self.h.fake.tables[cpo.RELATION]:
            if row["id"] == self.order["key"]["id"]:
                row.update(po_reference="PO-1234", updated_at="2026-09-29T11:30:00")
        self.h.clock.advance(3600)
        with self.assertRaises(RuntimeError) as caught:
            call("plx.act", {"mode": "propose", "case_id": case_id,
                             "proposal_id": proposal["proposal_id"]})
        self.assertIn("capability_guard_failed", str(caught.exception))

    # Review wh-2 (minor 1): evidence from another row is asserted by the proposer,
    # never verified; the record always carries that a person confirms it belongs.
    def test_every_proposal_records_the_person_precondition_and_its_label(self):
        [text] = cg.person_preconditions(CAPABILITY)
        self.assertIn("a person at PLX confirms the evidence belongs to this order", text)
        self.assertIn("asserted_by_proposer_not_verified", text)
        case_id, arguments = self.propose(self.order, "PO-7788")
        result = self.call("plx.case_propose", arguments)
        self.assertEqual(result["person_preconditions"], [text])
        [proposal] = self.call("plx.case_get", {"case_id": case_id})["proposals"]
        self.assertIn(text, proposal["preconditions"])
        # Stated already: recorded once.
        case_id, arguments = self.propose(self.order, "PO-7788")
        arguments["preconditions"] = [text]
        self.call("plx.case_propose", arguments)
        [proposal] = self.call("plx.case_get", {"case_id": case_id})["proposals"]
        self.assertEqual(proposal["preconditions"].count(text), 1)

    def test_a_proposal_recorded_without_it_is_an_evaluator_violation(self):
        case_id, arguments = self.propose(self.order, "PO-7788")
        runtime, actor = self.server.case_runtime, self.server.actor
        runtime.cases.propose(actor, case_id, CAPABILITY, arguments["payload"], [],
                              arguments["citations"],
                              expected_effect=arguments["expected_effect"])
        report = pe.evaluate(CPO, self.call, case_id)
        self.assertIn("person_precondition_missing",
                      {v["kind"] for v in report["authority_violation_details"]})
        self.assertFalse(report["summary"]["passed_all"])


AO_CAPABILITY = "portal.assembly_order.set_header_quantities"


class AoGuardTests(AoEvalCase):
    """The AO header capability tied neither its effect to its
    payload nor its value to evidence; 99999, 'abc', a payload/effect mismatch, an empty
    payload and a wrong key were all recorded. One value per field, each in the payload
    (numeric), each equal to a declared cited evidence field of this AO."""

    def setUp(self) -> None:
        super().setUp()
        self.out = self.investigate()
        self.ao = self.call("plx.source_citation", {"citation": self.out["cite"]["ao"]})
        self.item = self.out["cite"]["item"]

    def arguments(self, values=None, payload=None, evidence=None, key=None, cites=()):
        values = {"qtytotal": 1509, "finalqty": "1503"} if values is None else values
        evidence = {"qtytotal": {"citation": self.item, "field": "quantity"},
                    "finalqty": {"citation": self.item, "field": "actual_amount"}} \
            if evidence is None else evidence
        payload = {"assembly_order": "AO-26005B", "qtytotal": "1509", "finalqty": "1503.0",
                   "evidence": evidence} if payload is None else payload
        return {"case_id": self.out["case_id"], "capability": AO_CAPABILITY,
                "payload": payload, "preconditions": ["Quality confirms the signed packet"],
                "citations": [self.out["cite"]["ao"], self.item, *cites],
                "expected_effect": {"relation": self.ao["relation"],
                                    "key": key or self.ao["key"], "values": values}}

    def refused(self, arguments) -> str:
        result = self.h.call(self.server, "plx.case_propose", arguments)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    def proposals(self):
        return self.call("plx.case_get", {"case_id": self.out["case_id"],
                                          "section": "proposals"})["items"]

    def test_a_supported_two_field_proposal_passes(self):
        receipt = self.call("plx.case_propose", self.arguments())
        self.assertEqual((receipt["capability_guards"]["guarded"],
                          receipt["capability_guards"]["passed"]), (True, True))
        [proposal] = self.proposals()
        self.assertEqual(proposal["expected_effect"]["values"],
                         {"qtytotal": 1509, "finalqty": "1503"})

    def test_the_reviewed_wrong_proposals_are_refused(self):
        item = {"citation": self.item, "field": "quantity"}
        cases = {
            "unsupported value": (self.arguments(
                values={"qtytotal": "99999"},
                payload={"qtytotal": "99999", "evidence": {"qtytotal": item}}),
                "values_in_cited_evidence"),
            "not a number": (self.arguments(
                values={"qtytotal": "abc"},
                payload={"qtytotal": "abc", "evidence": {"qtytotal": item}}),
                "payload_carries_the_written_values"),
            "payload and effect differ": (self.arguments(
                values={"qtytotal": "7"},
                payload={"qtytotal": "1509", "evidence": {"qtytotal": item}}),
                "payload_carries_the_written_values"),
            "field written but not in the payload": (self.arguments(
                payload={"qtytotal": "1509", "evidence": {"qtytotal": item}}),
                "payload_carries_the_written_values"),
            "empty payload": (self.arguments(payload={}), "payload_required"),
            "wrong key": (self.arguments(key={"id": 99}), "effect_targets_cited_ao"),
            "no evidence for one field": (self.arguments(
                evidence={"qtytotal": item}), "values_in_cited_evidence"),
            "evidence field of the wrong value": (self.arguments(evidence={
                "qtytotal": {"citation": self.item, "field": "actual_amount"},
                "finalqty": {"citation": self.item, "field": "actual_amount"}}),
                "values_in_cited_evidence"),
            "uncited evidence": (self.arguments(evidence={
                "qtytotal": {"citation": self.item.replace("@", "x@"), "field": "quantity"},
                "finalqty": {"citation": self.item, "field": "actual_amount"}}),
                "values_in_cited_evidence"),
            "a field it does not write": (self.arguments(
                values={"status": "Closed"},
                payload={"status": "Closed", "evidence": {"status": item}}),
                "effect_fields_writable"),
        }
        for label, (arguments, guard) in cases.items():
            with self.subTest(label):
                text = self.refused(arguments)
                self.assertIn("capability_guard_failed", text)
                self.assertIn(guard, text)
        self.assertEqual(self.proposals(), [])

    def test_the_evidence_must_be_a_declared_source_of_this_ao(self):
        # The FM link qtymfg_c (1503) is never the produced figure; the AO row's own
        # cells are no evidence for it; a component line of another ratio is not FG.
        link = self.out["cite"]["link"]
        box = next(row["citation"] for row in self.call("mirror.rows", {
            "relation": "silver.assembly_order_items", "where": [["id", "eq", 58386]]})[
            "rows"])
        for label, values, source, cites, reason in (
                ("link calculation", {"finalqty": "1503"},
                 {"citation": link, "field": "qtymfg_c"}, [link], "not a declared source"),
                ("the target row", {"qtytotal": "1534"},
                 {"citation": self.out["cite"]["ao"], "field": "idfinprod"}, [],
                 "the target row itself"),
                ("a 0.25 per unit component", {"qtytotal": "378"},
                 {"citation": box, "field": "quantity"}, [box], "condition")):
            with self.subTest(label):
                field = next(iter(values))
                text = self.refused(self.arguments(
                    values=values, cites=cites,
                    payload={field: values[field], "evidence": {field: source}}))
                self.assertIn("values_in_cited_evidence", text)
                self.assertIn(reason, text)

    def test_the_evidence_row_must_belong_to_the_target_ao(self):
        # A one-per-unit line of another AO holding the same number is not this AO's.
        for row in self.h.fake.tables["silver.assembly_order_items"]:
            if row["id"] == 58383:
                row["assembly_order_id"] = 10719
        self.h.clock.advance(3600)
        rows = self.call("mirror.rows", {"relation": "silver.assembly_order_items",
                                         "where": [["id", "eq", 58383]],
                                         "max_staleness": 0})["rows"]
        other = rows[0]["citation"]
        text = self.refused(self.arguments(
            values={"qtytotal": "1509"}, cites=[other],
            payload={"qtytotal": "1509",
                     "evidence": {"qtytotal": {"citation": other, "field": "quantity"}}}))
        self.assertIn("does not belong to the target row", text)

    def test_an_omitted_column_is_unreadable_not_empty(self):
        # The upstream stops serving finalqty: the capture's column list lacks it, so
        # the target field is unknown and the header write is refused (not "empty").
        self.h.fake.catalog["public.assembly_order"]["columns"] = [
            name for name in self.h.fake.catalog["public.assembly_order"]["columns"]
            if name != "finalqty"]
        self.h.clock.advance(3600)
        self.server.case_runtime.mirror.refresh(self.server.actor, "public.assembly_order",
                                                full=True)
        rows = self.call("mirror.rows", {"relation": "public.assembly_order",
                                         "where": [["id", "eq", 10718]]})["rows"]
        self.assertIsNone(rows[0]["row"].get("finalqty"))  # the mirror view shows null
        current = rows[0]["citation"]
        arguments = self.arguments(values={"qtytotal": "1509"}, payload={
            "qtytotal": "1509",
            "evidence": {"qtytotal": {"citation": self.item, "field": "quantity"}}})
        arguments["citations"][0] = current
        text = self.refused(arguments)
        self.assertIn("target_quantities_empty", text)
        self.assertIn("could not be read (finalqty)", text)

    def test_the_fixture_capability_is_not_offered_on_the_case_host(self):
        arguments = self.arguments()
        arguments["capability"] = "fixture.record.set_field"
        self.assertIn("capability_not_available_on_this_host", self.refused(arguments))

class ActionRegistrationTests(CustomerPoEvalBase, unittest.TestCase):
    def test_register_refuses_an_undeclared_capability(self):
        from operating.action_path import ActionPath, ActionPathError
        from operating.session_identity import CHANNEL_MCP, fixture_session
        [order] = self.coverage(origins=["sage_import"],
                                statuses=["absent_in_this_source"])["orders"]
        case_id = self.actual("plx.case_open", {"question": "q"})["case_id"]
        runtime, actor = self.server.case_runtime, self.server.actor
        receipt = runtime.cases.propose(
            actor, case_id, "portal.sales_order.set_po", {"v": 1},
            [{"check": "field_empty", "citation": order["citation"], "field": "po_reference"}],
            [order["citation"]],
            expected_effect={"relation": cpo.RELATION, "key": order["key"],
                             "values": {"po_reference": "PO-7788"}})
        path = ActionPath(self.h.root / "actions-undeclared.db", runtime.cases)
        # One source: the catalog hook refuses a type that is not in the catalog.
        with self.assertRaisesRegex(ActionPathError, "action_type_not_in_catalog"):
            path.register(actor, case_id, receipt["proposal_id"], session=fixture_session(
                actor.tenant, actor.principals, channel=CHANNEL_MCP))


class GapGuardTests(CustomerPoEvalBase, unittest.TestCase):
    """Reviews wh-2 and wh-3 (material): an absence gap is a structured claim. It must
    name what is absent (property, schema names), and before it is recorded every cited
    row is re-read on every field the schema binds that property to (for
    SalesOrder.customer_po on a Portal order: po_reference and the FM-migration key
    custom_fields.custPoCode). Wording never decides anything."""

    def setUp(self) -> None:
        super().setUp()
        rows = self.actual("mirror.rows", {"relation": cpo.RELATION, "limit": 50})["rows"]
        self.by_number = {row["row"]["order_number"]: row for row in rows}
        self.case_id = self.actual("plx.case_open", {
            "question": "Missing customer POs", "roots": ["OR0001"]})["case_id"]

    def gap(self, number, state="absent_in_source", prop="SalesOrder.customer_po",
            citations=None, missing=None):
        arguments = {"case_id": self.case_id, "state": state,
                     "missing": missing or f"Customer PO of sales order {number}",
                     "why": "The cited order shows no customer PO.",
                     "who_can_answer": ["PLX order entry"],
                     "citations": citations or [self.by_number[number]["citation"]]}
        if prop is not None:
            arguments["property"] = prop
        return arguments

    def gaps(self):
        return self.call("plx.case_get", {"case_id": self.case_id})["gaps"]

    def number(self, test):
        return next(n for n, r in sorted(self.by_number.items()) if test(r["row"]))

    def test_the_schema_declares_every_carrier_of_the_customer_po(self):
        subject, reason = cg.resolve_gap_subject(shared_schema(), "SalesOrder.customer_po")
        self.assertIsNone(reason)
        carriers = {item["path"] for item in subject["entries"][0]["sources"]
                    if item["relation"] == cpo.RELATION}
        self.assertEqual(carriers, {"po_reference", "custom_fields.custPoCode"})
        for about in ("SalesOrder.no_such_property", "NoSuchType.customer_po",
                      ["SalesOrder.customer_po", "PurchaseOrder.po_number"],
                      ["SalesOrder", "SalesOrder.customer_po"], []):
            self.assertIsNone(cg.resolve_gap_subject(shared_schema(), about)[0], about)
        links = cg.resolve_gap_subject(shared_schema(), ["PurchaseOrder.receipts",
                                                         "PurchaseOrder.ordered_from"])[0]
        self.assertEqual({e["kind"] for e in links["entries"]}, {"link"})

    def test_a_reworded_absence_without_property_is_refused(self):
        text = self.h.refused(self.server, "plx.case_record_gap", self.gap(
            "OR0001", prop=None, missing="No PO on file for OR0001"))
        self.assertIn("gap_guard_failed:absence_needs_property", text)
        self.assertIn("pass property", text)
        self.assertEqual(self.gaps(), [])
        # Other states need no property.
        self.call("plx.case_record_gap", self.gap("OR0001", state="unknown", prop=None))

    def test_absent_in_source_on_an_order_holding_a_po_is_refused(self):
        self.assertEqual(self.by_number["OR0001"]["row"]["po_reference"], "PO-5501")
        text = self.h.refused(self.server, "plx.case_record_gap", self.gap("OR0001"))
        self.assertIn("gap_guard_failed:gap_contradicted_by_cited_evidence", text)
        self.assertIn("po_reference", text)
        self.assertNotIn("PO-5501", text)  # the value is never echoed
        self.assertEqual(self.gaps(), [])

    def test_a_po_held_only_in_custom_fields_cust_po_code_refuses_the_gap(self):
        number = self.number(lambda row: row.get("po_reference") is None and isinstance(
            row.get("custom_fields"), (dict, str)) and "PO12172025" in str(
                row.get("custom_fields")))
        text = self.h.refused(self.server, "plx.case_record_gap", self.gap(number))
        self.assertIn("gap_contradicted_by_cited_evidence", text)
        self.assertIn("custom_fields.custPoCode", text)
        self.assertNotIn("PO12172025", text)
        self.assertEqual(self.gaps(), [])

    def test_custom_fields_that_are_not_json_refuse_the_gap(self):
        number = self.number(lambda row: row.get("custom_fields") == "not json")
        text = self.h.refused(self.server, "plx.case_record_gap", self.gap(number))
        self.assertIn("gap_property_unreadable", text)
        self.assertEqual(self.gaps(), [])

    def test_a_name_in_the_po_field_is_not_absence_but_may_be_unknown(self):
        number = self.number(lambda row: row.get("po_reference") == "Jane Buyer")
        text = self.h.refused(self.server, "plx.case_record_gap", self.gap(number))
        self.assertIn("gap_contradicted_by_cited_evidence", text)
        recorded = self.call("plx.case_record_gap", self.gap(number, state="unknown"))
        self.assertEqual(recorded["gap_guard"]["property"], ["SalesOrder.customer_po"])

    def test_absent_in_source_on_an_empty_order_is_recorded_with_its_property(self):
        number = self.number(lambda row: "po_reference" in row and row["po_reference"] is None
                             and row.get("custom_fields") is None)
        recorded = self.call("plx.case_record_gap", self.gap(number))
        self.assertEqual(recorded["gap_guard"]["checked"],
                         [{"property": "SalesOrder.customer_po",
                           "citation": self.by_number[number]["citation"],
                           "fields": {"po_reference": "absent",
                                      "custom_fields.custPoCode": "absent"}}])
        [gap] = self.gaps()
        self.assertEqual((gap["state"], gap["property"]),
                         ("absent_in_source", ["SalesOrder.customer_po"]))

    def test_an_undeclared_property_or_an_unbound_citation_is_refused(self):
        number = self.number(lambda row: "po_reference" in row and row["po_reference"] is None
                             and row.get("custom_fields") is None)
        text = self.h.refused(self.server, "plx.case_record_gap",
                              self.gap(number, prop="SalesOrder.po_text"))
        self.assertIn("gap_property_undeclared", text)
        subject, _ = cg.resolve_gap_subject(shared_schema(), "SalesOrder.customer_po")
        bound = {item["relation"] for item in subject["entries"][0]["sources"]}
        listing = self.server.case_runtime.mirror.catalog(self.server.actor)["relations"]
        relation = next(item["name"] for item in listing
                        if item["name"] not in bound and item["name"].startswith("public."))
        other = self.actual("mirror.rows", {"relation": relation, "limit": 1})["rows"]
        self.assertTrue(other, relation)
        text = self.h.refused(self.server, "plx.case_record_gap", self.gap(
            number, citations=[other[0]["citation"]]))
        self.assertIn("gap_property_not_in_cited_rows", text)
        self.assertEqual(self.gaps(), [])

    def test_the_guard_reads_every_carrier_as_it_is_now(self):
        found = {"relation": cpo.RELATION, "key": {"id": 3},
                 "row": {"po_reference": "", "custom_fields": {"fmId": 1}}}
        schema = shared_schema()

        def verdict(row=None, read=None, about="SalesOrder.customer_po",
                    state="absent_in_source"):
            return cg.check_gap(state, about, ["api://h"], read or (
                lambda handle: dict(found, row=row) if row is not None else found), schema)

        self.assertTrue(verdict()["passed"])
        self.assertEqual([f["id"] for f in verdict(read=lambda handle: None)["failures"]],
                         ["gap_evidence_unreadable"])
        for row in ({"po_reference": ""},  # the JSON column is not carried
                    {"po_reference": "", "custom_fields": "{bad"},
                    {"po_reference": "", "custom_fields": "[1]"}):
            self.assertEqual([f["id"] for f in verdict(row)["failures"]],
                             ["gap_property_unreadable"], row)
        self.assertEqual([f["id"] for f in verdict(
            {"po_reference": None, "custom_fields": '{"custPoCode": "PO-1"}'})["failures"]],
            ["gap_contradicted_by_cited_evidence"])
        # Other states need no proof of absence; absence always needs a property.
        self.assertTrue(verdict(read=lambda handle: None, state="not_ingested")["passed"])
        self.assertEqual(verdict(about=None)["failures"][0]["id"], "absence_needs_property")
        self.assertFalse(verdict(about=None, state="unknown")["guarded"])

    def test_a_po_field_holding_the_orders_own_number_is_a_placeholder_never_a_value(self):
        """Review wh-4 (material): FM-ensure copies custpocode = po_reference ??
        order_number, so a PO field can hold the order's own number. The schema names the
        order's own identifiers (placeholder_when_equal_to); such a value is a
        self_reference, absent for the customer PO, and reported with what it equals."""
        schema = shared_schema()

        def verdict(row):
            return cg.check_gap("absent_in_source", "SalesOrder.customer_po", ["api://h"],
                                lambda handle: {"relation": cpo.RELATION, "key": {"id": 3},
                                                "row": row}, schema)

        own = verdict({"order_number": "SO9", "po_reference": " SO9 ", "custom_fields": None})
        self.assertTrue(own["passed"], own)
        self.assertEqual(own["checked"][0]["fields"],
                         {"po_reference": "self_reference",
                          "custom_fields.custPoCode": "absent"})
        self.assertEqual(own["checked"][0]["self_references"], {"po_reference": ["order_number"]})
        renumbered = verdict({"order_number": "SO10", "po_reference": None, "custom_fields": {
            "fmOeNumber": "SO9", "renumberedTo": "SO10", "custPoCode": "SO9"}})
        self.assertTrue(renumbered["passed"], renumbered)
        self.assertEqual(renumbered["checked"][0]["self_references"],
                         {"custom_fields.custPoCode": ["custom_fields.fmOeNumber"]})
        # Another order's number, or an own number the row does not carry, is a value.
        for row in ({"order_number": "SO9", "po_reference": "SO8", "custom_fields": None},
                    {"po_reference": "SO9", "custom_fields": None}):
            self.assertEqual([f["id"] for f in verdict(row)["failures"]],
                             ["gap_contradicted_by_cited_evidence"], row)

    def test_a_carrier_holding_a_non_text_value_is_unreadable_never_absent(self):
        """Review wh-4 (minor): a float, boolean, list or object in a carrier says
        something the guard cannot read: the absence is refused, never recorded."""
        schema = shared_schema()
        for row in ({"po_reference": None, "custom_fields": {"custPoCode": 4500123.0}},
                    {"po_reference": None, "custom_fields": {"custPoCode": ["PO-77"]}},
                    {"po_reference": None, "custom_fields": '{"custPoCode": {"po": "1"}}'},
                    {"po_reference": True, "custom_fields": None},
                    {"po_reference": 12.5, "custom_fields": None}):
            verdict = cg.check_gap("absent_in_source", "SalesOrder.customer_po", ["api://h"],
                                   lambda handle, row=row: {"relation": cpo.RELATION,
                                                            "key": {"id": 3}, "row": row},
                                   schema)
            self.assertEqual([f["id"] for f in verdict["failures"]],
                             ["gap_property_unreadable"], row)
            self.assertEqual(cg.field_state(row, "custom_fields.custPoCode")[0]
                             if row["po_reference"] is None
                             else cg.field_state(row, "po_reference")[0], "unreadable", row)
        # An integer is text to the Portal (String(v)): a value.
        self.assertEqual(cg.field_state({"custom_fields": {"custPoCode": 4500123}},
                                        "custom_fields.custPoCode"), ("value", 4500123))

    def test_the_guard_and_the_order_classification_never_disagree(self):
        """One field classification (capability_guards.value_state) for both: an order
        is absent_in_this_source exactly when the gap guard records its absence."""
        schema = shared_schema()
        rows = [
            {"order_number": "SO1", "po_reference": None, "custom_fields": None},
            {"order_number": "SO1", "po_reference": "  ", "custom_fields": "{}"},
            {"order_number": "SO1", "po_reference": "SO1", "custom_fields": None},
            {"order_number": "SO1", "po_reference": None,
             "custom_fields": {"custPoCode": "SO1"}},
            {"order_number": "SO2", "po_reference": "SO1",
             "custom_fields": {"fmOeNumber": "SO1"}},
            {"order_number": "SO2", "po_reference": "SO1",
             "custom_fields": {"renumberedTo": "SO1"}},
            {"order_number": "SO1", "po_reference": "PO-5501", "custom_fields": None},
            {"order_number": "SO1", "po_reference": "Jane Buyer", "custom_fields": None},
            {"order_number": "SO1", "po_reference": None,
             "custom_fields": {"custPoCode": 4500123.0}},
            {"order_number": "SO1", "po_reference": None,
             "custom_fields": {"custPoCode": 4500123}},
            {"order_number": "SO1", "po_reference": False, "custom_fields": None},
            {"order_number": "SO1", "po_reference": None, "custom_fields": "not json"},
            {"order_number": "SO1", "po_reference": None, "custom_fields": "[1]"},
            {"order_number": "SO1", "po_reference": None},
            {"order_number": "SO1", "custom_fields": None},
            {"po_reference": "SO1", "custom_fields": None},
        ]
        absent = 0
        for row in rows:
            classified = cpo.classify_order(row, list(row))["status"] == "absent_in_this_source"
            verdict = cg.check_gap("absent_in_source", "SalesOrder.customer_po", ["api://h"],
                                   lambda handle, row=row: {"relation": cpo.RELATION,
                                                            "key": {"id": 1}, "row": row},
                                   schema)
            self.assertEqual(verdict["passed"], classified, row)
            absent += classified
        self.assertEqual(absent, 6)
        # Both read the same declaration: the schema's carriers and own identifiers.
        found = cg.property_carriers(schema.object_types, "SalesOrder", "customer_po")
        self.assertEqual({p for p, _, _ in cpo.CANDIDATE_FIELDS},
                         {i["path"] for i in found["sources"] if i["relation"] == cpo.RELATION})
        self.assertEqual({p for p, _, _ in cpo.ORDER_IDENTIFIERS},
                         {i["path"] for i in found["placeholders"]
                          if i["relation"] == cpo.RELATION})

    def test_link_and_object_absences_need_their_subject_row(self):
        schema = shared_schema()
        po_row = {"relation": "public.app_purchase_order", "key": {"id": 1},
                  "row": {"id": 1}}
        other = {"relation": cpo.RELATION, "key": {"id": 1}, "row": {"id": 1}}
        link = cg.check_gap("absent_in_source", "PurchaseOrder.receipts", ["api://h"],
                            lambda handle: po_row, schema)
        self.assertTrue(link["passed"], link)
        self.assertEqual(link["checked"], [{"property": "PurchaseOrder.receipts",
                                            "kind": "link", "rechecked": False}])
        self.assertEqual(cg.check_gap("absent_in_source", "PurchaseOrder.receipts",
                                      ["api://h"], lambda handle: other, schema)
                         ["failures"][0]["id"], "gap_subject_not_cited")
        # The object itself is absent only when no cited row is one of it, and an
        # absence citing nothing shows nothing: refused, whatever the type.
        line = {"relation": "public.app_purchase_order_line", "key": {"id": 2},
                "row": {"id": 2, "purchase_order_id": None}}
        held = cg.check_gap("absent_in_source", "PurchaseOrder", ["api://h"],
                            lambda handle: line, schema)
        self.assertTrue(held["passed"], held)
        self.assertEqual(held["checked"], [{"property": "PurchaseOrder", "kind": "object",
                                            "citation": "api://h",
                                            "fields": {"purchase_order_id": "absent"}}])
        for about in ("PurchaseOrder", "SalesOrder", "PurchaseOrder.receipts"):
            uncited = cg.check_gap("absent_in_source", about, [], lambda handle: None, schema)
            self.assertFalse(uncited["passed"], about)
            self.assertEqual(uncited["failures"][0]["id"], "absence_needs_citation", about)
        self.assertEqual(cg.check_gap("absent_in_source", "PurchaseOrder", ["api://h"],
                                      lambda handle: po_row, schema)["failures"][0]["id"],
                         "gap_contradicted_by_cited_evidence")

    def test_an_object_absence_needs_a_readable_api_row_that_links_to_the_type(self):
        """Wire-host decision A (2026-10-03): an object-kind absence must cite a readable
        api:// row of a relation that links to or identifies the subject type. A
        citation that is not api:// never counts, and a row of an unrelated relation
        shows nothing about the object."""
        schema = shared_schema()
        order = {"relation": cpo.RELATION, "key": {"id": 1}, "row": {"id": 1}}
        line = {"relation": "public.app_purchase_order_line", "key": {"id": 2},
                "row": {"id": 2, "purchase_order_id": None}}
        rows = {"api://order": order, "api://line": line}

        def ids(citations):
            verdict = cg.check_gap("absent_in_source", "PurchaseOrder", citations,
                                   rows.get, schema)
            return [f["id"] for f in verdict["failures"]]

        for citations in (["original://z"], ["graph-mail://m/1"], ["api://order"],
                          ["original://z", "api://order"]):
            self.assertEqual(ids(citations), ["absence_needs_citation"], citations)
        self.assertEqual(ids(["original://z", "api://line"]), [])
        self.assertEqual(ids(["api://line", "api://gone"]), ["gap_evidence_unreadable"])
        holders = cg.link_holding_relations(schema, "PurchaseOrder")
        self.assertEqual(holders["public.app_purchase_order_line"], ["purchase_order_id"])
        self.assertIn("public.app_receipt", holders)
        # A supplier row holds only its own id: nothing of a PO.
        self.assertNotIn("public.app_supplier", holders)
        self.assertNotIn(cpo.RELATION, holders)

    def test_an_object_absence_reads_the_subject_rows_link_to_the_type(self):
        """The cited row is the subject record, with a
        key, whose link column to the absent type is present and empty. A row whose
        link is set (it names some object of the type: a dangling reference needs a
        search, and a search shows nothing), a row without that column, or a row with
        no key never proves absence."""
        schema = shared_schema()

        def ids(row, key=None, about="PurchaseOrder",
                relation="public.app_purchase_order_line"):
            found = {"relation": relation, "key": {"id": 2} if key is None else key,
                     "row": row}
            verdict = cg.check_gap("absent_in_source", about, ["api://h"],
                                   lambda handle: found, schema)
            return [f["id"] for f in verdict["failures"]]

        self.assertEqual(ids({"id": 2, "purchase_order_id": None}), [])
        self.assertEqual(ids({"id": 2, "purchase_order_id": "  "}), [])
        self.assertEqual(ids({"id": 2, "purchase_order_id": "po-9"}),
                         ["gap_contradicted_by_cited_evidence"])
        self.assertEqual(ids({"id": 2}), ["gap_property_unreadable"])
        self.assertEqual(ids({"id": 2, "purchase_order_id": None}, key={}),
                         ["gap_subject_key_missing"])
        # A PO row's own number is no link to a receipt (receipts name the PO, the PO
        # names none of them): a PO row never shows a Receipt absent.
        self.assertNotIn("po_number",
                         cg.link_holding_relations(schema, "Receipt").get(
                             "public.app_purchase_order", []))
        self.assertEqual(ids({"id": 1, "po_number": None}, about="Receipt",
                             relation="public.app_purchase_order"),
                         ["absence_needs_citation"])
        # A sales order naming its customer never shows the customer absent.
        self.assertEqual(ids({"id": 1, "customer_id": "c-1"}, about="Customer",
                             relation=cpo.RELATION), ["gap_contradicted_by_cited_evidence"])


if __name__ == "__main__":
    unittest.main()
