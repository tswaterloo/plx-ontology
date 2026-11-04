"""Meetings capture over an injected Graph transport (evidence: injected-transport).

No network and no credentials: an in-process fake Graph holds a small tenant (two cleared
users, one excluded person, an external organizer), their calendars, online meetings with
transcripts and Planner plans. Covers the boundary the capture must hold: GET only through
the closed grammar; the people guard fails closed before any request and refuses an
excluded person's mailbox, events, tasks and meetings before their content is requested
(only a count is kept); incremental reads by change key and etag; a refused online-meeting
read is a blocked part naming its permission; a transcript naming an unlisted speaker is
withheld whole; every event survives paging at N=1/10/60; and a reader the audience does
not grant sees nothing.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from integrations import capture_meetings as cm
from integrations import graph_read
from integrations.live_source_guard import LiveSourceAudience
from ontology.types import Actor

# The fixture's meetings sit two days before now (the live-check argv reads with the real
# clock and a 30-day window).
NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
A_ID, B_ID = "0000aaaa-0000-4000-8000-0000000000a1", "0000bbbb-0000-4000-8000-0000000000b1"
X_ID = "0e0e0e0e-0000-4000-8000-00000000e0e1"   # the excluded person
A, B, X = "ann.test@example-plx.com", "bob.test@example-plx.com", "xena.test@example-plx.com"
EXTERNAL = "guest@vendor-example.com"
GROUP = "0000cccc-0000-4000-8000-0000000000c1"
ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": X_ID,
       "AZURE_TENANT_ID": "11111111-2222-3333-4444-555555555555",
       "AZURE_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
       "AZURE_CLIENT_SECRET": "s3cr3t~AZURE~value~do-not-leak"}
CAPTURE = Actor("plx", frozenset({cm.CAPTURE_PRINCIPAL}), "restricted")
MEMBER = Actor("plx", frozenset({"staff:plan-member"}), "restricted")
WITHHELD = Actor("plx", frozenset({"staff:meetings-withheld"}), "restricted")


def audience() -> LiveSourceAudience:
    return LiveSourceAudience(
        {"mail:*": [cm.CAPTURE_PRINCIPAL], "teams:planner:*": [cm.CAPTURE_PRINCIPAL,
                                                               "staff:plan-member"]},
        tenant="plx", identities={"staff:plan-member": [f"group:{GROUP}"]})


def j(status, value):
    return status, {}, json.dumps(value).encode()


def who(address, name):
    return {"emailAddress": {"address": address, "name": name}}


def join_url(n):
    return f"https://teams.microsoft.com/l/meetup-join/19%3ameeting_M{n}%40thread.v2/0"


def user(oid, address, name):
    return {"id": oid, "mail": address, "userPrincipalName": address, "displayName": name,
            "proxyAddresses": [f"SMTP:{address}"], "userType": "Member", "department": "Ops"}


VTT = ("WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\n<v Ann Test>Shipping PO PO100235 "
       "today.</v>\n\n00:00:05.000 --> 00:00:07.500\n<v Bob Test>Vendor Acme confirmed.</v>\n")


class FakeGraph:
    """A small tenant's directory, calendars, online meetings and Planner."""

    def __init__(self) -> None:
        self.users = {A_ID: user(A_ID, A, "Ann Test"), B_ID: user(B_ID, B, "Bob Test"),
                      X_ID: user(X_ID, X, "Xena Test")}
        self.events: dict[str, dict[str, dict]] = {A_ID: {}, B_ID: {}}
        self.meetings: dict[str, dict] = {}       # join url -> meeting
        self.transcripts: dict[str, list[dict]] = {}
        self.vtt: dict[str, str] = {}
        self.plans = {"PlanOne_000000000000000001": {"title": "Ops board", "group": GROUP}}
        self.tasks: dict[str, dict] = {}
        self.forbidden: list[str] = []
        self.calls: list[tuple[str, str]] = []
        past = NOW - timedelta(days=2)
        self.event(A_ID, "EvA1", organizer=(A, "Ann Test"), attendees=[(B, "Bob Test")],
                   start=past, online=1, organizer_flag=True, subject="Weekly PO review")
        self.event(A_ID, "EvA2", organizer=(X, "Xena Test"), attendees=[(A, "Ann Test")],
                   start=past, online=2, subject="excluded organizer")
        self.event(A_ID, "EvA3", organizer=(A, "Ann Test"), attendees=[(X, "Xena Test")],
                   start=past, online=3, subject="excluded attendee")
        self.event(A_ID, "EvA4", organizer=(A, "Ann Test"), attendees=[(B, "Bob Test")],
                   start=past,
                   online=4, sensitivity="private", subject="private matter")
        self.event(B_ID, "EvB1", organizer=(A, "Ann Test"), attendees=[(B, "Bob Test")],
                   start=past, online=1, subject="Weekly PO review")
        self.meeting(1, "MeetingOne", ["TranscriptOne"])
        self.meeting(3, "MeetingThree", ["TranscriptThree"])
        self.task("TaskAnn_00000000000000001", assignees=[A_ID], title="Chase PO100235")
        self.task("TaskXena_0000000000000001", assignees=[X_ID], title="excluded assignee")
        self.task("TaskMade_0000000000000001", assignees=[B_ID], creator=X_ID,
                  title="excluded creator")

    def event(self, box, eid, *, organizer, attendees, start, online=None, subject="",
              sensitivity="normal", organizer_flag=False, change="ck1"):
        self.events[box][eid] = {
            "id": eid, "iCalUId": f"ical-{eid if not online else online}", "changeKey": change,
            "subject": subject, "bodyPreview": f"body of {subject}",
            "location": {"displayName": "Room 1"}, "webLink": "https://outlook.example/x",
            "organizer": who(*organizer),
            "attendees": [{**who(*a), "type": "required",
                           "status": {"response": "accepted"}} for a in attendees],
            "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
                      "timeZone": "UTC"},
            "end": {"dateTime": (start + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"},
            "isAllDay": False, "isCancelled": False, "isOrganizer": organizer_flag,
            "isOnlineMeeting": bool(online), "sensitivity": sensitivity, "type": "singleInstance",
            "onlineMeeting": {"joinUrl": join_url(online)} if online else None,
            "onlineMeetingProvider": "teamsForBusiness" if online else "unknown",
            "createdDateTime": "2026-10-01T10:00:00Z",
            "lastModifiedDateTime": "2026-10-02T10:00:00Z"}

    def meeting(self, n, mid, transcripts):
        self.meetings[join_url(n)] = {"id": mid, "joinWebUrl": join_url(n)}
        held = (NOW - timedelta(days=2, minutes=-5)).isoformat()   # in the fixture meeting
        self.transcripts[mid] = [{"id": t, "createdDateTime": held, "endDateTime": held}
                                 for t in transcripts]
        for t in transcripts:
            self.vtt[t] = VTT

    def task(self, tid, *, assignees, title, creator=A_ID, etag='W/"1"'):
        self.tasks[tid] = {"id": tid, "planId": "PlanOne_000000000000000001",
                           "bucketId": "BucketOne_00000000000000001", "title": title,
                           "percentComplete": 50, "priority": 5,
                           "dueDateTime": "2026-11-10T00:00:00Z",
                           "createdDateTime": "2026-10-20T00:00:00Z",
                           "createdBy": {"user": {"id": creator}},
                           "assignments": {a: {} for a in assignees}, "@odata.etag": etag}

    def gets(self, pattern: str) -> list[str]:
        return [url for method, url in self.calls if method == "GET"
                and re.search(pattern, unquote(url))]

    def _page(self, parts, items):
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        top, skip = int(query.get("$top", "1000")), int(query.get("$skip", "0"))
        page = {"value": items[skip:skip + top]}
        if skip + top < len(items):
            rest = {k: v for k, v in query.items() if k != "$skip"}
            page["@odata.nextLink"] = (f"https://graph.microsoft.com{parts.path}?"
                                       + urlencode({**rest, "$skip": skip + top}))
        return j(200, page)

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": "eyJ.TOKEN-do-not-leak.sig", "expires_in": 3599})
        assert method == "GET", method
        path = unquote(parts.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if any(re.search(p, path) for p in self.forbidden):
            return j(403, {"error": {"code": "Forbidden"}})
        if path == "/v1.0/users":
            return self._page(parts, list(self.users.values()))
        found = re.fullmatch(r"/v1\.0/users/([^/]+)(/.*)?", path)
        if found:
            ref, rest = found[1].lower(), found[2] or ""
            box = next((u for u in self.users.values() if ref in (u["id"], u["mail"])), None)
            if box is None:
                return j(404, {"error": {"code": "Request_ResourceNotFound"}})
            if not rest:
                return j(200, {k: box[k] for k in ("id", "mail", "userPrincipalName",
                                                   "proxyAddresses")})
            events = self.events.get(box["id"], {})
            if rest == "/calendarView":
                fields = query["$select"].split(",")
                listed = [{k: e[k] for k in fields if k in e} for e in events.values()]
                return self._page(parts, sorted(listed, key=lambda e: e["id"]))
            got = re.fullmatch(r"/events/([^/]+)", rest)
            if got and got[1] in events:
                fields = query["$select"].split(",")
                return j(200, {k: events[got[1]][k] for k in fields if k in events[got[1]]})
            if rest == "/onlineMeetings":
                url_given = re.fullmatch(r"JoinWebUrl eq '(.*)'", query["$filter"])[1]
                meeting = self.meetings.get(url_given.replace("''", "'"))
                return j(200, {"value": [meeting] if meeting else []})
            got = re.fullmatch(r"/onlineMeetings/([^/]+)/transcripts(/([^/]+)/content)?", rest)
            if got and got[2]:
                return 200, {"Content-Type": "text/vtt"}, self.vtt[got[3]].encode()
            if got:
                return j(200, {"value": self.transcripts.get(got[1], [])})
            if rest == "/planner/tasks":
                mine = [t for t in self.tasks.values() if box["id"] in t["assignments"]]
                return self._page(parts, mine)
            return j(404, {"error": {"code": "itemNotFound"}})
        got = re.fullmatch(r"/v1\.0/planner/plans/([^/]+)(/buckets|/tasks)?", path)
        if got and got[1] in self.plans:
            plan = self.plans[got[1]]
            if got[2] == "/buckets":
                return j(200, {"value": [{"id": "BucketOne_00000000000000001",
                                          "name": "Doing", "planId": got[1]}]})
            if got[2] == "/tasks":
                return self._page(parts, list(self.tasks.values()))
            return j(200, {"id": got[1], "title": plan["title"],
                           "container": {"containerId": plan["group"], "type": "group"}})
        got = re.fullmatch(r"/v1\.0/planner/tasks/([^/]+)/details", path)
        if got and got[1] in self.tasks:
            return j(200, {"id": got[1], "description": "Vendor Acme owes the CoA",
                           "checklist": {"a": {"title": "Call vendor", "isChecked": True}},
                           "references": {"https%3A//x": {"alias": "PO100235 sheet",
                                                          "type": "Excel"}}})
        return j(404, {"error": {"code": "itemNotFound"}})


def scope(**overrides) -> cm.MeetingsScope:
    return cm.MeetingsScope({"schema": cm.SCOPE_SCHEMA, "mailboxes": [A, B, X],
                             "page_size": 2, **overrides})


class MeetingsCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.graph = FakeGraph()
        self.store = cm.open_store(self.state, audience())
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)

    def capture(self, env=ENV, **scope_overrides):
        api, transport = cm.build_api(env, sender=self.graph)
        capture = cm.MeetingsCapture(self.store, api, audience(), CAPTURE, workers=2,
                                     clock=lambda: NOW)
        return capture.run(scope(**scope_overrides)), transport

    def rows(self, kind):
        return {r.path: r.payload for unit in self.store.units(kind.name)
                for r in self.store.current_records(kind.name, unit)}

    def test_excluded_people_never_requested_and_withheld_whole(self) -> None:
        report, _ = self.capture()
        self.assertEqual(report["status"], "complete", report["reasons"])
        counts = report["counts"]
        # The excluded mailbox is refused before its lookup; nothing names it but the
        # directory listing, which names nobody.
        named = [u for m, u in self.graph.calls if m == "GET" and unquote(u).split("?")[0]
                 != "https://graph.microsoft.com/v1.0/users"
                 and (X_ID in unquote(u).lower() or X in unquote(u).lower())]
        self.assertEqual(named, [])
        self.assertEqual(counts["mailboxes_excluded"], 1)
        # Events naming the excluded person: no content read, no meeting lookup, no record.
        self.assertEqual(self.graph.gets(r"/events/EvA[23]"), [])
        self.assertEqual(self.graph.gets(r"meeting_M[23]"), [])
        self.assertEqual(counts["events_withheld"], 2)
        events = self.rows(cm.EVENT_KIND)
        self.assertEqual({p[1] for p in events}, {"EvA1", "EvA4", "EvB1"})
        held = json.dumps(list(events.values()))
        self.assertNotIn("excluded", held)
        self.assertNotIn(X, held)
        # Planner: a task assigned to or created by the excluded person is withheld and its
        # details are never requested.
        self.assertEqual(self.graph.gets(r"/planner/tasks/Task(Xena|Made)"), [])
        self.assertEqual(counts["tasks_withheld"], 2)
        self.assertEqual({p[1] for p in self.rows(cm.TASK_KIND)}, {"TaskAnn_00000000000000001"})

    def test_task_without_details_is_not_read_for_them(self) -> None:
        self.graph.task("TaskBare_0000000000000001", assignees=[A_ID], title="no details")
        self.graph.tasks["TaskBare_0000000000000001"].update(
            hasDescription=False, checklistItemCount=0, referenceCount=0)
        self.capture(parts=["planner"])
        self.assertEqual(self.graph.gets(r"/planner/tasks/TaskBare"), [])
        rows = {p[1]: v["row"] for p, v in self.rows(cm.TASK_KIND).items()}
        self.assertEqual(rows["TaskBare_0000000000000001"]["title"], "no details")
        self.assertEqual(rows["TaskBare_0000000000000001"]["checklist"], [])

    def test_directory_mailboxes_less_the_excluded(self) -> None:
        report, _ = self.capture(mailboxes=[], all_mailboxes=True, parts=["calendar"])
        self.assertEqual(report["status"], "complete", report["reasons"])
        self.assertEqual((report["counts"]["mailboxes_open"],
                          report["counts"]["mailboxes_excluded"]), (2, 1))
        self.assertEqual(self.graph.gets(X_ID), [])

    def test_rows_content_and_sensitive_events(self) -> None:
        self.capture()
        events = {p[1]: v["row"] for p, v in self.rows(cm.EVENT_KIND).items()}
        one = events["EvA1"]
        self.assertEqual(one["subject"], "Weekly PO review")
        self.assertEqual(one["start"], (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(one["attendees"][0]["address"], B)
        self.assertEqual(one["online_meeting"]["join_url"], join_url(1))
        # A private event is kept without content, and its content is never requested.
        self.assertEqual(events["EvA4"]["content"], "withheld_sensitivity")
        self.assertIsNone(events["EvA4"]["subject"])
        self.assertEqual((events["EvA4"]["attendees"], events["EvA4"]["categories"]), ([], []))
        self.assertEqual(self.graph.gets(r"/events/EvA4"), [])
        # ... nor is its online meeting looked up.
        self.assertEqual(self.graph.gets(r"meeting_M4"), [])
        # Listing reads identity fields only.
        for listing in self.graph.gets(r"/calendarView"):
            self.assertNotIn("subject", parse_qs(urlsplit(listing).query)["$select"][0])
        task = next(iter(self.rows(cm.TASK_KIND).values()))
        self.assertEqual(task["row"]["bucket_name"], "Doing")
        self.assertEqual(task["row"]["checklist"], [{"title": "Call vendor", "checked": True}])
        self.assertNotIn("https", json.dumps(task["row"]["references"]))
        self.assertIn(f"group:{GROUP}", task["meta"]["source_acl"]["identities"])

    def test_transcript_with_speakers_and_unknown_speaker_withheld(self) -> None:
        report, _ = self.capture()
        transcripts = list(self.rows(cm.TRANSCRIPT_KIND).values())
        self.assertEqual(len(transcripts), 1)   # one meeting, though on two calendars
        row = transcripts[0]["row"]
        self.assertEqual(row["speakers"], ["Ann Test", "Bob Test"])
        self.assertEqual(row["cues"][0], {"start": "00:00:01.000", "end": "00:00:04.000",
                                          "speaker": "Ann Test",
                                          "text": "Shipping PO PO100235 today."})
        self.assertIn("[00:00:01] Ann Test: Shipping PO PO100235", row["content_text"])
        # Checked once per join URL: a second run does not look it up again.
        before = len(self.graph.gets(r"/onlineMeetings$|/onlineMeetings\?"))
        self.capture()
        self.assertEqual(len(self.graph.gets(r"/onlineMeetings$|/onlineMeetings\?")), before)
        # A speaker the invitation did not list withholds the transcript whole.
        fresh = FakeGraph()
        fresh.vtt["TranscriptOne"] = VTT + "\n00:00:09.000 --> 00:00:10.000\n<v Stranger>hi</v>\n"
        self.graph = fresh
        self.store.close()
        self.store = cm.open_store(Path(self.tmp.name) / "other", audience())
        report, _ = self.capture()
        self.assertEqual(report["counts"]["transcripts_withheld_unknown_speaker"], 1)
        self.assertEqual(self.rows(cm.TRANSCRIPT_KIND), {})

    def test_series_transcripts_match_their_own_occurrence_and_later_ones_are_read(
            self) -> None:
        # A weekly series: one join URL, two occurrences 9 and 2 days ago, each with its
        # own attendees; a third transcript appears after a later occurrence ends.
        for eid, days, attendee in (("EvS1", 9, (B, "Bob Test")), ("EvS2", 2, None)):
            self.graph.event(A_ID, eid, organizer=(A, "Ann Test"),
                             attendees=[attendee] if attendee else [],
                             start=NOW - timedelta(days=days), online=7, organizer_flag=True,
                             subject=f"Standup {days}")
            self.graph.events[A_ID][eid]["type"] = "occurrence"
            self.graph.events[A_ID][eid]["iCalUId"] = f"ical-{eid}"
        self.graph.meeting(7, "MeetingSeven", ["TrWeek1", "TrWeek2"])
        listed = self.graph.transcripts["MeetingSeven"]
        listed[0]["createdDateTime"] = (NOW - timedelta(days=9, minutes=-5)).isoformat()
        listed[1]["createdDateTime"] = (NOW - timedelta(days=2, minutes=-5)).isoformat()
        self.graph.vtt["TrWeek2"] = VTT.split("\n\n00:00:05")[0] + "\n"   # Ann only
        self.capture()
        rows = {r["row"]["id"]: r for r in self.rows(cm.TRANSCRIPT_KIND).values()
                if r["row"]["meeting_id"] == "MeetingSeven"}
        self.assertEqual({t: rows[t]["row"]["subject"] for t in rows},
                         {"TrWeek1": "Standup 9", "TrWeek2": "Standup 2"})
        # access follows the occurrence: Bob attended week 1 only
        self.assertIn(B, rows["TrWeek1"]["meta"]["source_acl"]["identities"])
        self.assertNotIn(B, rows["TrWeek2"]["meta"]["source_acl"]["identities"])
        # A later occurrence ends: the join URL is due again and only the new one is read.
        later = NOW + timedelta(days=5)
        self.graph.event(A_ID, "EvS3", organizer=(A, "Ann Test"), attendees=[],
                         start=later - timedelta(hours=2), online=7, organizer_flag=True,
                         subject="Standup next")
        self.graph.events[A_ID]["EvS3"]["iCalUId"] = "ical-EvS3"
        listed.append({"id": "TrWeek3", "createdDateTime": (later - timedelta(hours=2))
                       .isoformat(), "endDateTime": later.isoformat()})
        self.graph.vtt["TrWeek3"] = self.graph.vtt["TrWeek2"]
        api, _ = cm.build_api(ENV, sender=self.graph)
        cm.MeetingsCapture(self.store, api, audience(), CAPTURE, clock=lambda: later
                           ).run(scope(window_days_back=30))
        rows = {r["row"]["id"]: r["row"] for r in self.rows(cm.TRANSCRIPT_KIND).values()}
        self.assertEqual(rows["TrWeek3"]["subject"], "Standup next")
        self.assertEqual(len(self.graph.gets(r"/transcripts/TrWeek1/content")), 1)
        # a transcript no cleared occurrence holds is withheld whole
        listed.append({"id": "TrOrphan", "createdDateTime": (NOW - timedelta(days=20))
                       .isoformat()})
        report = cm.MeetingsCapture(self.store, api, audience(), CAPTURE,
                                    clock=lambda: later + timedelta(days=2)).run(
            scope(window_days_back=30))
        self.assertNotIn("TrOrphan", {p[2] for p in self.rows(cm.TRANSCRIPT_KIND)})

    def test_hr_organizer_is_withheld(self) -> None:
        self.graph.event(A_ID, "EvHr", organizer=("payroll@example-plx.com", "Payroll"),
                         attendees=[(A, "Ann Test")], start=NOW - timedelta(days=1),
                         subject="pay review")
        report, _ = self.capture(parts=["calendar"])
        self.assertEqual(report["counts"]["events_denied"], 1)
        self.assertEqual(self.graph.gets(r"/events/EvHr"), [])
        self.assertNotIn("EvHr", {p[1] for p in self.rows(cm.EVENT_KIND)})

    def test_refused_online_meetings_is_a_blocked_part(self) -> None:
        self.graph.forbidden = [r"/onlineMeetings$"]
        report, _ = self.capture()
        self.assertEqual(report["status"], "blocked")
        part = next(p for p in report["blocked_upstream"]
                    if p["part"] == "meetings.online_meetings")
        self.assertEqual((part["http_status"], part["permission"]),
                         (403, "OnlineMeetings.Read.All"))
        self.assertEqual(len(self.graph.gets(r"/onlineMeetings")), 1)   # stops at the first
        self.assertEqual(len(self.rows(cm.EVENT_KIND)), 3)              # calendars still held

    def test_guard_unavailable_reads_nothing(self) -> None:
        env = {k: v for k, v in ENV.items() if k != "PLX_EXCLUDED_PEOPLE_OIDS"}
        report, transport = self.capture(env=env)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["blocked_upstream"][0]["part"], "meetings.people_exclusion")
        self.assertEqual([c for c in self.graph.calls if c[0] == "GET"], [])
        self.assertEqual(self.store.units(cm.EVENT_KIND.name), [])

    def test_incremental_reads_only_changed(self) -> None:
        self.capture()
        first = len(self.graph.gets(r"/events/"))
        self.capture()
        self.assertEqual(len(self.graph.gets(r"/events/")), first)
        self.graph.events[A_ID]["EvA1"]["changeKey"] = "ck2"
        self.graph.events[A_ID]["EvA1"]["subject"] = "Weekly PO review moved"
        self.graph.tasks["TaskAnn_00000000000000001"]["@odata.etag"] = 'W/"2"'
        details = len(self.graph.gets(r"/details"))
        report, _ = self.capture()
        self.assertEqual(self.graph.gets(r"/events/")[first:],
                         [u for u in self.graph.gets(r"/events/EvA1")][-1:])
        self.assertEqual(len(self.graph.gets(r"/details")), details + 1)
        self.assertEqual(report["counts"]["versions_new"], 2)
        rows = {p[1]: v["row"] for p, v in self.rows(cm.EVENT_KIND).items()}
        self.assertEqual(rows["EvA1"]["subject"], "Weekly PO review moved")

    def test_cardinality_paging_keeps_every_event(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n):
                graph = FakeGraph()
                graph.events = {A_ID: {}, B_ID: {}}
                for i in range(n):
                    graph.event(A_ID, f"Ev{i:03d}", organizer=(A, "Ann Test"),
                                attendees=[(B, "Bob Test")], start=NOW - timedelta(days=i))
                self.graph = graph
                store = cm.open_store(self.state / f"n{n}", audience())
                self.addCleanup(store.close)
                api, _ = cm.build_api(ENV, sender=graph)
                report = cm.MeetingsCapture(store, api, audience(), CAPTURE, clock=lambda: NOW
                                            ).run(scope(mailboxes=[A], page_size=7,
                                                        parts=["calendar"]))
                self.assertEqual(report["status"], "complete")
                held = [r.path[1] for u in store.units(cm.EVENT_KIND.name)
                        for r in store.current_records(cm.EVENT_KIND.name, u)]
                self.assertEqual(sorted(held), sorted(graph.events[A_ID]))
                self.assertEqual(len(graph.gets(r"/events/")), n)

    def test_content_cap_resumes_from_the_page(self) -> None:
        report, _ = self.capture(max_event_reads_per_run=1, parts=["calendar"])
        self.assertIn("cap:event_reads", report["reasons"])
        report, _ = self.capture(parts=["calendar"])
        self.assertEqual(report["status"], "complete")
        self.assertEqual({p[1] for p in self.rows(cm.EVENT_KIND)}, {"EvA1", "EvA4", "EvB1"})

    def test_readers_are_trimmed(self) -> None:
        self.capture()
        for kind in cm.KINDS:
            for unit in self.store.units(kind.name):
                with self.assertRaises(Exception):
                    self.store.records(WITHHELD, kind.name, unit, limit=10)
        unit = self.store.units(cm.TASK_KIND.name)[0]
        self.assertEqual(len(self.store.records(MEMBER, cm.TASK_KIND.name, unit,
                                                limit=10)["items"]), 1)
        # The capture actor holds the plan's audience but not its group: a teams: resource
        # needs the task's own people or group.
        self.assertEqual(self.store.records(CAPTURE, cm.TASK_KIND.name, unit,
                                            limit=10)["items"], [])

    def test_scope_and_credentials_fail_closed(self) -> None:
        for bad in ({"schema": cm.SCOPE_SCHEMA}, {"schema": cm.SCOPE_SCHEMA, "mailboxes": [A],
                                                  "parts": ["mail"]},
                    {"schema": cm.SCOPE_SCHEMA, "mailboxes": ["not an address"]},
                    {"schema": cm.SCOPE_SCHEMA, "mailboxes": [A], "surprise": 1}):
            with self.assertRaises(cm.MeetingsCaptureError):
                cm.MeetingsScope(bad)
        with self.assertRaises(cm.MeetingsCaptureError):
            cm.credential_prefix({cm.CREDENTIALS_ENV: "BC"})
        # Every other reader still refuses the send-only admin application's credentials.
        from integrations.live_source_guard import ClientCredentials, ReadOnlyGuardError
        with self.assertRaises(ReadOnlyGuardError):
            ClientCredentials.from_env("AZURE", ENV)
        with self.assertRaises(Exception):
            graph_read.build_graph(ENV, sender=self.graph)   # no MICROSOFT_GRAPH_* given
        self.assertEqual(cm.credential_prefix({}), "AZURE")

    def test_cli_reports_counts_only(self) -> None:
        tmp = Path(self.tmp.name)
        (tmp / "scope.json").write_text(json.dumps({"schema": cm.SCOPE_SCHEMA,
                                                    "mailboxes": [A, B, X]}))
        args = argparse.Namespace(state_dir=str(tmp / "cli"), scope=str(tmp / "scope.json"),
                                  live_audience=str(Path(cm.REPO_ROOT) / "examples"
                                                    / "meetings_capture_audience.live-check.json"),
                                  principal=cm.CAPTURE_PRINCIPAL,
                                  withheld_principal="staff:meetings-withheld", runs=1,
                                  budget_seconds=None, workers=2)
        report = cm.run_cli(args, environ=ENV, sender=self.graph)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual(report["relations"]["calendarEvents"], 3)
        self.assertEqual(report["leak_check"], {"withheld_actor_rows": 0,
                                                "withheld_actor_resolved": 0})
        text = json.dumps(report)
        for secret in (ENV["AZURE_CLIENT_SECRET"], "TOKEN-do-not-leak", A, X_ID, "PO100235"):
            self.assertNotIn(secret, text)


class ProbeTests(unittest.TestCase):
    """The live-check spec's own argv over the fake Graph: its expectations hold on the
    fixture output, which carries no content."""

    SPEC = json.loads((Path(cm.REPO_ROOT) / "devloop/live-checks/graph.meetings_capture.json")
                      .read_text())

    def test_the_spec_passes_on_the_fixture(self) -> None:
        import contextlib
        import io
        from verify import live_verify as lv

        root = Path(cm.REPO_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            argv = [str(root / a) if a.startswith("examples/") else a.replace("{tmp}", tmp)
                    for a in self.SPEC["command"][3:]]
            out = io.StringIO()
            graph = FakeGraph()
            with contextlib.redirect_stdout(out):
                code = graph_read.main(argv, environ=ENV, sender=graph)
        report = json.loads(out.getvalue())
        self.assertEqual(lv.check_expectations(self.SPEC, code, report), [], report)
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual(report["status"], "complete")
        self.assertIn("graph.online_meeting_transcript_content", report["operations_read"])
        text = json.dumps(report)
        for value in (A, B, X, X_ID, "Weekly PO", "PO100235", "do-not-leak"):
            self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
