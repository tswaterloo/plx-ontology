"""Document links firmed by their sender: a mail document's reference to a record is
``corroborated`` when the record's own party sent it (2026-11-02).

A ``post_project`` step (``operating/build_steps.py`` ``sender_corroboration``; after
``document_fields``, ``mentions`` and ``reviews``) over every document-field object type
whose ``attests`` link type (``ontology/schema/document_fields.json``) declares a
``mention`` binding allowing ``corroborated`` (today ``SupplierConfirmation.confirms``):

* **Carrier references** (``doccarrier:``): the message carrying a captured mail
  attachment names a record in its subject, its own (unquoted) body or the attachment's
  name (its stored ``Email.mentions_<type>`` links, an exact identifier, no veto), and
  that message names exactly one number of the kind: the document gets a ``lead`` link to
  that record (feature ``carrier_reference``, citing the message's span), unless its own
  text already links it there. A lead: a number in a subject is no proof by itself.
* **Firm links** (``docfirm:``): a current ``lead`` link of such a type (the document's
  own reference, or a carrier reference) gets a ``corroborated`` link beside it when
  both hold over the link's time:

  1. the reference is exact: the number read is an identifier the target holds of the
     same kind (``ontology.mentions.exact_candidate``), with no label/kind conflict and
     no number split at a column gap;
  2. the message carrying the document was sent from a domain of the target's own party
     (``party_domain``): a party a ``verified`` link from the target names (the order's
     supplier), or its ``same_record_as`` twins at a confirming tier, holds that domain as
     its website or a mail address (:data:`DOMAIN_PROPERTIES`), or one of its contacts
     (an object a ``verified`` link joins to the party) holds a mail address there; and
     the domain is distinctive: no shared mail provider (``ontology.mentions``) and held
     by no other party.

  Vetoes keep the lead: the sender is the own party (a colleague's forward); the message
  is dated before the target's order date; a person rejected the link (``contradicted``);
  one reference of the document reaches several targets that pass and are not one
  record's twins. A SharePoint file has no sender and stays a lead.

The lead stays beside the firm link, so a reader who may not read the party's or the
message's record (its handles are the firm link's evidence, its markings the link's: a
contact's mail address is ``pii``) still reads the lead. Rows a previous build wrote under
either prefix are removed first (an incremental build seeds projected links forward).
Stdlib only.
"""

from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
import re
from typing import Any, Callable, Iterable, Iterator, Mapping

from ontology import mentions
from ontology.store import canonical

FIRM_PREFIX, CARRIER_PREFIX = "docfirm:", "doccarrier:"
FIRM_TIER = mentions.CORROBORATED
# The party properties naming its domains: a website, or mail address(es).
DOMAIN_PROPERTIES = {"website": "website", "bc_email": "mail", "rep_email": "mail",
                     "csr_email": "mail", "email": "mail"}
CONFIRMING = ("verified", "reviewed", "corroborated")
_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_REFERENCE_FEATURES = ("document_reference", "carrier_reference")
_VETOES = {"label_kind_conflict", "quoted_history", "split_at_gap"}
MESSAGE_BINDING = "graph_mail.messages"


def _digest(*parts: Any) -> str:
    return sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()[:32]


def properties_read_by_name() -> set[str]:
    """The properties read store-wide by name, whatever type holds them (every holder of
    a domain): a step migration declares every type holding one as this step's input."""
    return set(DOMAIN_PROPERTIES)


