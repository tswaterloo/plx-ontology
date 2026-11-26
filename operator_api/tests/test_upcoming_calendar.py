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


class CalendarNotReadTests(unittest.TestCase):
    """Owner walk 11-17 P4: the Meetings page said "No meetings today or in the next two
    weeks" while 195 events were captured for those days. A calendar search that fails, or
    finds nothing before its time runs out, is never an empty calendar."""

    def view(self, answer):
        class Failing(View):
            def _call(self, tool, args):
                return answer(tool, args)
        return Failing()

    def test_a_failed_search_is_no_empty_calendar(self):
        for answer in (lambda t, a: {"error": {"code": "query_timeout", "message": "slow"}},
                       lambda t, a: {"error": {"code": "unknown_new_code"}}):
            with mock.patch.object(br, "env_people", return_value=None):
                with self.assertRaises(br.CalendarNotRead):
                    ob.upcoming_of(self.view(answer), Reader(), None)

    def test_a_search_out_of_time_before_any_hit_is_no_empty_calendar(self):
        with mock.patch.object(br, "Reads") as reads:
            reads.return_value.cited.find.return_value = []
            reads.return_value.cited.over.return_value = True
            with self.assertRaises(br.CalendarNotRead):
                br.upcoming_meetings(lambda t, a: {}, frozenset(), people=None)
            reads.return_value.cited.over.return_value = False
            self.assertEqual(br.upcoming_meetings(lambda t, a: {}, frozenset(), people=None),
                             [])   # a finished search that found nothing: truly none

    def test_the_route_says_try_again_never_no_meetings(self):
        from operator_api.errors import UserFacing
        view = self.view(lambda t, a: {"error": {"code": "query_timeout"}})
        backend = mock.Mock()
        backend._brief_run = lambda reader, job: job(view)
        backend._upcoming_cached = lambda v, who: ob.upcoming_model(v, who, None)
        with mock.patch.object(br, "env_people", return_value=None), \
                self.assertRaises(UserFacing) as raised:
            ob.BriefsMixin.upcoming_meetings(backend, Reader())
        self.assertEqual((raised.exception.code, raised.exception.status), ("busy", 503))
        self.assertIn("could not be read", raised.exception.message)


class MeetingPeopleDecided(unittest.TestCase):
    """Owner walk 11-17: the owner's Meetings page said "No meetings today or in the next two
    weeks" over 81 events: the calendar read's people guard (the passes' ``env_people``, no
    directory) could decide no address, so every event naming anyone was dropped. The route
    reads with the API's read-path guard: a colleague's address decided by the mail
    capture's directory, an outsider's by the tenant's mail domains; an excluded person's
    still excluded, an unknown address at the company's domain still withheld. Only the
    reader's own meetings are asked for (their address and object ids)."""

    EXCLUDED = "00000000-0000-4000-8000-0000000000e1"
    COLLEAGUE = "00000000-0000-4000-8000-0000000000c1"

    def people_for(self, guard):
        from operator_api import feedback
        seen: dict = {}

        def upcoming(call, held, **kw):
            seen.update(kw)
            return []
        with mock.patch.object(feedback, "exclusion", return_value=guard), \
                mock.patch.object(br, "upcoming_meetings", side_effect=upcoming):
            ob.upcoming_of(View(), Reader(), None)
        return seen

    def test_addresses_are_decided_as_the_other_read_paths_do(self):
        from integrations.people_exclusion import PeopleExclusion
        guard = PeopleExclusion([self.EXCLUDED])
        guard.attach_directory([
            (self.EXCLUDED, "gone@synth-home.example", ["gone@synth-home.example"]),
            (self.COLLEAGUE, "col@synth-home.example", ["col@synth-home.example"])])
        seen = self.people_for(guard)
        people = seen["people"]
        self.assertIsNone(br.people_unavailable(people))
        self.assertFalse(people.is_excluded("col@synth-home.example"))
        self.assertTrue(people.is_excluded("Gone@synth-home.example"))
        self.assertTrue(people.is_excluded(self.EXCLUDED))
        self.assertFalse(people.is_excluded("buyer@synth-foods.example"))   # an outsider
        self.assertTrue(people.is_excluded("stranger@synth-home.example"))  # withheld
        self.assertEqual(people.known_domains(), frozenset({"synth-home.example"}))
        self.assertIn("me@synth.example", seen["reader"])

    def test_no_directory_or_no_guard_withholds_never_shows(self):
        from integrations.people_exclusion import PeopleExclusion
        # object ids alone: no address can be told from a colleague's (withheld)
        people = self.people_for(PeopleExclusion([self.EXCLUDED]))["people"]
        self.assertTrue(people.is_excluded("col@synth-home.example"))
        self.assertEqual(br.people_unavailable(self.people_for(None)["people"]),
                         "exclusion_not_loaded")
        down = self.people_for(PeopleExclusion(unavailable="excluded_people_oids_absent"))
        self.assertEqual(br.people_unavailable(down["people"]), "excluded_people_oids_absent")


