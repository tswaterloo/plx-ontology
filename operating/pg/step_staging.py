"""The links an in-place step run re-derives, held once in Postgres as their difference to
the served links, read by the later steps and applied set-based.

Why (measured 11-11 on served plx): the ``mentions`` step of migration-next re-derives
~20M links (1.5 KB each in a step store, 2.3 KB as a Postgres row). Each later step was a
process loading every earlier step's store into its own ``pg_temp.run_rows`` (~45 GB and
~50 min a step, 14 steps), and the step layer copied every link twice more before the
apply wrote them row by row. Here a finished step's store is compared once with the served
links of its owned scope (as ``operating.pg.migrate_chunks`` compares them: by link id and
every column as the ``links`` view reads them) and only the difference is kept, in the
run-owned schema :data:`SCHEMA` (never a served table):

* ``links``: the run's links Postgres does not hold alike (new, or replacing a served one);
* ``gone``: the served links of the scope the run does not hold alike (removed or replaced);
* ``staged``: one row per staged (step, part) with its counts and digest, the marker a
  rerun and the apply check.

A later step's overlay (``ontology.pg_step_inplace.Overlay``) shows the served links less
``gone`` plus ``links``, of the steps before it only (by name, the steps' order), so what
it reads is what the full build shows it, without any copy. The apply
(``migrate_chunks``, chunk ``steps:staged:<step>``) deletes a step's ``gone`` and inserts
its ``links`` in one statement each; :func:`drop` removes the schema after the apply.

**Extractor-only narrowing** (``mentions`` only, :data:`NARROWED`): a re-derived mention
link that differs from the served one only by the ``extractor`` named in its reason (the
identifier grammar's digest: ``mentions.extractor_version``) counts as held alike and the
served row is kept. Its evidence, citations, digests, tier and markings are equal; the
served stamp names the grammar that produced it, as on every mention link of a document
outside the run's scope. Measured on a 0.5 % sample: 61 % of the re-derived links, so the
apply writes ~7.7M links instead of ~20M. Counted per stage (``extractor_only``).

Fails closed: a stage of a (step, part) another run staged (:data:`SCHEMA` ``meta`` names
the run), a store link outside the scope it is staged under, a store holding one link id
twice; the apply refuses when the markers differ from the step layer's record or a ``gone``
link is no longer served (something wrote links since).

Stdlib and psycopg.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence

SCHEMA = os.environ.get("PLX_STEP_STAGE_SCHEMA", "steps_stage")
NARROWED = frozenset({"mentions"})
_BATCH = 20_000
_OBJECT_BATCH = 20_000


class StageRefused(RuntimeError):
    """The difference cannot be staged (or applied) as the full build's: nothing kept."""


def schema() -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", SCHEMA):
        raise StageRefused(f"stage_schema_name:{SCHEMA}")
    return SCHEMA


def ensure(cur: Any, run_key: str, s: str | None = None) -> None:
    """The stage schema and its tables (unlogged: a crash loses them together, markers
    included, and the steps are staged again from their stores); refused when another
    run's key is held. Commit it before staging: no DDL runs once the tables are there, so
    a part staging never locks the tables the other parts' overlays read (measured 11-11:
    an ``ALTER TABLE`` here held the links table exclusively for each part's whole stage,
    and every other part's reads waited on it). ``s``: another stage schema than
    :func:`schema` (the delta follower's, :data:`DELTA_SCHEMA`)."""
    s = s or schema()
    cur.execute("SELECT to_regclass(%s)", (f"{s}.staged",))
    if cur.fetchone()[0] is None:
        # made once, by one session (the parts may start together)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(s),))
        cur.execute("SELECT to_regclass(%s)", (f"{s}.staged",))
        if cur.fetchone()[0] is None:
            _create(cur, s)
    cur.execute(f"INSERT INTO {s}.meta (key, value) VALUES ('run', %s) ON CONFLICT DO NOTHING",
                (run_key,))
    cur.execute(f"SELECT value FROM {s}.meta WHERE key = 'run'")
    held = cur.fetchone()[0]
    if held != run_key:
        raise StageRefused(f"stage_of_another_run:{held}")


def _lock_key(s: str) -> int:
    return int(hashlib.sha256(f"plx-step-stage:{s}".encode()).hexdigest()[:15], 16)


