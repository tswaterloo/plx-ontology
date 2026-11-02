"""Fast answers: the questions an executive asks most, answered in seconds by deterministic
reads and the same answer gate, with no model loop.

The agent path (``operator_api.agent_ask``) answers any question in about a minute: a
model chooses tool calls, submits claims to the answer gate and writes the reply. For a
question of a known shape about one record ("where's TASK-123 at", "how much R-0162 do we
have on hand", "open AR for <customer>") the calls are always the same, so code makes
them. What is fixed here is only *which* reads answer an intent; what may be said is
decided exactly as for the agent:

* **Recognise** (``understand``): the question folded and its shorthand expanded as query
  understanding reads it (``query_understanding.fold``), one intent of ``INTENTS`` matched
  whole by its words, and the one record it names (an identifier-shaped token, else a
  customer name). Two intents, two records, a negation or a time frame the intent does not
  take: no fast answer (the agent answers).
* **Resolve** (``Reads.resolve``): ``plx.find`` as the reader, typed. A candidate is the
  record only when its own identifier (or, for a customer, its exact normalised name) is
  what the question wrote and it is the one such record of the system the intent reads;
  several are offered as a did-you-mean (``Choices``), never picked; a lead (a text match)
  is never the record.
* **Read** (the intent's recipe): the generic tools (``plx.object``, ``plx.read``,
  ``plx.around``, ``plx.process``, ``plx.investigate``, ``plx.changes``) through
  ``Reads.call``: the host's own tool table for the reader, its employee gate and read
  policy, one projection generation for the whole answer, as ``MCPServer._generic_call``
  serves an MCP client; every result is a session event with the handles it returned as
  evidence (``answer_gate.evidence_handles`` on the result as it leaves the output
  boundary) and the counts and currencies the gate binds claims to.
* **Gate** (``Reads.gate``): the answer's values, each one cited claim
  (``agent_session.host_submission``), go through ``mcp_server.answer_decision``, the
  ``plx.case_get`` answer mode an agent submits to: same checks, same re-reads as the
  reader, no repair. The reply is the session's final reply (``agent_session.gated_run``'s
  ``gate_final_reply``): only what the gate passed is said.

Fail closed: an unknown or unexpected input, a missing or withheld field, a gap or conflict
a recipe reads, a claim the gate refuses, or any error is ``None``: the agent answers. A
fast answer never guesses and never says less than the gate passed.

Evidence: isolated-fixture (operating/tests/test_fast_answer.py); the live-ops dev cards in
process over the captured sources (``scripts/eval_fast_path.py``, live-captures).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import re
import time
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from .customer_profit import normal_name

ANSWER_FORMAT = "plx-eval-answer/v1"
FIND_LIMIT = 10

# --------------------------------------------------------------------------- understand

# An identifier-shaped token: letters, digits and joiners with at least one digit
# (``R-0162``, ``PO#4819``, ``S-ORD101085``, ``TASK-3383``, ``25A0165``).
_TOKEN = re.compile(r"(?<![\w#-])([A-Za-z]{0,6}[#-]?[A-Za-z0-9]*\d[A-Za-z0-9#-]*)(?![\w#-])")
_TASK = re.compile(r"^TASK-\d{1,6}$", re.I)
_PO = re.compile(r"^(?:PO)[#-]?\s?(\d{3,8})$", re.I)
_SO = re.compile(r"^(?:S-?ORD|SO)-?\d{3,8}$", re.I)
_BATCH = re.compile(r"^BC-\d{4,6}[A-Z]?$", re.I)
_ITEM = re.compile(r"^(?:[A-Z]{1,3}-\d{2,6}[A-Z0-9-]*|[A-Z]{2,4}-\d{2,4}-[A-Z0-9]+)$", re.I)

_NEGATION = re.compile(r"\b(not|never|without|except|excluding|no longer)\b")
_FRAME = re.compile(r"\b(since|before|after|between|last (?:week|month|year|quarter)|"
                    r"in (?:19|20)\d\d|yesterday|today|this (?:week|month|year))\b")


@dataclass(frozen=True)
class Intent:
    name: str
    words: re.Pattern       # matched on the folded question
    entity: str             # what it names: task, item, lot, batch, po, so, customer, none
    takes_frame: bool = False


def _w(pattern: str) -> re.Pattern:
    return re.compile(pattern)


# The intents, most specific first; a question matching two is not fast.
INTENTS: tuple[Intent, ...] = (
    Intent("task_status", _w(r"\b(where|status|stage|owner|owns|own|who|at|progress|update)\b"),
           "task"),
    Intent("on_hand", _w(r"\b(on hand|in stock|stock|inventory|how much|how many)\b"), "item"),
    Intent("price_trend", _w(r"\b(trend|going up|up or down|more or less|rising|falling|"
                             r"increas\w*|decreas\w*)\b"), "item"),
    Intent("last_price", _w(r"\b(last (?:price|paid|cost|time)|paid|pay|price paid|"
                            r"buy \w+ from|who do we buy|from who|supplier|vendor)\b"),
           "item"),
    Intent("lot_expiry", _w(r"\b(expir\w*|exp date|shelf life|best before)\b"), "lot"),
    Intent("batch_status", _w(r"\b(hold|held|release\w*|qc|quality)\b"), "batch"),
    Intent("order_margin", _w(r"\b(margin|profit\w*|make on|made on|money on)\b"), "so"),
    Intent("po_status", _w(r"\b(status|where|wheres|late|overdue|eta|due|land|arriv\w*|"
                           r"come in|going on|update)\b"), "po"),
    Intent("open_ar", _w(r"\b(ar|a r|receivables?|owes?|owing|balance|outstanding)\b"),
           "customer"),
    Intent("open_sos", _w(r"\b(open|still need to ship|not shipped|unshipped|outstanding)\b"
                          r".*\b(orders?|sos?|sales)\b|\b(orders?|sos?)\b.*\b(open|still "
                          r"need to ship|not shipped|unshipped)\b"),
           "customer"),
    Intent("changes", _w(r"\b(changed?|changes|new or changed|updated|modified)\b"), "none",
           takes_frame=True),
)

# Words that never belong to a customer's name in an open-AR or open-order question.
_NAME_FILLER = re.compile(
    r"\b(open|ar|a/r|receivables?|owes?|owing|us|we|our|balance|due|outstanding|what|whats|"
    r"what's|how|much|does|do|is|are|the|for|with|on|of|in|any|sales|orders?|sos?|still|"
    r"need|needs|to|ship|shipped|unshipped|not|bc|business central|portal|show|me|list|"
    r"pls|please|rn|now|right|currently|have|has)\b", re.I)


@dataclass(frozen=True)
class Ask:
    intent: str
    mention: str            # the identifier or name as the question wrote it
    system: Optional[str]   # the system the question names (bc, portal), else None


def fold(question: str) -> str:
    from .query_understanding import _SHORTHAND, fold as qu_fold
    words = qu_fold(question).split()
    return " ".join(_SHORTHAND.get(word, word) for word in words)


def _tokens(question: str) -> list[str]:
    out = []
    for found in _TOKEN.finditer(question or ""):
        token = found.group(1).strip("-#")
        if len(token) >= 3 and any(c.isdigit() for c in token) and token not in out:
            out.append(token)
    return out


def _kind(token: str) -> Optional[str]:
    if _TASK.match(token):
        return "task"
    if _PO.match(token):
        return "po"
    if _SO.match(token):
        return "so"
    if _BATCH.match(token):
        return "batch"
    if _ITEM.match(token):
        return "item"
    if re.fullmatch(r"[A-Za-z0-9]{5,14}", token) and sum(c.isdigit() for c in token) >= 4:
        return "lot"   # a lot number is any other id-shaped token; asked with "lot"
    return None


def understand(question: str) -> Optional[Ask]:
    """The one intent and record the question asks about, or None (not a fast question)."""
    if not isinstance(question, str) or not question.strip() or len(question) > 300:
        return None
    folded = fold(question)
    said = fold(_TOKEN.sub(" ", question))
    system = ("bc" if re.search(r"\b(bc|business central)\b", said) else
              "portal" if re.search(r"\bportal\b", said) else None)
    # A record's name written beside its identifier, in brackets ("R-0162 (Ethyl Alcohol
    # 100% SDAG)"), is a label of that record, not a second one.
    outside = re.sub(r"\([^()]*\)", " ", question)
    tokens = _tokens(outside) if _tokens(outside) else _tokens(question)
    kinds = [k for k in (_kind(t) for t in tokens) if k is not None]
    if len(tokens) > 1 or (tokens and not kinds):
        return None     # two records, or one the grammar does not read: the agent's
    named = kinds[0] if kinds else None
    token = tokens[0] if tokens else None
    matched = [i for i in INTENTS if i.words.search(folded)
               and (i.entity == named or (named == "lot" and i.entity == "lot")
                    or (named is None and i.entity in ("customer", "none")))]
    if named == "lot" and not re.search(r"\blot\b|\bexpir|\bexp\b", folded):
        matched = [i for i in matched if i.entity != "lot"]
    if named == "item":
        # an item question asks one thing: trend wins over a price word inside it
        names = [i.name for i in matched]
        if "price_trend" in names:
            matched = [i for i in matched if i.name != "last_price"]
        if "price_trend" in names or "last_price" in names:
            matched = [i for i in matched if i.name != "on_hand"]
    if named == "customer" or named is None:
        names = [i.name for i in matched]
        if "open_ar" in names and "open_sos" in names:
            return None
    if len(matched) != 1:
        return None
    intent = matched[0]
    if _NEGATION.search(folded) and intent.name not in ("batch_status",):
        return None
    if _FRAME.search(folded) and not intent.takes_frame:
        return None
    if intent.entity == "customer":
        name = _customer_name(question)
        return Ask(intent.name, name, system) if name else None
    if intent.entity == "none":
        return Ask(intent.name, "", system) if not tokens else None
    return Ask(intent.name, token or "", system)


def _customer_name(question: str) -> str:
    """The customer's name as the question writes it: what follows its "for" or "with",
    else the question without its leading and trailing intent and filler words
    (``what does A1 American owe us`` -> ``A1 American``); inner words are the name's."""
    text = re.sub(r"\b(?:in|from)\s+(?:bc|business central|the portal|portal)\b", " ",
                  question, flags=re.I)
    text = re.sub(r"[?!]+", " ", text).strip()
    after = re.search(r"\b(?:for|with)\s+(\S.*)$", text, flags=re.I)
    words = (after.group(1) if after else text).split()
    filler = lambda word: bool(_NAME_FILLER.fullmatch(word.strip(",.:;'\"").lower())) \
        or not word.strip(",.:;'\"-")
    while words and filler(words[0]) and not after:
        words.pop(0)
    while words and filler(words[-1]):
        words.pop()
    return " ".join(words).strip(" ,.-'")[:120]


