"""``plx.my_open_work`` (``operator_api/open_work_tool.py``): the Ask reads the person's own
built-up store of work, every age and every channel, paged and never cut, with their notes;
an off or unreadable store says so; only the owner reads another person's.
Evidence label: ``isolated-fixture`` (a fake pool and ``as_items``; the SQL is tested in
operating/tests/test_assist_matters)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from operator_api import open_work_tool as ow

NOW = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)


def matter(n, channel="mail", days=0, kind="conversation"):
    return {"item_id": f"{n:024x}", "kind_of_matter": kind, "state": "open", "channel": channel,
            "sender": f"p{n}", "subject": f"S{n}", "summary": f"Do {n}", "message": "q" * 100,
            "note": "n", "ask": "", "body": "", "basis_at": NOW - timedelta(days=days),
            "due_at": None, "still_open": days > 30}


class Pool:
    def connection(self): return self
    def __enter__(self): return self
    def __exit__(self, *e): return False


class Memory:
    def __init__(self, rows=None, fail=False): self.rows, self.fail, self.asked = rows or [], fail, []
    def items(self, reader, **kw):
        self.asked.append(kw)
        if self.fail: raise OSError("down")
        return self.rows


class Backend:
    def __init__(self, owner=False, memory=None):
        self.memory = memory or Memory()
        self.memory.pool = Pool()
        self.owner = owner
        self.autopilot_grant = None
    def _apa_is_owner(self, r): return self.owner
    def _apa_me(self, r): return {"vince@plx.example"}
    def _apa_pools(self, r): return ()
    def _apa_state(self): return None
    def _apa_rows(self, s): return []


class Reader: key = "k"


def run(backend, items, args=None, status="ok", shown=True, older=0):
    got = {"items": items, "older": older, "status": status, "reason": "", "as_of": NOW}
    seen = {}

    def as_items(pg, owners, **kw):
        seen.update(kw, owners=owners)
        return got
    with patch("operating.assist_matters.as_items", as_items), \
            patch("operating.assist_matters.shown_to", return_value=shown), \
            patch("operating.autopilot_engine.load_draft", return_value={}):
        return ow.call(backend, Reader(), args or {}), seen


class OpenWork(unittest.TestCase):
    def test_every_age_and_channel_is_read_with_the_notes(self):
        mem = Memory([{"id": 1, "kind": "fact", "at": "x", "text": "I handled the Azelis PO",
                       "records": ["Customer:x:1"]}, {"id": 2, "kind": "fact", "at": "y",
                                                      "text": "plain", "records": []}])
        out, seen = run(Backend(memory=mem), [matter(1, "teams", 90), matter(2, "mail", 0)])
        self.assertTrue(seen["all_ages"])               # no 14-day window
        self.assertEqual([m["channel"] for m in out["matters"]], ["teams", "mail"])
        self.assertEqual(out["matters_total"], 2)
        self.assertIsNone(out["next_offset"])
        self.assertEqual(out["notes"][0]["about"], ["Customer:x:1"])   # about a record first
        self.assertIsNone(mem.asked[0]["limit"])         # all the notes, not 200

    def test_nothing_is_dropped_between_pages(self):
        items = [matter(n) for n in range(1, 301)]
        seen_ids, offset = [], 0
        while offset is not None:
            out, _ = run(Backend(), items, {"offset": offset})
            seen_ids += [m["id"] for m in out["matters"]]
            self.assertTrue(out["matters"])
            offset = out["next_offset"]
        self.assertEqual(seen_ids, [i["item_id"] for i in items])   # N=300: all, once, in order

    def test_a_store_that_is_off_or_stale_says_so(self):
        out, _ = run(Backend(), [], status="off")
        self.assertEqual(out["status"], "off")
        out, _ = run(Backend(), [matter(1)], status="stale")
        self.assertEqual(out["status"], "stale")
        self.assertEqual(len(out["matters"]), 1)

    def test_unreadable_notes_are_said_not_empty(self):
        out, _ = run(Backend(memory=Memory(fail=True)), [matter(1)])
        self.assertIn("notes_status", out)

    def test_only_the_owner_reads_another_person(self):
        out, _ = run(Backend(owner=False), [matter(1)], {"person": "x@plx.example"})
        self.assertEqual(out["status"], "refused")
        self.assertNotIn("matters", out)
        out, seen = run(Backend(owner=True), [matter(1)], {"person": "X@plx.example"})
        self.assertEqual(seen["owners"], {"x@plx.example"})
        self.assertNotIn("notes", out)       # their notes are theirs

    def test_no_store_is_a_typed_status(self):
        b = Backend(); b.memory.pool = None
        out = ow.call(b, Reader(), {})
        self.assertEqual(out["status"], "not_connected")


if __name__ == "__main__":
    unittest.main()
