"""Generic ontology tools ``plx.aggregate`` and ``plx.compare``.

Both tools read the L2 projection store through the base tools' ``StoreReader``/``ReadPolicy``
(``operating/ontology_tools.py``) and the traverse tools' visibility rules and link walk
(``operating/ontology_tools_traverse.py``), and answer exactly the frozen contracts in
``operating/ontology_tool_contracts.json``: every output is validated with
``ontology/json_contract.py`` and bounded by ``limits.max_result_bytes`` before it
leaves. Nothing here names an object type, property, link type or case: types,
properties, cardinalities and authority come from the compiled schema and the store.

Permissions first. Set members, dimension objects and values are
trimmed to what the reader may read at ``as_known_at`` *before* anything is grouped,
summed or compared: the traverse SQL rules (audience granted; every version and value from T
on carries only held markings and passes the item gate) plus the base tools' visibility check. A
value that exists for a readable object but is withheld from this reader never
contributes: it is counted in ``withheld`` and the affected answer is incomplete.

``plx.aggregate`` (count / count_distinct / sum / min / max over a set handle):

* the measure is read on the set's own objects (its grain). ``group_by`` properties are
  read on the set's objects, or with ``path`` on the objects 1-2 declared hops away.
  A hop may only go to-one (dbt MetricFlow's join rule): a to-many hop would
  repeat a measure once per reached object and is refused (``fan_out``). An N:M hop is
  refused (``n_to_m_allocation``) unless it is a sum's one hop: a member reaching one
  record over it is grouped under it; a member reaching several is split only by a
  person's reviewed allocations (``plx.review`` ``allocate``, projected as ``reviewed``
  links whose ``person_allocated`` feature names the member as supply, ER-11), one per
  reached record for the measured property, in the member's own unit and together at
  most its value; each group then sums its share, citing the member's value and the
  allocation's evidence (gap ``reviewed_allocation_used``). Without one per reached
  record the member is not grouped (``allocation_review_required``), allocations that do
  not fit are ``allocation_conflict``: never an inferred split. A lead (a link below the
  floor) naming another record counts as reached for that decision unless a person
  rejected it (a person's rejection of a pair, a reviewed ``contradicted`` link the
  reader sees, also stops any inferred link on it, below ``reviewed``, from reaching
  there, on every hop), and one this reader may not see makes the member's group unknown
  (``group_link_withheld``), never the whole value one record's. A member reaching
  exactly one record over the hop is grouped under it whole, and the answer says so
  (``n_to_m_single_reach``: an attribution by reach, not a reviewed allocation). Links below
  ``stored_by_text_join`` (and links touching ``gold_native_staging`` rows, but a foreign
  key one source stores) are not followed, as plx.around's default floor;
* exact ``Decimal`` arithmetic. Roles, units and currencies are never mixed within a group:
  two quantity roles (planned vs filled vs inspected ...), two units, two currencies, an
  unknown unit or currency, or a quantity next to an unroled number is refused with the
  contract's code. A sum, min or max over a set of several object types is refused too
  (``role_mixing``, the closest contract code): two types are two grains, never one total.
  Unit tokens are compared after Portal's spelling fold (``ontology/units.py``, mirroring
  ``normalizeProductUom``: ``each``/``pcs`` = ``ea``, ``kgs`` = ``kg``; an unknown token
  such as ``GR`` stays itself), never converted. Currencies are never converted. Amounts
  on two tax bases (the schema's ``tax_basis`` per money source: ``incl_tax``,
  ``excl_tax``, ``tax_only``), or a known basis next to an ``unknown`` one, or ``unknown``
  bases read from two source columns, are refused like two quantity roles
  (``role_mixing``, naming the bases); amounts of one column's unknown basis combine,
  with a ``tax_basis_unknown`` gap saying so. A unit price (the schema's ``money_measure:
  unit_price``, money per one unit of a denominator) is a rate: a sum of unit prices is
  refused (``role_mixing``), and a min or max only combines prices per one known
  denominator (``unit_unknown`` / ``unit_mixing`` otherwise), shown as the group's unit.
  A plain ``decimal`` (a source number the schema types as neither money nor quantity)
  combines only within one recorded unit: one with no unit records no currency either,
  so its values may be amounts in two currencies or counts in two units (USD 10000 and
  CNY 10000 are no 20000, 2.50 per each and 180 per thousand no 182.5) and a sum, min,
  max or count_distinct over it is refused (``unit_unknown``);
* a member's value is the one its schema precedence ranks first (``authority_rank``);
  different values with no strict precedence are a conflict: the member is left out and
  the group is incomplete, never a silent pick. Where a person's correction of that value
  is in force and this reader may read it (``plx.review`` ``correct_value``, as
  ``plx.object`` shows it, ``ontology_tools_review.value_reviews``), the member's value is
  the person's, in their unit and currency (an amount corrected without a unit keeps the
  source's denominator): its group cites the value it corrects and the correction's cited
  fields beside it (gap ``person_corrected_value_used``); a ``group_by`` value a person
  corrected groups the member by the person's value, citing the correction the same way.
  A correction of a conflicted or withheld value resolves nothing;
* explicit incompleteness: a group known to miss an input (a member with no value in
  the source, a withheld value or an unresolved conflict; without ``group_by`` also a
  member left out because it is hidden, not in the projection or not groupable) has
  ``value: null`` and a typed gap. Members whose group cannot be known (an ambiguous or
  withheld group value) are left out of every group with a gap. A member whose path link
  exists but is hidden from this reader (the link's markings, its evidence gate or its
  target) is grouped under null; that group shows no value and the reason is withheld,
  never the hidden target. A set none of whose members this reader can count has no
  value (never a count of 0). Members one identity link joins (a link of an identity
  type, ``ontology_tools.identity_link_type``, at an identity tier: one record in two systems) make a
  count or sum of their group unknown with a ``cross_system_twins`` gap, never a double
  count. Either way
  ``partial`` is true and the answer is not complete: a partial number is never shown as
  a complete total. More than 200 groups, or more than one response holds, is
  ``truncated``;
* time: members, values and absence are as known at ``as_known_at`` from
  the versioned captures. A member first captured after T is not counted (a
  ``not_ingested`` gap ``captured_after_as_known_at`` says how many: nothing was known of
  it then, which is not evidence it did not exist); one with a version live at T is;
* a member its source asserts it did not hold at T (one removal test,
  ``StoreReader.removals``) is no member then: it is left out of every count, sum, min,
  max and group, and its gap counts the removed members by basis, ``absent_in_source``.
  Soft-deleted by the source's own column: ``deleted_in_source``. No longer returned by
  the source's traversal: ``missing_from_source``, asserted only when its source is a
  current capture of a complete traversal (the case drivers' one absence rule,
  ``operating/absence_claims.traversal_problem``) and, at a caller-chosen T, that capture
  was taken by T; else the member stays counted with an ``unknown`` gap saying why. The
  store keeps only the latest traversal's completeness, so a traversal a later capture
  replaced cannot show absence at T. Before its removal time a member counts. A removal
  time the store cannot parse is a removal of unknown time, in force at every T (fail
  closed). plx.compare's object sides take the same removal test and gap states (an
  object first captured after its side's T is ``not_known_at``, never
  ``object_not_found``);
* ``meta.freshness`` holds at most the contract's 8 sources; an answer fed by more
  states the total in a ``freshness_truncated`` gap, never drops them silently;
* every group cites the value rows it was computed from (``inputs_citations``, at most 50;
  more is stated as a ``citations_capped`` gap) and carries the union of the markings of
  its inputs: those value rows, its members' versions and, along a path, the links
  followed, the dimension objects' versions and the group-key rows.

``plx.compare`` (two objects, two versions via ``*_as_known_at``, or an object and a cited
document value set; a citation side is read at that record's own revision unless its
``*_as_known_at`` says otherwise, and a cited value not live at the time read is
``incomparable``, never ``missing``): one row per property with ``equal`` / ``different`` /
``incomparable`` / ``missing_left`` / ``missing_right`` and a reason. Numbers compare as
``Decimal``; quantities only within one role and one (folded) unit, money only within one
known currency and one tax basis (``tax_basis_differs`` / ``tax_basis_unknown``), unit
prices only per one known denominator after the unit fold (``price_denominator_unknown`` /
``price_denominator_differs``; a unit price never equals an amount,
``money_measure_differs``), plain decimals only within one recorded unit (``unit_unknown``
when either side records none, ``unit_differs``); text
after whitespace and case folding (reason ``equal_after_normalizing``). A side with disagreeing values and no strict precedence is
``incomparable`` (``side_values_conflict``). It never resolves a difference or picks a
winner between the two sides. A person's review in force of a side's value (as of that
side's time) is shown beside it as ``plx.object`` shows it (``review``; a confirmation
marks it ``human_reviewed``); the comparison stays between what the sources state.

Evidence label of the tests: isolated-fixture (tests/test_ontology_tools_aggregate.py).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import json
import re
import secrets
import sqlite3
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from ontology.json_contract import validate
from ontology.reviews import ALLOCATED
from ontology.store import withhold_currency
from ontology.types import Actor
from ontology.units import is_unit_price, same_denominator, unit_token, value_basis
from operating.absence_claims import ABSENT, traversal_problem
from operating.claim_checks import _plain, to_decimal
from operating.ontology_tools import (
    LIVE_LOOKUP_HINT,
    OntologyTools,
    REMOVAL_REASONS,
    ToolError,
    _HANDLE,
    _IDENTITY_TIERS,
    _compare_key,
    _envelope,
    _envelope_bytes,
    _error,
    _gap,
    _inline_defs,
    _markings,
    _source_of,
    _value_out,
    unit_out,
    units_text,
)
from operating.ontology_tools_traverse import (
    CAPTURED_AFTER,
    DEFAULT_TIER_FLOOR,
    PERMISSION_LOSSES,
    TraverseTools,
    _effective_tier,
    _from_t,
    _live,
    _parse_time,
    _TIER_RANK,
    _visible_object,
    _visible_value,
)
from operating.quantity_types import QUANTITY_TYPES

TOOL_NAMES = ("plx.aggregate", "plx.compare")

MAX_GROUPS = 200
MAX_CITATIONS = 50
MAX_GAPS = 20
MAX_ROWS = 200
NUMERIC = frozenset({"integer", "decimal", "money", "quantity"})

# --------------------------------------------------------------------------- value picking

class _Pick:
    """The value of one property of one object for this reader: ``row`` when one
    value stands (single, all agreeing, or ranked first by the schema's precedence),
    else ``status`` says why not (``no_value``, ``source_null``, ``conflict``,
    ``withheld``)."""

    __slots__ = ("row", "status", "others_differ")

    def __init__(self, row: Mapping[str, Any] | None, status: str,
                 others_differ: bool = False) -> None:
        self.row, self.status, self.others_differ = row, status, others_differ


def _value_key(row: Mapping[str, Any]) -> tuple:
    return (_compare_key(row["value_type"], row["value"]), unit_token(row["unit"]),
            row["currency"], row["quantity_role"])


def _pick(rows: Sequence[Mapping[str, Any]], withheld: bool) -> _Pick:
    present = [r for r in rows if r["value"] is not None]
    if not present:
        if withheld:
            return _Pick(None, "withheld")
        return _Pick(None, "source_null" if rows else "no_value")
    keys = {_value_key(r) for r in present}
    if len(keys) == 1:
        # Agreeing readable values; a withheld one may still disagree, so it stays
        # incomplete for this reader.
        return _Pick(present[0], "withheld" if withheld else "ok")
    first = present[0]
    rank = first["authority_rank"]
    rest = [r["authority_rank"] for r in present[1:] if _value_key(r) != _value_key(first)]
    if rank is not None and all(other is None or other > rank for other in rest):
        return _Pick(first, "withheld" if withheld else "ok", others_differ=True)
    return _Pick(None, "conflict")


def _group_display(row: Mapping[str, Any], limit: int) -> Any:
    """A group key value: integers and booleans as they are, any text an envelope."""
    value_type, raw = row["value_type"], row["value"]
    if raw is None:
        return None
    if value_type == "integer" and re.fullmatch(r"-?[0-9]{1,18}", raw):
        return int(raw)
    if value_type == "boolean" and raw in ("true", "false"):
        return raw == "true"
    return _envelope(raw, row["l1_handle"], row["field_path"], limit)


def _corrected(row: Mapping[str, Any], correction: Mapping[str, Any]) -> dict[str, Any]:
    """A value row read as a person corrected it: their value, unit and currency (a money
    correction states its currency and may leave out the unit: a unit price then stays per
    the source's denominator), the source's citation kept."""
    unit = correction.get("unit")
    if unit is None and row["value_type"] == "money":
        unit = row["unit"]
    return dict(row, value=correction["value"], unit=unit, currency=correction.get("currency"))


def _cite(row: Mapping[str, Any]) -> dict[str, Any] | None:
    value, _ = _value_out(row, "X", 1)
    return None if value is None else value["citation"]


class _Gaps:
    """Gaps folded by (state, reason, subject) with counts, so one answer stays within
    the contract's 20 gaps however many groups or members a reason touches."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], list] = {}

    def add(self, state: str, reason: str, subject: str, detail: str, count: int = 1) -> None:
        key = (state, reason, subject[:700])
        if key in self._items:
            self._items[key][1] += count
        else:
            self._items[key] = [detail, count]

    def extend(self, gaps: Iterable[Mapping[str, Any] | None]) -> None:
        for gap in gaps:
            if gap:
                self.add(gap["state"], gap["reason"], gap["subject"], gap.get("detail", ""), 0)

    def out(self) -> list[dict[str, Any]]:
        result = []
        for (state, reason, subject), (detail, count) in self._items.items():
            text = f"{count} affected: {detail}" if count else detail
            result.append(_gap(state, reason, subject, text or None))
        return result[:MAX_GAPS]

    def __bool__(self) -> bool:
        return bool(self._items)


# --------------------------------------------------------------------------- tools

class AggregateTools:
    """``plx.aggregate`` and ``plx.compare`` over one projection store.

    ``base``: the host's base ``OntologyTools`` (store, policy, item gate, schema,
    contracts, clock, set registry); ``walk``: the ``TraverseTools`` over the same
    base (built when not given), whose visibility SQL and link walk are reused so the
    tools apply one permission rule.
    """

    def __init__(self, base: OntologyTools, walk: TraverseTools | None = None) -> None:
        self.base = base
        self.walk = walk if walk is not None else TraverseTools(base)
        self.store = base.store
        self.contracts = base.contracts
        self.limits = self.contracts["limits"]

    @property
    def schema(self):  # noqa: ANN201 - base SchemaView or None
        return self.base.schema

    # ------------------------------------------------------------------ entry

    def call(self, name: str, arguments: Any, actor: Actor) -> dict[str, Any]:
        """Run one tool for ``actor``; the result always satisfies the contract."""
        if name not in TOOL_NAMES:
            raise KeyError(name)
        contract = self.contracts["tools"][name]
        if not isinstance(arguments, dict):
            return self._checked(name, _error("invalid_input", "arguments must be an object"))
        problems = validate(arguments, contract["input"], root=self.contracts, limit=3)
        if problems:
            return self._checked(name, _error("invalid_input", "; ".join(problems)))
        handler = {"plx.aggregate": self._aggregate, "plx.compare": self._compare}[name]
        gate_name = "plx_l9_gate_" + secrets.token_hex(8)
        with self.store.lock:  # one call at a time on the shared connection
            self.store.conn.create_function(gate_name, 1, self.walk._gate_function(actor),
                                            deterministic=True)
            try:
                result = handler(arguments, actor, gate_name)
            except ToolError as exc:
                result = _error(exc.code, exc.message, exc.hint)
                try:
                    result["meta"] = self.base._meta(self.base._store_meta(),
                                                     self.base.clock(), ())
                except ToolError:
                    pass
            except sqlite3.Error as exc:
                result = _error("store_unavailable",
                                f"the projection store could not be read "
                                f"({type(exc).__name__})", "rebuild or reopen the projection store")
            finally:
                self.store.conn.create_function(gate_name, 1, None)
        return self._checked(name, result)

    def _checked(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        problems = validate(result, self.contracts["tools"][name]["output"],
                            root=self.contracts, limit=5)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        if _envelope_bytes(result) > self.limits["max_result_bytes"]:
            return _error("result_too_large", "the answer does not fit one response",
                          "narrow the set, group by fewer properties or compare fewer properties")
        return result

    # ------------------------------------------------------------------ shared reads

    def _declared(self, object_type: str) -> dict[str, Mapping[str, Any]]:
        spec = (self.schema.object_types.get(object_type) if self.schema else None) or {}
        return {p["name"]: p for p in spec.get("properties", ())}

    def _basis(self, row: Mapping[str, Any]) -> str:
        """The tax basis of one stored money value (stored with it, else the schema's for
        its binding and lineage)."""
        prop = self._declared(row["object_id"].split(":", 1)[0]).get(row["property"])
        return value_basis(prop, row)

    def _rate(self, row: Mapping[str, Any]) -> bool:
        """True when a stored money value is a unit price (the schema's money_measure)."""
        return is_unit_price(self._declared(row["object_id"].split(":", 1)[0])
                             .get(row["property"]))

    def _require_declared(self, types: Iterable[str], names: Iterable[str], what: str) -> None:
        if self.schema is None:
            raise ToolError("schema_unavailable", "the compiled ontology schema is not loaded, "
                            "so properties cannot be checked",
                            "install the schema loader and domain files")
        for object_type in sorted(set(types)):
            declared = self._declared(object_type)
            missing = sorted(n for n in names if n not in declared)
            if missing:
                raise ToolError("unknown_property",
                                f"{object_type} declares no {what} property "
                                f"{', '.join(missing)}",
                                "call plx.schema with object_type; aggregate a set of one "
                                "object type that declares it")

    def _require_declared_by_any(self, types: Iterable[str], names: Iterable[str]) -> None:
        """Every name is declared by at least one of ``types`` (the other side may simply
        lack it: ``missing_*``), else ``unknown_property``."""
        if self.schema is None:
            raise ToolError("schema_unavailable", "the compiled ontology schema is not loaded, "
                            "so properties cannot be checked",
                            "install the schema loader and domain files")
        declared = {n for object_type in types for n in self._declared(object_type)}
        missing = sorted(n for n in names if n not in declared)
        if missing:
            raise ToolError("unknown_property",
                            f"{' / '.join(sorted(types))} declares no property "
                            f"{', '.join(missing)}", "call plx.schema with object_type")

    def _values(self, scope: Any, ids: Sequence[str], properties: Sequence[str],
                handle: str | None = None
                ) -> tuple[dict[tuple[str, str], list[sqlite3.Row]], dict[tuple[str, str], str]]:
        """(visible value rows per (object, property), authority order; withheld
        (object, property) -> 'marking' | 'audience' | 'marking_and_audience') for
        already visible objects; ``handle`` keeps only values read from that L1 record."""
        visible: dict[tuple[str, str], list[sqlite3.Row]] = {}
        withheld: dict[tuple[str, str], str] = {}
        if not ids or not properties:
            return visible, withheld
        params = scope.params(ids=json.dumps(sorted(set(ids))),
                              props=json.dumps(sorted(set(properties))), h=handle)
        only = "AND (:h IS NULL OR v.l1_handle = :h) "
        seen: set[tuple[str, str, str, str]] = set()
        # A currency read on another record (its company's local currency) only where this
        # reader may read that record (ontology.store.withhold_currency).
        for row in withhold_currency(self.store.conn.execute(
                "SELECT v.* FROM object_values v WHERE v.object_id IN "
                "(SELECT value FROM json_each(:ids)) AND v.property IN "
                f"(SELECT value FROM json_each(:props)) AND {_visible_value('v', scope.gate)} "
                + only + "ORDER BY v.object_id, v.property, v.authority_rank IS NULL, v.authority_rank, "
                "v.l1_handle, v.field_path", params), lambda sources: (
                    found for found, in self.store.conn.execute(
                        "SELECT o.object_id FROM objects o WHERE o.object_id IN (SELECT value "
                        f"FROM json_each(:ids)) AND {_visible_object('o', scope.gate)}",
                        scope.params(ids=json.dumps(list(sources)))))):
            visible.setdefault((row["object_id"], row["property"]), []).append(row)
            seen.add((row["object_id"], row["property"], row["l1_handle"], row["field_path"]))
        held_sets = set(scope.marking_sets)
        for row in self.store.conn.execute(
                "SELECT v.object_id, v.property, v.l1_handle, v.field_path, v.markings, "
                f"{scope.gate}(v.l1_handle) AS gated FROM object_values v WHERE v.object_id IN (SELECT value FROM json_each(:ids)) "
                f"AND v.property IN (SELECT value FROM json_each(:props)) AND {_live('v')} "
                + only, params):
            key = (row["object_id"], row["property"], row["l1_handle"], row["field_path"])
            if key in seen:
                continue
            # The item gate failing is the audience; anything else (this row's marking, or a
            # row of the same property from T on that carries one) is a marking.
            reason = ("audience" if row["markings"] in held_sets else "marking_and_audience"
                      ) if not row["gated"] else "marking"
            pair = (row["object_id"], row["property"])
            withheld[pair] = ("marking_and_audience"
                              if withheld.get(pair, reason) != reason else reason)
        return visible, withheld

    def _corrections(self, actor: Actor, at: dt.datetime, ids: Sequence[str],
                     props: Sequence[str]) -> dict[tuple[str, str], Mapping[str, Any]]:
        """(object, property) -> the person's correction of that value in force at ``at``
        that this reader may read (``ontology_tools_review.value_reviews``, as
        ``plx.object`` shows it), with ``citations`` (its cited fields as value citations)
        and ``marks`` (the markings of the versions it cites, inputs of the group); for the
        objects any value review names and ``props`` (a measure or group_by properties)."""
        reviews = self.base.reviews
        if reviews is None or not props or not ids:
            return {}
        from .ontology_tools_review import correction_inputs, value_reviews
        scope = self.base._scope(actor, at)
        out: dict[tuple[str, str], Mapping[str, Any]] = {}
        for oid in sorted(reviews.value_subjects(actor.tenant, ids)):
            for prop, found in value_reviews(self.base, reviews, scope, oid, props).items():
                if found["decision"] == "correct_value":
                    citations, marks = correction_inputs(self.base, found)
                    out[(oid, prop)] = dict(found, citations=citations, marks=marks)
        return out

    def _removed(self, scope: Any, ids: Sequence[str]) -> tuple[dict[str, str], str]:
        """(those of ``ids`` their source did not hold at T, id -> removal basis, from the
        one removal test ``StoreReader.removals``; the gap state of the missing ones).
        A soft-deleted one is the source's own assertion. A missing one is
        ``absent_in_source`` only when every source it comes from is a current capture of
        a complete traversal (``absence_claims.traversal_problem``, the drivers' rule)
        and, at a caller-chosen T, every such capture was taken by T; else ``unknown``,
        the second item then being the detail."""
        removed = {oid: basis for oid, (basis, _since) in
                   self.store.removals(ids, scope.t).items()}
        gone = {oid for oid, basis in removed.items() if basis != "source_soft_delete"}
        if not gone:
            return removed, ABSENT
        sources = self._sources(scope, (), gone)
        freshness = self.base._freshness(self.base._store_meta(), sources) if sources else []
        problem = traversal_problem(freshness)
        if problem is None and scope.explicit_at and any(
                (_parse_time(item["captured_at"]) or dt.datetime.max.replace(
                    tzinfo=dt.timezone.utc)) > scope.at for item in freshness):
            problem = ("a capture after as_known_at replaced the traversal known then, and "
                       "the store does not keep whether that traversal was complete, so a "
                       "record it did not return may still have existed")
        return removed, ABSENT if problem is None else problem

    def _twins(self, scope: Any, ids: Sequence[str]) -> list[set[str]]:
        """Clusters of ``ids`` joined by identity links this reader sees (a link of an
        identity type, ``ontology_tools.identity_link_type``, at an identity tier, live at T): one
        record held in two systems, never two records to add up. A ``same_record`` key
        between two types joins two records."""
        if len(ids) < 2:
            return []
        gate = scope.gate
        parent: dict[str, str] = {}

        def find(oid: str) -> str:
            while parent.get(oid, oid) != oid:
                oid = parent[oid]
            return oid
        for row in self.store.conn.execute(
                f"""SELECT l.from_object, l.to_object, l.tier,
                      (SELECT v.lineage FROM object_versions v WHERE v.object_id = l.from_object
                         AND {_live('v')}) AS from_lineage,
                      (SELECT v.lineage FROM object_versions v WHERE v.object_id = l.to_object
                         AND {_live('v')}) AS to_lineage
                    FROM links l WHERE l.link_type IN (SELECT value FROM json_each(:itypes))
                    AND l.from_object IN (SELECT value FROM json_each(:ids))
                    AND l.to_object IN (SELECT value FROM json_each(:ids))
                    AND {_live('l')} AND l.markings IN (SELECT value FROM json_each(:marks))
                    AND NOT EXISTS (SELECT 1 FROM json_each(l.evidence_handles) h
                                    WHERE NOT {gate}(h.value))""",
                scope.params(ids=json.dumps(sorted(set(ids))), itypes=json.dumps(sorted(
                    self.schema.identity_link_types() if self.schema else ())))):
            if _effective_tier(row) in _IDENTITY_TIERS and row[0] != row[1]:
                parent[find(row[0])] = find(row[1])
        clusters: dict[str, set[str]] = {}
        for oid in set(parent) | set(parent.values()):
            clusters.setdefault(find(oid), set()).add(oid)
        return [c for c in clusters.values() if len(c) > 1]

    def _meta(self, scope: Any, sources: Iterable[str | None]) -> tuple[dict, list[dict]]:
        """The walk's meta and freshness gaps; sources beyond what ``meta.freshness``
        can list (the contract's 8) are stated with their total, never dropped
        silently (``TraverseTools._meta``)."""
        return self.walk._meta(scope, sources)

    def _sources(self, scope: Any, rows: Iterable[Mapping[str, Any]],
                 ids: Iterable[str]) -> set[str]:
        out = {_source_of(r["l1_handle"], r["lineage"]) for r in rows}
        for version in self.walk._versions_at(scope, sorted(set(ids))).values():
            out.add(_source_of(version["l1_handle"], version["lineage"]))
        return {s for s in out if s}

    # ------------------------------------------------------------------ plx.aggregate

    def _hop_cardinality(self, path: Sequence[Mapping[str, str]], op: str
                         ) -> dict[str, str] | None:
        """A ``refused`` object when a hop goes to-many, or N:M other than a sum's one
        hop (split by reviewed allocations), else None."""
        for step in path:
            link = self.schema.link_types.get(step["link_type"]) if self.schema else None
            if link is None:
                continue  # _validate_path names the undeclared link
            card = link["cardinality"]
            forward = step["direction"] == "forward"
            if card == "N:M" and (op != "sum" or len(path) != 1):
                return {"code": "n_to_m_allocation",
                        "message": f"{step['link_type']} is many-to-many: a measure is spread "
                                   "over it only as a sum over that one hop, split by "
                                   "reviewed allocations, and none is inferred",
                        "hint": "sum over that hop alone, aggregate each side separately, or "
                                "record allocations with plx.review (decision allocate)"}
            to_many = (card == "1:N") if forward else (card == "N:1")
            if to_many:
                return {"code": "fan_out",
                        "message": f"{step['link_type']} {step['direction']} reaches several "
                                   "objects per measured object, so the measure would be "
                                   "counted more than once",
                        "hint": "aggregate at the set's grain, or move the set along the hop "
                                "with plx.around and aggregate the reached objects"}
        return None

    def _dimensions(self, scope: Any, roots: Sequence[str], hops: Sequence[tuple],
                    gaps: _Gaps, split: bool = False) -> tuple[dict[str, str | None | bool], dict[str, set[str]],
                                          dict[str, str], dict[str, list[str]],
                                          dict[str, dict[str, dict[str, Mapping[str, Any]]]]]:
        """(root -> the one dimension object it reaches along the to-one path, None when
        it reaches nothing visible at or above the tier floor, False when it reaches
        several (an ambiguous to-one link, or an N:M hop: the member cannot be grouped
        without allocations); root -> the markings of the links it followed; root -> the
        withheld reason when a link on its path exists at T but is hidden from this
        reader; root -> the objects it reaches when several; root -> property -> reached
        object -> the reviewed allocation a link it followed gives that object of the
        root's property). ``split`` (one N:M hop of a sum): a record a link below the floor
        names (a lead mention) counts as reached when deciding whether a member reaches
        several, unless a person rejected that link, so a lead never lets a member's whole
        value fall to one record."""
        floor = _TIER_RANK[DEFAULT_TIER_FLOOR]
        current: dict[str, set[str]] = {oid: {oid} for oid in roots}
        link_marks: dict[str, set[str]] = {oid: set() for oid in roots}
        hidden: dict[str, str] = {}
        offered: dict[str, dict[str, dict[str, Mapping[str, Any]]]] = {}
        named: dict[str, set[str]] = {}       # split: records only below the floor names
        rejected: dict[str, set[str]] = {}    # split: records a person's review rejected
        for link_type, direction, _source, _target in hops:
            frontier = sorted({o for objs in current.values() for o in objs})
            reached: dict[str, dict[str, set[str]]] = {}
            below: set[str] = set()
            withheld_from: dict[str, str] = {}
            if frontier:
                seen: set[str] = set()
                firm: set[tuple[str, str]] = set()      # pairs a verified or reviewed link holds
                refused: set[tuple[str, str]] = set()   # pairs a person's rejection names
                for row in self.walk._hop_edges(scope, link_type, direction, frontier):
                    seen.add(row["link_id"])
                    near, far = ((row["from_object"], row["to_object"]) if direction == "forward"
                                 else (row["to_object"], row["from_object"]))
                    tier = _effective_tier(row)
                    if _TIER_RANK[tier] <= floor:
                        reached.setdefault(near, {}).setdefault(far, set()).update(
                            _markings(row["markings"]))
                        if _TIER_RANK[tier] <= _TIER_RANK["reviewed"]:
                            firm.add((near, far))
                        for share in _allocations(row, near):
                            offered.setdefault(near, {}).setdefault(
                                share["property"], {})[far] = share
                    else:
                        below.add(near)
                        if tier == "contradicted" and row["binding_kind"] == "reviewed":
                            refused.add((near, far))
                        if split:
                            (rejected if tier == "contradicted" else named
                             ).setdefault(near, set()).add(far)
                # A person's rejection of a pair: an inferred link on it is not followed.
                for near, far in refused - firm:
                    reached.get(near, {}).pop(far, None)
                known = None
                if split:   # what the member's visible links already name, or a person rejected
                    known = {near: set(reached.get(near, {})) | named.get(near, set())
                             | rejected.get(near, set()) for near in frontier}
                withheld_from = self._hidden_links(scope, link_type, direction, frontier, seen,
                                                   known)
            for root, objs in current.items():
                for reason in (withheld_from[o] for o in objs if o in withheld_from):
                    hidden[root] = _merge_reason(hidden.get(root), reason)
                nxt: set[str] = set()
                for near in objs:
                    for far, marks in reached.get(near, {}).items():
                        nxt.add(far)
                        link_marks[root].update(marks)
                current[root] = nxt
            weak = sum(1 for objs in current.values() if not objs) and below
            if weak:
                gaps.add("unknown", "below_tier_floor", f"{link_type} {direction}",
                         f"some members are linked only below tier {DEFAULT_TIER_FLOOR}; "
                         "they are not grouped by that link", 0)
        out: dict[str, str | None | bool] = {}
        for root, objs in current.items():
            out[root] = None if not objs else (next(iter(objs)) if len(objs) == 1 else False)
        several = {root: sorted(objs) for root, objs in current.items() if len(objs) > 1}
        for root, objs in current.items():
            candidates = objs | (named.get(root, set()) - rejected.get(root, set()))
            if len(candidates) > 1:
                out[root], several[root] = False, sorted(candidates)
        return out, link_marks, hidden, several, offered

    def _shares(self, demands: Sequence[str], offered: Mapping[str, Mapping[str, Any]],
                choice: _Pick, gaps: _Gaps, subject: str
                ) -> dict[str, Mapping[str, Any]] | None:
        """demand -> its reviewed allocation, for a member reaching several records over
        an N:M hop (or one a person allocated part of), when a person's allocation names
        each of them and together they fit
        the member's own value (one unit after the fold, at most the value; a value this
        reader cannot use leaves each group without a value, as any member's would);
        else None and the gap that says why. Never a split the reviews do not state."""
        if any(demand not in offered for demand in demands):
            gaps.add("unknown", "allocation_review_required", subject,
                     "a member reaches several records over a many-to-many link and no "
                     "reviewed allocation (plx.review allocate) gives each its share; it was "
                     "not grouped, never split by inference")
            return None
        if choice.row is not None:
            total = to_decimal(choice.row["value"])
            parts = [to_decimal(offered[demand]["quantity"]) for demand in demands]
            unit = unit_token(choice.row["unit"])
            if total is None or None in parts or unit is None or any(
                    unit_token(offered[demand]["unit"]) != unit for demand in demands
            ) or sum(parts, Decimal(0)) > total:
                gaps.add("unknown", "allocation_conflict", subject,
                         "reviewed allocations do not fit the member's value (another or no "
                         "unit, or more than the value together); it was not grouped")
                return None
            if sum(parts, Decimal(0)) < total:
                gaps.add("unknown", "allocation_remainder_unallocated", subject,
                         "reviewed allocations give out less than a member's value; the "
                         "remainder is in no group")
        gaps.add("unknown", "reviewed_allocation_used", subject,
                 "a member's value is split over the records it reaches by a person's "
                 "reviewed allocations; each group sums its share (plx.review shows them)")
        return {demand: offered[demand] for demand in demands}

    def _hidden_links(self, scope: Any, link_type: str, direction: str,
                      frontier: Sequence[str], seen: set[str],
                      known: Mapping[str, set[str]] | None = None) -> dict[str, str]:
        """near object -> withheld reason, for links of ``link_type`` live at T at or
        above the tier floor (the links the walk follows) from ``frontier`` to an object
        live at T that ``_hop_edges`` did not return for this reader: a marking it lacks on the link or the target (``marking``), or the
        target's audience or an item gate it fails (``audience``). ``known`` (the N:M hop
        of a sum: near -> the records its visible links name or a person rejected): a
        hidden link below the floor counts too unless it is contradicted or names one of
        those, as a lead the reader saw would make the member need allocations. Only the
        reason leaves here; never the link or its target."""
        anchor, other = (("from_object", "to_object") if direction == "forward"
                         else ("to_object", "from_object"))
        gate = scope.gate
        floor = _TIER_RANK[DEFAULT_TIER_FLOOR]
        out: dict[str, str] = {}
        for row in self.store.conn.execute(
                f"""SELECT l.link_id, l.tier, l.binding_kind, l.evidence_handles,
                    l.{anchor} AS near, l.{other} AS far,
                    (SELECT v.lineage FROM object_versions v WHERE v.object_id = l.from_object
                       AND {_live('v')}) AS from_lineage,
                    (SELECT v.lineage FROM object_versions v WHERE v.object_id = l.to_object
                       AND {_live('v')}) AS to_lineage, CASE
                      WHEN l.markings NOT IN (SELECT value FROM json_each(:marks)) THEN 'marking'
                      WHEN x.audience_key NOT IN (SELECT value FROM json_each(:aud))
                        OR EXISTS (SELECT 1 FROM json_each(l.evidence_handles) h
                                   WHERE NOT {gate}(h.value))
                        OR EXISTS (SELECT 1 FROM object_versions g WHERE g.object_id = x.object_id
                                   AND {_from_t('g')} AND NOT {gate}(g.l1_handle))
                        THEN 'audience'
                      ELSE 'marking' END AS reason
                    FROM links l JOIN objects x ON x.object_id = l.{other}
                    WHERE l.link_type = :lt
                    AND l.{anchor} IN (SELECT value FROM json_each(:frontier)) AND {_live('l')}
                    AND EXISTS (SELECT 1 FROM object_versions v WHERE v.object_id = x.object_id
                                AND {_live('v')})""",
                scope.params(lt=link_type, frontier=json.dumps(list(frontier)))):
            if row["link_id"] in seen:
                continue
            tier = _effective_tier(row)
            if _TIER_RANK[tier] <= floor or (
                    known is not None and tier != "contradicted"
                    and row["far"] not in known.get(row["near"], ())):
                out[row["near"]] = _merge_reason(out.get(row["near"]), row["reason"])
        return out

    def _aggregate(self, arguments: Mapping[str, Any], actor: Actor, gate: str
                   ) -> dict[str, Any]:
        scope = self.walk._scope(actor, arguments, gate)
        measure = arguments["measure"]
        op, prop = measure["op"], measure.get("property")
        group_by = list(arguments.get("group_by", ()))
        path = list(arguments.get("path", ()))
        if op != "count" and prop is None:
            raise ToolError("invalid_input", f"measure op {op} needs a property")
        if path and not group_by:
            raise ToolError("invalid_input", "path leads to the objects whose properties "
                            "group the measure; name them in group_by")
        roots, root_gaps = self.walk._root(scope, arguments["set"])
        gaps = _Gaps()
        gaps.extend(root_gaps)
        # A member its source asserts removed at T is no member then (left out of every
        # group, counted by basis); one whose absence is not established stays counted.
        removed, unshown = self._removed(scope, roots)
        asserted = {oid: basis for oid, basis in removed.items()
                    if basis == "source_soft_delete" or unshown == ABSENT}
        for basis, detail in (("source_soft_delete", "the source marks these members "
                               "deleted (its own soft-delete column); they are left out of "
                               "the answer"),
                              ("missing_from_source", "the source's complete latest "
                               "traversal no longer returns these members (key-only scan); "
                               "they are left out of the answer")):
            count = sum(1 for found in asserted.values() if found == basis)
            if count:
                gaps.add(ABSENT, REMOVAL_REASONS[basis], prop or "members", detail, count)
        if len(removed) > len(asserted):
            gaps.add("unknown", "missing_from_source", prop or "members",
                     "the source's latest traversal did not return some members, but "
                     f"{unshown}; they are still counted as known",
                     len(removed) - len(asserted))
        # Members first captured after T are simply not known at T: the answer as known
        # then is complete without them (their gap says so).
        lost_gaps = [g for g in root_gaps if g["reason"] != CAPTURED_AFTER]
        partial = bool(lost_gaps)
        withheld_objects = any(g["reason"] in PERMISSION_LOSSES for g in root_gaps)
        # Types of every readable member, removed or not: whether a measure applies never
        # depends on which members the source still holds.
        root_types = {oid.split(":", 1)[0] for oid in roots}
        roots = [oid for oid in roots if oid not in asserted]
        if prop is not None:
            self._require_declared(root_types, [prop], "measure")
            if op in ("sum", "min", "max"):
                for object_type in root_types:
                    spec = self._declared(object_type)[prop]
                    if spec["value_type"] not in NUMERIC:
                        raise ToolError("measure_not_numeric",
                                        f"{object_type}.{prop} is {spec['value_type']}; "
                                        f"{op} needs a number, quantity or money",
                                        "use count or count_distinct")
                rates = sorted(t for t in root_types if is_unit_price(self._declared(t)[prop]))
                if op == "sum" and rates:
                    return self._refusal(scope, {
                        "code": "role_mixing",
                        "message": (f"{', '.join(f'{t}.{prop}' for t in rates)} is a unit "
                                    "price (money per one unit of its denominator): a sum of "
                                    "unit prices adds rates and is no amount")[:600],
                        "hint": "sum an amount such as a line total, or use min, max or "
                                "count over unit prices"}, gaps, roots)
                if len(root_types) > 1:
                    return self._refusal(scope, {
                        "code": "role_mixing",
                        "message": (f"the set holds {', '.join(sorted(root_types))}: their "
                                    f"{prop} values are different measures (one per object "
                                    "type's grain) and are never combined")[:600],
                        "hint": "aggregate one object type at a time (narrow the set); a "
                                "count of members may span types"}, gaps, roots)
        hops: list[tuple] = []
        split = False   # one N:M hop of a sum: members reaching several are split
        if path:
            refused = self._hop_cardinality(path, op)
            hops = self.walk._validate_path(root_types or None, path)
            if refused is not None:
                return self._refusal(scope, refused, gaps, roots)
            dim_types = {hops[-1][3]}
            split = any(self.schema.link_types[h[0]]["cardinality"] == "N:M" for h in hops)
        else:
            dim_types = root_types
        if group_by:
            self._require_declared(dim_types, group_by, "group_by")
        for object_type in sorted(root_types | (dim_types if group_by else set())):
            for name in [prop] * (prop is not None) + group_by:
                spec = self._declared(object_type).get(name)
                if spec and spec.get("availability") == "not_served":
                    gaps.add("inaccessible", "not_served", f"{object_type}.{name}",
                             spec.get("availability_reason") or "not served by its source", 0)
                    partial = True

        # Permission-trimmed inputs: measure values on the members, group values on the
        # dimension objects (the members themselves without a path).
        measure_rows, measure_withheld = self._values(scope, roots, [prop] if prop else [])
        corrections = self._corrections(actor, scope.at, roots, [prop] if prop else [])
        link_marks: dict[str, set[str]] = {}
        hidden_links: dict[str, str] = {}
        several: dict[str, list[str]] = {}
        offered: dict[str, dict[str, dict[str, Mapping[str, Any]]]] = {}
        if path:
            dims, link_marks, hidden_links, several, offered = self._dimensions(
                scope, roots, hops, gaps, split)
        else:
            dims = {oid: oid for oid in roots}
        dim_ids = sorted({d for d in dims.values() if isinstance(d, str)}
                         | ({d for objs in several.values() for d in objs} if split else set()))
        group_rows, group_withheld = self._values(scope, dim_ids, group_by)
        group_fixes = self._corrections(actor, scope.at, dim_ids, group_by)
        # Members' (and dimension objects') versions: their markings are inputs of every
        # group, a count's only ones.
        versions = self.walk._versions_at(scope, sorted(set(roots) | set(dim_ids)))
        limit = self.limits["max_untrusted_text_chars"]
        path_subject = " / ".join(f"{h[0]} {h[1]}" for h in hops)

        withheld_props = 0
        withheld_links = 0
        reasons: set[str] = {"audience"} if withheld_objects else set()
        skipped = 0  # members left out of every group (their group is unknown)
        groups: dict[tuple, dict[str, Any]] = {}
        used_rows: list[Mapping[str, Any]] = []
        for oid in roots:
            dim = dims.get(oid)
            if dim is False and not split:
                gaps.add("unknown", "ambiguous_group_link", path_subject,
                         "a member reaches several objects over a to-one link; it was not grouped")
                partial = True
                skipped += 1
                continue
            link_hidden = hidden_links.get(oid)
            if link_hidden is not None:
                withheld_links += 1
                reasons.add(link_hidden)
                gaps.add("inaccessible", "group_link_withheld", path_subject,
                         "a link on a member's path is withheld from this reader, so its "
                         "group is unknown" + ("; it is grouped under null and that group "
                                               "shows no value" if dim is None else
                                               "; it was not grouped"))
                if dim is not None:  # a visible target next to a hidden one: no silent pick
                    partial = True
                    skipped += 1
                    continue
            choice = None if prop is None else _pick(measure_rows.get((oid, prop), []),
                                                     (oid, prop) in measure_withheld)
            correction = corrections.get((oid, prop or ""))
            if choice is not None and choice.row is not None and correction is not None:
                # A person's correction in force: the member's value is the person's (in
                # their unit and currency), citing the value it corrects and the
                # correction's evidence beside it.
                choice = _Pick(_corrected(choice.row, correction), choice.status,
                               choice.others_differ)
                gaps.add("unknown", "person_corrected_value_used",
                         f"{oid.split(':', 1)[0]}.{prop}",
                         "a member's value is a person's correction in force (plx.review "
                         "correct_value); the group uses it, citing the correction "
                         "(plx.object shows it beside the source value)")
            # A member reaching several records over the N:M hop of a sum, or one a person
            # allocated: one share each, as reviewed allocations state them, or not
            # grouped at all.
            offers = offered.get(oid, {}).get(prop or "", {})
            shares = None
            if dim is False or (offers and isinstance(dim, str)):
                shares = self._shares(several.get(oid) or [dim], offers, choice, gaps,
                                      path_subject)
                if shares is None:
                    partial = True
                    skipped += 1
                    continue
            if choice is not None and choice.status == "withheld":
                withheld_props += 1
                reasons.add(measure_withheld[(oid, prop)])
            for target, share in (shares.items() if shares is not None else ((dim, None),)):
                key: list[tuple[str, Any]] = []
                shown: dict[str, Any] = {}
                key_rows: list[Mapping[str, Any]] = []
                key_fixes: list[Mapping[str, Any]] = []
                skip = False
                for name in group_by:
                    if target is None:
                        key.append((name, None))
                        shown[name] = None
                        continue
                    pick = _pick(group_rows.get((target, name), []),
                                 (target, name) in group_withheld)
                    if pick.status == "withheld":
                        reasons.add(group_withheld[(target, name)])
                    if pick.status == "withheld" and pick.row is None:
                        withheld_props += 1
                        skip = True
                        break
                    if pick.status == "conflict":
                        gaps.add("unknown", "group_value_conflict",
                                 f"{target.split(':', 1)[0]}.{name}",
                                 "sources disagree with no declared precedence; the member was "
                                 "not grouped")
                        skip = True
                        break
                    if pick.status == "withheld":
                        withheld_props += 1  # an agreeing visible value; a hidden one may differ
                        skip = True
                        break
                    row = pick.row
                    if row is None:
                        key.append((name, None))
                        shown[name] = None
                        continue
                    fix = group_fixes.get((target, name))
                    if fix is not None:   # grouped by the person's value, as the measure is
                        row = _corrected(row, fix)
                        key_fixes.append(fix)
                        gaps.add("unknown", "person_corrected_value_used",
                                 f"{target.split(':', 1)[0]}.{name}",
                                 "a member's group value is a person's correction in force "
                                 "(plx.review correct_value); it is grouped by it, citing the "
                                 "correction (plx.object shows it beside the source value)")
                    number = to_decimal(row["value"]) if row["value_type"] in NUMERIC else None
                    key.append((name, (row["value_type"],
                                       row["value"] if number is None else _plain(number))))
                    shown[name] = _group_display(row, limit)
                    if fix is not None and isinstance(shown[name], dict) and fix["citations"]:
                        # The person's value is no text of the source field: it is shown as
                        # read where the correction cites it first.
                        first = fix["citations"][0]
                        shown[name] = _envelope(row["value"], first["l1_handle"],
                                                first["field_path"], limit)
                    key_rows.append(row)
                if skip:
                    partial = True
                    skipped += 1
                    continue
                if split and share is None and isinstance(target, str):
                    gaps.add("unknown", "n_to_m_single_reach", path_subject,
                             "a member reaches exactly one record over a many-to-many link; "
                             "its whole value is grouped under that record, an attribution "
                             "by reach, not a reviewed allocation (plx.review allocate)")
                if target is None and group_by and link_hidden is None:
                    gaps.add("unknown", "no_group_link", path_subject,
                             "a member reaches no visible object along the path; it is grouped "
                             "under null")
                group = groups.setdefault(tuple(key), {"key": shown, "members": [], "rows": [],
                                                       "key_rows": [], "marks": set(),
                                                       "missing": 0, "source_null": 0,
                                                       "withheld": 0, "conflict": 0,
                                                       "link_withheld": 0, "twins": 0,
                                                       "cites": []})
                group["members"].append(oid)
                group["key_rows"].extend(key_rows)
                for fix in key_fixes:
                    group["cites"].extend(fix["citations"])
                    group["marks"].update(fix["marks"])
                group["link_withheld"] += link_hidden is not None
                group["marks"].update(link_marks.get(oid, ()))
                for object_id in {oid, target} if group_by and isinstance(target, str) else {oid}:
                    if object_id in versions:
                        group["marks"].update(_markings(versions[object_id]["markings"]))
                if choice is None:
                    continue
                if choice.row is not None:
                    # A share is the member's value row carrying the allocated quantity: it
                    # cites the value it splits, and the allocation's evidence beside it.
                    group["rows"].append(choice.row if share is None
                                         else dict(choice.row, value=share["quantity"]))
                    if share is not None:
                        group["cites"].extend(share.get("citations", ()))
                    if correction is not None:
                        group["cites"].extend(correction["citations"])
                        group["marks"].update(correction["marks"])
                    if choice.others_differ:
                        gaps.add("unknown", "authority_precedence_used",
                                 f"{oid.split(':', 1)[0]}.{prop}",
                                 "sources disagree; the schema's declared precedence chose the "
                                 "value used (plx.object shows them all)")
                if choice.status == "withheld":
                    group["withheld"] += 1
                elif choice.status == "conflict":
                    group["conflict"] += 1
                elif choice.status == "source_null":
                    group["source_null"] += 1
                elif choice.status == "no_value":
                    group["missing"] += 1

        # Roles, units and currencies over the values each group would combine (every
        # group carries its own unit, currency and role).
        if prop is not None and op in ("sum", "min", "max", "count_distinct"):
            for group in groups.values():
                refused = _mixing(group["rows"], op, self._basis, self._rate)
                if refused is not None:
                    return self._refusal(scope, refused, gaps, roots, group["rows"])

        if op in ("count", "sum"):
            # Twins (one record in two systems) would be counted or summed twice.
            group_of = {oid: key for key, group in groups.items() for oid in group["members"]}
            for cluster in self._twins(scope, roots):
                together: dict[tuple, int] = {}
                for oid in cluster:
                    if oid in group_of:
                        together[group_of[oid]] = together.get(group_of[oid], 0) + 1
                for key, count in together.items():
                    if count > 1:
                        groups[key]["twins"] += count
        out_groups: list[dict[str, Any]] = []
        for key in sorted(groups, key=_sort_key):
            group = groups[key]
            # Without group_by the one group surely lacks every left-out member, so it
            # shows no value; with group_by their groups are unknown: values stand and
            # the answer is partial.
            lost = not group_by and (bool(lost_gaps) or skipped > 0)
            out, incomplete = self._group_out(group, op, prop, gaps, lost)
            partial = partial or incomplete
            used_rows.extend(group["rows"])
            out_groups.append(out)
        if not groups and not group_by:
            # An empty readable set: a count is 0, any other measure has no value. Members
            # left out (hidden, not in the projection; the set's own gap says so) make the
            # count unknown, never 0.
            out_groups.append({"key": {}, "value": "0" if op in ("count", "count_distinct")
                               and not lost_gaps else None, "inputs": 0, "markings": []})
        total = len(out_groups)
        truncated = total > MAX_GROUPS
        out_groups = out_groups[:MAX_GROUPS]
        if withheld_props or withheld_objects or withheld_links:
            partial = True
        meta, fresh = self._meta(scope, self._sources(scope, used_rows, roots))
        gaps.extend(fresh)
        complete = not truncated and not partial
        completeness: dict[str, Any] = {"complete": complete, "truncated": truncated,
                                        "returned": len(out_groups), "total": total,
                                        "next_cursor": None}
        if not complete:
            completeness["reason"] = "group_cap" if truncated else "partial_inputs"
        result: dict[str, Any] = {"groups": out_groups, "partial": partial,
                                  "gaps": gaps.out(), "completeness": completeness,
                                  "meta": meta}
        if withheld_props or withheld_objects or withheld_links:
            result["withheld"] = {"properties": withheld_props, "objects": None,
                                  "reason": _reason_of(reasons)}
        return self._fit(result, "groups")

    def _fit(self, result: dict[str, Any], key: str) -> dict[str, Any]:
        """The longest prefix of ``result[key]`` that fits the result byte limit; a cut
        is stated as ``truncated`` with reason ``result_size`` (never silent)."""
        limit = self.limits["max_result_bytes"]
        if _envelope_bytes(result) <= limit:
            return result
        items = result[key]
        low, high, best = 0, len(items), None
        while low <= high:
            mid = (low + high) // 2
            trial = dict(result, **{key: items[:mid]}, completeness=dict(
                result["completeness"], complete=False, truncated=True, returned=mid,
                reason="result_size"))
            if _envelope_bytes(trial) <= limit:
                best, low = trial, mid + 1
            else:
                high = mid - 1
        if best is None:
            raise ToolError("result_too_large", "the answer does not fit one response",
                            "narrow the set or request fewer properties")
        return best

    def _group_out(self, group: Mapping[str, Any], op: str, prop: str | None,
                   gaps: _Gaps, set_lost: bool) -> tuple[dict[str, Any], bool]:
        """One group as the contract's group. ``set_lost``: members of the set that
        belong to this group were left out (no longer readable, not in the projection
        or not groupable), so it shows no value."""
        rows = group["rows"]
        lacking = (group["missing"] + group["source_null"] + group["withheld"]
                   + group["conflict"] + group["link_withheld"])
        subject = prop or "members"
        if group["source_null"]:
            gaps.add("absent_in_source", "measure_value_null", subject,
                     "members' source records hold no value for the measure",
                     group["source_null"])
        if group["missing"]:
            gaps.add("unknown", "measure_value_missing", subject,
                     "members have no projected value for the measure", group["missing"])
        if group["conflict"]:
            gaps.add("unknown", "measure_value_conflict", subject,
                     "a member's sources disagree with no declared precedence; no value "
                     "was chosen", group["conflict"])
        if group["withheld"]:
            gaps.add("inaccessible", "marking_withheld", subject,
                     "a member's value is withheld from this reader", group["withheld"])
        if group["twins"]:
            gaps.add("unknown", "cross_system_twins", subject,
                     "members are one record held in two systems (an identity link joins "
                     "them); counting or summing both would double it, so the group shows "
                     "no value", group["twins"])
            lacking += group["twins"]
        value: str | None
        incomplete = False
        if op == "count":
            value = str(len(group["members"]) if prop is None else len(rows))
            if (set_lost or group["withheld"] or group["conflict"] or group["link_withheld"]
                    or group["twins"]):
                value, incomplete = None, True
        else:
            numbers = [to_decimal(r["value"]) for r in rows]
            if any(n is None for n in numbers) and op != "count_distinct":
                gaps.add("unknown", "unparseable_value", subject,
                         "a stored value is not a decimal number")
                lacking += 1
            if lacking or set_lost:
                value, incomplete = None, True
            elif op == "count_distinct":
                value = str(len({_value_key(r) for r in rows}))
            elif not rows:
                value = None
            elif op == "sum":
                value = _plain(sum((n for n in numbers), Decimal(0)))
            elif op == "min":
                value = _plain(min(numbers))
            else:
                value = _plain(max(numbers))
        marks = sorted(group["marks"] | {m for r in list(rows) + list(group["key_rows"])
                                         for m in _markings(r["markings"])})
        out: dict[str, Any] = {"key": group["key"], "value": value, "inputs": len(rows),
                               "markings": marks}
        if rows and op in ("sum", "min", "max"):
            first = rows[0]
            if first["value_type"] in ("quantity", "decimal") or self._rate(first):
                if first["value_type"] == "quantity":
                    out["quantity_role"] = first["quantity_role"]
                # A quantity's unit, or the one denominator a unit price is per.
                raw = {r["unit"] for r in rows}
                # One spelling stays as the source wrote it; several (each, EA) are
                # shown as Portal's folded token. A unit that is no unit token even
                # folded is null with this group's own unit_invalid gap quoting its
                # cell as data, so each group's sum names its own unit.
                spelled = next(iter(raw)) if len(raw) == 1 else unit_token(first["unit"])
                out["unit"] = unit_out(spelled)
                if spelled is not None and out["unit"] is None:
                    out["unit_gap"] = dict(_gap(
                        "unknown", "unit_invalid", subject,
                        "the recorded unit is not a unit of measure; its text is quoted "
                        "as source text"),
                        source_text=_envelope(first["unit"], first["l1_handle"], None, 600))
            if first["value_type"] == "money":
                out["currency"] = first["currency"]
                if self._basis(first) == "unknown":
                    # One source column of unknown basis combines (it is one basis,
                    # whatever it is) but is never shown as a net or gross total.
                    gaps.add("unknown", "tax_basis_unknown", subject,
                             "tax_basis: unknown. These amounts come from one source "
                             f"column ({_column(first)}) whose tax basis no upstream code "
                             "grounds; the result may include tax on some or all inputs")
        citations = list({json.dumps(c, sort_keys=True): c for c in [
            c for c in (_cite(r) for r in rows) if c is not None] + group["cites"]}.values())
        if len(citations) > MAX_CITATIONS:
            gaps.add("unknown", "citations_capped", subject,
                     f"a group has more inputs than the {MAX_CITATIONS} citations one answer "
                     "carries; narrow the set to cite every input")
        if citations:
            out["inputs_citations"] = citations[:MAX_CITATIONS]
        return out, incomplete

    def _refusal(self, scope: Any, refused: Mapping[str, str], gaps: _Gaps,
                 roots: Sequence[str], rows: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        meta, fresh = self._meta(scope, self._sources(scope, rows, roots))
        gaps.extend(fresh)
        return {"groups": [], "refused": dict(refused), "partial": False, "gaps": gaps.out(),
                "completeness": {"complete": False, "truncated": False, "returned": 0,
                                 "total": None, "reason": f"refused_{refused['code']}",
                                 "next_cursor": None},
                "meta": meta}

    # ------------------------------------------------------------------ plx.compare

    def _side(self, spec: Any, actor: Actor, at_text: str | None, gate: str,
              wanted: Sequence[str] | None) -> dict[str, Any]:
        """One side: its scope, the readable objects it reads, the readable values per
        property (authority order), the properties with a withheld value and a label.
        An object side is one object; a citation side is every value projected from
        that L1 record, on any object the reader may read."""
        arguments = {} if at_text is None else {"as_known_at": at_text}
        scope = self.walk._scope(actor, arguments, gate)
        handle = None if isinstance(spec, str) else spec["citation"]
        if handle is not None and at_text is None:
            pinned = self._citation_time(scope, handle)
            if pinned is not None:
                scope = self.walk._scope(actor, arguments, gate, (pinned, True))
        if handle is None:
            ids = sorted(self.walk._visible_ids(scope, [spec]))
            if not ids:
                if self.base.sets.live_id(spec, actor):
                    raise ToolError("not_in_projection", "the object came from a live lookup, "
                                    "not the projection store", LIVE_LOOKUP_HINT)
                if self.walk._captured_after(scope, [spec]):
                    raise ToolError("not_known_at", "the object was first captured after this "
                                    "side's as_known_at: nothing was known of it then, which "
                                    "is not evidence it did not exist",
                                    "compare it at a later as_known_at")
                raise ToolError("object_not_found", "no such object is visible to this reader",
                                "resolve the identifier with plx.find first")
            label = spec
        else:
            candidates = sorted({r[0] for r in self.store.conn.execute(
                "SELECT DISTINCT object_id FROM object_values WHERE l1_handle = ?", (handle,))})
            ids = sorted(self.walk._visible_ids(scope, candidates))
            if not ids:
                # Same answer whether nothing was projected from it or it is hidden.
                raise ToolError("citation_not_found", "no value projected from this citation "
                                "is visible to this reader",
                                "read the source itself with plx.read")
            label = handle
        names = list(wanted) if wanted else sorted({r[0] for r in self.store.conn.execute(
            "SELECT DISTINCT property FROM object_values WHERE object_id IN "
            "(SELECT value FROM json_each(?)) AND (? IS NULL OR l1_handle = ?)",
            (json.dumps(ids), handle, handle))})
        visible, withheld = self._values(scope, ids, names, handle)
        stale: set[str] = set()
        if handle is not None:
            # Values of the cited record that exist for these readable objects but are not
            # live at the time read: never "missing" (the record holds them).
            shown = {prop for (_oid, prop) in visible} | {prop for (_oid, prop) in withheld}
            for row in self.store.conn.execute(
                    "SELECT v.object_id, v.property, v.markings IN (SELECT value FROM "
                    "json_each(:marks)) AS held FROM object_values v WHERE v.l1_handle = :h "
                    "AND v.object_id IN (SELECT value FROM json_each(:ids)) AND v.property IN "
                    f"(SELECT value FROM json_each(:props)) AND NOT ({_live('v')})",
                    scope.params(h=handle, ids=json.dumps(ids), props=json.dumps(names))):
                if row["property"] in shown:
                    continue
                if row["held"]:
                    stale.add(row["property"])
                else:
                    withheld[(row["object_id"], row["property"])] = "marking"
            stale -= {prop for (_oid, prop) in withheld}
        by_prop: dict[str, list[sqlite3.Row]] = {}
        for (_oid, prop), rows in sorted(visible.items()):
            by_prop.setdefault(prop, []).extend(rows)
        for rows in by_prop.values():
            rows.sort(key=lambda r: (r["authority_rank"] is None, r["authority_rank"] or 0,
                                     r["object_id"], r["l1_handle"], r["field_path"]))
        return {"scope": scope, "ids": ids, "label": label, "values": by_prop,
                "hidden": {prop for (_oid, prop) in withheld},
                "hidden_reasons": set(withheld.values()), "stale": stale,
                "object": handle is None}

    def _citation_time(self, scope: Any, handle: str) -> dt.datetime | None:
        """When no value this reader may hold from ``handle`` is live now (a later
        capture superseded the cited record): the latest time one was recorded, the
        cited revision's own time; None while one is live (read it now)."""
        params = scope.params(h=handle)
        held = ("v.l1_handle = :h AND v.markings IN (SELECT value FROM json_each(:marks))")
        if self.store.conn.execute(f"SELECT 1 FROM object_values v WHERE {held} AND "
                                   f"{_live('v')} LIMIT 1", params).fetchone():
            return None
        row = self.store.conn.execute(f"SELECT MAX(plx_l9_time(v.recorded_at)) FROM "
                                      f"object_values v WHERE {held}", params).fetchone()
        return None if row[0] is None else _parse_time(row[0])

    def _compare(self, arguments: Mapping[str, Any], actor: Actor, gate: str
                 ) -> dict[str, Any]:
        wanted = list(arguments.get("properties", ())) or None
        object_types = {side.split(":", 1)[0] for side in (arguments["left"], arguments["right"])
                        if isinstance(side, str)}
        if wanted and object_types:
            self._require_declared_by_any(object_types, wanted)
        left = self._side(arguments["left"], actor, arguments.get("left_as_known_at"), gate,
                          wanted)
        right = self._side(arguments["right"], actor, arguments.get("right_as_known_at"), gate,
                           wanted)
        # Without a property list: every property readable on either side. A property
        # whose values are all withheld is counted, never named.
        names = wanted or sorted(set(left["values"]) | set(right["values"])
                                 | left["stale"] | right["stale"])
        gaps = _Gaps()
        limit = self.limits["max_untrusted_text_chars"]
        rows: list[dict[str, Any]] = []
        used: list[Mapping[str, Any]] = []
        shown: dict[tuple[str, str], list[dict[str, Any]]] = {}   # (side, object) -> values
        withheld = 0
        not_live = 0
        for name in names:
            stale = next((which for which, side in (("left", left), ("right", right))
                          if name in side["stale"]), None)
            if stale is not None:
                not_live += 1
                gaps.add("unknown", "citation_not_live", f"{stale}.{name}",
                         "the cited record holds this value, but not live at the time read; "
                         f"read it with {stale}_as_known_at at the citation's revision")
            picks = []
            for which, side in (("left", left), ("right", right)):
                choice = _pick(side["values"].get(name, []), name in side["hidden"])
                if name in side["hidden"]:
                    withheld += 1
                if choice.others_differ:
                    gaps.add("unknown", "authority_precedence_used", f"{which}.{name}",
                             "this side's sources disagree; its declared precedence chose "
                             "the value compared")
                if choice.status == "conflict":
                    gaps.add("unknown", "side_values_conflict", f"{which}.{name}",
                             f"{side['label'][:200]} holds disagreeing values with no "
                             "declared precedence; nothing was chosen")
                if choice.row is not None:
                    used.append(choice.row)
                picks.append(choice)
            rows.append(_compare_row(name, picks[0], picks[1], limit, self._basis, self._rate,
                                     stale))
            for which, choice in (("left", picks[0]), ("right", picks[1])):
                if choice.row is not None and rows[-1][which] is not None:
                    shown.setdefault((which, choice.row["object_id"]), []).append(
                        rows[-1][which])
        if self.base.reviews is not None:
            # A person's review in force beside each side's value, as plx.object shows it:
            # the verdict compares what the sources state, never the person's value.
            from .ontology_tools_review import show_value_reviews
            for (which, oid), values in sorted(shown.items(), key=lambda item: item[0]):
                side = left if which == "left" else right
                show_value_reviews(self.base, self.base.reviews,
                                   self.base._scope(actor, side["scope"].at), oid, values)
        for which, side in (("left", left), ("right", right)):
            if side["object"]:
                removed, unshown = self._removed(side["scope"], side["ids"])
                for oid, basis in sorted(removed.items()):
                    if basis == "source_soft_delete":
                        gaps.add(ABSENT, REMOVAL_REASONS[basis], f"{which}: {oid}",
                                 "the source marks this record deleted (its own "
                                 "soft-delete column)")
                    elif unshown == ABSENT:
                        gaps.add(ABSENT, "missing_from_source", f"{which}: {oid}",
                                 "the source's complete latest traversal no longer returns "
                                 "this record (key-only scan); not proof it was deleted")
                    else:
                        gaps.add("unknown", "missing_from_source", f"{which}: {oid}",
                                 "the source's latest traversal did not return this record, "
                                 f"but {unshown}")
        unnamed = sum(len(side["hidden"] - set(names)) for side in (left, right))
        withheld += unnamed
        total = len(rows)
        truncated = total > MAX_ROWS
        rows = rows[:MAX_ROWS]
        sources = (self._sources(left["scope"], used, left["ids"])
                   | self._sources(right["scope"], (), right["ids"]))
        meta, fresh = self._meta(left["scope"], sources)
        gaps.extend(fresh)
        complete = not truncated and not withheld and not not_live
        completeness: dict[str, Any] = {"complete": complete, "truncated": truncated,
                                        "returned": len(rows), "total": total,
                                        "next_cursor": None}
        if not complete:
            completeness["reason"] = ("row_cap" if truncated else "values_withheld" if withheld
                                      else "citation_not_live")
        result: dict[str, Any] = {"rows": rows, "gaps": gaps.out(),
                                  "completeness": completeness, "meta": meta}
        if withheld:
            result["withheld"] = {"properties": withheld, "objects": None, "reason": _reason_of(
                left["hidden_reasons"] | right["hidden_reasons"])}
        return self._fit(result, "rows")


# --------------------------------------------------------------------------- pure helpers

def _allocations(row: Mapping[str, Any], supply: str) -> list[Mapping[str, Any]]:
    """The reviewed allocations of ``supply`` a link carries: ``person_allocated``
    features of a link a person's review wrote (``ontology.reviews.link_step``), never
    of any other link."""
    if row["binding_kind"] != "reviewed" or row["tier"] != "reviewed" \
            or not row["review_action_id"]:
        return []
    try:
        features = json.loads(row["reason"])
    except ValueError:
        return []
    return [f for f in features if isinstance(f, dict) and f.get("feature") == ALLOCATED
            and f.get("supply") == supply and isinstance(f.get("property"), str)]


def _sort_key(key: tuple) -> tuple:
    """Groups in key order: numbers numerically, then text, null keys last."""
    out = []
    for _name, value in key:
        if value is None:
            out.append((2, Decimal(0), ""))
            continue
        value_type, raw = value
        number = to_decimal(raw) if value_type in NUMERIC else None
        out.append((0, number, str(raw)) if number is not None else (1, Decimal(0), str(raw)))
    return tuple(out)


def _merge_reason(old: str | None, new: str) -> str:
    return new if old in (None, new) else "marking_and_audience"


def _reason_of(reasons: Iterable[str]) -> str:
    """One contract withheld reason for a set of per-value reasons."""
    found = {part for r in reasons for part in
             (("marking", "audience") if r == "marking_and_audience" else (r,))}
    return found.pop() if len(found) == 1 else "marking_and_audience"


def _column(row: Mapping[str, Any]) -> str:
    return f"{row['binding_id']}.{row['property']}"


def _mixing(rows: Sequence[Mapping[str, Any]], op: str,
            basis: Callable[[Mapping[str, Any]], str],
            rate: Callable[[Mapping[str, Any]], bool]) -> dict[str, str] | None:
    """The contract's refusal when the values would mix roles, units, currencies, tax
    bases or price denominators, or carry an unknown unit, currency or denominator (a plain
    decimal with no unit records neither); None when they combine. ``basis`` is a money
    value's declared tax basis, ``rate`` whether it is a unit price."""
    if not rows:
        return None
    kinds = {("quantity" if r["value_type"] == "quantity" else
              "money" if r["value_type"] == "money" else "number") for r in rows}
    roles = {r["quantity_role"] for r in rows if r["value_type"] == "quantity"}
    if len(kinds) > 1 or len(roles) > 1:
        found = sorted(roles) + sorted(kinds - {"quantity"})
        return {"code": "role_mixing",
                "message": f"the values mix {', '.join(found)}; they are different "
                           "quantities and are never combined",
                "hint": "narrow the set to one role (planned, filled, inspected ... stay "
                        "separate) or one value type"}
    unknown_roles = roles - QUANTITY_TYPES
    if unknown_roles:
        return {"code": "role_mixing", "message": "a value carries an unknown quantity role",
                "hint": "rebuild the projection with the current schema"}
    plain = [r for r in rows if r["value_type"] == "decimal"]
    if plain:
        units = {unit_token(r["unit"]) for r in plain}
        if None in units:
            first = plain[0]
            return {"code": "unit_unknown",
                    "message": (f"{first['object_id'].split(':', 1)[0]}.{first['property']} "
                                "is a plain decimal whose source records no unit or "
                                "currency: its values may be amounts in different currencies "
                                f"or counts in different units, so a {op} would combine "
                                "them as one")[:600],
                    "hint": "use count, or type the column as money (currency_from) or a "
                            "quantity (unit_from) in the schema"}
        if len(units) > 1:
            return {"code": "unit_mixing",
                    "message": f"the values are in different units ({units_text(units)}); "
                               "units are never converted here",
                    "hint": "aggregate each unit separately (narrow the set)"}
    if "quantity" in kinds:
        units = {unit_token(r["unit"]) for r in rows}
        if None in units:
            return {"code": "unit_unknown",
                    "message": "some values have no recorded unit, so they cannot be "
                               f"combined with a {op}",
                    "hint": "narrow the set to values with a unit, or record the unit gap "
                            "with plx.case_record_gap"}
        if len(units) > 1:
            return {"code": "unit_mixing",
                    "message": f"the values are in different units "
                               f"({units_text(units)}); "
                               "units are never converted here",
                    "hint": "aggregate each unit separately (narrow the set)"}
    if "money" in kinds:
        currencies = {r["currency"] for r in rows}
        if None in currencies:
            return {"code": "currency_unknown",
                    "message": "some amounts have no recorded currency",
                    "hint": "narrow the set to amounts with a currency, or record the gap"}
        if len(currencies) > 1:
            return {"code": "currency_mixing",
                    "message": f"the amounts are in {', '.join(sorted(currencies))}; currencies "
                               "are never converted",
                    "hint": "aggregate each currency separately (narrow the set)"}
        priced = [r for r in rows if rate(r)]
        if priced and not same_denominator(r["unit"] for r in priced):
            units = {unit_token(r["unit"]) for r in priced}
            if None in units:
                return {"code": "unit_unknown",
                        "message": "some unit prices have no known denominator (the unit "
                                   f"they are per), so they cannot be combined with a {op}",
                        "hint": "narrow the set to prices whose source records the unit "
                                "they are per"}
            return {"code": "unit_mixing",
                    "message": f"the unit prices are per different units "
                               f"({units_text(units)}); prices per two units are never "
                               "combined and units are never converted here",
                    "hint": "aggregate the prices of each unit separately (narrow the set)"}
        bases = {basis(r) for r in rows}
        unknown = sorted({_column(r) for r in rows if basis(r) == "unknown"})
        known = sorted(bases - {"unknown"})
        if len(known) > 1 or (known and unknown) or len(unknown) > 1:
            named = known + (["unknown"] if unknown else [])
            detail = f" (unknown from {', '.join(unknown)})" if unknown else ""
            return {"code": "role_mixing",
                    "message": (f"the amounts mix tax bases {', '.join(named)}{detail}; "
                                "amounts with and without tax are never combined")[:600],
                    "hint": "narrow the set to amounts read from one source binding (each "
                            "money source declares its tax_basis in the schema)"}
    return None


def _shown(row: Mapping[str, Any] | None, limit: int) -> dict[str, Any] | None:
    if row is None:
        return None
    value, _ = _value_out(row, "X", limit)
    if value is not None and value.get("unit") is not None:
        value["unit"] = value["unit"][:40]
    return value


def _compare_row(name: str, left: _Pick, right: _Pick, limit: int,
                 basis: Callable[[Mapping[str, Any]], str],
                 rate: Callable[[Mapping[str, Any]], bool],
                 stale: str | None = None) -> dict[str, Any]:
    """One property of the two sides: equal / different / incomparable / missing_*.
    A difference is reported, never resolved. ``stale``: the side whose cited record
    holds the property but not live at the time read."""
    lval, rval = _shown(left.row, limit), _shown(right.row, limit)
    row: dict[str, Any] = {"property": name, "left": lval, "right": rval}

    def verdict(comparison: str, reason: str | None = None) -> dict[str, Any]:
        return dict(row, comparison=comparison, **({"reason": reason} if reason else {}))

    if stale is not None:
        return verdict("incomparable", f"{stale}_citation_not_live")

    for which, choice in (("left", left), ("right", right)):
        if choice.status == "conflict":
            return verdict("incomparable", "side_values_conflict")
        if choice.row is None and choice.status == "withheld":
            return verdict("incomparable", f"{which}_withheld")
    if left.row is None and right.row is None:
        return verdict("incomparable", "missing_both")
    if left.row is None:
        return verdict("missing_left", left.status)
    if right.row is None:
        return verdict("missing_right", right.status)
    if lval is None or rval is None:  # a stored value that cannot be cited or parsed
        return verdict("incomparable", "unparseable_value")
    for which, choice in (("left", left), ("right", right)):
        if choice.status == "withheld":
            # A readable value stands, but a withheld value of that side may differ.
            return verdict("incomparable", f"{which}_partly_withheld")
    lrow, rrow = left.row, right.row
    lt, rt = lrow["value_type"], rrow["value_type"]
    if lt in NUMERIC and rt in NUMERIC:
        if lt != rt and ({lt, rt} & {"money", "quantity"}):
            return verdict("incomparable", "value_type_differs")
        if "decimal" in (lt, rt):
            # A plain decimal records no currency: it compares only within one unit.
            lu, ru = unit_token(lrow["unit"]), unit_token(rrow["unit"])
            if lu is None or ru is None:
                return verdict("incomparable", "unit_unknown")
            if lu != ru:
                return verdict("incomparable", "unit_differs")
        if lt == "quantity":
            if lrow["quantity_role"] != rrow["quantity_role"]:
                return verdict("incomparable", "quantity_role_differs")
            lu, ru = unit_token(lrow["unit"]), unit_token(rrow["unit"])
            if lu is None or ru is None:
                return verdict("incomparable", "unit_unknown")
            if lu != ru:
                return verdict("incomparable", "unit_differs")
        if lt == "money":
            if lrow["currency"] is None or rrow["currency"] is None:
                return verdict("incomparable", "currency_unknown")
            if lrow["currency"] != rrow["currency"]:
                return verdict("incomparable", "currency_differs")
            if rate(lrow) != rate(rrow):
                return verdict("incomparable", "money_measure_differs")
            if rate(lrow):
                lu, ru = unit_token(lrow["unit"]), unit_token(rrow["unit"])
                if lu is None or ru is None:
                    return verdict("incomparable", "price_denominator_unknown")
                if lu != ru:
                    return verdict("incomparable", "price_denominator_differs")
            lb, rb = basis(lrow), basis(rrow)
            if "unknown" in (lb, rb) and (lb != rb or _column(lrow) != _column(rrow)):
                return verdict("incomparable", "tax_basis_unknown")
            if lb != rb:
                return verdict("incomparable", "tax_basis_differs")
        a, b = to_decimal(lrow["value"]), to_decimal(rrow["value"])
        if a is None or b is None:
            return verdict("incomparable", "unparseable_value")
        if a == b:
            return verdict("equal")
        return verdict("different", "left_greater" if a > b else "left_less")
    if lt != rt:
        return verdict("incomparable", "value_type_differs")
    if lrow["value"] == rrow["value"]:
        return verdict("equal")
    if _compare_key(lt, lrow["value"]) == _compare_key(rt, rrow["value"]):
        return verdict("equal", "equal_after_normalizing")
    return verdict("different", "values_differ")


# --------------------------------------------------------------------------- host wiring

def tool_specs(contracts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """MCP tool listings (name, description, inputSchema) for these tools."""
    return [{"name": name, "description": contracts["tools"][name]["description"],
             "inputSchema": _inline_defs(contracts["tools"][name]["input"], contracts),
             "annotations": {"readOnlyHint": True}}
            for name in TOOL_NAMES]


def register(tool_table: MutableMapping[str, dict[str, Any]], tools: AggregateTools) -> None:
    """Add ``plx.aggregate``/``plx.compare`` to a host's tool table:
    name -> {name, description, inputSchema, annotations, handler}; ``handler`` is
    ``handler(arguments, actor) -> result``. A name already present is refused."""
    for spec in tool_specs(tools.contracts):
        name = spec["name"]
        if name in tool_table:
            raise ValueError(f"tool already registered: {name}")
        tool_table[name] = dict(spec, handler=(lambda arguments, actor, _n=name:
                                               tools.call(_n, arguments, actor)))


__all__ = ["AggregateTools", "TOOL_NAMES", "register", "tool_specs"]