# --------------------------------------------------------------------------- reading


def plain(value: Any) -> Any:
    """A tool value with its output envelope removed."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        return value["untrusted_text"]
    return value


class Unanswerable(Exception):
    """The fast path cannot answer this (a code, never shown): the agent answers."""


class Reads:
    """One reader's reads for one fast answer: the host's generic tools for ``actor``
    (``host``: its ``CaseHostTools``; ``runtime``: its ``CaseRuntime``), served as
    ``MCPServer._generic_call`` serves them (the employee gate, the read policy decided
    for the reader, plx.read paged), all inside one ``host.request()`` (one projection
    generation); ``server``: the reader's MCP session, whose answer gate re-reads every
    cited record as the reader. Every call is a session event (as an agent transcript
    records it); ``seen`` and ``returned`` are what the gate binds claims to."""

    def __init__(self, host: Any, runtime: Any, actor: Any, server: Any) -> None:
        self.host, self.runtime, self.actor, self.server = host, runtime, actor, server
        self.events: list[dict[str, Any]] = []
        self.seen: dict[str, None] = {}
        self.returned: dict[str, Any] = {"counts": [], "currencies": {},
                                         "currency_citations": {}}
        self._gated: Any = None
        self._read: Any = None
        self.reply: dict[str, Any] = {}   # the final reply's gate report (counts, tokens)

    @classmethod
    def of_server(cls, server: Any) -> "Reads":
        """The reads of an ``MCPServer`` session (its ontology host, runtime and actor)."""
        if server.ontology is None:
            raise Unanswerable("no_ontology_host")
        return cls(server.ontology, server.case_runtime, server.actor, server)

    @contextmanager
    def request(self) -> Iterator["Reads"]:
        with self.host.request():
            self._gated = self.runtime.employee_gate(self.actor)
            yield self

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        from . import answer_gate, mcp_server
        from .claim_checks import returned_evidence
        if self._gated is None:
            raise Unanswerable("outside_request")
        arguments = dict(arguments)
        started = time.monotonic()
        try:
            if name == "plx.read":
                from .case_tools_host import PagedRead
                tools = self.host.tools
                tools.policy = self.host.policy_now(self._gated)
                if self._read is None or self._read[0] is not tools:
                    self._read = (tools, PagedRead(self.host.evidence, self.runtime))
                result = self._read[1].call(arguments, self._gated)
            else:
                result = self.host.call(name, arguments, self._gated)
            error = not isinstance(result, Mapping) or "error" in result
        except Exception:  # noqa: BLE001 - a refused or failed read answers nothing
            result, error = {"error": {"code": "tool_execution_failed"}}, True
        shown = mcp_server._firewall(result, name) if not error else result
        self.events.append({"seq": len(self.events), "type": "tool_call", "name": name,
                            "arguments": arguments, "result": shown, "is_error": error,
                            "elapsed_ms": int((time.monotonic() - started) * 1000)})
        if error:
            raise Unanswerable(f"tool_refused:{name}")
        returned_evidence(name, arguments, result, self.returned)
        for handle in sorted(answer_gate.evidence_handles(name, shown, arguments=arguments)):
            self.seen[handle] = None
        return dict(result)

    def gate(self, submission: Mapping[str, Any]) -> dict[str, Any]:
        """``submission`` through the answer gate the agent submits to (``plx.case_get``
        answer mode, ``mcp_server.answer_decision``), recorded as its event."""
        from . import mcp_server
        from .agent_session import ANSWER_GATE_TOOL
        started = time.monotonic()
        returned = {"counts": list(self.returned.get("counts") or ()),
                    "currencies": dict(self.returned.get("currencies") or {}),
                    "currency_citations": dict(self.returned.get("currency_citations") or {})}
        result, _gated, _repairing = mcp_server.answer_decision(
            dict(submission), self.server, seen=set(self.seen), returned=returned,
            may_repair=False, repairs_left=0, unread=None)
        self.events.append({"seq": len(self.events), "type": "tool_call",
                            "name": ANSWER_GATE_TOOL, "arguments": {"answer": dict(submission)},
                            "result": result, "is_error": False,
                            "elapsed_ms": int((time.monotonic() - started) * 1000)})
        return result

    # ---- reads the recipes share ---------------------------------------------------

    def find(self, query: str, object_type: Optional[str] = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {"query": query[:200], "limit": FIND_LIMIT}
        if object_type:
            arguments["object_type"] = object_type
        return self.call("plx.find", arguments)

    def row(self, handle: str) -> dict[str, Any]:
        """A cited record's fields as the reader reads them (``plx.read``): field path ->
        value. A record not resolved for this reader is ``Unanswerable``."""
        read = self.call("plx.read", {"citation": handle})
        if read.get("status") not in ("resolved",) or not isinstance(read.get("fields"),
                                                                       Mapping):
            raise Unanswerable("record_not_resolved")
        if (read.get("completeness") or {}).get("truncated"):
            raise Unanswerable("record_read_in_part")
        return {"citation": read.get("citation") or handle,
                **{k: plain(v) for k, v in read["fields"].items()}}

    def object(self, object_id: str) -> dict[str, Any]:
        return self.call("plx.object", {"object_id": object_id})


@dataclass
class Choices:
    """Several records the question's name or number may mean: asked back, never picked."""
    mention: str
    candidates: list[dict[str, Any]]


