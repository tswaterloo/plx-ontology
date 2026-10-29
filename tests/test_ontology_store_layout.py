"""Store layout v6/v10 at its boundary: the ``object_values`` and ``links`` views read back
exactly what was written, whichever way it was written (the builder's packed path, a
direct write through the view's triggers, an update, a delete), and a text the packed
layout cannot hold byte for byte is stored as written or refused like v5 refused it.

The physical tables are an implementation detail; these tests read only the views (what
every reader uses) and assert on sizes only where the layout exists to save them.
"""

from __future__ import annotations

import collections
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from ontology import store_ddl
from ontology.project import build_store
from ontology.store import StoreBuilder, content_digest, iter_rows, pack_link_texts
from ontology.tests import synthetic_captures

T = datetime(2026, 10, 16, tzinfo=timezone.utc)
_SCHEMA: list[Any] = []


def schema() -> Any:
    if not _SCHEMA:
        from ontology.schema import load_schema
        _SCHEMA.append(load_schema())
    return _SCHEMA[0]


def logical(conn: sqlite3.Connection, table: str) -> collections.Counter:
    columns = store_ddl.VALUE_COLUMNS if table == "object_values" else store_ddl.LINK_COLUMNS
    return collections.Counter(conn.execute(f"SELECT {', '.join(columns)} FROM {table}"))


class Recorded:
    """Every row a build hands ``StoreBuilder.add`` for the two packed tables."""

    def __init__(self) -> None:
        self.rows: dict[str, list[tuple]] = {"object_values": [], "links": []}
        self.original = StoreBuilder.add
        recorder = self

        def add(builder: Any, table: str, rows: Any) -> int:
            if table in recorder.rows:
                rows = list(rows)
                columns = (store_ddl.VALUE_COLUMNS if table == "object_values"
                           else store_ddl.LINK_COLUMNS)
                recorder.rows[table] += [tuple(r.get(c) for c in columns) for r in rows]
            return recorder.original(builder, table, rows)

        self.patch = mock.patch.object(StoreBuilder, "add", add)


