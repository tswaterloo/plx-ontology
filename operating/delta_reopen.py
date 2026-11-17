"""Reopen in the delta the mail records whose served objects no cycle will revisit (CAP-L96).

A delta cycle finds changed mail from the capture store's own marks: versions past its
scan position, attachment texts and message events since (``delta.mail_changes``). A
served Email or Attachment that is still current (no ``missing_from_source_since``)
although the store says it is gone stays that way when nothing about it is new to the
discovery. This happens when the projection that closed it ran before the rule that marks
it missing (an attachment the message's complete current listing dropped, 59c16c0fcc), or
when its layer was cut. Live 11-15: 101 attachments dropped by their message's current
version and 10 of messages deleted on 11-04, all left current.

:func:`stale_mail_records` takes the served objects that are still current
(``(binding_id, native_key)``) and returns the mail records a full build would mark
missing:

* ``gone``: the message's newest version is a removal (``removed_reason``). Every object
  of the message is missing from the source.
* ``dropped``: an attachment absent from the message's current version, when that version
  is no removal and its listing reached its end (``attachments_status = 'complete'``).

A message the store holds no version of is left alone and only counted (``absent``).
Removing those is the purge path (``operating.mail_exclusion_purge``). So is a truncated
listing: like the projection, it says nothing about an attachment.

:func:`reopen` opens those records in the delta state (``State.add``, as discovery does).
The next cycle projects them with the one projector and the layer replaces their served
rows. Nothing else is written. A mailbox the people exclusion names is never opened, and an
exclusion that is unavailable or empty opens nothing (fail closed), as in ``build_delta``.

``python3 -m operating.delta_reopen --state-dir D [--apply]`` reads the served objects from
Postgres (``PLX_PG_DSN``, a read-only transaction) and the mail store read-only. It prints
one JSON line of counts (never a key or an address) and opens the records only with
``--apply``. It is safe beside a running cycle: a record reopened while a cycle runs stays
open for the next one (``State.close_over``).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping

MAIL_BINDINGS = ("graph_mail.messages", "graph_mail.attachments")


def _message_key(binding: str, native: Mapping[str, Any]) -> tuple[str, str] | None:
    mailbox = native.get("mailbox")
    message = native.get("id") if binding == "graph_mail.messages" else native.get("message_id")
    if not isinstance(mailbox, str) or not isinstance(message, str) or not mailbox or not message:
        return None
    return mailbox, message


def stale_mail_records(capture: sqlite3.Connection,
                       served: Iterable[tuple[str, str]]) -> tuple[set[tuple[str, str]],
                                                                   dict[str, int]]:
    """(the mail records to project again, counts) for ``served``: the ``(binding_id,
    native_key JSON)`` of every served mail object that is still current (module
    docstring). Keys are the store's own ``(mailbox_id, message_id)``."""
    boxes = {str(r[0]).lower(): r[0] for r in capture.execute("SELECT mailbox_id FROM mailboxes")}
    wanted: dict[tuple[str, str], set[str | None]] = {}   # message -> attachment ids (None: Email)
    counts = {"served": 0, "unreadable_keys": 0, "messages": 0, "absent": 0, "gone": 0,
              "dropped": 0, "truncated_listing": 0, "records": 0}
    for binding, native_json in served:
        if binding not in MAIL_BINDINGS:
            continue
        counts["served"] += 1
        try:
            native = json.loads(native_json)
        except (TypeError, ValueError):
            native = None
        key = _message_key(binding, native) if isinstance(native, dict) else None
        attachment = native.get("attachment_id") if key and binding.endswith(".attachments") else None
        if key is None or (binding.endswith(".attachments") and not isinstance(attachment, str)):
            counts["unreadable_keys"] += 1
            continue
        # one message whatever casing its objects' keys spell the mailbox in
        wanted.setdefault((key[0].lower(), key[1]), set()).add(attachment)
    out: set[tuple[str, str]] = set()
    for (mailbox, message), attachments in wanted.items():
        counts["messages"] += 1
        box = boxes.get(mailbox)
        newest = None if box is None else capture.execute(
            "SELECT l1_handle, superseded_at, removed_reason, attachments_status FROM messages "
            "WHERE mailbox_id = ? AND message_id = ? ORDER BY version_id DESC LIMIT 1",
            (box, message)).fetchone()
        if newest is None:
            counts["absent"] += 1
            continue
        handle, superseded, removed, status = newest
        if removed is not None:
            counts["gone"] += 1
            out.add((box, message))
            continue
        if superseded is not None:   # a newest interval closed with no removal: not decided here
            continue
        ids = {a for a in attachments if a is not None}
        if not ids:
            continue
        if status != "complete":
            counts["truncated_listing"] += 1
            continue
        listed = {r[0] for r in capture.execute(
            "SELECT attachment_id FROM attachments WHERE message_handle = ?", (handle,))}
        if ids - listed:
            counts["dropped"] += 1
            out.add((box, message))
    counts["records"] = len(out)
    return out, counts


