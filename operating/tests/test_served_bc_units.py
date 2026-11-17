"""``ontology_host.served_bc_units``: the BC units a served generation holds are read once
per process per generation, and a generation is told apart by its file's whole stamp
(``_file_stamp``: device, inode, modification time and size), never its inode alone: a
removed file frees its inode for the next one, which may be another generation's, and the
stale units denied every BC record of the new one (seen as ``root_not_readable`` /
``scope_unread`` in operating.tests.test_unfamiliar_order_case).

Evidence label: isolated (in-process fakes; no store, no network).
"""

from __future__ import annotations

import unittest
from unittest import mock

from operating import ontology_host


class Reader:
    def __init__(self, units: dict[str, str]) -> None:
        self.units, self.reads = units, 0


def units_of(reader: Reader) -> dict[str, str]:
    reader.reads += 1
    return dict(reader.units)


class ServedBcUnitsTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(ontology_host, "store_bc_units", units_of)
        patcher.start()
        self.addCleanup(patcher.stop)
        ontology_host._BC_UNITS.clear()
        self.addCleanup(ontology_host._BC_UNITS.clear)

    def test_one_generation_is_read_once(self):
        first = Reader({"bc:CO1/salesInvoices": "production"})
        self.assertEqual(ontology_host.served_bc_units(first, (1, 7)), first.units)
        self.assertEqual(ontology_host.served_bc_units(first, (1, 7)), first.units)
        self.assertEqual(first.reads, 1)
        # what a caller changes is its own copy
        ontology_host.served_bc_units(first, (1, 7)).clear()
        self.assertEqual(ontology_host.served_bc_units(first, (1, 7)), first.units)

    def test_another_generation_at_the_same_path_reads_its_own_units(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / "projection.sqlite"
            store.write_bytes(b"one generation")
            old = Reader({"bc:CO1/salesInvoices": "production"})
            ontology_host.served_bc_units(old, ontology_host._file_stamp(store))
            # the next generation renamed over the path (its inode may be the old one's)
            new_file = Path(temp) / "next.sqlite"
            new_file.write_bytes(b"the next generation, longer")
            os.replace(new_file, store)
            new = Reader({"bc:CO2/salesInvoices": "production"})
            self.assertEqual(
                ontology_host.served_bc_units(new, ontology_host._file_stamp(store)),
                new.units)
            self.assertEqual(new.reads, 1)

    def test_no_identity_reads_every_time(self):
        reader = Reader({"bc:A/x": "p"})
        ontology_host.served_bc_units(reader, None)
        ontology_host.served_bc_units(reader, None)
        self.assertEqual(reader.reads, 2)


if __name__ == "__main__":
    unittest.main()
