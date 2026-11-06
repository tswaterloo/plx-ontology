"""The Ask's live progress in plain words, and its first-line verdict.

The agent path takes about twenty seconds; the reader sees what it is doing as it works:
"Finding PO 90412", "Reading Business Central receipts", "Checking supplier confirmations
in mail", "Checking the answer against the records". Each step is a fixed phrase chosen
from what the agent's tool calls did, never a tool name, an id, a handle or an argument
as written. The only words taken from outside the vocabulary are the reader's own: an
identifier as written in their question.

A step names a source system or a kind of record only from a tool result the reader's own
session was served without error (the session reads as the reader, so a source they may
not see returns nothing or an error and is never named). Anything not recognised is a
generic step that names no source (fail closed).

``verdict``: the answer's first sentence, taken the moment the session's answer gate
accepts it (an ``answered`` decision), screened again by the gate's reply screen
(``answer_gate.final_reply``) with the claims that decision passed; the backend shows it
only after deciding every citation of those claims for this reader again."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional

from operating.agent_session import ANSWER_GATE_TOOL

MAX_STEP_CHARS = 120
MAX_VERDICT_CHARS = 400
# The reader's own identifier, as written in their question, at most this long.
MAX_MENTION_CHARS = 40
GATE_STEP = "Checking the answer against the records"
GATE_PASSED = "The answer checks out against the records"
GATE_REPAIR = "A figure did not check out; correcting it"
GATE_CONTINUE = "Part of the question is still open; reading further"

# A tool, before its result, in words that name no source.
CALL_WORDS = {"plx.find": "Looking up records", "plx.object": "Reading a record",
              "plx.around": "Following links between records",
              "plx.timeline": "Reading the history", "plx.documents": "Searching documents "
              "and email", "plx.read": "Reading a document", "plx.investigate":
              "Running a checked analysis", "plx.aggregate": "Adding up figures", "plx.query": "Following and adding up records",
              "plx.process": "Checking the process steps", "plx.compare": "Comparing records",
              "plx.identity": "Checking which records are the same",
              ANSWER_GATE_TOOL: GATE_STEP}
# The verb a tool's source-named step starts with.
VERBS = {"plx.find": "Looking up", "plx.object": "Reading", "plx.around": "Reading",
         "plx.timeline": "Reading the history of", "plx.read": "Reading",
         "plx.aggregate": "Adding up", "plx.query": "Adding up", "plx.process": "Checking", "plx.compare": "Comparing",
         "plx.investigate": "Checking"}
# An object id's source token (``Type:<source>.<table>:<key>``) as its system's name.
SYSTEMS = {"bc": "Business Central", "portal": "Portal", "sharepoint": "SharePoint",
           "mc": "Mission Control"}
# An object type in words (plural). A type not here is never named.
TYPES = {"Receipt": "receipts", "ReceiptLine": "receipts",
         "PurchaseOrder": "purchase orders", "PurchaseOrderLine": "purchase orders",
         "PurchaseInvoice": "purchase invoices", "PurchaseInvoiceLine": "purchase invoices",
         "SalesOrder": "sales orders", "SalesOrderLine": "sales orders", "Order": "orders",
         "SalesInvoice": "invoices", "Invoice": "invoices", "Shipment": "shipments",
         "Product": "items", "Supplier": "suppliers", "Customer": "customers",
         "AssemblyOrder": "assembly orders", "Quote": "quotes"}
# Types a supplier's mail is about.
SUPPLIER_TYPES = frozenset({"PurchaseOrder", "PurchaseOrderLine", "Receipt", "ReceiptLine",
                            "Supplier", "PurchaseInvoice", "PurchaseInvoiceLine"})
# What a mail search looks for, from fixed words its query holds (never the query).
TOPICS = ((re.compile(r"confirm", re.I), "confirmations"),
          (re.compile(r"invoice", re.I), "invoices"),
          (re.compile(r"quot", re.I), "quotes"),
          (re.compile(r"ship|track|deliver|eta\b", re.I), "shipping updates"),
          (re.compile(r"delay|late|push", re.I), "delay notices"))
# A document kind (``plx.documents`` items) as where it was found.
KINDS = {"email": "mail", "mail": "mail", "teams": "Teams", "teams_message": "Teams"}
OBJECT_ID = re.compile(r"\b([A-Z][A-Za-z]{1,40}):([a-z][a-z_]{0,30})\.[a-z_]+:")


def finding_steps(question: str, limit: int = 2) -> list[str]:
    """"Finding <identifier>" for each identifier the reader's question writes (their own
    words, the label with the value: "PO 90412"), at most ``limit``."""
    from ontology.identifiers import find_identifiers

    out: list[str] = []
    for mention in find_identifiers(question or ""):
        start = mention.label_start if isinstance(mention.label_start, int) else mention.start
        said = " ".join(question[start:mention.end].split())
        if not said or len(said) > MAX_MENTION_CHARS:
            continue
        step = f"Finding {said}"
        if step not in out:
            out.append(step)
        if len(out) >= limit:
            break
    return out


def call_step(tool: str) -> str:
    """A tool call as it starts, in words that name no source."""
    return CALL_WORDS.get(tool, "Reading records")


def _body(result: Any) -> Any:
    """A tool result's parsed body (None: unreadable)."""
    from operating import deep_investigation as deep

    if isinstance(result, list):
        result = "".join(str(part.get("text") or "") for part in result
                         if isinstance(part, Mapping))
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    body, _note = deep.strip_note(result)
    return body


