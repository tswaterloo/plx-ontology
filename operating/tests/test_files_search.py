"""SharePoint files as a search-only comms table (``business_comms.FILES``): the parts of
the rule that need no database.

- The guarded view admits a row only inside a search statement (``plx.reader_files`` =
  'search', set by ``SqlTool._comms_search`` alone) and decides Freightcom on the text, or
  on the cells when the file has no text; an unknown scope shows nothing.
- No reader query names the table: it is not in the dictionary, ``describe`` or the
  catalog's type map.
- The search's item gate: a refused file row is never returned, every other row is, in
  order, and the next page starts after the last row read (N = 1, 10, 60; refused rows at
  the page edge never come back and no row is lost or repeated across pages).
- ``plx.search``: a full reader's files come from the comms leg; when the comms leg did not
  search them (any other reader) they go to the documents search as before.

Evidence label: fixture (no database, no network)."""
from __future__ import annotations

import datetime as dt
import unittest

from operating import ontology_tools_search as search
from operating import ontology_tools_sql as ts
from operating.pg import business_comms as bc
from operating.pg import business_views as bv
from operating.pg import comms_search as cs


def _ddl(scope: str | None = None) -> str:
    v = bv.VIEWS["sharepoint_files"]
    if scope is None:
        return bc.view_ddl(v, "biz", "biz_sql")
    import dataclasses
    spec = dataclasses.replace(bc.FILES, scope=scope)
    old = bc.SPECS["sharepoint_files"]
    bc.SPECS["sharepoint_files"] = spec
    try:
        return bc.view_ddl(v, "biz", "biz_sql")
    finally:
        bc.SPECS["sharepoint_files"] = old


class TheRule(unittest.TestCase):
    def test_only_a_search_statement_reads_files(self):
        sql = _ddl()
        self.assertIn("current_setting('plx.reader_files', true) IN ('search', 'full')", sql)
        self.assertIn("t.ai_ok IS NOT FALSE", sql)
        for held in ("t.unit = ANY(", "t.excluded IS FALSE", "plx.reader_hidden"):
            self.assertIn(held, sql)
        # mail keeps its text-only Freightcom rule
        mail = bc.view_ddl(bv.VIEWS["emails"], "biz", "biz_sql")
        self.assertNotIn("cells_ok", mail)
        self.assertNotIn("plx.reader_files", mail)

    def test_an_unknown_scope_shows_nothing(self):
        self.assertTrue(_ddl("drive").rstrip().endswith("AND false"))

    def test_the_setting_is_never_one_a_reader_query_carries(self):
        self.assertNotIn(bc.FILES_SETTING, ts.COMMS_SETTINGS)

    def test_a_full_reader_names_the_table_through_the_door_alone(self):
        """11-22 (A full reader sees every business record): the table is listed
        and described, and the door admits it for ``plx.reader_files`` search (every row
        re-gated afterwards) or full (listed and counted by plx.sql, set only for a full
        reader); it is still not a generic record table."""
        self.assertIn("sharepoint_files", bv.listed())
        self.assertIn("sharepoint_files", bv.dictionary())
        self.assertFalse(bv.VIEWS["sharepoint_files"].covers)
        ddl = _ddl()
        self.assertIn(f"current_setting('{bc.FILES_SETTING}', true) IN ('{bc.FILES_SEARCH}', "
                      f"'{bc.FILES_FULL}')", ddl)
        # no other value of the setting admits a row
        self.assertEqual(ddl.count(bc.FILES_SETTING), 1)

    def test_the_word_index_is_scanned_once_for_every_view(self):
        """11-22: each view's fallback scanned the word index's whole match on its own (0.5 s
        a scan for a stem query, three views). One materialized match serves them all; a
        statement without query words has none."""
        views = ["emails", "teams_messages", "sharepoint_files"]
        sql = cs.search_sql(views, guarded="g", biz="biz", query=True, party_kind=None,
                            since=False, until=False, cursor=False)
        self.assertEqual(sql.count("wm AS MATERIALIZED"), 1)
        self.assertEqual(sql.count("FROM ontology.chunk_words w WHERE w.words @@"), 1)
        # each view's fallback joins that one match (the per-row checks keep the index)
        self.assertGreaterEqual(sql.count("JOIN wm ON wm.chunk_id = c.chunk_id"), len(views))
        plain = cs.search_sql(views, guarded="g", biz="biz", query=False, party_kind=None,
                              since=False, until=False, cursor=False)
        self.assertNotIn("wm", plain.replace("with", ""))

    def test_files_are_searched_never_opened_through_the_table(self):
        sql = cs.search_sql(["emails", "sharepoint_files"], guarded="g", biz="biz",
                            query=True, party_kind=None, since=False, until=False,
                            cursor=False)
        part = sql[sql.index("'sharepoint_files'::text"):]
        self.assertIn("e.modified_at", part)
        # its text only once a build or refresh checked it; its own title, no sender
        self.assertIn("AND b.ai_ok", sql)
        self.assertIn("coalesce(nullif(btrim(e.path), ''), e.name) AS title", sql)

        class PG:
            def execute(self, *_a):
                class R:
                    def fetchall(self):
                        return [("emails",), ("teams_messages",), ("sharepoint_files",)]
                return R()
        self.assertEqual(cs.present(PG(), "g"), ["emails", "teams_messages",
                                                 "sharepoint_files"])
        self.assertEqual(cs.present(PG(), "g", opening=True), ["emails", "teams_messages"])


