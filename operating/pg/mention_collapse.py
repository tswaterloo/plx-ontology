"""CC-C-04 physical collapse: a document's repeated mention rows of one link folded into one
row in place, set-based, per link type and ``from_object`` range, with an exact undo.

    python3 -m operating.pg.mention_collapse plan   [--dsn D] [--rows 20000]  # batches, no writes
    python3 -m operating.pg.mention_collapse run    [--dsn D] [--workers 12] [--budget-minutes 110]
                                                    [--dry-run] [--types T,..]
    python3 -m operating.pg.mention_collapse verify [--dsn D]   # every done batch against its undo
    python3 -m operating.pg.mention_collapse undo   [--dsn D] [--marks M,..]
    python3 -m operating.pg.mention_collapse train  --plan PLAN.json [run's options]
    python3 -m operating.pg.mention_collapse markers [--dsn D]  # the repeats markers again
    python3 -m operating.pg.mention_collapse retire [--dsn D] [--min-days 14]  # drop the undo

``train`` is a train's step for a plan naming steps under ``"collapsed"``
(``ontology.step_migrate.COLLAPSED``): nothing when the plan names none; refused unless the
served step code is the plan's ``steps_to`` (the readers that read occurrences back are the
served ones); then ``run``, ``verify`` and the ``ontology.mention_repeats`` markers of every
folded document marked again (``operating.pg.mention_repeats refresh``: a deleted twin's
marker is stale). ``undo`` marks the documents it puts back again the same way.

``ontology/mentions.py`` writes one live ``mention:`` row per occurrence of an identifier in a
document; a mail naming an order in its subject and twice in its body holds three live rows of
one (link type, mail, order, mail version) link. This folds them, the rule of
``ontology.mention_occurrences``:

* **Key**: link type, ``from_object``, ``to_object``, the document version (the first evidence
  handle) and the id's suffix past the 40-character ``mention:<digest>`` (``#held``, the
  candidate piece, a review piece's ``@<from>``). Live rows only (``superseded_at`` null),
  binding kind ``mention``.
* **Keeper**: the key's strongest tier (:data:`TIER_ORDER`, the read side's), then least id
  (code point order): the row ``operating.pg.mention_repeats`` and ``plx.object`` keep.
* **Twin**: another row of the key whose tier, markings, evidence handles, contract and
  recorded time equal the keeper's, neither reviewed, holding no folded twins itself. A row
  differing in any of those stays its own row (the read-side collapse still shows it once).
* **Fold**: the keeper keeps its id and every column; its reason gains the feature
  ``identifier_occurrences`` (appended last) holding each twin's occurrence digest, reason
  text and why_not text; the twins are deleted. The readers that judge occurrences
  (``unique_mentions``, ``task_ids``) read every occurrence back
  (``mention_occurrences.views``), so their firm results are those of the uncollapsed rows.
* **Remap** of the firm rows a member cites (``crb-mid:`` / ``crb-mcid:``, id = prefix +
  digest of the lead id, same link type and ends, citing the lead last as ``corroborates``):
  per prefix, the live firm row of the key's least member holding one is the keeper's: the
  keeper's own is kept as it is; a twin's moves to the keeper's id with only its
  ``corroborates`` link id rewritten (what the occurrence-aware step writes from that twin's
  occurrence); every other member's firm row is deleted. A key whose firm rows do not have
  the step's shape, or whose keeper id another row of the type already holds, is not folded.
* **Batches**: per link type, per ``from_object`` range; each is one transaction holding the
  delta's lock shared (the delta's apply takes it exclusive: no layer applies mid-batch),
  marked done in ``ontology.load_state`` (``c04:<type>:<n>``) so a run resumes and a re-run
  folds nothing twice; the batches run side by side, a run stops starting them after its
  budget.
* **Undo**: before any write a batch copies the whole physical row of every row it updates or
  deletes into ``ontology.mention_collapse_undo`` (``to_jsonb``: ``row_no``, packed slots and
  all) and, after its writes, the logical row (``ontology.links``) of every row it leaves
  (``post``: the keeper and its firm rows), each tagged with its key. ``undo`` goes key by
  key: a key whose live rows are still its ``post`` rows, its deleted ids still absent, is
  put back exactly; any other key (a delta layer replaced its rows after capture resumed, a
  purge deleted them) is skipped and counted, never revived. A batch undone whole drops its
  mark. ``ontology.mail_exclusion_purge`` deletes every key touching an excluded record
  from the undo table. ``retire`` drops the table once the newest batch is ``--min-days``
  (14) old: no undo after that.

Packed rows (``reason_id``/``slots``) are read through the ``ontology.links`` view; a row the
batch rewrites gets its reason as text (``reason_id`` null) and keeps its evidence slots.
Nothing outside ``ontology.link_rows`` and the two bookkeeping tables is written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable, Mapping, Sequence

LOCK = "SELECT pg_advisory_xact_lock_shared(hashtext('ontology_delta'))"
MARK = "c04:"
PLAN_MARK = "c04-plan"
LEAD_LO, LEAD_HI = "mention:", "mention;"
FIRM_PREFIXES = ("crb-mid:", "crb-mcid:")
# the read side's tier order (operating/ontology_tools.py TIER_ORDER)
TIER_ORDER = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead",
              "contradicted")
TAIL = ',{"feature":"identifier_occurrences","occurrences":['
# a lead id whose JSON text is '"' + id + '"' (so its firm digest is sha256('["<id>"]'))
SAFE_ID = r'^[ !#-\[\]-~]+$'

UNDO = "ontology.mention_collapse_undo"
RETIRED = "c04-retired"
RETIRE_DAYS = 14.0
# keeper: the key a row belongs to (undo goes key by key); from/to: the key's ends (the
# exclusion purge drops every key touching an excluded record without reading the images)
# undo_id: every ontology/search table needs a primary key, or migrate_chunks refuses the
# undo log of every later train (undo_needs_primary_key; train 5 stop 11-15). A store made
# before it gets the column (the served store got the same column by hand on 11-15).
UNDO_DDL = (f"CREATE TABLE IF NOT EXISTS {UNDO} (undo_id bigserial PRIMARY KEY, "
            "mark text NOT NULL, action text NOT NULL, "
            "link_id text NOT NULL, link_type text NOT NULL, image jsonb, keeper text, "
            "from_object text, to_object text)",
            f"ALTER TABLE {UNDO} ADD COLUMN IF NOT EXISTS keeper text",
            f"ALTER TABLE {UNDO} ADD COLUMN IF NOT EXISTS from_object text",
            f"ALTER TABLE {UNDO} ADD COLUMN IF NOT EXISTS to_object text",
            f"ALTER TABLE {UNDO} ADD COLUMN IF NOT EXISTS undo_id bigserial PRIMARY KEY")
UNDO_INDEX = (f"CREATE INDEX IF NOT EXISTS mention_collapse_undo_mark ON {UNDO} (mark)")

# ---------------------------------------------------------------------- one batch, set-based
STAGE_SQL = """
CREATE TEMP TABLE c04_m ON COMMIT DROP AS
SELECT v.link_id, v.from_object, v.to_object, v.tier, v.link_contract, v.reason, v.why_not,
       v.evidence_handles, v.markings, v.recorded_at,
       (v.reviewed_by IS NULL AND v.reviewed_at IS NULL AND v.review_action_id IS NULL)
           AS unreviewed
  FROM ontology.links v
 WHERE v.link_type = %(t)s AND v.from_object >= %(lo)s
   AND (%(hi)s::text IS NULL OR v.from_object < %(hi)s::text)
   AND left(v.link_id, 8) = 'mention:' AND v.superseded_at IS NULL
   AND v.binding_kind = 'mention';
