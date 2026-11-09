"""Per-person system activity: actor resolution through the directory only, the people
exclusion failing closed, coverage per system, hand-offs and the owner note. SYNTHETIC
stores and ids only; evidence label: isolated-fixture."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from operating import system_activity as sa

TODAY = date(2026, 11, 9)
SINCE = TODAY - timedelta(weeks=26)


def oid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


EXCL = oid(999)


def directory(n: int = 3, aliases=None) -> sa.Directory:
    addresses = {f"user{i}@synth.example": oid(i) for i in range(1, n + 1)}
    addresses["shared.box@synth.example"] = oid(500)
    return sa.Directory(addresses, aliases or {})


def excluded(value: str) -> bool:
    return value == EXCL


def snap(actions, d=None, excl=excluded):
    counts, links = sa.fold(actions, SINCE, TODAY)
    return sa.activity(counts, links, d or directory(), excl, since=SINCE, until=TODAY)


def cover(s, system):
    return next(c for c in s["coverage"] if c["system"] == system)


class Resolution(unittest.TestCase):
    def test_only_directory_identities_and_owner_aliases_attribute(self):
        d = directory(aliases={"sage": {"AB01": oid(2), "ZZ": oid(77)}, "nosuch": {"x": oid(1)}})
        day = TODAY - timedelta(days=3)
        acts = [sa.Action("planner", "task", "created", day, oid(1)),          # object id
                sa.Action("calendar", "meeting", "attended", day, "USER3@synth.example"),
                sa.Action("sage", "gl_je_header", "entered", day, "ab01  "),   # alias file
                sa.Action("sage", "gl_je_header", "entered", day, "ZZ"),       # alias to unlisted id
                sa.Action("sage", "gl_je_header", "entered", day, "user2"),    # local part: candidate
                sa.Action("portal", "batch_record", "tested", day, "Someone"),
                sa.Action("planner", "task", "assigned", day, oid(4242)),      # unlisted id
                sa.Action("bc", "salesOrders", "changed", day, None),
                sa.Action("calendar", "meeting", "attended", day, "buyer@outside.example"),
                sa.Action("calendar", "meeting", "attended", day, "left@synth.example"),
                sa.Action("mission_control", "task_event", "checkout", day, "agent-x",
                          non_person=True)]
        s = snap(acts, d)
        self.assertEqual(sorted(s["people"]), [oid(1), oid(2), oid(3)])
        self.assertEqual(s["people"][oid(2)]["systems"], {"sage": {"entered": 1}})
        sage = cover(s, "sage")
        self.assertEqual((sage["actions"], sage["attributed"], sage["by_alias"],
                          sage["candidate"], sage["unresolved"]), (3, 1, 1, 1, 1))
        self.assertAlmostEqual(sage["share"], 1 / 3, places=3)
        self.assertEqual((cover(s, "planner")["attributed"], cover(s, "planner")["unresolved"]),
                         (1, 1))
        self.assertEqual(cover(s, "bc")["share"], 0.0)
        cal = cover(s, "calendar")   # an outsider is outside the share; an unlisted colleague is in
        self.assertEqual((cal["external"], cal["unresolved"], cal["share"]), (1, 1, 0.5))
        mc = cover(s, "mission_control")
        self.assertEqual((mc["non_person"], mc["share"]), (1, 0.0))
        # an unknown system's alias table and malformed values attribute nothing
        self.assertEqual(d.resolve("sage", ""), (None, "empty"))
        self.assertEqual(d.resolve("nosuch", "x"), (None, "unresolved"))
        self.assertEqual(d.resolve("sage", {"user": oid(1)}), (None, "empty"))

    def test_excluded_person_appears_nowhere_and_an_unavailable_guard_clears_nobody(self):
        d = sa.Directory({"user1@synth.example": oid(1), "gone@synth.example": EXCL},
                         {"sage": {"XX": EXCL}})
        day = TODAY - timedelta(days=2)
        acts = [sa.Action("portal", "purchase_order", "created", day, "gone@synth.example",
                          "PO123"),
                sa.Action("sage", "po_header", "entered", day + timedelta(days=1), "XX", "PO123"),
                sa.Action("bc", "purchaseOrders", "changed", day + timedelta(days=2),
                          "user1@synth.example", "PO123"),
                sa.Action("planner", "task", "created", day, EXCL)]
        s = snap(acts, d)
        text = json.dumps(s)
        self.assertNotIn(EXCL, text)
        self.assertEqual(list(s["people"]), [oid(1)])
        self.assertEqual(cover(s, "portal")["withheld"], 1)
        self.assertEqual(cover(s, "sage")["withheld"], 1)
        self.assertEqual(s["handoffs"], [])   # the excluded steps are in no chain
        self.assertEqual(s["people"][oid(1)]["handoffs_in"], {})
        closed = snap(acts, d, excl=lambda _o: True)
        self.assertEqual(closed["people"], {})
        self.assertEqual(sum(c["attributed"] for c in closed["coverage"]), 0)


class Cardinality(unittest.TestCase):
    def test_every_person_and_action_is_kept_once_at_1_10_60(self):
        systems = ("planner", "calendar", "sharepoint", "mission_control")
        for n in (1, 10, 60):
            d = directory(n)
            acts = []
            for i in range(1, n + 1):
                for k in range(40):
                    acts.append(sa.Action(systems[k % 4], "x", "changed",
                                          TODAY - timedelta(days=k % 180),
                                          f"user{i}@synth.example" if k % 2 else oid(i)))
            started = time.monotonic()
            s = snap(acts, d)
            took = time.monotonic() - started
            self.assertEqual(sorted(s["people"]), sorted(oid(i) for i in range(1, n + 1)))
            for i in range(1, n + 1):
                row = s["people"][oid(i)]
                self.assertEqual(sum(sum(v.values()) for v in row["systems"].values()), 40)
                self.assertEqual(sum(sum(v.values()) for v in row["weeks"].values()), 40)
            self.assertEqual(sum(c["attributed"] for c in s["coverage"]), 40 * n)
            self.assertLess(took, 2.0, n)


class Handoffs(unittest.TestCase):
    def test_a_record_moving_between_systems_and_people_is_one_handoff_per_step(self):
        day = TODAY - timedelta(days=30)
        acts = [sa.Action("portal", "purchase_order", "created", day, "user1@synth.example",
                          "PO777"),
                sa.Action("bc", "purchaseOrders", "changed", day + timedelta(days=1),
                          "user2@synth.example", "PO777"),
                sa.Action("bc", "purchaseOrders", "changed", day + timedelta(days=2),
                          "user2@synth.example", "PO777"),
                sa.Action("sage", "po_receipt_header", "entered", day + timedelta(days=200),
                          None, "PO777"),                        # past the window: no step
                sa.Action("sage", "oe_order_header", "entered", day + timedelta(days=1),
                          None, "PO777")]                        # another workflow
        s = snap(acts)
        self.assertEqual(s["handoffs"], [{"from": "portal", "to": "bc",
                                          "workflow": "procure_to_pay", "n": 1}])
        self.assertEqual(s["people"][oid(1)]["handoffs_out"], {"portal>bc": 1})
        self.assertEqual(s["people"][oid(2)]["handoffs_in"], {"portal>bc": 1})
        self.assertEqual(s["people"][oid(1)]["workflows"], {"procure_to_pay": 1})


class OwnerNote(unittest.TestCase):
    def test_note_names_exactly_the_covered_systems_and_stale_snapshots_say_mail_only(self):
        s = snap([sa.Action("planner", "task", "created", TODAY - timedelta(days=3), oid(1)),
                  sa.Action("planner", "task", "created", TODAY - timedelta(days=60), oid(1)),
                  sa.Action("sage", "gl_je_header", "entered", TODAY, "nobody"),
                  sa.Action("planner", "task", "created", TODAY, EXCL)],
                 sa.Directory({"user1@synth.example": oid(1), "x@synth.example": EXCL}),
                 excl=lambda _o: False)    # built before the person was excluded
        per, coverage, note = sa.owner_columns(s, excluded, today=TODAY, days=28)
        self.assertEqual(per, {oid(1): {"planner": 1}})          # re-checked here; 28 days
        self.assertIn("records created or changed in Planner (100% tied to a person). "
                      "Not tied to people: Sage (", note)
        self.assertIn("Not captured: Business Central, the Portal, SharePoint, "
                      "Mission Control, calendars.", note)
        self.assertIn("undercount", note)
        self.assertEqual({c["system"] for c in coverage if c["actions"]}, {"planner", "sage"})
        old = dict(s, generated_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        for bad in (old, None, {"schema": "other"}, dict(s, generated_at="nonsense")):
            per, coverage, note = sa.owner_columns(bad, excluded, today=TODAY, days=28)
            self.assertEqual((per, coverage), ({}, []))
            self.assertTrue(note.startswith("Covers mail, Teams and tasks only."))


class Extractors(unittest.TestCase):
    """The real store readers on synthetic stores of the captured shapes, read-only."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / "case-runtime").mkdir()
        db = sqlite3.connect(self.state / "case-runtime" / "source-mirror.sqlite")
        db.execute("CREATE TABLE relation_state (relation TEXT, columns_json TEXT)")
        db.execute("CREATE TABLE row_current (relation TEXT, row_json TEXT)")
        recent = (TODAY - timedelta(days=5)).isoformat()
        rels = {"public.app_purchase_order": (["created_at", "created_by", "approved_at",
                                               "approved_by", "updated_at", "po_number"],
                                              [{"created_at": recent + "T10:00:00",
                                                "created_by": "user1@synth.example",
                                                "approved_at": recent, "approved_by": oid(2),
                                                "updated_at": recent, "po_number": "PO-1"}]),
                "public.app_sales_order_line": (["created_at", "updated_at"],
                                                [{"created_at": recent, "updated_at": recent}]),
                "sage.gl_je_header": (["audtdate", "enteredby", "docnumber"],
                                      [{"audtdate": recent.replace("-", ""),
                                        "enteredby": "ADMIN   "}]),
                "sage.gl_account": (["audtdate"], [{"audtdate": "20261101"}])}
        for rel, (cols, rows) in rels.items():
            db.execute("INSERT INTO relation_state VALUES (?, ?)", (rel, json.dumps(cols)))
            for r in rows:
                db.execute("INSERT INTO row_current VALUES (?, ?)", (rel, json.dumps(r)))
        db.commit()
        db.close()
        (self.state / "meetings-capture").mkdir()
        db = sqlite3.connect(self.state / "meetings-capture" / "capture.db")
        db.execute("CREATE TABLE versions (kind TEXT, payload TEXT, superseded_at TEXT)")
        event = {"ical_uid": "u1", "start": recent + "T14:00:00Z", "created_at": recent,
                 "organizer": {"address": "user3@synth.example"},
                 "attendees": [{"address": "user1@synth.example", "response": "accepted"},
                               {"address": "user2@synth.example", "response": "declined"}]}
        for _mailbox in range(2):   # one meeting captured in two mailboxes
            db.execute("INSERT INTO versions VALUES ('calendar_event', ?, NULL)",
                       (json.dumps({"row": event}),))
        task = {"created_at": recent, "created_by": oid(1), "assignees": [oid(2), oid(3)],
                "completed_at": recent, "completed_by": oid(3)}
        db.execute("INSERT INTO versions VALUES ('planner_task', ?, NULL)",
                   (json.dumps({"row": task}),))
        db.commit()
        db.close()
        (self.state / "sharepoint-capture").mkdir()
        db = sqlite3.connect(self.state / "sharepoint-capture" / "capture.db")
        db.execute("CREATE TABLE items (drive_id TEXT, item_id TEXT, record TEXT)")
        up = recent + "T09:00:00Z"
        files = [("d", "f1", {"created_at": up, "last_modified": up,         # an upload
                              "last_modified_by": "user1@synth.example"}),
                 ("d", "f2", {"created_at": "2026-10-01T00:00:00Z", "last_modified": up,
                              "last_modified_by": "user2@synth.example"}),
                 ("d", "f2", {"created_at": "2026-10-01T00:00:00Z", "last_modified": up,
                              "last_modified_by": "user2@synth.example"}),   # same version
                 ("d", "f3", {"created_at": up, "last_modified": recent + "T10:00:00Z"})]
        for drive, item, rec in files:
            db.execute("INSERT INTO items VALUES (?, ?, ?)", (drive, item, json.dumps(rec)))
        db.commit()
        db.close()

    def test_build_reads_each_system_once_and_counts_only_recorded_actions(self):
        counts, links = sa.build(self.state, since=SINCE, until=TODAY, workers=2)
        s = sa.activity(counts, links, directory(), excluded, since=SINCE, until=TODAY)
        by = {c["system"]: c for c in s["coverage"]}
        # Portal: created + approved; updated_at with no modifier column is not a change
        self.assertEqual((by["portal"]["actions"], by["portal"]["attributed"]), (3, 2))
        self.assertEqual(s["people"][oid(2)]["systems"]["portal"], {"approved": 1})
        # Sage: the entered header (named, unresolved) and the account table (not a header)
        self.assertEqual((by["sage"]["actions"], by["sage"]["unresolved"]), (1, 1))
        # one meeting across two mailboxes; a declined attendee did not attend
        self.assertEqual(by["calendar"]["actions"], 2)
        self.assertEqual(s["people"][oid(3)]["systems"]["calendar"], {"organised": 1})
        self.assertNotIn("calendar", s["people"][oid(2)]["systems"])
        self.assertEqual(s["people"][oid(3)]["systems"]["planner"],
                         {"assigned": 1, "completed": 1})
        self.assertEqual(by["planner"]["share"], 1.0)
        # SharePoint: the upload's modifier made it; one change per version; no modifier: nobody
        self.assertEqual(s["people"][oid(1)]["systems"]["sharepoint"], {"created": 1})
        self.assertEqual(s["people"][oid(2)]["systems"]["sharepoint"], {"changed": 1})
        self.assertEqual((by["sharepoint"]["actions"], by["sharepoint"]["unresolved"]), (5, 3))


if __name__ == "__main__":
    unittest.main()
