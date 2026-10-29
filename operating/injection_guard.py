"""Injection defence: source text is data, action parameters trace to what was read.

Mail bodies, attachments and
document text are the main inputs of the PO, freight and AO cases, and any of them
may carry text written to steer the agent ("update the PO to ...", "payment is
approved", "ignore previous instructions"). Three deterministic rules hold that
line; no model is involved in any of them.

1. **Argument provenance** (:func:`trace_parameters`, :func:`check_registration`;
   Progent-style). Every parameter value a proposed action would send must equal a
   *typed* value the proposer cited (a cell of a cited structured row, by citation
   handle and field path, read for the proposer and digest-unchanged) or a literal
   of the user's objective (the case question recorded at ``CaseStore.open``) when a
   human wrote it. A question an agent supplied (MCP ``plx.case_open``) or one with
   no recorded author is agent text and authorizes nothing
   (``injection_value_only_in_agent_authored_objective``); a subject identifier in
   the question (the target key, a case root) is never a value. A value found
   anywhere else (a mail body, a document page, a value the proposer typed) refuses
   with ``injection_untraced_parameter``. The per-parameter trace (source kind and
   citation/field, never the value) is stored on the intent and summarised in the
   Mission Control approval reason. The parameters of a case
   proposal are what ``goal2_exec_owner.dispatch_payload`` sends: the expected
   effect's ``relation`` and ``key`` (the cited target row), each of its ``fields``
   (a column the cited target row has) and each field's own value (``values``; the
   trace is named ``equals`` for a one-field effect and ``values.<field>`` per field
   otherwise): every written value is traced, with its **role** checked: a cell of
   another role than the written field's (``cited_role``: an amount for a received
   quantity, an ordered quantity for a received one, a customer PO for a Petra PO)
   never supplies it (``injection_parameter_role_mismatch``). The
   proposal ``payload`` is the approver's description and is never dispatched.
   ``operating.action_path.ActionPath.register`` runs :func:`check_registration`
   on every proposal before an intent exists.

2. **Source-text firewall** (:class:`UntrustedText`, :func:`firewall_content`,
   :func:`firewall_tool_output`: every string of a tool result but a typed field,
   ``TYPED_FIELDS``, is wrapped). Text read from a source reaches a model only as
   an ``untrusted_text`` envelope (``operating/ontology_tool_contracts.json``
   ``$defs/untrusted_text``: ``{"untrusted_text": ..., "origin": {...}}``; text cut at
   ``ENVELOPE_CHARS`` says so with ``"truncated": true``). An owning system's row
   (``api://``, ``fixture://``, a ``bc://`` record's fields) leaves as one row envelope
   ``{"origin": {"citation": ...}, "cells": {...}}`` whose free-text cells are
   ``{"untrusted_text": ...}`` markers under the row's origin; only schema-typed cells
   are bare (``row_envelope``). An
   envelope is never a typed value: rule 1 skips it, so text inside it can never
   become an action parameter, and the claim gate compares cells by value, never by
   reading their text as an instruction. A citation of a *document* (mail message
   or attachment, SharePoint file, attached original; :func:`is_source_text_citation`)
   is source text as a whole: none of its fields is a typed value, because every
   one of them (subject, body, file name, page text) was written by its author.
   Typed values come out of such text only through deterministic parsers
   (``ontology.mentions.extract``), and those are leads, never parameters. The one
   exception (decision chain-document-evidence, option 2): the ``documentFields`` row
   ``ontology.document_fields.pinned_document_row`` re-extracts from a byte-pinned
   SharePoint file or mail attachment for the reader now (:func:`document_fields_row`;
   ``ActionPath.register`` reads it, never the case's stored view) supplies the
   ``fields.<name>.value`` cells a schema document binding types with a role, each of
   that role only, and only a field the profile's ``attests`` rule proposes through the
   proposal's type (``ActionPath.register`` keeps no other); the per-call human approval
   still decides. An
   identifier parsed out of such text is served bare only for a kind whose declared
   value pattern is kind-specific (``kind_specific``);
   the boundary re-checks every normalized identifier and typed value per kind and
   value type (``_identifier_record_typed``, ``_value_record_typed``).

3. **External assertions** (:func:`is_external_assertion`, :func:`may_confirm`).
   Text an internal sender did not write (an external or unknown sender, or quoted
   reply/forward history in anyone's message) is an external assertion: by itself it
   never confirms a payment, receipt or quantity fact. The fact classes and the
   confirming tiers are ``ontology.mentions``'s, so the mention projection
   (``mentions.confirmation_support``) and this predicate cannot drift apart.

Boundary: the MCP host passes every tool result through :func:`firewall_tool_output`
once at its output boundary (``operating.mcp_server.MCPServer.handle``), so original page
text, case-mail bodies and subjects and Portal/BC cells reach a model only as envelopes
(decided (a)). The ontology tools
(``operating/ontology_tools*.py``) and the mail capture views
(``integrations/capture_mail.py``) emit envelopes themselves (their outputs are
contract-validated: ``mcp_server.SELF_ENVELOPING_TOOLS``); their results pass the same
boundary with the strings their contract types (``contract_typing``) left bare, so any
other bare string would still be wrapped. In-process host code (``mcp_server.local_client``,
the playbook and eval-case readers) reads results as the tools built them
(``MCPServer.handle(host_code=True)``): it is not a model.

Stdlib only. Evidence label of everything here: ``isolated-fixture`` (it reads no
source; it judges what the case store resolved).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ontology import identifiers
from ontology.mentions import CONFIRMING_TIERS, EXTERNAL_UNCONFIRMABLE, SENDER_CLASSES
from ontology.schema import load_schema

from .citation_schemes import FIXTURE_SCHEME, handle_prefix

REFUSAL = "injection_untraced_parameter"
# The value appears only in a case question an agent wrote (or nobody attributed).
REFUSAL_AGENT_OBJECTIVE = "injection_value_only_in_agent_authored_objective"
# A text value with surrounding whitespace (matching trims; dispatch would not).
REFUSAL_UNNORMALIZED = "injection_parameter_not_normalized"
# A value found only in a cited cell of another role (a quantity cited as an amount, an
# ordered quantity as a received one, a customer PO as a Petra PO).
REFUSAL_ROLE = "injection_parameter_role_mismatch"
# A written field whose role neither the action type nor the owner's schema declares.
REFUSAL_UNTYPED_FIELD = "injection_target_field_untyped"
CASE_AUTHOR_KINDS = ("human", "agent", "unknown")
ENVELOPE_CHARS = 20_000  # operating/ontology_tool_contracts.json limits.max_untrusted_text_chars
# Citations whose every field is author-written text (rule 2). Scheme of the L1
# handle, or the relation the resolver reports for it.
SOURCE_TEXT_SCHEMES = ("graph-mail://", "sharepoint://", "original://")
SOURCE_TEXT_RELATION_PREFIXES = ("graph-mail.", "sharepoint.", "original.")
_JOINERS = "_-/#.,"
_QUOTES = (('"', '"'), ("'", "'"), ("`", "`"), ("\u201c", "\u201d"), ("\u2018", "\u2019"))
_NUMBER_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


# --------------------------------------------------------------------------- envelopes

@dataclass(frozen=True)
class UntrustedText:
    """Source text as data. Its only rendering is the contract envelope."""

    text: str
    citation: str | None = None
    field_path: str | None = None
    sender_class: str | None = None
    # Who wrote case text (a ``case://`` origin): human, agent or unknown.
    author_kind: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("untrusted_text_must_be_text")
        if self.sender_class is not None and self.sender_class not in SENDER_CLASSES:
            raise ValueError(f"sender_class_invalid:{self.sender_class!r}")
        if self.author_kind is not None and self.author_kind not in CASE_AUTHOR_KINDS:
            raise ValueError(f"author_kind_invalid:{self.author_kind!r}")

    def envelope(self) -> dict[str, Any]:
        origin = {key: value for key, value in (("citation", self.citation),
                                                ("field_path", self.field_path),
                                                ("sender_class", self.sender_class),
                                                ("author_kind", self.author_kind))
                  if value is not None}
        out = {"untrusted_text": self.text[:ENVELOPE_CHARS], "origin": origin}
        if len(self.text) > ENVELOPE_CHARS:
            out["truncated"] = True  # never cut silently
        return out


# Why a tool withheld an envelope's source text ($defs/untrusted_text ``withheld``): the
# text is then empty, so the envelope carries nothing to wrap.
WITHHELD_REASONS = frozenset({"not_po_like"})


def is_envelope(value: Any) -> bool:
    """Whether ``value`` is an ``untrusted_text`` envelope (contract shape). A withheld
    envelope (``withheld`` a known reason) is one only when its text is empty."""
    if not (isinstance(value, Mapping) and isinstance(value.get("untrusted_text"), str)):
        return False
    if "withheld" in value:
        return (value["withheld"] in WITHHELD_REASONS and value["untrusted_text"] == ""
                and set(value) <= {"untrusted_text", "origin", "withheld"})
    return set(value) <= {"untrusted_text", "origin", "truncated"}


def is_source_text_citation(handle: Any, content: Any = None) -> bool:
    """Whether a citation names a document (mail, attachment, file, original), whose
    fields are all author-written text, by handle scheme, by the relation an api://
    handle names (a mirrored document relation such as ``graph-mail.message``) or by
    the resolved relation."""
    if isinstance(handle, str) and (handle.startswith(SOURCE_TEXT_SCHEMES) or (
            handle.startswith("api://")
            and _relation_of(handle).startswith(SOURCE_TEXT_RELATION_PREFIXES))):
        return True
    relation = content.get("relation") if isinstance(content, Mapping) else None
    return isinstance(relation, str) and relation.startswith(SOURCE_TEXT_RELATION_PREFIXES)


def _wrap_strings(value: Any, citation: str | None, path: str,
                  sender: str | None) -> Any:
    if is_envelope(value):
        return dict(value)
    if isinstance(value, str):
        return UntrustedText(value, citation, path, sender).envelope()
    if isinstance(value, Mapping):
        return {key: _wrap_strings(item, citation, f"{path}.{key}", sender)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wrap_strings(item, citation, f"{path}.{index}", sender)
                for index, item in enumerate(value)]
    return value


def firewall_content(handle: str, content: Mapping[str, Any],
                     sender_class: str | None = None) -> dict[str, Any]:
    """A resolved citation with its source text marked as data.

    For a document citation every string under ``row`` becomes an envelope (field
    path ``row.<name>``); a structured row is returned unchanged (its cells are the
    owning system's typed values). The input is not modified."""
    out = dict(content)
    if is_source_text_citation(handle, content) and isinstance(content.get("row"), Mapping):
        out["row"] = _wrap_strings(content["row"], handle, "row", sender_class)
    return out


# Typed fields a tool result may carry bare (rule 2 helper): an explicit allowlist of
# keys whose values the Workbench itself generates or validates (its ids, handles,
# digests, codes, timestamps, numbers), each with a grammar. A string under one of these
# keys stays bare only when it also matches its grammar (no whitespace, bounded), so
# prose can never ride an allowlisted key. Nothing is inferred from a key's shape: a
# field the source or a sender controls (internet_message_id, mime/content types, file
# names, subjects, addresses, display names) is never listed and is always wrapped.
# Mirrors integrations/capture_mail.py's message view, which envelopes
# from/to/cc/internet_message_id/original_url/attachment names.
# Characters no typed field holds: C0/C1 controls, soft hyphen, zero-width and joiner
# characters, bidi marks, embeddings, overrides and isolates, line/paragraph separators,
# the BOM, interlinear annotation and tag characters. A string carrying one is never
# typed (``_typed``, ``ContractTyping.typed``); a projected native key percent-encodes
# them (``ontology.project.object_id_of``). The patterns of $defs/handle, object_id and
# object_key_value exclude them too (asserted equal in tests).
INVISIBLE_CHARS = (r"\x00-\x1f\x7f-\x9f\xad\u061c\u180e\u200b-\u200f\u2028-\u202e"
                   r"\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U000e0001"
                   r"\U000e0020-\U000e007f")
_INVISIBLE = re.compile(f"[{INVISIBLE_CHARS}]")
VISIBLE_CHAR = rf"[^\s{INVISIBLE_CHARS}]"  # one character of a typed handle, id or key
# A normalized identifier ($defs/identifier_value): one code token, or code groups
# separated by single spaces where every group carries a digit, after at most three
# PO-style marker words (the markers of operating/customer_po_coverage.py: 'PO# 4500 12',
# 'P.O. NO. 4500', 'CUST PO 4500'), or two or three groups of which one carries a digit
# (an item code spelled as a short description: 'BLUE CAP 28'). Prose ('JOHN SMITH',
# 'IGNORE PREVIOUS ...', 'ORDER PER EMAIL 45001') never matches; it leaves as source text
# with an identifier_invalid gap.
_ID_CHAR = r"[A-Za-z0-9_./#@:+&()-]"
_ID_CODED = rf"{_ID_CHAR}*[0-9]{_ID_CHAR}*"


def _ascii_ci(word: str) -> str:
    """``word`` matched ASCII-case-insensitively with explicit classes: Python's ``(?i)``
    folds Unicode ('CU\u017fT' matches 'CUST') and JSON-schema validators disagree on it."""
    return "".join(f"[{c.upper()}{c.lower()}]" if c.isascii() and c.isalpha() else c
                   for c in word)


_ID_MARKER = ("(?:(?:" + "|".join((
    _ascii_ci("CUST") + "(?:" + _ascii_ci("OMER") + r")?\.?", _ascii_ci("CLIENT"),
    _ascii_ci("PURCHASE"), _ascii_ci("ORDER"), _ascii_ci("P") + r"\.?" + _ascii_ci("O") + r"\.?",
    _ascii_ci("P/O"), _ascii_ci("NO") + r"\.?", _ascii_ci("NUMBER"), "#")) + r")[#:.]?)")
# The identifier kind of a record matched by its own key (plx.find): its normalized
# value is the object id's native part.
OBJECT_KEY_KIND = "object_key"
_GRAMMARS = {
    "handle": re.compile(handle_prefix(FIXTURE_SCHEME)
                         + VISIBLE_CHAR + "{1,4090}"),
    "digest": re.compile(r"[0-9a-fA-F]{64}|wb-[0-9a-f]{40}"),
    # ASCII digits only: ``\d`` would admit fullwidth and other Unicode digits
    "time": re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}"
                       r"(?::[0-9]{2}(?:\.[0-9]{1,9})?)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?"
                       r"|[0-9]{8}"),
    "number": re.compile(r"-?[0-9]{1,60}(?:\.[0-9]{1,60})?"),
    "date": re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}"),
    # ids, codes, enums, relation/field/column names, principals, source-system ids:
    # one whitespace-free token of at most 128 characters
    "token": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+=!,-]{0,127}"),
    "boolean": re.compile(r"(?i:true|false|t|f)|[01]"),
    # opaque version tags (eTag, cTag, change key): printable, no whitespace
    "version": re.compile(r"[!-~]{1,200}"),
    # a projected object id, <Type>:<binding id>:<canonical key> (no whitespace or
    # invisible character), a unit of measure (one token, or a listed multi-word unit:
    # 'fl oz', the only one Portal spells, plx-customer-portal@6e76a5be4
    # portal/src/lib/costing/fill-weight-from-size.ts:62 and
    # portal/src/app/operations/product-packaging/page.tsx:688; PRODUCT_UOM_OPTIONS in
    # portal/src/lib/mrp/product-uom.ts:30-49 are all one token), a normalized identifier
    # and an object key: the patterns of operating/ontology_tool_contracts.json
    # $defs/object_id, unit, identifier_value and object_key_value (asserted equal in
    # tests)
    "object_id": re.compile(r"[A-Z][A-Za-z0-9]{1,63}:[a-z][a-z0-9_.-]{1,95}:"
                            + VISIBLE_CHAR + "{1,512}"),
    "unit": re.compile("(?:[A-Za-z0-9%$#\u00b0\u00b5][A-Za-z0-9%$#\u00b0\u00b5._/*^()+-]{0,19}"
                       "|" + _ascii_ci("fl oz") + ")"),
    "identifier": re.compile(rf"(?=.{{1,128}}$)(?:{_ID_CHAR}+|(?:{_ID_MARKER} ){{0,3}}"
                             rf"{_ID_CODED}(?: {_ID_CODED})*"
                             rf"|(?=.*[0-9]){_ID_CHAR}+(?: {_ID_CHAR}+){{1,2}})"),
    "object_key": re.compile(VISIBLE_CHAR + "{1,200}"),
}
TYPED_FIELDS: Mapping[str, str] = {
    **{key: "handle" for key in (
        "citation", "handle", "current_citation", "metadata_citation", "source_citation",
        "source_metadata_citation", "document_id", "original_locator", "page_citations",
        "page_citation_pattern", "target_citation", "from_citation", "to_citation",
        "evidence_citation", "message_citation", "po_citation", "provenance",
        "page_citation", "l1_handle")},
    **{key: "digest" for key in (
        "digest", "current_digest", "recorded_digest", "source_fingerprint",
        "source_revision", "idempotency_key", "fingerprint", "sha256Hash", "sha256",
        "text_sha256", "page_text_sha256", "content_sha256", "body_sha256", "shown_sha256",
        "original_sha256", "extraction_text_sha256", "schema_digest", "proposal_digest",
        "cell_text_sha256")},
    **{key: "time" for key in (
        "at", "last_modified", "since", "until", "time", "start", "end",
        "relation_last_refresh_at", "read_at", "updated_at", "created_at", "opened_at",
        "closed_at", "extracted_at", "received_at", "sent_at", "captured_at", "as_known_at",
        "generated_at", "started_at", "finished_at", "content_last_seen_at", "expires_at",
        "confirmed_at", "decided_at", "recorded_at", "attached_at")},
    **{key: "number" for key in ("result",)},
    "object_id": "object_id",
    "unit": "unit",
    **{key: "version" for key in ("etag", "ctag", "change_key", "cited_version",
                                  "current_version")},
    **{key: "token" for key in (
        "case_id", "intent_id", "proposal_id", "finding_id", "gap_id", "calc_id",
        "verification_id", "original_id", "link_id", "gate_id", "task_id", "attempt_id",
        "evidence_finding_id", "event_id", "confirmation_id", "company_id",
        "id", "ref", "status", "state", "kind", "reason", "reason_code", "outcome", "view",
        "source", "label", "by", "step", "check", "evidence_label", "objective_author_kind",
        "author_kind",
        "citations_status", "inputs_status", "case_status", "text_source", "reading",
        "unit_basis", "attribution", "authority", "statement_by", "record_status",
        "comparison", "id_type", "source_acl_status", "extraction_method", "method",
        "capability", "owner_system", "schema", "view_schema",
        "classification", "tenant", "relation", "field", "fields", "column",
        "link", "repo", "commit", "basis", "actor", "owner", "attached_by", "recorded_by",
        "extracted_by", "closed_by", "sub", "audience", "drive_id", "item_id", "mailbox",
        "mailbox_upn", "parent_folder_id", "identity", "conversation_id", "decider_role",
        "trigger", "code", "error", "tool", "mode", "section", "bucket", "blocked_by",
        "agent_tool", "role", "operation", "concludes", "reconciled_by", "dispatch_outcome",
        "system", "receipt", "sender_class", "bound", "contract_id", "direction",
        # the ontology vocabulary (schema names, tiers, lineages, kinds)
        "binding", "identifier_kind", "lineage", "object_type", "property", "tier",
        "link_type", "feature", "field_path", "completeness", "value_type", "currency",
        "quantity_role", "markings", "availability", "cardinality", "tier_ceiling",
        "time_basis", "event_type", "qualifier", "set", "events", "sources",
        "truncated_cells")},
}
# Workbench-authored prose under a cited record (the tool's own words about it, never
# the record's text): wrapped like everything else, with the Workbench as origin.
WORKBENCH_TEXT_KEYS = frozenset({"statement", "next_tools", "next_step", "limitations"})
WORKBENCH_ORIGIN = "workbench://"  # + the tool name: text the Workbench wrote
# A Workbench statement about a cited document (``statement_by: workbench`` with a mail,
# SharePoint or original citation) quotes that document's own fields (sender, recipients,
# subject, file name, path): its envelope names the document as origin, never the
# Workbench (``_quoted_origin``).
# An owning system's record (a structured citation) leaves as ONE row envelope per row,
# ``{"origin": {"citation": <row citation>}, "cells": {column: value}}``: the origin is
# stated once. A cell stays bare only when the compiled ontology schema types its column
# (a binding key, or properties that all agree on one value type in ``COLUMN_GRAMMARS``)
# and the value matches the type's grammar; numbers, booleans and nulls are bare; every
# other cell (free text, names, notes, untyped or unknown columns) is an
# ``{"untrusted_text": ...}`` marker whose origin is the row's. A cell holding a JSON
# object or array is serialized and marked whole: source JSON never re-enters a result
# as structure (a cell shaped like an envelope or a cited row is text). Columns are
# typed by the binding schema of the citation's source only, never by their names.
# Scheme -> binding source; the row is ``row``/``rows`` of an api:// or fixture://
# citation (beside its ``key``) and ``fields`` (under ``record``) of a bc:// read.
STRUCTURED_SOURCES: Mapping[str, str] = {"api://": "portal_api", "fixture://": "portal_api",
                                         "bc://": "bc"}
