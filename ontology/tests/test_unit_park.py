"""The unit layer parked while the step layer works (``ontology.unit_park``): restored it
is the layer byte for byte; parked it is never readable as a layer (the apply finds no
file) while its record still is (the step build's resume reads it); a stopped park or
restore runs again; a damaged or foreign parked file is refused and kept."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from ontology import unit_park


def _layer(path: Path, rows: int, plan: str = "p1") -> str:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migration (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany("INSERT INTO schema_migration VALUES (?, ?)",
                     [(k, json.dumps(v)) for k, v in
                      {"id": f"layer-{plan}-{rows}", "plan": plan}.items()])
    conn.execute("CREATE TABLE objects (object_id TEXT PRIMARY KEY, body TEXT)")
    conn.executemany("INSERT INTO objects VALUES (?, ?)",
                     [(f"o:{i}", f"text {i} " * (i % 7)) for i in range(rows)])
    conn.commit()
    conn.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(shutil.which("zstd"), "zstd command missing")
class UnitParkTests(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_park_and_restore_give_the_layer_back_byte_for_byte(self):
        for rows in (1, 10, 60_000):
            with self.subTest(rows=rows):
                layer = self.dir / f"unit-{rows}.sqlite"
                digest = _layer(layer, rows)
                rec = unit_park.record(layer)
                got = unit_park.park(layer)
                self.assertFalse(layer.exists())        # nothing an apply could read
                self.assertTrue(Path(got["parked"]).is_file())
                self.assertEqual(unit_park.record(layer), rec)   # resume reads it parked
                self.assertEqual(unit_park.park(layer), {"parked": "already"})
                if rows == 60_000:
                    self.assertLess(got["parked_size"], got["size"] / 2)
                unit_park.restore(layer)
                self.assertEqual(hashlib.sha256(layer.read_bytes()).hexdigest(), digest)
                self.assertEqual(sorted(p.name for p in self.dir.glob(f"unit-{rows}*")),
                                 [layer.name])
                self.assertEqual(unit_park.restore(layer), {"restored": "not_parked"})

    def test_a_stopped_park_or_restore_runs_again(self):
        layer = self.dir / "unit.sqlite"
        digest = _layer(layer, 500)
        # a park stopped mid-compress: a partial file beside the whole layer
        (self.dir / "unit.sqlite.zst.part").write_bytes(b"partial")
        unit_park.park(layer)
        self.assertFalse((self.dir / "unit.sqlite.zst.part").exists())
        # a restore stopped mid-decompress: a partial file beside the whole parked one
        (self.dir / "unit.sqlite.restore").write_bytes(b"partial")
        unit_park.restore(layer)
        self.assertEqual(hashlib.sha256(layer.read_bytes()).hexdigest(), digest)
        self.assertFalse((self.dir / "unit.sqlite.restore").exists())

    def test_a_damaged_or_foreign_parked_layer_is_refused_and_kept(self):
        layer = self.dir / "unit.sqlite"
        _layer(layer, 2_000)
        unit_park.park(layer)
        zst = self.dir / "unit.sqlite.zst"
        good = zst.read_bytes()
        bad = bytearray(good)
        bad[len(bad) // 2] ^= 0xFF
        zst.write_bytes(bytes(bad))
        with self.assertRaises((unit_park.ParkError, subprocess.CalledProcessError)):
            unit_park.restore(layer)
        self.assertFalse(layer.exists())
        self.assertTrue(zst.is_file())
        # another layer's file under this record: decompresses, but is not this layer
        other = self.dir / "other.sqlite"
        _layer(other, 2_000, plan="p2")
        subprocess.run(["zstd", "-q", "-f", "-o", str(zst), str(other)], check=True)
        held = (self.dir / "unit.sqlite.parked.json").read_text()
        meta = json.loads(held)
        meta["size"] = other.stat().st_size
        (self.dir / "unit.sqlite.parked.json").write_text(json.dumps(meta))
        with self.assertRaises(unit_park.ParkError) as caught:
            unit_park.restore(layer)
        self.assertIn("record_differs", str(caught.exception))
        self.assertFalse(layer.exists())
        zst.write_bytes(good)
        (self.dir / "unit.sqlite.parked.json").write_text(held)
        unit_park.restore(layer)
        self.assertEqual(unit_park.record(layer)["plan"], "p1")

    def test_missing_open_or_doubly_held_layers_are_refused(self):
        layer = self.dir / "unit.sqlite"
        with self.assertRaises(unit_park.ParkError):
            unit_park.record(layer)
        with self.assertRaises(unit_park.ParkError):
            unit_park.park(layer)
        with self.assertRaises(unit_park.ParkError):
            unit_park.restore(layer)
        _layer(layer, 10)
        Path(f"{layer}-journal").write_bytes(b"")
        with self.assertRaises(unit_park.ParkError):   # a writer may still hold it
            unit_park.park(layer)
        Path(f"{layer}-journal").unlink()
        unit_park.park(layer)
        _layer(layer, 10)                              # a new layer beside a parked one
        with self.assertRaises(unit_park.ParkError):
            unit_park.restore(layer)
        self.assertEqual(unit_park.main(["status", str(layer)]), 0)
        self.assertEqual(unit_park.main(["bogus", str(layer)]), 2)


if __name__ == "__main__":
    unittest.main()
