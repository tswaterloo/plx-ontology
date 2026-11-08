"""The link explorer's read: a record's neighbours one hop out, grouped by kind of link.

Read as the reader (``plx.object`` for the links its card lists, then one capped
``plx.around`` per link kind), so a neighbour, an edge or a whole kind the reader may not
read is simply not there. What was trimmed is said without a number: ``partial`` when the
walk lost records to the reader's grants, ``capped`` when this read kept itself small.
Walking on is the same read from a neighbour (two hops are two reads, lazily); a neighbour
the reader cannot open reads as not found, as any record they cannot see.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from . import models as m
from . import presentation

MAX_KINDS = 12        # kinds of link read per record
NODES_PER_KIND = 8    # neighbours listed per kind
MAX_NODES = 60        # neighbours listed in all

FIRM = frozenset({"verified", "reviewed", "corroborated"})
_RANK = {"verified": 0, "reviewed": 1, "corroborated": 2, "stored_by_text_join": 3,
         "lead": 4, "contradicted": 5}
SAME_RECORD = "Same record in another system"

PARTIAL_MESSAGE = "Some linked records are not visible to you."
CAPPED_MESSAGE = ("Only the first few links of each kind are shown. Open a record to see "
                  "what it links to.")
EMPTY_MESSAGE = "No linked records are visible to you for this one."


def _plain(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text", value.get("value"))
    return str(value)[:200] if isinstance(value, (str, int, float)) else None


def kind_label(link_type: str, direction: str, target: str) -> str:
    """A link kind in words: 'Sales orders: ordered by' (the records reached, then the
    link's own name; the same-record link is said as what it means)."""
    name = link_type.split(".", 1)[-1]
    if name == "same_record_as":
        return SAME_RECORD
    kind = presentation.words(re.sub(r"(?<=[a-z])(?=[A-Z])", "_", target or "record"))
    kind = (kind[:1].upper() + kind[1:]) or "Records"
    return f"{kind}: {presentation.words(name)}"


def _tiers(edges: Any, other_ids: set[str], root: str) -> dict[str, str]:
    """The strongest edge tier to each neighbour (an unknown tier counts as suggested)."""
    best: dict[str, str] = {}
    for edge in edges or ():
        if not isinstance(edge, Mapping):
            continue
        ends = {edge.get("from"), edge.get("to")}
        tier = edge.get("tier") if edge.get("tier") in _RANK else "lead"
        for oid in ends & other_ids:
            if oid != root and (oid not in best or _RANK[tier] < _RANK[best[oid]]):
                best[oid] = tier
    return best


def _lost(result: Mapping[str, Any]) -> bool:
    """Whether the reader's grants may have removed records from this walk (what
    ``plx.around`` marks without ever counting them)."""
    from operating.ontology_tools_traverse import PERMISSION_LOSSES
    if any((g or {}).get("reason") in PERMISSION_LOSSES for g in result.get("gaps") or ()):
        return True
    return (result.get("completeness") or {}).get("reason") in PERMISSION_LOSSES


def explore(view: Any, oid: str, *, open_url) -> m.Explore:
    """``oid``'s neighbours as ``view``'s reader reads them. KeyError when the record is
    not one they may read (the same as not found)."""
    card = view._call("plx.object", {"object_id": oid, "response_format": "detailed"})
    if (card.get("error") or {}).get("code") in ("object_not_found", "not_found"):
        raise KeyError("object_not_found")
    if card.get("error"):
        raise KeyError("object_not_found")   # a failed read says nothing about the record
    obj = card.get("object") or {}
    kind = str(obj.get("object_type") or oid.split(":", 1)[0])
    label = presentation.record_label(_plain(obj.get("display")), kind, oid)
    listed = [i for i in card.get("linked_counts") or ()
              if isinstance(i, Mapping) and (i.get("count") or i.get("tiers"))
              and isinstance(i.get("link_type"), str)
              and i.get("direction") in ("forward", "reverse")]
    capped = len(listed) > MAX_KINDS
    partial = False
    groups: list[m.ExploreGroup] = []
    budget = MAX_NODES
    for item in listed[:MAX_KINDS]:
        got = view._call("plx.around", {
            "from": oid, "tier_floor": "lead", "limit": NODES_PER_KIND,
            "response_format": "detailed",
            "path": [{"link_type": item["link_type"], "direction": item["direction"]}]})
        if got.get("error"):
            partial = True   # a kind that could not be read for this reader is not shown
            continue
        if _lost(got):
            partial = True
        objects = [o for o in got.get("objects") or () if isinstance(o, Mapping)]
        leads = [o for o in (got.get("possibly_related") or {}).get("objects") or ()
                 if isinstance(o, Mapping)]
        ids = {o["object_id"] for o in objects + leads if isinstance(o.get("object_id"), str)}
        tiers = _tiers(got.get("edges"), ids, oid)
        lead_ids = {o.get("object_id") for o in leads}
        nodes: list[m.ExploreNode] = []
        for o in objects + leads:
            nid = o.get("object_id")
            if not isinstance(nid, str) or nid == oid or nid in {n.object_id for n in nodes}:
                continue
            if len(nodes) >= NODES_PER_KIND or budget <= 0:
                capped = True
                break
            tier = tiers.get(nid, "lead" if nid in lead_ids else "stored_by_text_join")
            ntype = str(o.get("object_type") or nid.split(":", 1)[0])
            nodes.append(m.ExploreNode(
                object_id=nid, type=ntype,
                label=presentation.record_label(_plain(o.get("display")), ntype, nid)[:200],
                open_url=open_url(ntype, nid), tier="firm" if tier in FIRM else "suggested"))
            budget -= 1
        more = bool((got.get("completeness") or {}).get("next_cursor")) or \
            len(objects) + len(leads) > len(nodes)
        if more:
            capped = True
        if nodes:
            target = nodes[0].type
            groups.append(m.ExploreGroup(
                key=f"{item['link_type']}|{item['direction']}",
                label=kind_label(item["link_type"], item["direction"], target),
                nodes=nodes, more=more))
        elif _lost(got):
            partial = True
    # firm kinds first, then by label: a stable order
    groups.sort(key=lambda g: (all(n.tier != "firm" for n in g.nodes), g.label, g.key))
    message = (PARTIAL_MESSAGE if partial else CAPPED_MESSAGE if capped
               else EMPTY_MESSAGE if not groups else None)
    if partial and capped:
        message = f"{PARTIAL_MESSAGE} {CAPPED_MESSAGE}"
    return m.Explore(object_id=oid, type=kind, label=label[:200], groups=groups,
                     partial=partial, capped=capped, message=message)
