"""plx.find filter listings resume a capped scan (decision
2026-10-28-landed-cost-estimate-live-filter-scan-cap, option 1).

A filter listing scans at most ``FILTER_SCAN_CAP`` readable objects of a type per call,
ordered by object id. A scan that stops at the cap returns a cursor carrying the scan
position, bound to the filter, the reader and the projection generation; the next call
continues the scan from there, re-checking readability; the listing is complete only on
the page whose scan reached the end of the type. Isolated fixture: synthetic ids only.
"""

from __future__ import annotations

import base64
import json
import unittest
import zlib
from unittest import mock

from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from tests.test_ontology_tools import ContractMixin, Store, tools

CAP = 7
READER = Actor("plx", frozenset({"sales"}))     # api:* audience, no finance marking
FULL = Actor("plx", frozenset({"sales", "finance_clerk"}))
PRESENT = {"object_type": "PurchaseOrder",
           "filters": [{"property": "po_number", "op": "present"}]}


def _oid(name: str) -> str:
    return f"PurchaseOrder:portal.app_purchase_order:{name}"


def listing_store(matches: int, *, generation: str | None = "gen-1",
                  hidden: tuple[str, ...] = ()) -> Store:
    """``matches`` readable orders with a po_number, each followed by a readable order
    without one (so every scan segment holds matches and non-matches), plus ``hidden``
    orders this reader may not read (finance-marked versions) that would match.
    ``generation`` None: the store states no content digest (nor build time)."""
    s = Store()
    if generation is not None:
        s.conn.execute("INSERT INTO store_meta VALUES ('content_digest', ?)", (generation,))
    for index in range(matches):
        for suffix, numbered in (("a", True), ("b", False)):
            name = f"bulk-{index:04d}{suffix}"
            handle = f"api://public.app_purchase_order/{name}@0000000000000001-9"
            s.obj(_oid(name), "api:public.app_purchase_order", handle, "gold_etl_from_fm")
            if numbered:
                s.val(_oid(name), "po_number", "identifier", f"PO{300000 + index}", handle,
                      "row.po_number", "gold_etl_from_fm")
    for name in hidden:
        handle = f"api://public.app_purchase_order/{name}@0000000000000001-9"
        s.obj(_oid(name), "api:public.app_purchase_order", handle, "gold_etl_from_fm",
              markings='["finance"]')
        s.val(_oid(name), "po_number", "identifier", "PO399999", handle, "row.po_number",
              "gold_etl_from_fm")
    s.conn.commit()
    return s


def _decode(cursor: str) -> dict:
    payload = cursor[len(ot.SCAN_CURSOR_PREFIX):]
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(
        (payload + "=" * (-len(payload) % 4)).encode())))


def _encode(body: dict) -> str:
    raw = zlib.compress(json.dumps(body, separators=(",", ":")).encode())
    return ot.SCAN_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


