"""Load, validate and digest the ontology schema.

The schema is a set of ``plx-ontology-domain/v1`` files (C0 format,
``ontology/schema/_format.schema.json``):

* ``ontology/schema/domains/*.json``: curated domain files (``origin: curated``);
* ``ontology/schema/generated/*.json``: the skeleton written by
  ``scripts/gen_ontology_skeleton.py`` (``origin: generated``), one raw object type
  per served Portal reporting-API relation, plus Prisma foreign keys and the
  existing link contracts as link types, and one ``not_served`` type (no binding,
  an upstream ask) per Portal relation it knows of but the API does not serve;
* ``ontology/schema/identifiers.json`` (identifier kinds), when present;
* ``ontology/schema/generated/meta/coverage.json``: the skeleton's explicit
  relation exclusions and the catalog digest it was generated from.

Compilation rule: a curated binding to a source relation (a Portal relation, a BC entity
set) **shadows** the generated raw type bound to the same relation (curation wins; the raw type and its generated
links leave the compiled schema and are listed in ``shadowed``). Nothing else is
merged or overridden: ids must be unique across every file.

Every file is checked against the frozen C0 format with ``ontology.json_contract``
(the committed ``ontology/schema/`` files are format-checked only on request,
``check_format=True``: ``python3 -m ontology.schema`` and the skeleton generator's
``generate --check``, both in the ``ontology-schema`` gate, do it; see ``load_schema``);
then the cross-file rules the format leaves to the loader are checked (unique ids,
type/binding/property references, link endpoints, identifier kinds, link contract
ids, stored-key bindings (fk/json_path/same_record) never comparing ``text``,
authority coverage of multi-source properties, and three value invariants:
consolidation copies are never authority, money is finance-marked, a literal unit
or price denominator comes from ``LITERAL_UNITS``), one action rule (an owner-system
write takes no parameter that names a record or scope from the user objective,
``OWNER_REFERENCE_PARAMS``) and, when a catalog is given,
every Portal binding, key, predicate, property source and link pair is checked
against the reporting-API catalog: an unknown relation or column is an error.

**Document types** (the classes ``ontology/document_fields.py`` reads from captured
documents) are declared once, in ``ontology/schema/document_fields.json``: a curated type
whose bindings read relation ``documentFields`` selects one profile, and the loader
derives one property per profile field for every such binding
(``derive_document_types``); a hand-written ``fields.*`` property is refused, and every
profile's ``attests`` rules must reach declared links and properties (``_attests_errors``).

``CompiledSchema.digest`` is a SHA-256 over a canonical form: independent of file
order, of the order of id-keyed lists, and of JSON formatting; ordered semantics
(authority precedence, key and pair order, lineage rule order) are kept.

Link bindings whose two ends are BC captures compare within one BC company:
document and master numbers are unique only per company (book), so
``CompiledSchema.company_scope`` lists them (``link_company_scope``) until the C0
format can say it (``bc-link-company-scope``). A binding
joining a BC capture to another source on a BC number, not on the BC record's key, does
not know the company (``bc_company_not_known``): its links are at most leads.

**Pending** files (``ontology/schema/pending/*.json``, curated, opt-in with
``include_pending``) declare bindings for a relation the reporting API does not serve
yet (an upstream ask), so they are reviewed and tested before the relation exists.
A pending object type replaces the curated ``not_served`` placeholder of the same id
and keeps the placeholder's markings (a replacement can add markings, never drop
one); anything else is merged like any other file. Their links from served relations are
not typed by hand: each foreign key the generator found from a served relation to a
pending binding's relation (``coverage.json`` ``prisma_relations_not_bound`` entries with
a ``link``) becomes a link type from every type binding the served relation
(``pending_links``), unless a declared link joins the same bindings over the same pairs.
They never load by default, so the served schema (and every catalog check) is unchanged; ``pending_report`` lists their
relations and whether a catalog now serves them (time to move the file to domains/).

``drift_report`` compares a compiled schema with a (newer) catalog: served
relations neither bound nor excluded, bound relations or columns that are gone
(a property whose column is gone is reported as ``degraded``), served columns
no binding reads, and declared ``not_served`` columns that are now served.

Stdlib only; no network; imports only ``ontology.json_contract`` (the profile file is read
as JSON, never through ``ontology.document_fields``).
"""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import pickle
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ontology import json_contract

FORMAT = "plx-ontology-domain/v1"
SCHEMA_DIR = Path(__file__).with_name("schema")
FORMAT_PATH = SCHEMA_DIR / "_format.schema.json"
DOMAINS_DIR = SCHEMA_DIR / "domains"
PENDING_DIR = SCHEMA_DIR / "pending"
GENERATED_DIR = SCHEMA_DIR / "generated"
META_DIR = GENERATED_DIR / "meta"
COVERAGE_PATH = META_DIR / "coverage.json"
CATALOG_SNAPSHOT_PATH = META_DIR / "portal_catalog.json"
IDENTIFIERS_PATH = SCHEMA_DIR / "identifiers.json"
LINK_CONTRACTS_PATH = Path(__file__).resolve().parent.parent / "operating" / "link_contracts.json"

COVERAGE_FORMAT = "plx-ontology-skeleton-coverage/v1"
CATALOG_FORMAT = "plx-portal-catalog-snapshot/v1"

