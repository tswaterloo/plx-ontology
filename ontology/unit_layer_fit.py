"""The unit migration layer fitted to the host: re-inserted copies left unread, the link
phase on every core.

Measured 2026-11-09 on the live host (the combined migration build, stopped by the disk
guard at 6:34 AM with /data at 28 GB): after its documents, the unit layer spent 1 h 20 m
on ``portal.app_manufacturing_order_line`` alone and about 2 h on the six other bindings
over relations the Portal ETL re-inserts on every run (``ontology.project.ETL_REINSERTED``:
53.8M mirror rows for 433k records), then more than 3 h in its link specs on one core,
while its working file grew past 93 GB.

The rows were never the defect: the projection keeps one object per record of such a
relation (``Projector._collapse_reinserted``: one per natural key, audience and source
presence; the first copy inserted, then the smallest object id), and the served store
holds 262,853 ``AssemblyOrderComponent`` objects for the 31.7M copies. The cost was the
read: every copy was decoded, staged and compressed into the working file, only to be
deleted again by the collapse. Here:

* :class:`CollapsingMirror` reads, of such a relation, only the copies the collapse could
  keep. One light pass over the relation's rows in the build's own snapshot groups its
  single-version copies by their row less the minted columns, as text, and keeps the
  first of each group in the collapse's own order (``_keep_order`` of ``created_at``, then
  the object id); every key with history, a run, an unfinished capture, a row that is not
  strict JSON or one whose natural key the projection would not compute
  (``_natural_key`` gives ``record:``) is read as before, as is a relation any binding of
  which is not keyed by the minted ``id`` alone or reads more than its row. Equal text is
  an equal natural key, so each copy left unread loses the collapse to the one kept from
  its group: the objects, versions and every later row are a full read's.
  The rest of the mirror is read unchanged (TEMP views over ``row_current`` and
  ``row_version``, the pattern ``source_mirror.attach_scoped`` uses).
* :func:`parallel_links` projects the link specs of a :class:`Projector` in worker
  processes: each worker reads the build's working file read-only (its compare keys,
  candidates and links in TEMP tables of its own) and writes each link type's links to a
  part, which the build appends to its working file in link type order, so its ``links``
  table is a serial build's row for row (``seq`` included). Report entries merge
  (``_Report`` keeps counts and the smallest samples: order-free).

Why a module of its own: ``ontology.project`` and ``ontology.schema_migrate`` are
derivation code (``ontology.project.derivation_code``, the key of the kept document
fields), so changing them would make the next build parse every document again. Nothing
the derivation code covers imports this module; ``ontology.unit_layer_workers`` turns it
on for its build (:func:`fitted`).

Stdlib only.
"""

from __future__ import annotations

import concurrent.futures as futures
import contextlib
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import pickle
import re
import secrets
import shutil
import sqlite3
import sys
import time
from itertools import groupby
from typing import Any, Iterator, Mapping, Sequence

from ontology import project as P

# ----------------------------------------------------------------------------- mirror


def collapsible(schema: Any, source: str = "portal_api") -> dict[str, tuple[str, ...]]:
    """relation -> its minted columns (sorted), for each ETL re-inserted relation of
    ``source`` whose copies :class:`CollapsingMirror` may leave unread: every binding over
    it is keyed by the minted ``id`` alone and reads nothing but its row (no ``where``,
    no ``stands_for`` either way, no capture lineage)."""
    reinserted = P.ETL_REINSERTED.get(source, {})
    bindings = [b for t in (P._get(schema, "object_types") or {}).values()
                for b in t.get("bindings", ())]
    stood = {(b.get("stands_for") or {}).get("binding") for b in bindings}
    out: dict[str, tuple[str, ...]] = {}
    for relation, minted in sorted(reinserted.items()):
        over = [b for b in bindings if b.get("source") == source
                and b.get("relation") == relation]
        if over and "id" in minted and all(
                list(b.get("key") or ()) == ["id"] and not b.get("where")
                and not b.get("stands_for") and b["id"] not in stood
                and "from_capture" not in (b.get("lineage") or {}) for b in over):
            out[relation] = tuple(sorted(minted))
    return out


_VIEW = """CREATE TEMP VIEW {table} AS
    SELECT m.* FROM temp.plx_keep k JOIN main.{table} m
      ON m.relation = k.relation AND m.key_json = k.key_json
    UNION ALL
    SELECT m.* FROM main.{table} m
    WHERE NOT EXISTS (SELECT 1 FROM temp.plx_collapsed c WHERE c.relation = m.relation)"""


class CollapsingMirror(P.MirrorCaptures):
    """:class:`ontology.project.MirrorCaptures` reading, of each relation in
    ``collapse`` (:func:`collapsible`), only the copies the projection's collapse could
    keep (module docstring). ``unread``: relation -> the copies left unread by the last
    :meth:`records`."""

    def __init__(self, path: str | Path, *, collapse: Mapping[str, Sequence[str]],
                 prisma_facts: Mapping[str, Any] | None = None,
                 kept_dir: str | Path | None = None, kept_file: str | Path | None = None,
                 pinned: Sequence[Any] | None = None) -> None:
        super().__init__(path, prisma_facts=prisma_facts)
        self.collapse = {rel: tuple(minted) for rel, minted in collapse.items()}
        self.unread: dict[str, int] = {}
        # where the kept copies of a snapshot are kept once computed (``_kept_file``):
        # the parent of a parallel build computes them there once, for every relation it
        # collapses; its workers get the file itself (``kept_file``) and only load it
        # (a missing or unreadable file fails their open, never computed again)
        self.kept_dir = kept_dir
        self.kept_file = kept_file
        # (the parent's watermark, its relation marks): a worker whose file marks moved
        # (a reader or a no-op writer open touched the WAL) but whose relation marks of
        # what it reads are the parent's reads the parent's records (``watermark``)
        self.pinned = pinned
        self._read: list[str] = []
        self.kept_path: Path | None = None   # the file the last :meth:`records` used
        self.kept_loaded = False
        conn = self._conn
        conn.execute("CREATE TEMP TABLE plx_keep (relation TEXT NOT NULL, key_json TEXT "
                     "NOT NULL, PRIMARY KEY (relation, key_json)) WITHOUT ROWID")
        conn.execute("CREATE TEMP TABLE plx_collapsed (relation TEXT PRIMARY KEY)")
        for table in ("row_current", "row_version"):
            if conn.execute("SELECT 1 FROM temp.sqlite_master WHERE name = ?",
                            (table,)).fetchone() is not None:
                # a scoped fallback's views already stand there: read it as it is
                self.collapse = {}
                return
        for table in ("row_current", "row_version"):
            conn.execute(_VIEW.format(table=table))

    def records(self, relations: Sequence[str]) -> Mapping[str, Any]:
        out = super().records(relations)   # the snapshot every relation is read in
        conn = self._conn
        conn.execute("DELETE FROM temp.plx_keep")
        conn.execute("DELETE FROM temp.plx_collapsed")
        self.unread = {}
        self.kept_loaded, self.kept_path = False, None
        self._read = sorted(set(relations))
        wanted = sorted(set(relations) & set(self.collapse))
        if not wanted:
            return out
        if self.kept_file is not None:   # a worker: the parent's file, never computed
            path = self.kept_path = Path(self.kept_file)
            if not _load_kept(path, conn, self.unread, only=wanted):
                raise P.ProjectionError("kept_copies_missing", f"kept_copies_missing:{path}")
            self.kept_loaded = True
            return out
        asked = set(wanted)
        if self.kept_dir is not None:
            wanted = sorted(self.collapse)   # once, for every relation it collapses
        path = self.kept_path = _kept_file(self, wanted)
        if path is not None and _load_kept(path, conn, self.unread, only=sorted(asked)):
            self.kept_loaded = True
            return out
        finished = {row[0] for row in conn.execute(
            "SELECT c.capture_id FROM main.capture c JOIN main.capture_state s "
            "ON s.capture_id = c.capture_id WHERE s.state = 'complete'")}
        for relation in wanted:
            kept = _kept_copies(conn, relation, frozenset(self.collapse[relation]), finished)
            if kept is None:
                continue
            keys, unread = kept
            conn.executemany("INSERT INTO temp.plx_keep VALUES (?, ?)",
                             ((relation, key) for key in sorted(keys)))
            conn.execute("INSERT INTO temp.plx_collapsed VALUES (?)", (relation,))
            self.unread[relation] = unread
        if path is not None:
            _save_kept(path, conn, self.unread)
        if set(wanted) != asked:   # the others are kept in the file, not read here
            conn.execute(f"DELETE FROM temp.plx_keep WHERE relation NOT IN "
                         f"({', '.join('?' for _ in asked)})", sorted(asked))
            conn.execute(f"DELETE FROM temp.plx_collapsed WHERE relation NOT IN "
                         f"({', '.join('?' for _ in asked)})", sorted(asked))
            self.unread = {r: n for r, n in self.unread.items() if r in asked}
        return out

    def watermark(self) -> list[Any] | None:
        """The parent's watermark (``pinned``) when this snapshot's relation marks of
        what the last :meth:`records` read are the parent's (equal marks, equal records:
        ``MirrorCaptures.relation_marks``), else the file's own."""
        own = super().watermark()
        if self.pinned is None or not self._snapshot:
            return own
        mark, relation_marks = self.pinned
        if own is not None and P.canonical(own) == P.canonical(mark):
            return own
        have = self.relation_marks(self._read) if self._read else None
        if have is None or relation_marks is None or \
                P.canonical(have) != P.canonical({r: relation_marks.get(r) for r in self._read}):
            return own
        return mark


