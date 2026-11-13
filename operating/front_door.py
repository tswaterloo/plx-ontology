"""The front door: every question is read first by a warm model session, the way Claude
reads a prompt, before any route sees it.

A person types however they type (typos, shorthand, dictation, missing words, a
follow-up that says "it"). The model restates the question as one clear, complete
question and says whether it is a lookup (one thing about one record, or one standard
list) or an investigation (several records walked, totalled or compared). The routes
then read the clean question: the deterministic, cited recipes (``fast_answer``, the
late list) answer it when they fit, the agent otherwise; a lookup the recipes decline
still goes to the agent. No word list, lexicon or rule here understands anything: the
model does.

* **Warm.** ``FrontDoor`` keeps ``POOL`` headless Claude Code sessions open
  (``claude -p --input-format stream-json``, the operator's own login,
  ``agent_environment``: no paid API key passes), each primed with ``RULES`` once, so a
  question costs one model turn, not a process start. A session is retired after
  ``TURNS`` questions (its context stays short) and replaced in the background.
* **Grounded.** A restatement may fix wording, never add a subject: every identifier in
  it must be one the question (or the conversation) wrote, and every capitalised name
  must be grounded in them (``query_understanding._grounded_text``). An ungrounded
  restatement, a reply that is not the JSON asked for, no free session or no reply
  within ``SECONDS`` is None: the question goes on as typed (the fallback).
* **Scope.** The model also says whether PLX's records, general knowledge or both answer
  the question (``general_answer``); anything else it says is ``company``.
* **Display.** The answer keeps the person's own question; the restatement is shown as
  how it was read.

Nothing here reads a source or a record; the model sees only the question and the
conversation's earlier questions.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable, Optional, Sequence

POOL = 2
# The ready answers the model may pick (``operator_api.backend.ask``'s shortcuts).
RECIPES = ("late_list", "at_risk", "profit")
QUEUE_SECONDS = 8.0   # a question waits this long for a free session, then routes as typed
TURNS = 20
SECONDS = 12.0
SETTLE_SECONDS = 90.0   # a session whose turn timed out has this long to send its reply
MODEL_SETTING = "PLX_FRONT_DOOR_MODEL"
SWITCH = "PLX_FRONT_DOOR"          # "off" turns the front door off

RULES = """You are the front door of the PLX Workbench, where employees of a contract \
manufacturer ask about purchase orders, sales orders, items, lots, batches, assembly \
orders, customers, suppliers, invoices, inventory, costs and their mail and files.

For each message you get {"question": ..., "earlier": [earlier questions of the same \
conversation]}. Read the question the way a colleague would, however it is typed: typos, \
shorthand, letters spoken one by one, dictation errors, missing words, and references to \
the earlier questions. Then reply with JSON only, no other text:

{"rewrite": "<the question restated as one clear, complete question in plain business \
English>", "route": "lookup" | "investigate", "status": "<3 to 8 words saying what \
you will look up, e.g. Looking up late purchase orders>", "recipe": "<one recipe name \
below that answers the whole question, or null>", "scope": "company" | "general" | "mixed", \
"open": null | {"what": "<the one choice the answer depends on, in a few words>", \
"among": "<the kind of record the choice is among, one of the record kinds below, or \
null when it is not a record>"}}

open says whether the answer turns on a choice the person has not made: two plausible \
readings of the question would give materially different answers (which of several \
records a name could mean, which customer or product, which period or measure, which \
template or recipient a document needs), and neither the question nor the earlier \
questions settle it. A question about one record that does not say which (the \
customer, the product, the order, "of batch", "this PO", a record kind with no number, \
code or name) is open, among that kind, \
unless an earlier question names it. A question that names its record by number, code \
or name, or asks about all of something, or whose reading any colleague would take for \
granted, is not open: null. Only flag it; never refuse or answer the question here.
Record kinds: <KINDS>

