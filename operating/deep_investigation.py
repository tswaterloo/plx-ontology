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
SESSION_SECONDS = 60     # the Ask's whole wall budget (ANSWER_SECONDS); was 150
# Measured (12 of 240 sessions at 105 s / 120 s): the gate had passed the answer and the
# session was cut while the agent wrote its final message. Research now ends 60 s before
# the session, every call 50 s before it: composing the claims (p90 28 s), the gate's
# decisions (p90 9 s) and a short final message fit.
RESERVE_SECONDS = round(SESSION_SECONDS * 60 / 150)   # 60 s of a 150 s session
# Every tool call ends this many seconds before the session's end (``call_limits``: the
# host cuts each call's limit to it), so the agent always has time to submit to the
# answer gate and write its reply before the app's hard cap.
SUBMIT_SECONDS = round(SESSION_SECONDS * 50 / 150)    # 50 s of a 150 s session
MAX_STEPS = 30
# One Ask's whole wall budget, the routes tried before the agent included: the agent's
# session gets what is left of it (``answer_budget``), at least MIN_SESSION_SECONDS.
# Its research and call deadlines keep the proportions measured at a 150 s session
# (research 90 s, calls 100 s: ``deadlines``); a short session's small context composes
# and passes the gate faster (measured 18-27 s per whole agent answer).
ANSWER_SECONDS = SESSION_SECONDS
MIN_SESSION_SECONDS = 40
_RESEARCH_SHARE, _CALLS_SHARE = 90 / 150, 100 / 150


def answer_budget(elapsed: float, session: float | None) -> float | None:
    """The agent session's budget when ``elapsed`` seconds of the Ask are spent: what is
    left of ``ANSWER_SECONDS`` (never under ``MIN_SESSION_SECONDS``), at most
    ``session`` (None: unbudgeted, unchanged)."""
    if session is None:
        return None
    return min(float(session), max(float(MIN_SESSION_SECONDS),
                                   float(ANSWER_SECONDS) - max(0.0, float(elapsed))))


def deadlines(budget: float) -> tuple[float, float]:
    """(research ends, every tool call ends), in seconds from the session's start, for a
    session of ``budget`` seconds: research ends at 60 % of it, every call at 2/3, the
    rest is for submitting to the answer gate and the final reply."""
    budget = float(budget)
    return budget * _RESEARCH_SHARE, budget * _CALLS_SHARE

# Reads still served after the budget is spent, once the agent has submitted to the
# answer gate: re-reading a passage to repair a refused quote.
REPAIR_READS = 3
REPAIR_TOOLS = frozenset({"plx.read"})
# Calls still served after the research time ends, so a session slowed by a loaded
# machine or a slow tool reads what it found before it submits, instead of submitting
# nothing it read (measured: a map found at the deadline and never opened). Only fast
# reads (``GRACE_TOOLS``: plx.read, a page or a find in a text; p99 5 s, limit 20 s),
# and only while one still ends before the submit deadline (``SUBMIT_SECONDS``), so a
# grace call can never push the session past its cap. The step budget has no grace.
GRACE_CALLS = 3
GRACE_TOOLS = frozenset({"plx.read"})
# What the reply says when research was cut short (budget spent, a call stopped at its
# limit), when the agent did not say it itself (``agent_session.gated_run``).
STOPPED_LINE = ("I stopped researching at the time limit; the parts not yet checked are "
                "listed as open.")
STOPPED_WORDS = "stopped researching"
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


# Question intents an open question has (``query_understanding.classify_intent``): a
# reason, a comparison or a rule needs the deep method even when it names one record.
OPEN_INTENTS = frozenset({"why", "compare", "policy"})


def is_lookup(question: Any) -> bool:
    """Whether ``question`` is a lookup of one named record: the identifier grammar reads
    exactly one identifier in it (``query_understanding.identifier_leads``) and its intent
    is not an open one (``OPEN_INTENTS``). A lookup gets the short method
    (``method_rule(lookup=True)``); any other question, and anything the analysis cannot
    read, the deep one."""
    from operating import query_understanding as qu
    if not isinstance(question, str) or not question.strip():
        return False
    try:
        leads = qu.identifier_leads(question)
        intent = qu.classify_intent(question)
    except Exception:  # noqa: BLE001 - unread: the deep method
        return False
    return len(leads) == 1 and intent not in OPEN_INTENTS


