"""F8: Prepared system writes wired to dry-run adapters across BC, Calendar, Planner,
SharePoint, and Mission Control (``operating.assist_writes``).

Checked at the boundary:
* 5 distinct real prepared writes per system across all 5 systems (25 dry-run intents total),
  each validated against the existing adapter schema with before/after diff, readiness, and
  ``mode == "dry_run"``.
* 0 automatic on class B: money, invoices, payments, PO/SO totals or prices, credit notes,
  charges, approvals, deletes, and external sends are always ``safety_class == "class_b"``,
  ``confirm_only == True``, ``auto_allowed == False``, and ``enforce_execution_policy(..., auto=True)``
  refuses even on a tampered intent.
* Fail-closed permission, people exclusion, door visibility, and sensitive-markings guards
  (unreadable or denied records never expose ``before`` fields).
* Zero external HTTP/network side effects in dry-run mode (every adapter has ``transport=None``).
* End-to-end prepared session integration via ``assist_prepared.serve`` and ``assist_worker.outcome``.

Evidence label: ``isolated-fixture``."""
from __future__ import annotations

import base64
import datetime as dt
import http.client
import socket
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from operating import assist_prepared as prep
from operating import assist_writes as asw
from operating import autopilot_write_readiness as readiness
from operating import prepared_writes as pw
from operating.tests import test_assist_matters as base
from operating.tests.test_autopilot_write import HIDDEN, OWNER as OWNER_EMAIL
from operating.tests.test_autopilot_write_guards import OTHER, OTHER_OID, people_guard

COMPANY_ID = "11111111-1111-1111-1111-111111111111"
DOC_ID = "22222222-2222-2222-2222-222222222222"
LINE_ID = "33333333-3333-3333-3333-333333333333"
PO_ID = "44444444-4444-4444-4444-444444444444"
ASSIGNEE_ID = OTHER_OID
SITE_ID = "plx.sharepoint.com,aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa,bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
LIST_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
DRIVE_ID = "b!drive1234567890"
PARENT_ID = "01PARENTITEMID12345"
FILE_ITEM_ID = "01FILEITEMID1234567"

OWNER = pw.Person(ids=(OWNER_EMAIL,), full=False)
COMPANY_DOMAINS = ("plx.example",)
NOW = dt.datetime(2026, 11, 25, 14, 0, tzinfo=dt.timezone.utc)

BC_ENV = {
    readiness.COMPANY_ENV: COMPANY_ID,
    "PLX_BC_SANDBOX_ENVIRONMENT": "Sandbox2",
}

READY_MAP = {
    "schema": readiness.SCHEMA,
    "checked_at": NOW.isoformat(timespec="seconds"),
    "systems": {
        "bc": {"status": "ready-live", "live_flag": True, "missing": []},
        "calendar": {"status": "ready-live", "live_flag": True, "missing": []},
        "planner": {"status": "ready-live", "live_flag": True, "missing": []},
        "sharepoint": {"status": "ready-live", "live_flag": True, "missing": []},
        "mc": {"status": "ready-dry-run", "live_flag": False, "missing": []},
    },
}

PDF_B64 = base64.b64encode(b"%PDF-1.4 sample report content").decode("ascii")
OLD_PDF_B64 = base64.b64encode(b"%PDF-1.4 previous report content").decode("ascii")


def _base_kwargs(**overrides):
    kw = dict(
        may=lambda person, action: None,
        guard=people_guard(),
        environ=BC_ENV,
        readiness_map=READY_MAP,
        known_domains=COMPANY_DOMAINS,
        now=NOW,
    )
    kw.update(overrides)
    return kw


