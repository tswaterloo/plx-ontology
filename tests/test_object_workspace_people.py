"""Object workspace console routes for people and records: the person controls (Planner
follow-ups, MC approvals, aliases), recording an investigate answer on a case, grant
snapshots, the store's provenance and host access requests.

Split from ``tests/test_object_workspace_http.py`` (NW-16) so each part fits the per-module
limit; the fixture world and evidence label are that module's: isolated-fixture /
injected-transport, no network, nothing live.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
from http.client import HTTPConnection
import importlib.util
import json
import os
from pathlib import Path
from queue import Queue
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote
from ontology.core import Actor
from ontology.reviews import STORE_FILE as REVIEW_STORE_FILE, ReviewStore
from operating.action_path import ActionPath
from operating.case_runtime import CaseRuntime
from operating.employee_permissions import (EmployeePermissionPolicy, EmployeePermissionResolver,
                                            PortalAccessSnapshot)
from operating.cases import PROPOSAL_STATUS
from operator_api.workspace import case_console
from operator_api.tests import case_fixtures
from operator_api.workspace import object_workspace as objects
from operating import goal2_mc_approval as g2
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt
from operating import session_identity
from operator_api.workspace.case_workspace import CaseWorkspace
from operating.host import BC_DIR, BC_FILE
from operator_api.tests.console_harness import make_server
from operator_api.workspace.session_journal import SessionJournal, ask_counts, investigate_counts
from tests import test_investigate_landed_cost as landed
from types import SimpleNamespace
from tests.test_object_workspace_http import OPS


MC_OPERATOR = "workbench.operator@isolated-fixture.example"
FOLLOW_UP_BUCKET = "BKT-ISOLATED-FOLLOW-UPS"
ALIAS_A = "ricardo.alias@petralabx.com"   # another address of the same Entra user


class PersonControlsTest(unittest.TestCase):
    """The workspace's two controls over HTTP: an employee confirms a link (``plx.review``
    into the Workbench review store, surviving a console restart) and proposes a
    follow-up (an MC follow-up task intent whose exact call waits for another person's
    approval; nothing is sent). Seeded fixture mirror, in-process consoles on 127.0.0.1,
    signed test tickets; evidence: isolated-fixture."""

    @classmethod
    def setUpClass(cls) -> None:
        from operating.tests import test_session_identity as ids

        cls.ids = ids
        cls.tmp = Path(tempfile.mkdtemp(prefix="object-controls-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        cls.store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, cls.store)
        cls.policy, cls.portal = ids.identity_files(cls.tmp / "identity", {
            case_fixtures.OPERATIONS: ("VIEW_OPERATIONS", "operations.sales", "ops"),
            "review:identity": ("VIEW_OPERATIONS", "operations.sales", "ops")})

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def console(self, *, live, audience=None, ticket=None, planner=None, principals=OPS):
        """A console as ``case_console`` serves it: live, every request's employee from
        its signed ticket (``TicketSessions``; ``ticket``, default a fresh one); else the
        fixture operations session. ``audience``: the follow-up bucket's MC audience
        (``case_console`` takes the committed file's, ``load_bucket_audiences``)."""
        from operating.employee_permissions import (EmployeePermissionPolicy,
                                                    EmployeePermissionResolver,
                                                    PortalAccessSnapshot)
        ready: Queue = Queue(maxsize=1)

        def run():
            try:
                resolver = (EmployeePermissionResolver(
                    EmployeePermissionPolicy.from_file(self.policy),
                    PortalAccessSnapshot(self.portal)) if live else None)
                runtime = CaseRuntime(self.state, client=None, tenant="plx",
                                      audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                                      employee_permissions=resolver, require_employee=live)

                path = ActionPath(self.state / case_fixtures.ACTIONS_FILE, runtime.cases,
                                  mc_operator_email=MC_OPERATOR,
                                  mc_follow_up_bucket=FOLLOW_UP_BUCKET,
                                  mc_follow_up_audience=audience)

                def objects_for(session):
                    return case_console.object_workspace_for(
                        runtime, self.store, session.actor, state_dir=self.state,
                        session=session, action_path=path, planner=planner)
                fixture = tickets = None
                if live:
                    tickets = case_console.TicketSessions(
                        resolver, self.ids.SECRET,
                        lambda session: CaseWorkspace(runtime, session.actor),
                        build_objects=objects_for,
                        replay_guard=session_identity.replay_guard_for(self.state))
                    server = make_server(ticket_sessions=tickets,
                                         journal=SessionJournal.for_state_dir(self.state))
                else:
                    session = session_identity.fixture_session(
                        "plx", principals, channel=session_identity.CHANNEL_CONSOLE)
                    fixture = objects_for(session)
                    server = make_server(session=session,
                                         case_workspace=CaseWorkspace(runtime, session.actor),
                                         object_workspace=fixture)
                ready.put(server)
                server.serve_forever(poll_interval=0.05)
                server.server_close()
                for view in (fixture, tickets):
                    if view is not None:
                        view.close()
                runtime.close()
            except BaseException as error:  # surfaced to the test thread
                ready.put(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        server = ready.get(timeout=60)
        if isinstance(server, BaseException):
            raise server
        stop = lambda: (server.shutdown(), thread.join(timeout=10))  # noqa: E731
        self.addCleanup(stop)
        bearer = ((ticket or self.ids.Fixture.ticket()) if live
                  else server.operator_access_token)

        def call(method, path, body=None, *, csrf=True):
            conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
            headers = {"Authorization": "Bearer " + bearer}
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
                if csrf:
                    headers["X-Operator-Token"] = call.csrf
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            value = response.status, json.loads(response.read())
            conn.close()
            return value
        status, session = call("GET", "/api/session")
        self.assertEqual(status, 200, session)
        call.csrf, call.stop = session["csrf_token"], stop
        if live:   # the journal's key for this session (SessionIdentity.session_ref)
            call.session_ref = hashlib.sha256(session_identity.verify_session_ticket(
                bearer, self.ids.SECRET)["jti"].encode()).hexdigest()
        return call

    def open_po(self, call):
        status, found = call("GET", "/api/objects?q=" + quote(case_fixtures.PO_NUMBER, safe=""))
        self.assertEqual(status, 200, found)
        po = next(c["object"]["object_id"] for c in found["find"]["candidates"]
                  if c["object"]["object_type"] == "PurchaseOrder")
        status, view = call("GET", "/api/object?id=" + quote(po, safe=""))
        self.assertEqual(status, 200, view)
        return po, view

    def test_an_employee_confirms_a_link_and_the_decision_survives_a_restart(self):
        call = self.console(live=True)
        po, view = self.open_po(call)
        self.assertEqual(view["reviews"], {"status": "read", "reviews": [], "withheld": 0,
                                           "unread_subjects": 0})
        edge = next(e for hop in view["around"]["hops"] for e in hop["result"]["edges"]
                    if e["from"] == po)
        shown = view["card"]["values"][0]["citation"]
        decision = {"decision": "accept_link", "subject": edge["from"], "target": edge["to"],
                    "link_type": edge["link_type"],
                    "evidence": [{key: shown[key] for key in ("l1_handle", "digest",
                                                              "field_path")}]}
        # The CSRF token is required; the author is never a body field.
        self.assertEqual(call("POST", "/api/object-review", decision, csrf=False)[0], 403)
        self.assertEqual(call("POST", "/api/object-review",
                              dict(decision, author="someone-else"))[0], 400)
        # A version the person was not shown is refused, nothing recorded.
        status, refused = call("POST", "/api/object-review", dict(decision, evidence=[
            dict(decision["evidence"][0], digest="0" * 64)]))
        self.assertEqual((status, refused["error"]), (409, "evidence_changed"))
        status, recorded = call("POST", "/api/object-review", decision)
        self.assertEqual(status, 200, recorded)
        review = recorded["review"]["review"]
        self.assertEqual((review["decision"], review["state"], review["author"]["principal"],
                          recorded["business_system_writes"]),
                         ("accept_link", "effective", "employee:" + self.ids.OID_A, 0))
        call.stop()
        # The session journal recorded what the employee opened and decided, as counts:
        # the panels the object view showed, each review attempt by its answer.
        recorded = SessionJournal.for_state_dir(self.state).summary(
            call.session_ref)["sessions"][call.session_ref]
        self.assertEqual(recorded["objects_opened"], {"PurchaseOrder": 1})
        self.assertEqual({name: recorded["panel_items"][name] for name in (
            "timeline", "originals", "gaps", "conflicts", "reviews")},
            {"timeline": view["timeline"]["total"], "originals": view["originals"]["total"],
             "gaps": view["gaps"]["total"], "conflicts": len(view["conflicts"]),
             "reviews": 0})
        self.assertTrue(any(recorded["panel_items"].values()), recorded)   # not vacuous
        self.assertEqual({key: count for key, count in recorded["by_route"].items()
                          if "/api/object-review" in key},
                         {"POST /api/object-review 403": 1, "POST /api/object-review 400": 1,
                          "POST /api/object-review 409": 1, "POST /api/object-review 200": 1})
        dumped = json.dumps(recorded)
        for value in (po, edge["to"], case_fixtures.PO_NUMBER, self.ids.OID_A):
            self.assertNotIn(value, dumped)

        restarted = self.console(live=True)
        _, again = self.open_po(restarted)
        self.assertEqual([(r["review_id"], r["decision"], r["target"], r["link_type"],
                           r["state"]) for r in again["reviews"]["reviews"]],
                         [(review["review_id"], "accept_link", edge["to"],
                           edge["link_type"], "effective")])

    def test_ask_and_investigate_over_http_reach_the_session_journal_as_counts(self):
        def planner(request):
            card = request["transcript"][0]["observations"][0]["result"]
            cell = next(v for v in card["values"]
                        if v.get("citation") and v.get("value") is not None)
            value = cell["value"]
            return {"action": "answer", "unknowns": [], "text": "It is on record as {v}.",
                    "claims": [{"id": "v", "citation": cell["citation"]["l1_handle"],
                                "field": cell["citation"]["field_path"],
                                "value": value["untrusted_text"] if isinstance(value, dict)
                                else value}]}, None

        call = self.console(live=True, planner=planner)
        po, _ = self.open_po(call)
        status, asked = call("POST", "/api/object-ask",
                             {"object_id": po, "question": "Is it confirmed?"})
        self.assertEqual(status, 200, asked)
        status, investigated = call("GET", "/api/investigate?family=three_way_match&root="
                                    + quote(po, safe=""))
        self.assertEqual(status, 200, investigated)
        call.stop()
        recorded = SessionJournal.for_state_dir(self.state).summary(
            call.session_ref)["sessions"][call.session_ref]
        ask, investigation = ask_counts(asked), investigate_counts(investigated)
        self.assertGreater(ask["citations"], 0)           # not vacuous
        self.assertGreater(investigation["citations"], 0)
        self.assertEqual({key: count for key, count in recorded["by_route"].items()
                          if "ask" in key or "investigate" in key},
                         {"POST /api/object-ask 200": 1, "GET /api/investigate 200": 1})
        self.assertEqual(recorded["panel_items"]["citations"],
                         ask["citations"] + investigation["citations"])
        self.assertEqual(recorded["panel_items"]["claims"],
                         ask["claims"] + investigation["claims"])
        dumped = json.dumps(recorded)
        for value in (po, case_fixtures.PO_NUMBER, self.ids.OID_A):
            self.assertNotIn(value, dumped)

    def test_an_employee_corrects_a_value_and_it_shows_beside_the_source_after_a_restart(self):
        # Its own state directory: the link test reads every review of the PO's lines.
        self.state = Path(tempfile.mkdtemp(dir=self.tmp)) / "state"
        case_fixtures.seed(self.state, "plx")
        self.store = self.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(self.state, self.store)
        call = self.console(live=True)
        po, view = self.open_po(call)
        line = next(e["from"] for hop in view["around"]["hops"] for e in hop["result"]["edges"]
                    if e["to"] == po and e["from"].startswith("PurchaseOrderLine:"))
        status, opened = call("GET", "/api/object?id=" + quote(line, safe=""))
        self.assertEqual(status, 200, opened)
        shown = next(v for v in opened["card"]["values"] if v["property"] == "ordered_quantity")
        cited = {key: shown["citation"][key] for key in ("l1_handle", "digest", "field_path")}
        decision = {"decision": "correct_value", "subject": line,
                    "value": {"property": "ordered_quantity", "value": "12000",
                              "unit": shown["unit"]},
                    "evidence": [dict(cited, start=0, end=len(shown["value"]))]}
        # A correction that cites no page or span of what was read is refused.
        status, refused = call("POST", "/api/object-review", dict(decision, evidence=[cited]))
        self.assertEqual((status, refused["error"]), (409, "citation_required"))
        status, recorded = call("POST", "/api/object-review", decision)
        self.assertEqual(status, 200, recorded)
        review = recorded["review"]["review"]
        self.assertEqual((review["decision"], review["state"], review["author"]["principal"],
                          recorded["review"]["projection"], recorded["business_system_writes"]),
                         ("correct_value", "effective", "employee:" + self.ids.OID_A,
                          "plx_object_now", 0))
        call.stop()

        restarted = self.console(live=True)
        status, again = restarted("GET", "/api/object?id=" + quote(line, safe=""))
        self.assertEqual(status, 200, again)
        value = next(v for v in again["card"]["values"] if v["property"] == "ordered_quantity")
        self.assertEqual((value["value"], value["review"]["value"], value["review"]["unit"],
                          value["review"]["tier"], value["review"]["review_id"]),
                         (shown["value"], "12000", shown["unit"], "reviewed",
                          review["review_id"]))

    def add_packets(self):
        """Two synthetic AO packets in this store, each one Portal-cited quantity: P1
        reaches the fixture's sales order and a second one over the many-to-many
        ``AssemblyOrderPacket.for_sales_order``; P2 reaches the fixture's order and one
        whose audience no reader holds. Rows added to the projected store as a build would
        write them (synthetic identifiers, isolated-fixture)."""
        conn = sqlite3.connect(self.store)
        self.addCleanup(conn.close)
        so, at = conn.execute("SELECT object_id, recorded_at FROM object_versions WHERE "
                              "object_id LIKE 'SalesOrder:%'").fetchone()

        def obj(object_id, relation, native):
            handle = f"api://{relation}/%7B%22id%22%3A{native}%7D@synthetic-1"
            conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                         (object_id, object_id.split(":")[0], object_id.split(":")[1],
                          json.dumps({"id": native}), "api:" + relation, at, None))
            conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                         (object_id, handle, "%064x" % native, "fm_bronze", "[]", at, None))
            return handle

        packets, others = [], []
        for native, relation in ((99001, "public.order_entry"),
                                 (99002, "public.isolated_withheld_orders")):
            packet = f'AssemblyOrderPacket:sharepoint.ao_packets:{{"id":{native}}}'
            other = f'SalesOrder:portal.fm_order_entry:{{"id":{native + 10}}}'
            handle = obj(packet, "public.assembly_order", native)
            obj(other, relation, native + 10)
            conn.execute("INSERT INTO object_values VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (packet, "produced_quantity", "quantity", "1920", "each", "produced",
                          None, None, None, handle, "row.produced_quantity", "%064x" % native,
                          "fm_bronze", "sharepoint.ao_packets", "decimal_text", None, "[]",
                          at, None))
            for order in (so, other):
                conn.execute(
                    "INSERT INTO links(link_id, link_type, from_object, to_object, tier, "
                    "binding_kind, reason, why_not, evidence_handles, markings, recorded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"lnk-{native}-{order}", "AssemblyOrderPacket.for_sales_order", packet,
                     order, "corroborated", "mention",
                     json.dumps([{"feature": "identifier_mention", "agrees": True}]), "[]",
                     json.dumps([handle]), "[]", at))
            packets.append(packet)
            others.append(other)
        conn.commit()
        return packets, so, others

    def test_an_employee_allocates_a_shared_packet_through_plx_review(self):
        """ER-11 at the console: a packet reaching two orders is offered for allocation
        (plx.aggregate's allocation_review_required, with its own value and unit); a share
        over that value is refused (allocation_conflict); a share that fits is the very
        record plx.review allocate records; a link the reader may not see is a withheld
        group, never named and never offered."""
        self.state = Path(tempfile.mkdtemp(dir=self.tmp)) / "state"
        case_fixtures.seed(self.state, "plx")
        self.store = self.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(self.state, self.store)
        (shared, hidden), so, (so2, withheld) = self.add_packets()
        ticket = self.ids.Fixture.ticket()
        call = self.console(live=True, ticket=ticket)
        status, view = call("GET", "/api/object?id=" + quote(shared, safe=""))
        self.assertEqual(status, 200, view)
        [item] = view["allocations"]["items"]
        self.assertEqual((item["link_type"], item["property"], item["state"], item["reached"],
                          [(v["value"], v["unit"]) for v in item["values"]]),
                         ("AssemblyOrderPacket.for_sales_order", "produced_quantity",
                          "allocation_review_required", [so, so2], [("1920", "each")]))
        cited = {key: item["values"][0]["citation"][key]
                 for key in ("l1_handle", "digest", "field_path")}
        decision = {"decision": "allocate", "subject": shared, "target": so,
                    "link_type": item["link_type"], "evidence": [cited],
                    "allocation": {"property": "produced_quantity", "quantity": "1000",
                                   "unit": "each"}}
        status, refused = call("POST", "/api/object-review", dict(decision, allocation=dict(
            decision["allocation"], quantity="1921")))
        self.assertEqual((status, refused["error"]), (409, "allocation_conflict"))
        status, recorded = call("POST", "/api/object-review", decision)
        self.assertEqual(status, 200, recorded)
        self.assertEqual((recorded["review"]["review"]["state"], recorded["review"]["created"],
                          recorded["business_system_writes"]), ("effective", True, 0))
        # plx.review allocate itself, called through the case host in the same session,
        # answers exactly that record: the store holds one allocation, recorded once.
        resolver = EmployeePermissionResolver(EmployeePermissionPolicy.from_file(self.policy),
                                              PortalAccessSnapshot(self.portal))
        runtime = CaseRuntime(self.state, client=None, tenant="plx",
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                              employee_permissions=resolver, require_employee=True)
        self.addCleanup(runtime.close)
        session, _ = case_console.TicketSessions(
            resolver, self.ids.SECRET, lambda s: None).authenticate(ticket)
        access = objects.host_access(runtime, self.store, session.actor, session=session,
                                     reviews=ReviewStore(self.state / REVIEW_STORE_FILE))
        self.addCleanup(access.close)
        with access.request():
            direct = access.tools()["plx.review"]["handler"](
                {"mode": "record", **decision}, session.actor)
        self.assertEqual((direct["created"], direct["review"]),
                         (False, recorded["review"]["review"]))
        self.assertEqual([r["decision"] for r in ReviewStore(
            self.state / REVIEW_STORE_FILE).history("plx", shared)], ["allocate"])
        # The packet whose second order this reader may not see: withheld, not offered.
        status, view = call("GET", "/api/object?id=" + quote(hidden, safe=""))
        self.assertEqual(status, 200, view)
        self.assertEqual([(i["state"], i["reached"]) for i in view["allocations"]["items"]],
                         [("group_link_withheld", [so])])
        self.assertNotIn(withheld, json.dumps(view))

    def test_a_follow_up_waits_for_another_persons_approval_of_the_exact_call(self):
        """The employee's follow-up is opened and recorded with them as its author, its
        MC call is prepared exactly and nothing is sent; a double submit answers the same
        case; the employee themself (by email or by an alias of their Entra oid) can never
        approve it."""
        call = self.console(live=True)
        po, view = self.open_po(call)
        body = {"object_id": po, "missing": "The supplier's confirmed ship date.",
                "why": "Production waits on the receipt.", "who_can_answer": ["purchasing"]}
        status, proposed = call("POST", "/api/object-follow-up", body)
        self.assertEqual(status, 200, proposed)
        proposal = proposed["proposal"]
        self.assertEqual((proposal["capability"], proposal["state"], proposal["executed"],
                          proposal["approved"], proposed["business_system_writes"]),
                         ("mc.task.create_follow_up", "call_approval_required", False, False,
                          0))
        request = proposal["prepared_request"]
        key = request["arguments"]["idempotencyKey"]
        description = request["arguments"]["description"]
        self.assertEqual(request, {"tool": "mc_create_task", "sent": False, "arguments": {
            "title": body["missing"], "bucket": FOLLOW_UP_BUCKET, "priority": "medium",
            "reporter": MC_OPERATOR, "idempotencyKey": key, "description": description}})
        # Why, then each citation, then the reference
        # that carries the key. This console knows no audience for its bucket, so no
        # source text goes.
        self.assertTrue(description.startswith(f"{body['why']}\n\nCited sources ("))
        self.assertTrue(description.endswith(
            f"\n\nPLX Workbench case {proposal['case_id']}, action {proposal['intent_id']}, "
            f"key {key}."))
        self.assertTrue(proposal["cited"])
        for index, handle in enumerate(proposal["cited"], 1):
            self.assertIn(f"\n\n[{index}] {handle}\n{g2.WITHHELD_UNRESOLVED}\n\n", description)
        self.assertIn(view["card"]["values"][0]["citation"]["l1_handle"], proposal["cited"])
        # A double submit opens nothing new.
        status, again = call("POST", "/api/object-follow-up", body)
        self.assertEqual(status, 200, again)
        self.assertEqual([again["proposal"][k] for k in ("case_id", "proposal_id", "intent_id",
                                                         "duplicate")],
                         [proposal["case_id"], proposal["proposal_id"], proposal["intent_id"],
                          True])
        runtime = CaseRuntime(self.state, client=None, tenant="plx",
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE)
        self.addCleanup(runtime.close)
        reader = Actor("plx", OPS)
        self.assertEqual(sum(1 for case in runtime.cases.list(reader, limit=200)
                             if case["question"] == body["missing"]), 1)
        # The author can never approve: by their email, or by another address of their oid.
        from operating.action_path import ActionPathError, CALL_APPROVAL_SCHEMA
        from operating.goal2_mc_approval import ApproverRoster
        path = ActionPath(self.state / case_fixtures.ACTIONS_FILE, runtime.cases,
                          call_approvers={self.ids.EMAIL_A: self.ids.OID_A,
                                          ALIAS_A: self.ids.OID_A},
                          mc_operator_email=MC_OPERATOR, approver_roster=ApproverRoster(),
                          mc_follow_up_bucket=FOLLOW_UP_BUCKET)
        state = path.get(reader, proposal["intent_id"])
        session = session_identity.fixture_session("plx", OPS,
                                                   channel=session_identity.CHANNEL_CONSOLE)
        now = datetime.now(timezone.utc)
        for approver in (self.ids.EMAIL_A, ALIAS_A):
            with self.subTest(approver=approver), self.assertRaises(ActionPathError) as caught:
                path.approve_call(reader, proposal["intent_id"], None, {
                    "schema": CALL_APPROVAL_SCHEMA, "tool": "mc_create_task",
                    "arguments": state["prepared"]["arguments"], "approved_by": approver,
                    "approved_at": (now - timedelta(minutes=1)).isoformat(),
                    "expires_at": (now + timedelta(hours=1)).isoformat(),
                    "evidence_ref": "test: the author approving their own follow-up"},
                    session=session)
            self.assertEqual(caught.exception.code, "call_approver_is_subject")
        self.assertEqual(path.get(reader, proposal["intent_id"])["state"],
                         "call_approval_required")

    def test_a_follow_up_quotes_a_source_only_when_every_audience_employee_reads_it(self):
        """The bucket's audience is read through the console's own employee gate: with
        only the author (who reads the PO) the cited text goes; with an employee the
        Portal snapshot does not know, it is withheld with the reason."""
        members = [Actor("plx", frozenset({"employee:" + self.ids.OID_A}))]
        call = self.console(live=True, audience=lambda bucket: g2.McBucketAudience(
            bucket, tuple(members), "test: restricted MC project members"))
        po, _view = self.open_po(call)
        for missing, member, reason in (
                ("The supplier's confirmed ship date (audience A).", None, None),
                ("The supplier's confirmed ship date (audience A, B).",
                 Actor("plx", frozenset({"employee:" + self.ids.OID_B})),
                 g2.WITHHELD_NOT_READABLE)):
            if member is not None:
                members.append(member)
            status, proposed = call("POST", "/api/object-follow-up", {
                "object_id": po, "missing": missing, "why": "Production waits.",
                "who_can_answer": ["purchasing"]})
            self.assertEqual(status, 200, proposed)
            proposal = proposed["proposal"]
            description = proposal["prepared_request"]["arguments"]["description"]
            self.assertTrue(proposal["cited"])
            for index, handle in enumerate(proposal["cited"], 1):
                line = f"\n\n[{index}] {handle}\n"
                self.assertIn(line, description)
                shown = description.split(line, 1)[1].split("\n\n", 1)[0]
                if reason is None:   # the row's fields, as the author reads them
                    self.assertIn("\nstatus: ", shown)
                    self.assertNotIn("text withheld", shown)
                else:
                    self.assertEqual(shown, reason)

    def test_an_employee_records_a_question_familys_follow_ups_on_a_case_as_themself(self):
        """Record on case on the live console: the employee's ticket session is the recorded
        subject of every follow-up, through the console's action path and the case host's
        tools; a second press records nothing twice; nothing is proposed."""
        call = self.console(live=True)
        po, _view = self.open_po(call)
        body = {"family": "landed_cost", "root": po}
        status, first = call("POST", "/api/investigate-record", body)
        self.assertEqual(status, 200, first)
        self.assertTrue(first["recorded"])
        self.assertEqual({d["outcome"] for d in first["recorded"]}, {"recorded"})
        self.assertEqual(len(first["case"]["follow_ups"]), len(first["recorded"]))
        runtime = CaseRuntime(self.state, client=None, tenant="plx",
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                              employee_permissions=EmployeePermissionResolver(
                                  EmployeePermissionPolicy.from_file(self.policy),
                                  PortalAccessSnapshot(self.portal)), require_employee=True)
        self.addCleanup(runtime.close)
        path = ActionPath(self.state / case_fixtures.ACTIONS_FILE, runtime.cases)
        employee = Actor("plx", frozenset({"employee:" + self.ids.OID_A}))
        attempts = path.internal_actions(employee, first["case_id"])
        self.assertEqual(len(attempts), len(first["recorded"]))
        for attempt in attempts:
            self.assertEqual((attempt["sub"]["kind"], attempt["sub"]["principal"],
                              attempt["act"][0]["channel"]),
                             ("employee", "employee:" + self.ids.OID_A,
                              session_identity.CHANNEL_CONSOLE))
        self.assertEqual(runtime.cases.get(employee, first["case_id"])["proposals"], [])
        status, again = call("POST", "/api/investigate-record", body)
        self.assertEqual((status, again["case_id"], {d["outcome"] for d in again["recorded"]}),
                         (200, first["case_id"], {"exists"}))

    def test_a_fixture_follow_up_has_no_subject_and_a_fixture_person_reviews_with_the_grant(self):
        call = self.console(live=False)
        po, view = self.open_po(call)
        status, proposed = call("POST", "/api/object-follow-up", {
            "object_id": po, "missing": "The supplier's confirmed ship date for PO5447.",
            "why": "Production waits on the receipt.", "who_can_answer": ["purchasing"]})
        self.assertEqual(status, 200, proposed)
        proposal = proposed["proposal"]
        self.assertEqual((proposal["capability"], proposal["executed"], proposal["approved"],
                          proposed["business_system_writes"]),
                         ("mc.task.create_follow_up", False, False, 0))
        self.assertIn(view["card"]["values"][0]["citation"]["l1_handle"], proposal["cited"])
        runtime = CaseRuntime(self.state, client=None, tenant="plx",
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE)
        self.addCleanup(runtime.close)
        actor = Actor("plx", OPS)
        case = runtime.cases.get(actor, proposal["case_id"])
        self.assertEqual([(p["id"], p["status"]) for p in case["proposals"]],
                         [(proposal["proposal_id"], PROPOSAL_STATUS)])
        # A fixture session names no employee: the intent is registered but never gets a
        # call to approve (no approval of it could count).
        self.assertEqual((proposal["state"], proposal["not_prepared"],
                          proposal["prepared_request"]),
                         ("proposed", "subject_unknown", None))
        # Proposed from inside the open case citing the PO, it attaches to that case; a
        # repeat answers the same proposal, and a case the reader cannot see is not found.
        inside = next(c for c in view["cases"]["cases"] if c["status"] == "open")
        body = {"object_id": po, "case_id": inside["case_id"],
                "missing": "Who signed the PO5447 receipt.", "why": "Quality asks.",
                "who_can_answer": ["operations"]}
        status, attached = call("POST", "/api/object-follow-up", body)
        self.assertEqual(status, 200, attached)
        self.assertEqual((attached["proposal"]["case_id"], attached["proposal"]["duplicate"]),
                         (inside["case_id"], False))
        again = call("POST", "/api/object-follow-up", body)[1]["proposal"]
        self.assertEqual((again["proposal_id"], again["duplicate"]),
                         (attached["proposal"]["proposal_id"], True))
        on_case = runtime.cases.get(actor, inside["case_id"])
        self.assertIn(attached["proposal"]["proposal_id"], [p["id"] for p in on_case["proposals"]])
        self.assertEqual(call("POST", "/api/object-follow-up",
                              dict(body, case_id="case_" + "0" * 24))[0], 404)
        # The fixture console is a person (author human), not an agent session: without the
        # identity-review grant the decision is refused for the grant, nothing written.
        edge = next(e for hop in view["around"]["hops"] for e in hop["result"]["edges"])
        shown = view["card"]["values"][0]["citation"]
        decision = {
            "decision": "reject_link", "subject": edge["from"], "target": edge["to"],
            "link_type": edge["link_type"],
            "evidence": [{"l1_handle": shown["l1_handle"], "digest": shown["digest"]}]}
        status, refused = call("POST", "/api/object-review", decision)
        self.assertEqual((status, refused["error"]), (409, "identity_review_required"))
        self.assertEqual(self.open_po(call)[1]["reviews"]["reviews"], [])
        # With the grant the same person records it (a copy of the state: the other tests
        # read this world's reviews), authored as a fixture name, never an employee.
        copy = Path(tempfile.mkdtemp(dir=self.tmp)) / "state"
        shutil.copytree(self.state, copy)
        self.state, self.store = copy, copy / case_console.ONTOLOGY_FILE
        granted = self.console(live=False, principals=OPS | {"review:identity"})
        status, recorded = granted("POST", "/api/object-review", decision)
        self.assertEqual(status, 200, recorded)
        review = recorded["review"]["review"]
        self.assertEqual((review["decision"], review["state"], recorded["business_system_writes"]),
                         ("reject_link", "effective", 0))
        self.assertTrue(review["author"]["principal"].startswith("fixture:"), review)
        _, again = self.open_po(granted)
        self.assertEqual([(r["review_id"], r["decision"]) for r in again["reviews"]["reviews"]],
                         [(review["review_id"], "reject_link")])


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "forwarder quotes are read from PDFs: needs pypdf and reportlab")
class RecordOnCaseTest(unittest.TestCase):
    """LOG-07 at the console: an investigate answer recorded on a case through the one path
    (``ontology_tools_investigate.record_follow_ups``) over the session's action path, and the
    case's follow-ups and drafts listed per reader. The landed_cost forwarder-quote world
    (``tests.test_investigate_landed_cost.ForwarderQuoteTests``: SYNTHETIC quote PDFs captured
    from an injected SharePoint drive, linked to the purchase order they name, projected with
    Portal rows by the one projector; the quote files admit staff:quality by their own item
    permissions), served by fixture consoles; the case store resolves each citation for its
    reader through the same rows. Evidence: isolated-fixture."""

    RECORDER = Actor("plx", frozenset({"staff:quality", "staff:sales"}), "restricted")
    OUTSIDER = Actor("plx", frozenset({"staff:sales"}), "restricted")  # on the case only
    STRANGER = Actor("plx", frozenset({"staff:finance"}), "restricted")  # not on the case

    @classmethod
    def setUpClass(cls) -> None:
        landed.ForwarderQuoteTests.setUpClass.__func__(cls)
        cls.addClassCleanup(landed.ForwarderQuoteTests.tearDownClass.__func__, cls)

    def rows(self, handle, actor):
        return landed.ForwarderQuoteTests.rows(self, handle, actor)

    def setUp(self) -> None:
        self.actions, _, _ = landed._case_path(self, self.rows, self.RECORDER)

    def serve(self, actor):
        """A fixture console for ``actor``: the world's tools, recording through
        ``self.actions`` for its console session. Returns ``call(method, path, body)``: the
        console answers on this thread (the world's capture store is bound to it), the
        client asks from another."""
        session = session_identity.fixture_session(
            "plx", actor.principals, channel=session_identity.CHANNEL_CONSOLE,
            clearance=actor.clearance)
        tools = landed.ForwarderQuoteTests.base(self, session.actor)
        table: dict = {}
        ot.register(table, tools)
        tt.register(table, tt.TraverseTools(tools))
        oi.register(table, oi.InvestigateTools(table, rows=self.rows, scope=oi.read_scope(tools)))
        # Portal rows open (a resolver is configured); without a case host the console
        # grants no other family (ApiOnlyAccess), so a quote it does not open is not offered.
        view = objects.ObjectWorkspace(table, session.actor, sources=object(),
                                       follow_ups=objects.FollowUps(self.actions, session))
        # The case routes are not used here: the console only checks their reader.
        server = make_server(session=session, case_workspace=SimpleNamespace(
            actor=session.actor), object_workspace=view)
        server.timeout = 60
        self.addCleanup(server.server_close)

        def call(method, path, body=None):
            answer: Queue = Queue(maxsize=1)

            def client():
                try:
                    conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=120)
                    headers = {"Authorization": "Bearer " + server.operator_access_token}
                    if body is not None:
                        headers.update({"Content-Type": "application/json",
                                        "X-Operator-Token": call.csrf})
                    conn.request(method, path, None if body is None else json.dumps(body),
                                 headers)
                    response = conn.getresponse()
                    answer.put((response.status, json.loads(response.read())))
                    conn.close()
                except BaseException as error:  # surfaced to the test thread
                    answer.put(error)
            thread = threading.Thread(target=client, daemon=True)
            thread.start()
            server.handle_request()
            thread.join(timeout=120)
            value = answer.get(timeout=1)
            if isinstance(value, BaseException):
                raise value
            return value
        call.csrf = None
        call.csrf = call("GET", "/api/session")[1]["csrf_token"]
        call.session = session
        return call

    def test_an_answer_recorded_on_a_case_lists_its_follow_ups_and_drafts_per_reader(self):
        call = self.serve(self.RECORDER)
        status, found = call("GET", "/api/objects?q=PO100961")
        self.assertEqual(status, 200, found)
        po = next(c["object"]["object_id"] for c in found["find"]["candidates"]
                  if c["object"]["object_type"] == "PurchaseOrder")
        body = {"family": "landed_cost", "root": po}
        with mock.patch.object(oi, "record_follow_ups", wraps=oi.record_follow_ups) as spy:
            status, first = call("POST", "/api/investigate-record", body)
        self.assertEqual(status, 200, first)
        case_id = first["case_id"]
        # One call of the one path, with the console's action path, reader and session.
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[:3], (self.actions, call.session.actor, case_id))
        self.assertIs(spy.call_args.kwargs["session"], call.session)
        recorded = first["recorded"]
        ups = [d for d in recorded if d["action"] == oi.FOLLOW_UP_ACTION]
        drafts = [d for d in recorded if d["action"] == oi.DRAFT_ACTION]
        self.assertTrue(ups and drafts)
        self.assertEqual({(d["outcome"], d["read_back"]) for d in recorded},
                         {("recorded", "verified")})
        self.assertEqual((first["read_only"], first["business_system_writes"],
                          first["answer_pages"]["complete"]), (False, 0, True))
        self.assertGreater(first["answer_pages"]["read"], 1)  # every page of the answer
        # The case lists exactly what was recorded, as the recorder reads it now.
        listed = first["case"]
        self.assertEqual(call("GET", "/api/object-case?case_id=" + case_id), (200, {
            **{k: v for k, v in first.items() if k in ("read_only", "note", "evidence",
                                                       "business_system_writes", "actor",
                                                       "sources_openable", "store",
                                                       "withheld_sources")},
            "read_only": True, "note": objects.READ_ONLY_NOTE, "case": listed}))
        self.assertEqual([f["record_id"] for f in listed["follow_ups"]],
                         [d["record_id"] for d in ups])
        self.assertEqual([f["record_id"] for f in listed["drafts"]],
                         [d["record_id"] for d in drafts])
        for item in listed["follow_ups"]:
            self.assertTrue(item["missing"] and item["who_can_answer"] and item["state"], item)
        quote_requests = [d for d in listed["drafts"]
                          if d["statement"].startswith("Draft freight quote request to ")]
        self.assertTrue(quote_requests)
        self.assertTrue(all("(not sent)" in d["statement"] and d["status"] == "unknown"
                            for d in listed["drafts"]))
        # Each cites what the answer showed this reader: the quotes it read among them.
        quoted = [d for d in listed["drafts"]
                  if any(c["l1_handle"].startswith("sharepoint://") for c in d["citations"])]
        self.assertTrue(quoted)
        cited = [c for item in listed["drafts"] + listed["follow_ups"] for c in item["citations"]]
        self.assertEqual({c["status"] for c in cited}, {"resolved"})
        self.assertEqual({(c["l1_handle"].split("://")[0], c["openable"]) for c in cited},
                         {("api", True), ("sharepoint", False)})
        # Nothing is proposed, registered or sent: internal records only.
        self.assertEqual(self.actions.cases.get(self.RECORDER, case_id)["proposals"], [])
        self.assertEqual(self.actions.list_for_case(self.RECORDER, case_id)["intents"], [])
        attempts = self.actions.internal_actions(self.RECORDER, case_id)
        self.assertEqual(len(attempts), len(recorded))

        # A second press records nothing twice: the same case, every item exists.
        status, again = call("POST", "/api/investigate-record", body)
        self.assertEqual(status, 200, again)
        self.assertEqual(again["case_id"], case_id)
        self.assertEqual({d["outcome"] for d in again["recorded"]}, {"exists"})
        self.assertEqual(len(again["recorded"]), len(recorded))
        status, named = call("POST", "/api/investigate-record", {**body, "case_id": case_id})
        self.assertEqual((status, {d["outcome"] for d in named["recorded"]}), (200, {"exists"}))

        # A reader the case does not admit is refused before the plan runs or anything is
        # written; an unknown field or a missing case id is refused too.
        stranger = self.serve(self.STRANGER)
        with mock.patch.object(oi, "record_follow_ups", wraps=oi.record_follow_ups) as spy:
            for who, method, path, payload, code in (
                    (stranger, "POST", "/api/investigate-record", {**body, "case_id": case_id},
                     404),
                    (stranger, "GET", "/api/object-case?case_id=" + case_id, None, 404),
                    (call, "POST", "/api/investigate-record", {**body, "author": "x"}, 400),
                    (call, "GET", "/api/object-case", None, 400)):
                with self.subTest(path=path, payload=payload):
                    self.assertEqual(who(method, path, payload)[0], code)
            spy.assert_not_called()
        self.assertEqual(len(self.actions.internal_actions(self.RECORDER, case_id)),
                         len(attempts))

        # A reader on the case but outside the quotes' item permissions sees the drafts and
        # follow-ups citing a quote withheld (no text, no handle), the others as recorded.
        outside = self.serve(self.OUTSIDER)
        status, seen = outside("GET", "/api/object-case?case_id=" + case_id)
        self.assertEqual(status, 200, seen)
        self.assertNotIn("sharepoint://", json.dumps(seen))
        shown = {item["record_id"]: item for item in seen["case"]["drafts"]
                 + seen["case"]["follow_ups"]}
        self.assertEqual(set(shown), {item["record_id"] for item in listed["drafts"]
                                      + listed["follow_ups"]})
        for item in listed["drafts"] + listed["follow_ups"]:
            outsider = shown[item["record_id"]]
            if any(c["l1_handle"].startswith("sharepoint://") for c in item["citations"]):
                self.assertEqual((outsider.get("statement"), outsider.get("missing"),
                                  outsider.get("who_can_answer"), outsider["content"]),
                                 (None, None, None, "withheld_citation_unavailable"), outsider)
            else:
                self.assertEqual(outsider, item)
        self.assertTrue(any(shown[d["record_id"]].get("content") for d in quoted))
        # A withheld draft says whether it is superseded, as the case store records it.
        replaced = quoted[0]["record_id"]
        self.actions.cases.supersede(self.RECORDER, case_id, replaced, "test: a newer quote",
                                     author_kind=session_identity.AUTHOR_HUMAN)
        drafts = outside("GET", "/api/object-case?case_id=" + case_id)[1]["case"]["drafts"]
        self.assertEqual({d["record_id"]: d["superseded"] for d in drafts},
                         {d["record_id"]: d["record_id"] == replaced for d in listed["drafts"]})
        self.assertEqual(next(d for d in drafts if d["record_id"] == replaced)["content"],
                         "withheld_citation_unavailable")

        # Without a case id an answer that is not a plx.investigate answer opens no case.
        with mock.patch.object(objects.ObjectWorkspace, "_whole_answer",
                               lambda view, family, root, first: (
                                   dict(first, procedure="not a procedure"),
                                   {"read": 1, "complete": True})), \
                mock.patch.object(self.actions, "open_case",
                                  wraps=self.actions.open_case) as opened:
            status, refused = outside("POST", "/api/investigate-record", body)
        self.assertEqual((status, refused.get("detail")), (400, "not_an_investigate_answer"))
        opened.assert_not_called()

        # A case another session claims is refused, named or answered by a repeat, before
        # the recorder runs or any attempt is audited.
        holder = {"session": hashlib.sha256(b"another session").hexdigest(),
                  "subject": "fixture:another-session"}
        self.actions.cases.claim(self.RECORDER, case_id, holder, 600)
        with mock.patch.object(oi, "record_follow_ups", wraps=oi.record_follow_ups) as spy:
            for payload in ({**body, "case_id": case_id}, body):
                with self.subTest(payload=payload):
                    status, refused = call("POST", "/api/investigate-record", payload)
                    self.assertEqual((status, refused.get("detail")), (403, "case_claimed"))
            spy.assert_not_called()
        self.assertEqual(len(self.actions.internal_actions(self.RECORDER, case_id)),
                         len(attempts))
        self.actions.cases.claim(self.RECORDER, case_id, holder, 0)   # released

        # A closed case takes no more records when named; an unnamed press opens a fresh case.
        self.actions.cases.close(self.RECORDER, case_id, "test: the answer was followed up")
        with mock.patch.object(oi, "record_follow_ups", wraps=oi.record_follow_ups) as spy:
            status, refused = call("POST", "/api/investigate-record", {**body, "case_id": case_id})
            self.assertEqual((status, refused.get("detail")), (403, "case_closed"))
            spy.assert_not_called()
        # Read to its first page only, the answer is typed partial: page 1 holds no quote, so
        # a gap no read entry matches, and a draft from it, is not recorded (no citation would
        # gate its text; never the purchase order's in its place) and is named as not recorded.
        with mock.patch.object(objects, "MAX_RECORD_PAGES", 1):
            status, fresh = call("POST", "/api/investigate-record", body)
        self.assertEqual(status, 200, fresh)
        self.assertNotEqual(fresh["case_id"], case_id)
        self.assertEqual(self.actions.cases.get(self.RECORDER, fresh["case_id"])["status"],
                         "open")
        self.assertEqual(fresh["answer_pages"],
                         {"read": 1, "complete": False, "partial": "answer_pages_unread"})
        skipped = [d for d in fresh["recorded"] if d["outcome"] == "not_recorded"]
        self.assertEqual({(d["action"], d["partial"], "record_id" in d) for d in skipped},
                         {(oi.FOLLOW_UP_ACTION, "answer_pages_unread", False),
                          (oi.DRAFT_ACTION, "answer_pages_unread", False)})
        self.assertEqual({d["outcome"] for d in fresh["recorded"]} - {"not_recorded"},
                         {"recorded"})
        stored = fresh["case"]["follow_ups"] + fresh["case"]["drafts"]
        self.assertEqual(len(stored), len(fresh["recorded"]) - len(skipped))
        self.assertTrue(all(item["citations"] for item in stored), stored)
        # A case reader without the quotes reads none of the text the quotes gate.
        quoted_text = {item.get("missing") or item["statement"]
                       for item in listed["drafts"] + listed["follow_ups"]
                       if any(c["l1_handle"].startswith("sharepoint://")
                              for c in item["citations"])}
        self.assertTrue(quoted_text)
        status, seen = outside("GET", "/api/object-case?case_id=" + fresh["case_id"])
        self.assertEqual(status, 200, seen)
        shown_text = {value for item in seen["case"]["follow_ups"] + seen["case"]["drafts"]
                      for value in item.values() if isinstance(value, str)}
        self.assertEqual(quoted_text & shown_text, set())


