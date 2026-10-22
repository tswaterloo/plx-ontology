"""The typed action catalog (operating/action_catalog.json) end to end.

* The catalog loads, covers every capability the action path knows, and its loader
  refuses an action type that could write a real owner, skip a trace or reach a
  store method outside the allow-list.
* Workbench-internal actions (identity link review, correction, follow-up, draft)
  execute as one case-store write and are verified by re-reading the case as the
  acting reader; every refusal writes nothing.
* Owner-system proposals register through the catalog hook of
  ``ActionPath.register`` against the isolated owner (``goal2_exec.Lifecycle``):
  nothing is dispatched without the MC gate; with it the isolated owner is written
  once and read back; a Portal proposal is refused before any MC gate; unknown,
  internal and untraced types never become intents.

Evidence label: ``isolated-fixture`` / ``workbench-store``. ``urlopen`` is patched
to fail in every test: no network, no Petra system, no credential.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from types import SimpleNamespace

from ontology.core import Actor
from operating import action_adapters as adapters
from operating import action_catalog as ac
from operating import action_path as ap
from operating import goal2_exec as gx
from operating.case_links import LinkContracts
from operating.cases import CaseStore, CitationError
from operating.goal2_exec_owner import IsolatedOwnerSystem
from twin import mode as twin_mode
from twin.portal import ORDER_LINES, SHIPMENTS, PortalTwin, TwinError, load_captures
from twin.transport import TwinTransport

OPS = Actor("plx", frozenset({"ops"}))
OUTSIDER = Actor("plx", frozenset({"finance"}))
PO = "api://public.purchase_order/5@g1-c1"
VENDOR = "api://public.vendor/7@g1-c1"
OTHER_VENDOR = "api://public.vendor/8@g1-c1"
ORDER = "api://public.app_sales_order/2@g1-c1"
PORTAL_EFFECT = {"relation": "public.app_sales_order", "key": {"id": 2},
                 "fields": ["po_reference"], "equals": "PO-ISO-3"}
CONTRACTS = {
    "schema": "plx-case-link-contracts/v1",
    "contracts": [{
        "id": "test-po-to-vendor", "from": {"relation": "public.purchase_order"},
        "to": {"relation": "public.vendor"}, "match": "fk",
        "pairs": [{"from": "vendor_id", "to": "id", "compare": "integer_id"}],
        "meaning": "A test purchase order names its vendor by id.",
        "evidence": ["test contract (isolated fixture)"],
        "verification": "fixture rows only"}]}


def no_network(case: unittest.TestCase) -> None:
    patch = mock.patch("urllib.request.urlopen", side_effect=AssertionError("no network"))
    patch.start()
    case.addCleanup(patch.stop)


class CatalogShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = ac.ActionCatalog.builtin()
        self.document = json.loads(ac.CATALOG_PATH.read_text(encoding="utf-8"))

    def test_seeded_with_the_internal_actions_and_every_known_capability(self):
        kinds = {name: self.catalog.get(name)["kind"] for name in self.catalog.ids()}
        internal = sorted(name for name, kind in kinds.items() if kind == "workbench_internal")
        self.assertEqual(internal, ["workbench.draft.record", "workbench.finding.correct",
                                    "workbench.follow_up.record",
                                    "workbench.identity_link.record"])
        for name, capability in ap.REAL_CAPABILITIES.items():
            entry = self.catalog.get(name)
            self.assertEqual((entry["kind"], entry["owner_system"]),
                             ("owner_system_proposal", capability.owner_system))
            # A real capability is reached only through its reviewed adapter, gated.
            self.assertEqual(entry["executor"]["dispatch"], "gated_adapter")
            real = self.catalog.real_capability(name)
            self.assertEqual((real["target_system"], real["read_back"]["method"]),
                             (capability.owner_system, "GET"))
            self.assertIn(real["risk_tier"], ac.RISK_TIERS)
        portal = self.catalog.real_capability("portal.sales_order.set_customer_po")
        # status.ts@31a2623 canEditSalesOrderUntilShipped: blank or EDIT_LOCKED refuses.
        self.assertEqual((portal["owner_state"]["api_field"], portal["owner_state"]["locked"]),
                         ("status", ["PARTIALLY_SHIPPED", "SHIPPED", "COMPLETE", "CANCELLED"]))
        fixture = self.catalog.get(ap.FIXTURE_CAPABILITY)
        self.assertEqual((fixture["owner_system"], fixture["executor"]["dispatch"]),
                         ("fixture", "isolated_owner_only"))
        for name, kind in kinds.items():
            entry = self.catalog.get(name)
            # Mission Control's own writes are approved per call; every other owner
            # through an MC gate.
            self.assertEqual(entry["approval"]["required"],
                             "none" if kind == "workbench_internal" else
                             "per_call_human_approval" if name in ap.MC_CALL_CAPABILITIES
                             else "mc_gate", name)
            for param, spec in entry["parameters"].items():
                self.assertIn(spec["trace"]["kind"], ac.TRACES, f"{name}.{param}")

    def test_internal_executors_name_only_allow_listed_store_writes(self):
        for name in self.catalog.ids():
            entry = self.catalog.get(name)
            if entry["kind"] == "workbench_internal":
                executor = entry["executor"]
                self.assertIn(executor["operation"], ac.STORE_OPERATIONS[executor["store"]])
                self.assertTrue(callable(getattr(CaseStore, executor["operation"])))

    def test_the_catalog_is_the_one_source_of_capability_guards(self):
        # Decision action-catalog-format option (b): no second guard file; the guards
        # are the owner entries' preconditions with a guard check, and the checker's
        # registry is a view of this catalog.
        from operating import capability_guards as cg
        self.assertFalse(Path(ac.__file__).with_name("capability_guards.json").exists())
        self.assertEqual(ac.GUARD_CHECKS, cg.CHECKS)
        registry = cg.registry()["capabilities"]
        self.assertEqual(sorted(registry), self.catalog.proposal_ids())
        for name in self.catalog.proposal_ids():
            entry = self.catalog.get(name)
            self.assertEqual(registry[name]["guards"],
                             [item for item in entry["preconditions"]
                              if item["check"] in ac.GUARD_CHECKS])
            self.assertEqual((registry[name]["target_relations"], registry[name]["hosts"]),
                             (entry["target_relations"], entry["hosts"]))
        # The test-only declaration (hosts) and the action path's hard-guard set agree.
        self.assertEqual(ap.FIXTURE_CAPABILITIES, frozenset(
            name for name in self.catalog.proposal_ids()
            if self.catalog.get(name)["owner_system"] == "fixture"))
        for name in ap.FIXTURE_CAPABILITIES:
            self.assertFalse(cg.available(name))
            self.assertTrue(cg.available(name, test_host=True))

    def test_owner_entries_bind_relations_and_hosts(self):
        portal = "portal.sales_order.set_customer_po"
        for change, code in (
                (lambda e: e.pop("target_relations"), "catalog_target_relations_required"),
                (lambda e: e.update(target_relations=[]), "catalog_target_relations_required"),
                (lambda e: e.update(target_relations=["public.*"]),
                 "catalog_real_owner_binds_exact_relations"),
                (lambda e: e.update(hosts=["everywhere"]), "catalog_hosts_invalid"),
                (lambda e: e.pop("hosts"), "catalog_hosts_invalid")):
            with self.subTest(code=code):
                self.assert_refused(self.mutated(portal, change), code)
        for change in (lambda e: e.update(hosts=["case_host"]),
                       lambda e: e.update(target_relations=["public.app_sales_order"])):
            self.assert_refused(self.mutated(ap.FIXTURE_CAPABILITY, change),
                                "catalog_isolated_owner_binding")
        self.assert_refused(self.mutated("test.record.set_field",
                                         lambda e: e.update(hosts=["case_host", "test"])),
                            "catalog_test_owner_hosts")

    def mutated(self, action_id, change):
        document = copy.deepcopy(self.document)
        change(document["actions"][action_id])
        return document

    def assert_refused(self, document, code_prefix):
        with self.assertRaises(ac.CatalogError) as caught:
            ac.ActionCatalog(document)
        self.assertTrue(str(caught.exception).startswith(code_prefix), str(caught.exception))

    def test_loader_refuses_unsafe_or_untraced_action_types(self):
        portal = "portal.sales_order.set_customer_po"
        cases = [
            (portal, lambda e: e["executor"].update(dispatch="isolated_owner_only"),
             "catalog_owner_dispatch_not_allowed"),
            (portal, lambda e: e["approval"].update(required="none"), "catalog_owner_approval"),
            (portal, lambda e: e["parameters"]["proposed_customer_po"].update(
                trace={"kind": "actor_statement"}), "catalog_actor_statement_on_owner_proposal"),
            (portal, lambda e: e["parameters"]["evidence_field"].pop("trace"),
             "catalog_parameter_untraced"),
            (portal, lambda e: e["preconditions"].pop(1),
             "catalog_owner_structured_preconditions"),
            ("workbench.draft.record", lambda e: e["executor"].update(operation="close"),
             "catalog_internal_executor_not_allowed"),
            ("workbench.draft.record", lambda e: e["executor"].update(operation="propose"),
             "catalog_internal_executor_not_allowed"),
            ("workbench.draft.record", lambda e: e["approval"].update(required="mc_gate"),
             "catalog_internal_approval"),
            ("workbench.draft.record", lambda e: e["parameters"]["statement"].update(
                trace={"kind": "cited_value", "citation": "citations", "field": "statement"}),
             "catalog_cited_trace_needs_proposal"),
            ("workbench.follow_up.record", lambda e: e["preconditions"].clear(),
             "catalog_preconditions_missing"),
        ]
        # argument_provenance belongs to the owner value parameter only.
        cases.append((portal, lambda e: e["parameters"]["evidence_field"].update(
            trace={"kind": "argument_provenance"}), "catalog_argument_provenance_not_the_value"))
        cases.append(("workbench.draft.record", lambda e: e["parameters"]["statement"].update(
            trace={"kind": "argument_provenance"}), "catalog_argument_provenance_needs_proposal"))
        # An owner value traced to its own expected_effect.equals is a self-trace.
        cases.append((ap.FIXTURE_CAPABILITY, lambda e: e["parameters"]["value"].update(
            trace={"kind": "effect", "effect_key": "equals"}),
            "catalog_owner_value_self_traced"))
        cases.append((portal, lambda e: e["parameters"]["proposed_customer_po"].update(
            trace={"kind": "effect", "effect_key": "equals"}),
            "catalog_owner_value_self_traced"))
        # Mission Control's own write: every argument it is sent declares a role, is one
        # the MC tool takes, comes from a traced parameter, the host or the intent, and
        # only MC is written through the write door.
        follow_up = "mc.task.create_follow_up"
        arguments = lambda e: e["mc_call"]["arguments"]   # noqa: E731
        cases += [
            (follow_up, lambda e: arguments(e)["bucket"].pop("role"),
             "catalog_mc_call_role_undeclared"),
            (follow_up, lambda e: arguments(e)["title"].update(role="enum"),
             "catalog_mc_call_role_mismatch"),
            (follow_up, lambda e: arguments(e).update(stage={
                "from": ["param:title"], "role": "text", "read_back": False}),
             "catalog_mc_call_argument_unknown"),
            (follow_up, lambda e: arguments(e)["bucket"].update({"from": ["param:question"]}),
             "catalog_mc_call_source_invalid"),
            (follow_up, lambda e: e["parameters"]["title"].update(
                trace={"kind": "case_gap", "record": "gap_id", "field": "who_can_answer"}),
             "catalog_case_gap_trace"),
            (follow_up, lambda e: e["mc_call"].update(tool="mc_report_progress"),
             "catalog_mc_call_invalid"),
            (portal, lambda e: e["executor"].update(dispatch="mc_write_door"),
             "catalog_mc_write_door_binding")]
        # Fails closed: a key the loader does not read, or a real capability that names
        # no condition for an owner without keys, a non-GET read-back, a field the type
        # may not write or an undeclared risk tier, does not load.
        real = lambda e: e["real_capability"]   # noqa: E731
        cases += [
            (portal, lambda e: e.update(auto_dispatch=True), "catalog_unknown_field"),
            (portal, lambda e: real(e).update(allow=True), "catalog_unknown_field"),
            (portal, lambda e: e["parameters"]["proposed_customer_po"].update(default="X"),
             "catalog_unknown_field"),
            (portal, lambda e: e["preconditions"][2].update(skip=True), "catalog_unknown_field"),
            (portal, lambda e: real(e).update(risk_tier="none"),
             "catalog_real_capability_risk_tier"),
            (portal, lambda e: real(e)["read_back"].update(method="PATCH"),
             "catalog_real_capability_http"),
            (portal, lambda e: real(e)["write"]["fields"].update(order_number="orderNumber"),
             "catalog_real_capability_fields_not_writable"),
            (portal, lambda e: real(e)["idempotency"].update(conditional="none"),
             "catalog_real_capability_unconditional_without_key"),
            (portal, lambda e: e["executor"].update(dispatch="refused", later_ask="x"),
             "catalog_real_capability_binding")]
        # The owner state a write needs (the Portal takes a header edit until the first
        # shipment or cancel, canEditSalesOrderUntilShipped): closed, a field and a
        # non-empty set of locked values, with its rule and source; never an allowed list;
        # an MC write door names none (MC states its own).
        state = lambda e: real(e)["owner_state"]   # noqa: E731
        cases += [
            (portal, lambda e: state(e).update(default="DRAFT"), "catalog_unknown_field"),
            (portal, lambda e: state(e).update(allowed=["DRAFT"]), "catalog_unknown_field"),
            (portal, lambda e: state(e).update(locked=[]),
             "catalog_real_capability_owner_state"),
            (portal, lambda e: state(e).pop("locked"), "catalog_real_capability_owner_state"),
            (portal, lambda e: state(e).update(locked=["SHIPPED", "any value"]),
             "catalog_real_capability_owner_state"),
            (portal, lambda e: state(e).update(api_field="order.status"),
             "catalog_real_capability_owner_state"),
            (portal, lambda e: state(e).pop("upstream"), "catalog_real_capability_owner_state"),
            # The owner's own lock as its GET reports it: closed, typed, never a free text.
            (portal, lambda e: state(e)["edit_lock"].update(override=True),
             "catalog_unknown_field"),
            (portal, lambda e: state(e)["edit_lock"].pop("flag"),
             "catalog_real_capability_owner_state"),
            (portal, lambda e: state(e)["edit_lock"].update(refusal="Order shipped!"),
             "catalog_real_capability_owner_state"),
            (follow_up, lambda e: real(e).update(owner_state={
                "api_field": "stage", "locked": ["done"], "rule": "r", "upstream": "u"}),
             "catalog_real_capability_owner_state")]
        for action_id, change, code in cases:
            with self.subTest(code=code):
                self.assert_refused(self.mutated(action_id, change), code)
        document = copy.deepcopy(self.document)
        document["auto_approve"] = True
        self.assert_refused(document, "catalog_unknown_field:document")
        document = copy.deepcopy(self.document)
        document["actions"]["Portal.Sales_Order"] = document["actions"][portal]
        self.assert_refused(document, "catalog_action_id_not_canonical")
        document = copy.deepcopy(self.document)
        document["actions"]["bc.purchase_order.post"] = copy.deepcopy(
            document["actions"][ap.FIXTURE_CAPABILITY])
        document["actions"]["bc.purchase_order.post"]["owner_system"] = "bc"
        self.assert_refused(document, "catalog_owner_dispatch_not_allowed")

    def test_no_action_type_writes_a_route_that_raises_or_posts_a_bc_financial_document(self):
        # BC alone posts invoices, credit memos and payments; the Portal raises only
        # unposted drafts (3PL invoice, return credit memo), refuses payment writes and posts
        # misc AP bills (action_catalog.BC_FINANCIAL_ROUTES). No Workbench action reaches one,
        # whatever its approval: by segment, any case, a sub-route or a placeholder included.
        portal = "portal.sales_order.set_customer_po"

        def write(path, keys):
            def change(entry):
                entry["real_capability"]["write"].update(path=path, path_keys=keys)
            return change

        for path, keys in (("/api/mrp/sales-returns", {}), ("/api/mrp/3pl/invoice", {}),
                           ("/api/mrp/ap-payments", {}), ("/API/MRP/AP-SETTLE/", {}),
                           ("/api/mrp/ap-misc/{id}", {"id": "id"}),
                           ("/api/mrp/purchase-orders/{id}/vendor-invoice", {"id": "id"}),
                           # a record value fills a placeholder at dispatch: it may name one
                           ("/api/mrp/{id}", {"id": "id"}), ("/api/mrp/sales-{id}", {"id": "id"})):
            with self.subTest(path=path):
                self.assert_refused(self.mutated(portal, write(path, keys)),
                                    "catalog_real_capability_bc_financial_route")
        # A dot segment could step around the route list: it never loads.
        self.assert_refused(self.mutated(portal, write("/api/mrp/x/../sales-returns", {})),
                            "catalog_real_capability_http")
        # Segments, not substrings: a near name is another route.
        ac.ActionCatalog(self.mutated(portal, write("/api/mrp/sales-returns-summary/{id}",
                                                    {"id": "id"})))

    def test_every_owner_write_declares_its_role_or_the_catalog_refuses(self):
        # roles-declared (inj4-1): what an owning system is asked to write has a role.
        self.assertEqual(self.catalog.get("portal.sales_order.set_customer_po")["parameters"]
                         ["proposed_customer_po"]["role"], "identifier:customer_po")
        for name in self.catalog.proposal_ids():
            entry = self.catalog.get(name)
            effect = entry["effect"]
            written = ([effect["value_parameter"]] if "value_parameter" in effect
                       else list(effect["field_parameters"].values()))
            for param, spec in entry["parameters"].items():
                with self.subTest(action=name, parameter=param):
                    self.assertEqual("role" in spec, param in written)
        portal, fixture = "portal.sales_order.set_customer_po", ap.FIXTURE_CAPABILITY
        cases = [
            (portal, lambda e: e["parameters"]["proposed_customer_po"].pop("role"),
             "catalog_parameter_role_undeclared:portal.sales_order.set_customer_po."
             "proposed_customer_po"),
            (fixture, lambda e: e["parameters"]["value"].pop("role"),
             f"catalog_parameter_role_undeclared:{fixture}.value"),
            ("portal.assembly_order.set_header_quantities",
             lambda e: e["parameters"]["finalqty"].pop("role"),
             "catalog_parameter_role_undeclared:portal.assembly_order.set_header_quantities"
             ".finalqty"),
            (portal, lambda e: e["parameters"]["evidence_field"].update(role="text"),
             "catalog_parameter_role_not_written"),
            ("workbench.draft.record", lambda e: e["parameters"]["statement"].update(
                role="text"), "catalog_parameter_role_not_written")]
        for role in ("identifier:no_such_kind", "quantity:guessed", "money:total", "customer_po",
                     "Identifier:customer_po", "", None, "any"):
            cases.append((portal, lambda e, role=role: e["parameters"]["proposed_customer_po"]
                          .update(role=role), "catalog_parameter_role_invalid"))
        for action_id, change, code in cases:
            with self.subTest(code=code, action=action_id):
                self.assert_refused(self.mutated(action_id, change), code)
        for role in ("identifier:bc_external_document_no", "quantity:received", "money:amount",
                     "text", "owner_declared"):
            with self.subTest(role=role):
                ac.ActionCatalog(self.mutated(portal, lambda e: e["parameters"]
                                              ["proposed_customer_po"].update(role=role)))

    def test_real_owner_roles_agree_with_the_schema_bindings_of_their_targets(self):
        # owner_declared on a real owner names a role the target's schema binding
        # declares for every writable field; a fixed role never contradicts one.
        from operating import injection_guard
        for name in self.catalog.proposal_ids():
            entry = self.catalog.get(name)
            if entry["owner_system"] in ("fixture", "test") or name in ap.MC_CALL_CAPABILITIES:
                continue   # an MC call writes no cited row: its roles are its mc_call's
            fields = next(item["fields"] for item in entry["preconditions"]
                          if item["check"] == "effect_fields_within")
            effect = entry["effect"]
            for field in fields:
                param = effect.get("value_parameter") or effect["field_parameters"][field]
                declared = entry["parameters"][param]["role"]
                for relation in entry["target_relations"]:
                    # The one role rule (schema role, else the carriers' kind).
                    found = injection_guard.cited_role(
                        f"api://{relation}/1@g1", {"relation": relation, "row": {}},
                        f"row.{field}")
                    with self.subTest(action=name, field=field):
                        if "." in field:
                            # A JSON key declares no role: registration refuses a
                            # write to it (test_a_customer_po_comes_from_...).
                            self.assertIsNone(found)
                            continue
                        self.assertIsNotNone(found)
                        if declared != ac.OWNER_DECLARED_ROLE:
                            self.assertTrue(injection_guard.same_role(declared, found))

    def test_a_declared_date_field_takes_only_a_calendar_date(self):
        # The action path's type check (an owner effect, a typed owner relation).
        for value, fits in (("2026-11-02", True), ("2024-02-29", True), ("2026-13-45", False),
                            ("2026-02-30", False), ("2025-02-29", False), ("20261102", False),
                            ("2026-11-02T00:00:00Z", False), ("٢٠٢٦-11-02",
                                                              False), (20261102, False)):
            with self.subTest(value=value):
                self.assertIs(ac.value_fits_type("date", value), fits)

    def test_action_path_takes_only_a_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = CaseStore(Path(tmp) / "c.db")
            with self.assertRaises(TypeError):
                ap.ActionPath(Path(tmp) / "a.db", cases, catalog={"actions": {}})


class OwnerEditLockTests(unittest.TestCase):
    """The Portal locks a sales order once a shipment is SHIPPED or DELIVERED or a line has
    shipped (plx-customer-portal@5cb95e1 portal/src/lib/sales-order/editability.ts:42-67,
    113-157, called by service.ts updateSalesOrderHeader). Its GET reports that lock; the
    adapter's pre-write GET and the twin's 409 decide by one function
    (``owner_state_refusal``), so a shipped order is refused before send as
    ``order_locked_shipped``, never recorded as the owner's rejection. Text is trimmed as
    JavaScript trims it (``js_trim``). Evidence: ``twin-transport``, ``injected-transport``."""

    CAPABILITY = "portal.sales_order.set_customer_po"
    ORDER_RELATION = "public.app_sales_order"
    PATH = "/api/mrp/sales-orders/so-twin-1"

    def setUp(self) -> None:
        no_network(self)
        mode = twin_mode.test_mode()
        mode.__enter__()
        self.addCleanup(mode.__exit__, None, None, None)

    def world(self, *rows):
        """A Portal twin holding order so-twin-1 (DRAFT, no PO) with line line-1."""
        twin = PortalTwin(session_role="STAFF")
        order = next(r["row"] for r in load_captures() if r["row"].get("id") == "so-twin-1")
        twin.seed(self.ORDER_RELATION, order)
        twin.seed(ORDER_LINES, {"id": "line-1", "sales_order_id": "so-twin-1"})
        for relation, row in rows:
            twin.seed(relation, row)
        transport = TwinTransport(twin)
        return twin, transport, adapters.PortalAdapter(self.CAPABILITY, transport)

    def intent(self, value="PO-LOCK-1"):
        return SimpleNamespace(
            capability=self.CAPABILITY, owner_system="portal", idempotency_key="k-" + value,
            checked_preconditions=(),
            expected_effect={"relation": self.ORDER_RELATION, "key": {"id": "so-twin-1"},
                             "fields": ["po_reference"], "equals": value})

    def patch(self, transport, value):
        return transport.send(adapters.OwnerRequest("portal", "PATCH", self.PATH,
                                                     body={"poReference": value}))

    def get(self, transport):
        return transport.send(adapters.OwnerRequest("portal", "GET", self.PATH))["body"]

    def test_a_shipped_order_is_refused_before_send_and_an_unshipped_one_is_written(self):
        shipment = lambda key, order, status: (SHIPMENTS, {   # noqa: E731
            "id": key, "sales_order_id": order, "status": status})
        shipped = {
            "shipped_shipment": ([shipment("sh-1", "so-twin-1", "SHIPPED")], []),
            # editability.ts:45: the status is upper-cased before the compare.
            "delivered_shipment": ([shipment("sh-1", "so-twin-1", "delivered")], []),
            # A consolidated shipment names no order: only the line's allocation locks it.
            "shipped_line": ([shipment("sh-9", None, "SHIPPED")], [("line-1", "sh-9", 2)])}
        for name, (rows, allocations) in shipped.items():
            with self.subTest(name):
                twin, transport, adapter = self.world(*rows)
                for allocation in allocations:
                    twin.allocate(*allocation)
                body = self.get(transport)
                self.assertEqual((body["editable"], body["editLock"]), (False, {
                    "reason": "SHIPPED", "message": "This order has a shipment that has "
                                                    "already shipped, so it can no longer be "
                                                    "edited."}))
                transport.requests.clear()
                with self.assertRaises(ap.DispatchRefused) as caught:
                    ap.RealSystemDispatcher(adapter).dispatch(self.intent())
                self.assertEqual(str(caught.exception), "order_locked_shipped")
                self.assertEqual(transport.requests, [("portal", "GET", self.PATH)])
                answer = self.patch(transport, "PO-LOCK-1")
                self.assertEqual((answer["status"], answer["body"]["code"]),
                                 (409, "ORDER_NOT_EDITABLE"))
                self.assertEqual(twin.writes, 0)
                self.assertIsNone(twin.records[self.ORDER_RELATION]["so-twin-1"]["poReference"])
        # Not shipped: a cancelled shipment, a line allocated on a shipment not yet shipped,
        # another order's shipped line. The header edit goes through; the lines lock.
        twin, transport, adapter = self.world(
            shipment("sh-c", "so-twin-1", "CANCELLED"), shipment("sh-p", "so-twin-1", "PICKING"),
            shipment("sh-x", "so-other", "SHIPPED"),
            (ORDER_LINES, {"id": "line-x", "sales_order_id": "so-other"}))
        twin.allocate("line-1", "sh-p", 5)
        twin.allocate("line-x", "sh-x", 1)
        self.assertEqual({k: self.get(transport)[k] for k in ("editable", "editLock",
                                                               "linesLocked")},
                         {"editable": True, "editLock": None, "linesLocked": True})
        self.assertTrue(ap.RealSystemDispatcher(adapter).dispatch(self.intent())
                        .startswith("portal:http_200:"))
        self.assertEqual(twin.writes, 1)
        self.assertEqual(twin.records[self.ORDER_RELATION]["so-twin-1"]["poReference"],
                         "PO-LOCK-1")
        with self.assertRaises(TwinError) as caught:
            twin.allocate("line-1", "sh-none", 1)
        self.assertEqual(caught.exception.code, "allocation_invalid")

    def test_a_lock_the_get_does_not_report_as_the_owner_does_sends_nothing(self):
        class Injected:
            evidence = adapters.EVIDENCE_LABEL

            def __init__(self, extra):
                self.body = {"id": "so-twin-1", "status": "DRAFT", "poReference": None, **extra}
                self.methods = []

            def send(self, request):
                self.methods.append(request.method)
                return {"status": 200, "body": dict(self.body)}

        for extra, code in (
                ({}, "owner_state_refused:editLock=absent"),
                ({"editable": True}, "owner_state_refused:editLock=absent"),
                ({"editable": True, "editLock": {"reason": "STATUS"}},
                 "owner_state_refused:editLock=STATUS"),
                ({"editable": False, "editLock": None}, "owner_state_refused:editLock=unreadable"),
                ({"editable": "true", "editLock": None}, "owner_state_refused:editLock=unreadable"),
                ({"editable": True, "editLock": "SHIPPED"},
                 "owner_state_refused:editLock=unreadable"),
                ({"editable": True, "editLock": {"reason": "SHIPPED"}}, "order_locked_shipped")):
            with self.subTest(extra=extra):
                injected = Injected(extra)
                with self.assertRaises(adapters.NotSent) as caught:
                    adapters.PortalAdapter(self.CAPABILITY, injected).dispatch(self.intent())
                self.assertEqual((caught.exception.code, injected.methods), (code, ["GET"]))

    def test_customer_po_and_status_text_are_trimmed_as_javascript_trims(self):
        # String.prototype.trim strips U+FEFF and keeps U+001C-U+001F (and U+0085);
        # Python's str.strip() does the opposite on both.
        self.assertEqual(ac.js_trim("\ufeff PO-1\u3000\ufeff"), "PO-1")
        self.assertEqual(ac.js_trim("\x1cPO-1\x1f"), "\x1cPO-1\x1f")
        self.assertEqual(adapters.normalized("trim_collapse_upper", "\ufeffpo\x85 \u2028 1"),
                         "PO\x85 1")
        self.assertIsNone(adapters.normalized("trim_collapse_upper", "\ufeff\u00a0"))
        # Two other orders of the same customer, their POs "PO-1" and U+001C.
        base = next(r["row"] for r in load_captures() if r["row"].get("id") == "so-twin-1")
        twin, transport, adapter = self.world(*[
            (self.ORDER_RELATION, {**base, "id": key, "order_number": key, "po_reference": po})
            for key, po in (("so-fe", "PO-1"), ("so-1c", "\x1c"))])
        warning = ac.ActionCatalog.builtin().real_capability(
            self.CAPABILITY)["owner_warnings"][0]
        peers = twin.peers(OPS, self.ORDER_RELATION)
        row = next(p["row"] for p in peers if p["key"]["id"] == "so-twin-1")
        # The adapter's reuse count and the twin's warning agree on both characters.
        for sent, reused, po in (("\ufeffpo-1 ", 1, "PO-1"), ("\x1c", 1, "\x1c"),
                                 ("\ufeff", 0, None)):
            with self.subTest(sent=sent):
                self.assertEqual(adapters.reused_by(warning, {"id": "so-twin-1"}, row, sent,
                                                    peers), reused)
                warnings = self.patch(transport, sent)["body"]["warnings"]
                self.assertEqual([w.get("poReference") for w in warnings], [po] if po else [])
        # A status of U+FEFF is blank to canEditSalesOrderUntilShipped (it trims): refused
        # before send and by the twin; U+001C is not blank, so the Portal takes the edit.
        record = twin.records[self.ORDER_RELATION]["so-twin-1"]
        record.update(status="\ufeff", poReference=None)
        with self.assertRaises(adapters.NotSent) as caught:
            adapter.dispatch(self.intent())
        self.assertEqual(caught.exception.code, "owner_state_refused:status=blank")
        self.assertEqual(self.patch(transport, "PO-2")["status"], 409)
        record.update(status="\x1c", poReference=None)
        self.assertTrue(adapter.dispatch(self.intent("PO-3")).startswith("portal:http_200:"))


class InternalActionTests(unittest.TestCase):
    """Workbench-internal actions execute now and verify by re-reading the case."""

    def setUp(self) -> None:
        no_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.rows = {
            PO: {"relation": "public.purchase_order", "key": {"id": 5},
                 "row": {"id": 5, "vendor_id": 7}},
            VENDOR: {"relation": "public.vendor", "key": {"id": 7}, "row": {"id": 7}},
            OTHER_VENDOR: {"relation": "public.vendor", "key": {"id": 8}, "row": {"id": 8}}}
        self.cases = CaseStore(Path(tmp.name) / "cases.db", citation_resolver=self.resolve,
                               link_contracts=LinkContracts(CONTRACTS))
        self.stores = {"cases": self.cases}
        self.catalog = ac.ActionCatalog.builtin()
        self.case_id = self.cases.open(OPS, "Which vendor does PO 5 belong to?",
                                       roots=["PO-5"])

    def resolve(self, handle, actor):
        if "ops" not in actor.principals:
            raise CitationError("not visible")
        if handle not in self.rows:
            raise CitationError("unknown")
        return copy.deepcopy(self.rows[handle])

    def run_action(self, action_id, parameters, actor=OPS, case_id=None):
        return self.catalog.execute(actor, action_id, case_id or self.case_id, parameters,
                                    stores=self.stores)

    def events(self):
        return self.cases.get(OPS, self.case_id)["integrity"]["events"]

    def test_draft_follow_up_and_correction_execute_and_verify(self):
        draft = self.run_action("workbench.draft.record", {
            "statement": "Vendor 7 looks like the supplier; to be confirmed.",
            "citations": [PO]})
        self.assertEqual((draft["executed"], draft["recorded"], draft["approval"],
                          draft["evidence_label"]),
                         (True, True, "none", "workbench-store"))
        self.assertEqual(draft["read_back"]["outcome"], "verified", draft)
        finding = self.cases.get(OPS, self.case_id)["findings"]["unknown"][0]
        self.assertEqual((finding["id"], finding["kind"], finding["label"]),
                         (draft["receipt"]["finding_id"], "interpretation",
                          "model_output_interpretation"))

        follow_up = self.run_action("workbench.follow_up.record", {
            "missing": "the supplier confirmation for PO 5",
            "why": "no confirmation mail is cited",
            "who_can_answer": ["purchasing"], "state": "not_ingested"})
        self.assertEqual(follow_up["read_back"]["outcome"], "verified", follow_up)
        [gap] = self.cases.get(OPS, self.case_id)["gaps"]
        self.assertEqual((gap["id"], gap["who_can_answer"], gap["state"]),
                         (follow_up["receipt"]["gap_id"], ["purchasing"], "not_ingested"))

        correction = self.run_action("workbench.finding.correct", {
            "finding_id": draft["receipt"]["finding_id"],
            "reason": "the vendor was checked by the link contract instead"})
        self.assertEqual(correction["read_back"]["outcome"], "verified", correction)
        [superseded] = self.cases.get(OPS, self.case_id)["findings"]["superseded"]
        self.assertEqual(superseded["superseded"]["reason"],
                         "the vendor was checked by the link contract instead")
        # A finding already corrected is no longer a current record: refused, nothing written.
        before = self.events()
        with self.assertRaises(ac.ActionRefused) as caught:
            self.run_action("workbench.finding.correct", {
                "finding_id": draft["receipt"]["finding_id"], "reason": "again"})
        self.assertEqual(caught.exception.code,
                         "precondition_not_met:finding_exists_and_current")
        self.assertEqual(self.events(), before)

    def test_identity_link_review_records_the_contract_outcome_and_verifies(self):
        verified = self.run_action("workbench.identity_link.record", {
            "from_citation": PO, "to_citation": VENDOR, "contract_id": "test-po-to-vendor"})
        self.assertEqual((verified["recorded"], verified["receipt"]["outcome"],
                          verified["read_back"]["outcome"]), (True, "verified", "verified"))
        # The other vendor contradicts: the contract decides, never the caller.
        contradicted = self.run_action("workbench.identity_link.record", {
            "from_citation": PO, "to_citation": OTHER_VENDOR,
            "contract_id": "test-po-to-vendor"})
        self.assertEqual((contradicted["receipt"]["outcome"],
                          contradicted["read_back"]["outcome"]), ("contradicted", "verified"))
        links = self.cases.get(OPS, self.case_id)["links"]
        self.assertEqual([link["outcome"] for link in links], ["verified", "contradicted"])
        before = self.events()
        with self.assertRaises(ac.ActionRefused) as caught:
            self.run_action("workbench.identity_link.record", {
                "from_citation": PO, "to_citation": VENDOR, "contract_id": "no-such-contract"})
        self.assertTrue(caught.exception.code.startswith("store_refused:"), caught.exception.code)
        with self.assertRaises(ac.ActionRefused) as caught:
            self.run_action("workbench.identity_link.record", {
                "from_citation": PO, "to_citation": PO, "contract_id": "test-po-to-vendor"})
        self.assertEqual(caught.exception.code, "precondition_not_met:two_different_records")
        self.assertEqual(self.events(), before)

    def test_read_back_that_does_not_match_is_not_verified(self):
        original = CaseStore.get

        def tampered(store, actor, case_id):
            view = original(store, actor, case_id)
            for gap in view["gaps"]:
                gap["state"] = "unknown"
            return view

        with mock.patch.object(CaseStore, "get", tampered):
            result = self.run_action("workbench.follow_up.record", {
                "missing": "x", "why": "y", "who_can_answer": ["ops"], "state": "stale"})
        self.assertEqual(result["read_back"]["outcome"], "not_observed")
        self.assertIn({"path": "state", "holds": False}, result["read_back"]["checks"])

    def test_refusals_write_nothing(self):
        before = self.events()
        refusals = [
            ("workbench.nothing.here", {"statement": "x"}, OPS, "action_type_not_in_catalog"),
            ("Workbench.Draft.Record", {"statement": "x"}, OPS, "action_type_not_in_catalog"),
            ("portal.sales_order.set_customer_po", {"proposed_customer_po": "PO-1"}, OPS,
             "owner_system_action_needs_mc_gate"),
            ("workbench.draft.record", {"statement": "x", "kind": "fact"}, OPS,
             "parameter_unknown:kind"),
            ("workbench.draft.record", {}, OPS, "parameter_missing:statement"),
            ("workbench.draft.record", {"statement": ""}, OPS, "parameter_type:statement"),
            ("workbench.follow_up.record", {"missing": "x", "why": "y",
                                            "who_can_answer": ["ops"], "state": "solved"},
             OPS, "parameter_type:state"),
            ("workbench.finding.correct", {"finding_id": "F9", "reason": "r"}, OPS,
             "parameter_untraced:finding_id:record_not_on_case"),
            ("workbench.draft.record", {"statement": "x"}, OUTSIDER, "case_not_found"),
            ("workbench.draft.record", {"statement": "x", "citations": ["api://nope/1@g"]},
             OPS, "store_refused:"),
        ]
        for action_id, parameters, actor, code in refusals:
            with self.subTest(code=code):
                with self.assertRaises(ac.ActionRefused) as caught:
                    self.run_action(action_id, parameters, actor=actor)
                self.assertTrue(caught.exception.code.startswith(code), caught.exception.code)
        self.assertEqual(self.events(), before)
        with self.assertRaises(ac.ActionRefused) as caught:
            self.catalog.execute(OPS, "workbench.draft.record", self.case_id,
                                 {"statement": "x"}, stores={})
        self.assertEqual(caught.exception.code, "store_not_configured:cases")
        self.cases.close(OPS, self.case_id, "answered")
        with self.assertRaises(ac.ActionRefused) as caught:
            self.run_action("workbench.draft.record", {"statement": "x"})
        self.assertEqual(caught.exception.code, "precondition_not_met:case_open")


class OwnerProposalTests(unittest.TestCase):
    """Owner-system proposals through the catalog hook, against the isolated owner."""

    def setUp(self) -> None:
        no_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.world = gx.Lifecycle(Path(tmp.name))
        # The isolated owner's records plus one Portal order row, so a Portal proposal
        # can cite its target (the case store requires it); nothing reaches the Portal.
        # Its PO fields read and empty, as the mirror serves them: ActionPath.register
        # enforces the capability guards on it.
        self.portal_rows = {ORDER: {"relation": "public.app_sales_order", "key": {"id": 2},
                                    "row": {"id": 2, "order_number": "SO-2",
                                            "po_reference": None, "custom_fields": None}}}
        self.world.cases = CaseStore(Path(tmp.name) / "cases-with-portal.db",
                                     citation_resolver=self.resolve)
        self.world.path = self.world.new_path()
        self.path, self.owner = self.world.path, self.world.owner

    def resolve(self, handle, actor):
        if handle in self.portal_rows:
            if "ops" not in actor.principals:
                raise CitationError("not visible")
            return copy.deepcopy(self.portal_rows[handle])
        return self.owner.resolve(handle, actor)

    def proposal(self, case_id, capability, payload, effect, citations, target):
        return self.world.cases.propose(
            gx.ACTOR, case_id, capability, payload,
            [{"check": "field_empty", "citation": target, "field": "po_reference"},
             "a person confirms the PO belongs to this order"],
            citations=citations, expected_effect=effect)["proposal_id"]

    def isolated_case(self, number, question=None):
        key = {"id": number}
        target = self.owner.seed(gx.RELATION, key, {"id": number, "po_reference": None})
        # The employee's question, as a human session records it.
        case_id = self.world.cases.open(
            gx.ACTOR, question or f"Set the PO on isolated order {number} to PO-ISO-{number}?",
            objective_author_kind="human")
        effect = {"relation": gx.RELATION, "key": key, "fields": ["po_reference"],
                  "equals": f"PO-ISO-{number}"}
        return case_id, target, effect

    def test_an_owner_proposal_is_never_dispatched_without_the_mc_gate(self):
        case_id, iid = self.world.open_case_and_register(1)
        self.assertEqual(self.path.get(gx.ACTOR, iid)["state"], "proposed")
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.dispatch(gx.ACTOR, iid, self.world.registry, self.world.mc_client,
                               session=gx.SESSION)
        self.assertEqual(caught.exception.code, "gate_not_authorized")
        self.assertEqual(self.owner.dispatch_calls, 0)
        # The catalog never executes it either: owner-system writes go through the gate.
        with self.assertRaises(ac.ActionRefused) as caught:
            self.path.catalog.execute(gx.ACTOR, ap.FIXTURE_CAPABILITY, case_id,
                                      {"field": "po_reference", "value": "PO-ISO-1"},
                                      stores={"cases": self.world.cases})
        self.assertEqual(caught.exception.code, "owner_system_action_needs_mc_gate")
        # With the human MC gate: dispatched once to the isolated owner, read back.
        self.world.authorize(iid, "TASK-ISO-1")
        self.assertEqual(self.path.dispatch(gx.ACTOR, iid, self.world.registry,
                                            self.world.mc_client,
                                            session=gx.SESSION)["state"], "dispatched")
        self.path.read_back(gx.ACTOR, iid, self.world.read_back, session=gx.SESSION)
        view = self.path.view(gx.ACTOR, iid)
        self.assertEqual((view["state"], view["outcome"], self.owner.dispatch_calls),
                         ("verified", "verified", 1))

    def test_a_portal_proposal_registers_typed_and_is_refused_before_any_gate(self):
        case_id, target, _ = self.isolated_case(2)
        evidence = self.owner.seed("fixture.customer_document", {"id": 9},
                                   {"id": 9, "customer_po": "PO-ISO-2"})
        effect = {"relation": "public.app_sales_order", "key": {"id": 2},
                  "fields": ["po_reference"], "equals": "PO-ISO-2"}
        pid = self.proposal(case_id, "portal.sales_order.set_customer_po",
                            {"proposed_customer_po": "PO-ISO-2", "evidence_citation": evidence,
                             "evidence_field": "customer_po"},
                            effect, [ORDER, evidence], ORDER)
        intent = self.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
        self.assertEqual(intent["owner_system"], "portal")
        self.world.mc.add_task("TASK-ISO-2", "portal proposal")
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.prepare_gate(gx.ACTOR, intent["intent_id"], "TASK-ISO-2",
                                   self.world.mc_client, session=gx.SESSION)
        self.assertEqual(caught.exception.code, "production_target_refused")
        self.assertEqual((self.world.mc.tool_calls, self.owner.dispatch_calls), ([], 0))

    def test_unknown_internal_and_untraced_types_never_become_intents(self):
        case_id, target, effect = self.isolated_case(3)
        evidence = self.owner.seed("fixture.customer_document", {"id": 9},
                                   {"id": 9, "customer_po": "PO-OTHER"})
        fixture_payload = {"field": "po_reference", "value": "PO-ISO-3"}
        portal_payload = {"proposed_customer_po": "PO-ISO-3", "evidence_citation": evidence,
                          "evidence_field": "customer_po"}
        refused = [
            ("bc.purchase_order.post", fixture_payload, effect, [target],
             "action_type_not_in_catalog"),
            ("workbench.draft.record", {"statement": "x"}, effect, [target],
             "catalog_action_is_workbench_internal"),
            (ap.FIXTURE_CAPABILITY, {**fixture_payload, "note": "x"}, effect, [target],
             "parameter_unknown:note"),
            (ap.FIXTURE_CAPABILITY, {"field": "po_reference"}, effect, [target],
             "parameter_missing:value"),
            # The value is argument_provenance: the injection hook after the catalog.
            (ap.FIXTURE_CAPABILITY, {"field": "po_reference", "value": "PO-ISO-33"},
             {**effect, "equals": "PO-ISO-33"}, [target], "injection_untraced_parameter"),
            (ap.FIXTURE_CAPABILITY, {"field": "ship_note", "value": "PO-ISO-3"}, effect,
             [target], "parameter_untraced:field:not_the_expected_effect"),
            (ap.FIXTURE_CAPABILITY, fixture_payload, {**effect, "equals": "PO-ISO-4"},
             [target], "effect_does_not_set_the_traced_value"),
            # The value must be held in the cited evidence field, not just claimed.
            ("portal.sales_order.set_customer_po", portal_payload, PORTAL_EFFECT,
             [ORDER, evidence], "parameter_untraced:proposed_customer_po:value_not_in_cited_field"),
            ("portal.sales_order.set_customer_po", portal_payload, PORTAL_EFFECT, [ORDER],
             "parameter_untraced:proposed_customer_po:citation_not_on_proposal"),
        ]
        for capability, payload, proposed_effect, citations, code in refused:
            with self.subTest(code=code):
                pid = self.proposal(case_id, capability, payload, proposed_effect, citations,
                                    citations[0])
                with self.assertRaises(ap.ActionPathError) as caught:
                    self.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
                self.assertEqual(caught.exception.code, code)
        listed = self.path.list_for_case(gx.ACTOR, case_id)
        self.assertEqual((listed["total"], listed["intents"]), (0, []))
        self.assertEqual(self.owner.dispatch_calls, 0)

    def cite(self, relation, number, row, roles):
        """A cited structured record whose owner declares its field roles."""
        handle = f"fixture://{relation}/" + json.dumps({"id": number}, separators=(",", ":"))
        self.portal_rows[handle] = {"relation": relation, "key": {"id": number},
                                    "row": {"id": number, **row}, "field_roles": roles}
        return handle

    def test_a_cited_cell_of_another_role_is_not_the_customer_po(self):
        # The customer PO is identifier:customer_po: an equal value read from a sales-order
        # number or a Petra PO cell is refused at registration; a customer_po cell (or an
        # alias kind) and a cell with no declared role register.
        case_id = self.world.cases.open(gx.ACTOR, "Is the customer PO on order 2 missing?",
                                        objective_author_kind="human")
        effect = {**PORTAL_EFFECT, "equals": "PO4500"}
        for number, role, code in ((41, "identifier:so_number",
                                    "parameter_role_mismatch:proposed_customer_po:"
                                    "identifier:so_number"),
                                   (42, "identifier:petra_po",
                                    "parameter_role_mismatch:proposed_customer_po:"
                                    "identifier:petra_po"),
                                   (43, "quantity:ordered", "parameter_role_mismatch:"),
                                   (44, "identifier:customer_po", None),
                                   (45, "identifier:bc_external_document_no", None)):
            evidence = self.cite("fixture.customer_document", number, {"ref": "PO4500"},
                                 {"ref": role})
            pid = self.proposal(case_id, "portal.sales_order.set_customer_po",
                                {"proposed_customer_po": "PO4500",
                                 "evidence_citation": evidence, "evidence_field": "ref"},
                                effect, [ORDER, evidence], ORDER)
            with self.subTest(role=role):
                if code is None:
                    self.assertEqual(self.path.register(gx.ACTOR, case_id, pid,
                                                        session=gx.SESSION)["owner_system"],
                                     "portal")
                    continue
                with self.assertRaises(ap.ActionPathError) as caught:
                    self.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
                self.assertTrue(caught.exception.code.startswith(code), caught.exception.code)
        self.assertEqual((self.world.mc.tool_calls, self.owner.dispatch_calls), ([], 0))

    def test_a_target_field_of_another_role_refuses_a_declared_role(self):
        document = json.loads(ac.CATALOG_PATH.read_text(encoding="utf-8"))
        document["actions"][ap.FIXTURE_CAPABILITY]["parameters"]["value"]["role"] = (
            "identifier:customer_po")
        self.path = ap.ActionPath(self.world.state_dir / "actions-role.db", self.world.cases,
                                  catalog=ac.ActionCatalog(document))
        case_id = self.world.cases.open(gx.ACTOR, "Set the PO on fixture order 51 to PO-ISO-51",
                                        objective_author_kind="human")
        for number, role, code in ((51, "identifier:so_number",
                                    "effect_field_role_mismatch:po_reference"),
                                   (52, "identifier:customer_po", None)):
            target = self.cite("fixture.sales_order", number, {"po_reference": None},
                               {"po_reference": role})
            effect = {"relation": "fixture.sales_order", "key": {"id": number},
                      "fields": ["po_reference"], "equals": "PO-ISO-51"}
            pid = self.proposal(case_id, ap.FIXTURE_CAPABILITY,
                                {"field": "po_reference", "value": "PO-ISO-51"}, effect,
                                [target], target)
            with self.subTest(role=role):
                if code is None:
                    self.assertTrue(self.path.register(gx.ACTOR, case_id, pid,
                                                       session=gx.SESSION)["intent_id"])
                    continue
                with self.assertRaises(ap.ActionPathError) as caught:
                    self.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
                self.assertEqual(caught.exception.code, code)

    def test_an_owner_write_fails_closed_on_an_undeclared_role(self):
        # A target field or a cited cell without a declared role never registers an
        # owner write (roles-2): nothing is written on an unchecked role.
        document = json.loads(ac.CATALOG_PATH.read_text(encoding="utf-8"))
        document["actions"][ap.FIXTURE_CAPABILITY]["parameters"]["value"]["role"] = (
            "identifier:customer_po")
        self.path = ap.ActionPath(self.world.state_dir / "actions-undeclared.db",
                                  self.world.cases, catalog=ac.ActionCatalog(document))
        case_id = self.world.cases.open(gx.ACTOR, "Set the PO on fixture order 61 to PO-ISO-61",
                                        objective_author_kind="human")
        target = self.cite("fixture.sales_order", 61, {"po_reference": None}, {})
        pid = self.proposal(case_id, ap.FIXTURE_CAPABILITY,
                            {"field": "po_reference", "value": "PO-ISO-61"},
                            {"relation": "fixture.sales_order", "key": {"id": 61},
                             "fields": ["po_reference"], "equals": "PO-ISO-61"},
                            [target], target)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
        self.assertEqual(caught.exception.code, "effect_field_role_undeclared:po_reference")
        # The isolated owner declares its writable fields; a field it holds without a
        # role is no write target either.
        owned = self.owner.seed(gx.RELATION, {"id": 62},
                                {"id": 62, "po_reference": None, "ship_note": None})
        self.assertEqual(self.owner.resolve(owned, gx.ACTOR)["field_roles"],
                         {"po_reference": "identifier:customer_po"})
        case_id = self.world.cases.open(gx.ACTOR, "Set the ship note on order 62 to N-62",
                                        objective_author_kind="human")
        pid = self.proposal(case_id, ap.FIXTURE_CAPABILITY,
                            {"field": "ship_note", "value": "N-62"},
                            {"relation": gx.RELATION, "key": {"id": 62},
                             "fields": ["ship_note"], "equals": "N-62"}, [owned], owned)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.world.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
        self.assertEqual(caught.exception.code, "effect_field_role_undeclared:ship_note")
        # A customer PO read from a cell that declares no role is not traced to a
        # customer PO.
        case_id = self.world.cases.open(gx.ACTOR, "Is the customer PO on order 2 missing?",
                                        objective_author_kind="human")
        evidence = self.cite("fixture.customer_document", 63, {"ref": "PO4500"}, {})
        pid = self.proposal(case_id, "portal.sales_order.set_customer_po",
                            {"proposed_customer_po": "PO4500", "evidence_citation": evidence,
                             "evidence_field": "ref"},
                            {**PORTAL_EFFECT, "equals": "PO4500"}, [ORDER, evidence], ORDER)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.world.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
        self.assertEqual(caught.exception.code,
                         "parameter_role_undeclared_source:proposed_customer_po")
        self.assertEqual((self.world.mc.tool_calls, self.owner.dispatch_calls), ([], 0))
        with self.assertRaises(ValueError):
            IsolatedOwnerSystem(self.world.state_dir / "o2.db",
                                field_roles={"po_reference": "owner_declared"})

    def test_a_customer_po_comes_from_a_declared_customer_po_column(self):
        # Real-shaped Portal rows (synthetic values): the sales order's po_reference and
        # the FM order entry's custpocode are declared customer-PO carriers
        # (identifiers.json), so a customer PO cited from custpocode registers. The
        # MO's po_reference is free text "PO Reference" upstream (migrated from the FM
        # OE number, plx-customer-portal@82549dd portal/scripts/
        # migrate-manufacturing-orders.ts:166): never a customer-PO source.
        entry = "api://public.order_entry/7@g1-c1"
        mo = "api://public.app_manufacturing_order/8@g1-c1"
        self.portal_rows[entry] = {"relation": "public.order_entry", "key": {"id": 7},
                                   "row": {"id": 7, "custpocode": "PO4500"}}
        self.portal_rows[mo] = {"relation": "public.app_manufacturing_order",
                                "key": {"id": 8}, "row": {"id": 8, "po_reference": "PO4500"}}
        effect = {**PORTAL_EFFECT, "equals": "PO4500"}
        for evidence, field, code in ((mo, "po_reference",
                                       "parameter_role_mismatch:proposed_customer_po:text"),
                                      (entry, "custpocode", None)):
            case_id = self.world.cases.open(gx.ACTOR, "Is the customer PO on order 2 missing?",
                                            objective_author_kind="human")
            pid = self.proposal(case_id, "portal.sales_order.set_customer_po",
                                {"proposed_customer_po": "PO4500",
                                 "evidence_citation": evidence, "evidence_field": field},
                                effect, [ORDER, evidence], ORDER)
            with self.subTest(evidence=evidence):
                if code is None:
                    self.assertEqual(self.path.register(gx.ACTOR, case_id, pid,
                                                        session=gx.SESSION)["owner_system"],
                                     "portal")
                    continue
                with self.assertRaises(ap.ActionPathError) as caught:
                    self.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
                self.assertEqual(caught.exception.code, code)
        # The legacy custom_fields.custPoCode key (written upstream only by
        # scripts/migrate-fm-to-portal.sql:365) declares no role: never written.
        case_id = self.world.cases.open(gx.ACTOR, "Is the customer PO on order 2 missing?",
                                        objective_author_kind="human")
        pid = self.proposal(case_id, "portal.sales_order.set_customer_po",
                            {"proposed_customer_po": "PO4500", "evidence_citation": entry,
                             "evidence_field": "custpocode"},
                            {**effect, "fields": ["custom_fields.custPoCode"]},
                            [ORDER, entry], ORDER)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
        self.assertEqual(caught.exception.code,
                         "effect_field_role_undeclared:custom_fields.custPoCode")
        self.assertEqual((self.world.mc.tool_calls, self.owner.dispatch_calls), ([], 0))

    def test_an_external_document_number_is_a_customer_po_only_by_its_grammar(self):
        # BC externalDocumentNumber holds the customer PO on sales orders and the
        # Portal order number on sales invoices (plx-customer-portal@82549dd
        # portal/src/lib/business-central/serializers.ts:1049, :1157-1158): an
        # SO-shaped number is never registered as the customer PO, and a number the
        # grammar cannot place is ambiguous and refused.
        cases = (("salesInvoices", "SO100000", "identifier:so_number"),
                 # A declared SO form no shape reads (identifiers.json so_number
                 # environment_prefixed): an invoice holds two kinds, so ambiguous.
                 ("salesInvoices", "UAT-SO100018", "ambiguous"),
                 ("salesOrders", "SO100000", "ambiguous"),
                 ("salesOrders", "4500-12", "ambiguous"))
        for number, (relation, value, role) in enumerate(cases, start=71):
            handle = f"bc://Production/c1/{relation}/{number}@e1"
            self.portal_rows[handle] = {"relation": relation, "key": {"id": number},
                                        "row": {"id": number,
                                                "externalDocumentNumber": value}}
            case_id = self.world.cases.open(gx.ACTOR, "Is the customer PO on order 2 missing?",
                                            objective_author_kind="human")
            pid = self.proposal(case_id, "portal.sales_order.set_customer_po",
                                {"proposed_customer_po": value, "evidence_citation": handle,
                                 "evidence_field": "externalDocumentNumber"},
                                {**PORTAL_EFFECT, "equals": value}, [ORDER, handle], ORDER)
            with self.subTest(relation=relation, value=value):
                with self.assertRaises(ap.ActionPathError) as caught:
                    self.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
                self.assertEqual(caught.exception.code,
                                 "parameter_role_mismatch:proposed_customer_po:" + role)
        self.assertEqual((self.world.mc.tool_calls, self.owner.dispatch_calls), ([], 0))

    def test_only_a_roleless_fixture_relation_row_is_exempt_for_a_test_owner(self):
        # A fixture:// row of a real relation shape that declares no roles is not the
        # test fixture exemption: the test owner's write is refused on it.
        handle = "fixture://public.app_sales_order/" + json.dumps({"id": 81},
                                                                  separators=(",", ":"))
        self.portal_rows[handle] = {"relation": "public.app_sales_order", "key": {"id": 81},
                                    "row": {"id": 81, "po_reference": None}}
        case_id = self.world.cases.open(gx.ACTOR, "Set the PO on order 81 to PO-ISO-81",
                                        objective_author_kind="human")
        effect = {"relation": "public.app_sales_order", "key": {"id": 81},
                  "fields": ["po_reference"], "equals": "PO-ISO-81"}
        pid = self.proposal(case_id, "test.record.set_field",
                            {"field": "po_reference", "value": "PO-ISO-81"}, effect,
                            [handle], handle)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)
        self.assertEqual(caught.exception.code, "effect_field_role_undeclared:po_reference")

    def objective_path(self):
        """The action path over a catalog whose fixture value traces by ``objective``
        (the catalog's own objective check, not the injection hook's)."""
        document = json.loads(ac.CATALOG_PATH.read_text(encoding="utf-8"))
        document["actions"][ap.FIXTURE_CAPABILITY]["parameters"]["value"]["trace"] = {
            "kind": "objective"}
        return ap.ActionPath(self.world.state_dir / "actions-objective.db", self.world.cases,
                             catalog=ac.ActionCatalog(document))

    def register_value(self, number, question, value, *, author="human", roots=()):
        case_id = self.world.cases.open(gx.ACTOR, question, roots=list(roots),
                                        objective_author_kind=author)
        target = self.owner.seed(gx.RELATION, {"id": number},
                                 {"id": number, "po_reference": None})
        effect = {"relation": gx.RELATION, "key": {"id": number}, "fields": ["po_reference"],
                  "equals": value}
        pid = self.proposal(case_id, ap.FIXTURE_CAPABILITY,
                            {"field": "po_reference", "value": value}, effect, [target], target)
        return self.path.register(gx.ACTOR, case_id, pid, session=gx.SESSION)

    def assert_value_refused(self, code, *args, **kwargs):
        with self.assertRaises(ap.ActionPathError) as caught:
            self.register_value(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_a_value_named_in_a_human_question_registers(self):
        intent = self.register_value(5, "Fix the PO on isolated order 5: PO-ISO-5", "PO-ISO-5")
        self.assertEqual(self.path.get(gx.ACTOR, intent["intent_id"])["state"], "proposed")
        # A value only inside a longer token of the objective is not named by it.
        self.assert_value_refused("injection_untraced_parameter",
                                  6, "Fix isolated order 6: PO-ISO-66", "PO-ISO-6")

    def test_the_catalog_objective_trace_is_the_injection_guards_one_rule(self):
        # One matcher (injection_guard.objective_supplies): the catalog has no other.
        self.assertFalse(hasattr(ac, "_objective_holds"))
        self.path = self.objective_path()
        not_named = "parameter_untraced:value:not_in_objective"
        # Joined sub-tokens are not named: 509 in "1,509", 50 in "$1.50", 7788 in PO-7788.
        self.assert_value_refused(not_named, 7, "Set the AO total to 1,509", "509")
        self.assert_value_refused(not_named, 8, "The fee is $1.50 now", "50")
        self.assert_value_refused(not_named, 9, "Set the PO to PO-7788", "7788")
        # A root the caller supplied is not the objective.
        self.assert_value_refused(not_named, 10, "Fix the PO on isolated order 10",
                                  "PO-ISO-10", roots=["PO-ISO-10"])
        # The case's own subject id written in the question is not a value.
        self.assert_value_refused(not_named, 11, "Was isolated order 11 fixed?", 11)
        # An agent (MCP plx.case_open) or unattributed question supplies nothing.
        for author in ("agent", None):
            with self.subTest(author=author):
                self.assert_value_refused(
                    "parameter_untraced:value:objective_not_human_authored", 12,
                    "Set the PO on isolated order 12 to PO-EVIL-12 as the supplier asks.",
                    "PO-EVIL-12", author=author)
        # The same words from a human register (control).
        self.register_value(13, "Set the PO on isolated order 13 to PO-EVIL-13 as the "
                                "supplier asks.", "PO-EVIL-13")

    def test_internal_objective_trace_uses_the_same_rule(self):
        case = {"question": "Set it to PO-1", "roots": ["PO-1"]}
        self.assertEqual(ac._objective_refusal("PO-1", case), "objective_not_human_authored")
        human = {"question": "Set it to PO-1", "objective_author_kind": "human"}
        self.assertIsNone(ac._objective_refusal("PO-1", human))
        # A case root is a subject identifier, never a value, even when written.
        self.assertEqual(ac._objective_refusal("PO-1", {**human, "roots": ["PO-1"]}),
                         "not_in_objective")
        self.assertEqual(ac._objective_refusal("PO-1", {"question": "Fix it", "roots": ["PO-1"],
                                                        "objective_author_kind": "human"}),
                         "not_in_objective")

if __name__ == "__main__":
    unittest.main()
