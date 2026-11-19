"""Sender-party links kept right over delta layers: each message judged against the whole
served party directory.

``ontology.sender_parties`` (link prefix ``sendparty:``) links a message to the customer or
supplier that sent it by the parties' stored addresses and domains, and a calendar event or
meeting to the parties its organizer and attendees name. A full build judges every sender
against every party. A delta layer judges only against the parties its closure brought in
(``ontology/delta.py``: the records the changed records' links, identifiers and mentions
name), and a party holding the sender's domain is none of those: measured on the 7,304 served
messages of three days, the layers linked 93 (and 6 of them to a party the whole
directory says is not the sender's alone); the step's own code over the whole directory
links 376 (208 leads, 168 corroborated). Mail from a domain a party holds was linked 55-60%
a day while trains re-derived the links in place, 10-20% after.

So after the layers of a cycle (``operating.pg.ontology_delta.apply_listed``),
:func:`recompute` judges the messages the layers replaced since its mark (``store_meta``
``whole_steps:sender_parties``: the newest ``applied_owned`` row it covered), and the
messages linked to a party record they replaced, with the step's own code
(``read_parties``, ``Parties.judge``, ``named_parties_batch``, ``link_rows``,
``meeting_rows``; ``ontology/sender_parties.py`` unchanged, no digest moves) over the whole
served directory (every Customer, Supplier and Contact, read once per run before the lock),
reading only those messages' values by index (``people_scope.ObjectsConnection``). One
transaction under the delta's lock, chunks of :data:`CHUNK`, at most :data:`RUN_MAX`
objects a run (a catch-up drains over cycles); only changed links written (the served
``sendparty:`` rows from the judged objects replaced, never a person's rejection, which
also vetoes a pair); the from-ends recorded as a ``whole-steps-senders-`` layer (the
traverse facts fold it) and the link slice folded in the same transaction. The layer apply
still writes its own judgement first; this run then makes it the whole directory's.

The first run sets the mark at the newest ``applied_owned`` row and judges nothing;
:func:`rejudge` (``backfill``) judges a given set (the messages received since a date) the
same way, in one short transaction per chunk, the mark unmoved (``--dry-run``: judged and
counted, nothing written).

A failed run changes nothing (its transaction rolls back) and the mark stays; the cycle
reports ``sender_links: {"error": ...}`` (alarmed: ``ops_alarms_delta``).

    python3 -m operating.pg.sender_scope run [--dsn DSN]
    python3 -m operating.pg.sender_scope backfill --since YYYY-MM-DD [--dry-run]
        [--journal FILE] [--dsn DSN]

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import sender_parties as sp
from operating.pg import people_scope as ps
from operating.pg import whole_steps

STATE = "whole_steps:sender_parties"        # store_meta key: {"through": seq}
LAYER_PREFIX = whole_steps.LAYER_PREFIX + "senders-"
CHUNK = 5000                                # objects judged at once
RUN_MAX = 50_000                            # objects a run judges (a catch-up drains)
MESSAGE = sp.MESSAGE_TYPE + ":"
DIRECTORY_TYPES = (*sp.PARTY_TYPES, sp.CONTACT_TYPE)


class Refused(RuntimeError):
    """A read the run cannot answer as the step would: nothing is written."""


def prefix() -> tuple[str, str]:
    """The step's link id range ``[start, end)``."""
    return sp.PREFIX, sp._PREFIX_END


