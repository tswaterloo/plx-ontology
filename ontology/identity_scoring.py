"""Weighted identity resolution across systems: record pairs scored Fellegi-Sunter style
(Splink-like blocking keys, field comparison levels, m/u weights) and offered for human
review; nothing here writes a link.

Kinds (``KINDS``): parties (``supplier``, ``customer``, ``person``: a BC contact across
BC books), documents (``purchase_order``, ``invoice``: a sales invoice, ``shipment``) and
``mail_sender`` (a captured mail's sender address against Supplier and Customer records).
A *system* is a record's binding and audience unit, so two BC books are two systems;
pairs are only formed across systems.

* Read permission-first (``Scope``): only records in an audience the reader may read,
  whose current version carries only markings the reader holds and whose item ACL (a
  mail) admits the reader, and only such value rows; every count, m, u and candidate is
  computed over that view, so a reader never learns of a withheld record.
* Identifiers come from the schema, never from a list here: a document kind compares one
  feature per ``identifier_kind`` its object type's properties declare (``resolved``),
  over exactly that kind's properties, so two kinds (a ``petra_po`` and a
  ``bc_document_no``) never meet in one feature. Values are keyed by
  ``identifiers.identity_key``; a kind whose identity is scoped (``customer_po``: per
  customer) is keyed within the record's customer, the identity component (visible
  identity-tier ``Customer.same_record_as``) its visible identity-tier N:1 link reaches;
  a record with no such customer, or several, has no key. A text property no grammar
  types (``invoice_number``) is compared as stored.
* Blocking: name first token and 4-character prefix, identifier identity and lead keys,
  an e-mail address, a phone number's last 7 digits, a document date, a sender domain and
  its label. A block joining more than ``MAX_BLOCK_PAIRS`` pairs is skipped
  and counted (``blocks_capped``); past ``MAX_PAIRS`` pairs no block is added.
* Comparison levels: names (casefold, accents and legal suffixes dropped; Jaro-Winkler)
  ``exact``/``close``/``near``/``differ``; identifiers ``exact`` (one identity key) /
  ``lead`` (one ``identifiers.lead_key`` in one scope: weighs a candidate, never makes an
  identity) / ``differ``;
  dates ``same_day``/``within_7``/``differ``; money (Decimal) ``equal``/``within_1pct``/
  ``differ`` and unknown unless both amounts are in one stated currency.
* m (probability of a level among true matches) comes only from identity-tier links of
  the kind's link type (``verified``: a stored key; ``reviewed``: a person), with
  ``M_PSEUDO`` pseudo-counts toward a declared prior; a feature a verified link's stored
  key joins on (the schema ``same_record`` pair) is not counted for that link. u
  (probability among non-matches) is label-free: exact levels from the two systems' value
  frequencies (per value: term-frequency adjusted), fuzzy levels from the blocked pairs.
  The prior odds of a system pair are its identity links over its pairs, at least half
  the smaller system matching (``prior``).
* Bands: ``identity`` (one identity component already), ``rejected`` (a person's
  ``contradicted`` link), ``review`` (probability at least ``REVIEW_PROBABILITY``) and
  ``below_review``. No probability merges anything: a candidate in ``review`` carries the
  ``plx.review`` ``accept_link`` payload (``review``) a person records (the console's
  confirm, ``operating.human_links``), and ``ontology.reviews.link_step`` projects it as a
  ``reviewed`` link in the next build. A one-to-one link type an end already holds in the
  other end's system is ``conflict`` and carries no payload.
* ``evaluation``: two folds by SHA-256 of the pair; each fold's identity pairs are scored
  with m from the other fold only (``train_eval_overlap`` 0). Precision counts reviewed
  rejections as negatives, and separately also ``closure`` negatives (another record of
  the same system blocked with an end of an identity pair); coverage is the share of
  identity pairs blocking reaches at or above the review threshold. Record kinds also
  split this, with their pairs and bands, by the source pair of the two systems
  (``by_sources``: ``bc+portal_api`` is the Portal-BC sample).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field, replace
import datetime as dt
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
import re
import sqlite3
import time
import unicodedata
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import identifiers, store_ddl
from ontology.mentions import (TWIN_SUPPLIER_FEATURES, mail_domain, registrable_domain,
                               shared_mail_domain, website_domain)
from ontology.store import withhold_currency

REVIEW_PROBABILITY = 0.5
IDENTITY_TIERS = ("verified", "reviewed")
M_PSEUDO = 4.0          # pseudo-counts of the m prior beside the identity pairs counted
U_PSEUDO = 1.0          # pseudo-count of the u prior beside the system pair's pairs
MAX_BLOCK_PAIRS = 5000
MAX_PAIRS = 400_000
CANDIDATE_LIMIT = 200
_CHUNK = 5000
_LEGAL = frozenset("inc incorporated ltd limited llc llp lp co corp corporation company gmbh "
                   "sa srl sas bv ag plc pty ulc the".split())


class IdentityScoringError(ValueError):
    """A request the scorer refuses (its code is the message)."""


# --------------------------------------------------------------------------- comparators

def normalize_name(text: Any) -> str:
    """Casefold, accents stripped, ``&`` read as ``and``, punctuation dropped and legal
    suffixes removed (``Northwind Chemical Co.`` -> ``northwind chemical``)."""
    if not isinstance(text, str):
        return ""
    folded = "".join(c for c in unicodedata.normalize("NFKD", text)
                     if not unicodedata.combining(c)).casefold().replace("&", " and ")
    tokens = re.findall(r"[^\W_]+", folded)
    kept = [t for t in tokens if t not in _LEGAL]
    return " ".join(kept or tokens)


def jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0 if a else 0.0
    if not a or not b:
        return 0.0
    reach = max(max(len(a), len(b)) // 2 - 1, 0)
    taken = [False] * len(b)
    hits_a = []
    for i, ch in enumerate(a):
        for j in range(max(0, i - reach), min(len(b), i + reach + 1)):
            if not taken[j] and b[j] == ch:
                taken[j] = True
                hits_a.append(ch)
                break
    if not hits_a:
        return 0.0
    hits_b = [b[j] for j in range(len(b)) if taken[j]]
    swaps = sum(x != y for x, y in zip(hits_a, hits_b)) / 2
    m = len(hits_a)
    jaro = (m / len(a) + m / len(b) + (m - swaps) / m) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def _token_match(x: str, y: str) -> bool:
    """One token for another: equal, or a long word misspelt (never a number: ``1001``
    and ``1002`` are two names)."""
    return x == y or (min(len(x), len(y)) >= 4 and not re.search(r"\d", x + y)
                      and jaro_winkler(x, y) >= 0.9)


def _name_level(a: str, b: str) -> str:
    if a == b:
        return "exact"
    ta, tb = a.split(), b.split()
    if len(ta) == len(tb) and all(_token_match(x, y) for x, y in zip(sorted(ta), sorted(tb))):
        return "close"
    if re.sub(r"\D", "", a) == re.sub(r"\D", "", b) and jaro_winkler(a, b) >= 0.92:
        return "close"
    shared = sum(1 for x in set(ta) if any(_token_match(x, y) for y in set(tb)))
    union = len(set(ta)) + len(set(tb)) - shared
    return "near" if union and shared / union >= 0.5 else "differ"


def _best(levels: Sequence[str], found: Iterable[str]) -> str | None:
    best = None
    for level in found:
        if best is None or levels.index(level) < levels.index(best):
            best = level
    return best


def _shared(left: Sequence[Sequence[Any]], right: Sequence[Sequence[Any]]) -> int:
    """Pairs (one left, one right record) sharing a value, summed per value."""
    fl = Counter(v for values in left for v in set(values))
    fr = Counter(v for values in right for v in set(values))
    return sum(n * fr[v] for v, n in fl.items() if v in fr)


class Comparator:
    """One field comparison: ``prepare`` -> a tuple of comparable values (None when the
    record has none), ``level`` of two prepared values, block ``keys``, the u counts of the
    levels value frequencies decide (``counts``; the rest are counted over blocked pairs,
    ``counted``) and the value a level's u is term-frequency adjusted on (``tf``)."""
    levels: tuple[str, ...] = ()
    counted: tuple[str, ...] = ()
    m_prior: Mapping[str, float] = {}
    u_prior: Mapping[str, float] = {}

    def prepare(self, rows: Sequence[Mapping[str, Any]]) -> tuple | None:
        raise NotImplementedError

    def level(self, a: tuple, b: tuple) -> str | None:
        raise NotImplementedError

    def keys(self, value: tuple) -> Iterable[str]:
        return ()

    def counts(self, left: Sequence[tuple], right: Sequence[tuple]) -> dict[str, int]:
        return {}

    def tf(self, a: tuple, b: tuple, level: str) -> Sequence[Any]:
        return ()


class Name(Comparator):
    levels = ("exact", "close", "near", "differ")
    counted = ("close", "near")
    m_prior = {"exact": 0.70, "close": 0.15, "near": 0.10, "differ": 0.05}
    u_prior = {"exact": 0.001, "close": 0.004, "near": 0.02, "differ": 0.975}

    def prepare(self, rows):
        names = sorted({n for n in (normalize_name(r["value"]) for r in rows) if n})
        return tuple(names) or None

    def level(self, a, b):
        return _best(self.levels, (_name_level(x, y) for x in a for y in b))

    def keys(self, value):
        for name in value:
            yield "t:" + name.split()[0]
            collapsed = name.replace(" ", "")
            if len(collapsed) >= 4:
                yield "p:" + collapsed[:4]

    def counts(self, left, right):
        return {"exact": _shared(left, right)}

    def tf(self, a, b, level):
        return sorted(set(a) & set(b)) if level == "exact" else ()


