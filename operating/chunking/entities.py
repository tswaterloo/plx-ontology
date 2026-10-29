"""``search.entities``: the names and ids a question may name, for fuzzy resolution.

Customers, suppliers, items, purchase and sales orders, customer POs, lots, batches,
assembly orders and contacts (people, by their own records: BC contacts carry ``pii`` and
only a reader holding it sees them). Each entity is one projected object: its display
name (a name value, else its primary identifier), its aliases (every business
identifier, raw and normalised, and the name without a company suffix) and a folded
form for trigram matching (``fold``). Keyed as its object: ``objects.audience_key`` and
the markings of its current and latest versions plus its name's (a contact's name is
``pii``: only a reader holding it sees the row); an identifier marked beyond that is left
out of the aliases, never raising the row. Never a merge: two records of one customer
stay two entities.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from . import clean
from . import headers as hd
from .permissions import parse_markings, valid_key
from .projection import Counts

# object type -> (entity type, name properties in order)
ENTITY_TYPES = {"Customer": ("customer", ("name",)), "Supplier": ("supplier", ("name",)),
                "Product": ("item", ("name",)), "FgItem": ("item", ("name",)),
                "PurchaseOrder": ("purchase_order", ()), "SalesOrder": ("sales_order", ()),
                "CustomerPurchaseOrder": ("customer_po", ()), "Lot": ("lot", ()),
                "Batch": ("batch", ()), "AssemblyOrder": ("assembly_order", ()),
                "Contact": ("person", ("display_name",)),
                "Manufacturer": ("manufacturer", ("name",)),
                "RelPublicAppManufacturer": ("manufacturer", ("name",)),
                "RelSageApVendor": ("supplier", ("vendname", "legalname")),
                "CustomerLocation": ("customer_location", ("label",))}
# The record's own values people describe an entity by (place, category, short name,
# site). Read at markings the row already carries, never raising it: a value marked
# beyond the row (a location's ``pii`` city, a supplier item's ``finance`` origin) is left
# out, so nothing person-private reaches an alias. Never a linked record's value: another
# audience unit's fact would reach this row's readers.
ATTRIBUTE_PROPS = ("country", "codectry", "country_of_origin", "state", "codestte",
                   "province", "city", "namecity", "item_category", "category", "brand",
                   "shortname", "legalname", "website", "currency", "location_type",
                   "ship_to_code", "bc_ship_to_code")
# Country and province codes and names -> the words a question uses for them.
PLACE_WORDS = {
    "ca": "canada canadian", "can": "canada canadian", "canada": "canada canadian",
    "us": "united states usa american", "usa": "united states usa american",
    "united states": "united states usa american", "mx": "mexico mexican",
    "mexico": "mexico mexican", "gb": "united kingdom uk british", "uk": "united kingdom uk british",
    "fr": "france french", "de": "germany german", "it": "italy italian", "es": "spain spanish",
    "cn": "china chinese", "in": "india indian", "au": "australia australian",
    "nl": "netherlands dutch", "kr": "korea korean", "jp": "japan japanese",
    "on": "ontario", "qc": "quebec", "bc": "british columbia", "ab": "alberta",
    "mb": "manitoba", "ns": "nova scotia", "nb": "new brunswick", "sk": "saskatchewan",
    "ny": "new york", "ca-on": "ontario", "ca-qc": "quebec"}
_COUNTRY_PROPS = frozenset({"country", "codectry", "country_of_origin", "state", "codestte",
                            "province"})
_SUFFIX = re.compile(r"\b(?:inc|incorporated|ltd|limited|llc|l l c|corp|corporation|co|"
                     r"company|gmbh|s a|sa|srl|bv|plc|pty|ag|ulc|lp)\b\.?$")
BATCH = 2000


@dataclass(frozen=True)
class Entity:
    entity_id: str
    object_id: str
    type: str
    name: str
    alias: tuple[str, ...]
    normalized: str
    audience_key: str
    markings: tuple[str, ...]


def fold(text: str) -> str:
    """Lower case, accents and punctuation off, ``&`` as ``and``, spaces collapsed."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = text.replace("&", " and ")
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


# Entity types that are people: kept only when the people exclusion clears their address.
PERSON_TYPES = frozenset({"person"})
_PERSON_KEYS = ("email",)