def _create(cur: Any, s: str) -> None:
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
    cur.execute(f"CREATE UNLOGGED TABLE IF NOT EXISTS {s}.meta (key text PRIMARY KEY, "
                "value text NOT NULL)")
    # the served table's column defaults (``ek``, ``slots``) but no row number: the apply
    # numbers the rows as it inserts them (in part and store order)
    cur.execute(f"CREATE UNLOGGED TABLE IF NOT EXISTS {s}.links (LIKE ontology.link_rows "
                "INCLUDING DEFAULTS, step text NOT NULL, part integer NOT NULL, "
                "ord bigint NOT NULL)")
    cur.execute(f"ALTER TABLE {s}.links ALTER COLUMN row_no DROP DEFAULT")
    cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS links_by_id ON {s}.links (link_id)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS links_from ON {s}.links (from_object, link_type)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS links_to ON {s}.links (to_object, link_type)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS links_by_step ON {s}.links (step, part, ord)")
    cur.execute(f"CREATE UNLOGGED TABLE IF NOT EXISTS {s}.gone (link_id text PRIMARY KEY, "
                "step text NOT NULL, part integer NOT NULL)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS gone_by_step ON {s}.gone (step, part)")
    # the marker last: its presence says the tables are all there
    cur.execute(f"CREATE UNLOGGED TABLE IF NOT EXISTS {s}.staged (step text NOT NULL, "
                "part integer NOT NULL, report text NOT NULL, PRIMARY KEY (step, part))")


def exists(cur: Any, s: str | None = None) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"{s or schema()}.staged",))
    return cur.fetchone()[0] is not None


def staged(cur: Any, step: str, s: str | None = None) -> dict[int, dict[str, Any]]:
    """The staged parts of ``step``: part -> its report (none when the schema is gone)."""
    if not exists(cur, s):
        return {}
    cur.execute(f"SELECT part, report FROM {s or schema()}.staged WHERE step = %s", (step,))
    return {p: json.loads(r) for p, r in cur.fetchall()}


def stripped(reason: Any) -> Any:
    """A link's reason with the ``extractor`` of its features left out (the narrowing's
    comparison); a reason that is no JSON list of features is returned as it is."""
    if not isinstance(reason, str):
        return reason
    try:
        features = json.loads(reason)
    except ValueError:
        return reason
    if not isinstance(features, list):
        return reason
    out = [{k: v for k, v in f.items() if k != "extractor"} if isinstance(f, dict) else f
           for f in features]
    return json.dumps(out, sort_keys=True, separators=(",", ":"))


def _hashes(row: Sequence[Any], narrow: bool) -> tuple[int, int | None]:
    from ontology import store_ddl
    from operating.pg.migrate_chunks import _hash
    if not narrow:
        return _hash(row), None
    at = store_ddl.LINK_COLUMNS.index("reason")
    other = list(row)
    other[at] = stripped(row[at])
    return _hash(row), _hash(other)


def _served(cur: Any, scope: Mapping[str, Any], narrow: bool
            ) -> dict[str, tuple[int, int | None]]:
    """The served links of ``scope`` (``prefix``/``end``/``from``, or ``objects``: the ids
    they are from), link id -> (row hash, narrowed hash)."""
    from ontology.pg_byte_order import key_collate
    from operating.pg.migrate_chunks import _served_links
    out: dict[str, tuple[int, int | None]] = {}
    c = key_collate(cur)
    if scope.get("objects") is not None:
        objects = sorted(scope["objects"])
        for k in range(0, len(objects), _OBJECT_BATCH):
            for row in _served_links(cur, f"l.from_object = ANY(%s::text[]) AND l.link_id"
                                     f"{c} >= %s AND l.link_id{c} < %s",
                                     [objects[k:k + _OBJECT_BATCH], scope["prefix"],
                                      scope["end"]]):
                out[row[0]] = _hashes(row, narrow)
        return out
    for row in _served_links(cur, f"l.link_id{c} >= %s AND l.link_id{c} "
                             "< %s AND (%s::text[] IS NULL OR EXISTS (SELECT 1 FROM "
                             "unnest(%s::text[]) f WHERE starts_with(l.from_object, f)))",
                             [scope["prefix"], scope["end"], scope["from"], scope["from"]]):
        out[row[0]] = _hashes(row, narrow)
    return out