class Exact(Comparator):
    """Equal after ``normalize`` (casefold by default)."""
    levels = ("agree", "differ")
    m_prior = {"agree": 0.90, "differ": 0.10}
    u_prior = {"agree": 0.05, "differ": 0.95}

    def __init__(self, normalize: Callable[[Any], str | None] | None = None,
                 block: Callable[[str], str | None] | None = None) -> None:
        self.normalize = normalize or (lambda v: v.strip().casefold() or None
                                       if isinstance(v, str) else None)
        self.block = block

    def prepare(self, rows):
        values = sorted({n for n in (self.normalize(r["value"]) for r in rows) if n})
        return tuple(values) or None

    def level(self, a, b):
        return "agree" if set(a) & set(b) else "differ"

    def keys(self, value):
        if self.block is not None:
            for v in value:
                key = self.block(v)
                if key:
                    yield key

    def counts(self, left, right):
        return {"agree": _shared(left, right)}

    def tf(self, a, b, level):
        return sorted(set(a) & set(b)) if level == "agree" else ()


def _digits(value: Any) -> str | None:
    text = re.sub(r"\D", "", value) if isinstance(value, str) else ""
    return text[-10:] if len(text) >= 7 else None


class Identifier(Comparator):
    """The values of one identifier kind (``ontology.identifiers``), never of two: ``exact``
    (one ``identity_key``: the grammar's normalised value, within the record's scope when
    the kind's identity is scoped) or ``lead`` (one ``lead_key`` within that same scope:
    ``PO-100777`` and ``100777``; a lead weighs a review candidate, never an identity)."""
    levels = ("exact", "lead", "differ")
    m_prior = {"exact": 0.80, "lead": 0.10, "differ": 0.10}
    u_prior = {"exact": 0.001, "lead": 0.005, "differ": 0.994}

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.scoped = bool(identifiers.kind_spec(kind).get("identity_scope"))

    def prepare(self, rows, scope=None):
        """((identity key, lead key or None), ...) of ``rows``; None without a value or,
        for a scoped kind, without the record's one scope (no scope names no identity)."""
        if self.scoped and scope is None:
            return None
        found = set()
        for row in rows:
            key = identifiers.identity_key(self.kind, row["value"], scope)
            if key is None:
                continue
            lead = identifiers.lead_key(self.kind, row["value"])
            found.add((key, key[:-1] + (lead,) if lead else None))
        return tuple(sorted(found, key=repr)) or None

    def level(self, a, b):
        if {k for k, _ in a} & {k for k, _ in b}:
            return "exact"
        leads = {lead for _, lead in a} - {None}
        return "lead" if leads & {lead for _, lead in b} else "differ"

    def keys(self, value):
        for key, lead in value:
            yield "v:" + "\x1f".join(key)
            if lead:
                yield "l:" + "\x1f".join(lead)

    def counts(self, left, right):
        exact = _shared([[k for k, _ in v] for v in left], [[k for k, _ in v] for v in right])
        leads = _shared([[x for _, x in v if x] for v in left],
                        [[x for _, x in v if x] for v in right])
        return {"exact": exact, "lead": max(leads - exact, 0)}

    def tf(self, a, b, level):
        return sorted({k for k, _ in a} & {k for k, _ in b}) if level == "exact" else ()


class Date(Comparator):
    levels = ("same_day", "within_7", "differ")
    m_prior = {"same_day": 0.70, "within_7": 0.20, "differ": 0.10}
    u_prior = {"same_day": 0.01, "within_7": 0.05, "differ": 0.94}

    def __init__(self, block: bool = False) -> None:
        self.block = block

    def prepare(self, rows):
        days = set()
        for row in rows:
            try:
                days.add(dt.date.fromisoformat(str(row["value"])[:10]).toordinal())
            except ValueError:
                continue
        return tuple(sorted(days)) or None

    def level(self, a, b):
        gap = min(abs(x - y) for x in a for y in b)
        return "same_day" if gap == 0 else "within_7" if gap <= 7 else "differ"

    def keys(self, value):
        return ("d:%d" % day for day in value) if self.block else ()

    def counts(self, left, right):
        days = sorted(v[0] for v in right)
        same = near = 0
        for v in left:
            equal = bisect_right(days, v[0]) - bisect_left(days, v[0])
            same += equal
            near += bisect_right(days, v[0] + 7) - bisect_left(days, v[0] - 7) - equal
        return {"same_day": same, "within_7": near}


class Money(Comparator):
    """Decimal amounts compared only within one stated currency (else unknown)."""
    levels = ("equal", "within_1pct", "differ")
    m_prior = {"equal": 0.70, "within_1pct": 0.15, "differ": 0.15}
    u_prior = {"equal": 0.001, "within_1pct": 0.01, "differ": 0.989}

    def prepare(self, rows):
        out = set()
        for row in rows:
            currency = row.get("currency")
            try:
                amount = Decimal(str(row["value"]))
            except (InvalidOperation, ValueError):
                continue
            if isinstance(currency, str) and currency.strip() and amount.is_finite():
                out.add((currency.strip().upper(), amount))
        return tuple(sorted(out)) or None

    def level(self, a, b):
        found = []
        for ca, x in a:
            for cb, y in b:
                if ca == cb:
                    found.append("equal" if x == y else "within_1pct"
                                 if abs(x - y) * 100 <= max(abs(x), abs(y)) else "differ")
        return _best(self.levels, found)

    def counts(self, left, right):
        amounts: dict[str, list[Decimal]] = {}
        for v in right:
            amounts.setdefault(v[0][0], []).append(v[0][1])
        for values in amounts.values():
            values.sort()
        equal = near = 0
        for v in left:
            currency, x = v[0]
            values = amounts.get(currency, [])
            same = bisect_right(values, x) - bisect_left(values, x)
            equal += same
            tolerance = abs(x) / 100
            near += bisect_right(values, x + tolerance) - bisect_left(values, x - tolerance) - same
        return {"equal": equal, "within_1pct": near}


class Label(Comparator):
    """A sender domain's label against a party name without spaces (``northwindchem``
    and ``northwindchemical``: ``similar``)."""
    levels = ("exact", "similar", "differ")
    counted = ("similar",)
    m_prior = {"exact": 0.45, "similar": 0.35, "differ": 0.20}
    u_prior = {"exact": 0.001, "similar": 0.005, "differ": 0.994}

    def prepare(self, rows):
        values = sorted({v for v in (r["value"] for r in rows) if isinstance(v, str)
                         and len(v) >= 3})
        return tuple(values) or None

    def level(self, a, b):
        def one(x: str, y: str) -> str:
            if x == y:
                return "exact"
            short, other = sorted((x, y), key=len)
            if len(short) >= 4 and other.startswith(short) or jaro_winkler(x, y) >= 0.9:
                return "similar"
            return "differ"
        return _best(self.levels, (one(x, y) for x in a for y in b))

    def keys(self, value):
        return ("l:" + v[:4] for v in value if len(v) >= 4)

    def counts(self, left, right):
        return {"exact": _shared(left, right)}

    def tf(self, a, b, level):
        return sorted(set(a) & set(b)) if level == "exact" else ()


class ContactName(Comparator):
    """A BC contact holding the sender address, its company name against the party's
    name: ``agree`` (exact or close) or ``differ``; unknown with no such contact."""
    levels = ("agree", "differ")
    counted = ("agree",)
    m_prior = {"agree": 0.90, "differ": 0.10}
    u_prior = {"agree": 0.001, "differ": 0.999}

    def prepare(self, rows):
        return Name().prepare(rows)

    def level(self, a, b):
        found = Name().level(a, b)
        return None if found is None else "agree" if found in ("exact", "close") else "differ"

    def keys(self, value):
        return Name().keys(value)


# --------------------------------------------------------------------------- kinds

@dataclass(frozen=True)
class Feature:
    name: str
    comparator: Comparator
    properties: tuple[str, ...] = ()           # the record's (a party's) properties
    left: tuple[str, ...] | None = None         # mail_sender: the sender unit's field
    block: bool = True
    identifier_kind: str | None = None          # resolved: the one kind its properties carry
    scope: tuple[str, str] | None = None        # resolved: (party link, party identity link)


@dataclass(frozen=True)
class Kind:
    """A record kind's comparisons. ``KINDS`` names no identifier property: a kind with
    ``identifiers`` compares one feature per identifier kind its object type's schema
    properties declare (``resolved``), blocking on all but the ``unblocked`` kinds (a
    party code many records share)."""
    name: str
    object_type: str
    link_type: str | None
    features: tuple[Feature, ...]
    targets: Mapping[str, str] = field(default_factory=dict)   # mail: party type -> link
    identifiers: bool = False
    unblocked: frozenset[str] = frozenset()


