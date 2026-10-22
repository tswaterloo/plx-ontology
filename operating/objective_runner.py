"""The objective runner: one free-form objective, no object id, investigated by the
employee agent loop (``operating.agent_loop.run``) for one reader.

``check`` reads a request (``FIELDS``: ``objective``, ``mode`` one of ``MODES``, an optional
ISO ``deadline``) and refuses anything else (``ValueError`` codes). ``run``:

* **Roots.** The identifiers the objective names (``ontology.identifiers.find_identifiers``,
  at most ``MAX_MENTIONS``) are looked up with the reader's ``plx.find`` (``find``): a
  ``found`` one is a root; an ``ambiguous`` one lists its candidates (at most
  ``MAX_CANDIDATES``) and none is chosen; a ``not_found`` one is a gap with the tool's own
  typed gaps. An objective naming nothing tells the agent to find its records itself.
  A record the agent read (``plx.object``) that a shown claim cites is a root found by the
  agent once the reader's own ``read`` of it returns that cited record.
* **The run.** The objective, the roots and the mode's instructions (``frame``, within
  the loop's question bound) go to ``agent_loop.run`` unchanged: its tools, bounds,
  evidence rule and claim gate. ``shown`` (the caller's filter) keeps the claims the
  reader may see; nothing else of the run's tool results leaves here.
* **Scenario mode** (a forward-looking objective, "air-ship 100k bottles by Nov 15"):
  the agent is told to reuse what records already quote, never to predict. Each value
  claim of a shown claim is an input; its source date is the date its record states it
  was quoted or issued, on or before ``as_of``: a date value claim the agent cited on the
  same citation, or a quote/document date (``_SOURCE_DATE``) of the reader's card for that
  record. Modified, sync and event dates (``_NOT_SOURCE_DATE``: updated_at, shipped_at,
  expected_date, ...) never date an input. It is ``current``, ``stale`` (dated more than
  ``STALE_AFTER_DAYS`` before ``as_of``), ``expired`` / ``expires_before_target`` (a
  validity date, ``_EXPIRY``, before ``as_of`` / the target) or ``unknown`` (no qualifying
  date, or qualifying dates that disagree: ``conflicting_dates``). The target
  date is the request's ``deadline``, else the first date the objective's text states
  (the next such day on or after ``as_of`` when it names no year), else none.
  ``must_confirm`` lists what a person must confirm: every input not current, every
  ambiguous identifier, a target read from text or missing, every unknown the agent left.
* **Follow-ups**, shaped as the console's propose-follow-up control takes them
  (``operator_ui.object_workspace.FOLLOW_UP_FIELDS``), each on a root or an input's
  object with who can answer for its record (``operating.answerers``): proposed only,
  never recorded, approved or sent here; one with no object or no answerer stays a gap.

Read-only: nothing here writes; every value it returns is a shown claim's or the reader's
own tool result's.
"""

from __future__ import annotations

from datetime import date, timedelta
import re
from typing import Any, Callable, Mapping, Sequence

from . import agent_loop, answerers

FIELDS = frozenset({"objective", "mode", "deadline"})
MODES = ("investigate", "scenario")
MAX_OBJECTIVE = 1000
MAX_QUESTION = 2000      # agent_loop.run's question bound
MAX_MENTIONS = 6
MAX_CANDIDATES = 5
MAX_ROOTS = 12
MAX_INPUTS = 40
MAX_FOLLOW_UPS = 10
MAX_TEXT = 500
STALE_AFTER_DAYS = 30
FOLLOW_UP_STATUS = "proposed_not_recorded"
ROOT_LINE = "Root record (plx object id): "
_EXPIRY = re.compile(r"valid_?(until|to|thru|through)|expir", re.IGNORECASE)
# Which date fields say when a value was quoted or issued (``_SOURCE_DATE``), and which
# never do (``_NOT_SOURCE_DATE``: record modification/sync stamps and event dates; it wins).
_SOURCE_DATE = re.compile(r"quot|document|doc_?date|date_?generated|order_?date|posting|"
                          r"invoice_?date|issued?_?(date|on|at)", re.IGNORECASE)
_NOT_SOURCE_DATE = re.compile(r"modif|updat|sync|audit|last_?(changed|edit)|ship|expect|due|"
                              r"eta|promis|deliver|receiv|arriv", re.IGNORECASE)
_DATE_TYPES = frozenset({"date", "datetime", "timestamp"})
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})"
    r"(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?", re.IGNORECASE)

