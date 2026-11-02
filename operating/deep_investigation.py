"""Deep investigation: how the answering agent works a messy question, and the budget it
works within. The card scoreboard and the app's Ask both run it (``agent_session``:
``task_prompt`` carries the method and the question analysis; the tool gate enforces
the budget), so they answer alike.

* ``method_rule``: the method the agent follows: plan (sub-questions, the sources that
  would settle each, clean rewrites of the question: identifier forms, synonyms, an
  expected-answer query), search broadly across every source type and follow links,
  keep a checklist, cross-check sources against the system of record, critique the draft
  before the answer gate, search again before calling anything missing, and answer what
  is established with each missing piece stated precisely.
* ``question_analysis``: the Workbench's query understanding of the question, the parts
  that need no index or reader (``operating.query_understanding``: the identifier
  grammar's readings with the forms to search, the time frame against today, the intent):
  hints the agent's plan builds on, never facts.
* ``Budget``: the session's step and time budget, decided in the tool gate
  (``agent_session.serve_gate``). Each served call's result carries a short
  ``BUDGET_KEY`` note (steps used, seconds left); once the research time or the step
  budget is spent, every further read is refused with ``BUDGET_REFUSAL`` (a few reads stay
  open after a gate submission, to repair a refused quote), so the agent submits what it
  established instead of being cut off without an answer. The answer gate itself is never
  refused. ``strip_note`` takes the note off a result again for the transcript.

Stdlib only; no read of any source.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Mapping, Sequence

# The session's budget: seconds from the start of the session to its answer, of which
# the last RESERVE_SECONDS are kept for submitting to the answer gate (a submission, at
# most one repair, the final message: 25-45 s measured), and the tool calls it may make
# besides the answer gate's.
SESSION_SECONDS = 150
RESERVE_SECONDS = 45
MAX_STEPS = 30
# Reads still served after the budget is spent, once the agent has submitted to the
# answer gate: re-reading a passage to repair a refused quote.
REPAIR_READS = 3
REPAIR_TOOLS = frozenset({"plx.read"})
# Calls still served after the research time ends (before the session's end), so a
# session slowed by a loaded machine or a slow tool reads what it found before it
# submits, instead of submitting nothing it read (measured: a map found at the deadline
# and never opened). The step budget has no grace.
GRACE_CALLS = 3
# Turns a budgeted session may take (each turn makes one or more calls): above the step
# budget, so the budget, not the turn cap, ends the research.
MAX_TURNS = 45

BUDGET_KEY = "workbench_budget"
BUDGET_REFUSAL = "budget_spent"
ANSWER_TOOL = "plx.case_get"

INTENT_SOURCES = {
    "status": "the record's current state in its system of record (Business Central or "
              "the Portal), then the latest mail, Teams messages and tasks naming it",
    "why": "the record's timeline and the mail, Teams messages, tasks and notes around it "
           "that give a reason; a reason is stated only where a source states it",
    "who": "the owner, buyer, contact or assignee fields on the records and the senders "
           "and recipients of the messages about them",
    "how_much": "the amounts on the records (order and invoice lines, receipts) and the "
                "prices quoted in mail or documents",
    "compare": "each side from its own source, then the two compared",
    "find_document": "the document itself, read whole",
    "policy": "the use-case, process or policy documents, read whole",
    "timeline": "the record's dated events (plx.timeline) and dated messages",
}


def method_rule(seconds: int = SESSION_SECONDS, steps: int = MAX_STEPS,
                reserve: int = RESERVE_SECONDS) -> str:
    """The deep-investigation method, as the task states it (``agent_session``)."""
    return " ".join([
        "How to investigate: work as a careful analyst who answers from the sources, never "
        "guesses, and does not give up while a source you can open may hold the answer.",
        "(1) Plan first, in a few lines before your first tool call: the question restated "
        "cleanly; its sub-questions (each fact or value the answer needs); for each, the "
        "records, documents and messages that would settle it and the system that owns it "
        "(a transaction's status, dates, quantities and amounts: its Business Central or "
        "Portal record; a decision, reason, promise or reply: the mail, Teams message or "
        "Mission Control task that states it; a process rule or a use case's facts: that "
        "use case's own documents). Then several clean search queries: each identifier in "
        "its likely forms, each company or product by its full and its short name, "
        "synonyms of the key words (late: overdue, delayed, ETA, expected receipt; "
        "pricing: price, quote, cost), and one expected-answer query written as the "
        "sentence a source holding the answer would contain.",
        "(2) Search broadly, several independent calls in one turn: plx.find for records "
        "(an identifier or a name; object_type with filters), plx.documents for mail, "
        "SharePoint files and originals (a query naming a use case lists its documents; "
        "sources [\"graph_mail\"] searches mail alone; for the last email with, from or to "
        "a party, plx.documents participant (an address, @domain, or the customer's or "
        "supplier's object_id from plx.find; add query or object_id for one order) lists "
        "their mail newest first with its direction: cite the newest match's handle, never "
        "an older message a word search found), and for each record that matters "
        "plx.object (its links counted per link type), plx.around along those links "
        "(receipts, invoices, and the mail, Teams messages and Mission Control tasks that "
        "mention it) and plx.timeline (its dated events). Before you settle on one source, "
        "search at least two ways (different words, identifier forms or sources): several "
        "copies of a document or several records may match, and the first is not always "
        "the one that answers. Read the documents and threads the question is about whole "
        "(the paging rule above): find locates a passage but shows none of its context, "
        "and a table row read alone loses its column headings and notes. A "
        "plan (plx.investigate) that fits the question runs its standard chain in one "
        "call.",
        "(3) Keep a checklist: for each sub-question, the evidence found (its handle) or "
        "the searches that found nothing. A search that finds nothing proves no absence: "
        "before you call a sub-question missing, run at least one more targeted search "
        "for it (another wording or identifier form, another source, or a link from a "
        "record you found).",
        "(4) Cross-check: where two sources state the same fact (a record and a message, "
        "the same order in two systems, a map and the record it describes), compare them "
        "(plx.compare, or the two values side by side). The system of record decides a "
        "transaction's state and figures; where sources disagree, cite both and add the "
        "conflicting_sources gap.",
        "(5) Critique your draft before the answer gate: each value appears verbatim in "
        "its quote and each quote verbatim in the text a tool returned for its handle "
        "(copy, never paraphrase); each sub-question is answered or has its gap; nothing "
        "rests on a file name or your own inference. Fix what fails, then submit. A "
        "claim the gate refuses: re-read its passage, correct the quote or the citation "
        "(its current version when told so) and submit again; never drop an established "
        "fact for a quote you can fix.",
        f"(6) Budget: about {seconds} seconds and {steps} tool calls for the whole "
        "session. Tool results carry a " + BUDGET_KEY + " note (steps used, seconds left). "
        f"In the last {reserve} seconds the note says research time is up: read only what "
        "you already found that the answer needs (a few reads stay open), then submit; "
        "past that, or once the steps are used, the tools refuse further reads ("
        + BUDGET_REFUSAL + ") and only the answer gate stays open: submit what you have "
        "established then, never nothing. Spend the budget: a question answered in three "
        "calls was rarely searched broadly enough.",
        "(7) Answer what is established. When you found part of the answer, state each "
        "established part with its citation (partial), never only that it cannot be "
        "answered. Each missing piece gets a gap whose subject says precisely what is "
        "missing, what you searched (the sources and the terms) and where it would live "
        "(the system, mailbox, record or document that would hold it).",
    ])


# How the answer reads (``answer_shape_rule``): the verdict, a few figures, what is missing.
MAX_BULLETS = 5
COULD_NOT_CONFIRM = "What I couldn't confirm:"


def answer_shape_rule() -> str:
    """How the answer's "text" (and the words around the json block) reads: a sharp
    analyst briefing an executive. It shapes only the wording: every figure in it is still
    a value the number rule and the answer gate check, so the shape adds none."""
    return " ".join([
        "How your answer reads: like a sharp analyst briefing the CEO, never a wall of "
        "text.",
        "Its first line is the verdict: the direct answer to the question in one sentence, "
        "yes or no first when the question asks one. The verdict carries no figure you "
        "computed yourself (days late, a difference, a total): only a figure one of your "
        "cited values states, or none at all, in words instead (\"Yes, it's late: nothing "
        "has been received since the expected receipt date.\"); the figures go in the "
        "bullets. A verdict with an unchecked figure is removed and the answer opens "
        "without one.",
        f"Then 2 to {MAX_BULLETS} bullets, one per line, each starting \"- \": one key "
        "figure or fact each, a few words, the record it comes from named by its number "
        "or title. Each bullet states one of your cited values or claims, nothing else. "
        "In the json block's \"text\" the lines are joined by \\n.",
        f"Then, only when the answer is partial (a gap the question asks about), one line "
        f"starting \"{COULD_NOT_CONFIRM}\" naming what is missing in words, no figure. "
        "An answerable answer has no such line.",
        "A partial answer says plainly what you found (the verdict and bullets) and what "
        "is missing (that line); never only that it cannot be answered.",
        "In your sentences write money, prices and quantities as a reader would, rounded "
        "to 2 decimals (up to 4 for a unit price that needs them): 116.25, never "
        "116.25000000000001; the json block's values stay exactly as the record holds them.",
        "No headings, no bold labels, no closing summary, no suggestions of your own: "
        "the app adds the follow-up questions. Every number in the verdict and the "
        "bullets follows the number rule (a cited or calculated value, else left out).",
    ])


def _today(pinned: str, today: dt.date | None) -> tuple[dt.date | None, bool]:
    """(the day the question is asked on, whether it is today): a pinned question's day
    is its pin's (``Answer as known at <time>``); an unpinned one's is today."""
    if today is not None:
        return today, True
    found = re.search(r"\b(\d{4})-(\d{2})-(\d{2})", pinned or "")
    if found:
        try:
            return dt.date(*map(int, found.groups())), False
        except ValueError:
            return None, False
    return None, False


def question_analysis(question: str, pinned: str = "",
                      today: dt.date | None = None) -> str:
    """The query understanding's reading of ``question`` that needs no index and no
    reader (``operating.query_understanding``): the identifiers the grammar reads in it
    with the forms to search, the time frame against the day it is asked on, and the
    intent with the sources that usually settle it. Hints, never facts: every form is a
    lead the agent resolves through its tools. ``today``: the day an unpinned question is
    asked on; a pinned one's day is read from ``pinned``. Empty when nothing is read."""
    from operating import query_understanding as qu

    lines: list[str] = []
    day, is_today = _today(pinned, today)
    if day is not None and is_today:
        lines.append(f"Today is {day.isoformat()}.")
    try:
        leads = qu.identifier_leads(question)
    except Exception:  # noqa: BLE001 - an analysis hint; the agent resolves on its own
        leads = []
    for lead in leads[:6]:
        forms = [f for f in dict.fromkeys(lead.forms) if f][:6]
        kind = ("read as a " + _words(lead.prefer)) if lead.prefer else \
            ("possibly a " + " or ".join(_words(t) for t in lead.types[:3])) if lead.types \
            else "an identifier-shaped word"
        lines.append(f"\"{lead.text}\": {kind}{'' if lead.exact else ' (a lead only)'}; "
                     f"search the forms {', '.join(forms)}.")
    try:
        frame = qu.parse_time_frame(question, day) if day is not None else None
    except Exception:  # noqa: BLE001
        frame = None
    if frame and frame.get("start"):
        end = frame.get("end")
        lines.append(f"\"{frame.get('text')}\" is the time frame from {frame['start']}"
                     + (f" up to (not including) {end}" if end else "") + ".")
    try:
        intent = qu.classify_intent(question)
    except Exception:  # noqa: BLE001
        intent = "other"
    if intent in INTENT_SOURCES:
        lines.append(f"It reads as a {intent.replace('_', ' ')} question: usually settled "
                     f"by {INTENT_SOURCES[intent]}.")
    lines += question_kind_hints(question)
    hint = use_case_hint(question)
    if hint:
        lines.append(hint)
    if not lines:
        return ""
    return ("Question analysis (the Workbench's query understanding; hints for your plan, "
            "never facts): " + " ".join(lines))


