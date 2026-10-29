"""Source deletions (ING-09): a soft delete the source asserts, and removal intervals kept.

A Portal relation whose catalog carries the declared soft-delete column (``deleted_at``,
``integrations/source_mirror_ingest.json`` ``soft_delete_rules``) labels a row with a value
there ``deleted_in_source`` (basis ``source_soft_delete``), hides it from ``rows`` and
``resolve`` like a missing row and keeps it readable through its citation with the label.
The deletion feeds the one removal path of the ontology: reviews citing the record are
withdrawn and their links end at the deletion time; ``as_known_at`` before it reads them.
A record missing from its source that returns keeps its past removal interval (in the
mirror and in the BC capture store), so ``as_known_at`` inside it reads the link ended and
after the return reads it again. A full capture that finds a child row replaced by a
fresh id (same parent and natural key, never the parent's foreign keys alone) labels it
``replaced_in_source`` with its successor, or ``ambiguous_replacement`` with every candidate; plx.review reads a
withdrawn review through to the successor. Fake Workbench transport, static mail, temp
files; all identifiers are synthetic. Evidence: isolated-fixture.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from integrations import mirror_capture
from integrations.capture_store import (REMOVALS_COLUMN, CaptureStore, CapturedRecord, bc_unit,
                                        removals_of)
from integrations.live_source_guard import LiveSourceAudience
from integrations.http import SourceHttpError
from integrations.source_mirror import (PRISMA_FACTS_PATH, SourceMirror, replacement_key,
                                        replacement_models)
from integrations.tests.test_source_mirror import (ALL, BASE, KEY, OPS, OUTSIDER, Clock,
                                                   FakeWorkbench)
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import identifiers
from ontology import schema as schema_loader
from ontology.project import MirrorCaptures, ProjectionError, build_store
from ontology.json_contract import validate
from ontology.project_bc import BcCaptures
from ontology import reviews
from operating import ontology_tools_review as rt
from tests import test_ontology_tools_review as tr

CUSTOMERS = "public.app_customer"
NOTES = "public.app_note"          # same family, no soft-delete column in its catalog
LEDGER = "sage.ledger"             # carries deleted_at, outside the rule's relations
LINK = "Email.mentions_customer"
A = tr.employee_session(tr.OID_A)    # orders desk: Portal and the mailbox


def customer(key, deleted_at=None, updated_at="2026-09-29T10:00:00"):
    return {"id": key, "code": f"K-{key}", "deleted_at": deleted_at, "updated_at": updated_at}


def workbench(rows):
    fake = FakeWorkbench()
    fake.catalog = {
        CUSTOMERS: {"columns": ["id", "code", "deleted_at", "updated_at"], "key": ["id"],
                    "domain": "customers",
                    "since": {"column": "updated_at", "mode": "timestamp-utc"}},
        NOTES: {"columns": ["id", "code"], "key": ["id"], "domain": "customers",
                "since": None},
        LEDGER: {"columns": ["id", "code", "deleted_at"], "key": ["id"], "domain": "finance",
                 "since": None}}
    fake.tables = {CUSTOMERS: rows, NOTES: [{"id": "n1", "code": "K-n1"}],
                   LEDGER: [{"id": "l1", "code": "K-l1", "deleted_at": "2026-09-01T00:00:00"}]}
    return fake


def open_mirror(test, path, fake, clock, **options):
    mirror = SourceMirror(path, WorkbenchReadApiClient(BASE, KEY, fetch=fake), ALL, clock=clock,
                          retry_delay_seconds=0,
                          identifiers={CUSTOMERS: [["code", "customer_code",
                                                    "system_document_number"]]},
                          **options)
    test.addCleanup(mirror.close)
    return mirror


class MirrorSoftDeleteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_a_soft_deleted_row_is_labelled_hidden_and_citable_at_1_10_60(self):
        for count in (1, 10, 60):
            with self.subTest(rows=count):
                # Even ids are soft-deleted; id 1 carries a value that is no instant.
                rows = [customer(f"c{i:02d}", deleted_at=("2026-09-28T08:00:00" if i % 2 == 0
                                                          else "not-a-time" if i == 1
                                                          else None))
                        for i in range(count)]
                fake, clock = workbench(rows), Clock()
                # A narrower projection never drops the deletion marker.
                mirror = open_mirror(self, self.root / f"m{count}.sqlite", fake, clock,
                                     projections={CUSTOMERS: ["code"]})
                started = time.monotonic()
                mirror.sync(OPS)
                mirror.refresh(OPS, CUSTOMERS, full=True)   # a re-read adds nothing
                self.assertLess(time.monotonic() - started, 10)
                deleted = {f"c{i:02d}" for i in range(count) if i % 2 == 0 or i == 1}
                shown = {r["key"]["id"] for r in mirror.rows(OPS, CUSTOMERS, limit=500)["rows"]}
                self.assertEqual(shown, {r["id"] for r in rows} - deleted)
                every = {r["key"]["id"]: r for r in mirror.rows(
                    OPS, CUSTOMERS, limit=500, include_missing=True)["rows"]}
                self.assertEqual(len(every), count)
                self.assertEqual((every["c00"]["deleted_in_source"], every["c00"]["deleted_basis"]),
                                 ("2026-09-28T08:00:00.000000Z", "source_soft_delete"))
                if count > 1:
                    # Not an instant: still deleted (fail closed), at the capture's time.
                    self.assertEqual((every["c01"]["deleted_in_source"],
                                      every["c01"]["deleted_basis"]),
                                     ("2026-09-29T12:00:00.000000Z",
                                      "source_soft_delete_observed"))
                if count > 3:
                    self.assertNotIn("deleted_in_source", every["c03"])
                cited = mirror.resolve_citation(every["c00"]["citation"], OPS)
                self.assertTrue(cited["ok"])
                self.assertEqual((cited["row"]["code"], cited["deleted_in_source"]),
                                 ("K-c00", "2026-09-28T08:00:00.000000Z"))
                self.assertFalse(cited["deletions_proven"])
                self.assertEqual(mirror.resolve(OPS, "K-c00")["candidate_count"], 0)
                self.assertEqual(mirror.resolve_citation(every["c00"]["citation"],
                                                         OUTSIDER)["error"], "not_visible")
                coverage = mirror.coverage(OPS)
                self.assertEqual(coverage["rows_deleted_in_source_total"], len(deleted))
                self.assertEqual(mirror.coverage(OUTSIDER)["relations"], [])
                open_intervals = mirror._db.execute(
                    "SELECT COUNT(*) FROM row_removal WHERE relation = ? AND until IS NULL",
                    (CUSTOMERS,)).fetchone()[0]
                self.assertEqual(open_intervals, len(deleted))
        # The column absent, or a relation outside the rule: data only, nothing hidden.
        self.assertEqual(len(mirror.rows(OPS, NOTES)["rows"]), 1)
        ledger = mirror.rows(OPS, LEDGER)["rows"]
        self.assertEqual([r["row"]["deleted_at"] for r in ledger], ["2026-09-01T00:00:00"])
        self.assertNotIn("deleted_in_source", ledger[0])

    def test_a_mirror_from_before_soft_deletes_is_labelled_and_keeps_open_absences(self):
        fake, clock = workbench([customer("c1", deleted_at="2026-09-28T08:00:00"),
                                 customer("c2")]), Clock()
        path = self.root / "old.sqlite"
        mirror = open_mirror(self, path, fake, clock)
        mirror.rows(OPS, CUSTOMERS)
        c2 = fake.tables[CUSTOMERS].pop()
        clock.advance(3600)
        mirror.refresh(OPS, CUSTOMERS, full=True)                  # c2 missing at 13:00
        mirror.close()
        with contextlib.closing(sqlite3.connect(str(path))) as db:   # the file as it was
            db.executescript("DROP TABLE row_removal; DROP TABLE relation_soft_delete; "
                             "ALTER TABLE row_current DROP COLUMN deleted_in_source; "
                             "ALTER TABLE row_current DROP COLUMN deleted_basis;")
        mirror = open_mirror(self, path, fake, clock)
        self.assertEqual(mirror.rows(OPS, CUSTOMERS)["rows"], [])
        fake.tables[CUSTOMERS].append(c2)
        clock.advance(3600)
        mirror.refresh(OPS, CUSTOMERS, full=True)                  # c2 back at 14:00
        self.assertEqual([r["key"]["id"] for r in mirror.rows(OPS, CUSTOMERS)["rows"]], ["c2"])
        self.assertEqual([tuple(row) for row in mirror._db.execute(
            "SELECT key_json, basis, since, until FROM row_removal ORDER BY key_json")],
            [('{"id":"c1"}', "source_soft_delete", "2026-09-28T08:00:00.000000Z", None),
             ('{"id":"c2"}', "missing_from_source", "2026-09-29T13:00:00.000000Z",
              "2026-09-29T14:00:00.000000Z")])

    def test_the_live_check_records_soft_deletes_as_counts_only(self):
        """The mirror.full_coverage command (scripts.live_read_probes) over the fake
        transport, read through the spec exactly as the verifier records it."""
        from devloop import live_verify as lv
        from scripts import live_read_probes as probes
        fake = workbench([customer("c1"), customer("c2", deleted_at="2026-09-28T08:00:00")])
        fake.tables[CUSTOMERS][1]["code"] = "SECRET-VALUE"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = probes.main(["mirror-full-coverage", "--db", str(self.root / "probe.sqlite"),
                                "--resolve-sample", "10"], environ={},
                               clients={"mirror-full-coverage":
                                        WorkbenchReadApiClient(BASE, KEY, fetch=fake)})
        self.assertEqual(code, 0)
        self.assertNotIn("SECRET-VALUE", out.getvalue())
        report = json.loads(out.getvalue())
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.full_coverage.json")
                          .read_text())
        recorded = lv._recorded(spec, report)
        self.assertEqual((recorded["rows_deleted_in_source_total"],
                          recorded["soft_delete_relation_count"],
                          recorded["soft_deleted_rows"]), (1, 1, {CUSTOMERS: 1}))
        failures = lv.check_expectations(spec, code, report)
        self.assertFalse([f for f in failures if "deleted" in f or "soft_delete" in f], failures)


class RemovalPathTests(unittest.TestCase):
    """The mirror's removals reach plx.review, its links and as_known_at."""

    def test_soft_delete_withdraws_and_a_returned_record_keeps_its_removal_interval(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        fake, clock = workbench([customer("c1"), customer("c2")]), Clock()
        mirror = open_mirror(self, root / "mirror.sqlite", fake, clock)
        mirror.rows(OPS, CUSTOMERS)                                  # 09-29 12:00
        reviewed_at = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
        store = reviews.ReviewStore(root / "reviews.sqlite", clock=lambda: reviewed_at)
        ids = {key: f'Customer:portal.app_customer:{{"id":"{key}"}}' for key in ("c1", "c2")}

        def build(path):
            build_store(path, schema_loader.load_schema(),
                        {"portal_api": MirrorCaptures(root / "mirror.sqlite"),
                         "graph_mail": tr.Static("graph_mail", tr.FIRST)},
                        normalizer=identifiers.normalize, clock=lambda: clock.now,
                        post_project={"reviews": reviews.link_step(store, "plx")})

        def tools(path, now):
            base = tr.ontology(path, store, now)
            self.addCleanup(base.store.conn.close)
            return base, rt.ReviewTools(base, store, session=A, gate=tr.identity_reviewer)

        build(root / "first.sqlite")
        base, review = tools(root / "first.sqlite", reviewed_at)
        for key, oid in ids.items():
            cite = next(v["citation"] for v in base.call(
                "plx.object", {"object_id": oid}, A.actor)["values"] if v["property"] == "code")
            recorded = review.call("plx.review", {
                "mode": "record", "decision": "accept_link", "subject": tr.EMAIL,
                "target": oid, "link_type": LINK,
                "evidence": [{"l1_handle": cite["l1_handle"], "digest": cite["digest"],
                              "field_path": "row.code"}]}, A.actor)
            self.assertEqual(recorded["review"]["state"], "effective", recorded)

        # c1 is soft-deleted at 10-01 00:00 and read at 06:00; c2 is missing from a full
        # traversal on 10-02 and returns unchanged on 10-03.
        fake.update(CUSTOMERS, "c1", deleted_at="2026-10-01T00:00:00",
                    updated_at="2026-10-01T00:00:00")
        clock.now = dt.datetime(2026, 10, 1, 6, tzinfo=dt.timezone.utc)
        mirror.refresh(OPS, CUSTOMERS, full=True)
        c2 = fake.tables[CUSTOMERS].pop(1)
        clock.now = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)
        mirror.refresh(OPS, CUSTOMERS, full=True)
        fake.tables[CUSTOMERS].append(c2)
        clock.now = dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc)
        mirror.refresh(OPS, CUSTOMERS, full=True)
        self.assertEqual([r["key"]["id"] for r in mirror.rows(OPS, CUSTOMERS)["rows"]], ["c2"])

        clock.now = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)
        build(root / "later.sqlite")
        base, review = tools(root / "later.sqlite", clock.now)
        status = review.call("plx.review", {"mode": "status", "subject": tr.EMAIL}, A.actor)
        self.assertEqual(sorted((r["target"], r["state"], r.get("state_reason"))
                                for r in status["reviews"]),
                         [(ids["c1"], "withdrawn", "source_removed"),
                          (ids["c2"], "effective", None)])

        def reviewed(key, at):
            answer = base.call("plx.object", {"object_id": ids[key], "as_known_at": at},
                               A.actor)
            return {c["link_type"]: c.get("tiers") for c in answer["linked_counts"]}.get(LINK)

        for key, at, tiers in (
                ("c1", "2026-09-30T12:00:00Z", {"reviewed": 1}),
                ("c1", "2026-10-01T03:00:00Z", None),     # deleted at its value, read later
                ("c1", "2026-10-03T12:00:00Z", None),
                ("c2", "2026-09-30T12:00:00Z", {"reviewed": 1}),
                ("c2", "2026-10-02T12:00:00Z", None),     # inside the past removal
                ("c2", "2026-10-03T12:00:00Z", {"reviewed": 1})):
            self.assertEqual(reviewed(key, at), tiers, (key, at))