# Same grammar as integrations/workbench_read_api.py (the catalog reader).
_QUALIFIED = re.compile(r"^([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# Lineages the C0 format admits as authority only with a human confirmation:
# leaving them out of a precedence is expected, so it is not even a warning.
NEVER_AUTHORITY_UNCONFIRMED = frozenset({"gold_native_staging"})

# Lineages that are never authority, whatever the evidence: the BC consolidation book
# holds copies of the operating books' entries (company-pins.ts ``BcBookKey`` "consol",
# plx-customer-portal@b46934a), so ranking it would let a copy outrank its original.
NEVER_AUTHORITY = frozenset({"bc_consol"})

# Units a schema file may state as a literal (``unit``). Anything else is an invented
# unit unless an evidenced unit contract states it (the property or source cites
# ``UNIT_CONTRACTS_EVIDENCE``, as the skeleton generator does): read the unit from the
# source (``unit_from``) or declare ``unit_unknown``. Placeholders such as "unknown" or
# "item_uom" are always refused. Adding a unit here is a reviewed loader change.
LITERAL_UNITS = frozenset({
    "each", "box", "case", "carton", "pallet", "bottle", "drum", "pail", "tote",
    "kg", "g", "mg", "lb", "oz", "L", "mL", "gal",
    "1", "percent",
})
UNIT_CONTRACTS_EVIDENCE = "operating/source_unit_contracts.json"
PLACEHOLDER_UNITS = frozenset({"unknown", "n/a", "na", "tbd", "uom", "item_uom",
                               "component_uom", "unit"})

# Scope a link binding's pairs are compared in (see link_company_scope).
SAME_BC_COMPANY = "same_bc_company"
# Why a link between a BC capture and another source is capped (see bc_company_not_known).
BC_COMPANY_NOT_KNOWN = "bc_company_not_known"

# Link binding kinds whose pairs compare stored keys, and the compares they may use
# (DECISIONS/2026-10-02-stored-key-compare): ``text`` is a free-text compare, legal
# only on ``text_match`` bindings.
STORED_KEY_KINDS = frozenset({"fk", "json_path", "same_record"})
STORED_KEY_COMPARES = frozenset({"integer_id", "text_id", "identifier"})

# Action parameter types that name a record or a scope. An owner-system write (executor
# human_in_owner_system, whatever the system) takes them only from a cited value or a
# schema constant, never from the user objective: a typed code or name is not an identity.
# A generator types every input naming a record or scope as one of them
# (ontology/generator/mcp.py ``_NAMES_RECORD_OR_SCOPE``).
OWNER_REFERENCE_PARAMS = frozenset({"identifier", "object_ref", "citation"})


# Mention links (decision mention-link-polymorphism (a)): "a document mentions any object"
# is stored as one concrete link type per target, generated here from this one rule, never
# copied by hand. The rule applies when the domain file that declares ``from`` is loaded:
# every served object type outside that domain with an ``identifier`` property gets
# ``<from>.mentions_<target in snake case>`` (``ontology/mentions.py`` stores the links).
# ``evidence`` names refs of that file's evidence catalog.
MENTION_LINK_RULE: Mapping[str, Any] = {
    "from": "Email",
    "from_binding": "graph_mail.messages",
    "reverse_name": "mentioned_in_email",
    "cardinality": "N:M",
    "tier_ceiling": "corroborated",
    "evidence": ("wb.mentions", "ctx.mentions"),
    "description": ("An identifier of a {target} written in a captured message (subject, body "
                    "or an attachment name), resolved through the identifier index "
                    "(ontology/mentions.py). A lead unless two or more independent mention "
                    "features agree (ontology/mentions.py MENTION_FEATURES: the mention, the "
                    "sender at a distinctive domain of the target's party, an internal "
                    "mention in the same conversation; no veto), then corroborated, never "
                    "more: one candidate is still a candidate, several stay several links "
                    "(ambiguous), quoted history and external senders are external "
                    "assertions."),
}
# The same rule for captured SharePoint files (integrations/capture_sharepoint.py): the
# file name and its extracted text are scanned; the author is unknown, so every such
# mention is an external assertion.
SHAREPOINT_MENTION_LINK_RULE: Mapping[str, Any] = {
    "from": "SharePointFile",
    "from_binding": "sharepoint.drive_items",
    "reverse_name": "mentioned_in_sharepoint_file",
    "cardinality": "N:M",
    "tier_ceiling": "lead",
    "evidence": ("wb.mentions", "ctx.mentions"),
    "description": ("An identifier of a {target} written in a captured SharePoint file (its "
                    "name or extracted text), resolved through the identifier index "
                    "(ontology/mentions.py). Always a lead: one candidate is still a "
                    "candidate, several stay several links (ambiguous); the author is "
                    "unknown, so every mention is an external assertion."),
}
# The same rule for captured mail attachments (ontology/project_graph.py): the file name
# and its extracted text, a PDF's page by page; the file's author is unknown, so every
# such mention is an external assertion.
ATTACHMENT_MENTION_LINK_RULE: Mapping[str, Any] = {
    "from": "Attachment",
    "from_binding": "graph_mail.attachments",
    "reverse_name": "mentioned_in_attachment",
    "cardinality": "N:M",
    "tier_ceiling": "lead",
    "evidence": ("wb.mentions", "ctx.mentions"),
    "description": ("An identifier of a {target} written in a captured mail attachment (its "
                    "name or extracted text, a PDF cited by page), resolved through the "
                    "identifier index (ontology/mentions.py). Always a lead: one candidate "
                    "is still a candidate, several stay several links (ambiguous); the "
                    "file's author is unknown, so every mention is an external assertion."),
}
# The same rule for members of captured containers (ontology/project_graph.py): a member's
# name and extracted text, a PDF member's page by page; mail and SharePoint containers
# alike (the link's from_binding names one of the type's two bindings; mention links are
# stored by ontology/mentions.py whatever the binding).
CONTAINER_MEMBER_MENTION_LINK_RULE: Mapping[str, Any] = {
    "from": "ContainerMember",
    "from_binding": "sharepoint.container_members",
    "reverse_name": "mentioned_in_container_member",
    "cardinality": "N:M",
    "tier_ceiling": "lead",
    "evidence": ("wb.mentions", "ctx.mentions"),
    "description": ("An identifier of a {target} written in a member of a captured zip, "
                    ".msg or .eml file (its name or extracted text, a PDF cited by page), "
                    "resolved through the identifier index (ontology/mentions.py). Always "
                    "a lead: one candidate is still a candidate, several stay several links "
                    "(ambiguous); the file's author is unknown, so every mention is an "
                    "external assertion."),
}
# Mission Control records (integrations/mc_sources.py): a task's title, description and
# comments, an approval gate's reason and a knowledge entry's title and snippet. Their
# authors are MC users and agents, not verified senders, so every mention is an external
# assertion; a knowledge entry is derived brain text (an untrusted lead) on top.
_MC_MENTION_DESCRIPTION = ("An identifier of a {target} written in a captured {what}, "
                           "resolved through the identifier index (ontology/mentions.py). "
                           "Always a lead: one candidate is still a candidate, several stay "
                           "several links (ambiguous); the text's author is not verified, "
                           "so every mention is an external assertion.")
MC_MENTION_LINK_RULES: tuple[Mapping[str, Any], ...] = tuple(
    {"from": source, "from_binding": binding, "reverse_name": reverse, "cardinality": "N:M",
     "tier_ceiling": "lead", "evidence": ("wb.mentions", "ctx.mentions"),
     "description": _MC_MENTION_DESCRIPTION.replace("{what}", what)}
    for source, binding, reverse, what in (
        ("MCTask", "mission_control.tasks", "mentioned_in_mc_task",
         "Mission Control task (title, description or comments)"),
        ("MCApproval", "mission_control.approvals", "mentioned_in_mc_approval",
         "Mission Control approval gate reason"),
        ("KnowledgeEntry", "mission_control.knowledge", "mentioned_in_knowledge_entry",
         "knowledge entry (title or snippet)")))
MENTION_LINK_RULES: tuple[Mapping[str, Any], ...] = (MENTION_LINK_RULE,
                                                     SHAREPOINT_MENTION_LINK_RULE,
                                                     ATTACHMENT_MENTION_LINK_RULE,
                                                     CONTAINER_MEMBER_MENTION_LINK_RULE,
                                                     *MC_MENTION_LINK_RULES)


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def mention_links(object_types: Mapping[str, Mapping[str, Any]], domain_types: Iterable[str],
                  catalog: Mapping[str, Any], rule: Mapping[str, Any] = MENTION_LINK_RULE,
                  copy_kinds: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The link types ``rule`` generates over ``object_types`` (the compiled types);
    ``domain_types`` are the types of the domain declaring ``rule["from"]`` (never
    targets) and ``catalog`` is that domain's evidence catalog. ``copy_kinds``: the
    identifier kinds that declare ``copies`` (a Message-ID, a content digest). A copy
    identity is never read from text, so a type whose only identifiers are copy kinds is
    no target: its link type would stay empty and read as an absence."""
    skip, copies = set(domain_types), set(copy_kinds)
    evidence = [catalog[ref] for ref in rule["evidence"] if ref in catalog]
    out = []
    for target in sorted(object_types):
        spec = object_types[target]
        if target in skip or spec.get("availability", {}).get("state") == "not_served":
            continue
        if not any(p.get("value_type") == "identifier" and p.get("identifier_kind") not in copies
                   for p in spec.get("properties", ())):
            continue
        name = "mentions_" + _snake_case(target)
        out.append({
            "id": f"{rule['from']}.{name}", "from": rule["from"], "to": target, "name": name,
            "reverse_name": rule["reverse_name"], "cardinality": rule["cardinality"],
            "description": rule["description"].format(target=target),
            "bindings": [{"kind": "mention", "tier_ceiling": rule["tier_ceiling"],
                          "from_binding": rule["from_binding"],
                          "evidence": [dict(e) for e in evidence]}]})
    return out


def pending_links(compiled: "CompiledSchema", pending_relations: Mapping[str, str],
                  not_bound: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Link types to the pending bindings (``pending_relations``: relation -> binding id)
    from the generator's foreign keys whose target relation is not served (``not_bound``,
    coverage ``prisma_relations_not_bound``): one per type binding the served source
    relation. A declared link over the same bindings and pairs stands in for it."""
    declared = {(lb.get("from_binding"), lb.get("to_binding"),
                 tuple((p["from"], p["to"]) for p in lb.get("pairs", ())))
                for link in compiled.link_types.values() for lb in link["bindings"]}
    owners = compiled.portal_bindings()
    out = []
    for fk in not_bound:
        spec, to_bid = fk.get("link"), pending_relations.get(fk.get("to"))
        if spec is None or to_bid not in compiled.bindings:
            continue
        to_tid = compiled.bindings[to_bid][0]
        pairs = tuple((p["from"], p["to"]) for p in spec["pairs"])
        for from_tid, from_binding in sorted(owners.get(fk["from"], ()),
                                             key=lambda owner: owner[1]["id"]):
            if (from_binding["id"], to_bid, pairs) in declared:
                continue
            out.append({
                "id": f"{from_tid}.{spec['name']}", "from": from_tid, "to": to_tid,
                "name": spec["name"],
                "reverse_name": f"{_snake_case(from_tid)}_{spec['name']}"[:64],
                "cardinality": spec["cardinality"], "description": spec["description"],
                "bindings": [{"kind": "fk", "tier_ceiling": "verified",
                              "from_binding": from_binding["id"], "to_binding": to_bid,
                              "pairs": [dict(p) for p in spec["pairs"]],
                              "evidence": [dict(e) for e in spec["evidence"]]}]})
    return out


# Document object types (ontology/document_fields.py): one declaration. A type whose
# bindings read relation ``documentFields`` selects one profile of
# ``ontology/schema/document_fields.json`` (``where profile eq <id>``), and its field
# properties are derived from that profile here, one per field for every binding; a
# curated property reading ``fields.*`` of such a binding is refused (no restatement).
DOCUMENT_FIELDS_PATH = SCHEMA_DIR / "document_fields.json"
DOCUMENT_FIELDS_FORMAT = "plx-document-field-profiles/v1"
DOCUMENT_FIELDS_RELATION = "documentFields"
CHECKBOX_CHECKED = "checked"   # ontology.document_fields.CHECKED: a checkbox's one value
_FIELD_EVIDENCE = {"kind": "workbench_code", "path": "ontology/document_fields.py"}
# Per kind, the description every derived property carries after its field's own; {noun}
# names the document (the profile's noun, else its object type in words).
_FIELD_DESCRIPTIONS = {
    "text": "Machine-read text as written on the {noun} (untrusted); a lead claim.",
    "identifier": ("The identifier as labelled on the {noun}, normalised by its kind; an "
                   "identity lead, never a record's number by itself."),
    "date": ("Machine-read date on the {noun} (ISO, or a numeric date whose day/month order "
             "is forced); a lead claim, null with a gap in evidence when blank or ambiguous."),
    "money": ("Machine-read from the {noun}: Decimal with the currency written on the "
              "amount or one consistent currency declaration; a lead claim, null with a gap "
              "in evidence when unstated, unreadable or ambiguous."),
    "quantity": ("Machine-read from the {noun}: Decimal with the unit written next to it "
                 "(Portal spelling fold{default}); a lead claim, null with a gap in evidence "
                 "when blank, unclear or not unambiguous (a blank is unknown, never zero)."),
    "code": ("Machine-read code on the {noun} (exactly one vocabulary word on the labelled "
             "line; none or several is a gap in evidence); a lead claim."),
    "place": ("The rest of the labelled line after the code (a named place); untrusted "
              "{noun} text."),
    "checkbox": ("One printed box on the {noun}: 'checked' when a checked mark is read; an "
                 "unchecked box or an unreadable mark is a gap in evidence, never no."),
    "keyed": ("One entry per key with its own value, unit and evidence gap: per key, never a "
              "total (untrusted {noun} text)."),
}
_FIELD_REFERENCE = (" It names a {kind} identifier: read through the identifier grammar and "
                    "linked to the record holding it as a lead mention, never an identifier "
                    "of this {noun}.")


def _profile_of(binding: Mapping[str, Any]) -> str | None:
    """The profile a ``documentFields`` binding selects (its one ``profile eq`` predicate)."""
    hits = [w.get("value") for w in binding.get("where", ())
            if w.get("path") == "profile" and w.get("op") == "eq"]
    return hits[0] if len(hits) == 1 and isinstance(hits[0], str) else None


def _document_noun(profile: Mapping[str, Any]) -> str:
    """The profile's ``noun``, else its object type in words (AssemblyOrderPacket ->
    'assembly order packet')."""
    noun = profile.get("noun")
    if isinstance(noun, str) and noun:
        return noun
    return re.sub(r"(?<!^)(?=[A-Z])", " ", str(profile.get("object_type", "document"))).lower()


def _field_authority_note(profile: Mapping[str, Any], field: str, noun: str,
                          bindings: list[Mapping[str, Any]]) -> str:
    """The authority note of one derived property: how many sources project the type
    (each object from one), and what its field is compared with (``attests``)."""
    sources = ", ".join(sorted({str(b.get("source")) for b in bindings}))
    note = (f"One object per captured {noun} version, each projected from one binding "
            f"(sources: {sources}), so no two sources meet on one object")
    attests = profile.get("attests") or {}
    for rule in attests.get("compare", ()):
        if rule.get("field") != field:
            continue
        what = (f"event {rule['event']}" if "event" in rule else
                f"property {rule.get('property')}" + (" per key" if "key_path" in rule else ""))
        via = " over its declared path" if rule.get("path") else ""
        note += (f"; compared at read time with the {what} of the record it attests "
                 f"({attests.get('link_type')}{via}) and reported as a conflict, never merged "
                 "(attests in ontology/schema/document_fields.json)")
    return note + "."


def _marking_vocabulary() -> set[str]:
    return set(_read_json(FORMAT_PATH)["$defs"]["marking"]["enum"])


def document_field_properties(profile: Mapping[str, Any], vocabularies: Mapping[str, Any],
                              bindings: list[Mapping[str, Any]]
                              ) -> tuple[list[dict[str, Any]], list[str]]:
    """(properties, problems): one property per field of ``profile`` read by every one of
    ``bindings``, named by the field (or its ``property`` key; a code field adds
    ``<name>_place``; a ``count`` quantity is a decimal with its unit and no quantity
    role), marked by its ``marking`` (money always ``finance``), described and given an
    authority note with the type's specifics (its noun, a default unit, what the field
    is compared with, a ``refers_to`` reference); a field the derivation cannot type
    (an unknown kind or marking, a quantity without its ``quantity_role``) is a problem,
    never skipped."""
    lineages = sorted({lineage for b in bindings for lineage in _binding_lineages(b)})
    noun = _document_noun(profile)
    out: list[dict[str, Any]] = []
    problems: list[str] = []

    def prop(name: str, kind: str, spec: Mapping[str, Any], shape: Mapping[str, Any],
             path: str, parser: str, **source: str) -> dict[str, Any]:
        default = (f"; {spec['default_unit']} when none is written"
                   if spec.get("default_unit") else "")
        description = _FIELD_DESCRIPTIONS[kind].format(noun=noun, default=default)
        if spec.get("description") and kind != "place":
            description = f"{spec['description']} {description}"
        if spec.get("refers_to") and kind != "place":
            description += _FIELD_REFERENCE.format(kind=spec["refers_to"], noun=noun)
        markings = sorted({*([spec["marking"]] if spec.get("marking") else []),
                           *shape.get("markings", ())})
        return {"name": name, **shape, **({"markings": markings} if markings else {}),
                "description": description,
                "sources": [{"binding": b["id"], "path": path, "parser": parser, **source}
                            for b in bindings],
                "evidence": [dict(_FIELD_EVIDENCE)],
                "authority": {"precedence": [{"lineage": list(lineages),
                                              "evidence": [dict(_FIELD_EVIDENCE)]}],
                              "note": _field_authority_note(
                                  profile, spec.get("name") if kind != "place" else None,
                                  noun, bindings)}}

    for spec in profile.get("fields", ()):
        field, kind = spec.get("name"), spec.get("kind")
        name = spec.get("property", field)   # the property's name: the field's by default
        value = f"fields.{field}.value"
        where = f"profile {profile.get('id')!r} field {field!r}"
        if "identifier_kind" in spec and kind != "text":
            problems.append(f"{where}: identifier_kind on a {kind} field")
        elif "marking" in spec and spec["marking"] not in _marking_vocabulary():
            problems.append(f"{where}: marking {spec['marking']!r} is not a marking")
        elif "refers_to" in spec and (kind != "text" or "identifier_kind" in spec):
            problems.append(f"{where}: refers_to only on a text field without identifier_kind")
        elif "quantity_role" in spec and (kind != "quantity" or "keyed" in spec):
            problems.append(f"{where}: quantity_role on a field that is not a plain quantity")
        elif "count" in spec and (spec["count"] is not True or kind != "quantity"
                                  or "keyed" in spec or "quantity_role" in spec):
            problems.append(f"{where}: count is true only on a plain quantity without a "
                            "quantity_role")
        elif "keyed" in spec:
            out.append(prop(name, "keyed", spec, {"value_type": "json"},
                            f"fields.{field}.entries", "as_is"))
        elif kind == "text" and "identifier_kind" in spec:
            out.append(prop(name, "identifier", spec, {
                "value_type": "identifier", "identifier_kind": spec["identifier_kind"]},
                value, "identifier"))
        elif kind == "text":
            out.append(prop(name, kind, spec, {"value_type": "text"}, value, "as_is"))
        elif kind == "date":
            out.append(prop(name, kind, spec, {"value_type": "date"}, value, "iso_date"))
        elif kind == "checkbox":
            out.append(prop(name, kind, spec, {"value_type": "enum",
                                               "enum_values": [CHECKBOX_CHECKED]},
                            value, "trim"))
        elif kind == "code" and isinstance(vocabularies.get(spec.get("vocabulary")), list):
            out.append(prop(name, kind, spec, {
                "value_type": "enum", "enum_values": list(vocabularies[spec["vocabulary"]])},
                value, "trim"))
            out.append(prop(f"{name}_place", "place", spec, {"value_type": "text"},
                            f"fields.{field}.place", "as_is"))
        elif kind == "money":
            per_unit = bool(spec.get("per_unit"))
            out.append(prop(name, kind, spec, {
                "value_type": "money", "money_measure": "unit_price" if per_unit else "amount",
                "markings": ["finance"]}, value, "decimal_text",
                currency_from=f"fields.{field}.currency",
                tax_basis=spec.get("tax_basis", "unknown"),
                **({"per_unit_from": f"fields.{field}.per_unit"} if per_unit else {})))
        elif kind == "quantity" and spec.get("count") is True:
            out.append(prop(name, kind, spec, {"value_type": "decimal"},
                            value, "decimal_text", unit_from=f"fields.{field}.unit"))
        elif kind == "quantity" and isinstance(spec.get("quantity_role"), str):
            out.append(prop(name, kind, spec, {"value_type": "quantity",
                                               "quantity_role": spec["quantity_role"]},
                            value, "decimal_text", unit_from=f"fields.{field}.unit"))
        elif kind == "quantity":
            problems.append(f"{where}: a quantity field names no quantity_role")
        else:
            problems.append(f"{where}: kind {kind!r} (or its vocabulary) has no property type")
    names = [p["name"] for p in out]
    problems.extend(f"profile {profile.get('id')!r}: two fields derive property {n!r}"
                    for n in sorted({n for n in names if names.count(n) > 1}))
    return out, problems


def derive_document_types(name: str, doc: Mapping[str, Any], profiles: Any
                          ) -> tuple[Mapping[str, Any], list[str]]:
    """(``doc`` with every document type's field properties derived from its profile,
    errors). ``profiles`` is the parsed profile file (``None``: absent)."""
    types = doc.get("object_types") if isinstance(doc, Mapping) else None
    if not isinstance(types, list) or not any(
            isinstance(b, dict) and b.get("relation") == DOCUMENT_FIELDS_RELATION
            for t in types if isinstance(t, dict) for b in t.get("bindings", ())):
        return doc, []
    if not isinstance(profiles, dict) or profiles.get("format") != DOCUMENT_FIELDS_FORMAT:
        return doc, [f"{name}: a binding reads {DOCUMENT_FIELDS_RELATION} but no "
                     f"{DOCUMENT_FIELDS_FORMAT} profile file is loaded to derive it from"]
    by_id = {p.get("id"): p for p in profiles.get("profiles", ()) if isinstance(p, dict)}
    errors: list[str] = []
    out = []
    for t in types:
        bound = [b for b in t.get("bindings", ()) if isinstance(b, dict)
                 and b.get("relation") == DOCUMENT_FIELDS_RELATION] if isinstance(t, dict) else []
        if not bound:
            out.append(t)
            continue
        where = f"{name}: object type {t.get('id')!r}"
        selected = {_profile_of(b) for b in bound}
        profile = by_id.get(next(iter(selected))) if len(selected) == 1 else None
        if profile is None:
            errors.append(f"{where}: its {DOCUMENT_FIELDS_RELATION} bindings must select one "
                          "known profile (where profile eq <id>), got "
                          f"{sorted(map(str, selected))}")
            out.append(t)
            continue
        if profile.get("object_type") != t.get("id"):
            errors.append(f"{where}: profile {profile['id']!r} declares object_type "
                          f"{profile.get('object_type')!r}")
        derived, problems = document_field_properties(profile, profiles.get("vocabularies", {}),
                                                      bound)
        errors.extend(f"{where}: {p}" for p in problems)
        bound_ids = {b["id"] for b in bound}
        names = {p["name"]: p for p in derived}
        # The extraction (``evidence``) holds every field's value: it carries every marking
        # a field property does, so no field is read around its marking.
        marks = {m for p in derived for m in p.get("markings", ())}
        kept = []
        for p in t.get("properties", ()):
            if isinstance(p, dict) and names.get(p.get("name")) == p:
                continue   # an already derived document (derivation is idempotent)
            if isinstance(p, dict) and marks - set(p.get("markings", ())) and any(
                    isinstance(s, dict) and s.get("binding") in bound_ids
                    and s.get("path") == "evidence" for s in p.get("sources", ())):
                p = {**p, "markings": sorted(marks | set(p.get("markings", ())))}
            if isinstance(p, dict) and any(
                    isinstance(s, dict) and s.get("binding") in bound_ids
                    and str(s.get("path", "")).startswith("fields.") for s in p.get("sources", ())):
                errors.append(f"{where}: property {p.get('name')!r} restates a field of profile "
                              f"{profile['id']!r}; field properties are derived from "
                              "ontology/schema/document_fields.json")
            elif isinstance(p, dict) and p.get("name") in names:
                errors.append(f"{where}: property {p['name']!r} declared twice (derived from "
                              f"profile {profile['id']!r} field)")
            kept.append(p)
        out.append({**t, "properties": derived + kept})
    return {**doc, "object_types": out}, errors


def _attests_errors(compiled: "CompiledSchema", profiles: Any) -> list[str]:
    """Every profile resolves in the compiled schema: its object type exists and selects it,
    and each ``attests`` rule names a declared link from that type, a declared path, and a
    property (or event, key property) of the type it reaches. Checked only when the
    document types are loaded (a type binds ``documentFields``)."""
    if not isinstance(profiles, dict) or not any(
            b.get("relation") == DOCUMENT_FIELDS_RELATION for _, b in compiled.bindings.values()):
        return []
    errors: list[str] = []

    def walk(where: str, start: str, path: Iterable[Mapping[str, Any]]) -> str | None:
        current = start
        for step in path:
            link = compiled.link_types.get(step.get("link_type"))
            if link is None:
                errors.append(f"{where}: path link {step.get('link_type')!r} is not declared")
                return None
            forward = step.get("direction", "forward") == "forward"
            near, far = (link["from"], link["to"]) if forward else (link["to"], link["from"])
            if near != current:
                errors.append(f"{where}: path link {link['id']!r} does not start at {current}")
                return None
            current = far
        return current

    def has(type_id: str | None, prop: str) -> bool:
        spec = compiled.object_types.get(type_id or "") or {}
        return any(p.get("name") == prop for p in spec.get("properties", ()))

    for profile in profiles.get("profiles", ()):
        where = f"document_fields.json: profile {profile.get('id')!r}"
        tid = profile.get("object_type")
        if not any(owner == tid and b.get("relation") == DOCUMENT_FIELDS_RELATION
                   and _profile_of(b) == profile.get("id")
                   for owner, b in compiled.bindings.values()):
            errors.append(f"{where}: object type {tid!r} has no {DOCUMENT_FIELDS_RELATION} "
                          "binding selecting it, so its fields have no property")
            continue
        attests = profile.get("attests") or {}
        if not attests:
            continue
        link = compiled.link_types.get(attests.get("link_type"))
        if link is None or link["from"] != tid:
            errors.append(f"{where}: attests link {attests.get('link_type')!r} is not a "
                          f"declared link from {tid}")
            continue
        for rule in attests.get("compare", ()):
            at = f"{where} attests {rule.get('field')!r}"
            if "event" in rule:
                event = compiled.event_types.get(rule["event"])
                if event is None or event.get("object_type") != link["to"]:
                    errors.append(f"{at}: event {rule['event']!r} is not an event of {link['to']}")
                continue
            end = walk(at, link["to"], rule.get("path", ()))
            if end is not None and not has(end, rule.get("property", "")):
                errors.append(f"{at}: property {rule.get('property')!r} is not a property of {end}")
            if end is not None and "key_path" in rule:
                key_end = walk(at, end, rule["key_path"])
                if key_end is not None and not has(key_end, rule.get("key_property", "")):
                    errors.append(f"{at}: key property {rule.get('key_property')!r} is not a "
                                  f"property of {key_end}")
    return errors


def _reference_errors(compiled: "CompiledSchema", profiles: Any, identifiers: Any
                      ) -> list[str]:
    """A ``refers_to`` field names an identifier kind whose object type the profile's type
    declares a mention link to, so the reference it reads can link (never a silent
    ``undeclared_target``). Checked when the document types and the grammar are loaded."""
    kinds = identifiers.get("kinds") if isinstance(identifiers, dict) else None
    if not isinstance(profiles, dict) or not isinstance(kinds, dict):
        return []
    errors = []
    for profile in profiles.get("profiles", ()):
        tid = profile.get("object_type")
        if tid not in compiled.object_types:
            continue
        for spec in profile.get("fields", ()):
            if "refers_to" not in spec:
                continue
            target = (kinds.get(spec["refers_to"]) or {}).get("object_type")
            if not any(link["from"] == tid and link["to"] == target and any(
                    b.get("kind") == "mention" for b in link.get("bindings", ()))
                    for link in compiled.link_types.values()):
                errors.append(f"document_fields.json: profile {profile.get('id')!r} field "
                              f"{spec.get('name')!r}: refers_to {spec['refers_to']!r} but "
                              f"{tid} declares no mention link to {target}")
    return errors


class SchemaLoadError(ValueError):
    """The schema files are invalid; ``errors`` lists every problem found."""

    def __init__(self, errors: Iterable[str]):
        self.errors = sorted(set(errors))
        super().__init__(f"{len(self.errors)} ontology schema error(s):\n  " +
                         "\n  ".join(self.errors[:40]))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- catalog

@dataclass(frozen=True)
class CatalogRelation:
    name: str
    columns: tuple[str, ...]
    key: tuple[str, ...] | None
    since: Mapping[str, str] | None = None
    domain: str | None = None
    unavailable: str | None = None

    @property
    def served(self) -> bool:
        return self.unavailable is None and self.key is not None


@dataclass(frozen=True)
class Catalog:
    """A Portal reporting-API relation catalog (``GET /v1/relations`` shape)."""

    relations: Mapping[str, CatalogRelation]
    evidence_mode: str = "unspecified"

    def served(self) -> dict[str, CatalogRelation]:
        return {name: rel for name, rel in self.relations.items() if rel.served}

    @property
    def digest(self) -> str:
        """Order-independent hash of the catalog (tracked on each sync)."""
        return _sha256({name: {"columns": sorted(rel.columns),
                               "key": list(rel.key) if rel.key is not None else None,
                               "since": dict(rel.since) if rel.since else None,
                               "domain": rel.domain, "unavailable": rel.unavailable}
                        for name, rel in self.relations.items()})


def catalog_from_json(value: Any) -> Catalog:
    """Accept ``{"relations": [entry, ...]}`` (``/v1/relations``, catalog snapshot) or
    ``{"relations": {name: entry}}`` (mirror/live catalog fixtures). Entries need
    ``columns`` and ``key``; ``since``, ``domain`` and ``unavailable`` are optional."""
    if not isinstance(value, dict) or "relations" not in value:
        raise ValueError("catalog must be an object with 'relations'")
    rows = value["relations"]
    if isinstance(rows, dict):
        items = [(name, entry) for name, entry in rows.items()]
    elif isinstance(rows, list):
        items = [(entry.get("name") if isinstance(entry, dict) else None, entry)
                 for entry in rows]
    else:
        raise ValueError("catalog 'relations' must be a list or an object")
    relations: dict[str, CatalogRelation] = {}
    for name, entry in items:
        if not isinstance(name, str) or _QUALIFIED.fullmatch(name) is None:
            raise ValueError(f"catalog relation name invalid: {name!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"catalog entry {name} is not an object")
        columns = entry.get("columns")
        key = entry.get("key")
        unavailable = entry.get("unavailable")
        if (not isinstance(columns, list) or not columns or
                any(not isinstance(c, str) or _COLUMN.fullmatch(c) is None for c in columns) or
                len(set(columns)) != len(columns)):
            raise ValueError(f"catalog entry {name}: columns invalid")
        if key is not None and (not isinstance(key, list) or not key or
                                any(c not in columns for c in key) or
                                len(set(key)) != len(key)):
            raise ValueError(f"catalog entry {name}: key invalid")
        if unavailable is not None and not isinstance(unavailable, str):
            raise ValueError(f"catalog entry {name}: unavailable invalid")
        since = entry.get("since")
        if since is not None and (not isinstance(since, dict) or since.get("column") not in columns):
            raise ValueError(f"catalog entry {name}: since invalid")
        if name in relations:
            raise ValueError(f"catalog relation {name} listed twice")
        relations[name] = CatalogRelation(
            name=name, columns=tuple(columns), key=tuple(key) if key else None,
            since=dict(since) if since else None,
            domain=entry.get("domain") if isinstance(entry.get("domain"), str) else None,
            unavailable=unavailable)
    mode = value.get("evidence_mode", value.get("_evidence_mode", "unspecified"))
    return Catalog(relations=relations, evidence_mode=str(mode))


def load_catalog(path: str | Path = CATALOG_SNAPSHOT_PATH) -> Catalog:
    return catalog_from_json(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- documents

def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_files(root: str | Path = SCHEMA_DIR, *,
                 include_pending: bool = False) -> list[Path]:
    """Domain files: ``domains/*.json`` and ``generated/*.json`` (top level only), plus
    ``pending/*.json`` when ``include_pending``."""
    root = Path(root)
    out: list[Path] = []
    for sub in ("domains", "generated") + (("pending",) if include_pending else ()):
        directory = root / sub
        if directory.is_dir():
            out.extend(p for p in sorted(directory.glob("*.json"))
                       if p.is_file() and not p.name.startswith("_"))
    return out


def load_documents(root: str | Path = SCHEMA_DIR, *,
                   include_pending: bool = False) -> list[tuple[str, Any]]:
    root = Path(root)
    return [(p.relative_to(root).as_posix(), _read_json(p))
            for p in schema_files(root, include_pending=include_pending)]


def _is_pending(name: str) -> bool:
    return name.split("/", 1)[0] == "pending"


def identifier_kinds(identifiers: Any) -> set[str]:
    """Identifier kind names declared by ``identifiers.json`` (``kinds`` object or list)."""
    kinds = identifiers.get("kinds") if isinstance(identifiers, dict) else None
    if isinstance(kinds, dict):
        return set(kinds)
    if isinstance(kinds, list):
        return {k.get("id") or k.get("name") for k in kinds if isinstance(k, dict)} - {None}
    raise ValueError("identifiers.json has no 'kinds'")


def link_contract_ids(contracts: Any) -> set[str]:
    rows = contracts.get("contracts") if isinstance(contracts, dict) else None
    if not isinstance(rows, list):
        raise ValueError("link contracts file has no 'contracts' list")
    return {row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}


# --------------------------------------------------------------------------- compile

@dataclass
class CompiledSchema:
    object_types: dict[str, dict] = field(default_factory=dict)
    link_types: dict[str, dict] = field(default_factory=dict)
    event_types: dict[str, dict] = field(default_factory=dict)
    action_types: dict[str, dict] = field(default_factory=dict)
    bindings: dict[str, tuple[str, dict]] = field(default_factory=dict)
    domains: dict[str, dict] = field(default_factory=dict)
    origin_of_type: dict[str, str] = field(default_factory=dict)
    shadowed: dict[str, str] = field(default_factory=dict)
    excluded_relations: dict[str, str] = field(default_factory=dict)
    pinned_catalog_digest: str | None = None
    identifiers_digest: str | None = None
    warnings: list[str] = field(default_factory=list)
    # link id -> {binding index: scope}; today only SAME_BC_COMPANY (link_company_scope).
    company_scope: dict[str, dict[int, str]] = field(default_factory=dict)
    # object type id -> pending file whose declaration replaced its not_served placeholder.
    pending_types: dict[str, str] = field(default_factory=dict)
    digest: str = ""

    def portal_bindings(self) -> dict[str, list[tuple[str, dict]]]:
        """relation -> [(object type id, binding)] for portal_api bindings."""
        out: dict[str, list[tuple[str, dict]]] = {}
        for type_id, binding in self.bindings.values():
            if binding["source"] == "portal_api":
                out.setdefault(binding["relation"], []).append((type_id, binding))
        return out


def resolve_evidence_refs(doc: Mapping[str, Any]) -> tuple[dict, list[str]]:
    """Copy of ``doc`` with every ``{"kind": "ref", "id": ...}`` evidence replaced by the
    file's ``evidence_catalog`` entry (C0 format); returns (resolved, unknown ids).
    The catalog itself is dropped: a ref and its inline entry mean the same thing."""
    entries = doc.get("evidence_catalog", {})
    missing: list[str] = []

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"kind", "id"} and value.get("kind") == "ref":
                if value["id"] not in entries:
                    missing.append(value["id"])
                    return value
                return walk(entries[value["id"]])
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    resolved = walk({k: v for k, v in doc.items() if k != "evidence_catalog"})
    return resolved, sorted(set(missing))


def _ref(ref: Any) -> tuple[str | None, str]:
    """(via_link or None, path) of a C0 ref_path (a path or {via_link, path})."""
    if isinstance(ref, dict):
        return ref["via_link"], ref["path"]
    return None, ref


def _binding_lineages(binding: Mapping[str, Any]) -> set[str]:
    lineage = binding["lineage"]
    if "from_capture" in lineage:
        return {"bc_*"}
    return {lineage["default"], *(r["lineage"] for r in lineage.get("rules", []))}


def _lineage_matches(produced: set[str], lineage: str) -> bool:
    return lineage in produced or ("bc_*" in produced and lineage.startswith("bc_"))


def _lineages_meet(one: set[str], other: set[str]) -> bool:
    """Whether two bindings can produce records of one lineage."""
    return bool(one & other) or any(_lineage_matches(one, lin) for lin in other) or \
        any(_lineage_matches(other, lin) for lin in one)


def _column(path: str) -> str:
    return path.split(".", 1)[0]


def _is_bc_capture(binding: Mapping[str, Any] | None) -> bool:
    return (binding is not None and binding.get("source") == "bc" and
            "from_capture" in binding.get("lineage", {}))


def _cites_unit_contract(*holders: Mapping[str, Any]) -> bool:
    return any(e.get("kind") == "workbench_code" and e.get("path") == UNIT_CONTRACTS_EVIDENCE
               for h in holders for e in h.get("evidence", []) if isinstance(e, dict))


def link_company_scope(link_binding: Mapping[str, Any],
                       bindings: Mapping[str, tuple[str, Mapping[str, Any]]]) -> str | None:
    """``SAME_BC_COMPANY`` when both ends of a link binding are BC captures, else None.

    BC numbers (document No., customer/vendor/item No.) are unique only within one
    company, and the same number recurs across books (masters synced to several
    books, consolidation copies, legacy auto-numbering). A projector must therefore
    pair BC records only within one capture company (``bc_<book>`` equal on both
    ends); a match across companies is no link. ``bindings`` maps binding id ->
    (type id, binding), as ``CompiledSchema.bindings``."""
    ends = [bindings.get(link_binding.get(k, "")) for k in ("from_binding", "to_binding")]
    if all(end is not None and _is_bc_capture(end[1]) for end in ends):
        return SAME_BC_COMPANY
    return None


def bc_company_not_known(link_binding: Mapping[str, Any],
                         bindings: Mapping[str, tuple[str, Mapping[str, Any]]]) -> bool:
    """Whether a link binding joins a BC capture to a record of another source
    without knowing the BC company: some BC end is reached on a number, not on its
    binding's whole key.

    BC numbers (document No., customer/vendor/item No.) recur across companies, and a
    Portal row does not serve the book it was posted to (``bc-link-company-scope``), so such a pair names that number in every company
    that holds it: a link is at most a ``lead`` until the book is served. A pair on
    the BC binding's key (its system id; a key answering in two companies is dropped
    as ``binding_key_not_unique``) names one record of one company. ``bindings`` as
    for :func:`link_company_scope`."""
    via = link_binding.get("via") if isinstance(link_binding.get("via"), Mapping) else None
    names = {"from": link_binding.get("from_binding"), "to": link_binding.get("to_binding")}
    if via is not None:
        names["via"] = via.get("binding")
    ends = {role: bindings.get(name or "") for role, name in names.items()}
    bc = [role for role, end in ends.items() if end is not None and _is_bc_capture(end[1])]
    if not bc or len(bc) == len(ends):
        return False
    if via is None:
        pairs = list(link_binding.get("pairs") or ())
        compared = {"from": {p.get("from") for p in pairs}, "to": {p.get("to") for p in pairs}}
    else:
        into, onward = list(via.get("from_pairs") or ()), list(via.get("to_pairs") or ())
        compared = {"from": {p.get("from") for p in into},
                    "via": {p.get("to") for p in into} | {p.get("from") for p in onward},
                    "to": {p.get("to") for p in onward}}
    return any(not ends[role][1].get("key")
               or not set(ends[role][1]["key"]) <= compared[role] for role in bc)


def stored_key_compare_errors(name: str, doc: Any) -> list[str]:
    """A pair on a stored-key binding (fk/json_path/same_record) that does not compare
    a stored key, named by link and binding. Checked on the raw document so the error
    names the binding even though the C0 format rejects the same file by JSON path."""
    out: list[str] = []
    links = doc.get("link_types") if isinstance(doc, Mapping) else None
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, Mapping):
            continue
        for i, lb in enumerate(link.get("bindings") or []):
            if not isinstance(lb, Mapping) or lb.get("kind") not in STORED_KEY_KINDS:
                continue
            via = lb.get("via") if isinstance(lb.get("via"), Mapping) else {}
            for group in ("pairs", "from_pairs", "to_pairs"):
                holder = lb if group == "pairs" else via
                pairs = holder.get(group)
                for pair in pairs if isinstance(pairs, list) else []:
                    if not isinstance(pair, Mapping) or \
                            pair.get("compare") in STORED_KEY_COMPARES:
                        continue
                    compared = (f"compares {pair.get('compare')!r}"
                                if "compare" in pair
                                else "has no compare")
                    out.append(
                        f"{name}: {link.get('id')} binding {i} "
                        f"({lb.get('from_binding')} -> {lb.get('to_binding')}): {lb['kind']} "
                        f"{'via ' if holder is via else ''}pair {pair.get('from')!r} -> "
                        f"{pair.get('to')!r} {compared}; a stored-key "
                        f"binding compares integer_id, text_id or identifier (text is "
                        f"for text_match bindings only)")
    return out


def compile_documents(documents: Iterable[tuple[str, Any]], *,
                      catalog: Catalog | None = None,
                      identifiers: Any = None,
                      link_contracts: Any = None,
                      coverage: Any = None,
                      format_schema: Any = None,
                      check_format: bool = True,
                      document_fields: Any = "default") -> CompiledSchema:
    """Validate and compile domain documents; raise ``SchemaLoadError`` on any error.

    ``document_fields``: the parsed profile file the document types' field properties are
    derived from (``derive_document_types``), ``"default"`` to read the committed
    ``DOCUMENT_FIELDS_PATH``, ``None`` when absent (a ``documentFields`` binding then fails).

    ``check_format=False`` skips the per-file C0 format check (most of the cost) for
    documents already format-checked by the gate; every cross-file rule still runs."""
    fmt = format_schema if format_schema is not None else _read_json(FORMAT_PATH)
    json_contract.check_schema(fmt)
    if document_fields == "default":
        document_fields = _read_json(DOCUMENT_FIELDS_PATH)
    errors: list[str] = []
    compiled = CompiledSchema()
    docs: list[tuple[str, dict]] = []
    mention_homes: list[tuple[Mapping[str, Any], Mapping[str, Any], list[str], str]] = []
    for name, doc in documents:
        doc, derive_errors = derive_document_types(name, doc, document_fields)
        errors.extend(derive_errors)
        errors.extend(stored_key_compare_errors(name, doc))
        problems = json_contract.validate(doc, fmt, root=fmt, limit=20) if check_format else []
        if problems:
            errors.extend(f"{name}: format: {p}" for p in problems)
            continue
        expected = {"domains": "curated", "generated": "generated",
                    "pending": "curated"}.get(name.split("/", 1)[0])
        if expected and doc["origin"] != expected:
            errors.append(f"{name}: origin {doc['origin']!r} but the file is under "
                          f"{name.split('/', 1)[0]}/ (expected {expected!r})")
        stem = Path(name).stem
        if "/" in name and stem != doc["domain"]:
            errors.append(f"{name}: domain {doc['domain']!r} differs from the file name {stem!r}")
        if doc["domain"] in compiled.domains:
            errors.append(f"{name}: domain {doc['domain']!r} declared twice")
            continue
        for rule in MENTION_LINK_RULES:
            if not any(t.get("id") == rule["from"] for t in doc.get("object_types", [])):
                continue
            unknown = [r for r in rule["evidence"] if r not in doc.get("evidence_catalog", {})]
            errors.extend(f"{name}: unknown evidence ref {ref!r} (mention link rule)"
                          for ref in unknown)
            mention_homes.append((rule, doc.get("evidence_catalog", {}),
                                  [t["id"] for t in doc.get("object_types", [])], name))
        doc, missing = resolve_evidence_refs(doc)
        errors.extend(f"{name}: unknown evidence ref {ref!r}" for ref in missing)
        compiled.domains[doc["domain"]] = {"origin": doc["origin"],
                                           "upstream_pins": doc.get("upstream_pins", {}),
                                           "file": name}
        docs.append((name, doc))

    # Shadowing: a curated binding hides the generated raw type bound to the same source
    # relation (a Portal relation, a BC entity set, ...).
    curated_relations = {(b["source"], b["relation"]) for _, d in docs if d["origin"] == "curated"
                         for t in d.get("object_types", []) for b in t["bindings"]}
    for _, doc in docs:
        if doc["origin"] != "generated":
            continue
        for t in doc.get("object_types", []):
            hit = sorted(b["relation"] for b in t["bindings"]
                         if (b["source"], b["relation"]) in curated_relations)
            if hit:
                compiled.shadowed[t["id"]] = "curated binding on " + ", ".join(hit)

    # Collect, with global id uniqueness.
    def put(kind: str, table: dict, item_id: str, item: dict, name: str) -> None:
        if item_id in table:
            errors.append(f"{name}: {kind} {item_id!r} declared twice")
        else:
            table[item_id] = item

    # Pending files last: their declaration replaces a curated not_served placeholder.
    docs.sort(key=lambda item: _is_pending(item[0]))
    for name, doc in docs:
        for t in doc.get("object_types", []):
            if t["id"] in compiled.shadowed:
                continue
            held = compiled.object_types.get(t["id"])
            if _is_pending(name) and held is not None and not held["bindings"] and \
                    held.get("availability", {}).get("state") == "not_served" and \
                    compiled.origin_of_type.get(t["id"]) == "curated" and \
                    t["id"] not in compiled.pending_types:
                inherited = set(held.get("markings", ()))
                if inherited - set(t.get("markings", ())):
                    # The placeholder's markings hold the curated access decision.
                    t = {**t, "markings": sorted(inherited | set(t.get("markings", ())))}
                compiled.object_types[t["id"]] = t
                compiled.pending_types[t["id"]] = name
                for b in t["bindings"]:
                    if b["id"] in compiled.bindings:
                        errors.append(f"{name}: binding {b['id']!r} declared twice")
                    else:
                        compiled.bindings[b["id"]] = (t["id"], b)
                continue
            put("object type", compiled.object_types, t["id"], t, name)
            compiled.origin_of_type[t["id"]] = doc["origin"]
            for b in t["bindings"]:
                if b["id"] in compiled.bindings:
                    errors.append(f"{name}: binding {b['id']!r} declared twice")
                else:
                    compiled.bindings[b["id"]] = (t["id"], b)
        for kind, key, table in (("link type", "link_types", compiled.link_types),
                                 ("event type", "event_types", compiled.event_types),
                                 ("action type", "action_types", compiled.action_types)):
            for item in doc.get(key, []):
                ends = ({item["from"], item["to"]} if key == "link_types" else
                        {item["object_type"]} if key == "event_types" else {item["target"]})
                dropped = sorted(ends & set(compiled.shadowed))
                if dropped and doc["origin"] == "generated":
                    compiled.shadowed[item["id"]] = "endpoint shadowed: " + ", ".join(dropped)
                    continue
                put(kind, table, item["id"], item, name)
    kind_specs = identifiers.get("kinds") if isinstance(identifiers, dict) else None
    copy_kinds = {name for name, spec in (kind_specs or {}).items()
                  if isinstance(spec, dict) and spec.get("copies")} \
        if isinstance(kind_specs, dict) else set()
    for rule, catalog_entries, home_types, home in mention_homes:
        for item in mention_links(compiled.object_types, home_types, catalog_entries, rule,
                                  copy_kinds):
            put("link type", compiled.link_types, item["id"], item,
                f"{home} (generated by MENTION_LINK_RULES)")
    pending_relations = {b["relation"]: b["id"] for name, doc in docs if _is_pending(name)
                         for t in doc.get("object_types", []) for b in t["bindings"]
                         if b["source"] == "portal_api"}
    if pending_relations and isinstance(coverage, dict):
        for item in pending_links(compiled, pending_relations,
                                  coverage.get("prisma_relations_not_bound", [])):
            put("link type", compiled.link_types, item["id"], item,
                "generated/meta/coverage.json (pending_links)")

    errors.extend(_attests_errors(compiled, document_fields))
    errors.extend(_reference_errors(compiled, document_fields, identifiers))

    kinds: set[str] | None = None
    if identifiers is not None:
        try:
            kinds = identifier_kinds(identifiers)
        except ValueError as exc:
            errors.append(f"identifiers.json: {exc}")
        compiled.identifiers_digest = _sha256(identifiers)
    contract_ids = link_contract_ids(link_contracts) if link_contracts is not None else None
    if isinstance(coverage, dict):
        compiled.excluded_relations = dict(coverage.get("excluded_relations", {}))
        compiled.pinned_catalog_digest = coverage.get("catalog_digest")

    def check_kind(where: str, kind: str | None) -> None:
        if kind is None:
            return
        if kinds is None:
            compiled.warnings.append(f"{where}: identifier kind {kind!r} unchecked "
                                     "(ontology/schema/identifiers.json absent)")
        elif kind not in kinds:
            errors.append(f"{where}: unknown identifier kind {kind!r}")

    def check_columns(where: str, binding: Mapping[str, Any], paths: Iterable[str]) -> None:
        if catalog is None or binding["source"] != "portal_api":
            return
        rel = catalog.relations.get(binding["relation"])
        if rel is None:
            return  # reported once per binding below
        for path in paths:
            if _column(path) not in rel.columns:
                errors.append(f"{where}: unknown column {_column(path)!r} of "
                              f"{binding['relation']} (path {path!r})")

    def hop(where: str, tid: str, binding: Mapping[str, Any], via_link: str) -> list[dict]:
        """Bindings reached by one declared N:1/1:1 hop of ``tid`` starting at ``binding``."""
        link = compiled.link_types.get(via_link)
        if link is None:
            errors.append(f"{where}: via_link {via_link!r} is not a declared link type")
            return []
        if link["from"] != tid:
            errors.append(f"{where}: via_link {via_link!r} starts at {link['from']}, not {tid}")
            return []
        if link["cardinality"] not in ("N:1", "1:1"):
            errors.append(f"{where}: via_link {via_link!r} is {link['cardinality']}; one hop "
                          "must be N:1 or 1:1")
            return []
        targets = []
        for lb in link["bindings"]:
            if lb.get("from_binding") == binding["id"] and lb.get("to_binding") in compiled.bindings:
                targets.append(compiled.bindings[lb["to_binding"]][1])
        if not targets:
            errors.append(f"{where}: via_link {via_link!r} has no binding starting at "
                          f"{binding['id']}")
        return targets

    def check_ref(where: str, tid: str, binding: Mapping[str, Any], ref: Any
                  ) -> list[tuple[str, str]]:
        """Check a ref_path; (binding id, path) of each cell it reads."""
        via, path = _ref(ref)
        if via is None:
            check_columns(where, binding, [path])
            return [(binding["id"], path)]
        found = []
        for target in hop(where, tid, binding, via):
            check_columns(where + f" via {via}", target, [path])
            found.append((target["id"], path))
        return found

    def check_predicates(where: str, tid: str, binding: Mapping[str, Any],
                         predicates: Iterable[Mapping[str, Any]]) -> None:
        for w in predicates:
            check_ref(where, tid, binding,
                      {"via_link": w["via_link"], "path": w["path"]} if "via_link" in w
                      else w["path"])

    # Lineage inheritance: one declared hop per binding; no cycles.
    inherits: dict[str, list[str]] = {}
    for tid0, t0 in compiled.object_types.items():
        for b0 in t0["bindings"]:
            if "inherit" in b0["lineage"]:
                inherits[b0["id"]] = [x["id"] for x in hop(
                    f"{tid0} binding {b0['id']} lineage inherit", tid0, b0,
                    b0["lineage"]["inherit"])]
    cyclic: set[str] = set()
    for start in sorted(inherits):
        path, node = [start], start
        while True:
            nxt = [n for n in inherits.get(node, [])]
            if not nxt:
                break
            node = nxt[0]
            if node in path:
                cycle = path[path.index(node):] + [node]
                if not cyclic & set(cycle):
                    errors.append("lineage inheritance cycle: " + " -> ".join(cycle))
                cyclic.update(cycle)
                break
            path.append(node)

    def lineages_of(binding: Mapping[str, Any], seen: frozenset = frozenset()) -> set[str]:
        out = _binding_lineages(binding)
        if binding["id"] in seen or binding["id"] in cyclic:
            return out
        for target in inherits.get(binding["id"], []):
            out |= lineages_of(compiled.bindings[target][1], seen | {binding["id"]})
        return out

    # Object types.
    currency_cells: set[tuple[str, str]] = set()   # (binding id, path) a currency_from reads
    for tid, t in compiled.object_types.items():
        own = {b["id"]: b for b in t["bindings"]}
        props = {}
        for p in t["properties"]:
            if p["name"] in props:
                errors.append(f"{tid}: property {p['name']!r} declared twice")
            props[p["name"]] = p
        for b in t["bindings"]:
            where = f"{tid} binding {b['id']}"
            if catalog is not None and b["source"] == "portal_api":
                rel = catalog.relations.get(b["relation"])
                if rel is None:
                    errors.append(f"{where}: unknown relation {b['relation']!r} "
                                  "(not in the reporting-API catalog)")
                elif not rel.served:
                    errors.append(f"{where}: relation {b['relation']!r} is not served "
                                  f"({rel.unavailable or 'no key'})")
            check_columns(where + " key", b, b["key"])
            check_predicates(where + " where", tid, b, b.get("where", []))
            for rule in b["lineage"].get("rules", []):
                check_predicates(where + " lineage rule", tid, b, rule["when"])
            stands = b.get("stands_for")
            if stands is not None:
                target = own.get(stands["binding"])
                if target is None or target.get("stands_for") is not None \
                        or target["source"] != b["source"]:
                    errors.append(f"{where}: stands_for {stands['binding']!r} is not a "
                                  f"binding of {tid} of the same source that stands for none")
                    continue
                compared = [(p["to"], p["compare"], p.get("identifier_kind"))
                            for p in stands["pairs"]]
                for pair in stands["pairs"]:
                    check_kind(where + " stands_for", pair.get("identifier_kind"))
                    check_columns(where + " stands_for", b, [pair["from"]])
                    check_columns(where + " stands_for", target, [pair["to"]])
                for other in t["bindings"]:
                    if other is b:
                        break
                    if (other.get("stands_for") or {}).get("binding") == stands["binding"] and [
                            (p["to"], p["compare"], p.get("identifier_kind"))
                            for p in other["stands_for"]["pairs"]] != compared:
                        errors.append(f"{where}: stands_for compares {stands['binding']!r} "
                                      f"otherwise than {other['id']!r}")

        type_markings = set(t.get("markings", []))
        for p in t["properties"]:
            where = f"{tid}.{p['name']}"
            check_kind(where, p.get("identifier_kind"))
            if p["value_type"] == "money" and \
                    "finance" not in type_markings | set(p.get("markings", [])):
                errors.append(f"{where}: money must carry the finance marking "
                              "(on the property or its object type)")
            for holder in [p] + list(p["sources"]):
                label = where if holder is p else f"{where} source {holder['binding']}"
                for key in ("unit", "per_unit"):
                    unit = holder.get(key)
                    if unit is None:
                        continue
                    if unit.lower() in PLACEHOLDER_UNITS:
                        errors.append(f"{label}: {key} {unit!r} is a placeholder; read it "
                                      f"with {key}_from or declare {key}_unknown")
                    elif unit not in LITERAL_UNITS and not _cites_unit_contract(holder, p):
                        errors.append(f"{label}: {key} {unit!r} is not a known literal unit "
                                      "(LITERAL_UNITS) and no unit contract states it; read "
                                      f"it with {key}_from or declare {key}_unknown")
            names = {q["name"]: q for q in t["properties"]}
            for other in p.get("placeholder_when_equal_to", []):
                q = names.get(other)
                if q is None or q["value_type"] != "identifier" or other == p["name"]:
                    errors.append(f"{where}: placeholder_when_equal_to names {other!r}, not "
                                  f"another identifier property of {tid}")
                elif not {s["binding"] for s in q["sources"]} & \
                        {s["binding"] for s in p["sources"]}:
                    errors.append(f"{where}: placeholder_when_equal_to {other!r} shares no "
                                  "source binding with the property")
            for i, rule in enumerate(p.get("authority", {}).get("precedence", [])):
                for lin in sorted(set(rule["lineage"]) & NEVER_AUTHORITY):
                    errors.append(f"{where}: authority rule {i} names {lin!r}, which is "
                                  "never authority (consolidation copies)")
            source_bindings = []
            for s in p["sources"]:
                b = own.get(s["binding"])
                if b is None:
                    errors.append(f"{where}: source binding {s['binding']!r} is not a binding "
                                  f"of {tid}")
                    continue
                source_bindings.append(b)
                if p.get("availability", "served") == "served":
                    check_columns(where, b, [s["path"]])
                    check_predicates(f"{where} source {s['binding']} when", tid, b,
                                     s.get("when", []))
                    for k in ("unit_from", "currency_from"):
                        # A source's own unit/currency overrides the property default.
                        override = any(k.replace("_from", x) in s for x in ("", "_from", "_unknown"))
                        read: list[tuple[str, str]] = []
                        if k in s:
                            read = check_ref(f"{where} source {s['binding']} {k}", tid, b, s[k])
                        elif k in p and not override:
                            read = check_ref(f"{where} {k}", tid, b, p[k])
                        if k == "currency_from":
                            currency_cells.update(read)
                    for k in ("per_unit_from", "tax_basis_from"):
                        if k in s:
                            check_ref(f"{where} source {s['binding']} {k}", tid, b, s[k])
                    rules = s.get("per_unit_rules", [])
                    for rule in rules:
                        # Rules read the record itself, never a hop (an incremental
                        # build re-reads a value only when its own record changes).
                        label = f"{where} source {s['binding']} per_unit_rules {rule['id']}"
                        if any("via_link" in w for w in rule.get("when", [])):
                            errors.append(f"{label}: a rule tests the record itself, "
                                          "never via_link")
                        check_predicates(label, tid, b, rule.get("when", []))
                        for path in rule.get("agreeing", []):
                            check_ref(label, tid, b, path)
                        unit = rule.get("per_unit")
                        if unit is not None and unit not in LITERAL_UNITS:
                            errors.append(f"{label}: per_unit {unit!r} is not a known "
                                          "literal unit (LITERAL_UNITS)")
                    if len({r["id"] for r in rules}) != len(rules):
                        errors.append(f"{where} source {s['binding']}: per_unit_rules "
                                      "ids repeat")
                for key in ("per_unit_unknown_by_lineage", "tax_basis_by_lineage"):
                    for lin in sorted(s.get(key, ())):
                        if not _lineage_matches(lineages_of(b), lin):
                            errors.append(f"{where} source {s['binding']}: {key} names "
                                          f"lineage {lin!r} that the binding never produces")
            if "authority" in p and source_bindings:
                produced = {b["id"]: lineages_of(b) for b in source_bindings}
                for i, rule in enumerate(p["authority"]["precedence"]):
                    if "binding" in rule:
                        if rule["binding"] not in produced:
                            errors.append(f"{where}: authority rule {i} names binding "
                                          f"{rule['binding']!r}, not a source of the property")
                            continue
                        scope = {rule["binding"]: produced[rule["binding"]]}
                    else:
                        scope = produced
                    for lin in rule["lineage"]:
                        hits = [bid for bid, lins in scope.items() if _lineage_matches(lins, lin)]
                        if not hits:
                            errors.append(f"{where}: authority rule {i} names lineage {lin!r} "
                                          "that no source binding produces")
                # A lineage left out of the precedence is never authority: its values are
                # still shown and conflicts reported (C0: authority picks the displayed
                # value; it does not have to rank every lineage). Reported as a warning,
                # except gold_native_staging, which C0 admits as authority only with a
                # human confirmation, so leaving it out is the expected case.
                for bid in sorted(produced):
                    ranked = {lin for rule in p["authority"]["precedence"]
                              if rule.get("binding", bid) == bid for lin in rule["lineage"]}
                    gaps = sorted(lin for lin in produced[bid] - NEVER_AUTHORITY_UNCONFIRMED
                                  if lin != "bc_*" and lin not in ranked)
                    if "bc_*" in produced[bid] and not any(r.startswith("bc_") for r in ranked):
                        gaps.append("bc_<company>")
                    if gaps:
                        compiled.warnings.append(f"{where}: lineages {gaps} of source binding "
                                                 f"{bid!r} are never authority (unranked)")
        for key in t.get("identity_keys", []):
            check_kind(f"{tid} identity key {key['name']}", key.get("identifier_kind"))
            for name in key["properties"]:
                if name not in props:
                    errors.append(f"{tid} identity key {key['name']}: unknown property {name!r}")
        for name in t.get("display", []):
            if name not in props:
                errors.append(f"{tid} display: unknown property {name!r}")

    # local_currency, on any binding: its blank cell is a currency a currency_from reads
    # there, and the record holding the local currency shares a lineage with it (the
    # projector reads the one such record in the holder's lineage, a BC company's).
    for tid, t in compiled.object_types.items():
        for b in t["bindings"]:
            local = b.get("local_currency")
            if local is None:
                continue
            where = f"{tid} binding {b['id']} local_currency"
            target = (compiled.bindings.get(local["binding"]) or (None, None))[1]
            if (b["id"], local["blank"]) not in currency_cells:
                errors.append(f"{where}: blank {local['blank']!r} is not a currency field of "
                              "the binding (no currency_from reads it)")
            if target is None:   # not compiled in (curated files alone): never read
                compiled.warnings.append(f"{where}: binding {local['binding']!r} is not "
                                         "loaded; a blank currency stays unresolved")
            elif not _lineages_meet(lineages_of(b), lineages_of(target)):
                errors.append(f"{where}: {local['binding']!r} shares no lineage with the "
                              "binding, so none of its records is in a record's scope")
            else:
                check_columns(where, b, [local["blank"]])
                check_columns(where, target, [local["path"]])

    # Link types.
    for lid, link in compiled.link_types.items():
        if not lid.startswith(link["from"] + "."):
            errors.append(f"{lid}: id must start with its from type {link['from']!r}")
        for end in ("from", "to"):
            if link[end] not in compiled.object_types:
                errors.append(f"{lid}: {end} type {link[end]!r} does not exist")
        for i, lb in enumerate(link["bindings"]):
            where = f"{lid} binding {i}"
            ends = {}
            for end in ("from", "to"):
                bid = lb.get(f"{end}_binding")
                if bid is None:
                    continue
                owner = compiled.bindings.get(bid)
                if owner is None:
                    errors.append(f"{where}: {end}_binding {bid!r} does not exist")
                elif owner[0] != link[end]:
                    errors.append(f"{where}: {end}_binding {bid!r} belongs to {owner[0]}, "
                                  f"not {link[end]}")
                else:
                    ends[end] = owner[1]
            for pair in lb.get("pairs", []):
                check_kind(where, pair.get("identifier_kind"))
                if "from" in ends:
                    check_columns(where + " pair", ends["from"], [pair["from"]])
                if "to" in ends:
                    check_columns(where + " pair", ends["to"], [pair["to"]])
            via = lb.get("via")
            if via is not None:
                owner = compiled.bindings.get(via["binding"])
                if owner is None:
                    errors.append(f"{where}: via binding {via['binding']!r} does not exist")
                else:
                    for pair in via["from_pairs"]:
                        check_kind(where, pair.get("identifier_kind"))
                        if "from" in ends:
                            check_columns(where + " via", ends["from"], [pair["from"]])
                        check_columns(where + " via", owner[1], [pair["to"]])
                    for pair in via["to_pairs"]:
                        check_kind(where, pair.get("identifier_kind"))
                        check_columns(where + " via", owner[1], [pair["from"]])
                        if "to" in ends:
                            check_columns(where + " via", ends["to"], [pair["to"]])
            if "link_contract" in lb and contract_ids is not None and \
                    lb["link_contract"] not in contract_ids:
                errors.append(f"{where}: unknown link contract {lb['link_contract']!r}")
            scope = link_company_scope(lb, compiled.bindings)
            if scope is not None:
                compiled.company_scope.setdefault(lid, {})[i] = scope

    # Navigation names are unique per type: a link's name on its from type, its
    # reverse_name on its to type (tools walk links by these names).
    nav: dict[tuple[str, str], str] = {}
    for lid in sorted(compiled.link_types):
        link = compiled.link_types[lid]
        for end, key in (("from", "name"), ("to", "reverse_name")):
            slot = (link[end], link[key])
            if slot in nav and nav[slot] != lid:
                errors.append(f"{lid}: navigation name {link[key]!r} on {link[end]} is "
                              f"also used by {nav[slot]}")
            nav.setdefault(slot, lid)

    # Event types.
    for eid, ev in compiled.event_types.items():
        t = compiled.object_types.get(ev["object_type"])
        if not eid.startswith(ev["object_type"] + "."):
            errors.append(f"{eid}: id must start with its object type {ev['object_type']!r}")
        if t is None:
            errors.append(f"{eid}: object type {ev['object_type']!r} does not exist")
        else:
            names = {p["name"] for p in t["properties"]}
            if "observed_property" in ev and ev["observed_property"] not in names:
                errors.append(f"{eid}: observed_property {ev['observed_property']!r} unknown")
            if "time" in ev:
                owner = compiled.bindings.get(ev["time"]["binding"])
                if owner is None or owner[0] != ev["object_type"]:
                    errors.append(f"{eid}: time binding {ev['time']['binding']!r} is not a "
                                  f"binding of {ev['object_type']}")
                else:
                    check_columns(eid + " time", owner[1], [ev["time"]["path"]])
        for q in ev["qualifiers"]:
            if q["object_type"] not in compiled.object_types:
                errors.append(f"{eid}: qualifier object type {q['object_type']!r} does not exist")
            if q["via"] == "self":
                if q["object_type"] != ev["object_type"]:
                    errors.append(f"{eid}: qualifier {q['qualifier']!r} via self must be "
                                  f"{ev['object_type']}")
                continue
            link = compiled.link_types.get(q["via"])
            if link is None:
                errors.append(f"{eid}: qualifier via link {q['via']!r} does not exist")
            elif {link["from"], link["to"]} != {ev["object_type"], q["object_type"]} and \
                    not (link["from"] == link["to"] == ev["object_type"] == q["object_type"]):
                errors.append(f"{eid}: link {q['via']} does not connect {ev['object_type']} "
                              f"and {q['object_type']}")

    # Action types.
    for aid, act in compiled.action_types.items():
        target = compiled.object_types.get(act["target"])
        if target is None:
            errors.append(f"{aid}: target type {act['target']!r} does not exist")
        owner_write = act["executor"] == "human_in_owner_system"
        for pname, param in act["params"].items():
            if owner_write and param["type"] in OWNER_REFERENCE_PARAMS \
                    and "user_objective" in param["provenance"]:
                errors.append(f"{aid}.{pname}: an owner-system write takes this {param['type']} "
                              "only from a cited value or a schema constant, never the user "
                              "objective")
            if "object_type" in param and param["object_type"] not in compiled.object_types:
                errors.append(f"{aid}.{pname}: object type {param['object_type']!r} does not exist")
            ref = param.get("property_ref")
            if ref:
                tname, prop = ref.split(".", 1)
                t = compiled.object_types.get(tname)
                if t is None or prop not in {p["name"] for p in t["properties"]}:
                    errors.append(f"{aid}.{pname}: property_ref {ref!r} does not exist")
        effect = act.get("expected_effect")
        if effect is not None:
            if target is not None and effect["property"] not in {p["name"] for p in target["properties"]}:
                errors.append(f"{aid}: expected_effect property {effect['property']!r} unknown")
            if effect["equals"].split(".", 1)[1] not in act["params"]:
                errors.append(f"{aid}: expected_effect names unknown {effect['equals']!r}")
        for item in act["idempotency"]:
            if item.startswith("params.") and item.split(".", 1)[1] not in act["params"]:
                errors.append(f"{aid}: idempotency names unknown {item!r}")

    # Explicit exclusions must name catalog relations and never a bound one.
    bound = compiled.portal_bindings()
    for rel_name in compiled.excluded_relations:
        if rel_name in bound:
            errors.append(f"coverage: relation {rel_name} is both bound and excluded")
        if catalog is not None and rel_name not in catalog.relations:
            errors.append(f"coverage: excluded relation {rel_name} is not in the catalog")

    if errors:
        raise SchemaLoadError(errors)
    compiled.warnings = sorted(set(compiled.warnings))
    compiled.digest = schema_digest(compiled)
    return compiled


# --------------------------------------------------------------------------- digest

def _sorted_set(item: dict, *keys: str) -> dict:
    out = dict(item)
    for key in keys:
        if key in out:
            out[key] = sorted(out[key])
    return out


def _canonical_type(t: Mapping[str, Any]) -> dict:
    out = _sorted_set(dict(t), "markings", "interfaces")
    out["bindings"] = sorted(t["bindings"], key=lambda b: b["id"])
    out["properties"] = sorted((_sorted_set(p, "markings") for p in t["properties"]),
                               key=lambda p: p["name"])
    if "identity_keys" in t:
        out["identity_keys"] = sorted(t["identity_keys"], key=lambda k: k["name"])
    return out


def _canonical_link(link: Mapping[str, Any]) -> dict:
    out = _sorted_set(dict(link), "markings")
    out["bindings"] = sorted(link["bindings"], key=canonical_json)
    return out


def schema_digest(compiled: CompiledSchema) -> str:
    """Stable digest of the compiled schema: same content gives the same digest
    whatever the file order, id-keyed list order or JSON formatting."""
    body = {
        "format": FORMAT,
        "domains": {name: {"origin": d["origin"], "upstream_pins": d["upstream_pins"]}
                    for name, d in compiled.domains.items()},
        "object_types": [_canonical_type(compiled.object_types[k])
                         for k in sorted(compiled.object_types)],
        "link_types": [_canonical_link(compiled.link_types[k]) for k in sorted(compiled.link_types)],
        "event_types": [_sorted_set(compiled.event_types[k], "markings")
                        for k in sorted(compiled.event_types)],
        "action_types": [_sorted_set(compiled.action_types[k], "markings")
                         for k in sorted(compiled.action_types)],
        "excluded_relations": compiled.excluded_relations,
        "identifiers": compiled.identifiers_digest,
    }
    return "sha256:" + _sha256(body)


# --------------------------------------------------------------------------- entry points

# Compiled schemas by the digest of everything they were compiled from (file bytes,
# catalog, parsed inputs, options): an edited file on disk is a new key, never a stale hit.
_LOADED: "OrderedDict[str, bytes]" = OrderedDict()  # key -> pickled CompiledSchema
_LOADED_MAX = 8
_LOADED_LOCK = threading.Lock()


def load_schema(root: str | Path = SCHEMA_DIR, *,
                catalog: Catalog | str | Path | None = None,
                identifiers: Any = "default",
                link_contracts: Any = "default",
                include_pending: bool = False,
                check_format: bool | None = None) -> CompiledSchema:
    """Load and validate every domain file under ``root``.

    ``include_pending`` also loads ``pending/*.json`` (bindings for relations not served
    yet); with a catalog that lacks their relations this is an error, as it should be.

    ``catalog``: a ``Catalog`` or a catalog JSON path to check bindings against
    (``None`` skips catalog checks). ``identifiers``/``link_contracts``: parsed JSON,
    or ``"default"`` to read ``<root>/identifiers.json`` (skipped with a warning when
    absent) and ``operating/link_contracts.json``.

    ``check_format``: check each file against the C0 format. ``None`` (default) checks
    every root except the committed ``SCHEMA_DIR``, whose files the ``ontology-schema``
    gate format-checks (``python3 -m ontology.schema``, ``gen_ontology_skeleton.py
    generate --check``); a committed file that breaks the compiler anyway is re-compiled
    with the check, so it fails as a ``SchemaLoadError``, not a crash.

    The result is memoized by the content digest of every input (re-read and re-hashed
    on each call), and each call returns its own copy: a caller's mutation never
    reaches another caller.
    """
    root = Path(root)
    if check_format is None:
        check_format = root.resolve() != Path(SCHEMA_DIR).resolve()
    if isinstance(catalog, (str, Path)):
        catalog = load_catalog(catalog)
    blobs = [(p.relative_to(root).as_posix(), p.read_bytes())
             for p in schema_files(root, include_pending=include_pending)]
    domain_names = [name for name, _ in blobs]

    def add(label: str, path: Path, wanted: bool = True) -> bytes | None:  # optional input
        data = path.read_bytes() if wanted and path.is_file() else None
        if data is not None:
            blobs.append((label, data))
        return data

    identifiers_raw = add("@identifiers", root / "identifiers.json", identifiers == "default")
    link_contracts_raw = None
    if link_contracts == "default":  # required: an absent file raises, as before
        link_contracts_raw = LINK_CONTRACTS_PATH.read_bytes()
        blobs.append(("@link_contracts", link_contracts_raw))
    coverage_raw = add("@coverage", root / "generated" / "meta" / "coverage.json")
    document_fields_raw = add("@document_fields", root / "document_fields.json")
    format_path = root / "_format.schema.json"
    format_raw = (format_path if format_path.is_file() else FORMAT_PATH).read_bytes()
    blobs.append(("@format", format_raw))
    try:
        digest = hashlib.sha256(canonical_json({
            "check_format": check_format, "include_pending": include_pending,
            "catalog": None if catalog is None else [catalog.digest, catalog.evidence_mode],
            "identifiers": "file" if identifiers == "default" else ["value", identifiers],
            "link_contracts": ("file" if link_contracts == "default"
                               else ["value", link_contracts])}).encode("utf-8"))
    except (TypeError, ValueError):  # parsed inputs that are not JSON: compile uncached
        digest = None
    if digest is not None:
        for label, data in blobs:  # length-prefixed: no two input sets hash alike
            digest.update(f"\0{label}\0{len(data)}\0".encode("utf-8"))
            digest.update(data)
    key = digest.hexdigest() if digest is not None else None
    if key is not None:
        with _LOADED_LOCK:
            hit = _LOADED.get(key)
            if hit is not None:
                _LOADED.move_to_end(key)
        if hit is not None:
            return pickle.loads(hit)

    def compile_(check: bool) -> CompiledSchema:
        return compile_documents(
            [(name, json.loads(data)) for name, data in blobs[:len(domain_names)]],
            catalog=catalog,
            identifiers=(json.loads(identifiers_raw) if identifiers_raw is not None else None)
            if identifiers == "default" else identifiers,
            link_contracts=json.loads(link_contracts_raw) if link_contracts == "default"
            else link_contracts,
            coverage=json.loads(coverage_raw) if coverage_raw is not None else None,
            format_schema=json.loads(format_raw), check_format=check,
            document_fields=json.loads(document_fields_raw)
            if document_fields_raw is not None else None)

    try:
        compiled = compile_(check_format)
    except SchemaLoadError:
        raise
    except Exception:
        if check_format:
            raise
        compiled = compile_(True)  # a malformed committed file: report its format errors
    # Callers get their own copy (an unpickle: a fresh object graph, 3x faster than
    # copy.deepcopy), so one caller's mutation never reaches another.
    blob = pickle.dumps(compiled, protocol=pickle.HIGHEST_PROTOCOL)
    if key is not None:
        with _LOADED_LOCK:
            _LOADED[key] = blob
            while len(_LOADED) > _LOADED_MAX:
                _LOADED.popitem(last=False)
    return pickle.loads(blob)


def pending_report(catalog: Catalog, root: str | Path = SCHEMA_DIR) -> list[dict[str, Any]]:
    """Every Portal relation a pending file binds, and whether ``catalog`` serves it now
    (then the file belongs in domains/; until then the relation is an upstream ask)."""
    out = []
    for name, doc in load_documents(root, include_pending=True):
        if not _is_pending(name):
            continue
        for t in doc.get("object_types", []):
            for b in t.get("bindings", []):
                if b.get("source") != "portal_api":
                    continue
                rel = catalog.relations.get(b["relation"])
                out.append({"file": name, "object_type": t["id"], "binding": b["id"],
                            "relation": b["relation"],
                            "served": bool(rel is not None and rel.served),
                            "upstream_ask": t.get("availability", {}).get("upstream_ask")})
    return sorted(out, key=lambda e: (e["file"], e["binding"]))


def drift_report(compiled: CompiledSchema, catalog: Catalog) -> dict[str, Any]:
    """Compare a compiled schema with a catalog (drift guard)."""
    bound = compiled.portal_bindings()
    served = catalog.served()
    unbound = sorted(set(served) - set(bound) - set(compiled.excluded_relations))
    missing_relations, degraded, now_served = [], [], []
    read_columns: dict[str, set[str]] = {}
    for rel_name, owners in sorted(bound.items()):
        rel = catalog.relations.get(rel_name)
        if rel is None or not rel.served:
            missing_relations.append({"relation": rel_name,
                                      "reason": "absent" if rel is None else
                                      (rel.unavailable or "no key"),
                                      "object_types": sorted({o for o, _ in owners})})
            continue
        cols = read_columns.setdefault(rel_name, set())
        for _, b in owners:
            cols.update(_column(p) for p in b["key"])
            cols.update(_column(w["path"]) for w in b.get("where", []))
    for tid, t in sorted(compiled.object_types.items()):
        own = {b["id"]: b for b in t["bindings"] if b["source"] == "portal_api"}
        for p in t["properties"]:
            for s in p["sources"]:
                b = own.get(s["binding"])
                if b is None:
                    continue
                rel = catalog.relations.get(b["relation"])
                if rel is None or not rel.served:
                    continue
                col = _column(s["path"])
                state = p.get("availability", "served")
                if state == "served":
                    if col in rel.columns:
                        read_columns[b["relation"]].add(col)
                    else:
                        degraded.append({"object_type": tid, "property": p["name"],
                                         "binding": b["id"], "column": col,
                                         "availability": "degraded",
                                         "reason": f"column {col} not in the catalog of "
                                                   f"{b['relation']}"})
                elif state == "not_served" and col in rel.columns:
                    now_served.append({"object_type": tid, "property": p["name"],
                                       "binding": b["id"], "column": col})
    for link in compiled.link_types.values():
        for lb in link["bindings"]:
            for end in ("from", "to"):
                owner = compiled.bindings.get(lb.get(f"{end}_binding", ""))
                if owner and owner[1]["relation"] in read_columns:
                    read_columns[owner[1]["relation"]].update(
                        _column(pair[end]) for pair in lb.get("pairs", []))
    unbound_columns = {rel: sorted(set(served[rel].columns) - cols)
                       for rel, cols in sorted(read_columns.items())
                       if set(served[rel].columns) - cols}
    return {
        "format": "plx-ontology-drift/v1",
        "schema_digest": compiled.digest,
        "catalog_digest": "sha256:" + catalog.digest,
        "pinned_catalog_digest": compiled.pinned_catalog_digest,
        "catalog_changed": (compiled.pinned_catalog_digest is not None and
                            compiled.pinned_catalog_digest != "sha256:" + catalog.digest),
        "catalog_evidence_mode": catalog.evidence_mode,
        "served_relations": len(served),
        "bound_relations": len(set(served) & set(bound)),
        "excluded_relations": {k: compiled.excluded_relations[k]
                               for k in sorted(compiled.excluded_relations)},
        "unbound_relations": unbound,
        "missing_relations": missing_relations,
        "degraded": degraded,
        "unbound_columns": unbound_columns,
        "not_served_now_served": now_served,
        "ok": not unbound and not missing_relations and not degraded,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Validate the ontology schema and report "
                                                 "drift against a catalog (no network).")
    parser.add_argument("--root", default=str(SCHEMA_DIR))
    parser.add_argument("--catalog", default=str(CATALOG_SNAPSHOT_PATH),
                        help="catalog JSON (/v1/relations shape or mirror fixture shape)")
    parser.add_argument("--drift", action="store_true", help="print the drift report")
    args = parser.parse_args(argv)
    catalog = load_catalog(args.catalog)
    try:
        compiled = load_schema(args.root, catalog=catalog, check_format=True)
    except SchemaLoadError as exc:
        print(json.dumps({"ok": False, "errors": exc.errors}, indent=1))
        return 1
    out: dict[str, Any] = {"ok": True, "digest": compiled.digest,
                           "object_types": len(compiled.object_types),
                           "link_types": len(compiled.link_types),
                           "shadowed": len(compiled.shadowed),
                           "company_scoped_link_bindings": sum(
                               len(v) for v in compiled.company_scope.values()),
                           "warnings": compiled.warnings}
    if args.drift:
        out["drift"] = drift_report(compiled, catalog)
        out["drift"]["pending"] = pending_report(catalog, args.root)
        out["ok"] = out["drift"]["ok"]
    print(json.dumps(out, indent=1))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
