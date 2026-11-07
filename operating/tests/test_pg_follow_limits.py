"""The Postgres follower's connections carry connect, statement and lock-wait limits
(operating/pg/ontology_delta.py ``FOLLOW_LIMITS``), so a hung server or a held lock ends
a cycle instead of the follower. Evidence label: ``isolated-fixture`` (a made-up served
store; the connection is refused before any statement)."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

try:
    import psycopg
    from operating.pg import ontology_delta
except ImportError:  # psycopg not installed
    ontology_delta = None


class Refused(Exception):
    pass


@unittest.skipIf(ontology_delta is None, "psycopg not installed")
class FollowLimitTests(unittest.TestCase):
    def test_the_follower_connects_with_its_limits(self):
        with tempfile.TemporaryDirectory() as root:
            projection = Path(root) / "projection"
            projection.mkdir()
            with sqlite3.connect(projection / "ontology.sqlite") as conn:
                conn.execute("CREATE TABLE store_meta(key TEXT, value TEXT)")
                conn.execute("INSERT INTO store_meta VALUES ('content_digest', 'sha256:00')")
            seen = []

            def connect(dsn, **kwargs):
                seen.append(kwargs)
                raise Refused()
            with mock.patch.object(psycopg, "connect", side_effect=connect):
                with self.assertRaises(Refused):
                    ontology_delta.follow(root, "postgresql://made-up/db")
        self.assertEqual(seen, [ontology_delta.FOLLOW_LIMITS])
        self.assertIn("statement_timeout", ontology_delta.FOLLOW_LIMITS["options"])
        self.assertIn("lock_timeout", ontology_delta.FOLLOW_LIMITS["options"])
        self.assertTrue(ontology_delta.FOLLOW_LIMITS["connect_timeout"])


if __name__ == "__main__":
    unittest.main()