class FiveSystemsDryRunTests(unittest.TestCase):
    """5 real prepared writes per system across BC, Calendar, Planner, SharePoint, and MC."""

    def _assert_dry_run_shape(self, intent: dict, system: str, action: str) -> None:
        self.assertEqual(intent["system"], system)
        self.assertEqual(intent["action"], action)
        self.assertEqual(intent["mode"], "dry_run")
        self.assertTrue(intent["intent_id"].startswith("awi_"))
        self.assertTrue(intent["target_handle"])
        self.assertIsInstance(intent["before"], dict)
        self.assertIsInstance(intent["after"], dict)
        self.assertIsInstance(intent["diff"], list)
        self.assertIn(intent["safety_class"], ("class_a", "class_b"))
        self.assertIsInstance(intent["confirm_only"], bool)
        self.assertIsInstance(intent["auto_allowed"], bool)
        self.assertIsInstance(intent["readiness"], dict)
        self.assertIsInstance(intent["request_preview"], dict)
        if intent["safety_class"] == "class_b":
            self.assertTrue(intent["confirm_only"])
            self.assertFalse(intent["auto_allowed"])

    def test_five_bc_prepared_writes_in_dry_run_with_before_and_after(self):
        cases = [
            (
                "sales_order_update",
                {
                    "system": "bc",
                    "operation": "sales_order_update",
                    "target": {"company_id": COMPANY_ID, "document_id": DOC_ID},
                    "change": {"requestedDeliveryDate": "2026-12-05", "shipToContact": "Dana"},
                    "reason": "Customer asked to move delivery date.",
                },
                {
                    "name": "SO-10421",
                    "handle": f"bc://salesOrders/{DOC_ID}",
                    "condition": {"etag": 'W/"etag-so-1"', "status": "Open"},
                    "values": {"requestedDeliveryDate": "2026-12-01", "shipToContact": "Alex"},
                },
                "class_b",
                {"requestedDeliveryDate": "2026-12-01", "shipToContact": "Alex"},
            ),
            (
                "purchase_order_update",
                {
                    "system": "bc",
                    "operation": "purchase_order_update",
                    "target": {"company_id": COMPANY_ID, "document_id": PO_ID},
                    "change": {"requestedReceiptDate": "2026-12-10"},
                    "reason": "Supplier confirmed revised ETA.",
                },
                {
                    "name": "PO-8812",
                    "handle": f"bc://purchaseOrders/{PO_ID}",
                    "condition": {"etag": 'W/"etag-po-1"', "status": "Open"},
                    "values": {"requestedReceiptDate": "2026-12-02"},
                },
                "class_b",
                {"requestedReceiptDate": "2026-12-02"},
            ),
            (
                "sales_order_line_update",
                {
                    "system": "bc",
                    "operation": "sales_order_line_update",
                    "target": {"company_id": COMPANY_ID, "document_id": DOC_ID, "line_id": LINE_ID},
                    "change": {"quantity": 24, "shipmentDate": "2026-12-06"},
                    "reason": "Customer updated case count.",
                },
                {
                    "name": "SO-10421 line 10000",
                    "condition": {"etag": 'W/"etag-line-1"', "status": "Open"},
                    "values": {"quantity": 12, "shipmentDate": "2026-12-01"},
                },
                "class_b",
                {"quantity": 12, "shipmentDate": "2026-12-01"},
            ),
            (
                "purchase_order_comment",
                {
                    "system": "bc",
                    "operation": "purchase_order_comment",
                    "target": {"company_id": COMPANY_ID, "document_id": PO_ID},
                    "change": {"description": "Deliver to Dock B before 2 PM."},
                    "reason": "Warehouse dock instruction.",
                },
                None,
                "class_a",
                {"description": None},
            ),
            (
                "sales_order_charge_line",
                {
                    "system": "bc",
                    "operation": "sales_order_charge_line",
                    "target": {"company_id": COMPANY_ID, "document_id": DOC_ID},
                    "change": {
                        "lineType": "Account",
                        "lineObjectNumber": "4200",
                        "description": "Expedited freight",
                        "quantity": 1,
                        "unitPrice": "185.00",
                    },
                    "reason": "Add agreed freight charge.",
                },
                None,
                "class_b",
                {"lineType": None, "lineObjectNumber": None, "description": None, "quantity": None, "unitPrice": None},
            ),
        ]
        self.assertEqual(len(cases), 5)
        for op, step, current, expected_class, expected_before in cases:
            with self.subTest(bc_op=op):
                intent = asw.build_write_intent(
                    step,
                    OWNER,
                    **_base_kwargs(read_current=(lambda _p, _a, c=current: c) if current is not None else None),
                )
                self._assert_dry_run_shape(intent, "bc", op)
                self.assertEqual(intent["safety_class"], expected_class)
                self.assertEqual(intent["before"], expected_before)
                self.assertEqual(intent["readiness"]["status"], "ready-live")

    def test_five_calendar_prepared_writes_in_dry_run_with_before_and_after(self):
        cases = [
            (
                "event_create",
                {
                    "system": "calendar",
                    "operation": "event_create",
                    "target": {},
                    "change": {
                        "subject": "Weekly production sync",
                        "start": {"dateTime": "2026-11-26T15:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-11-26T15:30:00", "timeZone": "UTC"},
                        "attendees": [OTHER],
                    },
                    "reason": "Schedule internal sync.",
                },
                None,
                "class_a",
            ),
            (
                "event_create",
                {
                    "system": "calendar",
                    "operation": "event_create",
                    "target": {},
                    "change": {
                        "subject": "Customer quality review",
                        "start": {"dateTime": "2026-11-27T16:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-11-27T17:00:00", "timeZone": "UTC"},
                        "attendees": ["buyer@customer.example"],
                    },
                    "reason": "Review quality report with customer.",
                },
                None,
                "class_b",
            ),
            (
                "event_update",
                {
                    "system": "calendar",
                    "operation": "event_update",
                    "target": {"event_id": "AAMkEvent001"},
                    "change": {"location": "Boardroom B"},
                    "reason": "Move room.",
                },
                {"name": "Weekly production sync", "values": {"location": "Boardroom A"}},
                "class_a",
            ),
            (
                "event_update",
                {
                    "system": "calendar",
                    "operation": "event_update",
                    "target": {"event_id": "AAMkEvent002"},
                    "change": {
                        "start": {"dateTime": "2026-11-26T16:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-11-26T16:45:00", "timeZone": "UTC"},
                    },
                    "reason": "Shift meeting by one hour.",
                },
                {
                    "name": "Warehouse check-in",
                    "values": {
                        "start": {"dateTime": "2026-11-26T15:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-11-26T15:45:00", "timeZone": "UTC"},
                    },
                },
                "class_a",
            ),
            (
                "event_respond",
                {
                    "system": "calendar",
                    "operation": "event_respond",
                    "target": {"event_id": "AAMkEvent003"},
                    "change": {"response": "accept", "comment": "I will attend."},
                    "reason": "Accept internal invitation.",
                },
                {"name": "Packaging review", "values": {"response": "notResponded"}},
                "class_a",
            ),
        ]
        self.assertEqual(len(cases), 5)
        for idx, (op, step, current, expected_class) in enumerate(cases):
            with self.subTest(calendar_case=idx, op=op):
                intent = asw.build_write_intent(
                    step,
                    OWNER,
                    **_base_kwargs(read_current=(lambda _p, _a, c=current: c) if current is not None else None),
                )
                self._assert_dry_run_shape(intent, "calendar", op)
                self.assertEqual(intent["safety_class"], expected_class)
                if current is not None:
                    for key in step["change"]:
                        if key in current["values"]:
                            self.assertEqual(intent["before"][key], current["values"][key])

    def test_five_planner_prepared_writes_in_dry_run_with_before_and_after(self):
        cases = [
            (
                "task_create",
                {
                    "system": "planner",
                    "operation": "task_create",
                    "target": {},
                    "change": {
                        "planId": "PlanId001",
                        "title": "Follow up on CoA",
                        "dueDateTime": "2026-11-28T17:00:00Z",
                    },
                    "reason": "Track CoA follow-up.",
                },
                None,
            ),
            (
                "task_create",
                {
                    "system": "planner",
                    "operation": "task_create",
                    "target": {},
                    "change": {
                        "planId": "PlanId001",
                        "bucketId": "Bucket001",
                        "title": "Confirm pallet spec",
                        "assignees": [ASSIGNEE_ID],
                    },
                    "reason": "Assign pallet confirmation.",
                },
                None,
            ),
            (
                "task_update",
                {
                    "system": "planner",
                    "operation": "task_update",
                    "target": {"task_id": "TaskId001"},
                    "change": {"title": "Confirm Grade A pallet spec"},
                    "reason": "Clarify pallet grade in title.",
                },
                {
                    "name": "Confirm pallet spec",
                    "condition": {"etag": 'W/"etag-pt-1"'},
                    "values": {"title": "Confirm pallet spec"},
                },
            ),
            (
                "task_update",
                {
                    "system": "planner",
                    "operation": "task_update",
                    "target": {"task_id": "TaskId002"},
                    "change": {"percentComplete": 50},
                    "reason": "Mark task in progress.",
                },
                {
                    "name": "Audit batch docs",
                    "condition": {"etag": 'W/"etag-pt-2"'},
                    "values": {"percentComplete": 0},
                },
            ),
            (
                "task_update",
                {
                    "system": "planner",
                    "operation": "task_update",
                    "target": {"task_id": "TaskId003"},
                    "change": {"dueDateTime": "2026-12-03T17:00:00Z", "bucketId": "Bucket002"},
                    "reason": "Move due date and bucket.",
                },
                {
                    "name": "Monthly GMP summary",
                    "condition": {"etag": 'W/"etag-pt-3"'},
                    "values": {"dueDateTime": "2026-11-30T17:00:00Z", "bucketId": "Bucket001"},
                },
            ),
        ]
        self.assertEqual(len(cases), 5)
        for idx, (op, step, current) in enumerate(cases):
            with self.subTest(planner_case=idx, op=op):
                intent = asw.build_write_intent(
                    step,
                    OWNER,
                    **_base_kwargs(read_current=(lambda _p, _a, c=current: c) if current is not None else None),
                )
                self._assert_dry_run_shape(intent, "planner", op)
                self.assertEqual(intent["safety_class"], "class_a")
                if current is not None:
                    self.assertEqual(intent["before"], current["values"])
                    self.assertEqual(intent["after"], step["change"])

    def test_five_sharepoint_prepared_writes_in_dry_run_with_before_and_after(self):
        cases = [
            (
                "list_item_create",
                {
                    "system": "sharepoint",
                    "operation": "list_item_create",
                    "target": {"site_id": SITE_ID, "list_id": LIST_ID},
                    "change": {"Title": "Lot 9021 hold", "Status": "Open"},
                    "reason": "Log lot hold in tracker.",
                },
                None,
                "class_a",
            ),
            (
                "list_item_update",
                {
                    "system": "sharepoint",
                    "operation": "list_item_update",
                    "target": {"site_id": SITE_ID, "list_id": LIST_ID, "item_id": "42"},
                    "change": {"Status": "Released"},
                    "reason": "Update lot status to released.",
                },
                {
                    "name": "Lot 9021 hold",
                    "condition": {"etag": '"sp-etag-1"'},
                    "values": {"Status": "Open"},
                },
                "class_a",
            ),
            (
                "list_item_update",
                {
                    "system": "sharepoint",
                    "operation": "list_item_update",
                    "target": {"site_id": SITE_ID, "list_id": LIST_ID, "item_id": "43"},
                    "change": {"Notes": "Carrier confirmed pickup", "Priority": "High"},
                    "reason": "Record carrier confirmation.",
                },
                {
                    "name": "Shipment 774",
                    "condition": {"etag": '"sp-etag-2"'},
                    "values": {"Notes": "Awaiting pickup", "Priority": "Normal"},
                },
                "class_a",
            ),
            (
                "file_upload",
                {
                    "system": "sharepoint",
                    "operation": "file_upload",
                    "target": {"drive_id": DRIVE_ID, "parent_id": PARENT_ID},
                    "change": {
                        "name": "CoA-Lot-9021.pdf",
                        "content_b64": PDF_B64,
                        "content_type": "application/pdf",
                    },
                    "reason": "File signed CoA in folder.",
                },
                None,
                "class_a",
            ),
            (
                "file_replace",
                {
                    "system": "sharepoint",
                    "operation": "file_replace",
                    "target": {"drive_id": DRIVE_ID, "item_id": FILE_ITEM_ID},
                    "change": {
                        "content_b64": PDF_B64,
                        "content_type": "application/pdf",
                    },
                    "reason": "Replace draft CoA with signed revision.",
                },
                {
                    "name": "CoA-Lot-9021.pdf",
                    "condition": {"etag": '"sp-file-etag-1"'},
                    "values": {"content_b64": OLD_PDF_B64, "content_type": "application/pdf"},
                },
                "class_b",
            ),
        ]
        self.assertEqual(len(cases), 5)
        for idx, (op, step, current, expected_class) in enumerate(cases):
            with self.subTest(sharepoint_case=idx, op=op):
                intent = asw.build_write_intent(
                    step,
                    OWNER,
                    **_base_kwargs(read_current=(lambda _p, _a, c=current: c) if current is not None else None),
                )
                self._assert_dry_run_shape(intent, "sharepoint", op)
                self.assertEqual(intent["safety_class"], expected_class)
                if op in ("file_upload", "file_replace"):
                    self.assertNotIn(PDF_B64, str(intent["after"]))
                    self.assertIn("sha256", str(intent["after"]["content_b64"]))

    def test_five_mission_control_prepared_writes_stay_dry_run_only(self):
        cases = [
            (
                "task_note",
                {
                    "system": "mc",
                    "operation": "task_note",
                    "target": {"task_id": "TASK-1001"},
                    "change": {"notes": "Customer confirmed receipt of revised CoA."},
                    "reason": "Log progress on task.",
                },
                None,
                "class_a",
            ),
            (
                "task_note",
                {
                    "system": "mc",
                    "operation": "task_note",
                    "target": {"task_id": "TASK-1002"},
                    "change": {"notes": "Warehouse switched shipment to Grade A pallets."},
                    "reason": "Record corrective action.",
                },
                None,
                "class_a",
            ),
            (
                "task_update",
                {
                    "system": "mc",
                    "operation": "task_update",
                    "target": {"task_id": "TASK-1003"},
                    "change": {"title": "Investigate damaged gallon cases"},
                    "reason": "Refine task title.",
                },
                {"name": "Damaged cases", "values": {"title": "Damaged cases"}},
                "class_b",
            ),
            (
                "task_update",
                {
                    "system": "mc",
                    "operation": "task_update",
                    "target": {"task_id": "TASK-1004"},
                    "change": {"priority": "high", "addLabels": ["customer-impact"]},
                    "reason": "Escalate task priority.",
                },
                {
                    "name": "Follow up PO 8812",
                    "values": {"priority": "medium", "labels": ["purchasing"]},
                },
                "class_b",
            ),
            (
                "approval_decide",
                {
                    "system": "mc",
                    "operation": "approval_decide",
                    "target": {"task_id": "TASK-1005", "gate_id": "gate_launch_1"},
                    "change": {"decision": "approve"},
                    "reason": "Approve launch gate.",
                },
                {
                    "name": "Launch gate 1",
                    "requested_by": OTHER,
                    "scope": {"kind": "launch", "target": "batch-9021"},
                    "values": {"status": "pending"},
                },
                "class_b",
            ),
        ]
        self.assertEqual(len(cases), 5)
        for idx, (op, step, current, expected_class) in enumerate(cases):
            with self.subTest(mc_case=idx, op=op):
                intent = asw.build_write_intent(
                    step,
                    OWNER,
                    **_base_kwargs(read_current=(lambda _p, _a, c=current: c) if current is not None else None),
                )
                self._assert_dry_run_shape(intent, "mc", op)
                self.assertEqual(intent["safety_class"], expected_class)
                self.assertTrue(intent["readiness"]["dry_run_only"])
                self.assertTrue(intent["confirm_only"])
                self.assertFalse(intent["auto_allowed"])


