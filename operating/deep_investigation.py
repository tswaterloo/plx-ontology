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
# Tiered (``question_budget``): a lookup of one named record keeps a short session; a
# question that needs several records walked, summed or compared (``is_multi_step``) gets
# the deep one, so a reconciliation is not cut off before its records are read.
# 45 s was too short on a loaded host: a lookup of a name no record holds spent its 27 s of
# research on two finds and was stopped before it could say so (curveball q38, 11-06).
# It is a cap: a lookup answered in 20 s still ends in 20 s.
LOOKUP_SECONDS = 75
DEEP_SECONDS = 180
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
# An answer submitted while research time is left and the gate's decision leaves parts
# of the question open (an unknown, a refused claim, a partial or blocked answerability)
# is sent back once with the open parts (``Budget.after_answer``): measured 2026-11-06 on
# four multi-part questions, every session submitted with 60-90 s of research left and
# each open part still reachable by a link of a record it had read. The decision still
# stands as the answer when the agent finds nothing more (the session's last decision).
CONTINUE_KEY = "continue"
# Twice: a sent-back decision is often answered first by a claim fixed, not a part
# researched (2026-11-06 smoke: the one continuation went on a refused claim's repair).
MAX_CONTINUES = 2
CONTINUE_SECONDS = 25     # research seconds left for a continuation to be worth it
CONTINUE_STEPS = 4        # tool calls left for one
# session seconds left for the mail and next-route send-backs (``Budget.required_parts``):
# a search and a resubmission take about a minute on a loaded host
REQUIRED_PARTS_SECONDS = 60
MAX_SCOPE_ROOTS = 5       # named records a type-wide query is sent to start from
SCOPE_REFUSAL = "scope_from_named"
MAX_OPEN_SHOWN = 8
# A decision that passed claims but whose reply drops sentences of the submitted text (a
# number, date or name no passed claim states: ``answer_gate.final_reply``) goes back once
# with those sentences, research or not, while the session has time for one more
# submission (``TEXT_REPAIR_SECONDS``: composing it and the gate's decision). Measured
# 2026-11-06 (intel-5, 42 questions): 11 replies lost sentences, 6 of them at a decision
# that ended the session, e.g. "SharePoint holds three versions ... 20ml ..." went because
# its count was a lower bound and "20ml" was part of a file name no claim stated, so the
# reply opened on its second bullet.
MAX_TEXT_REPAIRS = 1
TEXT_REPAIR_SECONDS = 25
# The adaptive answer deadline (``Budget.note``): once the research seconds left fall
# under a third of the research window, or under the slowest call this session has made
# (a loaded host's queries took 30-60 s each, measured 2026-11-07 on five hard DEV
# timeouts: two such calls spent the whole research window), each result's note tells the
# agent to start no new part and submit what is established with gaps for the rest, so
# the answer is written before the cap instead of the session stopping mid-read.
WRAP_UP_SHARE = 1 / 3
WRAP_UP_NEXT = ("time is nearly up: start no new part; read only what an open part's "
                "answer needs, then submit each established part with its citation and "
                "each other part as a gap saying what was not checked")



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


def question_budget(question: Any, cap: float | None = None, route: str | None = None
                    ) -> float:
    """The agent session's seconds for ``question``: ``LOOKUP_SECONDS`` when the front
    door read it as a lookup (``operating.front_door``: ``route``), else
    ``DEEP_SECONDS``; never above ``cap`` (the app's hard cap of one Ask, when given). No
    wording of the question decides it: the model's reading does, and without one the
    question gets the deep budget."""
    seconds = float(LOOKUP_SECONDS if route == "lookup" else DEEP_SECONDS)
    return min(seconds, float(cap)) if cap else seconds


# Records before documents (both methods): the figures of a business question live in
# the records and their links; mail and files say why, promise or reply.
RECORDS_FIRST = (
    "Records first, then text. Figures, dates, owners and statuses live in the records "
    "of Sage, FileMaker, Business Central and the Portal and in their links; mail, Teams "
    "and files hold what people wrote. "
    "(a) Resolve: plx.find on each name or code returns entities: every record of that "
    "thing in every system (status match), and related records offered as leads (never "
    "state a lead as the same thing). Work from all of an entity's records: each system "
    "holds a different part of it, so read every record of the entity with plx.object in "
    "one turn before you choose where to walk. "
    "(b) Walk: plx.object on a record lists its links counted per link type, and what "
    "the records linked to it link to in turn (linked_beyond: a line's orders, a "
    "component's purchases); follow the "
    "ones the question needs with plx.around (up to 3 hops per call, filters on the "
    "objects reached, e.g. a date or status; call it again from the set it returns to go "
    "further). To reach a type several links away (a lot's suppliers, an order's "
    "receipts), plx.query traverse [{\"to\": \"<type>\"}] walks every declared route "
    "there from the records you have and says what each reached; a hop that reaches "
    "nothing names the other routes. To keep the records of a related record (a "
    "supplier's purchase orders, a customer's orders) filter on the link, never on a "
    "copied name some systems do not record: {\"property\": \"<link name>\", \"op\": "
    "\"prefix\", \"value\": \"<name>\"} (or eq/in its records or a set) keeps what the link "
    "joins to them in every system; a filter_not_sourced gap names the systems a property "
    "filter left out. Counts, totals and lists rest on confirmed links only: walk "
    "at the default tier_floor. When firm links run out, a walk with tier_floor "
    "\"lead\" sets apart what only lead links reach (a possibly_related gap naming its "
    "set): say those apart as possibly related, never added to a count, total or list; "
    "a lead_basis gap marks a figure resting on leads, claimed with lead true. "
    "(c) Total: plx.query does resolve, walk and total in one call (from an entity, its "
    "records walked one by one; traverse several hops; where; measures; group_by with "
    "group_path for a linked record's date; bucket for a date's year, quarter, month, "
    "week or day; having for the groups whose value holds a condition; window for a "
    "period; every group is computed, a summary covers them all and cursor pages them): "
    "each group lists the "
    "citations of its inputs, so claim a total as {\"op\": \"sum\", \"of\": [each "
    "input's l1_handle as citation and field_path as field]} with its unit and the "
    "claim's name saying what it totals (no role). plx.aggregate sums, "
    "counts, mins or maxes a set (group_by a property, or "
    "a property of a linked record through path, e.g. each invoice line by its invoice's "
    "date for a time window); plx.case_compute does the arithmetic between cited figures "
    "(a difference, a balance, a ratio). A count is plx.aggregate's or plx.query's count over a set (or "
    "object_type with filters: no scan cap), claimed as {\"op\": \"count\", \"of\": the "
    "records it listed}; one it marks incomplete is said as at least N. "
    "(d) Search mail and documents (plx.documents) only for what the records cannot hold, "
    "after the records, or when a figure the question quotes is in no record (then say "
    "where it was found); never in your first turn when the question names a record, a "
    "code or a party (a document search takes far longer than a record read). Run "
    "independent reads in the same turn. A message's attachments are part of what it "
    "says: plx.read of a message lists them, and you read the ones that may bear on the "
    "question by their citations (plx.read of a message with find searches it and its "
    "attachments' text at once: attachment_matches). Prefer the latest record or message "
    "on the subject, "
    "say what it supersedes, and say how recent the newest one you read is. What was "
    "sent rests on the sent copy (or the recipient's), never on a draft. A question "
    "that points at a document without naming it (this, these, the attached) means the "
    "person's most recent one on its subject: find it among their newest mail "
    "attachments and files (plx.documents, newest first), say which one you used (its "
    "name, sender and date), read it whole, and check its rows against the records "
    "(plx.query from codes with per_code and rows answers a sheet's item codes at "
    "once: each code's records walked, what they reach listed with cited values and a "
    "date, its last, lowest and highest; page with code_offset). "
    "A question that names correspondence (an email, message, thread, reply, who said or "
    "wrote, whether anyone contacted a party) or asks why or what holds something up asks "
    "what people wrote: after the first record read, search mail for it (plx.documents "
    "sources [\"graph_mail\"], one call per party it names, by participant when you have "
    "the party's record, and by the subject's words), read the messages that match, and "
    "never answer that part from the records alone. "
    "(e) Do not stop early: before you submit, check each part of the question; while one "
    "is unsettled and the budget note shows time left, take the next link or record that "
    "could settle it. A partial answer is for what no record or document you can open "
    "holds, never for what you did not read."
)