_EMAIL = Exact(block=lambda v: "e:" + v)
KINDS: dict[str, Kind] = {k.name: k for k in (
    Kind("supplier", "Supplier", "Supplier.same_record_as",
         (Feature("name", Name(), ("name",)),
          Feature("currency", Exact(lambda v: v.strip().upper() or None
                                    if isinstance(v, str) else None), ("currency",)))),
    Kind("customer", "Customer", "Customer.same_record_as",
         (Feature("name", Name(), ("name",)),
          Feature("currency", Exact(lambda v: v.strip().upper() or None
                                    if isinstance(v, str) else None), ("currency",)))),
    Kind("person", "Contact", "Contact.same_record_as",
         (Feature("name", Name(), ("display_name",)),
          Feature("email", _EMAIL, ("email",)),
          Feature("phone", Exact(_digits, block=lambda v: "f:" + v[-7:]), ("phone_number",)),
          Feature("company", Name(), ("company_name",), block=False))),
    Kind("purchase_order", "PurchaseOrder", "PurchaseOrder.same_record_as",
         (Feature("date", Date(block=True), ("order_date",)),
          Feature("total", Money(), ("total_cost",), block=False)),
         identifiers=True, unblocked=frozenset({"supplier_code"})),
    # ``invoice_number`` is text (BC No. beside Sage invnumber, no one grammar): compared
    # as stored, never as an identifier kind.
    Kind("invoice", "SalesInvoice", "SalesInvoice.same_record_as",
         (Feature("invoice_text", Exact(block=lambda v: "n:" + v), ("invoice_number",)),
          Feature("date", Date(block=True), ("invoice_date",)),
          Feature("total", Money(), ("total_amount",), block=False)),
         identifiers=True, unblocked=frozenset({"customer_erp_code"})),
    Kind("shipment", "Shipment", "Shipment.same_record_as",
         (Feature("date", Date(block=True), ("ship_date",)),),
         identifiers=True),
    Kind("mail_sender", "Email", None,
         (Feature("domain", Exact(block=lambda v: "w:" + v), ("website",), left=("domain",)),
          Feature("domain_name", Label(), ("collapsed",), left=("label",)),
          Feature("contact", ContactName(), ("name",), left=("contact",))),
         {"Supplier": "Email.sent_by_supplier", "Customer": "Email.sent_by_customer"}),
)}
PARTY_KINDS = ("supplier", "customer", "person")
DOCUMENT_KINDS = ("purchase_order", "invoice", "shipment")


def resolved(schema: Any, name: str) -> Kind:
    """``KINDS[name]`` with its identifier features read from ``schema``: one per
    ``identifier_kind`` the object type's properties carry, in schema order, over exactly
    the properties of that kind (a ``petra_po`` and a ``bc_document_no`` never meet in one
    feature). A kind whose identity is scoped (``identity_scope``: ``customer_po`` per
    customer) is compared only within the record's one party of that scope's type, reached
    by the object type's one N:1 schema link to it; with no such link the feature is not
    compared at all. An unknown kind name or identifier kind fails closed."""
    kind = KINDS.get(name)
    if kind is None:
        raise IdentityScoringError("identity_kind_unknown")
    if not kind.identifiers:
        return kind
    by_kind: dict[str, list[str]] = {}
    for prop in schema.object_types.get(kind.object_type, {}).get("properties", ()):
        if prop.get("identifier_kind"):
            by_kind.setdefault(prop["identifier_kind"], []).append(prop["name"])
    features = []
    for ident, props in by_kind.items():
        if ident not in identifiers.kinds():
            raise IdentityScoringError("identifier_kind_unknown")
        comparator = Identifier(ident)
        scope = _party_scope(schema, kind, ident) if comparator.scoped else None
        if comparator.scoped and scope is None:
            continue
        features.append(Feature(ident, comparator, tuple(props),
                                block=ident not in kind.unblocked, identifier_kind=ident,
                                scope=scope))
    names = [f.name for f in features] + [f.name for f in kind.features]
    if len(names) != len(set(names)):
        raise IdentityScoringError("identity_feature_name_clash")
    return replace(kind, features=tuple(features) + kind.features)


def _party_scope(schema: Any, kind: Kind, ident: str) -> tuple[str, str] | None:
    """(the object type's one N:1 link to the scope's party type, that party kind's
    identity link type), or None when the scope names no party kind or no single link."""
    party = KINDS.get(identifiers.kind_spec(ident)["identity_scope"].get("party", ""))
    if party is None or party.name not in PARTY_KINDS or not party.link_type:
        return None
    links = [name for name, link in schema.link_types.items()
             if link.get("from") == kind.object_type and link.get("to") == party.object_type
             and link.get("cardinality") == "N:1"]
    return (links[0], party.link_type) if len(links) == 1 else None


# --------------------------------------------------------------------------- reading

@dataclass(frozen=True)
class Scope:
    """What one reader may see, decided before any read: audience units, the canonical
    marking sets it holds and the item ACL check of a record version's handle."""
    audiences: tuple[str, ...]
    marks: tuple[str, ...]
    item_allowed: Callable[[str], bool] = lambda handle: True

    @classmethod
    def of_tools(cls, tools: Any, actor: Any) -> "Scope":
        """The scope ``operating.ontology_tools.OntologyTools`` serves ``actor`` with
        (its policy now, its item gate)."""
        policy = tools.policy
        held = policy.held_markings(actor)
        return cls(tuple(k for k in tools.store.audience_keys() if policy.may_read(actor, k)),
                   tuple(s for s in store_ddl.MARKING_SETS if set(json.loads(s)) <= held),
                   lambda handle: tools._item_allowed(actor, handle))

    def params(self) -> dict[str, str]:
        return {"aud": json.dumps(list(self.audiences)), "marks": json.dumps(list(self.marks))}


@dataclass
class Record:
    object_id: str
    object_type: str
    system: tuple[str, str]
    handle: str
    digest: str
    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


# The scorer's own record visibility. ``ProjectionStore._visible_object`` admits a record
# missing from its source and ``_evidence_visible`` is link-evidence SQL; neither has these
# semantics, so neither is reused. Every read here (records, links, a party's scope) goes
# through ``_records``/``_links`` and this one clause.
_VISIBLE = ("o.audience_key IN (SELECT value FROM json_each(:aud)) "
            "AND o.missing_from_source_since IS NULL "
            "AND v.superseded_at IS NULL AND v.markings IN (SELECT value FROM json_each(:marks))")


def _records(conn: sqlite3.Connection, scope: Scope, object_type: str,
             properties: Sequence[str]) -> dict[str, Record]:
    params = dict(scope.params(), type=object_type)
    out: dict[str, Record] = {}
    for oid, binding, audience, handle, digest in conn.execute(
            "SELECT o.object_id, o.binding_id, o.audience_key, v.l1_handle, v.digest "
            f"FROM objects o JOIN object_versions v ON v.object_id = o.object_id "
            f"WHERE o.object_type = :type AND {_VISIBLE}", params):
        if scope.item_allowed(handle):
            out[oid] = Record(oid, object_type, (binding, audience), handle, digest)
    ids = sorted(out)
    for start in range(0, len(ids), _CHUNK):
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row
        rows = cursor.execute(
            "SELECT object_id, property, value, currency, currency_citation, l1_handle, "
            "field_path FROM object_values WHERE object_id IN (SELECT value FROM "
            "json_each(:ids)) AND property IN (SELECT value FROM json_each(:props)) "
            "AND superseded_at IS NULL AND value IS NOT NULL "
            "AND markings IN (SELECT value FROM json_each(:marks))",
            dict(params, ids=json.dumps(ids[start:start + _CHUNK]),
                 props=json.dumps(list(properties))))
        for row in withhold_currency(rows, lambda cited: _readable(conn, scope, cited)):
            record = out[row["object_id"]]
            record.rows.setdefault(row["property"], []).append(
                {"value": row["value"], "currency": row["currency"],
                 "l1_handle": row["l1_handle"], "field_path": row["field_path"]})
    return out


def _readable(conn: sqlite3.Connection, scope: Scope, ids: Sequence[str]) -> list[str]:
    rows = conn.execute(
        "SELECT o.object_id, v.l1_handle FROM objects o JOIN object_versions v "
        f"ON v.object_id = o.object_id WHERE o.object_id IN (SELECT value FROM "
        f"json_each(:ids)) AND {_VISIBLE}", dict(scope.params(), ids=json.dumps(list(ids))))
    return [oid for oid, handle in rows if scope.item_allowed(handle)]


def _links(conn: sqlite3.Connection, scope: Scope, link_types: Sequence[str],
           visible: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    """``(link type, from, to, tier)`` of the current identity-tier and contradicted links
    of ``link_types`` the reader may see: both ends visible, the link's markings held
    and every record it cites readable."""
    rows = conn.execute(
        "SELECT l.link_type, l.from_object, l.to_object, l.tier, l.evidence_handles "
        "FROM links l WHERE l.link_type IN (SELECT value FROM json_each(:types)) "
        "AND l.superseded_at IS NULL AND l.tier IN ('verified', 'reviewed', 'contradicted') "
        "AND l.markings IN (SELECT value FROM json_each(:marks))",
        dict(scope.params(), types=json.dumps(list(link_types)))).fetchall()
    out = []
    for link_type, a, b, tier, handles in rows:
        if a not in visible or b not in visible:
            continue
        cited = json.loads(handles or "[]")
        if cited and len(_cited_readable(conn, scope, cited)) < len(set(cited)):
            continue
        out.append((link_type, a, b, tier))
    return out


def _cited_readable(conn: sqlite3.Connection, scope: Scope, handles: Sequence[str]) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT v.l1_handle FROM object_versions v JOIN objects o "
        "ON o.object_id = v.object_id WHERE v.l1_handle IN (SELECT value FROM "
        "json_each(:h)) AND o.audience_key IN (SELECT value FROM json_each(:aud)) "
        "AND v.markings IN (SELECT value FROM json_each(:marks))",
        dict(scope.params(), h=json.dumps(sorted(set(handles)))))
    return {handle for (handle,) in rows if scope.item_allowed(handle)}