STRUCTURED_SCHEMES = tuple(STRUCTURED_SOURCES)
ROW_ENVELOPE_KEYS = frozenset({"origin", "cells"})
COLUMN_GRAMMARS: Mapping[str, str] = {
    "identifier": "token", "enum": "token", "boolean": "boolean", "integer": "number",
    "decimal": "number", "quantity": "number", "money": "number", "date": "time",
    "timestamp": "time"}
_KEY_COLUMN = "identifier"
_COLUMN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# binding source -> relation -> {column: value type}.
ColumnTypes = Mapping[str, Mapping[str, Mapping[str, str]]]
_COLUMN_TYPES: list[ColumnTypes] = []


def schema_column_types(schema: Any) -> dict[str, dict[str, dict[str, str]]]:
    """``ColumnTypes`` of a compiled ontology schema's ``portal_api`` and ``bc`` bindings
    (the relations ``api://`` and ``bc://`` citations name): binding key columns are
    identifiers; a column is typed by its properties' value types when they all agree
    on one in ``COLUMN_GRAMMARS`` (a conflict or a text binding leaves it untyped)."""
    sources = frozenset(STRUCTURED_SOURCES.values())
    seen: dict[tuple[str, str], dict[str, set[str]]] = {}
    for _type_id, binding in schema.bindings.values():
        if binding.get("source") in sources:
            columns = seen.setdefault((binding["source"], binding["relation"]), {})
            for column in binding.get("key") or ():
                columns.setdefault(column, set()).add(_KEY_COLUMN)
    for object_type in schema.object_types.values():
        for prop in object_type.get("properties") or ():
            for source in prop.get("sources") or ():
                found = schema.bindings.get(source.get("binding"))
                if found is None or found[1].get("source") not in sources:
                    continue
                path = str(source.get("path") or "")
                # A nested path reads inside a json cell: the cell itself is not typed.
                column = path.split(".", 1)[0]
                kind = prop.get("value_type") if "." not in path else "json"
                seen.setdefault((found[1]["source"], found[1]["relation"]), {}).setdefault(
                    column, set()).add(kind)
    out: dict[str, dict[str, dict[str, str]]] = {}
    for (source, relation), columns in seen.items():
        out.setdefault(source, {})[relation] = {
            column: next(iter(kinds)) for column, kinds in columns.items()
            if len(kinds) == 1 and next(iter(kinds)) in COLUMN_GRAMMARS}
    return out