@dataclass
class Draft:
    """What a recipe read: answer values (each cited), the sentences that say them, gaps
    and the records it is about."""
    values: list[dict[str, Any]] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    roots: list[dict[str, Any]] = field(default_factory=list)
    answerability: str = "answerable"

    def value(self, name: str, value: Any, handle: str, field_path: str, **extra: Any) -> str:
        if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise Unanswerable(f"value_missing:{name}")
        text = str(value).strip()
        if not text:
            raise Unanswerable(f"value_empty:{name}")
        self.values.append({"name": name, "value": text,
                            "citations": [{"l1_handle": handle, "field_path": field_path}],
                            **{k: v for k, v in extra.items() if v}})
        return text


# --------------------------------------------------------------------------- resolve

_SAME_SYSTEMS = {"bc": "bc.", "portal": "portal."}


def _norm_id(text: Any) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(plain(text) or "").upper())


def _candidates(found: Mapping[str, Any], types: Sequence[str]) -> list[dict[str, Any]]:
    out = []
    for c in found.get("candidates") or ():
        obj = c.get("object") or {}
        if obj.get("object_type") in types and isinstance(obj.get("object_id"), str):
            out.append(c)
    return out


def _binding(candidate: Mapping[str, Any]) -> str:
    oid = str((candidate.get("object") or {}).get("object_id") or "")
    return oid.split(":", 2)[1] if oid.count(":") >= 2 else ""