INVESTIGATE_NOTE = (
    "Investigate across sources: read each record above (plx.object, plx.around, "
    "plx.timeline, plx.process, plx.documents) and the records linked to it that bear on "
    "the objective. Cite only what tools returned to you in this run and list every part "
    "of the objective the records do not answer as an unknown.")
SCENARIO_NOTE = (
    "This objective plans ahead: do not predict, estimate or invent any figure. Reuse "
    "the inputs records already quote (landed cost, lead times, supplier quotes, "
    "quantities, prices, dates), each as a value claim on the record that quotes it, "
    "and state that record's dates (quoted, valid until, expected) as date value claims "
    "on the same citation. List each input the plan needs that no record quotes as an "
    "unknown, \"not in the record: <input>\".")


# The parts of an objective the server answers itself for each record its identifiers
# name, beside the agent's run: a plan family (``ontology/plans``) or an answer of the
# record's own page (``object_workspace.object_answers``). ``asked_parts`` lists the parts
# the objective's words ask, in the order first asked; a part no server answer covers for
# a root (or any part when no identifier names a root) is left to the agent.
PARTS = (("margin", re.compile(r"\bmargins?\b", re.IGNORECASE)),
         ("landed_cost", re.compile(r"landed[\s-]*costs?|\blanded\b", re.IGNORECASE)),
         ("late", re.compile(r"\b(late|lateness|overdue|delay(?:ed|s)?|on[\s-]time|behind)\b",
                             re.IGNORECASE)),
         ("missing", re.compile(r"\b(missing|gaps?|lacking|outstanding)\b", re.IGNORECASE)),
         ("supplier", re.compile(r"\b(suppliers?|vendors?|supplies|supplied|ordered from)\b",
                                 re.IGNORECASE)))
PART_PLANS = {"late": "late_po", "landed_cost": "landed_cost", "margin": "order_margin"}
PAGE_PARTS = ("missing", "supplier")


def asked_parts(objective: str) -> list[str]:
    """The parts (``PARTS``) the objective asks, each once, in the order first asked."""
    text = str(objective or "")
    found = [(match.start(), name) for name, pattern in PARTS
             if (match := pattern.search(text))]
    return [name for _, name in sorted(found)]


