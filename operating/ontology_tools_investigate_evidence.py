"""The evidence of ``InvestigateTools``: reading, counting and harvesting the records a
plan's steps reach, and the gaps each read leaves.

Split from ``operating/ontology_tools_investigate.py``; moved unchanged.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import itertools
from typing import Any, Callable, Mapping, Sequence
from ontology.json_contract import validate
from ontology.types import Actor
from ontology.units import unit_token
from .ontology_tools_identity import _compare_key
from .ontology_tools_investigate_gaps import (
    _as_corrected, _binding_of, _corrected_under, _element_gaps, _empty_value, _gap,
    _type_of, _value_text,
)
from .ontology_tools_investigate_plans import (
    ACTUALS_MARKING, ESTIMATES_MARKING, RECORD_READS, _Collection, _EVIDENCE_TIERS,
    _Run, _VALUE, _canonical, say_direction,
)
from .ontology_tools_investigate_pages import (
    _add_conflict, _api_row, _cell_of, _cells, _citation_of, _merge_withheld, _own_key, _records,
    _ref_handle, _select_elements, _select_path,
)
from .ontology_tools_investigate_host import gate_rows


class _EvidenceMixin:
    """Methods of ``InvestigateTools`` (``ontology_tools_investigate_evidence``); mixed in there, never used alone."""

    # ------------------------------------------------------------------ evidence

    def _objects_of(self, run: _Run, item: Mapping[str, Any]) -> list[str]:
        bindings = set(item.get("bindings") or ())
        return sorted(oid for oid in run.objects if _type_of(oid) == item["object_type"]
                      and (not bindings or _binding_of(oid) in bindings))

    def _evidence(self, run: _Run) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(answer entries, the tool values they cite)."""
        answer: list[dict[str, Any]] = []
        cited: list[dict[str, Any]] = []
        produced: dict[str, list[dict[str, Any]]] = {}
        leads = self._lead_only(run)
        for item in run.plan.doc["evidence"]:
            kind = item["kind"]
            if kind == "property":
                entries = self._property(run, item, cited)
            elif kind == "count":
                entries = self._count(run, item)
            elif kind == "compute":
                entries = self._compute(run, item, produced)
            else:
                entries = self._select(run, item, produced)
            produced[item["name"]] = entries
            for entry in entries:
                basis = entry.get("basis") or [entry.get("object_id")]
                if leads.intersection(basis):
                    entry["lead_basis"] = True
            named = {e["name"]: e for e in answer + entries}
            for entry in entries:  # a value computed from an entry resting on a lead does too
                if any(named.get(n, {}).get("lead_basis") for n in entry.get("inputs") or ()):
                    entry["lead_basis"] = True
            answer.extend(entries)
        if run.withhold_entries:
            dropped = [e for e in answer if e["name"] in run.withhold_entries]
            cells = {(e["citation"]["l1_handle"], e["citation"]["field_path"])
                     for e in dropped if isinstance(e.get("citation"), Mapping)}
            answer = [e for e in answer if e["name"] not in run.withhold_entries]
            cited = [v for v in cited if not isinstance(v.get("citation"), Mapping) or (
                v["citation"].get("l1_handle"), v["citation"].get("field_path")) not in cells]
            # The record itself goes too (its display is its subject or name, its edges
            # cite it): a chain the answer had room for showed a withheld mail's subject
            # (AUD-11 paging; before, only the chain's trim to one response hid it).
            roots = set((run.collections.get("root") or _Collection()).ids)
            handles = {handle for handle, _path in cells}
            held = {e["object_id"] for e in dropped if isinstance(e.get("object_id"), str)}
            held |= {oid for oid, ref in run.objects.items()
                     if _ref_handle(ref) in handles}
            run.withheld_objects |= held - roots
        return answer, cited

    @staticmethod
    def _lead_only(run: _Run) -> set[str]:
        """Objects the run reached only through links below the default tier floor: not
        connected to a root by a chain of links at an evidence tier."""
        strong: dict[str, set[str]] = {}
        for edge in run.edges.values():
            if edge.get("tier") in _EVIDENCE_TIERS:
                strong.setdefault(edge["from"], set()).add(edge["to"])
                strong.setdefault(edge["to"], set()).add(edge["from"])
        roots = (run.collections.get("root") or _Collection()).ids
        seen, frontier = set(roots), list(roots)
        while frontier:
            for other in strong.get(frontier.pop(), ()):
                if other not in seen:
                    seen.add(other)
                    frontier.append(other)
        return set(run.objects) - seen

    def _missing(self, run: _Run, item: Mapping[str, Any], state: str, reason: str,
                 subject: str, detail: str, name: str | None = None) -> list[dict[str, Any]]:
        if not item.get("required"):
            return []
        if item.get("quantity_role"):
            # A quantity is named by its role (what the question asks), the type in detail.
            subject, detail = f"{item['quantity_role']} quantity", f"{subject}: {detail}"
        run.add_gap(_gap(state, reason, subject, detail, name or item["name"]))
        run.reason(f"missing_{item['name']}")
        return [{"name": name or item["name"], "kind": item["kind"], "value": None,
                 "check": "not_applicable"}]

    def _markings_lacked(self, run: _Run, type_id: str, props: Sequence[str]) -> list[str]:
        """The schema markings this reader lacks when every one of ``props`` carries one it
        does not hold (from the schema and the reader's own grant, never from a row); empty
        when one property carries none it lacks or the host cannot say what it holds."""
        if self.held_markings is None:
            return []
        try:
            held = set(self.held_markings(run.actor))
        except Exception:  # noqa: BLE001 - an undecided grant decides nothing here
            return []
        declared = {p["name"]: p for p in (self.schema.object_types.get(type_id) or {}
                                           ).get("properties") or ()}
        lacked: set[str] = set()
        for prop in props:
            missing = set((declared.get(prop) or {}).get("markings") or ()) - held
            if not missing:
                return []
            lacked |= missing
        return sorted(lacked)

    def _blank_in_source(self, run: _Run, type_id: str, props: Sequence[str],
                         objects: Sequence[str]) -> list[tuple[str, dict[str, Any]]]:
        """AUD08-GAPSTATE: for a property no reached record carries, the empty cells that
        say why, (object, {property, citation of the cell}) each (``_blank_entry`` makes
        them the ``absent_in_source`` / ``value_empty`` gaps a stored null made before
        AUD-08). Only
        for a property plx.object reported dropped by its source's tests
        (``source_condition_unmet``) on a source that has tests, and only when every
        object's own cell is re-read through this reader's rows (as the claim gate reads
        it: refused, withheld, changed since, absent or unlocatable is no proof; located
        from a citation of the object's own record, ``_citation_of``) and holds
        nothing: blank or null, never a 0 (a 0 stays a value the tests failed). Else none,
        and the property stays ``not_carried``. Reader side only: nothing stored moves."""
        from operating.claim_checks import dig
        from operating.claim_checks_parse import _withheld
        if self.rows is None or not objects:
            return []
        unmet = {g.get("subject") for g in run.gaps
                 if isinstance(g, Mapping) and g.get("reason") == "source_condition_unmet"}
        declared = {p["name"]: p for p in (self.schema.object_types.get(type_id) or {}
                                           ).get("properties") or () if isinstance(p, Mapping)}
        rows = gate_rows(self.rows, run.actor, run.as_known_at)
        out: list[tuple[str, dict[str, Any]]] = []
        for oid in objects:
            binding = oid.partition(":")[2].partition(":")[0]
            for prop in props:
                spec = declared.get(prop)
                if spec is None or f"{type_id}.{prop}" not in unmet:
                    continue
                source = next((s for s in spec.get("sources") or ()
                               if isinstance(s, Mapping) and s.get("binding") == binding
                               and s.get("when") and isinstance(s.get("path"), str)), None)
                cite = source and _cell_of(declared, binding, _citation_of(run, oid),
                                           source["path"])
                relation = run.relations.get(binding)
                if not cite or (relation is not None and _api_row(cite["l1_handle"])
                                != (relation, _own_key(oid))):
                    continue   # not shown to be this object's own row
                resolved = rows(cite["l1_handle"])
                if not isinstance(resolved, Mapping) or resolved.get("changed_since") \
                        or _withheld(resolved, cite["field_path"]):
                    continue
                there, cell = dig(resolved, cite["field_path"])
                if not there or not (cell is None or (isinstance(cell, str)
                                                      and not cell.strip())):
                    continue
                out.append((oid, {"property": prop, "citation": cite}))
                break
            else:
                return []   # one object not shown blank in its source: none is said
        return out

    def _blank_entry(self, run: _Run, item: Mapping[str, Any],
                     blank: Sequence[tuple[str, Mapping[str, Any]]]) -> list[dict[str, Any]]:
        """The answer entry of a property every reached record holds blank in its source
        (``_blank_in_source``): no value, and per record the ``value_empty`` gap in the
        item's null state (``if_null``, default ``absent_in_source``) a stored null gave
        before AUD-08. The entry cites nothing: a record version's digest and lineage are
        known only from a stored value, and none is."""
        if item.get("required"):
            run.reason(f"missing_{item['name']}")
        for oid, found in blank:
            run.add_gap(_gap(item.get("if_null", "absent_in_source"), "value_empty",
                             f"{_type_of(oid)}.{found['property']}",
                             f"the record's {found['citation']['field_path']} is empty in "
                             "its source", item["name"]))
        return [{"name": item["name"], "kind": item["kind"], "value": None,
                 "check": "not_applicable"}]

    def _property(self, run: _Run, item: Mapping[str, Any],
                  cited: list[dict[str, Any]]) -> list[dict[str, Any]]:
        objects = self._objects_of(run, item)
        type_id, props = item["object_type"], item["properties"]
        if not objects:
            return self._missing(
                run, item, item.get("if_no_object", "unknown"), "no_object_reached", type_id,
                f"the plan reached no {type_id} this reader can see")
        unread = [oid for oid in objects if oid not in run.read]
        if len(unread) == len(objects):
            return self._missing(run, item, "unknown", "not_read", f"{type_id}",
                                 f"{len(objects)} {type_id} reached but not read")
        found: list[tuple[str, Mapping[str, Any]]] = []
        for oid in objects:
            if oid in unread:
                continue
            values = [v for v in run.values.get(oid, [])
                      if not item.get("omit_empty") or not _empty_value(v.get("value"))]
            for prop in props:
                carried = [v for v in values if v.get("property") == prop]
                if carried:
                    found.extend((oid, v) for v in carried)
                    break
        if not found and not item.get("omit_empty"):
            # AUD-08: a value its source's tests drop (a 0 or blank FM final quantity) is
            # no stored null any more; a blank cell, re-read, is still absent in source
            blank = self._blank_in_source(run, type_id, props,
                                          [oid for oid in objects if oid not in unread])
            if blank:
                return self._blank_entry(run, item, blank)
        if not found:
            lacked = self._markings_lacked(run, type_id, props)
            if lacked:
                # Every property read here carries a marking this reader does not hold: a
                # withheld value, never a missing one (whether a record holds one is not said).
                return self._missing(
                    run, item, "inaccessible", "marking_withheld", f"{type_id}.{'|'.join(props)}",
                    f"this property carries the {', '.join(lacked)} marking, which this reader "
                    "does not hold; whether a reached record holds a value is not shown")
            return self._missing(run, item, "unknown", "not_carried",
                                 f"{type_id}.{'|'.join(props)}",
                                 "no reached record carries a readable value of this property")
        if item.get("total"):
            return self._total(run, item, objects, cited)
        many = len(found) > 1 or item.get("numbering") == "value"
        if item.get("numbering") == "value":
            found.sort(key=lambda pair: (_value_text(pair[1].get("value")) or "", pair[0]))
        entries = [self._value_entry(run, item, oid, value,
                                     f"{item['name']}_{index}" if many else item["name"], cited)
                   for index, (oid, value) in enumerate(found, start=1)]
        # Records agree when their values do: a value is its source text (each record's
        # citation differs) compared by type, with its currency and unit.
        distinct = {(_compare_key(e["value_type"], _value_text(e["value"]) or _canonical(e["value"])
                                  if isinstance(e["value"], Mapping) else e["value"]),
                     e.get("currency"), unit_token(e.get("unit")) or e.get("unit"))
                    for e in entries if e["value"] is not None}
        if item.get("single") and len(distinct) > 1:
            _add_conflict(run.conflicts, {"kind": "value_disagreement",
                                          "subject": f"{type_id}.{'|'.join(props)}"[:700],
                                          "values": [v for _, v in found][:12]})
            run.reason(f"conflict_{item['name']}")
        return entries

    def _value_entry(self, run: _Run, item: Mapping[str, Any], oid: str,
                     value: Mapping[str, Any], name: str,
                     cited: list[dict[str, Any]]) -> dict[str, Any]:
        """The answer entry of one read value (a null one is its value_empty gap)."""
        entry = {"name": name[:64], "kind": "property", "value": value.get("value"),
                 "value_type": value["value_type"], "object_id": oid,
                 "object_type": _type_of(oid), "property": value["property"],
                 "citation": value["citation"], "check": "not_checked"}
        role = item.get("quantity_role") or value.get("quantity_role")
        if role:
            entry["quantity_role"] = role
        for key in ("unit", "currency"):
            if key in value:
                entry[key] = value[key]
        if item.get("canonical_unit") and unit_token(entry.get("unit")) is not None:
            # One unit vocabulary (ontology/units.py): each -> ea; the claim gate still
            # checks the cited cell in its own spelling (_source_unit).
            entry["unit"] = unit_token(entry["unit"])
        if isinstance(value.get("review"), Mapping):
            # A person's review in force (plx.object), beside the source value and its
            # citation, never in their place.
            entry["review"] = dict(value["review"])
        if value.get("value") is None:
            entry["check"] = "not_applicable"
            if item.get("required"):
                run.reason(f"missing_{item['name']}")
            run.add_gap(_gap(item.get("if_null", "absent_in_source"), "value_empty",
                             f"{_type_of(oid)}.{value['property']}",
                             f"the cited record's {value['citation']['field_path']} is "
                             "empty", item["name"]))
        elif value not in cited:
            cited.append(value)
        return entry

    def _total(self, run: _Run, item: Mapping[str, Any], objects: Sequence[str],
               cited: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """A ``total: sum`` quantity: per unit, the sum of the reached objects' values (per
        object its ``prefer`` records' valued sum, in its unit when they carry none, when it
        has one, else its own value), as
        one computed entry over the cited lines (``{name}_line_<n>``); a unit of one line
        is that line's entry. Named ``name``, or ``name_<n>`` per unit in the unit's order.
        Any reached object unread or carrying no value, a record ``prefer`` cannot tie to
        one object, a non-numeric value or an incomplete run leaves the total unknown."""
        from .claim_checks_parse import _plain, to_decimal

        type_id, props = item["object_type"], item["properties"]
        parts: list[tuple[str, Mapping[str, Any]]] = []

        def first(oid: str, wanted: Sequence[str]) -> Mapping[str, Any] | None:
            for prop in wanted:
                carried = [v for v in run.values.get(oid, []) if v.get("property") == prop]
                if carried:
                    return carried[0] if len(carried) == 1 else None
            return None

        def unknown(reason: str, detail: str) -> list[dict[str, Any]]:
            return self._missing(run, item, "unknown", reason, f"{type_id}.{'|'.join(props)}",
                                 detail)
        if run.truncated is not None or run.stopped:
            return unknown("total_not_determined", "the run did not read every record it "
                           f"reached ({run.truncated or 'stopped'}), so no total is stated")
        prefer = item.get("prefer")
        for oid in objects:
            if oid not in run.read:
                return unknown("not_read", f"{oid} was reached but not read, so no total "
                               "is stated")
            if prefer is not None:
                details, why = self._preferred(run, oid, prefer)
                if why is not None:
                    return unknown(*why)
                valued = [(d, v) for d in details
                          for v in [first(d, [prefer["property"]])]
                          if v is not None and v.get("value") is not None]
                own = first(oid, props)
                if valued and own is not None and own.get("unit") is not None:
                    # A record that carries no unit counts in its object's unit.
                    valued = [(d, v if v.get("unit") is not None else {**v, "unit": own["unit"]})
                              for d, v in valued]
                if valued:
                    parts.extend(valued)
                    continue
            own = first(oid, props)
            if own is None and not item.get("omit_empty"):
                # AUD08-GAPSTATE: its blank cell, re-read: absent in source, no total
                blank = self._blank_in_source(run, type_id, props, [oid])
                if blank:
                    return self._blank_entry(run, item, blank)
            if own is None:
                return unknown("not_carried", f"{oid} carries no one readable value of this "
                               "property, so no total is stated")
            parts.append((oid, own))
        if len(parts) == 1:
            return [self._value_entry(run, item, *parts[0], item["name"], cited)]
        groups: dict[str | None, list[tuple[str, Mapping[str, Any]]]] = {}
        for oid, value in parts:
            if value.get("value") is not None and to_decimal(value["value"]) is None:
                return unknown("total_not_determined", f"{oid}'s value is not a number")
            unit = value.get("unit")
            groups.setdefault(unit.strip() if isinstance(unit, str) and unit.strip() else None,
                              []).append((oid, value))
        if any(len(rows) > 50 for rows in groups.values()):
            return unknown("total_not_determined", "more than 50 records of one unit")
        entries: list[dict[str, Any]] = []
        line = 0
        for index, unit in enumerate(sorted(groups, key=lambda u: (u is None, u or "")), 1):
            name = item["name"] if len(groups) == 1 else f"{item['name']}_{index}"
            rows = groups[unit]
            if len(rows) == 1:
                entries.append(self._value_entry(run, item, *rows[0], name, cited))
                continue
            lines = []
            for oid, value in rows:
                line += 1
                lines.append(self._value_entry(run, item, oid, value,
                                               f"{item['name']}_line_{line}", cited))
            numbers = [to_decimal(e["value"]) for e in lines]
            value = None if None in numbers else _plain(sum(numbers[1:], numbers[0]))
            steps = [(" + ".join(str(e["value"]) for e in lines) + f" = {value}")[:400]] \
                if value is not None else []
            total = {"name": name[:64], "kind": "computed", "value": value,
                     "value_type": "quantity", "inputs": [e["name"] for e in lines],
                     "steps": steps, "check": "not_applicable"}
            for key in ("quantity_role", "unit"):
                if lines[0].get(key) is not None:
                    total[key] = lines[0][key]
            total["digest"] = hashlib.sha256(_canonical(
                {"op": "sum", "value": value, "unit": total.get("unit"),
                 "inputs": [e["citation"] for e in lines]}).encode("utf-8")).hexdigest()
            entries.extend([*lines, total])
        return entries

    def _preferred(self, run: _Run, oid: str, prefer: Mapping[str, Any]
                   ) -> tuple[list[str], tuple[str, str] | None]:
        """The ``prefer`` records of ``oid``: the objects of ``prefer.object_type`` its
        edges reach along ``prefer.via`` (link types, hop by hop, either direction), and
        why none can be used (a hop reaching several objects of a 1:1 link, or a reached
        record not read)."""
        frontier = [oid]
        for hop, link_type in enumerate(prefer["via"]):
            reached = sorted({e["to"] if e["from"] == node else e["from"]
                              for node in frontier for e in run.edges.values()
                              if e.get("link_type") == link_type and node in (e["from"], e["to"])
                              and e.get("tier") != "contradicted"})
            if hop == 0 and len(reached) > 1:
                return [], ("prefer_not_unique", f"{oid} has {len(reached)} {link_type} "
                            "records, so its own value is not one of theirs")
            frontier = reached
        details = [d for d in frontier if _type_of(d) == prefer["object_type"]]
        unread = [d for d in details if d not in run.read]
        if unread:
            return [], ("not_read", f"{len(unread)} {prefer['object_type']} of {oid} reached "
                        "but not read, so no total is stated")
        return details, None

    def _count(self, run: _Run, item: Mapping[str, Any]) -> list[dict[str, Any]]:
        if "from" in item:
            bindings = set(item.get("bindings") or ())
            objects = sorted(oid for oid in (run.collections.get(item["from"]) or _Collection()).ids
                             if item.get("object_type") in (None, _type_of(oid))
                             and (not bindings or _binding_of(oid) in bindings))
        else:
            objects = self._objects_of(run, item)
        subject = item.get("object_type") or item["from"]
        if not objects:
            proof = self._zero_proof(run, item) if item.get("zero") == "complete_search" \
                else None
            if proof is None:
                return self._missing(
                    run, item, item.get("if_no_object", "unknown"), "no_object_reached",
                    subject, f"the plan reached no {subject} this reader can see; that is not "
                    "a count of zero")
            return [{"name": item["name"], "kind": "count", "value": 0, "basis": [],
                     "check": "not_applicable", "object_type": item["object_type"],
                     "proof": proof}]
        if item.get("distinct") == "content":
            self._read_records(run, item["name"], objects)
            objects = self._distinct_content(run, objects)
        entry: dict[str, Any] = {"name": item["name"], "kind": "count", "value": len(objects),
                                 "basis": objects[:100], "check": "not_applicable"}
        if "object_type" in item:
            entry["object_type"] = item["object_type"]
        self._read_records(run, item["name"], objects)
        records = _records(run, objects)
        if records:
            entry["records"] = records
        return [entry]

    def _zero_proof(self, run: _Run, item: Mapping[str, Any]) -> dict[str, Any] | None:
        """The proof that no record of the count's bindings carries the root identifier
        (``zero: complete_search``), or None (the count stays a gap): the run is whole (not
        stopped or truncated); the root's identifier search answered complete and
        untruncated; each binding's relation
        holds a carrier of the searched identifier kind (of the searched type) over all
        its rows that hold it, no
        candidate of that search, at any tier, is a record of that relation, and the host
        proves the relation complete for this reader now (``complete``: every marking and
        the relation's grant held, so no record of it was withheld from the search; a run's
        ``withheld`` report concerns what the reader may not read elsewhere). A hidden, stale,
        partly captured or unsearched record, or one asked of the past, is never a
        confident zero."""
        from ontology import resolve

        if self.complete is None or run.search is None or run.stopped or run.truncated:
            return None
        query, found = run.search
        completeness = found.get("completeness")
        if "error" in found or not isinstance(completeness, Mapping) \
                or completeness.get("complete") is not True or completeness.get("truncated") \
                or completeness.get("next_cursor") is not None:
            return None
        candidates = [c for c in found.get("candidates") or () if isinstance(c, Mapping)]
        if completeness.get("total") != len(candidates):
            return None
        kinds = {query["identifier_kind"]} if query.get("identifier_kind") else {
            (c.get("matched") or {}).get("identifier_kind") for c in candidates}
        if not kinds or None in kinds:
            return None
        # A carrier binding that keeps only some of its relation's rows (beyond the path
        # being present) leaves the others unsearched.
        searched = {(c.source, c.relation) for c in resolve.carriers(
            self.schema, kinds, query.get("object_type"))
            if all(w.get("op") == "present" and w.get("path") == c.path for w in
                   (resolve.binding_spec(self.schema, c.binding) or ("", {}))[1].get(
                       "where", ()))}
        held: set[tuple[str, str]] = set()
        for cand in candidates:
            oid = str((cand.get("object") or {}).get("object_id") or "")
            spec = resolve.binding_spec(self.schema, _binding_of(oid)) if oid else None
            if spec is None:
                return None  # a candidate of no declared binding: its relation is unknown
            held.add((spec[1]["source"], spec[1]["relation"]))
        relations = []
        for binding in item["bindings"]:
            spec = resolve.binding_spec(self.schema, binding)
            if spec is None:
                return None
            where = (spec[1]["source"], spec[1]["relation"])
            if where not in searched or where in held:
                return None
            at = self.complete(binding, run.actor, run.as_known_at)
            if at is None:
                return None
            relations.append({"binding": binding, "relation": where[1], "complete_at": at})
        return {"kind": "complete_search", "identifier_kinds": sorted(kinds),
                "relations": relations}

    def _distinct_content(self, run: _Run, objects: Sequence[str]) -> list[str]:
        """One object per document content: objects whose record citation pins the same
        whole-document bytes (a mailed and a filed copy of one file) are one, kept as the
        copy evidence-tier links join to a root when one is, else the first by id. An
        object without such a citation (a member of a container, a page, a record) is its
        own: copies are never merged without their bytes."""
        from ontology.document_fields import _pinned

        leads = self._lead_only(run)
        kept: dict[tuple[str, str], str] = {}
        for oid in sorted(objects, key=lambda each: (each in leads, each)):
            pinned = _pinned((_citation_of(run, oid) or {}).get("l1_handle"))
            kept.setdefault(("content", pinned[2]) if pinned else ("object", oid), oid)
        return sorted(kept.values())

    def _read_records(self, run: _Run, name: str, objects: Sequence[str]) -> None:
        """Read (``plx.object`` as the reader, within the step budget) each counted object
        the run holds no citation of, for its record citation only: the read's values are
        not answer evidence, its ``withheld`` report joins the run's."""
        unread = [oid for oid in objects if _citation_of(run, oid) is None]
        if not unread or len(objects) > 100 or not self._available("plx.object"):
            return
        calls = 0
        for oid in unread:
            if run.spent():
                run.add_gap(_gap("unknown", "step_budget", name,
                                 f"{len(unread) - calls} counted record(s) were not read "
                                 "within the step budget, so the count lists no records"))
                break
            result = self._dispatch(run, "plx.object", {"object_id": oid}, harvest=False)
            calls += 1
            if isinstance(result.get("withheld"), Mapping):
                run.withheld = _merge_withheld(run.withheld, result["withheld"])
            owner = result.get("object") if "error" not in result else None
            cite = next((v["citation"] for v in result.get("values") or ()
                         if isinstance(v, Mapping) and isinstance(v.get("citation"), Mapping)),
                        None)
            if isinstance(owner, Mapping) and owner.get("object_id") == oid and cite:
                run.record_cites[oid] = cite
        entry = next((e for e in run.trace if e["step"] == RECORD_READS), None)
        if entry is None:
            entry = {"step": RECORD_READS, "tool": "plx.object", "status": "ok", "calls": 0,
                     "objects": 0}
            run.trace.append(entry)
        entry["calls"] += calls
        entry["objects"] += calls
        if calls < len(unread):
            entry["status"] = "budget_exhausted"

    @staticmethod
    def _list_cuts(run: _Run, item: Mapping[str, Any], result: Mapping[str, Any],
                   parts: Sequence[str]) -> None:
        """A list the path reads inside an entry (``lateness.lines.*``: each entry's
        ``lines``) that the primitive cut to fit says so on that entry (gap
        ``<list>_truncated``): the gap becomes the item's own and the run's answerability
        is partial, whatever the entry's verdict, so an answer stating the listed
        elements never reads as all of them."""
        for depth, key in enumerate(parts):
            if key == "*" or depth == 0:
                continue
            entries = _select_path(result, list(parts[:depth]))
            entries = [x for e in entries for x in (e if isinstance(e, list) else [e])]
            for entry in entries:
                own = entry.get("gaps") if isinstance(entry, Mapping) else None
                for gap in own if isinstance(own, list) else ():
                    if (isinstance(gap, Mapping) and gap.get("reason") == f"{key}_truncated"
                            and isinstance(gap.get("state"), str)
                            and isinstance(gap.get("subject"), str)):
                        detail = gap.get("detail")
                        run.add_gap(_gap(gap["state"], gap["reason"], gap["subject"],
                                         detail if isinstance(detail, str) else None,
                                         item["name"]))
                        run.reason(gap["reason"])   # a cut list: the answer is partial

    def _select(self, run: _Run, item: Mapping[str, Any],
                produced: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """A value read from the step's own results, with the cited values the selected
        elements compared (``cells``); with ``per``, one value per group of elements.
        ``all``/``any`` are three-valued: a null the primitive left undecided (or a
        truncated result) leaves the value null unless a decided element settles it; the
        executor never decides in its place."""
        sid, path = item["step"], item["path"]
        trace = next((t for t in run.trace if t["step"] == sid), None)
        if trace is not None and trace["status"] == "step_unavailable":
            return self._missing(run, item, "unknown", "step_unavailable", item["name"],
                                 f"step {sid} ({trace['tool']}) is not served by this host, "
                                 "so this value is not decided")
        elements: list[tuple[Any, list[Any]]] = []
        truncated = False
        for result in run.results.get(sid, ()):
            if "error" not in result:
                elements.extend(_select_elements(result, path.split("."), item.get("where"),
                                                 item.get("where_any")))
                truncated = truncated or bool((result.get("completeness") or {}).get("truncated"))
                self._list_cuts(run, item, result, path.split("."))
        per = item.get("per")
        if per is None:
            return self._reduced(run, item, item["name"], elements, truncated)
        groups: dict[str, list[tuple[Any, list[Any]]]] = {}
        loose = 0
        for element, picked in elements:
            key = element.get(per) if isinstance(element, Mapping) else None
            if isinstance(key, str) and key:
                groups.setdefault(key, []).append((element, picked))
            else:
                loose += 1
        if loose:
            run.add_gap(_gap("unknown", "not_grouped", item["name"],
                             f"{loose} element(s) at {path} name no {per}, so no "
                             f"{item['name']} entry states them"))
        if not groups:
            if item.get("omit_empty"):
                return []
            return self._missing(run, item, "unknown", "not_determined", item["name"],
                                 f"step {sid} gave no element with a {per} at {path}")
        numbered = produced.get(item.get("number_as", ""), ())
        numbers: dict[str, int] = {}
        for index, entry in enumerate(numbered, start=1):
            numbers.setdefault(str(entry.get("object_id")), index)
        rest = itertools.count(len(numbered) + 1)
        ordered = sorted(groups, key=lambda key: (key not in numbers, numbers.get(key, 0), key))
        out: list[dict[str, Any]] = []
        for key in ordered:
            number = numbers[key] if key in numbers else next(rest)
            out.extend(self._reduced(run, item, f"{item['name']}_{number}"[:64], groups[key],
                                     truncated))
        return out

    def _reduced(self, run: _Run, item: Mapping[str, Any], name: str,
                 elements: Sequence[tuple[Any, list[Any]]], truncated: bool
                 ) -> list[dict[str, Any]]:
        """The select entry ``name`` reduced from ``elements`` (element, picked values)."""
        sid, path = item["step"], item["path"]
        picked = [value for _, values in elements for value in values]
        reduce = item["reduce"]
        if item.get("omit_empty") and not [v for v in picked if v is not None]:
            return []   # an optional select: a group with no value is no entry and no gap
        if reduce in ("all", "any"):
            if not picked or not all(v is None or isinstance(v, bool) for v in picked):
                return self._missing(run, item, "unknown", "not_determined", name,
                                     f"step {sid} gave no list of verdicts at {path}", name)
            settles = reduce != "all"  # the decided value that settles the reduction
            if settles in picked:
                value: Any = settles
            elif None in picked or truncated:
                why = _element_gaps(elements, name) if item.get("required") else []
                missing = self._missing(run, item, "unknown", "not_decided", name,
                                        f"{sid} left {picked.count(None)} of {len(picked)} "
                                        f"verdict(s) at {path} undecided"
                                        + (" and its result was truncated" if truncated
                                           else "")
                                        + ("; why: the gaps that concern " + name if why
                                           else ""), name)
                for gap in why:
                    run.add_gap(gap)
                return missing
            else:
                value = not settles
        else:
            picked = [v for v in picked if v is not None]
            if not picked:
                return self._missing(run, item, "unknown", "not_determined", name,
                                     f"step {sid} gave no value at {path}", name)
            if reduce == "count":
                value = len(picked)
            else:
                value = picked[0] if isinstance(picked[0], (str, int, bool)) else None
        entry: dict[str, Any] = {"name": name, "kind": "select", "value": value,
                                 "check": "not_applicable"}
        cells = _cells([element for element, _ in elements], item.get("cells_only"))
        if cells and not any(validate(cell, _VALUE, root=self.contracts, limit=1)
                             for cell in cells):  # a primitive's malformed value: no cells
            entry["cells"] = cells
        return [entry]

    def _compute(self, run: _Run, item: Mapping[str, Any],
                 produced: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """The named engine over the entries produced so far; its gaps and reason codes join
        the run. The engine re-reads every citation it uses for the actor (``rows``)."""
        moment = run.as_known_at or self.clock().astimezone(dt.timezone.utc).isoformat()
        rows = self.rows

        def resolver(handle: str, actor: Actor) -> Mapping[str, Any] | None:
            return rows(handle, actor) if rows is not None else None
        roots = (run.collections.get("root") or _Collection()).ids

        def covers(link_type: str) -> bool:
            if self.scope is None or not roots:
                return False
            try:
                return all(self.scope(link_type, root, run.actor) is True for root in roots)
            except Exception:  # noqa: BLE001 - an undecided scope proves nothing
                return False

        def withheld(link_type: str) -> int | None:
            """Objects the link attaches to the roots that carry a marking the reader lacks."""
            counter = getattr(self.scope, "withheld", None)
            if counter is None or not roots:
                return None
            try:
                counts = [counter(link_type, root, run.actor) for root in roots]
            except Exception:  # noqa: BLE001 - an undecided count states nothing
                return None
            return None if None in counts else sum(counts)

        evidence = {e["name"]: e for e in run.plan.doc["evidence"]}

        def markings_lacked(name: str) -> list[str]:
            """The markings this reader lacks on every property evidence ``name`` reads
            (from the schema and its grant; empty when it may read one of them)."""
            spec = evidence.get(name)
            if spec is None or spec.get("kind") != "property":
                return []
            return self._markings_lacked(run, spec["object_type"], spec["properties"])

        def captured(relation: str) -> bool | None:
            if self.captured is None or not roots:
                return None
            try:
                says = [self.captured(relation, root, run.actor) for root in roots]
            except Exception:  # noqa: BLE001 - an unreadable claim proves nothing
                return None
            return None if None in says else all(says)

        def reviews(review_id: str, actor: Actor) -> Mapping[str, Any] | None:
            return (self.reviews(review_id, actor, run.as_known_at)
                    if self.reviews is not None else None)
        spec = self.engines[item["engine"]]
        corrected = {e["name"]: e["review"] for done in produced.values() for e in done
                     if (e.get("review") or {}).get("decision") == "correct_value"}
        reviewed = bool(spec.get("reviewed_inputs")) and bool(corrected)
        if reviewed:
            produced = {name: [_as_corrected(e) if e["name"] in corrected else e
                               for e in done] for name, done in produced.items()}
        try:
            engine = spec["run"]
            takes = inspect.signature(engine).parameters
            extra = {"captured": captured} if "captured" in takes or any(
                p.kind is p.VAR_KEYWORD for p in takes.values()) else {}
            if "withheld" in takes:   # by name only, as ``reviews``
                extra["withheld"] = withheld
            if "markings_lacked" in takes:   # by name only, as ``reviews``
                extra["markings_lacked"] = markings_lacked
            if "reviews" in takes:   # by name only: a wrapper forwarding **kw is not one
                extra["reviews"] = reviews
            if "assumptions" in takes:   # by name only, as ``reviews``
                extra["assumptions"] = self._assumptions(run, roots)
            if "landed_actuals" in takes and self.landed_actuals is not None:
                extra["landed_actuals"] = self._actuals_reader(run)
            if "people" in takes:   # by name only; None: the engine names nobody
                extra["people"] = self.people
            if "documents" in takes:   # by name only; None: the engine reads no text
                extra["documents"] = None if self.documents is None else (
                    lambda handle, _read=self.documents, _actor=run.actor: _read(handle, _actor))
            if "roots" in takes:   # by name only: the run's root object ids
                extra["roots"] = tuple(roots)
            if "objects" in takes:   # by name only: the reader's object references
                extra["objects"] = {k: dict(v) for k, v in run.objects.items()}
            if "cut_steps" in takes:   # by name only: which steps were cut (None: unknown)
                extra["cut_steps"] = (None if run.cut_steps is None or (
                    run.truncated is not None and not run.cut_steps) else frozenset(
                        run.cut_steps))
            out = engine(
                item["engine_args"], produced, list(run.edges.values()),
                items={e["name"]: e for e in run.plan.doc["evidence"]}, name=item["name"],
                as_of=moment[:10], resolver=resolver, actor=run.actor,
                complete=run.truncated is None and not run.stopped, covers=covers,
                **extra)
        except Exception as exc:  # noqa: BLE001 - a failing engine is a gap, not a crash
            out = {"entries": [], "reasons": ["engine_failed"],
                   "gaps": [_gap("unknown", "engine_failed", item["name"],
                                 f"{item['engine']} raised {type(exc).__name__}")]}
        for gap in out["gaps"]:
            run.add_gap(gap)
        for code in out["reasons"]:
            run.reason(code)
        # Property entries the engine found to name a person the people exclusion withholds
        # (or cannot clear): dropped from the answer (``_evidence``), never shown.
        run.withhold_entries.update(n for n in out.get("withhold") or () if isinstance(n, str)
                                    and any(e["name"] == n and e.get("kind") == "property"
                                            for done in produced.values() for e in done))
        self._engine_conflicts(run, out.get("conflicts") or (), produced)
        entries = list(out["entries"])
        computed = {e["name"]: e for e in [*(e for done in produced.values() for e in done
                                             if e.get("kind") == "computed"), *entries]}
        for entry in entries:
            used = sorted(_corrected_under(entry, corrected, computed))
            if used and reviewed:   # computed from the person's value: it cites the correction
                if entry.get("value") is not None:   # a null value computed from nothing
                    entry["reviews"] = [{"input": name, "review": dict(corrected[name])}
                                        for name in used][:50]
            elif used:   # an engine re-reading its citations reads the source value
                run.add_gap(_gap("unknown", "input_person_corrected", entry["name"],
                                 f"computed from {', '.join(used)}, which a person corrected; "
                                 "this engine re-reads the cited source and uses its value"))
        say_direction(entries)
        if item.get("required") and not any(e["name"] == item["name"] and e["value"] is not None
                                            for e in entries):
            run.reason(f"missing_{item['name']}")
            if not entries:
                entries = [{"name": item["name"], "kind": "computed", "value": None,
                            "inputs": [], "steps": [], "check": "not_applicable"}]
        return entries

    def _actuals_reader(self, run: _Run) -> Callable[[Sequence[str]], Mapping[str, Any]]:
        """The capture-built recorded charges (``duty_actual.ActualsFile``) for this reader:
        amounts are finance-marked money, so a reader who holds neither the finance marking
        nor ``ESTIMATES_MARKING``, or whose markings the host cannot say, gets ``withheld`` and
        no record; one holding ``ESTIMATES_MARKING`` alone gets ``actuals_withheld``, no record
        and only the company-wide (level ``all``) history estimates; a provider that fails
        reads as unavailable, never as no charge."""
        try:
            held = set(self.held_markings(run.actor)) if self.held_markings is not None \
                else set()
        except Exception:  # noqa: BLE001 - an undecided grant grants nothing
            held = set()
        provider = self.landed_actuals

        def read(keys: Sequence[str]) -> Mapping[str, Any]:
            if ACTUALS_MARKING not in held and ESTIMATES_MARKING not in held:
                return {"withheld": ACTUALS_MARKING}
            try:
                out = provider(list(keys))
            except Exception:  # noqa: BLE001 - an unreadable capture is no record, named
                return {"unavailable": "LANDED_ACTUALS_UNREADABLE"}
            if not isinstance(out, Mapping):
                return {"unavailable": "LANDED_ACTUALS_UNREADABLE"}
            if ACTUALS_MARKING in held:
                return out
            if out.get("unavailable"):   # a code, no money: named as for a finance reader
                return {"unavailable": out["unavailable"]}
            # A purchasing reader (purchasing_prices, no finance): the capture's labelled
            # company-wide estimates only. No per-PO record, no supplier or country rate
            # (derived from few orders' AP charges, it can restate one), no linked numbers.
            defaults = out.get("default_estimates")
            defaults = {part: rec for part, rec in defaults.items()
                        if isinstance(rec, Mapping) and rec.get("level") == "all"} \
                if isinstance(defaults, Mapping) else {}
            return {"actuals_withheld": ACTUALS_MARKING, "records": {}, "known": [],
                    "default_estimates": defaults, "digest": out.get("digest"),
                    "built_at": out.get("built_at")}
        return read

    def _assumptions(self, run: _Run, roots: Sequence[str]) -> list[Mapping[str, Any]]:
        """The person-recorded assumptions on the run's roots, for its reader; none (and a
        gap) when the provider fails, so an unreadable record never becomes a number."""
        if self.assumptions is None:
            return []
        try:
            return [dict(a) for root in roots for a in self.assumptions(root, run.actor) or ()
                    if isinstance(a, Mapping)][:20]
        except Exception:  # noqa: BLE001 - an unreadable assumption is none, named
            run.add_gap(_gap("unknown", "assumptions_unreadable", "recorded assumptions",
                             "the case's recorded assumptions could not be read; none is "
                             "applied"))
            return []

    @staticmethod
    def _engine_conflicts(run: _Run, conflicts: Sequence[Mapping[str, Any]],
                          produced: Mapping[str, list[dict[str, Any]]]) -> None:
        """An engine's conflicts with the cited values of the answer entries it names
        (``inputs``): only values this run read for its actor, never one it did not show."""
        by_name = {e["name"]: e for entries in produced.values() for e in entries}
        for conflict in conflicts:
            values = []
            for name in conflict.get("inputs") or ():
                entry = by_name.get(name) or {}
                value = next((v for v in run.values.get(entry.get("object_id") or "", ())
                              if v.get("property") == entry.get("property")
                              and v.get("citation") == entry.get("citation")), None)
                if value is not None and value not in values:
                    values.append(value)
            if len(values) < 2:
                continue  # nothing two-sided this reader was shown; the engine's gap stays
            found = {k: v for k, v in conflict.items() if k != "inputs"}
            found["values"] = values[:12]
            _add_conflict(run.conflicts, found)
