"""The open-replies list's background drafts (``operating.reply_drafts``)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from operating import reply_drafts as rd

ANA = "aaaaaaaa-0000-0000-0000-000000000001"
BEN = "bbbbbbbb-0000-0000-0000-000000000002"


def inbox(path: Path, rows: list[tuple[str, str, str, int, str]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gaps (gap_id TEXT PRIMARY KEY, kind TEXT, object_id TEXT, "
                 "follow_up TEXT, priority INTEGER, state TEXT, first_seen TEXT)")
    for n, (kind, oid, owner, priority, state) in enumerate(rows):
        conn.execute("INSERT INTO gaps VALUES (?,?,?,?,?,?,?)",
                     (f"g{n}", kind, oid, json.dumps({"owner": owner}), priority, state,
                      f"2026-11-1{n % 10}"))
    conn.commit()
    conn.close()
    return path


class ReplyDrafts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.kept = rd.Kept(rd.path_of(self.tmp))

    def test_open_lines_are_the_reply_kinds_heaviest_first(self):
        path = inbox(self.tmp / "inbox.sqlite", [
            ("reply_owed", "Email:1", ANA, 1, "open"),
            ("needs_my_reply", "Email:2", f"tenant:{ANA.upper()}", 5, "open"),
            ("reply_owed", "Email:3", ANA, 9, "resolved"),
            ("po_late", "Email:4", ANA, 9, "open")])
        self.assertEqual(rd.open_lines(path), [(ANA, "Email:2", 5), (ANA, "Email:1", 1)])

    def test_wanted_caps_each_person_and_the_run_and_skips_ready_drafts(self):
        lines = [(ANA, f"Email:a{i}", 9 - i) for i in range(5)] + [(BEN, "Email:b0", 1)]
        self.kept.mark(ANA, "Email:a0", "ready", kind="reply", body="Yes, Friday.")
        got = rd.wanted(lines, self.kept, per_person=2, per_run=10)
        self.assertEqual(got, [(ANA, "Email:a1"), (ANA, "Email:a2"), (BEN, "Email:b0")])
        self.assertEqual(len(rd.wanted(lines, self.kept, per_person=9, per_run=3)), 3)
        self.assertEqual(rd.wanted(lines, self.kept, people=[BEN]), [(BEN, "Email:b0")])

    def test_a_drafting_mark_a_dead_run_left_is_drafted_again_later(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(rd.due({"state": "drafting", "at": now.isoformat()}, now))
        self.assertTrue(rd.due({"state": "drafting", "at": (now - timedelta(hours=1))
                                .isoformat()}, now))
        self.assertFalse(rd.due({"state": "ready", "at": "2020-01-01T00:00:00+00:00"}, now))

    def test_run_marks_drafting_first_then_keeps_each_outcome(self):
        seen = []

        def draft(owner, oid):
            seen.append(self.kept.rows([owner])[oid]["state"])
            if oid == "Email:boom":
                raise RuntimeError("model down")
            if oid == "Email:q":
                return [{"kind": "your_call", "ask": "Ship Friday?",
                         "options": [{"label": "Yes", "reply": "Yes, Friday."}]}]
            if oid == "Email:none":
                return [{"why": "nothing_useful"}]
            return [{"kind": "reply", "draft": "PO 4521 ships on [date].", "note": "the date is yours"}]

        counts = rd.run([(ANA, "Email:r"), (ANA, "Email:q"), (ANA, "Email:none"),
                         (BEN, "Email:boom")], self.kept, draft, parallel=2)
        self.assertEqual(seen, ["drafting"] * 4)
        self.assertEqual(counts, {"ready": 2, "none": 2})
        ana = self.kept.rows([ANA])
        self.assertEqual(ana["Email:r"]["body"], "PO 4521 ships on [date].")
        self.assertEqual(ana["Email:r"]["ask"], "the date is yours")      # a reply's note
        self.assertEqual(ana["Email:q"]["options"][0]["reply"], "Yes, Friday.")
        self.assertEqual(ana["Email:none"]["why"], "nothing_useful")
        self.assertTrue(self.kept.rows([BEN])["Email:boom"]["why"].startswith("draft_failed"))

    def test_attach_puts_the_kept_draft_on_its_line_never_waiting(self):
        self.kept.mark(ANA, "Email:r", "ready", kind="reply", body="PO 4521 ships Friday.",
                       ask="checked the PO's date")
        self.kept.mark(ANA, "Email:q", "ready", kind="your_call", ask="Ship Friday?",
                       options=[{"label": "Yes", "reply": "Yes."}])
        self.kept.mark(ANA, "Email:d", "drafting")
        hold = {"body": "I'm looking into your message.", "copy_check": {}, "sent": False}
        lines = [{"object_id": o, "draft": dict(hold)}
                 for o in ("Email:r", "Email:q", "Email:d", "Email:new")]
        rd.attach(lines, rd.kept_for(self.tmp, [f"tenant:{ANA}"], lines))
        self.assertEqual([x["draft_state"] for x in lines],
                         ["ready", "ready", "drafting", "queued"])
        self.assertEqual(lines[0]["draft"]["body"], "PO 4521 ships Friday.")
        self.assertEqual(lines[0]["drafted_by"], "model")
        self.assertEqual(lines[0]["draft_note"], "checked the PO's date")
        self.assertNotIn("draft_note", lines[3])
        self.assertNotIn("draft_warning", lines[0])
        self.assertEqual(lines[1]["your_call"]["ask"], "Ship Friday?")
        self.assertEqual(lines[3]["draft"]["body"], hold["body"])
        # another person's kept drafts never reach this reader's lines
        other = [{"object_id": "Email:r", "draft": dict(hold)}]
        rd.attach(other, rd.kept_for(self.tmp, [BEN], other))
        self.assertEqual(other[0]["draft_state"], "queued")
        # no state dir, nothing kept: the page still answers
        self.assertEqual(rd.kept_for(None, [ANA], lines), {})
        self.assertEqual(rd.kept_for(self.tmp / "none", [ANA], lines), {})

    def test_a_flagged_reply_is_kept_with_its_warning_on_the_line(self):
        got = rd.result_of([{"draft": "Order 777 ships Monday.", "note": "",
                             "warning": "Check before sending: it names another customer."}])
        state = got.pop("state")
        self.kept.mark(ANA, "Email:w", state, **got)
        lines = [{"object_id": "Email:w", "draft": {"body": "hold"}}]
        rd.attach(lines, rd.kept_for(self.tmp, [f"tenant:{ANA}"], lines))
        self.assertEqual(lines[0]["draft"]["body"], "Order 777 ships Monday.")
        self.assertEqual(lines[0]["draft_warning"], "Check before sending: it names another customer.")

    def test_line_draft_screens_first_then_the_assistant_reply_kept_with_note_and_warning(self):
        from unittest import mock
        from operating import autopilot as apm, autopilot_engine as eng
        from operating.autopilot_agent import Decision
        reg = {"autopilots": [{"id": "assistant"}], "limits": {"handler_seconds": 5}}
        ev = {"event_type": "message.received", "cleared": True, "object_id": "Email:r",
              "side": "internal", "sender": "bo@plx.example", "to": ["ana@plx.example"],
              "to_me": True, "asks": True}
        ran = []

        class Assistant:
            answer = Decision(reply="Ships [date].", note="date is yours")

            def run(self, event, owner, described, person, people, **kw):
                ran.append((owner, person, kw.get("reads")))
                got = self.answer
                return Decision(**{k: v for k, v in got.__dict__.items()}), ""
        a = Assistant()
        both = {ANA: {"ana@plx.example"}, "employee:BO": {"bo@plx.example"}}
        maps = ({}, both, ("plx.example",))
        with mock.patch.object(apm, "discover", return_value=([ev], {})), \
                mock.patch.object(eng, "_prepare", return_value={"work": {"material": {"text": "When?"}}}):
            self.assertEqual(rd.line_draft(None, reg, None, maps, a, ANA, "Email:r"),
                             [{"draft": "Ships [date].", "note": "date is yours"}])
            self.assertEqual(ran, [(ANA, "ana@plx.example", True)])    # reads as the person
            # the support check flagged it: a reviewed draft is kept with the warning
            a.answer = Decision(reply="Ships Friday.", warning="Check before sending: x")
            self.assertEqual(rd.line_draft(None, reg, None, maps, a, ANA, "Email:r"),
                             [{"draft": "Ships Friday.", "note": "",
                               "warning": "Check before sending: x"}])
            # a question alone is the model's own card
            a.answer = Decision(ask={"question": "Which day?", "options": []})
            self.assertEqual(rd.line_draft(None, reg, None, maps, a, ANA, "Email:r"),
                             [{"kind": "your_call", "ask": "Which day?", "options": []}])
            self.assertEqual(rd.line_draft(None, reg, None, maps, None, ANA, "Email:r"),
                             [{"why": "draft_model_off"}])
            ev["cleared"] = False
            self.assertEqual(rd.line_draft(None, reg, None, maps, a, ANA, "Email:r"),
                             [{"why": "participants_not_cleared"}])
            ev["cleared"] = True
            with mock.patch.object(eng, "pay_or_hr", return_value=True):
                self.assertEqual(rd.line_draft(None, reg, None, maps, a, ANA, "Email:r"),
                                 [{"why": "pay_or_hr"}])
            self.assertEqual(len(ran), 3)


if __name__ == "__main__":
    unittest.main()
