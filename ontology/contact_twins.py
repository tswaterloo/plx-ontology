"""Two BC contact cards of one company inside one BC company (``Contact.same_record_as``).

BC keeps a company's contact card once per relation that was set up for it: a card made
with the customer, another with the vendor, a third typed in by hand. Measured on the
11-14 capture, all 9,427 contacts sit in one BC company and 4,509 of them share a
normalized display name with another card.
The person scorer (``ontology.identity_scoring`` kind ``person``) pairs cards only across
BC companies, so none of these was ever linked.

One ``post_project`` step (``operating/build_steps.py`` ``twins_contacts``; steps run in
name order, so after ``reviews`` and a person's rejection is read), link id prefix
``PREFIX``, self-contained: it reads the current store only, removes the rows it wrote
before and writes them again, so it runs in a full build or alone on a store in place
(the step migration, ``ontology.step_migrate``: it reads ``Contact`` rows only).

* Inputs: every current ``bc.contacts`` card's ``display_name``, ``contact_type``,
  ``business_relation``, ``email``, ``phone_number``, ``mobile_phone_number``, ``city``,
  ``state`` and ``postal_code``; its version's lineage (``bc_<book>``) and audience scope
  (one BC company's contacts); a person's rejections (``contradicted`` links of the pair,
  either way round); the people exclusion (``PLX_EXCLUDED_PEOPLE_OIDS`` / ``_FILE``, with
  the mail capture's directory and tenant listing when the build names its state
  directory) and our own mail domains.
* Two cards of one BC company (same lineage and audience scope) are compared only when
  their display names are equal under ``identity_scoring.normalize_name`` and they hold
  a **specific** e-mail address or phone number in common (``identity_scoring._digits``:
  at least 7 digits, the last 10 compared). A name alone never links them (``name_only``).
* Not specific: an address or phone on cards of ``COMMON_NAMES`` or more distinct names in
  the book (a chain's central payables mailbox or switchboard: measured 11-14, one
  mailbox on 1,080 store names; ``common_contact_only``), and a role mailbox (``ap@``,
  ``accounts@``, ``info@``, ``invoices@`` ... :data:`ROLE_MAILBOXES`) unless both cards
  state a location and it agrees.
* Location: city, state and postal code compared where both cards state them. A
  disagreement (two sites of one chain under one name) is a **lead** at most
  (``location_differs``); agreement or a side stating none keeps the next rule.
* **corroborated**: both cards are ``Company`` cards and their business relations are
  Vendor + Vendor, Customer + Customer, Customer + none or none + none. The link cites
  both cards' names, the shared e-mail or phone fields and the locations compared.
* **lead**: any other pair with that evidence (a customer card and a vendor card, a
  vendor card and an unrelated one, a ``Person`` card, a relation BC lists as Employee,
  Bank Account, Other or Multiple, or one this step does not know): a person decides
  through ``plx.review`` (the link type's ``reviewed`` binding).
* Never linked: a pair a person rejected, a pair already ``verified`` or ``reviewed``,
  two cards of different BC companies (the scorer's cross-book pairs), and any card with
  an address at one of our own domains or one the people exclusion names (an outside
  address included, when the guard can decide it). Without a usable exclusion list the
  step writes nothing (``exclusion_unavailable``): fail closed.

A contact twin says two cards are one company's card. It never makes the customers or
vendors the cards are related to (``Contact.bc_customer`` / ``Contact.bc_vendor``) one
party: its binding kind is ``mention`` (a step's), and every reader that joins parties
follows only the party type's own ``same_record_as`` links or ``same_record`` /
``reviewed`` bindings (``ontology.sender_parties``, ``ontology.sender_corroboration``,
``ontology.po_mention_resolve``, ``operating.source_precedence``); the tests hold that.

Stdlib only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from itertools import combinations
import json
import os
import re
from typing import Any, Iterable, Mapping, Sequence

from ontology import mentions
from ontology.identity_scoring import _digits, mail_address, mail_domain, normalize_name
from ontology.mc_task_links import current_values

STEP = "twins_contacts"
PREFIX = "crb-contact-"
LINK_TYPE = "Contact.same_record_as"
OBJECT_TYPE = "Contact"
BINDING = "bc.contacts"
CORROBORATED, LEAD = "corroborated", "lead"
BATCH = 2000
PROPERTIES = ("display_name", "contact_type", "business_relation", "email", "phone_number",
              "mobile_phone_number", "city", "state", "postal_code")
LOCATION = ("city", "state", "postal_code")
# An address or phone on cards of this many distinct names in one book is no firm's own
# (measured 11-14: 4,778 keys on one name, 429 on two, 74 on three, up to 1,080).
COMMON_NAMES = 3
# Mailboxes of a role, not of a firm: shared by a chain's sites, a parent's subsidiaries.
ROLE_MAILBOXES = frozenset({
    "ap", "account", "accounts", "accountspayable", "acctspayable", "payable", "payables",
    "apinvoices", "invoice", "invoices", "ar", "accountsreceivable", "receivable",
    "receivables", "billing", "bills", "info", "admin", "office", "sales", "order",
    "orders", "purchasing", "procurement", "finance", "accounting", "contact", "hello",
    "support", "service", "customerservice", "cs", "team", "mail", "general", "enquiries",
    "inquiries", "noreply", "remittance", "remittances", "payments", "treasury"})
PHONES = ("phone_number", "mobile_phone_number")
COMPANY = "company"
# BC ``contactBusinessRelation``: an unknown value is no relation this step trusts.
RELATIONS = {"customer": "customer", "vendor": "vendor", "none": "none", "": "none",
             "bank_x0020_account": "bank_account", "bank account": "bank_account",
             "employee": "employee", "other": "other", "multiple": "multiple"}
# The relation pairs whose same-evidence twins are corroborated (sorted pairs).
FIRM_RELATIONS = frozenset({("vendor", "vendor"), ("customer", "customer"),
                            ("customer", "none"), ("none", "none")})
_OUTCOMES = ("cards", "pairs", CORROBORATED, LEAD, "name_only", "common_contact_only",
             "location_differs", "rejected", "already_firm", "excluded_card",
             "own_domain_card", "not_bc_card", "name_missing")


def _end(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _digest(*parts: Any) -> str:
    return sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()[:32]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _q(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    return mentions._query(conn, sql, list(params)).fetchall()


def declared(links: Mapping[str, Any]) -> bool:
    """Whether the schema declares this step's binding on ``LINK_TYPE``."""
    return any(b.get("kind") == mentions.BINDING_KIND and b.get("written_by_step") == STEP
               for b in (links.get(LINK_TYPE) or {}).get("bindings", ()))