class CaptureStoreRemovalTests(unittest.TestCase):
    def test_a_record_seen_again_keeps_its_missing_interval_for_the_projection(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "bc.sqlite"
        company = "0f0e0d0c-0b0a-0908-0706-050403020100"
        now = [dt.datetime(2026, 10, 1, 8, tzinfo=dt.timezone.utc)]
        audience = LiveSourceAudience({"bc:production:*": ["svc"]}, tenant="plx",
                                      default_classification="restricted")
        capture = CaptureStore(path, audience, clock=lambda: now[0])
        self.addCleanup(capture.close)
        unit = bc_unit(company, "items")
        rec = "aaaaaaaa-0000-0000-0000-000000000001"
        item = CapturedRecord(path=("Production", company.upper(), "items", rec),
                              payload={"row": {"id": rec}, "meta": {}},
                              source_version='W/"1"', lineage="bc_manufacturing")
        for hour, records in ((8, [item]), (9, []), (11, [item])):
            now[0] = now[0].replace(hour=hour)
            run = capture.begin_run("bc", unit, [f"bc:production:{company}:items"])
            capture.save_page(run["run_id"], records, None)
            capture.finish_run(run["run_id"], "complete")
        [row] = capture._db.execute(f"SELECT *, {REMOVALS_COLUMN} FROM versions").fetchall()
        self.assertIsNone(row["missing_from_source_since"])
        self.assertEqual(removals_of(row), (("2026-10-01T09:00:00.000000Z",
                                             "2026-10-01T11:00:00.000000Z",
                                             "missing_from_source"),))
        with BcCaptures(path) as source:
            [record] = list(source.records(["items"])["items"])
        self.assertEqual(record.removals, removals_of(row))


LINES = "public.app_purchase_order_line"
RECEIPT_LINES = "public.app_receipt_line"
SINCE = {"column": "updated_at", "mode": "timestamp-utc"}
# Generated-facts shape with a parent-scoped unique line number (the served Portal model
# has none: the replacement tests below that use the committed facts pair by product).
NUMBERED = {"models": {"PurchaseOrderLine": {
    "table": "app_purchase_order_line", "id": ["id"],
    "unique": [["purchase_order_id", "line_number"]],
    "relations": [{"columns": ["purchase_order_id"], "on_delete": "Cascade"},
                  {"columns": ["product_id"], "on_delete": None}]}}}


def line(key, po, number=None, product="prod-1", updated_at="2026-09-29T10:00:00"):
    return {"id": key, "purchase_order_id": po, "line_number": number, "product_id": product,
            "quantity": "5", "updated_at": updated_at}


def lines_workbench(rows):
    fake = FakeWorkbench()
    fake.catalog = {LINES: {"columns": ["id", "purchase_order_id", "line_number",
                                        "product_id", "quantity", "updated_at"],
                            "key": ["id"], "domain": "purchasing", "since": SINCE}}
    fake.tables = {LINES: rows}
    return fake


def labels(mirror):
    """key -> the replacement and absence labels of every stored row."""
    names = ("missing_from_source_since", "replaced_in_source", "replaced_basis",
             "successor", "ambiguous_replacement", "replacement_candidates")
    return {r["key"]["id"]: {n: r[n] for n in names if n in r}
            for r in mirror.rows(OPS, LINES, limit=500, include_missing=True)["rows"]}


class MirrorReplacementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_a_replaced_line_names_its_successor_at_1_10_60(self):
        for count in (1, 10, 60):
            with self.subTest(orders=count):
                # Each PO has lines 1 and 2; one more PO's only line is removed outright.
                rows = [line(f"l{i:02d}-{n}", f"po{i:02d}", n) for i in range(count)
                        for n in (1, 2)] + [line("gone", "po-x", 1)]
                fake, clock = lines_workbench(rows), Clock()
                mirror = open_mirror(self, self.root / f"m{count}.sqlite", fake, clock,
                                     prisma_facts=NUMBERED, projections={LINES: ["quantity"]})
                mirror.refresh(OPS, LINES, full=True)
                # Line 1 of every PO comes back under a fresh id; "gone" has no successor.
                fake.tables[LINES] = [r for r in rows if r["id"] != "gone" and
                                      not r["id"].endswith("-1")] + [
                    line(f"z{i:02d}", f"po{i:02d}", 1) for i in range(count)]
                clock.advance(3600)
                started = time.monotonic()
                mirror.refresh(OPS, LINES, full=True)
                self.assertLess(time.monotonic() - started, 10)
                seen = labels(mirror)
                self.assertEqual(len(seen), 3 * count + 1)      # no row lost or doubled
                missing = "2026-09-29T13:00:00.000000Z"
                self.assertEqual(seen["gone"], {"missing_from_source_since": missing})
                for i in range(count):
                    old = seen[f"l{i:02d}-1"]
                    self.assertEqual((old["replaced_in_source"], old["replaced_basis"]),
                                     (missing, "source_replacement"))
                    successor = mirror.resolve_citation(old["successor"], OPS)
                    self.assertEqual((successor["status"], successor["key"]),
                                     ("current", {"id": f"z{i:02d}"}))
                    self.assertEqual(seen[f"l{i:02d}-2"], {})
                    self.assertEqual(seen[f"z{i:02d}"], {})
                cited = mirror.resolve_citation(
                    mirror.rows(OPS, LINES, include_missing=True,
                                where=[["id", "eq", "l00-1"]])["rows"][0]["citation"], OPS)
                self.assertEqual(cited["successor"], seen["l00-1"]["successor"])
                coverage = mirror.coverage(OPS)
                self.assertEqual((coverage["rows_replaced_in_source_total"],
                                  coverage["rows_ambiguous_replacement_total"],
                                  coverage["rows_missing_total"]), (count, 0, count + 1))
                self.assertEqual(mirror.coverage(OUTSIDER)["relations"], [])
        # An incremental (since) capture never infers a removal, nor a replacement.
        fake.tables[LINES] = [r for r in fake.tables[LINES] if r["id"] != "l00-2"]
        fake.tables[LINES].append(line("z-late", "po00", 2, updated_at="2026-09-29T14:30:00"))
        clock.advance(3600)
        mirror.refresh(OPS, LINES)
        later = labels(mirror)
        self.assertEqual((later["l00-2"], later["z-late"]), ({}, {}))
        self.assertEqual(mirror.coverage(OPS)["rows_replaced_in_source_total"], 60)
        # The next full capture pairs it with the successor the since capture read.
        clock.advance(3600)
        mirror.refresh(OPS, LINES, full=True)
        self.assertEqual(mirror.resolve_citation(labels(mirror)["l00-2"]["successor"],
                                                 OPS)["key"], {"id": "z-late"})
        # Facts of an unexpected shape fail closed (the mirror never opens on them).
        with self.assertRaises(ValueError):
            replacement_models({"models": {"PurchaseOrderLine": {"table": "x", "id": "id"}}})

    def test_a_pairing_that_is_not_one_to_one_is_ambiguous_and_names_no_successor(self):
        """The committed generated facts: a Portal PO line has no line number, so its
        successor is matched on parent and product, one to one in both captures."""
        rows = [line("a", "po1", product="prod-1"), line("b", "po1", product="prod-1"),
                line("c", "po2", product="prod-1"), line("d", "po3", product="prod-1"),
                line("e", "po3", product="prod-1"), line("f", "po4", product=None)]
        fake, clock = lines_workbench(rows), Clock()
        mirror = open_mirror(self, self.root / "pairs.sqlite", fake, clock)
        mirror.refresh(OPS, LINES, full=True)
        # po1: two same-product lines -> two fresh ids; po2: one -> one; po3: one of two
        # same-product lines -> one fresh id; po4: a line with no product -> a fresh id.
        fake.tables[LINES] = [line("e", "po3", product="prod-1")] + [
            line(key, po, product=product) for key, po, product in (
                ("n1", "po1", "prod-1"), ("n2", "po1", "prod-1"), ("n3", "po2", "prod-1"),
                ("n4", "po3", "prod-1"), ("n5", "po4", None))]
        clock.advance(3600)
        mirror.refresh(OPS, LINES, full=True)
        seen = labels(mirror)

        def candidates(key):
            return sorted(json.loads(identifiers_of(h))["id"]
                          for h in seen[key]["replacement_candidates"])

        for key in ("a", "b"):
            self.assertNotIn("successor", seen[key])
            self.assertEqual(candidates(key), ["n1", "n2"])
        self.assertEqual(seen["c"]["replaced_basis"], "source_replacement")
        self.assertEqual(mirror.resolve_citation(seen["c"]["successor"], OPS)["key"],
                         {"id": "n3"})
        self.assertEqual(candidates("d"), ["n4"])     # e is held in both captures
        self.assertEqual(set(seen["f"]), {"missing_from_source_since"})
        coverage = mirror.coverage(OPS)
        self.assertEqual((coverage["rows_replaced_in_source_total"],
                          coverage["rows_ambiguous_replacement_total"]), (1, 3))

    def test_the_live_check_records_replaced_rows_as_counts_only(self):
        """mirror.relation_counts over the fake transport: the rows are replaced between
        the counter's two reads, read through the spec as the verifier records it."""
        from devloop import live_verify as lv
        from scripts import mirror_measure

        def measure(rows, after, *args):
            fake, reads = lines_workbench(list(rows)), []

            def fetch(url, headers, *, max_bytes):
                out = fake(url, headers, max_bytes=max_bytes)
                if url.split("?")[0].endswith("/rows"):
                    reads.append(url)
                    if len(reads) == 1:   # after the full traversal, before the second read
                        fake.tables[LINES] = after
                return out

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mirror_measure.main(["relation-counts", "--relation", LINES, *args],
                                           environ={}, client=WorkbenchReadApiClient(
                                               BASE, KEY, fetch=fetch))
            self.assertEqual((code, len(reads)), (0, 2))
            self.assertNotIn("po1", out.getvalue())
            return json.loads(out.getvalue())

        # Fresh ids sorting inside the second read's window (before the surviving "zz").
        rows = [line("a", "po1", product="prod-1"), line("b", "po1", product="prod-2"),
                line("c", "po2", product="prod-1"), line("d", "po2", product="prod-1"),
                line("e", "po3", product="prod-1"), line("zz", "po9", product="prod-9")]
        report = measure(rows, [rows[1], rows[5]] + [
            line(key, po, product="prod-1") for key, po in
            (("n1", "po1"), ("n2", "po2"), ("n3", "po2"))])
        self.assertEqual(report["row_data_check"]["leaked"], 0)
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.relation_counts.json")
                          .read_text())
        recorded = lv._recorded(spec, report)
        self.assertEqual((recorded["replaced_rows_total"],
                          recorded["ambiguous_replacement_total"]), (1, 2))
        self.assertEqual([recorded[f"relations[*].{name}"] for name in (
            "replacement_key", "replaced_rows", "ambiguous_replacements")],
            [[True], [1], [2]])
        failures = lv.check_expectations(spec, 0, report)
        self.assertFalse([f for f in failures if "replac" in f], failures)
        # A plain deletion: the same-PO, same-product row past the 2-key sample was read
        # by the first full read, so it is no successor (reviewer repro).
        rows = [line("a", "po1"), line("b", "po2"), line("c", "po1")]
        report = measure(rows, rows[1:], "--sample-keys", "2")
        self.assertEqual((report["replaced_rows_total"],
                          report["ambiguous_replacement_total"]), (0, 0))


