"""Conflicts between what a captured document attests and the records it names (P8).

A document class may declare what it ``attests`` about a record type
(``ontology/schema/document_fields.json``; :func:`ontology.document_fields.comparisons`):
a field compared with a property reached over declared link paths, with an event of the
record, or, for a keyed field (per-component damage), entry by entry through a key path.
``plx.object`` on either end (the record or the document) calls :func:`conflicts`, which
compares only what this reader may see now: links, objects and values pass the same
audience, marking, time and item gates as every other value of the answer.

A disagreement is a typed ``value_disagreement`` conflict listing the document's value
first and every compared record value after it; nothing is picked, merged or summed. A
field the paper leaves blank (extraction gap ``value_missing``) facing a stated record
value is a conflict too, its document value null: a blank is unknown, never zero or a
match. A field the document does not state, or states unreadably, is not compared. A compared
property the schema does not declare (on the document type or a reached record type) is a
``property_not_declared`` gap, never a silently skipped comparison; when only some reached
types lack it, the records of the declaring types are still compared (conflict, match or
lead) and the gap names the others. One gap several documents give is reported once, its
``count`` the number of those documents (absent: one).
Quantities with two different known units are not compared (not the same measure). A
code (an Incoterm) is compared only with a record value naming exactly one word of its
vocabulary, its synonyms folded into one code ('CFR (CNF)' is one Incoterm); one naming
none or several is a ``unreadable`` / ``several_values`` gap, never
a match (the document side's code is exactly one word already, else an extraction gap).
Money is compared only in one stated currency and on one declared tax basis (the document
field's ``tax_basis`` in the profile, the record value's from its schema source,
``ontology/units.py``): a pair in two or an unknown currency, or on two or an unknown tax
basis (a document total read without a stated basis against a BC total including tax), is
a ``currency_*`` / ``tax_basis_*`` gap, never a conflict and never a match; nothing is
converted. Every conflict is ``tier`` ``lead``, never authoritative. A record value cited by a ``bc://`` handle
names its Business Central company (``bc_companies``: the company GUID and the book its
lineage names); when every compared record value comes from Business Central (no Portal
row reached) the note says so.

A value the document states that the attested records hold empty is no conflict but a
``missing_in_record`` lead (REC-01): the document's value first (with the page and span
citations of its field, ``document_citations``, when the reader may read the
extraction), then every empty read of the property on the reached records. It is
reported only when this reader may read every value of that property there, none holds
a value, and each record has at least one field read as empty (a source null, or a
placeholder copy of the record's own number); a declared field the projection holds no
read of is listed (``fields_not_read``), never taken as empty, and a withheld value, an
event or a keyed field gives no lead. When the rule declares an owner-system proposal
type (``propose``) the lead carries the ``plx.case_propose`` arguments for it (capability,
payload cited to the document field, citations, expected effect on the record's empty
field): a draft only, recorded after its capability guards pass on the cited rows and a
person confirms the evidence belongs to the record.

Inside one document (``plx.object`` on a document type), an amount stated in more than
one currency (extraction gap ``currency_conflict``) is a typed conflict listing each
reading: the property's value stays null, nothing is coerced, converted or summed.
:func:`label_reads` labels each value of a document type with the reader of the field it
was projected from (``read_by``: ``parser`` or ``ocr``, ``ontology.document_fields``).
A document class that declares who may *not* have issued it
(``not_issued_by_own_party``) is reported with the gap ``issued_by_own_party`` when a
record this reader may see carries the document's own number: the class stage reads the
document text alone, so a purchase order the own party issued may be classed as a
customer's (:func:`ontology.document_fields.own_record_keys`).
:func:`agreement` states, per compared field of one document and one record it attests,
whether this reader's comparison agrees, conflicts or could not be made (the measure of
``scripts/eval_document_fields_live.py --links``); a value of another measure (two
units) is not comparable there, never an agreement.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Iterable, Mapping

MAX_VALUES = 12
NOTE = ("machine-read document (a lead) against the cited record values; reported, never "
        "resolved")
BLANK_NOTE = ("the document leaves this blank: unknown, never zero and never a match; "
              "the record value is not confirmed by it")
BC_ONLY_NOTE = ("machine-read document (a lead) against Business Central alone (no Portal "
                "record compared), company {companies}; reported, never resolved and never "
                "authoritative")
CURRENCY_NOTE = ("the document states this amount in more than one currency: reported, "
                 "never coerced to one currency, converted or summed")
ISSUED_BY_OWN_PARTY = "issued_by_own_party"
ISSUED_DETAIL = ("a record this reader may see carries this document's own number, so the "
                 "own party may have issued it: the class was read from the document text "
                 "alone, which does not say who issued it")
MISSING_NOTE = ("machine-read document (a lead) states a value the cited record fields hold "
                "empty; reported, never written: a person confirms it belongs to the record")
TIER = "lead"


def conflicts(tools: Any, scope: Any, object_id: str, object_type: str
              ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(conflicts, not-compared gaps, missing-in-record leads) of every declared
    comparison of ``object_id`` (a document or a record it attests)."""
    from ontology.document_fields import comparisons

    schema = tools.schema
    if schema is None:
        return [], [], []
    out: list[dict[str, Any]] = []
    leads: list[dict[str, Any]] = []
    documents: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    by_gap: dict[str, tuple[dict[str, Any], set[str]]] = {}   # gap -> documents giving it
    extra: list[dict[str, Any]] = []
    if object_type in _document_types():
        documents[object_id] = _evidence(tools, scope, object_id)
        out.extend(_currency_conflicts(tools, object_type, documents[object_id]))
        extra.extend(_issued_by_own_party(tools, scope, object_id, object_type,
                                          documents[object_id][1]))
    for rule in comparisons():
        link = schema.link_types.get(rule["link_type"]) or {}
        if object_type == rule["document_type"]:
            pairs = [(object_id, other) for other in _ends(tools, scope, [object_id],
                                                           rule["link_type"], "forward")]
        elif object_type == link.get("to"):
            pairs = [(other, object_id) for other in _ends(tools, scope, [object_id],
                                                           rule["link_type"], "reverse")]
        else:
            continue
        for document, record in pairs:
            if document not in documents:
                documents[document] = _evidence(tools, scope, document)
            found, not_compared, missing, _ = _compare(tools, scope, rule,
                                                       documents[document], record,
                                                       link.get("to", ""))
            out.extend(found)
            for gap in not_compared:
                by_gap.setdefault(json.dumps(gap, sort_keys=True), (gap, set()))[1].add(document)
            leads.extend(missing)
    gaps = [{**gap, "count": len(seen)} if len(seen) > 1 else gap
            for gap, seen in by_gap.values()]
    return out, extra + gaps, leads