def lookup_rule(seconds: int = SESSION_SECONDS, steps: int = MAX_STEPS,
                reserve: int = RESERVE_SECONDS) -> str:
    """The method for a lookup of one named record (``is_lookup``): the record first, its
    plan and links in the same turn, mail and documents only for what the question asks
    of them, then the answer: no written plan and no broad search (measured: a lookup
    run with the deep method took a median 11 turns)."""
    return " ".join([
        "How to answer: the question asks about one named record. Answer it from that "
        "record and what links to it; never guess, and do not give up while a source you "
        "can open may hold the answer.",
        "(1) No written plan: start with your tool calls. Make independent calls together "
        "in one turn: in your first turn, plx.find on the identifier (its forms in the "
        "question analysis) and, when a plan family fits the question (plx.investigate; "
        "its description lists them), plx.investigate on the record (root {\"identifier\": "
        "\"<the identifier>\"}).",
        "(2) In your next turn, read in one go what the question asks of the record you "
        "found: plx.object, plx.around along the links it needs, plx.timeline for its dated "
        "events, plx.documents (sources [\"graph_mail\"] for mail) for what a message or "
        "document says. Where the identifier names several records, say which ones; read "
        "the documents and threads the answer rests on whole (the paging rule above). A "
        "search that finds nothing proves no absence: before calling a value missing, try "
        "one more form or source.",
        "(3) Submit as soon as each value the question asks for is cited or has its gap; do "
        "not search beyond what the question asks. Before the gate, check that each value "
        "appears verbatim in its quote and each quote verbatim in the text a tool returned "
        "for its handle. A claim the gate refuses: re-read its passage, correct the quote or "
        "the citation and submit again.",
        f"(4) Budget: at most {seconds} seconds and {steps} tool calls; tool results carry a "
        + BUDGET_KEY + " note. Once the tools refuse with " + BUDGET_REFUSAL + ", submit "
        "what you have established, never nothing.",
        "(5) Answer what is established: each established part with its citation (partial "
        "when part is missing); each missing piece gets a gap whose subject says what is "
        "missing, what you searched and where it would live.",
    ])


