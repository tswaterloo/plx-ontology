"""The final-reply gate of an agent session (moved out of ``operating.agent_session``, which
re-exports every name): what the session's own answer-gate decisions (``plx.case_get``
answer mode, ``operating.answer_gate``) passed, the host's own submission when the agent
ran out before submitting, and ``gate_final_reply``, the agent's final message as the
reply a reader sees. Deterministic, no read."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from operating.answer_contract import (ANSWERABILITY, _JSON_FENCE, NUMERIC_OPS,
                                       answer_candidate, answer_problems, parse_answer)

ANSWER_GATE_TOOL = "plx.case_get"
# Tools the gate answers itself whose results are no evidence (``LocalTool``): the reader's
# memory (``operator_api.memory.TOOL_NAME``) is what was said before, never a read of the
# sources now, so a name only it returned stays unbacked in the final reply.
NOT_EVIDENCE_TOOLS = frozenset({"plx.memory"})


def answer_gate_record(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What the Workbench's final-answer gate (``plx.case_get`` answer mode,
    ``operating.answer_gate``) returned to the agent in this run, from the run's own tool
    results: ``submissions``, each ``outcomes`` entry (``answered``, ``repair``,
    ``abstained``, or ``unreadable``/``refused`` when its result cannot be read or the call
    failed), the claims each passed and refused, whether a repair followed (``repaired``:
    a later submission was answered) and the last outcome (``final``; None: the agent never
    submitted); ``host_submitted``: the last submission was the host's own at session
    end (``host_submission``). Counts only; never a claim's text or citation."""
    outcomes, passed, refused = [], [], []
    host = False
    for event in events:
        arguments = event.get("arguments")
        if event.get("type") != "tool_call" or event.get("name") != ANSWER_GATE_TOOL \
                or not isinstance(arguments, Mapping) or "answer" not in arguments:
            continue
        result = event.get("result")
        outcome = result.get("outcome") if isinstance(result, Mapping) else None
        if event.get("is_error") or (isinstance(result, Mapping) and "error" in result
                                     and outcome is None):
            outcomes.append("refused")
        elif outcome not in ("answered", "repair", "abstained"):
            outcomes.append("unreadable")
        else:
            outcomes.append(outcome)
        claims = result.get("claims") if isinstance(result, Mapping) else None
        unsupported = result.get("unsupported_claims") if isinstance(result, Mapping) \
            else None
        passed.append(len(claims) if isinstance(claims, list) else 0)
        refused.append(len(unsupported) if isinstance(unsupported, list) else 0)
        host = event.get("submitted_by") == HOST_SUBMITTER
    first_repair = outcomes.index("repair") if "repair" in outcomes else None
    return {"submissions": len(outcomes), "outcomes": outcomes, "passed_claims": passed,
            "refused_claims": refused, "final": outcomes[-1] if outcomes else None,
            "repaired": first_repair is not None
            and "answered" in outcomes[first_repair + 1:], "host_submitted": host}


# The submitter of the gate call the host makes itself at a session's end
# (``host_submission``), recorded on its transcript event.
HOST_SUBMITTER = "host"


