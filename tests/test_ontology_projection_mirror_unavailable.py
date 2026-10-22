"""The ontology.projection live check while bound relations are unavailable upstream.

The live state of 2026-10-14 (later_ask portal-sage-regrant): eight bound ``sage.*``
relations the Portal catalog still lists with every approved column "missing or not
granted" and no key. A fake Workbench transport serves Portal customers and sales orders
beside such relations; the probe (``scripts.ontology_projection_probe``) cold, as the
session producer and as that session's consumer reports each as a ``blocked_upstream``
part (cause ``relation_unavailable``) and judges ``mirror_complete`` over the available
bound relations, so the verifier scores the spec blocked, never a pass; a bound relation
that is available but not captured, or unserved without a reason, still fails it. Temp
files, synthetic identifiers.
Evidence: injected-transport.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from verify import live_verify as lv
from integrations.tests.test_source_mirror import BASE, KEY
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import identifiers
from ontology import schema as schema_loader
from scripts import ontology_projection_probe as probe
from tests.test_ontology_projection_probe_removals import CUSTOMERS, ORDERS, workbench

ROOT = Path(__file__).resolve().parents[1]
GONE = tuple(f"sage.{name}" for name in (
    "ar_customer", "ar_ship_to", "oe_shipment_detail", "oe_shipment_header", "po_detail",
    "po_header", "po_receipt_detail", "po_receipt_header"))
SPEC = json.loads((ROOT / "verify/live-checks/ontology.projection.json").read_text("utf-8"))


def world(unavailable=(), failing=(), keyless=()):
    """The fake Workbench: ``unavailable`` relations listed with an upstream reason and no
    key, ``keyless`` ones listed unserved without a reason, ``failing`` ones served whose
    row reads always fail."""
    fake = workbench(2)
    for name in unavailable:
        fake.catalog[name] = {"columns": ["id", "name"], "key": None, "since": None,
                              "domain": "sage",
                              "unavailable": "2 approved columns are missing or not granted"}
    for name in keyless:
        fake.catalog[name] = {"columns": ["id"], "key": None, "since": None, "domain": "sage"}
    for name in failing:
        fake.catalog[name] = {"columns": ["id"], "key": ["id"], "since": None,
                              "domain": "sage"}
        fake.tables[name] = [{"id": "x1"}]
    rows = fake._rows

    def broken(name, query):
        if name in failing:
            raise RuntimeError("upstream failure")
        return rows(name, query)
    fake._rows = broken
    return WorkbenchReadApiClient(BASE, KEY, fetch=fake)


class MirrorUnavailableTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.schema = schema_loader.load_schema()

    def run_probe(self, client, extra=(), name="run"):
        (self.root / name).mkdir()
        return probe.projection_probe(
            client, str(self.root / name / "mirror.sqlite"),
            str(self.root / name / "store.sqlite"), schema=self.schema,
            normalizer=identifiers.normalize, claim_sample=20,
            bound=[CUSTOMERS, ORDERS, *extra])

    def scored(self, report, expect=None):
        """The verifier's failures on what this change decides (other floors are sized for
        the live data, not this fixture)."""
        return [f for f in lv.check_expectations(SPEC, 0, report, expect)
                if f.startswith(("mirror_complete", "mirror_unavailable_bound_count"))]

    def test_unavailable_bound_relations_block_and_never_pass(self):
        """(a) the live eight: blocked naming each, mirror_complete over the rest; 25 such
        relations still give one part each up to the verifier's cap, every one counted."""
        for names in (GONE[:1], GONE, tuple(f"sage.gone_{i:02d}" for i in range(25))):
            with self.subTest(count=len(names)):
                report = self.run_probe(world(unavailable=names), names, f"n{len(names)}")
                shown = sorted(names)[:probe.MAX_BLOCKED_PARTS]
                self.assertEqual((report["mirror_complete"],
                                  report["mirror_unavailable_bound_count"],
                                  report["mirror_unavailable_bound"]),
                                 (True, len(names), shown))
                self.assertEqual(report["completeness"]["bound_incomplete"], sorted(names))
                parts = lv.blocked_parts(report)
                self.assertEqual([(p["part"], p["source"], p["cause"]) for p in parts],
                                 [(n, probe.PORTAL, "relation_unavailable") for n in shown])
                # Parts present: expect_blocked decides and is met; the full expect is not.
                self.assertEqual(self.scored(report, SPEC["expect_blocked"]), [])
                self.assertEqual(self.scored(report),
                                 ["mirror_unavailable_bound_count != expected"])
        self.assertEqual(lv.degraded_relations({"result": "blocked", "detail": {"blocked": {
            "parts": lv.blocked_parts(report), "measured": True}}}),
            sorted(names)[:probe.MAX_BLOCKED_PARTS])

    def test_an_available_bound_relation_not_captured_still_fails(self):
        """(b) beside the unavailable eight, a served relation whose capture fails, or one
        unserved without an upstream reason, leaves mirror_complete false: a failure."""
        for kind in ("failing", "keyless"):
            with self.subTest(kind=kind):
                extra = ("sage.ic_item_uom",)
                report = self.run_probe(world(unavailable=GONE, **{kind: extra}),
                                        (*GONE, *extra), kind)
                self.assertIs(report["mirror_complete"], False)
                self.assertEqual(report["mirror_unavailable_bound_count"], 8)
                self.assertEqual([p["part"] for p in lv.blocked_parts(report)], list(GONE))
                self.assertEqual(self.scored(report, SPEC["expect_blocked"]),
                                 ["mirror_complete != expected"])

    def test_nothing_unavailable_keeps_the_strict_complete_mirror(self):
        """(c) every bound relation available and complete: true, no part, no count."""
        report = self.run_probe(world())
        self.assertEqual((report["mirror_complete"], report["mirror_unavailable_bound_count"]),
                         (True, 0))
        self.assertNotIn("blocked_upstream", report)
        self.assertEqual(self.scored(report), [])
        # A --build-only build (the probes that build through this one) adds no part and
        # keeps the strict figure.
        (self.root / "build").mkdir()
        built = probe.projection_probe(
            world(unavailable=GONE), str(self.root / "build" / "mirror.sqlite"),
            str(self.root / "build" / "store.sqlite"), schema=self.schema,
            normalizer=identifiers.normalize, build_only=True,
            bound=[CUSTOMERS, ORDERS, *GONE])
        self.assertIs(built["mirror_complete"], False)
        self.assertNotIn("blocked_upstream", built)

    def test_session_producer_and_consumer_name_each_once(self):
        """The session producer reports the eight; its manifest keeps them out, so its
        consumer (no sync of its own) names each exactly once, from the pinned mirror."""
        from scripts.mirror_measure import ordered_full_coverage
        bound = [CUSTOMERS, ORDERS, *GONE]
        db, session = str(self.root / "mirror.sqlite"), str(self.root / "projection")
        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        ordered_full_coverage(world(unavailable=GONE), db, resolve_sample=0, bound=bound)
        common = {"schema": self.schema, "normalizer": identifiers.normalize,
                  "session": session}
        with mock.patch("scripts.mirror_measure.bound_relations", return_value=bound):
            produced = probe.projection_probe(None, db, str(self.root / "p.sqlite"),
                                              produce=True, **common)
            (self.root / "c").mkdir()
            consumed = probe.projection_probe(None, db, str(self.root / "c" / "s.sqlite"),
                                              fresh_since=start, claim_sample=20, **common)
        manifest = json.loads(Path(session, probe.SESSION_MANIFEST).read_text())
        self.assertEqual(manifest["blocked_upstream"], [])
        self.assertEqual(consumed["session_projection"]["mode"], "reused")
        for report in (produced, consumed):
            self.assertEqual((report["mirror_complete"],
                              report["mirror_unavailable_bound_count"]), (True, 8))
            self.assertEqual([(p["part"], p["cause"]) for p in lv.blocked_parts(report)],
                             [(n, "relation_unavailable") for n in GONE])


if __name__ == "__main__":
    unittest.main()
