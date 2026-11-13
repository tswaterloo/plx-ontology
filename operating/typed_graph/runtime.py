"""Typed objects over the served graph, read only through the reader's own tools.

The generated models (``operating/typed_graph/models.py``, from the ontology schema by
``tools/gen_models.py``) are frozen dataclasses: one per object type, a field per
declared property holding a :class:`Cited` typed value (money as :class:`Money` with its
currency, quantities as :class:`Quantity` with their unit, dates as ``datetime.date``),
and one method per declared link type, named by the link (``po.lines()``,
``line.receipt_lines()``, ``lot.received_in()``), returning one object or a
:class:`Related` list as the link's cardinality says.

:class:`GraphRepository` builds them. It holds a host's tool table (``reads``: anything
with ``call(name, arguments, actor)``, in production ``operating.ontology_host.
CaseHostTools``, reading Postgres through ``operating.pg.ontology_reader``) and one
reader. Every read is that reader's ``plx.object`` or ``plx.around`` call, so the read
policy (audience keys, marking sets, value markings, the item gate, the people exclusion,
the as-known-at time) is applied once, where it always is: this module adds no SQL and no
permission rule of its own. What the reader may not see is absent (an object is ``None``
exactly as a missing one; a withheld value leaves its field ``None`` and is counted in
``withheld``), never inferred.

Fail closed:

* a tool error other than "not visible" raises :class:`GraphReadError`;
* an object of a type the models do not know, a property they do not declare or a value
  of another value type raises :class:`SchemaMismatch` (the models are stale);
* a to-one link that reaches two or more objects raises :class:`CardinalityError` with
  both, never one picked;
* a value that does not parse as its declared type (an undeclared enum member, a money
  amount that is not a number) is kept as ``raw`` with ``problem`` set and ``value`` None;
* values of one property that disagree leave the field ``None`` and name the property in
  ``conflicts``; every value stays in ``values``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields as dataclass_fields
import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Generic, Iterable, Mapping, Optional, Sequence, TypeVar

T = TypeVar("T")
M = TypeVar("M", bound="GraphModel")


class GraphReadError(RuntimeError):
    """A tool answered an error other than "not visible to this reader"."""

    def __init__(self, tool: str, code: str, message: str = "") -> None:
        super().__init__(f"{tool}: {code}" + (f": {message}" if message else ""))
        self.tool, self.code = tool, code


class SchemaMismatch(RuntimeError):
    """The served data names a type, property or value type the models do not declare:
    regenerate them (``python3 tools/gen_models.py``)."""


class CardinalityError(RuntimeError):
    """A to-one link reached more than one object."""

    def __init__(self, link_type: str, direction: str, start: str, found: Sequence[str]
                 ) -> None:
        super().__init__(f"{link_type} {direction} from {start} reached {len(found)} "
                         f"objects, the schema allows one: {', '.join(found)}")
        self.link_type, self.direction, self.start = link_type, direction, start
        self.found = tuple(found)


@dataclass(frozen=True)
class Citation:
    l1_handle: str
    field_path: Optional[str]
    digest: Optional[str]
    lineage: Optional[str]

    @classmethod
    def of(cls, raw: Mapping[str, Any]) -> "Citation":
        return cls(raw.get("l1_handle"), raw.get("field_path"), raw.get("digest"),
                   raw.get("lineage"))


@dataclass(frozen=True)
class Money:
    """An amount and its currency. ``currency`` is None when the source states none and
    none could be cited (``currency_gap`` says why): never a default currency."""
    amount: Decimal
    currency: Optional[str]
    currency_gap: Optional[str] = None


@dataclass(frozen=True)
class Quantity:
    """An amount, its unit (None with ``unit_gap`` when unknown) and its role."""
    amount: Decimal
    unit: Optional[str]
    role: Optional[str] = None
    unit_gap: Optional[str] = None


@dataclass(frozen=True)
class Cited(Generic[T]):
    """One property value as the reader's tool returned it, typed, with its source.
    ``value`` is None when the stored value does not parse as its declared type
    (``problem`` says why; ``raw`` is what was stored)."""
    property: str
    value: Optional[T]
    citation: Citation
    markings: tuple[str, ...]
    raw: Any = None
    problem: Optional[str] = None
    untrusted: bool = False
    authority_rank: Optional[int] = None


class Related(list, Generic[M]):  # type: ignore[type-arg]
    """The objects a to-many link reaches, as the reader may see them. ``complete`` is
    False when the tool said the answer is partial or the page limit stopped the walk;
    ``withheld`` is the tool's statement of what this reader may not see (never a list)."""

    def __init__(self, items: Iterable[M] = (), *, complete: bool = True,
                 withheld: Optional[Mapping[str, Any]] = None,
                 gaps: Sequence[Mapping[str, Any]] = (),
                 tiers: Optional[Mapping[str, str]] = None) -> None:
        super().__init__(items)
        self.complete = complete
        self.withheld = dict(withheld) if withheld else None
        self.gaps = list(gaps)
        self.tiers = dict(tiers or {})   # object id -> best tier of the link reaching it


