"""A delta cycle orders record keys of mixed shapes, and a removal check against the store's
latest time never parses that constant per row."""
import sqlite3
import unittest

from ontology import delta, store


class MixedKeyOrder(unittest.TestCase):
    def test_text_and_tuple_keys_sort_together_in_a_stable_order(self):
        keys = {("item", "d", "i"), "legacy-key", ("a", "b"), "another"}
        first = sorted(keys, key=delta._order)
        self.assertEqual(first, sorted(reversed(first), key=delta._order))
        self.assertEqual(len(first), len(keys))
        with self.assertRaises(TypeError):   # the failure this guards against
            sorted(keys)

    def test_list_and_tuple_forms_of_one_key_order_alike(self):
        self.assertEqual(delta._order(("a", "b"))[0], delta._order(["a", "b"])[0])


class RemovalAtLatest(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE objects (object_id TEXT, missing_from_source_since TEXT);
            CREATE TABLE object_removals (object_id TEXT, since TEXT, until TEXT);
            INSERT INTO objects VALUES ('held', NULL), ('gone', NULL), ('ended', NULL),
                ('absent', '2026-01-01T00:00:00Z'), ('junk', NULL);
            INSERT INTO object_removals VALUES ('gone', '2026-02-01T00:00:00Z', NULL),
                ('ended', '2026-02-01T00:00:00Z', '2026-03-01T00:00:00Z'),
                ('junk', 'not a time', NULL);
        """)
        return db

    def test_latest_constant_is_a_number_and_answers_are_unchanged(self):
        sql = store.removed_at_sql("o.object_id")
        self.assertNotIn("julianday('9999", sql)
        self.assertIn(repr(store._LATEST_JULIAN), sql)
        db = self._db()
        got = {r[0] for r in db.execute(
            f"SELECT o.object_id FROM objects o WHERE {sql}")}
        self.assertEqual(got, {"gone", "absent", "junk"})   # junk time: removed, fail closed
        self.assertEqual({r[0] for r in db.execute(store.removed_ids_sql())}, got)

    def test_an_explicit_time_still_compares_by_julianday(self):
        sql = store.removed_at_sql("o.object_id", ":at")
        self.assertIn("julianday(:at)", sql)
        got = {r[0] for r in self._db().execute(
            f"SELECT o.object_id FROM objects o WHERE {sql}", {"at": "2026-02-15T00:00:00Z"})}
        self.assertEqual(got, {"gone", "ended", "absent", "junk"})


if __name__ == "__main__":
    unittest.main()
