"""A filter narrowing read value first (``StoreReader.narrowed_ids``, Postgres) lists the
same records, gaps and cursors as the span-by-span scan, for a full and a restricted reader,
at N = 1, 10 and 60 matches; a narrowing holding more than its cap falls back to the scan.
Isolated fixture: synthetic ids only, the SQLite fixture store read with ``VALUES_FIRST``
switched on (the statement is the one Postgres runs, through its SQLite dialect)."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from ontology.types import Actor
from operating import ontology_tools as ot
from tests.test_ontology_tools import ContractMixin, Store, tools

READER = Actor("plx", frozenset({"sales"}))     # api:* audience, no finance marking
FULL = Actor("plx", frozenset({"sales", "finance_clerk"}))
VOLATILE = {"meta", "elapsed_ms", "timings", "served_at", "set"}   # a set handle is minted per call


def _oid(name: str) -> str:
    return f"PurchaseOrder:portal.app_purchase_order:{name}"


def population(matches: int) -> Store:
    """``matches`` orders numbered ``PO77xxxx`` among 3x as many others (``PO11xxxx``),
    plus two that would match whose number only a finance reader may read and one whose
    whole version is finance-marked."""
    s = Store()
    s.conn.execute("INSERT INTO store_meta VALUES ('content_digest', 'gen-1')")

    def order(name: str, number: str, *, value_marks: str = "[]", marks: str = "[]") -> None:
        handle = f"api://public.app_purchase_order/{name}@0000000000000001-9"
        s.obj(_oid(name), "api:public.app_purchase_order", handle, "gold_etl_from_fm",
              markings=marks)
        s.val(_oid(name), "po_number", "identifier", number, handle, "row.po_number",
              "gold_etl_from_fm", markings=value_marks)
    for index in range(matches * 3):
        order(f"other-{index:04d}", f"PO11{index:04d}")
    for index in range(matches):
        order(f"want-{index:04d}", f"PO77{index:04d}")
    order("withheld-a", "PO770999", value_marks='["finance"]')
    order("withheld-b", "PO110999", value_marks='["finance"]')
    order("hidden", "PO770998", marks='["finance"]')
    s.conn.commit()
    return s


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in sorted(value.items()) if k not in VOLATILE}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


FILTERS = (
    [{"property": "po_number", "op": "prefix", "value": "PO77"}],
    [{"property": "po_number", "op": "contains", "value": "770"}],
    [{"property": "po_number", "op": "eq", "value": "PO770003"}],
    [{"property": "po_number", "op": "in", "value": ["PO770000", "PO110001"]}],
    [{"property": "po_number", "op": "eq", "value": "PO999999"}],
)


class ValuesFirstNarrowingTests(ContractMixin, unittest.TestCase):
    def _both(self, store: Store, actor: Actor, filters: list, **extra) -> tuple:
        out = []
        for first in (False, True):
            with mock.patch.object(ot.StoreReader, "VALUES_FIRST", first):
                t = tools(store)
                args = {"object_type": "PurchaseOrder", "filters": filters, "limit": 50, **extra}
                result = t.call("plx.find", args, actor)
                self.assertContract("plx.find", result)
                out.append(_clean(result))
        return tuple(out)

    def test_value_first_lists_what_the_span_scan_lists_for_every_reader(self):
        for matches in (1, 10, 60):
            store = population(matches)
            for actor in (READER, FULL):
                for filters in FILTERS:
                    with self.subTest(n=matches, actor=repr(actor), filters=filters):
                        spans, first = self._both(store, actor, filters)
                        self.assertEqual(first, spans)
            # the prefix listing holds every wanted order once, no other; the finance
            # reader also gets the order whose number it may read; the hidden one never
            spans, first = self._both(store, READER, list(FILTERS[0]))
            ids = [c["object"]["object_id"] for c in first["candidates"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual({i for i in ids if "want-" in i},
                             {_oid(f"want-{k:04d}") for k in range(matches)}
                             if matches <= 50 else {i for i in ids if "want-" in i})
            self.assertNotIn(_oid("hidden"), ids)
            self.assertNotIn(_oid("withheld-a"), ids)
            self.assertFalse(any("other-" in i for i in ids))
            if matches <= 10:   # within the page of 50
                full = self._both(store, FULL, list(FILTERS[0]))[1]
                self.assertIn(_oid("withheld-a"),
                              [c["object"]["object_id"] for c in full["candidates"]])
            self.assertNotIn(_oid("hidden"), json.dumps(first))

    def test_a_narrowing_past_its_cap_falls_back_to_the_span_scan(self):
        store = population(10)
        reads = []
        real = ot.StoreReader.narrowed_ids

        def spy(self_, *args, **kwargs):
            got = real(self_, *args, **kwargs)
            reads.append(got)
            return got
        with mock.patch.object(ot.StoreReader, "NARROWED_IDS_CAP", 3), \
                mock.patch.object(ot.StoreReader, "narrowed_ids", spy):
            spans, first = self._both(store, READER, list(FILTERS[0]))
        self.assertEqual(first, spans)
        self.assertIn(None, reads)   # more than 3 candidates: the scan ran instead

    def test_short_of_time_the_span_scan_answers_what_it_decided(self):
        """With little of the call's time left the value-first read is not started: the
        span scan stops between statements, lists what it decided with a cursor, and
        following it lists the rest, none twice (never nothing at the limit)."""
        import time as clock
        from operating import call_limits
        store = population(7)
        asked = {"object_type": "PurchaseOrder", "limit": 50, "filters": list(FILTERS[0])}
        with mock.patch.object(ot.StoreReader, "VALUES_FIRST", True), \
                mock.patch.object(ot, "NARROWED_SPAN", 2):
            t = tools(store)
            with mock.patch.object(ot.StoreReader, "narrowed_ids",
                                   side_effect=AssertionError("read value first")), \
                    call_limits.sql_deadline(clock.monotonic() + 1.0):
                first = t.call("plx.find", asked, READER)
            self.assertTrue(first["completeness"]["truncated"], first)
            seen = [c["object"]["object_id"] for c in first["candidates"]]
            cursor = first["completeness"]["next_cursor"]
            while cursor:
                page = t.call("plx.find", dict(asked, cursor=cursor), READER)
                seen += [c["object"]["object_id"] for c in page.get("candidates") or ()]
                cursor = page["completeness"]["next_cursor"]
        self.assertEqual(sorted(seen), sorted(set(seen)))
        self.assertEqual({i for i in seen if "want-" in i},
                         {_oid(f"want-{k:04d}") for k in range(7)})

    def test_a_resumed_segment_never_reads_values_first_again(self):
        """A count scanned over many segments (plx.aggregate) reads the narrowing value
        first once, on its first segment: past its cap, the span scan runs on."""
        from operating import ontology_tools_aggregate as oa
        store = population(10)
        reads = []
        real = ot.StoreReader.narrowed_ids

        def spy(self_, *args, **kwargs):
            reads.append(1)
            return real(self_, *args, **kwargs)
        counted = []
        for first in (False, True):
            with mock.patch.object(ot.StoreReader, "VALUES_FIRST", first), \
                    mock.patch.object(ot.StoreReader, "NARROWED_IDS_CAP", 3), \
                    mock.patch.object(ot, "FILTER_SCAN_CAP", 4), \
                    mock.patch.object(ot.StoreReader, "narrowed_ids", spy):
                out = oa.AggregateTools(tools(store)).call("plx.aggregate", {
                    "object_type": "PurchaseOrder", "measure": {"op": "count"},
                    "filters": list(FILTERS[0])}, FULL)
            self.assertNotIn("error", out, out)
            self.assertTrue(out["completeness"]["complete"], out)
            counted.append(out["groups"][0]["value"])
        self.assertEqual(counted, ["12", "12"])   # 10 wanted, the finance-only two
        self.assertEqual(len(reads), 1)

    def test_value_first_reads_no_key_spans(self):
        store = population(10)
        with mock.patch.object(ot.StoreReader, "VALUES_FIRST", True), \
                mock.patch.object(ot, "NARROWED_SPAN", 2), \
                mock.patch.object(ot.StoreReader, "key_ahead",
                                  side_effect=AssertionError("span read")):
            result = tools(store).call("plx.find", {"object_type": "PurchaseOrder",
                                                    "filters": list(FILTERS[0])}, READER)
        self.assertNotIn("error", result)
        self.assertEqual(len(result["candidates"]), 10)

    def test_a_dated_filter_reads_each_columns_bulk_days_once(self):
        """``bulk_default_dates`` reads ``default_days`` once per (type, property), never
        once per record: N = 1, 10 and 60 dated records, one read each time."""
        for n in (1, 10, 60):
            t = tools(population(1))
            calls = []
            with mock.patch.object(ot.OntologyTools, "default_days",
                                   lambda self_, otype, prop: calls.append((otype, prop))
                                   or {}):
                dated = {(_oid(f"d-{k}"), "order_date"): "2026-01-01" for k in range(n)}
                with t.store.lock:
                    scope = t._scope(READER, t.clock())
                    t.bulk_default_dates(scope, dated)
            self.assertEqual(calls, [("PurchaseOrder", "order_date")], n)


if __name__ == "__main__":
    unittest.main()