# --------------------------------------------------------------------------- values

def _text(value: Any) -> tuple[Any, bool]:
    if isinstance(value, Mapping) and "untrusted_text" in value:
        return value["untrusted_text"], True
    return value, False


def _decimal(text: Any) -> Optional[Decimal]:
    if isinstance(text, bool) or text is None:
        return None
    try:
        out = Decimal(str(text))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def parse_value(raw: Mapping[str, Any], spec: "PropertySpec") -> Cited[Any]:
    """The tool's value object as a :class:`Cited` of the declared Python type."""
    if raw.get("value_type") != spec.value_type:
        raise SchemaMismatch(f"{spec.owner}.{spec.name}: served as "
                             f"{raw.get('value_type')}, declared {spec.value_type}")
    stored, untrusted = _text(raw.get("value"))
    cited = dict(property=spec.name, citation=Citation.of(raw.get("citation") or {}),
                 markings=tuple(raw.get("markings") or ()), raw=stored, untrusted=untrusted,
                 authority_rank=raw.get("authority_rank"))
    gap = raw.get("value_gap")
    if stored is None:
        return Cited(value=None, problem=(gap or {}).get("reason") or "no_value", **cited)
    kind = spec.value_type
    value: Any = None
    problem: Optional[str] = None
    if kind in ("text", "identifier"):
        value = stored if isinstance(stored, str) else None
    elif kind == "enum":
        if not isinstance(stored, str):
            problem = "not_text"
        elif spec.enum_values and stored not in spec.enum_values:
            problem = "enum_value_undeclared"
        else:
            value = stored
    elif kind == "money":
        amount = _decimal(stored)
        if amount is not None:
            value = Money(amount, raw.get("currency"),
                          (raw.get("currency_gap") or {}).get("reason"))
    elif kind == "quantity":
        amount = _decimal(stored)
        if amount is not None:
            value = Quantity(amount, raw.get("unit"), raw.get("quantity_role"),
                             (raw.get("unit_gap") or {}).get("reason"))
    elif kind == "decimal":
        value = _decimal(stored)
    elif kind == "integer":
        number = _decimal(stored)
        if number is not None and number == number.to_integral_value():
            value = int(number)
    elif kind == "boolean":
        value = stored if isinstance(stored, bool) else {
            "true": True, "false": False, "1": True, "0": False}.get(str(stored).lower())
    elif kind == "date":
        try:
            value = dt.date.fromisoformat(str(stored))
        except ValueError:
            value = None
    elif kind == "timestamp":
        try:
            moment = dt.datetime.fromisoformat(str(stored).replace("Z", "+00:00"))
            value = moment if moment.tzinfo is not None else None
        except ValueError:
            value = None
    elif kind == "json":
        value = stored
    else:
        raise SchemaMismatch(f"{spec.owner}.{spec.name}: value type {kind} has no Python type")
    if value is None and problem is None:
        problem = f"not_a_{kind}"
    return Cited(value=value, problem=problem, **cited)


def _same(first: Cited[Any], second: Cited[Any]) -> bool:
    return first.value == second.value and first.problem is None and second.problem is None


# --------------------------------------------------------------------------- models

@dataclass(frozen=True)
class PropertySpec:
    owner: str
    name: str
    value_type: str
    enum_values: tuple[str, ...] = ()
    field_name: str = ""


@dataclass(frozen=True)
class LinkSpec:
    link_type: str
    direction: str          # forward | reverse
    target: str             # object type reached
    to_one: bool