def builtin_column_types() -> ColumnTypes:
    """``schema_column_types`` of the committed ontology schema (loaded once)."""
    if not _COLUMN_TYPES:
        _COLUMN_TYPES.append(schema_column_types(load_schema()))
    return _COLUMN_TYPES[0]


@dataclass(frozen=True)
class ContractTyping:
    """How a self-enveloping tool's contract types its strings: ``grammars`` (key ->
    the patterns and enums the contract gives a string under that key), ``bare_keys``
    (keys whose plain strings the contract allows: Workbench prose and the agent's own
    query text) and ``prose`` (contract path -> a pattern that accepts words and
    spaces, such as a use case's catalog title: ``prose_pattern``). A prose pattern
    types only the property it is declared on, never every string under that key
    name: a source document's ``title`` elsewhere in a result matches 'PAY 5000 NOW' as
    well as a catalog title does, so typed by its key alone it would leave bare."""

    grammars: Mapping[str, tuple[Any, ...]]
    bare_keys: frozenset[str] = frozenset()
    prose: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)

    def typed(self, key: Any, value: str, path: str | None = None) -> bool:
        """Whether ``value`` under ``key`` stays bare; ``path``: its dotted path in the
        result (list indexes included). Without one no prose pattern applies."""
        if _INVISIBLE.search(value):
            return False
        if key in self.bare_keys:
            return True
        grammars = tuple(self.grammars.get(key, ())) if isinstance(key, str) else ()
        if isinstance(path, str):
            grammars += tuple(self.prose.get(contract_path(path), ()))
        for grammar in grammars:
            if (value in grammar if isinstance(grammar, frozenset)
                    else grammar.fullmatch(value) is not None):
                return True
        return False


def contract_path(path: str) -> str:
    """A result's dotted path as a contract path: list indexes left out
    (``use_case.also.0.title`` -> ``use_case.also.title``)."""
    return ".".join(part for part in path.split(".") if part and not part.isdigit())


def prose_pattern(pattern: re.Pattern[str]) -> bool:
    """Whether a contract pattern accepts words separated by spaces (an instruction
    such as 'PAY 5000 NOW' of ``GENERIC_IDENTIFIER_SHAPES``): it cannot tell its own
    field's text from a source's, so it types only where it is declared."""
    return any(" " in shape and pattern.fullmatch(shape) is not None
               for shape in GENERIC_IDENTIFIER_SHAPES)


def contract_typing(contracts: Mapping[str, Any]) -> ContractTyping:
    """``ContractTyping`` of an ontology tool contract document
    (``operating/ontology_tool_contracts.json``): every string property of every tool
    output that has a ``pattern`` or an ``enum`` (``$ref`` resolved) types the strings
    under its key; a property the contract types as a plain string (Workbench prose such
    as detail and hint, the agent's own query text) is a bare key. The envelope's own
    ``untrusted_text`` never is: source text is an envelope by the contract's rule. A
    pattern that accepts prose (``prose_pattern``) types its own contract path alone."""
    defs = contracts.get("$defs") or {}
    found: dict[str, list[Any]] = {}
    scoped: dict[str, list[Any]] = {}
    plain: set[str] = set()

    def resolve(schema: Any) -> Any:
        seen = 0
        while isinstance(schema, Mapping) and "$ref" in schema and seen < 32:
            schema, seen = defs.get(str(schema["$ref"]).rsplit("/", 1)[-1]), seen + 1
        return schema

    def add(table: dict[str, list[Any]], key: str, grammar: Any) -> None:
        if grammar not in table.setdefault(key, []):
            table[key].append(grammar)

    def walk(schema: Any, key: str | None, path: str, depth: int = 0) -> None:
        schema = resolve(schema)
        if not isinstance(schema, Mapping) or depth > 40:
            return
        kinds = schema.get("type")
        if key is not None and (kinds == "string" or (isinstance(kinds, list)
                                                      and "string" in kinds)):
            if isinstance(schema.get("pattern"), str):
                grammar = re.compile(schema["pattern"].removeprefix("^").removesuffix("$"))
                add(scoped if prose_pattern(grammar) else found,
                    path if prose_pattern(grammar) else key, grammar)
            if isinstance(schema.get("enum"), list):
                add(found, key, frozenset(v for v in schema["enum"] if isinstance(v, str)))
            if "pattern" not in schema and "enum" not in schema and key != "untrusted_text":
                plain.add(key)
        for name, sub in (schema.get("properties") or {}).items():
            walk(sub, name, f"{path}.{name}" if path else name, depth + 1)
        if "items" in schema:
            walk(schema["items"], key, path, depth + 1)
        for combined in ("anyOf", "oneOf", "allOf"):
            for sub in schema.get(combined) or ():
                walk(sub, key, path, depth + 1)

    for tool in (contracts.get("tools") or {}).values():
        walk(tool.get("output"), None, "")
    return ContractTyping({key: tuple(items) for key, items in found.items()},
                          frozenset(plain),
                          {path: tuple(items) for path, items in scoped.items()})


def _typed(key: Any, value: str) -> bool:
    """Whether ``value`` under ``key`` is a typed field: ``key`` is in ``TYPED_FIELDS``
    and ``value`` matches that field's grammar."""
    kind = TYPED_FIELDS.get(key) if isinstance(key, str) else None
    return (kind is not None and _GRAMMARS[kind].fullmatch(value) is not None
            and not _INVISIBLE.search(value))


def is_unit(text: Any) -> bool:
    """Whether ``text`` is a unit of measure as $defs/unit types it (a projected unit
    outside it is source text: the generic tools gap it as ``unit_invalid``)."""
    return isinstance(text, str) and _GRAMMARS["unit"].fullmatch(text) is not None


# Generic identifier shapes: a code word with a digit, a
# hyphenated word chain, two or three groups of which one carries a digit, a word chain
# before an address (a sender-chosen Message-ID). A declared value pattern that accepts
# any of them ('[^\x00-\x1f]{1,80}' of customer_po) is not kind-specific: it cannot tell
# an identifier from an instruction such as 'PAY 5000 NOW'.
GENERIC_IDENTIFIER_SHAPES = ("PAY5000", "pay5000", "PAY-5000-NOW", "PAY 5000", "PAY 5000 NOW",
                             "5000 PAY NOW", "PAY-5000-NOW@example.com")