# Question kinds whose standard route the method names (``question_kind_hints``): the words
# a question of that kind uses, never a record, party or case.
PRICE_MOVE = re.compile(
    r"\b(trend\w*|up or down|going (up|down)|went (up|down)|gone (up|down)|paying (more|less)"
    r"|more or less|increas\w*|decreas\w*|rising|falling|dropp\w*|cheaper|pricier)\b", re.I)
PRICE_WORD = re.compile(r"\b(price\w*|cost\w*|paying|paid|pay)\b", re.I)
CORRESPONDENCE = re.compile(
    r"\b(e-?mails?|mails?|msgs?|messages?|replied|reply|replies|heard|correspondence)\b", re.I)
# what a party said or confirmed: only with the party named as a kind of party
PARTY_SAID = re.compile(r"\b(said|wrote|confirm\w*)\b", re.I)
PARTY = re.compile(r"\b(vendors?|suppliers?|customers?|clients?)\b", re.I)
PRICE_HINT = (
    "It asks how a purchase price moves: run plx.investigate family price_history on the "
    "item (its posted receipt, invoice and order prices, each dated and cited, and the trend "
    "of each). State the direction in words (went up, went down, unchanged) beside the last "
    "and previous prices it cites, each a value with its citation, unit and currency; an "
    "item card's or a supplier list's cost is no price paid and no trend.")
