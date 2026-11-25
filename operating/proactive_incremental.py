"""Incremental proactive passes: the store-only rules judged again only where the data moved.

A full proactive run (``operating.proactive_engine.run``) reads every record of the store:
up to an hour and several GB on the live store, so its findings trail the data. This pass
runs after each row-delta cycle (``ontology.delta``; ``deploy/systemd/plx-proactive-inc``,
every 5 minutes) and judges again only what the changes since the last pass can move, with
the same rules and the same code, so what it folds equals what a full run would fold:

1. **Changes** (``changes``): the records the delta layers own (``delta_owned`` of each
   layer the served pointer lists that this pass has not taken in yet; on Postgres only a
   layer Postgres has applied, ``ontology.load_state``), each fingerprinted in the store
   the engine reads (its type, audience, current versions' digests and markings, whether it
   is removed, and every current link at it): a record whose fingerprint is the one kept is
   unchanged (a compacted layer re-owning it changes nothing). Without a kept baseline for
   the served generation every owned record counts as changed (more work, never less).
   On Postgres with the change feed (migration 0026, ``operating.pg.change_feed``) the
   candidates are instead the records the applied cycles really changed past this pass's
   cursor (``_feed``; the applied layers are marked taken unopened); a migration's scope
   or more rows than ``FEED_LIMIT`` judge the rules whole; the first read and a gap read
   the layers as above, then follow the feed. Without the feed nothing changes.
2. **Subjects** (``plan``): per rule, the subjects whose declared inputs (``INPUTS``: the
   link types it reads, the hops from its subject, the identity links followed whole and
   the shared records it reads but never walks through) hold a changed record, by a walk
   back from the changed records and from the records they were linked to before (the
   generation's links and the kept ones: a link that went away moves both its ends), plus
   the subjects of the rule's open gaps that cite a changed record. Then **the scope**:
   every record those subjects read, by the same walk forward.
3. **Judge** (``proactive_engine.run(scope=)``): the rules run over the scope only (a
   whole-type read lists the scope's records, a link read the links at them) and keep the
   gaps about their own subjects; the fold writes those and resolves a subject's open gap
   it no longer finds. The run records, coverage counts and generation stay the full
   run's.

**Time**: every rule judges on the business day (ET, ``po_status.business_today``), so
within a day only data moves a claim; on the first pass of a new day the rules whose truth
moves with the date (due dates, expiry: ``DATED``) are judged again whole (an exact
re-evaluation of every dated claim, once a day), the others incrementally.

**Safety nets**: a scope over ``READ_CAP`` records, a rule without declared inputs or one
that reads more than its subject's links (``WHOLE``) is judged whole instead; a served
generation or engine code the inbox's full run has not read (``served_behind``) is left to
the full pass (``operating.vendor_watch --refresh new``), and a nightly full pass
(``plx-proactive``) re-reads everything. The engine's runs and this pass hold one lock per
inbox (``proactive_engine.run``): they never fold over each other.

**Watchlists**: the records that moved are also judged against the people's follows and
alert rules (``operating.watchlists``), whose alerts go to each owner's own inbox.

Read only on the store and the delta layers; writes only the inbox, its change file
``<inbox>.changes.sqlite`` (fingerprints, counts) and the watchlists file beside the store
(``<store>.watchlists.sqlite``: snapshots and alerts). Run::

    python3 -m operating.proactive_incremental --state-dir STATE [--as-of YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import resource
import sqlite3
import sys
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology import store as ostore

from . import po_status
from . import proactive_engine as pe
from . import proactive_engine_inbox
from operating import declared_schema as _schemas
from operating.declared_schema import proactive_incremental as _PROACTIVE_INCREMENTAL

CHANGES_SUFFIX = ".changes.sqlite"
READ_CAP = 400_000   # a scope larger than this: its rules are judged whole instead
CHUNK = 500
# a pass finding the full engine running exits at once ("busy"): it kept what it had seen
# (the delta layers' fingerprints), so the next timer tick or publish judges everything that
# changed meanwhile. Waiting for a full pass held the unit for up to 15 minutes a tick
# (20 wall-hours of waiting in two days, CC-P-03); ``--wait-seconds`` still asks for it.
LOCK_WAIT_SECONDS = 0.0
# the fingerprint's form (``fingerprints``): kept prints of another form are dropped
PRINT_FORMAT = 2
PRINT_ROWS = 100_000   # link rows a fingerprint read takes at once (``_link_reads``)


@dataclass(frozen=True)
class Inputs:
    """What one rule's verdict on a subject reads: ``subjects`` (the types its gaps are
    about), ``links`` (every link type it reads, walked both ways up to ``hops`` from the
    subject), ``identity`` (link types whose clusters it reads whole: followed to the end,
    no hop counted) and ``hubs`` (links to a record many subjects share, a party or an
    item: read, never walked through, so only a change of that record walks back over
    them)."""
    subjects: tuple[str, ...]
    links: tuple[str, ...]
    hops: int
    identity: tuple[str, ...] = ()
    hubs: tuple[str, ...] = ()


# Per rule, its declared inputs (operating/proactive_engine.py and the modules it reads;
# a link type a rule reads that is missing here makes this pass wrong, so the equivalence
# test, operating/tests/test_proactive_incremental.py, compares a full run on every change
# it seeds). Filled from ``_declared`` (the engine's own link-type constants).
INPUTS: dict[str, Inputs] = {}
# Rules judged whole on every pass (they read what no link walk from a subject reaches).
WHOLE: frozenset[str] = frozenset()
# Rules whose truth or wording moves with the business day: judged whole on a new day.
DATED: frozenset[str] = frozenset()
# The rules an incremental pass judges: the store-only ones (the tool-call rules read
# through ``plx.process``/``plx.investigate`` and stay with the full and vendor passes,
# the customer-PO scan reads the Portal mirror, not the store).
STORE_ONLY = tuple(d for d in pe.DETECTORS
                   if d in pe.SET_BASED and d != "order_without_customer_po")
NOT_INCREMENTAL: frozenset[str] = frozenset()


def _declared() -> None:
    """Fill ``INPUTS``, ``WHOLE`` and ``DATED`` from the engine's constants (lazily: the
    schema's identity links are read once)."""
    global WHOLE, DATED, NOT_INCREMENTAL
    if INPUTS:
        return
    from . import declared_inputs
    INPUTS.update(declared_inputs.inputs())
    WHOLE = frozenset(declared_inputs.WHOLE)
    DATED = frozenset(declared_inputs.DATED)
    NOT_INCREMENTAL = frozenset(declared_inputs.NOT_INCREMENTAL)


# --------------------------------------------------------------------------- change file

KEEP_PASSES = 2000


def changes_path(inbox: Any) -> Path:
    inbox = Path(inbox)
    return inbox.with_name(inbox.name + CHANGES_SUFFIX)


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    _schemas.apply(conn, _PROACTIVE_INCREMENTAL)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _meta(conn: sqlite3.Connection | None, key: str) -> Any:
    if conn is None:
        return None
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))


# --------------------------------------------------------------------------- changes

