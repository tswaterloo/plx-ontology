"""Messages linked to the customer or supplier that sent them (``Email.sent_by_supplier``,
``Email.sent_by_customer``), from the sender address and the party's own stored addresses.

A ``post_project`` step (``operating/build_steps.py`` ``sender_parties``; after
``reviews``, so a pair a person rejected is never linked). For every current message
(``graph_mail.messages``) whose sender is an address:

* **Internal** senders (our own domains: the own-party names that are domains,
  ``document_fields`` ``own_parties``, and the mail capture's configured internal domains;
  ``ontology.mentions.sent_from_own_domain``) link nothing.
* **corroborated**: the sender address is exactly an address a party stores (a
  supplier's ``bc_email``/``rep_email``/``csr_email``, a customer's ``bc_email``/``email``,
  or a contact's ``email`` that a ``verified`` link joins to the party), and that address
  names one party only.
* **lead**: otherwise, the sender's domain is at or under a domain a party holds (those
  addresses' domains, or a supplier's ``website``), the domain is no shared mail provider
  (``ontology.mentions.shared_mail_domain``), it names one party only, and the party's
  name is the one the domain spells (``name_fits_domain``): a domain a party only uses (a
  government's or school board's address on a customer location, an agent's) is no lead.

**The record a message names** decides two more cases, for a sender of outside, by the
parties of the records the message itself names (its own links, any tier but
``contradicted``, to a sales or purchase order, a sales or purchase invoice, a shipment, a
customer PO or an aged payable, and each of those records' confirming or stored-join links
to a customer or supplier; or its links straight to one), and only where the sender's
domain spells that party's name (``name_fits_domain``):

* a sender whose address or domain a customer and a supplier both hold (otherwise
  ``shared_by_parties``) is linked to the holders of the side the named records' party is on
  and that are that party (``named_side_address``, ``corroborated``; ``named_side_domain``,
  ``lead``);
* a sender whose domain no party holds (otherwise ``no_party``) is a ``lead`` to the one
  party the named records belong to (``named_record``).

Measured 2026-11-09 on 20,000 sampled messages:
the named party is the sender's own on 60 of 66 messages already linked when the domain
spells its name (91%), on 1 of 40 when it does not, so a domain that spells no name never
links: a broker, a carrier or a parent company names its client's orders.

*One party*: the records holding the address or domain are all one party when each two
are joined by a confirming ``same_record`` link (verified, reviewed, corroborated), or
carry the same normalised name (``identity_scoring.normalize_name``: one customer synced
to several BC books). Any other pair of holders is two parties, and nothing is linked
(counted ``shared_by_parties``): a reused or shared domain never links a message to the
wrong party. The message is linked to every record holding it and to each holder's
confirming ``same_record`` twins of its type.

**Meetings** (``CalendarEvent.with_supplier`` / ``with_customer``, ``Meeting.with_supplier`` /
``with_customer``): every address of a calendar event's or meeting's organizer and attendees
is judged as a sender is, and the event is linked to each party an address names
(corroborated when one address is one the party stores, else a lead by its domain).

Each link cites what it read (the message's sender value, the party value) as features,
never the values themselves; its evidence handles are the message version, the party
value and the target's version, and its markings their union (a stored address is
``pii``). A person's rejection (a ``contradicted`` link of the pair) vetoes it. Rows a
previous build wrote (link id prefix ``PREFIX``) are removed first, as an incremental build
seeds links forward. Stdlib only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ontology import mentions

PREFIX = "sendparty:"
_PREFIX_END = PREFIX[:-1] + chr(ord(PREFIX[-1]) + 1)
LINK_TYPES = {"Supplier": "Email.sent_by_supplier", "Customer": "Email.sent_by_customer"}
PARTY_TYPES = tuple(LINK_TYPES)
CONTACT_TYPE = "Contact"
# Party properties naming its addresses (``mail``) or its site (``website``).
PROPERTIES = {"website": "website", "bc_email": "mail", "rep_email": "mail",
              "csr_email": "mail", "email": "mail"}
CONFIRMING = ("verified", "reviewed", "corroborated")
# The records a message can name whose customer or supplier tells its side (module docstring).
NAMED_TYPES = ("SalesOrder", "PurchaseOrder", "SalesInvoice", "PurchaseInvoice", "Shipment",
               "AgedPayable", "CustomerPurchaseOrder")
NAMED_PARTY_TIERS = (*CONFIRMING, "stored_by_text_join")
NAMED = "named_record"
MESSAGE_TYPE = "Email"
SENDER = "sender"
MEETING_LINK_TYPES = {
    "CalendarEvent": {"Supplier": "CalendarEvent.with_supplier",
                      "Customer": "CalendarEvent.with_customer"},
    "Meeting": {"Supplier": "Meeting.with_supplier", "Customer": "Meeting.with_customer"}}
MEETING_PROPS = ("organizer", "attendees")
BATCH = 5000
_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class Holding:
    """One stored party value naming an address or a domain."""
    party: str            # the Supplier/Customer object the value is the party's
    party_type: str
    prop: str
    domain: str
    address: str | None   # the whole address of a mail value, lower case
    handle: str
    cite: Mapping[str, Any]
    markings: str
    recorded_at: str


def addresses(value: Any) -> list[str]:
    return [a.lower() for a in _ADDRESS.findall(value)] if isinstance(value, str) else []


def _domains(prop: str, value: Any) -> list[tuple[str, str | None]]:
    """(domain, address or None) of one party value."""
    if PROPERTIES.get(prop) == "website":
        found = mentions.website_domain(value)
        return [(found, None)] if found else []
    return [(d, a) for a in addresses(value) if (d := mentions.mail_domain(a))]


class Parties:
    """Every party value naming an address or domain, and which records are one party."""

    def __init__(self, holdings: Iterable[Holding], roots: Mapping[str, str],
                 names: Mapping[str, str], twins: Mapping[str, Sequence[str]]) -> None:
        self.by_domain: dict[str, list[Holding]] = defaultdict(list)
        self.by_address: dict[str, list[Holding]] = defaultdict(list)
        for h in holdings:
            self.by_domain[h.domain].append(h)
            if h.address:
                self.by_address[h.address].append(h)
        self.roots, self.names, self.twins = roots, names, twins

    def one_party(self, holdings: Sequence[Holding]) -> bool:
        """Whether every holder is one party (module docstring)."""
        return self.one_of(h.party for h in holdings)

    def one_of(self, records: Iterable[str]) -> bool:
        """Whether every record is one party (module docstring)."""
        parties = sorted(set(records))
        if len(parties) < 2:
            return bool(parties)
        parent = {p: p for p in parties}

        def root(p: str) -> str:
            while parent[p] != p:
                p = parent[p]
            return p
        for key in (lambda p: self.roots.get(p, p), lambda p: self.names.get(p) or None):
            seen: dict[str, str] = {}
            for p in parties:
                k = key(p)
                if k is None:
                    continue
                if k in seen:
                    parent[root(p)] = root(seen[k])
                else:
                    seen[k] = p
        return len({root(p) for p in parties}) == 1

    def judge(self, sender: str, own: Sequence[str]) -> tuple[str, str | None, list[Holding]]:
        """(outcome, tier, holdings) for one sender address."""
        domain = mentions.mail_domain(sender)
        if domain is None:
            return "no_sender", None, []
        if mentions.sent_from_own_domain(domain, own):
            return "own_sender", None, []
        exact = self.by_address.get(sender.lower(), [])
        if exact:
            if not self.one_party(exact):
                return "shared_by_parties", None, exact
            return "address", "corroborated", exact
        if mentions.shared_mail_domain(domain):
            return "shared_mail_provider", None, []
        labels = domain.split(".")
        for i in range(len(labels) - 1):
            held = self.by_domain.get(".".join(labels[i:]))
            if held:
                if not self.one_party(held):
                    return "shared_by_parties", None, held
                if not any(name_fits_domain(self.names.get(h.party, ""), domain)
                           for h in held):
                    return "domain_name_disagrees", None, []
                return "domain", "lead", held
        return "no_party", None, []

    def same(self, a: str, b: str) -> bool:
        """Whether two party records are one party (twins or one normalised name)."""
        return a == b or self.roots.get(a, a) == self.roots.get(b, b) or bool(
            self.names.get(a) and self.names.get(a) == self.names.get(b))

    def by_named(self, sender: str, outcome: str, held: Sequence[Holding],
                 named: Sequence[Holding]) -> tuple[str, str | None, list[Holding]]:
        """(outcome, tier, holdings) for a sender ``judge`` left unlinked as
        ``shared_by_parties`` (``held``: the holders it found) or ``no_party``, by the
        parties of the records the message names (``named``: one ``named_record`` holding
        per party; module docstring). Anything else keeps ``outcome``, unlinked."""
        domain = mentions.mail_domain(sender)
        if domain is None or not named:
            return outcome, None, []
        if outcome == "shared_by_parties":
            pick = [h for h in held if any(h.party_type == n.party_type
                                           and self.same(h.party, n.party) for n in named)]
            if pick and self.one_party(pick) and any(
                    name_fits_domain(self.names.get(h.party, ""), domain) for h in pick):
                if any(h.address == sender.lower() for h in pick):
                    return "named_side_address", "corroborated", pick
                return "named_side_domain", "lead", pick
        elif outcome == "no_party" and not mentions.shared_mail_domain(domain):
            if self.one_party(named) and any(
                    name_fits_domain(self.names.get(n.party, ""), domain) for n in named):
                return NAMED, "lead", list(named)
        return outcome, None, []


# Name words that say what a party is, not who: never enough to tie a name to a domain.
_GENERIC = frozenset(
    "the and of for inc ltd llc corp co company corporation limited group international "
    "canada canadian usa america american north south east west global services service "
    "industries industrial products product packaging solutions systems supply supplies "
    "sales trading hotel hotels club centre center eft ach cad usd warehouse store "
    "stores division billing".split())


def name_fits_domain(name: str, domain: str) -> bool:
    """Whether a party's (normalised) name is the one its domain spells: the domain's
    registrable label holds the name's first word (three letters or more) or another
    distinctive word of it (four or more), or the whole name run together. A domain a
    party only uses (a government's, a school board's, an agent's) names it by no word
    and is no lead: its sender may be anyone of that organisation."""
    label = re.sub(r"[^a-z0-9]", "", mentions.registrable_domain(domain).split(".")[0])
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _GENERIC]
    if not label or not words:
        return False
    if "".join(words) in label or label in "".join(words):
        return len(label) >= 3
    return (len(words[0]) >= 3 and words[0] in label) or any(
        len(w) >= 4 and w in label for w in words[1:])


def _digest(*parts: Any) -> str:
    return sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()[:32]


def _q(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    return mentions._query(conn, sql, list(params)).fetchall()


def _fields(conn: Any, props: Iterable[str]) -> dict[int, str]:
    props = list(props)
    return {r[0]: r[1] for r in _q(conn, "SELECT field, property FROM value_fields WHERE "
                                         f"property IN ({','.join('?' * len(props))})", props)}


_CELLS = ("SELECT v.object_id, v.l1_handle, v.markings AS vmarks, k.markings AS kmarks, "
          "coalesce(s.digest, v.digest) AS digest, coalesce(s.lineage, v.lineage) AS lineage, "
          "s.recorded_at, lf.field, f.field_path, CASE WHEN c.field IS NULL THEN json_extract(c.value, "
          "lf.path) ELSE c.value END AS value FROM value_layout_fields lf JOIN value_cells c "
          "ON c.layout = lf.layout JOIN value_sources s ON s.src = c.src JOIN object_versions v "
          "ON v.rowid = s.ver JOIN value_kinds k ON k.kind = lf.kind JOIN value_fields f ON "
          "f.field = lf.field WHERE lf.field IN ({fields}) AND s.superseded_at IS NULL")


def _values(conn: Any, props: Iterable[str], prefixes: Sequence[str] = ()) -> Iterator[Any]:
    """Current value rows of ``props`` (objects whose id starts with one of ``prefixes``)."""
    fields = _fields(conn, props)
    if not fields:
        return
    cursor = mentions._query(conn, _CELLS.format(fields=",".join(str(f) for f in fields)), [])
    for row in cursor:
        if row["value"] is None or (prefixes and not row["object_id"].startswith(prefixes)):
            continue
        yield row, fields[row["field"]]


def _confirming_neighbours(conn: Any, oid: str) -> list[tuple[str, str]]:
    """(other end, link markings) of oid's current confirming same_record links."""
    link = oid.split(":", 1)[0] + ".same_record_as"
    return [(r[0], r[1]) for r in _q(
        conn, "SELECT to_object, markings FROM link_rows WHERE from_object = ? AND link_type = ? "
              "AND tier IN ('verified', 'reviewed', 'corroborated') AND superseded_at IS NULL "
              "UNION SELECT from_object, markings FROM link_rows WHERE to_object = ? AND "
              "link_type = ? AND tier IN ('verified', 'reviewed', 'corroborated') AND "
              "superseded_at IS NULL", [oid, link, oid, link])]


def read_parties(conn: Any) -> Parties:
    """The parties' holdings, identity components, names and twins from the store."""
    prefixes = tuple(t + ":" for t in (*PARTY_TYPES, CONTACT_TYPE))
    raw: list[tuple[Any, str]] = list(_values(conn, PROPERTIES, prefixes))
    holdings: list[Holding] = []
    for row, prop in raw:
        oid = row["object_id"]
        if oid.startswith(CONTACT_TYPE + ":"):
            if prop != "email":
                continue
            owners = [r[0] for r in _q(
                conn, "SELECT to_object FROM link_rows WHERE from_object = ? AND tier = "
                      "'verified' AND binding_kind NOT IN ('mention', 'same_record') AND "
                      "superseded_at IS NULL", [oid])
                      if r[0].split(":", 1)[0] in PARTY_TYPES]
        elif prop in PROPERTIES:
            owners = [oid]
        else:
            continue
        marks = mentions._marks(row["vmarks"], row["kmarks"])
        cite = {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
                "digest": row["digest"], "lineage": row["lineage"]}
        for domain, address in _domains(prop, row["value"]):
            for owner in owners:
                holdings.append(Holding(owner, owner.split(":", 1)[0], prop, domain, address,
                                        row["l1_handle"], cite, marks, row["recorded_at"]))
    # identity components and twins of every holder (confirming same_record links)
    roots: dict[str, str] = {}
    twins: dict[str, list[str]] = {}
    for start in sorted({h.party for h in holdings}):
        _component(conn, start, roots, twins)
    from ontology.identity_scoring import normalize_name
    names: dict[str, str] = {}   # every party's: a named record's party may hold nothing
    for row, _prop in _values(conn, ["name"], tuple(t + ":" for t in PARTY_TYPES)):
        names.setdefault(row["object_id"], normalize_name(row["value"]))
    return Parties(holdings, roots, names, twins)


def _component(conn: Any, start: str, roots: dict[str, str],
               twins: dict[str, list[str]]) -> None:
    """Record ``start``'s identity component (confirming same_record links) in ``roots``
    and ``twins``, unless it is known."""
    if start in roots:
        return
    component, todo = {start}, [start]
    while todo:
        for other, _marks in _confirming_neighbours(conn, todo.pop()):
            if other not in component and len(component) < 50:
                component.add(other)
                todo.append(other)
    root = min(component)
    for member in component:
        roots[member] = root
        twins[member] = sorted(o for o in component
                               if o != member and o.split(":", 1)[0] == member.split(":", 1)[0])


def named_parties(conn: Any, message: str, parties: Parties) -> list[Holding]:
    """One ``named_record`` holding per party record the records ``message`` names
    belong to (module docstring), citing the named record (or the party it names)."""
    found: dict[str, str] = {}   # party -> the record naming it
    for (target,) in _q(conn, "SELECT to_object FROM link_rows WHERE from_object = ? AND "
                              "tier <> 'contradicted' AND superseded_at IS NULL AND NOT "
                              "(link_id >= ? AND link_id < ?)",
                        [message, PREFIX, _PREFIX_END]):
        kind = target.split(":", 1)[0]
        if kind in PARTY_TYPES:
            found.setdefault(target, target)
        elif kind in NAMED_TYPES:
            for (party,) in _q(conn, "SELECT to_object FROM link_rows WHERE from_object = ? "
                                     f"AND tier IN ({','.join('?' * len(NAMED_PARTY_TIERS))}) "
                                     "AND superseded_at IS NULL",
                               [target, *NAMED_PARTY_TIERS]):
                if party.split(":", 1)[0] in PARTY_TYPES:
                    found.setdefault(party, target)
    if not found:
        return []
    versions = _versions(conn, set(found.values()))
    out = []
    for party, record in sorted(found.items()):
        version = versions.get(record)
        if version is None:
            continue
        _component(conn, party, parties.roots, parties.twins)
        out.append(Holding(party, party.split(":", 1)[0], NAMED, "", None,
                           version["l1_handle"], {"l1_handle": version["l1_handle"]},
                           version["markings"], version["recorded_at"]))
    return out


def own_domains(projector: Any) -> list[str]:
    from ontology.document_fields import profiles
    own = {n.lower() for n in profiles()["own_parties"]["names"] if "." in n}
    for source in (getattr(projector, "sources", None) or {}).values():
        own |= {str(d).lower() for d in getattr(source, "internal_domains", ()) or ()}
    return sorted(mentions.own_mail_domains(own))


def _versions(conn: Any, oids: Iterable[str]) -> dict[str, Any]:
    oids = sorted(set(oids))
    out: dict[str, Any] = {}
    for i in range(0, len(oids), 500):
        part = oids[i:i + 500]
        for r in _q(conn, "SELECT object_id, l1_handle, markings, recorded_at FROM "
                          "object_versions WHERE superseded_at IS NULL AND object_id IN "
                          f"({','.join('?' * len(part))})", part):
            out[r["object_id"]] = r
    return out


def link_rows(message: Any, tier: str, holdings: Sequence[Holding], parties: Parties,
              versions: Mapping[str, Any], declared: Mapping[str, str],
              role: str = SENDER, named: Sequence[Holding] = ()) -> list[dict[str, Any]]:
    """The link rows of one message (``message``: its sender value row) to every holder
    and its twins (``declared``: party type -> link type)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    msg_cite = {"l1_handle": message["l1_handle"], "field_path": message["field_path"],
                "digest": message["digest"], "lineage": message["lineage"]}
    for h in sorted(holdings, key=lambda h: (h.party, h.prop, h.handle)):
        for target in [h.party, *parties.twins.get(h.party, ())]:
            link_type = declared.get(target.split(":", 1)[0])
            version = versions.get(target)
            if link_type is None or version is None or (link_type, target) in out:
                continue
            exact = tier == "corroborated"
            if h.prop == NAMED:
                reason = [{"feature": f"{role}_domain", "agrees": True,
                           "citations": [msg_cite]},
                          {"feature": "named_record_party", "agrees": True,
                           "citations": [dict(h.cite)]}]
                why_not = [{"feature": "domain_spells_name", "agrees": None,
                            "detail": "no party stores the sender's address or domain; the "
                                      "message names this party's record and the domain "
                                      "spells its name: a lead"}]
            else:
                reason = [{"feature": f"{role}_address" if exact else f"{role}_domain",
                           "agrees": True, "citations": [msg_cite]},
                          {"feature": "party_address" if exact else "party_domain",
                           "agrees": True, "matched_on": h.prop, "citations": [dict(h.cite)]}]
                why_not = [] if exact else [{
                    "feature": "domain_only", "agrees": None,
                    "detail": "the sender's domain is one only this party holds; the address "
                              "itself is not one it stores: a lead"}]
            reason.extend({"feature": "named_record_party", "agrees": True,
                           "citations": [dict(n.cite)]} for n in named
                          if h.prop != NAMED and n.party_type == h.party_type
                          and parties.same(n.party, h.party))
            if target != h.party:
                reason.append({"feature": "same_record_twin", "agrees": True})
            out[(link_type, target)] = {
                "link_id": PREFIX + _digest(message["object_id"], message["l1_handle"],
                                            link_type, target),
                "link_type": link_type, "from_object": message["object_id"],
                "to_object": target, "tier": tier, "binding_kind": mentions.BINDING_KIND,
                "link_contract": None,
                "reason": json.dumps(reason, sort_keys=True, separators=(",", ":")),
                "why_not": json.dumps(why_not, sort_keys=True, separators=(",", ":")),
                "evidence_handles": json.dumps(list(dict.fromkeys(
                    [message["l1_handle"], h.handle, version["l1_handle"],
                     *(n.handle for n in named if h.prop != NAMED)])),
                    separators=(",", ":")),
                "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
                "markings": mentions._marks(message["vmarks"], message["kmarks"], h.markings,
                                            version["markings"],
                                            *(n.markings for n in named if h.prop != NAMED)),
                "recorded_at": max(message["recorded_at"], h.recorded_at,
                                   version["recorded_at"]),
                "superseded_at": None}
    return [out[k] for k in sorted(out)]


def declared_types(schema_links: Mapping[str, Mapping[str, Any]],
                   types: Mapping[str, str] = LINK_TYPES) -> dict[str, str]:
    """party type -> link type, for the declared ones with a ``mention`` binding."""
    return {party: link for party, link in types.items()
            if any(b.get("kind") == mentions.BINDING_KIND
                   for b in (schema_links.get(link) or {}).get("bindings", ()))}


def judge_messages(conn: Any, parties: Parties, own: Sequence[str],
                   limit: int | None = None) -> Iterator[tuple[Any, str, str | None,
                                                               list[Holding]]]:
    """(sender value row, outcome, tier, holdings) of every current message sender."""
    seen = 0
    for row, _prop in _values(conn, [SENDER], (MESSAGE_TYPE + ":",)):
        found = addresses(row["value"])
        if len(found) != 1:
            yield row, "no_sender", None, []
        else:
            yield (row, *parties.judge(found[0], own))
        seen += 1
        if limit is not None and seen >= limit:
            return


def meeting_rows(conn: Any, parties: Parties, own: Sequence[str],
                 schema_links: Mapping[str, Mapping[str, Any]], counts: Counter
                 ) -> list[dict[str, Any]]:
    """The rows linking calendar events and meetings to the parties their organizer and
    attendee addresses name (module docstring): a corroborated link wins over a lead."""
    declared = {t: declared_types(schema_links, m) for t, m in MEETING_LINK_TYPES.items()}
    declared = {t: d for t, d in declared.items() if d}
    if not declared:
        return []
    judged: list[tuple[Any, str, list[Holding]]] = []
    for row, _prop in _values(conn, MEETING_PROPS, tuple(t + ":" for t in declared)):
        for address in addresses(row["value"]):
            outcome, tier, holdings = parties.judge(address, own)
            counts["meeting_" + outcome] += 1
            if tier is not None:
                judged.append((row, tier, holdings))
    targets = {t for _r, _t, hs in judged for h in hs
               for t in (h.party, *parties.twins.get(h.party, ()))}
    versions = _versions(conn, targets)
    out: dict[str, dict[str, Any]] = {}
    for row, tier, holdings in sorted(judged, key=lambda j: j[1] != "corroborated"):
        for r in link_rows(row, tier, holdings, parties, versions,
                           declared[row["object_id"].split(":", 1)[0]], role="attendee"):
            key = r["from_object"] + "|" + r["link_type"] + "|" + r["to_object"]
            if key not in out:
                r["link_id"] = PREFIX + _digest(r["from_object"], r["link_type"],
                                                r["to_object"], "meeting")
                out[key] = r
    return [out[k] for k in sorted(out)]


def sender_parties_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``post_project`` step (module docstring)."""
    conn = builder.connection
    conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                 (PREFIX, _PREFIX_END))
    declared = declared_types(getattr(projector, "links", {}) or {})
    counts: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    if not declared:
        return {"counts": {}, "link_tiers": {}}
    parties = read_parties(conn)
    own = own_domains(projector)
    targets = {h.party for hs in parties.by_domain.values() for h in hs}
    targets |= {t for p in targets for t in parties.twins.get(p, ())}
    versions = _versions(conn, targets)
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if not pending:
            return
        rejected = set()
        for email in sorted({r["from_object"] for r in pending}):
            rejected |= {(email, r[0], r[1]) for r in _q(
                conn, "SELECT to_object, link_type FROM link_rows WHERE from_object = ? AND "
                      "tier = 'contradicted'", [email])}
        kept = [r for r in pending
                if (r["from_object"], r["to_object"], r["link_type"]) not in rejected]
        counts["rejected"] += len(pending) - len(kept)
        if kept:
            builder.add("links", kept)
        for r in kept:
            tiers[r["tier"]] += 1
        pending.clear()

    for message, outcome, tier, holdings in judge_messages(conn, parties, own):
        named: list[Holding] = []
        if tier is None and outcome in ("shared_by_parties", "no_party"):
            named = named_parties(conn, message["object_id"], parties)
            if named:
                found = addresses(message["value"])
                outcome, tier, holdings = parties.by_named(found[0], outcome, holdings, named)
        counts[outcome] += 1
        if tier is None:
            continue
        missing = {t for h in holdings for t in (h.party, *parties.twins.get(h.party, ()))
                   if t not in versions}
        if missing:
            versions.update(_versions(conn, missing))
        pending.extend(link_rows(message, tier, holdings, parties, versions, declared,
                                 named=named))
        if len(pending) >= BATCH:
            flush()
    flush()
    pending.extend(meeting_rows(conn, parties, own, getattr(projector, "links", {}) or {},
                                counts))
    flush()
    return {"counts": dict(counts), "link_tiers": dict(tiers)}


__all__ = ["LINK_TYPES", "MEETING_LINK_TYPES", "meeting_rows", "PREFIX", "Holding", "Parties", "declared_types", "judge_messages",
           "link_rows", "name_fits_domain", "named_parties", "own_domains", "read_parties",
           "sender_parties_step"]