def _key_properties(schema: Any, link_type: str, object_type: str
                    ) -> dict[frozenset[str], set[str]]:
    """Binding pair -> the properties its ``same_record`` key joins on (a verified link's
    agreement on them is the key, not evidence the scorer may learn from)."""
    sources = {(s["binding"], s["path"]): p["name"]
               for p in schema.object_types.get(object_type, {}).get("properties", ())
               for s in p.get("sources", ())}
    out: dict[frozenset[str], set[str]] = {}
    for binding in schema.link_types.get(link_type, {}).get("bindings", ()):
        if binding.get("kind") != "same_record":
            continue
        pair = frozenset((binding.get("from_binding"), binding.get("to_binding")))
        for item in binding.get("pairs", ()):
            for end, side in (("from_binding", "from"), ("to_binding", "to")):
                prop = sources.get((binding.get(end), item.get(side)))
                if prop:
                    out.setdefault(pair, set()).add(prop)
    return out


# --------------------------------------------------------------------------- scoring

def _pair_fold(a: str, b: str) -> int:
    return int(sha256(f"{a}\x1f{b}".encode("utf-8")).hexdigest()[:8], 16) % 2


class _Groups:
    """Units (records or senders) by side, the comparisons between sides, blocking, u and
    the prior per side pair."""

    def __init__(self, units: Mapping[str, Any], side_of: Callable[[str], Any],
                 side_pairs: Sequence[tuple[Any, Any]], features: Sequence[Feature],
                 prepared: Mapping[str, Mapping[str, tuple | None]]) -> None:
        self.features, self.prepared = features, prepared
        self.sides: dict[Any, list[str]] = {}
        for uid in sorted(units):
            self.sides.setdefault(side_of(uid), []).append(uid)
        self.side_of = {uid: side for side, ids in self.sides.items() for uid in ids}
        self.side_pairs = [p for p in side_pairs if p[0] in self.sides and p[1] in self.sides]
        self.blocks_capped, self.pairs_capped = 0, False
        self.pairs: dict[tuple[str, str], tuple[Any, Any]] = {}
        self.u: dict[tuple[Any, Any], dict[str, dict[str, float]]] = {}
        self.freq: dict[tuple[Any, Any], dict[str, tuple[Counter, Counter, int]]] = {}

    def block(self) -> None:
        index: dict[Any, dict[str, list[str]]] = {side: {} for side in self.sides}
        for side, ids in self.sides.items():
            for uid in ids:
                for feature in self.features:
                    value = self.prepared[feature.name].get(uid)
                    if feature.block and value is not None:
                        for key in set(feature.comparator.keys(value)):
                            index[side].setdefault(f"{feature.name}|{key}", []).append(uid)
        blocks = []
        for left, right in self.side_pairs:
            for key in index[left].keys() & index[right].keys():
                blocks.append((len(index[left][key]) * len(index[right][key]), key, left,
                               right))
        for size, key, left, right in sorted(blocks):
            if size > MAX_BLOCK_PAIRS:
                self.blocks_capped += 1
                continue
            if len(self.pairs) + size > MAX_PAIRS:
                self.pairs_capped = True
                break
            for a in index[left][key]:
                for b in index[right][key]:
                    self.pairs.setdefault((a, b), (left, right))

    def estimate_u(self) -> None:
        levels: dict[tuple[Any, Any], dict[str, Counter]] = {}
        for (a, b), sides in self.pairs.items():
            counted = levels.setdefault(sides, {})
            for feature in self.features:
                if not feature.comparator.counted:
                    continue
                pa, pb = self.prepared[feature.name].get(a), self.prepared[feature.name].get(b)
                if pa is not None and pb is not None:
                    level = feature.comparator.level(pa, pb)
                    if level in feature.comparator.counted:
                        counted.setdefault(feature.name, Counter())[level] += 1
        for sides in self.side_pairs:
            left, right = (self.sides[s] for s in sides)
            per: dict[str, dict[str, float]] = {}
            freq: dict[str, tuple[Counter, Counter, int]] = {}
            for feature in self.features:
                cmp, values = feature.comparator, self.prepared[feature.name]
                lv = [values[u] for u in left if values.get(u) is not None]
                rv = [values[u] for u in right if values.get(u) is not None]
                total = len(lv) * len(rv)
                counts = dict(cmp.counts(lv, rv)) if total else {}
                counts.update(levels.get(sides, {}).get(feature.name, {}))
                u = {lvl: (counts.get(lvl, 0) + U_PSEUDO * cmp.u_prior[lvl]) / (total + U_PSEUDO)
                     for lvl in cmp.levels[:-1]}
                u[cmp.levels[-1]] = max(1.0 - sum(u.values()), 1e-6)
                per[feature.name] = u
                freq[feature.name] = (Counter(v for x in lv for v in set(x)),
                                      Counter(v for x in rv for v in set(x)), total)
            self.u[sides], self.freq[sides] = per, freq

    def levels(self, a: str, b: str) -> dict[str, str]:
        out = {}
        for feature in self.features:
            pa, pb = self.prepared[feature.name].get(a), self.prepared[feature.name].get(b)
            if pa is not None and pb is not None:
                level = feature.comparator.level(pa, pb)
                if level is not None:
                    out[feature.name] = level
        return out

    def weigh(self, a: str, b: str, sides: tuple[Any, Any], m: Mapping[str, Mapping[str, float]],
              prior: float) -> tuple[float, float, list[dict[str, Any]]]:
        """``(probability, match weight, features)`` of one pair."""
        weight = prior
        out = []
        for name, level in sorted(self.levels(a, b).items()):
            feature = next(f for f in self.features if f.name == name)
            u = self.u[sides][name][level]
            shared = feature.comparator.tf(self.prepared[name][a], self.prepared[name][b], level)
            if shared:   # term frequency: the commonest shared value's own u
                fl, fr, total = self.freq[sides][name]
                u = max((fl[v] * fr[v] + U_PSEUDO * feature.comparator.u_prior[level])
                        / (total + U_PSEUDO) for v in shared)
            w = math.log2(m[name][level] / u)
            weight += w
            out.append({"feature": name, "level": level, "weight": round(w, 3)})
        probability = 1.0 / (1.0 + 2.0 ** -max(min(weight, 1000.0), -1000.0))
        return probability, weight, out

    def prior(self, sides: tuple[Any, Any], identity_pairs: int) -> tuple[float, dict]:
        nl, nr = len(self.sides[sides[0]]), len(self.sides[sides[1]])
        expected = max(float(identity_pairs), min(nl, nr) / 2.0)
        share = min(expected / (nl * nr), 0.5)
        return math.log2(share / (1 - share)), {"identity_pairs": identity_pairs,
                                                 "assumed_matches": min(nl, nr) / 2.0,
                                                 "pairs": nl * nr}


def _estimate_m(groups: _Groups, positives: Iterable[tuple[str, str]],
                excluded: Callable[[str, str], set[str]]) -> tuple[dict, dict]:
    counts: dict[str, Counter] = {f.name: Counter() for f in groups.features}
    used = 0
    for a, b in positives:
        used += 1
        skip = excluded(a, b)
        for name, level in groups.levels(a, b).items():
            if name not in skip:
                counts[name][level] += 1
    m = {}
    for feature in groups.features:
        cmp, seen = feature.comparator, counts[feature.name]
        n = sum(seen.values())
        m[feature.name] = {lvl: (seen[lvl] + M_PSEUDO * cmp.m_prior[lvl]) / (n + M_PSEUDO)
                           for lvl in cmp.levels}
    return m, {"identity_pairs": used, "pseudo_count": M_PSEUDO,
               "pairs_per_feature": {name: sum(c.values()) for name, c in counts.items()}}