class ParentOnlyReplacementTests(unittest.TestCase):
    """The committed facts: a model whose foreign keys are only its cascade parent
    (customer location, MO line) never pairs a delete and an add under that parent; a
    unique constraint on the parent alone (a customer's user) and a line keyed on
    parent and more (sales order) still do. A file labelled under another rule is never
    read with those labels; a writer opening it drops them and keeps the real ones."""

    TABLES = {"public.app_customer": ("user_id", ["code"]),
              "public.app_customer_location": ("customer_id", ["label"]),
              "public.app_manufacturing_order_line": ("assembly_order_id", ["product_id"]),
              "public.app_sales_order_line": ("sales_order_id",
                                              ["product_id", "warehouse_id"])}
    PARENT_ONLY = ("public.app_customer_location", "public.app_manufacturing_order_line")
    PAIRED = ("public.app_customer", "public.app_sales_order_line")

    def test_an_unserved_optional_link_is_no_key_column_an_unserved_required_one_is(self):
        """Portal 5cb95e1 (TASK-2564) adds receipt line cmr_line_id (onDelete SetNull), which
        the reporting API does not serve: it takes no part in the key; an unserved link
        of another kind still stops the inference."""
        line = {"id": ["id"], "unique": [], "relations": [
            {"columns": ["receipt_id"], "on_delete": "Cascade"},
            {"columns": ["product_id"], "on_delete": None},
            {"columns": ["cmr_line_id"], "on_delete": "SetNull"}]}
        models = {"public.line": line}
        served = ["id", "receipt_id", "product_id"]
        self.assertEqual(replacement_key(models, "public.line", served),
                         ["receipt_id", "product_id"])
        self.assertEqual(replacement_key(models, "public.line", served + ["cmr_line_id"]),
                         ["receipt_id", "product_id", "cmr_line_id"])
        self.assertIsNone(replacement_key(models, "public.line", ["id", "receipt_id"]))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def replaced(self, count):
        """A mirror whose two full captures saw, under each of ``count`` parents, every
        relation's row deleted and one with the same values added."""
        fake, clock = FakeWorkbench(), Clock()
        fake.catalog = {name: {"columns": ["id", parent, *rest, "updated_at"],
                               "key": ["id"], "domain": "orders", "since": SINCE}
                        for name, (parent, rest) in self.TABLES.items()}

        def rows(prefix):
            return {name: [{"id": f"{prefix}{i}", parent: f"p{i}",
                            **{c: f"v{i}" for c in rest}, "updated_at": "2026-09-29T10:00:00"}
                           for i in range(count)]
                    for name, (parent, rest) in self.TABLES.items()}

        fake.tables = rows("old")
        path = self.root / f"m{count}.sqlite"
        mirror = open_mirror(self, path, fake, clock)
        for name in self.TABLES:
            mirror.refresh(OPS, name, full=True)
        fake.tables = rows("new")
        clock.advance(3600)
        for name in self.TABLES:
            mirror.refresh(OPS, name, full=True)
        return mirror, path, fake, clock

    def assertPairings(self, mirror, count):
        """No row lost or merged; only the paired relations name their successors."""
        for name in self.TABLES:
            rows = {r["key"]["id"]: r for r in mirror.rows(
                OPS, name, limit=500, include_missing=True)["rows"]}
            self.assertEqual(len(rows), 2 * count)
            for i in range(count):
                self.assertNotIn("missing_from_source_since", rows[f"new{i}"])
                old = rows[f"old{i}"]
                if name in self.PARENT_ONLY:
                    self.assertIn("missing_from_source_since", old)
                    self.assertFalse({"replaced_in_source", "successor",
                                      "ambiguous_replacement"} & set(old))
                else:
                    self.assertEqual(mirror.resolve_citation(old["successor"], OPS)["key"],
                                     {"id": f"new{i}"})
        self.assertEqual(mirror.coverage(OPS)["rows_replaced_in_source_total"], 2 * count)

    def forge_parent_only_labels(self, conn, count):
        """Label each parent-only ``old<i>`` replaced by ``new<i>``, as the parent-only
        rule (or a copy it labelled, imported before 5759fdb7a) left the file."""
        for name in self.PARENT_ONLY:
            for i in range(count):
                capture = conn.execute(
                    "SELECT capture_id FROM row_current WHERE relation = ? AND "
                    "key_json = ?", (name, f'{{"id":"new{i}"}}')).fetchone()[0]
                forged = conn.execute(
                    "UPDATE row_removal SET replaced_basis = 'source_replacement', "
                    "replaced_by = ? WHERE relation = ? AND key_json = ?",
                    (f'[[{{"id":"new{i}"}},{capture}]]', name, f'{{"id":"old{i}"}}'))
                self.assertEqual(forged.rowcount, 1)

    def test_a_delete_and_add_under_one_parent_is_no_replacement_at_1_10_60(self):
        for count in (1, 10, 60):
            with self.subTest(parents=count):
                started = time.monotonic()
                mirror = self.replaced(count)[0]
                self.assertPairings(mirror, count)
                self.assertLess(time.monotonic() - started, 30)

    def test_ro_projection_of_a_parent_only_labelled_mirror_names_no_successor(self):
        count = 3
        mirror, path, fake, clock = self.replaced(count)
        mirror.close()
        # The file as the parent-only rule labelled it, not reopened by a writer since.
        conn = sqlite3.connect(str(path))
        with conn:
            self.forge_parent_only_labels(conn, count)
            conn.execute("DELETE FROM mirror_meta WHERE name = 'replacement_rule'")
        conn.close()

        def build(name):
            with MirrorCaptures(path) as captures:
                build_store(self.root / name, schema_loader.load_schema(),
                            {"portal_api": captures}, normalizer=identifiers.normalize,
                            clock=lambda: clock.now)
            conn = sqlite3.connect(str(self.root / name))
            try:
                return dict(conn.execute(
                    "SELECT object_id, replaced_by FROM object_removals WHERE until IS NULL"))
            finally:
                conn.close()

        with self.assertRaises(ProjectionError) as caught:
            build("stale.sqlite")
        self.assertEqual(caught.exception.code, "mirror_replacement_rule_stale")
        # A relation a running mirror fills from a persisted copy of that file through
        # an outage brings none of those labels either.
        store = mirror_capture.MirrorCaptureStore(self.root / "state", clock=clock)
        location = self.PARENT_ONLY[0]
        self.assertTrue(store.persist(path, [location], {"runs": [
            {"relation": location, "outcome": "captured"}]})["persisted"])

        def outage(url, headers, *, max_bytes):
            raise SourceHttpError("read-only source GET failed: HTTPError", status=500,
                                  error_code="internal")

        running = SourceMirror(self.root / "running.sqlite",
                               WorkbenchReadApiClient(BASE, KEY, fetch=outage), ALL,
                               clock=clock, retry_delay_seconds=0, persisted_store=store)
        self.addCleanup(running.close)
        filled = running.rows(OPS, location, limit=500, include_missing=True)
        self.assertEqual(filled["freshness"]["refresh_reason"], "persisted capture")
        self.assertEqual(sorted(r["key"]["id"] for r in filled["rows"]
                                if "missing_from_source_since" in r and "successor" not in r),
                         [f"old{i}" for i in range(count)])
        # A writer opening it drops the parent-only labels and keeps the real pairings,
        # which the projection then reads (one per paired relation and parent).
        mirror = open_mirror(self, path, fake, clock)
        self.assertPairings(mirror, count)
        removals = build("clean.sqlite")
        self.assertEqual(len(removals), len(self.TABLES) * count, removals)
        self.assertEqual(sum(1 for successor in removals.values() if successor),
                         len(self.PAIRED) * count, removals)

    def test_ro_projection_of_a_v1_marked_mirror_reads_its_labels_an_unknown_marker_none(self):
        """A file the v1 rule labelled (no pairing on a unique constraint on the parent
        alone) and no writer reopened since projects read-only with its successors as
        they stand, less the parent-only labels a relation it filled from a persisted copy
        before 5759fdb7a carried in (the rule in force gives those relations no key); a
        marker of a rule this reader does not know is refused, labelled or not."""
        count = 10
        mirror, path, _fake, clock = self.replaced(count)
        mirror.close()
        customer, line = self.PAIRED
        conn = sqlite3.connect(str(path))
        with conn:
            conn.execute("UPDATE row_removal SET replaced_basis = NULL, replaced_by = NULL "
                         "WHERE relation = ?", (customer,))
            self.forge_parent_only_labels(conn, count)
            conn.execute("UPDATE mirror_meta SET value = 'beyond-cascade-parent/v1' "
                         "WHERE name = 'replacement_rule'")
        conn.close()

        def build(name):
            with MirrorCaptures(path) as captures:
                build_store(self.root / name, schema_loader.load_schema(),
                            {"portal_api": captures}, normalizer=identifiers.normalize,
                            clock=lambda: clock.now)
            conn = sqlite3.connect(str(self.root / name))
            try:
                return conn.execute("SELECT object_id, replaced_basis, replaced_by FROM "
                                    "object_removals WHERE until IS NULL").fetchall()
            finally:
                conn.close()

        removals = build("v1.sqlite")
        self.assertEqual(len(removals), len(self.TABLES) * count, removals)
        labelled = [row for row in removals if row[1] is not None]
        self.assertEqual(len(labelled), count, removals)
        for object_id, basis, successor in labelled:
            self.assertIn("app_sales_order_line", object_id)
            self.assertEqual(basis, "source_replacement")
            self.assertEqual(len(json.loads(successor)), 1)
            self.assertEqual(json.loads(successor)[0].replace("new", "old"), object_id)
        for marker, labels in (("beyond-cascade-parent/v9", True), ("unknown", False)):
            with self.subTest(marker=marker, labelled=labels):
                conn = sqlite3.connect(str(path))
                with conn:
                    if not labels:
                        conn.execute("UPDATE row_removal SET replaced_basis = NULL, "
                                     "replaced_by = NULL")
                    conn.execute("UPDATE mirror_meta SET value = ? "
                                 "WHERE name = 'replacement_rule'", (marker,))
                conn.close()
                with self.assertRaises(ProjectionError) as caught:
                    build(f"{marker.replace('/', '_')}.sqlite")
                self.assertEqual(caught.exception.code, "mirror_replacement_rule_stale")

    def test_reader_and_writer_drop_the_same_labels_under_injected_facts(self):
        """One rule, one model set: on a v1-marked file a read-only build and a writer
        opening it, both given facts under which the sales order line has no owning
        parent, leave exactly the same relations labelled (the customer's alone)."""
        mirror, path, fake, clock = self.replaced(3)
        mirror.close()
        conn = sqlite3.connect(str(path))
        with conn:
            conn.execute("UPDATE mirror_meta SET value = 'beyond-cascade-parent/v1' "
                         "WHERE name = 'replacement_rule'")
        conn.close()
        facts = json.loads(PRISMA_FACTS_PATH.read_text())
        for model in facts["models"].values():
            if model["table"] == "app_sales_order_line":
                for link in model["relations"]:
                    link["on_delete"] = None

        def read(name, **options):
            with MirrorCaptures(path, **options) as captures:
                build_store(self.root / name, schema_loader.load_schema(),
                            {"portal_api": captures}, normalizer=identifiers.normalize,
                            clock=lambda: clock.now)
            conn = sqlite3.connect(str(self.root / name))
            try:
                return {"public." + object_id.split(":")[1].split(".")[-1]
                        for (object_id,) in conn.execute(
                            "SELECT object_id FROM object_removals "
                            "WHERE until IS NULL AND replaced_by IS NOT NULL")}
            finally:
                conn.close()

        self.assertEqual(read("default.sqlite"), set(self.PAIRED))
        reader = read("injected.sqlite", prisma_facts=facts)
        open_mirror(self, path, fake, clock, prisma_facts=facts).close()
        conn = sqlite3.connect(str(path))
        try:
            writer = {name for (name,) in conn.execute(
                "SELECT DISTINCT relation FROM row_removal WHERE replaced_by IS NOT NULL")}
        finally:
            conn.close()
        self.assertEqual(reader, {CUSTOMERS})
        self.assertEqual(writer, reader)


