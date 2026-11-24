"""Assist matters (``operating.assist_matters``, ASSIST specs 2.3, contract C5) on Postgres:
the migrations' own tables, made-up comms rows, MC and Planner objects, and readings written
as the reading run writes them.

Checked at the boundary: a matter opens only from a reading that names a staff actor (one
per person and thread, the newest reading its basis), an unresolved actor opens nothing and
is counted; the owner's later message or a colleague answering the asker closes it; a new ask
in the thread reopens it; a message gone from the store makes it unverified and the list
says it could not be re-checked; approvals close as decided or superseded; a task closes
when completed and for anyone it was taken from; queue items follow their item; the list
shows the last 14 days and counts the rest; nobody is shown without an opt-in, an
unreadable opt-in journal shows nobody, the owner sees every person's marked "not shown to
staff"; the door decides each item (a matter of the partners' mail reaches only the
owner's door); a role pool's matter reaches its members. 1/10/60: the cycle and the list
take the same number of statements for 1, 10 and 60 threads.

Not here: C7 identity (``record_group`` returns the handle until MODEL lands D1, so the same
order seen in two systems is two matters). Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from operating import assist_matters as am
from operating import assist_readings as ar
from operating.tests import pg_testdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

NOW = dt.datetime(2026, 11, 23, 15, 0, tzinfo=dt.timezone.utc)
ANN, BOB, CY = "p-ann", "p-bob", "p-cy"           # staff ids (the Entra ids in production)
OUT = "buyer@customer.example"
OWNER_KEY = "plx:owner"


class Counting:
    """The connection with every statement counted (the 1/10/60 case)."""

    def __init__(self, conn):
        self.conn, self.n = conn, 0

    def execute(self, *a, **k):
        self.n += 1
        return self.conn.execute(*a, **k)

    def transaction(self):
        return self.conn.transaction()


class Matters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)

    def setUp(self):
        self.pg = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.pg.close)
        from operating.pg.migrate import MIGRATIONS_DIR
        p = self.pg
        p.execute("DROP SCHEMA IF EXISTS assist CASCADE; DROP SCHEMA IF EXISTS biz CASCADE; "
                  "DROP SCHEMA IF EXISTS ontology CASCADE; CREATE SCHEMA biz; CREATE SCHEMA "
                  "ontology")
        for stmt in ar.ddl():
            p.execute(stmt)
        p.execute((MIGRATIONS_DIR / "0063_assist_work.sql").read_text(encoding="utf-8"))
        self.biz_tables()
        for name in ("0065_assist_api.sql", "0073_assist_items_view_columns.sql",
                     "0076_assist_activity.sql"):
            p.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        for pid, addr in ((ANN, "ann@ours.example"), (BOB, "bob@ours.example"),
                          (CY, "cy@ours.example")):
            p.execute("INSERT INTO biz.staff VALUES (%s, %s, %s)", (pid, addr, pid))
            p.execute("INSERT INTO biz.mailbox_owners VALUES (%s, %s)", (pid, [addr]))
        self.n = 0

    def biz_tables(self):
        p = self.pg
        p.execute("CREATE TABLE biz.emails (handle text, message_id text, thread text, mailbox "
                  "text, sender text, to_addresses text[], cc_addresses text[], subject text, "
                  "received_at timestamptz, audience_keys text[], rec_marks text[], "
                  "object_ids text[])")
        p.execute("CREATE TABLE biz.teams_messages (handle text, chat text, sender text, "
                  "created_at timestamptz, subject text, audience_keys text[], rec_marks "
                  "text[], object_ids text[])")
        p.execute("CREATE TABLE biz.staff (person_id text, email text, name text)")
        p.execute("CREATE TABLE biz.mailbox_owners (mailbox text, addrs text[])")
        p.execute("CREATE TABLE biz.alerts (rule text, handle text, topic text, concerns text, "
                  "mailbox text, why text, since timestamptz, state text, kind text)")
        p.execute("CREATE TABLE ontology.objects (object_id text, object_type text, "
                  "audience_key text, first_recorded_at text, missing_from_source_since text)")
        p.execute("CREATE TABLE ontology.object_values (object_id text, property text, value "
                  "text, markings text, superseded_at text)")

    # ---------------------------------------------------------------- fixture writers

    def mail(self, thread, sender, to, *, box=ANN, at, cc=(), aud=None, marks="[]"):
        self.n += 1
        h = f"mail://{thread}/{self.n}"
        self.pg.execute(
            "INSERT INTO biz.emails VALUES (%s, %s, %s, %s, %s, %s, %s, 'Order 55', %s, %s, %s, "
            "%s)", (h, f"<m{self.n}>", thread, box, sender, list(to), list(cc), at,
                    aud or [f"mail:{box}"], [marks], [f"o:{self.n}"]))
        return h

    def read(self, handle, *, source="emails", thread, actor, at, needs=True, basis="address",
             act_class="reply", summary="Asks for the delivery date", key=None):
        self.pg.execute(
            "INSERT INTO assist.readings (source, message_key, handles, thread_key, said_at, "
            "read_ver, needs_act, act, act_class, summary, quote, status, actor_person, "
            "actor_basis) VALUES (%s, %s, %s, %s, %s, 2, %s, 'answer them', %s, %s, 'when?', "
            "'ok', %s, %s)", (source, key or handle, [handle], thread, at, needs, act_class,
                              summary, actor, basis))

    def obj(self, oid, otype, aud, **props):
        self.pg.execute("INSERT INTO ontology.objects VALUES (%s, %s, %s, %s, NULL)",
                        (oid, otype, aud, "2026-11-20T10:00:00Z"))
        for k, v in props.items():
            self.pg.execute("INSERT INTO ontology.object_values VALUES (%s, %s, %s, '[]', NULL)",
                            (oid, k, v))

    def cycle(self, pg=None, **kw):
        out = am.cycle(pg or self.pg, now=NOW, environ={}, **kw)
        self.assertEqual(out["status"], "ok", out)
        return out

    def matters(self, kind=None):
        rows = self.pg.execute(
            "SELECT owner_person, kind, subject_key, state, done_reason, verify_note, basis_at "
            "FROM assist.matters WHERE %s::text IS NULL OR kind = %s ORDER BY 1, 3",
            (kind, kind)).fetchall()
        return rows

    def log(self, what):
        return self.pg.execute("SELECT count(*) FROM assist.matter_log WHERE what = %s",
                               (what,)).fetchone()[0]

    @staticmethod
    def everything(items):
        return {i["item_id"] for i in items}

    # ---------------------------------------------------------------- conversations

    def test_a_reading_naming_a_staff_actor_opens_one_matter_per_person_and_thread(self):
        t0 = NOW - dt.timedelta(hours=5)
        h1 = self.mail("t1", OUT, ["ann@ours.example"], at=t0)
        h2 = self.mail("t1", OUT, ["ann@ours.example"], at=t0 + dt.timedelta(hours=1))
        h3 = self.mail("t2", OUT, ["ann@ours.example", "bob@ours.example"], at=t0)
        self.read(h1, thread="t1", actor=ANN, at=t0)
        self.read(h2, thread="t1", actor=ANN, at=t0 + dt.timedelta(hours=1),
                  summary="Asks again")
        self.read(h3, thread="t2", actor=None, basis="unresolved:2", at=t0)   # two staff match
        h4 = self.mail("t3", OUT, ["ann@ours.example"], at=t0)
        self.read(h4, thread="t3", actor=None, basis="", at=t0)              # an outside actor
        h5 = self.mail("t4", OUT, ["ann@ours.example"], at=t0)
        self.read(h5, thread="t4", actor=ANN, needs=False, at=t0)             # nothing to do
        out = self.cycle()
        self.assertEqual(out["readings"]["actor_unresolved"], 1)
        rows = self.matters("conversation")
        self.assertEqual([(r[0], r[2], r[3]) for r in rows], [(ANN, "t1", "open")])
        self.assertEqual(rows[0][6], t0 + dt.timedelta(hours=1))       # the newest is the basis
        title, srcs = self.pg.execute("SELECT title, sources FROM assist.matters").fetchone()
        self.assertEqual(title, "Asks again")
        self.assertEqual(len(srcs["readings"]), 2)
        # a second cycle changes nothing and logs nothing new
        self.cycle()
        self.assertEqual(self.log("opened"), 1)
        self.assertEqual(self.log("basis_moved"), 0)

    def test_a_matter_stays_open_however_old_until_a_source_fact_closes_it(self):
        old = NOW - dt.timedelta(days=400)
        self.read(self.mail("t-old", OUT, ["ann@ours.example"], at=old), thread="t-old",
                  actor=ANN, at=old)
        self.cycle()
        self.assertEqual([r[3] for r in self.matters()], ["open"])
        self.cycle()
        self.assertEqual([r[3] for r in self.matters()], ["open"])       # no age closes it

    def test_the_owner_answering_closes_and_a_new_ask_reopens(self):
        t0 = NOW - dt.timedelta(hours=5)
        h1 = self.mail("t1", OUT, ["ann@ours.example"], at=t0)
        self.read(h1, thread="t1", actor=ANN, at=t0)
        self.cycle()
        self.mail("t1", "ann@ours.example", [OUT], at=t0 + dt.timedelta(hours=1))   # Ann replied
        self.cycle()
        self.assertEqual([r[3:5] for r in self.matters()], [("done", "owner_acted")])
        h3 = self.mail("t1", OUT, ["ann@ours.example"], at=t0 + dt.timedelta(hours=2))
        self.read(h3, thread="t1", actor=ANN, at=t0 + dt.timedelta(hours=2))
        self.cycle()
        self.assertEqual([r[3:5] for r in self.matters()], [("open", None)])
        self.assertEqual(self.log("basis_moved"), 1)

    def say(self, matter, decision, at=None):
        """What the API writes for a person's word on a matter (its role may append this only)."""
        self.pg.execute("INSERT INTO assist.matter_log (matter_id, what, detail, at) VALUES "
                        "(%s, 'closed', %s, coalesce(%s, now()))",
                        (matter, json.dumps({"by": ["ann"], "decision": decision}), at))

    def test_a_persons_word_closes_or_drops_the_matter_and_an_older_word_closes_nothing(self):
        t0 = NOW - dt.timedelta(hours=5)
        for thread in ("t1", "t2", "t3"):
            self.read(self.mail(thread, OUT, ["ann@ours.example"], at=t0), thread=thread,
                      actor=ANN, at=t0)
        self.cycle()
        ids = {r[2]: self.pg.execute("SELECT matter_id FROM assist.matters WHERE subject_key = %s",
                                     (r[2],)).fetchone()[0] for r in self.matters()}
        self.say(ids["t1"], "accept", at=NOW - dt.timedelta(hours=4))
        self.say(ids["t2"], "reject", at=NOW - dt.timedelta(hours=4))
        self.say(ids["t3"], "accept", at=t0 - dt.timedelta(hours=1))      # before the ask
        out = self.cycle()
        self.assertEqual(out["persons"], {"person_done": 1, "person_dropped": 1})
        self.assertEqual({r[2]: r[3:5] for r in self.matters()},
                         {"t1": ("done", "person_done"), "t2": ("dropped", "person_dropped"),
                          "t3": ("open", None)})
        self.cycle()                                                      # stays closed
        self.assertEqual({r[2]: r[3] for r in self.matters()},
                         {"t1": "done", "t2": "dropped", "t3": "open"})
        self.assertEqual(self.log("closed"), 5)       # three words, two applied, none doubled
        # a newer ask on a closed thread opens it again
        t1 = NOW - dt.timedelta(hours=1)
        self.read(self.mail("t1", OUT, ["ann@ours.example"], at=t1), thread="t1", actor=ANN, at=t1)
        self.cycle()
        self.assertEqual({r[2]: r[3] for r in self.matters()}["t1"], "open")

    def test_a_colleague_answering_the_asker_closes_it_but_not_one_writing_elsewhere(self):
        t0 = NOW - dt.timedelta(hours=5)
        h1 = self.mail("t1", OUT, ["ann@ours.example"], at=t0)
        self.read(h1, thread="t1", actor=ANN, at=t0)
        h2 = self.mail("t2", OUT, ["ann@ours.example"], at=t0)
        self.read(h2, thread="t2", actor=ANN, at=t0)
        self.cycle()
        # in t2 Bob writes to a third party only; in t1 Bob answers the asker
        self.mail("t2", "bob@ours.example", ["other@else.example"], box=BOB,
                  at=t0 + dt.timedelta(hours=1))
        self.mail("t1", "bob@ours.example", [OUT], box=BOB, at=t0 + dt.timedelta(hours=1))
        self.cycle()
        self.assertEqual([(r[2], r[3], r[4]) for r in self.matters()],
                         [("t1", "done", "answered_in_thread"), ("t2", "open", None)])

    def test_a_teams_turn_closes_when_the_owner_posts_after_it(self):
        t0 = NOW - dt.timedelta(hours=5)
        self.pg.execute("INSERT INTO biz.teams_messages VALUES ('teams://c1/1', 'c1', %s, %s, "
                        "NULL, '{teams:chat:c1}', '{[]}', '{o:t1}')",
                        (json.dumps({"id": BOB, "display_name": "Bob"}), t0))
        self.read("teams://c1/1", source="teams_messages", thread="c1", actor=ANN, at=t0,
                  basis="teams_id")
        self.cycle()
        item = am.as_items(self.pg, [ANN], shown=True, door=self.everything,
                           now=NOW)["items"][0]
        self.assertEqual((item["channel"], item["sender"]), ("teams", "Bob"))
        self.pg.execute("INSERT INTO biz.teams_messages VALUES ('teams://c1/2', 'c1', %s, %s, "
                        "NULL, '{teams:chat:c1}', '{[]}', '{o:t2}')",
                        (json.dumps({"id": ANN, "display_name": "Ann"}),
                         t0 + dt.timedelta(minutes=5)))
        self.cycle()
        self.assertEqual([r[3:5] for r in self.matters()], [("done", "owner_acted")])

    def test_a_message_gone_from_the_store_is_unverified_and_listed_as_such(self):
        t0 = NOW - dt.timedelta(hours=5)
        h1 = self.mail("t1", OUT, ["ann@ours.example"], at=t0)
        self.read(h1, thread="t1", actor=ANN, at=t0)
        self.cycle()
        self.pg.execute("DELETE FROM biz.emails")
        self.cycle()
        row = self.matters()[0]
        self.assertEqual(row[3], "unverified")
        self.assertIn("no longer in the mail store", row[5])
        got = am.as_items(self.pg, [ANN], shown=True, door=self.everything, now=NOW)
        self.assertEqual(len(got["items"]), 1)
        self.assertIn(am.UNVERIFIED_WORDS, got["items"][0]["note"])
        self.assertEqual(self.log("unverified"), 1)
        self.cycle()                                  # no second log row for the same state
        self.assertEqual(self.log("unverified"), 1)

    # ---------------------------------------------------------------- typed MC / Planner rows

    def test_approvals_open_for_the_approver_and_close_decided_or_superseded(self):
        aud = "mc:live/approvals/P1"
        self.obj("A1", "MCApproval", aud, status="pending", task_id="T1",
                 requested_at="2026-11-17T01:00:00Z", requested_by="cos@ours.example",
                 reason="Merge PR 1 at head a")
        self.obj("A2", "MCApproval", aud, status="pending", task_id="T1",
                 requested_at="2026-11-17T03:00:00Z", requested_by="cos@ours.example",
                 reason="Merge PR 1 at head b")
        self.obj("A3", "MCApproval", aud, status="approved", task_id="T2",
                 requested_at="2026-11-17T03:00:00Z", requested_by="cos@ours.example",
                 reason="done already")
        with mock.patch("operating.autopilot._approvers", return_value=[CY]):
            self.cycle()
            self.assertEqual([(r[0], r[2], r[3]) for r in self.matters("approval")],
                             [(CY, "A2", "open")])
            # A2 decided
            self.pg.execute("UPDATE ontology.object_values SET value = 'approved' WHERE "
                            "object_id = 'A2' AND property = 'status'")
            self.cycle()
        self.assertEqual([r[3:5] for r in self.matters("approval")], [("done", "approval_decided")])

    def test_a_task_opens_for_its_assignee_and_closes_when_done_or_taken_away(self):
        aud = "meetings:plan:P"
        self.obj("PT1", "PlannerTask", aud, title="Send the COA", assignees=json.dumps([ANN]),
                 created_at="2026-11-22T10:00:00Z")
        self.obj("PT2", "PlannerTask", aud, title="Book the truck", assignees=json.dumps([BOB]),
                 created_at="2026-11-22T10:00:00Z")
        self.obj("PT3", "PlannerTask", aud, title="Old", assignees=json.dumps(["p-gone"]),
                 created_at="2026-11-22T10:00:00Z")
        out = self.cycle()
        self.assertEqual(out["tasks"]["assignee_unresolved"], 1)
        self.assertEqual([(r[0], r[2]) for r in self.matters("task")], [(ANN, "PT1"), (BOB, "PT2")])
        self.pg.execute("INSERT INTO ontology.object_values VALUES ('PT1', 'completed_at', "
                        "'2026-11-23T10:00:00Z', '[]', NULL)")
        self.pg.execute("UPDATE ontology.object_values SET value = %s WHERE object_id = 'PT2' "
                        "AND property = 'assignees'", (json.dumps([CY]),))
        self.cycle()
        self.assertEqual([(r[0], r[2], r[3], r[4]) for r in self.matters("task")],
                         [(ANN, "PT1", "done", "task_closed"), (BOB, "PT2", "done", "reassigned"),
                          (CY, "PT2", "open", None)])

    # ---------------------------------------------------------------- the queue

    def queue(self, d):
        path = Path(d) / "autopilot.sqlite"
        q = sqlite3.connect(path)
        q.execute("CREATE TABLE items (item_id TEXT PRIMARY KEY, event_id TEXT, autopilot TEXT, "
                  "owner TEXT, title TEXT, body TEXT, audiences TEXT, markings TEXT, citations "
                  "TEXT, due_at TEXT, created_at TEXT, state TEXT, done_reason TEXT, done_at "
                  "TEXT, prepare_ms INTEGER, latency_s REAL)")
        rows = [("q1", "qc_hold", CY, {"happened": {"object_id": "Batch:1"}}, "open"),
                ("q2", "approval_request", CY, {"happened": {"object_id": "A9",
                                                             "event_type": "approval.requested"}},
                 "open"),
                ("q3", "teams_reply", ANN, {"happened": {"object_id": "o:t1",
                                                         "event_type": "message.received"}},
                 "done")]
        for item, auto, owner, body, state in rows:
            q.execute("INSERT INTO items VALUES (?, 'e', ?, ?, ?, ?, '[\"mc:x\"]', '[]', '[]', "
                      "'2026-11-19T00:00:00Z', '2026-11-18T00:00:00Z', ?, NULL, NULL, 0, 0)",
                      (item, auto, owner, f"title {item}", json.dumps(body), state))
        q.commit()
        q.close()
        return path

    def test_adopt_moves_the_open_items_and_the_first_reverify_decides_them(self):
        self.obj("A9", "MCApproval", "mc:x", status="approved", task_id="T9",
                 requested_at="2026-11-17T01:00:00Z", requested_by="cos@ours.example",
                 reason="r")
        with tempfile.TemporaryDirectory() as d:
            path = self.queue(d)
            with mock.patch("operating.autopilot._approvers", return_value=[CY]):
                out = am.adopt(self.pg, path, now=NOW)
            self.assertEqual((out["items"], out["moved"], out["open"],
                              out["closed_by_first_reverify"]), (2, 2, 1, 1))
            self.assertEqual([(r[1], r[3], r[4]) for r in self.matters()],
                             [("approval", "done", "approval_decided"), ("record", "open", None)])
            # the qc item follows its queue item
            q = sqlite3.connect(path)
            q.execute("UPDATE items SET state = 'done', done_reason = 'batch_released' WHERE "
                      "item_id = 'q1'")
            q.commit()
            q.close()
            self.cycle(queue=path)
        self.assertEqual([(r[1], r[3], r[4]) for r in self.matters()],
                         [("approval", "done", "approval_decided"),
                          ("record", "done", "queue:batch_released")])

    # ---------------------------------------------------------------- lists and doors

    def test_the_list_shows_14_days_counts_the_rest_and_shows_only_the_opted_in(self):
        for n, days in enumerate((1, 3, 20, 40)):
            at = NOW - dt.timedelta(days=days)
            h = self.mail(f"t{n}", OUT, ["ann@ours.example"], at=at)
            self.read(h, thread=f"t{n}", actor=ANN, at=at)
        self.cycle()
        got = am.as_items(self.pg, [ANN], shown=True, door=self.everything, now=NOW)
        self.assertEqual((len(got["items"]), got["older"], got["total"], got["status"]),
                         (2, 2, 4, "ok"))
        # a list read after the cycle stopped says it is stale, and since when (F7)
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=am.STALE_MINUTES + 1)
        stale = am.as_items(self.pg, [ANN], shown=True, door=self.everything, now=later,
                            all_ages=True)
        self.assertEqual((stale["status"], len(stale["items"])), ("stale", 4))
        self.assertIn("last re-checked", stale["reason"])
        everything = am.as_items(self.pg, [ANN], shown=True, door=self.everything,
                                 all_ages=True, now=NOW)
        self.assertEqual((len(everything["items"]), everything["older"]), (4, 0))
        off = am.as_items(self.pg, [ANN], shown=False, door=self.everything, now=NOW)
        self.assertEqual((off["items"], off["status"]), ([], "off"))
        broken = am.as_items(self.pg, [ANN], shown=None, door=self.everything, now=NOW)
        self.assertEqual((broken["status"], broken["reason"]), ("off", am.OPT_IN_UNREADABLE))
        owner = am.as_items(self.pg, [], shown=False, owner_view=True, everyone=True,
                            door=self.everything, now=NOW)
        self.assertEqual(len(owner["items"]), 2)
        self.assertTrue(all("not shown to staff" in i["note"] for i in owner["items"]))
        # the door's default is nothing
        self.assertEqual(am.as_items(self.pg, [ANN], shown=True, now=NOW)["items"], [])

    def test_the_api_role_reads_the_list_view_and_appends_log_rows_only(self):
        """As plx_api (not a superuser): the list's view and log appends, nothing else of
        the schema (the readings' text and the matters table stay out of its reach)."""
        from operating.pg.migrate import MIGRATIONS_DIR
        if not self.pg.execute("SELECT 1 FROM pg_roles WHERE rolname = 'plx_api'").fetchone():
            try:
                self.pg.execute("CREATE ROLE plx_api NOLOGIN")
            except psycopg.errors.DuplicateObject:
                pass
        self.pg.execute("GRANT USAGE ON SCHEMA biz TO plx_api; GRANT SELECT ON ALL TABLES IN "
                        "SCHEMA biz TO plx_api")
        self.pg.execute("DROP VIEW assist.matter_items")        # as a fresh database has none
        for f in ("0063_assist_work.sql", "0065_assist_api.sql",
                  "0073_assist_items_view_columns.sql", "0076_assist_activity.sql"):   # their grants, role present
            self.pg.execute((MIGRATIONS_DIR / f).read_text(encoding="utf-8"))
        t0 = NOW - dt.timedelta(hours=1)
        h = self.mail("t1", OUT, ["ann@ours.example"], at=t0)
        self.read(h, thread="t1", actor=ANN, at=t0)
        self.cycle()
        with psycopg.connect(self.dsn, autocommit=True) as api:
            api.execute("SET ROLE plx_api")
            got = am.as_items(api, [ANN], shown=True, door=self.everything, now=NOW)
            self.assertEqual(len(got["items"]), 1)
            api.execute("INSERT INTO assist.matter_log (matter_id, what) VALUES (%s, "
                        "'requested')", (got["items"][0]["item_id"],))
            for table in ("assist.matters", "assist.readings", "assist.matter_log"):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege, msg=table):
                    api.execute(f"SELECT count(*) FROM {table}")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                api.execute("UPDATE assist.matters SET state = 'done'")

    def test_migration_0065_is_the_modules_ddl(self):
        from operating.pg.migrate import MIGRATIONS_DIR
        text = (MIGRATIONS_DIR / "0065_assist_api.sql").read_text(encoding="utf-8")
        self.assertTrue(all(stmt in text for stmt in am.items_view_ddl()))
        later = (MIGRATIONS_DIR / "0073_assist_items_view_columns.sql").read_text(encoding="utf-8")
        self.assertIn(am.items_view_ddl(True)[0], later)
        newest = (MIGRATIONS_DIR / "0076_assist_activity.sql").read_text(encoding="utf-8")
        self.assertIn(am.items_view_ddl(2)[0], newest)

    def test_the_list_view_is_made_without_the_comms_views_and_filled_in_once_they_exist(self):
        p = self.pg
        p.execute("DROP VIEW assist.matter_items; ALTER TABLE biz.emails RENAME TO emails_away; "
                  "ALTER TABLE biz.teams_messages RENAME TO teams_away")
        from operating.pg.migrate import MIGRATIONS_DIR
        p.execute((MIGRATIONS_DIR / "0065_assist_api.sql").read_text(encoding="utf-8"))
        self.assertEqual(am.ensure_items_view(p), "no_biz")
        self.assertEqual(p.execute("SELECT count(*) FROM assist.matter_items").fetchone()[0], 0)
        p.execute("ALTER TABLE biz.emails_away RENAME TO emails; ALTER TABLE biz.teams_away "
                  "RENAME TO teams_messages")
        t0 = NOW - dt.timedelta(hours=5)
        self.read(self.mail("t1", OUT, ["ann@ours.example"], at=t0), thread="t1", actor=ANN, at=t0)
        self.cycle()                                      # the cycle makes the full view
        self.assertEqual(p.execute("SELECT sender FROM assist.matter_items").fetchone()[0], OUT)

    def test_shown_to_is_an_own_opt_in_or_the_draft_file_and_fails_closed(self):
        rows = [{"event": "opt_in", "person": "ann@ours.example", "by": "ann@ours.example",
                 "workflow": "mail_thread", "on": True, "at": "2026-11-20T00:00:00Z"},
                {"event": "opt_in", "person": "bob@ours.example", "by": "ann@ours.example",
                 "workflow": "mail_thread", "on": True, "at": "2026-11-20T00:00:00Z"}]
        self.assertTrue(am.shown_to([ANN, "ann@ours.example"], rows=rows, draft={}))
        self.assertFalse(am.shown_to([BOB, "bob@ours.example"], rows=rows, draft={}))  # not by Bob
        self.assertTrue(am.shown_to([BOB, "bob@ours.example"], rows=rows,
                                    draft={"bob@ours.example": ["mail_thread"]}))
        paused = rows + [{"event": "opt_in_paused", "person": "ann@ours.example",
                          "by": "ann@ours.example", "paused": True,
                          "at": "2026-11-21T00:00:00Z"}]
        self.assertFalse(am.shown_to([ANN, "ann@ours.example"], rows=paused, draft={}))
        self.assertIsNone(am.shown_to([ANN], rows=None, draft={}))

    def test_each_reader_sees_only_what_their_door_gives_and_partner_mail_only_the_owner(self):
        t0 = NOW - dt.timedelta(hours=5)
        h1 = self.mail("t1", OUT, ["ann@ours.example"], at=t0)
        self.read(h1, thread="t1", actor=ANN, at=t0)
        h2 = self.mail("t2", OUT, ["ann@ours.example"], at=t0, aud=[OWNER_KEY])  # partner mail
        self.read(h2, thread="t2", actor=ANN, at=t0)
        h3 = self.mail("t3", OUT, ["ann@ours.example"], at=t0, marks='["finance"]')
        self.read(h3, thread="t3", actor=ANN, at=t0)
        self.cycle()

        def door(keys, held):     # the API's check: every audience key and marking held
            return lambda items: {i["item_id"] for i in items if set(i["audiences"]) <= keys
                                  and set(i["markings"]) <= held and i["citations"]}
        staff = door({f"mail:{ANN}"}, set())
        lead = door({f"mail:{ANN}"}, {"finance"})
        owner = door({f"mail:{ANN}", OWNER_KEY}, {"finance"})
        subj = lambda got: sorted(i["item_id"] for i in got["items"])   # noqa: E731
        ids = {t: am.matter_id(ANN, "conversation", t) for t in ("t1", "t2", "t3")}
        self.assertEqual(subj(am.as_items(self.pg, [ANN], shown=True, door=staff, now=NOW)),
                         sorted([ids["t1"]]))
        self.assertEqual(subj(am.as_items(self.pg, [ANN], shown=True, door=lead, now=NOW)),
                         sorted([ids["t1"], ids["t3"]]))
        self.assertEqual(subj(am.as_items(self.pg, [ANN], shown=True, door=owner, now=NOW)),
                         sorted(ids.values()))
        # every reading's handle is cited, so the door decides the message itself
        item = am.as_items(self.pg, [ANN], shown=True, door=owner, now=NOW)["items"][0]
        self.assertTrue(item["citations"])

    def test_a_role_pool_matter_reaches_each_member(self):
        self.pg.execute(
            "INSERT INTO assist.matters (matter_id, owner_person, kind, subject_key, sources, "
            "basis_at, title, audiences) VALUES (%s, 'pool:quality', 'record', 'b1', '{}', %s, "
            "'Batch on hold', '[\"mc:x\"]')", (am.matter_id("pool:quality", "record", "b1"), NOW))
        for member in (ANN, BOB):
            got = am.as_items(self.pg, [member, "pool:quality"], shown=True,
                              door=self.everything, now=NOW)
            self.assertEqual([i["summary"] for i in got["items"]], ["Batch on hold"])

    def test_the_true20_check_samples_shown_peoples_matters_and_needs_hand_verdicts(self):
        import contextlib
        import io
        from operating import assist_checks as ac
        t0 = NOW - dt.timedelta(hours=1)
        for t, who in (("t1", ANN), ("t2", BOB)):
            h = self.mail(t, OUT, [f"{who[2:]}@ours.example"], at=t0)
            self.read(h, thread=t, actor=who, at=t0)
        self.cycle()
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / "autopilot-write" / "autopilot-writes" / "audit.jsonl"
            journal.parent.mkdir(parents=True)
            from operating.card_actions import Journal
            with Journal(journal).locked() as held:        # the journal's own hash chain
                held.append({"event": "opt_in", "person": "ann@ours.example",
                             "by": "ann@ours.example", "workflow": "mail_thread",
                             "on": True, "at": "2026-11-20T00:00:00Z"})
            labels = Path(d) / "labels.jsonl"
            labels.write_text(json.dumps({"matter_id": am.matter_id(ANN, "conversation", "t1"),
                                          "true": True}) + "\n")
            for lab, rc_want in ((None, 2), (labels, 1)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        mock.patch("operating.autopilot_engine.load_draft", return_value={}):
                    rc, _ = ac.as3_true20(self.pg, state=Path(d), labels=lab)
                last = out.getvalue().strip().splitlines()[-1]
                self.assertEqual(rc, rc_want, last)
            # Bob is not shown: only Ann's one matter is drawn; 1 of 1 true, short of 20
            self.assertTrue(last.startswith("as3_true20 1/1 fail"), last)
            self.assertIn("only 1 open matters", last)

    # ---------------------------------------------------------------- 1 / 10 / 60

    def test_statements_do_not_grow_with_the_number_of_threads(self):
        counts, seconds = {}, {}
        for n in (1, 10, 60):
            self.setUp()
            for i in range(n):
                at = NOW - dt.timedelta(hours=1, minutes=i)
                h = self.mail(f"t{i}", OUT, ["ann@ours.example"], at=at)
                self.read(h, thread=f"t{i}", actor=ANN, at=at)
                self.mail(f"t{i}", "ann@ours.example", [OUT], at=at + dt.timedelta(minutes=1)) \
                    if i % 2 else None
            pg = Counting(self.pg)
            started = time.monotonic()
            self.cycle(pg)
            self.cycle(pg)
            got = am.as_items(pg, [ANN], shown=True, door=self.everything, now=NOW)
            seconds[n] = time.monotonic() - started
            counts[n] = pg.n
            self.assertEqual(len(got["items"]), n - n // 2)
        self.assertEqual(counts[1], counts[10])
        self.assertEqual(counts[1], counts[60])
        self.assertLess(seconds[60], 3 * seconds[1] + 1.0, seconds)


if __name__ == "__main__":
    unittest.main()