def relation(value: Any) -> str:
    """A card's business relation, ``unknown`` for a value BC is not known to send."""
    text = value.strip().casefold() if isinstance(value, str) else ""
    return RELATIONS.get(text, "unknown")


def phone(value: Any) -> str | None:
    return _digits(value)


class Card:
    """One contact card's current fields (value rows by property)."""

    def __init__(self, oid: str) -> None:
        self.oid = oid
        self.rows: dict[str, list[Any]] = defaultdict(list)
        self.book: tuple[str, str] | None = None

    def values(self, prop: str) -> list[Any]:
        return [r["value"] for r in self.rows.get(prop, ())]

    def one(self, prop: str) -> Any:
        found = set(self.values(prop))
        return next(iter(found)) if len(found) == 1 else None

    def name(self) -> str:
        names = {normalize_name(v) for v in self.values("display_name")} - {""}
        return next(iter(names)) if len(names) == 1 else ""

    def emails(self) -> dict[str, Any]:
        out = {}
        for row in self.rows.get("email", ()):
            address = mail_address(row["value"])
            if address:
                out.setdefault(address, row)
        return out

    def phones(self) -> dict[str, Any]:
        out = {}
        for prop in PHONES:
            for row in self.rows.get(prop, ()):
                digits = phone(row["value"])
                if digits:
                    out.setdefault(digits, row)
        return out

    def location(self) -> dict[str, str]:
        """Each stated location field, compared without case, spaces or punctuation."""
        out = {}
        for prop in LOCATION:
            found = {re.sub(r"[^0-9a-z]", "", str(v).casefold()) for v in self.values(prop)}
            found.discard("")
            if found:
                out[prop] = "|".join(sorted(found))
        return out

    def kind(self) -> tuple[str, str]:
        """(contact type, relation): ``company`` only for a card typed Company alone."""
        typed = self.one("contact_type")
        kind = COMPANY if isinstance(typed, str) and typed.strip().casefold() == COMPANY \
            else "other"
        relations = {relation(v) for v in self.values("business_relation")} or {"none"}
        return kind, (next(iter(relations)) if len(relations) == 1 else "unknown")


def tier_of(a: Card, b: Card) -> str:
    """``corroborated`` for two Company cards of a firm relation pair, else ``lead``."""
    (kind_a, rel_a), (kind_b, rel_b) = a.kind(), b.kind()
    if kind_a == kind_b == COMPANY and tuple(sorted((rel_a, rel_b))) in FIRM_RELATIONS:
        return CORROBORATED
    return LEAD


def where(a: Card, b: Card) -> tuple[str, list[str]]:
    """``differs`` (with the fields), ``agrees`` (every field both state is equal, at least
    one) or ``unstated``."""
    la, lb = a.location(), b.location()
    both = [p for p in LOCATION if p in la and p in lb]
    differs = [p for p in both if la[p] != lb[p]]
    if differs:
        return "differs", differs
    return ("agrees" if both else "unstated"), both


