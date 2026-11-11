"""The autopilot write adapters for every business system, driven through the one gated
executor (``operating.autopilot_write.Executor``): each records the exact request and the
exact undo request as a ``dry_run`` while its workflow is not live; live, the undo is filled
from the system's answer; every unknown, unsafe or excluded input fails closed.

SYNTHETIC items, ids and addresses; fake transports only: nothing here reaches a network.
Evidence label: injected-transport.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import unittest

from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating.tests.test_autopilot_write import (BC_COMPANY, HIDDEN, HIDDEN_OID, NOW, OWNER,
                                                  OWNER_OID, Base, Transport, guard)

GRAPH = "https://graph.microsoft.com/v1.0"
DOC = "55555555-5555-4555-8555-555555555555"
TEAM = "66666666-6666-4666-8666-666666666666"
BC_ROOT = f"/v2.0/{{tenant}}/Production/api/v2.0/companies({BC_COMPANY})"
FM = "/fmi/data/vLatest/databases/Plant/layouts/Batch/records"
MCP = "https://mc.plxcustomer.io/api/cursor/mcp"
SITE = "plx.sharepoint.com,aaaa1111-0000-4000-8000-000000000001,bbbb2222-0000-4000-8000-000000000002"


def item(n, write=None, draft=None):
    prepared = {"summary": "Routine follow-up", "gaps": []}
    if write is not None:
        prepared["write"] = {"reason": "Routine follow-up from the thread", **write}
    if draft is not None:
        prepared["draft"] = draft
    return {"item_id": f"it_sys_{n}", "autopilot": "customer_po", "owner": OWNER,
            "title": "Routine follow-up", "citations": ["graph-mail://x/m1@1"],
            "body": {"label": "Routine follow-up",
                     "happened": {"event_type": "message.received", "object_type": "Email",
                                  "sender": "buyer@customer.example", "subject": "Follow-up"},
                     "prepared": prepared}}


def mcp(tool, arguments):
    return {"method": "POST", "url": MCP, "body": {"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": tool, "arguments": arguments}}}


# (name, prepared write or draft, the request's essentials, the undo request or None)
CASES = [
    ("mc_note", {"write": {"system": "mc", "operation": "task_note",
                           "target": {"task_id": "TASK-7"},
                           "payload": {"notes": "Shipping docs filed in the job folder."}}},
     mcp("mc_report_progress", {"taskId": "TASK-7",
                                "notes": "Shipping docs filed in the job folder."}), None),
    ("mc_update", {"write": {"system": "mc", "operation": "task_update",
                             "target": {"task_id": "TASK-7"},
                             "payload": {"addLabels": ["waiting-on-customer"],
                                         "removeLabels": ["triage"], "title": "Ship lot 4"},
                             "precondition": {"prior": {"labels": ["triage", "lane:ops"],
                                                        "title": "Ship lot"}}}},
     mcp("mc_update_task", {"taskId": "TASK-7", "patch": {
         "addLabels": ["waiting-on-customer"], "removeLabels": ["triage"],
         "title": "Ship lot 4"}}),
     mcp("mc_update_task", {"taskId": "TASK-7", "patch": {
         "addLabels": ["triage"], "removeLabels": ["waiting-on-customer"],
         "title": "Ship lot"}})),
    ("bc_line", {"write": {"system": "bc", "operation": "sales_order_line_create",
                           "target": {"document_id": DOC},
                           "payload": {"lineType": "Item", "lineObjectNumber": "ITEM-1",
                                       "quantity": "4"},
                           "precondition": {"document": "SO-1 open"}}},
     {"method": "POST", "url": f"{BC_ROOT}/salesOrders({DOC})/salesOrderLines",
      "body": {"lineType": "Item", "lineObjectNumber": "ITEM-1", "quantity": "4"}},
     {"method": "DELETE", "url": f"{BC_ROOT}/salesOrders({DOC})/salesOrderLines({{response:id}})",
      "headers": {"If-Match": "{response:@odata.etag}"}, "body": None}),
    ("bc_comment", {"write": {"system": "bc", "operation": "purchase_order_comment",
                              "target": {"document_id": DOC},
                              "payload": {"description": "Vendor confirmed dock time 9 am"},
                              "precondition": {"document": "PO-1 open"}}},
     {"method": "POST", "url": f"{BC_ROOT}/purchaseOrders({DOC})/purchaseOrderLines",
      "body": {"lineType": "Comment", "description": "Vendor confirmed dock time 9 am"}},
     {"method": "DELETE",
      "url": f"{BC_ROOT}/purchaseOrders({DOC})/purchaseOrderLines({{response:id}})",
      "headers": {"If-Match": "{response:@odata.etag}"}, "body": None}),
    ("portal_po", {"write": {"system": "portal", "operation": "sales_order_set_customer_po",
                             "target": {"order_id": "so_abc123"},
                             "payload": {"poReference": "P-778"},
                             "precondition": {"prior": {"poReference": None}}}},
     {"method": "PATCH", "url": "/api/mrp/sales-orders/so_abc123",
      "body": {"poReference": "P-778"}},
     {"method": "PATCH", "url": "/api/mrp/sales-orders/so_abc123",
      "headers": {"Content-Type": "application/json"}, "body": {"poReference": None}}),
    ("fm_create", {"write": {"system": "filemaker", "operation": "record_create",
                             "target": {"database": "Plant", "layout": "Batch"},
                             "payload": {"Status": "Ready", "Lot": "L-1"}}},
     {"method": "POST", "url": FM, "body": {"fieldData": {"Status": "Ready", "Lot": "L-1"}}},
     {"method": "DELETE", "url": FM + "/{response:response/recordId}", "body": None}),
    ("fm_edit", {"write": {"system": "filemaker", "operation": "record_edit",
                           "target": {"database": "Plant", "layout": "Batch", "record_id": "12"},
                           "payload": {"Status": "Ready"},
                           "precondition": {"mod_id": "3", "prior": {"Status": "Hold"}}}},
     {"method": "PATCH", "url": FM + "/12",
      "body": {"fieldData": {"Status": "Ready"}, "modId": "3"}},
     {"method": "PATCH", "url": FM + "/12", "headers": {"Content-Type": "application/json"},
      "body": {"fieldData": {"Status": "Hold"}, "modId": "{response:response/modId}"}}),
    ("teams_chat", {"draft": {"channel": "teams", "chat_id": "19:abc@thread.v2",
                              "body": "Docs are in the folder.", "members": []}},
     {"method": "POST", "url": f"{GRAPH}/chats/19:abc@thread.v2/messages",
      "body": {"body": {"contentType": "text", "content": "Docs are in the folder."}}},
     {"method": "POST", "url": f"{GRAPH}/users/{OWNER}/chats/19:abc@thread.v2/messages/"
      "{response:id}/softDelete", "body": None}),
    ("teams_channel", {"draft": {"channel": "teams", "team_id": TEAM,
                                 "channel_id": "19:ops@thread.tacv2", "body": "Lot 4 shipped."}},
     {"method": "POST", "url": f"{GRAPH}/teams/{TEAM}/channels/19:ops@thread.tacv2/messages",
      "body": {"body": {"contentType": "text", "content": "Lot 4 shipped."}}},
     {"method": "POST", "url": f"{GRAPH}/teams/{TEAM}/channels/19:ops@thread.tacv2/messages/"
      "{response:id}/softDelete", "body": None}),
    ("planner_create", {"write": {"system": "planner", "operation": "task_create",
                                  "payload": {"planId": "plan-1", "title": "Book the carrier",
                                              "assignees": [OWNER_OID]}}},
     {"method": "POST", "url": f"{GRAPH}/planner/tasks",
      "body": {"planId": "plan-1", "title": "Book the carrier", "assignments": {OWNER_OID: {
          "@odata.type": "#microsoft.graph.plannerAssignment", "orderHint": " !"}}}},
     {"method": "DELETE", "url": f"{GRAPH}/planner/tasks/{{response:id}}",
      "headers": {"If-Match": "{response:@odata.etag}"}, "body": None}),
    ("planner_update", {"write": {"system": "planner", "operation": "task_update",
                                  "target": {"task_id": "task-9"},
                                  "payload": {"percentComplete": 100},
                                  "precondition": {"etag": "W/\"e1\"",
                                                   "prior": {"percentComplete": 50}}}},
     {"method": "PATCH", "url": f"{GRAPH}/planner/tasks/task-9",
      "headers": {"Content-Type": "application/json", "If-Match": "W/\"e1\"",
                  "Prefer": "return=representation"}, "body": {"percentComplete": 100}},
     {"method": "PATCH", "url": f"{GRAPH}/planner/tasks/task-9",
      "headers": {"Content-Type": "application/json", "If-Match": "{response:@odata.etag}",
                  "Prefer": "return=representation"}, "body": {"percentComplete": 50}}),
    ("calendar_create", {"write": {"system": "calendar", "operation": "event_create",
                                   "target": {"user": "someone-else@plx.example"},
                                   "payload": {"subject": "Carrier pickup",
                                               "start": {"dateTime": "2026-11-13T09:00:00",
                                                         "timeZone": "America/Toronto"},
                                               "end": {"dateTime": "2026-11-13T09:30:00",
                                                       "timeZone": "America/Toronto"},
                                               "attendees": ["buyer@customer.example"]}}},
     {"method": "POST", "url": f"{GRAPH}/users/{OWNER}/events",
      "body": {"subject": "Carrier pickup",
               "start": {"dateTime": "2026-11-13T09:00:00", "timeZone": "America/Toronto"},
               "end": {"dateTime": "2026-11-13T09:30:00", "timeZone": "America/Toronto"},
               "attendees": [{"emailAddress": {"address": "buyer@customer.example"},
                              "type": "required"}]}},
     {"method": "DELETE", "url": f"{GRAPH}/users/{OWNER}/events/{{response:id}}", "body": None}),
    ("calendar_update", {"write": {"system": "calendar", "operation": "event_update",
                                   "target": {"event_id": "AAMk="},
                                   "payload": {"location": "Dock 2"},
                                   "precondition": {"prior": {"location": "Dock 1"}}}},
     {"method": "PATCH", "url": f"{GRAPH}/users/{OWNER}/events/AAMk=",
      "body": {"location": {"displayName": "Dock 2"}}},
     {"method": "PATCH", "url": f"{GRAPH}/users/{OWNER}/events/AAMk=",
      "headers": {"Content-Type": "application/json"},
      "body": {"location": {"displayName": "Dock 1"}}}),
    ("sp_item_create", {"write": {"system": "sharepoint", "operation": "list_item_create",
                                  "target": {"site_id": SITE, "list_id": "list-1"},
                                  "payload": {"Title": "Lot 4 retained sample", "Shelf": 3}}},
     {"method": "POST", "url": f"{GRAPH}/sites/{SITE}/lists/list-1/items",
      "body": {"fields": {"Title": "Lot 4 retained sample", "Shelf": 3}}},
     {"method": "DELETE", "url": f"{GRAPH}/sites/{SITE}/lists/list-1/items/{{response:id}}",
      "body": None}),
    ("sp_item_update", {"write": {"system": "sharepoint", "operation": "list_item_update",
                                  "target": {"site_id": SITE, "list_id": "list-1",
                                             "item_id": "17"},
                                  "payload": {"Status": "Released"},
                                  "precondition": {"etag": "\"e,4\"",
                                                   "prior": {"Status": "On hold"}}}},
     {"method": "PATCH", "url": f"{GRAPH}/sites/{SITE}/lists/list-1/items/17/fields",
      "headers": {"Content-Type": "application/json", "If-Match": "\"e,4\""},
      "body": {"Status": "Released"}},
     {"method": "PATCH", "url": f"{GRAPH}/sites/{SITE}/lists/list-1/items/17/fields",
      "headers": {"Content-Type": "application/json", "If-Match": "{response:@odata.etag}"},
      "body": {"Status": "On hold"}}),
]

# a file upload is never routine: only a person's approval runs one (Approved below)
UPLOAD = {"write": {"system": "sharepoint", "operation": "file_upload",
                    "target": {"drive_id": "b!drive1", "parent_id": "01FOLDER"},
                    "payload": {"name": "Lot 4 CoA.pdf", "content_type": "application/pdf",
                                "content_b64": base64.b64encode(b"%PDF-1.4 lot 4").decode()}}}


class Systems(Base):
    def setUp(self) -> None:
        super().setUp()
        self.fake = Transport()

    def live_adapters(self, env):
        return {cls.kind: cls(self.fake, environ={"PLX_AUTOPILOT_BC_COMPANY_ID": BC_COMPANY,
                                                 **env})
                for cls in (wa.MCTaskAdapter, wa.BCAdapter, wa.PortalAdapter,
                            wa.FileMakerAdapter, wa.TeamsAdapter, wa.PlannerAdapter,
                            wa.CalendarAdapter, wa.SharePointAdapter)}

    def test_every_system_records_exact_request_and_undo_as_dry_run(self):
        # transports wired in, yet nothing goes out: the gate is not passed
        ex = self.executor({}, **self.live_adapters({}))
        for n, (name, prepared, want, undo) in enumerate(CASES):
            with self.subTest(name):
                rows = ex.run(item(n, **prepared), NOW)
                self.assertEqual([r["status"] for r in rows], ["dry_run"], rows[-1].get("reasons"))
                r = rows[0]
                self.assertEqual(r["reasons"], ["gate_not_passed"])
                # a message's or an invitation's words and a task's assignee ids are
                # journalled as digests (the draft holds them)
                want = aw._audit_view(r["kind"], want)
                if r["kind"] in ("send_mail", "send_teams"):
                    self.assertNotIn("Docs are in the folder", json.dumps(r["request"]))
                for key, value in want.items():
                    self.assertEqual(r["request"][key], value, key)
                self.assertEqual(r["undo_request"], aw._audit_view(r["kind"], undo))
                self.assertEqual(r["undo_waits_for"], wa.placeholders(undo))
                for words in ("Carrier pickup", "Dock 1", "Dock 2", OWNER_OID):
                    self.assertNotIn(words, json.dumps([r["request"], r["undo_request"]]))
                self.assertTrue(r["undo_note"])
        self.assertEqual(self.fake.calls, [])
        rows = ex.journal.rows()
        self.assertEqual(sum(r.get("status") == "dry_run" for r in rows), len(CASES))
        self.assertFalse(any(r.get("event") == "attempt" for r in rows))
        # status names every adapter, its operations and what it lacks; nothing is live
        found = {a["kind"]: a for a in aw.status(self.state, {})["adapters"]}
        self.assertEqual(set(found), set(aw.KINDS))
        for kind, a in found.items():
            if kind != "send_mail":
                self.assertTrue(a["state"].startswith("unavailable: ") and a["needs"], kind)
        self.assertEqual(found["sage_write"]["operations"], [])

    def test_live_undo_is_filled_from_the_answer_or_refused(self):
        env = self.go_live(("customer_order_intake",))
        ex = self.executor(env, **self.live_adapters(env))
        planner = {name: p for name, p, _w, _u in CASES}["planner_create"]
        self.fake.answers = [(201, {"id": "pt_1", "@odata.etag": "W/\"e2\""}), (204, {}),
                             (201, {"id": "../me", "@odata.etag": "W/\"e3\""})]
        done = ex.run(item(100, **planner), NOW)[0]
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["inverse"], {"method": "DELETE",
                                           "url": f"{GRAPH}/planner/tasks/pt_1",
                                           "headers": {"If-Match": "W/\"e2\""}, "body": None})
        # the master switch off: no undo goes out either (AP-21)
        off = aw.Executor(self.state, environ={k: v for k, v in env.items()
                                               if k != aw.MASTER_ENV},
                          adapters=ex.adapters, guard=ex.guard)
        with self.assertRaises(ValueError):
            off.undo(done["key"], NOW)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(ex.undo(done["key"], NOW)["status"], "done")
        self.assertEqual(self.fake.calls[-1][0]["url"], f"{GRAPH}/planner/tasks/pt_1")
        # an answer whose id is not a plain id gives no undo, never a guessed one
        unsafe = ex.run(item(101, **planner), NOW)[0]
        self.assertEqual((unsafe["status"], unsafe["inverse"]), ("done", None))
        with self.assertRaises(ValueError):
            ex.undo(unsafe["key"], NOW)
        self.assertEqual(len(self.fake.calls), 3)
        # a body placeholder takes the answer's value (FileMaker's new modId)
        plan = CASES[6][3]
        self.assertEqual(wa.fill(plan, {"response": {"modId": "4"}})["body"]["modId"], "4")
        self.assertIsNone(wa.fill(plan, {"response": {"modId": {"x": 1}}}))
        self.assertIsNone(wa.fill(CASES[2][3], {"id": DOC, "@odata.etag": "W/\"1\"\r\nX: y"}))
        # a BC change's undo restores the prior values as BC types them (decimals as
        # numbers on the wire), and the journal keeps that wire text for the undo
        bc = wa.BCAdapter(environ={"PLX_AUTOPILOT_BC_COMPANY_ID": BC_COMPANY})
        change = {"operation": "purchase_order_line_update", "target": {"record_id": DOC},
                  "payload": {"quantity": "5"},
                  "precondition": {"etag": "W/\"1\"", "prior": {"quantity": "4.5"}}}
        back = wa.fill(bc.undo_request(change, bc.request(change)), {"@odata.etag": "W/\"2\""})
        kept = aw._journal_safe(back, wire=True)
        self.assertEqual((kept["method"], kept["headers"]["If-Match"], kept["body_json"]),
                         ("PATCH", "W/\"2\"", '{"quantity":4.5}'))

    def test_unknown_unsafe_or_unundoable_input_fails_closed(self):
        ex = self.executor({}, **self.live_adapters({}))
        prepared = {name: p for name, p, _w, _u in CASES}

        def run(n, change, which):
            p = json.loads(json.dumps(prepared[which]))
            change(p.get("write") or p.get("draft"))
            return ex.run(item(n, **p), NOW)[0]

        refused = {
            "mc assignee": ("mc_update", lambda w: w["payload"].update(accountableOwner=OWNER)),
            "mc governance label": ("mc_update", lambda w: w["payload"].update(
                addLabels=["lane:finance"])),
            "mc update without prior": ("mc_update", lambda w: w.pop("precondition")),
            "mc stage via note": ("mc_note", lambda w: w["payload"].update(stage="merged")),
            "bc unknown op": ("bc_line", lambda w: w.update(operation="post_invoice")),
            "bc G/L line": ("bc_line", lambda w: w["payload"].update(lineType="Account")),
            "bc line for no document": ("bc_line", lambda w: w.update(target={})),
            "portal overwrite": ("portal_po", lambda w: w["precondition"]["prior"].update(
                poReference="P-1")),
            "portal no prior": ("portal_po", lambda w: w["precondition"].update(
                prior={"x": 1})),
            "fm related field": ("fm_create", lambda w: w["payload"].update(
                {"Customer::Name": "x"})),
            "fm edit without prior": ("fm_edit", lambda w: w["precondition"].update(prior={})),
            "fm edit without modId": ("fm_edit", lambda w: w["precondition"].pop("mod_id")),
            "planner reassign": ("planner_update", lambda w: w["payload"].update(
                assignments={})),
            "planner header injection": ("planner_update", lambda w: w["precondition"].update(
                etag="W/\"1\"\r\nX: y")),
            "calendar update other fields": ("calendar_update", lambda w: w["payload"].update(
                attendees=["buyer@customer.example"])),
            "teams bad channel": ("teams_channel", lambda d: d.update(channel_id="../x")),
            "sp system column": ("sp_item_create", lambda w: w["payload"].update(Author="x")),
            "sp underscore column": ("sp_item_create", lambda w: w["payload"].update(
                {"_ModerationStatus": 0})),
            "sp nested value": ("sp_item_create", lambda w: w["payload"].update(Shelf={"a": 1})),
            "sp update without etag": ("sp_item_update", lambda w: w["precondition"].pop("etag")),
            "sp update without prior": ("sp_item_update", lambda w: w["precondition"].update(
                prior={})),
            "sp bad site": ("sp_item_create", lambda w: w["target"].update(site_id="../x")),
        }
        for n, (name, (which, change)) in enumerate(refused.items(), 200):
            with self.subTest(name):
                r = run(n, change, which)
                self.assertIn(r["status"], ("refused", "not_routine"), r)
        # a malformed shape is refused, never raised mid-run (one bad item stopped the batch)
        malformed = {"payload list": ("fm_create", lambda w: w.update(payload=["x"])),
                     "target list": ("fm_create", lambda w: w.update(target=["x"])),
                     "precondition list": ("fm_edit", lambda w: w.update(precondition=["x"])),
                     "people number": ("fm_create", lambda w: w.update(people=5)),
                     "people text": ("fm_create", lambda w: w.update(people="ab")),
                     "members number": ("teams_channel", lambda d: d.update(members=5)),
                     "members text": ("teams_channel", lambda d: d.update(members="ab")),
                     "unknown system": ("mc_note", lambda w: w.update(system="xyz"))}
        for n, (name, (which, change)) in enumerate(malformed.items(), 280):
            with self.subTest(name):
                self.assertEqual(run(n, change, which)["status"], "refused", name)
        sage = item(300, write={"system": "sage", "operation": "invoice_entry", "payload": {}})
        self.assertEqual(ex.run(sage, NOW)[0]["status"], "not_routine")
        with self.assertRaises(wa.AdapterRefused):           # no write path, even if listed
            wa.SageAdapter(environ={}).request({"operation": "invoice_entry", "payload": {}})
        # never money, prices, credit, permissions in an MC task or a Planner task
        for n, (which, text) in enumerate([("mc_note", "Chase the overdue payment"),
                                           ("mc_note", "Grant access to the vendor portal"),
                                           ("planner_create", "Agree the new price list"),
                                           ("mc_update", "Raise the credit limit")], 310):
            def change(w, text=text):
                w["payload"]["notes" if which == "mc_note" else "title"] = text
            self.assertEqual(run(n, change, which)["status"], "not_routine", text)
        # people: an excluded assignee or attendee, an attendee on an unknown domain
        self.assertEqual(run(320, lambda w: w["payload"].update(assignees=[HIDDEN_OID]),
                             "planner_create")["reasons"], ["person_excluded"])
        self.assertEqual(run(321, lambda w: w["payload"].update(assignees="x"),
                             "planner_create")["status"], "excluded")
        self.assertEqual(run(322, lambda w: w["payload"].update(attendees=[HIDDEN]),
                             "calendar_create")["reasons"], ["person_excluded"])
        self.assertEqual(run(323, lambda w: w["payload"].update(
            attendees=["someone@elsewhere.example"]), "calendar_create")["reasons"],
            ["unknown_domain"])
        self.assertEqual(self.fake.calls, [])

    def test_dry_run_cardinality(self):
        ex = self.executor({}, **self.live_adapters({}))
        seen = 0
        for count in (1, 10, 60):
            began = time.monotonic()
            for k in range(count):
                name, prepared, _want, _undo = CASES[k % len(CASES)]
                rows = ex.run(item(f"c{count}_{k}", **prepared), NOW)
                self.assertEqual([r["status"] for r in rows], ["dry_run"], name)
            self.assertLess(time.monotonic() - began, 30)
            seen += count
            rows = [r for r in ex.journal.rows() if r.get("status") == "dry_run"]
            self.assertEqual(len(rows), seen)
            self.assertEqual(len({r["key"] for r in rows}), seen)
        self.assertEqual(self.fake.calls, [])


class Approved(Base):
    """A person's approval of the exact item (``autopilot_write.Approval``) replaces the
    replay gate, nothing else: the approver is the item's owner and cleared by the
    exclusion guard, the owner opened the kind to approved writes, money or judgment calls
    need each reason acknowledged, kill switches, idempotency and rate limits hold."""

    def setUp(self) -> None:
        super().setUp()
        self.fake = Transport()

    def ex(self, env=None, g=None):
        env = {"PLX_AUTOPILOT_BC_COMPANY_ID": BC_COMPANY, **(env or {})}
        adapters = {cls.kind: cls(self.fake, environ=env)
                    for cls in (wa.PlannerAdapter, wa.SharePointAdapter, wa.CalendarAdapter)}
        return self.executor(env, g=g, **adapters)

    def test_approval_runs_through_the_owners_switch_not_the_gate(self):
        planner = {name: p for name, p, _w, _u in CASES}["planner_create"]
        mine = aw.Approval(by=(OWNER, OWNER_OID))
        # approved but the kind is not opened: a dry run naming both missing switches
        row = self.ex().run(item(1, **planner), NOW, mine)[0]
        self.assertEqual(row["status"], "dry_run")
        self.assertEqual(row["reasons"], ["approved_writes_off:planner_write",
                                          "gate_not_passed"])
        self.assertEqual(row["approved_by"], OWNER)
        self.assertEqual(self.fake.calls, [])
        opened = {aw.APPROVED_ENV: "planner_write,not_a_kind"}
        self.fake.answers = [(201, {"id": "pt_1", "@odata.etag": "W/\"e2\""})]
        ex = self.ex(opened)
        self.assertEqual(ex.live_kinds(item(2, **planner)), ["planner_write"])
        # live, but the person has not confirmed it goes live: nothing is sent or keyed
        held = ex.run(item(2, **planner), NOW, mine)[0]
        self.assertEqual((held["status"], held["reasons"]),
                         ("needs_confirm", ["live_needs_confirm:approval"]))
        self.assertEqual(self.fake.calls, [])
        mine = aw.Approval(by=(OWNER, OWNER_OID), confirmed_live=True)
        done = ex.run(item(2, **planner), NOW, mine)[0]
        self.assertEqual((done["status"], done["mode"]), ("done", "live"), done)
        self.assertEqual(self.fake.calls[0][0]["url"], f"{GRAPH}/planner/tasks")
        # without an approval the same item stays behind the replay gate
        self.assertEqual(ex.run(item(3, **planner), NOW)[0]["reasons"], ["gate_not_passed"])
        # the same approved item again is never written twice
        self.assertEqual(ex.run(item(2, **planner), NOW, mine)[0]["status"], "duplicate")
        self.assertEqual(len(self.fake.calls), 1)
        # its undo needs the switch it went out under: closed, nothing is sent
        with self.assertRaises(ValueError):
            self.ex({}).undo(done["key"], NOW)
        self.assertEqual(len(self.fake.calls), 1)
        self.fake.answers = [(204, {})]
        undone = ex.undo(done["key"], NOW, by=OWNER)
        self.assertEqual((undone["status"], undone["by"], undone["live_by"]),
                         ("done", OWNER, "approval"))
        self.assertEqual(self.fake.calls[-1][0]["method"], "DELETE")
        # an unknown kind name opens nothing
        self.assertEqual(aw.approved_kinds({aw.APPROVED_ENV: "1,all,*"}), set())

    def test_approver_must_be_the_cleared_owner(self):
        planner = {name: p for name, p, _w, _u in CASES}["planner_create"]
        opened = {aw.APPROVED_ENV: "planner_write"}
        cases = {
            "someone else": (aw.Approval(by=("other@plx.example",)), None, "approver_not_owner"),
            "no identity": (aw.Approval(by=()), None, "approver_unknown"),
            "ids not a tuple": (aw.Approval(by=[OWNER]), None, "approver_unknown"),
            "excluded approver id": (aw.Approval(by=(OWNER, HIDDEN_OID)), None,
                                     "approver_excluded"),
            "guard unavailable": (aw.Approval(by=(OWNER,)), guard(unavailable=True),
                                  "exclusion_unavailable"),
            "unknown origin": (aw.Approval(by=(OWNER,), origin="model"), None,
                               "approval_origin_unknown"),
        }
        for n, (name, (approval, g, why)) in enumerate(cases.items(), 10):
            with self.subTest(name):
                row = self.ex(opened, g=g).run(item(n, **planner), NOW, approval)[0]
                self.assertEqual((row["status"], row["reasons"]), ("refused", [why]))
        # the global kill switch beats an approval
        row = self.ex({**opened, aw.KILL_ENV: "1"}).run(
            item(30, **planner), NOW, aw.Approval(by=(OWNER,)))[0]
        self.assertEqual(row["status"], "killed")
        self.assertEqual(self.fake.calls, [])

    def test_money_or_judgment_needs_each_reason_acknowledged(self):
        prepared = {name: p for name, p, _w, _u in CASES}["planner_create"]
        p = json.loads(json.dumps(prepared))
        p["write"]["payload"]["title"] = "Agree the new price list"
        ex = self.ex({aw.APPROVED_ENV: "planner_write"})
        row = ex.run(item(40, **p), NOW, aw.Approval(by=(OWNER,)))[0]
        self.assertEqual((row["status"], row["reasons"], row["needs_acknowledgement"]),
                         ("not_routine", ["term:price"], True))
        partial = aw.Approval(by=(OWNER,), acknowledged=("term:money",))
        self.assertEqual(ex.run(item(40, **p), NOW, partial)[0]["status"], "not_routine")
        self.assertEqual(self.fake.calls, [])
        seen = aw.Approval(by=(OWNER,), acknowledged=("term:price",), confirmed_live=True)
        done = ex.run(item(40, **p), NOW, seen)[0]
        self.assertEqual((done["status"], done["acknowledged"]), ("done", ["term:price"]))
        # the people exclusion still refuses an excluded assignee under approval
        p2 = json.loads(json.dumps(prepared))
        p2["write"]["payload"]["assignees"] = [HIDDEN_OID]
        self.assertEqual(ex.run(item(41, **p2), NOW, aw.Approval(by=(OWNER,)))[0]["reasons"],
                         ["person_excluded"])

    def test_instruction_upload_approved_by_its_reader_keeps_bytes_out_of_the_journal(self):
        drafted = item(50, **UPLOAD)
        drafted["autopilot"] = "instruction"           # typed by the reader: no registry entry
        ex = self.ex({aw.APPROVED_ENV: "sharepoint_write"})
        # never routine on its own, even with every gate open
        self.assertEqual(ex.run(drafted, NOW)[0]["status"], "refused")   # unknown_autopilot
        self.fake.answers = [(201, {"id": "01NEWFILE", "eTag": "\"{A1},1\""})]
        row = ex.run(drafted, NOW, aw.Approval(
            by=(OWNER,), origin="instruction", confirmed_live=True,
            acknowledged=("operation_not_routine:file_upload",)))[0]
        self.assertEqual(row["status"], "done", row)
        sent = self.fake.calls[0][0]
        self.assertEqual((sent["method"], sent["url"]), (
            "PUT", f"{GRAPH}/drives/b!drive1/items/01FOLDER:/Lot%204%20CoA.pdf:/content"
                   "?@microsoft.graph.conflictBehavior=fail"))
        self.assertEqual(base64.b64decode(sent["body_b64"]), b"%PDF-1.4 lot 4")
        self.assertEqual(row["inverse"], {"method": "DELETE",
                                          "url": f"{GRAPH}/drives/b!drive1/items/01NEWFILE",
                                          "headers": {"If-Match": "\"{A1},1\""}, "body": None})
        journal = (self.state / aw.AUDIT_FILE).read_text()
        self.assertNotIn(sent["body_b64"], journal)
        self.assertIn(hashlib.sha256(b"%PDF-1.4 lot 4").hexdigest(), journal)
        # unsafe names and contents are refused before anything is sent
        for n, change in enumerate([{"name": "../x.pdf"}, {"name": "a:b.pdf"},
                                    {"name": "trailing. "}, {"name": "Bob's CoA.pdf"},
                                    {"name": "R&D plan.pdf"}, {"name": "Caf\u00e9.pdf"},
                                    {"name": "a,b.pdf"}, {"content_b64": "not base64!"},
                                    {"content_b64": ""}, {"overwrite": True},
                                    {"content_b64": base64.b64encode(
                                        b"x" * (wa.SharePointAdapter.MAX_BYTES + 1)).decode()}],
                                   60):
            bad = json.loads(json.dumps(UPLOAD))
            bad["write"]["payload"].update(change)
            got = item(n, **bad)
            got["autopilot"] = "instruction"
            r = ex.run(got, NOW, aw.Approval(by=(OWNER,), origin="instruction",
                                             acknowledged=("operation_not_routine:file_upload",)))
            self.assertEqual(r[0]["status"], "refused", change)
        self.assertEqual(len(self.fake.calls), 1)

    def test_approved_rate_limit_stops_a_runaway_bulk_accept(self):
        planner = {name: p for name, p, _w, _u in CASES}["planner_create"]
        ex = self.ex({aw.APPROVED_ENV: "planner_write"})
        mine = aw.Approval(by=(OWNER,), confirmed_live=True)
        out = [ex.run(item(f"r{k}", **planner), NOW, mine)[0]["status"] for k in range(31)]
        self.assertEqual(out[:30], ["done"] * 30)
        self.assertEqual(out[30], "rate_limited")
        self.assertEqual(len(self.fake.calls), 30)


class ReviewFixes(Base):
    """The gate path's own limit, undo under the lock and naming
    who undid, an invitation's words out of the audit yet undoable, person columns and
    names the guard refuses stopped before anyone approves."""

    def setUp(self) -> None:
        super().setUp()
        self.fake = Transport()

    ex = Approved.ex
    live_adapters = Systems.live_adapters

    def test_a_studio_accept_live_by_the_gate_keeps_the_workflows_limit(self):
        env = self.go_live(("customer_order_intake",))
        ex = self.executor(env, **self.live_adapters(env))
        planner = {name: p for name, p, _w, _u in CASES}["planner_create"]
        self.fake.answers = [(201, {"id": f"pt_{k}", "@odata.etag": "W/\"e\""})
                             for k in range(40)]
        sure = aw.Approval(by=(OWNER,), confirmed_live=True)
        rows = [ex.run(item(f"g{k}", **planner), NOW, sure)[0] for k in range(8)]
        per_hour = int(aw.load_rules()["rate_limits"]["per_hour"])
        self.assertLess(per_hour, 8)
        self.assertEqual([r["status"] for r in rows],
                         ["done"] * per_hour + ["rate_limited"] * (8 - per_hour))
        self.assertTrue(all(r["live_by"] == "gate" for r in rows[:per_hour]))

    def test_two_undos_at_once_perform_one(self):
        planner = {name: p for name, p, _w, _u in CASES}["planner_create"]
        ex = self.ex({aw.APPROVED_ENV: "planner_write"})
        self.fake.answers = [(201, {"id": "pt_1", "@odata.etag": "W/\"e2\""})]
        done = ex.run(item(70, **planner), NOW, aw.Approval(by=(OWNER,), confirmed_live=True))[0]
        second: list = []
        real = self.fake.__call__

        def racing(request, key):
            if key.startswith("undo_") and not second:
                try:                                   # the other undo, while this one runs
                    ex.undo(done["key"], NOW, by=OWNER)
                    second.append("performed")
                except ValueError as refused:
                    second.append(str(refused))
            return real(request, key)
        ex.adapters["planner_write"].transport = racing
        self.fake.answers = [(204, {})]
        self.assertEqual(ex.undo(done["key"], NOW, by=OWNER)["status"], "done")
        self.assertEqual(second, ["already undone or in progress"])
        self.assertEqual([c[0]["method"] for c in self.fake.calls], ["POST", "DELETE"])
        rows = [r for r in ex.journal.rows() if r.get("of") == done["key"]]
        self.assertTrue(rows and all(r.get("by") == OWNER for r in rows))

    def test_an_invitations_words_stay_out_of_the_audit_and_its_undo_restores_them(self):
        ex = self.ex({aw.APPROVED_ENV: "calendar_write"})
        update = {name: p for name, p, _w, _u in CASES}["calendar_update"]
        update = json.loads(json.dumps(update))
        update["write"]["payload"] = {"subject": "Pickup moved to Dock 2",
                                      "body": "Driver asks for the side door"}
        update["write"]["precondition"]["prior"] = {"subject": "Pickup at Dock 1",
                                                   "body": "Front door as usual"}
        self.fake.answers = [(200, {"id": "AAMk="}), (200, {"id": "AAMk="})]
        done = ex.run(item(80, **update), NOW,
                      aw.Approval(by=(OWNER,), confirmed_live=True))[0]
        self.assertEqual(done["status"], "done", done)
        audit = (self.state / aw.AUDIT_FILE).read_text()
        for words in ("Dock 2", "side door", "Dock 1", "Front door"):
            self.assertNotIn(words, audit)
        self.assertTrue(done["inverse_sealed"])
        self.assertEqual(ex.undo(done["key"], NOW, by=OWNER)["status"], "done")
        back = self.fake.calls[-1][0]
        self.assertEqual((back["method"], back["body"]["subject"],
                          back["body"]["body"]["content"]),
                         ("PATCH", "Pickup at Dock 1", "Front door as usual"))
        self.assertEqual(oct((self.state / aw.UNDO_FILE).stat().st_mode & 0o777), "0o600")

    def test_person_columns_and_names_the_guard_refuses_stop_before_approval(self):
        from integrations import live_source_guard as guard
        ex = self.ex({aw.APPROVED_ENV: "sharepoint_write"})
        create = {name: p for name, p, _w, _u in CASES}["sp_item_create"]
        for n, column in enumerate(("AssignedToLookupId", "ReviewerLookupIds",
                                    "managerlookupid"), 90):
            bad = json.loads(json.dumps(create))
            bad["write"]["payload"][column] = 12
            row = ex.run(item(n, **bad), NOW, aw.Approval(by=(OWNER,), confirmed_live=True))[0]
            self.assertEqual(row["status"], "refused", column)
            self.assertIn("person or lookup column", row["reasons"][0])
        self.assertEqual(self.fake.calls, [])
        # an approvable name is one the write guard lets through, word for word
        for name in ("Lot 4 CoA.pdf", "Report (v2).pdf", "a_b-c.1.xlsx"):
            self.assertIsNone(wa.SharePointAdapter.file_name_refused(name), name)
            up = json.loads(json.dumps(UPLOAD))
            up["write"]["payload"]["name"] = name
            request = wa.SharePointAdapter(self.fake, environ={}).request(up["write"])
            self.assertEqual(guard.write_shape(request["method"], request["url"]),
                             "sharepoint", name)
        for name in ("Bob's.pdf", "R&D.pdf", "Caf\u00e9.pdf", "a,b.pdf", " x.pdf", "x.pdf "):
            self.assertTrue(wa.SharePointAdapter.file_name_refused(name), name)


if __name__ == "__main__":
    unittest.main()