def _in_scope(row: Sequence[Any], scope: Mapping[str, Any], objects: set[str] | None) -> bool:
    if not scope["prefix"] <= row[0] < scope["end"]:
        return False
    if objects is not None:
        return row[2] in objects
    return scope["from"] is None or row[2].startswith(tuple(scope["from"]))


def _store_rows(path: Path) -> Iterator[tuple]:
    from ontology import store_ddl
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = conn.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM links "
                           "ORDER BY rowid")
        while True:
            got = cur.fetchmany(_BATCH)
            if not got:
                return
            yield from (tuple(r) for r in got)
    finally:
        conn.close()


def stage(dsn: str, run_key: str, step: str, part: int, store: Path,
          scopes: Sequence[Mapping[str, Any]], objects: Iterable[str] | None = None
          ) -> dict[str, Any]:
    """Stage ``store`` (a finished step's links, or one part of a parted step) against the
    served links of ``scopes`` (the step's owned entries; ``objects``: the part's document
    objects, every scope then holding only links from them, and every store link must be
    from one). One transaction: the (step, part) staged again from nothing, its marker
    last. Returns its report."""
    import psycopg
    narrow = step in NARROWED
    objs = set(objects) if objects is not None else None
    s = schema()
    digest = hashlib.sha256()
    with psycopg.connect(dsn, application_name="plx-step-stage") as pg:
        with pg.cursor() as cur:
            ensure(cur, run_key)
            pg.commit()   # the tables made (if they were not) before the long transaction
            cur.execute("SET LOCAL jit = off")
            cur.execute(f"DELETE FROM {s}.staged WHERE step = %s AND part = %s", (step, part))
            cur.execute(f"DELETE FROM {s}.links WHERE step = %s AND part = %s", (step, part))
            cur.execute(f"DELETE FROM {s}.gone WHERE step = %s AND part = %s", (step, part))
            held: dict[str, tuple[int, int | None]] = {}
            for scope in scopes:
                held.update(_served(cur, {**scope, "objects": sorted(objs)}
                                    if objs is not None else scope, narrow))
            outside: list[str] = []

            def check(row: tuple) -> None:
                if not any(_in_scope(row, sc, objs) for sc in scopes):
                    if objs is not None:
                        raise StageRefused(f"store_link_outside_scope:{step}:{row[0]}")
                    outside.append(row[0])

            report, digest, _replaced = stage_rows(cur, s, step, part, _store_rows(store),
                                                   held, narrow, check, digest=digest)
            report["outside"] = len(outside)
            # a store link outside every owned scope (the layer's ``steps:rest``): written as
            # the plain apply writes it, replacing a served one of its id
            if outside:
                for k in range(0, len(outside), _BATCH):
                    cur.execute("SELECT l.link_id FROM ontology.link_rows l WHERE l.link_id = "
                                "ANY(%s::text[])", (outside[k:k + _BATCH],))
                    found = [r[0] for r in cur.fetchall()]
                    if found:
                        raise StageRefused(f"store_link_outside_scope_served:{step}:"
                                           f"{found[0]}")
            rest = sorted(held)   # served, not in the store: removed
            for k in range(0, len(rest), _BATCH):
                _gone(cur, s, step, part, rest[k:k + _BATCH])
            for g in rest:
                digest.update(b"-" + g.encode())
            report["gone"] += len(rest)
            report["digest"] = digest.hexdigest()   # "ends": its links' end type prefixes
            cur.execute(f"INSERT INTO {s}.staged (step, part, report) VALUES (%s, %s, %s)",
                        (step, part, json.dumps(report, sort_keys=True)))
        pg.commit()
    return report