@dataclass(frozen=True, kw_only=True)
class GraphModel:
    """Base of every generated model. ``values``: every value shown, per property;
    ``conflicts``: properties whose values disagree (their field is None); ``withheld``:
    the tool's count of what this reader may not see; ``complete``: whether every value
    was read."""
    OBJECT_TYPE: ClassVar[str] = ""
    PROPERTIES: ClassVar[Mapping[str, PropertySpec]] = {}

    object_id: str
    display: Optional[str] = None
    lineage: Optional[str] = None
    values: Mapping[str, tuple[Cited[Any], ...]] = field(default_factory=dict, repr=False)
    conflicts: frozenset[str] = frozenset()
    withheld: Optional[Mapping[str, Any]] = field(default=None, repr=False)
    gaps: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)
    complete: bool = True
    _repo: Any = field(default=None, repr=False, compare=False)

    def values_of(self, prop: str) -> tuple[Cited[Any], ...]:
        if prop not in self.PROPERTIES:
            raise SchemaMismatch(f"{self.OBJECT_TYPE} declares no property {prop}")
        return self.values.get(prop, ())

    def _one(self, link: LinkSpec) -> Optional["GraphModel"]:
        return self._bound().one(self, link)

    def _many(self, link: LinkSpec) -> Related[Any]:
        return self._bound().many(self, link)

    def _bound(self) -> "GraphRepository":
        if self._repo is None:
            raise GraphReadError("model", "not_bound", "read the object through a GraphRepository")
        return self._repo


# --------------------------------------------------------------------------- repository