# What every answer owes the question, whatever its kind (both methods; the answer gate
# sends back a decision that skips one while research time is left, ``required_parts``):
# the asked system's figure, the asked total, the searches before an absence, the asked
# stock measure.
ANSWER_DUTIES = (
    "Answer duties. (A) The system decides: when the question names a system (\"in the "
    "Portal\", \"BC says\", \"per FileMaker\") or names a record by the number one "
    "system writes for it, the answer is that system's record and figure, said with its "
    "system in the first sentence (\"In the Portal, 22 ...\"); another system's copy is "
    "context after it, never the answer in its place. Distinct records of one system are "
    "never dismissed as repeated copies: they are counted unless a tool marks them as "
    "duplicates. When two systems' copies of one record disagree, the copy the record's "
    "number names is answered first; of the others, the one with the newest own edit is "
    "current and an older one is said as superseded with its last edit date, never as "
    "the answer. (B) A total asked (total, combined, altogether, in all, overall, sum) "
    "is claimed as a sum (op sum over its inputs from plx.query or plx.aggregate): one "
    "per side or period a comparison names, and the combined figure when the question "
    "asks for one; rows alone are no total. (C) Before a part is called unknown or not "
    "found: the named party's own records are walked (a supplier's or customer's orders, "
    "lines and items; a maker's product also bought through a distributor or reseller is "
    "reached from the item to its purchase lines, whoever the vendor), and mail is "
    "searched for the party and the subject. When the question asks whether anyone wrote "
    "to or heard from a set of parties (these suppliers, those customers), mail is "
    "searched once per party the answer names (plx.documents participant or the party's "
    "name), never one search for all. (D) Stock: \"in stock\", \"on hand\" is the "
    "quantity on hand and \"available\" the quantity free to use (on hand less what is "
    "reserved or allocated), each read from the item's or lot's own quantity property "
    "(plx.schema lists it), as the question words it; a movement, adjustment or "
    "transaction entry is never a stock level, and its largest value is no balance. "
    "(E) Done is read on the record too: a record whose own status or completion field "
    "says it is complete, closed, shipped, received or invoiced is not open, even when "
    "no linked shipment, receipt or invoice was found; cite that field and say the "
    "linked record was not found, never list it as open or not shipped on the missing "
    "link alone. (F) Names: a question asking which or what things (products, items, "
    "customers, suppliers) or who names each by its name or description as its record "
    "holds it, the code beside it; codes alone are no names."
)


