"""The final-answer gate: the one function every answer path of the Workbench runs.

measure what ships: the console's Ask (``operating.agent_loop``) and an
employee's MCP session (``plx.case_get`` answer mode on ``operating.mcp_server``, where
Claude Code or any MCP client submits its final claims; the real-agent eval's agent is
one such client) both call ``gate_answer``. Nothing else decides which claims of a final answer
stand; ``evals.runner`` records what this gate returned in the session, from the
session's own tool results, and its final reply holds only what this gate passed
(``evals.runner.gate_final_reply`` over ``unbacked``).

``gate_answer(decision, server, seen=, returned=, may_repair=)``:

* Claims: an answer is a list of structured claims (``CLAIM_KEYS``), each one value
  (``value``) read from one record (``citation``, an L1 handle, and ``field``), counted
  or summed over cited records (``op`` ``count`` or ``sum`` ``of`` them) or computed by an
  ``expression`` over figures cited texts print (``op`` ``expression``, each input a
  figure the gate checks on its text, ``claim_checks._check_expression``), with optional
  ``role``, ``unit``, ``currency``, ``quote`` (a cited claim's passage as written, checked
  as well as the value: a quote the cited text does not hold refuses the claim), an ``id``
  and ``lead`` (the value rests on a lead-tier link), plus short prose (``text``) that names claims by id (``{c1}``). A key the gate
  does not know, a bad id or a duplicate one refuses the claim (``claim_invalid``). An MCP
  client's claim (``text``, ``citations``, ``value_claims``) is one claim per value claim
  (``claim_checks.parse_value_claims``; malformed: ``value_claims_invalid``, also a value
  claim citing one page (``PAGE_CITATION``) in another field than the page's ``text``;
  none: ``claim_states_no_value``); its text is never shown. A citation counts only if a tool
  returned it to this session as evidence (``evidence_handles``); one handle the session was
  not returned makes the whole claim unsupported (``cites_what_this_run_did_not_return``;
  no citation at all: ``cites_no_evidence``).
* Claim checks: every claim's value goes through ``claim_checks.check_items`` by
  reference, never by reading prose: over the cited record as the session's actor
  re-reads it now (``citation_rows`` and ``gate_rows``, ``plx.investigate``'s reader: a
  refusal reads as no row, a revision the source moved past as changed since) with the
  counts and money currencies this session's tool results returned (``returned``,
  ``claim_checks.returned_evidence``). A value that rests only on lead-tier links
  (``returned["leads"]``: the cell it cites is one a ``plx.investigate`` answer entry
  marked ``lead_basis`` states its value from, and no other entry does) passes only stated
  as a lead (``claim_lead_unmarked``).
  A claim that fails is dropped and listed unsupported (``claim_check_failed`` with the
  gate's codes).
* Rendering: code writes what a passing claim says (``<role or field>: <value>``, a count
  no complete count binds as ``at least N``, the cell as recorded when the record writes
  it otherwise, its unit or currency, a lead as one) and the answer text
  (``rendered_text``): the prose's sentences whose every named claim passed, the names
  replaced by those values, none whose own words could state something (a digit, number
  word, negator, capitalised name or a quantity word that is not a named claim's role),
  then a line for each claim no sentence names. Nothing the gate did not check is
  rendered.
* Partly read: a passed claim citing a text this session was served only part of
  (``unread``, an MCP session's truncated ``plx.read``, the console Ask's included) adds
  one unknown naming the unread rest (``unread_unknown``: however many texts, the answer
  keeps at most ``MAX_UNKNOWNS``) and makes the answer ``partial`` (``partly_read``),
  unless the claim was read in full in what was served (``_in_full``: the served parts the
  re-read holds unchanged, the claim passing on them alone). A truncated read of another
  text never makes a claim partial.
* Final values: a stated answer value is kept only as its passed claim states it
  (``kept_value``, ``backs``): no quote, role or unit the gate did not check.
* Repair once: an answer the gate leaves with no claim is not final while the caller
  allows a repair (``may_repair``): the outcome is ``repair`` with the refused claims (their
  text, reason and gate codes, never their citations) and ``REPAIR_ANSWER``; the caller
  asks again and the gate decides the next answer the same way. The instruction adds a
  hint per refusal reason it lists (``REPAIR_HINTS``). With no repair left it is
  an abstention (``no_checked_claims`` when cited claims failed the gate or their shape,
  else ``no_cited_claims``). An answer with at least one passing claim is ``answered``: the
  passing claims are the answer, the refused ones stay visible as unsupported.

The caller keeps the repair count (the Ask loop's ``Bounds.max_repairs`` and step budget;
an MCP session's ``MAX_REPAIRS``). ``shown_unsupported`` lists unsupported claims for a
reader: never their citations; a claim citing a record withheld from the reader keeps
only its reason and codes.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from ontology.json_contract import validate
from . import claim_checks
from .claim_checks import (LEAD_MARK, ORIGINAL_TEXT_FIELDS, PAGE_TEXT,
                           SCHEMA as CLAIM_CHECKS_SCHEMA, check_items, dig, field_label,
                           parse_value_claims)
from .ontology_tools import load_contracts
from .ontology_tools_investigate import citation_rows, default_unit_contracts, gate_rows

CONTRACTS = load_contracts()
HANDLE = re.compile(CONTRACTS["$defs"]["handle"]["pattern"])
HANDLE_REF, SOURCE_TEXT_REF = "#/$defs/handle", "#/$defs/untrusted_text"
# The ``status`` values with which a contract's result object says the records it
# returns were read for this principal (plx.read ``resolved``/``changed_since``, plx.find
# ``found``/``ambiguous``). Any other, a contract's later addition included, makes
# nothing in that object evidence (fail closed).
RESOLVED_STATUSES = frozenset({"resolved", "changed_since", "found", "ambiguous"})
MAX_CLAIMS = 20
MAX_CLAIM_CHARS = 1000
MAX_CITATIONS_PER_CLAIM = 8
MAX_UNKNOWNS, MAX_UNKNOWN_CHARS = 20, 300
# Repairs an MCP session gets over all its answers (the Ask's default bound too).
MAX_REPAIRS = 1
OUTCOMES = ("answered", "repair", "abstained")
# A structured claim: one value, the L1 handle it was read from and its field (or a count
# or sum over cited records), optional role, unit, currency, and whether it is a lead.
CLAIM_KEYS = frozenset({"id", "value", "citation", "field", "role", "unit", "currency",
                        "op", "of", "lead", "quote", "expression"})
CLAIM_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
MAX_OP_CITATIONS = 100
# A value resting only on lead-tier links that its claim does not state as a lead.
LEAD_UNMARKED = "claim_lead_unmarked"
MAX_TEXT_CHARS = 2000
PLACEHOLDER = re.compile(r"\{([A-Za-z][A-Za-z0-9_]{0,31})\}")
SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+|\n+")
# A citation of one page (an original's ``#page=<n>``, a captured document's or archive
# member's ``&page=<n>``): its value is read on that page's text, field ``text``
# (``PAGE_TEXT``), as every re-read of a page (the scorer's included) gives it.
PAGE_CITATION = re.compile(r"#(?:[^#\s]*&)?page=[1-9][0-9]*(?:&|$)")

# A question about what a named document establishes or leaves unverified is answered
# from the whole text, its "holds" and "not provided" passages compared (case-card runs
# read only some windows, or read both passages and never compared them). Paging stops
# at ``DOCUMENT_READ_CHARS`` read: the rest is said unread, a gap, never paged on past
# the run's budget. ``find`` locates a passage by a phrase wherever it lies (the
# "held" and "not provided" lines far apart). The eval prompt and the console planner
# state it.
DOCUMENT_READ_CHARS = 150_000
DOCUMENT_READ_RULE = (
    "For a question about what a named document establishes or leaves unverified, page "
    "through its whole text before you state it: call plx.read again with text_offset "
    "set to the text_window.next_offset it returned, until next_offset is null or you "
    f"have read {DOCUMENT_READ_CHARS:,} characters; then stop, say which characters you "
    "read (from offset 0 to the last offset read) and state the rest as unread, a gap. "
    "To locate a passage, call plx.read with find set to a short phrase: each hit gives "
    "its line, its text_offset and a snippet you may quote. Then compare what the "
    "document says it holds or supplied with what it says is not provided or missing: "
    "where it says it holds something in one place and says that thing is not provided "
    "in another, that is a conflict.")
# A record that states the gap, link or fact asked about answers that part of the question
# (case-card run: a process map read and stating "Blank SKU: none" was paraphrased without a
# value claim, refused, and the answer abstained as blocked). Said in every repair and by
# the Ask's planner rules, so a statement a record makes is cited, never abandoned.
STATED_GAP = (
    "A record you read that states the gap, link or fact the question asks about (a "
    "document saying a link is missing, not recorded or none) answers that part: claim its "
    "statement as a text value, the passage copied as written from the text you read, and "
    "the answer is partial, never blocked.")
# Each answering sentence rests on a quote of the document text that says it (an own-
# document audit of fresh-data case-card runs: answers resting on a file's content type or
# name, one side of a change, an uncited sentence, a map's worked example given as the
# record, prices or people's names where the card forbade them). Said by the eval prompt,
# every repair and the Ask's planner rules.
ANSWER_BACKING = (
    "Each sentence that answers the question is backed by a value claim that states it; "
    "from a document, the claim quotes the text that says it (field text or "
    "row.content_text, with \"quote\"). A file's name, type, size or date answers "
    "nothing, nor does a sentence no claim states. A change states both sides, each quoted. A figure from a document's worked "
    "example (a map's or template's example) is stated as that example, never as the "
    "record's value. Where the question limits what to state (no prices, roles only), "
    "state no such value: no price, no person's name.")
REPAIR_ANSWER = (
    "Every claim of your answer was refused (refused_claims: each claim's reason and the "
    "gate's codes). Fix it: state each value as a claim of its own, the value copied "
    "exactly as the cited record holds it with that record's handle and field (what a "
    "document says is the passage copied as written from the text you read, its field "
    "that text's field: row.content_text, row.body_text, row.description or text; a "
    "number written in that text is claimed on that field with \"quote\", the passage "
    "as written that holds it), cite only "
    "handles the tools returned, mark a value that rests on a lead (lead_basis) "
    "\"lead\": true, drop what no record supports and state it as an unknown (\"not in "
    "the record: <what>\"), read more if a record is missing. " + STATED_GAP + " "
    + ANSWER_BACKING + " Abstain "
    "only if nothing the tools returned bears on the question. Then submit the fixed "
    "answer once more: the gate decides that submission as final.")
# What a repair adds for each refusal reason it lists (``refused_claims``), so a refused
# claim that a record does support is fixed instead of abandoned.
REPAIR_HINTS = {
    "claim_states_no_value": (
        "claim_states_no_value: a claim without value_claims states nothing; give what the "
        "record says as a value claim, the passage copied as written from the text you "
        "read (row.content_text or row.body_text; a page's field is text), a statement "
        "that something is missing or not recorded included; a file's name or type is "
        "no statement of it."),
    "cites_what_this_run_did_not_return": (
        "cites_what_this_run_did_not_return: a handle seen only inside a search hit (a "
        "plx.find candidate's matched or display text, a snippet) is not evidence; read "
        "that record with plx.object or plx.read and cite the handle that read returns."),
    "cites_no_evidence": (
        "cites_no_evidence: every claim cites a handle a tool returned as a record you read; "
        "that no record was found is an unknown, not a claim, unless a record you read "
        "says so (then cite its passage)."),
    "value_claims_invalid": (
        "value_claims_invalid: a value claim is {value, citation, field} (a page "
        "citation's field is text; a number written in a text adds \"quote\", the passage "
        "as written that holds it), an op count or sum over cited records, or an op "
        "expression over figures read in cited texts ({value, op: expression, expression: "
        "\"x0 - x1\", of: [{citation, field, value, quote}, ...]}, no number of its own); "
        "each detail says what to fix."),
}
# The answerability a final decision gives (``answerability``, its ``state``): ``blocked`` only when no
# claim passed; with a passed claim ``partial`` while the answer leaves an unknown or a
# refused claim, else ``answerable``.
ANSWERABILITY = ("answerable", "partial", "blocked")
# Added to a final decision with a passed claim: a passed claim is a cited answer.
ANSWER_NOT_BLOCKED = (
    "A claim passed, so your answer is not blocked: state each passed claim as its "
    "answer_values entry gives it (add only its name): an expression claim as a "
    "calculation (op expression, the same expression and inputs, each input's figure as "
    "printed and its quote, no citations), a count or sum claim as a calculation over its "
    "inputs, and only a cited claim as a cited value (a passage a document states as a "
    "text value), with no role, unit or currency the entry does not carry; give your "
    "answerability as this decision's answerability.state; it is partial while any value "
    "the question asks for is an unknown or refused, each null with a gap.")
# What a caller is told with every final decision: the answer is what was checked, and
# nothing written after the check is.
ANSWER_FINAL = (
    "This is the gate's final decision. Your answer is the claims it returned (claims) and "
    "reply.text, the text you submitted without what no passed claim backs: state "
    "reply.text as your answer text and add no number, name, date or finding after this "
    "check. State each value as its passed claim states it: that value, citation, field "
    "and quote, and no quote, role or unit the claim does not carry (one the gate did "
    "not check withholds the value).")
# Added when the final decision refused claims: a refused value is no finding, whatever
# the answer says around it.
WITHHOLD_REFUSED = (
    "Every refused claim (unsupported_claims) is not supported by a record you were "
    "served: give its value as null with a gap (state unknown, its reason) and leave it "
    "out of your text; never state it as a finding.")


# ------------------------------------------------------------------ what is evidence

def evidence_handles(tool: Any, result: Any, out: set[str] | None = None, *,
                     arguments: Any = None) -> set[str]:
    """The L1 handles ``tool``'s result, for a call with ``arguments``, returns as
    evidence: strings at the positions its output contract types ``$defs/handle``, in
    the branches the result matches; none inside an object whose ``status`` is not in
    ``RESOLVED_STATUSES``; a handle the arguments hold only where the result resolved
    it: inside an object with a resolved status or one the contract requires to carry
    the ``digest`` of what was read (an echo is not a resolution). A tool without an
    output contract returns none (fail closed)."""
    out = set() if out is None else out
    contract = CONTRACTS["tools"].get(tool) if isinstance(tool, str) else None
    if isinstance(contract, Mapping) and "output" in contract:
        _typed_handles(result, contract["output"], out, _given(arguments, set()), False)
    return out


def _given(value: Any, found: set[str]) -> set[str]:
    """Every handle among a call's arguments, at any depth."""
    if isinstance(value, str) and HANDLE.fullmatch(value):
        found.add(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            _given(item, found)
    elif isinstance(value, list):
        for item in value:
            _given(item, found)
    return found


def _typed_handles(value: Any, schema: Any, out: set[str], given: set[str],
                   resolved: bool) -> None:
    if not isinstance(schema, Mapping):
        return
    if isinstance(value, Mapping):
        if "status" in (schema.get("properties") or {}):
            status = value.get("status")
            if not isinstance(status, str) or status not in RESOLVED_STATUSES:
                return
            resolved = True
        # A value citation: a handle with the digest of what this read returned from it.
        resolved = resolved or "digest" in (schema.get("required") or ())
    ref = schema.get("$ref")
    if ref == HANDLE_REF:
        if isinstance(value, str) and HANDLE.fullmatch(value) \
                and (resolved or value not in given):
            out.add(value)
        return
    if ref == SOURCE_TEXT_REF:
        return
    if isinstance(ref, str):
        node: Any = CONTRACTS
        for part in ref.removeprefix("#/").split("/"):
            node = node.get(part) if isinstance(node, Mapping) else None
        _typed_handles(value, node, out, given, resolved)
    for sub in schema.get("allOf") or ():
        _typed_handles(value, sub, out, given, resolved)
    for key in ("oneOf", "anyOf"):
        for sub in schema.get(key) or ():
            if not validate(value, sub, root=CONTRACTS, limit=1):
                _typed_handles(value, sub, out, given, resolved)
    if "if" in schema:
        matched = not validate(value, schema["if"], root=CONTRACTS, limit=1)
        _typed_handles(value, schema.get("then" if matched else "else"), out, given,
                       resolved)
    if isinstance(value, Mapping):
        properties = schema.get("properties") or {}
        extra = schema.get("additionalProperties")
        for key, item in value.items():
            _typed_handles(item, properties.get(key, extra), out, given, resolved)
    elif isinstance(value, list):
        for item in value:
            _typed_handles(item, schema.get("items"), out, given, resolved)


# ------------------------------------------------------------------ the gate

def gate_answer(decision: Mapping[str, Any], server: Any, *, seen: set[str],
                returned: Mapping[str, Any], may_repair: bool,
                unread: Callable[[str, str], bool] | None = None) -> dict[str, Any]:
    """The gated final answer of ``decision`` (``claims``, ``text``, ``unknowns``) for
    ``server``'s session (module doc): ``{"outcome": "answered" | "repair" | "abstained",
    "reason", "claims", "dropped", "unsupported", "unknowns", "checks", "repair", "text",
    "prose"}``. ``claims``: the checked claims, each with the text code rendered for it;
    ``text``: the answer as code renders it from them (``rendered_text``), ``prose`` the
    templates it used. ``unsupported`` entries keep the strings they cited under
    ``_cited`` for ``shown_unsupported`` only; ``repair`` is the request to the answerer
    (refused claims without citations), or None. ``seen``: the handles tool results
    returned this session as evidence. ``unread(handle, field)``: whether this session
    was served only part of that text of that record (``UNREAD_PART``)."""
    claims, dropped, unsupported = _claims(decision, seen)
    raw = decision.get("unknowns") if isinstance(decision, Mapping) else None
    unknowns = [str(u)[:MAX_UNKNOWN_CHARS] for u in raw
                if isinstance(u, str)][:MAX_UNKNOWNS] if isinstance(raw, list) else []
    checks = None
    rows = _reader(server) if claims else None
    if claims:
        claims, failed, checks = _check(claims, rows, returned)
        dropped += len(failed)
        unsupported += failed
    in_full = None if rows is None else _in_full(server, rows, returned)
    partly = _partly_read(claims, unread, in_full)
    if partly:
        said = unread_unknown(partly)
        unknowns = [u for u in unknowns if u != said][:MAX_UNKNOWNS - 1] + [said]
    prose = _prose(decision.get("text") if isinstance(decision, Mapping) else None)
    out = {"outcome": "answered", "reason": None, "claims": claims, "dropped": dropped,
           "unsupported": unsupported, "unknowns": unknowns, "checks": checks,
           "repair": None, "text": rendered_text(claims, prose), "prose": prose,
           "partly_read": [claim["id"] for claim in claims
                           if _partly_read([claim], unread, in_full)]}
    if claims:
        return out
    if may_repair:
        reasons = dict.fromkeys(entry["reason"] for entry in unsupported)
        out.update(outcome="repair", reason="answer_refused", repair={
            "kind": "answer_refused",
            "refused_claims": [{k: v for k, v in entry.items() if k != "_cited"}
                               for entry in unsupported],
            "instruction": " ".join([REPAIR_ANSWER, *(REPAIR_HINTS[reason]
                                                      for reason in reasons
                                                      if reason in REPAIR_HINTS)])})
        return out
    # Cited claims that all failed the gate (or its shape) are not "uncited".
    checked = any(u["reason"] in ("claim_check_failed", "value_claims_invalid",
                                  "claim_invalid", "claim_states_no_value")
                  for u in unsupported)
    out.update(outcome="abstained",
               reason="no_checked_claims" if checked else "no_cited_claims")
    return out


def answerability(gated: Mapping[str, Any]) -> str | None:
    """The answerability of ``gate_answer``'s final decision (``ANSWERABILITY``): None for
    a repair (not final); ``blocked`` only when no claim passed; ``partial`` when a claim
    passed beside an unknown or a refused claim, or a passed claim cites a text the
    session was served only part of (``partly_read``); else ``answerable``. A passed claim
    is a cited value, so an answer with one is never blocked."""
    if gated.get("outcome") == "repair":
        return None
    if gated.get("outcome") != "answered" or not gated.get("claims"):
        return "blocked"
    return "partial" if gated.get("unknowns") or gated.get("unsupported") \
        or gated.get("partly_read") else "answerable"


# The unknown a final decision adds per text field a passed claim cites that this session
# was served only part of (a ``plx.read`` the session got truncated): the rest is unread,
# so the answer is partial. No number in it: the reply screen keeps it (``final_reply``).
UNREAD_PART = ("not read: the rest of the cited {field} past the part plx.read served (it "
               "was truncated); page it with text_offset to read what follows")


# Several such fields are one unknown (``unread_unknown``), so the answer's unknowns stay
# within ``MAX_UNKNOWNS`` and each within ``MAX_UNKNOWN_CHARS``. Its count is a number word
# up to nine, else ``more than nine``: the reply screen drops a digit, or a number word from
# ten, that no passed claim states.
UNREAD_PARTS = ("not read: the rest of {count} cited texts past the part plx.read served "
                "(each was truncated): {fields}; page each with text_offset to read what "
                "follows")
_COUNT_WORDS = ("two", "three", "four", "five", "six", "seven", "eight", "nine")


def unread_unknown(fields: Sequence[str]) -> str:
    """The one unknown naming the unread rest of the cited text ``fields`` (at least one,
    each once): ``UNREAD_PART`` for one field, else ``UNREAD_PARTS`` with how many and as
    many of their names as fit ``MAX_UNKNOWN_CHARS`` (``and others`` for the rest)."""
    if len(fields) == 1:
        said = UNREAD_PART.format(field=fields[0])
        # a field name too long to say whole is said as the cited text
        return said if len(said) <= MAX_UNKNOWN_CHARS else UNREAD_PART.format(field="text")
    count = _COUNT_WORDS[len(fields) - 2] if len(fields) <= 9 else "more than nine"
    shown: list[str] = []
    for field in fields:
        named = ", ".join([*shown, field] + (["and others"]
                                             if len(shown) + 1 < len(fields) else []))
        if len(UNREAD_PARTS.format(count=count, fields=named)) > MAX_UNKNOWN_CHARS:
            break
        shown.append(field)
    rest = ["and others"] if len(shown) < len(fields) else []
    return UNREAD_PARTS.format(count=count, fields=", ".join(shown + rest) or "and others")


def _partly_read(claims: Sequence[Mapping[str, Any]],
                 unread: Callable[[str, str], bool] | None,
                 in_full: Callable[[Mapping[str, Any]], bool] | None = None) -> list[str]:
    """The text fields, in order and once, of the passed ``claims``' value claims that
    cite a text ``unread`` says this session was served only part of, unless the claim
    was read in full in what was served (``in_full``: its value, and its quote, pass the
    same check on the served characters alone). A value claim's text is its ``field`` of
    its ``citation``, an expression's each input's (a count or sum reads rows, never a
    text). A truncated read of another text, or of the rest of this one, never makes this
    claim partial."""
    if unread is None:
        return []
    fields: list[str] = []
    for claim in claims:
        for value_claim in [read for given in claim.get("value_claims") or ()
                            for read in [given, *claim_checks.expression_inputs(given)]]:
            handle, field = value_claim.get("citation"), value_claim.get("field")
            if isinstance(handle, str) and isinstance(field, str) \
                    and field not in fields and unread(handle, field) \
                    and not (in_full is not None and in_full(value_claim)):
                fields.append(field)
    return fields


def _in_full(server: Any, rows: Callable[[str], Any], returned: Mapping[str, Any]
             ) -> Callable[[Mapping[str, Any]], bool] | None:
    """Whether a passed value claim on a partly served text was read in full: the server's
    served parts of that text (``served_parts``: start, end and the SHA-256 of the
    characters served) that the gate's own re-read holds unchanged at the same place,
    joined where they meet, each without the word at an edge inside the FULL text (a
    window ending ``Qty shipped 15`` of ``150`` serves ``Qty shipped``; one ending
    ``Shipment released`` of ``released to quarantine`` serves ``Shipment``); and the claim passing
    ``check_items`` on the full text with that served passage as its quote, so only an
    occurrence inside it counts, read in its full-text context (token, negation, unit),
    never one outside the served parts. A claim's own quote must lie inside the same
    passage; where every place the text holds that quote lies inside the passage, the
    claim is checked with its own quote (it pins the occurrence it means, a figure the
    text writes elsewhere with another meaning included). A part whose characters differ
    now, a server that keeps no parts, or a text that is not a string: not in full (the
    answer stays partial)."""
    served = getattr(server, "served_parts", None)
    if not callable(served):
        return None

    def in_full(value_claim: Mapping[str, Any]) -> bool:
        handle, field = value_claim.get("citation"), value_claim.get("field")
        try:
            parts = list(served(handle, field) or ())
        except Exception:  # noqa: BLE001 - unknown parts: not read in full
            return False
        resolved = rows(handle)
        if not parts or not isinstance(resolved, Mapping):
            return False
        text = resolved.get("text") if field == PAGE_TEXT else dig(resolved, field)[1]
        if not isinstance(text, str):
            return False
        own = value_claim.get("quote")
        quoted = claim_checks._normalised(own) if isinstance(own, str) else None
        everywhere = len(claim_checks._spans(quoted, claim_checks._normalised(text))) \
            if quoted else 0
        for start, end in _verified_ranges(text, parts):
            passage = _whole_tokens(text, start, end)
            inside = len(claim_checks._spans(quoted, claim_checks._normalised(passage))) \
                if quoted and passage is not None else 0
            if passage is None or quoted and not inside:
                continue
            # The claim's own quote pins the occurrence it means (a figure written twice
            # with two meanings): when every place the text holds that quote lies inside
            # this served passage, the claim as given passing on the full text read it in
            # full. Else the passage itself is the quote, as for a claim with none.
            pinned = quoted is not None and inside == everywhere
            report = check_items([{"id": "in_full", "texts": [],
                                   "citations": [{"handle": handle, "status": "resolved"}],
                                   "value_claims": [value_claim if pinned
                                                    else {**value_claim, "quote": passage}]}],
                                 rows=lambda cited: resolved if cited == handle else None,
                                 contracts=default_unit_contracts(), returned=returned)
            if not report["failures"] and all(
                    result.get("status") == "passed" for entry in report["items"]
                    for result in entry["claims"]):
                return True
        return False
    return in_full


def _verified_ranges(text: str, parts: Sequence[Any]) -> list[tuple[int, int]]:
    """The served ``parts`` (start, end, sha256) whose characters ``text`` holds at the
    same place, joined where they overlap or meet, in order."""
    found = []
    for part in parts:
        if not (isinstance(part, (list, tuple)) and len(part) == 3):
            continue
        start, end, sha = part
        if isinstance(start, int) and isinstance(end, int) and isinstance(sha, str) \
                and 0 <= start < end <= len(text) and hashlib.sha256(
                    text[start:end].encode("utf-8")).hexdigest() == sha:
            found.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(found):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _whole_tokens(text: str, start: int, end: int) -> str | None:
    """``text[start:end]`` without the word touching each edge that falls inside the text
    (a served edge that is not the text's start or end): that word may be cut (``15|0``,
    ``re|leased``) or go on past the edge (``released| to quarantine``), so it was not
    read in full. White space at the edges goes. None when nothing is left."""
    if start > 0:
        start = next((at for at in range(start, end) if text[at].isspace()), end)
    if end < len(text):
        end = next((at for at in range(end - 1, start - 1, -1) if text[at].isspace()),
                   start)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return text[start:end] if end > start else None


def _claims(decision: Any, seen: set[str]) -> tuple[list[dict], int, list[dict]]:
    """(the claims to check, each one value claim with the handles it cites, its id and
    whether it is stated as a lead; the dropped count; the dropped claims as unsupported:
    text, reason and, under ``_cited``, the strings they cited). A structured claim
    (``CLAIM_KEYS``) is one claim; a claim in the MCP shape (``text``, ``citations``,
    ``value_claims``) is one claim per value claim, its text never shown."""
    kept: list[dict] = []
    dropped, unsupported = 0, []
    claims = decision.get("claims") if isinstance(decision, Mapping) else None
    claims = claims if isinstance(claims, list) else []
    given = {claim.get("id") for claim in claims[:MAX_CLAIMS]
             if isinstance(claim, Mapping) and isinstance(claim.get("id"), str)}
    ids: set[str] = set()

    def fresh(prefix: str) -> str:
        """The first ``<prefix><n>`` no claim of this answer names."""
        n = 1
        while f"{prefix}{n}" in given or f"{prefix}{n}" in ids:
            n += 1
        ids.add(f"{prefix}{n}")
        return f"{prefix}{n}"

    def refuse(text: str, reason: str, cited: list[str], **more: Any) -> None:
        nonlocal dropped
        dropped += 1
        unsupported.append({"text": text, "reason": reason, **more, "_cited": cited})

    for claim in claims[:MAX_CLAIMS]:
        if not isinstance(claim, Mapping):
            dropped += 1
            continue
        if "text" in claim:
            # Not ``dropped += _legacy(...)``: that reads ``dropped`` before ``refuse``
            # counts a refusal and loses it.
            textless = _legacy(claim, seen, kept, refuse, fresh)
            dropped += textless
            continue
        cid = claim.get("id")
        if cid is None:   # unnamed: the first free c<n>
            cid = fresh("c")
            ids.discard(cid)
        cited = _strings(claim)
        label = f"{cid if isinstance(cid, str) else '?'}: {_shown_value(claim.get('value'))}"
        unknown = sorted(str(key)[:40] for key in claim if key not in CLAIM_KEYS)
        problem = (f"unknown key {unknown[0]}" if unknown
                   else "id is not a short name" if not isinstance(cid, str)
                   or not CLAIM_ID.fullmatch(cid)
                   else "id given twice" if cid in ids
                   else "lead is true or false" if not isinstance(claim.get("lead", False),
                                                                  bool) else None)
        if problem is not None:
            # Fail closed: a key the gate does not know may carry what it cannot check.
            refuse(label, "claim_invalid", cited, detail=problem)
            continue
        ids.add(cid)
        handles = _cited(claim)
        if not handles or any(handle not in seen for handle in handles):
            # Every citation must be evidence this session returned: one invented handle
            # makes the claim unsupported.
            refuse(label, "cites_no_evidence" if not handles
                   else "cites_what_this_run_did_not_return", cited)
            continue
        if "value" not in claim:
            refuse(label, "claim_states_no_value", cited)
            continue
        value_claim = {key: claim[key] for key in claim if key not in ("id", "lead")}
        try:
            parsed = parse_value_claims([value_claim], handles, [])
        except ValueError as error:
            refuse(label, "value_claims_invalid", cited, detail=invalid_detail(error))
            continue
        problem = _page_field_problem(parsed)
        if problem is not None:
            refuse(label, "value_claims_invalid", cited, detail=problem)
            continue
        kept.append({"id": cid, "citations": handles, "value_claims": parsed,
                     "lead": claim.get("lead") is True})
    if len(claims) > MAX_CLAIMS:
        dropped += len(claims) - MAX_CLAIMS
    return kept, dropped, unsupported


def _legacy(claim: Mapping[str, Any], seen: set[str], kept: list[dict],
            refuse: Callable[..., None], fresh: Callable[[str], str]) -> int:
    """A claim in the MCP shape: its value claims as claims of their own (ids
    ``m<n>``, citing what each value claim cites; refused, listed with the claim's text),
    or refused whole as before. The dropped count it adds (a claim with no text)."""
    text, cites = claim.get("text"), claim.get("citations")
    if not isinstance(text, str) or not text.strip():
        return 1
    text = text.strip()[:MAX_CLAIM_CHARS]
    if (isinstance(cites, list) and not cites) or cites is None:
        # No citations of its own: the handles its value claims cite (a value claim's
        # citation, an op's inputs, an inline calculation's inputs), never another.
        cites = _value_claim_handles(claim.get("value_claims"))
    cited = cites[:MAX_CITATIONS_PER_CLAIM] if isinstance(cites, list) else []
    strings = [c for c in cites if isinstance(c, str)] if isinstance(cites, list) else []
    if not cited or any(not isinstance(c, str) or c not in seen for c in cited):
        refuse(text, "cites_no_evidence" if not cited
               else "cites_what_this_run_did_not_return", strings)
        return 0
    raw = claim.get("value_claims")
    try:
        # Null (a model's "none") is no value claim; any other non-list is malformed.
        value_claims = parse_value_claims([] if raw is None else raw, cited, [])
    except ValueError as error:
        refuse(text, "value_claims_invalid", strings, detail=invalid_detail(error))
        return 0
    if not value_claims:
        # Its text is never shown: a claim states what it says as value claims.
        refuse(text, "claim_states_no_value", strings)
        return 0
    problem = _page_field_problem(value_claims)
    if problem is not None:
        refuse(text, "value_claims_invalid", strings, detail=problem)
        return 0
    for value_claim in value_claims:
        kept.append({"id": fresh("m"), "lead": claim.get("lead") is True, "said": text,
                     "citations": list(dict.fromkeys(
                         [value_claim["citation"]] if "citation" in value_claim
                         else [given["citation"] for given in value_claim["of"]])),
                     "value_claims": [value_claim]})
    return 0


# What a repair is told for each shape code of ``claim_checks.parse_value_claims`` it can
# act on (PS-04: ``invalid_value_claim_value:0`` named no fix for a 300-character value).
VALUE_CLAIM_FIXES = {
    "value_claim_value_too_long": (
        f"the value is longer than {claim_checks.MAX_VALUE_CHARS} characters: claim the "
        "shortest passage that states the fact as the value (its own words copied as "
        "written), give a longer passage that holds it as its quote, or split it into "
        "claims of their own"),
    "value_claim_expression_literal": (
        "the expression holds a number of its own: every operand is an input x0, x1, ... "
        "read from a cited text, each named once"),
    "value_claim_expression_name": (
        "the expression names something other than its inputs: name them x0, x1, ... in "
        "the order of \"of\""),
    "value_claim_expression_repeated_input": (
        "the expression names an input more than once: name each input once; a figure the "
        "text prints twice is two inputs, each quoted where it is printed"),
    "value_claim_expression_unused_input": (
        "the expression leaves an input of \"of\" unused: use each one, or drop it"),
    "value_claim_expression_input_value": (
        "an expression input's value is the figure as written in its cited text: digits, "
        "with an optional thousands comma and fraction, and % for a printed percent"),
    "invalid_value_claim_expression": (
        "an expression claim is {\"value\", \"op\": \"expression\", \"expression\": "
        "\"x0 - x1\", \"of\": [{\"citation\", \"field\", \"value\", \"quote\"}, ...]}"),
}


def invalid_detail(error: Exception) -> str:
    """A ``value_claims_invalid`` refusal's detail: the shape code, and how to fix it
    where ``VALUE_CLAIM_FIXES`` says."""
    code = str(error)[:200]
    fix = VALUE_CLAIM_FIXES.get(code.split(":", 1)[0])
    return f"{code}: {fix}" if fix else code


def _value_claim_handles(raw: Any) -> list[Any]:
    """The handles an MCP claim's value claims cite, in order and once: each value
    claim's ``citation``, its ``of`` inputs' ``citation`` and its ``calculation``'s
    ``inputs`` (``l1_handle`` or ``citation``). A non-string among them is kept, so the
    claim is refused as citing what this session did not return."""
    found: list[Any] = []
    for value_claim in raw if isinstance(raw, list) else ():
        if not isinstance(value_claim, Mapping):
            continue
        if "citation" in value_claim:
            found.append(value_claim["citation"])
        calculation = value_claim.get("calculation")
        inputs = [*(value_claim.get("of") or () if isinstance(value_claim.get("of"), list)
                    else ()),
                  *(calculation.get("inputs") or () if isinstance(calculation, Mapping)
                    and isinstance(calculation.get("inputs"), list) else ())]
        for given in inputs:
            if isinstance(given, Mapping):
                found.append(given.get("l1_handle", given.get("citation")))
    out: list[Any] = []
    for handle in found:
        if handle not in out:
            out.append(handle)
    return out


def _page_field_problem(value_claims: Sequence[Mapping[str, Any]]) -> str | None:
    """Why a value claim citing one page names another field than the page's text
    (``PAGE_CITATION``), or None. A row field of the document the page belongs to
    (``row.content_text``) is not what that page holds: the claim is refused so its
    repair cites the page's ``text``, the field every re-read of the page checks."""
    for claim in [read for given in value_claims
                  for read in [given, *claim_checks.expression_inputs(given)]]:
        citation = claim.get("citation")
        if isinstance(citation, str) and PAGE_CITATION.search(citation) \
                and claim.get("field") != PAGE_TEXT:
            return (f"a page citation (#page=<n> or &page=<n>) states its value in field "
                    f"\"{PAGE_TEXT}\", the page's text, never {str(claim.get('field'))[:60]}")
    return None


def _cited(claim: Mapping[str, Any]) -> list[str]:
    """The handles a structured claim cites (its ``citation``, or each of its ``of``), in
    order and once; any that is not a string makes none."""
    of = claim.get("of")
    raw = [claim.get("citation")] if "citation" in claim else \
        [given.get("citation") if isinstance(given, Mapping) else None
         for given in of] if isinstance(of, list) else []
    if any(not isinstance(handle, str) for handle in raw):
        return []
    return list(dict.fromkeys(raw))[:MAX_OP_CITATIONS]


def _strings(claim: Mapping[str, Any]) -> list[str]:
    of = claim.get("of")
    raw = [claim.get("citation")] + ([given.get("citation") for given in of
                                      if isinstance(given, Mapping)]
                                     if isinstance(of, list) else [])
    return [handle for handle in raw if isinstance(handle, str)]


def _shown_value(value: Any) -> str:
    return str(value)[:200] if isinstance(value, (str, int)) \
        and not isinstance(value, bool) else "?"


def _reader(server: Any) -> Callable[[str], Any]:
    """The cited records as ``server``'s actor re-reads them now (``gate_rows`` over
    ``citation_rows``), each read once per decision."""
    read = gate_rows(citation_rows(server.case_runtime), server.actor)
    cache: dict[str, Any] = {}

    def rows(handle: str) -> Any:
        if handle not in cache:
            cache[handle] = read(handle)
        return cache[handle]
    return rows


def _check(claims: list[dict], rows: Callable[[str], Any], returned: Mapping[str, Any]
           ) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """(the claims that pass, with their rendered text, the failed ones as unsupported
    with the gate's codes, the gate's summary): each claim's value claim through
    ``check_items`` over the cited records as ``server``'s actor re-reads them now, with no
    prose (what a claim says is its value claim), then a value resting only on lead-tier
    links (``returned["leads"]``) that the claim does not state as a lead fails
    (``claim_lead_unmarked``)."""
    report = check_items([{"id": claim["id"], "texts": [],
                           "citations": [{"handle": handle, "status": "resolved"}
                                         for handle in claim["citations"]],
                           "value_claims": claim["value_claims"]} for claim in claims],
                         rows=rows, contracts=default_unit_contracts(), returned=returned)
    failures: dict[str, list[dict[str, Any]]] = {}
    for failure in report["failures"]:
        failures.setdefault(failure["item"], []).append(
            {key: failure[key] for key in ("code", "value", "detail")})
    results = {entry["item"]: entry["claims"][0] for entry in report["items"]
               if entry["claims"]}
    leads = returned.get("leads") if isinstance(returned.get("leads"), Mapping) else {}
    counts = dict(report["counts"])
    passed, failed = [], []
    for claim in claims:
        value_claim, result = claim["value_claims"][0], results.get(claim["id"]) or {}
        problems = failures.get(claim["id"])
        if problems is None and not result:
            problems = [{"code": "claim_uncited", "value": value_claim["value"],
                         "detail": "the claim was not checked"}]
        if problems is None and not claim["lead"] and _on_a_lead(value_claim, claim, leads):
            problems = [{"code": LEAD_UNMARKED, "value": value_claim["value"],
                         "detail": "the value rests only on lead-tier links (lead_basis): "
                                   "state it as a lead"}]
            counts.update(passed=counts["passed"] - 1, failed=counts["failed"] + 1)
            if result.get("role_basis") in counts:
                counts[result["role_basis"]] -= 1
        if problems is not None:
            failed.append({"text": claim.get("said") or f"{claim['id']}: {value_claim['value']}",
                           "reason": "claim_check_failed", "failures": problems,
                           "_cited": claim["citations"]})
            continue
        phrase = _phrase(value_claim, result, _recorded(value_claim, rows), claim["lead"])
        passed.append({"id": claim["id"], "text": f"{field_label(value_claim)}: {phrase}",
                       "phrase": phrase, "citations": claim["citations"],
                       "value_claims": [_as_written(given, rows)
                                        for given in claim["value_claims"]],
                       "lead": claim["lead"],
                       "source": _source(claim["citations"][0]),
                       "check": {"status": "passed", "failures": [],
                                 **({"unit_basis": result.get("unit_basis")}
                                    if "unit" in value_claim else {})}})
    return passed, failed, {"schema": CLAIM_CHECKS_SCHEMA, "counts": counts,
                            "passed": report["passed"] and len(passed) == len(claims)}


def _as_written(value_claim: Mapping[str, Any], rows: Callable[[str], Any]
                ) -> dict[str, Any]:
    """A passed value claim with each quote (its own, an expression input's) as the cited
    text writes the passage it matched (``claim_checks.verbatim_quote``), so the final reply
    states the page's words, never the agent's respacing of them."""
    def written(given: Mapping[str, Any]) -> dict[str, Any]:
        quote, handle = given.get("quote"), given.get("citation")
        if not isinstance(quote, str) or not isinstance(handle, str):
            return dict(given)
        resolved = rows(handle)
        text = None if not isinstance(resolved, Mapping) else resolved.get("text") \
            if given.get("field") == PAGE_TEXT else dig(resolved, str(given.get("field")))[1]
        span = claim_checks.verbatim_quote(quote, text) if isinstance(text, str) else None
        return {**given, "quote": span} if span else dict(given)
    out = written(value_claim)
    if isinstance(value_claim.get("of"), list) and value_claim.get("op") == "expression":
        out["of"] = [written(given) for given in value_claim["of"]]
    return out


TEXT_INSTRUCTION = (
    "Your text states numbers or ISO dates (uncovered) that no passing claim states. A "
    "number in prose is a claim: state each as a cited value claim (an identifier whose "
    "digits stand alone after capitals, such as ABC-012, as a text value of the field "
    "that holds it) and submit again, or leave it out of the text.")
UNSTATED_INSTRUCTION = (
    "Your text writes a passing claim's value negated (uncovered, claim_value_unstated): "
    "it denies what the claim states. Say the value as the claim states it.")
CURRENCY_INSTRUCTION = (
    "Your text writes a passing claim's value beside a currency other than the one the "
    "claim checked (uncovered, claim_currency_mismatch): write the claim's currency, or "
    "none.")
UNCHECKED_INSTRUCTION = (
    "Some words of your text (unchecked) state something no claim of their sentence is "
    "about (a state such as late or received, a quantity word beside another role): add "
    "the claim that states it, or leave the word out.")


def check_text(text: Any, claims: list[dict], server: Any,
               returned: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the final answer's prose ``text`` states only what the passing ``claims``
    (``gate_answer``'s, already checked) state, by the two checks the real-agent eval's
    scorer runs on the answer text: ``claim_checks.check_items``'s close-outcome check
    (every number, number word and ISO date covered by a claim's value) and the one prose
    screen (``claim_checks.screen_prose`` over the passing value claims; the answer mode
    takes no declared gaps, so a gap's state said in prose is unchecked). ``{"passed", "uncovered", "unchecked", "instruction"}``, entries
    ``{"value", "code", "detail"}``: ``uncovered`` fails the text (an uncovered number, a
    value written negated); ``unchecked`` lists ``claim_prose_unchecked`` words,
    unverified, never failing (as the scorer counts them). No claim passing leaves every
    number uncovered. Bounded to ``MAX_TEXT_CHARS``; reads only cited records."""
    prose = text[:MAX_TEXT_CHARS] if isinstance(text, str) else ""
    rows = _reader(server)
    report = check_items([{"id": claim["id"], "texts": [],
                           "citations": [{"handle": handle, "status": "resolved"}
                                         for handle in claim["citations"]],
                           "value_claims": claim["value_claims"]} for claim in claims],
                         rows=rows, contracts=default_unit_contracts(), returned=returned,
                         outcome=prose or None)
    failures = [failure for failure in report["failures"] if failure["item"] == "outcome"]
    failures += claim_checks.screen_prose(
        prose, [value for claim in claims for value in claim["value_claims"]])
    shown = [{key: failure.get(key) for key in ("code", "value", "detail")}
             for failure in failures]
    uncovered = [entry for entry in shown if entry["code"] != claim_checks.PROSE_UNCHECKED]
    unchecked = [entry for entry in shown if entry["code"] == claim_checks.PROSE_UNCHECKED]
    said = [line for line, needed in (
        (TEXT_INSTRUCTION, any(e["code"] not in ("claim_value_unstated",
                                                  "claim_currency_mismatch")
                               for e in uncovered)),
        (UNSTATED_INSTRUCTION, any(e["code"] == "claim_value_unstated" for e in uncovered)),
        (CURRENCY_INSTRUCTION, any(e["code"] == "claim_currency_mismatch" for e in uncovered)),
        (UNCHECKED_INSTRUCTION, bool(unchecked))) if needed]
    return {"passed": not uncovered, "uncovered": uncovered, "unchecked": unchecked,
            "instruction": " ".join(said) or None}


# A word that names a record, field or state rather than joining prose: a status in
# capitals (REJECTED, EXPIRED) or a snake_case column or table name (app_quote,
# rejected_at). Acronyms of two or three capitals (PO, BC, SKU) are plain prose.
STATEMENT_WORD = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Z]{4,}|[a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![A-Za-z0-9_])")
MAX_REPLY_CHARS = 20_000


def unbacked(text: Any, values: Sequence[Any], *, question: Any = "",
             refused: Sequence[Any] = (), percents: Sequence[Any] = ()) -> list[str]:
    """The factual statements of a final reply's ``text`` that no passed claim's value
    (``values``) backs, as written, in order and once, by the gate's own tokenisers:
    every number (``claim_checks.prose_numbers``: handles, identifiers and dates apart)
    whose value no number of ``values`` states, the question's own numbers included (the
    gate's text check counts them); a number printed as a percent only by a value of
    ``percents`` (passed claims in a percent unit), at percent scale: ``100%`` by 100
    percent, never by a unit-less 1 (the gate's percent contract); every label
    (``claim_checks.labels``: ``Q3``, ``P-01234``) as one token, never a figure: backed
    when the ``question`` names it (on token boundaries) or when ``values`` state each
    number its digits hold (``claim_checks.label_numbers``), else written whole; every
    date (``claim_checks.written_dates``) no value names the day of; every identifier (``claim_checks.IDENTIFIER``) that no value holds
    and the ``question`` does not say; and every record word (``STATEMENT_WORD``) of a
    claim the gate refused (``refused``: its text and values) that no value holds and the
    question does not say, so a refused value reworded is still found. A text value of
    ``values`` covers what it holds. Plain prose, other words and the question restated
    are not statements. Fail closed: a text that is not a string, or longer than
    ``MAX_REPLY_CHARS``, is one unbacked statement."""
    if not isinstance(text, str) or len(text) > MAX_REPLY_CHARS:
        return ["<reply unreadable>"]
    asked = question.lower() if isinstance(question, str) else ""
    shown = [" ".join(str(value).split()) for value in values
             if isinstance(value, (str, int, float)) and not isinstance(value, bool)]
    numbers = {claim_checks.to_decimal(value) for value in shown} - {None}
    covered = [value for value in shown if claim_checks.to_decimal(value) is None and value]
    days = {moment[0] for value in covered if (moment := claim_checks._moment(value))}
    held = " ".join(covered).lower()
    words = {match.group(0).lower() for said in refused if isinstance(said, str)
             for match in STATEMENT_WORD.finditer(said)}
    out: list[str] = []

    def add(token: str) -> None:
        if token not in out:
            out.append(token)

    percent_values = {claim_checks.to_decimal(value) for value in percents
                      if isinstance(value, (str, int)) and not isinstance(value, bool)} - {None}

    def backed_number(written: str, number: Decimal) -> bool:
        # The gate's percent contract (``claim_checks._uncovered``): a printed percent is
        # backed only by a claim in a percent unit, at percent scale (``100%`` by 100
        # percent; never by a unit-less 1 or 100).
        if claim_checks._percent_written(written):
            return abs(number) * 100 in percent_values
        return number in numbers or abs(number) in numbers

    prose = " ".join(text.split())
    scan = prose
    for value in sorted({value for value in covered if value}, key=len, reverse=True):
        scan = scan.replace(value, " ")
    scan = claim_checks.HANDLE.sub(lambda match: " " * len(match.group(0)), scan)
    found = claim_checks.labels(scan)
    for match in found:
        label = match.group(0)
        if not re.search(r"(?<![A-Za-z0-9_])" + re.escape(label.lower())
                         + r"(?![A-Za-z0-9_])", asked) and not all(
                number in numbers for number in claim_checks.label_numbers(label)):
            add(label)
    for match in reversed(found):   # each label judged whole above, never its digits
        scan = scan[:match.start()] + " " * len(match.group(0)) + scan[match.end():]
    for written, number in claim_checks.prose_numbers(scan):
        if not backed_number(written, number):
            add(written)
    rest = claim_checks.HANDLE.sub(" ", prose)
    for value in sorted(set(covered), key=len, reverse=True):
        rest = rest.replace(value, " ")
    for written, moment, _iso in claim_checks.written_dates(rest):
        if moment is None or moment[0] not in days:
            add(written)
    for match in claim_checks.IDENTIFIER.finditer(rest):
        if match.group(0).lower() not in held and match.group(0).lower() not in asked:
            add(match.group(0))
    for match in STATEMENT_WORD.finditer(rest):
        token = match.group(0).lower()
        if token in words and token not in held and token not in asked:
            add(match.group(0))
    return out


# ------------------------------------------------------------------ the final reply

# One sentence, clause list item or line of a reply, with the break after it kept.
REPLY_BREAK = re.compile(r"((?<=[.!?;])[ \t]+|[ \t]*\n+[ \t]*)")
WITHHELD_WHY = "refused or not checked by the answer gate"
WITHHELD_STATEMENTS = "Withheld: statements the answer gate did not pass."
MAX_SHOWN_UNBACKED = 20


def plain(value: Any) -> Any:
    """A gate result's value without the output boundary's ``untrusted_text`` envelope
    (an MCP client's copy of a decision); any other value as it is."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        return value["untrusted_text"]
    return value


def backing(claims: Sequence[Any]) -> list[Any]:
    """What passed claims state, as a reply may write it: each value claim's value and the
    phrase and line code rendered for the claim (``gate_answer``'s ``claims``, plain or as
    an MCP client received them)."""
    out: list[Any] = []
    for claim in claims:
        if not isinstance(claim, Mapping) or (claim.get("check") or {}).get("status",
                                                                         "passed") != "passed":
            continue
        out += [plain(value_claim.get("value")) for value_claim in claim.get("value_claims")
                or () if isinstance(value_claim, Mapping)]
        out += [plain(claim.get(key)) for key in ("phrase", "text")]
    return [value for value in out if isinstance(value, (str, int)) and not isinstance(
        value, bool) and value != ""]


def in_percent(claims: Sequence[Any]) -> list[Any]:
    """The values of passed claims' value claims in a percent unit (``%``, ``percent``,
    ``pct``: ``claim_checks._page_unit``) the gate verified (``unit_basis``, as
    ``_checked_marks`` reads it), for ``unbacked``'s ``percents``."""
    out: list[Any] = []
    for claim in claims:
        check = claim.get("check") if isinstance(claim, Mapping) else None
        check = check if isinstance(check, Mapping) else {}
        if not isinstance(claim, Mapping) or check.get("status", "passed") != "passed" \
                or plain(check.get("unit_basis")) in (None, "unverified"):
            continue
        for value_claim in claim.get("value_claims") or ():
            unit = plain(value_claim.get("unit")) if isinstance(value_claim, Mapping) else None
            value = plain(value_claim.get("value")) if isinstance(value_claim, Mapping) else None
            if isinstance(unit, str) and claim_checks._page_unit(unit) == "percent" and \
                    isinstance(value, (str, int)) and not isinstance(value, bool):
                out.append(value)
    return out


def refused_said(unsupported: Sequence[Any]) -> list[str]:
    """What refused claims said (their text and failing values), for ``unbacked``."""
    out: list[Any] = []
    for entry in unsupported:
        if isinstance(entry, Mapping):
            out.append(plain(entry.get("text")))
            out += [plain(failure.get("value")) for failure in entry.get("failures") or ()
                    if isinstance(failure, Mapping)]
    return [said for said in out if isinstance(said, str)]


def final_reply(text: Any, claims: Sequence[Any], *, unsupported: Sequence[Any] = (),
                unknowns: Sequence[Any] = (), question: Any = "",
                refused: Sequence[str] = (), withheld: Sequence[str] = ()) -> dict[str, Any]:
    """The final reply as it may leave every answer path (the Ask, an MCP session's
    answer mode, the real-agent eval): only what the gate passed (``claims``, the passed
    claims of ``gate_answer``), never a refused claim in other words.

    Each sentence, list item or line of ``text`` that carries an unbacked statement
    (``unbacked`` over what the claims state, the question and what ``unsupported`` and
    ``refused`` said), or that writes a passed value beside a currency other than the one
    the gate checked with it (``_currencies_as_checked``), is dropped; the rest stays as
    written, without a unit or currency beside a passed value that the gate did not check
    for it (``_unchecked_units_dropped``). A reply whose surviving text
    states no passed claim gets the claims' lines; one with nothing surviving is the claims'
    lines alone. Then the withheld list: each name in ``withheld`` (values the caller held
    back, ``WITHHELD_WHY``) and ``WITHHELD_STATEMENTS`` when a sentence was dropped. The
    ``unknowns`` are screened the same way, each one whole. ``{"text", "unknowns",
    "modified", "replaced", "sentences", "removed_sentences", "removed_unknowns",
    "unbacked"}``. Deterministic; reads nothing. Fail closed: a text that is not a string
    or is too long (``MAX_REPLY_CHARS``) keeps nothing."""
    backed = backing(claims)
    percents = in_percent(claims)
    said = refused_said(unsupported) + [s for s in refused if isinstance(s, str)]
    lines = [str(plain(claim.get("text"))) for claim in claims if isinstance(claim, Mapping)
             and isinstance(plain(claim.get("text")), str)]
    marks = _checked_marks(claims)
    found: list[str] = []
    sentences = removed = dropped_units = 0
    kept: list[str] = []
    for sentence, gap in _sentences(text if isinstance(text, str)
                                    and len(text) <= MAX_REPLY_CHARS else ""):
        if not sentence.strip():
            kept.append(sentence + gap)
            continue
        sentences += 1
        tokens = unbacked(sentence, backed, question=question, refused=said,
                          percents=percents)
        bare, other = _currencies_as_checked(sentence, marks)
        tokens += [token for token in other if token not in tokens]
        if tokens:
            removed += 1
            found += [token for token in tokens if token not in found]
            # A dropped sentence keeps the line break after it, never its spaces.
            kept.append("\n" if "\n" in gap and "".join(kept).strip()
                        and not "".join(kept).endswith("\n") else "")
            continue
        bare = _unchecked_units_dropped(bare, marks)
        dropped_units += bare != sentence
        kept.append(bare + gap)
    if not isinstance(text, str) or len(text) > MAX_REPLY_CHARS:
        removed, found = max(sentences, 1), ["<reply unreadable>"]
    body = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", "".join(kept))).strip()
    states = body and (any(_states_value(body, value) for value in backed)
                       or any(_states_value(body, value, percent=True) for value in percents))
    replaced = bool(removed) and not body
    out_lines = [body] if body else []
    if (removed or withheld) and lines and not states:
        out_lines += lines
    out_lines += [f"Withheld: {name} ({WITHHELD_WHY})" for name in withheld]
    if removed:
        out_lines.append(WITHHELD_STATEMENTS)
    shown_unknowns, dropped = [], 0
    for unknown in unknowns:
        if isinstance(unknown, str) and not unbacked(unknown, backed, question=question,
                                                     refused=said, percents=percents):
            shown_unknowns.append(unknown)
        else:
            dropped += 1
    modified = bool(removed or withheld or dropped or dropped_units)
    return {"text": "\n".join(out_lines) if modified else text,
            "unknowns": shown_unknowns, "modified": modified, "replaced": replaced,
            "sentences": sentences, "removed_sentences": removed,
            "removed_unknowns": dropped, "unbacked": found[:MAX_SHOWN_UNBACKED]}


def _checked_marks(claims: Sequence[Any]) -> dict[Any, dict[str, set[str]]]:
    """By each value of a passed claim: the units the gate verified for it (``units``,
    ``claim_checks``' page units: "kilograms" is kg), the unit spellings a claim of it
    stated that the gate left unverified (``dropped``, case-folded: the value is kept
    without them), and the currencies the gate checked with it (``currencies``). A value
    it left without a verified unit or a checked currency has none."""
    out: dict[Any, dict[str, set[str]]] = {}
    for claim in claims:
        check = claim.get("check") if isinstance(claim, Mapping) else None
        check = check if isinstance(check, Mapping) else {}
        if check.get("status", "passed") != "passed":
            continue
        verified = plain(check.get("unit_basis")) not in (None, "unverified")
        for value_claim in claim.get("value_claims") or ():
            if not isinstance(value_claim, Mapping):
                continue
            number = claim_checks.to_decimal(plain(value_claim.get("value")))
            if number is None:
                continue
            marks = out.setdefault(number, {"units": set(), "dropped": set(),
                                            "currencies": set()})
            unit, currency = plain(value_claim.get("unit")), plain(value_claim.get("currency"))
            if isinstance(unit, str) and unit.strip():
                if verified:
                    marks["units"].add(claim_checks._page_unit(unit))
                else:
                    marks["dropped"].add(" ".join(unit.casefold().split()))
            if isinstance(currency, str) and currency.strip():
                marks["currencies"].add(currency.strip().upper())
    return out


def _unchecked_units_dropped(sentence: str, marks: Mapping[Any, Mapping[str, set[str]]]
                             ) -> str:
    """``sentence`` without each unit word right after a passed claim's figure
    (``claim_checks.unit_words``) that the gate did not verify for it: "68,160 kg" of a
    claim whose unit the page never printed is "68,160". The unit spelling a claim of
    that value stated and the gate left unverified goes too, whatever the word (``120
    ctns`` of a claim in ctns, for that value only). No unit is ever added."""
    known = {(start, end) for _, start, end, _ in claim_checks.unit_words(sentence)}
    for value, start, end, word in reversed(claim_checks.figure_words(sentence)):
        if value not in marks:
            continue
        unit = claim_checks._page_unit(word)
        if (start, end) in known and unit not in marks[value]["units"] or \
                word in marks[value]["dropped"] and unit not in marks[value]["units"]:
            sentence = sentence[:start] + sentence[end:]
    return sentence


def _currencies_as_checked(sentence: str, marks: Mapping[Any, Mapping[str, set[str]]]
                           ) -> tuple[str, list[str]]:
    """``sentence`` without each currency written beside a passed claim's figure that the
    gate checked no currency for (the value kept, as an unverified unit is), and the
    currencies written beside one that contradict the currency it checked (``EUR``
    beside a value checked in USD; ``claim_checks.same_currency``): a sentence with one
    states what no claim checked."""
    other: list[str] = []
    for value, start, end, written in reversed(claim_checks.currency_words(sentence)):
        if value not in marks:
            continue
        checked = marks[value]["currencies"]
        if not checked:
            sentence = sentence[:start] + sentence[end:]
        elif not any(claim_checks.same_currency(written, code) for code in checked):
            other.insert(0, sentence[start:end].strip())
    return sentence, other


ORDINAL = re.compile(r"\s*(?:[-*]\s*)?(?:\d{1,3}|[A-Za-z])[.)]")


def _sentences(text: str) -> list[tuple[str, str]]:
    """(sentence, the break after it) of ``text`` (``REPLY_BREAK``); a list ordinal
    ("1.", "b)") stays with the item it numbers, so dropping one drops both."""
    pieces = REPLY_BREAK.split(text)
    out: list[tuple[str, str]] = []
    carry = ""
    for index in range(0, len(pieces), 2):
        sentence = carry + pieces[index]
        gap = pieces[index + 1] if index + 1 < len(pieces) else ""
        if ORDINAL.fullmatch(sentence) and gap and "\n" not in gap and index + 2 < len(pieces):
            carry = sentence + gap
            continue
        carry = ""
        out.append((sentence, gap))
    return out


def _states_value(text: str, value: Any, *, percent: bool = False) -> bool:
    """Whether ``text`` writes ``value`` (its words, or its number; a value in a percent
    unit, ``percent``, also as a printed percent: ``100%`` of 100 percent)."""
    number = claim_checks.to_decimal(value)
    if number is not None:
        return any(found == number or percent and claim_checks._percent_written(written)
                   and found * 100 == number
                   for written, found in claim_checks.prose_numbers(text))
    return " ".join(str(value).split()).lower() in " ".join(text.split()).lower()


def passed_cells(decision: Any) -> list[dict[str, Any]]:
    """The value claims a final gate decision passed, ``{"value", "cells", "claim", "op",
    "expression", "inputs", "quote", "role", "currency", "unit", "calculation",
    "input_quotes"}``: the (handle, field or None) each reads, its claim's id, an op
    claim's ``op`` and ``inputs`` as an answer calculation states them
    (``_input_as_checked``: an expression input with its figure as printed; a cited
    claim: None), an expression claim's ``expression`` text (``calculation``: its
    ``_expression_key``, and ``input_quotes`` the quote each of its inputs was checked
    with; else None), and the quote,
    role, currency and unit the gate checked with it (None where the claim gave none; a unit only when the gate
    verified it, never one it recorded ``unverified``); none of a decision that is not
    ``answered`` (a repair never resubmitted, an abstention) or that cannot be read."""
    if not isinstance(decision, Mapping) or decision.get("outcome") != "answered":
        return []
    out = []
    for claim in decision.get("claims") or ():
        check = claim.get("check") if isinstance(claim, Mapping) else None
        if not isinstance(check, Mapping) or check.get("status") != "passed":
            continue
        for value_claim in claim.get("value_claims") or ():
            if not isinstance(value_claim, Mapping):
                continue
            value = plain(value_claim.get("value"))
            if "citation" in value_claim:
                cells = {(plain(value_claim["citation"]), plain(value_claim.get("field")))}
            else:
                cells = {(plain(given.get("citation")), plain(given.get("field")))
                         for given in value_claim.get("of") or () if isinstance(given, Mapping)}
            unit = plain(value_claim.get("unit"))
            verified = plain(check.get("unit_basis")) not in (None, "unverified")
            op = plain(value_claim.get("op"))
            if isinstance(value, (str, int)) and not isinstance(value, bool) and cells:
                out.append({"value": value, "cells": cells, "claim": plain(claim.get("id")),
                            "op": op if isinstance(op, str) else None,
                            "expression": plain(value_claim.get("expression"))
                            if op == "expression" else None,
                            # each input as the gate checked it, its figure as printed
                            "inputs": [_input_as_checked(given, op == "expression")
                                       for given in value_claim.get("of") or ()
                                       if isinstance(given, Mapping)]
                            if "citation" not in value_claim else None,
                            "quote": _verbatim(plain(value_claim.get("quote"))),
                            "role": _text_or_none(plain(value_claim.get("role"))),
                            "currency": _text_or_none(plain(value_claim.get("currency"))),
                            "unit": _text_or_none(unit) if verified else None,
                            "calculation": _expression_key(
                                plain(value_claim.get("expression")),
                                [(plain(given.get("citation")), plain(given.get("field")),
                                  plain(given.get("value")), plain(given.get("unit")))
                                 for given in value_claim.get("of") or ()
                                 if isinstance(given, Mapping)])
                            if plain(value_claim.get("op")) == "expression" else None,
                            # each expression input's quote as the gate checked it
                            "input_quotes": [_verbatim(plain(given.get("quote")))
                                             for given in value_claim.get("of") or ()
                                             if isinstance(given, Mapping)]
                            if plain(value_claim.get("op")) == "expression" else None})
    return out


def _input_as_checked(given: Mapping[str, Any], expression: bool) -> dict[str, Any]:
    """An op claim's input (``{citation, field, value, quote, unit}``) as an answer
    calculation states it: ``l1_handle`` and ``field_path`` (where given), and for an
    expression input its figure as printed, its quote and its unit (where given)."""
    shown = {"l1_handle": plain(given.get("citation"))}
    if plain(given.get("field")) is not None:
        shown["field_path"] = plain(given.get("field"))
    if expression:
        shown["value"] = plain(given.get("value"))
        if _verbatim(plain(given.get("quote"))) is not None:
            shown["quote"] = _verbatim(plain(given.get("quote")))
        if plain(given.get("unit")) is not None:
            shown["unit"] = plain(given.get("unit"))
    return shown


def answer_values(decision: Any) -> list[dict[str, Any]]:
    """Each value claim a final decision passed (``passed_cells``) as a ready-to-copy
    answer value, ``{"claim", "answer_value"}``: the exact form ``kept_value`` keeps, so
    an answer that states it (adding only its ``name``) loses nothing in the final reply.
    An expression as its ``calculation`` (the gate's expression over its inputs, each
    with its printed figure and quote); a count or a sum as its ``calculation`` over its
    inputs; any other claim as a cited value with the quote the gate checked; a role,
    unit or currency only where the gate checked it. None of a decision not answered."""
    out = []
    for cell in passed_cells(decision):
        shown: dict[str, Any] = {"value": cell["value"]}
        if cell["inputs"] is not None:
            shown["calculation"] = {"op": cell["op"], **(
                {"expression": cell["expression"]} if cell["op"] == "expression" else {}),
                "inputs": [dict(given) for given in cell["inputs"]]}
        else:
            shown["citations"] = [
                {"l1_handle": handle, **({"field_path": field} if field is not None else {}),
                 **({"quote": cell["quote"]} if cell["quote"] is not None else {})}
                for handle, field in sorted(cell["cells"], key=repr)]
        for key, checked in (("quantity_role", cell["role"]), ("unit", cell["unit"]),
                             ("currency", cell["currency"])):
            if checked is not None:
                shown[key] = checked
        if kept_value(shown, [cell]) == shown:   # only a form the final reply keeps whole
            out.append({"claim": cell["claim"], "answer_value": shown})
    return out


def _expression_key(expression: Any, inputs: Sequence[tuple[Any, Any, Any, Any]]
                    ) -> tuple[Any, ...] | None:
    """An expression as checked or stated, comparable: its text without spaces, and each
    input's (handle, field, figure, percent or unit) in order; None when unreadable."""
    if not isinstance(expression, str):
        return None
    shown = []
    for handle, field, figure, unit in inputs:
        text = str(figure).strip() if isinstance(figure, (str, int)) \
            and not isinstance(figure, bool) else ""
        percent = text.endswith("%") or isinstance(unit, str) \
            and claim_checks._page_unit(unit) == "percent"
        number = claim_checks.to_decimal(text.rstrip("%"))
        if number is None:
            return None
        shown.append((handle, field, number, "percent" if percent else _text_or_none(unit)))
    return ("".join(expression.split()), tuple(shown))


def _stated_expression(value: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The ``expression`` calculation an answer value states (``_expression_key``), or
    None for any other value."""
    calculation = value.get("calculation")
    if not isinstance(calculation, Mapping) or calculation.get("op") != "expression":
        return None
    inputs = calculation.get("inputs") if isinstance(calculation.get("inputs"), list) else []
    return _expression_key(calculation.get("expression"), [
        (given.get("l1_handle"), given.get("field_path"), given.get("value"), given.get("unit"))
        for given in inputs if isinstance(given, Mapping)]) or ("unreadable",)


def _text_or_none(value: Any) -> str | None:
    """A string with its runs of white space as one space, or None (none, or empty)."""
    return " ".join(value.split()) or None if isinstance(value, str) else None


def _verbatim(value: Any) -> str | None:
    """A quote as the gate checked it, characters kept (a reader checks it verbatim)."""
    return value.strip() or None if isinstance(value, str) else None


def _same(one: Any, other: Any) -> bool:
    a, b = claim_checks.to_decimal(one), claim_checks.to_decimal(other)
    if a is not None and b is not None:
        return a == b
    return " ".join(str(one).split()) == " ".join(str(other).split())


def kept_value(value: Mapping[str, Any], passed: Sequence[Mapping[str, Any]]
               ) -> dict[str, Any] | None:
    """A stated answer value (``value``, ``citations`` of ``l1_handle``, ``field_path``
    and an optional ``quote``, or a ``calculation``'s ``inputs``) as the final reply may
    keep it, or None: the passed claim (``passed_cells``) that states its value on every
    cell it cites (an ``expression`` calculation only as the gate checked it: the same
    expression over the same figures, in order), and the value carrying only what that
    claim checked: each citation's
    quote is the claim's (added where it gave none, dropped where the claim has none),
    its ``quantity_role``, ``unit`` and ``currency`` kept only when they are the claim's. A boolean,
    unreadable or uncited value, or one no passed claim states on those cells: None."""
    stated = value.get("value")
    if stated is None or isinstance(stated, bool) or not isinstance(stated, (str, int, float)):
        return None
    calculation = value.get("calculation")
    inputs = (calculation.get("inputs") or []) if isinstance(calculation, Mapping) else []
    cites = [*(value.get("citations") or []), *inputs]
    if not cites or any(not isinstance(cite, Mapping) for cite in cites):
        return None
    number = claim_checks.to_decimal(stated) is not None
    # An expression is backed only by the expression the gate checked, over the same
    # figures in the same order, and backs no value stated any other way.
    expression = _stated_expression(value)
    claims = [claim for claim in passed if _same(stated, claim["value"])
              and claim.get("calculation") == expression
              and all((cite.get("l1_handle"), cite.get("field_path")) in claim["cells"]
                      or (cite.get("l1_handle"), None) in claim["cells"] for cite in cites)
              and (number or claim["quote"] is not None or all(
                  not cite.get("quote") or _same(cite["quote"], claim["value"])
                  for cite in cites))]
    if not claims:
        return _restated_as_checked(value, passed)
    # The claim whose quote the value states, else the first that states it.
    said = {_text_or_none(cite.get("quote")) for cite in cites} - {None}
    claim = next((claim for claim in claims if _text_or_none(claim["quote"]) in said),
                 claims[0])

    def cite_as_checked(cite: Mapping[str, Any]) -> dict[str, Any]:
        kept = {key: item for key, item in cite.items() if key != "quote"}
        if claim["quote"] is not None:
            kept["quote"] = claim["quote"]
        elif not number and isinstance(cite.get("quote"), str) \
                and _same(cite["quote"], claim["value"]):
            kept["quote"] = cite["quote"]   # a text value quoted as itself: what was checked
        return kept
    out = {key: item for key, item in value.items()
           if key not in ("citations", "calculation", "quantity_role", "unit", "currency")}
    if "citations" in value:
        out["citations"] = [cite_as_checked(cite) for cite in value["citations"]]
    if "calculation" in value:
        # An expression input keeps the quote the gate checked it with (and its unit,
        # which the expression key already matched); any other input states no quote.
        checked = claim.get("input_quotes") or []
        out["calculation"] = {**calculation, "inputs": [
            {**{key: item for key, item in given.items() if key != "quote"},
             **({"quote": checked[at]} if at < len(checked) and checked[at] else {})}
            for at, given in enumerate(inputs)]} if isinstance(calculation, Mapping) \
            else calculation
    for key, checked in (("quantity_role", claim["role"]), ("unit", claim["unit"]),
                         ("currency", claim.get("currency"))):
        if key in value and _text_or_none(value[key]) is not None \
                and _text_or_none(value[key]) == checked:
            out[key] = value[key]
    return out


def _restated_as_checked(value: Mapping[str, Any], passed: Sequence[Mapping[str, Any]]
                         ) -> dict[str, Any] | None:
    """A plain cited value (``citations``, no ``calculation``) that a passed expression
    claim states, kept as that claim's calculation, or None. The claim states the same
    value and reads every cited (handle, field) among its inputs; the value is then the
    gate's expression over the gate's inputs (each its printed figure and quote), its
    ``unit``, ``currency`` and ``quantity_role`` kept only where the claim checked them
    (``kept_value``). An agent that restates the checked result as a cited figure
    gives the reader what the gate checked, never more."""
    cites = value.get("citations")
    if value.get("calculation") is not None or not isinstance(cites, list) or not cites \
            or any(not isinstance(cite, Mapping) for cite in cites):
        return None
    for claim in passed:
        if claim.get("op") != "expression" or not claim.get("inputs") \
                or not isinstance(claim.get("expression"), str) \
                or not _same(value.get("value"), claim["value"]) \
                or not all((cite.get("l1_handle"), cite.get("field_path")) in claim["cells"]
                           or (cite.get("l1_handle"), None) in claim["cells"]
                           for cite in cites):
            continue
        restated = {key: item for key, item in value.items()
                    if key not in ("citations", "calculation")}
        restated["calculation"] = {"op": "expression", "expression": claim["expression"],
                                   "inputs": [dict(given) for given in claim["inputs"]]}
        kept = kept_value(restated, [claim])
        if kept is not None:
            return kept
    return None


def backs(value: Mapping[str, Any], passed: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a stated answer value states exactly what one passed claim states and
    nothing it did not check (``kept_value``): its value equal to the claim's, every cell
    it cites one the claim reads, every quote the claim's own, and no ``quantity_role``,
    ``unit`` or ``currency`` the claim did not carry. A value that adds a quote, role,
    unit or currency the gate never checked is not backed, whatever the rest."""
    kept = kept_value(value, passed)
    if kept is None:
        return False
    for key in ("quantity_role", "unit", "currency"):
        if key in value and value[key] is not None and key not in kept:
            return False
    for given, checked in zip(_quotes(value), _quotes(kept)):
        if given is not None and given != _text_or_none(checked):
            return False
    return True


def _quotes(value: Mapping[str, Any]) -> list[str | None]:
    calculation = value.get("calculation")
    inputs = (calculation.get("inputs") or []) if isinstance(calculation, Mapping) else []
    return [_text_or_none(cite.get("quote")) if isinstance(cite, Mapping) else None
            for cite in [*(value.get("citations") or []), *inputs]]


def _recorded(value_claim: Mapping[str, Any], rows: Callable[[str], Any]) -> str | None:
    """The cited cell as the record holds it (as re-read for the check), when it is a
    short scalar and written otherwise than the claimed value; else None."""
    field = value_claim.get("field")
    if "citation" not in value_claim or field == PAGE_TEXT or field in ORIGINAL_TEXT_FIELDS \
            or claim_checks.text_field(field):
        return None
    resolved = rows(value_claim["citation"])
    found, cell = dig(resolved, field) if isinstance(resolved, Mapping) else (False, None)
    if not found or isinstance(cell, (bool, Mapping, list)) or cell is None:
        return None
    literal = " ".join(str(cell).split())
    return literal[:200] if literal and literal != value_claim["value"] else None


def _phrase(value_claim: Mapping[str, Any], result: Mapping[str, Any],
            recorded: str | None, lead: bool) -> str:
    """A passing claim's value as the answer states it: its bound (``at least N`` for a
    count no complete count binds), the cell as recorded when written otherwise, its
    verified unit and its currency, and a lead said to be one."""
    value = value_claim["value"]
    out = f"at least {value}" if result.get("bound") == "at_least" else value
    if recorded is not None:
        out += f' (recorded as "{recorded}")'
    # A unit the check left unverified (a page that prints none after the figure) is not
    # stated; a currency passes only verified.
    if value_claim.get("unit") and result.get("unit_basis") not in (None, "unverified"):
        out += f" {value_claim['unit']}"
    if value_claim.get("currency"):
        out += f" {value_claim['currency']}"
    return f"{out} {LEAD_MARK}" if lead else out


def _source(handle: str) -> str:
    """The system and table or store of an L1 handle (``api://public.app_sales_order``)."""
    scheme, _, rest = handle.partition("://")
    return f"{scheme}://{rest.split('/', 1)[0].split('#', 1)[0]}"[:200]


def _prose(raw: Any) -> list[str]:
    """The answerer's short prose as sentence templates naming claims ``{id}``: a
    sentence naming none is not kept (``rendered_text`` screens the rest)."""
    if not isinstance(raw, str):
        return []
    out = []
    for sentence in SENTENCE_BREAK.split(raw[:MAX_TEXT_CHARS]):
        sentence = " ".join(sentence.split())
        if PLACEHOLDER.search(sentence):
            out.append(sentence)
    return out[:MAX_CLAIMS]


def _on_a_lead(value_claim: Mapping[str, Any], claim: Mapping[str, Any],
               leads: Mapping[Any, Any]) -> bool:
    """Whether the claim's value rests only on lead-tier links: the cell a field claim
    cites is a lead cell; a claim with no field (a count or other operation over records)
    counts a record whose lead cell this session was returned."""
    field = value_claim.get("field")
    if isinstance(field, str):
        return leads.get((value_claim.get("citation"), field)) is True
    cited = set(claim["citations"])
    return any(flag is True and isinstance(cell, tuple) and cell[0] in cited
               for cell, flag in leads.items())


def rendered_text(claims: list[Mapping[str, Any]], prose: list[str] | None = None) -> str:
    """The answer text code renders from checked ``claims`` (a reader's: pass only the
    claims it is shown): each ``prose`` template whose every ``{id}`` is one of them,
    rendered with those claims' values, that the one prose screen passes
    (``claim_checks.screen_prose``: its own words state nothing no claim of it checked);
    then a line (``<schema label>: <value>``) per claim no kept template names. Nothing
    else: no figure the checked claims do not state, no claim that was refused."""
    by_id = {claim["id"]: claim for claim in claims}
    lines, named = [], set()
    for template in prose or ():
        ids = PLACEHOLDER.findall(template)
        if not ids or any(cid not in by_id or not by_id[cid].get("value_claims")
                          for cid in ids):
            continue
        pieces, spans, value_claims, at = [], [], [], 0
        for match in PLACEHOLDER.finditer(template):
            pieces.append(template[at:match.start()])
            start = sum(len(piece) for piece in pieces)
            phrase = by_id[match.group(1)]["phrase"]
            spans.append((start, start + len(phrase), len(value_claims)))
            value_claims.append(by_id[match.group(1)]["value_claims"][0])
            pieces.append(phrase)
            at = match.end()
        sentence = "".join(pieces + [template[at:]])
        if claim_checks.screen_prose(sentence, value_claims, spans=spans):
            continue
        lines.append(sentence)
        named.update(ids)
    lines += [claim["text"] for claim in claims if claim["id"] not in named]
    return "\n".join(lines)


def shown_unsupported(unsupported: list[dict[str, Any]],
                      withholds: Callable[[list[str]], bool] | None) -> list[dict[str, Any]]:
    """The unsupported claims as listed: never their citations; a claim citing a record
    ``withholds`` says the reader may not open keeps only its reason and failure codes
    (``text: None``, ``text_withheld``). A failing ``withholds`` withholds."""
    out = []
    for entry in unsupported:
        cited = entry.get("_cited") or []
        shown = {key: value for key, value in entry.items() if key != "_cited"}
        if withholds is not None and cited:
            try:
                hidden = bool(withholds(list(cited)))
            except Exception:  # noqa: BLE001 - an access check that fails withholds
                hidden = True
            if hidden:
                shown = {"text": None, "reason": entry["reason"],
                         "text_withheld": "cites_a_record_withheld_from_the_reader",
                         **({"failures": [{"code": f["code"]} for f in entry["failures"]]}
                            if entry.get("failures") else {})}
        out.append(shown)
    return out


__all__ = ["ANSWER_BACKING", "CLAIM_KEYS", "LEAD_UNMARKED", "MAX_REPAIRS", "OUTCOMES", "REPAIR_ANSWER",
           "ANSWER_FINAL", "WITHHOLD_REFUSED",
           "RESOLVED_STATUSES", "backs", "evidence_handles", "final_reply", "gate_answer",
           "kept_value", "passed_cells", "rendered_text", "unbacked",
           "shown_unsupported"]
