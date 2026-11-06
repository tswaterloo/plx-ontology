"""Row-delta updates (``ontology/delta.py``): a delta over a served generation serves, for
every changed record, exactly the rows a full build of the same captures holds.

The fixture Portal mirror (real ``SourceMirror``, injected transport) is captured, a full
build is the served generation, rows change upstream (one changed, one added, one
re-pointed to another order) and are captured again; the delta cycle then runs over the
served generation, and each object it owns is compared row for row (versions, values,
identifiers, events, links at both ends) with a full build of the new captures. Both
builds run the production build steps (``operating.build_steps.post_project``).
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from integrations.source_mirror import AudienceMap, SourceMirror
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import delta
from ontology.project import MirrorCaptures, build_store
from operating import build_steps
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
                        normalizer=normalizer, clock=lambda: T2,
                        post_project=build_steps.post_project(
                            self.state, "plx", scratch=self.root / "steps"))
        return self.root / name

    def cycle(self, base, **options):
        return delta.build_delta(self.state, self.mirror_path, self.root / "delta",
                                 base_path=base, schema=self.schema, normalizer=normalizer,
                                 environ={}, **options)

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
        # no people exclusion in this environment: mail is skipped, the rest carries on
        self.assertTrue(report["mail_skipped"])
        # the orders the moved and the new line now name gain a link: owned too, so each
        # lists its new line (back-links), all equal to the full build
        self.assertEqual(sorted(o.split(":", 2)[2] for o in owned),
                         ['{"id":"l1"}', '{"id":"l9"}', '{"id":"po-a"}', '{"id":"po-b"}',
                          '{"id":"po-c"}'], owned)
        result = delta.compare(Path(report["store"]), full, owned)
        self.assertEqual((result["equal"], result["differ"]), (5, 0), result)

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
        self.assertEqual(len(later), 2, later)   # the line, and its order (re-linked)
        self.assertEqual(delta.compare(Path(layer["store"]), newer, later)["differ"], 0)
        pointer = json.loads((self.root / "delta" / delta.POINTER).read_text())
        self.assertEqual([item["store"] for item in pointer["layers"]],
                         [layer["store"], report["store"]])

        # the fold replaces both layers by one holding every change, equal to the full
        # build of the newest captures
        fold = self.cycle(base, compact_only=True)
        self.assertEqual(fold["layer"], "compacted")
        pointer = json.loads((self.root / "delta" / delta.POINTER).read_text())
        self.assertEqual([item["store"] for item in pointer["layers"]], [fold["store"]])
        folded = self.owned(fold["store"])
        self.assertEqual(set(folded), set(owned) | set(later))
        self.assertEqual(delta.compare(Path(fold["store"]), newer, folded)["differ"], 0)

    def served_by(self, object_id):
        """The store of the newest published layer owning ``object_id`` (None: served)."""
        pointer = json.loads((self.root / "delta" / delta.POINTER).read_text())
        return next((item["store"] for item in pointer["layers"]
                     if object_id in self.owned(item["store"])), None)

    def test_a_fold_keeps_a_change_a_cycle_has_not_published_yet(self):
        """The fold replaces every layer it read: a change a cycle discovered but did not
        publish (still open) is folded too, so a record never falls back to the served
        version, and stays open for the next cycle."""
        self.capture(T1)
        base = self.full("gen1.sqlite")
        self.fake.set("public.app_purchase_order", "po-a", status="RECEIVED",
                      total_cost="36.00")
        self.capture(T2)
        first = self.cycle(base)
        po_a = next(o for o in self.owned(first["store"]) if '"po-a"' in o)
        self.fake.set("public.app_purchase_order_line", "l1", quantity="99")
        self.capture(T2 + timedelta(minutes=30))
        self.cycle(base)
        # po-a changes again; a cycle discovers it but publishes nothing
        self.fake.set("public.app_purchase_order", "po-a", status="CLOSED", total_cost="40.00")
        self.capture(T2 + timedelta(hours=1))
        self.cycle(base, publish=False)
        fold = self.cycle(base, compact_only=True)
        self.assertEqual(self.served_by(po_a), fold["store"])
        newest = self.full("gen-newest.sqlite")
        self.assertEqual(delta.compare(Path(fold["store"]), newest, [po_a])["differ"], 0)
        # still open: the next cycle projects it into a layer of its own
        again = self.cycle(base)
        self.assertEqual(again["layer"], "added")
        self.assertIn(po_a, self.owned(again["store"]))

    def test_a_cycle_that_fails_before_publishing_leaves_its_changes_to_the_next(self):
        self.capture(T1)
        base = self.full("gen1.sqlite")
        self.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        self.capture(T2)
        with mock.patch.object(delta, "project_layer", side_effect=RuntimeError("cut")):
            with self.assertRaises(RuntimeError):
                self.cycle(base)
        self.assertFalse((self.root / "delta" / delta.POINTER).exists())
        report = self.cycle(base)   # nothing new is discovered, the open change is kept
        self.assertEqual(report["new_changes"]["portal_api"], 0)
        owned = self.owned(report["store"])
        self.assertTrue(any('"po-a"' in o for o in owned), owned)
        self.assertEqual(delta.compare(Path(report["store"]), self.full("gen2.sqlite"),
                                       owned)["differ"], 0)

    def test_a_value_changed_and_changed_back_is_projected_with_its_history(self):
        """A -> B -> A: the current row reads as the served one, but a version in between
        differs, so the record is a change (a full build holds all three versions)."""
        self.capture(T1)
        base = self.full("gen1.sqlite")
        self.fake.set("public.app_purchase_order", "po-b", status="CANCELLED")
        self.capture(T2)
        self.fake.set("public.app_purchase_order", "po-b", status="OPEN")
        self.capture(T2 + timedelta(hours=1))
        report = self.cycle(base)
        owned = self.owned(report["store"])
        po_b = [o for o in owned if '"po-b"' in o]
        self.assertTrue(po_b, owned)
        self.assertEqual(delta.compare(Path(report["store"]), self.full("gen2.sqlite"),
                                       po_b)["differ"], 0)

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



class StateUpgradeTests(unittest.TestCase):

    def test_a_state_holding_bc_paths_alone_reads_every_record_as_kind_and_path(self):
        """A state written when a BC record was its path alone, beside records written
        since as (kind, path): one key shape per source after opening, so every closure
        sorts and binds (live, 2026-11-06: "'<' not supported between instances of
        'tuple' and 'str'" and "Incorrect number of bindings supplied")."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            old = delta.State(path)
            old.conn.execute("DELETE FROM meta WHERE name = 'state_format'")
            bc = "production/ca41/salesOrders/4319"
            for table, rows in (
                    ("changed", [("bc", json.dumps(bc), "t", 1),
                                 ("bc", json.dumps(["bc", "production/ca41/items/1"]), "t", 0)]),
                    ("context", [("bc", json.dumps(bc)), ("bc", json.dumps(["bc", bc]))]),
                    ("owned", [("SalesOrder:bc.sales_orders:x", "bc", json.dumps(bc))])):
                marks = ", ".join("?" * len(rows[0]))
                old.conn.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
            old.conn.close()
            state = delta.State(path)
            records = state.context("bc") | set(state.changed("bc"))
            self.assertEqual(sorted(records), [("bc", "production/ca41/items/1"),
                                               ("bc", bc)])
            self.assertEqual(state.changed("bc", open_only=True),
                             [("bc", "production/ca41/items/1")])
            self.assertEqual(state.owned_records(["SalesOrder:bc.sales_orders:x"]),
                             {"SalesOrder:bc.sales_orders:x": ("bc", ("bc", bc))})


class HandleTests(unittest.TestCase):

    def test_each_family_reads_a_record_from_its_handle_and_back(self):
        handles = [
            "api://public.app_purchase_order/%7B%22id%22%3A%22po-a%22%7D@5a1c29f59e06162d-18",
            "graph-mail://0211d86b-8ede/AAkALgAA-EWg0A@CQAAABYAAAA7#attachment=AAk&sha256=47",
            "bc://production/ca41a27f/salesOrders/4319a7ac@ae9590e2cdcedba5-3620",
            "teams-chat://19%3Ameeting_MDY%40thread.v2/1766155101264@c7f46f0e7f4f3f3b-412",
            "mc://live/tasks/TASK-4725@7e3f6f53ea5c2690-68827",
            "planner-task://1zvuhHH6/egEOewGr@43884bf09f09ba3c-9511",
            "sharepoint://b%21ArixYskX/01QVQSKBX7@%22%7BA4EB%7D%2C82%22",
            "sharepoint-list://petrasoap.sharepoint.com%2C6b19/50792b7b/100@f5df870b6562679a-1"]
        for handle in handles:
            with self.subTest(handle.split("://")[0]):
                source, key = delta.record_of_handle(handle)
                self.assertTrue(handle.startswith(delta.handle_prefix(source, key)))
        self.assertEqual(delta.record_of_handle(handles[6])[1], ("item", "b!ArixYskX",
                                                                 "01QVQSKBX7"))


if __name__ == "__main__":
    unittest.main()