def entities(conn: Any, counts: Counts | None = None, types: Sequence[str] | None = None,
             limit: int | None = None, exclusion: Any = None) -> Iterator[Entity]:
    """Every entity; ``exclusion`` (``permissions.Exclusion``) withholds a unit it does not
    clear and a person whose address it does not clear (no guard, an unavailable one, or
    no address: no person is listed)."""
    counts = counts or Counts()
    produced = 0
    for object_type, (etype, name_props) in sorted(ENTITY_TYPES.items()):
        if types is not None and etype not in types:
            continue
        after = ""
        while True:
            rows = conn.execute(
                "SELECT o.object_id, o.audience_key, v.markings FROM objects o "
                "JOIN object_versions v ON v.object_id = o.object_id AND v.superseded_at IS NULL "
                "WHERE o.object_type = ? AND o.object_id > ? AND o.missing_from_source_since "
                "IS NULL ORDER BY o.object_id LIMIT ?", (object_type, after, BATCH)).fetchall()
            if not rows:
                break
            after = rows[-1]["object_id"]
            ids = json.dumps([r["object_id"] for r in rows])
            latest: dict[str, str] = {}
            for r in conn.execute("SELECT object_id, markings FROM object_versions WHERE "
                                  "object_id IN (SELECT value FROM json_each(?)) "
                                  "ORDER BY julianday(recorded_at)", (ids,)):
                latest[r["object_id"]] = r["markings"]
            names: dict[str, list[tuple[str, str]]] = {}
            attrs: dict[str, list[tuple[str, str, str]]] = {}
            addresses: dict[str, list[str]] = {}
            for r in conn.execute(
                    "SELECT object_id, property, value, markings FROM object_values WHERE "
                    "object_id IN (SELECT value FROM json_each(?)) AND superseded_at IS NULL "
                    "AND property IN (SELECT value FROM json_each(?))",
                    (ids, json.dumps(list(name_props) + list(ATTRIBUTE_PROPS)
                                     + list(_PERSON_KEYS)))):
                if not (isinstance(r["value"], str) and r["value"].strip()):
                    continue
                if r["property"] in _PERSON_KEYS:
                    if etype in PERSON_TYPES:
                        addresses.setdefault(r["object_id"], []).append(r["value"].strip())
                    continue
                if r["property"] in name_props:
                    names.setdefault(r["object_id"], []).append(
                        (name_props.index(r["property"]), r["value"].strip(), r["markings"]))
                else:
                    attrs.setdefault(r["object_id"], []).append(
                        (r["property"], r["value"].strip(), r["markings"]))
            ordered = {oid: [(v, m) for _, v, m in sorted(found)]   # declared order
                       for oid, found in names.items()}
            idents: dict[str, list[Any]] = {}
            for r in conn.execute(
                    "SELECT object_id, identifier_kind, normalized, raw, role, markings FROM "
                    "identifiers WHERE object_id IN (SELECT value FROM json_each(?)) AND "
                    "superseded_at IS NULL ORDER BY role DESC, identifier_kind, normalized",
                    (ids,)):
                if r["identifier_kind"] not in hd.TECHNICAL:
                    idents.setdefault(r["object_id"], []).append(r)
            for row in rows:
                if not _cleared(row, etype, addresses.get(row["object_id"], []), exclusion,
                                counts):
                    continue
                entity = _entity(row, etype, latest.get(row["object_id"], "[]"),
                                 ordered.get(row["object_id"], []),
                                 idents.get(row["object_id"], []), counts,
                                 attrs.get(row["object_id"], []))
                if entity is None:
                    continue
                counts.chunks[f"entity:{etype}"] += 1
                yield entity
                produced += 1
                if limit is not None and produced >= limit:
                    return


def _cleared(row: Any, etype: str, addresses: list[str], exclusion: Any,
             counts: Counts) -> bool:
    if exclusion is not None and not exclusion.unit_clear(row["audience_key"]):
        counts.skip("people_exclusion:entity_unit")
        counts.withheld.add(row["audience_key"])
        return False
    if etype not in PERSON_TYPES:
        return True
    if exclusion is None or exclusion.unavailable or not addresses:
        counts.skip("people_exclusion:person_undecided")
        return False
    if not all(exclusion.identity_clear(a) for a in addresses):
        counts.skip("people_exclusion:person")
        return False
    return True


def attribute_words(prop: str, value: str) -> list[str]:
    """The alias words of one attribute value: the value, and for a place code or name
    the words a question uses ("CA" -> "canada canadian")."""
    out = [value]
    if prop in _COUNTRY_PROPS or prop == "location_type":
        words = PLACE_WORDS.get(value.strip().lower())
        if words:
            out.append(words)
    if prop == "website":
        host = re.sub(r"^(?:https?://)?(?:www\.)?", "", value.strip().lower()).split("/")[0]
        out = [host] if host else []
    if prop == "currency":
        out = [f"currency {value.strip().upper()}"]
    return out


def _entity(row: Any, etype: str, latest: str, names: list[tuple[str, str]],
            idents: list[Any], counts: Counts,
            attrs: list[tuple[str, str, str]] = ()) -> Entity | None:
    oid, audience = row["object_id"], row["audience_key"]
    if not valid_key(audience):
        counts.skip("entity_audience_key_unknown")
        return None
    base = parse_markings(row["markings"], latest)
    if base is None:
        counts.skip("entity_markings_unknown")
        return None
    marks = set(base)
    name = None
    for value, value_marks in names:
        held = parse_markings(value_marks)
        if held is None:
            continue
        name = value
        marks |= set(held)   # the row prints the name: a reader must hold its markings
        break
    alias: list[str] = [value for value, value_marks in names
                        if value != name and (held := parse_markings(value_marks)) is not None
                        and set(held) <= marks]
    for r in idents:
        held = parse_markings(r["markings"])
        if held is None or not set(held) <= marks:
            continue   # never raise a row to an identifier's markings; leave it out
        alias.extend([r["raw"], r["normalized"], hd.id_label(r["identifier_kind"],
                                                             r["normalized"])])
    if name is None:
        primary = next((r for r in idents if (h := parse_markings(r["markings"])) is not None
                        and set(h) <= marks), None)
        if primary is None:
            counts.skip(f"entity_unnamed:{etype}")
            return None
        name = hd.id_label(primary["identifier_kind"], primary["normalized"])
    for prop, value, value_marks in attrs:
        held = parse_markings(value_marks)
        if held is None or not set(held) <= marks:
            continue   # beyond the row's markings (a pii city): never an alias
        alias.extend(attribute_words(prop, value))
    folded = fold(name)
    short = _SUFFIX.sub("", folded).strip()
    if short and short != folded:
        alias.append(short)
    alias = [a for a in dict.fromkeys(a.strip() for a in alias if isinstance(a, str))
             if a and a != name]
    return Entity(entity_id=f"e:{oid}", object_id=oid, type=etype, name=clean(name),
                  alias=tuple(clean(a) for a in alias[:40]), normalized=folded, audience_key=audience,
                  markings=tuple(sorted(marks)))


__all__ = ["ATTRIBUTE_PROPS", "ENTITY_TYPES", "Entity", "attribute_words", "entities", "fold"]