def context(environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """What the step reads besides the store: the schema's links, the declared message and
    meeting link types, and our own mail domains (``sender_parties.own_domains`` of a
    projector whose mail source has the delta's internal domains). None when the schema
    declares none of the step's links."""
    from ontology.delta import _internal_domains
    from ontology.schema import load_schema
    environ = os.environ if environ is None else environ
    links = load_schema().link_types
    declared = sp.declared_types(links)
    meetings = {t: d for t, m in sp.MEETING_LINK_TYPES.items()
                if (d := sp.declared_types(links, m))}
    if not declared and not meetings:
        return None
    mail = SimpleNamespace(internal_domains=tuple(_internal_domains(environ)))
    types = ([sp.MESSAGE_TYPE] if declared else []) + sorted(meetings)
    return {"links": links, "declared": declared, "meetings": meetings,
            "own": sp.own_domains(SimpleNamespace(sources={"graph_mail": mail})),
            "types": tuple(types),
            "link_types": tuple(sorted(set(declared.values()) | {
                lt for d in meetings.values() for lt in d.values()}))}


# --------------------------------------------------------------------------- reads

def directory_objects(cur: Any) -> list[str]:
    """Every Customer, Supplier and Contact object id (the directory the step reads)."""
    c = ps._collate(cur)
    out: list[str] = []
    for kind in DIRECTORY_TYPES:
        cur.execute(f"SELECT object_id FROM ontology.objects WHERE object_id{c} >= %s AND "
                    f"object_id{c} < %s", (kind + ":", kind + ";"))
        out.extend(r[0] for r in cur.fetchall())
    return sorted(out)


def served_links(cur: Any, objects: Sequence[str], link_types: Sequence[str]
                 ) -> dict[str, dict[str, Any]]:
    """The served ``sendparty:`` links from ``objects`` (every column), by id."""
    from ontology import store_ddl
    start, end = prefix()
    out: dict[str, dict[str, Any]] = {}
    objects = sorted(set(objects))
    for i in range(0, len(objects), 20_000):
        cur.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM ontology.links WHERE "
                    "from_object = ANY(%s) AND link_type = ANY(%s)",
                    (objects[i:i + 20_000], list(link_types)))
        names = [d.name for d in cur.description]
        for row in cur.fetchall():
            if start <= row[0] < end:
                out[row[0]] = dict(zip(names, row))
    return out


def linked_to(cur: Any, parties: Iterable[str], link_types: Sequence[str]) -> set[str]:
    """The objects a served ``sendparty:`` link names one of ``parties`` from."""
    start, end = prefix()
    parties = sorted(set(parties))
    out: set[str] = set()
    for i in range(0, len(parties), 20_000):
        cur.execute("SELECT DISTINCT from_object FROM ontology.link_rows WHERE "
                    "to_object = ANY(%s) AND link_type = ANY(%s) AND link_id COLLATE \"C\" "
                    ">= %s AND link_id COLLATE \"C\" < %s",
                    (parties[i:i + 20_000], list(link_types), start, end))
        out.update(r[0] for r in cur.fetchall())
    return out


def _replaced(objects: Iterable[str]) -> Callable[[Mapping[str, Any]], bool]:
    """The served rows a judgement of ``objects`` stands for: the step's links from them,
    never a person's rejection."""
    held = frozenset(objects)
    start, end = prefix()

    def replaced(row: Mapping[str, Any]) -> bool:
        return (start <= row["link_id"] < end and row.get("tier") != "contradicted"
                and row["from_object"] in held)
    return replaced


# --------------------------------------------------------------------------- judging

class Directory:
    """The parties the step judges against (``sender_parties.read_parties`` over every
    Customer, Supplier and Contact) and the party records' current versions (read once)."""

    def __init__(self, conn: ps.ObjectsConnection, cur: Any) -> None:
        conn.objects = directory_objects(cur)
        self.count = len(conn.objects)
        try:
            self.parties = sp.read_parties(conn)
        finally:
            conn.objects = []
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
            for row in ps._named(self._cur):
                self._versions[row["object_id"]] = row
        return {k: v for k, v in self._versions.items() if v is not None}


def judge(conn: ps.ObjectsConnection, cur: Any, ctx: Mapping[str, Any], d: Directory,
          objects: Sequence[str], counts: Counter) -> list[dict[str, Any]]:
    """The step's link rows from ``objects`` (its messages and meetings; any other object
    is judged to none), as ``sender_parties_step`` makes them over the whole directory."""
    parties = d.parties
    held = set(objects)
    rows: dict[str, dict[str, Any]] = {}
    messages = sorted(o for o in held if o.startswith(MESSAGE))
    if messages and ctx["declared"]:
        conn.objects = messages
        try:
            window = list(sp.judge_messages(conn, parties, ctx["own"]))
        finally:
            conn.objects = []
        if any(m["object_id"] not in held for m, _o, _t, _h in window):
            raise Refused("sender_scope_value_outside_objects")
        wanted = [m["object_id"] for m, outcome, tier, _h in window
                  if tier is None and outcome in ("shared_by_parties", "no_party")]
        names = sp.named_parties_batch(conn, wanted, parties) if wanted else {}
        judged = []
        for message, outcome, tier, holdings in window:
            named: list[sp.Holding] = []
            if tier is None and outcome in ("shared_by_parties", "no_party"):
                named = names.get(message["object_id"], [])
                if named:
                    found = sp.addresses(message["value"])
                    outcome, tier, holdings = parties.by_named(found[0], outcome, holdings,
                                                               named)
            counts[outcome] += 1
            if tier is not None:
                judged.append((message, tier, holdings, named))
        versions = d.versions({t for _m, _t, hs, _n in judged for h in hs
                               for t in (h.party, *parties.twins.get(h.party, ()))})
        for message, tier, holdings, named in judged:
            for row in sp.link_rows(message, tier, holdings, parties, versions,
                                    ctx["declared"], named=named):
                rows.setdefault(row["link_id"], row)
    meetings = sorted(o for o in held if o.split(":", 1)[0] in ctx["meetings"])
    if meetings:
        conn.objects = meetings
        try:
            made = sp.meeting_rows(conn, parties, ctx["own"], ctx["links"], counts)
        finally:
            conn.objects = []
        for row in made:
            if row["from_object"] not in held:
                raise Refused("sender_scope_value_outside_objects")
            rows.setdefault(row["link_id"], row)
    rejected = ps._rejected(cur, sorted(held)) if rows else set()
    out = []
    for link_id in sorted(rows):
        row = rows[link_id]
        if (row["from_object"], row["to_object"], row["link_type"]) in rejected:
            counts["rejected"] += 1
            continue
        out.append(row)
    return out