def _withheld(found: Mapping[str, Any]) -> bool:
    withheld = found.get("withheld")
    if isinstance(withheld, Mapping):
        return any(isinstance(v, int) and v > 0 for v in withheld.values())
    return bool(withheld)


def by_identifier(reads: Reads, token: str, types: Sequence[str],
                  binding: Optional[str] = None, found: Optional[Mapping[str, Any]] = None
                  ) -> dict[str, Any] | Choices:
    """The one record of ``types`` (of ``binding``'s system when given) whose own
    identifier is ``token`` (in ``found``, a ``plx.find`` already made, else one typed
    find); several such records are ``Choices``; none, or only lead (text) matches, is
    ``Unanswerable``."""
    if found is None:
        found = reads.find(token, types[0] if len(types) == 1 else None)
    want = _norm_id(token)
    exact = []
    for c in _candidates(found, types):
        matched = c.get("matched") or {}
        said = _norm_id(matched.get("raw") or matched.get("normalized"))
        if c.get("tier") == "lead" and c.get("reason") not in ("exact_identifier",):
            # a text match is a lead: it is the record only when the cell it matched is the
            # record's own identifier field and reads exactly the token
            if matched.get("identifier_kind") != "display_text" or said != want \
                    or matched.get("property") not in ("task_id", "code", "batch_code",
                                                       "lot_number", "po_number",
                                                       "order_number"):
                continue
        elif said and said != want and _norm_id(matched.get("normalized")) != want:
            continue
        if binding and not _binding(c).startswith(binding):
            continue
        exact.append(c)
    if not exact:
        raise Unanswerable("not_resolved")
    ids = list(dict.fromkeys(c["object"]["object_id"] for c in exact))
    if len(ids) > 1:
        return Choices(token, [{"object_id": c["object"]["object_id"],
                                "object_type": c["object"]["object_type"],
                                "display": c["object"].get("display")} for c in exact])
    return exact[0]


def by_name(reads: Reads, name: str, binding: str) -> dict[str, Any] | Choices:
    """The one customer of ``binding``'s system whose name, normalised
    (``customer_profit.normal_name``), is the name asked; several are ``Choices``."""
    found = reads.find(name, "Customer")
    want = normal_name(name)
    if not want:
        raise Unanswerable("no_name")
    exact = [c for c in _candidates(found, ("Customer",))
             if _binding(c).startswith(binding)
             and normal_name(str(plain((c.get("object") or {}).get("display")) or "")) == want]
    if not exact:
        raise Unanswerable("customer_not_resolved")
    if len(exact) > 1:
        return Choices(name, [{"object_id": c["object"]["object_id"],
                               "object_type": "Customer",
                               "display": c["object"].get("display")} for c in exact])
    return exact[0]


def _handle_of(candidate: Mapping[str, Any]) -> str:
    """The record handle a find candidate cites (its matched cell's or display's)."""
    for part in ((candidate.get("matched") or {}).get("raw"),
                 (candidate.get("object") or {}).get("display")):
        origin = part.get("origin") if isinstance(part, Mapping) else None
        handle = origin.get("citation") if isinstance(origin, Mapping) else None
        if isinstance(handle, str):
            return handle
    raise Unanswerable("no_handle")


def _object_values(card: Mapping[str, Any], prop: str) -> list[tuple[Any, str, str]]:
    """(value, handle, field) of each source's value of ``prop`` on an object card."""
    out = []
    for v in card.get("values") or ():
        cite = v.get("citation") or {}
        if v.get("property") == prop and isinstance(cite.get("l1_handle"), str):
            out.append((plain(v.get("value")), cite["l1_handle"], cite.get("field_path")))
    return out


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        raise Unanswerable("not_a_number") from None


def _amount(value: Decimal) -> str:
    """A money or quantity figure as written for a reader (no exponent, no float)."""
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


# --------------------------------------------------------------------------- recipes