CREATE TEMP TABLE c04_k ON COMMIT DROP AS
SELECT m.*, first_value(m.link_id) OVER (
           PARTITION BY m.from_object, m.to_object,
                        CASE WHEN m.evidence_handles LIKE '[%%' THEN
                             m.evidence_handles::jsonb ->> 0 END,
                        substr(m.link_id, 41)
           ORDER BY coalesce(array_position(%(tiers)s::text[], m.tier), 99),
                    m.link_id COLLATE "C") AS keeper
  FROM c04_m m;
CREATE INDEX ON c04_m (link_id);
ANALYZE c04_m;
ANALYZE c04_k;
CREATE TEMP TABLE c04_t ON COMMIT DROP AS
SELECT t.link_id AS twin, t.keeper, t.reason, t.why_not
  FROM c04_k t JOIN c04_m k ON k.link_id = t.keeper
 WHERE t.link_id <> t.keeper
   AND t.tier = k.tier AND t.markings = k.markings
   AND t.evidence_handles IS NOT DISTINCT FROM k.evidence_handles
   AND t.evidence_handles LIKE '[%%'
   AND t.link_contract IS NOT DISTINCT FROM k.link_contract
   AND t.recorded_at = k.recorded_at AND t.unreviewed AND k.unreviewed
   AND t.reason IS NOT NULL AND t.why_not IS NOT NULL
   AND k.reason LIKE '[_%%]' AND strpos(t.reason, %(tail)s) = 0
   AND strpos(k.reason, %(tail)s) = 0
   AND t.link_id ~ %(safe)s AND k.link_id ~ %(safe)s;
CREATE TEMP TABLE c04_f ON COMMIT DROP AS
SELECT p.prefix, x.member, x.keeper, f.link_id AS firm_id, f.reason AS firm_reason
  FROM (SELECT DISTINCT keeper AS member, keeper FROM c04_t
        UNION ALL SELECT twin, keeper FROM c04_t) x
 CROSS JOIN unnest(%(prefixes)s::text[]) AS p(prefix)
  JOIN ontology.links f ON f.link_type = %(t)s AND f.superseded_at IS NULL
   AND f.link_id = p.prefix || left(encode(sha256(convert_to('["' || x.member || '"]',
                                                             'UTF8')), 'hex'), 32);
-- a key whose firm rows are not the step's shape is left whole
DELETE FROM c04_t WHERE keeper IN (
    SELECT keeper FROM c04_f
     WHERE firm_reason IS NULL OR right(firm_reason, length(
               '{"agrees":true,"binding_kind":"mention","feature":"corroborates","link_id":"'
               || member || '"}]')) <> '{"agrees":true,"binding_kind":"mention",'
               || '"feature":"corroborates","link_id":"' || member || '"}]');
