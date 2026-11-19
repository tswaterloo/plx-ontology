"""The general prepared-writes desk (``operating.prepared_writes``) Ask and Autopilot share:
prepare -> preview -> two-step confirm -> the one executor as the person -> undo; who may
prepare and confirm; what fails closed.

SYNTHETIC people, plans and companies; fake transports only: nothing here reaches a
network, and no test confirms a live send. Evidence label: injected-transport.
"""
from __future__ import annotations

import json
import unittest

from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating import prepared_writes as pw
from operating.tests.test_autopilot_write import (BC_COMPANY, HIDDEN, NOW, OWNER, OWNER_OID,
                                                  Base, Transport, guard)

PLAN = "plan-0001"
OTHER = pw.Person(("other@plx.example", "55555555-5555-4555-8555-555555555555"))
ME = pw.Person((OWNER, OWNER_OID))


def planner(title: str = "Chase the carrier for the late pallet") -> dict:
    return {"system": "planner", "operation": "task_create",
            "target": {}, "change": {"planId": PLAN, "title": title},
            "reason": "You asked for a task to chase it."}


class Desk(Base):
    def setUp(self) -> None:
        super().setUp()
        self.fake = Transport()
        self.checked: list[str] = []

    def desk(self, env: dict | None = None, *, may="ok", g=None, read=None) -> pw.Desk:
        env = {"PLX_AUTOPILOT_BC_COMPANY_ID": BC_COMPANY, **(env or {})}

        def executor() -> aw.Executor:
            return self.executor(env, g=g, planner_write=wa.PlannerAdapter(self.fake, environ=env),
                                 send_mail=wa.MailAdapter(environ=env))

        def allowed(person, action):
            self.checked.append(str(action.get("kind")))
            return None if may == "ok" else may
        return pw.Desk(self.state, executor, may=allowed if may is not None else None,
                       read=read)

    def test_prepare_preview_two_step_confirm_runs_once_as_the_person_then_undo(self):
        desk = self.desk({aw.APPROVED_ENV: "planner_write"})
        card = desk.prepare(ME, planner(), now=NOW)
        # preparing writes nothing, and the same request prepares the same item
        self.assertEqual((card["state"], self.fake.calls), ("prepared", []))
        self.assertEqual(desk.prepare(ME, planner(), now=NOW)["prepared_id"], card["prepared_id"])
        self.assertEqual(card["diff"], [{"field": "planId", "before": None, "after": PLAN},
                                        {"field": "title", "before": None,
                                         "after": "Chase the carrier for the late pallet"}])
        shown = desk.preview(ME, card["prepared_id"], now=NOW)
        self.assertTrue(shown["live"], shown["missing"])
        pid = card["prepared_id"]
        # step two needs the explicit confirm and the preview as it still is
        for kw in ({"preview_hash": shown["preview_hash"], "confirm": False},
                   {"preview_hash": "0" * 64, "confirm": True}):
            with self.assertRaises(pw.Refused) as refused:
                desk.confirm(ME, pid, now=NOW, **kw)
            self.assertEqual(refused.exception.status, 409)
        self.assertEqual(self.fake.calls, [])
        self.fake.answers = [(201, {"id": "pt_1", "@odata.etag": 'W/"e2"'})]
        done = desk.confirm(ME, pid, preview_hash=shown["preview_hash"], confirm=True, now=NOW)
        self.assertEqual(done["state"], "confirmed")
        self.assertEqual([(o["status"], o["mode"]) for o in done["outcome"]], [("done", "live")])
        self.assertEqual(len(self.fake.calls), 1)
        audit = [r for r in aw.Journal(self.state / aw.AUDIT_FILE).rows()
                 if r.get("event") == "outcome"]
        self.assertEqual((audit[-1]["approved_by"], audit[-1]["origin"]), (OWNER, "instruction"))
        # a second confirm (a double click) never runs it again
        again = desk.confirm(ME, pid, preview_hash=shown["preview_hash"], confirm=True, now=NOW)
        self.assertEqual((again["state"], len(self.fake.calls)), ("confirmed", 1))
        self.fake.answers = [(204, {})]
        undone = desk.undo(ME, pid, now=NOW)
        self.assertEqual((undone["state"], self.fake.calls[-1][0]["method"]), ("undone", "DELETE"))
        with self.assertRaises(pw.Refused):
            desk.undo(ME, pid, now=NOW)

    def test_not_opened_kind_is_shown_and_runs_as_a_dry_run_only(self):
        desk = self.desk({})
        pid = desk.prepare(ME, planner(), now=NOW)["prepared_id"]
        shown = desk.preview(ME, pid, now=NOW)
        self.assertFalse(shown["live"])
        self.assertTrue(any("PLX_APPROVED_WRITES" in m for m in shown["missing"]))
        done = desk.confirm(ME, pid, preview_hash=shown["preview_hash"], confirm=True, now=NOW)
        self.assertEqual([o["status"] for o in done["outcome"]], ["dry_run"])
        self.assertEqual(self.fake.calls, [])

    def test_a_person_prepares_and_confirms_only_what_they_could_do_themselves(self):
        # another person never sees, confirms, dismisses or undoes someone's prepared write
        desk = self.desk({aw.APPROVED_ENV: "planner_write"})
        pid = desk.prepare(ME, planner(), now=NOW)["prepared_id"]
        for step in (lambda: desk.preview(OTHER, pid), lambda: desk.dismiss(OTHER, pid),
                     lambda: desk.confirm(OTHER, pid, preview_hash="x", confirm=True),
                     lambda: desk.undo(OTHER, pid)):
            with self.assertRaises(pw.Refused) as refused:
                step()
            self.assertEqual(refused.exception.status, 404)
        self.assertEqual(desk.mine(OTHER, now=NOW), [])
        self.assertEqual([c["prepared_id"] for c in desk.mine(ME, now=NOW)], [pid])
        # the person's own grant decides: refused by the check, or no check at all
        for may in ("you are not a member of that plan", None):
            with self.subTest(may=may), self.assertRaises(pw.Refused) as refused:
                self.desk(may=may).prepare(ME, planner("Another"), now=NOW)
            self.assertIn("cannot make this planner change yourself",
                          " ".join(refused.exception.problems))
        # a full reader skips the grant check only; their own mailbox needs none
        full = pw.Person(ME.ids, full=True)
        self.checked.clear()
        self.assertEqual(self.desk(may="no").prepare(full, planner("Full"), now=NOW)["state"],
                         "prepared")
        mail = {"system": "mail", "operation": "send_email", "target": {},
                "change": {"to": ["buyer@customer.example"], "subject": "PO 7",
                           "body": "Confirmed."}, "reason": "Reply to the buyer."}
        self.assertEqual(self.desk(may=None).prepare(ME, mail, now=NOW)["actions"][0]["kind"],
                         "send_mail")
        self.assertEqual(self.checked, [])

    def test_unknown_excluded_freightcom_read_only_and_bc_company_fail_closed(self):
        desk = self.desk({aw.APPROVED_ENV: "planner_write,bc_write"})
        bc = {"system": "bc", "operation": "sales_order_create",
              "target": {"company_name": "PHSI Group"},
              "change": {"customerNumber": "C-1", "externalDocumentNumber": "P-1",
                         "orderDate": "2026-11-12",
                         "salesOrderLines": [{"lineType": "Item", "lineObjectNumber": "I-1",
                                              "quantity": 1}]},
              "precondition": {"party": "C-1"}, "reason": "Enter the PO."}
        cases = {
            "unknown system": ({**planner(), "system": "erp9"}, "No write path"),
            "unknown operation": ({**planner(), "operation": "task_delete_all"},
                                  "unknown operation"),
            "bad value": ({**planner(), "change": {"planId": PLAN, "title": ""}}, "title"),
            "filemaker": ({**planner(), "system": "filemaker"}, "read-only"),
            "sage": ({**planner(), "system": "sage"}, "no write path"),
            "freightcom": (planner("Book the Freightcom pickup"), "carrier is never acted on"),
            "excluded person": ({"system": "mail", "operation": "send_email", "target": {},
                                 "change": {"to": [HIDDEN], "subject": "Hi", "body": "x"},
                                 "reason": "r"}, "keeps out"),
            "no company named": ({**bc, "target": {}}, "Name the BC company"),
            # a target key the write never reads would show on the card and not be written
            "plan named, another posted": ({**planner(), "target": {"plan_id": "plan-0002"}},
                                           "does not use"),
            "file named, another written": ({"system": "sharepoint",
                                             "operation": "list_item_create",
                                             "target": {"site_id": "x", "list_id": "y",
                                                        "site_name": "Quality"},
                                             "change": {"Title": "t"}, "reason": "r"},
                                            "does not use"),
        }
        for name, (request, words) in cases.items():
            with self.subTest(name), self.assertRaises(pw.Refused) as refused:
                desk.prepare(ME, request, now=NOW)
            self.assertIn(words.lower(), " ".join(refused.exception.problems).lower())
        with self.assertRaises(pw.Refused) as refused:
            self.desk(g=guard(unavailable=True)).prepare(ME, planner(), now=NOW)
        self.assertIn("not loaded", " ".join(refused.exception.problems))
        self.assertEqual([r for r in desk.journal.rows() if r.get("event") == "prepared"], [])
        self.assertEqual(self.fake.calls, [])

    def test_cardinality_rate_limit_and_the_models_tool(self):
        desk = self.desk({})
        for n in (1, 10, 60):
            with self.subTest(n=n):
                made = set()
                for i in range(n):
                    try:
                        made.add(desk.prepare(ME, planner(f"Task {n}-{i}"), now=NOW)["prepared_id"])
                    except pw.Refused as refused:
                        self.assertEqual(refused.status, 429)
                        break
                listed = [c["prepared_id"] for c in desk.mine(ME, now=NOW)]
                self.assertEqual(len(listed), len(set(listed)))
                self.assertTrue(made <= set(listed))
        self.assertEqual(len(desk.mine(ME, now=NOW)), pw.PREPARE_PER_HOUR)
        # the model's tool prepares (never writes) and says refusals in words
        tool = pw.tool(self.desk({}), ME)
        self.assertEqual(tool.spec["name"], pw.TOOL_NAME)
        state = self.state
        self.state = state.parent / "other-state"
        self.state.mkdir()
        tool = pw.tool(self.desk({aw.APPROVED_ENV: "planner_write"}), ME)
        got = tool.call({**planner("From Ask"), "citations": ["graph-mail://x/m1@1"]})
        self.assertTrue(got["prepared_id"].startswith("pw_"))
        self.assertIn("not done", got["note"])
        self.assertEqual(self.fake.calls, [])
        self.assertIn("refused", tool.call({**planner(), "confirm": True}))
        self.assertIn("refused", tool.call({**planner(), "system": "filemaker"}))
        self.assertNotIn("Freightcom", json.dumps(tool.call(planner("Freightcom quote"))))
        # the model's words for a field ("due", "expected date") are refused naming the
        # system's own fields, and the tool lists every operation's fields up front
        wrong = {**planner("Due Friday")}
        wrong["change"] = {**wrong["change"], "due_at": "2026-11-20"}
        refused = " ".join(tool.call(wrong)["refused"])
        self.assertIn("cannot set due_at", refused)
        self.assertIn("dueDateTime", refused)
        described = tool.spec["description"]
        for line in ("planner task_create: target -; change planId, bucketId, title, "
                     "dueDateTime", "bc purchase_order_update: target company_name, "
                     "document_id; change requestedReceiptDate",
                     "portal purchase-order.update: target id; change expectedDate"):
            self.assertIn(line, described)

    # ---- the card shows exactly what runs (review 58aa613cd6) ---------------------------

    def assert_card_shows_what_runs(self, desk: pw.Desk, card: dict) -> None:
        """Every target id and every payload value of every action the executor would run
        from the stored item appears on the card, and nothing runs that is not on it."""
        first, _rows = desk._own(ME, card["prepared_id"])
        actions = aw.actions_of(desk._unstash(first["item"]))
        self.assertEqual(len(actions), len(card["actions"]))
        shown = json.dumps(card["actions"], default=str)
        for action, on_card in zip(actions, card["actions"]):
            self.assertLessEqual(set(action), pw.ACTION_KEYS)
            self.assertEqual((on_card["system"], on_card["operation"]),
                             (action["system"], action["operation"]))
            for part in ("target", "payload"):
                for value in pw._strings(pw._plain(action.get(part) or {})):
                    self.assertIn(json.dumps(value)[1:-1], shown, (part, value))

    def test_a_message_is_shown_whole_and_nothing_outside_change_is_sent(self):
        desk = self.desk({})
        # Recipients and body in target, only the subject in change
        hidden = {"system": "mail", "operation": "send_email",
                  "target": {"to": ["buyer@customer.example"], "body": "SECRET BODY NOT SHOWN"},
                  "change": {"subject": "PO 7"}, "reason": "r"}
        teams = {"system": "teams", "operation": "chat_message",
                 "target": {"chat_id": "19:abc@thread.v2", "body": "unseen"},
                 "change": {"body": "Seen"}, "reason": "r"}
        for request in (hidden, teams):
            with self.subTest(request["system"]), self.assertRaises(pw.Refused) as refused:
                desk.prepare(ME, request, now=NOW)
            self.assertIn("card shows everything", " ".join(refused.exception.problems))
        mail = {"system": "mail", "operation": "send_email", "target": {},
                "change": {"to": ["buyer@customer.example"], "cc": [OWNER],
                           "subject": "PO 7", "body": "Line one.\nLine two, all of it."},
                "reason": "Reply to the buyer."}
        card = desk.prepare(ME, mail, now=NOW)
        self.assertEqual(card["actions"][0]["message"], {
            "sender": OWNER, "to": ["buyer@customer.example"], "cc": [OWNER],
            "subject": "PO 7", "body": "Line one.\nLine two, all of it."})
        self.assert_card_shows_what_runs(desk, card)
        task = desk.prepare(ME, planner(), now=NOW)
        self.assertEqual(task["actions"][0]["fields"],
                         {"planId": PLAN, "title": "Chase the carrier for the late pallet"})
        self.assert_card_shows_what_runs(desk, task)

    def test_an_edits_before_is_read_from_the_system_never_the_models(self):
        update = {"system": "planner", "operation": "task_update",
                  "target": {"task_id": "task-0001"}, "change": {"title": "New title"},
                  "prior": {"title": "Old title"}, "precondition": {"etag": 'W/"e1"'},
                  "reason": "Rename it."}
        seen = []

        def read(values):
            def go(person, action):
                seen.append((person.email, action["target"].get("task_id")))
                return None if values is None else {"name": "Chase the carrier",
                                                    "values": values}
            return go
        for values, words in ((None, "cannot be read from planner"),
                              ({"title": "Someone renamed it"}, "other values now")):
            with self.subTest(values=values), self.assertRaises(pw.Refused) as refused:
                self.desk({}, read=read(values)).prepare(ME, update, now=NOW)
            self.assertIn(words, " ".join(refused.exception.problems))
        desk = self.desk({}, read=read({"title": "Old title"}))
        card = desk.prepare(ME, update, now=NOW)
        self.assertEqual(card["diff"], [{"field": "title", "before": "Old title",
                                         "after": "New title"}])
        self.assertEqual((card["actions"][0]["record"], card["actions"][0]["target"]["task_id"]),
                         ("Chase the carrier", "task-0001"))
        self.assertEqual(seen[-1], (OWNER, "task-0001"))
        self.assert_card_shows_what_runs(desk, card)

    def test_ask_read_values_under_another_spelling_are_the_changed_fields(self):
        """Live dry run (Portal, through the replica): 3 of 14 edits were refused as
        changed because Ask named what it read by the record's columns (``shipped_at``) or
        added a field the edit does not set (``status``); every such key was compared
        with a value never read. Now a key spelled another way is the changed field, a
        key for a field the edit does not set is not compared, and a changed field whose
        value really differs from the system's is still refused."""
        def update(prior):
            return {"system": "planner", "operation": "task_update",
                    "target": {"task_id": "task-0001"}, "change": {"dueDateTime": "2026-11-25"},
                    "prior": prior, "precondition": {"etag": 'W/"e1"'}, "reason": "Move it."}
        system = {"name": "Chase the carrier", "values": {"dueDateTime": None}}
        for prior in ({"due_date_time": None, "status": "open"},
                      {"DueDateTime": None}, {"percent_complete": 50}):
            with self.subTest(prior=prior):
                desk = self.desk({}, read=lambda p, a: system)
                card = desk.prepare(ME, update(prior), now=NOW)
                self.assertEqual(card["diff"], [{"field": "dueDateTime", "before": None,
                                                 "after": "2026-11-25"}])
                held = aw.actions_of(desk._unstash(desk._own(ME, card["prepared_id"])[0]["item"]))
                self.assertEqual(held[0]["precondition"]["prior"], {"dueDateTime": None})
                self.assert_card_shows_what_runs(desk, card)
        for prior in ({"due_date_time": "2026-11-20"},
                      {"dueDateTime": "2026-11-20", "due_date_time": None}):
            with self.subTest(prior=prior), self.assertRaises(pw.Refused) as refused:
                self.desk({}, read=lambda p, a: system).prepare(ME, update(prior), now=NOW)
            self.assertIn("other values now", " ".join(refused.exception.problems))

    def test_a_near_miss_field_is_refused_naming_where_the_system_keeps_it(self):
        """Ask sent ``expected_date`` for a BC purchase order and was refused with only the
        fields the operation sets; the refusal now also names the closest field of the
        operation and of the system's others, so the model tries again rightly."""
        tool = pw.tool(self.desk({}), ME)
        got = tool.call({"system": "planner", "operation": "task_update",
                         "target": {"task_id": "task-0001"}, "change": {"due_date": "2026-12-05"},
                         "reason": "Move it."})
        self.assertIn("refused", got)
        self.assertIn("task_update sets dueDateTime", " ".join(got["refused"]))
        from operating import autopilot_jobs as aj
        hint = " ".join(aj.field_hints(["bc: purchase_order_update cannot set expected_date "
                                        "(it sets: requestedReceiptDate)"]))
        self.assertIn("purchase_order_update sets requestedReceiptDate", hint)
        self.assertIn("purchase_order_line_update sets expectedReceiptDate", hint)
        self.assertIn("expectedReceiptDate", tool.spec["description"])   # the field list

    def test_mc_requester_is_read_from_mission_control(self):
        gate = {"system": "mc", "operation": "approval_decide",
                "target": {"task_id": "TASK-1", "gate_id": "apg_1"},
                "change": {"decision": "approve", "scope": {"kind": "launch"}},
                "reason": "Routine."}
        with self.assertRaises(pw.Refused) as refused:   # the model's requester: refused
            self.desk({}).prepare(ME, {**gate, "target": {**gate["target"],
                                                          "requested_by": "lee@plx.example"}},
                                  now=NOW)
        self.assertIn("card shows everything", " ".join(refused.exception.problems))
        with self.assertRaises(pw.Refused) as refused:   # nothing read: refused
            self.desk({}).prepare(ME, gate, now=NOW)
        self.assertIn("cannot be read", " ".join(refused.exception.problems))
        with self.assertRaises(pw.Refused) as refused:   # MC shows no scope: refused
            self.desk({}, read=lambda p, a: {"requested_by": OWNER}).prepare(ME, gate, now=NOW)
        self.assertIn("shows no scope", " ".join(refused.exception.problems))
        asked = []
        mc_scope = {"kind": "launch", "runtime": "label-run"}
        desk = self.desk({}, may=None, read=lambda p, a: {"requested_by": OWNER.upper(),
                                                         "scope": mc_scope})
        desk.may = lambda person, action: asked.append(action["people"]) or None
        # the model's scope (or none) is replaced by the gate's own as MC states it
        for change in ({"decision": "approve", "scope": {"kind": "merge"}},
                       {"decision": "approve"}):
            card = desk.prepare(ME, {**gate, "change": change, "reason": str(change)},
                                now=NOW)
            self.assertEqual(card["actions"][0]["people"], [OWNER])
            first, _rows = desk._own(ME, card["prepared_id"])
            sent = aw.actions_of(desk._unstash(first["item"]))[0]
            self.assertEqual(sent["payload"]["scope"], mc_scope)
            self.assert_card_shows_what_runs(desk, card)
        self.assertEqual(asked[0], [OWNER])

    def test_confirm_checks_the_grant_again_and_old_items_expire(self):
        desk = self.desk({aw.APPROVED_ENV: "planner_write"})
        pid = desk.prepare(ME, planner(), now=NOW)["prepared_id"]
        shown = desk.preview(ME, pid, now=NOW)
        # The grant is revoked between prepare and confirm
        desk.may = lambda person, action: "you are no longer a member of that plan"
        with self.assertRaises(pw.Refused) as refused:
            desk.confirm(ME, pid, preview_hash=shown["preview_hash"], confirm=True, now=NOW)
        self.assertEqual(refused.exception.status, 403)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(desk.card(ME, pid, now=NOW)["state"], "prepared")
        # a full reader is not re-checked, every other guard stays
        full = pw.Person(ME.ids, full=True)
        later = NOW + pw.EXPIRES
        self.assertEqual(desk.card(ME, pid, now=later)["state"], "expired")
        self.assertEqual(desk.mine(ME, now=later), [])
        with self.assertRaises(pw.Refused) as refused:
            desk.confirm(full, pid, preview_hash=shown["preview_hash"], confirm=True,
                         now=later)
        self.assertEqual((refused.exception.status, self.fake.calls), (409, []))

    def test_a_change_that_did_not_run_can_be_prepared_again(self):
        desk = self.desk({})
        pid = desk.prepare(ME, planner(), now=NOW)["prepared_id"]
        shown = desk.preview(ME, pid, now=NOW)
        ran = desk.confirm(ME, pid, preview_hash=shown["preview_hash"], confirm=True, now=NOW)
        self.assertEqual([o["status"] for o in ran["outcome"]], ["dry_run"])
        # the model asking again, or the person's retry: a new item, open
        again = desk.prepare(ME, planner(), now=NOW)
        self.assertNotEqual(again["prepared_id"], pid)
        self.assertEqual(again["state"], "prepared")
        desk.dismiss(ME, again["prepared_id"], now=NOW)
        retried = desk.retry(ME, again["prepared_id"], now=NOW)
        self.assertNotIn(retried["prepared_id"], (pid, again["prepared_id"]))
        # a confirmed change with no outcome (a crash) is shown not run, never re-run here
        with desk.journal.locked() as held:
            held.append({"event": "prepared", "at": aw._iso(NOW), "id": "pw_crashed",
                         "owner": OWNER, "origin": "instruction",
                         "item": pw.item_of(planner("x"), OWNER, "pw_crashed", "instruction"),
                         "shown": [], "diff": []})
            held.append({"event": "confirmed", "at": aw._iso(NOW), "id": "pw_crashed",
                         "by": OWNER})
        self.assertEqual(desk.card(ME, "pw_crashed", now=NOW + pw.NOT_RUN_AFTER)["state"],
                         "not_run")
        with self.assertRaises(pw.Refused):
            desk.retry(ME, "pw_crashed", now=NOW + pw.NOT_RUN_AFTER)


if __name__ == "__main__":
    unittest.main()