KEPT_FORMAT = "plx-kept-copies/v1"


def _kept_file(source: CollapsingMirror, relations: Sequence[str]) -> Path | None:
    """The file the kept copies of ``relations`` in ``source``'s current snapshot are
    kept in under its ``kept_dir``, named by what decides them: the snapshot's watermark
    (the mirror's file marks), the collapse of each relation, this module's code that
    computes them. None without a directory or a watermark (a snapshot nothing vouches
    for is computed, never shared)."""
    if source.kept_dir is None:
        return None
    # the relation marks of every relation it collapses when the mirror vouches for them
    # (a WAL touched by a reader leaves them), else the file marks
    mark = source.relation_marks(sorted(source.collapse))
    if mark is None:
        mark = source.watermark()
    if mark is None:
        return None
    stamp = hashlib.sha256(P.canonical([
        KEPT_FORMAT, mark, [[rel, list(source.collapse[rel])] for rel in relations],
        code_digest(_kept_copies, P._keep_order, P._natural_key, P.object_id_of,
                    P._blank)]).encode("utf-8")).hexdigest()
    return Path(source.kept_dir) / f"kept-copies-{stamp[:32]}.sqlite"


def _load_kept(path: Path, conn: sqlite3.Connection, unread: dict[str, int],
               only: Sequence[str] | None = None) -> bool:
    """Fills ``conn``'s ``plx_keep``/``plx_collapsed`` and ``unread`` from ``path`` as
    :func:`_save_kept` wrote it, streamed (no key held in memory); False, with nothing
    filled, when the file is missing, unfinished or unreadable (then computed)."""
    if not path.exists():
        return False
    try:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            meta = dict(db.execute("SELECT name, value FROM meta"))
            if meta.get("format") != KEPT_FORMAT or meta.get("complete") != "1":
                return False
            held = {rel: int(n) for rel, n in db.execute("SELECT relation, unread FROM collapsed")
                    if only is None or rel in only}
            for rel in sorted(held):
                conn.executemany("INSERT INTO temp.plx_keep VALUES (?, ?)", db.execute(
                    "SELECT relation, key_json FROM keep WHERE relation = ? "
                    "ORDER BY key_json", (rel,)))
            conn.executemany("INSERT INTO temp.plx_collapsed VALUES (?)",
                             ((rel,) for rel in sorted(held)))
    except (sqlite3.Error, ValueError, TypeError):
        conn.execute("DELETE FROM temp.plx_keep")
        conn.execute("DELETE FROM temp.plx_collapsed")
        return False
    unread.update(held)
    return True


def _save_kept(path: Path, conn: sqlite3.Connection, unread: Mapping[str, int]) -> None:
    """Writes ``conn``'s ``plx_keep``/``plx_collapsed`` and ``unread`` to ``path``, whole
    or not at all (a temporary file renamed)."""
    part = path.with_name(f"{path.name}.{os.getpid()}.part")
    try:
        part.unlink(missing_ok=True)
        with contextlib.closing(sqlite3.connect(part)) as db:
            db.execute("CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT)")
            db.execute("CREATE TABLE collapsed (relation TEXT PRIMARY KEY, unread INTEGER)")
            db.execute("CREATE TABLE keep (relation TEXT NOT NULL, key_json TEXT NOT NULL, "
                       "PRIMARY KEY (relation, key_json)) WITHOUT ROWID")
            db.executemany("INSERT INTO collapsed VALUES (?, ?)", (
                (rel, unread[rel]) for (rel,) in conn.execute(
                    "SELECT relation FROM temp.plx_collapsed ORDER BY relation")))
            db.executemany("INSERT INTO keep VALUES (?, ?)", conn.execute(
                "SELECT relation, key_json FROM temp.plx_keep ORDER BY relation, key_json"))
            db.executemany("INSERT INTO meta VALUES (?, ?)",
                           (("format", KEPT_FORMAT), ("complete", "1")))
            db.commit()
        os.replace(part, path)
    except (OSError, sqlite3.Error):   # not kept: each reader computes them again
        part.unlink(missing_ok=True)