def method_rule(seconds: int = SESSION_SECONDS, steps: int = MAX_STEPS,
                reserve: int = RESERVE_SECONDS, *, lookup: bool = False) -> str:
    """The deep-investigation method, as the task states it (``agent_session``);
    ``lookup``: the short method of a lookup of one named record (``lookup_rule``)."""
    if lookup:
        return lookup_rule(seconds, steps, reserve)
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
        "the mail they sent and the mail we sent them newest first with its direction: for "
        "the last email with them cite the newest of either direction, never "
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
        "record you found). Before you settle a sub-question on a document, read the top "
        "document for it whole at least once (page with text_offset until next_offset is "
        "null, or to the read cap): a find hit or a search snippet is not a read.",
        "(4) Cross-check: where two sources state the same fact (a record and a message, "
        "the same order in two systems, a map and the record it describes), compare them "
        "(plx.compare, or the two values side by side). The system of record decides a "
        "transaction's state and figures; where sources disagree, cite both and add the "
        "conflicting_sources gap.",
        "(5) Critique your draft before the answer gate: each value appears verbatim in "
        "its quote and each quote verbatim in the text a tool returned for its handle "
        "(copy, never paraphrase); each claim cites only a handle you read in this session "
        "(a handle seen in a search result is read with plx.read first); where a use "
        "case's own document (listed in its use_case block) and another copy hold the "
        "same text, read and cite the use case's own document; each sub-question is "
        "answered or has its gap; nothing rests on a file name or your own inference. "
        "Fix what fails, then submit. A "
        "claim the gate refuses: re-read its passage, correct the quote or the citation "
        "(its current version when told so) and submit again; never drop an established "
        "fact for a quote you can fix.",
        f"(6) Budget: about {seconds} seconds and {steps} tool calls for the whole "
        "session. Tool results carry a " + BUDGET_KEY + " note (steps used, seconds left). "
        f"In the last {reserve} seconds the note says research time is up: read only what "
        "you already found that the answer needs (a few reads stay open), then submit; "
        "past that, or once the steps are used, the tools refuse further reads ("
        + BUDGET_REFUSAL + ") and only the answer gate stays open: submit what you have "
        "established then, never nothing. Each call also has a time limit: a call that "
        "passes it returns tool_timed_out with nothing; narrow it or go on without it. "
        "When research was cut short (the time limit, a refused or timed-out call), begin "
        "your reply with \"I stopped researching at the time limit; not yet checked: <the "
        "open sub-questions>\". Spend the budget: a question answered in three calls was "
        "rarely searched broadly enough.",
        "(7) Answer what is established, the answer itself first: the number a how-many "
        "question asks (then what it counts), the list a which or what question asks "
        "(each item and how many), the figures a comparison asks (each side and the "
        "difference), then the evidence. A plan entry whose first step reads \"in words: "
        "...\" (a direction) is said in those words, beside the cited figures it names, in "
        "one sentence. When you found part of the answer, state each "
        "established part with its citation (partial), never only that it cannot be "
        "answered. Each missing piece gets a gap whose subject says precisely what is "
        "missing, what you searched (the sources and the terms) and where it would live "
        "(the system, mailbox, record or document that would hold it). After the gate's "
        "decision, write the reply at once and keep it short (a few sentences and the json "
        "block): the session ends at its time limit whether or not the reply is written. "
        "An answer that "
        "says a source was only partly read, or that any part of the question stays "
        "unchecked, is partial, never answerable; a figure from a map's worked example "
        "is that example's, never the record's.",
        "(8) Lead with the answer itself, in the question's own terms, before any detail, "
        "each part a value claim: a how-many question with its count (plx.find the "
        "object type with filters for the set, then plx.aggregate count over its set "
        "handle: a complete count is the exact figure, its records the count's inputs; a "
        "record a filter cannot reach is a gap, never a smaller count); a what-changed or "
        "what-is-new-since question with plx.changes since that moment (its object types), "
        "listing each changed record or giving their count; a what-is-open or which "
        "question with the records themselves (each one's number and the status that "
        "makes it one); a did-we-get-everything question with the ordered and the "
        "received quantity per item and the difference between them (short or over by "
        "it, an expression over the two cited quantities), then whether that is all.",
    ])


# How the answer reads (``answer_shape_rule``): the verdict, a few figures, what is missing.
MAX_BULLETS = 5
# The line naming what the answer leaves open, as the fast answers word it. Never "what I
# couldn't confirm": on a question whether someone confirmed something it reads as "they
# did not confirm" (an answer that found no confirmation would say the opposite of one
# it simply did not read).
NOT_SETTLED = "Not settled:"


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
        f"starting \"{NOT_SETTLED}\" naming what is missing in words, no figure. "
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
    # The catalog hint only for a question that names no identifier: on questions naming
    # an order, PO or document number it sent the agent to maps over the records
    # (measured, 20 cards' messy variants, hint alone: 27/60 correct without, 19/60 with).
    hint = "" if any(any(ch.isdigit() for ch in lead.text) for lead in leads) \
        else use_case_hint(question)
    if hint:
        lines.append(hint)
    lines += answer_shape_hints(question)
    if not lines:
        return ""
    return ("Question analysis (the Workbench's query understanding; hints for your plan, "
            "never facts): " + " ".join(lines))


# Answer shapes the method names (``answer_shape_hints``): the words a question of that
# shape uses, never a record, party or case.
HOW_MANY = re.compile(r"\b(how many|number of|count of|count the|how much stock is on)\b", re.I)
LATE_WORDS = re.compile(r"\b(late|overdue|past due|behind)\b", re.I)
PO_WORDS = re.compile(r"\b(pos?|p\.o\.?s?|purchase orders?)\b", re.I)
OPEN_ORDERS = re.compile(r"\b(open|outstanding|unreceived|not (?:yet )?received)\b", re.I)
ORDER_WORDS = re.compile(r"\b(pos?|purchase orders?|orders?|sales orders?|sos?)\b", re.I)
RECEIVED_VS_ORDERED = re.compile(
    r"\b(received|receiv\w*|get|got|came in)\b.*\b(ordered|order(?:ed)? qty)\b"
    r"|\b(ordered)\b.*\b(received|get|got|came in)\b|\bshort(?:fall|ed|age)?\b", re.I)
