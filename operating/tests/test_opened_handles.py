"""Every handle a stored plan run asked the door about is kept with its line (S5: a row used to
depend only on the handles it cited): the snapshot keeps them once each in order, worker threads
joined to it add to the same list, outside a snapshot nothing is kept, and the runner's line
carries them for every family.
Evidence label: isolated-fixture."""
from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from unittest import mock

from operating import ontology_tools_policy as otp


class Opened(unittest.TestCase):
    def test_the_snapshot_keeps_each_handle_once_in_order_and_outside_it_nothing(self):
        otp.note_opened(["lost"])
        with otp.snapshot_reads():
            otp.note_opened(["a", "b"])
            otp.note_opened(["b", "c", "", None, 7])
            snap = otp.current_snapshot()
            self.assertEqual(otp.opened_in(snap), ["a", "b", "c"])
        self.assertEqual(otp.opened_in(None), [])

    def test_a_worker_joined_to_the_snapshot_adds_to_it(self):
        with otp.snapshot_reads():
            snap = otp.current_snapshot()

            def work():
                with otp.joined_snapshot(snap):
                    otp.note_opened(["from-worker"])
            t = threading.Thread(target=work)
            t.start()
            t.join()
            self.assertEqual(otp.opened_in(snap), ["from-worker"])

    def test_the_runner_line_of_a_family_carries_what_the_run_opened(self):
        from operating import evidence_trail as et
        from operator_api import plan_batch as pb

        class View:
            @contextmanager
            def _request(self):
                yield

            def _call(self, name, args):
                return {}

        def trail(call, oid, seconds=None):
            otp.note_opened(["mail://x", "record://y"])
            return {"subject": oid, "state": "late", "steps": [{"kind": "due_date", "handle": "record://y"}],
                    "gaps": []}
        with mock.patch.object(et, "po_trail", trail):
            line = pb.answer(View(), "PO1", "po_trail")
        self.assertEqual(line["opened"], ["mail://x", "record://y"])


if __name__ == "__main__":
    unittest.main()