class ChildProcessesDecidePeople(unittest.TestCase):
    """Live 11-18: the owner's Meetings page still said "No meetings today or in the next two
    weeks" after the fix above, while his own request read his two Weekly catchups. The
    warm-up process (and the section pool) never attached the mail capture's directory the
    API attaches as it starts, so there every colleague's address was withheld, every
    meeting naming one dropped, and that empty page, kept on disk under the owner's key,
    was served to him. Each process the API spawns decides people as the API does."""

    EXCLUDED = MeetingPeopleDecided.EXCLUDED
    COLLEAGUE = MeetingPeopleDecided.COLLEAGUE
    CAPTURE = "/synth/state"

    class Built(Exception):
        pass

    def setUp(self):
        from operator_api import feedback
        feedback.attach_capture_directory(None)
        self.addCleanup(feedback.attach_capture_directory, None)

    def decided_in(self, child, *args):
        """Run ``child``'s serve up to building its backend; there, decide a colleague's,
        an excluded person's and an unknown company address as the briefs do."""
        import os
        from integrations import people_exclusion as pe
        from integrations.people_exclusion import PeopleExclusion
        from operating import parent_watch
        from operating.pg import permissions
        from operator_api import backend as be
        from operator_api.workspace import case_console
        seen = {}

        def directory(state_dir, **_):
            if str(state_dir) != self.CAPTURE:
                return None
            return [(self.EXCLUDED, "gone@synth-home.example", ["gone@synth-home.example"]),
                    (self.COLLEAGUE, "col@synth-home.example", ["col@synth-home.example"])]

        def build(*_a, **_k):
            people = ob._people()
            seen.update({a: people.is_excluded(a) for a in (
                "col@synth-home.example", "gone@synth-home.example",
                "stranger@synth-home.example")})
            raise self.Built()

        def main(argv, serve_with=None, fixtures=None, **_):
            return serve_with({"objects_for": None, "mail_capture_dir": self.CAPTURE})
        with mock.patch.object(permissions, "load_exclusion",
                               return_value=PeopleExclusion([self.EXCLUDED])), \
                mock.patch.object(pe, "mail_capture_directory", side_effect=directory), \
                mock.patch.object(pe, "mail_capture_listing", return_value=None), \
                mock.patch.object(parent_watch, "die_with"), \
                mock.patch.object(parent_watch, "spawned_parent"), \
                mock.patch.object(os, "nice", return_value=19), \
                mock.patch("logging.basicConfig"), \
                mock.patch.object(case_console, "main", side_effect=main), \
                mock.patch.object(be, "WorkspaceBackend", side_effect=build), \
                self.assertRaises(self.Built):
            child(*args)
        return seen

    def test_warm_up_and_section_pool_decide_colleagues_as_the_api_does(self):
        from operator_api import section_pool, warm_process
        for child, args in ((warm_process.child, (None, [], {}, "dev")),
                            (section_pool.child, (None, [], {}))):
            with self.subTest(child=child.__module__):
                self.assertEqual(self.decided_in(child, *args), {
                    "col@synth-home.example": False,      # a colleague: their meeting shows
                    "gone@synth-home.example": True,      # an excluded person: still hidden
                    "stranger@synth-home.example": True})  # unknown at home: withheld