def stage_rows(cur: Any, s: str, step: str, part: int, rows: Iterable[tuple],
               held: dict[str, tuple[int, int | None]], narrow: bool = False,
               check: Any = None, digest: Any = None) -> tuple[dict[str, Any], Any, int]:
    """``rows`` (``LINK_COLUMNS`` tuples) staged in the caller's transaction as (step,
    part) of schema ``s`` against ``held`` (the served links of the scope: link id -> row
    hashes, :func:`served_rows`): a row held alike is left out, any other is written to
    ``links`` (a served one of its id to ``gone``); the ids found are popped from
    ``held``, so what it keeps after is served and not in ``rows`` (the caller's to gone).
    ``check(row)``: called on every row (it may refuse). Refused: one link id twice.
    Returns (report, digest so far, rows replacing a served one)."""
    from ontology import store_ddl
    cols = list(store_ddl.LINK_COLUMNS)
    digest = digest or hashlib.sha256()
    report = {"rows": 0, "same": 0, "extractor_only": 0, "written": 0, "gone": 0,
              "outside": 0}
    seen: set[str] = set()
    pending: list[tuple] = []
    ends: set[str] = set()
    replaced = 0

    def flush() -> None:
        if not pending:
            return
        with cur.copy(f"COPY {s}.links ({', '.join(cols)}, step, part, ord) "
                      "FROM STDIN") as cp:
            for row in pending:
                cp.write_row(row)
        pending.clear()

    for n, row in enumerate(rows):
        report["rows"] += 1
        ends.add(row[2].split(":", 1)[0] + ":")
        ends.add(row[3].split(":", 1)[0] + ":")
        if row[0] in seen:
            raise StageRefused(f"store_link_twice:{step}:{row[0]}")
        seen.add(row[0])
        if check is not None:
            check(row)
        h, hn = _hashes(row, narrow)
        served = held.pop(row[0], None)
        if served is not None and served[0] == h:
            report["same"] += 1
            continue
        if served is not None and narrow and served[1] == hn:
            report["extractor_only"] += 1
            continue
        if served is not None:
            digest.update(b"-" + row[0].encode())
            _gone(cur, s, step, part, [row[0]])
            report["gone"] += 1
            replaced += 1
        digest.update(b"+" + json.dumps(row, default=str).encode())
        pending.append((*row, step, part, n))
        report["written"] += 1
        if len(pending) >= _BATCH:
            flush()
    flush()
    report["ends"] = sorted(ends)
    return report, digest, replaced


def served_rows(cur: Any, where: str, params: Sequence[Any] = (),
                held: dict[str, tuple[int, int | None]] | None = None
                ) -> dict[str, tuple[int, int | None]]:
    """The served links ``where`` holds (on ``ontology.link_rows l``), link id -> row
    hashes, as :func:`stage_rows` compares them (added to ``held``)."""
    from operating.pg.migrate_chunks import _served_links
    held = {} if held is None else held
    for row in _served_links(cur, where, list(params)):
        held[row[0]] = _hashes(row, False)
    return held


def _gone(cur: Any, s: str, step: str, part: int, ids: Sequence[str]) -> None:
    with cur.copy(f"COPY {s}.gone (link_id, step, part) FROM STDIN") as cp:
        for i in ids:
            cp.write_row((i, step, part))


