"""A record's card as a fast answer (``record_status``: "status of OR1000154182", "details
of MC3025-GR"): the one record whose own identifier is the token the question names, of
any object type the reader's ``plx.find`` resolves it to exactly (``fast_answer.
by_identifier``; several records of that identifier are choices, a text lead is none), and
its card (``plx.object``) said as cited claims: its identifier, its status words, its dates
and its amounts (money only with its own currency; a quantity only with its unit). Internal
keys, system stamps, free text and links are never said. The same answer gate decides
every claim as for any fast answer; a refused one sends the question to the agent.

As of a moment (``Reads.as_of``, an ISO instant: what the person knew when they wrote):
the find and the card are read ``as_known_at`` it. A record the store had not captured by
then is read as it is now: a property its history shows changed after that moment is left
out (its value then is not the one said now), and so is a status when the history starts
after the moment (first captured later: what it was then is unknown); a history read in
part says nothing.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Mapping, Optional

from . import fast_answer as fa

MAX_STATED = 6          # card values one answer states
# what a card says: status words (text), dates, amounts, quantities
_STATUS_PROP = re.compile(r"(?:^|_)(?:status|state|stage)$")
# references a message quotes back (the customer's PO, a tracking number)
_REFERENCE_PROP = re.compile(r"(?:^|_)(?:po|reference|tracking(?:_number)?|carrier)$")
_SAID_TYPES = ("date", "money", "quantity", "enum")
# keys and stamps a person never writes in a message
_INTERNAL = re.compile(r"(?:^|_)(?:id|uniq|uuid|key|hash|digest|type|code|version|seq)$|"
                       r"_at$|(?:^|_)(?:created|modified|updated)(?:_|$)")


def _words(name: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ").strip().lower()


def _instant(text: Any) -> str:
    return str(text or "")[:19]


HISTORY_PAGES = 6       # history pages read back to the moment, at most


def _changed_since(reads: fa.Reads, object_id: str, as_of: str,
                   properties: list[str]) -> tuple[set[str], bool]:
    """(The properties whose values changed after ``as_of``, whether the history reaches
    back to ``as_of``) (the record's history, newest
    first, paged back to ``as_of``, of ``properties`` only: no link is read); a history not read back that far, or withheld in
    part, is unread (``Unanswerable``): nothing is said as of then."""
    out: set[str] = set()
    arguments: dict[str, Any] = {"object_id": object_id, "history": True,
                                 "properties": list(properties)}
    for _page in range(HISTORY_PAGES):
        hist = reads.call("plx.object", arguments)
        if fa._withheld(hist):
            raise fa.Unanswerable("history_withheld")
        for entry in hist.get("history") or ():
            if _instant(entry.get("at")) <= _instant(as_of):
                return out, True
            if entry.get("kind") in ("removed_from_source", "restored"):
                raise fa.Unanswerable("record_removed_since")
            if entry.get("kind") == "changed":
                out.update(str(c.get("property")) for c in entry.get("changes") or ())
        cursor = (hist.get("completeness") or {}).get("next_cursor")
        if not isinstance(cursor, str) or not cursor:
            return out, False   # first captured after the moment: read to its start
        arguments = dict(arguments, cursor=cursor)
    raise fa.Unanswerable("history_in_part")


def _stated(value: Mapping[str, Any]) -> Optional[tuple[str, dict[str, Any]]]:
    """(what the value says, the claim's extras) or None: not a value a message states."""
    prop, kind = str(value.get("property") or ""), value.get("value_type")
    raw = fa.plain(value.get("value"))
    if raw is None or isinstance(raw, bool) or _INTERNAL.search(prop):
        return None
    if kind == "text" and (_STATUS_PROP.search(prop) or _REFERENCE_PROP.search(prop)):
        text = str(raw).strip()
        return (text, {}) if text and len(text) <= 40 else None
    if kind not in _SAID_TYPES:
        return None
    if kind == "money":
        currency = value.get("currency")
        if not (isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency)):
            return None     # an amount is never said without its own currency
        return fa._amount(fa._decimal(raw)), {"currency": currency}
    if kind == "quantity":
        unit = value.get("unit")
        if not isinstance(unit, str) or not unit:
            return None
        return str(Decimal(str(raw)).normalize()), {"unit": unit}
    text = str(raw).strip()
    return (text, {}) if text else None


MAX_COPIES = 3          # records of one identifier and type (one per system) said apart