def _kept_copies(conn: sqlite3.Connection, relation: str, minted: frozenset[str],
                 finished: set[Any]) -> tuple[set[str], int] | None:
    """(the keys of ``relation`` to read, the copies left unread), or None to read all of
    it: some row of it is not strict JSON keyed by its own ``id`` text (one id names one
    key, so the collapse's objects are the mirror's keys). Read in the caller's
    snapshot."""
    # One pass over the rows (the relation's bytes are most of the cost): the check and
    # the grouping read each row once. JSON is parsed only where it is strict.
    odd = conn.execute(
        "SELECT 1 FROM main.row_version WHERE relation = ? AND (CASE WHEN "
        "json_valid(row_json) THEN json_type(row_json, '$.id') IS 'text' AND key_json "
        "IS json_object('id', json_extract(row_json, '$.id')) END) IS NOT 1 LIMIT 1",
        (relation,)).fetchone()
    if odd is not None:
        return None
    history = {row[0] for row in conn.execute(
        "SELECT DISTINCT key_json FROM main.row_version WHERE relation = ?", (relation,))}
    paths = ", ".join("?" for _ in minted)
    keep: set[str] = set()
    best: dict[tuple[bytes, bool], tuple[str, str, str]] = {}
    refused: set[tuple[bytes, bool]] = set()
    single = 0
    for key, length, capture, present, ok, natural, created, kind, ident in conn.execute(
            f"SELECT key_json, run_length, capture_id, missing IS NULL, ok, "
            f"CASE WHEN ok THEN json_remove(row_json, {paths}) END, "
            f"CASE WHEN ok THEN json_extract(row_json, '$.created_at') END, "
            f"CASE WHEN ok THEN json_type(row_json, '$.created_at') END, "
            f"CASE WHEN ok THEN json_extract(row_json, '$.id') END FROM ("
            f"SELECT key_json, run_length, capture_id, row_json, "
            f"missing_from_source_since AS missing, (CASE WHEN json_valid(row_json) THEN "
            f"json_type(row_json, '$.id') IS 'text' AND key_json IS json_object('id', "
            f"json_extract(row_json, '$.id')) END) IS 1 AS ok "
            f"FROM main.row_current WHERE relation = ?)",
            [f"$.{name}" for name in sorted(minted)] + [relation]):
        if not ok:
            return None
        if (length != 1 or key in history or capture not in finished
                or not isinstance(ident, str) or P._blank(ident)):
            keep.add(key)
            continue
        group = (hashlib.sha256(natural.encode("utf-8")).digest(), bool(present))
        if group in refused:
            keep.add(key)
            continue
        single += 1
        rank = P._keep_order(created if kind == "text" else None)
        oid = P.object_id_of("", "", {"id": ident})
        held = best.get(group)
        if held is None:
            # The projection's own natural key of this group's rows (equal text, equal
            # value): a row it would not collapse keeps every copy of its group.
            (row,) = conn.execute("SELECT row_json FROM main.row_current WHERE relation = ? "
                                  "AND key_json = ? AND run_index = 0",
                                  (relation, key)).fetchone()
            if P._natural_key(json.loads(row), minted, key)[0].startswith("record:"):
                refused.add(group)
                keep.add(key)
                single -= 1
                continue
            best[group] = (rank, oid, key)
        elif (rank, oid) < held[:2]:
            best[group] = (rank, oid, key)
    keep.update(item[2] for item in best.values())
    return keep, single - len(best)


def mirror_opener(schema: Any) -> Any:
    """``MirrorCaptures`` as :func:`ontology.schema_migrate.build_from_state` opens it,
    collapsing the relations :func:`collapsible` names under ``schema``."""
    collapse = collapsible(schema)

    def opened(path: str | Path, **kwargs: Any) -> CollapsingMirror:
        return CollapsingMirror(path, collapse=collapse, **kwargs)
    return opened


# ----------------------------------------------------------------------------- links

LINK_WORKERS_ENV = "PLX_BUILD_WORKERS"
# Rows a worker's TEMP compare keys may hold before it forgets them between link types
# (each worker computes the key sets of the bindings its link types read; this bounds
# its temporary disk).
K_ROWS_LIMIT = 5_000_000
_TEMP_DDL = (
    "CREATE TEMP TABLE k (sig INTEGER NOT NULL, norm TEXT NOT NULL, vid INTEGER NOT NULL)",
    "CREATE INDEX temp.k_by_norm ON k(sig, norm, vid)",
    "CREATE INDEX temp.k_by_vid ON k(sig, vid)",
    "CREATE TEMP TABLE cand (a INTEGER NOT NULL, i INTEGER NOT NULL, b INTEGER NOT NULL)",
    "CREATE INDEX temp.cand_by_a ON cand(a, i, b)",
    """CREATE TEMP TABLE links (seq INTEGER PRIMARY KEY, link_type TEXT NOT NULL,
        binding_kind TEXT, from_object TEXT NOT NULL, to_object TEXT NOT NULL,
        from_vid INTEGER NOT NULL, to_vid INTEGER NOT NULL, from_type TEXT NOT NULL,
        to_type TEXT NOT NULL, tier TEXT NOT NULL, recorded_at TEXT NOT NULL,
        superseded_at TEXT, row BLOB NOT NULL)""",
)
_LINK_COLUMNS = ("link_type, binding_kind, from_object, to_object, from_vid, to_vid, "
                 "from_type, to_type, tier, recorded_at, superseded_at, row")
_SERIAL_LINKS = P.Projector._project_links
_SERIAL_PAIRS = P.Projector._link_pairs
_SERIAL_CANDIDATES = P.Projector._candidates


def _link_pairs(self: P.Projector, lid: str, link: Mapping[str, Any], index: int,
                lb: Mapping[str, Any]) -> None:
    """``Projector._link_pairs``, its candidates found on every pair at once when only a
    match on every pair counts (a foreign key of several pairs: ``whole``)."""
    self._fit_whole = lb["kind"] == "fk" and len(lb["pairs"]) > 1
    self._fit_where = f"{lid}#{index}"
    try:
        _SERIAL_PAIRS(self, lid, link, index, lb)
    finally:
        self._fit_whole = False


def _candidates(self: P.Projector, joins: Sequence[tuple[int, int]]
                ) -> Iterator[tuple[int, list[tuple[int, int]]]]:
    """``Projector._candidates``. For a foreign key of several pairs, whose link spec
    keeps only the targets matching on every pair (``_link_pairs``: ``whole``), only those
    are listed: the from-version and target pairs whose compare keys agree on every pair,
    joined on the keys together (one key per version and compare set: ``_sig``), each
    with every pair index, in the serial order. The serial build joined each pair alone:
    a pair of few values (a batch type, a payment code, a source ledger) paired most
    from-versions with most targets, every one of them written to the working file and
    then dropped as a partial match (measured on the live unit layer: tens of GB and
    hours). The links are the same: a partial match never makes a candidate, and the
    full matches come in the same order."""
    if not getattr(self, "_fit_whole", False) or len(joins) < 2:
        # every pair's join written whole: its size, counted from the keys, first
        _within_budget(self, sum(_join_size(self._db, f, t) for f, t in joins))
        yield from _SERIAL_CANDIDATES(self, joins)
        return
    db = self._db
    db.execute("DELETE FROM cand")

    def keys(end: int) -> dict[tuple, list[int]]:
        """compare keys on every pair -> the versions holding them (one key per
        version and compare set: ``_sig``); a version lacking one pair holds none."""
        held: dict[int, list[str]] | None = None
        for pair in joins:
            got = dict(db.execute("SELECT vid, norm FROM k WHERE sig = ?", (pair[end],)))
            if held is None:
                held = {vid: [norm] for vid, norm in got.items()}
            else:
                held = {vid: [*norms, got[vid]] for vid, norms in held.items() if vid in got}
        out: dict[tuple, list[int]] = {}
        for vid, norms in (held or {}).items():
            out.setdefault(tuple(norms), []).append(vid)
        return out
    targets = keys(1)
    froms = keys(0)
    _within_budget(self, len(joins) * sum(len(vids) * len(targets.get(key, ()))
                                          for key, vids in froms.items()))
    pairs = sorted((a, b) for key, vids in froms.items() for a in vids
                   for b in targets.get(key, ()))
    for start in range(0, len(pairs), 50_000):
        chunk = pairs[start:start + 50_000]
        db.executemany("INSERT INTO cand (a, i, b) VALUES (?, ?, ?)",
                       [(a, index, b) for a, b in chunk for index in range(len(joins))])
    last = 0
    while True:
        self._tick()
        ends = db.execute("SELECT DISTINCT a FROM cand WHERE a > ? ORDER BY a LIMIT ?",
                          (last, self.batch)).fetchall()
        if not ends:
            return
        rows = db.execute("SELECT a, i, b FROM cand WHERE a > ? AND a <= ? "
                          "ORDER BY a, i, b", (last, ends[-1][0])).fetchall()
        for vid, group in groupby(rows, key=lambda row: row[0]):
            yield vid, [(row[1], row[2]) for row in group]
        last = ends[-1][0]


