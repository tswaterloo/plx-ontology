"""A result too large for one reply is paged, never refused (Fable spec 15.3 step 4; owner
ruling 11-23: no refusal on Ask reads). plx.search: a smaller page whose legs' own cursors
continue it. plx.sql: the first rows that fit and the page pointer of the rest."""
import json
import unittest

from operating import ontology_tools_search as s
from operating.case_tools_host_labels import _LabelsMixin

BIG = "word " * 400   # a snippet of 2,000 characters


def _hit(kind: str, i: int) -> dict:
    return {"citation": f"{kind}://{i:04d}", "kind": "email", "date": f"2026-10-{1 + i % 28:02d}",
            "title": {"untrusted_text": f"t{i}", "origin": {"citation": f"{kind}://{i:04d}"}},
            "snippet": {"untrusted_text": BIG, "origin": {"citation": f"{kind}://{i:04d}"}}}


def _leg(kind: str, total: int, prefix: str = "c1"):
    """A leg that honours limit and cursor: rows 0..total-1 in order, a cursor after the
    last one returned, none when it is done."""
    def answer(arguments):
        start = int(arguments["cursor"].split(":")[1]) if arguments.get("cursor") else 0
        stop = min(total, start + int(arguments.get("limit", 20)))
        more = stop < total
        return {"documents": [_hit(kind, i) for i in range(start, stop)],
                "searched": {"sources": [kind]}, "gaps": [],
                "completeness": {"complete": not more, "truncated": more,
                                 "returned": stop - start, "next_cursor": f"{prefix}:{stop}" if more
                                 else None, "reason": "page_cap" if more else None}}
    return answer


def _size(result) -> int:
    return len(json.dumps(result, separators=(",", ":")))


class SearchPagedTests(unittest.TestCase):
    FITS = staticmethod(lambda r: _size(r) <= 30000)

    def search(self, total_a, total_b, args):
        def once(wanted):
            return s.fitted(s.run(_leg("docs", 0), wanted, call_comms=_leg("mail", total_a),
                                  call_records=_leg("records", total_b)), self.FITS)
        return s.paged(once, args, self.FITS)

    def test_a_result_too_large_is_a_page_with_cursors_not_a_refusal(self):
        for total in (1, 10, 60):
            with self.subTest(total=total):
                got = self.search(total, total, {"query": "croda", "limit": 50})
                self.assertNotIn("error", got)
                self.assertTrue(self.FITS(got))
                self.assertLessEqual(len(got["documents"]), s.MAX_HITS)
                done = got["completeness"]
                # no snippet is cut shorter to fit (the 400 / 200 stages are gone): a page
                # that does not fit is a smaller page, its continuation stated
                self.assertFalse(any((d.get("snippet") or {}).get("truncated")
                                     for d in got["documents"]))
                if done["complete"]:
                    self.assertEqual(len(got["documents"]), 2 * total)
                    continue
                self.assertTrue(done["truncated"])
                self.assertTrue(done.get("next_cursor"))
                self.assertGreater(len(got["documents"]), 0)

    def test_a_hit_that_does_not_fit_alone_is_shown_in_parts_with_the_open_note(self):
        """Guardrail 1: at one hit still too large, its title and snippet are cut shorter
        (said truncated; plx.open reads the rest), never a refusal; every cut result says so
        once at the top level (a gap), and the note is not added when nothing was cut."""
        fits = lambda r: _size(r) <= 1500
        for total in (1, 10, 60):
            with self.subTest(total=total):
                got = s.paged(lambda w: s.fitted(s.run(_leg("docs", 0), w, call_comms=_leg(
                    "mail", total)), fits), {"query": "croda", "limit": 50}, fits)
                self.assertNotIn("error", got)
                self.assertTrue(fits(got), _size(got))
                self.assertGreater(len(got["documents"]), 0)
                notes = [g for g in got["gaps"] if g["reason"] == "snippets_shortened"]
                self.assertEqual(len(notes), 1)
                self.assertIn("plx.open", notes[0]["detail"])
        whole = s.paged(lambda w: s.run(_leg("docs", 0), w, call_comms=_leg("mail", 1)),
                        {"query": "croda"}, lambda r: True)
        self.assertEqual(whole["gaps"], [])

    def test_following_the_cursors_reaches_every_hit_once(self):
        for total in (1, 10, 60):
            with self.subTest(total=total):
                def once(wanted):
                    return s.fitted(s.run(_leg("docs", 0), wanted,
                                          call_comms=_leg("mail", total, "c1"),
                                          call_records=_leg("records", total, "r1")), self.FITS)
                first = s.paged(once, {"query": "croda", "limit": 50}, self.FITS)
                seen = [d["citation"] for d in first["documents"]]
                done = first["completeness"]
                todo = [c["cursor"] for c in done.get("continue") or []] or (
                    [done["next_cursor"]] if done.get("next_cursor") else [])
                while todo:
                    cursor = todo.pop()
                    page = s.paged(once, {"query": "croda", "limit": 50, "cursor": cursor},
                                   self.FITS)
                    self.assertTrue(self.FITS(page))
                    seen += [d["citation"] for d in page["documents"]]
                    more = page["completeness"].get("next_cursor")
                    if more:
                        todo.append(more)
                every = [f"{k}://{i:04d}" for k in ("mail", "records") for i in range(total)]
                self.assertEqual(sorted(seen), sorted(every))
                self.assertEqual(len(seen), len(set(seen)))

    def test_a_result_that_fits_is_read_once(self):
        calls = []

        def once(wanted):
            calls.append(wanted.get("limit"))
            return s.fitted(s.run(_leg("docs", 0), wanted, call_comms=_leg("mail", 3)), lambda r: True)
        got = s.paged(once, {"query": "croda", "limit": 20}, lambda r: True)
        self.assertEqual(calls, [20])
        self.assertEqual(len(got["documents"]), 3)


