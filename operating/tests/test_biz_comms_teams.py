"""``teams_messages`` (``operating.pg.business_comms``) read through the one door
(``plx.sql`` over ``biz_sql.teams_messages``): the set a reader sees is the Teams item
gate's, row for row.

The Teams parity fixture (``test_table_teams_parity``: a real capture of one chat whose
members are cos (whole history) and a guest; vince joins later with history from between
the first two messages; the two fixture messages, one naming Freightcom in its text only,
and 60 more) loaded into a throwaway Postgres; the document text written by the projection's text
pass; the people exclusion's set built by its real build; ``teams_messages`` built by the
business build and its guarded view installed. Readers: narrow (granted Teams, member of
nothing; and one not granted at all), mid (vince: from his history on), owner-equivalent
(cos: every message). Checked at the boundary: the handles ``plx.sql`` returns are the
gate's minus Freightcom's; a message whose document text cannot be read is withheld; an unset or
foreign window shows nothing; a member excluded after the capture withholds the chat.

Evidence label: injected-transport (no network, made-up ids and addresses)."""
from __future__ import annotations

import json
import os
from unittest import mock

from integrations.people_exclusion import PeopleExclusion
from integrations.tests import test_capture_teams as tct
from operating import ontology_tools_sql as ts
from operating.pg import business_comms as bc
from operating.pg import business_views as bv
from operating.pg import people_excluded, sql_views
from operating.tests import test_table_teams_parity as ttp

# the disk guard's floor (PLX_BIZ_MIN_FREE_GB) is the served host's: a test host's free
# space is not what these tests are about (the guard's own test sets the floor back)
_NO_FLOOR = mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "0"})


def setUpModule():
    _NO_FLOOR.start()


def tearDownModule():
    _NO_FLOOR.stop()

READERS = ttp.READERS


def _cell(value):
    return value["untrusted_text"] if isinstance(value, dict) else value


