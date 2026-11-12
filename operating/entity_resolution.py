"""Entity resolution: any name or code in a question, resolved to every record of that
real thing the reader may read, in every system, each with a confidence and why.

``plx.find`` serves it (``OntologyTools._find``): a text query gets ``entities`` beside its
candidates, and ``mode: "entities"`` resolves the mentions of a whole question (``text``)
without the record search, for the Ask's first read (``objective_runner.find_roots``).

* **Mentions** (``mentions``): a short query is one mention; a question's mentions are its
  code-shaped tokens (the identifier grammar's leads, multi-part codes such as
  ``BZ-DEO-21F`` whole) and its name phrases (runs of capitalised words that are not
  scaffolding, a calendar word or the sentence's first word; at most ``MAX_MENTIONS``).
* **Lookup**: the entity index (``search.entities``, ``query_understanding.PgEntityIndex``),
  scoped to the reader in SQL before anything is ranked: a code by its exact aliases, a
  name by trigram word similarity over names and alias words.
* **Expansion**: each record's identity links in the store (``*.same_record_as`` and
  ``Product.bc_posted_as``, at their tier) and every other record carrying one of its codes
  (an alias such as an FM product's Sage code), so a code typed in one system reaches the
  records of the others even when no link was derived.
* **Confidence and provenance** (``why``): an exact code 0.97; a name equal to the mention
  (legal words aside) 0.95; a name that begins with the mention 0.9 for a party (customer,
  supplier, person), 0.7 for anything else (a product named after its brand owner is
  related, not the party); a looser name match its strict similarity scaled under 0.7; an
  identity link by tier (verified/reviewed 0.95, corroborated 0.85, stored_by_text_join
  0.75, lead 0.5); a shared code 0.9. A record is a ``match`` at ``MATCH_AT`` or above,
  else a ``lead``: a lead is never stated as the same thing, only offered.
* **Grouping**: records joined by a shared code, an identity link of tier at least
  stored_by_text_join, or the same normalised party name are one entity.

Permission: the index is the reader's scope; every record is then re-checked by the
store's own gate (``_visible_versions``: audience, markings, time, item ACL) before it is
listed, and its display is the reader's readable value. A record the reader may not read
is never named or counted here. Nothing is written; no source is called.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

MATCH_AT = 0.85
DERIVED_SCORE = 0.8   # a code read out of a longer reference: offered, never the same thing
WEAK_SCORE = 0.7      # most a lower-case word's name match counts for
IN_ORDER_SCORE = 0.86  # the one name differing from the asked only by a plural
IN_ORDER_LEAD = 0.78   # the asked words in order with others between or a letter slip: a lead
MAX_MENTIONS = 6
MAX_ENTITIES = 12
MAX_LEADS = 4      # lead entities listed after the matches (a brand's products: a few)
MAX_RECORDS = 12
MAX_EXPAND = 60
PARTY_TYPES = frozenset({"customer", "supplier", "person", "manufacturer", "contact"})
# Records that carry a name only as one of their participants (a sender or recipient read
# off a message): a name match on one names the message it was read on, never a record of
# that person or party, so it is a lead at most (2026-11-06: an acronym in a question and
# the company's own name each matched a message's participant, stated as the thing).
MESSAGE_TYPES = frozenset({"Email", "TeamsChatMessage", "TeamsChannelMessage"})
# Why a record carrying a generic label's number is offered, not matched (``_entities``).
SHARED_WHY = ("several different records carry this number: read each, answer for the one "
              "the records support, and say the others share the number")
IDENTITY_TIER = {"verified": 0.95, "reviewed": 0.95, "corroborated": 0.85,
                 "stored_by_text_join": 0.75, "lead": 0.5}
GROUPING_TIERS = frozenset({"verified", "reviewed", "corroborated", "stored_by_text_join"})
LEGAL = frozenset("inc incorporated ltd limited llc corp corporation co company plc gmbh sa "
                  "the of and".split())
SYSTEMS = (("bc.", "Business Central"), ("portal.fm_", "FileMaker"), ("portal.sage_", "Sage"),
           ("portal.", "Portal"), ("graph_mail.", "Mail"), ("sharepoint", "SharePoint"),
           ("teams", "Teams"), ("mc.", "Mission Control"))


# A system's name as a question writes where records are held ("in Sage", "in MC"): the
# words before it that make it that place (``mentions``), and the names, folded.
SYSTEM_PLACE_WORDS = frozenset({"in", "into", "within", "outside", "besides", "beyond"})
# Two words that make the system named next the place set apart ("apart from Sage", "other
# than BC"): the records asked about are elsewhere, the system is still no record.
SYSTEM_PLACE_PAIRS = frozenset({("apart", "from"), ("aside", "from"), ("other", "than")})
SYSTEM_WORDS = frozenset({"sage", "bc", "business central", "filemaker", "portal",
                          "the portal", "mission control", "mc", "teams", "microsoft teams",
                          "sharepoint", "outlook", "planner"})


def system_of(object_id: str) -> str:
    binding = object_id.split(":", 2)[1] if object_id.count(":") >= 2 else ""
    return next((name for prefix, name in SYSTEMS if binding.startswith(prefix)), "other")


def _fold(text: str) -> str:
    from operating.query_understanding import fold
    return fold(text)


def _name_key(text: str) -> str:
    """A party name without its legal words (``remcoda llc`` -> ``remcoda``)."""
    return " ".join(w for w in _fold(text).split() if w not in LEGAL)


def _code_aliases(aliases: Iterable[str]) -> set[str]:
    """The aliases that are codes: one token holding a digit (``BZ-DEO-21F``, ``REMCO01``),
    never an attribute word (``currency USD``, ``FG``) or a labelled copy (``Item X``)."""
    out = set()
    for alias in aliases:
        alias = str(alias or "").strip()
        if alias and " " not in alias and re.search(r"\d", alias) and len(alias) <= 40 \
                and not (alias.isdigit() and len(alias) < 5):
            out.add(alias.upper())   # a short bare number names too many things
    return out


_CAPITALISED = re.compile(r"[A-Z][\w&'’.-]*")


def mentions(text: str) -> list[dict[str, Any]]:
    """The mentions to resolve in ``text``: ``{"text", "kind": "code"|"name", "forms"}``.
    A short text (at most four words, no question) is itself one mention of each kind."""
    from ontology.identifiers import is_quantity
    from operating.query_understanding import STOPWORDS, _CALENDAR_WORDS, identifier_leads
    text = " ".join(str(text or "").split())
    if not text:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(span: str, kind: str, forms: Sequence[str] = (), lead: Any = None) -> None:
        key = (kind, span.upper())
        if span and str(key) not in seen and len(out) < MAX_MENTIONS:
            seen.add(str(key))
            out.append({"text": span, "kind": kind,
                        "forms": sorted({f for f in forms if f} | {span.upper()})})
            number = re.search(r"\d[\w-]*$", span)
            if lead is not None and not lead.exact and len(lead.types or ()) > 1 \
                    and number is not None \
                    and not is_quantity(text, number.group(0), lead.span[1]):
                # a label naming several kinds of record ("order 465": a sales or a
                # purchase order): the number is looked up on each (``SHARED_WHY``);
                # never a quantity ("order 500 units")
                out[-1]["label_types"] = sorted(lead.types)

    words = text.split()
    if len(words) <= 4 and "?" not in text:
        # a code typed with a space or a label word ("PO 3360", "invoice IN100"): the
        # grammar's own forms of it, never the label glued into the code
        leads = [lead for lead in identifier_leads(text)
                 if not re.fullmatch(r"[\d,.]+", text[lead.span[0]:lead.span[1]])]
        rest = text
        for lead in sorted(leads, key=lambda lead: -lead.span[0]):
            add(text[lead.span[0]:lead.span[1]], "code", lead.forms, lead)
            rest = rest[:lead.span[0]] + " " + rest[lead.span[1]:]
        if leads and len(rest.split()) <= 1:
            return out   # the code and at most a label word: nothing else to name
        if not leads:
            add(text, "code" if re.search(r"\d", text) else "name")
        if re.search(r"\d", text) and len(words) > 1:
            add(text, "name")
        return out
    taken: list[tuple[int, int]] = []
    for lead in identifier_leads(text):
        span = text[lead.span[0]:lead.span[1]]
        if re.fullmatch(r"[\d,.]+", span):
            continue   # a bare number is a quantity or a year far more often than a record
        add(span, "code", lead.forms, lead)
        taken.append(lead.span)
    sentence_start = {0} | {m.end() for m in re.finditer(r"[.!?]\s+", text)}
    run: list[str] = []
    before: tuple[str, str] = ("", "")
    last: tuple[str, str] = ("", "")   # the two words before this one, "the" skipped

    def close() -> None:
        # "sales orders in Sage", "approvals in the Portal", "apart from Sage": the system
        # the records are held in (or set apart), never a record named so (the vendor Sage
        # Software, read first as the subject)
        place = before[1] in SYSTEM_PLACE_WORDS or before in SYSTEM_PLACE_PAIRS
        if not (place and _fold(" ".join(run)) in SYSTEM_WORDS):
            add(" ".join(run), "name")

    for match in re.finditer(r"\S+", text):
        token = match.group(0).strip(",;:()\"'?!.")
        inside = any(a <= match.start() < b for a, b in taken)
        word = _fold(token)
        name_like = (bool(_CAPITALISED.fullmatch(token)) and not inside
                     and match.start() not in sentence_start and word not in STOPWORDS
                     and word not in _CALENDAR_WORDS and len(token) > 1
                     and (not re.search(r"\d", token) or len(token) <= 4))
        if name_like:
            if not run:
                before = last
            run.append(token)
        elif run:
            close()
            run = []
        if word != "the":
            last = (last[1], word)
    if run:
        close()
    return out


def _name_score(row: Any, key: str, text: str, loose: bool = True
                ) -> tuple[float, str] | None:
    """(score, why) of an index row's name against a name mention's key (module doc), or
    None when it is only a loose likeness (or ``loose`` is False and it is not closer)."""
    name = _name_key(row.normalized or row.name)
    party = row.type in PARTY_TYPES
    if name == key:
        return 0.95, f"its name is \"{text}\""
    if name.startswith(key + " ") or (" " + key + " ") in (" " + name + " "):
        if party and name.startswith(key + " "):
            return 0.9, f"its name begins with \"{text}\""
        if " " in key:
            # a phrase of several words, whole in the name: that record names it
            return 0.88, f"its name holds the phrase \"{text}\""
        return 0.7, f"its name contains \"{text}\" (related, not the same)"
    if " " in key and _plural_only(key.split(), name.split()):
        return IN_ORDER_SCORE, f"its name is \"{text}\" but for a plural"
    if " " in key and _in_order(key.split(), name.split()):
        return IN_ORDER_LEAD, f"its name holds the words of \"{text}\" in order (a lead)"
    strict = float(getattr(row, "strict", 0.0) or 0.0)
    if strict < 0.8 or not loose:
        return None   # a loose likeness names nothing
    return round(0.7 * strict, 2), f"its name is like \"{text}\""


def _by_whole_name(row: Any, terms: Sequence[str], words: Sequence[str]) -> bool:
    """Whether an index row was found by the whole name asked (its term, or an alias equal
    to it), not by one of its words alone."""
    matched = getattr(row, "matched", "") or ""
    if matched == "alias":   # the index's exact alias branch: which alias, then
        return any(_fold(str(a)) in terms for a in row.alias)
    return matched not in words


def _same_word(asked: str, held: str) -> bool:
    """A word of a name as asked: equal, or for a word of five letters or more its plural
    or one letter apart (a slip in either spelling: ``logisitics``)."""
    if asked == held:
        return True
    if len(asked) < 5 or len(held) < 5:
        return False
    from operating.query_understanding import _edits
    return held in (asked + "s", asked + "es") or asked in (held + "s", held + "es") \
        or _edits(asked, held, 1) <= 1


def _plural_only(asked: Sequence[str], held: Sequence[str]) -> bool:
    """The same words in the same order, some only as their plural (no word between, no
    letter slip)."""
    return len(asked) == len(held) and all(
        a == h or h in (a + "s", a + "es") or a in (h + "s", h + "es")
        for a, h in zip(asked, held))


def _in_order(asked: Sequence[str], held: Sequence[str]) -> bool:
    """Every asked word is a word of the name, in the asked order (others between)."""
    at = 0
    for word in asked:
        while at < len(held) and not _same_word(word, held[at]):
            at += 1
        if at == len(held):
            return False
        at += 1
    return True


class _Index:
    """One ``PgEntityIndex`` per process (its connection reused), opened on first use."""
    _lock = threading.Lock()
    _index: Any = None

    @classmethod
    def get(cls) -> Any:
        import os
        if not os.environ.get("PLX_PG_DSN"):
            return None   # no search database configured (a fixture host): no index
        with cls._lock:
            if cls._index is None:
                from operating.query_understanding import PgEntityIndex
                cls._index = PgEntityIndex()
            return cls._index


class Resolver:
    """Entity resolution for one host's ``OntologyTools`` (``base``): its store, gate,
    policy and displays. ``index``: the entity index (default the process's
    ``PgEntityIndex``); a callable returning one, or None for none (tests)."""

    def __init__(self, base: Any, index: Any = "default") -> None:
        self.base = base
        self._index = index

    def index(self) -> Any:
        if self._index == "default":
            return _Index.get()
        return self._index() if callable(self._index) else self._index

    # ------------------------------------------------------------------ resolve

    def resolve(self, scope: Any, actor: Any, text: str,
                object_type: str | None = None, lock: Any = None
                ) -> tuple[list[dict[str, Any]], list[dict]]:
        """(entities, gaps) for the mentions of ``text`` (module doc). ``lock``: the
        store's lock, taken around each store read only (the entity index is read
        without it); None: the caller holds it."""
        from contextlib import nullcontext
        held = lock if lock is not None else nullcontext()
        found = mentions(text)
        try:
            index = self.index()
            escope = index.scope(self.base.policy, actor) if index is not None else None
        except Exception:  # noqa: BLE001 - no index: no entities, said as a gap
            index, escope = None, None
        if index is not None and escope is not None:
            found += _distinctive(index, escope, text, found)
        if not found:
            return [], []
        if index is None or escope is None:
            return [], [_gap("entity_index_unavailable", "entity index",
                             "the entity index could not be read; records were searched "
                             "by the record search alone")]
        scores: dict[str, dict[str, Any]] = {}
        for mention in found:
            for row, score, why in self._lookup(index, escope, mention):
                if mention.get("weak"):
                    score = min(score, WEAK_SCORE)   # a lower-case word: a lead at most
                self._note(scores, row.object_id, score, why, mention["text"],
                           row=row)
        for mention in found:
            if mention.get("label_types"):
                for row, why, key in self._stored(scope, mention, held):
                    if row["object_id"] not in scores:
                        self._note(scores, row["object_id"], 0.97, why, mention["text"])
                        scores[row["object_id"]]["type"] = row["object_id"].split(":", 1)[0]
                    scores[row["object_id"]].setdefault("keys", set()).add(key)
        if not scores:
            return [], []
        # the store's gate first: nothing the reader may not read is matched or expanded
        with held:
            visible = self.base._visible_versions(scope, list(scores))
        scores = {oid: s for oid, s in scores.items() if oid in visible}
        self._expand(index, escope, scope, scores, held)
        with held:
            visible = self.base._visible_versions(scope, list(scores))
            scores = {oid: s for oid, s in scores.items() if oid in visible}
            displays = self.base._displays(scope, sorted(scores))
        shared = {m["text"] for m in found if m.get("label_types")}
        return self._entities(scores, visible, displays, object_type, shared), []

    def _stored(self, scope: Any, mention: Mapping[str, Any], held: Any):
        """(row, why, key) of the store's identifiers equal to a generic label's number
        (``mentions``' ``label_types``), of those types only, as this reader may read
        them: a record the entity index does not carry (a FileMaker order entry known by
        its number alone) is still found. ``key`` joins one record's copies (the same
        number in the same property of one type)."""
        types = set(mention["label_types"])
        forms = sorted({f for f in mention["forms"] if f and " " not in f})
        store = getattr(self.base, "store", None)
        reader = getattr(store, "identifiers_by_value", None)
        if not forms or not callable(reader):
            return []
        try:
            with held:
                # the label's types and the cap in the statement, in a set order: other
                # records sharing a short number never push the asked type past the cap
                rows = list(reader(scope, forms, types=sorted(types), cap=MAX_EXPAND))
        except Exception:  # noqa: BLE001 - the store's identifiers are added value only
            return []
        out = []
        for row in rows[:MAX_EXPAND]:
            oid = str(row["object_id"])
            if oid.split(":", 1)[0] not in types:
                continue   # a store answering past its filter: still only those types
            out.append((row, f"its identifier {row['raw']} is the number asked",
                        f"{oid.split(':', 1)[0]}|{row['property']}|{row['normalized']}"))
        return out

    def _lookup(self, index: Any, escope: Any, mention: Mapping[str, Any]):
        text = mention["text"]
        if mention["kind"] == "code":
            rows, _hidden = index.by_identifier(list(mention["forms"]), escope)
            for row in rows:
                yield row, 0.97, f"its code {row.matched or text} is the code asked"
            seen = {row.object_id for row in rows}
            try:
                split = _split_values(index, escope, text)
            except Exception:  # noqa: BLE001 - a split lookup is added value only
                split = []
            for row, held in split:
                if row.object_id not in seen:
                    # derived, never the same thing: the model checks its type and context
                    yield row, DERIVED_SCORE, (f"possible: split from {held} (a value of a "
                                               "longer reference, or the number without its "
                                               "letters)")
            return
        # the name as folded today and as an index row written before the fold dropped
        # apostrophes holds it ("plant s power"): both are looked up and compared
        spellings = list(dict.fromkeys((text, text.replace("'", " ").replace("’", " "))))
        terms = [_fold(t) for t in spellings]
        keys = [k for k in dict.fromkeys(_name_key(t) for t in spellings) if k]
        if not keys:
            return
        # each distinctive word of a name of several words is looked up too, so a name
        # whose other words are common ("X Logistics" among dozens of logistics firms)
        # is still among the rows; a row found by one word alone is scored on the whole
        # name and never as a loose likeness
        words = sorted({w for k in keys for w in k.split()
                        if len(w) >= 3 and len(k.split()) > 1} - set(terms))
        rows, _hidden = index.by_terms(list(dict.fromkeys(terms)) + words, escope)
        best: dict[str, tuple[Any, float, str]] = {}
        for row in rows:
            for key in keys:
                got = _name_score(row, key, text, loose=_by_whole_name(row, terms, words))
                held = best.get(row.object_id)
                if got is not None and (held is None or got[0] > held[1]):
                    best[row.object_id] = (row, got[0], got[1])
        # a name differing only by a plural names a record only when it is the one such
        # name and nothing is named more closely; otherwise it is a lead (two firms a
        # letter apart, "Prater" and "Crater", are never both the one asked)
        plural = {_name_key(row.normalized or row.name)
                  for row, score, _why in best.values() if score == IN_ORDER_SCORE}
        closer = any(score > IN_ORDER_SCORE for _row, score, _why in best.values())
        for row, score, why in best.values():
            if score == IN_ORDER_SCORE and closer:
                score, why = WEAK_SCORE, why + " (a closer name was found)"
            elif score == IN_ORDER_SCORE and len(plural) > 1:
                score, why = IN_ORDER_LEAD, why + " (several names differ so)"
            if str(row.object_id).split(":", 1)[0] in MESSAGE_TYPES:
                score = min(score, WEAK_SCORE)
                why += " (a participant named on a message)"
            yield row, score, why

    @staticmethod
    def _note(scores: dict[str, dict[str, Any]], object_id: str, score: float, why: str,
              mention: str, row: Any = None, codes: Iterable[str] = ()) -> None:
        entry = scores.setdefault(object_id, {"score": 0.0, "why": [], "mention": mention,
                                              "codes": set(), "name": "", "type": ""})
        if score > entry["score"]:
            entry["score"], entry["mention"] = score, mention
        if why not in entry["why"]:
            entry["why"].append(why)
        if row is not None:
            entry["codes"] |= _code_aliases(row.alias)
            entry["name"] = entry["name"] or _name_key(row.normalized or row.name)
            entry["type"] = entry["type"] or row.type
            if row.type == "person":
                # a participant row per mailbox: its first alias is the address
                address = next((a for a in row.alias[:1] if "@" in str(a)), None)
                if address:
                    entry["address"] = str(address).lower()
        entry["codes"] |= set(codes)

    def _expand(self, index: Any, escope: Any, scope: Any,
                scores: dict[str, dict[str, Any]], held: Any) -> None:
        """Identity links and shared codes of the confident records (module doc)."""
        confident = [oid for oid, s in scores.items() if s["score"] >= MATCH_AT]
        if not confident:
            return
        codes = sorted({c for oid in confident for c in scores[oid]["codes"]})[:20]
        if codes:
            try:
                rows, _hidden = index.by_identifier(codes, escope)
            except Exception:  # noqa: BLE001 - expansion is best effort
                rows = []
            for row in rows[:MAX_EXPAND]:
                shared = sorted(_code_aliases(row.alias) & set(codes))
                # the same kind of record only: an order carrying its supplier's code is
                # linked to that supplier, never the supplier itself
                holders = [o for o in confident if set(shared) & scores[o]["codes"]
                           and scores[o]["type"] == row.type]
                if not shared or not holders:
                    continue
                source = scores[holders[0]]["mention"]
                self._note(scores, row.object_id, 0.9,
                           f"it carries the code {shared[0]} of a matched record", source,
                           row=row)
        types = self._identity_link_types()
        if not types:
            return
        ids = [oid for oid, s in scores.items() if s["score"] >= MATCH_AT][:MAX_EXPAND]
        with held:
            links = self.base.store.links_touching(scope, ids, types)[:MAX_EXPAND * 4]
        for link in links:
            tier = link["tier"]
            if tier not in IDENTITY_TIER:
                continue
            ends = (link["from_object"], link["to_object"])
            for here, other in (ends, ends[::-1]):
                if here in scores and scores[here]["score"] >= MATCH_AT \
                        and here.split(":", 1)[0] == other.split(":", 1)[0]:
                    score = round(min(scores[here]["score"], IDENTITY_TIER[tier]), 2)
                    self._note(scores, other, score,
                               f"{link['link_type']} ({tier}) to a matched record",
                               scores[here]["mention"])
                    if tier in GROUPING_TIERS:
                        scores[other].setdefault("joined", set()).add(here)

    def _identity_link_types(self) -> list[str]:
        schema = getattr(self.base, "schema", None)
        declared = getattr(schema, "link_types", None) or {}
        return sorted(t for t in declared if t.endswith(".same_record_as")
                      or t == "Product.bc_posted_as")

    def _entities(self, scores: Mapping[str, Mapping[str, Any]], visible: Mapping[str, Any],
                  displays: Mapping[str, Any], object_type: str | None,
                  shared: Iterable[str] = ()) -> list[dict]:
        ids = sorted(scores)
        parent = {oid: oid for oid in ids}

        def root(oid: str) -> str:
            while parent[oid] != oid:
                parent[oid] = parent[parent[oid]]
                oid = parent[oid]
            return oid

        def join(a: str, b: str) -> None:
            ra, rb = root(a), root(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        by_code: dict[str, str] = {}
        by_name: dict[str, str] = {}
        by_address: dict[str, str] = {}
        for oid in ids:
            entry = scores[oid]
            if entry.get("address"):
                # one person seen in several mailboxes is one person
                if entry["address"] in by_address:
                    join(oid, by_address[entry["address"]])
                else:
                    by_address[entry["address"]] = oid
            for code in [*entry["codes"], *sorted(entry.get("keys", ()))]:
                if code in by_code:
                    join(oid, by_code[code])
                else:
                    by_code[code] = oid
            if entry["type"] in PARTY_TYPES and entry["name"] and entry["score"] >= MATCH_AT:
                key = (entry["type"], entry["name"])
                if str(key) in by_name:
                    join(oid, by_name[str(key)])
                else:
                    by_name[str(key)] = oid
            for other in entry.get("joined", ()):
                if other in parent:
                    join(oid, other)
        groups: dict[str, list[str]] = {}
        for oid in ids:
            groups.setdefault(root(oid), []).append(oid)
        out = []
        for members in groups.values():
            members.sort(key=lambda o: (-scores[o]["score"], o))
            if object_type is not None and not any(
                    o.split(":", 1)[0] == object_type for o in members):
                continue
            best = members[0]
            confidence = round(max(scores[o]["score"] for o in members), 2)
            records = []
            for oid in members[:MAX_RECORDS]:
                score = round(scores[oid]["score"], 2)
                status = "match" if score >= MATCH_AT else (
                    "derived" if score == DERIVED_SCORE else "lead")
                records.append({
                    "object": {"object_id": oid, "object_type": oid.split(":", 1)[0],
                               "display": displays.get(oid),
                               "lineage": visible[oid]["lineage"]},
                    "system": system_of(oid), "confidence": score,
                    "status": status,
                    "why": [_safe(w) for w in scores[oid]["why"][:4]]})
            entity = {"mention": scores[best]["mention"][:200],
                      "object_type": best.split(":", 1)[0],
                      "confidence": confidence,
                      "status": "match" if confidence >= MATCH_AT else (
                          "derived" if confidence == DERIVED_SCORE else "lead"),
                      "systems": sorted({system_of(o) for o in members
                                         if scores[o]["score"] >= MATCH_AT}) or
                                 [system_of(best)],
                      "records": records}
            if len(members) > MAX_RECORDS:
                entity["more_records"] = len(members) - MAX_RECORDS
            out.append(entity)
        out.sort(key=lambda e: (-e["confidence"], -len(e["records"]), e["mention"]))
        # A generic label's number ("order 465") carried by several different records
        # (a cancelled purchase order and a sales order, W-0070 11-10) names none of
        # them as the one: each is offered, to be read, never stated as the thing asked
        # (``SHARED_WHY``; the Ask's brief says so, and none is a root).
        for mention in shared:
            alike = [e for e in out if e["mention"] == mention[:200]
                     and e["status"] == "match"]
            if len(alike) > 1:
                for entity in alike:
                    entity["status"], entity["shares_number"] = "derived", True
                    for record in entity["records"]:
                        if record["status"] == "match":
                            record["status"] = "derived"
                            record["why"] = [SHARED_WHY, *record["why"]][:4]
        matches = [e for e in out if e["status"] == "match"]
        # Each asked code's records read out of a longer reference (``derived``) are the
        # only records that code names: the first of every code is kept before any lead
        # or another code's second, never cut by the leads' bound (2026-11-05: a question
        # asking four purchase orders lost the fourth's reference to the cap).
        firsts, seconds, mentions = [], [], set()
        for entity in (e for e in out if e["status"] == "derived"):
            (seconds if entity["mention"] in mentions and not entity.get("shares_number")
             else firsts).append(entity)
            mentions.add(entity["mention"])
        leads = [e for e in out if e["status"] == "lead"][:MAX_LEADS]
        return (matches + firsts + leads + seconds)[:MAX_ENTITIES]


def _safe(text: str) -> str:
    """A why line in the Workbench's own words: codes and names folded to plain
    characters (the contract's pattern), never source text verbatim."""
    out = re.sub(r"[^A-Za-z0-9 _.,:()/#&+-]", "", text.replace('"', "")).strip()
    return (out or "matched")[:200]


def _distinctive(index: Any, escope: Any, text: str, found: Sequence[Mapping[str, Any]]
                 ) -> list[dict[str, Any]]:
    """Name mentions written in lower case: each word of the text (four letters or more,
    no scaffolding, business noun or calendar word, not inside a mention already found)
    that names between one and a few records this reader may see
    (``PgEntityIndex.distinctive_words``): a word in a name, never a catalogue word."""
    from operating.query_understanding import STOPWORDS, _CALENDAR_WORDS
    taken = {w for m in found for w in _fold(m["text"]).split()}
    words = []
    for token in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text or ""):
        word = _fold(token)
        if word and word not in STOPWORDS and word not in _CALENDAR_WORDS \
                and word not in taken and word not in words:
            words.append(word)
    if not words or len(found) >= MAX_MENTIONS:
        return []
    try:
        named = _named_words(index, escope, words[:20])
    except Exception:  # noqa: BLE001 - lower-case names are added value only
        return []
    return [{"text": word, "kind": "name", "forms": [word.upper()], "weak": True}
            for word in words if word in named][:MAX_MENTIONS - len(found)]


