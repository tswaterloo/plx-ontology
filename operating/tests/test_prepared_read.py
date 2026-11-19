"""A prepared edit's "before" comes from the system (``operating.prepared_read``) through
the desk (``prepared_writes.Desk(read=SystemReader)``): what each system's reader GETs, what
the card shows, what the write then sends (If-Match the etag as read, the undo's prior
values as read) and what fails closed.

SYNTHETIC records; fake GETs and fake write transports only: nothing reaches a network.
Evidence label: injected-transport.
"""
from __future__ import annotations

from datetime import timedelta
import unittest
from urllib.parse import unquote

from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating import prepared_writes as pw
from operating.prepared_read import SystemReader
from operating.tests.test_autopilot_write import (BC_COMPANY, HIDDEN_OID, NOW, OWNER,
                                                  OWNER_OID, Base, Transport)

TENANT = "66666666-6666-4666-8666-666666666666"
ORDER = "77777777-7777-4777-8777-777777777777"
LINE = "88888888-8888-4888-8888-888888888888"
SITE = "plx.sharepoint.com,1a2b3c4d-0000-4000-8000-000000000001," \
       "1a2b3c4d-0000-4000-8000-000000000002"
LIST = "1a2b3c4d-0000-4000-8000-000000000003"
PETRA = "99999999-9999-4999-8999-999999999999"
ME = pw.Person((OWNER, OWNER_OID))
OTHER = pw.Person(("other@plx.example", "55555555-5555-4555-8555-555555555555"))


class FakeGet:
    """GET by URL path suffix -> JSON (or an exception); records every GET."""

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, headers) -> object:
        self.calls.append((unquote(url), dict(headers)))
        for suffix, answer in self.answers.items():
            if unquote(url).split("?", 1)[0].endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise LookupError("no such record")


