"""Projection disk need (lane projection-disk-need, owner order 38b49958892d).

The 2026-10-16 daily stopped four projection checks with SQLITE_FULL at 21-23.6 GB free:
one full build took about twice its store. These tests pin, over synthetic captures and
the real curated schema (never live data):

- the gold relations the ETL re-inserts under a fresh ``cuid()`` project one object per
  natural key (``ontology.project.ETL_REINSERTED``), the first-inserted copy, with every
  dropped copy reported and nothing of it reachable in the store;
- a row that cannot be compared, or differs in any served column, is never collapsed;
- the projector's working file holds the captured records once, compressed (what each
  stage holds is reported through ``on_stage``), so a build's peak is about its store;
- the probe records that per stage with the de-duplicated object count, takes a previous
  peak as the need only when it was measured under the current working-file layout, and
  refuses a build short of space with the typed ``disk_insufficient``.

Evidence label: synthetic (fixture captures; no business system is read).
"""

from __future__ import annotations

import hashlib
import json
import pickle
import random
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from unittest import mock

from ontology import project
from ontology.project import (ETL_REINSERTED, STAGES, WORK_LAYOUT, CapturedRecord, Projector,
                              build_store)
from ontology.project_incremental import build_generation
from ontology.tests import synthetic_captures
from scripts import ontology_projection_probe as probe

T = datetime(2026, 10, 16, tzinfo=timezone.utc)
MANUFACTURER = "public.app_manufacturer"
SUPPLIER = "public.app_supplier"
BINDING = "gen.public.app_manufacturer"
TYPE = "RelPublicAppManufacturer"
COLLAPSED = "objects.etl_duplicates_collapsed"


_SCHEMA: list[Any] = []


def schema() -> Any:
    """The curated schema, compiled once per run."""
    if not _SCHEMA:
        from ontology.schema import load_schema
        _SCHEMA.append(load_schema())
    return _SCHEMA[0]


def manufacturer(content: int, copy: int, **extra: Any) -> dict[str, Any]:
    """Copy ``copy`` of silver row ``content`` as the ETL inserts it: a fresh id and
    insert times, every other column the same."""
    row = {"id": f"cuid-{content:04d}-{copy:02d}-{(content * 7919 + copy * 104729) % 9973:04d}",
           "name": f"Maker {content}", "city": f"City {content % 3}", "state": None,
           "country": "GB", "contact": None, "gmp_compliance": content % 2 == 0,
           "supplier_id": f"sup-{content % 5}",
           "created_at": f"2026-0{1 + copy % 9}-1{copy % 10}T08:00:0{copy % 10}.{copy}",
           "updated_at": f"2026-0{1 + copy % 9}-1{copy % 10}T08:00:0{copy % 10}.{copy}"}
    row.update(extra)
    return row


class Rows:
    """A ``CaptureSource`` of given Portal rows per relation (one current version each,
    unless a row lists ``_versions``; ``_audience`` and ``_missing`` set its current
    version's audience key and missing-from-source time)."""

    source = "portal_api"

    def __init__(self, rows: Mapping[str, Sequence[Any]]) -> None:
        self.rows = rows

    def _stream(self, relation: str) -> Iterator[CapturedRecord]:
        for i, row in enumerate(self.rows.get(relation, ())):
            meta = {k: row.pop(k) for k in ("_versions", "_audience", "_missing")
                    if k in row} if isinstance(row, dict) else {}
            versions = meta.get("_versions")
            key = row["id"] if isinstance(row, Mapping) and "id" in row else f"r{i}"
            chain = [*(versions or []), row]
            for n, version in enumerate(chain):
                start = f"2026-10-0{1 + n}T00:00:00.000000Z"
                end = None if n == len(chain) - 1 else f"2026-10-0{2 + n}T00:00:00.000000Z"
                yield CapturedRecord(
                    "portal_api", relation, json.dumps({"id": key}), version,
                    f"portal-api://mirror/{relation}/{key}@{n}", "row",
                    hashlib.sha256(f"{relation}/{key}@{n}".encode()).hexdigest(),
                    start, end, end or start,
                    meta.get("_audience", f"api:{relation}") if end is None
                    else f"api:{relation}",
                    meta.get("_missing") if end is None else None)
            row.update(meta)

    def records(self, relations: Sequence[str]) -> Mapping[str, Any]:
        from ontology.project import _Relations
        return _Relations(relations, self._stream)

    def describe(self) -> dict[str, Any]:
        return {"generation": "fixture", "relations": {}}