def task_status(reads: Reads, ask: Ask, draft: Draft) -> None:
    found = by_identifier(reads, ask.mention, ("MCTask",))
    if isinstance(found, Choices):
        raise _Choice(found)
    row = reads.row(_handle_of(found))
    handle = row["citation"]
    task = str(row.get("row.id") or "")
    if _norm_id(task) != _norm_id(ask.mention):
        raise Unanswerable("task_mismatch")
    draft.value("task", task, handle, "row.id")
    stage = draft.value("stage", row.get("row.stage"), handle, "row.stage")
    owner_field = "row.accountableOwner" if row.get("row.accountableOwner") else "row.assignee"
    owner = draft.value("owner", row.get(owner_field), handle, owner_field)
    draft.sentences.append(f"{task} is at the {stage} stage in Mission Control, owned by "
                           f"{owner}.")
    draft.roots.append({"object_id": found["object"]["object_id"], "object_type": "MCTask",
                        "mention": task})


def on_hand(reads: Reads, ask: Ask, draft: Draft) -> None:
    found = by_identifier(reads, ask.mention, ("Product",), "bc.")
    if isinstance(found, Choices):
        raise _Choice(found)
    row = reads.row(_handle_of(found))
    handle = row["citation"]
    if _norm_id(row.get("row.number")) != _norm_id(ask.mention) \
            or row.get("row.type") != "Inventory":
        raise Unanswerable("not_an_inventory_item")
    draft.value("item", row.get("row.number"), handle, "row.number")
    stock = draft.value("on_hand", _amount(_decimal(row.get("row.inventory"))), handle,
                        "row.inventory")
    unit = row.get("row.baseUnitOfMeasureCode")
    said = f"Business Central shows {stock} of {row['row.number']} on hand"
    if isinstance(unit, str) and unit.strip():
        draft.value("base_unit", unit, handle, "row.baseUnitOfMeasureCode")
        said += f" (base unit {unit})"
    draft.sentences.append(said + ".")
    draft.roots.append({"object_id": found["object"]["object_id"], "object_type": "Product",
                        "mention": str(row["row.number"])})


def lot_expiry(reads: Reads, ask: Ask, draft: Draft) -> None:
    token = re.sub(r"(?i)^lot\s*", "", ask.mention)
    found = by_identifier(reads, token, ("Lot",))
    if isinstance(found, Choices):
        raise _Choice(found)
    card = reads.object(found["object"]["object_id"])
    expiries = [v for v in _object_values(card, "expiry_date") if v[0]]
    if not expiries or len({str(v[0])[:10] for v in expiries}) != 1 or card.get("conflicts"):
        raise Unanswerable("expiry_not_one")
    day, handle, field_path = expiries[0]
    numbers = [v for v in _object_values(card, "lot_number") if _norm_id(v[0]) == _norm_id(token)]
    if not numbers:
        raise Unanswerable("lot_number_unread")
    token = draft.value("lot_number", numbers[0][0], numbers[0][1], numbers[0][2])
    stated = draft.value("expiry_date", str(day)[:10], handle, field_path)
    draft.sentences.append(f"Lot {token} expires on {stated}.")
    draft.roots.append({"object_id": found["object"]["object_id"], "object_type": "Lot",
                        "mention": token})


def batch_status(reads: Reads, ask: Ask, draft: Draft) -> None:
    found = by_identifier(reads, ask.mention, ("Batch",), "portal.app_batch_record")
    if isinstance(found, Choices):
        raise _Choice(found)
    row = reads.row(_handle_of(found))
    handle = row["citation"]
    if _norm_id(row.get("row.batch_number")) != _norm_id(ask.mention):
        raise Unanswerable("batch_mismatch")
    draft.value("batch", row.get("row.batch_number"), handle, "row.batch_number")
    qc = draft.value("qc_status", row.get("row.status_qc"), handle, "row.status_qc")
    status = row.get("row.status")
    said = f"Batch {row['row.batch_number']}'s QC status is {qc}"
    if isinstance(status, str) and status.strip():
        draft.value("batch_status", status, handle, "row.status")
        said += f" (batch status {status})"
    draft.sentences.append(said + ".")
    draft.roots.append({"object_id": found["object"]["object_id"], "object_type": "Batch",
                        "mention": str(row["row.batch_number"])})


def po_status(reads: Reads, ask: Ask, draft: Draft) -> None:
    number = _PO.match(ask.mention)
    if number is None:
        raise Unanswerable("not_a_po")
    # one untyped find: an identifier lookup over every system; a typed one scans
    found = reads.find(ask.mention)
    reasons = {g.get("reason") for g in found.get("gaps") or () if isinstance(g, Mapping)}
    exact = [c for c in _candidates(found, ("PurchaseOrder",)) if c.get("tier") != "lead"]
    if not exact:
        nothing = found.get("status") == "not_found" or "named_record_not_found" in reasons
        if nothing and not _withheld(found):
            # no record the reader may read holds that number as a purchase order (the
            # find says so: every candidate is a lead of another kind): said as such,
            # nothing cited, as there is nothing to cite
            # (the number is not restated: a figure in a reply is backed by a claim, and
            # nothing holds this one)
            draft.sentences.append(
                "I can't find that purchase order: no purchase order with that number is on "
                "record in Business Central, the Portal or FileMaker that you can see.")
            draft.gaps.append({"state": "unknown", "subject": f"{ask.mention}",
                               "reason": "named_record_not_found"})
            return
        raise Unanswerable("po_not_resolved")
    picked = by_identifier(reads, ask.mention, ("PurchaseOrder",), "portal.app_purchase_order",
                           found=found)
    if isinstance(picked, Choices):
        raise _Choice(picked)
    root = picked["object"]["object_id"]
    process = reads.call("plx.process", {"from": root})
    lateness = [x for x in process.get("lateness") or () if x.get("stage") == "receipt"]
    if len(lateness) != 1 or lateness[0].get("gaps") or lateness[0].get("systems_disagree") \
            or process.get("gaps"):
        raise Unanswerable("lateness_has_gaps")
    verdict = lateness[0]
    governing = [v for v in verdict.get("values") or () if (v.get("citation") or {}).get(
        "l1_handle") and str(plain(v.get("value")))[:10] == str(verdict.get("governing_date"))]
    if verdict.get("late") is True and governing:
        cite = governing[0]["citation"]
        due = draft.value("due_date", str(verdict["governing_date"]), cite["l1_handle"],
                          cite["field_path"])
        draft.sentences.append(f"{ask.mention} is late: it was due {due} and is not fully "
                               f"received.")
    elif verdict.get("late") is False and verdict.get("state") == "received":
        draft.sentences.append(f"{ask.mention} has been received.")
        raise Unanswerable("received_without_cited_receipt")
    else:
        raise Unanswerable("lateness_undecided")
    draft.roots.append({"object_id": root, "object_type": "PurchaseOrder",
                        "mention": ask.mention})


