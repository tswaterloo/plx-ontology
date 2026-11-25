"""Regression guards for learning (``operating.learning``, ``learning_fewshot``,
``operator_api.learning_memory``): the evidence floor of a learned skip at its exact edge,
stale and future evidence, duplicate signals, fail-closed privacy, per-person isolation,
view-as, concurrent ledger writes and hand-overs, and bad ledger rows.

Each test here was checked against a broken version of the product code (see the commit
that added this file)."""

from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from operating import learning as L
from operating import learning_fewshot as fs
from operating.tests.test_autopilot import NOW
from operating.tests.test_learning import ANA, BEN, CLEAR, EXCLUDED, lessons_of, sent
from operating.tests.test_learning_fewshot import event, replied, work

_ENV = mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED})


def setUpModule() -> None:
    _ENV.start()
    os.environ.pop("PLX_EXCLUDED_PEOPLE_FILE", None)
    os.environ.pop(L.DIR_ENV, None)
    os.environ.pop(L.OFF_ENV, None)


def tearDownModule() -> None:
    _ENV.stop()


EV = {"channel": "mail", "side": "customer"}


def leave(n: int, *, days: float = 0.0, person: str = ANA, ref: str = "") -> L.Signal:
    """A prepared customer_po item left, ``days`` before NOW."""
    return L.Signal(kind="draft_ignored", person=person,
                    at=L._iso(NOW - timedelta(days=days)),
                    ref=ref or f"item:left-{person[:4]}-{n}",
                    data={"autopilot": "customer_po", "channel": "mail", "side": "customer"})


def decision(lessons: L.Lessons, person: str = ANA) -> L.Lesson:
    return lessons.by[("person", person)]["decision:decision:customer_po|mail|customer"]


def ledger_with(tmp: str, signals, roles=None) -> Path:
    directory = Path(tmp) / "learning"
    led = L.Ledger(directory)
    L.run(led, signals, guard=CLEAR, now=NOW, roles=roles)
    led.close()
    return directory


class NoPlaceholders(unittest.TestCase):
    """A decision lesson names only the parts a signal states: never "unknown" (the owner saw
    "Usually acts on prepared items (job_approval, unknown, unknown)")."""

    def acts(self, data, n=4):
        return [L.Signal(kind="item_acted", person=ANA, at=L._iso(NOW - timedelta(days=1)),
                         ref=f"item:act-{i}", data=data) for i in range(n)]

    def test_an_approval_with_no_channel_or_side_is_about_its_workflow_alone(self):
        got = lessons_of(self.acts({"autopilot": "job_approval"}))
        text = [l.text for l in got.by[("person", ANA)].values() if l.kind == "decision"]
        self.assertTrue(text, got.by)
        self.assertEqual(text, ["Usually acts on prepared items (job approval)"])
        self.assertFalse(any("unknown" in k for k in got.by[("person", ANA)]))

    def test_a_signal_that_names_no_workflow_teaches_nothing_about_a_kind(self):
        got = lessons_of(self.acts({"channel": "mail"}))
        self.assertEqual([k for k in got.by.get(("person", ANA), {}) if k.startswith("decision:")], [])


class SkipEdges(unittest.TestCase):
    def test_three_leaves_are_exactly_the_evidence_floor_two_are_not(self):
        three = lessons_of([leave(i) for i in range(3)])
        self.assertEqual(decision(three).weight, L.SKIP_MIN)       # exactly at the floor
        self.assertTrue(three.skip_reason(ANA, (), "customer_po", EV))
        two = lessons_of([leave(i) for i in range(2)])
        self.assertIsNone(two.skip_reason(ANA, (), "customer_po", EV))

    def test_three_old_leaves_are_not_three_leaves_of_evidence(self):
        # confident enough (all leaves), but decayed below the floor: not skipped
        old = lessons_of([leave(i, days=10) for i in range(3)])
        lesson = decision(old)
        self.assertGreaterEqual(lesson.confidence, L.SKIP_AT)
        self.assertLess(lesson.weight, L.SKIP_MIN)
        self.assertIsNone(old.skip_reason(ANA, (), "customer_po", EV))

    def test_a_stale_style_lesson_no_longer_shapes_a_draft(self):
        stale = lessons_of([sent(i) for i in range(400, 404)])
        self.assertTrue(all(l.confidence < L.APPLY_AT for l in stale.of(ANA)))
        w = {"draft": {"body": "Hi Sam,\n\nOrder SO-1 shipped.\n\nBest regards,"}}
        self.assertEqual(stale.apply_draft(w, ANA), w)
        self.assertEqual(stale.memory_items(ANA), [])

    def test_signals_from_after_now_teach_nothing(self):
        future = [leave(i, days=-1) for i in range(6)] + [
            sent(-i - 1) for i in range(4)]
        self.assertEqual(L.distil(future, now=NOW), [])

    def test_malformed_signal_data_never_breaks_distil(self):
        odd = [L.Signal(kind="draft_sent", person=ANA, at=L._iso(NOW), ref=f"odd{i}",
                        data={"words": w, "systems": None, "added": None})
               for i, w in enumerate((None, "12", -3, 0))]
        got = L.distil(odd, now=NOW)
        self.assertNotIn("length", {l.key for l in got})
        self.assertIn("greeting", {l.key for l in got})