_TEXT_KINDS: dict[str, bool] = {}


def kind_specific(kind: Any) -> bool:
    """Whether ``ontology/identifiers.json`` declares ``kind`` with a kind-specific value
    pattern: one that rejects every ``GENERIC_IDENTIFIER_SHAPES`` (``AO-\\d{3,5}[A-Z]{1,3}``
    does, customer_po's '[^\\x00-\\x1f]{1,80}' does not). Measured on the declared
    pattern itself, so tightening it in identifiers.json is all a kind needs."""
    if not isinstance(kind, str) or kind not in identifiers.kinds():
        return False
    if kind not in _TEXT_KINDS:
        _TEXT_KINDS[kind] = not any(identifiers.valid(kind, shape)
                                    for shape in GENERIC_IDENTIFIER_SHAPES)
    return _TEXT_KINDS[kind]


def is_identifier(value: Any, kind: str | None = None, citation: Any = None) -> bool:
    """Whether ``value`` is a normalized identifier a tool emits bare: it matches
    $defs/identifier_value and, for a kind ``ontology/identifiers.json`` declares, that
    kind's value pattern (``identifiers.valid``). Of ``OBJECT_KEY_KIND`` it is an object
    id's native part ($defs/object_key_value). Anything else (a customer PO field holding
    a name or a sentence) is source text: the generic tools gap it as
    ``identifier_invalid`` and quote it in an envelope.

    ``citation``: where the value was read. A value extracted from source text (a mail,
    an OCR'd original, a file: ``is_source_text_citation``) is bare only for a declared
    kind-specific kind (``kind_specific``): the author of the
    text chose it, and a generic shape ('PAY 5000 NOW', 'IGNORE-ALL-PREVIOUS-1') stays
    source text."""
    if not isinstance(value, str):
        return False
    if kind == OBJECT_KEY_KIND:
        return _GRAMMARS["object_key"].fullmatch(value) is not None
    if _GRAMMARS["identifier"].fullmatch(value) is None:
        return False
    if is_source_text_citation(citation) and not kind_specific(kind):
        return False
    return kind not in identifiers.kinds() or identifiers.valid(kind, value)


_IDENTIFIER_KINDS: list[Mapping[tuple[str, str], str]] = []


def _identifier_kinds() -> Mapping[tuple[str, str], str]:
    """``(object type, property) -> identifier kind`` of the committed ontology schema
    (loaded once)."""
    if not _IDENTIFIER_KINDS:
        _IDENTIFIER_KINDS.append({
            (type_id, item["name"]): item["identifier_kind"]
            for type_id, spec in load_schema().object_types.items()
            for item in spec.get("properties") or ()
            if isinstance(item.get("identifier_kind"), str) and "name" in item})
    return _IDENTIFIER_KINDS[0]


def identifier_kind_of(object_type: str, prop: str) -> str | None:
    """The identifier kind the committed ontology schema gives ``object_type.prop``;
    None when it declares none."""
    return _identifier_kinds().get((object_type, prop))


def _property_kinds(object_type: Any, prop: Any) -> frozenset[str]:
    """The identifier kinds of ``object_type.prop``; of ``prop`` on any object type when
    the type is not known (an aggregate or compare value names only its property)."""
    kind = identifier_kind_of(object_type, prop) if isinstance(object_type, str) else None
    if kind is not None:
        return frozenset({kind})
    return frozenset(found for (_type, name), found in _identifier_kinds().items()
                     if name == prop)


# The grammar of a value record's string ``value`` per its ``value_type`` ($defs/value):
# text and json values are envelopes, integers and booleans are never strings, and an
# identifier is checked against its kind (``_value_record_typed``).
VALUE_TYPE_GRAMMARS: Mapping[str, str] = {
    "decimal": "number", "money": "number", "quantity": "number", "date": "date",
    "timestamp": "time", "enum": "token"}


def _value_record_typed(item: Mapping[str, Any]) -> bool | None:
    """Whether a value record's string ``value`` (a mapping with a ``value_type``) is typed
    by that value type, the boundary's own check (review ib-firewall-3 m3/m4: 'value' is a
    plain contract string, so its contract typing alone would let any text through);
    None for any other mapping. An identifier must be one of its property's kinds (of
    its object type when the record names it), read where its citation says."""
    value_type, value = item.get("value_type"), item.get("value")
    if not isinstance(value_type, str) or not isinstance(value, str):
        return None
    if value_type == "identifier":
        cited = item.get("citation")
        handle = cited.get("l1_handle") if isinstance(cited, Mapping) else cited
        kinds = _property_kinds(item.get("object_type"), item.get("property")) or {None}
        return any(is_identifier(value, kind, handle) for kind in kinds)
    grammar = VALUE_TYPE_GRAMMARS.get(value_type)
    return (grammar is not None and _GRAMMARS[grammar].fullmatch(value) is not None
            and not _INVISIBLE.search(value))


def _untyped_gap(item: Mapping[str, Any], key: str, quoted: Mapping[str, Any]
                 ) -> dict[str, Any]:
    """The contract's gap for a ``normalized`` or ``value`` the boundary does not serve as
    typed (the shape ``ontology_tools.identifier_out`` gives one the tool itself refuses)."""
    identifier = key == "normalized" or item.get("value_type") == "identifier"
    subject = item.get("identifier_kind") if key == "normalized" else item.get("property")
    return {"state": "unknown", "reason": "identifier_invalid" if identifier else "value_invalid",
            "subject": str(subject or key)[:700],
            "detail": ("the value is not a typed identifier of its kind here; its text is "
                       "quoted as source text" if identifier else
                       "the value does not fit its value type; its text is quoted as source "
                       "text"),
            "source_text": dict(quoted)}


# A plx.find text match (``ontology_tools`` ``display_text``): its ``normalized`` is the
# agent's own query, matched against a displayed cell, never a value read from a source.
QUERY_TEXT_KIND = "display_text"


def _identifier_record_typed(item: Mapping[str, Any], origin: str | None
                             ) -> tuple[bool, str | None] | None:
    """``(typed, citation)`` of an identifier record's string ``normalized`` (a plx.find
    match, a document mention: a mapping with an ``identifier_kind``), else None. The
    boundary re-checks it per kind as the emitting tool does (``is_identifier``), read
    where the record's own source text envelope (``raw``, ``span``) or its enclosing
    record says it was read; with no citation at all it is held to the source-text rule."""
    kind, value = item.get("identifier_kind"), item.get("normalized")
    if not isinstance(kind, str) or not isinstance(value, str):
        return None
    if kind == QUERY_TEXT_KIND:
        return is_identifier(value, kind), origin
    cited = next((env["origin"]["citation"] for env in (item.get("raw"), item.get("span"))
                  if is_envelope(env) and isinstance(env.get("origin"), Mapping)
                  and isinstance(env["origin"].get("citation"), str)), None) or origin
    if cited is None:
        return is_identifier(value, kind) and (kind == OBJECT_KEY_KIND
                                               or kind_specific(kind)), None
    return is_identifier(value, kind, cited), cited


def _quoted_origin(item: Mapping[str, Any]) -> str | None:
    """The first document a Workbench statement cites (see ``WORKBENCH_TEXT_KEYS``), the
    origin of the source text it quotes; none for any other record."""
    citations = item.get("citations")
    if item.get("statement_by") != "workbench" or not isinstance(citations, (list, tuple)):
        return None
    for cite in citations:
        handle = cite.get("handle") if isinstance(cite, Mapping) else cite
        if (isinstance(handle, str) and _typed("handle", handle)
                and is_source_text_citation(handle)):
            return handle
    return None


def _relation_of(handle: str) -> str:
    """The relation a structured citation names: ``api://<relation>/<key>@...``;
    ``bc://<environment>/<company>/<entity set>/<id>@...`` (else none)."""
    body = handle.split("://", 1)[1]
    if not handle.startswith("bc://"):
        return body.split("/", 1)[0]
    parts = body.split("@", 1)[0].split("/")
    return parts[2] if len(parts) == 4 else ""


def is_row_envelope(value: Any) -> bool:
    """Whether ``value`` is a row envelope (``{"origin": {"citation"}, "cells": {...}}``)."""
    return (isinstance(value, Mapping) and set(value) == ROW_ENVELOPE_KEYS
            and isinstance(value["origin"], Mapping) and set(value["origin"]) == {"citation"}
            and isinstance(value["origin"]["citation"], str)
            and isinstance(value["cells"], Mapping))


def _is_marker(value: Any) -> bool:
    return (isinstance(value, Mapping) and isinstance(value.get("untrusted_text"), str)
            and set(value) <= {"untrusted_text", "truncated"})


def _marker(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"untrusted_text": text[:ENVELOPE_CHARS]}
    if len(text) > ENVELOPE_CHARS:
        out["truncated"] = True  # never cut silently
    return out


def _cell(value: Any, kind: str | None) -> Any:
    if isinstance(value, str):
        if kind is not None and _GRAMMARS[COLUMN_GRAMMARS[kind]].fullmatch(value):
            return value
        return _marker(value)
    if _is_marker(value):  # already marked (a second pass): one marker
        return dict(value)
    if isinstance(value, (Mapping, list, tuple)):  # source JSON: text, never structure
        return _marker(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                  default=str))
    return value


def row_envelope(row: Mapping[str, Any], citation: str,
                 kinds: Mapping[str, str]) -> dict[str, Any]:
    """One structured row as a row envelope (see ``STRUCTURED_SOURCES``). A row already
    enveloped under the same citation is re-checked cell by cell (idempotent)."""
    if is_row_envelope(row) and row["origin"]["citation"] == citation:
        row = row["cells"]
    return {"origin": {"citation": citation},
            "cells": {column: _cell(value, kinds.get(column)) for column, value in row.items()}}


