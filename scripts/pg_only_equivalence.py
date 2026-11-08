"""Equivalence of Postgres-only serving (``PLX_ONTOLOGY_PG=only``) with the SQLite readers on
a sample of a real generation: the word index (values and document text) and the read
tools' word, chunk and find reads, under a full and a restricted reader.

    python3 -m scripts.pg_only_equivalence sample STORE OUT.sqlite [--objects 60000]
        [--fields 30000] [--seed 11]
    python3 -m scripts.pg_only_equivalence compare OUT.sqlite [--queries FILE ...]
        [--vocab 400] [--keep-db]

``sample`` copies a seeded random sample of ``STORE`` (read only) into a store of the same
DDL: random runs of objects and of document text fields, with every row of theirs the
projection keeps (versions, value sources and cells, identifiers, events, links and their
texts, text fields and chunks; the small tables whole). Its FTS5 tables are filled the way
the build fills them (``value_text`` by the value triggers, ``chunk_text`` from the chunks'
text), so the sample's word index is the SQLite store's own.

``compare`` loads the sample into a scratch database it creates and drops
(``operating.tests.pg_testdb``; every migration, every load step, the word index among
them), opens a ``PgStoreReader`` with Postgres the only store and a ``StoreReader`` on the
sample, and compares, for each query (the bench files' queries cut into FTS5 queries, plus
``--vocab`` words and prefixes drawn from the sample's own text and values):

* the FTS5 rows of ``value_text`` (as object, property, value) and ``chunk_text`` (chunk
  ids), every match;
* ``text_matches`` and ``chunk_matches`` (the tools' paged reads with the reader's
  permission predicates), the first ``PAGES`` pages in the order read (a page cut inside
  a value source is the same cut), for a full and a restricted reader;
* ``plx.find`` {query} answers (meta and cursors removed) for both readers.

It prints a JSON report: counts equal / differing and the first differing queries. Nothing
outside the scratch database is written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
from typing import Any, Sequence

_FTS_SHADOW = re.compile(r"^(value_text|chunk_text)_(data|idx|config|docsize|content)$")
# the pages of a paged read compared per query (each 200 rows; the raw FTS5 rows above are
# compared whole)
PAGES = 3
_WHOLE = ("store_meta", "value_fields", "value_kinds", "value_layouts", "value_layout_fields",
          "link_skeletons")


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _copy(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    cols = ", ".join(_columns(conn, table, "g"))
    try:
        cur = conn.execute(f"INSERT OR IGNORE INTO main.{table} (rowid, {cols}) "
                           f"SELECT rowid, {cols} FROM g.{table} {where}")
    except sqlite3.OperationalError:   # a table without rowid
        cur = conn.execute(f"INSERT OR IGNORE INTO main.{table} ({cols}) "
                           f"SELECT {cols} FROM g.{table} {where}")
    return cur.rowcount


def sample(store: str, out: str, *, objects: int, fields: int, seed: int) -> dict[str, Any]:
    from ontology.text_index import chunk_text
    rng = random.Random(seed)
    Path(out).unlink(missing_ok=True)
    conn = sqlite3.connect(out, isolation_level=None)
    conn.execute("ATTACH DATABASE ? AS g", (f"file:{store}?mode=ro",))
    ddl = conn.execute("SELECT type, name, sql FROM g.sqlite_master WHERE sql IS NOT NULL "
                       "AND name NOT LIKE 'sqlite_%'").fetchall()
    order = {"table": 0, "index": 1, "view": 2, "trigger": 3}
    conn.execute("BEGIN")
    for kind, name, sql in sorted(ddl, key=lambda r: order.get(r[0], 9)):
        if kind == "table" and _FTS_SHADOW.match(name):
            continue
        conn.execute(sql)
    report: dict[str, Any] = {}
    for table in _WHOLE:
        report[table] = _copy(conn, table)
    conn.execute("CREATE TEMP TABLE sel (object_id TEXT PRIMARY KEY)")
    top = conn.execute("SELECT max(rowid) FROM g.objects").fetchone()[0]
    for _ in range(30):
        low = rng.randrange(1, max(2, top - objects // 30))
        conn.execute("INSERT OR IGNORE INTO sel SELECT object_id FROM g.objects WHERE rowid >= ? "
                     "AND rowid < ?", (low, low + objects // 30))
    top = conn.execute("SELECT max(field_id) FROM g.text_fields").fetchone()[0] or 0
    for _ in range(15):
        low = rng.randrange(1, max(2, top - fields // 15))
        conn.execute("INSERT OR IGNORE INTO sel SELECT object_id FROM g.text_fields WHERE "
                     "field_id >= ? AND field_id < ?", (low, low + fields // 15))
    sel = "(SELECT object_id FROM temp.sel)"
    report["objects"] = _copy(conn, "objects", f"WHERE object_id IN {sel}")
    report["object_removals"] = _copy(conn, "object_removals", f"WHERE object_id IN {sel}")
    report["object_versions"] = _copy(conn, "object_versions", f"WHERE object_id IN {sel}")
    report["value_sources"] = _copy(conn, "value_sources", "WHERE ver IN (SELECT rowid FROM "
                                    "main.object_versions)")
    report["value_cells"] = _copy(conn, "value_cells", "WHERE src IN (SELECT src FROM "
                                  "main.value_sources)")
    report["identifiers"] = _copy(conn, "identifiers", f"WHERE object_id IN {sel}")
    report["event_objects"] = _copy(conn, "event_objects", f"WHERE object_id IN {sel}")
    report["events"] = _copy(conn, "events", "WHERE event_id IN (SELECT event_id FROM "
                             "main.event_objects)")
    report["link_rows"] = _copy(conn, "link_rows", f"WHERE from_object IN {sel}")
    report["link_rows"] += _copy(conn, "link_rows", f"WHERE to_object IN {sel}")
    slots = [c for c in _columns(conn, "link_rows") if re.fullmatch(r"s\d+", c)]
    conn.execute("CREATE TEMP TABLE texts (id INTEGER PRIMARY KEY)")
    for slot in slots:
        conn.execute(f"INSERT OR IGNORE INTO texts SELECT {slot} FROM main.link_rows "
                     f"WHERE {slot} IS NOT NULL")
    report["link_texts"] = _copy(conn, "link_texts", "WHERE id IN (SELECT id FROM temp.texts)")
    report["text_fields"] = _copy(conn, "text_fields", f"WHERE object_id IN {sel}")
    report["text_chunks"] = _copy(conn, "text_chunks", "WHERE field_id IN (SELECT field_id "
                                  "FROM main.text_fields)")
    for chunk_id, body in conn.execute("SELECT chunk_id, body FROM main.text_chunks").fetchall():
        conn.execute("INSERT INTO chunk_text (rowid, body) VALUES (?, ?)",
                     (chunk_id, chunk_text(body)))
    conn.execute("COMMIT")
    conn.execute("DETACH DATABASE g")
    conn.execute("VACUUM")
    conn.close()
    return report


# ---------------------------------------------------------------------------- queries
def _bench_queries(files: Sequence[str]) -> list[str]:
    """FTS5 queries of the bench files' text: each query's words, AND of quoted prefixes
    as ``plx.documents`` writes them, and each word alone."""
    texts: list[str] = []
    for name in files:
        path = Path(name)
        for file in ([path] if path.is_file() else sorted(path.rglob("q.json"))):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            calls = data if isinstance(data, list) else [[None, data]]
            for call in calls:
                args = call[1] if isinstance(call, list) and len(call) > 1 else call
                if isinstance(args, dict):
                    texts += [str(args[k]) for k in ("query", "question") if args.get(k)]
    out: list[str] = []
    for text in texts:
        words = [w for w in re.findall(r"[A-Za-z0-9À-￿]+", text) if len(w) > 2]
        if not words:
            continue
        out.append(" AND ".join(f'"{w}"' + ("" if any(c.isdigit() for c in w) else "*")
                                for w in words[:4]))
        out += [f'"{w}"' for w in words[:6]]
    return list(dict.fromkeys(out))


def _vocab_queries(conn: sqlite3.Connection, count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    terms: list[str] = []
    for table in ("value_text", "chunk_text"):
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.v_{table} USING "
                     f"fts5vocab(main, {table}, 'row')")
        rows = [r[0] for r in conn.execute(f"SELECT term FROM temp.v_{table}")]
        terms += rng.sample(rows, min(count // 2, len(rows)))
    out = []
    for term in terms:
        out.append('"' + term.replace('"', '""') + '"')
        if len(term) > 3:
            out.append('"' + term[:3].replace('"', '""') + '"*')
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------- compare
def _policies(conn: sqlite3.Connection) -> tuple[Any, Any, Any, Any]:
    from ontology.store_ddl import MARKINGS
    from ontology.types import Actor
    from operating.ontology_tools import ReadPolicy
    prefixes = sorted({k.split(":", 1)[0] for (k,) in
                       conn.execute("SELECT DISTINCT audience_key FROM objects")})
    audiences = {f"{p}:*": {"full"} for p in prefixes}
    for p in ("api", "bc"):
        audiences.setdefault(f"{p}:*", set()).add("part")
    policy = ReadPolicy(tenant="plx", audiences=audiences,
                        markings={m: {"full"} for m in MARKINGS})
    return policy, Actor("plx", frozenset({"full"})), Actor("plx", frozenset({"part"})), \
        prefixes


def _clean(value: Any) -> Any:
    volatile = {"meta", "cursor", "next_cursor", "set", "set_handle", "timings",
                "elapsed_ms", "served_at", "warm"}
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in sorted(value.items()) if k not in volatile}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _rows(conn: Any, sql: str, query: str) -> Any:
    try:
        return sorted(tuple(r) for r in conn.execute(sql, (query,)).fetchall())
    except sqlite3.OperationalError as error:
        return ("error", str(error)[:60].split(":")[0])


def compare(sample_path: str, *, queries: Sequence[str], vocab: int, keep: bool,
            seed: int = 11) -> dict[str, Any]:
    import psycopg
    from ontology import identifiers
    from operating.ontology_tools import (OntologyTools, StoreReader, default_schema,
                                          load_contracts)
    from operating.pg import ontology_load, ontology_reader
    from operating.pg.migrate import MIGRATIONS_DIR
    from operating.pg.sqlite_compat import PgCompatConnection
    from operating.pg import scratch_db as pg_testdb

    report: dict[str, Any] = {"sample": sample_path}
    name, dsn = pg_testdb.create_db()
    try:
        with psycopg.connect(dsn, autocommit=True) as pg:
            for migration in sorted(MIGRATIONS_DIR.glob("00*.sql")):
                if migration.name >= "0006":
                    pg.execute(migration.read_text(encoding="utf-8"))
        started = time.monotonic()
        load = ontology_load.load(sample_path, dsn, workers=4, log=open("/dev/null", "w"))
        report["load_seconds"] = round(time.monotonic() - started, 1)
        report["load"] = {t: {k: v for k, v in r.items() if k in ("rows", "bytes")}
                          for t, r in load.items()}
        report["whole_problem"] = ontology_reader.whole(dsn)
        lite = StoreReader.open(sample_path)
        pgr = ontology_reader.PgStoreReader.open(dsn, only=True)
        assert pgr.words_loaded() and pgr.chunk_words_loaded()
        qs = list(dict.fromkeys([*_bench_queries(queries),
                                 *_vocab_queries(lite.conn, vocab, seed)]))
        report["queries"] = len(qs)
        # 1. the FTS5 rows themselves
        raw = PgCompatConnection(dsn)
        raw.words = True
        checks = {
            "value_text": (
                "SELECT v.object_id, v.property, v.value FROM value_text f JOIN "
                "object_values_numbered v ON v.row_no = f.rowid WHERE value_text MATCH ?",
                "SELECT v.object_id, v.property, v.value FROM value_text f JOIN "
                "object_values_numbered v ON v.src = f.src AND v.field = f.field "
                "WHERE value_text MATCH ?"),
            "chunk_text": ("SELECT f.rowid FROM chunk_text f WHERE chunk_text MATCH ?",) * 2,
        }
        for table, (lite_sql, pg_sql) in checks.items():
            equal, differ, rows = 0, [], 0
            for q in qs:
                a, b = _rows(lite.conn, lite_sql, q), _rows(raw, pg_sql, q)
                if a == b:
                    equal += 1
                    rows += len(a) if isinstance(a, list) else 0
                else:
                    differ.append({"query": q, "sqlite": len(a), "postgres": len(b)})
            report[f"fts.{table}"] = {"equal": equal, "differing": len(differ),
                                      "matched_rows": rows, "first": differ[:5]}
        # 2. the tools' paged reads and 3. plx.find, per reader
        policy, full, part, prefixes = _policies(lite.conn)
        report["audience_prefixes"] = prefixes
        tools = {}
        for side, reader in (("sqlite", lite), ("postgres", pgr)):
            tools[side] = OntologyTools(reader, policy, schema=default_schema(),
                                        grammar=identifiers, contracts=load_contracts(),
                                        item_gate=lambda a, h: True)
        at = dt.datetime.now(dt.timezone.utc)
        spent: dict[str, list[float]] = {}
        for who, actor in (("full", full), ("restricted", part)):
            scopes = {s: t._scope(actor, at) for s, t in tools.items()}
            for method in ("text_matches", "chunk_matches"):
                equal, differ, rows = 0, [], 0
                for q in qs:
                    got = {}
                    for side, reader in (("sqlite", lite), ("postgres", pgr)):
                        out, after, pages = [], 0, 0
                        started = time.monotonic()
                        try:
                            while True:
                                pages += 1
                                args = ((None, after, 200) if method == "chunk_matches"
                                        else (None, after, 200))
                                page = getattr(reader, method)(scopes[side], q, *args)
                                if method == "text_matches":
                                    out += [(r["object_id"], r["property"], r["value"],
                                             r["l1_handle"]) for r in page]
                                else:
                                    out += [(r["row_no"], r["object_id"], r["citation"])
                                            for r in page]
                                if len(page) < 200 or pages >= PAGES:
                                    break
                                after = page[-1]["row_no"]
                        except sqlite3.OperationalError as error:
                            out = [("error", str(error)[:40])]
                        got[side] = out   # in the order read: a cut page is the same page
                        spent.setdefault(f"{method}.{side}", []).append(
                            time.monotonic() - started)
                    if got["sqlite"] == got["postgres"]:
                        equal += 1
                        rows += len(got["sqlite"])
                    else:
                        differ.append({"query": q, "sqlite": len(got["sqlite"]),
                                       "postgres": len(got["postgres"])})
                report[f"{method}.{who}"] = {"equal": equal, "differing": len(differ),
                                             "rows": rows, "first": differ[:5]}
            equal, differ = 0, []
            for q in qs[:150]:
                text = " ".join(re.findall(r"[^\s\"*()]+", q.replace(" AND ", " ")))
                answers = {}
                for side, t in tools.items():
                    try:
                        answers[side] = _clean(t.call("plx.find", {"query": text}, actor))
                    except Exception as error:  # noqa: BLE001 - compared as such
                        answers[side] = ("raised", type(error).__name__)
                if answers["sqlite"] == answers["postgres"]:
                    equal += 1
                else:
                    differ.append(text)
            report[f"plx.find.{who}"] = {"equal": equal, "differing": len(differ),
                                         "first": differ[:5]}
        report["seconds_per_query"] = {
            k: {"p50": round(sorted(v)[len(v) // 2], 3),
                "p90": round(sorted(v)[min(len(v) - 1, len(v) * 9 // 10)], 3),
                "max": round(max(v), 3),
                "total": round(sum(v), 1)} for k, v in spent.items()}
        raw.close()
        pgr.close()
        # fail closed: an ontology that is not whole refuses the reader
        with psycopg.connect(dsn, autocommit=True) as pg:
            pg.execute("DELETE FROM ontology.load_state WHERE name = 'table:chunk_words'")
        try:
            ontology_reader.PgStoreReader.open(dsn, only=True).close()
            report["fail_closed"] = False
        except ontology_reader.PostgresNotWhole as error:
            report["fail_closed"] = str(error)
    finally:
        if not keep:
            pg_testdb.drop_db(name)
        else:
            report["kept_db"] = name
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pg_only_equivalence")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample")
    s.add_argument("store")
    s.add_argument("out")
    s.add_argument("--objects", type=int, default=60000)
    s.add_argument("--fields", type=int, default=30000)
    s.add_argument("--seed", type=int, default=11)
    c = sub.add_parser("compare")
    c.add_argument("sample")
    c.add_argument("--queries", nargs="*", default=[])
    c.add_argument("--vocab", type=int, default=400)
    c.add_argument("--keep-db", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "sample":
        out = sample(args.store, args.out, objects=args.objects, fields=args.fields,
                     seed=args.seed)
    else:
        out = compare(args.sample, queries=args.queries, vocab=args.vocab, keep=args.keep_db)
    print(json.dumps(out, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
