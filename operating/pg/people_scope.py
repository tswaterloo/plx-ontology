"""Person links (``ontology.people_links``, link ids ``person:``) kept right over Postgres
after delta layers: a whole-store step with a run and a mark of its own
(``operating.pg.whole_steps.OWN_RUNS``).

**Why** (``/data/scratch/person-links-1118.md``): a person link is
judged against the whole people directory (an address names a Person only when exactly one
Person holds it), but a delta layer is built from its changed records and their closure.
``ontology.people_directory`` makes an outside contact only from a *sender* address of the
messages a build sees, and ``ontology.delta.record_of_handle`` brings no Person into a
closure, so a layer holds the staff Persons and the outside contacts who sent mail in it,
nothing else. Its ``with_people`` therefore writes no link from a re-projected mail to any
other outside recipient (or outside sender), and the apply replaced every link of the mail
by the layer's: each re-projection dropped them. Measured 11-18: 82% of the outside
recipients of new mail unlinked (staff all linked); a whole-store run of the same rule
restored 158,869 ``Email.addressed_to_person`` and 3,514 ``Email.sent_by_person`` links.

**What changes**: a layer apply leaves every ``person:`` link alone (neither deletes nor
copies it: ``whole_steps.ranges``), and after the layers of a cycle :func:`recompute`
judges the person links of every object of the step's input types a layer replaced since
its mark again, by the step's own code (``read_people``, ``judge_values``, ``link_row``,
``alias_pairs``, ``alias_row``: unchanged, so the step digest does not move) over the whole
served people directory, reading only those objects' values (by index,
:class:`ObjectsConnection`):

* the mark (``store_meta`` :data:`STATE`, ``{"through": seq}``): the newest
  ``ontology.applied_owned`` row it covered. The first layer applied by this code sets it
  just before its own rows when there is none (:func:`start_mark`, in the layer's
  transaction), so no layer that left its person links to this run is missed; after a load
  (whose ``with_people`` ran whole) there is none until a layer comes;
* one transaction under the delta's lock (``ontology_migrate.LOCK``), the objects in chunks
  of :data:`CHUNK` (bounded memory), at most :data:`RUN_MAX` objects a run
  (``PLX_PERSON_LINKS_RUN_MAX``; a catch-up goes on over the next cycles, each holding the
  lock a bounded time; ``remaining`` says so); only links that changed are written (by id: gone, new,
  other content; ``whole_steps.diff``), never a ``contradicted`` row; their from-ends
  recorded as a layer of their own (``whole-steps-people-<mark>-<ms>``) so the traverse
  facts fold it, and the kept link slice folded in the same transaction
  (``link_marks.layer_start`` / ``fold_layer``) so a cycle never un-vouches it;
* a Person a layer replaced (none has so far: no layer owns one) re-judges every object
  linked to it or to a Person sharing one of its keys, and the alias links
  (``Person.same_person_as``); an object naming one of its *new* keys that no link names
  yet is found only by :func:`backfill` (counted ``person_keys_unscoped``).

**Same rows as the step**: a pair two values name (To and Cc) is one link, the one of the
property the step declares first (``people_links.FAMILIES`` order; the step keeps the one
its read meets first, which Postgres does not order), and a person's rejection
(``contradicted`` row of the pair) vetoes it. The mention collapse
(``operating.pg.mention_collapse``) folds only rows of one key (type, ends, version) and
one tier, so the (to, type) pairs of a mail's contradicted rows read here are the
uncollapsed store's.

**Excluded people never linked, fail closed**: the people exclusion must load (else the run
is refused and changes nothing; delta reads no mail without it either), it decides the
alias path as in the step, and a link to a Person holding an identity it decides is an
excluded person's is dropped (counted ``excluded_target``; the directory makes no such
Person, so this only guards against one appearing).

**Backfill** (:func:`backfill`, ``python3 -m operating.pg.people_scope backfill``): the
same judgement over every object of the step's source types, in key ranges side by side on
``--workers`` processes. Each range is judged off the lock over one read-only snapshot
(``--dry-run`` stops there and counts), then written under the lock in a short transaction
of its own, without the objects a layer replaced since that snapshot (the next run judges
them), its replaced and written rows and its transaction id fsynced to
``WORK/journal-<n>.jsonl`` inside it, before it writes. ``undo`` takes back the entries
whose transaction committed (a failed range's entry is skipped), refuses when a later write
moved one of their rows, and refreshes the derived link caches as a write does (a journal
of the 11-18 release, without transaction ids: committed when its range's layer is
recorded, refused unless its rows still show its write and no later run changed their
objects' links; ``--dry-run`` checks every entry and writes nothing). The app
keeps serving; the delta waits on the lock only for a range's write. Every statement is
bounded (:data:`STATEMENT_MS`, ``--statement-timeout-ms``; the incremental run's
``PLX_PERSON_LINKS_STATEMENT_MS``), the wait for the lock by :data:`LOCK_WAIT_MS`.

**Digests**: none moves. The step's code (``ontology/people_links.py``) is unchanged and
this module is in no step's, derivation's or key's code list; the links written are the
step's own rows for the served data (``store_meta`` and ``ontology.applied_owned`` are in
no content digest).

    python3 -m operating.pg.people_scope run [--dsn DSN]
    python3 -m operating.pg.people_scope backfill --work DIR (--dry-run | --apply)
        [--workers N] [--types Email,...] [--every K] [--dsn DSN]
    python3 -m operating.pg.people_scope undo --work DIR [--dry-run] [--dsn DSN]

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import resource
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology import people_links as pl
from ontology.pg_step_inplace import SplitConnection, _read_only
from operating.pg import whole_steps

STATE = "whole_steps:with_people"            # store_meta key: {"through": seq}
LAYER_PREFIX = whole_steps.LAYER_PREFIX + "people-"
CHUNK = 5000                                  # objects judged at once
PERSON_PREFIX = pl.PERSON + ":"
# every link type the step writes (its families' and the alias link)
LINK_TYPES = tuple(sorted(set(pl.LINK_TYPES) | {pl.ALIAS_LINK}))
STEP = "with_people"
# each statement's limit (ms) for the incremental run and the backfill (``0``: none); the
# wait for the delta's lock has its own, longer one (a cycle may hold it minutes)
STATEMENT_MS = 120_000
LOCK_WAIT_MS = 30 * 60_000


class Refused(RuntimeError):
    """The run would not judge what the step judges: nothing is written."""


def prefix() -> tuple[str, str]:
    """The step's link id range ``[start, end)``."""
    return pl.PREFIX, pl._PREFIX_END


# --------------------------------------------------------------------------- context

def _guard(environ: Mapping[str, str] | None = None) -> Any:
    """The people exclusion (``people_links._excluder``'s), or None when unavailable."""
    from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
    guard = PeopleExclusion.from_env(environ_with_exclusion(
        os.environ if environ is None else environ))
    return None if guard.unavailable else guard


