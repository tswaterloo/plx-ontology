"""Re-index the queued ``Person`` objects of the people directory from their served values,
in place, and clear each one the current people exclusion clears from ``ontology.reproject``.

Why: a migration's text-index step (``operating.pg.ontology_migrate._text_index``) queues
every object of a binding over a document source that holds a version the served rows did
not. ``graph_mail.people`` is such a binding (``MailCaptures`` has documents), so a train
that rewrote it queued every ``Person``. No delta cycle projects a Person again: the people
directory is a whole-corpus derivation (``ontology.people_directory``), and its handles
name no captured record (``ontology.delta.reproject_requests`` counts them ``no_record``).

What the search text of a Person is (each index's own rule):

* ``ontology.text_fields`` / ``text_chunks`` / ``chunk_words``: the text fields of the
  captured *documents* whose citation is the object's version handle
  (``ontology.text_index``: ``object_versions.l1_handle = document.citation``). A person's
  handle (``graph-mail://people/person/<key>``, ``graph-mail://<oid>/person/<oid>``) is no
  document's citation (a mail cites its message, an attachment or member its own handle),
  so a Person's text index is empty in a full build too.
* ``search.entities``: ``Person`` is no entity type (``operating.chunking.entities``).
* ``search.chunks``: a record of the ``mail`` / ``mail-any`` audience families is left to
  its documents' chunks (``record_text.json`` ``document_families``).
* Its values carry their own words (``value_words``, over the served ``value_cells``):
  already served, under the object's audience and markings.

So re-indexing a Person from its served values writes no text row: the rebuilt index is
the empty set, and nothing is exposed that a full build would not. This command checks
that each time and refuses the run when the code says otherwise (``Person`` an entity
type, or a family no longer left to documents), so a later index that does hold a
Person's text is never skipped by a cleared queue entry.

**The people exclusion, now, per person, fail closed** (``operating.chunking.permissions.
load_exclusion``, the guard the search index applies): a queued Person leaves the queue
only when the guard loaded and clears

* its audience unit: ``mail:<oid>`` the mailbox's id; ``mail-any:<bitmap>.<check>``
  decoded against the mail capture's mailboxes now (``people_directory.decode_audience``)
  and every mailbox it names (a key that no longer decodes is undecided);
* every identity its served current version states (``row.object_id``, ``row.upn``, each
  of ``row.addresses``) and a staff person's handle id; at least one must be stated.

An excluded, undecided or unreadable person (or a queued object that is no current
people-directory Person of one version, or holds text fields) stays queued and is only
counted, never named; nothing of it is written. The served rows of everyone are left
as they are: the only write is ``DELETE FROM ontology.reproject`` of the cleared.

**Digests**: the run refuses unless ``ontology_migrate.check`` passes (served schema and
steps digests are this tree's) and, given ``--expect-steps`` / ``--expect-schema``, the
served ones start with them. No ``store_meta`` key, schema or steps digest is written
(``ontology.reproject`` is in no content digest), so no train is needed. Each range's
delete re-reads both digests under the delta's advisory lock and refuses on a change.

**Parallel**: the queued Persons split into ``--workers`` ranges (``hashtext(object_id)``),
one process and connection each: one read of its persons and one of their values, judged
in memory, then one transaction under the lock (the same lock the apply and delta take)
deleting the cleared rows whose queue entry, current version and audience are still those
judged (a row changed since stays). Before a range deletes, its rows are fsynced to
``WORK/undo-<i>-of-<n>.jsonl``; ``undo`` puts them back.

    python3 -m operating.pg.people_reindex run --dsn DSN --state-dir /data/plx-state \\
        --work DIR (--dry-run | --apply) [--workers N] [--expect-steps steps:bf06102e]
    python3 -m operating.pg.people_reindex undo --dsn DSN --work DIR

Output is one JSON report (counts only; the DSN is never printed); also ``WORK/report.json``.
Exit 0 done, 1 refused, 2 bad usage. Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

LOCK = "SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))"   # ontology_migrate.LOCK
BINDING = "graph_mail.people"
TYPE = "Person"
IDENTITY_PATHS = ("row.object_id", "row.upn", "row.addresses")
PERSON_FAMILIES = ("mail", "mail-any")
READ_TIMEOUT_MS = 300_000
LOCK_TIMEOUT_MS = 900_000     # a delta cycle may hold the lock for minutes; a rerun resumes

# why a queued Person stays (counted, never named)
STAYS = ("not_people_binding", "not_one_current_version", "not_person_handle",
         "missing_from_source", "text_fields_present", "unit_excluded_or_undecided",
         "no_identity", "identity_unreadable", "identity_excluded_or_undecided",
         "changed_since_read")


class Refused(RuntimeError):
    pass


# ----------------------------------------------------------------- the rules

def index_rows_expected() -> list[str]:
    """What in this tree's code would index a Person's text beyond its values (empty:
    the rebuilt index of a Person is no row)."""
    from operating.chunking.entities import ENTITY_TYPES
    from operating.chunking.records import load_config
    out = []
    if TYPE in ENTITY_TYPES:
        out.append("search.entities")
    families = set(load_config().get("document_families") or ())
    out.extend(f"search.chunks:{f}" for f in PERSON_FAMILIES if f not in families)
    return out


def identities(values: Iterable[tuple[str, Any]]) -> list[str] | None:
    """Every identity the version states (``(field_path, value)`` pairs); None when a
    stated one cannot be read (an addresses list that is no JSON list of strings)."""
    out: list[str] = []
    for path, value in values:
        if value is None:
            continue
        if path == "row.addresses":
            try:
                held = json.loads(value)
            except (TypeError, ValueError):
                return None
            if not isinstance(held, list) or not all(isinstance(a, str) for a in held):
                return None
            out.extend(a for a in held if a.strip())
        elif isinstance(value, str) and value.strip():
            out.append(value)
    return out


def verdict(row: Mapping[str, Any], values: Sequence[tuple[str, Any]], exclusion: Any,
            mailboxes: Sequence[str]) -> str | None:
    """None when the queued Person may leave the queue, else the ``STAYS`` reason."""
    from ontology.people_directory import decode_audience, parse_person_handle
    from integrations.people_exclusion import is_object_id
    if row["binding_id"] != BINDING:
        return "not_people_binding"
    if row["current_versions"] != 1:
        return "not_one_current_version"
    parsed = parse_person_handle(row["l1_handle"])
    if parsed is None:
        return "not_person_handle"
    if row["missing"] is not None:
        return "missing_from_source"
    if row["text_fields"]:
        return "text_fields_present"
    if exclusion is None or exclusion.unavailable:
        return "unit_excluded_or_undecided"
    key = row["audience_key"]
    try:
        if not isinstance(key, str) or key.split(":", 1)[0] not in PERSON_FAMILIES \
                or not exclusion.unit_clear(key):
            return "unit_excluded_or_undecided"
        if key.startswith("mail-any:"):
            named = decode_audience(key, mailboxes)
            if not named or not all(exclusion.identity_clear(m) for m in named):
                return "unit_excluded_or_undecided"
    except Exception:  # noqa: BLE001 - an undecidable guard withholds
        return "unit_excluded_or_undecided"
    stated = identities(values)
    if stated is None:
        return "identity_unreadable"
    mailbox, handle_key = parsed
    if mailbox is not None:   # a staff person: the handle names its own object id
        if not is_object_id(handle_key):
            return "not_person_handle"
        stated.append(handle_key)
    if not stated:
        return "no_identity"
    try:
        if not all(exclusion.identity_clear(i) for i in stated):
            return "identity_excluded_or_undecided"
    except Exception:  # noqa: BLE001
        return "identity_excluded_or_undecided"
    return None


# ----------------------------------------------------------------- a range

_PERSONS = """
SELECT r.object_id, r.migration, r.since, o.binding_id, o.audience_key,
       o.missing_from_source_since AS missing,
       (SELECT count(*) FROM ontology.object_versions v
         WHERE v.object_id = r.object_id AND v.superseded_at IS NULL) AS current_versions,
       (SELECT max(v.ver) FROM ontology.object_versions v
         WHERE v.object_id = r.object_id AND v.superseded_at IS NULL) AS ver,
       (SELECT max(v.l1_handle) FROM ontology.object_versions v
         WHERE v.object_id = r.object_id AND v.superseded_at IS NULL) AS l1_handle,
       EXISTS (SELECT 1 FROM ontology.text_fields f WHERE f.object_id = r.object_id)
         AS text_fields
  FROM ontology.reproject r JOIN ontology.objects o ON o.object_id = r.object_id
 WHERE o.object_type = %s AND abs(hashtext(r.object_id)::bigint) %% %s = %s
 ORDER BY r.object_id"""
# every value source of the current version (a superseded source too: more identities
# only ever withhold more)
_VALUES = """
SELECT s.ver, f.field_path, c.value
  FROM ontology.value_sources s JOIN ontology.value_cells c ON c.src = s.src
  JOIN ontology.value_fields f ON f.field = c.field
 WHERE s.ver = ANY(%s) AND f.field_path = ANY(%s)"""
_DIGESTS = ("SELECT key, value FROM ontology.store_meta WHERE key IN "
            "('schema_digest', 'steps_digest')")
_CLEAR = """
DELETE FROM ontology.reproject r USING cleared c
 WHERE r.object_id = c.object_id AND r.migration = c.migration AND r.since = c.since
   AND EXISTS (SELECT 1 FROM ontology.objects o WHERE o.object_id = c.object_id
               AND o.audience_key = c.audience_key AND o.missing_from_source_since IS NULL)
   AND EXISTS (SELECT 1 FROM ontology.object_versions v WHERE v.object_id = c.object_id
               AND v.ver = c.ver AND v.superseded_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM ontology.object_versions v WHERE v.object_id = c.object_id
               AND v.ver <> c.ver AND v.superseded_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM ontology.text_fields f WHERE f.object_id = c.object_id)
