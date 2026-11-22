"""The communications tables (``operating.pg.business_comms``) read through the one door
(``plx.sql`` over ``biz_sql.emails``): the set a reader sees is the mail item gate's, row
for row.

The mail parity fixture (``test_table_mail_parity``: a real capture of three mailboxes, the
finance one's owner excluded, messages copied to the excluded person, to an undecided
tenant address, Bcc copies, senders of 1, 10 and 60 messages, one naming Freightcom)
loaded into a throwaway Postgres; the people exclusion's set built by its real build;
``emails`` built by the business build and its guarded view installed. Each reader's
audience is its mailboxes, as the staff scope grants them (narrow: one, mid: two, owner:
all three). Checked at the boundary: every handle ``plx.sql`` returns is one the item gate
serves and every one it serves is returned (Freightcom's never); an undecided or another
exclusion list, or mail the set has not decided yet, shows nothing; per-sender counts of
1/10/60 are exact; an in-place refresh reads the same rows and drops a source-deleted one.

Evidence label: injected-transport (no network, made-up ids and addresses)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import unittest
from unittest import mock

from operating import ontology_tools as ot
from operating import ontology_tools_sql as ts
from operating.pg import business_comms as bc
from operating.pg import business_views as bv
from operating.pg import people_excluded, sql_views
from operating.tests import test_table_mail_parity as tmp
from tests import test_ontology_tools_evidence as ev

# the disk guard's floor (PLX_BIZ_MIN_FREE_GB) is the served host's: a test host's free
# space is not what these tests are about (the guard's own test sets the floor back)
_NO_FLOOR = mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "0"})


def setUpModule():
    _NO_FLOOR.start()


def tearDownModule():
    _NO_FLOOR.stop()

READERS = (tmp.NARROW, tmp.MID, tmp.OWNER)
# the stored envelope of mail a mailing platform sent (its bounce host and DKIM signer)
PLATFORM = ("mx.example-plx.com; spf=pass smtp.mailfrom=bounce.vendor2.example; "
            "dkim=pass header.d=em123.vendor2.example")
PERSON = "jane.doe@vendor2.example"
SECOND = "orders.desk@example-plx.com"   # a second address of the orders mailbox


_BASE_GRAPH = tmp.graph


def _graph():
    """The parity fixture's mailboxes plus the mail the flags are about, in the orders
    mailbox (owner addresses: orders@ and its own proxy ``SECOND``; shared@ is two
    mailboxes' and so never cleared)."""
    g = _BASE_GRAPH()
    g.users[tmp.BOX_A]["proxyAddresses"].append(f"smtp:{SECOND}")

    def put(mid, subject, *, sender, to, cc=(), body="Thanks, see the list.", conv=None,
            at=None, envelope=None):
        g.put(tmp.BOX_A, mid, "CK-" + mid, subject=subject, body=body, received=at)
        m = g.messages[tmp.BOX_A][mid]
        m["from"] = {"emailAddress": {"address": sender}}
        m["toRecipients"] = [{"emailAddress": {"address": a}} for a in to]
        m["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
        if conv:
            m["conversationId"] = conv
        if envelope:
            m["internetMessageHeaders"] = [{"name": "Authentication-Results",
                                            "value": envelope}]
    # a person's address and text, a platform's envelope: bulk; its relay of a person's
    # message is not, nor platform mail in a thread one of ours wrote in before it
    put("F-PLAT", "Spring range", sender=PERSON, to=[tmp.UPN_A], envelope=PLATFORM)
    put("F-RELAY", "Booking 778", sender=PERSON, to=[tmp.UPN_A], envelope=PLATFORM,
        body="Always communicate through the app.")
    put("F-OURS", "Samples", sender=tmp.UPN_A, to=[PERSON], conv="CONV-F1",
        at="2026-09-21T09:00:00Z")
    put("F-PLAT2", "RE: Samples", sender=PERSON, to=[tmp.UPN_A], conv="CONV-F1",
        at="2026-09-21T10:00:00Z", envelope=PLATFORM)
    # platform mail in that thread BEFORE ours wrote: still bulk (only an earlier message
    # of ours makes it a person's)
    put("F-PLAT0", "Range launch", sender=PERSON, to=[tmp.UPN_A], conv="CONV-F1",
        at="2026-09-21T08:00:00Z", envelope=PLATFORM)
    # a word in older subjects and in a newer body only: the subjects rank first
    put("F-LAB", "Lab update", sender=tmp.SUPPLIER, to=[tmp.UPN_A],
        body="The samples left the lab today.", at="2026-09-23T09:00:00Z")
    put("F-FORM", "Weekly digest", sender=PERSON, to=[tmp.UPN_A],
        body="Shop now: 20% off everything")
    # the owner's place by every address they have 
    put("F-TO2", "To the second address", sender=tmp.SUPPLIER, to=[SECOND])
    put("F-CC2", "Cc the second address", sender=tmp.SUPPLIER, to=["buyer@other.example"],
        cc=[SECOND])
    put("F-NONE", "Not to the owner", sender=tmp.SUPPLIER, to=["buyer@other.example"])
    # a thread the owner answered (from the second address), then the customer wrote again
    put("R-IN", "Quote please", sender=tmp.SUPPLIER, to=[tmp.UPN_A], conv="CONV-R",
        at="2026-09-22T09:00:00Z")
    put("R-OUT", "RE: Quote please", sender=SECOND, to=[tmp.SUPPLIER], conv="CONV-R",
        at="2026-09-22T11:00:00Z")
    put("R-IN2", "RE: Quote please again", sender=tmp.SUPPLIER, to=[tmp.UPN_A],
        conv="CONV-R", at="2026-09-22T12:00:00Z")
    return g


def _cell(value):
    return value["untrusted_text"] if isinstance(value, dict) else value


class EmailsThroughTheDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        from ontology.text_index import store_text_index
        built = tmp.build_store

        def indexed(*args, **kwargs):   # the served build indexes the bodies' text
            return built(*args, **kwargs, post_project={"text_index": store_text_index})
        with mock.patch.object(tmp, "graph", _graph), \
                mock.patch.object(tmp, "build_store", indexed):
            tmp.TableMailParity.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            if not pg.execute("SELECT count(*) FROM ontology.chunk_words").fetchone()[0]:
                from operating.pg import words
                rows = pg.execute("SELECT chunk_id, body FROM ontology.text_chunks").fetchall()
                for cid, vec in words.chunk_word_rows((r[0], bytes(r[1])) for r in rows):
                    pg.execute("INSERT INTO ontology.chunk_words VALUES (%s, %s::tsvector)",
                               (cid, vec))
        # the flags read the mail capture of the host's state directory
        cls.env = mock.patch.dict(os.environ, {"PLX_STATE_DIR": str(cls.state)})
        cls.env.start()
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for name in ("0027_item_audiences.sql", "0038_own_issued_documents.sql", "0040_people_excluded_objects.sql",
                         "0041_business_views.sql"):
                pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        with psycopg.connect(cls.dsn) as pg:
            people_excluded.build(pg, cls.state, environ=tmp.EXCLUDED_B, workers=2)
        cls.tag = people_excluded.current_exclusion(tmp.EXCLUDED_B)
        assert cls.tag, "the fixture's exclusion list must be decidable"
        cls.built = bv.build(cls.dsn, ["emails"], schema=cls.compiled)
        sql_views.install(cls.dsn)
        keys = {row[0] for row in cls.sqlite.execute(
            "SELECT DISTINCT audience_key FROM objects WHERE object_type = 'Email'")}
        box = {k: k.rsplit(":", 1)[-1].lower() for k in keys}
        principal = {tmp.BOX_A.lower(): "staff:orders", tmp.BOX_B.lower(): "staff:finance",
                     tmp.BOX_C.lower(): "staff:sales"}
        assert set(box.values()) <= set(principal), box
        cls.policy = ot.ReadPolicy(tenant="plx", audiences={
            k: {principal[b]} for k, b in box.items()})

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        tmp.TableMailParity.tearDownClass.__func__(cls)

    runtime = tmp.TableMailParity.runtime
    gated = tmp.TableMailParity.gated
    subjects = tmp.TableMailParity.subjects

    def tool(self, owner=False):
        import dataclasses
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=True)
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.sqlite))
        # The one holding ``OWNER_KEY``
        policy = dataclasses.replace(self.policy, unrestricted=True, owner=True) if owner \
            else self.policy
        base = ot.OntologyTools(reader, policy, item_gate=lambda a, h: True,
                                schema=ot.SchemaView.from_compiled(self.compiled),
                                grammar=None, contracts=ev.CONTRACTS, clock=lambda: ev.NOW)
        return ts.SqlTool(base)

    def sql(self, actor, sql, tag=None, owner=False):
        with mock.patch.object(ts, "_people_tag", return_value=self.tag if tag is None
                               else tag):
            out = self.tool(owner).call("plx.sql", {"sql": sql}, actor)
        self.assertNotIn("error", out, out)
        return out

    def handles(self, actor, tag=None, owner=False):
        out = self.sql(actor, "SELECT handle FROM emails", tag, owner)
        self.assertFalse(out["truncated"])
        return {_cell(r["cells"][0]) for r in out["rows"]}

    def test_each_readers_rows_are_the_item_gates(self):
        runtime = self.runtime()
        oid_of = {h: o for o, h in self.emails.items()}
        for actor in READERS:
            with self.subTest(actor=sorted(actor.principals)):
                gate = self.gated(runtime, actor)
                freightcom = {o for o in gate if "Freightcom" in str(self.subjects({o}))}
                self.assertEqual(len(freightcom), 1)
                got = self.handles(actor)
                self.assertTrue(got <= set(oid_of), got - set(oid_of))
                self.assertEqual({oid_of[h] for h in got}, gate - freightcom,
                                 (self.subjects({oid_of[h] for h in got}),
                                  self.subjects(gate)))
        self.assertLess(len(self.handles(tmp.NARROW)), len(self.handles(tmp.MID)))

    def test_the_people_sets_mail_is_the_owners_alone(self):
        """The set's mail is never removed."""
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            in_set = {r[0] for r in pg.execute(
                f"SELECT handle FROM {bv.SCHEMA}.emails WHERE excluded AND ai_ok")}
        self.assertTrue(in_set)   # not vacuous: the fixture's set holds mail
        everyone = self.handles(tmp.OWNER)
        self.assertFalse(everyone & in_set)
        for actor in READERS:
            self.assertFalse(self.handles(actor) & in_set, sorted(actor.principals))
        owner = self.handles(tmp.OWNER, owner=True)
        self.assertEqual(owner, everyone | in_set)
        # whatever the list tag says, nothing is withheld from the owner by the set
        self.assertEqual(self.handles(tmp.OWNER, "", owner=True), owner)

    def test_a_row_the_set_grew_over_is_flagged_by_the_set_change_not_the_next_refresh(self):
        """11-22 live: a mail refreshed before the set grew kept ``excluded`` false until
        its table's next refresh (the leak scan counted it). ``people_excluded.flag_comms``
        flags every row of an object of the set at once (only those: no other row moves),
        whole or for the grown objects alone."""
        import psycopg
        from operating.pg import people_excluded
        with psycopg.connect(self.dsn) as pg:
            flagged = {r[0]: r[1] for r in pg.execute(
                f"SELECT handle, object_ids[1] FROM {bv.SCHEMA}.emails WHERE excluded")}
            clear = pg.execute(f"SELECT count(*) FROM {bv.SCHEMA}.emails WHERE "
                               "excluded IS FALSE").fetchone()[0]
            self.assertTrue(flagged)
            some = sorted(flagged)[0]
            pg.execute(f"UPDATE {bv.SCHEMA}.emails SET excluded = false WHERE handle = %s",
                       (some,))
            pg.commit()
            self.assertEqual(people_excluded.flag_comms(pg, ["no-such-object"]), {})
            self.assertEqual(people_excluded.flag_comms(pg, [flagged[some]]).get("emails"), 1)
            pg.execute(f"UPDATE {bv.SCHEMA}.emails SET excluded = false WHERE handle = %s",
                       (some,))
            pg.commit()
            self.assertEqual(people_excluded.flag_comms(pg).get("emails"), 1)
            pg.commit()
            again = {r[0] for r in pg.execute(
                f"SELECT handle FROM {bv.SCHEMA}.emails WHERE excluded")}
            self.assertEqual(again, set(flagged))
            self.assertEqual(pg.execute(f"SELECT count(*) FROM {bv.SCHEMA}.emails WHERE "
                                        "excluded IS FALSE").fetchone()[0], clear)
            self.assertEqual(people_excluded.flag_comms(pg), {})   # nothing left to do

    def test_an_undecided_or_other_exclusion_list_shows_nothing(self):
        for tag in ("", "another-list"):
            with self.subTest(tag=tag):
                self.assertEqual(self.handles(tmp.OWNER, tag), set())
        # business views are not the exclusion's: they read as before
        self.assertIn("emails", json.dumps(self.sql(tmp.OWNER, "SELECT count(*) FROM emails",
                                                    "")["views"]))

    def test_mail_the_set_has_not_decided_yet_is_withheld(self):
        import psycopg
        before = self.handles(tmp.OWNER)
        self.assertTrue(before)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            (covered,) = pg.execute("SELECT covered_ver FROM ontology.people_excluded_state"
                                    ).fetchone()
            pg.execute("UPDATE ontology.people_excluded_state SET covered_ver = 0")
            try:
                # plx.sql: no row (or the door refuses while it catches up); the search
                # (its own settings, no door): no hit
                with mock.patch.object(ts, "_people_tag", return_value=self.tag):
                    out = self.tool().call("plx.sql", {"sql": "SELECT handle FROM emails"},
                                           tmp.OWNER)
                self.assertTrue(out.get("rows") == [] or
                                (out.get("error") or {}).get("code") == "not_ready", out)
                self.assertEqual(self.search(tmp.OWNER, query="agreed")["documents"], [])
            finally:
                pg.execute("UPDATE ontology.people_excluded_state SET covered_ver = %s",
                           (covered,))
        self.assertEqual(self.handles(tmp.OWNER), before)

    def test_senders_of_1_10_60_messages_counted_exactly(self):
        out = self.sql(tmp.MID, "SELECT subject FROM emails WHERE subject LIKE 'Bulk %'")
        counts: dict[str, int] = {}
        for row in out["rows"]:
            n = _cell(row["cells"][0]).split()[1]
            counts[n] = counts.get(n, 0) + 1
        self.assertEqual(counts, {"1": 1, "10": 10, "60": 60})
        out = self.sql(tmp.MID, "SELECT count(*) FROM emails")
        self.assertEqual(int(_cell(out["rows"][0]["cells"][0])), len(self.handles(tmp.MID)))

    def test_columns_hold_the_message(self):
        out = self.sql(tmp.OWNER, "SELECT subject, sender, addresses, domains, received_date, "
                                  "mailbox, bulk_sender, attachments FROM emails "
                                  "WHERE subject = 'Order note one'")
        self.assertEqual(len(out["rows"]), 1, out)
        row = dict(zip(out["columns"], (_cell(c) for c in out["rows"][0]["cells"])))
        self.assertEqual(row["sender"], tmp.SUPPLIER)
        self.assertIn(tmp.SUPPLIER, json.dumps(row["addresses"]))
        self.assertIn("outside.example", json.dumps(row["domains"]))
        self.assertIsNotNone(row["received_date"])
        self.assertIs(row["bulk_sender"], False)
        self.assertFalse(bc.bulk_sender(tmp.SUPPLIER))
        self.assertTrue(bc.bulk_sender("no-reply@shop.example"))

    def flags(self, actor=tmp.OWNER):
        out = self.sql(actor, "SELECT subject, is_bulk, owner_role, owner_replied_at, "
                              "bulk_sender FROM emails WHERE mailbox = '%s'" % tmp.BOX_A.lower())
        rows = [[_cell(c) for c in r["cells"]] for r in out["rows"]]
        got = {r[0]: tuple(r[1:4]) for r in rows}
        self.assertEqual(len(got), len(rows), "one copy per subject in the orders mailbox")
        return got

    def test_bulk_flag_by_sender_form_and_envelope(self):
        got = self.flags()
        self.assertIs(got["Spring range"][0], True)            # the envelope's platform
        self.assertIs(got["Weekly digest"][0], True)           # the form (a mailing's text)
        self.assertIs(got["Booking 778"][0], False)            # a platform's relay
        self.assertIs(got["RE: Samples"][0], False)            # one of ours wrote first
        self.assertIs(got["Range launch"][0], True)            # ours wrote only after it
        self.assertIs(got["Samples"][0], False)                # our own person's mail
        self.assertIs(got["Order note one"][0], False)         # a supplier's own note
        self.assertTrue(all(v[0] in (True, False) for v in got.values()), got)

    def test_owner_role_by_every_address_of_the_owner(self):
        got = self.flags()
        self.assertEqual(got["Order note one"][1], "to")
        self.assertEqual(got["To the second address"][1], "to")
        self.assertEqual(got["Cc the second address"][1], "cc")
        self.assertEqual(got["Not to the owner"][1], "none")

    def test_owner_replied_at_is_the_owners_latest_reply_after(self):
        got = self.flags()
        self.assertEqual(dt.datetime.fromisoformat(str(got["Quote please"][2])),
                         dt.datetime(2026, 9, 22, 11, tzinfo=dt.timezone.utc), got)
        self.assertIsNone(got["RE: Quote please"][2])           # the reply itself
        self.assertIsNone(got["RE: Quote please again"][2])     # written after the reply
        self.assertIsNone(got["Order note one"][2])             # never answered

    def test_flags_follow_a_refresh_and_an_unreadable_capture_says_unknown(self):
        import psycopg
        before = self.flags()
        out = self.sql(tmp.OWNER, "SELECT handle FROM emails WHERE subject = "
                                  "'RE: Quote please'")
        reply = {h: o for o, h in self.emails.items()}[_cell(out["rows"][0]["cells"][0])]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            # the owner's reply removed at the source: the question is unanswered again
            pg.execute("INSERT INTO ontology.object_removals (object_id, basis, since) "
                       "VALUES (%s, 'source_soft_delete', '2026-01-01T00:00:00Z')", (reply,))
            try:
                bc.refresh_changed(pg, "emails", self.marks(pg))
                self.assertIsNone(self.flags()["Quote please"][2])
            finally:
                pg.execute("DELETE FROM ontology.object_removals WHERE object_id = %s",
                           (reply,))
            # the rules over a process pool (as on the served 3.3M) decide the same
            with mock.patch.multiple(bc, FLAG_POOL_MIN=0, FLAG_BATCH=3, FLAG_WORKERS=2):
                bc.refresh_changed(pg, "emails", {"ver": 0, "link": 0})
            self.assertEqual(self.flags(), before)
            with mock.patch.dict(os.environ, {"PLX_STATE_DIR": str(self.root / "none")}):
                bc.refresh_changed(pg, "emails", {"ver": 0, "link": 0})
            try:
                got = self.flags()
                self.assertEqual({v[1] for v in got.values()}, {None})   # not known
                self.assertEqual({v[2] for v in got.values()}, {None})
                self.assertIs(got["Spring range"][0], False)   # no envelope: no signal
                self.assertIs(got["Weekly digest"][0], True)   # the form still says
            finally:
                bc.refresh_changed(pg, "emails", {"ver": 0, "link": 0})
            self.assertEqual(self.flags(), before)

    def test_an_in_place_refresh_never_stamps_a_code_fingerprint(self):
        """Rows a refresh keeps were built by the code the state row records: only a build
        stamps ``code`` (a refresh that stamped the current one would hide old rows)."""
        import psycopg
        q = f"SELECT notes->>'code' FROM {bv.SCHEMA}.state WHERE view = 'emails'"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            built = pg.execute(q).fetchone()[0]
            self.assertTrue(built)   # the build stamped it
            try:
                pg.execute(f"UPDATE {bv.SCHEMA}.state SET notes = notes || "
                           "'{\"code\": \"older\"}'::jsonb WHERE view = 'emails'")
                bc.refresh_changed(pg, "emails", self.marks(pg))
                self.assertEqual(pg.execute(q).fetchone()[0], "older")
                pg.execute(f"UPDATE {bv.SCHEMA}.state SET notes = notes - 'code' WHERE "
                           "view = 'emails'")
                bc.refresh_changed(pg, "emails", self.marks(pg))
                self.assertIsNone(pg.execute(q).fetchone()[0])
            finally:
                pg.execute(f"UPDATE {bv.SCHEMA}.state SET notes = notes || "
                           "jsonb_build_object('code', %s::text) WHERE view = 'emails'",
                           (built,))

    def marks(self, pg):
        notes = pg.execute(f"SELECT notes FROM {bv.SCHEMA}.state WHERE view = 'emails'"
                           ).fetchone()[0]
        return (json.loads(notes) if isinstance(notes, str) else notes)["marks"]

    def test_no_build_or_refresh_statement_reads_a_table_per_row(self):
        """Every statement the build and a refresh run, planned as the served data would
        be (seq scans off: an index probe wherever one exists): no subplan reads a table
        or a spilled set per row. Only a correlated index probe is allowed (one lookup per
        row); a hashed subplan over a temp table is not (past work_mem it re-reads the
        whole set for each row: the 11-18 cm_flag statement sat 90 min over 3.3M mails)."""
        import psycopg
        found: list[tuple[str, str]] = []
        readers: list[_Explaining] = []

        def connect():   # the build's row readers' connections are planned too
            readers.append(_Explaining(psycopg.connect(self.dsn, autocommit=True), found))
            return readers[-1]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            probe = _Explaining(pg, found)
            bc.refresh_changed(probe, "emails", {"ver": 0, "link": 0})
            try:
                with pg.transaction(force_rollback=True):
                    cols = ", ".join(f'"{n}" {t}' for n, t in bc.columns(bv.VIEWS["emails"]))
                    pg.execute(f"CREATE TABLE {bv.SCHEMA}.emails_probe ({cols})")
                    at = len(probe.seen)
                    bc.fill(bc.SPECS["emails"], probe, "emails_probe", connect=connect)
                    built = pg.execute(f"SELECT * FROM {bv.SCHEMA}.emails_probe").fetchall()
                    served = pg.execute(f"SELECT * FROM {bv.SCHEMA}.emails").fetchall()
                    left = [pg.execute("SELECT to_regclass(%s)", (f"{bv.SCHEMA}.{n}",)
                                       ).fetchone()[0] for n in bc.stage_names("emails_probe")]
            finally:   # the readers commit the stage: the rolled-back probe leaves it
                bc.drop_stage(pg, "emails_probe")
        self.assertGreater(probe.planned, 20, "the statements were planned")
        self.assertTrue(any("cm_flag" in sql for sql, _ in probe.seen), "the flags ran")
        seen = probe.seen[at:] + [x for r in readers for x in r.seen]
        self.assertTrue(any(re.match(r"\s*with r as materialized", sql, re.I)
                            for r in readers for sql, _ in r.seen), "the rows were planned")
        self.assertEqual(found, [], "a subplan reads a table per row")
        # the build writes each row of its table ONCE (11-18: every whole-table UPDATE in
        # the build's one transaction left a dead copy: /data fell 1.7 GB a minute)
        target = re.compile(rf"\b{bv.SCHEMA}\.emails_probe(_stage(_text)?)?\b")
        rewrites = [sql[:120] for sql, _ in seen if re.match(
            r"\s*(update|delete)\b", sql, re.I) and target.search(sql)]
        self.assertEqual(rewrites, [], "a build statement rewrites rows it wrote")
        writes = [sql for sql, _ in probe.seen[at:] if re.match(
            rf"\s*insert into {bv.SCHEMA}\.emails_probe\b", sql, re.I)]
        self.assertTrue(writes and all(" s.sn > " in w for w in writes),
                        "the table is written from the stage, by row number ranges")
        self.assertEqual(left, [None, None], "the stage is dropped")
        self.assertEqual(sorted(map(repr, built)), sorted(map(repr, served)),
                         "the build's rows, every column")

    def test_a_build_statement_is_capped_by_the_build_timeout(self):
        """The build's own writes run under BUILD_STATEMENT_MS, as a refresh's do (the
        11-18 emails build ran statements uncapped: one sat 90 min)."""
        seen = []

        def stop(n, reader, write, **kw):
            seen.append(write.execute("SHOW statement_timeout").fetchone()[0])
            raise RuntimeError("probe")
        with mock.patch.object(bv, "build_view", side_effect=stop), \
                self.assertRaisesRegex(RuntimeError, "views not built"):
            bv.build(self.dsn, ["emails"], schema=self.compiled)
        self.assertEqual(seen, [f"{bv.BUILD_STATEMENT_MS // 60000}min"])

    def test_a_build_short_of_disk_stops_and_keeps_the_served_table(self):
        """Below the free-space floor the build stops cleanly: its transaction rolled back
        (no ``emails_next``, no stage left), the served table untouched; a disk whose free
        space cannot be read stops it too (fail closed)."""
        import psycopg
        table = f"{bv.SCHEMA}.emails"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            before = sorted(map(repr, pg.execute(f"SELECT * FROM {table}").fetchall()))
        def low_at_the_write(path):   # room for the stage, then short before the write
            return (24 if "writing the table" in steps[-1] else 400) * 1024 ** 3
        steps = [""]
        guard = bc._disk_guard

        def step(write, path, at, need=0):
            steps.append(at)
            return guard(write, path, at, need)
        for reader, said in ((low_at_the_write, "floor .* before writing the table"),
                             (OSError("gone"), "cannot be read")):
            with self.subTest(said=said), \
                    mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": ""}), \
                    mock.patch.object(bc, "_disk_guard", step), \
                    mock.patch.object(bc, "free_bytes", side_effect=reader), \
                    self.assertRaisesRegex(RuntimeError, said):
                bv.build(self.dsn, ["emails"], schema=self.compiled)
            with psycopg.connect(self.dsn, autocommit=True) as pg:
                for left in ("emails_next", "emails_next_stage"):
                    self.assertIsNone(pg.execute("SELECT to_regclass(%s)", (
                        f"{bv.SCHEMA}.{left}",)).fetchone()[0], left)
                self.assertEqual(sorted(map(repr, pg.execute(
                    f"SELECT * FROM {table}").fetchall())), before)
        self.assertIn("the mail flags", steps, "it stopped after the stage was written")
        # an unreadable floor is refused, never read as no floor
        with mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "lots"}), \
                self.assertRaisesRegex(RuntimeError, "PLX_BIZ_MIN_FREE_GB"):
            bc._min_free_bytes()

    def test_a_paused_wal_budget_never_holds_a_build_open(self):
        """11-22: a build waited for the WAL budget inside its one transaction for an hour,
        holding ~20 GB. Paused before it starts: nothing begun. Paused at the write: rolled
        back at once. Either way no ``emails_next`` and the served table unchanged."""
        import psycopg
        from operating.pg import wal_budget
        table = f"{bv.SCHEMA}.emails"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            before = sorted(map(repr, pg.execute(f"SELECT * FROM {table}").fetchall()))
        calls = []

        def at_start(budget):
            calls.append(budget.job)
            return "standby_missing:plx_s7d3d"

        def at_write(budget):
            calls.append(budget.job)
            return "standby_missing:plx_s7d3d" if len(calls) > 1 else None
        for blocked, said in ((at_start, "not begun"), (at_write, "paused the write")):
            calls.clear()
            with self.subTest(said=said), _NO_FLOOR, \
                    mock.patch.object(wal_budget.Budget, "blocked", blocked), \
                    mock.patch.object(wal_budget.Budget, "wait",
                                      side_effect=AssertionError("waited")):
                # a quiet retry: reported as paused, never a failure (no alarm)
                got = bv.build(self.dsn, ["emails"], schema=self.compiled)
            self.assertEqual([g["view"] for g in got], ["emails"])
            self.assertRegex(got[0]["paused"], said)
            with psycopg.connect(self.dsn, autocommit=True) as pg:
                for left in ("emails_next", "emails_next_stage"):
                    self.assertIsNone(pg.execute("SELECT to_regclass(%s)", (
                        f"{bv.SCHEMA}.{left}",)).fetchone()[0], left)
                self.assertEqual(sorted(map(repr, pg.execute(
                    f"SELECT * FROM {table}").fetchall())), before)

    def test_a_whole_build_is_not_begun_without_room_for_its_peak(self):
        """The disk must hold BUILD_NEED_FACTOR x the live table above the floor before a
        whole build starts; the default floor sits above the 60 GB slot-guard line."""
        import psycopg
        from operating.pg.build_pause import BUILD_NEED_FACTOR
        self.assertGreater(bc.MIN_FREE_GB_DEFAULT, 60)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            live = pg.execute("SELECT pg_total_relation_size(%s::regclass)",
                              (f"{bv.SCHEMA}.emails",)).fetchone()[0]
        floor = 10 * 1024 ** 3
        just_short = floor + int(BUILD_NEED_FACTOR * live) - 1
        with mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "10"}), \
                mock.patch.object(bc, "free_bytes", return_value=just_short), \
                self.assertRaisesRegex(RuntimeError, "before starting the build"):
            bv.build(self.dsn, ["emails"], schema=self.compiled)

    def _ranges(self, n):
        """``CHUNK`` patched so the store's versions make about ``n`` ranges."""
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            top = int(bc.marks(pg)["ver"])
        return mock.patch.object(bc, "CHUNK", max(1, top // n + 1))

    def test_the_rows_are_read_by_many_connections_and_match_one(self):
        """Every core reads the rows (owner 11-18: the row read sat on one core ~1 h): the
        version ranges are taken from one queue by ``PLX_BIZ_READ_WORKERS`` connections at
        once. The table built so is the served one row for row, every column (no row lost
        or doubled across ranges or readers); more than one connection read ranges; a
        worker count that is not a whole number of 1 or more is refused."""
        import threading
        import time
        import psycopg
        table = f"{bv.SCHEMA}.emails"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            before = sorted(map(repr, pg.execute(f"SELECT * FROM {table}").fetchall()))
        readers, real = [], bc._rows_sql

        def rows_sql(*args, **kwargs):   # which reader read a range (each a moment)
            readers.append(threading.get_ident())
            time.sleep(0.05)
            return real(*args, **kwargs)
        with self._ranges(12), mock.patch.object(bc, "_rows_sql", rows_sql), \
                mock.patch.dict(os.environ, {"PLX_BIZ_READ_WORKERS": "4"}):
            got = bv.build(self.dsn, ["emails"], schema=self.compiled)
        self.assertGreaterEqual(len(readers), 10, "the versions were read in ranges")
        self.assertGreater(len(set(readers)), 1, "more than one connection read rows")
        self.assertLessEqual(len(set(readers)), 4)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            after = sorted(map(repr, pg.execute(f"SELECT * FROM {table}").fetchall()))
            for n in bc.stage_names("emails_next"):
                self.assertIsNone(pg.execute("SELECT to_regclass(%s)", (
                    f"{bv.SCHEMA}.{n}",)).fetchone()[0], n)
        self.assertEqual(got[0]["rows"], len(before))
        self.assertEqual(after, before, "every row, every column")
        for raw in ("0", "-2", "lots", "1.5"):
            with self.subTest(raw=raw), \
                    mock.patch.dict(os.environ, {"PLX_BIZ_READ_WORKERS": raw}), \
                    self.assertRaisesRegex(RuntimeError, "PLX_BIZ_READ_WORKERS"):
                bc.read_workers()
        with mock.patch.dict(os.environ, {"PLX_BIZ_READ_WORKERS": ""}):
            self.assertEqual(bc.read_workers(), max(1, os.cpu_count() or 1))

    def test_the_build_refresh_and_recheck_commands_never_lower_their_priority(self):
        """A build, refresh or recheck runs at full speed (the unit bounds its
        memory, the build its disk); the commands never renice themselves."""
        def niced(_inc):
            raise AssertionError("a command lowered its priority")
        with mock.patch("os.nice", side_effect=niced), \
                mock.patch.object(bv, "refresh", return_value={}), \
                mock.patch.object(bv, "_stop_on_term"), \
                mock.patch.object(bc, "recheck_all", return_value=[]), \
                mock.patch("builtins.print"):
            self.assertEqual(bv.main(["refresh", "--dsn", self.dsn]), 0)
            self.assertEqual(bc.main(["recheck", "--dsn", self.dsn]), 0)

    def test_a_reader_failing_stops_the_build_and_leaves_no_stage(self):
        """One reader failing mid-way (here: the disk floor before its range) stops the
        others and the build: no ``emails_next`` and none of the stage its readers
        committed is left, the served table untouched. A stage a killed build left is
        swept by the next refresh."""
        import psycopg
        table = f"{bv.SCHEMA}.emails"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            before = sorted(map(repr, pg.execute(f"SELECT * FROM {table}").fetchall()))
        guard, seen = bc._disk_guard, []

        def step(write, path, at, need=0):
            seen.append(at)
            if at.startswith("versions") and len([s for s in seen if s.startswith(
                    "versions")]) == 3:
                raise RuntimeError(f"disk below the floor, build stopped before {at}")
            return guard(write, path, at, need)
        with self._ranges(12), mock.patch.object(bc, "_disk_guard", step), \
                mock.patch.dict(os.environ, {"PLX_BIZ_READ_WORKERS": "4"}), \
                self.assertRaisesRegex(RuntimeError, "views not built.*stopped before versions"):
            bv.build(self.dsn, ["emails"], schema=self.compiled)
        self.assertNotIn("the mail flags", seen, "nothing ran after the readers")
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for left in ("emails_next", *bc.stage_names("emails_next")):
                self.assertIsNone(pg.execute("SELECT to_regclass(%s)", (
                    f"{bv.SCHEMA}.{left}",)).fetchone()[0], left)
            self.assertEqual(sorted(map(repr, pg.execute(
                f"SELECT * FROM {table}").fetchall())), before)
            # a killed build's committed stage (no unwinding ran): the sweep drops it
            for n in bc.stage_names("emails_next"):
                pg.execute(f"CREATE UNLOGGED TABLE {bv.SCHEMA}.{n} (x int)")
            swept = bv.sweep_leftovers(pg)
            self.assertEqual(sorted(swept), sorted(bc.stage_names("emails_next")))
            self.assertIsNotNone(pg.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0])

    def test_refresh_in_place_reads_the_same_rows_and_drops_a_removed_one(self):
        import psycopg
        table = f"{bv.SCHEMA}.emails"
        # every column: a refresh sets the derived ones by the build's own definitions,
        # so a refresh of every record reproduces the build row for row (no drift)
        cols = "*"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            before = sorted(map(repr, pg.execute(f"SELECT {cols} FROM {table}").fetchall()))
            got = bc.refresh_changed(pg, "emails", {"ver": 0, "link": 0})
            self.assertEqual(sorted(map(repr, pg.execute(f"SELECT {cols} FROM {table}"
                                                         ).fetchall())), before)
            self.assertEqual(got["rows"], len(before))
            victim = sorted(self.emails)[0]
            pg.execute("INSERT INTO ontology.object_removals (object_id, basis, since) "
                       "VALUES (%s, 'source_soft_delete', '2026-01-01T00:00:00Z')", (victim,))
            try:
                got = bc.refresh_changed(pg, "emails", got["marks"])
                self.assertEqual(got["dropped"], 1, got)
                self.assertNotIn(self.emails[victim], self.handles(tmp.OWNER))
            finally:
                pg.execute("DELETE FROM ontology.object_removals WHERE object_id = %s",
                           (victim,))
                bc.refresh_changed(pg, "emails", {"ver": 0, "link": 0})
            self.assertEqual(len(pg.execute(f"SELECT 1 FROM {table}").fetchall()),
                             len(before))


    def test_a_moved_link_rule_decides_every_rows_links_again(self):
        """A refresh reads only the records changed since its marks, so a row written under an
        older link rule kept its links for good (11-19: 57 of 160,150 sampled mails held the
        customers their stored links name). The build keeps the rule's stamp in its marks;
        a refresh whose marks hold another stamp relinks every row, and one that holds the
        current stamp leaves unchanged rows alone."""
        import psycopg
        table = f"{bv.SCHEMA}.emails"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            got = bc.refresh_changed(pg, "emails", {"ver": 0, "link": 0})
            self.assertEqual(got["marks"]["links"], bc.links_rule(bc.SPECS["emails"]))
            truth = pg.execute(f"SELECT handle, customers, suppliers, records FROM {table} "
                               "ORDER BY handle").fetchall()
            stale = "(customers, suppliers) = (ARRAY['ghost'], ARRAY['ghost'])"
            victim = truth[0][0]
            try:
                pg.execute(f"UPDATE {table} SET {stale} WHERE handle = %s", (victim,))
                bc.refresh_changed(pg, "emails", got["marks"])    # current stamp, nothing new
                self.assertEqual(pg.execute(f"SELECT customers FROM {table} WHERE handle = %s",
                                            (victim,)).fetchone()[0], ["ghost"])
                bc.refresh_changed(pg, "emails", {**got["marks"], "links": "older-rule"})
                self.assertEqual(pg.execute(f"SELECT handle, customers, suppliers, records "
                                            f"FROM {table} ORDER BY handle").fetchall(), truth)
            finally:
                pg.execute(f"UPDATE {table} SET customers = %s, suppliers = %s, records = %s "
                           "WHERE handle = %s", (truth[0][1], truth[0][2], truth[0][3], victim))

    def test_a_name_written_by_many_sender_domains_is_a_common_word(self):
        """``message_parties.common_names`` reads the mail table's own words and sender
        domains: a word three rows carry from three domains is common when the bar is 3 and
        not when it is 4; nothing else is; a table that is not built has none."""
        import psycopg
        from operating.pg import message_parties as mp
        table = f"{bv.SCHEMA}.emails"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            rows = pg.execute(f"SELECT handle, sender_domain, words FROM {table} ORDER BY "
                              "handle LIMIT 3").fetchall()
            self.assertEqual(len(rows), 3)
            try:
                for i, (handle, _d, _w) in enumerate(rows):
                    pg.execute(f"UPDATE {table} SET sender_domain = %s, words = "
                               "to_tsvector('simple', 'zqxvord') WHERE handle = %s",
                               (f"d{i}.test", handle))
                for bar, expected in ((3, {"zqxvord"}), (4, set())):
                    mp._SPREAD.update(at=0.0, key=None)
                    with mock.patch.object(mp, "MAX_SPREAD", bar):
                        self.assertEqual(set(mp.common_names(pg, ["zqxvord", "plain"])),
                                         expected)
            finally:
                mp._SPREAD.update(at=0.0, key=None)
                for handle, dom, words in rows:
                    pg.execute(f"UPDATE {table} SET sender_domain = %s, words = %s WHERE "
                               "handle = %s", (dom, words, handle))

    def test_mail_gains_the_party_its_domain_or_subject_names_and_only_that(self):
        """``message_parties.apply`` on the real table: a customer whose stored address holds a
        participant domain of some mails, and a supplier whose name the subject of others
        spells, are linked to exactly those rows (both kinds in one rewrite of a row); a
        second apply writes nothing; an own-staff domain links nothing."""
        import psycopg
        from ontology import mentions
        from operating.pg import message_parties as mp
        table = f"{bv.SCHEMA}.emails"
        made = ["staff", "customers", "suppliers", "sales_orders", "sales_invoices",
                "customer_balances", "purchase_orders", "supplier_invoices",
                "supplier_balances"]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute(f"SET search_path = {bv.SCHEMA}")
            existing = [t for t in made if pg.execute("SELECT to_regclass(%s)", (
                f"{bv.SCHEMA}.{t}",)).fetchone()[0]]
            self.assertEqual(existing, [], "the fixture builds only the mail table")
            before = pg.execute(f"SELECT handle, customers, suppliers, subject FROM {table} "
                                "ORDER BY handle").fetchall()
            dom = pg.execute(f"SELECT d FROM {table}, unnest(domains) d WHERE cardinality("
                             "domains) <= 3 GROUP BY d ORDER BY count(*) DESC LIMIT 1"
                             ).fetchone()[0]
            word = "zqxvord"
            try:
                pg.execute("CREATE TABLE staff (name text, email text)")
                pg.execute("CREATE TABLE customers (handle text, name text, email text)")
                pg.execute("CREATE TABLE suppliers (handle text, name text, email text, "
                           "website text)")
                for t, k in (("sales_orders", "customer"), ("sales_invoices", "customer"),
                             ("customer_balances", "customer"), ("purchase_orders", "supplier"),
                             ("supplier_invoices", "supplier"),
                             ("supplier_balances", "supplier")):
                    pg.execute(f"CREATE TABLE {t} ({k}_handle text)")
                pg.execute("INSERT INTO customers VALUES ('c-1', 'Quorvex Holdings', %s)",
                           (f"ap@{dom}",))
                pg.execute("INSERT INTO sales_orders VALUES ('c-1')")
                pg.execute("INSERT INTO suppliers VALUES ('s-1', %s, NULL, NULL)",
                           (f"{word} Chemicals",))
                pg.execute("INSERT INTO purchase_orders VALUES ('s-1')")
                pg.execute(f"UPDATE {table} SET subject = %s WHERE handle = (SELECT handle "
                           f"FROM {table} ORDER BY handle LIMIT 1)", (f"Re: {word} chemicals PO",))
                got = mp.apply(pg, "emails", table)
                self.assertGreater(got["rows_updated"], 0, got)
                for handle, cust, supp, domains, subject in pg.execute(
                        f"SELECT handle, customers, suppliers, domains, subject FROM {table}"
                        ).fetchall():
                    held = len(domains) <= mp.MAX_DOMAINS and any(
                        mentions.registrable_domain(d) == mentions.registrable_domain(dom)
                        for d in domains)
                    self.assertEqual("c-1" in cust, held, handle)
                    self.assertEqual("s-1" in supp, word in (subject or ""), handle)
                self.assertEqual(mp.apply(pg, "emails", table)["rows_updated"], 0)
            finally:
                pg.execute("DROP TABLE IF EXISTS " + ", ".join(made))
                for handle, cust, supp, subject in before:
                    pg.execute(f"UPDATE {table} SET customers = %s, suppliers = %s, subject = "
                               "%s WHERE handle = %s", (cust, supp, subject, handle))

    # ------------------------------------------------------------- search and open

    def search(self, actor, person=False, **args):
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            tool = self.tool()
            return (tool.person_search if person else tool.comms_search)(args, actor)

    def found(self, actor, person=False, **args):
        """Every hit, page by page (no hit lost or doubled across pages); ``person``: a
        person's own search."""
        got, cursor = [], None
        for _ in range(20):
            out = self.search(actor, person, limit=50, **args, **({"cursor": cursor} if
                                                                  cursor else {}))
            got += [h["citation"] for h in out["documents"]]
            cursor = out["completeness"]["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(got), len(set(got)), "a hit doubled across pages")
        return set(got)

    def texts(self):
        import psycopg
        from operating.pg import comms_search as cs
        with psycopg.connect(self.dsn) as pg:
            rows = pg.execute("SELECT f.citation, f.field_id, c.start, c.body FROM "
                              "ontology.text_fields f JOIN ontology.text_chunks c ON "
                              "c.field_id = f.field_id ORDER BY 1, 2, 3").fetchall()
        out: dict[str, list] = {}
        for h, f, start, body in rows:
            out.setdefault(h, []).append((f, start, body))
        return {h: cs.assemble(v) for h, v in out.items()}

    def test_search_hits_are_the_doors_rows_and_never_more(self):
        texts = self.texts()
        runtime = self.runtime()
        for actor in READERS:
            with self.subTest(actor=sorted(actor.principals)):
                mine = self.handles(actor)
                gate = {self.emails[o] for o in self.gated(runtime, actor)}
                body = {h for h in mine if "agreed" in texts.get(h, "")}
                self.assertTrue(body)
                self.assertEqual(self.found(actor, query="agreed"), body)
                # header words: the subject (1/10/60 senders' notes, two pages)
                notes = {_cell(r["cells"][0]) for r in self.sql(
                    actor, "SELECT handle FROM emails WHERE subject ILIKE '%note%'")["rows"]}
                self.assertEqual(self.found(actor, query="note"), notes)
                self.assertTrue(self.found(actor, query="note") <= gate)
                # Freightcom is never found, by subject or by any word of it
                self.assertEqual(self.found(actor, query="Freightcom"), set())
                self.assertEqual(self.found(actor, query="pickup"), set())
        self.assertLess(len(self.found(tmp.NARROW, query="agreed")),
                        len(self.found(tmp.OWNER, query="agreed")))
        # a body-only word, and dates as a plain predicate
        relay = {_cell(r["cells"][0]) for r in self.sql(
            tmp.OWNER, "SELECT handle FROM emails WHERE subject = 'Booking 778'")["rows"]}
        self.assertEqual(self.found(tmp.OWNER, query="communicate"), relay)
        self.assertEqual(self.found(tmp.OWNER, query="agreed", since="2100-01-01"), set())
        # a dated word search (its dates bound the word match too) keeps exactly the rows
        # on each side of an instant
        hits = self.found(tmp.OWNER, query="samples")
        at = {_cell(r["cells"][0]): _cell(r["cells"][1]) for r in self.sql(
            tmp.OWNER, "SELECT handle, to_char(received_at AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS') FROM emails")["rows"]}
        last = max(at[h] for h in hits)
        after = {h for h in hits if at[h] >= last}
        self.assertTrue(after and after < hits, (hits, at))
        self.assertEqual(self.found(tmp.OWNER, query="samples", since=last), after)
        self.assertEqual(self.found(tmp.OWNER, query="samples", until=last), hits - after)

    def test_a_few_hit_search_widens_to_some_words_and_meaning_through_the_same_door(self):
        """Fewer than WIDEN_BELOW rows holding every word: the rows holding some of the
        words (most first) and the rows a meaning ranking names are added, each read
        through the reader's own guarded views: never a row the reader's word search
        could not show, never Freightcom, the dates still bound them."""
        import psycopg
        from operating.search import plug
        relay = {_cell(r["cells"][0]) for r in self.sql(
            tmp.OWNER, "SELECT handle FROM emails WHERE subject = 'Booking 778'")["rows"]}
        out = self.search(tmp.OWNER, query="communicate zzqqnoword")
        self.assertEqual({h["citation"] for h in out["documents"]}, relay)
        self.assertEqual(out["documents"][0]["match"], "1 of 2 words")
        self.assertIn("widened", out["searched"])
        self.assertIsNone(out["completeness"]["next_cursor"])
        # every word held: nothing widened, as before
        self.assertNotIn("match", self.search(tmp.OWNER, query="communicate")["documents"][0])
        self.assertEqual(self.found(tmp.OWNER, query="Freightcom zzqqnoword"), set())
        with psycopg.connect(self.dsn) as pg:
            oids = [r[0] for r in pg.execute(
                f"SELECT object_ids[1] FROM {bv.SCHEMA}.emails ORDER BY handle")]
        # Freightcom mail: a person's own search shows it, the AI's never (meaning or not)
        freight = self.found(tmp.OWNER, person=True, query="Freightcom")
        self.assertTrue(freight)
        ranked = plug.Ranked([{"object_id": o, "handle": None} for o in oids], "hybrid:x")
        with mock.patch.object(plug, "enabled", return_value=True), \
                mock.patch.object(plug, "_rank", return_value=ranked), \
                mock.patch.object(plug, "_gated", side_effect=lambda r, *a: r):
            for actor in READERS:
                with self.subTest(actor=sorted(actor.principals)):
                    mine = self.handles(actor)
                    docs = self.search(actor, query="zzqq yyxx", limit=50)["documents"]
                    got = [h["citation"] for h in docs]
                    self.assertTrue(got)
                    self.assertTrue(set(got) <= mine, set(got) - mine)
                    self.assertFalse(set(got) & freight)
                    meant = [h["meaning_rank"] for h in docs]
                    self.assertEqual(meant, sorted(meant))
            self.assertLess(len(self.search(tmp.NARROW, query="zzqq yyxx", limit=50)
                                ["documents"]),
                            len(self.search(tmp.OWNER, query="zzqq yyxx", limit=50)
                                ["documents"]))
            self.assertEqual(self.search(tmp.OWNER, query="zzqq yyxx", since="2100-01-01")
                             ["documents"], [])
            # a page the word match fills still shows up to MEANING_EXTRA rows close in
            # meaning, none it already listed; a later page none
            out = self.search(tmp.OWNER, query="communicate", limit=1)
            docs = out["documents"]
            self.assertNotIn("match", docs[0])
            self.assertTrue(1 < len(docs) <= 1 + ts.MEANING_EXTRA)
            self.assertEqual(len({h["citation"] for h in docs}), len(docs))
            self.assertTrue(all("close in meaning" in h["match"] for h in docs[1:]))
            self.assertIn("widened", out["searched"])
            if out["completeness"]["next_cursor"]:
                self.assertFalse(any("match" in h for h in self.search(
                    tmp.OWNER, query="communicate", limit=1,
                    cursor=out["completeness"]["next_cursor"])["documents"]))
        # read ahead for every ranked object, shown only for those the gate passed: when
        # the gate passes none, no row comes back as close in meaning, though they were read
        with mock.patch.object(plug, "enabled", return_value=True), \
                mock.patch.object(plug, "_rank", return_value=ranked), \
                mock.patch.object(plug, "_gated",
                                  side_effect=lambda r, *a: plug.Ranked([], "hybrid:x")):
            docs = self.search(tmp.OWNER, query="zzqq yyxx", limit=50)["documents"]
        self.assertFalse([h for h in docs if "meaning_rank" in h
                          or "close in meaning" in str(h.get("match"))])
        # the ranking failed or late: a few-hit search still widens by the words held
        with mock.patch.object(plug, "enabled", return_value=True), \
                mock.patch.object(plug, "_rank", side_effect=RuntimeError("down")):
            out = self.search(tmp.OWNER, query="communicate zzqqnoword")
            self.assertEqual({h["citation"] for h in out["documents"]}, relay)
            self.assertEqual(out["documents"][0]["match"], "1 of 2 words")

    def test_a_full_pages_meaning_rows_never_come_back_on_a_later_page(self):
        """A meaning ranking that names rows the word match lists on later pages: a full
        first page adds none of them (each comes on its own page), only rows the word
        match never lists; walked page by page no hit repeats and none is lost."""
        import psycopg
        from operating.search import plug
        notes = self.found(tmp.OWNER, query="note")
        with psycopg.connect(self.dsn) as pg:
            oid = dict(pg.execute(f"SELECT handle, object_ids[1] FROM {bv.SCHEMA}.emails"))
        others = sorted(h for h in self.handles(tmp.OWNER) - notes if h in oid)[:3]
        self.assertTrue(len(notes) > 4 and others)
        # within the ranking's first MEANING_K, fewer others than MEANING_EXTRA: the
        # rest of the room would be taken by later pages' rows
        ranked = plug.Ranked([{"object_id": oid[h], "handle": None}
                              for h in others + sorted(notes)], "hybrid:x")
        with mock.patch.object(plug, "enabled", return_value=True), \
                mock.patch.object(plug, "_rank", return_value=ranked), \
                mock.patch.object(plug, "_gated", side_effect=lambda r, *a: r):
            got, cursor = [], None
            for _ in range(len(notes) + 2):
                out = self.search(tmp.OWNER, query="note", limit=2,
                                  **({"cursor": cursor} if cursor else {}))
                got += [(h["citation"], "match" in h) for h in out["documents"]]
                cursor = out["completeness"]["next_cursor"]
                if not cursor:
                    break
        cites = [c for c, _ in got]
        self.assertEqual(len(cites), len(set(cites)), "a hit came back on a later page")
        self.assertEqual({c for c, added in got if not added}, notes)
        self.assertTrue({c for c, added in got if added} <= set(others))
        self.assertTrue(any(added for _, added in got))

    def test_header_matches_rank_first_and_pages_lose_or_repeat_nothing(self):
        """A row whose header (subject, addresses) holds every query word comes before the
        rows matched in their text only, newest first within each; walked 1, 3 or 50 at a
        time the hits are the same set, each once, in that order."""
        import psycopg
        from operating.pg import comms_search as cs
        mixed = 0
        for word in ("agreed", "note", "samples", "communicate", "booking", "order"):
            with psycopg.connect(self.dsn) as pg:
                header = {r[0] for r in pg.execute(
                    f"SELECT handle FROM {bv.SCHEMA}.emails WHERE words @@ %s::tsquery",
                    (cs.tsquery(word),))}
                at = dict(pg.execute(f"SELECT handle, received_at FROM {bv.SCHEMA}.emails"))
            # the word match's own rows (a widening, by stem or meaning, is a first
            # page's of few rows: never paged)
            widened = {h["citation"] for h in self.search(tmp.OWNER, query=word, limit=50)[
                "documents"] if "match" in h}
            every = [h for h in self.found(tmp.OWNER, query=word) if h not in widened]
            for limit in (1, 3, 50):
                with self.subTest(word=word, limit=limit):
                    walk, cursor = [], None
                    for _ in range(len(every) + 2):
                        out = self.search(tmp.OWNER, query=word, limit=limit,
                                          **({"cursor": cursor} if cursor else {}))
                        walk += [h["citation"] for h in out["documents"]]
                        cursor = out["completeness"]["next_cursor"]
                        if not cursor:
                            break
                    self.assertEqual(len(walk), len(set(walk)), "a hit doubled across pages")
                    walk = [h for h in walk if h not in widened]
                    self.assertEqual(set(walk), set(every))
                    tiers = [h in header for h in walk]
                    self.assertEqual(tiers, sorted(tiers, reverse=True), (word, walk))
                    for tier in (True, False):
                        dates = [at[h] for h, t in zip(walk, tiers) if t is tier]
                        self.assertEqual(dates, sorted(dates, reverse=True))
            first = [at[h] for h in every if h in header]
            rest = [at[h] for h in every if h not in header]
            mixed += bool(first and rest and min(first) < max(rest))
        # the rank changed an order somewhere (an older header match before a newer text one)
        self.assertTrue(mixed)

    def test_a_search_is_the_same_whatever_rows_the_walk_reads_first(self):
        """The search walks each view's newest rows before it reads the word index's whole
        match (``comms_search.WALK_ROWS``): a walk of 1, 2 or 7 rows (the rest from the
        index) and one over every row give each reader the same hits, in the same order,
        page by page (1, 10, 60 a page), with dates, a party and a cursor's tier; so does a
        rare word's search, which reads the index at once (``comms_search.walk_rows``)."""
        from operating.pg import comms_search as cs

        tagged = mock.patch.object(ts, "_people_tag", return_value=self.tag)
        tagged.start()
        self.addCleanup(tagged.stop)
        tool = self.tool()   # one tool (its own connections) for the hundreds of pages

        def pages(actor, limit, **args):
            got, cursor = [], None
            for _ in range(200):
                out = tool.comms_search({"limit": limit, **args, **(
                    {"cursor": cursor} if cursor else {})}, actor)
                got.append([h["citation"] for h in out["documents"]])
                cursor = out["completeness"]["next_cursor"]
                if not cursor:
                    return got
            self.fail("the pages never ended")
        hits = self.found(tmp.OWNER, query="samples")
        at = {_cell(r["cells"][0]): _cell(r["cells"][1]) for r in self.sql(
            tmp.OWNER, "SELECT handle, to_char(received_at AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS') FROM emails")["rows"]}
        middle = sorted(at[h] for h in hits)[len(hits) // 2]
        asks = [{"query": w} for w in ("agreed", "note", "samples", "order", "booking")] + [
            {"query": "samples", "since": middle}, {"query": "samples", "until": middle},
            {"query": "note", "participant": "@vendor.example"},
            {"query": "samples", "participant": "@vendor.example", "since": middle},
            {"query": "note", "participant": tmp.SUPPLIER}]
        import psycopg
        with psycopg.connect(self.dsn) as pg:   # the fixture's words are rare: no walk
            self.assertEqual(cs.walk_rows(pg, cs.tsquery("samples")), 1)
            with mock.patch.object(cs, "COMMON_CHUNKS", 1):
                self.assertEqual(cs.walk_rows(pg, cs.tsquery("samples")), cs.WALK_ROWS)
        walked = 0
        for actor in READERS:
            for ask in asks:
                for limit in (1, 10, 60):
                    with mock.patch.object(cs, "WALK_ROWS", 100000), \
                            mock.patch.object(cs, "COMMON_CHUNKS", 0):
                        whole = pages(actor, limit, **dict(ask))
                    # a rare word (the fixture's every word, by the estimate) reads the
                    # word index at once: the same pages
                    self.assertEqual(pages(actor, limit, **dict(ask)), whole)
                    for rows in (1, 2, 7):
                        with self.subTest(actor=sorted(actor.principals), ask=ask,
                                          limit=limit, walk=rows), \
                                mock.patch.object(cs, "WALK_ROWS", rows), \
                                mock.patch.object(cs, "COMMON_CHUNKS", 0):   # every word walks
                            self.assertEqual(pages(actor, limit, **dict(ask)), whole)
                    if "participant" in ask:
                        # a party's own rows read first (``PARTY_ROWS``) or, past them, the
                        # walk: the same pages
                        for most in (1, 3, 100000):
                            with self.subTest(actor=sorted(actor.principals), ask=ask,
                                              limit=limit, party_rows=most), \
                                    mock.patch.object(cs, "PARTY_ROWS", most):
                                self.assertEqual(pages(actor, limit, **dict(ask)), whole)
                    walked += sum(map(len, whole)) > 7
        self.assertTrue(walked, "no search held more rows than the walk: the index read "
                                "past the walk never ran")

    def test_party_search_counts_1_10_60_exactly_and_follows_the_gate(self):
        for n in (1, 10, 60):
            out = self.found(tmp.MID, participant=f"bulk{n}@vendor.example")
            self.assertEqual(len(out), n)
        page = self.search(tmp.MID, participant="bulk60@vendor.example", limit=5)
        self.assertEqual({h["direction"] for h in page["documents"]}, {"from_them"})
        self.assertTrue(page["completeness"]["truncated"])
        self.assertEqual(len(self.found(tmp.MID, participant="@vendor.example")), 71)
        for actor in READERS:
            for who in (tmp.SUPPLIER, tmp.UPN_B, tmp.LIST):
                with self.subTest(actor=sorted(actor.principals), who=who):
                    rows = self.sql(actor, "SELECT handle FROM emails WHERE addresses @> "
                                           f"ARRAY['{who.lower()}']")["rows"]
                    self.assertEqual(self.found(actor, participant=who),
                                     {_cell(r["cells"][0]) for r in rows})

    def test_unexpected_search_input_fails_closed(self):
        for args in ({"query": "agreed", "cursor": "not-a-cursor"},
                     # a cursor of before the rank (date|handle, no tier): refused
                     {"query": "agreed", "cursor": "c1:2026-11-01T00:00:00+00:00|h"},
                     {"query": "agreed", "cursor": "c1:7|2026-11-01T00:00:00+00:00|h"},
                     {"participant": "nobody"}, {"query": "!!!"},
                     {"query": "agreed", "since": "yesterday"}):
            with self.subTest(args=args):
                with self.assertRaises(ts.ToolError) as caught:
                    self.search(tmp.OWNER, **args)
                self.assertEqual(caught.exception.code, "invalid_input")

    def _attachment(self, pg, handle, *texts, markings="[]"):
        """A stored attachment of the mail version ``handle``: one text field per text
        (pieces of one version), as the capture stores a PDF's pages."""
        import zlib
        from operating.pg import words
        version = f"{handle}#attachment=zzatt{abs(hash(texts)) % 10 ** 8}&sha256=00"
        oid = f"Attachment:graph_mail.attachments:{version[-24:]}"
        for page, text in enumerate(texts, start=1):
            fid = pg.execute("SELECT coalesce(max(field_id), 0) + 1 FROM "
                             "ontology.text_fields").fetchone()[0]
            pg.execute("INSERT INTO ontology.text_fields VALUES (%s, %s, %s, %s, "
                       "'row.content_text', 'attachment', 'external', %s, 'x')",
                       (fid, oid, version, f"{version}&page={page}", markings))
            cid = pg.execute("SELECT coalesce(max(chunk_id), 0) + 1 FROM "
                             "ontology.text_chunks").fetchone()[0]
            body = zlib.compress(text.encode())
            pg.execute("INSERT INTO ontology.text_chunks VALUES (%s, %s, 0, %s)",
                       (cid, fid, body))
            for chunk, vec in words.chunk_word_rows([(cid, body)]):
                pg.execute("INSERT INTO ontology.chunk_words VALUES (%s, %s::tsvector)",
                           (chunk, vec))
        pg.execute(f"UPDATE {bv.SCHEMA}.emails SET attachments = greatest(attachments, 1) "
                   "WHERE handle = %s", (handle,))
        return oid

    def test_mail_holding_every_word_by_its_stem_is_found_through_the_same_view(self):
        """Few rows hold every word as typed: the rows holding every word by its English
        stem (small words dropped) are added, each saying so, through the reader's own
        guarded view (never a mail its word search could not show)."""
        import psycopg
        from operating.pg import words as wd
        owner, narrow = self.handles(tmp.OWNER), self.handles(tmp.NARROW)
        mine, both = sorted(owner - narrow)[0], sorted(owner & narrow)[0]
        subject = "Zzrenovation Zzideas"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            saved = pg.execute(f"SELECT handle, subject, words FROM {bv.SCHEMA}.emails "
                               "WHERE handle = ANY(%s)", ([mine, both],)).fetchall()

            def restore():
                with psycopg.connect(self.dsn, autocommit=True) as c:
                    for handle, subj, words in saved:
                        c.execute(f"UPDATE {bv.SCHEMA}.emails SET subject = %s, words = %s "
                                  "WHERE handle = %s", (subj, words, handle))
            self.addCleanup(restore)
            pg.execute(f"UPDATE {bv.SCHEMA}.emails SET subject = %s, words = %s::tsvector "
                       "WHERE handle = ANY(%s)", (subject, wd.tsvector_text(
                           wd.text_words([subject])[0]), [mine, both]))

        def got(actor, q):
            return {h["citation"]: h.get("match")
                    for h in self.search(actor, query=q, limit=50)["documents"]}
        for q in ("zzrenovating zzidea", "the zzrenovations"):
            hits = got(tmp.OWNER, q)
            self.assertEqual(hits.get(mine), "every word, by its stem", q)
            self.assertEqual(hits.get(both), "every word, by its stem", q)
            hits = got(tmp.NARROW, q)
            self.assertNotIn(mine, hits, q)
            self.assertEqual(hits.get(both), "every word, by its stem", q)
        held = got(tmp.OWNER, "zzrenovation zzideas")   # every word as typed
        self.assertIn(mine, held)
        self.assertIsNone(held[mine])
        # the stem pass runs beside the word pass (its own pooled connection), not after
        # it: it starts while the (slowed) word pass is still reading, and its rows still
        # come only through the reader's own view
        import threading
        import time as _time
        real = ts.SqlTool._comms_scoped
        spans, lock = [], threading.Lock()

        def timed(tool, conn, settings, work, actor):
            began = _time.monotonic()
            if getattr(work, "__name__", "") == "work":
                _time.sleep(0.4)   # the word pass
            try:
                return real(tool, conn, settings, work, actor)
            finally:
                with lock:
                    spans.append((getattr(work, "__name__", ""), began, _time.monotonic()))
        with mock.patch.object(ts.SqlTool, "_comms_scoped", timed):
            hits = got(tmp.NARROW, "zzrenovating zzidea")
        self.assertNotIn(mine, hits)
        self.assertEqual(hits.get(both), "every word, by its stem")
        words_span = next(s for s in spans if s[0] == "work")
        stem_span = next(s for s in spans if s[0] == "stemmed")
        self.assertLess(stem_span[1], words_span[2], spans)

    def test_a_full_readers_search_finds_mail_by_its_attachments_own_text(self):
        """Mail whose attachment's text (not its own) holds every query word: on a full
        reader's first page, after the word match, through the same guarded view (never
        a mail the reader cannot read), each attachment's whole text checked for
        Freightcom first (any field of its version naming it: left out), never a marked
        field's text, never an attachment the people exclusion holds, the dates and
        party still bound, never on a later page, the walk
        and the word index giving the same rows; not for a reader who is not full."""
        import psycopg
        from operating.pg import comms_search as cs
        owner, narrow = self.handles(tmp.OWNER), self.handles(tmp.NARROW)
        mine = sorted(h for h in owner - narrow
                      if h not in self.found(tmp.OWNER, person=True, query="Freightcom"))
        self.assertGreaterEqual(len(mine), 5)
        plain, freight, marked, late, gone = mine[:5]
        q = "zzwidget quarterly"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            saved = dict(pg.execute(f"SELECT handle, attachments FROM {bv.SCHEMA}.emails"))
            ids = [self._attachment(pg, plain, "the zzwidget quarterly report"),
                   self._attachment(pg, freight, "zzwidget quarterly",
                                    "shipped by Freightcom"),
                   self._attachment(pg, marked, "zzwidget quarterly", markings='["x"]'),
                   self._attachment(pg, late, "zzwidget only"),
                   self._attachment(pg, gone, "zzwidget quarterly")]
            # the attachment itself in the people exclusion's objects
            pg.execute("INSERT INTO ontology.people_excluded_objects VALUES (%s, %s, "
                       "'participant')", (gone, ids[-1]))

        def undo():
            with psycopg.connect(self.dsn, autocommit=True) as pg:
                pg.execute("DELETE FROM ontology.people_excluded_objects WHERE "
                           "object_id = ANY(%s)", (ids,))
                pg.execute("DELETE FROM ontology.chunk_words w USING ontology.text_chunks "
                           "c, ontology.text_fields fd WHERE w.chunk_id = c.chunk_id AND "
                           "c.field_id = fd.field_id AND fd.object_id = ANY(%s)", (ids,))
                pg.execute("DELETE FROM ontology.text_chunks c USING ontology.text_fields "
                           "fd WHERE c.field_id = fd.field_id AND fd.object_id = ANY(%s)",
                           (ids,))
                pg.execute("DELETE FROM ontology.text_fields WHERE object_id = ANY(%s)",
                           (ids,))
                for h, n in saved.items():
                    pg.execute(f"UPDATE {bv.SCHEMA}.emails SET attachments = %s WHERE "
                               "handle = %s", (n, h))
        self.addCleanup(undo)
        full = mock.patch.object(people_excluded, "full_reader", lambda base, a: "tag")
        with full:
            for walk in (1, cs.ATTACH_WALK):
                with self.subTest(walk=walk), \
                        mock.patch.object(cs, "walk_rows", lambda pg, q, walk=walk: walk):
                    out = self.search(tmp.OWNER, query=q)
                    docs = [h for h in out["documents"] if "attachment" in h.get("match", "")]
                    self.assertEqual([h["citation"] for h in docs], [plain])
                    self.assertIn("zzwidget", docs[0]["snippet"]["untrusted_text"])
                    self.assertEqual(docs[0]["kind"], "email")
                    self.assertIn("attachment", out["searched"]["widened"])
            # on the first page only (``found`` fails a hit doubled across pages)
            self.assertIn(plain, self.found(tmp.OWNER, query=q))
            self.assertFalse(self.search(tmp.OWNER, query=q, since="2100-01-01")["documents"])
            # a participant's search: the attachment pass runs bound to that party
            with psycopg.connect(self.dsn, autocommit=True) as pg:
                (addresses,) = pg.execute(f"SELECT addresses FROM {bv.SCHEMA}.emails WHERE "
                                          "handle = %s", (plain,)).fetchone()
            bound = self.search(tmp.OWNER, query=q, participant=addresses[0])
            self.assertIn(plain, [h["citation"] for h in bound["documents"]
                                  if "attachment" in h.get("match", "")])
            self.assertNotIn(plain, {h["citation"] for h in self.search(
                tmp.OWNER, query=q, participant="nobody@nowhere.example")["documents"]})
            self.assertNotIn(plain, {h["citation"] for h in self.search(
                tmp.NARROW, query=q)["documents"]})
            # the person's own search: the same rule (Freightcom attachments left out too)
            got = {h["citation"] for h in self.search(tmp.OWNER, person=True, query=q)
                   ["documents"]}
            self.assertIn(plain, got)
            self.assertFalse({freight, marked, late, gone} & got)
        # not a full reader: no attachment's text read
        with mock.patch.object(people_excluded, "full_reader", lambda base, a: ""):
            self.assertNotIn(plain, {h["citation"] for h in self.search(
                tmp.OWNER, query=q)["documents"]})

    def test_a_full_readers_search_and_open_run_with_the_longer_limit_same_rows(self):
        # a full reader's mail/Teams statements get SEARCH_FULL_MS (a common word over
        # millions of mails ran past 10 s), anyone else's STATEMENT_MS; the rows are the
        # door's either way (a NameError here once failed every search and open)
        hit = sorted(self.found(tmp.OWNER, query="agreed"))[0]
        seen = {}
        for full in (False, True):
            spy = mock.patch.object(ts, "scoped_read", wraps=ts.scoped_read)
            as_full = mock.patch.object(people_excluded, "full_reader",
                                        lambda base, a, full=full: "tag" if full else "")
            with spy as read, as_full:
                found = self.found(tmp.OWNER, query="agreed")   # every page
                with mock.patch.object(ts, "_people_tag", return_value=self.tag):
                    opened = self.tool().comms_open({"citation": hit}, tmp.OWNER)
            want = (ts.SEARCH_FULL_MS if full else ts.STATEMENT_MS) / 1000.0
            self.assertGreaterEqual(len(read.call_args_list), 2)
            self.assertEqual({c.args[3] for c in read.call_args_list}, {want})
            seen[full] = (found, opened["text"]["untrusted_text"])
        self.assertEqual(seen[False], seen[True])
        self.assertIn(hit, seen[True][0])

    def test_open_reads_one_item_through_the_same_door(self):
        tool = self.tool()
        mine, narrow = self.handles(tmp.OWNER), self.handles(tmp.NARROW)
        hit = sorted(self.found(tmp.OWNER, query="agreed"))[0]
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            out = tool.comms_open({"citation": hit}, tmp.OWNER)
            self.assertIn("agreed", out["text"]["untrusted_text"])
            self.assertEqual(out["citation"], hit)
            # out of scope: the same answer as a handle that is no mail at all
            denied = sorted(mine - narrow)[0]
            self.assertEqual(tool.comms_open({"citation": denied}, tmp.NARROW),
                             {"not_comms": True})
            fc = [h for o, h in self.emails.items() if "Freightcom" in
                  str(self.subjects({o}))]
            with self.assertRaises(ts.ToolError) as caught:
                tool.comms_open({"citation": fc[0]}, tmp.OWNER)
            self.assertEqual(caught.exception.code, "egress_withheld")
            # the narrow reader: withheld where the item gate admits them, else no comms row
            gate = {self.emails[o] for o in self.gated(self.runtime(), tmp.NARROW)}
            for h in fc:
                if h in gate:
                    with self.assertRaises(ts.ToolError) as caught:
                        tool.comms_open({"citation": h}, tmp.NARROW)
                    self.assertEqual(caught.exception.code, "egress_withheld")
                else:
                    self.assertEqual(tool.comms_open({"citation": h}, tmp.NARROW),
                                     {"not_comms": True})
            self.assertEqual(tool.comms_open({"citation": "sharepoint://site/x.pdf"},
                                             tmp.OWNER), {"not_comms": True})
        # with the tag undecided nothing is opened or found
        with mock.patch.object(ts, "_people_tag", return_value=""):
            self.assertEqual(tool.comms_open({"citation": hit}, tmp.OWNER),
                             {"not_comms": True})
            self.assertEqual(tool.comms_search({"query": "agreed"}, tmp.OWNER)["documents"],
                             [])

    def test_a_replay_ceiling_shows_nothing_from_after_its_moment(self):
        """A replay of a past moment (``read_ceiling``): plx.sql, the search (every page,
        any ``until``) and open see exactly the mail before it, the same rows the
        unbounded view dates before it; a qualified view name, an unreadable ceiling or a
        describe's sample rows never get past it."""
        from operating import read_ceiling
        allrows = self.sql(tmp.OWNER, "SELECT handle, coalesce(received_at, sent_at), "
                                      "subject FROM emails")["rows"]
        dated = {_cell(r["cells"][0]): (_cell(r["cells"][1]), _cell(r["cells"][2]))
                 for r in allrows}
        out_h = next(h for h, (_, subj) in dated.items() if subj == "RE: Quote please")
        in_h = next(h for h, (_, subj) in dated.items() if subj == "Quote please")
        moment = "2026-09-22T10:00:00Z"
        cut = read_ceiling.ceiling({read_ceiling.CEILING_ENV: moment})
        before = {h for h, (at, _) in dated.items()
                  if at is not None and dt.datetime.fromisoformat(str(at)) < cut}
        self.assertIn(in_h, before)
        self.assertNotIn(out_h, before)
        self.assertTrue(before < set(dated))
        with mock.patch.dict(os.environ, {read_ceiling.CEILING_ENV: moment}):
            for actor in READERS:
                with self.subTest(actor=sorted(actor.principals)):
                    mine = self.handles(actor)
                    self.assertTrue(mine <= before)
                    # the result is as of the moment, never the tables' build time
                    self.assertEqual(self.sql(actor, "SELECT 1 FROM emails LIMIT 1")["as_of"],
                                     cut.isoformat())
                    # a subquery and the reader's own common table read the bounded rows
                    nested = self.sql(actor, "WITH e AS (SELECT handle FROM emails) SELECT "
                                             "handle FROM e WHERE handle IN (SELECT handle "
                                             "FROM emails)")["rows"]
                    self.assertEqual({_cell(r["cells"][0]) for r in nested}, mine)
            owner = self.handles(tmp.OWNER)
            self.assertEqual(owner, before)
            # every page of a search, an until past the moment clamped to it
            for args in ({"query": "quote"}, {"participant": tmp.SUPPLIER},
                         {"query": "quote", "until": "2026-12-31"}):
                with self.subTest(args=args):
                    self.assertTrue(self.found(tmp.OWNER, **args) <= before)
            self.assertIn(in_h, self.found(tmp.OWNER, query="quote"))
            with mock.patch.object(ts, "_people_tag", return_value=self.tag):
                tool = self.tool()
                self.assertEqual(tool.comms_open({"citation": in_h}, tmp.OWNER)["citation"],
                                 in_h)
                with self.assertRaises(ts.ToolError) as caught:
                    tool.comms_open({"citation": out_h}, tmp.OWNER)
                self.assertEqual(caught.exception.code, "not_found")
                # a message before the moment outside the narrow reader's views: not
                # found, never handed to the old read
                hidden = before - self.handles(tmp.NARROW)
                self.assertTrue(hidden)
                for h in sorted(hidden)[:3]:
                    with self.assertRaises(ts.ToolError) as caught:
                        tool.comms_open({"citation": h}, tmp.NARROW)
                    self.assertEqual(caught.exception.code, "not_found")
                # no mail or Teams item at all (a PO, an order): the old read, bounded by
                # its own time (``_open_single``), never "no such message"
                for h in ("sharepoint://site/x.pdf",
                          'api://public.app_purchase_order/{"id":"po1"}@x-1'):
                    self.assertEqual(tool.comms_open({"citation": h}, tmp.OWNER),
                                     {"not_comms": True})
                described = tool.call("plx.sql", {"describe": "emails"}, tmp.OWNER)
                self.assertNotIn("sample", described)
                self.assertIn("emails", described["views"])
                qualified = tool.call("plx.sql", {"sql": "SELECT handle FROM "
                                      f"{sql_views.guarded_schema()}.emails"}, tmp.OWNER)
                self.assertEqual(qualified["error"]["code"], "invalid_sql")
        for bad in ("yesterday", "2026-09-22T10:00:00"):
            with mock.patch.dict(os.environ, {read_ceiling.CEILING_ENV: bad}), \
                    mock.patch.object(ts, "_people_tag", return_value=self.tag):
                tool = self.tool()
                with self.subTest(ceiling=bad):
                    self.assertEqual(tool.call("plx.sql", {"sql": "SELECT handle FROM emails"},
                                               tmp.OWNER)["error"]["code"], "not_ready")
                    for call in (lambda: tool.comms_search({"query": "quote"}, tmp.OWNER),
                                 lambda: tool.comms_open({"citation": in_h}, tmp.OWNER)):
                        with self.assertRaises(ts.ToolError) as caught:
                            call()
                        self.assertEqual(caught.exception.code, "not_ready")
        # no ceiling: the later reply reads again
        self.assertIn(out_h, self.handles(tmp.OWNER))

    # ---------------------------------------------------------------- production roles

    def api_dsn(self):
        """The fixture database as the API's production login ``plx_api`` (NOINHERIT,
        deploy/pg/plx_api_role.sql), its grants made the production way: the role file,
        migration 0041 and ``sql_views install``. Every statement of a connection on this
        DSN runs as ``plx_api`` with exactly its own grants (never the fixture's
        superuser's)."""
        import time
        import psycopg
        from psycopg.conninfo import make_conninfo
        from operating.pg.migrate import MIGRATIONS_DIR
        if getattr(type(self), "_api_dsn", None):
            return type(self)._api_dsn
        role_file = (MIGRATIONS_DIR.parents[2] / "deploy" / "pg" / "plx_api_role.sql")
        for attempt in range(5):   # the roles are the cluster's: parallel modules may race
            try:
                with psycopg.connect(self.dsn, autocommit=True) as pg:
                    # the schemas the role file grants on (the served database has them)
                    # (the two logs the API writes, as their own tables: their shape is not
                    # what is tested here)
                    pg.execute("CREATE SCHEMA IF NOT EXISTS search; CREATE SCHEMA IF NOT "
                               "EXISTS plx_meta; CREATE TABLE IF NOT EXISTS search.query_log "
                               "(id bigserial PRIMARY KEY); CREATE TABLE IF NOT EXISTS "
                               "search.feedback (id bigserial PRIMARY KEY)")
                    pg.execute(role_file.read_text(encoding="utf-8"))
                    pg.execute((MIGRATIONS_DIR / "0041_business_views.sql"
                                ).read_text(encoding="utf-8"))
                sql_views.install(self.dsn)
                break
            except psycopg.errors.InternalError:
                time.sleep(0.5 * (attempt + 1))
        type(self)._api_dsn = make_conninfo(self.dsn, options="-c role=plx_api")
        with psycopg.connect(type(self)._api_dsn) as pg:
            assert pg.execute("SELECT current_user").fetchone()[0] == "plx_api"
        return type(self)._api_dsn

    def api_tool(self):
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.api_dsn(), link_guard=True)
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.sqlite))
        base = ot.OntologyTools(reader, self.policy, item_gate=lambda a, h: True,
                                schema=ot.SchemaView.from_compiled(self.compiled),
                                grammar=None, contracts=ev.CONTRACTS, clock=lambda: ev.NOW)
        return ts.SqlTool(base)

    def paged(self, fn, actor, **args):
        """Every hit of ``fn`` (a search of ``api_tool``), page by page, none doubled."""
        got, cursor = [], None
        for _ in range(20):
            out = fn({**args, "limit": 50, **({"cursor": cursor} if cursor else {})}, actor)
            got += [h["citation"] for h in out["documents"]]
            cursor = out["completeness"]["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(got), len(set(got)), "a hit doubled across pages")
        return set(got)

    def test_search_and_open_as_the_production_api_role_are_the_item_gates(self):
        texts = self.texts()
        runtime = self.runtime()
        tool = self.api_tool()
        fc_all = {h for o, h in self.emails.items() if "Freightcom" in str(self.subjects({o}))}
        everyone = set(self.emails.values())
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            for actor in READERS:   # full (owner), staff (two mailboxes), narrow (one)
                with self.subTest(actor=sorted(actor.principals)):
                    gate = {self.emails[o] for o in self.gated(runtime, actor)} - fc_all
                    want = {h for h in gate if "agreed" in texts.get(h, "")}
                    self.assertTrue(want)
                    self.assertEqual(self.paged(tool.comms_search, actor, query="agreed"),
                                     want)
                    for hit in sorted(want):
                        opened = tool.comms_open({"citation": hit}, actor)
                        self.assertEqual(opened["citation"], hit)
                        self.assertIn("agreed", opened["text"]["untrusted_text"])
                    # outside the gate: the answer of a handle that is no mail at all
                    for other in sorted(everyone - gate - fc_all)[:5]:
                        self.assertEqual(tool.comms_open({"citation": other}, actor),
                                         {"not_comms": True})
                    self.assertEqual(tool.comms_open({"citation": "sharepoint://s/x.pdf"},
                                                     actor), {"not_comms": True})
                    # a person's own pages read through the person views as the same role
                    self.assertEqual(self.paged(tool.person_search, actor, query="agreed"),
                                     self.found(actor, person=True, query="agreed"))

    def test_the_sql_reader_role_reads_neither_the_person_views_nor_the_text_index(self):
        import psycopg
        self.api_dsn()
        person, guarded = sql_views.person_schema(), sql_views.guarded_schema()
        with psycopg.connect(self.dsn) as pg:
            for role, what, ok in (
                    (sql_views.ROLE, f"{person}.emails", False),
                    (sql_views.ROLE, "ontology.text_fields", False),
                    (sql_views.ROLE, "ontology.text_chunks", False),
                    (sql_views.ROLE, "ontology.chunk_words", False),
                    ("plx_api", f"{guarded}.emails", True),
                    ("plx_api", f"{person}.emails", True)):
                with self.subTest(role=role, what=what):
                    try:
                        with pg.transaction():
                            pg.execute(f"SET LOCAL ROLE {role}")
                            pg.execute(f"SELECT 1 FROM {what} LIMIT 0")
                        read = True
                    except psycopg.errors.InsufficientPrivilege:
                        read = False
                    self.assertEqual(read, ok)
            # plx_api's SELECT there is on the communications views only
            self.assertFalse(pg.execute(
                "SELECT bool_or(has_table_privilege('plx_api', c.oid, 'SELECT')) FROM "
                "pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = "
                "%s AND c.relname NOT IN ('emails', 'teams_messages')", (guarded,)
            ).fetchone()[0])

    def test_a_missing_grant_falls_back_to_the_old_path_and_never_fails(self):
        import psycopg
        from operating import ontology_tools_search as s
        tool = self.api_tool()
        guarded = sql_views.guarded_schema()
        hit = sorted(self.found(tmp.OWNER, query="agreed"))[0]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute(f"REVOKE SELECT ON {guarded}.emails FROM plx_api")
        self.addCleanup(sql_views.install, self.dsn)
        reads, docs = [], []

        def read(arguments):
            reads.append(arguments)
            return {"status": "resolved", "citation": arguments["citation"],
                    "text": {"untrusted_text": "old path"}}

        def documents(arguments):
            docs.append(arguments)
            return {"documents": [], "searched": {"sources": ["graph_mail"]}, "gaps": [],
                    "completeness": {"complete": True, "truncated": False, "returned": 0}}
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            with self.assertRaises(ts.ToolError) as caught:
                tool.comms_search({"query": "agreed"}, tmp.OWNER)
            self.assertEqual(caught.exception.code, "not_ready")
            out = s.run(documents, {"query": "agreed", "sources": ["graph_mail"]}, read,
                        lambda a: tool.guarded(tool.comms_search, a, tmp.OWNER))
            self.assertNotIn("error", out, out)
            self.assertEqual(len(docs), 1)
            got = s.open_one(read, {"citation": hit},
                             lambda a: tool.guarded(tool.comms_open, a, tmp.OWNER))
            self.assertEqual(got["text"]["untrusted_text"], "old path")
            self.assertEqual(len(reads), 1)

    def test_freightcom_is_kept_from_the_ai_path_only_a_person_still_reads_it(self):
        import psycopg
        runtime = self.runtime()
        fc_all = {h for o, h in self.emails.items() if "Freightcom" in str(self.subjects({o}))}
        tool = self.tool()
        with psycopg.connect(self.dsn) as pg:
            not_ai = {r[0] for r in pg.execute(
                f"SELECT handle FROM {bv.SCHEMA}.emails WHERE ai_ok IS NOT TRUE")}
        self.assertTrue(fc_all <= not_ai)
        for actor in READERS:
            with self.subTest(actor=sorted(actor.principals)):
                gate = {self.emails[o] for o in self.gated(runtime, actor)}
                fc = fc_all & gate
                self.assertTrue(fc)
                with mock.patch.object(ts, "_people_tag", return_value=self.tag):
                    person = self.found(actor, person=True, query="pickup")
                    ai = {h["citation"] for h in tool.comms_search(
                        {"query": "pickup", "limit": 50}, actor)["documents"]}
                    # the person's own search: exactly the Freightcom mail they may read
                    self.assertEqual(person, fc)
                    self.assertEqual(ai, set())
                    # every other word: the person sees the AI's rows, and beyond them only
                    # rows the AI is kept from (ai_ok not true), never one outside the gate
                    p_agreed = self.found(actor, person=True, query="agreed")
                    ai_agreed = self.found(actor, query="agreed")
                    self.assertTrue(p_agreed <= gate)
                    self.assertTrue(ai_agreed <= p_agreed)
                    self.assertTrue(p_agreed - ai_agreed <= not_ai, p_agreed - ai_agreed)
                    hit = sorted(fc)[0]
                    self.assertEqual(tool.person_open({"citation": hit}, actor)["citation"],
                                     hit)
                    with self.assertRaises(ts.ToolError):
                        tool.comms_open({"citation": hit}, actor)
        # a person outside the gate reads none of it on the person path either
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            denied = sorted(self.handles(tmp.OWNER) - self.handles(tmp.NARROW))[0]
            self.assertEqual(tool.person_open({"citation": denied}, tmp.NARROW),
                             {"not_comms": True})
            with mock.patch.object(ts, "_people_tag", return_value=""):
                self.assertEqual(tool.person_search({"query": "pickup"}, tmp.OWNER)
                                 ["documents"], [])
        # the AI's role holds no grant on the person views, and plx.sql cannot name them
        person = sql_views.person_schema()
        with psycopg.connect(self.dsn) as pg:
            self.assertFalse(pg.execute(
                "SELECT has_schema_privilege(%s, %s, 'USAGE') OR has_table_privilege(%s, %s, "
                "'SELECT')", (sql_views.ROLE, person, sql_views.ROLE, f"{person}.emails")
            ).fetchone()[0])
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            out = self.tool().call("plx.sql", {"sql": f"SELECT handle FROM {person}.emails"},
                                   tmp.OWNER)
        self.assertIn("error", out)
        # and no registered tool reaches them: the AI's tools are plx.sql and the AI views
        table: dict = {}
        ts.register(table, self.tool().base)
        self.assertEqual(sorted(table), sorted([ts.TOOL_NAME, ts.COMMS_SEARCH, ts.COMMS_OPEN,
                                                ts.RECORDS_SEARCH]))

    def test_an_opened_item_is_kept_as_read_text_the_figure_check_binds(self):
        """plx.open of a mail item keeps its text as a plx.read's is kept (whole, or the
        spans served of it, per field as the text index names it): the answer's figure
        check (``answer_gate.bind_read_figures`` over ``read_texts``) binds to it."""
        import psycopg
        from operating import mcp_server
        from operating.mcp_server_calls import CallsMixin

        class Session(CallsMixin):
            pass

        hit = sorted(self.found(tmp.OWNER, query="agreed"))[0]
        with psycopg.connect(self.dsn) as pg:
            (field,) = pg.execute("SELECT DISTINCT field_path FROM ontology.text_fields "
                                  "WHERE citation = %s", (hit,)).fetchone()
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            whole = self.tool().comms_open({"citation": hit}, tmp.OWNER)
            part = self.tool().comms_open({"citation": hit, "text_offset": 2,
                                           "max_chars": 5}, tmp.OWNER)
        for result, spans in ((whole, None), (part, [(2, 7)])):
            with self.subTest(spans=spans):
                session = Session.__new__(Session)
                session._evidence = mcp_server._session_evidence()
                session._note_texts(json.loads(json.dumps(result)))
                self.assertEqual(session.read_texts(), [([hit], field, spans)])
        # what the field says is the characters served, never more
        self.assertEqual(part["fields_served"][0]["chars"], 5)
        session = Session.__new__(Session)
        session._evidence = mcp_server._session_evidence()
        forged = {**part, "fields_served": [{**part["fields_served"][0], "chars": 50}]}
        session._note_texts(forged)
        self.assertEqual(session.read_texts(), [], "a span past the served text binds nothing")

    def test_a_near_window_starts_at_the_words_and_is_kept_as_read_text(self):
        """plx.open near words (and several citations): each mail's window starts just
        before the first of the words its text holds, its served span is the window's own
        (the figure check binds exactly those characters), a text holding none of them is
        shown from its start (near_found false), and the items of a several-citation open
        are kept as each one's open is."""
        from operating import mcp_server
        from operating.mcp_server_calls import CallsMixin

        class Session(CallsMixin):
            pass

        hit = sorted(self.found(tmp.OWNER, query="agreed"))[0]
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            whole = self.tool().comms_open({"citation": hit}, tmp.OWNER)
            text = whole["text"]["untrusted_text"]
            at = text.lower().find("agreed")
            self.assertGreater(at, 0)
            near = self.tool().comms_open({"citation": hit, "max_chars": 8,
                                           "near": ["Agreed"]}, tmp.OWNER)
            none = self.tool().comms_open({"citation": hit, "max_chars": 8,
                                           "near": ["zzzunheard"]}, tmp.OWNER)
            for bad in (["x" * 101], "agreed", [3], ["w"] * 17):
                with self.assertRaises(ts.ToolError):
                    self.tool().comms_open({"citation": hit, "near": bad}, tmp.OWNER)
        window = near["text"]["untrusted_text"]
        self.assertTrue(near["near_found"])
        self.assertEqual(window, text[near["text_offset"]:near["text_offset"] + 8])
        self.assertLessEqual(near["text_offset"], at)
        self.assertGreater(near["text_offset"] + 8, at)
        self.assertEqual((none["near_found"], none["text_offset"]), (False, 0))
        session = Session.__new__(Session)
        session._evidence = mcp_server._session_evidence()
        session._note_texts(json.loads(json.dumps({"opened": [near], "window_chars": 8})))
        (cited, _field, spans), = session.read_texts()
        self.assertEqual((cited, spans), ([hit], [(near["text_offset"],
                                                    near["text_offset"] + 8)]))

    def test_a_body_indexed_late_is_withheld_until_read_then_shown(self):
        import psycopg
        table = f"{bv.SCHEMA}.emails"
        hit = sorted(self.found(tmp.OWNER, query="agreed"))[0]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            fields = pg.execute("SELECT * FROM ontology.text_fields WHERE citation = %s",
                                (hit,)).fetchall()
            chunks = pg.execute("SELECT c.* FROM ontology.text_chunks c JOIN "
                                "ontology.text_fields f ON f.field_id = c.field_id WHERE "
                                "f.citation = %s", (hit,)).fetchall()
            self.assertTrue(fields and chunks)
            pg.execute("DELETE FROM ontology.text_chunks c USING ontology.text_fields f WHERE "
                       "f.field_id = c.field_id AND f.citation = %s", (hit,))
            pg.execute("DELETE FROM ontology.text_fields WHERE citation = %s", (hit,))
            got = bc.refresh_changed(pg, "emails", {"ver": 0, "link": 0,
                                                    "field": self.marks(pg)["field"]})
            self.assertIsNone(pg.execute(f"SELECT ai_ok FROM {table} WHERE handle = %s",
                                         (hit,)).fetchone()[0])
            self.assertNotIn(hit, self.handles(tmp.OWNER))
            self.assertEqual(bc.recheck(pg, "emails")["undecided"],
                             bc.recheck(pg, "emails")["undecided_before"])
            # the body arrives (new field ids): the next refresh reads it by the text mark
            top = pg.execute("SELECT coalesce(max(field_id), 0) FROM ontology.text_fields"
                             ).fetchone()[0]
            ids = {f[0]: top + 1 + i for i, f in enumerate(fields)}
            for f in fields:
                pg.execute("INSERT INTO ontology.text_fields VALUES (" + ", ".join(
                    ["%s"] * len(f)) + ")", (ids[f[0]], *f[1:]))
            for c in chunks:
                pg.execute("INSERT INTO ontology.text_chunks VALUES (" + ", ".join(
                    ["%s"] * len(c)) + ")", (c[0], ids[c[1]], *c[2:]))
            got = bc.refresh_changed(pg, "emails", got["marks"])
            self.assertGreaterEqual(got["read"], 1)
            self.assertTrue(pg.execute(f"SELECT ai_ok FROM {table} WHERE handle = %s",
                                       (hit,)).fetchone()[0])
        self.assertIn(hit, self.handles(tmp.OWNER))
        self.assertIn(hit, self.found(tmp.OWNER, query="agreed"))

    def test_a_message_the_source_says_has_no_body_is_decided_by_its_cells(self):
        """A meeting reply or invitation with no body (Graph's preview empty, no text to
        index) is decided by its cells: shown to the AI (refresh and the nightly recheck),
        never left undecided for good; one whose preview has no value, or has words but no
        text indexed yet, stays withheld."""
        import psycopg
        table = f"{bv.SCHEMA}.emails"
        hit = sorted(self.found(tmp.OWNER, query="agreed"))[0]
        cell = ("FROM ontology.value_sources s, ontology.object_versions v, "
                "ontology.value_fields f WHERE s.src = c.src AND v.ver = s.ver AND "
                "f.field = c.field AND f.property = 'body_preview' AND s.superseded_at IS NULL "
                "AND v.object_id = %s")
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            oid = pg.execute(f"SELECT object_ids[1] FROM {table} WHERE handle = %s",
                             (hit,)).fetchone()[0]
            preview = pg.execute("SELECT c.src, c.field, c.value "
                                 + cell.replace("FROM ", "FROM ontology.value_cells c, ", 1),
                                 (oid,)).fetchall()
            self.assertEqual(len(preview), 1, preview)
            self.assertRegex(preview[0][2] or "", r"\S")
            fields = pg.execute("SELECT * FROM ontology.text_fields WHERE citation = %s",
                                (hit,)).fetchall()
            chunks = pg.execute("SELECT c.* FROM ontology.text_chunks c JOIN "
                                "ontology.text_fields f ON f.field_id = c.field_id WHERE "
                                "f.citation = %s", (hit,)).fetchall()
            self.assertTrue(fields and chunks)

            def set_preview(value):
                pg.execute(f"UPDATE ontology.value_cells c SET value = %s {cell}", (value, oid))

            def refreshed():
                bc.refresh_changed(pg, "emails", {"ver": 0, "link": 0,
                                                  "field": self.marks(pg)["field"]})
                return pg.execute(f"SELECT ai_ok FROM {table} WHERE handle = %s",
                                  (hit,)).fetchone()[0]
            pg.execute("DELETE FROM ontology.text_chunks c USING ontology.text_fields f WHERE "
                       "f.field_id = c.field_id AND f.citation = %s", (hit,))
            pg.execute("DELETE FROM ontology.text_fields WHERE citation = %s", (hit,))
            try:
                self.assertIsNone(refreshed())   # words in the preview, no text yet
                set_preview(" \r\n\t")
                self.assertIs(refreshed(), True)  # the source says: no body
                self.assertIn(hit, self.handles(tmp.OWNER))
                pg.execute(f"UPDATE {table} SET ai_ok = NULL WHERE handle = %s", (hit,))
                bc.recheck(pg, "emails")   # the nightly re-flag decides it the same way
                self.assertIs(pg.execute(f"SELECT ai_ok FROM {table} WHERE handle = %s",
                                         (hit,)).fetchone()[0], True)
                set_preview(None)   # no value: nothing says the body is empty
                pg.execute(f"UPDATE {table} SET ai_ok = NULL WHERE handle = %s", (hit,))
                self.assertIsNone(refreshed())
                self.assertNotIn(hit, self.handles(tmp.OWNER))
            finally:
                set_preview(preview[0][2])
                for f in fields:
                    pg.execute("INSERT INTO ontology.text_fields VALUES (" + ", ".join(
                        ["%s"] * len(f)) + ")", f)
                for c in chunks:
                    pg.execute("INSERT INTO ontology.text_chunks VALUES (" + ", ".join(
                        ["%s"] * len(c)) + ")", c)
                self.assertIs(refreshed(), True)

    def test_the_text_check_pages_its_handles_and_decides_the_same(self):
        """The Freightcom text check holds a page of handles at a time (11-18: the served
        mail read through one process took the build past its 8 GB): pages of 2, in this
        process or over a pool whose workers read the text over their own connections (this
        process then never reads a body), decide every handle as one pass does, none lost or
        doubled; a handle whose text is not indexed is undecided (NULL), never cleared."""
        import psycopg
        table = f"{bv.SCHEMA}.emails"

        class Counting:
            def __init__(self, pg):
                self._pg, self.pages, self.texts = pg, 0, 0

            def __getattr__(self, name):
                return getattr(self._pg, name)

            def execute(self, sql, params=None):
                self.pages += sql.startswith("SELECT handle, array_agg")
                self.texts += sql == bc.TEXT_SQL
                return self._pg.execute(sql, params)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            bare = pg.execute(f"SELECT handle FROM {table} WHERE ai_ok IS NOT FALSE "
                              "ORDER BY handle LIMIT 1").fetchone()[0]
            fields = pg.execute("SELECT * FROM ontology.text_fields WHERE version_handle = %s",
                                (bare,)).fetchall()
            chunks = pg.execute("SELECT c.* FROM ontology.text_chunks c JOIN "
                                "ontology.text_fields f ON f.field_id = c.field_id WHERE "
                                "f.version_handle = %s", (bare,)).fetchall()
            self.assertTrue(fields and chunks)
            # committed: the pool's workers read over their own connections
            pg.execute("DELETE FROM ontology.text_chunks c USING ontology.text_fields f WHERE "
                       "f.field_id = c.field_id AND f.version_handle = %s", (bare,))
            pg.execute("DELETE FROM ontology.text_fields WHERE version_handle = %s", (bare,))
            try:
                want = pg.execute(f"SELECT count(DISTINCT handle) FROM {table} WHERE ai_ok IS "
                                  "NOT FALSE").fetchone()[0]

                def verdicts():
                    rows = pg.execute("SELECT handle, ok FROM cm_ai").fetchall()
                    self.assertEqual(len(rows), want, "every handle once")
                    return dict(rows)
                bc._ai_verdicts(pg, table, "true")
                one = verdicts()
                self.assertIsNone(one[bare], "no text indexed: undecided")
                self.assertIn(True, one.values())
                for pool_min in (10 ** 9, 0):
                    probe = Counting(pg)
                    with self.subTest(pool=pool_min == 0), \
                            mock.patch.multiple(bc, TEXT_BATCH=2, FLAG_POOL_MIN=pool_min,
                                                FLAG_WORKERS=2):
                        bc._ai_verdicts(probe, table, "true")
                        self.assertEqual(verdicts(), one)
                        self.assertGreaterEqual(probe.pages, (want + 1) // 2, "paged by 2")
                        if pool_min == 0:
                            self.assertEqual(probe.texts, 0, "the workers read the text")
                        else:
                            self.assertGreater(probe.texts, 0)
            finally:
                for f in fields:
                    pg.execute("INSERT INTO ontology.text_fields VALUES (" + ", ".join(
                        ["%s"] * len(f)) + ")", f)
                for c in chunks:
                    pg.execute("INSERT INTO ontology.text_chunks VALUES (" + ", ".join(
                        ["%s"] * len(c)) + ")", c)

    def test_the_nightly_recheck_decides_a_text_written_under_an_older_field(self):
        """``python3 -m operating.pg.business_comms recheck`` (the nightly unit): a row
        left undecided whose text sits under a field the refresh's text mark has passed is
        decided by the recheck; never beside a running refresh (its lock)."""
        import contextlib
        import io
        import psycopg
        table = f"{bv.SCHEMA}.emails"
        hit = sorted(self.found(tmp.OWNER, query="agreed"))[0]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute(f"UPDATE {table} SET ai_ok = NULL WHERE handle = %s", (hit,))
            self.assertNotIn(hit, self.handles(tmp.OWNER))
            with psycopg.connect(self.dsn, autocommit=True) as other:
                other.execute("SELECT pg_advisory_lock(%s)", (bv.REFRESH_LOCK,))
                self.assertEqual(bc.recheck_all(self.dsn),
                                 [{"skipped": "a business views refresh is running"}])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(bc.main(["recheck", "--dsn", self.dsn]), 0)
            got = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual({g["view"] for g in got}, set(bc.SPECS))
            # tables this fixture never built are skipped and say so
            self.assertTrue({"meetings", "meeting_transcripts"} <= {
                g["view"] for g in got if g.get("skipped") == "not built"}, got)
            mail = next(g for g in got if g["view"] == "emails")
            self.assertLess(mail["undecided"], mail["undecided_before"])
            self.assertTrue(pg.execute(f"SELECT ai_ok FROM {table} WHERE handle = %s",
                                       (hit,)).fetchone()[0])
        self.assertIn(hit, self.handles(tmp.OWNER))

    def test_plx_own_documents_are_kept_from_every_reader_by_the_table_not_the_setting(self):
        """11-22: PLX's own sales documents held as supplier documents were sent as a 10 MB
        list in ``plx.reader_hidden`` with every comms statement; the views now read
        ``ontology.own_issued_documents`` by its key. A mail whose object is in it is read by
        no reader, with the setting carrying none of the ids; deleting the row brings it back."""
        import psycopg
        before = self.handles(tmp.OWNER)
        victim = sorted(before)[0]
        oid = {h: o for o, h in self.emails.items()}[victim]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("INSERT INTO ontology.own_issued_documents (object_id, object_type, "
                       "content_sha256, evidence, detected_at) VALUES (%s, 'SupplierInvoice', "
                       "%s, 'test', 'now')", (oid, "0" * 64))
        try:
            for actor in READERS:
                with self.subTest(actor=sorted(actor.principals)):
                    self.assertNotIn(victim, self.handles(actor))
            self.assertEqual(self.handles(tmp.OWNER), before - {victim})
        finally:
            with psycopg.connect(self.dsn, autocommit=True) as pg:
                pg.execute("DELETE FROM ontology.own_issued_documents WHERE object_id = %s",
                           (oid,))
        self.assertEqual(self.handles(tmp.OWNER), before)

    def test_the_own_issued_ids_travel_in_the_setting_until_every_view_reads_the_table(self):
        """11-22 review (deploy order): the views are installed by sql_views.install, not by
        the deploy. Until every comms view carries the table check the code keeps sending
        PLX's own document ids in plx.reader_hidden, so old views stay safe; once all carry
        it they are not sent."""
        import psycopg
        from operating.search import document_marks
        with psycopg.connect(self.dsn) as pg:
            self.assertTrue(bc.views_check_own_issued(pg))   # this fixture's views are new
        # an installed view without the check (the previous release's): not carried
        class Old:
            def __init__(self, pg):
                self.pg = pg

            def execute(self, sql, params=None):
                cur = self.pg.execute(sql, params)
                if "pg_get_viewdef" in sql:
                    class Row:
                        def fetchone(_):
                            return ("SELECT 1 FROM biz.emails",)
                    return Row()
                return cur
        with psycopg.connect(self.dsn) as pg:
            self.assertFalse(bc.views_check_own_issued(Old(pg)))
        tool = self.tool()
        scope = tool._reader(tmp.OWNER)[1]
        own = frozenset({"OWN-ISSUED-1"})
        with mock.patch.object(document_marks, "_own_issued", return_value=own), \
                mock.patch.object(ts, "_people_tag", return_value=self.tag):
            with mock.patch.object(bc, "own_issued_in_views", return_value=False):
                sent = json.loads(tool._comms_settings(scope, tmp.OWNER)["plx.reader_hidden"])
            with mock.patch.object(bc, "own_issued_in_views", return_value=True):
                kept = json.loads(tool._comms_settings(scope, tmp.OWNER)["plx.reader_hidden"])
        self.assertIn("OWN-ISSUED-1", sent)
        self.assertNotIn("OWN-ISSUED-1", kept)

    def test_a_day_filter_reads_its_own_index(self):
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            days = [r[0] for r in pg.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = "
                "'emails'", (bv.SCHEMA,)).fetchall()]
            pg.execute("SET LOCAL enable_seqscan = off")
            plan = "\n".join(r[0] for r in pg.execute(
                f"EXPLAIN SELECT handle FROM {bv.SCHEMA}.emails WHERE received_date >= "
                "'2026-09-01'").fetchall())
        self.assertTrue(any(d.endswith("(received_date)") for d in days), days)
        self.assertRegex(plan, r"Cond: \(received_date")

    def test_hidden_ids_are_one_hashed_set_per_statement(self):
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            ddl = pg.execute("SELECT pg_get_viewdef(%s::regclass)",
                             (f"{sql_views.guarded_schema()}.emails",)).fetchone()[0]
            pg.execute("SELECT set_config('plx.reader_hidden', '[\"x\"]', true)")
            plan = "\n".join(r[0] for r in pg.execute(
                f"EXPLAIN SELECT * FROM {bv.SCHEMA}.emails t WHERE t.object_ids[1] NOT IN "
                "(SELECT json_array_elements_text(current_setting('plx.reader_hidden', "
                "true)::json))").fetchall())
        self.assertIn("plx.reader_hidden", ddl)
        self.assertIn("hashed SubPlan", plan)
        # each setting is checked once per statement (an InitPlan), never per rescan
        for name in sql_views.SETTINGS:
            self.assertRegex(ddl, r"\(\s*SELECT \(?current_setting\('" + name.replace(".", r"\.")
                             + r"'::text, true\) IS NOT NULL\)?\s*\)")


class _Explaining:
    """A connection that plans (EXPLAIN, seq scans off, the least work_mem) each statement
    before running it and records every subplan that reads a table other than by a
    correlated index probe, or reads a materialized set, per row."""
    _EXPLAINABLE = ("select", "with", "insert", "update", "delete")

    def __init__(self, pg, found):
        self._pg, self.found, self.planned, self.seen = pg, found, 0, []

    def __getattr__(self, name):
        return getattr(self._pg, name)

    def execute(self, sql, params=None):
        import re
        head = sql.lstrip().lower()
        if head.startswith(self._EXPLAINABLE) or re.match(r"create temp table \w+ as ", head):
            self._pg.execute("SET enable_seqscan = off")
            self._pg.execute("SET work_mem = '64kB'")
            try:
                plan = self._pg.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()[0]
            finally:
                self._pg.execute("RESET enable_seqscan")
                self._pg.execute("RESET work_mem")
            self.planned += 1
            self.seen.append((sql, plan))
            for bad in _per_row_reads(plan[0]["Plan"]):
                self.found.append((sql[:160], bad))
        return self._pg.execute(sql, params)


def _per_row_reads(node, in_subplan=False):
    """The nodes of ``node``'s tree that a subplan (run per row, hashed or not) reads a
    relation with other than a conditioned index probe (an index or bitmap index scan on
    the row's key: one lookup per row), or a materialized set."""
    sub = in_subplan or (node.get("Parent Relationship") == "SubPlan")
    out = []
    if sub:
        kind = node.get("Node Type", "")
        if kind in ("Materialize", "CTE Scan", "WorkTable Scan"):
            out.append(f"{kind} in {node.get('Subplan Name', 'a subplan')}")
        elif node.get("Relation Name") and not (
                (kind in ("Index Scan", "Index Only Scan") and node.get("Index Cond"))
                or (kind == "Bitmap Heap Scan" and node.get("Recheck Cond"))):
            out.append(f"{kind} on {node['Relation Name']}")
    for child in node.get("Plans", ()):
        out += _per_row_reads(child, sub)
    return out


if __name__ == "__main__":
    unittest.main()