DELETE FROM c04_f WHERE keeper NOT IN (SELECT keeper FROM c04_t);
CREATE TEMP TABLE c04_pick ON COMMIT DROP AS
SELECT DISTINCT ON (prefix, keeper) prefix, keeper, member, firm_id, firm_reason,
       prefix || left(encode(sha256(convert_to('["' || keeper || '"]', 'UTF8')), 'hex'), 32)
           AS keeper_firm
  FROM c04_f ORDER BY prefix, keeper, member COLLATE "C";
-- a moved firm row may not land on an id the type already holds (history included)
DELETE FROM c04_t WHERE keeper IN (
    SELECT p.keeper FROM c04_pick p JOIN ontology.link_rows r
        ON r.link_type = %(t)s AND r.link_id = p.keeper_firm
     WHERE p.member <> p.keeper);
DELETE FROM c04_f WHERE keeper NOT IN (SELECT keeper FROM c04_t);
DELETE FROM c04_pick WHERE keeper NOT IN (SELECT keeper FROM c04_t);
"""

COUNT_SQL = """
SELECT (SELECT count(*) FROM c04_m), (SELECT count(DISTINCT keeper) FROM c04_t),
       (SELECT count(*) FROM c04_t), (SELECT count(*) FROM c04_f),
       (SELECT count(*) FROM c04_pick WHERE member <> keeper),
       (SELECT count(*) FROM c04_k WHERE link_id <> keeper)
"""

UNDO_SQL = """
INSERT INTO ontology.mention_collapse_undo (mark, action, link_id, link_type, image, keeper,
                                            from_object, to_object)
SELECT %(mark)s, 'restore', r.link_id, r.link_type, to_jsonb(r), x.keeper, r.from_object,
       r.to_object
  FROM ontology.link_rows r
  JOIN (SELECT keeper AS id, keeper FROM c04_t UNION SELECT twin, keeper FROM c04_t
        UNION SELECT firm_id, keeper FROM c04_f) x ON x.id = r.link_id
 WHERE r.link_type = %(t)s;
"""

# what the batch leaves, logically (the links view: a later repacking keeps it equal): the
# keeper and its firm rows; an undo restores a key only while its rows are still these
POST_SQL = """
INSERT INTO ontology.mention_collapse_undo (mark, action, link_id, link_type, image, keeper,
                                            from_object, to_object)
SELECT %(mark)s, 'post', v.link_id, v.link_type, to_jsonb(v), x.keeper, v.from_object,
       v.to_object
  FROM ontology.links v
  JOIN (SELECT DISTINCT keeper AS id, keeper FROM c04_t
        UNION SELECT keeper_firm, keeper FROM c04_pick) x ON x.id = v.link_id
 WHERE v.link_type = %(t)s;
"""

APPLY_SQL = """
INSERT INTO ontology.link_rows ({cols})
SELECT {moved}
  FROM c04_pick p JOIN ontology.link_rows r ON r.link_type = %(t)s AND r.link_id = p.firm_id
 WHERE p.member <> p.keeper;
DELETE FROM ontology.link_rows WHERE link_type = %(t)s AND link_id IN (
    SELECT firm_id FROM c04_f WHERE member <> keeper);
UPDATE ontology.link_rows r
   SET reason = left(k.reason, -1) || %(tail)s || o.occurrences || ']}}]',
       reason_id = NULL, slots = r.slots[1:r.ek]
  FROM (SELECT keeper, string_agg('{{"occurrence":' || to_json(substr(twin, 9, 32))::text
                                  || ',"reason":' || to_json(reason)::text
                                  || ',"why_not":' || to_json(why_not)::text || '}}',
                                  ',' ORDER BY twin COLLATE "C") AS occurrences
          FROM c04_t GROUP BY keeper) o
  JOIN c04_m k ON k.link_id = o.keeper
 WHERE r.link_type = %(t)s AND r.link_id = o.keeper;