def totals(parts: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    """A step's parts' reports as one (counts added; the digests in part order)."""
    out: dict[str, Any] = {"parts": len(parts)}
    for k in ("rows", "same", "extractor_only", "written", "gone", "outside"):
        out[k] = sum(int(p.get(k) or 0) for p in parts.values())
    out["digest"] = hashlib.sha256("".join(parts[p]["digest"] for p in sorted(parts))
                                   .encode()).hexdigest()
    out["ends"] = sorted({e for p in parts.values() for e in p.get("ends") or ()})
    return out


def overlay_sql(step: str, part_before: int | None = None) -> tuple[str, str, list[Any]]:
    """(the condition hiding the served links a staged earlier step replaced or removed,
    on ``l``; the staged links of the steps before ``step``, as a select of ``cols``; its
    parameters): only steps before ``step`` count (by name, the steps' order).
    ``part_before``: also ``step``'s own parts numbered below it (the apply-as-you-go
    proof: a mention batch sees the batches before it as if they were applied)."""
    s = schema()
    if part_before is None:
        return (f"NOT EXISTS (SELECT 1 FROM {s}.gone g WHERE g.link_id = l.link_id AND "
                f"g.step < %s)", f"FROM {s}.links WHERE step < %s", [step, step])
    return (f"NOT EXISTS (SELECT 1 FROM {s}.gone g WHERE g.link_id = l.link_id AND "
            f"(g.step < %s OR (g.step = %s AND g.part < %s)))",
            f"FROM {s}.links WHERE (step < %s OR (step = %s AND part < %s))",
            [step, step, part_before, step, step, part_before])


# ------------------------------------------------- applied as they go (11-11, owner)

def _ensure_applied(cur: Any, s: str) -> None:
    """The applied markers and the undo of every applied bit (made once, like
    :func:`ensure`; logged tables: they must survive a crash, as the served rows do)."""
    cur.execute("SELECT to_regclass(%s)", (f"{s}.applied",))
    if cur.fetchone()[0] is not None:
        return
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(s + ".applied"),))
    cur.execute("SELECT to_regclass(%s)", (f"{s}.applied",))
    if cur.fetchone()[0] is not None:
        return
    # the served rows a bit deleted, as they were (raw: skeleton ids and all)
    cur.execute(f"CREATE TABLE IF NOT EXISTS {s}.undo_deleted (LIKE ontology.link_rows, "
                "step text NOT NULL, part integer NOT NULL)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS undo_deleted_by_bit ON {s}.undo_deleted "
                "(step, part)")
    # the link ids a bit inserted
    cur.execute(f"CREATE TABLE IF NOT EXISTS {s}.undo_inserted (link_id text NOT NULL, "
                "step text NOT NULL, part integer NOT NULL)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS undo_inserted_by_bit ON {s}.undo_inserted "
                "(step, part)")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {s}.applied (step text NOT NULL, part integer "
                "NOT NULL, staged text NOT NULL, report text NOT NULL, applied_at "
                "timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (step, part))")


def applied(cur: Any, step: str, s: str | None = None) -> dict[int, dict[str, Any]]:
    """The applied parts of ``step``: part -> the staged report it applied."""
    s = s or schema()
    cur.execute("SELECT to_regclass(%s)", (f"{s}.applied",))
    if cur.fetchone()[0] is None:
        return {}
    cur.execute(f"SELECT part, staged FROM {s}.applied WHERE step = %s", (step,))
    return {p: json.loads(r) for p, r in cur.fetchall()}


def apply_now(dsn: str, run_key: str, step: str, parts: Iterable[int] | None = None,
              columns: Sequence[str] | None = None) -> dict[str, Any]:
    """The staged bits of ``step`` (``parts``: those parts; None: every staged part)
    written to the served links now, each bit in its own transaction under the delta's
    lock: its served rows kept as they were (``undo_deleted``) and deleted, its staged
    links inserted (their ids kept: ``undo_inserted``) and dropped from the stage, the
    marker (``applied``: the bit's staged report and the rows deleted and inserted per
    link type) written last. A bit applied before is skipped (resumable, idempotent).
    Refused (that bit rolled back) when a ``gone`` link is no longer served, or the
    counts per link type differ from the stage."""
    import psycopg
    from operating.pg.ontology_migrate import LOCK
    s = schema()
    out: dict[str, Any] = {"step": step, "bits": {}}
    with psycopg.connect(dsn, application_name="plx-step-stage") as pg:
        with pg.cursor() as cur:
            ensure(cur, run_key)
            _ensure_applied(cur, s)
            pg.commit()
            held = staged(cur, step)
            done = applied(cur, step)
            pg.commit()
            want = sorted(held) if parts is None else sorted(parts)
            for part in want:
                if part not in held:
                    raise StageRefused(f"not_staged:{step}:{part}")
                if part in done:
                    if done[part] != held[part]:
                        raise StageRefused(f"applied_other_stage:{step}:{part}")
                    out["bits"][part] = {"skipped": "applied"}
                    continue
                cur.execute(LOCK)
                cur.execute("SET LOCAL jit = off")
                try:
                    bit = apply_in(cur, s, step, part, held[part], columns)
                except StageRefused:
                    pg.rollback()
                    raise
                pg.commit()
                out["bits"][part] = bit
    return out