def identifiers_of(handle):
    from integrations.source_mirror import parse_citation
    return json.dumps(parse_citation(handle)[2])


class ReplacementReadThroughTests(unittest.TestCase):
    def test_a_review_on_a_replaced_receipt_line_reads_through_to_its_successor(self):
        """The committed facts: a receipt upsert re-inserts its line under a fresh id."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        fake, clock = FakeWorkbench(), Clock()
        fake.catalog = {RECEIPT_LINES: {"columns": ["id", "receipt_id", "product_id",
                                                     "supplier_lot_number", "updated_at"],
                                        "key": ["id"], "domain": "purchasing",
                                        "since": SINCE}}

        def receipt_line(key, lot):
            return {"id": key, "receipt_id": "rc1", "product_id": "prod-1",
                    "supplier_lot_number": lot, "updated_at": "2026-09-29T10:00:00"}

        fake.tables = {RECEIPT_LINES: [receipt_line("r1", "LOT-A")]}
        mirror = open_mirror(self, root / "mirror.sqlite", fake, clock)
        mirror.refresh(OPS, RECEIPT_LINES, full=True)
        reviewed_at = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
        store = reviews.ReviewStore(root / "reviews.sqlite", clock=lambda: reviewed_at)
        old, new = (f'ReceiptLine:portal.app_receipt_line:{{"id":"{key}"}}'
                    for key in ("r1", "r2"))

        def build(path):
            build_store(path, schema_loader.load_schema(),
                        {"portal_api": MirrorCaptures(root / "mirror.sqlite"),
                         "graph_mail": tr.Static("graph_mail", tr.FIRST)},
                        normalizer=identifiers.normalize, clock=lambda: clock.now,
                        post_project={"reviews": reviews.link_step(store, "plx")})
            base = tr.ontology(path, store, clock.now)
            self.addCleanup(base.store.conn.close)
            return base, rt.ReviewTools(base, store, session=A, gate=tr.identity_reviewer)

        base, review = build(root / "first.sqlite")
        cite = next(v["citation"] for v in base.call("plx.object", {"object_id": old},
                                                       A.actor)["values"]
                    if v["property"] == "supplier_lot_number")
        recorded = review.call("plx.review", {
            "mode": "record", "decision": "accept_link", "subject": tr.EMAIL, "target": old,
            "link_type": "Email.mentions_receipt_line",
            "evidence": [{"l1_handle": cite["l1_handle"], "digest": cite["digest"],
                          "field_path": "row.supplier_lot_number"}]}, A.actor)
        self.assertEqual(recorded["review"]["state"], "effective", recorded)

        # The receipt is saved again: its line comes back as r2 with the same content.
        fake.tables[RECEIPT_LINES] = [receipt_line("r2", "LOT-A")]
        clock.now = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        mirror.refresh(OPS, RECEIPT_LINES, full=True)
        clock.now = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)
        base, review = build(root / "later.sqlite")
        status = review.call("plx.review", {"mode": "status", "subject": tr.EMAIL}, A.actor)
        self.assertEqual(validate(status, tr.CONTRACTS["tools"]["plx.review"]["output"],
                                  root=tr.CONTRACTS), [])
        [shown] = status["reviews"]
        self.assertEqual((shown["state"], shown["state_reason"], shown["replaced"]),
                         ("withdrawn", "source_removed",
                          [{"object_id": old, "basis": "source_replacement",
                            "successor": new}]))
        # The review never moves to the successor: its link ends with the old line.
        counts = {c["link_type"]: c.get("tiers") for c in base.call(
            "plx.object", {"object_id": new}, A.actor)["linked_counts"]}
        self.assertNotIn("Email.mentions_receipt_line", counts)


if __name__ == "__main__":
    unittest.main()
