"""Mail newer than the served projection, read from the mail capture at question time.

A projection generation pins the mail capture when its build starts and is served until
the next one publishes, so the mail it serves is always at least one build old
(option 1, owner-approved). A
``plx.documents`` mail search on a host that holds the mail capture
(``CaptureCaseHostTools``) therefore also reads the capture's messages the generation
does not hold: :func:`top_up` adds them to the first page of the search, each marked
``captured_after_refresh`` ("newer than the last full refresh"), cited by its
``graph-mail://`` handle exactly as a projected message is (``plx.read`` resolves it from
the same capture, and the answer gate re-reads it there).

What is read, and for whom (the same decisions as projected mail, never a second rule):

* only a mailbox whose ``mail:<mailbox>`` unit the generation holds and this call's read
  policy grants the reader (``ontology_tools.reader_grants``, the scope every projected
  search is made of; the case host grants a unit only for a mailbox the capture's
  audience check and the mail item gate admit: mailbox identities and owner ACL, the
  people exclusion ``PLX_EXCLUDED_PEOPLE_OIDS``, the case mail scope, the employee's
  item ACL). A mailbox captured since the generation was built holds no unit there and
  is never read. Rows of any other mailbox are never selected;
* every message then goes through the tools' per-item gate (``OntologyTools._item_allowed``,
  the host's mail item gate: the sender's exclusion, the reader's full-read grant, the
  per-message ACL), as each projected message does before it is listed;
* the mail type's markings (record and the properties read here) must all be held by the
  reader, else nothing is added (the projection would hide those values).

Matching: a participant search reads the addresses and domains the projected search
resolved for this reader (``searched.participants``: values the reader may see) and
matches the captured sender, To and CC exactly as the projection does (an address
whole, a domain or its subdomains); a query matches when every word of it is a word
(or a letter or digit run of one) of the message's subject or preview (narrower than the projection's identifier and body
search: it under-claims, never over-claims). Only a message none of whose versions the
generation pinned is added (``version_id`` above the generation's
``source_freshness.graph_mail.messages.last_capture_id``), current at the source (not
superseded, not removed), inside ``since``/``until``.

Bounds: read-only SQL on the kept capture reader, newest versions first in chunks of
``CHUNK`` ids until ``BUDGET_SECONDS`` or the pin is reached; at most ``MAX_ADDED`` (and
the call's ``limit``) messages are added, newest received first. A scan stopped by the
time bound or more matches than are added is a ``newer_mail_cut`` gap and the answer is
incomplete: newer mail is never silently left out. Projected documents are never
displaced (the page's cursor still continues the projected list).

Evidence label: isolated-fixture (``operating/tests/test_mail_topup.py``); the live path
is the host's kept read-only capture reader (live-unverified).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from operating import ontology_tools as ot

SOURCE = "graph_mail"
RELATION = "messages"
FLAG = "captured_after_refresh"
SCOPE = "capture:graph_mail"
CHUNK = 20_000            # capture version ids read per statement
# Scan time one search spends on the capture at most. A cut scan makes the answer
# incomplete (``newer_mail_cut``), so the bound waits for a whole scan of a large delta:
# the live capture held 803,000 versions past gen 7's pin (a history backfill); a whole
# scan took 2.7-4.7 s idle, so 0.5 s cut every mail search of the 2026-11-02 run.
BUDGET_SECONDS = 8.0
MAX_ADDED = 10            # newer messages one answer adds at most
MAX_TERMS = 8             # query words a top-up matches (more: not topped up)
SNIPPET_CHARS = 300
# The record fields read here and the mail properties they are projected to.
_PROPERTIES = ("subject", "sender", "to_addresses", "cc_addresses", "received_at",
               "body_preview")
_ADDRESS = re.compile(r"[a-z0-9._%+'-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)")
_WORD = re.compile(r"[a-z0-9]+")
_RUN = re.compile(r"[a-z]+|[0-9]+")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})$")


def pinned_capture_id(store_meta_value: Any) -> int | None:
    """The mail capture version id the served generation was built through
    (``source_freshness.graph_mail.messages.last_capture_id``); None when the generation
    names none (no top-up: what it holds cannot be told apart)."""
    try:
        facts = json.loads(store_meta_value) if isinstance(store_meta_value, str) else None
    except ValueError:
        return None
    entry = ((facts or {}).get(SOURCE) or {}).get(RELATION) if isinstance(facts, dict) \
        else None
    pin = entry.get("last_capture_id") if isinstance(entry, dict) else None
    return pin if type(pin) is int and pin >= 0 else None


def _parse(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not _TIME.match(value):
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _within(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


def _role(record: Mapping[str, Any], addresses: frozenset[str], domains: frozenset[str]
          ) -> tuple[str, str] | None:
    """(direction, matched_by) of a captured message for the participant needles, as the
    projected participant search decides it (``ontology_tools_evidence``): an address
    whole, else a domain or one of its subdomains; ``from_them`` when the sender matches."""
    out: tuple[str, str] | None = None
    for field, direction in (("from", "from_them"), ("to", "to_them"), ("cc", "to_them")):
        values = record.get(field)
        for value in (values if isinstance(values, list) else [values]):
            match = None
            for found in _ADDRESS.finditer(str(value or "").lower()):
                if found[0] in addresses:
                    match = "address"
                    break
                if any(_within(found[1], d) for d in domains):
                    match = match or "domain"
            if match is None:
                continue
            if out is None:
                out = (direction, match)
            else:
                out = ("from_them" if "from_them" in (out[0], direction) else out[0],
                       "address" if "address" in (out[1], match) else out[1])
    return out


def _words(text: Any) -> set[str]:
    """The words of ``text``, lower-case, and their letter and digit runs (``PO100234``
    is the words ``po100234``, ``po`` and ``100234``: an order number written against its
    prefix still matches the number)."""
    if not isinstance(text, str):
        return set()
    words = set(_WORD.findall(text.lower()))
    return words | {run for word in words for run in _RUN.findall(word)}


def query_terms(query: Any) -> list[str] | None:
    """The words a query top-up requires (each a word of the subject or preview); None
    when the query is none, holds no word or more than ``MAX_TERMS``."""
    if not isinstance(query, str):
        return None
    terms = sorted(set(_WORD.findall(query.lower())))
    return terms if terms and len(terms) <= MAX_TERMS else None


def scan(db: sqlite3.Connection, *, pin: int, mailboxes: Sequence[str],
         addresses: frozenset[str], domains: frozenset[str], terms: Sequence[str] | None,
         since: dt.datetime | None, until: dt.datetime | None,
         clock: Callable[[], float] = time.monotonic, budget: float = BUDGET_SECONDS,
         chunk: int = CHUNK) -> tuple[list[dict[str, Any]], bool]:
    """The current captured messages of ``mailboxes`` past ``pin`` that match, newest
    received first, as ({handle, mailbox, record, date, direction, matched_by}, cut):
    ``cut`` when the time bound stopped the scan before the pin."""
    if not mailboxes or not (addresses or domains or terms):
        return [], False
    top = db.execute("SELECT MAX(version_id) FROM messages").fetchone()[0]
    if top is None or top <= pin:
        return [], False
    needles = sorted(addresses | domains)
    marks = ",".join("?" * len(mailboxes))
    # ``+mailbox_id``: never the mailbox index, which would read every version of the
    # granted mailboxes for each chunk; each statement reads its version-id range alone.
    where = [f"+mailbox_id IN ({marks})", "superseded_at IS NULL", "removed_reason IS NULL"]
    params: list[Any] = list(mailboxes)
    if needles:   # addresses are stored lower-case (graph_read._addresses)
        where.append("(" + " OR ".join("instr(record, ?) > 0" for _ in needles) + ")")
        params += needles
    for term in terms or ():
        where.append("instr(lower(record), ?) > 0")
        params.append(term)
    # A message any of whose versions the generation pinned is the projection's.
    where.append("NOT EXISTS (SELECT 1 FROM messages p WHERE p.mailbox_id = m.mailbox_id "
                 "AND p.message_id = m.message_id AND p.version_id <= ?)")
    params.append(pin)
    sql = (f"SELECT version_id, l1_handle, mailbox_id, record FROM messages m "
           f"WHERE version_id > ? AND version_id <= ? AND {' AND '.join(where)}")
    started = clock()
    found: list[dict[str, Any]] = []
    high, cut = top, False
    while high > pin:
        if clock() - started > budget:
            cut = True
            break
        low = max(pin, high - chunk)
        for version_id, handle, mailbox, raw in db.execute(sql, (low, high, *params)):
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            moment = _parse(record.get("received_at"))
            if moment is None or (since and moment < since) or (until and moment > until):
                continue
            role = None
            if needles:
                role = _role(record, addresses, domains)
                if role is None:
                    continue
            if terms and not set(terms) <= (_words(record.get("subject"))
                                             | _words(record.get("body_preview"))):
                continue
            found.append({"handle": handle, "mailbox": mailbox, "record": record,
                          "moment": moment, "version_id": version_id, "role": role})
        high = low
    found.sort(key=lambda f: (-f["moment"].timestamp(), -f["version_id"]))
    return found, cut


def _document(found: Mapping[str, Any]) -> dict[str, Any]:
    record, handle = found["record"], found["handle"]
    preview = record.get("body_preview")
    doc: dict[str, Any] = {
        "citation": handle, "kind": "email",
        "title": ot._envelope(record.get("subject"), handle, "row.subject", 20_000),
        "date": record["received_at"],
        "snippet": ot._envelope(preview[:SNIPPET_CHARS] if isinstance(preview, str)
                                else None, handle, "row.body_preview", SNIPPET_CHARS),
        "mentions": [], FLAG: True}
    if found["role"] is not None:
        doc["direction"], doc["matched_by"] = found["role"]
    return doc


def held_mail_markings(tools: Any, held: Iterable[str]) -> bool:
    """Whether the reader holds every marking the mail record type and the properties
    read here carry (unknown schema: no)."""
    schema = getattr(tools, "schema", None)
    types = getattr(schema, "object_types", None)
    if not isinstance(types, Mapping):
        return False
    bound = [spec for spec in types.values()
             if any(b.get("source") == SOURCE and b.get("relation") == RELATION
                    for b in spec.get("bindings") or ())]
    if not bound:
        return False
    needed = {m for spec in bound for m in spec.get("markings") or ()}
    needed |= {m for spec in bound for p in spec.get("properties") or ()
               if p.get("name") in _PROPERTIES for m in p.get("markings") or ()}
    return needed <= set(held)


def top_up(tools: Any, arguments: Mapping[str, Any], actor: Any, result: dict[str, Any],
           opened: Callable[[], Any], *, clock: Callable[[], float] = time.monotonic,
           budget: float = BUDGET_SECONDS, chunk: int = CHUNK) -> dict[str, Any]:
    """``result`` (a ``plx.documents`` answer for ``actor`` under ``tools.policy``) with
    the capture's newer matching messages this reader may see added (module docstring).
    ``opened()`` is a context of the mail capture store (read-only). Any input this does
    not top up, or any failure, returns ``result`` unchanged."""
    if arguments.get("cursor") or arguments.get("object_id") is not None \
            or arguments.get("as_known_at") or "error" in result \
            or SOURCE not in (arguments.get("sources") or (SOURCE,)):
        return result
    participants = None
    if arguments.get("participant") is not None:
        participants = (result.get("searched") or {}).get("participants") or []
        if not participants:
            return result   # nothing the reader may see to match on
    terms = query_terms(arguments.get("query"))
    if participants is None and terms is None:
        return result
    if arguments.get("query") is not None and terms is None:
        return result
    since = ot._parse_time(arguments["since"]) if arguments.get("since") else None
    until = (ot._parse_time(arguments["until"], end_of_day=True)
             if arguments.get("until") else None)
    try:
        with tools.store.lock:
            row = tools.store.conn.execute(
                "SELECT value FROM store_meta WHERE key = 'source_freshness'").fetchone()
        pin = pinned_capture_id(row[0] if row else None)
        keys, held, _sets, _sources = ot.reader_grants(tools, tools.policy, actor,
                                                       tools.store.audience_keys())
    except (sqlite3.Error, AttributeError, TypeError, ValueError):
        return result
    mailboxes = sorted(k.partition(":")[2] for k in keys if k.startswith("mail:"))
    if pin is None or not mailboxes or not held_mail_markings(tools, held):
        return result
    addresses = frozenset(p for p in participants or () if "@" in p)
    domains = frozenset(p for p in participants or () if "@" not in p)
    try:
        with opened() as store:
            found, cut = scan(store.db, pin=pin, mailboxes=mailboxes, addresses=addresses,
                              domains=domains, terms=terms, since=since, until=until,
                              clock=clock, budget=budget, chunk=chunk)
    except (sqlite3.Error, OSError, ValueError):
        return result
    listed = {doc.get("citation") for doc in result.get("documents") or ()}
    room = min(MAX_ADDED, int(arguments.get("limit") or MAX_ADDED),
               50 - len(result.get("documents") or ()))
    added: list[dict[str, Any]] = []
    more = False
    for item in found:
        if item["handle"] in listed:
            continue
        # The per-item gate every projected message passes (outside the store context:
        # the gate opens the capture reader itself).
        if not tools._item_allowed(actor, item["handle"]):
            continue
        if len(added) >= room:
            more = True
            break
        added.append(_document(item))
        listed.add(item["handle"])
    if not added and not cut:
        return result
    documents = list(result.get("documents") or ())
    if participants is not None:
        # "The last email with them": the date alone orders, newest first.
        documents = sorted(documents + added, key=lambda d: (
            -(_parse(d.get("date")) or dt.datetime.min.replace(
                tzinfo=dt.timezone.utc)).timestamp(), d["citation"]))
    else:
        documents = added + documents
    gaps = [g for g in result.get("gaps") or () if not (added and g.get("reason") == "no_match")]
    if cut or more:
        gaps.insert(0, ot._gap(
            "unknown", "newer_mail_cut", SCOPE,
            "mail captured since the last full refresh was not all searched or listed in "
            "this answer (the capture's newer messages exceed what one answer reads); "
            "narrow the question by dates or ask again"))
    searched = dict(result.get("searched") or {})
    searched["scopes"] = [*searched.get("scopes", []), SCOPE][:16]
    completeness = dict(result.get("completeness") or {})
    completeness["returned"] = len(documents)
    if completeness.get("total") is not None:
        completeness["total"] += len(added)
    if (cut or more) and completeness.get("complete") is not False:
        completeness.update(complete=False, reason="partial_scope")
    return dict(result, documents=documents, gaps=gaps[:20], searched=searched,
                completeness=completeness)


__all__ = ["BUDGET_SECONDS", "FLAG", "MAX_ADDED", "pinned_capture_id", "query_terms", "scan",
           "top_up"]