def firewall_tool_output(value: Any, *, citation: str | None = None,
                         sender_class: str | None = None, tool: str | None = None,
                         column_types: ColumnTypes | None = None,
                         contract: ContractTyping | None = None) -> Any:
    """A tool result with every string an ``untrusted_text`` envelope except typed fields.

    The contract is inverted: a string stays bare only as a typed field (``_typed``:
    an allowlisted key and its grammar) or as a typed cell of a structured row, which
    leaves as a row envelope (``row_envelope``; cells typed by ``column_types``, the
    builtin schema's by default). Everything else, source text and prose alike, is
    wrapped. Idempotent; non-strings are unchanged; a list's strings take their key's
    rule.

    Every envelope names an origin citation: a nested mapping naming its own
    ``citation`` (or ``handle``) is the origin of the text below it; Workbench prose
    (``WORKBENCH_TEXT_KEYS``, or text under no citation) has ``workbench://<tool>``, but
    a Workbench statement about a cited document names that document (``_quoted_origin``).
    Text under a ``graph-mail://`` origin carries a ``sender_class``: the record's own
    typed ``sender_class``, else ``sender_class``, else ``unknown`` (an unknown
    sender's text is an external assertion). Inside a document (mail, file, original)
    a nested mapping names an origin only when it cites another document (never a row,
    so never row typing or a Workbench origin), and an envelope found under a cited
    origin keeps only its text (and field path when it cites the same document): its
    origin and sender class are the enclosing record's, never its own.

    ``contract``: the result of a self-enveloping tool (its outputs are
    contract-validated, ``mcp_server.SELF_ENVELOPING_TOOLS``): a string the contract
    types (``ContractTyping.typed``: its key's pattern or enum, or a key whose plain
    strings the contract allows) stays bare, and the tool's own envelopes are kept as
    built."""
    types = builtin_column_types() if column_types is None else column_types
    workbench = WORKBENCH_ORIGIN + (tool or "plx")

    def wrap(text: str, origin: str | None, field_path: str | None,
             sclass: str | None) -> dict[str, Any]:
        cited = origin or workbench
        mail = cited.startswith("graph-mail://")
        return UntrustedText(text, cited, field_path,
                             (sclass or "unknown") if mail else None).envelope()

    def rewrap(item: Mapping[str, Any], origin: str | None, sclass: str | None) -> Any:
        if origin is None or contract is not None:
            return dict(item)
        own = item.get("origin") if isinstance(item.get("origin"), Mapping) else {}
        cited = own.get("citation")
        same = isinstance(cited, str) and cited.split("#", 1)[0] == origin.split("#", 1)[0]
        path = own.get("field_path") if same and isinstance(own.get("field_path"), str) else None
        out = wrap(item["untrusted_text"], cited if same else origin, path, sclass)
        if item.get("truncated") is True:
            out["truncated"] = True
        return out

    def kinds_of(handle: str) -> dict[str, str]:
        source = next(STRUCTURED_SOURCES[s] for s in STRUCTURED_SCHEMES if handle.startswith(s))
        return dict(types.get(source, {}).get(_relation_of(handle), {}))

    def key_part(key: Any, origin: str) -> Any:
        if not isinstance(key, Mapping):
            return walk(key, None, origin, None, "key")
        out = {}
        for column, item in key.items():
            if isinstance(item, str) and _GRAMMARS["token"].fullmatch(item) is None:
                path = f"key.{column}" if _COLUMN_NAME.fullmatch(str(column)) else None
                out[column] = wrap(item, origin, path, None)
            else:
                out[column] = _cell(item, _KEY_COLUMN)
        return out

    def walk(item: Any, key: Any, origin: str | None, sclass: str | None, path: str,
             bc_row: tuple[str, dict[str, str]] | None = None) -> Any:
        if is_envelope(item):  # Workbench prose names the Workbench, as built
            return rewrap(item, None if key in WORKBENCH_TEXT_KEYS else origin, sclass)
        if isinstance(item, str):
            if _typed(key, item) or (contract is not None
                                     and contract.typed(key, item, path)):
                return item
            if key in WORKBENCH_TEXT_KEYS:
                return wrap(item, None, None, None)
            return wrap(item, origin, None, sclass)
        if isinstance(item, Mapping):
            named = next((item[k] for k in ("citation", "handle")
                          if isinstance(item.get(k), str) and _typed(k, item[k])), None)
            if (origin is not None and is_source_text_citation(origin)
                    and not is_source_text_citation(named)):
                named = None  # inside a document only another document is an origin
            here = named or origin
            own = item.get("sender_class")
            here_class = own if own in SENDER_CLASSES else sclass
            structured = isinstance(named, str) and named.startswith(STRUCTURED_SCHEMES)
            kinds: dict[str, str] = {}
            if structured:
                kinds = kinds_of(named)
                if isinstance(item.get("key"), Mapping):  # the row's own key columns
                    kinds.update({column: _KEY_COLUMN for column in item["key"]})
                bc_row = (named, kinds) if named.startswith("bc://") else None
            elif named is not None:
                bc_row = None
            quoted = _quoted_origin(item)
            # A normalized identifier and a value record's value: bare only when typed
            # by their own kind or value type too (the contract types both loosely).
            # Untyped, they leave in the contract's own shape: null with a gap quoting
            # the text (a text or json value: its envelope).
            untyped: dict[str, str | None] = {}
            identifier = _identifier_record_typed(item, here)
            if identifier is not None and not identifier[0]:
                untyped["normalized"] = identifier[1]
            if _value_record_typed(item) is False:
                cited = item.get("citation")
                untyped["value"] = (cited.get("l1_handle") if isinstance(cited, Mapping)
                                    and _typed("handle", str(cited.get("l1_handle")))
                                    else None)
            out = {}
            gaps = {}
            for sub_key, sub in item.items():
                where = f"{path}.{sub_key}" if path else str(sub_key)
                if sub_key == "statement" and quoted is not None and isinstance(sub, str):
                    out[sub_key] = wrap(sub, quoted, None, None)
                elif sub_key in untyped and isinstance(sub, str):
                    quoted = wrap(sub, untyped[sub_key] or here, None, here_class)
                    if sub_key == "value" and item.get("value_type") in ("text", "json"):
                        out[sub_key] = quoted
                    else:
                        out[sub_key] = None
                        gaps[f"{sub_key}_gap"] = _untyped_gap(item, sub_key, quoted)
                elif structured and bc_row is None and sub_key == "key":
                    out[sub_key] = key_part(sub, named)
                elif structured and bc_row is None and sub_key == "row" and isinstance(
                        sub, Mapping):
                    out[sub_key] = row_envelope(sub, named, kinds)
                elif structured and bc_row is None and sub_key == "rows" and isinstance(
                        sub, (list, tuple)):
                    out[sub_key] = [row_envelope(row, named, kinds) if isinstance(row, Mapping)
                                    else walk(row, None, named, None, f"{where}.{index}")
                                    for index, row in enumerate(sub)]
                elif bc_row is not None and sub_key == "fields" and isinstance(
                        sub, Mapping) and contract is None:
                    # A self-enveloping tool's ``fields`` (plx.read) are its contract's
                    # typed values and envelopes, never a raw BC row.
                    out[sub_key] = row_envelope(sub, *bc_row)
                elif key in WORKBENCH_TEXT_KEYS:
                    out[sub_key] = walk(sub, key, here, here_class, where, bc_row)
                else:
                    out[sub_key] = walk(sub, sub_key, here, here_class, where, bc_row)
            out.update({key: gap for key, gap in gaps.items() if key not in out})
            return out
        if isinstance(item, (list, tuple)):
            return [walk(sub, key, origin, sclass, f"{path}.{i}", bc_row)
                    for i, sub in enumerate(item)]
        return item

    return walk(value, None, citation, sender_class, "")


def untyped_strings(value: Any, *, column_types: ColumnTypes | None = None,
                    contract: ContractTyping | None = None) -> list[str]:
    """Paths of strings outside any envelope that :func:`firewall_tool_output` would
    wrap (neither a typed field nor a typed cell of a structured row; a string inside a
    JSON cell counts, the cell is marked whole). Empty for every result that left the
    output boundary."""
    found: list[str] = []
    wrapped = firewall_tool_output(value, column_types=column_types,
                                   contract=contract)

    def strings(before: Any, path: str) -> None:
        if isinstance(before, str):
            found.append(path)
        elif isinstance(before, Mapping):
            for key, sub in before.items():
                strings(sub, f"{path}.{key}")
        elif isinstance(before, (list, tuple)):
            for index, sub in enumerate(before):
                strings(sub, f"{path}.{index}")

    def walk(before: Any, after: Any, path: str) -> None:
        if isinstance(before, str):
            if before != after:
                found.append(path)
        elif is_envelope(before) or _is_marker(before):
            return
        elif is_envelope(after) or _is_marker(after):  # a JSON cell, marked whole
            strings(before, path)
        elif isinstance(before, Mapping):
            if is_row_envelope(after) and not is_row_envelope(before):
                after = after["cells"]
            for key, sub in before.items():
                walk(sub, after[key], f"{path}.{key}" if path else str(key))
        elif isinstance(before, (list, tuple)):
            for index, sub in enumerate(before):
                walk(sub, after[index], f"{path}.{index}")

    walk(value, wrapped, "")
    return found


def bare_text_leaks(value: Any, needle: str) -> list[str]:
    """Paths where ``needle`` occurs in a string outside any envelope or marker (empty =
    the text reaches a reader only as data)."""
    found: list[str] = []

    def walk(item: Any, path: str) -> None:
        if is_envelope(item):
            return
        if isinstance(item, str):
            if needle in item:
                found.append(path)
        elif isinstance(item, Mapping):
            for key, sub in item.items():
                walk(sub, f"{path}.{key}" if path else str(key))
        elif isinstance(item, (list, tuple)):
            for index, sub in enumerate(item):
                walk(sub, f"{path}.{index}")

    walk(value, "")
    return found


# --------------------------------------------------------------------------- provenance

