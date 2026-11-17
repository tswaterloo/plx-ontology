"""``plx.object`` steps of ``OntologyTools``: derived values, twins, links and the
reading beyond an object."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ontology.mentions import corroboration_objects, reader_view
from operating.ontology_tools_identity import _gap, ToolError
from operating.ontology_tools_policy import (_citation, _envelope_bytes, _IDENTITY_TIERS,
                                             _LINK_TYPE, _TIER_RANK, BEYOND_CHILDREN,
                                             collapse_mention_rows, MAX_TWINS, TIER_ORDER)
from operating.ontology_tools_results import _completeness, _edge

if TYPE_CHECKING:
    from operating.ontology_tools_policy import _Scope


class ObjectSteps:
    """``plx.object`` steps of ``OntologyTools``."""

    def _derived(self, scope: _Scope, object_id: str, object_type: str,
                 wanted: set[str] | None
                 ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """(derived, check gap) of a sales order (``operating.order_value``): its value
        derived from what prices it when its header states none, labelled with its basis
        and cited inputs, beside the source copy (``total_amount`` as stored); and a gap
        when its header's total and its lines disagree (both figures, and which one is
        the order's value, if either). Only from what this reader may read."""
        from operating import order_value as ov
        if object_type != ov.TYPE or (wanted is not None and ov.TOTAL not in wanted):
            return [], None
        got = ov.for_reader(self, scope, object_id)
        checked = None
        if got.check is not None and scope.holds(ov.markings(got.check_rows)):
            checked = _gap("unknown", ov.CHECK_REASONS[got.check["status"]],
                           f"{object_id}.{ov.TOTAL}", ov.check_words(got))
        if not got.derived:
            return [], checked
        inputs = [c for c in (_citation(r) for r in got.rows) if c is not None]
        marks = ov.markings(got.rows)
        if len(inputs) != len(got.rows) or not scope.holds(marks):
            return [], checked
        source = [c for c in (_citation(r) for r in got.source_rows) if c is not None]
        out: dict[str, Any] = {
            "property": ov.DERIVED_PROPERTY, "value_type": "money",
            "value": format(got.amount, "f"), "currency": got.currency,
            "derived_from": got.basis, "description": ov.BASIS_WORDS[got.basis],
            "inputs": list({json.dumps(c, sort_keys=True): c for c in inputs}.values())
            [:ov.MAX_INPUTS], "markings": marks}
        if source:
            out["source_total"] = {"value": str(got.source_rows[0]["value"]),
                                   "citation": source[0]}
        return [out], checked

    def _with_leads(self, result: dict[str, Any], leads: list[dict], object_id: str,
                    total: int) -> dict[str, Any]:
        """``result`` plus as many missing-in-record leads as the room its values leave
        (values come first); fewer than found is said in a gap, never silent: each linked
        document's own ``plx.object`` lists its leads. Values that fill the response to the
        byte leave room for that gap: they are cut further (completeness says so)."""
        def trial(shown: int) -> dict[str, Any]:
            out = dict(result, leads=leads[:shown])
            if shown < len(leads):
                out["gaps"] = [_gap(
                    "unknown", "leads_truncated", object_id,
                    f"{len(leads)} missing-in-record leads, the first {shown} shown; "
                    "plx.object on each linked document lists its own"),
                    *result["gaps"]][:50]
            return out

        limit = self.limits["max_result_bytes"]
        low, high = 0, min(50, len(leads))
        while low < high:
            mid = (low + high + 1) // 2
            if _envelope_bytes(trial(mid)) <= limit:
                low = mid
            else:
                high = mid - 1
        out = trial(low)
        while len(out["gaps"]) > 1 and _envelope_bytes(out) > limit:
            out["gaps"] = out["gaps"][:-1]   # the leads gap stays first
        if _envelope_bytes(out) > limit:
            out = self._fit_object(out, total)
        return out

    def _fit_object(self, result: dict[str, Any], total: int) -> dict[str, Any]:
        low, high = 0, min(400, len(result["values"]))
        best = None
        while low <= high:
            mid = (low + high) // 2
            trial = dict(result, values=result["values"][:mid],
                         conflicts=[c for c in result["conflicts"]][:max(0, min(50, mid))],
                         completeness=_completeness(mid, total, True, "result_size", None))
            if _envelope_bytes(trial) <= self.limits["max_result_bytes"]:
                best, low = trial, mid + 1
            else:
                high = mid - 1
        if best is None:
            raise ToolError("result_too_large", "the object does not fit one response",
                            "request fewer properties")
        return best

    def _twins_named(self, scope: _Scope, object_id: str, identity: Sequence[Mapping]
                     ) -> list[dict[str, Any]]:
        """The record's copies in other systems as a name check reads them (11-07 dev
        0047: the billed customer's name lacked the word asked about, its Portal copy's
        held it, over a lead link): each identity edge's other end with its display
        name, link type and tier (a lead stays a lead), strongest tier first. Only the
        edges ``_links`` already kept for this reader (both ends visible, marking held),
        so a twin the reader may not see is never named."""
        others: dict[str, Mapping] = {}
        for edge in identity:   # strongest first (``_links``)
            other = edge.get("to") if edge.get("from") == object_id else edge.get("from")
            if isinstance(other, str) and other != object_id and other not in others:
                others[other] = edge
        if not others:
            return []
        ids = list(others)[:MAX_TWINS]
        visible = self._visible_versions(scope, ids)
        ids = [oid for oid in ids if oid in visible]
        objects = self.store.objects(ids) if ids else {}
        displays = self._displays(scope, ids)
        return [{"object_id": oid, "object_type": objects[oid]["object_type"],
                 "display": displays.get(oid), "link_type": others[oid]["link_type"],
                 "tier": others[oid]["tier"]} for oid in ids if oid in objects]

    def _links(self, scope: _Scope, object_id: str, detailed: bool
               ) -> tuple[list[dict], list[dict], list[tuple[str, Mapping]], list[dict]]:
        rows = [r for r in self.store.links_of(object_id) if self._row_visible(scope, r)]
        others = {r["to_object"] if r["from_object"] == object_id else r["from_object"]
                  for r in rows}
        visible = self._visible_versions(scope, others)
        identity: list[dict] = []
        counts: dict[tuple[str, str], dict[str, int]] = {}
        counted: dict[tuple[str, str], list[tuple[str, Any]]] = {}
        identity_peers: list[str] = []
        gaps: list[dict] = []
        itypes = self.schema.identity_link_types() if self.schema is not None else frozenset()
        handles: dict[str, str | None] = {}
        for row in rows:
            forward = row["from_object"] == object_id
            other = row["to_object"] if forward else row["from_object"]
            if other not in visible:
                continue  # never counted: the reader cannot see the other end
            if row["link_type"] in itypes:
                edge = _edge(row, detailed)
                if edge is not None and row["tier"] == "corroborated":
                    # A corroborated identity (a purchase-order twin) as this reader may
                    # see it (ontology.mentions.reader_view): the lead it rests on when it
                    # may not read the supplier record its corroboration cites.
                    reason = json.loads(row["reason"])
                    cited = corroboration_objects(self.store.conn, reason, handles)
                    shown, kept, why_not = reader_view(
                        edge["tier"], edge["reason"], edge["why_not"],
                        cited is not None and cited <= set(self._visible_versions(scope,
                                                                                  cited)))
                    edge.update(tier=shown, reason=kept, why_not=why_not[:16])
                if edge is not None and detailed:
                    edge = self._trim_citations(scope, edge, {object_id, other})
                if edge is None:
                    gaps.append(_gap("unknown", "malformed_link", row["link_id"][:700]))
                    continue
                identity.append(edge)
                if row["tier"] in _IDENTITY_TIERS:
                    identity_peers.append(other)
                continue
            if not _LINK_TYPE.match(row["link_type"]):
                continue
            tier = row["tier"]
            if tier == "corroborated":   # a mention link or a purchase-order twin
                # The reader's view of the link (ontology.mentions.reader_view): a lead when
                # it may not read what the corroboration cites.
                reason = json.loads(row["reason"])
                cited = corroboration_objects(self.store.conn, reason, handles)
                tier = reader_view(tier, reason, [], cited is not None and cited <= set(
                    self._visible_versions(scope, cited)))[0]
            counted.setdefault((row["link_type"], "forward" if forward else "reverse"),
                               []).append((tier, row))
        # A document's repeated mentions of one record are one link (``mention_pair_key``).
        for (lt, d), items in counted.items():
            tiers = counts.setdefault((lt, d), {})
            for tier, _row in collapse_mention_rows(items):
                tiers[tier] = tiers.get(tier, 0) + 1
        linked_counts = [{"link_type": lt, "direction": d, "count": sum(t.values()),
                          "tiers": dict(sorted(t.items(), key=lambda kv: _TIER_RANK[kv[0]]))}
                         for (lt, d), t in sorted(counts.items())]
        peer_values = [(row["object_id"], row)
                       for row in self.store.readable_values(scope, identity_peers)]
        # Strongest first (link id within a tier), so a cut never drops a person's
        # reviewed identity behind text-matched leads.
        identity.sort(key=lambda edge: _TIER_RANK.get(edge["tier"], len(TIER_ORDER)))
        return identity, linked_counts, peer_values, gaps

    def _beyond(self, scope: _Scope, object_id: str) -> list[dict[str, Any]]:
        """What the records linked to this one (its lines, entries, components: a reverse
        link of at most ``BEYOND_CHILDREN`` records) link to in turn, counted per link
        type, so a figure one link further (an order line's assembly orders, a
        component's purchase lines) is seen before it is walked. Only links records
        hold (never a mention read in text, a lead or a contradicted link), both ends
        readable by this reader as ``plx.object`` counts them, the link back to this
        record left out."""
        firm = lambda row: row["binding_kind"] != "mention" and row["tier"] not in (  # noqa: E731
            "lead", "contradicted")
        children: dict[str, set[str]] = {}
        for row in self.store.links_of(object_id):
            if row["to_object"] != object_id or not firm(row) \
                    or not self._row_visible(scope, row):
                continue
            children.setdefault(row["link_type"], set()).add(row["from_object"])
        out: list[dict[str, Any]] = []
        for via in sorted(children):
            kids = children[via]
            if len(kids) > BEYOND_CHILDREN:
                continue
            kids = set(self._visible_versions(scope, kids))
            counts: dict[tuple[str, str], set[str]] = {}
            linked = self.store.links_of_many(sorted(kids))
            rows = [(kid, row) for kid in sorted(kids) for row in linked[kid]
                    if firm(row) and object_id not in (row["from_object"], row["to_object"])
                    and self._row_visible(scope, row)]
            others = {row["to_object"] if row["from_object"] == kid else row["from_object"]
                      for kid, row in rows}
            readable = self._visible_versions(scope, others)
            for kid, row in rows:
                forward = row["from_object"] == kid
                other = row["to_object"] if forward else row["from_object"]
                if other in readable and _LINK_TYPE.match(row["link_type"]):
                    counts.setdefault((row["link_type"], "forward" if forward
                                       else "reverse"), set()).add(other)
            out += [{"via": via, "link_type": lt, "direction": d, "count": len(reached)}
                    for (lt, d), reached in sorted(counts.items())]
        return out

    def _trim_citations(self, scope: _Scope, edge: dict[str, Any],
                        ends: set[str]) -> dict[str, Any]:
        """Keep a feature citation only when it names a value of one of the two
        (readable) ends that this reader may read at T, or a compared cell no stored
        value holds (a join column no property projects) of the version of one of the
        ends current at T that this reader may read: a link never echoes a handle, path
        or digest of a value or version withheld from the reader."""
        pairs = {(c["l1_handle"], c["field_path"]) for f in edge["reason"] + edge["why_not"]
                 for c in f.get("citations", ())}
        rows = self.store.values_by_citation(sorted(pairs))
        stored = {(row["l1_handle"], row["field_path"]) for row in rows}
        readable = {(row["l1_handle"], row["field_path"]) for row in rows
                    if row["object_id"] in ends and self._row_visible(scope, row)}
        versions = ({row["l1_handle"] for row in self._visible_versions(scope, ends).values()}
                    if pairs - stored else set())
        for feature in edge["reason"] + edge["why_not"]:
            if "citations" in feature:
                kept = [c for c in feature["citations"]
                        if (c["l1_handle"], c["field_path"]) in readable
                        or ((c["l1_handle"], c["field_path"]) not in stored
                            and c["l1_handle"] in versions)]
                if kept:
                    feature["citations"] = kept
                else:
                    del feature["citations"]
        return edge
