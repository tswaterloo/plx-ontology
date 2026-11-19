"""The looks' computed change list (``operating.autopilot_changes``) on a throwaway Postgres
with the tables it reads (``ontology`` events and Sage cells, ``biz`` rows, lineage, access
classes): whose step it is from what people did before, listed once when the record reaches
the step, never when done by then, never a row the AI may not read; the cycle's wiring."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest import mock

from operating import autopilot as apm
from operating import autopilot_agent_io as io
from operating import autopilot_changes as ch
from operating.tests import pg_testdb

DIRECTORY = {"stefan@petra.test": "p-stefan", "oleg@petra.test": "p-oleg",
             "ross@petra.test": "p-ross", "maria.lopez@petra.test": "p-maria",
             "maria.chan@petra.test": "p-maria2"}
DOMAINS = ("petra.test",)
UTC = timezone.utc

SCHEMA = """
CREATE SCHEMA ontology; CREATE SCHEMA biz;
CREATE TABLE ontology.events (event_id text primary key, event_type text, time text,
  superseded_at text);
CREATE TABLE ontology.event_objects (event_id text, object_id text, qualifier text);
CREATE TABLE biz.access_classes (id int primary key, audience_keys text[], rec_marks text[],
  doc_marks text[], blocked boolean);
CREATE TABLE biz.lineage (view text, handle text, object_ids text[]);
CREATE TABLE biz.batches (batch_code text, checked_by text, tested_by text, handle text,
  access_class int, ai_ok boolean);
CREATE TABLE biz.records (type text, fields jsonb, access_class int, ai_ok boolean);
CREATE TABLE biz.sales_orders (order_number text, customer text, value numeric, handle text,
  access_class int, ai_ok boolean);
CREATE TABLE biz.purchase_orders (po_number text, purchaser text, handle text,
  access_class int, ai_ok boolean);
INSERT INTO biz.access_classes VALUES (1, '{a}', '{[]}', '{}', false),
  (2, '{a}', '{"[\\"finance\\"]", []}', '{}', false), (3, '{a}', '{[]}', '{}', true),
  (4, '{a}', '{[]}', '{doc}', false);
