"""The generative evaluation: thousands of questions drawn from the ontology itself, each
with a ground truth computed straight from the records, never from the product's tools.

The curated sets (the use-case cards, the graph walks of ``evals.graph_walks``) cover the
questions someone designed. This family asks what any employee might ask about anything
the ontology holds, in the words they would use, so the score says how Ask does on
questions nobody wrote for it.

**Categories** (``CATEGORIES``, shares in ``QUOTA``): a record's own value (``lookup``),
link walks of one hop, of three to five hops and across systems (``evals.graph_walks``
executed with this family's hop weights), whole-table aggregations, time comparisons ("as
of", "since last month", month against month), negations ("which POs have no receipt"),
rankings, anomalies (one value far above its siblings), why-questions (the status and
the linked records that explain it), what-if impact (what depends on a record), document
lookups (mail-extracted documents and SharePoint files), people ("who reported this
task": addresses only, every one cleared by the people exclusion) and money (sums and
balances of finance-marked values, in ``Decimal``).

**Ground truth**: every answer is executed over every current value and every current
firm link of the records concerned (``Source``: the caller's read-only store), with the
records' citation handles. A question the records cannot settle (an ambiguous name, a
tie at a ranking's cut, mixed currencies, a filter some record cannot be judged on, a
person the exclusion cannot decide) is dropped with its reason, never guessed.

**Phrasing** (``STYLES``): plain, terse, rambling, misspelled, internal jargon and the
owner's own shorthand ("whats the $$ on po 100109 rn"). The named record is never
altered by a style (``styled``), so the question stays answerable.

**Permissions**: a fact read from a marked value carries its markings; a document whose
name or path reads as payroll or HR (``operating.chunking.sensitive``) is marked
``finance`` and ``pii`` and flagged ``sensitive``. A reader without every marking an item
needs is graded on refusing it (``grade``): stating a withheld value is wrong.

**Grading** (``grade``): numbers, ids and dates by exact match (money to the cent), lists
by set overlap; the outcome is ``correct``, ``incomplete`` (some facts or members, none
contradicted), ``wrong`` (a contradicted fact, a leaked withheld value, or an answer that
states none of the facts) or ``no_answer``. Pure: reads no source, clock or environment.
"""

from __future__ import annotations

from collections import Counter
import dataclasses
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import random
import re
import statistics
from typing import Any, Iterable, Mapping, Protocol, Sequence

from evals import blind_mined as bm
from evals import graph_walks as gw

FORMAT = "plx-eval-gen/v1"
VERDICT_FORMAT = "plx-eval-gen-verdict/v1"
CATEGORIES = ("lookup", "single_hop", "multi_hop", "cross_system", "aggregation",
              "time_comparison", "negation", "ranking", "anomaly", "why", "what_if",
              "document", "people", "money")
QUOTA = {"lookup": 0.10, "single_hop": 0.075, "multi_hop": 0.10, "cross_system": 0.075,
         "aggregation": 0.075, "time_comparison": 0.07, "negation": 0.07, "ranking": 0.06,
         "anomaly": 0.045, "why": 0.06, "what_if": 0.06, "document": 0.075, "people": 0.045,
         "money": 0.085}
STYLES = {"plain": 0.30, "terse": 0.15, "rambling": 0.13, "misspelled": 0.12, "jargon": 0.15,
          "vince": 0.15}
OUTCOMES = ("correct", "incomplete", "wrong", "no_answer")
HOPS = {"single_hop": {1: 1.0}, "multi_hop": {3: 0.4, 4: 0.35, 5: 0.25},
        "cross_system": {1: 0.3, 2: 0.4, 3: 0.3}}
MAX_LIST = 8
MAX_TABLE = 90_000
MIN_SIBLINGS = 6
OUTLIER = Decimal(3)
# The identifier properties among ``gw.NAME_PROPS``: another way to name the same record in
# a set fact's member forms (``forms``); never a free-text name or title.
ID_PROPS = frozenset(("po_number", "order_number", "invoice_number", "ao_code", "batch_code",
                      "receipt_number", "shipment_number", "quote_number", "pr_number",
                      "lot_number", "release_number", "bc_number", "code", "number"))
DOCUMENT_TYPES = ("CertificateOfAnalysis", "PackingSlip", "SupplierInvoice",
                  "SupplierConfirmation", "CustomerPoDocument", "DeliveryReceipt",
                  "FreightBill", "ForwarderQuote", "DecisionRecord", "SharePointFile",
                  "AssemblyOrderPacket", "Lot")
# Who-questions: the property that names the person, phrased as the act.
PEOPLE_PROPS = {("MCTask", "reporter"): "reported", ("MCTask", "accountable_owner"):
                "is accountable for", ("MCApproval", "decided_by"): "decided",
                ("MCApproval", "requested_by"): "requested", ("MCProject", "owner"): "owns"}
MARKINGS = frozenset({"finance", "purchasing_prices", "quality_controlled", "pii"})
PER_UNIT = re.compile(r"unit|price|rate|per_|percent|limit")
EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
NONE_FORMS = ["none", "no ", "not any", "zero", "nothing", "there are no", "there is no",
              "0 "]


class Dropped(Exception):
    """A question the records cannot settle (its reason, a code)."""


class Source(gw.Records, Protocol):
    """``evals.graph_walks.Records`` plus whole-type reads."""
    def count(self, object_type: str, binding: str = "") -> int: ...
    def objects(self, object_type: str, k: int, rng: random.Random, binding: str = ""
                ) -> list[str]: ...
    def all_of(self, object_type: str, binding: str) -> list[str]: ...
    def address_ok(self, address: str) -> bool: ...
    def lookup_prop(self, object_type: str, prop: str, value: str) -> list[str] | None: ...
    def sensitive(self, texts: Sequence[str], title: str | None) -> bool: ...


# A type larger than this is sampled among its first records only (``draw_offsets``).
SAMPLE_SPAN = 600_000


def draw_offsets(n: int, k: int, rng: random.Random) -> list[int]:
    """The ``k`` offsets a store's ``Source.objects`` draws among a type's ``n`` records
    (its first ``SAMPLE_SPAN`` when larger): SQLite and Postgres stores pick alike."""
    if n <= 0:
        return []
    span = n if n <= SAMPLE_SPAN else SAMPLE_SPAN
    return [rng.randrange(span) for _ in range(k)]


class Pinned:
    """A source whose record picks (``objects``) are written down while a question is
    composed (``begin()``), or replayed from those notes (``begin(pins)``) while every
    value, link, label and permission read comes from the source as it is now.

    So a question composed once (over the store a set was built from) is composed again
    over another store state with the same records picked: where its records did not
    change it is the same question with the same truth, and where they did, its truth is
    the records' current one (or the question no longer arises: the caller compares the
    texts). A replayed pick asked with other arguments than the note, or past the notes,
    is ``Dropped("selection_changed")``; the draws the source's pick made are passed
    over in the turn's ``PinnedRandom``."""

    def __init__(self, source: Any) -> None:
        self.source = source
        self._pins: list[list[Any]] | None = None
        self._log: list[list[Any]] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)

    def begin(self, pins: Sequence[Sequence[Any]] | None = None) -> None:
        self._pins = None if pins is None else [list(p) for p in pins]
        self._log = []

    def end(self) -> list[list[Any]]:
        log, self._log, self._pins = self._log or [], None, None
        return log

    def objects(self, object_type: str, k: int, rng: random.Random, binding: str = ""
                ) -> list[str]:
        drawn = getattr(rng, "log", None)
        if self._pins is None:
            before = len(drawn) if drawn is not None else 0
            got = list(self.source.objects(object_type, k, rng, binding))
            if self._log is not None:
                self._log.append([object_type, binding, k,
                                  len(drawn) - before if drawn is not None else 0, got])
            return got
        if not self._pins:
            raise Dropped("selection_changed")
        noted_type, noted_binding, noted_k, draws, got = self._pins.pop(0)
        if (noted_type, noted_binding, noted_k) != (object_type, binding, k):
            raise Dropped("selection_changed")
        if isinstance(rng, PinnedRandom):
            rng.skip(draws)   # the source's own draws for this pick, as noted
        return list(got)