# How any question becomes a query (both methods): one walk, its filters and its measure
# in one plx.query call, counted by the tool, checked part by part before the gate.
DECOMPOSE = (
    "Turn the question into one query before you call: (i) the start: the record or "
    "records it names, in the system it names (a question naming a system's record "
    "starts from that record's object_id alone, not from every copy of it; its "
    "\"matching\" records in another system are its own copies there, along its "
    "identity link, never every record of that type there; a record named by the "
    "number one system writes for it, in that system's own form, starts at that "
    "system's record, not at its copy in another system); (ii) the "
    "chain: each \"of the\", \"for the\", \"on the\", \"linked to\" is a hop along a "
    "link (plx.object lists a record's link types; plx.schema the types'); (iii) the "
    "filters, each on the hop whose records it describes (a traverse hop's own where): "
    "a name that mentions a word is contains, over is gt, under is lt, at least gte, "
    "at most lte, a period a gte and lt pair; (iv) the measure: how many is count, a "
    "total is sum, the largest, smallest or latest is max, min or rows (newest first), "
    "a comparison is one query per side, all in the same turn, and what a record's "
    "linked records say (\"what do its lines say?\") is how many there are (a count) "
    "and what they hold that bears on the question; \"on how many X\" counts the "
    "distinct X records, the query ending at the hop that reaches them (a product on 14 "
    "component lines of 7 products is on 7), never the lines or links between. Then "
    "send it as one "
    "plx.query (from, traverse with each hop's where, where, measures) instead of a "
    "call per hop. Independent calls sent in one turn run at once (a few at a time; more "
    "wait their turn, in the order sent), so a question's parts are read side by side, "
    "never one turn after another: send the call that settles the question first and a "
    "text search after it, never a broad text search ahead of a record query. A figure is the tool's, never your own tally: count and total with "
    "plx.query or plx.aggregate, not by reading a list (records reached one by one with "
    "plx.object, plx.around or plx.read are counted by sending that walk, with its "
    "filters, as one plx.query with measures count, or plx.aggregate count over the set "
    "plx.around returned). Dates over a set are set-based the same way: the newest or "
    "oldest message of a record's mail or a channel's or chat's Teams messages, or how "
    "many fall in a period, is one plx.query from that record along its message link "
    "(where on the date for the period, measures max or min on sent_at, received_at or "
    "created_at, rows with that date to name the message), never message by message "
    "with plx.read or plx.object. One record held in several "
    "systems (copies an identity link joins) is one thing; two records of one system "
    "are two, however alike their names: a count over copies shows no value (cross_system_twins), so count one system's records; an "
    "entity's records walked one by one (by_record) are the same thing seen in each "
    "system, never added together: give the figure of the system the question names, "
    "else each system's figure with its system (\"22 in the Portal, 5 in FileMaker\"). Before you walk, restate the "
    "start record and each hop's link type from the question: a set the question scopes "
    "by a record (a task's bucket, an order's customer, a file's members) is reached from "
    "that record along its link, never every record of the type with that word in its "
    "name. When a walk reaches nothing, take the next route before you state an absence: "
    "the other routes the result names (other_routes; a sibling link to the same type, "
    "an item rather than a product), the records' copies in another system (twin_route: "
    "their identity link with tier_floor \"lead\"; plx.query does this itself and marks "
    "the hop via_twin, and what a lead twin reaches is said as a likely match), then a "
    "text or mail search for the start's identifier. Absence is a finding "
    "only when the data shows it: a complete query over the right records that returned "
    "zero or nothing reached, said with what was queried; a refusal, a time-out or an "
    "error proves nothing: correct the call (its hint names valid properties, links or "
    "routes) or narrow it and call again. A name the records spell differently (a typo) "
    "is matched to the nearest name they hold, and the answer says which. Before the "
    "gate, check every part of the question: each has a claimed value or a gap; each "
    "count and total came from a tool; each filter is the question's (over is not at "
    "least; the period's bounds); the start is the record named. A date the question "
    "reads \"as of\" that is at or after the data's end (each source's captured_at) "
    "is now: read without as_known_at, which makes a count only a lower bound. Claim a count as "
    "{\"op\": \"count\", \"value\": N, \"of\": [{\"citation\": each counted record's "
    "handle}]} (a citation alone, no field). A count or figure the gate refuses is "
    "corrected and kept, never dropped: a count the gate calls a lower bound is counted "
    "by a tool before you answer (plx.aggregate count over the set your walk returned, or "
    "the same plx.query with measures count), and said as \"at least N\" only when no "
    "tool can count it; a "
    "record cited twice is cited once, a wrong field is the one the tool named; "
    "\"could not confirm\" is only for a value no tool returned. When a system of record's "
    "records and a document (a sheet, report or mail) both answer, the records' current "
    "values are the answer and the document is supporting evidence, often an older copy: "
    "a status, quantity or party the question asks about now is read from the records "
    "(plx.find lists the record properties whose values the question names under "
    "structured), and a document that disagrees is said as such, with its date. "
    + ANSWER_DUTIES
)