DELETE FROM ontology.link_rows WHERE link_type = %(t)s AND link_id IN (SELECT twin FROM c04_t);
"""


def _columns(conn: Any) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = 'ontology.link_rows'::regclass "
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum").fetchall()]


def _apply_sql(conn: Any) -> str:
    cols = [c for c in _columns(conn) if c != "row_no"]
    moved = []
    for c in cols:
        if c == "link_id":
            moved.append("p.keeper_firm")
        elif c == "reason":
            moved.append("left(p.firm_reason, length(p.firm_reason) - length(p.member) - 3)"
                         " || p.keeper || '\"}]'")
        elif c == "reason_id":
            moved.append("NULL")
        elif c == "slots":
            moved.append("r.slots[1:r.ek]")
        else:
            moved.append("r." + c)
    return APPLY_SQL.format(cols=", ".join(cols), moved=", ".join(moved))


def ensure(conn: Any) -> None:
    for statement in UNDO_DDL:
        conn.execute(statement)
    conn.execute(UNDO_INDEX)


def _undo_present(conn: Any) -> bool:
    return conn.execute("SELECT to_regclass(%s)", (UNDO,)).fetchone()[0] is not None


def _params(batch: Sequence[Any], mark: str = "") -> dict[str, Any]:
    link_type, _n, lo, hi = batch
    return {"t": link_type, "lo": lo, "hi": hi, "tiers": list(TIER_ORDER), "tail": TAIL,
            "safe": SAFE_ID, "prefixes": list(FIRM_PREFIXES), "mark": mark}


def run_batch(dsn: str, batch: Sequence[Any], dry: bool = False) -> dict[str, Any]:
    """Fold one batch (module docstring) in one transaction; ``dry``: count, roll back."""
    import psycopg
    link_type, n, _lo, _hi = batch
    mark = f"{MARK}{link_type}:{n}"
    started = time.monotonic()
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(LOCK)
            if conn.execute("SELECT 1 FROM ontology.load_state WHERE name = %s",
                            (mark,)).fetchone():
                return {"batch": mark, "done_before": True}
            params = _params(batch, mark)
            conn.execute("SET LOCAL work_mem = '256MB'")
            for statement in _split(STAGE_SQL):
                conn.execute(statement, params)
            rows, keepers, twins, firms, moved, key_repeats = conn.execute(COUNT_SQL).fetchone()
            out = {"batch": mark, "live_mention_rows": rows, "keepers": keepers,
                   "twins_deleted": twins, "firm_rows_of_members": firms,
                   "firm_rows_moved": moved, "repeats_of_any_kind": key_repeats}
            if dry:
                raise _DryRun(out)   # leaves the transaction: rolled back
            for statement in _split(UNDO_SQL):
                conn.execute(statement, params)
            for statement in _split(_apply_sql(conn)):
                conn.execute(statement, params)
            conn.execute(POST_SQL, params)
            conn.execute("INSERT INTO ontology.load_state (name, value) VALUES (%s, %s)",
                         (mark, json.dumps(out)))
    out["seconds"] = round(time.monotonic() - started, 2)
    return out


class _DryRun(Exception):
    def __init__(self, out: dict[str, Any]) -> None:
        super().__init__("dry run")
        self.out = out


def _dry(dsn: str, batch: Sequence[Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        return run_batch(dsn, batch, dry=True)
    except _DryRun as done:   # the transaction rolled back
        done.out["seconds"] = round(time.monotonic() - started, 2)
        done.out["dry_run"] = True
        return done.out


def _split(sql: str) -> list[str]:
    out, current = [], []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            out.append("\n".join(current).strip().rstrip(";"))
            current = []
    if "".join(current).strip():
        out.append("\n".join(current).strip())
    return [s for s in out if s]


# ------------------------------------------------------------------------------ the plan
def make_plan(conn: Any, rows_per_batch: int = 20000, sample: float = 1.0,
              types: Iterable[str] | None = None) -> list[tuple[str, int, str, str | None]]:
    """(link type, n, lo, hi) per mention link type and ``from_object`` range of about
    ``rows_per_batch`` live mention rows, cut from a ``sample`` percent block sample."""
    want = set(types or ())
    counts: dict[str, int] = {}
    cuts: dict[str, list[str]] = {}
    # ordered by Postgres (the column's collation), the order the batches' ranges compare in
    for link_type, from_object in conn.execute(
            f"SELECT link_type, from_object FROM ontology.link_rows TABLESAMPLE SYSTEM "
            f"({float(sample)}) REPEATABLE (7) WHERE left(link_id, 8) = %s AND "
            "superseded_at IS NULL ORDER BY link_type, from_object", (LEAD_LO,)):
        if want and link_type not in want:
            continue
        counts[link_type] = counts.get(link_type, 0) + 1
        cuts.setdefault(link_type, []).append(from_object)
    out: list[tuple[str, int, str, str | None]] = []
    for link_type in sorted(cuts):
        values = cuts[link_type]
        estimated = counts[link_type] * 100.0 / float(sample)
        parts = max(1, int(round(estimated / rows_per_batch)))
        step = max(1, len(values) // parts)
        edges = [""]
        for value in values[step::step]:
            if value != edges[-1]:
                edges.append(value)
        for n, lo in enumerate(edges):
            out.append((link_type, n, lo, edges[n + 1] if n + 1 < len(edges) else None))
    return out


def saved_plan(conn: Any, make: Any) -> list[tuple[str, int, str, str | None]]:
    """The plan the first run saved (``ontology.load_state`` ``c04-plan``), else ``make()``'s,
    saved now: a re-run reads the same ranges, so a done mark names the rows it folded."""
    row = conn.execute("SELECT value FROM ontology.load_state WHERE name = %s",
                       (PLAN_MARK,)).fetchone()
    if row:
        saved = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else row[0]
        return [tuple(b) for b in saved]  # type: ignore[misc]
    made = make()
    with conn.transaction():
        conn.execute("INSERT INTO ontology.load_state (name, value) VALUES (%s, %s)",
                     (PLAN_MARK, json.dumps(made)))
    return made


def schema_mention_types() -> set[str]:
    """The schema's link types with a ``mention`` binding (the mentions step's)."""
    from ontology.schema import load_schema
    schema = load_schema()
    types = schema.get("link_types") if isinstance(schema, dict) else \
        getattr(schema, "link_types", None)
    return {lid for lid, link in (types or {}).items()
            if any(b.get("kind") == "mention" for b in link.get("bindings", ()))}


def missing_types(plan: Sequence[Sequence[Any]], types: Iterable[str] | None = None
                  ) -> list[str]:
    """Mention link types (the schema's, and ``types``) no batch of ``plan`` covers (a type
    the block sample missed): the run covers each with one whole-type batch."""
    planned = {b[0] for b in plan}
    wanted = set(types) if types else schema_mention_types()
    return sorted(wanted - planned)


def run(dsn: str, workers: int = 12, budget_minutes: float = 110.0, dry: bool = False,
        types: Sequence[str] | None = None, rows_per_batch: int = 20000,
        log: Any = None) -> dict[str, Any]:
    """Every batch of the saved plan (module docstring), ``workers`` side by side."""
    import psycopg
    say = log or (lambda m: print(m, file=sys.stderr, flush=True))
    started = time.monotonic()
    with psycopg.connect(dsn, autocommit=True) as conn:
        ensure(conn)
        if dry:
            plan = make_plan(conn, rows_per_batch, types=types)
        else:
            plan = saved_plan(conn, lambda: make_plan(conn, rows_per_batch))
        extra = missing_types(plan, types)   # a type the sample missed: one whole batch
        if extra:
            plan = plan + [(t, 10_000, "", None) for t in extra]
    if types:
        plan = [b for b in plan if b[0] in set(types)]
    say(f"mention_collapse: {len(plan)} batches, {workers} workers, dry={dry}")
    totals: dict[str, float] = {}
    done, skipped, failed = 0, 0, []
    stop_at = started + budget_minutes * 60
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending = {}
        queue = list(plan)
        def submit() -> None:
            while queue and len(pending) < workers and time.monotonic() < stop_at:
                b = queue.pop(0)
                pending[pool.submit(_dry if dry else run_batch, dsn, b)] = b
        submit()
        while pending:
            for fut in as_completed(list(pending)):
                b = pending.pop(fut)
                try:
                    out = fut.result()
                except Exception as exc:  # noqa: BLE001 - the batch rolled back; reported
                    failed.append(f"{b[0]}:{b[1]}:{type(exc).__name__}:{exc}"[:300])
                    say(f"FAILED {b[0]}:{b[1]} {exc}")
                else:
                    if out.get("done_before"):
                        skipped += 1
                    else:
                        done += 1
                        for k, v in out.items():
                            if isinstance(v, (int, float)) and not isinstance(v, bool):
                                totals[k] = totals.get(k, 0) + v
                submit()
                break
    left = len(queue)
    out = {"batches": len(plan), "done": done, "done_before": skipped, "failed": failed,
           "not_started_budget": left, "totals": totals,
           "wall_seconds": round(time.monotonic() - started, 1), "dry_run": dry}
    say(json.dumps(out, sort_keys=True))
    return out


# ------------------------------------------------------------------------------ undo
# the keys of one mark whose rows are no longer what the batch left: a 'post' row gone or
# changed (a layer replaced the document's or target's rows, a purge deleted them, a
# review), or a deleted row's id live again (a re-derived twin or firm row)
_STALE_SQL = """
CREATE TEMP TABLE c04_stale ON COMMIT DROP AS
SELECT DISTINCT u.keeper FROM ontology.mention_collapse_undo u
  LEFT JOIN ontology.links v ON v.link_type = u.link_type AND v.link_id = u.link_id
 WHERE u.mark = %(mark)s AND u.action = 'post'
   AND (v.link_id IS NULL OR to_jsonb(v) <> u.image)
