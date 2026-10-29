"""A person confirms the link a blocked case is missing, in the console.

A case can stop on a link no source states: the landed-cost case's proforma invoice names
no purchase order (``pi_not_linked_to_po``), a forwarder quote prices a purchase order it
does not name (``quote_not_linked_to_po``), and the purchase-order document chain's
supplier numbers join nothing until a person says which purchase order they name
(``ontology.mentions.reference_alias_outcome``). This module is the console's one way to
let a signed-in (or fixture) employee say so:

- ``prepare_link`` / ``prepare_alias`` read, as the session's reader, the record and the
  document the person names (an object id, or a number found with ``plx.find``) and
  propose one ``plx.review`` decision: ``accept_link`` of the one link type the schema
  declares between the two records' types, citing each end's own identifier field, or
  ``alias`` of the supplier number the document states (an identifier kind whose
  ``reference_alias`` is the record's type), citing the field that states it. Nothing is
  written; the decision is shown to the person with a plain sentence.
- ``confirm`` records exactly that decision through ``plx.review`` mode ``record`` (the
  session is its author: a person, never an agent; ``identity_review_required`` without
  the ``review:identity`` grant), so the human link is cited, attributed and timed in the
  review store. ``plx.find`` reads an alias at once; a link is in the next projection
  build (``build_steps.post_project``'s review step), which every case reads.
- ``undo`` retracts one of the record's confirmed links or aliases (``plx.review``
  ``retract``, as guarded as the decision); ``links`` lists those in force with who and
  when.

Nothing here decides a link: it only records what a person confirmed, every refusal is
``plx.review``'s own (in plain words, ``PLAIN``), and a request outside the modes and
fields below fails closed (``ValueError("link_confirm_invalid")``) before any call.

The live check ``case.human_link`` (``scripts/eval_human_link_check.py``)
reads the links confirmed here back from the console's review store, read only.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

Call = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]

GRANT = "review:identity"
CONFIRMED = ("accept_link", "alias")
# Request mode -> (required fields, optional fields).
REQUEST_MODES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "prepare_link": (frozenset({"record", "document"}), frozenset({"link_type"})),
    "prepare_alias": (frozenset({"record", "document"}), frozenset({"number"})),
    "confirm": (frozenset({"review"}), frozenset()),
    "undo": (frozenset({"record", "review_id"}), frozenset()),
    "links": (frozenset({"record"}), frozenset()),
}
_DECISION_FIELDS = {"accept_link": frozenset({"decision", "subject", "target", "link_type",
                                              "evidence"}),
                    "alias": frozenset({"decision", "subject", "alias", "evidence"})}
_EVIDENCE_FIELDS = frozenset({"l1_handle", "digest", "field_path"})
_TEXT_MAX = 2000
# An identifier is shown in a sentence only when it is plainly one (source text never is).
_SHOWABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./#-]{0,63}")

# The landed-cost oracle's missing-link codes and the link a person confirms for each.
MISSING_LINKS = {"pi_not_linked_to_po": "SupplierConfirmation.confirms",
                 "quote_not_linked_to_po": "ForwarderQuote.for_purchase_order"}
# ``reference_alias_outcome`` -> what the supplier-number join needs.
ALIAS_STATES = {"alias_joined": "answered", "alias_lead_pending": "confirm_needed",
                "no_corroborated_target": "confirm_needed", "several_targets": "confirm_needed",
                "alias_rejected": "rejected"}
WHERE = "console: the purchase order's workspace, Gaps, Confirm a missing link"

PLAIN = {
    "identity_review_required": "You may not confirm links between records: that needs the "
                                "identity review grant (review:identity). Ask an identity "
                                "reviewer to confirm it.",
    "review_needs_a_person": "Only a person signed in as an employee can confirm a link; an "
                             "agent can only propose one.",
    "review_needs_session": "This console has no signed-in session, so nobody could be named "
                            "as the author. Sign in and try again.",
    "session_actor_mismatch": "Your session has changed or expired. Sign in again and try "
                              "again.",
    "evidence_changed": "One of the records changed since you read it. Reload it and confirm "
                        "again.",
    "evidence_not_found": "One of the records is not one you can read now.",
    "evidence_field_not_found": "The cited number is not one you can read on that record.",
    "evidence_ambiguous": "The cited document is shared by several records; open the one "
                          "you mean.",
    "object_not_found": "No record by that id is one you can read.",
    "undeclared_link": "These two kinds of record are not linked this way.",
    "alias_not_in_cited_field": "The document does not state that number.",
    "review_not_found": "There is no confirmed link by that id on this record that you can "
                        "read.",
    "document_not_found": "No document you can read matches that, linkable to this record.",
    "document_ambiguous": "Several documents you can read match that; open the one you mean "
                          "and use its id.",
    "no_link_between": "These two kinds of record cannot be linked.",
    "link_type_ambiguous": "These two kinds of record can be linked in more than one way; "
                           "choose the link.",
    "no_supplier_number": "That document states no supplier number that could name this "
                          "record.",
    "number_ambiguous": "That document states several supplier numbers; enter the one you "
                        "mean.",
    "number_not_in_document": "That document does not state that number.",
    "no_identifier_to_cite": "One of the records has no number of its own to cite.",
    "tool_not_configured": "This console does not record reviews.",
}


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def says(code: str) -> str:
    return PLAIN.get(code, f"The link was not recorded ({code}).")


def _refused(code: str) -> dict[str, Any]:
    return {"status": "refused", "code": code, "says": says(code),
            "business_system_writes": 0}


def _text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _TEXT_MAX


def _invalid() -> ValueError:
    return ValueError("link_confirm_invalid")


def _checked_request(payload: Any) -> tuple[str, dict[str, Any]]:
    """The request's mode and fields, or ``ValueError`` before anything is called."""
    if not isinstance(payload, Mapping) or payload.get("mode") not in REQUEST_MODES:
        raise _invalid()
    required, optional = REQUEST_MODES[payload["mode"]]
    fields = {k: v for k, v in payload.items() if k != "mode"}
    if not required <= fields.keys() <= required | optional:
        raise _invalid()
    for key, value in fields.items():
        if key == "review":
            _checked_review(value)
        elif not _text(value):
            raise _invalid()
    return payload["mode"], fields