def _rows(n: int) -> list[tuple]:
    t0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    out = []
    for i in range(n):
        view = "sharepoint_files" if i % 3 else "emails"
        out.append((view, f"h{i:04d}", "t", None, t0 - dt.timedelta(minutes=i), None, None,
                    None, 0, None))
    return out


class _Base:
    def __init__(self, refused):
        self.refused = set(refused)
        self.asked = []
        self.item_gate = object()

    def _item_allowed(self, actor, handle):
        self.asked.append(handle)
        return handle not in self.refused


def _tool(refused) -> ts.SqlTool:
    tool = object.__new__(ts.SqlTool)
    tool.base = _Base(refused)
    return tool


def _pages(every, refused, limit, k):
    """Every page the cursor walks (as the statement reads: the store's rows after the
    cursor's row, ``k`` and the rows it skips at a time). No cursor names a refused
    file."""
    from operating.pg import comms_search as cs
    tool, seen, cursor = _tool(refused), [], None
    for _ in range(len(every) + 2):
        cur = cs.read_cursor(cursor) if cursor else None
        start = 0 if cur is None or cur[2] is None else next(
            i for i, r in enumerate(every) if r[1] == cur[2]) + 1
        read = every[start:start + k + (cur[3] if cur else 0)]
        kept, cursor = tool._gated_files(read, "actor", limit, k + (cur[3] if cur else 0),
                                         cur)
        seen.append([r[1] for r in kept])
        assert not any(h in (cursor or "") for h in refused), cursor
        if cursor is None:
            return seen
        assert cursor, "a run of refused rows stopped the walk"
    raise AssertionError("the walk never ended")


