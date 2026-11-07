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
                 counts: Counter) -> list[Any]:
    """The leads of ``leads`` (one object's) that get a firm link beside them."""
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
            counts["several_records"] += 1
            continue
        counts["firm"] += 1
        out += own
    return out


def firm_row(lead: Any) -> dict[str, Any]:
    reason = [*json.loads(lead["reason"] or "[]"),
              {"feature": "exact_identifier", "agrees": True},
              {"feature": "unique_record", "agrees": True},
              {"feature": "corroborates", "agrees": True, "binding_kind": "mention",
               "link_id": lead["link_id"]}]
    return {"link_id": PREFIX + _digest(lead["link_id"]),
            "link_type": lead["link_type"], "from_object": lead["from_object"],
            "to_object": lead["to_object"], "tier": FIRM, "binding_kind": mentions.BINDING_KIND,
            "link_contract": None, "reason": _canon(reason), "why_not": "[]",
            "evidence_handles": lead["evidence_handles"], "reviewed_by": None,
            "reviewed_at": None, "review_action_id": None, "markings": lead["markings"],
            "recorded_at": lead["recorded_at"], "superseded_at": None}


def firm_links(conn: Any, object_types: Iterable[str], counts: Counter,
               objects: Iterable[str] | None = None) -> Iterator[dict[str, Any]]:
    """The firm rows (module docstring), outcomes counted in ``counts``; ``objects``
    limits the from ends."""
    owner = anchor_types(object_types)
    for oid in (objects if objects is not None else _from_objects(conn)):
        leads = _q(conn, "SELECT * FROM links WHERE from_object = ? AND link_id >= ? AND "
                         "link_id < ? AND tier = 'lead' AND superseded_at IS NULL",
                   [oid, LEAD_PREFIX, _end(LEAD_PREFIX)])
        if not leads:
            continue
        chosen = judge_groups(conn, leads, owner, counts)
        if not chosen:
            continue
        rejected = {(r[0], r[1]) for r in _q(
            conn, "SELECT to_object, link_type FROM link_rows WHERE from_object = ? AND "
                  "tier = 'contradicted'", [oid])}
        for lead in chosen:
            if (lead["to_object"], lead["link_type"]) in rejected:
                counts["rejected"] += 1
                continue
            yield firm_row(lead)


def unique_mentions_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``unique_mentions`` step (module docstring)."""
    conn = builder.connection
    types = list((getattr(projector, "types", None) or {}).keys())
    counts: Counter[str] = Counter()
    rows = list(firm_links(conn, types, counts))
    conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                 (PREFIX, _end(PREFIX)))
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])
    counts["links"] = len(rows)
    return {"counts": dict(counts), "link_tiers": {FIRM: len(rows)} if rows else {}}


__all__ = ["PREFIX", "anchor_types", "firm_links", "judge_groups", "unique_mentions_step",
           "written_exactly"]