class GrantSnapshotTest(unittest.TestCase):
    """Every tool call of one answer runs inside one grant request; a missing mirror
    fails closed."""

    def test_workspace_calls_run_inside_one_grant_request(self):
        entered = []

        class Grants(objects.ApiOnlyAccess):
            def request(self):
                from contextlib import contextmanager

                @contextmanager
                def scope():
                    entered.append("in")
                    yield
                    entered.append("out")
                return scope()

        def handler(arguments, actor):
            self.assertEqual(entered[-1], "in")
            return {"status": "not_found", "candidates": [], "gaps": []}
        table = {name: {"handler": handler} for name in objects.TOOLS}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"p"})), access=Grants())
        view.find("anything")
        self.assertEqual(entered, ["in", "out"])

    def test_an_unreadable_mirror_fails_closed(self):
        tmp = Path(tempfile.mkdtemp(prefix="object-workspace-unknown-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        state = tmp / "state"
        case_fixtures.seed(state, "plx")
        store = state / case_console.ONTOLOGY_FILE
        objects.project_mirror(state, store)
        status = objects.StoreStatus(store, tmp / "no-such-state")
        self.assertEqual(status.read()["state"], "unknown")
        table = {name: {"handler": lambda arguments, actor: self.fail("tool called")}
                 for name in objects.TOOLS}
        view = objects.ObjectWorkspace(table, Actor("plx", OPS), store_status=status)
        for call in (lambda: view.find("x"), lambda: view.workspace("A:b.c:1")):
            with self.assertRaises(PermissionError):
                call()


class StoreProvenanceTest(unittest.TestCase):
    """A store is a state directory's only when
    it records a Portal mirror or BC capture generation that is that source's capture
    there now; a source the store did not project is labelled not_projected, never
    refused (a store built during a Portal outage). The status is per source, so only a projected
    source of another generation refuses, naming that source and no other."""

    @classmethod
    def setUpClass(cls) -> None:
        from integrations.capture_mail import MailCaptureStore
        from integrations.capture_store import CaptureStore
        from integrations.source_mirror import AUDIENCE_SCHEMA, SourceMirror
        from ontology import project
        from ontology.project_bc import BcCaptures
        from ontology.schema import load_schema

        cls.tmp = Path(tempfile.mkdtemp(prefix="store-provenance-"))
        audience = cls.tmp / "mirror-audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {}}), "utf-8")
        bc_audience = SimpleNamespace(tenant="plx", allows=lambda *a, **k: True)

        def state(name, *, bc=True, mail=False):
            """A host state directory with a fresh empty mirror and, as asked, an empty
            BC capture (its own random generation) and mail capture."""
            where = cls.tmp / name
            objects.mirror_path(where).parent.mkdir(parents=True)
            SourceMirror(objects.mirror_path(where), None, audience, tenant="plx").close()
            if bc:
                (where / BC_DIR).mkdir()
                CaptureStore(where / BC_DIR / BC_FILE, bc_audience).close()
            if mail:
                MailCaptureStore(where).db.close()
            return where
        cls.state, cls.other, cls.no_bc = (state("state", mail=True), state("other"),
                                           state("no-bc", bc=False))
        schema = load_schema()
        cls.bc_only, cls.mail_only = cls.tmp / "bc-only.sqlite", cls.tmp / "mail-only.sqlite"
        cls.bc_mail = cls.tmp / "bc-mail.sqlite"
        with BcCaptures(cls.state / BC_DIR / BC_FILE) as bc, \
                project.MailCaptures(cls.state) as mail:
            for target, sources in ((cls.bc_only, {"bc": bc}),
                                    (cls.mail_only, {"graph_mail": mail}),
                                    (cls.bc_mail, {"bc": bc, "graph_mail": mail})):
                project.build_store(target, schema, sources)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def refused(self, store, state):
        """The store state ``checked_store_status`` refuses ``store`` with in ``state``."""
        with self.assertRaises(ValueError) as refusal:
            case_console.checked_store_status(store, state)
        return re.match(r"ontology store refused \((\w+)\)", str(refusal.exception)).group(1)

    def with_generations(self, generations):
        """A copy of the BC-only store whose ``l1_generations`` is ``generations``."""
        copy = self.tmp / f"altered-{len(list(self.tmp.glob('altered-*')))}.sqlite"
        shutil.copyfile(self.bc_only, copy)
        with sqlite3.connect(copy) as conn:
            conn.execute("UPDATE store_meta SET value = ? WHERE key = 'l1_generations'",
                         (json.dumps(generations),))
        conn.close()
        return copy

    def test_a_bc_only_store_is_served_with_the_portal_not_projected(self):
        status = case_console.checked_store_status(self.bc_only, self.state)
        table = {name: {"handler": lambda arguments, actor: {
            "status": "not_found", "candidates": [], "gaps": []}} for name in objects.TOOLS}
        view = objects.ObjectWorkspace(table, Actor("plx", OPS), store_status=status)
        store = view.find("anything")["store"]
        self.assertEqual((store["state"], store["projected"], store["not_projected"]),
                         ("current", ["bc"], ["graph_mail", "portal_api"]), store)
        # A capture the store did not project is labelled from its file, never opened.
        self.assertEqual((store["mirror_generation"], store["mirror_generation_now"]),
                         (None, None))
        self.assertEqual(store["sources"]["portal_api"],
                         {"state": "not_projected", "label": "Portal"})
        # The mail capture it did not project is the one a BC and mail store matches.
        self.assertEqual(objects.StoreStatus(self.bc_mail, self.state).read()["state"],
                         "current")

    def test_another_bc_capture_is_foreign_and_none_is_unknown_both_refused(self):
        self.assertEqual(self.refused(self.bc_only, self.other), "foreign")
        self.assertEqual(self.refused(self.bc_only, self.no_bc), "unknown")
        # Beside the same BC capture, a recorded mail capture missing here is unknown.
        alone = self.tmp / "bc-alone"
        for part in (objects.mirror_path(self.state).parent.name, BC_DIR):
            shutil.copytree(self.state / part, alone / part)
        self.assertEqual(case_console.checked_store_status(self.bc_only, alone).read()["state"],
                         "current")

    def state_copy(self, name, *, source=None, teams=None):
        """A copy of the mirror and BC capture of ``source`` (default the BC and mail
        state) without its mail capture, with a Teams capture file holding ``teams``."""
        where = self.tmp / name
        for part in (objects.mirror_path(self.state).parent.name, BC_DIR):
            shutil.copytree((source or self.state) / part, where / part)
        if teams is not None:
            from integrations import capture_teams
            (where / capture_teams.STORE_DIR).mkdir()
            (where / capture_teams.STORE_DIR / capture_teams.DB_FILE).write_bytes(teams)
        return where

    def answer(self, status):
        """``plx.find``'s ``store`` as the workspace serves it over ``status``."""
        table = {name: {"handler": lambda arguments, actor: {
            "status": "not_found", "candidates": [], "gaps": []}} for name in objects.TOOLS}
        view = objects.ObjectWorkspace(table, Actor("plx", OPS), store_status=status)
        return view.find("anything")["store"]

    @staticmethod
    def described_again(status):
        """``status`` once a capture written after it was last described has been
        described again (in the background, ``CAPTURE_STATUS_SECONDS`` after the last)."""
        with mock.patch.object(objects, "CAPTURE_STATUS_SECONDS", 0.0):
            status.read()
            deadline = time.monotonic() + 30
            while status._refresh_running and time.monotonic() < deadline:
                time.sleep(0.02)
        return status

    def test_an_unprojected_unreadable_capture_never_refuses_or_is_blamed_on_bc(self):
        garbage = self.state_copy("garbage-teams", teams=b"not a capture store " * 64)
        store = self.answer(case_console.checked_store_status(self.bc_only, garbage))
        self.assertEqual((store["state"], store["not_projected"]),
                         ("current", ["portal_api", "teams"]), store)
        self.assertEqual(store["sources"]["teams"], {"state": "not_projected",
                                                     "label": "Teams"})
        self.assertEqual(store["sources"]["bc"]["state"], "current")

    def test_a_removed_capture_is_labelled_and_another_generation_names_only_its_source(self):
        # The mail capture the store projected was deleted: mail is removed, the store
        # still answers, labelled stale.
        alone = self.state_copy("mail-removed")
        store = self.answer(case_console.checked_store_status(self.bc_mail, alone))
        self.assertEqual((store["state"], store["removed"], store["sources"]["graph_mail"]),
                         ("stale", ["graph_mail"], {"state": "removed", "label": "mail"}),
                         store)
        self.assertIn("mail capture it was projected from is gone", store["detail"])
        # Another BC generation (beside a removed mail capture and an unreadable Teams
        # file the store never projected) refuses, naming bc and nothing else.
        foreign = self.state_copy("foreign-bc", source=self.other, teams=b"garbage")
        with self.assertRaises(PermissionError) as refusal:
            self.answer(objects.StoreStatus(self.bc_mail, foreign))
        self.assertEqual(str(refusal.exception),
                         "ontology_store_from_another_capture_generation:bc")
        self.assertIn("another bc capture generation", objects.StoreStatus(
            self.bc_mail, foreign).read()["detail"])

    def test_one_store_generation_over_unchanged_captures_opens_them_once(self):
        opened, plain = [], objects.StoreStatus(self.bc_mail, self.state)
        status = objects.StoreStatus(self.bc_mail, self.state, opener=lambda source: (
            opened.append(source) or plain.open_capture(source)))
        first = self.answer(status)
        self.assertEqual((first["state"], sorted(opened)), ("current", ["bc", "graph_mail"]))
        self.assertEqual(self.answer(status), first)
        self.assertEqual(sorted(opened), ["bc", "graph_mail"])   # no capture opened again
        # A capture file that changes is read again on the next answer.
        bc_file = self.state / BC_DIR / BC_FILE
        stat = bc_file.stat()
        os.utime(bc_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        # A capture still being written changes every few seconds: a request is answered
        # from the kept status at once, never by describing the captures again ...
        self.assertEqual(self.answer(status)["state"], "current")
        self.assertEqual(sorted(opened), ["bc", "graph_mail"])
        # ... which happens in the background once CAPTURE_STATUS_SECONDS have passed
        with mock.patch.object(objects, "CAPTURE_STATUS_SECONDS", 0.0):
            self.assertEqual(self.answer(status)["state"], "current")
            deadline = time.monotonic() + 30
            while (status._refresh_running or len(opened) < 4) and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual(sorted(opened), ["bc", "bc", "graph_mail", "graph_mail"])

    def test_a_bc_capture_newer_than_the_store_reads_stale(self):
        from integrations.tests import test_bc_capture as tb
        from ontology import project
        from ontology.project_bc import BcCaptures
        from ontology.schema import load_schema

        where = self.tmp / "bc-newer"
        (where / BC_DIR).mkdir(parents=True)
        fake = tb.FakeBc({"purchaseOrders": 1})
        capture = tb.Harness(self, fake, db=where / BC_DIR / BC_FILE)
        capture.run(entity_sets=("purchaseOrders",))
        store = self.tmp / "bc-newer.sqlite"
        with BcCaptures(where / BC_DIR / BC_FILE) as bc:
            project.build_store(store, load_schema(), {"bc": bc})
        status = case_console.checked_store_status(store, where)
        self.assertEqual(self.answer(status)["state"], "current")
        fake.data[(tb.CO[0], "purchaseOrders")][0].update(
            {"@odata.etag": 'W/"changed"', "number": "N-CHANGED"})
        capture.run(entity_sets=("purchaseOrders",))
        self.assertIn("bc/purchaseOrders", self.described_again(status).read()["newer_relations"])
        # A reader BC is withheld from (no host grant) sees BC stale, not which of its
        # relations changed.
        stale = self.answer(status)
        self.assertEqual(stale["state"], "stale", stale)
        self.assertEqual(stale["sources"]["bc"], {
            "state": "stale", "generation": status.read()["sources"]["bc"]["generation"],
            "label": "Business Central"})
        self.assertEqual(stale["newer_relations"], [])
        self.assertIn("Business Central capture is newer than this store", stale["detail"])

    def test_teams_and_mc_relations_newer_than_the_store_show_only_to_their_grantees(self):
        # Gap gap-d2a0aa06aafc: every grantable capture family is withheld from a reader
        # the host does not grant it, not only BC, mail and SharePoint.
        from integrations import capture_teams, mc_sources
        from operating.ontology_tools import ReadPolicy

        where = self.state_copy("teams-mc-stale", teams=b"")
        mc_sources.store_path(where).parent.mkdir(parents=True)
        mc_sources.store_path(where).write_bytes(b"")
        self.assertTrue((where / capture_teams.STORE_DIR / capture_teams.DB_FILE).is_file())
        newer = {"teams": "channelMessages", "mission_control": "tasks"}
        with sqlite3.connect(self.bc_only) as conn:
            meta = dict(conn.execute("SELECT key, value FROM store_meta"))
        conn.close()
        generations = {**json.loads(meta["l1_generations"]),
                       **{source: {"generation": source + "-1"} for source in newer}}
        freshness = {**json.loads(meta.get("source_freshness") or "{}"),
                     **{source: {relation: {"last_capture_id": 1}}
                        for source, relation in newer.items()}}
        meta.update(l1_generations=json.dumps(generations),
                    source_freshness=json.dumps(freshness))
        plain = objects.StoreStatus(self.bc_only, where)

        @contextlib.contextmanager
        def opener(source):
            if source not in newer:
                with plain.open_capture(source) as capture:
                    yield capture
                return
            yield SimpleNamespace(describe=lambda: {
                "generation": source + "-1",
                "relations": {newer[source]: {"last_capture_id": 2}}})
        status = objects.StoreStatus(self.bc_only, where, opener=opener)
        base = {"api:*": OPS, "bc:*": OPS}
        table = {name: {"handler": lambda arguments, actor: {
            "status": "not_found", "candidates": [], "gaps": []}} for name in objects.TOOLS}

        def answer(audiences):
            """``plx.find``'s answer to an OPS employee over the case-host access whose
            policy grants ``audiences``."""
            host = SimpleNamespace(
                policy_now=lambda reader: ReadPolicy("plx", audiences),
                request=contextlib.nullcontext, store_meta=lambda: dict(meta))
            access = objects.HostAccess(host, SimpleNamespace(employee_gate=lambda a: a),
                                        Actor("plx", OPS))
            return objects.ObjectWorkspace(table, Actor("plx", OPS), store_status=status,
                                           access=access).find("anything")

        ungranted = answer(base)
        store = ungranted["store"]
        self.assertEqual(store["state"], "stale", store)
        for source, label in (("teams", "Teams"), ("mission_control", "Mission Control")):
            self.assertEqual(store["sources"][source], {
                "state": "stale", "generation": source + "-1", "label": label})
        self.assertEqual(store["newer_relations"], [])
        self.assertNotIn("channelMessages", json.dumps(ungranted))
        self.assertNotIn("tasks", json.dumps(store))
        self.assertIn("Mission Control, Teams capture is newer", store["detail"])
        self.assertTrue({"teams", "mission_control"} <= {
            item["source"] for item in ungranted["withheld_sources"]})

        granted = answer({**base, "teams:*": OPS, "mc:*": OPS})
        self.assertEqual(granted["store"]["newer_relations"],
                         ["mission_control/tasks", "teams/channelMessages"])
        self.assertEqual(granted["store"]["sources"]["teams"]["newer_relations"],
                         ["channelMessages"])
        self.assertFalse({"teams", "mission_control"} & {
            item["source"] for item in granted["withheld_sources"]})

        class ApiOnly(objects.ApiOnlyAccess):
            def store_meta(self):
                return dict(meta)
        # An API-only reader (no case host) is granted the Portal alone: its object view
        # withholds every other grantable family, Teams and MC included, for want of a
        # host grant, and names none of their relations.
        api_only = objects.ObjectWorkspace(
            table, Actor("plx", OPS), store_status=status,
            access=ApiOnly()).workspace('Header:portal.sample_header:{"id":7}')
        self.assertEqual(api_only["status"], "read")
        withheld = [item["source"] for item in api_only["withheld_sources"]]
        self.assertEqual(withheld, [source for source, _ in objects.grantable_sources()
                                    if source not in objects.GRANTED_FAMILIES])
        self.assertTrue({"bc", "graph_mail", "sharepoint", "teams", "mission_control"}
                        <= set(withheld))
        self.assertTrue(all(item["reason"] == objects.NO_HOST_GRANT
                            for item in api_only["withheld_sources"]))
        self.assertEqual(api_only["store"]["newer_relations"], [])
        for source, label in (("teams", "Teams"), ("mission_control", "Mission Control")):
            self.assertEqual(api_only["store"]["sources"][source], {
                "state": "stale", "generation": source + "-1", "label": label})
        self.assertNotIn("channelMessages", json.dumps(api_only))
        self.assertNotIn("tasks", json.dumps(api_only["store"]))
        # A family no host grant names is never shown its relation names (fails closed).
        unknown = objects._store_for({"newer_relations": ["future/x"], "sources": {
            "future": {"state": "stale", "newer_relations": ["x"]}}}, set())
        self.assertEqual(unknown, {"newer_relations": [], "sources": {
            "future": {"state": "stale"}}})

    def meta(self, store):
        with sqlite3.connect(store) as conn:
            value = conn.execute("SELECT value FROM store_meta WHERE key = "
                                 "'l1_generations'").fetchone()[0]
        conn.close()
        return value

    def test_a_store_with_no_portal_or_bc_generation_is_refused(self):
        # Mail's generation is its capture schema version, the same in every copy: the
        # mail-only store matches this state's mail capture and is still not its own.
        self.assertEqual(json.loads(self.meta(self.mail_only)),
                         {"graph_mail": json.loads(self.meta(self.bc_mail))["graph_mail"]})
        self.assertEqual(self.refused(self.mail_only, self.state), "unknown")
        for generations in ({}, {"portal_api": {"generation": None}},
                            {"bc": "bc-capture/0"}, ["bc"]):
            with self.subTest(generations=generations):
                self.assertEqual(self.refused(self.with_generations(generations),
                                              self.state), "unknown")


class HostAccessRequestTests(unittest.TestCase):
    def test_one_grant_per_answer_and_a_revocation_applies_to_the_next(self):
        from operating.ontology_tools import ReadPolicy

        decided, audiences = [], {"api:*": OPS}

        def policy_now(reader):
            decided.append(reader)
            return ReadPolicy("plx", dict(audiences))
        host = SimpleNamespace(policy_now=policy_now, request=contextlib.nullcontext)
        access = objects.HostAccess(host, SimpleNamespace(employee_gate=lambda a: a),
                                    Actor("plx", OPS))
        with access.request():
            may_read, _ = access.grant()
            with access.request():   # a nested part of the same answer (a page section)
                with access.request():
                    self.assertTrue(access.grant()[0]("api:public.app_purchase_order"))
            # the outer answer's grant stands after its nested parts
            self.assertTrue(access.grant()[0]("api:public.app_purchase_order"))
            self.assertTrue(may_read("api:public.app_purchase_order"))
        self.assertEqual(len(decided), 1)
        audiences.clear()   # revoked between two answers
        with access.request():
            self.assertFalse(access.grant()[0]("api:public.app_purchase_order"))
        self.assertEqual(len(decided), 2)

    def test_a_policy_with_a_transient_answer_is_never_kept(self):
        """A policy decided with a transient answer in it (a BC listing that failed, a
        mailbox check that could not decide: ``policy_settled`` false), or whose inputs
        changed while it was decided, or from inputs that cannot be named, is decided
        again on the next request."""
        from operating.ontology_tools import ReadPolicy

        decided, state = [], {"settled": False, "inputs": ((1,), ()), "moving": False}

        def inputs(reader):
            if state["moving"]:
                state["inputs"] = ((state["inputs"][0][0] + 1,), ())
            return state["inputs"]
        host = SimpleNamespace(
            policy_now=lambda reader: decided.append(reader) or ReadPolicy("plx", {}),
            request=contextlib.nullcontext, policy_inputs=inputs,
            policy_settled=lambda reader: state["settled"])
        access = objects.HostAccess(host, SimpleNamespace(employee_gate=lambda a: a),
                                    Actor("plx", OPS))
        for _ in range(2):
            with access.request():
                pass
        self.assertEqual(len(decided), 2)   # unsettled: decided each time
        state.update(settled=True, moving=True)
        for _ in range(2):
            with access.request():
                pass
        self.assertEqual(len(decided), 4)   # inputs moved while decided: not kept
        state.update(moving=False, inputs=None)
        for _ in range(2):
            with access.request():
                pass
        self.assertEqual(len(decided), 6)   # no inputs named: decided each time
        state.update(inputs=((7,), ()))
        for _ in range(3):
            with access.request():
                pass
        self.assertEqual(len(decided), 7)   # settled and still: kept


if __name__ == "__main__":
    unittest.main()