UNION
SELECT u.keeper FROM ontology.mention_collapse_undo u
  JOIN ontology.link_rows r ON r.link_type = u.link_type AND r.link_id = u.link_id
 WHERE u.mark = %(mark)s AND u.action = 'restore' AND NOT EXISTS (
       SELECT 1 FROM ontology.mention_collapse_undo p WHERE p.mark = u.mark
          AND p.action = 'post' AND p.link_id = u.link_id AND p.link_type = u.link_type)
UNION
SELECT u.keeper FROM ontology.mention_collapse_undo u
 WHERE u.mark = %(mark)s AND (u.keeper IS NULL OR NOT EXISTS (
       SELECT 1 FROM ontology.mention_collapse_undo p WHERE p.mark = u.mark
          AND p.action = 'post' AND p.keeper = u.keeper))
"""


def undo(dsn: str, marks: Sequence[str] | None = None) -> dict[str, Any]:
    """Put back, exactly, every key of the done batches (``marks``: those only) whose live
    rows are still what the batch left (``post``: the keeper and its firm rows equal, every
    deleted twin and firm row still absent); one transaction per batch, under the delta's
    lock taken exclusive. Any other key is skipped and reported: capture moved on (a layer
    replaced its rows) or a purge removed them, and putting the old rows back would bring
    superseded or purged rows to life. A batch undone whole loses its mark; a batch with a
    skipped key keeps it and the skipped keys' images (``retire`` drops them)."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        if not _undo_present(conn):
            return {"undone": 0, "rows_restored": 0, "keys_restored": 0, "keys_skipped": 0,
                    "skipped": "no_undo_table"}
        ensure(conn)
        found = [r[0] for r in conn.execute(
            f"SELECT DISTINCT mark FROM {UNDO} ORDER BY 1").fetchall()]
        cols = _columns(conn)
    todo = [m for m in found if not marks or m in set(marks)]
    restored = keys_restored = keys_skipped = undone = 0
    partial: dict[str, int] = {}
    collist = ", ".join(cols)
    pcols = ", ".join("p." + c for c in cols)
    good = ("u.mark = %(mark)s AND u.keeper IS NOT NULL AND u.keeper NOT IN "
            "(SELECT keeper FROM c04_stale WHERE keeper IS NOT NULL)")
    for mark in todo:
        with psycopg.connect(dsn) as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))")
                params = {"mark": mark}
                conn.execute(_STALE_SQL, params)
                stale = conn.execute("SELECT count(*) FROM c04_stale").fetchone()[0]
                keys = conn.execute(
                    f"SELECT count(DISTINCT keeper) FROM {UNDO} u WHERE {good}",
                    params).fetchone()[0]
                conn.execute(
                    f"DELETE FROM ontology.link_rows r USING {UNDO} u WHERE {good} AND "
                    "u.action = 'post' AND r.link_id = u.link_id AND r.link_type = u.link_type",
                    params)
                cur = conn.execute(
                    f"INSERT INTO ontology.link_rows ({collist}) SELECT {pcols} FROM {UNDO} u, "
                    "LATERAL jsonb_populate_record(NULL::ontology.link_rows, u.image) p "
                    f"WHERE {good} AND u.action = 'restore'", params)
                restored += cur.rowcount
                conn.execute(f"DELETE FROM {UNDO} u WHERE {good}", params)
                keys_restored += keys
                keys_skipped += stale
                if stale:
                    partial[mark] = stale
                else:
                    conn.execute(f"DELETE FROM {UNDO} WHERE mark = %s", (mark,))
                    conn.execute("DELETE FROM ontology.load_state WHERE name = %s", (mark,))
                    undone += 1
    if not marks and not partial:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM ontology.load_state WHERE name = %s", (PLAN_MARK,))
    return {"undone": undone, "rows_restored": restored, "keys_restored": keys_restored,
            "keys_skipped": keys_skipped, "marks_kept": partial}


