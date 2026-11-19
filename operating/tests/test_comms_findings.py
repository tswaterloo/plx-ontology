"""What mail records (``operating.pg.comms_findings``), read through the one door: the mail
parity fixture's real capture (three mailboxes, the finance owner excluded, copies to the
excluded person, one naming Freightcom, senders of 1, 10 and 60 messages), its people
exclusion built by its real build, ``emails`` built and its guarded view installed
(``test_biz_comms.EmailsThroughTheDoor``), then the findings run with a stand-in model.

Checked at the boundary: nothing the door would withhold from every reader (Freightcom,
an excluded person's copy anywhere in the message, bulk mail) reaches the model; each
reader's findings through ``plx.sql`` are exactly those of the messages their own
``emails`` view shows (narrow < mid); an undecided exclusion list shows none; a second run
reads nothing again; a failed call is tried again; the 1/10/60 senders' findings are
counted exactly; a rebuild of ``emails`` (the swap drops its guarded view) keeps the
findings readable; no body, quote or address is stored. The reply parser keeps only items
whose quote is in the message.

Evidence label: injected-transport (no network, made-up ids and addresses; the model is a
stand-in)."""
from __future__ import annotations

import datetime as dt
import json
import unittest
from unittest import mock

from operating.pg import business_views as bv
from operating.pg import comms_findings as cf
from operating.tests import test_biz_comms as tbc
from operating.tests import test_table_mail_parity as tmp

setUpModule = tbc.setUpModule
tearDownModule = tbc.tearDownModule
NOW = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)


class Model:
    """A stand-in model: one request per message (its own words quoted), every message it
    was given kept; ``fail`` answers nothing."""

    def __init__(self, fail: bool = False) -> None:
        self.fail, self.seen = fail, []

    def __call__(self, batch):
        self.seen.extend(batch)
        if self.fail:
            return None
        return [[{"kind": "request", "topic": "order", "summary": f"Asked about {m['subject']}",
                  "who": ""}] for m in batch]


class FindingsThroughTheDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tbc.EmailsThroughTheDoor.setUpClass.__func__(cls)
        cls.patch = mock.patch.object(cf, "MIN_WORDS", 1)
        cls.patch.start()
        run = dict(environ=tmp.EXCLUDED_B, days=3650, now=NOW, batch=7, parallel=3)
        cls.failed = Model(fail=True)
        cls.first = cf.run(cls.dsn, extract=cls.failed, model="stand-in", **run)
        cls.model = Model()
        cls.second = cf.run(cls.dsn, extract=cls.model, model="stand-in", **run)
        cls.run_args = run

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        tbc.EmailsThroughTheDoor.tearDownClass.__func__(cls)

    tool = tbc.EmailsThroughTheDoor.tool
    sql = tbc.EmailsThroughTheDoor.sql
    handles = tbc.EmailsThroughTheDoor.handles

    def pg(self):
        import psycopg
        conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(conn.close)
        return conn

    def findings(self, actor, tag=None):
        out = self.sql(actor, "SELECT handle, summary FROM comms_findings", tag)
        self.assertFalse(out["truncated"])
        return sorted(tuple(tbc._cell(c) for c in r["cells"]) for r in out["rows"])

    def test_nothing_the_door_withholds_from_everyone_reaches_the_model(self):
        seen = {m["key"] for m in self.model.seen}
        self.assertTrue(seen)
        withheld = {r[0] for r in self.pg().execute(
            "SELECT DISTINCT message_id FROM biz.emails WHERE NOT ai_ok OR excluded OR "
            "is_bulk OR coalesce(bulk_sender, false) OR mailbox = %s",
            (tmp.BOX_B.lower(),)).fetchall()}
        self.assertTrue(withheld)
        self.assertEqual(seen & withheld, set())
        said = json.dumps([m["text"] + m["subject"] for m in self.model.seen]).lower()
        self.assertNotIn("freightcom", said)
        # a message the model read was read once, though both runs ran
        self.assertEqual(len(seen), len(self.model.seen))

    def test_each_reader_sees_the_findings_of_the_messages_they_read(self):
        rows = self.pg().execute("SELECT handles, summary FROM biz.comms_findings").fetchall()
        self.assertTrue(rows)
        counts = []
        for actor in tbc.READERS:
            with self.subTest(actor=sorted(actor.principals)):
                mine = self.handles(actor)
                want = sorted((min(set(h) & mine), s) for h, s in rows if set(h) & mine)
                got = self.findings(actor)
                self.assertEqual(got, want)
                counts.append(len(got))
        self.assertLess(counts[0], counts[1])

    def test_an_undecided_or_other_exclusion_list_shows_no_finding(self):
        for tag in ("", "another-list"):
            with self.subTest(tag=tag):
                self.assertEqual(self.findings(tmp.OWNER, tag), [])

    def test_a_failed_call_is_tried_again_and_a_read_message_never(self):
        self.assertEqual(self.first["status"], "failed")
        self.assertEqual(self.first["read"], 0)
        self.assertGreater(self.first["failed"], 0)
        self.assertEqual({m["key"] for m in self.failed.seen},
                         {m["key"] for m in self.model.seen})
        again = Model()
        out = cf.run(self.dsn, extract=again, model="stand-in", **self.run_args)
        self.assertEqual((out["status"], out["candidates"], out["calls"]), ("ok", 0, 0))
        self.assertEqual(again.seen, [])

    def test_senders_of_1_10_60_messages_counted_exactly(self):
        out = self.sql(tmp.MID, "SELECT subject, count(*) FROM comms_findings WHERE subject "
                                "LIKE 'Bulk %' GROUP BY 1")
        counts: dict[str, int] = {}
        for row in out["rows"]:
            subject, n = (tbc._cell(c) for c in row["cells"])
            self.assertEqual(int(n), 1, subject)
            counts[subject.split()[1]] = counts.get(subject.split()[1], 0) + 1
        self.assertEqual(counts, {"1": 1, "10": 10, "60": 60})

    def test_rebuilding_emails_keeps_the_findings_readable(self):
        before = self.findings(tmp.OWNER)
        self.assertTrue(before)
        bv.build(self.dsn, ["emails"], schema=self.compiled)
        self.assertEqual(self.findings(tmp.OWNER), before)

    def test_no_body_quote_or_address_is_kept(self):
        cols = [r[0] for r in self.pg().execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'biz' "
            "AND table_name = 'comms_findings' ORDER BY ordinal_position").fetchall()]
        self.assertEqual(cols, ["source_view", "message_key", "n", "handles", "kind", "topic",
                                "summary", "who", "from_side", "said_at", "model",
                                "found_at"])
        for (summary,) in self.pg().execute("SELECT summary FROM biz.comms_findings"):
            self.assertTrue(summary.startswith("Asked about "), summary)
        # the migration is the runner's DDL
        from operating.pg.migrate import MIGRATIONS_DIR
        text = (MIGRATIONS_DIR / "0047_comms_findings.sql").read_text(encoding="utf-8")
        self.assertTrue(all(stmt in text for stmt in cf.ddl("biz")))