def role_mailbox(address: str) -> bool:
    local = address.split("@", 1)[0]
    return local in ROLE_MAILBOXES or re.sub(r"[._+-]", "", local) in ROLE_MAILBOXES


def read_cards(conn: Any) -> dict[str, Card]:
    """Every current ``bc.contacts`` card with its book (lineage, audience scope)."""
    cards: dict[str, Card] = {}
    for row in current_values(conn, PROPERTIES, (OBJECT_TYPE,)):
        oid = row["object_id"]
        cards.setdefault(oid, Card(oid)).rows[row["property"]].append(row)
    oids = sorted(cards)
    for i in range(0, len(oids), 500):
        part = oids[i:i + 500]
        marks = ",".join("?" * len(part))
        scope = {r["object_id"]: (r["binding_id"], r["audience_key"]) for r in _q(
            conn, f"SELECT object_id, binding_id, audience_key FROM objects WHERE object_id "
                  f"IN ({marks})", part)}
        for r in _q(conn, "SELECT object_id, lineage FROM object_versions WHERE superseded_at "
                          f"IS NULL AND object_id IN ({marks})", part):
            binding, audience = scope.get(r["object_id"], (None, None))
            if binding == BINDING and audience and isinstance(r["lineage"], str) \
                    and r["lineage"].startswith("bc_"):
                cards[r["object_id"]].book = (r["lineage"], audience)
    return cards


def _excluded(card: Card, guard: Any, own: frozenset[str]) -> str | None:
    """Why a card is never read further: an own-domain or an excluded person's address."""
    for address in card.emails():
        if mail_domain(address) in own or any(mail_domain(address).endswith("." + d)
                                              for d in own):
            return "own_domain_card"
        if guard.decides(address) and guard.is_excluded(address):
            return "excluded_card"
    return None


def _cite(row: Any) -> dict[str, Any]:
    return {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
            "digest": row["digest"], "lineage": row["lineage"]}