class Readers(Base):
    def setUp(self) -> None:
        super().setUp()
        self.fake = Transport()
        self.env = {"PLX_AUTOPILOT_BC_COMPANY_ID": BC_COMPANY, "BC_TENANT_ID": TENANT,
                    "PLX_BC_SANDBOX_ENVIRONMENT": "Sandbox2",
                    aw.APPROVED_ENV: "bc_write,planner_write,calendar_write,"
                                     "sharepoint_write"}
        self.graph, self.calendar, self.bc_get = FakeGet({}), FakeGet({}), FakeGet({})
        self.gates: dict = {}
        self.rows: dict = {}
        self.replica_reads: list = []
        self.probe_bc(self.env, {BC_COMPANY: "PLX"})

    def desk(self, may=None) -> pw.Desk:
        env = self.env

        def executor() -> aw.Executor:
            return self.executor(env, planner_write=wa.PlannerAdapter(self.fake, environ=env),
                                 calendar_write=wa.CalendarAdapter(self.fake, environ=env),
                                 sharepoint_write=wa.SharePointAdapter(self.fake, environ=env))
        reader = SystemReader(env, executor().adapters, graph=self.graph,
                              calendar=self.calendar, bc=self.bc_get,
                              mc=lambda task: self.gates[task],
                              portal=self.replica)
        return pw.Desk(self.state, executor, may=may or (lambda p, a: None), read=reader)

    def confirm(self, desk: pw.Desk, card: dict) -> dict:
        shown = desk.preview(ME, card["prepared_id"], now=NOW)
        return desk.confirm(ME, card["prepared_id"], preview_hash=shown["preview_hash"],
                            confirm=True, now=NOW)

    # ---- BC ---------------------------------------------------------------------------

    def order(self, **over) -> dict:
        return {"id": ORDER, "number": "SO-1001", "customerName": "Acme Foods",
                "status": "Open", "externalDocumentNumber": "PO-77",
                "requestedDeliveryDate": "2026-11-20", "@odata.etag": 'W/"bc1"', **over}

    def bc_update(self, **over) -> dict:
        return {"system": "bc", "operation": "sales_order_update",
                "target": {"company_name": "PLX", "document_id": ORDER},
                "change": {"requestedDeliveryDate": "2026-11-27"},
                "reason": "The customer moved the delivery.", **over}

    def test_bc_order_edit_reads_the_sandbox_record_and_writes_against_it(self):
        self.bc_get.answers = {f"salesOrders({ORDER})": self.order()}
        desk = self.desk()
        card = desk.prepare(ME, self.bc_update(), now=NOW)
        # one GET of the order in the environment and company the write goes to
        self.assertEqual(len(self.bc_get.calls), 1)
        self.assertIn(f"/v2.0/{TENANT}/Sandbox2/api/v2.0/companies({BC_COMPANY})/salesOrders("
                      f"{ORDER})", self.bc_get.calls[0][0])
        self.assertEqual(card["diff"], [{"field": "requestedDeliveryDate",
                                         "before": "2026-11-20", "after": "2026-11-27"}])
        self.assertEqual(card["actions"][0]["record"], "Sales order SO-1001 (Acme Foods)")
        self.assertEqual(card["actions"][0]["condition"], {"etag": 'W/"bc1"', "status": "Open"})
        self.fake.answers = []
        self.bc.answers = [(200, {"id": ORDER, "@odata.etag": 'W/"bc2"'})]
        done = self.confirm(desk, card)
        self.assertEqual([o["status"] for o in done["outcome"]], ["done"], done["outcome"])
        sent = self.bc.calls[-1][0]
        self.assertEqual((sent["method"], sent["headers"]["If-Match"]), ("PATCH", 'W/"bc1"'))
        self.assertEqual(sent["body"], {"requestedDeliveryDate": "2026-11-27"})
        # the undo patches back the value BC held, with the etag BC answered
        self.bc.answers = [(200, {"id": ORDER})]
        desk.undo(ME, card["prepared_id"], now=NOW)
        back = self.bc.calls[-1][0]
        self.assertEqual((back["body"], back["headers"]["If-Match"]),
                         ({"requestedDeliveryDate": "2026-11-20"}, 'W/"bc2"'))

    def test_bc_record_that_moved_since_ask_read_it_or_is_closed_is_refused(self):
        self.bc_get.answers = {f"salesOrders({ORDER})": self.order()}
        cases = {
            "stale etag": (self.bc_update(precondition={"etag": 'W/"old"'}), "newer version"),
            "other value": (self.bc_update(prior={"requestedDeliveryDate": "2026-11-18"}),
                            "other values now"),
        }
        for name, (request, words) in cases.items():
            with self.subTest(name), self.assertRaises(pw.Refused) as refused:
                self.desk().prepare(ME, request, now=NOW)
            self.assertIn(words, " ".join(refused.exception.problems))
        self.bc_get.answers = {f"salesOrders({ORDER})": self.order(status="Released")}
        with self.assertRaises(pw.Refused) as refused:
            self.desk().prepare(ME, self.bc_update(), now=NOW)
        self.assertIn("document open", " ".join(refused.exception.problems))

    def test_a_bc_company_named_by_name_is_the_company_read_shown_and_written(self):
        # two companies configured and found by the probe: the name picks the id, the card
        # shows that id and BC's name, and the GET and the write go to that company only
        self.env["PLX_AUTOPILOT_BC_COMPANY_IDS"] = PETRA
        self.probe_bc(self.env, {BC_COMPANY: "PLX", PETRA: "Petra Soap"})
        self.bc_get.answers = {f"salesOrders({ORDER})": self.order()}
        desk = self.desk()
        card = desk.prepare(ME, self.bc_update(target={"company_name": "petra  soap",
                                                       "document_id": ORDER}), now=NOW)
        shown = card["actions"][0]
        self.assertEqual(shown["company"], {"id": PETRA, "name": "Petra Soap"})
        self.assertEqual(shown["target"], {"company_id": PETRA, "company_name": "Petra Soap",
                                           "document_id": ORDER})
        self.assertIn(f"/companies({PETRA})/salesOrders({ORDER})", self.bc_get.calls[-1][0])
        self.bc.answers = [(200, {"id": ORDER, "@odata.etag": 'W/"bc2"'})]
        self.confirm(desk, card)
        self.assertIn(f"/companies({PETRA})/salesOrders({ORDER})", self.bc.calls[-1][0]["url"])
        # a new order named for the default company by its name goes to the default
        create = {"system": "bc", "operation": "sales_order_create",
                  "target": {"company_name": "PLX"},
                  "change": {"customerNumber": "C-1", "externalDocumentNumber": "P-1",
                             "orderDate": "2026-11-12",
                             "salesOrderLines": [{"lineType": "Item",
                                                  "lineObjectNumber": "I-1", "quantity": 1}]},
                  "precondition": {"party": "C-1"}, "reason": "Enter the PO."}
        card = desk.prepare(ME, create, now=NOW)
        self.assertEqual(card["actions"][0]["company"], {"id": BC_COMPANY, "name": "PLX"})
        self.bc.answers = [(201, {"id": ORDER, "@odata.etag": 'W/"n1"'})]
        self.confirm(desk, card)
        self.assertIn(f"/companies({BC_COMPANY})/salesOrders", self.bc.calls[-1][0]["url"])
        # never the default for a name it does not know, a company not configured, an id
        # and a name of two companies, or a name with no probe to match it against
        cases = {
            "made-up name": ({"company_name": "Totally Made Up Inc"}, "no company"),
            "unconfigured id": ({"company_id": "12121212-1212-4121-8121-121212121212"},
                                "not one the Workbench writes"),
            "id and other name": ({"company_id": BC_COMPANY, "company_name": "Petra Soap"},
                                  "do not name the same company"),
            "unused target key": ({"company_id": BC_COMPANY, "company": "Petra Soap"},
                                  "does not use"),
        }
        for name, (target, words) in cases.items():
            with self.subTest(name), self.assertRaises(pw.Refused) as refused:
                desk.prepare(ME, {**create, "target": target}, now=NOW)
            self.assertIn(words, " ".join(refused.exception.problems))
        with self.assertRaises(pw.Refused) as refused:
            desk.prepare(ME, create, now=NOW + timedelta(hours=1))
        self.assertIn("have not been checked", " ".join(refused.exception.problems))
        # a card whose company the host no longer writes is not confirmed
        card = desk.prepare(ME, {**create, "target": {"company_name": "Petra Soap"}}, now=NOW)
        self.env["PLX_AUTOPILOT_BC_COMPANY_IDS"] = ""
        calls = len(self.bc.calls)
        with self.assertRaises(pw.Refused):
            self.confirm(desk, card)
        self.assertEqual(len(self.bc.calls), calls)

    def test_bc_line_edit_and_order_delete_read_the_lines(self):
        line = {"id": LINE, "description": "Lavender bar 4oz", "quantity": 10,
                "shippedQuantity": 0, "@odata.etag": 'W/"l1"'}
        lines = f"salesOrders({ORDER})/salesOrderLines"
        self.bc_get.answers = {f"salesOrders({ORDER})": self.order(),
                               lines: {"value": [line]}}
        card = self.desk().prepare(ME, {
            "system": "bc", "operation": "sales_order_line_update",
            "target": {"company_name": "PLX", "document_id": ORDER, "line_id": LINE},
            "change": {"quantity": 12}, "reason": "They want two more."}, now=NOW)
        self.assertEqual(card["diff"], [{"field": "quantity", "before": 10, "after": 12}])
        self.assertEqual(card["actions"][0]["condition"], {"etag": 'W/"l1"', "status": "Open"})
        delete = {"system": "bc", "operation": "sales_order_delete",
                  "target": {"company_name": "PLX", "document_id": ORDER}, "change": {},
                  "reason": "Cancelled by the customer."}
        self.assertEqual(self.desk().prepare(ME, delete, now=NOW)["actions"][0]["condition"],
                         {"etag": 'W/"bc1"', "status": "Open", "shipped": 0})
        # something shipped (as BC holds it, whatever Ask said): refused
        self.bc_get.answers[lines] = {"value": [{**line, "shippedQuantity": 2}]}
        with self.assertRaises(pw.Refused) as refused:
            self.desk().prepare(ME, {**delete, "precondition": {"shipped": 0}}, now=NOW)
        self.assertIn("shipped", " ".join(refused.exception.problems))
        # a line that is not on the order: not read, refused
        self.bc_get.answers[lines] = {"value": []}
        with self.assertRaises(pw.Refused) as refused:
            self.desk().prepare(ME, {
                "system": "bc", "operation": "sales_order_line_update",
                "target": {"company_name": "PLX", "document_id": ORDER, "line_id": LINE},
                "change": {"quantity": 12}, "reason": "x"}, now=NOW)
        self.assertIn("cannot be read from bc", " ".join(refused.exception.problems))

    # ---- Graph ------------------------------------------------------------------------

    def test_planner_task_edit_reads_the_task_and_clears_its_people(self):
        task = {"id": "task-0001", "title": "Chase the carrier", "percentComplete": 0,
                "assignments": {OWNER_OID: {}}, "@odata.etag": 'W/"p1"'}
        self.calendar.answers = {"/planner/tasks/task-0001": task}
        update = {"system": "planner", "operation": "task_update",
                  "target": {"task_id": "task-0001"}, "change": {"percentComplete": 100},
                  "reason": "Done."}
        card = self.desk().prepare(ME, update, now=NOW)
        self.assertEqual(card["diff"], [{"field": "percentComplete", "before": 0,
                                         "after": 100}])
        self.assertEqual((card["actions"][0]["record"], card["actions"][0]["condition"]),
                         ("Chase the carrier", {"etag": 'W/"p1"'}))
        # read on the meetings app the Planner capture uses, never the mail app (403 live)
        self.assertEqual(self.graph.calls, [])
        # an edit to the value the task already holds changes nothing: refused (live walk)
        with self.assertRaises(pw.Refused) as refused:
            self.desk().prepare(ME, {**update, "change": {"percentComplete": 0}}, now=NOW)
        self.assertIn("nothing to change", " ".join(refused.exception.problems))
        # a task of a person the Workbench keeps out: refused, nothing of it shown
        self.calendar.answers["/planner/tasks/task-0001"] = {
            **task, "assignments": {HIDDEN_OID: {}}}
        with self.assertRaises(pw.Refused) as refused:
            self.desk().prepare(ME, update, now=NOW)
        self.assertIn("keeps out", " ".join(refused.exception.problems))
        self.assertNotIn("Chase", " ".join(refused.exception.problems))

    def test_own_calendar_event_is_read_by_the_owners_id_in_utc(self):
        self.calendar.answers = {"/events/evt-1": {
            "subject": "Supplier call", "start": {"dateTime": "2026-11-19T15:00:00.0000000",
                                                  "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-19T15:30:00.0000000", "timeZone": "UTC"},
            "organizer": {"emailAddress": {"address": OWNER}}, "attendees": []}}
        card = self.desk().prepare(ME, {
            "system": "calendar", "operation": "event_update", "target": {"event_id": "evt-1"},
            "change": {"start": {"dateTime": "2026-11-19T11:00:00",
                                 "timeZone": "America/New_York"}},
            # Ask's value in Eastern is the same instant as the system's UTC one
            "prior": {"start": {"dateTime": "2026-11-19T10:00:00",
                                "timeZone": "America/New_York"}},
            "reason": "Move it an hour."}, now=NOW)
        url, headers = self.calendar.calls[0]
        self.assertIn(f"/users/{OWNER_OID}/events/evt-1", url)
        self.assertIn('outlook.timezone="UTC"', headers["Prefer"])
        self.assertEqual(card["actions"][0]["record"], "Supplier call")

    def test_sharepoint_list_item_reads_its_columns(self):
        self.graph.answers = {f"/lists/{LIST}/items/12/fields": {
            "Title": "Lot 4471 hold", "Status": "Open", "@odata.etag": '"i1,3"'}}
        card = self.desk().prepare(ME, {
            "system": "sharepoint", "operation": "list_item_update",
            "target": {"site_id": SITE, "list_id": LIST, "item_id": "12"},
            "change": {"Status": "Released", "Notes": "QA passed"}, "reason": "QA passed."},
            now=NOW)
        self.assertEqual(card["diff"], [
            {"field": "Status", "before": "Open", "after": "Released"},
            {"field": "Notes", "before": None, "after": "QA passed"}])
        self.assertEqual(card["actions"][0]["condition"], {"etag": '"i1,3"'})

    # ---- Mission Control --------------------------------------------------------------

    def test_mc_gate_requester_and_state_come_from_mission_control(self):
        gate = {"id": "apg_1", "status": "pending", "requestedBy": OWNER.upper(),
                "reason": "Launch the label run", "scope": {"kind": "launch"}}
        self.gates = {"TASK-1": {"task": {"title": "Label run", "approvalGates": [gate]}}}
        request = {"system": "mc", "operation": "approval_decide",
                   "target": {"task_id": "TASK-1", "gate_id": "apg_1"},
                   "change": {"decision": "approve", "scope": {"kind": "launch"}},
                   "reason": "Routine."}
        seen = []
        card = self.desk(may=lambda p, a: seen.append(a.get("people")) or None).prepare(
            ME, request, now=NOW)
        self.assertEqual(card["actions"][0]["people"], [OWNER])
        self.assertEqual(card["actions"][0]["record"], "Label run: Launch the label run")
        self.assertIn([OWNER], seen)   # the own-request check sees MC's requester
        # the route sends an approval only: a card saying another decision is refused
        with self.assertRaises(pw.Refused) as refused:
            self.desk().prepare(ME, {**request, "change": {**request["change"],
                                                           "decision": "reject"}}, now=NOW)
        self.assertIn("only an approval is sent", " ".join(refused.exception.problems))
        self.gates["TASK-1"]["task"]["approvalGates"] = [{**gate, "status": "approved"}]
        with self.assertRaises(pw.Refused) as refused:
            self.desk().prepare(ME, request, now=NOW)
        self.assertIn("approved in Mission Control now", " ".join(refused.exception.problems))

    # ---- Portal ---------------------------------------------------------------------------

    def replica(self, table, record, columns):
        self.replica_reads.append((table, record, tuple(columns)))
        row = self.rows.get((table, record))
        return {c: row.get(c) for c in columns} if row else None

    def test_portal_order_edit_reads_the_replica_and_goes_to_the_staff_route(self):
        from datetime import datetime, timezone
        seen = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)
        self.rows[("app_sales_order", "so_9")] = {
            "id": "so_9", "order_number": "SO-2026-0042", "po_reference": "PO-1",
            "required_date": datetime(2026, 11, 20, tzinfo=timezone.utc),
            "updated_at": seen}
        portal = wa.PortalAdapter(self.fake, environ={})
        self.env[aw.APPROVED_ENV] += ",portal_write"
        desk = pw.Desk(self.state, lambda: self.executor(self.env, portal_write=portal),
                       may=lambda p, a: None, read=SystemReader(
                           self.env, {"portal_write": portal}, portal=self.replica))
        card = desk.prepare(ME, {"system": "portal", "operation": "sales-order.update",
                                 "target": {"id": "so_9"},
                                 "change": {"poReference": "PO-2", "requiredDate": "2026-11-27"},
                                 "reason": "The customer sent a new PO."}, now=NOW)
        self.assertEqual(self.replica_reads[-1][:2], ("app_sales_order", "so_9"))
        self.assertEqual(card["actions"][0]["record"], "Sales order SO-2026-0042")
        self.assertEqual(card["actions"][0]["condition"], {"updated_at": seen.isoformat()})
        self.assertEqual(card["diff"], [
            {"field": "poReference", "before": "PO-1", "after": "PO-2"},
            {"field": "requiredDate", "before": "2026-11-20", "after": "2026-11-27"}])
        self.fake.answers = [(200, {"id": "so_9"})]
        done = self.confirm(desk, card)
        self.assertEqual([o["status"] for o in done["outcome"]], ["done"], done["outcome"])
        sent = self.fake.calls[-1][0]
        self.assertEqual((sent["method"], sent["url"]), ("PATCH", "/api/mrp/sales-orders/so_9"))
        self.assertEqual(sent["body"], {"poReference": "PO-2", "requiredDate": "2026-11-27",
                                        "expectedUpdatedAt": seen.isoformat()})
        # the undo puts the values as read back
        self.fake.answers = [(200, {"id": "so_9"})]
        desk.undo(ME, card["prepared_id"], now=NOW)
        self.assertEqual(self.fake.calls[-1][0]["body"],
                         {"poReference": "PO-1", "requiredDate": "2026-11-20"})
        # no such row, or no replica configured: the edit is refused, never shown blind
        for reader in (SystemReader(self.env, {"portal_write": portal}, portal=self.replica),
                       SystemReader({}, {"portal_write": portal})):
            with self.subTest(), self.assertRaises(pw.Refused) as refused:
                pw.Desk(self.state, lambda: self.executor(self.env, portal_write=portal),
                        may=lambda p, a: None, read=reader).prepare(
                    ME, {"system": "portal", "operation": "sales-order.update",
                         "target": {"id": "so_404" if reader._portal else "so_9"},
                         "change": {"notes": "x"}, "reason": "r"}, now=NOW)
            self.assertIn("cannot be read from portal", " ".join(refused.exception.problems))

    # ---- fail closed, cardinality -------------------------------------------------------

    def test_unreadable_unknown_and_not_permitted_fail_closed_without_a_read(self):
        self.calendar.answers = {"/planner/tasks/task-0001": TimeoutError("slow")}
        update = {"system": "planner", "operation": "task_update",
                  "target": {"task_id": "task-0001"}, "change": {"title": "New"},
                  "reason": "Rename."}
        with self.assertRaises(pw.Refused) as refused:
            self.desk().prepare(ME, update, now=NOW)
        self.assertIn("cannot be read from planner", " ".join(refused.exception.problems))
        # a person who could not make the change: refused before anything is read
        self.calendar.calls.clear()
        with self.assertRaises(pw.Refused):
            self.desk(may=lambda p, a: "no grant").prepare(OTHER, update, now=NOW)
        self.assertEqual(self.calendar.calls, [])
        # a system with no reader (the Portal edit): never shown against Ask's word
        with self.assertRaises(pw.Refused):
            self.desk().prepare(ME, {"system": "portal", "operation": "order_update",
                                     "target": {"order_id": "1"}, "change": {"notes": "x"},
                                     "prior": {"notes": "y"}, "reason": "x"}, now=NOW)

    def test_one_read_per_record_at_1_10_and_40_prepares(self):
        for n in (1, 10, 40):
            with self.subTest(n=n):
                self.tearDown()
                self.setUp()
                self.calendar.answers = {f"/planner/tasks/t-{i}": {
                    "id": f"t-{i}", "title": f"Task {i}", "percentComplete": 0,
                    "@odata.etag": f'W/"{i}"'} for i in range(n)}
                desk = self.desk()
                cards = [desk.prepare(ME, {
                    "system": "planner", "operation": "task_update",
                    "target": {"task_id": f"t-{i}"}, "change": {"percentComplete": 50},
                    "reason": "Half done."}, now=NOW) for i in range(n)]
                self.assertEqual(len(self.calendar.calls), n)
                self.assertEqual(len({c["prepared_id"] for c in cards}), n)
                self.assertEqual(sorted(c["actions"][0]["record"] for c in cards),
                                 sorted(f"Task {i}" for i in range(n)))



class UndoStore(Base):
    def test_prior_bytes_are_kept_30_days_then_pruned(self):
        from datetime import timedelta
        from operating.card_actions import Journal
        ex = self.executor({})
        old, young = ex._undo_blob("QUJD"), ex._undo_blob("REVG")
        with Journal(self.state / aw.UNDO_FILE).locked() as kept:
            kept.append({"key": "k_old", "inverse": {"body_b64": old},
                         "at": aw._iso(NOW - aw.UNDO_KEEP - timedelta(hours=1))})
            kept.append({"key": "k_young", "inverse": {"body_b64": young}, "at": aw._iso(NOW)})
        self.assertEqual(ex.prune_undo(NOW), 1)
        names = {p.name for p in (self.state / aw.UNDO_BLOBS).iterdir()}
        self.assertEqual(names, {young.rsplit(":", 1)[1]})
        self.assertEqual(ex.prune_undo(NOW, undone="k_young"), 1)
        self.assertEqual(list((self.state / aw.UNDO_BLOBS).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