def order_margin(reads: Reads, ask: Ask, draft: Draft) -> None:
    found = by_identifier(reads, ask.mention, ("SalesOrder",))
    if isinstance(found, Choices):
        raise _Choice(found)
    root = found["object"]["object_id"]
    result = reads.call("plx.investigate", {"family": "order_margin", "root": root})
    answerable = (result.get("answerability") or {}).get("state") == "answerable"
    if not answerable or result.get("conflicts"):
        raise Unanswerable("margin_not_answerable")
    entries = {e.get("name"): e for e in result.get("answer") or () if isinstance(e, Mapping)}
    revenue = [e for n, e in entries.items() if re.fullmatch(r"revenue(_\d+)?", str(n))]
    costs = [e for n, e in entries.items() if re.fullmatch(r"booked_cost(_\d+)?", str(n))]
    total = entries.get("order_margin_revenue")
    cost_total = entries.get("order_margin_booked_cost")
    if not (revenue and costs and total and cost_total) or len(revenue) != 1 \
            or len(costs) != 1:
        raise Unanswerable("margin_lines_not_one")   # one line, one cost: cited as read
    rev_cite, cost_cite = revenue[0].get("citation") or {}, costs[0].get("citation") or {}
    rev = _decimal(plain(revenue[0].get("value")))
    cost = _decimal(plain(costs[0].get("value")))
    if rev != _decimal(plain(total.get("value"))) or abs(cost) != _decimal(
            plain(cost_total.get("value"))) or rev <= 0:
        raise Unanswerable("margin_totals_differ")
    currency = plain(total.get("unit")) if isinstance(plain(total.get("unit")), str) else None
    money = {"currency": currency} if currency and re.fullmatch(r"[A-Z]{3}", currency) else {}
    draft.value("revenue", _amount(rev), rev_cite.get("l1_handle"), rev_cite.get("field_path"),
                **money)
    draft.value("cost", _amount(cost), cost_cite.get("l1_handle"), cost_cite.get("field_path"),
                **money)
    margin = rev + cost if cost < 0 else rev - cost
    if margin != _decimal(plain(entries["order_margin"].get("value"))) \
            if entries.get("order_margin") else True:
        raise Unanswerable("margin_differs")
    # The margin is revenue less the booked cost (the ledger books a sale's cost negative),
    # each input cited; a percent of it would name revenue twice, which no gate check
    # can bind to one printed figure, so the amount is what is said.
    draft.values.append({"name": "margin", "value": _amount(margin),
                         "calculation": {"op": "expression",
                                         "expression": "x0 + x1" if cost < 0 else "x0 - x1",
                                         "inputs": [
                                             {"l1_handle": rev_cite["l1_handle"],
                                              "field_path": rev_cite["field_path"],
                                              "value": _amount(rev)},
                                             {"l1_handle": cost_cite["l1_handle"],
                                              "field_path": cost_cite["field_path"],
                                              "value": _amount(cost)}]}})
    unit = f" {currency}" if money else ""
    # the posted invoice the revenue is booked on, named so the reader can open it
    invoice = entries.get("invoice") or {}
    cite = invoice.get("citation") or {}
    if invoice.get("kind") != "property" or invoice.get("check") not in ("passed",) \
            or not isinstance(cite.get("l1_handle"), str):
        raise Unanswerable("invoice_unread")
    number = draft.value("invoice", plain(invoice.get("value")), cite["l1_handle"],
                         cite.get("field_path"))
    draft.sentences.append(
        f"{ask.mention} made a margin of {_amount(margin)} on item cost in Business Central: "
        f"revenue {_amount(rev)}{unit} on invoice {number}, and the ledger books its item "
        f"cost at {_amount(cost)}{unit}.")
    draft.roots.append({"object_id": root, "object_type": "SalesOrder",
                        "mention": ask.mention})


MAX_SAME_NAME = 4          # customer cards of one exact name an answer reads, each named