def twin_links(conn: Any, guard: Any, own_domains: Iterable[str]
               ) -> tuple[list[dict[str, Any]], Counter]:
    """The link rows (module docstring) and the outcome counts."""
    counts: Counter[str] = Counter({c: 0 for c in _OUTCOMES})
    own = frozenset(d.lower() for d in own_domains)
    groups: dict[tuple, list[Card]] = defaultdict(list)
    # per book, the distinct names each address and phone is on (every named card)
    names_of: dict[tuple, set[str]] = defaultdict(set)
    for oid, card in sorted(read_cards(conn).items()):
        if card.book is None:
            counts["not_bc_card"] += 1
            continue
        why = _excluded(card, guard, own)
        if why:   # never read further, not even counted under its name
            counts[why] += 1
            continue
        name = card.name()
        if name:
            for key in [("e", e) for e in card.emails()] + [("p", t) for t in card.phones()]:
                names_of[(card.book, key)].add(name)
        if not name:
            counts["name_missing"] += 1
            continue
        counts["cards"] += 1
        groups[(card.book, name)].append(card)
    pairs: dict[tuple[str, str], tuple[Card, Card]] = {}
    for cards in groups.values():
        for a, b in combinations(sorted(cards, key=lambda c: c.oid), 2):
            pairs[(a.oid, b.oid)] = (a, b)
    ends = sorted({oid for pair in pairs for oid in pair})
    settled: dict[tuple[str, str], set[str]] = defaultdict(set)
    for i in range(0, len(ends), 500):
        part = ends[i:i + 500]
        for r in _q(conn, "SELECT from_object, to_object, tier FROM link_rows WHERE link_type = "
                          "? AND superseded_at IS NULL AND NOT (link_id >= ? AND link_id < ?) "
                          f"AND from_object IN ({','.join('?' * len(part))})",
                    [LINK_TYPE, PREFIX, _end(PREFIX), *part]):
            settled[tuple(sorted((r["from_object"], r["to_object"])))].add(r["tier"])
    out: list[dict[str, Any]] = []
    for (a_oid, b_oid), (a, b) in sorted(pairs.items()):
        counts["pairs"] += 1
        tiers = settled.get((a_oid, b_oid), set())
        if "contradicted" in tiers:
            counts["rejected"] += 1
            continue
        if tiers & {"verified", "reviewed"}:
            counts["already_firm"] += 1
            continue
        a_mail, b_mail = a.emails(), b.emails()
        a_tel, b_tel = a.phones(), b.phones()
        shared = [("e", k) for k in sorted(set(a_mail) & set(b_mail))] \
            + [("p", k) for k in sorted(set(a_tel) & set(b_tel))]
        if not shared:
            counts["name_only"] += 1
            continue
        place, fields = where(a, b)
        specific = [key for key in shared
                    if len(names_of[(a.book, key)]) < COMMON_NAMES
                    and not (key[0] == "e" and role_mailbox(key[1]) and place != "agrees")]
        if not specific:
            counts["common_contact_only"] += 1
            continue
        tier = tier_of(a, b)
        if place == "differs":
            tier = LEAD
            counts["location_differs"] += 1
        counts[tier] += 1
        name_a, name_b = a.rows["display_name"][0], b.rows["display_name"][0]
        reason: list[dict[str, Any]] = [
            {"feature": "display_name", "agrees": True, "level": "exact",
             "normalized": a.name(), "citations": [_cite(name_a), _cite(name_b)]}]
        cited = [name_a, name_b]
        for kind, key in specific[:2]:
            rows = [a_mail[key], b_mail[key]] if kind == "e" else [a_tel[key], b_tel[key]]
            reason.append({"feature": "email" if kind == "e" else "phone", "agrees": True,
                           "level": "exact", "names_holding": len(names_of[(a.book, (kind, key))]),
                           "citations": [_cite(r) for r in rows]})
            cited += rows
        located = [r for card in (a, b) for p in fields for r in card.rows.get(p, ())]
        reason.append({"feature": "location", "agrees": {"agrees": True, "differs": False}
                       .get(place), "level": place, "fields": fields,
                       "citations": [_cite(r) for r in located]})
        cited += located
        (kind_a, rel_a), (kind_b, rel_b) = a.kind(), b.kind()
        reason.append({"feature": "same_bc_company", "agrees": True, "lineage": a.book[0]})
        reason.append({"feature": "business_relation", "agrees": None,
                       "values": [rel_a, rel_b], "contact_types": [kind_a, kind_b]})
        why_not = []
        if place == "differs":
            why_not.append({"feature": "location", "agrees": False, "fields": fields,
                            "detail": "the cards are in different places: two sites of "
                                      "one name; a person confirms"})
        elif tier == LEAD:
            why_not.append({"feature": "relations", "agrees": None,
                            "detail": "the cards are not two company cards of one side "
                                      "(customer, vendor or none): a person confirms"})
        link_id = PREFIX + _digest(a_oid, b_oid)
        handles = list(dict.fromkeys(r["l1_handle"] for r in cited))
        out.append({
            "link_id": link_id, "link_type": LINK_TYPE, "from_object": a_oid,
            "to_object": b_oid, "tier": tier, "binding_kind": mentions.BINDING_KIND,
            "link_contract": None, "reason": _canon(reason), "why_not": _canon(why_not),
            "evidence_handles": _canon(handles),
            "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
            "markings": mentions._marks(*(m for r in cited for m in (r["vmarks"], r["kmarks"]))),
            "recorded_at": max(r["recorded_at"] for r in cited),
            "superseded_at": None})
    return out, counts


def _excluder(state_dir: Any = None) -> Any:
    """The people exclusion from the environment (with the mail capture's directory and
    tenant listing under ``state_dir``, so an outside address of an excluded person the
    tenant lists is decided too), or None when it is unavailable."""
    from integrations.people_exclusion import (PeopleExclusion, environ_with_exclusion,
                                               with_directory)
    guard = PeopleExclusion.from_env(environ_with_exclusion(os.environ))
    if guard.unavailable:
        return None
    if state_dir is not None:
        try:
            guard = with_directory(guard, state_dir)
        except Exception:  # noqa: BLE001 - an unreadable capture: no link is decided by it
            return None
    return guard


def step_for(state_dir: Any = None) -> Any:
    """The ``twins_contacts`` step of a build over the Workbench state directory."""
    def step(builder: Any, projector: Any) -> dict[str, Any]:
        return contact_twins_step(builder, projector, state_dir=state_dir)
    return step


def contact_twins_step(builder: Any, projector: Any, *, state_dir: Any = None
                       ) -> dict[str, Any]:
    """The ``twins_contacts`` step (module docstring)."""
    from ontology.sender_parties import own_domains
    conn = builder.connection
    conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                 (PREFIX, _end(PREFIX)))
    if not declared(getattr(projector, "links", {}) or {}):
        return {"counts": {}, "link_tiers": {}}
    guard = _excluder(state_dir)
    if guard is None:
        return {"counts": {"exclusion_unavailable": 1}, "link_tiers": {}}
    rows, counts = twin_links(conn, guard, own_domains(projector))
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])
    return {"counts": dict(counts),
            "link_tiers": dict(Counter(r["tier"] for r in rows))}


__all__ = ["COMMON_NAMES", "LINK_TYPE", "PREFIX", "ROLE_MAILBOXES", "STEP",
           "contact_twins_step", "declared", "step_for", "tier_of", "twin_links", "where"]