DISTINCTIVE_MAX = 5   # a word in more visible names than this is a catalogue word
SPLIT_LIMIT = 50      # records a split-value probe reads
_SEPARATORS = re.compile(r"\s*(?:[/,;&+|]|\band\b)\s*", re.I)
_CODE_PARTS = re.compile(r"^([A-Za-z]*[^A-Za-z0-9]?)?(\d+)(.*)$")


def components(value: str) -> list[str]:
    """The values a reference field holds (``PO0101004/ 0103440``, ``A, B and C``): split
    on separators; a part written as digits only continues the first part's letters
    (``0103440`` after ``PO0101004`` is ``PO0103440``); a part with a short ``-N`` or
    ``-X`` suffix also stands as the value without it (``PO0101004-1``)."""
    parts = [p.strip() for p in _SEPARATORS.split(value or "") if p and p.strip()]
    if len(parts) == 1 and not re.search(r"-\w{1,2}$", parts[0]):
        return []
    out: list[str] = []
    prefix = None
    for part in parts:
        found = _CODE_PARTS.match(part.replace(" ", ""))
        if not found:
            continue
        letters, digits, rest = found.groups()
        letters = letters or ""
        if letters:
            prefix = prefix or letters
        elif prefix:
            letters = prefix
        out.append((letters + digits + rest).upper())
        if re.fullmatch(r"-\w{1,2}", rest or ""):
            out.append((letters + digits).upper())
    return list(dict.fromkeys(out))