class GraphRepository:
    """Typed reads for one reader through that reader's tool table (module docstring).

    ``models``: object type -> generated model class (default: every generated model).
    ``as_known_at``: passed to every call (None: now). ``max_pages`` bounds a to-many
    walk (50 objects a page); a walk stopped by it is marked incomplete."""

    def __init__(self, reads: Any, actor: Any, *, models: Mapping[str, type] | None = None,
                 as_known_at: str | None = None, max_pages: int = 20) -> None:
        if models is None:
            from operating.typed_graph.models import MODELS
            models = MODELS
        self.reads, self.actor, self.models = reads, actor, dict(models)
        self.as_known_at, self.max_pages = as_known_at, max_pages

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.as_known_at is not None:
            arguments["as_known_at"] = self.as_known_at
        result = self.reads.call(tool, arguments, self.actor)
        if not isinstance(result, Mapping):
            raise GraphReadError(tool, "malformed_result")
        return dict(result)

    def get(self, object_id: str, model: type[M] | None = None) -> M | None:
        """The object as this reader sees it, or None when it is not visible to them
        (the same answer as for an object that does not exist)."""
        pages: list[dict[str, Any]] = []
        cursor = None
        while True:
            arguments: dict[str, Any] = {"object_id": object_id, "response_format": "detailed"}
            if cursor:
                arguments["cursor"] = cursor
            result = self._call("plx.object", arguments)
            error = result.get("error")
            if error:
                if error.get("code") == "object_not_found":
                    return None
                raise GraphReadError("plx.object", str(error.get("code")),
                                     str(error.get("message") or ""))
            pages.append(result)
            cursor = (result.get("completeness") or {}).get("next_cursor")
            if not cursor or len(pages) >= self.max_pages:
                break
        return self._build(pages, model)

    def _build(self, pages: Sequence[Mapping[str, Any]], model: type[M] | None) -> M:
        first = pages[0]
        ref = first["object"]
        object_type = ref.get("object_type")
        cls = self.models.get(object_type)
        if cls is None:
            raise SchemaMismatch(f"no model for object type {object_type}")
        if model is not None and cls is not model:
            raise SchemaMismatch(f"{ref.get('object_id')} is a {object_type}, "
                                 f"not a {model.OBJECT_TYPE}")
        grouped: dict[str, list[Cited[Any]]] = {}
        for page in pages:
            for raw in page.get("values") or ():
                spec = cls.PROPERTIES.get(raw.get("property"))
                if spec is None:
                    raise SchemaMismatch(f"{object_type} declares no property "
                                         f"{raw.get('property')}")
                grouped.setdefault(spec.name, []).append(parse_value(raw, spec))
        disputed = {value.get("property") for page in pages
                    for conflict in page.get("conflicts") or ()
                    if conflict.get("kind") == "value_disagreement"
                    for value in conflict.get("values") or ()}
        chosen: dict[str, Any] = {}
        conflicts = set()
        for name, cited in grouped.items():
            if name in disputed or any(not _same(cited[0], other) for other in cited[1:]):
                conflicts.add(name)
                continue
            chosen[cls.PROPERTIES[name].field_name] = cited[0]
        last = pages[-1].get("completeness") or {}
        display, _ = _text(ref.get("display"))
        return cls(object_id=ref["object_id"], display=display, lineage=ref.get("lineage"),
                   values={k: tuple(v) for k, v in grouped.items()},
                   conflicts=frozenset(conflicts), withheld=first.get("withheld"),
                   gaps=tuple(g for page in pages for g in page.get("gaps") or ()),
                   complete=bool(last.get("complete")) and not last.get("next_cursor"),
                   _repo=self, **chosen)

    def _walk(self, start: GraphModel, link: LinkSpec, limit: int, pages: int
              ) -> tuple[list[tuple[str, str]], dict[str, str], bool, Any, list[Any]]:
        refs: list[tuple[str, str]] = []
        tiers: dict[str, str] = {}
        withheld, gaps = None, []
        cursor, complete = None, True
        self._cut_off = False
        for page in range(pages):
            arguments: dict[str, Any] = {
                "from": start.object_id, "limit": limit,
                "path": [{"link_type": link.link_type, "direction": link.direction}]}
            if cursor:
                arguments["cursor"] = cursor
            result = self._call("plx.around", arguments)
            error = result.get("error")
            if error:
                if error.get("code") == "object_not_found":
                    return [], {}, True, None, []
                raise GraphReadError("plx.around", str(error.get("code")),
                                     str(error.get("message") or ""))
            if page == 0:
                withheld = result.get("withheld")
            gaps.extend(result.get("gaps") or ())
            for ref in result.get("objects") or ():
                if ref["object_id"] not in tiers:
                    refs.append((ref["object_id"], ref.get("object_type")))
                    tiers[ref["object_id"]] = ""
            for edge in result.get("edges") or ():
                for end in (edge.get("from"), edge.get("to")):
                    if end in tiers and not tiers[end]:
                        tiers[end] = edge.get("tier") or ""
            completeness = result.get("completeness") or {}
            cursor = completeness.get("next_cursor")
            if not cursor:
                complete = bool(completeness.get("complete"))
                break
        else:
            complete = False   # stopped by the page limit
            self._cut_off = True
        return refs, tiers, complete, withheld, gaps

    def one(self, start: GraphModel, link: LinkSpec) -> Optional[GraphModel]:
        """The object a to-one link reaches, None when none is visible; two or more
        raise :class:`CardinalityError` (never one picked)."""
        refs, _tiers, complete, _withheld, _gaps = self._walk(start, link, 2, 1)
        if self._cut_off and len(refs) <= 1:
            # the page was cut off: an unread edge may reach a second object
            raise GraphReadError("plx.around", "trimmed",
                                 f"{link.link_type} {link.direction} read was cut off "
                                 "before every edge was seen")
        if len(refs) > 1:
            raise CardinalityError(link.link_type, link.direction, start.object_id,
                                   [r[0] for r in refs])
        if not refs:
            return None
        return self._hydrate(refs[0], link)

    def many(self, start: GraphModel, link: LinkSpec) -> Related[Any]:
        refs, tiers, complete, withheld, gaps = self._walk(start, link, 50, self.max_pages)
        out = []
        for ref in refs:
            obj = self._hydrate(ref, link)
            if obj is None:          # not visible on the object read: never invented
                complete = False
                continue
            out.append(obj)
        return Related(out, complete=complete, withheld=withheld, gaps=gaps, tiers=tiers)

    def _hydrate(self, ref: tuple[str, str], link: LinkSpec) -> Optional[GraphModel]:
        object_id, object_type = ref
        if object_type != link.target:
            raise SchemaMismatch(f"{link.link_type} {link.direction} reached a "
                                 f"{object_type}, declared {link.target}")
        return self.get(object_id, self.models.get(object_type))


def model_fields(cls: type) -> list[str]:
    return [f.name for f in dataclass_fields(cls)]
