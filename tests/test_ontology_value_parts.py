"""A step build's values taken by parts (``ontology.store.ValuePart``,
``StoreBuilder.add_value_parts``) store exactly what adding the same rows one by one
stores: every table row for row (row ids and interned ids too), the word index's every
term on the same cells, and the same refusals. Cut into 1, 10 or 60 parts, nothing is
lost, doubled or renumbered."""

from __future__ import annotations

import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ontology import store_ddl
from ontology.store import StoreBuilder, StoreError, ValuePart

COLS = store_ddl.VALUE_COLUMNS


def store_rows(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every table's rows in rowid (or key) order, and per word of the word index the
    cells it finds: what any reader of the store can see."""
    out: dict[str, list[tuple]] = {}
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                                  "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        if name.startswith(("value_text", "chunk_text")):
            continue   # the word index's own pages: compared by what it finds, below
        order = "rowid" if "WITHOUT ROWID" not in sql.upper() else "1, 2"
        out[name] = conn.execute(f"SELECT * FROM {name} ORDER BY {order}").fetchall()
    out["sqlite_stat1"] = conn.execute("SELECT tbl, idx, stat FROM sqlite_stat1 "
                                       "ORDER BY 1, 2").fetchall()   # what plans read
    out["schema"] = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                                 "ORDER BY type, name").fetchall()
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.words "
                 "USING fts5vocab(main, value_text, 'row')")
    out["value_text"] = [(term, tuple(r[0] for r in conn.execute(
        "SELECT rowid FROM value_text WHERE value_text MATCH ? ORDER BY rowid",
        ('"' + term.replace('"', '""') + '"',))))
        for (term,) in conn.execute("SELECT term FROM temp.words ORDER BY term")]
    return out


def fixture(seed: int, objects: int = 40):
    """Objects, versions and their value rows in add order: several sources of one
    version (a second binding, a later record), values whose digest or lineage differs
    from their version's, shared and one-off fields and kinds, text the word index takes."""
    rnd = random.Random(seed)
    objs, versions, values = [], [], []
    props = [f"p{i}" for i in range(12)]
    kinds = [("text", None, None, None, "plain", None, "[]"),
             ("identifier", None, None, None, "plain", 0, "[]"),
             ("money", "USD", None, "excl_tax", "money", 1, '["finance"]'),
             ("quantity", "EA", "ordered", None, "qty", None, "[]"),
             ("decimal", None, None, None, "plain", 2, '["pii"]')]
    for n in range(objects):
        oid = f"Thing:fm:{n:04d}"
        objs.append({"object_id": oid, "object_type": "Thing", "binding_id": "fm",
                     "native_key": f'["{n}"]', "audience_key": "all",
                     "first_recorded_at": "2026-01-01T00:00:00Z",
                     "missing_from_source_since": None})
        count = rnd.randint(1, 3)
        for k in range(count):
            handle = f"l1:{oid}:{k}"
            at = f"2026-01-0{k + 1}T00:00:00Z"
            later = None if k == count - 1 else f"2026-01-0{k + 2}T00:00:00Z"
            versions.append({"object_id": oid, "l1_handle": handle, "digest": f"d{n}{k}",
                             "lineage": "fm_bronze", "markings": "[]", "recorded_at": at,
                             "superseded_at": later})
            for binding in rnd.sample(["fm", "fm_extra"], rnd.randint(1, 2)):
                digest = f"d{n}{k}" if rnd.random() < .8 else f"other{n}"
                lineage = "fm_bronze" if rnd.random() < .8 else "silver_view"
                for prop in sorted(rnd.sample(props, rnd.randint(1, 6))):
                    kind = rnd.choice(kinds)
                    path = f"row.{prop}" if rnd.random() < .9 else f"row.{prop}.alt{n % 3}"
                    value = None if rnd.random() < .05 else (
                        f"word{rnd.randint(0, 30)} text {n}" if kind[0] in ("text", "identifier")
                        else str(rnd.randint(1, 999)))
                    row = dict(zip(("value_type", "unit", "quantity_role", "tax_basis",
                                    "parser", "authority_rank", "markings"), kind))
                    row.update(object_id=oid, property=prop, value=value,
                               currency="USD" if kind[0] == "money" else None,
                               currency_citation=None, l1_handle=handle, field_path=path,
                               digest=digest, lineage=lineage, binding_id=binding,
                               recorded_at=at, superseded_at=later)
                    values.append(row)
    values.sort(key=lambda r: (r["object_id"], r["l1_handle"], r["binding_id"],
                               r["property"], r["field_path"]))
    return objs, versions, values


class ValuePartsTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="value-parts-"))

    def builder(self, name, objs, versions):
        b = StoreBuilder(self.root / name, foreign_keys=False)
        self.addCleanup(b.abort)
        b.add("objects", objs)
        b.add("object_versions", versions)
        return b

    def parts(self, values, cuts, tag):
        """``values`` cut into parts at object boundaries (``cuts`` of them, about)."""
        objects = sorted({r["object_id"] for r in values})
        step = max(1, -(-len(objects) // cuts))
        bounds = objects[::step][1:]
        out, i = [], 0
        for n, high in enumerate([*bounds, None]):
            part = ValuePart(self.root / f"{tag}-{n}.sqlite")
            batch = []
            while i < len(values) and (high is None or values[i]["object_id"] < high):
                batch.append(tuple(values[i][c] for c in COLS))
                i += 1
            for k in range(0, len(batch), 7):   # written in pieces, as a worker fetches
                part.write(batch[k:k + 7])
            out.append(part.close())
        self.assertEqual(i, len(values))
        return out

    def test_parts_store_what_one_by_one_stores(self):
        for seed, cuts in ((1, 1), (2, 10), (3, 60)):
            with self.subTest(parts=cuts):
                objs, versions, values = fixture(seed, objects=60)
                one = self.builder(f"one-{cuts}.sqlite", objs, versions)
                for k in range(0, len(values), 50):   # add's batches, as the copy gives them
                    one.add("object_values", values[k:k + 50])
                parts = self.builder(f"parts-{cuts}.sqlite", objs, versions)
                files = self.parts(values, cuts, f"p{cuts}")
                self.assertEqual(len(files), min(cuts, 60))
                self.assertEqual(parts.add_value_parts(files), len(values))
                self.assertFalse(any(f.exists() for f in files))   # taken, then removed
                got, want = store_rows(parts.connection), store_rows(one.connection)
                self.assertEqual(set(got), set(want))
                for table in want:
                    self.assertEqual(got[table], want[table], table)
                self.assertEqual(len(want["value_cells"]), len(values))
                self.assertGreater(len(want["value_text"]), 10)
                # what a later add interns next agrees too (the ids continue alike)
                extra = [dict(values[0], property="late", field_path="row.late",
                              parser="late_parser")]
                one.add("object_values", extra)
                parts.add_value_parts(self.parts(extra, 1, f"late{cuts}"))
                self.assertEqual(store_rows(parts.connection), store_rows(one.connection))

    def test_parts_refuse_what_add_refuses(self):
        objs, versions, values = fixture(4, objects=5)
        orphan = dict(values[0], l1_handle="l1:nowhere")
        undigested = dict(values[0], digest=None)
        twice = dict(values[0])
        for bad, error in ((orphan, "FOREIGN KEY"), (undigested, "NOT NULL"),
                           (twice, "UNIQUE")):
            with self.subTest(error=error):
                one = self.builder(f"one-{error}.sqlite", objs, versions)
                with self.assertRaisesRegex(sqlite3.IntegrityError, error):
                    one.add("object_values", [*values, bad])
                parts = self.builder(f"parts-{error}.sqlite", objs, versions)
                with self.assertRaisesRegex(sqlite3.IntegrityError, error):
                    parts.add_value_parts(self.parts([*values, bad], 2, f"bad{error[:3]}"))
                # the builder is still usable after the refusal (a transaction open)
                self.assertTrue(parts.connection.in_transaction)

    def test_only_a_cells_part_builder_takes_parts(self):
        objs, versions, values = fixture(5, objects=3)
        generation = StoreBuilder(self.root / "generation.sqlite")
        self.addCleanup(generation.abort)
        with self.assertRaisesRegex(StoreError, "value_parts_need_a_part"):
            generation.add_value_parts([])
        packed = self.builder("packed.sqlite", objs, versions)
        packed.packed_bindings = frozenset({"fm"})
        with self.assertRaisesRegex(StoreError, "value_parts_need_cells"):
            packed.add_value_parts([])

    def layer(self, seed: int, objects: int) -> tuple[Path, int]:
        """A unit layer (a store file) whose ``fm`` values are packed records, the rest
        cells, with links."""
        objs, versions, values = fixture(seed, objects)
        b = StoreBuilder(self.root / f"layer-{seed}.sqlite", foreign_keys=False)
        b.packed_bindings = frozenset({"fm"})
        b.add("objects", objs)
        b.add("object_versions", versions)
        b.add("object_values", values)
        b.add("links", [{"link_id": f"t:{a['object_id']}", "link_type": "Thing.next",
                         "from_object": a["object_id"], "to_object": z["object_id"],
                         "tier": "lead", "binding_kind": "fk", "link_contract": None,
                         "reason": '[{"k": "v"}]', "why_not": "[]",
                         "evidence_handles": '["h"]', "reviewed_by": None,
                         "reviewed_at": None, "review_action_id": None, "markings": "[]",
                         "recorded_at": "2026-01-01T00:00:00Z", "superseded_at": None}
                        for a, z in zip(objs, objs[1:])])
        packed = b.connection.execute("SELECT count(*) FROM value_cells WHERE field IS NULL"
                                      ).fetchone()[0]
        return b.keep_as_part(), packed

    def test_a_unit_layer_copied_by_workers_equals_one_copied_row_by_row(self):
        from ontology.step_migrate import copy_unit_layer
        layer, packed = self.layer(6, objects=80)
        self.assertGreater(packed, 0)   # packed records are decoded on the way
        got = {}
        for workers in (0, 1, 3):
            b = StoreBuilder(self.root / f"copy-{workers}.sqlite", foreign_keys=False)
            self.addCleanup(b.abort)
            links = list(copy_unit_layer(layer, b, workers=workers))
            got[workers] = (store_rows(b.connection), links)
        self.assertEqual(len(got[0][1]), 79)
        self.assertGreater(len(got[0][0]["value_cells"]), 80)
        self.assertEqual(got[1], got[0])
        self.assertEqual(got[3], got[0])

    def test_a_staged_unit_layer_is_taken_as_copy_unit_layer_takes_it(self):
        """The unit layer staged and parked (``ontology.unit_stage``, 11-10: the layer and
        its copy never whole on disk together) gives the input the same rows, row ids,
        interned ids and word index, and the same links in the same order (the old
        ``links`` read sorted by the view's NULL rowid: the scan order of ``link_rows``,
        here unlike the link id order), for 1, 10 and 60 objects and 0, 1 or 3 workers."""
        import shutil
        from ontology import unit_park, unit_stage
        from ontology.step_migrate import copy_unit_layer
        if not shutil.which("zstd"):
            self.skipTest("zstd command missing")
        for objects in (1, 10, 60):
            for workers in (0, 1, 3):
                with self.subTest(objects=objects, workers=workers):
                    layer, _ = self.layer(objects, objects=objects)
                    conn = sqlite3.connect(layer)   # links stored out of id order
                    conn.execute("CREATE TABLE schema_migration (key TEXT PRIMARY KEY, "
                                 "value TEXT NOT NULL)")
                    conn.execute("INSERT INTO schema_migration VALUES ('id', '\"u-1\"')")
                    conn.execute("UPDATE link_rows SET rowid = -rowid")
                    conn.commit()
                    conn.close()
                    old = StoreBuilder(self.root / f"old-{objects}-{workers}.sqlite",
                                       foreign_keys=False)
                    self.addCleanup(old.abort)
                    old_links = list(copy_unit_layer(layer, old, workers=workers))
                    new = StoreBuilder(self.root / f"new-{objects}-{workers}.sqlite",
                                       foreign_keys=False)
                    self.addCleanup(new.abort)
                    stage = self.root / f"stage-{objects}-{workers}"
                    got = unit_stage.prepare(layer, new, stage, workers=workers)
                    self.assertFalse(layer.exists())      # parked before the copy
                    self.assertEqual(got["counts"]["links"], max(0, objects - 1))
                    new_links = list(unit_stage.copy_staged(stage, new, unit_id="u-1"))
                    self.assertEqual(new_links, old_links)
                    if objects > 2:
                        ids = [r["link_id"] for r in new_links]
                        self.assertNotEqual(ids, sorted(ids))
                    self.assertEqual(store_rows(new.connection), store_rows(old.connection))
                    self.assertEqual(sorted(p.name for p in stage.rglob("*")
                                            if p.is_file()), [])   # each taken file gone
                    unit_park.restore(layer)

    def test_a_unit_stage_is_taken_only_whole_and_of_its_layer(self):
        import shutil
        from ontology import unit_park, unit_stage
        if not shutil.which("zstd"):
            self.skipTest("zstd command missing")
        layer, _ = self.layer(9, objects=10)
        conn = sqlite3.connect(layer)
        conn.execute("CREATE TABLE schema_migration (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_migration VALUES ('id', '\"u-9\"')")
        conn.commit()
        conn.close()
        b = StoreBuilder(self.root / "in.sqlite", foreign_keys=False)
        self.addCleanup(b.abort)
        stage = self.root / "stage"
        unit_stage.prepare(layer, b, stage, workers=1)
        with self.assertRaisesRegex(unit_stage.StageError, "stage_of_another_unit_layer"):
            list(unit_stage.copy_staged(stage, b, unit_id="u-other"))
        taking = unit_stage.copy_staged(stage, b, unit_id="u-9")
        next(taking)   # stopped while taken: no longer whole
        taking.close()
        self.assertIsNone(unit_stage.read(stage))
        with self.assertRaisesRegex(unit_stage.StageError, "stage_not_whole"):
            list(unit_stage.copy_staged(stage, b, unit_id="u-9"))
        b2 = StoreBuilder(self.root / "in2.sqlite", foreign_keys=False)
        self.addCleanup(b2.abort)
        got = unit_stage.prepare(layer, b2, stage, workers=1)   # parked: restored, staged
        self.assertIn("restored", got)
        self.assertEqual(len(list(unit_stage.copy_staged(stage, b2, unit_id="u-9"))), 9)
        unit_park.restore(layer)
        conn = sqlite3.connect(layer)   # a links view the stage does not know: refused
        conn.execute("DROP VIEW links")
        conn.execute("CREATE VIEW links AS SELECT link_id FROM link_rows l WHERE tier = 'lead'")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(unit_stage.StageError, "links_view"):
            unit_stage.make(layer, b2, self.root / "stage2", workers=1)


if __name__ == "__main__":
    unittest.main()