class Parse(unittest.TestCase):
    BATCH = [{"text": "Please send the COA for lot 4471 by Friday.", "subject": "COA"},
             {"text": "Thanks!", "subject": ""}]

    def test_only_items_quoted_from_their_message_are_kept(self):
        reply = json.dumps([
            {"n": 1, "items": [
                {"kind": "request", "topic": "documents", "summary": "Asks for the COA.",
                 "who": "Ana", "quote": "send the  COA for lot 4471"},
                {"kind": "complaint", "topic": "quality", "summary": "Invented.",
                 "quote": "the batch failed testing"},
                {"kind": "gossip", "topic": "other", "summary": "x", "quote": "Please send"},
                {"kind": "request", "topic": "nonsense", "summary": "Asks for the COA again.",
                 "quote": "Please send the COA"}]},
            {"n": 7, "items": [{"kind": "praise", "summary": "x", "quote": "Thanks!!!!"}]}])
        got = cf.parse("Here you go:\n" + reply, self.BATCH)
        self.assertEqual([i["summary"] for i in got[0]],
                         ["Asks for the COA.", "Asks for the COA again."])
        self.assertEqual([i["topic"] for i in got[0]], ["documents", "other"])
        self.assertEqual(got[1], [])

    def test_an_unreadable_or_doubled_answer_is_not_known(self):
        self.assertEqual(cf.parse("no json", self.BATCH), [None, None])
        doubled = json.dumps([{"n": 1, "items": []}, {"n": 1, "items": []}])
        self.assertEqual(cf.parse(doubled, self.BATCH), [None, []])

    def test_text_screens_and_own_text(self):
        self.assertEqual(cf.screened("Booked with FreightCom today", ""), "freightcom")
        self.assertEqual(cf.screened("Please ship order 552 on Monday", "Order"), "")
        own = cf.own_text("Yes, go ahead <https://x.example/a?b=c>.\n\nOn Mon, Nov 2, 2026 "
                          "at 9:00 AM Ana <ana@x.example> wrote:\n> old text", mail=True)
        self.assertEqual(own, "Yes, go ahead .")

    def test_the_prompt_carries_no_header_address(self):
        prompt = cf.prompt_of([{"channel": "email", "from": "customer", "subject": "Hello",
                                "text": "Need a quote", "handle": "graph-mail://x/y",
                                "key": "<mid@x>", "sender": "ana@x.example"}])
        self.assertNotIn("ana@x.example", prompt)
        self.assertNotIn("graph-mail://", prompt)


if __name__ == "__main__":
    unittest.main()
