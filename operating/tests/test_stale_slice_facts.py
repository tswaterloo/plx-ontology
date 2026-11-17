"""CAP-L291: store facts survive data changes. While a Postgres store state's slice facts
are still being kept (a fold over the layers since, or a whole read after a change no fold
carries), a reader's slice decisions are taken on the facts of an earlier store state
first, and such a decision is kept only when it is the weaker one ("may hide",
"unknown"): earlier facts can miss a marking, audience or gated handle the data gained
since, so they never show that nothing is hidden. Measured 2026-11-13: each customer
margin call after an in-place change waited 8 s for a fold that never came, then read
its slices (18 s)."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from operating import ontology_tools_traverse as tt

HELD = '[]'
HR = '["hr"]'


def _scope(*, marking_sets=(HELD,)):
    at = dt.datetime(2026, 11, 13, tzinfo=dt.timezone.utc)
    return tt._Scope("reader", at, tt._fixed(at), False, ("portal:1",), frozenset(),
                     tuple(marking_sets), ("portal",), "gate")


def _slice(*marks, handles=None):
    return ({m: tt._OPEN for m in marks}, {} if handles is None else handles)


class StaleSliceTests(unittest.TestCase):
    def setUp(self):
        tt._STALE_KEPT.clear()
        self.addCleanup(tt._STALE_KEPT.clear)

    def tools(self, *, current=None, stale=None, exact=None):
        """Tools over a Postgres-like store: ``current`` the facts this state holds,
        ``stale`` the slice facts held from an earlier state, ``exact`` what this state's
        read (``_slices_of``) gives."""
        tools = tt.TraverseTools.__new__(tt.TraverseTools)
        tools.store = SimpleNamespace(conn=SimpleNamespace(postgres=True),
                                      stale_slices=stale)
        tools.base = SimpleNamespace(policy=SimpleNamespace(may_read=lambda actor, key: True))
        tools._audiences = lambda: {"PurchaseOrder": ("portal:1",)}
        tools._current_facts = mock.Mock(return_value=dict(current or {}))
        # this state's facts are being read in the background (``prime_lazily``)
        tools._pending = (("marker", {}), (), threading.Event())
        tools._slices_of = mock.Mock(
            side_effect=lambda kind, names: {n: v for n, v in (exact or {}).get(
                kind, {}).items() if n in names})
        self.enterContext(mock.patch.object(tt, "_store_identity", return_value=None))
        return tools

    def test_earlier_facts_that_show_a_hidden_marking_decide_without_a_read(self):
        tools = self.tools(stale={("slices", "object"): {"PurchaseOrder": _slice(HR)}})
        self.assertTrue(tools._slice_trimmed(_scope(), ["PurchaseOrder"]))
        tools._slices_of.assert_not_called()
        self.assertEqual(tools._absence_state(_scope(), ["PurchaseOrder"], "link", []),
                         "unknown")
        tools._slices_of.assert_not_called()

    def test_earlier_facts_never_show_that_nothing_is_hidden(self):
        # earlier facts saw only held markings; the data since gained an hr row: the
        # exact read decides, and it says trimmed
        tools = self.tools(stale={("slices", "object"): {"PurchaseOrder": _slice(HELD)}},
                           exact={"object": {"PurchaseOrder": _slice(HELD, HR)}})
        self.assertTrue(tools._slice_trimmed(_scope(), ["PurchaseOrder"]))
        tools._slices_of.assert_called()
        # and with nothing hidden in the exact read, not trimmed
        tools = self.tools(stale={("slices", "object"): {"PurchaseOrder": _slice(HELD)}},
                           exact={"object": {"PurchaseOrder": _slice(HELD)}})
        self.assertFalse(tools._slice_trimmed(_scope(), ["PurchaseOrder"]))
        tools._slices_of.assert_called()

    def test_this_states_facts_when_held_are_never_overruled_by_earlier_ones(self):
        current = {("slices", "object"): {"PurchaseOrder": _slice(HELD)}}
        tools = self.tools(current=current,
                           stale={("slices", "object"): {"PurchaseOrder": _slice(HR)}},
                           exact={"object": {"PurchaseOrder": _slice(HELD)}})
        self.assertFalse(tools._slice_trimmed(_scope(), ["PurchaseOrder"]))
        self.assertEqual(tools._slices_or_stale("object", {"PurchaseOrder"}),
                         {"PurchaseOrder": _slice(HELD)})
        # per-type facts of this state count as held too
        per_type = {("slice_of", "object", "PurchaseOrder"): {"PurchaseOrder": _slice(HELD)}}
        tools = self.tools(current=per_type,
                           stale={("slices", "object"): {"PurchaseOrder": _slice(HR)}})
        self.assertEqual(tools._slices_or_stale("object", {"PurchaseOrder"}),
                         {"PurchaseOrder": _slice(HELD)})

    def test_earlier_item_handles_never_decide_through_the_gate(self):
        # earlier facts list a gated handle: the item gate is decided only on this state's
        tools = self.tools(stale={("slices", "link"): {"cites": _slice(HELD, handles={
            "mail:1": tt._OPEN})}},
            exact={"link": {"cites": _slice(HELD)}})
        self.assertFalse(tools._slice_trimmed(_scope(), [], links=["cites"]))
        tools._slices_of.assert_called()
        # past the handle cap (None) earlier facts do say trimmed
        tools = self.tools(stale={("slices", "link"): {"cites": ({HELD: tt._OPEN}, None)}})
        self.assertTrue(tools._slice_trimmed(_scope(), [], links=iter(["cites"])))
        tools._slices_of.assert_not_called()

    def test_kept_facts_of_an_earlier_version_serve_a_new_process(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        marker = Path(folder) / "pg-db"
        marker.touch()
        code = tt._facts_code_digest()
        self.assertIsNotNone(code)
        identity = {"postgres": "db", "data_version": "100", "generation": "g", "code": code}
        tt._save_facts(str(marker), identity,
                       {("slices", "object"): {"PurchaseOrder": _slice(HR)}})
        tools = self.tools()
        with mock.patch.object(tt, "_store_identity",
                               return_value=(str(marker), {**identity,
                                                           "data_version": "200"})):
            self.assertTrue(tools._slice_trimmed(_scope(), ["PurchaseOrder"]))
        tools._slices_of.assert_not_called()
        # another fact code's file serves nothing
        tt._save_facts(str(marker), {**identity, "code": "other"},
                       {("slices", "object"): {"PurchaseOrder": _slice(HR)}})
        self.assertEqual(tt._stale_kept_slices(str(marker)), {})

    def test_earlier_facts_decide_only_while_this_states_are_being_read(self):
        """No background read (or one that is done): the exact decision runs, so a weaker
        claim never stands for a whole data version (review a0ebf813d8)."""
        stale = {("slices", "object"): {"PurchaseOrder": _slice(HR)}}
        exact = {"object": {"PurchaseOrder": _slice(HELD)}}
        for pending in (None, "done"):
            tools = self.tools(stale=stale, exact=exact)
            if pending is None:
                tools._pending = None
            else:
                tools._pending[2].set()
            self.assertFalse(tools._slice_trimmed(_scope(), ["PurchaseOrder"]), pending)
            tools._slices_of.assert_called()
        # still being read: repeated calls take the weaker claim without a read
        tools = self.tools(stale=stale, exact=exact)
        for _ in range(3):
            self.assertTrue(tools._slice_trimmed(_scope(), ["PurchaseOrder"]))
        tools._slices_of.assert_not_called()

    def test_a_reader_leaving_a_state_holds_its_slices_merged(self):
        store = SimpleNamespace()
        tt._stash_stale(store, {("slices", "object"): {"A": _slice(HELD)},
                                "audiences": {}, ("slice_of", "link", "x"): {"x": _slice()}})
        tt._stash_stale(store, {("slices", "object"): {"B": _slice(HR)}})
        self.assertEqual(set(store.stale_slices), {("slices", "object"),
                                                   ("slice_of", "link", "x")})
        self.assertEqual(store.stale_slices[("slices", "object")], {"B": _slice(HR)})
        tt._stash_stale(store, {"audiences": {}})   # nothing to hold: kept as it was
        self.assertIn(("slice_of", "link", "x"), store.stale_slices)

    def test_a_call_waits_only_for_a_fold_that_can_come(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        marker = Path(folder) / "pg-db"
        marker.touch()
        code = tt._facts_code_digest()
        identity = {"postgres": "db", "data_version": "100", "generation": "g", "code": code}
        tt._save_facts(str(marker), identity, {("slices", "object"): {}})
        conn = SimpleNamespace()
        now = (str(marker), {**identity, "data_version": "200"})

        def wait(changed, current=now):
            tt._STALE_KEPT.clear()
            with mock.patch.object(tt, "_store_identity", return_value=current), \
                    mock.patch.object(tt, "_pg_changed_since", return_value=changed):
                return tt._pg_request_wait(str(marker), conn=conn)

        self.assertEqual(wait({"delta_layers", "delta:L1"}), tt.PG_REQUEST_WAIT_SECONDS)
        self.assertEqual(wait({"delta_layers", "delta:L1", "generation"}), 0.0)
        self.assertEqual(wait({"migration:0019"}), 0.0)
        self.assertEqual(wait({"delta_layers"}, (str(marker), {**now[1], "generation": "h"})),
                         0.0)
        # unreadable: waits as before
        self.assertEqual(wait(None), tt.PG_REQUEST_WAIT_SECONDS)
        # without a connection, as before
        self.assertEqual(tt._pg_request_wait(str(marker)), tt.PG_REQUEST_WAIT_SECONDS)
        os.unlink(tt.facts_path(str(marker)))
        self.assertEqual(wait({"migration:0019"}), 0.0)


if __name__ == "__main__":
    unittest.main()
