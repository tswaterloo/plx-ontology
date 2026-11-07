"""One purchase order for a mention of a reused order number, when the evidence decides it.

An order number held by more than one order (a BC order and a FileMaker-era Portal order
whose Portal row carries the same number, two Portal orders sharing one) makes every
number-text mention of it a lead to each holder. This module picks the one order a mention
is about only when the evidence is decisive, and says why; otherwise the mention stays
ambiguous (no order is chosen, never a guess).

*Holders*: every purchase order record the identifier index (the one the mention writer
searches) holds the number for under the order number's kind or its alias kinds (``po_number``,
``fm_po_number``, ``sage_po_number``, ...), in any stretch (a superseded row still made
leads), and every record holding it in ``NUMBER_PROPS``. A linked record the holders miss (a
reviewed alias) is its own candidate (``Resolver.resolve_linked``); a linked record that is
no known order makes the mention ambiguous (``candidate_unknown``).

*Orders*: purchase order records joined by a confirming twin link (``TWIN_LINKS`` at
``CONFIRMING``) are one order; a ``lead`` twin link joins nothing (on the live store the
lead twins between colliding numbers are a BC order and an unrelated Portal order).

Evidence, in order:

1. **One order** (``one_order``): every holder of the number is one order.
2. **Supplier** (``supplier``): the mail's counterparty, the parties its external senders
   are (``ontology.sender_parties.Parties.judge``: a stored address, or a domain the
   party's name spells), the message's own sender and its conversation's, is the supplier
   of exactly one candidate order (``PurchaseOrder.ordered_from`` at ``CONFIRMING``; one
   supplier across systems by a confirming ``same_record`` twin or one normalised name).
   When it also falls outside that order's life the evidence conflicts: ambiguous.
3. **Order life** (``order_life``): with no counterparty that decides, the message date
   falls within exactly one candidate's life and every other candidate is out of its life
   on a known date: before its order day less ``START_SLACK_DAYS``, or, closed in every
   system, after its last known day plus ``END_SLACK_DAYS``. A day a bulk share of a
   source's records hold (a sync's or migration's day, ``operating.source_defaults``) is
   not known.

The company context (a BC company) is not used: the store's BC orders come from one
company and carry none (``company_unknown`` is never a reason to pick).

Reasons: ``Resolution.reason`` is one of ``RESOLVED`` (a pick) or ``UNRESOLVED`` (none).
The reader takes what callers may read: the detector passes only cleared messages.
Stdlib only.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
import json
import re
from typing import Any, Iterable, Mapping, Sequence

PO_TYPE = "PurchaseOrder"
SUPPLIER_TYPE = "Supplier"
SUPPLIER_LINK = "PurchaseOrder.ordered_from"
TWIN_LINKS = ("PurchaseOrder.same_record_as", "PurchaseOrder.bc_posted_as")
SUPPLIER_TWIN = "Supplier.same_record_as"
CONFIRMING = ("verified", "reviewed", "corroborated")
NUMBER_PROPS = ("po_number", "fm_po_number")
START_PROPS = ("order_date",)
DAY_PROPS = ("order_date", "expected_date")
STATUS_PROPS = ("status", "fm_status", "bc_status")
CLOSED_WORDS = frozenset({"complete", "completed", "cancel", "cancelled", "canceled",
                          "closed", "received"})
START_SLACK_DAYS = 30    # a number is quoted before its order is entered, not earlier
END_SLACK_DAYS = 365     # a closed order is still talked about this long after its last day
CHUNK = 5000

RESOLVED = ("one_order", "supplier", "order_life")
UNRESOLVED = ("no_candidates", "supplier_out_of_life", "suppliers_tie", "no_evidence",
              "lives_overlap", "life_unknown", "candidate_unknown")
# the identifier kinds an order number is held under in the identifier index the mention
# writer searches (``ontology.mentions._store_candidates``): the order number's kind and the
# kinds it aliases or that alias it (a BC document number, a FileMaker number, a twin's)
NUMBER_KIND = "petra_po"


@dataclass(frozen=True)
class Order:
    """One order across its confirming twins."""
    key: str                      # its least record id
    records: tuple[str, ...]
    suppliers: frozenset          # party keys of its suppliers
    start: date | None            # its earliest known order day
    last: date | None             # its latest known day
    closed: bool                  # closed in every system that states a status

    def life(self, sent: date) -> bool | None:
        """Whether ``sent`` is within the order's life; None when it cannot be told."""
        if self.start is not None and sent < self.start - timedelta(days=START_SLACK_DAYS):
            return False
        if self.closed and self.last is not None \
                and sent > self.last + timedelta(days=END_SLACK_DAYS):
            return False
        if self.start is None and not (self.closed and self.last is not None):
            return None
        return True