# --------------------------------------------------------------------------- the run

def _state(cur: Any) -> dict[str, Any]:
    cur.execute("SELECT value FROM ontology.store_meta WHERE key = %s", (STATE,))
    row = cur.fetchone()
    try:
        held = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        return {}
    return held if isinstance(held, dict) and isinstance(held.get("through"), int) else {}


def _set_state(cur: Any, through: int, **extra: Any) -> None:
    cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES (%s, %s) ON CONFLICT "
                "(key) DO UPDATE SET value = excluded.value",
                (STATE, json.dumps({"through": int(through), **extra})))


def _run_max(environ: Mapping[str, str] | None = None) -> int:
    held = os.environ if environ is None else environ
    try:
        return max(1, int(held.get("PLX_SENDER_LINKS_RUN_MAX") or RUN_MAX))
    except ValueError:
        return RUN_MAX


class _Session:
    """One run's judging: a compat connection, its server-binding cursor, a local store
    folder, the directory (read once, before the lock)."""

    def __init__(self, dsn: str, folder: Path, statement_timeout_ms: int) -> None:
        from ontology.pg_step_inplace import _local_store
        from operating.pg.sqlite_compat import PgCompatConnection
        self.folder = folder
        self.pg = PgCompatConnection(dsn, statement_timeout_ms=statement_timeout_ms)
        self.cur = self.pg._pg.cursor()
        self.native = ps.native(self.pg)
        self._local = _local_store(folder / "senders-read.sqlite")
        self.conn = ps.ObjectsConnection(self._local.connection, self.pg)
        self.d = Directory(self.conn, self.native)

    def close(self) -> None:
        try:
            self._local.abort()
        finally:
            self.pg.close()


def _judge_write(s: _Session, cur: Any, ctx: Mapping[str, Any], objects: Sequence[str],
                 counts: Counter, totals: Counter, *, write: bool = True,
                 journal: Any = None) -> set[str]:
    """Judge ``objects`` in chunks and make the served rows theirs; the from-ends whose
    links changed (counted in ``totals`` also when ``write`` is False). ``journal``: a
    file each write's replaced served rows and written ids go to (a backfill's)."""
    ends: set[str] = set()
    ordered = sorted(set(objects))
    for i in range(0, len(ordered), CHUNK):
        part = ordered[i:i + CHUNK]
        served = served_links(s.native, part, ctx["link_types"])
        rows = judge(s.conn, s.native, ctx, s.d, part, counts)
        new = whole_steps._normal(rows, s.folder, "senders") if rows else {}
        if write:
            got, (gone, changed, _added) = ps._write(cur, new, served, _replaced(part), totals)
            if journal is not None and (gone or changed or _added):
                journal.write(json.dumps({"replaced": [served[lid] for lid in gone + changed],
                                          "written": sorted(changed + _added)},
                                         sort_keys=True, default=str) + "\n")
                journal.flush()
            ends |= got
        else:
            gone, changed, added, kept = whole_steps.diff(new, served, _replaced(part))
            for name, value in (("gone", len(gone)), ("changed", len(changed)),
                                ("added", len(added)), ("kept", kept)):
                totals[name] += value
            for lid in added:
                totals[f"added:{new[lid][1]}"] += 1
            for lid in gone:
                totals[f"gone:{served[lid]['link_type']}"] += 1
    return ends