def method_for(question: Any, route: str | None = None) -> str:
    """The method a question is asked with: the short one for a lookup (the front door's
    reading, else one named record by the identifier grammar, ``is_lookup``), the deep one
    otherwise, each stating the question's own budget (``question_budget``: research
    ends at 60 % of it)."""
    seconds = question_budget(question, route=route)
    lookup = route == "lookup" if route else is_lookup(question)
    return method_rule(int(seconds), reserve=round(seconds * 0.4), lookup=lookup)


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
        RECORDS_FIRST,
        DECOMPOSE,
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
        RECORDS_FIRST,
        DECOMPOSE,
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
        "sentence a source holding the answer would contain. A question with several "
        "parts (a comparison's sides, several named records, several figures, who and "
        "where, the list and its count) lists them as its plan's first lines, one record "
        "query each, and your first turn sends each part's query together: the parts are "
        "read at once, a part whose call times out does not hold the others, and each "
        "part ends with its value or its gap.",
        "(2) Your first turn reads records only: plx.find on the names and codes "
        "(an identifier or a name; object_type with filters), plx.object on every record "
        "of what they name (its links counted per link type), plx.query or plx.around "
        "along the links the question needs, plx.timeline for dated events; a plan "
        "(plx.investigate) that fits runs its standard chain in one call. In later turns, "
        "search text for what the records leave open, several independent calls at once: "
        "plx.documents for mail, SharePoint files and originals (a query naming a use case "
        "lists its documents; sources [\"graph_mail\"] searches mail alone; for the last "
        "email with, from or to a party, plx.documents participant (an address, @domain, "
        "or the customer's or supplier's object_id from plx.find; add query or object_id "
        "for one order) lists the mail they sent and the mail we sent them newest first "
        "with its direction: for the last email with them cite the newest of either "
        "direction, never an older message a word search found), and plx.around to the "
        "mail, Teams messages and Mission Control tasks that mention a record. Before you "
        "settle on one source, search at least two ways (different words, identifier "
        "forms or sources): several copies of a document or several records may match, "
        "and the first is not always the one that answers. Read the documents and threads "
        "the question is about whole (the paging rule above): find locates a passage but "
        "shows none of its context, and a table row read alone loses its column headings "
        "and notes.",
        "(3) Keep a checklist: for each sub-question, the evidence found (its handle) or "
        "the searches that found nothing. A search that finds nothing proves no absence: "
        "before you call a sub-question missing, run at least one more targeted search "
        "for it (another wording or identifier form, another source, or a link from a "
        "record you found). Before you settle a sub-question on a document, read the top "
        "document for it whole at least once (page with text_offset until next_offset is "
        "null, or to the read cap): a find hit or a search snippet is not a read. "
        "A sub-question stays open until it is answered or proven unavailable: its "
        "records read, every link type of those records that could hold it followed "
        "(plx.object lists them; a figure a record lacks often sits one or two links "
        "away, on its lines or on the records that made, used, received or billed it), "
        "and two searches made. A total that comes back null or from no inputs answers "
        "nothing: read one of its records to see which property holds the figure or "
        "which link leads to it, then total that. Submit when every sub-question is "
        "answered or proven unavailable, or when research time is up; never earlier.",
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
        "passes it returns tool_timed_out with nothing; narrow it or go on without it "
        "(the exact call again times out again at once: a repeated read is answered from "
        "the session's first result, never run twice). Once the note shows less than a "
        "third of the time left, start no new part: read only what an open part's answer "
        "needs, then submit each part established with its citation and each other part "
        "as a gap that says what was not checked. "
        "When research was cut short (the time limit, a refused or timed-out call), begin "
        "your reply with \"I stopped researching at the time limit; not yet checked: <the "
        "open sub-questions>\". Spend the budget: a question answered in three calls was "
        "rarely searched broadly enough, and research time left is for the open "
        "sub-questions.",
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
# The line before it: what was read to look for the missing part, in words (no figure).
CHECKED = "Checked:"


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
        f"starting \"{CHECKED}\" saying in words what you read to look for the missing "
        "part (the records and links you followed, the mail or files you searched), no "
        f"figure, and one line starting \"{NOT_SETTLED}\" naming what is missing in words, "
        "no figure. An answerable answer has neither line.",
        "A partial answer says plainly what you found (the verdict and bullets) and what "
        "is missing (that line); never only that it cannot be answered.",
        "In your sentences write money, prices and quantities as a reader would, rounded "
        "to 2 decimals (up to 4 for a unit price that needs them): 116.25, never "
        "116.25000000000001; the json block's values stay exactly as the record holds them.",
        "No headings, no bold labels, no closing summary, no suggestions of your own: "
        "the app adds the follow-up questions. Every number in the verdict and the "
        "bullets follows the number rule (a cited or calculated value, else left out).",
        "Write plainly and stop at the answer. Use ordinary words in whole sentences, each "
        "one able to stand alone, because a sentence the gate cannot back is removed. "
        "Never coin a shorthand of your own, such as a figure glued to a word or an "
        "abbreviation the records do not use. Say what each number counts. Do not set a "
        "point against something nobody said (\"not X but Y\"). Do not group things in "
        "threes for rhythm, and do not put two short sentences in a row. Add nothing "
        "after the point: no sentence that explains, sums up or reassures.",
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
    # The catalog hint only for a question that names no identifier: on questions naming
    # an order, PO or document number it sent the agent to maps over the records
    # (measured, 20 cards' messy variants, hint alone: 27/60 correct without, 19/60 with).
    hint = "" if any(any(ch.isdigit() for ch in lead.text) for lead in leads) \
        else use_case_hint(question)
    if hint:
        lines.append(hint)
    if not lines:
        return ""
    return ("Question analysis (the Workbench's query understanding; hints for your plan, "
            "never facts): " + " ".join(lines))


# Use cases a catalog hint lists at most.
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


# The record reads a session makes before any text search (``Budget.decide``), and the
# text search that waits for one: a refusal the agent gets, never a step.
RECORD_TOOLS = frozenset({"plx.find", "plx.object", "plx.around", "plx.query",
                          "plx.aggregate", "plx.timeline", "plx.investigate", "plx.process",
                          "plx.compare", "plx.identity", "plx.read"})
TEXT_SEARCH_TOOLS = frozenset({"plx.documents"})
RECORDS_FIRST_REFUSAL = "records_first"


# Correspondence or a reason a question asks about (what people wrote): wording of the
# language, never of a case; a mail search is then part of the research (``Budget``).
CORRESPONDENCE = re.compile(
    r"\b(?:e-?mail(?:s|ed|ing)?|mailed|thread|correspondence|wrote|written to|"
    r"repl(?:y|ied|ies)|respond(?:ed)?|who said|said (?:that|we|they)|messag(?:e|es|ed)|"
    r"contacted|reached out|why|holding (?:\w+ )?up|held up|reason)\b", re.I)
# A set of parties a correspondence question asks about (``required_parts``: one mail
# search per party, never one for all).
PARTY_SET = re.compile(
    r"\b(?:suppliers|vendors|customers|clients|parties|companies|distributors|carriers|"
    r"manufacturers|them)\b", re.I)
# A total the question asks for (``required_parts``: claimed as a sum), and a comparison
# whose sides each need one; a how-many question's "in total" asks a count, not a sum.
TOTAL_ASKED = re.compile(
    r"\b(?:total|totals|combined|altogether|in all|overall|sum|add(?:ed|s)? up)\b", re.I)
COUNT_ASKED = re.compile(r"\b(?:how many|number of|count of)\b", re.I)
# "than"/"against" compare two periods or sides only before a thing, not before a figure
# ("worth more than 5000", "against 1,200"): a threshold names one side.
COMPARED = re.compile(
    r"\b(?:compared?|comparison|versus|vs\.?)\b"
    r"|\b(?:than|against)\b(?!\s+(?:(?:about|around|roughly|approximately|at least)\s+)?"
    r"[$€£]?\s*\d)", re.I)
# The systems a question or a reply names, by their own words (``asked_system``); a short
# name never matches inside an identifier (MO-FM-5320 names no system).
SYSTEM_WORDS = (
    ("Portal", re.compile(r"\bportal\b", re.I)),
    ("Business Central", re.compile(r"\bbusiness central\b|(?<![\w-])BC(?![\w-])", re.I)),
    ("FileMaker", re.compile(r"\bfile ?maker\b|(?<![\w-])FM(?![\w-])", re.I)),
    ("Sage", re.compile(r"\bsage\b", re.I)),
    ("Mission Control", re.compile(r"\bmission control\b|(?<![\w-])MC(?![\w-])", re.I)))
# Record types of a party, whose own records are walked before an absence is stated.
PARTY_TYPES = re.compile(r"supplier|vendor|customer|party|company|organi[sz]ation|"
                         r"manufacturer|distributor|account", re.I)