class SafetyAndFailClosedTests(unittest.TestCase):
    """Enforce 0 automatic on class B, fail-closed permissions/exclusions, and 0 network calls."""

    def test_zero_automatic_on_class_b_and_tamper_resistance(self):
        class_b_steps = [
            (
                {
                    "system": "bc",
                    "operation": "sales_order_delete",
                    "target": {"company_id": COMPANY_ID, "document_id": DOC_ID},
                    "change": {},
                    "reason": "Cancel duplicate open order.",
                },
                {"name": "SO-999", "condition": {"etag": 'W/"e1"', "status": "Open", "shipped": 0},
                 "values": {"number": "SO-999", "status": "Open"}},
            ),
            (
                {
                    "system": "bc",
                    "operation": "sales_invoice_charge_line",
                    "target": {"company_id": COMPANY_ID, "document_id": DOC_ID},
                    "change": {
                        "lineType": "Charge",
                        "lineObjectNumber": "FREIGHT",
                        "description": "Freight surcharge",
                        "quantity": 1,
                        "unitPrice": "250.00",
                    },
                    "reason": "Add freight charge.",
                },
                None,
            ),
            (
                {
                    "system": "mc",
                    "operation": "approval_decide",
                    "target": {"task_id": "TASK-2001", "gate_id": "gate_money_1"},
                    "change": {"decision": "approve"},
                    "reason": "Approve spend gate.",
                },
                {
                    "name": "Spend gate",
                    "requested_by": OTHER,
                    "scope": {"kind": "spend", "usd": 1200},
                    "values": {"status": "pending"},
                },
            ),
            (
                {
                    "system": "calendar",
                    "operation": "event_create",
                    "target": {},
                    "change": {
                        "subject": "External pricing call",
                        "start": {"dateTime": "2026-11-28T15:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-11-28T15:30:00", "timeZone": "UTC"},
                        "attendees": ["director@external.example"],
                    },
                    "reason": "External pricing discussion.",
                },
                None,
            ),
        ]
        for step, current in class_b_steps:
            intent = asw.build_write_intent(
                step,
                OWNER,
                **_base_kwargs(read_current=(lambda _p, _a, c=current: c) if current is not None else None),
            )
            self.assertEqual(intent["safety_class"], "class_b")
            self.assertTrue(intent["confirm_only"])
            self.assertFalse(intent["auto_allowed"])
            with self.assertRaises(pw.Refused) as err:
                asw.enforce_execution_policy(intent, auto=True)
            self.assertEqual(err.exception.status, 403)
            # Even if someone tampers with the intent dict, re-classification still refuses auto!
            tampered = {**intent, "safety_class": "class_a", "confirm_only": False, "auto_allowed": True}
            with self.assertRaises(pw.Refused) as err2:
                asw.enforce_execution_policy(tampered, auto=True)
            self.assertEqual(err2.exception.status, 403)
            # Without confirmed_by, non-auto execution is also refused; with confirmed_by, allowed.
            with self.assertRaises(pw.Refused):
                asw.enforce_execution_policy(intent, auto=False, confirmed_by=None)
            asw.enforce_execution_policy(intent, auto=False, confirmed_by=OWNER_EMAIL)

    def test_fail_closed_permissions_and_unreadable_record_never_expose_before(self):
        step = {
            "system": "bc",
            "operation": "sales_order_update",
            "target": {"company_id": COMPANY_ID, "document_id": DOC_ID},
            "change": {"requestedDeliveryDate": "2026-12-09"},
            "reason": "Move delivery date.",
        }
        read_called = []

        def spy_read(_p, _a):
            read_called.append(True)
            return {
                "name": "SO-SECRET",
                "condition": {"etag": 'W/"e1"', "status": "Open"},
                "values": {"requestedDeliveryDate": "2026-12-01"},
            }

        # 1. Denied permission fails closed BEFORE calling read_current.
        with self.assertRaises(pw.Refused) as denied_err:
            asw.build_write_intent(
                step,
                OWNER,
                **_base_kwargs(may=lambda _p, _a: "no BC write grant", read_current=spy_read),
            )
        self.assertEqual(denied_err.exception.status, 403)
        self.assertEqual(read_called, [])
        self.assertNotIn("2026-12-01", str(denied_err.exception))

        # 2. Missing may check on non-full reader fails closed.
        with self.assertRaises(pw.Refused) as no_may_err:
            asw.build_write_intent(step, OWNER, **_base_kwargs(may=None, read_current=spy_read))
        self.assertEqual(no_may_err.exception.status, 403)
        self.assertEqual(read_called, [])

        # 3. Unreadable / denied target record fails closed.
        for bad_read, expected_status in (
            ({"denied": True, "values": {"requestedDeliveryDate": "2026-12-01"}}, 403),
            ({"unreadable": True, "values": {"requestedDeliveryDate": "2026-12-01"}}, 422),
            (None, 422),
        ):
            with self.assertRaises(pw.Refused) as err:
                asw.build_write_intent(
                    step, OWNER, **_base_kwargs(read_current=lambda _p, _a, r=bad_read: r)
                )
            self.assertEqual(err.exception.status, expected_status)
            self.assertNotIn("2026-12-01", str(err.exception))

        # 4. Door denial and sensitive markings fail closed.
        sensitive_record = {
            "name": "SO-HR",
            "markings": ["payroll"],
            "condition": {"etag": 'W/"e1"', "status": "Open"},
            "values": {"requestedDeliveryDate": "2026-12-01"},
        }
        with self.assertRaises(pw.Refused) as mark_err:
            asw.build_write_intent(
                step, OWNER, **_base_kwargs(read_current=lambda _p, _a: sensitive_record)
            )
        self.assertEqual(mark_err.exception.status, 403)

        with self.assertRaises(pw.Refused) as door_err:
            asw.build_write_intent(
                step,
                OWNER,
                **_base_kwargs(
                    read_current=lambda _p, _a: {**sensitive_record, "markings": []},
                    door=lambda _rec: False,
                ),
            )
        self.assertEqual(door_err.exception.status, 403)

    def test_people_exclusion_and_screen_fail_closed(self):
        step = {
            "system": "calendar",
            "operation": "event_create",
            "target": {},
            "change": {
                "subject": "Internal check-in",
                "start": {"dateTime": "2026-11-26T15:00:00", "timeZone": "UTC"},
                "end": {"dateTime": "2026-11-26T15:30:00", "timeZone": "UTC"},
                "attendees": [OTHER],
            },
            "reason": "Quick sync.",
        }
        # Missing guard (exclusion unavailable) fails closed.
        with self.assertRaises(pw.Refused) as no_guard:
            asw.build_write_intent(step, OWNER, **_base_kwargs(guard=None))
        self.assertEqual(no_guard.exception.status, 403)
        self.assertIn("exclusion_unavailable", str(no_guard.exception))

        # Excluded attendee fails closed.
        excluded_step = {
            **step,
            "change": {**step["change"], "attendees": [HIDDEN]},
        }
        with self.assertRaises(pw.Refused) as excl_err:
            asw.build_write_intent(
                excluded_step,
                OWNER,
                **_base_kwargs(guard=people_guard()),
            )
        self.assertEqual(excl_err.exception.status, 403)

        # Screen hit on proposed payload fails closed.
        with self.assertRaises(pw.Refused) as screen_err:
            asw.build_write_intent(
                step,
                OWNER,
                **_base_kwargs(screen=lambda text: "pay_or_hr" if "check-in" in text else None),
            )
        self.assertEqual(screen_err.exception.status, 403)

    def test_zero_external_http_or_transport_calls_in_dry_run_mode(self):
        adapters = asw.dry_run_adapters(BC_ENV)
        self.assertTrue(adapters)
        for kind, adapter in adapters.items():
            with self.subTest(kind=kind):
                self.assertIsNone(adapter.transport)

        def forbid_network(*_args, **_kwargs):
            raise AssertionError("Network call must never happen during dry-run write preparation")

        with mock.patch.object(urllib.request, "urlopen", side_effect=forbid_network), \
             mock.patch.object(http.client.HTTPConnection, "request", side_effect=forbid_network), \
             mock.patch.object(socket, "create_connection", side_effect=forbid_network):
            intent = asw.build_write_intent(
                {
                    "system": "planner",
                    "operation": "task_create",
                    "target": {},
                    "change": {"planId": "PlanId001", "title": "Dry-run only check"},
                    "reason": "Verify zero side effects.",
                },
                OWNER,
                **_base_kwargs(),
            )
        self.assertEqual(intent["mode"], "dry_run")

    def test_no_em_dashes_in_assist_writes_strings(self):
        source = Path(asw.__file__).read_text(encoding="utf-8")
        self.assertNotIn("\u2014", source)


