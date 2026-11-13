"""The people the mail capture knows: our staff (one per captured mailbox) and the outside
contacts who wrote to them, as the ``Person`` records of ``MailCaptures`` relation
``people`` (``graph_mail.people``).

* **Staff**: one per row of the capture's ``mailboxes`` table (its Entra object id, UPN
  and SMTP identities).
* **Outside contacts**: one per sender address of the current captured messages that is
  no automated sender (``capture_mail.automated_sender``) and is not at or under one of
  our own domains (the directory's domains and the configured internal ones,
  ``mentions.own_mail_domains``). It carries no message count (a count over all mailboxes would show a
  reader how much mail went to mailboxes they cannot read; counts come from queries
  over the links the reader can see).

**Who sees a person**: a staff person, a reader of any captured mailbox it corresponds in
(a current message there from, to or cc one of its addresses; its own mailbox always);
an outside contact, a reader of any captured mailbox it wrote to (a current message
there from it): what makes an address an outside contact is mail it sent, so a reader
who only wrote to it is told nothing of mail it sent elsewhere. Never a reader of any
other mailbox. One mailbox: audience ``mail:<object id>``; more: the
composite ``mail-any:<bitmap>.<check>`` (``audience_key``), a bitmap over the capture's
sorted mailbox ids and the first 16 hex of the sha256 of the ids it names, so the key
stays short (a Postgres index entry) and a reader decodes it against the same capture
(``decode_audience``): a key that does not decode to exactly the ids its check names
(the mailbox list changed) is no grant to anyone. Each mailbox of it is granted as its
own ``mail:`` unit is, so the excluded people's mailboxes never open one.

**Excluded people fail closed** (``integrations.people_exclusion``). With the exclusion
list unavailable no record is made at all (counted ``exclusion_unavailable``). A
mailbox whose object id or any of whose addresses is an excluded person's is no record,
and its mail adds to no outside contact. An address at our own domains that no
captured mailbox holds (or two hold) cannot be decided: it is withheld, never an
outside contact (``withheld_unmapped``). Counts only ever name how many, never who.

A staff person is cited ``graph-mail://<its object id>/person/<its object id>`` (the id
its row shows anyway); an outside contact ``graph-mail://people/person/<key>``, the key
the sha256 of its lower-case address (as the capture's ``priority_senders`` census keys
a sender), so its citation names neither an address nor any mailbox (a mailbox id is an
employee's Entra id). The earlier ``graph-mail://<mailbox>/person/<key>`` form of an
outside contact is still read, its mailbox ignored: a person is resolved only against
the reader's own mailboxes (``capture_mail.MailCaptureStore``). Stdlib only."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence

HANDLE_PART = "/person/"
PEOPLE_HOST = "people"      # the host of an outside contact's citation: no mailbox
_ADDRESS_KEY = re.compile(r"[0-9a-f]{64}")
_ADDRESS = re.compile(r"[^@\s<>\"]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+")
_OID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Readers of the sender census side by side (one connection each, a range of mailboxes).
SCAN_READERS = 8


def address(value: Any) -> str | None:
    """A whole mail address, lower case and trimmed, else None."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    return lowered if _ADDRESS.fullmatch(lowered) else None


def object_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    return lowered if _OID.fullmatch(lowered) else None