class TheGate(unittest.TestCase):
    def test_refused_files_never_return_and_no_row_is_lost_or_repeated(self):
        # mail between the files, and files only (a run of refused rows longer than a page)
        for n, every in [(n, rows) for n in (1, 10, 60) for rows in (
                _rows(n), [("sharepoint_files",) + r[1:] for r in _rows(n)])]:
            files = [r[1] for r in every if r[0] == "sharepoint_files"]
            for refused in (set(), set(files[::2]), set(files)):
                for limit in (1, 3, 20):
                    k = 2 * limit + 1
                    pages = _pages(every, refused, limit, k)
                    flat = [h for p in pages for h in p]
                    want = [r[1] for r in every if r[1] not in refused]
                    self.assertEqual(flat, want, (n, len(refused), limit))
                    self.assertTrue(all(len(p) <= limit for p in pages))

    def test_mail_rows_are_never_asked_of_the_file_gate(self):
        tool = _tool(set())
        tool._gated_files(_rows(10), "actor", 20, 41)
        self.assertTrue(all(h for h in tool.base.asked))
        self.assertEqual(len(tool.base.asked), len([r for r in _rows(10)
                                                    if r[0] == "sharepoint_files"]))

    def test_meaning_rows_pass_the_file_gate_one_by_one(self):
        """The rows a meaning ranking names (``_comms_widened``): a file row the item gate
        refuses is never added and takes no room; mail rows are never asked of it; rows
        the word search matches are left to its own pages."""
        from unittest import mock
        from operating.search import plug
        t0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        near = []
        for i in range(12):
            view = "sharepoint_files" if i % 2 else "emails"
            near.append((view, f"h{i:02d}", "t", None, t0 - dt.timedelta(minutes=i), None,
                         None, None, 0, None, 0, i == 11, f"o{i:02d}"))
        refused = {"h01", "h03", "h05"}
        tool = _tool(refused)
        tool.base.policy = None
        tool._comms_scoped = lambda conn, settings, work, actor: ([], near)

        class Ranking:
            def result(self, timeout=None):
                return plug.Ranked([{"object_id": r[-1], "handle": None} for r in near],
                                   "hybrid:x")

            def cancel(self):
                return True
        with mock.patch.object(plug, "_gated", side_effect=lambda r, *a: r):
            for room in (3, 5, 20):
                rest: dict = {}
                kept, how = tool._comms_widened(
                    None, {}, "g", "actor", ["emails", "sharepoint_files"], "oral rinse",
                    ["oral", "rinse"], None, None, None, room, {"h00"}, Ranking(), False,
                    rest=rest)
                got = [r[1] for r in kept]
                want = [r[1] for r in near if r[1] not in refused | {"h00", "h11"}]
                self.assertEqual(got, want[:room], room)
                # rows past the room are not dropped: the stream goes on from their place,
                # and its next page is exactly the rows after these
                self.assertEqual("pos" in rest, len(want) > room, room)
                if "pos" in rest:
                    more, _ = tool._comms_widened(
                        None, {}, "g", "actor", ["emails", "sharepoint_files"], "oral rinse",
                        ["oral", "rinse"], None, None, None, 100, set(), Ranking(), False,
                        skip=rest["pos"])
                    self.assertEqual([r[1] for r in more],
                                     [h for h in want[room:]])
                self.assertEqual([h for h, _ in how], ["close in meaning"] * len(got))
                self.assertFalse(set(tool.base.asked) - {r[1] for r in near
                                                         if r[0] == "sharepoint_files"})

    def test_a_reader_who_is_not_full_or_a_host_without_gate_reads_no_file_rows(self):
        tool = _tool(set())
        tool._full = lambda actor: False
        self.assertFalse(tool._files_reader("a"))
        tool._full = lambda actor: True
        self.assertTrue(tool._files_reader("a"))
        tool.base.item_gate = None
        self.assertFalse(tool._files_reader("a"))


def _hit(kind, date):
    return {"citation": f"x://{kind}/{date}", "kind": kind, "title": None, "date": date,
            "snippet": None}


class TheRouting(unittest.TestCase):
    def run_search(self, served):
        calls = {}

        def comms(args):
            calls["comms"] = args["sources"]
            return {"documents": [_hit("email", "2026-11-02")],
                    "searched": {"sources": [s for s in args["sources"] if s in served]},
                    "gaps": [], "completeness": {"complete": True, "truncated": False,
                                                 "returned": 1}}

        def documents(args):
            calls["documents"] = args.get("sources")
            return {"documents": [], "searched": {"sources": args.get("sources") or []},
                    "gaps": [], "completeness": {"complete": True, "truncated": False,
                                                 "returned": 0, "total": 0}}
        out = search.run(documents, {"query": "certificate"}, call_comms=comms)
        return calls, out

    def test_a_full_readers_files_come_from_the_comms_leg(self):
        calls, out = self.run_search({"graph_mail", "teams", "sharepoint"})
        self.assertEqual(calls["comms"], ["graph_mail", "sharepoint", "teams"])
        self.assertEqual(calls["documents"], ["original"])
        self.assertNotIn("error", out)

    def test_files_the_comms_leg_did_not_search_go_to_the_documents_search(self):
        calls, _ = self.run_search({"graph_mail", "teams"})
        self.assertEqual(calls["documents"], ["sharepoint", "original"])


if __name__ == "__main__":
    unittest.main()