@dataclass(frozen=True)
class Mail:
    """What a message tells about its order: its sent day and its counterparty parties."""
    sent: date | None
    parties: frozenset = frozenset()
    handles: tuple[str, ...] = ()   # the messages whose sender named a party


@dataclass(frozen=True)
class Resolution:
    order: Order | None
    reason: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def feature(self) -> dict[str, Any]:
        """The reason feature a link or a count cites (ids and reasons, never values)."""
        return {"feature": "number_disambiguated", "agrees": self.order is not None,
                "reason": self.reason, **dict(self.detail)}


def resolve(orders: Sequence[Order], mail: Mail) -> Resolution:
    """The one order of ``orders`` (every holder of the number) the mention is about, or
    None with why (module docstring)."""
    if not orders:
        return Resolution(None, "no_candidates")
    if len(orders) == 1:
        return Resolution(orders[0], "one_order", {"candidates": 1})
    n = len(orders)
    life = {o.key: (None if mail.sent is None else o.life(mail.sent)) for o in orders}
    if mail.parties:
        fit = [o for o in orders if o.suppliers & mail.parties]
        if len(fit) == 1:
            if life[fit[0].key] is False:
                return Resolution(None, "supplier_out_of_life", {"candidates": n})
            return Resolution(fit[0], "supplier", {"candidates": n})
        if len(fit) > 1:
            orders = fit
            if mail.sent is None:
                return Resolution(None, "suppliers_tie", {"candidates": n})
    if mail.sent is None:
        return Resolution(None, "no_evidence", {"candidates": n})
    inside = [o for o in orders if life[o.key] is True]
    unknown = [o for o in orders if life[o.key] is None]
    if unknown:
        return Resolution(None, "life_unknown", {"candidates": n})
    if len(inside) == 1:
        return Resolution(inside[0], "order_life", {"candidates": n})
    return Resolution(None, "lives_overlap" if inside else "no_evidence", {"candidates": n})


def number_key(raw: Any) -> str:
    """An order number compared across systems (``operating.po_status._number_key``)."""
    text = re.sub(r"[^0-9A-Z]", "", str(raw or "").upper())
    text = re.sub(r"^PO", "", text)
    return text.lstrip("0") or text


def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()[:10]) if raw not in (None, "") else None
    except ValueError:
        return None


def _rows(conn: Any, sql: str, ids: Sequence[str], *extra: Any) -> Iterable[tuple]:
    ids = sorted(set(ids))
    for start in range(0, len(ids), CHUNK):
        yield from conn.execute(sql, (*extra, json.dumps(ids[start:start + CHUNK])))


def _clusters(pairs: Iterable[tuple[str, str]], members: Iterable[str]) -> dict[str, str]:
    parent = {m: m for m in members}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in pairs:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    return {m: find(m) for m in parent}


def _binding(oid: str) -> str:
    parts = oid.split(":", 2)
    return parts[1] if len(parts) > 2 else ""


def read_parties(conn: Any) -> Any:
    """``ontology.sender_parties.Parties`` read through ``object_values`` and
    ``link_rows`` alone (the SQLite and the Postgres store both answer them): every
    supplier's, customer's and contact's stored address or site, a contact's for the party
    a ``verified`` link joins it to, one party across confirming ``same_record`` twins or
    one normalised name."""
    from ontology import sender_parties as sp
    from ontology.identity_scoring import normalize_name

    types = (*sp.PARTY_TYPES, sp.CONTACT_TYPE)
    held: list[tuple[str, str, Any]] = []
    # the party records listed by type first, then their values by id (a scan of every
    # value by property takes minutes on the live store)
    listed = [r[0] for r in conn.execute(
        "SELECT object_id FROM objects WHERE object_type IN (SELECT value FROM json_each(?))",
        (json.dumps(list(types)),))]
    for oid, prop, value in _rows(
            conn, "SELECT object_id, property, value FROM object_values WHERE superseded_at "
                  "IS NULL AND property IN (SELECT value FROM json_each(?)) AND object_id IN "
                  "(SELECT value FROM json_each(?))", listed, json.dumps(list(sp.PROPERTIES))):
        if value not in (None, "") and oid.split(":", 1)[0] in types:
            held.append((oid, prop, value))
    contacts = sorted({oid for oid, prop, _v in held if oid.startswith(sp.CONTACT_TYPE + ":")})
    owners: dict[str, list[str]] = defaultdict(list)
    for frm, to in _rows(conn, "SELECT from_object, to_object FROM link_rows WHERE "
                               "superseded_at IS NULL AND tier = 'verified' AND binding_kind NOT "
                               "IN ('mention', 'same_record') AND from_object IN (SELECT value "
                               "FROM json_each(?))", contacts):
        if to.split(":", 1)[0] in sp.PARTY_TYPES:
            owners[frm].append(to)
    holdings = []
    for oid, prop, value in held:
        if oid.startswith(sp.CONTACT_TYPE + ":"):
            if prop != "email":
                continue
            parties = owners.get(oid, [])
        else:
            parties = [oid]
        for domain, address in sp._domains(prop, value):
            for party in parties:
                holdings.append(sp.Holding(party, party.split(":", 1)[0], prop, domain, address,
                                           "", {}, "[]", None))
    holders = sorted({h.party for h in holdings})
    twins: list[tuple[str, str]] = []
    for frm, to, kind in _rows(conn, "SELECT from_object, to_object, link_type FROM link_rows "
                                     "WHERE superseded_at IS NULL AND tier IN ('verified', "
                                     "'reviewed', 'corroborated') AND from_object IN (SELECT "
                                     "value FROM json_each(?))", holders):
        if kind == frm.split(":", 1)[0] + ".same_record_as":
            twins.append((frm, to))
    roots = _clusters(twins, {*holders, *(t for pair in twins for t in pair)})
    names = {oid: normalize_name(value) for oid, value in _rows(
        conn, "SELECT object_id, value FROM object_values WHERE superseded_at IS NULL AND "
              "property = ? AND object_id IN (SELECT value FROM json_each(?))", holders, "name")
        if normalize_name(value)}
    return sp.Parties(holdings, roots, names, {})


