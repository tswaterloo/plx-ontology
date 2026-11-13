"""Meeting autopilot at its boundaries: the transcript admission (people exclusion fail
closed, unknown formats refused, hr marking), the follow-up's precision on the synthetic
meetings, who sees a follow-up through every view, that nothing is sent or written, the
evidence that closes an item, nudges, the prep-brief carry-over, the Ask skill and the
N=1/10/60 cardinality. Evidence label: isolated-fixture."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from evals import meeting_followup_eval as ev
from integrations import meeting_transcripts as mt
from operating import meeting_followup as mf

DANA = "dana.price@plx.example"
SAM = "sam.okafor@plx.example"
PRIYA = "priya.nair@plx.example"
MARCO = "marco.silva@plx.example"


def _raw(name: str):
    path = ev.HERE / name
    return json.loads(path.read_text()) if path.suffix == ".json" else path.read_text()


def _md(mid: str, lines: list[tuple[str, str]], *, attendees=None, start="2026-11-02T14:00:00Z",
        title="Ops weekly") -> str:
    people = attendees or [("Dana Price", DANA), ("Sam Okafor", SAM), ("Priya Nair", PRIYA)]
    body = [f"# {title}", f"Date: {start}", f"Organizer: {people[0][0]} <{people[0][1]}>",
            "Attendees: " + ", ".join(f"{n} <{a}>" for n, a in people[1:]), ""]
    body += [f"[00:{i // 60:02d}:{i % 60:02d}] {w}: {t}" for i, (w, t) in enumerate(lines)]
    return "\n".join(body) + f"\n\n<!-- {mid} -->\n"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = mf.FollowUps(self.tmp.name, ev.Guard())
        self.world = ev.World()

    def ingest(self, name: str, source: str | None = None, guard=None):
        src = source or ("circleback" if "circleback" in name else "granola"
                         if "granola" in name else "teams_vtt")
        return self.store.ingest(_raw(name), guard or ev.Guard(), self.world, source=src)


class AdmissionTest(Base):
    def test_people_exclusion_fails_closed(self) -> None:
        name = "ops-2026-11-02.granola.md"
        cases = {
            "excluded_person": ev.Guard(excluded={SAM}),
            "people_exclusion_unavailable": type("G", (), {"unavailable": "absent"})(),
            "undecided_person": ev.Guard(domain="elsewhere.example"),
        }
        for code, guard in cases.items():
            with self.subTest(code):
                self.assertEqual(self.ingest(name, guard=guard),
                                 {"status": "withheld", "reason": code})
        self.assertEqual(self.store.all(), [])
        # None as the guard, and a speaker nobody invited, withhold too
        self.assertEqual(self.store.ingest(_raw(name), None, self.world)["reason"],
                         "people_exclusion_unavailable")
        guest = _md("g", [("Dana Price", "Hello."), ("Unknown Guest", "I'll send it.")])
        self.assertEqual(self.store.ingest(guest, ev.Guard(), self.world)["reason"],
                         "unknown_speaker")
        self.assertEqual(self.store.all(), [])
        self.assertFalse(any(t == "plx.find" for t, _ in self.world.calls))

    def test_unknown_formats_refused(self) -> None:
        for raw in ("WEBVTT\n\n00:00.000 --> 00:01.000\n<v A>hi</v>", "just some prose",
                    b"\xff\xfe", {"title": "x"}, 42, '{"transcript": "nope"}'):
            with self.subTest(raw=str(raw)[:20]):
                with self.assertRaises(mt.TranscriptRefused):
                    mt.parse(raw)

    def test_markings(self) -> None:
        self.ingest("hr-2026-11-04.granola.md")
        self.ingest("ops-2026-11-02.granola.md")
        marks = {r["meeting"]["title"]: r["meeting"]["markings"] for r in self.store.all()}
        self.assertEqual(marks["1:1 Sam / Hannah"], ["hr", "meeting", "pii"])
        self.assertEqual(marks["Ops weekly"], ["meeting"])
        for said in ("Her pay is going up 5%.", "We should increase Jim's pay.",
                     "We need to fire Mark.", "We are letting Jo go at the end of the month.",
                     "What's his hourly rate now?", "She's on leave for surgery.",
                     "His salary review is next week.", "Wages go up in January.",
                     "She is pregnant and needs an accommodation.", "He was terminated.",
                     "We're laying off two drivers.", "That's a disciplinary matter.",
                     "Her performance review is due.", "He called in sick.",
                     "Bonuses are paid in March.", "His compensation changes.",
                     "Was he fired?", "She got a raise.", "a medical issue"):
            with self.subTest(said=said):
                self.assertIn("hr", mt.markings({"title": "x", "cues": [{"text": said}]}))
        # "raise a PO" is purchasing, not pay
        self.assertEqual(mt.markings({"title": "x", "cues": [
            {"text": "I'll raise a PO for the freight."}]}), ["meeting"])


class ExclusionAfterAdmissionTest(Base):
    def test_a_person_excluded_later_empties_every_surface(self) -> None:
        self.ingest("ops-2026-11-02.granola.md")
        record = self.store.all()[0]
        mid = record["meeting"]["meeting_id"]
        dana = mf.Viewer.of([], [], DANA)
        self.assertTrue(mf.my_items(self.store, mf.Viewer.of([], [], PRIYA)))
        for guard, gone in ((type("G", (), {"unavailable": "absent"})(), False),
                            (ev.Guard(domain="elsewhere.example"), True),
                            (ev.Guard(excluded={SAM}), True)):
            with self.subTest(guard=type(guard).__name__):
                if gone:   # put it back: the previous guard deleted it
                    mf.FollowUps(self.tmp.name, ev.Guard()).put(record)
                store = mf.FollowUps(self.tmp.name, guard)
                nxt = {**record["meeting"], "start": "2026-11-09T14:00:00Z"}
                self.assertIsNone(store.get(mid))
                self.assertEqual(store.all(), [])
                self.assertEqual(mf.my_items(store, mf.Viewer.of([], [], PRIYA)), [])
                self.assertEqual(mf.my_items(store, dana, PRIYA), [])
                self.assertEqual(mf.ask(store, dana, "what did we decide about Gulf Coast "
                                        "Starch")["lines"], [])
                self.assertEqual(mf.carried_items(store, nxt, dana), [])
                self.assertEqual(mf.nudges(store, date(2026, 11, 20)), [])
                self.assertEqual(mf.track(store, mf.Evidence(None), date(2026, 11, 20)),
                                 {"open": 0, "done": 0, "overdue": 0, "changed": 0})
                stored = list((Path(self.tmp.name) / mf.STORE_DIR).glob("*.json"))
                self.assertEqual(bool(stored), not gone)   # deleted unless the guard is down
        # re-ingesting with the attendee excluded deletes it too and keeps nothing
        mf.FollowUps(self.tmp.name, ev.Guard()).put(record)
        self.assertEqual(self.ingest("ops-2026-11-02.granola.md",
                                     guard=ev.Guard(excluded={SAM}))["reason"], "excluded_person")
        self.assertEqual(list((Path(self.tmp.name) / mf.STORE_DIR).glob("*.json")), [])


class UnderstandTest(Base):
    def test_precision_on_the_synthetic_meetings(self) -> None:
        tuned, held_out = ev.measure(), ev.measure(ev.HERE / "heldout")
        for found in (tuned, held_out):
            self.assertGreaterEqual(found["action_precision"], 0.9, found["wrong"])
            self.assertGreaterEqual(found["link_precision"], 0.9, found["wrong_links"])
        self.assertGreaterEqual(tuned["action_recall"], 0.9, tuned["misses"])

    def test_every_item_cited_and_ambiguous_names_stay_leads(self) -> None:
        self.ingest("ops-2026-11-02.granola.md")
        record = self.store.all()[0]
        for group in ("decisions", "actions", "questions", "risks"):
            self.assertTrue(record[group], group)
            for row in record[group]:
                self.assertRegex(row["citations"][0], r"^transcript://granola/.+#t=\d\d:\d\d:\d\d$")
        links = mf.link_records(self.world, "Let's call Prairie about pulses.", "c#t=00:00:01")
        self.assertEqual([lk["status"] for lk in links], ["ambiguous"])
        self.assertNotIn("object_id", links[0])

    def test_model_items_must_quote_their_line(self) -> None:
        transcript = mt.parse(_raw("ops-2026-11-02.granola.md"), source="granola")
        reply = {"items": [
            {"kind": "risk", "line": 6, "quote": "we're short on item TAP-2200"},
            {"kind": "action", "line": 6, "quote": "Marco will buy 90,000 kg tomorrow",
             "owner": "Marco"},
            {"kind": "decision", "line": 99, "quote": "anything at all here"}]}
        spawn = lambda *a: json.dumps({"result": json.dumps(reply)})  # noqa: E731
        items = mf.ModelExtractor(spawn)(transcript)
        self.assertEqual([(i["kind"], i["text"]) for i in items],
                         [("risk", "we're short on item TAP-2200")])


class ShowTest(Base):
    def setUp(self) -> None:
        super().setUp()
        for name in ("ops-2026-11-02.granola.md", "ops-2026-11-09.granola.md",
                     "hr-2026-11-04.granola.md", "qa-2026-11-03.circleback.json"):
            self.assertEqual(self.ingest(name)["status"], "kept")
        self.by_title = {(r["meeting"]["title"], r["meeting"]["start"][:10]): r
                         for r in self.store.all()}
        self.hr = self.by_title[("1:1 Sam / Hannah", "2026-11-04")]

    def test_hr_meeting_hidden_from_non_hr_readers_everywhere(self) -> None:
        sam = mf.Viewer.of([], ["finance"], SAM)          # attendee without hr
        cleared = mf.Viewer.of([], ["hr"], SAM)
        stranger = mf.Viewer.of([], ["hr", "finance", "pii"], "other@plx.example")
        self.assertIsNone(mf.page(self.hr, sam, self.world))
        self.assertIsNone(mf.page(self.hr, stranger, self.world))   # not a participant
        self.assertIsNotNone(mf.page(self.hr, cleared, self.world))
        hr_items = {a["id"] for a in self.hr["actions"]}
        self.assertFalse(hr_items & {a["id"] for a in mf.my_items(self.store, sam,
                                                                     "hannah.cole@plx.example")})
        self.assertFalse(any("pay review" in ln["text"] for ln in mf.ask(
            self.store, sam, "what's open from Wednesday's 1:1 meeting")["lines"]))
        nxt = {**self.hr["meeting"], "start": "2026-11-11T18:00:00Z"}
        self.assertEqual(mf.carried_items(self.store, nxt, sam), [])
        self.assertTrue(mf.carried_items(self.store, nxt, cleared))
        self.assertIsNotNone(mf.page(self.hr, mf.Viewer(owner=True), self.world))

    def test_my_items_only_for_the_person_and_the_organiser(self) -> None:
        priya, dana = mf.Viewer.of([], [], PRIYA), mf.Viewer.of([], [], DANA)
        sam = mf.Viewer.of([], [], SAM)
        mine = mf.my_items(self.store, priya)
        self.assertTrue(mine)
        self.assertTrue(all(a["owner"]["address"] == PRIYA for a in mine))
        self.assertEqual(len(mf.my_items(self.store, dana, PRIYA)), len(mine))  # organiser
        self.assertEqual(mf.my_items(self.store, sam, PRIYA), [])              # a peer: none

    def test_page_prepares_work_and_nothing_sends_or_writes(self) -> None:
        dana = mf.Viewer.of([], [], DANA)
        sent = mock.Mock(side_effect=AssertionError("sent"))
        with mock.patch("operating.card_actions.ActionDesk.confirm", sent), \
                mock.patch("operating.write_intents.WriteDesk.prepare", sent), \
                mock.patch("operating.write_intents.WriteDesk.execute", sent, create=True):
            pages = [mf.page(r, dana, self.world) for r in self.store.all()]
        kinds = set()
        for p in filter(None, pages):
            self.assertEqual((p["sent"], p["writes"]), (False, 0))
            for a in p["actions"]:
                work = a["prepared"]
                kinds.add(work["kind"])
                self.assertEqual((work["sent"], work["writes"]), (False, 0))
                self.assertTrue(work["citations"])
        self.assertEqual(kinds, {"answer", "write_intent", "document_pack", "message",
                                 "planner_task"})
        self.assertLessEqual({t for t, _ in self.world.calls}, {
            "plx.find", "plx.object", "plx.around", "plx.documents"})   # reads only
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()),
                         [mf.STORE_DIR])
        ops = self.by_title[("Ops weekly", "2026-11-09")]
        answer = next(a for a in mf.page(ops, dana, self.world)["actions"]
                      if a["prepared"]["kind"] == "answer")["prepared"]
        self.assertEqual(answer["answers"][0]["values"][0]["value"], "800")
        # a finance-marked value is never in an answer for a reader without finance
        tap = mf.prepare(ops["meeting"], {"id": "x", "text": "check stock on item TAP-2200",
                                          "citations": ["c"], "plan": {
                                              "kind": "answer", "object_ids": ["Item:TAP-2200"]}},
                         self.world, [])
        self.assertEqual([v["property"] for v in tap["answers"][0]["values"]], ["on_hand_kg"])
        email = next(a for a in mf.page(self.by_title[("Ops weekly", "2026-11-02")], dana,
                                        self.world)["actions"]
                     if a["prepared"]["kind"] == "message"
                     and a["owner"]["address"] == PRIYA)["prepared"]
        self.assertEqual(email["card"]["draft"]["to"]["address"], "orders@northfield.example")

    def test_ask_skill(self) -> None:
        dana = mf.Viewer.of([], [], DANA)
        decided = mf.ask(self.store, dana, "What did we decide about Gulf Coast Starch?")
        self.assertEqual(decided["status"], "answered")
        self.assertIn("Gulf Coast Starch", decided["lines"][0]["text"])
        self.assertTrue(decided["lines"][0]["citations"])
        opened = mf.ask(self.store, dana, "what's open from Monday's ops meeting",
                        today=date(2026, 11, 10))
        self.assertEqual(opened["date"], "2026-11-09")
        self.assertTrue(any("PO100871" in ln["text"] for ln in opened["lines"]))
        outsider = mf.Viewer.of([], [], "x@plx.example")
        self.assertEqual(mf.ask(self.store, outsider, "what's open from Monday's ops "
                                "meeting")["lines"], [])
        self.assertEqual(mf.ask(self.store, dana, "tell me a joke")["status"],
                         "unknown_question")


class TrackTest(Base):
    def test_evidence_closes_items_and_nudges_go_to_the_owner(self) -> None:
        self.ingest("ops-2026-11-02.granola.md")
        record = self.store.all()[0]
        by_owner = {a["owner"]["address"]: a for a in record["actions"]}
        email = by_owner[PRIYA]
        self.world.edges = [
            {"from": "PurchaseOrder:PO100999", "to": "Supplier:S1",
             "link_type": "PurchaseOrder.ordered_from", "tier": "verified"},
            {"from": "Shipment:SH1", "to": "SalesOrder:104511", "link_type": "Shipment.ships",
             "tier": "verified", "l1_handle": "fixture://Shipment:SH1"}]
        records = dict(ev.RECORDS)
        records["PurchaseOrder:PO100999"] = {"values": [ev._v("order_date", "2026-11-03",
                                                              "PurchaseOrder:PO100999")]}
        self.world.records = records
        # before any evidence: Wednesday's PO item is due soon on Tuesday, then overdue
        before = mf.track(self.store, mf.Evidence(None), date(2026, 11, 3))
        self.assertEqual((before["open"], before["overdue"], before["done"]), (2, 1, 0))
        soon = mf.nudges(self.store, date(2026, 11, 3))
        self.assertEqual({(n["kind"], n["owner"]) for n in soon},
                         {("due_soon", MARCO), ("overdue", PRIYA)})
        journal = [{"card_id": "meeting:" + email["id"], "status": "sent", "at": "t"}]
        counts = mf.track(self.store, mf.Evidence(self.world, journal), date(2026, 11, 6))
        self.assertEqual(counts["done"], 3, self.store.all()[0]["actions"])
        done = {a["owner"]["address"]: a["evidence"][0]["what"]
                for a in self.store.all()[0]["actions"]}
        self.assertEqual(done, {PRIYA: "sent", MARCO: "purchase order created",
                                SAM: "shipment went out"})
        self.assertEqual(mf.nudges(self.store, date(2026, 11, 6)), [])

    def test_open_items_roll_into_the_next_meeting_of_the_series(self) -> None:
        self.ingest("ops-2026-11-02.granola.md")
        self.ingest("qa-2026-11-03.circleback.json")
        dana = mf.Viewer.of([], [], DANA)
        nxt = {"title": "Ops weekly", "start": "2026-11-09T14:00:00Z",
               "organizer": {"name": "Dana Price", "address": DANA}}
        carried = mf.carried_items(self.store, nxt, dana)
        self.assertEqual(len(carried), 3)
        self.assertTrue(all(r["label"].startswith("Action item:") and r["sources"]
                            for r in carried))
        # API-016: each source is a handle the brief keeps (a bare string was dropped)
        from operator_api import briefs
        for r in carried:
            self.assertEqual([s.handle for s in briefs._sources(r["sources"])],
                             [s["handle"] for s in r["sources"]])


class CardinalityTest(unittest.TestCase):
    def test_n_1_10_60(self) -> None:
        lines = [("Dana Price", "Sam, can you ship SO 104511 on Thursday?"),
                 ("Sam Okafor", "Yes."),
                 ("Priya Nair", "I'll email Northfield Bakery today with the new date.")]
        for n in (1, 10, 60):
            with self.subTest(n=n), tempfile.TemporaryDirectory() as tmp:
                exports = Path(tmp) / mf.EXPORTS_DIR
                exports.mkdir()
                for i in range(n):
                    (exports / f"m{i:03d}.granola.md").write_text(_md(f"m{i}", lines, start=(
                        f"2026-10-{1 + i % 28:02d}T{10 + i // 28:02d}:00:00Z")))
                started = time.monotonic()
                report = mf.run(tmp, ev.Guard(), ev.World())
                self.assertLess(time.monotonic() - started, 0.5 * n + 2)
                self.assertEqual(report["counts"], {"kept": n})
                store = mf.FollowUps(tmp, ev.Guard())
                actions = [a["id"] for r in store.all() for a in r["actions"]]
                self.assertEqual(len(actions), 2 * n)
                self.assertEqual(len(set(actions)), 2 * n)
                self.assertEqual(mf.run(tmp, ev.Guard(), ev.World())["counts"], {"unchanged": n})


if __name__ == "__main__":
    unittest.main()


class AgendaSkillTest(Base):
    def test_plx_agenda_follow_up_answers_only_the_attendee(self) -> None:
        from operating.meeting_agenda import AgendaTool
        self.ingest("ops-2026-11-02.granola.md")
        guard = ev.Guard()
        guard.object_id_of = lambda a: "11111111-2222-3333-4444-555555555555" if a == DANA \
            else None
        self.store.ingest(_raw("ops-2026-11-09.granola.md"), guard, self.world, source="granola")
        tool = AgendaTool(lambda *a: {}, None, held_markings=lambda actor: [],
                          follow_ups=self.tmp.name)
        tool.people = guard
        actor = type("A", (), {"principals": frozenset(
            {"employee:11111111-2222-3333-4444-555555555555"})})()
        q = {"follow_up": "what did we decide about releasing it"}
        self.assertEqual(tool.call(q, actor)["status"], "answered")
        other = type("A", (), {"principals": frozenset(
            {"employee:99999999-2222-3333-4444-555555555555"})})()
        self.assertEqual(tool.call(q, other)["lines"], [])
        self.assertIn("error", tool.call({**q, "brief": "weekly"}, actor))
        self.assertIn("error", AgendaTool(lambda *a: {}, None).call(q, actor))


class TeamsJoinAndCycleTest(unittest.TestCase):
    ORG = "aaaaaaaa-0000-4000-8000-000000000001"

    def _event(self, mailbox, is_org, attendees):
        return {"ical_uid": "uid-1", "mailbox": mailbox, "is_organizer": is_org,
                "organizer": {"name": "Dana Price", "address": DANA},
                "attendees": [{"name": n, "address": a} for n, a in attendees]}

    def _row(self):
        return {"source": "graph_meeting_transcript", "ical_uid": "uid-1",
                "organizer": self.ORG, "meeting_id": "m1", "subject": "Ops weekly",
                "meeting_start": "2026-11-02T14:00:00Z",
                "cues": [{"start": "00:00:05.000", "speaker": "Dana Price",
                          "text": "Sam, can you ship SO 104511 on Thursday?"},
                         {"start": "00:00:09.000", "speaker": "Sam Okafor", "text": "Yes."}]}

    def test_join_admits_on_one_answer_and_fails_closed_otherwise(self) -> None:
        sam = [("Sam Okafor", SAM)]
        joined, withheld = mf.join_teams([self._row()], [
            self._event(self.ORG, True, sam), self._event("other", False, [])])
        self.assertEqual(len(joined), 1)          # the organiser's own copy decides
        with tempfile.TemporaryDirectory() as tmp:
            store = mf.FollowUps(tmp, ev.Guard())
            self.assertEqual(store.ingest(joined[0], ev.Guard(), ev.World(),
                                          source="teams_vtt")["status"], "kept")
        # no organiser copy and attendee copies that disagree: ambiguous, withheld
        joined, withheld = mf.join_teams([self._row()], [
            self._event("a", False, sam), self._event("b", False, [])])
        self.assertEqual((joined, withheld["attendees_ambiguous"]), ([], 1))
        joined, withheld = mf.join_teams([self._row()], [])
        self.assertEqual((joined, withheld["attendees_unjoined"]), ([], 1))
        # unjoined, the raw organiser id decides nobody: never admitted
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(mf.FollowUps(tmp, ev.Guard()).ingest(self._row(), ev.Guard(), ev.World(),
                                                      source="teams_vtt")["status"], "withheld")

    def test_cycle_is_a_no_op_without_transcripts_or_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            out = mf.cycle(tmp, lambda *a: calls.append(a) or {}, people=ev.Guard())
            self.assertEqual(out, {"skipped": "no_transcripts"})
            self.assertEqual((calls, list(Path(tmp).iterdir())), ([], []))
            (Path(tmp) / mf.EXPORTS_DIR).mkdir()
            (Path(tmp) / mf.EXPORTS_DIR / "a.granola.md").write_text(
                (ev.HERE / "ops-2026-11-02.granola.md").read_text())
            out = mf.cycle(tmp, ev.World(), people=ev.Guard(), today=date(2026, 11, 3))
            self.assertEqual(out["counts"], {"kept": 1})
            self.assertEqual(out["tracked"]["overdue"], 1)
            self.assertEqual((out["sent"], out["writes"]), (False, 0))