# Gap reasons of a walk that reached nothing (plx.query, plx.around).
DEAD_END = re.compile(r'"reason":\s*"(?:nothing_reached|twin_route|other_routes)"')


def searches_mail(name: Any, arguments: Any) -> bool:
    """Whether a call searches or reads mail: plx.documents over mail (no sources named,
    or graph_mail among them), or plx.read of a mail handle."""
    if not isinstance(arguments, Mapping):
        return False
    if name == "plx.documents":
        sources = arguments.get("sources")
        return not sources or (isinstance(sources, list) and "graph_mail" in sources)
    if name == "plx.read":
        return str(arguments.get("citation") or "").startswith("graph-mail://")
    if name in ("plx.around", "plx.query"):
        # a walk to the messages that mention a record
        return '"Email.' in json.dumps(arguments, default=str)
    return False


def systems_named(text: Any) -> list[str]:
    """The systems ``text`` names by their words (``SYSTEM_WORDS``), in that order."""
    if not isinstance(text, str):
        return []
    return [name for name, words in SYSTEM_WORDS if words.search(text)]


def asked_system(question: Any, roots: Sequence[Mapping[str, Any]] = ()) -> str | None:
    """The system whose record answers the question: the one system it names in words;
    else, when it names one record and no system, that record's own system (the number
    one system writes for it names its record there, ``entity_resolution.system_of``).
    None: no system, or several named."""
    named = systems_named(question)
    if named:
        return named[0] if len(named) == 1 else None
    ids = {root.get("object_id") for root in roots if isinstance(root, Mapping)}
    if len(ids) != 1:
        return None
    from operating.entity_resolution import system_of
    system = {"Mail": None, "other": None}.get(found := system_of(str(next(iter(ids)))),
                                                found)
    return system if system in {name for name, _ in SYSTEM_WORDS} else None


def lead_sentence(decision: Mapping[str, Any]) -> str:
    """The first sentence (or line) of a decision's reply text, else of its text."""
    reply = decision.get("reply")
    text = _plain(reply.get("text")) if isinstance(reply, Mapping) else None
    if not isinstance(text, str) or not text.strip():
        text = _plain(decision.get("text"))
    if not isinstance(text, str):
        return ""
    first = text.strip().split("\n", 1)[0]
    return re.split(r"(?<=[.!?])\s+(?=[A-Z])", first, maxsplit=1)[0]


def sums_passed(decision: Mapping[str, Any]) -> int:
    """How many passed value claims of an answered decision state a total: a sum, or an
    expression adding cited figures."""
    from operating.answer_gate import passed_cells
    return sum(1 for cell in passed_cells(decision)
               if cell.get("op") == "sum" or (cell.get("op") == "expression"
                                              and "+" in str(cell.get("expression") or "")))


