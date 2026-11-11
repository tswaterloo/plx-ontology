"""Reads kept between requests: each stays per reader, so a restricted reader never sees
what a wider reader's kept read held, and expires as it says.

* ``suggest.same_records`` with a ``SameRecordMemo``: a record walked for one reader is
  not walked again for that reader, and is walked anew for any other;
* ``document_marks.withheld_set``: the split of the marked documents is kept per marking
  set and per loaded set (a refresh drops it)."""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from operating.search import document_marks
from operator_api import suggest

A = 'Customer:portal.app_customer:{"id":"a"}'
B = 'Customer:bc.customers:{"id":"b"}'
C = 'Customer:bc.customers:{"id":"c"}'


class View:
    """A reader's view: ``twins`` is what ``plx.around`` returns this reader."""

    def __init__(self, twins: dict) -> None:
        self.twins, self.calls = twins, []

    def _request(self):
        return contextlib.nullcontext()

    def _call(self, tool, args):
        assert tool == "plx.around", tool
        self.calls.append(args["from"])
        got = self.twins.get((args["from"], args["path"][0]["direction"]), ())
        return {"objects": [{"object_id": o} for o in got]}


class SameRecordMemoTests(unittest.TestCase):

    def test_a_walk_is_kept_for_its_reader_only(self):
        memo = suggest.SameRecordMemo()
        wide = View({(A, "forward"): [B], (B, "reverse"): [A]})
        self.assertEqual(suggest.same_records(wide, [A, B, C], memo=memo, reader="wide"),
                         {frozenset((A, B))})
        self.assertEqual(len(wide.calls), 6)   # two directions per record
        # the next keystroke: nothing walked again, the same pairs
        self.assertEqual(suggest.same_records(wide, [B, A], memo=memo, reader="wide"),
                         {frozenset((A, B))})
        self.assertEqual(len(wide.calls), 6)
        # a reader the link is not shown to walks for itself and sees the copies apart
        narrow = View({})
        self.assertEqual(suggest.same_records(narrow, [A, B], memo=memo, reader="narrow"),
                         set())
        self.assertEqual(len(narrow.calls), 4)
        self.assertEqual(memo.pairs("narrow", [A, B]), set())
        self.assertEqual(memo.pairs("wide", [A, B]), {frozenset((A, B))})

    def test_a_kept_walk_expires_and_never_joins_a_record_not_asked(self):
        now = [0.0]
        memo = suggest.SameRecordMemo(clock=lambda: now[0])
        view = View({(A, "forward"): [B, C]})
        suggest.same_records(view, [A, B], memo=memo, reader="r")
        self.assertEqual(memo.pairs("r", [A, B]), {frozenset((A, B))})
        self.assertEqual(memo.pairs("r", [A]), set())   # C was never shown with A
        self.assertEqual(memo.missing("r", [A, B, C]), [C])
        now[0] = suggest.SAME_RECORD_KEEP_SECONDS + 1
        self.assertEqual(memo.missing("r", [A, B]), [A, B])
        self.assertEqual(memo.pairs("r", [A, B]), set())

    def test_many_records_each_walked_once_at_most_the_ask_cap(self):
        for n in (1, 10, 60):
            ids = [f'Customer:bc.customers:{{"id":"{i}"}}' for i in range(n)]
            view, memo = View({}), suggest.SameRecordMemo()
            suggest.same_records(view, ids, memo=memo, reader="r")
            asked = min(n, suggest.SAME_RECORD_ASKS)
            self.assertEqual(len(view.calls), 2 * asked)
            self.assertEqual(len(set(view.calls)), asked)
            suggest.same_records(view, ids, memo=memo, reader="r")
            self.assertEqual(len(view.calls), 2 * asked)


class WithheldSplitTests(unittest.TestCase):
    DOC = "SharePointFile:sp:{\"id\":\"pay\"}"
    LETTER = "SharePointFile:sp:{\"id\":\"hr\"}"

    def setUp(self):
        document_marks.forget()
        self.addCleanup(document_marks.forget)
        self.loaded = ({self.DOC: frozenset({"finance"}), self.LETTER: frozenset({"pii"})},
                       {"sharepoint:pay": frozenset({"finance"}),
                        "sharepoint:hr": frozenset({"pii"})})
        for patch in (mock.patch.object(document_marks, "enabled", lambda: True),
                      mock.patch.object(document_marks, "holds_every",
                                        lambda held: {"finance", "pii"} <= set(held)),
                      mock.patch.object(document_marks._MARKED, "get",
                                        lambda: self.loaded)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_each_marking_set_gets_its_own_split(self):
        finance = document_marks.withheld_set({"finance"})
        self.assertEqual(finance, (frozenset({self.LETTER}), frozenset({"sharepoint:hr"})))
        # a reader with no marking, asking after a wider one: both documents withheld
        none = document_marks.withheld_set(())
        self.assertEqual(none[0], frozenset({self.DOC, self.LETTER}))
        self.assertTrue(document_marks.handle_withheld("sharepoint:pay#page=2", ()))
        self.assertFalse(document_marks.handle_withheld("sharepoint:pay", {"finance"}))
        self.assertEqual(document_marks.withheld_set({"finance"}), finance)
        self.assertEqual(document_marks.withheld_set({"finance", "pii"}),
                         (frozenset(), frozenset()))

    def test_a_refreshed_load_is_split_again(self):
        self.assertEqual(document_marks.withheld_set({"finance"})[0], {self.LETTER})
        # the re-mark marks the pay stub pii too: a new loaded set, a new split
        self.loaded = ({self.DOC: frozenset({"finance", "pii"}),
                        self.LETTER: frozenset({"pii"})}, {})
        self.assertEqual(document_marks.withheld_set({"finance"})[0],
                         {self.DOC, self.LETTER})

    def test_an_unreadable_store_withholds_every_file(self):
        self.loaded = None
        self.assertIsNone(document_marks.withheld_set({"finance"}))
        self.assertTrue(document_marks.handle_withheld("sharepoint:anything", {"finance"}))


if __name__ == "__main__":
    unittest.main()