def retire(dsn: str, min_days: float = RETIRE_DAYS, now: Any = None) -> dict[str, Any]:
    """Drop the undo table once the newest collapse batch is ``min_days`` old (the
    acceptance window: no undo is wanted after it); refused before. The batches' marks stay
    (a re-run folds nothing twice); ``c04-retired`` records the drop."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        if not _undo_present(conn):
            return {"ok": True, "retired": False, "skipped": "no_undo_table"}
        row = conn.execute(
            f"SELECT count(*), max(s.updated_at), coalesce(%s::timestamptz, now()) FROM "
            f"(SELECT DISTINCT mark FROM {UNDO}) u JOIN ontology.load_state s ON s.name = u.mark",
            (now,)).fetchone()
        n, newest, at = row
        rows = conn.execute(f"SELECT count(*) FROM {UNDO}").fetchone()[0]
        if newest is not None and (at - newest).total_seconds() < min_days * 86400:
            return {"ok": False, "retired": False, "refused": "inside_window",
                    "newest_batch": str(newest), "min_days": min_days, "undo_rows": rows}
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))")
            conn.execute(f"DROP TABLE {UNDO}")
            conn.execute(
                "INSERT INTO ontology.load_state (name, value) VALUES (%s, %s) ON CONFLICT "
                "(name) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (RETIRED, json.dumps({"marks": n, "undo_rows": rows,
                                      "newest_batch": str(newest)})))
    return {"ok": True, "retired": True, "marks": n, "undo_rows": rows,
            "newest_batch": str(newest)}


# ------------------------------------------------------------------------------ verify
def verify(dsn: str, marks: Sequence[str] | None = None, limit: int | None = None
           ) -> dict[str, Any]:
    """Every done batch against its undo images (SAME_ROWS-style, on the stored rows):

    * each deleted twin is a view of its live keeper (``mention_occurrences.views``): every
      column equal but the id, its reason and why_not byte for byte;
    * each keeper's own view is its row before, byte for byte;
    * per firm prefix, a keeper holds a live firm row iff some member held one before, and
      it is the least such member's under the keeper's id (only ``corroborates`` rewritten);
    * no live firm row cites a deleted twin.
    """
    import psycopg
    from ontology import mention_occurrences as MO
    bad: list[str] = []
    seen = {"twins": 0, "keepers": 0, "firm_kept": 0, "firm_moved": 0}
    with psycopg.connect(dsn, autocommit=True) as conn:
        found = [r[0] for r in conn.execute(
            f"SELECT DISTINCT mark FROM {UNDO} ORDER BY 1").fetchall()] \
            if _undo_present(conn) else []
        for mark in found:
            if marks and mark not in set(marks):
                continue
            images = {(r[0], r[1]): r[2] for r in conn.execute(
                "SELECT link_id, link_type, image FROM ontology.mention_collapse_undo "
                "WHERE mark = %s AND action = 'restore'", (mark,))}
            ids = sorted({lid for lid, _t in images})
            live = {(r[0], r[1]): dict(zip(("link_id", "link_type", "from_object",
                                            "to_object", "tier", "reason", "why_not",
                                            "evidence_handles", "markings", "recorded_at",
                                            "superseded_at", "link_contract"), r))
                    for r in conn.execute(
                        "SELECT link_id, link_type, from_object, to_object, tier, reason, "
                        "why_not, evidence_handles, markings, recorded_at, superseded_at, "
                        "link_contract FROM ontology.links WHERE link_id = ANY(%s)", (ids,))}
            before = {}
            for (lid, lt), img in images.items():
                before[(lid, lt)] = _logical(conn, img)
            _check_mark(mark, before, live, conn, bad, seen, MO)
            if limit is not None and len(bad) >= limit:
                break
    return {"ok": not bad, "problems": bad[:50], "problem_count": len(bad), **seen}


def _logical(conn: Any, image: Mapping[str, Any]) -> dict[str, Any]:
    """An undo image's logical row (its packed reason / evidence read back as text)."""
    row = dict(image)
    for col, ident, lo, hi in (("reason", "reason_id", row.get("ek", 0) + 1, 64),
                               ("evidence_handles", "evidence_id", 1, row.get("ek", 0))):
        if row.get(col) is None and row.get(ident) is not None:
            row[col] = conn.execute("SELECT ontology.link_text(%s, %s::bigint[], %s, %s)",
                                    (row[ident], row.get("slots") or [], lo, hi)).fetchone()[0]
    return row