def apply_in(cur: Any, s: str, step: str, part: int, rep: Mapping[str, Any],
             columns: Sequence[str] | None = None) -> dict[str, Any]:
    """One staged bit (``rep``: its staged report) written to the served links in the
    caller's transaction, which holds the delta's lock: its served rows kept as they were
    (``undo_deleted``) and deleted, its staged links inserted (their ids kept:
    ``undo_inserted``) and dropped from the stage, the marker (``applied``) written last.
    Refused (the caller rolls back) when a ``gone`` link is no longer served, or the
    counts per link type differ from the stage. Returns the bit's report."""
    from ontology import store_ddl
    cols = ", ".join(columns or store_ddl.LINK_COLUMNS)
    t = time.monotonic()
    cur.execute(f"SELECT link_type, count(*) FROM {s}.links WHERE step = %s AND "
                "part = %s GROUP BY 1", (step, part))
    to_insert = dict(cur.fetchall())
    cur.execute(f"INSERT INTO {s}.undo_deleted SELECT l.*, g.step, g.part FROM "
                f"ontology.link_rows l JOIN {s}.gone g ON g.link_id = l.link_id "
                "WHERE g.step = %s AND g.part = %s", (step, part))
    cur.execute(f"DELETE FROM ontology.link_rows l USING {s}.gone g WHERE "
                "g.step = %s AND g.part = %s AND l.link_id = g.link_id "
                "RETURNING l.link_type", (step, part))
    deleted: dict[str, int] = {}
    for (t_,) in cur.fetchall():
        deleted[t_] = deleted.get(t_, 0) + 1
    if sum(deleted.values()) != int(rep["gone"]):
        raise StageRefused(f"gone_not_served:{step}:{part}:"
                           f"{sum(deleted.values())}:{rep['gone']}")
    cur.execute(f"INSERT INTO ontology.link_rows ({cols}) SELECT {cols} FROM "
                f"{s}.links WHERE step = %s AND part = %s ORDER BY ord "
                "RETURNING link_type", (step, part))
    inserted: dict[str, int] = {}
    for (t_,) in cur.fetchall():
        inserted[t_] = inserted.get(t_, 0) + 1
    if inserted != to_insert or sum(inserted.values()) != int(rep["written"]):
        raise StageRefused(f"inserted_differs:{step}:{part}")
    cur.execute(f"INSERT INTO {s}.undo_inserted (link_id, step, part) SELECT "
                f"link_id, step, part FROM {s}.links WHERE step = %s AND "
                "part = %s", (step, part))
    cur.execute(f"DELETE FROM {s}.links WHERE step = %s AND part = %s", (step, part))
    types = sorted(set(deleted) | set(inserted))
    bit = {"deleted": sum(deleted.values()), "inserted": sum(inserted.values()),
           "types": {t_: {"deleted": deleted.get(t_, 0),
                          "inserted": inserted.get(t_, 0)} for t_ in types},
           "seconds": round(time.monotonic() - t, 3)}
    cur.execute(f"INSERT INTO {s}.applied (step, part, staged, report) VALUES "
                "(%s, %s, %s, %s)", (step, part, json.dumps(rep, sort_keys=True),
                                     json.dumps(bit, sort_keys=True)))
    return bit


def undo(dsn: str, step: str, parts: Iterable[int] | None = None,
         s: str | None = None) -> dict[str, Any]:
    """An applied bit taken back (the latest first): its inserted links deleted, its
    deleted rows put back as they were, its marker removed (the stage of its links is
    gone: the step runs again for it)."""
    import psycopg
    from operating.pg.ontology_migrate import LOCK
    s = s or schema()
    out: dict[str, Any] = {}
    with psycopg.connect(dsn, application_name="plx-step-stage") as pg, pg.cursor() as cur:
        done = applied(cur, step, s)
        for part in sorted(done if parts is None else parts, reverse=True):
            if part not in done:
                continue
            cur.execute(LOCK)
            out[part] = undo_in(cur, s, step, part)
            pg.commit()
    return out