def _chunks(items: Sequence[Any], size: int = CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def read_kinds() -> frozenset[str]:
    """Every link type an incremental rule's walk follows (``INPUTS``: links, identity
    clusters, hubs). A link of another type moves no subject any rule judges here, so the
    neighbours kept, the earlier links and the walks read only these: a mailbox owner's
    person record holds ~0.5 M mail links of no rule's type (11-17: fingerprinting and
    walking them grew the pass past its 12 GB cap)."""
    _declared()
    return frozenset(t for spec in INPUTS.values()
                     for t in (*spec.links, *spec.identity, *spec.hubs))


def fingerprints(st: pe._Store, ids: Iterable[str], kinds: Iterable[str] | None = None
                 ) -> dict[str, tuple[str, list[str]]]:
    """object id -> (fingerprint, the records it is linked to) as the store reads now: its
    type and audience, its current versions (handle, digest, markings), whether it is
    removed, and every current link at it (type, ends, tier, markings). A record the store
    does not hold has the fingerprint ``absent``.

    The links are folded into the digest as a set (each row's own hash summed), in reads
    of at most ``PRINT_ROWS`` rows (``_link_reads``), never holding a record's links: a
    person with half a million mail links costs a bounded read at a time. ``kinds``: the
    link types its neighbours are listed over (None: every type); the fingerprint still
    covers every link (the watchlists are told of any change)."""
    ids = sorted(set(ids))
    keep = None if kinds is None else frozenset(kinds)
    out: dict[str, tuple[str, list[str]]] = {}
    for part in _chunks(ids):
        held = json.dumps(list(part))
        head: dict[str, list[Any]] = {}
        for r in st.conn.execute(
                "SELECT o.object_id, o.object_type, o.audience_key, "
                + ostore.removed_at_sql("o.object_id") + " AS gone FROM objects o "
                "WHERE o.object_id IN (SELECT value FROM json_each(?))", (held,)):
            head[r[0]] = [("o", r[1], r[2], bool(r[3]))]
        for r in st.conn.execute(
                "SELECT object_id, l1_handle, digest, markings FROM object_versions "
                "WHERE superseded_at IS NULL AND object_id IN (SELECT value FROM json_each(?))",
                (held,)):
            if r[0] in head:
                head[r[0]].append(("v", r[1], r[2], r[3]))
        sums = dict.fromkeys(head, 0)
        neighbors: dict[str, set[str]] = {oid: set() for oid in part}
        for end, rows in _link_reads(st, list(part)):
            mark = end[0].encode()
            for r in rows:
                if r[0] in sums:
                    row = mark + json.dumps(list(r[2:])).encode()
                    sums[r[0]] = (sums[r[0]] + int.from_bytes(
                        hashlib.sha256(row).digest()[:16], "big")) % (1 << 128)
                if keep is None or r[3] in keep:
                    neighbors[r[0]].add(r[1])
        for oid in part:
            if oid not in head:
                out[oid] = ("absent", sorted(neighbors[oid]))
                continue
            h = hashlib.sha256(b"print2")
            for row in sorted(head[oid], key=repr):
                h.update(json.dumps(row).encode() + b"\n")
            h.update(b"links:%032x" % sums[oid])
            out[oid] = (h.hexdigest()[:32], sorted(neighbors[oid]))
    return out


_LINKS_AT = ("SELECT l.{end}, l.{other}, l.link_id, l.link_type, l.from_object, l.to_object, "
             "l.tier, l.markings FROM link_rows l WHERE l.superseded_at IS NULL AND ")


def _link_reads(st: pe._Store, ids: list[str]) -> Iterator[tuple[str, list[Any]]]:
    """(the end read, rows (that end, the other, link id, type, from, to, tier, markings))
    of every current link at ``ids``, each read at most ``PRINT_ROWS`` rows: a read over
    more is split in halves, and one record over it alone (a mailbox owner's person) is
    read a page at a time in its key order. A read returns all its rows at once
    (Postgres), and the ids of a chunk sort side by side, so a few mailbox owners shared
    one read of millions of rows (11-17)."""
    for end, other in (("from_object", "to_object"), ("to_object", "from_object")):
        stack = [ids]
        while stack:
            part = stack.pop()
            rows = st.conn.execute(
                _LINKS_AT.format(end=end, other=other)
                + f"l.{end} IN (SELECT value FROM json_each(?)) LIMIT ?",
                (json.dumps(part), PRINT_ROWS + 1)).fetchall()
            if len(rows) <= PRINT_ROWS:
                yield end, rows
            elif len(part) > 1:
                half = len(part) // 2
                stack.extend((part[half:], part[:half]))
            else:
                del rows
                offset = 0
                while True:
                    page = st.conn.execute(
                        _LINKS_AT.format(end=end, other=other) + f"l.{end} = ? "
                        "ORDER BY l.link_id, l.link_type LIMIT ? OFFSET ?",
                        (part[0], PRINT_ROWS, offset)).fetchall()
                    yield end, page
                    if len(page) < PRINT_ROWS:
                        break
                    offset += len(page)


def _pointer(store: Path, pointer_file: Path | None = None) -> tuple[str | None, list[str]]:
    """(the base digest, the layer stores newest first) of the served delta pointer."""
    from . import delta_overlay

    path = pointer_file if pointer_file is not None else delta_overlay.pointer_path(store)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, []
    if not isinstance(raw, dict):
        return None, []
    layers = [str(layer.get("store")) for layer in raw.get("layers") or ()
              if isinstance(layer, dict) and layer.get("store")]
    if not layers and raw.get("store"):
        layers = [str(raw["store"])]
    return raw.get("base_digest"), layers


def _applied(store: Path) -> set[str] | None:
    """The layer file names Postgres has applied when the engine reads Postgres
    (``operating.pg.ontology_delta``); None: the engine reads the store file itself."""
    from operating.pg import ontology_reader

    if not ontology_reader.serves(store):
        return None
    import psycopg

    with psycopg.connect(ontology_reader.dsn(), connect_timeout=10) as conn:
        return {row[0] for row in conn.execute(
            "SELECT substr(name, 7) FROM ontology.load_state WHERE name LIKE 'delta:%'")}


def generation_digest(stamp: str | None) -> str | None:
    """The generation's content digest within ``stamp`` (a SQLite store's digest as it
    is; Postgres's ``<digest>@<data version>``, the part before the ``@``)."""
    if not isinstance(stamp, str):
        return None
    return stamp.partition("@")[0] or None


def layer_owned(store: Path, digest: str | None, ch: sqlite3.Connection | None,
                applied: set[str] | None, *, reread: bool = False,
                seen_layers: set[str] | None = None,
                pointer_file: Path | None = None,
                owned_cache: Mapping[str, Sequence[str]] | None = None
                ) -> tuple[set[str], list[str], bool]:
    """(the records owned by the served layers not taken in yet, those layers, whether a
    layer is still waiting for Postgres): a layer of another generation is never read.
    ``applied``: the layers the store read holds (Postgres's applied ones; None: every
    layer, a store built with them). ``reread``: the layers already taken in are read
    again too (the change feed missed rows: ``feed_owned``)."""
    base, layers = _pointer(store, pointer_file=pointer_file)
    if base is None or base != digest:
        return set(), [], False
    if reread:
        seen: set[str] = set()
    elif seen_layers is not None:
        seen = set(seen_layers)
    elif ch is not None:
        seen = {row[0] for row in ch.execute(
            "SELECT layer FROM layers WHERE base_digest = ?", (digest,))}
    else:
        seen = set()
    owned: set[str] = set()
    taken, waiting = [], False
    for layer in layers:
        if layer in seen:
            continue
        if applied is not None and Path(layer).name not in applied:
            waiting = True   # not in Postgres yet: the next pass takes it in
            continue
        if owned_cache is not None and layer in owned_cache:
            owned.update(owned_cache[layer])
            taken.append(layer)
            continue
        try:
            with closing(sqlite3.connect(f"file:{layer}?mode=ro", uri=True)) as conn:
                owned.update(row[0] for row in conn.execute("SELECT object_id FROM delta_owned"))
        except sqlite3.Error:
            waiting = True   # pruned or unreadable now: a later layer holds its records
            continue
        taken.append(layer)
    return owned, taken, waiting


# The change feed (``operating.pg.change_feed``, migration 0026): with the engine on
# Postgres, the records each applied cycle really changed, past this pass's cursor, instead
# of every record the layers own (each owns every record changed since the generation).
FEED_CONSUMER = "proactive_incremental"
FEED_LIMIT = 4 * READ_CAP   # rows read at once; more: the rules are judged whole


def _feed(digest: str | None, applied: set[str] | None, *,
          require_writable: bool = True) -> Any:
    """The feed's rows past this pass's cursor for the generation ``digest`` when the
    engine reads Postgres (``applied`` is its applied layers); None without the feed (not
    migrated, unreadable): the layers are read as before."""
    if applied is None or digest is None:
        return None
    from operating.pg import change_feed, ontology_reader
    return change_feed.read(ontology_reader.dsn(), FEED_CONSUMER, generation=digest,
                            limit=FEED_LIMIT, require_writable=require_writable)


def feed_owned(store: Path, digest: str | None, ch: sqlite3.Connection | None,
               applied: set[str], batch: Any, *,
               seen_layers: set[str] | None = None,
               pointer_file: Path | None = None) -> tuple[set[str], list[str], bool]:
    """``layer_owned`` from a feed batch read ``ok``: its records, and the applied layers
    not taken in yet marked taken without being opened (their changes are the feed's; the
    delta's release rule reads them), whether a layer still waits for Postgres."""
    base, layers = _pointer(store, pointer_file=pointer_file)
    if base is None or base != digest:
        return batch.ids(), [], False
    if seen_layers is not None:
        seen = set(seen_layers)
    elif ch is not None:
        seen = {row[0] for row in ch.execute("SELECT layer FROM layers WHERE base_digest = ?",
                                             (digest,))}
    else:
        seen = set()
    taken = [layer for layer in layers
             if layer not in seen and Path(layer).name in applied]
    waiting = any(Path(layer).name not in applied for layer in layers)
    return batch.ids(), taken, waiting


class BaseUnreadable(RuntimeError):
    """No store the changed records' earlier links can be read from: the pass stops with
    an error (judging with no earlier neighbours would silently judge less)."""


def _neighbors_sql(conn: Any, ids: Sequence[str], sql: str, typed: str,
                   param: Callable[[list], Any], out: dict[str, set[str]],
                   types: Iterable[str] | None = None) -> None:
    """``sql`` per end, ``typed`` appended (the link types bound after the ids) when only
    ``types`` are read: filtered in the read, so a mailbox owner's mail links never
    come back."""
    kinds = None if types is None else param(sorted(types))
    for part in _chunks(sorted(ids)):
        held = param(list(part))
        for end, other in (("from_object", "to_object"), ("to_object", "from_object")):
            text = sql.format(end=end, other=other) + ("" if kinds is None else typed)
            for a, b in conn.execute(text, (held,) if kinds is None else (held, kinds)):
                out.setdefault(a, set()).add(b)


def _file_neighbors(base: Path, ids: Sequence[str], out: dict[str, set[str]],
                    types: Iterable[str] | None = None) -> None:
    """The links of ``ids`` in the generation file ``base``; sqlite3.Error when unreadable."""
    with closing(sqlite3.connect(f"file:{base}?mode=ro", uri=True)) as conn:
        _neighbors_sql(conn, ids, "SELECT {end}, {other} FROM link_rows WHERE superseded_at "
                       "IS NULL AND {end} IN (SELECT value FROM json_each(?))",
                       " AND link_type IN (SELECT value FROM json_each(?))", json.dumps,
                       out, types)


def _pg_neighbors(ids: Sequence[str], out: dict[str, set[str]],
                  target: str | None = None, types: Iterable[str] | None = None) -> None:
    """The links of ``ids`` in the Postgres ontology (``PLX_PG_DSN``), read by the link end
    indexes. Raises when it cannot be read or holds no loaded links (a load in progress)."""
    import psycopg
    from operating.pg import ontology_reader

    with psycopg.connect(target or ontology_reader.dsn(), connect_timeout=10) as conn:
        if conn.execute("SELECT 1 FROM ontology.load_state WHERE name = 'table:link_rows'"
                        ).fetchone() is None:
            raise BaseUnreadable("postgres_links_not_loaded")
        _neighbors_sql(conn, ids, "SELECT {end}, {other} FROM ontology.link_rows WHERE "
                       "superseded_at IS NULL AND {end} = ANY(%s)",
                       " AND link_type = ANY(%s)", list, out, types)


def _base_neighbors(base: Path | None, ids: Sequence[str], *,
                    postgres: bool = False,
                    types: Iterable[str] | None = None) -> dict[str, set[str]]:
    """The records each of ``ids`` was linked to before the changes being taken in.

    The engine reading the SQLite generation (``postgres`` False): the links in the
    generation file ``base`` (what the full run's data held before any layer).
    The engine reading Postgres: Postgres's links (``_pg_neighbors``; it applies each layer
    in place, so a link a layer removed is gone from it), together with the generation
    file's when one is still there (``PLX_ONTOLOGY_PG=1``: the links before any layer, a
    superset judged, never less). With Postgres the only store the file is gone and
    Postgres alone is read.
    ``types``: only links of these types (``read_kinds``; None: every type).
    Raises :class:`BaseUnreadable` when no store can be read (Postgres down or not loaded,
    or the SQLite generation file missing or unreadable): never an empty answer that
    would judge fewer subjects."""
    out: dict[str, set[str]] = {}
    if not ids:
        return out
    path = Path(base) if base is not None else None
    if postgres:
        try:
            _pg_neighbors(ids, out, types=types)
        except BaseUnreadable:
            raise
        except Exception as error:  # noqa: BLE001 - unreadable: the pass stops, loudly
            raise BaseUnreadable(f"postgres_unreadable:{type(error).__name__}") from error
        if path is not None and path.is_file():
            try:
                _file_neighbors(path, ids, out, types)
            except sqlite3.Error:
                pass   # Postgres was read; the file is the optional earlier half
        return out
    if path is None or not path.is_file():
        raise BaseUnreadable(f"generation_file_missing:{path}")
    try:
        _file_neighbors(path, ids, out, types)
    except sqlite3.Error as error:
        raise BaseUnreadable(f"generation_file_unreadable:{type(error).__name__}") from error
    return out


# --------------------------------------------------------------------------- the scope

def _walk(st: pe._Store, start: Iterable[str], spec: Inputs, *, back: bool,
          links: dict[str, list[tuple]], types: Iterable[str] | None = None) -> set[str]:
    """Every record within ``spec.hops`` link hops of ``start`` over the rule's links,
    identity clusters taken whole at each step. Forward (``back`` False: what a subject
    reads), a shared record reached by a hub link is read, never walked through; back
    (what a changed record moves), a hub link is walked only from a changed record itself."""
    kinds = set(spec.links) | set(spec.identity) | set(spec.hubs)
    hubs, identity = set(spec.hubs), set(spec.identity)

    def at(ids: Iterable[str]) -> list[tuple]:
        need = [i for i in ids if i not in links]
        if need:
            for oid in need:
                links[oid] = []
            for row in st.touching(need, None if types is None else sorted(types)):
                links.setdefault(row[3], []).append(row) if row[3] in links else None
                if row[4] != row[3]:
                    links.setdefault(row[4], []).append(row) if row[4] in links else None
        return [row for i in ids for row in links[i]]

    def cluster(ids: set[str]) -> set[str]:
        grown, edge = set(ids), set(ids)
        while edge and identity:
            nxt = set()
            for row in at(edge):
                if row[2] in identity:
                    nxt.update((row[3], row[4]))
            edge = nxt - grown
            grown |= edge
        return grown

    reached = cluster(set(start))
    frontier = set(reached)
    seeds = set(start)
    terminal: set[str] = set()
    for _hop in range(spec.hops):
        nxt: set[str] = set()
        for node in frontier - terminal:
            for row in links.get(node) or at([node]):
                if row[2] not in kinds:
                    continue
                other = row[4] if row[3] == node else row[3]
                if row[2] in hubs:
                    if back and node not in seeds:
                        continue
                    if not back:
                        terminal.add(other)
                nxt.add(other)
        nxt = cluster(nxt - reached) if nxt - reached else set()
        frontier = nxt
        reached |= nxt
        if not frontier:
            break
    return reached


def _types(st: pe._Store, ids: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in _chunks(sorted(set(ids))):
        for oid, kind in st.conn.execute(
                "SELECT object_id, object_type FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)):
            out[oid] = kind
    return out


def plan(st: pe._Store, changed: set[str], before: Mapping[str, set[str]],
         inbox: Path, rules: Sequence[str], kinds: Iterable[str] | None = None
         ) -> tuple[dict[str, frozenset[str]], set[str]]:
    """({rule: the subjects it judges again}, the records they read) for the records that
    changed (``changed``) and the records each was linked to before (``before``).
    ``kinds``: the link types the walks read (default ``read_kinds``)."""
    _declared()
    kinds = read_kinds() if kinds is None else frozenset(kinds)
    seeds = set(changed)
    for oid in changed:
        seeds |= before.get(oid, set())
    cited: dict[str, set[str]] = {}
    if inbox.is_file():
        with closing(sqlite3.connect(f"file:{inbox}?mode=ro", uri=True)) as conn:
            for part in _chunks(sorted(seeds)):
                for detector, subject in conn.execute(
                        "SELECT DISTINCT g.detector, g.object_id FROM gaps g JOIN gap_cites c "
                        "ON c.gap_id = g.gap_id WHERE g.state = 'open' AND c.object_id IN "
                        "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)):
                    cited.setdefault(detector, set()).add(subject)
    links: dict[str, list[tuple]] = {}
    moved: dict[str, set[str]] = {}
    for rule in rules:
        moved[rule] = _walk(st, seeds, INPUTS[rule], back=True, links=links, types=kinds) \
            | cited.get(rule, set())
    otypes = _types(st, set().union(*moved.values()) if moved else ())
    subjects: dict[str, frozenset[str]] = {}
    read: set[str] = set()
    for rule in rules:
        spec = INPUTS[rule]
        mine = frozenset(oid for oid in moved[rule]
                         if otypes.get(oid) in spec.subjects or "*" in spec.subjects
                         or oid in cited.get(rule, ()))
        subjects[rule] = mine
        if mine:
            read |= _walk(st, mine, spec, back=False, links=links, types=kinds)
    return subjects, read


def _audience_keys_from_meta(st: pe._Store, held: Any, digest: str | None,
                             changed: Iterable[str]) -> tuple[frozenset[str], dict[str, Any]]:
    if not isinstance(held, dict) or held.get("digest") != digest:
        held = {"digest": digest, "keys": sorted(pe._Store.audience_keys(st))}
    keys = set(held["keys"])
    for part in _chunks(sorted(set(changed))):
        keys.update(r[0] for r in st.conn.execute(
            "SELECT DISTINCT audience_key FROM objects WHERE object_id IN "
            "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)))
    return frozenset(keys), {"digest": digest, "keys": sorted(keys)}


def _audience_keys(st: pe._Store, ch: sqlite3.Connection, digest: str | None,
                   changed: Iterable[str]) -> frozenset[str]:
    """The store's audience keys: the generation's (read whole once per generation, kept)
    and those of the records changed since."""
    keys, held = _audience_keys_from_meta(st, _meta(ch, "audience_keys"), digest, changed)
    _set_meta(ch, "audience_keys", held)
    return keys


# --------------------------------------------------------------------------- one pass

def _rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _baseline_flags(ch: sqlite3.Connection | None, digest: str | None,
                    reader: str) -> tuple[bool, bool, bool]:
    """Return ``(keep_prints, keep_layers, digest_changed)`` against ``ch``'s meta."""
    prev_digest = _meta(ch, "digest")
    prev_format = _meta(ch, "print_format")
    prev_reader = _meta(ch, "reader")
    same_digest = prev_digest == digest
    same_format = prev_format == PRINT_FORMAT
    same_reader = prev_reader == reader
    return same_digest and same_format and same_reader, same_digest and same_reader, not same_digest


def _kept_prints(ch: sqlite3.Connection | None, fallback_ch: sqlite3.Connection | None,
                 ids: Iterable[str]) -> dict[str, str]:
    kept: dict[str, str] = {}
    missing: list[str] = []
    for part in _chunks(sorted(ids)):
        part_list = list(part)
        if ch is not None:
            found = {r[0]: r[1] for r in ch.execute(
                "SELECT object_id, print FROM prints WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(part_list),))}
            kept.update(found)
            missing.extend(oid for oid in part_list if oid not in found)
        else:
            missing.extend(part_list)
    if fallback_ch is not None and missing:
        for part in _chunks(missing):
            kept.update({r[0]: r[1] for r in fallback_ch.execute(
                "SELECT object_id, print FROM prints WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(list(part)),))})
    return kept


def _kept_neighbors(ch: sqlite3.Connection | None, fallback_ch: sqlite3.Connection | None,
                    moved: set[str], before: dict[str, set[str]]) -> None:
    missing: list[str] = []
    for part in _chunks(sorted(moved)):
        part_list = list(part)
        seen_here: set[str] = set()
        if ch is not None:
            for r in ch.execute("SELECT object_id, neighbors FROM prints WHERE "
                                "object_id IN (SELECT value FROM json_each(?))",
                                (json.dumps(part_list),)):
                before.setdefault(r[0], set()).update(json.loads(r[1]))
                seen_here.add(r[0])
        missing.extend(oid for oid in part_list if oid not in seen_here)
    if fallback_ch is not None and missing:
        for part in _chunks(missing):
            for r in fallback_ch.execute("SELECT object_id, neighbors FROM prints WHERE "
                                         "object_id IN (SELECT value FROM json_each(?))",
                                         (json.dumps(list(part)),)):
                before.setdefault(r[0], set()).update(json.loads(r[1]))


def _feed_payload(feed: Any) -> dict[str, Any] | None:
    if feed is None:
        return None
    return {
        "consumer": feed.consumer,
        "state": feed.state,
        "start": feed.start,
        "end": feed.end,
        "head": feed.head,
        "overflow": feed.overflow,
        "summary": feed.summary(),
    }


def _feed_from_payload(data: Mapping[str, Any] | None) -> Any:
    if data is None:
        return None
    from operating.pg import change_feed
    return change_feed.Batch(
        consumer=str(data["consumer"]),
        state=str(data["state"]),
        start=data.get("start"),
        end=int(data["end"]),
        overflow=bool(data.get("overflow", False)),
        head=data.get("head"),
    )


def _write_json_atomic(folder: Path, prefix: str, count: int,
                       payload: Mapping[str, Any]) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}-{time.time_ns()}-{os.getpid()}-{count}.json"
    tmp = folder / ("." + name)
    tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")),
                   encoding="utf-8")
    tmp.replace(folder / name)
    return name


def _apply_changes_state(ch: sqlite3.Connection, state: Mapping[str, Any],
                         summary: Mapping[str, Any]) -> None:
    """Apply fingerprints, taken layers, day/audience watermarks and pass summary to
    ``changes.sqlite`` in one ``BEGIN IMMEDIATE`` transaction; watermarks never regress."""
    digest = state["generation"]
    reader = state["reader"]
    moment_iso = state["moment"]
    as_of_iso = state["as_of"]
    ch.execute("BEGIN IMMEDIATE")
    try:
        keep_prints, keep_layers, _ = _baseline_flags(ch, digest, reader)
        if not keep_layers:
            ch.execute("DELETE FROM layers")
        if not keep_prints:
            ch.execute("DELETE FROM prints")
        _set_meta(ch, "digest", digest)
        _set_meta(ch, "print_format", PRINT_FORMAT)
        _set_meta(ch, "reader", reader)
        ch.executemany("INSERT OR REPLACE INTO prints VALUES (?, ?, ?)", [
            (oid, fp, json.dumps(n)) for oid, (fp, n) in state["prints"].items()])
        ch.executemany("INSERT OR IGNORE INTO layers VALUES (?, ?, ?)", [
            (layer, digest, moment_iso) for layer in state["taken"]])
        held_aud = state.get("audience_keys_held")
        if isinstance(held_aud, dict):
            existing_aud = _meta(ch, "audience_keys")
            if isinstance(existing_aud, dict) and generation_digest(
                    existing_aud.get("digest")) == generation_digest(held_aud.get("digest")):
                merged_keys = sorted(set(existing_aud.get("keys") or ())
                                     | set(held_aud.get("keys") or ()))
                _set_meta(ch, "audience_keys", {"digest": held_aud.get("digest"),
                                                "keys": merged_keys})
            else:
                _set_meta(ch, "audience_keys", held_aud)
        if state.get("day_completed"):
            prev_day = _meta(ch, "day")
            if prev_day is None or as_of_iso > prev_day:
                _set_meta(ch, "day", as_of_iso)
        ch.execute("INSERT INTO passes (at, summary) VALUES (?, ?)",
                   (moment_iso, json.dumps(summary, sort_keys=True)))
        ch.execute("DELETE FROM passes WHERE pass_id NOT IN (SELECT pass_id FROM passes "
                   "ORDER BY pass_id DESC LIMIT ?)", (KEEP_PASSES,))
        ch.execute("COMMIT")
    except BaseException:
        ch.execute("ROLLBACK")
        raise


def _compute_core(store: Path, inbox: Path, ch: sqlite3.Connection | None, *,
                  fallback_ch: sqlite3.Connection | None = None,
                  pointer_file: Path | None = None,
                  owned_cache: Mapping[str, Sequence[str]] | None = None,
                  as_of: date | None = None,
                  clock: Callable[[], datetime],
                  changed: Iterable[str] | None = None,
                  base: Any = None, people: Any = None,
                  read_cap: int = READ_CAP,
                  holds_layers: bool | None = None,
                  require_writable_feed: bool = True,
                  run_watch: bool = True,
                  fold_export: Callable[[Mapping[str, Any]], None] | None = None,
                  write_inbox: bool = True) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """Plan and run one incremental pass; returns ``(summary, state_payload, feed_batch)``."""
    started = time.monotonic()
    moment = clock()
    day = po_status.business_today(moment) if as_of is None else as_of
    out: dict[str, Any] = {"as_of": day.isoformat()}
    behind = pe.rules_behind(store, inbox) & set(STORE_ONLY)
    st = pe._Store(store)
    try:
        stamp = st.meta("content_digest")
        digest = generation_digest(stamp)
        taken: list[str] = []
        waiting = False
        feed, whole_feed = None, False
        applied = _applied(store) if holds_layers is None else None
        reader = "postgres" if applied is not None else (
            "store_with_layers" if holds_layers else "generation_only")
        keep_prints, keep_layers, _ = _baseline_flags(ch, digest, reader)
        fb = None
        if fallback_ch is not None and _baseline_flags(fallback_ch, digest, reader)[0]:
            fb = fallback_ch
        seen_layers: set[str] = set()
        if keep_layers and ch is not None:
            seen_layers = {row[0] for row in ch.execute(
                "SELECT layer FROM layers WHERE base_digest = ?", (digest,))}
        out["reader"] = reader
        flood: set[str] | None = None
        kinds = read_kinds()
        if changed is None and reader == "generation_only":
            owned, taken, waiting = set(), [], bool(_pointer(store, pointer_file=pointer_file)[1])
            prints, moved = {}, set()
        elif changed is None:
            feed = _feed(digest, applied, require_writable=require_writable_feed)
            if feed is not None and feed.ready:
                owned, taken, waiting = feed_owned(
                    store, digest, ch, applied, feed,
                    seen_layers=seen_layers, pointer_file=pointer_file)
                whole_feed = bool(feed.migrated) or feed.overflow
            else:
                owned, taken, waiting = layer_owned(
                    store, digest, ch, applied,
                    reread=feed is not None and feed.state == "gap",
                    seen_layers=seen_layers, pointer_file=pointer_file,
                    owned_cache=owned_cache)
        if changed is None and reader != "generation_only" and (
                len(owned) > read_cap or whole_feed):
            prints, moved, flood = {}, set(), owned
            out["flooded"] = len(owned)
        elif changed is None and reader != "generation_only":
            flood = None
            prints = fingerprints(st, owned, kinds)
            kept = _kept_prints(ch if keep_prints else None, fb, prints)
            moved = {oid for oid, (fp, _n) in prints.items() if kept.get(oid) != fp}
        elif changed is not None:
            moved = set(changed)
            prints = fingerprints(st, moved, kinds)
        out["candidates"] = len(prints)
        out["changed"] = len(moved)
        before: dict[str, set[str]] = _base_neighbors(
            Path(base) if base is not None else store, sorted(moved),
            postgres=reader == "postgres", types=kinds)
        if keep_prints or fb is not None:
            _kept_neighbors(ch if keep_prints else None, fb, moved, before)
        last_day = _meta(ch, "day") or _last_run_day(inbox)
        new_day = last_day is None or day.isoformat() > last_day
        whole = sorted(set(WHOLE) | (set(DATED) if new_day else set()) | behind)
        rules = [r for r in STORE_ONLY if r not in whole and r in INPUTS
                 and r not in NOT_INCREMENTAL]
        subjects, read = (plan(st, moved, before, inbox, rules, kinds) if moved
                          else ({}, set()))
        if len(read) > read_cap or flood is not None:
            whole = sorted(set(whole) | set(rules))
            subjects, read = {}, set()
        if subjects:
            held_aud = _meta(ch, "audience_keys")
            if not isinstance(held_aud, dict) and fb is not None:
                held_aud = _meta(fb, "audience_keys")
            keys, aud_meta = _audience_keys_from_meta(st, held_aud, stamp, moved)
        else:
            keys, aud_meta = None, None
        watch_moved = moved if flood is None else flood
        if run_watch:
            out["watchlists"] = _watch(st, store, watch_moved, clock)
        owned = flood = set()   # not held through the whole rules' run
    finally:
        st.close()
    out["planned_seconds"] = round(time.monotonic() - started, 2)
    ran: dict[str, Any] = {}
    busy = {rule: s for rule, s in subjects.items() if s}
    if busy:
        found = pe.run(store, inbox, day, clock=clock, people=people,
                       scope=proactive_engine_inbox.Scope(busy, frozenset(read), keys),
                       locked=True, fold_export=fold_export, write_inbox=write_inbox)
        ran["scoped"] = {"subjects": {r: len(s) for r, s in sorted(busy.items())},
                         "read": len(read), "gaps_found": found.get("gaps_found"),
                         "seconds": round(time.monotonic() - started, 2)}
        # a rule whose population moved is judged whole (below)
        whole = sorted(set(whole) | set(found.get("population_moved") or ()))
        if found.get("populations_moved"):
            ran["populations_moved"] = found["populations_moved"]
    whole = [r for r in STORE_ONLY if r in whole]
    if whole:
        ran["whole"] = _judge_whole(store, inbox, day, whole, clock=clock,
                                    people=people, fold_export=fold_export,
                                    write_inbox=write_inbox)
    day_completed = bool(new_day and set(DATED) & set(STORE_ONLY) <= set(whole))
    out.update(state="ran", waiting_for_postgres=waiting, **ran,
               seconds=round(time.monotonic() - started, 2), max_rss_mb=_rss_mb())
    state_payload = {
        "inc_transport": 1,
        "generation": digest,
        "stamp": stamp,
        "reader": reader,
        "print_format": PRINT_FORMAT,
        "as_of": day.isoformat(),
        "moment": moment.isoformat(),
        "prints": {oid: [fp, neighbors] for oid, (fp, neighbors) in prints.items()},
        "taken": taken,
        "waiting_for_postgres": waiting,
        "watch_moved": sorted(watch_moved),
        "audience_keys_held": aud_meta,
        "day_completed": day_completed,
        "feed": _feed_payload(feed),
        "summary": out,
    }
    return out, state_payload, feed


def run_pass(store_path: Any, *, as_of: date | None = None,
             clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
             changed: Iterable[str] | None = None, base: Any = None,
             people: Any = None, wait: bool | float = LOCK_WAIT_SECONDS,
             read_cap: int = READ_CAP, holds_layers: bool | None = None) -> dict[str, Any]:
    """One incremental pass over the store served at ``store_path`` into its inbox; counts
    and enums only. ``changed``: the records that changed since the last pass (a test or a
    replay; default the delta layers', ``layer_owned``, fingerprinted); ``base``: the
    generation file whose links the changed records had before (default ``store_path``).
    ``wait``: how long to wait for a running engine pass before returning ``busy`` (True:
    until it ends; False: not at all).
    ``holds_layers``: whether the store read holds the delta layers (None: when the engine
    reads Postgres, its applied ones); a store that does not (the SQLite generation) takes
    no layer in, so none is marked seen before a store holding it is read."""
    _declared()
    store = Path(store_path)
    inbox = pe.inbox_path(store)
    moment = clock()
    day = po_status.business_today(moment) if as_of is None else as_of
    if not inbox.is_file():
        return {"as_of": day.isoformat(), "state": "no_inbox"}
    with pe.pass_lock(inbox, wait=wait) as held:
        if not held:
            return {"as_of": day.isoformat(), "state": "busy"}
        with closing(_open(changes_path(inbox))) as ch:
            out, state_payload, feed = _compute_core(
                store, inbox, ch, as_of=day, clock=lambda: moment, changed=changed,
                base=base, people=people, read_cap=read_cap, holds_layers=holds_layers,
                require_writable_feed=True, run_watch=True, write_inbox=True)
            _apply_changes_state(ch, state_payload, out)
            if feed is not None:
                # what was judged is kept: the feed's cursor moves past it (a failed write
                # reads the same rows again; their prints say they are unchanged)
                from operating.pg import change_feed, ontology_reader
                out["feed"] = {**feed.summary(),
                               "committed": change_feed.commit_to(ontology_reader.dsn(), feed)}
    return out


def compute(store_path: Any, export_dir: Any, *, as_of: date | None = None,
            clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
            changed: Iterable[str] | None = None, base: Any = None,
            people: Any = None, read_cap: int = READ_CAP,
            holds_layers: bool | None = None,
            context_dir: Any = None) -> dict[str, Any]:
    """Read-only Spark compute half of the incremental pass.

    Reads the store (against the Spark's hot standby when ``PLX_ONTOLOGY_PG=only``),
    the inbox and ``changes.sqlite`` read-only (or a compact shipped ``context_dir``),
    and writes fold payloads (``fold-*.json``) and an incremental state payload
    (``inc-*.json``) to ``export_dir``. Makes zero writes to the box's inbox,
    ``changes.sqlite``, ``watchlists.sqlite``, or Postgres ``change_cursors``.
    """
    _declared()
    store = Path(store_path)
    export = Path(export_dir)
    moment = clock()
    day = po_status.business_today(moment) if as_of is None else as_of
    ctx = Path(context_dir) if context_dir is not None else None
    inbox = (ctx / "inbox.sqlite") if ctx is not None else pe.inbox_path(store)
    if not inbox.is_file():
        return {"as_of": day.isoformat(), "state": "no_inbox"}
    ch_path = (ctx / "changes.sqlite") if ctx is not None else changes_path(inbox)
    fallback_path = changes_path(pe.inbox_path(store)) if ctx is not None else None
    pointer_file = (ctx / "delta-current.json") if ctx is not None and (
        ctx / "delta-current.json").is_file() else None
    owned_cache: dict[str, list[str]] | None = None
    if ctx is not None and (ctx / "layer-owned.json.gz").is_file():
        with gzip.open(ctx / "layer-owned.json.gz", "rt", encoding="utf-8") as fh:
            owned_cache = json.load(fh)
    ch = _open_ro(ch_path)
    fb = _open_ro(fallback_path) if fallback_path is not None else None
    fold_count = [0]
    fold_files: list[str] = []

    def export_fold(payload: Mapping[str, Any]) -> None:
        fold_count[0] += 1
        fold_files.append(_write_json_atomic(export, "fold", fold_count[0], payload))

    try:
        out, state_payload, _feed = _compute_core(
            store, inbox, ch, fallback_ch=fb, pointer_file=pointer_file,
            owned_cache=owned_cache, as_of=day, clock=lambda: moment,
            changed=changed, base=base, people=people, read_cap=read_cap,
            holds_layers=holds_layers, require_writable_feed=False,
            run_watch=False, fold_export=export_fold, write_inbox=False)
    finally:
        if ch is not None:
            ch.close()
        if fb is not None:
            fb.close()
    state_payload["fold_files"] = fold_files
    _write_json_atomic(export, "inc", 1, state_payload)
    return out


def prepare(state_dir: Any, out_dir: Any, *,
            wait: bool | float = LOCK_WAIT_SECONDS) -> dict[str, Any]:
    """Prepare a compact read-only context snapshot in ``out_dir`` on the box for Spark
    ``compute``: open-gap inbox subset, ``changes.sqlite`` metadata/layers and candidate
    ``prints``, ``delta/CURRENT.json``, and any un-taken ``delta_owned`` ids."""
    from . import delta_overlay
    from .host import projection_path

    store = projection_path(Path(state_dir))
    inbox = pe.inbox_path(store)
    if not inbox.is_file():
        return {"state": "no_inbox"}
    out = Path(out_dir)
    with pe.pass_lock(inbox, wait=wait) as held:
        if not held:
            return {"state": "busy"}
        out.mkdir(parents=True, exist_ok=True)
        os.chmod(out, 0o700)
        dst_inbox = out / "inbox.sqlite"
        with closing(sqlite3.connect(f"file:{inbox.resolve()}?mode=ro", uri=True,
                                     timeout=30)) as src, \
                closing(pe._open_inbox(dst_inbox)) as dst:
            dst.execute("BEGIN IMMEDIATE")
            for table in ("meta", "detector_runs"):
                rows = src.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    marks = ",".join("?" for _ in rows[0])
                    dst.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({marks})",
                                    [tuple(r) for r in rows])
            last_run = src.execute(
                "SELECT run_id, run_at, as_of, store_digest, summary FROM runs "
                "ORDER BY run_id DESC LIMIT 1").fetchall()
            if last_run:
                dst.executemany("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?)",
                                [tuple(r) for r in last_run])
            open_gaps = src.execute("SELECT * FROM gaps WHERE state = 'open'").fetchall()
            if open_gaps:
                marks = ",".join("?" for _ in open_gaps[0])
                dst.executemany(f"INSERT OR REPLACE INTO gaps VALUES ({marks})",
                                [tuple(r) for r in open_gaps])
            cites = src.execute(
                "SELECT c.* FROM gap_cites c JOIN gaps g ON g.gap_id = c.gap_id "
                "WHERE g.state = 'open'").fetchall()
            if cites:
                marks = ",".join("?" for _ in cites[0])
                dst.executemany(f"INSERT OR REPLACE INTO gap_cites VALUES ({marks})",
                                [tuple(r) for r in cites])
            dst.execute("COMMIT")
            dst.execute("PRAGMA journal_mode = DELETE")
        os.chmod(dst_inbox, 0o600)

        candidate_ids, feed_ok = _prepare_feed_candidates(store)
        ch_src = changes_path(inbox)
        digest_meta = None
        seen_layers: set[str] = set()
        if ch_src.is_file():
            dst_ch = out / "changes.sqlite"
            with closing(_open_ro(ch_src)) as src, closing(_open(dst_ch)) as dst:
                assert src is not None
                dst.execute("BEGIN IMMEDIATE")
                for row in src.execute("SELECT key, value FROM meta"):
                    dst.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", tuple(row))
                    if row[0] == "digest":
                        digest_meta = json.loads(row[1])
                layer_rows = src.execute(
                    "SELECT layer, base_digest, seen_at FROM layers").fetchall()
                if layer_rows:
                    dst.executemany("INSERT OR IGNORE INTO layers VALUES (?, ?, ?)",
                                    [tuple(r) for r in layer_rows])
                    seen_layers = {r[0] for r in layer_rows if r[1] == digest_meta}
                for part in _chunks(sorted(candidate_ids)):
                    rows = src.execute(
                        "SELECT object_id, print, neighbors FROM prints WHERE object_id IN "
                        "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)
                    ).fetchall()
                    if rows:
                        dst.executemany("INSERT OR REPLACE INTO prints VALUES (?, ?, ?)",
                                        [tuple(r) for r in rows])
                dst.execute("COMMIT")
                dst.execute("PRAGMA journal_mode = DELETE")
            os.chmod(dst_ch, 0o600)

        pointer = delta_overlay.pointer_path(store)
        has_pointer = pointer.is_file()
        if has_pointer:
            dst_ptr = out / "delta-current.json"
            dst_ptr.write_bytes(pointer.read_bytes())
            os.chmod(dst_ptr, 0o600)
        has_layer_owned = False
        base_digest, layers = _pointer(store)
        if layers and (not feed_ok or any(l not in seen_layers for l in layers)):
            unseen = [l for l in layers if base_digest != digest_meta or l not in seen_layers]
            if unseen and (not feed_ok or len(unseen) <= 8):
                owned_map: dict[str, list[str]] = {}
                for layer in unseen:
                    try:
                        with closing(sqlite3.connect(f"file:{layer}?mode=ro", uri=True)) as lc:
                            owned_map[layer] = [
                                r[0] for r in lc.execute("SELECT object_id FROM delta_owned")]
                    except sqlite3.Error:
                        continue
                if owned_map:
                    gz_path = out / "layer-owned.json.gz"
                    with gzip.open(gz_path, "wt", encoding="utf-8") as fh:
                        json.dump(owned_map, fh, separators=(",", ":"))
                    os.chmod(gz_path, 0o600)
                    has_layer_owned = True
                    if not feed_ok and ch_src.is_file():
                        extra = {oid for oids in owned_map.values() for oid in oids}
                        if len(extra) <= READ_CAP:
                            with closing(_open_ro(ch_src)) as src, \
                                    closing(_open(out / "changes.sqlite")) as dst:
                                assert src is not None
                                dst.execute("BEGIN IMMEDIATE")
                                for part in _chunks(sorted(extra - candidate_ids)):
                                    rows = src.execute(
                                        "SELECT object_id, print, neighbors FROM prints "
                                        "WHERE object_id IN (SELECT value FROM json_each(?))",
                                        (json.dumps(list(part)),)).fetchall()
                                    if rows:
                                        dst.executemany(
                                            "INSERT OR REPLACE INTO prints VALUES (?, ?, ?)",
                                            [tuple(r) for r in rows])
                                dst.execute("COMMIT")
                                dst.execute("PRAGMA journal_mode = DELETE")
        ctx_path = out / "context.json"
        ctx_path.write_text(json.dumps({
            "state": "prepared",
            "store": str(store),
            "has_changes": ch_src.is_file(),
            "has_pointer": has_pointer,
            "has_layer_owned": has_layer_owned,
        }, sort_keys=True), encoding="utf-8")
        os.chmod(ctx_path, 0o600)
    return {"state": "prepared", "out": str(out)}


def _prepare_feed_candidates(store: Path) -> tuple[set[str], bool]:
    """Candidate ``object_id``s past ``proactive_incremental``'s Postgres cursor for
    ``prepare``; returns ``(ids, feed_ok)``. Never raises if Postgres is unreachable."""
    try:
        from operating.pg import change_feed, ontology_reader
        if not ontology_reader.serves(store):
            return set(), False
        import psycopg
        with psycopg.connect(ontology_reader.dsn(), connect_timeout=10) as conn:
            if not change_feed.present(conn):
                return set(), False
            pos = change_feed.position(conn, FEED_CONSUMER)
            pruned = conn.execute(
                f"SELECT coalesce((SELECT position FROM {change_feed.CURSORS} "
                "WHERE consumer = %s), 0)", (change_feed.PRUNED,)).fetchone()[0]
            if pos is None or pos < pruned:
                return set(), False
            rows = conn.execute(
                f"SELECT DISTINCT object_id FROM {change_feed.FEED} "
                "WHERE seq > %s AND object_id <> '*' LIMIT %s",
                (pos, READ_CAP + 1)).fetchall()
            if len(rows) > READ_CAP:
                return set(), True
            return {r[0] for r in rows}, True
    except Exception as exc:  # noqa: BLE001 - logged; prepare still copies metadata/layers
        print(f"proactive_incremental prepare: feed check failed ({type(exc).__name__})",
              file=sys.stderr)
        return set(), False


def _validate_fold_file(path: Path, generation: str | None) -> tuple[str, dict[str, Any] | None]:
    """Read and validate one exported ``fold-*.json`` file: ``('ok'|'stale'|'bad', payload)``."""
    _declared()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        results = payload.get("results")
        gaps = payload.get("gaps")
        populations = payload.get("populations")
        if (payload.get("fold_transport") != 1
                or not isinstance(results, dict)
                or not set(results) <= set(STORE_ONLY)
                or not isinstance(gaps, dict)
                or not isinstance(populations, dict)):
            return "bad", None
    except (OSError, ValueError, TypeError, KeyError):
        return "bad", None
    if generation is None or generation_digest(payload.get("digest")) != generation:
        return "stale", None
    return "ok", payload


def apply_fold_payloads(store_path: Any, export_dir: Any, *,
                        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                        people: Any = None,
                        wait: bool | float = LOCK_WAIT_SECONDS) -> dict[str, Any]:
    """Apply exported incremental fold and state payloads to the live inbox and
    ``changes.sqlite``, oldest first.

    Each fold payload runs through ``proactive_engine.write_fold_payload`` under the
    inbox's ``pass_lock``; state watermarks (``prints``, ``layers``, ``day``,
    ``change_feed`` cursor) advance ONLY to what compute saw and never regress. Stale or
    bad files stay in ``export_dir`` for inspection.
    """
    _declared()
    store = Path(store_path)
    inbox = pe.inbox_path(store)
    export = Path(export_dir)
    fold_files = sorted(export.glob("fold-*.json"))
    inc_files = sorted(export.glob("inc-*.json"))
    total = len(fold_files) + len(inc_files)
    out: dict[str, Any] = {"files": total, "applied": 0, "stale": 0, "bad": 0, "left": 0}
    if not total:
        return out
    if not inbox.is_file():
        return {**out, "stale": total, "left": total, "state": "no_inbox"}
    with pe.pass_lock(inbox, wait=wait) as held:
        if not held:
            return {**out, "state": "busy", "skipped": "pass_running", "left": total}
        generation = pe.served_generation(store, inbox)
        handled_folds: set[Path] = set()
        for inc_path in inc_files:
            try:
                inc = json.loads(inc_path.read_text(encoding="utf-8"))
                if (inc.get("inc_transport") != 1
                        or not isinstance(inc.get("prints"), dict)
                        or not isinstance(inc.get("taken"), list)
                        or not isinstance(inc.get("summary"), dict)):
                    raise ValueError("not_an_incremental_payload")
                fold_names = list(inc.get("fold_files") or ())
            except (OSError, ValueError, TypeError, KeyError):
                out["bad"] += 1
                continue
            if generation is None or inc.get("generation") != generation:
                out["stale"] += 1
                for fname in fold_names:
                    fpath = export / fname
                    if fpath.is_file() and fpath not in handled_folds:
                        handled_folds.add(fpath)
                        status, _ = _validate_fold_file(fpath, generation)
                        out["stale" if status == "stale" else "bad"] += 1
                continue
            validated_folds: list[tuple[Path, dict[str, Any]]] = []
            fold_issue: str | None = None
            for fname in fold_names:
                fpath = export / fname
                handled_folds.add(fpath)
                status, payload = _validate_fold_file(fpath, generation)
                if status != "ok" or payload is None:
                    out[status] += 1
                    fold_issue = status
                else:
                    validated_folds.append((fpath, payload))
            if fold_issue is not None:
                # do not partially apply folds or advance watermarks if any fold failed
                for fpath, _ in validated_folds:
                    out["stale" if fold_issue == "stale" else "bad"] += 1
                out[fold_issue] += 1
                continue
            try:
                for _fpath, payload in validated_folds:
                    pe.write_fold_payload(inbox, payload)
                watch_out = _apply_watchlists(store, set(inc.get("watch_moved") or ()), clock)
                summary = {**inc["summary"], "watchlists": watch_out}
                with closing(_open(changes_path(inbox))) as ch:
                    _apply_changes_state(ch, inc, summary)
                feed_batch = _feed_from_payload(inc.get("feed"))
                if feed_batch is not None:
                    from operating.pg import change_feed, ontology_reader
                    summary["feed"] = {
                        **feed_batch.summary(),
                        "committed": change_feed.commit_to(ontology_reader.dsn(), feed_batch),
                    }
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                out["bad"] += 1 + len(validated_folds)
                continue
            for fpath, _ in validated_folds:
                fpath.unlink()
                out["applied"] += 1
            inc_path.unlink()
            out["applied"] += 1
        for fpath in fold_files:
            if fpath in handled_folds or not fpath.is_file():
                continue
            status, payload = _validate_fold_file(fpath, generation)
            if status != "ok" or payload is None:
                out[status] += 1
                continue
            try:
                pe.write_fold_payload(inbox, payload)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                out["bad"] += 1
                continue
            fpath.unlink()
            out["applied"] += 1
    out["left"] = len(list(export.glob("fold-*.json"))) + len(list(export.glob("inc-*.json")))
    return out


def _apply_watchlists(store: Path, moved: set[str],
                      clock: Callable[[], datetime]) -> dict[str, Any]:
    from . import watchlists
    wpath = watchlists.watch_path(store)
    if not wpath.is_file():
        return {"follows": 0, "rules": 0, "alerts": 0, "state": "none"}
    st = pe._Store(store)
    try:
        return _watch(st, store, moved, clock)
    finally:
        st.close()


def _judge_whole(store: Path, inbox: Path, day: date, whole: Sequence[str], *,
                 clock: Callable[[], datetime], people: Any,
                 fold_export: Callable[[Mapping[str, Any]], None] | None = None,
                 write_inbox: bool = True) -> dict[str, Any]:
    """The rules judged whole, under the pass's lock, in the full pass's rule groups
    (``proactive_engine.rule_groups``): each group on a store of its own, so what one
    group's rules read (populations, mail, documents) is let go before the next starts;
    the link rows are read once and shared (``PassCache``). One run of every rule held
    all their reads at once (11-17: 11.7 GB for the rules a code change put behind)."""
    shared = pe.PassCache()
    out: dict[str, Any] = {"rules": list(whole), "gaps_found": 0, "detectors": {},
                           "groups": 0}
    try:
        for group in pe.rule_groups(whole, threads=pe.detector_threads()):
            found = pe.run(store, inbox, day, clock=clock, only=group, people=people,
                           threads=None, locked=True, pass_cache=shared,
                           fold_export=fold_export, write_inbox=write_inbox)
            out["groups"] += 1
            out["gaps_found"] += int(found.get("gaps_found") or 0)
            out["detectors"].update(found.get("detectors") or {})
    finally:
        shared.clear()
    return out


def _watch(st: pe._Store, store: Path, moved: set[str],
           clock: Callable[[], datetime]) -> dict[str, Any]:
    """The people's follows and alert rules judged over what moved
    (``operating.watchlists``): counts only; a failure is reported and leaves the pass's
    own work untouched (no alert is written, the snapshots stay as they were)."""
    from . import watchlists

    try:
        return watchlists.evaluate(st, watchlists.watch_path(store), moved, clock=clock)
    except Exception as error:  # noqa: BLE001 - the watchlists never stop the pass
        return {"state": "failed", "error": type(error).__name__}


def _last_run_day(inbox: Path) -> str | None:
    """The day the inbox's last full run judged on."""
    with closing(sqlite3.connect(f"file:{inbox.resolve()}?mode=ro", uri=True)) as conn:
        try:
            row = conn.execute("SELECT as_of FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            return None
    return row[0] if row else None


def main(argv: Sequence[str] | None = None) -> int:
    from .host import projection_path

    parser = argparse.ArgumentParser(prog="python3 -m operating.proactive_incremental",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--wait", action="store_true",
                        help="wait for a running engine pass however long it takes")
    parser.add_argument("--wait-seconds", type=float, default=LOCK_WAIT_SECONDS,
                        help="wait this long for a running engine pass before skipping "
                             f"(default {LOCK_WAIT_SECONDS:.0f})")
    parser.add_argument("--prepare-dir", type=Path, default=None,
                        help="prepare a compact context snapshot here on the box for Spark compute")
    parser.add_argument("--export-dir", type=Path, default=None,
                        help="run read-only compute and write fold/delta JSON payloads here")
    parser.add_argument("--context-dir", type=Path, default=None,
                        help="read prepared inbox/changes/pointer snapshot from here (--export-dir)")
    parser.add_argument("--apply-folds", type=Path, default=None,
                        help="apply exported fold/delta payloads from this folder to the live inbox")
    args = parser.parse_args(argv)
    if args.wait_seconds < 0:
        parser.error("--wait-seconds must not be negative")
    modes = sum(x is not None for x in (args.prepare_dir, args.export_dir, args.apply_folds))
    if modes > 1:
        parser.error("--prepare-dir, --export-dir and --apply-folds are mutually exclusive")
    if args.context_dir is not None and args.export_dir is None:
        parser.error("--context-dir requires --export-dir")
    wait = True if args.wait else args.wait_seconds
    try:   # a broken window lock never stops the job
        from ontology import window_lock
        held = window_lock.skip_report("proactive", state_dir=args.state_dir)
    except Exception as exc:  # noqa: BLE001 - logged, the job runs
        print(f"window check failed, running anyway: {exc}", file=sys.stderr)
        held = None
    if held:
        print(json.dumps(held, indent=1, sort_keys=True))
        return 0
    if args.prepare_dir is not None:
        print(json.dumps(prepare(args.state_dir, args.prepare_dir, wait=wait),
                         indent=1, sort_keys=True))
        return 0
    if args.export_dir is not None:
        print(json.dumps(compute(projection_path(args.state_dir), args.export_dir,
                                 as_of=args.as_of, context_dir=args.context_dir),
                         indent=1, sort_keys=True))
        return 0
    if args.apply_folds is not None:
        applied = apply_fold_payloads(projection_path(args.state_dir), args.apply_folds,
                                      wait=wait)
        print(json.dumps(applied, indent=1, sort_keys=True))
        failed_only = applied["applied"] == 0 and applied["stale"] + applied["bad"] > 0
        return 1 if failed_only else 0
    print(json.dumps(run_pass(projection_path(args.state_dir), as_of=args.as_of,
                              wait=wait),
                     indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
