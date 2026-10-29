"""scripts.mirror_measure: the no-store keyset counter and the bound-first coverage probe.

Every test runs the real ``WorkbenchReadApiClient`` over an injected transport (no
network). ``KeysetFake`` follows Portal's keyset contract (petralabx/plx-customer-portal
services/workbench-api/src/core/query.mjs: rows ordered by key, ``nextCursor`` the last
key, null at the end) with scripted HTTP failures and ETL re-inserts.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from devloop import live_verify as lv
from integrations.http import SourceHttpError
from integrations.workbench_read_api import WorkbenchReadApiClient
from scripts import mirror_measure as mm

BASE = "https://wb.example.test"
KEY = "plxwb_" + "k" * 30


def cuid(number: int, prefix: str = "c") -> str:
    """A cuid-shaped id (time-ordered text); long enough for the leak check."""
    return f"{prefix}m{number:010d}x"


class KeysetFake:
    def __init__(self) -> None:
        self.catalog: dict[str, dict] = {}
        self.tables: dict[str, list[dict]] = {}
        self.requests: list[tuple[str, int, str | None]] = []
        self.fail_above: dict[str, int] = {}   # HTTP 500 when limit > value
        self.throttle_once: set[str] = set()
        self.overlap: set[str] = set()          # next page restarts at the cursor row
        self.on_page = None                     # callable(fake, name, cursor)
        self.sort_key = lambda row: row["id"]   # the service's ORDER BY

    def add(self, name: str, ids: list[str], **entry) -> None:
        self.catalog[name] = {"columns": ["id", "qty", "updated_at"], "key": ["id"],
                              "since": None, **entry}
        self.tables[name] = [{"id": value, "qty": "1", "updated_at": "2026-10-01"}
                             for value in ids]

    def __call__(self, url, headers, *, max_bytes):
        parts = urlsplit(url)
        if parts.path == "/v1/relations":
            return {"relations": [{"name": name, "schema": name.split(".")[0],
                                   "relation": name.split(".")[1], "unavailable": None,
                                   **entry} for name, entry in self.catalog.items()]}
        name = parts.path.split("/")[3]
        query = parse_qs(parts.query)
        limit = int(query["limit"][0])
        cursor = query.get("cursor", [None])[0]
        self.requests.append((name, limit, cursor))
        if name in self.throttle_once:
            self.throttle_once.discard(name)
            raise SourceHttpError("read-only source GET failed: HTTPError", status=429,
                                  retry_after_seconds=2.0)
        if name in self.fail_above and limit > self.fail_above[name]:
            raise SourceHttpError("read-only source GET failed: HTTPError", status=500)
        if self.on_page is not None:
            self.on_page(self, name, cursor)
        columns = sorted(set(query["columns"][0].split(",")) | {"id"})
        rows = sorted(self.tables[name], key=self.sort_key)
        if cursor is not None:
            after = self.sort_key({"id": json.loads(cursor)})
            rows = [row for row in rows if (self.sort_key(row) >= after
                                            if name in self.overlap
                                            else self.sort_key(row) > after)]
        page, more = rows[:limit], len(rows) > limit
        return {"relation": name, "columns": columns,
                "rows": [{c: row[c] for c in columns} for row in page],
                "nextCursor": json.dumps(page[-1]["id"]) if more else None}


class CounterFixture:
    """The fake keyset service and the counter, shared without inheriting tests."""

    def setUp(self) -> None:
        self.fake = KeysetFake()
        self.client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        self.sleeps: list[float] = []

    def count(self, names, **kwargs):
        kwargs.setdefault("mirror_bound", 1_000_000)
        kwargs.setdefault("sample_keys", 3000)
        return mm.relation_counts(self.client, names, sleep=self.sleeps.append, **kwargs)

    def item(self, report, name):
        return {entry["relation"]: entry for entry in report["relations"]}[name]


class CounterTests(CounterFixture, unittest.TestCase):
    def test_stable_ids_give_the_true_count_and_print_no_key(self):
        self.fake.add("public.app_stable", [cuid(i) for i in range(12_000)])
        report = self.count(["public.app_stable"])
        item = self.item(report, "public.app_stable")
        self.assertEqual((item["complete"], item["stop"]), (True, "end"))
        self.assertEqual((item["rows_seen"], item["distinct_keys"], item["pages"]),
                         (12_000, 12_000, 3))
        self.assertEqual((item["first_read_keys"], item["seen_in_both"]), (3000, 3000))
        self.assertEqual(item["ratio_seen_in_both"], 1.0)
        self.assertTrue(item["hash_equal"])
        self.assertRegex(item["first_keys_hash"], r"^[0-9a-f]{16}$")
        self.assertEqual((item["verdict"], item["classification"]),
                         ("stable_within_bound", "ok"))
        self.assertIsNone(item["previous_run_hash_equal"])
        self.assertEqual(report["data_truth_defect_count"], 0)
        self.assertFalse(report["stores_rows"])
        # Only key columns were requested: the counter never reads a value column.
        self.assertTrue(all(limit == 5000 for _, limit, _ in self.fake.requests))
        check = report["row_data_check"]
        self.assertGreaterEqual(check["values_checked"], 3000)
        self.assertEqual(check["leaked"], 0)
        text = json.dumps(report)
        self.assertNotIn(cuid(0), text)
        self.assertNotIn(cuid(11_999), text)

    def test_stable_count_above_the_mirror_bound_is_a_mirror_bound_not_a_data_defect(self):
        self.fake.add("public.app_big", [cuid(i) for i in range(12_000)])
        item = self.item(self.count(["public.app_big"], mirror_bound=10_000), "public.app_big")
        self.assertTrue(item["exceeds_mirror_bound"])
        self.assertEqual((item["verdict"], item["classification"]),
                         ("stable_exceeds_mirror_bound", "mirror_bound"))

    def test_ids_reinserted_between_reads_are_a_data_truth_defect(self):
        self.fake.add("public.app_etl", [cuid(i) for i in range(8000)])
        reads = {"start": 0}

        def reinsert(fake, name, cursor):
            # The ETL replaces every row with a fresh id just before the second read.
            if cursor is None:
                reads["start"] += 1
                if reads["start"] == 2:
                    fake.tables[name] = [{**row, "id": cuid(i, "d")}
                                         for i, row in enumerate(fake.tables[name])]

        self.fake.on_page = reinsert  # fresh ids sort after every old one
        report = self.count(["public.app_etl"])
        item = self.item(report, "public.app_etl")
        self.assertTrue(item["complete"])
        self.assertEqual(item["distinct_keys"], 8000)  # the count itself looked fine
        self.assertEqual((item["seen_in_both"], item["ratio_seen_in_both"]), (0, 0.0))
        self.assertEqual(item["missing_in_second"], 3000)
        self.assertEqual(item["new_in_window"], 0)  # fresh cuids sort after the window
        self.assertFalse(item["hash_equal"])
        self.assertEqual((item["verdict"], item["classification"]),
                         ("ids_replaced", "data_truth_defect"))
        self.assertIn("bulk purge", item["verdict_note"])
        self.assertEqual(report["data_truth_defect_count"], 1)
        self.assertEqual(report["by_classification"], {"data_truth_defect": ["public.app_etl"]})
        self.assertEqual(report["row_data_check"]["leaked"], 0)

    def _between_reads(self, name, change):
        reads = {"start": 0}

        def hook(fake, relation, cursor):
            if relation == name and cursor is None:
                reads["start"] += 1
                if reads["start"] == 2:
                    change(fake.tables[relation])

        self.fake.on_page = hook

    def test_a_single_delete_between_reads_is_not_a_defect_and_the_spec_passes(self):
        self.fake.add("public.app_del", [cuid(i) for i in range(30_000)])
        self._between_reads("public.app_del", lambda rows: rows.pop(1234))
        report = self.count(["public.app_del"], sample_keys=20_000)
        item = self.item(report, "public.app_del")
        self.assertEqual((item["missing_in_second"], item["new_in_window"]), (1, 0))
        self.assertEqual(item["ratio_seen_in_both"], 0.99995)
        self.assertEqual((item["verdict"], item["classification"]), ("deletes_observed", "ok"))
        self.assertEqual(report["data_truth_defect_count"], 0)
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.relation_counts.json")
                          .read_text())
        failures = lv.check_expectations(spec, 0, report)
        self.assertEqual([f for f in failures if "data_truth" in f], [])

    def test_a_delete_plus_a_new_key_inside_the_window_is_a_reinsert(self):
        self.fake.add("public.app_swap", [cuid(i * 2) for i in range(10_000)])

        def swap(rows):
            gone = rows.pop(100)
            rows.append({**gone, "id": cuid(201)})  # odd: never read, sorts inside

        self._between_reads("public.app_swap", swap)
        item = self.item(self.count(["public.app_swap"]), "public.app_swap")
        self.assertEqual((item["missing_in_second"], item["new_in_window"]), (1, 1))
        self.assertEqual((item["verdict"], item["classification"]),
                         ("ids_reinserted", "data_truth_defect"))

    def test_int8_keys_as_strings_trust_the_service_order_and_a_delete_stays_ok(self):
        # int8 keys arrive as strings in numeric order ("9" before "10"); a code-point
        # re-sort would misplace them. One delete must stay deletes_observed.
        self.fake.add("public.app_int8", [str(i) for i in range(1, 3001)])
        self.fake.sort_key = lambda row: int(row["id"])
        self._between_reads("public.app_int8", lambda rows: rows.pop(500))
        item = self.item(self.count(["public.app_int8"], sample_keys=2000), "public.app_int8")
        self.assertEqual((item["missing_in_second"], item["new_in_window"]), (1, 0))
        self.assertEqual((item["verdict"], item["classification"]), ("deletes_observed", "ok"))

    def test_a_deleted_tail_key_shortens_the_window_and_hides_a_new_key_among_the_tail(self):
        """Known limitation: the window ends at the last surviving first-read key, so a
        new key sorting between deleted tail keys is not seen; window_shortened says so."""
        self.fake.add("public.app_tail", [cuid(i * 2) for i in range(1000)])

        def swap(rows):
            rows.pop(99)
            rows.pop(98)
            rows.append({"id": cuid(197), "qty": "1", "updated_at": "x"})  # between them

        self._between_reads("public.app_tail", swap)
        item = self.item(self.count(["public.app_tail"], sample_keys=100), "public.app_tail")
        self.assertEqual((item["missing_in_second"], item["new_in_window"]), (2, 0))
        self.assertTrue(item["window_shortened"])
        self.assertEqual(item["verdict"], "deletes_observed")

    def test_a_mid_delete_does_not_shorten_the_window(self):
        self.fake.add("public.app_mid", [cuid(i * 2) for i in range(1000)])
        self._between_reads("public.app_mid", lambda rows: rows.pop(10))
        item = self.item(self.count(["public.app_mid"], sample_keys=100), "public.app_mid")
        self.assertFalse(item["window_shortened"])

    def test_an_insert_alone_inside_the_window_is_not_a_defect(self):
        self.fake.add("public.app_ins", [cuid(i * 2) for i in range(10_000)])
        self._between_reads("public.app_ins", lambda rows: rows.append(
            {"id": cuid(201), "qty": "1", "updated_at": "x"}))
        item = self.item(self.count(["public.app_ins"]), "public.app_ins")
        self.assertEqual((item["missing_in_second"], item["new_in_window"]), (0, 1))
        self.assertEqual(item["classification"], "ok")

    def test_the_stability_reread_stays_inside_the_relation_budget(self):
        self.fake.add("public.app_tight", [cuid(i) for i in range(20_000)])
        clock = {"now": 0.0}
        real = self.fake.__call__

        def fetch(url, headers, *, max_bytes):
            if urlsplit(url).path.endswith("/rows"):
                clock["now"] += 10.0
            return real(url, headers, max_bytes=max_bytes)

        client = WorkbenchReadApiClient(BASE, KEY, fetch=fetch)
        # 4 traversal pages end at t=40 (inside 0.9 x 45 s); the re-read gets 5 s more.
        report = mm.relation_counts(client, ["public.app_tight"], budget_seconds=45,
                                    mirror_bound=1_000_000, sample_keys=6000,
                                    sleep=self.sleeps.append, monotonic=lambda: clock["now"])
        item = self.item(report, "public.app_tight")
        self.assertTrue(item["complete"])
        self.assertTrue(item["second_read_cut"])
        self.assertLessEqual(clock["now"], 50.0)
        self.assertEqual((item["verdict"], item["classification"]),
                         ("count_lower_bound", "inconclusive"))

    def test_rolling_reinsert_never_ends_and_is_still_named_a_data_defect(self):
        # The live failure shape: every page the ETL drops the oldest rows and inserts
        # the same number under fresh, later-sorting ids, so a traversal never ends.
        self.fake.add("public.app_rolling", [cuid(i) for i in range(10_000)])
        state = {"next": 10_000}

        def roll(fake, name, cursor):
            rows = sorted(fake.tables[name], key=lambda row: row["id"])[5000:]
            rows += [{"id": cuid(state["next"] + i), "qty": "1", "updated_at": "x"}
                     for i in range(5000)]
            state["next"] += 5000
            fake.tables[name] = rows

        self.fake.on_page = roll
        item = self.item(self.count(["public.app_rolling"], max_keys=40_000),
                         "public.app_rolling")
        self.assertEqual((item["complete"], item["stop"]), (False, "key_bound"))
        self.assertGreaterEqual(item["distinct_keys"], 40_000)
        self.assertEqual(item["seen_in_both"], 0)
        self.assertEqual((item["verdict"], item["classification"]),
                         ("ids_replaced", "data_truth_defect"))

    def test_http_500_halves_the_page_and_retries_the_same_cursor(self):
        self.fake.add("sage.oe_detail_like", [cuid(i) for i in range(3000)])
        self.fake.fail_above["sage.oe_detail_like"] = 1250
        item = self.item(self.count(["sage.oe_detail_like"]), "sage.oe_detail_like")
        self.assertEqual([limit for _, limit, _ in self.fake.requests[:3]], [5000, 2500, 1250])
        self.assertEqual({cursor for _, _, cursor in self.fake.requests[:3]}, {None})
        self.assertEqual((item["page_halvings"], item["page_size_final"]), (2, 1250))
        self.assertEqual(item["retried"], {"500": 2})
        self.assertEqual((item["complete"], item["distinct_keys"]), (True, 3000))
        self.assertEqual(item["classification"], "ok")

    def test_http_500_at_the_smallest_page_is_recorded_upstream_unavailable(self):
        self.fake.add("sage.always_500", [cuid(i) for i in range(10)])
        self.fake.add("public.app_fine", [cuid(i) for i in range(10)])
        self.fake.fail_above["sage.always_500"] = 0
        report = self.count(["sage.always_500", "public.app_fine"])
        item = self.item(report, "sage.always_500")
        self.assertEqual([limit for name, limit, _ in self.fake.requests
                          if name == "sage.always_500"], [5000, 2500, 1250, 625, 312, 250])
        self.assertEqual((item["failure_reason"], item["failure_http_status"]),
                         ("upstream_unavailable", 500))
        self.assertEqual(item["failure_error_class"], "SourceHttpError")
        self.assertEqual((item["verdict"], item["classification"]),
                         ("upstream_unavailable", "upstream_unavailable"))
        self.assertEqual(item["pages"], 0)
        # One unreadable relation does not stop the others.
        self.assertEqual(self.item(report, "public.app_fine")["classification"], "ok")
        self.assertEqual(report["upstream_unavailable_count"], 1)
        self.assertEqual(report["measured_count"], 1)

    def test_throttle_waits_retry_after_without_halving(self):
        self.fake.add("public.app_busy", [cuid(i) for i in range(10)])
        self.fake.throttle_once.add("public.app_busy")
        item = self.item(self.count(["public.app_busy"]), "public.app_busy")
        self.assertEqual(self.sleeps, [2.0])
        self.assertEqual((item["throttle_waits"], item["page_halvings"]), (1, 0))
        self.assertTrue(item["complete"])

    def test_a_key_repeated_on_a_later_page_is_a_paging_defect(self):
        self.fake.add("public.app_overlap", [cuid(i) for i in range(30)])
        self.fake.overlap.add("public.app_overlap")
        item = self.item(self.count(["public.app_overlap"], page_size=10, min_page_size=5,
                                    sample_keys=5), "public.app_overlap")
        self.assertGreater(item["cross_page_repeats"], 0)
        self.assertEqual(item["distinct_keys"], 30)
        self.assertEqual((item["verdict"], item["classification"]),
                         ("keyset_repeats", "paging_defect"))

    def test_relation_missing_from_the_catalog_is_not_served(self):
        self.fake.add("public.app_other", [cuid(1)])
        item = self.item(self.count(["public.app_gone"]), "public.app_gone")
        self.assertEqual((item["in_catalog"], item["verdict"]), (False, "not_served"))
        self.assertEqual(self.fake.requests, [])

    def test_budget_cut_is_a_lower_bound_not_a_verdict_on_the_data(self):
        self.fake.add("public.app_slow", [cuid(i) for i in range(20_000)])
        ticks = iter(range(0, 10_000, 10))
        report = mm.relation_counts(self.client, ["public.app_slow"], budget_seconds=35,
                                    mirror_bound=1_000_000, sample_keys=100,
                                    sleep=self.sleeps.append,
                                    monotonic=lambda: float(next(ticks)))
        item = self.item(report, "public.app_slow")
        self.assertEqual((item["complete"], item["stop"]), (False, "budget"))
        self.assertLess(item["distinct_keys"], 20_000)
        self.assertEqual((item["verdict"], item["classification"]),
                         ("count_lower_bound", "inconclusive"))

    def test_previous_run_hash_is_compared_from_a_verifier_report(self):
        self.fake.add("public.app_stable", [cuid(i) for i in range(50)])
        first = self.item(self.count(["public.app_stable"]), "public.app_stable")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            fields = {"relations[*].relation": ["public.app_stable"],
                      "relations[*].first_keys_hash": [first["first_keys_hash"]]}
            path.write_text(json.dumps({"schema": "plx-live-verify/v1", "checks": [
                {"id": "mirror.relation_counts", "detail": {"fields": fields}}]}))
            again = self.item(self.count(["public.app_stable"], previous_report=path),
                              "public.app_stable")
            self.assertTrue(again["previous_run_hash_equal"])
            fields["relations[*].first_keys_hash"] = ["0" * 16]
            path.write_text(json.dumps({"checks": [
                {"id": "mirror.relation_counts", "detail": {"fields": fields}}]}))
            changed = self.item(self.count(["public.app_stable"], previous_report=path),
                                "public.app_stable")
            self.assertFalse(changed["previous_run_hash_equal"])

    def test_spec_passes_and_records_compactly_through_the_verifier(self):
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.relation_counts.json")
                          .read_text())
        self.assertEqual(lv.validate_spec(spec), [])
        self.assertEqual(spec["cadence"], "weekly")
        self.assertIn("scripts.mirror_measure", lv.ALLOWED_MODULES)
        names = [spec["command"][i + 1] for i, arg in enumerate(spec["command"])
                 if arg == "--relation"]
        self.assertEqual(len(names), 6)
        for index, name in enumerate(names):
            self.fake.add(name, [cuid(index * 1000 + i) for i in range(400)])
        report = self.count(names, sample_keys=100)
        self.assertEqual(lv.check_expectations(spec, 0, report), [])
        detail = lv._cap(lv.scrub_tree({"fields": lv._recorded(spec, report)}, []))
        self.assertNotIn("fields_truncated", detail)
        fields = detail["fields"]
        self.assertEqual(fields["relations[*].relation"], names)
        self.assertEqual(fields["relations[*].classification"], ["ok"] * 6)
        self.assertTrue(all(len(h) == 16 for h in fields["relations[*].first_keys_hash"]))
        self.assertEqual(fields["row_data_check"]["leaked"], 0)
        # Each relation's completing run is recorded as its stamp, not reduced to a length.
        self.assertEqual((fields["complete_count"], fields["complete_this_run_count"]), (6, 6))
        self.assertTrue(all(isinstance(at, str) and mm._STAMP.fullmatch(at)
                            for at in fields["relations[*].completed_at"]))
        report["new_data_truth_defect_count"] = 1
        self.assertEqual(lv.check_expectations(spec, 0, report),
                         ["new_data_truth_defect_count != expected"])
        # ... a defect on a relation already known unstable does not, every day again.
        report.update(new_data_truth_defect_count=0, data_truth_defect_count=1)
        self.assertEqual(lv.check_expectations(spec, 0, report), [])
        self.assertNotIn("data_truth_defect_count", spec["expect"]["json_equals"])

    def test_cli_prints_the_error_class_only(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = mm.main(["relation-counts", "--relation", "public.app_x"], environ={})
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err.getvalue()), {"error": "LookupError",
                                                      "probe": "relation-counts"})
        self.fake.add("public.app_x", [cuid(i) for i in range(5)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mm.main(["relation-counts", "--relation", "public.app_x"], environ={},
                           client=self.client)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["complete_count"], 1)


class BoundRelationTests(unittest.TestCase):
    def test_repository_bound_relations_are_curated_portal_bindings(self):
        names = mm.bound_relations()
        self.assertGreater(len(names), 0)
        self.assertEqual(names, sorted(set(names)))
        self.assertTrue(all(mm._RELATION.fullmatch(name) for name in names))
        catalog = json.loads((lv.ROOT / "ontology/schema/generated/meta/portal_catalog.json")
                             .read_text())
        served = {entry["name"] for entry in catalog["relations"]}
        self.assertLess(len(names), len(served))  # the denominator is a subset

    def test_only_portal_bindings_outside_evidence_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.json").write_text(json.dumps({"object_types": [{"bindings": [
                {"source": "portal_api", "relation": "public.one"},
                {"source": "bc", "relation": "salesOrders"},
                {"source": "portal_api", "relation": "public.two",
                 "evidence": [{"source": "portal_api", "relation": "public.cited"}]}]}]}))
            Path(tmp, "_skip.json").write_text(json.dumps(
                {"source": "portal_api", "relation": "public.skip"}))
            self.assertEqual(mm.bound_relations(tmp), ["public.one", "public.two"])


class BoundFirstCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        from integrations.tests.test_source_mirror import FakeWorkbench
        self.fake = FakeWorkbench()
        self.client = WorkbenchReadApiClient(BASE, KEY, fetch=self._fetch)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mirror.sqlite"

    def _fetch(self, url, headers, *, max_bytes):
        from integrations.tests.test_source_mirror import KEY as FAKE_KEY
        return self.fake(url, {"Authorization": "Bearer " + FAKE_KEY}, max_bytes=max_bytes)

    def first_row_request(self):
        return [p.split("/")[3] for p in self.fake.paths if p.endswith("/rows")]

    def test_bound_relations_are_captured_first_and_counted_separately(self):
        names = sorted(self.fake.catalog)
        bound = [names[-1], names[-2], "public.not_served_anywhere"]
        report = mm.ordered_full_coverage(self.client, self.path, resolve_sample=10,
                                          bound=bound, retry_delay_seconds=0,
                                          sleep=lambda _: None)
        order = list(dict.fromkeys(self.first_row_request()))
        self.assertEqual(set(order[:2]), set(bound[:2]))
        self.assertEqual(report["capture_order"], "bound_first")
        self.assertEqual((report["bound_total"], report["bound_in_catalog_count"]), (3, 2))
        self.assertEqual(report["complete_bound_relation_count"], 2)
        self.assertEqual(report["bound_incomplete"], ["public.not_served_anywhere"])
        self.assertFalse(report["all_bound_complete"])
        self.assertEqual(report["complete_relation_count"], len(names))
        self.assertTrue(report["relations"][names[-1]]["bound"])
        self.assertFalse(report["relations"][names[0]]["bound"])
        self.assertEqual(report["bound_run_outcomes"], {"captured": 2})

    def test_the_budget_is_spent_on_bound_relations_before_the_rest(self):
        names = sorted(self.fake.catalog)
        bound = [names[-1]]
        clock = {"now": 0.0}

        def fetch(url, headers, *, max_bytes):
            if urlsplit(url).path.endswith("/rows"):
                clock["now"] += 10.0  # every page costs 10 s
            return self._fetch(url, headers, max_bytes=max_bytes)

        client = WorkbenchReadApiClient(BASE, KEY, fetch=fetch)
        report = mm.ordered_full_coverage(client, self.path, resolve_sample=0, bound=bound,
                                          budget_seconds=25.0, retry_delay_seconds=0,
                                          sleep=lambda _: None,
                                          monotonic=lambda: clock["now"])
        # Alphabetical order would have spent the budget on the first relations.
        self.assertEqual(self.first_row_request()[0], names[-1])
        self.assertTrue(report["all_bound_complete"])
        self.assertEqual(report["complete_bound_relation_count"], 1)
        self.assertFalse(report["all_served_complete"])
        self.assertLess(report["complete_relation_count"], len(names))
        self.assertIn("budget_exhausted", report["run_outcomes"])
        self.assertEqual(report["bound_run_outcomes"], {"captured": 1})

    def test_full_coverage_spec_is_daily_and_gates_on_the_bound_denominator(self):
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.full_coverage.json")
                          .read_text())
        self.assertEqual(lv.validate_spec(spec), [])
        self.assertEqual(spec["cadence"], "daily")
        self.assertGreater(spec["timeout_seconds"], lv.MAX_TIMEOUT)
        budget = float(spec["command"][spec["command"].index("--budget-seconds") + 1])
        self.assertLess(budget, spec["timeout_seconds"])
        expect = spec["expect"]
        self.assertTrue(expect["json_equals"]["all_bound_complete"])
        self.assertEqual(expect["json_equals"]["key_only_row_data_check.leaked"], 0)
        self.assertEqual(expect["json_min"]["bound_declared_total"], 50)
        # The 93-relation figures are recorded, never gated.
        for field in ("all_stable_served_complete", "accounted_equals_catalog",
                      "accounted_relation_count"):
            self.assertIn(field, spec["record"])
        self.assertIn(lv.DAILY_REPORT_ARG, spec["command"])

    def test_unstable_relations_are_never_staged_and_leave_the_denominator(self):
        names = sorted(self.fake.catalog)
        unstable, bound = names[0], [names[0], names[-1]]
        requests: list[tuple[str, list[str]]] = []

        def fetch(url, headers, *, max_bytes):
            parts = urlsplit(url)
            if parts.path.endswith("/rows"):
                requests.append((parts.path.split("/")[3],
                                 parse_qs(parts.query)["columns"][0].split(",")))
            return self._fetch(url, headers, max_bytes=max_bytes)

        client = WorkbenchReadApiClient(BASE, KEY, fetch=fetch)
        report = mm.ordered_full_coverage(client, self.path, resolve_sample=10, bound=bound,
                                          unstable=[unstable], retry_delay_seconds=0,
                                          sleep=lambda _: None)
        key = self.fake.catalog[unstable]["key"]
        read = [columns for name, columns in requests if name == unstable]
        self.assertTrue(read)
        self.assertTrue(all(sorted(columns) == sorted(key) for columns in read))  # key-only
        self.assertEqual(report["relations"][unstable]["rows_current"], 0)    # nothing staged
        self.assertEqual(report["relations"][unstable]["capture_mode"], "key_only")
        self.assertTrue(report["relations"][unstable]["key_only"]["complete"])
        block = report["completeness"]
        self.assertEqual((block["bound_declared_total"], block["bound_total"]), (2, 1))
        self.assertEqual(block["unstable_excluded"], [unstable])
        self.assertEqual((report["complete_bound_relation_count"], report["bound_total"]),
                         (1, 1))
        self.assertTrue(report["all_bound_complete"])
        self.assertEqual(report["completeness_rate"], 1.0)
        # The raw catalog figure is honest (the unstable relation is not mirrored) ...
        self.assertFalse(report["all_served_complete"])
        self.assertEqual(report["complete_relation_count"], len(names) - 1)
        # ... and every relation is still accounted for.
        self.assertTrue(report["all_stable_served_complete"])
        self.assertEqual(report["accounted_relation_count"], len(names))
        self.assertTrue(report["accounted_equals_catalog"])
        self.assertEqual(report["key_only_row_data_check"]["leaked"], 0)
        self.assertEqual(report["unstable_relations"], [unstable])
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.full_coverage.json")
                          .read_text())
        failures = lv.check_expectations(spec, 0, report)
        self.assertTrue(all(f.split(" ")[0] in spec["expect"]["json_min"] for f in failures),
                        failures)   # only the live-size floors (93 relations) fail here
        detail = lv._cap(lv.scrub_tree({"fields": lv._recorded(spec, report)}, []))
        self.assertEqual(detail["fields"]["unstable_relations"], [unstable])

    def test_the_probe_reads_unstable_relations_from_the_previous_daily_report(self):
        names = sorted(self.fake.catalog)
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "latest.json"
            daily.write_text(json.dumps(daily_report({
                "unstable_relations": [names[0], "not a relation"]})))
            self.assertEqual(mm.previous_unstable(daily), [names[0]])
            self.assertEqual(mm.previous_unstable(Path(tmp) / "absent.json"), [])
            from scripts import live_read_probes as probes
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = probes.main(["mirror-full-coverage", "--db", str(self.path),
                                    "--resolve-sample", "0",
                                    f"--previous-report={daily}"],
                                   environ={}, clients={"mirror-full-coverage": self.client})
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue())
        self.assertEqual(report["unstable_relations"], [names[0]])
        self.assertEqual(report["relations"][names[0]]["rows_current"], 0)


class RunawayCoverageTests(unittest.TestCase):
    """The probe reads in parallel, a row-bound
    or HTTP 500 relation never blocks the rest, every relation not captured is
    reported with its failure class and codes, and the next run syncs row-bound
    relations last while they stay in the completeness denominator."""

    RUNAWAY, BROKEN = "public.app_runaway", "sage.oe_broken"

    def setUp(self) -> None:
        from integrations.tests.test_source_mirror import KEY as FAKE_KEY
        from integrations.tests.test_source_mirror import RunawayFake, _numbered
        self.fake = RunawayFake()
        self.client = WorkbenchReadApiClient(
            BASE, KEY, fetch=lambda url, headers, *, max_bytes: self.fake(
                url, {"Authorization": "Bearer " + FAKE_KEY}, max_bytes=max_bytes))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mirror.sqlite"
        _numbered(self.fake, self.RUNAWAY, 3)
        self.fake.reinsert[self.RUNAWAY] = 4       # fresh ids on every page (ETL cuid())
        _numbered(self.fake, self.BROKEN, 3)
        self.fake.server_errors[self.BROKEN] = ("internal", None)
        self.bound = [self.RUNAWAY, self.BROKEN, "public.customer"]

    def coverage(self, **kwargs):
        kwargs.setdefault("bound", self.bound)
        return mm.ordered_full_coverage(self.client, self.path, resolve_sample=10,
                                        retry_delay_seconds=0, sleep=lambda _: None,
                                        page_size=4, min_page_size=2,
                                        max_rows_per_relation=12, **kwargs)

    def test_failures_are_isolated_classified_and_recorded_per_relation(self):
        report = self.coverage(workers=2)
        self.assertEqual(report["workers"], 2)
        outcomes = {item["relation"]: item for item in report["relation_outcomes"]}
        self.assertEqual(set(outcomes), {self.RUNAWAY, self.BROKEN})
        runaway, broken = outcomes[self.RUNAWAY], outcomes[self.BROKEN]
        self.assertEqual((runaway["outcome"], runaway["failure_class"]), ("failed", "row_bound"))
        self.assertEqual(runaway["rows_received"], 12)
        self.assertTrue(runaway["bound"])
        self.assertEqual((broken["failure_class"], broken["http_status"],
                          broken["upstream_error_code"]), ("source_defect", 500, "internal"))
        self.assertEqual(broken["page_size_final"], 2)
        self.assertEqual(report["failure_classes"], {"row_bound": 1, "source_defect": 1})
        # Every other relation is captured; the two failures are bound and incomplete.
        self.assertEqual(report["run_outcomes"],
                         {"captured": len(self.fake.catalog) - 2, "failed": 2})
        self.assertEqual((report["complete_bound_relation_count"], report["bound_total"]),
                         (1, 3))
        self.assertEqual(report["bound_incomplete"], sorted([self.RUNAWAY, self.BROKEN]))
        self.assertEqual(report["defer_next_run"], [self.RUNAWAY])
        self.assertEqual(len(report["slowest_relations"]),
                         min(mm.SLOWEST, len(self.fake.catalog) - 2))
        seconds = [item["seconds"] for item in report["slowest_relations"]]
        self.assertEqual(seconds, sorted(seconds, reverse=True))
        # The verifier records the per-relation evidence compactly, with no row value.
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.full_coverage.json")
                          .read_text())
        self.assertEqual(lv.validate_spec(spec), [])
        self.assertIn("--workers", spec["command"])
        fields = lv._cap(lv.scrub_tree({"fields": lv._recorded(spec, report)}, []))["fields"]
        self.assertEqual(fields["relation_outcomes[*].relation"],
                         [item["relation"] for item in report["relation_outcomes"]])
        self.assertEqual(fields["relation_outcomes[*].failure_class"],
                         [item["failure_class"] for item in report["relation_outcomes"]])
        self.assertIn("internal", fields["relation_outcomes[*].upstream_error_code"])
        self.assertEqual(fields["failure_classes"], {"row_bound": 1, "source_defect": 1})
        self.assertEqual(fields["defer_next_run"], [self.RUNAWAY])
        text = json.dumps(fields)
        for row in self.fake.tables["public.customer"]:
            self.assertNotIn(row["name"], text)

    def test_a_bound_relation_the_source_does_not_serve_keeps_the_run_incomplete_and_is_named(self):
        # Failing first: a bound relation the catalog marks unavailable is not complete, so
        # all_bound_complete stays false and the run names it as a typed relation_unavailable
        # part; a bound relation incomplete for any other reason is never typed that way,
        # and the check is blocked only while every missing relation is typed.
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.full_coverage.json")
                          .read_text())
        live = {"relation_count": 93, "catalog_relation_count": 93, "handles_resolved": 5000,
                "bound_declared_total": 63, "soft_delete_relation_count": 1}
        down = "public.customer"
        self.fake.catalog[down]["unavailable"] = "not replicated"
        report = self.coverage(bound=[down, self.BROKEN], workers=2)
        self.assertFalse(report["all_bound_complete"])
        self.assertIn(down, report["bound_incomplete"])
        self.assertEqual([p["part"] for p in report["blocked_upstream"]], [down])
        self.assertEqual(report["blocked_upstream"][0]["cause"], "relation_unavailable")
        self.assertEqual(report["bound_incomplete_untyped"], [self.BROKEN])
        full = {**report, **live, "accounted_relation_count": 88}
        # The untyped incomplete relation, and only it, keeps the check failing.
        failures = lv.check_expectations(spec, 0, full, spec["expect_blocked"])
        self.assertTrue(failures)
        self.assertEqual([f for f in failures if "bound_incomplete_untyped" not in f], [],
                         failures)
        # Only typed unavailable relations missing: blocked, never a pass.
        only = self.coverage(bound=[down], workers=2)
        self.assertFalse(only["all_bound_complete"])
        self.assertEqual(only["bound_incomplete_untyped"], [])
        only_full = {**only, **live, "accounted_relation_count": 88}
        self.assertTrue(lv.check_expectations(spec, 0, only_full))   # the full expect fails
        self.assertEqual(lv.check_expectations(spec, 0, only_full, spec["expect_blocked"]),
                         [])
        # A fully served bound set carries no blocked part.
        served = [n for n in sorted(self.fake.catalog)
                  if n not in (down, self.RUNAWAY, self.BROKEN)][0]
        done = self.coverage(bound=[served], workers=2)
        self.assertTrue(done["all_bound_complete"])
        self.assertEqual(done["blocked_upstream"], [])

    def test_the_check_passes_on_all_bound_complete_whatever_the_catalog_figures(self):
        # Pass =
        # complete_bound_relation_count == bound_total; the 93-relation figures are fields.
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.full_coverage.json")
                          .read_text())
        # The live sizes the fixture cannot reach (93 relations, 5,000 handles, ...).
        live = {"relation_count": 93, "catalog_relation_count": 93, "handles_resolved": 5000,
                "bound_declared_total": 63, "soft_delete_relation_count": 1}

        def verdict(report):
            report = {**report, **live, "accounted_relation_count": 88}
            return report, lv.check_expectations(spec, 0, report)

        # Both failing relations are unbound: every bound relation completes.
        report, failures = verdict(self.coverage(bound=["public.customer"], workers=2))
        self.assertEqual(failures, [])
        self.assertEqual((report["complete_bound_relation_count"], report["bound_total"]),
                         (1, 1))
        self.assertFalse(report["all_stable_served_complete"])
        self.assertFalse(report["accounted_equals_catalog"])
        fields = lv._recorded(spec, report)
        self.assertEqual((fields["accounted_relation_count"],
                          fields["catalog_relation_count"]), (88, 93))
        self.assertEqual(fields["complete_bound_relation_count"], 1)
        # One bound relation incomplete fails the check on the bound figure alone.
        _, failures = verdict(self.coverage(bound=["public.customer", self.BROKEN],
                                            workers=2))
        self.assertEqual(failures, ["all_bound_complete != expected"])
        # A data_truth_defect relation leaves the denominator, so it cannot fail it.
        _, failures = verdict(self.coverage(bound=["public.customer", self.BROKEN],
                                            unstable=[self.BROKEN], workers=2))
        self.assertEqual(failures, [])

    def test_a_closed_window_source_defect_is_deferred_and_stays_counted(self):
        # 4+ rows time out, fewer end inside an equal-key run: no size reads the cursor.
        self.fake.timeout_from[self.BROKEN] = 4
        self.fake.server_errors[self.BROKEN] = ("key-run-too-long", 4)
        report = self.coverage(workers=2)
        broken = {item["relation"]: item for item in report["relation_outcomes"]}[self.BROKEN]
        self.assertEqual(broken["failure_class"], "source_defect")
        self.assertEqual(report["defer_next_run"], sorted([self.RUNAWAY, self.BROKEN]))
        # Deferred, not excluded: it stays in the denominator, incomplete.
        self.assertEqual((report["complete_bound_relation_count"], report["bound_total"]),
                         (1, 3))
        self.assertIn(self.BROKEN, report["bound_incomplete"])

    def test_sync_key_only_and_deferred_reads_are_timed_separately(self):
        from integrations.tests.test_source_mirror import _numbered
        unstable = "silver.unstable_view"
        _numbered(self.fake, unstable, 5)
        del self.fake.server_errors[self.BROKEN]
        # One second per row request, so each timing counts the requests of its phase.
        report = self.coverage(unstable=[unstable], deferred=[self.BROKEN],
                               monotonic=lambda: float(len(self.fake.requests)))
        per = {}
        for name, _, _ in self.fake.requests:
            per[name] = per.get(name, 0) + 1
        self.assertEqual(report["key_only_seconds"], per[unstable])
        self.assertEqual(report["deferred_sync_seconds"], per[self.BROKEN])
        self.assertEqual(report["sync_seconds"],
                         len(self.fake.requests) - per[unstable] - per[self.BROKEN])
        self.assertGreater(report["sync_seconds"], 0)

    def test_the_next_run_syncs_deferred_relations_last_and_keeps_them_counted(self):
        first = self.coverage()
        unstable = "silver.shipments"
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "latest.json"
            daily.write_text(json.dumps(daily_report(
                {"unstable_relations": [unstable],
                 "relations[*].relation": [self.BROKEN, unstable],
                 "relations[*].exceeds_mirror_bound": [True, False]},
                {"defer_next_run": first["defer_next_run"] + ["not a relation"]})))
            self.assertEqual(mm.previous_deferred(daily), sorted([self.RUNAWAY, self.BROKEN]))
            self.assertEqual(mm.previous_deferred(Path(tmp) / "absent.json"), [])
            self.fake.requests.clear()
            self.path.unlink()
            errors = self.fake.server_errors.pop(self.BROKEN)  # the CLI sleeps for real
            from scripts import live_read_probes as probes
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = probes.main(["mirror-full-coverage", "--db", str(self.path),
                                    "--resolve-sample", "0", "--workers", "2",
                                    f"--previous-report={daily}"],
                                   environ={}, clients={"mirror-full-coverage": self.client})
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue())
        order = list(dict.fromkeys(name for name, _, _ in self.fake.requests))
        # Every other relation, the unstable key-only read, then the deferred ones.
        self.assertEqual(set(order[-2:]), {self.RUNAWAY, self.BROKEN})
        self.assertEqual(order[-3], unstable)
        self.assertEqual(report["deferred_relations"], sorted([self.RUNAWAY, self.BROKEN]))
        self.assertEqual(report["workers"], 2)
        # Captured inside the CLI's own (default, larger) bound: no longer deferred.
        self.assertTrue(report["relations"][self.RUNAWAY]["deferred"])
        self.assertTrue(report["relations"][self.RUNAWAY]["complete"])
        self.assertEqual(report["defer_next_run"], [])
        # Deferred is not excused: both stay in the denominator, still incomplete.
        self.fake.server_errors[self.BROKEN] = errors
        self.path.unlink()
        report = self.coverage(deferred=[self.RUNAWAY, self.BROKEN])
        self.assertEqual((report["complete_bound_relation_count"], report["bound_total"]),
                         (1, 3))
        self.assertEqual(report["bound_incomplete"], sorted([self.RUNAWAY, self.BROKEN]))
        self.assertTrue(all(item["deferred"] for item in report["relation_outcomes"]))
        self.assertEqual(report["defer_next_run"], sorted([self.RUNAWAY, self.BROKEN]))
        # Once the source is repaired a deferred relation is captured and leaves the list.
        del self.fake.reinsert[self.RUNAWAY]
        self.fake.tables[self.RUNAWAY] = self.fake.tables[self.RUNAWAY][:5]
        report = self.coverage(deferred=[self.RUNAWAY])
        self.assertEqual(report["defer_next_run"], [])
        self.assertTrue(report["relations"][self.RUNAWAY]["complete"])

    def test_workers_flag_belongs_to_the_coverage_probe_only(self):
        from scripts import live_read_probes as probes
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            probes.main(["portal-health", "--workers", "2"], environ={})



class RotationTests(CounterFixture, unittest.TestCase):
    """Relations too large for an equal share complete in some run, carried over a window;
    every run reads the previous one as the verifier records it."""

    def rotate(self, names, path, day, daily=None):
        """One daily run ``day`` days after 2026-10-02T03:00Z: 10 s per page of 100 keys,
        a 130 s budget (one 1,000-key relation and its re-read), ``daily`` the daily
        deadline, the previous report."""
        from datetime import datetime, timedelta, timezone
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.relation_counts.json")
                          .read_text())
        clock = {"now": 0.0}

        def fetch(url, headers, *, max_bytes):
            if urlsplit(url).path.endswith("/rows"):
                clock["now"] += 10.0
            return self.fake(url, headers, max_bytes=max_bytes)

        at = datetime(2026, 10, 2, 3, tzinfo=timezone.utc) + timedelta(days=day)
        report = mm.relation_counts(
            WorkbenchReadApiClient(BASE, KEY, fetch=fetch), names, budget_seconds=130,
            daily_budget_seconds=daily, page_size=100, min_page_size=50, mirror_bound=1_000_000, sample_keys=100,
            previous_report=path if path.exists() else None, sleep=self.sleeps.append,
            monotonic=lambda: clock["now"], now=lambda: at)
        self.assertLessEqual(clock["now"], daily or 130.0)
        self.assertEqual(report["row_data_check"]["leaked"], 0)
        fields = lv._cap(lv.scrub_tree({"fields": lv._recorded(spec, report)}, []))["fields"]
        path.write_text(json.dumps(daily_report(fields, stamp=at.strftime(
            "%Y-%m-%dT%H:%M:%SZ"))))
        return report, {name: self.item(report, name) for name in names}

    def test_two_runs_complete_relations_an_equal_share_never_finishes(self):
        # N = 2,000 keys; a run fits N/2 (an equal share fits a quarter, so never ends).
        self.fake.add("public.app_status", [cuid(i) for i in range(1000)])
        self.fake.add("public.app_task", [cuid(5000 + i) for i in range(1000)])
        names = ["public.app_status", "public.app_task"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            report, item = self.rotate(names, path, day=0)
            done, cut = item["public.app_status"], item["public.app_task"]
            self.assertEqual((done["complete"], done["distinct_keys"]), (True, 1000))
            # A run cut by the budget stays a lower bound, in the run and in the window.
            self.assertEqual((cut["complete"], cut["stop"], cut["verdict"],
                              cut["classification"], cut["complete_in_window"]),
                             (False, "budget", "count_lower_bound", "inconclusive", False))
            self.assertEqual((report["complete_count"], report["complete_this_run_count"]),
                             (1, 1))

            report, item = self.rotate(names, path, day=1)
            carried, done = item["public.app_status"], item["public.app_task"]
            self.assertEqual((done["rotation_rank"], done["complete"], done["distinct_keys"],
                              done["completed_at"]), (0, True, 1000, "2026-10-03T03:00:00Z"))
            # Not read this run: never relabelled complete, but its completing run is named.
            self.assertEqual((carried["complete"], carried["stop"], carried["verdict"],
                              carried["classification"]),
                             (False, "rotation", "not_reached", "inconclusive"))
            self.assertEqual((carried["complete_in_window"], carried["completed_at"],
                              carried["completed_distinct_keys"]),
                             (True, "2026-10-02T03:00:00Z", 1000))
            self.assertEqual((report["complete_count"], report["complete_this_run_count"]),
                             (2, 1))
            recorded = json.loads(path.read_text())["checks"][0]["detail"]["fields"]
            self.assertEqual(recorded["relations[*].completed_at"],
                             ["2026-10-02T03:00:00Z", "2026-10-03T03:00:00Z"])

            # Five days later the run-2 completion is past the 96 h window.
            report, item = self.rotate(names, path, day=6)
            self.assertTrue(item["public.app_status"]["complete"])
            self.assertEqual((item["public.app_task"]["complete_in_window"],
                              report["complete_count"]), (False, 1))

    def test_an_unserved_relation_is_never_rotated_out_or_carried_complete(self):
        self.fake.add("public.app_status", [cuid(i) for i in range(1000)])
        self.fake.add("public.app_task", [cuid(5000 + i) for i in range(1000)])
        names = ["public.app_status", "public.app_task"]
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.relation_counts.json")
                          .read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            self.assertTrue(self.rotate(names, path, day=0)[1]["public.app_status"]["complete"])
            # Completed yesterday, gone from the catalog today while it would rotate out.
            del self.fake.catalog["public.app_status"]
            report, item = self.rotate(names, path, day=1)
            gone = item["public.app_status"]
            self.assertEqual((gone["verdict"], gone["classification"], gone["complete_in_window"]),
                             ("not_served", "upstream_unavailable", False))
            self.assertEqual((report["upstream_unavailable_count"], report["complete_count"]),
                             (1, 1))
            self.assertIn("upstream_unavailable_count != expected",
                          lv.check_expectations(spec, 0, report))

    def test_a_relation_no_run_fits_stays_a_lower_bound_and_yields_the_first_turn(self):
        self.fake.add("public.app_huge", [cuid(i) for i in range(3000)])
        self.fake.add("public.app_small", [cuid(5000 + i) for i in range(500)])
        names = ["public.app_huge", "public.app_small"]
        verdicts = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            for day in range(3):
                report, item = self.rotate(names, path, day=day)
                huge, small = item["public.app_huge"], item["public.app_small"]
                verdicts.append((huge["verdict"], small["verdict"]))
                self.assertFalse(huge["complete_in_window"])
                self.assertEqual(report["complete_count"], int(small["complete_in_window"]))
        # Day 1: the huge relation had the first turn and was cut, so the small one goes
        # first; day 2 the huge one has it again and the small one stays complete.
        self.assertEqual(verdicts, [("count_lower_bound", "not_reached"),
                                    ("not_reached", "stable_within_bound"),
                                    ("count_lower_bound", "not_reached")])
        self.assertTrue(small["complete_in_window"])

    def test_a_relation_one_run_cannot_hold_gets_the_daily_deadline_and_completes(self):
        # 2,000 keys need 210 s: over the 130 s run budget, inside the 300 s daily deadline.
        self.fake.add("public.app_big", [cuid(i) for i in range(2000)])
        self.fake.add("public.app_small", [cuid(5000 + i) for i in range(500)])
        names = ["public.app_big", "public.app_small"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            _, item = self.rotate(names, path, day=0, daily=300)
            big = item["public.app_big"]
            # No previous report: the run budget, cut with the first turn.
            self.assertEqual((big["budget_extended"], big["stop"], big["rotation_rank"],
                              big["exceeds_daily_bound"]), (False, "budget", 0, False))
            report, item = self.rotate(names, path, day=1, daily=300)
            big, small = item["public.app_big"], item["public.app_small"]
            self.assertTrue(small["complete"] and not small["budget_extended"])
            self.assertEqual((big["budget_extended"], big["complete"], big["distinct_keys"],
                              big["exceeds_daily_bound"]), (True, True, 2000, False))
            self.assertGreater(big["need_seconds"], 130)
            self.assertEqual((report["complete_count"], report["exceeds_daily_bound"]), (2, []))
            # Its recorded need over the run budget keeps the daily deadline next run.
            _, item = self.rotate(names, path, day=2, daily=300)
            self.assertEqual((item["public.app_big"]["budget_extended"],
                              item["public.app_big"]["complete"]), (True, True))
        for bad in (float("nan"), float("inf"), -1.0):
            with self.assertRaises(ValueError):
                self.count(names, budget_seconds=130, daily_budget_seconds=bad)

    def test_a_relation_cut_then_not_reached_keeps_the_daily_deadline_and_completes(self):
        # The live shape (public.app_production_task, gap-32a08b06dfaf): 1,500 keys need
        # 170 s, over the 130 s run budget and inside the 300 s daily deadline, beside five
        # small relations that fill what the daily deadline leaves after them. Day 0 the
        # budget cuts it with the first turn; day 1 it yields and gets the daily deadline,
        # but too little is left, so it is not reached. Its cut need is a lower bound no
        # larger than the run budget, so day 2 it must keep the daily deadline, not
        # take the first turn with the run budget and be cut again every second run.
        self.fake.add("public.app_task", [cuid(i) for i in range(1500)])
        small = [f"public.app_s{i}" for i in range(5)]
        for i, (name, size) in enumerate(zip(small, (130, 100, 50, 40, 10))):
            self.fake.add(name, [cuid(5000 + 1000 * i + j) for j in range(size)])
        names = ["public.app_task", *small]
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            for day in range(6):
                report, item = self.rotate(names, path, day=day, daily=250)
                task = item["public.app_task"]
                seen.append((task["rotation_rank"], task["stop"], task["budget_extended"]))
                if day >= 2:
                    self.assertEqual(report["complete_count"], len(names), day)
            self.assertEqual(seen[:3], [(0, "budget", False), (5, "rotation", True),
                                        (0, "end", True)])
            self.assertEqual((task["exceeds_daily_bound"], report["exceeds_daily_bound"]),
                             (False, []))

    def test_a_relation_past_the_daily_deadline_stays_a_lower_bound_named_exceeding(self):
        self.fake.add("public.app_huge", [cuid(i) for i in range(5000)])
        self.fake.add("public.app_small", [cuid(9000 + i) for i in range(500)])
        names = ["public.app_huge", "public.app_small"]
        flags = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            for day in range(4):
                report, item = self.rotate(names, path, day=day, daily=300)
                huge = item["public.app_huge"]
                flags.append((huge["rotation_rank"], huge["stop"], huge["budget_extended"],
                              huge["exceeds_daily_bound"]))
                self.assertFalse(huge["complete_in_window"])
            # Day 2 it had the first turn and the whole daily deadline and was still cut;
            # day 3 it is not reached and stays named.
            self.assertEqual(flags, [(0, "budget", False, False), (1, "budget", True, False),
                                     (0, "budget", True, True), (1, "rotation", True, True)])
            self.assertEqual((huge["verdict"], report["exceeds_daily_bound"],
                              report["exceeds_daily_bound_count"]),
                             ("not_reached", ["public.app_huge"], 1))
            detail = json.loads(path.read_text())["checks"][0]["detail"]
            self.assertEqual(detail["fields"]["exceeds_daily_bound"], ["public.app_huge"])


def daily_report(counter_fields: dict, coverage_fields: dict | None = None,
                 stamp: str = "2026-10-02T03:00:00Z") -> dict:
    checks = [{"id": "mirror.relation_counts", "result": "fail",
               "detail": {"fields": counter_fields}}]
    if coverage_fields is not None:
        checks.append({"id": "mirror.full_coverage", "result": "pass",
                       "detail": {"fields": coverage_fields}})
    return {"schema": "plx-live-verify/v1", "cadence": "daily", "generated_at": stamp,
            "main_sha": "a" * 40, "checks": checks}


class TwoRunRuleTests(unittest.TestCase):
    def test_two_consecutive_defect_runs_make_a_relation_unstable(self):
        once = mm.stability_streaks("data_truth_defect")
        self.assertEqual(once, {"defect_streak": 1, "stable_streak": 0, "unstable": False})
        twice = mm.stability_streaks("data_truth_defect",
                                     previous_defect_streak=once["defect_streak"],
                                     previous_stable_streak=once["stable_streak"])
        self.assertTrue(twice["unstable"])
        # A report from before the streaks existed counts its classification once.
        self.assertTrue(mm.stability_streaks(
            "data_truth_defect", previous_classification="data_truth_defect")["unstable"])

    def test_a_stable_run_in_between_breaks_the_streak(self):
        broken = mm.stability_streaks("ok", previous_defect_streak=1, previous_stable_streak=0)
        self.assertEqual(broken["defect_streak"], 0)
        again = mm.stability_streaks("data_truth_defect", previous_defect_streak=0,
                                     previous_stable_streak=broken["stable_streak"])
        self.assertFalse(again["unstable"])

    def test_an_inconclusive_run_carries_the_streak_without_evidence(self):
        held = mm.stability_streaks("inconclusive", previous_defect_streak=1,
                                    previous_stable_streak=0)
        self.assertEqual((held["defect_streak"], held["unstable"]), (1, False))
        self.assertTrue(mm.stability_streaks("data_truth_defect", previous_defect_streak=1,
                                             previous_stable_streak=0)["unstable"])

    def test_unstable_is_sticky_until_two_consecutive_stable_runs(self):
        state = {"defect_streak": 2, "stable_streak": 0, "unstable": True}
        for classification, unstable in (("ok", True), ("upstream_unavailable", True),
                                         ("mirror_bound", False)):
            state = mm.stability_streaks(classification,
                                         previous_defect_streak=state["defect_streak"],
                                         previous_stable_streak=state["stable_streak"],
                                         previously_unstable=state["unstable"])
            self.assertEqual(state["unstable"], unstable, classification)


class TwoRunCounterTests(CounterFixture, unittest.TestCase):
    """The rule carried through the counter and the verifier's recorded fields."""

    def reinsert_each_run(self, name: str) -> None:
        """An ETL that re-inserts the first rows under fresh ids between two reads."""
        reads = {"n": 0}

        def on_page(fake, relation, cursor):
            if relation == name and cursor is None:
                reads["n"] += 1
                if reads["n"] % 2 == 0:   # the second read of each run
                    rows = sorted(fake.tables[name], key=fake.sort_key)
                    fake.tables[name] = [{**row, "id": cuid(10_000 * reads["n"] + i, "d")}
                                         for i, row in enumerate(rows[:300])] + rows[300:]
        self.fake.on_page = on_page

    def run_with(self, names, path, day=0, hours_apart=24):
        """One daily run ``day`` days after 2026-10-02T03:00Z (reading the previous run's
        report, written ``hours_apart`` earlier), then this run's report written in its place."""
        from datetime import datetime, timedelta, timezone
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mirror.relation_counts.json")
                          .read_text())
        at = datetime(2026, 10, 2, 3, tzinfo=timezone.utc) + timedelta(hours=24 * day)
        if path.exists():   # restamp the previous report hours_apart before this run
            previous = json.loads(path.read_text())
            previous["generated_at"] = (at - timedelta(hours=hours_apart)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            path.write_text(json.dumps(previous))
        report = self.count(names, sample_keys=500, previous_report=path, now=lambda: at)
        self.assertEqual("new_data_truth_defect_count != expected" in
                         lv.check_expectations(spec, 0, report),
                         report["new_data_truth_defect_count"] > 0)
        fields = lv._cap(lv.scrub_tree({"fields": lv._recorded(spec, report)}, []))["fields"]
        path.write_text(json.dumps(daily_report(fields, stamp=at.strftime(
            "%Y-%m-%dT%H:%M:%SZ"))))
        return report, fields

    def test_known_unstable_relations_stop_failing_the_check(self):
        self.fake.add("public.app_child", [cuid(i) for i in range(1000)])
        self.reinsert_each_run("public.app_child")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            first, _ = self.run_with(["public.app_child"], path, day=0)
            second, _ = self.run_with(["public.app_child"], path, day=1)
            third, fields = self.run_with(["public.app_child"], path, day=2)
        # Day 1 and 2: new (not yet unstable in the previous report); day 3: known.
        self.assertEqual([r["new_data_truth_defect_count"] for r in (first, second, third)],
                         [1, 1, 0])
        self.assertEqual(second["new_data_truth_defects"], ["public.app_child"])
        self.assertEqual(third["data_truth_defect_count"], 1)
        self.assertEqual(third["unstable_relations"], ["public.app_child"])
        self.assertEqual(fields["relations[*].defect_streak"], [3])
        self.assertTrue(third["previous_report_consecutive"])
        self.assertEqual(third["previous_report_age_hours"], 24.0)

    def test_runs_more_than_36_hours_apart_are_not_consecutive(self):
        self.fake.add("public.app_child", [cuid(i) for i in range(1000)])
        self.reinsert_each_run("public.app_child")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            self.run_with(["public.app_child"], path, day=0)
            late, fields = self.run_with(["public.app_child"], path, day=2, hours_apart=48)
            self.assertFalse(late["previous_report_consecutive"])
            self.assertEqual(late["previous_report_age_hours"], 48.0)
            self.assertEqual(fields["relations[*].defect_streak"], [1])   # restarted
            self.assertEqual(late["unstable_relations"], [])
            # The boundary: 36 h still continues the streak.
            on_time, _ = self.run_with(["public.app_child"], path, day=3, hours_apart=36)
            self.assertTrue(on_time["previous_report_consecutive"])
            self.assertEqual(on_time["unstable_relations"], ["public.app_child"])

    def test_a_carried_counter_check_ages_from_its_own_run_not_the_report_stamp(self):
        """results/daily/latest.json is rewritten as a daily set runs: it carries the
        counter's last check (run 48 h ago) under a fresh ``generated_at``."""
        from datetime import datetime, timedelta, timezone
        self.fake.add("public.app_child", [cuid(i) for i in range(1000)])
        self.reinsert_each_run("public.app_child")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            self.run_with(["public.app_child"], path, day=0)
            carried = json.loads(path.read_text())
            ran = datetime(2026, 10, 2, 3, tzinfo=timezone.utc)
            carried["checks"][0].update(main_sha="a" * 40,
                                        checked_at=ran.strftime("%Y-%m-%dT%H:%M:%SZ"))
            path.write_text(json.dumps(carried))
            late, fields = self.run_with(["public.app_child"], path, day=2, hours_apart=0)
        self.assertEqual((late["previous_report_age_hours"],
                          late["previous_report_consecutive"]), (48.0, False))
        self.assertEqual(fields["relations[*].defect_streak"], [1])   # restarted
        self.assertEqual(late["unstable_relations"], [])

    def test_small_clock_skew_still_continues_a_streak(self):
        self.fake.add("public.app_child", [cuid(i) for i in range(1000)])
        self.reinsert_each_run("public.app_child")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            self.run_with(["public.app_child"], path, day=0)
            # previous report stamped 3 minutes in the future: still consecutive
            skewed, fields = self.run_with(["public.app_child"], path, day=1,
                                           hours_apart=-0.05)
            self.assertTrue(skewed["previous_report_consecutive"])
            self.assertEqual(fields["relations[*].defect_streak"], [2])
            # an hour ahead is a broken clock, not skew: the streak restarts
            far, fields = self.run_with(["public.app_child"], path, day=2, hours_apart=-1)
            self.assertFalse(far["previous_report_consecutive"])
            self.assertEqual(fields["relations[*].defect_streak"], [1])

    def test_a_defect_on_a_relation_unstable_over_fourteen_days_counts_as_new_once(self):
        from datetime import datetime, timezone
        self.fake.add("public.app_child", [cuid(i) for i in range(1000)])
        self.reinsert_each_run("public.app_child")
        long_streak = mm.UNSTABLE_RUNS + mm.REREAISE_DAYS
        now = datetime(2026, 10, 2, 3, tzinfo=timezone.utc)

        def previous(streak, path):
            path.write_text(json.dumps(daily_report(
                {"unstable_relations": ["public.app_child"],
                 "relations[*].relation": ["public.app_child"],
                 "relations[*].classification": ["data_truth_defect"],
                 "relations[*].defect_streak": [streak],
                 "relations[*].stable_streak": [0]}, stamp="2026-10-01T03:00:00Z")))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            previous(long_streak - 1, path)   # not yet 14 days: stays known
            quiet = self.count(["public.app_child"], sample_keys=500,
                               previous_report=path, now=lambda: now)
            self.assertEqual(quiet["new_data_truth_defect_count"], 0)
            self.assertFalse(self.item(quiet, "public.app_child")["reraised"])
            self.reinsert_each_run("public.app_child")
            previous(long_streak, path)
            again = self.count(["public.app_child"], sample_keys=500,
                               previous_report=path, now=lambda: now)
        self.assertEqual(again["new_data_truth_defects"], ["public.app_child"])
        item = self.item(again, "public.app_child")
        self.assertTrue(item["reraised"])
        self.assertTrue(item["unstable"])
        self.assertEqual(item["defect_streak"], mm.UNSTABLE_RUNS)   # the streak restarts
        self.assertEqual(mm.reraise_defect(
            "data_truth_defect", previously_unstable=True,
            previous_defect_streak=mm.UNSTABLE_RUNS), False)
        self.assertEqual(mm.reraise_defect(
            "ok", previously_unstable=True, previous_defect_streak=long_streak), False)

    def test_a_stale_report_keeps_its_unstable_relations(self):
        from datetime import datetime, timezone
        self.fake.add("public.app_stable", [cuid(i) for i in range(50)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            path.write_text(json.dumps(daily_report(
                {"unstable_relations": ["public.app_stable"],
                 "relations[*].relation": ["public.app_stable"],
                 "relations[*].stable_streak": [1]}, stamp="2026-09-01T03:00:00Z")))
            report = self.count(["public.app_stable"], previous_report=path,
                                now=lambda: datetime(2026, 10, 2, 3, tzinfo=timezone.utc))
        self.assertFalse(report["previous_report_consecutive"])
        # One stable run after a stale report is not two consecutive stable runs.
        self.assertEqual(report["unstable_relations"], ["public.app_stable"])

    def test_two_daily_runs_of_reinserted_ids_name_the_relation_unstable(self):
        self.fake.add("public.app_child", [cuid(i) for i in range(1000)])
        self.fake.add("public.app_stable", [cuid(5000 + i) for i in range(400)])
        self.reinsert_each_run("public.app_child")
        names = ["public.app_child", "public.app_stable"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            first, fields = self.run_with(names, path)
            self.assertEqual(self.item(first, "public.app_child")["classification"],
                             "data_truth_defect")
            self.assertEqual(first["unstable_relations"], [])
            self.assertFalse(first["previous_report_read"])
            self.assertEqual(fields["relations[*].defect_streak"], [1, 0])
            second, fields = self.run_with(names, path)
            self.assertTrue(second["previous_report_read"])
            self.assertEqual(second["unstable_relations"], ["public.app_child"])
            self.assertEqual(fields["unstable_relations"], ["public.app_child"])
            self.assertEqual(fields["relations[*].unstable"], [True, False])
            self.assertEqual(mm.previous_unstable(path), ["public.app_child"])
            # The counter keeps measuring it; it stays unstable while ids keep changing.
            third, _ = self.run_with(names, path)
            self.assertEqual(third["unstable_relations"], ["public.app_child"])
            self.assertEqual(third["row_data_check"]["leaked"], 0)

    def test_an_unstable_relation_no_longer_measured_stays_unstable(self):
        self.fake.add("public.app_stable", [cuid(i) for i in range(50)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            path.write_text(json.dumps(daily_report(
                {"unstable_relations": ["public.app_gone"]})))
            report = self.count(["public.app_stable"], previous_report=path)
        self.assertEqual(report["unstable_relations"], ["public.app_gone"])


class CompletenessTests(unittest.TestCase):
    def test_the_denominator_is_the_bound_set_minus_unstable_relations(self):
        block = mm.completeness(["public.a", "public.x"],
                                bound=["public.a", "public.b", "public.c"],
                                unstable=["public.c", "public.x"])
        self.assertEqual(block["metric"], "complete_bound_relation_count/bound_total")
        self.assertEqual((block["bound_declared_total"], block["bound_total"],
                          block["complete_bound_relation_count"]), (3, 2, 1))
        self.assertEqual(block["completeness_rate"], 0.5)
        self.assertEqual(block["bound_incomplete"], ["public.b"])
        self.assertEqual(block["unstable_excluded"], ["public.c"])
        self.assertFalse(block["all_bound_complete"])
        self.assertEqual(mm.completeness_ratio(block), "1/2")
        empty = mm.completeness([], bound=[])
        self.assertEqual((empty["completeness_rate"], empty["all_bound_complete"]),
                         (None, False))

    def test_every_binding_of_an_unstable_relation_carries_the_gap(self):
        bindings = mm._schema_bindings()
        self.assertTrue(bindings)
        binding, relation = bindings[0]
        gaps = mm.unstable_gaps([relation])
        self.assertIn({"code": "capture_unstable_ids", "where": binding, "count": 1,
                       "samples": []}, gaps)
        self.assertEqual({g["where"] for g in gaps},
                         {b for b, r in bindings if r == relation})
        self.assertTrue({r for _, r in bindings} <= set(mm.bound_relations()) |
                        {r for _, r in bindings})
        self.assertEqual(mm.unstable_gaps(["public.not_bound_anywhere"]), [])


class UpstreamFindingTests(unittest.TestCase):
    def test_finding_is_counts_and_classifications_only(self):
        fields = {"mirror_max_rows_per_relation": 1_000_000,
                  "unstable_relations": ["public.app_child"],
                  "relations[*].relation": ["public.app_child", "public.app_ok"],
                  "relations[*].classification": ["data_truth_defect", "ok"],
                  "relations[*].verdict": ["ids_reinserted", "stable_within_bound"],
                  "relations[*].distinct_keys": [1_000_000, 400],
                  "relations[*].ratio_seen_in_both": [0.4, 1.0],
                  "relations[*].defect_streak": [2, 0],
                  "relations[*].first_keys_hash": ["0123456789abcdef", "fedcba9876543210"]}
        older = dict(fields, **{"relations[*].classification": ["data_truth_defect", "ok"]})
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.json", Path(tmp) / "latest.json"
            a.write_text(json.dumps(daily_report(older, stamp="2026-10-01T03:00:00Z")))
            b.write_text(json.dumps(daily_report(fields, {"complete_bound_relation_count": 50,
                                                          "bound_total": 55})))
            text = mm.upstream_findings([b, a, b])   # latest.json repeats a run: counted once
            out = Path(tmp) / "UPSTREAM-FINDINGS.md"
            self.assertEqual(mm.main(["upstream-findings", str(a), str(b),
                                      "--output", str(out)]), 0)
            self.assertEqual(out.read_text(), text)
        self.assertIn("daily runs read: 2", text)
        self.assertNotIn("0123456789abcdef", text)   # no hash, key or row value


class ConsistentReportTests(unittest.TestCase):
    """The projection probe and the eval report the same completeness block."""

    def test_projection_probe_skips_unstable_relations_and_gaps_their_bindings(self):
        from ontology.tests import test_projection as tp
        from scripts.ontology_projection_probe import projection_probe

        schema = tp.compiled_schema()
        bindings = sorted({(b["id"], b["relation"]) for t in schema.object_types.values()
                           for b in t["bindings"] if b.get("source") == "portal_api"})
        _, unstable = bindings[0]
        fake = tp.FakeWorkbench()
        requested: list[str] = []

        def fetch(url, headers, *, max_bytes):
            if urlsplit(url).path.endswith("/rows"):
                requested.append(urlsplit(url).path.split("/")[3])
            return fake(url, headers, max_bytes=max_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            client = WorkbenchReadApiClient(tp.BASE, tp.KEY, fetch=fetch)
            result = projection_probe(client, f"{tmp}/mirror.sqlite", f"{tmp}/store.sqlite",
                                      schema=schema, normalizer=tp.normalizer,
                                      claim_sample=50, unstable=[unstable],
                                      bound=sorted({r for _, r in bindings}))
        self.assertNotIn(unstable, requested)       # not even key-only here
        self.assertTrue(result["mirror_complete"])  # every stable bound relation
        expected = sorted(b for b, r in bindings if r == unstable)
        self.assertEqual(result["capture_unstable_bindings"], expected)
        self.assertEqual(result["gap_codes"]["capture_unstable_ids"], len(expected))
        self.assertEqual(result["completeness"]["metric"],
                         "complete_bound_relation_count/bound_total")
        self.assertEqual(result["completeness"]["unstable_relations"], [unstable])
        self.assertEqual(result["denied_leaks"], 0)
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "ontology.projection.json")
                          .read_text())
        self.assertEqual(lv.validate_spec(spec), [])
        self.assertIn(lv.DAILY_REPORT_ARG, spec["command"])
        fields = lv._recorded(spec, result)
        self.assertEqual(fields["completeness"]["unstable_relations"], [unstable])

    def test_eval_build_never_syncs_unstable_relations_and_reports_the_same_block(self):
        from evals.oracle.fixture_world import build_world
        from ontology.schema import load_schema
        from scripts import eval_unfamiliar_sample as ev

        relations = ev.required_relations(load_schema(), ev.root_relations())
        world = build_world()
        fetch = ev._fixture_fetch(world)
        requested: list[str] = []

        def spy(url, headers, *, max_bytes=0):
            if urlsplit(url).path.endswith("/rows"):
                requested.append(urlsplit(url).path.split("/")[3])
            return fetch(url, headers, max_bytes=max_bytes)

        client = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                        "plxwb_" + "f" * 40, fetch=spy)
        with tempfile.TemporaryDirectory() as tmp:
            plain = ev.build_projection(client, Path(tmp) / "a", relations,
                                        budget_seconds=600)[1]["completeness"]
            done = [name for name in relations if name in set(requested)]
            unstable = done[0]
            requested.clear()
            _, info, complete = ev.build_projection(client, Path(tmp) / "b", relations,
                                                    budget_seconds=600, unstable=[unstable])
        self.assertNotIn(unstable, requested)
        self.assertNotIn(unstable, complete)
        block = info["completeness"]
        self.assertEqual(block["scope"], "eval_required")
        self.assertEqual(block["bound_total"], plain["bound_total"] - 1)
        self.assertEqual(block["complete_bound_relation_count"],
                         plain["complete_bound_relation_count"] - 1)
        self.assertEqual(block["unstable_excluded"], [unstable])
        self.assertEqual(block, mm.completeness(complete, bound=relations,
                                                unstable=[unstable], scope="eval_required"))
        self.assertEqual(info["capture_unstable_bindings"],
                         [g["where"] for g in mm.unstable_gaps([unstable],
                                                               schema=load_schema())])
        self.assertTrue(info["capture_unstable_bindings"])
        state = ev.product_state(ev.Product(None, info))
        self.assertEqual(state["completeness"], mm.completeness_ratio(block))
        self.assertEqual(state["unstable_excluded"], 1)


class DailyReportPlaceholderTests(unittest.TestCase):
    def spec(self, *args):
        return {"id": "mirror.x", "description": "test spec", "command": ["python3", "-m", "scripts.mirror_measure",
                                              "bound-relations", *args],
                "expect": {"exit_code": 0, "json_fields": []}}

    def test_only_the_previous_report_argument_may_name_the_daily_report(self):
        self.assertEqual(lv.validate_spec(self.spec(lv.DAILY_REPORT_ARG)), [])
        for bad in ("{daily_report}", "--other={daily_report}", "--x={secret}"):
            with self.assertRaises(lv.SpecError):
                lv.validate_spec(self.spec(bad))


if __name__ == "__main__":
    unittest.main()
