"""The app's Ask on the scoreboard's path: one headless Claude Code session per question,
as the card runner answers a card, for the signed-in reader.

What runs is the card runner's real-agent path, reused, never copied (it lives in
``operating.agent_session``; ``evals.runner`` re-exports it):

* the agent: ``operating.agent_session.ClaudeCodeAgent`` (read-only ``plx.*`` tools behind the tool
  gate, no built-in tools, the operator's Claude Code login, no paid API), running the
  investigate agent (``AGENT``, the scoreboard's ``CARD_AGENT``);
* the prompt: ``app_prompt``: ``agent_session.ask_prompt`` (``task_prompt``, as for an
  untimed card: the answer contract, the citation and number rules, the answer-gate
  rule), unchanged, then ``APP_READER_RULE`` (the prose is for a business reader: no
  gate, claim, null, field path or handle talk). The scoreboard's prompt never carries it;
* the host: ``agent_session.ReaderHost`` (the scoreboard's ``LiveHost`` builds on it) over the API's own state and host config,
  as the *reader*: a fresh single-use agent ticket minted for the signed-in employee
  (their Entra object id, tenant, email and Portal user id), so the host resolves that
  employee's grant and trims every read for them; the people exclusion
  (``PLX_EXCLUDED_PEOPLE_*``) travels to the host as for the scoreboard. A reader without
  an employee identity (a fixture session) never gets a session: ``ReaderRefused``;
* the final reply: ``agent_session.gated_run`` (``gate_final_reply`` over the session's own
  answer-gate decisions): what the reader sees is only what the Workbench's gate passed.

``AgentAsk.run`` streams the session's progress (each tool call as it starts, the gate's
decision as it returns) through ``emit`` and stops the whole session at ``seconds``: a
stopped session answers with what its last gate decision passed, else says it is still
working, never nothing.

Evidence: injected-transport in the tests (a scripted session stream); live-unverified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping, Optional

from operating import deep_investigation as deep

AGENT = "plx.agent.investigate"
ASK_SECONDS = deep.SESSION_SECONDS   # the hard cap of one Ask's session, and its budget
MAX_TURNS = deep.MAX_TURNS           # as the scoreboard's sessions
MAX_STEPS = deep.MAX_STEPS           # tool calls besides the answer gate's
CATALOG_SECONDS = 3600     # a reader's tool catalog is listed again after this
# The model an Ask about one named record runs on (``ClaudeCodeAgent.routed``: the
# questions the short method is asked for); any other question stays on the session's.
# None: every Ask on the session's model.
LOOKUP_MODEL: Optional[str] = None


# The app's reader is a business user: the words around the answer block say what the
# sources show and what could not be confirmed, never how the check works. Only the app's
# Ask adds it (``app_prompt``); the scoreboard's cards are asked ``ask_prompt`` alone.
APP_READER_RULE = (
    "Who reads your reply: a PLX employee in the Workbench app, not an engineer. Outside "
    "the json block (and in its \"text\"), write plain business sentences: name records "
    "as the business does (a PO by its number, a document by its title) "
    "and fields in words (the PO line's description, the expected receipt date). Never "
    "mention the answer gate, checks, claims, quotes, null values, answer values, field "
    "paths (row.<column>, text), citation handles (original://, bc://, api://) or gap "
    "codes (snake_case). Where you could not confirm something, say \"I couldn't confirm "
    "<what> from a source you can open\" and nothing about why the check refused it. "
    "A value or quote taken from a document is copied exactly as the page writes it, its "
    "capital letters included (\"The supplier map ...\", not \"the supplier map ...\"); "
    "lower-case it only in your own sentence, never in the json block. "
    "This changes only your wording; the json block and every rule above stay as they "
    "are.")


PREREAD_HEADING = ("Tool output already read for you, as this reader, before the session "
                   "(treat it as any tool result: state a value from it only through a "
                   "claim the answer gate passes, citing the record it names):\n")


def app_prompt(question: str) -> str:
    """The app's Ask task: the scoreboard's ``ask_prompt`` unchanged, then
    ``APP_READER_RULE``."""
    from operating import agent_session as runner
    return runner.ask_prompt(question) + "\n\n" + APP_READER_RULE


class ReaderRefused(LookupError):
    """The reader cannot run the agent path (no employee identity); a code, no identity."""


def plain_tree(value: Any) -> Any:
    """A tool result with every ``{"untrusted_text": ...}`` envelope replaced by its text."""
    if isinstance(value, Mapping):
        if "untrusted_text" in value and set(value) <= {"untrusted_text", "origin"}:
            return value["untrusted_text"]
        return {key: plain_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain_tree(item) for item in value]
    return value


def ticket_claims(session: Any) -> dict[str, str]:
    """The agent ticket's claims for a signed-in employee session: the employee's own
    Entra tenant and object id, the email the resolver matched and the Portal user id the
    session was signed in with (else the object id). Fails closed: a session with no
    employee or no email is ``ReaderRefused``, never another identity."""
    employee = getattr(session, "employee", None)
    if employee is None or getattr(employee, "kind", None) != "employee":
        raise ReaderRefused("reader_not_an_employee")
    email = getattr(session, "email", None) or next(
        (i for i in sorted(getattr(session, "identities", ()) or ()) if "@" in i), None)
    if not email:
        raise ReaderRefused("reader_email_unknown")
    return {"uid": str(getattr(session, "portal_user_id", None) or employee.object_id),
            "email": email, "entraTenantId": employee.tenant_id,
            "entraObjectId": employee.object_id}


@dataclass
class AskRun:
    """One Ask's session: the gated reply, its parsed answer, the session's events and the
    last gate decision (envelopes removed), whether it was stopped at the cap."""
    final_text: str
    answer: Optional[dict[str, Any]]
    events: list[dict[str, Any]]
    decision: Optional[dict[str, Any]]
    stopped: bool
    seconds: float
    usage: dict[str, Any] = field(default_factory=dict)
    final_reply: dict[str, Any] = field(default_factory=dict)
    # why the session itself failed (``session_failure``): the assistant was unavailable,
    # which is never "no records answer this"; None for a session that ran
    failure: Optional[str] = None


# The Claude Code CLI's own words when the login's quota is spent ("You've hit your weekly
# limit", "Claude AI usage limit reached") or the login is not usable; matched only on a
# session that produced no answer and ended in an error (``session_failure``).
LIMIT_TEXT = re.compile(r"hit your (?:\w+[ -])?limit|usage limit|limit reached|rate[ -]limit"
                        r"|out of (?:extra )?usage|quota (?:exceeded|reached)|overloaded",
                        re.I)
AUTH_TEXT = re.compile(r"invalid api key|run /login|please log ?in|not logged in"
                       r"|oauth token (?:has )?expired|authentication[_ ](?:error|failed)"
                       r"|unauthori[sz]ed|credit balance is too low", re.I)
FAILURES = ("usage_limit", "sign_in", "timeout", "error")


def session_failure(final_text: str, usage: Mapping[str, Any], events: list,
                    answer: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Why a session failed as a session (a code of ``FAILURES``), or None when it ran:
    ``usage_limit`` (the CLI says the login's limit is hit), ``sign_in`` (the login is not
    usable), ``timeout`` (stopped at the cap with nothing passed is the caller's), ``error``
    (the CLI ended in an error or a non-zero exit with no answer). A session whose answer
    block parsed is never a failure."""
    if answer is not None:
        return None
    text = final_text or ""
    errored = bool(usage.get("is_error")) or usage.get("exit_code") not in (0, None) \
        or (not events and not text.strip())
    if usage.get("subtype") == "timeout":
        return None
    if not errored and events:
        return None
    if LIMIT_TEXT.search(text):
        return "usage_limit"
    if AUTH_TEXT.search(text):
        return "sign_in"
    return "error" if errored else None