RETURNING r.object_id"""

_SHARED: dict[str, Any] = {}    # set in the parent before the workers fork


def _digests(cur: Any) -> dict[str, str]:
    cur.execute(_DIGESTS)
    return {k: v for k, v in cur.fetchall()}


def run_range(index: int) -> dict[str, Any]:
    import psycopg
    from psycopg.rows import dict_row
    shared = _SHARED
    exclusion, mailboxes, parts = shared["exclusion"], shared["mailboxes"], shared["parts"]
    started = time.monotonic()
    counts: dict[str, int] = {"queued": 0, "cleared": 0, **dict.fromkeys(STAYS, 0)}
    if exclusion is not None and exclusion.fingerprint() != shared["fingerprint"]:
        raise Refused("exclusion_changed_in_worker")
    with psycopg.connect(shared["dsn"], connect_timeout=10,
                         application_name="plx-people-reindex") as pg:
        with pg.cursor(row_factory=dict_row) as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = {READ_TIMEOUT_MS}")
            cur.execute(_PERSONS, (TYPE, parts, index))
            rows = cur.fetchall()
            values: dict[int, list[tuple[str, Any]]] = {}
            vers = [r["ver"] for r in rows if r["ver"] is not None]
            if vers:
                cur.execute(_VALUES, (vers, list(IDENTITY_PATHS)))
                for v in cur.fetchall():
                    values.setdefault(v["ver"], []).append((v["field_path"], v["value"]))
        pg.rollback()
        counts["queued"] = len(rows)
        cleared = []
        for row in rows:
            why = verdict(row, values.get(row["ver"], ()), exclusion, mailboxes)
            if why is None:
                cleared.append(row)
            else:
                counts[why] += 1
        del values
        if not shared["apply"] or not cleared:
            if not shared["apply"]:
                counts["would_clear"] = len(cleared)
            counts["seconds"] = round(time.monotonic() - started, 2)
            return counts
        undo = Path(shared["work"]) / f"undo-{index}-of-{parts}.jsonl"
        with undo.open("a", encoding="utf-8") as out:
            for row in cleared:
                out.write(json.dumps({"object_id": row["object_id"],
                                      "migration": row["migration"],
                                      "since": row["since"].isoformat()}) + "\n")
            out.flush()
            os.fsync(out.fileno())
        with pg.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}")
            cur.execute(LOCK)
            if _digests(cur) != shared["digests"]:
                raise Refused("served_digests_changed")
            cur.execute("CREATE TEMP TABLE cleared (object_id text PRIMARY KEY, migration "
                        "text NOT NULL, since timestamptz NOT NULL, audience_key text NOT "
                        "NULL, ver bigint NOT NULL) ON COMMIT DROP")
            with cur.copy("COPY cleared (object_id, migration, since, audience_key, ver) "
                          "FROM STDIN") as copy:
                for row in cleared:
                    copy.write_row((row["object_id"], row["migration"], row["since"],
                                    row["audience_key"], row["ver"]))
            cur.execute(_CLEAR)
            done = len(cur.fetchall())
        pg.commit()
    counts["cleared"] = done
    counts["changed_since_read"] = len(cleared) - done
    counts["seconds"] = round(time.monotonic() - started, 2)
    return counts


# ----------------------------------------------------------------- the run

def load_guard(state_dir: str | Path) -> tuple[Any, list[str]]:
    """The search index's exclusion and the mail capture's mailbox ids now; refused when
    the mail capture cannot be read (no person's units or addresses can be decided)."""
    import sqlite3
    from integrations.people_exclusion import environ_with_exclusion
    from ontology.people_directory import mailboxes
    from operating.chunking.permissions import load_exclusion
    path = Path(state_dir) / "mail-capture" / "capture.db"
    if not path.is_file():
        raise Refused("mail_capture_unreadable")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        try:
            boxes = [b.mailbox_id for b in mailboxes(conn)]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise Refused("mail_capture_unreadable") from exc
    if not boxes:
        raise Refused("mail_capture_no_mailboxes")
    try:
        return load_exclusion(state_dir, environ_with_exclusion()), boxes
    except (OSError, sqlite3.Error) as exc:   # a capture the guard reads cannot be opened
        raise Refused("exclusion_state_unreadable") from exc


def run(dsn: str, work: str | Path, *, exclusion: Any, mailboxes: Sequence[str],
        apply: bool, workers: int | None = None, expect_steps: str | None = None,
        expect_schema: str | None = None, check: Any = None) -> dict[str, Any]:
    """Judge (and with ``apply`` clear) every queued Person; the report (counts only)."""
    import psycopg
    from operating.pg import ontology_migrate
    started = time.monotonic()
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    expected = index_rows_expected()
    if expected:
        raise Refused("person_index_rows_expected:" + ",".join(expected))
    if exclusion is None or exclusion.unavailable:
        # nobody could leave the queue; an apply would only count
        raise Refused(f"exclusion_unavailable:{getattr(exclusion, 'unavailable', None)}")
    verdict_ = (check or ontology_migrate.check)(dsn)
    if not verdict_.get("ok"):
        raise Refused(f"served_digests:{verdict_.get('reason')}")
    with psycopg.connect(dsn, connect_timeout=10) as pg, pg.cursor() as cur:
        digests = _digests(cur)
    if set(digests) != {"schema_digest", "steps_digest"}:
        raise Refused("served_digests_absent")
    if expect_steps and not digests["steps_digest"].startswith(expect_steps):
        raise Refused("served_steps_digest_not_expected")
    if expect_schema and not digests["schema_digest"].startswith(expect_schema):
        raise Refused("served_schema_digest_not_expected")
    parts = max(1, int(workers or os.cpu_count() or 1))
    _SHARED.clear()
    _SHARED.update(dsn=dsn, exclusion=exclusion, mailboxes=list(mailboxes), parts=parts,
                   apply=apply, work=str(work), digests=digests,
                   fingerprint=exclusion.fingerprint())
    try:
        if parts == 1:
            results = [run_range(0)]
        else:
            from ontology import parent_watch
            with multiprocessing.get_context("fork").Pool(parts, **parent_watch.pool_init()) as pool:
                results = pool.map(run_range, range(parts))
    finally:
        _SHARED.clear()
    total: dict[str, Any] = {}
    for part in results:
        for k, v in part.items():
            if k != "seconds":
                total[k] = total.get(k, 0) + v
    report = {"mode": "apply" if apply else "dry-run", "ranges": parts,
              "served": {k: v[:20] for k, v in digests.items()},
              "exclusion": exclusion.fingerprint()[:16], "mailboxes": len(mailboxes),
              "text_rows_written": 0, **total,
              "slowest_range_s": max(p["seconds"] for p in results),
              "seconds": round(time.monotonic() - started, 2)}
    with psycopg.connect(dsn, connect_timeout=10) as pg, pg.cursor() as cur:
        if _digests(cur) != digests:
            raise Refused("served_digests_changed")
        cur.execute("SELECT count(*) FROM ontology.reproject r JOIN ontology.objects o "
                    "ON o.object_id = r.object_id WHERE o.object_type = %s", (TYPE,))
        report["still_queued"] = cur.fetchone()[0]
    # report.json: the last run; dry-run.json: the last dry run (the script's --apply
    # requires one of the digests served now)
    for name in ("report.json",) + (() if apply else ("dry-run.json",)):
        tmp = work / f"{name}.tmp"
        tmp.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(work / name)
    return report


def undo(dsn: str, work: str | Path) -> dict[str, Any]:
    """Put every queue entry this command cleared back (one already queued again keeps
    its own row)."""
    import psycopg
    rows = []
    for path in sorted(Path(work).glob("undo-*-of-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                held = json.loads(line)
                rows.append((held["object_id"], held["migration"], held["since"]))
    with psycopg.connect(dsn, connect_timeout=10) as pg, pg.cursor() as cur:
        cur.execute(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}")
        cur.execute(LOCK)
        restored = 0
        for start in range(0, len(rows), 5000):
            cur.executemany("INSERT INTO ontology.reproject (object_id, migration, since) "
                            "VALUES (%s, %s, %s) ON CONFLICT (object_id) DO NOTHING",
                            rows[start:start + 5000])
            restored += max(cur.rowcount, 0)
    return {"logged": len(rows), "restored": restored}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.people_reindex")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dsn", required=True)
    r.add_argument("--state-dir", required=True)
    r.add_argument("--work", required=True)
    mode = r.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    r.add_argument("--workers", type=int, default=None)
    r.add_argument("--expect-steps", default=None)
    r.add_argument("--expect-schema", default=None)
    r.add_argument("--after-dry-run", default=None,
                   help="refuse unless this dry run's served digests are served now")
    u = sub.add_parser("undo")
    u.add_argument("--dsn", required=True)
    u.add_argument("--work", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "undo":
            out = undo(args.dsn, args.work)
        else:
            if args.after_dry_run:
                try:
                    seen = json.loads(Path(args.after_dry_run).read_text())["served"]
                except (OSError, ValueError, KeyError) as exc:
                    raise Refused("dry_run_report_unreadable") from exc
                steps, schema = seen.get("steps_digest"), seen.get("schema_digest")
                if not steps or not schema or not steps.startswith(args.expect_steps or ""):
                    raise Refused("dry_run_of_other_digests")
                # the digests the dry run judged under (stricter than the given prefix)
                args.expect_steps, args.expect_schema = steps, schema
            exclusion, boxes = load_guard(args.state_dir)
            out = run(args.dsn, args.work, exclusion=exclusion, mailboxes=boxes,
                      apply=args.apply, workers=args.workers,
                      expect_steps=args.expect_steps, expect_schema=args.expect_schema)
    except Refused as exc:
        print(json.dumps({"refused": str(exc)}))
        return 1
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