def _checked_review(review: Any) -> None:
    """Only the two decisions ``prepare_*`` proposes, with only their fields."""
    if not isinstance(review, Mapping) or review.get("decision") not in _DECISION_FIELDS \
            or set(review) != _DECISION_FIELDS[review["decision"]]:
        raise _invalid()
    evidence = review["evidence"]
    if not isinstance(evidence, list) or not 0 < len(evidence) <= 4 or any(
            not isinstance(item, Mapping) or not {"l1_handle", "digest"} <= set(item)
            <= _EVIDENCE_FIELDS or not all(_text(v) for v in item.values())
            for item in evidence):
        raise _invalid()
    if review["decision"] == "alias":
        alias = review["alias"]
        if not isinstance(alias, Mapping) or set(alias) != {"identifier_kind", "value"} \
                or not all(_text(v) for v in alias.values()):
            raise _invalid()
    for key in _DECISION_FIELDS[review["decision"]] - {"evidence", "alias", "decision"}:
        if not _text(review[key]):
            raise _invalid()


def _error(result: Mapping[str, Any]) -> str | None:
    error = result.get("error") if isinstance(result, Mapping) else None
    if error is None:
        return None
    code = error.get("code") if isinstance(error, Mapping) else error
    return str(code) if isinstance(code, str) and _SHOWABLE.fullmatch(code) else "tool_error"


def _schema(schema: Any) -> Any:
    if schema is None:
        from .ontology_tools import default_schema
        schema = default_schema()
    return schema


def _grammar(grammar: Any) -> Any:
    if grammar is None:
        from .ontology_tools import default_grammar
        grammar = default_grammar()
    return grammar


def _type_of(object_id: str) -> str:
    return object_id.split(":", 1)[0]


# ---------------------------------------------------------------- reading


def _object(call: Call, object_id: str) -> Mapping[str, Any]:
    found = call("plx.object", {"object_id": object_id})
    code = _error(found)
    if code is not None:
        raise Refused("object_not_found" if code == "invalid_input" else code)
    return found


def _card(found: Mapping[str, Any]) -> dict[str, Any]:
    """The record as the tools showed it (display text stays in its untrusted envelope)."""
    obj = found.get("object") or {}
    return {key: obj.get(key) for key in ("object_id", "object_type", "display")}