class UpcomingMeetingsPgTests(unittest.TestCase):
    """Fast indexed Postgres ``biz.meetings`` path: enforces audience_keys, rec_marks,
    excluded, is_cancelled, reader ownership, (ical_uid, start_at) dedupe, and people
    exclusion; returns None for fallback when biz.meetings is absent."""

    def test_pg_fast_path_filters_dedupes_and_falls_back(self):
        from datetime import datetime, timezone
        from integrations.people_exclusion import PeopleExclusion

        guard = PeopleExclusion(["00000000-0000-4000-8000-0000000000e1"])
        guard.attach_directory([
            ("00000000-0000-4000-8000-0000000000e1", "gone@synth.example", ["gone@synth.example"]),
            ("00000000-0000-4000-8000-0000000000c1", "me@synth.example", ["me@synth.example"]),
            ("00000000-0000-4000-8000-0000000000c2", "col@synth.example", ["col@synth.example"]),
        ])
        start = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)
        end = datetime(2026, 11, 27, 15, 30, tzinfo=timezone.utc)
        meeting_rows = [
            # 1) Copy in colleague's mailbox for same ical_uid (deduped in favor of reader's copy)
            ("h-col", ["CalendarEvent:x:col"], start, end, "Ops sync", "col@synth.example",
             ["me@synth.example"], "https://teams.microsoft.com/l/meetup-join/abc", False, True,
             False, False, "uid-1", "col-box", ["meetings:calendar:col-box"], ["[]"], [], []),
            # 2) Reader's own mailbox copy (wins dedupe)
            ("h-me", ["CalendarEvent:x:me"], start, end, "Ops sync", "col@synth.example",
             ["me@synth.example"], "https://teams.microsoft.com/l/meetup-join/abc", False, True,
             False, False, "uid-1", "me@synth.example", ["meetings:calendar:me"], ["[]"], [], []),
            # 3) Unreadable audience key (filtered out)
            ("h-deny", ["CalendarEvent:x:deny"], start, end, "Secret sync", "me@synth.example",
             [], None, False, False, True, False, "uid-2", "me@synth.example",
             ["meetings:calendar:other"], ["[]"], [], []),
            # 4) Unheld marking (filtered out)
            ("h-hr", ["CalendarEvent:x:hr"], start, end, "HR sync", "me@synth.example",
             [], None, False, False, True, False, "uid-3", "me@synth.example",
             ["meetings:calendar:me"], ['["hr"]'], [], []),
            # 5) Excluded attendee (filtered out)
            ("h-excl", ["CalendarEvent:x:excl"], start, end, "With excluded", "me@synth.example",
             ["gone@synth.example"], None, False, False, True, False, "uid-4", "me@synth.example",
             ["meetings:calendar:me"], ["[]"], [], []),
        ]

        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows
            def fetchone(self):
                return self._rows[0] if self._rows else None
            def fetchall(self):
                return list(self._rows)

        class FakePg:
            def __init__(self, exists=True):
                self.exists = exists
            def execute(self, sql, params=None):
                if "to_regclass('biz.meetings')" in sql:
                    return FakeCursor([(not self.exists,)])
                if "FROM biz.meetings" in sql:
                    return FakeCursor(meeting_rows)
                return FakeCursor([(None,)])

        # Missing table -> returns None so caller falls back to ontology path
        self.assertIsNone(br.upcoming_meetings_pg(
            FakePg(exists=False), frozenset({"ops"}),
            may_read=lambda k: True, people=guard,
            now=datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc),
            reader={"me@synth.example"},
        ))
        got = br.upcoming_meetings_pg(
            FakePg(exists=True), frozenset({"ops"}),
            may_read=lambda k: k in ("meetings:calendar:col-box", "meetings:calendar:me"),
            people=guard,
            now=datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc),
            reader={"me@synth.example"},
        )
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["object_id"], "CalendarEvent:x:me")
        self.assertEqual(got[0]["label"], "Ops sync")
        self.assertTrue(got[0]["online"])
        self.assertEqual(got[0]["join_url"], "https://teams.microsoft.com/l/meetup-join/abc")
        self.assertEqual(got[0]["organizer"]["address"], "col@synth.example")
        self.assertEqual([a["address"] for a in got[0]["attendees"]], ["me@synth.example"])