"""

DAY_START = datetime(2026, 6, 10, 11, tzinfo=UTC)      # Wed 7 AM US Eastern
TESTED = "2026-06-09"


class Db:
    def __init__(self, pg) -> None:
        self.pg, self.n = pg, 0

    def _id(self) -> str:
        self.n += 1
        return f"x{self.n}"

    def event(self, etype: str, time: str, obj: str, qualifier: str) -> None:
        e = self._id()
        self.pg.execute("INSERT INTO ontology.events VALUES (%s, %s, %s, NULL)", (e, etype, time))
        self.pg.execute("INSERT INTO ontology.event_objects VALUES (%s, %s, %s)", (e, obj, qualifier))

    def row(self, view: str, obj: str, values: tuple, access: int = 1, ai_ok: bool = True) -> None:
        handle = f"h:{obj}"
        cols = {"batches": "batch_code, checked_by, tested_by",
                "sales_orders": "order_number, customer, value",
                "purchase_orders": "po_number, purchaser"}[view]
        marks = ", ".join(["%s"] * (len(values) + 3))
        self.pg.execute(f"INSERT INTO biz.{view} ({cols}, handle, access_class, ai_ok) "
                        f"VALUES ({marks})", (*values, handle, access, ai_ok))
        self.pg.execute("INSERT INTO biz.lineage VALUES (%s, %s, %s)", (view, handle, [obj]))

    def released_before(self, checker: str, n: int, done: str = "Batch.released",
                        column: int = 1) -> None:
        for i in range(n):
            obj = f"Batch:old{self._id()}"
            who = (checker, "") if column == 1 else ("", checker)
            self.row("batches", obj, (f"OLD-{self.n}", *who))
            self.event(done, "2026-06-01", obj, "batch")

    def record(self, rtype: str, fields: dict, access: int = 1, ai_ok: bool = True) -> None:
        self.pg.execute("INSERT INTO biz.records VALUES (%s, %s, %s, %s)",
                        (rtype, json.dumps(fields), access, ai_ok))

    def keyed(self, code: str, day: str, n: int, order: str = "", clock: str = "9000000") -> None:
        for i in range(n):
            self.record("RelSageArIbh", {"enteredby": code + "   ", "audtdate": day,
                                         "audttime": clock, "ordrnbr": order + "     "})


class ChangeList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.name, dsn = pg_testdb.create_db()
        cls.pg = psycopg.connect(dsn, autocommit=True)

    @classmethod
    def tearDownClass(cls):
        cls.pg.close()
        pg_testdb.drop_db(cls.name)

    def setUp(self):
        self.pg.execute("DROP SCHEMA IF EXISTS ontology CASCADE; DROP SCHEMA IF EXISTS biz CASCADE")
        self.pg.execute(SCHEMA)
        self.db = Db(self.pg)

    def changes(self, stages=ch.STAGES):
        return ch.Changes(self.pg, DIRECTORY, DOMAINS, io.previous_look, stages)

    def test_a_tested_batch_is_the_usual_checkers_step_once_until_released(self):
        db = self.db
        db.released_before("Stefan", 3)
        db.released_before("Ross", 2)                       # under the habit
        db.row("batches", "Batch:1", ("BC-1", "", ""))
        db.event("Batch.tested", TESTED, "Batch:1", "batch")
        got = self.changes()("p-stefan", DAY_START.isoformat())
        self.assertEqual(got, [{"record": "BC-1", "stage": "qc_release",
                                "line": f"Batch BC-1: QC results entered {TESTED}, not released yet"}])
        self.assertEqual(self.changes()("p-ross", DAY_START), [])
        self.assertEqual(self.changes()("p-oleg", DAY_START), [])
        # listed once: the next hour's look does not repeat it
        self.assertEqual(self.changes()("p-stefan", DAY_START + timedelta(hours=1)), [])
        # a test of the look's own day is not known yet (date-only): nothing at its day start
        self.assertEqual(self.changes()("p-stefan", DAY_START - timedelta(days=1)), [])
        # released before the look: done
        db.event("Batch.released", "2026-06-10T10:59:00.000000Z", "Batch:1", "batch")
        self.assertEqual(self.changes()("p-stefan", DAY_START), [])

    def test_a_release_after_the_look_does_not_hide_the_step(self):
        db = self.db
        db.released_before("Stefan", 3)
        db.row("batches", "Batch:1", ("BC-1", "", ""))
        db.event("Batch.tested", TESTED, "Batch:1", "batch")
        db.event("Batch.released", "2026-06-10T12:24:51.000000Z", "Batch:1", "batch")
        self.assertEqual([r["record"] for r in self.changes()("p-stefan", DAY_START)], ["BC-1"])

    def test_a_shipped_order_is_the_usual_invoice_keyers_step_until_its_ar_entry_is_keyed(self):
        db = self.db
        db.keyed("OLEGO", "20260601", 3)
        db.keyed("MARIA", "20260601", 9)                    # two Marias: nobody
        for n, obj in (("OR-1", "SalesOrder:1"), ("OR-2", "SalesOrder:2"),
                       ("OR-3", "SalesOrder:3"), ("OR-4", "SalesOrder:4")):
            db.row("sales_orders", obj, (n, "A Customer", 1234))
            db.event("Shipment.sage_shipped", TESTED, obj, "order")
            # the OE invoice dated the ship day is not the AR entry: never "done"
            db.event("SalesInvoice.invoiced", TESTED, obj, "order")
        db.keyed("OLEGO", "20260609", 1, "OR-2")                     # keyed before: done
        db.keyed("OLEGO", "20260601", 1, "OR-1")     # an earlier shipment's invoice: not done
        db.keyed("OLEGO", "20260610", 1, "OR-3", clock="6595900")    # 6:59 AM: done
        db.keyed("OLEGO", "20260610", 1, "OR-4", clock="12245100")   # keyed after the look
        got = self.changes()("p-oleg", DAY_START)
        self.assertEqual([r["record"] for r in got], ["OR-4", "OR-1"])
        self.assertEqual(got[0]["line"], f"Order OR-4: shipped {TESTED}, AR invoice not entered yet")
        for r in got:   # the key and the stage only: no customer, no amount
            self.assertNotIn("Customer", r["line"])
            self.assertNotIn("1234", r["line"])
        self.assertEqual(self.changes()("p-maria", DAY_START), [])
        self.assertEqual(self.changes()("p-maria2", DAY_START), [])

    def test_a_made_batch_is_the_usual_testers_step_until_its_results_are_entered(self):
        db = self.db
        db.released_before("Oleg", 3, done="Batch.tested", column=2)
        db.released_before("Stefan", 3)          # the checker, not the tester
        db.row("batches", "Batch:1", ("BC-1", "", ""))
        db.event("Batch.manufactured", TESTED, "Batch:1", "batch")
        got = self.changes()("p-oleg", DAY_START)
        self.assertEqual(got, [{"record": "BC-1", "stage": "qc_test",
                                "line": f"Batch BC-1: made {TESTED}, QC results not entered yet"}])
        self.assertEqual(self.changes()("p-stefan", DAY_START), [])
        db.event("Batch.tested", TESTED, "Batch:1", "batch")
        self.assertEqual(self.changes()("p-oleg", DAY_START), [])

    def test_a_done_event_dated_the_looks_own_day_is_known_live_not_in_a_replay(self):
        db = self.db
        db.released_before("Oleg", 3, done="Batch.tested", column=2)
        db.row("batches", "Batch:1", ("BC-1", "", ""))
        db.event("Batch.manufactured", TESTED, "Batch:1", "batch")
        db.event("Batch.tested", "2026-06-10", "Batch:1", "batch")     # the look's day, no time
        self.assertEqual(self.changes()("p-oleg", DAY_START), [])
        replay = ch.Changes(self.pg, DIRECTORY, DOMAINS, io.previous_look, replay=True)
        self.assertEqual([r["record"] for r in replay("p-oleg", DAY_START)], ["BC-1"])

    def test_the_days_first_look_lists_again_what_still_waits(self):
        db = self.db
        db.released_before("Stefan", 3)
        for n, day in (("BC-1", "2026-06-02"), ("BC-2", "2026-06-01"), ("BC-3", "2026-06-08")):
            db.row("batches", f"Batch:{n}", (n, "", ""))
            db.event("Batch.tested", day, f"Batch:{n}", "batch")
        db.event("Batch.released", "2026-06-08", "Batch:BC-3", "batch")
        # BC-1 reached it last Tuesday: listed again at this start; BC-2 is older than that
        self.assertEqual([r["record"] for r in self.changes()("p-stefan", DAY_START)], ["BC-1"])
        self.assertEqual(self.changes()("p-stefan", DAY_START + timedelta(hours=1)), [])

    def test_a_planner_task_is_its_assignees_from_its_due_day_until_completed(self):
        db = self.db
        task = {"title": "Monthly water  testing", "due_at": "2026-06-10T10:00:00Z",
                "assignees": '["p-stefan", "p-ross"]', "created_at": "2026-06-01T12:00:00Z",
                "completed_at": None}
        db.record("PlannerTask", task)
        db.record("PlannerTask", {**task, "title": "Done already",
                                  "completed_at": "2026-06-10T10:30:00Z"})
        db.record("PlannerTask", {**task, "title": "Done later",
                                  "completed_at": "2026-06-10T15:30:00Z"})
        db.record("PlannerTask", {**task, "title": "Created later",
                                  "created_at": "2026-06-10T12:00:00Z"})
        db.record("PlannerTask", {**task, "title": "Hidden"}, ai_ok=False)
        db.record("PlannerTask", {**task, "title": "Marked"}, access=2)
        got = self.changes()("p-stefan", DAY_START)
        self.assertEqual(got, [{"record": "Monthly water testing", "stage": "task_due",
                                "line": "Planner task Monthly water testing: due 2026-06-10, "
                                        "not completed yet"},
                               {"record": "Done later", "stage": "task_due",
                                "line": "Planner task Done later: due 2026-06-10, "
                                        "not completed yet"}])
        self.assertEqual(len(self.changes()("p-ross", DAY_START)), 2)
        self.assertEqual(self.changes()("p-oleg", DAY_START), [])
        # the day before its due day: nothing; overdue: listed again at the next day's start
        # (with the one created after its due day began)
        self.assertEqual(self.changes()("p-stefan", DAY_START - timedelta(days=1)), [])
        self.assertEqual(sorted(r["record"] for r in
                                self.changes()("p-stefan", DAY_START + timedelta(days=1))),
                         ["Created later", "Monthly water testing"])

    def test_whose_step_is_read_once_a_day_and_kept(self):
        db = self.db
        db.released_before("Stefan", 3)
        db.row("batches", "Batch:1", ("BC-1", "", ""))
        db.event("Batch.tested", TESTED, "Batch:1", "batch")
        made = self.changes()
        made("p-stefan", DAY_START)
        kept = made.habits_made()
        self.assertEqual(kept["day"], "2026-06-10")
        self.assertEqual(kept["stages"]["qc_release"], ["p-stefan"])
        with mock.patch.object(ch, "_habit_counts", side_effect=AssertionError("read again")):
            again = ch.Changes(self.pg, DIRECTORY, DOMAINS, io.previous_look, habits=kept)
            self.assertEqual([r["record"] for r in again("p-stefan", DAY_START)], ["BC-1"])
            self.assertIsNone(again.habits_made())
        # a failed read is never kept: the next day reads it again
        with mock.patch.object(ch, "_habit_counts", side_effect=OSError("down")):
            failed = ch.Changes(self.pg, DIRECTORY, DOMAINS, io.previous_look)
            self.assertEqual(failed("p-stefan", DAY_START), [])
            self.assertIsNone(failed.habits_made())

    def test_rows_the_ai_may_not_read_or_marked_or_blocked_are_never_listed(self):
        db = self.db
        db.released_before("Stefan", 3)
        for i, (access, ai_ok) in enumerate(((1, False), (2, True), (3, True), (4, True), (1, True))):
            db.row("batches", f"Batch:{i}", (f"BC-{i}", "", ""), access=access, ai_ok=ai_ok)
            db.event("Batch.tested", TESTED, f"Batch:{i}", "batch")
        self.assertEqual([r["record"] for r in self.changes()("p-stefan", DAY_START)], ["BC-4"])

    def test_a_late_purchase_order_is_its_purchasers_alone(self):
        db = self.db
        db.row("purchase_orders", "PO:1", ("PO-1", "Ross Smith"))
        db.event("PurchaseOrder.sage_expected", TESTED, "PO:1", "purchase_order")
        db.row("purchase_orders", "PO:2", ("PO-2", "ross"))
        db.event("PurchaseOrder.sage_expected", TESTED, "PO:2", "purchase_order")
        db.event("Receipt.sage_received", TESTED, "PO:2", "purchase_order")
        self.assertEqual([r["record"] for r in self.changes()("p-ross", DAY_START)], ["PO-1"])
        self.assertEqual(self.changes()("p-stefan", DAY_START), [])

    def test_an_unreadable_stage_lists_nothing_and_is_counted(self):
        self.db.released_before("Stefan", 3)
        self.pg.execute("DROP TABLE ontology.event_objects")
        made = self.changes()
        self.assertEqual(made("p-stefan", DAY_START), [])
        self.assertGreater(made.failed["qc_release"], 0)

    def test_cardinality_no_row_lost_or_doubled(self):
        db = self.db
        db.released_before("Stefan", 3)
        for n in (1, 10, 60):
            self.setUp()
            db = self.db
            db.released_before("Stefan", 3)
            for i in range(n):
                db.row("batches", f"Batch:{i}", (f"BC-{i}", "", ""))
                db.event("Batch.tested", TESTED, f"Batch:{i}", "batch")
                db.event("Batch.tested", TESTED, f"Batch:{i}", "batch")   # twice: one row
            got = self.changes()("p-stefan", DAY_START)
            self.assertEqual(sorted(r["record"] for r in got), sorted(f"BC-{i}" for i in range(n)))
            look = io.look_events(["p-stefan"], DAY_START, changes=self.changes())
            self.assertEqual(len(look[0]["fields"]["changes"]), min(n, io.LOOK_CHANGES))


class Windows(unittest.TestCase):
    def test_previous_look(self):
        mon = datetime(2026, 6, 8, 11, tzinfo=UTC)          # Mon 7 AM ET
        self.assertEqual(io.previous_look(mon), datetime(2026, 6, 5, 21, tzinfo=UTC))
        self.assertEqual(io.previous_look(mon + timedelta(hours=3)), mon + timedelta(hours=2))
        # the date-only events of Fri, Sat and Sun are known by Monday's day start
        self.assertEqual(ch.due_days(io.previous_look(mon), mon),
                         ["2026-06-05", "2026-06-06", "2026-06-07"])
        self.assertEqual(ch.due_days(mon, mon + timedelta(hours=1)), [])


class CycleWiring(unittest.TestCase):
    def test_postgres_unreachable_looks_carry_no_list(self):
        skipped: Counter = Counter()
        people = {"people": [{"id": "p-stefan", "addresses": ["stefan@petra.test"]}]}
        with mock.patch("psycopg.connect", side_effect=OSError("down")), \
                mock.patch.object(apm, "_people_maps", return_value=(DIRECTORY, {}, DOMAINS)):
            got = apm._looks(["p-stefan"], DAY_START, people, skipped)
        self.assertEqual([e["fields"]["changes"] for e in got], [[]])
        self.assertEqual(skipped["look_changes_unreadable"], 1)
        self.assertEqual(apm._looks(["p-stefan"], DAY_START.replace(hour=3), people, skipped), [])

    def test_the_cycle_passes_the_list_and_closes_its_connection(self):
        skipped: Counter = Counter()
        conn = mock.MagicMock()
        rows = [{"record": "BC-1", "stage": "qc_release", "line": "Batch BC-1: tested"}]
        with mock.patch("psycopg.connect", return_value=conn), \
                mock.patch.object(apm, "_people_maps", return_value=(DIRECTORY, {}, DOMAINS)), \
                mock.patch.object(ch.Changes, "__call__", return_value=rows):
            got = apm._looks(["p-stefan"], DAY_START, {}, skipped)
        self.assertEqual(got[0]["fields"]["changes"], [{"record": "BC-1", "line": "Batch BC-1: tested"}])
        conn.close.assert_called_once()


    def test_a_held_look_is_not_computed_again_and_whose_step_is_kept_in_the_queue(self):
        import tempfile
        from pathlib import Path
        from operating.autopilot_queue import Queue
        skipped: Counter = Counter()
        rows = [{"record": "BC-1", "stage": "qc_release", "line": "Batch BC-1: tested"}]
        kept = {"day": "2026-06-10", "stages": {"qc_release": ["p-stefan"]}}
        with tempfile.TemporaryDirectory() as d:
            queue = Queue(Path(d) / "autopilot.sqlite")
            seen = []

            def call(made, owner, at):
                seen.append(owner)
                return rows
            with mock.patch("psycopg.connect", return_value=mock.MagicMock()), \
                    mock.patch.object(apm, "_people_maps", return_value=(DIRECTORY, {}, DOMAINS)), \
                    mock.patch.object(ch.Changes, "__call__", call), \
                    mock.patch.object(ch.Changes, "habits_made", return_value=kept):
                got = apm._looks(["p-stefan", "p-ross"], DAY_START, {}, skipped, queue)
                self.assertEqual(len(got), 2)
                self.assertEqual(queue.meta(apm.LOOK_HABITS), kept)
                with queue.conn:
                    queue.add_event(got[0], "now")
                again = apm._looks(["p-stefan", "p-ross"], DAY_START + timedelta(minutes=10),
                                   {}, skipped, queue)
            self.assertEqual([e["owner_hint"] for e in again],
                             [e["owner_hint"] for e in got[1:]])
            self.assertEqual(len(seen), 3)
            queue.close()

class InvitationAnswer(unittest.TestCase):
    """An invitation put to the person whose calendar entry is not readable here: its answer
    is a to-do with what the mail says; never for anything else or a screened message."""
    EV = {"event_type": "message.received", "channel": "mail", "item_kind": "meeting_request",
          "to_me": True, "subject": "Invitation: Line standards review",
          "sender": "jesse@petra.test", "sender_name": "Jesse V",
          "at": "2026-06-09T21:54:00Z", "mailbox": "p-stefan"}

    def test_the_answer_is_a_todo_with_the_invitations_details(self):
        from operating.autopilot_agent import Decision
        got, _held, why = io.agent_output(Decision(), self.EV, "p-stefan", "", {}, {}, None,
                                          "stefan@petra.test")
        self.assertEqual(why, "")
        [todo] = got.todos
        self.assertEqual(todo["record"], "Line standards review")
        self.assertIn("Jesse V", todo["what"])
        self.assertIn("Tue Jun 9, 5:54 PM ET", todo["what"])
        # a session that left nothing: the same to-do; a screened one: nothing
        self.assertEqual(io.invitation_only(self.EV, {}, "nothing_to_do").todos, [todo])
        for why in ("not_cleared", "freightcom", "job_failed", "agent_no_session"):
            self.assertIsNone(io.invitation_only(self.EV, {}, why))

    def test_not_for_a_readable_entry_an_answer_a_copy_or_other_mail(self):
        from operating.autopilot_agent import Decision
        answered = Decision(proposals=[{"tool": "calendar", "action": "event_respond"}])
        got, _h, _w = io.agent_output(answered, self.EV, "p-stefan", "", {}, {}, None, "s")
        self.assertEqual(got.todos, [])
        for ev, material in ((self.EV, {"invites": ["on the calendar"]}),
                             ({**self.EV, "item_kind": "meeting_response"}, {}),
                             ({**self.EV, "to_me": False}, {}),
                             ({**self.EV, "item_kind": None}, {}),
                             ({**self.EV, "subject": "Invitation: "}, {})):
            self.assertIsNone(io.invitation_todo(ev, material), ev)


if __name__ == "__main__":
    unittest.main()