class StoreLayoutTests(unittest.TestCase):

    def test_the_views_read_back_every_row_the_build_wrote_at_any_cardinality(self):
        for n in (1, 10, 60):
            with self.subTest(rows=n), tempfile.TemporaryDirectory() as tmp:
                recorded = Recorded()
                path = Path(tmp) / "store.sqlite"
                with recorded.patch:
                    report = build_store(path, schema(), synthetic_captures.sources(schema(), n),
                                         clock=lambda: T)
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    for table, rows in recorded.rows.items():
                        self.assertTrue(rows, table)
                        # no row lost, duplicated or changed by a byte
                        self.assertEqual(logical(conn, table), collections.Counter(rows), table)
                    self.assertEqual(conn.execute("SELECT value FROM store_meta WHERE key = "
                                                  "'format'").fetchone()[0], store_ddl.FORMAT)
                    self.assertEqual(content_digest(conn), report["content_digest"])
                    # the row numbers a keyset pager uses are unique and cover every cell;
                    # the rest are values of packed bindings (v10), there in the view too
                    numbers = [r[0] for r in conn.execute(
                        "SELECT row_no FROM object_values_numbered")]
                    self.assertEqual(len(numbers), len(set(numbers)))
                    self.assertNotIn(None, numbers)
                    packed = {b["id"] for t in schema().object_types.values()
                              for b in t.get("bindings", []) if b.get("value_storage") == "packed"}
                    at = store_ddl.VALUE_COLUMNS.index("binding_id")
                    in_packs = [r for r in recorded.rows["object_values"] if r[at] in packed]
                    self.assertTrue(in_packs)
                    self.assertEqual(len(numbers) + len(in_packs), len(recorded.rows["object_values"]))
                    self.assertEqual(conn.execute("SELECT count(*) FROM value_cells WHERE field "
                                                  "IS NULL").fetchone()[0],
                                     len({(r[0], r[9]) for r in in_packs}))
                    # iter_rows reads values in object order over its chunks, all of them
                    with mock.patch("ontology.store._CHUNK_OBJECTS", 3):
                        chunked = list(iter_rows(conn, "object_values"))
                    self.assertEqual(collections.Counter(tuple(r) for r in chunked),
                                     logical(conn, "object_values"))
                    at = [store_ddl.VALUE_COLUMNS.index(c)
                          for c in ("object_id", "property", "l1_handle", "field_path")]
                    keys = [tuple(r[i] for i in at) for r in chunked]
                    self.assertEqual(keys, sorted(keys))
                    with mock.patch("ontology.store._CHUNK_OBJECTS", 3):
                        self.assertEqual(content_digest(conn), report["content_digest"])
                finally:
                    conn.close()

    def test_a_shared_provenance_is_stored_once_and_a_link_text_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite"
            build_store(path, schema(), synthetic_captures.sources(schema(), 20), clock=lambda: T)
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                one = conn.execute
                versions = one("SELECT count(DISTINCT object_id || ' ' || l1_handle) "
                               "FROM object_values").fetchone()[0]
                self.assertLessEqual(one("SELECT count(*) FROM value_sources").fetchone()[0],
                                     2 * versions)
                handles = collections.Counter(h for (e,) in one(
                    "SELECT evidence_handles FROM links") for h in json.loads(e))
                self.assertEqual(one("SELECT count(*) FROM link_texts").fetchone()[0],
                                 one("SELECT count(DISTINCT text) FROM link_texts").fetchone()[0])
                for handle in [h for h, k in handles.items() if k > 1][:20]:
                    self.assertEqual(one("SELECT count(*) FROM link_texts WHERE text = ?",
                                         (json.dumps(handle)[1:-1],)).fetchone()[0], 1, handle)
                # no index besides the declared ones duplicates a text column
                auto = [r[0] for r in one("SELECT name FROM sqlite_master WHERE type = 'index' "
                                          "AND tbl_name IN ('value_cells', 'value_sources')")]
                self.assertNotIn("sqlite_autoindex_value_sources_1", auto)
            finally:
                conn.close()


def link(link_id: str, reason: str, evidence: str, **extra: Any) -> dict[str, Any]:
    return {"link_id": link_id, "link_type": "t", "from_object": "A:b:1", "to_object": "A:b:2",
            "tier": "lead", "binding_kind": "fk", "link_contract": None, "reason": reason,
            "why_not": "[]", "evidence_handles": evidence, "reviewed_by": None,
            "reviewed_at": None, "review_action_id": None, "markings": "[]",
            "recorded_at": "2026-10-01T00:00:00.000000Z", "superseded_at": None, **extra}