def _documents(call: Call, schema: Any, text: str, types: set[str]
               ) -> Mapping[str, Any]:
    """The one record of ``types`` that ``text`` names: an object id, or a number
    ``plx.find`` matches as an identifier (``ROOT_REASONS``, ``LINKED_REASON``; a display
    text or filter match is a lead, never the record a person named);
    ``document_not_found`` / ``document_ambiguous`` otherwise."""
    from .ontology_tools import LINKED_REASON, ROOT_REASONS

    if _type_of(text) in schema.object_types and ":" in text:
        if _type_of(text) not in types:
            raise Refused("document_not_found")
        return _object(call, text)
    found = call("plx.find", {"query": text})
    code = _error(found)
    if code is not None and code != "invalid_input":
        raise Refused(code)
    ids = sorted({c["object"]["object_id"] for c in (found.get("candidates") or [])
                  if isinstance(c, Mapping) and isinstance(c.get("object"), Mapping)
                  and c["object"].get("object_type") in types
                  and c.get("reason") in ROOT_REASONS | {LINKED_REASON}})
    if not ids:
        raise Refused("document_not_found")
    if len(ids) > 1:
        raise Refused("document_ambiguous")
    return _object(call, ids[0])


def _links_between(schema: Any, one: str, other: str) -> list[tuple[str, str, str]]:
    """(link type, from type, to type) of every declared link between two types that is
    not an identity (a link between records of one type)."""
    return sorted((lid, link["from"], link["to"]) for lid, link in schema.link_types.items()
                  if link.get("from") != link.get("to")
                  and {link.get("from"), link.get("to")} == {one, other})


def _identifier_citation(schema: Any, found: Mapping[str, Any]) -> dict[str, str]:
    """The citation of the record's first identifier value (schema property order) that
    cites a field: what the person reads to say which record it is."""
    object_type = (found.get("object") or {}).get("object_type")
    order = [p["name"] for p in schema.object_types[object_type]["properties"]
             if p.get("value_type") == "identifier"]
    values = sorted((v for v in found.get("values") or []
                     if v.get("property") in order and v.get("value") is not None
                     and (v.get("citation") or {}).get("field_path")),
                    key=lambda v: order.index(v["property"]))
    if not values:
        raise Refused("no_identifier_to_cite")
    citation = values[0]["citation"]
    return {key: citation[key] for key in ("l1_handle", "digest", "field_path")}


def _number(found: Mapping[str, Any], *properties: str) -> str | None:
    """The record's first identifier value among ``properties`` that is plainly one."""
    for value in found.get("values") or []:
        text = value.get("value")
        if value.get("property") in properties and isinstance(text, str) \
                and _SHOWABLE.fullmatch(text):
            return text
    return None


def _named(object_type: str, number: str | None) -> str:
    return f"{object_type} {number}" if number else f"this {object_type}"


def _identifiers(schema: Any, object_type: str) -> tuple[str, ...]:
    return tuple(p["name"] for p in schema.object_types[object_type]["properties"]
                 if p.get("value_type") == "identifier")


def _prepare_link(call: Call, fields: Mapping[str, Any], schema: Any) -> dict[str, Any]:
    record = _object(call, fields["record"])
    record_type = record["object"]["object_type"]
    linkable = {t for t in schema.object_types if _links_between(schema, record_type, t)}
    document = _documents(call, schema, fields["document"], linkable)
    document_type = document["object"]["object_type"]
    declared = _links_between(schema, record_type, document_type)
    if "link_type" in fields:
        declared = [d for d in declared if d[0] == fields["link_type"]]
        if not declared:
            raise Refused("no_link_between")
    if len(declared) > 1:
        raise Refused("link_type_ambiguous")
    link_type, from_type, _ = declared[0]
    subject, target = ((document, record) if from_type == document_type
                       else (record, document))
    review = {"decision": "accept_link", "subject": subject["object"]["object_id"],
              "target": target["object"]["object_id"], "link_type": link_type,
              "evidence": [_identifier_citation(schema, subject),
                           _identifier_citation(schema, target)]}
    doc_name = _named(document_type, _number(document, *_identifiers(schema, document_type)))
    rec_name = _named(record_type, _number(record, *_identifiers(schema, record_type)))
    return {"status": "ready", "review": review, "document": _card(document),
            "record": _card(record),
            "says": f"Confirm that {doc_name} belongs to {rec_name} ({link_type}). Your name "
                    "and the time are recorded with the numbers both records state; you can "
                    "undo it."}


