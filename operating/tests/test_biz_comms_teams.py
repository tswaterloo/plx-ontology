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
from unittest import mock

from integrations.people_exclusion import PeopleExclusion
from integrations.tests import test_capture_teams as tct
from operating import ontology_tools_sql as ts
from operating.pg import business_comms as bc
from operating.pg import business_views as bv
from operating.pg import people_excluded, sql_views
from operating.tests import test_table_teams_parity as ttp

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
            for name in ("0027_item_audiences.sql", "0040_people_excluded_objects.sql",
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
                self.assertEqual(self.ids(got), expected[actor])
                out = self.sql(actor, "SELECT count(*), count(DISTINCT chat) FROM "
                                      "teams_messages")
                self.assertEqual(int(_cell(out["rows"][0]["cells"][0])), len(got))
                self.assertNotIn("Freightcom", json.dumps(out))
        self.assertTrue(self.gated(tct.COS_ONLY) & self.fc())

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
        reader's chats or history window, never Freightcom's."""
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
        for word in ("note", "quote", "freight", "pickup", "freightcom"):
            for actor in READERS:
                with self.subTest(word=word, actor=sorted(actor.principals)):
                    mine = self.handles(actor)
                    want = {h for h in mine if re.search(rf"\b{word}\b", texts.get(h, ""))}
                    self.assertEqual(self.found(actor, word), want)
        self.assertEqual(len(self.found(tct.COS_ONLY, "note")), 60)
        self.assertEqual(len(self.found(tct.VINCE_ONLY, "note")), 60)
        self.assertEqual(self.found(tct.OUTSIDER, "note"), set())
        self.assertEqual(self.found(tct.COS_ONLY, "pickup"), set())
        # vince's window starts after the first message: its words are cos's only
        self.assertLess(len(self.found(tct.VINCE_ONLY, "quote")),
                        len(self.found(tct.COS_ONLY, "quote")))

    def test_a_freightcom_message_reaches_its_readers_pages_never_the_ai(self):
        tool = ts.SqlTool(self.tool().base)
        fc = {self.messages[o] for o in self.fc()}
        with mock.patch.object(ts, "_people_tag", return_value=self.tag):
            for actor in READERS:
                with self.subTest(actor=sorted(actor.principals)):
                    theirs = {self.messages[o] for o in self.gated(actor)} & fc
                    person = {h["citation"] for h in tool.person_search(
                        {"query": "freightcom", "limit": 50}, actor)["documents"]}
                    self.assertEqual(person, theirs)
                    self.assertEqual(self.found(actor, "freightcom"), set())
                    for h in theirs:
                        self.assertEqual(tool.person_open({"citation": h}, actor)["citation"], h)
                        with self.assertRaises(ts.ToolError):
                            tool.comms_open({"citation": h}, actor)
        self.assertTrue(fc & {self.messages[o] for o in self.gated(tct.COS_ONLY)})

    def test_cardinality_1_10_60_by_day(self):
        out = self.sql(tct.COS_ONLY, "SELECT created_date, count(*) FROM teams_messages "
                                     "GROUP BY 1 ORDER BY 1")
        got = {str(_cell(r["cells"][0])): int(_cell(r["cells"][1])) for r in out["rows"]}
        self.assertEqual(set(got), {"2026-09-30", "2026-10-01"}, got)
        self.assertEqual(sum(got.values()), len(self.gated(tct.COS_ONLY) - self.fc()))

    def test_unindexed_text_unset_window_or_other_exclusion_shows_nothing(self):
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
        self.assertEqual(self.handles(tct.COS_ONLY), set(), "unreadable text is withheld")
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

    # the plx.table cases of the base class are not this view's
    test_cardinality_1_10_60_by_day_bucket = None
    test_past_the_cap_or_the_time_undecided_messages_are_withheld_never_counted = None
    test_an_unusable_people_guard_serves_no_teams_message = None
    test_a_host_without_the_runtime_gate_is_refused = None
    test_channels_stay_refused = None




if __name__ == "__main__":
    import unittest
    unittest.main()
