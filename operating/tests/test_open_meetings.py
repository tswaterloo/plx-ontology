"""Calendar events, Planner tasks and meeting transcripts open through ``plx.read`` /
``plx.open`` (evidence: injected-transport Graph of ``integrations.tests
.test_capture_meetings``, isolated fixture; live-unverified).

The meetings capture's handles (``calendar-event://``, ``planner-task://``,
``meeting-transcript://``) are read from the host's capture through the one case-runtime
resolver, as Teams and Mission Control handles are: a reader the audience grants a calendar
gets its events' fields, a reader granted another calendar or none gets nothing (read alike
as unresolvable, no field of the record in the answer), a host whose people exclusion is not
loaded serves no meetings record, and a scheme the reader does not know stays unsupported.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from integrations import capture_meetings as cm
from integrations.live_source_guard import LiveSourceAudience
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AudienceMap
from integrations.tests import test_capture_meetings as tcm
from ontology.types import Actor
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.ontology_tools_evidence import RuntimeCitations

OWNER = Actor("plx", frozenset({"staff:owner"}), "restricted")
ANN = Actor("plx", frozenset({"staff:ann"}), "restricted")
NOBODY = Actor("plx", frozenset({"staff:nobody"}), "restricted")


def _audience() -> LiveSourceAudience:
    return LiveSourceAudience(
        {"mail:*": [cm.CAPTURE_PRINCIPAL, "staff:owner"], f"mail:{tcm.A_ID}": ["staff:ann", "staff:owner"],
         "teams:planner:*": [cm.CAPTURE_PRINCIPAL, "staff:plan-member", "staff:ann"]},
        tenant="plx", identities={"staff:plan-member": [f"group:{tcm.GROUP}"]})


class OpenMeetings(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name)
        store = cm.open_store(cls.state, tcm.audience())
        api, _ = cm.build_api(tcm.ENV, sender=tcm.FakeGraph())
        report = cm.MeetingsCapture(store, api, tcm.audience(), tcm.CAPTURE,
                                    transcripts_granted=True, clock=lambda: tcm.NOW,
                                    ).run(tcm.scope())
        assert report["status"] == "complete", report["reasons"]
        cls.handles: dict[str, list[tuple[str, dict]]] = {}
        for kind in cm.KINDS:
            for unit in store.units(kind.name):
                # a Planner task is read by its plan's group member (its own ACL)
                who = tcm.MEMBER if kind is cm.TASK_KIND else tcm.CAPTURE
                page = store.records(who, kind.name, unit, limit=100)
                for rec in page["items"]:
                    cls.handles.setdefault(kind.scheme, []).append((rec["citation"], rec["row"]))
        store.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def reader(self, exclusion: bool = True, env: dict | None = None) -> RuntimeCitations:
        runtime = CaseRuntime(self.state, client=None, audience=AudienceMap({}, tenant="plx"),
                              tenant="plx", live=LiveReaders(_audience()),
                              people_exclusion=PeopleExclusion.from_env(env or tcm.ENV)
                              if exclusion else None)
        self.addCleanup(runtime.close)
        return RuntimeCitations(runtime)

    def test_every_meetings_scheme_opens_for_its_readers_only(self) -> None:
        self.assertEqual(sorted(self.handles), sorted(k.scheme for k in cm.KINDS))
        reader = self.reader()
        events = self.handles["calendar-event"]
        ann_events = [h for h, _ in events if h.startswith(f"calendar-event://{tcm.A_ID.lower()}")
                      or h.startswith(f"calendar-event://{tcm.A_ID}")]
        self.assertTrue(ann_events and len(ann_events) < len(events), events)
        for scheme, found in self.handles.items():
            for handle, row in found:
                with self.subTest(handle=handle):
                    out = reader.read(handle, tcm.MEMBER if scheme == "planner-task" else OWNER)
                    self.assertEqual(out["status"], "resolved", out)
                    if row.get("subject"):
                        self.assertEqual(out["fields"]["row.subject"], row["subject"])
        # a task is never opened by a reader outside its plan's group, granted the plan or not
        for handle, _ in self.handles["planner-task"]:
            for actor in (ANN, NOBODY):
                self.assertEqual(reader.read(handle, actor)["status"], "unresolvable")
        weekly = next(h for h, r in events if h in ann_events and r.get("subject"))
        self.assertEqual(reader.read(weekly, ANN)["status"], "resolved")
        for handle, row in events:
            if handle in ann_events:
                continue
            for actor in (ANN, NOBODY):
                with self.subTest(handle=handle, actor=actor):
                    out = reader.read(handle, actor)
                    self.assertEqual(out["status"], "unresolvable", out)
                    self.assertNotIn("fields", out)
                    self.assertNotIn(json.dumps(row.get("attendees") or "x")[1:-1] or "x",
                                     json.dumps(out))

    def test_no_people_exclusion_serves_no_meetings_record(self) -> None:
        reader = self.reader(exclusion=False)
        for found in self.handles.values():
            for handle, _ in found:
                with self.subTest(handle=handle):
                    out = reader.read(handle, tcm.MEMBER if handle.startswith("planner-task")
                                      else OWNER)
                    self.assertEqual(out["status"], "unresolvable", out)
                    self.assertNotIn("fields", out)

    def test_a_person_excluded_after_the_capture_withholds_their_calendar(self) -> None:
        bobs = [h for h, _ in self.handles["calendar-event"]
                if h.lower().startswith(f"calendar-event://{tcm.B_ID.lower()}/")]
        self.assertTrue(bobs)
        for handle in bobs:
            self.assertEqual(self.reader().read(handle, OWNER)["status"], "resolved")
        later = dict(tcm.ENV, PLX_EXCLUDED_PEOPLE_OIDS=f"{tcm.X_ID},{tcm.B_ID}")
        reader = self.reader(env=later)
        for handle in bobs:
            with self.subTest(handle=handle):
                out = reader.read(handle, OWNER)
                self.assertEqual(out["status"], "unresolvable", out)
                self.assertNotIn("fields", out)

    def test_an_unknown_scheme_stays_unsupported(self) -> None:
        handle = self.handles["calendar-event"][0][0].replace("calendar-event://",
                                                              "calendar-events://", 1)
        out = self.reader().read(handle, OWNER)
        self.assertEqual((out["status"], out["reason"]),
                         ("unresolvable", "citation_scheme_not_supported"))


def _long_vtt(cues: int, said: str | None = None, at: int = 0) -> str:
    """A WebVTT transcript of ``cues`` spoken lines (~150 chars each), line ``at`` saying
    ``said`` when given."""
    lines = ["WEBVTT", ""]
    for n in range(cues):
        text = said if said is not None and n == at else (
            f"Line {n}: the batch PO100235 assay came back in spec and the drums ship on "
            "the usual lane with the pallet labels checked twice by the shift lead")
        start = f"00:{n // 60:02d}:{n % 60:02d}.000"
        lines += [f"{start} --> {start[:-4]}.900", f"<v Ann Test>{text}</v>", ""]
    return "\n".join(lines)


class OpenLongTranscripts(unittest.TestCase):
    """plx.open of a meeting transcript longer than one field read (its ``content_text``
    and ``cues`` cut at the read limit): the whole of each cut field is paged and checked
    for Freightcom before its head is shown (``ontology_tools_search.open_one`` over the
    host's own ``PagedRead``)."""

    def opened(self, vtt: str) -> tuple[dict, int]:
        from operating.case_tools_host import PagedRead
        from operating import ontology_tools as ot
        from operating import ontology_tools_evidence as ev
        from operating import ontology_tools_search as search
        from ontology import identifiers
        from tests import test_ontology_tools_evidence as te
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        graph = tcm.FakeGraph()
        graph.vtt["TranscriptOne"] = vtt
        store = cm.open_store(state, tcm.audience())
        api, _ = cm.build_api(tcm.ENV, sender=graph)
        report = cm.MeetingsCapture(store, api, tcm.audience(), tcm.CAPTURE,
                                    transcripts_granted=True, clock=lambda: tcm.NOW,
                                    ).run(tcm.scope())
        self.assertEqual(report["status"], "complete", report["reasons"])
        rows = [(rec["citation"], rec["row"]) for unit in store.units(cm.TRANSCRIPT_KIND.name)
                for rec in store.records(tcm.CAPTURE, cm.TRANSCRIPT_KIND.name, unit,
                                         limit=100)["items"]]
        store.close()
        handle, row = next((h, r) for h, r in rows if len(r["content_text"]) > 20_000)
        runtime = CaseRuntime(state, client=None, audience=AudienceMap({}, tenant="plx"),
                              tenant="plx", live=LiveReaders(_audience()),
                              people_exclusion=PeopleExclusion.from_env(tcm.ENV))
        self.addCleanup(runtime.close)
        base = ot.OntologyTools(ot.StoreReader(te.build().conn), te.POLICY,
                                item_gate=lambda a, h: True, schema=te.SCHEMA,
                                grammar=identifiers, contracts=te.CONTRACTS,
                                clock=lambda: te.NOW)
        evidence = ev.EvidenceTools(base, reader=RuntimeCitations(runtime))
        paged = PagedRead(evidence, runtime)
        return (search.open_one(lambda wanted: paged.call(wanted, OWNER),
                                {"citation": handle}), len(row["content_text"]))

    def test_a_long_clean_transcript_opens_cut(self) -> None:
        got, chars = self.opened(_long_vtt(300))
        self.assertGreater(chars, 40_000)
        self.assertEqual(got.get("status"), "resolved", got)
        shown = got["fields"]["row.content_text"]
        self.assertTrue(shown["truncated"])
        self.assertIn("Line 0: the batch PO100235", shown["untrusted_text"])

    def test_freightcom_past_the_field_cut_is_withheld(self) -> None:
        got, chars = self.opened(_long_vtt(300, "rates booked through Freight" "com today",
                                           at=280))
        self.assertGreater(chars, 40_000)
        self.assertEqual(got, {"error": {"code": "egress_withheld",
                                         "message": "this item is never shown to the "
                                                    "assistant"}})


if __name__ == "__main__":
    unittest.main()
