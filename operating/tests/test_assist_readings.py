"""One reading per message (``operating.assist_readings``, contract C4), read through the one
door: the mail parity fixture's real capture (three mailboxes, the finance owner excluded,
copies to the excluded person, one naming Freightcom (read like any other), bulk senders of 1, 10 and 60
messages), its people exclusion built by its real build, ``emails`` built and its guarded
view installed (``test_biz_comms.EmailsThroughTheDoor``), then reading runs with a stand-in
model.

Checked at the boundary: nothing the door withholds from every reader reaches the model and
no address does; every message due is accounted for (a reading row with a typed status, or
counted withheld), and a run's counts are kept; a failure is a typed row that is tried again
(an outage uses no attempt); a run that runs out of time says how many it left; a reading
by older code is read again; each reader's findings through ``plx.sql`` are exactly those
of the messages their own ``emails`` view shows; the 1/10/60 senders are counted exactly; a
Spark export loads to the same rows. The parser keeps a verdict only when it is a yes/no,
and only quotes, record numbers and findings that stand in the text.

Evidence label: injected-transport (no network, made-up ids and addresses; the model is a
stand-in)."""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from operating import assist_readings as ar
from operating.pg import business_views as bv
from operating.tests import test_biz_comms as tbc
from operating.tests import test_table_mail_parity as tmp

setUpModule = tbc.setUpModule
tearDownModule = tbc.tearDownModule
NOW = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)


class Model:
    """A stand-in model: every message asks its first participant for a reply, quoting its
    own first words as a request; ``fail`` answers nothing; ``drop`` keys are left out of
    the answer (that message's own failure)."""

    def __init__(self, fail: bool = False, drop: frozenset = frozenset()) -> None:
        self.fail, self.drop, self.seen, self.prompts = fail, drop, [], []

    def __call__(self, batch):
        self.seen.extend(batch)
        self.prompts.append(ar.prompt_of(batch))
        if self.fail:
            return None
        out = []
        for m in batch:
            if m["key"] in self.drop:
                out.append(None)
                continue
            words = " ".join(m["text"].split()[:4])
            out.append(ar.reading_of({
                "needs_act": True, "actor": "P1", "act": "answer", "act_class": "reply",
                "summary": f"Asked about {m['subject']}", "quote": words if len(words) >= 8 else "",
                "items": [{"kind": "request", "topic": "order", "who": "",
                           "summary": f"Asked about {m['subject']}", "quote": words}]}, m))
        return out


class ReadingsThroughTheDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tbc.EmailsThroughTheDoor.setUpClass.__func__(cls)
        cls.args = dict(environ=tmp.EXCLUDED_B, days=3650, now=NOW, batch=7, parallel=3)
        cls.failed = Model(fail=True)
        cls.first = ar.run(cls.dsn, read=cls.failed, model="stand-in", **cls.args)
        cls.model = Model()
        cls.second = ar.run(cls.dsn, read=cls.model, model="stand-in", **cls.args)

    @classmethod
    def tearDownClass(cls):
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

    def rows(self, where="true"):
        return self.pg().execute(f"SELECT message_key, status, reason, attempts, read_ver "
                                 f"FROM assist.readings WHERE {where}").fetchall()

    def test_nothing_withheld_and_no_address_reaches_the_model(self):
        seen = {m["key"] for m in self.model.seen}
        self.assertTrue(seen)
        withheld = {r[0] for r in self.pg().execute(
            "SELECT DISTINCT coalesce(message_id, handle) FROM biz.emails WHERE NOT ai_ok OR "
            "excluded OR is_bulk OR coalesce(bulk_sender, false) OR mailbox = %s",
            (tmp.BOX_B.lower(),)).fetchall()}
        self.assertTrue(withheld)
        self.assertEqual(seen & withheld, set())
        said = " ".join(self.model.prompts).lower()
        self.assertNotIn("@", said)
        self.assertNotIn("graph-mail://", said)
        self.assertEqual(len(seen), len(self.model.seen))   # read once over both runs

    def test_every_message_due_has_a_typed_row_or_is_counted_withheld(self):
        keys = {r[0] for r in self.pg().execute(
            "SELECT DISTINCT coalesce(message_id, handle) FROM biz.emails").fetchall()}
        rows = {r[0]: r[1] for r in self.rows()}
        first = self.first
        self.assertEqual(first["due"], len(keys))
        self.assertEqual(len(keys - set(rows)), first["withheld"])
        self.assertTrue(set(rows) <= keys)
        self.assertGreater(first["withheld"], 0)
        # bulk mail has its own row, screened with the reason, never sent to the model
        self.assertIn("bulk", {r[2] for r in self.rows("status = 'screened'")})
        self.assertEqual({r[1] for r in self.rows()} - set(ar.READING_STATUSES), set())
        runs = self.pg().execute("SELECT status, counts FROM assist.reading_runs ORDER BY "
                                 "run_at, seconds").fetchall()
        self.assertTrue(any(c.get("withheld") == first["withheld"] for _s, c in runs))

    def test_an_outage_uses_no_attempt_and_every_message_is_read_after(self):
        self.assertEqual(self.first["status"], "failed")
        self.assertEqual(self.first["read"], 0)
        self.assertEqual(self.first["failed"].get("no_model_answer"), len(self.failed.seen))
        self.assertEqual({m["key"] for m in self.failed.seen}, {m["key"] for m in self.model.seen})
        self.assertEqual(self.second["status"], "ok")
        again = Model()
        out = ar.run(self.dsn, read=again, model="stand-in", **self.args)
        self.assertEqual((out["status"], out["calls"], again.seen), ("ok", 0, []))

    def test_a_message_the_model_did_not_answer_is_a_typed_failure_tried_again(self):
        key = sorted(m["key"] for m in self.model.seen)[0]
        self.pg().execute("DELETE FROM assist.readings WHERE message_key = %s", (key,))
        for n in range(1, ar.ATTEMPTS + 1):
            ar.run(self.dsn, read=Model(drop=frozenset({key})), model="stand-in", **self.args)
            (row,) = self.rows(f"message_key = '{key}'")
            self.assertEqual(row[1:4], ("failed", "no_answer_for_it", n))
        soon = Model()
        ar.run(self.dsn, read=soon, model="stand-in",
               **{**self.args, "now": NOW + dt.timedelta(hours=1)})
        self.assertEqual(soon.seen, [])
        later = Model()
        ar.run(self.dsn, read=later, model="stand-in",
               **{**self.args, "now": NOW + dt.timedelta(hours=ar.RETRY_HOURS + 1)})
        self.assertEqual([m["key"] for m in later.seen], [key])
        self.assertEqual(self.rows(f"message_key = '{key}'")[0][1], "ok")

    def test_a_run_out_of_time_counts_what_it_left_and_older_code_is_read_again(self):
        self.pg().execute("UPDATE assist.readings SET read_ver = 0 WHERE status = 'ok'")
        n = self.pg().execute("SELECT count(*) FROM assist.readings WHERE read_ver = 0"
                              ).fetchone()[0]
        self.assertGreater(n, 0)
        out = ar.run(self.dsn, read=Model(), model="stand-in", **{**self.args, "seconds": 0})
        self.assertEqual((out["read"], out["left"]), (0, n))
        self.assertIn(f"{n} messages not reached", out["reason"])
        back = Model()
        ar.run(self.dsn, read=back, model="stand-in", **self.args)
        self.assertEqual(len(back.seen), n)
        self.assertEqual(self.rows("read_ver = 0"), [])

    def test_each_reader_sees_the_findings_of_the_messages_they_read(self):
        rows = self.pg().execute("SELECT handles, i->>'summary' FROM assist.readings, "
                                 "jsonb_array_elements(findings) i").fetchall()
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

    def test_an_undecided_or_other_exclusion_list_shows_no_finding_and_reads_nothing(self):
        for tag in ("", "another-list"):
            with self.subTest(tag=tag):
                self.assertEqual(self.findings(tmp.OWNER, tag), [])
        model = Model()
        out = ar.run(self.dsn, read=model, model="stand-in",
                     **{**self.args, "environ": {**tmp.ENV, "PLX_EXCLUDED_PEOPLE_OIDS": ""}})
        self.assertEqual((out["status"], model.seen), ("blocked", []))

    def test_senders_of_1_10_60_messages_counted_exactly(self):
        got = dict(self.pg().execute(
            "SELECT split_part(e.subject, ' ', 2), count(DISTINCT r.message_key) FROM "
            "assist.readings r JOIN biz.emails e ON e.handle = ANY(r.handles) WHERE "
            "e.subject LIKE 'Bulk %' GROUP BY 1").fetchall())
        self.assertEqual(got, {"1": 1, "10": 10, "60": 60})

    def test_rebuilding_emails_keeps_the_findings_readable(self):
        before = self.findings(tmp.OWNER)
        self.assertTrue(before)
        bv.build(self.dsn, ["emails"], schema=self.compiled)
        self.assertEqual(self.findings(tmp.OWNER), before)

    def test_a_spark_export_loads_to_the_same_readings(self):
        cols = "message_key, status, reason, needs_act, actor, act_class, findings"
        want = self.pg().execute(f"SELECT {cols} FROM assist.readings ORDER BY 1").fetchall()
        with tempfile.TemporaryDirectory() as d:
            self.pg().execute("TRUNCATE assist.readings")
            got = ar.export(self.dsn, Path(d), since=NOW - dt.timedelta(days=3650),
                            until=NOW, read=Model(), model="stand-in",
                            environ=tmp.EXCLUDED_B, batch=7, parallel=3)
            self.assertEqual(got["status"], "ok")

            again = ar.export(self.dsn, Path(d), since=NOW - dt.timedelta(days=3650),
                              until=NOW, read=Model(), model="stand-in",
                              environ=tmp.EXCLUDED_B)
            self.assertEqual(again["read"] + sum(again["screened"].values()), 0)
            ar.load(self.dsn, Path(d) / "readings.jsonl")
        self.assertEqual(self.pg().execute(f"SELECT {cols} FROM assist.readings ORDER BY 1"
                                           ).fetchall(), want)

    def test_a_spark_export_reads_the_whole_backlog_and_says_what_it_cost_by_estimate(self):
        """Messages older than FRESH_DAYS are the backlog: nothing caps what an export reads
        (owner 11-23), a second export of the same folder reads nothing more, and the counts
        carry the estimated cost as a figure, never a limit."""
        self.pg().execute("CREATE TABLE assist.readings_keep AS SELECT * FROM assist.readings")

        def restore():       # the other tests read the fixture's own readings
            self.pg().execute("TRUNCATE assist.readings")
            self.pg().execute("INSERT INTO assist.readings SELECT * FROM assist.readings_keep")
            self.pg().execute("DROP TABLE assist.readings_keep")
        self.addCleanup(restore)
        self.pg().execute("TRUNCATE assist.readings")
        kw = dict(since=NOW - dt.timedelta(days=3650), until=NOW, read=Model(),
                  model="stand-in", environ=tmp.EXCLUDED_B)
        with tempfile.TemporaryDirectory() as d:
            one = ar.export(self.dsn, Path(d, "a"), **kw)
            self.assertEqual(one["status"], "ok")
            self.assertGreater(one["backlog_read"], 3)          # more than any old cap allowed
            self.assertEqual(one["left"], 0)
            self.assertNotIn("backlog_waiting", one)
            self.assertEqual(one["usd_estimated"], round(one["read"] * ar.MESSAGE_USD, 2))
            again = ar.export(self.dsn, Path(d, "a"), **kw)
            self.assertEqual(again["read"], 0)

    def test_earlier_thread_messages_are_all_shown_whole_until_the_models_window_is_full(self):
        def rows(n):
            return [(f"k{i}", NOW - dt.timedelta(hours=i + 1), "s@x", f"h{i}", f"o{i}") for i in range(n)]
        m = {"said": NOW, "key": "now", "view": "emails", "text": "ask"}
        texts = {(f"h{i}", f"o{i}"): ("Please note line %d. " % i * 400, "") for i in range(30)}
        out, left = ar.earlier_of(m, rows(30), texts)
        self.assertEqual((len(out), left), (30, 0))                 # 30 earlier messages, none cut
        self.assertTrue(all(len(x["text"]) > 1500 for x in out))     # each whole (no 1,500 cut)
        window = ar.MODEL_WINDOW_CHARS
        texts = {(f"h{i}", f"o{i}"): ("x " * (window // 8), "") for i in range(30)}
        out, left = ar.earlier_of(m, rows(30), texts)
        self.assertGreater(left, 0)                                  # the window, said as a count
        self.assertEqual(len(out) + left, 30)
        self.assertEqual(out[-1]["sent"], ar._minute(NOW - dt.timedelta(hours=1)))   # newest kept

    def test_a_message_over_the_models_window_is_read_in_pages_never_cut(self):
        seen = []

        def read(batch):
            seen.append([m["text"] for m in batch])
            return [ar.reading_of({"needs_act": len(seen) == 2, "actor": "P1", "act": f"page {len(seen)}",
                                   "act_class": "reply", "summary": "long", "quote": "", "items": []}, m)
                    for m in batch]
        long = {"key": "k", "view": "emails", "subject": "s", "text": "word " * (ar.MODEL_WINDOW_CHARS // 5 * 2 + 40)}
        pages = ar._pages(long["text"])
        self.assertEqual("".join(pages), long["text"])               # every character is in a page
        self.assertEqual(len(pages), 3)
        got = ar._read_paged(read, long)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(t.startswith("[part ") for [t] in seen))
        self.assertEqual(got["act"], "page 2")                       # the last page that needs an act
        self.assertIsNone(ar._read_paged(lambda b: [None] * len(b), long))   # a page unanswered: none

    def test_findings_not_made_yet_are_said_as_not_built(self):
        """Between the migration and the first reading run the guarded findings view does
        not exist: plx.sql says "not built yet" (typed ``not_ready``), not an unknown view."""
        from unittest import mock
        from operating import ontology_tools_sql as ts
        from operating.pg import comms_findings as cf
        actor = tmp.OWNER
        cf.drop_view(self.pg())
        self.addCleanup(lambda: cf.create_view(self.pg()))
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            out = self.tool(True).call("plx.sql", {"sql": "SELECT count(*) FROM comms_findings"},
                                       actor)
        err = out.get("error") or {}
        self.assertEqual(err.get("code"), "not_ready", out)
        self.assertIn("not built yet", str(err.get("message")))

    def test_the_migrations_are_the_runners_ddl_and_readers_hold_no_grant(self):
        from operating.pg.migrate import MIGRATIONS_DIR
        text = (MIGRATIONS_DIR / "0062_assist_readings.sql").read_text(encoding="utf-8")
        work = (MIGRATIONS_DIR / "0063_assist_work.sql").read_text(encoding="utf-8")
        self.assertTrue(all(stmt in text for stmt in ar.ddl("assist")))
        self.assertTrue(all(stmt in work for stmt in ar.deltas("assist")))
        from operating.pg import comms_findings as cf
        # the API's role reads the findings feed only: its grant comes after the revokes
        self.assertTrue(all(stmt in work for stmt in cf.feed_ddl()))
        self.assertGreater(work.index(cf.feed_ddl()[-1]),
                           work.rindex("REVOKE ALL ON ALL TABLES IN SCHEMA assist FROM plx_api"))
        for role in ("plx_api", "plx_sql_reader"):
            self.assertIn(f"REVOKE ALL ON ALL TABLES IN SCHEMA assist FROM {role}", text)
            self.assertIn(f"REVOKE ALL ON ALL TABLES IN SCHEMA assist FROM {role}", work)

    def test_a_fresh_run_reads_only_the_newest_messages(self):
        newest = self.pg().execute("SELECT max(received_at) FROM biz.emails").fetchone()[0]
        want = self.pg().execute(
            "SELECT count(DISTINCT coalesce(message_id, handle)) FROM biz.emails WHERE "
            "received_at >= %s", (newest - dt.timedelta(minutes=30),)).fetchone()[0]
        self.pg().execute("UPDATE assist.readings SET read_ver = 1")
        out = ar.run(self.dsn, read=Model(), model="stand-in",
                     **{**self.args, "now": newest + dt.timedelta(seconds=1)}, fresh_minutes=30)
        self.assertEqual(out["due"], want)
        self.assertLess(want, self.first["due"])
        ar.run(self.dsn, read=Model(), model="stand-in", **self.args)   # the rest again

    def test_a_parked_run_reads_only_the_fresh_days_and_says_why_and_an_unknown_reading_reads_all(self):
        now = self.pg().execute("SELECT max(received_at) FROM biz.emails").fetchone()[0] + dt.timedelta(seconds=1)
        edge = now - dt.timedelta(days=ar.FRESH_DAYS)
        want = self.pg().execute(
            "SELECT count(DISTINCT coalesce(message_id, handle)) FROM biz.emails WHERE received_at >= %s",
            (edge,)).fetchone()[0]
        every = self.pg().execute(
            "SELECT count(DISTINCT coalesce(message_id, handle)) FROM biz.emails").fetchone()[0]
        self.assertLess(want, every, "the fixture must hold messages older than the fresh days")
        self.pg().execute("UPDATE assist.readings SET read_ver = 1")
        args = {**self.args, "now": now}
        parked = ar.run(self.dsn, read=Model(), model="stand-in", **args,
                        wall={"parked": True, "pct": 91.0, "status": "ok", "reason": ""})
        self.assertEqual(parked["due"], want)
        self.assertEqual(parked["parked"], 91.0)
        self.assertIn("parked", parked["reason"])
        self.pg().execute("UPDATE assist.readings SET read_ver = 1")
        unknown = ar.run(self.dsn, read=Model(), model="stand-in", **args,
                         wall={"parked": False, "pct": None, "status": "unknown", "reason": "usage file missing"})
        self.assertEqual(unknown["due"], every)
        self.assertIn("unknown", unknown["usage"])
        self.assertNotIn("parked", unknown)

    def test_reading_1_10_60_messages_takes_the_same_statements(self):
        rows = self.pg().execute(
            "SELECT coalesce(message_id, handle), bool_or(is_bulk OR coalesce(bulk_sender, "
            "false)) FROM biz.emails GROUP BY 1 HAVING bool_and(ai_ok AND NOT excluded) "
            "ORDER BY 2, 1").fetchall()
        keys = [k for k, _b in rows]
        bulk = {k for k, b in rows if b}
        self.assertGreaterEqual(len(keys), 60)
        tag = ar._covered(self.pg(), tmp.EXCLUDED_B)
        self.pg().execute("UPDATE assist.readings SET read_ver = 1")   # all due again
        self.addCleanup(lambda: ar.run(self.dsn, read=Model(), model="stand-in", **self.args))
        everyone = [k for (k,) in self.pg().execute(
            "SELECT DISTINCT coalesce(message_id, handle) FROM biz.emails").fetchall()]
        counts, seconds = {}, {}
        for n in (1, 10, 60):
            calls = []

            class Counted:
                def __init__(self, pg):
                    self.pg = pg

                def execute(self, sql, params=None):
                    calls.append(sql)
                    return self.pg.execute(sql, params)
            others = sorted(set(everyone) - set(keys[:n]))   # read as if already read
            model = Model()
            with tempfile.TemporaryDirectory() as d:
                sink = ar.Sink(path=Path(d) / "r.jsonl")
                started = dt.datetime.now()
                ar.read_messages(ar._Skip(Counted(self.pg()), others), sink, covered=tag,
                                 read=model, model="stand-in", schema=bv.SCHEMA,
                                 since=NOW - dt.timedelta(days=3650),
                                 until=NOW + dt.timedelta(days=1), now=NOW, page=100)
                sink.close()
            seconds[n] = (dt.datetime.now() - started).total_seconds()
            read = n - len(bulk & set(keys[:n]))      # bulk mail is screened, never sent
            self.assertEqual(len(model.seen), read)
            self.assertLessEqual(len(model.prompts), -(-read // ar.BATCH) + 1)
            counts[n] = len(calls)
        self.assertEqual(len(set(counts.values())), 1, counts)
        self.assertLess(seconds[60], 3 * seconds[1] + 2.0, seconds)


class Parse(unittest.TestCase):
    MSG = {"view": "emails", "text": "Please send the COA for lot 4471 by Friday. PO 552 is waiting.",
           "said": NOW, "people": [
               {"label": "P1", "ident": "ana@x.example", "role": "sender", "persons": ["s-ana"],
                "basis": "address"},
               {"label": "P2", "ident": "bo@y.example", "role": "to", "persons": [],
                "basis": "address"},
               {"label": "P3", "ident": "team@x.example", "role": "to",
                "persons": ["s-1", "s-2"], "basis": "address"}]}

    def read(self, **row):
        return ar.reading_of({"needs_act": True, **row}, self.MSG)

    def test_a_verdict_is_a_yes_or_no_and_nothing_else(self):
        for bad in ("yes", None, 1):
            self.assertIsNone(ar.reading_of({"needs_act": bad}, self.MSG))
        self.assertEqual(ar.parse("no json", [self.MSG, self.MSG]), [None, None])
        doubled = json.dumps([{"n": 1, "needs_act": False}, {"n": 1, "needs_act": True},
                              {"n": 2, "needs_act": False}])
        got = ar.parse("ok:\n" + doubled, [self.MSG, self.MSG])
        self.assertIsNone(got[0])
        self.assertFalse(got[1]["needs_act"])

    def test_only_what_stands_in_the_text_is_kept_and_unknowns_are_said(self):
        r = self.read(actor="P2", act_class="reply", due="2026-11-06",
                      records=["lot 4471", "PO 552", "SO 99"], quote="send the  COA for lot 4471",
                      items=[{"kind": "request", "topic": "nonsense", "summary": "Asks for the COA.",
                              "quote": "Please send the COA"},
                             {"kind": "complaint", "summary": "Invented.", "quote": "the batch failed"},
                             {"kind": "gossip", "summary": "x", "quote": "Please send the COA"}])
        self.assertEqual((r["actor"], r["act_class"], r["due"]),
                         ("bo@y.example", "reply", dt.date(2026, 11, 6)))
        self.assertEqual(r["records"], ["lot 4471", "PO 552"])
        self.assertEqual([(i["kind"], i["topic"]) for i in r["findings"]], [("request", "other")])
        self.assertEqual(r["finding_kind"], "request")
        self.assertIn("1 record numbers not in the text", r["reason"])
        r = self.read(actor="P9", act_class="phone_call", due="Friday", quote="made up words here")
        self.assertEqual((r["actor"], r["act_class"], r["due"], r["quote"]), ("", None, None, ""))
        for said in ("actor not a participant", "act class not known: phone_call",
                     "quote not in the text"):
            self.assertIn(said, r["reason"])
        self.assertEqual(self.read(actor="OUT")["actor"], "outside")
        self.assertIsNone(ar.reading_of({"needs_act": False, "act_class": "reply"},
                                        self.MSG)["act_class"])

    def test_the_actor_and_sender_are_staff_ids_only_on_exactly_one_match(self):
        r = self.read(actor="P1")
        self.assertEqual((r["actor_person"], r["actor_basis"], r["sender_person"]),
                         ("s-ana", "address", "s-ana"))
        self.assertEqual((self.read(actor="P2")["actor_person"], self.read(actor="P2")["actor_basis"]),
                         (None, ""))
        self.assertEqual((self.read(actor="P3")["actor_person"], self.read(actor="P3")["actor_basis"]),
                         (None, "unresolved:2"))
        staff = ar.Staff([("s-ana", "Ana@X.example", "Ana Ruiz"), ("t-bo", "", "Bo Lee")])
        mail = ar.participants({"view": "emails", "sender": "ana@x.example", "to": ["c@y.example"],
                                "mailboxes": ["t-bo"]}, staff)
        self.assertEqual([(p["label"], p["persons"], p["name"]) for p in mail],
                         [("P1", ["s-ana"], "Ana Ruiz"), ("P2", [], "")])
        sender = json.dumps({"id": "t-bo", "display_name": "Bo Lee", "kind": "user"})
        team = ar.participants({"view": "teams_messages", "sender": sender,
                                "earlier": [{"sender": '{"id": "x9", "display_name": "Cy", '
                                                       '"kind": "application"}'}]}, staff)
        self.assertEqual([(p["persons"], p["basis"], p["side"]) for p in team],
                         [(["t-bo"], "teams_id", "our staff"), ([], "teams_id", "an app")])
        self.assertEqual(ar.teams_sender("not json"), ("", "", ""))
        prompt = ar.prompt_of([{**self.MSG, "people": mail, "key": "k", "subject": "s"}])
        self.assertNotIn("@", prompt)
        self.assertNotIn("s-ana", prompt)

    def test_own_text_is_whole_unless_a_caller_asks_for_a_cut_and_it_says_so(self):
        long = "Thanks for the update. " * 1200 + "Please confirm the ship date for PO 552."
        own, left = ar.own_text(long, mail=False)
        self.assertEqual(left, 0)
        self.assertTrue(own.endswith("Please confirm the ship date for PO 552."))
        cut, left = ar.own_text(long, mail=False, most=1500)    # a caller that asks for a cut
        self.assertGreater(left, 0)
        self.assertTrue(cut.endswith(ar.CUT.format(n=left)))
        self.assertEqual(ar.own_text("Please ship order 552 on Monday", mail=False),
                         ("Please ship order 552 on Monday", 0))


if __name__ == "__main__":
    unittest.main()
