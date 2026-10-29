"""Synthetic captures generated from a compiled schema's own declarations.

Not business data and never a live result: for each served binding, ``rows`` records are
generated lazily (nothing is held): record ``i`` of every relation carries the same
synthetic id in every key and link pair path, so declared links pair record ``i`` with
record ``i`` across bindings (a text-compared pair of two like-parsed properties, a name
matched by name, keeps the property value record ``i`` holds on both sides), and each
property path gets a value its declared parser accepts (unit and currency columns a value
names hold a unit or currency). Every fifth
record has a second, later capture whose property values differ (versions, capture
events). Where-predicates are satisfied when the operator allows it. A binding that
``stands_for`` another names a key of its own (``S<n>X<id>``, n its place among the type's
stand-ins), so no record of the stood-for binding or of an earlier stand-in holds it and
every stand-in projects. Each record cites
its family's scheme under a ``synthetic/`` path (``is_synthetic``).

Used to measure the projector without a source (the projection benchmarks) and to prove
every binding projects and reads back through the ontology tools (``fixture_queryable``
in ``scripts/source_registry.py``).
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping, Sequence

T0 = "2026-01-01T00:00:00.000000Z"
T1 = "2026-01-02T00:00:00.000000Z"
_BC_COMPANIES = ("bc_manufacturing", "bc_distribution")
# Capture family -> (citation scheme, audience prefix), as the real capture sources cite
# and scope their records; the handle path starts ``synthetic/`` so no synthetic citation
# reads as a source record. An unknown family keeps the ``synthetic://`` scheme, which
# no tool output contract accepts.
FAMILY_SCHEMES = {"portal_api": ("api://", "api"), "bc": ("bc://", "bc"),
                  "graph_mail": ("graph-mail://", "mail"),
                  "sharepoint": ("sharepoint://", "sharepoint"),
                  "mission_control": ("mc://", "mc"), "brain": ("brain://", "brain"),
                  "teams": ("teams-channel://", "teams")}
# A relation its family cites under another scheme (Teams chats: ``teams-chat://``).
RELATION_SCHEMES = {("teams", "chatMessages"): "teams-chat://",
                    ("teams", "chats"): "teams-chat://"}
SYNTHETIC_PATH = "synthetic/"
_PARSED = {"decimal_text": "12.50", "integer_text": "7", "iso_date": "2026-01-02",
           "yyyymmdd_date": "20260102", "iso_timestamp": "2026-01-02T03:04:05Z",
           "boolean_text": "true"}
# Columns a value names its unit or currency from (``unit_from``, ``per_unit_from``,
# ``currency_from`` paths): they hold a unit or currency, so the value is typed.
_QUALIFIERS = {"unit_from": "kg", "per_unit_from": "kg", "currency_from": "CAD"}


def _paths(schema: Any) -> dict[str, dict[str, Any]]:
    """binding id -> {"keys": paths, "links": {path: compare}, "props": {path: parser},
    "where": predicates}."""
    out: dict[str, dict[str, Any]] = {}
    for t in schema.object_types.values():
        stand_ins = 0
        for b in t.get("bindings", []):
            out[b["id"]] = {"binding": b, "keys": list(b["key"]), "links": {}, "props": {},
                            "where": list(b.get("where", []))}
            if b.get("stands_for"):
                stand_ins += 1
                out[b["id"]]["stand_in"] = stand_ins
        for p in t.get("properties", []):
            for source in p["sources"]:
                if source["binding"] in out:
                    props = out[source["binding"]]["props"]
                    props.setdefault(source["path"], source.get("parser", "as_is"))
                    for qualifier, value in _QUALIFIERS.items():
                        if isinstance(source.get(qualifier), str):
                            props[source[qualifier]] = "=" + value
    for link in schema.link_types.values():
        for lb in link["bindings"]:
            sides = [(lb.get("from_binding"), "from", lb.get("pairs", [])),
                     (lb.get("to_binding"), "to", lb.get("pairs", []))]
            via = lb.get("via")
            if via:
                sides += [(lb.get("from_binding"), "from", via["from_pairs"]),
                          (via["binding"], "to", via["from_pairs"]),
                          (via["binding"], "from", via["to_pairs"]),
                          (lb.get("to_binding"), "to", via["to_pairs"])]
            for binding, side, pairs in sides:
                if binding in out:
                    for pair in pairs:
                        if not _keeps_property(out, lb, pair):
                            out[binding]["links"].setdefault(pair[side], pair["compare"])
    return out


def _keeps_property(out: Mapping[str, dict[str, Any]], lb: Mapping[str, Any],
                    pair: Mapping[str, Any]) -> bool:
    """A text-compared pair whose both columns are properties (a name matched by name):
    each keeps its property value, which record ``i`` holds on both sides alike, so a
    stored-key link elsewhere on that record never conflicts over an id-shaped name."""
    ends = [out.get(lb.get("from_binding")), out.get(lb.get("to_binding"))]
    return (pair["compare"] == "text" and not lb.get("via") and None not in ends
            and pair["from"] in ends[0]["props"] and pair["to"] in ends[1]["props"]
            and ends[0]["props"][pair["from"]] == ends[1]["props"][pair["to"]])


def _put(row: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        nxt = row.get(part)
        if not isinstance(nxt, dict):
            nxt = row[part] = {}
        row = nxt
    row[parts[-1]] = value


def _row(spec: Mapping[str, Any], i: int, version: int) -> dict[str, Any] | None:
    row: dict[str, Any] = {}
    ident = 1000 + i
    for path, parser in spec["props"].items():
        if parser.startswith("="):
            value: Any = parser[1:]
        elif parser in _PARSED:
            value = _PARSED[parser]
            if version and parser in ("decimal_text", "integer_text"):
                value = str(int(float(value)) + version)
        elif parser == "identifier":
            value = f"PO{ident}"
        else:
            value = f"text {(i + version) % 7}"
        _put(row, path, value)
    for path, compare in spec["links"].items():
        _put(row, path, ident if compare == "integer_id" else f"PO{ident}")
    for path in spec["keys"]:
        _put(row, path, f"PO{ident}")
    for predicate in spec["where"]:
        op, value = predicate["op"], predicate.get("value")
        if op == "absent":
            _put(row, predicate["path"], None)
        elif op == "present":
            _put(row, predicate["path"], f"x{i}")
        elif op == "eq":
            _put(row, predicate["path"], value)
        elif op == "in":
            _put(row, predicate["path"], value[0])
        elif op == "prefix":
            _put(row, predicate["path"], f"{value}{ident}")
        elif op == "gte":
            _put(row, predicate["path"], value)
        elif op == "lt" and isinstance(value, int) and not isinstance(value, bool):
            _put(row, predicate["path"], ident if ident < value else value - 1)
        elif op == "ne":
            _put(row, predicate["path"], f"ne{ident}")
        else:
            return None   # lt on a non-integer: not worth generating
    if "stand_in" in spec:
        for pair in spec["binding"]["stands_for"]["pairs"]:
            _put(row, pair["from"], f"S{spec['stand_in']}X{ident}")
    return row


class SyntheticCaptures:
    """A ``CaptureSource`` of ``rows`` generated records per relation, streamed. In the
    ``changed`` relations record 3 holds other text (its keys and link values too, so
    its identity and links move): a capture that changed."""

    def __init__(self, schema: Any, source: str, rows: int,
                 changed: Sequence[str] = ()) -> None:
        self.source = source
        self.rows = rows
        self.changed = frozenset(changed)
        self._specs: dict[str, list[dict[str, Any]]] = {}
        for spec in _paths(schema).values():
            if spec["binding"]["source"] == source:
                self._specs.setdefault(spec["binding"]["relation"], []).append(spec)

    def _stream(self, relation: str) -> Iterator[Any]:
        from ontology.project import CapturedRecord

        specs = self._specs.get(relation, [])
        scheme, audience = FAMILY_SCHEMES.get(self.source, ("synthetic://", "synthetic"))
        scheme = RELATION_SCHEMES.get((self.source, relation), scheme)
        for i in range(self.rows):
            spec = specs[i % len(specs)] if specs else None
            if spec is None:
                return
            company = _BC_COMPANIES[i % 2] if self.source == "bc" else None
            versions = 2 if i % 5 == 0 else 1
            for version in range(versions):
                row = _row(spec, i, version)
                if row is None:
                    break
                digest = f"{i:064x}"
                if i == 3 and relation in self.changed:
                    row = {k: v + "9" if isinstance(v, str) else v for k, v in row.items()}
                    digest = "f" * 64
                start = T0 if version == 0 else T1
                end = T1 if version + 1 < versions else None
                yield CapturedRecord(
                    self.source, relation, f"{relation}/{i}", row,
                    f"{scheme}{SYNTHETIC_PATH}{relation}/{i}@v{version}", "row", digest, start,
                    end, end or start, f"{audience}:{relation}",
                    capture_lineage=company)

    def records(self, relations: Sequence[str]) -> Mapping[str, Any]:
        from ontology.project import _Relations
        return _Relations(relations, self._stream)

    def describe(self) -> dict[str, Any]:
        return {"generation": "synthetic", "relations": {}}


def is_synthetic(l1_handle: Any) -> bool:
    """A handle these captures cite (any family): the item-ACL re-check of a synthetic
    store grants these and nothing else."""
    schemes = {scheme for scheme, _ in FAMILY_SCHEMES.values()} | set(RELATION_SCHEMES.values())
    return isinstance(l1_handle, str) and any(
        l1_handle.startswith(scheme + SYNTHETIC_PATH) for scheme in schemes)


def min_rows(schema: Any) -> int:
    """Rows per relation for every binding to get a record: bindings sharing a relation
    take its records in turn."""
    shared: dict[tuple[str, str], int] = {}
    for spec in _paths(schema).values():
        key = (spec["binding"]["source"], spec["binding"]["relation"])
        shared[key] = shared.get(key, 0) + 1
    return max(shared.values(), default=1)


def sources(schema: Any, rows: int, changed: Sequence[str] = (),
            only: Sequence[str] = ()) -> dict[str, SyntheticCaptures]:
    """One synthetic source per capture family of ``schema`` (``only``: just those)."""
    families = sorted(set(only) or {b["source"] for t in schema.object_types.values()
                                    for b in t.get("bindings", [])})
    return {family: SyntheticCaptures(schema, family, rows, changed) for family in families}
