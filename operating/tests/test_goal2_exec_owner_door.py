"""An isolated owner with a door has one write path (9eb1ad9b4ab8;
decision 14).

``IsolatedOwnerSystem.process_pending`` refuses (``OwnerRejected``) a door operation, on
the owner with the door and on a reopened owner without one, whether the door's answer
was lost or never came, and any pending operation without the owner's own marker (one
logged before door operations were marked); a door operation whose answer was lost says
why it waits, in the owner's read-back and the Workbench action record; a door that
failed before its PATCH settles the key not applied and fenced, and tells the employee it
is safe to retry with a new intent; no door exception's message (a transport's names the
URL) reaches the owner's read-back, the action record or a logged traceback, and a code
is kept only while every part is in a code vocabulary (the Workbench's door codes, the
Portal's own codes generated from its source, BC's error codes, Python's built-in
exception types) and a lost answer's exception type only while it is Python's own
(built-in, http.client, urllib, socket, ssl): a dotted BC code is kept; a single-label
host, a person's or a customer's name, a PO number and another exception type are refused; a definite refusal from the Portal adapter is recorded as its code only,
when the Portal's vocabulary holds it, else as a fixed hint by HTTP status, never the
Portal's message; a fault the door cannot emulate, or an unknown one, is
refused (``OwnerRejected``, the key fenced, nothing sent), so the action path records a
certain rejection, never ``dispatched_unknown``. Run on the BC twin's own OData door and
the Portal twin. Evidence label: ``isolated-fixture``.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import traceback
from unittest import mock


from operating.goal2_exec_owner import (DISPATCH_SCHEMA, DOOR_CODE_REDACTED, DOOR_FAULTS,
                                        FAULTS, IsolatedOwnerSystem, NothingSent,
                                        OwnerRejected, door_code)
from operating.action_adapters import owner_code
from operating.tests.test_twin_bc import BcTwinTestCase
from twin.bc.lifecycle import TARGET_FIELD, bc_intent, targets
from twin import mode
from twin.bc.odata import VERSION_FIELD, ODataDoor
from twin.world import ACTOR, PORTAL_CAPABILITY, SESSION


class _Fails:
    """BC answering ``method`` with 503, or (``raises``) never answering it at all."""

    def __init__(self, transport, method, raises=False, message="reset") -> None:
        self.transport, self.method, self.raises = transport, method, raises
        self.message = message

    def send(self, request):
        if request.method == self.method:
            if self.raises:
                raise ConnectionError(self.message)
            return {"status": 503, "body": {}}
        return self.transport.send(request)


RETRY = "not sent, safe to retry with a new intent"
HOST = "bc-tenant.example.invalid"
URL = f"https://{HOST}:7048/v2.0/companies(1)/salesOrders?token=abc"
LABEL = "bctenant"   # a single-label host
DOTTED = "odata_400:Application_Microsoft.Dynamics.Nav.Types.NavCSideException"
# Free shapes a code's old shape rules let through: a host, a person, a customer, a PO
# number, a dotted host under a BC prefix, a made-up exception type.
FREE = ("bc_tenant", "jane_doe", "ACME_CORP", "PO_5501", "Internal_Bctenant.Example.Invalid",
        "AcmeCorpError")


class AcmeCorpError(Exception):
    """An exception type outside Python's own transport modules."""