def _prepare_alias(call: Call, fields: Mapping[str, Any], schema: Any, grammar: Any
                   ) -> dict[str, Any]:
    record = _object(call, fields["record"])
    record_type = record["object"]["object_type"]

    def alias_properties(object_type: str) -> dict[str, str]:
        out = {}
        for prop in schema.object_types[object_type]["properties"]:
            kind = prop.get("identifier_kind")
            if prop.get("value_type") == "identifier" and kind in grammar.kinds() \
                    and record_type in (grammar.reference_alias_of(kind) or ()):
                out[prop["name"]] = kind
        return out

    types = {t for t in schema.object_types if t != record_type and alias_properties(t)}
    document = _documents(call, schema, fields["document"], types)
    properties = alias_properties(document["object"]["object_type"])
    stated = [v for v in document.get("values") or []
              if v.get("property") in properties and isinstance(v.get("value"), str)
              and (v.get("citation") or {}).get("field_path")]
    if "number" in fields:
        wanted = fields["number"]
        stated = [v for v in stated if grammar.normalize(properties[v["property"]], wanted)
                  and grammar.normalize(properties[v["property"]], v["value"])
                  == grammar.normalize(properties[v["property"]], wanted)]
        if not stated:
            raise Refused("number_not_in_document")
    if not stated:
        raise Refused("no_supplier_number")
    distinct = {(properties[v["property"]],
                 grammar.normalize(properties[v["property"]], v["value"])) for v in stated}
    if len(distinct) > 1:
        raise Refused("number_ambiguous")
    value = stated[0]
    kind = properties[value["property"]]
    citation = value["citation"]
    review = {"decision": "alias", "subject": record["object"]["object_id"],
              "alias": {"identifier_kind": kind, "value": value["value"]},
              "evidence": [{key: citation[key]
                            for key in ("l1_handle", "digest", "field_path")}]}
    number = value["value"] if _SHOWABLE.fullmatch(value["value"]) else None
    rec_name = _named(record_type, _number(record, *_identifiers(schema, record_type)))
    return {"status": "ready", "review": review, "document": _card(document),
            "record": _card(record),
            "says": f"Confirm that the supplier number {number or 'this document states'} "
                    f"is {rec_name}. Your name and the time are recorded with the document "
                    "that states it; you can undo it."}


def _recorded(result: Mapping[str, Any], status: str) -> dict[str, Any]:
    review = dict(result["review"])
    author = (review.get("author") or {}).get("principal")
    who = author if isinstance(author, str) and _SHOWABLE.fullmatch(author.replace(
        "+", ".").replace("@", ".")) else "you"
    when = review.get("recorded_at")
    done = "Undone" if status == "undone" else "Confirmed"
    return {"status": status, "review": review, "projection": result.get("projection"),
            "business_system_writes": result.get("business_system_writes", 0),
            "says": f"{done} by {who} at {when}. The next projection build "
                    + ("drops it." if status == "undone" else "uses it; you can undo it.")}


def _links(call: Call, record: str) -> list[dict[str, Any]]:
    status = call("plx.review", {"mode": "status", "subject": record})
    code = _error(status)
    if code is not None:
        raise Refused(code)
    return [{key: review[key] for key in ("review_id", "decision", "subject", "target",
                                          "link_type", "alias", "author", "recorded_at",
                                          "evidence") if key in review}
            for review in status.get("reviews") or []
            if review.get("decision") in CONFIRMED and review.get("state") == "effective"]


def request(call: Call, payload: Any, *, schema: Any = None, grammar: Any = None
            ) -> dict[str, Any]:
    """One console request (module docstring) through ``call(name, arguments)``, the
    session's own tool call. ``ValueError("link_confirm_invalid")`` for a request outside
    ``REQUEST_MODES``; a refusal is ``{"status": "refused", "code", "says"}``."""
    mode, fields = _checked_request(payload)
    try:
        if mode == "prepare_link":
            return _prepare_link(call, fields, _schema(schema))
        if mode == "prepare_alias":
            return _prepare_alias(call, fields, _schema(schema), _grammar(grammar))
        if mode == "links":
            _object(call, fields["record"])
            return {"status": "links", "record": fields["record"],
                    "links": _links(call, fields["record"])}
        if mode == "undo":
            if fields["review_id"] not in {link["review_id"]
                                           for link in _links(call, fields["record"])}:
                raise Refused("review_not_found")
            arguments = {"mode": "record", "decision": "retract",
                         "retracts": fields["review_id"]}
        else:
            arguments = {"mode": "record", **fields["review"]}
        result = call("plx.review", arguments)
        code = _error(result)
        if code is not None:
            raise Refused(code)
        return _recorded(result, "undone" if mode == "undo" else "confirmed")
    except Refused as refused:
        return _refused(refused.code)