def _domains(prop: str, value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    if DOMAIN_PROPERTIES[prop] == "website":
        found = mentions.website_domain(value)
        return {found} if found else set()
    return {d for d in (mentions.mail_domain(a) for a in _ADDRESS.findall(value)) if d}


def corroborable_types(schema_links: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """document object type -> its attests link type, for the profiles whose link type
    declares a ``mention`` binding allowing ``corroborated``."""
    from ontology.document_fields import profiles

    raising = mentions.corroborable_link_types(schema_links)
    return {p["object_type"]: p["attests"]["link_type"] for p in profiles()["profiles"]
            if (p.get("attests") or {}).get("link_type") in raising}


def message_of(object_id: str) -> str | None:
    """The ``Email`` object of the message carrying a mail document-field object (its
    source key ``[mailbox, message, attachment]``), else None (a SharePoint file, a
    container member)."""
    try:
        _type, binding, key = object_id.split(":", 2)
        source = json.loads(json.loads(key)["source_key"])
    except (ValueError, KeyError, TypeError):
        return None
    if not binding.startswith("graph_mail.") or not isinstance(source, list) \
            or len(source) != 3 or not all(isinstance(s, str) for s in source):
        return None
    return f"Email:{MESSAGE_BINDING}:" + canonical({"id": source[1], "mailbox": source[0]})


class _Facts:
    """Read access to the store being built, one connection."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[Any]:
        return mentions._query(self.conn, sql, list(params)).fetchall()

    def values(self, oid: str, props: Iterable[str]) -> list[Any]:
        props = list(props)
        return self.query("SELECT * FROM object_values WHERE object_id = ? AND property IN "
                          f"({','.join('?' * len(props))}) AND value IS NOT NULL ORDER BY "
                          "recorded_at, l1_handle", [oid, *props])

    def held_value(self, oid: str, prop: str, start: str, end: str | None) -> Any | None:
        rows = self.values(oid, [prop])
        by_value: dict[Any, list[Any]] = defaultdict(list)
        for row in rows:
            by_value[row["value"]].append(row)
        held = [rs for rs in by_value.values() if mentions._held(rs, start, end)]
        return held[0][-1] if len(held) == 1 else None


class _Parties:
    """Every party's domains (:data:`DOMAIN_PROPERTIES` of the party, its confirming
    ``same_record_as`` twins and the contacts a ``verified`` link joins to it) and which
    parties hold each domain, read once per build."""

    def __init__(self, facts: _Facts) -> None:
        self.facts = facts
        props = list(DOMAIN_PROPERTIES)
        self.cluster: dict[str, str] = {}
        twins = facts.query("SELECT from_object, to_object FROM links WHERE binding_kind = "
                            "'same_record' AND tier IN ('verified', 'reviewed', "
                            "'corroborated') AND superseded_at IS NULL")
        parent: dict[str, str] = {}

        def root(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x
        for a, b in twins:
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            parent[root(a)] = root(b)
        self._root = root
        self.holders: dict[str, set[str]] = defaultdict(set)
        holding = facts.query(
            "SELECT object_id, property, value FROM object_values WHERE property IN "
            f"({','.join('?' * len(props))}) AND value IS NOT NULL AND superseded_at IS NULL",
            props)
        contact_of: dict[str, set[str]] = defaultdict(set)
        for contact, party in facts.query(
                "SELECT from_object, to_object FROM links WHERE tier = 'verified' AND "
                "binding_kind <> 'mention' AND superseded_at IS NULL AND from_object IN "
                "(SELECT object_id FROM object_values WHERE property = 'email' AND value IS "
                "NOT NULL AND superseded_at IS NULL)"):
            contact_of[contact].add(party)
        for oid, prop, value in holding:
            owners = contact_of.get(oid) if prop == "email" else None
            for domain in _domains(prop, value):
                for owner in owners or (oid,):
                    self.holders[domain].add(root(owner))
        self.contact_of = contact_of

    def distinctive(self, domain: str, party: str) -> bool:
        if mentions.shared_mail_domain(domain):
            return False
        return self.holders.get(domain, set()) <= {self._root(party)}

    def match(self, target: str, sender: str, start: str, end: str | None
              ) -> tuple[dict[str, Any], list[str], list[str]] | None:
        """The ``party_domain`` feature when ``sender`` lies at or under a distinctive
        domain of ``target``'s party over [start, end): (feature, cited handles, markings)."""
        facts = self.facts
        parties: dict[str, list[Any]] = defaultdict(list)
        for link in facts.query(
                "SELECT * FROM links WHERE from_object = ? AND tier = 'verified' AND "
                "binding_kind NOT IN ('mention', 'same_record') ORDER BY link_id", [target]):
            parties[link["to_object"]].append(link)
        for party, links in sorted(parties.items()):
            if not mentions._held(links, start, end):
                continue
            members = sorted(o for o in {party} | {
                r[0] for r in facts.query(
                    "SELECT CASE WHEN from_object = ? THEN to_object ELSE from_object END "
                    "FROM links WHERE (from_object = ? OR to_object = ?) AND binding_kind = "
                    "'same_record' AND tier IN ('verified', 'reviewed', 'corroborated') AND "
                    "superseded_at IS NULL", [party, party, party])})
            contacts = sorted(c for c, ps in self.contact_of.items() if ps & set(members))
            holders = [(m, [p for p in DOMAIN_PROPERTIES if p != "email"]) for m in members] \
                + [(c, ["email"]) for c in contacts]
            for holder, props in holders:
                for row in facts.values(holder, props):
                    if not mentions._held([row], start, end):
                        continue
                    for domain in sorted(_domains(row["property"], row["value"])):
                        if mentions.domain_under(sender, domain) and \
                                self.distinctive(domain, party):
                            link = max(links, key=lambda r: r["recorded_at"])
                            feature = {"feature": "party_domain", "agrees": True,
                                       "citations": [mentions._value_cite(row)]}
                            return (feature, [row["l1_handle"],
                                              *json.loads(link["evidence_handles"])],
                                    [row["markings"], *(r["markings"] for r in links)])
        return None


def _own_domains(conn: Any) -> set[str]:
    from ontology.document_fields import profiles

    own = {n.lower() for n in profiles()["own_parties"]["names"] if "." in n}
    return own


def _reference(reason: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    return next((f for f in reason if f.get("feature") in _REFERENCE_FEATURES), {})


def _exact(reason: list[Mapping[str, Any]]) -> bool:
    ref = _reference(reason)
    candidate = mentions._candidate(reason)
    if candidate.get("via") == "reviewed_alias":
        return True
    return (candidate.get("via") == "identifier_index"
            and isinstance(ref.get("normalized"), str) and bool(ref["normalized"])
            and candidate.get("matched_kind") == ref.get("identifier_kind")
            and candidate.get("matched_normalized") == ref["normalized"])


def _carrier_links(facts: _Facts, doc_type: str, link_type: str, target_type: str,
                   email_link: str | None, *,
                   each: Callable[[list[dict[str, Any]]], Iterable[list[dict[str, Any]]]]
                   | None = None) -> list[dict[str, Any]]:
    """The ``doccarrier:`` lead rows of every current mail object of ``doc_type``.
    ``each``: the rows of each of the documents, in their order (``_carrier_rows`` of
    each by default)."""
    if email_link is None:
        return []
    docs = [dict(doc) for doc in facts.query(
        "SELECT o.object_id, v.l1_handle, v.digest, v.lineage, v.markings, "
        "v.recorded_at, v.superseded_at FROM objects o JOIN object_versions v "
        "ON v.object_id = o.object_id WHERE o.object_type = ? AND "
        "v.superseded_at IS NULL ORDER BY o.object_id", [doc_type])]
    found = (each(docs) if each is not None else
             (_carrier_rows(facts, doc, link_type, target_type, email_link) for doc in docs))
    return [row for rows in found for row in rows]


def _carrier_rows(facts: _Facts, doc: Mapping[str, Any], link_type: str, target_type: str,
                  email_link: str) -> list[dict[str, Any]]:
    """The ``doccarrier:`` lead rows of one mail document (a row of ``_carrier_links``)."""
    rows: list[dict[str, Any]] = []
    email = message_of(doc["object_id"])
    if email is None:
        return rows
    message = doc["l1_handle"].split("#", 1)[0]
    own = {r["to_object"] for r in facts.query(
        "SELECT to_object FROM links WHERE from_object = ? AND link_type = ? AND "
        "superseded_at IS NULL", [doc["object_id"], link_type])}
    named: dict[str, list[Any]] = defaultdict(list)
    for link in facts.query("SELECT * FROM links WHERE from_object = ? AND link_type = ? "
                            "AND superseded_at IS NULL ORDER BY link_id",
                            [email, email_link]):
        handles = json.loads(link["evidence_handles"])
        why = {f.get("feature") for f in json.loads(link["why_not"])}
        reason = json.loads(link["reason"])
        mention = next((f for f in reason if f.get("feature") == "identifier_mention"), {})
        cites = mention.get("citations") or []
        # this message version's own text, or this attachment's name
        in_carrier = handles[:1] == [message] and all(
            c.get("l1_handle") in (message, doc["l1_handle"].split("&sha256=", 1)[0],
                                   doc["l1_handle"]) for c in cites)
        if not in_carrier or why & _VETOES or not mentions.exact_candidate(reason):
            continue
        named[mention["normalized"]].append(link)
    if len(named) != 1:
        return rows   # none, or several numbers: the message does not say which
    (normalized, links), = named.items()
    for link in links:
        target = link["to_object"]
        if target in own or not target.startswith(target_type + ":"):
            continue
        window = mentions._overlap((doc["recorded_at"], doc["superseded_at"]),
                                   (link["recorded_at"], link["superseded_at"]))
        if window is None:
            continue
        reason = json.loads(link["reason"])
        mention = next(f for f in reason if f.get("feature") == "identifier_mention")
        feature = {"feature": "carrier_reference", "agrees": True,
                   "identifier_kind": mention.get("identifier_kind"),
                   "normalized": normalized, "citations": mention.get("citations"),
                   "mention_group": _digest(doc["object_id"], doc["l1_handle"], email,
                                            normalized)}
        why_not = [{"feature": "machine_read", "agrees": None,
                    "detail": "read from the message carrying the document (its subject, "
                              "body or attachment name), not from the document; a lead "
                              "until corroborated or reviewed"},
                   {"feature": "external_assertion", "agrees": False,
                    "cannot_confirm": list(mentions.EXTERNAL_UNCONFIRMABLE)}]
        rows.append({
            "link_id": CARRIER_PREFIX + _digest(doc["object_id"], doc["l1_handle"],
                                                link["link_id"]),
            "link_type": link_type, "from_object": doc["object_id"], "to_object": target,
            "tier": mentions.TIER, "binding_kind": mentions.BINDING_KIND,
            "link_contract": None,
            "reason": json.dumps([feature, mentions._candidate(reason)], sort_keys=True,
                                 separators=(",", ":")),
            "why_not": json.dumps(why_not, sort_keys=True, separators=(",", ":")),
            "evidence_handles": json.dumps(list(dict.fromkeys(
                [doc["l1_handle"], *json.loads(link["evidence_handles"])])),
                separators=(",", ":")),
            "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
            "markings": mentions._marks(doc["markings"], link["markings"]),
            "recorded_at": window[0], "superseded_at": window[1]})
    return rows


def _screen(lead: Mapping[str, Any], rejected: set[tuple[str, str]]) -> tuple[str, Any]:
    """A lead refused before its sender is read (``("count", reason)``), else
    ``("sender", the Email object carrying it)``."""
    reason, why = json.loads(lead["reason"]), json.loads(lead["why_not"])
    if {f.get("feature") for f in why} & _VETOES or not _exact(reason):
        return "count", "not_exact"
    if (lead["from_object"], lead["to_object"]) in rejected:
        return "count", "rejected"
    email = message_of(lead["from_object"])
    if email is None:
        return "count", "no_sender"
    return "sender", email


def _sender(facts: _Facts, email: str, window: tuple[str, str | None]
            ) -> tuple[str, Any] | None:
    """(domain, value row) of the sender of ``email`` held over ``window`` (the first lead
    of the message's: one sender per message), else None."""
    row = facts.held_value(email, "sender", *window)
    domain = mentions.mail_domain(row["value"]) if row is not None else None
    return (domain, row) if domain else None


def _verdict(facts: _Facts, parties: _Parties, lead: Mapping[str, Any],
             sender: tuple[str, Any] | None, own: set[str]) -> tuple[Any, ...]:
    """What the firm step decides of a lead whose message's sender is ``sender``:
    ``("count", reason)``, or ``("passed", group, (party feature, sender row))``."""
    if sender is None:
        return "count", "no_sender"
    domain, sender_row = sender
    if any(mentions.domain_under(domain, d) for d in own):
        return "count", "own_sender"
    email = message_of(lead["from_object"])
    start, end = lead["recorded_at"], lead["superseded_at"]
    sent = next((mentions.day(r["value"]) for p in ("sent_at", "received_at")
                 if (r := facts.held_value(email, p, start, end)) is not None), None)
    ordered = facts.held_value(lead["to_object"], "order_date", start, end)
    if ordered is not None and mentions.date_order(
            mentions.day(ordered["value"]), sent)["status"] == "message_predates_target":
        return "count", "message_predates_target"
    found = parties.match(lead["to_object"], domain, start, end)
    if found is None:
        return "count", "no_party_domain"
    group = _reference(json.loads(lead["reason"])).get("mention_group") or lead["link_id"]
    return "passed", group, (found, sender_row)


def _verdicts(facts: _Facts, parties: _Parties, leads: list[dict[str, Any]],
              screened: list[tuple[str, Any]], windows: Mapping[str, tuple[str, str | None]],
              own: set[str]) -> Iterator[tuple[Any, ...]]:
    """:func:`_verdict` of each lead, in order (its screen's when it was refused)."""
    senders: dict[str, tuple[str, Any] | None] = {}
    for lead, (kind, value) in zip(leads, screened):
        if kind == "count":
            yield kind, value
            continue
        if value not in senders:
            senders[value] = _sender(facts, value, windows[value])
        yield _verdict(facts, parties, lead, senders[value], own)


def _firm_links(facts: _Facts, parties: _Parties | None, link_type: str, own: set[str],
                extra: list[dict[str, Any]], *,
                judge: Callable[..., Iterable[tuple[Any, ...]]] | None = None
                ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The ``docfirm:`` rows of the current leads of ``link_type`` and ``extra`` (this
    build's carrier leads) and why each other lead stays one. ``judge``: the verdicts of
    the leads, in order, given the leads, their screens and the window each message's
    sender is read over (:func:`_verdicts` over ``parties`` by default)."""
    counts = defaultdict(int)
    leads = [dict(r) for r in facts.query(
        "SELECT * FROM links WHERE link_type = ? AND tier = ? AND binding_kind = ? AND "
        "superseded_at IS NULL AND substr(link_id, 1, ?) <> ? ORDER BY link_id",
        [link_type, mentions.TIER, mentions.BINDING_KIND, len(CARRIER_PREFIX),
         CARRIER_PREFIX])] + [dict(r) for r in extra]
    rejected = {(r["from_object"], r["to_object"]) for r in facts.query(
        "SELECT from_object, to_object FROM links WHERE link_type = ? AND tier = "
        "'contradicted'", [link_type])}
    screened = [_screen(lead, rejected) for lead in leads]
    # a message's sender is read once, over the window of its first lead read so far
    windows: dict[str, tuple[str, str | None]] = {}
    for lead, (kind, value) in zip(leads, screened):
        if kind == "sender" and value not in windows:
            windows[value] = (lead["recorded_at"], lead["superseded_at"])
    verdicts = (judge(leads, screened, windows) if judge is not None
                else _verdicts(facts, parties, leads, screened, windows, own))
    passed: dict[tuple[str, str], list[tuple[dict, tuple]]] = defaultdict(list)
    for lead, verdict in zip(leads, verdicts, strict=True):
        if verdict[0] == "count":
            counts[verdict[1]] += 1
        else:
            passed[(lead["from_object"], verdict[1])].append((lead, verdict[2]))
    rows = []
    for (_doc, _group), found in sorted(passed.items()):
        targets = sorted({lead["to_object"] for lead, _ in found})
        if len(targets) > 1 and not _one_record(facts, targets):
            counts["several_targets"] += len(found)
            continue
        for lead, ((feature, handles, marks), sender_row) in found:
            reason = json.loads(lead["reason"]) + [
                feature, {"feature": "sender_domain", "agrees": True,
                          "citations": [mentions._value_cite(sender_row)]}]
            rows.append({**lead, "link_id": FIRM_PREFIX + _digest(lead["link_id"]),
                         "tier": FIRM_TIER,
                         "reason": json.dumps(reason, sort_keys=True, separators=(",", ":")),
                         "evidence_handles": json.dumps(list(dict.fromkeys(
                             [*json.loads(lead["evidence_handles"]), *handles,
                              sender_row["l1_handle"]])), separators=(",", ":")),
                         "markings": mentions._marks(lead["markings"], *marks,
                                                     sender_row["markings"])})
            counts["firm"] += 1
    return rows, dict(counts)


def _one_record(facts: _Facts, targets: list[str]) -> bool:
    """Whether every two of ``targets`` are joined by a confirming ``same_record`` link."""
    for i, a in enumerate(targets):
        for b in targets[i + 1:]:
            if not facts.query("SELECT 1 FROM links WHERE ((from_object = ? AND to_object = ?) "
                               "OR (from_object = ? AND to_object = ?)) AND binding_kind = "
                               "'same_record' AND tier IN ('verified', 'reviewed', "
                               "'corroborated') AND superseded_at IS NULL LIMIT 1",
                               [a, b, b, a]):
                return False
    return True


def sender_corroboration_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``post_project`` step: carrier leads, then firm links (module docstring)."""
    conn = builder.connection
    schema_links = getattr(projector, "links", {}) or {}
    for prefix in (FIRM_PREFIX, CARRIER_PREFIX):
        conn.execute("DELETE FROM links WHERE substr(link_id, 1, ?) = ?", (len(prefix), prefix))
    facts = _Facts(conn)
    declared = mentions.mention_link_types(schema_links)
    parties: _Parties | None = None
    own = _own_domains(conn)
    report: dict[str, Any] = {"counts": {}, "link_tiers": {}}
    for doc_type, link_type in sorted(corroborable_types(schema_links).items()):
        target_type = (schema_links.get(link_type) or {}).get("to")
        email_link = declared.get(("Email", target_type))
        carrier = _carrier_links(facts, doc_type, link_type, target_type, email_link)
        if carrier:
            builder.add("links", carrier)
        if parties is None:
            parties = _Parties(facts)
        firm, counts = _firm_links(facts, parties, link_type, own, carrier)
        if firm:
            builder.add("links", firm)
        report["counts"][link_type] = {"carrier_leads": len(carrier), **counts}
        tiers = report["link_tiers"]
        tiers[mentions.TIER] = tiers.get(mentions.TIER, 0) + len(carrier)
        tiers[FIRM_TIER] = tiers.get(FIRM_TIER, 0) + len(firm)
    return report


__all__ = ["CARRIER_PREFIX", "DOMAIN_PROPERTIES", "FIRM_PREFIX", "corroborable_types",
           "message_of", "properties_read_by_name", "sender_corroboration_step"]