class DoorOneWritePathTest(BcTwinTestCase):
    def payload(self, cited, value):
        return {"schema": DISPATCH_SCHEMA, "capability": "fixture.record.set_field",
                "relation": cited["relation"], "key": cited["key"],
                "set": {TARGET_FIELD: value},
                "expect": [{"check": "field_equals", "field": VERSION_FIELD,
                            "value": cited["row"][VERSION_FIELD]}]}

    def test_process_pending_refuses_a_door_operation_and_a_lost_answer_says_why(self):
        world = self.world()
        owner = world.bc.owner
        first, second = targets(world)[:2]
        cited = world.resolve(first, ACTOR)
        owner.door = door = ODataDoor(world.bc, _Fails(world.transport, "PATCH"))
        _, iid = bc_intent(world, first, "2026-11-22")   # through the action path
        world.authorize(iid, "TASK-TWIN-BC-DOOR")
        sent = world.path.dispatch(ACTOR, iid, world.bc_registry, world.mc_client,
                                   session=SESSION)
        self.assertEqual(sent["state"], "dispatched_unknown")
        self.assertEqual([answer[:2] for answer in door.answers], [("GET", 200), ("PATCH", 503)])
        lost = "door_answer_lost:RuntimeError"
        world.path.read_back(ACTOR, iid, world.bc_read_back, session=SESSION)
        [read] = world.path.view(ACTOR, iid)["read_backs"]   # the Workbench action record
        self.assertEqual((read["operation"]["status"], read["operation"]["reason"]),
                         ("pending", lost))
        key_a = world.path.get(ACTOR, iid)["intent"]["idempotency_key"]

        def crashes(payload):   # the process dies inside the door: no answer recorded
            raise SystemExit("killed")

        owner.door = crashes
        other = world.resolve(second, ACTOR)
        with self.assertRaises(SystemExit):
            owner.dispatch(self.payload(other, "2026-11-23"), "wb-" + "b" * 40)
        self.assertEqual(
            [(op["status"], op["reason"]) for op in owner.operations()],
            [("pending", lost), ("pending", "door_awaiting_answer")])
        reopened = IsolatedOwnerSystem(owner.path)   # the same owner state, no door
        for system in (owner, reopened):
            for key in (key_a, "wb-" + "b" * 40):
                for apply in (True, False):
                    with self.subTest(door=system.door is not None, key=key, apply=apply), \
                            self.assertRaises(OwnerRejected) as refused:
                        system.process_pending(key, apply=apply)
                    self.assertEqual(refused.exception.code, "door_operation_not_processed")
        self.assertEqual([op["status"] for op in owner.operations()], ["pending", "pending"])
        for cited_record, key, reason in ((cited, key_a, lost),   # nothing written
                                          (other, "wb-" + "b" * 40, "door_awaiting_answer")):
            seen = reopened.read_back({"idempotency_key": key,
                                       "relation": cited_record["relation"],
                                       "key": cited_record["key"]})
            self.assertEqual((seen["operation"], seen["fenced"], seen["reason"], seen["record"]),
                             ("pending", False, reason, cited_record["row"]))

    def test_a_door_that_fails_before_its_patch_settles_not_applied(self):
        world = self.world()
        owner = world.bc.owner
        for number, (transport, code) in enumerate((
                (_Fails(world.transport, "GET"), "door_nothing_sent:odata_answer_503:" + RETRY),
                (_Fails(world.transport, "GET", raises=True),
                 "door_nothing_sent:odata_read_failed:ConnectionError:" + RETRY))):
            target = targets(world)[number]
            cited = world.resolve(target, ACTOR)
            owner.door = door = ODataDoor(world.bc, transport)
            _, iid = bc_intent(world, target, "2026-11-26")
            world.authorize(iid, f"TASK-TWIN-BC-DOOR-{number}")
            sent = world.path.dispatch(ACTOR, iid, world.bc_registry, world.mc_client,
                                       session=SESSION)
            key = world.path.get(ACTOR, iid)["intent"]["idempotency_key"]
            with self.subTest(code=code):
                self.assertEqual((sent["state"], sent["reason"]), ("failed", code))
                self.assertNotIn("PATCH", [answer[0] for answer in door.answers])
                seen = owner.read_back({"idempotency_key": key, "relation": cited["relation"],
                                        "key": cited["key"]})
                self.assertEqual((seen["operation"], seen["fenced"], seen["reason"],
                                  seen["record"]), ("not_applied", True, code, cited["row"]))
                with self.assertRaises(OwnerRejected) as replay:   # the key is fenced
                    owner.dispatch(self.payload(cited, "2026-11-26"), key)
                self.assertEqual(replay.exception.code, "idempotency_key_fenced")
                with self.assertRaises(KeyError):   # nothing left pending to process
                    IsolatedOwnerSystem(owner.path).process_pending(key, apply=True)

    def test_no_door_exception_message_reaches_the_read_back_or_the_action_record(self):
        world = self.world()
        owner = world.bc.owner
        twin_door = owner.door

        def raising(exc):
            def door(payload):
                raise exc
            return door

        for number, (door, state, operation, reason) in enumerate((
                (ODataDoor(world.bc, _Fails(world.transport, "PATCH", True, URL)),
                 "dispatched_unknown", "pending", "door_answer_lost:ConnectionError"),
                (raising(NothingSent(f"read {URL} failed")), "failed", "not_applied",
                 "door_nothing_sent:door_code_redacted:" + RETRY),
                (raising(OwnerRejected(f"http_403:{URL}")), "failed", "not_applied",
                 "door_code_redacted"),
                (raising(OwnerRejected(HOST)), "failed", "not_applied", "door_code_redacted"),
                (raising(OwnerRejected(f"odata_400:{LABEL}")), "failed", "not_applied",
                 "door_code_redacted"),
                (raising(NothingSent(f"odata_read_failed:{LABEL}")), "failed", "not_applied",
                 "door_nothing_sent:door_code_redacted:" + RETRY),
                (raising(OwnerRejected(DOTTED)), "failed", "not_applied", DOTTED),
                (raising(NothingSent("x" * 121)), "failed", "not_applied",
                 "door_nothing_sent:door_code_redacted:" + RETRY))):
            target = targets(world)[min(number, 2)]   # a refused write leaves it empty
            cited = world.resolve(target, ACTOR)
            owner.door = door
            _, iid = bc_intent(world, target, f"2026-11-{20 + number}")
            world.authorize(iid, f"TASK-TWIN-BC-REDACT-{number}")
            sent = world.path.dispatch(ACTOR, iid, world.bc_registry, world.mc_client,
                                       session=SESSION)
            owner.door = twin_door
            if state == "dispatched_unknown":
                world.path.read_back(ACTOR, iid, world.bc_read_back, session=SESSION)
            key = world.path.get(ACTOR, iid)["intent"]["idempotency_key"]
            seen = owner.read_back({"idempotency_key": key, "relation": cited["relation"],
                                    "key": cited["key"]})
            record = world.path.view(ACTOR, iid)   # the Workbench action record
            with self.subTest(reason=reason):
                self.assertEqual(sent["state"], state)
                self.assertEqual((seen["operation"], seen["reason"]), (operation, reason))
                self.assertEqual(record["read_backs"][-1]["operation"]["reason"]
                                 if record["read_backs"] else record["dispatches"][-1]["reason"],
                                 reason)
                for shown in (sent, seen, record, world.path.get(ACTOR, iid)):
                    for host in (HOST, LABEL):
                        self.assertNotIn(host, json.dumps(shown, default=str))
                if state == "failed":   # the employee is told what happened
                    self.assertEqual(sent["reason"], reason)
                self.assertEqual(world.resolve(target, ACTOR)["row"], cited["row"])
        # A logged traceback of the owner's rejection names no host: nothing is chained.
        cited = world.resolve(targets(world)[2], ACTOR)
        for number, raised in enumerate((OwnerRejected(f"http_403:{URL}"),
                                         NothingSent(f"read {URL} failed"))):
            owner.door = raising(raised)
            with self.subTest(raised=type(raised).__name__), \
                    self.assertRaises(OwnerRejected) as refused:
                owner.dispatch(self.payload(cited, "2026-11-28"), f"wb-{number:040x}")
            shown = "".join(traceback.format_exception(
                type(refused.exception), refused.exception, refused.exception.__traceback__))
            self.assertIn(refused.exception.code, shown)
            self.assertNotIn(HOST, shown)
            self.assertIsNone(refused.exception.__context__)
        owner.door = twin_door
        stored = [path for path in self.dir.rglob("*") if path.is_file()]   # every log, db
        self.assertTrue(stored)
        self.assertEqual([path.name for path in stored
                          if any(host.encode() in path.read_bytes() for host in (HOST, LABEL))],
                         [])

    def test_an_adapter_rejection_records_only_a_code(self):
        world = self.world()
        allow = mode.test_mode(allow={PORTAL_CAPABILITY})
        allow.__enter__()
        self.addCleanup(allow.__exit__, None, None, None)
        for number, (status, body, reason) in enumerate((
                (400, {"error": f"Refused by {URL}", "code": LABEL}, "http_400:validation"),
                (403, {"error": f"Forbidden for {LABEL}"}, "http_403:forbidden"),
                (409, {"error": f"Order locked, see {URL}", "code": "ORDER_NOT_EDITABLE"},
                 "http_409:ORDER_NOT_EDITABLE"),
                (422, {"error": {"code": DOTTED.split(":")[1], "message": URL}},
                 "http_422:" + DOTTED.split(":")[1]),
                # No code: a fixed hint by status, never the Portal's words.
                (404, {"error": "Sales order not found"}, "http_404:not_found"),
                # A one-word Portal code is kept; one inside an error object with no code.
                (400, {"error": "Invalid customer PO", "code": "VALIDATION"},
                 "http_400:VALIDATION"),
                (409, {"error": {"message": URL}, "code": "CONFLICT"}, "http_409:CONFLICT"),
                # A free shape is not the Portal's code.
                (422, {"error": "PO_5501 rejected", "code": "PO_5501"}, "http_422:validation"),
                (401, {"error": "Sign in", "code": "ACME_CORP"}, "http_401")), start=1):
            order = (1, 2, 3, 4, 5, 9)[(number - 1) % 6]   # orders with no PO; a refusal
            _, iid = world.portal_intent(order, f"PO71{number:02d}")   # leaves them so
            world.authorize(iid, f"TASK-TWIN-SO-REDACT-{number}")
            with mock.patch.object(world.portal, "_write",
                                   return_value={"status": status, "body": body}):
                sent = world.path.dispatch(ACTOR, iid, world.registry, world.mc_client,
                                           session=SESSION)
            record = world.path.view(ACTOR, iid)   # the Workbench action record
            with self.subTest(status=status):
                self.assertEqual((sent["state"], sent["reason"]), ("failed", reason))
                self.assertEqual(record["dispatches"][-1]["reason"], reason)
                for shown in (sent, record, world.path.get(ACTOR, iid)):
                    for host in (HOST, LABEL, "not found", "PO_5501", "ACME_CORP"):
                        self.assertNotIn(host, json.dumps(shown, default=str))
        stored = [path for path in self.dir.rglob("*") if path.is_file()]
        self.assertEqual([path.name for path in stored
                          if any(host.encode() in path.read_bytes() for host in (HOST, LABEL))],
                         [])

    def test_only_vocabulary_codes_pass_the_door(self):
        for shape in FREE:
            for text in (shape, f"odata_400:{shape}", f"odata_read_failed:{shape}"):
                with self.subTest(text=text):
                    self.assertEqual(door_code(text), DOOR_CODE_REDACTED)
            with self.subTest(owner_code=shape):
                self.assertEqual(owner_code(422, {"error": {"code": shape}, "code": shape}),
                                 "http_422:validation")
        for body in ("Sales order not found", None, {"code": ["NOT_FOUND"]},   # unexpected
                     {"error": ["NOT_FOUND"], "code": 404}):
            with self.subTest(body=body):
                self.assertEqual(owner_code(404, body), "http_404:not_found")
        for text in ("VALIDATION", "http_409:ORDER_NOT_EDITABLE", "odata_404:BadRequest_NotFound",
                     DOTTED, "odata_read_failed:ConnectionError", "etag_mismatch"):
            with self.subTest(text=text):
                self.assertEqual(door_code(text), text)
        # A lost answer keeps the type of Python's own transport exceptions, nothing else.
        world = self.world()
        owner, twin_door = world.bc.owner, world.bc.owner.door
        cited = world.resolve(targets(world)[0], ACTOR)
        for number, (raised, reason) in enumerate((
                (http.client.RemoteDisconnected(URL), "door_answer_lost:RemoteDisconnected"),
                (http.client.IncompleteRead(URL.encode()), "door_answer_lost:IncompleteRead"),
                (AcmeCorpError(URL), "door_answer_lost:" + DOOR_CODE_REDACTED),
                # A type named like http.client's but defined elsewhere is not kept.
                (type("RemoteDisconnected", (ConnectionError,), {})(URL),
                 "door_answer_lost:" + DOOR_CODE_REDACTED))):

            def door(payload, raised=raised):
                raise raised

            owner.door = door
            key = f"wb-{number + 16:040x}"
            with self.subTest(reason=reason), self.assertRaises(type(raised)):
                owner.dispatch(self.payload(cited, "2026-11-29"), key)
            seen = owner.read_back({"idempotency_key": key, "relation": cited["relation"],
                                    "key": cited["key"]})
            self.assertEqual((seen["operation"], seen["reason"]), ("pending", reason))
        owner.door = twin_door

    def test_a_fault_the_door_does_not_emulate_is_refused_with_nothing_sent(self):
        world = self.world()
        owner, door = world.bc.owner, world.bc.owner.door
        cited = world.resolve(targets(world)[0], ACTOR)
        unsupported = [fault for fault in FAULTS if fault not in DOOR_FAULTS] + ["no_such"]
        self.assertTrue(unsupported[:-1])
        for number, fault in enumerate(unsupported):
            key = f"wb-{number:040x}"
            owner.faults.append(fault)
            with self.subTest(fault=fault), self.assertRaises(OwnerRejected) as refused:
                owner.dispatch(self.payload(cited, "2026-11-24"), key)
            self.assertEqual(refused.exception.code,
                             "fault_unknown" if fault == "no_such"
                             else "fault_not_emulated_through_the_door")
            with self.assertRaises(OwnerRejected) as replay:   # the key is fenced
                owner.dispatch(self.payload(cited, "2026-11-24"), key)
            self.assertEqual(replay.exception.code, "idempotency_key_fenced")
            seen = owner.read_back({"idempotency_key": key, "relation": cited["relation"],
                                    "key": cited["key"]})
            self.assertEqual((seen["operation"], seen["fenced"], seen["record"]),
                             ("not_applied", True, cited["row"]))
        self.assertEqual(door.answers, [])
        # On the action path: a certain rejection, never dispatched_unknown, never sent.
        _, iid = bc_intent(world, targets(world)[0], "2026-11-25")
        world.authorize(iid, "TASK-TWIN-BC-DOOR")
        owner.faults.append("timeout_pending")
        first = world.path.dispatch(ACTOR, iid, world.bc_registry, world.mc_client,
                                    session=SESSION)
        self.assertNotEqual(first["state"], "dispatched_unknown")
        self.assertEqual(first["reason"], "fault_not_emulated_through_the_door")
        self.assertEqual(door.answers, [])
        self.assertEqual(world.resolve(targets(world)[0], ACTOR)["row"], cited["row"])

    def test_an_unknown_fault_without_a_door_is_refused_and_fenced(self):
        owner = IsolatedOwnerSystem(self.dir / "owner.sqlite")
        owner.seed("fixture.order", {"id": 1}, {"po_reference": ""})
        owner.faults.append("no_such")
        payload = {"schema": DISPATCH_SCHEMA, "capability": "fixture.record.set_field",
                   "relation": "fixture.order", "key": {"id": 1},
                   "set": {"po_reference": "PO-1"}, "expect": []}
        with self.assertRaises(OwnerRejected) as refused:
            owner.dispatch(payload, "wb-" + "c" * 40)
        self.assertEqual(refused.exception.code, "fault_unknown")
        self.assertEqual((owner.record("fixture.order", {"id": 1}),
                          [(op["status"], op["reason"]) for op in owner.operations()]),
                         ({"po_reference": ""}, [("rejected", "fault_unknown")]))

    def test_an_unmarked_pending_operation_is_refused_without_a_door(self):
        owner = IsolatedOwnerSystem(self.dir / "owner.sqlite")
        payload = {"schema": DISPATCH_SCHEMA, "capability": "fixture.record.set_field",
                   "relation": "fixture.order", "set": {"po_reference": "PO-1"}, "expect": []}
        for number in (1, 2):
            owner.seed("fixture.order", {"id": number}, {"po_reference": ""})
            owner.faults.append("timeout_pending")
            with self.assertRaises(TimeoutError):
                owner.dispatch({**payload, "key": {"id": number}}, f"wb-{number:040x}")
        # Key 1 as a door operation logged before door operations were marked holds it.
        with sqlite3.connect(owner.path) as db:
            db.execute("UPDATE owner_operation SET reason=NULL WHERE idempotency_key=?",
                       (f"wb-{1:040x}",))
        for apply in (True, False):
            with self.subTest(apply=apply), self.assertRaises(OwnerRejected) as refused:
                owner.process_pending(f"wb-{1:040x}", apply=apply)
            self.assertEqual(refused.exception.code, "pending_operation_unmarked")
        self.assertEqual(owner.process_pending(f"wb-{2:040x}", apply=True), "applied")
        self.assertEqual([(op["status"], owner.record("fixture.order", {"id": number}))
                          for number, op in zip((1, 2), owner.operations())],
                         [("pending", {"po_reference": ""}),
                          ("applied", {"po_reference": "PO-1"})])


if __name__ == "__main__":
    import unittest
    unittest.main()
