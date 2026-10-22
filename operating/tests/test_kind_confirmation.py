"""Human-only confirmation of an original's Portal document fields (operating.kind_confirmation).

Evidence label: ``injected-transport``. Originals are attached and read through the real
``MCPServer`` / ``CaseRuntime`` with the real GET-only ``SharePointReader`` over the in-process
``FakeSender`` (fixtures of ``test_playbook_originals``); the employee resolves through the real
``EmployeePermissionResolver``. The MCP client is simulated: the test plays the person by
answering the ``elicitation/create`` request the tool sent. No network, no credential.
"""

from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from integrations.tests.test_live_source_adapters import SP_HOST, drive_item
from ontology.core import Actor
from operating import confirmation_forms as cf
from operating import kind_confirmation as kc
from operating import mcp_server
from operating import playbook_eval as pe
from operating.tests.firewall_view import client_result
from operating.tests import test_case_live_citations as live_citations
from operating.tests import test_playbook_originals as originals
from operating.tests.test_case_live_citations import SP_BASE, j
from operating.tests.test_employee_case_runtime import RICARDO_PERMS
from operating.tests.test_playbook_originals import AO, EMPLOYEE, EMPLOYEE_P, PORTAL_PO, PROFORMA

ELICITING = {"elicitation": {}}
DOWNLOAD = f"{SP_HOST}/sites/QCQA/_layouts/15/download.aspx"
OTHER_PERMS = {"value": [{"roles": ["read"], "grantedToV2": {"user": {
    "email": "someone.else@petralabx.com", "id": "x-9"}}}]}
QUOTE = "P.O. NUMBER PO5447"
ROLE = ("purchase_order_pdf", True)


def suggestion(text: str = PORTAL_PO, value: str = "SIGNED_PO", quote: str = QUOTE) -> dict:
    start = text.index(quote)
    return {"column": "DocumentCategory", "value": value, "model": "extractor-test",
            "span": {"start": start, "end": start + len(quote)}, "quote": quote}


class Environment(live_citations.LiveCitationTestCase):
    """The RecordedKindTest environment (attach, listItem columns, employee permissions)."""
    fresh = originals.RecordedKindTest.fresh
    extract = originals.RecordedKindTest.extract
    attach_sharepoint = originals.RecordedKindTest.attach_sharepoint
    evaluate = originals.RecordedKindTest.evaluate
    gaps = staticmethod(originals.RecordedKindTest.gaps)
    filled = originals.RecordedKindTest.filled

    def setUp(self) -> None:
        super().setUp()  # as RecordedKindTest.setUp, with a switchable item ACL
        self.item["sharepointIds"] = dict(originals.SP_IDS)
        self.pages, self.columns, self.perms = [PORTAL_PO], {}, RICARDO_PERMS
        self.sender.routes[f"{SP_BASE}/items/AO1/permissions"] = lambda url: j(200, self.perms)
        self.sender.routes[f"{SP_BASE}/items/AO1/listItem"] = lambda url: j(200, {
            "id": "7", "eTag": "\"list,1\"", "sharepointIds": dict(originals.SP_IDS),
            "fields": dict(self.columns, FileLeafRef="AO-26005B.pdf")})
        self.runs = 0
        self.fresh()
        self.store = kc.KindConfirmations(self.root / "confirmations.sqlite")
        self.sent: list[dict] = []

    # ------------------------------------------------------------------ helpers

    def ask(self, citation: str, actor: Actor = EMPLOYEE, caps=ELICITING, store=None,
            **arguments) -> dict:
        arguments.setdefault("model_suggestions", [suggestion()])
        return kc.call_tool({"citation": citation, **arguments},
                            confirmations=store or self.store, runtime=self.runtime,
                            actor=actor, client_capabilities=caps,
                            send_request=self.sent.append, client_name="test-client")

    def answer(self, request: dict | None = None, actor: Actor = EMPLOYEE, action="accept",
               store=None, **content) -> dict:
        request = request or self.sent[-1]
        asked = request["params"]["requestedSchema"]["properties"]
        content = {**({"DocumentCategory": "SIGNED_PO"} if "DocumentCategory" in asked else {}),
                   "confirm": True, **content}
        message = {"jsonrpc": "2.0", "id": request["id"],
                   "result": {"action": action, "content": content}}
        self.assertTrue(cf.is_response(message))
        return kc.handle_response(message, confirmations=store or self.store,
                                  runtime=self.runtime, actor=actor)

    def reader(self, server=None):
        server = server or self.employee

        def read(name, arguments):
            result = self.call(server, name, arguments)
            if result["isError"]:
                raise RuntimeError(result["content"][0]["text"])
            return result["structuredContent"]
        return read

    def kinds(self, actor=EMPLOYEE):
        return kc.confirmed_kinds(self.store, actor)

    def confirm_signed_po(self) -> dict:
        got = self.attach_sharepoint()
        self.ask(got["citation"])
        outcome = self.answer()
        self.assertTrue(outcome["confirmed"], outcome)
        return got

    def assert_get_only(self) -> None:
        methods = {call[0] for call in self.sender.api_calls()}
        self.assertEqual(methods, {"GET"})