class TeamsThroughTheDoor(ttp.TableTeamsParity):
    def project(self):
        """The fixture's projection with the document text index the served build writes
        (``ontology.text_index``): the record pass chunks a Teams message from it."""
        import datetime as dt
        from pathlib import Path
        from ontology import schema as schema_loader
        from ontology.project import build_store
        from ontology.project_graph import TeamsCaptures
        from ontology.text_index import store_text_index
        path = Path(self.tmp.name) / "projection.sqlite"
        if not path.exists():
            with TeamsCaptures(self.store.path) as captured:
                self.report = build_store(path, schema_loader.load_schema(),
                                          {"teams": captured},
                                          clock=lambda: dt.datetime.now(dt.timezone.utc),
                                          post_project={"text_index": store_text_index})
        return path, self.report

    def setUp(self) -> None:
        super().setUp()
        import psycopg
        from ontology import schema as schema_loader
        from operating.pg.migrate import MIGRATIONS_DIR
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for name in ("0027_item_audiences.sql", "0038_own_issued_documents.sql", "0040_people_excluded_objects.sql",
                         "0041_business_views.sql"):
                pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
            # the message bodies are in the projection's document text, which the build reads
            self.assertEqual(pg.execute("SELECT count(DISTINCT object_id) FROM "
                                        "ontology.text_fields WHERE object_id LIKE 'TeamsChatMessage:%'"
                                        ).fetchone()[0], 63)
        # the people exclusion's build reads the mailbox directory from the mail capture:
        # this host captured no mail, so its store is there and empty (no mailbox)
        from integrations.capture_mail import MailCaptureStore
        MailCaptureStore(self.runtime.state_dir).db.close()
        with psycopg.connect(self.dsn) as pg:
            people_excluded.build(pg, self.runtime.state_dir, environ=tct.ENV, workers=2)
        self.tag = people_excluded.current_exclusion(tct.ENV)
        self.assertTrue(self.tag)
        bv.build(self.dsn, ["teams_messages"], schema=schema_loader.load_schema())
        sql_views.install(self.dsn)

    def sql(self, actor, sql, tag=None):
        tool = ts.SqlTool(self.tool().base)
        with mock.patch.object(ts, "_people_tag", return_value=self.tag if tag is None
                               else tag):
            out = tool.call("plx.sql", {"sql": sql}, actor)
        self.assertNotIn("error", out, out)
        return out

    def handles(self, actor, tag=None):
        out = self.sql(actor, "SELECT handle FROM teams_messages", tag)
        self.assertFalse(out["truncated"])
        return {_cell(r["cells"][0]) for r in out["rows"]}

    def oids(self, actor):
        of = {h: o for o, h in self.messages.items()}
        got = self.handles(actor)
        self.assertTrue(got <= set(of), got - set(of))
        return {of[h] for h in got}

    def test_sql_scoped_set_is_the_item_gates_row_for_row(self):
        bulk = {f"4{k:03d}" for k in range(60)}
        expected = {tct.OUTSIDER: set(), ttp.WITHHELD: set(),
                    tct.VINCE_ONLY: {"3002"} | bulk, tct.COS_ONLY: {"3001", "3002"} | bulk}
        for actor in READERS:
            with self.subTest(actor=sorted(actor.principals)):
                got = self.oids(actor)
                self.assertEqual(got, self.gated(actor) - self.fc())
                self.assertEqual(self.ids(got), expected[actor] | (
                    self.ids(self.gated(actor)) & {"3003"}))
                out = self.sql(actor, "SELECT count(*), count(DISTINCT chat) FROM "
                                      "teams_messages")
                self.assertEqual(int(_cell(out["rows"][0]["cells"][0])), len(got))

    def found(self, actor, query):
        """Every plx.search hit (mail and Teams path) for ``query``, page by page."""
        tool = ts.SqlTool(self.tool().base)
        got, cursor = [], None
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            for _ in range(20):
                out = tool.comms_search({"query": query, "limit": 25,
                                         **({"cursor": cursor} if cursor else {})}, actor)
                got += [h["citation"] for h in out["documents"]]
                self.assertEqual({h["kind"] for h in out["documents"]} - {"teams_message"},
                                 set())
                cursor = out["completeness"]["next_cursor"]
                if not cursor:
                    break
        self.assertEqual(len(got), len(set(got)), "a hit doubled across pages")
        return set(got)

    def test_search_finds_only_messages_in_the_readers_chats_and_window(self):
        """A word of a Teams message's text: the hits are exactly the reader's own rows
        (``teams_messages`` through the door) whose text holds it; never one outside the
        reader's chats or history window."""
        import psycopg
        import re
        from operating.pg import comms_search as cs
        with psycopg.connect(self.dsn) as pg:
            rows = pg.execute("SELECT f.citation, f.field_id, c.start, c.body FROM "
                              "ontology.text_fields f JOIN ontology.text_chunks c ON "
                              "c.field_id = f.field_id ORDER BY 1, 2, 3").fetchall()
        pieces: dict[str, list] = {}
        for h, f, start, body in rows:
            pieces.setdefault(h, []).append((f, start, body))
        texts = {h: cs.assemble(v).lower() for h, v in pieces.items()}
        # ("freight" and "freightcom" tested the Freightcom withholding, removed 11-23)
        for word in ("note", "quote", "pickup"):
            for actor in READERS:
                with self.subTest(word=word, actor=sorted(actor.principals)):
                    mine = self.handles(actor)
                    want = {h for h in mine if re.search(rf"\b{word}\b", texts.get(h, ""))}
                    self.assertEqual(self.found(actor, word), want)
        self.assertEqual(len(self.found(tct.COS_ONLY, "note")), 60)
        self.assertEqual(len(self.found(tct.VINCE_ONLY, "note")), 60)
        self.assertEqual(self.found(tct.OUTSIDER, "note"), set())
        # vince's window starts after the first message: its words are cos's only
        self.assertLess(len(self.found(tct.VINCE_ONLY, "quote")),
                        len(self.found(tct.COS_ONLY, "quote")))

    def test_the_text_sidecar_finds_what_the_word_index_finds_and_follows_a_marking(self):
        """``biz.comm_text`` (migration 0074): a word search through the sidecar returns, for
        every reader, the hits the chunk word index returns (60 for 'note'); a message the
        sidecar has no row for yet is found all the same (its text read the old way); a
        marking set on a field after the build hides the message at once, and lifted shows
        it again; no state (the sidecar not built) reads the old way."""
        import psycopg
        from operating.pg import comm_text
        from operating.pg.migrate import MIGRATIONS_DIR
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for name in ("0074_comm_text.sql", "0075_comm_text_parts.sql"):
                pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        words = ("note", "quote", "pickup")
        old = {(tuple(sorted(a.principals)), w): self.found(a, w) for a in READERS
               for w in words}                       # no state yet: the old way
        self.assertEqual(len(old[(tuple(sorted(tct.COS_ONLY.principals)), "note")]), 60)
        with mock.patch("operating.pg.generic_records.room"):
            built = comm_text.build(self.dsn, views=["teams_messages"], log=lambda *a, **k: None)
        self.assertGreater(built["teams_messages"]["rows"], 60)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            self.assertEqual(pg.execute("SELECT count(*) FROM biz.comm_text WHERE field_id IS "
                                        "NOT NULL").fetchone()[0],
                             built["teams_messages"]["rows"] - built["teams_messages"]["no_text"])
        for a in READERS:
            for w in words:
                with self.subTest(sidecar="built", actor=sorted(a.principals), word=w):
                    self.assertEqual(self.found(a, w), old[(tuple(sorted(a.principals)), w)])
        # 1, 10 and all of the sidecar's rows gone: each of those messages read the old way
        for n in (1, 10, 60):
            with psycopg.connect(self.dsn, autocommit=True) as pg:
                pg.execute("UPDATE biz.comm_text_state SET built_field_id = 0")
                pg.execute("DELETE FROM biz.comm_text WHERE handle IN (SELECT handle FROM "
                           "biz.comm_text WHERE field_id IS NOT NULL ORDER BY handle LIMIT %s)",
                           (n,))
            for a in READERS:
                with self.subTest(missing=n, actor=sorted(a.principals)):
                    self.assertEqual(self.found(a, "note"),
                                     old[(tuple(sorted(a.principals)), "note")])
            with mock.patch("operating.pg.generic_records.room"):
                comm_text.refresh(self.dsn, log=lambda *a, **k: None)   # puts them back
        # a marking set after the build: the message is gone at once; lifted, it is back
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            h, fid = pg.execute("SELECT handle, field_id FROM biz.comm_text WHERE field_id IS "
                                "NOT NULL ORDER BY handle LIMIT 1").fetchone()
            pg.execute("UPDATE ontology.text_fields SET markings = '[\"hr\"]' WHERE "
                       "field_id = %s", (fid,))
        gone = self.found(tct.COS_ONLY, "note") | self.found(tct.COS_ONLY, "quote") \
            | self.found(tct.COS_ONLY, "pickup")
        self.assertNotIn(h, gone)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("UPDATE ontology.text_fields SET markings = '[]' WHERE field_id = %s",
                       (fid,))
        back = self.found(tct.COS_ONLY, "note") | self.found(tct.COS_ONLY, "quote") \
            | self.found(tct.COS_ONLY, "pickup")
        self.assertEqual(back, old[(tuple(sorted(tct.COS_ONLY.principals)), "note")]
                         | old[(tuple(sorted(tct.COS_ONLY.principals)), "quote")]
                         | old[(tuple(sorted(tct.COS_ONLY.principals)), "pickup")])

    def test_the_sidecar_holds_a_second_text_field_and_a_split_field_whole(self):
        """Review minors on 0074: a message with two text fields has a sidecar row for each
        (a word only the second holds is found, one row per field, no stop); a field whose
        vectors pass the piece size is held in pieces (every piece a row, none lost) and still
        matches what the word index matches, words that sit in two pieces included; the search
        and the refresh never drop a message for either."""
        import psycopg
        from operating.pg import comm_text
        from operating.pg.migrate import MIGRATIONS_DIR
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for name in ("0074_comm_text.sql", "0075_comm_text_parts.sql"):
                pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        words = ("note", "quote", "pickup")
        old = {w: self.found(tct.COS_ONLY, w) for w in words}
        with mock.patch("operating.pg.generic_records.room"):
            comm_text.build(self.dsn, views=["teams_messages"], log=lambda *a, **k: None)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            h, obj, fid = pg.execute(
                "SELECT t.handle, f.object_id, f.field_id FROM biz.comm_text t JOIN "
                "ontology.text_fields f ON f.field_id = t.field_id ORDER BY t.handle LIMIT 1"
            ).fetchone()
            top = pg.execute("SELECT max(field_id) FROM ontology.text_fields").fetchone()[0]
            cid = pg.execute("SELECT coalesce(max(chunk_id), 0) FROM ontology.text_chunks"
                             ).fetchone()[0] + 1
            pg.execute("INSERT INTO ontology.text_fields (field_id, object_id, version_handle, "
                       "citation, field_path, document_kind, sender_class, markings, "
                       "text_digest) SELECT %s, object_id, version_handle, citation, "
                       "'row.second', document_kind, sender_class, '[]', 'd2' FROM "
                       "ontology.text_fields WHERE field_id = %s", (top + 1, fid))
            pg.execute("INSERT INTO ontology.text_chunks (chunk_id, field_id, start, body) "
                       "VALUES (%s, %s, 0, ''::bytea)", (cid, top + 1))
            pg.execute("INSERT INTO ontology.chunk_words (chunk_id, words) VALUES (%s, "
                       "to_tsvector('simple', 'zebrasecond'))", (cid,))
            pg.execute("UPDATE biz.comm_text_state SET built_field_id = %s", (top,))
        with mock.patch("operating.pg.generic_records.room"):
            comm_text.refresh(self.dsn, log=lambda *a, **k: None)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            rows = pg.execute("SELECT field_id, part, whole FROM biz.comm_text WHERE handle = %s "
                              "ORDER BY field_id", (h,)).fetchall()
        self.assertEqual([(r[0], r[1], r[2]) for r in rows], [(fid, 0, True), (top + 1, 0, True)])
        self.assertIn(h, self.found(tct.COS_ONLY, "zebrasecond"))
        for w in words:
            self.assertEqual(self.found(tct.COS_ONLY, w), old[w])
        # pieces: the same build with a piece of a few bytes (every field splits)
        with mock.patch.object(comm_text, "PIECE_BYTES", 60), \
                mock.patch("operating.pg.generic_records.room"):
            comm_text.build(self.dsn, views=["teams_messages"], log=lambda *a, **k: None)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            split, whole = pg.execute("SELECT count(*) FILTER (WHERE NOT whole), "
                                      "count(*) FILTER (WHERE whole AND field_id IS NOT NULL) "
                                      "FROM biz.comm_text").fetchone()
            pieces = pg.execute("SELECT max(part) FROM biz.comm_text").fetchone()[0]
        self.assertGreater(split, 0)
        self.assertGreater(pieces, 0)
        for w in words:
            self.assertEqual(self.found(tct.COS_ONLY, w), old[w], w)
        self.assertIn(h, self.found(tct.COS_ONLY, "zebrasecond"))
        # seal (a build that stopped on its disk floor): refused while a message since the date
        # has no row, written once every one has
        import datetime as dt
        since = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("DELETE FROM biz.comm_text WHERE handle = %s", (h,))
        with self.assertRaises(comm_text.CommTextError):
            comm_text.seal(self.dsn, view="teams_messages", since=since, log=lambda *a, **k: None)
        with mock.patch("operating.pg.generic_records.room"):
            comm_text.refresh(self.dsn, log=lambda *a, **k: None)
        got = comm_text.seal(self.dsn, view="teams_messages", since=since,
                             log=lambda *a, **k: None)
        self.assertGreater(got["teams_messages"]["rows"], 60)

    def test_a_word_search_reads_the_word_index_once_whatever_the_reader_reads(self):
        """The search's word match is read once per statement, never again for each row
        the reader may read: live, a Teams search re-ran it for every one of a full
        reader's messages (past 90 s). Checked on the plan the reader's own statement runs:
        every word-index node outside a row's own check (the walk's per-row subplans, bounded
        by the walk) is an init plan run at most once; with a walk of 1 or 2 rows (too few
        for the page) the whole match is read, and once. A walk reads no more of the reader's
        rows than its stages hold."""
        from operating.pg import comms_search as cs
        tool = ts.SqlTool(self.tool().base)
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            conn, settings = tool._comms_reader(tct.COS_ONLY)
            views = ["teams_messages"]
            sql = cs.search_sql(views, guarded=sql_views.guarded_schema(), biz=bv.SCHEMA,
                                query=True, party_kind=None, since=False, until=False,
                                cursor=False)
            plans = {walk: tool._comms_scoped(conn, settings, lambda pg, walk=walk: pg.execute(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + sql,
                {"q": cs.tsquery("note"), "party": None, "since": None, "until": None,
                 "c_at": None, "c_h": None, "k": 26, "k_own": 26, "walk": walk}).fetchone(),
                tct.COS_ONLY)[0] for walk in (1, 2, cs.WALK_ROWS)}
        whole = 0
        for walk, plan in plans.items():
            # every node with the relationships above it (InitPlan: run once per statement;
            # a join's inner side: run again for each outer row when the planner chooses to)
            nodes, stack = [], [((plan[0] if isinstance(plan, list) else plan)["Plan"], ())]
            while stack:
                node, above = stack.pop()
                nodes.append((node, above))
                stack += [(c, above + ((c.get("Parent Relationship"), c),)) for c in
                          node.get("Plans") or []]
            # the match on the message's own words (``b.words @@``) and on its text's words
            words = [(n, above) for n, above in nodes if n.get("Relation Name") in (
                "teams_messages", "chunk_words") and "words @@" in str(n.get("Index Cond") or "")
                + str(n.get("Recheck Cond") or "") + str(n.get("Filter") or "")
                and "SubPlan" not in [r for r, _ in above]]   # a row's own check, a snippet
            self.assertTrue(words, (walk, [n.get("Node Type") for n, _ in nodes]))
            # a walk reads the reader's rows from its floors down, never every row the
            # reader holds (a COALESCE kept the floor out of the guarded view: 2.5 million
            # mails read and sorted per walk, live)
            walked = [n for n, above in nodes if n.get("Relation Name") == "teams_messages"
                      and any(str(a.get("Subplan Name", "")).startswith("CTE found_")
                              for _, a in above)
                      and "SubPlan" not in [r for r, _ in above]]
            self.assertTrue(walked, walk)
            for n in walked:
                self.assertLessEqual(n["Actual Rows"] * n["Actual Loops"],
                                     walk * cs.DEEPER ** (cs.STAGES - 1), (walk, n))
            for n, above in words:
                once = [a for r, a in above if r == "InitPlan"]
                self.assertTrue(once, (walk, n.get("Relation Name"), [r for r, _ in above]))
                self.assertIn(once[0]["Actual Loops"], (0, 1), (walk, n.get("Relation Name")))
                whole += once[0]["Actual Loops"]
        self.assertTrue(whole, "no walk was short: the whole match never ran")
        self.assertEqual(len(self.found(tct.COS_ONLY, "note")), 60)

    def test_a_teams_search_is_the_same_whatever_rows_the_walk_reads_first(self):
        """A Teams search walks the reader's newest rows before it reads the word index's
        whole match (``comms_search.WALK_ROWS``): a walk of 1, 2 or 7 rows, one over every
        row and a rare word's (the index at once) give each reader the same pages, in the
        same order (1, 10, 60 a page)."""
        from operating.pg import comms_search as cs
        tool = ts.SqlTool(self.tool().base)

        def pages(actor, limit, query):
            got, cursor = [], None
            for _ in range(200):
                out = tool.comms_search({"query": query, "limit": limit, **(
                    {"cursor": cursor} if cursor else {})}, actor)
                got.append([h["citation"] for h in out["documents"]])
                cursor = out["completeness"]["next_cursor"]
                if not cursor:
                    return got
            self.fail("the pages never ended")
        walked = 0
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            for actor in READERS:
                for word in ("note", "quote"):
                    for limit in (1, 10, 60):
                        with mock.patch.object(cs, "WALK_ROWS", 100000), \
                                mock.patch.object(cs, "COMMON_CHUNKS", 0), \
                                mock.patch.object(cs, "COMMON_MESSAGES", 0):
                            whole = pages(actor, limit, word)
                        self.assertEqual(pages(actor, limit, word), whole)
                        for rows in (1, 2, 7):
                            with self.subTest(actor=sorted(actor.principals), word=word,
                                              limit=limit, walk=rows), \
                                    mock.patch.object(cs, "WALK_ROWS", rows), \
                                    mock.patch.object(cs, "COMMON_CHUNKS", 0), \
                                    mock.patch.object(cs, "COMMON_MESSAGES", 0):
                                self.assertEqual(pages(actor, limit, word), whole)
                        walked += sum(map(len, whole)) > 7
        self.assertTrue(walked, "no search held more rows than the walk: the index read "
                                "past the walk never ran")

    def test_search_and_open_show_the_posters_name_not_the_raw_sender(self):
        """D16: a Teams hit (and the opened message) names its poster by the view's
        ``author``, read when the result is made, never the sender's raw JSON; a message
        with no named poster states no address. n of m against the view itself, per reader."""
        import psycopg
        tool = ts.SqlTool(self.tool().base)
        checked = 0
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            for actor in READERS:
                name = {_cell(r["cells"][0]): _cell(r["cells"][1]) for r in self.sql(
                    actor, "SELECT handle, author FROM teams_messages")["rows"]}
                out = tool.comms_search({"query": "note", "limit": 25}, actor)
                for hit in out["documents"]:
                    got = (hit["sender"].get("address") or {}).get("untrusted_text")
                    self.assertEqual(got, name[hit["citation"]] or None, hit["citation"])
                    self.assertFalse((got or "").startswith("{"))
                    opened = tool.comms_open({"citation": hit["citation"]}, actor)
                    said = (opened.get("sender") or {}).get("untrusted_text")
                    self.assertEqual(said, name[hit["citation"]] or None)
                    checked += 1
        self.assertTrue(checked)

    def test_the_view_carries_the_author_and_the_whole_text(self):
        """``author`` is the sender's display name and ``said`` the message's whole text, for
        every row the reader reads (n of m against the document text itself); a marked text
        field is not read into ``said``; applying again changes nothing."""
        import zlib
        import psycopg
        from operating.pg import teams_text
        out = self.sql(tct.COS_ONLY, "SELECT handle, sender, author, author_kind, said FROM "
                                     "teams_messages")
        rows = [[_cell(c) for c in r["cells"]] for r in out["rows"]]
        self.assertTrue(rows)
        with psycopg.connect(self.dsn) as pg:
            want = {}
            for handle, fid in pg.execute(
                    "SELECT version_handle, field_id FROM ontology.text_fields WHERE "
                    "document_kind = 'teams_message' AND markings = '[]'").fetchall():
                want[handle] = "".join(zlib.decompress(bytes(b)).decode() for (b,) in pg.execute(
                    "SELECT body FROM ontology.text_chunks WHERE field_id = %s ORDER BY start",
                    (fid,)).fetchall())
        wrong = [r[0] for r in rows if (r[4] or None) != (want.get(r[0]) or None)]
        self.assertEqual(wrong, [], "said differs from the document text")
        self.assertEqual(sum(1 for r in rows if r[4]), len(rows))   # every fixture message has text
        for handle, sender, author, kind, _said in rows:
            who = json.loads(sender) if sender and sender.startswith("{") else {}
            self.assertEqual(author, who.get("display_name") or None, handle)
            self.assertEqual(kind, who.get("kind") or None, handle)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("UPDATE ontology.text_fields SET markings = '[\"finance\"]' WHERE "
                       "version_handle = %s", (rows[0][0],))
            got = teams_text.apply(pg, "biz.teams_messages")
            self.assertEqual(got["marked_text_withheld"], 1)
            self.assertEqual(got["with_text"], len(rows) - 1)
            self.assertIsNone(pg.execute("SELECT said FROM biz.teams_messages WHERE handle = %s",
                                         (rows[0][0],)).fetchone()[0])   # no stale text

    def test_a_text_marked_after_the_build_leaves_no_said_for_any_reader(self):
        """A field's markings are changed in place after the build (``text_remark`` and the
        ontology migration write ``ontology.text_fields.markings``): the writer's hook
        (``teams_text.apply_fields``) clears ``said`` in the same transaction, so a message
        whose text is marked later is never read as text by owner (cos), staff (vince) or a
        restricted reader; undoing the mark gives the text back."""
        import json as _json
        import tempfile
        from pathlib import Path
        import psycopg
        from operating.pg import text_remark
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            handles = [h for (h,) in pg.execute(
                "SELECT handle FROM biz.teams_messages WHERE said IS NOT NULL ORDER BY handle "
                "LIMIT 3").fetchall()]
            self.assertEqual(len(handles), 3)
            fields = {h: f for h, f in pg.execute(
                "SELECT version_handle, field_id FROM ontology.text_fields WHERE "
                "version_handle = ANY(%s) AND document_kind = 'teams_message'",
                (handles,)).fetchall()}
        self.assertEqual(set(fields), set(handles))

        def said(actor):
            out = self.sql(actor, "SELECT handle, said FROM teams_messages")
            return {_cell(r["cells"][0]): _cell(r["cells"][1]) for r in out["rows"]}
        before = {a: said(a) for a in READERS}
        self.assertTrue(all(before[tct.COS_ONLY][h] for h in handles))
        self.assertTrue(any(v for v in before[tct.VINCE_ONLY].values()))
        for actor in READERS:   # a restricted reader sees no row it may not, and no text of one
            self.assertLessEqual(set(before[actor]), set(before[tct.COS_ONLY]))
        with tempfile.TemporaryDirectory() as tmp:
            # the writer under test is text_remark's undo: it sets each field's markings back
            # to ``old`` (here a mark) where they are ``new`` (now none)
            Path(tmp, "undo.jsonl").write_text("\n".join(_json.dumps(
                {"field_id": f, "old": '["finance"]', "new": "[]"}) for f in fields.values()))
            self.assertEqual(text_remark.undo(Path(tmp), self.dsn)["restored"], 3)
            for actor in READERS:
                got = said(actor)
                for h in handles:
                    self.assertIsNone(got.get(h), (sorted(actor.principals), h))
                rest = {h for h in got if h not in handles and got[h]}
                self.assertEqual(rest, {h for h in before[actor] if h not in handles
                                        and before[actor][h]})   # the others keep their text
            # the mark lifted again: the writer gives the text back
            Path(tmp, "undo.jsonl").write_text("\n".join(_json.dumps(
                {"field_id": f, "old": "[]", "new": '["finance"]'}) for f in fields.values()))
            self.assertEqual(text_remark.undo(Path(tmp), self.dsn)["restored"], 3)
            self.assertEqual(said(tct.COS_ONLY), before[tct.COS_ONLY])

    def _said_fixture(self):
        import psycopg
        pg = psycopg.connect(self.dsn)
        self.addCleanup(pg.close)
        rows = pg.execute("SELECT t.handle, f.field_id, f.object_id, f.citation, f.field_path, "
                          "f.document_kind, f.sender_class, f.markings, f.text_digest FROM "
                          "biz.teams_messages t JOIN ontology.text_fields f ON f.version_handle "
                          "= t.handle AND f.document_kind = 'teams_message' WHERE t.said IS NOT "
                          "NULL ORDER BY t.handle LIMIT 3").fetchall()
        self.assertEqual(len(rows), 3)
        return pg, rows

    def _said(self, pg, handle):
        return pg.execute("SELECT said FROM biz.teams_messages WHERE handle = %s",
                          (handle,)).fetchone()[0]

    def test_a_document_removed_by_a_migration_leaves_no_said(self):
        """``ontology_migrate`` deletes the text fields of a version the store no longer holds:
        the message's stored text goes in the same transaction; the others keep theirs."""
        from operating.pg import ontology_migrate
        pg, rows = self._said_fixture()
        gone, kept = rows[0], rows[1:]
        pg.execute("CREATE TEMP TABLE prior (object_id text PRIMARY KEY)")
        pg.execute("INSERT INTO prior VALUES (%s)", (gone[2],))
        pg.execute("DELETE FROM ontology.object_versions WHERE object_id = %s AND l1_handle = %s",
                   (gone[2], gone[0]))
        before = {r[0]: self._said(pg, r[0]) for r in rows}
        self.assertTrue(all(before.values()))
        report: dict = {}
        ontology_migrate._drop_gone_fields(pg.cursor(), report)
        self.assertEqual(report["deleted.text_fields"], 1)
        self.assertIsNone(self._said(pg, gone[0]))
        for r in kept:
            self.assertEqual(self._said(pg, r[0]), before[r[0]])
        pg.rollback()

    def test_a_delta_that_copies_text_fields_again_rereads_said(self):
        """``ontology_delta`` replaces a layer's text fields: a message whose text came back
        marked, or is gone from the layer, loses its ``said``; an unchanged one keeps it."""
        import json as _json
        import sqlite3
        from operating.pg import ontology_delta
        pg, rows = self._said_fixture()
        marked, removed, same = rows
        lite = sqlite3.connect(":memory:")
        lite.execute("CREATE TABLE text_fields (field_id integer, object_id text, "
                     "version_handle text, citation text, field_path text, document_kind text, "
                     "sender_class text, markings text, text_digest text)")
        lite.execute("CREATE TABLE text_chunks (field_id integer, start integer, body blob)")
        for row in (marked, same):   # the layer holds these two; ``removed`` is not in it
            mark = '["finance"]' if row is marked else row[7]
            lite.execute("INSERT INTO text_fields VALUES (?,?,?,?,?,?,?,?,?)",
                         (row[1], row[2], row[0], *row[3:7], mark, row[8]))
            for start, body in pg.execute("SELECT start, body FROM ontology.text_chunks WHERE "
                                          "field_id = %s", (row[1],)).fetchall():
                lite.execute("INSERT INTO text_chunks VALUES (?,?,?)",
                             (row[1], start, bytes(body)))
        owned = [r[2] for r in rows]
        before = {r[0]: self._said(pg, r[0]) for r in rows}
        cur = pg.cursor()
        cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY)")
        for oid in owned:
            cur.execute("INSERT INTO owned VALUES (%s)", (oid,))
        report = ontology_delta._diff_text(lite, cur, _json.dumps(owned), None)
        self.assertEqual(report["kept.text_fields"], 1)           # the unchanged one
        self.assertIsNone(self._said(pg, marked[0]))                # came back marked
        self.assertIsNone(self._said(pg, removed[0]))               # gone from the layer
        self.assertEqual(self._said(pg, same[0]), before[same[0]])  # unchanged
        pg.rollback()

    def test_a_rebuild_swaps_in_under_any_views_over_the_table_and_restores_them(self):
        """Found on the Teams train copy: ``assist.matter_items`` reads biz.teams_messages and
        blocked the swap's DROP of the old table. Any view over the table, and a view on
        top of that one, is saved, dropped top-down and made again bottom-up: the same
        definition, options, owner, grants and comments."""
        import psycopg
        from ontology import schema as schema_loader
        from operating.pg.migrate import MIGRATIONS_DIR
        snapshot = ("SELECT c.relname, pg_get_viewdef(c.oid, true), c.reloptions, "
                    "pg_get_userbyid(c.relowner), c.relacl::text, obj_description(c.oid, 'pg_class'), "
                    "(SELECT array_agg(a.attname || '=' || col_description(a.attrelid, a.attnum) "
                    "ORDER BY a.attnum) FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 "
                    "AND col_description(a.attrelid, a.attnum) IS NOT NULL) "
                    "FROM pg_class c WHERE c.oid = ANY(%s::regclass[]) ORDER BY 1")
        names = ["assist.matter_items", "assist.t_low", "assist.t_top"]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for role in ("plx_api", "plx_eval"):
                if not pg.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                    pg.execute(f"CREATE ROLE {role} NOLOGIN")
            pg.execute("CREATE TABLE IF NOT EXISTS biz.emails (handle text, sender text, "
                       "subject text)")   # the list view reads both comms tables when both exist
            for name in ("0062_assist_readings.sql", "0063_assist_work.sql", "0065_assist_api.sql",
                         "0073_assist_items_view_columns.sql"):
                pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
            # two more views of different shapes, one on the other
            pg.execute("CREATE VIEW assist.t_low WITH (security_barrier = true) AS "
                       "SELECT handle, sender FROM biz.teams_messages WHERE handle IS NOT NULL")
            pg.execute("GRANT SELECT ON assist.t_low TO plx_eval WITH GRANT OPTION")
            pg.execute("COMMENT ON VIEW assist.t_low IS 'low view'")
            pg.execute("COMMENT ON COLUMN assist.t_low.sender IS 'who posted'")
            pg.execute("CREATE VIEW assist.t_top AS SELECT handle, upper(sender) AS who "
                       "FROM assist.t_low")
            pg.execute("GRANT SELECT ON assist.t_top TO plx_api, PUBLIC")
            before = pg.execute(snapshot, (names,)).fetchall()
            self.assertEqual(len(before), 3)
            self.assertIn("security_barrier=true", before[1][2] and before[1][2][0].replace(" ", ""))
            rows = pg.execute("SELECT count(*) FROM biz.teams_messages").fetchone()[0]
        bv.build(self.dsn, ["teams_messages"], schema=schema_loader.load_schema())
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            self.assertEqual(pg.execute("SELECT count(*) FROM biz.teams_messages").fetchone()[0], rows)
            self.assertEqual(pg.execute(snapshot, (names,)).fetchall(), before)
            self.assertEqual(pg.execute("SELECT count(*) FROM assist.t_top").fetchone()[0],
                             pg.execute("SELECT count(*) FROM assist.t_low").fetchone()[0])

    def test_cardinality_1_10_60_by_day(self):
        out = self.sql(tct.COS_ONLY, "SELECT created_date, count(*) FROM teams_messages "
                                     "GROUP BY 1 ORDER BY 1")
        got = {str(_cell(r["cells"][0])): int(_cell(r["cells"][1])) for r in out["rows"]}
        self.assertEqual(set(got), {"2026-09-30", "2026-10-01"}, got)
        self.assertEqual(sum(got.values()), len(self.gated(tct.COS_ONLY) - self.fc()))

    def test_a_teams_finding_shows_only_with_the_message_the_reader_reads(self):
        """The findings view checks a Teams finding against its one message by equality
        (``comms_findings.view_ddl``): each reader sees exactly the findings of the messages
        their own guarded view shows, and a day filter has its own index."""
        import psycopg
        from operating import assist_readings as ar
        from operating.pg import comms_findings as cf
        with psycopg.connect(self.dsn) as pg:
            ar.ensure(pg)
            pg.execute(f"INSERT INTO {ar.SCHEMA}.{ar.TABLE} (source, message_key, handles, "
                       "said_at, read_ver, status, findings) SELECT 'teams_messages', handle, "
                       "ARRAY[handle], created_at, 1, 'ok', '[{\"kind\": \"request\", \"topic\": "
                       "\"order\", \"summary\": \"Asked\", \"who\": \"\", \"quote\": \"\"}]' "
                       f"FROM {bv.SCHEMA}.teams_messages ON CONFLICT DO NOTHING")
            cf.create_view(pg)
            days = [r[0] for r in pg.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = "
                "'teams_messages'", (bv.SCHEMA,)).fetchall()]
        self.assertTrue(any(d.endswith("(created_date)") for d in days), days)
        seen = 0
        for actor in READERS:
            with self.subTest(actor=sorted(actor.principals)):
                out = self.sql(actor, "SELECT handle, source FROM comms_findings")
                self.assertFalse(out["truncated"])
                got = [tuple(_cell(c) for c in r["cells"]) for r in out["rows"]]
                self.assertEqual(sorted(h for h, _s in got), sorted(self.handles(actor)))
                self.assertEqual({s for _h, s in got} - {"teams"}, set())
                seen += len(got)
        self.assertTrue(seen)

    def test_unset_window_or_other_exclusion_shows_nothing(self):
        import psycopg
        for tag in ("", "another-list"):
            with self.subTest(tag=tag):
                self.assertEqual(self.handles(tct.COS_ONLY, tag), set())
        with mock.patch.object(ts, "_teams_windows", return_value={}):
            self.assertEqual(self.handles(tct.COS_ONLY), set())
        with mock.patch.object(bc, "teams_windows", side_effect=RuntimeError("store")):
            self.assertEqual(self.handles(tct.COS_ONLY), set())
        before = self.handles(tct.COS_ONLY)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            (state,) = pg.execute("SELECT notes FROM biz.state WHERE view = "
                                  "'teams_messages'").fetchone()
            pg.execute("DELETE FROM ontology.text_chunks")
            pg.execute("DELETE FROM ontology.text_fields")
            bc.refresh_changed(pg, "teams_messages", {**state["marks"], "ver": 0, "link": 0})
        # a row whose text is not indexed is no longer withheld from the AI (the text check
        # went with the Freightcom rule, 11-23): the reader's rows stay
        self.assertEqual(self.handles(tct.COS_ONLY), before)
        self.assertTrue(before)

    def test_a_member_excluded_after_the_capture_withholds_the_chat_from_everyone(self):
        env = {**tct.ENV, "PLX_EXCLUDED_PEOPLE_OIDS":
               tct.ENV["PLX_EXCLUDED_PEOPLE_OIDS"] + "," + tct.U_OTHER}
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            people_excluded.build(pg, self.runtime.state_dir, environ=env, workers=2)
        self.runtime.people_exclusion = PeopleExclusion.from_env(env)
        self.tag = people_excluded.current_exclusion(env)
        # the set changed after the table's flags: withheld before the refresh flags them
        # (read from the set) and after it (flagged)
        for refreshed in (False, True):
            if refreshed:
                with psycopg.connect(self.dsn, autocommit=True) as pg:
                    (state,) = pg.execute("SELECT notes FROM biz.state WHERE view = "
                                          "'teams_messages'").fetchone()
                    bc.refresh_changed(pg, "teams_messages", state["marks"])
                    self.assertTrue(pg.execute("SELECT bool_and(excluded) FROM "
                                               "biz.teams_messages").fetchone()[0])
            for actor in READERS:
                with self.subTest(actor=sorted(actor.principals), refreshed=refreshed):
                    self.assertEqual(self.gated(actor), set())
                    self.assertEqual(self.handles(actor), set())

    def door(self, base, actor):
        """The handles ``plx.sql`` returns ``actor`` from ``teams_messages`` over ``base``."""
        tool = ts.SqlTool(base)
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            out = tool.call("plx.sql", {"sql": "SELECT handle FROM teams_messages"}, actor)
        self.assertNotIn("error", out, out)
        return {_cell(r["cells"][0]) for r in out["rows"]}

    def test_the_served_hosts_gate_hands_the_runtime_to_the_door(self):
        """11-18 live: the served host wraps the runtime's gate (``_GatesMixin._item_gate``)
        and the wrapper carried no runtime, so plx.sql showed every reader, the owner
        too, 0 Teams messages of 253,460. Through the wrapper the door shows exactly what
        it shows through the runtime's own gate."""
        from types import SimpleNamespace
        from operating.case_tools_host_gates import _GatesMixin
        from operating.ontology_tools_results import item_gate_from_case_runtime
        inner = item_gate_from_case_runtime(self.runtime)
        host = SimpleNamespace(runtime=self.runtime, mail_capture_dir=None,
                               sharepoint_capture_dir=None)
        wrapped = _GatesMixin._item_gate(host, inner)
        self.assertIs(wrapped.runtime, self.runtime)
        base = self.tool().base
        for actor in READERS:
            with self.subTest(actor=sorted(actor.principals)):
                want = self.handles(actor)
                base.item_gate = wrapped
                self.assertEqual(self.door(base, actor), want)
        self.assertTrue(self.handles(tct.COS_ONLY))
        # a wrapped gate with no runtime under it still shows nothing (fail closed)
        base.item_gate = _GatesMixin._item_gate(host, None)
        self.assertEqual(self.door(base, tct.COS_ONLY), set())

    def test_a_staff_employee_reads_the_chats_they_are_in_whatever_the_live_audience(self):
        """11-18 live: a staff employee's gated actor holds only ``employee:<oid>``; the
        host's live audience grants Teams to staff principals no employee policy grants
        any more (11-17 model), so every staff reader read 0 Teams messages. An employee's
        chat window is their own membership (or full-read grant): the member reads the
        same messages as the gate's member reader; one in no chat and a refused employee
        read none."""
        from ontology.types import Actor
        from operating.employee_permissions import EmployeeEntitlement, EmployeePrincipal
        tenant = tct.ENV["MICROSOFT_GRAPH_TENANT_ID"]
        stranger = "0000aaaa-0000-0000-0000-000000000099"
        ents = {}

        def employee(oid, address, refused=False):
            actor = Actor("plx", frozenset({"employee:" + oid}), "restricted")
            ents[actor] = EmployeeEntitlement(
                principal=EmployeePrincipal(tenant, oid), actor=None if refused else actor,
                denied_reason="denied" if refused else None,
                source_identities=frozenset({oid, address}))
            return actor
        vince = employee(tct.U_VINCE, tct.VINCE)
        nobody = employee(stranger, "nobody@petrasoap.com")
        refused = employee(tct.U_COS, tct.COS, refused=True)
        base = self.tool().base
        everyone = set().union(*(a.principals for a in (*READERS, *ents)))
        base.policy = ttp.ot.ReadPolicy(tenant="plx", audiences={"teams:*": everyone,
                                                                 "api:*": everyone})
        chat = next(iter(bc.teams_windows(self.runtime, tct.VINCE_ONLY)))
        for actor in ents:
            self.assertFalse(self.runtime.live.audience.allows(actor, chat))
        with mock.patch.object(self.runtime.employee_gate, "entitlement",
                               side_effect=lambda actor: ents.get(actor)):
            self.assertEqual(bc.teams_windows(self.runtime, vince),
                             bc.teams_windows(self.runtime, tct.VINCE_ONLY))
            want = self.handles(tct.VINCE_ONLY)
            self.assertTrue(want)
            self.assertEqual(self.door(base, vince), want)
            for actor in (nobody, refused):
                self.assertEqual(bc.teams_windows(self.runtime, actor), {})
                self.assertEqual(self.door(base, actor), set())

    def test_kept_windows_follow_the_store_the_grant_and_the_exclusion(self):
        """A reader's Teams windows are kept between calls only while nothing they were
        decided from moved: a membership change committed to the capture store, another
        entitlement or a reloaded people exclusion decides them again at once."""
        import sqlite3
        from types import SimpleNamespace
        from integrations import capture_teams
        from ontology.types import Actor
        from operating.employee_permissions import EmployeeEntitlement, EmployeePrincipal
        tenant = tct.ENV["MICROSOFT_GRAPH_TENANT_ID"]
        vince = Actor("plx", frozenset({"employee:" + tct.U_VINCE}), "restricted")
        guard = [object()]
        grant = SimpleNamespace(guard=lambda: guard[0],
                                allows_item=lambda scheme, acl, record=None: False)

        def ent(identities):
            return EmployeeEntitlement(principal=EmployeePrincipal(tenant, tct.U_VINCE),
                                       actor=vince, denied_reason=None,
                                       source_identities=frozenset(identities),
                                       full_read=grant)
        now = [ent({tct.U_VINCE, tct.VINCE})]
        gate = self.runtime.employee_gate
        real = capture_teams._history_acl
        decided = []

        def counted(*a, **k):
            decided.append(1)
            return real(*a, **k)
        with mock.patch.object(gate, "entitlement", side_effect=lambda actor: now[0]), \
                mock.patch.object(capture_teams, "_history_acl", side_effect=counted):
            first = bc.teams_windows(self.runtime, vince)
            self.assertEqual(first, bc.teams_windows(self.runtime, tct.VINCE_ONLY))
            self.assertTrue(first)
            decided.clear()
            self.assertEqual(bc.teams_windows(self.runtime, vince), first)
            self.assertEqual(decided, [])                       # kept: nothing moved
            # a reloaded people exclusion (a new guard object): decided again
            guard[0] = object()
            self.assertEqual(bc.teams_windows(self.runtime, vince), first)
            self.assertTrue(decided)
            # another entitlement (no identity in any chat): decided again, nothing
            now[0] = ent({"nobody@petrasoap.com"})
            self.assertEqual(bc.teams_windows(self.runtime, vince), {})
            now[0] = ent({tct.U_VINCE, tct.VINCE})
            self.assertEqual(bc.teams_windows(self.runtime, vince), first)
            # a chat's membership gone from the capture store (another connection's
            # commit): gone from the windows on the very next call
            chat = next(iter(first))
            path = self.runtime.state_dir / capture_teams.STORE_DIR / capture_teams.DB_FILE
            with sqlite3.connect(path) as db:
                gone = db.execute("SELECT * FROM unit_acl WHERE unit = ?", (chat,)).fetchall()
                db.execute("DELETE FROM unit_acl WHERE unit = ?", (chat,))
            db.close()
            after = bc.teams_windows(self.runtime, vince)
            self.assertNotIn(chat, after)
            self.assertEqual(after, {u: w for u, w in first.items() if u != chat})
            # a reopened store connection is decided again whatever its data_version says
            # (a new connection counts from its own start: equal numbers mean nothing)
            self.runtime._drop_capture_read(path)
            with sqlite3.connect(path) as db:
                db.executemany("INSERT INTO unit_acl VALUES (" + ", ".join(
                    "?" * len(gone[0])) + ")", gone)
            db.close()
            key = (str(path), repr(now[0]))
            kept = bc._WINDOWS.get(key)
            self.assertIsNotNone(kept)
            fresh = self.runtime._capture_read(path)._db
            # the new connection's counter made equal to the kept one: only the
            # connection's identity can tell the kept windows are stale
            version = fresh.execute("PRAGMA data_version").fetchone()[0]
            bc._WINDOWS[key] = (kept[0], version, *kept[2:])
            self.assertEqual(bc.teams_windows(self.runtime, vince), first)

    # the plx.table cases of the base class are not this view's
    test_cardinality_1_10_60_by_day_bucket = None
    test_past_the_cap_or_the_time_undecided_messages_are_withheld_never_counted = None
    test_an_unusable_people_guard_serves_no_teams_message = None
    test_a_host_without_the_runtime_gate_is_refused = None
    test_channels_stay_refused = None




if __name__ == "__main__":
    import unittest
    unittest.main()
