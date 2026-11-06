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
below that answers the whole question, or null>"}

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
        deadline = time.monotonic() + seconds
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
                result = message.get("result")
                return result if isinstance(result, str) else None

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


class FrontDoor:
    """The warm sessions and the read of one question (module doc). ``spawn``: tests
    replace the session factory (``argv, env -> session with turn/close``)."""

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

    @staticmethod
    def enabled() -> bool:
        return (os.environ.get(SWITCH) or "").strip().lower() != "off"

    def argv(self) -> list[str]:
        argv = [self.binary, "-p", "--input-format", "stream-json", "--output-format",
                "stream-json", "--verbose", "--setting-sources", "", "--tools", "",
                "--no-session-persistence"]
        return argv + (["--model", self.model] if self.model else [])

    def warm(self) -> None:
        """Start sessions in the background until ``size`` are free or starting."""
        with self._lock:
            missing = self.size - self._free.qsize() - self._starting - self._busy
            self._starting += max(0, missing)
        for _ in range(max(0, missing)):
            threading.Thread(target=self._start, daemon=True,
                             name="front-door-warm").start()

    def _start(self) -> None:
        try:
            from operating.agent_environment import agent_environment
            session = self._spawn(self.argv(), agent_environment(os.environ))
            ready = _parse(session.turn(RULES, 60.0))
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
            reply = _parse(session.turn(json.dumps(
                {"question": question, "earlier": list(earlier)[-3:]}, ensure_ascii=False),
                self.seconds))
            if reply is None:
                keep = False
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
            return Reading(question, " ".join(rewrite.split()), route, status,
                           time.monotonic() - started,
                           recipe if recipe in RECIPES else None)
        finally:
            with self._lock:
                self._busy -= 1
            if keep and session.turns < TURNS:
                self._free.put(session)
            else:
                session.close()
                self.warm()


__all__ = ["FrontDoor", "Reading", "RULES", "grounded"]