def _type_of(object_id: Any) -> Optional[tuple[str, str]]:
    match = OBJECT_ID.match(object_id) if isinstance(object_id, str) else None
    return (match.group(1), match.group(2)) if match else None


def _named(pairs: Iterable[tuple[str, str]]) -> Optional[str]:
    """The most common (system, type) the pairs name, both known, in words."""
    counts: dict[str, int] = {}
    for kind, source in pairs:
        if kind in TYPES and source in SYSTEMS:
            words = f"{SYSTEMS[source]} {TYPES[kind]}"
            counts[words] = counts.get(words, 0) + 1
    return max(counts, key=lambda words: counts[words]) if counts else None


def result_step(tool: str, arguments: Any, result: Any, is_error: Any = False
                ) -> Optional[str]:
    """A tool's step once its result is in, naming the source and the kind of record the
    reader's session was served; None when the result is an error, unreadable, empty or
    names nothing this vocabulary knows (the call's own step stands)."""
    if is_error or tool == ANSWER_GATE_TOOL:
        return None
    body = _body(result)
    if not isinstance(body, Mapping) or body.get("error") or body.get("refused"):
        return None
    arguments = arguments if isinstance(arguments, Mapping) else {}
    if tool == "plx.documents":
        documents = [d for d in body.get("documents") or () if isinstance(d, Mapping)]
        where = {KINDS.get(str(d.get("kind")), "documents") for d in documents}
        if not where:
            return None
        query = arguments.get("query") if isinstance(arguments.get("query"), str) else ""
        topic = next((words for pattern, words in TOPICS if pattern.search(query)), None)
        about = _type_of(arguments.get("object_id"))
        if topic and about and about[0] in SUPPLIER_TYPES:
            topic = f"supplier {topic}"
        place = "mail" if where == {"mail"} else "Teams" if where == {"Teams"} else \
            "SharePoint documents" if where == {"documents"} else "documents and mail"
        return f"Checking {topic} in {place}" if topic else f"Checking {place}"
    verb = VERBS.get(tool)
    if verb is None:
        return None
    start = arguments.get("from") if tool in ("plx.around",) else None
    if tool in ("plx.object", "plx.timeline", "plx.process"):
        named = _named([pair for pair in (_type_of(arguments.get("object_id")
                                                   or arguments.get("from")),) if pair])
    else:
        text = json.dumps(body, default=str)
        named = _named(pair for pair in OBJECT_ID.findall(text)
                       if not (isinstance(start, str) and start.startswith(f"{pair[0]}:"
                                                                           f"{pair[1]}.")))
    if named is None:
        return None
    return f"{verb} {named}"[:MAX_STEP_CHARS]


def verdict(decision: Any, question: str, refused: Iterable[Any] = ()) -> Optional[str]:
    """The first sentence of the reply an ``answered`` gate decision carries, as the
    reader would read it, when the gate's reply screen keeps it whole against the claims
    that decision passed (``refused``: what every submission of the session had refused);
    None for any other decision, an empty reply or a sentence the screen changes."""
    from operating import answer_gate
    from . import presentation
    from .agent_ask import plain_tree

    decision = plain_tree(decision) if isinstance(decision, Mapping) else None
    if not decision or decision.get("outcome") != "answered":
        return None
    passed = [c for c in decision.get("claims") or () if isinstance(c, Mapping)]
    if not passed or not answer_gate.passed_cells(decision):
        return None
    reply = decision.get("reply") if isinstance(decision.get("reply"), Mapping) else {}
    text = reply.get("text") if isinstance(reply.get("text"), str) else ""
    first = next((s.strip() for s, _gap in answer_gate._sentences(text)
                  if s.strip() and not s.strip().startswith("Withheld")), "")
    if not first:
        return None
    screened = answer_gate.final_reply(first, passed, unsupported=list(refused),
                                       question=question)
    if screened["modified"] or screened["text"].strip() != first:
        return None
    said, _removed = presentation.reader_prose(first, [c.get("phrase") for c in passed])
    said = presentation.shown_words(said.strip())
    return said[:MAX_VERDICT_CHARS] if said else None


def citations(decision: Any) -> Optional[list[str]]:
    """Every citation of the claims an ``answered`` decision passed; None when a passed
    claim cites nothing (the answer would hold it back: no early verdict)."""
    from .agent_ask import plain_tree

    decision = plain_tree(decision) if isinstance(decision, Mapping) else {}
    out: list[str] = []
    for claim in decision.get("claims") or ():
        if not isinstance(claim, Mapping):
            continue
        cited = [c for c in claim.get("citations") or () if isinstance(c, str)]
        if not cited:
            return None
        out += cited
    return list(dict.fromkeys(out))