def context(environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """What the step reads besides the store: its declared families, whether the alias
    link is declared, our own mail domains (``sender_parties.own_domains`` of a projector
    whose mail source has the delta's internal domains) and the people exclusion. None
    when the schema declares no person link; ``Refused`` without the exclusion."""
    from ontology import mentions, step_migrate
    from ontology.delta import _internal_domains
    from ontology.schema import load_schema
    from ontology.sender_parties import own_domains
    environ = os.environ if environ is None else environ
    links = load_schema().link_types
    families = pl.declared_families(links)
    if not families:
        return None
    guard = _guard(environ)
    if guard is None:
        raise Refused("exclusion_unavailable")
    mail = SimpleNamespace(internal_domains=tuple(_internal_domains(environ)))
    return {"families": families, "links": links, "guard": guard,
            "own": own_domains(SimpleNamespace(sources={"graph_mail": mail})),
            "aliased": any(b.get("kind") == mentions.BINDING_KIND for b in
                           (links.get(pl.ALIAS_LINK) or {}).get("bindings", ())),
            "inputs": tuple(sorted(step_migrate.STEPS[STEP].inputs(None))),
            "sources": tuple(sorted(families))}


# --------------------------------------------------------------------------- reads

# ``sender_parties._CELLS`` (the step's current-value read) in Postgres' shape, over the
# objects of a list by index: their versions, those versions' current sources, the cells
# of the read's fields (the step's rows, columns and filters; ``_values`` drops a None)
_CELLS_PG = (
    "SELECT v.object_id, v.l1_handle, v.markings AS vmarks, k.markings AS kmarks, "
    "coalesce(s.digest, v.digest) AS digest, coalesce(s.lineage, v.lineage) AS lineage, "
    "s.recorded_at, c.field, f.field_path, c.value AS value "
    "FROM unnest(%s::text[]) o(object_id) "
    "JOIN ontology.object_versions v ON v.object_id = o.object_id "
    "JOIN ontology.value_sources s ON s.ver = v.ver AND s.superseded_at IS NULL "
    "JOIN ontology.value_cells c ON c.src = s.src AND c.field = ANY(%s::int[]) "
    "JOIN ontology.value_kinds k ON k.kind = c.kind "
    "JOIN ontology.value_fields f ON f.field = c.field")
_CELLS_FIELDS = re.compile(r"WHERE lf\.field IN \(([0-9]+(?:,[0-9]+)*)\) AND "
                           r"s\.superseded_at IS NULL$")


class _Row:
    """A row read by name or position (``sqlite3.Row``'s face)."""

    __slots__ = ("_names", "_values")

    def __init__(self, names: Mapping[str, int], values: Sequence[Any]) -> None:
        self._names, self._values = names, tuple(values)

    def __getitem__(self, key: Any) -> Any:
        return self._values[key if isinstance(key, int) else self._names[key]]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._names)


class _Rows:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows
        self._at = 0

    def __iter__(self) -> Iterator[_Row]:
        return iter(self._rows)

    def fetchall(self) -> list[_Row]:
        out, self._at = self._rows[self._at:], len(self._rows)
        return out

    def fetchone(self) -> _Row | None:
        if self._at >= len(self._rows):
            return None
        self._at += 1
        return self._rows[self._at - 1]


def _named(cur: Any) -> list[_Row]:
    names = {d.name: i for i, d in enumerate(cur.description or ())}
    return [_Row(names, r) for r in cur.fetchall()]


def native(pg: Any) -> Any:
    """A server-binding cursor on the compat connection's own session (its transaction)."""
    import psycopg
    return psycopg.Cursor(pg._pg)


class ObjectsConnection(SplitConnection):
    """``SplitConnection`` whose current-value read (``sender_parties._values``, the only
    one the step makes) sees the values of :attr:`objects` only, read by index in this
    session's transaction. Any other cells read is refused (fail closed); other reads go
    to Postgres as they are."""

    def __init__(self, local: Any, pg: Any) -> None:
        super().__init__(local, pg)
        self.objects: list[str] = []
        self.cell_reads = 0

    def _route(self, sql: str, params: Any, row_factory: Any = None) -> Any:
        if _read_only(sql) and "value_cells" in sql:
            from ontology.sender_parties import _CELLS
            flat = " ".join(sql.split())
            found = _CELLS_FIELDS.search(flat)
            if found is None or flat != " ".join(
                    _CELLS.format(fields=found.group(1)).split()) or tuple(params or ()):
                raise Refused("people_scope_cells_read_unknown")
            cur = native(self.pg)
            cur.execute(_CELLS_PG, (list(self.objects),
                                    [int(f) for f in found.group(1).split(",")]))
            self.cell_reads += 1
            self.reads += 1
            return _Rows(_named(cur))
        return super()._route(sql, params, row_factory)


def _collate(cur: Any) -> str:
    from ontology.pg_byte_order import key_collate
    return key_collate(cur)


def persons(cur: Any) -> list[str]:
    """Every Person's object id (the directory the step judges against)."""
    c = _collate(cur)
    cur.execute(f"SELECT object_id FROM ontology.objects WHERE object_id{c} >= %s AND "
                f"object_id{c} < %s", (PERSON_PREFIX, pl.PERSON + ";"))
    return sorted(r[0] for r in cur.fetchall())