_SAME = ("link_type", "from_object", "to_object", "tier", "evidence_handles", "markings",
         "recorded_at", "superseded_at", "link_contract")


def _check_mark(mark: str, before: Mapping[tuple[str, str], Mapping[str, Any]],
                live: Mapping[tuple[str, str], Mapping[str, Any]], conn: Any, bad: list[str],
                seen: dict[str, int], MO: Any) -> None:
    import hashlib
    mention = {k: v for k, v in before.items() if k[0].startswith(LEAD_LO)}
    firm_before = {k: v for k, v in before.items() if not k[0].startswith(LEAD_LO)}
    keepers = {k: v for k, v in mention.items() if k in live}
    gone = {k: v for k, v in mention.items() if k not in live}
    views: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, row in keepers.items():
        seen["keepers"] += 1
        vs = list(MO.views([live[key]]))
        views[key] = vs
        own = vs[0]
        if own["reason"] != row["reason"] or own["why_not"] != row["why_not"] or any(
                live[key][c] != row[c] for c in _SAME):
            bad.append(f"{mark}:keeper_changed:{key[0]}")
    by_occ = {(k[1], k[0][len(LEAD_LO) + 32:], v[MO.OCCURRENCE]): (k, v)
              for k, vs in views.items() for v in vs[1:]}
    member_of: dict[str, str] = {}
    for key, row in gone.items():
        seen["twins"] += 1
        hit = by_occ.get((key[1], key[0][len(LEAD_LO) + 32:], MO.occurrence(key[0])))
        if hit is None:
            bad.append(f"{mark}:twin_without_keeper:{key[0]}")
            continue
        keeper_key, view = hit
        member_of[key[0]] = keeper_key[0]
        if view["reason"] != row["reason"] or view["why_not"] != row["why_not"] or any(
                view[c] != row[c] for c in _SAME):
            bad.append(f"{mark}:twin_not_its_view:{key[0]}")
    for key in keepers:
        member_of[key[0]] = key[0]

    def firm_id(prefix: str, lead: str) -> str:
        return prefix + hashlib.sha256(json.dumps([lead]).encode()).hexdigest()[:32]

    by_keeper: dict[tuple[str, str], list[str]] = {}
    for member, keeper in member_of.items():
        for prefix in FIRM_PREFIXES:
            by_keeper.setdefault((prefix, keeper), []).append(member)
    lt = next(iter(before))[1] if before else None
    for (prefix, keeper), members in by_keeper.items():
        held = sorted(m for m in members if (firm_id(prefix, m), lt) in firm_before)
        now = conn.execute("SELECT reason FROM ontology.links WHERE link_id = %s AND "
                           "link_type = %s AND superseded_at IS NULL",
                           (firm_id(prefix, keeper), lt)).fetchone()
        if not held:
            if now is not None and (firm_id(prefix, keeper), lt) not in firm_before:
                bad.append(f"{mark}:firm_appeared:{keeper}")
            continue
        if now is None:
            bad.append(f"{mark}:firm_lost:{prefix}{keeper}")
            continue
        old = firm_before[(firm_id(prefix, held[0]), lt)]["reason"]
        want = old if held[0] == keeper else \
            old[:len(old) - len(held[0]) - 3] + keeper + '"}]'
        if now[0] != want:
            bad.append(f"{mark}:firm_differs:{prefix}{keeper}")
        seen["firm_kept" if held[0] == keeper else "firm_moved"] += 1
        for m in members:
            if m != keeper and conn.execute(
                    "SELECT 1 FROM ontology.link_rows WHERE link_id = %s AND link_type = %s",
                    (firm_id(prefix, m), lt)).fetchone():
                bad.append(f"{mark}:twin_firm_left:{prefix}{m}")


# ------------------------------------------------------------------------- the train
def collapsed_objects(conn: Any, marks: Sequence[str] | None = None) -> list[str]:
    """The documents of every row the done batches (``marks``: those only) changed: a twin
    and its keeper share one ``from_object``, so these hold every folded pair."""
    if not _undo_present(conn):
        return []
    sql = ("SELECT DISTINCT image ->> 'from_object' FROM ontology.mention_collapse_undo "
           "WHERE action = 'restore' AND image ->> 'from_object' IS NOT NULL")
    params: tuple[Any, ...] = ()
    if marks:
        sql, params = sql + " AND mark = ANY(%s)", (list(marks),)
    return sorted(r[0] for r in conn.execute(sql, params).fetchall())


