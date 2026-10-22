"""One identifier grammar for the Workbench (``ontology/schema/identifiers.json``).

The schema file is the single source of every identifier pattern: business
identifier kinds (pattern, normaliser, alias kinds, carrying fields per system
with the upstream code that writes them), the mention profiles that find
identifiers in text, the technical identifier grammars of the live sources
(GUIDs, UPNs, Graph ids, BC number literals) and the cross-system keys declared
as link bindings. This module loads and compiles it and adds the normalisers.

What it decides, and what it never decides:

* ``parse`` / ``format`` / ``normalize`` turn one written form into a canonical
  value (``PO#5447``, ``PO 5447``, ``Our PO 5447``, ``P.O. # 5447`` ->
  ``PO5447``). A normalised value finds candidates; it is never an identity.
  An exact match stays the literal comparison the owning system makes (Portal
  ``TRIM(x) ILIKE TRIM($1)``), and orders, POs, AOs and invoices never
  auto-merge (``identity_rules`` in the schema).
* ``identity_key`` is the identity one value names. A kind whose grammar declares
  ``identity_scope`` is unique only within that scope (``customer_po``: per customer,
  as the Portal matches a PO only with its customer), so its key carries the scope
  and a value without one names no identity. A key states equality of identity for
  a reviewer; it never merges anchors.
* ``copy_key`` is the content one captured record holds, for a kind whose grammar
  declares ``copies`` (``internet_message_id``: one message delivered to several
  mailboxes; ``content_sha256``: one file attached to a mail, stored in SharePoint or
  packed in a container). Records with one copy key are copies: evidence they carry
  counts once, while each copy keeps its own record, citation and permissions (never a
  merge). A value its sender writes (a Message-ID) is no proof of one content by itself:
  the carrier names the fields (``copy_content``) whose equal values the copies must also
  hold, so a forged or reused Message-ID groups nothing it does not copy.
* ``blocking_key`` is a lossier key for leads (``petra_po``: digits without
  the ``PO``/``UAT`` prefix or leading zeros).
* ``find_identifiers`` scans untrusted text and returns spans, kinds and
  normalised values. It is deterministic: a fixed profile order, then overlap
  resolution by position, length and label priority. Every pattern is
  anchored by fixed literals with bounded repetition (no nested quantifiers
  over the same characters), so the scan is linear in the text length
  (``ontology/tests/test_identifiers.py`` measures it).
* A kind whose value pattern is kind-specific rejects generic text shapes; a kind
  whose values have no fixed format upstream (customer free text, a configurable BC
  number series, several writers) says so in ``no_fixed_format`` (why, upstream code)
  instead of an invented pattern, and its value pattern stays the stored-value bound.
* ``system_pattern`` / ``valid`` serve the closed request grammar of the live
  sources (``integrations/live_source_grammar.py``) and other strict id checks.
* Leads, never identity: ``same_number`` (a kind's declared ``same_number``
  rule: ``PO#7100``, a bare stored ``7100`` and ``PO#7100-FM9`` hold the same
  number; ``ontology/resolve.py`` ``SameNumberIndex`` finds such twins among values
  read, ``same_number_prefixes`` where to read them in a canonical index),
  ``prefix_leads`` (a kind's declared ``prefix_forms``: a day prefix
  ``AO-26042`` leads to every AO of that day, both spellings of the day),
  ``marked_value`` / ``lead_key`` (a kind's declared marked form: ``PO#PO-6000`` leads to
  the customer POs whose blocking key, spaces dropped, ``PO-6000`` shares) and
  ``document_form`` (what a structured document number names: ``PLX-AO-AO-26197B``
  reads the AO ``AO-26197B``).

Stdlib only; no I/O except reading the schema file once.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

FORMAT = "plx-identifiers/v1"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "identifiers.json"
_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
_MAX_VALUE = 200
# Literal comparisons a kind's exact match may use (identity_rules.exact_compare).
EXACT_COMPARES = ("trim", "trim_casefold", "normalized")
# What a carrier's ``scope_within`` may qualify its scope value by (identity_rules).
SCOPE_WITHIN = ("company",)


class IdentifierSchemaError(ValueError):
    """The identifier schema file is missing, of another format or malformed."""


def _flags(text: str) -> int:
    value = 0
    for char in text or "":
        if char not in _FLAGS:
            raise IdentifierSchemaError(f"unknown regex flag {char!r}")
        value |= _FLAGS[char]
    return value


@lru_cache(maxsize=1)
def schema() -> Mapping[str, Any]:
    """The parsed identifier schema (``format`` must be ``plx-identifiers/v1``)."""
    try:
        data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IdentifierSchemaError(f"identifier schema unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise IdentifierSchemaError(f"identifier schema format must be {FORMAT}")
    for name in ("kinds", "system_kinds", "mention_profiles", "link_bindings"):
        if name not in data:
            raise IdentifierSchemaError(f"identifier schema lacks {name}")
    return data


@dataclass(frozen=True)
class ProfilePattern:
    """One compiled mention pattern of a profile."""
    kind: str
    pattern: re.Pattern[str]
    group: int | str
    label_group: int | str | None = None
    confidence: str = "literal"


@dataclass(frozen=True)
class Parsed:
    """A parsed identifier: its kind, canonical parts and source text."""
    kind: str
    parts: tuple[tuple[str, str], ...]
    text: str

    def part(self, name: str) -> str:
        return dict(self.parts).get(name, "")


@dataclass(frozen=True)
class Mention:
    """An identifier found in text. ``start``/``end`` delimit the value as
    written; ``label_start`` is where its label begins (equal to ``start``
    for an unlabelled shape). ``normalized`` is None when the value does not
    parse as its kind (it stays a literal mention)."""
    kind: str
    start: int
    end: int
    text: str
    normalized: str | None
    form: str
    label_start: int
    profile: str
    confidence: str = "literal"
    label: str | None = field(default=None)
    # Readings of the same value by another kind's self-delimiting shape, kept when
    # this mention's label assigned the value to a different kind (a supplier's
    # "Customer PO: PO100234" names the Petra PO it was sent). Each secondary
    # reading has ``label_kind_conflict`` set; it is never the primary reading.
    secondary: tuple["Mention", ...] = field(default=())
    label_kind_conflict: bool = False


def _value_pattern(spec: Mapping[str, Any], prefixes: str) -> str:
    """A kind's value grammar. An ``environment_prefixed`` kind also accepts its value
    behind any declared environment reference prefix (schema ``environment_prefixes``:
    the one list of prefixes), kept as part of the value."""
    pattern = spec["value_pattern"]
    if spec.get("environment_prefixed") and prefixes:
        return f"(?:{prefixes})?(?:{pattern})"
    return pattern


class _Compiled:
    def __init__(self, data: Mapping[str, Any]) -> None:
        self.kinds: dict[str, Mapping[str, Any]] = dict(data["kinds"])
        for name, spec in self.kinds.items():
            if spec.get("exact_compare") not in EXACT_COMPARES:
                raise IdentifierSchemaError(f"kind {name!r}: exact_compare must be one of "
                                            f"{', '.join(EXACT_COMPARES)}")
            if spec.get("normalizer") == "prefixed_int" and not (
                    isinstance(spec.get("id_prefix"), str) and spec["id_prefix"].strip()):
                raise IdentifierSchemaError(f"kind {name!r}: the prefixed_int normalizer "
                                            "needs id_prefix")
            free = spec.get("no_fixed_format")
            if free is not None and not (
                    isinstance(free, Mapping)
                    and all(isinstance(free.get(key), str) and free[key].strip()
                            for key in ("why", "upstream"))):
                raise IdentifierSchemaError(f"kind {name!r}: no_fixed_format needs why and "
                                            "upstream")
            alias = spec.get("reference_alias")
            if alias is not None and not (
                    isinstance(alias, Mapping) and isinstance(alias.get("of"), list)
                    and alias["of"] and all(isinstance(t, str) and t for t in alias["of"])
                    and all(isinstance(alias.get(key), str) and alias[key].strip()
                            for key in ("why", "upstream"))):
                raise IdentifierSchemaError(f"kind {name!r}: reference_alias needs of (object "
                                            "types), why and upstream")
            copies = spec.get("copies")
            if copies is not None and not (
                    isinstance(copies, Mapping)
                    and all(isinstance(copies.get(key), str) and copies[key].strip()
                            for key in ("why", "upstream"))):
                raise IdentifierSchemaError(f"kind {name!r}: copies needs why and upstream")
            for carrier in spec.get("carriers", ()):
                content = carrier.get("copy_content")
                if content is not None and not (
                        copies is not None and isinstance(content, list) and content
                        and all(isinstance(field, str) and field for field in content)):
                    raise IdentifierSchemaError(f"kind {name!r}: copy_content names the "
                                                "fields a carrier's copies hold equal, for a "
                                                "kind that declares copies")
            scope = spec.get("identity_scope")
            if scope is not None and not (
                    isinstance(scope, Mapping)
                    and all(isinstance(scope.get(key), str) and scope[key].strip()
                            for key in ("party", "why", "upstream"))
                    and all(isinstance(carrier.get("scope_field", ""), str)
                            for carrier in spec.get("carriers", ()))):
                raise IdentifierSchemaError(f"kind {name!r}: identity_scope needs party, why "
                                            "and upstream, and a carrier's scope_field is a "
                                            "field name")
            for carrier in spec.get("carriers", ()):
                within, excludes = carrier.get("scope_within"), carrier.get("scope_excludes")
                if (within is not None or excludes is not None) and not carrier.get("scope_field"):
                    raise IdentifierSchemaError(f"kind {name!r}: scope_within and "
                                                "scope_excludes qualify a scope_field")
                if within not in (None, *SCOPE_WITHIN):
                    raise IdentifierSchemaError(f"kind {name!r}: scope_within must be one of "
                                                f"{', '.join(SCOPE_WITHIN)}")
                if excludes is not None and not (
                        isinstance(excludes, Mapping) and isinstance(excludes.get("field"), str)
                        and excludes["field"] and excludes.get("values")
                        and all(isinstance(v, str) for v in excludes["values"])
                        and isinstance(excludes.get("upstream"), str)):
                    raise IdentifierSchemaError(f"kind {name!r}: scope_excludes needs field, "
                                                "values and upstream")
        prefixes = "|".join(re.escape(str(entry["prefix"]))
                            for entry in data.get("environment_prefixes", ()))
        self.values: dict[str, re.Pattern[str]] = {
            name: re.compile(_value_pattern(spec, prefixes), _flags(spec.get("flags", "")))
            for name, spec in self.kinds.items()}
        self.systems: dict[str, re.Pattern[str]] = {
            name: re.compile(spec["pattern"], _flags(spec.get("flags", "")))
            for name, spec in data["system_kinds"].items()}
        self.profiles: dict[str, tuple[ProfilePattern, ...]] = {}
        for name, profile in data["mention_profiles"].items():
            self.profiles[name] = tuple(
                ProfilePattern(entry["kind"],
                               re.compile(entry["pattern"], _flags(entry.get("flags", ""))),
                               entry.get("group", 1), entry.get("label_group"),
                               entry.get("confidence", "literal"))
                for entry in profile["patterns"])
        self.label_kinds: dict[str, str] = dict(data.get("label_kind_map", {}))
        self.environment = tuple(str(entry["prefix"]).upper()
                                 for entry in data.get("environment_prefixes", ()))
        self.prefix_forms: dict[str, tuple[tuple[Mapping[str, Any], re.Pattern[str],
                                                 re.Pattern[str]], ...]] = {
            name: tuple((form, re.compile(form["pattern"], _flags(spec.get("flags", ""))),
                         re.compile(form["rest_pattern"], _flags(spec.get("flags", ""))))
                        for form in spec.get("prefix_forms", ()))
            for name, spec in self.kinds.items()}
        self.document_forms: dict[str, tuple[tuple[Mapping[str, Any], re.Pattern[str]], ...]] = {
            name: tuple((form, re.compile(form["pattern"], _flags(spec.get("flags", ""))))
                        for form in spec.get("forms", ()) if "pattern" in form)
            for name, spec in self.kinds.items()}
        # A declared marked form's marker, matched case-insensitively at the start.
        self.markers: dict[str, tuple[re.Pattern[str], ...]] = {
            name: tuple(re.compile(form["marker"], re.IGNORECASE)
                        for form in spec.get("forms", ()) if "marker" in form)
            for name, spec in self.kinds.items()}


@lru_cache(maxsize=1)
def _compiled() -> _Compiled:
    return _Compiled(schema())


# ------------------------------------------------------------------ lookups

def kinds() -> tuple[str, ...]:
    """Every business identifier kind, in schema order."""
    return tuple(_compiled().kinds)


def kind_spec(kind: str) -> Mapping[str, Any]:
    try:
        return _compiled().kinds[kind]
    except KeyError:
        raise KeyError(f"unknown identifier kind {kind!r}") from None


def alias_kinds(kind: str) -> tuple[str, ...]:
    return tuple(kind_spec(kind).get("alias_kinds", ()))


def reference_alias_of(kind: str) -> tuple[str, ...]:
    """The object types a value of ``kind`` may be proposed as an alias of (schema
    ``reference_alias.of``: a supplier's own number naming the PO it confirms); empty for
    a kind that declares none."""
    return tuple((kind_spec(kind).get("reference_alias") or {}).get("of", ()))


def alias_of_kinds(kind: str) -> tuple[str, ...]:
    """The kinds that declare ``kind`` as one of their ``alias_kinds`` (the reverse of
    ``alias_kinds``), in schema order."""
    return tuple(other for other in kinds() if kind in alias_kinds(other))


def search_keys(kind: str, normalized: str, raw: Any,
                grammar: Any = None) -> list[tuple[str, str]]:
    """(kind, normalized) pairs one identifier is searched under: its own kind, its
    alias kinds and the kinds it is an alias of, each normalised by its own normaliser
    from the ``raw`` text. ``grammar`` (default this module) is the host's grammar
    (``kinds``, ``kind_spec``, ``normalize``). A kind whose normaliser refuses the form
    or fails is left out, and an unknown ``kind`` is searched as itself only."""
    grammar = grammar if grammar is not None else _MODULE
    keys = [(kind, normalized)]
    try:
        related = list(grammar.kind_spec(kind).get("alias_kinds", ()))
    except Exception:  # noqa: BLE001 - unknown kind: itself only
        return keys
    for other in grammar.kinds():
        try:
            if kind in grammar.kind_spec(other).get("alias_kinds", ()):
                related.append(other)
        except Exception:  # noqa: BLE001 - a kind the grammar cannot describe
            continue
    for other in related:
        try:
            value = grammar.normalize(other, raw)
        except Exception:  # noqa: BLE001 - the normaliser refuses this form
            value = None
        if isinstance(value, str) and value and (other, value) not in keys:
            keys.append((other, value))
    return keys


def carriers(kind: str) -> tuple[Mapping[str, Any], ...]:
    """The fields that carry ``kind`` per system (with their upstream citation)."""
    return tuple(kind_spec(kind).get("carriers", ()))


def carrier_kind(relation: Any, field: Any) -> str | None:
    """The one kind whose ``carriers`` declare ``relation``'s ``field`` (its upstream
    meaning, cited per carrier), or None: no kind or more than one declares it."""
    found = {kind for kind in kinds() for carrier in carriers(kind)
             if carrier.get("relation") == relation and carrier.get("field") == field}
    return next(iter(found)) if len(found) == 1 else None


def identity_scope(kind: str, relation: Any, field: Any) -> tuple[str, str] | None:
    """(scope space, scope field) bounding ``kind``'s identity on the carrier
    ``relation``.``field``: the field of the same record that holds the scope (the
    carrier's ``scope_field``) and the space its values belong to (one system's
    column; values of two spaces never equal). None when the kind declares no
    ``identity_scope`` or the carrier names no scope field."""
    if kind not in _compiled().kinds or not kind_spec(kind).get("identity_scope"):
        return None
    for carrier in carriers(kind):
        if carrier.get("relation") == relation and carrier.get("field") == field:
            name = carrier.get("scope_field")
            return (f'{carrier["system"]}:{relation}.{name}', name) if name else None
    return None


def scope_rules(kind: str, relation: Any, field: Any) -> Mapping[str, Any]:
    """How the carrier ``relation``.``field`` of a scoped ``kind`` qualifies its scope:
    ``within`` (``company``: the scope value holds within the record's company, as BC
    customer numbers repeat across companies; None: as stored) and ``excludes`` ((field,
    values): a record whose field holds one of them never holds the value, as the Portal
    skips a cancelled order; None: no record is excluded). Empty for any other carrier."""
    if kind not in _compiled().kinds or not kind_spec(kind).get("identity_scope"):
        return {}
    for carrier in carriers(kind):
        if carrier.get("relation") == relation and carrier.get("field") == field:
            excludes = carrier.get("scope_excludes")
            return {"within": carrier.get("scope_within"),
                    "excludes": (excludes["field"], frozenset(excludes["values"]))
                    if excludes else None}
    return {}


def identity_key(kind: str, value: Any, scope: Any = None) -> tuple[str, ...] | None:
    """The identity one value of ``kind`` names, or None when it names none.

    A kind whose grammar declares ``identity_scope`` is unique only within that scope
    (``customer_po``: per customer, Portal ``resolveOrderByNumber`` matches customer plus
    normalised PO): ``scope`` is (space, value) from ``identity_scope`` and the record,
    the key is (space, scope value, normalised value), and a missing or blank scope names
    no identity, as the Portal skips the PO match without a customer. Any other kind:
    (normalised value,). An unknown kind or an unparseable value names none."""
    if kind not in _compiled().kinds:
        return None
    normalized = normalize(kind, value)
    if normalized is None:
        return None
    if not kind_spec(kind).get("identity_scope"):
        return (normalized,)
    if not isinstance(scope, tuple) or len(scope) != 2:
        return None
    space, raw = scope
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    if not isinstance(space, str) or not space or not isinstance(raw, str) or not raw.strip():
        return None
    return (space, raw.strip(), normalized)


def copy_key(system: str | None, relation: str, record: Mapping[str, Any]
             ) -> tuple[str, ...] | None:
    """The content a captured ``record`` of ``system``'s ``relation`` holds: (kind,
    normalised value) of the first kind, in schema order, that declares ``copies`` and a
    carrier on that relation whose field the record holds in a valid form, plus the
    SHA-256 of the record's ``copy_content`` fields when the carrier names them (a
    sender-written value is no proof of one content: a record whose fields differ is no
    copy). ``system`` None: a relation every system serves alike (container members), any
    of its carriers. None when no such kind or value exists (the record is then a copy of
    nothing else: counted on its own, never grouped by a guess)."""
    if not isinstance(record, Mapping):
        return None
    for kind in kinds():
        if not kind_spec(kind).get("copies"):
            continue
        for carrier in carriers(kind):
            if system not in (None, carrier.get("system")) \
                    or carrier.get("relation") != relation:
                continue
            normalized = normalize(kind, record.get(carrier.get("field")))
            if normalized is None:
                continue
            content = carrier.get("copy_content")
            if content is None:
                return kind, normalized
            held = json.dumps([record.get(name) for name in content], sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False, default=str)
            return kind, normalized, sha256(held.encode("utf-8")).hexdigest()
    return None


def link_bindings() -> tuple[Mapping[str, Any], ...]:
    return tuple(schema()["link_bindings"])


def environment_prefix(value: Any) -> str | None:
    """The declared environment reference prefix ``value`` starts with (``UAT``
    for ``UAT-SH100017``; schema ``environment_prefixes``), else None. A diagnosis
    of where a number was issued, never a normaliser: matches keep the prefix."""
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    for entry in schema().get("environment_prefixes", ()):
        prefix = str(entry["prefix"]).upper()
        if text.startswith(prefix) and len(text) > len(prefix):
            return prefix.rstrip("-_ ")
    return None


def sent_rule(kind: str, relation: str, field: str, system: str) -> Mapping[str, Any] | None:
    """The ``sent_to`` rule for ``system`` that the carrier of ``kind`` at
    ``relation``.``field`` declares, or None (such a value reaches ``system`` as held)."""
    for carrier in carriers(kind):
        if carrier.get("relation") != relation or carrier.get("field") != field:
            continue
        for rule in carrier.get("sent_to", ()):
            if rule.get("system") == system:
                return rule
    return None


def sent_form(kind: str, relation: str, field: str, value: Any, system: str) -> str | None:
    """The form in which ``system`` receives ``value`` held at ``relation``.``field`` (a
    carrier of ``kind``), by that carrier's declared ``sent_to`` rule for ``system``
    (``sent_rule``): None when the owning system never sends such a value there (its
    ``when_pattern`` is not found in it), else the trimmed value cut to ``max_length``. A
    carrier without a rule for ``system`` sends the trimmed value as held; a blank value
    is None."""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    rule = sent_rule(kind, relation, field, system)
    if rule is None:
        return text
    if rule.get("when_pattern") and not re.search(rule["when_pattern"], text):
        return None
    return text[:rule["max_length"]] if rule.get("max_length") else text


def system_pattern(name: str) -> re.Pattern[str]:
    """The compiled technical identifier grammar ``name``; use ``fullmatch``."""
    try:
        return _compiled().systems[name]
    except KeyError:
        raise KeyError(f"unknown system identifier {name!r}") from None


def valid(name: str, value: Any) -> bool:
    """Whether ``value`` is a string that fully matches the grammar ``name``
    (a system identifier, else a business kind's value pattern)."""
    if not isinstance(value, str):
        return False
    compiled = _compiled()
    pattern = compiled.systems.get(name) or compiled.values.get(name)
    if pattern is None:
        raise KeyError(f"unknown identifier grammar {name!r}")
    return pattern.fullmatch(value) is not None


def profile(name: str) -> tuple[ProfilePattern, ...]:
    try:
        return _compiled().profiles[name]
    except KeyError:
        raise KeyError(f"unknown mention profile {name!r}") from None


def profile_patterns(name: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """``(kind, compiled pattern)`` pairs of a profile, in schema order."""
    return tuple((entry.kind, entry.pattern) for entry in profile(name))


def profile_pattern(name: str) -> re.Pattern[str]:
    """The single compiled pattern of a one-pattern profile."""
    entries = profile(name)
    if len(entries) != 1:
        raise KeyError(f"mention profile {name!r} has {len(entries)} patterns")
    return entries[0].pattern


# ------------------------------------------------------------------ normalisers

def _ascii_digits(text: str) -> bool:
    return bool(text) and all("0" <= c <= "9" for c in text)


def _strip_zeros(digits: str) -> str:
    return digits.lstrip("0") or "0"


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _int_text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else None
    text = str(value).strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number != number.to_integral_value():
        return None
    return str(int(number))


def _split_fm_suffix(text: str) -> tuple[str, str | None]:
    upper = text.upper()
    index = upper.rfind("-FM")
    if index > 0 and _ascii_digits(text[index + 3:]):
        return text[:index], text[index + 3:]
    return text, None


def fm_suffix_base(value: str) -> str | None:
    """The number before a Portal ``-FM<fmPoId>`` clash suffix
    (create-fm-purchase-order.ts:279-291), else None."""
    if not isinstance(value, str):
        return None
    index = value.upper().rfind("-FM")
    if index > 0 and value[index + 3:].isdecimal():
        return value[:index]
    return None


def _petra_po_parts(text: str) -> tuple[tuple[str, str], ...] | None:
    upper = _collapse(text).upper()
    if upper.startswith("OUR "):
        upper = upper[4:]
    uat = upper.startswith("UAT-")
    if uat:
        upper = upper[4:]
    body, fm = _split_fm_suffix(upper)
    digits_at = len(body)
    while digits_at > 0 and "0" <= body[digits_at - 1] <= "9":
        digits_at -= 1
    head, digits = body[:digits_at], body[digits_at:]
    if digits and not head and not uat:
        # A bare stored number (Sage / FM ponumber): series-less, never the PO series.
        parts = [("series", ""), ("number", _strip_zeros(digits))]
        return tuple(parts + ([("fm", _strip_zeros(fm))] if fm is not None else []))
    series = "DPO" if head.startswith("DPO") else "PO"
    if digits and _numeric_head(head):
        parts = [("series", series), ("number", _strip_zeros(digits))]
        if fm is not None:
            parts.append(("fm", _strip_zeros(fm)))
        if uat:
            parts.append(("uat", "UAT"))
        return tuple(parts)
    if fm is None and head.startswith("PO-"):
        # Legacy PO-{base 36 time} (operations route): no DPO- form, at most 8 characters.
        code = body[3:]
        if code.isalnum() and 3 <= len(code) <= 8:
            parts = [("series", "PO"), ("code", code)]
            if uat:
                parts.append(("uat", "UAT"))
            return tuple(parts)
    return None


def _numeric_head(head: str) -> bool:
    """The label before the number: a PO word and separators only."""
    rest = head
    for word in ("PURCHASE ORDER", "DPO", "PO", "P.O.", "P. O.", "P.O", "P. O", "P O"):
        if rest.startswith(word):
            rest = rest[len(word):]
            break
    else:
        return False
    rest = rest.strip(" ")
    for word in ("NUMBER", "NUM.", "NUM", "NO.", "NO"):
        if rest.startswith(word):
            rest = rest[len(word):]
            break
    return all(c in " #:.-" for c in rest)


def petra_po_lead_key(value: Any) -> str | None:
    """Comparison key for leads only: ASCII alphanumerics, upper case, without the
    UAT prefix, a leading ``PO`` and leading zeros (``PO 5447``, ``UAT-PO#05447``,
    ``PO5447`` -> ``5447``); None when shorter than 3. Never an exact match."""
    if not isinstance(value, str):
        return None
    key = "".join(c for c in value.upper() if "A" <= c <= "Z" or "0" <= c <= "9")
    if key.startswith("UAT"):
        key = key[3:]
    if key.startswith("PO"):
        key = key[2:]
    key = key.lstrip("0")
    return key if len(key) >= 3 else None


def petra_po_shape(value: Any) -> str:
    """A value-free shape class of a PO token (for counts-only diagnostics):
    ``canonical_series`` (PO/DPO 100000+), ``legacy_number``, ``fm_suffixed``,
    ``base36``, ``uat``, ``bare_digits`` (a labelled number) or ``other``."""
    if not isinstance(value, str):
        return "other"
    text = value.strip()
    if _ascii_digits(text):
        return "bare_digits"
    parsed = parse("petra_po", text)
    if parsed is None:
        base, fm = _split_fm_suffix(text)
        return "fm_suffixed" if fm is not None and _ascii_digits(base) else "other"
    if parsed.part("uat"):
        return "uat"
    if parsed.part("fm"):
        return "fm_suffixed"
    if parsed.part("code"):
        return "base36"
    number = int(parsed.part("number"))
    return "canonical_series" if number >= 100000 else "legacy_number"


def _customer_po_core(text: str) -> str:
    upper = _collapse(text).upper()
    for qualifier in ("CUSTOMER ", "CUST. ", "CUST ", "CLIENT "):
        if upper.startswith(qualifier):
            upper = upper[len(qualifier):]
            break
    marker = None
    for word in ("PURCHASE ORDER", "P.O.", "P/O", "P.O", "PO"):
        if upper.startswith(word) and not upper[len(word):len(word) + 1].isalpha():
            marker = word
            break
    if marker is None and upper.startswith("#"):
        marker = "#"
    rest = upper[len(marker):] if marker else upper
    if marker and marker != "#":
        rest = rest.lstrip(" .")
        for word in ("NUMBER", "NO.", "NO", "#"):
            if rest.startswith(word) and not rest[len(word):len(word) + 1].isalpha():
                rest = rest[len(word):]
                break
        rest = rest.lstrip(" :#.")
    groups = rest.split()
    if marker or len(groups) <= 1:
        return "".join(groups)
    return rest


# What a lot field says when there is no lot (read case-blind, whole value): no lot reference.
LOT_PLACEHOLDERS = frozenset({"N/A", "NA", "NONE", "-"})


def placeholder(kind: str, value: Any) -> bool:
    """Whether ``value`` is a field's stand-in for no value of ``kind`` (a lot field's
    "N/A"): no identifier at all, never a malformed one."""
    try:
        normalizer = kind_spec(kind)["normalizer"]
    except KeyError:
        return False
    return (normalizer == "lot_number" and isinstance(value, str)
            and value.partition("@")[0].strip().upper() in LOT_PLACEHOLDERS)


_NORMALIZERS = ("upper_trim", "lower_trim", "trim", "compact_upper", "int_text",
                "bc_item_number", "external_id", "lot_number", "petra_po",
                "customer_po", "pi_ref", "message_id", "prefixed_int")


def parse(kind: str, value: Any) -> Parsed | None:
    """Parse one written value of ``kind``; None when it is not a valid form."""
    spec = kind_spec(kind)
    normalizer = spec["normalizer"]
    if normalizer == "prefixed_int":
        return _prefixed_int(kind, spec, value)
    if normalizer == "int_text" and isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _int_text(value)
        return None if number is None else Parsed(kind, (("value", number),), str(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_VALUE or _compiled().values[kind].fullmatch(text) is None:
        return None
    if normalizer == "petra_po":
        parts = _petra_po_parts(text)
        return None if parts is None else Parsed(kind, parts, value)
    if normalizer == "pi_ref":
        body = text.upper()[2:].lstrip("-")
        letters = "".join(c for c in body if c.isalpha())
        digits = body[len(letters):]
        return Parsed(kind, (("letters", letters), ("number", digits)), value)
    if normalizer == "lot_number":
        base, _, bin_code = text.partition("@")
        if base.strip().upper() in LOT_PLACEHOLDERS:
            return None   # a stand-in for "no lot", never a lot
        parts = [("base", base.strip().upper())]
        if bin_code:
            parts.append(("bin", bin_code.strip().upper()))
        return Parsed(kind, tuple(parts), value)
    if normalizer == "external_id":
        lower = text.lower()
        for prefix in ("local-cust-", "sage-cust-"):
            if lower.startswith(prefix):
                rest = text[len(prefix):]
                return Parsed(kind, (("prefix", prefix),
                                     ("id", rest.lower() if prefix == "local-cust-" else rest.upper())),
                              value)
        entity = lower.split("-")[1]
        return Parsed(kind, (("prefix", f"fm-{entity}-"), ("id", text[len(entity) + 4:].upper())),
                      value)
    if normalizer == "int_text":
        number = _int_text(text)
        return None if number is None else Parsed(kind, (("value", number),), value)
    if normalizer == "customer_po":
        return Parsed(kind, (("value", _collapse(text).upper()),), value)
    if normalizer == "message_id":
        # RFC 5322 msg-id: the angle brackets delimit the id, they are not part of it.
        bare = text[1:-1] if text.startswith("<") and text.endswith(">") else text
        return Parsed(kind, (("value", bare),), value)
    if normalizer == "upper_trim":
        return Parsed(kind, (("value", _collapse(text).upper()),), value)
    if normalizer == "lower_trim":
        return Parsed(kind, (("value", text.lower()),), value)
    if normalizer == "trim":
        return Parsed(kind, (("value", text),), value)
    if normalizer == "compact_upper":
        return Parsed(kind, (("value", "".join(c for c in text.upper() if c not in " -")),),
                      value)
    if normalizer == "bc_item_number":
        # The whole code: BC's 20-character cut is the form it is sent in (sent_form),
        # read only where a value is compared with a BC item No., never its identity.
        return Parsed(kind, (("value", text.upper()),), value)
    raise IdentifierSchemaError(f"unknown normalizer {normalizer!r} for {kind}")


def _prefixed_int(kind: str, spec: Mapping[str, Any], value: Any) -> Parsed | None:
    """A non-negative integer record id held bare (a number or its integral text) or
    behind the kind's ``id_prefix`` (``fm-supp-7``, case-insensitive): both forms are one
    key, the bare integer text, so a prefixed stored key compares with the bare id."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = _int_text(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > _MAX_VALUE or \
                _compiled().values[kind].fullmatch(text) is None:
            return None
        prefix = str(spec["id_prefix"])
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]
        number = _int_text(text)
    else:
        return None
    if number is None or number.startswith("-"):
        return None
    return Parsed(kind, (("value", number),), str(value))


def format(parsed: Parsed) -> str:  # noqa: A001 - the grammar's own verb
    """The canonical written form of a parsed identifier."""
    normalizer = kind_spec(parsed.kind)["normalizer"]
    part = parsed.part
    if normalizer == "petra_po":
        prefix = "UAT-" if part("uat") else ""
        if part("code"):
            return f"{prefix}{part('series')}-{part('code')}"
        suffix = f"-FM{part('fm')}" if part("fm") else ""
        return f"{prefix}{part('series')}{part('number')}{suffix}"
    if normalizer == "pi_ref":
        return f"PI-{part('letters')}{part('number')}"
    if normalizer == "lot_number":
        return part("base") + (f"@{part('bin')}" if part("bin") else "")
    if normalizer == "external_id":
        return part("prefix") + part("id")
    return part("value")


def normalize(kind: str, value: Any) -> str | None:
    """``format(parse(value))``: the canonical form, or None when not a valid form."""
    parsed = parse(kind, value)
    return None if parsed is None else format(parsed)


def exact_compare(kind: str | None, stored: Any, asked: Any) -> bool:
    """Whether a stored identifier text is an exact match for the asked text by the owning
    system's literal comparison for ``kind`` (``identity_rules.exact_compare``): ``trim``
    is case-sensitive equality after trimming both sides, ``trim_casefold`` ignores case
    too (Portal ``TRIM(x) ILIKE TRIM($1)`` without wildcards), ``normalized`` is equality of
    the kind's own normal form (Portal ``normalizePoReference`` on both sides: trimmed,
    inner whitespace collapsed, upper case). A kind the grammar does not
    declare compares ``trim``, the stricter rule. The one exact rule for ``plx.find`` and
    ``find_root``; a normalised match that fails it is never exact."""
    if not isinstance(stored, str) or not isinstance(asked, str):
        return False
    left, right = stored.strip(), asked.strip()
    if not left or not right:
        return False
    mode = _compiled().kinds.get(kind, {}).get("exact_compare", "trim") \
        if isinstance(kind, str) else "trim"
    if mode == "trim_casefold":
        return left.casefold() == right.casefold()
    if mode == "normalized":
        normal = normalize(kind, left)
        return normal is not None and normal == normalize(kind, right)
    return left == right


def blocking_key(kind: str, value: Any) -> str | None:
    """A lossy key that groups spellings of one identifier for candidate search
    (leads only). ``petra_po`` accepts a bare labelled number too."""
    normalizer = kind_spec(kind)["normalizer"]
    if normalizer == "petra_po":
        return petra_po_lead_key(normalize(kind, value) or value)
    parsed = parse(kind, value)
    if parsed is None:
        return None
    if normalizer == "pi_ref":
        return _strip_zeros(parsed.part("number"))
    if normalizer == "lot_number":
        return parsed.part("base")
    if normalizer == "customer_po":
        core = _customer_po_core(parsed.part("value"))
        return core or None
    return format(parsed)


def lead_key(kind: str, value: Any) -> str | None:
    """``blocking_key`` without whitespace: one lead key for every spacing of the digit
    groups (``PO# 4500 12``, ``4500 12`` and ``PO 450012``). Leads only."""
    key = blocking_key(kind, value)
    key = "".join(key.split()) if key else None
    return key or None


def marked_value(kind: str, value: Any) -> str | None:
    """The value a text in ``kind``'s declared marked form names (a form's ``marker``:
    customer PO ``PO#PO-6000`` names ``PO-6000``, ``PO# 7000`` names ``7000``), normalised;
    None when the kind declares no marker, the text does not start with it or no valid
    value follows. A lead to that value, never that value: the owning system compares the
    whole text (Portal normalizePoReference keeps the marker)."""
    if not isinstance(value, str) or kind not in _compiled().kinds:
        return None
    text = value.strip()
    for marker in _compiled().markers[kind]:
        found = marker.match(text)
        if found is not None and found.end() < len(text):
            return normalize(kind, text[found.end():])
    return None


# ------------------------------------------------------------------ leads

def same_number_key(kind: str, value: Any) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """The kind's declared ``same_number`` key of ``value``: (the parts that must be
    equal, the parts a blank matches any value of); None when the kind declares no
    rule or the value lacks a required part (``petra_po``: a base36 ``PO-K3J9X2``)."""
    rule = kind_spec(kind).get("same_number")
    parsed = parse(kind, value) if rule else None
    if parsed is None or not all(parsed.part(name) for name in rule.get("required", ())):
        return None
    return (tuple(parsed.part(name) for name in rule["parts"]),
            tuple(parsed.part(name) for name in rule.get("blank_matches_any", ())))


def same_number(kind: str, first: Any, second: Any) -> bool:
    """Whether two values hold the same number under the kind's ``same_number`` rule
    (``PO#7100``, ``7100`` and ``PO-07100-FM9``; never ``DPO7100`` vs ``PO7100``,
    never ``UAT-PO7100`` vs ``PO7100``). Twins are leads, never one record."""
    a, b = same_number_key(kind, first), same_number_key(kind, second)
    return (a is not None and b is not None and a[0] == b[0]
            and all(x == y or "" in (x, y) for x, y in zip(a[1], b[1])))


def same_number_prefixes(kind: str, value: Any) -> tuple[str, ...]:
    """Canonical prefixes every stored same-number twin of ``value`` is written under:
    the rule's parts as asked, each ``blank_matches_any`` part as asked or blank and, when
    asked blank, as every value the kind's declared form examples write (``4821`` ->
    ``4821``, ``PO4821``, ``DPO4821``); any other part (an ``-FM<id>`` suffix) dropped. A
    superset for an index range scan: ``same_number`` decides (``PO48210`` is no twin)."""
    rule = kind_spec(kind).get("same_number")
    parsed = parse(kind, value) if same_number_key(kind, value) is not None else None
    if parsed is None:
        return ()
    blanks = tuple(rule.get("blank_matches_any", ()))
    declared = [parse(kind, form.get("example")) for form in kind_spec(kind).get("forms", ())]
    choices: list[list[str]] = []
    for name in blanks:
        asked = parsed.part(name)
        options = [asked, ""] if asked else [""] + sorted(
            {p.part(name) for p in declared if p is not None} - {""})
        choices.append(list(dict.fromkeys(options)))
    keep = set(rule["parts"]) | set(blanks)
    base = [(name, text) for name, text in parsed.parts if name in keep]
    out: list[str] = []
    combos: list[tuple[str, ...]] = [()]
    for options in choices:
        combos = [combo + (option,) for combo in combos for option in options]
    for combo in combos:
        chosen = dict(zip(blanks, combo))
        parts = tuple((name, chosen.get(name, text)) for name, text in base)
        written = format(Parsed(kind, parts, parsed.text))
        if written and written not in out:
            out.append(written)
    return tuple(out)


@dataclass(frozen=True)
class PrefixLead:
    """A value that is a declared prefix of ``kind`` (``prefix_forms``): every stored
    value that is one of ``prefixes`` followed by a ``rest`` is a lead, never one
    record. ``prefixes`` are canonical spellings, the one as typed first."""
    kind: str
    form: str
    prefixes: tuple[str, ...]
    rest: re.Pattern[str]

    def matches(self, stored: Any) -> bool:
        """Whether a stored value of ``kind`` is led to (compared in its canonical form)."""
        text = normalize(self.kind, stored)
        return text is not None and any(
            text.startswith(prefix) and self.rest.fullmatch(text[len(prefix):]) is not None
            for prefix in self.prefixes)


def prefix_leads(value: Any) -> tuple[PrefixLead, ...]:
    """Every kind of which the whole ``value`` is a declared prefix form (``AO-26042``:
    the AOs of day 42 of 2026, spelled ``AO-26042`` by FM and ``AO-2642`` by the Portal).
    An environment prefix the kind accepts is kept on every spelling."""
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_VALUE:
        return ()
    compiled = _compiled()
    text = _collapse(value).upper()
    out: list[PrefixLead] = []
    for kind, forms in compiled.prefix_forms.items():
        spec = compiled.kinds[kind]
        env = next((p for p in compiled.environment if spec.get("environment_prefixed")
                    and text.startswith(p) and len(text) > len(p)), "")
        for form, pattern, rest in forms:
            match = pattern.fullmatch(text[len(env):])
            if match is None:
                continue
            groups = {name: int(found) for name, found in match.groupdict().items()}
            if any(not low <= groups[name] <= high
                   for name, (low, high) in form.get("integer_ranges", {}).items()):
                continue
            prefixes = [env + text[len(env):]]
            for spelling in form["spellings"]:
                written = env + spelling.format(**groups)
                if written not in prefixes:
                    prefixes.append(written)
            out.append(PrefixLead(kind, form["form"], tuple(prefixes), rest))
    return tuple(out)


@dataclass(frozen=True)
class DocumentForm:
    """A structured number of ``kind`` in one declared form, and what it names:
    ``reads`` holds ``(part, kind or None, object_type or None, value)``; a None kind
    is a value that names no record (the day a document was built)."""
    kind: str
    form: str
    reads: tuple[tuple[str, str | None, str | None, str], ...]


def document_form(kind: str, value: Any) -> DocumentForm | None:
    """The declared form (``forms`` with a ``pattern``) a value of ``kind`` is written
    in, with the identifiers it embeds (``PLX-AO-AO-26197B`` -> ao_code ``AO-26197B``;
    ``PLX-FMAO-9035`` -> the assembly_order id 9035). A lead to the named record,
    never its identity; None when no declared form matches."""
    text = normalize(kind, value)
    if text is None:
        return None
    for form, pattern in _compiled().document_forms[kind]:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        reads = []
        for part, meaning in form.get("reads", {}).items():
            named = meaning.get("kind")
            found = match.group(part)
            reads.append((part, named, meaning.get("object_type"),
                          (normalize(named, found) or found) if named else found))
        return DocumentForm(kind, form["form"], tuple(reads))
    return None


# ------------------------------------------------------------------ scanner

_PROFILE_ORDER = (("mail_labelled", "labelled", 0), ("petra_po_labelled", "labelled", 0),
                  ("petra_po_attached", "shape", 1), ("pi_attached", "shape", 1),
                  ("shapes", "shape", 1))


def _mention_value(kind: str, value: str, label_text: str) -> str | None:
    if kind == "petra_po":
        if _ascii_digits(_split_fm_suffix(value)[0]):
            return normalize(kind, label_text) or normalize(kind, "PO" + value)
        return normalize(kind, value)
    return normalize(kind, value)


def find_identifiers(text: Any) -> list[Mention]:
    """Every identifier mention in ``text``: span of the value, kind, normalised
    value, form (``labelled`` / ``shape``) and the profile that found it.

    Labelled values must contain a digit. Overlapping mentions resolve
    deterministically: earlier start first, then the longer value, then a
    labelled mention before a bare shape (``Customer PO PO12172025`` is a
    customer PO, not a Petra PO). When a value's shape matches kind X but its
    label assigns kind Y, the label reading is the mention and the shape reading
    is kept in its ``secondary`` (``label_kind_conflict`` set): callers may treat
    it as a lead, never as the reading."""
    if not isinstance(text, str) or not text:
        return []
    compiled = _compiled()
    found: list[tuple[int, int, int, int, Mention]] = []
    order = 0
    for name, form, priority in _PROFILE_ORDER:
        for entry in compiled.profiles[name]:
            kind = compiled.label_kinds.get(entry.kind, entry.kind)
            for match in entry.pattern.finditer(text):
                value = match.group(entry.group).rstrip(".,;:")
                start = match.start(entry.group)
                end = start + len(value)
                if not value or (form == "labelled" and not any("0" <= c <= "9" for c in value)):
                    continue
                label_text = text[match.start():end]
                mention = Mention(kind, start, end, value, _mention_value(kind, value, label_text),
                                  form, match.start(), name, entry.confidence,
                                  text[match.start():start].strip() or None)
                found.append((start, -(end - start), priority, order, mention))
                order += 1
    found.sort(key=lambda item: item[:4])
    accepted: list[Mention] = []
    skipped: list[Mention] = []
    last_end = -1
    for start, _neg, _priority, _order, mention in found:
        if start < last_end:
            skipped.append(mention)
            continue
        accepted.append(mention)
        last_end = mention.end
    return _with_secondary(accepted, skipped)


def _with_secondary(accepted: list[Mention], skipped: Sequence[Mention]) -> list[Mention]:
    """Attach to each labelled mention the other kinds' readings of its value.

    A secondary reading is a skipped mention of another kind overlapping the
    labelled mention's value: a self-delimiting shape (``Customer PO:
    PO100234``) or another kind's labelled reading that its own grammar accepts
    (``Customer PO: 5280`` also reads ``PO: 5280``, a Petra PO form). Accepted
    mentions never overlap and are sorted by start, so each skipped mention
    finds the mentions it overlaps by bisection: O(n log n) in the mentions.
    One reading per kind and normalised value is kept (the earlier, longer one:
    ``PO100234`` read as a shape and as ``PO`` + ``100234`` is one reading).
    """
    ends = [mention.end for mention in accepted]
    readings: dict[int, list[Mention]] = {}
    seen: set[tuple[int, str, Any]] = set()  # one reading per kind and value
    for other in skipped:
        if other.form != "shape" and (other.form != "labelled" or other.normalized is None):
            continue
        index = bisect_right(ends, other.start)  # first accepted ending after its start
        while index < len(accepted) and accepted[index].start < other.end:
            mention = accepted[index]
            key = (index, other.kind, other.normalized or (other.start, other.end))
            if mention.form == "labelled" and other.kind != mention.kind and key not in seen:
                seen.add(key)
                readings.setdefault(index, []).append(replace(other, label_kind_conflict=True))
            index += 1
    return [replace(mention, secondary=tuple(readings[index])) if index in readings
            else mention for index, mention in enumerate(accepted)]


_MODULE = sys.modules[__name__]

__all__ = ["FORMAT", "SCHEMA_PATH", "IdentifierSchemaError", "Mention", "Parsed",
           "EXACT_COMPARES", "ProfilePattern", "DocumentForm", "PrefixLead", "alias_kinds",
           "reference_alias_of",
           "alias_of_kinds", "blocking_key", "carrier_kind", "carriers", "copy_key",
           "document_form",
           "exact_compare", "find_identifiers",
           "fm_suffix_base", "format", "identity_key", "identity_scope", "kind_spec", "kinds",
           "lead_key", "link_bindings", "marked_value", "normalize",
           "parse", "petra_po_lead_key", "petra_po_shape", "prefix_leads", "profile",
           "profile_pattern", "profile_patterns", "same_number", "same_number_key",
           "same_number_prefixes", "schema", "scope_rules",
           "search_keys", "system_pattern", "valid"]