scope says what answers the question:
- company: PLX's own records (its orders, items, lots, customers, suppliers, people, \
costs, inventory, mail or files).
- general: general knowledge, reasoning, math or writing, with nothing from PLX's \
records: explaining a concept, industry practice, a calculation from figures the \
question gives, planning advice, or drafting an email, policy or job description that \
names no PLX record.
- mixed: both, such as PLX's own figures judged against general practice, or a draft \
about a PLX record the question names.
When unsure between company and anything else, say company; a general question gets \
recipe null.

Recipes (ready answers the Workbench computes from its records):
- late_list: the list of the reader's late purchase orders, all of them, with nothing \
else asked about them.
- at_risk: which customer orders late purchase orders put at risk (all of them, or those \
of one customer or order).
- profit: the margin or profit of a customer or of one sales order.
Name a recipe only when it answers everything the question asks; otherwise null.

Rules for the rewrite:
- Keep every identifier, code, record number, name, quantity and date exactly as written \
(you may join letters spoken apart into the word they spell, and fix the case). Never \
invent or complete a record number, code, name or figure, and never add a subject the \
question does not have; resolve "it" or "that one" only from the earlier questions.
- Expand shorthand into the words it stands for and fix misspellings of ordinary words.
- If the question is already clear, repeat it unchanged.

route is "lookup" when the question asks for one fact about one record, or one list as \
the records keep it; "investigate" when answering needs several records combined, \
totalled, compared or explained.

