"""Meeting briefs prepared ahead and kept (``operator_api/meeting_briefs.py``, ``briefs.
meeting_brief_of``): built whole with no time budget, kept per reader and grant, drawn from
the store while every source still opens, refreshed on request, and the person's own notes
added at draw time, never kept in the brief. Evidence label: ``isolated-fixture``."""
from __future__ import annotations

import contextlib
import tempfile
import unittest
from unittest.mock import patch

from operator_api import briefs, meeting_briefs as mb
from operating import briefs as br

OID = "CalendarEvent:graph.c1"
RAW = {"kind": "meeting", "title": "T", "as_of": "2026-11-24T12:00:00Z", "basis": "b",
       "subject": {"object_id": OID, "label": "Sync"}, "sections": [
           {"key": "threads", "title": "Mail", "state": "shown", "rows": [
               {"label": "x", "sources": [{"handle": "graph-mail://a/1@1"}]}], "more": 0,
            "figures": []}], "parties": [], "gaps": []}


class View:
    _call = None
    @contextlib.contextmanager
    def _request(self):
        yield


def build(tmp, calls, decide=lambda hs: {h: None for h in hs}, grant="g1", **kw):
    store = mb.MeetingBriefStore(tmp)

    def fake(call, held, alerts, oid, **k):
        calls.append(k)
        return dict(RAW)
    with patch.object(briefs, "_held", return_value=frozenset()), \
            patch.object(briefs, "_decide", return_value=decide), \
            patch.object(briefs, "_grant_key", return_value=grant), \
            patch.object(briefs, "_alerts", return_value={}), \
            patch.object(briefs, "_people", return_value=None), \
            patch.object(br, "meeting_brief", fake):
        return briefs.meeting_brief_of(View(), OID, None, None, store=store, reader="r1", **kw)


