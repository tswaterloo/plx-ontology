"""A record page's plan runs are shared by its sections: the sections of one page read at the
same time run each plan once, a plan that failed is run again, and a source handle that no
longer opens for the reader is a new read (item-level permissions are decided on every serve).
isolated-fixture; no network."""

from __future__ import annotations

import contextlib
import threading
import time
import unittest
from unittest import mock

from operator_api import backend as be
from operator_api.serving import ReaderCache


class View:
    """A view whose plan reads are counted; ``blocked`` handles are closed to the reader."""

    def __init__(self):
        self.runs = []
        self.blocked = set()
        self.fail = False
        self.lock = threading.Lock()
        outer = self

        class Access:
            def decide(self, handles):
                return {h: ({"reason": "closed"} if h in outer.blocked else None) for h in handles}
        self.access = Access()

    @contextlib.contextmanager
    def _request(self):
        yield

    def investigate(self, family, oid):
        with self.lock:
            self.runs.append(family)
        time.sleep(0.05)
        if self.fail:
            raise RuntimeError("plan failed")
        return {"verdict": {"state": "ok"},
                "investigation": {"answer": [{"l1_handle": f"graph-mail://{family}/{oid}"}]}}


class Backend:
    def __init__(self):
        self.page_cache = ReaderCache(8)


@contextlib.contextmanager
def keyed():
    with mock.patch.object(be, "page_key", lambda view, what: ("k", what)):
        yield


class KeptPlanRun(unittest.TestCase):
    def test_sections_read_together_run_each_plan_once(self):
        view, backend, got = View(), Backend(), []
        with keyed():
            threads = [threading.Thread(target=lambda: got.append(
                be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "late_po"))) for _ in range(6)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            more = be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "late_po")
        self.assertEqual(view.runs, ["late_po"])
        self.assertTrue(all(g == more and g["late_po"]["verdict"] for g in got))

    def test_a_caller_cannot_change_what_another_reads(self):
        view, backend = View(), Backend()
        with keyed():
            first = be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "late_po")
            first["late_po"]["verdict"]["state"] = "changed"
            second = be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "late_po")
        self.assertEqual(second["late_po"]["verdict"]["state"], "ok")

    def test_a_failed_plan_is_left_out_and_read_again(self):
        view, backend = View(), Backend()
        with keyed():
            view.fail = True
            self.assertEqual(be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "late_po"), {})
            view.fail = False
            self.assertIn("late_po", be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "late_po"))
        self.assertEqual(len(view.runs), 2)

    def test_a_handle_that_stops_opening_is_a_new_read(self):
        view, backend = View(), Backend()
        with keyed():
            be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "late_po")
            be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "late_po")
            self.assertEqual(len(view.runs), 1)
            view.blocked.add("graph-mail://late_po/PO1")
            be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "late_po")
        self.assertEqual(len(view.runs), 2)

    def test_a_plan_the_record_does_not_have_and_each_family_apart(self):
        view, backend = View(), Backend()
        with keyed():
            self.assertEqual(be.kept_plan_run(backend, view, "Contact", "C1", "late_po"), {})
            a = be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "late_po")
            b = be.kept_plan_run(backend, view, "PurchaseOrder", "PO1", "people_on_record")
        self.assertEqual(set(a) | set(b), {"late_po", "people_on_record"})
        self.assertEqual(sorted(view.runs), ["late_po", "people_on_record"])


if __name__ == "__main__":
    unittest.main()
