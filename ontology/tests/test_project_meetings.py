"""The meetings capture projected through the host's build (evidence: injected-transport).

A state directory holding a meetings capture (calendar events, Planner tasks, a meeting
transcript; captured over the fake Graph of ``integrations.tests.test_capture_meetings``)
beside a Portal purchase order projects ``CalendarEvent``, ``Meeting``, ``PlannerTask`` and
``MeetingTranscript`` objects in the calendar's, plan's and organizer's audience units;
each attendee's event links to the one meeting, the transcript to its meeting, and a PO
number spoken in the transcript or written in a task becomes a lead mention link to that
PO. The case runtime resolves a meetings handle for a granted reader only, and withholds
it from every reader once a person it names is excluded.
"""

from __future__ import annotations

from contextlib import ExitStack
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from integrations import capture_meetings as cm
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AudienceMap
from integrations.tests import test_capture_meetings as tcm
from ontology.tests.test_find_identity import Source
from operating import host
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.cases import CitationUnresolvable


class MeetingsProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        store = cm.open_store(self.state, tcm.audience())
        api, _ = cm.build_api(tcm.ENV, sender=tcm.FakeGraph())
        report = cm.MeetingsCapture(store, api, tcm.audience(), tcm.CAPTURE, transcripts_granted=True,
                                    clock=lambda: tcm.NOW).run(tcm.scope())
        store.close()
        self.assertEqual(report["status"], "complete", report["reasons"])
        with ExitStack() as stack:
            sources = host.state_captures(self.state, stack)
            self.assertIn("meetings", sources)
            sources["portal_api"] = Source("portal_api", {"public.app_purchase_order": [
                {"id": "p2", "po_number": "PO100235"}]})
            self.built = host.build_projection(self.state, sources)
        self.conn = sqlite3.connect(host.projection_path(self.state))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    def count(self, sql: str, *args) -> int:
        return self.conn.execute(sql, args).fetchone()[0]

    def links(self, link_type: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT o.object_type, o.audience_key, t.object_type AS to_type, l.reason, l.tier "
            "FROM links l JOIN objects o ON o.object_id = l.from_object "
            "JOIN objects t ON t.object_id = l.to_object WHERE l.link_type = ?",
            (link_type,)).fetchall()

    def test_objects_links_and_mentions(self) -> None:
        types = dict(self.conn.execute(
            "SELECT object_type, COUNT(*) FROM objects WHERE object_type IN ('CalendarEvent', "
            "'Meeting', 'PlannerTask', 'MeetingTranscript') GROUP BY object_type").fetchall())
        self.assertEqual(types, {"CalendarEvent": 3, "Meeting": 1, "PlannerTask": 1,
                                 "MeetingTranscript": 1})
        keys = {r[0].split(":", 2)[1] for r in self.conn.execute(
            "SELECT audience_key FROM objects WHERE object_type IN ('CalendarEvent', 'Meeting', "
            "'PlannerTask', 'MeetingTranscript')")}
        self.assertEqual(keys, {"calendar", "plan", "organizer"})
        # Both attendees' copies of the online meeting reach the one Meeting; so does its
        # transcript.
        self.assertEqual(len(self.links("CalendarEvent.of_meeting")), 2)
        self.assertEqual(len(self.links("MeetingTranscript.of_meeting")), 1)
        for link_type in ("MeetingTranscript.mentions_purchase_order",
                          "PlannerTask.mentions_purchase_order"):
            # The mention step's lead, and the corroborated twin the unique_mentions step
            # (ontology/mention_firm.py) writes beside an exact one-record mention.
            [row] = [r for r in self.links(link_type) if r["tier"] == "lead"]
            self.assertLessEqual({r["tier"] for r in self.links(link_type)},
                                 {"lead", "corroborated"})
            self.assertEqual(row["to_type"], "PurchaseOrder")
            self.assertIn("identifier_mention", row["reason"])
        self.assertEqual(self.built["post_project"]["mentions"]["counts"]["undeclared_target"],
                         0)
        # Nothing of what the capture withheld (an excluded organizer, attendee, assignee or
        # creator) is anywhere in the projection, nor the excluded person's address.
        everything = []
        for (table,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                          "AND sql NOT LIKE 'CREATE VIRTUAL%'").fetchall():
            try:
                everything += [repr(tuple(r)) for r in self.conn.execute(f'SELECT * FROM "{table}"')]
            except sqlite3.OperationalError:
                continue   # a shadow table of a virtual one
        everything = "\n".join(everything)
        for withheld in ("excluded organizer", "excluded attendee", "excluded assignee",
                         "excluded creator", tcm.X, tcm.X_ID):
            self.assertNotIn(withheld, everything)
        self.assertIn("Weekly PO review", everything)

    def test_handles_resolve_for_granted_readers_and_exclusion_wins(self) -> None:
        handle = self.conn.execute(
            "SELECT v.l1_handle FROM object_versions v JOIN objects o ON o.object_id = "
            "v.object_id WHERE o.object_type = 'CalendarEvent' AND json_extract(o.native_key, "
            "'$.id') = 'EvA1'").fetchone()[0]
        self.assertTrue(handle.startswith("calendar-event://"))

        def runtime(excluded: str) -> CaseRuntime:
            found = CaseRuntime(self.tmp.name, client=None,
                                audience=AudienceMap({}, tenant="plx"), tenant="plx",
                                live=LiveReaders(tcm.audience()),
                                people_exclusion=PeopleExclusion([excluded]))
            self.addCleanup(found.close)
            return found

        view = runtime(tcm.X_ID).resolve_citation(handle, tcm.CAPTURE)
        self.assertEqual(view["row"]["subject"], "Weekly PO review")
        with self.assertRaises(CitationUnresolvable):
            runtime(tcm.X_ID).resolve_citation(handle, tcm.WITHHELD)
        # Bob attends EvA1: once he is excluded, nobody reads it (his object id is named in
        # the event's people only by address, so the transcript, whose audience names the
        # organizer's object id, shows the rule on object ids too).
        transcript = self.conn.execute(
            "SELECT v.l1_handle FROM object_versions v JOIN objects o ON o.object_id = "
            "v.object_id WHERE o.object_type = 'MeetingTranscript'").fetchone()[0]
        self.assertTrue(runtime(tcm.X_ID).resolve_citation(transcript, tcm.CAPTURE)["row"])
        with self.assertRaises(CitationUnresolvable):
            runtime(tcm.A_ID).resolve_citation(transcript, tcm.CAPTURE)


if __name__ == "__main__":
    unittest.main()