# A set of records in a state, asked in the plural (quarantined lots, batches on hold):
# its size is part of the answer.
STATE_SET = re.compile(r"\b(quarantin\w*|on (?:qc )?hold|held up|blocked|expir\w*|late|"
                       r"overdue|past due)\b", re.I)
PLURAL_RECORDS = re.compile(r"\b(lots|batches|pos|purchase orders|orders|receipts|"
                            r"invoices|shipments|items)\b", re.I)
COUNT_HINT = (
    "It asks how many (or which, a set whose size is part of the answer): state the "
    "number first, then what it counts. Count with an exact "
    "count of what you may read, never by paging a list: late purchase orders are Home's "
    "late list (plx.find query \"which POs are late\": its late-order count, every late "
    "order, cite the listed ones); anything else is plx.aggregate with object_type and "
    "filters (plx.find's) and measure {\"op\": \"count\", \"property\": a property "
    "every member carries} (no scan cap). Submit it as a count claim {\"op\": \"count\", "
    "\"of\": records the tool listed}; a count the tool marks incomplete is \"at least N\".")
OPEN_ORDERS_HINT = (
    "It asks for a party's open orders: list each open order's number first and say how "
    "many (plx.find object_type with filters on the party and the status: its candidates "
    "and completeness.total, or plx.aggregate's count over the same filters), each order "
    "cited.")
RECEIVED_HINT = (
    "It compares received with ordered: run plx.investigate three_way_match (or "
    "plx.process purchase_to_invoice with include_groups) and state per item first the "
    "ordered and received quantities and the shortfall (its quantity check's sums and "
    "difference, in its unit), each figure cited; the shortfall an expression claim over "
    "the cited ordered and received values.")


def answer_shape_hints(question: str) -> list[str]:
    """What the answer leads with for the shape the question reads as (a count, a party's
    open orders, received against ordered), from its own words only: hints, never facts.
    Empty for any other question and for anything that is not text."""
    if not isinstance(question, str) or not question.strip():
        return []
    out = []
    if HOW_MANY.search(question) or (STATE_SET.search(question)
                                      and PLURAL_RECORDS.search(question)):
        out.append(COUNT_HINT)
    if OPEN_ORDERS.search(question) and ORDER_WORDS.search(question) \
            and not HOW_MANY.search(question):
        out.append(OPEN_ORDERS_HINT)
    if RECEIVED_VS_ORDERED.search(question):
        out.append(RECEIVED_HINT)
    return out


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
        self.timed_out = 0   # calls the host stopped at their limit (``call_limits``)

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
                and name in GRACE_TOOLS and self._grace_fits(now):
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

    def _grace_fits(self, now: float) -> bool:
        """Whether a grace read still ends before the submit deadline."""
        from operating.call_limits import MIN_SECONDS
        return self.ends_at is None or now < self.ends_at - SUBMIT_SECONDS - MIN_SECONDS

    @property
    def cut_short(self) -> bool:
        """Whether research ended before the agent was done: a call refused for the
        budget, a grace read used, or a call stopped at its limit."""
        return bool(self.refused or self.grace or self.timed_out)

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
                self.grace < self.grace_calls and self._grace_fits(now):
            left = self.grace_calls - self.grace
            out["next"] = (f"research time is up: at most {left} more plx.read call"
                           + ("" if left == 1 else "s") + " of what you already found, "
                           "then submit to the answer gate")
        elif self.spent(now):
            out["next"] = "submit your answer to the answer gate now"
        return out

    def summary(self) -> dict[str, Any]:
        return {"steps_used": self.steps, "max_steps": self.max_steps,
                "refused_after_budget": self.refused, "repair_reads": self.repairs,
                "grace_calls": self.grace, "timed_out": self.timed_out,
                "cut_short": self.cut_short,
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