def _issued_by_own_party(tools: Any, scope: Any, object_id: str, object_type: str,
                         evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The gap ``issued_by_own_party`` when a record of the own-record type this reader
    may see carries the document's own number (``ontology.document_fields.own_record_keys``:
    the class's declared own-record identifier kind, read from its field by the identifier
    grammar, and the record type that kind identifies): a purchase order the own party
    issued is no customer's purchase order, and the class stage cannot see the records.
    Only what this reader may read; never a name. A customer's number the identifier
    grammar reads as an own purchase order's number is reported too: a stated doubt,
    never a dismissal of the document."""
    from operating.ontology_tools import _gap
    from ontology.document_fields import own_record_keys

    keys = own_record_keys(object_type, evidence)
    if not keys:
        return []
    types = {(kind, value): record_type for kind, value, record_type in keys}
    rows = tools.store.identifiers_by_keys(scope, list(types))
    if not any(row["object_id"] != object_id and types.get(
            (row["identifier_kind"], row["normalized"])) == row["object_type"] for row in rows):
        return []
    return [_gap("unknown", ISSUED_BY_OWN_PARTY, object_id, ISSUED_DETAIL)]


AGREEMENTS = ("conflict", "not_comparable", "agree")   # a field two rules compare: first
# The reasons of the not-compared gaps ``_compare`` reports (``_undeclared``,
# ``_not_comparable``, ``_code_not_comparable``); the lead kind of a value the record
# holds empty. Each is the cause of a not_comparable field it is the first of.
GAP_REASONS = ("property_not_declared", "currency_unknown", "currency_differs",
               "tax_basis_unknown", "tax_basis_differs", "unreadable", "several_values")
MISSING_IN_RECORD = "missing_in_record"
# Why a rule ``_compare`` called not compared compared nothing, when it met no gap and no
# lead (a gap's reason or ``MISSING_IN_RECORD`` is the cause otherwise):
NOT_COMPARED = (
    "document_not_stated",           # the reader may read no value of the document field
    "record_event_absent",           # the record holds no readable event of the rule's type
    "no_record_reached",             # the rule's path reaches no record declaring it
    "record_property_not_served",    # every record reached declares it not served
    "record_source_lacks_property",  # no record reached is from a source that holds it
    "document_blank",                # a blank page field facing no record value
    "record_value_absent",           # no readable record value (empty, withheld)
    "no_entry_compared",             # keyed: no document entry met a record entry's key
    "document_value_unread",         # the document field holds no value it was read as
    "different_measure")             # values met, none of one measure (unit, a code word)


def agreement(tools: Any, scope: Any, document: str, record: str) -> dict[str, str]:
    """Per field of every declared comparison of ``document`` with ``record`` (an end of
    the rule's link type from the document that this reader may see), what the reader may
    compare there (``AGREEMENTS``): ``conflict`` (a typed conflict, a blank paper field
    facing a stated value included), ``not_comparable`` (a not-compared gap, a
    missing-in-record lead, or no stated value on one side this reader may read) or
    ``agree`` (every compared record value is the document's)."""
    return {field: outcome for field, (outcome, _) in
            agreement_with_causes(tools, scope, document, record).items()}


def agreement_with_causes(tools: Any, scope: Any, document: str, record: str
                          ) -> dict[str, tuple[str, str | None]]:
    """:func:`agreement` with, per field, why a ``not_comparable`` one was not compared
    (else None), as the same comparison decided it: of the field's first rule that was
    not comparable, the reason of the first gap it reported, else ``missing_in_record``
    (a lead), else the ``NOT_COMPARED`` cause ``_compare`` gave. A document this reader
    may not read gives no field (its values are read only for a reader that may)."""
    from ontology.document_fields import comparisons

    row = tools.store.objects([document]).get(document) if tools.schema is not None and \
        document in tools._visible_versions(scope, [document]) else None
    out: dict[str, tuple[str, str | None]] = {}
    evidence = None
    for rule in comparisons():
        if row is None or rule["document_type"] != row["object_type"] or record not in _ends(
                tools, scope, [document], rule["link_type"], "forward"):
            continue
        if evidence is None:
            evidence = _evidence(tools, scope, document)
        link = tools.schema.link_types.get(rule["link_type"]) or {}
        found, gaps, missing, not_compared = _compare(tools, scope, rule, evidence, record,
                                                      link.get("to", ""))
        outcome = ("conflict" if found else "not_comparable" if gaps or missing
                   or not_compared is not None else "agree")
        cause = None if outcome != "not_comparable" else str(gaps[0].get("reason")) \
            if gaps else MISSING_IN_RECORD if missing else not_compared
        held = out.get(rule["field"])
        if held is None or AGREEMENTS.index(outcome) < AGREEMENTS.index(held[0]):
            out[rule["field"]] = (outcome, cause)
    return out


def label_reads(tools: Any, scope: Any, object_id: str, object_type: str,
                values: list[dict[str, Any]]) -> None:
    """Set ``read_by`` on each of ``values`` (``plx.object`` values of ``object_id``) that
    a document type projects from an extracted field, as the reader may read it."""
    if tools.schema is None or object_type not in _document_types():
        return
    labelled, _ = _evidence(tools, scope, object_id)
    for value in values:
        read_by = (labelled.get(value["property"]) or {}).get("read_by")
        if read_by is not None:
            value["read_by"] = read_by


def _document_types() -> set[str]:
    from ontology.document_fields import profiles
    return {profile["object_type"] for profile in profiles()["profiles"]}


def _fields_of(tools: Any, object_type: str) -> dict[str, str]:
    """property -> the extracted field it is projected from (``fields.<field>.*``)."""
    out = {}
    for prop in (tools.schema.object_types.get(object_type) or {}).get("properties", ()):
        for source in prop.get("sources", ()):
            m = re.match(r"fields\.([a-z][a-z0-9_]*)\.", str(source.get("path", "")))
            if m:
                out[prop["name"]] = m.group(1)
    return out


def _currency_conflicts(tools: Any, object_type: str,
                        document: tuple[dict[str, Any], dict[str, Any]]
                        ) -> list[dict[str, Any]]:
    """An amount the document states in more than one currency (module docstring)."""
    values, evidence = document
    out = []
    for prop, field in sorted(_fields_of(tools, object_type).items()):
        item = (evidence.get("fields") or {}).get(field) or {}
        readings = [r for r in item.get("readings") or () if isinstance(r, Mapping)]
        stated = values.get(prop)
        if item.get("gap") != "currency_conflict" or stated is None or \
                stated.get("value_type") != "money" or \
                len({r.get("currency") for r in readings}) < 2:
            continue
        base = {k: v for k, v in stated.items() if k not in ("value", "currency_gap")}
        out.append({"kind": "value_disagreement",
                    "subject": f"{object_type}.{prop} currency"[:700],
                    "values": [{**base, "value": r.get("value"), "currency": r["currency"]}
                               for r in readings][:MAX_VALUES],
                    "note": CURRENCY_NOTE, "tier": TIER})
    return out


def _ends(tools: Any, scope: Any, starts: Iterable[str], link_type: str,
          direction: str) -> list[str]:
    """Objects one hop along ``link_type`` from ``starts`` that the reader may see."""
    found: list[str] = []
    for start in starts:
        for row in tools.store.links_of(start):
            if row["link_type"] != link_type or not tools._row_visible(scope, row):
                continue
            forward = row["from_object"] == start
            if forward != (direction == "forward"):
                continue
            found.append(row["to_object"] if forward else row["from_object"])
    return sorted(tools._visible_versions(scope, dict.fromkeys(found)))


def _walk(tools: Any, scope: Any, start: str, path: Iterable[Mapping[str, str]]) -> list[str]:
    objects = [start]
    for hop in path:
        objects = _ends(tools, scope, objects, hop["link_type"], hop["direction"])
    return objects


def _values(tools: Any, scope: Any, objects: list[str], prop: str
            ) -> list[tuple[str, dict[str, Any], str]]:
    """(object, value, tax basis) of ``prop`` the reader may read; the basis is the
    value's schema source's (``unknown`` when not money or not declared)."""
    from ontology.units import value_basis
    from operating.ontology_tools import _value_out
    types = {oid: row["object_type"] for oid, row in tools.store.objects(objects).items()}
    out = []
    for row in tools.store.readable_values(scope, objects):
        if row["property"] == prop:
            value, _ = _value_out(row, types[row["object_id"]], 2000)
            if value is not None:
                spec = next((p for p in (tools.schema.object_types.get(
                    types[row["object_id"]]) or {}).get("properties", ())
                    if p["name"] == prop), None)
                out.append((row["object_id"], value,
                            value_basis(spec, row)))
    return out


def _event_values(tools: Any, scope: Any, record: str, event_type: str
                  ) -> list[dict[str, Any]]:
    """An event of ``record`` as a dated value (its own citation), when the reader may
    read the event's row."""
    from operating.ontology_tools import _citation, _known_at, _markings
    rows = tools.store.conn.execute(
        "SELECT e.* FROM events e JOIN event_objects eo ON eo.event_id = e.event_id "
        "WHERE eo.object_id = ? AND e.event_type = ? ORDER BY e.event_id",
        (record, event_type)).fetchall()
    out = []
    for row in rows:
        if not _known_at(row, scope.at) or not scope.holds(_markings(row["markings"])) or \
                not tools._item_allowed(scope.actor, row["l1_handle"]) or not row["time"]:
            continue
        citation = _citation(row)
        if citation is not None:
            out.append({"property": event_type.split(".", 1)[1], "value_type": "date",
                        "value": str(row["time"])[:10], "citation": citation,
                        "markings": list(_markings(row["markings"]))})
    return out


def _evidence(tools: Any, scope: Any, document: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the document's values by property, its extraction) as this reader may read them;
    the extraction (read whole from the store, never a truncated envelope) is empty when
    it is withheld from the reader."""
    from operating.ontology_tools import _value_out
    object_type = tools.store.objects([document])[document]["object_type"]
    by_property: dict[str, dict[str, Any]] = {}
    raw = None
    for row in tools.store.readable_values(scope, [document]):
        value, _ = _value_out(row, object_type, 2000)
        if value is not None:
            by_property[row["property"]] = value
        if row["property"] == "evidence":
            raw = row["value"]
    try:
        evidence = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        evidence = {}
    evidence = evidence if isinstance(evidence, dict) else {}
    from ontology.document_fields import READ_BY
    fields = evidence.get("fields") if isinstance(evidence.get("fields"), dict) else {}
    for prop, field in _fields_of(tools, object_type).items():
        read_by = (fields.get(field) or {}).get("read_by")
        if prop in by_property and read_by in READ_BY:
            by_property[prop]["read_by"] = read_by
    return by_property, evidence


def _property_of(tools: Any, object_type: str, field: str, keyed: bool) -> str | None:
    """The document type's property projected from ``fields.<field>``."""
    path = f"fields.{field}.entries" if keyed else f"fields.{field}.value"
    for prop in (tools.schema.object_types.get(object_type) or {}).get("properties", ()):
        if any(source.get("path") == path for source in prop.get("sources", ())):
            return prop["name"]
    return None


def _declares(tools: Any, object_type: str, prop: str) -> bool:
    spec = tools.schema.object_types.get(object_type) or {}
    return any(p.get("name") == prop for p in spec.get("properties", ()))


def _undeclared(subject: str, *types: str) -> dict[str, Any]:
    """The gap of a declared comparison whose property the schema does not declare (the
    profile and the schema disagree): reported, never a skipped comparison."""
    from operating.ontology_tools import _gap
    return _gap("unknown", "property_not_declared", subject,
                f"the schema declares no property of {', '.join(types)} for this declared "
                "comparison; it is not compared: neither a conflict, a match nor a missing "
                "value is reported")


def _code_words(text: Any, vocabulary: Iterable[str]) -> set[str]:
    """The codes of ``vocabulary`` a record value names, synonyms folded into one code
    (``ontology.document_fields.code_fold``: 'CFR (CNF)' names one Incoterm)."""
    from ontology.document_fields import code_fold

    words = {w.casefold() for w in vocabulary}
    return {code_fold(token) for token in re.findall(r"[A-Za-z]+", str(text))
            if token.casefold() in words}


def _same(kind: str, left: dict[str, Any], right: dict[str, Any],
          vocabulary: Iterable[str] = ()) -> bool | None:
    """Whether two values agree; None when they are not the same measure (a code
    compares with the one word of its vocabulary a record's text holds, else not)."""
    from operating.ontology_tools import compared_text
    a, b = left.get("value"), right.get("value")
    if kind == "money":   # one currency and tax basis: _not_comparable decided that
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except (InvalidOperation, ValueError):
            return None
    # A withheld text (not_po_like) compares on its source text, never shown; without
    # it the value is unknown: neither a match nor a conflict.
    withheld = [v for v in (a, b) if isinstance(v, Mapping) and "withheld" in v]
    if any(compared_text(v) is None for v in withheld):
        return None
    if isinstance(a, Mapping):
        a = compared_text(a) if "withheld" in a else a.get("untrusted_text")
    if isinstance(b, Mapping):
        b = compared_text(b) if "withheld" in b else b.get("untrusted_text")
    if kind == "quantity":
        ua, ub = _unit(left.get("unit")), _unit(right.get("unit"))
        if ua and ub and ua != ub:
            return None
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except (InvalidOperation, ValueError):
            return str(a) == str(b)
    if kind == "code":
        codes = _code_words(b, vocabulary)
        if len(codes) != 1:
            return None
        b = codes.pop()
    return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


def _unit(unit: Any) -> str | None:
    from ontology.units import unit_token
    return unit_token(unit) if isinstance(unit, str) and unit else None


def _conflict(subject: str, document_value: dict[str, Any], others: list[dict[str, Any]],
              blank: bool) -> dict[str, Any]:
    out = {"kind": "value_disagreement", "subject": subject[:700],
           "values": [document_value, *others][:MAX_VALUES],
           "note": BLANK_NOTE if blank else NOTE, "tier": TIER}
    companies = [_bc_company(value) for value in others]
    named = {c["company_id"]: c for c in companies if c is not None}
    if named:
        out["bc_companies"] = [named[key] for key in sorted(named)][:MAX_VALUES]
        if not blank and None not in companies:   # Business Central alone (D2)
            out["note"] = BC_ONLY_NOTE.format(companies=", ".join(
                c["book"] or c["company_id"] for c in out["bc_companies"]))[:600]
    return out


def _bc_company(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """The Business Central company a ``bc://`` cited value comes from, else None."""
    from integrations.live_source_guard import parse_handle
    citation = value.get("citation") or {}
    try:
        (_environment, company, _set, _record), _, _ = parse_handle(
            citation.get("l1_handle"), "bc", 4)
    except (TypeError, ValueError):
        return None
    lineage = str(citation.get("lineage") or "")
    return {"company_id": company.lower(),
            "book": lineage[3:] if lineage.startswith("bc_") else None}


def _compare(tools: Any, scope: Any, rule: Mapping[str, Any],
             document: tuple[dict[str, Any], dict[str, Any]], record: str, record_type: str
             ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
                        str | None]:
    """(conflicts, not-compared gaps, missing-in-record leads, not compared) of one rule
    between a document and a record; ``not compared`` is None when a stated document value
    met at least one record value of the same measure and every one it met was, else why
    not: a ``NOT_COMPARED`` cause, or the reason of the gap that left nothing to compare."""
    doc_type = rule["document_type"]
    prop = _property_of(tools, doc_type, rule["field"], rule["keyed"])
    if prop is None:
        gap = _undeclared(f"{doc_type} field {rule['field']}", doc_type)
        return [], [gap], [], gap["reason"]
    values, evidence = document
    stated = values.get(prop)
    field = (evidence.get("fields") or {}).get(rule["field"]) or {}
    reached: list[str] = []
    rows: dict[str, Any] = {}
    not_declared: list[dict[str, Any]] = []
    if rule.get("event"):
        others = [(value, "unknown") for value in _event_values(tools, scope, record,
                                                                 rule["event"])]
        target = rule["event"]
    else:
        reached = _walk(tools, scope, record, rule["path"])
        rows = tools.store.objects(reached)
        types = {oid: row["object_type"] for oid, row in rows.items()}
        undeclared = sorted({t for t in types.values()
                             if not _declares(tools, t, rule["property"])})
        if undeclared:   # nothing could be read there: never an empty record or a match
            not_declared = [_undeclared(f"{'/'.join(undeclared)}.{rule['property']} vs "
                                        f"{doc_type}.{prop}", *undeclared)]
            reached = [oid for oid in reached if types[oid] not in undeclared]
            if not reached:
                return [], not_declared, [], not_declared[0]["reason"]
        others = [(value, basis) for _, value, basis in _values(tools, scope, reached,
                                                                 rule["property"])]
        declaring = sorted({types[oid] for oid in reached})
        target = f"{'/'.join(declaring) or record_type}.{rule['property']}"
    found, gaps, missing, not_compared = _compare_values(
        tools, scope, rule, stated, field, evidence, reached, others,
        f"{target} vs {doc_type}.{prop}")
    if not_compared == "record_value_absent":
        not_compared = _absent_cause(tools, rule, [rows[oid] for oid in reached], stated)
    return found, not_declared + gaps, missing, not_compared


def _absent_cause(tools: Any, rule: Mapping[str, Any], reached: list[Any],
                  stated: Mapping[str, Any]) -> str:
    """Why the reached records (of types declaring the property) gave no readable value:
    none reached, every one's property not served, none from a source binding it, else a
    blank page field or a record value that is empty or withheld from this reader."""
    if rule.get("event"):
        return "record_event_absent"
    if not reached:
        return "no_record_reached"
    specs = [(row, next(p for p in tools.schema.object_types[row["object_type"]][
        "properties"] if p["name"] == rule["property"])) for row in reached]
    if all(_availability(spec) == "not_served" for _, spec in specs):
        return "record_property_not_served"
    if not any(source.get("binding") == row["binding_id"]
               for row, spec in specs for source in spec.get("sources", ())):
        return "record_source_lacks_property"
    return "document_blank" if stated.get("value") is None else "record_value_absent"


def _availability(spec: Mapping[str, Any]) -> str:
    held = spec.get("availability", "served")
    return str(held.get("state") if isinstance(held, Mapping) else held)


def _compare_values(tools: Any, scope: Any, rule: Mapping[str, Any], stated: Any,
                    field: Mapping[str, Any], evidence: Mapping[str, Any], reached: list[str],
                    others: list[tuple[dict[str, Any], str]], subject: str
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
                               str | None]:
    """``_compare`` of the document's stated value with the record values of the reached
    records whose types declare the property (or of the record's event); a rule that met
    no readable record value is ``record_value_absent`` here (``_compare`` refines it)."""
    others = [item for item in others if item[0].get("value") is not None]
    if stated is None:
        return [], [], [], "document_not_stated"
    if not others:
        if rule.get("event") or rule["keyed"] or stated.get("value") is None:
            # an event not found or a blank page field says nothing
            return [], [], [], "record_value_absent"
        lead = _missing(tools, scope, rule, stated, evidence, reached, subject)
        return [], [], [] if lead is None else [lead], "record_value_absent"
    if rule["keyed"]:
        found, compared = _compare_keyed(tools, scope, rule, reached, stated, field, subject)
        return found, [], [], None if compared else "no_entry_compared"
    if stated.get("value") is None:
        blank = field.get("gap") == "value_missing"
        return ([_conflict(subject, stated, [v for v, _ in others], True)] if blank else []), \
            [], [], "document_value_unread"
    gaps, compared = [], []
    for other, basis in others:
        gap = _not_comparable(subject, stated, rule["tax_basis"], other, basis) \
            if rule["kind"] == "money" else _code_not_comparable(subject, other, rule) \
            if rule["kind"] == "code" else None
        if gap is None:
            compared.append(other)
        else:
            gaps.append(gap)
    same = [_same(rule["kind"], stated, other, rule["vocabulary"]) for other in compared]
    measured = None if same and None not in same else \
        str(gaps[0]["reason"]) if gaps else "different_measure"
    if False not in same:
        return [], gaps, [], measured
    return [_conflict(subject, stated, compared, False)], gaps, [], measured


def _missing(tools: Any, scope: Any, rule: Mapping[str, Any], stated: dict[str, Any],
             evidence: Mapping[str, Any], reached: list[str], subject: str
             ) -> dict[str, Any] | None:
    """The ``missing_in_record`` lead of a value the document states and the reached
    records hold empty (module docstring), else None."""
    read = _empty_reads(tools, scope, reached, rule["property"])
    if read is None:
        return None
    empty, not_read = read
    lead: dict[str, Any] = {"kind": MISSING_IN_RECORD, "subject": subject[:700],
                            "values": [stated, *empty][:MAX_VALUES], "note": MISSING_NOTE,
                            "tier": TIER}
    if not_read:
        lead["fields_not_read"] = not_read[:MAX_VALUES]
    from ontology.document_fields import full_citation
    cited = [{k: v for k, v in full_citation(evidence, c).items() if k != "text"}
             for c in ((evidence.get("fields") or {}).get(rule["field"]) or {}).get(
                 "citations") or () if isinstance(c, Mapping)]
    if cited:
        lead["document_citations"] = cited[:MAX_VALUES]
    proposal = _proposal(rule, stated, evidence, empty)
    if proposal is not None:
        lead["propose"] = proposal
    return lead


_FIELD_PATH = re.compile(r"^(?:row|rows\.[0-9]+)\.(.+)$")


def _empty_reads(tools: Any, scope: Any, objects: list[str], prop: str
                 ) -> tuple[list[dict[str, Any]], list[str]] | None:
    """(the empty reads of ``prop``, the declared fields with no read) on ``objects`` when
    this reader may read every value of it there, none holds a value and each object has
    at least one field read as empty (a source null or a placeholder copy of its own
    number); else None. A field the projection holds no read of (a column not mirrored,
    a key not in its JSON object) is listed, never taken as empty."""
    from operating.ontology_tools import _value_out
    if not objects or tools.store.withheld_values(scope, objects, [prop]):
        return None
    rows = [row for row in tools.store.readable_values(scope, objects)
            if row["property"] == prop]
    if any(row["value"] is not None or not tools._item_allowed(scope.actor, row["l1_handle"])
           for row in rows):
        return None
    empty: list[dict[str, Any]] = []
    not_read: list[str] = []
    for object_id, obj in sorted(tools.store.objects(objects).items()):
        spec = next((p for p in (tools.schema.object_types.get(obj["object_type"]) or {})
                     .get("properties", ()) if p["name"] == prop), None)
        if spec is None or spec.get("availability") in ("not_served", "degraded"):
            return None
        declared = [s["path"] for s in spec.get("sources", ())
                    if s["binding"] == obj["binding_id"]]
        mine = [row for row in rows if row["object_id"] == object_id]
        if not mine:
            return None
        read = set()
        for row in mine:
            value, _ = _value_out(row, obj["object_type"], 2000)
            m = _FIELD_PATH.match(row["field_path"])
            if value is None or m is None:
                return None
            read.add(m.group(1))
            empty.append(value)
        not_read += [path for path in declared if path not in read]
    return empty, sorted(set(not_read))


def _proposal(rule: Mapping[str, Any], stated: Mapping[str, Any],
              evidence: Mapping[str, Any], empty: list[dict[str, Any]]
              ) -> dict[str, Any] | None:
    """The ``plx.case_propose`` arguments (all but the case) of the rule's declared
    owner-system proposal type for a value the record lacks: its cited-value parameter
    set to the document's typed value, cited to the document's field; its effect on the
    first empty Portal field the type may write. None when the rule declares none, the
    type is not offered on the case host or refuses registration, the value is document
    text (never a typed value) or no empty field of a bound relation is writable. The
    capability guards re-check every precondition on the cited rows before anything is
    recorded; a person confirms the evidence belongs to the record."""
    from integrations.source_mirror import parse_citation
    from operating import capability_guards
    from operating.action_catalog import ActionCatalog
    capability = rule.get("propose")
    value = stated.get("value")
    handle = evidence.get("l1_handle")
    entry = ActionCatalog.builtin().get(capability)
    if entry is None or not capability_guards.available(capability) or \
            entry.get("registration") or not isinstance(value, str) or \
            not isinstance(handle, str):
        return None
    traced = [(name, spec["trace"]) for name, spec in entry["parameters"].items()
              if (spec.get("trace") or {}).get("kind") == "cited_value"]
    if len(traced) != 1:
        return None
    (name, trace), = traced
    writable = [path for item in entry["preconditions"]
                if item.get("check") == "effect_fields_within" for path in item["fields"]]
    for path in writable:
        for read in empty:
            citation = read["citation"]
            m = _FIELD_PATH.match(citation["field_path"])
            try:
                relation, _, key, _ = parse_citation(citation["l1_handle"])
            except ValueError:
                continue
            if m is None or m.group(1) != path or not capability_guards.bound(entry, relation):
                continue
            return {"tool": "plx.case_propose", "capability": capability,
                    "payload": {name: value, trace["citation"]: handle,
                                trace["field"]: f"fields.{rule['field']}.value"},
                    "citations": [handle, citation["l1_handle"]],
                    "expected_effect": {"relation": relation, "key": key,
                                        "values": {path: value}}}
    return None


def _not_comparable(subject: str, stated: Mapping[str, Any], basis: str,
                    other: Mapping[str, Any], other_basis: str) -> dict[str, Any] | None:
    """The gap why a document amount and a record amount are not compared (not one
    stated currency or not one declared tax basis), else None."""
    from operating.ontology_tools import _gap
    a, b = stated.get("currency"), other.get("currency")
    if not a or not b:
        reason, why = "currency_unknown", f"currency {a or 'unknown'} vs {b or 'unknown'}"
    elif a != b:
        reason, why = "currency_differs", f"currency {a} vs {b}; never converted"
    elif "unknown" in (basis, other_basis):
        reason, why = "tax_basis_unknown", f"tax basis {basis} vs {other_basis}"
    elif basis != other_basis:
        reason, why = "tax_basis_differs", f"tax basis {basis} vs {other_basis}"
    else:
        return None
    company = _bc_company(other)
    where = f" (Business Central company {company['book'] or company['company_id']})" \
        if company else ""
    return _gap("unknown", reason, subject, (
        f"the document amount and the record amount{where} are not compared, {why}: "
        "neither a conflict nor a match is reported")[:600])


def _code_not_comparable(subject: str, other: Mapping[str, Any], rule: Mapping[str, Any]
                         ) -> dict[str, Any] | None:
    """The gap why a document code (an Incoterm) is not compared with a record value that
    names no word of the code's vocabulary or several (``several_values`` /
    ``unreadable``, as the extractor names them on the document side), else None: never
    a match, never a conflict."""
    from operating.ontology_tools import _gap
    value = other.get("value")
    text = value.get("untrusted_text") if isinstance(value, Mapping) else value
    codes = _code_words(text, rule["vocabulary"])
    if len(codes) == 1:
        return None
    reason = "several_values" if codes else "unreadable"
    return _gap("unknown", reason, subject, (
        f"the record value names {len(codes)} codes of the vocabulary, not exactly one: the "
        "document code is not compared with it, neither a conflict nor a match is "
        "reported")[:600])


def _compare_keyed(tools: Any, scope: Any, rule: Mapping[str, Any], reached: list[str],
                   stated: dict[str, Any], field: Mapping[str, Any], subject: str
                   ) -> tuple[list[dict[str, Any]], bool]:
    """Entry by entry: each reached record value's key is read through the key path (a
    value whose key is not exactly one is not compared); one key's values are compared
    with that key's document entry, never added up. Also whether some stated entry met
    record values of the same measure and every one it met was."""
    entries = {str(entry.get("key", "")).upper(): entry for entry in field.get("entries") or ()
               if isinstance(entry, Mapping)}
    by_key: dict[str, list[dict[str, Any]]] = {}
    for object_id, value, _ in _values(tools, scope, reached, rule["property"]):
        if value.get("value") is None:
            continue
        keys = {_key_text(key.get("value")) for _, key, _ in _values(
            tools, scope, _walk(tools, scope, object_id, rule["key_path"]), rule["key_property"])}
        keys.discard(None)
        if len(keys) == 1:
            by_key.setdefault(keys.pop(), []).append(value)
    out: list[dict[str, Any]] = []
    same: list[bool | None] = []
    for key, others in sorted(by_key.items()):
        entry = entries.get(key)
        if entry is None:
            continue   # the document names no entry for it: not stated, not zero
        role = next((o.get("quantity_role") for o in others if o.get("quantity_role")), None)
        if role is None:
            continue   # a quantity is never typed without its role
        unit = entry.get("unit")
        document_value = {**{k: v for k, v in stated.items() if k != "value"},
                          "value_type": "quantity", "value": entry.get("value"), "unit": unit,
                          "quantity_role": role}
        if unit is None:
            document_value["unit_gap"] = {"state": "unknown", "reason": "unit_unknown",
                                          "subject": f"{subject} {key}"[:700]}
        if entry.get("value") is None:
            if entry.get("gap") == "value_missing":
                out.append(_conflict(f"{subject} {key}", document_value, others, True))
            continue
        each = [_same("quantity", document_value, other) for other in others]
        same += each
        if False in each:
            out.append(_conflict(f"{subject} {key}", document_value, others, False))
    return out, bool(same) and None not in same


def _key_text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("value") or value.get("normalized") or value.get("untrusted_text")
    return " ".join(str(value).split()).upper() if value else None