def declares_roles(handle: Any, content: Any) -> bool:
    """Whether a cited record is held to schema typing: anything but a ``fixture://``
    row of a ``fixture.*`` relation that declares no field roles at all (a test fixture
    seeded without them; a real owner's type never binds or reads one)."""
    return not (isinstance(handle, str) and handle.startswith("fixture://")
                and isinstance(content, Mapping)
                and str(content.get("relation")).startswith("fixture.")
                and FIELD_ROLES_KEY not in content)


def typed_values(handle: str, content: Any) -> Iterator[tuple[str, Any]]:
    """``(field_path, value)`` of every schema-typed scalar cell of one resolved citation:
    a top-level ``row.<column>`` whose role the schema bindings or the owner's declared
    ``field_roles`` give (``cited_role``). A free-text, untyped, unknown or nested cell
    never supplies a value. Only a role-less test fixture (``declares_roles``) gives
    every scalar cell (nested objects as dotted paths, ``row.a.b``).

    Nothing for a document citation (rule 2) but the role-typed ``fields.<name>.value``
    cells of its ``documentFields`` row (:func:`document_fields_row`), or content without
    a ``row``; envelopes and booleans/None are skipped."""
    document = document_fields_row(handle, content)
    if document is not None:
        for name, field in (document.get("fields") or {}).items():
            cell = field.get("value") if isinstance(field, Mapping) else None
            path = f"row.fields.{name}.value"
            if (isinstance(cell, (str, int, Decimal)) and not isinstance(cell, bool)
                    and cited_role(handle, content, path) is not None):
                yield path, cell
        return
    if not isinstance(content, Mapping) or is_source_text_citation(handle, content):
        return
    row = content.get("row")
    if not isinstance(row, Mapping):
        return
    if declares_roles(handle, content):
        for column, cell in row.items():
            if (isinstance(cell, (str, int, float, Decimal)) and not isinstance(cell, bool)
                    and cited_role(handle, content, f"row.{column}") is not None):
                yield f"row.{column}", cell
        return

    def walk(item: Any, path: str) -> Iterator[tuple[str, Any]]:
        if is_envelope(item) or item is None or isinstance(item, bool):
            return
        if isinstance(item, Mapping):
            for key, sub in item.items():
                yield from walk(sub, f"{path}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, sub in enumerate(item):
                yield from walk(sub, f"{path}.{index}")
        elif isinstance(item, (str, int, float, Decimal)):
            yield path, item

    yield from walk(row, "row")


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str) and _NUMBER_TEXT.fullmatch(value.strip()):
        try:
            return Decimal(value.strip())
        except InvalidOperation:  # pragma: no cover - the pattern admits only numbers
            return None
    return None


def same_value(parameter: Any, cell: Any) -> bool:
    """Exact equality of a parameter and a cited cell. Text compares as text (after
    trimming); a number, or canonical decimal text (no leading zeros), compares
    numerically with a number or canonical decimal text (``1509`` = ``"1509.0"``;
    ``"0042"`` is an identifier, never 42)."""
    if isinstance(parameter, bool) or isinstance(cell, bool):
        return False
    if isinstance(parameter, str) and isinstance(cell, str):
        left, right = _number(parameter), _number(cell)
        if left is not None and right is not None:  # "1509" = "1509.0" (float-formatted text)
            return left == right
        return bool(parameter.strip()) and parameter.strip() == cell.strip()
    if isinstance(parameter, (int, float, Decimal)) or isinstance(cell, (int, float, Decimal)):
        left, right = _number(parameter), _number(cell)
        return left is not None and right is not None and left == right
    return False


def objective_literal(value: Any, objective: Any) -> bool:
    """Whether ``value`` is written in the objective as a whole token: not part of a
    longer identifier (``7788`` is not in ``PO-7788``; ``PO-7788`` is in ``to PO-7788?``).
    A value without a digit counts only when quoted (``set status to "paid"``)."""
    if not isinstance(objective, str) or isinstance(value, bool):
        return False
    if isinstance(value, int):
        literal = str(value)
    elif isinstance(value, str):
        literal = value.strip()
    else:
        return False
    if not literal:
        return False
    # A plain word (no digit) is part of the question's wording, not a value the user
    # gave ("Has it been received?" does not ask to write "received"): it counts only
    # when the user quoted it exactly.
    if not any(char.isdigit() for char in literal):
        return any(f"{left}{literal}{right}" in objective for left, right in _QUOTES)
    start = objective.find(literal)
    while start >= 0:
        end = start + len(literal)
        before = objective[start - 1] if start else ""
        after = objective[end] if end < len(objective) else ""
        after_next = objective[end + 1] if end + 1 < len(objective) else ""
        joined_before = bool(before) and (before.isalnum() or before in _JOINERS)
        joined_after = bool(after) and (after.isalnum() or after in "_-#"
                                        or (after in ".,/" and after_next.isalnum()))
        if not joined_before and not joined_after:
            return True
        start = objective.find(literal, start + 1)
    return False


# The role of a cited cell and of a written field (trace roles): its value type plus
# what the type leaves open: the identifier kind, the quantity role (planned, produced,
# received ... never mixed) or the money measure (amount or unit_price). Roles come
# from the compiled ontology schema's bindings for api:// and bc:// rows, never from a
# column's name; a fixture:// owner the Workbench runs itself declares them on its
# citation content (``field_roles``). Source text never sets a role. A column the
# schema reads both as raw text and as an identifier takes the kinds whose identifier
# ``carriers`` declare it (identifiers.json); an identifier role is then decided per
# value by the identifier grammar (``cited_role``), and an undecidable one is
# ``AMBIGUOUS_ROLE``, which supplies and takes nothing.
ROLE = re.compile(r"[a-z][a-z_]{0,31}(?::[a-z0-9_]{1,64})?")
FIELD_ROLES_KEY = "field_roles"
AMBIGUOUS_ROLE = "ambiguous"
_IDENTIFIER_ROLE = "identifier:"
_COLUMN_ROLES: list[Mapping[str, Mapping[str, Mapping[str, str]]]] = []
_CARRIED: list[Mapping[tuple[str, str], frozenset[str]]] = []
_CELL = object()  # cited_role's default value: the cited cell's own
_DOCUMENT_ROLES: list[Mapping[tuple[str, str, str], str]] = []
_DOCUMENT_FIELD = re.compile(r"^row\.(fields\.([a-z][a-z0-9_]{0,63})\.value)$")


def _property_role(prop: Mapping[str, Any]) -> Any:
    kind = prop.get("value_type")
    qualifier = {"identifier": prop.get("identifier_kind"),
                 "quantity": prop.get("quantity_role"),
                 "money": prop.get("money_measure") or "amount"}.get(kind)
    return f"{kind}:{qualifier}" if qualifier else kind


def schema_column_roles(schema: Any) -> dict[str, dict[str, dict[str, str]]]:
    """binding source -> relation -> {column: role} of a compiled ontology schema's
    ``portal_api`` and ``bc`` bindings; a column whose properties disagree has none."""
    sources = frozenset(STRUCTURED_SOURCES.values())
    seen: dict[tuple[str, str], dict[str, set[str]]] = {}
    for object_type in schema.object_types.values():
        for prop in object_type.get("properties") or ():
            role = _property_role(prop)
            for source in prop.get("sources") or ():
                found = schema.bindings.get(source.get("binding"))
                path = str(source.get("path") or "")
                if (found is None or found[1].get("source") not in sources or "." in path
                        or not isinstance(role, str)):
                    continue
                seen.setdefault((found[1]["source"], found[1]["relation"]), {}).setdefault(
                    path, set()).add(role)
    out: dict[str, dict[str, dict[str, str]]] = {}
    for (source, relation), columns in seen.items():
        out.setdefault(source, {})[relation] = {
            column: next(iter(found)) for column, found in columns.items() if len(found) == 1}
    return out


def _document_roles() -> Mapping[tuple[str, str, str], str]:
    """``(<source>.documentFields, profile, fields.<name>.value) -> role`` of the committed
    schema's document bindings (one per document source and profile, ``where profile
    eq``); a path whose properties disagree has none."""
    if not _DOCUMENT_ROLES:
        schema = load_schema()
        seen: dict[tuple[str, str, str], set[str]] = {}
        for object_type in schema.object_types.values():
            for prop in object_type.get("properties") or ():
                role = _property_role(prop)
                for source in prop.get("sources") or ():
                    spec = (schema.bindings.get(source.get("binding")) or (None, {}))[1]
                    profile = [item.get("value") for item in spec.get("where") or ()
                               if item.get("path") == "profile" and item.get("op") == "eq"]
                    if (spec.get("relation") == "documentFields" and len(profile) == 1
                            and isinstance(role, str)):
                        seen.setdefault((f"{spec['source']}.documentFields", profile[0],
                                         str(source.get("path"))), set()).add(role)
        _DOCUMENT_ROLES.append({key: next(iter(roles)) for key, roles in seen.items()
                                if len(roles) == 1})
    return _DOCUMENT_ROLES[0]


def document_fields_row(handle: Any, content: Any) -> Mapping[str, Any] | None:
    """The ``documentFields`` row of a byte-pinned document citation, or None: ``content``
    as ``capability_guards.current_reader`` gives it (``pinned_document_row``: the pinned
    bytes re-extracted for the reader), a relation and profile a schema document binding
    declares and a row naming ``handle`` itself as its document. A capture's own view
    (``graph-mail.attachment``, ``sharepoint.driveItems``) is never one."""
    if not isinstance(handle, str) or not isinstance(content, Mapping):
        return None
    row = content.get("row")
    if not isinstance(row, Mapping) or row.get("document") != handle:
        return None
    declared = {(relation, profile) for relation, profile, _ in _document_roles()}
    return row if (content.get("relation"), row.get("profile")) in declared else None


def _carried_kinds(relation: str, column: str) -> frozenset[str]:
    """The identifier kinds whose ``carriers`` declare ``relation.column`` (a carrier's
    relation may list alternatives with ``|``)."""
    if not _CARRIED:
        found: dict[tuple[str, str], set[str]] = {}
        for kind in identifiers.kinds():
            for carrier in identifiers.carriers(kind):
                for name in str(carrier.get("relation") or "").split("|"):
                    found.setdefault((name, str(carrier.get("field"))), set()).add(kind)
        _CARRIED.append({key: frozenset(kinds) for key, kinds in found.items()})
    return _CARRIED[0].get((relation, column), frozenset())


