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
    of record types of the schema (``identifiers.firm_types``: its ``object_type`` and the
    types its ``firm_mention.types`` declares, a receipt lab id's receipt line); only
    candidates of those own types are raised (an invoice carrying an order number as a
    reference is not the order), each own type judged on its own;
  - the value is written as it is normalized: the span written is as long as the
    normalized value (a number a label assembles, ``PO 352``, is not), and it holds at
    least ``MIN_DIGITS`` digits and a letter, or matches whole the kind's declared
    ``firm_mention.shape`` (a lab id ``26161-1``, read only behind its label): a bare
    number, a short code or a count is never firm;
  - an own type's candidates are one record: one object, or objects each two of which a
    confirming ``same_record`` link joins (a Portal order and its FileMaker twin).
  - no lead of the group carries a veto in its ``why_not`` (``VETOES``): a span in
    quoted reply/forward history (``quoted_history``: someone else wrote it, so a number
    seen only in a reply's history is a lead, never firmed like the message's own text),
    a label of another kind reading the number (``label_kind_conflict``: a secondary
    reading is only ever a lead) or a number split from its label (``split_at_gap``).
    A why_not that cannot be read is a veto too. The same number in the message's own
    text is its own mention group, judged on its own.
  The lead stays; the firm link cites it.
* Never from a list: a mention in a field whose own leads (no quoted or secondary
  reading) name ``mentions.LIST_FILE_MIN`` or more distinct values of the own type's
  anchor kinds, or whose lead says ``list_file`` (its text names that many), gets no firm
  link; a number in the document's own name (file name, subject) always may. The leads
  stay leads, saying ``list_file`` (a tracker or statement listing 300 orders is no order's
  own document, 11-14 audit: 60% of sampled attachment-order links).
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
from itertools import islice
import json
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ontology import identifiers as ids
from ontology import mention_occurrences as MO
from ontology import mentions
from ontology import step_reads as SR
from ontology.posting_links import one_record

PREFIX = "crb-mid:"
LEAD_PREFIX = "mention:"
FIRM = "corroborated"
SKIP_TYPES = ("MCTask",)
MIN_DIGITS = 4
RESOLVED_TYPE = "PurchaseOrder"   # the record type a reused number is resolved for
MAIL_PREFIX = "Email:"
# why_not features that keep a mention a lead (``ontology.mentions``, as
# ``ontology.sender_corroboration`` reads them)
VETOES = frozenset({"quoted_history", "label_kind_conflict", "split_at_gap"})
BATCH = 2000
WINDOW = 500      # objects whose leads and rejected pairs are read together


def _end(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _digest(*parts: Any) -> str:
    return sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()[:32]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _q(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    return mentions._query(conn, sql, list(params)).fetchall()


def anchor_types(object_types: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """anchor identifier kind -> the record types of the schema it identifies
    (``identifiers.firm_types``, those the schema has)."""
    present = set(object_types)
    out = {}
    for kind in ids.kinds():
        if ids.kind_spec(kind).get("identity") != "anchor":
            continue
        types = tuple(t for t in ids.firm_types(kind) if t in present)
        if types:
            out[kind] = types
    return out


def written_exactly(mention: Mapping[str, Any]) -> bool:
    """The value is written as normalized, and is a code: enough digits and a letter, or
    the whole value in its kind's declared ``firm_mention.shape``."""
    value = str(mention.get("normalized") or "")
    span = mention.get("span") or ()
    if len(span) != 2 or span[1] - span[0] != len(value):
        return False
    if sum(ch.isdigit() for ch in value) < MIN_DIGITS:
        return False
    if any(ch.isalpha() for ch in value):
        return True
    try:
        shape = ids.firm_shape(str(mention.get("identifier_kind")))
    except KeyError:   # a kind the schema no longer declares: never firm
        return False
    return shape is not None and shape.fullmatch(value) is not None


def vetoed(lead: Any) -> str | None:
    """The veto a lead's ``why_not`` states (``VETOES``), ``unreadable`` when it cannot be
    read; None when it states none."""
    try:
        raw = lead["why_not"]
        found = json.loads(raw) if isinstance(raw, str) else raw
    except (KeyError, IndexError, TypeError, ValueError):
        return "unreadable"
    if found is None:
        found = []
    if not isinstance(found, list):
        return "unreadable"
    for item in found:
        if not isinstance(item, Mapping):
            return "unreadable"
        if item.get("feature") in VETOES:
            return str(item["feature"])
    return None


def _from_objects(conn: Any) -> Iterator[str]:
    """The objects holding mention leads, by the from end of the mention link prefix."""
    for (oid,) in mentions._query(
            conn, "SELECT DISTINCT from_object FROM link_rows WHERE link_id >= ? AND "
                  "link_id < ? AND tier = 'lead'", [LEAD_PREFIX, _end(LEAD_PREFIX)]):
        if oid.split(":", 1)[0] not in SKIP_TYPES:
            yield oid


def _judgeable(leads: Sequence[Any], owner: Mapping[str, Sequence[str]], counts: Counter
               ) -> Iterator[tuple[list[tuple[Any, Mapping[str, Any]]], Mapping[str, Any],
                                   list[tuple[str, list[Any]]]]]:
    """The mention groups of ``leads`` (one object's), in group order, that pass the
    kind, written-exactly, veto (``VETOES``) and list-file tests: (members, the mention,
    [(own type, its leads)])."""
    groups: dict[str, list[tuple[Any, Mapping[str, Any]]]] = defaultdict(list)
    # list key (field, own type) -> the values the document's own text names there, as
    # ``mentions.listed_records`` counts them (no quoted or secondary reading)
    named: dict[tuple[Any, Any, Any], set[tuple[str, str]]] = defaultdict(set)
    said: set[str] = set()   # mention groups a lead of which says list_file
    for lead in MO.views(leads):   # a collapsed row: each occurrence it stands for
        reason = json.loads(lead["reason"] or "[]")
        mention = next((f for f in reason if isinstance(f, dict)
                        and f.get("feature") == "identifier_mention"), None)
        if mention is None:
            continue
        group = str(mention.get("mention_group"))
        groups[group].append((lead, mention))
        why_not = _features(lead["why_not"] if "why_not" in lead.keys() else None)
        if mentions.LIST_FILE in why_not:
            said.add(group)
        kind_types = owner.get(mention.get("identifier_kind"))
        if isinstance(kind_types, str):
            kind_types = (kind_types,)
        if kind_types and mention.get("normalized") and not why_not & _UNCOUNTED:
            for own_type in kind_types:
                named[_list_key(mention, own_type)].add(
                    (mention["identifier_kind"], str(mention["normalized"])))
    for _group, members in sorted(groups.items()):
        _lead, mention = members[0]
        own_types = owner.get(mention.get("identifier_kind"))
        if own_types is None:
            counts["not_anchor"] += 1
            continue
        if isinstance(own_types, str):   # one type, as ``anchor_types`` gave it before
            own_types = (own_types,)
        if not written_exactly(mention):
            counts["not_exact"] += 1
            continue
        veto = next((v for l, _m in members if (v := vetoed(l)) is not None), None)
        if veto is not None:
            counts[f"veto_{veto}"] += 1
            continue
        listed = [t for t in own_types if _field(mention) not in mentions.TITLE_FIELDS and (
            _group in said or len(named[_list_key(mention, t)]) >= mentions.LIST_FILE_MIN)]
        if listed and len(listed) == len(own_types):
            counts["list_file"] += 1   # a list names the record; it is not its document
            continue
        by_type = [(t, [l for l, _m in members if l["to_object"].split(":", 1)[0] == t])
                   for t in own_types if t not in listed]
        by_type = [(t, own) for t, own in by_type if own]
        if not by_type:
            counts["no_record_of_kind"] += 1
            continue
        yield members, mention, by_type


def _decide(judgeable: Iterable[tuple[list[tuple[Any, Mapping[str, Any]]], Mapping[str, Any],
                                      list[tuple[str, list[Any]]]]],
            is_one: Any, counts: Counter, pending: list | None) -> list[Any]:
    out = []
    for members, mention, by_type in judgeable:
        firm: set[tuple[str, str]] = set()   # (link id, occurrence) of each firm view
        for own_type, own in by_type:
            if not is_one([l["to_object"] for l in own]):
                if pending is not None and own_type == RESOLVED_TYPE \
                        and own[0]["from_object"].startswith(MAIL_PREFIX):
                    pending.append(([dict(l) for l in own], dict(mention)))
                counts["several_records"] += 1
                continue
            counts["firm"] += 1
            firm.update((l["link_id"], l.get(MO.OCCURRENCE)) for l in own)
        out += [l for l, _m in members
                if (l["link_id"], l.get(MO.OCCURRENCE)) in firm]   # in the leads' order
    return out


# why_not features of a lead whose number is not the document's own text: not counted
_UNCOUNTED = frozenset({"quoted_history", "label_kind_conflict"})


def _features(why_not: Any) -> set[str]:
    try:
        found = json.loads(why_not) if why_not else []
    except ValueError:
        return set()
    return {f.get("feature") for f in found if isinstance(f, dict)} if isinstance(
        found, list) else set()


def _field(mention: Mapping[str, Any]) -> Any:
    cites = mention.get("citations")
    cite = cites[0] if isinstance(cites, list) and cites and isinstance(cites[0], dict) else {}
    return cite.get("field_path", mention.get("field_path"))


def _list_key(mention: Mapping[str, Any], own_type: str) -> tuple[Any, Any, Any]:
    """``mentions.list_key`` of a lead's mention: its cited field and the own type."""
    cites = mention.get("citations")
    cite = cites[0] if isinstance(cites, list) and cites and isinstance(cites[0], dict) else {}
    return mentions.list_key(cite.get("l1_handle", mention.get("citation")), _field(mention),
                             own_type)


def judge_groups(conn: Any, leads: Sequence[Any], owner: Mapping[str, Sequence[str]],
                 counts: Counter, pending: list | None = None) -> list[Any]:
    """The leads of ``leads`` (one object's) that get a firm link beside them; a purchase
    order mention of several records in a message is added to ``pending`` (resolved by
    ``resolved_rows``). One ``same_record`` read per candidate: :func:`firm_links`
    judges a window of objects with one read for all its candidates instead."""
    return _decide(_judgeable(leads, owner, counts), lambda oids: one_record(conn, oids),
                   counts, pending)


def same_record_joins(conn: Any, candidate_sets: Iterable[Sequence[str]]
                      ) -> dict[tuple[str, str], set[str]]:
    """(object, its ``<type>.same_record_as`` link type) -> the objects a confirming
    link of that type joins it to (either end), for every set of ``candidate_sets`` of
    two or more objects, read in one statement per :data:`step_reads.CHUNK` objects
    (NW-13; ``posting_links.one_record`` read one per object). Every two members of a
    set are both in it, so the links leaving the members are all the joins between
    them."""
    wanted: set[str] = set()
    types: set[str] = set()
    for oids in candidate_sets:
        oids = set(oids)
        if len(oids) < 2:
            continue
        wanted |= oids
        types.add(min(oids).split(":", 1)[0] + ".same_record_as")
    joins: dict[tuple[str, str], set[str]] = defaultdict(set)
    if not wanted:
        return joins
    kinds = sorted(types)
    for part in SR.chunks(sorted(wanted)):
        for frm, to, link_type in SR.query(
                conn, "SELECT from_object, to_object, link_type FROM link_rows WHERE "
                      f"link_type IN ({','.join('?' * len(kinds))}) AND tier IN "
                      "('verified', 'reviewed', 'corroborated') AND superseded_at IS NULL "
                      f"AND from_object IN ({','.join('?' * len(part))})", [*kinds, *part]):
            joins[(frm, link_type)].add(to)
            joins[(to, link_type)].add(frm)
    return joins


def one_record_in(joins: Mapping[tuple[str, str], set[str]], oids: Sequence[str]) -> bool:
    """``posting_links.one_record`` over the joins :func:`same_record_joins` read."""
    oids = sorted(set(oids))
    if len(oids) < 2:
        return bool(oids)
    link = oids[0].split(":", 1)[0] + ".same_record_as"
    empty: set[str] = set()
    return all(set(oids[i + 1:]) <= joins.get((a, link), empty)
               for i, a in enumerate(oids))


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
            "recorded_at": lead["recorded_at"], "superseded_at": None,
            **({MO.OCCURRENCE: lead[MO.OCCURRENCE]} if MO.OCCURRENCE in lead.keys() else {})}


def _handles(stored: Any, extra: Sequence[str]) -> Any:
    if not extra:
        return stored
    found = json.loads(stored or "[]")
    return _canon([*found, *(h for h in sorted(set(extra)) if h not in found)])


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
    rejected_of = SR.rejected_pairs(conn, mails)
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
        rejected = rejected_of.get(mail, ())
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
    source = iter(objects if objects is not None else _from_objects(conn))
    while True:
        window = list(islice(source, WINDOW))
        if not window:
            break
        # the leads of the window's objects in one statement, their rejected pairs in a second
        leads_of = SR.rows_by_object(
            conn, window, "SELECT * FROM links WHERE link_id >= ? AND link_id < ? AND tier = "
            "'lead' AND superseded_at IS NULL", [LEAD_PREFIX, _end(LEAD_PREFIX)])
        # every group's candidates judged as one record from one read for the window
        found = [(oid, list(_judgeable(leads_of[oid], owner, counts)))
                 for oid in window if leads_of.get(oid)]
        joins = same_record_joins(conn, ([l["to_object"] for l in own]
                                         for _o, groups in found
                                         for _m, _n, by_type in groups
                                         for _t, own in by_type))
        judged = []
        for oid, groups in found:
            chosen = _decide(groups, lambda oids: one_record_in(joins, oids), counts,
                             pending)
            if chosen:
                judged.append((oid, chosen))
        rejected = SR.rejected_pairs(conn, (oid for oid, _c in judged))
        for oid, chosen in judged:
            for lead in chosen:
                if (lead["to_object"], lead["link_type"]) in rejected.get(oid, ()):
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

    # one firm row per lead: a collapsed lead's occurrences may each give it one
    rows = MO.one_per_lead(firm_links(conn, types, counts, own=own_domains(projector)))
    conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                 (PREFIX, _end(PREFIX)))
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])
    counts["links"] = len(rows)
    return {"counts": dict(counts), "link_tiers": {FIRM: len(rows)} if rows else {}}


__all__ = ["PREFIX", "VETOES", "anchor_types", "firm_links", "judge_groups", "one_record_in",
           "resolved_rows", "same_record_joins", "unique_mentions_step", "vetoed",
           "written_exactly"]
