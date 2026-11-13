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
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from integrations.source_mirror import AudienceMap, SourceMirror
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import delta
from ontology.project import MirrorCaptures, build_store
from operating import build_steps
from ontology.tests.test_projection import (AUDIENCE, BASE, CATALOG, KEY, OPS, T1, T2, Clock,
                                            FakeWorkbench, compiled_schema, normalizer)


class DeltaCase(unittest.TestCase):
    """A captured fixture Portal mirror, its full builds and delta cycles over them."""

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



class DeltaEquivalenceTests(DeltaCase):

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
        # the layer's store facts are kept beside it as it is published: no host reads
        # them on a reader's first plx.around (operating.ontology_tools_traverse)
        from operating import ontology_tools_traverse as traverse
        self.assertIs(report["layer_facts"], True)
        self.assertTrue(traverse.facts_path(report["store"]).is_file())
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

    def test_a_record_restamped_after_its_layer_is_not_projected_again(self):
        """The Portal ETL rewrites rows it did not change: a record a layer holds whose
        newer version reads the same on every path the build reads is not projected
        again (live 2026-11-07: 110,216 such rows fanned out to 418,523 records and 7.3M
        values in one cycle); a real change to it is, and a path read only by an event
        time is a change too."""
        self.capture(T1)
        base = self.full("gen1.sqlite")
        self.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        self.capture(T2)
        first = self.cycle(base)
        po_a = next(o for o in self.owned(first["store"]) if '"po-a"' in o)
        # a rewrite of a part of the row no projection reads
        self.fake.set("public.app_purchase_order", "po-a",
                      custom_fields={"source": "etl", "fmGeneratedPoId": 1, "etl_run": 2})
        self.capture(T2 + timedelta(minutes=30))
        again = self.cycle(base)
        self.assertEqual(again["layer"], "none: nothing new", again)
        self.assertEqual(again["new_changes"]["portal_api"], 0)
        # what serves po-a (the first layer) is what a full build of the newest captures
        # derives for it
        full = self.full("gen-restamped.sqlite")
        self.assertEqual(self.served_by(po_a), first["store"])
        # (the version history aside: the rewrite cites a newer capture, says the same)
        result = delta.compare(Path(first["store"]), full, [po_a], current=True)
        self.assertEqual((result["equal"], result["differ"]), (1, 0), result)
        # a real change to the same record is projected
        self.fake.set("public.app_purchase_order", "po-a", status="CLOSED")
        # and a receipt time (read by its event alone) is a change, not a restamp
        self.fake.set("public.app_receipt", "r1", received_at="2026-09-22T09:00:00Z")
        self.capture(T2 + timedelta(hours=1))
        later = self.cycle(base)
        owned = self.owned(later["store"])
        r1 = [o for o in owned if '"r1"' in o]
        self.assertIn(po_a, owned)
        self.assertTrue(r1, owned)
        newest = self.full("gen-changed.sqlite")
        self.assertEqual(delta.compare(Path(later["store"]), newest, [po_a, *r1])["differ"],
                         0)

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

    def test_a_cycle_without_room_for_its_size_is_refused_and_its_changes_kept(self):
        """11-11: cycles failed "database or disk is full" and near the memory limit an
        hour in. A cycle short of disk (work and temp) or memory for the records it follows
        is refused before its closure; its changes stay open for the next cycle."""
        self.capture(T1)
        base = self.full("gen1.sqlite")
        self.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        self.capture(T2)
        with mock.patch.object(delta, "PREFLIGHT_DISK_PER_RECORD", 1 << 60), \
                mock.patch.object(delta, "closure", side_effect=AssertionError("closure")):
            refused = self.cycle(base)
        self.assertEqual(refused["status"], "preflight_refused", refused)
        self.assertGreaterEqual(refused["preflight"]["records"], 1)
        self.assertTrue(all(k.startswith("disk:") for k in refused["preflight"]["short"]))
        with mock.patch.object(delta, "_memory_room", return_value=1 << 20):
            refused = self.cycle(base)
        self.assertEqual(list(refused["preflight"]["short"]), ["memory"], refused)
        self.assertFalse((self.root / "delta" / delta.POINTER).exists())
        report = self.cycle(base)   # room again: the kept change is projected
        self.assertNotIn("status", report)
        self.assertTrue(any('"po-a"' in o for o in self.owned(report["store"])))

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