class FindScanResumeTests(ContractMixin, unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ot, "FILTER_SCAN_CAP", CAP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def store(self, *args, **kwargs) -> Store:
        s = listing_store(*args, **kwargs)
        self.addCleanup(s.conn.close)
        return s

    def set_count(self, t, handle, actor):
        out = oa.AggregateTools(t).call("plx.aggregate", {"set": handle,
                                                           "measure": {"op": "count"}}, actor)
        self.assertContract("plx.aggregate", out)
        self.assertNotIn("error", out, out)
        [group] = out["groups"]
        return int(group["value"]), out

    def pages(self, t, arguments, actor=READER, limit=50):
        """Every page of the listing. A page carrying a set never holds a set smaller
        than its total, and a complete page's set counts exactly its total."""
        out, cursor = [], None
        for _ in range(200):
            page = self.ok(t, "plx.find", dict(arguments, limit=limit,
                                               **({"cursor": cursor} if cursor else {})),
                           actor)
            out.append(page)
            completeness = page["completeness"]
            if "set" in page and completeness["total"] is not None:
                count, aggregate = self.set_count(t, page["set"], actor)
                self.assertEqual(count, completeness["total"], (completeness, aggregate))
            cursor = completeness["next_cursor"]
            if cursor is None:
                return out
        self.fail("the listing never ended")

    def listed(self, pages):
        """The listing in rank order, each match exactly once. A page may repeat the last
        match or two listed before it (never a lone apparent winner): such a row is
        marked ``repeated`` and is the earlier row of its rank (same object, same rank);
        the rows not marked cover ranks 1..N, each once, each naming another object."""
        first: dict[int, str] = {}
        for page in pages:
            for c in page["candidates"]:
                oid, rank = c["object"]["object_id"], c["rank"]
                if c.get("repeated"):
                    self.assertIs(c["repeated"], True)
                    self.assertEqual(first.get(rank), oid,
                                     "a repeated row is not the earlier row of its rank")
                else:
                    self.assertNotIn(rank, first, "a rank listed again unmarked")
                    first[rank] = oid
        self.assertEqual(sorted(first), list(range(1, len(first) + 1)))
        ids = [first[r] for r in sorted(first)]
        self.assertEqual(len(ids), len(set(ids)), "a match was listed under two ranks")
        return ids

    def segments_gap(self, page):
        return [g for g in page["gaps"] if g["reason"] == "set_spans_scan_segments"]

    def test_paging_lists_every_match_past_the_cap_exactly_once(self):
        # N=1/10/60 matches; 2N readable orders, so N >= 4 spans more than one cap.
        for n in (1, 10, 60):
            with self.subTest(matches=n):
                t = tools(self.store(n))
                pages = self.pages(t, PRESENT)
                ids = self.listed(pages)
                expected = sorted(_oid(f"bulk-{i:04d}a") for i in range(n))
                self.assertEqual(sorted(ids), expected)
                last = pages[-1]["completeness"]
                self.assertEqual((last["complete"], last["total"]), (True, n), last)
                for page in pages[:-1]:
                    self.assertFalse(page["completeness"]["complete"])
                    self.assertIsNotNone(page["completeness"]["next_cursor"])
                if n >= 4:
                    self.assertGreater(len(pages), 1)
                # Small pages lose nothing either.
                small = self.listed(self.pages(t, PRESENT, limit=2))
                self.assertEqual(sorted(set(small)), expected)

    def test_an_unreadable_match_past_the_cap_is_never_returned(self):
        hidden = ("bulk-0008c", "bulk-0011c")      # past the first cap of 7 readable
        t = tools(self.store(12, hidden=hidden))
        ids = self.listed(self.pages(t, PRESENT))
        self.assertEqual(sorted(ids), sorted(_oid(f"bulk-{i:04d}a") for i in range(12)))
        for name in hidden:
            self.assertNotIn(_oid(name), ids)
        # A reader holding the marking lists them: the fixture really matches.
        full = self.listed(self.pages(t, PRESENT, actor=FULL))
        self.assertEqual(len(full), 14)
        self.assertTrue({_oid(n) for n in hidden} <= set(full))

    def test_each_call_scans_at_most_the_cap(self):
        t = tools(self.store(30, hidden=("bulk-0003c", "bulk-0020c")))
        scanned: list[int] = []
        original = t._filter_objects

        def counting(scope, object_ids, filters):
            scanned[-1] += len(object_ids)
            return original(scope, object_ids, filters)

        t._filter_objects = counting
        cursor, calls = None, 0
        while True:
            scanned.append(0)
            page = self.ok(t, "plx.find", dict(PRESENT, limit=50,
                                               **({"cursor": cursor} if cursor else {})),
                           READER)
            calls += 1
            cursor = page["completeness"]["next_cursor"]
            if cursor is None:
                break
        self.assertTrue(all(count <= CAP for count in scanned), scanned)
        self.assertEqual(sum(scanned), 60)       # every readable order scanned once
        self.assertGreaterEqual(calls, 60 // CAP)

    def test_a_listed_match_no_longer_readable_ends_the_listing(self):
        # Four matches in the first segment of 7; the second segment (one order without
        # a po_number) repeats the last two listed, re-checked as readable now.
        store = self.store(4)
        t = tools(store)
        first = self.ok(t, "plx.find", dict(PRESENT, limit=50), READER)
        cursor = first["completeness"]["next_cursor"]
        self.assertIsNotNone(cursor)
        marked = ("UPDATE object_versions SET markings = ? WHERE object_id = ?")
        store.conn.execute(marked, ('["finance"]', _oid("bulk-0003a")))
        result = t.call("plx.find", dict(PRESENT, limit=50, cursor=cursor), READER)
        self.assertEqual(result.get("error", {}).get("code"), "invalid_cursor", result)
        self.assertNotIn("bulk-0003a", json.dumps(result))
        store.conn.execute(marked, ("[]", _oid("bulk-0003a")))
        last = self.ok(t, "plx.find", dict(PRESENT, limit=50, cursor=cursor), READER)
        self.assertEqual((last["status"], last["completeness"]["complete"],
                          last["completeness"]["total"], [c["rank"] for c in last["candidates"]]),
                         ("ambiguous", True, 4, [3, 4]))

    def test_a_foreign_or_tampered_cursor_is_refused(self):
        store = self.store(10)
        t = tools(store)
        first = self.ok(t, "plx.find", dict(PRESENT, limit=50), READER)
        self.assertEqual(first["completeness"]["reason"], "scan_cap")
        cursor = first["completeness"]["next_cursor"]
        self.assertIsNotNone(cursor)
        self.assertNotIn("error", t.call("plx.find", dict(PRESENT, limit=50, cursor=cursor),
                                         READER))

        def refused(arguments, actor, tools_=t):
            result = tools_.call("plx.find", arguments, actor)
            self.assertEqual(result.get("error", {}).get("code"), "invalid_cursor", result)

        other_filter = {"object_type": "PurchaseOrder",
                        "filters": [{"property": "po_number", "op": "absent"}]}
        refused(dict(other_filter, limit=50, cursor=cursor), READER)
        refused(dict(PRESENT, limit=50, cursor=cursor), FULL)
        # Another projection generation: the scan position names another store's order.
        store.conn.execute("UPDATE store_meta SET value = 'gen-2' WHERE key = 'content_digest'")
        refused(dict(PRESENT, limit=50, cursor=cursor), READER)
        store.conn.execute("UPDATE store_meta SET value = 'gen-1' WHERE key = 'content_digest'")
        # Any edited field of the cursor is refused, the scan position included.
        body = _decode(cursor)
        for key, value in body.items():
            edited = dict(body, **{key: (value + 1 if isinstance(value, int)
                                         else str(value)[:-1] + "x")})
            with self.subTest(field=key):
                refused(dict(PRESENT, limit=50, cursor=_encode(edited)), READER)
        refused(dict(PRESENT, limit=50, cursor="not-a-cursor"), READER)

    def test_a_listing_spanning_scan_segments_carries_no_set_and_says_why(self):
        # The reviewer's case: cap 7, 20 matches over 40 readable orders.
        t = tools(self.store(20))
        pages = self.pages(t, PRESENT)
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertNotIn("set", page)
            [gap] = self.segments_gap(page)
            self.assertEqual(gap["state"], "unknown")
        last = pages[-1]["completeness"]
        self.assertEqual((last["complete"], last["total"]), (True, 20))
        self.assertEqual(len(self.listed(pages)), 20)
        # Small pages of a multi-segment listing carry none either.
        for page in self.pages(t, PRESENT, limit=2):
            self.assertNotIn("set", page)
            self.assertEqual(len(self.segments_gap(page)), 1)

    def test_a_single_segment_listing_keeps_its_set(self):
        # 3 matches over 6 readable orders: one scan segment under the cap of 7.
        t = tools(self.store(3))
        for limit in (50, 2):
            with self.subTest(limit=limit):
                pages = self.pages(t, PRESENT, limit=limit)
                for page in pages:
                    self.assertEqual(self.segments_gap(page), [])
                    self.assertIn("set", page)
                    self.assertEqual(self.set_count(t, page["set"], READER)[0], 3)
                self.assertEqual(pages[-1]["completeness"]["complete"], True)
                self.assertEqual(len(self.listed(pages)), 3)

    def test_a_store_stating_no_generation_never_resumes_a_scan(self):
        # No content digest nor build time: a scan cursor could not tell a rebuilt
        # store's object ids from these, so the cut listing ends honestly at the cap.
        store = self.store(10, generation=None)
        t = tools(store)
        first = self.ok(t, "plx.find", dict(PRESENT, limit=50), READER)
        self.assertEqual((first["completeness"]["complete"], first["completeness"]["reason"],
                          first["completeness"]["total"], first["completeness"]["next_cursor"]),
                         (False, "scan_cap", None, None))
        self.assertIn("scan_cap", {g["reason"] for g in first["gaps"]})
        self.assertNotIn("next_cursor", next(g["detail"] for g in first["gaps"]
                                             if g["reason"] == "scan_cap"))
        # Paging within the one segment still works there.
        small = self.pages(tools(self.store(3, generation=None)), PRESENT, limit=2)
        self.assertEqual(len(self.listed(small)), 3)
        self.assertTrue(small[-1]["completeness"]["complete"])
        # A resuming cursor issued under a generation is refused once the store states none.
        dated = self.store(10)
        cursor = self.ok(tools(dated), "plx.find", dict(PRESENT, limit=50),
                         READER)["completeness"]["next_cursor"]
        self.assertIsNotNone(cursor)
        dated.conn.execute("DELETE FROM store_meta WHERE key = 'content_digest'")
        result = tools(dated).call("plx.find", dict(PRESENT, limit=50, cursor=cursor), READER)
        self.assertEqual(result.get("error", {}).get("code"), "invalid_cursor", result)


if __name__ == "__main__":
    unittest.main()