def _shaped_kinds(value: Any) -> frozenset[str]:
    """The kinds whose self-delimiting shapes read the whole ``value``
    (``identifiers.find_identifiers``): ``SO100000`` is a sales-order number."""
    if not isinstance(value, str) or not value.strip():
        return frozenset()
    text = value.strip()
    return frozenset(m.kind for m in identifiers.find_identifiers(text)
                     if m.start == 0 and m.end == len(text) and m.normalized is not None)


def _decided_role(declared: str | None, carried: frozenset[str], value: Any) -> str | None:
    """The role of one cell holding ``value``: ``declared`` (schema or owner) unless it
    is an identifier or absent and carriers declare the column. The identifier kinds the
    column may hold are the declared and carried kinds; the value's grammar decides
    among them and their alias kinds: a whole-value shape of exactly one declared kind
    is that kind, any other shape is ambiguous (an externalDocumentNumber holding an SO
    number is no customer PO). With no shape, the one kind held, else ambiguous (a
    sales invoice's externalDocumentNumber holds an SO number or a BC external No.)."""
    if declared is not None and not declared.startswith(_IDENTIFIER_ROLE):
        return declared
    held = set(carried)
    if declared is not None:
        held.add(declared[len(_IDENTIFIER_ROLE):])
    if not held:
        return None
    known = identifiers.kinds()
    allowed = held.union(*(identifiers.alias_kinds(kind) for kind in held if kind in known))
    shaped = _shaped_kinds(value) & allowed
    if shaped:
        return (_IDENTIFIER_ROLE + next(iter(shaped)) if len(shaped) == 1 and shaped <= held
                else AMBIGUOUS_ROLE)
    return _IDENTIFIER_ROLE + next(iter(held)) if len(held) == 1 else AMBIGUOUS_ROLE


def cited_role(handle: Any, content: Any, field_path: Any, value: Any = _CELL) -> str | None:
    """The role of one top-level cell (``row.<column>``) of a resolved structured
    citation, or None (undeclared, nested, a document). The one role rule of the
    injection hook and the action catalog. ``value``: the value the cell holds (default:
    the cited row's); for a written field, the value written. A document's
    ``documentFields`` row (:func:`document_fields_row`) gives the role its schema
    document binding declares for ``row.fields.<name>.value``."""
    if (not isinstance(handle, str) or not isinstance(content, Mapping)
            or not isinstance(field_path, str) or not field_path.startswith("row.")):
        return None
    document = document_fields_row(handle, content)
    if document is not None:
        found = _DOCUMENT_FIELD.fullmatch(field_path)
        declared = None if found is None else _document_roles().get(
            (content["relation"], document["profile"], found[1]))
        if declared is None:
            return None
        if value is _CELL:
            cell = (document.get("fields") or {}).get(found[2])
            value = cell.get("value") if isinstance(cell, Mapping) else None
        return _decided_role(declared, frozenset(), value)
    if is_source_text_citation(handle, content):
        return None
    column = field_path[len("row."):]
    if value is _CELL:
        row = content.get("row")
        value = row.get(column) if isinstance(row, Mapping) else None
    if handle.startswith("fixture://"):
        declared = content.get(FIELD_ROLES_KEY)
        role = declared.get(column) if isinstance(declared, Mapping) else None
        return (_decided_role(role, frozenset(), value)
                if isinstance(role, str) and ROLE.fullmatch(role) else None)
    source = next((STRUCTURED_SOURCES[s] for s in ("api://", "bc://") if handle.startswith(s)),
                  None)
    if source is None:
        return None
    if not _COLUMN_ROLES:
        _COLUMN_ROLES.append(schema_column_roles(load_schema()))
    relation = str(content.get("relation"))
    return _decided_role(_COLUMN_ROLES[0].get(source, {}).get(relation, {}).get(column),
                         _carried_kinds(relation, column), value)


def same_role(written: str | None, cited: str | None) -> bool:
    """Whether a cell of role ``cited`` may supply a field of role ``written``. An
    ambiguous role on either side never fits; a field with no declared role takes any
    other cell; a declared one needs the same role, an identifier also one of its
    kind's declared alias kinds (identifiers.json)."""
    if AMBIGUOUS_ROLE in (written, cited):
        return False
    if written is None or written == cited:
        return True
    if cited is None or not (written.startswith(_IDENTIFIER_ROLE)
                             and cited.startswith(_IDENTIFIER_ROLE)):
        return False
    left, right = written.split(":", 1)[1], cited.split(":", 1)[1]
    known = identifiers.kinds()
    return (left in known and right in known
            and (right in identifiers.alias_kinds(left) or left in identifiers.alias_kinds(right)))


def trace_parameter(value: Any, evidence: Sequence[Mapping[str, Any]],
                    objective: Any, *, skip: Iterable[tuple[str, str]] = (),
                    not_from_objective: Iterable[Any] = (), role: str | None = None,
                    mismatched: list[dict[str, Any]] | None = None
                    ) -> dict[str, Any] | None:
    """Where one parameter value comes from, or None (untraced).

    ``evidence``: ``CaseStore.proposal_evidence`` items (content present only while it
    is exactly what was cited). The first matching typed cell of a fitting role wins
    (``role``: the written field's, ``same_role``), then the objective. ``objective``
    is text a human wrote (``check_registration`` passes None for an agent-written or
    unattributed question). ``skip``: ``(handle, field)`` cells that are never a source
    (the target row's own key columns); ``not_from_objective``: values the objective
    never supplies (the case's own subject identifiers). ``mismatched`` collects the
    equal cells refused for their role (citation, field, role)."""
    skipped = set(skip)
    for item in evidence:
        handle = item.get("handle", "")
        content = item.get("content")
        for path, cell in typed_values(handle, content):
            if (handle, path) in skipped or not same_value(value, cell):
                continue
            found = cited_role(handle, content, path)
            if same_role(role, found):
                trace = {"source": "citation", "citation": item["handle"], "field": path,
                         "relation": content.get("relation")}
                return trace if found is None else {**trace, "role": found}
            if mismatched is not None:
                mismatched.append({"citation": item["handle"], "field": path, "role": found})
    if any(same_value(value, ident) for ident in not_from_objective):
        return None
    if objective_literal(value, objective):
        return {"source": "objective", "author": "human"}
    return None


def trace_parameters(parameters: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]],
                     objective: Any) -> dict[str, Any]:
    """Trace every named parameter. ``traced`` is false when any is untraced; values
    are never echoed (a trace names a citation and field only)."""
    traces: dict[str, Any] = {}
    untraced: list[str] = []
    for name, value in parameters.items():
        found = trace_parameter(value, evidence, objective)
        traces[name] = found
        if found is None:
            untraced.append(name)
    return {"traced": not untraced, "reason": REFUSAL if untraced else None,
            "untraced": untraced, "traces": traces}


def _target(effect: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]
            ) -> Mapping[str, Any] | None:
    for item in evidence:
        content = item.get("content")
        if (isinstance(content, Mapping) and item.get("handle") == effect.get("target_citation")
                and content.get("relation") == effect.get("relation")
                and content.get("key") == effect.get("key")
                and isinstance(content.get("row"), Mapping)
                and not is_source_text_citation(item["handle"], content)):
            return item
    return None


def subject_identifiers(effect: Mapping[str, Any], roots: Iterable[Any] = ()) -> list[Any]:
    """The case's own subject identifiers: the target key's scalar values, each root,
    and each digit run inside a root (``SO-41`` gives ``SO-41`` and ``41``). A number
    in the question that names the subject is not a value to write."""
    found: list[Any] = []
    key = effect.get("key")
    if isinstance(key, Mapping):
        found += [v for v in key.values() if isinstance(v, (str, int, float, Decimal))
                  and not isinstance(v, bool)]
    for root in roots or ():
        if isinstance(root, str) and root.strip():
            found.append(root.strip())
            found += [int(run) for run in re.findall(r"[0-9]+", root)
                      if run == str(int(run))]
    return found


def effect_trace(effect: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]],
                 objective: Any, roots: Iterable[Any] = (),
                 roles: Mapping[str, str | None] | None = None) -> dict[str, Any]:
    """Trace the parameters of one expected effect (what a dispatch would send).

    ``relation``/``key`` must be the cited target row's (a structured row, unchanged);
    each field's column must be one the target row has (a schema name observed on the
    cited row, never a column named only by text); ``equals`` must trace by
    :func:`trace_parameter` to a cell of the written field's role (``cited_role``;
    a value equal only to a cell of another role refuses with ``REFUSAL_ROLE``), never
    from the target row's own key columns and never from a subject identifier written
    in the objective. ``roles``: field -> the role the action type writes there (the
    catalog parameter's); a field it leaves out, or sets to None, takes the cited target
    field's role for the value written; a field left with no role at all is refused
    (``REFUSAL_UNTYPED_FIELD``) unless the target is a role-less test fixture
    (``declares_roles``). A text ``equals`` with
    leading or trailing whitespace is refused (``REFUSAL_UNNORMALIZED``): matching
    trims, so dispatch could otherwise send a value other than the one traced."""
    target = _target(effect, evidence)
    traces: dict[str, Any] = {}
    untraced: list[str] = []
    reason = None
    target_trace = (None if target is None else
                    {"source": "citation", "citation": target["handle"], "field": "key",
                     "relation": target["content"].get("relation")})
    for name in ("relation", "key"):
        traces[name] = target_trace
        if target_trace is None:
            untraced.append(name)
    row = target["content"]["row"] if target else {}
    for index, field in enumerate(effect.get("fields") or ()):
        name = f"fields.{index}"
        column = field.split(".", 1)[0] if isinstance(field, str) else None
        found = (None if target is None or column not in row else
                 {"source": "citation", "citation": target["handle"],
                  "field": f"row.{column}", "relation": target["content"].get("relation")})
        traces[name] = found
        if found is None:
            untraced.append(name)
    key = effect.get("key") if isinstance(effect.get("key"), Mapping) else {}
    skip = ([(target["handle"], f"row.{column}") for column in key] if target else [])
    written = _written_values(effect)
    if not written:
        traces["equals"] = None
        untraced.append("equals")
    role_refused = False
    for field, value in written.items():
        name = value_trace_name(field, written)
        role = (roles or {}).get(field)
        if role is None and target is not None:
            role = cited_role(target["handle"], target["content"],
                              f"row.{field.split('.', 1)[0]}", value)
        if role is None and (target is None or declares_roles(target["handle"],
                                                              target["content"])):
            # A field no role types (neither the action nor the owner's schema) takes
            # no value: refused for every real owner (only a role-less fixture is not).
            traces[name] = None
            reason = reason or REFUSAL_UNTYPED_FIELD
        elif isinstance(value, str) and value != value.strip():
            traces[name] = None
            reason = REFUSAL_UNNORMALIZED
        else:
            mismatched: list[dict[str, Any]] = []
            traces[name] = trace_parameter(value, evidence, objective, skip=skip,
                                           not_from_objective=subject_identifiers(effect,
                                                                                  roots),
                                           role=role, mismatched=mismatched)
            role_refused = role_refused or (traces[name] is None and bool(mismatched))
            if traces[name] is not None and role is not None:
                traces[name]["written_role"] = role
        if traces[name] is None:
            untraced.append(name)
    if untraced and reason is None:
        reason = REFUSAL_ROLE if role_refused else REFUSAL
    return {"traced": not untraced, "reason": reason, "untraced": untraced, "traces": traces}