def reopen(state_path: Path, records: Iterable[tuple[str, str]], exclusion: Any
           ) -> dict[str, int]:
    """Open ``records`` in the delta state at ``state_path``, leaving out every mailbox
    ``exclusion`` names. If ``exclusion`` is None, unavailable or empty, nothing is
    opened."""
    from ontology import delta
    records = sorted(set(records), key=delta._order)
    if exclusion is None or exclusion.unavailable or not exclusion.count:
        return {"opened": 0, "excluded": 0, "refused": len(records)}
    decided: dict[str, bool] = {}   # mailbox -> may be opened
    for mailbox in {k[0] for k in records}:
        try:
            decided[mailbox] = bool(exclusion.decides(mailbox)) and \
                not exclusion.is_excluded(mailbox)
        except Exception:  # noqa: BLE001 - a mailbox the guard cannot decide is left out
            decided[mailbox] = False
    kept = [k for k in records if decided[k[0]]]
    state = delta.State(state_path)
    try:
        with state.transaction():
            opened = state.add("graph_mail", kept)
    finally:
        state.close()
    return {"opened": opened, "excluded": len(records) - len(kept), "refused": 0}


def _served_from_postgres(dsn: str) -> Iterable[tuple[str, str]]:
    import psycopg
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        with conn.cursor(name="cap_l96_served") as cur:
            cur.itersize = 50_000
            cur.execute("SELECT binding_id, native_key FROM ontology.objects WHERE binding_id "
                        "= ANY(%s) AND missing_from_source_since IS NULL", (list(MAIL_BINDINGS),))
            yield from cur


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    from integrations.people_exclusion import PeopleExclusion
    from operating.pg import config as pg_config
    environ = os.environ if environ is None else environ
    p = argparse.ArgumentParser(prog="python3 -m operating.delta_reopen",
                                description=__doc__.split("\n", 1)[0])
    p.add_argument("--state-dir", required=True, type=Path)
    p.add_argument("--delta-dir", type=Path, help="default <state dir>/projection/delta")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args(argv)
    delta_dir = args.delta_dir or args.state_dir / "projection" / "delta"
    store = args.state_dir / "mail-capture" / "capture.db"
    capture = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=300)
    try:
        records, counts = stale_mail_records(capture, _served_from_postgres(pg_config.dsn(environ)))
    finally:
        capture.close()
    report: dict[str, Any] = {"apply": args.apply, **counts}
    if args.apply:
        try:
            exclusion = PeopleExclusion.from_env(environ)
        except Exception as exc:  # noqa: BLE001 - fail closed: nothing opened
            report["exclusion"] = f"unreadable:{type(exc).__name__}"
            exclusion = None
        report.update(reopen(delta_dir / "state.sqlite", records, exclusion))
    print(json.dumps(report, sort_keys=True))
    return 0 if not report.get("refused") else 3


if __name__ == "__main__":
    sys.exit(main())
