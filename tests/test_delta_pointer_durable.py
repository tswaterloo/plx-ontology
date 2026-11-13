"""The delta pointer is published durably (DELTA-003): file bytes synced before the
rename, the directory entry after."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ontology import delta


class PointerDurable(unittest.TestCase):
    def test_the_file_and_the_directory_are_synced_around_the_rename(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        events = []
        real_replace = os.replace
        with mock.patch("os.fsync", side_effect=lambda fd: events.append("fsync")), \
                mock.patch("os.replace",
                           side_effect=lambda a, b: (events.append("replace"),
                                                     real_replace(a, b))[1]):
            delta._write_durable(Path(tmp.name) / delta.POINTER, json.dumps({"a": 1}))
        self.assertEqual(events, ["fsync", "replace", "fsync"])
        self.assertEqual(json.loads((Path(tmp.name) / delta.POINTER).read_text()), {"a": 1})
        self.assertEqual(os.listdir(tmp.name), [delta.POINTER])


if __name__ == "__main__":
    unittest.main()
