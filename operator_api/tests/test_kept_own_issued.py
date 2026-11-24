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

    def test_a_page_kept_under_the_older_marks_form_is_served_while_both_sets_hold(self):
        """11-22: the deploy that left the PLX-issued digest out of the screens' state
        refused every page kept before it (the marks part read differently), so every
        record page was read again cold. A page kept under the older form is served while
        the withheld set and the PLX-issued set are both the ones it was read under; a
        moved PLX-issued set still refuses it."""
        import pickle
        from operator_api import backend as be
        stamps = {True: "older", False: "older"}   # code before 11-22: one form
        with mock.patch.object(be, "_marks_stamp",
                               lambda view, own_issued=True: stamps[own_issued]):
            a = lgp.reader("grp-a")
            self.serve(a, lambda: late("PurchaseOrder:p1", total=1))
            read, calls = self.counted(late("PurchaseOrder:p9", total=9))
            self.assertEqual(self.serve(a, read).total, 1)         # kept
            stamps[False] = "withheld-only"                         # the new form
            got = self.serve(a, read)
            self.assertEqual((got.total, got.stale_since, calls), (1, None, []))   # served
            key = next(k for k in self.backend._good if k[0] == a.key)
            stamps[True] = "older-moved"                            # a new PLX-issued set
            got = self.serve(a, read)
            self.assertEqual((got.total, calls), (9, [1]))          # read again
            self.assertEqual(self.backend._good[key][2][3], "withheld-only")   # kept anew
        state = bp.AccessState((1, 2))
        state.legacy = (1, 3)
        self.assertIs(type(pickle.loads(pickle.dumps(state))), tuple)   # on disk: a tuple
        self.assertTrue(bp.same_access(state, (1, 3)))
        self.assertFalse(bp.same_access(state, (1, 4)))
        self.assertTrue(bp.same_answer_access((state, "own"), (1, 3)))
        self.assertFalse(bp.same_answer_access((state, "own"), (state, "own-moved")))

    def test_only_other_readers_audiences_moving_refuses_no_kept_page(self):
        """11-22 live: after each deploy a non-full reader's kept pages were refused with
        policy parts ["audiences", "audiences_keys"] changed: another person's new mailbox
        or meeting key moved the whole audience map. What this reader may read of it
        (``audiences_held``, the patterns they hold) is still compared: a pattern of theirs
        that moves refuses the page, as a moved unit or marking does."""
        from operating.pg import permissions
        parts = {"audiences": "a1", "audiences_held": "h1", "audiences_keys": "k1",
                 "audiences_reach": "r1", "markings": "m1", "units": "u1"}
        with mock.patch.object(permissions, "policy_parts",
                               lambda policy, gated: tuple(sorted(parts.items()))):
            a = lgp.reader("grp-a")
            self.serve(a, lambda: late("PurchaseOrder:p1", total=1))
            read, calls = self.counted(late("PurchaseOrder:p9", total=9))
            self.assertEqual(self.serve(a, read).total, 1)            # kept
            parts.update(audiences="a2", audiences_keys="k2")          # someone else's key
            got = self.serve(a, read)
            self.assertEqual((got.total, calls), (1, []))              # still served
            parts.update(audiences="a3", audiences_held="h2")          # one of theirs moved
            got = self.serve(a, read)
            self.assertEqual((got.total, calls), (9, [1]))             # read again
            for n, moved in enumerate(({"units": "u2"}, {"markings": "m2"})):
                kept = self.serve(a, read).total                       # kept under this state
                again, calls2 = self.counted(late("PurchaseOrder:p7", total=70 + n))
                self.assertEqual((self.serve(a, again).total, calls2), (kept, []))
                parts.update(moved)
                self.assertEqual((self.serve(a, again).total, calls2), (70 + n, [1]), moved)
                read = again
        # a state without the reader's reach of the map is compared whole
        whole = ((("audiences", "a1"), ("markings", "m1")), False)
        self.assertFalse(bp.same_access(whole, ((("audiences", "a2"), ("markings", "m1")), False)))

    def test_an_entry_inside_a_held_wildcard_refuses_the_kept_page(self):
        """The most specific pattern decides (``ReadPolicy``), so a
        deny, or an entry for another person, inside a wildcard the reader holds withdraws
        access while the patterns the reader holds stay the same: such a state never
        matches. Another person's entry outside every held pattern still does."""
        from ontology.types import Actor
        from operating.ontology_tools_policy import ReadPolicy
        from operating.pg import permissions

        me = Actor(tenant="t1", principals=frozenset({"me"}), clearance="internal")

        def state(audiences):
            policy = ReadPolicy("t1", {k: frozenset(v) for k, v in audiences.items()})
            return (permissions.policy_parts(policy, me), False, "x", None, None), policy
        held, before = state({"bc:*": {"me"}, "mail:other:*": {"them"}})
        self.assertTrue(before.may_read(me, "bc:finance"))
        for change in ({"bc:finance": set()},            # an exact deny inside bc:*
                       {"bc:finance": {"them"}},         # another person's exact entry
                       {"bc:fin*": {"them"}},            # a narrower wildcard of theirs
                       {"*": {"them"}, "bc:*": set()}):  # the held pattern itself withdrawn
            now, after = state({"bc:*": {"me"}, "mail:other:*": {"them"}, **change})
            self.assertFalse(bp.same_access(now, held), change)
        self.assertFalse(after.may_read(me, "bc:finance"))
        # outside every held pattern: someone else's new mailbox moves nothing of the reader's
        now, after = state({"bc:*": {"me"}, "mail:other:*": {"them"},
                            "mail:new:*": {"them"}})
        self.assertTrue(after.may_read(me, "bc:finance"))
        self.assertTrue(bp.same_access(now, held))
        self.assertNotEqual(now, held)
        # a wider pattern of theirs over a held one is in the reader's reach too
        now, _ = state({"bc:*": {"me"}, "mail:other:*": {"them"}, "*": {"them"}})
        self.assertFalse(bp.same_access(now, held))


if __name__ == "__main__":
    unittest.main()
