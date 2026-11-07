"""Identifier mention leads in mail, attachments, Teams, SharePoint, meetings and Planner
raised to a firm link beside them when the identifier is exact and names one record.

One ``post_project`` step (``operating/build_steps.py`` ``unique_mentions``; steps run in
name order, so after ``mentions`` and ``reviews``), link id prefix ``PREFIX``,
self-contained: it reads the current store only, removes the rows it wrote before and
writes them again, so it runs in a full build or alone on a store in place (the step
migration). Mission Control tasks are left to ``task_ids`` (``ontology/mc_task_links.py``),
which reads their text.

* Inputs: the current identifier mention leads of the mentions step (``mention:`` links,
  ``binding_kind`` mention, tier lead) from every type but ``MCTask``, their reason (the
  identifier kind, the normalized value, the span written, the mention group); the
  candidates' confirming ``same_record`` links; a person's rejections (``contradicted``
  links of a pair).
* A **corroborated** link is written beside a lead when, for its mention group:
  - the kind is an anchor kind (``ontology/schema/identifiers.json`` ``identity: anchor``)
    of a record type of the schema (``object_type``); its own-type candidates only are
    raised (an invoice carrying an order number as a reference is not the order);
  - the value is written as it is normalized: the span written is as long as the
    normalized value (a number a label assembles, ``PO 352``, is not), and it holds a
    letter and at least ``MIN_DIGITS`` digits (a bare number, a short code or a count is
    never firm);
  - the own-type candidates are one record: one object, or objects each two of which a
    confirming ``same_record`` link joins (a Portal order and its FileMaker twin).
  The lead stays; the firm link cites it.
* A mention of a purchase order number several orders hold (own-type candidates of more
  than one record) in a message (``Email``) is raised the same way, for the candidates of
  one order only, when ``ontology.po_mention_resolve`` picks that order from the message's
  date and the supplier its conversation's outside senders are; the firm link's reason
  says why (``number_disambiguated``), and its evidence adds the messages whose sender
  decided it. Not decisive: no firm link (the leads stay, ambiguous).

Stdlib only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
import json
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ontology import identifiers as ids
from ontology import mentions
from ontology.posting_links import one_record

PREFIX = "crb-mid:"
LEAD_PREFIX = "mention:"
FIRM = "corroborated"
SKIP_TYPES = ("MCTask",)
MIN_DIGITS = 4
RESOLVED_TYPE = "PurchaseOrder"   # the record type a reused number is resolved for
MAIL_PREFIX = "Email:"
BATCH = 2000


def _end(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _digest(*parts: Any) -> str:
    return sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()[:32]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _q(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    return mentions._query(conn, sql, list(params)).fetchall()


def anchor_types(object_types: Iterable[str]) -> dict[str, str]:
    """anchor identifier kind -> the record type of the schema it identifies."""
    present = set(object_types)
    out = {}
    for kind in ids.kinds():
        spec = ids.kind_spec(kind)
        if spec.get("identity") == "anchor" and spec.get("object_type") in present:
            out[kind] = spec["object_type"]
    return out


def written_exactly(mention: Mapping[str, Any]) -> bool:
    """The value is written as normalized, and is a code (a letter and enough digits)."""
    value = str(mention.get("normalized") or "")
    span = mention.get("span") or ()
    if len(span) != 2 or span[1] - span[0] != len(value):
        return False
    return any(ch.isalpha() for ch in value) and sum(ch.isdigit() for ch in value) >= MIN_DIGITS


def _from_objects(conn: Any) -> Iterator[str]:
    """The objects holding mention leads, by the from end of the mention link prefix."""
    for (oid,) in mentions._query(
            conn, "SELECT DISTINCT from_object FROM link_rows WHERE link_id >= ? AND "
                  "link_id < ? AND tier = 'lead'", [LEAD_PREFIX, _end(LEAD_PREFIX)]):
        if oid.split(":", 1)[0] not in SKIP_TYPES:
            yield oid


def judge_groups(conn: Any, leads: Sequence[Any], owner: Mapping[str, str],
                 counts: Counter, pending: list | None = None) -> list[Any]:
    """The leads of ``leads`` (one object's) that get a firm link beside them; a purchase
    order mention of several records in a message is added to ``pending`` (resolved by
    ``resolved_rows``)."""
    groups: dict[str, list[tuple[Any, Mapping[str, Any]]]] = defaultdict(list)
    for lead in leads:
        reason = json.loads(lead["reason"] or "[]")
        mention = next((f for f in reason if isinstance(f, dict)
                        and f.get("feature") == "identifier_mention"), None)
        if mention is not None:
            groups[str(mention.get("mention_group"))].append((lead, mention))
    out = []
    for _group, members in sorted(groups.items()):
        _lead, mention = members[0]
        own_type = owner.get(mention.get("identifier_kind"))
        if own_type is None:
            counts["not_anchor"] += 1
            continue
        if not written_exactly(mention):
            counts["not_exact"] += 1
            continue
        own = [l for l, _m in members if l["to_object"].split(":", 1)[0] == own_type]
        if not own:
            counts["no_record_of_kind"] += 1
            continue
        if not one_record(conn, [l["to_object"] for l in own]):
            if pending is not None and own_type == RESOLVED_TYPE \
                    and own[0]["from_object"].startswith(MAIL_PREFIX):
                pending.append(([dict(l) for l in own], dict(mention)))
            counts["several_records"] += 1
            continue
        counts["firm"] += 1
        out += own
    return out


def firm_row(lead: Any, picked: Mapping[str, Any] | None = None,
             handles: Sequence[str] = ()) -> dict[str, Any]:
    """The firm link beside ``lead``: its one record, or the order ``picked`` (the
    ``number_disambiguated`` feature) with the deciding messages' ``handles``."""
    reason = [*json.loads(lead["reason"] or "[]"),
              {"feature": "exact_identifier", "agrees": True},
              dict(picked) if picked else {"feature": "unique_record", "agrees": True},
              {"feature": "corroborates", "agrees": True, "binding_kind": "mention",
               "link_id": lead["link_id"]}]
    return {"link_id": PREFIX + _digest(lead["link_id"]),
            "link_type": lead["link_type"], "from_object": lead["from_object"],
            "to_object": lead["to_object"], "tier": FIRM, "binding_kind": mentions.BINDING_KIND,
            "link_contract": None, "reason": _canon(reason), "why_not": "[]",
            "evidence_handles": _handles(lead["evidence_handles"], handles),
            "reviewed_by": None,
            "reviewed_at": None, "review_action_id": None, "markings": lead["markings"],
            "recorded_at": lead["recorded_at"], "superseded_at": None}


def _handles(stored: Any, extra: Sequence[str]) -> Any:
    if not extra:
        return stored
    found = json.loads(stored or "[]")
    return _canon([*found, *(h for h in sorted(set(extra)) if h not in found)])


def _rejected(conn: Any, oid: str) -> set[tuple[str, str]]:
    return {(r[0], r[1]) for r in _q(
        conn, "SELECT to_object, link_type FROM link_rows WHERE from_object = ? AND "
              "tier = 'contradicted'", [oid])}


def resolved_rows(conn: Any, pending: Sequence[tuple[Sequence[Any], Mapping[str, Any]]],
                  counts: Counter, own: Sequence[str], resolver: Any = None
                  ) -> Iterator[dict[str, Any]]:
    """The firm rows of the reused-number purchase order mentions ``pending`` (each: one
    message's own-type leads of one mention group, and the mention) that
    ``ontology.po_mention_resolve`` resolves; reasons counted ``resolved:<reason>`` and
    ``ambiguous:<reason>``."""
    from ontology import po_mention_resolve as pmr

    if not pending:
        return
    if resolver is None:
        if not own:
            counts["ambiguous:own_domains_unknown"] += len(pending)
            return
        resolver = pmr.Resolver(conn, own)
    mails = sorted({own_leads[0]["from_object"] for own_leads, _m in pending})
    facts = _mail_facts(conn, mails)
    for own_leads, mention in pending:
        mail = own_leads[0]["from_object"]
        sent, senders = facts.get(mail, (None, {}))
        found = resolver.mail(sent, senders)
        # every record a lead of this mention reaches is a candidate, holder or not
        picked = resolver.resolve_linked(pmr.number_key(mention.get("normalized")), found,
                                         [l["to_object"] for l in own_leads])
        chosen = [l for l in own_leads if picked.order is not None
                  and l["to_object"] in picked.order.records]
        if not chosen:
            counts["ambiguous:" + (picked.reason if picked.order is None
                                   else "pick_not_linked")] += 1
            continue
        counts["resolved:" + picked.reason] += 1
        handles = _message_handles(conn, found.handles)
        rejected = _rejected(conn, mail)
        for lead in chosen:
            if (lead["to_object"], lead["link_type"]) in rejected:
                counts["rejected"] += 1
                continue
            yield firm_row(lead, picked.feature(), handles)


def _mail_facts(conn: Any, mails: Sequence[str]) -> dict[str, tuple[Any, dict[str, Any]]]:
    """message -> (its sent time, {message id: sender} of it and its conversation)."""
    def props(oids: Sequence[str], names: Sequence[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = defaultdict(dict)
        oids = sorted(set(oids))
        for start in range(0, len(oids), BATCH):
            for oid, prop, value in mentions._query(
                    conn, "SELECT object_id, property, value FROM object_values WHERE "
                          "superseded_at IS NULL AND property IN (SELECT value FROM "
                          "json_each(?)) AND object_id IN (SELECT value FROM json_each(?))",
                    [json.dumps(list(names)), json.dumps(oids[start:start + BATCH])]):
                if value not in (None, ""):
                    out[oid][prop] = value
        return out
    own = props(mails, ("conversation_id", "sent_at", "sender"))
    wanted = {row.get("conversation_id") for row in own.values()} - {None}
    thread: dict[str, list[str]] = defaultdict(list)
    if wanted:
        for oid, value in mentions._query(
                conn, "SELECT object_id, value FROM object_values WHERE property = "
                      "'conversation_id' AND superseded_at IS NULL AND object_id LIKE "
                      "'Email:%'", []):
            if value in wanted:
                thread[value].append(oid)
    peers = props([o for oids in thread.values() for o in oids], ("sender",))
    out = {}
    for mail in mails:
        row = own.get(mail, {})
        members = thread.get(row.get("conversation_id"), [])
        senders = {o: peers.get(o, {}).get("sender") for o in members}
        senders[mail] = row.get("sender")
        out[mail] = (row.get("sent_at"), senders)
    return out


def _message_handles(conn: Any, oids: Sequence[str]) -> list[str]:
    if not oids:
        return []
    return sorted(r[0] for r in mentions._query(
        conn, "SELECT l1_handle FROM object_versions WHERE superseded_at IS NULL AND "
              "object_id IN (SELECT value FROM json_each(?))", [json.dumps(list(oids))]))


def firm_links(conn: Any, object_types: Iterable[str], counts: Counter,
               objects: Iterable[str] | None = None, *, own: Sequence[str] = (),
               resolver: Any = None, pending: list | None = None
               ) -> Iterator[dict[str, Any]]:
    """The firm rows (module docstring), outcomes counted in ``counts``; ``objects``
    limits the from ends; ``own``: our mail domains (the reused-number resolution reads
    outside senders; none known: nothing resolves). With ``pending`` given, the
    reused-number mentions are added to it and left to the caller (``resolved_rows``)."""
    owner = anchor_types(object_types)
    defer = pending is not None
    pending = pending if defer else []
    for oid in (objects if objects is not None else _from_objects(conn)):
        leads = _q(conn, "SELECT * FROM links WHERE from_object = ? AND link_id >= ? AND "
                         "link_id < ? AND tier = 'lead' AND superseded_at IS NULL",
                   [oid, LEAD_PREFIX, _end(LEAD_PREFIX)])
        if not leads:
            continue
        chosen = judge_groups(conn, leads, owner, counts, pending)
        if not chosen:
            continue
        rejected = _rejected(conn, oid)
        for lead in chosen:
            if (lead["to_object"], lead["link_type"]) in rejected:
                counts["rejected"] += 1
                continue
            yield firm_row(lead)
    if not defer:
        yield from resolved_rows(conn, pending, counts, own, resolver)


def unique_mentions_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``unique_mentions`` step (module docstring)."""
    conn = builder.connection
    types = list((getattr(projector, "types", None) or {}).keys())
    counts: Counter[str] = Counter()
    from ontology.sender_parties import own_domains

    rows = list(firm_links(conn, types, counts, own=own_domains(projector)))
    conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                 (PREFIX, _end(PREFIX)))
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])
    counts["links"] = len(rows)
    return {"counts": dict(counts), "link_tiers": {FIRM: len(rows)} if rows else {}}


__all__ = ["PREFIX", "anchor_types", "firm_links", "judge_groups", "resolved_rows",
           "unique_mentions_step", "written_exactly"]