class DeltaDocumentTests(DeltaCase):
    """A cycle reads files under a budget: a file it has no time for is pending, never
    unreadable; the rows of its record publish, the record stays open, and the next
    cycle's layer equals a full build. Readings live in the delta directory, where a
    killed cycle leaves them, and the next cycle takes in what a killed one read."""

    def setUp(self):
        super().setUp()
        from integrations.tests.test_capture_sharepoint import CaptureCase, scope
        from ontology.schema import load_schema
        from ontology.tests.test_document_fields import SLIP, pdf
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        case = CaptureCase("run")
        case.setUp()
        self.addCleanup(case.tearDown)
        case.drive.file("01SLIP", "packing list PO100235.pdf", "01AOS", pdf(SLIP))
        self.files, self.scope, self.state = case, scope, case.state
        self.schema, self.slip = load_schema(), pdf(SLIP)

    def capture(self, at):
        super().capture(at)
        self.assertTrue(self.files.capture().run(self.scope())["complete"])

    def full(self, name):
        from ontology.project import SharePointCaptures
        with MirrorCaptures(self.mirror_path) as captures, \
                SharePointCaptures(self.state) as files:
            build_store(self.root / name, self.schema,
                        {"portal_api": captures, "sharepoint": files},
                        normalizer=normalizer, clock=lambda: T2,
                        post_project=build_steps.post_project(
                            self.state, "plx", scratch=self.root / "steps"))
        return self.root / name

    def cycle(self, base, environ=None, **options):
        return delta.build_delta(self.state, self.mirror_path, self.root / "delta",
                                 base_path=base, schema=self.schema, normalizer=normalizer,
                                 environ=environ or {}, **options)

    def test_a_file_without_time_is_pending_and_its_record_follows_a_layer_later(self):
        self.capture(T1)
        base = self.full("gen1.sqlite")
        # a new version of the packing list: bytes no build has read
        self.files.drive.change("01SLIP", self.slip.replace(b"PL-5512", b"PL-5513"))
        self.capture(T2)
        from integrations.tests.test_capture_sharepoint import DRIVE
        record = ("item", DRIVE, "01SLIP")
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            first = self.cycle(base, environ={"PLX_DELTA_DOCUMENT_SECONDS": "1e-9"})
        self.assertEqual(first["documents"]["held_open"], {"sharepoint": 1}, first)
        self.assertEqual(first["documents"]["timed_out"], 0)
        # the layer and the pointer say the record's document fields are pending
        with sqlite3.connect(first["store"]) as conn:
            noted = json.loads(conn.execute("SELECT value FROM store_meta WHERE key = "
                                            "'documents_pending'").fetchone()[0])
        self.assertEqual(noted["records"], {"sharepoint": [list(record)]})
        self.assertEqual(json.loads((self.root / "delta" / delta.POINTER).read_text())
                         ["documents"]["pending"], 1)
        state = delta.State(self.root / "delta" / "state.sqlite")
        self.addCleanup(state.close)
        # held: queued for a later cycle, not followed by every cycle
        self.assertEqual(state.queued(), {"sharepoint": [record]})
        self.assertNotIn(record, state.changed("sharepoint", open_only=True))
        # nothing was kept for the file: it is read, not served as unreadable, next time
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            second = self.cycle(base)
        self.assertEqual(second["held_queue"]["taken"], 1, second)
        self.assertEqual(second["documents"]["pending"], 0, second)
        self.assertEqual(state.queued(), {})
        self.assertNotIn(record, state.changed("sharepoint", open_only=True))
        full = self.full("gen2.sqlite")
        # the first layer lacks what the file's fields make; the second equals a full build
        held = self.owned(first["store"])
        self.assertGreater(delta.compare(Path(first["store"]), full, held)["differ"], 0)
        owned = self.owned(second["store"])
        self.assertLessEqual(set(held), set(owned))
        self.assertEqual(delta.compare(Path(second["store"]), full, owned)["differ"], 0)
        # the readings stay in the delta directory (none left in a work directory)
        delta_dir = self.root / "delta"
        self.assertTrue(all((delta_dir / name).is_file() for name in delta.KEPT))
        self.assertFalse([p for p in delta_dir.iterdir() if p.name.startswith("work-")])
        # the superseded version is found by every scan since the generation: once its
        # layer holds it, a cycle with nothing new projects nothing (live 2026-11-07:
        # about 1,900 such files re-projected with 25,000 records every cycle)
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            third = self.cycle(base)
        self.assertEqual(third["layer"], "none: nothing new", third)
        self.assertEqual(third["new_changes"].get("sharepoint"), 0)

    def test_a_cycle_follows_its_new_changes_and_one_batch_of_held_records(self):
        # 11-12: 200k records held for unread files were followed, with their closure,
        # by every cycle: 3.2 h for 24 new changes. Held records now wait in a queue and
        # a cycle takes back one batch; its new changes still equal a full build, and
        # the queue drains with no record lost or projected twice in one cycle.
        from integrations.tests.test_capture_sharepoint import DRIVE
        self.files.drive.file("02SLIP", "packing list 2.pdf", "01AOS", self.slip)
        self.files.drive.file("03SLIP", "packing list 3.pdf", "01AOS", self.slip)
        self.capture(T1)
        base = self.full("gen1.sqlite")
        from ontology.tests.test_document_fields import SLIP, pdf
        for n, iid in enumerate(("01SLIP", "02SLIP", "03SLIP")):   # three files, each new
            self.files.drive.change(iid, pdf([[line.replace("PL-5512", f"PL-6{n}00")
                                               for line in page] for page in SLIP]))
        self.capture(T2)
        records = [("item", DRIVE, iid) for iid in ("01SLIP", "02SLIP", "03SLIP")]
        one = {"PLX_DELTA_HELD_BATCH": "1"}
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            first = self.cycle(base, environ={**one, "PLX_DELTA_DOCUMENT_SECONDS": "1e-9"})
        self.assertEqual(first["documents"]["held_open"], {"sharepoint": 3}, first)
        self.assertEqual(first["documents"]["pending"], 3, first)
        state = delta.State(self.root / "delta" / "state.sqlite")
        self.addCleanup(state.close)
        self.assertEqual(sorted(state.queued()["sharepoint"]), records)
        # one new change: the cycle follows it and one held record, not all three
        self.fake.set("public.app_purchase_order", "po-a", status="RECEIVED",
                      total_cost="36.00")
        self.capture(T2 + timedelta(hours=1))
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            second = self.cycle(base, environ=one)
        self.assertEqual(second["held_queue"], {"batch": 1, "taken": 1}, second)
        self.assertEqual(second["followed"]["sharepoint"], 1, second)
        self.assertEqual(second["followed"]["portal_api"], 1, second)
        # the layer still says every pending file and held record, not only its own
        self.assertEqual(second["documents"]["pending"], 2, second)
        self.assertEqual(second["documents"]["queued"], 2, second)
        with sqlite3.connect(second["store"]) as conn:
            noted = json.loads(conn.execute("SELECT value FROM store_meta WHERE key = "
                                            "'documents_pending'").fetchone()[0])
        self.assertEqual(sorted(map(tuple, noted["records"]["sharepoint"])),
                         sorted(state.queued()["sharepoint"]))
        full = self.full("gen2.sqlite")
        owned = self.owned(second["store"])
        self.assertTrue([o for o in owned if "po-a" in o], owned)
        self.assertEqual(delta.compare(Path(second["store"]), full, owned)["differ"], 0)
        # the queue drains one record a cycle, oldest first, each equal to a full build
        layers = {}
        for _ in range(2):
            with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
                more = self.cycle(base, environ=one)
            self.assertEqual(more["held_queue"]["taken"], 1, more)
            self.assertEqual(sum(more["followed"].values()), 1, more)
            mine = self.owned(more["store"])
            self.assertEqual(delta.compare(Path(more["store"]), full, mine)["differ"], 0)
            layers[more["store"]] = mine
        self.assertEqual(state.queued(), {})
        self.assertEqual(state.held_files().execute("SELECT count(*) FROM files")
                         .fetchone()[0], 0)
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            last = self.cycle(base, environ=one)
        self.assertEqual(last["layer"], "none: nothing new", last)
        # every held record was projected again after its file was read
        projected = {o for owned_ids in [owned, *layers.values()] for o in owned_ids}
        for iid in ("01SLIP", "02SLIP", "03SLIP"):
            self.assertTrue([o for o in projected if iid in o], (iid, projected))

    def test_a_killed_cycles_work_is_taken_in_and_its_directory_removed(self):
        delta_dir = self.root / "delta"
        killed = delta_dir / "work-20261106T090527966581"
        live = delta_dir / "work-20261106T101114626273"
        for work in (killed, live):
            (work / "gen").mkdir(parents=True)
        with sqlite3.connect(killed / "gen" / "fields.sqlite") as conn:
            conn.execute("CREATE TABLE fields (key TEXT PRIMARY KEY, code TEXT NOT NULL, "
                         "value TEXT NOT NULL, used INTEGER NOT NULL)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO fields VALUES ('[\"aa\"]', 'code-1', '{}', 7)")
        orphan = delta_dir / ".delta-20261106T090527966581.sqlite.build-0a1b2c"
        owned = delta_dir / ".delta-20261106T101114626273.sqlite.build-3d4e5f"
        for path in (orphan, owned):
            path.write_bytes(b"partial store")
        old = 1_000_000_000
        for path in (killed, live, orphan, owned):
            os.utime(path, (old, old))
        with delta._locked(live / ".busy"):   # a cycle still works in this one
            swept = delta.sweep(delta_dir)
        self.assertEqual(swept, {"work_dirs": 1, "files": 1, "adopted": 1, "left": 0})
        self.assertFalse(killed.exists() or orphan.exists())
        self.assertTrue(live.is_dir() and owned.is_file())
        with sqlite3.connect(delta_dir / "fields.sqlite") as conn:
            self.assertEqual(conn.execute("SELECT key, code FROM fields").fetchall(),
                             [('["aa"]', "code-1")])
        # readings that cannot be read now: the directory waits for a later sweep
        broken = delta_dir / "work-20261106T110000000000"
        (broken / "gen").mkdir(parents=True)
        (broken / "gen" / "readings.sqlite").write_bytes(b"not a database, made-up" * 64)
        recent = time.time() - 3600
        os.utime(broken, (recent, recent))
        self.assertEqual(delta.sweep(delta_dir)["left"], 1)
        self.assertTrue(broken.is_dir())


