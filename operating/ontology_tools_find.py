"""``plx.schema`` and ``plx.find`` steps of ``OntologyTools``: matching, leads, filters,
corroboration, candidates and result sets."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from ontology.types import Actor
from operating.absence_claims import traversal_problem
from operating.ontology_tools_identity import (_declared_values, _gap, _literal_forms, _Match,
                                               _schema_memo, _text_tokens, _word_stem,
                                               exact_match, identity_scopes, origin_rank,
                                               RECORD_SOURCES, schema_offers_type, scoped_kind,
                                               STRUCTURED_LEADS, STRUCTURED_WORD_MIN, ToolError)
from operating.ontology_tools_policy import (_DATE, _envelope_bytes, _find_text_seconds,
                                             _format_time, _LABEL_KIND_CONFLICT,
                                             _SCHEMA_PAGE_CAPS, _SCHEME_SOURCE, _TIER_RANK,
                                             CARRIER_REASONS, FRESHNESS_REASONS, LINKED_REASON,
                                             NAMED_READ_NOTE, NEVER_AUTHORITY, REFERENCE_REASON,
                                             ROOT_REASONS, run_memo_for, SCAN_MARGIN_SECONDS,
                                             SCOPE_FEATURE, SCOPE_FEATURES, SCOPE_LEAD_REASON)
from operating.ontology_tools_results import (_business_day, _ceiling, _cited, _cited_values,
                                              _column_path, _completeness,
                                              _corroboration_features, _edge, _filters_verdict,
                                              _fingerprint, _freshness_first, _index_row_ok,
                                              _parse_day, _pinned, _read_cursor,
                                              _resolution_gaps, _scope_freshness_entry,
                                              _write_cursor, undated_marker)

if TYPE_CHECKING:
    from operating.ontology_tools_identity import _Key
    from operating.ontology_tools_policy import _Scope


class FindSteps:
    """``plx.schema`` and ``plx.find`` steps of ``OntologyTools``."""

    # ------------------------------------------------------------------ plx.schema

    def _schema_tool(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        if self.schema is None or self.schema.digest_hex is None:
            raise ToolError("schema_unavailable", "the compiled ontology schema is not loaded",
                            "install the schema loader (lane L4) and domain files")
        at = self._as_known_at({})
        store_meta = self._store_meta()
        object_type = arguments.get("object_type")
        section = arguments.get("section")
        types = self.schema.object_types
        if object_type is not None and object_type not in types:
            raise ToolError("unknown_object_type", f"no object type {object_type} in the schema",
                            "call plx.schema without object_type for the list")
        detailed = object_type is not None

        def offered(tid: Any) -> bool:
            return detailed or schema_offers_type(types, tid)
        chosen = [object_type] if object_type else [t for t in sorted(types) if offered(t)]

        def type_entry(tid: str) -> dict[str, Any]:
            spec = types[tid]
            entry: dict[str, Any] = {"id": tid,
                                     "availability": spec.get("availability", {}).get("state",
                                                                                     "served")}
            description = spec.get("description")
            if description:
                entry["description"] = description[:2000 if detailed else 200]
            if detailed:
                props = []
                for prop in spec.get("properties", ())[:200]:
                    item: dict[str, Any] = {"name": prop["name"], "value_type": prop["value_type"]}
                    for key in ("unit", "quantity_role", "availability"):
                        if key in prop:
                            item[key] = prop[key]
                    markings = sorted(set(prop.get("markings", ())) | set(spec.get("markings", ())))
                    if markings:
                        item["markings"] = markings
                    props.append(item)
                entry["properties"] = props
            return entry

        def related(item: Mapping[str, Any], *fields: str) -> bool:
            if object_type is None:
                return all(offered(item.get(f)) for f in fields)
            return any(item.get(f) == object_type for f in fields)

        sections = {"objects": ("object_types", [type_entry(t) for t in chosen]),
                    "links": ("link_types", [
                        {k: v for k, v in (("id", l["id"]), ("from", l["from"]), ("to", l["to"]),
                                           ("cardinality", l["cardinality"]),
                                           ("tier_ceiling", _ceiling(l))) if v is not None}
                        for _, l in sorted(self.schema.link_types.items())
                        if related(l, "from", "to")]),
                    "events": ("event_types", [
                        {"id": e["id"], "object_type": e["object_type"],
                         "time_basis": e["time_basis"]}
                        for _, e in sorted(self.schema.event_types.items())
                        if related(e, "object_type")]),
                    "actions": ("action_types", [
                        {"id": a["id"], "target": a["target"], "executor": a["executor"]}
                        for _, a in sorted(self.schema.action_types.items())
                        if related(a, "target")]),
                    # the process graphs plx.process serves on this host (none when it
                    # serves no plx.process): what an unnamed call picks, and when
                    "processes": ("processes", [
                        {"id": g["id"], "root_type": g["root_type"],
                         "implicit": bool(g.get("implicit", True)),
                         "chosen_when_open": bool(g.get("open_when")),
                         "description": (g.get("description")
                                         or g["id"])[:2000 if detailed else 200],
                         "stages": [st["id"] for st in g["stages"]][:40]}
                        for _, g in sorted((getattr(self, "process_graphs", None)
                                            or {}).items())
                        if related(g, "root_type")])}
        # One sequence over the chosen sections (objects first), paged by offset: a
        # page takes at most each list's contract cap and what fits one response.
        items = [(key, item) for name, (key, listed) in sections.items()
                 if section in (None, name) for item in listed]
        total = len(items)
        offset = 0
        fingerprint = _fingerprint(dict(arguments, schema_digest=self.schema.digest_hex), actor)
        if arguments.get("cursor") is not None:
            offset, _ = _read_cursor(arguments["cursor"], fingerprint)
            if offset >= total:
                raise ToolError("invalid_cursor", "the cursor is past the end of this result",
                                "repeat the request without cursor")
        meta = {"schema_digest": self.schema.digest_hex, "as_known_at": _format_time(at),
                "freshness": self._freshness(store_meta, ())}

        def page(count: int) -> dict[str, Any]:
            out: dict[str, Any] = {"schema_digest": self.schema.digest_hex}
            for name, (key, _) in sections.items():
                if section in (None, name):
                    out[key] = []
            for key, item in items[offset:offset + count]:
                out[key].append(item)
            done = offset + count
            next_cursor = _write_cursor(done, at, fingerprint) if done < total else None
            out["meta"] = meta
            out["completeness"] = _completeness(count, total, done < total, "paged",
                                                next_cursor)
            return out

        count, taken = 0, dict.fromkeys(_SCHEMA_PAGE_CAPS, 0)
        for key, _ in items[offset:]:
            if taken[key] == _SCHEMA_PAGE_CAPS[key]:
                break
            taken[key] += 1
            count += 1
        out = page(count)
        # Fit the response: shorten the page until it fits; the cursor resumes after it.
        while count > 1 and _envelope_bytes(out) > self.limits["max_result_bytes"]:
            count = max(1, count * 3 // 4)
            out = page(count)
        return out

    def _structured_leads(self, scope: Any, query: str) -> list[dict[str, Any]]:
        """The record properties of a system of record (``RECORD_SOURCES``) whose declared
        values hold a word of ``query`` (``_declared_values``; words compared by
        ``_word_stem``), each with that value and the ``plx.find`` call listing those
        records, at most ``STRUCTURED_LEADS``: a question about a status, kind or stage
        is answered from the records holding it before the documents that mention it.
        Only types and properties this reader holds the markings of and may list a
        record of (one ``LIMIT 1`` read per type)."""
        if self.schema is None:
            return []
        words = {_word_stem(word) for word in _text_tokens(query, [], limit=None)
                 if len(word) >= STRUCTURED_WORD_MIN}
        if not words:
            return []
        out: list[dict[str, Any]] = []
        for tid, spec in sorted(self.schema.object_types.items()):
            if not isinstance(spec, Mapping):
                continue
            sources = {b.get("source") for b in spec.get("bindings") or ()
                       if isinstance(b, Mapping)}
            if not sources or not sources <= RECORD_SOURCES:
                continue
            found = []
            for prop in spec.get("properties") or ():
                if not isinstance(prop, Mapping) or prop.get("identifier_kind") \
                        or not scope.holds(set(spec.get("markings") or ())
                                           | set(prop.get("markings") or ())):
                    continue
                for value in _declared_values(prop):
                    if words & {_word_stem(w) for w in re.findall(r"[^\W_]+", value)}:
                        found.append((str(prop["name"]), value))
                        break
            if not found or self.store.conn.execute(
                    "SELECT 1 FROM objects WHERE object_type = :tid AND audience_key IN "
                    "(SELECT value FROM json_each(:aud)) LIMIT 1",
                    scope.params(tid=tid)).fetchone() is None:
                continue
            for name, value in found:
                out.append({"object_type": tid, "property": name, "value": value,
                            "find": {"object_type": tid, "filters": [
                                {"property": name, "op": "eq", "value": value}]}})
                if len(out) >= STRUCTURED_LEADS:
                    return out
        return out

    def _entities(self, actor: Actor, text: str, object_type: str | None = None
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Every record of the things ``text`` names, across systems, with confidence
        and why (``operating.entity_resolution``): the entity index is read without the
        store lock, each store read under it."""
        from operating.entity_resolution import Resolver
        if self.resolver is None:
            self.resolver = Resolver(self)
        try:
            with self.store.lock:
                scope = self._scope(actor, self.clock())
            return self.resolver.resolve(scope, actor, text, object_type,
                                         lock=self.store.lock)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - resolution is added value, never the find
            if isinstance(exc, sqlite3.Error):
                raise
            return [], [_gap("unknown", "entity_index_unavailable", "entity index",
                             f"entity resolution could not run ({type(exc).__name__})")]

    def _meta_locked(self) -> dict[str, Any]:
        with self.store.lock:
            return self._meta(self._store_meta(), self.clock(), ())

    def _find_after(self, arguments: Mapping[str, Any], actor: Actor,
                    result: dict[str, Any]) -> dict[str, Any]:
        if (result["status"] != "not_found" or self.sources is None
                or arguments.get("query") is None):
            return result
        return self._find_sources(arguments, actor, result)

    def _named_not_found(self, query: Any, candidates: Sequence[Mapping[str, Any]]
                         ) -> list[dict[str, Any]]:
        """A query naming an identifier of one kind (``PO#10855``: a purchase order) whose
        every candidate is a lead of another kind or form (a customer PO with the same
        digits, a same-number twin): no record of the kind named, that this reader may
        read, holds it. Said first (``named_record_not_found``), so the lead is never read
        as the record asked about; the leads stay listed as what they are."""
        if (not isinstance(query, str) or not candidates or self.grammar is None
                or any(c.get("reason") in ROOT_REASONS for c in candidates)):
            return []
        try:
            named = {(m.kind, m.normalized) for m in self.grammar.find_identifiers(query)
                     if getattr(m, "kind", None) and getattr(m, "normalized", None)}
        except Exception:  # noqa: BLE001 - an unparsed query names no kind
            return []
        found = {(c.get("matched") or {}).get("identifier_kind") for c in candidates}
        return [_gap("unknown", "named_record_not_found", f"{kind}:{normalized}"[:700], (
            f"no record this reader may read holds {normalized} as a {kind}: the query "
            "names it, but every candidate listed is a lead (another kind of record, or "
            "another form, that shares its digits), not the record named; say that no "
            "such record was found and name the lead only as a lead"))
                for kind, normalized in sorted(named) if kind not in found]

    def _pinned_units(self, arguments: Mapping[str, Any]
                      ) -> set[tuple[str, str, str]] | None:
        """The live carriers a find whose filters pin its query (``_pinned``) can match:
        ``(source, relation, path)`` of each pinned property's sources in the asked
        type's bindings, as ``SourceResolver.resolve(units=)`` takes them. Every result
        must hold the query in a pinned property, so no other carrier is asked. None:
        nothing pinned (every carrier of the plan)."""
        pinned = _pinned(arguments)
        spec = (self.schema.object_types.get(arguments.get("object_type")) or {}
                if self.schema is not None and pinned else None)
        if spec is None:
            return None
        bindings = {b["id"]: b for b in spec.get("bindings", ())}
        return {(bindings[src["binding"]]["source"], bindings[src["binding"]]["relation"],
                 src["path"])
                for prop in spec.get("properties", ()) if prop.get("name") in pinned
                for src in prop.get("sources", ())
                if src.get("binding") in bindings and isinstance(src.get("path"), str)}

    def _with_scope_freshness(self, result: dict[str, Any], resolution: Any, *,
                              names_identifier: bool = True) -> dict[str, Any]:
        """The one rule for both answers of ``_find_sources`` (decision
        2026-10-04-scope-freshness): ``meta.freshness`` states every source family of
        the resolution's planned scope and how it was served (``live`` or
        ``persisted_capture`` with its label; the units not served at all with their
        HTTP status and error code), freshness gaps come first and are never cut, and a
        no-match is never ``absent_in_source``: live carriers were asked for a point
        value, not traversed, and a copy or an unavailable unit shows nothing
        (``absence_claims.traversal_problem``)."""
        entries = {item["source"]: dict(item) for item in result["meta"]["freshness"]}
        for source, units in sorted(resolution.scope_freshness.items()):
            entries[source] = _scope_freshness_entry(
                entries.get(source) or {"source": source, "captured_at": None,
                                        "completeness": "unknown", "stale": False},
                units)
        freshness = [entries[source] for source in sorted(entries)][:8]
        result["meta"] = dict(result["meta"], freshness=freshness)
        problem = traversal_problem(freshness)
        # A unit served from a copy or not at all: the no-match says why it shows nothing.
        not_live = any(unit["served_from"] != "live"
                       for units in resolution.scope_freshness.values()
                       for unit in units.values())
        fresh = self._source_gaps(freshness) + [
            g for g in result["gaps"] if g["reason"] == "schema_drift"]
        rest = []
        for gap in result["gaps"]:
            if gap["reason"] in FRESHNESS_REASONS or gap in fresh:
                continue
            if gap["state"] == "absent_in_source" or (
                    not_live and problem and gap["reason"] in ("no_match", "no_visible_match")):
                gap = _gap("unknown", "no_visible_match", gap["subject"],
                           (problem or "no record this reader may read carries it in the "
                            "live sources asked") +
                           "; absence from the source is not established",
                           gap.get("searched"))
            rest.append(gap)
        # A carrier read from a stale copy is the source's ``stale_source``: stated once
        # per source (its label above), never again per carrier.
        stale = {g["subject"] for g in fresh if g["reason"] == "stale_source"}
        for gap in _resolution_gaps(resolution, names_identifier=names_identifier):
            if gap["reason"] != "stale_source":
                rest.append(gap)
            elif gap["searched"]["sources"][0] not in stale:
                fresh.append(gap)
                stale.add(gap["searched"]["sources"][0])
        result["gaps"] = _freshness_first(fresh, rest)
        return result

    def _business_rivals(self, roots: Sequence[_Match], ranked: Sequence[_Match]) -> bool:
        """Whether a match beside ``roots`` (a lead: a same-number twin, a prefix form) is
        on a kind naming an object type of the ontology (a business number) and names a
        record none of the roots is: a real collision of a system's record id with a
        business number. Without one the roots stand (an item code, a supplier invoice
        number or a project key matched only as such is no collision)."""
        held = {m.object_id for m in roots}
        for match in ranked:
            if match.reason in ROOT_REASONS or match.object_id in held:
                continue
            try:
                kind_type = self.grammar.kind_spec(match.kind).get("object_type")
            except Exception:  # noqa: BLE001 - an unknown kind names no business record
                continue
            if self.schema is not None and kind_type in self.schema.object_types:
                return True
        return False

    def _only_system_keys(self, roots: Sequence[_Match]) -> bool:
        """Whether every root match is on a key of a kind that names no object type of
        the ontology (a system's own record id or document number: an FM record id is any
        integer). Such a key does not unseat the business numbers the same text reads as:
        their forms (a PO's same-number twin) stay candidates beside it, never hidden as
        its related leads (live: "PO 5457" searched as 5457 answered with FM record 5457,
        PO#5432, while PO#5457 rode on it unseen)."""
        if not roots or self.grammar is None or self.schema is None:
            return False
        for match in roots:
            try:
                kind_type = self.grammar.kind_spec(match.kind).get("object_type")
            except Exception:  # noqa: BLE001 - an unknown kind is read as a business key
                return False
            if kind_type in self.schema.object_types:
                return False
        return True

    def _match_query(self, scope: _Scope, query: str, keys: list[_Key],
                     kind_filter: str | None, object_type: str | None
                     ) -> tuple[list[_Match], list[dict], bool]:
        whole_keys = {(k.kind, k.normalized) for k in keys if k.whole}
        secondary_keys = {(k.kind, k.normalized) for k in keys if k.secondary}
        matches: list[_Match] = []
        whole_matches: list[_Match] = []
        rows = self.store.identifiers_by_keys(scope, [(k.kind or "", k.normalized) for k in keys])
        if self.aliases is not None:
            rows = list(rows) + self._reviewed_aliases(scope, keys)
        for row in rows:
            if _index_row_ok(row):
                match = self._identifier_match(row, typed=True)
                if (match.reason != REFERENCE_REASON
                        and (row["identifier_kind"], row["normalized"]) in secondary_keys):
                    # The query's label names another kind: a lead, never exact.
                    match.tier, match.reason = "lead", "secondary_reading"
                    match.why_not = _LABEL_KIND_CONFLICT
                matches.append(match)
                if (row["identifier_kind"], row["normalized"]) in whole_keys:
                    whole_matches.append(match)
        literal_rows = [] if kind_filter else self.store.identifiers_by_value(
            scope, _literal_forms(query))
        typed_seen = {(m.object_id, m.l1_handle, m.field_path, m.kind) for m in matches}
        for row in literal_rows:
            if (row["object_id"], row["l1_handle"], row["field_path"],
                    row["identifier_kind"]) in typed_seen or not _index_row_ok(row):
                continue
            match = self._identifier_match(row, typed=False)
            matches.append(match)
            whole_matches.append(match)
        for match in whole_matches:
            match.exact = exact_match(match.kind, match.raw, query, self.grammar)
        leads, truncated = self._lead_matches(scope, query, keys, kind_filter, typed_seen)
        matches.extend(leads)
        whole_matches.extend(leads)
        if object_type is not None:
            whole_matches = [m for m in whole_matches
                             if m.object_id.split(":", 1)[0] == object_type]
        # Display text is searched unless the whole query already resolved as an
        # identifier of a readable object. Several kinds accept almost any string,
        # so "the grammar parsed it" cannot be the test; "it matched" is. Every
        # row here passed the permission filter, so a denied match changes nothing.
        visible = self._visible_versions(scope, {m.object_id for m in whole_matches})
        notes: list[dict] = []
        if not visible and kind_filter is None:
            text, text_truncated, named, timed = self._text_matches(scope, query, keys,
                                                                    object_type)
            matches.extend(text)
            truncated = truncated or text_truncated
            if timed:
                notes.append(_gap(
                    "unknown", "time_budget", f"query:{' '.join(query.split())}"[:700],
                    f"the text search stopped at its {_find_text_seconds():g} s time "
                    f"budget: the {len(text)} record(s) found by then are listed, and more "
                    "may hold these words; narrow it with object_type, identifier_kind or "
                    "filters, or read document text with plx.documents"))
            if text_truncated and named:
                notes.append(_gap(
                    "unknown", "scan_cap", f"query:{' '.join(query.split())}"[:700],
                    f"{NAMED_READ_NOTE} ({named}) was read: they are listed "
                    f"first; other records holding the words may be cut; narrow it with "
                    f"object_type, identifier_kind or filters"))

        if self.grammar is None:
            notes.append(_gap("unknown", "grammar_unavailable", "identifier grammar",
                              "typed identifier parsing is off; only literal and text matches ran"))
        return matches, notes, truncated

    def _scope_identity(self, scope: _Scope, matches: Sequence[_Match], query: str | None
                        ) -> list[dict[str, Any]]:
        """Identity scope on the readable root-reason matches of a scoped kind (a customer
        PO: unique per customer; ``identity_scopes``), before any link is followed, a
        counterpart compared or a candidate ranked. A match is a root only when its record
        holds a readable scope value and, within that scope space (one system's column),
        every readable match of its value holds the same one: it gets the
        ``identity_scope`` feature and its exactness by ``exact_match``. Any other is a
        lead (``scope_lead``) whose why_not says why: ``no_scope_field``,
        ``no_scope_value``, ``scope_value_withheld`` or ``identity_scope_ambiguous`` (two
        or more scope values hold it, as two customers using one PO number: one gap per
        value, with the count this reader can read). The Portal resolves such a PO only
        with its customer and counts two live hits as no match
        (plx-customer-portal@40f7802 order-identity.ts:125-149).

        A display-text or filter match on a declared carrier of a scoped kind
        (``CARRIER_REASONS``, ``_carrier_typed``) is typed as that kind and read the same
        way: it keeps its reason, never becomes exact, and is a lead when no one scope
        holds it, so ``scoped_root_lead`` refuses ``PO 6000`` on a customerless order."""
        for match in matches:
            if match.reason in CARRIER_REASONS:
                self._carrier_typed(match)
        scoped = [m for m in matches if (m.reason in ROOT_REASONS or m.reason in
                                         CARRIER_REASONS) and scoped_kind(self.grammar, m.kind)]
        if not scoped:
            return []
        visible = self._visible_versions(scope, {m.object_id for m in scoped})
        scoped = [m for m in scoped if m.object_id in visible]
        readings = identity_scopes(
            self.store, self.grammar, self._binding_relations, scope.held,
            [(m.object_id, m.l1_handle, m.field_path, m.kind, m.raw) for m in scoped])
        holders: dict[tuple[str, str, str], set[str]] = {}
        for match in scoped:
            key = readings[(match.object_id, match.l1_handle, match.field_path)][0]
            if key is not None:
                holders.setdefault((match.kind, key[0], key[2]), set()).add(key[1])
        for match in scoped:
            key, feature, cited = readings[(match.object_id, match.l1_handle,
                                            match.field_path)]
            agrees = None
            if key is not None and len(holders[(match.kind, key[0], key[2])]) > 1:
                feature, agrees = "identity_scope_ambiguous", False
            elif key is not None:
                found: dict[str, Any] = {"feature": SCOPE_FEATURE, "agrees": True}
                if cited is not None:
                    found["citations"] = [cited]
                match.why_not = (found, *match.why_not)
                if match.reason in ROOT_REASONS:
                    match.exact = exact_match(match.kind, match.raw, query, self.grammar,
                                              True)
                continue
            match.tier, match.exact = "lead", False
            if match.reason in ROOT_REASONS:
                match.reason = SCOPE_LEAD_REASON
            match.why_not = ({"feature": feature, "agrees": agrees}, *match.why_not)
        return [_gap("unknown", "identity_scope_ambiguous", f"{kind}:{value}"[:700],
                     f"{len(held)} {space} values hold this {kind} among the records this "
                     "reader can read; it is unique only within one of them, so it names "
                     "no one record and no match is exact")
                for (kind, space, value), held in sorted(holders.items()) if len(held) > 1]

    def _carried(self, row: Mapping[str, Any]) -> bool:
        """Whether a value row is a declared identifier carrier's (``carrier_fields``):
        found by its identifier (its grammar and scope gate) or as display text, never as
        other text."""
        object_id = row["object_id"]
        relation = self._binding_relations.get(object_id.split(":", 2)[1]
                                               if object_id.count(":") >= 2 else "")
        return (relation, (row["field_path"] or "").partition(".")[2]) in self._carrier_fields

    def _carrier_typed(self, match: _Match) -> None:
        """Type a display-text or filter match whose matched field is a declared carrier
        of a scoped kind (``scoped_carriers``: the registry's relation and field) as that
        kind, its stored text normalised by the grammar; any other match is left as is."""
        relation = self._binding_relations.get(match.object_id.split(":", 2)[1]
                                               if match.object_id.count(":") >= 2 else "")
        kind = self._scoped_carriers.get((relation, match.field_path.partition(".")[2]))
        if kind is None or match.kind == kind:
            return
        try:
            normalized = self.grammar.normalize(kind, match.raw)
        except Exception:  # noqa: BLE001 - an unparsed value keeps the matched text
            normalized = None
        match.kind, match.normalized = kind, normalized or match.normalized

    def _identifier_match(self, row: Mapping[str, Any], *, typed: bool) -> _Match:
        if (row["object_type"], row["property"]) in self._references:
            tier, reason = "lead", REFERENCE_REASON
        elif not typed:
            tier, reason = "lead", "literal_identifier"
        else:
            # A reviewed alias (lineage workbench): a person confirmed it, so it is
            # reviewed, never verified.
            tier = ("lead" if row["lineage"] in NEVER_AUTHORITY else
                    "reviewed" if row["lineage"] == "workbench" else "verified")
            reason = "exact_identifier" if row["role"] == "primary" else "alias_match"
        return _Match(row["object_id"], tier, reason, row["role"], row["identifier_kind"],
                      row["normalized"], row["raw"], row["property"], row["binding_id"],
                      row["l1_handle"], row["field_path"])

    def _may_hold_document_text(self, object_type: str | None) -> bool:
        """Whether a record of ``object_type`` (None: any) can hold captured document
        text, so the ``document_text`` pass can match one. A field of that index belongs
        to an object projected from the document's own record, a record of the source
        its binding reads: a type none of whose bindings' sources has a field in the
        index (a customer: Business Central and Portal rows, never documents) matches
        nothing there, and that pass would only walk every chunk holding the words (a
        customer name in thousands of mails: 5-27 s per find on the live store) to
        drop each by type. Unknown (no schema, a source with no citation scheme): the
        pass runs."""
        if object_type is None or self.schema is None:
            return True
        sources = {b.get("source") for b in (self.schema.object_types.get(object_type) or {})
                   .get("bindings") or () if isinstance(b, Mapping)}
        schemes = [scheme for scheme, source in _SCHEME_SOURCE.items() if source in sources]
        if not sources or any(s not in _SCHEME_SOURCE.values() for s in sources):
            return True
        holds = getattr(self.store, "holds_text_of", None)
        return True if holds is None else holds(schemes)

    def _match_filters(self, scope: _Scope, object_type: str, filters: list[dict],
                       start: str = "", *, ids_only: bool = False
                       ) -> tuple[list[_Match], bool, set[str], str | None]:
        """Objects of one type whose readable values satisfy every filter, scanned in
        object-id order after ``start`` (a resumed scan, ``_read_scan_cursor``). The
        scan reads only readable objects (SQL), re-checks item ACLs, and counts only
        readable objects toward its cap; every match is returned (none dropped).
        Third: the filtered properties a verdict was undecided on (``_filter_objects``).
        Fourth: where a scan cut at its cap or row budget stopped (the last object id
        it decided on; the next scan starts after it), None when it reached the end.

        Inside a plan run (``run_memo``: one reader, one policy, one generation) a segment
        is scanned once: the pages of one listing resume in the same segment (a cursor's
        offset), and each would otherwise scan it again from its start.

        ``ids_only``: each match is only its ``object_id`` (``_MatchedId``), its matched
        identifier never read: a caller that counts or follows the records (plx.aggregate,
        a link filter's name lookup) never reads the identifiers of every match (21k
        sales orders of a dated count: 8.8 s of identifier reads, then dropped)."""
        kept = run_memo_for(("filter_segment", id(self), id(self.store.conn), scope.actor,
                             scope.audience_keys, scope.held, scope.marking_sets,
                             scope.sources, scope.at))
        key = (object_type, json.dumps(filters, sort_keys=True, default=str), start, ids_only)
        if kept is not None and key in kept:
            # copies: a caller marks its matches (tier, why_not), never the kept ones
            out, truncated, undecided, position = kept[key]
            return ([m if ids_only else dataclasses.replace(m) for m in out], truncated,
                    set(undecided), position)
        found = self._scan_filters(scope, object_type, filters, start, ids_only=ids_only)
        if kept is not None:
            kept[key] = (tuple(m if ids_only else dataclasses.replace(m) for m in found[0]),
                         found[1], frozenset(found[2]), found[3])
        return found

    def _narrowed_first(self, scope: _Scope, object_type: str,
                        narrowing: Sequence[tuple[str, dict[str, Any]]]) -> list[str] | None:
        """A selective narrowing read value first (``StoreReader.narrowed_ids``): only its
        records are then scanned, by their keys in batches, never span by span, the
        conditions still applied to them as before. None (the span scan runs): the store
        does not read values first, the narrowing is not selective, or the call's time is
        short. The read gets at most half the time left; cut there, the span scan answers
        what it decides in the rest (a scan short of time never answers nothing)."""
        if not getattr(self.store, "VALUES_FIRST", False):
            return None
        from operating import call_limits
        left = call_limits.seconds_left()
        if left is not None and left < 2 * SCAN_MARGIN_SECONDS:
            return None
        deadline = None if left is None else time.monotonic() + left / 2
        try:
            with call_limits.sql_deadline(deadline):
                return self.store.narrowed_ids(object_type, narrowing, scope.params()["marks"])
        except sqlite3.OperationalError:
            left = call_limits.seconds_left()
            if left is None or left <= 0:
                raise   # the call's own limit, not the read's half
            return None

    def _filter_matches(self, scope: _Scope, object_type: str, filters: list[dict],
                        matched: list[str], versions: Mapping[str, sqlite3.Row]
                        ) -> list[_Match]:
        """The ``filter_match`` of each readable object in ``matched`` (``versions``:
        their visible versions), keyed on its primary identifier or else its own key."""
        primary: dict[str, sqlite3.Row] = {}
        # A filter on a property carrying a scoped kind (a customer PO) matched on that
        # identifier: plx.find reads its scope (``_scope_identity``), whatever the spelling.
        filtered = {f["property"] for f in filters}
        declared = (self.schema.object_types.get(object_type, {}).get("properties", ())
                    if self.schema is not None else ())
        carried = {p["name"] for p in declared if isinstance(p, Mapping)
                   and p.get("name") in filtered
                   and scoped_kind(self.grammar, p.get("identifier_kind"))}
        for start in range(0, len(matched), 500):
            chunk = matched[start:start + 500]
            for row in self.store.identifiers_of(scope, chunk) if carried else ():
                if (row["property"] in carried and scoped_kind(self.grammar,
                                                               row["identifier_kind"])
                        and _index_row_ok(row)):
                    primary.setdefault(row["object_id"], row)
            for row in self.store.primary_identifiers(scope, chunk):
                if _index_row_ok(row):
                    primary.setdefault(row["object_id"], row)
        out = []
        for object_id in matched:
            row, version = primary.get(object_id), versions[object_id]
            tier = "lead" if version["lineage"] in NEVER_AUTHORITY else "verified"
            if row is not None:
                out.append(_Match(object_id, tier, "filter_match", "primary",
                                  row["identifier_kind"], row["normalized"], row["raw"],
                                  row["property"], row["binding_id"], row["l1_handle"],
                                  row["field_path"]))
            else:
                # No indexed identifier: the record's own key is what matched. The
                # normalized form is the id's whitespace-free (percent-encoded) native
                # part, a bare contract string; the decoded key is only ever raw, an
                # envelope (project.object_id_of).
                native = object_id.split(":", 2)[2]
                out.append(_Match(object_id, tier, "filter_match", "primary", "object_key",
                                  native[:200], unquote(native), "native_key",
                                  object_id.split(":", 2)[1], version["l1_handle"], ""))
        return out

    def default_days(self, object_type: str, prop: str) -> dict[str, frozenset]:
        """Per source binding, the days a bulk share of ``object_type.prop``'s records hold
        (``operating.source_defaults``: an import or migration day, never a date a person
        stated about one record), counted over the whole source column, cached per store
        generation. A store that cannot be counted has none (every date reads as stated)."""
        from operating.source_defaults import DATE_SHARE, bulk_values
        try:
            found = self.store.conn.execute("SELECT value FROM store_meta WHERE key = "
                                            "'content_digest'").fetchone()
            digest = found[0] if found else None
        except sqlite3.Error:
            digest = None
        cache = self.__dict__.setdefault("_default_days", {})
        key = (digest, object_type, prop)
        if digest is not None and key in cache:
            return cache[key]
        days: dict[str, list[str]] = {}
        try:
            for oid, value in self.store.conn.execute(
                    "SELECT o.object_id, v.value FROM objects o JOIN object_values v ON "
                    "v.object_id = o.object_id WHERE o.object_type = ? AND v.property = ? "
                    "AND v.superseded_at IS NULL AND v.value IS NOT NULL AND v.value <> ''",
                    (object_type, prop)):
                days.setdefault(oid.split(":", 2)[1] if oid.count(":") >= 2 else "",
                                []).append(str(value)[:10])
        except sqlite3.Error:
            days = {}
        out = {binding: found for binding, values in days.items()
               if (found := bulk_values(values, share=DATE_SHARE))}
        if digest is not None:
            if len(cache) > 64:
                cache.clear()
            cache[key] = out
        return out

    def bulk_default_dates(self, scope: _Scope, dated: Mapping[tuple[str, str], Any]
                           ) -> dict[tuple[str, str], dict[str, Any] | None]:
        """Of ``(object id, property) -> date value``, the dates that are a bulk default
        (an import or migration day), never a date of the record: the day is one a bulk
        share of its source column holds (``default_days``, the candidate test only: a
        busy real day is one too), and either

        * (a) the record is a copy whose origin or twin in another system (an identity
          twin no farther from the origin, ``origin_rank``) states a different date for
          the same property, or
        * (b) the property is a business date (declared ``date``, not a ``timestamp``)
          and the day is the record's own creation, migration or order stamp
          (``source_defaults.own_day`` over ``OWN_DAY_PROPS``: the proactive rules' one
          date-provenance test).

        Each judged date maps to the origin's stated date (``object_id``, ``value``,
        ``value_type``: the nearest-origin twin's) or None: the record states no date."""
        from operating.source_defaults import OWN_DAY_PROPS, own_day
        candidates: dict[tuple[str, str], str] = {}
        # one ``default_days`` per (type, property), never one per record: each call reads
        # the store's digest first, a round trip apiece on Postgres (21k sales orders of a
        # date count: 13 s of 80)
        columns: dict[tuple[str, str], dict[str, frozenset]] = {}
        for (oid, prop), value in dated.items():
            object_type, _, rest = oid.partition(":")
            day = str(value)[:10]
            column = columns.get((object_type, prop))
            if column is None:
                column = columns[(object_type, prop)] = self.default_days(object_type, prop)
            if day in column.get(rest.split(":", 1)[0], ()):
                candidates[(oid, prop)] = day
        if not candidates:
            return {}
        ids = sorted({oid for oid, _p in candidates})
        props = sorted({p for _o, p in candidates})
        twins = self._identity_twins(scope, ids)
        twin_ids = sorted({t for found in twins.values() for t in found})
        stated: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in self.store.readable_values(scope, twin_ids, props) if twin_ids else ():
            if row["value_type"] in ("date", "timestamp") and row["value"]:
                stated.setdefault((row["object_id"], row["property"]), []).append(row)
        stamps: dict[str, list[tuple[str, dt.date]]] = {}
        for row in self.store.readable_values(scope, ids, sorted(OWN_DAY_PROPS)):
            day = _parse_day(str(row["value"] or "")[:10])
            if day is not None:
                stamps.setdefault(row["object_id"], []).append((row["property"], day))
        lineage = self._visible_versions(scope, ids)
        out: dict[tuple[str, str], dict[str, Any] | None] = {}
        for (oid, prop), day in sorted(candidates.items()):
            object_type, _, rest = oid.partition(":")
            binding = rest.split(":", 1)[0]
            own_rank = origin_rank(lineage[oid]["lineage"]) if oid in lineage else 3
            origin = None
            for twin, twin_lineage in sorted(twins.get(oid, {}).items(),
                                             key=lambda t: (origin_rank(t[1]), t[0])):
                if twin.split(":", 2)[1] == binding or origin_rank(twin_lineage) > own_rank:
                    continue   # the same system, or a copy of this record
                other = next((r for r in stated.get((twin, prop), ())
                              if str(r["value"])[:10] != day), None)
                if other is not None:
                    origin = {"object_id": twin, "value": other["value"],
                              "value_type": other["value_type"]}
                    break
            if origin is not None:
                out[(oid, prop)] = origin
                continue
            business = self._declared_value_type(object_type, prop) == "date"
            own = [d for p, d in stamps.get(oid, ()) if p != prop]
            if business and own_day(_parse_day(day), own):
                out[(oid, prop)] = None
        return out

    def _declared_value_type(self, object_type: str, prop: str) -> str | None:
        declared = (self.schema.object_types.get(object_type, {}).get("properties", ())
                    if self.schema is not None else ())
        return next((p.get("value_type") for p in declared
                     if isinstance(p, Mapping) and p.get("name") == prop), None)

    def _undated_defaults(self, scope: _Scope,
                          values: dict[str, dict[str, list[tuple[str, Any]]]],
                          filters: list[dict]) -> set[str]:
        """A filtered date that is a bulk default (``bulk_default_dates``) is no date the
        record states: it is read as its origin's stated date when one exists, else
        dropped, and a record left with no date of that property is neither matched nor
        excluded on it. Returns the markers (``undated_marker``) of those properties,
        which the caller's undecided set carries to its gap (``_filter_gaps``)."""
        dated_props = {f["property"] for f in filters if f.get("op") not in ("present", "absent")}
        dated = {(oid, prop): v for oid, held in values.items()
                 for prop in dated_props & set(held)
                 for kind, v in held[prop] if kind in ("date", "timestamp")}
        if not dated:
            return set()
        judged = self.bulk_default_dates(scope, dated)
        undated: set[str] = set()
        for (oid, prop), origin in judged.items():
            day = str(dated[(oid, prop)])[:10]
            items = values[oid][prop]
            kept = [(kind, v) for kind, v in items
                    if kind not in ("date", "timestamp") or str(v)[:10] != day]
            if origin is not None:
                kept.append((origin["value_type"], origin["value"]))
            values[oid][prop] = kept
            if not kept:
                undated.add(undated_marker(prop))
        return undated

    def _filter_objects(self, scope: _Scope, object_ids: list[str], filters: list[dict]
                        ) -> tuple[set[str], set[str]]:
        """(objects every filter holds on, the filtered properties a verdict was
        undecided on): an object holding a value of a filtered property this reader may
        not read is matched only where its readable values already decide it, never
        read as missing that value (``_filters_verdict``)."""
        values: dict[str, dict[str, list[tuple[str, Any]]]] = {}
        # the filtered properties' values alone: no verdict reads another property
        for row in self.store.readable_values(scope, object_ids,
                                              sorted({f["property"] for f in filters})):
            values.setdefault(row["object_id"], {}).setdefault(row["property"], []).append(
                (row["value_type"], row["value"]))
        withheld: dict[str, set[str]] = {}
        for row in self.store.withheld_values(scope, object_ids,
                                              sorted({f["property"] for f in filters})):
            withheld.setdefault(row["object_id"], set()).add(row["property"])
        # a filter naming an event of the record's type (not a property it declares)
        # reads that event's times as the filtered values
        timed: dict[str, str] = {}
        for object_type in {oid.split(":", 1)[0] for oid in object_ids}:
            names, declared = self.event_names(object_type), self._declared_names(object_type)
            timed.update({names[f["property"]]: f["property"] for f in filters
                          if f.get("property") in names and f["property"] not in declared})
        if timed:
            # an event filtered by days (every bound a bare date) reads each time as its
            # business day (Eastern, as date groups are); a time bound reads it as it is
            by_day = {name for name in timed.values()
                      if all(isinstance(v, str) and _DATE.match(v)
                             for f in filters if f.get("property") == name
                             and f.get("op") not in ("present", "absent")
                             for v in (f.get("value") if isinstance(f.get("value"), list)
                                       else [f.get("value")]))}
            for row in self.store.event_times(scope, object_ids, sorted(timed)):
                name = timed[row["event_type"]]
                if not row["readable"]:
                    withheld.setdefault(row["object_id"], set()).add(name)
                    continue
                time_text = str(row["time"])
                if name in by_day:
                    time_text = _business_day(time_text)
                kind = "date" if _DATE.match(time_text) else "timestamp"
                values.setdefault(row["object_id"], {}).setdefault(name, []).append(
                    (kind, time_text))
        undated = self._undated_defaults(scope, values, filters)
        passing: set[str] = set()
        undecided: set[str] = set(undated)
        for oid in object_ids:
            verdict, unsure = _filters_verdict(values.get(oid, {}), withheld.get(oid, set()),
                                               filters)
            if verdict:
                passing.add(oid)
            undecided |= unsure
        return passing, undecided

    # ------------------------------------------------------------------ links

    def _linked_matches(self, scope: _Scope, matches: Sequence[_Match], object_type: str
                        ) -> list[_Match]:
        """Records of ``object_type`` a typed identifier match on a record of another
        type names through a declared single-valued link visible to this reader."""
        from ontology import resolve
        targets = [b["id"] for b in self.schema.object_types.get(object_type, {})
                   .get("bindings", ())]
        best: dict[str, _Match] = {}
        for match in matches:
            held = best.get(match.object_id)
            # Another type's record only: a record of the asked type is a candidate
            # itself, and its twin in another binding is an identity question
            # (plx.identity), not what the identifier names. A linked_identifier hop is
            # suppressed only for
            # (1) a kind ambiguous across tables: two or more types key it within a
            # binding (an FM id names a record in each FM table, so its holder's owner
            # is no record the value names), and (2) a kind the asked type keys within
            # a binding itself (an FM order's id asked of Customer names the FM
            # customer with that id). Every other hop (an order, PO or invoice number
            # to its customer or supplier) is followed.
            # A scope lead is followed too: the record carrying the PO is a candidate, a
            # lead that keeps the scope feature saying why (never a root).
            if ((match.reason in ROOT_REASONS or match.reason == SCOPE_LEAD_REASON)
                    and match.object_id.split(":", 1)[0] != object_type
                    and match.kind not in self._table_ambiguous_kinds
                    and match.kind not in self._binding_scoped_kinds.get(object_type, ())
                    and (held is None or match.sort_key() < held.sort_key())):
                best[match.object_id] = match
        visible = self._visible_versions(scope, best)
        best = {oid: match for oid, match in best.items() if oid in visible}
        hops: dict[str, list[tuple[Any, str]]] = {}
        for oid, match in best.items():
            for target in targets:
                if target != match.binding:
                    hops[oid] = hops.get(oid, []) + [
                        (hop, target) for hop in resolve.link_hops(self.schema, match.binding,
                                                                   target)]
        hops = {oid: found for oid, found in hops.items() if found}
        if not hops:
            return []
        rows = self.store.links_touching(scope, sorted(hops), sorted(
            {hop.link for found in hops.values() for hop, _ in found}))
        out: list[_Match] = []
        for row in rows:
            if row["tier"] == "contradicted" or not all(
                    self._item_allowed(scope.actor, handle)
                    for handle in json.loads(row["evidence_handles"] or "[]")):
                continue
            for oid, other in ((row["from_object"], row["to_object"]),
                               (row["to_object"], row["from_object"])):
                forward = oid == row["from_object"]
                if not any(hop.link == row["link_type"] and hop.kind == row["binding_kind"]
                           and hop.forward == forward and other.split(":", 2)[1] == target
                           for hop, target in hops.get(oid, ())):
                    continue
                edge = _edge(row, True)
                if edge is None:
                    continue
                # The link row's markings and evidence passed this reader's scope and
                # its far end must too (the caller keeps only visible candidates), so
                # the compared key values it cites are this reader's to see.
                feature: dict[str, Any] = {"feature": "linked_record", "agrees": True}
                cites = [c for f in edge["reason"] for c in f.get("citations", ())][:4]
                if cites:
                    feature["citations"] = cites
                source = best[oid]
                tier = max(source.tier, row["tier"], key=lambda t: _TIER_RANK[t])
                scoped = tuple(f for f in source.why_not if f.get("feature") in SCOPE_FEATURES)
                out.append(_Match(other, tier, LINKED_REASON, source.role, source.kind,
                                  source.normalized, source.raw, source.property,
                                  source.binding, source.l1_handle, source.field_path,
                                  (feature, *scoped), source.exact))
        return out

    # ------------------------------------------------------------------ corroboration

    def _corroborate(self, scope: _Scope, page: Sequence[_Match],
                     counterparts: Sequence[_Match]) -> dict[str, list[dict]]:
        """object_id -> corroboration features of each candidate on the page whose
        binding the schema compares with a binding of another source family."""
        if self.schema is None or not page:
            return {}
        from ontology import resolve
        binding_of = lambda oid: oid.split(":", 2)[1]  # noqa: E731
        schema = self.schema
        for match in page:
            binding = binding_of(match.object_id)
            if binding not in self._compared:
                self._compared[binding] = _schema_memo(schema, ("compared", binding), lambda: [
                    b["id"] for spec in schema.object_types.values()
                    for b in spec.get("bindings", ())
                    if resolve.comparable(schema, b["id"], binding)])
        wanted = {m.object_id: self._compared[binding_of(m.object_id)] for m in page}
        wanted = {oid: found for oid, found in wanted.items() if found}
        if not wanted:
            return {}
        peers = self._visible_versions(scope, {m.object_id for m in counterparts})
        reading = sorted(set(wanted) | set(peers))
        values: dict[str, list[sqlite3.Row]] = {}
        for row in self.store.readable_values(scope, reading):
            values.setdefault(row["object_id"], []).append(row)
        idents: dict[str, list[sqlite3.Row]] = {}
        for row in self.store.identifiers_of(scope, reading):
            if self._item_allowed(scope.actor, row["l1_handle"]):
                idents.setdefault(row["object_id"], []).append(row)
        out: dict[str, list[dict]] = {}
        for oid in sorted(wanted):
            checks: list[Any] = []
            seen = 0
            for peer in sorted(peers):
                if peer == oid or binding_of(peer) not in wanted[oid]:
                    continue
                seen += 1
                # A comparison with no value on either side says nothing: left out.
                checks += [check for check in self._checks(scope, oid, peer, values, idents)
                           if check.counterpart or check.candidate]
            out[oid] = _corroboration_features(resolve.verdict(checks), checks, seen)
        return out

    def _checks(self, scope: _Scope, oid: str, peer: str,
                values: Mapping[str, list[sqlite3.Row]],
                idents: Mapping[str, list[sqlite3.Row]]) -> list[Any]:
        """The schema's comparisons of one candidate with one counterpart."""
        from ontology import resolve
        cand_binding, peer_binding = oid.split(":", 2)[1], peer.split(":", 2)[1]
        out = []
        declared = {p["name"]: p for p in (self.schema.object_types.get(
            oid.split(":", 1)[0]) or {}).get("properties", ())}
        schema = self.schema
        for prop, value_type in _schema_memo(schema, ("shared", peer_binding, cand_binding),
                                             lambda: tuple(resolve.shared_properties(
                                                 schema, peer_binding, cand_binding))):
            spec = declared.get(prop)
            out.append(resolve.compare_shared(
                prop, value_type, _cited_values(values.get(peer, ()), prop, value_type, spec),
                _cited_values(values.get(oid, ()), prop, value_type, spec)))
        normalize = getattr(self.grammar, "normalize", None)
        for check in _schema_memo(schema, ("corroborations", peer_binding, cand_binding),
                                  lambda: tuple(resolve.corroborations(
                                      schema, peer_binding, cand_binding))):
            root = [_cited(row, row["raw"]) for row in idents.get(peer, ())
                    if row["identifier_kind"] == check.kind
                    and _column_path(row["field_path"]) == check.root_path]
            out.append(resolve.compare_linked(
                check, [value for value in root if value is not None],
                self._linked_identifiers(scope, oid, check), normalize))
        return out

    def _linked_identifiers(self, scope: _Scope, oid: str, check: Any
                            ) -> list[tuple[str, Any]] | None:
        """``(path, cited raw value)`` of the identifiers of ``check.kind`` held by the one
        record the candidate's fk link ``check.link`` names, when exactly one such record
        and its link are visible to this reader; else None."""
        rows = [row for row in self.store.links_touching(scope, [oid], [check.link])
                if row["from_object"] == oid and row["binding_kind"] == "fk"
                and row["tier"] != "contradicted"
                and row["to_object"].split(":", 2)[1] == check.linked_binding
                and all(self._item_allowed(scope.actor, handle)
                        for handle in json.loads(row["evidence_handles"] or "[]"))]
        linked = sorted({row["to_object"] for row in rows})
        if len(linked) != 1 or not self._visible_versions(scope, linked):
            return None
        out = []
        for row in self.store.identifiers_of(scope, linked):
            path = _column_path(row["field_path"])
            if (row["identifier_kind"] == check.kind and path in check.linked_paths
                    and self._item_allowed(scope.actor, row["l1_handle"])):
                value = _cited(row, row["raw"])
                if value is not None:
                    out.append((path, value))
        return out

    # ------------------------------------------------------------------ sets

    def _new_set(self, actor: Actor, object_ids: list[str],
                 links: Iterable[str] | None = None, complete: bool = False,
                 total: int | None = None) -> str:
        return self.sets.new(actor, object_ids, links=links, complete=complete, total=total)

    def resolve_set(self, handle: str, actor: Actor) -> tuple[str, ...] | None:
        """Object ids of a set handle this actor created, or None (unknown, expired
        or another actor's: indistinguishable). Callers re-check visibility."""
        return self.sets.resolve(handle, actor)