class Kept(unittest.TestCase):
    def test_built_whole_then_drawn_from_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            first = build(tmp, calls)
            self.assertIsNone(calls[0]["budget_seconds"])      # no time budget
            self.assertNotIn("kept", first)
            second = build(tmp, calls)
            self.assertEqual(len(calls), 1)                    # not built again
            self.assertTrue(second["kept"])
            self.assertIn("stored_at", second)
            build(tmp, calls, refresh=True)
            self.assertEqual(len(calls), 2)

    def test_a_changed_grant_or_a_source_no_longer_open_rebuilds(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            build(tmp, calls)
            build(tmp, calls, grant="g2")
            self.assertEqual(len(calls), 2)
            build(tmp, calls, grant="g2", decide=lambda hs: {h: {"reason": "x"} for h in hs})
            self.assertEqual(len(calls), 3)
            build(tmp, calls, grant="g2", decide=lambda hs: {})   # undecided: not served
            self.assertEqual(len(calls), 4)

    def test_another_reader_never_draws_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            build(tmp, [])
            store = mb.MeetingBriefStore(tmp)
            self.assertIsNone(store.get("r2", OID, "g1", lambda hs: {h: None for h in hs}))
            self.assertIsNotNone(store.get("r1", OID, "g1", lambda hs: {h: None for h in hs}))

    def test_an_unreadable_file_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = mb.MeetingBriefStore(tmp)
            store.put("r1", OID, "g1", RAW)
            store._path("r1", OID).write_text("{not json")
            self.assertIsNone(store.get("r1", OID, "g1", lambda hs: {}))


class Notes(unittest.TestCase):
    def test_only_notes_naming_the_meeting_or_its_parties(self):
        items = [{"text": "A", "at": "2026-11-20T10:00:00Z", "records": [OID]},
                 {"text": "B", "at": "x", "records": ["Customer:c:9"]},
                 {"text": "C", "at": "x", "records": []}]
        sec = mb.notes_section(items, {OID})
        self.assertEqual([r["label"] for r in sec["rows"]], ["A"])
        self.assertEqual(sec["rows"][0]["date"], "2026-11-20")
        empty = mb.notes_section([], {OID})
        self.assertEqual(empty["state"], "empty")
        self.assertTrue(empty["note"])


if __name__ == "__main__":
    unittest.main()


class AskedFromABrief(unittest.TestCase):
    def _brief(self):
        from operator_api import models as m
        return m.Brief(kind="meeting", title="Sync with Azelis", as_of="2026-11-24T12:00:00Z",
                       basis="b", sections=[
                           m.BriefSection(key="your_notes", title="Your notes", state="shown",
                                          rows=[m.BriefRow(label="They asked for net 60",
                                                           who="Your note")]),
                           m.BriefSection(key="po", title="Open orders", state="shown",
                                          more=3, rows=[m.BriefRow(
                                              label="PO 1", sources=[m.Source(
                                                  system="Email", handle="graph-mail://a/1@1",
                                                  label="m")])])])

    def test_the_brief_goes_in_as_context_with_the_notes_and_the_handles(self):
        from types import SimpleNamespace as NS
        from operator_api import brief_context as bc

        class Backend:
            def meeting_brief(self, reader, t, i):
                return AskedFromABrief._brief(self_)
        self_ = self
        ctx = NS(kind="brief", object_type="CalendarEvent", object_id=OID)
        got = bc.lines(Backend(), None, NS(context=ctx))
        self.assertEqual(len(got), 1)
        for needle in ("They asked for net 60", "graph-mail://a/1@1", "3 more rows exist"):
            self.assertIn(needle, got[0])
        self.assertEqual(bc.lines(Backend(), None, NS(context=NS(kind="page"))), [])
        self.assertEqual(bc.lines(Backend(), None, NS(context=None)), [])

    def test_a_brief_that_cannot_be_read_is_said_not_dropped(self):
        from types import SimpleNamespace as NS
        from operator_api import brief_context as bc

        class Backend:
            def meeting_brief(self, *a):
                raise RuntimeError("down")
        got = bc.lines(Backend(), None, NS(context=NS(kind="brief", object_type="Customer",
                                                      object_id="Customer:x:1")))
        self.assertIn("could not be read", got[0])


class DoorMeetingBriefRouting(unittest.TestCase):
    def test_uses_fast_postgres_door_when_available(self):
        from types import SimpleNamespace as NS
        from operating import meeting_brief_door as mbd
        from operator_api.errors import UserFacing

        class FakeBackend(briefs.BriefsMixin):
            doors = NS(get=lambda r: None)
            memory = None
            state_dir = None

            def _brief_run(self, *a, **k):
                raise AssertionError("slow worker should not run when door succeeds")

        door_raw = dict(RAW, sections=[
            {"key": "notes", "title": "Last meeting: notes and action items",
             "state": "not_available", "note": mbd.NO_TRANSCRIPT_NOTE,
             "rows": [], "more": 0, "figures": []},
        ])
        reader = NS(key="r1", email="a@plx.test", session=NS(principals=(), identities=()))
        with patch.object(mbd, "build_for_reader", return_value=door_raw):
            out = FakeBackend().meeting_brief(reader, "CalendarEvent", OID)
        self.assertEqual(out.sections[0].state, "not_available")
        self.assertEqual(out.sections[0].note, mbd.NO_TRANSCRIPT_NOTE)

        # BriefRefused from door MUST raise 404 (never fall back to leak!)
        with patch.object(mbd, "build_for_reader", side_effect=br.BriefRefused("not_found")):
            with self.assertRaises(UserFacing) as ctx:
                FakeBackend().meeting_brief(reader, "CalendarEvent", OID)
        self.assertEqual(ctx.exception.status, 404)

    def test_falls_back_when_door_unavailable(self):
        from types import SimpleNamespace as NS
        from operating import meeting_brief_door as mbd

        called = []

        class FakeBackend(briefs.BriefsMixin):
            doors = NS(get=lambda r: None)
            memory = None
            state_dir = None
            data_dir = None

            def _brief_run(self, reader, job, **k):
                called.append(True)
                return dict(RAW)

        reader = NS(key="r1", email="a@plx.test", session=NS(principals=(), identities=()))
        with patch.object(mbd, "build_for_reader", side_effect=mbd.DoorUnavailable("not ready")):
            out = FakeBackend().meeting_brief(reader, "CalendarEvent", OID)
        self.assertEqual(called, [True])
        self.assertEqual(out.title, "T")

    def test_with_carried_items_upgrades_not_available_notes_to_shown(self):
        from types import SimpleNamespace as NS
        from operating import meeting_followup as mf

        brief = dict(RAW, sections=[
            {"key": "notes", "title": "Last meeting: notes and action items",
             "state": "not_available", "note": "No transcript captured yet; brief built from mail, Teams, and open records.",
             "rows": [], "more": 0, "figures": []},
        ])
        who = NS(email="a@plx.test", session=NS(principals=(), identities=()))
        with patch.object(briefs, "_follow_ups", return_value=object()), \
                patch.object(mf, "carried_items", return_value=[{"label": "Send COA", "sources": []}]):
            out = briefs.with_carried_items(brief, frozenset(), who, "/tmp")
        self.assertEqual(out["sections"][0]["state"], "shown")
        self.assertIsNone(out["sections"][0]["note"])
        self.assertEqual(len(out["sections"][0]["rows"]), 1)
