"""Source write stamps: columns a capture rewrites without the record changing.

Source syncs restamp records they did not change: the Portal ETL bumps ``updated_at`` (and
a product's Sage sync times) on every run, BC moves an aged balance's ``agedAsOfDate`` and
the period labels computed from it every day, Teams regenerates the ids of an adaptive
card's attachments and moves a chat's ``last_activity_at``. Each restamp is a new captured
version; projected as a new object version it stores every value again and every link
touching the record once more per pair of end versions (GROWTH-AUDIT-1111: about 90% of the
ontology's growth).

``ontology/schema/stamps.json`` declares, per source relation, the columns that are write
stamps. The declaration is reviewed, never learned at run time: a column is listed only
when it is write metadata (a write or sync time, an entity tag, a regenerated id, or a label
computed from such a time; never an amount, quantity, status, flag or business date) and,
in the store's own history, at least ``SIGHTING_SHARE`` of the version pairs in which it
changes change nothing else but write metadata (each entry records that evidence).

The projector (``ontology.project.Projector``) reads consecutive captured versions of one
record that differ only in their stamps as one version, a sighting of the content it
already holds: the version starts at the first of them and cites the last (its values and
citations are the newest capture's, as before), and the capture that first held the
content stays the version's ``first_handle`` (``CapturedRecord``). Any other column that
changes still makes a new version.

Fail closed: a stamp may never be a column the projection identifies or joins records by
(a binding's key, ``where`` filter, lineage, link pairs, ``stands_for`` pairs, an event's
time, a currency rule's path); :func:`check` refuses such a declaration, and an entry
naming a source relation no binding reads is refused too (a typo never silently declares
nothing).

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Iterator, Mapping

FORMAT = "plx-ontology-stamps/v1"
PATH = Path(__file__).with_name("schema") / "stamps.json"
# The least share of a column's changes that must be sightings (nothing but write
# metadata changing with it) for the column to be declared a stamp.
SIGHTING_SHARE = 0.9
_EACH = "*"   # a path part naming every element of a list


class StampError(ValueError):
    pass


class Stamps:
    """The declared stamps of one source relation."""

    def __init__(self, entries: Iterable[Any]) -> None:
        self.paths: list[tuple[str, ...]] = []
        self.masks: list[tuple[tuple[str, ...], re.Pattern[str]]] = []
        for entry in entries:
            if isinstance(entry, str) and entry:
                self.paths.append(tuple(entry.split(".")))
            elif isinstance(entry, Mapping) and isinstance(entry.get("path"), str) and \
                    isinstance(entry.get("mask"), str):
                self.masks.append((tuple(entry["path"].split(".")), re.compile(entry["mask"])))
            else:
                raise StampError(f"stamp entry invalid: {entry!r}")

    def names(self) -> list[str]:
        return [".".join(p) for p in self.paths] + [".".join(p) for p, _ in self.masks]

    def sighting_key(self, row: Mapping[str, Any]) -> str:
        """Digest of ``row`` without its stamps: two versions with the same key differ in
        their stamps alone."""
        held = json.loads(json.dumps(row, default=str))
        for path in self.paths:
            _drop(held, path)
        for path, mask in self.masks:
            _mask(held, path, mask)
        text = json.dumps(held, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _nodes(value: Any, parts: tuple[str, ...]) -> Iterator[tuple[Any, str]]:
    """(container, last part) of every place ``parts`` names in ``value``."""
    if not parts:
        return
    head, rest = parts[0], parts[1:]
    if head == _EACH:
        items = value if isinstance(value, list) else []
        for item in items:
            yield from _nodes(item, rest) if rest else ()
        return
    if not isinstance(value, dict) or head not in value:
        return
    if not rest:
        yield value, head
    else:
        yield from _nodes(value[head], rest)


def _drop(row: Any, path: tuple[str, ...]) -> None:
    for container, name in list(_nodes(row, path)):
        container.pop(name, None)


def _mask(row: Any, path: tuple[str, ...], mask: re.Pattern[str]) -> None:
    for container, name in list(_nodes(row, path)):
        if isinstance(container[name], str):
            container[name] = mask.sub("", container[name])


def load(path: str | Path = PATH) -> dict[tuple[str, str], Stamps]:
    """(source, relation) -> its declared stamps."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != FORMAT:
        raise StampError(f"stamps format {data.get('format')!r} is not {FORMAT}")
    out: dict[tuple[str, str], Stamps] = {}
    for entry in data.get("relations", []):
        key = (entry.get("source"), entry.get("relation"))
        if not all(isinstance(k, str) and k for k in key) or key in out:
            raise StampError(f"stamp relation invalid or repeated: {key!r}")
        out[key] = Stamps(entry.get("stamps") or [])  # type: ignore[index]
    return out


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _paths_named(value: Any) -> Iterator[str]:
    """Every ``path`` / ``*path`` / ``from`` / ``to`` text in ``value``, at any depth."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            if isinstance(v, str) and (k in ("path", "from", "to") or k.endswith("path")):
                yield v
            elif isinstance(v, (Mapping, list, tuple)):
                yield from _paths_named(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _paths_named(item)


def identity_paths(bindings: Mapping[str, tuple[str, Mapping[str, Any]]],
                   links: Mapping[str, Mapping[str, Any]],
                   events: Mapping[str, Mapping[str, Any]],
                   types: Mapping[str, Mapping[str, Any]]) -> dict[str, set[str]]:
    """binding id -> the row paths it identifies, filters, joins or dates records by."""
    out: dict[str, set[str]] = {bid: set() for bid in bindings}
    for bid, (_tid, b) in bindings.items():
        held = out[bid]
        held |= set(_strings(b.get("key")))
        held |= set(_paths_named(b.get("where")))
        held |= set(_strings(b.get("lineage")))
        held |= set(_paths_named(b.get("local_currency")))
        stands = b.get("stands_for") or {}
        for pair in stands.get("pairs", ()):
            held.add(pair.get("from"))
            if stands.get("binding") in out:
                out[stands["binding"]].add(pair.get("to"))
    for link in links.values():
        for lb in link.get("bindings", ()):
            ends = (lb.get("from_binding"), lb.get("to_binding"))
            for pair in lb.get("pairs", ()) or ():
                for side, bid in zip(("from", "to"), ends):
                    if bid in out:
                        out[bid].add(pair.get(side))
            via = lb.get("via") or {}
            for pairs, sides in ((via.get("from_pairs"), (ends[0], via.get("binding"))),
                                 (via.get("to_pairs"), (via.get("binding"), ends[1]))):
                for pair in pairs or ():
                    for side, bid in zip(("from", "to"), sides):
                        if bid in out:
                            out[bid].add(pair.get(side))
            for bid in ends:
                if bid in out:
                    out[bid] |= set(_paths_named(lb.get("where")))
    for ev in events.values():
        time = ev.get("time") or {}
        if time.get("binding") in out:
            out[time["binding"]].add(time.get("path"))
        observed = ev.get("observed_property")
        tid = ev.get("object_type")
        if observed and tid in types:
            # a change event compares this property between versions: its sources must
            # never be folded into one version
            for prop in types[tid].get("properties", ()) or ():
                if isinstance(prop, Mapping) and prop.get("name") == observed:
                    for src in prop.get("sources", ()) or ():
                        if isinstance(src, Mapping) and src.get("binding") in out:
                            out[src["binding"]] |= set(_paths_named(src))
    return {bid: {p for p in paths if isinstance(p, str) and p} for bid, paths in out.items()}


def _overlaps(stamp: str, path: str) -> bool:
    a = stamp.split(".")
    b = path.split(".")
    n = min(len(a), len(b))
    return all(x == y or _EACH in (x, y) for x, y in zip(a[:n], b[:n]))


def check(declared: Mapping[tuple[str, str], Stamps],
          bindings: Mapping[str, tuple[str, Mapping[str, Any]]],
          links: Mapping[str, Mapping[str, Any]], events: Mapping[str, Mapping[str, Any]],
          types: Mapping[str, Mapping[str, Any]]) -> dict[str, Stamps]:
    """binding id -> the stamps of the relation it reads; refuses (``StampError``) a stamp
    on a path the projection identifies, filters, joins or dates records by."""
    read = identity_paths(bindings, links, events, types)
    out: dict[str, Stamps] = {}
    for bid, (_tid, b) in bindings.items():
        stamps = declared.get((b.get("source"), b.get("relation")))
        if stamps is None:
            continue
        for stamp in stamps.names():
            hit = sorted(p for p in read[bid] if _overlaps(stamp, p))
            if hit:
                raise StampError(f"stamp {stamp!r} of {b.get('source')}:{b.get('relation')} "
                                 f"is a path binding {bid} reads records by: {hit}")
        out[bid] = stamps
    return out


def unbound(declared: Mapping[tuple[str, str], Stamps],
            bindings: Mapping[str, tuple[str, Mapping[str, Any]]]) -> list[tuple[str, str]]:
    """Declared relations no binding reads."""
    bound = {(b.get("source"), b.get("relation")) for _tid, b in bindings.values()}
    return sorted(set(declared) - bound)


__all__ = ["FORMAT", "PATH", "SIGHTING_SHARE", "StampError", "Stamps", "check",
           "identity_paths", "load", "unbound"]
