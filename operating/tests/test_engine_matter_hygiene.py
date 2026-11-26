"""Engine-level matter hygiene tests on Postgres:
1. Automated IT monitoring alerts (Ricardo's UAT BC sync / email-relay / Portal-to-BC sync
   and Theo's Workbench API staging health & cos@petrasoap.com PR merge-class gates) are
   classified as ``system_noise`` and never open employee matters, and existing open noise
   matters are retired safely and idempotently.
2. Same-message / same-moment ``promise`` + ``conversation`` pairs (Vince's 8oz template and
   lunch order scenarios) merge into ONE enriched ``conversation`` matter instead of spawning
   two duplicate open rows, while unrelated promises stay open.
3. ``wait`` matters store plain-English titles naming what is waited for and always populate
   ``counterpart``, never falling back to 'No answer yet from them' when topic or person is
   known.
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import json
import unittest

from operating import assist_matters as am
from operating import assist_readings as ar
from operating import assist_waits as aw
from operating.pg import alerts as pg_alerts
from operating.tests import test_assist_matters as base

NOW = dt.datetime(2026, 11, 25, 16, 0, tzinfo=dt.timezone.utc)
RICARDO = "p-ricardo"
THEO = "p-theo"
VINCE = "p-vince"
GREG = "p-greg"


class EngineMatterHygiene(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    biz_tables = base.Matters.biz_tables

    def setUp(self):
        base.Matters.setUp(self)
        for pid, email, name in (
            (RICARDO, "ricardo@petrasoap.com", "Ricardo Silva"),
            (THEO, "theo@petrasoap.com", "Theo Park"),
            (VINCE, "vince@petrasoap.com", "Vince Russo"),
            (GREG, "greg@petrasoap.com", "Greg Martin"),
        ):
            self.pg.execute("INSERT INTO biz.staff VALUES (%s, %s, %s)", (pid, email, name))
            self.pg.execute("INSERT INTO biz.mailbox_owners VALUES (%s, %s)", (pid, [email]))

    def mail(self, thread, sender, to, *, subject="Business update", box=VINCE, at=NOW):
        self.n += 1
        h = f"mail://{thread}/{self.n}"
        self.pg.execute(
            "INSERT INTO biz.emails VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (h, f"<m{self.n}>", thread, box, sender, list(to), [], subject, at,
             [f"mail:{box}"], ["[]"], [f"o:{self.n}"]),
        )
        return h

    def insert_reading(
        self,
        handle,
        *,
        source="emails",
        thread,
        at,
        actor=None,
        sender_person=None,
        needs=True,
        act="Follow up",
        act_class="reply",
        summary="",
        quote="see details",
        promise="",
        waiting_on="",
        counterpart="",
        due=None,
        key=None,
    ):
        self.pg.execute(
            "INSERT INTO assist.readings (source, message_key, handles, thread_key, said_at, "
            "read_ver, needs_act, act, act_class, summary, quote, status, actor_person, "
            "actor_basis, sender_person, promise, waiting_on, counterpart, due) "
            "VALUES (%s, %s, %s, %s, %s, 2, %s, %s, %s, %s, %s, 'ok', %s, %s, %s, %s, %s, %s, %s)",
            (
                source, key or handle, [handle], thread, at, needs, act, act_class, summary,
                quote, actor, "address" if actor else "", sender_person, promise, waiting_on,
                counterpart, due,
            ),
        )

    def cycle(self):
        out = am.cycle(self.pg, now=NOW, environ={})
        self.assertEqual(out["status"], "ok", out)
        return out

    # ---------------------------------------------------------------- 1. System noise at source

    def test_automated_it_alerts_are_skipped_at_ingestion_for_ricardo_and_theo(self):
        t0 = NOW - dt.timedelta(hours=1)
        ricardo_alerts = [
            ("r-uat1", "cos@petrasoap.com", "UAT BC sync alert",
             "Automated alert: the UAT Business Central sync has not run in 24 hours."),
            ("r-relay", "cos@petrasoap.com", "Staging email-relay failure",
             "Automated alert: the email-relay job failed on staging."),
            ("r-portal", "stage-rail@plxcustomer.io", "Portal-to-BC sync has not run",
             "Portal-to-BC sync has not run since 03:00 UTC."),
            ("r-quar", "quarantine@messaging.microsoft.com",
             "Microsoft 365 security: You have messages in quarantine",
             "Microsoft quarantine notice: 3 messages held."),
        ]
        for tid, sender, subj, summ in ricardo_alerts:
            h = self.mail(tid, sender, ["ricardo@petrasoap.com"], subject=subj, box=RICARDO, at=t0)
            self.insert_reading(h, thread=tid, at=t0, actor=RICARDO, summary=summ, act="Investigate alert")

        theo_alerts = [
            ("t-health", "cos@petrasoap.com", "Daily Workbench API health alert",
             "Automated daily health alert: eight Workbench API staging relations are unavailable."),
            ("t-merge", "cos@petrasoap.com", "Mission Control PR merge-class gate",
             "PR merge-class gate requires review before landing."),
        ]
        for tid, sender, subj, summ in theo_alerts:
            h = self.mail(tid, sender, ["theo@petrasoap.com"], subject=subj, box=THEO, at=t0)
            self.insert_reading(h, thread=tid, at=t0, actor=THEO, summary=summ, act="Check system")

        # Genuine business email from cos@petrasoap.com MUST still open a matter
        h_real = self.mail(
            "t-real",
            "cos@petrasoap.com",
            ["theo@petrasoap.com"],
            subject="[ACTION REQUIRED] New Sourcing Request: Bluzen",
            box=THEO,
            at=t0,
        )
        self.insert_reading(
            h_real,
            thread="t-real",
            at=t0,
            actor=THEO,
            summary="Bluzen requested sourcing approval for 10oz bottles.",
            act="Approve sourcing request",
            act_class="decide",
        )

        out = self.cycle()
        self.assertEqual(out["readings"]["system_noise_skipped"], 6)
        open_rows = self.pg.execute(
            "SELECT owner_person, subject_key, title FROM assist.matters WHERE state = 'open'"
        ).fetchall()
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0][0], THEO)
        self.assertEqual(open_rows[0][1], "t-real")

    def test_retire_system_noise_matters_is_safe_and_idempotent(self):
        t0 = NOW - dt.timedelta(hours=2)
        h_spam = self.mail(
            "spam-1",
            "cos@petrasoap.com",
            ["ricardo@petrasoap.com"],
            subject="Staging email-relay alert",
            box=RICARDO,
            at=t0,
        )
        h_real = self.mail(
            "biz-1",
            "buyer@customer.example",
            ["ricardo@petrasoap.com"],
            subject="PO 90412 delivery date",
            box=RICARDO,
            at=t0,
        )
        # Seed existing open matters directly (simulating rows created before this fix)
        self.pg.execute(
            "INSERT INTO assist.matters (matter_id, owner_person, kind, subject_key, sources, "
            "basis_at, title, detail, next_act, audiences, markings) VALUES "
            "(%s, %s, 'conversation', 'spam-1', %s, %s, "
            "'Automated alert: the email-relay job failed on staging', 'Check relay', 'reply', '[]', '[]'), "
            "(%s, %s, 'conversation', 'biz-1', %s, %s, "
            "'Customer asks for PO 90412 delivery date', 'Reply with ETA', 'reply', '[]', '[]')",
            (
                am.matter_id(RICARDO, "conversation", "spam-1"),
                RICARDO,
                json.dumps({"readings": [["emails", h_spam]]}),
                t0,
                am.matter_id(RICARDO, "conversation", "biz-1"),
                RICARDO,
                json.dumps({"readings": [["emails", h_real]]}),
                t0,
            ),
        )
        first = am.retire_system_noise_matters(self.pg)
        self.assertEqual(first, {"retired": 1})
        second = am.retire_system_noise_matters(self.pg)
        self.assertEqual(second, {"retired": 0})
        states = dict(
            self.pg.execute("SELECT subject_key, (state, done_reason) FROM assist.matters").fetchall()
        )
        self.assertEqual(states["spam-1"], ("done", "system_noise"))
        self.assertEqual(states["biz-1"], ("open", None))
        closed_logs = self.pg.execute(
            "SELECT count(*) FROM assist.matter_log WHERE what = 'closed'"
        ).fetchone()[0]
        self.assertEqual(closed_logs, 1)

    def test_readings_prepare_classifies_automated_it_alerts_as_system_noise(self):
        self.assertTrue(
            ar.is_automated_system_alert(
                sender="ops-bot@example.com",
                subject="Daily status",
                message="Automated alert: the UAT Business Central sync has not run since Tuesday.",
            )
        )
        self.assertTrue(
            pg_alerts.is_automated_reply_alert(
                "cos@petrasoap.com wrote \"Automated daily health alert: eight Workbench API staging relations\"",
                sender="cos@petrasoap.com",
                subject="Daily Workbench API health alert",
            )
        )
        self.assertFalse(
            pg_alerts.is_automated_reply_alert(
                "buyer@customer.example wrote \"PO 90412 delivery date\"",
                sender="buyer@customer.example",
                subject="PO 90412 delivery date",
            )
        )

    # ---------------------------------------------------------------- 2. Vince promise + conversation

    def test_vince_same_message_promise_and_conversation_creates_one_enriched_matter(self):
        t_template = dt.datetime(2026, 11, 25, 15, 18, 33, tzinfo=dt.timezone.utc)
        t_lunch = dt.datetime(2026, 11, 25, 12, 37, 6, tzinfo=dt.timezone.utc)

        # Scenario A: Exact same message/thread (15:18:33) produces BOTH needs_act and promise
        h1 = self.mail(
            "vince-8oz",
            "vince@petrasoap.com",
            ["greg@petrasoap.com"],
            subject="8oz template",
            box=VINCE,
            at=t_template,
        )
        self.insert_reading(
            h1,
            thread="vince-8oz",
            at=t_template,
            actor=VINCE,
            sender_person=VINCE,
            needs=True,
            act="Send the 8oz template to Greg",
            act_class="send_document",
            summary="Vince has the 8oz template and will send it to Greg.",
            promise="send the 8oz template to Greg",
            counterpart="Greg Martin",
            due=dt.date(2026, 11, 26),
        )

        # Scenario B: Separate thread key within 120s with overlapping topic tokens (12:37:06)
        h2_conv = self.mail(
            "vince-lunch-conv",
            "giulia@petrasoap.com",
            ["vince@petrasoap.com"],
            subject="Lunch order",
            box=VINCE,
            at=t_lunch,
        )
        self.insert_reading(
            h2_conv,
            thread="vince-lunch-conv",
            at=t_lunch,
            actor=VINCE,
            needs=True,
            act="Send lunch orders to Giulia",
            act_class="reply",
            summary="Marco says he is trying to send Giulia his and Greg's lunch orders.",
        )
        h2_prom = self.mail(
            "vince-lunch-prom",
            "vince@petrasoap.com",
            ["giulia@petrasoap.com"],
            subject="Re: Lunch",
            box=VINCE,
            at=t_lunch + dt.timedelta(seconds=15),
        )
        self.insert_reading(
            h2_prom,
            thread="vince-lunch-prom",
            at=t_lunch + dt.timedelta(seconds=15),
            actor=None,
            sender_person=VINCE,
            needs=False,
            summary="Vince promised to send lunch orders",
            promise="send his and Greg's lunch order",
            due=dt.date(2026, 11, 25),
        )

        # Scenario C: Unrelated standalone promise within 120s MUST remain its own promise matter
        h3 = self.mail(
            "vince-coa",
            "vince@petrasoap.com",
            ["buyer@customer.example"],
            subject="COA Lot 904",
            box=VINCE,
            at=t_template + dt.timedelta(seconds=30),
        )
        self.insert_reading(
            h3,
            thread="vince-coa",
            at=t_template + dt.timedelta(seconds=30),
            actor=None,
            sender_person=VINCE,
            needs=False,
            summary="Vince will send COA for Lot 904",
            promise="send the COA for Lot 904 to Croda",
            counterpart="Croda",
            due=dt.date(2026, 11, 27),
        )

        out = self.cycle()
        self.assertEqual(out["waits"]["merged_into_conversation"], 2)

        open_rows = self.pg.execute(
            "SELECT kind, subject_key, title, next_act, due_at, sources, counterpart "
            "FROM assist.matters WHERE owner_person = %s AND state = 'open' "
            "ORDER BY kind, subject_key",
            (VINCE,),
        ).fetchall()
        # Expect ONLY 2 conversations (8oz template, lunch) + 1 unrelated promise (COA) = 3 total
        self.assertEqual([(r[0], r[1]) for r in open_rows], [
            ("conversation", "vince-8oz"),
            ("conversation", "vince-lunch-conv"),
            ("promise", "vince-coa"),
        ])
        by_key = {r[1]: r for r in open_rows}
        # Enriched conversation preserves commitment metadata and due date
        self.assertEqual(by_key["vince-8oz"][3], "attach")
        self.assertEqual(by_key["vince-8oz"][5]["promise"], "send the 8oz template to Greg")
        self.assertEqual(by_key["vince-lunch-conv"][5]["promise"], "send his and Greg's lunch order")
        self.assertIsNotNone(by_key["vince-lunch-conv"][4])

    def test_reconcile_merges_preexisting_open_promise_and_conversation_pair(self):
        t0 = dt.datetime(2026, 11, 25, 15, 18, 33, tzinfo=dt.timezone.utc)
        h1 = self.mail("dup-t1", "vince@petrasoap.com", ["greg@petrasoap.com"], box=VINCE, at=t0)
        self.pg.execute(
            "INSERT INTO assist.matters (matter_id, owner_person, kind, subject_key, sources, "
            "basis_at, title, detail, next_act, due_at, counterpart, audiences, markings) VALUES "
            "(%s, %s, 'conversation', 'dup-t1', %s, %s, "
            "'Vince has the 8oz template and will send it to Greg.', 'Send template', 'attach', %s, '', '[]', '[]'), "
            "(%s, %s, 'promise', 'dup-t1', %s, %s, "
            "'You said: send the 8oz template to Greg', 'send the 8oz template to Greg', 'nothing', %s, 'Greg Martin', '[]', '[]')",
            (
                am.matter_id(VINCE, "conversation", "dup-t1"),
                VINCE,
                json.dumps({"readings": [["emails", h1]]}),
                t0,
                t0 + dt.timedelta(days=2),
                am.matter_id(VINCE, "promise", "dup-t1"),
                VINCE,
                json.dumps({"readings": [["emails", h1]]}),
                t0,
                t0 + dt.timedelta(hours=4),
            ),
        )
        res = aw.reconcile_duplicate_promises(self.pg)
        self.assertEqual(res, {"merged": 1})
        self.assertEqual(aw.reconcile_duplicate_promises(self.pg), {"merged": 0})
        rows = {
            r[0]: r[1:]
            for r in self.pg.execute(
                "SELECT kind, state, done_reason, due_at, counterpart, sources FROM assist.matters"
            ).fetchall()
        }
        self.assertEqual(rows["promise"][:2], ("done", "merged_into_conversation"))
        self.assertEqual(rows["conversation"][0], "open")
        self.assertEqual(rows["conversation"][2], t0 + dt.timedelta(hours=4))
        self.assertEqual(rows["conversation"][3], "Greg Martin")
        self.assertEqual(rows["conversation"][4]["promise"], "send the 8oz template to Greg")

    # ---------------------------------------------------------------- 3. Plain-English wait titles

    def test_wait_matters_build_plain_english_titles_and_populate_counterpart(self):
        t0 = NOW - dt.timedelta(hours=2)
        # Case 1: Empty counterpart in reading, person named in waiting_on with (P2) label
        h1 = self.mail(
            "w1",
            "vince@petrasoap.com",
            ["venessa.bandiera@acme.example"],
            subject="Billing months",
            box=VINCE,
            at=t0,
        )
        self.insert_reading(
            h1,
            thread="w1",
            at=t0,
            actor=None,
            sender_person=VINCE,
            needs=False,
            waiting_on="which months to bill $40 + HST, from Venessa Bandiera (P2)",
            counterpart="",
        )
        # Case 2: Empty counterpart in reading and no person in waiting_on -> falls back to recipient
        h2 = self.mail(
            "w2",
            "vince@petrasoap.com",
            ["greg@petrasoap.com"],
            subject="8oz artwork",
            box=VINCE,
            at=t0,
        )
        self.insert_reading(
            h2,
            thread="w2",
            at=t0,
            actor=None,
            sender_person=VINCE,
            needs=False,
            waiting_on="updated 8oz artwork files",
            counterpart="",
        )
        self.cycle()
        rows = dict(
            self.pg.execute(
                "SELECT subject_key, (title, counterpart) FROM assist.matters WHERE kind = 'wait'"
            ).fetchall()
        )
        self.assertEqual(
            rows["w1"],
            ("Waiting for which months to bill $40 + HST from Venessa Bandiera", "Venessa Bandiera"),
        )
        self.assertEqual(
            rows["w2"],
            ("Waiting for updated 8oz artwork files from Greg Martin", "Greg Martin"),
        )
        for title, cp in rows.values():
            self.assertNotIn("No answer yet from them", title)
            self.assertNotIn("(P2)", title)
            self.assertNotIn("\u2014", title)
            self.assertTrue(cp)

    def test_existing_generic_wait_title_upgrades_in_place_without_spurious_log(self):
        t0 = NOW - dt.timedelta(hours=2)
        h = self.mail("w-old", "vince@petrasoap.com", ["greg@petrasoap.com"], box=VINCE, at=t0)
        self.insert_reading(
            h,
            thread="w-old",
            at=t0,
            actor=None,
            sender_person=VINCE,
            needs=False,
            waiting_on="confirmed ship date for PO 106001",
            counterpart="",
        )
        self.pg.execute(
            "INSERT INTO assist.matters (matter_id, owner_person, kind, subject_key, sources, "
            "basis_at, title, detail, next_act, counterpart, audiences, markings) VALUES "
            "(%s, %s, 'wait', 'w-old', %s, %s, 'No answer yet from them', "
            "'confirmed ship date for PO 106001', 'ask_person', '', '[]', '[]')",
            (am.matter_id(VINCE, "wait", "w-old"), VINCE, json.dumps({"readings": [["emails", h]]}), t0),
        )
        self.cycle()
        title, cp = self.pg.execute(
            "SELECT title, counterpart FROM assist.matters WHERE subject_key = 'w-old'"
        ).fetchone()
        self.assertEqual(title, "Waiting for confirmed ship date for PO 106001 from Greg Martin")
        self.assertEqual(cp, "Greg Martin")
        self.assertEqual(
            self.pg.execute("SELECT count(*) FROM assist.matter_log WHERE what = 'basis_moved'").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