CORRESPONDENCE_HINT = (
    "It asks what a message said or whether one exists: search mail (plx.documents, sources "
    "[\"graph_mail\"]) by each form of the record's number (with and without # or a space, "
    "and its numbers in the other systems) and by the party's name, its mail domain and its "
    "contacts' addresses, then read the latest message from or to the party. State a "
    "message by citing it: a value on its graph-mail handle (row.subject with its quote, or "
    "a quote of row.body_text) and its date. Say that no message or no confirmation is on "
    "record only after those searches found none, naming what you searched.")


def question_kind_hints(question: str) -> list[str]:
    """The standard route of a question kind the question reads as (a purchase price's
    movement; a message, reply or confirmation), from its own words only: hints, never
    facts. Empty for any other question, and for anything that is not text."""
    if not isinstance(question, str) or not question.strip():
        return []
    out = []
    if PRICE_MOVE.search(question) and (PRICE_WORD.search(question)
                                        or re.search(r"\btrend", question, re.I)):
        out.append(PRICE_HINT)
    if CORRESPONDENCE.search(question) or (PARTY_SAID.search(question)
                                           and PARTY.search(question)):
        out.append(CORRESPONDENCE_HINT)
    return out


MAX_CATALOG_HINT = 20


def use_case_hint(question: str, catalog: Any = None) -> str:
    """For a question that names no use case: the use-case catalog's ids and titles
    (``operating.use_case_catalog``: the business processes whose maps and briefs the
    Workbench holds) and how to list one's documents. A messy question rarely says "in
    the <x> use case" (measured: all 46 messy misses of 120 had dropped the name their
    original card gave, and the map that answers several was never listed). The catalog's own words only, never a document's
    title or a source's text: the host returns these titles for any named use case and
    still gates every document per reader. Empty when the question names one (the host
    lists its documents) or the catalog cannot be read (no hint, never a guess)."""
    from operating import use_case_catalog as ucc
    try:
        catalog = catalog if catalog is not None else ucc.load()
        named, _unknown = catalog.named(question)
    except Exception:  # noqa: BLE001 - the catalog fails closed: no hint
        return ""
    if named or not catalog.use_cases:
        return ""
    listed = "; ".join(f"{case.id} ({case.title})"
                       for case in catalog.use_cases[:MAX_CATALOG_HINT])
    return ("The question names no use case. The Workbench's use-case catalog (business "
            f"processes whose maps and briefs it holds): {listed}. A question about a "
            "process, a rule, a plan, a proposal, a map or a record such a document "
            "discusses may be about one of them without naming it: when one fits, search "
            "plx.documents with \"<id> use case\" to list its documents and read the ones "
            "that bear on the question, besides the records.")