class LinkTextTests(unittest.TestCase):
    """Texts of every shape the packer meets, through both write paths."""

    TEXTS = [
        # (reason, evidence)
        ('[{"feature":"fk","cites":["api://x@1","api://x@1"]}]', '["api://x@1"]'),
        ('[{"feature":"a\\"quoted\\" \\\\ back","v":"\\u00e9"}]', '["h\\"1","h\\\\2"]'),
        ('["k","k"]', '["k"]'),                            # a value equal to a key-like text
        ('[{"k":"k"}]', '["k"]'),                          # a key equal to a slot text stays
        ('[""]', '[""]'),                                  # the empty string
        (json.dumps([f"r{i}" * 3 for i in range(40)]),     # more strings than slots
         json.dumps([f"h{i}" for i in range(20)])),
        ('["\\uE000 literal"]', '["e"]'),                  # holds the token prefix
        ('["\\ue000 lower"]', '["\\uE00F"]'),              # lower-case escape; token in evidence
        ('[1, 2.5, true, null]', '[1]'),                   # no string at all
        ('[ "spaced" , {"a" : "b"} ]', '[ "x" ]'),         # spacing kept byte for byte
        ('[" raw private-use char"]', '[""]'),
    ]

    def store(self, tmp: str) -> StoreBuilder:
        builder = StoreBuilder(Path(tmp) / "s.sqlite")
        builder.add("objects", [{"object_id": f"A:b:{n}", "object_type": "A", "binding_id": "b",
                                 "native_key": f'{{"id":{n}}}', "audience_key": "api:x",
                                 "first_recorded_at": "2026-10-01T00:00:00.000000Z"}
                                for n in (1, 2)])
        return builder

    def test_every_text_reads_back_byte_for_byte_by_either_path(self):
        rows = [link(f"l{i}", r, e) for i, (r, e) in enumerate(self.TEXTS)]
        with tempfile.TemporaryDirectory() as tmp:
            builder = self.store(tmp)
            try:
                conn = builder.connection
                builder.add("links", rows)
                conn.executemany(
                    f"INSERT INTO links ({', '.join(store_ddl.LINK_COLUMNS)}) VALUES "
                    f"({', '.join('?' for _ in store_ddl.LINK_COLUMNS)})",
                    [tuple(dict(r, link_id="v" + r["link_id"])[c] for c in store_ddl.LINK_COLUMNS)
                     for r in rows])
                for row in rows:
                    for prefix in ("", "v"):
                        got = conn.execute("SELECT reason, evidence_handles FROM links "
                                           "WHERE link_id = ?", (prefix + row["link_id"],)
                                           ).fetchone()
                        self.assertEqual(got, (row["reason"], row["evidence_handles"]),
                                         prefix + row["link_id"])
                # the packed path stored at least the shared texts once
                self.assertGreater(conn.execute("SELECT count(*) FROM link_texts").fetchone()[0], 0)
            finally:
                builder.abort()

    def test_the_packer_declines_what_it_cannot_hold_exactly(self):
        self.assertIsNone(pack_link_texts('[1]', '[2]'))
        self.assertIsNone(pack_link_texts(None, '["a"]'))
        reason, evidence = self.TEXTS[5]
        packed = pack_link_texts(reason, evidence)
        self.assertIsNotNone(packed)
        # more strings than slots: the longest take the slots, the rest stay in the skeleton
        self.assertEqual(len(packed[2]), store_ddl.LINK_SLOTS)
        lengths = sorted(map(len, json.loads(reason) + json.loads(evidence)))
        self.assertEqual(sorted(map(len, packed[2])), lengths[-store_ddl.LINK_SLOTS:])
        self.assertEqual(packed[3], 0)   # the evidence strings are the shortest here

    def test_a_text_v5_refused_is_refused_by_either_path(self):
        bad = [('not json', '["a"]'), ('[]', '["a"]'), ('{"a":"b"}', '["a"]'),
               ('["a"]', '[]'), ('["a"]', 'nope'), ('["bad \\q escape"]', '["a"]'),
               ('["a"]', '["\\x"]')]
        for reason, evidence in bad:
            for path in ("builder", "view"):
                with self.subTest(reason=reason, evidence=evidence, path=path), \
                        tempfile.TemporaryDirectory() as tmp:
                    builder = self.store(tmp)
                    try:
                        row = link("x", reason, evidence)
                        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK constraint"):
                            if path == "builder":
                                builder.add("links", [row])
                            else:
                                builder.connection.execute(
                                    f"INSERT INTO links ({', '.join(row)}) VALUES "
                                    f"({', '.join('?' for _ in row)})", tuple(row.values()))
                        self.assertEqual(builder.connection.execute(
                            "SELECT count(*) FROM links").fetchone()[0], 0)
                    finally:
                        builder.abort()

    def test_update_and_delete_through_the_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = self.store(tmp)
            try:
                conn = builder.connection
                builder.add("links", [link("a", *self.TEXTS[0]), link("b", *self.TEXTS[1])])
                conn.execute("UPDATE links SET tier = 'corroborated', reason = ? "
                             "WHERE link_id = 'a'", ('["new","api://x@1"]',))
                self.assertEqual(conn.execute(
                    "SELECT tier, reason, evidence_handles FROM links WHERE link_id = 'a'"
                    ).fetchone(), ("corroborated", '["new","api://x@1"]', self.TEXTS[0][1]))
                self.assertEqual(conn.execute(
                    "SELECT reason, evidence_handles FROM links WHERE link_id = 'b'").fetchone(),
                    self.TEXTS[1])
                conn.execute("DELETE FROM links WHERE link_id = 'a'")
                self.assertEqual([r[0] for r in conn.execute("SELECT link_id FROM links")], ["b"])
            finally:
                builder.abort()