class Duplicates(unittest.TestCase):
    def test_the_same_leave_taken_again_and_again_is_one_leave(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "learning"
            led = L.Ledger(directory)
            taken = [L.run(led, [leave(0, ref="item:same")] * 3, guard=CLEAR, now=NOW)["taken"]
                     for _ in range(3)]
            self.assertEqual(taken, [1, 0, 0])
            (lesson,) = [l for l in led.lessons() if l.kind == "decision"]
            self.assertEqual((lesson.seen, lesson.weight), (1, 1.0))
            led.close()
            got = L.Lessons.load(directory, guard=CLEAR)
            self.assertIsNone(got.skip_reason(ANA, (), "customer_po", EV))


class Privacy(unittest.TestCase):
    def test_an_exclusion_that_cannot_load_blocks_even_with_a_people_directory(self):
        people = mock.Mock(identity_clear=lambda who: True)
        with mock.patch("operating.pg.permissions.load_exclusion",
                        side_effect=RuntimeError("unreadable")):
            guard = L.Guard.from_env(people)
        self.assertFalse(guard.available)
        self.assertFalse(guard.clear(ANA))
        with self.assertRaises(L.Blocked):
            L.admit([sent(1)], guard)

    def test_an_undecidable_identity_is_not_clear(self):
        ex = mock.Mock(unavailable=None, is_excluded=mock.Mock(side_effect=ValueError("?")))
        self.assertFalse(L.Guard(ex).clear(ANA))
        self.assertFalse(L.Guard(clear=mock.Mock(side_effect=KeyError("x"))).clear(ANA))
        everyone = L.Guard(clear=lambda who: True)
        self.assertFalse(everyone.clear("  "))
        kept, dropped = L.admit([leave(0, person="")], everyone)
        self.assertEqual((kept, dropped["person_not_cleared"]), ([], 1))

    def test_an_unavailable_exclusion_applies_no_lesson_not_even_a_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = ledger_with(tmp, [sent(i, ANA) for i in range(4)]
                                    + [sent(i, BEN) for i in range(4)],
                                    roles={"purchasing": [ANA, BEN]})
            warm = L.Lessons.load(directory, guard=CLEAR)            # the cache is warm
            self.assertIn(("role", "purchasing"), warm.by)
            cold = L.Lessons.load(directory, guard=L.Guard(None))
            self.assertFalse(cold)
            self.assertIsNone(cold.get("someone", ("purchasing",), "style", "greeting"))

    def test_a_person_excluded_after_learning_has_no_examples_or_lessons_at_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = ledger_with(tmp, [replied(i) for i in range(1, 5)]
                                    + [replied(i, person=BEN) for i in range(1, 5)])
            both = L.Lessons.load(directory, guard=CLEAR)
            self.assertTrue(both.examples.get(ANA))
            not_ana = L.Guard(clear=lambda who: ANA not in who)
            got = L.Lessons.load(directory, guard=not_ana)
            self.assertNotIn(ANA, got.examples)
            self.assertEqual(got.of(ANA), [])
            w = work()
            self.assertEqual(got.apply_draft(w, ANA, event=event(), autopilot="customer_po"),
                             w)
            self.assertTrue(got.examples.get(BEN))                    # others keep theirs


class PerPerson(unittest.TestCase):
    def test_a_drafter_prompt_reads_only_the_persons_own_past_replies(self):
        got = lessons_of([replied(i) for i in range(1, 5)]
                         + [replied(i, person=BEN) for i in range(1, 5)])
        read = []

        def text_of(signal):
            read.append(signal.person)
            return ("draft", "Hi Keith,\n\nThank you for the update.\n\nBest,")
        block = got.drafter_prompt(BEN, event(), "customer_po", text_of)
        self.assertTrue(block)
        self.assertEqual(set(read), {BEN})

    def test_an_ask_hands_over_only_the_readers_own_lessons(self):
        fact = [L.Signal(kind="answer_corrected", person=BEN, at=L._iso(NOW - timedelta(i)),
                         ref=f"c{i}", data={"question": "who carries the samples",
                                            "value": "Ben Freight", "markings": []})
                for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(L._SYNCED, clear=True):
            ledger_with(tmp, [sent(i) for i in range(4)] + fact)
            led = L.Ledger(Path(tmp) / "learning")
            ana_ids = {l.lesson_id for l in led.lessons() if l.who == ANA}
            led.close()
            calls = []
            mem = mock.Mock(remember=lambda reader, **kw: calls.append((reader, kw)) or 1)
            reader = mock.Mock(key=f"t:employee:{ANA},t:group:x")
            done = L.sync_memory(mem, reader, Path(tmp))
        self.assertTrue(done["kept"])
        refs = {kw["source_ref"].split("@")[0] for _r, kw in calls}
        self.assertLessEqual(refs, ana_ids)
        self.assertNotIn("Ben Freight", repr(calls))

    def test_view_as_never_hands_lessons_to_the_viewed_persons_memory(self):
        from operator_api.backend import WorkspaceBackend
        memory = mock.Mock()
        host = mock.Mock(memory=memory, state_dir=None)
        viewing = mock.Mock(session=mock.Mock(view_as=True))
        self.assertIsNone(WorkspaceBackend._memory_turn(host, viewing, mock.Mock(), "c", []))
        memory.submit.assert_not_called()
        # the Ask canary's service session (df7e06b444) never feeds or reads memory either
        canary = mock.Mock(session=mock.Mock(view_as=False, canary=True))
        self.assertIsNone(WorkspaceBackend._memory_turn(host, canary, mock.Mock(), "c", []))
        memory.submit.assert_not_called()
        # a bare Mock attribute is truthy, so the person's own session names canary=False
        own = mock.Mock(session=mock.Mock(view_as=False, canary=False))
        WorkspaceBackend._memory_turn(host, own, mock.Mock(), "c", [])
        memory.submit.assert_called_once()


class HandOver(unittest.TestCase):
    def test_a_failed_hand_over_is_tried_again_and_a_ledger_change_is_handed_over(self):
        reader = mock.Mock(key=f"t:employee:{ANA}")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(L._SYNCED, clear=True):
            ledger_with(tmp, [sent(i) for i in range(4)])
            broken = mock.Mock(remember=mock.Mock(side_effect=RuntimeError("down")))
            self.assertGreater(L.sync_memory(broken, reader, Path(tmp))["failed"], 0)
            good = mock.Mock(remember=mock.Mock(return_value=1))
            self.assertGreater(L.sync_memory(good, reader, Path(tmp))["kept"], 0)
            self.assertIsNone(L.sync_memory(good, reader, Path(tmp)))
            # the ledger changes: the reader's lessons are handed over again
            ledger_with(tmp, [sent(i) for i in range(4, 60)])
            self.assertGreater(L.sync_memory(good, reader, Path(tmp))["kept"], 0)

    def test_asks_at_once_hand_the_lessons_over_once(self):
        reader = mock.Mock(key=f"t:employee:{ANA}")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(L._SYNCED, clear=True):
            ledger_with(tmp, [sent(i) for i in range(4)])
            items = len(L.Lessons.load(Path(tmp) / "learning", guard=CLEAR)
                        .memory_items(ANA))
            calls, lock = [], threading.Lock()

            def remember(_reader, **item):
                time.sleep(0.02)
                with lock:
                    calls.append(item["subject"])
                return 1
            mem = mock.Mock(remember=remember)
            gate = threading.Barrier(6)

            def ask():
                gate.wait()
                L.sync_memory(mem, reader, Path(tmp))
            threads = [threading.Thread(target=ask) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertGreater(items, 0)
        self.assertEqual(len(calls), items)                       # once, never per Ask


class LedgerWrites(unittest.TestCase):
    def test_concurrent_writers_lose_and_duplicate_no_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "learning"
            L.Ledger(directory).close()
            errors = []
            gate = threading.Barrier(4)

            def write(t):
                led = L.Ledger(directory)
                try:
                    gate.wait()
                    for start in range(t * 10, t * 10 + 20, 5):     # overlaps the next writer
                        led.take([leave(n) for n in range(start, start + 5)])
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    led.close()
            threads = [threading.Thread(target=write, args=(t,)) for t in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            conn = sqlite3.connect(str(directory / L.FILE))
            refs = [r for (r,) in conn.execute("SELECT ref FROM signals")]
            conn.close()
        self.assertEqual(sorted(refs), sorted({leave(n).ref for n in range(50)}))

    def test_a_bad_ledger_row_or_file_gives_no_lessons_never_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = ledger_with(tmp, [sent(i) for i in range(4)])
            conn = sqlite3.connect(str(directory / L.FILE))
            with conn:
                conn.execute("UPDATE lessons SET evidence = 'not json' WHERE rowid = "
                             "(SELECT min(rowid) FROM lessons)")
            conn.close()
            with mock.patch.dict(L._CACHE, clear=True), mock.patch.dict(L._SYNCED, clear=True):
                self.assertFalse(L.Lessons.load(directory, guard=CLEAR))
                self.assertIsNone(L.sync_memory(mock.Mock(), mock.Mock(key=ANA), Path(tmp)))
                self.assertEqual(L.ask_rule(ANA, Path(tmp)), "")
            junk = Path(tmp) / "junk"
            junk.mkdir()
            (junk / L.FILE).write_bytes(b"this is not a database" * 64)
            with mock.patch.dict(L._CACHE, clear=True):
                self.assertFalse(L.Lessons.load(junk, guard=CLEAR))


class FewShotEdges(unittest.TestCase):
    @staticmethod
    def _nb(shapes):
        return [(2.0, L.Signal(kind="replied", person=ANA, at=L._iso(NOW - timedelta(i + 1)),
                               ref=f"r{i}", data={"shape": shape}))
                for i, shape in enumerate(shapes)]

    def test_a_sentence_in_exactly_two_like_replies_is_learned_one_is_not(self):
        lead, other = "thank you for the update.", "we are on it."
        two = self._nb([[lead, "#"], [lead, "#"]])
        self.assertEqual(fs._repeated(two), [(lead, True)])
        self.assertEqual(fs._repeated(self._nb([[lead, "#"], [other, "#"]])), [])
        # and in at least half of the like replies whose shape is known
        self.assertEqual([t for t, _o in fs._repeated(self._nb(
            [[lead], [lead], [other], [other]]))], [lead, other])
        self.assertEqual(fs._repeated(self._nb(
            [[lead], [lead], [other], [other], ["#"]])), [])

    def test_an_example_at_the_drafts_own_moment_is_not_from_before_it(self):
        ex = replied(1)
        self.assertTrue(fs.neighbours([ex], event(), "customer_po"))
        self.assertEqual(fs.neighbours([ex], event(), "customer_po", before=ex.at), [])

    def test_the_same_kind_of_item_alone_is_alike_shared_topic_alone_never(self):
        ex = L.Signal(kind="replied", person=ANA, at=L._iso(NOW), ref="r",
                      data={"autopilot": "customer_po",
                            "topic": sorted(fs.event_keys(event(OTHER_SENDER))["topic"])})
        self.assertEqual(fs.neighbours([ex], event(OTHER_SENDER), "customer_po")[0][0],
                         fs.FLOW + 1.0)
        flow_only = L.Signal(kind="replied", person=ANA, at=L._iso(NOW), ref="f",
                             data={"autopilot": "customer_po"})
        self.assertEqual(len(fs.neighbours([flow_only], event(OTHER_SENDER), "customer_po")),
                         1)                                          # exactly MIN_SCORE
        self.assertEqual(fs.neighbours([ex], event(OTHER_SENDER), "reply_prep"), [])

    def test_an_example_whose_words_cannot_be_read_is_left_out_of_the_prompt(self):
        nb = fs.neighbours([replied(i) for i in range(1, 3)], event(), "customer_po")

        def text_of(signal):
            if signal.ref == nb[0][1].ref:
                raise OSError("store unreadable")
            return (None, "Hi Keith,\n\nThank you for the update.\n\nBest,")
        block = fs.prompt_block(nb, [], text_of)
        self.assertIn("Example 1:", block)
        self.assertNotIn("Example 2:", block)


OTHER_SENDER = "nobody@elsewhere.example"


if __name__ == "__main__":
    unittest.main()