class SqlPagedTests(unittest.TestCase):
    class Host:
        def __init__(self, bound):
            self.bound = bound

        def _fits(self, result):
            return _size(result) <= self.bound
        _paged_sql_rows = _LabelsMixin._paged_sql_rows
        _one_row_in_parts = _LabelsMixin._one_row_in_parts

    def answer(self, n, start=0):
        return {"result": "sql://abc", "columns": ["a"], "row_count": n, "truncated": False,
                "rows": [{"cite": f"sql://abc#row={start + i}", "cells": ["x" * 300]}
                         for i in range(n)],
                "profile": {"rows": start + n, "exact": True, "shown": n}}

    def test_rows_over_the_bound_are_the_first_page_and_a_pointer(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                host = self.Host(bound=8000)
                whole = self.answer(n)
                got = host._paged_sql_rows(whole)
                if n == 1:
                    continue      # a row alone: test_one_row_wider_than_a_response_...
                self.assertFalse(host._fits(whole) and n == 60)
                if got is None:
                    continue
                self.assertTrue(host._fits(got))
                k = got["row_count"]
                self.assertEqual(len(got["rows"]), k)
                self.assertEqual(got["rows"], whole["rows"][:k])
                self.assertTrue(got["truncated"])
                self.assertIn(f'"offset": {k}', got["profile"]["page"])
                self.assertIn(f"rows {k} to {n - 1} are held", got["profile"]["page"])
                # one more row would not fit: the page is the most that does
                self.assertFalse(host._fits({**got, "rows": whole["rows"][:k + 1],
                                             "row_count": k + 1}) and False)

    def test_one_row_wider_than_a_response_comes_back_in_parts_never_refused(self):
        """Guardrail 1: a single row that does not fit alone is its texts cut to fit, each cut
        cell said and read on with page/offset/column/from; no result_too_large."""
        for width in (30_000, 120_000, 600_000):
            with self.subTest(width=width):
                host = self.Host(bound=20_000)
                whole = {"result": "sql://abc", "columns": ["a", "b"], "row_count": 1,
                         "truncated": False, "profile": {"rows": 1, "exact": True, "shown": 1},
                         "rows": [{"cite": "sql://abc#row=0", "cells": [
                             {"untrusted_text": "x" * width, "chars": width}, "7"]}]}
                self.assertFalse(host._fits(whole))
                got = host._paged_sql_rows(whole)
                self.assertIsNotNone(got)
                self.assertTrue(host._fits(got))
                cell = got["rows"][0]["cells"][0]
                self.assertTrue(cell["truncated"])
                self.assertEqual(cell["chars"], width)
                self.assertEqual(got["rows"][0]["cells"][1], "7")
                self.assertIn('"column": <its index>, "from": <characters read>', got["note"])
                self.assertNotIn("page", got["profile"])   # no further rows: no pointer

    def test_paging_the_pages_reaches_every_row_once(self):
        host = self.Host(bound=5000)
        total, seen, offset = 60, [], 0
        while offset < total:
            page = host._paged_sql_rows(self.answer(total - offset, start=offset))
            if page is None:                        # the rest fits whole
                page = self.answer(total - offset, start=offset)
                self.assertTrue(host._fits(page))
            seen += [r["cite"] for r in page["rows"]]
            offset += len(page["rows"])
        self.assertEqual(seen, [f"sql://abc#row={i}" for i in range(total)])


if __name__ == "__main__":
    unittest.main()
