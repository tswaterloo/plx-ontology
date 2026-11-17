"""The Templates page's answer bank (a whole-company document read, >40 s cold on every
visit, walk 11-16) is served as the other screens are: the app's first visit answers
``reading`` at once and the next is the kept page; a second visit reads nothing; each
reader and question has their own page, never another's; a list that could not be read
is answered as before and never kept; one still being read for the company says
``reading``; the read-ahead keeps it for a first visit."""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from operator_api import backend as be
from operator_api import models as m
from operator_api.record_documents import STILL_READING
from operator_api.tests import test_last_good_page as lg

reader = lg.reader


def bank(*titles: str, error: str | None = None) -> m.DocList:
    return m.DocList(view="answer_bank", title="Answers any customer may get", rows=[
        m.DocRow(title=t, detail=f"answer {t}") for t in titles], error=error,
        complete=error is None)


class AnswerBankServed(unittest.TestCase):
    def setUp(self):
        lg.SnapshotFirst.setUp(self)
        self.backend.first_wait = 0.2
        self.backend._first_failed = {}
        self.answers, self.calls = {}, []
        self.release = threading.Event()
        self.release.set()

        def read(backend, who, question=None):
            if backend is self.backend:   # never an earlier test's read still running
                self.calls.append((who.key, question))
            self.release.wait(10)
            got = self.answers.get(who.key, bank())
            return got(question) if callable(got) else got
        patch = mock.patch.object(be.BriefsMixin, "answer_bank", read)
        patch.start()
        self.addCleanup(patch.stop)

    wait_over = lg.LastGoodPage.wait_over

    def ask(self, who, question=None):
        """As the app's route asks (``app.first_partial``)."""
        from operator_api.app import first_partial
        with first_partial():
            return self.backend.answer_bank(who, question)

    def test_a_slow_first_visit_answers_reading_then_the_kept_page_with_no_second_read(self):
        who = reader("grp-a")
        self.answers[who.key] = bank("GMP certificate", "Allergen statement")
        self.release.clear()
        started = time.monotonic()
        got = self.ask(who)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((got.reading, got.rows, got.view, got.error), (True, [], "answer_bank",
                                                                        None))
        self.assertTrue(self.ask(who).reading)   # asked again: no second read beside it
        self.release.set()
        self.wait_over()
        got = self.ask(who)
        self.assertEqual(([r.title for r in got.rows], got.reading, got.stale_since),
                         (["GMP certificate", "Allergen statement"], False, None))
        self.assertEqual(self.calls, [(who.key, None)])
        # a second visit: the kept page, nothing read again
        got = self.ask(who)
        self.assertEqual((len(got.rows), self.calls), (2, [(who.key, None)]))

    def test_never_another_readers_or_another_questions_page_at_any_count(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                readers = [reader(f"grp-{n}-{i}") for i in range(n)]
                for who in readers:
                    self.answers[who.key] = bank(who.key)
                    self.assertEqual([r.title for r in self.ask(who).rows], [who.key])
                self.calls.clear()
                started = time.monotonic()
                for who in readers:
                    self.assertEqual([r.title for r in self.ask(who).rows], [who.key])
                self.assertEqual(self.calls, [])   # each served their own kept page
                self.assertLess(time.monotonic() - started, 0.02 * n + 0.5)
        who = reader("grp-q")
        self.answers[who.key] = lambda q: bank(f"ranked for {q}")
        self.assertEqual(self.ask(who).rows[0].title, "ranked for None")
        self.assertEqual(self.ask(who, "  GMP? ").rows[0].title, "ranked for GMP?")
        self.assertEqual(self.ask(who, "GMP?").rows[0].title, "ranked for GMP?")
        self.assertEqual(self.ask(who, "Halal?").rows[0].title, "ranked for Halal?")
        self.assertEqual([q for k, q in self.calls if k == who.key], [None, "GMP?", "Halal?"])

    def test_a_list_that_could_not_be_read_is_answered_and_never_kept(self):
        who = reader("grp-a")
        self.answers[who.key] = bank(error="The list could not be read.")
        for _ in range(2):
            got = self.ask(who)
            self.assertEqual((got.error, got.complete, got.reading),
                             ("The list could not be read.", False, False))
        self.assertNotIn((who.key, who.mode, ("answer_bank", None)), self.backend._good)
        self.answers[who.key] = bank("read now")
        self.backend._first_failed.clear()
        self.assertEqual([r.title for r in self.ask(who).rows], ["read now"])

    def test_documents_still_being_read_for_the_company_answer_reading_never_kept(self):
        who = reader("grp-a")
        self.answers[who.key] = bank(error=STILL_READING)
        got = self.ask(who)
        self.assertTrue(got.reading)
        self.assertNotIn((who.key, who.mode, ("answer_bank", None)), self.backend._good)
        self.answers[who.key] = bank("read now")
        for _ in range(3):   # the app asks again until the page is there
            got = self.ask(who)
            if not got.reading:
                break
            self.wait_over()
        self.assertEqual([r.title for r in got.rows], ["read now"])

    def test_without_the_apps_mark_the_read_is_waited_for(self):
        """The read-ahead (``SCREENS_AHEAD``) or any other caller never gets the partial."""
        who = reader("grp-a")
        self.answers[who.key] = bank("one")
        self.release.clear()
        threading.Timer(0.5, self.release.set).start()
        got = self.backend.answer_bank(who)
        self.assertEqual(([r.title for r in got.rows], got.reading), (["one"], False))
        self.assertIn(("answer_bank", None), be.SCREENS_AHEAD)

    def test_the_route_asks_with_the_apps_mark_and_passes_the_partial_on(self):
        from operator_api import serving
        from operator_api.mock import MockBackend
        seen = []

        def partial(backend, who, question=None):
            seen.append((serving.FIRST_PARTIAL.get(), question))
            return m.DocList(view="answer_bank", title="t", complete=False, reading=True)
        with mock.patch.object(MockBackend, "answer_bank", partial):
            got = _signed_in().get("/api/templates/answer-bank?question=GMP")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual((got.json()["reading"], got.json()["rows"]), (True, []))
        self.assertEqual(seen, [(True, "GMP")])


def _signed_in():
    from operator_api.tests import test_app_api
    return test_app_api.signed_in()


if __name__ == "__main__":
    unittest.main()
