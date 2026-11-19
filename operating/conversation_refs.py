"""A follow-up's reference to the previous answer's records: "open the second one", "who
sent that email?", "are those late?".

An ordinal ("the second one", "the 3rd order", "the last one") or a deictic ("that
customer", "this one", "those", "them") points at records the previous answer in the
same conversation showed its reader; it is never a name to look up ("one" is not "One
Wellness"). What it points at is read from that answer's own records (its subjects, in
the order the answer gave them), never from a template:

* a noun after it ("the second email", "that customer") keeps the records whose type's
  own name ends in that word (``Email``, ``Customer``; "order" holds ``SalesOrder`` and
  ``PurchaseOrder``; a party word as ``fast_listing`` reads it);
* "one" or no noun keeps the type the answer listed: the type most of its records are;
  two types listed as often is undecided (the reader is asked to say which);
* a noun no record of the answer is, or words after the phrase that frame a new question
  ("the last email from Unilever"), is no reference: the question is read as typed.

``pick`` finds the phrase; ``resolve`` picks the records or says, in words the reader
can act on, why it cannot. Pure: the caller supplies the previous answer's records (this
reader's own, of this conversation) and checks each picked record is still one the reader
may read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
             "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
             "twelfth": 12, "last": -1}
_ORD = r"(?P<ord>" + "|".join(_ORDINALS) + r"|(?P<num>[1-9]|1[0-9]|20)(?:st|nd|rd|th))"
_NOT_NOUN = r"(?!(?:of|from|to|for|by|in|on|and|or|is|was|are|were|that|which|you|we|i)\b)"
_NOUN = _NOT_NOUN + r"(?P<noun>[a-z]+)"
_PHRASE = re.compile(
    rf"\b(?:the\s+)?{_ORD}(?:\s+(?:one|ones|{_NOUN}))?\b"
    rf"|\b(?P<dem>that|this|those|these)\s+(?:one|ones|{_NOUN.replace('noun', 'dnoun')})\b"
    r"|(?:(?<!of )|(?<=any of ))\b(?P<pro>those|these|them)\b", re.I)
# Words after the phrase that make it a new question's frame, not a pointer.
_FRAMES = re.compile(r"\s*(?:from|to|for|by|about|with|since|before|after|in|on|this|"
                     r"last|of\s+(?!those|these|them)|\d)", re.I)
# A question that only asks to see the records it points at.
_OPEN_ONLY = re.compile(r"^\s*(?:please\s+)?(?:can you\s+|could you\s+)?(?:open|show(?: me)?|"
                        r"go to|pull up|bring up|view|display|see)?\s*(?:please\s*)?$", re.I)
_ALIASES = {"po": ("PurchaseOrder",), "pos": ("PurchaseOrder",), "so": ("SalesOrder",),
            "client": ("Customer",), "vendor": ("Supplier",), "mail": ("Email",),
            "message": ("Email",)}


@dataclass(frozen=True)
class Reference:
    said: str                    # the phrase as the question wrote it
    span: tuple[int, int]
    index: Optional[int] = None  # 1-based; -1 the last; None: a deictic
    plural: bool = False         # "those", "them": every record it keeps
    noun: Optional[str] = None   # the record noun said with it, if any


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if re.search(r"(?:ss|sh|ch|x)es$", word):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _type_words(object_type: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Z][a-z0-9]*", object_type or "")]


def noun_types(noun: str, types: Sequence[str]) -> list[str]:
    """Those of ``types`` the record noun ``noun`` names (its type's last word)."""
    word = noun.lower()
    alias = _ALIASES.get(word) or _ALIASES.get(_singular(word))
    head = _singular(word)
    return [t for t in types if (alias and t in alias)
            or (_type_words(t)[-1:] == [head] if _type_words(t) else False)]


def references(question: str) -> list[Reference]:
    """Each phrase of ``question`` that may point at an earlier answer's records."""
    if not isinstance(question, str) or len(question) > 300:
        return []
    found = []
    for hit in _PHRASE.finditer(question):
        if _FRAMES.match(question, hit.end()):
            continue   # "the last email from ...": a new question's frame
        if hit.group("ord"):
            word = hit.group("ord").lower()
            if word == "last" and hit.group("noun"):
                continue   # "the last emails", "last week": a window or a list, not a pointer
            index = int(hit.group("num")) if hit.group("num") else _ORDINALS[word]
            noun = hit.group("noun")
            if noun is None and not re.match(r"the\s", hit.group(0), re.I) \
                    and not re.search(r"\s(?:one|ones)$", hit.group(0), re.I):
                continue   # a bare "first" ("first, open ..."): no pointer
            span = hit.span()
            tail = re.match(r"\s+of\s+(?:those|these|them)\b", question[span[1]:], re.I)
            if tail:   # "the second of those": one phrase
                span = (span[0], span[1] + tail.end())
            found.append(Reference(question[span[0]:span[1]], span, index=index,
                                   noun=noun.lower() if noun else None))
        elif hit.group("dem"):
            dem = hit.group("dem").lower()
            noun = hit.group("dnoun")
            plural = dem in ("those", "these") or hit.group(0).lower().endswith("ones")
            found.append(Reference(hit.group(0), hit.span(), plural=plural,
                                   noun=noun.lower() if noun else None))
        else:
            found.append(Reference(hit.group(0), hit.span(), plural=True))
    return found


def pick(question: str, earlier: Sequence[Mapping[str, str]]
         ) -> Optional[tuple[Reference, "Resolved"]]:
    """The one pointer of ``question`` at ``earlier`` (the previous answer's records, in
    its order) and what it resolves to; None when the question points at nothing (or at
    two things: read as typed). A phrase whose noun names no record of that answer ("this
    week", "the first quarter") is no pointer."""
    settled = []
    for ref in references(question):
        if names_own_subject(question, ref, earlier):
            continue
        got = resolve(ref, earlier)
        if got is None and ref.noun is not None and ref.index is None and ref.plural:
            # "are those late?": the word after "those" is no record noun; "those" alone points
            word = re.match(r"\w+", ref.said).group(0)
            ref = Reference(word, (ref.span[0], ref.span[0] + len(word)), plural=ref.plural)
            got = resolve(ref, earlier)
        if got is not None:
            settled.append((ref, got))
    return settled[0] if len(settled) == 1 else None


_PARTY_WORDS = ("customer", "client", "supplier", "vendor")


_NOT_A_SUBJECT_AFTER = frozenset({"on", "about", "regarding"})


def _record_words() -> set[str]:
    """The last word of every record type the schema declares, and the party words."""
    # fast_answer first: it finishes loading fast_listing at its tail, so this never starts
    # that import half-way through fast_answer's own (the cycle that left fast_record and
    # fast_chain bound to a stale copy of fast_answer).
    from . import fast_answer  # noqa: F401
    from . import fast_listing
    try:
        schema = fast_listing._schema()
    except (OSError, ValueError, KeyError):   # no schema: the earlier answer's types decide
        schema = None
    if schema is None:
        return set(_PARTY_WORDS)
    words = {w for t, declared in schema.object_types.items()
             for w in _type_words(t)[-1:]
             if not t.startswith("Rel") and fast_listing._dating(declared) is not None
             and fast_listing._party_links(schema, t)}   # the records a list can be of
    return words | set(_PARTY_WORDS)


def names_own_subject(question: str, ref: Reference, earlier: Sequence[Mapping[str, str]]
                      ) -> bool:
    """Whether the rest of ``question`` (outside the phrase) names a record type, a party
    or a name of its own ("the first order Acme ever placed", "suppliers in Ontario and
    email them"): then the phrase is no pointer at the previous answer's records."""
    rest = question[:ref.span[0]] + " " + question[ref.span[1]:]
    kinds = _record_words() | {w for r in earlier for w in _type_words(str(r.get("type")))[-1:]}
    kinds |= {a for a in _ALIASES}
    before = re.findall(r"[A-Za-z]+", question[:ref.span[0]])[-1:]   # "email them": a verb
    words = re.findall(r"[A-Za-z][A-Za-z0-9'&.-]*", rest)
    for at, word in enumerate(words):
        if _singular(word.lower()) in kinds or word.lower() in kinds:
            if word in before or (at > 0 and words[at - 1].lower() in _NOT_A_SUBJECT_AFTER):
                continue   # "late on payment": the pointer's own predicate, no second subject
            return True
        if at > 0 and word[0].isupper() and word != "I":
            return True
    return False


@dataclass(frozen=True)
class Resolved:
    records: tuple[Mapping[str, str], ...]   # the picked records (object_id, type, label)
    why: str = ""                            # why none was picked (one clause), else ""


def resolve(ref: Reference, earlier: Sequence[Mapping[str, str]]) -> Optional[Resolved]:
    """The records of ``earlier`` (the previous answer's, in its order) ``ref`` points at;
    a Resolved with ``why`` when it points but cannot be settled; None when the noun names
    no record of that answer (no reference: the question is read as typed)."""
    records = [r for r in earlier if isinstance(r.get("object_id"), str)]
    if not records:
        if ref.noun is not None:
            return None   # "this week", "that order of ours": read as typed
        return Resolved((), "there is no earlier answer in this conversation listing "
                            "records to point to")
    types = list(dict.fromkeys(str(r.get("type") or "") for r in records))
    if ref.noun is not None:
        kept_types = noun_types(ref.noun, types)
        if not kept_types:
            return None
        noun = ref.noun if ref.noun.endswith("s") else ref.noun + "s"
    else:
        counts = {t: sum(1 for r in records if r.get("type") == t) for t in types}
        top = max(counts.values())
        kept_types = [t for t in types if counts[t] == top]
        if len(kept_types) > 1 and top > 1:
            return Resolved((), "the last answer listed more than one kind of record")
        kept_types = kept_types[:1]
        noun = (_type_words(kept_types[0])[-1:] or ["record"])[0] + "s"
    kept = [r for r in records if r.get("type") in kept_types]
    if ref.plural:
        return Resolved(tuple(kept))
    if ref.index is None:
        if len(kept) == 1:
            return Resolved((kept[0],))
        return Resolved((), f"the last answer listed {len(kept)} {noun}")
    if ref.index == -1:
        return Resolved((kept[-1],))
    if ref.index > len(kept):
        return Resolved((), f"the last answer listed only {len(kept)} {noun}")
    return Resolved((kept[ref.index - 1],))


def opens_only(question: str, ref: Reference) -> bool:
    """Whether ``question`` asks nothing but to see the records ``ref`` points at."""
    rest = (question[:ref.span[0]] + " " + question[ref.span[1]:]).strip(" ?.!")
    return bool(_OPEN_ONLY.match(rest))


def rewritten(question: str, ref: Reference, labels: Sequence[str]) -> str:
    """``question`` with the phrase replaced by the records' own labels."""
    named = ", ".join(f'"{label}"' for label in labels)
    return (question[:ref.span[0]] + named + question[ref.span[1]:]).strip()


def cannot(ref: Reference, why: str) -> str:
    """The one line said when a pointer cannot be settled: can't, why, how to ask again."""
    return (f"I can't tell which record \"{ref.said}\" means: {why}. Ask again with its "
            f"number or name.")
