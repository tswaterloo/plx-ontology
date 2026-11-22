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

    def test_a_line_without_a_session_is_tried_again_soon_a_judged_none_late(self):
        now = datetime.now(timezone.utc)
        hour = (now - timedelta(hours=1)).isoformat()
        self.assertTrue(rd.due({"state": "none", "why": "agent_no_session", "at": hour}, now))
        self.assertFalse(rd.due({"state": "none", "why": "agent_no_session",
                                 "at": now.isoformat()}, now))
        self.assertTrue(rd.due({"state": "none", "why": "draft_failed:HostListRefused",
                                "at": hour}, now))
        for why in ("nothing_to_do", "excluded_in_thread", "draft_failed:OSError"):
            self.assertFalse(rd.due({"state": "none", "why": why, "at": hour}, now), why)
        # past the retry: a failure or a screen is drafted again; the message's own judgment
        # (nothing to send, the reply sort's, only changes kept) stays like a ready draft
        late = (now - timedelta(hours=rd.RETRY_HOURS, minutes=1)).isoformat()
        for why in ("excluded_in_thread", "draft_failed:OSError", "no_draft", ""):
            self.assertTrue(rd.due({"state": "none", "why": why, "at": late}, now), why)
        for why in ("nothing_to_do", "no_reply_wanted", "changes_prepared"):
            self.assertFalse(rd.due({"state": "none", "why": why, "at": "2020-01-01T00:00:00"
                                     "+00:00"}, now), why)
        lines = [(ANA, "Email:j", 3), (ANA, "Email:f", 2)]
        self.kept.mark(ANA, "Email:j", "none", why="no_reply_wanted")
        self.kept.mark(ANA, "Email:f", "none", why="draft_failed:OSError")
        self.assertEqual(rd.wanted(lines, self.kept, now=now + timedelta(days=2)),
                         [(ANA, "Email:f")])

    def test_the_kept_store_is_readable_by_the_shared_group_the_api_reads_with(self):
        import os, stat
        state = self.tmp / "state"
        state.mkdir()
        os.chmod(state, 0o3770)   # the shared state root: setgid + sticky, group rwx
        old = os.umask(0o077)     # the jobs' private umask
        try:
            kept = rd.Kept(rd.path_of(state))
            kept.mark(ANA, "Email:1", "ready", kind="reply", body="Yes.")
        finally:
            os.umask(old)
        d, f = rd.path_of(state).parent, rd.path_of(state)
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode) & 0o060, 0o060)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode) & 0o070, 0o070)
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode) & 0o007, 0)
        self.assertTrue(os.stat(d).st_mode & stat.S_ISGID)
        self.assertIn("reply-drafts", (Path(__file__).resolve().parents[2] / "deploy" / "release"
                                       / "api_user_setup.sh").read_text().split("R_DIRS=(")[1]
                      .split(")")[0])

    def test_the_reply_sort_is_haiku_unless_set_or_off(self):
        self.assertEqual(rd.screen_model({}), "haiku")
        self.assertEqual(rd.screen_model({rd.SCREEN_ENV: "sonnet"}), "sonnet")
        for off in ("off", "none", "0", " OFF "):
            self.assertIsNone(rd.screen_model({rd.SCREEN_ENV: off}), off)

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

    def test_line_draft_reads_the_persons_habit_and_that_the_line_is_theirs_to_answer(self):
        from unittest import mock
        from operating import autopilot as apm, autopilot_engine as eng
        from operating.autopilot_agent import Decision
        reg = {"autopilots": [{"id": "assistant"}], "limits": {"handler_seconds": 5}}
        ev = {"event_type": "message.received", "cleared": True, "object_id": "Email:r",
              "channel": "mail", "mailbox": ANA, "at": "2026-11-17T14:00:00Z",
              "conversation_id": "T1", "sender": "bo@plx.example", "to": ["cy@plx.example"],
              "cc": ["ana@plx.example"]}
        seen = []

        class Assistant:
            def run(self, event, owner, described, person, people, **kw):
                seen.append(described)
                events.append(event)
                return Decision(reply="On it."), ""
        events = []

        class History:      # the live queue's reads, known before the message
            def __init__(self, fail=False):
                self.fail, self.asked = fail, []

            def engagement(self, owner, conv, sender, at):
                if self.fail:
                    raise sqlite3.OperationalError("locked")
                self.asked.append(("engagement", owner, conv, sender, at))
                return {"engaged": True, "correspondent": True}

            def reply_history(self, owner, sender, at, **kw):
                return (9, 2)

            def own_posts(self, owner, conv, at):
                return (30.0, 3)
        maps = ({}, {ANA: {"ana@plx.example"}}, ("plx.example",))
        hist = History()
        with mock.patch.object(apm, "discover", side_effect=lambda *a: ([dict(ev)], {})), \
                mock.patch.object(eng, "_prepare", return_value={"work": {"material": {"text": "Who?"}}}):
            self.assertEqual(rd.line_draft(None, reg, None, maps, Assistant(), ANA, "Email:r",
                                           history=hist), [{"draft": "On it.", "note": ""}])
            self.assertEqual(hist.asked, [("engagement", ANA, "T1", "bo@plx.example",
                                           "2026-11-17T14:00:00Z")])
            self.assertIn("the person answered 2 of the last 9 messages this sender sent them",
                          seen[-1])
            self.assertIn("they wrote in this conversation 3 time(s) recently", seen[-1])
            self.assertIn(rd.OWED.format(person="ana@plx.example"), seen[-1])
            # copied, never answering here before: no reply is due by the rules, but the line
            # on their replies to write says it is (the gate keeps the session's reply)
            from operating.autopilot_agent_io import reply_due
            self.assertEqual(reply_due({**ev, "addressed_other": True}), "")
            self.assertEqual(reply_due({**events[-1], "addressed_other": True}), "turn")
            self.assertNotIn("owed", ev)
            self.assertEqual(reply_due({**events[-1], "item_kind": "auto_reply"}), "")
            # the queue is the history when none is given; a failing read costs only the line
            rd.line_draft(None, reg, None, maps, Assistant(), ANA, "Email:r", queue=History())
            self.assertIn("History:", seen[-1])
            self.assertEqual(rd.line_draft(None, reg, None, maps, Assistant(), ANA, "Email:r",
                                           history=History(fail=True)),
                             [{"draft": "On it.", "note": ""}])
            self.assertNotIn("History:", seen[-1])
            self.assertIn(rd.OWED.format(person="ana@plx.example"), seen[-1])

    def test_a_sender_the_person_said_never_to_is_not_drafted_and_again_is(self):
        from unittest import mock
        from operating import autopilot as apm, autopilot_engine as eng
        from operating.autopilot_agent import Decision
        from operating.card_actions import Journal
        reg = {"autopilots": [{"id": "assistant"}], "limits": {"handler_seconds": 5}}
        ev = {"event_type": "message.received", "cleared": True, "object_id": "Email:r",
              "side": "internal", "sender": "Bo@plx.example", "to": ["ana@plx.example"],
              "to_me": True, "asks": True}
        ran = []

        class Assistant:
            def run(self, event, owner, described, person, people, **kw):
                ran.append(owner)
                return Decision(reply="Will do."), ""
        maps = ({}, {ANA: {"ana@plx.example"}}, ("plx.example",))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / rd.NEVER_FILE
            path.parent.mkdir(parents=True)

            def say(never, ids):
                with Journal(path).locked() as held:
                    held.append({"event": "never_sender", "sender": "bo@plx.example",
                                 "never": never, "by": ids[0], "ids": ids})

            def draft():
                return rd.line_draft(None, reg, None, maps, Assistant(), ANA, "Email:r",
                                     state_dir=Path(tmp))
            with mock.patch.object(apm, "discover", return_value=([ev], {})), \
                    mock.patch.object(eng, "_prepare",
                                      return_value={"work": {"material": {"text": "When?"}}}):
                say(True, ["employee:BEN"])                  # someone else's never
                self.assertEqual(draft(), [{"draft": "Will do.", "note": ""}])
                say(True, ["ana@plx.example", "tenant:" + ANA.upper()])
                self.assertEqual(draft(), [{"why": "never_sender"}])
                self.assertEqual(rd.never_senders(tmp, ANA), {"bo@plx.example"})
                say(False, [ANA])
                self.assertEqual(draft(), [{"draft": "Will do.", "note": ""}])
            self.assertEqual(ran, [ANA, ANA])

    def test_line_draft_keeps_proposed_changes_as_the_persons_job_item(self):
        from unittest import mock
        from operating import autopilot as apm, autopilot_engine as eng
        from operating.autopilot_agent import Decision
        reg = {"autopilots": [{"id": "assistant", "label": "Assistant"}],
               "limits": {"handler_seconds": 5, "per_owner_per_hour": 2}}
        ev = {"event_id": "ev1", "event_type": "message.received", "cleared": True,
              "object_id": "Email:r", "side": "internal", "sender": "bo@plx.example",
              "to": ["ana@plx.example"], "to_me": True, "asks": True, "subject": "PO date"}
        po = {"system": "portal", "operation": "purchase-order.update",
              "target": {"id": "cmpo0001"}, "change": {"expectedDate": "2026-11-20"},
              "reason": "The supplier confirmed delivery on 20 November."}
        move = {"tool": "plx.prepare_write", "arguments": po}
        unknown = {"tool": "plx.send_now", "arguments": po}

        class Assistant:
            answer = Decision(reply="Will do.", summary="Move the PO date",
                              proposals=[move, unknown])

            def run(self, event, owner, described, person, people, **kw):
                return Decision(**dict(self.answer.__dict__)), ""
        a = Assistant()
        maps = ({}, {ANA: {"ana@plx.example"}, "employee:BO": {"bo@plx.example"}},
                ("plx.example",))
        now = datetime(2026, 11, 20, 9, tzinfo=timezone.utc)
        queue = apm.Queue(self.tmp / "autopilot" / apm.QUEUE_FILE)
        self.addCleanup(queue.close)
        prep = {"work": {"material": {"text": "When?"}}, "audiences": ["tenant:" + ANA],
                "citations": ["mail://Email:r"]}

        def draft():
            return rd.line_draft(None, reg, None, maps, a, ANA, "Email:r", now=now, queue=queue)
        with mock.patch.object(apm, "discover", return_value=([ev], {})), \
                mock.patch.object(eng, "_prepare", return_value=prep):
            # no queue: the reply as before, nothing kept
            self.assertEqual(rd.line_draft(None, reg, None, maps, a, ANA, "Email:r", now=now),
                             [{"draft": "Will do.", "note": ""}])
            self.assertEqual(queue.items(), [])
            got = draft()
            self.assertEqual(got[0]["draft"], "Will do.")
            [item] = queue.items()
            self.assertEqual(got[0]["jobs"], item["item_id"])
            self.assertEqual(item["owner"], ANA)
            self.assertEqual(item["autopilot"], "assistant")
            self.assertEqual(item["state"], "open")
            self.assertEqual(item["audiences"], ["tenant:" + ANA])
            self.assertEqual(item["citations"], ["mail://Email:r"])
            # only a proposal the write path takes is kept; it waits for the person's confirm
            self.assertEqual(item["body"]["prepared"],
                             {"kind": "job", "summary": "Move the PO date",
                              "jobs": [move], "drafted_by": "model"})
            # a redraft of the line keeps the one item, and spends no more of the cap
            self.assertEqual(draft()[0]["jobs"], item["item_id"])
            self.assertEqual(len(queue.items()), 1)
            # changes alone, with no reply: the line says so, the item is the same one
            a.answer = Decision(summary="Move the PO date", proposals=[move])
            self.assertEqual(draft(), [{"why": "changes_prepared", "jobs": item["item_id"]}])
            # a question card carries them too
            a.answer = Decision(ask={"question": "Which day?", "options": []}, proposals=[move])
            self.assertEqual(draft()[0]["jobs"], item["item_id"])
            # nothing the write path takes: no item
            ev["event_id"] = "ev2"
            a.answer = Decision(reply="Sure.", proposals=[unknown])
            self.assertEqual(draft(), [{"draft": "Sure.", "note": ""}])
            # the kill switch: no item
            a.answer = Decision(reply="Sure.", proposals=[move])
            queue.killed_file.touch()
            self.assertEqual(draft(), [{"draft": "Sure.", "note": ""}])
            queue.killed_file.unlink()
            # the person's hourly cap (2 here, one spent): the next line's changes wait
            self.assertIn("jobs", draft()[0])
            ev["event_id"] = "ev3"
            self.assertEqual(draft(), [{"draft": "Sure.", "note": ""}])
            self.assertEqual(sorted(i["event_id"] for i in queue.items()), ["ev1", "ev2"])
            # things to do by hand alone (no write path): a to-do item, nothing to confirm
            ev["event_id"] = "ev5"
            queue.conn.execute("DELETE FROM rate")
            todo = {"system": "QuickBooks", "record": "INV-9", "what": "Mark INV-9 paid."}
            a.answer = Decision(reply="Sure.", todos=[todo, {"system": "x", "what": " "}])
            got = draft()
            done = queue.item(got[0]["jobs"])
            self.assertEqual(done["body"]["prepared"]["jobs"], [])
            self.assertEqual(done["body"]["prepared"]["todos"], [todo])
            # a queue that cannot be written: the draft stands, without the changes
            with mock.patch.object(queue, "add_item", side_effect=sqlite3.OperationalError):
                ev["event_id"] = "ev4"
                a.answer = Decision(reply="Sure.", proposals=[move])
                queue.conn.execute("DELETE FROM rate")
                self.assertEqual(draft(), [{"draft": "Sure.", "note": ""}])


if __name__ == "__main__":
    unittest.main()
