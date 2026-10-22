"""The final-answer gate: the one function every answer path of the Workbench runs.

Measure what ships: the console's Ask (``operating.agent_loop``) and an
employee's MCP session (``plx.case_get`` answer mode on ``operating.mcp_server``, where
Claude Code or any MCP client submits its final claims; the real-agent eval's agent is
one such client) both call ``gate_answer``. Nothing else decides which claims of a final answer
stand; the eval harness never rewrites an answer (``evals.runner`` records what this gate
returned in the session, from the session's own tool results).

``gate_answer(decision, server, seen=, returned=, may_repair=)``:

* Claims: an answer is a list of structured claims (``CLAIM_KEYS``), each one value
  (``value``) read from one record (``citation``, an L1 handle, and ``field``) or counted
  or summed over cited records (``op`` ``count`` or ``sum`` ``of`` them), with optional
  ``role``, ``unit``, ``currency``, an ``id`` and ``lead`` (the value rests on a lead-tier
  link), plus short prose (``text``) that names claims by id (``{c1}``). A key the gate
  does not know, a bad id or a duplicate one refuses the claim (``claim_invalid``). An MCP
  client's claim (``text``, ``citations``, ``value_claims``) is one claim per value claim
  (``claim_checks.parse_value_claims``; malformed: ``value_claims_invalid``; none:
  ``claim_states_no_value``); its text is never shown. A citation counts only if a tool
  returned it to this session as evidence (``evidence_handles``,
  ``docs/decisions/2026-10-07-agent-evidence-contract.md``); one handle the session was
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
* Repair once: an answer the gate leaves with no claim is not final while the caller
  allows a repair (``may_repair``): the outcome is ``repair`` with the refused claims (their
  text, reason and gate codes, never their citations) and ``REPAIR_ANSWER``; the caller
  asks again and the gate decides the next answer the same way. With no repair left it is
  an abstention (``no_checked_claims`` when cited claims failed the gate or their shape,
  else ``no_cited_claims``). An answer with at least one passing claim is ``answered``: the
  passing claims are the answer, the refused ones stay visible as unsupported.

The caller keeps the repair count (the Ask loop's ``Bounds.max_repairs`` and step budget;
an MCP session's ``MAX_REPAIRS``). ``shown_unsupported`` lists unsupported claims for a
reader: never their citations; a claim citing a record withheld from the reader keeps
only its reason and codes.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

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
                        "op", "of", "lead"})
CLAIM_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
MAX_OP_CITATIONS = 100
# A value resting only on lead-tier links that its claim does not state as a lead.
LEAD_UNMARKED = "claim_lead_unmarked"
MAX_TEXT_CHARS = 2000
PLACEHOLDER = re.compile(r"\{([A-Za-z][A-Za-z0-9_]{0,31})\}")
SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+|\n+")

REPAIR_ANSWER = (
    "Every claim of your answer was refused (refused_claims: each claim's reason and the "
    "gate's codes). Fix it: state each value as a claim of its own, the value copied "
    "exactly as the cited record holds it with that record's handle and field, cite only "
    "handles the tools returned, mark a value that rests on a lead (lead_basis) "
    "\"lead\": true, drop what no record supports and state it as an unknown (\"not in "
    "the record: <what>\"), read more if a record is missing, or abstain if nothing the "
    "tools returned answers any part.")


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
                returned: Mapping[str, Any], may_repair: bool) -> dict[str, Any]:
    """The gated final answer of ``decision`` (``claims``, ``text``, ``unknowns``) for
    ``server``'s session (module doc): ``{"outcome": "answered" | "repair" | "abstained",
    "reason", "claims", "dropped", "unsupported", "unknowns", "checks", "repair", "text",
    "prose"}``. ``claims``: the checked claims, each with the text code rendered for it;
    ``text``: the answer as code renders it from them (``rendered_text``), ``prose`` the
    templates it used. ``unsupported`` entries keep the strings they cited under
    ``_cited`` for ``shown_unsupported`` only; ``repair`` is the request to the answerer
    (refused claims without citations), or None. ``seen``: the handles tool results
    returned this session as evidence."""
    claims, dropped, unsupported = _claims(decision, seen)
    raw = decision.get("unknowns") if isinstance(decision, Mapping) else None
    unknowns = [str(u)[:MAX_UNKNOWN_CHARS] for u in raw
                if isinstance(u, str)][:MAX_UNKNOWNS] if isinstance(raw, list) else []
    checks = None
    if claims:
        claims, failed, checks = _check(claims, server, returned)
        dropped += len(failed)
        unsupported += failed
    prose = _prose(decision.get("text") if isinstance(decision, Mapping) else None)
    out = {"outcome": "answered", "reason": None, "claims": claims, "dropped": dropped,
           "unsupported": unsupported, "unknowns": unknowns, "checks": checks,
           "repair": None, "text": rendered_text(claims, prose), "prose": prose}
    if claims:
        return out
    if may_repair:
        out.update(outcome="repair", reason="answer_refused", repair={
            "kind": "answer_refused",
            "refused_claims": [{k: v for k, v in entry.items() if k != "_cited"}
                               for entry in unsupported],
            "instruction": REPAIR_ANSWER})
        return out
    # Cited claims that all failed the gate (or its shape) are not "uncited".
    checked = any(u["reason"] in ("claim_check_failed", "value_claims_invalid",
                                  "claim_invalid", "claim_states_no_value")
                  for u in unsupported)
    out.update(outcome="abstained",
               reason="no_checked_claims" if checked else "no_cited_claims")
    return out


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
            refuse(label, "value_claims_invalid", cited, detail=str(error)[:200])
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
        refuse(text, "value_claims_invalid", strings, detail=str(error)[:200])
        return 0
    if not value_claims:
        # Its text is never shown: a claim states what it says as value claims.
        refuse(text, "claim_states_no_value", strings)
        return 0
    for value_claim in value_claims:
        kept.append({"id": fresh("m"), "lead": claim.get("lead") is True, "said": text,
                     "citations": list(dict.fromkeys(
                         [value_claim["citation"]] if "citation" in value_claim
                         else [given["citation"] for given in value_claim["of"]])),
                     "value_claims": [value_claim]})
    return 0


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


def _check(claims: list[dict], server: Any, returned: Mapping[str, Any]
           ) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """(the claims that pass, with their rendered text, the failed ones as unsupported
    with the gate's codes, the gate's summary): each claim's value claim through
    ``check_items`` over the cited records as ``server``'s actor re-reads them now, with no
    prose (what a claim says is its value claim), then a value resting only on lead-tier
    links (``returned["leads"]``) that the claim does not state as a lead fails
    (``claim_lead_unmarked``)."""
    read = gate_rows(citation_rows(server.case_runtime), server.actor)
    cache: dict[str, Any] = {}

    def rows(handle: str) -> Any:
        if handle not in cache:
            cache[handle] = read(handle)
        return cache[handle]

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
                       "value_claims": claim["value_claims"], "lead": claim["lead"],
                       "source": _source(claim["citations"][0]),
                       "check": {"status": "passed", "failures": []}})
    return passed, failed, {"schema": CLAIM_CHECKS_SCHEMA, "counts": counts,
                            "passed": report["passed"] and len(passed) == len(claims)}


TEXT_INSTRUCTION = (
    "Your text states numbers or ISO dates (uncovered) that no passing claim states. A "
    "number in prose is a claim: state each as a cited value claim (an identifier whose "
    "digits stand alone after capitals, such as ABC-012, as a text value of the field "
    "that holds it) and submit again, or leave it out of the text.")
UNSTATED_INSTRUCTION = (
    "Your text writes a passing claim's value negated (uncovered, claim_value_unstated): "
    "it denies what the claim states. Say the value as the claim states it.")
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
    read = gate_rows(citation_rows(server.case_runtime), server.actor)
    cache: dict[str, Any] = {}

    def rows(handle: str) -> Any:
        if handle not in cache:
            cache[handle] = read(handle)
        return cache[handle]

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
        (TEXT_INSTRUCTION, any(e["code"] != "claim_value_unstated" for e in uncovered)),
        (UNSTATED_INSTRUCTION, any(e["code"] == "claim_value_unstated" for e in uncovered)),
        (UNCHECKED_INSTRUCTION, bool(unchecked))) if needed]
    return {"passed": not uncovered, "uncovered": uncovered, "unchecked": unchecked,
            "instruction": " ".join(said) or None}


def _recorded(value_claim: Mapping[str, Any], rows: Callable[[str], Any]) -> str | None:
    """The cited cell as the record holds it (as re-read for the check), when it is a
    short scalar and written otherwise than the claimed value; else None."""
    field = value_claim.get("field")
    if "citation" not in value_claim or field == PAGE_TEXT or field in ORIGINAL_TEXT_FIELDS:
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
    count no complete count binds), the cell as recorded when written otherwise, its unit
    or currency, and a lead said to be one."""
    value = value_claim["value"]
    out = f"at least {value}" if result.get("bound") == "at_least" else value
    if recorded is not None:
        out += f' (recorded as "{recorded}")'
    for key in ("unit", "currency"):
        if value_claim.get(key):
            out += f" {value_claim[key]}"
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


__all__ = ["CLAIM_KEYS", "LEAD_UNMARKED", "MAX_REPAIRS", "OUTCOMES", "REPAIR_ANSWER",
           "RESOLVED_STATUSES", "evidence_handles", "gate_answer", "rendered_text",
           "shown_unsupported"]
