"""Kept screens and the PLX-issued documents every reader is kept from
(``operating.pg.own_issued``): a new one refuses only the kept pages that show it, through
the re-check every kept page passes on each serve (``own_shown``); every other kept page
stays kept (11-22: each new PLX-issued document had un-kept every screen of every reader,
~2,000 refusals in 6 h). A page whose records cannot all be named is refused when the set
was first seen after it was read; an unreadable set refuses."""
from __future__ import annotations

import time
import unittest
from unittest import mock

from operating.search import document_marks as dm
from operator_api import backend_pages as bp
from operator_api import models as m
from operator_api.tests import test_last_good_page as lgp


def late(*ids: str, total: int = 1) -> m.LateList:
    return m.LateList(total=total, offset=0, more=False, next_offset=0, items=[
        m.LatePo(object_id=i, label=i, reason="late", open_url=f"/object/PurchaseOrder/{i}")
        for i in ids])


class OwnIssued(unittest.TestCase):
    serve = lgp.SnapshotFirst.serve
    counted = lgp.SnapshotFirst.counted

    def setUp(self):
        lgp.SnapshotFirst.setUp(self)
        self.own = [frozenset()]
        patch = mock.patch.object(dm, "_own_issued", lambda: self.own[0])
        patch.start()
        self.addCleanup(patch.stop)
        bp._OWN_SEEN.clear()

    def test_a_new_issued_document_refuses_only_the_pages_that_show_it(self):
        a, b = lgp.reader("grp-a"), lgp.reader("grp-b")
        shows = lambda: late("PurchaseOrder:p1", total=1)   # noqa: E731
        other = lambda: late("PurchaseOrder:p2", total=2)   # noqa: E731
        self.serve(a, shows)
        self.serve(b, other)
        read_a, calls_a = self.counted(late("PurchaseOrder:p9", total=9))
        read_b, calls_b = self.counted(late("PurchaseOrder:p8", total=8))
        self.assertEqual(self.serve(a, read_a).total, 1)     # both kept
        self.assertEqual(self.serve(b, read_b).total, 2)
        self.own[0] = frozenset({"PurchaseOrder:p1"})        # p1 is now a PLX-issued document
        got_b = self.serve(b, read_b)
        self.assertEqual((got_b.total, got_b.stale_since, calls_b), (2, None, []))   # still kept
        got_a = self.serve(a, read_a)
        self.assertNotIn("PurchaseOrder:p1", [i.object_id for i in got_a.items])
        self.assertEqual((got_a.total, calls_a), (9, [1]))   # read again, never the kept one

    def test_a_page_whose_records_cannot_be_named_is_refused_once_the_set_moves(self):
        self.assertFalse(bp.own_shown(frozenset({"X:1"}), time.time()))
        self.assertFalse(bp.own_shown(None, time.time() + 2 * bp.OWN_READ_MARGIN_SECONDS))
        self.own[0] = frozenset({"X:1"})                     # first seen now
        self.assertTrue(bp.own_shown(frozenset({"X:1"}), time.time()))
        self.assertTrue(bp.own_shown(None, time.time()))     # read before it was seen
        self.assertTrue(bp.own_shown(None, None))
        self.assertTrue(bp.own_shown(None, time.time() - 3600))
        with mock.patch.object(dm, "_own_issued", side_effect=RuntimeError("down")):
            self.assertTrue(bp.own_shown(frozenset(), time.time()))   # unreadable: refused

    def test_the_screens_state_leaves_the_issued_set_out_and_insights_keep_it(self):
        from types import SimpleNamespace
        view = SimpleNamespace(access=SimpleNamespace(grant=lambda: (None, ["m:a"])))
        sets = {frozenset({"m:a"}): (frozenset({"Doc:1"}), frozenset())}
        with mock.patch.object(dm, "withheld_set", lambda held: sets[frozenset(held)]), \
                mock.patch.dict(bp._MARK_DIGESTS, clear=True):
            before = (bp._marks_stamp(view, own_issued=False), bp._marks_stamp(view))
            self.own[0] = frozenset({"Doc:9"})
            after = (bp._marks_stamp(view, own_issued=False), bp._marks_stamp(view))
        self.assertEqual(before[0], after[0])     # the screens' state: unchanged
        self.assertNotEqual(before[1], after[1])  # record page keys: moved, as before
        first = bp.own_issued_stamp()
        self.own[0] = frozenset({"Doc:9", "Doc:10"})
        self.assertNotEqual(bp.own_issued_stamp(), first)   # tiles and search: moved


if __name__ == "__main__":
    unittest.main()