def _clean(folder: Path) -> None:
    for f in folder.glob("*"):
        f.unlink(missing_ok=True)
    folder.rmdir()


def recompute(dsn: str, *, work: str | Path | None = None, now: float | None = None,
              environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """One run over the layers applied since the mark (module docstring):
    ``{"pending": False}`` when there is nothing to do, else its report (``ends``: the
    objects whose sender links changed). Raises on failure: nothing is changed and the
    mark stays, so the next cycle runs it again."""
    import psycopg
    from operating.pg import link_marks
    started = time.monotonic()
    now = time.time() if now is None else now
    ctx = context(environ)
    if ctx is None:
        return {"pending": False, "declared": False}
    watched = list(ctx["types"]) + list(sp.PARTY_TYPES)
    with psycopg.connect(dsn, connect_timeout=10) as pg0, pg0.cursor() as cur0:
        cur0.execute("SELECT to_regclass('ontology.store_meta') IS NOT NULL")
        if not cur0.fetchone()[0]:
            return {"pending": False}
        state = _state(cur0)
        if "through" not in state:
            newest = ps._newest(cur0)
            if newest is None:
                return {"pending": False}
            # the first run: the mark at the newest replacement so far (``backfill`` for
            # the messages before it)
            cur0.execute("INSERT INTO ontology.store_meta (key, value) VALUES (%s, %s) "
                         "ON CONFLICT (key) DO NOTHING",
                         (STATE, json.dumps({"through": newest, "set_by": "first_run"})))
            pg0.commit()
            return {"pending": False, "started": newest}
        if not ps._touched(cur0, state["through"], watched, limit=1):
            return {"pending": False}
    folder = Path(tempfile.mkdtemp(prefix=".sender-scope-", dir=work))
    report: dict[str, Any] = {"mode": "incremental"}
    totals: Counter = Counter()
    counts: Counter = Counter()
    timeout = ps.statement_ms(environ)
    s = _Session(dsn, folder, timeout)
    try:
        cur = s.cur
        cur.execute("BEGIN")
        t_lock = time.monotonic()
        ps.lock(cur, timeout)
        cur.execute("SET LOCAL jit = off")
        state = _state(cur)
        if "through" not in state:   # a load in between
            cur.execute("ROLLBACK")
            return {"pending": False}
        newest, touched = ps._due(cur, state["through"], watched, _run_max(environ))
        report["remaining"] = newest < int(ps._newest(cur) or 0)
        party_types = set(sp.PARTY_TYPES)
        parties = [o for o in touched if o.split(":", 1)[0] in party_types]
        objects = {o for o in touched if o.split(":", 1)[0] in ctx["types"]}
        if parties:   # a party record replaced: the messages linked to it judged again
            objects |= linked_to(s.native, parties, ctx["link_types"])
        marks_start = link_marks.layer_start(s.native)
        ends = _judge_write(s, cur, ctx, sorted(objects), counts, totals)
        _set_state(cur, newest)
        report["layer"] = None
        if ends:
            report["layer"] = whole_steps._record(cur, ends, newest, now, prefix=LAYER_PREFIX)
            report["link_marks_folded"] = ps._fold_marks(s.native, marks_start, ends)
        cur.execute("COMMIT")
        report["lock_seconds"] = round(time.monotonic() - t_lock, 1)
    except BaseException:
        try:
            s.cur.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the session goes with the run
            pass
        raise
    finally:
        s.close()
        _clean(folder)
    report.update({"touched": len(touched), "parties_touched": len(parties),
                   "objects": len(objects), "directory": s.d.count,
                   "links": dict(sorted(totals.items())),
                   "counts": dict(sorted(counts.items())), "ends": sorted(ends)})
    if report["layer"]:
        report["facts_kept"] = whole_steps._fold_facts(dsn)
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def rejudge(dsn: str, objects: Iterable[str], *, work: str | Path | None = None,
            now: float | None = None, environ: Mapping[str, str] | None = None,
            dry_run: bool = False, log: Any = None,
            journal: str | Path | None = None) -> dict[str, Any]:
    """The sender links from ``objects`` (messages and meetings; others are left alone)
    judged again over the whole served directory now, as :func:`recompute` judges, one
    transaction under the delta's lock per :data:`CHUNK` objects (a short hold each); the
    mark is not moved. ``dry_run``: judged and counted, nothing written. ``journal``: a
    file the served rows each chunk replaced are appended to (by hand undo). Raises on
    failure: the chunk in hand is rolled back, the chunks before it stay written."""
    from operating.pg import link_marks
    started = time.monotonic()
    now = time.time() if now is None else now
    ctx = context(environ)
    if ctx is None:
        return {"declared": False}
    objects = sorted({o for o in objects if o.split(":", 1)[0] in ctx["types"]})
    folder = Path(tempfile.mkdtemp(prefix=".sender-scope-", dir=work))
    totals: Counter = Counter()
    counts: Counter = Counter()
    timeout = ps.statement_ms(environ)
    s = _Session(dsn, folder, timeout)
    layers: list[str] = []
    ends_all: set[str] = set()
    kept = open(journal, "a", encoding="utf-8") if journal and not dry_run else None
    try:
        cur = s.cur
        for i in range(0, len(objects), CHUNK):
            part = objects[i:i + CHUNK]
            cur.execute("BEGIN")
            try:
                ps.lock(cur, timeout)
                cur.execute("SET LOCAL jit = off")
                marks_start = link_marks.layer_start(s.native)
                ends = _judge_write(s, cur, ctx, part, counts, totals, write=not dry_run,
                                    journal=kept)
                if ends:
                    newest = int(ps._newest(cur) or 0)
                    layers.append(whole_steps._record(cur, ends, newest, now + i / 1e6,
                                                      prefix=LAYER_PREFIX))
                    ps._fold_marks(s.native, marks_start, ends)
                    ends_all |= ends
                cur.execute("ROLLBACK" if dry_run else "COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            if log is not None:
                print(json.dumps({"done": i + len(part), "of": len(objects),
                                  "links": dict(sorted(totals.items()))}, sort_keys=True),
                      file=log, flush=True)
    finally:
        if kept is not None:
            kept.close()
        s.close()
        _clean(folder)
    report = {"objects": len(objects), "directory": s.d.count, "dry_run": dry_run,
              "links": dict(sorted(totals.items())), "counts": dict(sorted(counts.items())),
              "layers": len(layers), "ends": sorted(ends_all)}
    if layers and not dry_run:
        report["facts_kept"] = whole_steps._fold_facts(dsn)
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def since_objects(dsn: str, since: str, types: Sequence[str]) -> list[str]:
    """The messages (``biz.emails``) and meetings (``biz.meetings``) dated on or after
    ``since`` (an ISO date): the backfill's set."""
    import psycopg
    out: set[str] = set()
    with psycopg.connect(dsn, connect_timeout=10) as pg, pg.cursor() as cur:
        if sp.MESSAGE_TYPE in types:
            cur.execute("SELECT object_ids[1] FROM biz.emails WHERE received_at >= %s",
                        (since,))
            out.update(r[0] for r in cur.fetchall() if r[0])
        meetings = [t for t in types if t != sp.MESSAGE_TYPE]
        if meetings:
            cur.execute("SELECT to_regclass('biz.meetings') IS NOT NULL")
            if cur.fetchone()[0]:
                cur.execute("SELECT unnest(object_ids) FROM biz.meetings WHERE start_at >= %s",
                            (since,))
                out.update(r[0] for r in cur.fetchall()
                           if r[0] and r[0].split(":", 1)[0] in meetings)
    return sorted(out)


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.sender_scope")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="one run over the layers since the mark")
    run.add_argument("--dsn", default=None)
    back = sub.add_parser("backfill", help="judge the messages since a date again")
    back.add_argument("--since", required=True)
    back.add_argument("--dry-run", action="store_true")
    back.add_argument("--journal", default=None, help="file the replaced rows go to")
    back.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    if args.cmd == "run":
        report = recompute(dsn)
    else:
        ctx = context()
        if ctx is None:
            print(json.dumps({"declared": False}), flush=True)
            return 0
        report = rejudge(dsn, since_objects(dsn, args.since, ctx["types"]),
                         dry_run=args.dry_run, log=sys.stderr, journal=args.journal)
    ends = report.pop("ends", None) or []
    report["ends"] = len(ends)
    if ends and not report.get("dry_run"):
        from operating.pg.ontology_delta import refresh_repeats
        report["mention_repeats"] = refresh_repeats(dsn, ends)
    print(json.dumps(report, sort_keys=True, default=str), flush=True)
    return 0


__all__ = ["LAYER_PREFIX", "STATE", "context", "judge", "prefix", "recompute", "rejudge"]


if __name__ == "__main__":
    sys.exit(main())