class Budget:
    """One session's step and time budget, as the tool gate decides it. ``deadline``:
    the wall-clock second research ends (None: no time limit); ``max_steps``: tool calls
    besides the answer gate's (None: no step limit); ``ends_at``: the wall-clock second the
    whole session ends, for the note's seconds left (default: ``deadline``)."""

    def __init__(self, deadline: float | None = None, max_steps: int | None = None,
                 ends_at: float | None = None, repair_reads: int = REPAIR_READS,
                 grace_calls: int = GRACE_CALLS, records_first: bool = False,
                 question: str | None = None,
                 roots: Sequence[Mapping[str, Any]] = ()) -> None:
        self.deadline, self.max_steps = deadline, max_steps
        self.ends_at = ends_at if ends_at is not None else deadline
        self.repair_reads, self.grace_calls = repair_reads, grace_calls
        self.steps = self.refused = self.repairs = self.submissions = self.grace = 0
        self.continues = 0   # decisions sent back with their open parts (``after_answer``)
        self.text_repairs = 0   # decisions sent back for the sentences their reply drops
        # the open parts of the decision last sent back for research (``after_answer``)
        self.sent_open: list[str] | None = None
        self.timed_out = 0   # calls the host stopped at their limit (``call_limits``)
        # a stopped call no research call was forwarded after (``cut_short``)
        self.timeout_open = False
        self.records_read = 0   # record reads forwarded (``RECORD_TOOLS``)
        # the question named records: a text search waits for the first record read
        self.records_first = records_first
        self.waited = 0         # text searches asked to wait for a record read
        # the question (``after_answer``: correspondence it names is searched in mail)
        self.question = question if isinstance(question, str) else ""
        self.mail_searched = False   # a mail search or message read was forwarded
        # the distinct mail searches forwarded (their arguments): one per party a
        # correspondence question names (``required_parts``)
        self.mail_searches: set[str] = set()
        # the named records (object_id) a forwarded record call started from or named
        self.walked: set[str] = set()
        # the step of the last walk that reached nothing (``observe``): a decision with no
        # call forwarded after it tried no other route
        self.dead_end_step: int | None = None
        # the records the question names, as the Ask resolved them (object_id, object_type,
        # same_as, mention): a set of another type is walked from them (``scope_from_named``)
        self.roots = [dict(root) for root in roots if isinstance(root, Mapping)
                      and isinstance(root.get("object_id"), str)][:MAX_SCOPE_ROOTS]
        self.scoped = False   # a type-wide query was sent to start from the named records
        self.began: float | None = None   # the first call's second (the research window)
        self.slowest = 0.0                # the longest call this session made, seconds

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
        if self.began is None:
            self.began = now
        if self.is_answer(name, arguments):
            self.submissions += 1
            return None
        why = self.spent(now)
        if why is None and self.records_first and name in TEXT_SEARCH_TOOLS \
                and not self.records_read:
            # the host serves one call at a time: a text search (up to a minute) ahead of
            # the records would hold every read behind it
            self.waited += 1
            return {"ok": False, "error": RECORDS_FIRST_REFUSAL,
                    "message": ("A text search is served once this session has read a "
                                "record: call plx.find on the names and codes (or "
                                "plx.object, plx.query or plx.around on the records named) "
                                "first, then search text for what the records leave "
                                "open.")}
        if why is None:
            scoped = self.scope_from_named(name, arguments, now)
            if scoped is not None:
                return scoped
            self.steps += 1
            self.timeout_open = False   # research went on after any stopped call
            if name in RECORD_TOOLS:
                self.records_read += 1
                said = json.dumps(arguments, default=str).casefold()
                for root in self.roots:
                    words = [root["object_id"], *(i for i in root.get("same_as") or ()
                                                  if isinstance(i, str))]
                    if isinstance(root.get("mention"), str) and root["mention"].strip():
                        words.append(root["mention"].strip())
                    if any(word.casefold() in said for word in words):
                        self.walked.add(root["object_id"])
            if searches_mail(name, arguments):
                self.mail_searched = True
                if name == "plx.documents":
                    self.mail_searches.add(json.dumps(
                        {k: arguments.get(k) for k in ("query", "participant", "object_id")},
                        sort_keys=True, default=str))
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

    def took(self, seconds: Any) -> None:
        """A forwarded call came back after ``seconds`` (the adaptive deadline's measure
        of how slow this session's calls are)."""
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) \
                and seconds > self.slowest:
            self.slowest = float(seconds)

    def wrapping_up(self, now: float) -> bool:
        """Whether research is in its last stretch (``WRAP_UP_SHARE`` of its window, or
        less than the slowest call took): research time is left but a new part would not
        finish in it."""
        if self.deadline is None or self.spent(now) or self.submissions:
            return False
        left = self.deadline - now
        window = self.deadline - (self.began if self.began is not None else now)
        return left <= max(window * WRAP_UP_SHARE, self.slowest)

    def stopped_call(self) -> None:
        """A call the host stopped at its limit (``call_limits``)."""
        self.timed_out += 1
        self.timeout_open = True

    @property
    def cut_short(self) -> bool:
        """Whether research ended before the agent was done: a call refused for the
        budget, a grace read used, or a call stopped at its limit with no research after
        it. A session whose call stopped early and that went on reading (measured: a
        40 s find in the first turn of a 180 s session, then eight reads and a
        submission with a minute of research left) did not stop at the time limit, and
        its reply never says so: what it left open is in its gaps."""
        return bool(self.refused or self.grace or self.timeout_open)

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
        elif self.wrapping_up(now):
            out["next"] = WRAP_UP_NEXT
        return out

    def scope_from_named(self, name: Any, arguments: Any, now: float
                         ) -> dict[str, Any] | None:
        """A query over every record of a type (``from`` an object_type, its where alone)
        when the question names records of another type the query never mentions: sent
        back once, as the walk from the named records to that type with the same filters
        (11-07 dev 0093: every bucket named "Product" counted, not the named task's own).
        The same call again is served (the question may mean every record of the type);
        never while under ``REQUIRED_PARTS_SECONDS`` of the session is left. None: forward."""
        if self.scoped or name != "plx.query" or not self.roots \
                or not isinstance(arguments, Mapping):
            return None
        start = arguments.get("from")
        if not isinstance(start, Mapping) or set(start) - {"object_type", "where"} \
                or not isinstance(start.get("object_type"), str):
            return None
        end = self.ends_at if self.ends_at is not None else self.deadline
        if end is not None and end - now < REQUIRED_PARTS_SECONDS:
            return None
        kind = start["object_type"]
        said = json.dumps(arguments, default=str).casefold()
        named = []
        for root in self.roots:
            ids = [root["object_id"], *(i for i in root.get("same_as") or ()
                                        if isinstance(i, str))]
            words = ids + [w for w in (root.get("mention"),) if isinstance(w, str) and w]
            if any(word.casefold() in said for word in words):
                return None   # the query is scoped by a named record already
            if root.get("object_type") != kind and not any(
                    i.split(":", 1)[0] == kind for i in ids):
                named.append(root)
        if not named:
            return None
        self.scoped = True
        hop: dict[str, Any] = {"to": kind}
        if start.get("where"):
            hop["where"] = start["where"]
        walk = {"from": {"object_ids": [root["object_id"] for root in named]},
                "traverse": [hop]}
        names = ", ".join(f"{root.get('mention') or root['object_id']} "
                          f"({root.get('object_type') or 'record'})" for root in named)
        return {"ok": False, "error": SCOPE_REFUSAL,
                "message": (f"The question names {names}. A set of {kind} the question "
                            "scopes by a named record is the one reached from it along its "
                            "link, not every record of the type: walk from it first, "
                            f"plx.query {json.dumps(walk, default=str)}. If the question "
                            f"means every {kind} with no tie to the named record, send this "
                            "call again unchanged and it is served."),
                "scoped_walk": walk}

    def observe(self, result: Any) -> None:
        """A served call's result: a walk that reached nothing (``DEAD_END`` among its
        gaps) is remembered, so a decision made with no call after it is sent back once
        (``after_answer``)."""
        try:
            text = result if isinstance(result, str) else json.dumps(result, default=str)
        except (TypeError, ValueError):
            return
        if DEAD_END.search(text):
            self.dead_end_step = self.steps

    def required_parts(self, decision: Mapping[str, Any] | None = None) -> list[str]:
        """Parts any decision leaves open while research may go on: correspondence the
        question names that no mail search read (``CORRESPONDENCE``), or a set of parties
        searched in one mail search (``PARTY_SET``); and a walk that reached nothing with
        no call after it (no other route tried). Of ``decision`` (the gate's): a total
        the question asks that no passed claim sums (``TOTAL_ASKED``, one per side a
        comparison names); a lead sentence answering from another system than the one
        asked (``asked_system``); an abstention or partial answer with the named party's
        records not walked or no mail searched (``ANSWER_DUTIES`` (C))."""
        parts: list[str] = []
        if CORRESPONDENCE.search(self.question) and not self.mail_searched:
            parts.append("the question asks what people wrote, and no mail was searched: "
                         "search mail (plx.documents sources [\"graph_mail\"], one call per "
                         "party and subject it names) and read the messages that match")
        elif CORRESPONDENCE.search(self.question) and PARTY_SET.search(self.question) \
                and len(self.mail_searches) < 2:
            parts.append("the question asks whether anyone wrote to a set of parties, and "
                         "mail was searched once for all of them: search mail once per "
                         "party your answer names (plx.documents sources [\"graph_mail\"] "
                         "with participant, its object_id or address, or its name as "
                         "query) and say for each whether a message was found")
        if isinstance(decision, Mapping):
            parts += self._decision_parts(decision)
        if self.dead_end_step is not None and self.dead_end_step >= self.steps:
            parts.append("a walk reached nothing and no other route was tried: take the "
                         "next route (the other routes or twin_route it names, the "
                         "records' copies in another system with tier_floor lead, a text "
                         "or mail search for the start) before stating an absence")
        return parts

    def _decision_parts(self, decision: Mapping[str, Any]) -> list[str]:
        """The answer duties (``ANSWER_DUTIES``) a gate decision leaves undone."""
        parts: list[str] = []
        answered = decision.get("outcome") == "answered"
        if answered and TOTAL_ASKED.search(self.question) \
                and not COUNT_ASKED.search(self.question):
            wanted = 2 if COMPARED.search(self.question) else 1
            if sums_passed(decision) < wanted:
                parts.append(
                    "the question asks for a total" + (" on each side it compares"
                                                       if wanted > 1 else "")
                    + " and no passed claim states it: total the records with plx.query "
                    "or plx.aggregate (measures sum; group_by or bucket for each period "
                    "or side) and claim each total as {\"op\": \"sum\", \"of\": its "
                    "inputs}, the combined figure too when the question asks for one")
        asked = asked_system(self.question, self.roots)
        if answered and asked:
            lead = systems_named(lead_sentence(decision))
            if lead and asked not in lead:
                parts.append(
                    f"the question asks about the {asked} record, and the answer leads "
                    f"with {', '.join(lead)}: give the {asked} figure first, from the "
                    f"{asked} records (each distinct record counts; none is a repeated "
                    "copy unless a tool says so), said as the " + asked + "'s, and the "
                    "other system's figure after it as context")
        state = _plain((decision.get("answerability") or {}).get("state")) \
            if isinstance(decision.get("answerability"), Mapping) else None
        # an answer that establishes little (abstained, blocked, or partial on a question
        # that is not one count): a count bound "at least N" asks no mail search
        if self.question.strip() and (
                decision.get("outcome") == "abstained" or state == "blocked"
                or (state == "partial" and not COUNT_ASKED.search(self.question))):
            unwalked = [root for root in self.roots
                        if root["object_id"] not in self.walked
                        and PARTY_TYPES.search(str(root.get("object_type")
                                                   or root["object_id"].split(":", 1)[0]))]
            for root in unwalked:
                parts.append(
                    f"part of the answer is open and the records of "
                    f"{root.get('mention') or root['object_id']} were not walked: read "
                    "its record (plx.object) and walk its orders, lines and items "
                    "(plx.query from its object_id), and the items' purchase or sales "
                    "lines whoever the other party is, before stating anything absent")
            if not self.mail_searched:
                parts.append(
                    "part of the answer is open and no mail was searched: search mail "
                    "(plx.documents sources [\"graph_mail\"]) for the parties and the "
                    "subject the question names before stating it unknown")
        return parts

    def after_answer(self, result: Any, now: float) -> dict[str, Any] | None:
        """The note an answer-gate decision carries when the session should go on: the
        decision passed claims (``answered``) but leaves parts of the question open
        (``open_parts``), research time and steps are left for at least one more link
        (``CONTINUE_SECONDS``, ``CONTINUE_STEPS``), and no decision was sent back before
        (``MAX_CONTINUES``); or its reply drops sentences of the submitted text
        (``dropped_text``) while the session has time for one more submission
        (``TEXT_REPAIR_SECONDS``, ``MAX_TEXT_REPAIRS``). The note says ``continue`` and
        lists the open parts and the dropped sentences; the session does not end at this
        decision (``agent_session.GateEnd``) and the next submission replaces it. None:
        the decision ends the session as before."""
        decision = decision_body(result)
        if not isinstance(decision, Mapping) \
                or decision.get("outcome") not in ("answered", "abstained"):
            return None
        research = self.continues < MAX_CONTINUES and self.deadline is not None \
            and self.deadline - now >= CONTINUE_SECONDS \
            and (self.max_steps is None or self.max_steps - self.steps >= CONTINUE_STEPS)
        # An abstained decision established nothing (every claim refused, or none given):
        # with research time left the whole question is still open (measured 11-06: three
        # sessions gave up at 60-85 s of a 180 s budget, a minute and more unspent).
        nothing = decision.get("outcome") == "abstained"
        parts = (open_parts(decision) or (
            ["the whole question: nothing is established yet"] if nothing else [])) \
            if research else []
        session_end = self.ends_at if self.ends_at is not None else self.deadline
        if research and self.continues == 0 and session_end is not None \
                and session_end - now >= REQUIRED_PARTS_SECONDS:
            # once: a route not tried and a mail search not made are open whatever the
            # decision lists (11-07: absences stated after one empty walk, correspondence
            # answered from the records alone), only while the session has the time for
            # them: a send-back never costs the answer (11-07 q42: no answer at the limit)
            parts = list(dict.fromkeys(parts + self.required_parts(decision)))
        if parts and self.sent_open is not None and not settled(self.sent_open, parts):
            # The research the last send-back asked for settled none of its open parts:
            # another round costs a minute for what the last one did not find, so this
            # decision is the answer (the session ends at it, ``agent_session.GateEnd``).
            parts = []
        ends = self.ends_at if self.ends_at is not None else self.deadline
        uncovered, dropped = dropped_text(decision) \
            if self.text_repairs < MAX_TEXT_REPAIRS and ends is not None \
            and ends - now >= TEXT_REPAIR_SECONDS else ([], [])
        if not parts and not dropped:
            return None
        note = self.note(now)
        said: list[str] = []
        if parts:
            self.continues += 1
            self.sent_open = list(parts)
            left = int(self.deadline - now)
            what = ("this decision establishes nothing of the question" if nothing else
                    "this decision leaves the parts listed in open unsettled")
            said.append(
                f"Research is not over: {left} seconds are left and {what}. A part no read "
                "so far settled may be reached another way: the records of a type selected "
                "by a status or a date, the links of records you read, the text of a "
                "message or file you found. Do not write your reply yet. This decision "
                "stays your answer if nothing more is found. For each open part, take the "
                "next record, link or document that could settle it: the links of the "
                "records you already read (plx.object counts them; plx.query walks several "
                "hops from many records at once), the lines and the records that made, "
                "used, received or shipped them, the attachments of a message you found. A "
                "claim refused as off the question needs \"via\". Then submit again with "
                "every claim, the ones passed here included: the later decision replaces "
                "this one.")
        if dropped:
            self.text_repairs += 1
            said.append(
                "Your reply as submitted loses the sentences listed in dropped: each states "
                "something no passed claim states (listed in uncovered: a number, date or "
                "name). Keep each sentence's point: add the cited claim that states it (a "
                "count the gate bounds is written \"at least N\"; a name or title holding "
                "a figure is a text claim of the field that holds it), or write the "
                "sentence without it. Then submit again with every claim, the ones passed "
                "here included" + ("." if parts else "; this decision stays your answer if "
                                    "you do not."))
        note.update({CONTINUE_KEY: True, "open": (parts + [
            f"dropped from the reply: {sentence}"[:200] for sentence in dropped]
        )[:MAX_OPEN_SHOWN], "next": " ".join(said)})
        if dropped:
            note["dropped"] = dropped[:MAX_OPEN_SHOWN]
            note["uncovered"] = uncovered[:MAX_OPEN_SHOWN]
        return note

    def summary(self) -> dict[str, Any]:
        return {"steps_used": self.steps, "max_steps": self.max_steps,
                "refused_after_budget": self.refused, "repair_reads": self.repairs,
                "grace_calls": self.grace, "timed_out": self.timed_out,
                "cut_short": self.cut_short,
                "submissions": self.submissions,
                **({"continues": self.continues} if self.continues else {}),
                **({"text_repairs": self.text_repairs} if self.text_repairs else {})}