Reply to this message with {"ready": true} only."""


@dataclass(frozen=True)
class Reading:
    """How the front door read a question: the restatement, its route, a progress line."""
    question: str
    rewrite: str
    route: str
    status: str
    seconds: float
    recipe: Optional[str] = None
    # what answers it (``general_answer.SCOPES``): anything the model did not say is
    # "company", the evidence path (``general_answer.scope_of`` decides the route)
    scope: str = "company"
    # the choice the answer turns on that the person has not made (``clarify``):
    # {"what": words, "among": a record type or None}; None when the question is settled
    open: Optional[dict[str, Any]] = None

    @property
    def changed(self) -> bool:
        return " ".join(self.rewrite.split()).casefold() != \
            " ".join(self.question.split()).casefold()


class _Session:
    """One warm headless Claude Code session (stream-json in and out)."""

    def __init__(self, argv: Sequence[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, env=env, start_new_session=True)
        self.turns = 0
        self.owed = 0   # results of turns that timed out, still to arrive (``settle``)
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True,
                         name="front-door-reader").start()

    def _pump(self) -> None:
        for line in self.proc.stdout:   # type: ignore[union-attr]
            self.lines.put(line)
        self.lines.put(None)

    def turn(self, text: str, seconds: float) -> Optional[str]:
        """Send one user message; the result text, or None (ended, timed out)."""
        if self.proc.poll() is not None:
            return None
        try:
            self.proc.stdin.write(json.dumps(   # type: ignore[union-attr]
                {"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            self.proc.stdin.flush()            # type: ignore[union-attr]
        except (BrokenPipeError, OSError, ValueError):
            return None
        self.turns += 1
        self.owed += 1
        found = self._result(time.monotonic() + seconds)
        return found if isinstance(found, str) else None

    def _result(self, deadline: float) -> Any:
        """The next result line's text (a non-text result: ``False``), or None by the
        deadline or at the session's end; each result read pays one owed turn."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("type") == "result":
                self.owed -= 1
                result = message.get("result")
                return result if isinstance(result, str) else False

    def settle(self, seconds: float) -> bool:
        """Read the late results of the turns that timed out (dropped: their question
        was answered without them), within ``seconds``; True when none is owed now and the
        session still runs, so its next turn reads its own result."""
        deadline = time.monotonic() + seconds
        while self.owed > 0:
            if self._result(deadline) is None:
                return False
        return self.proc.poll() is None

    def close(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


def _parse(text: Optional[str]) -> Optional[dict[str, Any]]:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def grounded(rewrite: str, question: str, earlier: Sequence[str]) -> bool:
    """Whether ``rewrite`` adds no subject the question and the conversation lack: each
    identifier in it is one they wrote (any form the grammar reads it in), and each
    capitalised word without a digit is grounded in their words (a near spelling, a
    demonym's place) or is scaffolding, a business noun or a calendar word, as query
    understanding checks a model rewrite (``query_understanding._grounded_text``)."""
    from operating.query_understanding import (DOMAIN_WORDS, SCAFFOLD, _CALENDAR_WORDS,
                                               _TOKEN_RE, _grounded, _grounding_words,
                                               fold, identifier_leads)
    said = " ".join([question, *earlier])
    asked = {form for lead in identifier_leads(said) for form in lead.forms}
    for lead in identifier_leads(rewrite):
        if not set(lead.forms) & asked:
            return False
    words = _grounding_words(question, list(earlier))
    for token in _TOKEN_RE.findall(rewrite):
        word = fold(token)
        if token[:1].isupper() and word and not re.search(r"\d", token) \
                and not all(_grounded(part, words) or part in SCAFFOLD
                            or part in DOMAIN_WORDS or part in _CALENDAR_WORDS
                            for part in word.split()):
            return False
    return True


def _status_grounded(status: str, question: str, earlier: Sequence[str]) -> bool:
    """A progress line names no record the question did not: its identifiers only."""
    from operating.query_understanding import identifier_leads
    asked = {f for lead in identifier_leads(" ".join([question, *earlier])) for f in lead.forms}
    return all(set(lead.forms) & asked for lead in identifier_leads(status))


_KINDS: list[str] = []


def record_kinds() -> list[str]:
    """The ontology's record kinds a person asks about (its object types, the source
    relations' mirror types left out), read once from the schema; [] when unreadable."""
    if not _KINDS:
        try:
            from ontology.schema import load_schema
            _KINDS.extend(sorted(t for t in load_schema().object_types
                                 if not t.startswith("Rel")))
        except Exception:  # noqa: BLE001 - no kinds named: "among" is then unchecked
            return []
    return list(_KINDS)


def open_point(said: Any) -> Optional[dict[str, Any]]:
    """The model's ``open`` as {"what", "among"}, or None: anything but an object with a
    short ``what`` is no open point (the question goes on as read)."""
    if not isinstance(said, dict):
        return None
    what, among = said.get("what"), said.get("among")
    if not isinstance(what, str) or not what.strip() or len(what) > 200:
        return None
    kinds = record_kinds()
    among = among if isinstance(among, str) and re.fullmatch(r"[A-Z][A-Za-z0-9]{1,63}",
                                                              among) \
        and (among in kinds or not kinds) else None
    return {"what": " ".join(what.split()), "among": among}


class FrontDoor:
    """The warm sessions and the read of one question (module doc). ``spawn``: tests
    replace the session factory (``argv, env -> session with turn/close``). ``rules``:
    what each session is primed with (``operating.clarify.Clarifier`` primes its own)."""

    rules = RULES
    ledger = "front_door"
    turns_kept = TURNS   # a session is retired after this many turns

    def __init__(self, *, binary: str = "claude", model: Optional[str] = None,
                 pool: int = POOL, spawn: Optional[Callable[..., Any]] = None,
                 seconds: float = SECONDS) -> None:
        self.binary = binary
        self.model = model if model is not None else (os.environ.get(MODEL_SETTING) or None)
        self.size, self.seconds = max(1, pool), seconds
        self._spawn = spawn or _Session
        self._free: "queue.Queue[Any]" = queue.Queue()
        self._lock = threading.Lock()
        self._starting = 0
        self._busy = 0   # sessions reading a question now
        self._settling = 0   # sessions reading a late reply before their next question

    @staticmethod
    def enabled() -> bool:
        return (os.environ.get(SWITCH) or "").strip().lower() != "off"

    def primer(self) -> str:
        """What a new session is primed with: ``rules``, the record kinds named."""
        return self.rules.replace("<KINDS>", " ".join(record_kinds()))

    def argv(self) -> list[str]:
        argv = [self.binary, "-p", "--input-format", "stream-json", "--output-format",
                "stream-json", "--verbose", "--setting-sources", "", "--tools", "",
                "--no-session-persistence"]
        return argv + (["--model", self.model] if self.model else [])

    def warm(self) -> None:
        """Start sessions in the background until ``size`` are free or starting."""
        with self._lock:
            missing = (self.size - self._free.qsize() - self._starting - self._busy
                       - self._settling)
            self._starting += max(0, missing)
        for _ in range(max(0, missing)):
            threading.Thread(target=self._start, daemon=True,
                             name="front-door-warm").start()

    def _start(self) -> None:
        try:
            from operating.agent_environment import agent_environment
            session = self._spawn(self.argv(), agent_environment(os.environ))
            ready = _parse(session.turn(self.primer(), 60.0))
            if ready and ready.get("ready"):
                self._free.put(session)
            else:
                session.close()
        except Exception:  # noqa: BLE001 - a session that cannot start is no session
            pass
        finally:
            with self._lock:
                self._starting -= 1

    def read(self, question: str, earlier: Sequence[str] = ()) -> Optional[Reading]:
        """How the model reads ``question`` (module doc), or None: no free session, no
        reply in time, not the JSON asked for, or a restatement that adds a subject."""
        started = time.monotonic()
        reply = self.reply({"question": question, "earlier": list(earlier)[-3:]})
        if reply is None:
            return None
        rewrite = reply.get("rewrite")
        route = reply.get("route")
        status = reply.get("status")
        if not isinstance(rewrite, str) or not rewrite.strip() or len(rewrite) > 1000 \
                or route not in ("lookup", "investigate"):
            return None
        if not grounded(rewrite, question, earlier):
            return None
        status = status.strip()[:80] if isinstance(status, str) and status.strip() \
            and _status_grounded(status, question, earlier) else "Reading your question"
        recipe = reply.get("recipe")
        scope = reply.get("scope")
        return Reading(question, " ".join(rewrite.split()), route, status,
                       time.monotonic() - started,
                       recipe if recipe in RECIPES else None,
                       scope if scope in ("general", "mixed") else "company",
                       open_point(reply.get("open")))

    def reply(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        """One turn of a warm session: ``message`` as JSON in, the JSON object it replies
        with out, or None (no free session in time, no reply in time, not JSON)."""
        self.warm()   # a session that ended is replaced; never more than the pool
        try:
            # questions beyond the pool wait their turn for a warm session
            session = self._free.get(timeout=QUEUE_SECONDS)
        except queue.Empty:
            return None
        with self._lock:
            self._busy += 1
        keep = True
        try:
            from operating import model_ledger
            asked = json.dumps(message, ensure_ascii=False)
            turn_started = time.monotonic()
            reply = _parse(session.turn(asked, self.seconds))
            # one turn of a warm session: the reading's own seconds, not the queue wait
            model_ledger.record(self.ledger, prompt=asked, ok=reply is not None, turns=1,
                                seconds=time.monotonic() - turn_started)
            if reply is None:
                keep = False
            return reply
        finally:
            with self._lock:
                self._busy -= 1
            if keep and session.turns < self.turns_kept:
                self._free.put(session)
            elif not keep and getattr(session, "owed", 0) > 0 and session.turns < self.turns_kept:
                # Timed out, not ended: the warm session reads its late reply on its own
                # thread and serves again, rather than a cold one starting in its place.
                with self._lock:
                    self._settling += 1
                threading.Thread(target=self._settle, args=(session,), daemon=True,
                                 name="front-door-settle").start()
            else:
                session.close()
                self.warm()

    def _settle(self, session: Any) -> None:
        try:
            settled = session.settle(SETTLE_SECONDS)
        except Exception:  # noqa: BLE001 - a session that cannot settle is replaced
            settled = False
        with self._lock:
            self._settling -= 1
        if settled:
            self._free.put(session)
        else:
            session.close()
            self.warm()


__all__ = ["FrontDoor", "Reading", "RULES", "grounded", "open_point"]