def _words(object_type: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", str(object_type)).lower()


class Budget:
    """One session's step and time budget, as the tool gate decides it. ``deadline``:
    the wall-clock second research ends (None: no time limit); ``max_steps``: tool calls
    besides the answer gate's (None: no step limit); ``ends_at``: the wall-clock second the
    whole session ends, for the note's seconds left (default: ``deadline``)."""

    def __init__(self, deadline: float | None = None, max_steps: int | None = None,
                 ends_at: float | None = None, repair_reads: int = REPAIR_READS,
                 grace_calls: int = GRACE_CALLS) -> None:
        self.deadline, self.max_steps = deadline, max_steps
        self.ends_at = ends_at if ends_at is not None else deadline
        self.repair_reads, self.grace_calls = repair_reads, grace_calls
        self.steps = self.refused = self.repairs = self.submissions = self.grace = 0

    @property
    def active(self) -> bool:
        return self.deadline is not None or self.max_steps is not None

    @staticmethod
    def is_answer(name: Any, arguments: Any) -> bool:
        """A submission to the answer gate (``plx.case_get`` answer mode): never
        budgeted."""
        return name == ANSWER_TOOL and isinstance(arguments, Mapping) and "answer" in arguments

    def spent(self, now: float) -> str | None:
        """Why research is over (``time`` or ``steps``), or None."""
        if self.deadline is not None and now >= self.deadline:
            return "time"
        if self.max_steps is not None and self.steps >= self.max_steps:
            return "steps"
        return None

    def decide(self, name: Any, arguments: Any, now: float) -> dict[str, Any] | None:
        """None: forward the call (counted as a step unless it is a submission); else the
        refusal the agent gets. Fails closed on nothing: an unbudgeted gate forwards."""
        if not self.active:
            return None
        if self.is_answer(name, arguments):
            self.submissions += 1
            return None
        why = self.spent(now)
        if why is None:
            self.steps += 1
            return None
        if why == "time" and not self.submissions and self.grace < self.grace_calls \
                and (self.ends_at is None or now < self.ends_at):
            # research time is up, the session's is not: a last few reads, then submit
            self.grace += 1
            self.steps += 1
            return None
        if self.submissions and name in REPAIR_TOOLS and self.repairs < self.repair_reads:
            self.repairs += 1
            self.steps += 1
            return None
        self.refused += 1
        return {"ok": False, "error": BUDGET_REFUSAL, "spent": why,
                "message": ("The investigation budget is spent (" + (
                    "research time" if why == "time" else f"{self.max_steps} tool calls")
                    + "). Submit your answer to the answer gate (plx.case_get with "
                    "answer) now with what you have established: state each established "
                    "part with its citation and give each open sub-question a gap that "
                    "says what you searched and where it would live." + (
                        "" if self.submissions else " After a submission, up to "
                        f"{self.repair_reads} reads stay open to repair a refused quote."))}

    def note(self, now: float) -> dict[str, Any]:
        """The note a served call's result carries."""
        out: dict[str, Any] = {"steps_used": self.steps}
        if self.max_steps is not None:
            out["max_steps"] = self.max_steps
        if self.deadline is not None:
            out["research_seconds_left"] = max(0, int(self.deadline - now))
        if self.ends_at is not None:
            out["session_seconds_left"] = max(0, int(self.ends_at - now))
        if self.spent(now) == "time" and not self.submissions and \
                self.grace < self.grace_calls:
            left = self.grace_calls - self.grace
            out["next"] = (f"research time is up: at most {left} more read"
                           + ("" if left == 1 else "s") + " of what you already found, "
                           "then submit to the answer gate")
        elif self.spent(now):
            out["next"] = "submit your answer to the answer gate now"
        return out

    def summary(self) -> dict[str, Any]:
        return {"steps_used": self.steps, "max_steps": self.max_steps,
                "refused_after_budget": self.refused, "repair_reads": self.repairs,
                "grace_calls": self.grace,
                "submissions": self.submissions}


def with_note(result: Any, note: Mapping[str, Any]) -> Any:
    """An MCP ``tools/call`` result carrying the budget note: in its ``structuredContent``
    (what Claude Code shows the model when present) and else as one more text block. A
    result that is not a tool result, or an error, is returned unchanged."""
    if not isinstance(result, dict) or result.get("isError"):
        return result
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return {**result, "structuredContent": {**structured, BUDGET_KEY: dict(note)}}
    content = result.get("content")
    if isinstance(content, list):
        return {**result, "content": content + [{"type": "text", "text": json.dumps(
            {BUDGET_KEY: dict(note)})}]}
    return result


def strip_note(body: Any) -> tuple[Any, dict[str, Any] | None]:
    """(a tool result's body without the budget note, the note or None): the body as the
    stream gives it (a parsed object, a string, or a list of content parts)."""
    if isinstance(body, dict) and isinstance(body.get(BUDGET_KEY), dict):
        rest = {key: value for key, value in body.items() if key != BUDGET_KEY}
        return rest, dict(body[BUDGET_KEY])
    if isinstance(body, list):
        kept, note = [], None
        for part in body:
            text = part.get("text") if isinstance(part, Mapping) else None
            if isinstance(text, str) and text.startswith("{\"" + BUDGET_KEY + "\""):
                try:
                    found = json.loads(text).get(BUDGET_KEY)
                except (ValueError, AttributeError):
                    found = None
                if isinstance(found, dict):
                    note = found
                    continue
            kept.append(part)
        return kept, note
    return body, None


def budget_summary(lines: Sequence[str]) -> dict[str, Any] | None:
    """The gate's budget log (one JSON line per decision, ``serve_gate``) as the run's
    budget report: the last summary line, or None when the gate kept no budget."""
    last = None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            last = entry
    return last
