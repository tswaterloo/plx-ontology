"""C-09: a timestamp source's declared zone decides the stored instant.

Prisma writes UTC into ``timestamp without time zone`` (``naive_zone: "UTC"``); the
Portal's FileMaker ETL writes Eastern wall time labelled UTC (``wall_clock_zone``); an
undeclared zoneless value stays the written wall time. Built end to end: the source
mirror, the projector, the store's values and events; plus the schema refusing a zone
on a non-timestamp source or both zones at once."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from integrations.source_mirror import AudienceMap, SourceMirror
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.project import MirrorCaptures, build_store
from ontology.store import ProjectionStore, ReadPolicy
from ontology.tests import test_projection as fx


ROWS = {
    # naive, Eastern daylight time: UTC 02:30, wall clock 06:30Z
    "po-a": "2026-09-01T02:30:00",
    # zoned: naive_zone keeps the offset; wall clock drops it (08:30 EDT)
    "po-b": "2026-09-05T08:30:00+02:00",
    # the spring-forward hour Eastern skips (2026-03-08 02:30)
    "po-c": "2026-03-08T02:30:00",
    # the fall-back hour Eastern repeats (2026-11-01 01:30)
    "po-d": "2026-11-01T01:30:00Z",
    # a Prisma midnight: UTC midnight under naive_zone (the read rule keeps its day)
    "po-e": "2026-09-11T00:00:00",
}


def _domain():
    doc = copy.deepcopy(fx.DOMAIN)
    po = next(t for t in doc["object_types"] if t["id"] == "PurchaseOrder")
    props = [p for p in po["properties"] if p["name"] != "created_at"]
    props += [
        {"name": "created_at", "value_type": "timestamp",
         "sources": [fx._src("t.gold_po", "created_at", "iso_timestamp", naive_zone="UTC")]},
        {"name": "created_wall", "value_type": "timestamp",
         "sources": [fx._src("t.gold_po", "created_at", "iso_timestamp",
                             wall_clock_zone="America/New_York")]},
        {"name": "created_raw", "value_type": "timestamp",
         "sources": [fx._src("t.gold_po", "created_at", "iso_timestamp")]},
    ]
    po["properties"] = props
    placed = next(e for e in doc["event_types"] if e["id"] == "PurchaseOrder.placed")
    placed["time"] = {"binding": "t.gold_po", "path": "created_at",
                      "parser": "iso_timestamp", "wall_clock_zone": "America/New_York"}
    return doc


class TimestampZoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        fake = fx.FakeWorkbench()
        for key, stamp in ROWS.items():
            fake.set("public.app_purchase_order", key, created_at=stamp)
        clock = fx.Clock()
        audience = AudienceMap(fx.AUDIENCE, tenant="plx")
        mirror_path = root / "mirror.sqlite"
        mirror = SourceMirror(mirror_path, WorkbenchReadApiClient(fx.BASE, fx.KEY, fetch=fake),
                              audience, clock=clock, retry_delay_seconds=0, page_size=50)
        mirror.sync(fx.OPS, relations=sorted(fx.CATALOG), full=True)
        mirror.close()
        cls.store_path = root / "store" / "ontology.sqlite"
        with MirrorCaptures(mirror_path) as captures:
            cls.report = build_store(cls.store_path, fx.compiled_schema(_domain()),
                                     {"portal_api": captures}, normalizer=fx.normalizer,
                                     clock=lambda: fx.T1)
        cls.policy = ReadPolicy.from_audience_map(audience, marking_principals=fx.MARKINGS)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def values(self, key):
        store = ProjectionStore(self.store_path, self.policy)
        self.addCleanup(store.close)
        obj = store.object(fx.OPS, fx.oid("PurchaseOrder", "t.gold_po", id=key))
        return {v["property"]: v["value"] for v in obj["values"]
                if v["property"].startswith("created")}, store

    def test_each_declared_zone_gives_its_instant(self):
        self.assertEqual(self.values("po-a")[0], {
            "created_at": "2026-09-01T02:30:00.000000Z",       # Prisma: UTC
            "created_wall": "2026-09-01T06:30:00.000000Z",     # Eastern wall, EDT
            "created_raw": "2026-09-01T02:30:00.000000"})      # undeclared: as written
        self.assertEqual(self.values("po-b")[0], {
            "created_at": "2026-09-05T06:30:00.000000Z",       # a stated offset is kept
            "created_wall": "2026-09-05T12:30:00.000000Z",     # the false label dropped
            "created_raw": "2026-09-05T06:30:00.000000Z"})
        self.assertEqual(self.values("po-e")[0]["created_at"], "2026-09-11T00:00:00.000000Z")

    def test_a_wall_time_the_zone_skips_or_repeats_is_a_gap_never_a_guess(self):
        for key in ("po-c", "po-d"):
            values, _ = self.values(key)
            self.assertNotIn("created_wall", values, key)
            self.assertIn("created_at", values, key)   # the other declarations still read
        gaps = {(g["code"], g["where"]) for g in self.report["gaps"]}
        self.assertIn(("parse_wall_time_ambiguous", "PurchaseOrder.created_wall@t.gold_po"),
                      gaps)
        self.assertIn(("event_time_wall_time_ambiguous", "PurchaseOrder.placed"), gaps)

    def test_the_event_time_honours_the_zone(self):
        _, store = self.values("po-a")
        placed = [e for e in store.events(fx.OPS, fx.oid("PurchaseOrder", "t.gold_po", id="po-a"))
                  if e["event_type"] == "PurchaseOrder.placed"]
        self.assertEqual([e["time"] for e in placed], ["2026-09-01T06:30:00.000000Z"])
        skipped = [e for e in store.events(fx.OPS, fx.oid("PurchaseOrder", "t.gold_po", id="po-c"))
                   if e["event_type"] == "PurchaseOrder.placed"]
        self.assertEqual(skipped, [])   # the skipped hour: no event, never a shifted one


class ZoneDeclarationTests(unittest.TestCase):
    def refused(self, mutate):
        doc = _domain()
        mutate(next(t for t in doc["object_types"] if t["id"] == "PurchaseOrder"))
        with self.assertRaises(Exception) as caught:
            fx.compiled_schema(doc)
        return str(caught.exception)

    def test_both_zones_are_refused(self):
        def both(po):
            src = next(p for p in po["properties"] if p["name"] == "created_at")["sources"][0]
            src["wall_clock_zone"] = "America/New_York"
        self.assertIn("exclude each other", self.refused(both))

    def test_a_zone_on_a_date_source_is_refused(self):
        def on_date(po):
            prop = next(p for p in po["properties"] if p["name"] == "order_date")
            prop["sources"][1]["naive_zone"] = "UTC"
        self.assertIn("needs an iso_timestamp source", self.refused(on_date))

    def test_an_unknown_zone_is_refused(self):
        def unknown(po):
            src = next(p for p in po["properties"] if p["name"] == "created_wall")["sources"][0]
            src["wall_clock_zone"] = "Europe/Paris"
        self.refused(unknown)


if __name__ == "__main__":
    unittest.main()