def _split_values(index: Any, escope: Any, asked: str) -> list[tuple[Any, str]]:
    """Visible records whose reference holds ``asked`` as one of several values or with
    a short suffix (``components``): one probe on the trigram index for the asked code's
    digits as a whole word, then each candidate's aliases split and compared."""
    from operating.query_understanding import EntityRow
    key = re.sub(r"[^0-9A-Za-z]", "", asked).upper()
    digits = re.search(r"(\d{4,})", key)
    if not digits:
        return []
    table = getattr(index, "table", "search.entities")
    probe = getattr(index, "split_candidates", None)
    if callable(probe):   # an index answering it itself (a fixture's)
        rows = probe(digits.group(1), escope)
    else:
        visible = index._visible(escope)
        rows = index._run(
            f"SELECT {index._COLS} FROM {table} e WHERE search.alias_text(e.alias) ~ "
            f"%(digits)s AND {visible} LIMIT %(lim)s",
            index._params(escope, None, digits=digits.group(1), lim=SPLIT_LIMIT))
        rows = [EntityRow(r[0], r[1], r[2], r[3] or "", r[4] or "", tuple(r[5] or ()))
                for r in rows]
    # the asked code's number alone ("PO0103510" as 0103510): a reference written
    # without its letters is the same number, said as split from it
    core = digits.group(1) if re.fullmatch(r"[A-Z]{1,4}\d{4,}", key) else None
    out = []
    for row in rows:
        for alias in row.alias:
            values = {re.sub(r"[^0-9A-Za-z]", "", v).upper()
                      for v in components(str(alias)) or [str(alias)]}
            if (key in values or (core is not None and core in values)) \
                    and re.sub(r"[^0-9A-Za-z]", "", str(alias)).upper() != key:
                out.append((row, str(alias)))
                break
    return out