def cut_submission(events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The answer a session stopped at its cap last submitted to its gate, for the host to
    submit again (``HostGate.finish``): None when a decision of the session already passed
    a claim (that decision is the reply) or it submitted nothing. The gate decides it as
    any submission: a value it refuses stays withheld."""
    from operating import answer_gate
    decision, _refused = _gate_results(events)
    if answer_gate.passed_cells(decision):
        return None
    for event in reversed(events):
        arguments = event.get("arguments")
        if event.get("type") != "tool_call" or event.get("name") != ANSWER_GATE_TOOL \
                or not isinstance(arguments, Mapping):
            continue
        answer = arguments.get("answer")
        if isinstance(answer, Mapping) and isinstance(answer.get("claims"), list):
            return dict(answer)
        recovered = _recovered_submission([event])
        if recovered is not None:
            return dict(recovered)
    return None


def _recovered_submission(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The answer of the session's last gate call that never reached the gate because its
    input was not valid JSON (Claude Code's ``__unparsedToolInput``), when the one fault
    is closing brackets past its one object (EMD-A1, QO-C2: ``}}}`` for ``}}``): the
    object read up to its end; None for any other fault (never guessed)."""
    for event in reversed(events):
        arguments = event.get("arguments")
        if event.get("type") != "tool_call" or event.get("name") != ANSWER_GATE_TOOL \
                or not isinstance(arguments, Mapping):
            continue
        raw = (arguments.get("__unparsedToolInput") or {}) \
            if "answer" not in arguments else None
        raw = raw.get("raw") if isinstance(raw, Mapping) else None
        if not isinstance(raw, str):
            return None
        try:
            value, end = json.JSONDecoder().raw_decode(raw.strip())
        except ValueError:
            return None
        rest = raw.strip()[end:]
        answer = value.get("answer") if isinstance(value, Mapping) else None
        if set(rest) <= set("}] \t\r\n") and isinstance(answer, Mapping) \
                and isinstance(answer.get("claims"), list):
            return answer
        return None
    return None


def host_submission(events: Sequence[Mapping[str, Any]], final_text: str
                    ) -> dict[str, Any] | None:
    """The answer the host submits to the session's own gate at its end, for a session
    that ended with a reply but no decision that passed a claim (it never submitted, its
    call was malformed, or it stopped at a repair), or None: a passed decision, or
    nothing to submit. The reply's answer block, each stated value as one claim citing
    what the value cites (its quote, role, unit and currency as stated; a calculation as
    its op over its inputs), each null value an unknown, its text as the text to check;
    else the session's last gate call recovered from unbalanced closing brackets
    (``_recovered_submission``). The gate decides it as any submission (same checks,
    same evidence of the session): a value it refuses stays withheld."""
    from operating import answer_gate
    decision, _refused = _gate_results(events)
    if answer_gate.passed_cells(decision):
        return None
    answer, _error = parse_answer(final_text)
    claims, unknowns = [], []
    for value in (answer or {}).get("values") or []:
        if not isinstance(value, Mapping):
            continue
        name, stated = str(value.get("name") or "value"), value.get("value")
        if stated is None:
            unknowns.append(name[:answer_gate.MAX_UNKNOWN_CHARS])
            continue
        if isinstance(stated, bool) or not isinstance(stated, (str, int, float)):
            continue
        claim = _value_claim(value, stated)
        if claim is None:
            continue
        cited = list(dict.fromkeys(
            [claim["citation"]] if "citation" in claim
            else [given["citation"] for given in claim["of"]]))
        extra = [_value_claim(dict(value, citations=[cite]), stated)
                 for cite in (value.get("citations") or [])[1:]]
        claims.append({"text": f"{name}: {stated}"[:answer_gate.MAX_CLAIM_CHARS],
                       "citations": cited[:answer_gate.MAX_CITATIONS_PER_CLAIM],
                       "value_claims": [claim, *(e for e in extra if e is not None)]})
    if not claims:
        recovered = _recovered_submission(events)
        return dict(recovered) if recovered is not None else None
    out: dict[str, Any] = {"claims": claims[:answer_gate.MAX_CLAIMS]}
    if unknowns:
        out["unknowns"] = unknowns[:answer_gate.MAX_UNKNOWNS]
    text = answer.get("text")
    if isinstance(text, str) and 0 < len(text) <= answer_gate.MAX_TEXT_CHARS:
        out["text"] = text
    return out


def _value_claim(value: Mapping[str, Any], stated: Any) -> dict[str, Any] | None:
    """One answer value as the gate's value claim (``ANSWER_GATE_RULE``'s shape): its first
    citation, or its calculation over its inputs; None when it cites nothing."""
    calculation = value.get("calculation")
    if isinstance(calculation, Mapping):
        inputs = [given for given in calculation.get("inputs") or []
                  if isinstance(given, Mapping) and isinstance(given.get("l1_handle"), str)]
        op = calculation.get("op")
        if not inputs or op not in ("expression", "count", "count_distinct", "days",
                                    "add_days", *NUMERIC_OPS):
            return None
        of = []
        for given in inputs:
            entry = {"citation": given["l1_handle"]}
            if op not in ("count", "count_distinct") \
                    and isinstance(given.get("field_path"), str):
                entry["field"] = given["field_path"]
            if op == "expression":
                entry.update({key: given[key] for key in ("value", "quote", "unit")
                              if given.get(key) is not None})
            elif op in NUMERIC_OPS and given.get("unit") is not None:
                entry["unit"] = given["unit"]
            of.append(entry)
        claim = {"value": stated, "op": op, "of": of}
        if op == "expression":
            claim["expression"] = calculation.get("expression")
        if op in ("days", "add_days") and "day" in calculation:
            claim["day"] = calculation["day"]
    else:
        cites = [cite for cite in value.get("citations") or []
                 if isinstance(cite, Mapping) and isinstance(cite.get("l1_handle"), str)]
        if not cites:
            return None
        claim = {"value": stated, "citation": cites[0]["l1_handle"]}
        if isinstance(cites[0].get("field_path"), str):
            claim["field"] = cites[0]["field_path"]
        if isinstance(cites[0].get("quote"), str) and cites[0]["quote"].strip():
            claim["quote"] = cites[0]["quote"]
    for key, given in (("role", value.get("quantity_role")), ("unit", value.get("unit")),
                       ("currency", value.get("currency"))):
        if isinstance(given, str) and given.strip():
            claim[key] = given
    return claim


FINAL_REPLY_FORMAT = "plx-final-reply-gate/v1"


WITHHELD_REASON = "withheld_by_answer_gate"


# Answerability labels from most to least certain: the final reply keeps the less certain
# of the gate's decision and the answer's own (``gate_final_reply``).
CERTAINTY = ("answerable", "partial", "blocked")


# The keys of a ``plx-eval-answer/v1`` answer (``evals.score.answer_problems``): the final
# reply drops any other (RM-05: a ``gaps_note`` beside them made the answer malformed and
# its json block was cut as prose), so nothing the gate never read is stated.
ANSWER_KEYS = ("format", "answerability", "text", "values", "gaps")


# A dropped key's note, kept as an unknown gap: at most this many, each this long.
NOTE_REASON, NOTE_CHARS, MAX_NOTES = "answer_note", 300, 5


def _gate_results(events: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any] | None,
                                                                 list[Any]]:
    """(the gate's last decision in the session, ``plx.case_get`` answer mode, or None:
    never submitted, or no submission gave a result it can read; the refused claims of
    every submission). A submission that failed or came back unreadable (an error,
    a truncated result) decided nothing, so it never undoes the decision before it
    (F23-0047 on v14: a resubmission refused result_too_large nulled the four values
    the decision before it had passed), unless it gives another value for a claim that
    decision passed (``answer_gate.contradicted_claims``: the same role, passage or
    record field): then the agent replaced that value, the earlier decision no longer
    stands and none is in force (the host told the agent so, a repair turn:
    ``answer_gate.CLEARED_BY_CONTRADICTION``)."""
    event, refused = _deciding_event(events)
    return (event.get("result") if event is not None else None), refused


