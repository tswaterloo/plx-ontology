"""The briefing watcher places a cited record in its newest owning delta layer from the
layers' owned ids read once (operating/ai_watcher.py ``Places``), not one query per layer
per record. Evidence label: ``isolated-fixture`` (made-up layers and ids)."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from operating import ai_watcher as aw


def layer(path: Path, owned: list[str]) -> str:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE delta_owned(object_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO delta_owned VALUES (?)", [(o,) for o in owned])
    return str(path)


class PlacesTests(unittest.TestCase):
    def test_the_newest_owner_serves_and_the_layers_are_read_once(self):
        with tempfile.TemporaryDirectory() as root:
            served = Path(root) / "projection" / "ontology.sqlite"
            served.parent.mkdir()
            with sqlite3.connect(served) as conn:
                conn.execute("CREATE TABLE store_meta(key TEXT PRIMARY KEY, value TEXT)")
            places = aw.Places(served)
            places.layers = [layer(Path(root) / "new.sqlite", ["PurchaseOrder:fx:1"]),
                             layer(Path(root) / "old.sqlite",
                                   ["PurchaseOrder:fx:1", "SalesOrder:fx:2"])]
            try:
                self.assertEqual(places._where("PurchaseOrder:fx:1"), places.layers[0])
                self.assertEqual(places._where("SalesOrder:fx:2"), places.layers[1])
                self.assertEqual(places._where("Email:fx:3"), str(served))
                for conn in places._conns.values():
                    conn.close()   # placing again reads no layer
                self.assertEqual(places._where("SalesOrder:fx:2"), places.layers[1])
            finally:
                places._conns.clear()


if __name__ == "__main__":
    unittest.main()