# Candidate rows one link type's join may write (its TEMP ``cand`` rows, about 50 bytes
# each with their index; the whole-key path holds them in memory first). Counted from
# the compare keys before any is written: a join over a value most rows share (the cross
# product that filled the disk) stops its build at once, naming the link type, instead
# of hours later. ``PLX_LINK_CANDIDATE_BUDGET`` raises or lowers it.
CANDIDATE_BUDGET_ENV = "PLX_LINK_CANDIDATE_BUDGET"
CANDIDATE_BUDGET = 100_000_000


class OverBudget(RuntimeError):
    """A link type's join would write more candidates than the budget allows."""


def candidate_budget() -> int:
    raw = os.environ.get(CANDIDATE_BUDGET_ENV)
    if raw is None or raw == "":
        return CANDIDATE_BUDGET
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise OverBudget(f"{CANDIDATE_BUDGET_ENV}_invalid:{raw!r}")
    return value


def _join_size(db: sqlite3.Connection, fsig: int, tsig: int) -> int:
    """The rows the key join of two compare-key sets yields (before DISTINCT): the sum,
    over every compare key, of its from-versions times its targets."""
    return db.execute(
        "SELECT COALESCE(SUM(f.n * t.n), 0) FROM (SELECT norm, COUNT(*) AS n FROM k "
        "WHERE sig = ? GROUP BY norm) f JOIN (SELECT norm, COUNT(*) AS n FROM k "
        "WHERE sig = ? GROUP BY norm) t ON t.norm = f.norm", (fsig, tsig)).fetchone()[0]


def _within_budget(projector: Any, rows: int) -> None:
    budget = candidate_budget()
    where = getattr(projector, "_fit_where", None) or "?"
    if rows > budget:
        raise OverBudget(f"link_candidates_over_budget:{where}:{rows}>{budget}")
    seen = getattr(projector, "_fit_candidates", None)
    if isinstance(seen, dict):
        seen[where] = seen.get(where, 0) + rows


_SERIAL_VIA = P.Projector._link_via


def _link_via(self: P.Projector, lid: str, link: Mapping[str, Any], index: int,
              lb: Mapping[str, Any]) -> None:
    """``Projector._link_via``, its joins counted against the budget under its name."""
    self._fit_where = f"{lid}#{index}"
    _SERIAL_VIA(self, lid, link, index, lb)


def _whole_keys() -> contextlib.AbstractContextManager:
    """While open, link specs find a several-pair foreign key's candidates on every pair
    at once (:func:`_candidates`), each join within the candidate budget."""
    return _patched(P.Projector, _link_pairs=_link_pairs, _candidates=_candidates,
                    _link_via=_link_via)