def served_links(cur: Any, objects: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The served ``person:`` links from ``objects`` (every column, ``ontology.links``
    unpacks the texts), by id."""
    from ontology import store_ddl
    start, end = prefix()
    out: dict[str, dict[str, Any]] = {}
    objects = sorted(set(objects))
    for i in range(0, len(objects), 20_000):
        cur.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM ontology.links WHERE "
                    "from_object = ANY(%s) AND link_type = ANY(%s)",
                    (objects[i:i + 20_000], list(LINK_TYPES)))
        names = [d.name for d in cur.description]
        for row in cur.fetchall():
            if start <= row[0] < end:
                out[row[0]] = dict(zip(names, row))
    return out


def served_aliases(cur: Any) -> dict[str, dict[str, Any]]:
    """The served ``person:`` alias links (``Person.same_person_as``), by id."""
    from ontology import store_ddl
    start, end = prefix()
    cur.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM ontology.links WHERE "
                "link_type = %s", (pl.ALIAS_LINK,))
    names = [d.name for d in cur.description]
    return {r[0]: dict(zip(names, r)) for r in cur.fetchall() if start <= r[0] < end}


def _replaced(objects: Iterable[str], *, aliases: bool = False
              ) -> Callable[[Mapping[str, Any]], bool]:
    """The served rows a judgement of ``objects`` (and of the alias links) stands for:
    the step's (``person:``) links from them, never a person's rejection."""
    held = frozenset(objects)
    start, end = prefix()

    def replaced(row: Mapping[str, Any]) -> bool:
        if not (start <= row["link_id"] < end) or row.get("tier") == "contradicted":
            return False
        if aliases and row["link_type"] == pl.ALIAS_LINK:
            return True
        return row["from_object"] in held and row["link_type"] != pl.ALIAS_LINK
    return replaced


# --------------------------------------------------------------------------- judging

class Directory:
    """The people directory the step judges against (``people_links.read_people`` over
    every Person), the Persons holding an identity the exclusion decides is an excluded
    person's, and the Persons' current versions (read once each)."""

    def __init__(self, conn: ObjectsConnection, ctx: Mapping[str, Any], cur: Any) -> None:
        conn.objects = persons(cur)
        self.count = len(conn.objects)
        self.people = pl.read_people(conn, ctx["own"])
        self.people.guard = ctx["guard"]
        conn.objects = []
        self.excluded = _excluded_persons(self.people, ctx["guard"])
        self._versions: dict[str, Any] = {}
        self._cur = cur

    def versions(self, wanted: Iterable[str]) -> dict[str, Any]:
        missing = sorted(set(wanted) - set(self._versions))
        for i in range(0, len(missing), 20_000):
            part = missing[i:i + 20_000]
            for p in part:
                self._versions[p] = None
            self._cur.execute("SELECT object_id, l1_handle, markings, recorded_at FROM "
                              "ontology.object_versions WHERE superseded_at IS NULL AND "
                              "object_id = ANY(%s)", (part,))
            for row in _named(self._cur):
                self._versions[row["object_id"]] = row
        return self._versions

    def keys_of(self, wanted: Iterable[str]) -> set[tuple[str, str]]:
        wanted = set(wanted)
        return {(kind, key) for kind, table in self.people.by.items()
                for key, held in table.items() if wanted & set(held)}


def _excluded_persons(people: pl.People, guard: Any) -> frozenset[str]:
    """The Persons holding an object id or address the exclusion decides is an excluded
    person's (one it decides but cannot read counts as one: fail closed)."""
    from integrations.people_exclusion import ExclusionUnavailable
    out = set()
    for kind in ("oid", "address"):
        for key, held in people.by[kind].items():
            if not guard.decides(key):
                continue
            try:
                hit = guard.is_excluded(key)
            except ExclusionUnavailable:
                hit = True
            if hit:
                out.update(held)
    return frozenset(out)


def _rejected(cur: Any, objects: Sequence[str]) -> set[tuple[str, str, str]]:
    """(from, to, link type) of every link a person rejected from ``objects``
    (``step_reads.rejected_pairs``' rows)."""
    out: set[tuple[str, str, str]] = set()
    objects = sorted(set(objects))
    for i in range(0, len(objects), 20_000):
        cur.execute("SELECT from_object, to_object, link_type FROM ontology.link_rows WHERE "
                    "tier = 'contradicted' AND from_object = ANY(%s)",
                    (objects[i:i + 20_000],))
        out.update(tuple(r) for r in cur.fetchall())
    return out


def _rank(families: Mapping[str, Mapping[str, Any]]) -> dict[tuple[str, str], int]:
    return {(source, prop): i for source, props in families.items()
            for i, prop in enumerate(props)}


def judge(conn: ObjectsConnection, cur: Any, ctx: Mapping[str, Any], d: Directory,
          objects: Sequence[str], counts: Counter,
          served: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The step's link rows from ``objects`` (module docstring), each pair once: of the
    values naming one pair (To and Cc, organizer and attendee), the one ``served`` (the
    served rows by id) cites when it is one of them, else the property the step declares
    first. The step keeps the one its read meets first, which Postgres does not order:
    keeping the served one leaves an unchanged pair unwritten."""
    conn.objects = sorted(set(objects))
    rank = _rank(ctx["families"])
    held = set(conn.objects)
    found: dict[str, list[tuple]] = {}
    try:
        judged = list(pl.judge_values(conn, d.people, ctx["families"], counts))
    finally:
        conn.objects = []
    versions = d.versions({holder.person for _v, _t, holder, _p in judged})
    for value, link_type, holder, prop in judged:
        source = value["object_id"]
        if source not in held:   # the read is the objects' own: never another's
            raise Refused("people_scope_value_outside_objects")
        link_id = pl.PREFIX + pl._digest(source, link_type, holder.person)
        order = (rank.get((source.split(":", 1)[0], prop), len(rank)),
                 value["field_path"] or "", value["l1_handle"] or "", holder.person)
        found.setdefault(link_id, []).append((order, value, link_type, holder, prop))
    rejected = _rejected(cur, conn.objects) if found else set()
    rows = []
    for link_id in sorted(found):
        candidates = sorted(found[link_id], key=lambda c: c[0])
        _order, value, link_type, holder, prop = candidates[0]
        version = versions.get(holder.person)
        if version is None:
            counts["no_version"] += 1
            continue
        if holder.person in d.excluded:
            counts["excluded_target"] += 1
            continue
        if (value["object_id"], holder.person, link_type) in rejected:
            counts["rejected"] += 1
            continue
        made = [pl.link_row(v, t, h, p, version) for _o, v, t, h, p in candidates]
        was = (served or {}).get(link_id)
        same = [r for r in made if was is not None and r["reason"] == was.get("reason")]
        rows.append(same[0] if same else made[0])
    return rows


def judge_aliases(cur: Any, ctx: Mapping[str, Any], d: Directory, counts: Counter
                  ) -> list[dict[str, Any]]:
    """The step's alias rows (``Person.same_person_as``) over the whole directory."""
    if not ctx["aliased"]:
        return []
    pairs = list(pl.alias_pairs(d.people, counts))
    versions = d.versions({h.person for a, b, _t, _f in pairs for h in (a, b)})
    rejected = _rejected(cur, sorted({a.person for a, _b, _t, _f in pairs}))
    rows: dict[str, dict[str, Any]] = {}
    for first, second, tier, feature in pairs:
        if first.person in d.excluded or second.person in d.excluded:
            counts["excluded_target"] += 1
            continue
        row = pl.alias_row(first, second, tier, feature, versions)
        if row is None:
            continue
        if (row["from_object"], row["to_object"], row["link_type"]) in rejected:
            counts["rejected"] += 1
            continue
        rows.setdefault(row["link_id"], row)
    return [rows[k] for k in sorted(rows)]


# --------------------------------------------------------------------------- the run

def _state(cur: Any) -> dict[str, Any]:
    cur.execute("SELECT value FROM ontology.store_meta WHERE key = %s", (STATE,))
    row = cur.fetchone()
    try:
        held = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        return {}
    return held if isinstance(held, dict) and isinstance(held.get("through"), int) else {}


def _newest(cur: Any) -> int | None:
    cur.execute("SELECT to_regclass('ontology.applied_owned') IS NOT NULL")
    if not cur.fetchone()[0]:
        return None
    cur.execute("SELECT coalesce(max(seq), 0) FROM ontology.applied_owned")
    return int(cur.fetchone()[0])


def start_mark(cur: Any) -> None:
    """In a delta layer's transaction (under the lock), before its replaced objects are
    recorded: the run's first mark, when it has none, at the newest ``applied_owned`` row
    so far, so every layer this code applies (which leaves its person links to the run)
    comes after it. Nothing without ``applied_owned`` (no layer is recorded either)."""
    cur.execute("SELECT 1 FROM ontology.store_meta WHERE key = %s", (STATE,))
    if cur.fetchone() is not None:
        return
    newest = _newest(cur)
    if newest is None:
        return
    cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO NOTHING",
                (STATE, json.dumps({"through": newest, "set_by": "first_layer"})))


def _touched(cur: Any, through: int, types: Sequence[str], *, limit: int | None = None
             ) -> list[str]:
    """The objects of ``types`` a layer (no whole-step run's) replaced after ``through``."""
    if _newest(cur) is None:
        return []
    cur.execute("SELECT DISTINCT object_id FROM ontology.applied_owned WHERE seq > %s AND "
                "NOT starts_with(layer, %s) AND split_part(object_id, ':', 1) = ANY(%s)"
                + (f" LIMIT {int(limit)}" if limit else ""),
                (int(through), whole_steps.LAYER_PREFIX, list(types)))
    return sorted(r[0] for r in cur.fetchall())


RUN_MAX = 50_000


def statement_ms(environ: Mapping[str, str] | None = None) -> int:
    """Each statement's limit in ms (``PLX_PERSON_LINKS_STATEMENT_MS``, default
    :data:`STATEMENT_MS`; ``0``: none). Not a number: the default."""
    held = (os.environ if environ is None else environ).get("PLX_PERSON_LINKS_STATEMENT_MS")
    try:
        return max(0, int(held)) if held not in (None, "") else STATEMENT_MS
    except ValueError:
        return STATEMENT_MS


def lock(cur: Any, statement_timeout_ms: int) -> None:
    """The delta's lock, in the open transaction, waited for up to :data:`LOCK_WAIT_MS`
    (a cycle may hold it longer than one statement's limit); then each statement's limit
    again."""
    from operating.pg import ontology_migrate as om
    cur.execute(f"SET LOCAL statement_timeout = {int(LOCK_WAIT_MS)}")
    cur.execute(om.LOCK)
    cur.execute(f"SET LOCAL statement_timeout = {max(0, int(statement_timeout_ms))}")


def _run_max(environ: Mapping[str, str] | None = None) -> int:
    held = os.environ if environ is None else environ
    try:
        return max(1, int(held.get("PLX_PERSON_LINKS_RUN_MAX") or RUN_MAX))
    except ValueError:
        return RUN_MAX


def _due(cur: Any, through: int, types: Sequence[str], most: int) -> tuple[int, list[str]]:
    """(the mark a run reaches, the objects it judges): the objects of ``types`` the
    layers replaced after ``through`` in replacement order, up to ``most`` of them (a
    catch-up is judged over several runs, each holding the lock for a bounded time); the
    mark is the last replacement taken, or the newest when all are."""
    newest = _newest(cur)
    if newest is None:
        return int(through), []
    cur.execute("SELECT object_id, seq FROM ontology.applied_owned WHERE seq > %s AND "
                "NOT starts_with(layer, %s) AND split_part(object_id, ':', 1) = ANY(%s) "
                "ORDER BY seq", (int(through), whole_steps.LAYER_PREFIX, list(types)))
    taken: dict[str, None] = {}
    for oid, seq in cur.fetchall():
        if oid not in taken and len(taken) >= most:
            return int(seq) - 1, sorted(taken)
        taken[oid] = None
    return int(newest), sorted(taken)


def pending(dsn: str) -> bool:
    """A layer replaced an input of the step since the run's mark."""
    import psycopg
    from ontology import step_migrate
    with psycopg.connect(dsn, connect_timeout=10) as pg, pg.cursor() as cur:
        state = _state(cur)
        return "through" in state and bool(_touched(
            cur, state["through"], sorted(step_migrate.STEPS[STEP].inputs(None)), limit=1))


def _linked_to(cur: Any, targets: Iterable[str]) -> set[str]:
    """The objects a served person link names one of ``targets`` from."""
    targets = sorted(set(targets))
    out: set[str] = set()
    for i in range(0, len(targets), 20_000):
        cur.execute("SELECT DISTINCT from_object FROM ontology.link_rows WHERE "
                    "to_object = ANY(%s) AND link_type = ANY(%s)",
                    (targets[i:i + 20_000], list(LINK_TYPES)))
        out.update(r[0] for r in cur.fetchall())
    return out


def _generation(cur: Any) -> Any:
    cur.execute("SELECT value FROM ontology.load_state WHERE name = 'generation'")
    row = cur.fetchone()
    return (row[0] or {}).get("content_digest") if row else None


def _fold_marks(cur: Any, start: Mapping[str, bool], ends: Iterable[str]) -> int:
    """The kept link slice (``link_marks``) carried over this transaction's writes: the
    links from or to its from-ends (a temporary table of them)."""
    from operating.pg import link_marks
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS people_scope_ends (object_id text PRIMARY "
                "KEY) ON COMMIT DROP")
    cur.execute("TRUNCATE people_scope_ends")
    with cur.copy("COPY people_scope_ends (object_id) FROM STDIN") as copy:
        for oid in sorted(set(ends)):
            copy.write_row((oid,))
    return link_marks.fold_layer(cur, dict(start), "people_scope_ends")


class Judge:
    """One session's judging: a compat connection, its server-binding cursor, a local
    store folder, the directory (read once)."""

    def __init__(self, dsn: str, ctx: Mapping[str, Any], folder: Path, *,
                 statement_timeout_ms: int = 0) -> None:
        from ontology.pg_step_inplace import _local_store
        from operating.pg.sqlite_compat import PgCompatConnection
        self.ctx, self.folder = ctx, folder
        self.pg = PgCompatConnection(dsn, statement_timeout_ms=statement_timeout_ms)
        self.cur = self.pg._pg.cursor()
        self.native = native(self.pg)
        self._local = _local_store(folder / "people-read.sqlite")
        self.conn = ObjectsConnection(self._local.connection, self.pg)
        self.d: Directory | None = None

    def directory(self) -> Directory:
        if self.d is None:
            self.d = Directory(self.conn, self.ctx, self.native)
        return self.d

    def new(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, tuple]:
        return whole_steps._normal(rows, self.folder, "people") if rows else {}

    def close(self) -> None:
        try:
            self._local.abort()
        finally:
            self.pg.close()


def _from_ends(gone: Sequence[str], changed: Sequence[str], added: Sequence[str],
               served: Mapping[str, Mapping[str, Any]], new: Mapping[str, tuple]
               ) -> set[str]:
    at = 2   # store_ddl.LINK_COLUMNS: from_object
    return ({served[lid]["from_object"] for lid in [*gone, *changed]}
            | {new[lid][at] for lid in [*changed, *added]})


def _write(cur: Any, new: Mapping[str, tuple], served: Mapping[str, Mapping[str, Any]],
           replaced: Callable[[Mapping[str, Any]], bool], totals: Counter
           ) -> tuple[set[str], tuple[list[str], list[str], list[str]]]:
    gone, changed, added, kept = whole_steps.diff(new, served, replaced)
    if gone or changed or added:
        whole_steps._write(cur, new, served, replaced)
    for name, value in (("gone", len(gone)), ("changed", len(changed)),
                        ("added", len(added)), ("kept", kept)):
        totals[name] += value
    for lid in gone:
        totals[f"gone:{served[lid]['link_type']}"] += 1
    for lid in added:
        totals[f"added:{new[lid][1]}"] += 1
    return _from_ends(gone, changed, added, served, new), (gone, changed, added)


def recompute(dsn: str, *, work: str | Path | None = None, now: float | None = None,
              environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """One run over the layers applied since the mark (module docstring):
    ``{"pending": False}`` when there is nothing to do, else its report (``ends``: the
    objects whose person links changed). Raises on failure: nothing is changed and the mark
    stays, so the next cycle runs it again."""
    import psycopg
    from operating.pg import link_marks
    started = time.monotonic()
    now = time.time() if now is None else now
    with psycopg.connect(dsn, connect_timeout=10) as pg0, pg0.cursor() as cur0:
        cur0.execute("SELECT to_regclass('ontology.store_meta') IS NOT NULL")
        if not cur0.fetchone()[0]:
            return {"pending": False}
        state = _state(cur0)
        if "through" not in state:
            return {"pending": False}
        from ontology import step_migrate
        inputs = sorted(step_migrate.STEPS[STEP].inputs(None))
        if not _touched(cur0, state["through"], inputs, limit=1):
            return {"pending": False}
    ctx = context(environ)
    if ctx is None:
        return {"pending": False, "declared": False}
    folder = Path(tempfile.mkdtemp(prefix=".people-scope-", dir=work))
    report: dict[str, Any] = {"mode": "incremental"}
    totals: Counter = Counter()
    counts: Counter = Counter()
    timeout = statement_ms(environ)
    report["statement_timeout_ms"] = timeout
    j = Judge(dsn, ctx, folder, statement_timeout_ms=timeout)
    try:
        cur = j.cur
        cur.execute("BEGIN")
        t_lock = time.monotonic()
        lock(cur, timeout)
        cur.execute("SET LOCAL jit = off")
        state = _state(cur)
        if "through" not in state:   # a load in between
            cur.execute("ROLLBACK")
            return {"pending": False}
        newest, touched = _due(cur, state["through"], ctx["inputs"], _run_max(environ))
        report["remaining"] = newest < int(_newest(cur) or 0)
        people_touched = [o for o in touched if o.startswith(PERSON_PREFIX)]
        sources = {o for o in touched if not o.startswith(PERSON_PREFIX)
                   and o.split(":", 1)[0] in ctx["sources"]}
        d = j.directory()
        if people_touched:
            # every object linked to one of them or to a Person sharing one of its keys
            keys = d.keys_of(people_touched)
            sharers = {p for kind, key in keys for p in d.people.by[kind].get(key, {})}
            sources |= _linked_to(j.native, set(people_touched) | sharers)
            report["persons_touched"] = len(people_touched)
            report["person_keys_unscoped"] = len(keys)
        marks_start = link_marks.layer_start(j.native)
        ends: set[str] = set()
        ordered = sorted(sources)
        for i in range(0, len(ordered), CHUNK):
            part = ordered[i:i + CHUNK]
            served = served_links(j.native, part)
            new = j.new(judge(j.conn, j.native, ctx, d, part, counts, served))
            got, _ = _write(cur, new, served, _replaced(part), totals)
            ends |= got
        if people_touched:
            new = j.new(judge_aliases(j.native, ctx, d, counts))
            got, _ = _write(cur, new, served_aliases(j.native),
                            _replaced((), aliases=True), totals)
            ends |= got
        cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES (%s, %s) ON "
                    "CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (STATE, json.dumps({"through": newest})))
        report["layer"] = None
        if ends:
            report["layer"] = whole_steps._record(cur, ends, newest, now, prefix=LAYER_PREFIX)
            report["link_marks_folded"] = _fold_marks(j.native, marks_start, ends)
        cur.execute("COMMIT")
        report["lock_seconds"] = round(time.monotonic() - t_lock, 1)
    except BaseException:
        try:
            j.cur.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the session goes with the run
            pass
        raise
    finally:
        j.close()
        for f in folder.glob("*"):
            f.unlink(missing_ok=True)
        folder.rmdir()
    report.update({"touched": len(touched), "objects": len(sources), "persons": d.count,
                   "links": dict(sorted(totals.items())), "counts": dict(sorted(
                       counts.items())), "ends": sorted(ends)})
    if report["layer"]:
        report["facts_kept"] = whole_steps._fold_facts(dsn)
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def rejudge(dsn: str, objects: Iterable[str], *, work: str | Path | None = None,
            now: float | None = None, environ: Mapping[str, str] | None = None,
            prefix: str = LAYER_PREFIX) -> dict[str, Any]:
    """The person links from ``objects`` (of the step's source types; others are left
    alone) judged again over the whole served directory now, as :func:`recompute` judges
    the objects the layers replaced, in one transaction under the delta's lock; the mark
    is not moved. For a Person that became served after the objects naming it were judged
    (``operating.pg.people_directory_scope``). Raises on failure: nothing is changed."""
    from operating.pg import link_marks
    started = time.monotonic()
    now = time.time() if now is None else now
    ctx = context(environ)
    if ctx is None:
        return {"declared": False}
    sources = sorted({o for o in objects if o.split(":", 1)[0] in ctx["sources"]})
    report: dict[str, Any] = {"objects": len(sources), "layer": None}
    if not sources:
        return report
    folder = Path(tempfile.mkdtemp(prefix=".people-rejudge-", dir=work))
    totals: Counter = Counter()
    counts: Counter = Counter()
    timeout = statement_ms(environ)
    j = Judge(dsn, ctx, folder, statement_timeout_ms=timeout)
    try:
        cur = j.cur
        cur.execute("BEGIN")
        lock(cur, timeout)
        cur.execute("SET LOCAL jit = off")
        d = j.directory()
        marks_start = link_marks.layer_start(j.native)
        ends: set[str] = set()
        for i in range(0, len(sources), CHUNK):
            part = sources[i:i + CHUNK]
            served = served_links(j.native, part)
            new = j.new(judge(j.conn, j.native, ctx, d, part, counts, served))
            got, _ = _write(cur, new, served, _replaced(part), totals)
            ends |= got
        if ends:
            report["layer"] = whole_steps._record(cur, ends, int(_newest(j.native) or 0),
                                                  now, prefix=prefix)
            report["link_marks_folded"] = _fold_marks(j.native, marks_start, ends)
        cur.execute("COMMIT")
    except BaseException:
        try:
            j.cur.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the session goes with the run
            pass
        raise
    finally:
        j.close()
        for f in folder.glob("*"):
            f.unlink(missing_ok=True)
        folder.rmdir()
    report.update({"links": dict(sorted(totals.items())),
                   "counts": dict(sorted(counts.items())), "ends": sorted(ends)})
    if report["layer"]:
        report["facts_kept"] = whole_steps._fold_facts(dsn)
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


# --------------------------------------------------------------------------- backfill

def _plan(pg: Any, types: Sequence[str], every: int = 1) -> list[tuple[str, str, str]]:
    """Key ranges ``(type, start, end)`` of :data:`CHUNK` objects each over every object
    of ``types`` (``every``: only each k-th range, a sample), read in one pass of a
    server-side cursor (``pg``: a connection inside a transaction)."""
    with pg.cursor() as plain:
        c = _collate(plain)
    out: list[tuple[str, str, str]] = []
    for name in types:
        lo, hi = f"{name}:", f"{name};"
        bounds = [lo]
        with pg.cursor(name="people_scope_plan") as cur:
            cur.itersize = 20_000
            cur.execute(f"SELECT object_id FROM ontology.objects WHERE object_id{c} >= %s "
                        f"AND object_id{c} < %s ORDER BY object_id{c}", (lo, hi))
            for n, row in enumerate(cur):
                if n and n % CHUNK == 0:   # the first object of the next range
                    bounds.append(row[0])
        bounds.append(hi)
        out += [(name, a, b) for a, b in zip(bounds, bounds[1:])]
    return out[::max(1, int(every))]


_WORKER: dict[str, Any] = {}


def _worker_judge() -> Judge:
    if "judge" not in _WORKER:
        folder = Path(tempfile.mkdtemp(prefix="worker-", dir=_WORKER["tmp"]))
        _WORKER["judge"] = Judge(_WORKER["dsn"], _WORKER["ctx"], folder,
                                 statement_timeout_ms=_WORKER["timeout_ms"])
    return _WORKER["judge"]


def _ids(cur: Any, start: str, end: str) -> list[str]:
    c = _collate(cur)
    cur.execute(f"SELECT object_id FROM ontology.objects WHERE object_id{c} >= %s AND "
                f"object_id{c} < %s", (start, end))
    return sorted(r[0] for r in cur.fetchall())


def _range(task: tuple[int, tuple[str, str, str]]) -> dict[str, Any]:
    """One key range judged over a read-only snapshot, then (``apply``) written under the
    lock (module docstring). Its counts."""
    from operating.pg import link_marks
    from operating.pg.ontology_delta import refresh_repeats
    number, (name, start, end) = task
    t0 = time.monotonic()
    j = _worker_judge()
    ctx, cur, nat = j.ctx, j.cur, j.native
    counts: Counter = Counter()
    totals: Counter = Counter()
    out: dict[str, Any] = {"range": number, "type": name}
    cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        cur.execute("SET LOCAL jit = off")
        snap = int(_newest(nat) or 0)
        if _generation(nat) != _WORKER["generation"]:
            raise Refused("generation_changed")
        d = j.directory()
        ids = _ids(nat, start, end)
        served = served_links(nat, ids) if ids else {}
        rows = judge(j.conn, nat, ctx, d, ids, counts, served) if ids else []
    finally:
        cur.execute("COMMIT")
    new = j.new(rows)
    gone, changed, added, kept = whole_steps.diff(new, served, _replaced(ids))
    out.update({"objects": len(ids), "judge_seconds": round(time.monotonic() - t0, 2)})
    if not _WORKER["apply"] or not (gone or changed or added):
        for k, v in (("gone", len(gone)), ("changed", len(changed)), ("added", len(added)),
                     ("kept", kept)):
            totals[k] += v
        for lid in gone:
            totals[f"gone:{served[lid]['link_type']}"] += 1
        for lid in added:
            totals[f"added:{new[lid][1]}"] += 1
        for lid in changed:
            totals[f"changed:{new[lid][1]}"] += 1
        out.update({"links": dict(totals), "counts": dict(counts),
                    "rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
                    "seconds": round(time.monotonic() - t0, 2)})
        return out
    # the write: under the lock, without the objects a layer replaced since the snapshot
    t1 = time.monotonic()
    cur.execute("BEGIN")
    try:
        lock(cur, _WORKER["timeout_ms"])
        cur.execute("SET LOCAL jit = off")
        if _generation(nat) != _WORKER["generation"]:
            raise Refused("generation_changed")
        nat.execute("SELECT EXISTS (SELECT 1 FROM ontology.applied_owned WHERE seq > %s AND "
                    "starts_with(object_id, %s))", (_WORKER["snap"], PERSON_PREFIX))
        if nat.fetchone()[0]:
            raise Refused("persons_changed")
        nat.execute("SELECT DISTINCT object_id FROM ontology.applied_owned WHERE seq > %s "
                    "AND object_id = ANY(%s)", (snap, ids))
        later = {r[0] for r in nat.fetchall()}
        keep = [o for o in ids if o not in later]
        served = served_links(nat, keep)
        new = {lid: row for lid, row in new.items() if row[2] not in later}
        replaced = _replaced(keep)
        gone, changed, added, kept = whole_steps.diff(new, served, replaced)
        ends: set[str] = set()
        if gone or changed or added:
            _journal(nat, Path(_WORKER["work"]) / f"journal-{number:05d}.jsonl", served,
                     new, gone, changed, added)
            marks_start = link_marks.layer_start(nat)
            ends, _ = _write(cur, new, served, replaced, totals)
            newest = int(_newest(nat) or 0)
            out["layer"] = whole_steps._record(cur, ends, newest, time.time(),
                                               prefix=f"{LAYER_PREFIX}backfill-{number}-")
            _fold_marks(nat, marks_start, ends)
        cur.execute("COMMIT")
    except BaseException:
        try:
            cur.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        raise
    out["lock_seconds"] = round(time.monotonic() - t1, 2)
    out["skipped_later"] = len(later)
    if ends:
        out["repeats"] = bool(refresh_repeats(_WORKER["dsn"], sorted(ends)))
    out.update({"links": dict(totals), "counts": dict(counts),
                "rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
                "seconds": round(time.monotonic() - t0, 2)})
    return out


def _journal(cur: Any, path: Path, served: Mapping[str, Mapping[str, Any]],
             new: Mapping[str, tuple], gone: Sequence[str], changed: Sequence[str],
             added: Sequence[str]) -> None:
    """A write's journal entry, fsynced inside its transaction before it writes: the rows
    it replaces (``put_back``), the rows it writes (``written``, every column) and its
    transaction id (``xid``). :func:`undo` takes back only an entry whose transaction
    committed (``pg_xact_status``): a write that failed or was stopped after its entry
    leaves an entry ``undo`` skips."""
    from ontology import store_ddl
    cols = store_ddl.LINK_COLUMNS
    cur.execute("SELECT pg_current_xact_id()::text")
    entry = {"xid": cur.fetchone()[0],
             "put_back": [[served[lid][c] for c in cols] for lid in [*gone, *changed]],
             "written": [list(new[lid]) for lid in [*changed, *added]]}
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        out.flush()
        os.fsync(out.fileno())


def backfill(dsn: str, work: str | Path, *, apply: bool, workers: int | None = None,
             types: Sequence[str] | None = None, every: int = 1,
             environ: Mapping[str, str] | None = None, log: Any = None,
             statement_timeout_ms: int = STATEMENT_MS) -> dict[str, Any]:
    """Every object of the step's source types (``types``: some of them) judged again
    (module docstring); ``apply`` writes, else a dry run counts. ``every``: each k-th key
    range only (a sample; never with ``apply``). ``statement_timeout_ms``: each
    statement's limit (0: none; the lock's wait: :func:`lock`)."""
    import multiprocessing
    import shutil
    import psycopg
    from ontology import parent_watch
    from operating.pg import link_marks
    if apply and every != 1:
        raise Refused("sample_never_applied")
    started = time.monotonic()
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    ctx = context(environ)
    if ctx is None:
        return {"declared": False}
    wanted = list(types or ctx["sources"])
    unknown = sorted(set(wanted) - set(ctx["sources"]))
    if unknown:
        raise Refused(f"types_not_judged:{','.join(unknown)}")
    timeout = int(statement_timeout_ms)
    with psycopg.connect(dsn, connect_timeout=10,
                         options=f"-c statement_timeout={timeout}") as pg:
        with pg.transaction():
            with pg.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                generation = _generation(cur)
                snap = int(_newest(cur) or 0)
            tasks = _plan(pg, wanted, every)
    tmp = work / "tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    _WORKER.clear()
    _WORKER.update(dsn=dsn, ctx=ctx, apply=apply, work=str(work), generation=generation,
                   snap=snap, tmp=str(tmp), timeout_ms=timeout)
    parts = max(1, int(workers or os.cpu_count() or 1))
    results: list[dict[str, Any]] = []
    try:
        if parts == 1:
            results = [_range(t) for t in enumerate(tasks)]
        else:
            with multiprocessing.get_context("fork").Pool(
                    parts, **parent_watch.pool_init()) as pool:
                for done in pool.imap_unordered(_range, list(enumerate(tasks))):
                    results.append(done)
                    if log is not None:
                        print(json.dumps(done, sort_keys=True), file=log, flush=True)
        if "judge" in _WORKER:
            _WORKER["judge"].close()
    finally:
        _WORKER.clear()
        shutil.rmtree(tmp, ignore_errors=True)
    totals: Counter = Counter()
    counts: Counter = Counter()
    for r in results:
        totals.update(r.get("links", {}))
        counts.update(r.get("counts", {}))
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run", "ranges": len(tasks), "every": every,
        "types": wanted, "workers": parts, "objects": sum(r["objects"] for r in results),
        "links": dict(sorted(totals.items())), "counts": dict(sorted(counts.items())),
        "slowest_range_s": max((r["seconds"] for r in results), default=0),
        "longest_lock_s": max((r.get("lock_seconds", 0) for r in results), default=0),
        "peak_worker_rss_mb": max((r["rss_mb"] for r in results), default=0),
        "skipped_later": sum(r.get("skipped_later", 0) for r in results)}
    # the alias links, over the whole directory, once (in a range's place)
    with tempfile.TemporaryDirectory(prefix=".people-aliases-", dir=work) as held:
        j = Judge(dsn, ctx, Path(held), statement_timeout_ms=timeout)
        try:
            cur = j.cur
            cur.execute("BEGIN" if apply else
                        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if apply:
                lock(cur, timeout)
            cur.execute("SET LOCAL jit = off")
            alias_counts: Counter = Counter()
            new = j.new(judge_aliases(j.native, ctx, j.directory(), alias_counts))
            served = served_aliases(j.native)
            replaced = _replaced((), aliases=True)
            gone, changed, added, kept = whole_steps.diff(new, served, replaced)
            report["aliases"] = {"gone": len(gone), "changed": len(changed),
                                 "added": len(added), "kept": kept}
            if apply and (gone or changed or added):
                _journal(j.native, work / "journal-aliases.jsonl", served, new, gone,
                         changed, added)
                marks_start = link_marks.layer_start(j.native)
                ends, _ = _write(cur, new, served, replaced, Counter())
                report["aliases"]["layer"] = whole_steps._record(
                    cur, ends, int(_newest(j.native) or 0), time.time(),
                    prefix=f"{LAYER_PREFIX}backfill-aliases-")
                _fold_marks(j.native, marks_start, ends)
            cur.execute("COMMIT")
        except BaseException:
            try:
                j.cur.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            j.close()
    if apply:
        report["facts_kept"] = whole_steps._fold_facts(dsn)
    report["seconds"] = round(time.monotonic() - started, 1)
    name = "report.json" if apply else "dry-run.json"
    (work / f"{name}.tmp").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n",
                                      encoding="utf-8")
    (work / f"{name}.tmp").replace(work / name)
    return report


def _xact_status(cur: Any, xid: Any) -> str | None:
    if not isinstance(xid, str) or not xid.isdigit():
        return None
    cur.execute("SELECT pg_xact_status(%s::xid8)", (xid,))
    return cur.fetchone()[0]


# A journal entry of the 11-18 backfill's release (9bfb137ffb) holds no transaction id and
# only the ids it wrote. Its write committed exactly when the range's layer
# (``whole-steps-people-backfill-<range>-<mark>-<ms>``, recorded in the same transaction)
# is in ``load_state``, timed from a minute before the journal was last written (fsynced
# just before the write) to :data:`LEGACY_AFTER_S` after it.
LEGACY_AFTER_S = 6 * 3600


def _legacy_layer(cur: Any, path: Path) -> list[tuple[str, int]]:
    """The layers that can be the commit of the old-format journal ``path``'s write:
    (name, ms) of each in the window."""
    suffix = path.stem[len("journal-"):]
    if suffix == "aliases":
        number = "aliases"
    elif suffix.isdigit():
        number = str(int(suffix))
    else:
        raise Refused(f"journal_name_unknown:{path.name}")
    prefix = f"delta:{LAYER_PREFIX}backfill-{number}-"
    cur.execute("SELECT name FROM ontology.load_state WHERE starts_with(name, %s)", (prefix,))
    written_at = path.stat().st_mtime
    found = []
    for (name,) in cur.fetchall():
        mark, _, ms = name[len(prefix):].partition("-")
        if not (mark.isdigit() and ms.isdigit()):
            continue
        if written_at - 60 <= int(ms) / 1000 <= written_at + LEGACY_AFTER_S:
            found.append((name[len("delta:"):], int(ms)))
    return found


def _later_writes(cur: Any, ends: set[str], layer: str, after_ms: int) -> int:
    """Objects of ``ends`` whose person links a run other than ``layer`` changed after
    ``after_ms`` (its ``whole-steps-people-*`` layers' ``applied_owned`` rows)."""
    if not ends:
        return 0
    cur.execute("SELECT object_id, layer FROM ontology.applied_owned WHERE object_id = "
                "ANY(%s) AND starts_with(layer, %s)", (sorted(ends), LAYER_PREFIX))
    moved = set()
    for oid, name in cur.fetchall():
        ms = name.rsplit("-", 1)[-1]
        if name != layer and ms.isdigit() and int(ms) > after_ms:
            moved.add(oid)
    return len(moved)


def undo(dsn: str, work: str | Path, *, now: float | None = None,
         dry_run: bool = False) -> dict[str, Any]:
    """Every committed backfill write of ``work`` taken back, newest first, in one
    transaction under the lock: the rows it wrote deleted and the rows it replaced put
    back. An entry whose write did not commit (``aborted``) is skipped. Refused, nothing
    changed, when an entry's outcome cannot be read (too old, still running) or a later
    write moved a row it wrote or put back (``undo_conflict``: an undo is the backfill's
    immediate rollback, never a merge). The changed ends are recorded as a layer and the
    derived link caches refreshed as a write's are (link slice, mention repeats, traverse
    facts); the journals are then moved to ``undone-<ms>/``, so a second undo finds
    nothing. ``dry_run``: every check in a read-only transaction, nothing written or moved.

    An entry of the 11-18 backfill's release (no ``xid``, ``written`` its ids only) counts
    as committed when its range's layer is recorded (:func:`_legacy_layer`; none: not
    committed, skipped); then each row it added must still be held, each it removed still
    gone, each it changed still other than it was, and no later person-links write may
    have changed the links of one of its ends (``undo_conflict``). An entry whose rows show
    part of its write, or a write without its layer, is refused (``journal_status_``)."""
    import psycopg
    from ontology import store_ddl
    from operating.pg import link_marks
    from operating.pg.ontology_delta import _copy, refresh_repeats
    work = Path(work)
    now = time.time() if now is None else now
    files = sorted(work.glob("journal-*.jsonl"), reverse=True)
    cols = list(store_ddl.LINK_COLUMNS)
    at = cols.index("from_object")
    out: dict[str, Any] = {"journals": len(files), "deleted": 0, "restored": 0,
                           "skipped_aborted": 0, "legacy": 0, "layer": None}
    if dry_run:
        out["mode"] = "dry-run"
    ends: set[str] = set()
    with psycopg.connect(dsn, connect_timeout=10) as pg, pg.cursor() as cur:
        with pg.transaction():
            if dry_run:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(f"SET LOCAL statement_timeout = {statement_ms()}")
            else:
                lock(cur, statement_ms())
                marks_start = link_marks.layer_start(cur)
            for path in files:
                entries = [json.loads(line) for line in
                           path.read_text(encoding="utf-8").splitlines() if line.strip()]
                for entry in reversed(entries):
                    legacy = "xid" not in entry and all(
                        isinstance(r, str) for r in entry.get("written") or ())
                    put_back = [tuple(r) for r in entry["put_back"]]
                    if legacy:
                        out["legacy"] += 1
                        written: dict[str, tuple | None] = dict.fromkeys(entry["written"])
                    else:
                        status = _xact_status(cur, entry.get("xid"))
                        if status == "aborted":
                            out["skipped_aborted"] += 1
                            continue
                        if status != "committed":
                            raise Refused(f"journal_status_{status or 'unknown'}:{path.name}")
                        written = {r[0]: tuple(r) for r in entry["written"]}
                    before = {r[0]: r for r in put_back}
                    ids = sorted(set(written) | set(before))
                    cur.execute(f"SELECT {', '.join(cols)} FROM ontology.links WHERE "
                                "link_id = ANY(%s)", (ids,))
                    now_held = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
                    if legacy:
                        # its effect: each row it wrote held and other than it was, each
                        # it removed gone
                        shown = {lid: (lid in now_held and not (
                            lid in before and whole_steps._same(before[lid], now_held[lid])))
                            if lid in written else lid not in now_held for lid in ids}
                        layers = _legacy_layer(cur, path)
                        if len(layers) > 1:
                            raise Refused(f"journal_status_ambiguous:{path.name}")
                        if not layers:
                            if any(shown.values()):   # rows moved, yet no commit recorded
                                raise Refused(f"journal_status_unknown:{path.name}")
                            out["skipped_aborted"] += 1
                            continue
                        mine = {now_held[lid]["from_object"] for lid in written
                                if lid in now_held} | {r[at] for r in put_back}
                        if not all(shown.values()) or _later_writes(cur, mine, *layers[0]):
                            raise Refused(f"undo_conflict:{path.name}")
                    else:
                        # each row it wrote still as written; each row it removed still gone
                        moved = [lid for lid in ids if (
                            lid not in now_held or not whole_steps._same(written[lid],
                                                                         now_held[lid])
                            if lid in written else lid in now_held)]
                        if moved:
                            raise Refused(f"undo_conflict:{len(moved)}:{path.name}")
                    if dry_run:
                        out["deleted"] += len(written)
                        out["restored"] += len(put_back)
                        ends.update(now_held[lid]["from_object"] for lid in written
                                    if lid in now_held)
                        ends.update(r[at] for r in put_back)
                        continue
                    if written:
                        cur.execute("DELETE FROM ontology.link_rows WHERE link_id = ANY(%s) "
                                    "RETURNING from_object", (sorted(written),))
                        ends.update(r[0] for r in cur.fetchall())
                        out["deleted"] += cur.rowcount
                    if put_back:
                        out["restored"] += _copy(cur, "link_rows", cols, put_back, keys={})
                        ends.update(r[at] for r in put_back)
            if ends and not dry_run:
                out["layer"] = whole_steps._record(cur, ends, int(_newest(cur) or 0), now,
                                                   prefix=f"{LAYER_PREFIX}undo-")
                out["link_marks_folded"] = _fold_marks(cur, marks_start, ends)
    out["ends"] = len(ends)
    if dry_run:
        return out
    if ends:
        out["mention_repeats"] = refresh_repeats(dsn, sorted(ends))
        out["facts_kept"] = whole_steps._fold_facts(dsn)
    if files:
        done = work / f"undone-{int(now * 1000)}"
        done.mkdir()
        for path in files:
            path.replace(done / path.name)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.people_scope")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="one incremental run now")
    run.add_argument("--dsn", default=None)
    fill = sub.add_parser("backfill", help="every object judged again")
    fill.add_argument("--dsn", default=None)
    fill.add_argument("--work", required=True)
    how = fill.add_mutually_exclusive_group(required=True)
    how.add_argument("--dry-run", action="store_true")
    how.add_argument("--apply", action="store_true")
    fill.add_argument("--workers", type=int, default=None)
    fill.add_argument("--types", default=None, help="comma-separated source types")
    fill.add_argument("--every", type=int, default=1, help="each k-th range (dry run)")
    fill.add_argument("--statement-timeout-ms", type=int, default=STATEMENT_MS,
                      help="each statement's limit (0: none)")
    back = sub.add_parser("undo", help="take a backfill's writes back")
    back.add_argument("--dsn", default=None)
    back.add_argument("--work", required=True)
    back.add_argument("--dry-run", action="store_true",
                      help="check every entry, write nothing")
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    if args.cmd == "run":
        out = recompute(dsn)
        out.pop("ends", None)
    elif args.cmd == "backfill":
        out = backfill(dsn, args.work, apply=args.apply, workers=args.workers,
                       types=args.types.split(",") if args.types else None,
                       every=args.every, log=sys.stderr,
                       statement_timeout_ms=args.statement_timeout_ms)
    else:
        out = undo(dsn, args.work, dry_run=args.dry_run)
    print(json.dumps(out, sort_keys=True, default=str), flush=True)
    return 0


__all__ = ["CHUNK", "Directory", "LAYER_PREFIX", "ObjectsConnection", "Refused", "STATE",
           "backfill", "context", "judge", "judge_aliases", "lock", "pending", "prefix",
           "recompute", "rejudge", "start_mark", "statement_ms", "undo"]


if __name__ == "__main__":
    sys.exit(main())