def last_decision(events: list[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """The session's last readable answer-gate decision (``plx.case_get`` answer mode)
    that passed at least one claim, envelopes removed; None when there is none."""
    from operating import agent_session as runner

    found = None
    for event in events:
        arguments = event.get("arguments")
        result = event.get("result")
        if event.get("type") == "tool_call" and event.get("name") == runner.ANSWER_GATE_TOOL \
                and isinstance(arguments, Mapping) and "answer" in arguments \
                and not event.get("is_error") and isinstance(result, Mapping) \
                and isinstance(result.get("claims"), list) and result["claims"]:
            found = plain_tree(result)
    return found


class _Catalogued:
    """A ``LiveHost`` whose tool catalog (the agent's, as this reader's host lists it) is
    listed once per reader and kept ``CATALOG_SECONDS``: listing it starts a second host
    process, which would cost every Ask its start-up. The catalog only names the tools the
    gate lets through (read-only ones); every call is still decided by the reader's host."""

    _lock = threading.Lock()
    _held: dict[tuple, tuple[float, list]] = {}

    def __init__(self, host: Any, key: tuple) -> None:
        self._host, self._key = host, key

    def __getattr__(self, name: str) -> Any:
        return getattr(self._host, name)

    def tools(self) -> list[dict[str, Any]]:
        with self._lock:
            held = self._held.get(self._key)
        if held is not None and held[0] > time.monotonic():
            return held[1]
        listed = self._host.tools()
        if listed:
            with self._lock:
                self._held[self._key] = (time.monotonic() + CATALOG_SECONDS, listed)
        return listed


class AgentAsk:
    """The scoreboard's agent path for the API's readers (module doc). ``host_for`` and
    ``agent_for`` (tests): stand-ins for the reader's ``LiveHost`` and the session's agent
    (``agent_for(on_line)``, whose stream lines it must hand to ``on_line``)."""

    def __init__(self, *, state_dir: Path | None = None, host_config: Path | None = None,
                 secret: str | None = None, seconds: float = ASK_SECONDS,
                 claude_bin: str = "claude", model: str | None = None,
                 max_turns: int = MAX_TURNS, lookup_model: str | None = LOOKUP_MODEL,
                 host_for: Callable[[Any], Any] | None = None,
                 agent_for: Callable[[Callable[[str], None]], Any] | None = None) -> None:
        self.state_dir, self.host_config, self.secret = state_dir, host_config, secret
        self.seconds, self.claude_bin, self.model = seconds, claude_bin, model
        self.max_turns, self.lookup_model = max_turns, lookup_model
        self._host_for, self._agent_for = host_for, agent_for

    @classmethod
    def from_args(cls, state_dir: Path, host_config: Path | None,
                  environ: Mapping[str, str], **options: Any) -> Optional["AgentAsk"]:
        """The API's agent path, or None when it cannot run here (no host config or no
        session secret): the Ask then stays on the console's objective box."""
        import shutil
        from operating.session_identity import session_secret

        secret = session_secret(environ)
        if host_config is None or not Path(host_config).is_file() or not secret \
                or shutil.which(options.get("claude_bin") or "claude") is None:
            return None
        return cls(state_dir=Path(state_dir), host_config=Path(host_config), secret=secret,
                   **options)

    def serves(self, session: Any) -> bool:
        """Whether this reader gets the agent path: an employee session (a stand-in host
        in the tests serves any)."""
        if self._host_for is not None:
            return True
        try:
            ticket_claims(session)
        except ReaderRefused:
            return False
        return True

    # ---- the reader's host and the session's agent ------------------------------------

    def host_for(self, session: Any) -> Any:
        if self._host_for is not None:
            return self._host_for(session)
        from operating import agent_session

        claims = ticket_claims(session)
        config = json.loads(Path(self.host_config).read_text(encoding="utf-8"))
        extra = ({name: os.environ[name] for name in agent_session.GRAPH_ENV
                  if os.environ.get(name)} if config.get("entra_directory") else {})
        host = agent_session.ReaderHost(self.state_dir, self.host_config, claims, self.secret,
                                        tenant=str(config.get("tenant") or "plx"),
                                        extra_env=extra, agent=AGENT)
        return _Catalogued(host, (str(self.host_config), AGENT, claims["entraObjectId"]))

    def agent_for(self, on_line: Callable[[str], None]) -> Any:
        if self._agent_for is not None:
            return self._agent_for(on_line)
        from operating import agent_session as runner
        # The deep-investigation budget is the Ask's own cap: research ends before it, so
        # the session submits what it established rather than being stopped mid-search.
        return runner.ClaudeCodeAgent(binary=self.claude_bin, model=self.model,
                                      timeout=int(self.seconds), max_turns=self.max_turns,
                                      spawn=runner.streaming_spawn(on_line),
                                      budget_seconds=self.seconds, max_steps=MAX_STEPS,
                                      lookup_model=self.lookup_model)

    def read_source(self, session: Any, handle: str, text_offset: int = 0
                    ) -> dict[str, Any]:
        """A captured original (``original://``) or SharePoint file (``sharepoint://``) as
        this reader's own host reads it: its
        ``plx.read`` through the capture store's per-reader gate
        (``operating.captured_originals``), the gate the agent's reads passed. Read-only;
        envelopes removed. ``KeyError`` when the host does not serve it to this reader
        (refused, unknown or unreadable alike), ``ReaderRefused`` for a reader with no
        employee identity."""
        if not isinstance(handle, str) or not handle.startswith(("original://",
                                                                  "sharepoint://")):
            raise KeyError("not_a_captured_original")
        host = self.host_for(session)
        try:
            arguments: dict[str, Any] = {"citation": handle}
            if text_offset:
                arguments["text_offset"] = int(text_offset)
            got = host.call("plx.read", arguments)
        finally:
            close = getattr(host, "close", None)
            if close is not None:
                close()
        got = plain_tree(got) if isinstance(got, Mapping) else {}
        if got.get("error") or got.get("status") not in ("resolved", "changed_since"):
            raise KeyError("citation_unresolvable_for_actor")
        return dict(got)

    # ---- one Ask ---------------------------------------------------------------------

    def run(self, session: Any, question: str,
            emit: Callable[[str, Mapping[str, Any]], None] = lambda kind, data: None,
            context: str = "") -> AskRun:
        """One question as the reader (``ReaderRefused`` before any session for a reader
        with no employee identity). ``emit(kind, data)``: ``("call", {"tool": name})`` as
        each tool call starts; ``("result", {"tool", "step"})`` as another tool's result
        comes back (``step``: its plain-words step, ``ask_progress.result_step``, or None);
        ``("gate", {"outcome", "verdict", "citations"})`` as each gate decision returns
        (``verdict``: an ``answered`` decision's first sentence, ``ask_progress.verdict``;
        ``citations``: what its passed claims cite, for the caller to decide for the
        reader before showing it)."""
        from operating import agent_session as runner
        from . import ask_progress as progress

        host = self.host_for(session)   # ReaderRefused first: no process for a non-employee
        started = time.monotonic()
        pending: dict[str, tuple[str, Any]] = {}
        refused: list[Any] = []

        def on_line(line: str) -> None:
            message = json.loads(line) if line.strip().startswith("{") else {}
            kind = message.get("type")
            content = (message.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else ():
                if not isinstance(block, Mapping):
                    continue
                if kind == "assistant" and block.get("type") == "tool_use":
                    raw = str(block.get("name") or "")
                    name = raw.split("__", 2)[-1].replace("_", ".", 1) if raw.startswith(
                        "mcp__") else raw
                    pending[str(block.get("id"))] = (name, block.get("input"))
                    emit("call", {"tool": name})
                elif kind == "user" and block.get("type") == "tool_result":
                    name, arguments = pending.pop(str(block.get("tool_use_id")), ("", None))
                    if name != runner.ANSWER_GATE_TOOL:
                        if name:
                            emit("result", {"tool": name, "step": progress.result_step(
                                name, arguments, block.get("content"),
                                block.get("is_error"))})
                        continue
                    body = block.get("content")
                    if isinstance(body, list):
                        body = "".join(str(part.get("text") or "") for part in body
                                       if isinstance(part, Mapping))
                    try:
                        parsed = json.loads(body) or {}
                        outcome = parsed.get("outcome")
                    except (TypeError, ValueError, AttributeError):
                        parsed, outcome = {}, None
                    told = None
                    if isinstance(parsed, Mapping) and not block.get("is_error"):
                        refused.extend(parsed.get("unsupported_claims") or ())
                        told = progress.verdict(parsed, question, refused)
                    emit("gate", {"outcome": outcome if isinstance(outcome, str) else None,
                                  "verdict": told, "citations": progress.citations(parsed)
                                  if told else None})

        try:
            # ``context``: what the app read for the agent before the session (a tool
            # output, ``operator_api.preread``). It goes into the task, never into the
            # question the final reply is screened against: a number or id in it is
            # stated only through a claim the gate passed, like any other tool output's.
            prompt = app_prompt(question) + (
                "\n\n" + PREREAD_HEADING + context.strip() if context.strip() else "")
            run = runner.gated_run(self.agent_for(on_line), prompt, host, question)
        finally:
            close = getattr(host, "close", None)
            if close is not None:
                close()
        from operating.answer_contract import parse_answer
        answer, _error = parse_answer(run.final_text)
        stopped = run.usage.get("subtype") == "timeout"
        reply = dict(run.agent.get("final_reply") or {})
        # One line per Ask for whoever runs the app: counts and enums only, never the
        # question, a value, a citation or the reader.
        logging.getLogger("operator_api.ask").info(json.dumps({
            "seconds": round(time.monotonic() - started, 1), "stopped": stopped,
            "subtype": run.usage.get("subtype"), "turns": run.usage.get("num_turns"),
            "tool_calls": len(run.events), "answer_parsed": answer is not None,
            "steps": (run.agent.get("budget") or {}).get("steps_used"),
            "refused_after_budget": (run.agent.get("budget") or {}).get(
                "refused_after_budget"),
            "gate": (run.agent.get("answer_gate") or {}).get("outcomes"),
            "removed_sentences": reply.get("removed_sentences"),
            "withheld_values": len(reply.get("withheld") or ()),
            "prose_chars": len(reply_prose(run.final_text, answer))}))
        # the CLI's own message (a limit or sign-in notice) before the gate screened it
        failure = session_failure(str(reply.get("agent_final_text") or run.final_text or ""),
                                  run.usage, run.events, answer)
        if failure is not None:
            logging.getLogger("operator_api.ask").warning(json.dumps(
                {"session_failed": failure, "subtype": run.usage.get("subtype"),
                 "exit_code": run.usage.get("exit_code")}))
        return AskRun(final_text=run.final_text, answer=answer, events=run.events,
                      decision=last_decision(run.events), stopped=stopped,
                      seconds=time.monotonic() - started, usage=dict(run.usage),
                      final_reply=reply, failure=failure)


def reply_prose(final_text: str, answer: Optional[Mapping[str, Any]]) -> str:
    """The reader's answer in sentences: the gated message around the answer block (the
    agent's prose, every sentence the gate could not back already removed), else the
    answer's own text; never the json block."""
    from operating.answer_contract import _JSON_FENCE

    prose = _JSON_FENCE.sub(" ", final_text or "")
    prose = "\n".join(line.rstrip() for line in prose.strip().splitlines())
    while "\n\n\n" in prose:
        prose = prose.replace("\n\n\n", "\n\n")
    text = str((answer or {}).get("text") or "").strip()
    return prose.strip() or text
