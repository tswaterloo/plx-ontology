"""The precomputed plan sections of a record page: ``<biz>.plan_section_items``, one row per
(record, section) holding a question plan's answer (the late section's: ``late_po``), read by one
indexed statement through the page door (``page_reads`` ``plan_section``, ``<biz>_page.
plan_section_items``) so a purchase order's late, people and landed-cost sections are never cold.

WHERE IT RUNS. A plan runs the ontology tools (``plx.investigate``), seconds per record, so it is
never set-based SQL and never run on the box: ``operating.plan_batch`` runs on a Spark as the
owner's full reader against the s7d3d standby (``deploy/spark/spark_run.sh --pg standby``, the
release's own code), once per record, and writes one JSON line per answer. ``load`` (here, on the
box) reads that file, tags every answer with the access classes derived from the handles it
cites and swaps the table in, as ``record_pages.build`` does. A plan's answer is the same for any
reader who may open everything it cites (``kept_plan_run``: "the same alone as after the
others"), so one run as a reader who may read everything serves every reader who may open the
same sources, and only those:

* ``access_class`` is the order's own class (the record page's header row), so a reader who may
  not read the order reads no section of it;
* ``needs`` lists the access class of every cited business record (``records.access_class``):
  the guarded view serves the row only when the reader holds every one of them (as the alerts
  view does for the rows an alert was computed from);
* ``comm_handles`` lists the cited mail and attachment handles: their audiences are decided by
  the communications door per item, so the page asks the reader's own decision
  (``HostAccess.decide``, the one live ``plx.investigate`` marks ``openable`` with) for each
  before it serves the row;
* a citation of any other kind (no business record, no mail) is a source this table cannot
  decide: that answer is not stored (the page then reads the plan live).

A reader missing any of this reads the section live, never a reduced one. Lineage (the order's
records and evidence, every cited record, the objects of every cited mail) is kept so the people
exclusion drops or owner-restricts a row as it does the source (``door.exclude``).

  python3 -m operating.pg.plan_sections load FILE... [--schema S]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from typing import Any, Iterable, Mapping, Sequence

from operating.pg import business_views as bv

TABLE = "plan_section_items"
STATE_VIEW = "plan_section_items"
COLS = ("record_id", "section", "payload", "comm_handles", "built_at", "src_stamp", "src_handle")
# the order views the answers rest on: their build stamps are recorded with each row (for the
# record only: what decides whether a row is current is its own dependency set, below)
SOURCES = ("purchase_orders", "purchase_order_lines")
# the watermarks a runner reads at its START (``ontology.object_versions.ver``, the newest object
# version; ``ontology.link_rows.row_no``, the newest link row): a row is current while nothing in
# its dependency set has a version, and no link touching it a row, past them
VER_SQL = "SELECT COALESCE(max(ver), 0) FROM ontology.object_versions"
LINK_SQL = "SELECT COALESCE(max(row_no), 0) FROM ontology.link_rows"
# the oldest transaction still running when the runner started (``pg_snapshot_xmin``, as a 32-bit
# xid like ``xmin``): a version or link row written by a transaction that began at or after it
# may have committed after the runner's ``ver`` / ``row_no`` watermark was read (those counters
# are handed out at insert, committed later), so it counts as newer whatever its number
XMIN_SQL = "SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint % 4294967296"
MARKS = "plan_link_marks"
MARKS_STATE = "plan_link_marks_state"
MARKS_FRESH_S = 90      # a hub's link marks older than this are not trusted: its rows read live
MARKS_OVERLAP = 2000    # link rows re-read below the last mark (a row committed late)
LOCK_WAIT_S = 15
# an object with more links than this (a supplier with thousands of orders) gets no link term:
# every new order of it would otherwise end every other order's row, and the probe would scan them
HUB_LINKS = 500
# the page's sections whose plan is stored, and the plan family each is the answer of
FAMILY = {"late": "late_po", "people": "people_on_record", "landed": "landed_cost",
          "trail": "po_trail"}
SECTION = {v: k for k, v in FAMILY.items()}
# A plan run's STATUS is structured, never read from the verdict's words: the investigation's own
# ``error`` and the reason codes of its ``answerability`` and gaps. These codes say the engine did
# not read what the plan needs (a primitive failed or was unavailable, a budget ran out, the root of
# a record that has its page was not found: a read that failed mid-run on a standby in recovery
# answered ``root_not_found`` + ``step_failed`` in 50 ms). Such a run is never an answer: it is stored
# typed ``failed`` and run again (back-off in the row). Every other code is a fact about the data.
RUN_FAILED_REASONS = frozenset({
    "step_failed", "step_unavailable", "step_budget_exhausted", "time_budget_exhausted",
    "root_not_found", "root_lookup_incomplete", "engine_failed"})
# the retry back-off of a failed row, kept IN the row (``attempts``, ``next_try_at``): the first
# retry after ``RETRY_FIRST_S``, doubling per attempt up to ``RETRY_MAX_S`` (a failed row is always
# run again; only the pace is bounded, so a restart of any process never retries them all at once)
RETRY_FIRST_S = 600
RETRY_MAX_S = 6 * 3600


def backoff_seconds(attempts: int) -> int:
    """Seconds before a row that has now failed ``attempts`` times is tried again."""
    return min(RETRY_FIRST_S * 2 ** max(0, min(int(attempts), 30) - 1), RETRY_MAX_S)


def run_status(ran: Any) -> dict[str, Any]:
    """The status of one plan run (``plan_runs``' value: ``investigation`` and ``verdict``) from its
    structure: ``{"status": "ok" | "failed", "reasons": [codes]}``. Failed: the investigation
    carries an ``error`` or no answer list, or one of ``RUN_FAILED_REASONS`` is among its
    answerability reasons or gap reasons."""
    inv = ran.get("investigation") if isinstance(ran, Mapping) else None
    if not isinstance(inv, Mapping):
        return {"status": "failed", "reasons": ["no_investigation"]}
    err = inv.get("error")
    if err:
        code = err.get("code") if isinstance(err, Mapping) else None
        return {"status": "failed", "reasons": [str(code or "investigation_error")]}
    if not isinstance(inv.get("answer"), list):
        return {"status": "failed", "reasons": ["no_answer_list"]}
    codes = list((inv.get("answerability") or {}).get("reasons") or ()) \
        if isinstance(inv.get("answerability"), Mapping) else []
    codes += [g.get("reason") for g in inv.get("gaps") or () if isinstance(g, Mapping)]
    hit = sorted({str(c) for c in codes if c in RUN_FAILED_REASONS})
    return {"status": "failed" if hit else "ok", "reasons": hit}
DDL = """CREATE TABLE {s}.{t} (
    record_id text NOT NULL, section text NOT NULL, payload jsonb NOT NULL,
    comm_handles text[] NOT NULL DEFAULT '{{}}', built_at text NOT NULL, src_stamp text NOT NULL,
    src_handle text NOT NULL, handle text NOT NULL,
    cites text[] NOT NULL DEFAULT '{{}}', access_class integer NOT NULL,
    col_class integer NOT NULL DEFAULT 0, ai_ok boolean NOT NULL, ver bigint NOT NULL DEFAULT 0,
    biz boolean NOT NULL DEFAULT true,
    dep_ids text[] NOT NULL DEFAULT '{{}}', link_ids text[] NOT NULL DEFAULT '{{}}',
    hub_ids text[] NOT NULL DEFAULT '{{}}',
    dep_ver bigint NOT NULL DEFAULT -1, dep_link bigint NOT NULL DEFAULT -1,
    dep_xmin bigint NOT NULL DEFAULT -1)"""
INDEXES = ("(record_id, section)", "(handle)", "(access_class)")


def class_ddl(alias: str = "t") -> str:
    """The order's own class is still the one the row was made under: the page's header row
    (``page_items``, kind ``record``, applied each refresh) has the same ``access_class``. A record
    restricted after the build no longer matches, so the row is hidden from every reader (and
    the resident renews it) instead of showing an old row under its build-time class; so is a
    record with no header any more. ``{s}`` is the business schema."""
    return ("EXISTS (SELECT 1 FROM {s}.page_items h WHERE h.page_kind = 'record' AND "
            f"h.page_key = {alias}.record_id AND h.section = 'header' AND "
            f"h.access_class = {alias}.access_class)")


def needs_ddl(alias: str = "t") -> str:
    """The guarded view's extra condition, decided at READ time: every business record the answer
    cites is still there under the handle it was cited by (a record that moved on has a new
    version in its handle) and the reader holds its access class as it stands now (a class held
    outright, or a document class once the reader has no hidden document at all: those rows'
    handles are not kept). ``{s}`` is the business schema, filled in by ``generic_views``."""
    from operating.pg import door
    held = door._in("r.access_class", door.HELD, "plx.classes")
    dheld = door._in("r.access_class", door.DHELD, "plx.dclasses")
    return (f"{class_ddl(alias)} AND "
            f"NOT EXISTS (SELECT 1 FROM unnest({alias}.cites) c(handle) WHERE NOT EXISTS "
            "(SELECT 1 FROM {s}.records r WHERE r.handle = c.handle AND "
            f"(coalesce({held}, false) OR (coalesce({dheld}, false) AND "
            "NULLIF(current_setting('plx.d_hidden', true), '') = '{}'))))")


def _later(col: str, mark: str) -> str:
    """``col`` (a row's ``xmin``) is at or after ``mark`` (an xid), going round the 32-bit
    counter; frozen rows (xmin 2) are never later."""
    return f"(mod(({col}::text::bigint - {mark}) + 4294967296, 4294967296) < 2147483648)"


def marked_ddl(alias: str = "t") -> str:
    """The guarded view's condition that a row has its three watermarks: one without them
    cannot say what it was read at, so it is never served (not even as an old row)."""
    return f"{alias}.dep_ver >= 0 AND {alias}.dep_link >= 0 AND {alias}.dep_xmin >= 0"


def deps_ddl(alias: str = "t") -> str:
    """The guarded view's freshness condition, decided at READ time from the row's own
    dependency set. The row is served only while:

    * it does not rest on a stale capture (``payload.raw.stale_sources`` empty): such a row is
      kept as an old one and renewed until the capture is fresh, so its "as captured" note is
      never shown after the capture caught up;
    * no object it depends on (``dep_ids``) has a version newer than the runner's start (its
      ``ver`` above ``dep_ver``, or written by a transaction that began at or after the runner's
      ``dep_xmin`` horizon: one that may have committed late);
    * no link row touching one of its ordinary objects (``link_ids``) is newer, by ``row_no``
      above ``dep_link`` or by the same horizon;
    * no link touching one of its hub objects (``hub_ids``: more links than ``HUB_LINKS``, a
      supplier) is newer: every hub has a newest-link mark (``plan_link_marks``, kept by
      ``mark_links`` every few seconds) not above ``dep_link``, the marks cover the row's
      watermark and are younger than ``MARKS_FRESH_S`` (else the row is not served).

    A row without its three watermarks (-1) is never served. Read through the view's own
    rights; one index probe per object (``object_versions_by_object``, ``link_rows_from``, then
    ``link_rows_to``), no scan of the tables. ``{s}`` is the business schema."""
    a = alias
    return (
        f"{a}.dep_ver >= 0 AND {a}.dep_link >= 0 AND {a}.dep_xmin >= 0 AND {class_ddl(a)} AND "
        f"COALESCE(jsonb_array_length(CASE WHEN jsonb_typeof({a}.payload #> '{{raw,stale_sources}}') = "
        f"'array' THEN {a}.payload #> '{{raw,stale_sources}}' END), 0) = 0 AND "
        "NOT EXISTS (SELECT 1 FROM ontology.object_versions v WHERE v.object_id = "
        f"ANY({a}.dep_ids) AND (v.ver > {a}.dep_ver OR {_later('v.xmin', f'{a}.dep_xmin')})) AND "
        "NOT EXISTS (SELECT 1 FROM ontology.link_rows k WHERE k.from_object = "
        f"ANY({a}.link_ids) AND (k.row_no > {a}.dep_link OR {_later('k.xmin', f'{a}.dep_xmin')})) "
        "AND NOT EXISTS (SELECT 1 FROM ontology.link_rows k WHERE k.to_object = "
        f"ANY({a}.link_ids) AND (k.row_no > {a}.dep_link OR {_later('k.xmin', f'{a}.dep_xmin')})) "
        f"AND (cardinality({a}.hub_ids) = 0 OR ((SELECT count(*) FROM {{s}}.{MARKS} m WHERE "
        f"m.object_id = ANY({a}.hub_ids) AND m.newest <= {a}.dep_link AND NOT "
        f"{_later('m.newest_xmin', f'{a}.dep_xmin')}) = cardinality({a}.hub_ids) "
        f"AND EXISTS (SELECT 1 FROM "
        f"{{s}}.{MARKS_STATE} s WHERE s.from_row <= {a}.dep_link AND s.at > now() - interval "
        f"'{MARKS_FRESH_S} seconds')))")


MARKS_DDL = (
    "CREATE TABLE IF NOT EXISTS {s}.%s (object_id text PRIMARY KEY, newest bigint NOT NULL, "
    "newest_xmin bigint NOT NULL DEFAULT 0)" % MARKS,
    "CREATE TABLE IF NOT EXISTS {s}.%s (one boolean PRIMARY KEY DEFAULT true CHECK (one), "
    "from_row bigint NOT NULL, through bigint NOT NULL, at timestamptz NOT NULL)" % MARKS_STATE)


def ensure_marks(pg: Any, s: str) -> None:
    """The hub marks' tables (the guarded view reads them), their coverage starting now when
    they are new: a row computed before that cannot be decided by them and is not served."""
    for ddl in MARKS_DDL:
        pg.execute(ddl.format(s=s))
    # a table made before the transaction horizon was kept
    # (a column check first: ALTER TABLE takes an exclusive lock even when the column is there)
    if pg.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = %s AND "
                  "table_name = %s AND column_name = 'newest_xmin'", (s, MARKS)).fetchone() is None:
        pg.execute(f"ALTER TABLE {s}.{MARKS} ADD COLUMN IF NOT EXISTS newest_xmin bigint NOT NULL DEFAULT 0")
    pg.execute(f"INSERT INTO {s}.{MARKS_STATE} (from_row, through, at) SELECT m, m, now() FROM "
               f"({LINK_SQL}) q(m) ON CONFLICT (one) DO NOTHING")


def track_hubs(pg: Any, s: str) -> int:
    """Every hub object of a stored row gets a mark (its newest link row as the box has it, the
    two link indexes), so a row naming a new hub is decided at once, never passed for lack of
    one. Returns how many were added."""
    new = [r[0] for r in pg.execute(
        f"SELECT DISTINCT h FROM {s}.{TABLE} t, unnest(t.hub_ids) h WHERE NOT EXISTS (SELECT 1 FROM "
        f"{s}.{MARKS} m WHERE m.object_id = h)")]
    (cur,) = pg.execute("SELECT pg_snapshot_xmax(pg_current_snapshot())::text::bigint % 4294967296"
                        ).fetchone()
    for obj in new:
        newest, xmin = 0, 0
        for col in ("from_object", "to_object"):
            for row_no, x in pg.execute(f"SELECT COALESCE(row_no, 0), xmin::text::bigint FROM "
                                        f"ontology.link_rows WHERE {col} = %s", (obj,)):
                newest = max(newest, row_no)
                if x > 2 and (cur - x) % 4294967296 < (cur - xmin) % 4294967296:
                    xmin = x           # the most recently written, going round the counter
        pg.execute(f"INSERT INTO {s}.{MARKS} VALUES (%s, %s, %s) ON CONFLICT (object_id) DO UPDATE "
                   f"SET newest = greatest({s}.{MARKS}.newest, EXCLUDED.newest), newest_xmin = "
                   f"EXCLUDED.newest_xmin", (obj, newest, xmin))
    return len(new)


def mark_links(dsn: str, schema: str | None = None) -> dict[str, Any]:
    """One round of the hub marks, run by the box's puller every few seconds: track new hubs,
    read the link rows written since the last round (``link_rows_by_row_no``; the last
    ``MARKS_OVERLAP`` rows again, in case one committed late) and raise the mark of each hub
    they touch, then stamp the round. A hub row is served only while this stamp is
    ``MARKS_FRESH_S`` young, so a stopped puller makes those rows read live."""
    import psycopg
    s = schema or bv.SCHEMA
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET lock_timeout = 5000")
        if pg.execute("SELECT to_regclass(%s)", (f"{s}.{TABLE}",)).fetchone()[0] is None:
            return {"hubs": 0, "links": 0}
        ensure_marks(pg, s)
        tracked = track_hubs(pg, s)
        (through,) = pg.execute(f"SELECT through FROM {s}.{MARKS_STATE}").fetchone()
        top = pg.execute(LINK_SQL).fetchone()[0]
        seen = 0
        hubs = [r[0] for r in pg.execute(f"SELECT object_id FROM {s}.{MARKS}")]
        (cur,) = pg.execute("SELECT pg_snapshot_xmax(pg_current_snapshot())::text::bigint % "
                            "4294967296").fetchone()
        if hubs:
            for col in ("from_object", "to_object"):
                for obj, newest, xmin, n in pg.execute(
                        f"SELECT {col}, max(row_no), (array_agg(xmin::text::bigint ORDER BY "
                        "mod(%s - xmin::text::bigint + 4294967296, 4294967296)))[1], count(*) FROM "
                        f"ontology.link_rows WHERE row_no > %s AND {col} = ANY(%s) GROUP BY 1",
                        (cur, through - MARKS_OVERLAP, hubs)):
                    seen += n
                    pg.execute(
                        f"UPDATE {s}.{MARKS} SET newest = greatest(newest, %s), newest_xmin = CASE "
                        "WHEN mod(%s - %s + 4294967296, 4294967296) < mod(%s - newest_xmin + "
                        "4294967296, 4294967296) THEN %s ELSE newest_xmin END WHERE object_id = %s",
                        (newest, cur, xmin, cur, xmin, obj))
        pg.execute(f"UPDATE {s}.{MARKS_STATE} SET through = greatest(through, %s), at = now()",
                   (top,))
    return {"hubs": len(hubs), "tracked": tracked, "links": seen}


def _late(verdict: Any) -> tuple[dict[str, Any], list[str]] | None:
    """The late section's payload: the verdict's state and text and its cited facts (a fact with
    no citation is never shown), as ``page_parts.late_of`` reads a run."""
    if not isinstance(verdict, Mapping) or not isinstance(verdict.get("state"), str):
        return None
    text = verdict.get("text")
    facts = []
    # a fact is shown only under a status the page presents (``late_of``: not "not established")
    if verdict["state"] != "not_established":
        for f in verdict.get("facts") or ():
            if isinstance(f, Mapping) and isinstance(f.get("citation"), str) and f.get("openable"):
                facts.append({"text": str(f.get("text") or ""), "citation": f["citation"]})
    return ({"state": verdict["state"], "text": text if isinstance(text, str) else "",
             "facts": facts}, [f["citation"] for f in facts])



def _people(entries: Any, read: Any = None) -> tuple[dict[str, Any], list[str]] | None:
    """The people section's payload: the plan's own computed entries that name a person (the ones
    ``page_parts.people_of`` reads: a mapping value and a ``roles:`` step), kept as the plan gave
    them so the page's own presentation reads them, and the handle each cites. None when the run
    carries anything that is not a list (a failed run is never an answer). ``read`` is every handle
    the whole run cited (the runner's ``read``): they are cited too, since who is found depends on
    what the reader may open of them. A run without it is not stored (the page reads live)."""
    if not isinstance(entries, list):
        return None
    if not isinstance(read, list) or not all(isinstance(h, str) and h for h in read):
        return None
    kept, cites = [], []
    for e in entries:
        value = e.get("value") if isinstance(e, Mapping) else None
        steps = [x for x in (e.get("steps") or ()) if isinstance(x, str)
                 and x.startswith("roles: ")] if isinstance(e, Mapping) else []
        handle = (value.get("origin") or {}).get("citation") if isinstance(value, Mapping) \
            and isinstance(value.get("origin"), Mapping) else None
        if not (isinstance(e, Mapping) and e.get("kind") == "computed" and steps
                and isinstance(handle, str)):
            continue   # one ``people_of`` passes over too: it names no person
        row = {"kind": "computed", "value": value, "steps": steps[:1]}   # the roles step the page reads
        kept.append(row)
        cites.append(handle)
    return {"entries": kept, "read": list(dict.fromkeys(read))}, list(dict.fromkeys([*cites, *read]))


LANDED_ROOTS = re.compile(r"landed_cost|landed_cost_line_\d+")


def _handle(citation: Any) -> str | None:
    """The L1 handle a citation names (``serving.citation_handle``): a string, or the
    ``l1_handle`` of a mapping."""
    if isinstance(citation, str):
        return citation or None
    if isinstance(citation, Mapping):
        got = citation.get("l1_handle")
        return got if isinstance(got, str) and got else None
    return None


def _landed(run: Any) -> tuple[dict[str, Any], list[str]] | None:
    """The landed-cost section's payload: what ``page_parts.landed_cost_of`` reads of the plan
    run: the computed total and line entries (``landed_cost``, ``landed_cost_line_<n>``) with the
    entries their sources are found through (each one's own citation, else its inputs', three
    levels as ``entry_sources`` goes), the plan's answerability state and what its follow-ups say is
    missing, whole. None for a run with no entries list (its status says why)."""
    if not isinstance(run, Mapping) or not isinstance(run.get("entries"), list):
        return None
    by_name = {e["name"]: e for e in run["entries"]
               if isinstance(e, Mapping) and isinstance(e.get("name"), str)}
    wanted: dict[str, int] = {n: 0 for n, e in by_name.items()
                              if e.get("kind") == "computed" and LANDED_ROOTS.fullmatch(n)}
    queue = list(wanted)
    while queue:
        name = queue.pop()
        depth = wanted[name]
        entry = by_name[name]
        if _handle(entry.get("citation")) is None and depth < 3:
            for inner in entry.get("inputs") or ():
                if isinstance(inner, str) and inner in by_name and inner not in wanted:
                    wanted[inner] = depth + 1
                    queue.append(inner)
    kept, cites = [], []
    for name in sorted(wanted):
        e = by_name[name]
        row: dict[str, Any] = {"name": name, "kind": e.get("kind")}
        for key in ("value", "currency", "unit"):
            if isinstance(e.get(key), (str, int, float)) and not isinstance(e.get(key), bool):
                row[key] = e[key]
        if isinstance(e.get("inputs"), list):
            row["inputs"] = [x for x in e["inputs"] if isinstance(x, str)]
        handle = _handle(e.get("citation"))
        if handle is not None:
            row["citation"] = e["citation"] if isinstance(e["citation"], str) else {"l1_handle": handle}
            cites.append(handle)
        kept.append(row)
    answerability = run.get("answerability")
    missing = [str(m) for m in run.get("missing") or () if m]
    payload = {"entries": kept, "answerability": answerability if isinstance(answerability, str)
               else None, "missing": missing}
    return payload, list(dict.fromkeys(cites))


TRAIL_HANDLE_KEYS = ("handle", "contact_handle")


def _handles_in(node: Any, out: list[str]) -> None:
    """Every handle a trail names (``handle`` and ``contact_handle`` anywhere in it, at any depth)
    into ``out``: nothing a trail holds is left unread."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, Mapping):
            for k, v in cur.items():
                if k in TRAIL_HANDLE_KEYS and isinstance(v, str) and v:
                    out.append(v)
                elif isinstance(v, (Mapping, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(x for x in cur if isinstance(x, (Mapping, list)))


def _trail_gaps(raw: Mapping[str, Any], reasons: Iterable[str]) -> list[str]:
    want = set(reasons)
    return sorted({str(g.get("reason")) for g in raw.get("gaps") or ()
                   if isinstance(g, Mapping) and g.get("reason") in want})


def _data_gaps() -> tuple[str, ...]:
    from operating.evidence_trail import DATA_GAPS
    return DATA_GAPS


def _trail_failed(raw: Mapping[str, Any]) -> list[str]:
    from operating.evidence_trail import FAILED_GAPS
    return _trail_gaps(raw, FAILED_GAPS)


def _trail(raw: Any) -> tuple[dict[str, Any], list[str]] | None:
    """The trail section's payload: the purchase order's raw evidence trail
    (``operating.evidence_trail.po_trail``) exactly as the tools returned it for the full
    reader, which ``operator_api.trail.Mapper`` then decides per reader (a step the reader
    may not open is hidden and counted). A trail that found no lateness entry (a gap and
    nothing else) or carries a failed read (``evidence_trail.FAILED_GAPS``) is not an answer.
    A data gap (``chain_incomplete``) is part of the answer, shown as the trail states it. Every
    handle it names is cited, whole."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("steps"), list):
        return None
    if raw.get("state") is None and not raw["steps"]:
        return None
    # a gap that says a read failed or the trail was cut is a run's accident, not the record's:
    # another read finds more, so it is not an answer to keep
    if _trail_failed(raw):
        return None
    cites: list[str] = []
    _handles_in(raw, cites)
    body = {k: v for k, v in raw.items() if k != "label"}
    return {"raw": body}, list(dict.fromkeys(cites))


def _parts(entry: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """(record id, section, family) of a runner line of a stored family; None for anything else."""
    family = entry.get("family")
    section = SECTION.get(family) if isinstance(family, str) else None
    record_id = entry.get("record_id")
    if section is None or not isinstance(record_id, str) or not record_id:
        return None
    return record_id, section, family


def _answer(entry: Mapping[str, Any], section: str) -> tuple[dict[str, Any], list[str]] | None:
    return (_late(entry.get("verdict")) if section == "late" else
            _people(entry.get("entries"), entry.get("read")) if section == "people" else
            _trail(entry.get("trail")) if section == "trail" else _landed(entry.get("landed")))


def item(entry: Mapping[str, Any]) -> tuple[str, str, dict[str, Any], list[str]] | None:
    """One runner line as (record id, section, payload, cited handles), None when it is not a
    complete answer of a stored family (anything unknown fails closed: not stored as an answer;
    ``outcome`` says what it is instead). The payload is exactly what the section's presentation
    reads of the plan run."""
    parts = _parts(entry)
    if parts is None or entry.get("failed"):
        return None
    record_id, section, family = parts
    got = _answer(entry, section)
    if got is None:
        return None
    payload, cites = got
    # the row depends on every handle the run read, cited or not (a reader who may not open one of
    # them gets a different live answer)
    cites = list(dict.fromkeys([*cites, *(entry.get("opened") or ())]))
    return record_id, section, {"family": family, **payload}, cites


def _opened_ok(opened: Any) -> bool:
    """A list of handles, and not empty: a run that answered read at least its record
    (``note_opened`` keeps even a record no version of which the reader may see), so an empty set means
    its reads were never recorded and its row would depend only on what it cited."""
    return isinstance(opened, list) and bool(opened) and all(isinstance(h, str) and h for h in opened)


def _failure(section: str, entry: Mapping[str, Any]) -> str | None:
    """Why a run gave no answer because it FAILED (the plan did not run to its end, a read it
    needs failed, it was cut short), as the exact reason; None when it completed and the answer
    is only one the table does not keep (nothing found). The plan's own status is structured
    (``run_status``, carried in the line's ``run``); the trail's is its failed-read gaps."""
    if section == "trail":
        raw = entry.get("trail")
        if not _opened_ok(entry.get("opened")):
            return "the line states no opened set (every handle the run read)"
        if not isinstance(raw, Mapping) or not isinstance(raw.get("steps"), list):
            return "the run returned no trail"
        cut = _trail_failed(raw)
        return "the trail's read failed: " + ", ".join(cut) if cut else None
    run = entry.get("run")
    if isinstance(run, Mapping) and run.get("status") == "failed":
        # a run that failed may have opened nothing (no such record): its own reason is the one said
        return "the plan's run failed: " + (", ".join(str(r) for r in run.get("reasons") or ())
                                            or "no reason stated")
    if not _opened_ok(entry.get("opened")):
        return "the line states no opened set (every handle the run read)"
    if not isinstance(run, Mapping) or run.get("status") not in ("ok", "failed"):
        return "the line states no run status"
    if section == "late":
        v = entry.get("verdict")
        if not isinstance(v, Mapping) or not isinstance(v.get("state"), str):
            return "the run returned no verdict"
    elif section == "people":
        if not isinstance(entry.get("entries"), list) or not isinstance(entry.get("read"), list):
            return "the run returned no entries or no read set"
    else:
        landed = entry.get("landed")
        if not isinstance(landed, Mapping) or not isinstance(landed.get("entries"), list):
            return "the run returned no entries"
    return None


def outcome(entry: Mapping[str, Any]) -> tuple[str, str]:
    """What a runner line of a stored family is: ``("answer", "")``, ``("failed", reason)`` (the run
    failed or was cut short, or the line states no status: stored as a typed failure and run again
    with a back-off kept in the row), ``("no_answer", reason)`` (the plan completed and its answer
    is not one the table keeps, a fact about the record: stored, not run again until something
    moves), or ``("skip", "")`` for a line of no stored family."""
    parts = _parts(entry)
    if parts is None:
        return "skip", ""
    said = entry.get("failed")
    if isinstance(said, str) and said:
        return "failed", said
    why = _failure(parts[1], entry)
    if why:
        return "failed", why
    got = _answer(entry, parts[1])
    if got is None:
        if parts[1] == "trail":
            data = _trail_gaps(entry["trail"], _data_gaps())
            return "no_answer", ("the trail states: " + ", ".join(data) if data else
                                 "the plan completed and found no answer the table keeps")
        return "no_answer", "the plan completed and found no answer the table keeps"
    return "answer", ""


# the comms views a cited or opened handle may be classed by (each carries ``handle`` and ``object_ids``)
COMM_VIEWS = ("emails", "attachments", "teams_messages", "sharepoint_files", "meetings",
              "meeting_transcripts")


def _classes(pg: Any, s: str, cites: Sequence[str]) -> dict[str, dict[str, Any]] | None:
    """Each cited handle's kind and class: a business record (its access class and record id)
    or a communication of any comms view (``COMM_VIEWS``: mail, attachment, Teams message,
    SharePoint file, meeting, transcript; its objects, its audience decided per item at serve time);
    None when any cited handle is none of these (not decidable here). An opened thing named by
    its object id is classed as the record or item it names, carrying that item's real ``handle``:
    the row's class check (``cites`` / ``comm_handles``) holds the handle, never the object id."""
    out: dict[str, dict[str, Any]] = {}
    if not cites:
        return out
    # a cited or opened thing is named by its handle (a cited source, a comms item) or by its object id
    # (``note_opened`` of a record asked about, whether or not a version of it was visible); both are
    # looked up, so an opened set may mix them and none is dropped
    for h, cls, rid in pg.execute(
            f"SELECT handle, access_class, record_id FROM {s}.records WHERE handle = ANY(%s)",
            (list(cites),)):
        out[h] = {"kind": "record", "class": cls, "objects": [rid], "handle": h}
    rest = [h for h in cites if h not in out]
    if rest:
        for h, cls, rid, real in pg.execute(
                f"SELECT record_id, access_class, record_id, handle FROM {s}.records "
                "WHERE record_id = ANY(%s)", (rest,)):
            out[h] = {"kind": "record", "class": cls, "objects": [rid], "handle": real}
        rest = [h for h in cites if h not in out]
    for table in COMM_VIEWS:
        if not rest:
            break
        if pg.execute("SELECT to_regclass(%s)", (f"{s}.{table}",)).fetchone()[0] is None:
            continue
        for h, objs in pg.execute(
                f"SELECT handle, object_ids FROM {s}.{table} WHERE handle = ANY(%s)", (rest,)):
            out[h] = {"kind": "comm", "objects": list(objs or ()), "handle": h}
        rest = [h for h in cites if h not in out]
    for table in COMM_VIEWS:
        if not rest:
            break
        if pg.execute("SELECT to_regclass(%s)", (f"{s}.{table}",)).fetchone()[0] is None:
            continue
        # an object id of a communication: the item it is the primary object of (``object_ids[1]``,
        # the indexed one)
        for oid, objs, real in pg.execute(
                f"SELECT DISTINCT ON (object_ids[1]) object_ids[1], object_ids, handle FROM {s}.{table} "
                "WHERE object_ids[1] = ANY(%s)", (rest,)):
            out[oid] = {"kind": "comm", "objects": list(objs or ()), "handle": real}
        rest = [h for h in cites if h not in out]
    return None if rest else out


def _mark(stamps: Mapping[str, Any] | None, key: str) -> int:
    """A watermark of the run's stamps as an integer, -1 when it is not one (never served)."""
    try:
        got = int(str((stamps or {}).get(key)))
    except (TypeError, ValueError):
        return -1
    return got if got >= 0 else -1


def _hub(pg: Any, memo: dict[str, bool], obj: str) -> bool:
    """Whether the object has more links than ``HUB_LINKS`` (either end), as the box has them."""
    if obj not in memo:
        n = 0
        for col in ("from_object", "to_object"):
            n += pg.execute(f"SELECT count(*) FROM (SELECT 1 FROM ontology.link_rows WHERE {col} = %s "
                            "LIMIT %s) q", (obj, HUB_LINKS + 1)).fetchone()[0]
        memo[obj] = n > HUB_LINKS
    return memo[obj]


def stage(pg: Any, s: str, new: str, entries: Iterable[Mapping[str, Any]],
          src_stamp: str = "", stamps: Mapping[str, Any] | None = None) -> dict[str, int]:
    """Insert the runner's entries into ``<s>.<new>``: the order's header row (class, handle,
    AI use) from the served record pages, the cited sources' classes and handles. Returns how many
    were stored and how many were not (no page header, an undecidable citation)."""
    stored = skipped = 0
    loaded = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    seen: set[tuple[str, str]] = set()
    dep_ver, dep_link, dep_xmin = (_mark(stamps, "ver"), _mark(stamps, "link"),
                                   _mark(stamps, "xmin"))
    base: dict[str, list[str]] = {}
    hubs: dict[str, bool] = {}
    for entry in entries:
        kind, said = outcome(entry)
        parts = _parts(entry)
        if parts is None:
            skipped += 1
            continue
        refused = said if kind == "no_answer" else None    # stored as "no answer" (typed)
        failed = said if kind == "failed" else None        # stored as "failed" (typed, run again)
        if kind != "answer":
            record_id, section, family = parts
            payload, cites = {"family": family}, []
        else:
            record_id, section, payload, cites = item(entry)
        if (record_id, section) in seen:
            skipped += 1   # one answer per record and section: a second is a mistake, not a choice
            continue
        head = pg.execute(
            f"SELECT src_handle, access_class, ai_ok, source_systems FROM {s}.page_items "
            "WHERE page_kind = 'record' AND page_key = %s AND section = 'header'",
            (record_id,)).fetchone()
        sources = _classes(pg, s, cites)
        if head is None:
            skipped += 1
            continue
        if sources is None:
            refused = "an answer's citation is of a kind that cannot be classed here"
            payload, cites, sources = {"family": parts[2]}, [], {}
        if failed:
            attempts = entry.get("attempts")
            attempts = attempts if isinstance(attempts, int) and not isinstance(attempts, bool) \
                and attempts >= 1 else 1
            began = dt.datetime.fromisoformat(
                (entry.get("run_at") if isinstance(entry.get("run_at"), str) and entry.get("run_at")
                 else loaded).replace("Z", "+00:00"))
            payload = {"family": parts[2], "failed": failed, "attempts": attempts,
                       "next_try_at": (began + dt.timedelta(seconds=backoff_seconds(attempts))
                                       ).isoformat(timespec="seconds")}
        elif refused:
            payload = {"family": parts[2], "no_answer": refused}
        seen.add((record_id, section))
        src, access_class, ai_ok, _ = head
        records = list(dict.fromkeys(sources[h]["handle"] for h in cites if sources[h]["kind"] == "record"))
        run_at = entry.get("run_at")
        built = run_at if isinstance(run_at, str) and run_at else loaded   # when the plan ran
        comm = list(dict.fromkeys(sources[h]["handle"] for h in cites if sources[h]["kind"] == "comm"))
        read = sorted({o for v in sources.values() for o in v["objects"]})
        if src not in base:
            held = pg.execute(f"SELECT object_ids, read_ids FROM {s}.lineage WHERE view = "
                              "'purchase_orders' AND handle = %s", (src,)).fetchone()
            base[src] = sorted({*(held[0] or ()), *(held[1] or ())}) if held else []
        # what the answer depends on: the order's members, lines and evidence, the order itself,
        # every cited record, mail and attachment, whole (no size at which a row goes unstored)
        deps = sorted({*base[src], record_id, *read})
        if not base[src]:
            skipped += 1
            continue
        hub = [o for o in deps if _hub(pg, hubs, o)]
        links = [o for o in deps if o not in hub]
        pg.execute(
            f"INSERT INTO {s}.{new} (record_id, section, payload, comm_handles, built_at, "
            "src_stamp, src_handle, handle, cites, access_class, col_class, ai_ok, ver, biz, "
            "dep_ids, link_ids, hub_ids, dep_ver, dep_link, dep_xmin) "
            "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, 0, %s, 0, true, %s, %s, %s, %s, %s, %s)",
            (record_id, section, json.dumps(payload), comm, built, src_stamp, src,
             f"{src}#{record_id}#plan:{section}", records, access_class, bool(ai_ok),
             deps, links, hub, dep_ver, dep_link, dep_xmin))
        pg.execute(
            "INSERT INTO pg_temp.ps_read VALUES (%s, %s) ON CONFLICT (handle) DO UPDATE SET "
            "ids = EXCLUDED.ids", (f"{src}#{record_id}#plan:{section}", read))
        stored += 1
    return {"stored": stored, "skipped": skipped}


def _lineage(pg: Any, s: str, new: str, handles: Sequence[str] | None = None) -> None:
    """Each row's lineage: its order's (records, evidence, systems, copies) plus the page's own
    record and every cited record and mail object (read), so the people exclusion follows it.
    ``handles``: only these rows' (an in-place upsert); none: every row's, the old ones dropped."""
    only = "" if handles is None else " AND i.handle = ANY(%s)"
    pg.execute(f"DELETE FROM {s}.lineage WHERE view = %s" + ("" if handles is None
               else " AND handle = ANY(%s)"), (TABLE,) if handles is None else (TABLE, list(handles)))
    pg.execute(
        f"INSERT INTO {s}.lineage (view, handle, object_ids, read_ids, col_objects, cites, "
        "systems, copies) SELECT %s, i.handle, "
        "ARRAY(SELECT DISTINCT x FROM unnest(COALESCE(l.object_ids, '{}'::text[]) || "
        "ARRAY[i.record_id]) x ORDER BY 1), "
        "ARRAY(SELECT DISTINCT x FROM unnest(COALESCE(l.read_ids, '{}'::text[]) || "
        "COALESCE(r.ids, '{}'::text[])) x ORDER BY 1), '{}'::jsonb, '{}'::jsonb, l.systems, "
        f"l.copies FROM {s}.{new} i JOIN {s}.lineage l ON l.handle = i.src_handle AND "
        "l.view = 'purchase_orders' LEFT JOIN pg_temp.ps_read r ON r.handle = i.handle"
        f"{' WHERE true' + only if only else ''}",
        (TABLE,) if handles is None else (TABLE, list(handles)))


def code_fingerprint() -> str | None:
    from ontology.build_keys import code_digest
    digest = code_digest([("operating.pg.plan_sections", "item"),
                          ("operating.pg.plan_sections", "stage"),
                          ("operating.pg.plan_sections", "_classes"),
                          ("operating.pg.plan_sections", "_lineage"),
                          ("operating.pg.plan_sections", "load")])
    return digest[:32] if digest else None


def stamp_of(stamps: Mapping[str, Any] | None) -> str:
    """The order views' build stamps the answers were computed from, as one text (what the page
    compares with the stamps now: an answer is served only while they are equal)."""
    held = stamps or {}
    return "|".join([*(str(held.get(v) or "") for v in SOURCES), str(held.get("ver") or "")])


def read_file(path: str) -> list[dict[str, Any]]:
    """The runner's file: one JSON object per line (a header line ``{"stamps": ...}`` first)."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load(dsn: str, entries: Sequence[Mapping[str, Any]], schema: str | None = None, *,
         stamps: Mapping[str, Any] | None = None,
         runs: Sequence[tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]] = ()
         ) -> dict[str, Any]:
    """Build the table from the runner's entries beside the served one, decide lineage and class
    counts, then swap in with the guarded view re-made (one transaction: a reader sees the old
    rows or the new). Any failure leaves the served table as it is."""
    import psycopg
    from operating.pg import generic_views as gv, door, swap_lock
    s = schema or bv.SCHEMA
    started = time.monotonic()
    new = f"{TABLE}_new"
    with psycopg.connect(dsn, autocommit=True) as pg:
        for needed in ("page_items", "records", "lineage", "access_classes"):
            if pg.execute("SELECT to_regclass(%s)", (f"{s}.{needed}",)).fetchone()[0] is None:
                raise RuntimeError(f"{s}.{needed} is not built: no plan sections")
        pg.execute(f"DROP TABLE IF EXISTS {s}.{new}")
        pg.execute(DDL.format(s=s, t=new))
        ensure_marks(pg, s)
        pg.execute("DROP TABLE IF EXISTS pg_temp.ps_read")
        pg.execute("CREATE TEMP TABLE ps_read (handle text PRIMARY KEY, ids text[])")
        # each run's answers carry the stamps of its own start (late and people are separate runs)
        counts = {"stored": 0, "skipped": 0}
        for held, group in [(stamps, entries), *runs]:
            if group:
                got = stage(pg, s, new, group, stamp_of(held), held)
                counts = {k: counts[k] + got[k] for k in counts}
        for key in INDEXES:
            pg.execute(f"CREATE INDEX ON {s}.{new} {key}")
        pg.execute(f"ALTER TABLE {s}.{new} ADD UNIQUE (handle)")
        pg.execute(f"ANALYZE {s}.{new}")
        items = pg.execute(f"SELECT count(*) FROM {s}.{new}").fetchone()[0]
        with pg.transaction():
            _lineage(pg, s, new)
            held = pg.execute(f"SELECT count(*) FROM {s}.lineage WHERE view = %s", (TABLE,)
                              ).fetchone()[0]
            if held != items:   # a row the exclusion could not follow is never served
                raise RuntimeError(f"{items} items but {held} with a lineage: not swapped")
            door.exclude(pg, s, only=TABLE, record=False, staged=(f"{s}.{new}", f"{s}.lineage"))
            door.recount(pg, s, TABLE, new)
            pg.execute(
                f"CREATE TABLE IF NOT EXISTS {s}.state (view text PRIMARY KEY, built_at text NOT "
                "NULL, digest text, rows bigint NOT NULL, seconds double precision, "
                "notes jsonb NOT NULL DEFAULT '{}')")
            pg.execute(
                f"INSERT INTO {s}.state (view, built_at, rows, seconds, notes) VALUES (%s, %s, "
                "%s, %s, %s) ON CONFLICT (view) DO UPDATE SET built_at = EXCLUDED.built_at, "
                "rows = EXCLUDED.rows, seconds = EXCLUDED.seconds, notes = EXCLUDED.notes",
                (STATE_VIEW, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 items, round(time.monotonic() - started, 1),
                 json.dumps({"from": dict(stamps or {}), "code": code_fingerprint(), **counts})))

            def steps() -> None:
                gv.sql_views.drop(pg, TABLE, s)
                pg.execute(f"DROP TABLE IF EXISTS {s}.{TABLE}")
                pg.execute(f"ALTER TABLE {s}.{new} RENAME TO {TABLE}")
                gv.make_views(pg, s, follow=False)
            swap_lock.swap_in(pg, steps)
        mark_links(dsn, s)
    return {"items": items, **counts, "seconds": round(time.monotonic() - started, 1)}


def upsert(dsn: str, runs: Sequence[tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]],
           schema: str | None = None) -> dict[str, Any]:
    """Put the answers of some records into the served table IN PLACE (the resident runner's
    incremental pass: a few rows, no whole-table swap): each (stamps, entries) run is staged as
    ``load`` stages it, then one transaction replaces the rows of the same (record, section) and
    adds the new ones, with their lineage, the people exclusion decided over their objects and
    the class counts. A row is never replaced by one computed from an OLDER state (its watermark
    below the stored row's): a late file cannot undo a newer answer. Returns the counts."""
    import psycopg
    from operating.pg import door
    s = schema or bv.SCHEMA
    started = time.monotonic()
    # a staging table of this call alone: the live puller and a fill both call upsert, and a shared
    # name made each drop the other's table mid-run (P17); dropped when this call ends, however it ends
    new = f"{TABLE}_inc_{os.getpid()}_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as pg:
        try:
            return _upsert_staged(pg, s, new, runs, started)
        finally:
            try:
                pg.execute(f"DROP TABLE IF EXISTS {s}.{new}")
            except Exception as exc:   # surfaced, never silent: a leftover is named for the operator
                print(f"plan_sections.upsert: staging table {s}.{new} not dropped: {exc!r}",
                      file=sys.stderr)


def _upsert_staged(pg: Any, s: str, new: str, runs: Any, started: float) -> dict[str, Any]:
    """The body of ``upsert`` over one connection and one staging table named for this call."""
    from operating.pg import door
    # a table being swapped (the order pages' rebuild holds its lock for minutes) is waited
    # for 15 s, then ``psycopg.errors.LockNotAvailable``: the caller tries again later
    pg.execute(f"SET lock_timeout = {int(LOCK_WAIT_S * 1000)}")
    for needed in (TABLE, "page_items", "records", "lineage", "access_classes"):
        if pg.execute("SELECT to_regclass(%s)", (f"{s}.{needed}",)).fetchone()[0] is None:
            raise RuntimeError(f"{s}.{needed} is not built: nothing to put rows into")
    pg.execute(f"DROP TABLE IF EXISTS {s}.{new}")
    pg.execute(DDL.format(s=s, t=new))
    ensure_marks(pg, s)
    cols = ", ".join(r[0] for r in pg.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND "
        "table_name = %s ORDER BY ordinal_position", (s, new)))
    pg.execute("DROP TABLE IF EXISTS pg_temp.ps_read")
    pg.execute("CREATE TEMP TABLE ps_read (handle text PRIMARY KEY, ids text[])")
    counts = {"stored": 0, "skipped": 0}
    # oldest run first: a record answered twice keeps the answer of the newer state
    for held, group in sorted(runs, key=lambda r: (_mark(r[0], "ver"), _mark(r[0], "link"))):
        if group:
            got = stage(pg, s, new, group, stamp_of(held), held)
            counts = {k: counts[k] + got[k] for k in counts}
    pg.execute(f"DELETE FROM {s}.{new} a USING {s}.{new} b WHERE a.handle = b.handle AND "
               "(a.dep_ver, a.dep_link, a.built_at, a.ctid::text) < (b.dep_ver, b.dep_link, "
               "b.built_at, b.ctid::text)")
    placed = replaced = 0
    with pg.transaction():
        # the rows to put: not computed from an older state than the stored one
        keep = [r[0] for r in pg.execute(
            f"SELECT n.handle FROM {s}.{new} n LEFT JOIN {s}.{TABLE} t ON t.handle = n.handle "
            "WHERE t.handle IS NULL OR (n.dep_ver >= t.dep_ver AND n.dep_link >= t.dep_link)")]
        if keep:
            now = dt.datetime.now(dt.timezone.utc)
            replaced = pg.execute(f"DELETE FROM {s}.{TABLE} WHERE handle = ANY(%s)",
                                  (keep,)).rowcount
            pg.execute(f"INSERT INTO {s}.{TABLE} ({cols}) SELECT {cols} FROM {s}.{new} "
                       "WHERE handle = ANY(%s)", (keep,))
            _lineage(pg, s, new, keep)
            have = pg.execute(f"SELECT count(*) FROM {s}.lineage WHERE view = %s AND "
                              "handle = ANY(%s)", (TABLE, keep)).fetchone()[0]
            if have != len(keep):   # a row the exclusion could not follow is never served
                raise RuntimeError(f"{len(keep)} rows but {have} with a lineage: not put")
            # the rows decided against the people exclusion as it stands (this table's lineage
            # alone, as ``make_views`` follows a rebuilt table), then the class counts
            door.follow(pg, s, TABLE)
            door.recount(pg, s, TABLE)
            track_hubs(pg, s)
            placed = len(keep)
            pg.execute(f"UPDATE {s}.state SET built_at = %s, rows = (SELECT count(*) FROM "
                       f"{s}.{TABLE}) WHERE view = %s", (now.isoformat(timespec="seconds"),
                                                          STATE_VIEW))
    return {"placed": placed, "replaced": replaced, "older": counts["stored"] - placed,
            "skipped": counts["skipped"],
            "seconds": round(time.monotonic() - started, 2)}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.plan_sections")
    ap.add_argument("command", choices=("load", "upsert"), help="load: replace the whole table; "
                    "upsert: put these files' records in place, every other row kept")
    ap.add_argument("file", nargs="+", help="one runner file per run (each with its own stamps)")
    ap.add_argument("--schema")
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    from operating.pg import config
    runs = []
    for path in args.file:
        lines = read_file(path)
        head = next((x["stamps"] for x in lines if isinstance(x.get("stamps"), dict)), {})
        runs.append((head, [x for x in lines if "record_id" in x]))
    if args.command == "upsert":
        print(json.dumps(upsert(args.dsn or config.dsn(), [(h, g) for h, g in runs], args.schema)))
        return 0
    print(json.dumps(load(args.dsn or config.dsn(), [], args.schema, runs=runs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