class KindConfirmationTest(Environment):
    # ------------------------------------------------------------------ who may confirm

    def test_an_agent_principal_can_never_ask_or_answer(self):
        got = self.attach_sharepoint()
        for principals in ({"agent:claude"}, {EMPLOYEE_P, "agent:claude"},
                           {EMPLOYEE_P, "sp_workbench"}, {"*"}, {"staff:quality"},
                           {EMPLOYEE_P, "employee:someone-else"}):
            with self.subTest(principals=principals):
                with self.assertRaises(kc.ConfirmationRefused) as refused:
                    self.ask(got["citation"], actor=Actor("plx", frozenset(principals)))
                self.assertIn(refused.exception.code,
                              ("agent_or_service_principal_cannot_confirm",
                               "one_employee_principal_required"))
        self.assertEqual(self.sent, [])
        # the person's open request cannot be answered by an agent session either; the
        # refusal closes it, so it cannot be answered afterwards by anyone
        self.ask(got["citation"])
        outcome = self.answer(actor=Actor("plx", frozenset({EMPLOYEE_P, "agent:claude"})))
        self.assertEqual(outcome, {"schema": kc.SCHEMA, "confirmed": False,
                                   "refused": "agent_or_service_principal_cannot_confirm",
                                   "business_system_writes": 0})
        self.assertEqual(self.answer()["refused"], "confirmation_request_refused")
        self.assertIsNone(self.store.active("plx", got["original_id"]))

    def test_nothing_an_agent_can_see_or_copy_confirms(self):
        got = self.attach_sharepoint()
        public = self.ask(got["citation"])
        [request] = self.sent
        token = request["id"]
        self.assertEqual(public["status"], "awaiting_human_confirmation")
        self.assertNotIn(token, json.dumps(public))  # the token only travels to the client
        self.assertEqual(request["method"], "elicitation/create")
        self.assertEqual(request["params"]["mode"], "form")
        schema = request["params"]["requestedSchema"]
        self.assertEqual(schema["properties"]["DocumentCategory"]["default"], "SIGNED_PO")
        self.assertIn("SIGNED_PO", schema["properties"]["DocumentCategory"]["enum"])
        self.assertEqual(schema["properties"]["confirm"]["default"], False)
        self.assertNotIn("Confirmed document kind", json.dumps(request))
        # a recorded finding, a forged token or an unticked form is not a confirmation
        with self.assertRaises(kc.ConfirmationRefused):
            self.store.complete(self.runtime, EMPLOYEE, "cf-" + "0" * 32,
                                {"action": "accept", "content": {"confirm": True}})
        with self.assertRaises(kc.ConfirmationRefused):
            self.store.complete(self.runtime, EMPLOYEE, public["proposal_digest"],
                                {"action": "accept", "content": {"confirm": True}})
        self.assertEqual(self.answer(confirm=False)["refused"], "not_confirmed_by_the_person")
        # single use: the token is spent even by a refused answer
        self.assertEqual(self.answer()["refused"], "confirmation_request_refused")
        self.ask(got["citation"])
        self.assertEqual(self.answer(action="decline"),
                         {"schema": kc.SCHEMA, "confirmed": False, "outcome": "decline",
                          "business_system_writes": 0})
        self.assertIsNone(self.store.active("plx", got["original_id"]))

    def test_a_client_without_elicitation_gets_no_form(self):
        got = self.attach_sharepoint()
        for caps in ({}, None, {"elicitation": {"url": {}}}, {"sampling": {}}):
            with self.subTest(caps=caps):
                with self.assertRaises(kc.ConfirmationRefused) as refused:
                    self.ask(got["citation"], caps=caps)
                self.assertTrue(refused.exception.code.startswith(
                    "human_confirmation_surface_unavailable"))
        self.assertEqual(self.sent, [])
        self.assertTrue(kc.client_supports_elicitation({"elicitation": {"form": {}}}))

    # ------------------------------------------------------------------ binding

    def test_the_confirmation_binds_the_proposal_digest(self):
        got = self.attach_sharepoint()
        public = self.ask(got["citation"])
        digest = public["proposal_digest"]
        # the caller's digest must be the current proposal's
        with self.assertRaises(kc.ConfirmationRefused) as refused:
            self.ask(got["citation"], expected_proposal_digest="f" * 64)
        self.assertEqual(refused.exception.code, "proposal_changed")
        # another proposal (other model suggestion) has another digest and supersedes the
        # earlier open request: the person can only answer the latest form they saw
        first = self.sent[-1]
        other = self.ask(got["citation"], model_suggestions=[], columns=["DocumentCategory"])
        self.assertNotEqual(other["proposal_digest"], digest)
        self.assertEqual(self.answer(first)["refused"], "confirmation_request_superseded")
        # the proposal moves between the question and the answer: refused
        public = self.ask(got["citation"], expected_proposal_digest=digest)
        self.assertEqual(public["proposal_digest"], digest)
        from operating import case_context_tools
        real = case_context_tools.document_metadata

        def moved(*args, **kwargs):
            result = real(*args, **kwargs)
            for row in result["columns"]:
                if row["column"] == "DocumentCategory":
                    row.update(status="model_suggestion", value="QUOTE")
            return result
        with mock.patch.object(case_context_tools, "document_metadata", moved):
            self.assertEqual(self.answer()["refused"], "proposal_or_original_changed")
        self.assertIsNone(self.store.active("plx", got["original_id"]))
        # an answer within the window records the digest it was given for
        self.ask(got["citation"], expected_proposal_digest=digest)
        outcome = self.answer()
        record = self.store.active("plx", got["original_id"])
        self.assertEqual((record["id"], record["proposal_digest"], record["confirmed_by"]),
                         (outcome["confirmation_id"], digest, EMPLOYEE_P))
        self.assertEqual(record["confirmed_fields"], {"DocumentCategory": "SIGNED_PO"})
        self.assertEqual(record["provenance"]["etag"], "\"{AO1},1\"")
        self.assertFalse(record["written_to_sources"])

    def test_a_changed_original_is_not_confirmed(self):
        got = self.confirm_signed_po()
        # the SharePoint item now holds other bytes: attaching it is a new original
        content = live_citations.AO_PDF + b"% revision 2\n"
        self.item = dict(drive_item("AO1", "AO-26005B.pdf", folder="/AOs/2026 - AOs",
                                    content=content, etag="\"{AO1},2\""),
                         cTag="\"c:{AO1},2\"", sharepointIds=dict(originals.SP_IDS))
        self.sender.routes[DOWNLOAD] = lambda url: (200, {}, content)
        changed = self.attach_sharepoint()
        self.assertNotEqual(changed["original_id"], got["original_id"])
        self.assertFalse(self.store.status(self.runtime, EMPLOYEE,
                                           changed["citation"])["confirmed"])
        # the case cites both: the confirmed bytes still fill, the new bytes do not
        report = self.evaluate(ROLE, fields=None)
        self.assertEqual(self.filled(report, "purchase_order_pdf"), [got["citation"]])
        self.assertIn(("role_unverifiable", changed["citation"]), self.gaps(report))
        # a confirmation that names other bytes is never used for this original
        record = self.store.active("plx", got["original_id"])
        report = self.evaluate(ROLE, fields=None, confirmations=lambda oid: record)
        doc = next(d for d in report["original_checks"]["originals"]
                   if d["handle"] == changed["citation"])
        self.assertEqual(doc["workbench_confirmation"], {"status": "not_this_original"})
        # the old proposal's digest does not bind the new original
        old = self.store.active("plx", got["original_id"])["proposal_digest"]
        with self.assertRaises(kc.ConfirmationRefused) as refused:
            self.ask(changed["citation"], expected_proposal_digest=old)
        self.assertEqual(refused.exception.code, "proposal_changed")

    def test_expired_requests_are_refused(self):
        now = [datetime(2026, 10, 1, 12, tzinfo=timezone.utc)]
        store = kc.KindConfirmations(self.root / "clocked.sqlite", clock=lambda: now[0],
                                     ttl_seconds=60)
        got = self.attach_sharepoint()
        self.ask(got["citation"], store=store)
        now[0] += timedelta(seconds=61)
        self.assertEqual(self.answer(store=store)["refused"], "confirmation_request_expired")

    # ------------------------------------------------------------------ permission

    def test_a_reader_without_permission_to_the_original_is_refused(self):
        got = self.attach_sharepoint()
        self.perms = OTHER_PERMS  # the item ACL no longer grants this employee
        with self.assertRaises(kc.ConfirmationRefused) as refused:
            self.ask(got["citation"])
        self.assertEqual(refused.exception.code, "original_not_visible")
        with self.assertRaises(kc.ConfirmationRefused):
            self.store.status(self.runtime, EMPLOYEE, got["citation"])
        # permission lost between the question and the answer
        self.perms = RICARDO_PERMS
        self.ask(got["citation"])
        self.perms = OTHER_PERMS
        self.assertEqual(self.answer()["refused"], "original_not_visible")
        self.assertIsNone(self.store.active("plx", got["original_id"]))
        # a confirmation never fills a role for a reader who cannot read the original now:
        # the evaluator binds it to its own read of the original, which this reader is refused
        self.perms = RICARDO_PERMS
        self.ask(got["citation"])
        self.assertTrue(self.answer()["confirmed"])
        self.assertEqual(self.filled(self.evaluate(ROLE, fields=None), "purchase_order_pdf"),
                         [got["citation"]])
        self.perms = OTHER_PERMS
        report = self.evaluate(ROLE, fields=None)
        self.assertEqual(self.filled(report, "purchase_order_pdf"), [])
        # the case shows the citation to this reader only as withheld
        self.assertIn(("cited_evidence_withheld_from_reader", None), self.gaps(report))

    def test_status_and_the_evaluator_share_one_binding_rule(self):
        got = self.confirm_signed_po()
        record = self.store.active("plx", got["original_id"])
        view = self.runtime.read_original(got["citation"], EMPLOYEE)
        self.assertTrue(originals.po.confirmation_binds(record, view))
        for key, value in (("etag", "\"{AO1},9\""), ("ctag", "\"c:{AO1},9\""),
                           ("item_id", "AO2"), ("drive_id", "other"), ("source", "x")):
            with self.subTest(key=key):
                moved = {**view, "provenance": {**view["provenance"], key: value}}
                self.assertFalse(originals.po.confirmation_binds(record, moved))
        self.assertFalse(originals.po.confirmation_binds(record, {**view, "sha256": "0" * 64}))
        with mock.patch.object(kc, "confirmation_binds", lambda record, view: False):
            status = self.store.status(self.runtime, EMPLOYEE, got["citation"])
        self.assertEqual((status["confirmed"], status["stale"]), (False, "original_changed"))

    def test_a_refused_read_or_store_failure_is_incomplete_never_unconfirmed(self):
        """A person's confirmation is never silently dropped."""
        got = self.confirm_signed_po()

        def refusing(name, arguments):
            if name == "plx.source_citation" and arguments.get("citation") == got["citation"]:
                raise pe.EvaluationError("evaluation_incomplete: rate_limit_exceeded")
            return self.ok(self.employee, name, arguments)
        with self.assertRaises(pe.EvaluationError):
            pe.evaluate(originals.with_roles(ROLE), refusing, self.case_id,
                        kind_confirmations=self.kinds())

        def broken(original_id):
            raise RuntimeError("database is locked")
        with self.assertRaisesRegex(pe.EvaluationError, "kind confirmation read refused"):
            self.evaluate(ROLE, fields=None, confirmations=broken)

    # ------------------------------------------------------------------ consumption

    def test_confirmed_fields_fill_the_role_and_write_nothing(self):
        got = self.attach_sharepoint()
        before = self.evaluate(ROLE, fields=None)
        self.assertEqual(self.filled(before, "purchase_order_pdf"), [])
        self.ask(got["citation"])
        self.answer()
        record = self.store.active("plx", got["original_id"])
        for fields in (None, "runtime"):  # without and with the live (empty) item column
            with self.subTest(fields=fields):
                report = self.evaluate(ROLE, fields=fields)
                checks = report["original_checks"]
                self.assertTrue(checks["ok"], checks["gaps"])
                self.assertEqual(self.filled(report, "purchase_order_pdf"), [got["citation"]])
                [doc] = checks["originals"]
                self.assertEqual(doc["kind_sources"], [{
                    "source": "workbench_confirmation", "kind": "purchase_order_pdf",
                    "column": "DocumentCategory", "value": "SIGNED_PO",
                    "confirmation": record["id"], "confirmed_by": EMPLOYEE_P,
                    "at": record["confirmed_at"], "channel": "mcp_elicitation"}])
                self.assertEqual(report["public_original_checks"]["originals"][0]
                                 ["kind_sources"], ["workbench_confirmation"])
                self.assertNotIn(EMPLOYEE_P, json.dumps(report["public_original_checks"]))
        status = self.store.status(self.runtime, EMPLOYEE, got["citation"])
        self.assertEqual((status["confirmed"], status["confirmed_fields"],
                          status["confirmed_by_you"]),
                         (True, {"DocumentCategory": "SIGNED_PO"}, True))
        self.assert_get_only()

    def test_text_still_contradicts_a_confirmed_kind(self):
        self.pages = [PROFORMA]
        got = self.attach_sharepoint()
        self.ask(got["citation"], model_suggestions=[], columns=["DocumentCategory"])
        self.assertTrue(self.answer()["confirmed"])
        report = self.evaluate(ROLE, fields=None)
        self.assertIn(("document_kind_ambiguous", got["citation"]), self.gaps(report))

    def test_a_live_column_that_disagrees_is_ambiguous(self):
        got = self.confirm_signed_po()
        for value in ("QUOTE", "COA"):  # consistent with other kinds / names another kind
            with self.subTest(value=value):
                self.columns = {"DocumentCategory": value}
                report = self.evaluate(ROLE)
                self.assertIn(("document_kind_ambiguous", got["citation"]), self.gaps(report))
                self.assertEqual(self.filled(report, "purchase_order_pdf"), [])
        self.columns = {"DocumentCategory": "Signed PO"}  # the same value, Portal-folded
        report = self.evaluate(ROLE)
        self.assertEqual(self.filled(report, "purchase_order_pdf"), [got["citation"]])
        self.assertEqual(sorted(s["source"] for s in report["original_checks"]["originals"][0]
                                ["kind_sources"]),
                         ["sharepoint_column", "workbench_confirmation"])

    def test_a_confirmed_document_kind_fills_a_role_the_portal_cannot_name(self):
        self.pages = [originals.FORWARDER_QUOTE]
        got = self.attach_sharepoint()
        self.ask(got["citation"], model_suggestions=[], proposed_document_kind="freight_quote")
        form = self.sent[-1]["params"]["requestedSchema"]
        self.assertEqual(form["properties"][kc.DOCUMENT_KIND]["default"], "freight_quote")
        self.assertIn("not a SharePoint column", form["properties"][kc.DOCUMENT_KIND]["title"])
        self.assertTrue(self.answer(**{kc.DOCUMENT_KIND: "freight_quote"})["confirmed"])
        record = self.store.active("plx", got["original_id"])
        self.assertEqual((record["document_kind"], record["confirmed_fields"]),
                         ("freight_quote", {}))
        report = self.evaluate(("freight_quote", True))
        self.assertEqual(self.filled(report, "freight_quote"), [got["citation"]])

    def test_a_later_confirmation_supersedes_and_records_the_correction(self):
        got = self.confirm_signed_po()
        first = self.store.active("plx", got["original_id"])
        self.ask(got["citation"])
        outcome = self.answer(DocumentCategory="QUOTE")
        self.assertEqual((outcome["supersedes"], outcome["amended_columns"]),
                         (first["id"], ["DocumentCategory"]))
        self.assertEqual(self.store.active("plx", got["original_id"])["confirmed_fields"],
                         {"DocumentCategory": "QUOTE"})
        self.assertEqual(len(self.store.history("plx", got["original_id"])), 2)
        # a value the Portal does not define is refused
        self.ask(got["citation"])
        self.assertEqual(self.answer(DocumentCategory="FREIGHT_QUOTE")["refused"],
                         "answer_value_not_a_portal_value")
        # QUOTE is consistent with a freight quote, never a fill (Portal has no such category)
        report = self.evaluate(ROLE, fields=None)
        self.assertEqual(self.filled(report, "purchase_order_pdf"), [])


