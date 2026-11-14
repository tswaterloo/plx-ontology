"""The documents whose ``mentions`` links a step migration can change, so the in-place step
run (``ontology.pg_step_inplace run``) resolves only those instead of all 5.8M.

A mention's links are a function of the document (its extracted mentions: the warm
extraction cache, ``ontology.mention_cache``), the declared mention link types, the
resolver code, and what resolving reads of the store: the identifier-index rows answering
each candidate key with their objects' version digests and markings
(``mentions._store_candidates``), and the corroboration's reads of each target (its
``verified`` / ``reviewed`` links, its values through the version digest, the party
domains store-wide, the conversation's other messages). A document is in scope when

* ``link_types``: it holds a served mention link of a link type the new schema no longer
  maps the same (``mentions.mention_link_types``: a ``written_by_step`` binding);
* ``reference``: one of its extracted mentions' candidate keys is held by a row of a
  property declared ``identifies: reference`` (the resolver now drops such rows beside an
  owner and vetoes a link through one: ``mentions._store_candidates``, ``is_reference``);
* ``changed``: one of its candidate keys is held, before or after the unit migration, by
  an object whose identifier rows, versions (digest, markings) or verified / reviewed
  links differ between the two :func:`snapshot` files taken around the unit applies, or
  whose markings the marks-only apply changed (its undo files);
* ``own``: the document's own object changed (same comparison);
* ``thread``: it shares a conversation with a document in scope (the corroboration reads
  the conversation's other messages' mention links).

What this cannot bound fails closed (:class:`ScopeRefused`, the caller runs the whole
step): a party-domain holder set that differs between the snapshots, or a document an
object holds that the cache does not (:func:`uncached_documents`: its mentions are
unknown). A document no object holds is no refusal: no run links it (:func:`documents`).

``python3 -m operating.pg.mention_scope snapshot --bindings FILE --out FILE`` reads only
(one ``REPEATABLE READ READ ONLY`` transaction) and refuses while a chunked migration is
recorded in progress when ``--before`` is given (the rows it reads would be half-applied).

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

from ontology import parent_watch


class ScopeRefused(RuntimeError):
    """The scope cannot be bounded: the caller re-derives every document."""


# One object's digest: what resolving and corroborating a mention reads of a candidate.
_OBJECT_DIGEST = """
SELECT o.object_id, o.object_type, md5(concat_ws('|', o.object_type,
  (SELECT string_agg(concat_ws(',', i.identifier_kind, i.normalized, i.property, i.role,
                               i.l1_handle, i.field_path, i.lineage, i.recorded_at,
                               i.superseded_at), ';'
                     ORDER BY i.identifier_kind, i.normalized, i.l1_handle, i.field_path)
     FROM ontology.identifiers i WHERE i.object_id = o.object_id),
  (SELECT string_agg(concat_ws(',', v.l1_handle, v.digest, v.lineage, v.markings,
                               v.recorded_at, v.superseded_at), ';'
                     ORDER BY v.l1_handle, v.recorded_at, v.ver)
     FROM ontology.object_versions v WHERE v.object_id = o.object_id),
  (SELECT string_agg(concat_ws(',', l.link_id, l.link_type, l.to_object, l.tier,
                               l.binding_kind, l.markings, l.recorded_at, l.superseded_at,
                               l.evidence_handles, l.evidence_id, l.slots::text), ';'
                     ORDER BY l.link_id)
     FROM ontology.link_rows l WHERE l.from_object = o.object_id
      AND l.tier IN ('verified', 'reviewed'))))
  FROM ontology.objects o WHERE o.binding_id = %s"""
_KEYS = ("SELECT DISTINCT i.identifier_kind, i.normalized, i.object_id FROM "
         "ontology.identifiers i JOIN ontology.objects o ON o.object_id = i.object_id "
         "WHERE o.binding_id = %s")


def _snapshot_part(dsn: str, jobs: Sequence[tuple[str, int, int]], out: str) -> dict[str, Any]:
    """``jobs`` (binding, part, parts: the binding's objects whose id hashes to ``part``)
    read in one ``REPEATABLE READ READ ONLY`` transaction into ``out``; refused while a
    chunked migration is in progress (the rows would be half-applied)."""
    import psycopg
    db = sqlite3.connect(out)
    db.executescript("CREATE TABLE objects (object_id TEXT, object_type TEXT, digest TEXT); "
                     "CREATE TABLE keys (kind TEXT, normalized TEXT, object_id TEXT)")
    counts = {"objects": 0, "keys": 0}
    with psycopg.connect(dsn) as pg:
        pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        pg.execute("SET LOCAL jit = off")
        if pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                      "'migration_in_progress'").fetchone() is not None:
            raise ScopeRefused("migration_in_progress")
        for binding, part, parts in jobs:
            cond = ("" if parts == 1 else
                    f" AND abs(hashtext(o.object_id)::bigint) %% {int(parts)} = {int(part)}")
            rows = pg.execute(_OBJECT_DIGEST + cond, (binding,)).fetchall()
            db.executemany("INSERT INTO objects VALUES (?, ?, ?)", rows)
            keys = pg.execute(_KEYS.replace("o.binding_id = %s", "o.binding_id = %s" + cond),
                              (binding,)).fetchall()
            db.executemany("INSERT INTO keys VALUES (?, ?, ?)", keys)
            counts["objects"] += len(rows)
            counts["keys"] += len(keys)
    db.commit()
    db.close()
    return counts


def snapshot(dsn: str, bindings: Sequence[str], out: str | Path, *, before: bool,
             domain_property: str, log: Any = None, workers: int = 1) -> dict[str, Any]:
    """Write ``out`` (SQLite, replaced): every object of ``bindings`` with its digest
    (``objects``), its identifier keys (``keys``), and the party-domain values store-wide
    (``domains``). ``workers`` > 1: the bindings (a large one split by object id hash)
    read by that many processes, each in its own read-only transaction; only while no
    migration is in progress (checked by each), so the rows they read are the same."""
    if workers > 1:
        return _snapshot_parallel(dsn, bindings, out, domain_property=domain_property,
                                  log=log, workers=workers)
    import psycopg
    out = Path(out)
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("CREATE TABLE objects (object_id TEXT PRIMARY KEY, object_type TEXT, "
                     "digest TEXT); CREATE TABLE keys (kind TEXT, normalized TEXT, "
                     "object_id TEXT); CREATE TABLE domains (object_id TEXT, value TEXT); "
                     "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    started = time.monotonic()
    report: dict[str, Any] = {"bindings": len(bindings), "objects": 0, "keys": 0}
    with psycopg.connect(dsn) as pg:
        pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        pg.execute("SET LOCAL jit = off")
        row = pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                         "'migration_in_progress'").fetchone()
        if before and row is not None:
            raise ScopeRefused("migration_in_progress")
        report["in_progress"] = row is not None
        for k in ("schema_digest", "steps_digest"):
            got = pg.execute("SELECT value FROM ontology.store_meta WHERE key = %s",
                             (k,)).fetchone()
            report[k] = got[0] if got else None
        for n, binding in enumerate(bindings):
            rows = pg.execute(_OBJECT_DIGEST, (binding,)).fetchall()
            db.executemany("INSERT OR REPLACE INTO objects VALUES (?, ?, ?)", rows)
            keys = pg.execute(_KEYS, (binding,)).fetchall()
            db.executemany("INSERT INTO keys VALUES (?, ?, ?)", keys)
            report["objects"] += len(rows)
            report["keys"] += len(keys)
            if log is not None:
                print(f"{n + 1}/{len(bindings)} {binding}: {len(rows)} objects, "
                      f"{len(keys)} keys", file=log, flush=True)
        domains = pg.execute("SELECT object_id, value FROM ontology.object_values WHERE "
                             "property = %s", (domain_property,)).fetchall()
        db.executemany("INSERT INTO domains VALUES (?, ?)", domains)
        report["domains"] = len(domains)
    db.execute("CREATE INDEX keys_by_object ON keys (object_id)")
    report["seconds"] = round(time.monotonic() - started, 1)
    db.executemany("INSERT INTO meta VALUES (?, ?)",
                   [(k, json.dumps(v)) for k, v in report.items()])
    db.commit()
    db.close()
    tmp.replace(out)
    return report


def changed_objects(before: str | Path, after: str | Path) -> tuple[set[str], set[tuple[str, str]]]:
    """(objects whose digest differs or that one side holds only, the identifier keys any
    of them holds on either side)."""
    a = sqlite3.connect(f"file:{before}?mode=ro", uri=True)
    try:
        a.execute("ATTACH DATABASE ? AS b", (f"file:{after}?mode=ro",))
        changed = {r[0] for r in a.execute(
            "SELECT x.object_id FROM main.objects x LEFT JOIN b.objects y USING (object_id) "
            "WHERE y.digest IS NOT x.digest UNION SELECT y.object_id FROM b.objects y "
            "LEFT JOIN main.objects x USING (object_id) WHERE x.object_id IS NULL")}
        keys: set[tuple[str, str]] = set()
        for schema in ("main", "b"):
            for kind, value, oid in a.execute(f"SELECT kind, normalized, object_id FROM "
                                              f"{schema}.keys"):
                if oid in changed:
                    keys.add((kind, value))
        return changed, keys
    finally:
        a.close()


def domains_changed(before: str | Path, after: str | Path) -> list[str]:
    """The party domains whose holder set differs between the snapshots."""
    from ontology.mentions import website_domain
    held: list[dict[str, set[str]]] = []
    for path in (before, after):
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            out: dict[str, set[str]] = {}
            for oid, value in db.execute("SELECT object_id, value FROM domains"):
                domain = website_domain(value)
                if domain:
                    out.setdefault(domain, set()).add(oid)
            held.append(out)
        finally:
            db.close()
    return sorted(d for d in set(held[0]) | set(held[1])
                  if held[0].get(d) != held[1].get(d))


def marked_objects(undo: str | Path) -> set[str]:
    """The objects whose version markings the marks-only apply changed (its undo files,
    ``<binding>.tsv``: ``v`` lines, a JSON list starting with the object id)."""
    out: set[str] = set()
    for path in sorted(Path(undo).glob("*.tsv")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                tag, _, row = line.partition("\t")
                if tag == "v":
                    out.add(json.loads(row)[0])
    return out


_SPLIT = 50_000   # objects per part of a large binding (parallel snapshot)


def _snapshot_parallel(dsn: str, bindings: Sequence[str], out: str | Path, *,
                       domain_property: str, log: Any, workers: int) -> dict[str, Any]:
    import concurrent.futures as futures
    import multiprocessing
    import psycopg
    import tempfile
    out = Path(out)
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    started = time.monotonic()
    report: dict[str, Any] = {"bindings": len(bindings), "objects": 0, "keys": 0,
                              "workers": workers}
    with psycopg.connect(dsn) as pg:
        pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        if pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                      "'migration_in_progress'").fetchone() is not None:
            raise ScopeRefused("migration_in_progress")
        report["in_progress"] = False
        for k in ("schema_digest", "steps_digest"):
            got = pg.execute("SELECT value FROM ontology.store_meta WHERE key = %s",
                             (k,)).fetchone()
            report[k] = got[0] if got else None
        sizes = dict(pg.execute("SELECT binding_id, count(*) FROM ontology.objects WHERE "
                                "binding_id = ANY(%s::text[]) GROUP BY 1",
                                (list(bindings),)).fetchall())
        domains = pg.execute("SELECT object_id, value FROM ontology.object_values WHERE "
                             "property = %s", (domain_property,)).fetchall()
    jobs = []
    for b in bindings:
        parts = max(1, -(-int(sizes.get(b, 0)) // _SPLIT))
        jobs += [(b, k, parts) for k in range(parts)]
    jobs.sort(key=lambda j: -sizes.get(j[0], 0) // j[2])
    lanes: list[list[tuple[str, int, int]]] = [[] for _ in range(min(workers, len(jobs)) or 1)]
    for n, job in enumerate(jobs):   # round robin, largest first
        lanes[n % len(lanes)].append(job)
    with tempfile.TemporaryDirectory(prefix=".snap-", dir=out.parent) as folder:
        paths = [str(Path(folder) / f"part-{n}.sqlite") for n in range(len(lanes))]
        with futures.ProcessPoolExecutor(max_workers=len(lanes),
                                         mp_context=multiprocessing.get_context("spawn"),
                                         **parent_watch.pool_init()) as pool:
            got = [f.result() for f in [pool.submit(_snapshot_part, dsn, lane, path)
                                        for lane, path in zip(lanes, paths)]]
        db = sqlite3.connect(tmp)
        db.executescript("CREATE TABLE objects (object_id TEXT PRIMARY KEY, object_type TEXT, "
                         "digest TEXT); CREATE TABLE keys (kind TEXT, normalized TEXT, "
                         "object_id TEXT); CREATE TABLE domains (object_id TEXT, value TEXT); "
                         "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        for path in paths:
            db.execute("ATTACH DATABASE ? AS p", (path,))
            db.execute("INSERT OR REPLACE INTO objects SELECT * FROM p.objects")
            db.execute("INSERT INTO keys SELECT * FROM p.keys")
            db.commit()
            db.execute("DETACH DATABASE p")
    report["objects"] = db.execute("SELECT count(*) FROM objects").fetchone()[0]
    report["keys"] = sum(g["keys"] for g in got)
    if report["objects"] != sum(g["objects"] for g in got) or \
            report["objects"] != sum(int(sizes.get(b, 0)) for b in bindings):
        db.close()
        tmp.unlink(missing_ok=True)
        raise ScopeRefused("snapshot_parts_disagree")   # an object read twice or missed
    db.executemany("INSERT INTO domains VALUES (?, ?)", domains)
    report["domains"] = len(domains)
    db.execute("CREATE INDEX keys_by_object ON keys (object_id)")
    report["seconds"] = round(time.monotonic() - started, 1)
    db.executemany("INSERT INTO meta VALUES (?, ?)",
                   [(k, json.dumps(v)) for k, v in report.items()])
    db.commit()
    db.close()
    tmp.replace(out)
    if log is not None:
        print(json.dumps(report, sort_keys=True), file=log, flush=True)
    return report


# ------------------------------------------------------------------ the cache, scanned

def _scan_part(cache: str, xcode: str, keys_db: str, low: int, high: int, out: str) -> str:
    """Rows ``[low, high)`` of the cache's ``extracted``: (document key, citation, whether
    a candidate key of one of its mentions is in ``keys_db``) into ``out``; and of its
    ``groups``: (group key, document key)."""
    import zlib
    from ontology.mentions import candidate_keys
    k = sqlite3.connect(f"file:{keys_db}?mode=ro", uri=True)
    try:
        hit_keys = {(a, b) for a, b in k.execute("SELECT kind, normalized FROM hit_keys")}
    finally:
        k.close()
    src = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    dst.execute("CREATE TABLE docs (doc_key TEXT, citation TEXT, hit INTEGER)")
    dst.execute("CREATE TABLE group_docs (group_key TEXT, doc_key TEXT)")
    rows = []
    for key, value in src.execute("SELECT key, value FROM extracted WHERE rowid >= ? AND "
                                  "rowid < ? AND xcode = ?", (low, high, xcode)):
        entry = json.loads(zlib.decompress(value))
        hit = any(key2 in hit_keys for m in entry.get("mentions") or ()
                  for key2 in candidate_keys(m))
        rows.append((key, entry["document"]["citation"], int(hit)))
    dst.executemany("INSERT INTO docs VALUES (?, ?, ?)", rows)
    rows = []
    for gkey, value in src.execute("SELECT key, value FROM groups WHERE rowid >= ? AND "
                                   "rowid < ?", (low, high)):
        for dkey in json.loads(zlib.decompress(value)) or ():
            rows.append((gkey, dkey))
    dst.executemany("INSERT INTO group_docs VALUES (?, ?)", rows)
    dst.commit()
    dst.close()
    src.close()
    return out


def scan_cache(cache: str | Path, hit_keys: Iterable[tuple[str, str]], out: str | Path,
               workers: int = 8) -> dict[str, Any]:
    """``out`` (SQLite, replaced): every document the extraction cache holds under this
    extractor code with its citation and whether one of its mentions' candidate keys is
    in ``hit_keys``, and every group's documents; scanned by ``workers`` processes."""
    import concurrent.futures as futures
    import multiprocessing
    import tempfile
    from ontology.mention_cache import extractor_code
    out = Path(out)
    out.unlink(missing_ok=True)
    started = time.monotonic()
    src = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
    try:
        top = max(src.execute("SELECT coalesce(max(rowid), 0) FROM extracted").fetchone()[0],
                  src.execute("SELECT coalesce(max(rowid), 0) FROM groups").fetchone()[0]) + 1
    finally:
        src.close()
    with tempfile.TemporaryDirectory(prefix=".scan-", dir=out.parent) as tmp:
        keys_db = Path(tmp) / "keys.sqlite"
        k = sqlite3.connect(keys_db)
        k.execute("CREATE TABLE hit_keys (kind TEXT, normalized TEXT)")
        k.executemany("INSERT INTO hit_keys VALUES (?, ?)", sorted(set(hit_keys)))
        k.commit()
        k.close()
        step = max(1, -(-top // (workers * 8)))
        jobs = [(str(cache), extractor_code(), str(keys_db), a, min(a + step, top),
                 str(Path(tmp) / f"part-{n}.sqlite"))
                for n, a in enumerate(range(0, top, step))]
        with futures.ProcessPoolExecutor(max_workers=workers,
                                         mp_context=multiprocessing.get_context("spawn"),
                                         **parent_watch.pool_init()) as pool:
            parts = [f.result() for f in [pool.submit(_scan_part, *j) for j in jobs]]
        db = sqlite3.connect(out)
        db.execute("CREATE TABLE docs (doc_key TEXT, citation TEXT, hit INTEGER)")
        db.execute("CREATE TABLE group_docs (group_key TEXT, doc_key TEXT)")
        for part in parts:
            db.execute("ATTACH DATABASE ? AS p", (part,))
            db.execute("INSERT INTO docs SELECT * FROM p.docs")
            db.execute("INSERT INTO group_docs SELECT * FROM p.group_docs")
            db.commit()
            db.execute("DETACH DATABASE p")
        db.execute("CREATE INDEX docs_by_key ON docs (doc_key)")
        db.execute("CREATE INDEX docs_by_citation ON docs (citation)")
        db.execute("CREATE INDEX group_docs_by_doc ON group_docs (doc_key)")
        db.execute("CREATE INDEX group_docs_by_group ON group_docs (group_key)")
        report = {"documents": db.execute("SELECT count(*) FROM docs").fetchone()[0],
                  "hits": db.execute("SELECT count(*) FROM docs WHERE hit").fetchone()[0],
                  "groups": db.execute("SELECT count(DISTINCT group_key) FROM group_docs"
                                       ).fetchone()[0],
                  "hit_keys": len(set(hit_keys)),
                  "seconds": round(time.monotonic() - started, 1)}
        db.commit()
        db.close()
    return report


def reference_keys(pg: Any) -> set[tuple[str, str]]:
    """The identifier keys a row of a property declared ``identifies: reference`` holds."""
    from ontology.mentions import _reference_properties
    pairs = sorted(_reference_properties())
    if not pairs:
        return set()
    rows = pg.execute("SELECT DISTINCT i.identifier_kind, i.normalized FROM "
                      "ontology.identifiers i JOIN ontology.objects o ON o.object_id = "
                      "i.object_id JOIN unnest(%s::text[], %s::text[]) AS r(t, p) ON "
                      "o.object_type = r.t AND i.property = r.p",
                      ([t for t, _ in pairs], [p for _, p in pairs])).fetchall()
    return {(a, b) for a, b in rows}


def remapped_link_types(served: Any, new: Any) -> list[str]:
    """The served mention link types whose (document type, target type) the new schema
    maps to no or another link type (``mentions.mention_link_types``)."""
    from ontology.mentions import mention_link_types
    old = mention_link_types(served.link_types)
    now = mention_link_types(new.link_types)
    return sorted({lid for pair, lid in old.items() if now.get(pair) != lid})


def keys_of(pg: Any, objects: Iterable[str]) -> set[tuple[str, str]]:
    ids = sorted(set(objects))
    out: set[tuple[str, str]] = set()
    for k in range(0, len(ids), 50_000):
        out |= {(a, b) for a, b in pg.execute(
            "SELECT DISTINCT identifier_kind, normalized FROM ontology.identifiers WHERE "
            "object_id = ANY(%s::text[])", (ids[k:k + 50_000],)).fetchall()}
    return out


def documents(scan: str | Path, pg: Any, *, also: Iterable[str] = (),
              thread_property: str = "conversation_id") -> dict[str, Any]:
    """The scope from a :func:`scan_cache` file: the objects of the hit documents and
    ``also`` (object ids), widened to their conversations, then to the whole groups
    holding any of their versions; ``{"groups": [...], "objects": [...]}``. A group's
    document whose version no object holds is counted (``documents_without_object``), not
    refused: no run derives a link from it."""
    db = sqlite3.connect(f"file:{scan}?mode=ro", uri=True)
    try:
        cites = [r[0] for r in db.execute("SELECT DISTINCT citation FROM docs WHERE hit")]
        objects = set(also) | _objects_of(pg, cites)
        objects |= _threads(pg, objects, thread_property)
        handles = _handles_of(pg, objects)
        db.execute("CREATE TEMP TABLE h (citation TEXT PRIMARY KEY)")
        db.executemany("INSERT OR IGNORE INTO temp.h VALUES (?)", [(h,) for h in handles])
        groups = sorted({r[0] for r in db.execute(
            "SELECT DISTINCT g.group_key FROM temp.h JOIN docs d USING (citation) JOIN "
            "group_docs g ON g.doc_key = d.doc_key")})
        db.execute("CREATE TEMP TABLE g (group_key TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO temp.g VALUES (?)", [(g,) for g in groups])
        all_cites = sorted({r[0] for r in db.execute(
            "SELECT DISTINCT d.citation FROM temp.g JOIN group_docs x USING (group_key) "
            "JOIN docs d ON d.doc_key = x.doc_key")})
    finally:
        db.close()
    scoped = _objects_of(pg, all_cites)
    held = _handles_of(pg, scoped)
    # A document no object holds (mail captured after the store's capture mark while the
    # delta was paused, or a version never projected) gets no mention link from the step,
    # scoped or whole (``mentions._document_links``: no version row, no rows), and has no
    # served link to hide: it is left to the delta, which projects its object and resolves
    # its mentions.
    unheld = sum(1 for c in all_cites if c not in held)
    return {"groups": groups, "objects": sorted(scoped | objects),
            "counts": {"hit_documents": len(cites), "groups": len(groups),
                       "documents": len(all_cites), "documents_without_object": unheld,
                       "objects": len(scoped | objects)}}


# version handles of capture sources that give the mentions step no document (no
# ``documents`` / ``document_groups``: ``ontology.project.MirrorCaptures`` (portal, api://)
# and ``ontology.project_bc.BcCaptures`` (bc://); a test holds both so): never uncached.
NO_DOCUMENT_HANDLES = ("api://", "bc://")


def uncached_documents(scan: str | Path, pg: Any, document_types: Sequence[str],
                       not_documents: Iterable[str] = ()) -> int:
    """How many CURRENT versions of objects of ``document_types`` (the types mention links
    are declared from) the cache holds no document of: a document whose mentions are
    unknown could be one whose links change, so the scope refuses on any. A superseded
    version is no document of any source (the sources read the current captures), so it
    contributes no mention,
    and neither is a
    version of a source that gives the step no document (:data:`NO_DOCUMENT_HANDLES`:
    11,714 current BC and 13,775 current portal versions on served plx). Every other
    scheme counts (fails closed). ``not_documents``: handles a listing of every source
    (the targeted prefill, ``--not-documents``) found no document of: they count neither."""
    db = sqlite3.connect(f"file:{scan}?mode=ro", uri=True)
    try:
        db.execute("CREATE TEMP TABLE held (citation TEXT PRIMARY KEY)")
        db.execute("INSERT OR IGNORE INTO temp.held SELECT citation FROM docs")
        db.executemany("INSERT OR IGNORE INTO temp.held VALUES (?)",
                       ((h,) for h in not_documents))
        missing = 0
        with pg.cursor(name="plx_scope_versions") as cur:
            cur.itersize = 50_000
            cur.execute("SELECT DISTINCT v.l1_handle FROM ontology.object_versions v JOIN "
                        "ontology.objects o ON o.object_id = v.object_id WHERE "
                        "o.object_type = ANY(%s::text[]) AND v.superseded_at IS NULL AND "
                        "NOT COALESCE(v.l1_handle LIKE ANY(%s::text[]), false)",
                        (sorted(set(document_types)), [h + "%" for h in NO_DOCUMENT_HANDLES] or [""]))
            while True:
                rows = cur.fetchmany(50_000)
                if not rows:
                    break
                missing += sum(1 for (h,) in rows if db.execute(
                    "SELECT 1 FROM temp.held WHERE citation = ?", (h,)).fetchone() is None)
        return missing
    finally:
        db.close()


def _objects_of(pg: Any, handles: Sequence[str]) -> set[str]:
    out: set[str] = set()
    handles = list(handles)
    for k in range(0, len(handles), 50_000):
        out |= {r[0] for r in pg.execute(
            "SELECT DISTINCT object_id FROM ontology.object_versions WHERE l1_handle = "
            "ANY(%s::text[])", (handles[k:k + 50_000],)).fetchall()}
    return out


def _handles_of(pg: Any, objects: Iterable[str]) -> set[str]:
    ids = sorted(set(objects))
    out: set[str] = set()
    for k in range(0, len(ids), 50_000):
        out |= {r[0] for r in pg.execute(
            "SELECT DISTINCT l1_handle FROM ontology.object_versions WHERE object_id = "
            "ANY(%s::text[])", (ids[k:k + 50_000],)).fetchall()}
    return out


def _threads(pg: Any, objects: Iterable[str], prop: str) -> set[str]:
    """The objects sharing a conversation (``prop`` value) with one of ``objects``."""
    ids = sorted(set(objects))
    values: set[str] = set()
    for k in range(0, len(ids), 50_000):
        values |= {r[0] for r in pg.execute(
            "SELECT DISTINCT value FROM ontology.object_values WHERE property = %s AND "
            "value IS NOT NULL AND object_id = ANY(%s::text[])",
            (prop, ids[k:k + 50_000])).fetchall()}
    vals = sorted(values)
    out: set[str] = set()
    for k in range(0, len(vals), 50_000):
        out |= {r[0] for r in pg.execute(
            "SELECT DISTINCT object_id FROM ontology.object_values WHERE property = %s AND "
            "value = ANY(%s::text[])", (prop, vals[k:k + 50_000])).fetchall()}
    return out


def remapped_documents(pg: Any, link_types: Sequence[str]) -> set[str]:
    """The objects holding a served ``mention:`` link of one of ``link_types``."""
    if not link_types:
        return set()
    from ontology.pg_byte_order import key_collate
    c = key_collate(pg)
    return {r[0] for r in pg.execute(
        "SELECT DISTINCT from_object FROM ontology.link_rows WHERE link_type = "
        f"ANY(%s::text[]) AND link_id{c} >= 'mention:' AND link_id{c} "
        "< 'mention;'", (list(link_types),)).fetchall()}


def scope(dsn: str, before: str | Path, after: str | Path, cache: str | Path,
          work: str | Path, *, served: Any, new: Any, undo: str | Path | None = None,
          workers: int = 8, log: Any = None,
          not_documents: str | Path | None = None) -> dict[str, Any]:
    """The mention scope (module docstring): ``{"groups", "objects", "counts"}``, or
    ``{"refused": reason}`` when it cannot be bounded (the caller re-derives every
    document). ``before`` / ``after``: :func:`snapshot` files around the unit applies
    (the same bindings); ``undo``: the marks-only apply's undo folder."""
    import psycopg
    from ontology.mention_cache import extractor_code
    started = time.monotonic()
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    counts: dict[str, Any] = {}
    try:
        metas = []
        for path in (before, after):
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                metas.append(dict(db.execute("SELECT key, value FROM meta").fetchall()))
            finally:
                db.close()
        if json.loads(metas[0]["bindings"]) != json.loads(metas[1]["bindings"]):
            raise ScopeRefused("snapshots_of_other_bindings")
        moved = domains_changed(before, after)
        if moved:
            raise ScopeRefused(f"party_domains_changed:{len(moved)}")
        changed, keys = changed_objects(before, after)
        marked = marked_objects(undo) if undo is not None else set()
        counts.update(changed=len(changed), marked=len(marked))
        with psycopg.connect(dsn) as pg:
            pg.execute("SET TRANSACTION READ ONLY")
            if pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                          "'migration_in_progress'").fetchone() is not None:
                raise ScopeRefused("migration_in_progress")
            keys |= keys_of(pg, marked)
            refs = reference_keys(pg)
            remapped = remapped_link_types(served, new)
            redocs = remapped_documents(pg, remapped)
            counts.update(changed_keys=len(keys), reference_keys=len(refs),
                          remapped_link_types=remapped, remapped_documents=len(redocs))
            src = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
            try:
                held = src.execute("SELECT count(*) FROM extracted WHERE xcode = ?",
                                   (extractor_code(),)).fetchone()[0]
            finally:
                src.close()
            if not held:   # the cache is of other extraction code: nothing can be told
                raise ScopeRefused("mention_cache_of_other_code")
            scanned = scan_cache(cache, keys | refs, work / "scan.sqlite", workers=workers)
            counts["scan"] = scanned
            if log is not None:
                print(json.dumps(counts, sort_keys=True, default=str), file=log, flush=True)
            from ontology.mentions import mention_link_types
            doc_types = sorted({frm for frm, _to in mention_link_types(new.link_types)})
            excused = [] if not_documents is None else [
                line.split("\t", 1)[0] for line in
                Path(not_documents).read_text(encoding="utf-8").splitlines() if line]
            counts["not_documents"] = len(excused)
            uncached = uncached_documents(work / "scan.sqlite", pg, doc_types, excused)
            counts["uncached_documents"] = uncached
            if uncached:
                raise ScopeRefused(f"documents_not_in_cache:{uncached}")
            out = documents(work / "scan.sqlite", pg, also=changed | marked | redocs)
        out["counts"] = {**counts, **out["counts"],
                         "seconds": round(time.monotonic() - started, 1)}
        return out
    except ScopeRefused as exc:
        return {"refused": str(exc), "counts": counts}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.mention_scope")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--bindings", required=True, help="a JSON list of binding ids")
    s.add_argument("--out", required=True)
    s.add_argument("--before", action="store_true",
                   help="refuse while a chunked migration is in progress")
    s.add_argument("--dsn", default=None)
    s.add_argument("--workers", type=int, default=1)
    c = sub.add_parser("scope", help="the documents whose mention links can change")
    c.add_argument("--before", required=True)
    c.add_argument("--after", required=True)
    c.add_argument("--cache", required=True, help="the warm mention extraction cache")
    c.add_argument("--served-ref", required=True, help="the served schema's git ref")
    c.add_argument("--undo", default=None, help="the marks-only apply's undo folder")
    c.add_argument("--not-documents", default=None,
                   help="version handles no source yields a document of (one a line)")
    c.add_argument("--work", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--workers", type=int, default=8)
    c.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    from ontology.mentions import MENTION_FEATURES
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    if args.command == "scope":
        from ontology import schema_migrate
        from ontology import step_migrate as sm
        from ontology.schema import load_schema
        out = scope(dsn, args.before, args.after, args.cache, args.work,
                    served=schema_migrate.schema_at(args.served_ref, sm.ROOT),
                    new=load_schema(), undo=args.undo, workers=args.workers, log=sys.stderr,
                    not_documents=args.not_documents)
        tmp = Path(args.out + ".part")
        tmp.write_text(json.dumps(out, sort_keys=True, default=str))
        tmp.replace(args.out)
        print(json.dumps({k: v for k, v in out.items() if k not in ("groups", "objects")},
                         sort_keys=True, default=str))
        return 0
    report = snapshot(dsn, json.loads(Path(args.bindings).read_text()),
                      args.out, before=args.before,
                      domain_property=MENTION_FEATURES["party_domain"]["property"],
                      log=sys.stderr, workers=args.workers)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