def _written_values(effect: Mapping[str, Any]) -> dict[str, Any]:
    """field -> value the effect writes: ``values`` (one per field), or the one-field
    ``fields``/``equals`` form of older records (each listed field gets ``equals``)."""
    values = effect.get("values")
    if isinstance(values, Mapping):
        return {str(field): value for field, value in values.items()}
    fields = effect.get("fields")
    if not isinstance(fields, (list, tuple)):
        return {}
    return {field: effect.get("equals") for field in fields if isinstance(field, str)}


def value_trace_name(field: str, written: Mapping[str, Any]) -> str:
    """The trace key of a written value: ``equals`` for a one-field effect, else
    ``values.<field>``."""
    return "equals" if len(written) == 1 else f"values.{field}"


def _value_trace_names(traces: Mapping[str, Any]) -> list[str]:
    return [name for name in traces if name == "equals" or name.startswith("values.")]


def objective_author_kind(case: Any) -> str:
    """``human``, ``agent`` or ``unknown``: who wrote a case's question, as the channel
    that opened the case recorded it (``CaseStore.open(objective_author_kind=...)``)."""
    kind = case.get("objective_author_kind") if isinstance(case, Mapping) else None
    return kind if kind in ("human", "agent") else "unknown"


def human_objective(case: Any) -> str | None:
    """The case question when a human wrote it, else None (agent or unknown author:
    fails closed). The one objective-provenance rule: every check that lets a value
    trace to "the user's objective" reads the objective through this helper."""
    if objective_author_kind(case) != "human":
        return None
    question = case.get("question")
    return question if isinstance(question, str) else None


def objective_supplies(value: Any, case: Any,
                       effect: Mapping[str, Any] | None = None) -> bool:
    """Whether the case's human-written question supplies ``value``: the question has
    a human author (:func:`human_objective`; agent or unknown fails closed), the value
    is written in it as a whole token (:func:`objective_literal`) and it is not one of
    the case's own subject identifiers (:func:`subject_identifiers`: the target key
    of ``effect``, the roots and their digit runs). Roots themselves never supply a
    value. The one objective rule for the injection hook and the action catalog."""
    text = human_objective(case)
    if text is None:
        return False
    roots = case.get("roots") or () if isinstance(case, Mapping) else ()
    if any(same_value(value, ident) for ident in subject_identifiers(effect or {}, roots)):
        return False
    return objective_literal(value, text)


def check_registration(cases: Any, actor: Any, case_id: str, proposal_id: str,
                       proposal: Mapping[str, Any], *,
                       evidence: Sequence[Mapping[str, Any]] | None = None
                       ) -> dict[str, Any]:
    """The action-path registration hook: trace one proposal's dispatched parameters
    against what the proposer cited (re-read for ``actor``) and the case objective.

    The objective is a source only when a human wrote it. A question an agent
    supplied (MCP ``plx.case_open``), or one whose author the opening channel did not
    record, is agent text: a value it alone carries is refused with
    ``REFUSAL_AGENT_OBJECTIVE``, so an agent restating a mail's request in its own
    question cannot launder the mail's value. ``evidence``: the caller's
    ``proposal_evidence`` for ``actor`` (``ActionPath.register``'s, its document
    citations read as :func:`document_fields_row` rows); default: read here."""
    effect = proposal.get("expected_effect")
    if not isinstance(effect, Mapping):
        return {"traced": False, "reason": REFUSAL, "untraced": ["expected_effect"],
                "traces": {}, "objective_author_kind": None}
    if evidence is None:
        evidence = cases.proposal_evidence(actor, case_id, proposal_id)
    case = cases.get(actor, case_id)
    author = objective_author_kind(case)
    roots = case.get("roots") or ()
    result = effect_trace(effect, evidence, human_objective(case), roots)
    untraced = result["untraced"]
    if (result["reason"] == REFUSAL and author != "human" and untraced
            and set(untraced) <= set(_value_trace_names(result["traces"]))
            and effect_trace(effect, evidence, case.get("question"), roots)["traced"]):
        result["reason"] = REFUSAL_AGENT_OBJECTIVE
    return {**result, "objective_author_kind": author}


def registration_refusal(cases: Any, actor: Any, case_id: str, proposal_id: str,
                         proposal: Mapping[str, Any],
                         checked: Mapping[str, Any] | None = None) -> str | None:
    """The refusal code when :func:`check_registration` fails, else None.
    ``checked``: its result when the caller already has it."""
    if checked is None:
        checked = check_registration(cases, actor, case_id, proposal_id, proposal)
    return checked["reason"]


def provenance_summary(traces: Mapping[str, Any]) -> str:
    """One short line for the approver: where the written value came from (never the
    value itself; the gate reason already states it)."""
    if not isinstance(traces, Mapping):
        return "value source: untraced"
    names = _value_trace_names(traces) or ["equals"]
    parts = []
    for name in names:
        found = traces.get(name)
        label = "" if name == "equals" else name[len("values."):] + ": "
        if not isinstance(found, Mapping):
            parts.append(label + "untraced")
        elif found.get("source") == "objective":
            parts.append(label + "case question written by a human")
        else:
            role = f" as {found['role']}" if isinstance(found.get("role"), str) else ""
            parts.append(f"{label}cited {found.get('relation') or 'row'} {found.get('field')}"
                         f"{role}")
    return "value source: " + "; ".join(parts)


# --------------------------------------------------------------------------- assertions

def is_external_assertion(sender_class: Any, quoted_history: bool = False) -> bool:
    """Text an internal sender did not write: an external or unknown sender (anything
    but ``internal``), or quoted history in any message."""
    return sender_class != "internal" or bool(quoted_history)


def may_confirm(fact: str, supports: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Whether these supports may confirm ``fact`` (``payment``, ``receipt``,
    ``quantity``, or any other class such as ``reference``).

    A support is ``{"sender_class", "quoted_history"?, "tier"}`` (one mention, one
    cited message). A payment, receipt or quantity fact needs a support an internal
    sender wrote at a confirming tier (``ontology.mentions.CONFIRMING_TIERS``);
    external assertions never confirm one by themselves. Same verdicts as
    ``ontology.mentions.confirmation_support`` over mention links."""
    supports = list(supports)
    internal = [s for s in supports
                if not is_external_assertion(s.get("sender_class"), s.get("quoted_history"))]
    external = len(supports) - len(internal)
    if fact not in EXTERNAL_UNCONFIRMABLE:
        return {"fact": fact, "supported": bool(supports),
                "reason": None if supports else "no_support", "external": external}
    usable = [s for s in internal if s.get("tier") in CONFIRMING_TIERS]
    reason = (None if usable else "lead_only" if internal
              else "external_assertion_only" if external else "no_support")
    return {"fact": fact, "supported": bool(usable), "reason": reason, "external": external}


__all__ = ["ENVELOPE_CHARS", "REFUSAL", "REFUSAL_AGENT_OBJECTIVE", "REFUSAL_UNNORMALIZED",
           "REFUSAL_UNTYPED_FIELD", "CASE_AUTHOR_KINDS", "declares_roles",
           "human_objective", "objective_author_kind", "objective_supplies", "provenance_summary", "subject_identifiers", "TYPED_FIELDS", "WORKBENCH_ORIGIN",
           "WORKBENCH_TEXT_KEYS", "COLUMN_GRAMMARS", "builtin_column_types", "schema_column_types", "SOURCE_TEXT_RELATION_PREFIXES",
           "SOURCE_TEXT_SCHEMES", "UntrustedText", "bare_text_leaks", "check_registration",
           "effect_trace", "firewall_content", "firewall_tool_output", "is_envelope",
           "is_external_assertion", "is_source_text_citation", "may_confirm",
           "objective_literal", "registration_refusal", "same_value", "trace_parameter",
           "trace_parameters", "untyped_strings", "typed_values", "is_row_envelope",
           "row_envelope", "ROW_ENVELOPE_KEYS", "STRUCTURED_SOURCES", "ContractTyping",
           "contract_typing", "INVISIBLE_CHARS", "VISIBLE_CHAR", "OBJECT_KEY_KIND", "identifier_kind_of",
           "is_identifier", "is_unit", "GENERIC_IDENTIFIER_SHAPES", "kind_specific",
           "VALUE_TYPE_GRAMMARS", "REFUSAL_ROLE", "ROLE", "FIELD_ROLES_KEY", "AMBIGUOUS_ROLE",
           "cited_role", "document_fields_row",
           "same_role", "schema_column_roles"]