class PortalValuesTest(unittest.TestCase):
    def test_a_person_confirms_only_values_the_portal_writes(self):
        """TASK-2215 (plx-customer-portal 2917255): DocStatus gains Superseded and new writes
        use the PLX retention values, while legacy 7_YEAR values stay valid."""
        for column, value, stored in (("DocStatus", "Superseded", "Superseded"),
                                      ("RetentionCategory", "7 years", "7 years"),
                                      ("RetentionCategory", " 7_YEAR ", "7_YEAR"),
                                      ("RetentionCategory", "Seven years", None),
                                      ("DocStatus", "Signed", None)):
            self.assertEqual(kc._valid_value(column, value), stored, (column, value))


class ReviewImportOrderTest(unittest.TestCase):
    """An earlier plx.review document type imported after a newer confirmation of the
    same file never supersedes it: the newer one stays current, no window is inverted; an
    equal one under another id is held; a withdrawal is never time-guarded."""

    def test_an_older_review_never_supersedes_a_newer_confirmation(self):
        import tempfile
        from contextlib import closing
        from pathlib import Path

        binding = {"citation": "sharepoint://D/I@E", "original_sha256": "a" * 64,
                   "provenance": {"source": kc.SHAREPOINT_ORIGIN, "drive_id": "D",
                                  "item_id": "I", "etag": "E", "ctag": None,
                                  "source_hashes": None}}
        cite = [{"object_id": "SharePointFile:x", "l1_handle": "sharepoint://D/I@E",
                 "digest": "d" * 64}]

        class Legacy:   # the review store: one document type in force since Sept 1
            kind, by = "QUOTE", "employee:a"

            def kind_history(self, tenant):
                return [{"review_id": "rev-" + "1" * 32, "decision": "document_kind",
                         "review_key": "k", "subject": "SharePointFile:x",
                         "document_kind": self.kind, "evidence": cite,
                         "author": {"principal": self.by},
                         "recorded_at": "2026-09-01T00:00:00.000000Z", "seq": 1}]

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(kc, "sharepoint_binding", lambda conn, oid, at: binding):
            path = Path(tmp) / "k.sqlite"
            store = kc.KindConfirmations(path)
            newer = kc.review_record("plx", "SharePointFile:x", binding, "INVOICE", cite,
                                     {"principal": "employee:b"}, "2026-10-01T00:00:00.000000Z")
            with closing(store._db()) as db:
                store._append(db, newer)
            for _ in range(2):
                self.assertEqual(store.import_reviews(None, Legacy(), "plx"),
                                 {"imported": 0, "held": 0, "not_bound": 0, "newer_current": 1})
            self.assertEqual(store.active("plx", "SharePointFile:x")["id"], newer["id"])
            records, _ = kc._current_records(path, "plx")
            self.assertEqual([(r["portal_document_type"], until) for r, _, until in records],
                             [("INVOICE", None)])
            # The same type, bytes, evidence and person under another id: held, not newer.
            legacy = Legacy()
            legacy.kind, legacy.by = "INVOICE", "employee:b"
            self.assertEqual(store.import_reviews(None, legacy, "plx"),
                             {"imported": 0, "held": 1, "not_bound": 0, "newer_current": 0})
            # A withdrawal stamped before the current one's time (clock skew) is still
            # recorded and ends it; nothing projects from it.
            withdrawal = store.withdraw("plx", newer["id"], {"principal": "employee:b"},
                                        "2026-09-15T00:00:00.000000Z")
            self.assertEqual((withdrawal["retracts"], withdrawal["supersedes"],
                              store.active("plx", "SharePointFile:x")),
                             (newer["id"], newer["id"], None))
            with self.assertRaises(kc.ConfirmationRefused) as again:
                store.withdraw("plx", newer["id"], {"principal": "employee:b"},
                               "2026-10-02T00:00:00.000000Z")
            self.assertEqual(again.exception.code, "already_retracted")


