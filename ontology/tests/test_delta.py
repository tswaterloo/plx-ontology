"""Row-delta updates (``ontology/delta.py``): a delta over a served generation serves, for
every changed record, exactly the rows a full build of the same captures holds.

The fixture Portal mirror (real ``SourceMirror``, injected transport) is captured, a full
build is the served generation, rows change upstream (one changed, one added, one
re-pointed to another order) and are captured again; the delta cycle then runs over the
served generation, and each object it owns is compared row for row (versions, values,
identifiers, events, links at both ends) with a full build of the new captures.
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from integrations.source_mirror import AudienceMap, SourceMirror
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import delta
from ontology.project import MirrorCaptures, build_store
from ontology.tests.test_projection import (AUDIENCE, BASE, CATALOG, KEY, OPS, T1, T2, Clock,
                                            FakeWorkbench, compiled_schema, normalizer)


class DeltaEquivalenceTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fake, self.clock = FakeWorkbench(), Clock()
        self.mirror_path = self.root / "mirror.sqlite"
        self.mirror = SourceMirror(self.mirror_path,
                                   WorkbenchReadApiClient(BASE, KEY, fetch=self.fake),
                                   AudienceMap(AUDIENCE, tenant="plx"), clock=self.clock,
                                   retry_delay_seconds=0, page_size=2)
        self.addCleanup(self.mirror.close)
        self.schema = compiled_schema()
        self.state = self.root / "state"
        self.state.mkdir()

    def capture(self, at):
        self.clock.now = at
        self.mirror.sync(OPS, relations=sorted(CATALOG), full=True)

    def full(self, name):
        with MirrorCaptures(self.mirror_path) as captures:
            build_store(self.root / name, self.schema, {"portal_api": captures},
                        normalizer=normalizer, clock=lambda: T2)
        return self.root / name

    def cycle(self, base, **options):
        return delta.build_delta(self.state, self.mirror_path, self.root / "delta",
                                 base_path=base, schema=self.schema, normalizer=normalizer,
                                 post_project={}, environ={}, **options)

    def owned(self, store):
        with sqlite3.connect(store) as conn:
            return [r[0] for r in conn.execute("SELECT object_id FROM delta_owned ORDER BY 1")]

    def test_changed_records_equal_a_full_build_and_nothing_else_is_owned(self):
        self.capture(T1)
        base = self.full("gen1.sqlite")
        # a changed order, a changed line re-pointed to another order, a new line
        self.fake.set("public.app_purchase_order", "po-a", status="RECEIVED",
                      total_cost="36.00")
        self.fake.set("public.app_purchase_order_line", "l1", purchase_order_id="po-b")
        self.fake.tables["public.app_purchase_order_line"].append(
            {"id": "l9", "purchase_order_id": "po-c", "quantity": "7", "uom": "EA",
             "unit_cost": "1"})
        self.capture(T2)
        report = self.cycle(base)
        full = self.full("gen2.sqlite")
        owned = self.owned(report["store"])
        self.assertEqual(report["changed"]["portal_api"], 3)
        self.assertEqual(len(owned), 3, owned)
        result = delta.compare(Path(report["store"]), full, owned)
        self.assertEqual((result["equal"], result["differ"]), (3, 0), result)

        # the pointer names the served generation; a second cycle with nothing new
        # projects the same records again (nothing applied twice, nothing lost)
        pointer = json.loads((self.root / "delta" / delta.POINTER).read_text())
        with sqlite3.connect(base) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        self.assertEqual(pointer["base_digest"], digest)
        again = self.cycle(base)
        self.assertEqual(again["layer"], "none: nothing new")
        self.assertEqual(json.loads((self.root / "delta" / delta.POINTER).read_text()),
                         pointer)

        # a later change is a layer of its own over the first: it owns only that record,
        # equal to a full build of the newer captures; the first layer stays listed
        self.fake.set("public.app_purchase_order_line", "l9", quantity="8")
        self.capture(T2 + timedelta(hours=1))
        layer = self.cycle(base)
        self.assertEqual(layer["layer"], "added")
        newer = self.full("gen3.sqlite")
        later = self.owned(layer["store"])
        self.assertEqual(len(later), 1, later)
        self.assertEqual(delta.compare(Path(layer["store"]), newer, later)["differ"], 0)
        pointer = json.loads((self.root / "delta" / delta.POINTER).read_text())
        self.assertEqual([item["store"] for item in pointer["layers"]],
                         [layer["store"], report["store"]])

    def test_an_unchanged_record_re_derived_equals_the_served_generation(self):
        """The closure is complete: re-deriving records that did not change gives the
        served generation's rows exactly, links to unchanged partners included."""
        self.capture(T1)
        base = self.full("gen1.sqlite")
        self.capture(T2 + timedelta(hours=1))   # a capture that changes nothing
        forced = {"portal_api": [("public.app_purchase_order", json.dumps({"id": "po-b"},
                                                                          separators=(",", ":"))),
                                 ("public.app_purchase_order_line",
                                  json.dumps({"id": "l2"}, separators=(",", ":")))]}
        report = self.cycle(base, force_changed=forced, publish=False)
        owned = self.owned(report["store"])
        self.assertEqual(len(owned), 2, owned)
        result = delta.compare(Path(report["store"]), base, owned)
        self.assertEqual(result["differ"], 0, result)


if __name__ == "__main__":
    unittest.main()