def _same_named(reads: Reads, name: str, binding: str) -> list[dict[str, Any]]:
    """Every customer of ``binding``'s system whose exact normalised name is ``name`` (a
    system may hold two cards of one name: each is read and named, never merged or one
    picked); more than ``MAX_SAME_NAME`` is ``Unanswerable``."""
    found = by_name(reads, name, binding)
    cards = found.candidates if isinstance(found, Choices) else [found["object"]]
    if len(cards) > MAX_SAME_NAME:
        raise Unanswerable("too_many_same_named")
    return [dict(card) for card in cards]


def _customer_handle(card: Mapping[str, Any]) -> str:
    return _handle_of({"object": card})


def open_ar(reads: Reads, ask: Ask, draft: Draft) -> None:
    cards = _same_named(reads, ask.mention, "bc.customers")
    parts = []
    for at, card in enumerate(cards, 1):
        row = reads.row(_customer_handle(card))
        handle = row["citation"]
        suffix = "" if len(cards) == 1 else f"_{at}"
        number = draft.value(f"customer_number{suffix}", row.get("row.number"), handle,
                             "row.number")
        balance = draft.value(f"open_ar{suffix}", _amount(_decimal(row.get("row.balanceDue"))),
                              handle, "row.balanceDue")
        currency = row.get("row.currencyCode")
        money = ""
        if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency.strip()):
            money = " " + draft.value(f"currency{suffix}", currency.strip(), handle,
                                      "row.currencyCode")
        parts.append((number, balance + money))
        draft.roots.append({"object_id": card["object_id"], "object_type": "Customer",
                            "mention": ask.mention})
    if len(parts) == 1:
        draft.sentences.append(f"{ask.mention}'s open AR (balance due in Business Central, "
                               f"customer {parts[0][0]}) is {parts[0][1]}.")
    else:
        draft.sentences.append(
            f"Business Central has more than one customer card named {ask.mention}; the "
            "open AR (balance due) of each: " + "; ".join(f"customer {n}: {b}" for n, b in parts)
            + ".")


MAX_ORDERS_READ = 12      # open orders one answer reads and cites one by one


def _orders_of(reads: Reads, root: str, filters: Sequence[Mapping[str, Any]] = ()
               ) -> list[dict[str, Any]]:
    """The sales orders that name ``root`` as their customer, as the reader reads them
    (every one: a cut list is ``Unanswerable``)."""
    arguments: dict[str, Any] = {"from": root, "limit": 50, "path": [
        {"link_type": "SalesOrder.ordered_by", "direction": "reverse"}]}
    if filters:
        arguments["filters"] = list(filters)
    around = reads.call("plx.around", arguments)
    if (around.get("completeness") or {}).get("truncated") or _withheld(around):
        raise Unanswerable("orders_cut_or_withheld")
    return [o for o in around.get("objects") or () if o.get("object_type") == "SalesOrder"]


def open_sos(reads: Reads, ask: Ask, draft: Draft) -> None:
    listed: list[str] = []
    unnumbered = 0
    if ask.system == "bc":
        system = "Business Central"
        cards = _same_named(reads, ask.mention, "bc.customers")
        orders = [o for card in cards for o in _orders_of(reads, card["object_id"])
                  if str(o["object_id"]).startswith("SalesOrder:bc.sales_orders:")]
        if len(orders) > MAX_ORDERS_READ:
            raise Unanswerable("too_many_orders")
        for order in orders:
            row = reads.row(_handle_of({"object": order}))
            if str(row.get("row.fullyShipped")).lower() != "false":
                continue
            at = len(listed) + 1
            listed.append(draft.value(f"open_order_{at}", row.get("row.number"),
                                      row["citation"], "row.number"))
            draft.value(f"open_order_{at}_fully_shipped", str(row["row.fullyShipped"]),
                        row["citation"], "row.fullyShipped")
        roots = cards
    else:
        system = "the Portal"
        found = by_name(reads, ask.mention, "portal.app_customer")
        if isinstance(found, Choices):
            raise _Choice(found)
        roots = [found["object"]]
        orders = _orders_of(reads, found["object"]["object_id"], [
            {"property": "status", "op": "in", "value": sorted(OPEN_PORTAL_ORDER)}])
        if len(orders) > MAX_ORDERS_READ:
            raise Unanswerable("too_many_orders")
        for order in orders:
            card = reads.call("plx.object", {"object_id": order["object_id"],
                                             "properties": ["order_number", "status"]})
            numbers = _object_values(card, "order_number")
            statuses = _object_values(card, "status")
            if len(statuses) != 1 or str(statuses[0][0]) not in OPEN_PORTAL_ORDER:
                raise Unanswerable("order_status_unread")
            if not numbers or not numbers[0][0]:
                unnumbered += 1
                continue
            at = len(listed) + 1
            listed.append(draft.value(f"open_order_{at}", numbers[0][0], numbers[0][1],
                                      numbers[0][2]))
            draft.value(f"open_order_{at}_status", statuses[0][0], statuses[0][1],
                        statuses[0][2])
    if not listed:
        raise Unanswerable("no_numbered_open_orders")   # an empty list: the agent says it
    draft.sentences.append(
        f"Open sales orders for {ask.mention} in {system}: " + ", ".join(listed) + ".")
    if unnumbered:
        draft.gaps.append({"state": "unknown", "reason": "order_number_missing",
                           "subject": f"{unnumbered} more open order(s) carry no order "
                                      "number"})
        draft.answerability = "partial"
    draft.roots += [{"object_id": card["object_id"], "object_type": "Customer",
                     "mention": ask.mention} for card in roots]


