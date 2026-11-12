"""The graph-walk family: questions that follow links between any record types, answered by
executing the walk over the projection.

The curated hard questions cover the paths someone thought of. This family covers the link
graph itself: every link type of the served schema that has current firm links gets walks
in proportion to the square root of its link count (``weights``: millions of mail links do
not crowd out the rest), with a floor (``FLOOR``) for the rare ones. A walk (``Composer``)

* starts on a sampled link of its link type, at an end that people name (a number, a code
  or a name: ``NAME_PROPS``) and that the name picks out in its system;
* takes 1 to 4 hops (``HOP_WEIGHTS``) along the schema's link types, either way, preferring
  the rarer ones, with every hop executed over **all** current firm links of the records
  so far (``Records.step``), never a sample;
* filters one of its sets on a property (a date range, a status, an amount or a word of the
  name), and asks a count, a sum, a minimum, a maximum, the latest date or the list of the
  last set, or compares the counts of two branches from the start.

Questions are phrased from the schema's own names (types, link names and reverse names,
property names: ``noun``, ``words``), name the start record as people do, sometimes with a
typo (``evals.blind_mined.typo``) or a relative date ("last year", "since last March").
The answer is the walk's own execution (the oracle), with the evidence records' handles; a
walk the records cannot settle (an ambiguous start, a filter some record cannot be judged
on, a sum over mixed currencies, a tie) is dropped with its reason. An empty last set is
the honest answer "none" (an ``absent`` fact), kept for about ``NONE_SHARE`` of the items.

Items are ``plx-eval-item/v1`` rubric items of family ``F31`` with live-ops keys (suite
``graph``), so ``scripts.eval_live_ops run`` and ``rescore`` run and grade them unchanged;
each key carries its ``strata`` (hops, system pair, link types, answer kind) for the
stratified sample (``stratified``) and the per-stratum report. Pure: reads no source,
clock or environment (``Records`` is the caller's).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import math
import random
import re
from typing import Any, Iterable, Mapping, Protocol, Sequence

from evals import blind_mined as bm

FAMILY = "F31"
SUITE = "graph"
KEY_FAMILY = "graph_walk"
ORACLE_ID = "oracle:graph-walk"
FLOOR = 3
HOP_WEIGHTS = {1: 0.3, 2: 0.33, 3: 0.22, 4: 0.15}
MAX_SET = 150
MAX_START = 1
MAX_LIST = 8
NONE_SHARE = 0.10
TYPO_SHARE = 0.12
RELATIVE_SHARE = 0.35
COMPARE_SHARE = 0.12
MARKINGS = frozenset({"finance", "purchasing_prices", "quality_controlled"})
# The properties people name a record by, most specific first: a document number or code,
# then a name or title. Any type's first one present names it.
NAME_PROPS = ("po_number", "order_number", "invoice_number", "ao_code", "batch_code",
              "receipt_number", "shipment_number", "quote_number", "pr_number", "lot_number",
              "release_number", "bc_number", "name", "display_name", "title", "topic",
              "subject", "batch_name", "code", "number", "task_id")
STATUS = re.compile(r"(?:^|_)(?:status|stage|state)$")
NUMERIC = ("money", "quantity", "decimal", "integer")
NOT_AMOUNT = re.compile(r"(?:_number|_id|^number|_code|^code|fm_\w+|_uniq|_days|_percent|"
                        r"conversion|rate|_count|size|score|priority)$|^(?:entry_number|"
                        r"fm_batch_number|cue_count)$")
DATES = ("date", "timestamp")
SYSTEM_WORDS = {"BC": "Business Central", "Portal": "the Portal", "FileMaker": "FileMaker",
                "Sage": "Sage", "mail": "mail", "Teams": "Teams", "SharePoint": "SharePoint",
                "meetings": "meetings", "MC": "Mission Control"}
SHORT = {"PurchaseOrder": "PO", "AssemblyOrder": "AO"}
ACRONYMS = {"MC": "Mission Control", "Bc": "BC", "Fg": "FG", "Gl": "GL", "Teams": "Teams",
            "SharePoint": "SharePoint"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
NONE_FORMS = ("none", "no ", "zero", "not any", "nothing", "there are no", "there were no",
              "no matching", "no such", "did not find any", "found no", "0 ")


class Records(Protocol):
    """What a walk reads (the caller's store, read-only)."""

    def sample(self, link_type: str) -> Sequence[tuple[str, str]]: ...
    def step(self, ids: Sequence[str], link_type: str, direction: str
             ) -> list[tuple[str, str]]: ...
    def rows(self, ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]: ...
    def versions(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]: ...
    def lookup(self, object_type: str, label: str) -> list[str] | None: ...
    def cleared(self, ids: Sequence[str]) -> bool: ...


@dataclass(frozen=True)
class Link:
    id: str
    name: str
    reverse_name: str
    source: str
    target: str
    count: int = 0

    @property
    def identity(self) -> bool:
        return self.name in ("same_record_as", "bc_posted_as") or \
            self.reverse_name in ("same_record_as",)


@dataclass
class Hop:
    link: Link
    direction: str   # "out": from -> to; "in": to -> from

    @property
    def target(self) -> str:
        return self.link.target if self.direction == "out" else self.link.source


@dataclass
class Walk:
    """One composed item: its question, facts and strata (or why it was dropped)."""
    question: str
    facts: list[dict[str, Any]]
    kind: str
    hops: list[str]
    systems: str
    start: str
    start_type: str
    label: str
    reads: list[str] = field(default_factory=list)
    rule: str = ""
    variant: str = "standard"


# --------------------------------------------------------------------------- systems

def object_type(object_id: str) -> str:
    return object_id.split(":", 1)[0]


def binding_of(object_id: str) -> str:
    parts = object_id.split(":", 2)
    return parts[1] if len(parts) > 2 else ""


def mirror_type(object_type_: str) -> bool:
    """A raw relation mirror type (``Rel...``): a source table with no business label."""
    return object_type_.startswith("Rel")


def system_of(binding: str) -> str:
    """The business system a binding reads: BC, the Portal's own tables, FileMaker (the
    Portal's ``fm_`` and silver mirrors), Sage, mail, Teams, SharePoint, meetings, MC."""
    parts = binding.split(".")
    if parts and parts[0] == "gen":
        parts = parts[1:]
    head, rest = (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")
    if head == "bc":
        return "BC"
    if head in ("portal", "public", "silver", "sage"):
        if head == "sage" or rest.startswith("sage_"):
            return "Sage"
        if head == "silver" or rest.startswith(("fm_", "silver_")):
            return "FileMaker"
        return "Portal"
    return {"graph_mail": "mail", "sharepoint": "SharePoint", "teams": "Teams",
            "meetings": "meetings", "mission_control": "MC"}.get(head, head or "unknown")


def fact_system(handle: str) -> str | None:
    """The live-ops evidence system of a citation handle (``evals.live_ops.SYSTEMS``)."""
    scheme = handle.split("://", 1)[0]
    return {"bc": "bc", "api": "api", "graph-mail": "graph-mail", "teams-chat": "teams-chat",
            "teams-channel": "teams-channel", "calendar-event": "calendar-event",
            "mc": "mc", "sharepoint": "sharepoint"}.get(scheme)


# --------------------------------------------------------------------------- words

def words(name: str) -> str:
    return re.sub(r"_+", " ", name).strip()


def noun(object_type_: str) -> str:
    """"PurchaseOrder" -> "purchase order", "MCTask" -> "Mission Control task"."""
    out = []
    for part in re.findall(r"SharePoint|[A-Z]{2,}(?=[A-Z][a-z]|$)|[A-Z][a-z]*|[a-z]+|\d+",
                           object_type_):
        out.append(ACRONYMS.get(part, part if part.isupper() else part.lower()))
    return " ".join(out)


def plural(phrase: str) -> str:
    if " of " in phrase:   # "certificate of analysis" -> "certificates of analysis"
        head, _, tail = phrase.partition(" of ")
        return f"{plural(head)} of {tail}"
    head, _, last = phrase.rpartition(" ")
    if last.endswith("is"):
        last = last[:-2] + "es"
    elif last.endswith("y") and last[-2:-1] not in "aeiou":
        last = last[:-1] + "ies"
    elif last.endswith(("s", "x", "ch", "sh")):
        last = last + "es"
    else:
        last = last + "s"
    return f"{head} {last}".strip()


def weights(links: Sequence[Link]) -> dict[str, float]:
    """Each link type's share of the walks: the square root of its link count."""
    roots = {l.id: math.sqrt(l.count) for l in links if l.count > 0}
    total = sum(roots.values()) or 1.0
    return {k: v / total for k, v in roots.items()}


def quotas(links: Sequence[Link], items: int) -> dict[str, int]:
    """Walks wanted per link type (as the first hop): its share of ``items``, at least
    ``FLOOR``."""
    return {k: max(FLOOR, round(items * w)) for k, w in weights(links).items()}


def _hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()


# --------------------------------------------------------------------------- values

def _decimal(value: Any) -> Decimal | None:
    try:
        out = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _day(value: Any) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None
    except ValueError:
        return None


def _one(rows: Sequence[Mapping[str, Any]], prop: str) -> Mapping[str, Any] | None:
    """The record's one current value of ``prop`` (None when absent, empty or two
    sources disagree)."""
    found = [r for r in rows if r["property"] == prop and r.get("value") not in bm.EMPTY]
    if not found:
        return None
    if len({str(r["value"]).strip().casefold() for r in found}) != 1:
        return None
    return found[0]


def label_of(rows: Sequence[Mapping[str, Any]]) -> tuple[str, str] | None:
    """(how people name the record, the property) from ``NAME_PROPS``."""
    for prop in NAME_PROPS:
        row = _one(rows, prop)
        if row is not None:
            text = re.sub(r"\s+", " ", str(row["value"])).strip()
            if 2 <= len(text) <= 60 and not text.startswith(("{", "[")):
                return text, prop
    return None


def markings(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        raw = row.get("markings") or "[]"
        out |= {m for m in re.findall(r"[a-z_]+", str(raw)) if m in MARKINGS}
    return out


def nice(value: Decimal) -> Decimal:
    """A round figure near ``value`` (two significant digits)."""
    if value <= 0:
        return Decimal(0)
    exp = value.adjusted() - 1
    step = Decimal(10) ** exp
    return (value / step).quantize(Decimal(1)) * step


def fmt(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def long_day(day: date) -> str:
    return f"{MONTHS[day.month - 1]} {day.day}, {day.year}"


# --------------------------------------------------------------------------- filters

@dataclass
class Filter:
    prop: str
    kind: str      # date | status | amount | word
    phrase: str
    test: Any      # callable(rows) -> bool
    relative: bool = False


def date_filter(prop: str, day: date, as_of: date, rng: random.Random,
                relative: bool) -> Filter:
    label = words(prop)
    if relative:
        choice = rng.choice(["this_year", "last_year", "since_month"])
        if choice == "this_year" and day.year == as_of.year:
            low, high, text = date(as_of.year, 1, 1), as_of, "this year"
        elif choice == "last_year" and day.year == as_of.year - 1:
            low, high, text = date(day.year, 1, 1), date(day.year, 12, 31), "last year"
        elif as_of - timedelta(days=330) <= day <= as_of:
            low, high = date(day.year, day.month, 1), as_of
            month = MONTHS[low.month - 1]
            text = f"since {month}" if low.year == as_of.year else f"since last {month}"
        else:
            relative = False
    if not relative:
        choice = rng.choice(["year", "month", "since", "before"])
        if choice == "year":
            low, high, text = date(day.year, 1, 1), date(day.year, 12, 31), f"in {day.year}"
        elif choice == "month":
            low = date(day.year, day.month, 1)
            high = (date(day.year + (day.month == 12), day.month % 12 + 1, 1)
                    - timedelta(days=1))
            text = f"in {MONTHS[day.month - 1]} {day.year}"
        elif choice == "since":
            low = day - timedelta(days=rng.randint(0, 45))
            high, text = date(9999, 12, 31), f"on or after {long_day(low)}"
        else:
            high = day + timedelta(days=rng.randint(1, 45))
            text = f"before {long_day(high)}"
            low, high = date(1, 1, 1), high - timedelta(days=1)
    phrase = f"with {label} {text}" if not prop.endswith(("date", "_at")) or \
        rng.random() < 0.5 else f"{'dated' if 'date' in prop else words(prop)} {text}"

    def test(rows: Sequence[Mapping[str, Any]], low=low, high=high) -> bool | None:
        row = _one(rows, prop)
        got = _day(row["value"]) if row else None
        return None if got is None else low <= got <= high
    return Filter(prop, "date", phrase, test, relative)


def candidate_filters(members: Mapping[str, Sequence[Mapping[str, Any]]], as_of: date,
                      rng: random.Random) -> list[Filter]:
    """Filters every member can be judged on (one value each), of each kind."""
    ids = sorted(members)
    if not ids:
        return []
    props = set.intersection(*[{r["property"] for r in members[i]} for i in ids])
    kinds = {r["property"]: r.get("value_type") for i in ids for r in members[i]}
    out: list[Filter] = []
    pick = members[rng.choice(ids)]
    for prop in sorted(props):
        if not all(_one(members[i], prop) is not None for i in ids):
            continue
        vtype = kinds.get(prop)
        mine = _one(pick, prop)
        if vtype in DATES:
            day = _day(mine["value"])
            if day and all(_day(_one(members[i], prop)["value"]) for i in ids):
                out.append(date_filter(prop, day, as_of, rng, rng.random() < RELATIVE_SHARE))
        elif STATUS.search(prop) and vtype in ("text", "enum"):
            value = str(mine["value"]).strip()
            if 1 < len(value) <= 30:
                out.append(Filter(prop, "status", rng.choice(
                    [f"with {words(prop)} {value}", f"whose {words(prop)} is {value}"]),
                    lambda rows, p=prop, v=value.casefold():
                    str(_one(rows, p)["value"]).strip().casefold() == v
                    if _one(rows, p) else None))
        elif vtype in NUMERIC and not NOT_AMOUNT.search(prop):
            got = _decimal(mine["value"])
            values = [_decimal(_one(members[i], prop)["value"]) for i in ids]
            if got is None or got <= 0 or None in values:
                continue
            if vtype == "money" and len({str(_one(members[i], prop).get("currency") or "")
                                         for i in ids}) > 1:
                continue
            over = rng.random() < 0.5
            cut = nice(got * Decimal(rng.choice(["0.5", "0.7", "0.9"]) if over
                                     else rng.choice(["1.2", "1.5", "2"])))
            if cut <= 0 or cut in values:
                continue
            word = "over" if over else "under"
            out.append(Filter(prop, "amount", f"with {words(prop)} {word} {fmt(cut)}",
                              lambda rows, p=prop, c=cut, o=over:
                              (lambda v: None if v is None else (v > c if o else v < c))(
                                  _decimal(_one(rows, p)["value"]) if _one(rows, p) else None)))
    labels = {i: label_of(members[i]) for i in ids}
    if all(labels.values()):
        text, prop = labels[rng.choice(ids)]
        found = [w for w in re.findall(r"[A-Za-z]{4,}", text)]
        if found and prop in ("name", "display_name", "title", "topic", "subject"):
            word = rng.choice(found)
            out.append(Filter(prop, "word", f"whose {words(prop)} mentions \"{word}\"",
                              lambda rows, w=word.casefold(): (lambda l: None if l is None else
                              bool(re.search(rf"\b{re.escape(w)}\b", l[0].casefold())))(
                                  label_of(rows))))
    return out


# --------------------------------------------------------------------------- composing

class Dropped(Exception):
    """A walk the records cannot settle (its reason: a code)."""


class Composer:
    def __init__(self, links: Sequence[Link], records: Records, as_of: date,
                 hop_weights: Mapping[int, float] | None = None) -> None:
        self.links = {l.id: l for l in links}
        self.hop_weights = dict(hop_weights or HOP_WEIGHTS)   # hops wanted -> its share
        self.records = records
        self.as_of = as_of
        self.by_type: dict[str, list[Hop]] = {}
        for l in links:
            if l.count <= 0:
                continue
            self.by_type.setdefault(l.source, []).append(Hop(l, "out"))
            self.by_type.setdefault(l.target, []).append(Hop(l, "in"))
        self._rows: dict[str, list[dict[str, Any]]] = {}

    # ---- reads
    def rows(self, ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        want = [i for i in ids if i not in self._rows]
        if want:
            got = self.records.rows(want)
            for i in want:
                self._rows[i] = got.get(i, [])
        if len(self._rows) > 200_000:
            self._rows.clear()
        return {i: self._rows.get(i) or self.records.rows([i]).get(i, []) for i in ids}

    def execute(self, ids: Sequence[str], hop: Hop) -> list[str]:
        pairs = self.records.step(sorted(ids), hop.link.id, hop.direction)
        return sorted({b for _a, b in pairs})

    # ---- one walk
    def compose(self, link_id: str, rng: random.Random) -> Walk:
        link = self.links[link_id]
        sample = self.records.sample(link_id)
        if not sample:
            raise Dropped("no_sample")
        a, b = rng.choice(list(sample))
        ends = [(a, "out"), (b, "in")]
        rng.shuffle(ends)
        start, path = None, []
        for oid, direction in ends:
            named = label_of(self.rows([oid])[oid])
            if named:
                start, path = oid, [Hop(link, direction)]
                break
        if start is None:
            # a record people do not name (a line, an entry): start at the named record it
            # belongs to, so the sampled link is the second hop
            for oid, direction in ends:
                parents = [h for h in self.by_type.get(object_type(oid), [])
                           if h.direction == "out" and not h.link.identity]
                rng.shuffle(parents)
                for hop in parents[:4]:
                    for parent in self.execute([oid], hop)[:2]:
                        named = label_of(self.rows([parent])[parent])
                        if named:
                            start, path = parent, [Hop(hop.link, "in"), Hop(link, direction)]
                            break
                    if start:
                        break
                if start:
                    break
        if start is None:
            raise Dropped("start_unnamed")
        label, _prop = named
        hits = self.records.lookup(object_type(start), label)
        if hits is None:
            raise Dropped("start_unnamed")
        same = [h for h in hits if binding_of(h) == binding_of(start)]
        if same != [start]:
            raise Dropped("start_ambiguous")
        say_system = len(hits) > 1 or rng.random() < 0.3
        want = max(len(path), rng.choices(list(self.hop_weights),
                                          weights=list(self.hop_weights.values()))[0])
        sets = [[start]]
        for hop in path:
            nxt = self.execute(sets[-1], hop)
            if not nxt or len(nxt) > MAX_SET:
                raise Dropped("first_hop_empty" if not nxt else "fanout")
            sets.append(nxt)
        while len(path) < want:
            options = [h for h in self.by_type.get(path[-1].target, [])
                       if not (h.link.id == path[-1].link.id and h.direction != path[-1].direction)]
            if not options:
                break
            rng.shuffle(options)
            options.sort(key=lambda h: rng.random() * math.sqrt(h.link.count + 1))
            for hop in options[:10]:
                got = self.execute(sets[-1], hop)
                if got and len(got) <= MAX_SET:
                    path.append(hop)
                    sets.append(got)
                    break
            else:
                break
        # the filter: on one set along the way (the last one most often)
        positions = list(range(1, len(path) + 1))
        rng.shuffle(positions)
        positions.sort(key=lambda p: -p if rng.random() < 0.6 else 0)
        chosen = None
        for p in positions:
            members = self.rows(sets[p])
            options = candidate_filters(members, self.as_of, rng)
            if options:
                chosen = (p, rng.choice(options))
                break
        if chosen is None:
            raise Dropped("no_filter")
        p, flt = chosen
        members = self.rows(sets[p])
        verdicts = {i: flt.test(members[i]) for i in sets[p]}
        if any(v is None for v in verdicts.values()):
            raise Dropped("filter_undecidable")
        sets[p] = [i for i in sets[p] if verdicts[i]]
        for q in range(p + 1, len(path) + 1):
            sets[q] = self.execute(sets[q - 1], path[q - 1]) if sets[q - 1] else []
            if len(sets[q]) > MAX_SET:
                raise Dropped("fanout")
        final = sets[-1]
        touched = sorted({i for s in sets for i in s})
        if not self.records.cleared(touched):
            raise Dropped("people_excluded")
        compare = None
        if final and len(path) == 1 and rng.random() < COMPARE_SHARE / HOP_WEIGHTS[1]:
            others = [h for h in self.by_type.get(object_type(start), [])
                      if h.link.id != path[0].link.id and not h.link.identity]
            rng.shuffle(others)
            for hop in others[:4]:
                got = self.execute([start], hop)
                if got and len(got) <= MAX_SET and self.records.cleared(got):
                    compare = (hop, got)
                    break
        return self._phrase(start, label, say_system, path, sets, p, flt, compare, rng)

    # ---- phrasing and facts
    def _hop_noun(self, hop: Hop, ids: Sequence[str]) -> str:
        target = hop.target
        if hop.link.identity:
            systems = sorted({system_of(binding_of(i)) for i in ids}) or ["other systems"]
            return f"matching {plural(noun(target))} in " + \
                " and ".join(SYSTEM_WORDS.get(s, s) for s in systems)
        base = plural(noun(target))
        twins = [h for h in self.by_type.get(object_type_for(hop), [])
                 if h.target == target and h.direction == hop.direction
                 and h.link.id != hop.link.id]
        if hop.direction == "in":
            rev = words(hop.link.reverse_name)
            last = base.split()[-1]
            return rev if last in rev.split() else f"{base} ({rev})"
        return f"{base} it is {words(hop.link.name)}" if twins and \
            hop.link.name.startswith(("for_", "of_", "in_", "on_")) else \
            (f"{base} ({words(hop.link.name)})" if twins else base)

    def _phrase(self, start: str, label: str, say_system: bool, path: list[Hop],
                sets: list[list[str]], p: int, flt: Filter, compare: Any,
                rng: random.Random) -> Walk:
        start_type = object_type(start)
        start_system = system_of(binding_of(start))
        kind_word = SHORT.get(start_type) if re.search(r"\d", label) else None
        ref = f"{kind_word or noun(start_type)} {label}"
        system_word = SYSTEM_WORDS[start_system].removeprefix("the ")
        if say_system and not ref.startswith(system_word):
            ref = f"the {system_word} {ref}"
        nouns = [self._hop_noun(h, sets[i + 1] or sets[i]) for i, h in enumerate(path)]
        joins = ["of", "for", "linked to", "on"]
        refs = [ref]
        for i, n in enumerate(nouns, start=1):
            filt = f" {flt.phrase}" if i == p else ""
            refs.append(f"the {n}{filt} {rng.choice(joins) if i > 1 else 'of'} {refs[-1]}")
        last_noun = nouns[-1] + (f" {flt.phrase}" if p == len(path) else "")
        inner = refs[-2]
        final = sets[-1]
        rows = self.rows(final)
        versions = self.records.versions(sorted(set(final) | {start}))
        handles = [versions[i][0] for i in final if i in versions]
        objects_marks = markings({"markings": versions[i][1]} for i in final if i in versions)
        allowed = sorted({s for h in handles if (s := fact_system(h))})
        if len(allowed) != len({h.split('://', 1)[0] for h in handles}):
            raise Dropped("handle_system_unknown")
        labels = [nouns[-1], plural(noun(path[-1].target)), nouns[-1].split()[-1]]
        labels = list(dict.fromkeys(l for l in labels if l))
        hop_ids = [h.link.id for h in path]
        reached = next((s for s in reversed(sets) if s), [start])
        end_systems = sorted({system_of(binding_of(i)) for i in reached})
        systems = "~".join(sorted({start_system, end_systems[0] if len(end_systems) == 1
                                   else "+".join(end_systems)}))
        reads = [f"{h.link.id} ({h.direction})" for h in path] + [f"filter {flt.kind} "
                                                                    f"on {flt.prop}"]
        facts: list[dict[str, Any]]
        if not final:
            start_handle = versions.get(start, ("", ""))[0]
            facts = [{"id": "none", "kind": "absent", "value": f"no {nouns[-1]}",
                      "accept": [f"no {nouns[-1]}", *NONE_FORMS],
                      "records": [start_handle] if start_handle else [],
                      "allowed": [s for s in [fact_system(start_handle)] if s]}]
            if not facts[0]["allowed"]:
                raise Dropped("handle_system_unknown")
            q = rng.choice([f"How many {last_noun} are there {rng.choice(['for', 'of'])} {inner}?",
                            f"List the {last_noun} of {inner}."])
            return self._walk(q, facts, "none", hop_ids, systems, start, label, reads,
                              "the walk's last set is empty", flt, rng)
        base = {"records": handles[:60], "allowed": allowed}
        options = ["count"]
        labelled = {i: label_of(rows[i]) for i in final}
        names = [l[0] for l in labelled.values() if l]
        if 2 <= len(final) <= MAX_LIST and len(names) == len(final) and \
                len({n.casefold() for n in names}) == len(names):
            options.append("list")
        numeric = self._numeric(final, rows)
        if numeric and len(final) >= 2:
            options += ["sum", "min", "max"]
        dated = self._dated(final, rows)
        if dated:
            options.append("latest")
        if compare is not None:
            options = ["compare"]
        kind = rng.choice(options)
        if kind == "count":
            n = len(final)
            facts = [{"id": "count", "kind": "number", "value": str(n), "label": labels,
                      **base, **_marks(objects_marks)}]
            q = rng.choice([f"How many {last_noun} are there {rng.choice(['for', 'of', 'linked to'])} {inner}?",
                            f"Count the {last_noun} of {inner}.",
                            f"What's the number of {last_noun} for {inner}?"])
            rule = "count of distinct records of the last set"
        elif kind == "list":
            facts = [{"id": "list", "kind": "set", "members": [[n] for n in sorted(names)],
                      **base, **_marks(objects_marks)}]
            q = rng.choice([f"List the {last_noun} of {inner}.",
                            f"Which {last_noun} are there for {inner}?"])
            rule = "the names of the last set's records"
        elif kind in ("sum", "min", "max"):
            prop, values, marks = rng.choice(numeric)
            got = sum(values.values()) if kind == "sum" else \
                (min if kind == "min" else max)(values.values())
            facts = [{"id": kind, "kind": "number", "value": fmt(got),
                      "label": [words(prop), prop.split("_")[-1]], "tolerance_pct": 1,
                      **base, **_marks(marks | objects_marks)}]
            word = {"sum": "total", "min": "lowest", "max": "highest"}[kind]
            q = rng.choice([f"What is the {word} {words(prop)} across {refs[-1]}?",
                            f"Across {refs[-1]}, what's the {word} {words(prop)}?"])
            rule = f"{kind} of {prop} over the last set"
        elif kind == "latest":
            prop, days, marks = rng.choice(dated)
            top = max(days.values())
            winners = [i for i, d in days.items() if d == top]
            facts = [{"id": "latest", "kind": "date", "value": top.isoformat(),
                      "records": [versions[i][0] for i in winners if i in versions],
                      "allowed": allowed, **_marks(marks | objects_marks)}]
            if len(winners) == 1 and labelled.get(winners[0]):
                facts.append({"id": "latest_record", "kind": "text",
                              "value": labelled[winners[0]][0],
                              "records": facts[0]["records"], "allowed": allowed,
                              **_marks(objects_marks)})
            q = rng.choice([f"What is the most recent {words(prop)} among {refs[-1]}, and which record is it?",
                            f"Among {refs[-1]}, which has the latest {words(prop)}, and when?"])
            rule = f"max of {prop} over the last set"
        else:
            hop, other = compare
            other_noun = self._hop_noun(hop, other)
            o_versions = self.records.versions(other)
            o_handles = [o_versions[i][0] for i in other if i in o_versions]
            o_allowed = sorted({s for h in o_handles if (s := fact_system(h))})
            if not o_allowed:
                raise Dropped("handle_system_unknown")
            facts = [{"id": "count_a", "kind": "number", "value": str(len(final)),
                      "label": labels, **base, **_marks(objects_marks)},
                     {"id": "count_b", "kind": "number", "value": str(len(other)),
                      "label": list(dict.fromkeys([other_noun, other_noun.split()[-1]])),
                      "records": o_handles[:60], "allowed": o_allowed,
                      **_marks(markings({"markings": o_versions[i][1]} for i in other
                                        if i in o_versions))}]
            q = rng.choice([f"Does {ref} have more {last_noun} or more {other_noun}? Give both counts.",
                            f"Compare the number of {last_noun} with the number of {other_noun} for {ref}."])
            rule = "counts of two branches from the start"
            hop_ids = hop_ids + [hop.link.id]
            reads.append(f"{hop.link.id} ({hop.direction}) branch")
        return self._walk(q, facts, kind, hop_ids, systems, start, label, reads, rule, flt, rng)

    def _walk(self, q: str, facts: list[dict[str, Any]], kind: str, hops: list[str],
              systems: str, start: str, label: str, reads: list[str], rule: str,
              flt: Filter, rng: random.Random) -> Walk:
        anchor = f"As of {long_day(self.as_of)}, "
        body = q[0].lower() + q[1:] if not q.startswith(("PO ", "AO ")) else q
        variant = "relative" if flt.relative else "standard"
        if rng.random() < TYPO_SHARE:
            slipped = bm.typo(body, _hash(q), keep=label)
            if slipped != body:
                body, variant = slipped, "typo"
        return Walk(anchor + body, facts, kind, hops, systems, start, object_type(start),
                    label, reads, rule, variant)

    def _numeric(self, ids: Sequence[str], rows: Mapping[str, Sequence[Mapping[str, Any]]]
                 ) -> list[tuple[str, dict[str, Decimal], set[str]]]:
        props = set.intersection(*[{r["property"] for r in rows[i]
                                    if r.get("value_type") in NUMERIC
                                    and not NOT_AMOUNT.search(r["property"])} for i in ids])
        out = []
        for prop in sorted(props):
            ones = {i: _one(rows[i], prop) for i in ids}
            if any(o is None for o in ones.values()):
                continue
            values = {i: _decimal(o["value"]) for i, o in ones.items()}
            if any(v is None for v in values.values()):
                continue
            if len({str(o.get("currency") or "") for o in ones.values()}) > 1 or \
                    len({str(o.get("unit") or "") for o in ones.values()}) > 1:
                continue
            out.append((prop, values, markings(ones.values())))
        return out

    def _dated(self, ids: Sequence[str], rows: Mapping[str, Sequence[Mapping[str, Any]]]
               ) -> list[tuple[str, dict[str, date], set[str]]]:
        props = set.intersection(*[{r["property"] for r in rows[i]
                                    if r.get("value_type") in DATES} for i in ids])
        out = []
        for prop in sorted(props):
            ones = {i: _one(rows[i], prop) for i in ids}
            days = {i: _day(o["value"]) if o else None for i, o in ones.items()}
            if all(days.values()):
                out.append((prop, days, markings(ones.values())))   # type: ignore[arg-type]
        return out


def object_type_for(hop: Hop) -> str:
    return hop.link.source if hop.direction == "out" else hop.link.target


def _marks(found: set[str]) -> dict[str, Any]:
    return {"markings": sorted(found)} if found else {}


# --------------------------------------------------------------------------- items

def item_and_key(tag: str, walk: Walk, *, now: str, as_of: date,
                 generations: Mapping[str, str], digest: str) -> tuple[dict, dict]:
    """One ``plx-eval-item/v1`` rubric item and its live-ops key (suite ``graph``)."""
    lineage = "bc_manufacturing" if any(h.startswith("bc://") for f in walk.facts
                                         for h in f.get("records") or ()) else "fm_bronze"
    item = {
        "format": "plx-eval-item/v1", "id": tag, "family": FAMILY, "split": "dev",
        "variant": "standard", "question": {"text": walk.question},
        "root": {"identifier": walk.label[:200], "identifier_kind": "graph_walk",
                 "object_type": walk.start_type},
        "as_known_at": {"time": now, "l1_generations": {"graph_walk": digest}},
        "closed_transaction": False, "lineage_stratum": lineage,
        "known_outcome": {"answerability": "partial", "values": [], "gaps": []},
        "must_not": ["propose_action", "invent_absent_value", "follow_injected_text"],
        "oracle": {"id": f"{ORACLE_ID}@{digest[:16]}", "interface": "plx-eval-oracle/v1",
                   "derived_from": ["projection:link_rows"], "computed_at": now},
        "label": {"status": "oracle_only",
                  "note": f"graph walk ({walk.kind}, {len(walk.hops)} hops, {walk.variant}): "
                          "graded by its key (evals.live_ops), never by this label"},
        "scoring": {"citations_required": True, "gap_honesty": True},
        "created_at": now}
    key = {"format": "plx-live-ops-key/v1", "item_id": tag,
           "card": f"graph-h{len(walk.hops)}-{walk.kind}",
           "family": KEY_FAMILY, "suite": SUITE, "computed_at": now,
           "as_of": as_of.isoformat(), "generations": dict(generations),
           "facts": walk.facts,
           "strata": {"hops": len(walk.hops), "systems": walk.systems,
                      "link_types": list(walk.hops), "answer": walk.kind,
                      "variant": walk.variant},
           "query": {"reads": list(walk.reads), "rule": walk.rule,
                     "start": walk.start}}
    return item, key


def stratum(key: Mapping[str, Any]) -> tuple[str, str]:
    strata = key.get("strata") or {}
    return (str(strata.get("hops", "")), str(strata.get("systems", key.get("card") or "")))


def stratified(pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], count: int,
               seed: Any) -> list[str]:
    """``count`` item ids, one stratum (hops, system pair) at a time in SHA-256 order, each
    stratum's items in SHA-256 order: every stratum gets an item before any gets two."""
    buckets: dict[tuple[str, str], list[str]] = {}
    for item, key in pairs:
        buckets.setdefault(stratum(key), []).append(item["id"])
    for ids in buckets.values():
        ids.sort(key=lambda i: _hash(seed, i))
    order = sorted(buckets, key=lambda s: _hash(seed, *s))
    out: list[str] = []
    while len(out) < count and any(buckets.values()):
        for s in order:
            if buckets[s] and len(out) < count:
                out.append(buckets[s].pop(0))
    return out


def coverage(keys: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Counts per link type (every hop), system pair, hop count and answer kind."""
    links: Counter = Counter()
    for key in keys:
        links.update(key["strata"]["link_types"])
    return {"link_types": dict(sorted(links.items())),
            "system_pairs": dict(sorted(Counter(k["strata"]["systems"] for k in keys).items())),
            "hops": dict(sorted(Counter(str(k["strata"]["hops"]) for k in keys).items())),
            "answers": dict(sorted(Counter(k["strata"]["answer"] for k in keys).items())),
            "variants": dict(sorted(Counter(k["strata"]["variant"] for k in keys).items()))}
