"""The Meetings calendar route's wiring (``operator_api.briefs.upcoming_of``): each meeting
gets the open action items of its series as the signed-in reader may see them (the follow-up
store's own ``carried_items`` decides who sees what); a private event or one whose people are
withheld gets none; no store, or no reader, means no items. SYNTHETIC; isolated-fixture."""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from operating import briefs as br
from operating import meeting_followup as mf
from operator_api import briefs as ob


class View:
    class access:   # noqa: N801 - the shape upcoming_of reads
        @staticmethod
        def grant():
            return None, frozenset({"ops"})

    @contextlib.contextmanager
    def _request(self):
        yield

    def _call(self, tool, args):  # pragma: no cover - br.upcoming_meetings is patched
        raise AssertionError("not read")


EVENTS = [
    {"object_id": "CalendarEvent:x:1", "label": "Ops review", "start": "2026-11-12T15:00:00Z",
     "organizer": {"name": "O", "address": "o@synth.example", "role": "organizer"},
     "private": False,
     "people_withheld": False, "parties": [], "attendees": [], "sources": []},
    {"object_id": "CalendarEvent:x:2", "label": "(no subject)", "start": "2026-11-13T15:00:00Z",
     "organizer": None, "private": True, "people_withheld": False, "parties": [],
     "attendees": [], "sources": []},
    {"object_id": "CalendarEvent:x:3", "label": "Ops review", "start": "2026-11-14T15:00:00Z",
     "organizer": None, "private": False, "people_withheld": True, "parties": [],
     "attendees": [], "sources": []},
]


class Reader:
    email = "me@synth.example"
    session = None


class UpcomingWiring(unittest.TestCase):
    def run_with(self, who, store):
        seen = []

        def carried(st, meeting, viewer):
            seen.append((meeting["title"], meeting["organizer"], viewer))
            return [{"label": "Action item: x", "detail": "d", "date": "2026-11-01",
                     "sources": []}]
        with mock.patch.object(br, "upcoming_meetings", return_value=[dict(e) for e in EVENTS]), \
                mock.patch.object(br, "env_people", return_value=None), \
                mock.patch.object(ob, "_follow_ups", return_value=store), \
                mock.patch.object(mf, "carried_items", side_effect=carried):
            return ob.upcoming_of(View(), who, "/state"), seen

    def test_items_only_for_meetings_whose_people_show_as_the_reader(self):
        raw, seen = self.run_with(Reader(), object())
        self.assertEqual([len(e.get("open_items") or []) for e in raw], [1, 0, 0])
        [(title, organizer, viewer)] = seen
        self.assertEqual((title, organizer["address"]), ("Ops review", "o@synth.example"))
        self.assertEqual(viewer.addresses, frozenset({"me@synth.example"}))
        self.assertEqual(viewer.held, frozenset({"ops"}))
        self.assertFalse(viewer.owner)
        entry = ob.upcoming_entry({**raw[0], "label": raw[0]["label"]})
        self.assertEqual(entry.open_items[0].label, "Action item: x")

    def test_no_store_or_no_reader_means_no_items(self):
        for who, store in ((Reader(), None), (None, object())):
            raw, seen = self.run_with(who, store)
            self.assertEqual(seen, [])
            self.assertFalse(any(e.get("open_items") for e in raw))

    def test_a_reader_without_a_grant_gets_nothing(self):
        class NoGrant(View):
            class access:   # noqa: N801
                grant = None
        self.assertIsNone(ob.upcoming_of(NoGrant(), Reader(), "/state"))
        self.assertEqual(ob.upcoming_model(NoGrant(), Reader(), "/state").meetings, [])


if __name__ == "__main__":
    unittest.main()