class UpcomingFastPathAndCacheTests(unittest.TestCase):
    """Fast PG path bypasses self._brief_run, maintains 60s fresh / 30m stale fallback
    cache, and READ_WORKERS is raised to 6."""

    def test_read_workers_is_six(self):
        from operator_api import serving
        self.assertEqual(serving.READ_WORKERS, 6)

    def test_fast_pg_path_bypasses_brief_run_and_caches_with_stale_fallback(self):
        from datetime import datetime, timezone
        from operator_api import models as m
        from operating.pg.access_classes import FULL_KEY

        class FakeReader:
            key = "alex-oid"
            email = "alex@synth.example"
            mode = "default"
            session = None

        class FakeGrant:
            keys = (FULL_KEY,)
            held = frozenset({"ops"})

        class FakeDoors:
            def get(self, _r):
                return (type("D", (), {"grant": FakeGrant()})(), "dsn", {})

        class FakeConn:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def transaction(self):
                return self
            def execute(self, sql, params=None):
                return self

        class FakePool:
            def connection(self):
                return FakeConn()

        calls = {"pg": 0, "brief_run": 0}

        def fake_pg_meetings(*a, **kw):
            calls["pg"] += 1
            return [{
                "object_id": "CalendarEvent:pg:1",
                "label": "Weekly ops",
                "start": "2026-11-26T15:00:00Z",
                "end": "2026-11-26T15:30:00Z",
                "all_day": False,
                "location": None,
                "online": True,
                "join_url": None,
                "agenda": None,
                "organizer": None,
                "attendees": [],
                "is_organizer": True,
                "private": False,
                "people_withheld": False,
                "parties": [],
                "open_items": [],
                "sources": [],
            }]

        class Host(ob.BriefsMixin):
            pg_dsn = "postgresql:///synth"
            state_dir = None
            doors = FakeDoors()
            def pg(self):
                return FakePool()
            def _brief_run(self, *a, **kw):
                calls["brief_run"] += 1
                raise br.CalendarNotRead("busy")
            def prepare_day_ahead(self, *a, **kw):
                pass

        host = Host()
        who = FakeReader()
        with mock.patch.object(br, "upcoming_meetings_pg", side_effect=fake_pg_meetings), \
                mock.patch.object(ob, "_people", return_value=None):
            first = host.upcoming_meetings(who)
            second = host.upcoming_meetings(who)
        self.assertEqual(len(first.meetings), 1)
        self.assertEqual(first.meetings[0].object.label, "Weekly ops")
        self.assertEqual(second.meetings[0].object.label, "Weekly ops")
        # Direct PG called once, second hit served from 60s cache, _brief_run never touched
        self.assertEqual(calls["pg"], 1)
        self.assertEqual(calls["brief_run"], 0)

        # Age cache past 60s fresh window (<30m stale window): transient PG + brief_run failure
        # serves stale cached meetings instead of 503
        lock, cache = host._upcoming_cache_store()
        with lock:
            for k, (ts, st, mdl) in list(cache.items()):
                cache[k] = (ts - 120.0, st, mdl)
        with mock.patch.object(br, "upcoming_meetings_pg", side_effect=RuntimeError("pg blip")), \
                mock.patch.object(ob, "_people", return_value=None):
            stale_ok = host.upcoming_meetings(who)
        self.assertEqual(len(stale_ok.meetings), 1)
        self.assertEqual(stale_ok.meetings[0].object.label, "Weekly ops")

    def test_calendar_auto_responses_and_cos_relay_filtered_from_replies(self):
        for subj in (
            "Accepted: Weekly sync",
            "Declined: Planning review",
            "Tentative: Supplier call",
            "Canceled: Standup",
            "Invitation: Q4 budget",
        ):
            self.assertTrue(ob._is_reply_noise(subject=subj), subj)
        self.assertTrue(ob._is_reply_noise(sender="cos@petrasoap.com", subject="Staging alert"))
        self.assertFalse(ob._is_reply_noise(sender="alex@supplier.example", subject="Need ETA for PO 500"))


if __name__ == "__main__":
    unittest.main()
