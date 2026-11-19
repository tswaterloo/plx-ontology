"""The people directory kept over delta layers: an outside contact first seen after the
last full build gets its served ``Person``, and a served outside contact whose mail changed
gets the ``Person`` a full build of the capture now makes of it (option
1).

**Why**: ``ontology.people_directory`` makes an outside contact from a sender address of
the whole capture, and its audience from every mailbox it wrote to; a delta layer holds
only its own mail, so no layer owns a ``Person`` and the served directory stayed the last
full build's. Mail from or to a contact first seen since then linked to no Person; a
contact who then wrote to another mailbox stayed hidden from that mailbox's readers, and
one whose mail in a mailbox was removed stayed visible to its readers.

**What** (:func:`recompute`, a whole-store step with a mark of its own, run after the
layers of a cycle and before the person links' run, ``operating.pg.ontology_delta``):

1. the mail the layers replaced since its mark (``store_meta`` :data:`STATE`, the newest
   ``ontology.applied_owned`` row covered; none: every layer since the load), at most
   :data:`RUN_MAX` mails a run (a catch-up drains over cycles, ``remaining``);
2. their senders as the capture holds them now (the current message, read by its id, the
   census's own ``lower(trim(from))`` and its undecided-address filter);
3. **new contacts**: of those, the addresses whose ``Person`` Postgres lacks that the
   directory's own rule (``people_directory.people``) makes an outside contact of: the rule
   judged over the capture's whole ``mailboxes`` table with the address's census over
   **every** mailbox (current messages from it per mailbox, read on the capture's
   ``messages_by_sender`` index; unkeyed rows the slow way, as the capture's own person
   read does), so its audience (the mailboxes it wrote to), its citation and capture times
   (the mailbox holding most of its mail) are the full build's: the rule gives an
   address's record from the mailboxes and that address's census alone. No second rule:
   the same function, the same exclusion, the same internal domains as the delta's
   projection;
4. **audience refresh**: the served outside contacts whose census these mails can move
   (the sender of any version of each mail the capture holds, current or removed, and the
   Person each mail's served ``Email.sent_by_person`` link names: a mail removed from the
   capture keeps that link until the person links' run), at most :data:`REFRESH_MAX` a run
   (the rest queued in the state, ``refresh``, taken first next run), judged by the same
   rule over the same whole-capture census. Each one's projected rows (object row:
   audience; versions: citation, markings, times, values) are compared with the served
   ones; only those that differ are owned by the layer: re-projected (audience widened,
   narrowed or moved, as the census says), or **removed** when the rule makes no outside
   contact of the address any more (its mail gone, or it became a withheld or excluded
   person's), as a full build of the capture would hold no Person. A served contact
   holding a row the layer would not write again (a link of no whole-store step, an event)
   is held back (``refresh_held_foreign``): the apply would delete that row;
5. those Persons projected by the one projector (``ontology.project.build_store`` over the
   capture's ``people`` relation narrowed to them) into a layer of their own
   (``people-directory-<mark>-<ms>``, owning exactly them and the removed ones, over the
   served generation's digest), applied by the layer apply (``ontology_delta.apply_layer``:
   rows, markings, audience, change feed, ``applied_owned``), so the person links' run then
   re-judges every object linked to a Person the layer replaced, and the alias links;
6. the objects naming one of the new contacts' addresses (``operating.pg.people_scope``'s
   source types, found through the values' word index ``value_cells_words``, at most
   :data:`NAMING_MAX` an address, counted ``naming_capped`` past it: left to the person
   links' backfill) judged again by ``people_scope.rejudge``; the mark moves last.

**Crash safety**: the state names the new contacts (``naming``) and the queued refresh
before the layer is applied, so a run stopped anywhere after that judges the objects naming
them on its next run (a contact whose layer never committed is named by nothing: no link);
the mark moves only when the run finished.

**Excluded people fail closed**: without the people exclusion (or with the mail scope's
internal domains unreadable, or a capture holding no mailbox) nothing is judged or written
and the mark stays (``Refused``); the rule withholds an excluded person's addresses and
mailboxes as the build does, so a refreshed audience never names an excluded person's
mailbox and an excluded person's address loses its Person.

**Settings**: read from the ``environ`` mapping given (the delta's own settings,
``ontology.delta.build_delta`` -> ``apply_listed``), never the process environment
directly: ``PLX_PEOPLE_DIRECTORY_RUN_MAX``, ``PLX_PEOPLE_DIRECTORY_REFRESH_MAX``,
``PLX_DELTA_MAIL_SCOPE``, the exclusion's.

**Digests**: none moves; no step's, derivation's or key's code (``ontology/*``: only
``ontology.delta.create_owned``, the layer table's one DDL, which no digest lists).

    python3 -m operating.pg.people_directory_scope run --state-dir DIR [--dsn DSN]
        [--dry-run]

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence

from operating.pg import people_scope

STATE = "whole_steps:people_directory"     # store_meta key: {"through": seq, ...}
LAYER_PREFIX = "people-directory-"
MAIL_TYPE = "Email"
MAIL_PREFIX = "Email:graph_mail.messages:"
SENT_BY = "Email.sent_by_person"
RUN_MAX = 50_000          # mails a run reads the senders of
REFRESH_MAX = 1_000       # served outside contacts a run judges the audience of again
NAMING_MAX = 5_000        # objects naming one new address judged again
# an outside contact's Person (its key: the address key, a sha256); a staff Person's key is
# its Entra object id
_OUTSIDE = re.compile(r'Person:graph_mail\.people:\{"person_key":"[0-9a-f]{64}"\}')


class Refused(RuntimeError):
    """The run would not judge what a full build judges: nothing is written."""


def _setting(environ: Mapping[str, str], name: str, default: int) -> int:
    try:
        return max(1, int(environ.get(name) or default))
    except ValueError:
        return default


def _run_max(environ: Mapping[str, str]) -> int:
    return _setting(environ, "PLX_PEOPLE_DIRECTORY_RUN_MAX", RUN_MAX)


def _refresh_max(environ: Mapping[str, str]) -> int:
    return _setting(environ, "PLX_PEOPLE_DIRECTORY_REFRESH_MAX", REFRESH_MAX)


def person_id(key: str) -> str:
    """The ``Person`` object id of a people key (the projector's own)."""
    from ontology.project import object_id_of
    return object_id_of("Person", "graph_mail.people", {"person_key": key})


def is_outside(object_id: Any) -> bool:
    """Whether ``object_id`` is an outside contact's ``Person`` id."""
    return isinstance(object_id, str) and _OUTSIDE.fullmatch(object_id) is not None


def message_of(object_id: str) -> tuple[str, str] | None:
    """(mailbox, message id) of an ``Email`` object id, else None."""
    if not object_id.startswith(MAIL_PREFIX):
        return None
    try:
        key = json.loads(object_id[len(MAIL_PREFIX):])
    except ValueError:
        return None
    if not isinstance(key, dict) or not all(isinstance(key.get(k), str)
                                            for k in ("id", "mailbox")):
        return None
    return key["mailbox"], key["id"]


def internal_domains(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The delta's internal mail domains (``ontology.delta._internal_domains``' file),
    ``Refused`` when the scope file cannot be read (an own address would pass as an
    outside contact's)."""
    from integrations import capture_mail
    scope = environ.get("PLX_DELTA_MAIL_SCOPE") or capture_mail.DEFAULT_SCOPE
    try:
        return tuple(capture_mail.CaptureScope.from_file(scope).internal_domains)
    except (OSError, ValueError):
        raise Refused("mail_scope_unreadable") from None


# --------------------------------------------------------------------------- the capture

class Capture:
    """The mail capture, read only: its mailboxes, current messages by id, the census of
    an address (``people_directory._CENSUS``'s sender counts, on the sender index)."""

    def __init__(self, state_dir: str | Path) -> None:
        from integrations.capture_mail import _sender_functions, undecided_free_sql
        from ontology import people_directory as pd
        self.path = Path(state_dir) / "mail-capture" / "capture.db"
        if not self.path.is_file():
            raise Refused("mail_capture_missing")
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                    isolation_level=None)
        _sender_functions(self.conn)
        self.conn.execute("BEGIN")   # one snapshot for every read of the run
        self.boxes = pd.mailboxes(self.conn)
        self.free = undecided_free_sql(self.conn, "m")
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        self.keyed = "sender_key" in columns and self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = "
            "'messages_by_sender'").fetchone() is not None

    def close(self) -> None:
        try:
            self.conn.execute("COMMIT")
        finally:
            self.conn.close()

    def senders(self, mails: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
        """lower-case trimmed sender -> the mailboxes of the given current messages from
        it (the census's own form of an address; a message an undecided address names is
        no reader's and counts for nothing)."""
        out: dict[str, set[str]] = {}
        sql = ("SELECT lower(trim(json_extract(m.record, '$.from'))) FROM messages m "
               "INDEXED BY messages_by_id WHERE m.mailbox_id = ? AND m.message_id = ? AND "
               f"m.superseded_at IS NULL AND {self.free}")
        for mailbox, message in mails:
            for (sender,) in self.conn.execute(sql, (mailbox, message)):
                if isinstance(sender, str) and sender:
                    out.setdefault(sender, set()).add(mailbox)
        return out

    def every_sender(self, mails: Iterable[tuple[str, str]]) -> set[str]:
        """The senders of every version the capture holds of the given messages, current,
        superseded or removed: each address whose census the change of one of them can
        have moved."""
        out: set[str] = set()
        sql = ("SELECT DISTINCT lower(trim(json_extract(m.record, '$.from'))) FROM messages "
               "m INDEXED BY messages_by_id WHERE m.mailbox_id = ? AND m.message_id = ?")
        for mailbox, message in mails:
            for (sender,) in self.conn.execute(sql, (mailbox, message)):
                if isinstance(sender, str) and sender:
                    out.add(sender)
        return out

    def census(self, senders: Iterable[str]) -> Counter:
        """(sender, mailbox) -> current messages from it there, over every mailbox: the
        sender part of ``people_directory.sender_counts`` for these addresses."""
        from ontology.people_directory import address_key
        counts: Counter = Counter()
        wanted = {address_key(s): s for s in senders}
        from_of = "lower(trim(json_extract(m.record, '$.from')))"
        for key, sender in sorted(wanted.items()):
            for box in self.boxes:
                if self.keyed:
                    rows = [self.conn.execute(
                        f"SELECT {from_of}, count(*) FROM messages m INDEXED BY "
                        "messages_by_sender WHERE m.mailbox_id = ? AND m.sender_key = ? AND "
                        f"m.superseded_at IS NULL AND {self.free} GROUP BY 1",
                        (box.mailbox_id, key)).fetchall(),
                        self.conn.execute(
                        f"SELECT {from_of}, count(*) FROM messages m INDEXED BY "
                        "messages_by_sender WHERE m.mailbox_id = ? AND m.sender_key IS NULL "
                        f"AND m.superseded_at IS NULL AND plx_address_key({from_of}) = ? AND "
                        f"{self.free} GROUP BY 1", (box.mailbox_id, key)).fetchall()]
                else:
                    rows = [self.conn.execute(
                        f"SELECT {from_of}, count(*) FROM messages m WHERE m.mailbox_id = ? "
                        f"AND m.superseded_at IS NULL AND plx_address_key({from_of}) = ? AND "
                        f"{self.free} GROUP BY 1", (box.mailbox_id, key)).fetchall()]
                for part in rows:
                    for found, n in part:
                        if found == sender and n:
                            counts[(sender, box.mailbox_id)] += int(n)
        return counts


def outside_people(capture: Capture, counts: Mapping[tuple[str, str], int], guard: Any,
                   domains: Sequence[str]) -> list[dict[str, Any]]:
    """The outside contacts ``people_directory.people`` makes of ``counts`` over the
    capture's mailboxes (``Refused`` without the exclusion)."""
    import copy
    from ontology import people_directory as pd
    found, stats = pd.people(capture.boxes, counts, copy.copy(guard), domains)
    if stats.get("exclusion_unavailable"):
        raise Refused("exclusion_unavailable")
    return [item for item in found if item["row"]["kind"] == "outside"]


def _held(cur: Any, ids: Iterable[str]) -> set[str]:
    """Those of ``ids`` Postgres holds an object of."""
    names = sorted(set(ids))
    held: set[str] = set()
    for i in range(0, len(names), 20_000):
        cur.execute("SELECT object_id FROM ontology.objects WHERE object_id = ANY(%s)",
                    (names[i:i + 20_000],))
        held.update(r[0] for r in cur.fetchall())
    return held


def new_people(capture: Capture, cur: Any, mails: Sequence[str], guard: Any,
               domains: Sequence[str], counts: Counter) -> list[dict[str, Any]]:
    """The directory's records (``people_directory.people``) of the outside contacts the
    given mails were sent by that Postgres holds no Person of, with their whole-capture
    census."""
    from ontology import people_directory as pd
    keyed = [m for m in map(message_of, mails) if m is not None]
    senders = capture.senders(keyed)
    counts["senders"] += len(senders)
    # the rule's decision with what these mails show (a current message from it in a
    # mailbox): staff, our own domains, automated, withheld or excluded never go further
    maybe = {item["row"]["addresses"][0] for item in outside_people(
        capture, {(s, b): 1 for s, boxes in senders.items() for b in boxes}, guard, domains)}
    counts["outside_senders"] += len(maybe)
    ids = {person_id(pd.address_key(a)): a for a in maybe}
    held = _held(cur, ids)
    missing = sorted(a for oid, a in ids.items() if oid not in held)
    counts["missing"] += len(missing)
    if not missing:
        return []
    out = outside_people(capture, capture.census(missing), guard, domains)
    return [item for item in out if item["row"]["addresses"][0] in set(missing)]


def refresh_candidates(capture: Capture, cur: Any, mails: Sequence[str]) -> set[str]:
    """The served outside contacts whose census the given mails can have moved: the sender
    of any version of each (the capture's), and the Person each mail's served
    ``Email.sent_by_person`` link names (a mail the capture no longer holds)."""
    from ontology import people_directory as pd
    keyed = [m for m in map(message_of, mails) if m is not None]
    ids = set()
    for sender in capture.every_sender(keyed):
        found = pd.address(sender)
        if found is not None:
            ids.add(person_id(pd.address_key(found)))
    names = sorted(set(mails))
    for i in range(0, len(names), 5_000):
        # each mail's links read on the from-end index (OFFSET 0 keeps the per-mail read)
        cur.execute("SELECT DISTINCT l.to_object FROM unnest(%s::text[]) AS o(id) CROSS "
                    "JOIN LATERAL (SELECT to_object FROM ontology.link_rows l WHERE "
                    "l.from_object = o.id AND l.link_type = %s OFFSET 0) l",
                    (names[i:i + 5_000], SENT_BY))
        ids.update(r[0] for r in cur.fetchall())
    return _held(cur, {i for i in ids if is_outside(i)})


# --------------------------------------------------------------------------- the layer

def _source(state_dir: Path, domains: Sequence[str], guard: Any,
            items: Sequence[Mapping[str, Any]]) -> Any:
    """The capture as the projector reads it, its ``people`` relation narrowed to
    ``items`` and every other relation empty."""
    import copy
    from ontology import project

    class DirectoryCaptures(project.MailCaptures):
        def people_records(self) -> tuple[list[dict[str, Any]], Any]:
            return [copy.deepcopy(dict(i)) for i in items], Counter()

        def records(self, relations: Sequence[str]) -> Any:
            super().records(relations)
            inner = self._stream

            def stream(relation: str, ocr: bool = False, sender: bool = False
                       ) -> Iterator[Any]:
                return inner(relation) if relation == "people" else iter(())
            self._stream = stream
            return project._Relations(relations, stream)

        def documents(self) -> Iterator[Any]:
            return iter(())

        def document_groups(self) -> Iterator[Any]:
            return iter(())

    return DirectoryCaptures(state_dir, internal_domains=domains, exclusion=copy.copy(guard))


def build_layer(path: Path, state_dir: Path, domains: Sequence[str], guard: Any,
                items: Sequence[Mapping[str, Any]], generation: str,
                gone: Sequence[str] = ()) -> list[str]:
    """``path``: a delta layer of the Persons of ``items`` projected by the one projector
    over the served generation ``generation`` (its content digest), owning exactly them
    and ``gone`` (Persons the apply removes: the layer holds no row of them). The object
    ids it owns."""
    from ontology import project
    from ontology.delta import create_owned
    from ontology.schema import load_schema
    with _source(state_dir, domains, guard, items) as source:
        project.build_store(path, load_schema(), {"graph_mail": source})
    wanted = {person_id(i["key"]) for i in items}
    with sqlite3.connect(path) as conn:
        made = {r[0] for r in conn.execute("SELECT object_id FROM objects")}
        if made != wanted or set(gone) & made:   # the projection made something else
            raise Refused(f"directory_layer_objects:{len(made ^ wanted)}")
        create_owned(conn)
        owned = sorted(made | set(gone))
        conn.executemany("INSERT INTO delta_owned VALUES (?)", [(o,) for o in owned])
        conn.execute("UPDATE store_meta SET value = ? WHERE key = 'content_digest'",
                     (generation,))
    return owned


def _text(value: Any) -> Any:
    from operating.pg.ontology_load import _clean
    value = _clean(value)
    return None if value is None else str(value)


_OBJECT_COLUMNS = ("object_id", "object_type", "binding_id", "native_key", "audience_key",
                   "first_recorded_at", "missing_from_source_since")


def _layer_rows(path: Path, ids: Sequence[str]) -> dict[str, tuple]:
    """object id -> (its object row, its versions with their signatures) in the layer."""
    from operating.pg import ontology_delta as od
    from operating.pg.ontology_load import _context
    owned = json.dumps(sorted(ids))
    out: dict[str, Any] = {i: [None, []] for i in ids}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        for row in conn.execute(f"SELECT {', '.join(_OBJECT_COLUMNS)} FROM objects WHERE "
                                "object_id IN (SELECT value FROM json_each(:ids))",
                                {"ids": owned}):
            out[row[0]][0] = tuple(map(_text, row))
        for row in od._layer_version_rows(conn, owned, _context(str(path))):
            # rowid, object_id, l1_handle, digest, lineage, markings, recorded_at,
            # superseded_at, sig
            out[row[1]][1].append(tuple(map(_text, row[2:])))
    return {i: (obj, tuple(sorted(vers, key=repr))) for i, (obj, vers) in out.items()}


def _served_rows(cur: Any, ids: Sequence[str]) -> dict[str, tuple]:
    """The same of the served Persons ``ids``, signed as the apply signs them."""
    from operating.pg import ontology_delta as od
    out: dict[str, Any] = {i: [None, []] for i in ids}
    cur.execute(f"SELECT {', '.join(_OBJECT_COLUMNS)} FROM ontology.objects WHERE "
                "object_id = ANY(%s)", (list(ids),))
    for row in cur.fetchall():
        out[row[0]][0] = tuple(map(_text, row))
    cur.execute(f"SELECT s.object_id, s.l1_handle, s.digest, s.lineage, s.markings, "
                f"s.recorded_at, v.superseded_at, s.sig FROM ({od._pg_signatures()}) s JOIN "
                "ontology.object_versions v ON v.ver = s.ver", {"ids": list(ids)})
    for row in cur.fetchall():
        out[row[0]][1].append(tuple(map(_text, row[1:])))
    return {i: (obj, tuple(sorted(vers, key=repr))) for i, (obj, vers) in out.items()}


def _foreign(cur: Any, path: Path | None, ids: Sequence[str]) -> set[str]:
    """Those of the served Persons ``ids`` holding a row the apply of the layer at ``path``
    (None: a layer holding none of them) would delete without writing it again: a link of
    no whole-store step with an end at it, an event naming it."""
    from operating.pg import whole_steps
    if not ids:
        return set()
    names = list(ids)
    served: dict[str, set[str]] = {}
    for end in ("from_object", "to_object"):
        cur.execute(f"SELECT l.{end}, l.link_id FROM unnest(%s::text[]) AS o(id) CROSS JOIN "
                    f"LATERAL (SELECT {end}, link_id FROM ontology.link_rows l WHERE "
                    f"l.{end} = o.id AND {whole_steps.pg_excluded()} OFFSET 0) l", (names,))
        for oid, link in cur.fetchall():
            served.setdefault(oid, set()).add("l:" + link)
    cur.execute("SELECT object_id, event_id FROM ontology.event_objects WHERE "
                "object_id = ANY(%s)", (names,))
    for oid, event in cur.fetchall():
        served.setdefault(oid, set()).add("e:" + str(event))
    if not served:
        return set()
    layer: dict[str, set[str]] = {}
    if path is not None:
        owned = json.dumps(sorted(served))
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            for end in ("from_object", "to_object"):
                for oid, link in conn.execute(
                        f"SELECT {end}, link_id FROM links WHERE {end} IN (SELECT value FROM "
                        f"json_each(:ids)) AND {whole_steps.sqlite_excluded()}",
                        {"ids": owned}):
                    layer.setdefault(oid, set()).add("l:" + link)
            for oid, event in conn.execute(
                    "SELECT object_id, event_id FROM event_objects WHERE object_id IN "
                    "(SELECT value FROM json_each(:ids))", {"ids": owned}):
                layer.setdefault(oid, set()).add("e:" + str(event))
    return {oid for oid, rows in served.items() if not rows <= layer.get(oid, set())}


def _mailboxes_of(key: Any, every: Sequence[str]) -> frozenset[str] | None:
    """The mailboxes an outside contact's audience key names (None: not decodable now)."""
    from ontology import people_directory as pd
    if isinstance(key, str) and key.startswith("mail:"):
        return frozenset({key[len("mail:"):]})
    chosen = pd.decode_audience(key, every)
    return frozenset(chosen) if chosen else None


def refresh(capture: Capture, cur: Any, ids: Sequence[str], guard: Any,
            domains: Sequence[str], state_dir: Path, generation: str | None, folder: Path,
            counts: Counter) -> tuple[list[dict[str, Any]], list[str]]:
    """(records to project again, Persons to remove) of the served outside contacts
    ``ids``: each judged by the rule over its whole-capture census, kept out when its
    projection equals the served rows, held back when the apply would delete a row it does
    not write again. Counts ``widened``, ``narrowed``, ``moved`` (mailboxes both gained and
    lost), ``same_mailboxes`` (citation or times), ``removed``."""
    from ontology import people_directory as pd
    if not ids:
        return [], []
    addresses: dict[str, str | None] = dict.fromkeys(ids)
    for oid, found in _addresses_of(cur, ids).items():
        # the address the Person's own key names, else never judged (never removed)
        found = pd.address(found)
        addresses[oid] = found if found and person_id(pd.address_key(found)) == oid else None
    unread = [oid for oid, found in addresses.items() if found is None]
    counts["refresh_unreadable"] += len(unread)   # no single address held: never judged
    readable = {oid: found for oid, found in addresses.items() if found is not None}
    records = {person_id(i["key"]): i for i in outside_people(
        capture, capture.census(sorted(set(readable.values()))), guard, domains)}
    records = {oid: item for oid, item in records.items() if oid in readable}
    gone = sorted(oid for oid in readable if oid not in records)
    changed: list[str] = []
    probe: Path | None = None
    if records:
        if not generation:
            raise Refused("no_generation")
        probe = folder / "refresh-probe.sqlite"
        build_layer(probe, state_dir, domains, guard, list(records.values()), generation)
        mine = _layer_rows(probe, sorted(records))
        theirs = _served_rows(cur, sorted(records))
        changed = sorted(oid for oid in records if mine[oid] != theirs[oid])
        served_keys = {oid: theirs[oid][0][4] if theirs[oid][0] else None for oid in changed}
    else:
        served_keys = {}
    held = _foreign(cur, probe, changed) | _foreign(cur, None, gone)
    counts["refresh_held_foreign"] += len(held)
    every = [b.mailbox_id for b in capture.boxes]
    for oid in changed:
        if oid in held:
            continue
        old = _mailboxes_of(served_keys.get(oid), every)
        new = frozenset(records[oid]["mailboxes"])
        counts["widened" if old is not None and new > old else
               "narrowed" if old is not None and new < old else
               "same_mailboxes" if old == new else "moved"] += 1
    counts["removed"] += len([g for g in gone if g not in held])
    counts["refresh_judged"] += len(readable)
    counts["refresh_unchanged"] += len(records) - len(changed)
    if probe is not None:
        probe.unlink(missing_ok=True)
    return ([records[oid] for oid in changed if oid not in held],
            [g for g in gone if g not in held])


# --------------------------------------------------------------------------- naming

def _fields(cur: Any, ctx: Mapping[str, Any]) -> list[int]:
    props = sorted({p for props in ctx["families"].values() for p in props})
    cur.execute("SELECT field FROM ontology.value_fields WHERE property = ANY(%s)", (props,))
    return sorted(r[0] for r in cur.fetchall())


def naming(cur: Any, ctx: Mapping[str, Any], addresses: Sequence[str], counts: Counter
           ) -> set[str]:
    """The current objects of the person step's source types a value of one of its
    properties holds every word of an address in (the word index; the judgement then
    decides), at most :data:`NAMING_MAX` an address: the source types filtered before the
    limit, so ``naming_capped`` counts every address past it."""
    fields = _fields(cur, ctx)
    sources = sorted(ctx["sources"])
    out: set[str] = set()
    for found in addresses:
        cur.execute("SELECT tsvector_to_array(ontology.sq_words(%s))", (found,))
        words = sorted(cur.fetchone()[0] or ())
        if not words:
            continue
        query = " & ".join("'" + w.replace("'", "''") + "'" for w in words)
        cur.execute("SELECT DISTINCT v.object_id FROM ontology.value_cells c "
                    "JOIN ontology.value_sources s ON s.src = c.src AND s.superseded_at IS NULL "
                    "JOIN ontology.object_versions v ON v.ver = s.ver AND "
                    "v.superseded_at IS NULL WHERE c.field = ANY(%s) AND c.value_num IS NULL "
                    "AND c.value IS NOT NULL AND ontology.sq_words(c.value) @@ %s::tsquery "
                    "AND split_part(v.object_id, ':', 1) = ANY(%s) "
                    f"LIMIT {NAMING_MAX + 1}", (fields, query, sources))
        got = sorted(r[0] for r in cur.fetchall())
        if len(got) > NAMING_MAX:
            counts["naming_capped"] += 1
            got = got[:NAMING_MAX]
        out.update(got)
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


def _set_state(dsn: str, value: Mapping[str, Any]) -> None:
    import psycopg
    held = {k: v for k, v in dict(value).items() if v or k == "through"}
    with psycopg.connect(dsn, connect_timeout=10) as pg:
        with pg.transaction():
            pg.execute("SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))")
            pg.execute("INSERT INTO ontology.store_meta (key, value) VALUES (%s, %s) ON "
                       "CONFLICT (key) DO UPDATE SET value = excluded.value",
                       (STATE, json.dumps(held, sort_keys=True)))


def _addresses_of(cur: Any, persons: Sequence[str]) -> dict[str, str]:
    """Served Person -> its one address (its ``addresses`` value; a Person holding none or
    several is left out)."""
    cur.execute("SELECT v.object_id, c.value FROM ontology.object_versions v JOIN "
                "ontology.value_sources s ON s.ver = v.ver AND s.superseded_at IS NULL JOIN "
                "ontology.value_cells c ON c.src = s.src JOIN ontology.value_fields f ON "
                "f.field = c.field AND f.property = 'addresses' WHERE v.object_id = ANY(%s) "
                "AND v.superseded_at IS NULL", (list(persons),))
    found: dict[str, set[str]] = {}
    for oid, value in cur.fetchall():
        try:
            held = json.loads(value)
        except (TypeError, ValueError):
            held = value
        for a in held if isinstance(held, list) else [held]:
            if isinstance(a, str):
                found.setdefault(oid, set()).add(a)
    return {oid: next(iter(a)) for oid, a in found.items() if len(a) == 1}


def _addresses(cur: Any, persons: Sequence[str]) -> list[str]:
    """The addresses the served Persons ``persons`` hold (their ``addresses`` value)."""
    return sorted(set(_addresses_of(cur, persons).values()))


def _rejudge_naming(dsn: str, ctx: Mapping[str, Any], persons: Sequence[str],
                    report: dict[str, Any], counts: Counter, *, work: Any, now: float,
                    environ: Mapping[str, str]) -> None:
    """The objects naming an address of ``persons`` judged again (``people_scope``)."""
    import psycopg
    with psycopg.connect(dsn, connect_timeout=10) as pg, pg.cursor() as cur:
        cur.execute(f"SET statement_timeout = {people_scope.statement_ms(environ)}")
        named = naming(cur, ctx, _addresses(cur, persons), counts)
    report["naming"] = report.get("naming", 0) + len(named)
    judged = people_scope.rejudge(dsn, sorted(named), work=work, now=now, environ=environ)
    ends = judged.pop("ends", [])
    report.setdefault("rejudged", []).append(judged)
    if ends:
        from operating.pg.ontology_delta import refresh_repeats
        report.setdefault("mention_repeats", []).append(refresh_repeats(dsn, ends))


def recompute(dsn: str, state_dir: str | Path, *, work: str | Path | None = None,
              now: float | None = None, environ: Mapping[str, str] | None = None,
              dry_run: bool = False) -> dict[str, Any]:
    """One run (module docstring): ``{"pending": False}`` when no mail was replaced since
    the mark and nothing is queued, else its report. Raises on failure: nothing is changed
    and the mark stays, or (failed after the state named the new contacts) the next run
    judges the objects naming them first."""
    import psycopg
    environ = os.environ if environ is None else environ
    now = time.time() if now is None else now
    started = time.monotonic()
    state_dir = Path(state_dir)
    with psycopg.connect(dsn, connect_timeout=10) as pg, pg.cursor() as cur:
        cur.execute("SELECT to_regclass('ontology.applied_owned') IS NOT NULL")
        if not cur.fetchone()[0]:
            return {"pending": False}
        state = _state(cur)
        through = int(state.get("through", 0))
        left = [p for p in state.get("naming") or () if isinstance(p, str)]
        queued = sorted({p for p in state.get("refresh") or () if is_outside(p)})
        if not left and not queued and not people_scope._touched(
                cur, through, [MAIL_TYPE], limit=1):
            return {"pending": False}
    guard = people_scope._guard(environ)
    if guard is None:
        raise Refused("exclusion_unavailable")
    domains = internal_domains(environ)
    ctx = people_scope.context(environ)
    if ctx is None:
        return {"pending": False, "declared": False}
    counts: Counter = Counter()
    report: dict[str, Any] = {"mode": "dry-run" if dry_run else "incremental"}
    if left and not dry_run:   # a run stopped after it named its new contacts
        _rejudge_naming(dsn, ctx, left, report, counts, work=work, now=now, environ=environ)
        _set_state(dsn, {"through": through, "refresh": queued})
        report["naming_left_over"] = len(left)
    folder = Path(tempfile.mkdtemp(prefix=".people-directory-", dir=work))
    try:
        capture = Capture(state_dir)
        try:
            if not capture.boxes:   # every audience would be judged empty
                raise Refused("mail_capture_no_mailboxes")
            with psycopg.connect(dsn, connect_timeout=10) as pg, pg.cursor() as cur:
                cur.execute(f"SET statement_timeout = {people_scope.statement_ms(environ)}")
                newest = int(people_scope._newest(cur) or 0)
                mark, mails = people_scope._due(cur, through, [MAIL_TYPE], _run_max(environ))
                items = new_people(capture, cur, mails, guard, domains, counts)
                cur.execute("SELECT value FROM ontology.load_state WHERE name = 'generation'")
                row = cur.fetchone()
                generation = (row[0] or {}).get("content_digest") if row else None
                made = {person_id(i["key"]) for i in items}
                waiting = sorted((set(queued) | refresh_candidates(capture, cur, mails))
                                 - made)
                most = _refresh_max(environ)
                take, rest = waiting[:most], waiting[most:]
                again, gone = refresh(capture, cur, take, guard, domains, state_dir,
                                      generation, folder, counts)
                if dry_run and items:
                    report["naming"] = len(naming(cur, ctx, sorted(
                        i["row"]["addresses"][0] for i in items), counts))
        finally:
            capture.close()
        report.update({"mails": len(mails), "remaining": mark < newest or bool(rest),
                       "persons": len(items),
                       "multi_mailbox": sum(len(i["mailboxes"]) > 1 for i in items),
                       "refreshed": len(again), "removed": len(gone),
                       "refresh_queued": len(rest)})
        if not dry_run and (items or again or gone):
            if not generation:
                raise Refused("no_generation")
            from operating.pg.ontology_delta import apply_layer
            path = folder / f"{LAYER_PREFIX}{mark}-{int(now * 1000)}.sqlite"
            build_layer(path, state_dir, domains, guard, [*items, *again], generation,
                        gone=gone)
            # named before it is applied: a run stopped from here on judges the objects
            # naming the new contacts next run, and keeps the queued refresh
            _set_state(dsn, {"through": through, "naming": sorted(made),
                             "refresh": sorted(set(queued) | set(rest))})
            applied = apply_layer(path, dsn)
            if not applied.get("applied"):
                raise Refused(f"directory_layer_not_applied:{applied.get('reason')}")
            report["layer"] = applied["layer"]
            if made:
                _rejudge_naming(dsn, ctx, sorted(made), report, counts, work=work, now=now,
                                environ=environ)
    finally:
        for f in folder.glob("*"):
            f.unlink(missing_ok=True)
        folder.rmdir()
    if not dry_run:
        _set_state(dsn, {"through": mark, "refresh": rest})
        report["through"] = mark
    report.update({"counts": dict(sorted(counts.items())),
                   "seconds": round(time.monotonic() - started, 1)})
    return report


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.people_directory_scope")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="one run now")
    run.add_argument("--dsn", default=None)
    run.add_argument("--state-dir", default="/data/plx-state")
    run.add_argument("--dry-run", action="store_true", help="judge and count, write nothing")
    args = parser.parse_args(argv)
    out = recompute(args.dsn or config.dsn(), args.state_dir, dry_run=args.dry_run)
    print(json.dumps(out, sort_keys=True, default=str), flush=True)
    return 0


__all__ = ["LAYER_PREFIX", "REFRESH_MAX", "Refused", "STATE", "build_layer", "is_outside",
           "message_of", "naming", "new_people", "person_id", "recompute", "refresh",
           "refresh_candidates"]


if __name__ == "__main__":
    sys.exit(main())