def _named_words(index: Any, escope: Any, words: Sequence[str]) -> set[str]:
    """The words that are a whole word of the names of between one and
    ``DISTINCTIVE_MAX`` records this reader may see: one probe per word on the trigram
    index (a word-boundary match), the reader's scope applied in SQL."""
    probe = getattr(index, "named_words", None)
    if callable(probe):   # an index answering it itself (a fixture's)
        return set(probe(words, escope))
    table = getattr(index, "table", "search.entities")
    visible = index._visible(escope)
    sql = (f"SELECT q.w FROM unnest(%(words)s::text[]) AS q(w) WHERE (SELECT count(*) FROM "
           f"(SELECT 1 FROM {table} e WHERE e.normalized ~ ('\\m' || q.w || '\\M') AND "
           f"{visible} LIMIT %(cap)s) x) BETWEEN 1 AND %(most)s")
    rows = index._run(sql, index._params(escope, None, words=list(words),
                                         cap=DISTINCTIVE_MAX + 1, most=DISTINCTIVE_MAX))
    return {row[0] for row in rows}


def _gap(reason: str, subject: str, detail: str) -> dict[str, Any]:
    return {"state": "unknown", "reason": reason, "subject": subject, "detail": detail}


__all__ = ["MATCH_AT", "Resolver", "mentions", "system_of"]
