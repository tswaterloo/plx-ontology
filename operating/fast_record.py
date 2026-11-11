"""A record's card as a fast answer (``record_status``: "status of OR1000154182", "details
of MC3025-GR"): the one record whose own identifier is the token the question names, of
any object type the reader's ``plx.find`` resolves it to exactly (``fast_answer.
by_identifier``; several records of that identifier are choices, a text lead is none), and
its card (``plx.object``) said as cited claims: its identifier, its name, its status words,
its dates and its amounts (money only with its own currency; a quantity never: its role
contract is the source's, not the card's).
A token held by no record as written is looked up in its other written forms (``_forms``:
"AO26278A" as "AO-26278A"; "PO8739" as the number of a record whose type the document word
names). One record kept in several systems is said once per system; of one system the
record whose own code is the token, never one that only refers to it. A record whose card
says nothing and that exactly one record carries (one verified link) is said through it.
Internal keys, system stamps, free text and links are never said. The same answer gate decides
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
from typing import Any, Mapping, Optional

from . import fast_answer as fa

MAX_STATED = 6          # card values one answer states
MAX_NAME = 60           # a name longer than this is free text, never said
# what a card says: status words (text), dates, amounts, quantities
_STATUS_PROP = re.compile(r"(?:^|_)(?:status|state|stage)$")
# references a message quotes back (the customer's PO, a tracking number)
_REFERENCE_PROP = re.compile(r"(?:^|_)(?:po|reference|tracking(?:_number)?|carrier)$")
_SAID_TYPES = ("date", "money", "enum")
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
    if kind == "text" and prop == "name":     # what the record is called (an item's name)
        text = str(raw).strip()               # as its cell holds it: the gate re-reads it
        return (text, {}) if text and len(text) <= MAX_NAME and "\n" not in text else None
    if kind not in _SAID_TYPES:
        return None
    if kind == "money":
        currency = value.get("currency")
        if not (isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency)):
            return None     # an amount is never said without its own currency
        return fa._amount(fa._decimal(raw)), {"currency": currency}
    if kind == "quantity":
        # a card's quantity is typed by its source's role contract, which the card does
        # not settle (a planned yield, ordered pieces): never said from a card alone
        return None
    text = str(raw).strip()
    return (text, {}) if text else None


MAX_COPIES = 4          # records of one identifier and type (one per system) said apart
# the kinds of match by which a record refers to another system's code (the Portal
# product holding the ERP item's code): a record whose own code is the token wins over it
_REFERENCE_KINDS = re.compile(r"^(?:erp|external|vendor|supplier|customer)_")
# a document word written before a number ("PO8739", "AO26278A", "CH#36261")
_PREFIXED = re.compile(r"^(?P<prefix>[A-Za-z]{2,4})[-#\s]?(?P<rest>\d[A-Za-z0-9]*)$")


def _system(object_id: str) -> str:
    binding = object_id.split(":", 2)[1] if object_id.count(":") >= 2 else ""
    return "Sage" if binding.startswith("portal.sage_") else fa._system_of(object_id)


def _initials(object_type: str) -> str:
    return "".join(c for c in object_type if c.isupper())


def _forms(token: str) -> list[tuple[str, Optional[str]]]:
    """Other ways the record the person wrote may be held, each with the type hint its
    document word gives (None: any type): the prefix with and without its hyphen
    ("AO26278A" <-> "AO-26278A"), then the number alone, kept only for a type whose
    initials end with the prefix ("PO8739" -> a customer's purchase order "8739", never
    the product coded 8739)."""
    m = _PREFIXED.match(token)
    if not m:
        return []
    prefix, rest = m.group("prefix"), m.group("rest")
    out = [(f"{prefix}-{rest}", None), (f"{prefix}{rest}", None)]
    if sum(c.isdigit() for c in rest) >= 3:
        out.append((rest, prefix.upper()))
    return [(f, hint) for f, hint in out if f != token]


def _find(reads: fa.Reads, token: str, as_of: Optional[str]) -> tuple[dict[str, Any], bool]:
    arguments: dict[str, Any] = {"query": token[:200], "limit": fa.FIND_LIMIT}
    found = reads.call("plx.find", dict(arguments, as_known_at=as_of) if as_of else arguments)
    then = bool(as_of) and bool(found.get("candidates"))
    if as_of and not then:
        found = reads.call("plx.find", arguments)    # not captured by then: read as now
    return found, then


def _own_first(found: Mapping[str, Any], ids: list[str]) -> list[str]:
    """Of each system, the records whose own identifier is the token when any is: a
    record that only refers to it (an ERP code it is mapped to) is another record."""
    kind = {}
    for c in found.get("candidates") or ():
        oid = (c.get("object") or {}).get("object_id")
        if oid in ids and oid not in kind:
            kind[oid] = str((c.get("matched") or {}).get("identifier_kind") or "")
    own = {_system(i) for i in ids if not _REFERENCE_KINDS.match(kind.get(i, ""))}
    return [i for i in ids if _system(i) not in own or not _REFERENCE_KINDS.match(kind.get(i, ""))]


def _resolve(reads: fa.Reads, token: str, as_of: Optional[str], hint: Optional[str] = None
             ) -> tuple[list[dict[str, Any]], bool]:
    """The records ``token`` names (one, or one record kept in several systems) and
    whether they were read as of the moment; several records are the reader's choice
    (``_Choice``), none ``Unanswerable``."""
    found, then = _find(reads, token, as_of)
    types = sorted({str((c.get("object") or {}).get("object_type"))
                    for c in found.get("candidates") or ()})
    if hint is not None:
        types = [t for t in types if _initials(t).endswith(hint) and len(_initials(t)) > 1]
    if not types:
        raise fa.Unanswerable("not_resolved")
    picked = fa.by_identifier(reads, token, types, found=found)
    if not isinstance(picked, fa.Choices):
        return [picked], then
    # the same identifier on one kind of record in a few systems (an order in Sage and
    # in the Portal): each said apart, named by its system; records of different
    # kinds, two of one system, or more of them, are the reader's choice, never picked
    ids = _own_first(found, list(dict.fromkeys(c["object_id"] for c in picked.candidates)))
    kinds = {c["object_type"] for c in picked.candidates if c["object_id"] in ids}
    if len(kinds) != 1 or len(ids) > MAX_COPIES or len({_system(i) for i in ids}) != len(ids):
        raise fa._Choice(fa.Choices(picked.mention, [c for c in picked.candidates
                                                     if c["object_id"] in ids]))
    held: dict[str, Mapping[str, Any]] = {}
    for c in found.get("candidates") or ():
        held.setdefault((c.get("object") or {}).get("object_id"), c)
    records = [dict(held[i]) for i in ids if i in held]
    if len(records) != len(ids):
        raise fa._Choice(picked)
    return records, then


def record_status(reads: fa.Reads, ask: fa.Ask, draft: fa.Draft) -> None:
    token = ask.mention
    as_of = getattr(reads, "as_of", None)
    try:
        records, then = _resolve(reads, token, as_of)
    except fa.Unanswerable:
        # not held as written: the other forms of it, the first that names a record
        for form, hint in _forms(token):
            try:
                records, then = _resolve(reads, form, as_of, hint)
                break
            except fa.Unanswerable:
                continue
        else:
            raise
    said_any = False
    for n, record in enumerate(records):
        said_any = _card(reads, draft, record, token, as_of, then,
                         f"_{n + 1}" if len(records) > 1 else "",
                         _system(record["object"]["object_id"]) if len(records) > 1 else ""
                         ) or said_any
    if not said_any:
        raise fa.Unanswerable("nothing_to_state")


def _card(reads: fa.Reads, draft: fa.Draft, record: Mapping[str, Any], token: str,
          as_of: Optional[str], then: bool, suffix: str, system: str,
          carrier: bool = True, about: Optional[tuple[str, str]] = None) -> bool:
    """One record's sentence; False when its card states nothing a message says. A record whose card
    states nothing and that one record carries (a customer's PO on the one sales order
    that references it, a verified link) is said through that record (``_carried``)."""
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
        got = _stated(value)
        if got is None:
            continue
        parts.append((prop, got[0], got[1], cite))
        done.add(prop)
    if as_of and not then and parts:
        changed, reached = _changed_since(reads, object_id, as_of, [p[0] for p in parts])
        # a status read first after the moment may have moved before it: never said
        parts = [p for p in parts if p[0] not in changed
                 and (reached or not _STATUS_PROP.search(p[0]))]
    parts = parts[:MAX_STATED]
    if not parts:
        return carrier and _carried(reads, draft, record, card, token, as_of, suffix, system)
    try:
        said = fa._said(draft, "record" + suffix, record, token or None)
    except fa.Unanswerable:
        if about:
            return False    # the carrying record's own number is not read: not said
        raise
    words = []
    for prop, text, extra, cite in parts:
        draft.value(prop + suffix, text, cite["l1_handle"], cite.get("field_path"), **extra)
        words.append(f"{_words(prop)} {text}" + (f" {extra['currency']}" if "currency" in extra
                                                 else ""))
    noun = _words(str(obj.get("object_type") or "record"))
    head = f"{noun} {said}" + (f" in {system}" if system else "")
    if about:       # (the link in words, the record asked about): which way it points
        link, asked = about
        head = f"{asked} {link} {head}" if not link.startswith("is ") else \
            f"{head} {link[3:]} {asked}"     # reverse: the carrying record speaks
    draft.sentences.append(head[:1].upper() + head[1:] + ": " + "; ".join(words) + ".")
    draft.roots.append({"object_id": object_id, "object_type": obj.get("object_type")})
    if as_of:   # how the moment was read: as known then, or now with no change since
        reads.as_of_reads.append("then" if then else "now_unchanged" if reached
                                 else "now_captured_after")
    return True


def _carried(reads: fa.Reads, draft: fa.Draft, record: Mapping[str, Any],
             card: Mapping[str, Any], token: str, as_of: Optional[str], suffix: str,
             system: str) -> bool:
    """The card of the one record that carries ``record`` (its only link, verified, to
    exactly one record), said as that record with ``record`` named beside it."""
    counts = [c for c in card.get("linked_counts") or () if isinstance(c, Mapping)]
    if len(counts) != 1 or counts[0].get("count") != 1 \
            or set((counts[0].get("tiers") or {})) != {"verified"} or fa._withheld(card):
        return False
    link = counts[0]
    around = reads.call("plx.around", {"from": record["object"]["object_id"], "limit": 2,
                                       "path": [{"link_type": link.get("link_type"),
                                                 "direction": link.get("direction")}]})
    objects = [o for o in around.get("objects") or ()
               if isinstance(o, Mapping) and o.get("object_id") != record["object"]["object_id"]]
    if len(objects) != 1 or fa._withheld(around) \
            or (around.get("completeness") or {}).get("truncated"):
        return False
    named = fa._said(draft, "record" + suffix, record, token)
    noun = _words(str(record["object"].get("object_type") or "record"))
    # the link's own words, the nouns of both records aside: "AO-1 for product X"
    # (forward, AssemblyOrder.for_product), "sales order X references customer purchase
    # order 8739" (reverse, SalesOrder.references_customer_po)
    nouns = set(noun.split()) | set(_words(str(objects[0].get("object_type") or "")).split())
    verb = " ".join(w for w in _words(str(link.get("link_type") or "").rsplit(".", 1)[-1])
                    .split() if w not in nouns and w not in ("po", "id")) or "linked to"
    link_words = verb if link.get("direction") != "reverse" else "is " + verb.split()[0]
    return _card(reads, draft, {"object": objects[0]}, "", as_of, False, suffix + "_on",
                 system, carrier=False, about=(link_words, f"{noun} {named}"))


RECIPES = {"record_status": record_status}