def _deciding_event(events: Sequence[Mapping[str, Any]]
                    ) -> tuple[Mapping[str, Any] | None, list[Any]]:
    """(the gate call whose decision is in force at the session's end, as
    ``_gate_results`` reads it, or None; the refused claims of every submission)."""
    from operating import answer_gate
    last, refused = None, []
    for event in events:
        arguments = event.get("arguments")
        if event.get("type") == "tool_call" and event.get("name") == ANSWER_GATE_TOOL \
                and isinstance(arguments, Mapping) and "answer" in arguments:
            result = event.get("result")
            readable = not event.get("is_error") and isinstance(result, Mapping) \
                and isinstance(result.get("claims"), list)
            if readable:
                last = event
                refused += list(result.get("unsupported_claims") or [])
            elif last is not None and isinstance(arguments["answer"], Mapping) \
                    and answer_gate.contradicted_claims(
                        last["result"], arguments["answer"].get("claims")):
                last = None
    return last, refused


def _quotes(value: Mapping[str, Any]) -> list[str | None]:
    """The distinct quotes a kept answer value's citations carry, or [None]."""
    quotes = [cite["quote"] for cite in value.get("citations") or ()
              if isinstance(cite, Mapping) and isinstance(cite.get("quote"), str)
              and cite["quote"].strip()]
    return list(dict.fromkeys(quotes)) or [None]


