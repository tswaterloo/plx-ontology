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
        self.addCleanup(state.conn.close)
        self.assertIn(record, state.changed("sharepoint", open_only=True))
        # nothing was kept for the file: it is read, not served as unreadable, next time
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            second = self.cycle(base)
        self.assertEqual(second["documents"]["pending"], 0, second)
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
        self.addCleanup(state.conn.close)
        held = [k for k in state.changed("graph_mail", open_only=True) if "MSG-D1" in k[1]]
        self.assertEqual(len(held), 1, state.changed("graph_mail", open_only=True))
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}):
            second = self.cycle(base)
        self.assertEqual(second["documents"]["pending"], 0, second)
        self.assertNotIn(held[0], state.changed("graph_mail", open_only=True))
        full = self.full("gen2.sqlite")
        # the second layer holds the quote's own objects the first had no reading for
        first_owned, owned = self.owned(first["store"]), self.owned(second["store"])
        read = set(owned) - set(first_owned)
        self.assertTrue(read and all("ATT-D1" in o for o in read), (first_owned, owned))
        self.assertEqual(delta.compare(Path(second["store"]), full, owned)["differ"], 0)


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