def _system(object_id: str) -> str:
    binding = object_id.split(":", 2)[1] if object_id.count(":") >= 2 else ""
    return "Sage" if binding.startswith("portal.sage_") else fa._system_of(object_id)


def record_status(reads: fa.Reads, ask: fa.Ask, draft: fa.Draft) -> None:
    token = ask.mention
    as_of = getattr(reads, "as_of", None)
    arguments: dict[str, Any] = {"query": token[:200], "limit": fa.FIND_LIMIT}
    found = reads.call("plx.find", dict(arguments, as_known_at=as_of) if as_of else arguments)
    then = bool(as_of) and bool(found.get("candidates"))
    if as_of and not then:
        found = reads.call("plx.find", arguments)    # not captured by then: read as now
    types = sorted({str((c.get("object") or {}).get("object_type"))
                    for c in found.get("candidates") or ()})
    if not types:
        raise fa.Unanswerable("not_resolved")
    picked = fa.by_identifier(reads, token, types, found=found)
    if isinstance(picked, fa.Choices):
        # the same identifier on one kind of record in a few systems (an order in Sage and
        # in the Portal): each said apart, named by its system; records of different
        # kinds, or more of them, are the reader's choice, never picked
        ids = [c["object_id"] for c in picked.candidates]
        kinds = {c["object_type"] for c in picked.candidates}
        if len(kinds) != 1 or len(ids) > MAX_COPIES or len({_system(i) for i in ids}) != len(ids):
            raise fa._Choice(picked)
        held: dict[str, Mapping[str, Any]] = {}
        for c in found.get("candidates") or ():
            held.setdefault((c.get("object") or {}).get("object_id"), c)
        records = [held[i] for i in ids if i in held]
        if len(records) != len(ids):
            raise fa._Choice(picked)
    else:
        records = [picked]
    said_any = False
    for n, record in enumerate(records):
        said_any = _card(reads, draft, record, token, as_of, then,
                         f"_{n + 1}" if len(records) > 1 else "",
                         _system(record["object"]["object_id"]) if len(records) > 1 else ""
                         ) or said_any
    if not said_any:
        raise fa.Unanswerable("nothing_to_state")


def _card(reads: fa.Reads, draft: fa.Draft, record: Mapping[str, Any], token: str,
          as_of: Optional[str], then: bool, suffix: str, system: str) -> bool:
    """One record's sentence; False when its card states nothing a message says."""
    obj = record["object"]
    object_id = obj["object_id"]
    card = reads.call("plx.object", dict({"object_id": object_id},
                                         **({"as_known_at": as_of} if then else {})))
    parts: list[tuple[str, str, dict[str, Any], Mapping[str, Any]]] = []
    reached = True
    done: set[str] = set()
    values = sorted((v for v in card.get("values") or () if isinstance(v, Mapping)),
                    key=lambda v: (v.get("authority_rank") or 0))
    for value in values:
        prop = str(value.get("property") or "")
        if prop in done:
            continue
        cite = value.get("citation") or {}
        if not isinstance(cite.get("l1_handle"), str):
            continue
        stated = _stated(value)
        if stated is None:
            continue
        parts.append((prop, stated[0], stated[1], cite))
        done.add(prop)
    if as_of and not then and parts:
        changed, reached = _changed_since(reads, object_id, as_of, [p[0] for p in parts])
        # a status read first after the moment may have moved before it: never said
        parts = [p for p in parts if p[0] not in changed
                 and (reached or not _STATUS_PROP.search(p[0]))]
    parts = parts[:MAX_STATED]
    if not parts:
        return False
    said = fa._said(draft, "record" + suffix, record, token)
    words = []
    for prop, text, extra, cite in parts:
        draft.value(prop + suffix, text, cite["l1_handle"], cite.get("field_path"), **extra)
        words.append(f"{_words(prop)} {text}" + (f" {extra['currency']}" if "currency" in extra
                                                 else f" {extra['unit']}" if "unit" in extra
                                                 else ""))
    noun = _words(str(obj.get("object_type") or "record"))
    draft.sentences.append(f"{noun[:1].upper() + noun[1:]} {said}"
                           + (f" in {system}" if system else "") + ": "
                           + "; ".join(words) + ".")
    draft.roots.append({"object_id": object_id, "object_type": obj.get("object_type")})
    if as_of:   # how the moment was read: as known then, or now with no change since
        reads.as_of_reads.append("then" if then else "now_unchanged" if reached
                                 else "now_captured_after")
    return True


RECIPES = {"record_status": record_status}
