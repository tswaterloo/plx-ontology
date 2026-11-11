"""What the app reads for the agent before its session starts, as the reader.

Two things, both from the generic tools as the signed-in reader (nothing is written):

* **Which record an identifier names** (``resolve``). ``find_roots`` leaves an identifier
  that matches several records unresolved. Here the candidates of the identifier kind's own
  record type are kept (a sync mapping that only carries the number is not the record), and
  those an established identity link joins (``why_trail.same_records``: verified or
  reviewed, conservative identity) are one record held in several systems. One such group:
  the identifier names that record. Several: they are different records that share the
  number (a Portal number reused by Business Central, say), and the person is asked which
  one (``choices``), never a guess. A record the request is already about (a pick, a page)
  settles it.
* **The evidence the answer's "Evidence" list shows** (``evidence``): a purchase order's
  lateness trail (its dated steps, the vendor's mail naming it or a twin, receipts) or an
  order's materials walk, built once by ``operator_api.trail`` and handed to the agent as a
  tool output it reads and cites from (``brief``), so its reply and the trail under it
  agree. The same trail is the answer's.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence

from . import models as m

MAX_GROUPS = 6
MAX_BRIEF_STEPS = 12
# What a choice shows of each record, when it has it (any record type).
CHOICE_PROPERTIES = ("vendor_name", "customer_name", "order_date", "document_date",
                     "fm_po_number", "po_number", "bc_status", "status")
SYSTEM_WORDS = (("bc.", "Business Central"), ("portal.fm_", "FileMaker"),
                ("portal.sage_", "Sage"), ("portal.", "the Portal"))
# How a question names one of those systems (``SYSTEM_WORDS``' words), folded.
SYSTEM_NAMES = ((r"business\s*central|bc", "Business Central"),
                (r"file\s*maker|fm", "FileMaker"), (r"sage", "Sage"),
                (r"(?:the\s+)?portal", "the Portal"))
# The words between a system's name and the number it qualifies ("Business Central PO
# PO100064", "the Portal purchase order 9"), at most.
SYSTEM_GAP_WORDS = 3


def system_words(object_id: str) -> str:
    binding = str(object_id).split(":", 2)[1] if str(object_id).count(":") >= 2 else ""
    return next((words for prefix, words in SYSTEM_WORDS if binding.startswith(prefix)),
                "another system")


def named_system(question: str, mention: Any) -> Optional[str]:
    """The system the question names for ``mention`` (``SYSTEM_WORDS``' words): its name
    just before the number ("Business Central PO PO100064", at most ``SYSTEM_GAP_WORDS``
    words between) or right after it ("PO100064 in BC"); None when it names none there,
    or the number is not in the question as written. A system named elsewhere in the
    question (a hop to another system's records) never qualifies the number."""
    import re
    text = " ".join(str(question or "").casefold().split())
    said = " ".join(str(mention or "").casefold().split())
    at = text.find(said) if said else -1
    if at < 0:
        return None
    before = text[:at].split()[-(SYSTEM_GAP_WORDS + 2):]
    after = text[at + len(said):]
    for pattern, words in SYSTEM_NAMES:
        name = rf"(?<![\w])(?:{pattern})(?![\w])"
        gap = rf"{name}(?:\s+[^\s]+){{0,{SYSTEM_GAP_WORDS}}}\s*$"
        if re.search(gap, " ".join(before) + " ") or re.match(
                rf"\s*(?:in|from|on)\s+{name}", after):
            return words
    return None


def _kind_type(kind: Any) -> Optional[str]:
    from ontology import identifiers
    try:
        return identifiers.kind_spec(str(kind)).get("object_type")
    except Exception:  # noqa: BLE001 - an unknown kind keeps every candidate
        return None


def resolve(call: Callable[[str, dict], Any], found: Mapping[str, Any],
            object_id: Optional[str] = None,
            same: Optional[Callable[[Sequence[str]], bool]] = None, question: str = ""
            ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(``found`` with each identifier that names one record moved to its roots, the
    identifiers that name several different records: {mention, groups: [[object id ...]],
    and ``party`` when the question names a supplier or customer none of them has}).
    ``same(ids)``: whether the records are one (default ``why_trail.same_records``).
    Different records sharing the number are told apart by the supplier or customer the
    question names (``why_trail.by_named_party``): the one record of that party is the
    root (``named_party``), never a guess between several."""
    from operating.why_trail import by_named_party, record_party
    if same is None:
        from operating.why_trail import same_records
        same = lambda ids: same_records(call, ids)   # noqa: E731
    roots = [dict(r) for r in found.get("roots") or () if isinstance(r, Mapping)]
    left: list[dict[str, Any]] = []
    several: list[dict[str, Any]] = []
    for item in found.get("ambiguous") or ():
        if not isinstance(item, Mapping):
            continue
        candidates = [c for c in item.get("candidates") or () if isinstance(c, Mapping)
                      and isinstance(c.get("object_id"), str)]
        wanted = _kind_type(item.get("kind"))
        own = [c for c in candidates if c.get("object_type") == wanted]
        candidates = own or candidates
        groups: list[list[Mapping[str, Any]]] = []
        for candidate in candidates:
            for group in groups:
                if same([group[0]["object_id"], candidate["object_id"]]) or \
                        same([candidate["object_id"], group[0]["object_id"]]):
                    group.append(candidate)
                    break
            else:
                groups.append([candidate])
        picked = next((g for g in groups if object_id in {c["object_id"] for c in g}), None)
        party = None
        system = named_system(question, item.get("mention")) \
            if picked is None and len(groups) > 1 else None
        if system is not None:
            # the system the question names for the number: its one record there is it
            held = [g for g in groups
                    if any(system_words(c["object_id"]) == system for c in g)]
            if len(held) == 1:
                picked = held[0]
                item = {**item, "named_system": system,
                        "others": [g[0]["object_id"] for g in groups if g is not picked]}
        if picked is None and len(groups) > 1 and question:
            try:
                hits, party = by_named_party(call, question,
                                             [[c["object_id"] for c in g] for g in groups])
            except Exception:  # noqa: BLE001 - unread: every record stays a choice
                hits, party = list(range(len(groups))), None
            if len(hits) == 1:
                picked = groups[hits[0]]
                party = record_party(call, picked[0]["object_id"])
                item = {**item, "named_party": party,
                        "others": [g[0]["object_id"] for g in groups if g is not picked]}
                party = None
        if picked is not None or len(groups) == 1:
            group = picked or groups[0]
            first = next((c for c in group if c["object_id"] == object_id), group[0])
            roots.append({"mention": item.get("mention"), "kind": item.get("kind"),
                          "object_id": first["object_id"],
                          "object_type": first.get("object_type"),
                          "found_by": "identifier",
                          "same_as": [c["object_id"] for c in group if c is not first],
                          **({key: item[key] for key in ("named_party", "named_system",
                                                         "others") if key in item}
                             if item.get("others") else {})})
        elif len(groups) > 1:
            several.append({"mention": item.get("mention"),
                            "groups": [[c["object_id"] for c in g] for g in groups[:MAX_GROUPS]],
                            **({"party": party} if party else {})})
        else:
            left.append(dict(item))
    return {**dict(found), "roots": roots, "ambiguous": left}, several


def choice_label(call: Callable[[str, dict], Any], mention: str, object_id: str) -> str:
    """``PO100109 in Business Central (Croda, order date 2026-07-14)``: the
    record's own values as this reader reads them, its system in words."""
    from .presentation import plain_words
    card = call("plx.object", {"object_id": object_id,
                               "properties": list(CHOICE_PROPERTIES)}) or {}
    values = {}
    for value in card.get("values") or ():
        raw = value.get("value")
        raw = raw.get("untrusted_text") if isinstance(raw, Mapping) else raw
        if isinstance(raw, (str, int, float)) and str(raw).strip() and \
                value.get("property") not in values:
            values[value.get("property")] = str(raw).strip()[:60]
    if "vendor_name" not in values and "customer_name" not in values:
        # the party the record links to (its own system's supplier or customer record)
        kind = object_id.split(":", 1)[0]
        for link, prop in ((f"{kind}.ordered_from", "vendor_name"),
                           (f"{kind}.ordered_by", "customer_name")):
            got = call("plx.around", {"from": object_id, "limit": 1,
                                      "path": [{"link_type": link, "direction": "forward"}]})
            party = next((o.get("display") for o in (got or {}).get("objects") or ()
                          if isinstance(o, Mapping)), None)
            party = party.get("untrusted_text") if isinstance(party, Mapping) else party
            if isinstance(party, str) and party.strip():
                values[prop] = party.strip()[:60]
                break
    shown = [values[p] if p in ("vendor_name", "customer_name") else
             f"{plain_words(p)} {values[p]}" for p in CHOICE_PROPERTIES
             if p in values and values[p] != mention][:3]
    return (f"{mention} in {system_words(object_id)}"
            + (f" ({', '.join(shown)})" if shown else ""))[:200]


def choices_answer(call: Callable[[str, dict], Any], request: m.AskRequest,
                   several: Sequence[Mapping[str, Any]], *, conversation: str,
                   freshness: m.Freshness, object_url: Callable[[str, str], str],
                   latency_ms: int, answer_id: str) -> m.AnswerPayload:
    """A did-you-mean for identifiers naming different records: each record a choice (the
    first of each group of one record's copies), nothing picked or merged."""
    item = several[0]
    refs = []
    for group in item["groups"]:
        oid = group[0]
        kind = oid.split(":", 1)[0]
        refs.append(m.ObjectRef(object_id=oid, type=kind,
                                label=choice_label(call, str(item["mention"]), oid),
                                open_url=object_url(kind, oid)))
    direct = (f"{item['mention']} names {len(refs)} different records that share the "
              "number" + (f", and none of them is from {item['party']}" if item.get("party")
                          else "") + ". Which did you mean?")
    return m.AnswerPayload(
        answer_id=answer_id, question=request.question, outcome="partial",
        direct_answer=direct, hidden=m.Hidden(count=0, by_owner=[], message=None),
        freshness=freshness, did_you_mean=refs, conversation_id=conversation,
        latency_ms=latency_ms)


def copies(call: Callable[[str, dict], Any], object_id: str) -> list[str]:
    """The record's copies in other systems (its firm identity links, as the trail reads
    them), each as "<number> in <system> (object id ...)": a message naming a copy's number
    is about this record."""
    from operating.evidence_trail import FIRM, text_of
    card = call("plx.object", {"object_id": object_id}) or {}
    out = []
    for edge in card.get("identity") or ():
        if not isinstance(edge, Mapping) or edge.get("tier") not in FIRM:
            continue
        other = edge.get("to") if edge.get("from") == object_id else edge.get("from")
        if not isinstance(other, str) or other == object_id:
            continue
        shown = text_of(((call("plx.object", {"object_id": other}) or {}).get("object")
                         or {}).get("display"))
        out.append(f"{shown or 'a record'} in {system_words(other)} (object id {other})")
    return out[:6]


MAX_MAPPED = 6


def link_map(call: Callable[[str, dict], Any], object_ids: Sequence[str]) -> list[str]:
    """Each record's links counted per link type (``plx.object`` ``linked_counts``, as
    this reader reads it), one line per record: where its quantities, documents and
    parties are, for the agent to walk (``plx.around``) instead of
    searching text."""
    out = []
    for oid in list(dict.fromkeys(object_ids))[:MAX_MAPPED]:
        try:
            card = call("plx.object", {"object_id": oid, "response_format": "concise"}) or {}
        except Exception:  # noqa: BLE001 - a record not read is not mapped
            continue
        counts = [f"{c.get('link_type')} {c.get('direction')} {c.get('count')}"
                  for c in card.get("linked_counts") or () if isinstance(c, Mapping)
                  and c.get("count")]
        if counts:
            out.append(f"- {oid} ({system_words(oid)}) links: " + "; ".join(counts[:14]) + ".")
    return out


def brief(trail: Optional[m.EvidenceTrail], roots: Sequence[Mapping[str, Any]],
          call: Optional[Callable[[str, dict], Any]] = None,
          entities: Sequence[Mapping[str, Any]] = (),
          unfound: Sequence[Mapping[str, Any]] = ()) -> str:
    """What the agent is told before it starts: the record each identifier names (and its
    copies in other systems), and the trail's own content (``trail.materials_brief`` for a
    materials walk; for a purchase order its dated steps, each with the source to read and
    cite). ``unfound``: the identifiers the Ask looked up as this reader that named no
    one record (``objective_runner.find_roots``' ``unfound``; only those whose lookup
    completed, reason ``not_found``, never a failed or timed-out one), said once so the session
    does not repeat the same lookup."""
    from .trail import materials_brief
    lines: list[str] = []
    missed = sorted({str(u["mention"])[:80] for u in unfound or ()
                     if isinstance(u, Mapping) and u.get("mention")
                     # a failed, timed-out or partial lookup says nothing
                     and u.get("reason") == "not_found" and not u.get("gaps")})
    if missed:
        lines.append("Already looked up for you, as this reader, by identifier with no "
                     "record found: " + ", ".join(f'"{m}"' for m in missed[:6]) + " (a "
                     "different spelling, a related record or a text search may still "
                     "find it; never say it does not exist from this alone).")
    for root in roots:
        if not root.get("mention") or not root.get("object_id"):
            continue
        same = root.get("same_as") or []
        if root.get("found_by") == "resolver":
            # a name or code the entity resolver matched: the same thing in every system
            lines.append(f"\"{root['mention']}\" names one {root.get('object_type') or 'record'}"
                         f" held as object ids {', '.join([root['object_id'], *same])} (one "
                         "thing in several systems, matched by its exact code or name; "
                         "start from these records and what links to them).")
            if call is not None:
                lines += link_map(call, [root["object_id"], *same])
            continue
        lines.append(f"\"{root['mention']}\" names the record with object id "
                     f"{root['object_id']}" + (" (the same record as " + ", ".join(same)
                                                + ")" if same else "") + ".")
        if root.get("others") and root.get("named_system"):
            # picked by the system the question names for the number
            lines.append(f"It is the one in {root['named_system']}, the system the question "
                         "names for it; " + ", ".join(root["others"]) + " share the number "
                         "but are different records (never read them as this one).")
        elif root.get("others"):
            # picked by the party the question names: the others are different orders
            lines.append(f"It is the one of {root.get('named_party') or 'the party named'}, "
                         "the supplier or customer the question names; "
                         + ", ".join(root["others"]) + " share the number but are different "
                         "records (say so in one line, never read them as this one).")
    for entity in entities or ():
        # a lead or derived entity, and such records of a matched one, are offered only
        records = [r for r in entity.get("records") or ()
                   if entity.get("status") != "match" or r.get("status") != "match"]
        # each with what it was read out of (its ``why``, the Workbench's own words)
        derived = [r["object_id"] + "".join(f" ({w})" for w in (r.get("why") or ())[:1])
                   for r in records if r.get("status") == "derived"][:4]
        leads = [r["object_id"] for r in records if r.get("status") != "derived"][:4]
        if derived:
            lines.append(f"Possible only (\"{entity.get('mention')}\" read out of a longer "
                         "reference, or as its number without its letters; check each "
                         f"record's type and context before you rely on it): "
                         f"{', '.join(derived)}.")
        if leads:
            lines.append(f"Leads only (related to \"{entity.get('mention')}\", not the same "
                         f"thing; read before you rely on them): {', '.join(leads)}.")
    if trail is not None and trail.kind == "materials":
        lines.append(materials_brief(trail))
    elif trail is not None and trail.kind == "purchase_order":
        lines.append("Already read for you, as this reader, by the Workbench's purchase "
                     "order trail (the PO, its copies in other systems, receipts, vendor "
                     "confirmations, and the mail and Teams messages that name it):")
        for po in trail.trails:
            lines.append(f"PO {po.subject.label} (object id {po.subject.object_id}):"
                         + (f" {po.eta.text}" if po.eta and po.eta.text else ""))
            same = copies(call, po.subject.object_id) if call is not None else []
            if same:
                lines.append("The same purchase order is also recorded as "
                             + "; ".join(same) + ": a message naming any of these numbers "
                             "is about it.")
            for step in po.steps[-MAX_BRIEF_STEPS:]:
                cite = f" [source {step.source.handle}]" if step.source else ""
                who = f" from {step.sender}" if step.sender else ""
                lines.append(f"- {step.at or 'undated'}: {step.title}{who}{cite}")
        if trail.hidden_steps:
            lines.append(f"{trail.hidden_steps} more steps are in places this reader "
                         "can't open.")
        lines.append("Read these sources (plx.read on a source, plx.object on a record) "
                     "before you say a message, receipt or confirmation does not exist.")
    return ("\n\n" + "\n".join(lines)) if lines else ""