class McpRegistrationTest(Environment):
    """``plx.document_fields`` (modes request, status) through ``MCPServer.handle`` and
    ``serve_stdio``."""

    def mcp(self, principals=frozenset({EMPLOYEE_P}), caps=ELICITING, store="store"):
        server = mcp_server.MCPServer(
            case_runtime=self.runtime, actor=Actor("plx", principals, clearance="restricted"),
            kind_confirmations=self.store if store == "store" else store)
        initialized = server.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": caps,
                       "clientInfo": {"name": "human-client", "version": "1"}}})
        self.assertIn("result", initialized)
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def tool(self, server, arguments):
        self.request_id += 1
        return client_result("plx.document_fields", server.handle({
            "jsonrpc": "2.0", "id": self.request_id, "method": "tools/call",
            "params": {"name": "plx.document_fields",
                       "arguments": arguments}})["result"])  # through the output boundary

    def arguments(self, got):
        return {"citation": got["citation"], "mode": "request",
                "model_suggestions": [suggestion()]}

    def test_the_form_goes_to_the_client_and_its_answer_confirms(self):
        got = self.attach_sharepoint()
        server = self.mcp()
        listed = server.handle({"jsonrpc": "2.0", "id": "l", "method": "tools/list"})
        self.assertIn("plx.document_fields",
                      [tool["name"] for tool in listed["result"]["tools"]])
        result = self.tool(server, self.arguments(got))
        self.assertFalse(result["isError"], result)
        [request] = server.outbound
        self.assertEqual(request["method"], "elicitation/create")
        self.assertNotIn(request["id"], json.dumps(result))
        # the client's answer is a JSON-RPC response: no reply, and it confirms
        answer = {"jsonrpc": "2.0", "id": request["id"], "result": {
            "action": "accept", "content": {"DocumentCategory": "SIGNED_PO", "confirm": True}}}
        self.assertIsNone(server.handle(answer))
        record = self.store.active("plx", got["original_id"])
        self.assertEqual((record["confirmed_fields"], record["client"]),
                         ({"DocumentCategory": "SIGNED_PO"}, "human-client"))
        status = self.tool(server, {"citation": got["citation"], "mode": "status"})
        self.assertTrue(status["structuredContent"]["confirmed"])
        # replaying the answer changes nothing and gets no reply
        self.assertIsNone(server.handle(answer))
        self.assertEqual(len(self.store.history("plx", got["original_id"])), 1)

    def test_other_messages_without_a_method_are_still_invalid(self):
        server = self.mcp()
        for message in ({"jsonrpc": "2.0", "id": 7, "result": {}},
                        {"jsonrpc": "2.0", "id": "x-1", "result": {}},
                        {"jsonrpc": "2.0", "id": "cf-" + "0" * 32}):
            with self.subTest(message=message):
                response = server.handle(message)
                self.assertEqual(response["error"]["message"], "Invalid Request")

    def test_an_agent_session_and_a_client_without_elicitation_are_refused(self):
        got = self.attach_sharepoint()
        for server, code in (
                (self.mcp(frozenset({EMPLOYEE_P, "agent:claude"})),
                 "agent_or_service_principal_cannot_confirm"),
                (self.mcp(frozenset({"agent:claude"})),
                 "agent_or_service_principal_cannot_confirm"),
                (self.mcp(caps={}), "human_confirmation_surface_unavailable"),
                (self.mcp(store=None), "kind_confirmations_not_configured")):
            with self.subTest(code=code):
                result = self.tool(server, self.arguments(got))
                self.assertTrue(result["isError"])
                self.assertIn(code, result["content"][0]["text"])
                self.assertEqual(server.outbound, [])
        self.assertIsNone(self.store.active("plx", got["original_id"]))

    def test_serve_stdio_writes_the_form_and_reads_the_answer(self):
        got = self.attach_sharepoint()
        server = mcp_server.MCPServer(case_runtime=self.runtime,
                                      actor=Actor("plx", frozenset({EMPLOYEE_P})),
                                      kind_confirmations=self.store)
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": ELICITING,
                "clientInfo": {"name": "stdio-client", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "plx.document_fields", "arguments": self.arguments(got)}}]
        out = io.BytesIO()
        with mock.patch.object(mcp_server, "CALLS_PER_MINUTE", 10 ** 6):
            mcp_server.serve_stdio(server, input_stream=io.BytesIO(
                b"".join(json.dumps(line).encode() + b"\n" for line in lines)),
                output_stream=out)
        written = [json.loads(line) for line in out.getvalue().splitlines()]
        # the form goes out while the tool call is in flight, before its result
        self.assertEqual([m.get("id") for m in (written[0], written[2])], [1, 2])
        request = written[1]
        self.assertEqual(request["method"], "elicitation/create")
        self.assertEqual(server.outbound, [])
        answer = {"jsonrpc": "2.0", "id": request["id"], "result": {
            "action": "accept", "content": {"DocumentCategory": "SIGNED_PO", "confirm": True}}}
        out = io.BytesIO()
        mcp_server.serve_stdio(server, input_stream=io.BytesIO(
            json.dumps(answer).encode() + b"\n"), output_stream=out)
        self.assertEqual(out.getvalue(), b"")
        self.assertEqual(self.store.active("plx", got["original_id"])["confirmed_fields"],
                         {"DocumentCategory": "SIGNED_PO"})
        self.assert_get_only()


if __name__ == "__main__":
    unittest.main()