@contextlib.contextmanager
def _patched(owner: Any, **names: Any) -> Iterator[None]:
    saved = {name: getattr(owner, name) for name in names}
    for name, value in names.items():
        setattr(owner, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(owner, name, value)
_STATE: dict[str, Any] = {}


def _link_jobs(projector: P.Projector) -> list[str]:
    """The link types whose link specs read the working file (``_project_links``: a
    projected kind between two known bindings in scope); the others report gaps only."""
    out = []
    for lid in sorted(projector.links):
        for lb in projector.links[lid]["bindings"]:
            if (lb["kind"] in P._PROJECTED_LINK_KINDS and lb.get("from_binding") in
                    projector.bindings and lb.get("to_binding") in projector.bindings
                    and (projector.scope is None or
                         P.link_ends(lb, projector.bindings) <= projector.scope)):
                out.append(lid)
                break
    return out


def _absorb(report: Any, other: Mapping[str, Any]) -> None:
    """Add a worker's report (``_part_dict``) to ``report`` (``_Report``): counts summed,
    gap counts summed, samples the smallest, recorded handles united."""
    def into(target: Any, part: Mapping[str, Any]) -> None:
        for name, amount in part["counts"].items():
            target.counts[name] = target.counts.get(name, 0) + amount
        for key, gap in part["gaps"].items():
            entry = target.gaps.setdefault(key, {"code": gap["code"], "where": gap["where"],
                                                 "count": 0, "samples": []})
            entry["count"] += gap["count"]
            entry["samples"] = P._smallest(entry["samples"], gap["samples"])
            if "handles" in gap:
                entry.setdefault("handles", set()).update(gap["handles"])
    into(report, other)
    for unit, part in other["parts"].items():
        mine = report.parts.get(unit)
        if mine is None:
            mine = report.parts[unit] = P._Report()
        into(mine, part)
    report.unattributed += other["unattributed"]


def _part_dict(report: Any) -> dict[str, Any]:
    def plain(r: Any) -> dict[str, Any]:
        return {"counts": dict(r.counts), "gaps": {k: dict(v) for k, v in r.gaps.items()}}
    return {**plain(report), "unattributed": report.unattributed,
            "parts": {unit: plain(part) for unit, part in report.parts.items()}}


def _init(payload: bytes) -> None:
    config = pickle.loads(payload)
    projector = P.Projector(config["schema"], {}, normalizer=config["normalizer"],
                            as_known_at=config["as_known_at"], batch_size=config["batch"],
                            scope=config["scope"], emit=config["emit"],
                            shards=config["shards"], objects=config["objects"])
    conn = sqlite3.connect(f"file:{config['work']}?mode=ro", uri=True, isolation_level=None)
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute(f"PRAGMA cache_size = -{P._SCRATCH_CACHE_KIB}")
    for statement in _TEMP_DDL:
        conn.execute(statement)
    projector._conn = conn
    projector._work_path = Path(config["work"])
    projector._recs._dicts = dict(config["dicts"])
    # this worker's process only: its link specs find whole keys at once
    P.Projector._link_pairs = _link_pairs
    P.Projector._candidates = _candidates
    P.Projector._link_via = _link_via
    _STATE.clear()
    _STATE.update(projector=projector, dir=config["dir"], links=config["links"])


def _link_job(index: int, lid: str) -> dict[str, Any]:
    """One link type's link specs, as the serial phase projects them, into a part (laid
    down under another name and renamed whole: a part that exists is complete)."""
    started = time.monotonic()
    projector: P.Projector = _STATE["projector"]
    conn = projector._conn
    projector.report = P._Report()
    projector.links = {lid: _STATE["links"][lid]}
    projector._fit_candidates = {}
    conn.execute("BEGIN")
    try:
        _SERIAL_LINKS(projector)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    part = Path(_STATE["dir"]) / f"links-{index:05d}.sqlite"
    count = conn.execute("SELECT COUNT(*) FROM temp.links").fetchone()[0]
    if count:
        writing = part.with_name(part.name + ".writing")
        writing.unlink(missing_ok=True)
        conn.execute("ATTACH DATABASE ? AS part", (str(writing),))
        try:
            conn.execute("PRAGMA part.journal_mode = OFF")
            conn.execute(f"CREATE TABLE part.links AS SELECT seq, {_LINK_COLUMNS} "
                         "FROM temp.links ORDER BY seq")
        finally:
            conn.execute("DETACH DATABASE part")
        os.replace(writing, part)
    conn.execute("DELETE FROM temp.links")
    conn.execute("DELETE FROM temp.cand")
    if conn.execute("SELECT COUNT(*) FROM temp.k").fetchone()[0] > K_ROWS_LIMIT:
        conn.execute("DELETE FROM temp.k")
        projector._sigs.clear()
        projector._unread_keys.clear()
    projector._cache.clear()
    temp = conn.execute("PRAGMA temp.page_count").fetchone()[0] * \
        conn.execute("PRAGMA temp.page_size").fetchone()[0]
    return {"index": index, "lid": lid, "part": str(part) if count else None,
            "links": count, "temp_bytes": temp,
            "candidates": sum(projector._fit_candidates.values()),
            "report": _part_dict(projector.report),
            "seconds": round(time.monotonic() - started, 3)}


def _workers(requested: int | None = None) -> int:
    from ontology import project_parallel as par
    return par.workers(requested)


def _clear_caches(projector: P.Projector) -> None:
    """Forget the cached versions and citations (read again from the working file): a
    checkpointed build and the build resumed from it go on from the same state."""
    projector._cache.clear()
    projector._cites.clear()


def parallel_links(projector: P.Projector, workers: int | None = None, *,
                   log: Any = None, resume: "Resume | None" = None) -> dict[str, Any]:
    """``projector``'s link phase (``Projector._project_links``) on ``workers`` processes
    (None: ``PLX_BUILD_WORKERS``, else the CPUs less two; 1: serially, in this
    process). The working file's ``links`` rows, ``seq`` included, and the report are a
    serial phase's (module docstring). With ``resume`` (:class:`Resume`) the build is
    checkpointed before the jobs (``loaded``) and after their links are in
    (``linked``), each finished job's part and report are kept, and a resumed build
    runs only the jobs not done. Returns what ran."""
    count = _workers(workers)
    jobs = _link_jobs(projector)
    if resume is not None and resume.phase == "linked":
        return {**resume.manifest.get("links", {}), "resumed": "linked"}
    resumed = resume is not None and resume.phase == "loaded"
    if count < 2 or len(jobs) < 2:
        _SERIAL_LINKS(projector)
        _clear_caches(projector)
        stats = {"workers": 1, "jobs": len(jobs)}
        if resume is not None:
            resume.checkpoint(projector, "linked", begin=True, links=stats)
        return stats
    every = projector.links
    if not resumed:
        # The gaps-only link types (the serial loop's first branches) in this process.
        others = {lid: link for lid, link in projector.links.items() if lid not in set(jobs)}
        try:
            projector.links = others
            _SERIAL_LINKS(projector)
        finally:
            projector.links = every
    conn = projector._db
    if conn.in_transaction:
        conn.execute("COMMIT")   # the workers read the working file as it stands
    _clear_caches(projector)
    if resume is not None:
        if not resumed:
            resume.checkpoint(projector, "loaded", begin=False)
        work = resume.parts
        work.mkdir(mode=0o700, exist_ok=True)
    else:
        work = (projector.scratch_dir or Path(projector._work_path).parent) / \
            f".plx-links-{secrets.token_hex(6)}"
        work.mkdir(mode=0o700)
    try:
        payload = pickle.dumps({
            "schema": projector.schema, "normalizer": projector.normalizer,
            "as_known_at": projector.cut, "batch": projector.batch,
            "scope": projector.scope, "emit": projector.emit, "shards": projector.shards,
            "objects": projector.objects, "work": str(projector._work_path),
            "dicts": projector._recs._dicts, "dir": str(work), "links": every})
    except Exception:  # noqa: BLE001 - a schema or normalizer a worker cannot get
        conn.execute("BEGIN")
        if resume is None:
            shutil.rmtree(work, ignore_errors=True)
        try:
            projector.links = {lid: every[lid] for lid in jobs}
            _SERIAL_LINKS(projector)
        finally:
            projector.links = every
        stats = {"workers": 1, "jobs": len(jobs), "serial": "not_picklable"}
        if resume is not None:
            conn.execute("COMMIT")
            resume.checkpoint(projector, "linked", begin=True, links=stats)
        return stats
    # Longest first: by the versions the link type's ends hold.
    sizes = dict(conn.execute("SELECT binding, COUNT(*) FROM v GROUP BY binding").fetchall())

    def weight(lid: str) -> int:
        return sum(sizes.get(end, 0) for lb in every[lid]["bindings"]
                   for end in P.link_ends(lb, projector.bindings))
    order = sorted(range(len(jobs)), key=lambda i: (-weight(jobs[i]), i))
    done: dict[int, dict[str, Any]] = resume.jobs(jobs) if resume is not None else {}
    stats = {"workers": count, "jobs": len(jobs), "links": 0, "seconds": {},
             "resumed_jobs": len(done)}
    todo = [i for i in order if i not in done]
    from ontology.store import main_not_imported
    count = max(1, min(count, len(todo)))
    pool = futures.ProcessPoolExecutor(max_workers=count,
                                       mp_context=multiprocessing.get_context("spawn"),
                                       initializer=_init, initargs=(payload,)) \
        if todo else None
    try:
        with main_not_imported():
            pending = {pool.submit(_link_job, i, jobs[i]) for i in todo}
        # The working file is not written while a worker reads it (a write would wait
        # on the workers' read locks); the parts wait on disk.
        while pending:
            if projector.checkpoint is not None:
                projector.checkpoint()
            finished, pending = futures.wait(pending, timeout=5,
                                             return_when=futures.FIRST_COMPLETED)
            for future in finished:
                out = future.result()
                if resume is not None:
                    resume.job_done(out)
                done[out["index"]] = out
                if log is not None:
                    print(f"link job {len(done)}/{len(jobs)} {jobs[out['index']]}: "
                          f"{out['seconds']} s, {out['links']} links, "
                          f"{out.get('candidates', 0)} candidates, part "
                          f"{Path(out['part']).stat().st_size if out['part'] else 0} B, "
                          f"worker temp {out['temp_bytes']} B", file=log, flush=True)
        if pool is not None:
            pool.shutdown(wait=True)
        # Appended in link type order: the serial phase's rows, seq included. A resumed
        # build appends only the parts its working file does not hold yet (each part's
        # index is recorded in the transaction that appends it).
        conn.execute("BEGIN")
        merged: set[int] = set()
        if resume is not None:
            conn.execute("CREATE TABLE IF NOT EXISTS main.plx_resume_merged "
                         "(idx INTEGER PRIMARY KEY)")
            merged = {row[0] for row in conn.execute("SELECT idx FROM main.plx_resume_merged")}
        for index in range(len(jobs)):
            out = done.pop(index)
            _absorb(projector.report, out["report"])
            if out["part"] and index not in merged:
                conn.execute("COMMIT")   # a database attaches outside a transaction
                conn.execute("ATTACH DATABASE ? AS part", (out["part"],))
                try:
                    conn.execute("BEGIN")
                    conn.execute(f"INSERT INTO main.links ({_LINK_COLUMNS}) SELECT "
                                 f"{_LINK_COLUMNS} FROM part.links ORDER BY seq")
                    if resume is not None:
                        conn.execute("INSERT INTO main.plx_resume_merged VALUES (?)", (index,))
                    conn.execute("COMMIT")
                finally:
                    conn.execute("DETACH DATABASE part")
                    conn.execute("BEGIN")
                if resume is None:
                    Path(out["part"]).unlink()
            stats["links"] += out["links"]
            stats["seconds"][jobs[index]] = out["seconds"]
    except BaseException:
        if pool is not None:
            for process in list((getattr(pool, "_processes", None) or {}).values()):
                process.terminate()
            pool.shutdown(wait=True, cancel_futures=True)
        if resume is None:
            shutil.rmtree(work, ignore_errors=True)
        if not conn.in_transaction:
            conn.execute("BEGIN")
        raise
    projector.report.unit = None
    if resume is not None:
        conn.execute("COMMIT")
        resume.checkpoint(projector, "linked", begin=True, links=stats)
        resume.drop_parts()
    else:
        shutil.rmtree(work, ignore_errors=True)
    return stats


# ----------------------------------------------------------------------------- resume

RESUME_FORMAT = "plx-unit-resume/v1"
# What a checkpoint does not keep of a projector: its sources (read only by the load
# phase), connections, callbacks, caches and what the resumed build's own constructor
# gives again from the same schema (the identity holds the schema digest).
_UNKEPT = frozenset({"sources", "_conn", "_finalizer", "checkpoint", "on_stage",
                     "object_parts", "_cache", "_cites", "_fit_builder", "_fit_resume",
                     "normalizer", "schema", "types", "links", "events", "bindings",
                     "_work_path", "scratch_dir"})


class ResumeRefused(RuntimeError):
    """A checkpoint that cannot be resumed (another build holds it, or it was made by
    other code, schema, plan or captures): never resumed from, never removed."""


def code_digest(*objects: Any) -> str:
    """A digest of the source text of functions, classes or modules (``inspect``)."""
    import inspect
    h = hashlib.sha256()
    for item in objects:
        h.update(b"\x1e" + (inspect.getsource(item) if not isinstance(item, (str, bytes, int))
                            else repr(item)).encode("utf-8"))
    return h.hexdigest()


def link_code() -> str:
    """What a kept job part was made by: this module's link code and the projector's."""
    return code_digest(_link_pairs, _candidates, _link_via, _join_size, _link_job, _init,
                       repr(_TEMP_DDL), K_ROWS_LIMIT, P.Projector)


def _atomic(path: Path, data: bytes) -> None:
    writing = path.with_name(path.name + ".writing")
    with open(writing, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(writing, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Resume:
    """The checkpoints of one unit layer build in ``directory`` (``0700``): its working
    file, the generation file's checkpoint (``StoreBuilder.keep_to_resume``), the
    projector's state after the load (``loaded``) and after the link phase
    (``linked``), each finished link job's part and report, and the values the build
    read once at its start (the delta mark, the sequence). ``identity``: what the
    checkpoints hold depends on (code, schema, plan, captures, output); a manifest of
    another identity is refused (:class:`ResumeRefused`), never resumed or removed,
    unless ``fresh``. One build holds the directory at a time (a lock)."""

    def __init__(self, directory: str | Path, identity: Mapping[str, Any], *,
                 fresh: bool = False) -> None:
        import fcntl
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = open(self.dir / "lock", "a+")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise ResumeRefused("resume_dir_in_use") from None
        self.identity = json.loads(json.dumps(dict(identity), sort_keys=True, default=str))
        self.token = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()
                                    ).hexdigest()[:16]
        self.parts = self.dir / "parts"
        held = self._read()
        if held is not None and not fresh:
            mine = held.get("identity") or {}
            differs = sorted(k for k in set(mine) | set(self.identity)
                             if mine.get(k) != self.identity.get(k))
            if held.get("format") != RESUME_FORMAT:
                differs = ["format"]
            if differs:
                self._lock.close()
                raise ResumeRefused("resume_identity_differs:" + ",".join(differs))
            if held.get("link_code") != link_code():
                if held.get("phase") == "linked":
                    self._lock.close()
                    raise ResumeRefused("resume_link_code_differs_after_links")
                self.drop_parts()   # the jobs run again; the load stands
        if held is None or fresh:
            self._clear()   # a run that never reached a checkpoint, or a fresh start
            held = {"format": RESUME_FORMAT, "identity": self.identity, "phase": None,
                    "values": {}}
        held["link_code"] = link_code()
        self.manifest = held
        self.phase: str | None = held.get("phase")

    @property
    def resuming(self) -> bool:
        return self.phase is not None

    def _read(self) -> dict[str, Any] | None:
        try:
            return json.loads((self.dir / "manifest.json").read_text())
        except FileNotFoundError:
            return None
        except ValueError:
            raise ResumeRefused("resume_manifest_unreadable") from None

    def _write(self) -> None:
        _atomic(self.dir / "manifest.json",
                json.dumps(self.manifest, sort_keys=True, indent=1).encode())

    def _clear(self) -> None:
        for path in self.dir.iterdir():
            if path.name == "lock":
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        held = getattr(self, "manifest", None) or {}
        builder = held.get("builder")
        if builder:
            for suffix in ("", "-journal"):
                Path(builder + suffix).unlink(missing_ok=True)

    def value(self, name: str, compute: Any) -> Any:
        """``name``'s value as the build first read it (``compute()`` then)."""
        values = self.manifest.setdefault("values", {})
        if name not in values:
            values[name] = compute()
            self._write()
        return values[name]

    def jobs(self, lids: Sequence[str]) -> dict[int, dict[str, Any]]:
        """The finished link jobs kept: index -> the job's result (its part whole)."""
        out: dict[int, dict[str, Any]] = {}
        if not self.parts.is_dir():
            return out
        for path in sorted(self.parts.glob("job-*.pickle")):
            job = pickle.loads(path.read_bytes())
            index = job.get("index")
            if not isinstance(index, int) or index >= len(lids) or job.get("lid") != lids[index]:
                continue
            if job["part"] is not None:
                part = self.parts / Path(job["part"]).name
                if not part.is_file():
                    continue
                job["part"] = str(part)
            out[index] = job
        return out

    def job_done(self, out: Mapping[str, Any]) -> None:
        _atomic(self.parts / f"job-{out['index']:05d}.pickle", pickle.dumps(dict(out)))

    def drop_parts(self) -> None:
        shutil.rmtree(self.parts, ignore_errors=True)

    def checkpoint(self, projector: P.Projector, phase: str, *, begin: bool,
                   **extra: Any) -> None:
        """Keep the build as it stands as ``phase``: the working file committed with a
        rollback journal from here on (a later stop rolls back to this point), the
        generation file kept to resume, the projector's state; then the manifest."""
        conn = projector._db
        if conn.in_transaction:
            conn.execute("COMMIT")
        conn.execute("PRAGMA journal_mode = DELETE")
        builder = projector._fit_builder
        if not builder.keep_to_resume({"id": self.token, "phase": phase}):
            raise ResumeRefused("generation_cannot_checkpoint")
        _clear_caches(projector)
        state = {k: v for k, v in vars(projector).items() if k not in _UNKEPT}
        # the record codec: its dictionaries (its compressors are made from them again)
        state["_recs"] = ("plx-rec-codec", dict(projector._recs._dicts))
        try:
            data = pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:  # noqa: BLE001 - name what cannot be kept
            raise ResumeRefused(f"projector_state_not_kept:{exc}") from exc
        _atomic(self.dir / f"state-{phase}.pickle", data)
        finalizer = projector._finalizer
        if finalizer is not None and finalizer.alive:
            finalizer.detach()   # the working file stays for a resumed build
            import weakref
            projector._finalizer = weakref.finalize(projector, conn.close)
        self.manifest.update(phase=phase, work=str(projector._work_path),
                             builder=str(builder.temp), **extra)
        self._write()
        for older in self.dir.glob("state-*.pickle"):
            if older.name != f"state-{phase}.pickle":
                older.unlink()
        self.phase = phase
        if begin:
            conn.execute("BEGIN")

    def restore(self, projector: P.Projector, builder: Any) -> None:
        """Put ``projector`` and the fresh generation ``builder`` back as the build stood
        at its checkpoint (the load phase is not run again: its captures are the
        checkpoint's)."""
        import weakref
        state = pickle.loads((self.dir / f"state-{self.phase}.pickle").read_bytes())
        self.restore_builder(builder)
        _, dicts = state.pop("_recs")

        codec = P._RecCodec()
        for binding, zdict in dicts.items():
            codec._dicts[binding] = zdict
            codec._primed[binding] = P.zlib.compressobj(P._REC_LEVEL, zdict=zdict)
        projector.__dict__.update(state)
        projector._recs = codec
        work = Path(self.manifest["work"])
        if not work.is_file():
            raise ResumeRefused("working_file_missing")
        db = sqlite3.connect(str(work), isolation_level=None)
        db.execute("PRAGMA journal_mode = DELETE")   # a stopped phase's journal rolls back
        db.execute("PRAGMA synchronous = OFF")
        db.execute("PRAGMA temp_store = FILE")
        db.execute(f"PRAGMA cache_size = -{P._SCRATCH_CACHE_KIB}")
        projector._conn, projector._work_path = db, work
        projector._finalizer = weakref.finalize(projector, db.close)
        if self.phase == "linked":
            db.execute("BEGIN")

    def restore_builder(self, builder: Any) -> None:
        """The layer file kept to resume, rolled back to its last commit, in place of
        the fresh one ``builder`` was opened on."""
        from ontology.store import StoreBuilder
        kept, held = StoreBuilder.reopen(self.manifest["builder"], builder.target, seeded=True)
        if held.get("id") != self.token:
            kept.abort()
            raise ResumeRefused("generation_checkpoint_differs")
        builder.discard()
        conn = kept.connection
        conn.execute("ROLLBACK")
        conn.execute(f"PRAGMA foreign_keys = {'OFF' if builder._part else 'ON'}")
        conn.execute("BEGIN IMMEDIATE")
        builder._conn, builder.temp = conn, kept.temp
        builder.kept, builder.checkpoint, builder._shared = True, held, {}
        P.open_generation(builder)

    def finish(self) -> None:
        """The layer is written: every checkpoint goes."""
        work = self.manifest.get("work")
        if work:
            for suffix in ("", "-journal"):
                Path(work + suffix).unlink(missing_ok=True)
        builder = self.manifest.get("builder")
        if builder:
            Path(builder + "-journal").unlink(missing_ok=True)
        self._lock.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def close(self) -> None:
        self._lock.close()


# ----------------------------------------------------------------------------- units

UNITS_ENV = "PLX_UNIT_LAYER_UNITS"   # "parallel" (default): the units on worker processes
DONE_TABLE = "plx_resume_done"


def _units_init(payload: bytes) -> None:
    """A unit worker (``ontology.project_parallel``): its link specs find whole keys at
    once and stay within the candidate budget, as the build's own do."""
    from ontology import project_parallel as par
    P.Projector._link_pairs = _link_pairs
    P.Projector._candidates = _candidates
    P.Projector._link_via = _link_via
    par._init(payload)


def _units_mode() -> str:
    mode = os.environ.get(UNITS_ENV) or "parallel"
    if mode not in ("parallel", "serial"):
        raise ValueError(f"{UNITS_ENV}_invalid:{mode!r}")
    return mode


def parallel_units(projector: P.Projector, builder: Any, serial_into: Any, *,
                   workers: int | None = None, log: Any = None,
                   resume: "Resume | None" = None) -> dict[str, Any]:
    """Project ``projector``'s units (a partial build: ``emit``, ``scope``) into
    ``builder`` as an incremental build projects its changed units
    (``ontology.project_parallel.project``, ``project_incremental._parallel_units``): each
    task (a unit, or the units that must be projected together) by a worker process into
    a part, longest first, merged into the layer as it ends; the units no worker can
    project alone (a ``same_record`` link unit whose ends are not projected with it) and
    those of a task whose worker could not read the build's bytes, afterwards in this
    process (``serial_into``: the fitted projection of those units). The layer holds a
    partial build's rows (the merge lays them down in another order: its logical rows are
    the single projector's). With ``resume``, the layer file is kept to resume and each
    merged task is recorded with its rows (one transaction): a stopped build merges only
    the tasks not merged yet."""
    from dataclasses import replace
    from ontology import project_incremental as pi
    from ontology import project_parallel as par

    started = time.monotonic()
    count = _workers(workers)
    the_plan = pi.plan(projector.schema)
    affected = set(projector.emit or ())
    stats: dict[str, Any] = {"mode": "parallel_units", "units": len(affected)}
    conn = builder.connection
    if resume is not None and not resume.resuming:
        if not builder.keep_to_resume({"id": resume.token, "phase": "units"}):
            raise ResumeRefused("generation_cannot_checkpoint")
        resume.manifest.update(phase="units", builder=str(builder.temp))
        resume._write()
        resume.phase = "units"
    if conn.in_transaction:
        conn.execute("COMMIT")
    conn.execute(f"CREATE TABLE IF NOT EXISTS main.{DONE_TABLE} (units TEXT PRIMARY KEY)")
    done = {u for (text,) in conn.execute(f"SELECT units FROM main.{DONE_TABLE}")
            for u in json.loads(text)}
    stats["resumed_units"] = len(done)
    chosen = pi._parallel_units(the_plan, affected, projector.objects or {}, count,
                                {}) if count > 1 else set()
    todo = chosen - done
    result = None
    if todo:
        # merged in a transaction each (a part's rows and its record together); the
        # layer's indexes stay (a resumed merge finds them), its foreign keys off
        builder.seed_mode = builder.seed_mode or "unit_layer"
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")   # the projection commits it before its pool
        marks: dict[str, Any] = {}
        shared: list[CollapsingMirror] = []   # their kept copies' files go with the pool
        sources = dict(projector.sources)
        wanted: dict[str, set[str]] = {}
        for unit in todo:
            for bid in the_plan.units[unit]:
                b = the_plan.bindings[bid][1]
                if b["source"] in sources:
                    wanted.setdefault(b["source"], set()).add(b["relation"])
        for name, relations in sorted(wanted.items()):
            source = sources[name]
            inner = getattr(source, "_inner", source)
            collapsing = isinstance(inner, CollapsingMirror) and inner.kept_file is None
            if collapsing and inner.kept_dir is None:
                # computed once here, for every relation it collapses (``_kept_file``)
                inner.kept_dir = str(resume.dir if resume is not None else
                                     projector.scratch_dir or Path(builder.temp).parent)
                shared.append(inner)
            records = source.records(sorted(relations))   # its snapshot, pinned
            mark = source.watermark() if callable(getattr(source, "watermark", None)) else None
            if collapsing and inner.opened_with is not None and set(relations) & set(inner.collapse):
                # every worker loads this very file and never computes it; and reads the
                # parent's records while its relation marks are the parent's
                if inner.kept_path is None or not inner.kept_path.is_file():
                    raise P.ProjectionError("kept_copies_not_saved", f"kept_copies_not_saved:{name}")
                kwargs = {k: v for k, v in inner.opened_with[1].items() if k != "kept_dir"}
                inner.opened_with = (inner.opened_with[0], {
                    **kwargs, "kept_file": str(inner.kept_path),
                    "pinned": [mark, inner.relation_marks(sorted(relations))]})
            marks[name] = {"mark": P.canonical(mark) if mark is not None else None}
            sources[name] = pi._Pinned(source, records)
        hints = _hints()
        sub = replace(the_plan, units={u: the_plan.units[u] for u in sorted(todo)},
                      rows={u: the_plan.rows[u] for u in sorted(todo)})
        merge_rows = par._merge_rows
        order = par.tasks(sub, par.pieces(max(1, min(count, len(todo)))))

        def recorded(builder_: Any, main: sqlite3.Connection, links: Any) -> None:
            merge_rows(builder_, main, links)
            if resume is None:
                return
            (name,) = [row[2] for row in main.execute("PRAGMA database_list")
                       if row[1] == "part"]
            index = int(re.search(r"part-(\d+)\.sqlite", Path(name).name).group(1))
            main.execute(f"INSERT INTO main.{DONE_TABLE} VALUES (?)",
                         (json.dumps(list(order[index])),))

        saved = (par._init, par._merge_rows)
        par._init, par._merge_rows = _units_init, recorded
        try:
            result = par.project(builder, schema=projector.schema, sources=sources,
                                 marks=marks, the_plan=sub, normalizer=projector.normalizer,
                                 as_known_at=projector.cut, batch_size=projector.batch,
                                 shards=projector.shards,
                                 scratch_dir=projector.scratch_dir or Path(builder.temp).parent,
                                 count=count, hints=hints)
        except par.ParallelUnavailable as why:
            if "kept_copies_missing" in str(why):   # never computed again in a worker
                raise P.ProjectionError("kept_copies_missing", str(why)) from None
            result = None
            stats["unavailable"] = str(why)
        finally:
            par._init, par._merge_rows = saved
            if resume is None:   # a resumed build keeps them in its directory to resume
                for inner in shared:
                    if inner.kept_path is not None:
                        inner.kept_path.unlink(missing_ok=True)
        if result is not None and "kept_copies_missing" in str(result.unavailable or ""):
            raise P.ProjectionError("kept_copies_missing", str(result.unavailable))
        if result is not None:
            done |= set(result.done)
            stats.update(workers=result.workers, tasks=len(result.seconds),
                         merge_seconds=result.merge_seconds,
                         work_peak=result.work_peak, part_bytes=result.part_bytes,
                         unavailable=result.unavailable)
            stats["task_seconds"] = dict(result.seconds)
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
    elif not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    rest = sorted(affected - done)
    stats["serial_units"] = len(rest)
    stats["parallel_seconds"] = round(time.monotonic() - started, 1)
    if log is not None:
        print(f"units: {json.dumps({k: v for k, v in stats.items() if k != 'task_seconds'})}",
              file=log, flush=True)
    if rest:
        t = time.monotonic()
        serial_into(rest)
        stats["serial_seconds"] = round(time.monotonic() - t, 1)
    conn = builder.connection
    conn.execute(f"DROP TABLE IF EXISTS main.{DONE_TABLE}")
    return stats


HINTS_ENV = "PLX_UNIT_LAYER_HINTS"   # a JSON file: task (first unit) -> seconds measured


def _hints() -> dict[str, float]:
    """The measured seconds of each task (its first unit) a rehearsal wrote, for the
    longest-first order; none when unset or unreadable."""
    path = os.environ.get(HINTS_ENV)
    if not path:
        return {}
    try:
        held = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in held.items() if isinstance(v, (int, float))}


# ----------------------------------------------------------------------------- wiring

@contextlib.contextmanager
def fitted(schema: Any, *, workers: int | None = None, log: Any = None,
           resume: Resume | None = None) -> Iterator[dict]:
    """While open, a unit layer build (``ontology.schema_migrate.build_from_state``)
    reads the mirror through :class:`CollapsingMirror`, projects its link phase with
    :func:`parallel_links` (each join within the candidate budget) and, with
    ``resume``, checkpoints itself there and resumes from its last checkpoint. Logs
    each phase's time and bytes and each binding's load time. Yields what each did."""
    seen: dict[str, Any] = {"unread": {}, "links": None, "stages": {}}
    opener = mirror_opener(schema)
    saved = (P.MirrorCaptures, P.Projector._project_links, P.Projector._load,
             P.Projector.project_into, P.Projector._project_binding)

    def mirror(path: str | Path, **kwargs: Any) -> CollapsingMirror:
        source = opener(path, **kwargs)
        seen["mirror"] = source
        return source

    def own(self: P.Projector) -> Any:
        """The checkpoints of this projector's build: the layer's own projector only."""
        return getattr(self, "_fit_resume", None)

    def links(self: P.Projector) -> None:
        started = time.monotonic()
        seen["links"] = parallel_links(self, workers, log=log, resume=own(self))
        seen["links"]["total_seconds"] = round(time.monotonic() - started, 1)
        if log is not None:
            print(f"links: {json.dumps({k: v for k, v in seen['links'].items() if k != 'seconds'})}",
                  file=log, flush=True)

    def load(self: P.Projector, builder: Any) -> None:
        held = own(self)
        if held is not None and held.resuming:
            held.restore(self, builder)
            if log is not None:
                print(f"resumed at {held.phase}", file=log, flush=True)
            return
        saved[2](self, builder)

    def project_binding(self: P.Projector, tid: str, b: Mapping[str, Any], *args: Any,
                        **kwargs: Any) -> None:
        started = time.monotonic()
        saved[4](self, tid, b, *args, **kwargs)
        if log is not None:
            print(f"binding {b['id']}: {round(time.monotonic() - started, 2)} s",
                  file=log, flush=True)

    def project_into(self: P.Projector, builder: Any) -> None:
        from ontology.store import CHECKPOINT_META
        top = not getattr(self, "_fit_serial", False)
        held = resume if top else None
        if held is not None:
            self.scratch_dir = held.dir   # the working file and the parts kept there
            self._fit_builder = builder
            self._fit_resume = held
        clock = [time.monotonic()]

        def stage(name: str, work: int, store: int) -> None:
            now = time.monotonic()
            seen["stages"][name] = {"seconds": round(now - clock[0], 1), "work_bytes": work,
                                    "store_bytes": store}
            clock[0] = now
            if log is not None:
                print(f"stage {name}: {json.dumps(seen['stages'][name])}", file=log,
                      flush=True)
        if self.on_stage is None:
            self.on_stage = stage
        if top and self.emit is not None and _units_mode() == "parallel":
            if held is not None and held.resuming:
                if held.phase != "units":
                    raise ResumeRefused(f"resume_of_another_mode:{held.phase}")
                held.restore_builder(builder)
            from ontology import project_incremental as pi
            the_plan = pi.plan(self.schema)

            def serial_into(units: Sequence[str]) -> None:
                rest = P.Projector(
                    self.schema, self.sources, normalizer=self.normalizer,
                    as_known_at=self.cut, batch_size=self.batch,
                    scratch_dir=self.scratch_dir,
                    scope=set().union(*(the_plan.units[u] for u in units)), emit=units,
                    shards=self.shards, objects=self.objects)
                rest._fit_serial = True
                rest.project_into(builder)
            builder.packed_bindings = frozenset(
                bid for bid, (_, b) in self.bindings.items()
                if b.get("value_storage") == "packed")
            try:
                seen["units"] = parallel_units(self, builder, serial_into, workers=workers,
                                               log=log, resume=held)
            finally:
                self.close()
        else:
            if held is not None and held.phase == "units":
                raise ResumeRefused("resume_of_another_mode:units")
            saved[3](self, builder)
        if held is not None:   # the layer holds no checkpoint of its own
            builder.connection.execute("DELETE FROM store_meta WHERE key = ?",
                                       (CHECKPOINT_META,))

    P.MirrorCaptures = mirror
    P.Projector._project_links = links
    P.Projector._load = load
    P.Projector.project_into = project_into
    P.Projector._project_binding = project_binding
    try:
        with _whole_keys():
            yield seen
    finally:
        (P.MirrorCaptures, P.Projector._project_links, P.Projector._load,
         P.Projector.project_into, P.Projector._project_binding) = saved
        if "mirror" in seen:
            seen["unread"] = dict(seen["mirror"].unread)