def _plain(value: Any) -> Any:
    """A tool value with its ``untrusted_text`` envelope removed."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        return value.get("untrusted_text")
    return value


def decision_body(result: Any) -> Mapping[str, Any] | None:
    """The answer gate's decision in an MCP ``tools/call`` result (its structured
    content, else its one JSON text part), without a budget note; None when unreadable."""
    if not isinstance(result, Mapping) or result.get("isError"):
        return None
    body: Any = result.get("structuredContent")
    if not isinstance(body, Mapping):
        parts = [p for p in result.get("content") or () if isinstance(p, Mapping)]
        if len(parts) != 1 or not isinstance(parts[0].get("text"), str):
            return None
        try:
            body = json.loads(parts[0]["text"])
        except ValueError:
            return None
    body, _note = strip_note(body)
    return body if isinstance(body, Mapping) else None


def dropped_text(decision: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """What an answered decision's reply loses of its submitted text: (the values its
    text check left uncovered, the sentences its reply drops: ``answer_gate.final_reply``'s
    ``dropped``). Nothing dropped: ([], [])."""
    reply = decision.get("reply")
    if decision.get("outcome") != "answered" or not isinstance(reply, Mapping):
        return [], []
    dropped = [text.strip() for item in reply.get("dropped") or ()
               if isinstance(text := _plain(item), str) and text.strip()]
    if not dropped:
        return [], []
    check = decision.get("text_check")
    uncovered = [str(value) for entry in ((check or {}).get("uncovered") or ()
                                          if isinstance(check, Mapping) else ())
                 if isinstance(entry, Mapping)
                 and isinstance(value := _plain(entry.get("value")), (str, int, float))]
    return list(dict.fromkeys(uncovered)), dropped


def settled(before: Sequence[str], after: Sequence[str]) -> bool:
    """Whether a decision with open parts ``after`` settled any of the parts ``before``
    left open: it has fewer. Counted, not matched: the parts are the agent's own words,
    which it rewords between submissions."""
    return len(after) < len(before)


def open_parts(decision: Mapping[str, Any]) -> list[str]:
    """What an ``answered`` decision leaves open, in its own words: each unknown and each
    refused claim (its reason), or the answerability when it is not ``answerable`` and
    names nothing else."""
    parts: list[str] = []
    for unknown in decision.get("unknowns") or ():
        text = _plain(unknown)
        if isinstance(text, str) and text.strip():
            parts.append(text.strip()[:200])
    for refused in decision.get("unsupported_claims") or ():
        if not isinstance(refused, Mapping):
            continue
        text, reason = _plain(refused.get("text")), _plain(refused.get("reason"))
        if isinstance(text, str) and text.strip():
            parts.append((f"refused {text.strip()}"
                          + (f" ({reason})" if isinstance(reason, str) and reason else ""))[:200])
    state = _plain((decision.get("answerability") or {}).get("state")) \
        if isinstance(decision.get("answerability"), Mapping) else None
    if not parts and isinstance(state, str) and state in ("partial", "blocked"):
        parts.append(f"the answer is {state}")
    return list(dict.fromkeys(parts))


def with_answer_note(result: Any, note: Mapping[str, Any]) -> Any:
    """An answer-gate result carrying ``note`` the way the decision is read again
    (``decision_body``, ``agent_session.GateEnd``): in its structured content, else inside
    its one JSON text part; unchanged when it is neither."""
    if not isinstance(result, dict) or result.get("isError"):
        return result

    def told(body: dict) -> dict:
        # a decision sent back: its own instruction (state this as your answer) would
        # contradict the note, so the note's words take its place
        said = body.get("instruction")
        if note.get(CONTINUE_KEY) and isinstance(note.get("next"), str):
            said = ({**said, "untrusted_text": note["next"]} if isinstance(said, Mapping)
                    else note["next"])
        return {**body, **({"instruction": said} if said is not None else {}),
                BUDGET_KEY: dict(note)}
    if isinstance(result.get("structuredContent"), dict):
        body = told(result["structuredContent"])
        content = [{"type": "text", "text": json.dumps(body)}] \
            if isinstance(result.get("content"), list) else result.get("content")
        return {**result, "structuredContent": body, "content": content}
    parts = result.get("content")
    if isinstance(parts, list) and len(parts) == 1 and isinstance(parts[0], Mapping):
        try:
            body = json.loads(parts[0].get("text") or "")
        except ValueError:
            return result
        if isinstance(body, dict):
            return {**result, "content": [{**parts[0], "text": json.dumps(told(body))}]}
    return result


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