VALUE = {"object_id": "A:b:1", "property": "p", "value_type": "text", "value": "v",
         "unit": None, "quantity_role": None, "currency": None, "tax_basis": None,
         "currency_citation": None, "l1_handle": "api://x@1", "field_path": "row.p",
         "digest": "d", "lineage": "silver_view", "binding_id": "b", "parser": "plain",
         "authority_rank": None, "markings": "[]", "recorded_at": "2026-10-01T00:00:00.000000Z",
         "superseded_at": None}
LATER = "2026-10-02T00:00:00.000000Z"


class ValueWriteTests(unittest.TestCase):

    def builder(self, tmp: str) -> StoreBuilder:
        builder = StoreBuilder(Path(tmp) / "s.sqlite")
        builder.add("objects", [{"object_id": "A:b:1", "object_type": "A", "binding_id": "b",
                                 "native_key": '{"id":1}', "audience_key": "api:x",
                                 "first_recorded_at": VALUE["recorded_at"]}])
        builder.add("object_versions", [{"object_id": "A:b:1", "l1_handle": h, "digest": "d",
                                         "lineage": "silver_view", "markings": "[]",
                                         "recorded_at": begin, "superseded_at": end}
                                        for h, begin, end in (
                                            ("api://x@1", VALUE["recorded_at"], LATER),
                                            ("api://x@2", LATER, None))])
        return builder

    def insert(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        conn.execute(f"INSERT INTO object_values ({', '.join(row)}) VALUES "
                     f"({', '.join('?' for _ in row)})", tuple(row.values()))

    def test_the_logical_key_stays_unique_by_either_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = self.builder(tmp)
            try:
                conn = builder.connection
                builder.add("object_values", [VALUE])
                with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE"):
                    self.insert(conn, dict(VALUE, value="other"))
                with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE"):
                    builder.add("object_values", [dict(VALUE, value="other")])
                # a second source of the same version naming the same field is caught
                builder.add("object_values", [dict(VALUE, digest="d2", value="other")])
                self.assertIn("object_values_duplicate_key:A:b:1", builder.check())
            finally:
                builder.abort()

    def test_update_keeps_the_row_number_and_delete_frees_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = self.builder(tmp)
            try:
                conn = builder.connection
                self.insert(conn, VALUE)
                self.insert(conn, dict(VALUE, property="q", field_path="row.q", markings=None))
                number = conn.execute("SELECT row_no FROM object_values_numbered "
                                      "WHERE property = 'p'").fetchone()[0]
                conn.execute("UPDATE object_values SET value = 'w', l1_handle = 'api://x@2' "
                             "WHERE property = 'p'")
                self.assertEqual(conn.execute(
                    "SELECT row_no, value, l1_handle FROM object_values_numbered "
                    "WHERE property = 'p'").fetchone(), (number, "w", "api://x@2"))
                self.assertEqual(conn.execute("SELECT markings FROM object_values WHERE "
                                              "property = 'q'").fetchone()[0], "[]")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE"):
                    conn.execute("UPDATE object_values SET l1_handle = 'api://x@2' "
                                 "WHERE property = 'q'")
                    conn.execute("UPDATE object_values SET property = 'p', field_path = "
                                 "'row.p' WHERE property = 'q'")
                conn.execute("DELETE FROM object_values")
                self.assertEqual(conn.execute("SELECT count(*) FROM value_sources").fetchone()[0], 0)
                self.assertEqual(builder.check(), [])
            finally:
                builder.abort()


class PackedValueTests(unittest.TestCase):
    """v10 packed records: a ``value_storage: packed`` binding's values read back exactly,
    are not word-indexed, keep one field per source, and a direct write unpacks them."""

    def builder(self, tmp: str) -> StoreBuilder:
        builder = ValueWriteTests.builder(ValueWriteTests(), tmp)
        builder.packed_bindings = frozenset({"b"})
        return builder

    ROWS = [dict(VALUE, property=f"p{n}", field_path=f"row.p{n}", value=value, superseded_at=LATER)
            for n, value in enumerate(["v", None, "  padded  ", "0", "-7.700", "é \"quoted\" [x]",
                                       "", "line\nbreak"])]

    def test_a_record_reads_back_every_value_and_is_never_word_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = self.builder(tmp)
            try:
                conn = builder.connection
                money = dict(VALUE, property="amt", field_path="row.amt", value_type="money",
                             superseded_at=LATER,
                             value="12.50", currency="CAD", parser="decimal_text")
                builder.add("object_values", self.ROWS + [money])
                self.assertEqual(logical(conn, "object_values"), collections.Counter(
                    tuple(r.get(c) for c in store_ddl.VALUE_COLUMNS) for r in self.ROWS + [money]))
                self.assertEqual(conn.execute("SELECT count(*) FROM value_cells WHERE field IS NULL"
                                              ).fetchone()[0], 1)
                # the currency-bearing value is a cell; nothing of the record is in the index
                self.assertEqual([r[0] for r in conn.execute(
                    "SELECT property FROM object_values_numbered")], ["amt"])
                for word in ("padded", "quoted", "line"):
                    self.assertEqual(conn.execute("SELECT count(*) FROM value_text WHERE "
                                                  "value_text MATCH ?", (word,)).fetchone()[0], 0)
                # read by object, by property and by version, as every reader does
                self.assertEqual(conn.execute("SELECT value FROM object_values WHERE object_id = ? "
                                              "AND property = 'p4'", ("A:b:1",)).fetchone(),
                                 ("-7.700",))
                self.assertEqual(conn.execute("SELECT count(*) FROM object_values WHERE property "
                                              "= 'p1' AND value IS NULL").fetchone()[0], 1)
                self.assertEqual(builder.check(), [])
            finally:
                builder.abort()

    def test_one_field_per_source_whether_packed_or_a_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = self.builder(tmp)
            try:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE"):
                    builder.add("object_values", [self.ROWS[0], dict(self.ROWS[0], value="x")])
            finally:
                builder.abort()
        with tempfile.TemporaryDirectory() as tmp:
            builder = self.builder(tmp)
            try:
                builder.add("object_values", self.ROWS)
                for write in (lambda: builder.add("object_values", [dict(self.ROWS[3], value="1")]),
                              lambda: ValueWriteTests.insert(ValueWriteTests(), builder.connection,
                                                             dict(self.ROWS[3], value="1"))):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE"):
                        write()
                self.assertEqual(builder.check(), [])
            finally:
                builder.abort()

    def test_update_and_delete_through_the_view_unpack_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = self.builder(tmp)
            try:
                conn = builder.connection
                builder.add("object_values", self.ROWS)
                before = logical(conn, "object_values")
                conn.execute("UPDATE object_values SET value = 'changed' WHERE property = 'p2'")
                expect = collections.Counter(
                    tuple(r.get(c) for c in store_ddl.VALUE_COLUMNS) for r in
                    [dict(r, value="changed") if r["property"] == "p2" else r for r in self.ROWS])
                self.assertEqual(logical(conn, "object_values"), expect)
                self.assertNotEqual(logical(conn, "object_values"), before)
                self.assertEqual(conn.execute("SELECT count(*) FROM value_cells WHERE field IS NULL"
                                              ).fetchone()[0], 0)
                conn.execute("DELETE FROM object_values WHERE property = 'p5'")
                self.assertEqual(sorted(r[0] for r in conn.execute(
                    "SELECT property FROM object_values")),
                    sorted(r["property"] for r in self.ROWS if r["property"] != "p5"))
                self.assertEqual(builder.check(), [])
                conn.execute("DELETE FROM object_values")
                self.assertEqual(conn.execute("SELECT count(*) FROM value_sources").fetchone()[0], 0)
            finally:
                builder.abort()


if __name__ == "__main__":
    unittest.main()