class DeltaMailDocumentTests(DeltaCase):
    """The same for a mail attachment: a message whose attachment a cycle had no time for
    stays open, and the next cycle's layer equals a full build."""

    def setUp(self):
        super().setUp()
        from integrations.capture_mail import MailCaptureStore
        from integrations.tests import test_capture_mail as tcm
        from ontology.schema import load_schema
        from ontology.tests.test_document_fields import QUOTE, SLIP, pdf
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.tcm, self.schema = tcm, load_schema()
        self.graph = tcm.FakeGraph()
        self.graph.put(tcm.BOX_A, "MSG-D0", "CK-D0", subject="Documents 0",
                       body="Attached for PO100230.",
                       attachments=[tcm.file_attachment("ATT-D0", pdf(SLIP), "doc0.pdf")])
        self.quote = pdf(QUOTE)
        self.store = MailCaptureStore(self.state, clock=tcm.Clock())
        self.addCleanup(self.store.close)
        self.environ = {"PLX_EXCLUDED_PEOPLE_OIDS": tcm.ENV["PLX_EXCLUDED_PEOPLE_OIDS"]}

    def capture(self, at):
        from integrations import graph_read
        from integrations.capture_mail import MailCapture
        from integrations.graph_read import GraphMailReader
        from integrations.live_source_guard import BoundedBackoff
        super().capture(at)
        api, _ = graph_read.build_graph(self.tcm.ENV, sender=self.graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(self.store, GraphMailReader(api, self.tcm.capture_audience()),
                          self.tcm.CAPTURE, sleep=lambda s: None).run(self.tcm.SCOPE)
        self.assertTrue(run["complete"], run)

    def full(self, name):
        from ontology.project import MailCaptures
        with MirrorCaptures(self.mirror_path) as captures, MailCaptures(
                self.state, internal_domains=delta._internal_domains(self.environ)) as mail:
            build_store(self.root / name, self.schema,
                        {"portal_api": captures, "graph_mail": mail},
                        normalizer=normalizer, clock=lambda: T2,
                        post_project=build_steps.post_project(
                            self.state, "plx", scratch=self.root / "steps"))
        return self.root / name

    def cycle(self, base, environ=None, **options):
        return delta.build_delta(self.state, self.mirror_path, self.root / "delta",
                                 base_path=base, schema=self.schema, normalizer=normalizer,
                                 environ={**self.environ, **(environ or {})}, **options)

    def test_a_mailbox_the_guard_cannot_decide_after_the_closure_is_left_out_not_fatal(self):
        # CC-P-01: the changed mail is decided when it is found (``discover``); a mailbox
        # met only after the closure (a thread's context, a back-link) that the people
        # exclusion guard could not decide raised ExclusionUnavailable there, failing the
        # whole cycle after its closure was paid for (10.4 CPU-h of failed cycles). It is
        # now left out (never read) and counted; the cycle publishes the rest.
        from integrations.people_exclusion import ExclusionUnavailable, PeopleExclusion
        self.capture(T1)
        base = self.full("gen1.sqlite")
        self.graph.put(self.tcm.BOX_A, "MSG-D1", "CK-D1", subject="Documents 1",
                       body="Freight quote for PO100234.")
        self.capture(T2)
        finding = {"now": False}
        real_discover = delta.discover

        def discover(*args, **kwargs):
            finding["now"] = True
            try:
                return real_discover(*args, **kwargs)
            finally:
                finding["now"] = False

        def is_excluded(guard, value):
            if finding["now"]:
                return False
            raise ExclusionUnavailable("excluded_people_unresolved")
        with mock.patch.object(delta, "discover", discover), \
                mock.patch.object(PeopleExclusion, "is_excluded", is_excluded), \
                mock.patch.object(PeopleExclusion, "decides",
                                  lambda guard, value: finding["now"]), \
                mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            report = self.cycle(base)
        self.assertGreaterEqual(report.get("mail_undecided_mailboxes", 0), 1, report)
        self.assertIn(report["layer"], ("added", "compacted"), report)
        self.assertNotIn(self.tcm.BOX_A, json.dumps(report))     # a count, no address
        layer = sqlite3.connect(report["store"])
        self.addCleanup(layer.close)
        self.assertEqual(layer.execute(
            "SELECT count(*) FROM objects WHERE object_id LIKE '%MSG-D1%'").fetchone()[0], 0)
        # never read, so never marked done: it stays open and out of the kept context
        state = delta.State(self.root / "delta" / "state.sqlite")
        self.addCleanup(state.close)
        message = [k for k in state.changed("graph_mail") if "MSG-D1" in k[1]]
        self.assertEqual(len(message), 1, state.changed("graph_mail"))
        self.assertIn(message[0], state.changed("graph_mail", open_only=True))
        self.assertNotIn(message[0], state.context("graph_mail"))
        # the first cycle whose guard decides the mailbox projects it, as a full build does
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            second = self.cycle(base)
        self.assertNotIn("mail_undecided_mailboxes", second)
        self.assertEqual(second["layer"], "added", second)
        self.assertNotIn(message[0], state.changed("graph_mail", open_only=True))
        owned = [o for o in self.owned(second["store"]) if "MSG-D1" in o]
        self.assertTrue(owned, self.owned(second["store"]))
        full = self.full("gen2.sqlite")
        self.assertEqual(delta.compare(Path(second["store"]), full, owned)["differ"], 0)

    def test_a_mailbox_whose_exclusion_lookup_fails_is_left_open_not_fatal(self):
        # the guard decides the mailbox but its exclusion lookup is unavailable after the
        # closure: the same as undecided (left out, kept open), never a failed cycle
        from integrations.people_exclusion import ExclusionUnavailable, PeopleExclusion
        self.capture(T1)
        base = self.full("gen1.sqlite")
        self.graph.put(self.tcm.BOX_A, "MSG-D2", "CK-D2", subject="Documents 2",
                       body="Freight quote for PO100234.")
        self.capture(T2)
        finding = {"now": False}
        real_discover = delta.discover

        def discover(*args, **kwargs):
            finding["now"] = True
            try:
                return real_discover(*args, **kwargs)
            finally:
                finding["now"] = False

        def is_excluded(guard, value):
            if finding["now"]:
                return False
            raise ExclusionUnavailable("excluded_people_unresolved")
        with mock.patch.object(delta, "discover", discover), \
                mock.patch.object(PeopleExclusion, "is_excluded", is_excluded), \
                mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            report = self.cycle(base)
        self.assertGreaterEqual(report.get("mail_undecided_mailboxes", 0), 1, report)
        state = delta.State(self.root / "delta" / "state.sqlite")
        self.addCleanup(state.close)
        self.assertTrue([k for k in state.changed("graph_mail", open_only=True)
                         if "MSG-D2" in k[1]])

    def test_a_message_whose_attachment_is_pending_follows_a_layer_later(self):
        self.capture(T1)
        base = self.full("gen1.sqlite")
        # a new message carrying a quote no build has read
        self.graph.put(self.tcm.BOX_A, "MSG-D1", "CK-D1", subject="Documents 1",
                       body="Freight quote for PO100234.",
                       attachments=[self.tcm.file_attachment("ATT-D1", self.quote,
                                                             "quote.pdf")])
        self.capture(T2)
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            first = self.cycle(base, environ={"PLX_DELTA_DOCUMENT_SECONDS": "1e-9"})
        self.assertFalse(first.get("mail_skipped"), first)
        self.assertEqual(first["documents"]["held_open"], {"graph_mail": 1}, first)
        state = delta.State(self.root / "delta" / "state.sqlite")
        self.addCleanup(state.close)
        queued = state.queued().get("graph_mail", [])
        held = [k for k in queued if "MSG-D1" in k[1]]
        self.assertEqual(len(held), 1, queued)
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            second = self.cycle(base)
        self.assertEqual(second["documents"]["pending"], 0, second)
        self.assertNotIn(held[0], state.changed("graph_mail", open_only=True))
        self.assertNotIn(held[0], state.queued().get("graph_mail", []))
        full = self.full("gen2.sqlite")
        # the second layer holds the quote's own objects the first had no reading for
        first_owned, owned = self.owned(first["store"]), self.owned(second["store"])
        read = set(owned) - set(first_owned)
        self.assertTrue(read and all("ATT-D1" in o for o in read), (first_owned, owned))
        self.assertEqual(delta.compare(Path(second["store"]), full, owned)["differ"], 0)


class HeldQueueTests(unittest.TestCase):
    """The held queue (``State.hold``/``take``): N held records come back a batch at a
    time, the longest queued first, none lost or taken twice, the open ones never more
    than the batch; a change reopens a queued record at once."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = delta.State(Path(tmp.name) / "state.sqlite")
        self.addCleanup(self.state.close)

    def test_n_held_records_drain_in_batches_with_none_lost_or_doubled(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.state.reset("d")
                tuples = [("inbox", f"m{i:03d}") for i in range(n)]
                self.state.add("graph_mail", tuples, {k: "v1" for k in tuples})
                self.state.close_over({}, {"graph_mail": tuples})
                self.state.hold({"graph_mail": {k: {f"f{i}"} for i, k in enumerate(tuples)}},
                                {"graph_mail": tuples})
                self.assertEqual(self.state.open_count(), 0)
                self.assertEqual(self.state.pending_files({}, {}), n)
                seen, cycles, batch = [], 0, 4
                while self.state.queued():
                    report: dict = {}
                    taken = delta.take_held(self.state, {"PLX_DELTA_HELD_BATCH": str(batch)},
                                            report)
                    got = taken.get("graph_mail", [])
                    self.assertLessEqual(self.state.open_count(), batch)
                    self.assertEqual(len(got), min(batch, n - len(seen)))
                    seen.extend(got)
                    self.state.close_over({}, {"graph_mail": got})
                    self.state.hold({}, {"graph_mail": got})   # files read: none held
                    cycles += 1
                self.assertEqual(sorted(seen), sorted(tuples))          # none lost
                self.assertEqual(len(seen), len(set(seen)))              # none twice
                self.assertEqual(cycles, -(-n // batch))
                self.assertEqual(self.state.pending_files({}, {}), 0)
                self.assertEqual(self.state.changed("graph_mail", done_only=True),
                                 sorted(tuples, key=delta._order))

    def test_a_record_held_again_goes_to_the_back_and_a_change_reopens_it(self):
        keys = [("inbox", "a"), ("inbox", "b"), ("inbox", "c")]
        self.state.add("graph_mail", keys, {k: "v1" for k in keys})
        self.state.close_over({}, {"graph_mail": keys})
        self.state.hold({"graph_mail": {k: {"f-" + k[1]} for k in keys}},
                        {"graph_mail": keys})
        time.sleep(0.002)
        first = self.state.take(1)["graph_mail"]
        self.assertEqual(first, [("inbox", "a")])
        self.state.close_over({}, {"graph_mail": first})
        self.state.hold({"graph_mail": {first[0]: {"f-a"}}}, {"graph_mail": first})
        self.assertEqual(self.state.queued()["graph_mail"][-1], ("inbox", "a"))
        # the same version found again leaves it queued; a new version reopens it
        self.state.add("graph_mail", [("inbox", "b")], {("inbox", "b"): "v1"})
        self.assertNotIn(("inbox", "b"), self.state.changed("graph_mail", open_only=True))
        self.state.add("graph_mail", [("inbox", "b")], {("inbox", "b"): "v2"})
        self.assertIn(("inbox", "b"), self.state.changed("graph_mail", open_only=True))
        self.assertNotIn(("inbox", "b"), self.state.queued()["graph_mail"])
        # a record changed again while its cycle ran is not queued: it stays open
        self.state.hold({"graph_mail": {("inbox", "b"): {"f-b"}}}, {"graph_mail": []})
        self.assertIn(("inbox", "b"), self.state.changed("graph_mail", open_only=True))

    def test_the_batch_grows_when_every_file_was_read_and_shrinks_when_most_were_not(self):
        report: dict = {}
        taken = {"graph_mail": [("inbox", str(i)) for i in range(10)]}
        delta.adapt_held_batch(self.state, {}, taken, {}, report)
        self.assertEqual(self.state.get("held_batch"), 2 * delta.HELD_BATCH)
        held = {"graph_mail": {("inbox", str(i)): {"f"} for i in range(8)}}
        delta.adapt_held_batch(self.state, {}, taken, held, report)
        self.assertEqual(self.state.get("held_batch"), delta.HELD_BATCH)
        for _ in range(20):
            delta.adapt_held_batch(self.state, {}, taken, held, report)
        self.assertEqual(self.state.get("held_batch"), delta.HELD_BATCH_MIN)
        # a fixed batch (or one that is no number: adaptive) is honoured
        self.assertEqual(delta.held_batch(self.state, {"PLX_DELTA_HELD_BATCH": "7"}), (7, True))
        self.assertEqual(delta.held_batch(self.state, {"PLX_DELTA_HELD_BATCH": "x"}),
                         (delta.HELD_BATCH_MIN, False))


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
                columns = ("(source, key, seen_at, closed)" if table == "changed" else "")
                old.conn.executemany(f"INSERT INTO {table} {columns} VALUES ({marks})", rows)
            old.conn.close()
            state = delta.State(path)
            records = state.context("bc") | set(state.changed("bc"))
            self.assertEqual(sorted(records), [("bc", "production/ca41/items/1"),
                                               ("bc", bc)])
            self.assertEqual(state.changed("bc", open_only=True),
                             [("bc", "production/ca41/items/1")])
            self.assertEqual(state.owned_records(["SalesOrder:bc.sales_orders:x"]),
                             {"SalesOrder:bc.sales_orders:x": ("bc", ("bc", bc))})


    def test_a_state_without_input_marks_reopens_each_record_once_then_holds_it(self):
        """A state written before input marks: opened, it gains them; a record found again
        under the mark it was last taken at stays closed, a new mark reopens it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE changed (source TEXT NOT NULL, key TEXT NOT NULL, "
                             "seen_at TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0, "
                             "PRIMARY KEY (source, key))")
                conn.execute("INSERT INTO changed VALUES ('teams', ?, 't', 1)",
                             (json.dumps(["teams_chat", "c/1"]),))
            conn.close()
            state = delta.State(path)
            self.addCleanup(state.conn.close)
            key = ("teams_chat", "c/1")
            self.assertEqual(state.add("teams", [key], {key: "m1"}), 1)   # unmarked: once
            state.close_over({}, {"teams": [key]})
            self.assertEqual(state.add("teams", [key], {key: "m1"}), 0)
            self.assertEqual(state.changed("teams", open_only=True), [])
            self.assertEqual(state.add("teams", [key], {key: "m2"}), 1)
            self.assertEqual(state.changed("teams", open_only=True), [key])
            state.close_over({}, {"teams": [key]})
            self.assertEqual(state.add("teams", [key]), 1)   # no mark: always reopened


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

    def test_a_person_citation_is_no_mail_record(self):
        # the closure met graph-mail://people/person/<key> (sender parties) and took
        # "people" for a mailbox the exclusion guard cannot decide: the whole cycle failed
        from ontology import delta
        from ontology.people_directory import person_handle
        for handle in (person_handle(None, "a" * 64) + "@ck",
                       "graph-mail://people/person/" + "b" * 64,
                       "graph-mail://0211d86b-8ede-4c1f-9a5e-0d1f2a3b4c5d/person/"
                       "0211d86b-8ede-4c1f-9a5e-0d1f2a3b4c5d@ck"):
            with self.subTest(handle=handle[:40]):
                self.assertIsNone(delta.record_of_handle(handle))
        self.assertEqual(delta.record_of_handle("graph-mail://0211d86b/AAkALgAA@CQ"),
                         ("graph_mail", ("0211d86b", "AAkALgAA")))


if __name__ == "__main__":
    unittest.main()


class WherePathsTest(__import__("unittest").TestCase):
    def test_a_bindings_filter_columns_are_read_paths(self):
        from ontology import delta
        where = [{"path": "categorymo", "op": "eq", "value": "AO"},
                 {"any": [{"path": "status", "op": "eq", "value": "x"}]}]
        self.assertEqual(set(delta._where_paths(where)), {"categorymo", "status"})

    def test_the_live_schema_reads_production_category_for_its_bindings(self):
        from ontology import delta
        from ontology.schema import load_schema
        schema = load_schema()
        hits = [(tid, b) for tid, t in schema.object_types.items()
                for b in (t.get("bindings") or ())
                if any(p == "categorymo" for p in delta._where_paths(b.get("where")))]
        if not hits:
            self.skipTest("no binding filters on categorymo in this schema")
        for _tid, b in hits:
            self.assertIn("categorymo", set(delta._where_paths(b.get("where"))))


class CycleTempTests(unittest.TestCase):
    def test_a_cycle_without_a_temp_place_spills_beside_its_delta_not_to_the_root_disk(self):
        """11-11: a cycle on a unit without TMPDIR sorted to /tmp on the small root disk and
        failed "database or disk is full": its temp files go beside the delta then; a
        place the unit names is kept."""
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            env = {}
            got = delta._temp_beside(Path(tmp), env)
            self.assertEqual(got, Path(tmp) / "tmp")
            self.assertEqual((env["SQLITE_TMPDIR"], env["TMPDIR"]), (str(got), str(got)))
            self.assertTrue(got.is_dir())
            named = {"SQLITE_TMPDIR": "/data/x", "TMPDIR": "/data/x"}
            self.assertEqual(delta._temp_beside(Path(tmp), named), Path("/data/x"))
            self.assertEqual(named, {"SQLITE_TMPDIR": "/data/x", "TMPDIR": "/data/x"})


class DeltaCaptureStoreAbsenceTests(DeltaCase):
    """A capture-store record a complete run no longer finds (DELTA-001: the absence is a
    mark on its version, no new version, no removal row) is projected again as missing,
    as a full build of the same captures holds it; one marked after the generation read
    the store but before its build time is found too (DELTA-002)."""

    def setUp(self):
        super().setUp()
        from integrations import mc_sources
        from integrations.tests import test_mc_sources as tms
        self.tms, self.mc = tms, tms.FakeMc()
        self.mc_path = mc_sources.store_path(self.state)
        self.mc_path.parent.mkdir(parents=True)
        self.at = T1

    def mc_clock(self):
        self.at += timedelta(microseconds=1)
        return self.at

    def capture(self, at):
        from integrations.capture_store import CaptureStore
        from integrations.mc_sources import CaptureSpec, McSourcesCapture
        super().capture(at)
        self.at = at
        with CaptureStore(self.mc_path, self.tms.audience(), clock=self.mc_clock) as store:
            McSourcesCapture(store, self.tms.mc_client(self.mc)).capture(
                CaptureSpec("ops", ()))

    def full(self, name):
        from ontology.project import capture_source
        with MirrorCaptures(self.mirror_path) as captures, \
                capture_source("mission_control", self.mc_path) as mc:
            build_store(self.root / name, self.schema,
                        {"portal_api": captures, "mission_control": mc},
                        normalizer=normalizer, clock=lambda: T2,
                        post_project=build_steps.post_project(
                            self.state, "plx", scratch=self.root / "steps"))
        return self.root / name

    def task(self, store, task_id):
        with sqlite3.connect(store) as conn:
            return conn.execute(
                "SELECT object_id, missing_from_source_since FROM objects WHERE object_type "
                "= 'MCTask' AND json_extract(native_key, '$.id') = ?", (task_id,)).fetchone()

    def test_a_record_gone_from_the_source_is_projected_missing_by_the_next_layer(self):
        from ontology.schema import load_schema
        self.schema = load_schema()
        self.capture(T1)
        base = self.full("gen1.sqlite")          # built at T2, read the store at T1
        oid, missing = self.task(base, "TASK-3")
        self.assertIsNone(missing)
        # gone after the generation read the store, before its build time (DELTA-002)
        del self.mc.tasks["TASK-3"]
        self.capture(T2 - timedelta(minutes=30))
        first = self.cycle(base)
        self.assertIn(oid, self.owned(first["store"]), first)
        full = self.full("gen2.sqlite")
        self.assertIsNotNone(self.task(full, "TASK-3")[1])
        self.assertEqual(self.task(first["store"], "TASK-3"), self.task(full, "TASK-3"))
        self.assertEqual(delta.compare(Path(first["store"]), full, [oid])["differ"], 0)
        # nothing new: the same absence opens nothing again
        self.assertEqual(self.cycle(base)["layer"], "none: nothing new")
        # gone after the build time (DELTA-001): the next layer serves it missing
        oid2 = self.task(base, "TASK-1")[0]
        del self.mc.tasks["TASK-1"]
        self.capture(T2 + timedelta(hours=2))
        second = self.cycle(base)
        self.assertEqual(second["layer"], "added", second)
        self.assertIn(oid2, self.owned(second["store"]))
        newer = self.full("gen3.sqlite")
        self.assertIsNotNone(self.task(newer, "TASK-1")[1])
        self.assertEqual(self.task(second["store"], "TASK-1"), self.task(newer, "TASK-1"))
        self.assertEqual(delta.compare(Path(second["store"]), newer, [oid2])["differ"], 0)



class LayerReleaseTests(unittest.TestCase):
    """Over a Postgres base a big (or old) layer Postgres applied leaves the pointer before
    ``PG_KEPT_LAYERS`` newer ones exist, once every reader following the layers by name
    has taken it in; an unreadable reader state holds it, and a file a process still holds
    open is not removed (11-12: the 37.9 GB catch-up layer was kept for 47 cycles)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.dir = self.state / "projection" / "delta"
        self.dir.mkdir(parents=True)
        self.now = time.time()
        # newest first, as the pointer lists them: one small and one big layer, applied
        self.small = self.layer("delta-20261112T050000000000.sqlite", 10)
        self.big = self.layer("delta-20261111T234712486965.sqlite", 5000)
        self.unapplied = self.layer("delta-20261111T200000000000.sqlite", 5000)
        self.pg = sqlite3.connect(":memory:")
        self.addCleanup(self.pg.close)
        self.pg.execute("CREATE TABLE load_state (name TEXT)")
        self.pg.executemany("INSERT INTO load_state VALUES (?)",
                            [("delta:" + p.name,) for p in (self.small, self.big)])
        self.env = {"PLX_DELTA_RELEASE_BYTES": "1000"}

    def layer(self, name, size):
        path = self.dir / name
        path.write_bytes(b"x" * size)
        return path

    def kept(self, env=None, keep=47):
        out = {}
        layers = [{"store": str(p)} for p in (self.small, self.big, self.unapplied)]
        rule = delta._release_rule(self.state, self.env if env is None else env, out)
        left = delta._not_in_postgres(layers, self.pg, keep=keep, release=rule)
        return [Path(item["store"]).name for item in left], out

    def search(self, layers, partial=None, hours_ago=0.1, raw=None):
        path = self.state / "search" / "follow.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                              time.gmtime(self.now - hours_ago * 3600))
        body = {"digest": "sha256:a", "layers": [str(p) for p in layers], "updated_at": stamp}
        if partial is not None:
            body["partial"] = {"layer": str(partial), "done": 1}
        path.write_text(raw if raw is not None else json.dumps(body))

    def proactive(self, layers, hours_ago=0.1, reader="postgres"):
        from operating import proactive_engine, proactive_incremental as pi
        inbox = proactive_engine.inbox_path(delta.served_store(self.state))
        inbox.write_text("")
        with __import__("contextlib").closing(pi._open(pi.changes_path(inbox))) as ch:
            ch.execute("DELETE FROM layers")
            ch.execute("DELETE FROM passes")
            pi._set_meta(ch, "reader", reader)
            ch.executemany("INSERT INTO layers VALUES (?, ?, ?)",
                           [(str(p), "sha256:a", "t") for p in layers])
            ch.execute("INSERT INTO passes (at, summary) VALUES (?, '{}')", (time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self.now - hours_ago * 3600)),))

    def watcher(self, layers, switch="on", hours_ago=0.1):
        from operating import ai_watcher
        ai_watcher.set_switch(self.state, switch)
        with __import__("contextlib").closing(
                ai_watcher.open_store(ai_watcher.watch_dir(self.state))) as ws:
            ws.executemany("INSERT OR REPLACE INTO layers VALUES (?, ?, ?)",
                           [(str(p), "sha256:a", "t") for p in layers])
            ws.execute("INSERT INTO runs (run_at, status, model_run, summary) VALUES "
                       "(?, 'ok', 0, '{}')", (time.strftime(
                           "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - hours_ago * 3600)),))

    def every_reader_took(self, layers):
        self.search(layers)
        self.proactive(layers)
        self.watcher(layers)

    def test_the_readers_state_is_read_where_each_reader_keeps_it(self):
        from operating import ai_watcher, proactive_engine, proactive_incremental as pi
        from operating.embed import follow
        store = delta.served_store(self.state)
        self.assertEqual(pi.changes_path(proactive_engine.inbox_path(store)),
                         store.with_name(store.name + delta.PROACTIVE_CHANGES_SUFFIX))
        self.assertEqual(Path(*delta.SEARCH_CURSOR), follow.CURSOR_NAME)
        self.assertEqual(ai_watcher.watch_dir(self.state), self.state / delta.WATCHER_DIR)

    def test_a_big_applied_layer_every_reader_took_in_leaves_the_pointer(self):
        self.every_reader_took([self.small, self.big, self.unapplied])
        names, out = self.kept()
        # the small applied one stays (count rule), the unapplied big one stays (Postgres
        # does not hold it yet)
        self.assertEqual(names, [self.small.name, self.unapplied.name])
        self.assertEqual(out, {"layers": [self.big.name]})

    def test_a_reader_that_has_not_taken_it_in_holds_it(self):
        self.every_reader_took([self.small, self.big])
        # stopped inside the big layer (named in its list too: the slice not done holds)
        self.search([self.small, self.big], partial=self.big)
        names, out = self.kept()
        self.assertIn(self.big.name, names)
        self.assertEqual(out, {"held": {self.big.name: ["search_follow"]}})
        self.search([self.small, self.big])
        self.proactive([self.small])
        self.assertEqual(self.kept()[1], {"held": {self.big.name: ["proactive_incremental"]}})
        self.proactive([self.small, self.big])
        self.watcher([])   # its runs and layers kept; this one never saw the big layer
        with sqlite3.connect(self.state / "ai-watcher" / "watcher.sqlite") as ws:
            ws.execute("DELETE FROM layers WHERE store = ?", (str(self.big),))
        self.assertEqual(self.kept()[1], {"held": {self.big.name: ["ai_watcher"]}})

    def test_a_stopped_or_switched_off_reader_holds_nothing(self):
        self.every_reader_took([self.small])
        self.search([self.small, self.big])
        self.proactive([self.small], hours_ago=delta.READER_IDLE_HOURS + 1)   # stopped
        self.watcher([], switch="off")
        names, out = self.kept()
        self.assertNotIn(self.big.name, names)
        self.assertEqual(out, {"layers": [self.big.name]})
        # the incremental pass reading the generation alone takes no layer in
        self.proactive([self.small], reader="generation_only")
        self.assertNotIn(self.big.name, self.kept()[0])

    def test_unreadable_or_missing_reader_state_holds_the_layer(self):
        self.every_reader_took([self.small, self.big])
        for raw in ("{not json", "[]", json.dumps({"layers": "x"})):
            self.search([], raw=raw)
            names, out = self.kept()
            self.assertIn(self.big.name, names, raw)
            self.assertEqual(out["held"][self.big.name], ["search_follow"], raw)
        (self.state / "search" / "follow.json").unlink()   # no cursor: search holds all
        self.assertIn(self.big.name, self.kept()[0])
        self.search([self.small, self.big])
        changes = delta.served_store(self.state).with_name(
            "ontology.sqlite" + delta.PROACTIVE_CHANGES_SUFFIX)
        changes.write_bytes(b"not a database at all, longer than a header" * 4)
        for side in ("-wal", "-shm"):
            Path(str(changes) + side).unlink(missing_ok=True)
        self.assertEqual(self.kept()[1]["held"][self.big.name], ["proactive_incremental"])
        changes.unlink()   # an inbox, no change file yet: its first pass takes all in
        self.assertEqual(self.kept()[1]["held"][self.big.name], ["proactive_incremental"])
        self.proactive([self.small, self.big])
        (self.state / "ai-watcher" / "watcher.sqlite").unlink()   # on, not run yet
        for side in ("-wal", "-shm"):
            (self.state / "ai-watcher" / f"watcher.sqlite{side}").unlink(missing_ok=True)
        self.assertEqual(self.kept()[1]["held"][self.big.name], ["ai_watcher"])
        self.watcher([self.small, self.big])
        self.assertNotIn(self.big.name, self.kept()[0])
        broken = (("search_follow", mock.Mock(side_effect=RuntimeError("x"))),
                  *delta.LAYER_READERS[1:])
        with mock.patch.object(delta, "LAYER_READERS", broken):   # a reader failing
            self.assertEqual(self.kept()[1]["held"][self.big.name], ["search_follow"])

    def test_release_off_or_not_a_number_keeps_layers_by_count(self):
        self.every_reader_took([self.small, self.big, self.unapplied])
        for env in ({"PLX_DELTA_RELEASE_BYTES": "0", "PLX_DELTA_RELEASE_DAYS": "0"},
                    {"PLX_DELTA_RELEASE_BYTES": "big", "PLX_DELTA_RELEASE_DAYS": "nan"}):
            names, out = self.kept(env)
            self.assertEqual(names, [self.small.name, self.big.name, self.unapplied.name])
            self.assertEqual(out, {})
        # past the count every applied layer goes, held or not (as before)
        self.search([])
        self.assertEqual(self.kept(keep=0)[0], [self.unapplied.name])

    def test_an_old_small_layer_leaves_by_age(self):
        self.every_reader_took([self.small, self.big])
        old = self.now - (delta.RELEASE_DAYS + 0.5) * 86400
        os.utime(self.small, (old, old))
        names, out = self.kept({"PLX_DELTA_RELEASE_BYTES": "0"})
        self.assertEqual(names, [self.big.name, self.unapplied.name])
        self.assertEqual(out, {"layers": [self.small.name]})

    def test_prune_leaves_a_layer_a_process_holds_open_to_the_next_prune(self):
        old = self.now - 3600
        for path in (self.small, self.big, self.unapplied):
            os.utime(path, (old, old))
        if delta.open_files() is None:
            self.skipTest("no /proc: prune removes by age alone")
        with open(self.big, "rb"):
            out = delta.prune(self.dir, keep={self.small.resolve()})
            self.assertTrue(self.big.exists())
        self.assertEqual(out["held_open"], [self.big.name])
        self.assertEqual(out["removed"], [self.unapplied.name])
        again = delta.prune(self.dir, keep={self.small.resolve()})
        self.assertEqual((again["removed"], again["held_open"]), ([self.big.name], []))
        self.assertTrue(self.small.exists())
