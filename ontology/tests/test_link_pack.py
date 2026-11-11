"""Links added through ``ontology.link_pack.LinkWriter`` (texts packed in a pool) store
exactly what ``StoreBuilder.add("links", batch)`` of the same batches stores: every table
row for row, row ids and interned ids too, for 1, 10 or 60 links over several batches.
A builder whose link code is not the one followed, or a refused row, is not written
around: the first adds as before in its own process, the second raises before the
links are taken."""

from __future__ import annotations

import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ontology import link_pack, store
from ontology.store import StoreBuilder


def link_rows(n: int, seed: int) -> list[dict]:
    """``n`` links whose texts cover what the packer meets: shared and distinct strings,
    more strings than slots, '%' and escaped quotes, object keys, and texts with no
    string to slot (stored as written)."""
    rnd = random.Random(seed)
    words = ["PO-1001", "LOT 7%", 'say "hi"', "", "x" * 40, "shared", "%s", "a\\b"]
    out = []
    for i in range(n):
        kind = i % 6
        strings = [rnd.choice(words) + str(rnd.randrange(3)) for _ in range(rnd.randrange(30))]
        if kind == 0:
            reason, evidence = json.dumps([{"why": strings}]), json.dumps(strings[:3] or [1])
        elif kind == 1:   # no string to slot: stored as written
            reason, evidence = "[100, 2.5]", "[true]"
        elif kind == 2:
            reason, evidence = (json.dumps([{"k": {"nested": strings}}]),
                                json.dumps([{"h": "v%"}]))
        elif kind == 3:
            reason, evidence = json.dumps(strings + ["100%"]), json.dumps([None])
        elif kind == 4:
            reason, evidence = json.dumps([{"key only": 1}]), json.dumps(["only"])
        else:
            reason, evidence = json.dumps([{"a": "shared0"}]), json.dumps(["shared0", "PO-10010"])
        out.append({"link_id": f"L{seed}-{i:04d}", "link_type": "T.rel", "from_object": "o:a",
                    "to_object": "o:b", "tier": "verified", "binding_kind": "fk",
                    "link_contract": None, "reason": reason, "why_not": "[]",
                    "evidence_handles": evidence, "reviewed_by": None, "reviewed_at": None,
                    "review_action_id": None, "markings": "[]",
                    "recorded_at": "2026-11-10T00:00:00Z", "superseded_at": None})
    return out


def tables(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {t: conn.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall()
            for t in ("link_rows", "link_texts", "link_skeletons")}


class LinkPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def builder(self, name: str) -> StoreBuilder:
        return StoreBuilder(self.root / name, foreign_keys=False)   # a step input's

    def batches(self, rows: list[dict], size: int) -> list[list[dict]]:
        return [rows[k:k + size] for k in range(0, len(rows), size)]

    def test_the_code_followed_is_the_builders(self) -> None:
        self.assertEqual(link_pack.source_digest(), link_pack.ADD_LINKS_DIGEST,
                         "StoreBuilder._add_links or pack_link_texts changed: make "
                         "link_pack.add_packed follow it, then pin the new digest")

    def test_pool_stores_what_add_stores(self) -> None:
        for n in (1, 10, 60):
            rows = link_rows(n, seed=n)
            old = self.builder(f"old-{n}.sqlite")
            for batch in self.batches(rows, 7):
                old.add("links", batch)
            new = self.builder(f"new-{n}.sqlite")
            with link_pack.LinkWriter(new, 3, ahead=2) as writer:
                self.assertTrue(writer.parallel)
                for batch in self.batches(rows, 7):
                    writer.add(batch)
                    batch.clear()   # the caller reuses its list: the writer kept its own
            self.assertEqual(writer.added, n)
            got, want = tables(new.connection), tables(old.connection)
            self.assertEqual(len(got["link_rows"]), n)
            self.assertEqual(got, want)
            if n == 60:
                self.assertGreater(len(want["link_texts"]), 10)   # slots were packed
            old.abort()
            new.abort()

    def test_code_not_followed_adds_in_this_process(self) -> None:
        rows = link_rows(10, seed=3)
        old = self.builder("old.sqlite")
        old.add("links", rows)
        new = self.builder("new.sqlite")
        with mock.patch.object(link_pack, "ADD_LINKS_DIGEST", "other"):
            with link_pack.LinkWriter(new, 4) as writer:
                self.assertFalse(writer.parallel)
                writer.add(rows)
        self.assertEqual(tables(new.connection), tables(old.connection))

        class Other(StoreBuilder):
            pass
        other = Other(self.root / "other.sqlite", foreign_keys=False)
        self.assertFalse(link_pack.follows(other))
        other.abort()

    def test_refused_row_raises_before_the_links_are_taken(self) -> None:
        rows = link_rows(10, seed=5)
        rows[8]["tier"] = "unknown"   # the CHECK refuses it as add() would
        new = self.builder("new.sqlite")
        with self.assertRaises(sqlite3.IntegrityError):
            with link_pack.LinkWriter(new, 2) as writer:
                for batch in self.batches(rows, 4):
                    writer.add(batch)
        with self.assertRaises(ValueError):
            link_pack.add_packed(new, rows[:2], [None])
        new.abort()


if __name__ == "__main__":
    unittest.main()
