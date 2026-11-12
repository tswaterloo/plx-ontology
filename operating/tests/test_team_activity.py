"""The Team view's snapshot built from synthetic captures of the real shapes (mail, Teams,
calendars, the per-system snapshot): counts per person and day, reply times, names, roles,
partners, the people exclusion failing closed, and 1/10/60 people. SYNTHETIC ids, names and
addresses only; evidence label: isolated-fixture."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from operating import system_activity as sa
from operating import team_activity as ta

NOW = datetime.now(timezone.utc).replace(microsecond=0)
UNTIL = NOW.astimezone(ta.TZ).date()
DOMAIN = "synth.example"


def oid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def addr(n: int) -> str:
    return f"user{n}@{DOMAIN}"


EXCL = oid(999)


class Guard:
    """A bound people exclusion: decides every id, excludes ``EXCL``."""
    unavailable = None

    def decides(self, value):
        return True

    def is_excluded(self, value):
        return value in (EXCL, "gone@" + DOMAIN)


class World:
    def __init__(self, n: int = 3) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        (self.state / "mail-capture").mkdir()
        self.mail = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        self.mail.executescript(
            "CREATE TABLE mailboxes (mailbox_id TEXT PRIMARY KEY, upn TEXT, identities TEXT, "
            "resources TEXT, source_acl TEXT, first_captured_at TEXT, identity_checked_at TEXT);"
            "CREATE TABLE messages (version_id INTEGER PRIMARY KEY AUTOINCREMENT, mailbox_id TEXT, "
            "record TEXT, recorded_at TEXT, superseded_at TEXT, removed_reason TEXT);"
            "CREATE TABLE mailbox_directory (mailbox_id TEXT PRIMARY KEY, department TEXT, "
            "listed_at TEXT);"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE tenant_listing (address TEXT PRIMARY KEY, object_id TEXT, kind TEXT);")
        for i in [*range(1, n + 1), 999]:
            a = "gone@" + DOMAIN if i == 999 else addr(i)
            self.mail.execute("INSERT INTO mailboxes VALUES (?, ?, ?, '[]', '{}', ?, ?)",
                              (oid(i), a, json.dumps([a]), NOW.isoformat(), NOW.isoformat()))
        self.mail.execute("INSERT INTO meta VALUES ('tenant_listing_at', ?)", (NOW.isoformat(),))
        self.mail.execute("INSERT INTO tenant_listing VALUES (?, NULL, 'group')",
                          ("sales@" + DOMAIN,))
        (self.state / "teams-capture").mkdir()
        self.teams = sqlite3.connect(self.state / "teams-capture" / "capture.db")
        self.teams.execute("CREATE TABLE versions (capture_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                           "kind TEXT, payload TEXT, recorded_at TEXT, superseded_at TEXT)")
        (self.state / "meetings-capture").mkdir()
        self.meet = sqlite3.connect(self.state / "meetings-capture" / "capture.db")
        self.meet.execute("CREATE TABLE versions (kind TEXT, payload TEXT, superseded_at TEXT)")
        self.n = 0

    def message(self, mailbox: int, frm: str, to: list[str], *, ago: timedelta, conv="c1",
                mid=None, cc=()) -> None:
        self.n += 1
        at = (NOW - ago).isoformat().replace("+00:00", "Z")
        self.mail.execute("INSERT INTO messages (mailbox_id, record, recorded_at) VALUES (?, ?, ?)",
                          (oid(mailbox), json.dumps({
                              "from": frm, "to": to, "cc": list(cc), "sent_at": at,
                              "conversation_id": conv, "internet_message_id": mid or f"m{self.n}",
                              "subject": "SECRET SUBJECT"}), NOW.isoformat()))

    def chat(self, sender: str, chat: str, *, ago: timedelta, name=None) -> None:
        self.teams.execute("INSERT INTO versions (kind, payload, recorded_at) VALUES "
                           "('teams_chat', ?, ?)", (json.dumps({"row": {
                               "from": {"id": sender, "display_name": name, "kind": "user"},
                               "created_at": (NOW - ago).isoformat(), "chat_id": chat,
                               "message_type": "message", "body_text": "SECRET BODY"}}),
                               NOW.isoformat()))

    def build(self, *, system=None, roles=None, guard=Guard()) -> dict:
        for db in (self.mail, self.teams, self.meet):
            db.commit()
        return ta.build(self.state, until=UNTIL, system=system, roles=roles or {},
                        people=guard, workers=2)


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.tmp.cleanup)

    def test_counts_names_replies_partners_and_the_systems_days(self):
        w, h = self.w, timedelta(hours=1)
        outside = "buyer@customer.example"
        # user1 is asked by an outsider and answers 30 minutes later, then again 90 minutes
        w.message(1, outside, [addr(1)], ago=10 * h, conv="a")
        w.message(1, addr(1), [outside], ago=10 * h - timedelta(minutes=30), conv="a")
        w.message(1, outside, [addr(1)], ago=6 * h, conv="b")
        w.message(1, addr(1), [outside], ago=6 * h - timedelta(minutes=90), conv="b")
        # one internal mail held in both mailboxes counts once
        w.message(1, addr(1), [addr(2)], ago=3 * h, mid="shared", cc=["sales@" + DOMAIN])
        w.message(2, addr(1), [addr(2)], ago=3 * h, mid="shared", cc=["sales@" + DOMAIN])
        w.message(2, "no-reply@vendor.example", [addr(2)], ago=2 * h)   # automated: not "received"
        w.message(1, addr(1), [outside], ago=timedelta(days=60))         # before the window
        w.chat(oid(2), "chat-1", ago=h, name="Bea Okafor")
        w.chat(oid(2), "chat-1", ago=2 * h, name="Bea Okafor")
        w.chat(oid(3), "chat-1", ago=h, name="Cal Mendes")
        w.chat(oid(77), "chat-1", ago=h, name="Guest Person")               # not in the directory
        w.meet.execute("INSERT INTO versions VALUES ('calendar_event', ?, NULL)", (json.dumps({
            "row": {"organizer": {"address": addr(1), "name": "Avery Lindqvist"},
                    "attendees": []}}),))
        day = UNTIL.isoformat()
        system = {"schema": sa.SCHEMA, "generated_at": NOW.isoformat(), "coverage": [
            {"system": "planner", "share": 1.0, "actions": 3}],
            "people": {oid(3): {"days": {day: {"planner": 3, "nonsense": 9}}},
                       EXCL: {"days": {day: {"planner": 5}}}}}
        s = w.build(system=system, roles={oid(1): ["purchasing"]})
        self.assertEqual(s["status"], "built")
        p1, p2, p3 = (s["people"][oid(i)] for i in (1, 2, 3))
        self.assertEqual((p1["name"], p1["name_from"]), ("Avery Lindqvist", "calendar"))
        self.assertEqual((p2["name"], p2["name_from"]), ("Bea Okafor", "teams"))
        self.assertEqual(p1["roles"], ["purchasing"])
        total = lambda p, ch: sum(c.get(ch, 0) for c in p["days"].values())
        self.assertEqual((total(p1, "mail_sent"), total(p1, "mail_received")), (3, 2))
        self.assertEqual((total(p2, "mail_received"), total(p2, "teams")), (1, 2))
        self.assertEqual(total(p3, "planner"), 3)
        self.assertEqual(p1["reply"]["7"], 60)                   # median of 30 and 90 minutes
        self.assertEqual(p1["reply_n"]["28"], 2)
        self.assertEqual(sum(p1["hours"]), 3)
        self.assertEqual(p1["with"], {oid(2): 1})                 # the shared mail, once
        self.assertEqual(p2["with"], {oid(3): 2, oid(1): 1})      # two messages in their chat
        self.assertNotIn(oid(77), s["people"])
        self.assertEqual(s["coverage"], [{"system": "planner", "share": 1.0, "actions": 3}])
        text = json.dumps(s)
        for leak in ("SECRET", "customer.example", EXCL, "Guest"):
            self.assertNotIn(leak, text)
        # a stale per-system snapshot adds nothing
        old = dict(system, generated_at=(NOW - timedelta(days=30)).isoformat())
        self.assertEqual(total(w.build(system=old)["people"].get(oid(3), {"days": {}}),
                               "planner"), 0)

    def test_excluded_people_and_undecidable_colleagues_fail_closed(self):
        w, h = self.w, timedelta(hours=1)
        w.message(1, addr(1), [addr(2), "gone@" + DOMAIN], ago=h)          # names the excluded
        w.message(1, addr(1), [addr(2), "stranger@" + DOMAIN], ago=h)      # undecidable colleague
        w.message(999, "gone@" + DOMAIN, [addr(1)], ago=h)                # their mailbox
        w.message(1, addr(1), [addr(2)], ago=h)                           # the one counted
        w.chat(EXCL, "chat-x", ago=h, name="Gone Person")
        w.chat(oid(1), "chat-x", ago=h, name="Avery Lindqvist")           # a chat with them
        w.chat(oid(1), "chat-y", ago=h, name="Avery Lindqvist")
        s = w.build()
        self.assertEqual(sum(c.get("mail_sent", 0) for c in s["people"][oid(1)]["days"].values()), 1)
        self.assertEqual(sum(c.get("teams", 0) for c in s["people"][oid(1)]["days"].values()), 1)
        self.assertNotIn(EXCL, json.dumps(s))
        self.assertNotIn("Gone", json.dumps(s))
        self.assertEqual(s["withheld"], {"mail": 2, "mail_mailbox": 1, "teams": 2})
        for guard in (None, mock.Mock(unavailable="no_list")):
            blocked = w.build(guard=guard)
            self.assertEqual((blocked["status"], blocked["people"]), ("blocked", {}))

    def test_one_ten_sixty_people_each_counted_once(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World(n)
                self.addCleanup(w.tmp.cleanup)
                for i in range(1, n + 1):
                    for k in range(5):
                        w.message(i, addr(i), ["x@customer.example"], ago=timedelta(hours=k + 1))
                    w.chat(oid(i), f"chat-{i}", ago=timedelta(hours=1), name=f"Person {i}")
                started = time.monotonic()
                s = w.build()
                self.assertLess(time.monotonic() - started, 20)
                self.assertEqual(sorted(s["people"]), sorted(oid(i) for i in range(1, n + 1)))
                for i in range(1, n + 1):
                    days = s["people"][oid(i)]["days"].values()
                    self.assertEqual(sum(c.get("mail_sent", 0) for c in days), 5)
                    self.assertEqual(s["people"][oid(i)]["name"], f"Person {i}")


class SystemDays(unittest.TestCase):
    def test_the_last_days_are_kept_per_day_and_channel_and_the_weeks_still_add_up(self):
        until = date(2026, 11, 9)
        d = sa.Directory({addr(1): oid(1)})
        acts = [sa.Action("mission_control", "approval", "decided", until, addr(1)),
                sa.Action("mission_control", "task_event", "question.asked", until, addr(1)),
                sa.Action("mission_control", "task_event", "task.completed", until, addr(1)),
                sa.Action("calendar", "meeting", "attended", until - timedelta(days=1), addr(1)),
                sa.Action("planner", "task", "created", until - timedelta(days=90), oid(1))]
        counts, links = sa.fold(acts, until - timedelta(weeks=26), until)
        s = sa.activity(counts, links, d, lambda _o: False, since=until - timedelta(weeks=26),
                        until=until)
        row = s["people"][oid(1)]
        self.assertEqual(row["days"], {
            (until - timedelta(days=1)).isoformat(): {"meetings": 1},
            until.isoformat(): {"mc_approvals": 1, "mc_questions": 1, "mc_tasks": 1}})
        self.assertEqual(sum(sum(v.values()) for v in row["weeks"].values()), 5)


if __name__ == "__main__":
    unittest.main()
