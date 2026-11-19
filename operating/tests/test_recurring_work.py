"""A person's recurring work (``operating.recurring_work``): what recurs is found from their
own actions, people first (an excluded or undecidable colleague's mail never counts; an
unavailable exclusion reads nothing), coverage is measured, and a scheduled job no autopilot
prepares becomes one ``record.due`` event a day once due, for its person only.

SYNTHETIC capture store, people and store rows. Evidence label: ``isolated-fixture``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from integrations.people_exclusion import PeopleExclusion
from operating import autopilot as ap
from operating import recurring_work as rw
from operating.autopilot_queue import matches

ME = ("me@plx.example", "aaaaaaaa-0000-4000-8000-000000000001")
PAL = ("pal@plx.example", "aaaaaaaa-0000-4000-8000-000000000002")
BARRED = ("barred@plx.example", "aaaaaaaa-0000-4000-8000-000000000003")
AS_OF = date(2026, 11, 20)            # a Friday
ET = rw.TZ


def guard(excluded=(BARRED[1],)) -> PeopleExclusion:
    g = PeopleExclusion(list(excluded))
    g.attach_directory([(oid, addr, []) for addr, oid in (ME, PAL, BARRED)])
    return g


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET).astimezone(
        timezone.utc)


def fridays(n: int) -> list[date]:
    return [AS_OF - timedelta(weeks=i) for i in range(n)]


def file_edit(day: date, series: str = "s1", minute: int = 0) -> rw.Act:
    return rw.Act("sharepoint", "edit_file", at(day, 11, 30 + minute), series,
                  handle=f"sharepoint://{series}/{day}", label="Weekly stock.xlsx")


class Recurrence(unittest.TestCase):
    def test_weekly_and_event_jobs_are_found_and_irregular_work_is_not(self) -> None:
        acts = [file_edit(d) for d in fridays(5)]
        # the same file on scattered days and hours: no schedule
        acts += [rw.Act("sharepoint", "edit_file", at(AS_OF - timedelta(days=d), h), "s2")
                 for d, h in ((1, 8), (9, 16), (17, 10), (30, 18))]
        # answers on 5 days: an event job; on 4 days: not yet
        acts += [rw.Act("mission_control", "decide_approval", at(AS_OF - timedelta(days=d), 9),
                        "a", "approval requested", at(AS_OF - timedelta(days=d), 8))
                 for d in range(5)]
        acts += [rw.Act("mail", "reply", at(AS_OF - timedelta(days=d), 9), "r",
                        "mail from vendor.example", at(AS_OF - timedelta(days=d), 7))
                 for d in range(4)]
        jobs = rw.find(acts, AS_OF)
        self.assertEqual(sorted((j["system"], j["trigger"]["type"]) for j in jobs),
                         [("mission_control", "event"), ("sharepoint", "schedule")])
        weekly = next(j for j in jobs if j["system"] == "sharepoint")
        self.assertEqual((weekly["trigger"]["cadence"], weekly["trigger"]["weekday"],
                          weekly["trigger"]["at"], weekly["n"]), ("weekly", 4, "11:30", 5))
        approvals = next(j for j in jobs if j["system"] == "mission_control")
        self.assertEqual((approvals["median_minutes"], approvals["days"]), (60, 5))

    def test_a_meeting_series_on_several_weekdays_keeps_its_calendars_schedule(self) -> None:
        """Vince's two 9:00 series meet on two or three weekdays a week: neither daily nor
        weekly by our rules, yet recurring; the calendar says when the next one is."""
        days = [AS_OF - timedelta(days=d) for d in range(0, 28) if
                (AS_OF - timedelta(days=d)).weekday() in (0, 2)]
        acts = [rw.Act("calendar", "run_meeting", at(d, 9), "m", handle=f"cal/{d}",
                       ref="S1") for d in days]
        (job,) = rw.find(acts, AS_OF)
        self.assertEqual((job["trigger"]["cadence"], job["trigger"]["at"], job["ref"]),
                         ("calendar", "09:00", "S1"))
        self.assertEqual(rw.find(acts[:2], AS_OF), [])        # two meetings: no series yet

    def test_cardinality_every_series_is_one_job_none_lost_or_doubled(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n):
                acts = [file_edit(d, f"s{i}", minute=i % 20) for i in range(n)
                        for d in fridays(4)]
                jobs = rw.find(acts, AS_OF)
                self.assertEqual(sorted(j["series"] for j in jobs),
                                 sorted(f"s{i}" for i in range(n)))
                self.assertTrue(all(j["n"] == 4 for j in jobs))

    def test_a_daily_job_is_due_on_weekdays_from_shortly_before_its_time(self) -> None:
        daily = {"trigger": {"type": "schedule", "cadence": "daily", "at": "09:00"}}
        weekly = {"trigger": {"type": "schedule", "cadence": "weekly", "weekday": 4,
                              "at": "11:30"}}
        monday = date(2026, 11, 16)
        self.assertFalse(rw.due_now(daily, at(monday, 8, 29)))
        self.assertTrue(rw.due_now(daily, at(monday, 8, 30)))
        self.assertFalse(rw.due_now(daily, at(date(2026, 11, 21), 10)))      # Saturday
        self.assertFalse(rw.due_now(weekly, at(monday, 12)))
        self.assertTrue(rw.due_now(weekly, at(AS_OF, 11)))
        for odd in ({"trigger": {"type": "schedule", "cadence": "monthly", "at": "09:00"}},
                    {"trigger": {"type": "event"}}, {}):
            self.assertFalse(rw.due_now(odd, at(monday, 12)), odd)


class SystemFiles(unittest.TestCase):
    def test_a_file_sharepoint_keeps_by_itself_is_nobodys_work(self) -> None:
        """Live dry run: the one weekly "file edit" found for a person was OneDrive's own
        cache of their recent items; the job model rightly proposed nothing. Such files
        are no longer read as work."""
        for rec in ({"name": "__Root__.txt", "path": "/drives/x/root:/PersonalCacheLibrary"},
                    {"name": "a.xlsx", "path": "/sites/s/_catalogs/masterpage"},
                    {"name": "~$Stock.xlsx", "path": "/Shared Documents"},
                    {"name": "AllItems.aspx", "path": "/Lists/Orders/Forms"}):
            self.assertTrue(rw.system_file(rec), rec)
        self.assertFalse(rw.system_file({"name": "Weekly stock.xlsx",
                                         "path": "/Shared Documents/Planning"}))


class Coverage(unittest.TestCase):
    REGISTRY = {"autopilots": [{"id": "teams_reply"}, {"id": "mc_question", "enabled": False},
                               {"id": "meeting_followup"}, {"id": "routine_update"}]}

    def job(self, system, kind, trigger="event", last=AS_OF, **extra):
        return {"system": system, "kind": kind, "trigger": {"type": trigger},
                "last_at": at(last, 9).isoformat(), **extra}

    def test_coverage_is_measured_and_says_what_blocks_it(self) -> None:
        cov = lambda job, prepared=None, blocked=None: rw.coverage(  # noqa: E731
            job, self.REGISTRY, blocked or {}, prepared or {}, AS_OF)
        self.assertTrue(cov(self.job("teams", "reply"), {"teams_reply": 3})["can_prepare"])
        got = cov(self.job("mail", "reply"), blocked={"mail": "refused (403)"})
        self.assertEqual((got["can_prepare"], got["blocked"]), (False, "refused (403)"))
        self.assertIn("nothing prepares", cov(self.job("sharepoint", "edit_file",
                                                       "schedule"))["blocked"])
        self.assertIn("is off", cov(self.job("mission_control", "answer_question"))["blocked"])
        # off in the registry but seen preparing: it does prepare
        self.assertTrue(cov(self.job("mission_control", "answer_question"),
                            {"mc_question": 2})["can_prepare"])
        # happened this week, nothing prepared: blocked, measured
        self.assertIn("prepared nothing", cov(self.job("calendar", "run_meeting",
                                                       "schedule"))["blocked"])
        self.assertTrue(cov(self.job("calendar", "run_meeting", "schedule",
                                     last=AS_OF - timedelta(days=20)))["can_prepare"])
        self.assertIn("colleagues only", cov(self.job("mail", "send", "schedule",
                                                      internal=False),
                                             {"routine_update": 0})["blocked"])


class MailSource(unittest.TestCase):
    def store(self, root: Path, messages) -> None:
        folder = root / "mail-capture"
        folder.mkdir(parents=True)
        conn = sqlite3.connect(folder / "capture.db")
        conn.execute("CREATE TABLE mailboxes (mailbox_id TEXT, upn TEXT)")
        conn.execute("CREATE TABLE folders (mailbox_id TEXT, status TEXT, reason TEXT, "
                     "last_complete_at TEXT)")
        conn.execute("CREATE TABLE messages (mailbox_id TEXT, l1_handle TEXT, record TEXT, "
                     "superseded_at TEXT)")
        conn.execute("INSERT INTO mailboxes VALUES (?, ?)", (ME[1], ME[0]))
        conn.execute("INSERT INTO folders VALUES (?, 'complete', NULL, '2026-11-20T10:00Z')",
                     (ME[1],))
        for i, rec in enumerate(messages):
            conn.execute("INSERT INTO messages VALUES (?, ?, ?, NULL)",
                         (ME[1], f"graph-mail://{ME[1]}/{i}", json.dumps(rec)))
        conn.commit()
        conn.close()

    def test_mail_counts_only_cleared_people_and_groups_replies_by_their_side(self) -> None:
        def mail(day, sender, to, subject, conv, hour=9):
            return {"from": {"address": sender}, "to": [{"address": a} for a in to],
                    "cc": [], "subject": subject, "conversation_id": conv,
                    "sent_at": at(day, hour).isoformat(), "received_at": at(day, hour).isoformat()}
        msgs = []
        for d in range(6):
            day = AS_OF - timedelta(days=d)
            msgs.append(mail(day, "orders@vendor.example", [ME[0]], "PO 7", f"c{d}", 8))
            msgs.append(mail(day, ME[0], ["orders@vendor.example"], "RE: PO 7", f"c{d}", 10))
            # to an excluded colleague, and to an address of ours the directory cannot decide
            msgs.append(mail(day, ME[0], [BARRED[0]], "Weekly numbers for you", f"b{d}"))
            msgs.append(mail(day, ME[0], ["ghost@plx.example"], "Weekly numbers for you",
                             f"g{d}"))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.store(root, msgs)
            g = guard()
            person = rw.person_of(root, ME[0], g)
            acts = rw.mail_acts(str(root), person, "2026-10-01", rw._cleared(g))
            self.assertEqual({(a.kind, a.trigger) for a in acts},
                             {("reply", "mail from vendor.example")})
            self.assertEqual(len(acts), 6)
            found = rw.measure(root, ME[0], as_of=AS_OF, guard=g, workers=1,
                               registry={"autopilots": [{"id": "reply_prep"}]})
            self.assertEqual(found["summary"]["found"], 1)
            # unavailable exclusion or an excluded person: nothing is read
            self.assertEqual(rw.measure(root, ME[0], as_of=AS_OF, guard=PeopleExclusion(
                unavailable="excluded_people_oids_absent"))["status"], "blocked")
            self.assertEqual(rw.measure(root, ME[0], as_of=AS_OF, guard=guard(
                (ME[1],)))["status"], "blocked")
            # the mailbox refused: mail jobs say so
            conn = sqlite3.connect(root / "mail-capture" / "capture.db")
            conn.execute("UPDATE folders SET status = 'incomplete', reason = 'forbidden'")
            conn.commit()
            conn.close()
            self.assertIn("403", rw.mail_blocked(root, person))


class DueEvents(unittest.TestCase):
    class Store:
        def __init__(self, rows):
            self.conn = sqlite3.connect(":memory:")
            self.conn.execute("CREATE TABLE object_versions (object_id TEXT, l1_handle TEXT, "
                              "superseded_at TEXT)")
            self.conn.executemany("INSERT INTO object_versions VALUES (?, ?, NULL)", rows)

    def found(self, *jobs):
        return {"schema": rw.SCHEMA, "status": "built", "as_of": AS_OF.isoformat(),
                "jobs": list(jobs)}

    TODAY = "calendar-event://ev%2F7@g1-42"     # today's occurrence, as captured

    def calendar(self, state: Path, start: datetime, cancelled: bool = False) -> None:
        folder = state / "meetings-capture"
        folder.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(folder / "capture.db")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT, value TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS versions (kind TEXT, payload TEXT, path TEXT, "
                     "capture_id INTEGER, superseded_at TEXT)")
        conn.execute("DELETE FROM meta")
        conn.execute("DELETE FROM versions")
        conn.execute("INSERT INTO meta VALUES ('generation', 'g1')")
        conn.execute("INSERT INTO versions VALUES ('calendar_event', ?, 'ev%2F7', 42, NULL)",
                     (json.dumps({"row": {"series_master_id": "S1", "is_cancelled": cancelled,
                                          "start": start.isoformat()}}),))
        conn.commit()
        conn.close()

    def weekly(self, system="sharepoint", kind="edit_file", autopilots=(), handle="h1",
               ref=None):
        return {"id": f"{system}-{kind}", "system": system, "kind": kind, "n": 5, "days": 5,
                "ref": ref if ref is not None else "S1" if system == "calendar" else "",
                "last_at": at(AS_OF - timedelta(weeks=1), 11).isoformat(),
                "examples": [handle], "label": "Weekly stock.xlsx",
                "trigger": {"type": "schedule", "cadence": "weekly", "weekday": 4,
                            "at": "11:30"},
                "coverage": {"autopilots": list(autopilots)}}

    def test_a_due_uncovered_job_is_one_event_a_day_for_its_person_only(self) -> None:
        calls = []

        def measure(state, owner, as_of):
            calls.append((owner, as_of))
            return self.found(self.weekly(), self.weekly("calendar", "run_meeting",
                                                         ["meeting_followup"], handle="m1"),
                              self.weekly("teams", "post"),
                              self.weekly("planner", "create_task", handle="gone"))
        st = self.Store([("SharePointFile:1", "h1"), ("Meeting:1", self.TODAY)])
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            # no occurrence today (or a cancelled one): no brief; then today's at 11:30
            self.assertEqual(rw.due_events(state, st, [ME[1]], at(AS_OF, 10, 59), measure), [])
            self.calendar(state, at(AS_OF, 11, 30), cancelled=True)
            self.assertEqual(rw.due_events(state, st, [ME[1]], at(AS_OF, 10, 59), measure), [])
            self.calendar(state, at(AS_OF, 11, 30))
            self.assertEqual(rw.due_events(state, st, [ME[1]], at(AS_OF, 10, 29), measure), [])
            # the hour before the meeting it runs: a brief (only then: its follow-up after
            # it is meeting_followup's); the file job from 30 minutes before its time
            early = rw.due_events(state, st, [ME[1]], at(AS_OF, 10, 59), measure)
            self.assertEqual([(e["object_id"], e["fields"].get("brief")) for e in early],
                             [("Meeting:1", True)])
            got = rw.due_events(state, st, [ME[1], ME[1].upper()], at(AS_OF, 11, 5), measure)
            again = rw.due_events(state, st, [ME[1]], at(AS_OF, 15), measure)
            self.assertEqual(len(calls), 1)               # measured once that day
            self.assertEqual([e["object_id"] for e in got], ["SharePointFile:1", "Meeting:1"])
            self.assertEqual([e["object_id"] for e in again], ["SharePointFile:1"])
            self.assertEqual(got[1]["event_id"], early[0]["event_id"])   # once a day
            self.assertEqual(early[0]["handles"], ["m1", self.TODAY])
            self.assertEqual(early[0]["subject"], "Brief for Weekly stock.xlsx at 11:30")
            self.assertEqual(got[0]["event_id"], again[0]["event_id"])
            event = got[0]
            self.assertEqual((event["owner_hint"], event["audience"], event["handles"]),
                             (ME[1], f"mail:{ME[1]}", ["h1"]))
            registry = ap.load_registry()
            hit = [a["id"] for a in registry["autopilots"]
                   if a["on"] == "record.due" and matches(a.get("when") or {}, event)]
            self.assertEqual(hit, ["job_recurring"])
            from operating import workflows as wf
            self.assertEqual(wf.of_item("job_recurring", {}), "routine_update")
            # the next day it is measured again
            rw.due_events(state, st, [ME[1]], at(AS_OF + timedelta(days=7), 11, 5), measure)
            self.assertEqual(len(calls), 2)

    def test_each_person_gets_their_due_work_whatever_the_people_before_them_had(self):
        """Several people in one cycle: one who runs a meeting today (its brief) does not
        change the day the next is measured for, and one whose scan fails costs only their
        own work (counted), never the others' (N = 1, 10, 60 people)."""
        from collections import Counter

        def oid(i):
            return f"aaaaaaaa-0000-4000-8000-{i:012d}"
        for n in (1, 10, 60):
            days = []

            def measure(state, owner, as_of):
                days.append(as_of)
                if owner == oid(n + 1):
                    raise sqlite3.OperationalError("store unreadable")
                runs = int(owner[-12:]) % 2 == 0
                return self.found(self.weekly("calendar", "run_meeting", handle="m1")
                                  if runs else self.weekly(handle=f"h{owner[-12:]}"))
            st = self.Store([("Meeting:1", self.TODAY)] + [
                (f"SharePointFile:{i}", f"h{oid(i)[-12:]}") for i in range(n)])
            failed = Counter()
            with tempfile.TemporaryDirectory() as d:
                self.calendar(Path(d), at(AS_OF, 11, 30))
                got = rw.due_events(Path(d), st, [oid(i) for i in range(n)] + [oid(n + 1)],
                                    at(AS_OF, 11, 5), measure, failed=failed)
            self.assertEqual(set(days), {AS_OF}, n)
            self.assertEqual(len(days), n + 1, n)
            self.assertEqual(failed["recurring_unreadable"], 1, n)
            want = sorted((oid(i), "Meeting:1" if i % 2 == 0 else f"SharePointFile:{i}")
                          for i in range(n))
            self.assertEqual(sorted((e["owner_hint"], e["object_id"]) for e in got), want, n)
            self.assertEqual(len({e["event_id"] for e in got}), n, n)

    def test_a_scheduled_teams_post_is_drafted_into_its_chat_when_due(self) -> None:
        def measure(state, owner, as_of):
            return self.found(self.weekly("teams", "post", handle="t1", ref="19:c@thread.v2"),
                              self.weekly("teams", "post", handle="t2", ref=""))   # channel
        st = self.Store([("TeamsChatMessage:1", "t1"), ("TeamsChatMessage:2", "t2")])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rw.due_events(Path(d), st, [ME[1]], at(AS_OF, 10, 59), measure),
                             [])
            (event,) = rw.due_events(Path(d), st, [ME[1]], at(AS_OF, 11, 5), measure)
        self.assertEqual((event["object_id"], event["fields"]["post_to"],
                          event["fields"]["brief"]),
                         ("TeamsChatMessage:1", "19:c@thread.v2", True))

    def test_a_scan_that_could_not_be_built_is_tried_again_hourly_not_kept_all_day(self):
        calls = []

        def measure(state, owner, as_of):
            calls.append(as_of)
            return {"schema": rw.SCHEMA, "status": "blocked" if len(calls) < 2 else "built",
                    "as_of": as_of.isoformat(), "jobs": []}
        st = self.Store([])
        with tempfile.TemporaryDirectory() as d:
            for minutes, made in ((0, 1), (30, 1), (61, 2), (200, 2)):
                rw.due_events(Path(d), st, [ME[1]], at(AS_OF, 8) + timedelta(minutes=minutes),
                              measure)
                self.assertEqual(len(calls), made, minutes)


if __name__ == "__main__":
    unittest.main()