class _Components:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def _evaluate(groups: _Groups, positives: Sequence[tuple[str, str]],
              rejected: Sequence[tuple[str, str]], components: _Components,
              excluded: Callable[[str, str], set[str]],
              pair_sides: Callable[[str, str], tuple[Any, Any]],
              label: Callable[[tuple[Any, Any]], str] | None = None) -> dict[str, Any]:
    """Two-fold evaluation on identity pairs (module doc); with ``label`` (a side pair's
    source pair) also per label (``by_sources``)."""
    positive_set = set(positives)
    closure = set()
    by_end: dict[tuple[str, Any], set[str]] = {}
    for a, b in groups.pairs:
        by_end.setdefault((a, groups.side_of[b]), set()).add(b)
        by_end.setdefault((b, groups.side_of[a]), set()).add(a)
    for a, b in positives:
        for end, other in ((a, b), (b, a)):
            for x in by_end.get((end, groups.side_of[other]), ()):
                if x != other and components.find(x) != components.find(end):
                    closure.add(tuple(sorted((end, x), key=lambda u: (groups.side_of[u], u))))
    closure -= positive_set
    tally: Counter = Counter()   # by (label, count); label None is the whole kind
    overlap = 0

    def add(pair: tuple[str, str], name: str, value: int) -> None:
        tally[(None, name)] += value
        if label is not None:
            tally[(label(pair_sides(*pair)), name)] += value

    for pair in positives:
        add(pair, "identity_pairs", 1)
    for pair in rejected:
        add(pair, "rejected_pairs", 1)
    for fold in (0, 1):
        train = [p for p in positives if _pair_fold(*p) != fold]
        held = [p for p in positives if _pair_fold(*p) == fold]
        overlap += len(set(train) & set(held))
        m, _ = _estimate_m(groups, train, excluded)
        k: Counter = Counter(pair_sides(*p) for p in train)

        def above(pair: tuple[str, str]) -> bool:
            sides = pair_sides(*pair)
            prior, _ = groups.prior(sides, 2 * k[sides])
            return groups.weigh(*pair, sides, m, prior)[0] >= REVIEW_PROBABILITY

        for pair in held:
            blocked = pair in groups.pairs
            add(pair, "blocked", blocked)
            add(pair, "tp", blocked and above(pair))
        for name, negatives in (("fp_rejected", rejected), ("fp_closure", closure)):
            for pair in negatives:
                if _pair_fold(*pair) == fold and pair in groups.pairs:
                    add(pair, name, above(pair))

    def share(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    def metrics(key: str | None) -> dict[str, Any]:
        t = {name: tally[(key, name)] for name in ("identity_pairs", "rejected_pairs",
                                                    "blocked", "tp", "fp_rejected",
                                                    "fp_closure")}
        return {"identity_pairs": t["identity_pairs"], "rejected_pairs": t["rejected_pairs"],
                "true_positives": t["tp"], "false_positives_rejected": t["fp_rejected"],
                "false_positives_closure": t["fp_closure"],
                "precision_reviewed": share(t["tp"], t["tp"] + t["fp_rejected"]),
                "precision_with_closure": share(t["tp"], t["tp"] + t["fp_rejected"]
                                                + t["fp_closure"]),
                "coverage": share(t["tp"], t["identity_pairs"]),
                "blocking_recall": share(t["blocked"], t["identity_pairs"])}

    out = dict(metrics(None), closure_negatives=len(closure), folds=2,
               train_eval_overlap=overlap)
    if label is not None:
        out["by_sources"] = {key: metrics(key) for key in sorted(
            {k for k, _ in tally if k is not None} | {label(s) for s in groups.side_pairs})}
    return out


def _cite(record: Record, prop: str) -> dict[str, str] | None:
    for row in record.rows.get(prop, ()):
        if row["l1_handle"] == record.handle and row["field_path"]:
            return {"l1_handle": record.handle, "digest": record.digest,
                    "field_path": row["field_path"]}
    return None


def score(conn: sqlite3.Connection, schema: Any, scope: Scope, *,
          kinds: Sequence[str] | None = None, limit: int = CANDIDATE_LIMIT,
          budget_seconds: float | None = None, internal_domains: Iterable[str] = (),
          monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Candidates of every kind in ``kinds`` (default all) for the reader ``scope`` names;
    reads only (the connection is held query-only throughout)."""
    names = list(KINDS) if kinds is None else list(kinds)
    unknown = [k for k in names if k not in KINDS]
    if unknown or not names:
        raise IdentityScoringError("identity_kind_unknown")
    started = monotonic()
    report: dict[str, Any] = {"review_probability": REVIEW_PROBABILITY,
                              "budget_exhausted": False, "kinds": {}}
    previous = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only = ON")
    try:
        for name in names:
            if budget_seconds is not None and monotonic() - started > budget_seconds:
                report["budget_exhausted"] = True
                break
            kind = resolved(schema, name)
            report["kinds"][name] = (_score_mail(conn, schema, scope, kind, limit,
                                                 internal_domains)
                                     if name == "mail_sender" else
                                     _score_records(conn, schema, scope, kind, limit))
    finally:
        conn.execute(f"PRAGMA query_only = {int(previous)}")
    report["seconds"] = round(monotonic() - started, 3)
    return report


def _candidates(groups: _Groups, m: Mapping, priors: Mapping, band_of: Callable,
                entry_of: Callable, limit: int, label: Callable[[Any], str] = lambda s: ""
                ) -> tuple[Counter, dict[str, Counter], list[dict[str, Any]]]:
    bands: Counter = Counter()
    by_label: dict[str, Counter] = {}
    review = []
    for (a, b), sides in groups.pairs.items():
        band = band_of(a, b)
        if band is None:
            probability, weight, features = groups.weigh(a, b, sides, m, priors[sides][0])
            band = "review" if probability >= REVIEW_PROBABILITY else "below_review"
            if band == "review":
                review.append((-probability, a, b, weight, features, sides))
        bands[band] += 1
        by_label.setdefault(label(sides), Counter())[band] += 1
    review.sort(key=lambda item: item[:3])
    out = []
    for negative, a, b, weight, features, sides in review[:limit]:
        out.append(dict(entry_of(a, b, features), probability=round(-negative, 4),
                        match_weight=round(weight, 3), band="review",
                        features=features, prior=priors[sides][1]))
    return bands, by_label, out


def _score_records(conn: sqlite3.Connection, schema: Any, scope: Scope, kind: Kind,
                   limit: int) -> dict[str, Any]:
    props = sorted({p for f in kind.features for p in f.properties})
    records = _records(conn, scope, kind.object_type, props)
    declared = schema.link_types.get(kind.link_type)
    one_to_one = (declared or {}).get("cardinality") == "1:1"
    scoped = {f.scope: _party_of(conn, schema, scope, records, *f.scope)
              for f in kind.features if f.scope}

    def prepare(f: Feature, oid: str, record: Record) -> tuple | None:
        rows = [row for p in f.properties for row in record.rows.get(p, ())]
        if f.scope is None:
            return f.comparator.prepare(rows)
        return f.comparator.prepare(rows, scoped[f.scope].get(oid))

    prepared = {f.name: {oid: prepare(f, oid, r) for oid, r in records.items()}
                for f in kind.features}
    systems = sorted({r.system for r in records.values()})
    groups = _Groups(records, lambda oid: records[oid].system,
                     [(s, t) for i, s in enumerate(systems) for t in systems[i + 1:]],
                     kind.features, prepared)

    def ordered(a: str, b: str) -> tuple[str, str]:
        return tuple(sorted((a, b), key=lambda oid: (records[oid].system, oid)))

    components = _Components()
    identity: set[tuple[str, str]] = set()
    tiers: Counter = Counter()
    rejected: set[tuple[str, str]] = set()
    partners: dict[tuple[str, Any], int] = Counter()
    links = _links(conn, scope, [kind.link_type], records) if declared else []
    verified_pairs = set()
    for _, a, b, tier in links:
        if records[a].system == records[b].system:
            continue
        pair = ordered(a, b)
        if tier == "contradicted":
            rejected.add(pair)
            continue
        tiers[tier] += 1
        components.join(a, b)
        if pair not in identity:
            identity.add(pair)
            partners[(a, records[b].system)] += 1
            partners[(b, records[a].system)] += 1
        if tier == "verified":
            verified_pairs.add(pair)
    rejected -= identity
    keys = _key_properties(schema, kind.link_type, kind.object_type) if declared else {}

    def excluded(a: str, b: str) -> set[str]:
        if (a, b) not in verified_pairs:
            return set()
        joined = keys.get(frozenset((records[a].system[0], records[b].system[0])), set())
        return {f.name for f in kind.features if joined & set(f.properties)}

    groups.block()
    groups.estimate_u()
    positives = sorted(identity)
    m, basis = _estimate_m(groups, positives, excluded)
    k = Counter((records[a].system, records[b].system) for a, b in positives)
    priors = {sides: groups.prior(sides, k[sides]) for sides in groups.side_pairs}
    for sides, (weight, detail) in priors.items():
        detail["weight"] = round(weight, 3)

    def band_of(a: str, b: str) -> str | None:
        if components.find(a) == components.find(b):
            return "identity"
        return "rejected" if (a, b) in rejected else None

    def entry_of(a: str, b: str, features: list[dict[str, Any]]) -> dict[str, Any]:
        conflict = bool(partners[(a, records[b].system)] or partners[(b, records[a].system)])
        entry = {"subject": a, "target": b, "link_type": kind.link_type, "conflict": conflict,
                 "review": None}
        if declared is None:
            entry["not_reviewable"] = "link_type_not_declared"
        elif conflict and one_to_one:
            entry["not_reviewable"] = "end_already_linked"
        else:
            cites = _evidence(kind, features, records[a], records[b])
            if cites:
                entry["review"] = {"decision": "accept_link", "subject": a, "target": b,
                                   "link_type": kind.link_type, "evidence": cites}
            else:
                entry["not_reviewable"] = "no_citable_field"
        return entry

    source = {b["id"]: b["source"] for b in
              schema.object_types.get(kind.object_type, {}).get("bindings", ())}

    def label(sides: tuple[Any, Any]) -> str:   # a side pair's source pair: "bc+portal_api"
        return "+".join(sorted(source.get(side[0], "unknown") for side in sides))

    bands, by_label, candidates = _candidates(groups, m, priors, band_of, entry_of, limit,
                                              label)
    evaluation = _evaluate(groups, positives, sorted(rejected), components, excluded,
                           lambda a, b: (records[a].system, records[b].system), label)
    for key, metrics in evaluation["by_sources"].items():
        found = by_label.get(key, Counter())
        metrics.update(pairs_compared=sum(found.values()), bands=dict(found))
    return {"object_type": kind.object_type, "link_type": kind.link_type,
            "reviewable": declared is not None, "records": len(records),
            "systems": len(systems), "pairs_compared": len(groups.pairs),
            "blocks_capped": groups.blocks_capped, "pairs_capped": groups.pairs_capped,
            "identity_links": dict(tiers), "bands": dict(bands),
            "review_total": bands["review"], "candidates": candidates,
            "m": _rounded(m), "m_basis": basis, "evaluation": evaluation}


def _party_of(conn: sqlite3.Connection, schema: Any, scope: Scope,
              records: Mapping[str, Record], party_link: str, identity_link: str
              ) -> dict[str, tuple[str, str]]:
    """Record -> (the party identity space, its party's identity component) for each record
    whose visible identity-tier ``party_link`` links reach exactly one component of the
    party's visible identity-tier ``identity_link`` links (a lead, a contradicted link or
    a record the reader may not read joins nothing). A record with none or several has no
    scope, so its scoped identifier names no identity."""
    party_type = schema.link_types[party_link]["to"]
    parties = _records(conn, scope, party_type, ())
    components = _Components()
    for _, a, b, tier in _links(conn, scope, [identity_link], parties):
        if tier in IDENTITY_TIERS:
            components.join(a, b)
    members: dict[str, list[str]] = {}
    for oid in parties:
        members.setdefault(components.find(oid), []).append(oid)
    canonical = {oid: min(group) for group in members.values() for oid in group}
    reached: dict[str, set[str]] = {}
    for _, a, b, tier in _links(conn, scope, [party_link], {**records, **parties}):
        if tier in IDENTITY_TIERS and a in records and b in parties:
            reached.setdefault(a, set()).add(canonical[b])
    return {oid: (identity_link, next(iter(found))) for oid, found in reached.items()
            if len(found) == 1}


def _evidence(kind: Kind, features: Sequence[Mapping[str, Any]], *ends: Record
              ) -> list[dict[str, str]]:
    """Each end's own field of the strongest agreeing feature both ends can cite."""
    for item in sorted(features, key=lambda f: -f["weight"]):
        if item["weight"] <= 0:
            break
        feature = next(f for f in kind.features if f.name == item["feature"])
        for prop in feature.properties:
            cites = [_cite(end, prop) for end in ends]
            if all(cites):
                return cites
    return []


def _rounded(m: Mapping[str, Mapping[str, float]]) -> dict[str, dict[str, float]]:
    return {name: {lvl: round(p, 4) for lvl, p in levels.items()} for name, levels in m.items()}


# --------------------------------------------------------------------------- mail senders

_ADDRESS = re.compile(r"[^\s<>@,;]+@[^\s<>@,;]+")


def mail_address(text: Any) -> str | None:
    found = _ADDRESS.search(text) if isinstance(text, str) else None
    address = found.group(0).strip(".").casefold() if found else None
    return address if address and mail_domain(address) else None


def _score_mail(conn: sqlite3.Connection, schema: Any, scope: Scope, kind: Kind, limit: int,
                internal_domains: Iterable[str]) -> dict[str, Any]:
    internal = {registrable_domain(d) for d in internal_domains if d}
    emails = _records(conn, scope, "Email", ("sender", "mailbox", "received_at"))
    senders: dict[str, list[Record]] = {}
    skipped = Counter()
    for record in emails.values():
        rows = record.rows.get("sender", ())
        address = mail_address(rows[0]["value"]) if rows else None
        if address is None:
            skipped["no_sender_address"] += 1
            continue
        domain = registrable_domain(mail_domain(address))
        boxes = {registrable_domain(d) for d in (
            mail_domain(r["value"]) for r in record.rows.get("mailbox", ())) if d}
        if domain in internal or domain in boxes:
            skipped["internal_sender"] += 1
            continue
        senders.setdefault("sender:" + address, []).append(record)
    for unit in senders.values():   # latest first
        unit.sort(key=lambda r: (str((r.rows.get("received_at") or [{"value": ""}])[0]["value"]),
                                 r.object_id), reverse=True)
    types = [t for t in kind.targets if schema.link_types.get(kind.targets[t])]
    parties: dict[str, Record] = {}
    for party_type in types:
        parties.update(_records(conn, scope, party_type, ("name", "website")))
    contacts = _records(conn, scope, "Contact", ("email", "company_name"))
    holders: dict[str, list[Record]] = {}
    for contact in contacts.values():
        for row in contact.rows.get("email", ()):
            address = mail_address(row["value"])
            if address:
                holders.setdefault("sender:" + address, []).append(contact)

    prepared: dict[str, dict[str, tuple | None]] = {f.name: {} for f in kind.features}
    for uid in senders:
        domain = registrable_domain(mail_domain(uid[7:]))
        distinctive = not shared_mail_domain(domain)
        label = re.sub(r"[^0-9a-z]", "", domain.split(".")[0])
        prepared["domain"][uid] = (domain,) if distinctive else None
        prepared["domain_name"][uid] = (label,) if distinctive and len(label) >= 3 else None
        prepared["contact"][uid] = ContactName().prepare(
            [row for c in holders.get(uid, ()) for row in c.rows.get("company_name", ())])
    for oid, party in parties.items():
        names = Name().prepare(party.rows.get("name", ()))
        sites = {website_domain(r["value"]) for r in party.rows.get("website", ())} - {None}
        prepared["domain"][oid] = tuple(sorted({registrable_domain(d) for d in sites
                                                if not shared_mail_domain(d)})) or None
        prepared["domain_name"][oid] = tuple(n.replace(" ", "") for n in names) if names else None
        prepared["contact"][oid] = names
    units = {**{uid: None for uid in senders}, **{oid: None for oid in parties}}
    groups = _Groups(units, lambda uid: "sender" if uid in senders else "party",
                     [("sender", "party")], kind.features, prepared)

    link_types = [kind.targets[t] for t in types]
    by_email = {r.object_id: uid for uid, unit in senders.items() for r in unit}
    visible = {**{oid: True for oid in by_email}, **{oid: True for oid in parties}}
    linked: dict[tuple[str, str], set[str]] = {}
    identity, rejected, tiers = set(), set(), Counter()
    for _, email, party, tier in _links(conn, scope, link_types, visible):
        if email not in by_email or party not in parties:
            continue
        pair = (by_email[email], party)
        if tier == "contradicted":
            rejected.add(pair)
            continue
        tiers[tier] += 1
        identity.add(pair)
        linked.setdefault(pair, set()).add(email)
    rejected -= identity
    groups.block()
    groups.estimate_u()
    positives = sorted(identity)
    m, basis = _estimate_m(groups, positives, lambda a, b: set())
    sides = ("sender", "party")
    priors = {sides: groups.prior(sides, len(positives))} if sides in groups.side_pairs else {}
    for weight, detail in priors.values():
        detail["weight"] = round(weight, 3)
    components = _Components()

    def band_of(uid: str, oid: str) -> str | None:
        if (uid, oid) in rejected:
            return "rejected"
        unlinked = [r for r in senders[uid] if r.object_id not in linked.get((uid, oid), ())]
        return "identity" if not unlinked else None

    def entry_of(uid: str, oid: str, features: list[dict[str, Any]]) -> dict[str, Any]:
        party = parties[oid]
        email = next(r for r in senders[uid] if r.object_id not in linked.get((uid, oid), ()))
        link_type = kind.targets[party.object_type]
        cites = [_cite(email, "sender"), _cite(party, "name")]
        agreed = {f["feature"] for f in features if f["weight"] > 0}
        if "contact" in agreed:
            party_names = prepared["contact"][oid] or ()
            for contact in holders.get(uid, ()):
                names = ContactName().prepare(contact.rows.get("company_name", ()))
                if names and ContactName().level(names, party_names) == "agree":
                    cites.append(_cite(contact, "email"))
                    break
        cites = [c for c in cites if c]
        entry = {"subject": email.object_id, "target": oid, "link_type": link_type,
                 "messages": len(senders[uid]),
                 "messages_linked": len(linked.get((uid, oid), ())), "conflict": False,
                 "review": None}
        if len(cites) >= 2:
            entry["review"] = {"decision": "accept_link", "subject": email.object_id,
                               "target": oid, "link_type": link_type, "evidence": cites[:4]}
        else:
            entry["not_reviewable"] = "no_citable_field"
        return entry

    bands, _, candidates = _candidates(groups, m, priors, band_of, entry_of, limit)
    return {"object_type": "Email", "link_type": link_types, "reviewable": bool(link_types),
            "records": len(emails), "senders": len(senders), "parties": len(parties),
            "senders_skipped": dict(skipped), "pairs_compared": len(groups.pairs),
            "blocks_capped": groups.blocks_capped, "pairs_capped": groups.pairs_capped,
            "identity_links": dict(tiers), "bands": dict(bands),
            "review_total": bands["review"], "candidates": candidates,
            "m": _rounded(m), "m_basis": basis,
            "evaluation": _evaluate(groups, positives, sorted(rejected), components,
                                    lambda a, b: set(), lambda a, b: sides)}


# --------------------------------------------------------------- firm purchase-order twins
#
# A Portal-BC purchase-order pair the schema joins only on the PO number text (the
# ``text_match`` binding of ``PurchaseOrder.same_record_as``, a ``lead`` by its ceiling) is
# raised to a firm twin only by corroboration recorded with its evidence: a separate
# ``same_record`` link of tier ``corroborated`` beside the lead (which stays as it is; the
# store keeps ``text_match`` links at or below lead). It is written when all of these hold
# and nothing conflicts:
#
# * ``po_number``: the two numbers are the same identifier after the binding's normaliser;
# * ``supplier``: the supplier agrees, by name (``normalize_name`` level ``exact`` or
#   ``close``) or by a vendor number the Portal sends to BC as held (``identifiers.
#   sent_form``); a name that differs or a number that differs is a conflict;
# * at least one of ``order_date`` (within ``TWIN_DATE_DAYS`` days) or ``total_cost``
#   (Decimal, one stated currency, within ``TWIN_TOTAL_SHARE``). Line count and item codes
#   are not compared: neither end projects its lines' item codes onto the order.
#
# Never on the number alone. A pair whose end has another same-record link to the other
# system (several candidates), a pair a person rejected (a ``contradicted`` link), a value
# of a lineage that is never authority, a currency stated through another record
# (``currency_citation``: fail closed) or any conflicting feature stays a lead, and the
# identity scorer keeps offering it for a person's ``accept_link`` (it learns from verified
# and reviewed links only). Each feature cites what it read; the link cites every record it
# read (both orders, the supplier, the supplier link's evidence) and carries their
# markings: a reader without a marking never sees it, and a reader who may not read the
# supplier record is shown the lead it rests on (``ontology.mentions.reader_view``, applied
# by ``plx.around`` and ``plx.object`` to every corroborated link).

TWIN_LINK = "PurchaseOrder.same_record_as"
TWIN_SUPPLIER_LINK = "PurchaseOrder.ordered_from"
TWIN_PREFIX = "crb-po-"
TWIN_TIER = "corroborated"
TWIN_DATE_DAYS = 1
TWIN_TOTAL_SHARE = Decimal("0.005")
TWIN_SUPPLIER_KIND = "supplier_code"
# The features that read the supplier record, beside the two orders: a reader who may not
# read it is shown the link as the lead it rests on (``ontology.mentions.reader_view``).
TWIN_CORROBORATING = TWIN_SUPPLIER_FEATURES
_TWIN_PO_PROPS = ("po_number", "order_date", "total_cost", "vendor_number", "vendor_name")
_TWIN_SUPPLIER_PROPS = {"name": "vendor_name", "code": "vendor_number"}
_TWIN_OUTCOMES = ("firm", "ambiguous", "rejected", "already_firm", "lineage_not_authority",
                  "number_not_exact", "supplier_conflict", "supplier_not_agreed",
                  "supplier_ambiguous", "conflict", "no_secondary_agreement")


def twin_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``post_project`` step (``twins``) writing the firm purchase-order twins of the
    generation being built (:func:`corroborate_po_twins`)."""
    return corroborate_po_twins(builder.connection, projector.schema,
                                lambda rows: builder.add("links", rows),
                                normalizer=getattr(projector, "normalizer", None))


def corroborate_po_twins(conn: sqlite3.Connection, schema: Any,
                         add: Callable[[list[dict[str, Any]]], int], *,
                         normalizer: Callable[[str, Any], str | None] | None = None,
                         replace: bool = True) -> dict[str, Any]:
    """Write a ``corroborated`` ``same_record`` link (id ``crb-po-<sha256 of the lead's
    id>``) beside each current number-text lead of ``TWIN_LINK`` that the rules above
    corroborate, after removing the ones a previous build wrote (an incremental build
    seeds projected link kinds forward). Returns counts per outcome, the outcome of each
    lead (``outcomes``: lead link id -> code) and ``link_tiers``. ``replace=False``
    judges without removing anything (a report over a read-only connection; ``add``
    then only counts)."""
    from ontology import identifiers
    from ontology.resolve import NEVER_AUTHORITY, _part, binding_spec, identifiers_agree

    if normalizer is None:
        normalizer = identifiers.normalize
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        if replace:
            conn.execute("DELETE FROM links WHERE substr(link_id, 1, ?) = ?",
                         (len(TWIN_PREFIX), TWIN_PREFIX))
        link = _part(schema, "link_types").get(TWIN_LINK) or {}
        counts = Counter({code: 0 for code in _TWIN_OUTCOMES})
        outcomes: dict[str, str] = {}
        rows: list[dict[str, Any]] = []
        for binding in link.get("bindings", ()):
            if binding.get("kind") != "text_match":
                continue
            kind = next((p.get("identifier_kind") for p in binding.get("pairs", ())
                         if p.get("identifier_kind")), None)
            ends = (binding_spec(schema, binding["from_binding"]),
                    binding_spec(schema, binding["to_binding"]))
            if kind is None or None in ends:
                continue
            twin = _Twin(conn, schema, kind, ends, normalizer, identifiers.sent_form,
                         identifiers_agree, NEVER_AUTHORITY)
            for lead in conn.execute(
                    "SELECT * FROM links WHERE link_type = ? AND binding_kind = 'text_match' "
                    "AND tier = 'lead' AND superseded_at IS NULL ORDER BY link_id",
                    (TWIN_LINK,)).fetchall():
                ids = {r[0]: r[1] for r in conn.execute(
                    "SELECT object_id, binding_id FROM objects WHERE object_id IN (?, ?)",
                    (lead["from_object"], lead["to_object"]))}
                if (ids.get(lead["from_object"]), ids.get(lead["to_object"])) != (
                        binding["from_binding"], binding["to_binding"]):
                    continue
                code, row = twin.judge(lead)
                counts[code] += 1
                outcomes[lead["link_id"]] = code
                if row is not None:
                    rows.append(row)
        written = add(rows) if rows else 0
        return {"counts": dict(counts), "outcomes": outcomes,
                "link_tiers": {TWIN_TIER: written}}
    finally:
        conn.row_factory = previous


class _Twin:
    """The judge of one ``text_match`` binding's leads (see the section comment)."""

    def __init__(self, conn: sqlite3.Connection, schema: Any, kind: str,
                 ends: tuple[Any, Any], normalizer: Callable[[str, Any], str | None],
                 sent_form: Callable[..., str | None], agree: Callable[..., bool],
                 never: frozenset[str]) -> None:
        self.conn, self.schema, self.kind, self.normalizer = conn, schema, kind, normalizer
        self.sent_form, self.agree, self.never = sent_form, agree, never
        self.bindings = (ends[0][1]["id"], ends[1][1]["id"])
        self.systems = (ends[0][1].get("source"), ends[1][1].get("source"))

    # ---- reads
    def _version(self, oid: str) -> Any:
        return self.conn.execute(
            "SELECT l1_handle, digest, lineage, markings, recorded_at FROM object_versions "
            "WHERE object_id = ? AND superseded_at IS NULL", (oid,)).fetchone()

    def _values(self, oid: str, props: Iterable[str]) -> list[Any]:
        return self.conn.execute(
            "SELECT property, value, currency, currency_citation, tax_basis, l1_handle, "
            "field_path, digest, lineage, binding_id, markings, recorded_at FROM object_values "
            "WHERE object_id = ? AND superseded_at IS NULL AND value IS NOT NULL AND property "
            "IN (SELECT value FROM json_each(?)) ORDER BY property, l1_handle, field_path",
            (oid, json.dumps(sorted(props)))).fetchall()

    def _pair_links(self, a: str, b: str) -> list[Any]:
        return self.conn.execute(
            "SELECT tier, binding_kind FROM links WHERE link_type = ? AND superseded_at IS NULL "
            "AND ((from_object = ? AND to_object = ?) OR (from_object = ? AND to_object = ?))",
            (TWIN_LINK, a, b, b, a)).fetchall()

    def _partners(self, oid: str, other_binding: str) -> set[str]:
        """The records of ``other_binding`` a current, not contradicted same-record link
        joins ``oid`` to (either direction, every kind)."""
        return {r[0] for r in self.conn.execute(
            "SELECT CASE WHEN l.from_object = :o THEN l.to_object ELSE l.from_object END "
            "FROM links l JOIN objects x ON x.object_id = CASE WHEN l.from_object = :o "
            "THEN l.to_object ELSE l.from_object END WHERE l.link_type = :t AND "
            "l.superseded_at IS NULL AND l.tier <> 'contradicted' AND "
            "(l.from_object = :o OR l.to_object = :o) AND x.binding_id = :b",
            {"o": oid, "t": TWIN_LINK, "b": other_binding})}

    def _suppliers(self, oid: str) -> list[Any]:
        return self.conn.execute(
            "SELECT to_object, evidence_handles, markings, recorded_at FROM links WHERE "
            "link_type = ? AND from_object = ? AND tier = 'verified' AND superseded_at IS NULL "
            "ORDER BY link_id", (TWIN_SUPPLIER_LINK, oid)).fetchall()

    # ---- the decision
    def judge(self, lead: Any) -> tuple[str, dict[str, Any] | None]:
        a, b = lead["from_object"], lead["to_object"]
        tiers = {r["tier"] for r in self._pair_links(a, b)}
        if "contradicted" in tiers:
            return "rejected", None
        if tiers & set(IDENTITY_TIERS):
            return "already_firm", None
        if self._partners(a, self.bindings[1]) != {b} or self._partners(b, self.bindings[0]) != {a}:
            return "ambiguous", None
        versions = (self._version(a), self._version(b))
        if None in versions or any(v["lineage"] in self.never for v in versions):
            return "lineage_not_authority", None
        sides = []
        for oid in (a, b):
            side = {"po": self._values(oid, _TWIN_PO_PROPS), "supplier": [], "handles": [],
                    "marks": [], "times": []}
            suppliers = self._suppliers(oid)
            if len(suppliers) > 1:
                return "supplier_ambiguous", None
            for link in suppliers:
                version = self._version(link["to_object"])
                if version is None:
                    continue
                side["supplier"] = self._values(link["to_object"], _TWIN_SUPPLIER_PROPS)
                side["handles"] += [version["l1_handle"], *json.loads(link["evidence_handles"])]
                side["marks"] += [version["markings"], link["markings"]]
                side["times"] += [version["recorded_at"], link["recorded_at"]]
            sides.append(side)
        features = [self._number(sides)]
        if not features[0]["agrees"]:
            return "number_not_exact", None
        supplier = [f for f in (self._supplier_name(sides), self._supplier_number(sides)) if f]
        if any(f["agrees"] is False for f in supplier):
            return "supplier_conflict", None
        if not any(f["agrees"] for f in supplier):
            return "supplier_not_agreed", None
        secondary = [f for f in (self._date(sides), self._total(sides)) if f]
        if any(f["agrees"] is False for f in secondary):
            return "conflict", None
        if not any(f["agrees"] for f in secondary):
            return "no_secondary_agreement", None
        # only what agrees is recorded (an unknown total would mark the link finance)
        features += [f for f in supplier + secondary if f["agrees"]]
        return "firm", self._row(lead, versions, sides, features)

    @staticmethod
    def _cite(row: Any) -> dict[str, str]:
        return {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
                "digest": row["digest"], "lineage": row["lineage"]}

    def _usable(self, rows: Iterable[Any], prop: str) -> list[Any]:
        return [r for r in rows if r["property"] == prop and r["lineage"] not in self.never]

    def _number(self, sides: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        left, right = (self._usable(s["po"], "po_number") for s in sides)
        normal = []
        for rows in (left, right):
            found = set()
            for r in rows:
                try:
                    found.add(self.normalizer(self.kind, r["value"]))
                except (KeyError, ValueError):
                    found.add(None)
            normal.append(found)
        agrees = (len(normal[0]) == 1 and normal[0] == normal[1] and None not in normal[0])
        return {"feature": "po_number", "agrees": agrees, "level": "exact" if agrees else "differ",
                "identifier_kind": self.kind, "used": left[:1] + right[:1],
                "citations": [self._cite(r) for r in left[:1] + right[:1]]}

    def _supplier_rows(self, side: Mapping[str, Any], own: str) -> list[Any]:
        """The side's supplier values of PO property ``own`` (``vendor_name`` /
        ``vendor_number``): the order's own and its supplier's (``name`` / ``code``)."""
        theirs = next(k for k, v in _TWIN_SUPPLIER_PROPS.items() if v == own)
        return self._usable(side["po"], own) + self._usable(side["supplier"], theirs)

    def _supplier_name(self, sides: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        left, right = (self._supplier_rows(s, "vendor_name") for s in sides)
        levels = [(_name_level(normalize_name(x["value"]), normalize_name(y["value"])), x, y)
                  for x in left for y in right
                  if normalize_name(x["value"]) and normalize_name(y["value"])]
        if not levels:
            return None
        if any(level == "differ" for level, _x, _y in levels):
            level, x, y = next(t for t in levels if t[0] == "differ")
            return {"feature": TWIN_CORROBORATING[0], "agrees": False, "level": level, "used": [x, y],
                    "citations": [self._cite(x), self._cite(y)]}
        best = next((t for t in levels if t[0] in ("exact", "close")), None)
        level, x, y = best or levels[0]
        return {"feature": TWIN_CORROBORATING[0], "agrees": True if best else None, "level": level,
                "used": [x, y], "citations": [self._cite(x), self._cite(y)]}

    def _sent(self, row: Any, to_system: str | None) -> str | None:
        spec = None
        from ontology.resolve import binding_spec
        found = binding_spec(self.schema, row["binding_id"])
        if found is not None:
            spec = found[1]
        relation = (spec or {}).get("relation", "")
        field_name = row["field_path"][4:] if row["field_path"].startswith("row.") \
            else row["field_path"]
        return self.sent_form(TWIN_SUPPLIER_KIND, relation, field_name, row["value"],
                              to_system or "")

    def _supplier_number(self, sides: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        left, right = (self._supplier_rows(s, "vendor_number") for s in sides)
        pairs = []
        for x in left:
            for y in right:
                # each value as the other system receives it; one never sent is not compared
                sx, sy = self._sent(x, self.systems[1]), self._sent(y, self.systems[0])
                if sx is None or sy is None:
                    continue
                pairs.append((self.agree(TWIN_SUPPLIER_KIND, sx, sy, self.normalizer), x, y))
        if not pairs:
            return None
        agrees, x, y = next((p for p in pairs if not p[0]), pairs[0])
        return {"feature": TWIN_CORROBORATING[1], "agrees": agrees,
                "level": "exact" if agrees else "differ", "identifier_kind": TWIN_SUPPLIER_KIND,
                "used": [x, y], "citations": [self._cite(x), self._cite(y)]}

    def _date(self, sides: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        left, right = (self._usable(s["po"], "order_date") for s in sides)

        def day(row: Any) -> dt.date | None:
            try:
                return dt.date.fromisoformat(str(row["value"])[:10])
            except ValueError:
                return None
        pairs = [(abs((day(x) - day(y)).days), x, y) for x in left for y in right
                 if day(x) is not None and day(y) is not None]
        if not pairs:
            return None
        gap, x, y = max(pairs, key=lambda p: p[0])
        return {"feature": "order_date", "agrees": gap <= TWIN_DATE_DAYS, "days_apart": gap,
                "within_days": TWIN_DATE_DAYS, "used": [x, y],
                "citations": [self._cite(x), self._cite(y)]}

    def _total(self, sides: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        """Totals compared in one stated currency (a currency stated through another
        record is not used). Amounts within ``TWIN_TOTAL_SHARE`` agree when neither tax
        basis is declared different from the other (``unknown`` may be either); amounts
        that differ conflict only on one declared, known tax basis, else are unknown."""
        left, right = ([r for r in self._usable(s["po"], "total_cost")
                        if r["currency"] and r["currency_citation"] is None] for s in sides)
        found = []
        for x in left:
            for y in right:
                if x["currency"] != y["currency"]:
                    continue
                try:
                    ax, ay = Decimal(str(x["value"])), Decimal(str(y["value"]))
                except InvalidOperation:
                    continue
                if not ax.is_finite() or not ay.is_finite() or ax == 0 or ay == 0:
                    continue
                share = abs(ax - ay) / max(abs(ax), abs(ay))
                bx, by = x["tax_basis"] or "unknown", y["tax_basis"] or "unknown"
                if share <= TWIN_TOTAL_SHARE:
                    agrees = True if (bx == by or "unknown" in (bx, by)) else None
                else:
                    agrees = False if (bx == by and bx != "unknown") else None
                found.append((agrees, share, bx, by, x, y))
        if not found:
            return None
        agrees, share, bx, by, x, y = next((f for f in found if f[0] is False),
                                           next((f for f in found if f[0]), found[0]))
        return {"feature": "total_cost", "agrees": agrees, "currency": x["currency"],
                "share_apart": str(share.quantize(Decimal("0.0001"))),
                "within_share": str(TWIN_TOTAL_SHARE), "tax_basis": [bx, by], "used": [x, y],
                "citations": [self._cite(x), self._cite(y)]}

    def _row(self, lead: Any, versions: Sequence[Any], sides: Sequence[Mapping[str, Any]],
             features: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        from ontology.store import canonical, markings_text
        used = [r for f in features for r in f["used"]]
        handles = {v["l1_handle"] for v in versions} | {r["l1_handle"] for r in used} \
            | {h for s in sides for h in s["handles"]} | set(json.loads(lead["evidence_handles"]))
        marks: set[str] = set(json.loads(lead["markings"]))
        for text in [v["markings"] for v in versions] + [r["markings"] for r in used] \
                + [m for s in sides for m in s["marks"]]:
            marks.update(json.loads(text or "[]"))
        times = [lead["recorded_at"], *(v["recorded_at"] for v in versions),
                 *(r["recorded_at"] for r in used), *(t for s in sides for t in s["times"])]
        reason = [{k: v for k, v in f.items() if k != "used"} for f in features]
        reason.append({"feature": "corroborates", "agrees": True, "binding_kind": "text_match",
                       "link_id": lead["link_id"]})
        return {"link_id": TWIN_PREFIX + sha256(lead["link_id"].encode()).hexdigest()[:32],
                "link_type": lead["link_type"], "from_object": lead["from_object"],
                "to_object": lead["to_object"], "tier": TWIN_TIER, "binding_kind": "same_record",
                "link_contract": None, "reason": canonical(reason), "why_not": "[]",
                "evidence_handles": canonical(sorted(handles)), "reviewed_by": None,
                "reviewed_at": None, "review_action_id": None,
                "markings": markings_text(marks), "recorded_at": max(times),
                "superseded_at": None}
