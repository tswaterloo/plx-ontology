"""The door decides a list of handles with the item gate's read-ahead (``door_ahead``): the
gate reads its batched facts once for every mail/Teams/file handle before the per-handle
decisions, the decisions are the gate's own, and a failing read-ahead is said, not silent."""
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from ontology.types import Actor
from operator_api.workspace import object_workspace as ow
from operator_api.workspace.door_ahead import read_ahead

MAIL = "graph-mail://mb/{}"


class Gate:
    """An item gate that answers True for even mail numbers and records what it was asked."""

    def __init__(self, prefetch=True, fail=False):
        self.order, self.fail = [], fail
        if prefetch:
            self.prefetch = self._prefetch

    def _prefetch(self, actor, handles):
        self.order.append(("prefetch", tuple(handles)))
        if self.fail:
            raise RuntimeError("capture unreadable")

    def __call__(self, actor, handle):
        self.order.append(("gate", handle))
        return int(handle.rsplit("/", 1)[1]) % 2 == 0


def workspace(gate):
    view = object.__new__(ow.HostAccess)
    view._local = SimpleNamespace(state=(
        "gated", SimpleNamespace(may_read=lambda g, key: True), {}))
    host = SimpleNamespace(tools=SimpleNamespace(item_gate=gate), per_call=contextmanager(
        lambda: (yield)), policy_now=None)
    view.host, view.actor = host, Actor("plx", frozenset({"u:r"}))
    return view


class DoorAhead(unittest.TestCase):
    def test_decide_reads_ahead_once_then_decides_each_handle_the_gate_way(self):
        for n in (1, 10, 60):
            gate = Gate()
            handles = [MAIL.format(i) for i in range(n)]
            mixed = handles + ["mc:live/tasks/x#1", "original://a/1"]
            got = workspace(gate).decide(mixed)
            ahead = [o for o in gate.order if o[0] == "prefetch"]
            self.assertEqual(len(ahead), 1)                         # one read-ahead, not per handle
            self.assertEqual(set(ahead[0][1]), set(handles) | {"original://a/1"})
            self.assertEqual(gate.order[0][0], "prefetch")          # before the first decision
            self.assertEqual(len([o for o in gate.order if o[0] == "gate"]), n + 1)
            self.assertEqual(len(got), n + 2)                       # no row lost
            for i, h in enumerate(handles):
                self.assertEqual(got[h] is None, i % 2 == 0)        # the gate's own answer

    def test_a_failing_read_ahead_is_said_and_the_decisions_still_run(self):
        gate = Gate(fail=True)
        with self.assertLogs("operator_api.workspace.door_ahead", "WARNING") as seen:
            got = workspace(gate).decide([MAIL.format(1), MAIL.format(2)])
        self.assertIn("read-ahead failed for 2 handles", seen.output[0])
        self.assertIsNotNone(got[MAIL.format(1)])
        self.assertIsNone(got[MAIL.format(2)])

    def test_a_gate_without_prefetch_or_a_list_without_acl_handles_reads_nothing_ahead(self):
        self.assertFalse(read_ahead(Gate(prefetch=False), "a", [MAIL.format(1)], ("graph-mail://",)))
        gate = Gate()
        self.assertFalse(read_ahead(gate, "a", ["mc:live/x"], ("graph-mail://",)))
        self.assertFalse(read_ahead(None, "a", [MAIL.format(1)], ("graph-mail://",)))
        self.assertEqual(gate.order, [])


if __name__ == "__main__":
    unittest.main()