def _said(text: str) -> bool:
    """Whether a final reply's text says anything beyond its withheld list."""
    return any(line.strip() and not line.startswith("Withheld: ")
               for line in text.splitlines())


def one_gap_per_value(gaps: Sequence[Any]) -> list[Any]:
    """``gaps`` with "one value, one gap": the first gap that ``concerns`` a value keeps
    it, and each later gap concerning that value loses its ``concerns`` (it stays, as a
    gap about its subject). AG-05 gave one null value two gaps in two states, which the
    scorer reads as contradictory; no gap is dropped."""
    seen: set[str] = set()
    out = []
    for gap in gaps:
        concerns = gap.get("concerns") if isinstance(gap, Mapping) else None
        if isinstance(concerns, str) and concerns in seen:
            gap = {key: item for key, item in gap.items() if key != "concerns"}
        elif isinstance(concerns, str):
            seen.add(concerns)
        out.append(gap)
    return out


def gate_final_reply(final_text: str, events: Sequence[Mapping[str, Any]],
                     question: str = "") -> tuple[str, dict[str, Any]]:
    """The agent's final message as the product's final reply
    (``operating.answer_gate.final_reply``, the function every answer path runs), over
    the session's own gate decisions: the scorer grades what a reader of that session
    gets. Deterministic, no read: the gate already decided every claim.

    * A stated value is kept when it states a claim the session's last decision passed,
      on the same cell, and kept as the gate checked it (``answer_gate.kept_value``): the
      claim's quote on each citation, and a unit or currency only when the claim
      carried it as verified; one the gate did not verify (a unit the page does not print
      beside the figure) is dropped, never the value, and so is that unit word beside
      the value in the texts; a currency the texts write beside it other than the one
      the gate checked refuses that sentence (``answer_gate.final_reply``). Any other value (refused, reworded
      after its refusal, never submitted, or stating a quantity role the gate never
      checked: the reply's quantity words ride on it) is withheld: null, uncited, a gap
      (``unknown``, ``WITHHELD_REASON``) unless a gap already concerns it.
    * The reply states the less certain (``CERTAINTY``) of the gate's answerability (its
      final decision's ``answerability.state``, when the decision passed a claim and
      gives one; a decision that passed nothing is ``blocked`` for that alone) and the
      answer's own: the gate never makes an agent's ``partial`` or ``blocked``
      ``answerable``; an agent's ``blocked`` beside a claim the gate passed is
      ``partial`` (never blocked, never answerable). A withheld value or an unresolved
      source conflict gap (``answer_gate.unresolved_conflict``) makes an ``answerable``
      one ``partial``, and nothing backed surviving makes it ``blocked``.
    * Its gaps are the answer's own, none dropped (the gate refutes no gap), with one gap
      per value (``one_gap_per_value``): a later gap concerning a value an earlier gap
      concerns stays without ``concerns``.
    * A kept value in a percent unit the gate checked is written with ``%`` on its
      claim's line (``name: 100%``), the printed percent the gate's and the scorer's
      prose screens read at percent scale.
    * The answer's text and the message around the answer block lose each sentence that
      carries an unbacked statement; the rest stays; the answer text ends with the
      withheld list (``final_reply``).
    * An answer key the scorer does not read (``ANSWER_KEYS``: a ``gaps_note``) is dropped
      and named in ``report["dropped_keys"]``; a non-empty text under it becomes an unknown
      gap (``NOTE_REASON``) when the reply screen passes it whole, so the reader still sees
      it; the rest is gated as any answer. One still
      malformed without it is no answer (the message is screened as prose).
    * Fail closed: no answer block, no decision, a decision that is not ``answered`` or
      a result the session could not read: nothing passed.

    ``(text, report)``: ``report`` (``FINAL_REPLY_FORMAT``): ``rewritten``, the values
    withheld (names), the claims the gate refused, sentences seen and removed, the
    unbacked statements."""
    from operating import answer_gate, claim_checks
    from operating import claim_checks_grammar

    gate, refused = _gate_results(events)
    passed = answer_gate.passed_cells(gate)
    claims = list(gate.get("claims") or []) if passed else []
    # the gate's own passed claims: a count or total among them leads a reply that asks
    # for one and never states it (``answer_gate.unstated_figures``)
    figure_claims = list(claims)
    report: dict[str, Any] = {"format": FINAL_REPLY_FORMAT, "gate": (gate or {}).get("outcome"),
                              "rewritten": False, "withheld": [],
                              "refused_by_gate": len(refused), "sentences": 0,
                              "removed_sentences": 0, "unbacked": []}

    # A name a tool returned verbatim (a record number, a customer or file name) is a
    # name in the reply, never a figure (``answer_gate.returned_names``).
    names = answer_gate.returned_names(events, skip_tools=(ANSWER_GATE_TOOL,
                                                           *NOT_EVIDENCE_TOOLS))
    # an amount a tool returned that the reply writes with no currency beside it gets
    # one footnote line, never an edit of its sentences (``answer_gate.currency_footnote``)
    amounts = answer_gate.returned_amounts(events, skip_tools=(ANSWER_GATE_TOOL,
                                                               *NOT_EVIDENCE_TOOLS))

    def footnote(*replies: dict[str, Any]) -> None:
        for reply in replies:
            noted = answer_gate.currency_footnote(reply["text"], amounts)
            if noted != reply["text"]:
                reply["text"], reply["modified"] = noted, True
                report["currency_footnote"] = True

    def lead_figures(*replies: dict[str, Any]) -> None:
        # The count or total the question asks for leads the reply when none of its texts
        # states it (11-07 dev 0066), ahead of every sentence kept, never in their place
        # (11-07 dev 0093: a reply of the counts alone when they went into the empty text
        # around the answer block). Each text that may be shown gets them: a reader sees
        # the prose around the block or, with none, the answer's own text, never both
        # (``reply_prose``), so this never repeats them; when every one is empty, the
        # first does.
        figures = answer_gate.unstated_figures(
            "\n".join(str(reply["text"]) for reply in replies), figure_claims, question)
        if not figures:
            return
        report["figures_added"] = len(figures)
        filled = [reply for reply in replies if str(reply["text"]).strip()] or replies[:1]
        for reply in filled:
            reply["text"] = "\n".join(figures + ([str(reply["text"]).strip()]
                                                 if str(reply["text"]).strip() else []))
            reply["modified"] = True

    def screen(text: Any, withheld: Sequence[str] = ()) -> dict[str, Any]:
        reply = answer_gate.final_reply(text, claims, unsupported=refused, question=question,
                                        withheld=withheld, names=names)
        report["sentences"] += reply["sentences"]
        report["removed_sentences"] += reply["removed_sentences"]
        report["unbacked"] = list(dict.fromkeys(report["unbacked"] + reply["unbacked"]))[
            :answer_gate.MAX_SHOWN_UNBACKED]
        return reply

    answer, error = parse_answer(final_text)
    dropped, notes = [], []
    if answer is None and error == "answer_malformed:unknown_keys":
        candidate = answer_candidate(final_text) or {}
        trimmed = {key: candidate[key] for key in ANSWER_KEYS if key in candidate}
        if not answer_problems(trimmed):
            answer, dropped = trimmed, sorted(str(key)[:40] for key in candidate
                                              if key not in ANSWER_KEYS)
            report["dropped_keys"] = dropped[:20]
            # A note the answer gave under its own key (RM-05's gaps_note) stays visible as
            # an unknown gap, screened as the reply's text is; any other value drops.
            notes = [" ".join(candidate[key].split())[:NOTE_CHARS] for key in candidate
                     if key not in ANSWER_KEYS and isinstance(candidate[key], str)
                     and candidate[key].strip()][:MAX_NOTES]
    if answer is None:
        reply = screen(final_text)
        lead_figures(reply)
        footnote(reply)
        if reply["modified"]:
            report.update(rewritten=True, answer_error=error)
        return reply["text"], report
    if passed and not answer.get("values"):
        # An answer block that copies none of the claims the gate passed still states
        # them (its prose does: 11-06, six replies lost sentences whose figures only the
        # passed claims hold): they are its values, each in the form the reply keeps
        # whole (``answer_gate.answer_values``), so the prose stating them is backed.
        from operating.answer_contract import MAX_VALUES
        # each named by its claim's own words (the reader never sees a claim id)
        said = {claim.get("id"): answer_gate.plain(claim.get("text"))
                for claim in gate.get("claims") or () if isinstance(claim, Mapping)}
        copied = [{"name": (said.get(item["claim"]) if isinstance(said.get(item["claim"]), str)
                            and said[item["claim"]].strip() else item["claim"])[:120],
                   **item["answer_value"]}
                  for item in answer_gate.answer_values(gate)
                  if isinstance(item.get("claim"), str)]
        copied = [value for value in copied
                  if not answer_problems({"answerability": "partial", "values": [value]})]
        if copied:
            answer = {**answer, "values": copied[:MAX_VALUES]}
    values, gaps = [], one_gap_per_value(answer.get("gaps") or [])
    concerned = {gap.get("concerns") for gap in gaps}
    said: list[str] = []
    unchecked: dict[int, Any] = {}   # by place in values: a unit the gate did not verify
    for value in answer.get("values") or []:
        if value.get("value") is None:
            values.append(value)
            continue
        kept = answer_gate.kept_value(value, passed)
        if kept is not None and value.get("quantity_role") is not None \
                and "quantity_role" not in kept:
            # A role the gate never checked is withheld with its value, never dropped: the
            # reply's quantity words ride on it (``ordered 2400``), and without the role
            # they would state what nothing checked.
            kept = None
        if kept is not None:
            if value.get("unit") and not kept.get("unit"):
                unchecked[len(values)] = value["unit"]
            values.append(kept)
            continue
        report["withheld"].append(value["name"])
        said += [str(value["value"])] + [cite["quote"] for cite in value.get("citations") or ()
                                         if isinstance(cite, Mapping)
                                         and isinstance(cite.get("quote"), str)]
        values.append({key: item for key, item in value.items()
                       if key in ("name", "unit", "quantity_role", "currency")} | {"value": None})
        if value["name"] not in concerned:
            gaps.append({"state": "unknown", "subject": value["name"],
                         "reason": WITHHELD_REASON, "concerns": value["name"]})
    # A withheld value's own words are refused material too: its rewording is found.
    refused = refused + [{"text": said_text} for said_text in said]
    # The answer's text may state what its kept values state, each cited there (a value
    # the gate passed that the answer does not keep has no citation in it).
    # A kept value's unit and currency are the ones the gate checked (``kept_value``): the
    # texts keep them; a unit it dropped from the value goes from the texts too, for that
    # value (``final_reply``), and a currency beside it other than its own refuses the
    # sentence.
    # A value in a percent unit the gate checked is written as the percent it is
    # (``100%``), as the gate and the scorer read a printed percent (the line of the
    # claim the reply falls back to).
    claims = [{"text": f"{value['name']}: "
               + ("at least " if value.get("bound") == "at_least" else "")
               + f"{value['value']}" + (
                   "%" if value.get("unit") and claim_checks_grammar._page_unit(str(value["unit"]))
                   == "percent" and not str(value["value"]).endswith("%") else ""),
               # One value claim per quote the gate checked it with (the reply screen
               # reads what each is about: ``answer_gate._denied``).
               "value_claims": [{"value": value["value"],
                                 **({"unit": value["unit"]} if value.get("unit") else
                                    {"unit": unchecked[at]} if at in unchecked else {}),
                                 **({"currency": value["currency"]}
                                    if value.get("currency") else {}),
                                 **({"quote": quote} if quote is not None else {})}
                                for quote in _quotes(value)],
               # GATE-004: a count the gate passed only as a lower bound stays one in
               # the reply ("at least 2"; ``answer_gate.lower_bounds``)
               "check": {"status": "passed",
                         **({"unit_basis": "gate_verified"} if value.get("unit") else
                            {"unit_basis": "unverified"} if at in unchecked else {}),
                         **({"bound": "at_least"} if value.get("bound") == "at_least"
                            else {})}}
              for at, value in enumerate(values)
              if value.get("value") is not None and not isinstance(value["value"], bool)]
    for note in notes:
        if not answer_gate.final_reply(note, claims, unsupported=refused,
                                       question=question)["modified"]:
            gaps.append({"state": "unknown", "subject": note, "reason": NOTE_REASON})
    text = screen(answer.get("text") or "", report["withheld"])
    fence = _JSON_FENCE
    outside = screen(fence.sub(" ", final_text))
    lead_figures(text, outside)
    # a reader sees the prose around the block or, with none, the answer's own text: the
    # line goes in each text that may be shown, never into an empty one
    footnote(text, outside)
    stated = (gate or {}).get("answerability")
    stated = stated.get("state") if isinstance(stated, Mapping) else None
    # A decision that passed nothing says only that (``blocked``): the reply's own
    # answerability stands, and withholding below decides what it becomes.
    stated = stated if stated in ANSWERABILITY and passed else None
    # The less certain of the gate's decision and the answer's own label: the gate never
    # makes an agent's partial or blocked answerable (its own gaps, kept above, say what
    # it could not establish), and makes an answerable one partial. An agent's blocked
    # beside a claim the gate passed is partial: a passed claim is a cited answer, never
    # blocked (decision 2026-10-29). A withheld value still makes it partial
    # (the gate never saw a value never submitted).
    own = answer.get("answerability")
    answerability = max((label for label in (stated, own) if label in CERTAINTY),
                        key=CERTAINTY.index, default=own)
    if answerability == "blocked" and stated is not None:
        answerability = "partial"
    # An unresolved source conflict among the answer's gaps leaves part of it open: an
    # answerable reply carrying one is partial (``answer_gate.unresolved_conflict``).
    if answerability == "answerable" and answer_gate.unresolved_conflict(gaps):
        answerability = "partial"
    # A business state the reply asserted that no passed claim is about was removed
    # (``answer_gate.unbacked_assertions``, GATE-003): what remains is not the whole
    # answer the agent gave.
    if answerability == "answerable" and (text.get("asserted") or outside.get("asserted")):
        answerability = "partial"
    if not (text["modified"] or outside["modified"] or report["withheld"] or dropped
            or values != list(answer.get("values") or [])
            or gaps != list(answer.get("gaps") or [])
            or answerability != own):
        return final_text, report
    if report["withheld"] and not any(v.get("value") is not None for v in values) \
            and not passed and not _said(text["text"]):
        answerability = "blocked"   # nothing backed survives: values withheld, no text
    elif report["withheld"] and answerability == "answerable":
        answerability = "partial"
    gated = {**answer, "answerability": answerability, "text": text["text"],
             "values": values, "gaps": gaps}
    report["rewritten"] = True
    prose = outside["text"].strip() if outside["modified"] else fence.sub(
        "", final_text).strip()
    block = "```json\n" + json.dumps(gated, ensure_ascii=False) + "\n```"
    return (prose + "\n\n" + block) if prose else block, report