def check(request: Any) -> dict[str, Any]:
    """The request's objective, mode and deadline (a ``date`` or None); refuses unknown
    fields, an empty or over-long objective, an unknown mode or a deadline that is not an
    ISO date."""
    if not isinstance(request, dict) or set(request) - FIELDS:
        raise ValueError("objective_fields_invalid")
    objective = request.get("objective")
    if not isinstance(objective, str) or not objective.strip() \
            or len(objective) > MAX_OBJECTIVE:
        raise ValueError(f"objective_1_to_{MAX_OBJECTIVE}_chars_required")
    mode = request.get("mode", "investigate")
    if mode not in MODES:
        raise ValueError("objective_mode_invalid")
    deadline = request.get("deadline")
    if deadline is not None:
        if not isinstance(deadline, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
            raise ValueError("objective_deadline_invalid")
        try:
            deadline = date.fromisoformat(deadline)
        except ValueError:
            raise ValueError("objective_deadline_invalid") from None
    return {"objective": objective.strip(), "mode": mode, "deadline": deadline}


def target_date(objective: str, deadline: date | None, as_of: date
                ) -> tuple[date | None, str]:
    """(the target date, where it came from: ``request``, ``objective_text`` or ``none``)."""
    if deadline is not None:
        return deadline, "request"
    found: list[tuple[int, date]] = []
    for match in _ISO.finditer(objective):
        try:
            found.append((match.start(), date(*map(int, match.groups()))))
        except ValueError:
            continue
    for match in _MONTH_DAY.finditer(objective):
        month = _MONTHS.index(match.group(1).lower()[:3]) + 1
        day, year = int(match.group(2)), match.group(3)
        try:
            when = date(int(year) if year else as_of.year, month, day)
            if not year and when < as_of:
                when = date(as_of.year + 1, month, day)
        except ValueError:
            continue
        found.append((match.start(), when))
    return (min(found)[1], "objective_text") if found else (None, "none")


def find_roots(objective: str, find: Callable[[dict[str, Any]], Mapping[str, Any]]
               ) -> dict[str, list[dict[str, Any]]]:
    """The records the objective's identifiers name for the reader (module doc)."""
    from ontology.identifiers import find_identifiers

    roots: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    unfound: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for mention in find_identifiers(objective):
        key = (mention.kind, mention.normalized or mention.text)
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > MAX_MENTIONS:
            unfound.append({"mention": mention.text, "kind": mention.kind,
                            "state": "unknown", "reason": "mentions_bounded",
                            "detail": f"only the first {MAX_MENTIONS} identifiers are "
                                      "looked up"})
            break
        result = find({"query": mention.text[:200], "identifier_kind": mention.kind,
                       "limit": 10})
        if (result.get("error") or {}).get("code") == "invalid_input":
            result = find({"query": mention.text[:200], "limit": 10})
        named = {"mention": mention.text, "kind": mention.kind}
        candidates = [{"object_id": (c.get("object") or {}).get("object_id"),
                       "object_type": (c.get("object") or {}).get("object_type")}
                      for c in result.get("candidates") or []
                      if (c.get("object") or {}).get("object_id")]
        status = result.get("status")
        if status == "found" and len(candidates) == 1:
            roots.append({**named, **candidates[0], "found_by": "identifier"})
        elif status in ("found", "ambiguous") and candidates:
            ambiguous.append({**named, "candidates": candidates[:MAX_CANDIDATES],
                              "more_candidates": max(0, len(candidates) - MAX_CANDIDATES),
                              "chosen": None})
        else:
            error = result.get("error") or {}
            gaps = [{key: gap.get(key) for key in ("state", "reason", "subject", "detail")}
                    for gap in result.get("gaps") or [] if isinstance(gap, Mapping)]
            unfound.append({**named, "state": "unknown",
                            "reason": error.get("code") or "not_found",
                            "gaps": gaps[:5]})
    return {"roots": roots, "ambiguous": ambiguous, "unfound": unfound}


def frame(objective: str, mode: str, found: Mapping[str, Sequence[Mapping[str, Any]]],
          as_of: date, target: date | None) -> str:
    """The loop's question: the objective, what its identifiers name, the mode's
    instructions; root lines are dropped from the end to stay within the loop's bound."""
    head = [objective, "", f"As of {as_of.isoformat()}."]
    if target is not None:
        head.append(f"Target date: {target.isoformat()}.")
    lines = [ROOT_LINE + root["object_id"] for root in found["roots"]]
    for item in found["ambiguous"]:
        lines.append(f"\"{item['mention']}\" names several records, none chosen: "
                     + "; ".join(c["object_id"] for c in item["candidates"])
                     + ". Read each; say which the records support, never pick one.")
    for item in found["unfound"]:
        lines.append(f"\"{item['mention']}\" names no record you may read now.")
    if not found["roots"] and not found["ambiguous"]:
        lines.append("No identifier in the objective named a record: find the records it "
                     "concerns with plx.find.")
    tail = ["", INVESTIGATE_NOTE] + ([SCENARIO_NOTE] if mode == "scenario" else [])
    more: list[str] = []
    while lines:
        text = "\n".join(head + lines + more + tail)
        if len(text) <= MAX_QUESTION:
            return text
        lines.pop()
        more = ["(more records named; not listed)"]
    return "\n".join(head + more + tail)[:MAX_QUESTION]


def run(request: Any, *, find: Callable[[dict[str, Any]], Mapping[str, Any]],
        read: Callable[[str], Mapping[str, Any]], server: Any, planner: Any,
        bounds: Any = None, withholds: Callable[[list[str]], bool] | None = None,
        shown: Callable[[list], list] = list, as_of: date) -> dict[str, Any]:
    """The objective investigated for the reader of ``find`` / ``read`` (``plx.find`` and
    ``plx.object`` as the reader's own tools) and ``server`` (the loop's session)."""
    asked = check(request)
    objective, mode = asked["objective"], asked["mode"]
    target, target_source = target_date(objective, asked["deadline"], as_of)
    found = find_roots(objective, find)
    result = agent_loop.run(frame(objective, mode, found, as_of, target), server, planner,
                            bounds or agent_loop.Bounds(), withholds=withholds)
    claims = shown(result["claims"])
    outcome, reason = result["outcome"], result["reason"]
    if outcome == "answered" and not claims:
        outcome, reason = "abstained", "no_cited_claims"
    cards = _cited_cards(result, claims, read)
    roots = list(found["roots"])
    known = {root["object_id"] for root in roots}
    for object_id, card in cards.items():
        if object_id not in known and len(roots) < MAX_ROOTS:
            known.add(object_id)
            roots.append({"object_id": object_id,
                          "object_type": (card.get("object") or {}).get("object_type"),
                          "found_by": "agent"})
    out: dict[str, Any] = {
        "objective": objective, "mode": mode, "as_of": as_of.isoformat(),
        "target": {"date": target.isoformat() if target else None, "source": target_source},
        "roots": roots, "ambiguous": found["ambiguous"], "unfound": found["unfound"],
        "outcome": outcome, "reason": reason, "claims": claims,
        "withheld": len(result["claims"]) - len(claims), "loop": result}
    inputs = _inputs(claims, cards, as_of, target) if mode == "scenario" else []
    confirm = _must_confirm(out, inputs) if mode == "scenario" else []
    if mode == "scenario":
        out["scenario"] = {"inputs": inputs, "must_confirm": confirm,
                           "stale_after_days": STALE_AFTER_DAYS}
    out["gaps"] = _gaps(out)
    out["follow_ups"] = _follow_ups(out, inputs, cards, target)
    return out


def _cited_cards(result: Mapping[str, Any], claims: Sequence[Mapping[str, Any]],
                 read: Callable[[str], Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """object id -> the reader's own card, for each object the loop read whose card holds
    a record a shown claim cites (the reader's read decides; the loop's never shown)."""
    cited = {handle for claim in claims for handle in claim["citations"]}
    wanted: list[str] = []
    for call in result["tool_calls"]:
        object_id = (call.get("arguments") or {}).get("object_id")
        if call.get("tool") == "plx.object" and not call.get("is_error") \
                and isinstance(object_id, str) and cited & set(call.get("evidence") or ()) \
                and object_id not in wanted:
            wanted.append(object_id)
    cards: dict[str, dict[str, Any]] = {}
    for object_id in wanted[:MAX_ROOTS]:
        card = dict(read(object_id))
        if not card.get("error") and cited & _card_handles(card):
            cards[object_id] = card
    return cards


def _card_handles(card: Mapping[str, Any]) -> set[str]:
    return {(value.get("citation") or {}).get("l1_handle") for value in card.get("values") or []
            if isinstance(value, Mapping)} - {None}


def _day(value: Any) -> date | None:
    text = str(value.get("untrusted_text") if isinstance(value, Mapping) else value or "")
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", text.strip())
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _inputs(claims: Sequence[Mapping[str, Any]], cards: Mapping[str, Mapping[str, Any]],
            as_of: date, target: date | None) -> list[dict[str, Any]]:
    """Each value a shown claim quotes, with its record's source date and state."""
    dated: dict[str, dict[str, date]] = {}     # handle -> field -> date
    owner: dict[str, str] = {}
    for object_id, card in cards.items():
        for value in card.get("values") or []:
            cite = value.get("citation") or {}
            handle = cite.get("l1_handle")
            if not handle:
                continue
            owner.setdefault(handle, object_id)
            field = str(cite.get("field_path"))
            when = (_day(value.get("value")) if value.get("value_type") in _DATE_TYPES and (
                _EXPIRY.search(field) or (_SOURCE_DATE.search(field)
                                          and not _NOT_SOURCE_DATE.search(field)))
                    else None)
            if when is not None:
                dated.setdefault(handle, {})[field] = when
    quoted: list[dict[str, Any]] = []
    for claim in claims:
        for item in claim.get("value_claims") or []:
            handle = item.get("citation")
            when = _day(item.get("value"))
            if when is not None and handle:
                if not _NOT_SOURCE_DATE.search(str(item.get("field"))):
                    dated.setdefault(handle, {})[str(item.get("field"))] = when
            elif handle:
                quoted.append({"value": item.get("value"), "field": item.get("field"),
                               "role": item.get("role"), "unit": item.get("unit"),
                               "citation": handle})
    inputs: list[dict[str, Any]] = []
    for item in quoted[:MAX_INPUTS]:
        dates = dated.get(item["citation"], {})
        expiries = sorted(when for field, when in dates.items() if _EXPIRY.search(field))
        stated = sorted((when, field) for field, when in dates.items()
                        if not _EXPIRY.search(field) and when <= as_of)
        conflicting = len({when for when, _ in stated}) > 1
        source_date, source_field = stated[0] if stated and not conflicting else (None, None)
        expires = expiries[0] if expiries else None
        if expires is not None and expires < as_of:
            state = "expired"
        elif expires is not None and target is not None and expires < target:
            state = "expires_before_target"
        elif source_date is None:
            state = "unknown"
        elif as_of - source_date > timedelta(days=STALE_AFTER_DAYS):
            state = "stale"
        else:
            state = "current"
        inputs.append({**item, "object_id": owner.get(item["citation"]),
                       "source_date": source_date.isoformat() if source_date else None,
                       "source_date_field": source_field,
                       "age_days": (as_of - source_date).days if source_date else None,
                       "expires": expires.isoformat() if expires else None, "state": state,
                       **({"conflicting_dates": [{"field": field, "date": when.isoformat()}
                                                 for when, field in stated]}
                          if conflicting else {})})
    return inputs


def _input_why(item: Mapping[str, Any], as_of: str) -> str:
    if item.get("conflicting_dates"):
        return "its record states different dates for it: " + ", ".join(
            f"{entry['field']} {entry['date']}" for entry in item["conflicting_dates"])
    if item["state"] == "unknown":
        return "no date on its record states when it was quoted or recorded"
    if item["state"] == "stale":
        return (f"its record is dated {item['source_date']} ({item['source_date_field']}), "
                f"{item['age_days']} days before {as_of}")
    if item["state"] == "expired":
        return f"its record's validity ended {item['expires']}, before {as_of}"
    return f"its record's validity ends {item['expires']}, before the target date"


def _must_confirm(out: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]]
                  ) -> list[dict[str, Any]]:
    confirm: list[dict[str, Any]] = []
    for item in inputs:
        if item["state"] != "current":
            confirm.append({"what": f"{item['field']} = {item['value']}",
                            "why": _input_why(item, out["as_of"]), "state": item["state"],
                            "citation": item["citation"]})
    for item in out["ambiguous"]:
        confirm.append({"what": f"which record \"{item['mention']}\" means",
                        "why": f"it names {len(item['candidates'])} records you may read; "
                               "none was chosen", "state": "unknown"})
    target = out["target"]
    if target["source"] == "objective_text":
        confirm.append({"what": f"the target date, read as {target['date']}",
                        "why": "read from the objective's words", "state": "unknown"})
    elif target["source"] == "none":
        confirm.append({"what": "the target date", "why": "the objective states none",
                        "state": "unknown"})
    for unknown in out["loop"]["unknowns"]:
        confirm.append({"what": str(unknown)[:MAX_TEXT], "why": "no record read answers it",
                        "state": "unknown"})
    return confirm


def _gaps(out: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the run could not answer: identifiers not found or ambiguous, the agent's
    unknowns."""
    gaps = [{"kind": "identifier_not_found", "subject": item["mention"],
             "state": item["state"], "reason": item["reason"]} for item in out["unfound"]]
    gaps += [{"kind": "identifier_ambiguous", "subject": item["mention"], "state": "unknown",
              "reason": f"{len(item['candidates'])} candidates, none chosen"}
             for item in out["ambiguous"]]
    gaps += [{"kind": "unknown", "subject": str(unknown)[:MAX_TEXT], "state": "unknown",
              "reason": "no record read answers it"} for unknown in out["loop"]["unknowns"]]
    return gaps


def _who(object_id: str | None, handle: str | None, cards: Mapping[str, Mapping[str, Any]],
         roots: Sequence[Mapping[str, Any]]) -> list[str]:
    if handle:
        return answerers.for_citation(handle)[:10]
    kind = next((root.get("object_type") for root in roots
                 if root["object_id"] == object_id), None)
    if kind is None and object_id in cards:
        kind = (cards[object_id].get("object") or {}).get("object_type")
    return answerers.resolve([(kind, "", "")])[:10] if kind else []


def _follow_ups(out: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]],
                cards: Mapping[str, Mapping[str, Any]], target: date | None
                ) -> list[dict[str, Any]]:
    """Proposed follow-ups (module doc): each input not current, on its object; each
    unknown the agent left, on the first root. Proposed only, never recorded here."""
    priority = "high" if target is not None else "medium"
    first = out["roots"][0]["object_id"] if out["roots"] else None
    wanted: list[tuple[str | None, str | None, str, str, str]] = []
    for item in inputs:
        if item["state"] != "current":
            wanted.append((item["object_id"], item["citation"],
                           f"Confirm {item['field']} = {item['value']} is still current",
                           _input_why(item, out["as_of"]),
                           "unknown" if item["state"] == "unknown" else "stale"))
    for unknown in out["loop"]["unknowns"]:
        wanted.append((first, None, str(unknown), "The objective needs it: "
                       + out["objective"], "unknown"))
    proposed: list[dict[str, Any]] = []
    for object_id, handle, missing, why, state in wanted:
        who = _who(object_id, handle, cards, out["roots"]) if object_id else []
        if not object_id or not who:
            continue   # stays a gap: nothing names the object or who could answer
        proposal = {"object_id": object_id, "missing": missing[:MAX_TEXT],
                    "why": why[:1000], "who_can_answer": who, "state": state,
                    "priority": priority}
        if proposal not in (item["proposal"] for item in proposed):
            proposed.append({"proposal": proposal, "status": FOLLOW_UP_STATUS})
        if len(proposed) >= MAX_FOLLOW_UPS:
            break
    return proposed
