"""One business behind several party rows: ``party_handles`` of ``suppliers`` and
``customers``, every row of the same business by name or mail domain (11-22: a supplier's
overdue Sage balance sat on one Atlas Containers row and its statement mail on another, so
"has anyone emailed them" found nothing).

Never a merge: each row keeps its own codes, copies and handle (identity stays the merge
rule's, ``business_views.party_merge``); this only says which rows to read together, so a
join of mail, balances or orders over ``party_handles`` reaches all of them. Two rules,
read from the rows themselves (no name or domain is written here) and conservative:

* **Name**: the same name once read the way people write it: a parenthesised part and a
  trailing payment or currency word are how Sage names a vendor's payment method or account
  (``P.R. RESOURCES (EFT)``, ``PK CHEM INDUSTRIES - EFT``), not the business; then
  ``identity_scoring.normalize_name`` (case, punctuation, legal suffixes). A name of
  generic words only (``sender_parties._GENERIC``, ``message_parties.FUNCTION_WORDS``)
  joins nothing.
* **Domain**: the same registrable domain in the rows' email or website, never a shared
  mail provider, a domain held under more than ``MAX_DOMAIN_NAMES`` different names (a
  parent, a forwarder, our own), and only when the two names share a word of at least
  ``MIN_WORD`` letters that is not generic (a factoring company writing from its client's
  domain stays apart).

A group larger than ``MAX_GROUP`` rows joins nothing (a placeholder name many rows carry).
Stdlib only."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable, Mapping, Sequence

MAX_DOMAIN_NAMES = 3
MAX_GROUP = 12
MIN_WORD = 4
PAYMENT_WORDS = frozenset({"eft", "ach", "cad", "usd", "us", "wire"})
_ADDR = re.compile(r"[A-Za-z0-9._%+'-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PAREN = re.compile(r"\([^()]*\)?")
HANDLES_DOC = ("every row of the same business, this one included (the same name as people "
               "write it, or the same mail domain; never merged: each keeps its own codes "
               "and copies). Join through it to reach all of them: mail by e.{kind}s && "
               "party_handles, balances, invoices and orders by {kind}_handle = "
               "ANY(party_handles)")


def _generic() -> frozenset:
    from ontology.sender_parties import _GENERIC
    from operating.pg.message_parties import FUNCTION_WORDS
    return _GENERIC | FUNCTION_WORDS


def name_key(name: object) -> str:
    """The name as one business is written (``Name`` rule); '' when it names nothing."""
    from ontology.identity_scoring import normalize_name
    if not isinstance(name, str):
        return ""
    words = normalize_name(_PAREN.sub(" ", name)).split()
    while words and words[-1] in PAYMENT_WORDS:
        words.pop()
    generic = _generic()
    if not words or all(w in generic or len(w) < 2 for w in words):
        return ""
    return " ".join(words)


def _domains(email: object, website: object) -> set[str]:
    from ontology import mentions
    held = {m.group(1).lower() for m in _ADDR.finditer(email if isinstance(email, str) else "")}
    site = mentions.website_domain(website)
    if site:
        held.add(site)
    return {d for d in (mentions.registrable_domain(h) for h in held)
            if d and not mentions.shared_mail_domain(d)}


def _words(key: str) -> set[str]:
    generic = _generic()
    return {w for w in key.split() if len(w) >= MIN_WORD and w not in generic}


def groups(rows: Iterable[Mapping[str, object]]) -> dict[str, list[str]]:
    """handle -> every handle of its business, sorted (itself included), for rows
    ``{"handle", "name", "email", "website"}``."""
    rows = [r for r in rows if isinstance(r.get("handle"), str)]
    parent = {r["handle"]: r["handle"] for r in rows}

    def find(h: str) -> str:
        while parent[h] != h:
            parent[h] = parent[parent[h]]
            h = parent[h]
        return h

    def join(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    keys = {r["handle"]: name_key(r.get("name")) for r in rows}
    by_name: dict[str, list[str]] = defaultdict(list)
    by_domain: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if keys[r["handle"]]:
            by_name[keys[r["handle"]]].append(r["handle"])
        for d in _domains(r.get("email"), r.get("website")):
            by_domain[d].append(r["handle"])
    for held in by_name.values():
        for h in held[1:]:
            join(held[0], h)
    for held in by_domain.values():
        if len({keys[h] for h in held}) > MAX_DOMAIN_NAMES:
            continue
        for i, a in enumerate(held):
            for b in held[i + 1:]:
                if _words(keys[a]) & _words(keys[b]):
                    join(a, b)
    members: dict[str, list[str]] = defaultdict(list)
    for h in parent:
        members[find(h)].append(h)
    out: dict[str, list[str]] = {}
    for held in members.values():
        kept = sorted(held) if len(held) <= MAX_GROUP else None
        for h in held:
            out[h] = kept if kept is not None else [h]
    return out


def fill(rows: Sequence[object]) -> None:
    """Set ``party_handles`` on built view rows (``business_views.Row``: values name, email,
    website and the row's handle) and read every other row of the group into that column
    (its records' audience keys and markings are the column's own class)."""
    got = groups({"handle": r.handle, "name": r.values.get("name"),
                  "email": r.values.get("email"), "website": r.values.get("website")}
                 for r in rows)
    by_handle = {r.handle: r for r in rows}
    for row in rows:
        held = got.get(row.handle) or [row.handle]
        row.values["party_handles"] = held
        for other in held:
            if other == row.handle:
                continue
            for oid in by_handle[other].objects:
                row.read_record(oid, "*", column="party_handles")
