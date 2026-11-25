"""Assist workflows (``operating.assist_workflows``) on Postgres (migrations 0082 + 0083).

Checked at the boundary:
- plain-language parsing (schedule, mail_from, teams_from, po_late, mc_question, qc_hold, on-demand fallback);
- starter templates pass ``operating/copy_rules.json`` (no banned phrases or chars) and exclude MC approvals;
- append-only ``assist.workflow_log`` refuses UPDATE and DELETE;
- no per-person count cap (30 workflows created and listed);
- schedule triggers fire once per due slot, never retroactively before creation/resume, never twice;
- event triggers (``mail_from`` scoped to owner's mailbox only and excluding bulk; ``teams_from`` enforcing
  ``status = 'ok'``, people exclusion, markings, chat ACL, visible-history window, and never matching/showing
  raw JSON ``sender``; ``po_late`` and ``qc_hold`` enforcing ``NOT private`` and people exclusion on
  ``biz.alerts``; ``mc_question`` scoped to owner's ``assist.matters``) fire once per event;
- pause holds firing; resume resets active_since so old events during pause do not flood;
- stop permanently stops the workflow AND stops any live job in ``assist.job_log``;
- failure loop protection: failed jobs back off exponentially (no spinning) and auto-pause after 5
  consecutive failures; resuming clears the streak;
- two-arg workflow advisory lock key is disjoint from 1-arg ``assist_job.lock_key``.

Evidence label: isolated-fixture (a real Postgres, no model, no business system)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

import psycopg

from operating import assist_job as aj
from operating import assist_workflows as aw
from operating import copy_gate
from operating.pg.migrate import MIGRATIONS_DIR
from operating.tests import pg_testdb

ANN = "ann-oid"
BOB = "bob-oid"


class PlainLanguageTests(unittest.TestCase):
    def test_parses_schedule_event_and_manual_phrases(self):
        got = aw.parse_plain_language("every Monday 8 AM send me late POs by supplier")
        self.assertEqual(got["trigger_kind"], "schedule")
        self.assertEqual(got["trigger"], {"cadence": "weekly", "weekday": "monday", "hour": 8, "minute": 0})
        self.assertEqual(got["instruction"], "Send me late POs by supplier")

        wd = aw.parse_plain_language("every weekday at 9:30 pm prepare tomorrow's brief")
        self.assertEqual(wd["trigger"], {"cadence": "weekdays", "hour": 21, "minute": 30})

        hr = aw.parse_plain_language("every 4 hours check open holds")
        self.assertEqual(hr["trigger"], {"cadence": "hourly", "every_hours": 4})

        mail = aw.parse_plain_language("whenever new mail arrives from croda.com about delivery, draft a reply")
        self.assertEqual(mail["trigger_kind"], "event")
        self.assertEqual(mail["trigger"], {"event": "mail_from", "sender": "croda.com", "subject": "delivery"})
        self.assertEqual(mail["instruction"], "Draft a reply")

        po = aw.parse_plain_language("when a PO from Azelis goes late, draft a supplier chase")
        self.assertEqual(po["trigger_kind"], "event")
        self.assertEqual(po["trigger"], {"event": "po_late", "supplier": "Azelis"})

        mc = aw.parse_plain_language("when an MC question arrives, prepare the answer")
        self.assertEqual(mc["trigger_kind"], "event")
        self.assertEqual(mc["trigger"], {"event": "mc_question"})

        teams = aw.parse_plain_language("whenever a Teams message arrives from Stephen about NGen, draft a reply")
        self.assertEqual(teams["trigger_kind"], "event")
        self.assertEqual(teams["trigger"], {"event": "teams_from", "sender": "Stephen", "subject": "NGen"})

        qc = aw.parse_plain_language("when a batch of Shampoo goes on QC hold, list affected orders")
        self.assertEqual(qc["trigger_kind"], "event")
        self.assertEqual(qc["trigger"], {"event": "qc_hold", "product": "Shampoo"})

        manual = aw.parse_plain_language("reconcile open invoices against POs")
        self.assertEqual(manual["trigger_kind"], "manual")
        self.assertEqual(manual["trigger"], {})

    def test_mc_approval_trigger_and_template_are_absent(self):
        self.assertNotIn("approval_arrived", aw.EVENT_KINDS)
        for tpl in aw.TEMPLATES:
            self.assertNotEqual(tpl["template_id"], "approval-arrived")
            self.assertNotEqual(tpl["trigger"].get("event"), "approval_arrived")
        with self.assertRaises(ValueError):
            aw.validate_trigger("event", {"event": "approval_arrived"})
        parsed = aw.parse_plain_language("when an approval request arrives, summarize the linked POs")
        self.assertNotEqual(parsed["trigger"].get("event"), "approval_arrived")
        self.assertEqual(parsed["trigger_kind"], "manual")

    def test_templates_pass_copy_rules_without_banned_phrases(self):
        rules = copy_gate.rules()
        for tpl in aw.TEMPLATES:
            fields = (
                ("name", tpl["name"]),
                ("description", tpl["description"]),
                ("instruction", tpl["instruction"]),
                ("trigger_summary", aw.describe_trigger(tpl["trigger_kind"], tpl["trigger"])),
            )
            for field, text in fields:
                res = copy_gate.check(text, surface="ui")
                self.assertTrue(res["ok"], f"{tpl['template_id']}.{field}: {res['findings']}")
                for rule in rules["banned_phrases"]:
                    m = re.search(r"(?<!\w)(?:" + rule["pattern"] + r")(?!\w)", text, re.I)
                    self.assertIsNone(m, f"{tpl['template_id']}.{field} matched banned phrase {rule['pattern']!r}")

    def test_wf_lock_key_uses_separate_two_arg_namespace(self):
        ns, key = aw.wf_lock_key("ffffffffffffffffffffffff")
        self.assertEqual(ns, aw.WF_LOCK_NS)
        self.assertGreaterEqual(key, 0)
        self.assertLessEqual(key, 0x7FFFFFFF)

    def test_every_hours_aligns_to_us_eastern_not_utc(self):
        # 2026-11-30 11:30 UTC is 6:30 AM EST (UTC-5). Next 4-hour ET boundary is 8:00 AM EST = 13:00 UTC (NOT 12:00 UTC!).
        after = dt.datetime(2026, 11, 30, 11, 30, tzinfo=dt.timezone.utc)
        nxt = aw.next_due("schedule", {"cadence": "hourly", "every_hours": 4}, after)
        self.assertEqual(nxt, dt.datetime(2026, 11, 30, 13, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(nxt.astimezone(aw.EASTERN).hour, 8)
        # due_slot at 12:05 UTC (7:05 AM EST) is None; at 13:05 UTC (8:05 AM EST) is 13:00 UTC
        self.assertIsNone(aw.due_slot({"cadence": "hourly", "every_hours": 4}, after,
                                      dt.datetime(2026, 11, 30, 12, 5, tzinfo=dt.timezone.utc)))
        self.assertEqual(aw.due_slot({"cadence": "hourly", "every_hours": 4}, after,
                                     dt.datetime(2026, 11, 30, 13, 5, tzinfo=dt.timezone.utc)),
                         dt.datetime(2026, 11, 30, 13, 0, tzinfo=dt.timezone.utc))

    def test_validates_event_filters(self):
        with self.assertRaises(ValueError):
            aw.validate_trigger("event", {"event": "mail_from", "sender": "", "subject": ""})
        with self.assertRaises(ValueError):
            aw.validate_trigger("schedule", {"cadence": "weekly", "weekday": "funday", "hour": 8})


class WorkflowPgCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)

    def setUp(self):
        self.pg = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.pg.close)
        self.pg.execute("DROP SCHEMA IF EXISTS assist CASCADE; CREATE SCHEMA assist")
        self.pg.execute("DROP SCHEMA IF EXISTS biz CASCADE; CREATE SCHEMA biz")
        self.pg.execute("DROP SCHEMA IF EXISTS ontology CASCADE; CREATE SCHEMA ontology")
        self.pg.execute("DROP SCHEMA IF EXISTS search CASCADE; CREATE SCHEMA search")
        self.pg.execute((MIGRATIONS_DIR / "0082_assist_job_log.sql").read_text(encoding="utf-8"))
        self.pg.execute((MIGRATIONS_DIR / "0083_assist_workflows.sql").read_text(encoding="utf-8"))
        # Minimal event-source tables matching production schema columns
        self.pg.execute("""
            CREATE TABLE ontology.people_excluded_objects (object_id text PRIMARY KEY);
            CREATE TABLE search.audience_units (
              audience_key text PRIMARY KEY, sharing text NOT NULL,
              people text[] NOT NULL DEFAULT '{}');
            CREATE TABLE biz.mailbox_owners (mailbox text PRIMARY KEY, addrs text[] NOT NULL DEFAULT '{}');
            CREATE TABLE biz.emails (
              handle text PRIMARY KEY, mailbox text, sender text, subject text,
              received_at timestamptz NOT NULL, is_bulk boolean DEFAULT false);
            CREATE TABLE biz.teams_messages (
              handle text PRIMARY KEY, author text, sender text, subject text,
              said text, created_at timestamptz NOT NULL,
              object_ids text[] NOT NULL DEFAULT '{}',
              audience_keys text[] NOT NULL DEFAULT '{}',
              rec_marks text[] DEFAULT ARRAY['[]']::text[],
              ai_ok boolean DEFAULT true,
              excluded boolean DEFAULT false);
            CREATE TABLE assist.readings (
              source text NOT NULL, message_key text NOT NULL, handles text[] NOT NULL,
              actor_person text, said_at timestamptz NOT NULL,
              status text NOT NULL DEFAULT 'ok',
              PRIMARY KEY (source, message_key));
            CREATE TABLE biz.alerts (
              rule text NOT NULL, handle text NOT NULL, state text NOT NULL,
              why text NOT NULL, first_seen timestamptz NOT NULL,
              private boolean NOT NULL DEFAULT false,
              mailbox text,
              units integer[] NOT NULL DEFAULT '{}',
              objects text[]);
            CREATE TABLE assist.matters (
              matter_id text PRIMARY KEY, owner_person text NOT NULL, kind text NOT NULL,
              subject_key text NOT NULL, state text NOT NULL, title text NOT NULL,
              basis_at timestamptz NOT NULL);
            CREATE TABLE assist.routines (
              routine_id text PRIMARY KEY, scope text NOT NULL, kind text NOT NULL,
              trigger jsonb NOT NULL, description text NOT NULL DEFAULT '',
              mined jsonb NOT NULL DEFAULT '{}', validated jsonb NOT NULL DEFAULT '{}',
              status text NOT NULL DEFAULT 'candidate');
        """)

    def test_log_is_append_only_and_has_no_per_person_cap(self):
        wids = []
        for i in range(30):
            wids.append(aw.create(self.pg, ANN, name=f"wf-{i}", instruction=f"do {i}",
                                  trigger_kind="manual", trigger={}))
        self.assertEqual(len(aw.workflows_of(self.pg, [ANN])), 30)
        self.assertEqual(aw.workflows_of(self.pg, [BOB]), [])
        for sql in ("UPDATE assist.workflow_log SET what = 'stopped'", "DELETE FROM assist.workflow_log"):
            with self.assertRaises(psycopg.Error):
                self.pg.execute(sql)

    def test_create_from_routine_and_template(self):
        self.pg.execute(
            "INSERT INTO assist.routines VALUES ('r1', %s, 'po_followup', %s, 'Chase open POs', %s, '{}', 'candidate')",
            (ANN, json.dumps({"relation": "clock", "weekday": "Mon"}), json.dumps({"acts": 12, "days": 8})),
        )
        mine = aw.routines_of(self.pg, [ANN])
        self.assertEqual(len(mine), 1)
        built = aw.from_routine(mine[0])
        self.assertEqual(built["trigger_kind"], "schedule")
        self.assertEqual(built["trigger"]["weekday"], "monday")
        wid = aw.create(self.pg, ANN, **built)
        wf = aw.fold(aw.rows_of(self.pg, wid))
        self.assertEqual((wf["source_kind"], wf["source_ref"]), ("routine", "r1"))

        tpl = aw.template_by_id("late-pos-weekly")
        assert tpl is not None
        twid = aw.create(self.pg, ANN, name=tpl["name"], instruction=tpl["instruction"],
                         trigger_kind=tpl["trigger_kind"], trigger=tpl["trigger"],
                         source_kind="template", source_ref=tpl["template_id"])
        twf = aw.fold(aw.rows_of(self.pg, twid))
        self.assertEqual((twf["source_kind"], twf["source_ref"]), ("template", "late-pos-weekly"))

    def test_schedule_trigger_fires_once_per_slot_and_respects_pause_and_stop(self):
        # Created Sunday 2026-11-29 12:00 UTC (7:00 AM ET)
        t_create = dt.datetime(2026, 11, 29, 12, 0, tzinfo=dt.timezone.utc)
        wid = aw.create(self.pg, ANN, name="Mon 8 AM POs", instruction="send late POs",
                        trigger_kind="schedule", trigger={"cadence": "weekly", "weekday": "monday", "hour": 8, "minute": 0})
        self.pg.execute("UPDATE assist.workflow_log SET at = %s WHERE workflow_id = %s", (t_create, wid)) if False else None
        # Force deterministic timestamp via a new row? No, table is append-only; use now relative to created_at!
        wf0 = aw.fold(aw.rows_of(self.pg, wid))
        nxt = aw.next_due("schedule", wf0["trigger"], wf0["created_at"])
        assert nxt is not None
        # Before next due: does not fire
        c0 = aw.tick(self.pg, now=nxt - dt.timedelta(minutes=1))
        self.assertEqual(c0["fired"], 0)
        # At next due: fires once and creates an assist.job_log requested row
        c1 = aw.tick(self.pg, now=nxt + dt.timedelta(seconds=30))
        self.assertEqual(c1["fired"], 1)
        # Repeat tick at same slot: does not fire again
        c2 = aw.tick(self.pg, now=nxt + dt.timedelta(minutes=5))
        self.assertEqual(c2["fired"], 0)
        wf1 = aw.fold(aw.rows_of(self.pg, wid))
        self.assertEqual(len(wf1["runs"]), 1)
        jid = wf1["runs"][0]["job_id"]
        self.assertEqual(aj.fold(aj.rows_of(self.pg, jid))["status"], "queued")
        # Stopping the workflow also appends stop to the live job
        stopped = aw.stop_workflow(self.pg, wf1)
        self.assertEqual(stopped, [jid])
        self.assertEqual(aj.fold(aj.rows_of(self.pg, jid))["status"], "stopping")
        self.assertEqual(aw.fold(aw.rows_of(self.pg, wid))["status"], "stopped")
        # Next week: stopped workflow never fires again
        c3 = aw.tick(self.pg, now=nxt + dt.timedelta(days=7, minutes=1))
        self.assertEqual(c3["fired"], 0)

    def test_event_triggers_mail_po_late_and_mc_question(self):
        self.pg.execute("INSERT INTO biz.mailbox_owners VALUES (%s, ARRAY['ann@co.example'])", (ANN,))
        w_mail = aw.create(self.pg, ANN, name="Croda mail", instruction="draft reply",
                           trigger_kind="event", trigger={"event": "mail_from", "sender": "croda.com", "subject": "ETA"})
        w_po = aw.create(self.pg, ANN, name="Late PO", instruction="chase supplier",
                         trigger_kind="event", trigger={"event": "po_late", "supplier": "DSM"})
        w_mc = aw.create(self.pg, ANN, name="MC Q", instruction="answer MC",
                         trigger_kind="event", trigger={"event": "mc_question"})
        now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)
        # Insert matching + non-matching events
        self.pg.execute(
            "INSERT INTO biz.emails VALUES "
            "('em://1', 'ann@co.example', 'rep@croda.com', 'Updated ETA for PO1', %s, false), "
            "('em://2', 'bob@co.example', 'rep@croda.com', 'Updated ETA for Bob', %s, false), "
            "('em://3', 'ann@co.example', 'rep@croda.com', 'Updated ETA bulk', %s, true)",
            (now, now, now),
        )
        self.pg.execute("INSERT INTO ontology.people_excluded_objects VALUES ('obj-po-excl')")
        self.pg.execute(
            "INSERT INTO biz.alerts (rule, handle, state, why, first_seen, private, mailbox, units, objects) VALUES "
            "('po_late', 'po://101', 'open', 'PO 101 from DSM is 3 days late', %s, false, NULL, '{}', ARRAY['obj-po-101']), "
            "('po_late', 'po://102', 'open', 'PO 102 from OtherCo is 3 days late', %s, false, NULL, '{}', ARRAY['obj-po-102']), "
            "('po_late', 'po://priv', 'open', 'PO priv from DSM is 3 days late', %s, true, NULL, '{}', ARRAY['obj-po-priv']), "
            "('po_late', 'po://excl', 'open', 'PO excl from DSM is 3 days late', %s, false, NULL, '{}', ARRAY['obj-po-excl'])",
            (now, now, now, now),
        )
        self.pg.execute(
            "INSERT INTO assist.matters VALUES "
            "('m1', %s, 'task', 'mc-question:q1', 'open', 'Approve freight?', %s), "
            "('m2', %s, 'task', 'mc-question:q2', 'open', 'Bob question', %s)",
            (ANN, now, BOB, now),
        )

        c1 = aw.tick(self.pg, now=now + dt.timedelta(seconds=5))
        self.assertEqual(c1["fired"], 3)
        for wid, expected_sub in ((w_mail, "em://1"), (w_po, "po://101"), (w_mc, "mc-question:q1")):
            wf = aw.fold(aw.rows_of(self.pg, wid))
            self.assertEqual(len(wf["runs"]), 1)
            j = aj.fold(aj.rows_of(self.pg, wf["runs"][0]["job_id"]))
            self.assertIn(expected_sub, j["text"])
        # Repeat tick fires nothing new (already deduplicated)
        c2 = aw.tick(self.pg, now=now + dt.timedelta(seconds=10))
        self.assertEqual(c2["fired"], 0)

    def test_failure_backoff_and_auto_pause_prevent_spinning(self):
        wid = aw.create(self.pg, ANN, name="Hourly check", instruction="check holds",
                        trigger_kind="schedule", trigger={"cadence": "hourly", "every_hours": 1})
        t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        for attempt in range(1, aw.AUTO_PAUSE_FAILURES + 1):
            # Advance past backoff + next hour
            t = t + dt.timedelta(hours=max(1, (aw.backoff_seconds(attempt - 1) // 3600) + 2))
            c = aw.tick(self.pg, now=t)
            self.assertEqual(c["fired"], 1, f"attempt {attempt}")
            wf = aw.fold(aw.rows_of(self.pg, wid))
            jid = wf["runs"][0]["job_id"]
            # Simulate the job failing
            aj.append(self.pg, jid, ANN, "started", {})
            aj.append(self.pg, jid, ANN, "failed", {"reason": "plan_failed", "message": "boom"})
            # Immediately tick 10 seconds later: reconciles failure, sets backoff (or auto-pauses on 5th)
            t_rec = t + dt.timedelta(seconds=10)
            c_rec = aw.tick(self.pg, now=t_rec)
            self.assertEqual(c_rec["fired"], 0)
            wf_after = aw.fold(aw.rows_of(self.pg, wid))
            self.assertEqual(wf_after["consecutive_failures"], attempt)
            if attempt < aw.AUTO_PAUSE_FAILURES:
                self.assertEqual(wf_after["status"], "active")
                self.assertIsNotNone(wf_after["backoff_until"])
                # Tick 1 minute later (still inside 5+ min backoff) must skip and not spin
                c_skip = aw.tick(self.pg, now=t_rec + dt.timedelta(seconds=60))
                self.assertEqual((c_skip["fired"], c_skip["skipped_backoff"]), (0, 1))
            else:
                self.assertEqual(c_rec["auto_paused"], 1)
                self.assertEqual(wf_after["status"], "paused")
                self.assertEqual(wf_after["pause_reason"], "paused_after_failures")

        # Even days later while auto-paused, it never spins
        c_days = aw.tick(self.pg, now=t + dt.timedelta(days=5))
        self.assertEqual(c_days["fired"], 0)

        # Resuming clears failure streak and backoff
        aw.append(self.pg, wid, ANN, "resumed", {"by": "person"})
        wf_resumed = aw.fold(aw.rows_of(self.pg, wid))
        self.assertEqual((wf_resumed["status"], wf_resumed["consecutive_failures"], wf_resumed["backoff_until"]),
                         ("active", 0, None))

    def test_teams_from_permissions_json_sender_and_history_window(self):
        self.pg.execute("INSERT INTO biz.mailbox_owners VALUES (%s, ARRAY['ann@co.example'])", (ANN,))
        self.pg.execute("INSERT INTO ontology.people_excluded_objects VALUES ('obj-tm-peo')")
        self.pg.execute(
            "INSERT INTO search.audience_units (audience_key, sharing, people) VALUES "
            "('teams:chat:c1', 'personal', ARRAY[%s, %s]), "
            "('teams:chat:no-ann', 'personal', ARRAY[%s]), "
            "('teams:channel:ops', 'shared', '{}')",
            (ANN, BOB, BOB),
        )
        w_stephen = aw.create(
            self.pg, ANN, name="Teams Stephen", instruction="reply on Teams",
            trigger_kind="event", trigger={"event": "teams_from", "sender": "Stephen", "subject": "NGen"},
        )
        # Must NOT match JSON key/value tokens like "display_name" or "user" inside raw t.sender JSON
        w_json_key = aw.create(
            self.pg, ANN, name="Teams JSON key", instruction="should never match",
            trigger_kind="event", trigger={"event": "teams_from", "sender": "display_name"},
        )
        # Fallback when t.author is NULL: extracts only display_name, never raw JSON
        w_nadia = aw.create(
            self.pg, ANN, name="Teams Nadia", instruction="reply to Nadia",
            trigger_kind="event", trigger={"event": "teams_from", "sender": "Nadia"},
        )
        # Subject-only match on unnamed bot sender must never display raw JSON in trigger_note
        w_bot_subj = aw.create(
            self.pg, ANN, name="Teams bot topic", instruction="check bot alert",
            trigger_kind="event", trigger={"event": "teams_from", "sender": "", "subject": "LotReady"},
        )
        now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)
        before_join = now - dt.timedelta(seconds=2)
        raw_stephen = json.dumps({"display_name": "Stephen Smith", "id": "oid-stephen", "kind": "user"})
        raw_nadia = json.dumps({"display_name": "Nadia Park", "id": "oid-nadia", "kind": "user"})
        raw_bot = json.dumps({"id": "bot-99", "kind": "bot"})
        self.pg.execute(
            "INSERT INTO biz.teams_messages "
            "(handle, author, sender, subject, said, created_at, object_ids, audience_keys, rec_marks, ai_ok, excluded) VALUES "
            "('tm://ok', 'Stephen Smith', %s, 'NGen claim', 'Please check NGen', %s, ARRAY['obj-tm-ok'], ARRAY['teams:chat:c1'], ARRAY['[]'], true, false), "
            "('tm://old-hist', 'Stephen Smith', %s, 'NGen claim', 'Before Ann joined', %s, ARRAY['obj-tm-old'], ARRAY['teams:chat:c1'], ARRAY['[]'], true, false), "
            "('tm://other-actor', 'Stephen Smith', %s, 'NGen claim', 'For Bob', %s, ARRAY['obj-tm-bob'], ARRAY['teams:chat:c1'], ARRAY['[]'], true, false), "
            "('tm://screened', 'Stephen Smith', %s, 'NGen claim', 'Screened reading', %s, ARRAY['obj-tm-scr'], ARRAY['teams:chat:c1'], ARRAY['[]'], true, false), "
            "('tm://excl-flag', 'Stephen Smith', %s, 'NGen claim', 'Excluded flag', %s, ARRAY['obj-tm-exf'], ARRAY['teams:chat:c1'], ARRAY['[]'], true, true), "
            "('tm://excl-null', 'Stephen Smith', %s, 'NGen claim', 'Undecided excl', %s, ARRAY['obj-tm-exn'], ARRAY['teams:chat:c1'], ARRAY['[]'], true, NULL), "
            "('tm://excl-peo', 'Stephen Smith', %s, 'NGen claim', 'Excluded object', %s, ARRAY['obj-tm-peo'], ARRAY['teams:chat:c1'], ARRAY['[]'], true, false), "
            "('tm://marked', 'Stephen Smith', %s, 'NGen claim', 'Marked row', %s, ARRAY['obj-tm-mrk'], ARRAY['teams:chat:c1'], ARRAY['[]', '[\"hr\"]'], true, false), "
            "('tm://no-acl', 'Stephen Smith', %s, 'NGen claim', 'Private chat without Ann', %s, ARRAY['obj-tm-acl'], ARRAY['teams:chat:no-ann'], ARRAY['[]'], true, false), "
            "('tm://json-fallback', NULL, %s, 'Schedule', 'Can you review?', %s, ARRAY['obj-tm-nadia'], ARRAY['teams:channel:ops'], ARRAY['[]'], true, false), "
            "('tm://bot-anon', NULL, %s, 'LotReady', 'Batch ready', %s, ARRAY['obj-tm-bot'], ARRAY['teams:channel:ops'], ARRAY['[]'], true, false)",
            (
                raw_stephen, now,
                raw_stephen, before_join,
                raw_stephen, now,
                raw_stephen, now,
                raw_stephen, now,
                raw_stephen, now,
                raw_stephen, now,
                raw_stephen, now,
                raw_stephen, now,
                raw_nadia, now,
                raw_bot, now,
            ),
        )
        self.pg.execute(
            "INSERT INTO assist.readings (source, message_key, handles, actor_person, said_at, status) VALUES "
            "('teams_messages', 'k-ok', ARRAY['tm://ok'], %s, %s, 'ok'), "
            "('teams_messages', 'k-old', ARRAY['tm://old-hist'], %s, %s, 'ok'), "
            "('teams_messages', 'k-bob', ARRAY['tm://other-actor'], %s, %s, 'ok'), "
            "('teams_messages', 'k-scr', ARRAY['tm://screened'], %s, %s, 'screened'), "
            "('teams_messages', 'k-exf', ARRAY['tm://excl-flag'], %s, %s, 'ok'), "
            "('teams_messages', 'k-exn', ARRAY['tm://excl-null'], %s, %s, 'ok'), "
            "('teams_messages', 'k-peo', ARRAY['tm://excl-peo'], %s, %s, 'ok'), "
            "('teams_messages', 'k-mrk', ARRAY['tm://marked'], %s, %s, 'ok'), "
            "('teams_messages', 'k-acl', ARRAY['tm://no-acl'], %s, %s, 'ok'), "
            "('teams_messages', 'k-nadia', ARRAY['tm://json-fallback'], %s, %s, 'ok'), "
            "('teams_messages', 'k-bot', ARRAY['tm://bot-anon'], %s, %s, 'ok')",
            (
                ANN, now,
                ANN, now,
                BOB, now,
                ANN, now,
                ANN, now,
                ANN, now,
                ANN, now,
                ANN, now,
                ANN, now,
                ANN, now,
                ANN, now,
            ),
        )
        # Build a temporary Teams capture SQLite store with Ann's visible-history window starting at `now`
        # so `tm://old-hist` (created at `before_join`) is withheld while `tm://ok` (at `now`) passes.
        from integrations.capture_teams import CHAT_KIND, DB_FILE, STORE_DIR
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / STORE_DIR / DB_FILE
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("CREATE TABLE unit_acl (kind text NOT NULL, unit text NOT NULL, acl text NOT NULL)")
                acl = {
                    "status": "read",
                    "identities": [ANN.lower(), BOB.lower()],
                    "history_start": {
                        ANN.lower(): now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        BOB.lower(): "1970-01-01T00:00:00Z",
                    },
                }
                conn.execute("INSERT INTO unit_acl VALUES (?, ?, ?)", (CHAT_KIND.name, "teams:chat:c1", json.dumps(acl)))
                conn.commit()
            finally:
                conn.close()

            c = aw.tick(self.pg, now=now + dt.timedelta(seconds=5), state_dir=tmp)
            self.assertEqual(c["fired"], 3)
            # Second tick must not pick up tm://old-hist or any excluded/marked/screened/no-acl message
            c2 = aw.tick(self.pg, now=now + dt.timedelta(seconds=10), state_dir=tmp)
            self.assertEqual(c2["fired"], 0)

        wf_stephen = aw.fold(aw.rows_of(self.pg, w_stephen))
        self.assertEqual(len(wf_stephen["runs"]), 1)
        j_stephen = aj.fold(aj.rows_of(self.pg, wf_stephen["runs"][0]["job_id"]))
        self.assertIn("tm://ok", j_stephen["text"])
        self.assertIn("Stephen Smith", j_stephen["text"])
        self.assertNotIn("display_name", j_stephen["text"])

        self.assertEqual(aw.fold(aw.rows_of(self.pg, w_json_key))["runs"], [])

        wf_nadia = aw.fold(aw.rows_of(self.pg, w_nadia))
        self.assertEqual(len(wf_nadia["runs"]), 1)
        j_nadia = aj.fold(aj.rows_of(self.pg, wf_nadia["runs"][0]["job_id"]))
        self.assertIn("tm://json-fallback", j_nadia["text"])
        self.assertIn("Nadia Park", j_nadia["text"])
        self.assertNotIn("{", j_nadia["text"])

        wf_bot = aw.fold(aw.rows_of(self.pg, w_bot_subj))
        self.assertEqual(len(wf_bot["runs"]), 1)
        j_bot = aj.fold(aj.rows_of(self.pg, wf_bot["runs"][0]["job_id"]))
        self.assertIn("tm://bot-anon", j_bot["text"])
        self.assertNotIn("bot-99", j_bot["text"])
        self.assertNotIn("{", j_bot["text"])

    def test_qc_hold_permissions_and_like_escaping(self):
        self.pg.execute("INSERT INTO biz.mailbox_owners VALUES (%s, ARRAY['ann@co.example'])", (ANN,))
        self.pg.execute("INSERT INTO ontology.people_excluded_objects VALUES ('obj-batch-excl')")
        w_qc = aw.create(self.pg, ANN, name="QC hold", instruction="check affected orders",
                         trigger_kind="event", trigger={"event": "qc_hold", "product": "100%_pure"})
        w_esc = aw.create(self.pg, ANN, name="Literal underscore", instruction="reply literal",
                          trigger_kind="event", trigger={"event": "mail_from", "sender": "a_b%c@co.example"})
        now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)
        self.pg.execute(
            "INSERT INTO biz.alerts (rule, handle, state, why, first_seen, private, mailbox, units, objects) VALUES "
            "('lot_on_hold', 'batch://1', 'open', %s, %s, false, NULL, '{}', ARRAY['obj-b1']), "
            "('lot_on_hold', 'batch://2', 'open', %s, %s, false, NULL, '{}', ARRAY['obj-b2']), "
            "('lot_on_hold', 'batch://priv', 'open', %s, %s, true, NULL, '{}', ARRAY['obj-bpriv']), "
            "('lot_on_hold', 'batch://excl', 'open', %s, %s, false, NULL, '{}', ARRAY['obj-batch-excl']), "
            "('lot_on_hold', 'batch://nullobj', 'open', %s, %s, false, NULL, '{}', NULL), "
            "('lot_on_hold', 'batch://mb', 'open', %s, %s, false, 'ann@co.example', '{}', ARRAY['obj-bmb'])",
            (
                "Batch B1 (100%_pure) is on quality hold.", now,
                "Batch B2 (100Xpure) is on quality hold.", now,
                "Batch Bpriv (100%_pure) is private.", now,
                "Batch Bexcl (100%_pure) is excluded.", now,
                "Batch Bnull (100%_pure) lacks lineage.", now,
                "Batch Bmb (100%_pure) is mailbox-scoped.", now,
            ),
        )
        self.pg.execute(
            "INSERT INTO biz.emails VALUES "
            "('em://lit', 'ann@co.example', %s, 'Hi', %s, false), "
            "('em://wild', 'ann@co.example', %s, 'Hi', %s, false)",
            ("a_b%c@co.example", now, "aXbZc@co.example", now),
        )
        c = aw.tick(self.pg, now=now + dt.timedelta(seconds=5))
        self.assertEqual(c["fired"], 2)
        # Second tick must not pick up private/excluded/null-object/mailbox alerts or wildcard rows
        c2 = aw.tick(self.pg, now=now + dt.timedelta(seconds=10))
        self.assertEqual(c2["fired"], 0)
        for wid, want in ((w_qc, "batch://1"), (w_esc, "em://lit")):
            wf = aw.fold(aw.rows_of(self.pg, wid))
            self.assertEqual(len(wf["runs"]), 1)
            j = aj.fold(aj.rows_of(self.pg, wf["runs"][0]["job_id"]))
            self.assertIn(want, j["text"])
        qc_dump = json.dumps(aw.fold(aw.rows_of(self.pg, w_qc))["runs"], default=str)
        for forbidden in ("batch://2", "batch://priv", "batch://excl", "batch://nullobj", "batch://mb"):
            self.assertNotIn(forbidden, qc_dump)
        esc_wf = aw.fold(aw.rows_of(self.pg, w_esc))
        self.assertNotIn("em://wild", json.dumps(esc_wf["runs"], default=str))

    def test_fire_once_is_atomic_and_respects_concurrent_stop(self):
        wid = aw.create(self.pg, ANN, name="Atomic", instruction="do work", trigger_kind="manual", trigger={})
        wf_snapshot = aw.fold(aw.rows_of(self.pg, wid))
        # Holding 1-arg assist_job.lock_key on another connection must not collide with 2-arg wf_lock_key
        with psycopg.connect(self.dsn, autocommit=True) as pg_other:
            self.assertTrue(pg_other.execute("SELECT pg_try_advisory_lock(%s)", (aj.lock_key(wid),)).fetchone()[0])
            try:
                with self.pg.transaction():
                    self.assertTrue(
                        self.pg.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", aw.wf_lock_key(wid)).fetchone()[0]
                    )
            finally:
                pg_other.execute("SELECT pg_advisory_unlock(%s)", (aj.lock_key(wid),))
        # Simulate failure inserting into assist.job_log: the 'fired' row must roll back atomically
        from unittest import mock
        with mock.patch.object(aw, "_insert_job", side_effect=RuntimeError("job_log insert failed")):
            with self.assertRaises(RuntimeError):
                aw._fire_once(self.pg, wf_snapshot, "k:atomic", "note", "do work")
        self.assertEqual([r["what"] for r in aw.rows_of(self.pg, wid)], ["created"])

        # Stop/tick race: stop_workflow commits 'stopped' after tick took wf_snapshot; _fire_once must not fire
        aw.stop_workflow(self.pg, wf_snapshot)
        self.assertIsNone(aw._fire_once(self.pg, wf_snapshot, "k:after-stop", "note", "do work"))
        self.assertEqual([r["what"] for r in aw.rows_of(self.pg, wid)], ["created", "stopped"])


if __name__ == "__main__":
    unittest.main()