def build(rows: Mapping[str, Sequence[Any]], **options: Any) -> tuple[dict[str, Any], dict]:
    """Build a store over ``rows``; the report and what the store holds per object."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "store.sqlite"
        report = build_store(path, schema(), {"portal_api": Rows(rows)}, clock=lambda: T,
                             **options)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            held = {
                "objects": sorted(r[0] for r in conn.execute(
                    "SELECT object_id FROM objects WHERE binding_id = ?", (BINDING,))),
                # every table that names an object: none may name a dropped copy
                "named": sorted({r[0] for r in conn.execute(
                    """SELECT object_id FROM object_versions UNION ALL
                       SELECT object_id FROM object_values UNION ALL
                       SELECT object_id FROM identifiers UNION ALL
                       SELECT from_object FROM links UNION ALL SELECT to_object FROM links
                       UNION ALL SELECT object_id FROM event_objects""")}),
                "handles": sorted({r[0] for r in conn.execute(
                    "SELECT l1_handle FROM object_versions")}),
            }
        finally:
            conn.close()
    return report, held


def oid(row: Mapping[str, Any]) -> str:
    return project.object_id_of(TYPE, BINDING, {"id": row["id"]})


def gap_count(report: Mapping[str, Any], code: str) -> int:
    return sum(g["count"] for g in report["gaps"] if g["code"] == code)


class EtlDuplicateTests(unittest.TestCase):

    def test_the_declared_relations_are_portal_bindings_keyed_on_the_minted_id(self):
        bound = {(b["source"], b["relation"]): b for t in schema().object_types.values()
                 for b in t.get("bindings", [])}
        for source, relations in ETL_REINSERTED.items():
            for relation, minted in relations.items():
                self.assertIn((source, relation), bound, relation)
                self.assertEqual(bound[(source, relation)]["key"], ["id"], relation)
                self.assertEqual(minted, frozenset({"id", "created_at", "updated_at"}))

    def test_copies_collapse_to_the_first_inserted_one_at_any_cardinality(self):
        for contents in (1, 10, 60):
            with self.subTest(contents=contents):
                rows = [manufacturer(c, k) for c in range(contents) for k in range(3)]
                started = time.monotonic()
                report, held = build({MANUFACTURER: rows})
                seconds = time.monotonic() - started
                first = {c: min((r for r in rows if r["name"] == f"Maker {c}"),
                                key=lambda r: project._keep_order(r["created_at"]))
                         for c in range(contents)}
                # one object per silver row, none lost, none twice: the first inserted
                self.assertEqual(held["objects"], sorted(oid(r) for r in first.values()))
                self.assertEqual(report["counts"][COLLAPSED], 2 * contents)
                self.assertEqual(gap_count(report, "etl_duplicate_collapsed"), 2 * contents)
                dropped = {oid(r) for r in rows} - set(held["objects"])
                self.assertEqual(len(dropped), 2 * contents)
                self.assertFalse(dropped & set(held["named"]))
                self.assertFalse({h for h in held["handles"]
                                  if any(r["id"] in h for r in rows
                                         if oid(r) in dropped)})
                self.assertLess(seconds, 60)

    def test_the_choice_is_deterministic_whatever_the_order_or_batch(self):
        rows = [manufacturer(c, k) for c in range(12) for k in range(4)]
        reference, held = build({MANUFACTURER: rows})
        shuffled = list(rows)
        random.Random(7).shuffle(shuffled)
        for batch, order in ((project.DEFAULT_BATCH, shuffled), (1, rows), (5, shuffled)):
            report, again = build({MANUFACTURER: order}, batch_size=batch)
            self.assertEqual(again["objects"], held["objects"], batch)
            self.assertEqual(report["content_digest"], reference["content_digest"], batch)

    def test_rows_that_differ_or_cannot_be_compared_are_never_collapsed(self):
        rows = [manufacturer(1, 0), manufacturer(1, 1, city="Elsewhere"),   # differs
                # a cell that is not JSON (in a column no property reads): its own key
                manufacturer(2, 0, score=float("nan")), manufacturer(2, 1, score=float("nan")),
                manufacturer(3, 0, blob=b"x"), manufacturer(3, 1, blob=b"x")]
        report, held = build({MANUFACTURER: rows})
        self.assertEqual(held["objects"], sorted(oid(r) for r in rows))
        self.assertEqual(report["counts"].get(COLLAPSED, 0), 0)
        self.assertEqual(gap_count(report, "etl_duplicate_collapsed"), 0)

    def test_copies_under_different_audiences_stay_apart(self):
        # A collapse never moves a row from one audience's view to another's.
        rows = [manufacturer(1, 0), manufacturer(1, 1, _audience="api:restricted"),
                manufacturer(1, 2, _audience="api:restricted")]
        report, held = build({MANUFACTURER: rows})
        self.assertEqual(held["objects"], sorted([oid(rows[0]), oid(rows[1])]))
        self.assertEqual(report["counts"][COLLAPSED], 1)

    def test_a_copy_removed_from_the_source_keeps_its_removal(self):
        # A removed copy is never absorbed by a present one (its removal is a verified
        # outcome a reader of its id must still see); removed copies collapse together.
        gone = "2026-10-05T00:00:00.000000Z"
        rows = [manufacturer(1, 0, _missing=gone), manufacturer(1, 1, _missing=gone),
                manufacturer(1, 2), manufacturer(1, 3)]
        report, held = build({MANUFACTURER: rows})
        self.assertEqual(held["objects"], sorted([oid(rows[0]), oid(rows[2])]))
        self.assertEqual(report["counts"][COLLAPSED], 2)

    def test_presence_is_judged_as_known_at_the_cut(self):
        # An absence recorded after the cut is not yet known there: the object row says
        # present, so the copy collapses with the present ones, as the store reads it.
        gone = "2026-10-05T00:00:00.000000Z"
        rows = [manufacturer(1, 0, _missing=gone), manufacturer(1, 1), manufacturer(1, 2)]
        report, held = build({MANUFACTURER: rows}, as_known_at="2026-10-03T00:00:00.000000Z")
        self.assertEqual(len(held["objects"]), 1)
        self.assertEqual(report["counts"][COLLAPSED], 2)
        report, held = build({MANUFACTURER: [manufacturer(1, 0, _missing=gone),
                                             manufacturer(1, 1), manufacturer(1, 2)]})
        self.assertEqual(len(held["objects"]), 2)
        self.assertEqual(report["counts"][COLLAPSED], 1)

    def test_an_object_collapses_by_its_latest_version_only(self):
        older = manufacturer(1, 0)
        moved = manufacturer(1, 1, city="Moved")    # was a copy of ``older``, then changed
        moved["_versions"] = [{**manufacturer(1, 1), "id": moved["id"]}]
        report, held = build({MANUFACTURER: [older, moved]})
        self.assertEqual(held["objects"], sorted([oid(older), oid(moved)]))
        self.assertEqual(report["counts"].get(COLLAPSED, 0), 0)

    def test_a_relation_not_declared_is_never_collapsed(self):
        rows = [{"id": f"s{i}", "name": "Same", "created_at": "2026-01-01T00:00:00"}
                for i in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite"
            report = build_store(path, schema(), {"portal_api": Rows({SUPPLIER: rows})},
                                 clock=lambda: T)
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            count = conn.execute("SELECT COUNT(*) FROM objects WHERE object_id LIKE ?",
                                 ("Supplier:portal.app_supplier:%",)).fetchone()[0]
            conn.close()
        self.assertEqual(count, 5)
        self.assertEqual(report["counts"].get(COLLAPSED, 0), 0)


class StageDiskTests(unittest.TestCase):
    """What a build holds beside its store, stage by stage."""

    def stages(self, rows: int, **options: Any) -> tuple[list[tuple[str, int, int]], dict]:
        seen: list[tuple[str, int, int]] = []
        with tempfile.TemporaryDirectory() as tmp:
            report = build_store(Path(tmp) / "store.sqlite", schema(),
                                 synthetic_captures.sources(schema(), rows, only=["portal_api"]),
                                 clock=lambda: T,
                                 on_stage=lambda *a: seen.append(a), **options)
        return seen, report

    def test_every_stage_reports_its_bytes_and_measuring_changes_nothing(self):
        seen, report = self.stages(40)
        self.assertEqual([s[0] for s in seen], list(STAGES))
        self.assertTrue(all(type(w) is int and type(b) is int and w > 0 and b > 0
                            for _, w, b in seen), seen)
        with tempfile.TemporaryDirectory() as tmp:
            plain = build_store(Path(tmp) / "store.sqlite", schema(),
                                synthetic_captures.sources(schema(), 40, only=["portal_api"]),
                                clock=lambda: T)
        self.assertEqual(plain["content_digest"], report["content_digest"])
        self.assertEqual(plain["counts"], report["counts"])

    def test_the_working_file_holds_the_records_once_compressed(self):
        # The same suppliers twice, once with a wide served-nowhere column like the
        # Portal's long rows: what the wide rows add to the working file is what one
        # copy of their records costs there. Two copies (stage and ``v``, the layout
        # before) cost more than the pickles; a record compressed alone about half;
        # primed with its binding's dictionary, a few percent.
        words = random.Random(7).choices(
            ["batch", "lot", "release", "sterile", "fill", "line", "QA", "deviation",
             "temperature", "humidity", "operator", "signed", "vial", "label", "seal",
             "inspection", "passed", "hold", "retest", "supplier", "certificate"], k=260)
        text = " ".join(words)

        def work(wide: bool) -> tuple[int, int]:
            rows = [{"id": f"s{i:04d}", "name": f"Supplier {i}",
                     "created_at": "2026-01-01T00:00:00",
                     **({"notes": f"{text} #{i}"} if wide else {})} for i in range(600)]
            captured = sum(len(pickle.dumps(r, protocol=pickle.HIGHEST_PROTOCOL))
                           for r in Rows({SUPPLIER: rows})._stream(SUPPLIER))
            seen: list[tuple[str, int, int]] = []
            with tempfile.TemporaryDirectory() as tmp:
                build_store(Path(tmp) / "store.sqlite", schema(),
                            {"portal_api": Rows({SUPPLIER: rows})}, clock=lambda: T,
                            on_stage=lambda *a: seen.append(a))
            return max(w for _, w, _ in seen), captured

        narrow, wide = work(False), work(True)
        added = wide[1] - narrow[1]
        self.assertGreater(added, 600 * len(text) * 0.9)
        self.assertLess(wide[0] - narrow[0], 0.15 * added, (narrow, wide))

class ProbeDiskTests(unittest.TestCase):

    def test_stage_keys_are_the_projector_stages_and_the_verifier_records_them(self):
        from devloop import live_verify as lv
        self.assertEqual(probe.STAGE_KEYS, STAGES)
        self.assertEqual(tuple(lv.ENVELOPE_DISK["stages"]), STAGES)
        self.assertEqual(tuple(lv.ENVELOPE_DISK["peak"]), probe.PEAK_KEYS)

    def test_the_report_carries_layout_stages_and_the_largest_builds_objects(self):
        disk = probe._Disk(None, time.monotonic)
        spaces = iter([{"free_bytes": 1000, "total_bytes": 9},
                       {"free_bytes": 400, "total_bytes": 9},
                       {"free_bytes": 900, "total_bytes": 9},
                       {"free_bytes": 800, "total_bytes": 9}])
        with mock.patch.object(probe, "_space", lambda directory: next(spaces)):
            with disk.measured("/x", lambda: None):
                disk.stage("load", 30, 10)
                disk.stage("values", 50, 200)
            disk.built({"counts": {"objects": 77}})
            with disk.measured("/x", lambda: None):   # a smaller second build
                disk.stage("values", 20, 100)
            disk.built({"counts": {"objects": 70}})
        disk.keep("store_bytes", 300)
        out = disk.report()
        peak = out["disk"]["peak"]
        self.assertEqual(sorted(peak), sorted(probe.PEAK_KEYS))
        self.assertEqual((peak["build_bytes"], peak["free_bytes"], peak["objects"],
                          peak["work_bytes"], peak["layout"], peak["scratch_bytes"]),
                         (600, 1000, 77, 50, WORK_LAYOUT, 300))
        self.assertEqual(out["disk"]["stages"], {"load": 40, "values": 250})
        from devloop import live_verify as lv
        kept = lv._envelope_disk(out["disk"])
        self.assertEqual(kept["peak"], peak)
        self.assertEqual(kept["stages"], {"load": 40, "values": 250})
        disk.stage("not_a_stage", 1, 1)    # an unknown stage is never recorded
        self.assertNotIn("not_a_stage", disk.report()["disk"]["stages"])

    def test_an_older_layouts_peak_is_not_the_need_and_a_current_one_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp, "daily.json")
            old = 18_186_702_848    # the 2026-10-16 peak, measured under layout 1
            daily.write_text(json.dumps({"checks": [{"detail": {"fields": {"disk": {
                "peak": {"build_bytes": old, "store_bytes": 9_660_600_320}}}}}]}))
            self.assertIsNone(probe.previous_peak(str(daily)))
            source = Path(tmp, "mirror.sqlite")
            source.write_bytes(b"\0" * 1000)
            disk = probe._Disk(probe.previous_peak(str(daily)), time.monotonic)
            room = {"free_bytes": probe.BUILD_BYTES_PER_SOURCE_BYTE * 1000,
                    "total_bytes": 10 ** 6}
            with mock.patch.object(probe, "_space", return_value=room):
                disk.require(tmp, [str(source)])    # the ratio need fits: no refusal
            # Until a current-layout peak exists, the older peak scaled is a floor on the
            # forecast: the need is the larger of the two, never the smaller.
            floor = probe.forecast_floor(str(daily))
            self.assertEqual(floor, int(old * probe.LAYOUT_PEAK_SCALE))
            disk = probe._Disk(None, time.monotonic, floor=floor)
            with mock.patch.object(probe, "_space", return_value=room), \
                    self.assertRaises(probe.DiskInsufficient) as floored:
                disk.require(tmp, [str(source)])
            self.assertEqual(probe.error_line(floored.exception)["need_bytes"], floor)
            source.write_bytes(b"\0" * 4096)
            disk = probe._Disk(None, time.monotonic, floor=10)
            ratio = probe.BUILD_BYTES_PER_SOURCE_BYTE * 4096
            with mock.patch.object(probe, "_space", return_value={
                    "free_bytes": ratio - 1, "total_bytes": 10 ** 9}), \
                    self.assertRaises(probe.DiskInsufficient) as forecast:
                disk.require(tmp, [str(source)])
            self.assertEqual(probe.error_line(forecast.exception)["need_bytes"], ratio)
            with mock.patch.object(probe, "_space", return_value={
                    "free_bytes": 10 ** 9, "total_bytes": 10 ** 9}):
                disk.require(tmp, [str(source)])
            source.write_bytes(b"\0" * 1000)
            daily.write_text(json.dumps({"checks": [{"detail": {"fields": {"disk": {
                "peak": {"build_bytes": 5000, "layout": WORK_LAYOUT}}}}}]}))
            disk = probe._Disk(probe.previous_peak(str(daily)), time.monotonic)
            with mock.patch.object(probe, "_space", return_value=room), \
                    self.assertRaises(probe.DiskInsufficient) as refused:
                disk.require(tmp, [str(source)])
        line = probe.error_line(refused.exception)
        self.assertEqual((line["error"], line["code"], line["need_bytes"], line["free_bytes"]),
                         ("disk_insufficient", "disk_insufficient", 5000, room["free_bytes"]))

    def test_a_reused_generation_writes_no_copy_and_runs_no_stage(self):
        sources = synthetic_captures.sources(schema(), 6, only=["portal_api"])
        with tempfile.TemporaryDirectory() as tmp:
            first = build_generation(tmp, schema(), sources, clock=lambda: T)
            seen: list[Any] = []
            seeded: list[Any] = []
            again = build_generation(tmp, schema(), sources, clock=lambda: T, reuse=True,
                                     on_stage=lambda *a: seen.append(a),
                                     before_seed=seeded.append)
            kept = sorted(p.name for p in Path(tmp, "generations").iterdir())
        self.assertEqual((again.mode, again.store, again.content_digest),
                         ("reused", first.store, first.content_digest))
        self.assertEqual((seen, seeded), ([], []))
        self.assertEqual(kept, [first.id])


if __name__ == "__main__":
    unittest.main()