def undo_in(cur: Any, s: str, step: str, part: int) -> dict[str, int]:
    """One applied bit taken back in the caller's transaction (it holds the delta's
    lock): its inserted links deleted, its deleted rows put back as they were, its
    markers removed."""
    cur.execute(f"DELETE FROM ontology.link_rows l USING {s}.undo_inserted u WHERE "
                "u.step = %s AND u.part = %s AND l.link_id = u.link_id", (step, part))
    removed = cur.rowcount
    cur.execute("SELECT string_agg(quote_ident(column_name), ', ' ORDER BY "
                "ordinal_position) FROM information_schema.columns WHERE "
                "table_schema = 'ontology' AND table_name = 'link_rows'")
    raw = cur.fetchone()[0]
    cur.execute(f"INSERT INTO ontology.link_rows ({raw}) SELECT {raw} FROM "
                f"{s}.undo_deleted WHERE step = %s AND part = %s", (step, part))
    restored = cur.rowcount
    for table in ("undo_inserted", "undo_deleted", "applied", "staged", "gone"):
        cur.execute(f"DELETE FROM {s}.{table} WHERE step = %s AND part = %s", (step, part))
    return {"removed": removed, "restored": restored}


# ---------------------------------------------------------------- the apply's chunk

def apply_step(cur: Any, step: str, expected: Mapping[str, Any], write: bool,
               columns: Sequence[str]) -> dict[str, Any]:
    """One staged step applied in the caller's transaction: its ``gone`` links deleted and
    its ``links`` inserted (in part and store order). Refused when the markers differ from
    ``expected`` (the step layer's record of them) or a ``gone`` link is not served."""
    from operating.pg.ontology_migrate import Refused
    parts = staged(cur, step)
    got = totals(parts) if parts else None
    if got is None or any(got.get(k) != expected.get(k) for k in
                          ("parts", "rows", "written", "gone", "digest")):
        raise Refused(f"staged_differs:{step}")
    s = schema()
    out = {k: got[k] for k in ("rows", "same", "extractor_only", "written", "gone")}
    were = applied(cur, step)
    if were:
        # applied as it went (:func:`apply_now`): every part, as staged, or refused
        if set(were) != set(parts) or any(were[p] != parts[p] for p in parts):
            raise Refused(f"staged_applied_partly:{step}")
        return {**out, "applied_before": len(were)}
    if not write:
        return out
    cur.execute(f"DELETE FROM ontology.link_rows l USING {s}.gone g WHERE g.step = %s AND "
                "l.link_id = g.link_id", (step,))
    if cur.rowcount != got["gone"]:
        raise Refused(f"staged_gone_not_served:{step}:{cur.rowcount}:{got['gone']}")
    cols = ", ".join(columns)
    cur.execute(f"INSERT INTO ontology.link_rows ({cols}) SELECT {cols} FROM {s}.links "
                "WHERE step = %s ORDER BY part, ord", (step,))
    if cur.rowcount != got["written"]:
        raise Refused(f"staged_written:{step}:{cur.rowcount}:{got['written']}")
    out["deleted"] = got["gone"]
    return out


def drop(dsn: str, migration: str) -> dict[str, Any]:
    """The stage schema dropped once the steps migration ``migration`` is recorded applied
    (refused before: the apply would need it)."""
    import psycopg
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        cur.execute("SELECT 1 FROM ontology.schema_migrations WHERE id = %s",
                    ("steps:" + migration,))
        if cur.fetchone() is None:
            raise StageRefused(f"not_applied:{migration}")
        cur.execute(f"DROP SCHEMA IF EXISTS {schema()} CASCADE")
    return {"dropped": schema(), "migration": migration}


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys
    from operating.pg import config
    p = argparse.ArgumentParser(prog="python3 -m operating.pg.step_staging")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("drop", help="drop the stage schema after the steps apply")
    d.add_argument("--migration", required=True, help="the step layer's record id")
    d.add_argument("--dsn", default=None)
    st = sub.add_parser("stage", help="stage one part of a parted step (its store)")
    st.add_argument("--run-key", required=True)
    st.add_argument("--step", required=True)
    st.add_argument("--part", type=int, required=True)
    st.add_argument("--store", required=True)
    st.add_argument("--scope", required=True, help="the part's scope JSON (objects)")
    st.add_argument("--dsn", default=None)
    args = p.parse_args(argv)
    dsn = args.dsn or config.dsn()
    if args.command == "drop":
        print(json.dumps(drop(dsn, args.migration)))
        return 0
    from ontology import step_migrate as sm
    scope = json.loads(Path(args.scope).read_text())
    out = stage(dsn, args.run_key, args.step, args.part, Path(args.store),
                sm._owned(args.step, None, sm.STEPS), scope["objects"])
    print(json.dumps(out, sort_keys=True))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
