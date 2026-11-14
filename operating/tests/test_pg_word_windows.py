"""How a Postgres word query is cut into windows (``ontology_reader.WordWindows``): a value
word estimated at a few ten thousand matches, or any word read only in named properties'
cells, is read in one window, never range by range (each range a bitmap of the cells' key
index: 101 s for a customer name on the served store, 2026-11-13); a very common word is
still read window by window, so its first page stops early. No database: the statistics
and the reads are the store's stand-ins."""

from __future__ import annotations

import unittest

from operating.pg import ontology_reader as R

ROWS = 490_000_000          # the value words index's rows
LOW, HIGH = 1, 57_000_000   # the value sources' key range


def _windows(share: float, *, fields=None, table="value_text") -> list[tuple[int, int]]:
    """The (low, high) key range of each window read for one word of this share."""
    reads: list[tuple[int, int]] = []
    ww = R.WordWindows(conn=None)
    facts = {"rank": {7: 1}, "field_of": {1: 7}, "by_property": {"name": [7]},
             "indexed": frozenset({1}), "text": frozenset({1}), "estimates": {}, "kept": {},
             table: (LOW, HIGH, float(ROWS)),
             f"stats:{table}": ({"word": share}, ["word"], 1e-5)}
    ww.facts = facts
    ww.refresh = lambda: facts   # type: ignore[method-assign]

    def read(_table, _query, lo, hi, *_rest):
        reads.append((lo, hi))
        return []   # nothing matches: every window is read
    ww._read = read   # type: ignore[method-assign]
    for _ in ww.windows(table, "'word'", 0, fields=fields):
        pass
    return reads


class WordWindowTests(unittest.TestCase):
    def test_tens_of_thousands_of_matches_read_once(self):
        # 24,000 matches estimated (the measured customer name): one read of the store
        self.assertEqual(_windows(24_000 / ROWS), [(LOW, HIGH)])
        self.assertEqual(_windows(90_000 / ROWS), [(LOW, HIGH)])

    def test_named_properties_read_once_unless_walked(self):
        # a name pass of a word matching 400,000 cells: few pass the fields, so one read
        self.assertEqual(_windows(400_000 / ROWS, fields=[7]), [(LOW, HIGH)])
        # unrestricted, the same word is read window by window (a page stops early)
        self.assertGreater(len(_windows(400_000 / ROWS)), 1)

    def test_common_word_still_windowed(self):
        windows = _windows(5_000_000 / ROWS)
        self.assertGreater(len(windows), 1)
        self.assertEqual(windows[0][0], LOW)
        self.assertEqual(windows[-1][1], HIGH)   # every key read, none twice
        for (_a, b), (c, _d) in zip(windows, windows[1:]):
            self.assertEqual(c, b + 1)

    def test_document_text_threshold_unchanged(self):
        # document words are not value cells: 24,000 matches stay windowed there
        self.assertGreater(len(_windows(24_000 / ROWS, table="chunk_text")), 1)


if __name__ == "__main__":
    unittest.main()