# The Portal's sales order statuses that are still to ship (``app_sales_order.status``).
OPEN_PORTAL_ORDER = frozenset({"Open", "OPEN", "READY_TO_SHIP"})


_RECIPES: dict[str, Callable[[Reads, Ask, Draft], None]] = {
    "task_status": task_status, "on_hand": on_hand, "lot_expiry": lot_expiry,
    "batch_status": batch_status, "po_status": po_status, "order_margin": order_margin,
    "open_ar": open_ar, "open_sos": open_sos,
}
# Recognised, so never read as another intent, but left to the agent: an item's price
# history is a plan of several answer pages (``price_history``, about 3 s a page) and a
# change list (``plx.changes``) pages too, past a fast answer's time.


def serves(question: str) -> bool:
    """Whether ``question`` is one a fast answer may settle (it may still decline)."""
    ask = understand(question)
    return ask is not None and ask.intent in _RECIPES


class _Choice(Exception):
    def __init__(self, choices: Choices) -> None:
        super().__init__("choices")
        self.choices = choices


# --------------------------------------------------------------------------- answer


@dataclass
class FastRun:
    """A fast answer, shaped as an agent session's run: its events (the reads and the gate
    decision), the final text (the answer block, already through ``gate_final_reply``)
    and the records it is about (``found``: ``roots`` and ``ambiguous`` as
    ``objective_runner.find_roots`` gives them)."""
    intent: str
    events: list[dict[str, Any]]
    final_text: str
    agent_text: str
    reply: dict[str, Any]
    found: dict[str, Any]
    seconds: float


def answer(question: str, reads: Reads, why: Optional[list[str]] = None
           ) -> Optional[FastRun]:
    """The fast answer to ``question`` for ``reads``'s reader, or None (module doc).
    ``why``: given, the code of a declined answer is appended to it (never a value)."""
    why = why if why is not None else []
    ask = understand(question)
    if ask is None or ask.intent not in _RECIPES:
        why.append("not_understood")
        return None
    ran = _answer(question, ask, reads, why)
    logging.getLogger("operating.fast_answer").info(json.dumps(
        {"intent": ask.intent, "answered": ran is not None, "declined": why[-1:] or None,
         "calls": len(reads.events)}))
    return ran


def _answer(question: str, ask: Ask, reads: Reads, why: list[str]) -> Optional[FastRun]:
    from .agent_session import gate_final_reply, host_submission
    started = time.monotonic()
    draft = Draft()
    try:
        with reads.request():
            try:
                _RECIPES[ask.intent](reads, ask, draft)
            except _Choice as picked:
                return _choices(ask, picked.choices, reads, started)
            text = " ".join(draft.sentences)
            block = {"format": ANSWER_FORMAT, "answerability": draft.answerability,
                     "text": text, "values": draft.values, "gaps": draft.gaps}
            agent_text = text + "\n\n```json\n" + json.dumps(block, ensure_ascii=False) \
                + "\n```"
            if draft.values:
                submission = host_submission([], agent_text)
                if submission is None or len(submission.get("claims") or ()) \
                        != len(draft.values):
                    why.append("submission_incomplete")
                    return None
                decided = reads.gate(submission)
                if decided.get("outcome") != "answered" \
                        or decided.get("unsupported_claims") \
                        or len(decided.get("claims") or ()) < len(draft.values):
                    why.append("gate_refused")
                    return None   # a refused claim is a gap: the agent answers
    except Unanswerable as declined:
        why.append(str(declined))
        return None
    final_text, reply = gate_final_reply(agent_text, reads.events, question)
    reads.reply = reply
    if reply.get("withheld") or reply.get("removed_sentences"):
        why.append("reply_screened")
        return None   # the gate kept less than the recipe read: the agent answers
    return FastRun(intent=ask.intent, events=reads.events, final_text=final_text,
                   agent_text=agent_text, reply=reply,
                   found={"roots": draft.roots, "ambiguous": []},
                   seconds=time.monotonic() - started)


def _choices(ask: Ask, choices: Choices, reads: Reads, started: float) -> FastRun:
    return FastRun(intent=ask.intent, events=reads.events, final_text="", agent_text="",
                   reply={}, found={"roots": [], "ambiguous": [{
                       "mention": choices.mention, "candidates": choices.candidates,
                       "more_candidates": 0, "chosen": None}]},
                   seconds=time.monotonic() - started)


class FastAgent:
    """The fast path on the agent contract (``agent_session.Agent``) for the scoreboard's
    runner: ``run(prompt, host)`` answers ``question`` in ``host``'s in-process session
    (``host.agent_session()``, as the app's Ask reads) and returns the run the runner
    records; a question it does not answer is an empty run (no answer)."""

    gates_final_reply = True

    def __init__(self, question: str) -> None:
        self.question = question
        self.last: Optional[FastRun] = None
        self.why: list[str] = []

    def run(self, prompt: str, host: Any) -> Any:
        from .agent_session import AgentRun
        _session_host, server = host.agent_session()
        try:
            reads = Reads.of_server(server)
            ran = answer(self.question, reads, self.why)
        finally:
            server.close()
        self.last = ran
        if ran is None or not ran.agent_text:
            return AgentRun(events=list(reads.events), final_text="",
                            usage={"subtype": "fast_path_declined"},
                            agent={"fast_path": "declined"})
        return AgentRun(events=list(ran.events), final_text=ran.agent_text,
                        usage={"subtype": "fast_path", "duration_ms": int(ran.seconds * 1000)},
                        agent={"fast_path": ran.intent})