def address_key(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def person_handle(mailbox: str, key: str) -> str:
    """The citation of a person: a staff person's (``key`` its object id) names its own
    mailbox; an outside contact's (``key`` an address key) names none, whatever
    ``mailbox`` is."""
    if _ADDRESS_KEY.fullmatch(key):
        return f"graph-mail://{PEOPLE_HOST}{HANDLE_PART}{key}"
    return f"graph-mail://{mailbox}{HANDLE_PART}{key}"


def parse_person_handle(handle: Any) -> tuple[str | None, str] | None:
    """(mailbox, key) of a person citation, else None: mailbox None for an outside
    contact's ``graph-mail://people/person/<key>``; the earlier form naming a mailbox is
    read too (a reader must never decide on that mailbox: it is the citer's)."""
    if not isinstance(handle, str) or not handle.startswith("graph-mail://"):
        return None
    mailbox, sep, key = handle[len("graph-mail://"):].partition(HANDLE_PART)
    if not sep or not mailbox or "/" in mailbox:
        return None
    if mailbox == PEOPLE_HOST:
        return (None, key) if _ADDRESS_KEY.fullmatch(key) else None
    if not (_ADDRESS_KEY.fullmatch(key) or _OID.fullmatch(key)):
        return None
    return mailbox, key


@dataclass(frozen=True)
class Mailbox:
    mailbox_id: str
    upn: str
    identities: tuple[str, ...]
    recorded_at: str
    last_seen_at: str


def mailboxes(conn: sqlite3.Connection) -> list[Mailbox]:
    """The capture's mailboxes (``[]`` for a capture without the table)."""
    try:
        rows = conn.execute("SELECT mailbox_id, upn, identities, first_captured_at, "
                            "identity_checked_at FROM mailboxes ORDER BY mailbox_id").fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in rows:
        try:
            identities = json.loads(row[2]) or []
        except (TypeError, ValueError):
            identities = []
        out.append(Mailbox(row[0], row[1], tuple(str(i) for i in identities
                                                 if isinstance(i, str)), row[3], row[4]))
    return out


# Every address a mailbox's current messages are from, to or cc, with how many it sent
# (0: only ever a recipient there).
# A message naming an address the capture could not decide is no reader's ({free}:
# ``capture_mail.undecided_free_sql`` of ``m``).
_CENSUS = ("SELECT a, sum(s) FROM ("
           "SELECT lower(trim(json_extract(m.record, '$.from'))) AS a, 1 AS s FROM messages m "
           "WHERE m.mailbox_id = :m AND m.superseded_at IS NULL AND {free} UNION ALL "
           "SELECT lower(trim(j.value)), 0 FROM messages m, json_each(m.record, '$.to') j "
           "WHERE m.mailbox_id = :m AND m.superseded_at IS NULL AND {free} UNION ALL "
           "SELECT lower(trim(j.value)), 0 FROM messages m, json_each(m.record, '$.cc') j "
           "WHERE m.mailbox_id = :m AND m.superseded_at IS NULL AND {free}) GROUP BY a")


def _scan_range(path: str, ids: Sequence[str]) -> Counter:
    counts: Counter = Counter()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    try:
        from integrations.capture_mail import undecided_free_sql
        census = _CENSUS.format(free=undecided_free_sql(conn, "m"))
        for mailbox in ids:
            for found, n in conn.execute(census, {"m": mailbox}):
                if isinstance(found, str) and found:
                    counts[(found, mailbox)] += int(n or 0)
    finally:
        conn.close()
    return counts


def sender_counts(path: Any, mailbox_ids: Sequence[str],
                  readers: int = SCAN_READERS) -> Counter:
    """(lower-case address, mailbox) -> current messages it sent there, for every address
    a current message of the mailbox is from, to or cc (0: only a recipient there), read
    on ``readers`` read-only connections side by side (each over its own mailboxes, on
    the mailbox index)."""
    from concurrent.futures import ThreadPoolExecutor
    ids = sorted(set(mailbox_ids))
    if not ids:
        return Counter()
    parts = [ids[i::max(1, readers)] for i in range(max(1, readers))]
    total: Counter = Counter()
    with ThreadPoolExecutor(max_workers=len(parts)) as pool:
        for got in pool.map(lambda part: _scan_range(str(path), part), [p for p in parts if p]):
            total.update(got)
    return total


def _canonical_digest(row: Mapping[str, Any]) -> str:
    return sha256(json.dumps(row, sort_keys=True, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()


def people(boxes: Sequence[Mailbox], counts: Mapping[tuple[str, str], int], exclusion: Any,
           internal_domains: Iterable[str] = ()) -> tuple[list[dict[str, Any]], Counter]:
    """(person records, counts) from the mailboxes and the sender census (module
    docstring; ``counts``: ``sender_counts``, a 0 an address only received there). Each
    record: ``key``, ``audience`` (the mailbox holding most of its mail, ties the smaller
    id: its capture times), ``mailboxes`` (every kept mailbox a reader of which sees it,
    sorted: a staff person's own and those it corresponds in; an outside contact's those
    it wrote to), ``audience_key`` (``audience_key`` of them),
    ``row``, ``recorded_at``, ``last_seen_at``."""
    from integrations.capture_mail import automated_sender
    from integrations.people_exclusion import ExclusionUnavailable
    from ontology import mentions

    stats: Counter = Counter()
    if exclusion is None or exclusion.unavailable:
        stats["exclusion_unavailable"] = 1
        return [], stats
    exclusion.attach_directory((b.mailbox_id, b.upn, b.identities) for b in boxes)
    own = mentions.own_mail_domains({*exclusion.known_domains(),
                                     *(str(d).lower() for d in internal_domains)})

    def withheld(value: str) -> bool:
        try:
            return bool(exclusion.is_excluded(value))
        except ExclusionUnavailable:
            return True

    out: list[dict[str, Any]] = []
    staff_addresses: set[str] = set()
    kept: dict[str, Mailbox] = {}
    for box in boxes:
        oid = object_id(box.mailbox_id)
        found = sorted({a for a in (address(box.upn), *map(address, box.identities)) if a})
        if oid is None or withheld(oid) or any(withheld(a) for a in found):
            stats["withheld_staff"] += 1
            continue
        kept[box.mailbox_id] = box
        staff_addresses.update(found)
        upn = address(box.upn)
        row = {"person_key": oid, "kind": "staff", "object_id": oid, "upn": upn,
               "addresses": found, "domain": mentions.mail_domain(upn) if upn else None}
        out.append({"key": oid, "audience": box.mailbox_id, "row": row,
                    "mailboxes": {box.mailbox_id}, "found": found,
                    "recorded_at": box.recorded_at, "last_seen_at": box.last_seen_at})
        stats["staff"] += 1
    senders: dict[str, dict[str, int]] = defaultdict(dict)
    seen: dict[str, set[str]] = defaultdict(set)
    for (sender, mailbox), n in counts.items():
        if mailbox in kept and n >= 0:
            seen[sender].add(mailbox)
            if n > 0:
                senders[sender][mailbox] = senders[sender].get(mailbox, 0) + n
    for item in out:
        for found in item.pop("found"):
            item["mailboxes"] |= seen.get(found, set())
    for sender in sorted(senders):
        found = address(sender)
        if found is None or found in staff_addresses:
            continue
        domain = mentions.mail_domain(found)
        if domain is None:
            continue
        if mentions.sent_from_own_domain(domain, own):
            stats["withheld_unmapped"] += 1
            continue
        if automated_sender(found):
            stats["automated"] += 1
            continue
        held = senders[sender]
        audience = min(held, key=lambda m: (-held[m], m))
        key = address_key(found)
        row = {"person_key": key, "kind": "outside", "object_id": None, "upn": None,
               "addresses": [found], "domain": domain}
        box = kept[audience]
        # only the mailboxes it wrote to: being a contact is a fact of mail it sent
        out.append({"key": key, "audience": audience, "row": row,
                    "mailboxes": set(held),
                    "recorded_at": box.recorded_at, "last_seen_at": box.last_seen_at})
        stats["outside"] += 1
    every = [b.mailbox_id for b in boxes]
    for item in out:
        item["mailboxes"] = sorted(item["mailboxes"])
        item["audience_key"] = audience_key(item["mailboxes"], every)
        stats["multi_mailbox"] += len(item["mailboxes"]) > 1
        item["digest"] = _canonical_digest(item["row"])
    return out, stats


COMPOSITE = "mail-any:"
_COMPOSITE = re.compile(r"mail-any:([0-9a-f]{1,1024})\.([0-9a-f]{16})")


def _check(ids: Iterable[str]) -> str:
    return sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()[:16]


def audience_key(mailbox_ids: Sequence[str], every: Sequence[str]) -> str:
    """The audience key of a record seen by readers of any of ``mailbox_ids``: one
    mailbox ``mail:<id>``; more, ``mail-any:<bitmap>.<check>``, bit ``i`` (of a hex
    number) set for the ``i``-th of ``every`` (the capture's mailbox ids) sorted.
    ValueError for none, or a mailbox ``every`` does not hold."""
    chosen = sorted(set(mailbox_ids))
    if not chosen:
        raise ValueError("audience_mailboxes_empty")
    if len(chosen) == 1:
        return f"mail:{chosen[0]}"
    order = {m: i for i, m in enumerate(sorted(set(every)))}
    if any(m not in order for m in chosen):
        raise ValueError("audience_mailbox_unknown")
    bits = 0
    for m in chosen:
        bits |= 1 << order[m]
    return f"{COMPOSITE}{bits:x}.{_check(chosen)}"


def is_composite(key: Any) -> bool:
    """Whether ``key`` is in the grammar of a ``mail-any:`` key (decoded or not)."""
    return isinstance(key, str) and _COMPOSITE.fullmatch(key) is not None


def decode_audience(key: Any, every: Sequence[str]) -> tuple[str, ...] | None:
    """The mailbox ids a ``mail-any:`` key names, decoded against ``every`` (the
    capture's mailbox ids now); None (no grant) for any other key, a bit past the list,
    fewer than two ids, or ids whose check is not the key's (the list changed)."""
    if not isinstance(key, str):
        return None
    found = _COMPOSITE.fullmatch(key)
    if found is None:
        return None
    ordered = sorted(set(m for m in every if isinstance(m, str)))
    bits = int(found.group(1), 16)
    if bits >> len(ordered):
        return None
    chosen = tuple(m for i, m in enumerate(ordered) if bits >> i & 1)
    if len(chosen) < 2 or _check(chosen) != found.group(2):
        return None
    return chosen


__all__ = ["COMPOSITE", "HANDLE_PART", "PEOPLE_HOST", "Mailbox", "address", "address_key", "audience_key",
           "decode_audience", "is_composite", "mailboxes", "object_id", "parse_person_handle", "people",
           "person_handle", "sender_counts"]