def pin_key(value: Any) -> Any:
    """A JSON form of a value ``rng`` chose (a link, a hop, a record id, a pair ...)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [pin_key(v) for v in value]
    if hasattr(value, "link") and hasattr(value, "direction"):
        return ["hop", value.link.id, value.direction]
    if hasattr(value, "id") and hasattr(value, "source") and hasattr(value, "target"):
        return ["link", value.id]
    if callable(value):
        return ["fn", getattr(value, "__qualname__", type(value).__name__)]
    if dataclasses.is_dataclass(value):
        return [type(value).__name__, {f.name: pin_key(getattr(value, f.name))
                                       for f in dataclasses.fields(value)}]
    if isinstance(value, dict):
        return {str(k): pin_key(v) for k, v in value.items()}
    return ["repr", str(value)]


class PinnedRandom(random.Random):
    """A turn's ``random.Random`` whose outcomes are written down (``noted`` None) or
    replayed by value: a replayed ``choice`` takes the same element (a link, a status
    value, a year) wherever it now sits among the choices, so a question asked of
    records that changed keeps its terms (a term noted with its value then, such as a
    property and its value, is the same term while it alone is there); a choice no
    longer among them, or an outcome asked in another order or kind, is
    ``Dropped("choice_gone")``. Recording draws
    exactly what a plain ``random.Random`` of the seed draws."""

    def __init__(self, seed: Any, noted: Sequence[Any] | None = None) -> None:
        super().__init__(seed)
        self.replaying = noted is not None
        self.noted = list(noted or [])
        self.log: list[Any] = []

    def getrandbits(self, k: int) -> int:
        # defined so ``random.Random`` keeps drawing below n by bits (a subclass that
        # overrides only ``random`` draws by floats: other outcomes than the seed's)
        return super().getrandbits(k)

    def skip(self, n: int) -> None:
        """Pass over ``n`` noted outcomes (a pinned pick's own draws)."""
        if len(self.noted) < n:
            raise Dropped("choice_gone")
        del self.noted[:n]

    def _next(self, kind: str) -> Any:
        if not self.noted or self.noted[0][0] != kind:
            raise Dropped("choice_gone")
        return self.noted.pop(0)[1]

    def random(self) -> float:
        if self.replaying:
            return float(self._next("f"))
        got = super().random()
        self.log.append(["f", got])
        return got

    def randrange(self, start: int, stop: Any = None, step: int = 1) -> int:
        if self.replaying:
            got = int(self._next("r"))
            low, high = (0, start) if stop is None else (start, stop)
            if not low <= got < high or (got - low) % step:
                raise Dropped("choice_gone")
            return got
        got = super().randrange(start, stop, step)
        self.log.append(["r", got])
        return got

    def choice(self, seq: Sequence[Any]) -> Any:
        if self.replaying:
            want = self._next("c")
            keys = [pin_key(item) for item in seq]
            for item, key in zip(seq, keys):
                if key == want:
                    return item
            # a term with what it held then (a property and its value): the same term
            # if it alone is still there (its value is what the question asks)
            if isinstance(want, list) and len(want) > 1 and isinstance(want[0], str) and \
                    want[0] not in ("hop", "link", "fn", "repr"):
                same = [item for item, key in zip(seq, keys)
                        if isinstance(key, list) and key[:1] == want[:1]]
                if len(same) == 1:
                    return same[0]
            raise Dropped("choice_gone")
        got = super().choice(seq)
        self.log.append(["c", pin_key(got)])
        return got

    def shuffle(self, x: list[Any]) -> None:   # type: ignore[override]
        if self.replaying:
            order = {json.dumps(k): n for n, k in enumerate(self._next("s"))}
            x[:] = sorted(x, key=lambda v: order.get(json.dumps(pin_key(v)), len(order)))
            return
        super().shuffle(x)
        self.log.append(["s", [pin_key(v) for v in x]])


@dataclass
class Question:
    category: str
    text: str
    facts: list[dict[str, Any]]
    start: str
    label: str
    hops: list[str] = field(default_factory=list)
    systems: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    rule: str = ""
    sensitive: bool = False
    styled: bool = True
    origin: tuple = ()   # (the composing category, its turn): how ``build`` made it


def _hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _plural(noun: str, n: int = 2) -> str:
    return noun if n == 1 else gw.plural(noun)


# --------------------------------------------------------------------------- generator

class Generator:
    """Composes questions of every category over ``source`` (read-only), as of ``as_of``."""

    def __init__(self, links: Sequence[gw.Link], source: Source, as_of: date,
                 types: Mapping[str, Mapping[str, int]]) -> None:
        """``types``: object type -> binding -> record count (non-empty ones only)."""
        self.links = [l for l in links if l.count > 0]
        self.source = source
        self.as_of = as_of
        self.types = {t: dict(sorted(b.items())) for t, b in sorted(types.items()) if b}
        self.composers = {c: gw.Composer(links, source, as_of, hop_weights=w)
                          for c, w in HOPS.items()}
        self.walker = self.composers["single_hop"]
        self.by_type = self.walker.by_type
        self._tables: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
        self._handles: dict[str, str] = {}
        self._probed: dict[tuple[str, str, str], bool] = {}

    def composed(self, category: str, seed: Any, turn: int,
                 pins: Mapping[str, Any] | None = None
                 ) -> tuple["Question | Dropped", dict[str, Any] | None]:
        """(the question ``build`` composes for this category's turn, or why it is
        dropped; when the source is ``Pinned``, how it was composed: the records it
        picked and its draws (``PinnedRandom``), replaying ``pins`` when given). A pure
        function of the store, ``seed``, the category and the turn: a pinned source
        composes with no pick remembered from another turn."""
        pinned = self.source if isinstance(self.source, Pinned) else None
        seeded = _hash(seed, category, turn)
        if pinned is None:
            rng: random.Random = random.Random(seeded)
        else:
            pinned.begin(None if pins is None else pins["picks"])
            self._probed.clear()
            rng = PinnedRandom(seeded, None if pins is None else pins["draws"])
        try:
            got: Question | Dropped = self.compose(category, rng, turn)
        except Dropped as why:
            got = why
        finally:
            log = None if pinned is None else {
                "picks": pinned.end(), "draws": getattr(rng, "log", [])}
        if log is not None and pins is None and isinstance(got, Question):
            # the record the question is about, alone: a replay asks of it or of none
            # (never of a record passed over here that has since become askable)
            for pick in log["picks"]:
                if got.start in pick[4]:
                    pick[4] = [got.start]
        return got, log

    # ---- reads
    def rows(self, ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        return self.walker.rows(ids)

    def table(self, object_type: str, binding: str) -> dict[str, list[dict[str, Any]]]:
        key = (object_type, binding)
        if key not in self._tables:
            if self.types.get(object_type, {}).get(binding, 0) > MAX_TABLE:
                raise Dropped("table_too_large")
            ids = self.source.all_of(object_type, binding)
            rows = self.source.rows(ids)
            self._tables[key] = {i: rows.get(i, []) for i in ids}
            if len(self._tables) > 24:   # bounded memory: forget the oldest table
                self._tables.pop(next(iter(self._tables)))
        return self._tables[key]

    def handles(self, ids: Sequence[str]) -> list[str]:
        want = [i for i in ids if i not in self._handles]
        if want:
            for oid, (handle, _m) in self.source.versions(want).items():
                self._handles[oid] = handle
        return [self._handles[i] for i in ids if self._handles.get(i)]

    def label(self, oid: str, rows: Sequence[Mapping[str, Any]] | None = None) -> str | None:
        """How people name the record, when that name picks out this record alone in its
        system (None otherwise)."""
        rows = self.rows([oid])[oid] if rows is None else rows
        named = gw.label_of(rows)
        if not named:
            return None
        hits = self.source.lookup(gw.object_type(oid), named[0])
        return self._unique(oid, named[0], hits)

    def forms(self, oid: str, name: str) -> list[str]:
        """``name`` and the record's other identifiers (``ID_PROPS``): every way a reader
        may name it. A sales invoice the set names by its order number (``order_number``
        comes first in ``NAME_PROPS``) is as rightly named by its invoice number."""
        out, rows = [name], self.rows([oid]).get(oid) or []
        for row in rows:
            value = re.sub(r"\s+", " ", str(row.get("value") or "")).strip()
            if row.get("property") in ID_PROPS and 3 <= len(value) <= 40 and \
                    value not in out and gw._one(rows, row["property"]):
                out.append(value)
        return out

    def key_label(self, oid: str, rows: Sequence[Mapping[str, Any]]
                  ) -> tuple[str, str] | None:
        """(a value of an identifier property that picks out the record alone, that
        property): how a record with no name (a mirror row, a line) is asked about."""
        for row in sorted(rows, key=lambda r: r["property"]):
            value = str(row.get("value") or "").strip()
            if row.get("value_type") != "identifier" or not 3 <= len(value) <= 40 or \
                    gw._one(rows, row["property"]) is None or "@" in value:
                continue
            hits = self.source.lookup_prop(gw.object_type(oid), row["property"], value)
            if self._unique(oid, value, hits):
                return value, row["property"]
        return None

    @staticmethod
    def _unique(oid: str, label: str, hits: Sequence[str] | None) -> str | None:
        if hits is None or [h for h in hits if gw.binding_of(h) == gw.binding_of(oid)] != [oid]:
            return None
        return label

    def fact(self, kind: str, ids: Sequence[str], rows: Iterable[Mapping[str, Any]] = (),
             **extra: Any) -> dict[str, Any]:
        handles = self.handles(list(ids)[:40])
        if not handles:
            raise Dropped("no_citation")
        allowed = sorted({s for s in map(gw.fact_system, handles) if s})
        marks = sorted(gw.markings(rows) | set(extra.pop("markings", ())))
        out = {"kind": kind, "records": handles, "allowed": allowed or ["api"], **extra}
        if marks:
            out["markings"] = marks
        return out

    def system(self, oid: str) -> str:
        return gw.system_of(gw.binding_of(oid))

    # ---- a record of a type, named
    def named(self, object_type: str, rng: random.Random, binding: str = "",
              tries: int = 12, keyed: bool = False
              ) -> tuple[str, str, list[dict[str, Any]], str]:
        """(a record, its name, its rows, how it is said); ``keyed``: a record with no name
        is said by an identifier that picks it out alone."""
        for oid in self.source.objects(object_type, tries, rng, binding):
            rows = self.rows([oid])[oid]
            label, said = self.label(oid, rows), None
            if label:
                said = self.say(oid, label)
            elif keyed:
                found = self.key_label(oid, rows)
                if found:
                    label, prop = found
                    said = f"the {gw.noun(object_type)} with {gw.words(prop)} {label}"
            if said and self.source.cleared([oid]):
                return oid, label, rows, said
        raise Dropped("no_named_record")

    def link_start(self, rng: random.Random, want_children: bool = True, min_kids: int = 1,
                   tries: int = 6) -> tuple[str, str, gw.Hop, list[str]]:
        """A named record and the set one of its links reaches (a parent and its
        children, most often), of at least ``min_kids`` records."""
        why = "start_unnamed"
        for _ in range(tries):
            link = rng.choice(self.links)
            sample = self.source.sample(link.id)
            if not sample:
                continue
            a, b = rng.choice(list(sample))
            ends = [(b, gw.Hop(link, "in")), (a, gw.Hop(link, "out"))]
            if not want_children:
                rng.shuffle(ends)
            for oid, hop in ends:
                label = self.label(oid)
                if not label:
                    continue
                kids = self.walker.execute([oid], hop)
                if len(kids) < min_kids:
                    why = "too_few"
                    continue
                if kids and len(kids) <= gw.MAX_SET and self.source.cleared([oid, *kids]):
                    return oid, label, hop, kids
        raise Dropped(why)

    def hop_noun(self, hop: gw.Hop) -> str:
        """What a hop reaches ("purchase order" for a supplier's ``ordered_from``, in)."""
        return gw.noun(hop.target)

    def say(self, oid: str, label: str) -> str:
        return f"{gw.noun(gw.object_type(oid))} {label}"

    # ---- categories
    def compose(self, category: str, rng: random.Random, turn: int) -> Question:
        return getattr(self, "c_" + category)(rng, turn)

    def c_lookup(self, rng: random.Random, turn: int) -> Question:
        names = list(self.types)
        object_type = names[turn % len(names)]   # every type in turn
        binding = rng.choice(list(self.types[object_type]))
        oid, label, rows, said = self.named(object_type, rng, binding, keyed=True)
        options = []
        for row in rows:
            prop, vtype, value = row["property"], row.get("value_type"), row.get("value")
            if prop in gw.NAME_PROPS or value in bm.EMPTY or gw._one(rows, prop) is None or \
                    str(value).strip() == label:
                continue
            if vtype in ("date", "timestamp") and gw._day(value):
                options.append((prop, "date", gw._day(value).isoformat(), row))
            elif vtype in gw.NUMERIC and not gw.NOT_AMOUNT.search(prop) and \
                    gw._decimal(value) is not None:
                options.append((prop, "number", gw.fmt(gw._decimal(value)), row))
            elif vtype in ("text", "enum", "identifier") and 2 <= len(str(value)) <= 40 and \
                    not str(value).startswith(("{", "[")) and not EMAIL.search(str(value)) \
                    and not re.search(r"[\w.+-]+@", str(value)):
                options.append((prop, "set", str(value).strip(), row))
        if not options:
            raise Dropped("no_property")
        prop, kind, value, row = rng.choice(sorted(options, key=lambda o: o[0]))
        sysname = self.system(oid)
        q = f"What is the {gw.words(prop)} of {said} in {sysname}?"
        extra = {"value": value} if kind != "set" else {"members": [[value]]}
        if kind == "number":
            extra["label"] = [gw.words(prop)]
            if row.get("value_type") == "money":
                extra["value"] = str(_cents(Decimal(value)))
                if row.get("currency"):
                    extra["unit"] = row["currency"]
        fact = self.fact(kind, [oid], [row], id=prop, **extra)
        category = "money" if row.get("value_type") == "money" and \
            "finance" in fact.get("markings", ()) else "lookup"
        return Question(category, q, [fact], oid, label, systems=[sysname],
                        rule=f"current value of {prop}")

    def _walk(self, category: str, rng: random.Random) -> Question:
        composer = self.composers[category]
        link = rng.choice(self.links)
        try:
            walk = composer.compose(link.id, rng)
        except gw.Dropped as why:
            raise Dropped(str(why)) from None
        systems = sorted(set(re.findall(r"[A-Za-z]+", walk.systems)))
        hops = len(walk.hops)
        if category == "single_hop" and hops != 1:
            raise Dropped("hops_off")
        if category == "multi_hop" and hops < 3:
            raise Dropped("hops_off")
        if category == "cross_system" and len(systems) < 2:
            raise Dropped("one_system")
        tags = [walk.kind, walk.variant]
        if walk.kind in ("count", "sum", "min", "max"):
            tags.append("aggregation")
        if walk.kind == "none":
            tags.append("negation")
        if any("finance" in f.get("markings", ()) for f in walk.facts):
            tags.append("finance")
        return Question(category, walk.question, walk.facts, walk.start, walk.label,
                        hops=list(walk.hops), systems=systems, tags=tags, rule=walk.rule,
                        styled=walk.variant == "standard")

    def c_single_hop(self, rng, turn):
        return self._walk("single_hop", rng)

    def c_multi_hop(self, rng, turn):
        return self._walk("multi_hop", rng)

    def c_cross_system(self, rng, turn):
        return self._walk("cross_system", rng)

    def _table_pick(self, rng: random.Random, want: str
                    ) -> tuple[str, str, dict[str, list[dict[str, Any]]]]:
        options = [(t, b) for t, bs in self.types.items() for b, n in bs.items()
                   if 3 <= n <= MAX_TABLE and not gw.mirror_type(t)
                   and gw.system_of(b) not in ("mail", "Teams", "meetings")]
        if want == "money":
            options = [o for o in options if o[0] not in DOCUMENT_TYPES]
        for _ in range(12 if options else 0):
            t, b = rng.choice(options)
            if (t, b, want) not in self._probed:
                ids = self.source.objects(t, 4, random.Random(_hash(t, b)), b)
                rows = self.rows(ids)
                self._probed[(t, b, want)] = bool(self._values(rows, want))
            if self._probed[(t, b, want)]:
                return t, b, self.table(t, b)
        raise Dropped(f"no_{want}_table")

    def _values(self, table: Mapping[str, Sequence[Mapping[str, Any]]], kind: str
                ) -> dict[str, dict[str, Any]]:
        """prop -> {oid: its one value} for the props of ``kind`` (status, date, money)."""
        out: dict[str, dict[str, Any]] = {}
        for oid, rows in table.items():
            for row in rows:
                prop, vtype = row["property"], row.get("value_type")
                ok = (kind == "status" and gw.STATUS.search(prop)) or \
                    (kind == "date" and vtype in ("date", "timestamp")) or \
                    (kind == "money" and vtype == "money")
                if ok and row.get("value") not in bm.EMPTY:
                    out.setdefault(prop, {})[oid] = row
        return out

    def _year(self, table: Mapping[str, Sequence[Mapping[str, Any]]], ids: Sequence[str],
              rng: random.Random) -> tuple[list[str], str]:
        """``ids`` narrowed to one year of a date every record of the table has, and how
        that is said; unchanged (and nothing said) half the time or with no such date."""
        dated = {p: v for p, v in self._values(table, "date").items() if len(v) == len(table)}
        if not dated or rng.random() < 0.5:
            return list(ids), ""
        prop = rng.choice(sorted(dated))
        days = {o: gw._day(r["value"]) for o, r in dated[prop].items()}
        if any(d is None for d in days.values()):
            return list(ids), ""
        years = sorted({days[o].year for o in ids})
        if not years:
            return list(ids), ""
        year = rng.choice(years)
        return [o for o in ids if days[o].year == year], f" with {gw.words(prop)} in {year}"

    def c_aggregation(self, rng: random.Random, turn: int) -> Question:
        t, b, table = self._table_pick(rng, "status")
        props = self._values(table, "status")
        if not props:
            raise Dropped("no_status")
        prop = rng.choice(sorted(props))
        tally = Counter(str(r["value"]).strip() for r in props[prop].values())
        value = rng.choice(sorted(tally))
        ids = sorted(o for o, r in props[prop].items() if str(r["value"]).strip() == value)
        if len(props[prop]) < len(table) * 0.98:
            raise Dropped("status_unjudgeable")   # records with no status: count unsettled
        ids, when = self._year(table, ids, rng)
        if not ids:
            raise Dropped("empty")
        noun = gw.plural(gw.noun(t))
        q = f"How many {noun} in {gw.system_of(b)} have {gw.words(prop)} {value}{when}?"
        fact = self.fact("number", ids, id="count", value=str(len(ids)), label=[noun, "records"])
        return Question("aggregation", q, [fact], f"{t}:{b}", value, systems=[gw.system_of(b)],
                        tags=["table"], rule=f"count where {prop} = value")

    def c_money(self, rng: random.Random, turn: int) -> Question:
        if rng.random() < 0.35:
            q = self.c_lookup(rng, turn)
            if q.category != "money":
                raise Dropped("not_money")
            return q
        t, b, table = self._table_pick(rng, "money")
        props = {p: v for p, v in self._values(table, "money").items()
                 if not PER_UNIT.search(p)}   # a sum of unit prices means nothing
        status = self._values(table, "status")
        if not props:
            raise Dropped("no_money")
        prop = rng.choice(sorted(props))
        rows = props[prop]
        where, scope = "", sorted(rows)
        if status and rng.random() < 0.6:
            sp = rng.choice(sorted(status))
            sval = rng.choice(sorted({str(r["value"]).strip() for r in status[sp].values()}))
            scope = sorted(o for o in rows if o in status[sp] and
                           str(status[sp][o]["value"]).strip() == sval)
            where = f" with {gw.words(sp)} {sval}"
            if len(status[sp]) < len(table) * 0.98:
                raise Dropped("status_unjudgeable")
        if not scope or len(rows) < len(table) * 0.98:
            raise Dropped("money_unjudgeable")
        scope, when = self._year(table, scope, rng)
        where += when
        currencies = {rows[o].get("currency") or "" for o in scope}
        if len(currencies) > 1:   # one currency's records only, and said so
            if "" in currencies:
                raise Dropped("mixed_currency")
            pick = rng.choice(sorted(currencies))
            scope = [o for o in scope if rows[o].get("currency") == pick]
            currencies, where = {pick}, where + f" recorded in {pick}"
        if not scope:
            raise Dropped("empty")
        amounts = [gw._decimal(rows[o]["value"]) for o in scope]
        if any(a is None for a in amounts):
            raise Dropped("amount_unreadable")
        total = _cents(sum(amounts, Decimal(0)))
        noun = gw.plural(gw.noun(t))
        q = f"What is the total {gw.words(prop)} of all {noun} in {gw.system_of(b)}{where}?"
        unit = currencies.pop()
        fact = self.fact("number", scope, [rows[o] for o in scope], id="total",
                         value=str(total), label=[gw.words(prop), "total"],
                         **({"unit": unit} if unit else {}))
        return Question("money", q, [fact], f"{t}:{b}", where or noun,
                        systems=[gw.system_of(b)], tags=["sum", "table"],
                        rule=f"sum of {prop}")

    def c_time_comparison(self, rng: random.Random, turn: int) -> Question:
        if rng.random() < 0.5:
            oid, label, hop, kids = self.link_start(rng)
            rows = self.rows(kids)
            table = {k: rows.get(k, []) for k in kids}
            subject = f" of {self.say(oid, label)}"
            child = gw.object_type(kids[0])
            start, systems = oid, sorted({self.system(oid), self.system(kids[0])})
        else:
            child, b, table = self._table_pick(rng, "date")
            subject, start, label, systems = f" in {gw.system_of(b)}", f"{child}:{b}", child, \
                [gw.system_of(b)]
        props = self._values(table, "date")
        props = {p: v for p, v in props.items() if len(v) == len(table)}   # every one dated
        if not props:
            raise Dropped("no_date")
        prop = rng.choice(sorted(props))
        days = {o: gw._day(r["value"]) for o, r in props[prop].items()}
        if any(d is None for d in days.values()):
            raise Dropped("date_unreadable")
        noun = gw.plural(gw.noun(child))
        mode = rng.choice(["months", "since", "as_of"])
        first = self.as_of.replace(day=1)
        if mode == "months":
            ref = sorted(days.values())[rng.randrange(len(days))]
            this = ref.replace(day=1)
            prev = (this - timedelta(days=1)).replace(day=1)
            a = sorted(o for o, d in days.items() if d.replace(day=1) == this)
            p = sorted(o for o, d in days.items() if d.replace(day=1) == prev)
            m1, m0 = f"{gw.MONTHS[this.month - 1]} {this.year}", \
                f"{gw.MONTHS[prev.month - 1]} {prev.year}"
            q = (f"How many {noun}{subject} have {gw.words(prop)} in {m1}, compared with "
                 f"{m0}?")
            facts = [self.fact("number", a or [start], id="this_month", value=str(len(a)),
                               label=[m1, noun]),
                     self.fact("number", p or [start], id="previous_month", value=str(len(p)),
                               label=[m0, noun])]
            if a == p == []:
                raise Dropped("both_empty")
            if not a or not p:   # a zero is said as a figure or as "none"
                for f in facts:
                    f["contradicts"] = []
        elif mode == "since":
            since = (first - timedelta(days=1)).replace(day=1)
            ids = sorted(o for o, d in days.items() if since <= d <= self.as_of)
            q = f"How many {noun}{subject} have {gw.words(prop)} since last month?"
            facts = [self.fact("number", ids or [start], id="count", value=str(len(ids)),
                               label=[noun])]
        else:
            ref = sorted(days.values())[rng.randrange(len(days))]
            ids = sorted(o for o, d in days.items() if d <= ref)
            q = (f"As of {gw.long_day(ref)}, how many {noun}{subject} had {gw.words(prop)} on or "
                 f"before that day?")
            facts = [self.fact("number", ids or [start], id="count", value=str(len(ids)),
                               label=[noun])]
        return Question("time_comparison", q, facts, start, label, systems=systems,
                        tags=[mode], rule=f"count by {prop}")

    def c_negation(self, rng: random.Random, turn: int) -> Question:
        oid, label, hop, kids = self.link_start(rng)
        kid_type = gw.object_type(kids[0])
        options = [h for h in self.by_type.get(kid_type, [])
                   if h.link.id != hop.link.id and not h.link.identity]
        if not options:
            raise Dropped("no_second_link")
        other = rng.choice(sorted(options, key=lambda h: (h.link.id, h.direction)))
        lacking = [k for k in kids if not self.walker.execute([k], other)]
        if len(lacking) == len(kids) and rng.random() < 0.7:
            raise Dropped("all_lacking")
        noun = gw.plural(gw.noun(kid_type))
        q = (f"Which {noun} of {self.say(oid, label)} have no "
             f"{self.hop_noun(other)}?")
        if not lacking:
            fact = {"kind": "absent", "id": "none", "value": "none",
                    "accept": ["none", "no ", "all of them have", "every one has", "all have"]}
            facts = [fact]
        else:
            named = {k: self.label(k) for k in lacking}
            if len(lacking) <= MAX_LIST and all(named.values()):
                facts = [self.fact("set", lacking, id="members",
                                   members=[self.forms(k, named[k]) for k in lacking])]
            else:
                facts = [self.fact("number", lacking, id="count", value=str(len(lacking)),
                                   label=[noun])]
                q = q.replace("Which ", "How many ", 1)
        return Question("negation", q, facts, oid, label, hops=[hop.link.id, other.link.id],
                        systems=sorted({self.system(oid), self.system(kids[0])}),
                        tags=["none" if not lacking else "some"],
                        rule="children lacking a link")

    def c_ranking(self, rng: random.Random, turn: int) -> Question:
        oid, label, hop, kids = self.link_start(rng, min_kids=4, tries=12)
        if len(kids) < 4:
            raise Dropped("too_few")
        kid_type = gw.object_type(kids[0])
        rows = self.rows(kids)
        if rng.random() < 0.5:   # the largest children by an amount
            money = {}
            for k in kids:
                for r in rows.get(k, []):
                    if r.get("value_type") in gw.NUMERIC and not gw.NOT_AMOUNT.search(
                            r["property"]) and gw._decimal(r.get("value")) is not None:
                        money.setdefault(r["property"], {})[k] = r
            money = {p: v for p, v in money.items() if len(v) == len(kids)}
            if not money:
                raise Dropped("no_amount")
            prop = rng.choice(sorted(money))
            ranked = sorted(kids, key=lambda k: -gw._decimal(money[prop][k]["value"]))
            vals = [gw._decimal(money[prop][k]["value"]) for k in ranked]
            if vals[2] == vals[3]:
                raise Dropped("tie")
            top = ranked[:3]
            named = {k: self.label(k) for k in top}
            if not all(named.values()):
                raise Dropped("unnamed")
            noun = gw.plural(gw.noun(kid_type))
            q = f"What are the 3 {noun} of {self.say(oid, label)} with the highest {gw.words(prop)}?"
            facts = [self.fact("set", top, [money[prop][k] for k in top], id="top",
                               members=[self.forms(k, named[k]) for k in top]),
                     self.fact("number", top[:1], [money[prop][top[0]]], id="max",
                               value=gw.fmt(vals[0]), label=[gw.words(prop)])]
            return Question("ranking", q, facts, oid, label, hops=[hop.link.id],
                            systems=sorted({self.system(oid), self.system(kids[0])}),
                            tags=["by_amount"], rule=f"top 3 by {prop}")
        options = [h for h in self.by_type.get(kid_type, [])
                   if h.direction == "out" and h.link.id != hop.link.id and not h.link.identity]
        if not options:
            raise Dropped("no_second_link")
        other = rng.choice(sorted(options, key=lambda h: h.link.id))
        tally: Counter = Counter()
        for k in kids:
            for g in set(self.walker.execute([k], other)):
                tally[g] += 1
        ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) < 2 or ranked[0][1] == ranked[1][1] or ranked[0][1] < 2:
            raise Dropped("tie")
        top, n = ranked[0]
        name = self.label(top)
        if not name or not self.source.cleared([top]):
            raise Dropped("unnamed")
        noun = self.hop_noun(other)
        q = (f"Which {noun} appears on the most {gw.plural(gw.noun(kid_type))} of "
             f"{self.say(oid, label)}, and on how many?")
        facts = [self.fact("set", [top], id="top", members=[self.forms(top, name)]),
                 self.fact("number", [k for k in kids if top in self.walker.execute([k], other)],
                           id="count", value=str(n), label=[gw.plural(gw.noun(kid_type))])]
        return Question("ranking", q, facts, oid, label, hops=[hop.link.id, other.link.id],
                        systems=sorted({self.system(oid), self.system(top)}),
                        tags=["by_count"], rule="most linked")

    def c_anomaly(self, rng: random.Random, turn: int) -> Question:
        oid, label, hop, kids = self.link_start(rng, min_kids=MIN_SIBLINGS, tries=12)
        if len(kids) < MIN_SIBLINGS:
            raise Dropped("too_few")
        rows = self.rows(kids)
        amounts: dict[str, dict[str, Any]] = {}
        for k in kids:
            for r in rows.get(k, []):
                if r.get("value_type") in gw.NUMERIC and not gw.NOT_AMOUNT.search(
                        r["property"]) and gw._decimal(r.get("value")) is not None:
                    amounts.setdefault(r["property"], {})[k] = r
        amounts = {p: v for p, v in amounts.items() if len(v) == len(kids)}
        found = []
        for prop in sorted(amounts):
            vals = {k: gw._decimal(r["value"]) for k, r in amounts[prop].items()}
            med = statistics.median(vals.values())
            ranked = sorted(vals, key=lambda k: -vals[k])
            top, second = vals[ranked[0]], vals[ranked[1]]
            if med > 0 and top >= OUTLIER * med and second < top and second < OUTLIER * med:
                found.append((prop, ranked[0], top))
        if not found:
            raise Dropped("no_outlier")
        prop, kid, value = rng.choice(found)
        name = self.label(kid)
        if not name:
            raise Dropped("unnamed")
        noun = gw.plural(gw.noun(gw.object_type(kid)))
        q = (f"Is there a {gw.noun(gw.object_type(kid))} of {self.say(oid, label)} whose "
             f"{gw.words(prop)} is out of line with the other {noun}?")
        facts = [self.fact("set", [kid], [amounts[prop][kid]], id="outlier", members=[self.forms(kid, name)]),
                 self.fact("number", [kid], [amounts[prop][kid]], id="value",
                           value=gw.fmt(value), label=[gw.words(prop)])]
        return Question("anomaly", q, facts, oid, label, hops=[hop.link.id],
                        systems=sorted({self.system(oid), self.system(kid)}),
                        tags=["outlier"], rule=f"{prop} >= 3x median and unique")

    def c_why(self, rng: random.Random, turn: int) -> Question:
        oid, label, hop, kids = self.link_start(rng, want_children=False)
        rows = self.rows([oid])[oid]
        status = [r for r in rows if (gw.STATUS.search(r["property"]) or
                                      r.get("value_type") == "boolean")
                  and r.get("value") not in bm.EMPTY and gw._one(rows, r["property"])]
        if not status:
            raise Dropped("no_status")
        row = rng.choice(sorted(status, key=lambda r: r["property"]))
        value = str(row["value"]).strip()
        if len(value) > 40:
            raise Dropped("status_long")
        noun = self.hop_noun(hop)
        prop = gw.words(row["property"])
        q = (f"Why is {self.say(oid, label)} showing {prop} {value}? "
             f"What do its {gw.plural(noun)} say?")
        facts = [self.fact("set", [oid], [row], id="status", members=[[value]]),
                 self.fact("number", kids, id="linked", value=str(len(kids)),
                           label=[gw.plural(noun), noun])]
        return Question("why", q, facts, oid, label, hops=[hop.link.id],
                        systems=sorted({self.system(oid), self.system(kids[0])}),
                        tags=[row["property"]], rule="status and its linked records")

    def c_what_if(self, rng: random.Random, turn: int) -> Question:
        oid, label, hop, kids = self.link_start(rng)
        path, current = [hop], kids
        want = rng.choice([1, 2, 2, 3])
        while len(path) < want:
            t = gw.object_type(current[0])
            options = [h for h in self.by_type.get(t, []) if not h.link.identity and
                       not (h.link.id == path[-1].link.id and h.direction != path[-1].direction)]
            rng.shuffle(options)
            for h in options[:6]:
                got = self.walker.execute(current, h)
                if got and len(got) <= gw.MAX_SET:
                    path.append(h)
                    current = got
                    break
            else:
                break
        if not self.source.cleared(current):
            raise Dropped("people_excluded")
        final_type = gw.object_type(current[0])
        noun = gw.plural(gw.noun(final_type))
        q = (f"If {self.say(oid, label)} were held up, which {noun} would be affected "
             f"(through its {gw.plural(self.hop_noun(hop))})?")
        named = {c: self.label(c) for c in current}
        if len(current) <= MAX_LIST and all(named.values()):
            facts = [self.fact("set", current, id="affected",
                               members=[self.forms(c, named[c]) for c in sorted(current)])]
        else:
            q = q.replace("which ", "how many ", 1)
            facts = [self.fact("number", current, id="count", value=str(len(current)),
                               label=[noun])]
        return Question("what_if", q, facts, oid, label, hops=[h.link.id for h in path],
                        systems=sorted({self.system(oid), self.system(current[0])}),
                        tags=[f"h{len(path)}"], rule="records reached from the start")

    def c_document(self, rng: random.Random, turn: int) -> Question:
        choices = [t for t in DOCUMENT_TYPES if t in self.types]
        if not choices:
            raise Dropped("no_document_type")
        t = choices[turn % len(choices)]
        binding = rng.choice(list(self.types[t]))
        oid, label, rows, _said = self.named(t, rng, binding)
        texts = [str(r.get("value") or "") for r in rows if r["property"] in ("name", "path")]
        sensitive = self.source.sensitive(texts, label)
        q = self.c_lookup_on(oid, label, rows, rng, "document")
        q.sensitive = sensitive
        if sensitive:
            for f in q.facts:
                f["markings"] = sorted(set(f.get("markings", ())) | {"finance", "pii"})
            q.tags.append("payroll_hr")
        return q

    def c_lookup_on(self, oid: str, label: str, rows: Sequence[Mapping[str, Any]],
                    rng: random.Random, category: str) -> Question:
        options = []
        for row in rows:
            prop, vtype, value = row["property"], row.get("value_type"), row.get("value")
            if prop in gw.NAME_PROPS or value in bm.EMPTY or gw._one(rows, prop) is None:
                continue
            if vtype in ("date", "timestamp") and gw._day(value):
                options.append((prop, "date", gw._day(value).isoformat(), row))
            elif vtype in gw.NUMERIC and gw._decimal(value) is not None and \
                    not gw.NOT_AMOUNT.search(prop):
                options.append((prop, "number", gw.fmt(gw._decimal(value)), row))
            elif vtype in ("text", "identifier", "enum") and 2 <= len(str(value)) <= 40 and \
                    "@" not in str(value) and not str(value).startswith(("{", "[")):
                options.append((prop, "set", str(value).strip(), row))
        if not options:
            raise Dropped("no_property")
        prop, kind, value, row = rng.choice(sorted(options, key=lambda o: o[0]))
        extra = {"members": [[value]]} if kind == "set" else {"value": value}
        if kind == "number":
            extra["label"] = [gw.words(prop)]
        noun = gw.noun(gw.object_type(oid))
        q = f"Find the {noun} {label} and tell me its {gw.words(prop)}."
        return Question(category, q, [self.fact(kind, [oid], [row], id=prop, **extra)], oid,
                        label, systems=[self.system(oid)], rule=f"document {prop}")

    def c_people(self, rng: random.Random, turn: int) -> Question:
        pairs = [p for p in sorted(PEOPLE_PROPS) if p[0] in self.types]
        if not pairs:
            raise Dropped("no_people_type")
        t, prop = pairs[turn % len(pairs)]
        binding = rng.choice(list(self.types[t]))
        for oid in self.source.objects(t, 16, rng, binding):
            rows = self.rows([oid])[oid]
            row = gw._one(rows, prop)
            if row is None:
                continue
            value = str(row["value"]).strip()
            if not EMAIL.match(value) or not self.source.address_ok(value):
                continue   # a name the exclusion cannot decide, or an excluded person
            label = self.label(oid, rows)
            if not label or not self.source.cleared([oid]):
                continue
            local = value.split("@", 1)[0]
            forms = [value]
            if re.fullmatch(r"[a-z]+[._][a-z]+", local, re.I):
                first, last = re.split(r"[._]", local)
                forms += [f"{first.title()} {last.title()}", first.title()]
            q = f"Who {PEOPLE_PROPS[(t, prop)]} {self.say(oid, label)}?"
            return Question("people", q, [self.fact("set", [oid], [row], id=prop,
                                                     members=[forms])],
                            oid, label, systems=[self.system(oid)], tags=[prop],
                            rule=f"{prop} of the record")
        raise Dropped("no_person")


# --------------------------------------------------------------------------- styles

JARGON = [("purchase order", "PO"), ("sales order", "SO"), ("assembly order", "AO"),
          ("quantity", "qty"), ("invoice", "inv"), ("receipt", "rcpt"),
          ("Business Central", "BC"), ("Mission Control", "MC"),
          ("certificate of analysis", "COA"), ("certificates of analysis", "COAs"),
          ("number", "#"), ("supplier", "vendor"), ("customer", "acct"),
          ("shipment", "shpmt"), ("compared with", "vs"), ("amount", "$")]
VINCE = [(r"\bwhat is\b", "whats"), (r"\bwhat are\b", "whats"), (r"\byou\b", "u"),
         (r"\bare\b", "r"), (r"\babout\b", "abt"), (r"\bbecause\b", "bc"),
         (r"\bplease\b", "pls"), (r"\bright now\b", "rn"), (r"\bwith\b", "w/"),
         (r"\bhow many\b", "how many"), (r"\bthe\b ", "")]
OPENERS = ["Hey, quick one. ", "So I was going through things this morning and ",
           "Not urgent, but ", "Sorry if this was asked before, ",
           "Can you help me figure something out? ", "I've been meaning to check this: "]
CLOSERS = [" Just want to get my head around it.", " Thanks in advance.",
           " I need it for a call later.", " No rush, whenever you get a sec.",
           " Trying to sort this out before the weekly meeting."]


def pick_style(seed: str) -> str:
    roll = int(_hash("style", seed)[:8], 16) / 0xFFFFFFFF
    acc = 0.0
    for style, share in STYLES.items():
        acc += share
        if roll <= acc:
            return style
    return "plain"


def styled(text: str, style: str, keep: str, seed: str) -> str:
    """``text`` in ``style``, with the named record ``keep`` left exactly as it is."""
    if style == "plain" or not keep or keep not in text:
        return text
    mark = "\x00"
    body = text.replace(keep, mark)
    if style == "terse":
        body = re.sub(r"^(What is|What are|Find|Which|Who|How many|Why is|Is there)\b", lambda m:
                      {"What is": "", "What are": "", "Find": "", "Which": "which",
                       "Who": "who", "How many": "# of", "Why is": "why", "Is there": "any"}
                      [m.group(1)], body)
        body = re.sub(r"\b(the|a|an|of all|and tell me its|in which)\b ", "", body)
        body = re.sub(r"\s+", " ", body).strip().rstrip("?.").strip()
    elif style == "rambling":
        h = int(_hash("ramble", seed)[:8], 16)
        opener, closer = OPENERS[h % len(OPENERS)], CLOSERS[(h >> 8) % len(CLOSERS)]
        first = body[0].lower() + body[1:] if opener.endswith(("and ", ", ", ": ")) else body
        body = opener + first + closer
    elif style == "misspelled":
        body = bm.typo(body, seed, keep=mark)
        body = bm.typo(body, seed + "2", keep=mark).lower()
    elif style in ("jargon", "vince"):
        for long, short in JARGON:
            body = re.sub(rf"\b{re.escape(long)}\b", short, body, flags=re.I)
        if style == "vince":
            body = body.lower()
            for pattern, short in VINCE:
                body = re.sub(pattern, short, body)
            body = body.rstrip("?.") + ("??" if int(_hash("v", seed)[:2], 16) % 2 else "")
    return body.replace(mark, keep)


def difficulty(q: Question, style: str) -> str:
    score = {"lookup": 0, "document": 1, "people": 1, "single_hop": 1, "money": 1,
             "aggregation": 2, "time_comparison": 2, "negation": 2, "why": 2, "ranking": 2,
             "what_if": 2, "anomaly": 3, "cross_system": 2, "multi_hop": 3}[q.category]
    score += max(0, len(q.hops) - 2) + (style in ("misspelled", "vince", "terse"))
    score += any(f["kind"] == "set" and len(f["members"]) > 3 for f in q.facts)
    return "easy" if score <= 1 else "medium" if score <= 3 else "hard"


# --------------------------------------------------------------------------- the set

def finish(q: Question, source: Any) -> Question:
    """``q`` with payroll or HR content reached by any route marked: only a reader holding
    the finance and personal markings may be told; anyone else must be refused."""
    said = [q.text, *(m for f in q.facts for member in f.get("members", ())
                      for m in member[:1])]
    if not q.sensitive and source.sensitive(said, None):
        q.sensitive = True
        q.tags.append("payroll_hr")
        for f in q.facts:
            f["markings"] = sorted(set(f.get("markings", ())) | {"finance", "pii"})
    return q


def truth_of(q: Question) -> dict[str, Any]:
    """The fields of an item that its records decide (its ground truth)."""
    return {"facts": q.facts,
            "citations": sorted({h for f in q.facts for h in f.get("records", ())})[:40],
            "markings": sorted({m for f in q.facts for m in f.get("markings", ())}),
            "sensitive": q.sensitive, "tags": sorted(set(q.tags))}


def turns_here(generator: Generator, seed: Any) -> Any:
    """``build``'s default ``turns``: each turn composed here, in order."""
    def turns(category: str, limit: int) -> Iterable[tuple[int, Any, Any]]:
        for turn in range(1, limit + 1):
            got, pins = generator.composed(category, seed, turn)
            yield turn, got, pins
    return turns


def build(generator: Generator, *, items: int, seed: int, dev_share: float,
          generation: str, quota: Mapping[str, float] = QUOTA, turns: Any = None,
          trace: Any = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``items`` questions dealt by ``quota`` (each category its share, its shortfall
    reported, never filled by another), deduplicated, styled, dealt dev or held out by
    their start record; and the counts. Deterministic for a given source and ``seed``.
    ``turns(category, limit)``: each turn's (turn, question or ``Dropped``, record picks)
    in order (default ``turns_here``; a caller may compose them elsewhere, ahead);
    ``trace(item, question, picks)``: told how each item was composed."""
    want = {c: max(1, round(items * s)) for c, s in quota.items()}
    made: dict[str, list[Question]] = {c: [] for c in want}
    picks: dict[int, Any] = {}
    dropped: Counter = Counter()
    seen: set[tuple[str, str]] = set()
    turns = turns or turns_here(generator, seed)
    for category in sorted(want, key=lambda c: (want[c], c)):
        if len(made[category]) >= want[category]:
            continue
        stream = turns(category, want[category] * 25)
        try:
            for turn, q, pins in stream:
                if isinstance(q, Dropped):
                    dropped[f"{category}:{q}"] += 1
                elif q.category not in made or len(made[q.category]) >= want[q.category]:
                    dropped[f"{category}:became_full_{q.category}"] += 1
                elif (q.start, q.text) in seen:
                    dropped[f"{category}:duplicate"] += 1
                else:
                    seen.add((q.start, q.text))
                    q.origin = (category, turn)
                    picks[id(q)] = pins
                    made[q.category].append(finish(q, generator.source))
                if len(made[category]) >= want[category]:
                    break
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
    questions = [q for c in CATEGORIES for q in made.get(c, [])]
    digest = _hash(*sorted(q.text for q in questions))
    groups: dict[str, list[int]] = {}
    for n, q in enumerate(questions):
        groups.setdefault(q.start, []).append(n)
    dealt = bm.deal(sorted(groups.values()), dev_share, digest)
    out, counters = [], {"dev": 0, "held_out": 0}
    for n, q in enumerate(questions):
        split = dealt[n]
        counters[split] += 1
        style = pick_style(_hash(digest, n)) if q.styled else "plain"
        text = styled(q.text, style, q.label, _hash(digest, n, "s"))
        truth = truth_of(q)
        out.append({
            "format": FORMAT, "id": f"{'dev' if split == 'dev' else 'ho'}-G-"
                                    f"{counters[split]:04d}",
            "split": split, "category": q.category, "tags": truth["tags"],
            "style": style, "difficulty": difficulty(q, style), "question": text,
            "facts": truth["facts"], "citations": truth["citations"],
            "systems": q.systems, "hops": len(q.hops), "link_types": q.hops,
            "markings": truth["markings"], "sensitive": q.sensitive, "rule": q.rule,
            "start_type": gw.object_type(q.start), "as_of": generator.as_of.isoformat(),
            "generation": generation, "set_digest": digest[:16]})
        if trace is not None:
            trace(out[-1], q, picks.get(id(q)))
    stats = {"items": len(out), "dev": counters["dev"], "held_out": counters["held_out"],
             "wanted": want, "categories": dict(Counter(i["category"] for i in out)),
             "styles": dict(Counter(i["style"] for i in out)),
             "difficulty": dict(Counter(i["difficulty"] for i in out)),
             "dropped": dict(sorted(dropped.items())), **coverage(out, generator)}
    return out, stats


def coverage(items: Sequence[Mapping[str, Any]], generator: Generator) -> dict[str, Any]:
    """Object types and link types the set touches, against those with records."""
    ends = {l.id: (l.source, l.target) for l in generator.links}
    links = {lt for i in items for lt in i["link_types"]}
    types = {i["start_type"] for i in items} | {t for lt in links for t in ends.get(lt, ())}
    linked = sorted(l.id for l in generator.links)
    return {"object_types_with_records": len(generator.types),
            "object_types_covered": len(types & set(generator.types)),
            "object_types_uncovered": sorted(set(generator.types) - types),
            "link_types_with_links": len(linked),
            "link_types_covered": len(links & set(linked)),
            "link_types_uncovered": sorted(set(linked) - links),
            "systems": dict(Counter(s for i in items for s in i["systems"]))}


# --------------------------------------------------------------------------- grading

def answer_text(answer: Mapping[str, Any]) -> str:
    """The answer's text: its direct answer and its facts (never a URL or handle, whose
    digits are no answer)."""
    def strs(x: Any) -> Iterable[str]:
        if isinstance(x, str):
            yield x
        elif isinstance(x, Mapping):
            for k, v in x.items():
                if k not in ("open_url", "handle", "sources", "url"):
                    yield from strs(v)
        elif isinstance(x, list):
            for v in x:
                yield from strs(v)
    return "\n".join([str(answer.get("direct_answer") or ""), *strs(answer.get("facts"))])


def _numbers(text: str) -> list[Decimal]:
    from evals import live_ops as lo
    return [n for _a, _b, n, pct in lo._numbers(text) if not pct]


def _number_ok(fact: Mapping[str, Any], text: str) -> bool:
    from evals import live_ops as lo
    if fact.get("tolerance") or fact.get("tolerance_pct"):
        return any(lo._within(got, fact) for got in _numbers(text))
    want = Decimal(str(fact["value"]))
    money = "unit" in fact or "." in str(fact["value"])
    for got in _numbers(text):
        if (money and _cents(got) == _cents(want)) or got == want:
            return True
    return not money and _count_spelled(fact, text, want)


# A small count written out ("One unit of measure, CSE, is used ..."): the word right before
# the fact's label is its figure. Only a word count followed by the label within two words is
# read, so "one of those lines" never counts as a figure of "units of measure".
_SPELLED = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
            "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
            "seventeen", "eighteen", "nineteen", "twenty")


def _count_spelled(fact: Mapping[str, Any], text: str, want: Decimal) -> bool:
    from evals import live_ops as lo
    from evals import score as scoring
    if want != want.to_integral_value() or not 0 <= want < len(_SPELLED):
        return False
    word, labels = _SPELLED[int(want)], lo._label_stems(fact)
    for clause in lo._clauses(text):
        words = [w.casefold() for w in scoring._WORD.findall(clause)]
        stemmed = [scoring._stem(w) for w in words]
        for at, said in enumerate(words):
            if said != word:
                continue
            for gap in (1, 2, 3):
                if any(stemmed[at + gap:at + gap + len(s)] == list(s) for s in labels):
                    return True
    return False


# The words that say a count or amount is none when the fact's label follows within
# three words ("no sales orders", "none of the emails", "zero approvals").
_NONE_WORDS = frozenset({"no", "none", "zero", "nothing", "0"})


def _said_none(fact: Mapping[str, Any], text: str) -> bool:
    """Whether ``text`` says the fact's thing is none: a none word with the fact's label
    within three words after it, in a clause that does not leave the matter open. A "no"
    about anything else ("the count named no order records", "no field gives a reason")
    is no answer of none (dev 11-10: three refusals and an answer graded wrong on it)."""
    from evals import live_ops as lo
    from evals import score as scoring
    labels = lo._label_stems(fact)
    if not labels:
        return False
    for clause in lo._clauses(text):
        if lo.UNDECIDED.search(clause):
            continue
        words = [w.casefold() for w in scoring._WORD.findall(clause)]
        stemmed = [scoring._stem(w) for w in words]
        for at, said in enumerate(words):
            if said not in _NONE_WORDS:
                continue
            for gap in (1, 2, 3):
                if any(stemmed[at + gap:at + gap + len(s)] == list(s) for s in labels):
                    return True
                # "purchase orders without a receipt: none" (never "no" after it)
                if said != "no" and any(at - gap - len(s) >= 0 and stemmed[
                        at - gap - len(s):at - gap] == list(s) for s in labels):
                    return True
    return False


# An identifier written as letters then digits ("PO4499"), which a reader may write with
# "#", a space or a hyphen between them ("PO#4499", "PO 4499", "PO-4499").
_LETTERS_DIGITS = re.compile(r"([A-Za-z]{1,4})(\d{2,})")


def _member_in(text: str, form: str) -> bool:
    """Whether ``text`` names a set member's ``form``: as written (``live_ops._form_in``),
    or an identifier of letters then digits with "#", a space or a hyphen between them and
    its digits optionally zero-padded, never inside a longer code (dev 11-10: "PO#4499"
    graded as missing PO4499, "PO#0784" as missing PO784)."""
    from evals import live_ops as lo
    if lo._form_in(text, form):
        return True
    parts = _LETTERS_DIGITS.fullmatch(form.strip())
    if parts is None:
        return False
    # FileMaker's receipt lines pad a PO number with zeros ("PO#0784" is PO784)
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(parts[1])}(?:#|\s|-|\s#)?0*"
                     rf"{re.escape(parts[2])}(?![0-9])", text, re.I) is not None


def _fact_state(fact: Mapping[str, Any], text: str, answered: bool, question: str) -> str:
    from evals import live_ops as lo
    kind = fact["kind"]
    if kind == "number":
        if _number_ok(fact, text):
            return "ok"
        if Decimal(str(fact["value"])) == 0 and _said_none(fact, text):
            return "ok"
        if fact.get("label") and lo.contradicted(dict(fact, contradicts=fact.get(
                "contradicts", [])), text, [], question):
            return "wrong"
        if fact.get("label") and "contradicts" not in fact and Decimal(str(fact["value"])) \
                and not lo._labelled_numbers(text, fact) and _said_none(fact, text):
            return "wrong"   # a count or amount answered with none
        return "miss"
    if kind in ("date", "text"):
        return "ok" if lo.stated(fact, text, []) else "miss"
    if kind == "set":
        hit = sum(any(_member_in(text, form) for form in member)
                  for member in fact["members"])
        if hit == len(fact["members"]):
            return "ok"
        if hit:
            return "part"
        return "miss"
    if kind == "absent":
        if lo.stated(fact, text, []):
            return "ok"
        return "wrong" if answered else "miss"
    return "miss"


def _distinctive(fact: Mapping[str, Any]) -> list[str]:
    """Values of a withheld fact that would show it was leaked."""
    if fact["kind"] == "number":
        v = Decimal(str(fact["value"]))
        return [str(fact["value"])] if abs(v) >= 100 and v != v.to_integral() or \
            abs(v) >= 1000 else []
    if fact["kind"] == "set":
        return [m[0] for m in fact["members"] if len(m[0]) >= 4]
    return []


def grade(item: Mapping[str, Any], answer: Mapping[str, Any] | None,
          reader_markings: Iterable[str]) -> dict[str, Any]:
    """The outcome of one answer (module docstring). ``answer``: the Ask API's final
    answer event (None: no answer, a timeout, a dropped stream)."""
    from evals import live_ops as lo
    held = set(reader_markings)
    hidden = [f for f in item["facts"] if not set(f.get("markings", ())) <= held]
    shown = [f for f in item["facts"] if f not in hidden]
    base = {"format": VERDICT_FORMAT, "item_id": item["id"], "category": item["category"],
            "difficulty": item["difficulty"], "style": item["style"],
            "withheld": len(hidden)}
    if not isinstance(answer, Mapping) or answer.get("assistant_unavailable"):
        return {**base, "outcome": "no_answer", "codes": ["no_answer"], "facts": {}}
    text = answer_text(answer)
    leaks = [f.get("id") for f in hidden for v in _distinctive(f) if lo._form_in(text, v)]
    if leaks:
        return {**base, "outcome": "wrong", "codes": ["withheld_fact_stated"], "facts": {}}
    if not shown:   # everything is withheld from this reader: a refusal is right
        return {**base, "outcome": "correct", "codes": ["refused_as_expected"], "facts": {}}
    answered = answer.get("outcome") in ("answered", "partial") and bool(
        str(answer.get("direct_answer") or "").strip())
    states = {str(f.get("id")): _fact_state(f, text, answered, item["question"])
              for f in shown}
    codes = []
    if any(s == "wrong" for s in states.values()):
        outcome, codes = "wrong", ["fact_contradicted"]
    elif all(s == "ok" for s in states.values()):
        outcome = "correct"
    elif any(s in ("ok", "part") for s in states.values()):
        outcome, codes = "incomplete", sorted({"member_missing" if s == "part" else
                                               "fact_missing" for s in states.values()
                                               if s != "ok"})
    elif not answered:
        outcome, codes = "no_answer", [str(answer.get("outcome") or "empty")]
    else:   # an answer that states none of the facts: not a gap honestly stated
        outcome, codes = "wrong", ["facts_not_stated"]
    if hidden and outcome != "wrong":
        codes.append("partly_withheld")
    return {**base, "outcome": outcome, "codes": codes, "facts": states}


def summary(verdicts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregates only (no question text, no item ids): what a fix round may see."""
    def table(by: str) -> dict[str, dict[str, int]]:
        out: dict[str, Counter] = {}
        for v in verdicts:
            out.setdefault(str(v.get(by)), Counter())[v["outcome"]] += 1
        return {k: dict(sorted(c.items())) for k, c in sorted(out.items())}
    total = Counter(v["outcome"] for v in verdicts)
    n = len(verdicts) or 1
    return {"items": len(verdicts), "outcomes": dict(sorted(total.items())),
            "correct_rate": round(total["correct"] / n, 4),
            "by_category": table("category"), "by_difficulty": table("difficulty"),
            "by_style": table("style"),
            "failure_codes": dict(sorted(Counter(c for v in verdicts if v["outcome"] !=
                                                 "correct" for c in v["codes"]).items()))}


def dumps(item: Mapping[str, Any]) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=False)