def refresh_markers(dsn: str, objects: Sequence[str]) -> dict[str, Any]:
    """``operating.pg.mention_repeats`` refresh of ``objects``: skipped when this tree has
    no marker, its table is absent or was never built (an empty table: a refresh would mark
    only these documents' pairs)."""
    if not objects:
        return {"objects": 0, "skipped": "no_objects"}
    try:
        from operating.pg import mention_repeats as MR
    except ImportError:
        return {"objects": len(objects), "skipped": "no_module"}
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SELECT to_regclass(%s)", (MR.TABLE,)).fetchone()[0] is None:
            return {"objects": len(objects), "skipped": "no_table"}
        if not conn.execute(f"SELECT EXISTS (SELECT 1 FROM {MR.TABLE})").fetchone()[0]:
            return {"objects": len(objects), "skipped": "not_built"}
    return MR.refresh(dsn, objects)


def _markers_ok(report: Mapping[str, Any]) -> bool:
    return report.get("skipped") in (None, "no_objects", "no_module", "no_table", "not_built")


def plan_collapsed(the_plan: Mapping[str, Any]) -> list[str]:
    """The steps a step migration plan has the collapse stand for (``"collapsed"``)."""
    return sorted((the_plan.get("steps") or {}).get("collapsed") or ())


def train(dsn: str, the_plan: Mapping[str, Any], workers: int = 12,
          budget_minutes: float = 110.0, rows_per_batch: int = 20000,
          log: Any = None) -> dict[str, Any]:
    """A train's collapse step (module docstring): ``{"ok": bool, ...}``; a run stopped by
    its budget or a failed batch is not ok and resumes on a re-run (done batches are kept;
    ``undo`` puts every one back)."""
    import psycopg
    from operating.pg.ontology_migrate import served_steps
    steps = plan_collapsed(the_plan)
    if not steps:
        return {"ok": True, "collapsed": [], "skipped": "not_in_plan"}
    want = the_plan.get("steps_to")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        served = served_steps(cur)
    if not want or served != want:
        return {"ok": False, "collapsed": steps, "refused": "steps_not_served",
                "served_steps": served, "steps_to": want}
    out: dict[str, Any] = {"collapsed": steps}
    out["run"] = run(dsn, workers, budget_minutes, False, None, rows_per_batch, log)
    if out["run"]["failed"] or out["run"]["not_started_budget"]:
        out["ok"] = False
        return out
    out["verify"] = verify(dsn)
    if not out["verify"]["ok"]:
        out["ok"] = False
        return out
    with psycopg.connect(dsn, autocommit=True) as conn:
        objects = collapsed_objects(conn)
    out["markers"] = refresh_markers(dsn, objects)
    out["markers_stale"] = not _markers_ok(out["markers"])
    out["ok"] = True
    return out


def _dsn() -> str:
    from operating.pg.config import dsn
    return dsn()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.mention_collapse")
    parser.add_argument("command", choices=("plan", "run", "verify", "undo", "train",
                                            "markers", "retire"))
    parser.add_argument("--min-days", type=float, default=RETIRE_DAYS,
                        help="retire: the newest batch's least age in days")
    parser.add_argument("--plan", default=None, help="train: the step migration plan JSON")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--budget-minutes", type=float, default=110.0)
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--types", default="")
    parser.add_argument("--marks", default="")
    args = parser.parse_args(argv)
    dsn = args.dsn or _dsn()
    types = [t for t in args.types.split(",") if t] or None
    if args.command == "plan":
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            plan = make_plan(conn, args.rows, types=types)
        print(json.dumps({"batches": len(plan), "types": len({b[0] for b in plan})}))
        return 0
    if args.command == "run":
        out = run(dsn, args.workers, args.budget_minutes, args.dry_run, types, args.rows)
        return 1 if out["failed"] else 0
    if args.command == "verify":
        out = verify(dsn, [m for m in args.marks.split(",") if m] or None)
        print(json.dumps(out, sort_keys=True))
        return 0 if out["ok"] else 1
    if args.command == "train":
        if not args.plan:
            parser.error("train needs --plan")
        with open(args.plan, encoding="utf-8") as fh:
            the_plan = json.load(fh)
        out = train(dsn, the_plan, args.workers, args.budget_minutes, args.rows)
        print(json.dumps(out, sort_keys=True, default=str))
        return 0 if out["ok"] else 1
    if args.command == "retire":
        out = retire(dsn, args.min_days)
        print(json.dumps(out, sort_keys=True))
        return 0 if out["ok"] else 1
    import psycopg
    marks = [m for m in args.marks.split(",") if m] or None
    with psycopg.connect(dsn, autocommit=True) as conn:
        objects = collapsed_objects(conn, marks)
    if args.command == "markers":
        out = refresh_markers(dsn, objects)
        print(json.dumps(out, sort_keys=True))
        return 0 if _markers_ok(out) else 1
    out = undo(dsn, marks)
    out["markers"] = refresh_markers(dsn, objects)   # the restored twins' pairs again
    print(json.dumps(out, sort_keys=True))
    return 1 if out["keys_skipped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