class Resolver:
    """The store's orders by number, read once; ``mail`` and ``resolve`` per mention."""

    def __init__(self, conn: Any, own: Sequence[str], parties: Any = None) -> None:
        from ontology.identity_scoring import normalize_name

        self.own = tuple(own)
        self.normalize = normalize_name
        pos = [r[0] for r in conn.execute(
            "SELECT object_id FROM objects WHERE object_type = ?", (PO_TYPE,))]
        props: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
        for oid, prop, value in _rows(
                conn, "SELECT object_id, property, value FROM object_values WHERE "
                      "superseded_at IS NULL AND property IN (SELECT value FROM json_each(?)) "
                      "AND object_id IN (SELECT value FROM json_each(?))", pos,
                json.dumps([*NUMBER_PROPS, *DAY_PROPS, *STATUS_PROPS])):
            if value not in (None, ""):
                props[oid][prop].append(value)
        twins, supplier_of = [], defaultdict(set)
        for frm, to, kind in _rows(
                conn, "SELECT from_object, to_object, link_type FROM link_rows WHERE "
                      "superseded_at IS NULL AND tier IN (SELECT value FROM json_each(?)) AND "
                      "from_object IN (SELECT value FROM json_each(?))", pos,
                json.dumps(CONFIRMING)):
            if kind in TWIN_LINKS:
                twins.append((frm, to))
            elif kind == SUPPLIER_LINK:
                supplier_of[frm].add(to)
        # a supplier's keys: itself, its confirming twins and its normalised name
        suppliers = sorted({s for found in supplier_of.values() for s in found})
        self.supplier_keys: dict[str, set[str]] = defaultdict(set)
        for oid in suppliers:
            self.supplier_keys[oid].add(oid)
        for frm, to in _rows(
                conn, "SELECT from_object, to_object FROM link_rows WHERE superseded_at IS "
                      "NULL AND link_type = ? AND tier IN ('verified', 'reviewed', "
                      "'corroborated') AND from_object IN (SELECT value FROM json_each(?))",
                suppliers, SUPPLIER_TWIN):
            self.supplier_keys[frm].add(to)
            self.supplier_keys[to].add(frm)
        for oid, name in _rows(
                conn, "SELECT object_id, value FROM object_values WHERE superseded_at IS NULL "
                      "AND property = ? AND object_id IN (SELECT value FROM json_each(?))",
                suppliers, "name"):
            if normalize_name(name):
                self.supplier_keys[oid].add("name:" + normalize_name(name))
        # a day a bulk share of one source's records hold is no known day
        from operating.source_defaults import DATE_SHARE, bulk_values
        by_source: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for oid, found in props.items():
            for prop in DAY_PROPS:
                by_source[(prop, _binding(oid))] += [_day(v) for v in found.get(prop, [])]
        defaults = {k: bulk_values(v, share=DATE_SHARE) for k, v in by_source.items()}
        root = _clusters(twins, pos)
        members: dict[str, list[str]] = defaultdict(list)
        for oid, top in root.items():
            members[top].append(oid)
        self.orders: dict[str, Order] = {}
        self.holders: dict[str, set[str]] = defaultdict(set)
        for top, recs in members.items():
            known = {prop: [d for r in recs for v in props[r].get(prop, [])
                            if (d := _day(v)) is not None
                            and d not in defaults.get((prop, _binding(r)), ())]
                     for prop in DAY_PROPS}
            starts = [d for p in START_PROPS for d in known[p]]
            days = [d for p in DAY_PROPS for d in known[p]]
            words = [str(v).strip().casefold() for r in recs for p in STATUS_PROPS
                     for v in props[r].get(p, [])]
            keys = {k for r in recs for s in supplier_of.get(r, ()) for k in
                    self.supplier_keys[s]}
            self.orders[top] = Order(top, tuple(sorted(recs)), frozenset(keys),
                                     min(starts) if starts else None,
                                     max(days) if days else None,
                                     bool(words) and all(w in CLOSED_WORDS for w in words))
        self.root = root
        # each record's numbers: its own number properties and every identifier-index row
        # of an order number kind (the rows mention leads come from), superseded ones too
        self.numbers: dict[str, set[str]] = defaultdict(set)
        for r, found in props.items():
            for p in NUMBER_PROPS:
                for v in found.get(p, []):
                    if number_key(v):
                        self.numbers[r].add(number_key(v))
        for oid, value in _rows(
                conn, "SELECT object_id, normalized FROM identifiers WHERE identifier_kind "
                      "IN (SELECT value FROM json_each(?)) AND object_id IN (SELECT value "
                      "FROM json_each(?))", pos, json.dumps(number_kinds())):
            if number_key(value):
                self.numbers[oid].add(number_key(value))
        self.holding: dict[str, set[str]] = defaultdict(set)
        for r, found in self.numbers.items():
            for n in found:
                self.holding[n].add(r)
                self.holders[n].add(root.get(r, r))
        self.parties = parties if parties is not None else read_parties(conn)
        self.components: dict[str, set[str]] = defaultdict(set)
        for party, top in getattr(self.parties, "roots", {}).items():
            self.components[top].add(party)

    def numbers_of(self, record: str) -> set[str]:
        """The number keys an order record holds itself."""
        return set(self.numbers.get(record, ()))

    def candidates(self, number: str) -> list[Order]:
        return [self.orders[t] for t in sorted(self.holders.get(number, ())) if t in self.orders]

    def sender_parties(self, sender: Any) -> set[str]:
        """The party keys an external sender address is (none for ours or unknown)."""
        if not isinstance(sender, str) or "@" not in sender:
            return set()
        outcome, _tier, held = self.parties.judge(sender.strip().lower(), self.own)
        if outcome not in ("address", "domain"):
            return set()
        out: set[str] = set()
        roots = getattr(self.parties, "roots", {})
        names = getattr(self.parties, "names", {})
        for h in held:
            out.add(h.party)
            out |= self.components.get(roots.get(h.party, h.party), set())
            if names.get(h.party):
                out.add("name:" + names[h.party])
        return out

    def mail(self, sent: Any, senders: Mapping[str, Any]) -> Mail:
        """A message by its sent time and the senders (message id -> address) of it and
        its conversation; ``Mail.handles``: the messages whose sender named a party."""
        parties: set[str] = set()
        used = []
        for oid, sender in sorted(senders.items()):
            found = self.sender_parties(sender)
            if found:
                parties |= found
                used.append(oid)
        return Mail(_day(sent), frozenset(parties), tuple(used))

    def resolve(self, number: str, mail: Mail) -> Resolution:
        return resolve(self.candidates(number), mail)

    def resolve_linked(self, number: str, mail: Mail, linked: Iterable[str]) -> Resolution:
        """``resolve`` over the number's holders and the order of every record in ``linked``
        (the records the mention's leads reach) the holders miss: a candidate the index
        does not show is still a candidate. A linked record that is no known order: no pick
        (``candidate_unknown``), never a pick among the rest."""
        orders = self.candidates(number)
        keys = {o.key for o in orders}
        covered = {r for o in orders for r in o.records}
        for record in sorted(set(linked) - covered):
            top = self.root.get(record)
            if top is None or top not in self.orders:
                return Resolution(None, "candidate_unknown", {"candidates": len(keys) + 1})
            if top not in keys:
                keys.add(top)
                orders.append(self.orders[top])
        return resolve(orders, mail)


def number_kinds() -> list[str]:
    """The identifier kinds an order number is held under (``NUMBER_KIND`` and its alias
    kinds both ways, ``ontology.identifiers``)."""
    from ontology import identifiers as ids

    return list(dict.fromkeys((NUMBER_KIND, *ids.alias_kinds(NUMBER_KIND),
                               *ids.alias_of_kinds(NUMBER_KIND))))


__all__ = ["CONFIRMING", "Mail", "Order", "RESOLVED", "Resolution", "Resolver",
           "UNRESOLVED", "number_key", "number_kinds", "resolve"]