class PreparedSessionIntegrationTests(unittest.TestCase):
    """End-to-end wiring: assist_prepared.serve -> assist_writes -> assist_worker.outcome."""

    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables
    mail = base.Matters.mail
    read = base.Matters.read
    cycle = base.Matters.cycle

    def test_prepared_session_system_steps_become_dry_run_write_intents_on_matter(self):
        t0 = base.NOW - dt.timedelta(hours=2)
        h = self.mail("t-so", base.OUT, ["ann@ours.example"], at=t0, box=base.ANN)
        self.read(h, thread="t-so", actor=base.ANN, at=t0)
        self.cycle()

        proposal_bc = {
            "tool": "plx.prepare_write",
            "arguments": {
                "system": "bc",
                "operation": "sales_order_update",
                "target": {"company_id": COMPANY_ID, "document_id": DOC_ID},
                "change": {"requestedDeliveryDate": "2026-12-08"},
                "reason": "Customer requested Dec 8 delivery.",
            },
        }
        proposal_bad = {
            "tool": "plx.prepare_write",
            "arguments": {
                "system": "sage",
                "operation": "ar_invoice",
                "target": {},
                "change": {},
                "reason": "Unsupported direct write.",
            },
        }

        def session(_owner, _oid, extra="", as_of="", prepared=None):
            return [{"why": "changes_prepared", "proposals": [proposal_bc, proposal_bad]}]

        out = prep.serve(
            self.pg,
            [base.ANN],
            session,
            lambda ms: {m["matter_id"]: {"text": "Please move SO-10421 to Dec 8", "situation": "s"} for m in ms},
            lambda ms, _gs: {m["matter_id"]: ([], 0) for m in ms},
            screen=lambda _t: None,
            model="stand-in",
            write_context={
                "owner_of": lambda _staff: OWNER,
                "may": lambda _p, _a: None,
                "guard": people_guard(),
                "environ": BC_ENV,
                "readiness_map": READY_MAP,
                "known_domains": COMPANY_DOMAINS,
                "read_current": lambda _p, _a: {
                    "name": "SO-10421",
                    "condition": {"etag": 'W/"etag-so-9"', "status": "Open"},
                    "values": {"requestedDeliveryDate": "2026-12-01"},
                },
            },
        )
        self.assertEqual((out["served"], out.get("drafted")), (1, 1))
        row = self.pg.execute(
            "SELECT plan_status, plan->'prepared'->'write_intents', plan->'prepared'->'write_refusals' "
            "FROM assist.matters WHERE subject_key = 't-so'"
        ).fetchone()
        self.assertEqual(row[0], "drafted")
        intents, refusals = row[1], row[2]
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["system"], "bc")
        self.assertEqual(intents[0]["action"], "sales_order_update")
        self.assertEqual(intents[0]["before"], {"requestedDeliveryDate": "2026-12-01"})
        self.assertEqual(intents[0]["after"], {"requestedDeliveryDate": "2026-12-08"})
        self.assertEqual(intents[0]["safety_class"], "class_b")
        self.assertTrue(intents[0]["confirm_only"])
        self.assertFalse(intents[0]["auto_allowed"])
        self.assertEqual(intents[0]["mode"], "dry_run")
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["system"], "sage")


if __name__ == "__main__":
    unittest.main()
