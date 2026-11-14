"""Learning (``operating.learning``, ``operating.learning_signals``): signals become per-person
and per-role lessons with provenance, confidence and decay, applied to autopilot drafts,
item preparation and alert ranking; the people exclusion and the finance rule hold.

SYNTHETIC fixtures only; evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from operating import autopilot as ap
from operating import briefing as bf
from operating import learning as L
from operating import learning_signals as ls
from operating.tests.test_autopilot import NOW, OURS_OID, Fixture

ANA = "11111111-1111-4111-8111-111111111111"
BEN = "22222222-2222-4222-8222-222222222222"
CAL = "33333333-3333-4333-8333-333333333333"
EXCLUDED = "99999999-9999-4999-8999-999999999999"
DRAFT = "Hi Sam,\n\nOrder SO-1 shipped Fri 16 Oct.\nThanks, I am reviewing this and will " \
        "confirm today.\n\nBest regards,"
CLEAR = L.Guard(clear=lambda who: EXCLUDED not in who and "excluded@" not in who)


def sent(i: int, person: str = ANA, *, at: datetime = NOW, draft: str = DRAFT,
         reply: str = "Hey Sam,\n\nOrder SO-1 shipped Fri 16 Oct.\nLet me know if you need "
                      "anything else.\n\nThanks,\nAna\n\nFrom: Sam\nSent: ...\nold text",
         sender: str = "sam@synth-customer.example", **data) -> L.Signal:
    feats = L.edit_features(draft, reply)
    return L.Signal(kind="draft_sent", person=person, at=L._iso(at - timedelta(days=i)),
                    ref=f"item:{person[:4]}-{i}", people=(sender,),
                    data={"autopilot": "reply_prep", "channel": "mail", "side": "customer",
                          "sender": L.sender_key(sender), "item": f"ap_{i}",
                          "reply": f"Email:{i}", "topic": L.topic_keys("shipment status order"),
                          **feats, **data})


def ignored(i: int, person: str = ANA, *, at: datetime = NOW, autopilot: str = "customer_po",
            side: str = "customer") -> L.Signal:
    return L.Signal(kind="draft_ignored", person=person, at=L._iso(at - timedelta(days=i)),
                    ref=f"item:ign-{person[:4]}-{autopilot}-{i}",
                    data={"autopilot": autopilot, "channel": "mail", "side": side})


_ENV = mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED})


def setUpModule() -> None:
    _ENV.start()
    os.environ.pop("PLX_EXCLUDED_PEOPLE_FILE", None)
    os.environ.pop(L.DIR_ENV, None)
    os.environ.pop(L.OFF_ENV, None)


def tearDownModule() -> None:
    _ENV.stop()


def lessons_of(signals, **kw) -> L.Lessons:
    kept, _ = L.admit(signals, CLEAR)
    return L.Lessons(L.distil(kept, now=NOW, **kw), kept)


class Distil(unittest.TestCase):
    def test_edit_features_keep_forms_and_generic_templates_never_figures(self):
        f = L.edit_features(DRAFT, sent(0).data and
                            "Hey Sam,\n\nOrder SO-1 shipped Fri 16 Oct.\nLet me know if you "
                            "need anything else.\n\nThanks,\nAna\n\nFrom: Sam\nquoted")
        self.assertEqual((f["greeting"], f["signoff"]), ("hey_name", "thanks"))
        self.assertEqual(f["added"], ["let me know if you need anything else."])
        self.assertEqual(f["dropped"], ["thanks, i am reviewing this and will confirm today."])
        # the sentence with the order number and date is a fact: never a template
        self.assertFalse(any("so-1" in t for t in f["added"] + f["dropped"] + f["kept"]))
        self.assertNotIn("quoted", json.dumps(f))

    def test_confidence_grows_with_repetition_falls_with_contradiction_and_decays(self):
        one = lessons_of([sent(1)])
        self.assertIsNone(one.get(ANA, (), "style", "greeting"))      # one reply: 0.4
        three = lessons_of([sent(i) for i in range(3)])
        greet = three.get(ANA, (), "style", "greeting")
        self.assertEqual(greet.value, "hey_name")
        self.assertGreaterEqual(greet.confidence, L.APPLY_AT)
        self.assertEqual(len(greet.evidence), 3)                       # provenance kept
        self.assertTrue(all(e["ref"].startswith("item:") for e in greet.evidence))
        mixed = lessons_of([sent(i) for i in range(3)] + [
            sent(10 + i, reply="Hi Sam,\n\nOrder SO-1 shipped.\n\nThanks,") for i in range(3)])
        self.assertIsNone(mixed.get(ANA, (), "style", "greeting"))    # 3 against 3
        old = lessons_of([sent(400 + i) for i in range(3)])            # a year and more ago
        self.assertIsNone(old.get(ANA, (), "style", "greeting"))
        self.assertLess(old.by[("person", ANA)]["style:greeting"].confidence,
                        greet.confidence)

    def test_unknown_signal_kind_and_unreadable_time_teach_nothing(self):
        bad = [L.Signal(kind="mystery", person=ANA, at=L._iso(NOW), ref="x"),
               L.Signal(kind="draft_sent", person=ANA, at="yesterday", ref="y")]
        kept, dropped = L.admit(bad, CLEAR)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, {"kind_unknown": 1, "time_unknown": 1})

    def test_cardinality_no_signal_lost_or_slow(self):
        for n in (1, 10, 60):
            signals = [sent(i % 50, f"{i:08d}-0000-4000-8000-000000000000") for i in range(n)]
            began = time.monotonic()
            kept, _ = L.admit(signals, CLEAR)
            lessons = L.distil(kept, now=NOW)
            self.assertEqual(len(kept), n)
            self.assertEqual(len({l.who for l in lessons if l.key == "greeting"}), n)
            self.assertLess(time.monotonic() - began, 5.0)


class Privacy(unittest.TestCase):
    def test_unavailable_exclusion_blocks_and_leaves_the_ledger_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = L.Ledger(Path(tmp))
            L.run(led, [sent(i) for i in range(3)], guard=CLEAR, now=NOW)
            before = [l.public() for l in led.lessons()]
            with self.assertRaises(L.Blocked):
                L.run(led, [sent(9)], guard=L.Guard(None), now=NOW)
            self.assertEqual([l.public() for l in led.lessons()], before)
            self.assertEqual(oct(os.stat(Path(tmp) / L.FILE).st_mode & 0o777), "0o600")
            led.close()

    def test_excluded_person_or_counterpart_is_never_learned_and_purged_later(self):
        kept, dropped = L.admit([sent(1, EXCLUDED), sent(2, sender="excluded@x.example")],
                                CLEAR)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, {"person_not_cleared": 1, "counterpart_not_cleared": 1})
        with tempfile.TemporaryDirectory() as tmp:
            led = L.Ledger(Path(tmp))
            L.run(led, [sent(i, BEN) for i in range(3)], guard=CLEAR, now=NOW)
            now_excluded = L.Guard(clear=lambda who: BEN not in who)
            out = L.run(led, [], guard=now_excluded, now=NOW)
            self.assertEqual(out["purged"], 3)
            self.assertEqual(led.lessons(), [])
            led.close()

    def test_a_counterpart_excluded_later_is_purged_and_never_an_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = L.Ledger(Path(tmp))
            L.run(led, [sent(i, sender="dana@synth-customer.example") for i in range(4)],
                  guard=CLEAR, now=NOW)
            led.close()
            dana_out = L.Guard(clear=lambda who: "dana@" not in who)
            seen = L.Lessons.load(Path(tmp), environ={}, guard=dana_out)
            self.assertEqual(dict(seen.examples), {})              # withheld at once
            self.assertEqual(len(L.Lessons.load(Path(tmp), environ={}, guard=CLEAR)
                                 .examples[ANA]), 4)
            led = L.Ledger(Path(tmp))
            out = L.run(led, [], guard=dana_out, now=NOW)
            self.assertEqual(out["purged"], 4)
            self.assertEqual(led.signals(), [])
            self.assertEqual(led.lessons(), [])
            led.close()

    def test_pay_and_finance_never_become_a_lesson(self):
        money = sent(1, reply="Hi Sam,\n\nThe invoice balance is settled.\nYour salary "
                              "review is next week.\n\nThanks,")
        self.assertEqual(money.data["added"], [])          # finance sentences: no template
        fact = L.Signal(kind="answer_corrected", person=ANA, at=L._iso(NOW), ref="f1",
                        data={"question": "what is the credit limit", "value": "$5,000",
                              "markings": []})
        served = L.Signal(kind="answer_rated", person=ANA, at=L._iso(NOW), ref="f2",
                          data={"rating": 1, "systems": ["bc"], "markings": ["finance"]})
        unknown = L.Signal(kind="answer_rated", person=ANA, at=L._iso(NOW), ref="f3",
                           data={"rating": 1, "systems": ["bc"], "markings": None})
        kept, dropped = L.admit([fact, served, unknown], CLEAR)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, {"finance": 1, "finance_marking": 2})

    def test_lessons_stay_per_person_and_roles_learn_only_shared_categorical_patterns(self):
        signals = [sent(i, ANA) for i in range(4)] + [sent(i, BEN) for i in range(4)] + [
            L.Signal(kind="answer_corrected", person=ANA, at=L._iso(NOW), ref="c1",
                     data={"question": "who ships the samples", "value": "Freight desk",
                           "markings": []})] * 3
        got = lessons_of(signals, roles={"purchasing": [ANA, BEN, CAL]})
        own_fact = [l for l in got.of(ANA) if l.kind == "fact"]
        self.assertEqual(len(own_fact), 1)
        self.assertEqual([l for l in got.of(BEN) if l.kind == "fact"], [])
        roles = {(l.kind, l.key) for (scope, _w), ls_ in got.by.items() if scope == "role"
                 for l in ls_.values()}
        self.assertIn(("style", "greeting"), roles)
        self.assertFalse(any(k == "fact" or key.startswith(("add:", "drop:", "sender:"))
                             for k, key in roles))
        # the role's greeting reaches a member with none of their own, never over their own
        self.assertEqual(got.get(CAL, ("purchasing",), "style", "greeting").value, "hey_name")
        self.assertIsNone(got.get(CAL, (), "style", "greeting"))
        # one person's pattern alone is never a role lesson
        solo = lessons_of([sent(i, ANA) for i in range(4)], roles={"purchasing": [ANA, BEN]})
        self.assertFalse(any(scope == "role" for scope, _w in solo.by))

    def test_a_forgotten_lesson_is_never_learned_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = L.Ledger(Path(tmp))
            L.run(led, [sent(i) for i in range(3)], guard=CLEAR, now=NOW)
            self.assertTrue(led.forget(ANA, "style", "greeting", NOW))
            L.run(led, [sent(i) for i in range(3, 9)], guard=CLEAR, now=NOW)
            self.assertNotIn("greeting", {l.key for l in led.lessons() if l.who == ANA})
            led.close()


class Apply(unittest.TestCase):
    def test_a_draft_takes_the_persons_frame_and_keeps_every_figure(self):
        got = lessons_of([sent(i) for i in range(4)])
        work = {"summary": "s", "draft": {"body": DRAFT, "to": [{"address": "x@y.example"}]}}
        out = got.apply_draft(work, ANA, event={"subject": "order shipment status"})
        body = out["draft"]["body"]
        self.assertTrue(body.startswith("Hey Sam,"))
        self.assertIn("Order SO-1 shipped Fri 16 Oct.", body)
        self.assertNotIn("reviewing this", body)                      # always taken out
        self.assertIn("Let me know if you need anything else.", body)  # nearly always added
        self.assertTrue(body.rstrip().endswith("Thanks,"))
        self.assertNotIn("Best regards", body)
        kinds = {l["key"].split(":")[0] for l in out["learned"]["lessons"]}
        self.assertEqual(kinds, {"greeting", "signoff", "add", "drop"})
        self.assertTrue(out["learned"]["examples"])                    # refs, never text
        self.assertEqual(set(out["learned"]["examples"][0]), {"item", "at", "match"})
        self.assertEqual(work["draft"]["body"], DRAFT)                 # the input untouched
        # another person's lessons never shape this person's draft
        self.assertEqual(got.apply_draft(work, BEN), work)

    def test_a_draft_is_never_emptied_and_a_chat_reply_keeps_its_form(self):
        got = lessons_of([sent(i) for i in range(4)])
        only = "Hi Sam,\n\nThanks, I am reviewing this and will confirm today."
        out = got.restyle(only, ANA, ())[0]
        self.assertIn("reviewing this", out)             # the only thing it says stays
        chat = {"teams_draft": {"body": DRAFT, "post": False}}
        self.assertEqual(got.apply_draft(chat, ANA)["teams_draft"]["body"], DRAFT)

    def test_a_line_with_a_figure_is_never_rewritten_by_a_dropped_sentence(self):
        got = lessons_of([sent(i) for i in range(4)])
        line = "Thanks, I am reviewing this and will confirm today. PO 4500.12 ships 11.20."
        body = f"Hi Sam,\n\n{line}\nOrder SO-1 shipped Fri 16 Oct."
        out = got.restyle(body, ANA, ())[0]
        self.assertIn(line, out)

    def test_colleagues_leaving_a_kind_never_skips_it_for_someone_else(self):
        got = lessons_of([ignored(i, ANA) for i in range(8)] + [ignored(i, BEN)
                                                                  for i in range(8)],
                         roles={"purchasing": [ANA, BEN, CAL]})
        event = {"channel": "mail", "side": "customer"}
        self.assertIsNotNone(got.skip_reason(ANA, ("purchasing",), "customer_po", event))
        self.assertIsNone(got.skip_reason(CAL, ("purchasing",), "customer_po", event))

    def test_a_late_act_replaces_left_for_the_same_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = L.Ledger(Path(tmp))
            L.run(led, [ignored(i) for i in range(8)], guard=CLEAR, now=NOW)
            event = {"channel": "mail", "side": "customer"}
            self.assertTrue(L.Lessons(led.lessons()).skip_reason(
                ANA, (), "customer_po", event).startswith("learned_left:"))
            acted = [L.Signal(kind="item_acted", person=ANA, at=L._iso(NOW - timedelta(days=i)),
                              ref=s.ref, data=dict(s.data))
                     for i, s in enumerate(ignored(i) for i in range(8))]
            out = L.run(led, acted, guard=CLEAR, now=NOW)
            self.assertEqual(out["taken"], 8)
            self.assertEqual({s.kind for s in led.signals()}, {"item_acted"})
            self.assertIsNone(L.Lessons(led.lessons()).skip_reason(
                ANA, (), "customer_po", event))
            again = L.run(led, [ignored(0)], guard=CLEAR, now=NOW)   # never back to left
            self.assertEqual(again["taken"], 0)
            led.close()

    def test_items_a_person_keeps_leaving_are_not_prepared_others_are(self):
        got = lessons_of([ignored(i) for i in range(8)])
        event = {"channel": "mail", "side": "customer", "sender": "b@s.example"}
        self.assertTrue(got.skip_reason(ANA, (), "customer_po", event).startswith(
            "learned_left:"))
        self.assertIsNone(got.skip_reason(ANA, (), "reply_prep", event))
        self.assertIsNone(got.skip_reason(BEN, (), "customer_po", event))
        few = lessons_of([ignored(i) for i in range(3)])               # too little evidence
        self.assertIsNone(few.skip_reason(ANA, (), "customer_po", event))
        acted = [L.Signal(kind="item_acted", person=ANA, at=L._iso(NOW - timedelta(days=i)),
                          ref=f"a{i}", data={"autopilot": "customer_po", "channel": "mail",
                                             "side": "customer"}) for i in range(6)]
        self.assertIsNone(lessons_of([ignored(i) for i in range(8)] + acted).skip_reason(
            ANA, (), "customer_po", event))

    def test_dismissed_alert_kinds_rank_after_the_rest_never_hidden(self):
        verdicts = [L.Signal(kind="alert_verdict", person=ANA, at=L._iso(NOW - timedelta(i)),
                             ref=f"fb{i}", data={"verdict": "not_useful",
                                                 "item_kind": "lot_near_expiry"})
                    for i in range(4)]
        got = lessons_of(verdicts)
        reader = f"synth-tenant:employee:{ANA}"
        weight, lesson = got.alert_weight(reader, "lot_near_expiry")
        self.assertLess(weight, 1.0)
        self.assertIsNotNone(lesson)
        self.assertEqual(got.alert_weight(reader, "po_late"), (1.0, None))
        self.assertEqual(got.alert_weight(f"t:employee:{BEN}", "lot_near_expiry"), (1.0, None))
        with tempfile.TemporaryDirectory() as tmp:
            led = L.Ledger(Path(tmp) / "learning")
            L.run(led, verdicts, guard=CLEAR, now=NOW)
            led.close()
            shown = [{"id": "g1", "kind": "lot_near_expiry"}, {"id": "g2", "kind": "eta_slipped"}]
            from operating import proactive_engine as pe
            store = Path(tmp) / "projection" / "store.sqlite"
            store.parent.mkdir()
            pe._learned_order(shown, store, reader)
            self.assertEqual([g["id"] for g in shown], ["g2", "g1"])     # both still there
            self.assertEqual(shown[1]["learned"]["lesson_id"], lesson)
            other = [{"id": "g1", "kind": "lot_near_expiry"}, {"id": "g2", "kind": "eta_slipped"}]
            pe._learned_order(other, store, f"t:employee:{BEN}")
            self.assertEqual([g["id"] for g in other], ["g1", "g2"])

    def test_briefing_rank_puts_a_dismissed_kind_after_its_tier(self):
        a = mock.Mock(yours=None, acknowledged=False, event_type="lot_near_expiry",
                      orders_affected=0, money_place=1, new_this_generation=True, order=0,
                      gap={"first_seen": "2026-10-19", "learned": {"weight": 0.4}})
        b = mock.Mock(yours=None, acknowledged=False, event_type="eta_slipped",
                      orders_affected=0, money_place=1, new_this_generation=True, order=1,
                      gap={"first_seen": "2026-10-18"})
        for m in (a, b):
            m.amount, m.suspect, m.disagree, m.basis = None, False, None, None
        with mock.patch.object(bf, "_urgency", return_value=0):
            ordered, _ = bf.rank([a, b])
        self.assertEqual(ordered, [b, a])

    def test_memory_items_are_the_persons_own_applied_lessons(self):
        got = lessons_of([sent(i) for i in range(4)] + [ignored(i) for i in range(8)])
        items = got.memory_items(ANA)
        subjects = {i["subject"] for i in items}
        self.assertIn("learn.style.greeting", subjects)
        self.assertFalse(any(s.startswith("learn.decision.sender:") for s in subjects))
        self.assertTrue(all(i["source"] == "learn" and 0 < i["confidence"] <= 1
                            for i in items))
        self.assertEqual(got.memory_items(BEN), [])
        calls = []
        mem = mock.Mock(remember=lambda reader, **kw: calls.append((reader, kw)) or 1)
        done = L.publish(got, mem, lambda p: "reader-" + p if p == ANA else None, [ANA, BEN])
        self.assertEqual(done, {"kept": len(items), "no_reader": 1})
        self.assertTrue(all(r == "reader-" + ANA for r, _ in calls))


class AskHandOff(unittest.TestCase):
    def test_the_live_reader_gets_only_their_own_lessons_once_per_ledger_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            led = L.Ledger(state / "learning")
            fact = [L.Signal(kind="answer_corrected", person=ANA, at=L._iso(NOW - timedelta(i)),
                             ref=f"c{i}", data={"question": "who carries the samples",
                                                "value": "Synth Freight", "markings": []})
                    for i in range(3)]
            L.run(led, [sent(i) for i in range(4)] + [sent(i, BEN) for i in range(4)] + fact,
                  guard=CLEAR, now=NOW)
            led.close()
            calls = []
            mem = mock.Mock(remember=lambda reader, **kw: calls.append((reader, kw)) or 1)
            ana = mock.Mock(key=f"t:employee:{ANA},t:group:x")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(L.DIR_ENV, None)
                first = L.sync_memory(mem, ana, state)
                again = L.sync_memory(mem, ana, state)
                rule = L.ask_rule(ana.key, state)
                other = L.ask_rule(f"t:employee:{CAL}", state)
            self.assertTrue(first["kept"] >= 2)
            self.assertIsNone(again)                                  # nothing new
            self.assertTrue(all(r is ana for r, _ in calls))
            self.assertIn("Synth Freight", rule)
            self.assertEqual(other, "")
            broken = mock.Mock(remember=mock.Mock(side_effect=RuntimeError("down")))
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(L.DIR_ENV, None)
                got = L.sync_memory(broken, mock.Mock(key="t:employee:" + BEN), state)
            self.assertGreater(got["failed"], 0)                      # never raised


class Signals(unittest.TestCase):
    def test_feedback_store_gives_thumbs_corrections_and_alert_verdicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            from operator_api.feedback import FeedbackStore
            store = FeedbackStore(Path(tmp))
            reader = f"synth-tenant:employee:{ANA}"
            store.add({"kind": "answer", "reader": reader, "rating": -1,
                       "question": "who is the carrier for SO-1", "expected": "Synth Freight",
                       "context": {"markings": [], "claims": [{"citations": [
                           {"system": "mail", "handle": "h"}]}]}})
            store.add({"kind": "alert", "reader": reader, "verdict": "not_useful",
                       "item_id": "g1", "context": {"event_type": "lot_near_expiry",
                                                    "item_kind": "briefing"},
                       "object_type": "Lot"})
            store.add({"kind": "answer", "reader": "hidden", "rating": 1, "question": "q",
                       "context": {"reader_hidden": True, "markings": []}})
            store.close()
            got = ls.from_feedback(Path(tmp))
        kinds = sorted(s.kind for s in got)
        self.assertEqual(kinds, ["alert_verdict", "answer_corrected", "answer_rated"])
        self.assertTrue(all(s.person == ANA for s in got))
        verdict = next(s for s in got if s.kind == "alert_verdict")
        self.assertEqual(verdict.data["item_kind"], "lot_near_expiry")

    def test_asks_in_a_row_give_corrections_and_reasks(self):
        reader = f"t:employee:{ANA}"
        turns = [{"reader": reader, "at": "2026-10-19T10:00:00Z", "ref": 1, "markings": [],
                  "question": "when does the next soap shipment for Synth leave"},
                 {"reader": reader, "at": "2026-10-19T10:02:00Z", "ref": 2, "markings": [],
                  "question": "no, it's the Tuesday truck"},
                 {"reader": reader, "at": "2026-10-19T11:00:00Z", "ref": 3, "markings": [],
                  "question": "which lot of hand soap is on quality hold"},
                 {"reader": reader, "at": "2026-10-19T11:03:00Z", "ref": 4, "markings": [],
                  "question": "hand soap lot on quality hold which one"},
                 {"reader": reader, "at": "2026-10-19T15:00:00Z", "ref": 5, "markings": [],
                  "question": "hand soap lot quality hold which"}]       # hours later: new ask
        got = ls.from_asks(turns)
        self.assertEqual([(s.kind, s.ref) for s in got],
                         [("answer_corrected", "ask:2:correction"), ("reasked", "ask:4:reask")])
        self.assertEqual(got[0].data["value"], "the Tuesday truck")

    def test_queue_items_pair_drafts_with_replies_and_mark_what_was_left(self):
        items = [
            {"item_id": "i1", "event_id": "e1", "owner": ANA, "autopilot": "reply_prep",
             "state": "done", "done_reason": "owner_replied", "due_at": "2026-10-10T00:00:00Z",
             "created_at": "2026-10-09T00:00:00Z",
             "body": {"prepared": {"draft": {"body": DRAFT}}, "happened": {}}},
            {"item_id": "i2", "event_id": "e2", "owner": ANA, "autopilot": "customer_po",
             "state": "open", "due_at": "2026-10-10T00:00:00Z",
             "created_at": "2026-10-09T00:00:00Z", "body": {"prepared": {}, "happened": {}}},
            {"item_id": "i3", "event_id": "e3", "owner": ANA, "autopilot": "customer_po",
             "state": "open", "due_at": L._iso(NOW), "created_at": L._iso(NOW),
             "body": {"prepared": {}, "happened": {}}},
            {"item_id": "i4", "event_id": "e4", "owner": ANA, "autopilot": "approval_request",
             "state": "done", "done_reason": "approval_decided", "done_at": "2026-10-11T00:00:00Z",
             "due_at": "2026-10-10T00:00:00Z", "created_at": "2026-10-09T00:00:00Z",
             "body": {"prepared": {}, "happened": {}}}]
        events = {"e1": {"channel": "mail", "side": "customer", "sender": "sam@s.example",
                         "subject": "Order status", "conversation_id": "c1"}}
        got = ls.item_signals(items, events, now=NOW, reply_of=lambda i, e: (
            "Email:9", "2026-10-09T12:00:00Z", "Hey Sam,\n\nOrder SO-1 shipped.\n\nThanks,"))
        self.assertEqual([(s.kind, s.ref) for s in got],
                         [("draft_sent", "item:i1"), ("draft_ignored", "item:i2"),
                          ("item_acted", "item:i4")])
        self.assertEqual(got[0].data["reply"], "Email:9")
        self.assertNotIn("Order SO-1 shipped", json.dumps(got[0].data))  # features only


class AutopilotUsesLessons(Fixture):
    """The live queue's shape (``<state>/autopilot/autopilot.sqlite``): a replay's queue
    never takes lessons."""

    def setUp(self):
        super().setUp()
        # the fixture is a SQLite store: a Postgres-only reader setting (the
        # landing runs PLX_ONTOLOGY_PG=only) would refuse it before the cycle runs
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PLX_ONTOLOGY_PG", None)

    def cycle(self, ids, **kw):
        from operating import proactive_engine as pe
        from operating.tests.test_proactive_threads import people
        path = self.dir / "s.sqlite"
        if not path.exists():
            self.w.build(path)
        st = pe._Store(path, threads=1)
        q = ap.Queue(self.dir / "autopilot" / ap.QUEUE_FILE)
        try:
            return ap.run_cycle(st, q, ids, people=people(), environ=self.env, now=NOW,
                                landed_at=ap._iso(NOW - timedelta(minutes=2)), **kw)
        finally:
            st.close()
            q.close()

    def queue(self) -> ap.Queue:
        q = ap.Queue(self.dir / "autopilot" / ap.QUEUE_FILE)
        self.addCleanup(q.close)
        return q

    def test_a_replay_queue_never_takes_lessons(self):
        msg = self._po()
        left = self._ledger([ignored(i, OURS_OID) for i in range(8)])
        with mock.patch.dict(os.environ, {L.DIR_ENV: str(left)}):
            out = Fixture.cycle(self, [msg])           # the queue under "q/": a replay's
        self.assertEqual(out["prepared"], {"customer_po": 1})

    def test_lessons_are_not_applied_once_the_exclusion_is_unavailable_or_covers_them(self):
        msg = self._po()
        left = self._ledger([ignored(i, OURS_OID) for i in range(8)])
        self.assertTrue(L.Lessons.load(left))
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": ""}):
            self.assertFalse(L.Lessons.load(left))
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": OURS_OID}):
            self.assertFalse(L.Lessons.load(left).of(OURS_OID))
            with mock.patch.dict(os.environ, {L.DIR_ENV: str(left)}):
                out = self.cycle([msg])
        self.assertNotIn("customer_po:learned_left", out["skipped"])

    def _ledger(self, signals) -> Path:
        directory = self.dir / "learning"
        led = L.Ledger(directory)
        L.run(led, signals, guard=CLEAR, now=NOW)
        led.close()
        return directory

    def test_prepared_draft_carries_the_owners_lessons_and_left_kinds_are_skipped(self):
        self.order("PO-4512", on_hand="5", need="10")
        msg = self.message(1, "Please find attached our PO PO-4512 for 10 cases.",
                           subject="New PO PO-4512")
        styled = self._ledger([sent(i, OURS_OID) for i in range(4)])
        with mock.patch.dict(os.environ, {L.DIR_ENV: str(styled)}):
            self.cycle([msg])
        (item,) = self.queue().items()
        work = item["body"]["prepared"]
        self.assertIn("It can ship Sun 25 Oct.", work["draft"]["body"])   # the fact as is
        self.assertTrue(work["draft"]["body"].rstrip().endswith("Thanks,"))
        self.assertTrue(work["learned"]["lessons"])
        # the card the owner confirms carries the restyled body
        self.assertEqual(ap.card_of(item)["draft"]["body"], work["draft"]["body"])

    def _po(self) -> str:
        self.order("PO-4512", on_hand="5", need="10")
        return self.message(1, "Please find attached our PO PO-4512 for 10 cases.",
                            subject="New PO PO-4512")

    def test_a_kind_the_owner_keeps_leaving_is_not_prepared(self):
        msg = self._po()
        left = self._ledger([ignored(i, OURS_OID) for i in range(8)])
        with mock.patch.dict(os.environ, {L.DIR_ENV: str(left)}):
            out = self.cycle([msg])
        self.assertEqual(out["prepared"], {})
        self.assertEqual(out["skipped"]["customer_po:learned_left"], 1)
        self.assertEqual(self.queue().items(), [])

    def test_a_failing_lesson_never_stops_the_cycle(self):
        msg = self._po()
        styled = self._ledger([sent(i, OURS_OID) for i in range(4)])
        with mock.patch.dict(os.environ, {L.DIR_ENV: str(styled)}), \
                mock.patch.object(L.Lessons, "apply_draft", side_effect=RuntimeError("x")), \
                mock.patch.object(L.Lessons, "skip_reason", side_effect=RuntimeError("y")):
            out = self.cycle([msg])
        self.assertEqual(out["prepared"], {"customer_po": 1})
        (item,) = self.queue().items()
        self.assertNotIn("learned", item["body"]["prepared"])

    def test_the_off_switch_applies_nothing(self):
        msg = self._po()
        left = self._ledger([ignored(i, OURS_OID) for i in range(8)])
        with mock.patch.dict(os.environ, {L.DIR_ENV: str(left), L.OFF_ENV: "1"}):
            out = self.cycle([msg])
        self.assertEqual(out["prepared"], {"customer_po": 1})
        (item,) = self.queue().items()
        self.assertNotIn("learned", item["body"]["prepared"])

if __name__ == "__main__":
    unittest.main()


class ReplayMeasure(unittest.TestCase):
    def test_training_never_reads_an_outcome_inside_the_window(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "learning_replay", Path(__file__).resolve().parents[2] / "scripts" /
            "learning_replay.py")
        lr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lr)
        start = datetime(2026, 11, 6, tzinfo=timezone.utc)
        items, events, sent_rows = {}, {}, {ANA: []}
        # 8 items left well before the window, 6 left whose 72 h ran into it
        for i, at in enumerate([start - timedelta(days=10 + d) for d in range(8)]
                               + [start - timedelta(hours=10 + h) for h in range(6)]):
            eid = f"e{i}"
            events[eid] = {"at": L._iso(at), "conversation_id": f"c{i}", "channel": "mail",
                           "side": "customer"}
            items[(eid, ANA)] = {"item_id": f"i{i}", "event_id": eid, "owner": ANA,
                                 "autopilot": "customer_po", "created_at": L._iso(at),
                                 "body": {"prepared": {}, "happened": {}}}
        sent_rows[ANA] = [("x", L._iso(start + timedelta(days=9)))]   # data reach
        out = lr.measure(items, events, sent_rows, window=(start, start + timedelta(days=6)),
                         held=[(start, start + timedelta(days=6))],
                         horizon=timedelta(hours=72), reply_of=lambda i, e: None,
                         roles={}, any_train_day=False, guard=CLEAR)
        self.assertEqual(out["train_items"], 8)
        self.assertTrue(out["time_forward"])
        self.assertEqual(out["lessons_as_of"], L._iso(start))
